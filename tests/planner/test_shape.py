# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Wave 4 — plan shape selection (pure)
# ============================================================

from cil.state import ConversationState, Score
from planner.shape import (
    CLARIFY,
    DECOMPOSE,
    DIRECT,
    RETRIEVE,
    TOOL_USE,
    select_shape,
)


def _state(**kw) -> ConversationState:
    st = ConversationState()
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_default_is_direct_fast_path():
    d = select_shape(_state())
    assert d.shape == DIRECT


def test_high_tool_need_is_tool_use():
    d = select_shape(_state(tool_need=Score(score=0.8)))
    assert d.shape == TOOL_USE


def test_high_retrieval_need_is_retrieve():
    d = select_shape(_state(retrieval_need=Score(score=0.7)))
    assert d.shape == RETRIEVE


def test_multi_signal_is_decompose():
    d = select_shape(_state(tool_need=Score(score=0.7), retrieval_need=Score(score=0.7)))
    assert d.shape == DECOMPOSE


def test_deep_complexity_is_decompose():
    d = select_shape(_state(task_complexity="deep"))
    assert d.shape == DECOMPOSE


def test_clarify_short_circuits():
    d = select_shape(_state(clarification_needed=True, tool_need=Score(score=0.9)))
    assert d.shape == CLARIFY


def test_clarify_suppressed_mid_task():
    # never re-ask mid-task: continuation suppresses clarify
    d = select_shape(_state(clarification_needed=True, is_continuation=True,
                            retrieval_need=Score(score=0.7)))
    assert d.shape != CLARIFY


def test_clarify_disabled_by_profile():
    d = select_shape(_state(clarification_needed=True), clarify_enabled=False)
    assert d.shape != CLARIFY


def test_high_risk_sets_verify_heavy_and_budget():
    d = select_shape(_state(tool_need=Score(score=0.9)), risk_level="high")
    assert d.verify_heavy is True
    assert d.verify_budget == 5


def test_never_raises_on_bad_input():
    class Empty:  # not a ConversationState at all
        pass
    d = select_shape(Empty())
    assert d.shape == DIRECT
