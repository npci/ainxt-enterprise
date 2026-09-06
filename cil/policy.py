# SPDX-License-Identifier: MIT
# ============================================================
# CIL policy derivation — decisions from state + profile (pure)
# ============================================================
#
# docs/architecture/05-semantic-understanding.md §5.6 (Class 4) + §5.7. Splits
# the model's OBSERVATIONS (scores/labels on ConversationState) from the DECISIONS
# (risk? clarify? verify hard? escalate to cloud?). Observations are domain-neutral;
# decisions are domain-specific and live here so "a new domain is a new policy file,
# not a re-trained classifier" (Tenet 2).
#
# Pure functions of (state, profile) — no I/O, no model. Deterministic + instant +
# testable. Fail-safe: any error returns today's implicit default (low risk, no
# clarify), so understanding stays additive.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# risk ordering (low < medium < high) for threshold comparisons
_RISK_RANK = {"low": 0, "medium": 1, "high": 2}

# tool families considered mutating/write (drive high risk, §5.6)
_WRITE_FAMILIES = {"write", "email", "delete", "deploy", "payment", "exec", "shell"}


@dataclass
class DomainProfile:
    """Per-domain policy knobs. Defaults reproduce today's behavior (low-risk,
    rarely clarify). A new vertical ships a new DomainProfile, nothing else."""

    name: str = "general"
    high_risk_domains: List[str] = field(default_factory=list)   # e.g. ["finance","legal"]
    ambiguity_threshold: float = 0.6      # clarify only above this
    min_risk_to_clarify: str = "medium"   # never interrupt low-risk turns
    tool_need_threshold: float = 0.5      # per-profile tool trigger (§5.5 calibration)
    default_sensitivity: str = "internal" # public|internal|confidential|restricted


def _rank(risk: str) -> int:
    return _RISK_RANK.get(risk, 0)


def _has_write_tool(families: Optional[List[str]]) -> bool:
    return any((f or "").lower() in _WRITE_FAMILIES for f in (families or []))


def derive_risk_level(state, profile: DomainProfile) -> str:
    """high if a mutating tool is likely or domain is high-risk; medium if the
    content is sensitive; else low. Pure, never raises (§5.6)."""
    try:
        tool = getattr(state, "tool_need", None)
        tscore = getattr(tool, "score", 0.0) or 0.0
        tfams = getattr(tool, "tags", None) or getattr(tool, "families", None)
        if tscore > profile.tool_need_threshold and _has_write_tool(tfams):
            return "high"
        if getattr(state, "domain", "general") in (profile.high_risk_domains or []):
            return "high"
        if getattr(state, "sensitivity", None) in ("confidential", "restricted"):
            return "medium"
        return "low"
    except Exception:  # noqa: BLE001 — policy must never break a turn
        return "low"


def derive_clarification(state, profile: DomainProfile) -> bool:
    """Ask instead of guess only when ambiguous AND stakes are high enough AND the
    turn is not a continuation (§5.6, §5.7 'ambiguous but high-stakes')."""
    try:
        amb = getattr(state, "ambiguity", None)
        ascore = getattr(amb, "score", 0.0) or 0.0
        risk = derive_risk_level(state, profile)
        return (
            ascore > profile.ambiguity_threshold
            and _rank(risk) >= _rank(profile.min_risk_to_clarify)
            and not getattr(state, "is_continuation", False)
        )
    except Exception:  # noqa: BLE001
        return False


def derive_sensitivity(state, profile: DomainProfile, clearance: Optional[str] = None) -> str:
    """Combine any data-classification hint on the state with the profile default;
    a lower principal clearance can only RAISE the required handling, never lower
    the label. Returns the most-restrictive of {state hint, profile default}."""
    try:
        order = ["public", "internal", "confidential", "restricted"]
        candidates = [profile.default_sensitivity]
        hint = getattr(state, "sensitivity", None)
        if hint:
            candidates.append(hint)
        # most restrictive wins
        return max(candidates, key=lambda s: order.index(s) if s in order else 0)
    except Exception:  # noqa: BLE001
        return profile.default_sensitivity


def tools_allowed(state, profile: DomainProfile) -> bool:
    """Whether tool_need clears this profile's trigger threshold (§5.5)."""
    try:
        tool = getattr(state, "tool_need", None)
        return (getattr(tool, "score", 0.0) or 0.0) >= profile.tool_need_threshold
    except Exception:  # noqa: BLE001
        return False


def derive_policy(state, profile: DomainProfile, clearance: Optional[str] = None) -> Dict[str, Any]:
    """One-shot: all Class-4 decisions for a turn. The single entry point callers
    use; individual derivations remain public for targeted reuse/testing."""
    risk = derive_risk_level(state, profile)
    return {
        "risk_level": risk,
        "clarification_needed": derive_clarification(state, profile),
        "sensitivity": derive_sensitivity(state, profile, clearance),
        "tools_allowed": tools_allowed(state, profile),
        "profile": profile.name,
    }
