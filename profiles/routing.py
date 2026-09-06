# SPDX-License-Identifier: MIT
# ============================================================
# Constraint-filtered, weighted-score model routing (pure)
# ============================================================
#
# docs/architecture/10-model-router.md §10.2. Today's router (models/model_router.py
# route() :929) scores complexity only; privacy is implicit. This is a PURE
# decision function that adds the frontier behavior:
#
#   candidates → reject on hard constraints (privacy floor, window, ceilings)
#              → score survivors (weighted quality - cost - latency)
#              → pick argmax
#
# It is standalone/testable and NOT yet wired into the live router — adoption is
# a later flag+eval-gated step. Fail-safe: choose() returns None when no
# candidate satisfies the constraints, so the caller keeps today's route.
#
# Pure stdlib only.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from profiles.schema import RoutingPolicy

# privacy ordering: higher index = more sensitive / more restricted.
# NOTE: core/rag_acl.py has the authoritative platform ladder (uppercase, +a
# PCI_SENSITIVE tier). At wiring time derive both from that single source; kept
# local + lowercase here to stay pure/importable in a bare test env.
_PRIVACY_ORDER = ["public", "internal", "confidential", "restricted"]


def _privacy_rank(tier: str) -> int:
    try:
        return _PRIVACY_ORDER.index((tier or "public").lower())
    except ValueError:
        return 0


@dataclass(frozen=True)
class ModelCandidate:
    """Routing-relevant model metadata (docs/architecture/10 §10.8 RT1).

    At wiring time, `cost_per_1k` should be derived from the single source of
    truth in core/model_registry.MODEL_COST_PER_1M (reconcile its input/output
    tuple into a blended scalar) rather than hand-fed.
    """

    name: str
    context_window: int = 128000
    # highest sensitivity this model may handle. "restricted" == local-only
    # models that never egress; "public" == cloud models behind the proxy.
    privacy_tier: str = "public"
    quality: float = 0.5           # 0..1 relative capability
    cost_per_1k: float = 0.0       # USD per 1k tokens (0 for local)
    latency_ms: int = 0            # typical TTFT-ish estimate
    supports_vision: bool = False
    supports_tools: bool = True


@dataclass(frozen=True)
class RouteChoice:
    # (named RouteChoice, not RoutingDecision, to avoid colliding with the
    # existing models/model_router.RoutingDecision at integration time)
    model: str
    score: float
    rejected: int          # how many candidates were filtered out
    reason: str = ""


def _satisfies(c: ModelCandidate, *, sensitivity: str, tokens_needed: int,
               need_vision: bool, need_tools: bool, policy: RoutingPolicy) -> bool:
    # PRIVACY (hard): the request's sensitivity must not exceed what the model
    # may handle. For restricted content, this alone forces a restricted-capable
    # (local) model, since any lower-tier candidate is rejected here.
    if _privacy_rank(sensitivity) > _privacy_rank(c.privacy_tier):
        return False
    # PRIVACY FLOOR (hard): the profile can require a minimum handling tier
    # regardless of the request's own sensitivity (e.g. a regulated profile
    # that keeps even 'internal' traffic off public models).
    if _privacy_rank(c.privacy_tier) < _privacy_rank(policy.privacy_floor):
        # only enforce when the floor demands MORE than the model offers AND the
        # request itself is at/above the floor (don't over-restrict public asks
        # to public models). The floor raises the minimum sensitivity handled.
        if _privacy_rank(sensitivity) >= _privacy_rank(policy.privacy_floor):
            return False
    # context window (hard)
    if tokens_needed > c.context_window:
        return False
    # capability (hard)
    if need_vision and not c.supports_vision:
        return False
    if need_tools and not c.supports_tools:
        return False
    return True


def choose(
    candidates: List[ModelCandidate],
    *,
    sensitivity: str = "internal",
    tokens_needed: int = 0,
    need_vision: bool = False,
    need_tools: bool = False,
    policy: Optional[RoutingPolicy] = None,
) -> Optional[RouteChoice]:
    """Pick the highest-scoring model that satisfies all hard constraints.

    Returns None if nothing qualifies → caller falls back to today's route
    (fail-safe). Deterministic: ties broken by candidate order then name.
    """
    pol = policy or RoutingPolicy()
    survivors = [
        c for c in (candidates or [])
        if _satisfies(c, sensitivity=sensitivity, tokens_needed=tokens_needed,
                      need_vision=need_vision, need_tools=need_tools, policy=pol)
    ]
    rejected = len(candidates or []) - len(survivors)
    if not survivors:
        return None

    # soft ceilings: prefer within budget, but don't hard-reject on latency/cost
    # unless the profile set an explicit ceiling.
    def _feasible(c: ModelCandidate) -> bool:
        if pol.max_latency_ms is not None and c.latency_ms > pol.max_latency_ms:
            return False
        if pol.max_cost_per_turn_usd is not None:
            est = c.cost_per_1k * (tokens_needed / 1000.0)
            if est > pol.max_cost_per_turn_usd:
                return False
        return True

    feasible = [c for c in survivors if _feasible(c)] or survivors

    def _score(c: ModelCandidate) -> float:
        cost_est = c.cost_per_1k * (max(tokens_needed, 1) / 1000.0)
        # normalize latency to seconds so weights are comparable
        return (pol.w_quality * c.quality
                - pol.w_cost * cost_est
                - pol.w_latency * (c.latency_ms / 1000.0))

    scored = [(_score(c), -i, c) for i, c in enumerate(feasible)]
    best_score, _, chosen = max(scored)
    return RouteChoice(
        model=chosen.name,
        score=round(best_score, 6),
        rejected=rejected,
        reason=f"privacy<={chosen.privacy_tier};win>={tokens_needed}",
    )
