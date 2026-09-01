# SPDX-License-Identifier: Apache-2.0
"""Scoring: turn assembled-context decisions into the docs §6 metrics.

A probe is evaluated ONLY against the assembled context text (no LLM). The
premise: if the fact required to answer correctly is present, a competent model
can answer; if it is absent, the model must either refuse or hallucinate. This
makes context-omission (source B) directly measurable and deterministic.

Metrics (all lower = better), reported per strategy:
  - omission_rate      : fraction of recall/distractor/longctx probes whose
                         answer_fact is MISSING from context.
  - staleness_rate     : fraction of override probes where the STALE value is
                         present but the FRESH value is not (i.e. the context
                         would push the model to the outdated answer).
  - hallucination_risk : fraction of probes where NEITHER fresh nor stale fact
                         is present — the model has no grounding and must invent.
  - avg_tokens         : mean approx tokens sent across probes (cost proxy).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List

from .model import AssembledContext, Case, Probe
from .strategies import STRATEGIES


def _present(fact: str, ctx_text: str) -> bool:
    if not fact:
        return False
    return fact.lower() in ctx_text.lower()


@dataclass
class ProbeResult:
    case: str
    kind: str
    question: str
    fresh_present: bool
    stale_present: bool
    # Derived flags (mutually informative, not mutually exclusive across kinds).
    omitted: bool          # required fresh fact missing (recall-family)
    stale_win: bool        # override: stale present, fresh absent
    ungrounded: bool       # neither fact present -> pure hallucination surface


def score_probe(probe: Probe, ctx: AssembledContext) -> ProbeResult:
    text = ctx.flat_text()
    fresh = _present(probe.answer_fact, text)
    stale = _present(probe.stale_fact, text) if probe.stale_fact else False

    if probe.kind == "override":
        omitted = False
        stale_win = stale and not fresh
        ungrounded = (not fresh) and (not stale)
    else:  # recall / distractor / longctx
        omitted = not fresh
        stale_win = False
        ungrounded = not fresh and not stale
    return ProbeResult(
        case="", kind=probe.kind, question=probe.question,
        fresh_present=fresh, stale_present=stale,
        omitted=omitted, stale_win=stale_win, ungrounded=ungrounded,
    )


@dataclass
class StrategyReport:
    strategy: str
    n_probes: int
    omission_rate: float
    staleness_rate: float
    hallucination_risk: float
    avg_tokens: float
    results: List[ProbeResult]


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def run_strategy(strategy_name: str, cases: List[Case],
                 model_hint: str = "claude") -> StrategyReport:
    strat: Callable = STRATEGIES[strategy_name]
    results: List[ProbeResult] = []
    tokens_sum = 0
    n_override = 0
    n_recall_family = 0
    n_stale_win = 0
    n_omitted = 0
    n_ungrounded = 0

    for case in cases:
        for probe in case.probes:
            ctx = strat(case, model_hint)
            r = score_probe(probe, ctx)
            r.case = case.name
            results.append(r)
            tokens_sum += ctx.approx_tokens
            if probe.kind == "override":
                n_override += 1
                if r.stale_win:
                    n_stale_win += 1
            else:
                n_recall_family += 1
                if r.omitted:
                    n_omitted += 1
            if r.ungrounded:
                n_ungrounded += 1

    n = len(results)
    return StrategyReport(
        strategy=strategy_name,
        n_probes=n,
        omission_rate=_rate(n_omitted, n_recall_family),
        staleness_rate=_rate(n_stale_win, n_override),
        hallucination_risk=_rate(n_ungrounded, n),
        avg_tokens=round(tokens_sum / n, 1) if n else 0.0,
        results=results,
    )
