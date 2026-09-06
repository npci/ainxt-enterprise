# SPDX-License-Identifier: MIT
"""
Google Calendar custom adapter — Google Calendar API v3.

A custom adapter rather than GenericHTTPAdapter because of four quirks:
  - Time windows must be RFC-3339 with an offset. Users (and the model) supply
    plain dates like "2026-09-01", so those are widened to a whole local day.
  - Pagination is `pageToken`, not the generic `cursor` param.
  - A recurring event is one resource with a recurrence rule. Without
    `singleEvents=true` a weekly stand-up appears once instead of on each day,
    which is never what "what's on my calendar this week" means.
  - Creating an event needs nested {start:{dateTime}}/{end:{dateTime}} objects,
    not the flat params the generic adapter forwards.

Shares the Google OAuth client (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET) with the
Gmail and Google Drive connectors, so a deployment registers one Google app and
the user consents per scope.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
import re

import httpx

from connectors.adapters.base import AdapterBase, AdapterPage
from connectors.base import (
    ConnectorContext,
    ConnectorReauthRequired,
    ConnectorTokenRejected,
    ConnectorTool,
)
from core.logger import logger

CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"

# Bare calendar dates, e.g. "2026-09-01".
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# How far ahead "upcoming events" looks when the caller gives no window. Bounded
# deliberately: an unbounded timeMin returns every event ever scheduled.
_DEFAULT_WINDOW_DAYS = 14


def _to_rfc3339(value: str, *, end_of_day: bool = False) -> str:
    """Normalise a caller-supplied date or datetime to RFC-3339 with an offset.

    A date with no time is ambiguous: as a window start it means 00:00, as a
    window end it means 23:59:59. `end_of_day` picks which, so `date_to` of
    "2026-09-01" includes that whole day instead of excluding all of it.
    """
    v = (value or "").strip()
    if not v:
        raise ValueError("empty datetime value")

    if _DATE_ONLY.match(v):
        d = datetime.strptime(v, "%Y-%m-%d")
        if end_of_day:
            d = d.replace(hour=23, minute=59, second=59)
        return d.astimezone().isoformat()

    # Accept a trailing "Z" — datetime.fromisoformat rejects it before 3.11.
    iso = v[:-1] + "+00:00" if v.endswith("Z") else v
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(
            f"could not parse {value!r} as a date (YYYY-MM-DD) or RFC-3339 datetime"
        ) from exc
    # Naive input is local time as far as the user is concerned.
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat()


class GoogleCalendarAdapter(AdapterBase):

    TIMEOUT = 20

    def execute(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str] = None,
    ) -> AdapterPage:
        name = getattr(tool, "name", "") or ""
        method = (getattr(tool, "method", "GET") or "GET").upper()

        # WRITE: create an event. Reached only after the confirm + compliance gate
        # at POST /connectors/action, same as gmail_send_email.
        if method == "POST" or getattr(tool, "is_write", False):
            return self._create_event(tool, params, context)

        resolved_path, remaining = self._resolve_path(tool.path, params)
        url = CALENDAR_BASE + resolved_path
        headers = self.build_headers(context)
        qp = self._build_params(name, remaining, cursor)

        try:
            resp = httpx.get(url, headers=headers, params=qp, timeout=self.TIMEOUT)

            if resp.status_code == 401:
                # Let the engine refresh and retry once before this becomes a
                # "please reconnect" that deactivates the stored token.
                raise ConnectorTokenRejected(
                    "Google Calendar access token was rejected (401)."
                )
            if resp.status_code == 403:
                # Calendar returns 403 both for quota and for a missing scope.
                # Only the latter needs the user back in the consent screen.
                body = resp.text[:400]
                if "insufficientPermissions" in body or "ACCESS_TOKEN_SCOPE" in body:
                    raise ConnectorReauthRequired(
                        "Google Calendar needs to be reconnected to grant calendar access."
                    )

            resp.raise_for_status()
            data = resp.json()

            items = [self._normalize_event(e) for e in (data.get("items") or [])]
            return AdapterPage(
                items=items,
                next_cursor=data.get("nextPageToken"),
                meta={k: v for k, v in data.items() if k != "items"},
            )

        except (ConnectorReauthRequired, ConnectorTokenRejected):
            raise
        except httpx.HTTPStatusError as e:
            logger.error(
                f"GoogleCalendarAdapter: HTTP {e.response.status_code} for {tool.name}"
            )
            raise

    # ── Reads ────────────────────────────────────────────────────────────────

    def _build_params(self, tool_name: str, remaining: dict, cursor: Optional[str]) -> dict:
        qp: dict = {}

        if tool_name == "calendar_list_events":
            # Expand recurring events into their occurrences; ordering by start
            # time is only permitted when singleEvents is true.
            qp["singleEvents"] = "true"
            qp["orderBy"] = "startTime"

            date_from = remaining.pop("date_from", None)
            date_to = remaining.pop("date_to", None)

            now = datetime.now(timezone.utc).astimezone()
            qp["timeMin"] = _to_rfc3339(date_from) if date_from else now.isoformat()
            if date_to:
                qp["timeMax"] = _to_rfc3339(date_to, end_of_day=True)
            elif not date_from:
                # No window at all — bound it rather than returning everything.
                qp["timeMax"] = (now + timedelta(days=_DEFAULT_WINDOW_DAYS)).isoformat()

            if "search_query" in remaining:
                qp["q"] = remaining.pop("search_query")

        limit = remaining.pop("limit", None)
        if limit:
            try:
                # Calendar caps maxResults at 2500 and rejects anything above it.
                qp["maxResults"] = max(1, min(int(limit), 2500))
            except (TypeError, ValueError):
                pass

        if cursor:
            qp["pageToken"] = cursor

        # Anything the tool schema declared that we did not special-case.
        for k, v in remaining.items():
            if v is not None:
                qp[k] = v
        return qp

    def _normalize_event(self, e: dict) -> dict:
        """Flatten the parts of an event a caller actually reads.

        `start`/`end` are either {dateTime} for timed events or {date} for
        all-day ones; collapsing them here means every consumer does not have to
        handle both shapes.
        """
        start = e.get("start") or {}
        end = e.get("end") or {}
        all_day = "date" in start
        attendees = [
            a.get("email", "")
            for a in (e.get("attendees") or [])
            if a.get("email")
        ]
        organizer = (e.get("organizer") or {}).get("email", "")
        return {
            "id": e.get("id", ""),
            "title": e.get("summary", "(no title)"),
            "description": e.get("description", ""),
            "location": e.get("location", ""),
            "start": start.get("dateTime") or start.get("date", ""),
            "end": end.get("dateTime") or end.get("date", ""),
            "all_day": all_day,
            "status": e.get("status", ""),
            "organizer": organizer,
            "attendees": attendees,
            "html_link": e.get("htmlLink", ""),
            "recurring_event_id": e.get("recurringEventId", ""),
            "meeting_url": (e.get("conferenceData") or {}).get("entryPoints", [{}])[0].get("uri", "")
            if e.get("conferenceData") else "",
        }

    # ── Write ────────────────────────────────────────────────────────────────

    def _create_event(
        self, tool: ConnectorTool, params: dict, context: ConnectorContext
    ) -> AdapterPage:
        """Create an event (POST /calendars/{calendarId}/events)."""
        title = (params.get("title") or "").strip()
        start_raw = (params.get("start") or "").strip()
        end_raw = (params.get("end") or "").strip()
        if not title:
            raise ValueError("calendar_create_event requires a 'title'")
        if not start_raw:
            raise ValueError("calendar_create_event requires a 'start' date or datetime")

        # A start with no end is a common request ("book 30 minutes tomorrow at
        # 3pm"). Default to a one-hour meeting rather than rejecting it.
        all_day = bool(_DATE_ONLY.match(start_raw)) and (
            not end_raw or bool(_DATE_ONLY.match(end_raw))
        )
        if all_day:
            start_field = {"date": start_raw}
            # Calendar treats an all-day `end` as exclusive, so a single-day event
            # ends on the following date. Passing the same date creates nothing.
            end_date = end_raw or (
                datetime.strptime(start_raw, "%Y-%m-%d") + timedelta(days=1)
            ).strftime("%Y-%m-%d")
            end_field = {"date": end_date}
        else:
            start_iso = _to_rfc3339(start_raw)
            if end_raw:
                end_iso = _to_rfc3339(end_raw)
            else:
                end_iso = (
                    datetime.fromisoformat(start_iso) + timedelta(hours=1)
                ).isoformat()
            start_field = {"dateTime": start_iso}
            end_field = {"dateTime": end_iso}

        body: dict = {"summary": title, "start": start_field, "end": end_field}
        if params.get("description"):
            body["description"] = params["description"]
        if params.get("location"):
            body["location"] = params["location"]
        attendees = params.get("attendees")
        if attendees:
            if isinstance(attendees, str):
                attendees = [a.strip() for a in attendees.split(",") if a.strip()]
            body["attendees"] = [{"email": a} for a in attendees]

        calendar_id = params.get("calendar_id") or "primary"
        headers = self.build_headers(context)
        headers["Content-Type"] = "application/json"

        try:
            resp = httpx.post(
                f"{CALENDAR_BASE}/calendars/{calendar_id}/events",
                headers=headers,
                json=body,
                timeout=self.TIMEOUT,
            )
            if resp.status_code == 401:
                raise ConnectorTokenRejected(
                    "Google Calendar access token was rejected (401)."
                )
            resp.raise_for_status()
            data = resp.json()
            return AdapterPage(
                items=[{**self._normalize_event(data), "action": "created"}],
                next_cursor=None,
                meta=data,
            )
        except (ConnectorReauthRequired, ConnectorTokenRejected):
            raise
        except httpx.HTTPStatusError as e:
            logger.error(
                f"GoogleCalendarAdapter: create failed HTTP {e.response.status_code}"
            )
            raise
