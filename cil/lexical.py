# SPDX-License-Identifier: Apache-2.0
# ============================================================
# CIL Class-1 lexical signals — fast, deterministic, no model call
# ============================================================
#
# docs/architecture/05-semantic-understanding.md §5.3. These resolve the common
# case (freshness, continuation, obvious output format) with regex only, so the
# batched local-LLM call (Class 3) is reached only when genuinely needed. Pure
# stdlib — no infra, importable in a bare test env.
# ============================================================

from __future__ import annotations

import re
from typing import Optional


# temporal keywords → the answer likely needs fresh/live info
_FRESHNESS_RE = re.compile(
    r"\b(today|todays|tonight|now|currently|current|latest|recent|recently|"
    r"this (?:week|month|year)|yesterday|breaking|up[- ]?to[- ]?date|"
    r"20\d{2})\b",
    re.IGNORECASE,
)

# discourse markers that signal the turn continues the previous one
_CONTINUATION_RE = re.compile(
    r"^\s*(also|and then|then|next|additionally|furthermore|plus|"
    r"what about|how about|instead|actually|no,? i meant|as well|too)\b",
    re.IGNORECASE,
)

# explicit output-format cues
_TABLE_RE = re.compile(r"\b(as a table|in a table|tabular|table format|columns?)\b", re.IGNORECASE)
_CODE_RE = re.compile(r"\b(code|function|script|snippet|implement|refactor|debug|regex|sql query)\b", re.IGNORECASE)
_DOC_RE = re.compile(
    r"\b(write|create|generate|draft|make|prepare)\b.{0,40}\b"
    r"(report|document|doc|proposal|letter|email|memo|policy|deck|presentation|ppt|"
    r"spreadsheet|summary document)\b",
    re.IGNORECASE,
)


def detect_freshness(question: str) -> str:
    """Return 'high' | 'low' | 'none' from temporal keywords."""
    if not question:
        return "none"
    return "high" if _FRESHNESS_RE.search(question) else "none"


def detect_continuation(question: str) -> bool:
    """True if the turn opens with a continuation discourse marker."""
    return bool(question and _CONTINUATION_RE.match(question))


def detect_output_format(question: str) -> Optional[str]:
    """Return 'document' | 'table' | 'code' | None (no confident lexical signal).

    Coarse pre-LLM prefilter only. models.doc_intent.classify is the
    authoritative (LLM-backed) document detector downstream; this regex just
    provides a cheap CIL hint without a model call.

    Order matters: an explicit document request outranks a code mention
    ('write a report about the code' is a document).
    """
    if not question:
        return None
    if _DOC_RE.search(question):
        return "document"
    if _TABLE_RE.search(question):
        return "table"
    if _CODE_RE.search(question):
        return "code"
    return None
