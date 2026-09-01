# SPDX-License-Identifier: Apache-2.0
"""
ats_tools MCP Server — wraps tools/ats_tools_tools.py.

Tools exposed:
  list_pipeline           — list candidates in the requisition pipeline
  score_keyword_overlap   — deterministic JD-vs-resume keyword score (0-100)
  propose_stage_update    — write a PROPOSED stage change to the outbox

Use cases: 62 (resume-to-JD matching), 63 (interview scheduling),
           64 (candidate follow-up sequences).
"""

import asyncio

from mcp.servers.base import BaseMCPServer, MCPTool


class ATSToolsMCPServer(BaseMCPServer):

    server_name = "ats_tools"

    def _setup_tools(self):
        from tools.ats_tools_tools import (
            list_pipeline,
            score_keyword_overlap,
            propose_stage_update,
        )

        self._register(MCPTool(
            name="list_pipeline",
            description="List candidates in the configured requisition pipeline, optionally filtered by stage.",
            fn=list_pipeline,
            input_schema={
                "type": "object",
                "properties": {
                    "stage": {"type": "string", "default": ""},
                },
            },
        ))

        self._register(MCPTool(
            name="score_keyword_overlap",
            description="Deterministic keyword-coverage score of a resume against JD requirement phrases (0-100).",
            fn=score_keyword_overlap,
            input_schema={
                "type": "object",
                "properties": {
                    "resume_text":     {"type": "string"},
                    "jd_must_have":    {"type": "array", "items": {"type": "string"}},
                    "jd_nice_to_have": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["resume_text", "jd_must_have", "jd_nice_to_have"],
            },
        ))

        self._register(MCPTool(
            name="propose_stage_update",
            description=(
                "Write a PROPOSED stage change to the outbox for recruiter "
                "confirmation (no direct ATS write)."
            ),
            fn=propose_stage_update,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "new_stage":    {"type": "string"},
                    "rationale":    {"type": "string"},
                },
                "required": ["candidate_id", "new_stage", "rationale"],
            },
        ))


if __name__ == "__main__":
    asyncio.run(ATSToolsMCPServer().run_stdio())
