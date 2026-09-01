# SPDX-License-Identifier: Apache-2.0
"""
translator MCP Server — wraps tools/translator_tools.py.

Tools exposed:
  load_glossary        — load a CSV glossary (term + per-locale columns)
  translate_segments   — translate a list of segments to target_locale
  save_translation     — persist a translated document to the outbox

Use case: 94 (document translation & localization).
"""

import asyncio

from mcp.servers.base import BaseMCPServer, MCPTool


class TranslatorMCPServer(BaseMCPServer):

    server_name = "translator"

    def _setup_tools(self):
        from tools.translator_tools import (
            load_glossary,
            translate_segments,
            save_translation,
        )

        self._register(MCPTool(
            name="load_glossary",
            description="Load a glossary CSV (term, per-locale columns, instruction) to constrain translation.",
            fn=load_glossary,
            input_schema={
                "type": "object",
                "properties": {
                    "glossary_csv_path": {"type": "string"},
                },
                "required": ["glossary_csv_path"],
            },
        ))

        self._register(MCPTool(
            name="translate_segments",
            description=(
                "Translate text segments to target_locale honouring glossary rules. "
                "glossary_demo returns annotated segments for the agent to translate; "
                "mt_http calls the configured MT engine."
            ),
            fn=translate_segments,
            input_schema={
                "type": "object",
                "properties": {
                    "segments":      {"type": "array", "items": {"type": "string"}},
                    "target_locale": {"type": "string"},
                    "glossary": {
                        "type": "array",
                        "items": {"type": "object"},
                        "default": [],
                    },
                },
                "required": ["segments", "target_locale"],
            },
        ))

        self._register(MCPTool(
            name="save_translation",
            description="Persist a translated document to the translations outbox as <filename>.<locale>.md.",
            fn=save_translation,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "locale":   {"type": "string"},
                    "content":  {"type": "string"},
                },
                "required": ["filename", "locale", "content"],
            },
        ))


if __name__ == "__main__":
    asyncio.run(TranslatorMCPServer().run_stdio())
