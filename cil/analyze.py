# SPDX-License-Identifier: Apache-2.0
# ============================================================
# CIL.analyze — MODEL-ONLY turn understanding (ZERO REGEX)
# ============================================================
#
# docs/architecture/04 §4.3 / 05 §5.1. The CIL's understanding step. It produces
# ONE typed ConversationState per turn using a MODEL (cil/intent.py) — NOT
# keyword/regex heuristics. This is what makes the layer an "intelligence" layer.
#
# HARD RULE: analyze() NEVER raises AND NEVER uses regex. Any failure of the
# intent model → a SAFE STATIC DEFAULT ConversationState (medium/general/chat/
# no-tools/no-clarify), i.e. a plain dataclass with default values. That default
# is the ONLY fallback — there is deliberately no keyword/lexical path here.
#
# This module stays importable in a bare env (the model call is lazy inside
# cil.intent and fails safe to None).
# ============================================================

from __future__ import annotations

import os
import time
from typing import Optional

from cil.state import ConversationState

# Model-intent is the default (and only) understanding path. Set
# CIL_MODEL_INTENT=false to force the safe static default without any model call
# (e.g. to remove all per-turn model latency) — this does NOT re-enable regex;
# regex intent detection has been removed from the chat path entirely.
_CIL_MODEL_INTENT = os.getenv("CIL_MODEL_INTENT", "true").lower() == "true"


def analyze(question: str, *, rag_mode: str = "off",
            skip_model: bool = False,
            has_attachments: bool = False,
            doc_memory_summary: str = "",
            has_chat_context: bool = False,
            recent_turns: Optional[list] = None,
            include_doc_intent: bool = True,
            include_img_intent: bool = True,
            include_vid_intent: bool = True,
            attachment_kinds: Optional[list] = None) -> ConversationState:
    """Produce a ConversationState for one turn. Never raises. Regex-free.

    - Normal path: the model classifier (cil/intent.classify) understands the
      turn and fills the state.
    - skip_model=True (e.g. the user forced a specific model in the UI, so
      routing is already decided) OR CIL_MODEL_INTENT=false OR any model failure
      → a SAFE STATIC DEFAULT ConversationState (today's medium/general posture).
      There is NO regex/keyword fallback by design.
    - include_doc_intent: when False, the CIL prompt omits doc-intent schema
      fields, saving tokens for plain-chat requests. Gateway sets this based on
      a doc-signal regex pre-check (format noun, slash command, attachment, prior doc).
    - include_img_intent: when False, the CIL prompt omits img-intent schema
      fields. Gateway sets this based on an image-signal pre-check (image keyword
      in text, or an image attachment present). Kept independent of
      include_doc_intent so a pure image request gets img-intent without doc-intent.
    - include_vid_intent: when False, the CIL prompt omits vid-intent schema
      fields. Gateway sets this based on a video-signal pre-check (video keyword
      in text). Kept independent of the other gates so a pure video request gets
      vid-intent without doc/img-intent schema overhead.
    - attachment_kinds: the `kind` (e.g. "image"/"document") of each attachment
      referenced by attachment_ids, so the classifier prompt can say
      "Attachments present: yes (kind: image)" instead of a bare yes/no. This
      is metadata ONLY — never the attachment's parsed_text / image_description
      / image_caption content, which the gateway injects separately into the
      main LLM / image-generation prompts, not through the intent classifier.
    """
    t0 = time.time()
    q = question or ""

    _ui = None
    if _CIL_MODEL_INTENT and not skip_model and q.strip():
        try:
            from cil.intent import classify as _classify, to_conversation_state
            _ui = _classify(
                q, rag_mode=rag_mode,
                has_attachments=has_attachments,
                doc_memory_summary=doc_memory_summary,
                has_chat_context=has_chat_context,
                recent_turns=recent_turns,
                include_doc_intent=include_doc_intent,
                include_img_intent=include_img_intent,
                include_vid_intent=include_vid_intent,
                attachment_kinds=attachment_kinds,
            )
            if _ui is not None:
                st = to_conversation_state(_ui, rag_mode=rag_mode)
                st.analyze_ms = round((time.time() - t0) * 1000, 2)
                return st
        except Exception:  # noqa: BLE001 — understanding must never raise
            _ui = None

    # ── Safe static default (the ONLY fallback — no regex) ────────────
    # Equals today's implicit posture: medium complexity, general domain, plain
    # chat, no tools, no clarification. Reached when the model is off/unavailable
    # or intentionally skipped.
    st = ConversationState()
    st.intent_source = "default"
    st.signal_sources = ["default"]
    st.analyze_ms = round((time.time() - t0) * 1000, 2)
    return st
