# SPDX-License-Identifier: MIT
"""
Jira custom adapter — Atlassian Jira Cloud REST API v3.

This adapter is a thin dispatch layer over tools/jira_tools.py — the canonical
Jira HTTP client shared with the SDLC pipeline.  No HTTP calls are duplicated
here; the adapter's job is to:
  1. Receive the decrypted per-user credential from ConnectorEngine
     (context.access_token, stored as "email:api_token")
  2. Split it via extract_atlassian_creds() and inject it into the thread-local
     with set_credentials() so jira_tools picks it up
  3. Map the connector tool name → the matching jira_tools function
  4. Return an AdapterPage so the engine's pagination/compliance pipeline works

This mirrors connectors/adapters/gitlab.py exactly, and it is the same
token-injection pattern the SDLC pipeline uses.

WHY DELEGATE INSTEAD OF CALLING httpx DIRECTLY
    tools/jira_tools.py::_request() carries four behaviours a raw httpx call in
    this module cannot have:

      * Relay through POST {LLM_PROXY_URL}/atlassian/proxy.  This is NOT optional:
        Atlassian Cloud is reachable only from web02, so a direct call from app02
        fails in production while appearing to work in local dev.
      * Circuit breaker (get_breaker("jira")).
      * retry_llm() with backoff on 429/5xx.
      * request_id / chat_id correlation, so a Jira call is traceable back to the
        originating /ask or SDLC run.

    An earlier version of this adapter re-implemented the HTTP, ADF encoding, ADF
    flattening, and issue normalisation locally and had none of the above.

KEEP _TOOL_MAP IN SYNC with the tool list in connectors/seed.py — a tool listed
there but unmapped raises "unknown tool", and a mapped tool missing from there is
unreachable.  Also confirm the DB row matches (a catch-up migration once
overwrote GitLab's row with fewer tools):
    SELECT jsonb_array_length(tools), has_custom_adapter
    FROM ainxt.connector_definitions WHERE name='jira_connector';  -- expect 13 / TRUE
"""
from __future__ import annotations

from typing import Optional

from connectors.adapters.base import AdapterBase, AdapterPage
from connectors.base import ConnectorContext, ConnectorTool
from core.logger import logger


