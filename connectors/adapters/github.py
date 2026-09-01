# SPDX-License-Identifier: Apache-2.0
"""
GitHub custom adapter — GitHub REST API v3 (api.github.com).

Handles GitHub-specific patterns:
  - Bearer token authentication (fine-grained or classic PAT)
  - Link-header style pagination is not used here — GitHub also accepts
    page/per_page query params, and every gitlab_tools-equivalent function
    in tools/github_tools.py returns a plain list (no next-cursor), same
    contract as tools/gitlab_tools.py.
  - owner/repo path segments (no URL-encoding needed, unlike GitLab's
    namespace/project which uses '/' inside a single path segment)
  - Per-user token injection via tools/github_tools.set_token()

This adapter is a thin dispatch layer over tools/github_tools.py — the
canonical GitHub HTTP client shared with the SDLC pipeline (once GAP-41's
pipeline-side port lands; today it is used by chat/Cowork and CodebaseManager
indexing). No HTTP calls are duplicated here; the adapter's job is to:
  1. Receive the decrypted per-user PAT from ConnectorEngine (context.access_token)
  2. Inject it into the thread-local via set_token() so github_tools picks it up
  3. Map the connector tool name → the matching github_tools function
  4. Return an AdapterPage so the engine's pagination/compliance pipeline works

Mirrors connectors/adapters/gitlab.py's structure exactly so the two
providers stay interchangeable via SCM_PROVIDER (core/config.py).
"""
from __future__ import annotations

from typing import Optional

from connectors.adapters.base import AdapterBase, AdapterPage
from connectors.base import ConnectorContext, ConnectorTool
from core.logger import logger


