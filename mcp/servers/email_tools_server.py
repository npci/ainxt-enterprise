# SPDX-License-Identifier: Apache-2.0
"""
email_tools MCP Server — wraps tools/email_tools_tools.py.

Tools exposed:
  list_messages  — list message ids/from/subject/date from the mailbox
  read_message   — read full body of a message by id
  draft_reply    — write a DRAFT reply .eml to the outbox (never sends)

Use cases: 86 (exec inbox triage), 64 (candidate follow-up drafting).
Sending is intentionally absent — that is a gated tool registered
separately with critical=true.
"""

import asyncio

from mcp.servers.base import BaseMCPServer, MCPTool


class EmailToolsMCPServer(BaseMCPServer):

    server_name = "email_tools"

    def _setup_tools(self):
        from tools.email_tools_tools import (
            list_messages,
            read_message,
            draft_reply,
        )

        self._register(MCPTool(
            name="list_messages",
            description="List messages (id, from, subject, date) in the configured mailbox.",
            fn=list_messages,
            input_schema={"type": "object", "properties": {}},
        ))

        self._register(MCPTool(
            name="read_message",
            description="Read the full body of a message by id.",
            fn=read_message,
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                },
                "required": ["message_id"],
            },
        ))

        self._register(MCPTool(
            name="draft_reply",
            description=(
                "Write a DRAFT reply to the outbox (never sends). A human or a "
                "separately-approved send tool dispatches it."
            ),
            fn=draft_reply,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "body":       {"type": "string"},
                },
                "required": ["message_id", "body"],
            },
        ))


if __name__ == "__main__":
    asyncio.run(EmailToolsMCPServer().run_stdio())
