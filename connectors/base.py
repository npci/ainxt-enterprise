# SPDX-License-Identifier: MIT
"""
Core types for the AiNxt Universal Connector Framework.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional


# ── Exceptions ────────────────────────────────────────────────────────────────

class ConnectorNotConnectedError(Exception):
    """User has not connected this connector yet."""

class ConnectorTransientError(Exception):
    """A temporary failure (e.g. DB pool exhaustion / timeout) that is NOT a real
    disconnect. The caller should tell the user to RETRY, never to reconnect."""

class ConnectorReauthRequired(Exception):
    """Token expired/revoked — user must reconnect."""

class ConnectorTokenRejected(Exception):
    """The provider rejected the access token (e.g. Graph 401 / expired bearer).

    Distinct from ConnectorReauthRequired: this says "the ACCESS token this call
    used is stale", not "the user's grant is gone". The engine responds by forcing
    a token refresh and retrying the call ONCE; only if that also fails does it
    become ConnectorReauthRequired and deactivate the stored token.

    Adapters should raise THIS on a 401 rather than ConnectorReauthRequired —
    raising the latter directly meant a routine hourly access-token expiry
    permanently killed the connection and forced a manual reconnect.
    """

class ConnectorScopeError(Exception):
    """Token lacks required OAuth scopes for this tool."""

class ConnectorRateLimitError(Exception):
    """Per-user rate limit exceeded for this connector."""

class ConnectorValidationError(Exception):
    """Input params failed Pydantic validation."""

class ConnectorAccessDeniedError(Exception):
    """User's AD level or department does not meet connector access policy."""


# ── Data types ─────────────────────────────────────────────────────────────────

@dataclass
class OAuth2Config:
    """OAuth2 provider configuration for a connector."""
    authorize_url: str
    token_url: str
    client_id_env: str        # name of env var holding client_id
    client_secret_env: str    # name of env var holding client_secret
    scopes: list[str]
    pkce: bool = True
    extra_params: dict = field(default_factory=dict)  # e.g., {"response_mode": "query"}
    revoke_url: Optional[str] = None


@dataclass
class ConnectorTool:
    """Definition of a single tool exposed by a connector to the LLM."""
    name: str                          # e.g., "search_emails"
    description: str                   # LLM-facing description
    method: str                        # GET | POST
    path: str                          # URL path template, e.g. "/v1.0/me/messages"
    input_schema: dict                 # JSON Schema for LLM params
    requires_scopes: list[str] = field(default_factory=list)
    cache_ttl_s: int = 300             # Redis TTL in seconds (0 = no cache)
    paginated: bool = False            # whether this tool supports pagination
    max_items: int = 50                # hard limit on items returned
    is_write: bool = False             # True for POST/PATCH/DELETE operations (needs idempotency key)
    query_params: dict = field(default_factory=dict)   # static query params to always include
    response_items_path: str = "value"  # JSON path to extract item list from response
    response_count_path: Optional[str] = None  # JSON path to total count
    response_fields: list = field(default_factory=list)  # whitelist of fields to keep per item (empty = keep all)


@dataclass
class ConnectorContext:
    """Runtime context passed to every connector execution."""
    user_id: str
    connector_name: str
    access_token: str                 # OAuth token, API key, OR a DPI consent artifact (JSON)
    scopes: list[str] = field(default_factory=list)
    tenant_id: Optional[str] = None   # for Microsoft Graph
    metadata: dict = field(default_factory=dict)
    # Auth model for this connector: "oauth2" (default) | "api_key" | "dpi_consent".
    # DPI connectors use a signed CONSENT ARTIFACT, not a bearer token (Account
    # Aggregator / DEPA model). access_token then carries the artifact JSON.
    auth_type: str = "oauth2"
    # True when running the open DPI SANDBOX (synthetic data, no real upstream/
    # credentials) — set by the engine for dpi_* connectors when DPI_SANDBOX is on.
    is_sandbox: bool = False


