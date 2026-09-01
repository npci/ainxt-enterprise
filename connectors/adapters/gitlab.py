# SPDX-License-Identifier: Apache-2.0
"""
GitLab custom adapter — GitLab REST API v4.

Handles GitLab-specific patterns:
  - PRIVATE-TOKEN header authentication (PAT)
  - x-next-page / x-total-pages pagination headers (not @odata.nextLink)
  - URL-encoded namespace/project path segments
  - Per-user token injection via tools/gitlab_tools.set_token()

This adapter is a thin dispatch layer over tools/gitlab_tools.py — the
canonical GitLab HTTP client shared with the SDLC pipeline.  No HTTP calls
are duplicated here; the adapter's job is to:
  1. Receive the decrypted per-user PAT from ConnectorEngine (context.access_token)
  2. Inject it into the thread-local via set_token() so gitlab_tools picks it up
  3. Map the connector tool name → the matching gitlab_tools function
  4. Return an AdapterPage so the engine's pagination/compliance pipeline works

This is the same token-injection pattern the SDLC pipeline uses
(agents/sdlc_pipeline.py → platform_credentials.get_gitlab_token() → set_token()).
"""
from __future__ import annotations

import json
from typing import Optional
from urllib.parse import quote as _url_quote

from connectors.adapters.base import AdapterBase, AdapterPage
from connectors.base import ConnectorContext, ConnectorTool
from core.logger import logger


