# SPDX-License-Identifier: MIT
"""
task_tracker MCP Server — wraps tools/task_tracker_tools.py.

Tools exposed:
  create_task  — create a task with title/owner/due/details
  list_tasks   — list tasks, optionally filtered by status / owner
  update_task  — update a task's status / owner / due date

Use cases: 57 (action items from meeting notes), 65 (onboarding checklist),
           86 (executive inbox delegation).
"""

import asyncio

from mcp.servers.base import BaseMCPServer, MCPTool


class TaskTrackerMCPServer(BaseMCPServer):

    server_name = "task_tracker"

    def _setup_tools(self):
        from tools.task_tracker_tools import (
            create_task,
            list_tasks,
            update_task,
        )

        self._register(MCPTool(
            name="create_task",
            description="Create a task with optional owner (email) and due date (YYYY-MM-DD).",
            fn=create_task,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "title":   {"type": "string"},
                    "owner":   {"type": "string", "default": ""},
                    "due":     {"type": "string", "description": "YYYY-MM-DD", "default": ""},
                    "details": {"type": "string", "default": ""},
                },
                "required": ["title"],
            },
        ))

        self._register(MCPTool(
            name="list_tasks",
            description="List tasks, optionally filtered by status (open/done) and/or owner.",
            fn=list_tasks,
            input_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "default": ""},
                    "owner":  {"type": "string", "default": ""},
                },
            },
        ))

        self._register(MCPTool(
            name="update_task",
            description="Update a task's status, owner, or due date.",
            fn=update_task,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "status":  {"type": "string", "default": ""},
                    "owner":   {"type": "string", "default": ""},
                    "due":     {"type": "string", "default": ""},
                },
                "required": ["task_id"],
            },
        ))


if __name__ == "__main__":
    asyncio.run(TaskTrackerMCPServer().run_stdio())
