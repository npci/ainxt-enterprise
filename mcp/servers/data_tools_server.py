# SPDX-License-Identifier: MIT
"""
data_tools MCP Server — wraps tools/data_tools_tools.py.

Tools exposed:
  list_tables       — list CSV/XLSX sources under data_dir
  describe_table    — schema + sample rows + numeric summary
  query_table       — pandas-style filter / group_by / aggregate
  variance_report   — budget vs. actual variance with flagging
  reconcile         — match two transaction tables on fuzzy ref + amount
  make_chart        — render line/bar/scatter PNG to the charts outbox

Use cases: 69, 70, 83, 84, 90, 91, 97 (analysis halves of every flow that
needs a deterministic tabular answer).
"""

import asyncio

from mcp.servers.base import BaseMCPServer, MCPTool


class DataToolsMCPServer(BaseMCPServer):

    server_name = "data_tools"

    def _setup_tools(self):
        from tools.data_tools_tools import (
            list_tables,
            describe_table,
            query_table,
            variance_report,
            reconcile,
            make_chart,
        )

        self._register(MCPTool(
            name="list_tables",
            description="List CSV/XLSX tabular sources under the configured data root.",
            fn=list_tables,
            input_schema={"type": "object", "properties": {}},
        ))

        self._register(MCPTool(
            name="describe_table",
            description="Schema + sample rows + numeric summary for a CSV/XLSX source.",
            fn=describe_table,
            input_schema={
                "type": "object",
                "properties": {
                    "path":  {"type": "string"},
                    "sheet": {"type": "string", "default": ""},
                },
                "required": ["path"],
            },
        ))

        self._register(MCPTool(
            name="query_table",
            description=(
                "Query a table: optional pandas filter expression, optional "
                "group_by column(s) comma-separated with aggregate like sum/mean/count."
            ),
            fn=query_table,
            input_schema={
                "type": "object",
                "properties": {
                    "path":        {"type": "string"},
                    "filter_expr": {"type": "string", "default": ""},
                    "group_by":    {"type": "string", "default": ""},
                    "aggregate":   {"type": "string", "default": ""},
                    "sheet":       {"type": "string", "default": ""},
                    "limit":       {"type": "integer", "default": 100},
                },
                "required": ["path"],
            },
        ))

        self._register(MCPTool(
            name="variance_report",
            description="Compute budget-vs-actual variance per row and flag rows whose abs variance %% exceeds flag_pct.",
            fn=variance_report,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "path":       {"type": "string"},
                    "budget_col": {"type": "string"},
                    "actual_col": {"type": "string"},
                    "label_col":  {"type": "string"},
                    "flag_pct":   {"type": "number", "default": 5.0},
                    "sheet":      {"type": "string", "default": ""},
                },
                "required": ["path", "budget_col", "actual_col", "label_col"],
            },
        ))

        self._register(MCPTool(
            name="reconcile",
            description="Match two transaction tables on fuzzy reference + amount tolerance; report matches and discrepancies.",
            fn=reconcile,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "left_path":        {"type": "string"},
                    "right_path":       {"type": "string"},
                    "amount_col_left":  {"type": "string"},
                    "amount_col_right": {"type": "string"},
                    "ref_col_left":     {"type": "string"},
                    "ref_col_right":    {"type": "string"},
                    "tolerance":        {"type": "number", "default": 1.0},
                },
                "required": [
                    "left_path", "right_path",
                    "amount_col_left", "amount_col_right",
                    "ref_col_left", "ref_col_right",
                ],
            },
        ))

        self._register(MCPTool(
            name="make_chart",
            description="Render a chart (line/bar/scatter) from a table to PNG in the charts outbox.",
            fn=make_chart,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "path":   {"type": "string"},
                    "chart":  {"type": "string", "enum": ["line", "bar", "scatter"]},
                    "x":      {"type": "string"},
                    "y":      {"type": "string"},
                    "series": {"type": "string", "default": ""},
                    "title":  {"type": "string", "default": ""},
                    "sheet":  {"type": "string", "default": ""},
                },
                "required": ["path", "chart", "x", "y"],
            },
        ))


if __name__ == "__main__":
    asyncio.run(DataToolsMCPServer().run_stdio())
