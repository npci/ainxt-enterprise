# SPDX-License-Identifier: MIT
# ============================================================
# GRAPH APP CLIENT — app-only Microsoft Graph access
#
# Scope doc §4.5 / §7.2: post-meeting transcript + participant retrieval
# uses Microsoft Graph with APPLICATION permissions under CENTRALIZED admin
# consent (no end-user consent flows). This client does the OAuth2
# client-credentials grant against the AiNxt-registered Entra app
# (AZURE_AD_CLIENT_ID / AZURE_AD_CLIENT_SECRET / AZURE_AD_TENANT_ID) and
# exposes thin JSON + VTT GET helpers.
#
# Why app-only (not the per-user connector engine)?
#   • §7.2 forbids end-user premium consent; organizer artifacts are read
#     with app perms + a Teams Application Access Policy.
#   • The connector engine HARD-BLOCKS any response containing PII — a
#     transcript is all names/speech, so it would be dropped. The meeting
#     worker instead applies redact-and-proceed via the compliance engine
#     and audits each ingest (core/graph_audit).
#
# Transport-only: this client only MOVES bytes. No AI here.
# ============================================================

import os
import time
import threading
from typing import Optional
from urllib.parse import quote

import httpx

from connectors.net_relay import relay_request
from core.logger import logger

GRAPH_BASE     = "https://graph.microsoft.com"
_LOGIN_BASE    = os.getenv("AZURE_AD_LOGIN_BASE", "https://login.microsoftonline.com")
_CLIENT_ID     = os.getenv("AZURE_AD_CLIENT_ID", "")
_CLIENT_SECRET = os.getenv("AZURE_AD_CLIENT_SECRET", "")
_TENANT_ID     = os.getenv("AZURE_AD_TENANT_ID", "")

_TIMEOUT = 30  # Graph artifact reads can be slow

# ── App-token cache (process-local, refresh 60s before expiry) ───────────
_token_lock = threading.Lock()
_token_val: Optional[str] = None
_token_exp: float = 0.0


class GraphAppError(Exception):
    """App-only Graph call failed (config missing, auth, or HTTP error)."""


