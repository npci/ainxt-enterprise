# SPDX-License-Identifier: MIT
# ============================================================
# Plan shape selection — State → shape (pure, profile-driven)
# ============================================================
#
# docs/architecture/11-planner.md §11.4. Replaces the scattered fast-vs-agentic
# dispatch `if`s in gateway.py with one profile-driven decision table over the
# ConversationState. Pure function → unit-testable; NOT yet wired (adoption is a
# later flag-gated step). Fail-safe: unknown/low-signal → DIRECT (today's fast
# path).
#
# Pure stdlib only (imports the pure cil.state types lazily via duck typing —
# it only reads attributes, so it works with any State-shaped object).
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

# shapes (docs/architecture/11 §11.1)
DIRECT = "direct"                 # fast path, no tools/RAG
RETRIEVE = "retrieve"             # RAG then answer
CLARIFY = "clarify"              # ask instead of guess
TOOL_USE = "tool_use"            # shallow ReAct
DECOMPOSE = "decompose"          # deep ReAct + workflow
VERIFY_HEAVY = "verify_heavy"    # modifier flag (adds verification depth)

# risk → verify budget (matches react_orchestrator _MAX_VERIFY_LOOPS)
_VERIFY_BUDGET = {"high": 5, "medium": 3, "low": 1}


@dataclass(frozen=True)
class ShapeDecision:
    shape: str
    verify_budget: int = 1
    verify_heavy: bool = False
    reason: str = ""


def _score(obj: Any, attr: str) -> float:
    """Read a `.score` from a Score-shaped attribute; 0.0 if absent."""
    v = getattr(obj, attr, None)
    return float(getattr(v, "score", 0.0) or 0.0)


def select_shape(
    state: Any,
    *,
    tool_threshold: float = 0.5,
    retrieval_threshold: float = 0.5,
    risk_level: str = "low",
    clarify_enabled: bool = True,
) -> ShapeDecision:
    """Choose the plan shape from a ConversationState-shaped object.

    Thresholds come from the Domain Profile in a later wiring wave; the defaults
    reproduce a reasonable current-behavior split. Never raises.
    """
    try:
        tool_need = _score(state, "tool_need")
        retrieval_need = _score(state, "retrieval_need")
        complexity = getattr(state, "task_complexity", "medium")
        clarification_needed = bool(getattr(state, "clarification_needed", False))
        is_continuation = bool(getattr(state, "is_continuation", False))
    except Exception:  # noqa: BLE001
        return ShapeDecision(shape=DIRECT, verify_budget=1, reason="fallback")

    verify_heavy = (risk_level == "high")
    budget = _VERIFY_BUDGET.get(risk_level, 1)

    # 1) clarify short-circuits everything (but never mid-task)
    if clarify_enabled and clarification_needed and not is_continuation:
        return ShapeDecision(CLARIFY, budget, verify_heavy, "ambiguous+high-stakes")

    # 2) deep/multi-signal work → decompose
    multi = (tool_need >= tool_threshold and retrieval_need >= retrieval_threshold)
    if complexity in ("deep", "solution") or multi:
        return ShapeDecision(DECOMPOSE, budget, verify_heavy, "deep/multi-signal")

    # 3) single-family tool use
    if tool_need >= tool_threshold:
        return ShapeDecision(TOOL_USE, budget, verify_heavy, "tool_need>=thr")

    # 4) retrieval only
    if retrieval_need >= retrieval_threshold:
        return ShapeDecision(RETRIEVE, budget, verify_heavy, "retrieval_need>=thr")

    # 5) default: direct fast path (today's behavior)
    return ShapeDecision(DIRECT, budget, verify_heavy, "default")
