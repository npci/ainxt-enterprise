# SPDX-License-Identifier: Apache-2.0
# ============================================================
# DOCUMENT ROUTER — THE SINGLE AUTHORITY FOR DOC-GEN DECISIONS
#
# One function, resolve_doc_plan(), maps a raw user request (plus the uploaded
# attachments + the conversation's document memory) to a concrete DocPlan:
#
#     • intent            — generate | summarize | convert | extract | revise
#     • format            — pdf | docx | pptx | xlsx | csv | md | txt | None
#     • source_scope      — uploaded | chat | artifact | none
#     • target_artifact_id— which PRIOR doc a revise/convert acts on (or None)
#     • preserve          — for uploaded files: reproduce verbatim vs. generate new
#     • needs_clarification / clarify_question / clarify_options
#
# Why this exists: intent used to be decided in multiple disagreeing places —
# the React client, the worker, AND regex heuristics. That produced the
# "summarize became generate", "convert edited the wrong doc" bugs. Now the
# backend calls THIS once, and EVERY decision (intent, format, preserve,
# ambiguity) comes from the small LLM classifier — NO REGEX anywhere.
#
# Composes the EXISTING building blocks — it adds no new classifier logic:
#   models.doc_intent.classify()          → intent + format + preserve + confidence
#   services.doc_context.list_docs_for_chat / resolve_reference(strict=True)
#
# Fail-open: any error → a plain "generate" plan, never an exception. Document
# generation must never break because routing failed.
# ============================================================

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from core.logger import logger

# Below this confidence, a request that COULD act on a prior document but has no
# clear reference is treated as ambiguous → we ask the user rather than guess.
_CLARIFY_THRESHOLD = float(os.getenv("DOC_CLARIFY_THRESHOLD", "0.55") or "0.55")

# At/above this confidence, a hint the caller already computed (e.g. the
# gateway's CIL classification) is trusted enough to skip re-running
# models.doc_intent.classify() inside the worker — see the PERF note on
# resolve_doc_plan below. Set DOC_INTENT_TRUST_THRESHOLD=1.1 (or any value
# > 1.0) to disable this short-circuit without a deploy.
_INTENT_TRUST_THRESHOLD = float(os.getenv("DOC_INTENT_TRUST_THRESHOLD", "0.85") or "0.85")


@dataclass
class DocPlan:
    intent: str = "generate"
    format: Optional[str] = None
    source_scope: str = "none"           # uploaded | chat | artifact | none
    target_artifact_id: Optional[str] = None
    target_version: Optional[int] = None # latest version of the target artifact
    preserve: bool = False               # uploaded file: reproduce verbatim
    # compare intent: prior generated docs to diff (0-2). The worker loads each
    # one's content_md and adds it as a comparison source alongside any uploads.
    compare_prior_artifact_ids: list = field(default_factory=list)
    confidence: float = 0.5
    needs_clarification: bool = False
    clarify_question: str = ""
    clarify_options: list = field(default_factory=list)  # [{label, value}]
    reason: str = ""