@dataclass
class ConnectorResponse:
    """Normalized response from any connector tool. Always the same shape."""
    success: bool
    items: list[dict]           # normalized list of results
    count: int                  # len(items) or API-reported total
    source: str                 # connector name
    tool: str                   # tool name
    partial: bool = False       # True if pagination stopped early (page failure)
    truncated: bool = False     # True if cost guardrail capped results
    timed_out: bool = False     # True if wall-clock deadline was hit mid-pagination
    latency_ms: int = 0
    error: Optional[str] = None
    meta: dict = field(default_factory=dict)   # raw API metadata (nextLink, etc.)

    # Maximum characters injected per connector response to avoid context explosion.
    # ~8 000 chars ≈ 2 000 tokens — enough for 20 dense email summaries.
    MAX_CONTEXT_CHARS = 8_000

    # Calendar / meeting tools produce large JSON per item (attendees, previews, etc.)
    # and need a much larger budget so all meetings are visible to the LLM.
    # ~24 000 chars ≈ 6 000 tokens — comfortably fits 50+ compact meeting lines.
    _CALENDAR_TOOLS = frozenset({
        "calendar_list_events",
        "calendar_create_event",
        "calendar_update_event",
        "calendar_cancel_event",
        "calendar_delete_event",
        "calendar_accept_event",
        "calendar_decline_event",
        "calendar_tentative_event",
    })
    MAX_CONTEXT_CHARS_CALENDAR = 24_000

    # Full email read tools — body is pre-cleaned to plain text (HTML stripped,
    # truncated at 12 000 chars in _normalize_email_full). Give them a 16 000-char
    # budget so the cleaned body + headers always fit without hitting the wall.
    _EMAIL_READ_TOOLS = frozenset({
        "outlook_read_email",
        "gmail_read_email",
    })
    MAX_CONTEXT_CHARS_EMAIL = 16_000

    # Email SEARCH/LIST tools return up to 50 compact items (subject, sender,
    # date, ~255-char preview) as raw JSON per item (~600-700 chars each), which
    # only fit ~11-13 items inside the old 8 000-char generic budget — silently
    # dropping the other 37-39 before the LLM ever saw them (Fix: mail search
    # truncated, same class of bug as the earlier "meetings truncated" fix
    # above). Reuse the existing 16 000-char email budget tier so roughly
    # double the results are visible per call.
    _EMAIL_SEARCH_TOOLS = frozenset({
        "outlook_search_emails",
        "gmail_search_emails",
    })

    # ── Compact single-line formatter for calendar events ─────────────────────
    @staticmethod
    def _format_calendar_item(idx: int, item: dict) -> str:
        """Render one calendar event as a compact, human-readable line.

        Raw Graph JSON per event is ~500-800 chars (attendees list, bodyPreview,
        nested dicts).  This formatter collapses it to ~120-180 chars so all
        meetings fit inside the context budget without truncation.

        Output example:
          3. [2026-07-15 11:00→11:15] AI Governance Review | organizer: alice@ainxt.com
             attendees: bob@ainxt.com, carol@ainxt.com | online: yes
        """
        subject = item.get("subject") or "(no subject)"

        # Normalise datetime strings — strip the trailing fractional seconds /
        # timezone suffix that Graph returns so the line stays short.
        def _short_dt(raw: str) -> str:
            if not raw:
                return "?"
            # "2026-07-15T11:00:00.0000000" → "2026-07-15 11:00"
            raw = str(raw).replace("T", " ")
            # Drop seconds and beyond
            raw = raw[:16]
            return raw

        start = _short_dt(item.get("start", ""))
        end   = _short_dt(item.get("end", ""))
        # Only keep the time portion for end if it shares the same date
        if start[:10] == end[:10]:
            end_display = end[11:] if len(end) > 10 else end
        else:
            end_display = end

        organizer = item.get("organizer_email") or item.get("organizer_name") or ""

        attendees_raw = item.get("attendees") or []
        if isinstance(attendees_raw, list):
            attendee_emails = [
                a.get("email") or a.get("name") or ""
                for a in attendees_raw
                if isinstance(a, dict)
            ]
            # Drop the organizer from the attendee list to avoid duplication
            attendee_emails = [e for e in attendee_emails if e and e != organizer]
        else:
            attendee_emails = []

        is_online = item.get("is_online_meeting", False)
        location  = item.get("location") or ""
        event_id  = item.get("id", "")

        parts = [f"{idx}. [{start}→{end_display}] {subject}"]
        if organizer:
            parts.append(f"organizer: {organizer}")
        if attendee_emails:
            parts.append(f"attendees: {', '.join(attendee_emails[:10])}")
        if is_online:
            parts.append("online: yes")
        elif location:
            parts.append(f"location: {location[:60]}")
        if event_id:
            parts.append(f"id: {event_id[:40]}")

        return " | ".join(parts)

    def to_context_str(self) -> str:
        """Format for LLM context injection with hard character budget.

        Calendar/meeting tools use a compact single-line formatter and a larger
        character budget (24 000 chars) so all meetings are visible — the raw
        JSON representation of a meeting is ~600 chars, which caused only ~3
        events to fit inside the old 8 000-char budget (Fix: meetings truncated).
        """
        if not self.success:
            return f"[{self.source}.{self.tool} error: {self.error}]"

        flags = []
        if self.partial:
            flags.append("partial")
        if self.truncated:
            flags.append("truncated to limit")
        if self.timed_out:
            flags.append("timed out — results may be incomplete")
        flag_str = f" ({', '.join(flags)})" if flags else ""

        header = f"[{self.source}.{self.tool} — {self.count} result(s){flag_str}]"
        lines = [header]

        # Choose budget and item formatter based on tool type.
        is_calendar  = self.tool in self._CALENDAR_TOOLS
        is_email_budget = self.tool in self._EMAIL_READ_TOOLS or self.tool in self._EMAIL_SEARCH_TOOLS
        if is_calendar:
            max_chars = self.MAX_CONTEXT_CHARS_CALENDAR
        elif is_email_budget:
            max_chars = self.MAX_CONTEXT_CHARS_EMAIL
        else:
            max_chars = self.MAX_CONTEXT_CHARS

        # Build item lines; stop when we exceed the character budget.
        budget = max_chars - len(header) - 1
        shown = 0
        for item in self.items:
            if is_calendar:
                line = self._format_calendar_item(shown + 1, item)
            else:
                line = f"{shown + 1}. {json.dumps(item, default=str, ensure_ascii=False)}"
            if budget - len(line) - 1 < 0:
                lines.append(
                    f"… {len(self.items) - shown} more result(s) omitted "
                    "(response too large for context window)"
                )
                break
            lines.append(line)
            budget -= len(line) + 1
            shown += 1

        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "items": self.items,
            "count": self.count,
            "source": self.source,
            "tool": self.tool,
            "partial": self.partial,
            "truncated": self.truncated,
            "timed_out": self.timed_out,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "meta": self.meta,
        }
