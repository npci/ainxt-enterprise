# SPDX-License-Identifier: MIT
# ============================================================
# SKILL SYNTHESIS — shared, pure skill-code generator
#
# Extracted from routers/skills_router.generate_skill so BOTH the NL skill
# endpoint and the self-improving skill loop (workers/skill_loop_worker.py)
# generate skill bodies through ONE code path.
#
# This module performs NO database writes and NO compliance gating — callers
# own persistence (SkillRecord) and the compliance scan. It only builds the
# prompt, calls the model_router, and extracts the skill body.
#
# Two skill types (mirrors db/models.SkillRecord.skill_type):
#   "execution"  → Python `def run(input: str) -> dict`
#   "behavioral" → plain-text SOP injected into a system prompt
# ============================================================
from __future__ import annotations

import re as _re

from core.logger import logger


def _build_prompt(name: str, description: str, skill_type: str) -> str:
    """Build the model prompt for the requested skill type. Lifted verbatim
    (intent-preserving) from skills_router.generate_skill so behavior matches."""
    if skill_type == "behavioral":
        return (
            "You are an AiNxt AI Platform domain expert writing behavioral skill instructions.\n"
            "A behavioral skill is plain-text SOP (Standard Operating Procedure) that gets injected\n"
            "directly into an AI agent's system prompt to shape how it responds.\n\n"
            f"Skill name: {name}\n"
            f"What it should define: {description}\n\n"
            "RULES:\n"
            "1. Write clear, authoritative instructions in plain English — NO Python code\n"
            "2. Use numbered rules or bullet points for clarity\n"
            "3. Be specific to the AiNxt/fintech domain (UPI, IMPS, AiNxt switch, payment rails, etc.)\n"
            "4. Cover: tone, response format, domain rules, what to do, what NOT to do\n"
            "5. Keep it under 400 words — the LLM will read this as part of its system prompt\n"
            "6. Do NOT wrap in code blocks — return plain text only\n\n"
            "Write the behavioral instructions now:"
        )
    return (
        "You are an AiNxt AI Platform skill engineer.\n"
        "Generate a Python skill for the AiNxt engineering platform.\n\n"
        f"Skill name: {name}\n"
        f"What it should do: {description}\n\n"
        "RULES:\n"
        "1. Define EXACTLY one function: def run(input: str) -> dict\n"
        "2. The function receives a user request as a plain string\n"
        "3. Return a dict with at least {'output': str} — add extra keys as needed\n"
        "4. Use only Python standard library (json, re, datetime, collections, math) — no pip installs\n"
        "5. If the skill needs to call an external service, return a structured dict describing what to call\n"
        "6. Make it genuinely useful for an enterprise financial platform (AiNxt)\n"
        "7. Return ONLY the Python code in a ```python block — NO explanation\n\n"
        "Examples of useful patterns:\n"
        "- Parsing/extracting from text input\n"
        "- Formatting/templating output\n"
        "- Classification/routing logic\n"
        "- Summarization prompts that call the LLM via returned dict\n\n"
        "Generate the skill now:"
    )


def synthesize_skill(
    name: str,
    description: str,
    skill_type: str = "execution",
    department: str = "",
) -> dict:
    """Generate a skill body from a name + plain-English description.

    Pure: no DB writes, no compliance gating — the caller owns both.

    Returns:
      {"code": str, "skill_type": "execution"|"behavioral"}

    For execution skills, guarantees the returned code defines `def run(`
    (falls back to a trivial echo stub if the model omits it), matching the
    original router behavior.
    """
    from models.model_router import model_router

    skill_type = skill_type if skill_type in ("execution", "behavioral") else "execution"
    prompt = _build_prompt(name, description, skill_type)

    raw = model_router.generate(prompt, model_hint="claude")

    if skill_type == "behavioral":
        code = (raw or "").strip()
    else:
        match = _re.search(r"```python\s*(.*?)```", raw or "", _re.DOTALL)
        code = match.group(1).strip() if match else (raw or "").strip()
        if "def run(" not in code:
            code = (
                f"def run(input: str) -> dict:\n"
                f"    # Auto-generated from: {description}\n"
                f"    return {{'output': input}}"
            )

    logger.debug(f"[SkillSynthesis] synthesized {skill_type} skill {name!r} ({len(code)} chars)")
    return {"code": code, "skill_type": skill_type}
