# SPDX-License-Identifier: MIT
# ============================================================
# Wave 5 — Tier-2 evolution loop (pure)
# ============================================================

from evolution.tier2 import (
    Direction,
    EvalOutcome,
    Proposal,
    RolloutLadder,
    Verdict,
    evaluate,
)


def _prop(direction=Direction.HIGHER_IS_BETTER):
    return Proposal(target="router.w_cost", change="raise cost weight",
                    metric="grounding_rate", direction=direction)


def test_insufficient_samples_is_inconclusive():
    r = evaluate(_prop(), EvalOutcome(baseline=0.8, candidate=0.9, samples=5), min_samples=30)
    assert r.verdict == Verdict.INCONCLUSIVE


def test_improvement_promotes_higher_is_better():
    r = evaluate(_prop(), EvalOutcome(baseline=0.80, candidate=0.85, samples=100))
    assert r.verdict == Verdict.PROMOTE


def test_regression_beyond_tolerance_reverts():
    # higher-is-better metric drops 25% > 20% tol → revert
    r = evaluate(_prop(), EvalOutcome(baseline=0.80, candidate=0.60, samples=100))
    assert r.verdict == Verdict.REVERT
    assert r.regression > 0.20


def test_small_regression_within_tolerance_promotes():
    # 5% drop < 20% tolerance → keep (matches prompt_registry rule)
    r = evaluate(_prop(), EvalOutcome(baseline=0.80, candidate=0.76, samples=100))
    assert r.verdict == Verdict.PROMOTE


def test_lower_is_better_direction():
    # hallucination rate: candidate HIGHER than baseline = regression
    p = _prop(Direction.LOWER_IS_BETTER)
    worse = evaluate(p, EvalOutcome(baseline=0.10, candidate=0.30, samples=100))
    assert worse.verdict == Verdict.REVERT
    better = evaluate(p, EvalOutcome(baseline=0.10, candidate=0.05, samples=100))
    assert better.verdict == Verdict.PROMOTE


def test_rollout_ladder_advances_on_promote():
    ladder = RolloutLadder(proposal=_prop())
    assert ladder.current_stage == "offline"
    ladder.advance(EvalOutcome(baseline=0.8, candidate=0.85, samples=100))
    assert ladder.current_stage == "shadow"
    ladder.advance(EvalOutcome(baseline=0.8, candidate=0.82, samples=100))
    assert ladder.current_stage == "ab"


def test_rollout_ladder_reverts_and_stops():
    ladder = RolloutLadder(proposal=_prop())
    ladder.advance(EvalOutcome(baseline=0.8, candidate=0.85, samples=100))  # offline ok
    ladder.advance(EvalOutcome(baseline=0.8, candidate=0.50, samples=100))  # shadow regress
    assert ladder.reverted is True
    assert not ladder.complete


def test_rollout_ladder_completes():
    ladder = RolloutLadder(proposal=_prop())
    for _ in range(len(ladder.stages)):
        ladder.advance(EvalOutcome(baseline=0.8, candidate=0.85, samples=100))
    assert ladder.complete is True
    assert ladder.reverted is False


def test_inconclusive_stays_on_stage():
    ladder = RolloutLadder(proposal=_prop())
    ladder.advance(EvalOutcome(baseline=0.8, candidate=0.9, samples=5))  # too few samples
    assert ladder.current_stage == "offline"  # did not advance