class GitLabAdapter(AdapterBase):
    """
    Custom adapter for GitLab REST API v4.

    Delegates all HTTP work to tools/gitlab_tools.py (the shared SDLC client)
    after injecting the per-user PAT via set_token().  This ensures Buddy/Cowork
    and the SDLC pipeline use exactly the same code path — no duplication.
    """

    TIMEOUT = 20

    # ── Tool dispatch map ─────────────────────────────────────────────────────
    # Maps connector tool name → (gitlab_tools function, is_paginated)
    # Paginated tools return a list; non-paginated return a dict/str.
    _TOOL_MAP: dict = {
        # Read tools
        "gitlab_list_projects":         ("gitlab_list_projects",         True),
        # Cross-project "my work" tools — NO repo/project_id required. These answer
        # "show me my open merge requests" / "my issues", which name no project and
        # therefore cannot be served by any project-scoped tool below. Without them
        # the model finds no valid call and falls back to a shell/git guess.
        "gitlab_list_my_mrs":           ("gitlab_list_my_mrs",           True),
        "gitlab_list_my_issues":        ("gitlab_list_my_issues",        True),
        "gitlab_list_issues":           ("gitlab_list_issues",           True),
        "gitlab_list_mrs":              ("gitlab_list_mrs",              True),
        "gitlab_list_commits":          ("gitlab_list_commits",          True),
        "gitlab_list_branches":         ("gitlab_list_branches",         True),
        "gitlab_get_project":           ("gitlab_get_project",           False),
        "gitlab_read_file":             ("gitlab_read_file",             False),
        "gitlab_get_mr_files":          ("gitlab_get_mr_files",          False),
        "gitlab_search_code":           ("gitlab_search_code",           True),
        # Write tools
        "gitlab_create_issue":          ("gitlab_create_issue",          False),
        "gitlab_create_mr":             ("gitlab_create_mr",             False),
        "gitlab_create_branch":         ("gitlab_create_branch",         False),
        "gitlab_comment_on_mr":         ("gitlab_comment_on_mr",         False),
        "gitlab_merge_mr":              ("gitlab_merge_mr",              False),
        "gitlab_create_or_update_file": ("gitlab_create_or_update_file", False),
    }

    def execute(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str] = None,
    ) -> AdapterPage:
        """
        Execute one GitLab tool call.

        Injects the per-user PAT into the thread-local, delegates to the
        matching gitlab_tools function, then clears the token.
        """
        from tools.gitlab_tools import set_token

        # Inject per-user token — same pattern as SDLC pipeline
        set_token(context.access_token)
        try:
            return self._dispatch(tool.name, params, cursor)
        finally:
            set_token("")  # always clear — never leak across threads

    # ── Internal dispatch ─────────────────────────────────────────────────────

    def _dispatch(self, tool_name: str, params: dict, cursor: Optional[str]) -> AdapterPage:
        """Route tool_name to the correct gitlab_tools function."""
        entry = self._TOOL_MAP.get(tool_name)
        if entry is None:
            raise ValueError(f"GitLabAdapter: unknown tool '{tool_name}'")

        fn_name, is_list = entry

        try:
            import tools.gitlab_tools as _gt
            fn = getattr(_gt, fn_name)
        except AttributeError:
            raise ValueError(f"GitLabAdapter: tools.gitlab_tools has no function '{fn_name}'")

        # Normalise params: connector engine passes project_id for path-based tools;
        # gitlab_tools uses 'repo' (namespace/project string).  Map transparently.
        call_params = self._normalise_params(tool_name, params)

        # Inject cursor as 'page' param for paginated tools
        if cursor and is_list:
            call_params["page"] = cursor

        result = fn(**call_params)

        # Wrap result in AdapterPage
        if is_list:
            items = result if isinstance(result, list) else ([result] if result else [])
            # gitlab_tools returns plain lists — no next_cursor embedded.
            # The engine's pagination loop will stop when items < max_items.
            return AdapterPage(items=items, next_cursor=None)
        else:
            # Single-item result (dict or string)
            if isinstance(result, dict):
                return AdapterPage(items=[result], next_cursor=None)
            elif isinstance(result, str):
                return AdapterPage(items=[{"content": result}], next_cursor=None)
            elif result is None:
                return AdapterPage(items=[], next_cursor=None)
            else:
                return AdapterPage(items=[{"result": str(result)}], next_cursor=None)

    def _normalise_params(self, tool_name: str, params: dict) -> dict:
        """
        Normalise connector-engine param names to gitlab_tools param names.

        The connector engine uses 'project_id' (GitLab API path param) while
        gitlab_tools uses 'repo' (namespace/project string).  For tools that
        take a project path, map project_id → repo.

        Also strips None values so gitlab_tools defaults apply cleanly.
        """
        p = {k: v for k, v in params.items() if v is not None}

        # project_id → repo mapping for project-scoped tools
        _PROJECT_TOOLS = {
            "gitlab_list_issues", "gitlab_list_mrs", "gitlab_list_commits",
            "gitlab_list_branches",
            "gitlab_get_project", "gitlab_read_file", "gitlab_get_mr_files",
            "gitlab_create_issue", "gitlab_create_mr", "gitlab_create_branch",
            "gitlab_comment_on_mr", "gitlab_merge_mr", "gitlab_create_or_update_file",
            "gitlab_search_code",
        }
        if tool_name in _PROJECT_TOOLS and "project_id" in p and "repo" not in p:
            p["repo"] = p.pop("project_id")

        # state: connector seed uses "opened"/"closed"; gitlab_tools uses "open"/"closed"
        if "state" in p and p["state"] == "opened":
            p["state"] = "open"

        # limit → max_results for search_code
        if tool_name == "gitlab_search_code" and "limit" in p and "max_results" not in p:
            p["max_results"] = p.pop("limit")

        # Drop params the target function does not accept, so a hallucinated —
        # or engine-injected (e.g. the cost-guardrail's "limit") — extra argument
        # returns a clean result instead of a TypeError. Mirrors JiraAdapter's
        # _ALLOWED_PARAMS pattern. This is what fixes:
        #   gitlab_create_branch() got an unexpected keyword argument 'limit'
        allowed = self._ALLOWED_PARAMS.get(tool_name)
        if allowed is not None:
            dropped = set(p) - allowed
            if dropped:
                logger.debug(f"GitLabAdapter: dropping unsupported params for {tool_name}: {sorted(dropped)}")
            p = {k: v for k, v in p.items() if k in allowed}

        return p

    # Accepted parameters per tool, mirroring the tools/gitlab_tools.py function
    # signatures exactly. user_id/credentials are intentionally excluded: the
    # PAT arrives through the thread-local (set_token()), never as a param.
    _ALLOWED_PARAMS: dict = {
        # Cross-project "my work" tools — no repo/project_id
        "gitlab_list_projects":         {"limit", "membership", "search"},
        "gitlab_list_my_mrs":           {"scope", "state", "limit"},
        "gitlab_list_my_issues":        {"scope", "state", "limit"},
        # Project-scoped read tools
        "gitlab_list_issues":           {"repo", "state", "limit"},
        "gitlab_list_mrs":              {"repo", "state", "limit"},
        "gitlab_list_commits":          {"repo", "ref_name", "limit"},
        "gitlab_get_project":           {"repo"},
        "gitlab_read_file":             {"repo", "path", "branch"},
        "gitlab_get_mr_files":          {"repo", "mr_iid", "max_files"},
        "gitlab_search_code":           {"repo", "query", "max_results"},
        "gitlab_list_branches":         {"repo", "search", "limit"},
        # Write tools — none of these accept "limit"
        "gitlab_create_issue":          {"repo", "title", "body", "labels"},
        "gitlab_create_mr":             {"repo", "title", "body", "head", "base", "draft"},
        "gitlab_create_branch":         {"repo", "branch", "from_branch"},
        "gitlab_comment_on_mr":         {"repo", "mr_iid", "body"},
        "gitlab_merge_mr":              {"repo", "mr_iid", "merge_method"},
        "gitlab_create_or_update_file": {"repo", "path", "content", "message", "branch"},
    }


# Module-level singleton — ConnectorEngine._load_custom_adapter() discovers this
# via the AdapterBase instance scan (connector_name + "_adapter" convention).
gitlab_adapter = GitLabAdapter()
