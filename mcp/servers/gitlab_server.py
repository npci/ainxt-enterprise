# SPDX-License-Identifier: MIT
"""
GitLab MCP Server — wraps tools/gitlab_tools.py as a spec-compliant MCP server.

Tools exposed:
  gitlab_list_projects    — list projects/repos the user has access to
  gitlab_read_file        — read a file from a repo
  gitlab_list_issues      — list issues
  gitlab_create_issue     — create an issue
  gitlab_list_mrs         — list merge requests
  gitlab_create_mr        — create a merge request
  gitlab_create_branch    — create a branch
  gitlab_comment_on_mr    — comment on an MR
  gitlab_merge_mr         — merge an approved MR
  gitlab_get_mr_files     — get files changed in an MR
  gitlab_create_or_update_file — create or update a file

Token injection:
  handle_message() is overridden to inject the requesting user's GitLab PAT
  (from user_tokens table via core/platform_credentials) into the thread-local
  before dispatching any tools/call.  This is the same set_token() pattern the
  SDLC pipeline uses — ensuring Desktop MCP and Buddy use the per-user token
  rather than falling back to the GITLAB_TOKEN env var.
"""

import asyncio
from typing import Optional

from core.logger import logger
from mcp.servers.base import BaseMCPServer, MCPTool


