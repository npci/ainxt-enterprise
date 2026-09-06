# SPDX-License-Identifier: MIT
"""
Multi-repo run-diff MCP tool.

Phase 4a tool that lets the coding agent inspect what other repos in the
current SDLC run have already been modified. Read-only: no side effects on
the workspace.

Why this exists
---------------
When the state machine adds the per-editable-repo coder loop in Phase 4b,
the coder for repo R needs to know what changes were already made in
upstream deps so it can write code that consumes the new interfaces. A
retrieval call only returns indexed code (pre-run state). `get_run_diff`
returns the *in-progress* diff in a sibling repo's checkout — the
ground-truth latest state inside this run.

Until Phase 4b ships, the tool returns useful output for the primary repo
too (whatever's been edited in the run workspace) and a clear empty-state
for repos that have no checkout yet.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

from core.logger import logger


# ── Public callable for the MCP registry ─────────────────────────────────────

def get_run_diff(run_id: str, repo: Optional[str] = None, max_chars: int = 8000) -> str:
    """
    Return the git diff for a repo's workspace within a given SDLC run.

    Args:
        run_id:     The SDLC run ID. Required.
        repo:       Gitlab namespace/project of the target repo. When omitted,
                    defaults to the run's primary repo.
        max_chars:  Cap the returned diff length to avoid blowing up context
                    when a repo has large unstaged changes. Default 8000.

    Returns:
        A string. Either the diff text (possibly truncated with a clear
        marker) or an explanatory message ("no checkout found", "no changes",
        etc.). Never raises.
    """
    if not run_id:
        return "[Error: run_id is required]"

    try:
        from store.sdlc_store import list_run_repos
    except Exception as exc:
        return f"[Error: sdlc_store unavailable: {exc}]"

    try:
        rows = list_run_repos(run_id) or []
    except Exception as exc:
        return f"[Error: list_run_repos({run_id}) failed: {exc}]"

    if not rows:
        return f"No sdlc_run_repos rows for run {run_id}. " \
               f"Either the multi-repo flag is off or preflight did not populate the table."

    target = (repo or "").strip()
    if not target:
        primary = next((r for r in rows if r.get("kind") == "primary"), None)
        if not primary:
            return f"No primary repo recorded for run {run_id}."
        target = primary.get("repo", "")

    row = next((r for r in rows if r.get("repo") == target), None)
    if row is None:
        known = ", ".join(r.get("repo", "") for r in rows)
        return f"Repo {target!r} not in run {run_id}. Known repos: {known}"

    workspace = row.get("workspace_path") or ""
    if not workspace or not os.path.isdir(workspace):
        return f"No workspace checkout for {target!r} in run {run_id} " \
               f"(workspace_path={workspace!r}). Coding has not started for this repo yet."

    diff = _git_diff(workspace)
    if not diff.strip():
        return f"No changes in {target!r} workspace yet."

    if len(diff) > max_chars:
        head = diff[: max_chars // 2]
        tail = diff[-max_chars // 2 :]
        omitted = len(diff) - max_chars
        return (
            f"{head}\n"
            f"...\n"
            f"[diff truncated — {omitted} chars omitted from the middle for context budget]\n"
            f"...\n"
            f"{tail}"
        )
    return diff


def _git_diff(workspace: str) -> str:
    """
    Run `git diff HEAD` against the workspace and return the output.

    Captures both staged and unstaged changes (`HEAD` baseline). Errors are
    returned as `[Error ...]` strings so the MCP caller can decide whether to
    retry or move on.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", workspace, "diff", "HEAD", "--no-color"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return f"[Error: git diff exit={proc.returncode}: {(proc.stderr or '').strip()}]"
        return proc.stdout or ""
    except subprocess.TimeoutExpired:
        return "[Error: git diff timed out]"
    except FileNotFoundError:
        return "[Error: git binary not found on runtime host]"
    except Exception as exc:
        return f"[Error: {exc}]"


# ── MCP registration ─────────────────────────────────────────────────────────

def _register_run_diff_tool() -> None:
    """Register the get_run_diff tool with the platform registry on module load."""
    try:
        from mcp.tool_registry import tool_registry, ToolDefinition
        tool_registry.register(ToolDefinition(
            name="get_run_diff",
            description=(
                "Read the current git diff (vs HEAD) from the workspace of a "
                "specific repo within an in-progress SDLC run. Use when "
                "coding a downstream repo that consumes interfaces just edited "
                "in an upstream dep — `get_run_diff(repo='ainxt/payments-sdk')` "
                "returns the as-of-now changes in that upstream so the "
                "downstream coder can target the new shape rather than the "
                "stale indexed version."
            ),
            fn=get_run_diff,
            tags=["sdlc", "multi-repo", "diff", "read-only"],
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "SDLC run identifier (UUID). Required.",
                    },
                    "repo": {
                        "type": "string",
                        "description": (
                            "Gitlab namespace/project path of the target repo "
                            "(e.g. 'ainxt/payments-sdk'). Defaults to the run's "
                            "primary repo when omitted."
                        ),
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Cap on returned diff length. Default 8000.",
                        "default": 8000,
                    },
                },
                "required": ["run_id"],
            },
            author="platform",
        ))
    except Exception as exc:
        logger.warning(f"run_diff_tools: registration skipped ({exc})")


_register_run_diff_tool()