class JiraAdapter(AdapterBase):
    """
    Custom adapter for Atlassian Jira Cloud REST API v3.

    Delegates all HTTP work to tools/jira_tools.py (the shared SDLC client) after
    injecting the per-user credentials via set_credentials().  This ensures
    Buddy/Cowork and the SDLC pipeline use exactly the same code path.
    """

    # ── Tool dispatch map ─────────────────────────────────────────────────────
    # Maps connector tool name → (jira_tools function, returns_list)
    _TOOL_MAP: dict = {
        # ── Read tools ────────────────────────────────────────────────────────
        # jira_get_current_user needs no params and changes nothing, so it is also
        # the connection-test probe (connectors/probe.py::select_probe).
        "jira_get_current_user": ("jira_get_current_user", False),
        "jira_search_issues":    ("jira_search_issues",    True),
        "jira_get_issue":        ("jira_get_issue_dict",   False),
        "jira_list_projects":    ("jira_list_projects",    True),
        "jira_get_project":      ("jira_get_project",      False),
        "jira_get_transitions":  ("jira_get_transitions",  True),
        "jira_list_comments":    ("jira_list_comments",    True),
        "jira_count_issues":     ("jira_count_issues",     False),
        # ── Write tools ───────────────────────────────────────────────────────
        "jira_create_issue":     ("jira_create_issue",     False),
        "jira_add_comment":      ("jira_add_comment",      False),
        "jira_update_issue":     ("jira_update_issue",     False),
        "jira_transition_issue": ("jira_transition_issue", False),
        "jira_assign_issue":     ("jira_assign_issue",     False),
    }

    def execute(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str] = None,
    ) -> AdapterPage:
        """
        Execute one Jira tool call.

        Injects the per-user credentials into the thread-local, delegates to the
        matching jira_tools function, then always clears them.
        """
        from core.platform_credentials import extract_atlassian_creds
        from tools.jira_tools import set_credentials, clear_credentials

        # access_token is stored as "email:api_token"; metadata.email is the
        # fallback for legacy rows that hold a bare token.
        email, token = extract_atlassian_creds(
            context.access_token,
            (context.metadata or {}).get("email", ""),
        )
        if not token:
            from connectors.base import ConnectorReauthRequired
            raise ConnectorReauthRequired(
                "No Atlassian API token found. Please reconnect Jira from Profile → API Token Vault."
            )

        set_credentials(email, token)
        try:
            return self._dispatch(tool.name, params, cursor)
        except Exception as e:
            # jira_tools raises plain Exceptions carrying "HTTP 401: ..." from
            # _request(); translate auth failures so the engine prompts a
            # reconnect instead of surfacing a raw error.
            if "401" in str(e) or "403" in str(e):
                from connectors.base import ConnectorReauthRequired
                raise ConnectorReauthRequired(
                    "Jira token is invalid or expired. Please reconnect."
                )
            raise
        finally:
            clear_credentials()  # always clear — never leak across threads

    # ── Internal dispatch ─────────────────────────────────────────────────────

    def _dispatch(self, tool_name: str, params: dict, cursor: Optional[str]) -> AdapterPage:
        """Route tool_name to the correct jira_tools function."""
        entry = self._TOOL_MAP.get(tool_name)
        if entry is None:
            raise ValueError(f"JiraAdapter: unknown tool '{tool_name}'")

        fn_name, is_list = entry

        try:
            import tools.jira_tools as _jt
            fn = getattr(_jt, fn_name)
        except AttributeError:
            raise ValueError(f"JiraAdapter: tools.jira_tools has no function '{fn_name}'")

        call_params = self._normalise_params(tool_name, params)

        # Only jira_search_issues supports cursor pagination (nextPageToken).
        if cursor and tool_name == "jira_search_issues":
            call_params["cursor"] = cursor

        result = fn(**call_params)

        # jira_search_issues returns {"issues": [...], "next_cursor": str|None};
        # everything else returns a list, dict, or str.
        if isinstance(result, dict) and "issues" in result and "next_cursor" in result:
            return AdapterPage(
                items=result.get("issues") or [],
                next_cursor=result.get("next_cursor"),
                meta={},
            )

        if is_list:
            items = result if isinstance(result, list) else ([result] if result else [])
            return AdapterPage(items=items, next_cursor=None)

        if isinstance(result, dict):
            return AdapterPage(items=[result] if result else [], next_cursor=None)
        if isinstance(result, str):
            return AdapterPage(items=[{"result": result}], next_cursor=None)
        if result is None:
            return AdapterPage(items=[], next_cursor=None)
        return AdapterPage(items=[{"result": str(result)}], next_cursor=None)

    def _normalise_params(self, tool_name: str, params: dict) -> dict:
        """
        Normalise connector-engine param names to jira_tools param names.

        The connector schema is written for the LLM (project_key, issue_key,
        comment); jira_tools uses the SDLC pipeline's names (project, issue_key,
        comment). Map the differences and strip None so defaults apply cleanly.
        """
        p = {k: v for k, v in params.items() if v is not None}

        # jira_create_issue(project=...) vs schema "project_key"
        if tool_name == "jira_create_issue":
            if "project_key" in p:
                p["project"] = p.pop("project_key")
            # description is REQUIRED by the function but optional in the tool
            # schema (only project_key + summary are required there), so supply
            # a default rather than raising TypeError on a valid model call.
            p.setdefault("description", "")

        # jira_assign_issue(account_id=...) vs schema "account_id" — already aligned,
        # but accept assignee_account_id as a synonym for robustness.
        if tool_name == "jira_assign_issue" and "assignee_account_id" in p:
            p["account_id"] = p.pop("assignee_account_id")

        # Uppercase issue keys — Jira is case-sensitive on some endpoints and the
        # model frequently emits lowercase.
        if "issue_key" in p and isinstance(p["issue_key"], str):
            p["issue_key"] = p["issue_key"].upper()

        # Drop params the target function does not accept, so a hallucinated extra
        # argument returns an empty result instead of a TypeError.
        allowed = self._ALLOWED_PARAMS.get(tool_name)
        if allowed is not None:
            dropped = set(p) - allowed
            if dropped:
                logger.debug(f"JiraAdapter: dropping unsupported params for {tool_name}: {sorted(dropped)}")
            p = {k: v for k, v in p.items() if k in allowed}

        return p

    # Accepted parameters per tool, mirroring the jira_tools function signatures.
    # user_id / user_email are intentionally excluded: credentials arrive through
    # the thread-local, so a caller-supplied identity must never override them.
    _ALLOWED_PARAMS: dict = {
        "jira_get_current_user": set(),
        "jira_search_issues":    {"jql", "fields", "limit", "cursor"},
        "jira_get_issue":        {"issue_key"},
        "jira_list_projects":    {"limit", "search"},
        "jira_get_project":      {"project_key"},
        "jira_get_transitions":  {"issue_key"},
        "jira_list_comments":    {"issue_key", "limit"},
        "jira_count_issues":     {"jql"},
        "jira_create_issue":     {"project", "summary", "description", "priority", "issue_type"},
        "jira_add_comment":      {"issue_key", "comment"},
        "jira_update_issue":     {"issue_key", "status", "comment", "assignee_account_id", "priority"},
        "jira_transition_issue": {"issue_key", "status"},
        "jira_assign_issue":     {"issue_key", "account_id"},
    }


# Module-level singleton — ConnectorEngine._load_custom_adapter() discovers this
# via the AdapterBase instance scan (connector_name + "_adapter" convention).
jira_adapter = JiraAdapter()