class GitHubAdapter(AdapterBase):
    """
    Custom adapter for the GitHub REST API v3.

    Delegates all HTTP work to tools/github_tools.py after injecting the
    per-user PAT via set_token(). This ensures Buddy/Cowork and (once wired)
    the SDLC pipeline use exactly the same code path — no duplication.
    """

    TIMEOUT = 20

    # ── Tool dispatch map ─────────────────────────────────────────────────────
    # Maps connector tool name → (github_tools function, is_paginated)
    # Paginated tools return a list; non-paginated return a dict/str.
    _TOOL_MAP: dict = {
        # Read tools
        "github_read_file":             ("github_read_file",             False),
        "github_list_issues":           ("github_list_issues",           True),
        "github_list_prs":              ("github_list_prs",              True),
        "github_get_pr":                ("github_get_pr",                False),
        "github_get_pr_files":          ("github_get_pr_files",          False),
        # Write tools
        "github_create_issue":          ("github_create_issue",          False),
        "github_create_pr":             ("github_create_pr",             False),
        "github_create_branch":         ("github_create_branch",         False),
        "github_comment_on_pr":         ("github_comment_on_pr",         False),
        "github_merge_pr":              ("github_merge_pr",              False),
        "github_create_or_update_file": ("github_create_or_update_file", False),
    }

    def execute(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str] = None,
    ) -> AdapterPage:
        """
        Execute one GitHub tool call.

        Injects the per-user PAT into the thread-local, delegates to the
        matching github_tools function, then clears the token.
        """
        from tools.github_tools import set_token

        # Inject per-user token — same pattern as the GitLab adapter
        set_token(context.access_token)
        try:
            return self._dispatch(tool.name, params, cursor)
        finally:
            set_token("")  # always clear — never leak across threads

    # ── Internal dispatch ─────────────────────────────────────────────────────

    def _dispatch(self, tool_name: str, params: dict, cursor: Optional[str]) -> AdapterPage:
        """Route tool_name to the correct github_tools function."""
        entry = self._TOOL_MAP.get(tool_name)
        if entry is None:
            raise ValueError(f"GitHubAdapter: unknown tool '{tool_name}'")

        fn_name, is_list = entry

        try:
            import tools.github_tools as _gh
            fn = getattr(_gh, fn_name)
        except AttributeError:
            raise ValueError(f"GitHubAdapter: tools.github_tools has no function '{fn_name}'")

        # Normalise params: connector engine passes repo_id for path-based tools;
        # github_tools uses 'repo' (owner/repo string). Map transparently.
        call_params = self._normalise_params(tool_name, params)

        # Inject cursor as 'page' param for paginated tools (github_tools list_*
        # functions don't natively page — the engine stops once items < max_items).
        if cursor and is_list:
            call_params["page"] = cursor

        result = fn(**call_params)

        # Wrap result in AdapterPage
        if is_list:
            items = result if isinstance(result, list) else ([result] if result else [])
            return AdapterPage(items=items, next_cursor=None)
        else:
            if isinstance(result, dict):
                return AdapterPage(items=[result], next_cursor=None)
            elif isinstance(result, list):
                return AdapterPage(items=result, next_cursor=None)
            elif isinstance(result, str):
                return AdapterPage(items=[{"content": result}], next_cursor=None)
            elif result is None:
                return AdapterPage(items=[], next_cursor=None)
            else:
                return AdapterPage(items=[{"result": str(result)}], next_cursor=None)

    def _normalise_params(self, tool_name: str, params: dict) -> dict:
        """
        Normalise connector-engine param names to github_tools param names.

        The connector engine uses 'repo_id' (the seeded tool's path param,
        mirroring GitLab's 'project_id') while github_tools uses 'repo'
        (owner/repo string). Also strips None values so github_tools
        defaults apply cleanly.
        """
        p = {k: v for k, v in params.items() if v is not None}

        _REPO_TOOLS = {
            "github_read_file", "github_list_issues", "github_list_prs",
            "github_get_pr", "github_get_pr_files", "github_create_issue",
            "github_create_pr", "github_create_branch", "github_comment_on_pr",
            "github_merge_pr", "github_create_or_update_file",
        }
        if tool_name in _REPO_TOOLS and "repo_id" in p and "repo" not in p:
            p["repo"] = p.pop("repo_id")

        # state: connector seed may use "opened"/"closed" (GitLab convention);
        # github_tools uses "open"/"closed" like the raw GitHub API.
        if "state" in p and p["state"] == "opened":
            p["state"] = "open"

        # head/base: GitHub PR creation uses head/base branch names directly,
        # matching github_tools.github_create_pr's signature already.

        # Drop params the target function does not accept, so a hallucinated —
        # or engine-injected (e.g. the cost-guardrail's "limit") — extra argument
        # returns a clean result instead of a TypeError. Mirrors GitLabAdapter's
        # _ALLOWED_PARAMS pattern.
        allowed = self._ALLOWED_PARAMS.get(tool_name)
        if allowed is not None:
            dropped = set(p) - allowed
            if dropped:
                logger.debug(f"GitHubAdapter: dropping unsupported params for {tool_name}: {sorted(dropped)}")
            p = {k: v for k, v in p.items() if k in allowed}

        return p

    # Accepted parameters per tool, mirroring the tools/github_tools.py function
    # signatures exactly. user_id/credentials are intentionally excluded: the
    # PAT arrives through the thread-local (set_token()), never as a param.
    _ALLOWED_PARAMS: dict = {
        "github_read_file":             {"repo", "path", "branch"},
        "github_list_issues":           {"repo", "state", "limit"},
        "github_list_prs":              {"repo", "state", "limit"},
        "github_get_pr":                {"repo", "pr_number"},
        "github_get_pr_files":          {"repo", "pr_number", "max_files"},
        "github_create_issue":          {"repo", "title", "body", "labels"},
        "github_create_pr":             {"repo", "title", "body", "head", "base"},
        "github_create_branch":         {"repo", "branch", "from_branch"},
        "github_comment_on_pr":         {"repo", "pr_number", "body"},
        "github_merge_pr":              {"repo", "pr_number", "merge_method"},
        "github_create_or_update_file": {"repo", "path", "content", "message", "branch"},
    }


# Module-level singleton — ConnectorEngine._load_custom_adapter() discovers this
# via the AdapterBase instance scan (connector_name + "_adapter" convention).
github_adapter = GitHubAdapter()
