# SPDX-License-Identifier: MIT
# ============================================================
# Phase 3 — dispatch fork decision (pure)
# ============================================================

from cil.state import ConversationState, Score
from pipeline.dispatch import (
    FORK_GENERAL,
    FORK_ORCHESTRATOR,
    DispatchDecision,
    Lane,
    decide_fork,
    shape_of,
)


def _state(**kw):
    st = ConversationState()
    for k, v in kw.items():
        setattr(st, k, v)
    return st


# ── the hard safety invariant: repo/project ALWAYS orchestrator ─────────────

def test_repo_filter_always_orchestrator():
    # even a trivial-looking turn must go orchestrator when a repo is set (today's rule)
    assert decide_fork(_state(), repo_filter="myrepo") == FORK_ORCHESTRATOR


def test_project_id_always_orchestrator():
    assert decide_fork(_state(tool_need=Score(score=0.0)), project_id="proj1") == FORK_ORCHESTRATOR


def test_repo_wins_even_if_shape_says_direct():
    # a turn the CIL would call DIRECT still goes orchestrator if repo is set
    st = _state(task_complexity="simple")
    assert decide_fork(st, repo_filter="r") == FORK_ORCHESTRATOR


# ── no-repo/no-project: today = fast path, unless genuinely agentic ─────────

def test_trivial_no_repo_is_general():
    assert decide_fork(_state()) == FORK_GENERAL


def test_high_tool_need_promotes_to_orchestrator():
    st = _state(tool_need=Score(score=0.8))
    assert decide_fork(st) == FORK_ORCHESTRATOR


def test_deep_complexity_promotes_to_orchestrator():
    assert decide_fork(_state(task_complexity="deep")) == FORK_ORCHESTRATOR


def test_plain_question_stays_general():
    # retrieval-only turns stay on the fast path (RAG runs inside _general_stream)
    st = _state(retrieval_need=Score(score=0.7))
    assert decide_fork(st) == FORK_GENERAL


# ── fail-safe ────────────────────────────────────────────────────────────────

def test_never_raises_falls_back_to_todays_rule():
    class Bad:
        @property
        def tool_need(self):
            raise RuntimeError("boom")
    # no repo/project + error → today's rule = general
    assert decide_fork(Bad()) == FORK_GENERAL
    # repo set + error → today's rule = orchestrator
    assert decide_fork(Bad(), repo_filter="r") == FORK_ORCHESTRATOR


def test_shape_of_returns_string_or_none():
    s = shape_of(_state(tool_need=Score(score=0.8)))
    assert s is None or isinstance(s, str)


def test_dispatch_decision_dataclass():
    d = DispatchDecision(lane=Lane.GENERAL, reason="fast", shape="direct")
    assert d.lane == "general"  # str-Enum
    assert d.shape == "direct"
