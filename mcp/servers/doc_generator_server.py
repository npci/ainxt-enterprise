# SPDX-License-Identifier: Apache-2.0
"""
doc_generator MCP Server — wraps the MCP-tool surface appended at the
bottom of tools/doc_generator.py.

Tools exposed:
  write_markdown      — write a .md to the generated-docs outbox
  markdown_to_docx    — render simple markdown to a .docx
  slides_to_pptx      — render a list of slides to a .pptx

Use cases: 71 (financial report), 82 (press release), 91 (deck generation),
           93 (RFP response), 95 (policy/SOP drafting),
           96 (training material).
"""

import asyncio

from mcp.servers.base import BaseMCPServer, MCPTool


class DocGeneratorMCPServer(BaseMCPServer):

    server_name = "doc_generator"

    def _setup_tools(self):
        from tools.doc_generator import (
            write_markdown,
            markdown_to_docx,
            slides_to_pptx,
        )

        self._register(MCPTool(
            name="write_markdown",
            description="Write markdown content to a .md file in the generated-docs outbox.",
            fn=write_markdown,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "content":  {"type": "string"},
                },
                "required": ["filename", "content"],
            },
        ))

        self._register(MCPTool(
            name="markdown_to_docx",
            description="Render simple markdown (#/##/### headings, bullets, plain paragraphs) into a .docx file.",
            fn=markdown_to_docx,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "filename":         {"type": "string"},
                    "markdown_content": {"type": "string"},
                    "title":            {"type": "string", "default": ""},
                },
                "required": ["filename", "markdown_content"],
            },
        ))

        self._register(MCPTool(
            name="slides_to_pptx",
            description=(
                "Render slides into a .pptx. Each slide is "
                "{title: str, bullets: [str], notes?: str}."
            ),
            fn=slides_to_pptx,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "slides": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title":   {"type": "string"},
                                "bullets": {"type": "array", "items": {"type": "string"}},
                                "notes":   {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["filename", "slides"],
            },
        ))


if __name__ == "__main__":
    asyncio.run(DocGeneratorMCPServer().run_stdio())
