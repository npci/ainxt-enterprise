# SPDX-License-Identifier: Apache-2.0
"""
Confluence MCP Server — wraps tools/confluence_tools.py as a spec-compliant MCP server.

Tools exposed:
  confluence_search        — full-text search across spaces
  confluence_get_page      — get page by ID
  confluence_get_by_title  — get page by title and space
  confluence_create_page   — create a new page
  confluence_update_page   — update existing page content
"""

import asyncio

from mcp.servers.base import BaseMCPServer, MCPTool


class ConfluenceMCPServer(BaseMCPServer):

    server_name = "confluence"

    def _setup_tools(self):
        from tools.confluence_tools import (
            confluence_search,
            confluence_get_page,
            confluence_get_page_by_title,
            confluence_create_page,
            confluence_update_page,
        )

        self._register(MCPTool(
            name="confluence_search",
            description=(
                "Full-text search across Confluence spaces. Returns page titles, excerpts, and URLs. "
                "Use to find runbooks, architecture docs, API specs, HR policies, compliance guides."
            ),
            fn=confluence_search,
            input_schema={
                "type": "object",
                "properties": {
                    "query":     {"type": "string", "description": "Search query (CQL supported)"},
                    "space_key": {"type": "string", "description": "Limit search to a specific space (e.g. AiNxt, ARCH, HR)"},
                },
                "required": ["query"],
            },
        ))

        self._register(MCPTool(
            name="confluence_get_page",
            description="Retrieve the full content of a Confluence page by its numeric page ID.",
            fn=confluence_get_page,
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Confluence page ID (numeric)"},
                },
                "required": ["page_id"],
            },
        ))

        self._register(MCPTool(
            name="confluence_get_page_by_title",
            description="Find and retrieve a Confluence page by its exact title within a space.",
            fn=confluence_get_page_by_title,
            input_schema={
                "type": "object",
                "properties": {
                    "title":     {"type": "string", "description": "Exact page title"},
                    "space_key": {"type": "string", "description": "Space to search in"},
                },
                "required": ["title"],
            },
        ))

        self._register(MCPTool(
            name="confluence_create_page",
            description=(
                "Create a new Confluence page. "
                "Use to publish SDLC documentation, incident post-mortems, runbooks, or design specs."
            ),
            fn=confluence_create_page,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "title":     {"type": "string", "description": "Page title"},
                    "body":      {"type": "string", "description": "Page body (HTML or wiki markup)"},
                    "space_key": {"type": "string", "description": "Target space key"},
                    "parent_id": {"type": "string", "description": "Parent page ID (optional)"},
                },
                "required": ["title", "body", "space_key"],
            },
        ))

        self._register(MCPTool(
            name="confluence_update_page",
            description="Update the content of an existing Confluence page by page ID.",
            fn=confluence_update_page,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Confluence page ID"},
                    "title":   {"type": "string", "description": "Updated page title"},
                    "body":    {"type": "string", "description": "New page body content"},
                },
                "required": ["page_id", "title", "body"],
            },
        ))


if __name__ == "__main__":
    asyncio.run(ConfluenceMCPServer().run_stdio())
