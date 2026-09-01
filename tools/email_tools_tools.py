# SPDX-License-Identifier: Apache-2.0
"""
AiNxt Agentic Platform — email_tools MCP tools.

Read mailboxes (local .eml files) and draft replies to an outbox. Used by
UC-86 (executive inbox triage) and UC-64 (candidate follow-up drafting).

**Sending is intentionally absent** — drafts only. A separate gated tool
with critical=true handles outbound send + audit.

Functions exposed:
  list_messages  — list message ids/from/subject/date from the mailbox
  read_message   — read full body of a message by id
  draft_reply    — write a DRAFT reply .eml to the outbox

Companion server: mcp/servers/email_tools_server.py
Registered in:   mcp/registry.py:_register_tools()

Configuration (env vars):
  EMAIL_TOOLS_DATA_DIR    — root holding .eml mailbox files (default ./data/email)
  EMAIL_TOOLS_OUTBOX_DIR  — where draft .eml replies are written
                            (default ./outbox/mcp/email)
"""

import email
import os
import re
from typing import List


# ── Configuration ────────────────────────────────────────────────────────────

_DATA_DIR   = os.getenv("EMAIL_TOOLS_DATA_DIR",   "./data/email")
_OUTBOX_DIR = os.getenv("EMAIL_TOOLS_OUTBOX_DIR", "./outbox/mcp/email")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mailbox() -> List[dict]:
    out: List[dict] = []
    for r, _, files in os.walk(_DATA_DIR):
        for f in sorted(files):
            if f.endswith(".eml"):
                msg = email.message_from_file(open(os.path.join(r, f)))
                out.append({
                    "id":      f,
                    "path":    os.path.join(r, f),
                    "from":    msg["From"],
                    "subject": msg["Subject"],
                    "date":    msg["Date"],
                })
    return out


# ── Tool functions ───────────────────────────────────────────────────────────

def list_messages() -> List[dict]:
    """List messages (id, from, subject, date) in the configured mailbox."""
    return [{k: m[k] for k in ("id", "from", "subject", "date")} for m in _mailbox()]


def _decode_part(part) -> str:
    """Decode one MIME part's payload to text, honouring transfer-encoding + charset."""
    try:
        raw = part.get_payload(decode=True)
    except Exception:
        raw = None
    if raw is None:
        payload = part.get_payload()
        return payload if isinstance(payload, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, AttributeError):
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return str(raw)


def _extract_body(msg) -> str:
    """Extract the readable body from a (possibly nested multipart) message.

    Fix #3/#5: the old code used get_payload(decode=False) and only joined top-level
    parts, so real multipart/base64/quoted-printable emails came back empty/garbled.
    We now walk all parts, prefer text/plain, fall back to stripped text/html.
    """
    plain_chunks, html_chunks = [], []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            if ctype == "text/plain":
                plain_chunks.append(_decode_part(part))
            elif ctype == "text/html":
                html_chunks.append(_decode_part(part))
    else:
        if msg.get_content_type() == "text/html":
            html_chunks.append(_decode_part(msg))
        else:
            plain_chunks.append(_decode_part(msg))

    if any(c.strip() for c in plain_chunks):
        return "\n".join(c for c in plain_chunks if c.strip())
    # Fall back to HTML with tags stripped.
    html = "\n".join(c for c in html_chunks if c.strip())
    if html:
        html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
        html = re.sub(r"(?s)<[^>]+>", " ", html)
        html = re.sub(r"[ \t]+", " ", html)
        return html.strip()
    return ""


def read_message(message_id: str) -> dict:
    """Read the full body of a message by id."""
    for m in _mailbox():
        if m["id"] == message_id:
            with open(m["path"]) as _fh:
                msg = email.message_from_file(_fh)
            return {
                "id":      message_id,
                "from":    m["from"],
                "subject": m["subject"],
                "body":    _extract_body(msg),
            }
    raise FileNotFoundError(message_id)


def draft_reply(message_id: str, body: str) -> dict:
    """Write a DRAFT reply to the outbox (never sends). A human or a
    separately-approved send tool dispatches it."""
    orig = read_message(message_id)
    os.makedirs(_OUTBOX_DIR, exist_ok=True)
    fname = os.path.join(
        _OUTBOX_DIR,
        "draft_re_" + re.sub(r"[^A-Za-z0-9]+", "_", orig["subject"])[:50] + ".eml",
    )
    open(fname, "w").write(
        f"To: {orig['from']}\nSubject: Re: {orig['subject']}\nX-Status: DRAFT\n\n{body}\n"
    )
    return {"status": "draft_created", "file": fname}
