# SPDX-License-Identifier: MIT
"""
document_tools MCP Server — wraps tools/document_tools_tools.py as a
spec-compliant MCP server.

Tools exposed:
  list_documents      — list readable documents under data_dir
  extract_text        — pull plain text out of a document
  search_in_document  — find query occurrences with surrounding context

Use cases: 59, 62, 67, 72, 73, 74, 93 (any flow that ingests a
PDF/MD/TXT document).
"""

import asyncio

from mcp.servers.base import BaseMCPServer, MCPTool


class DocumentToolsMCPServer(BaseMCPServer):

    server_name = "document_tools"

    def _setup_tools(self):
        from tools.document_tools_tools import (
            list_documents,
            extract_text,
            extract_text_batch,
            search_in_document,
        )

        self._register(MCPTool(
            name="list_documents",
            description=(
                "List readable documents (pdf/md/txt/csv/eml/json/html/docx/xls/xlsx) "
                "under the configured document root, optionally within a subfolder."
            ),
            fn=list_documents,
            input_schema={
                "type": "object",
                "properties": {
                    "subfolder": {"type": "string", "description": "Optional subfolder", "default": ""},
                },
            },
        ))

        self._register(MCPTool(
            name="extract_text",
            description="Extract plain text from a document (PDF, Word .docx, Excel .xls/.xlsx, HTML, or plain text) given its path relative to the document root.",
            fn=extract_text,
            input_schema={
                "type": "object",
                "properties": {
                    "path":      {"type": "string"},
                    "max_chars": {"type": "integer", "default": 20000},
                },
                "required": ["path"],
            },
        ))

        self._register(MCPTool(
            name="extract_text_batch",
            description=("Extract text from MANY documents in ONE call — use this instead of "
                         "calling extract_text repeatedly when you must read dozens/hundreds "
                         "of files (e.g. a folder of HTML reports). Caps per-file and total "
                         "text so a large batch fits in one turn; returns any skipped files "
                         "to fetch in a follow-up call."),
            fn=extract_text_batch,
            input_schema={
                "type": "object",
                "properties": {
                    "paths":             {"type": "array", "items": {"type": "string"}},
                    "max_chars_each":    {"type": "integer", "default": 4000},
                    "total_char_budget": {"type": "integer", "default": 120000},
                },
                "required": ["paths"],
            },
        ))

        self._register(MCPTool(
            name="search_in_document",
            description="Find occurrences of a query string (case-insensitive) inside a document; returns surrounding context snippets.",
            fn=search_in_document,
            input_schema={
                "type": "object",
                "properties": {
                    "path":          {"type": "string"},
                    "query":         {"type": "string"},
                    "context_chars": {"type": "integer", "default": 300},
                },
                "required": ["path", "query"],
            },
        ))


if __name__ == "__main__":
    asyncio.run(DocumentToolsMCPServer().run_stdio())