def _get_app_token() -> str:
    """Client-credentials token for Graph (.default scope). Cached + auto-refreshed."""
    global _token_val, _token_exp
    with _token_lock:
        now = time.time()
        if _token_val and now < _token_exp - 60:
            return _token_val

        if not (_CLIENT_ID and _CLIENT_SECRET and _TENANT_ID):
            raise GraphAppError(
                "Graph app client not configured — set AZURE_AD_CLIENT_ID, "
                "AZURE_AD_CLIENT_SECRET, AZURE_AD_TENANT_ID."
            )

        token_url = f"{_LOGIN_BASE}/{_TENANT_ID}/oauth2/v2.0/token"
        try:
            # this host has no internet — relay through the LLM proxy server's LLM proxy.
            resp = relay_request(
                token_url,
                data={
                    "client_id":     _CLIENT_ID,
                    "client_secret": _CLIENT_SECRET,
                    "scope":         f"{GRAPH_BASE}/.default",
                    "grant_type":    "client_credentials",
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            tok = resp.json()
        except httpx.HTTPStatusError as e:
            raise GraphAppError(f"app token request failed: {e.response.status_code} {e.response.text[:300]}")
        except Exception as e:
            raise GraphAppError(f"app token request error: {e}")

        _token_val = tok.get("access_token", "")
        _token_exp = now + int(tok.get("expires_in", 3600))
        if not _token_val:
            raise GraphAppError("app token response had no access_token")
        logger.info("GraphAppClient: app token acquired")
        return _token_val


def _headers(accept: str = "application/json") -> dict:
    return {"Authorization": f"Bearer {_get_app_token()}", "Accept": accept}


def get_json(path: str, params: Optional[dict] = None) -> dict:
    """GET a Graph JSON resource (path starts with /v1.0/...). Raises GraphAppError."""
    url = GRAPH_BASE + path
    try:
        resp = relay_request("GET", url, headers=_headers("application/json"), params=params or {}, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise GraphAppError(f"GET {path} → {e.response.status_code} {e.response.text[:300]}")
    except Exception as e:
        raise GraphAppError(f"GET {path} error: {e}")


def post_json(path: str, body: dict) -> dict:
    """POST a JSON body to Graph. Returns parsed JSON, or {} for 202/empty bodies."""
    url = GRAPH_BASE + path
    try:
        resp = relay_request(
            url,
            headers={**_headers("application/json"), "Content-Type": "application/json"},
            json=body,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    except httpx.HTTPStatusError as e:
        raise GraphAppError(f"POST {path} → {e.response.status_code} {e.response.text[:300]}")
    except Exception as e:
        raise GraphAppError(f"POST {path} error: {e}")


def get_text(path: str, params: Optional[dict] = None) -> str:
    """GET a Graph resource that returns text (e.g. transcript content as text/vtt).

    This is the branch JSON adapters can't handle — Graph returns text/vtt,
    not application/json, for transcript `/content`.
    """
    url = GRAPH_BASE + path
    try:
        resp = relay_request("GET", url, headers=_headers("text/vtt"), params=params or {}, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPStatusError as e:
        raise GraphAppError(f"GET(text) {path} → {e.response.status_code} {e.response.text[:300]}")
    except Exception as e:
        raise GraphAppError(f"GET(text) {path} error: {e}")


# ── Meeting-specific convenience wrappers ────────────────────────────────
def list_transcripts(organizer_id: str, meeting_id: str) -> list[dict]:
    """List transcripts for an organizer's online meeting (post-meeting).

    GET /v1.0/users/{organizerId}/onlineMeetings/{meetingId}/transcripts
    Each item: {id, meetingId, transcriptContentUrl, createdDateTime, ...}.
    """
    path = f"/v1.0/users/{quote(organizer_id)}/onlineMeetings/{quote(meeting_id)}/transcripts"
    data = get_json(path)
    items = data.get("value", [])
    return items if isinstance(items, list) else []


def get_transcript_vtt(organizer_id: str, meeting_id: str, transcript_id: str) -> str:
    """Fetch a transcript's content as WebVTT text (post-meeting only)."""
    path = (
        f"/v1.0/users/{quote(organizer_id)}/onlineMeetings/{quote(meeting_id)}"
        f"/transcripts/{quote(transcript_id)}/content"
    )
    return get_text(path, params={"$format": "text/vtt"})


def get_meeting(organizer_id: str, meeting_id: str) -> dict:
    """Fetch online meeting metadata (subject, participants, start/end)."""
    path = f"/v1.0/users/{quote(organizer_id)}/onlineMeetings/{quote(meeting_id)}"
    return get_json(path)


def extract_participants(meeting: dict) -> list[dict]:
    """Pull [{name, email}] from an onlineMeeting's participants block."""
    out: list[dict] = []
    parts = (meeting or {}).get("participants", {}) or {}
    buckets = []
    if parts.get("organizer"):
        buckets.append(parts["organizer"])
    buckets.extend(parts.get("attendees", []) or [])
    for b in buckets:
        info = (b or {}).get("identity", {}).get("user", {}) or {}
        upn = b.get("upn") or info.get("userPrincipalName") or ""
        name = info.get("displayName") or upn.split("@")[0] if upn else info.get("displayName", "")
        if upn or name:
            out.append({"name": name or upn, "email": upn})
    # de-dup by email/name
    seen, uniq = set(), []
    for p in out:
        k = (p.get("email") or p.get("name", "")).lower()
        if k and k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def send_mail(organizer_id: str, to_emails: list[str], subject: str, body_text: str) -> None:
    """Send an email AS the organizer (app perm Mail.Send). Used to distribute MoM."""
    recipients = [{"emailAddress": {"address": e}} for e in to_emails if e]
    if not recipients:
        raise GraphAppError("send_mail: no recipients")
    path = f"/v1.0/users/{quote(organizer_id)}/sendMail"
    post_json(path, {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": recipients,
        },
        "saveToSentItems": True,
    })
