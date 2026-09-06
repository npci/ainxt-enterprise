# SPDX-License-Identifier: MIT
# ============================================================
# PERSONA COMPOSER — the "feel like a friend" system-prompt builder
# ============================================================
#
# Turns the CIL's understanding of a turn (tone, language, sentiment, domain)
# plus what we know about the user (name, custom instructions, durable memory,
# learned feedback preferences) into ONE persona system-prompt block that makes
# the assistant warm, human, and tone-matched — instead of a stateless bot.
#
# DESIGN:
#   * Pure function. No model call, no I/O. Never raises. String in, string out.
#   * Baseline = casual-buddy (configurable via CHAT_PERSONA_BASELINE), then
#     MIRRORS the user's detected tone/register/language.
#   * HARD GUARDRAIL: on sensitive domains (finance/legal/security/compliance)
#     or PCI-ish content the persona DIALS DOWN to professional — no buddy
#     language, no over-familiarity. A banking tool must not be flippant about a
#     compliance question. This overrides baseline + mirroring.
#   * Fail-safe: any bad input → a minimal neutral-professional block (today's
#     posture), never a crash.
# ============================================================

from __future__ import annotations

import os
from typing import Any, List, Optional

# Domains where warmth must yield to professionalism (banking safety guardrail).
_SENSITIVE_DOMAINS = {"finance", "legal", "security", "compliance", "risk", "audit"}
# Belt-and-suspenders content floor: if these appear we go professional even if
# the CIL domain came back "general".
_SENSITIVE_MARKERS = (
    "pci", "aadhaar", "aadhar", "kyc", "pan ", "compliance", "regulat",
    "audit", "fraud", "breach", "incident", "sanction", "aml", "dss",
)

_BASELINE = os.getenv("CHAT_PERSONA_BASELINE", "casual").lower().strip()


def _is_sensitive(domain: str, question: str) -> bool:
    try:
        if (domain or "").lower().strip() in _SENSITIVE_DOMAINS:
            return True
        ql = (question or "").lower()
        return any(m in ql for m in _SENSITIVE_MARKERS)
    except Exception:  # noqa: BLE001
        return True  # err toward professional


def _casual_block(first_name: str) -> List[str]:
    name = f" Their name is {first_name}." if first_name else ""
    return [
        "[PERSONA — talk like a helpful friend, not a corporate bot]",
        f"Be warm, natural, and genuinely conversational.{name} Use contractions "
        "and everyday language. React briefly and human-ly when it fits (a quick "
        "'nice', 'got it', 'ah, tricky one') — but never fake or over-do it.",
        "Default to SHORT answers; expand only when the question needs it. Lead "
        "with the answer, not preamble. When useful, end with ONE natural next "
        "step or offer ('want me to …?').",
        "Do NOT spam emojis or exclamation marks. Never invent facts to sound "
        "friendly — warmth never overrides accuracy.",
    ]


def _professional_block(first_name: str, *, reason: str) -> List[str]:
    name = f" The user's name is {first_name}." if first_name else ""
    return [
        f"[PERSONA — professional and precise ({reason})]",
        f"Respond in a clear, professional, and respectful tone.{name} Be concise "
        "and accurate. Avoid slang, jokes, and over-familiarity. Prioritise "
        "correctness and compliance over friendliness for this topic.",
    ]


def _neutral_block(first_name: str) -> List[str]:
    name = f" The user's name is {first_name}." if first_name else ""
    return [
        "[PERSONA — helpful and human]",
        f"Be clear, friendly, and concise.{name} Lead with the answer; add a next "
        "step only when useful. Never fabricate.",
    ]


def _mirror_lines(conv_state: Any) -> List[str]:
    """Instruct the model to match the user's register/tone/language."""
    out: List[str] = []
    try:
        tone = getattr(conv_state, "tone", "neutral")
        language = getattr(conv_state, "language", "en")
        sentiment = getattr(conv_state, "sentiment", "neutral")
        wants_brief = bool(getattr(conv_state, "wants_brief", False))
        formality = float(getattr(conv_state, "formality", 0.5) or 0.5)

        if language and language not in ("en", "unknown", ""):
            out.append(f"Match the user's language/register ({language}) — mirror "
                       "their code-switching naturally.")
        if tone == "casual" or formality < 0.35:
            out.append("The user is casual — keep it relaxed and informal.")
        elif tone == "formal" or formality > 0.7:
            out.append("The user is formal — keep a polished, respectful register.")
        if tone == "frustrated" or sentiment == "neg":
            out.append("The user seems frustrated — be calm, acknowledge it briefly, "
                       "and get straight to a fix. No cheeriness.")
        elif tone == "excited" or sentiment == "pos":
            out.append("The user is upbeat — match their energy lightly.")
        if wants_brief:
            out.append("They want it SHORT — answer in as few words as possible.")
    except Exception:  # noqa: BLE001
        pass
    return out


