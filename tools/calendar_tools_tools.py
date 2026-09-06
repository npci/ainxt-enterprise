# SPDX-License-Identifier: MIT
"""
AiNxt Agentic Platform — calendar_tools MCP tools.

Availability + draft event creation from ICS calendars. Used by UC-57
(meeting notes), UC-63 (interview scheduling), UC-86 (exec inbox triage),
UC-87 (calendar management). All event creation writes a tentative .ics to
the outbox — no live booking. Point provider at m365/google and add a
gated booking tool separately for live writes.

Functions exposed:
  list_calendars   — list .ics calendar files under data_dir
  get_busy         — return busy intervals from a calendar
  find_free_slots  — find common free slots across multiple calendars
  draft_event      — write a tentative draft .ics to the outbox (no booking)

Companion server: mcp/servers/calendar_tools_server.py
Registered in:   mcp/registry.py:_register_tools()

Configuration (env vars):
  CALENDAR_TOOLS_DATA_DIR      — root for .ics calendar files
                                  (default ./data/calendars)
  CALENDAR_TOOLS_OUTBOX_DIR    — where draft .ics files are written
                                  (default ./outbox/calendar)
  CALENDAR_TOOLS_WORK_HOURS    — working hours window (default 09:00-18:30)
  CALENDAR_TOOLS_TIMEZONE      — TZID for emitted .ics (default UTC)
"""

import os
import re
from datetime import datetime, timedelta
from typing import List


# ── Configuration ────────────────────────────────────────────────────────────

_DATA_DIR    = os.getenv("CALENDAR_TOOLS_DATA_DIR",   "./data/calendars")
_OUTBOX_DIR  = os.getenv("CALENDAR_TOOLS_OUTBOX_DIR", "./outbox/calendar")
_WORK_HOURS  = os.getenv("CALENDAR_TOOLS_WORK_HOURS", "09:00-18:30")
_TIMEZONE    = os.getenv("CALENDAR_TOOLS_TIMEZONE",   "UTC")

_FMT = "%Y%m%dT%H%M%S"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_ics(path: str) -> List[dict]:
    events, cur = [], {}
    for line in open(path):
        line = line.strip()
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            events.append(cur)
        elif ":" in line:
            k, v = line.split(":", 1)
            cur[k.split(";")[0]] = v
    return [
        {"start": e.get("DTSTART"), "end": e.get("DTEND"), "title": e.get("SUMMARY", "")}
        for e in events
    ]


# ── Tool functions ───────────────────────────────────────────────────────────

def list_calendars() -> List[str]:
    """List ICS calendar files available under the configured data root."""
    out: List[str] = []
    for r, _, files in os.walk(_DATA_DIR):
        out += [
            os.path.relpath(os.path.join(r, f), _DATA_DIR)
            for f in files if f.endswith(".ics")
        ]
    return out


def get_busy(calendar_path: str) -> List[dict]:
    """Return busy intervals (start, end, title) from a calendar file."""
    return _parse_ics(os.path.join(_DATA_DIR, calendar_path))


def find_free_slots(calendar_paths: List[str], date: str, duration_min: int = 60,
                    earliest: str = "", latest: str = "") -> List[dict]:
    """Find common free slots across multiple calendars on a date
    (YYYY-MM-DD), within working hours. Returns up to 10 slots."""
    wh = _WORK_HOURS.split("-")
    earliest, latest = earliest or wh[0], latest or wh[1]
    day = date.replace("-", "")
    start = datetime.strptime(day + "T" + earliest.replace(":", "") + "00", _FMT)
    end   = datetime.strptime(day + "T" + latest.replace(":", "")   + "00", _FMT)
    busy = []
    for cp in calendar_paths:
        for e in _parse_ics(os.path.join(_DATA_DIR, cp)):
            if e["start"] and e["start"].startswith(day):
                busy.append((
                    datetime.strptime(e["start"], _FMT),
                    datetime.strptime(e["end"],   _FMT),
                ))
    slots, cur = [], start
    while cur + timedelta(minutes=duration_min) <= end:
        slot_end = cur + timedelta(minutes=duration_min)
        if not any(b0 < slot_end and b1 > cur for b0, b1 in busy):
            slots.append({"start": cur.isoformat(), "end": slot_end.isoformat()})
            cur = slot_end
        else:
            cur += timedelta(minutes=30)
    return slots[:10]


def draft_event(title: str, start_iso: str, end_iso: str, attendees: List[str]) -> dict:
    """Create a DRAFT calendar event written to the outbox as a tentative
    .ics (not booked). A human or a gated booking tool sends it."""
    os.makedirs(_OUTBOX_DIR, exist_ok=True)
    s = re.sub(r"[-:]", "", start_iso)
    e = re.sub(r"[-:]", "", end_iso)
    fname = os.path.join(
        _OUTBOX_DIR,
        f"draft_{s}_{re.sub(r'[^A-Za-z0-9]+', '_', title)[:40]}.ics",
    )
    att = "\n".join(f"ATTENDEE:mailto:{a}" for a in attendees)
    open(fname, "w").write(
        f"BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:draft-{s}@mcp\n"
        f"DTSTART;TZID={_TIMEZONE}:{s}\nDTEND;TZID={_TIMEZONE}:{e}\n"
        f"SUMMARY:{title}\n{att}\nSTATUS:TENTATIVE\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    return {"status": "draft_created", "file": fname}
