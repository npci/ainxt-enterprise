# SPDX-License-Identifier: Apache-2.0
"""
Jira MCP Server — wraps tools/jira_tools.py as a spec-compliant MCP server.

Tools exposed:
  jira_create_issue    — create a new issue
  jira_list_issues     — list issues by project/status
  jira_get_issue       — get issue details by key
  jira_update_issue    — update status/comment/assignee/priority
  jira_add_comment     — add a comment to an issue
  jira_transition_issue — transition issue to a new status
  jira_link_issues     — link two issues together

Run as stdio server:
    python -m mcp.servers.jira_server

Exposed over SSE at:
    GET  /mcp/jira/sse
    POST /mcp/jira/message
"""

import sys
import asyncio

from mcp.servers.base import BaseMCPServer, MCPTool


class JiraMCPServer(BaseMCPServer):

    server_name = "jira"

    def _setup_tools(self):
        from tools.jira_tools import (
            jira_create_issue,
            jira_list_issues,
            jira_get_issue,
            jira_update_issue,
            jira_add_comment,
            jira_transition_issue,
            jira_link_issues,
        )

        self._register(MCPTool(
            name="jira_create_issue",
            description=(
                "Create a new Jira issue. Returns the created issue key (e.g. AiNxt-1234). "
                "Use for bug reports, feature requests, incident tickets, or task creation."
            ),
            fn=jira_create_issue,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "summary":     {"type": "string", "description": "Issue title/summary"},
                    "description": {"type": "string", "description": "Detailed description (markdown)"},
                    "project":     {"type": "string", "description": "Jira project key (e.g. AiNxt, PAY, INFRA)"},
                    "priority":    {"type": "string", "enum": ["Highest", "High", "Medium", "Low", "Lowest"], "default": "Medium"},
                    "issue_type":  {"type": "string", "enum": ["Bug", "Story", "Task", "Epic", "Incident"], "default": "Task"},
                    "user_id":     {"type": "string", "description": "Platform user ID (for audit trail)"},
                },
                "required": ["summary", "description"],
            },
        ))

        self._register(MCPTool(
            name="jira_list_issues",
            description=(
                "List Jira issues for a project, filtered by status. "
                "Returns a formatted summary of open/in-progress/closed issues."
            ),
            fn=jira_list_issues,
            input_schema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Jira project key"},
                    "status":  {"type": "string", "description": "Filter by status: Open, In Progress, Done, Closed", "default": "Open"},
                },
                "required": ["project"],
            },
        ))

        self._register(MCPTool(
            name="jira_get_issue",
            description="Get full details of a Jira issue by its key (e.g. AiNxt-1234).",
            fn=jira_get_issue,
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Jira issue key (e.g. PAY-456)"},
                },
                "required": ["issue_key"],
            },
        ))

        self._register(MCPTool(
            name="jira_update_issue",
            description=(
                "Update a Jira issue. Can change status, add a comment, reassign, or change priority. "
                "All fields are optional — only provide what needs updating."
            ),
            fn=jira_update_issue,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key":          {"type": "string", "description": "Jira issue key"},
                    "status":             {"type": "string", "description": "New status name"},
                    "comment":            {"type": "string", "description": "Comment to add"},
                    "assignee_account_id":{"type": "string", "description": "Jira account ID to assign"},
                    "priority":           {"type": "string", "enum": ["Highest", "High", "Medium", "Low", "Lowest"]},
                },
                "required": ["issue_key"],
            },
        ))

        self._register(MCPTool(
            name="jira_add_comment",
            description="Add a comment to an existing Jira issue.",
            fn=jira_add_comment,
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Jira issue key"},
                    "comment":   {"type": "string", "description": "Comment text (markdown supported)"},
                },
                "required": ["issue_key", "comment"],
            },
        ))

        self._register(MCPTool(
            name="jira_transition_issue",
            description="Transition a Jira issue to a new workflow status (e.g. 'In Progress', 'Done', 'Blocked').",
            fn=jira_transition_issue,
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string"},
                    "status":    {"type": "string", "description": "Target status name"},
                },
                "required": ["issue_key", "status"],
            },
        ))

        self._register(MCPTool(
            name="jira_link_issues",
            description="Create a link between two Jira issues (e.g. 'blocks', 'relates to', 'duplicates').",
            fn=jira_link_issues,
            input_schema={
                "type": "object",
                "properties": {
                    "inward_key":  {"type": "string", "description": "Source issue key"},
                    "outward_key": {"type": "string", "description": "Target issue key"},
                    "link_type":   {"type": "string", "description": "Link type", "default": "relates to"},
                },
                "required": ["inward_key", "outward_key"],
            },
        ))


if __name__ == "__main__":
    asyncio.run(JiraMCPServer().run_stdio())
