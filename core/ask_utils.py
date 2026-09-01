# SPDX-License-Identifier: Apache-2.0
"""
core/ask_utils.py
=================
Shared utility functions used by BOTH the Chat path (gateway.py) and the
KB ask path (routers/kb_ask_router.py).

WHY THIS FILE EXISTS
--------------------
Several helper functions were previously defined as closures inside
gateway.py's ask_ai() — they captured outer-scope variables like
_bypass_safety_filters, _rag_mode, _ce_ask. This made them impossible
to reuse in kb_ask_router.py without copy-pasting.

This module extracts those helpers as proper standalone functions with
explicit parameters so both paths import from one place. No logic is
changed — only the parameter-passing style.

FUNCTIONS
---------
hist_redact(text, bypass_safety, compliance_engine)
    Redact PII from history text (gated by COMPLIANCE_SCAN_HISTORY).

out_redact(text, bypass_safety, rag_mode, compliance_engine)
    Redact PII from LLM output (always ON for cloud KB, env-gated for Chat).

clean_for_history(content, role)
    Prepare a message for history injection — verbatim for user turns,
    compacted (strip code/JSON/tables) for assistant turns.

is_followup_question(q_text, history)
    Cheap regex-based follow-up heuristic. Zero LLM cost.
    Returns True when the question is short with no new named entities.

build_kb_grounded_prompt(safe_question, docs_ctx, is_followup,
                         has_history, chat_scope_doc_ids)
    Build the grounded prompt string to inject into _fp_messages[-1]
    for KB chat (follow-up / multi-doc / single-doc variants).
    Shared by gateway._general_stream() and kb_ask_router._kb_stream().
"""

from __future__ import annotations

import re
from typing import List, Optional

# Pre-import KB_DOC_PROMPT at module load time so it is never imported
# inside an async generator call (which caused a silent crash when
# agents.tools triggered DB/telemetry imports mid-stream).
from agents.tools import KB_DOC_PROMPT as _KB_DOC_PROMPT


# ---------------------------------------------------------------------------
# hist_redact
# ---------------------------------------------------------------------------

def hist_redact(
    text: str,
    bypass_safety: bool,
    compliance_engine,
) -> str:
    """
    Redact PII from a history message before injecting into the LLM prompt.

    Gated by COMPLIANCE_SCAN_HISTORY env var (default OFF).
    When bypass_safety=True (local model selected in KB mode) returns text as-is.
    """
    if bypass_safety:
        return text
    try:
        from core.config import COMPLIANCE_SCAN_HISTORY as _SH
        if not _SH:
            return text
        return compliance_engine.redact_text(text)[0]
    except Exception:
        return text


# ---------------------------------------------------------------------------
# out_redact
# ---------------------------------------------------------------------------

def out_redact(
    text: str,
    bypass_safety: bool,
    rag_mode: str,
    compliance_engine,
) -> str:
    """
    Redact PII from LLM output before persisting to history.

    For KB chat (rag_mode in {"auto","on"}): always redact for cloud models —
    KB chunks may contain PANs/card numbers/PII that must not echo back.
    For Chat (rag_mode="off"): gated by COMPLIANCE_SCAN_LLM_OUTPUT env var.
    When bypass_safety=True (local model) returns text as-is.
    """
    if bypass_safety:
        return text
    _force_kb_out = rag_mode in {"auto", "on"}
    if not _force_kb_out:
        try:
            from core.config import COMPLIANCE_SCAN_LLM_OUTPUT as _SO
            if not _SO:
                return text
        except Exception:
            return text
    try:
        return compliance_engine.redact_text(text)[0]
    except Exception:
        return text


# ---------------------------------------------------------------------------
# clean_for_history
# ---------------------------------------------------------------------------