def resolve_doc_plan(
    question: str,
    *,
    has_attachments: bool = False,
    attachment_filename: str = "",
    attachment_count: int = 0,
    chat_id: str = "",
    user_id: str = "",
    format_hint: Optional[str] = None,
    intent_hint: Optional[str] = None,
    hint_confidence: Optional[float] = None,
    has_chat_context: bool = False,
    source_scope_hint: Optional[str] = None,
) -> DocPlan:
    """Authoritative mapping of a request → DocPlan. Never raises.

    `intent_hint` / `format_hint` come from the client's cheap classifier; they
    are honoured only when the backend classifier agrees or is unsure — the
    backend always wins on genuine disagreement.

    PERF: `hint_confidence` is the caller's own classifier confidence for
    `intent_hint` (e.g. the gateway's CIL run, which already paid for one LLM
    call before the worker ever sees this request). When hint_confidence is
    at/above DOC_INTENT_TRUST_THRESHOLD (env, default 0.85) AND the request is
    the simple, unambiguous case — intent_hint == "generate", no attachment,
    no chat context to weigh — we skip the models.doc_intent.classify() call
    entirely and build the plan straight from the hint. This avoids paying for
    doc-intent classification TWICE per request (once in the gateway's CIL
    pass, again here) for the common "generate a fresh document" case.

    EXTENDED SHORT-CIRCUIT: When `source_scope_hint` is provided (the gateway
    already forwarded the CIL's source_scope), we trust the full gateway
    classification for ALL intents at high confidence — not just "generate".
    This eliminates the double-classification bug where the worker's second
    classify() call loses context and returns intent='none' for requests like
    "extract this into a pdf" (which have no attachment but do have chat scope).
    The gateway's CIL already paid for one LLM call with full context; the
    worker should not re-derive what the gateway already knows."""
    _hint_conf = float(hint_confidence) if hint_confidence is not None else None

    # Extended short-circuit: trust gateway's full classification (intent +
    # source_scope) for ALL non-attachment intents when confidence is high.
    # Requires source_scope_hint to be present — that's the signal that the
    # gateway forwarded its CIL result rather than a bare client hint.
    _valid_intents = ("generate", "summarize", "convert", "extract")
    if (
        intent_hint in _valid_intents
        and source_scope_hint is not None
        and not has_attachments
        and _hint_conf is not None
        and _hint_conf >= _INTENT_TRUST_THRESHOLD
    ):
        logger.info(
            f"[doc_router] trusting gateway CIL hint intent={intent_hint!r} "
            f"scope={source_scope_hint!r} confidence={_hint_conf:.2f} >= "
            f"{_INTENT_TRUST_THRESHOLD} — skipping models.doc_intent.classify() call"
        )
        return DocPlan(
            intent=intent_hint, format=format_hint,
            source_scope=source_scope_hint or "none",
            confidence=_hint_conf, reason="hint_trusted",
        )

    # Legacy short-circuit: generate with no context and no scope hint (old path).
    if (
        intent_hint == "generate"
        and not has_attachments
        and not has_chat_context
        and source_scope_hint is None
        and _hint_conf is not None
        and _hint_conf >= _INTENT_TRUST_THRESHOLD
    ):
        logger.info(
            f"[doc_router] trusting caller hint intent='generate' "
            f"confidence={_hint_conf:.2f} >= {_INTENT_TRUST_THRESHOLD} — "
            f"skipping models.doc_intent.classify() call"
        )
        return DocPlan(
            intent="generate", format=format_hint, source_scope="none",
            confidence=_hint_conf, reason="hint_trusted",
        )

    try:
        from models.doc_intent import classify, ACTION_INTENTS
        from services.doc_context import list_docs_for_chat, resolve_reference

        # Conversation document memory (newest first) — the basis for resolving
        # "that doc" and for deciding whether a follow-up is even possible.
        mem = list_docs_for_chat(chat_id, user_id) if chat_id else None
        mem_summary = mem.summary_for_llm() if mem else ""
        has_prior = bool(mem and not mem.is_empty())

        di = classify(
            question, has_attachments=has_attachments,
            doc_memory_summary=mem_summary, chat_id=chat_id or "", user_id=user_id,
            has_chat_context=has_chat_context,
        )
        intent = di.intent
        confidence = float(di.confidence or 0.5)

        # Trust a high-confidence client hint only when the backend is unsure
        # and the hint is a valid action intent — the backend otherwise wins.
        if (intent_hint in ACTION_INTENTS and confidence < 0.5
                and intent_hint != intent):
            logger.info(
                f"[doc_router] low backend confidence ({confidence:.2f}); "
                f"adopting client intent hint {intent_hint!r} over {intent!r}"
            )
            intent = intent_hint

        fmt = di.format or format_hint
        scope = di.source_scope or "none"
        target = di.target_artifact_id or None

        plan = DocPlan(
            intent=intent, format=fmt, source_scope=scope,
            target_artifact_id=target, confidence=confidence, reason=di.reason,
        )

        # ── Attachment reproduce-vs-generate ──
        # Decided by the small LLM classifier (di.preserve), NOT regex.
        if has_attachments:
            plan.preserve = bool(di.preserve)

        # ── COMPARE: diff exactly two documents into a comparison report ──
        # Sources can be uploaded files and/or prior generated docs. We need at
        # least TWO total. Count uploads (attachment_count) + resolve prior docs.
        if intent == "compare":
            _upload_n = max(0, int(attachment_count or 0))
            # If the caller flagged attachments but didn't pass a count, assume 1.
            if _upload_n == 0 and has_attachments:
                _upload_n = 1
            _needed = max(0, 2 - _upload_n)   # how many prior docs to pull in
            if _needed > 0 and has_prior and mem:
                # Take the most recent `_needed` prior artifacts as the other
                # side(s) of the comparison (newest first). The LLM ordering
                # inside the worker cites each source explicitly.
                plan.compare_prior_artifact_ids = [
                    d.artifact_id for d in mem.docs[:_needed]
                ]
                if plan.compare_prior_artifact_ids:
                    plan.source_scope = "artifact" if _upload_n == 0 else "uploaded"
            _total_sources = _upload_n + len(plan.compare_prior_artifact_ids)
            if _total_sources < 2:
                # Can't find two things to compare → ask the user to supply the
                # second document (upload another, or pick a prior one).
                plan.needs_clarification = True
                plan.clarify_question = (
                    "I need two documents to compare. Upload the second file, "
                    "or pick a document to compare against:"
                )
                plan.clarify_options = [
                    {"label": f'{d.title} ({d.format}, v{d.version})',
                     "value": d.artifact_id}
                    for d in (mem.docs[:5] if mem else [])
                ]
                plan.clarify_options.append(
                    {"label": "None — I'll upload another file", "value": "__new__"}
                )
                plan.reason = "compare_needs_two"
            return plan

        # ── Resolve which prior doc a follow-up acts on ──
        # revise/convert/summarize that target an existing artifact need a
        # concrete artifact_id. Try the classifier's pick, then strict resolve.
        _needs_artifact = intent in ("revise", "convert") or (
            intent == "summarize" and not has_attachments and not has_chat_context
        )
        if _needs_artifact and has_prior:
            if not target:
                ref = resolve_reference(chat_id, user_id, question,
                                        memory=mem, strict=True)
                if ref:
                    plan.target_artifact_id = ref.artifact_id
                    plan.target_version = int(ref.version or 1)
                    plan.source_scope = "artifact"
            # Fill target_version from the in-memory DocMemory (no extra query)
            # whether the id came from the classifier or strict resolution.
            if plan.target_artifact_id and plan.target_version is None and mem:
                for _d in mem.docs:
                    if _d.artifact_id == plan.target_artifact_id:
                        plan.target_version = int(_d.version or 1)
                        break
            # Still unresolved AND more than one candidate → ask the user.
            if not plan.target_artifact_id and mem and len(mem.docs) > 1:
                plan.needs_clarification = True
                plan.clarify_question = (
                    f"Which document should I {intent}?"
                )
                plan.clarify_options = [
                    {"label": f'{d.title} ({d.format}, v{d.version})',
                     "value": d.artifact_id}
                    for d in mem.docs[:5]
                ]
                plan.clarify_options.append(
                    {"label": "None — create a new document", "value": "__new__"}
                )
                plan.reason = "ambiguous_reference"
                return plan

        # ── Low-confidence new-vs-existing ambiguity ──
        # Only ask when the classifier itself signalled the request targets a
        # prior doc (source_scope=artifact) but was UNSURE. A clean "generate X
        # about Y" (scope != artifact) never clarifies. LLM-derived, no regex.
        _references_prior = (scope == "artifact") or bool(target)
        if (not plan.needs_clarification and has_prior
                and confidence < _CLARIFY_THRESHOLD
                and intent in ("revise", "convert", "summarize", "generate")
                and mem and len(mem.docs) >= 1
                and _references_prior):
            latest = mem.latest()
            plan.needs_clarification = True
            plan.clarify_question = (
                "Did you want a NEW document, or to work on one you made earlier?"
            )
            plan.clarify_options = [
                {"label": f'Edit "{latest.title}" ({latest.format})',
                 "value": latest.artifact_id},
                {"label": "Create a new document", "value": "__new__"},
            ]
            plan.reason = "low_confidence"

        return plan

    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[doc_router] resolve_doc_plan failed, defaulting to generate: {exc}")
        return DocPlan(intent="generate", format=format_hint, confidence=0.5,
                       reason="error_fallback")
