# SPDX-License-Identifier: MIT
# ============================================================
# PolicyResolver — resolves a DomainProfile to an EffectivePolicy
# ============================================================
#
# docs/architecture/17-personalization.md §17.1 defines a most-specific-wins
# merge: PLATFORM DEFAULTS ◄ DOMAIN PROFILE ◄ ORG ◄ ROLE ◄ USER. Wave 1
# implements ONLY the platform-default layer (org/role/user merge deferred to a
# later wave). The critical Wave-1 acceptance is that the resolved policy
# reproduces today's runtime constants exactly (usable_fraction == 0.75), so
# that when downstream code eventually reads EffectivePolicy instead of the
# inline gateway constant, behavior is unchanged.
#
# Pure stdlib only (importable in a bare test env).
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from profiles.schema import DomainProfile, GroundingPolicy, RoutingPolicy


@dataclass(frozen=True)
class EffectivePolicy:
    """The flattened, resolved policy the core reads per request (Waves 1-4)."""

    usable_fraction: float
    history_retrieval_enabled: bool
    durable_memory_max_tokens: int
    profile_id: str
    # nested policy sections are passed through by reference (frozen values)
    routing: RoutingPolicy = RoutingPolicy()
    grounding: GroundingPolicy = GroundingPolicy()


# The default profile — calibrated to reproduce today's behavior exactly.
ENTERPRISE_DEFAULT = DomainProfile()


class PolicyResolver:
    """Resolves the effective policy for a request.

    Wave 1: single platform-default layer. The `user_ctx` argument is accepted
    now (so callers are stable) but not yet used for org/role/user merging.
    """

    def resolve(
        self,
        *,
        user_ctx: Optional[Dict[str, Any]] = None,
        profile: Optional[DomainProfile] = None,
    ) -> EffectivePolicy:
        p = profile or ENTERPRISE_DEFAULT
        return EffectivePolicy(
            usable_fraction=p.context.usable_fraction,
            history_retrieval_enabled=p.context.history_retrieval_enabled,
            durable_memory_max_tokens=p.context.durable_memory_max_tokens,
            profile_id=p.profile_id,
            routing=p.routing,
            grounding=p.grounding,
        )
