# SPDX-License-Identifier: MIT
"""
lms_tools MCP Server — wraps tools/lms_tools_tools.py.

Tools exposed:
  list_modules         — list LMS modules, filterable by level / max duration
  save_learning_plan   — persist a per-learner plan as JSON
  get_learning_plan    — fetch a previously saved plan

Use cases: 96 (training material creation),
           100 (personalized learning tutor).
"""

import asyncio

from mcp.servers.base import BaseMCPServer, MCPTool


class LMSToolsMCPServer(BaseMCPServer):

    server_name = "lms_tools"

    def _setup_tools(self):
        from tools.lms_tools_tools import (
            list_modules,
            save_learning_plan,
            get_learning_plan,
        )

        self._register(MCPTool(
            name="list_modules",
            description="List learning modules from the configured catalog, filterable by level and max duration.",
            fn=list_modules,
            input_schema={
                "type": "object",
                "properties": {
                    "level":            {"type": "string", "default": ""},
                    "max_duration_min": {"type": "integer", "default": 0},
                },
            },
        ))

        self._register(MCPTool(
            name="save_learning_plan",
            description=(
                "Persist a learning plan (list of {week, modules, milestone, "
                "quiz_topic}) for a learner."
            ),
            fn=save_learning_plan,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "learner_id": {"type": "string"},
                    "plan": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                },
                "required": ["learner_id", "plan"],
            },
        ))

        self._register(MCPTool(
            name="get_learning_plan",
            description="Fetch a previously saved learning plan for a learner.",
            fn=get_learning_plan,
            input_schema={
                "type": "object",
                "properties": {
                    "learner_id": {"type": "string"},
                },
                "required": ["learner_id"],
            },
        ))


if __name__ == "__main__":
    asyncio.run(LMSToolsMCPServer().run_stdio())
