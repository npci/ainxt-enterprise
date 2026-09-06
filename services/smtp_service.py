# SPDX-License-Identifier: MIT
# ============================================================
# SMTP SERVICE — AiNxt email delivery
#
# Wraps Python's stdlib smtplib. All callers use send_html_email():
#
#     send_html_email(
#         to        = ["user@example.com"],
#         subject   = "Your AiNxt Monthly Statement — May 2026",
#         html_body = "<html>...</html>",
#         text_body = "Plain-text fallback ...",
#     )
#
# When AINXT_SMTP_HOST is empty (OSS default), send_html_email()
# returns False immediately — no connection, no timeout.
# Set AINXT_SMTP_HOST in .env to enable email features.
#
# Env vars (all optional):
#   AINXT_SMTP_HOST     default ""      (empty = disabled)
#   AINXT_SMTP_PORT     default 587
#   AINXT_SMTP_FROM     default noreply@ainxt.local
#   AINXT_SMTP_TIMEOUT  default 15  (seconds)
#   AINXT_SMTP_USER     default ""      (empty = no auth)
#   AINXT_SMTP_PASSWORD default ""      (empty = no auth)
# ============================================================

from __future__ import annotations

import os
import socket
import smtplib
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid
from email import encoders
from typing import Any, Dict, Iterable, List, Optional, Sequence

from core.logger import logger, mask_email


# ── Configuration ──────────────────────────────────────────────────────────
# AINXT_SMTP_HOST is intentionally empty by default (OSS safe default).
# When empty, all send_html_email() calls return False immediately with an
# INFO log — no connection attempt, no timeout, no error.
#
# OSS users:  leave empty to disable email features, or set to their own
#             SMTP server (e.g. smtp.gmail.com, smtp.sendgrid.net).
# Internal relay: set AINXT_SMTP_HOST in .env to the relay address.
SMTP_HOST    = os.getenv("AINXT_SMTP_HOST",    "")
SMTP_PORT    = int(os.getenv("AINXT_SMTP_PORT", "587"))
SMTP_FROM    = os.getenv("AINXT_SMTP_FROM",    "noreply@ainxt.local")
SMTP_TIMEOUT = int(os.getenv("AINXT_SMTP_TIMEOUT", "15"))


class SMTPSendError(RuntimeError):
    """Raised when the SMTP relay refused to accept the message."""


def _hostname_tag() -> str:
    """
    Return a 'HOSTNAME : <h>, IP : <ip>' footer string identical to the one
    the bash script appends.  Helps ops tracing in the relay logs.
    """
    try:
        host = socket.gethostname()
    except Exception:
        host = "unknown"
    try:
        ip = socket.gethostbyname(host)
    except Exception:
        ip = "0.0.0.0"
    return f"HOSTNAME : {host}, IP : {ip}"


def send_html_email(
    to:           Sequence[str],
    subject:      str,
    html_body:    str,
    text_body:    Optional[str] = None,
    cc:           Optional[Iterable[str]] = None,
    bcc:          Optional[Iterable[str]] = None,
    sender:       Optional[str] = None,
    attachments:  Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """
    Send a multipart/alternative HTML email through the AiNxt relay.

    Returns True on success.  On failure logs the error and either returns
    False (transport-level failure) or raises SMTPSendError if the relay
    explicitly rejected one or more recipients.

    No authentication and no STARTTLS — the relay is internal-only.

    `attachments` is an optional list of dicts:
        {"filename": "...", "content": bytes|str, "mimetype": "text/html"}
    When provided, the envelope is upgraded to `multipart/mixed` with the
    HTML/plain alternative as the first body part and each attachment as a
    subsequent base64-encoded part.
    """
    if not to:
        logger.warning("smtp_service.send_html_email called with empty recipient list")
        return False

    # ── SMTP disabled guard ───────────────────────────────────────────────
    # When AINXT_SMTP_HOST is empty (OSS default), skip silently.
    # No connection attempt → no 30-60 s timeout hanging the caller.
    if not SMTP_HOST:
        logger.info(
            "smtp_service.skipped — AINXT_SMTP_HOST not configured. "
            "Set AINXT_SMTP_HOST in .env to enable email features."
        )
        return False

    from_addr   = sender or SMTP_FROM
    # BCC addresses join the SMTP envelope recipient list but are deliberately
    # NEVER written to any message header below — that is what keeps them blind.
    recipients  = list(to) + (list(cc) if cc else []) + (list(bcc) if bcc else [])

    # Build the MIME envelope ----------------------------------------------
    # If attachments are present we need a mixed top-level container so the
    # text/html alternative pair lives alongside the attachment parts.
    alt = MIMEMultipart("alternative")
    if attachments:
        msg = MIMEMultipart("mixed")
        msg.attach(alt)
    else:
        msg = alt

    msg["Subject"]   = subject
    msg["From"]      = from_addr
    msg["To"]        = ", ".join(to)
    if cc:
        msg["Cc"]    = ", ".join(cc)
    msg["Date"]      = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="<YOUR_BASE_URL>")
    # Helpful trace header for the relay log — mirrors the bash script footer
    msg["X-AiNxt-Origin"] = _hostname_tag()

    # Plain-text part FIRST so clients without HTML support pick it.
    if not text_body:
        text_body = (
            "This email contains an HTML body.  If you are seeing this text "
            "your mail client does not support HTML rendering.\n\n"
            f"{_hostname_tag()}\n"
        )
    alt.attach(MIMEText(text_body, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html",  "utf-8"))

    # Attachments (optional) ------------------------------------------------
    for att in attachments or []:
        try:
            filename = att.get("filename") or "attachment.bin"
            content  = att.get("content")
            mimetype = att.get("mimetype") or "application/octet-stream"
            if content is None:
                logger.warning(
                    "smtp_service: skipping attachment %r with no content", filename,
                )
                continue
            if isinstance(content, str):
                content = content.encode("utf-8")
            main, _, sub = mimetype.partition("/")
            part = MIMEBase(main or "application", sub or "octet-stream")
            part.set_payload(content)
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="{filename}"',
            )
            msg.attach(part)
        except Exception as exc:
            logger.error(
                "smtp_service: failed to encode attachment %r: %s",
                att.get("filename"), exc,
            )

    # Talk to the relay ----------------------------------------------------
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as s:
            # HELO localhost  — matches send_email.sh exactly
            s.helo("localhost")
            refused = s.sendmail(from_addr, recipients, msg.as_string())
            if refused:
                logger.error(
                    "smtp_service: relay refused recipients=%s subject=%r",
                    refused, subject,
                )
                raise SMTPSendError(f"Relay refused recipients: {refused}")
        logger.info("smtp_service: sent subject=%r to=%s", subject, [mask_email(r) for r in recipients])
        return True
    except SMTPSendError:
        raise
    except Exception as exc:
        logger.error(
            "smtp_service: send failed host=%s:%s to=%s subject=%r err=%s",
            SMTP_HOST, SMTP_PORT, recipients, subject, exc,
        )
        return False
