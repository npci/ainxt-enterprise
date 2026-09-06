# SPDX-License-Identifier: MIT
"""
Connection-test probe selection — shared by the API and the diagnostics script.

WHY THIS MODULE EXISTS
    `GET /connectors/{name}/test` needs to call one cheap, always-valid tool to
    prove a connector actually works. It used to hardcode `tools[0]`, which is
    fragile in two ways:

      1. `tools` is a JSONB array, so "first" means *insertion order*. A prod
         catch-up SQL wrote GitLab's array with `gitlab_list_issues` first — a
         tool that requires `project_id` — so the test died in schema validation
         with `Required parameter missing: 'project_id'` before any HTTP request
         was made. The same latent trap exists for Microsoft 365, whose third
         tool requires `team_id` + `channel_id`.
      2. It forced per-connector band-aids in the router (a `membership` hint for
         GitLab, a `jql` hint for Jira) that silently rot when the order changes.

    Selecting by *capability* instead — the first non-write tool that declares no
    required parameters — is correct under any ordering and any tool count.

DELIBERATELY DEPENDENCY-FREE
    Imports nothing outside the standard library, so this logic can be exercised
    (in a REPL, a test, or an ad-hoc script) without pulling in httpx / structlog
    / the DB layer.

VERIFYING A CONNECTOR AFTER A DEFINITION CHANGE
    Confirm the selected probe needs no arguments — the array order in the DB is
    what matters, not the order in connectors/seed.py:

        SELECT tools->0->>'name', tools->0->'input_schema'->'required'
        FROM ainxt.connector_definitions WHERE name = 'gitlab';
        -- expect: gitlab_list_my_mrs , []
"""
from __future__ import annotations

from typing import Any, Optional

# Params supplied for a probe tool that cannot avoid a required argument.
#
# Keyed by TOOL name, not connector name: a connector's tool order can change
# (that is the bug this module exists to fix), and an entry here becomes inert
# the moment a genuinely parameterless tool is added to that connector.
#
# Jira was the motivating case: every jira_* tool used to declare required params,
# so there was no parameterless candidate and `jira_search_issues` needed a minimal
# always-valid JQL. That is no longer true — `jira_get_current_user` (GET /myself)
# takes no arguments, so Jira now resolves via STRATEGY_PARAMETERLESS and the entry
# below is inert, exactly as the docstring above predicts. It is kept as a safety
# net in case that tool is ever removed. GitLab needs no entry either:
# `gitlab_list_my_mrs` requires nothing.
PROBE_PARAM_HINTS: dict[str, dict[str, Any]] = {
    # Cheapest valid JQL: ordered, unfiltered, capped to 1 row by `limit` below.
    "jira_search_issues": {"jql": "ORDER BY updated DESC"},
}

# Strategy labels reported back to the caller (surfaced in the API response and
# printed by the diagnostics script) so it is obvious WHY a tool was chosen.
STRATEGY_PARAMETERLESS = "parameterless"   # ideal: schema needs nothing
STRATEGY_HINTED = "hinted-fallback"        # no safe tool; used PROBE_PARAM_HINTS
STRATEGY_UNSAFE = "unsafe-fallback"        # no safe tool and no hint — will fail


def _required_params(tool: dict) -> list[str]:
    """Required parameter names for a tool definition, defensively."""
    schema = tool.get("input_schema") or {}
    required = schema.get("required") or []
    return [r for r in required if isinstance(r, str)]


def is_safe_probe(tool: dict) -> bool:
    """True if this tool can be called with no arguments and changes nothing.

    Write tools are excluded even when they declare no required params: a
    connection test must never create an issue, send a message, or merge an MR.
    """
    return not tool.get("is_write", False) and not _required_params(tool)


def select_probe(tools: list[dict]) -> Optional[dict]:
    """Choose the tool to use for a connection test.

    Returns None when `tools` is empty, otherwise a dict:

        {
          "tool":     str,   # tool name to execute
          "params":   dict,  # params to send (always includes limit=1)
          "strategy": str,   # STRATEGY_* — why this tool was chosen
          "required": list,  # the tool's declared required params
        }

    Selection order:
      1. First non-write tool with no required params  -> STRATEGY_PARAMETERLESS
      2. Else the first tool whose required params are fully covered by
         PROBE_PARAM_HINTS                             -> STRATEGY_HINTED
      3. Else `tools[0]` with whatever hint exists     -> STRATEGY_UNSAFE
         (preserves the old behaviour rather than refusing to test at all, and
         the strategy label makes the expected failure diagnosable)
    """
    if not tools:
        return None

    def build(tool: dict, strategy: str) -> dict:
        # limit=1 keeps the probe cheap. The engine strips keys a tool's schema
        # does not declare, so passing it unconditionally is harmless.
        params: dict[str, Any] = {"limit": 1}
        params.update(PROBE_PARAM_HINTS.get(tool.get("name", ""), {}))
        return {
            "tool": tool.get("name", ""),
            "params": params,
            "strategy": strategy,
            "required": _required_params(tool),
        }

    # 1. A tool that needs nothing — the common, ideal case.
    for tool in tools:
        if is_safe_probe(tool):
            return build(tool, STRATEGY_PARAMETERLESS)

    # 2. No parameterless tool (Jira). Accept one whose required params we can
    #    fully supply from the hint table, preferring reads over writes.
    for want_read_only in (True, False):
        for tool in tools:
            if want_read_only and tool.get("is_write", False):
                continue
            hint = PROBE_PARAM_HINTS.get(tool.get("name", ""), {})
            if hint and not (set(_required_params(tool)) - set(hint)):
                return build(tool, STRATEGY_HINTED)

    # 3. Nothing safe. Fall back to the historical behaviour so the test still
    #    reports a real error from the provider rather than silently doing nothing.
    return build(tools[0], STRATEGY_UNSAFE)
