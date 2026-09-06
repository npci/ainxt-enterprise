# SPDX-License-Identifier: MIT
# ============================================================
# P6 — context engine age-tiering + segmentation (pure)
# ============================================================

from context.engine import DISTILL, SUMMARIZE, VERBATIM, plan_context


def test_fit_first_all_verbatim_when_under_budget():
    turns = [f"turn {i}" for i in range(10)]
    plan = plan_context(turns, total_tokens=100, usable_budget=1000)
    assert plan.fits_verbatim is True
    assert plan.recent_verbatim == 10
    assert all(t.band == VERBATIM for t in plan.turns)


def test_overflow_age_tiers_by_recency():
    turns = [f"turn {i}" for i in range(100)]
    plan = plan_context(turns, total_tokens=999999, usable_budget=1000,
                        recent_keep=20, mid_keep=40)
    assert plan.fits_verbatim is False
    # newest 20 verbatim, next 40 distilled, oldest 40 summarized
    assert plan.recent_verbatim == 20
    assert plan.mid_distilled == 40
    assert plan.old_summarized == 40
    # newest turn (index 99) is verbatim; oldest (index 0) is summarized
    assert plan.band_of(99) == VERBATIM
    assert plan.band_of(0) == SUMMARIZE


def test_recent_turns_are_sacred():
    turns = [f"t{i}" for i in range(50)]
    plan = plan_context(turns, total_tokens=999999, usable_budget=1, recent_keep=10, mid_keep=10)
    # even under extreme overflow, the newest recent_keep stay verbatim
    verbatim_idx = [t.index for t in plan.turns if t.band == VERBATIM]
    assert verbatim_idx == list(range(40, 50))  # the 10 newest


def test_topic_segmentation_counts_boundaries():
    turns = [
        "tell me about python",
        "and decorators?",
        "new question: how does dns work",
        "what about caching?",
    ]
    plan = plan_context(turns, total_tokens=1, usable_budget=999999)  # fits → verbatim
    assert plan.topics == 2  # one boundary marker → 2 topics
    # turns after the boundary carry the new topic id
    assert plan.turns[2].topic == 1
    assert plan.turns[3].topic == 1


def test_segmentation_disabled():
    turns = ["new question: a", "new topic: b"]
    plan = plan_context(turns, total_tokens=1, usable_budget=999999, segment=False)
    assert plan.topics == 1
    assert all(t.topic == 0 for t in plan.turns)


def test_empty_and_fail_safe():
    assert plan_context([], total_tokens=0, usable_budget=100).turns == []
    # None-ish input must not raise
    p = plan_context(None, total_tokens=0, usable_budget=100)  # type: ignore[arg-type]
    assert p.turns == []