def clean_for_history(content: str, role: str = "assistant") -> str:
    """
    Prepare a message for history injection.

    User turns: kept verbatim (only whitespace-collapsed + length-capped).
    Assistant turns: code blocks / JSON / tables stripped to prose so the
    model understands the outcome without re-processing large blocks.
    """
    if role == "user":
        text = re.sub(r'\n{3,}', '\n\n', content)
        text = re.sub(r'[ \t]{2,}', ' ', text).strip()
        if len(text) > 4000:
            text = text[:3600] + " … " + text[-360:]
        return text

    # Assistant: compact structured blocks to prose
    def _code_tag(m):
        lang  = (m.group(1) or "code").strip() or "code"
        lines = m.group(2).strip().splitlines()
        return f"[{lang} code: {len(lines)} lines]"

    text = re.sub(r'```(\w*)\n?([\s\S]*?)```', _code_tag, content)
    text = re.sub(r'(?m)^(    |\t).+', '', text)
    text = re.sub(r'\{[^}]{0,500}\}', '[data]', text)
    text = re.sub(r'\[[^\]]{20,}\]',  '[list]',  text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*{1,2}([^*\n]+)\*{1,2}', r'\1', text)
    text = re.sub(r'`([^`\n]+)`', r'\1', text)
    text = re.sub(r'^\s*\|.+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{2,}', '\n', text)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    if len(text) > 1200:
        text = text[:1000] + " … " + text[-180:]
    return text


# ---------------------------------------------------------------------------
# is_followup_question
# ---------------------------------------------------------------------------

def is_followup_question(q_text: str, history: list) -> bool:
    """
    Cheap regex-based follow-up heuristic. Zero LLM cost.

    Returns True when the question is a short follow-up with no new named
    technical entities anchoring it to a new topic.

    This is the GENERAL-PURPOSE signal used by EVERY chat turn regardless
    of KB status. KB Chat's more expensive LLM-based condensation is layered
    on top of this, KB-only, in the follow-up condenser.
    """
    _prior = [m for m in history if m.get("role") == "assistant"]
    if not _prior:
        return False
    if len(q_text.strip()) > 120:
        return False
    _has_entity = bool(re.search(
        r'[@/\\]|\.py\b|\.ts\b|\.js\b|\.java\b|\.go\b|\.rs\b'
        r'|https?://'
        r'|[A-Z][a-z]+[A-Z]'
        r'|`[^`]+`'
        r'|\b[A-Z_]{3,}\b',
        q_text,
    ))
    return not _has_entity


# ---------------------------------------------------------------------------
# build_kb_grounded_prompt
# ---------------------------------------------------------------------------

def build_kb_grounded_prompt(
    safe_question: str,
    docs_ctx: str,
    is_followup: bool,
    has_history: bool,
    chat_scope_doc_ids: list,
) -> str:
    """
    Build the grounded prompt string for KB chat.

    Three variants:
    1. Follow-up with history  → inject context as supplementary note
    2. Multi-doc (2+ selected) → citation-aware prompt
    3. Single doc              → strict KB grounding prompt (KB_DOC_PROMPT)

    Used by both gateway._general_stream() and kb_ask_router._kb_stream()
    so the grounding logic is never duplicated.
    """
    _KDP = _KB_DOC_PROMPT

    _fp_multi_doc_count = len(chat_scope_doc_ids) if chat_scope_doc_ids else 0

    if is_followup and has_history:
        return (
            safe_question
            + "\n\n[Additional reference context retrieved from the knowledge base:\n"
            + docs_ctx
            + "\nUse ONLY the above context to answer. "
            + "Do not supplement with general knowledge.]"
        )
    elif _fp_multi_doc_count > 1:
        return (
            f"Answer the following question using ONLY the "
            f"{_fp_multi_doc_count} documents provided below.\n"
            f"Each document is clearly labeled. For every piece of "
            f"information in your answer, cite the document name it came from. "
            f"If the answer spans multiple documents, synthesize them clearly "
            f"and note which document contributes each part.\n"
            f"Do NOT supplement with general knowledge or industry conventions.\n\n"
            f"{docs_ctx}\n\nQuestion: {safe_question}"
        )
    else:
        return _KDP.format(context=docs_ctx, question=safe_question)
