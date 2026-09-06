# SPDX-License-Identifier: MIT
# ============================================================
# Response shaping — most-specific-wins merge of style signals (pure)
# ============================================================
#
# docs/architecture/17-personalization.md §17.5 (PR5). The answer's SHAPE is a
# deterministic merge, not special-casing:
#
#   response shape = f(profile.tone, role.default_style, user.custom_response_style,
#                      cil.expertise_level, cil.emotional_tone, cil.response_style)
#
# Most-specific-wins: a per-turn "explain like I'm five" overrides the user's
# usual "concise", which overrides the role default, which overrides the profile.
# Output is a ResponseShape the prompt instruction slot (§9) renders. Pure →
# testable; fail-safe: any error yields the neutral platform default shape.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# style / tone vocabularies (docs/architecture/17 §17.2-17.5)
_STYLES = ("concise", "detailed", "step_by_step")
_TONES = ("neutral", "helpful", "formal-precise", "technical", "brand-voice")

# expertise → depth adjustment (§17.4): novices get scaffolding, experts terse
_EXPERTISE_DEPTH = {"novice": "detailed", "intermediate": None, "expert": "concise"}


@dataclass
class ResponseShape:
    style: str = "detailed"        # concise|detailed|step_by_step
    tone: str = "helpful"          # profile presentation tone
    depth: str = "normal"          # scaffolded|normal|terse (from expertise)
    empathetic: bool = False       # from emotional_tone
    show_citations: bool = False
    sources: List[str] = field(default_factory=list)   # provenance of each decision

    def as_dict(self) -> Dict[str, Any]:
        return {
            "style": self.style, "tone": self.tone, "depth": self.depth,
            "empathetic": self.empathetic, "show_citations": self.show_citations,
            "sources": list(self.sources),
        }


def _first_valid(candidates, allowed):
    """Return the first candidate that is a non-empty allowed value, with its rank
    index (for provenance). Candidates are ordered least→most specific."""
    chosen, src = None, None
    for name, val in candidates:
        if val and val in allowed:
            chosen, src = val, name   # later (more specific) overrides earlier
    return chosen, src


def shape_response(
    *,
    profile_tone: str = "helpful",
    profile_style: str = "detailed",
    role_style: Optional[str] = None,
    user_style: Optional[str] = None,
    turn_style: Optional[str] = None,
    expertise: Optional[str] = None,
    emotional_tone: Optional[str] = None,
    show_citations: bool = False,
) -> ResponseShape:
    """Merge style signals most-specific-wins. Precedence for style (low→high):
    profile < role < user < per-turn. Expertise adjusts depth (and nudges style
    if no explicit signal). Emotional tone flips empathy. Never raises."""
    shape = ResponseShape()
    try:
        # ── style: most-specific-wins over the ordered candidates ──
        style, style_src = _first_valid(
            [("profile", profile_style), ("role", role_style),
             ("user", user_style), ("turn", turn_style)],
            _STYLES,
        )
        # expertise only fills style if nothing more specific than profile set it
        if style_src in (None, "profile") and expertise in _EXPERTISE_DEPTH:
            exp_style = _EXPERTISE_DEPTH[expertise]
            if exp_style:
                style, style_src = exp_style, "expertise"
        shape.style = style or "detailed"

        # ── tone ──
        shape.tone = profile_tone if profile_tone in _TONES else "helpful"

        # ── depth from expertise (§17.4) ──
        shape.depth = {"novice": "scaffolded", "expert": "terse"}.get(expertise, "normal")

        # ── empathy from emotional tone (§17.5) ──
        shape.empathetic = emotional_tone in ("frustrated", "urgent")

        shape.show_citations = bool(show_citations)
        shape.sources = [s for s in [f"style:{style_src}",
                                     f"depth:{expertise}" if expertise else None] if s]
        return shape
    except Exception:  # noqa: BLE001 — shaping must never break a turn
        return ResponseShape()
