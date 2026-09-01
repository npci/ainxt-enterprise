# SPDX-License-Identifier: Apache-2.0
# ============================================================
# DomainProfile — Wave 1 subset (behavior-as-data)
# ============================================================
#
# A Domain Profile parameterizes the invariant core so the same code serves
# different audiences (enterprise / regulated / coding / customer-facing)
# without forking. See docs/architecture/17-personalization.md §17.2 for the
# full schema; Wave 1 defines ONLY the strict subset needed so the default
# profile can reproduce today's runtime constants exactly. Later waves widen
# this (routing/retrieval/grounding fields) — new fields append, matching the
# forward-compatible envelope rule.
#
# Pure stdlib only (importable in a bare test env). frozen=True: profiles are
# immutable value objects.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ContextPolicy:
    """Context-engineering knobs. Defaults MUST equal today's gateway constants.

    - usable_fraction 0.75 == gateway.py:279 _CONTEXT_USABLE_FRACTION (env CHAT_CONTEXT_USABLE_FRACTION).
    - history_retrieval_enabled False — the overflow-retrieval feature does not
      exist yet in gateway (kept False so the default profile matches reality).
    - durable_memory_max_tokens 800 — durable-memory slot cap (docs/architecture/17.2).
    """

    usable_fraction: float = 0.75
    history_retrieval_enabled: bool = False
    durable_memory_max_tokens: int = 800


@dataclass(frozen=True)
class RoutingPolicy:
    """Model-routing weights + hard constraints (docs/architecture/10 §10.2, 17 §17.2).

    Defaults reproduce today's implicit posture: quality-led, privacy handled by
    the local-first tiering (no hard floor), no cost/latency ceilings. A
    regulated profile raises privacy_floor and tightens ceilings.
    """

    # weighted-score terms (need not sum to 1; relative magnitudes matter)
    w_quality: float = 0.6
    w_cost: float = 0.2
    w_latency: float = 0.2
    # hard constraints
    privacy_floor: str = "public"        # public|internal|confidential|restricted
    max_latency_ms: Optional[int] = None
    max_cost_per_turn_usd: Optional[float] = None


@dataclass(frozen=True)
class GroundingPolicy:
    """Per-claim verification posture (docs/architecture/14 §14.6)."""

    per_claim_verification: str = "off"  # off|on_factual|always
    abstain_below_confidence: float = 0.0
    citation_required: bool = False


@dataclass(frozen=True)
class DomainProfile:
    """A named policy bundle over the invariant core (Waves 1-4 subset)."""

    profile_id: str = "enterprise_default"
    extends: Optional[str] = None
    context: ContextPolicy = field(default_factory=ContextPolicy)
    routing: RoutingPolicy = field(default_factory=RoutingPolicy)
    grounding: GroundingPolicy = field(default_factory=GroundingPolicy)
    # retrieval / planning / tools / presentation / governance sections
    # (docs/architecture/17.2) are added in later waves.
