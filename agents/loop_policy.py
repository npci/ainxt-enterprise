# SPDX-License-Identifier: MIT
# ============================================================
# Unified agent-loop control policy (adaptive depth)
# ============================================================
#
# docs/architecture/02 §2.7/§2.8 (pattern #7: "agency = depth × verify ×
# recover") and 12-tool-orchestration.md. Historically loop depth was a single
# fixed constant (orchestrator.MAX_ITERATIONS = 3) and verification/recovery
# depth lived in separate overlapping modules (react_orchestrator, react_engine,
# advanced_reasoning). This module is the ONE place that decides how deep the
# agent loop should go, derived from task complexity + whether the last pass
# left verification unresolved — so the depth ADAPTS instead of being flat.
#
# It does NOT rip out the other loop modules (that would be a high-risk runtime
# rewrite). It provides a single policy the main orchestrator loop consults; the
# other engines can adopt it incrementally. Pure stdlib, never raises: on any
# doubt it returns the historical default so behavior can only stay the same or
# deepen — never regress below today's floor.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass

# Historical fixed ceiling (orchestrator.MAX_ITERATIONS). The adaptive policy
# may RAISE this for genuinely complex/unresolved work, never lower it.
DEFAULT_MAX_ITERATIONS = 3

# Complexity → base iteration ceiling. Simple work needs one pass; deep/solution
# work is allowed more agentic depth (try → check → recover → retry).
_COMPLEXITY_DEPTH = {
    "simple":   1,
    "medium":   3,
    "complex":  5,
    "deep":     7,
    "solution": 8,
}

# Hard upper bound so an adaptive loop can never run away (cost/latency guard).
_ABSOLUTE_MAX_ITERATIONS = 10

# Verification-loop ceiling by risk tier. This is the SINGLE source of truth for
# how many verify→recover cycles the react_orchestrator runs (previously a
# hard-coded dict inside that module). Kept here so agentic depth is unified.
_VERIFY_LOOPS_BY_RISK = {"HIGH": 5, "MEDIUM": 3, "LOW": 1}

# ReAct micro-loop ceiling (react_engine). Also sourced here so no engine keeps
# its own private constant.
REACT_ITERATIONS = 3


def verify_loops_for_risk(risk: str) -> int:
    """Verify+recover loop ceiling for a risk tier (HIGH/MEDIUM/LOW). Unknown →
    MEDIUM. Never raises. Single source of truth for react_orchestrator."""
    try:
        return _VERIFY_LOOPS_BY_RISK.get((risk or "MEDIUM").strip().upper(), 3)
    except Exception:  # noqa: BLE001
        return 3


@dataclass(frozen=True)
class LoopBudget:
    max_iterations: int
    reason: str = ""


def decide_loop_budget(
    complexity: str = "medium",
    *,
    verify_unresolved: bool = False,
    has_tools: bool = False,
) -> LoopBudget:
    """Return the iteration ceiling for this turn. Never raises.

    - complexity: the CIL/classifier label (simple|medium|complex|deep|solution).
    - verify_unresolved: True when a prior pass produced claims that failed
      verification and could benefit from another try→check→recover cycle.
    - has_tools: True when the plan uses tools (agentic turns get a bit more
      room to recover from a failed tool call).

    The result is clamped to [DEFAULT_MAX_ITERATIONS, _ABSOLUTE_MAX_ITERATIONS]
    so it can only match or exceed today's fixed behavior, never regress.
    """
    try:
        base = _COMPLEXITY_DEPTH.get((complexity or "medium").strip().lower(),
                                     DEFAULT_MAX_ITERATIONS)
        depth = base
        reason_bits = [f"complexity={complexity}:{base}"]
        if verify_unresolved:
            depth += 2
            reason_bits.append("verify_unresolved:+2")
        if has_tools:
            depth += 1
            reason_bits.append("has_tools:+1")
        # Floor at the historical default (never regress), cap at the absolute max.
        depth = max(DEFAULT_MAX_ITERATIONS, min(_ABSOLUTE_MAX_ITERATIONS, depth))
        return LoopBudget(max_iterations=depth, reason=";".join(reason_bits))
    except Exception:  # noqa: BLE001 — control policy must never break the agent
        return LoopBudget(max_iterations=DEFAULT_MAX_ITERATIONS, reason="fallback")
