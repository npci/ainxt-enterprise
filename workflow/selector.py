# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Workflow selection — State → workflow (pure)
# ============================================================
#
# docs/architecture/13-generation-workflows.md §13.5. Chooses the specialized
# pipeline for a turn from the ConversationState. Pure function → testable; NOT
# yet wired (adoption later, flag-gated). Fail-safe: unknown → QA (the
# highest-volume default, today's behavior).
#
# Reads a ConversationState-shaped object duck-typed (only attribute reads), so
# it stays importable in a bare env and decoupled from cil.state.
# ============================================================

from __future__ import annotations

from enum import Enum
from typing import Any


class Workflow(str, Enum):
    """Specialized pipeline for a turn (docs/architecture/13 §13.2).

    str-Enum so values compare/serialize as plain strings at boundaries while
    giving callers a typed vocabulary (consistent with evolution.tier2.Verdict).
    """

    QA = "qa"                     # default, highest-volume
    DOCUMENT = "document"
    PRESENTATION = "presentation"
    SDLC = "sdlc"                 # deep coding pipeline
    CODING_CHAT = "coding_chat"   # shallow code turn
    DATA_ANALYSIS = "data_analysis"
    SUMMARIZE = "summarize"       # elevated segment→map→reduce pipeline (§13.6)


# module-level aliases (terse call sites / back-compat with string comparisons)
QA = Workflow.QA
DOCUMENT = Workflow.DOCUMENT
PRESENTATION = Workflow.PRESENTATION
SDLC = Workflow.SDLC
CODING_CHAT = Workflow.CODING_CHAT
DATA_ANALYSIS = Workflow.DATA_ANALYSIS
SUMMARIZE = Workflow.SUMMARIZE


def select_workflow(state: Any) -> Workflow:
    """Return the Workflow for a turn. Never raises.

    Precedence mirrors §13.5: explicit output format first, then a summarize
    intent, then the code-domain depth split, then data, else QA.
    """
    try:
        output_format = getattr(state, "output_format", "prose")
        domain = getattr(state, "domain", "general")
        complexity = getattr(state, "task_complexity", "medium")
        tool_tags = set(getattr(getattr(state, "tool_need", None), "tags", []) or [])
        intent = (getattr(state, "intent", "") or "").lower()
    except Exception:  # noqa: BLE001
        return QA

    if output_format == "document":
        return DOCUMENT
    if output_format == "presentation":
        return PRESENTATION
    if output_format == "data":
        return DATA_ANALYSIS

    # a summarize intent routes to the elevated summarize pipeline (§13.6),
    # unless a stronger output_format above already claimed the turn.
    if intent == "summarize" or "summari" in intent:
        return SUMMARIZE

    is_code = domain == "code" or bool(tool_tags & {"code", "vcs"})
    if is_code:
        return SDLC if complexity in ("deep", "solution") else CODING_CHAT

    return QA
