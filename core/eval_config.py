# SPDX-License-Identifier: MIT
"""
Platform → Evaluation Types Configuration
==========================================

Defines which evaluation checks run for each AiNxt platform.

To onboard a new platform:
  1. Add an entry to PLATFORM_EVAL_CONFIG below.
  2. Tag the platform's eval call site with platform="<key>".
  No other code changes required.

To disable an eval type for a platform at runtime:
  Set EVAL_ENABLED=false in .env to disable all evals globally.
  For per-platform control, remove the eval type from the list below
  and redeploy (no DB migration needed).

Eval types available:
  groundedness   — Hallucination detection (LLM-as-judge)
  relevance      — Answer usefulness (LLM-as-judge)
  coach_prompt   — Prompt quality assessment (LLM-as-judge)
  human_feedback — Direct thumbs up/down ratings (no judge, direct DB write)
"""

from __future__ import annotations

# ── Platform → eval types mapping ────────────────────────────────────────────
#
# Platform applicability matrix:
#
#   Platform         Hallucination  Usefulness  Prompt Quality  Human Feedback
#   ─────────────────────────────────────────────────────────────────────────
#   chat             ✅             ✅           ✅              ✅
#   knowledge_base   ✅             ✅           ✅              ✅
#   my_workspace     ✅             ✅           ✅              ❌
#   agent_studio     ✅             ✅           ❌              ❌
#   cli              ✅             ✅           ✅              ❌
#   ide_extension    ✅             ✅           ✅              ❌
#   buddy_cowork     ✅             ✅           ✅              ❌
#   workflows        ✅             ✅           ❌              ❌
#
# Note: human_feedback is always a direct DB write (not Kafka-routed).
# The Kafka eval_events topic handles: groundedness, relevance, coach_prompt.

PLATFORM_EVAL_CONFIG: dict[str, list[str]] = {
    "chat":           ["groundedness", "relevance", "coach_prompt", "human_feedback"],
    "knowledge_base": ["groundedness", "relevance", "coach_prompt", "human_feedback"],
    "my_workspace":   ["groundedness", "relevance", "coach_prompt"],
    "agent_studio":   ["groundedness", "relevance"],
    "cli":            ["groundedness", "relevance", "coach_prompt"],
    "ide_extension":  ["groundedness", "relevance", "coach_prompt"],
    "buddy_cowork":   ["groundedness", "relevance", "coach_prompt"],
    "workflows":      ["groundedness", "relevance"],
}

# Canonical platform name constants — use these instead of raw strings
# to avoid typos in call sites.
PLATFORM_CHAT           = "chat"
PLATFORM_KNOWLEDGE_BASE = "knowledge_base"
PLATFORM_MY_WORKSPACE   = "my_workspace"
PLATFORM_AGENT_STUDIO   = "agent_studio"
PLATFORM_CLI            = "cli"
PLATFORM_IDE_EXTENSION  = "ide_extension"
PLATFORM_BUDDY_COWORK   = "buddy_cowork"
PLATFORM_WORKFLOWS      = "workflows"

# Ordered list for UI dropdowns — matches display order in EvalsDashboard
PLATFORM_DISPLAY_ORDER: list[str] = [
    PLATFORM_CHAT,
    PLATFORM_KNOWLEDGE_BASE,
    PLATFORM_MY_WORKSPACE,
    PLATFORM_AGENT_STUDIO,
    PLATFORM_CLI,
    PLATFORM_IDE_EXTENSION,
    PLATFORM_BUDDY_COWORK,
    PLATFORM_WORKFLOWS,
]

PLATFORM_DISPLAY_NAMES: dict[str, str] = {
    PLATFORM_CHAT:           "Chat",
    PLATFORM_KNOWLEDGE_BASE: "Knowledge Base",
    PLATFORM_MY_WORKSPACE:   "My Workspace",
    PLATFORM_AGENT_STUDIO:   "Agent Studio",
    PLATFORM_CLI:            "CLI",
    PLATFORM_IDE_EXTENSION:  "IDE Extension",
    PLATFORM_BUDDY_COWORK:   "Buddy / CoWork",
    PLATFORM_WORKFLOWS:      "Workflows",
}


def get_eval_types(platform: str) -> list[str]:
    """
    Return the list of eval types enabled for a given platform.
    Falls back to [groundedness, relevance] for unknown platforms.
    """
    return PLATFORM_EVAL_CONFIG.get(platform, ["groundedness", "relevance"])


def is_eval_enabled_for(platform: str, eval_type: str) -> bool:
    """Return True if the given eval type is enabled for the platform."""
    return eval_type in get_eval_types(platform)
