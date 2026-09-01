# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Phase 7 — frontier-pattern scorecard (honest, inspectable)
# ============================================================
#
# Guards that the scorecard stays truthful and self-consistent. It does NOT
# assert "all patterns are full" — an honest scorecard is allowed (expected) to
# carry ◐ partials with named gaps. It asserts structural integrity + that we
# improved over the design-time §2.9 baseline (2 full / 5 partial).
# ============================================================

from evals.frontier_scorecard import (
    FULL,
    GAP,
    PARTIAL,
    SCORECARD,
    ENTERPRISE_INVARIANTS,
    render,
    summary,
)


def test_seven_patterns_present():
    assert len(SCORECARD) == 7
    assert [p.n for p in SCORECARD] == [1, 2, 3, 4, 5, 6, 7]


def test_status_values_valid():
    for p in SCORECARD:
        assert p.status in (FULL, PARTIAL, GAP)


def test_partial_patterns_name_a_gap():
    # honesty invariant: anything not FULL must state its remaining gap
    for p in SCORECARD:
        if p.status != FULL:
            assert p.gap, f"pattern {p.n} is {p.status} but names no gap"


def test_full_patterns_have_no_gap():
    for p in SCORECARD:
        if p.status == FULL:
            assert p.gap == ""


def test_every_pattern_has_wired_evidence():
    for p in SCORECARD:
        assert p.wired.strip(), f"pattern {p.n} states nothing wired"


def test_summary_counts_consistent():
    s = summary()
    assert s["total"] == 7
    assert s["full"] + s["partial"] + s["gap"] == 7


def test_improved_over_design_baseline():
    # §2.9 baseline was 2 full / 5 partial. Post-PIPELINE_V2 must be no worse
    # on 'full' and must have closed all hard gaps.
    s = summary()
    assert s["full"] >= 2
    assert s["gap"] == 0, "no pattern should be a hard gap after Phases 1-6"


def test_enterprise_invariants_full():
    # privacy floor + resilient doc-gen are the enterprise adds; both must be full
    assert len(ENTERPRISE_INVARIANTS) == 2
    for p in ENTERPRISE_INVARIANTS:
        assert p.status == FULL


def test_render_is_honest_markdown():
    md = render()
    assert "| # | Pattern | Status |" in md
    # the honest disclaimer MUST be present — no literal-model-parity claim
    assert "NOT literal frontier-model parity" in md
