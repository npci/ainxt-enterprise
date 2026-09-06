# SPDX-License-Identifier: MIT
# ============================================================
# Tier-2 evolution loop — propose → gate → measure → auto-revert
# ============================================================
#
# docs/architecture/21-evolution-engine.md §21.4-21.5. The frontier gap: today's
# self-improvement is Tier-1 (narrow auto) + Tier-3 (HITL). Tier-2 is the
# eval-gated, shadow-tested, AUTO-APPLIED-but-REVERSIBLE middle where most safe
# compounding value lives (router weights, source reweighting, prompt variants).
#
# This generalizes the proven prompt_registry auto-rollback rule
# (core/prompt_registry.py: drop > 0.20 vs control → rollback) into a reusable,
# PURE decision engine any evolvable knob can use. It decides; it does not apply
# — application/persistence is the caller's job (keeps this testable & safe).
#
# Safety property (§21.5, §21.7): a change is kept ONLY if it does not regress
# the guarded metric beyond tolerance; otherwise `should_revert` is True. The
# engine never *improves* on its own — it only gates and signals revert.
#
# Pure stdlib only.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

# generalized from core/prompt_registry._AUTO_ROLLBACK_THRESHOLD
DEFAULT_REGRESSION_TOLERANCE = 0.20


class Verdict(str, Enum):
    PROMOTE = "promote"       # candidate beat/matched baseline within tolerance → keep
    REVERT = "revert"         # candidate regressed beyond tolerance → roll back
    INCONCLUSIVE = "inconclusive"  # not enough signal yet → keep shadowing


class Direction(str, Enum):
    HIGHER_IS_BETTER = "higher"   # e.g. grounding rate, satisfaction
    LOWER_IS_BETTER = "lower"     # e.g. hallucination rate, latency


@dataclass(frozen=True)
class Proposal:
    """A candidate change to an evolvable knob (router weight, source, prompt)."""

    target: str                    # e.g. "router.w_cost" | "source:kb_x"
    change: str                    # human-readable description
    metric: str                    # the guarded KPI, e.g. "grounding_rate"
    direction: Direction = Direction.HIGHER_IS_BETTER


@dataclass
class EvalOutcome:
    baseline: float
    candidate: float
    samples: int = 0


@dataclass
class Tier2Report:
    verdict: Verdict
    regression: float = 0.0        # signed regression fraction vs baseline
    reason: str = ""


def evaluate(
    proposal: Proposal,
    outcome: EvalOutcome,
    *,
    tolerance: float = DEFAULT_REGRESSION_TOLERANCE,
    min_samples: int = 30,
) -> Tier2Report:
    """Gate a proposal by comparing candidate vs baseline on its guarded metric.

    Returns PROMOTE / REVERT / INCONCLUSIVE. Never raises. The comparison is
    direction-aware (higher- vs lower-is-better) and normalized like the
    prompt_registry rule: regression = (worse_delta) / |baseline|.
    """
    if outcome.samples < min_samples:
        return Tier2Report(Verdict.INCONCLUSIVE, 0.0,
                           f"insufficient samples ({outcome.samples}<{min_samples})")

    base = outcome.baseline
    cand = outcome.candidate
    denom = max(abs(base), 1e-3)

    if proposal.direction == Direction.HIGHER_IS_BETTER:
        # regression when candidate is LOWER than baseline
        regression = (base - cand) / denom
    else:
        # lower-is-better: regression when candidate is HIGHER than baseline
        regression = (cand - base) / denom

    if regression > tolerance:
        return Tier2Report(Verdict.REVERT, round(regression, 4),
                           f"regressed {regression:.1%} > tol {tolerance:.0%}")
    return Tier2Report(Verdict.PROMOTE, round(regression, 4),
                       f"within tolerance (reg {regression:.1%})")


@dataclass
class RolloutLadder:
    """Tracks a proposal through offline→shadow→ab→default (§21.5) with the
    guarantee that any stage failing the gate reverts. Pure state machine;
    the caller drives it with real EvalOutcomes per stage."""

    proposal: Proposal
    stages: List[str] = field(default_factory=lambda: ["offline", "shadow", "ab", "default"])
    _idx: int = 0
    history: List[Tier2Report] = field(default_factory=list)

    # Sentinel index meaning "aborted via REVERT" (distinct from completion).
    # No type annotation → dataclass intentionally treats this as a class
    # constant, NOT an __init__ field. Do not add an annotation.
    _REVERTED = -1

    @property
    def current_stage(self) -> Optional[str]:
        if self._idx < 0 or self._idx >= len(self.stages):
            return None
        return self.stages[self._idx]

    @property
    def complete(self) -> bool:
        # completed = advanced past the last stage; NOT the reverted sentinel
        return self._idx == len(self.stages)

    def advance(self, outcome: EvalOutcome, **kw) -> Tier2Report:
        """Evaluate the current stage. PROMOTE → advance; REVERT → stop;
        INCONCLUSIVE → stay. Never raises."""
        report = evaluate(self.proposal, outcome, **kw)
        self.history.append(report)
        if report.verdict == Verdict.PROMOTE:
            self._idx += 1
        elif report.verdict == Verdict.REVERT:
            self._idx = self._REVERTED
        return report

    @property
    def reverted(self) -> bool:
        return self._idx == self._REVERTED