class GitLabMCPServer(BaseMCPServer):

    server_name = "gitlab"

    def _setup_tools(self):
        from tools.gitlab_tools import (
            gitlab_list_projects,
            gitlab_read_file,
            gitlab_list_issues,
            gitlab_create_issue,
            gitlab_list_mrs,
            gitlab_create_mr,
            gitlab_create_branch,
            gitlab_comment_on_mr,
            gitlab_merge_mr,
            gitlab_get_mr_files,
            gitlab_create_or_update_file,
            gitlab_list_commits,
            gitlab_get_project,
            gitlab_search_code,
        )

        self._register(MCPTool(
            name="gitlab_list_projects",
            description=(
                "List GitLab projects/repositories the authenticated user has access to. "
                "Use this to answer questions like 'how many repos do I have access to' or "
                "'show me my GitLab projects'."
            ),
            fn=gitlab_list_projects,
            input_schema={
                "type": "object",
                "properties": {
                    "limit":      {"type": "integer", "description": "Max projects to return (default 50, max 100)", "default": 50},
                    "membership": {"type": "boolean", "description": "Only return projects the user is a member of (default true)", "default": True},
                    "search":     {"type": "string",  "description": "Filter projects by name"},
                },
                "required": [],
            },
            pci_audit=False,
        ))

        self._register(MCPTool(
            name="gitlab_read_file",
            description=(
                "Read the contents of a file from a GitLab repository at a specific branch. "
                "Use to inspect source code, config files, Dockerfiles, or CI/CD pipelines."
            ),
            fn=gitlab_read_file,
            input_schema={
                "type": "object",
                "properties": {
                    "repo":   {"type": "string", "description": "Repository in 'org/project' format"},
                    "path":   {"type": "string", "description": "File path within the repo (e.g. src/main/App.java)"},
                    "branch": {"type": "string", "description": "Branch name", "default": "main"},
                },
                "required": ["repo", "path"],
            },
        ))

        self._register(MCPTool(
            name="gitlab_list_issues",
            description="List open or closed issues in a GitLab repository.",
            fn=gitlab_list_issues,
            input_schema={
                "type": "object",
                "properties": {
                    "repo":  {"type": "string", "description": "Repository in 'org/project' format"},
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                    "limit": {"type": "integer", "description": "Max issues to return", "default": 20},
                },
                "required": ["repo"],
            },
        ))

        self._register(MCPTool(
            name="gitlab_create_issue",
            description="Create a new issue in a GitLab repository.",
            fn=gitlab_create_issue,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "repo":   {"type": "string", "description": "Repository in 'org/project' format"},
                    "title":  {"type": "string", "description": "Issue title"},
                    "body":   {"type": "string", "description": "Issue description (markdown)"},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Labels to apply"},
                },
                "required": ["repo", "title"],
            },
        ))

        self._register(MCPTool(
            name="gitlab_list_mrs",
            description="List merge requests in a GitLab repository.",
            fn=gitlab_list_mrs,
            input_schema={
                "type": "object",
                "properties": {
                    "repo":  {"type": "string", "description": "Repository in 'org/project' format"},
                    "state": {"type": "string", "enum": ["open", "closed", "merged", "all"], "default": "open"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["repo"],
            },
        ))

        self._register(MCPTool(
            name="gitlab_create_mr",
            description=(
                "Create a merge request in GitLab. "
                "Always check for existing open MR for the same branch before creating — handles 409 idempotently."
            ),
            fn=gitlab_create_mr,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "repo":  {"type": "string", "description": "Repository in 'org/project' format"},
                    "title": {"type": "string", "description": "MR title"},
                    "body":  {"type": "string", "description": "MR description"},
                    "head":  {"type": "string", "description": "Source branch name"},
                    "base":  {"type": "string", "description": "Target branch (merge into)", "default": "main"},
                },
                "required": ["repo", "title", "body", "head"],
            },
        ))

        self._register(MCPTool(
            name="gitlab_create_branch",
            description="Create a new branch in a GitLab repository from a base branch.",
            fn=gitlab_create_branch,
            input_schema={
                "type": "object",
                "properties": {
                    "repo":        {"type": "string", "description": "Repository in 'org/project' format"},
                    "branch":      {"type": "string", "description": "New branch name"},
                    "from_branch": {"type": "string", "description": "Base branch", "default": "main"},
                },
                "required": ["repo", "branch"],
            },
        ))

        self._register(MCPTool(
            name="gitlab_comment_on_mr",
            description="Add a comment to a GitLab merge request (use for code review feedback).",
            fn=gitlab_comment_on_mr,
            input_schema={
                "type": "object",
                "properties": {
                    "repo":   {"type": "string", "description": "Repository in 'org/project' format"},
                    "mr_iid": {"type": "integer", "description": "MR internal ID"},
                    "body":   {"type": "string", "description": "Comment body (markdown)"},
                },
                "required": ["repo", "mr_iid", "body"],
            },
        ))

        self._register(MCPTool(
            name="gitlab_merge_mr",
            description="Merge an approved merge request. Only call after all checks pass.",
            fn=gitlab_merge_mr,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "repo":         {"type": "string", "description": "Repository in 'org/project' format"},
                    "mr_iid":       {"type": "integer", "description": "MR internal ID"},
                    "merge_method": {"type": "string", "enum": ["squash", "merge", "rebase"], "default": "squash"},
                },
                "required": ["repo", "mr_iid"],
            },
        ))

        self._register(MCPTool(
            name="gitlab_get_mr_files",
            description="Get the list of files changed in a merge request (for code review context).",
            fn=gitlab_get_mr_files,
            input_schema={
                "type": "object",
                "properties": {
                    "repo":      {"type": "string", "description": "Repository in 'org/project' format"},
                    "mr_iid":    {"type": "integer", "description": "MR internal ID"},
                    "max_files": {"type": "integer", "default": 20},
                },
                "required": ["repo", "mr_iid"],
            },
        ))

        self._register(MCPTool(
            name="gitlab_create_or_update_file",
            description=(
                "Create or update a file in a GitLab repository. "
                "Use for committing AI-generated code, config changes, or documentation."
            ),
            fn=gitlab_create_or_update_file,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "repo":    {"type": "string", "description": "Repository in 'org/project' format"},
                    "path":    {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "File content"},
                    "message": {"type": "string", "description": "Commit message"},
                    "branch":  {"type": "string", "description": "Branch to commit to", "default": "main"},
                },
                "required": ["repo", "path", "content", "message"],
            },
        ))

        self._register(MCPTool(
            name="gitlab_list_commits",
            description="List commits in a GitLab repository branch.",
            fn=gitlab_list_commits,
            input_schema={
                "type": "object",
                "properties": {
                    "repo":     {"type": "string", "description": "Repository in 'org/project' format"},
                    "ref_name": {"type": "string", "description": "Branch or tag name (default: repo default branch)", "default": ""},
                    "limit":    {"type": "integer", "description": "Max commits to return (default 25)", "default": 25},
                },
                "required": ["repo"],
            },
        ))

        self._register(MCPTool(
            name="gitlab_get_project",
            description=(
                "Get metadata for a GitLab project: id, description, default branch, "
                "visibility, star count, and HTTP clone URL."
            ),
            fn=gitlab_get_project,
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in 'org/project' format"},
                },
                "required": ["repo"],
            },
        ))

        self._register(MCPTool(
            name="gitlab_search_code",
            description=(
                "Search for a text pattern in source files within a GitLab repository. "
                "Returns matching file paths and line snippets."
            ),
            fn=gitlab_search_code,
            input_schema={
                "type": "object",
                "properties": {
                    "repo":        {"type": "string", "description": "Repository in 'org/project' format"},
                    "query":       {"type": "string", "description": "Search query (text or regex)"},
                    "max_results": {"type": "integer", "description": "Max results to return (default 10)", "default": 10},
                },
                "required": ["repo", "query"],
            },
        ))

    # ── Per-user token injection ───────────────────────────────────────────────

    async def handle_message(
        self,
        body: dict,
        session_id: str = None,
        user_id: str = None,
    ) -> Optional[dict]:
        """
        Override BaseMCPServer.handle_message to inject the requesting user's
        GitLab PAT before any tools/call dispatch.

        This serves the internal MCP server path exposed by mcp_server_router.py
        (GET /mcp/gitlab/sse + POST /mcp/gitlab/message + POST /mcp/gitlab/sse).
        Callers include MCPBridge (SDLC IDE integration) and any external client
        that connects directly to the internal /mcp/gitlab endpoint.

        NOTE: Buddy/Cowork does NOT use this path. Buddy routes through
        connectors/mcp_bridge.py → connectors/adapters/gitlab.GitLabAdapter,
        which performs its own set_token() injection via context.access_token.

        Without this override, tools/gitlab_tools._resolve_token() falls back to
        the GITLAB_TOKEN env var (a service-account token), meaning MCPBridge
        tool calls would run as the service account rather than the requesting user.

        Token injection uses the same set_token() / thread-local pattern as the
        SDLC pipeline (agents/sdlc_pipeline.py → platform_credentials → set_token).
        The token is always cleared in a finally block so it never leaks across
        concurrent requests.
        """
        method = body.get("method", "") if isinstance(body, dict) else ""

        if user_id and method == "tools/call":
            from tools.gitlab_tools import set_token
            token_injected = False
            try:
                from core.platform_credentials import get_gitlab_token
                pat = get_gitlab_token(user_id=user_id)
                if pat:
                    set_token(pat)
                    token_injected = True
            except Exception as e:
                # PermissionError = user has no token stored; fall through to env var
                logger.debug(f"GitLabMCPServer: token lookup for user {user_id} → {e}")

            try:
                return await super().handle_message(body, session_id)
            finally:
                if token_injected:
                    set_token("")  # always clear — never leak across threads
        else:
            return await super().handle_message(body, session_id)


if __name__ == "__main__":
    asyncio.run(GitLabMCPServer().run_stdio())
