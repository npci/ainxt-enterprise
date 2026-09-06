# SPDX-License-Identifier: MIT
# ============================================================
# Gap #7 — unified adaptive agent-loop depth policy
# ============================================================
#
# Frontier pattern #7 ("agency = depth × verify × recover"): loop depth adapts
# to task complexity + unresolved verification instead of a flat constant. The
# policy is pure/deterministic; these tests guard the safety invariants —
# especially that it NEVER goes below the historical MAX_ITERATIONS floor and
# NEVER exceeds the absolute cap.
# ============================================================

from agents.loop_policy import (
    DEFAULT_MAX_ITERATIONS,
    REACT_ITERATIONS,
    _ABSOLUTE_MAX_ITERATIONS,
    decide_loop_budget,
    verify_loops_for_risk,
)


def test_floor_never_below_historical_default():
    for c in ("simple", "medium", "complex", "deep", "solution", "???", ""):
        assert decide_loop_budget(c).max_iterations >= DEFAULT_MAX_ITERATIONS


def test_complexity_ladder_is_monotonic():
    simple = decide_loop_budget("simple").max_iterations
    complex_ = decide_loop_budget("complex").max_iterations
    deep = decide_loop_budget("deep").max_iterations
    assert simple <= complex_ <= deep


def test_base_depths():
    assert decide_loop_budget("medium").max_iterations == 3
    assert decide_loop_budget("complex").max_iterations == 5
    assert decide_loop_budget("deep").max_iterations == 7
    assert decide_loop_budget("solution").max_iterations == 8


def test_signals_deepen_but_cap():
    b = decide_loop_budget("solution", verify_unresolved=True, has_tools=True)
    assert b.max_iterations == _ABSOLUTE_MAX_ITERATIONS  # 8+2+1 clamped to 10


def test_never_exceeds_absolute_cap():
    b = decide_loop_budget("deep", verify_unresolved=True, has_tools=True)
    assert b.max_iterations <= _ABSOLUTE_MAX_ITERATIONS


def test_unknown_complexity_defaults_to_floor():
    assert decide_loop_budget("gibberish").max_iterations == DEFAULT_MAX_ITERATIONS


def test_reason_is_populated():
    assert decide_loop_budget("complex").reason


# ── Gap #7 (7/7): unified depth — single source of truth for all engines ─────
def test_verify_loops_by_risk():
    assert verify_loops_for_risk("HIGH") == 5
    assert verify_loops_for_risk("MEDIUM") == 3
    assert verify_loops_for_risk("LOW") == 1
    assert verify_loops_for_risk("low") == 1  # case-insensitive


def test_verify_loops_unknown_defaults_medium():
    assert verify_loops_for_risk("bogus") == 3
    assert verify_loops_for_risk(None) == 3


def test_react_iterations_matches_historical():
    # react_engine's ceiling now comes from here; must match the historical 3.
    assert REACT_ITERATIONS == 3
