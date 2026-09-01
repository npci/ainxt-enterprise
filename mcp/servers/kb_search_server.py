# SPDX-License-Identifier: Apache-2.0
"""
kb_search MCP Server — wraps tools/kb_search_tools.py as a spec-compliant
MCP server.

Tools exposed:
  list_namespaces  — list configured KB namespaces + ACL band
  search           — keyword search a namespace; returns scored passages
  get_document     — fetch the full text of a document by id

Use cases: 59 (KB-grounded reply drafting), 66 (HR policy Q&A),
           93 (RFP content library).
"""

import asyncio

from mcp.servers.base import BaseMCPServer, MCPTool


class KBSearchMCPServer(BaseMCPServer):

    server_name = "kb_search"

    def _setup_tools(self):
        from tools.kb_search_tools import (
            list_namespaces,
            search,
            get_document,
        )

        self._register(MCPTool(
            name="list_namespaces",
            description="List configured KB namespaces and their ACL band.",
            fn=list_namespaces,
            input_schema={"type": "object", "properties": {}},
        ))

        self._register(MCPTool(
            name="search",
            description=(
                "Search a KB namespace for passages relevant to the query. "
                "Returns scored passages with source doc ids for citation."
            ),
            fn=search,
            input_schema={
                "type": "object",
                "properties": {
                    "namespace": {"type": "string", "description": "Namespace to search"},
                    "query":     {"type": "string", "description": "Search query"},
                    "top_k":     {"type": "integer", "description": "Max passages (0 = use default)", "default": 0},
                },
                "required": ["namespace", "query"],
            },
        ))

        self._register(MCPTool(
            name="get_document",
            description="Fetch the full text of a specific document in a namespace by its doc_id.",
            fn=get_document,
            input_schema={
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "doc_id":    {"type": "string"},
                    "max_chars": {"type": "integer", "default": 20000},
                },
                "required": ["namespace", "doc_id"],
            },
        ))


if __name__ == "__main__":
    asyncio.run(KBSearchMCPServer().run_stdio())