def compose_stable_persona(
    *,
    user_name: str = "",
    custom_about: str = "",
    custom_style: str = "",
    memory_facts: Optional[List[str]] = None,
    feedback_hint: str = "",
    sensitive: bool = False,
) -> str:
    """Session-stable persona block for local-model KV-cache prefix.

    Identical to compose_persona() except it deliberately omits
    _mirror_lines() — those lines react to per-turn tone/sentiment and
    would bust the vLLM APC prefix cache on every request.

    Returns "" when there is genuinely nothing to say (callers can skip
    injecting an empty block). Never raises.
    """
    try:
        first_name = (user_name or "").strip().split()[0] if user_name else ""

        parts: List[str] = []

        # 1. Base persona (guardrail wins).
        if sensitive:
            parts += _professional_block(first_name, reason="sensitive topic")
        elif _BASELINE == "professional":
            parts += _professional_block(first_name, reason="platform default")
        elif _BASELINE == "adaptive":
            parts += _neutral_block(first_name)
        else:  # casual (default)
            parts += _casual_block(first_name)

        # 2. NO _mirror_lines() — those are volatile (change per turn).

        # 3. Who the user is + their preferences (memory).
        who: List[str] = []
        if custom_about:
            who.append(custom_about.strip())
        for f in (memory_facts or [])[:6]:
            if f and str(f).strip():
                who.append(str(f).strip())
        if who:
            parts.append("[About the user] " + " ".join(who)[:1500])
        if custom_style:
            parts.append("[User's preferred response style] " + custom_style.strip()[:800])

        # 4. Learned feedback preference (loop C) — highest-signal style nudge.
        if feedback_hint:
            parts.append("[Learned preference] " + feedback_hint.strip()[:400])

        return "\n".join(p for p in parts if p).strip()
    except Exception:  # noqa: BLE001 — persona must never break the answer
        return "\n".join(_neutral_block((user_name or "").strip().split()[0] if user_name else ""))


def compose_persona(
    *,
    conv_state: Any = None,
    question: str = "",
    user_name: str = "",
    custom_about: str = "",
    custom_style: str = "",
    memory_facts: Optional[List[str]] = None,
    feedback_hint: str = "",
) -> str:
    """Build the persona system-prompt block. Pure; never raises.

    Returns "" only when there is genuinely nothing to say (so callers can skip
    injecting an empty block)."""
    try:
        first_name = (user_name or "").strip().split()[0] if user_name else ""
        domain = getattr(conv_state, "domain", "general") if conv_state is not None else "general"
        sensitive = _is_sensitive(domain, question)

        parts: List[str] = []

        # 1. Base persona (guardrail wins).
        if sensitive:
            parts += _professional_block(first_name, reason=f"sensitive topic: {domain}")
        elif _BASELINE == "professional":
            parts += _professional_block(first_name, reason="platform default")
        elif _BASELINE == "adaptive":
            parts += _neutral_block(first_name)
        else:  # casual (default)
            parts += _casual_block(first_name)

        # 2. Mirror the user's tone/register — but NOT on sensitive turns (stay
        #    professional there regardless of how casually they phrased it).
        if not sensitive and conv_state is not None:
            parts += _mirror_lines(conv_state)

        # 3. Who the user is + their preferences (memory).
        who: List[str] = []
        if custom_about:
            who.append(custom_about.strip())
        for f in (memory_facts or [])[:6]:
            if f and str(f).strip():
                who.append(str(f).strip())
        if who:
            parts.append("[About the user] " + " ".join(who)[:1500])
        if custom_style:
            parts.append("[User's preferred response style] " + custom_style.strip()[:800])

        # 4. Learned feedback preference (loop C) — highest-signal style nudge.
        if feedback_hint:
            parts.append("[Learned preference] " + feedback_hint.strip()[:400])

        return "\n".join(p for p in parts if p).strip()
    except Exception:  # noqa: BLE001 — persona must never break the answer
        # Minimal safe fallback = today's neutral posture.
        return "\n".join(_neutral_block((user_name or "").strip().split()[0] if user_name else ""))
