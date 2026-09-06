# SPDX-License-Identifier: MIT
# ============================================================
# KV-CACHE HOIST HELPERS
#
# Extracted from gateway.py so they can be unit-tested and
# imported without pulling in the full gateway module.
#
# Two exports consumed by gateway.py:
#
#   MEMORY_INSTRUCTION        — the hidden JSON-footer instruction
#                               appended to every system preface so
#                               the model piggybacks memory decisions
#                               on the answer turn (zero extra LLM calls).
#
#   build_local_system_message — assembles the stable system-message
#                               block placed at position 0 of the
#                               messages list for local-model routes.
#                               vLLM APC can cache those KV blocks
#                               across turns when the prefix is stable.
#
# Disable hoisting without redeploying:
#   LOCAL_KV_CACHE_HOIST=false  (checked in gateway.py, not here)
# ============================================================

from __future__ import annotations

from typing import List, Optional

# ── Memory instruction ────────────────────────────────────────────────────────
# Single source of truth shared between the cloud path (gateway.py
# _system_preface_parts) and the local KV-cache path (build_local_system_message).
# Changing the wording here propagates to both paths automatically.
MEMORY_INSTRUCTION: str = (
    "[MEMORY INSTRUCTION — follow exactly]\n"
    "At the very end of your response (after all answer text, on its own line), "
    "append this exact hidden tag — do NOT mention it, explain it, or show it inline:\n"
    '<!--MEMORY:{"store":true,"summary":"<≤150 char plain-English memory>",'
    '"context_key":"<2-4 word snake_case topic label>"}-->\n'
    "Set store=true ONLY when this exchange reveals: a user preference, personal fact "
    "(name, lucky number, location…), long-term interest, skill/tech stack, or important decision.\n"
    "Set store=false (with empty summary and context_key) for: greetings, one-off factual "
    "questions, trivial chat, or pure code with no personal context.\n"
    "context_key must be a stable snake_case label for the topic "
    "(e.g. lucky_number, kafka_streaming, solar_system) — use the SAME key if the topic "
    "was discussed before. This line is invisible to the user."
)


# ── Local system-message builder ─────────────────────────────────────────────

def build_local_system_message(
    *,
    agent_system_prompt: str = "",
    cowork_role_prompt: str = "",
    cowork_memory: str = "",
    custom_about: str = "",
    custom_style: str = "",
    memory_facts: Optional[List[str]] = None,
    feedback_hint: str = "",
    user_name: str = "",
    tone_pfx: str = "",
    sensitive: bool = False,
) -> str:
    """Build the stable system-message block for local-model KV-cache prefix.

    Assembles in order:
      1. Agent / cowork role prompt (if any)
      2. Stable persona block from cil.persona.compose_stable_persona
         (omits volatile per-turn mirror lines so the prefix stays cacheable)
      3. Cowork memory context (if any)
      4. Tone prefix (if any)
      5. MEMORY_INSTRUCTION footer

    Returns "" when there is nothing meaningful to inject (callers skip the
    hoist in that case). Never raises — any failure returns "".
    """
    try:
        parts: List[str] = []

        # 1. Agent / cowork role prompt — highest priority, placed first.
        if agent_system_prompt and agent_system_prompt.strip():
            parts.append(agent_system_prompt.strip())
        elif cowork_role_prompt and cowork_role_prompt.strip():
            parts.append(cowork_role_prompt.strip())

        # 2. Stable persona (no volatile mirror lines — keeps prefix cacheable).
        from cil.persona import compose_stable_persona
        persona = compose_stable_persona(
            user_name=user_name,
            custom_about=custom_about,
            custom_style=custom_style,
            memory_facts=memory_facts or [],
            feedback_hint=feedback_hint,
            sensitive=sensitive,
        )
        if persona:
            parts.append(persona)

        # 3. Cowork memory context (cross-chat facts injected by the memory layer).
        if cowork_memory and cowork_memory.strip():
            parts.append(cowork_memory.strip())

        # 4. Tone prefix (static part only — dynamic mirror lines are excluded).
        if tone_pfx and tone_pfx.strip():
            parts.append(tone_pfx.strip())

        # 5. Memory instruction footer — always last so the model sees it
        #    immediately before the conversation turns.
        parts.append(MEMORY_INSTRUCTION)

        result = "\n\n".join(p for p in parts if p).strip()
        return result

    except Exception:  # noqa: BLE001 — hoist must never break the answer
        return ""
