# SPDX-License-Identifier: MIT
# ============================================================
# Degradation ladder — health → serving level (pure)
# ============================================================
#
# docs/architecture/20-production-readiness.md §20.6. Consolidates every
# subsystem's fail-safe rung into ONE policy: given which capabilities are
# healthy, decide the highest serving level the platform can still deliver.
#
#   healthy → cloud-degraded → retrieval/tools-degraded → context-degraded → LOCAL-ONLY
#
# THE FLOOR: in-house model + flat-summary context + no tools = a degraded-but-
# alive assistant that never fully goes down while the perimeter is up (Tenet 3).
#
# Pure policy — no I/O, no health-probing (the caller supplies a Health snapshot
# from real probes). Deterministic + testable. Fail-safe: on any error it returns
# the LOCAL_ONLY floor, because "alive and degraded" always beats "crashed".
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

# serving levels, best → worst (docs/architecture/20 §20.6)
FULL = "full"                       # full quality: cloud + retrieval + tools + rich context
CLOUD_DEGRADED = "cloud_degraded"   # cloud provider(s) down → fallback/local generation
TOOLS_DEGRADED = "tools_degraded"   # retrieval/tools down → skip + hedge
CONTEXT_DEGRADED = "context_degraded"  # context engine down → flat-summary fallback
LOCAL_ONLY = "local_only"           # the floor: in-house model, no tools, flat context

LEVELS = [FULL, CLOUD_DEGRADED, TOOLS_DEGRADED, CONTEXT_DEGRADED, LOCAL_ONLY]
_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}  # higher rank = more degraded


@dataclass
class Health:
    """A snapshot of subsystem health from real probes (caller-supplied).
    `perimeter` is the local model + app; if it is down the platform is truly
    unavailable — everything else degrades gracefully around it."""

    perimeter: bool = True      # local model + app process reachable
    cloud: bool = True          # at least one cloud provider via proxy
    retrieval: bool = True      # pgvector / embed svc / rerank
    tools: bool = True          # tool executors
    context_engine: bool = True # context assembler


@dataclass
class Degradation:
    level: str = FULL
    reasons: List[str] = field(default_factory=list)
    available: bool = True      # False only when the perimeter itself is down
    use_cloud: bool = True
    use_retrieval: bool = True
    use_tools: bool = True
    rich_context: bool = True
    hedge: bool = False         # tell the user the answer is degraded/uncertain

    def as_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level, "available": self.available,
            "use_cloud": self.use_cloud, "use_retrieval": self.use_retrieval,
            "use_tools": self.use_tools, "rich_context": self.rich_context,
            "hedge": self.hedge, "reasons": list(self.reasons),
        }


def degrade(health: Health) -> Degradation:
    """Map a health snapshot to the highest serving level still deliverable.
    Never raises → returns the LOCAL_ONLY floor on any error (alive > crashed)."""
    try:
        d = Degradation()

        # perimeter down = the one true outage; everything else is survivable.
        if not getattr(health, "perimeter", True):
            return Degradation(
                level=LOCAL_ONLY, available=False,
                use_cloud=False, use_retrieval=False, use_tools=False,
                rich_context=False, hedge=True,
                reasons=["perimeter down — platform unavailable"],
            )

        worst = FULL

        if not getattr(health, "cloud", True):
            d.use_cloud = False
            d.reasons.append("cloud providers down → local generation")
            worst = _max(worst, CLOUD_DEGRADED)

        if not getattr(health, "retrieval", True) or not getattr(health, "tools", True):
            if not health.retrieval:
                d.use_retrieval = False
                d.reasons.append("retrieval down → skip evidence, hedge")
            if not health.tools:
                d.use_tools = False
                d.reasons.append("tools down → skip tool calls, hedge")
            d.hedge = True
            worst = _max(worst, TOOLS_DEGRADED)

        if not getattr(health, "context_engine", True):
            d.rich_context = False
            d.reasons.append("context engine down → flat-summary fallback")
            worst = _max(worst, CONTEXT_DEGRADED)

        d.level = worst
        d.available = True
        return d
    except Exception:  # noqa: BLE001 — degraded-but-alive always beats crashed
        return Degradation(
            level=LOCAL_ONLY, available=True,
            use_cloud=False, use_retrieval=False, use_tools=False,
            rich_context=False, hedge=True,
            reasons=["degradation policy error → local-only floor"],
        )


def _max(a: str, b: str) -> str:
    """Return the more-degraded (higher-rank) of two levels."""
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b
