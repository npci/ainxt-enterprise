# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Dispatch decision — the fast-vs-agentic fork, made explicit
# ============================================================
#
# docs/architecture/03-request-lifecycle.md §3.3 (L2). Today gateway.py dispatch
# is ~18 side-effecting early-return lanes with the primary fork implicit at
# `if not repo_filter and not q.project_id:` (line 4072). This module makes the
# lane + fork EXPLICIT so it can be recorded (shadow) and eventually driven,
# WITHOUT rewriting the side-effecting lanes.
#
# Pure stdlib + pure planner.shape import. Never raises. Importable in a bare env.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from planner.shape import DECOMPOSE, DIRECT, TOOL_USE, VERIFY_HEAVY, select_shape


class Lane(str, Enum):
    """The terminal lane a request takes through ask_ai (dispatch map §1)."""

    KILL_SWITCH = "kill_switch"       # 503 platform disabled
    CTX_ISOLATION = "ctx_isolation"   # 400 rag=off + repo/project
    AUTH = "auth"                     # 401
    DOC_ROUTE = "doc_route"           # {route:"doc"} enqueue
    BUDGET = "budget"                 # 402/429
    SAFETY_BLOCK = "safety_block"     # nemo/compliance/hardblock
    CACHE = "cache"                   # exact or semantic cache hit
    DISAMBIG = "disambig"             # KB clarification
    CLARIFY = "clarify"               # CIL ambiguity clarification
    DOC_EDIT = "doc_edit"
    DOC_GEN = "doc_gen"
    GENERAL = "general"               # _general_stream (fast path)
    INTENT = "intent"                 # skill/agent intent route
    CLI = "cli"                       # _cli_direct_stream
    ORCHESTRATOR = "orchestrator"     # response_stream (agentic)


# fork return values (match the two terminal generators)
FORK_GENERAL = "general"
FORK_ORCHESTRATOR = "orchestrator"


@dataclass
class DispatchDecision:
    lane: Lane
    reason: str = ""
    shape: Optional[str] = None       # planner.shape result, when computed


def decide_fork(
    state: Any,
    *,
    repo_filter: Any = None,
    project_id: Any = None,
    clarify_enabled: bool = True,
) -> str:
    """Decide the general-vs-orchestrator fork. Never raises.

    HARD SAFETY RULE (preserves today's behavior): a request with repo_filter or
    project_id ALWAYS goes to the orchestrator — repo/project context requires
    the agentic path. This function may only add orchestration for genuinely
    agentic *no-repo/no-project* turns (per the CIL shape); it can never divert a
    repo/project request, and it defaults to today's rule on any error.
    """
    try:
        # today's invariant: repo/project → orchestrator, unconditionally
        if repo_filter or project_id:
            return FORK_ORCHESTRATOR

        # no repo/project: today this is always the fast path. The CIL shape may
        # promote a genuinely agentic turn (tools / deep decompose) to the
        # orchestrator; everything else stays on the fast path (today's behavior).
        decision = select_shape(state, clarify_enabled=clarify_enabled)
        if decision.shape in (TOOL_USE, DECOMPOSE):
            return FORK_ORCHESTRATOR
        return FORK_GENERAL
    except Exception:  # noqa: BLE001 — fall back to today's rule
        return FORK_ORCHESTRATOR if (repo_filter or project_id) else FORK_GENERAL


def shape_of(state: Any, *, clarify_enabled: bool = True) -> Optional[str]:
    """Expose the raw plan shape for telemetry/recording. Never raises."""
    try:
        return select_shape(state, clarify_enabled=clarify_enabled).shape
    except Exception:  # noqa: BLE001
        return None
