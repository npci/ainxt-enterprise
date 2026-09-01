# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Model router policy — constraint filter + weighted score (pure)
# ============================================================
#
# docs/architecture/10-model-router.md §10.2-10.7 (RT1-RT4). Turns model choice
# from "complexity-only tier lookup" into "pick the CHEAPEST SUFFICIENT model that
# satisfies the constraints". Two parts:
#
#   1. ModelSpec — the capability/constraint metadata the registry should carry
#      (window, privacy_tier, cost, latency, supports_tools/vision).  [RT1]
#   2. route() — hard-filter unsafe/incapable candidates, then weighted-score the
#      survivors; weights come from the Domain Profile.                [RT2]
#      Privacy is a HARD constraint (restricted → local-only).         [RT3]
#      Budget nearing cap biases toward cheaper/local models.          [RT4]
#
# Pure stdlib. NOT wired into models/model_router.py yet (flag-gated adoption).
# Fail-safe: on any error or empty candidate set, returns the caller's
# `default_model` — i.e. today's priority-order choice — so routing never breaks.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# privacy tiers, ordered (higher index = more private / more restricted-capable)
_PRIVACY_ORDER = ["public", "internal", "confidential", "restricted"]


def _priv_rank(tier: str) -> int:
    return _PRIVACY_ORDER.index(tier) if tier in _PRIVACY_ORDER else 0


@dataclass
class ModelSpec:
    """Registry metadata for one model (docs/architecture/10 §10.2, RT1)."""

    name: str
    tier: str = "medium"              # simple|medium|complex|deep|solution|vision
    context_window: int = 8000
    privacy_tier: str = "public"      # highest sensitivity this model may handle
    cost_per_1k: float = 0.0          # USD per 1k tokens (0 = local/free)
    latency_ms: int = 500
    quality: float = 0.5              # 0..1 relative answer quality
    supports_tools: bool = False
    supports_vision: bool = False
    is_local: bool = False

    def can_handle_privacy(self, sensitivity: str) -> bool:
        """A model may handle content only if its privacy_tier is at least as
        restrictive-capable as the content's sensitivity. `restricted` content
        requires a local model (§10.3)."""
        if sensitivity == "restricted":
            return self.is_local
        return _priv_rank(self.privacy_tier) >= _priv_rank(sensitivity)


@dataclass
class RouteRequest:
    """The routing inputs assembled from CIL + context + profile."""

    sensitivity: str = "internal"
    tokens_needed: int = 0
    needs_tools: bool = False
    needs_vision: bool = False
    complexity: str = "medium"


@dataclass
class RouteWeights:
    """Domain-Profile-supplied scoring weights (§10.2). Defaults ≈ quality-first,
    reproducing today's complexity-led behavior when cost/latency ≈ 0."""

    quality: float = 1.0
    cost: float = 0.0
    latency: float = 0.0


@dataclass
class RouteResult:
    model: str
    reason: str = ""
    rejected: Dict[str, str] = field(default_factory=dict)  # name -> why
    score: float = 0.0


def _quality_for(spec: ModelSpec, complexity: str) -> float:
    """Reward matching the required tier; penalize under-powered models for hard
    turns so a 'simple' model isn't chosen for a 'deep' task purely on cost."""
    order = ["simple", "medium", "complex", "deep", "solution"]
    need = order.index(complexity) if complexity in order else 1
    have = order.index(spec.tier) if spec.tier in order else 1
    if have < need:
        return spec.quality * 0.4      # under-powered: heavy penalty
    return spec.quality


def route(
    request: RouteRequest,
    candidates: List[ModelSpec],
    *,
    weights: Optional[RouteWeights] = None,
    budget_remaining: Optional[float] = None,
    budget_cap: Optional[float] = None,
    default_model: str = "",
) -> RouteResult:
    """Choose the cheapest sufficient model. Hard-filters candidates that fail
    size/privacy/capability constraints, then argmax of a weighted score. Never
    raises: returns `default_model` if nothing qualifies or on error."""
    w = weights or RouteWeights()
    rejected: Dict[str, str] = {}
    try:
        # budget pressure: fraction of budget consumed (0..1); nearing cap → bias local
        pressure = 0.0
        if budget_cap and budget_cap > 0 and budget_remaining is not None:
            pressure = max(0.0, min(1.0, 1.0 - (budget_remaining / budget_cap)))

        viable: List[tuple] = []
        for m in candidates or []:
            if request.tokens_needed and m.context_window < request.tokens_needed:
                rejected[m.name] = "context_window too small"
                continue
            if not m.can_handle_privacy(request.sensitivity):
                rejected[m.name] = f"privacy: cannot handle {request.sensitivity}"
                continue
            if request.needs_tools and not m.supports_tools:
                rejected[m.name] = "no tool support"
                continue
            if request.needs_vision and not m.supports_vision:
                rejected[m.name] = "no vision support"
                continue

            cost = m.cost_per_1k * (request.tokens_needed / 1000.0 if request.tokens_needed else 1.0)
            # budget pressure amplifies the cost term and rewards local (free) models
            eff_cost_w = w.cost + pressure  # more pressure → cheaper preferred
            score = (
                w.quality * _quality_for(m, request.complexity)
                - eff_cost_w * cost
                - w.latency * (m.latency_ms / 1000.0)
            )
            if pressure > 0 and m.is_local:
                score += pressure * 0.5   # explicit downshift-to-local nudge
            viable.append((score, m))

        if not viable:
            return RouteResult(model=default_model, reason="no viable candidate",
                               rejected=rejected)

        # highest score wins; tie → lower cost, then name (determinism)
        best_score, best = max(
            viable, key=lambda t: (t[0], -t[1].cost_per_1k, t[1].name)
        )
        reason = "constraint+score"
        if best.is_local and request.sensitivity == "restricted":
            reason = "restricted→local-only"
        elif pressure > 0.5 and best.is_local:
            reason = "budget-downshift→local"
        return RouteResult(model=best.name, reason=reason, rejected=rejected,
                           score=round(best_score, 4))
    except Exception:  # noqa: BLE001 — routing must never break a turn
        return RouteResult(model=default_model, reason="error→default",
                           rejected=rejected)
