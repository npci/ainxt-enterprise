# SPDX-License-Identifier: MIT
"""Pytest regression guard for the context-assembly strategies (Phase C0/C1).

Fast, offline, deterministic. Enforces the docs §6 ship gate: no candidate
strategy may have higher staleness / omission / hallucination than the
'current' baseline, across every model window.
"""
from __future__ import annotations

import pytest

from .eval_set import build_cases
from .scorer import run_strategy
from .strategies import STRATEGIES

MODELS = ["local", "claude", "gpt-5", "gemini"]
CANDIDATES = [name for name in STRATEGIES if name != "current"]


@pytest.fixture(scope="module")
def cases():
    return build_cases()


@pytest.mark.parametrize("model", MODELS)
def test_baseline_has_no_staleness(cases, model):
    """The current strategy must never let a stale value win an override probe."""
    base = run_strategy("current", cases, model_hint=model)
    assert base.staleness_rate == 0.0, (
        f"baseline staleness regressed on {model}: {base.staleness_rate}")


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("candidate", CANDIDATES)
def test_candidate_not_worse_than_baseline(cases, model, candidate):
    """Ship gate: candidate <= baseline on all three quality metrics."""
    base = run_strategy("current", cases, model_hint=model)
    cand = run_strategy(candidate, cases, model_hint=model)
    assert cand.staleness_rate <= base.staleness_rate, (
        f"{candidate} staleness {cand.staleness_rate} > baseline "
        f"{base.staleness_rate} on {model}")
    assert cand.omission_rate <= base.omission_rate, (
        f"{candidate} omission {cand.omission_rate} > baseline "
        f"{base.omission_rate} on {model}")
    assert cand.hallucination_risk <= base.hallucination_risk, (
        f"{candidate} hallucination {cand.hallucination_risk} > baseline "
        f"{base.hallucination_risk} on {model}")


def test_probe_kinds_all_exercised(cases):
    """Guard against the eval set silently losing coverage of a probe kind."""
    kinds = {p.kind for c in cases for p in c.probes}
    assert {"recall", "override", "distractor", "longctx"} <= kinds
