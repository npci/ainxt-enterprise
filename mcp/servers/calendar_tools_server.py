# SPDX-License-Identifier: Apache-2.0
"""
calendar_tools MCP Server — wraps tools/calendar_tools_tools.py.

Tools exposed:
  list_calendars   — list .ics calendar files under data_dir
  get_busy         — return busy intervals from a calendar
  find_free_slots  — find common free slots across calendars
  draft_event      — write a tentative draft .ics to the outbox (no booking)

Use cases: 57 (meeting notes), 63 (interview scheduling), 86 (exec inbox),
           87 (calendar management).
"""

import asyncio

from mcp.servers.base import BaseMCPServer, MCPTool


class CalendarToolsMCPServer(BaseMCPServer):

    server_name = "calendar_tools"

    def _setup_tools(self):
        from tools.calendar_tools_tools import (
            list_calendars,
            get_busy,
            find_free_slots,
            draft_event,
        )

        self._register(MCPTool(
            name="list_calendars",
            description="List ICS calendar files available under the configured data root.",
            fn=list_calendars,
            input_schema={"type": "object", "properties": {}},
        ))

        self._register(MCPTool(
            name="get_busy",
            description="Return busy intervals (start, end, title) from a calendar file.",
            fn=get_busy,
            input_schema={
                "type": "object",
                "properties": {
                    "calendar_path": {"type": "string"},
                },
                "required": ["calendar_path"],
            },
        ))

        self._register(MCPTool(
            name="find_free_slots",
            description=(
                "Find common free slots across multiple calendars on a date "
                "(YYYY-MM-DD), within working hours."
            ),
            fn=find_free_slots,
            input_schema={
                "type": "object",
                "properties": {
                    "calendar_paths": {"type": "array", "items": {"type": "string"}},
                    "date":           {"type": "string", "description": "YYYY-MM-DD"},
                    "duration_min":   {"type": "integer", "default": 60},
                    "earliest":       {"type": "string", "description": "HH:MM (optional)", "default": ""},
                    "latest":         {"type": "string", "description": "HH:MM (optional)", "default": ""},
                },
                "required": ["calendar_paths", "date"],
            },
        ))

        self._register(MCPTool(
            name="draft_event",
            description=(
                "Create a DRAFT calendar event written to the outbox as a "
                "tentative .ics (not booked)."
            ),
            fn=draft_event,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "title":     {"type": "string"},
                    "start_iso": {"type": "string"},
                    "end_iso":   {"type": "string"},
                    "attendees": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "start_iso", "end_iso", "attendees"],
            },
        ))


if __name__ == "__main__":
    asyncio.run(CalendarToolsMCPServer().run_stdio())
