# SPDX-License-Identifier: Apache-2.0
# ============================================================
# CHAT WORKER — rq job that runs the full chat pipeline and
# publishes tokens to a Redis Stream for SSE consumption.
#
# Flow:
#   1. Compliance gate on input
#   2. PII mask
#   3. Inject conversation history
#   4. Classify + domain detect
#   5. Rewrite query (code domain only)
#   6. Cache check (skip LLM if hit)
#   7. RAG retrieval (repo-scoped or docs_kb)
#   8. Stream tokens via model_router.stream()
#   9. Output compliance check per token
#  10. XADD each token to chat:stream:{job_id}
#  11. XADD __done__ or __error__ sentinel (always, in finally)
#  12. EXPIRE stream 1h
#
# Hanging prevention:
#   - try/finally guarantees sentinel delivery even on crash
#   - EXPIRE sets TTL so orphaned streams self-clean
#   - RQ job timeout=120s kills runaway jobs
# ============================================================

import json
import os
import re
import time
import uuid as _uuid_mod

from core.config import RDB_CACHE, RDB_QUEUE, RDB_STREAM
from core.kv import get_kv

from core.logger import logger

# ── Document generation — slash command routing ────────────────────────────────
# Each supported output format has its own slash command prefix.
# No heuristic pattern matching — commands are the single source of truth.
# This mirrors how /image routes to image generation.
#
# Supported commands:
#   /pdf                    → PDF document
#   /docx | /doc | /word    → Word document
#   /xlsx | /excel          → Excel spreadsheet
#   /csv                    → CSV file  (generated as xlsx, labelled .csv by frontend)
#   /txt  | /text           → Plain text document
#   /md                     → Markdown document
#   /ppt  | /pptx | /pptagent → PowerPoint presentation
#   /convert <format>       → Convert uploaded file to target format
#                             e.g. "/convert pdf", "/convert docx"

_PPT_COMMAND_RE  = re.compile(r"^/(?:ppt|pptx|pptagent)\b",  re.IGNORECASE)
_PDF_COMMAND_RE  = re.compile(r"^/pdf\b",                     re.IGNORECASE)
_DOCX_COMMAND_RE = re.compile(r"^/(?:docx?|word)\b",         re.IGNORECASE)
_XLSX_COMMAND_RE = re.compile(r"^/(?:xlsx?|excel)\b",        re.IGNORECASE)
_CSV_COMMAND_RE  = re.compile(r"^/csv\b",                    re.IGNORECASE)
_TXT_COMMAND_RE  = re.compile(r"^/(?:txt|text)\b",           re.IGNORECASE)
_MD_COMMAND_RE   = re.compile(r"^/md\b",                     re.IGNORECASE)

# /convert <format> — explicit file conversion command.
# The target format is the first word after /convert.
# e.g. "/convert pdf", "/convert docx", "/convert xlsx"
_CONVERT_COMMAND_RE = re.compile(r"^/convert\b", re.IGNORECASE)

# ── File conversion intent (kept for legacy heuristic path) ──────────────────
# Used only when user uploads a file without /convert and says "convert to pdf".
_DOC_CONVERT_RE = re.compile(
    r"\b(convert|transform|export|change|turn)\b.{0,60}\b(pdf|docx?|word|doc)\b"
    r"|\b(pdf|docx?|word)\b.{0,30}\b(version|format|copy|file)\b"
    r"|\bgive\s+(me\s+)?(a\s+)?(pdf|docx?|word)\b"
    r"|\b(to|as|into)\s+(pdf|docx?|word\s+doc(ument)?)\b",
    re.IGNORECASE | re.DOTALL,
)

# ── Follow-up confirmations (short replies after a prior doc command) ─────────
# Only triggers when history already contains an explicit slash command.
_DOC_FOLLOWUP_RE = re.compile(
    r"^\s*(yes|ok|okay|sure|go ahead|proceed|do it|use that|option\s+[a-z1-9]"
    r"|go with|that one|sounds good|perfect|great|please do|alright|yep|yup"
    r"|confirmed|confirm|that works|looks good)\b",
    re.IGNORECASE,
)


def _is_doc_intent(text: str, has_attachment: bool = False) -> bool:
    """
    Returns True ONLY when the user typed an explicit slash command for a
    document format.  No heuristic pattern matching — commands are the
    single source of truth, exactly like /image routes to image generation.
    """
    result = bool(
        _PPT_COMMAND_RE.search(text)
        or _PDF_COMMAND_RE.search(text)
        or _DOCX_COMMAND_RE.search(text)
        or _XLSX_COMMAND_RE.search(text)
        or _CSV_COMMAND_RE.search(text)
        or _TXT_COMMAND_RE.search(text)
        or _MD_COMMAND_RE.search(text)
        or _CONVERT_COMMAND_RE.search(text)
    )
    if result:
        logger.info(
            f"chat_worker routing: route=doc method=command_prefix "
            f"text_preview={text[:80]!r}"
        )
    return result


def _is_doc_followup(question: str, question_with_history: str) -> bool:
    """True only when current message is a short confirmation AND prior history had doc intent."""
    if len(question.strip()) > 120:
        return False
    if not _DOC_FOLLOWUP_RE.search(question.strip()):
        return False
    # Check only the history portion, not the current question
    history_only = question_with_history
    if "[Current question]" in question_with_history:
        history_only = question_with_history.split("[Current question]")[0]
    return _is_doc_intent(history_only)


_DOC_FORMAT_RE = {
    "pptx": re.compile(r"\b(powerpoint|ppt|pptx|presentation|slides|slide.?deck)\b|\.pptx?", re.IGNORECASE),
    "docx": re.compile(r"\b(word|docx)\b|\.docx?", re.IGNORECASE),
    "pdf":  re.compile(r"\bpdf\b|\.pdf", re.IGNORECASE),
    # CSV must be matched BEFORE xlsx (dict order = match priority). A ".csv"
    # request is a flat CSV file, NOT an Excel workbook — routing it to "xlsx"
    # forced every CSV through the spreadsheet/test-data pipeline and skipped
    # the plain-text CSV handling in doc_worker. Keep "excel"/"spreadsheet"/
    # "xlsx" on the xlsx branch.
    "csv":  re.compile(r"\bcsv\b|\.csv\b", re.IGNORECASE),
    "xlsx": re.compile(r"\b(excel|xlsx|spreadsheet)\b|\.xlsx?", re.IGNORECASE),
    "md":   re.compile(r"\bmarkdown\b|\.md\b", re.IGNORECASE),
    "txt":  re.compile(r"\b(text.?file|plain.?text)\b|\.txt\b", re.IGNORECASE),
}

# _DOC_STRUCT_PROMPT is intentionally removed.
# chat_worker now delegates to doc_worker's rich prompt builders
# (_build_docx_prompt / _build_pdf_prompt) for Claude-quality output.


def _detect_doc_format(text: str) -> str:
    for fmt, pattern in _DOC_FORMAT_RE.items():
        if pattern.search(text):
            return fmt
    return "pdf"


def _is_convert_intent(text: str) -> bool:
    """Return True when the user wants to convert an uploaded file to another format."""
    return bool(_DOC_CONVERT_RE.search(text))


# ── Doc edit follow-up detector ────────────────────────────────────────────────
# After a document has been generated and md:session:{chat_id} persists, a
# free-form follow-up like "in upi_payment_architecture.docx, fix the title"
# should re-route to the edit pipeline instead of falling through to a normal
# chat reply.
#
# Match strategy (true if ANY of the following):
#   A. The message contains the persisted filename (with or without extension).
#   B. The message contains the persisted title (case-insensitive substring).
#   C. The message contains the persisted format extension AND an edit verb.
#   D. The message uses an edit verb + a doc reference noun (this/that/it/
#      doc/document/file/pdf/docx/word/...) within the same sentence.
#
# Heuristic — no LLM call. Keeps false-positive risk low by requiring either an
# explicit token match OR (edit verb + doc noun) together. Confirmation-only
# replies ("yes", "ok") are excluded — those go through _is_doc_followup.
_EDIT_VERB_RE = re.compile(
    r"\b(?:edit|update|revise|rewrite|rework|reword|amend|adjust|tweak|"
    r"change|modify|fix|correct|correction|improve|enhance|polish|refine|"
    r"add(?:\s+a)?|insert|append|include|extend|expand|"
    r"remove|delete|drop|strip|omit|"
    r"replace|swap|rename|reorder|reorganise|reorganize|restructure|"
    r"shorten|lengthen|tighten|simplify|clarify|elaborate|"
    r"regenerate|redo|redraft|recreate|"
    r"make\s+it|turn\s+it\s+into)\b",
    re.IGNORECASE,
)
_DOC_NOUN_RE = re.compile(
    r"\b(?:this|that|it|the\s+(?:doc(?:ument)?|file|report|paper|deck|"
    r"slide(?:s|deck)?|presentation|spreadsheet)|"
    r"doc(?:ument)?|file|report|deck|presentation|pdf|docx?|word|"
    r"pptx?|ppt|xlsx?|excel|markdown|md\b)\b",
    re.IGNORECASE,
)


def _versioned_basename(prev_doc_name: str) -> str:
    """
    Given the filename of a previously generated doc, return a NEW base name
    (no extension) for the updated/follow-up revision, so each revision is
    distinct instead of overwriting with the same name:
      "javascript-closures.docx"          -> "javascript-closures-updated"
      "javascript-closures-updated.docx"  -> "javascript-closures-v2"
      "javascript-closures-v2.pdf"        -> "javascript-closures-v3"
    """
    base = os.path.splitext(os.path.basename((prev_doc_name or "").strip()))[0]
    if not base:
        return "generated-document-updated"
    m = re.search(r"^(.*)-v(\d+)$", base, re.IGNORECASE)
    if m:
        return f"{m.group(1)}-v{int(m.group(2)) + 1}"
    m = re.search(r"^(.*)-updated$", base, re.IGNORECASE)
    if m:
        return f"{m.group(1)}-v2"
    return f"{base}-updated"


def _title_from_sections(sections: list | None) -> str:
    """
    Derive a content-based title from structured sections (first meaningful
    heading), used when the LLM returns no title — so the filename reflects the
    document content rather than the generic chat request.
    """
    for _s in (sections or []):
        if not isinstance(_s, dict):
            continue
        _h = (_s.get("heading") or _s.get("title") or "").strip()
        if _h and _h.lower() not in ("introduction", "overview", "summary", "content"):
            return _h
    for _s in (sections or []):
        if isinstance(_s, dict):
            _h = (_s.get("heading") or _s.get("title") or "").strip()
            if _h:
                return _h
    return ""


def _is_doc_edit_followup(question: str, chat_id: str) -> tuple[bool, dict | None]:
    """
    Decide whether a free-form follow-up should be routed to the doc-edit
    pipeline.

    Returns (is_edit, session_dict).  session_dict is the parsed md session
    when is_edit is True, else None.  Never raises — any Redis/JSON error
    returns (False, None) so the caller can fall through to normal chat.
    """
    if not chat_id:
        return (False, None)

    text = (question or "").strip()
    if not text:
        return (False, None)

    # Skip very short confirmation-only replies — those are handled by
    # _is_doc_followup against prior history, not here.
    if len(text) < 6:
        return (False, None)

    try:
        _r_md = get_kv(RDB_STREAM, decode_responses=True)
        raw = _r_md.get(f"md:session:{chat_id}")
        if not raw:
            return (False, None)
        session = json.loads(raw)
    except Exception as _se:
        logger.warning(f"chat_worker: edit-followup session load failed: {_se}")
        return (False, None)

    doc = (session or {}).get("document") or {}
    title    = (doc.get("title")    or "").strip()
    filename = (doc.get("filename") or "").strip()
    ext      = (doc.get("original_format") or "").strip().lower()
    if not (title or filename):
        return (False, None)

    text_lower = text.lower()
    has_edit_verb = bool(_EDIT_VERB_RE.search(text))
    has_doc_noun  = bool(_DOC_NOUN_RE.search(text))

    # (A) explicit filename mention (or its stem)
    fn_lower = filename.lower()
    fn_stem  = fn_lower.rsplit(".", 1)[0] if "." in fn_lower else fn_lower
    if fn_lower and fn_lower in text_lower:
        logger.info(
            f"chat_worker routing: route=doc_edit method=filename_match "
            f"chat_id={chat_id!r} filename={filename!r}"
        )
        return (True, session)
    # Stem match needs at least 6 chars to avoid trivial collisions
    # ("doc", "file") and must be accompanied by an edit signal.
    if fn_stem and len(fn_stem) >= 6 and fn_stem in text_lower and has_edit_verb:
        logger.info(
            f"chat_worker routing: route=doc_edit method=filename_stem "
            f"chat_id={chat_id!r} stem={fn_stem!r}"
        )
        return (True, session)

    # (B) explicit title mention (only count titles long enough to be
    # distinctive — short titles like "UPI" would over-match)
    title_lower = title.lower()
    if title_lower and len(title_lower) >= 6 and title_lower in text_lower:
        logger.info(
            f"chat_worker routing: route=doc_edit method=title_match "
            f"chat_id={chat_id!r} title={title!r}"
        )
        return (True, session)

    # (C) format extension mentioned with an edit verb
    if ext and re.search(rf"\b{re.escape(ext)}\b", text_lower) and has_edit_verb:
        logger.info(
            f"chat_worker routing: route=doc_edit method=ext_plus_verb "
            f"chat_id={chat_id!r} ext={ext!r}"
        )
        return (True, session)

    # (D) generic edit verb + doc-reference noun
    if has_edit_verb and has_doc_noun:
        logger.info(
            f"chat_worker routing: route=doc_edit method=verb_plus_noun "
            f"chat_id={chat_id!r}"
        )
        return (True, session)

    return (False, None)


def _route_doc_edit_followup(
        question: str,
        user_id: str,
        chat_id: str,
        stream_key: str,
) -> bool:
    """
    Top-level gate for plain-language document edit follow-ups.

    Called BEFORE normal chat when no slash command was typed.
    Checks if an md:session exists for this chat AND the message
    looks like an edit request (edit verb + doc noun).

    Returns True (and enqueues the edit job) if this is an edit follow-up.
    Returns False (no-op) otherwise — caller falls through to normal chat.

    SAFE: Never raises. On any error returns False so normal chat runs.
    Delegates to _handle_md_generation which detects mode="edit" from the
    existing session and preserves original_format + content-derived title.
    """
    if not chat_id:
        return False
    try:
        is_edit, session = _is_doc_edit_followup(question, chat_id)
        if not is_edit or not session:
            return False
        logger.info(
            f"chat_worker routing: route=doc_edit_followup method=plain_language "
            f"chat_id={chat_id!r} question_preview={question[:60]!r}"
        )
        # Delegate to the existing MD generation handler — it reads the session,
        # sets mode="edit", and enqueues generate_md_job with the correct
        # original_format and content-derived title.
        return _handle_md_generation(question, user_id, chat_id, stream_key)
    except Exception as _exc:
        logger.warning(
            f"chat_worker: _route_doc_edit_followup failed (falling through to chat): {_exc}"
        )
        return False


def _publish_doc_completion(
        *,
        stream_key: str,
        chat_id: str,
        user_id: str,
        question: str,
        answer: str,
        meta: dict | None,
        job_id: str,
) -> None:
    """Publish doc-flow completion metadata and persist the chat turn like normal chat."""
    _in_tok = int((meta or {}).get("in_tok", 0) or 0)
    _out_tok = int((meta or {}).get("out_tok", 0) or 0)
    _cost = float((meta or {}).get("cost_usd", (meta or {}).get("cost", 0.0)) or 0.0)
    _meta = {
        "model": (meta or {}).get("model", "doc_generator"),
        "tokens": _in_tok + _out_tok,
        "in_tok": _in_tok,
        "out_tok": _out_tok,
        "cost": _cost,
        "latency": float((meta or {}).get("latency", 0.0) or 0.0),
        "confidence": 1.0,
        "chunk_count": 0,
        "source": "document_generation",
    }

    try:
        from core.kafka_producer import produce, TOPIC_CHAT_HISTORY, TOPIC_METRICS
        produce(TOPIC_CHAT_HISTORY, {
            "chat_id":   chat_id,
            "user_id":   user_id,
            "question":  question,
            "answer":    answer,
            "model":     _meta.get("model", "doc_generator"),
            "in_tok":    _meta.get("in_tok", 0),
            "out_tok":   _meta.get("out_tok", 0),
            "latency":   _meta.get("latency", 0.0),
            "cost":      _meta.get("cost", 0.0),
            "job_id":    job_id,
        }, key=chat_id)
        produce(TOPIC_METRICS, {
            "user_id":           user_id,
            "model":             _meta.get("model", "doc_generator"),
            "prompt_tokens":     _meta.get("in_tok", 0),
            "completion_tokens": _meta.get("out_tok", 0),
            "total_tokens":      _meta.get("tokens", 0),
            "cost_usd":          _meta.get("cost", 0.0),
        }, key=user_id)
    except Exception as exc:
        logger.warning(f"chat_worker: doc-flow kafka persist failed: {exc}")

    _publish_done(stream_key, meta=_meta)



def _handle_doc_conversion(
        question: str,
        user_id: str,
        chat_id: str,
        stream_key: str,
        attachment_ids: list,
        forced_target_format: str | None = None,
) -> bool:
    """
    Convert an uploaded file to the target format.

    forced_target_format: when set (from /convert <fmt> command), skips
    heuristic detection and uses this format directly.
    When None, detects target format from the user's natural-language prompt.
    """
    from tools.doc_generator import FORMAT_EXTENSIONS

    # ── Resolve attachment metadata from DB ───────────────────────────────────
    try:
        from db.database import SessionLocal
        from db.models import ChatAttachment
        db = SessionLocal()
        try:
            att = db.query(ChatAttachment).filter(
                ChatAttachment.id == attachment_ids[0]
            ).first()
        finally:
            db.close()
    except Exception as exc:
        logger.error(f"chat_worker: convert — attachment lookup failed: {exc}")
        _publish_chunk(stream_key, f"\n⚠️ Could not read the uploaded file: {exc}")
        _publish_done(stream_key)
        return True

    if not att:
        _publish_chunk(stream_key, "\n⚠️ Uploaded file not found. Please re-upload and try again.")
        _publish_done(stream_key)
        return True

    source_filename = att.file_name or "document"
    source_ext      = (att.file_type or "").lower().strip(".")
    storage_path    = att.storage_path or ""

    # ── Determine target format ───────────────────────────────────────────────
    if forced_target_format:
        # /convert <format> command — use exactly what the user specified
        target_format = forced_target_format
        logger.info(f"chat_worker: convert — forced_target_format={target_format!r} from /convert command")
    else:
        # Natural-language path — detect from prompt text
        target_format = _detect_doc_format(question)
        # If detection falls back to "pdf" but source is already pdf, flip to docx
        if target_format == "pdf" and source_ext == "pdf":
            target_format = "docx"

    ext      = FORMAT_EXTENSIONS.get(target_format, target_format)
    job_id   = str(_uuid_mod.uuid4())
    filename = f"{source_filename.rsplit('.', 1)[0]}.{ext}"

    payload = {
        "job_id":          job_id,
        "storage_path":    storage_path,
        "source_filename": source_filename,
        "source_ext":      source_ext,
        "target_format":   target_format,
        "user_id":         user_id,
        "chat_id":         chat_id,
    }

    try:
        from core.job_queue import enqueue_job, Q_DOC
        enqueue_job("workers.doc_worker.convert_doc_job", payload,
                    queue_name=Q_DOC, timeout=300, retry_count=0)
    except Exception as exc:
        logger.error(f"chat_worker: convert job enqueue failed: {exc}")
        _publish_chunk(stream_key, f"\n⚠️ Conversion failed to start: {exc}")
        _publish_done(stream_key)
        return True

    fmt_label   = ext.upper()
    answer_text = (
        f"Converting **{source_filename}** to **{fmt_label}**.\n\n"
        f"The download button will appear below once it's ready.\n\n"
        f"[DOCJOB:{job_id}:{ext}:{filename}]"
    )
    _publish_chunk(stream_key, answer_text)
    _publish_doc_completion(
        stream_key=stream_key,
        chat_id=chat_id,
        user_id=user_id,
        question=question,
        answer=answer_text,
        meta=None,
        job_id=job_id,
    )
    logger.info(f"chat_worker: convert job {job_id} enqueued — {source_filename} → {filename}")
    return True


def _handle_doc_generation(
        question: str,
        question_with_history: str,
        context_text: str,
        user_id: str,
        chat_id: str,
        stream_key: str,
        attachment_ids: list | None = None,
) -> bool:
    """
    Detect doc intent → structure content via LLM → submit async job →
    publish [DOCJOB:job_id:format:filename] marker to stream.
    Returns True if this was a doc request (caller should skip normal generation).

    Handles three cases:
    - File conversion: user uploads a file + says "convert to PDF / Word"
    - Direct request: "generate a PPT for AiNxt growth"
    - Follow-up reply: "go with option A" after the LLM asked clarifying questions
    """

    # ── /convert command — explicit file conversion ───────────────────────────
    # Usage: /convert <format>  e.g. "/convert pdf", "/convert docx"
    # The target format is read from the command itself; no heuristic needed.
    # Falls back to legacy heuristic (_is_convert_intent) when no /convert command
    # but user has uploaded a file and says "convert to pdf" etc.
    if _CONVERT_COMMAND_RE.search(question):
        # Extract target format from command: "/convert pdf ..." → "pdf"
        _conv_m = re.match(r"^/convert\s+(\w+)", question.strip(), re.IGNORECASE)
        _conv_fmt = _conv_m.group(1).lower() if _conv_m else "pdf"
        # Normalise aliases
        _conv_fmt = {
            "doc": "docx", "word": "docx",
            "xls": "xlsx", "excel": "xlsx",
            "ppt": "pptx", "powerpoint": "pptx",
            "text": "txt",
        }.get(_conv_fmt, _conv_fmt)
        if attachment_ids:
            return _handle_doc_conversion(
                question, user_id, chat_id, stream_key, attachment_ids,
                forced_target_format=_conv_fmt,
            )
        else:
            # No file uploaded — tell user to upload first
            _publish_chunk(
                stream_key,
                "⚠️ Please upload a file first, then use `/convert <format>` to convert it.\n\n"
                "Example: upload a `.docx` file, then type `/convert pdf`",
            )
            _publish_done(stream_key)
            return True

    # ── Legacy heuristic conversion path (no /convert command) ───────────────
    # Triggered when user uploads a file AND uses natural language like "convert to PDF".
    if attachment_ids and _is_convert_intent(question):
        return _handle_doc_conversion(
            question, user_id, chat_id, stream_key, attachment_ids
        )

    has_attachment  = bool(attachment_ids)
    current_matches = _is_doc_intent(question, has_attachment=has_attachment)
    followup_match  = (
            not current_matches
            and _is_doc_followup(question, question_with_history or "")
    )

    if not current_matches and not followup_match:
        return False

    # ── Detect which slash command was used and strip the prefix ─────────────
    effective_question = question_with_history if followup_match else question

    if _PPT_COMMAND_RE.search(question):
        effective_question = re.sub(r"^/(?:ppt|pptx|pptagent)\s*", "", effective_question, flags=re.IGNORECASE)
        logger.info(f"chat_worker routing: route=ppt method=command_prefix stripped={effective_question[:50]!r}")
        return _handle_pptx_generation(
            effective_question, question, context_text,
            user_id, chat_id, stream_key,
        )

    if _PDF_COMMAND_RE.search(question):
        effective_question = re.sub(r"^/pdf\s*", "", effective_question, flags=re.IGNORECASE)
        fmt = "pdf"
    elif _DOCX_COMMAND_RE.search(question):
        effective_question = re.sub(r"^/(?:docx?|word)\s*", "", effective_question, flags=re.IGNORECASE)
        fmt = "docx"
    elif _XLSX_COMMAND_RE.search(question):
        effective_question = re.sub(r"^/(?:xlsx?|excel)\s*", "", effective_question, flags=re.IGNORECASE)
        fmt = "xlsx"
    elif _CSV_COMMAND_RE.search(question):
        effective_question = re.sub(r"^/csv\s*", "", effective_question, flags=re.IGNORECASE)
        fmt = "csv"    # flat CSV file (text/csv) — NOT an Excel workbook
    elif _TXT_COMMAND_RE.search(question):
        effective_question = re.sub(r"^/(?:txt|text)\s*", "", effective_question, flags=re.IGNORECASE)
        fmt = "txt"
    elif _MD_COMMAND_RE.search(question):
        effective_question = re.sub(r"^/md\s*", "", effective_question, flags=re.IGNORECASE)
        fmt = "md"
    else:
        # Followup reply — detect format from prior history
        fmt = _detect_doc_format(question_with_history or question)

    logger.info(f"chat_worker routing: route={fmt} method=command_prefix text_length={len(question)}")

    # PPTX: rich structured flow with template picker
    if fmt in ("pptx", "ppt", "powerpoint", "presentation", "slides"):
        return _handle_pptx_generation(
            effective_question, question, context_text,
            user_id, chat_id, stream_key,
            attachment_ids=attachment_ids,
        )

    # Markdown: dedicated agent with session-based continuation support
    if fmt == "md":
        return _handle_md_generation(
            effective_question, user_id, chat_id, stream_key,
            attachment_ids=attachment_ids,
            chat_context=context_text or "",
        )

    # Other formats: rich structure using the same skill-aligned prompt as doc_worker
    # ── Import prompt builders and helpers from doc_worker ───────────────────
    # NOTE: _build_docx_prompt / _build_pdf_prompt are DISABLED — they injected
    # a forced "Executive Summary" + "Closing Insight" callout into every doc,
    # which produced the off-brand legacy template on the server. Replaced with
    # _build_freeform_prompt (skill-aligned, mirrors the local test runner)
    # so server DOCX/PDF output matches the platform skill template.
    from workers.doc_worker import (
        _build_freeform_prompt, _build_xlsx_prompt, _build_csv_prompt,
        _build_preservation_prompt,
        _derive_title_from_question, _sanitize_llm_title,
    )
    from tools.doc_generator import smart_filename, FORMAT_EXTENSIONS

    _doc_llm_meta: dict = {}
    domain: str | None = None

    # ── Fetch parsed content from uploaded attachments ────────────────────────
    # When the user uploads a file and asks to convert/reproduce it, we fetch
    # the already-parsed text (stored at upload time by document_parser.py).
    #
    # IMPORTANT: Always run parsed text through the compliance engine's redactor
    # before injecting into any LLM prompt. The LLM proxy has its own compliance
    # filter — sending raw PAN/card/financial data causes it to return
    # "Request blocked due to PCI violation" instead of JSON, breaking doc gen.
    # Using redacted_text (PAN/RRN/ARD values masked) is safe and still preserves
    # all structural content (headings, tables, section text) needed for conversion.
    _attachment_context = ""
    _first_attachment_name = ""
    _first_attachment_parsed = ""
    if attachment_ids:
        try:
            from db.database import SessionLocal
            from db.models import ChatAttachment
            from agents.compliance_engine import compliance_engine as _ce_att
            _adb = SessionLocal()
            try:
                for _i, _aid in enumerate(attachment_ids[:3]):
                    _att = _adb.query(ChatAttachment).filter(
                        ChatAttachment.id == _aid
                    ).first()
                    if _att and _att.parsed_text:
                        _raw_parsed = (_att.parsed_text or "")
                        # Redact PCI/PII before sending to LLM — prevents proxy block.
                        # Gated by COMPLIANCE_SCAN_TOOL_RESULTS (file-read guard); OFF
                        # by default → attachment content used raw.
                        from core.config import COMPLIANCE_SCAN_TOOL_RESULTS
                        if not COMPLIANCE_SCAN_TOOL_RESULTS:
                            _parsed = _raw_parsed
                        else:
                            try:
                                _redact_result = _ce_att.validate_input(_raw_parsed[:12000])
                                _parsed = _redact_result.get("redacted_text") or _raw_parsed
                                if _redact_result.get("was_redacted"):
                                    logger.info(
                                        f"chat_worker: redacted attachment content for LLM "
                                        f"types={_redact_result.get('redacted_types')} "
                                        f"file={_att.file_name!r}"
                                    )
                            except Exception:
                                _parsed = _raw_parsed  # fail-open: use raw if redactor fails
                        # Keep first attachment's redacted parsed text for preservation prompt
                        if _i == 0:
                            _first_attachment_name   = _att.file_name or ""
                            _first_attachment_parsed = _parsed
                        # Truncate large files to keep prompt manageable (first 8000 chars)
                        _snippet = _parsed[:8000]
                        _attachment_context += (
                            f"\n\n--- Uploaded file: {_att.file_name} ---\n{_snippet}"
                        )
                        if len(_parsed) > 8000:
                            _attachment_context += "\n[... file truncated ...]"
            finally:
                _adb.close()
        except Exception as _att_err:
            logger.warning(f"chat_worker: attachment context fetch failed: {_att_err}")

    # Build the effective question with attachment content prepended — used
    # by the xlsx/csv generation branches below. (This assignment previously
    # existed but was dropped in a refactor while its two call sites remained,
    # causing a silent NameError → the uploaded file's content was never used
    # for xlsx/csv generation. Restored here.)
    _question_with_attachment = effective_question
    if _attachment_context:
        _question_with_attachment = (
            f"The user has uploaded the following file(s):{_attachment_context}"
            f"\n\nUser request: {effective_question}"
        )

    # ── Choose prompt strategy based on whether a file was uploaded ───────────
    #
    # PRESERVATION path: user uploaded a file AND the intent is to reproduce/
    #   convert that specific file (e.g. "generate a PDF of this DOCX",
    #   "convert this to Word", "preserve all formatting").
    #   → Use _build_preservation_prompt: LLM maps parsed content to sections
    #     WITHOUT inventing anything.
    #
    # GENERATION path: no file uploaded, OR user uploaded a file just as
    #   context/data source and wants NEW content generated from it
    #   (e.g. "I uploaded our Q1 data, generate a report about trends").
    #   → Use _build_pdf_prompt / _build_docx_prompt (rich analytical report).
    #
    # Preservation is detected when:
    #   1. A file was uploaded AND has parsed content, AND
    #   2. The user's prompt explicitly references the uploaded file by name
    #      OR uses preservation keywords (preserve, convert, reproduce, version of)
    #
    _PRESERVE_INTENT_RE = re.compile(
        r"\b(preserve|convert|reproduce|version\s+of|copy\s+of|same\s+as|"
        r"as\s+(?:a\s+)?(?:pdf|docx?|word)|generate\s+(?:a\s+)?(?:pdf|docx?|word)"
        r"\s+(?:version|copy|of))\b"
        r"|\buploaded\s+file\b"
        r"|\bthis\s+(?:file|document|docx|pdf)\b",
        re.IGNORECASE | re.DOTALL,
    )
    _is_preservation = bool(
        _first_attachment_parsed
        and (
            _PRESERVE_INTENT_RE.search(effective_question)
            # Also treat as preservation if the filename is explicitly mentioned
            or (_first_attachment_name and
                _first_attachment_name.lower().split(".")[0][:20].lower()
                in effective_question.lower())
        )
    )

    if _is_preservation:
        # Use the full parsed content of the uploaded file — no LLM generation
        struct_prompt = _build_preservation_prompt(
            parsed_content=_first_attachment_parsed[:10000],
            source_filename=_first_attachment_name,
            target_format=fmt,
        )
        logger.info(
            f"chat_worker: using PRESERVATION prompt for {_first_attachment_name!r} "
            f"→ {fmt} ({len(_first_attachment_parsed):,} chars parsed content)"
        )
    elif fmt in ("docx", "word", "doc"):
        struct_prompt = _build_freeform_prompt(effective_question[:6000], "docx")
    elif fmt == "csv":
        # Flat CSV — use the CSV prompt so the structuring LLM classifies the
        # request (csv_mode: test_data vs plain_text). doc_worker then either
        # generates synthetic rows or writes the answer as plain text.
        struct_prompt = _build_csv_prompt(_question_with_attachment[:6000])
    elif fmt == "xlsx":
        struct_prompt = _build_xlsx_prompt(_question_with_attachment[:6000])
    else:
        # pdf and all other text formats — use the same skill-aligned free-form
        # prompt as DOCX. Old _build_pdf_prompt forced Executive Summary +
        # Closing Insight callout into every PDF; freeform follows the user's
        # requested structure and lets the skill apply premium styling.
        struct_prompt = _build_freeform_prompt(effective_question[:6000], "pdf")

    try:
        from models.model_router import model_router as _mr
        _doc_result   = _mr.generate(struct_prompt, model_hint="complex", return_meta=True)
        raw           = (_doc_result["text"] or "").strip()
        _doc_llm_meta = _doc_result["meta"]

        # ── Robust JSON extraction (works across all LLM providers) ──────────
        # 1. Strip markdown code fences (```json ... ``` or ``` ... ```)
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw.strip())
        raw = raw.strip()
        # 2. If model prefixed JSON with prose, extract first { block
        if not raw.startswith("{"):
            _m = re.search(r"\{", raw)
            if _m:
                raw = raw[_m.start():]
        # 3. If model appended prose after JSON, trim to last }
        if raw and not raw.endswith("}"):
            _last = raw.rfind("}")
            if _last != -1:
                raw = raw[:_last + 1]

        struct        = json.loads(raw)
        sections  = struct.get("sections") or []
        # Always prefer the LLM-generated content title; sanitize to reject raw-prompt
        # leaks. When the LLM gives no title, derive one from the CONTENT (first
        # section heading) rather than the generic chat request ("convert this to a
        # word doc"), so the filename never echoes the prompt words.
        raw_llm_title = (struct.get("title") or "").strip()
        if raw_llm_title:
            title = _sanitize_llm_title(raw_llm_title, effective_question)
        else:
            title = _title_from_sections(sections) or _derive_title_from_question(effective_question)
        domain    = (struct.get("domain") or "").strip().lower() or None
        _doc_llm_meta["domain"] = domain

        # ── CSV: carry the structuring classification into the job payload ──
        # _build_csv_prompt returns either a {columns,row_count} test-data spec
        # or real informational `sections` (csv_mode=plain_text). Mirror
        # doc_worker._llm_structure: columns present ⇒ test_data (wrap as a
        # synthetic csv_schema section the code-writer materialises); otherwise
        # plain_text (keep real sections → written verbatim as a flat .csv).
        if fmt == "csv":
            _csv_cols = struct.get("columns") or []
            _csv_rows = struct.get("row_count")
            _csv_mode = "test_data" if _csv_cols else "plain_text"
            _doc_llm_meta["csv_mode"]      = _csv_mode
            _doc_llm_meta["csv_columns"]   = _csv_cols
            _doc_llm_meta["csv_row_count"] = _csv_rows
            if _csv_mode == "test_data":
                sections = [{
                    "heading": "csv_schema",
                    "csv_columns":  _csv_cols,
                    "csv_row_count": _csv_rows,
                }]
            # else plain_text: keep `sections` as the real content.
    except Exception as exc:
        logger.warning(f"chat_worker: doc struct failed ({exc}) — using fallback sections")
        title    = _derive_title_from_question(effective_question)
        sections = [{"heading": "Content", "content": effective_question, "bullets": [], "level": 1}]

    # ── Token accounting ──────────────────────────────────────────────────────
    # Budget deduction is handled by doc_worker.generate_doc_job (the RQ job)
    # after the file is successfully generated.  Do NOT call increment_usage
    # here — the llm_meta is passed in the payload and doc_worker deducts it
    # once on job completion.  Calling it here too would double-count the cost.

    content_md = f"# {title}\n\n"
    for sec in sections:
        h  = sec.get("heading", "")
        c  = sec.get("content", "")
        bl = sec.get("bullets") or []
        if h:
            content_md += f"## {h}\n\n"
        if c:
            content_md += f"{c}\n\n"
        for b in bl:
            content_md += f"- {b}\n"

    ext      = FORMAT_EXTENSIONS.get(fmt, fmt)
    # Derive source_doc_name from first attachment for context-aware filename
    _source_doc_name = ""
    if attachment_ids:
        try:
            from db.database import SessionLocal
            from db.models import ChatAttachment
            _fndb = SessionLocal()
            try:
                _fn_att = _fndb.query(ChatAttachment).filter(
                    ChatAttachment.id == attachment_ids[0]
                ).first()
                if _fn_att:
                    _source_doc_name = _fn_att.file_name or ""
            finally:
                _fndb.close()
        except Exception:
            pass

    # Derive prev_doc_name from the Redis md session for follow-up/update requests.
    # This lets smart_filename produce a meaningful name based on the original doc
    # rather than the terse update prompt (e.g. "add more info about X").
    _prev_doc_name = ""
    if not _source_doc_name and chat_id:
        try:
            _is_edit, _edit_session = _is_doc_edit_followup(question, chat_id)
            if _is_edit and _edit_session:
                _prev_doc_name = ((_edit_session.get("document") or {}).get("filename") or "")
        except Exception:
            pass

    # Use smart_filename for a proper context-aware filename (not raw question as title).
    #   • UPDATE/follow-up (prev_doc_name present) → version the previous doc name
    #     (-updated / -v2 / -v3 …) so each revision is distinct and content-derived,
    #     NOT named after the terse update prompt ("update the document with …").
    #   • NEW doc generated FROM CHAT (no uploaded source file) → the request is
    #     generic noise ("convert this to a word doc"), so pass question="" and let
    #     the LLM content title drive the name (Priority 4 over Priority 3).
    if _prev_doc_name:
        _base = _versioned_basename(_prev_doc_name)
    else:
        _from_chat = not _source_doc_name  # no uploaded file → derived from chat content
        _base = smart_filename(
            title=title,
            question="" if _from_chat else effective_question,
            source_doc_name=_source_doc_name,
        )
    filename = f"{_base}.{ext}"
    job_id   = str(_uuid_mod.uuid4())

    payload = {
        "job_id":          job_id,
        "format":          fmt,
        "title":           title,
        "sections":        sections,
        "content_md":      content_md,
        "user_id":         user_id,
        "chat_id":         chat_id,
        "llm_meta":        _doc_llm_meta or {},
        "source_doc_name": _source_doc_name,
        "prev_doc_name":   _prev_doc_name,
    }

    try:
        from core.job_queue import enqueue_job, Q_DOC
        enqueue_job("workers.doc_worker_agent.generate_doc_job", payload,
                    queue_name=Q_DOC, timeout=1800, retry_count=0)
    except Exception as exc:
        logger.error(f"chat_worker: doc job enqueue failed: {exc}")
        _publish_chunk(stream_key, f"\n\u26a0\ufe0f Document generation failed: {exc}")
        _publish_done(stream_key)
        return True

    fmt_label = ext.upper()
    answer_text = (
        f"I'm generating your **{fmt_label}** document titled **\"{title}\"**.\n\n"
        f"The download button will appear below once it's ready.\n\n"
        f"[DOCJOB:{job_id}:{ext}:{filename}]"
    )
    _publish_chunk(stream_key, answer_text)
    _publish_doc_completion(
        stream_key=stream_key,
        chat_id=chat_id,
        user_id=user_id,
        question=question,
        answer=answer_text,
        meta=_doc_llm_meta,
        job_id=job_id,
    )
    logger.info(f"chat_worker: doc job {job_id} enqueued — {filename}")
    return True



def _handle_md_generation(
        question: str,
        user_id: str,
        chat_id: str,
        stream_key: str,
        attachment_ids: list | None = None,
        chat_context: str = "",
) -> bool:
    """
    Markdown document generation / edit-continuation flow.

    Detects whether this is a new document request or an edit continuation
    by checking for an existing md:session:{chat_id} key in Redis.

    Enqueues workers.md_doc_worker.generate_md_job and publishes the
    [DOCJOB:job_id:md:filename] marker to the SSE stream.

    Returns True (caller skips normal chat generation).
    """
    from core.config import RDB_STREAM
    from core.kv import get_kv as _get_kv
    from tools.doc_generator import smart_filename
    from agents.doc_generator_agent import _derive_title_from_question

    _r_md = _get_kv(RDB_STREAM, decode_responses=True)

    # ── Detect mode: generate vs. edit continuation ───────────────────────────
    mode = "generate"
    title_hint = _derive_title_from_question(question)
    filename_hint = f"{smart_filename(title=title_hint, question=question, fmt_ext='md')}.md"

    if chat_id:
        try:
            import json as _json
            raw_session = _r_md.get(f"md:session:{chat_id}")
            if raw_session:
                session_data = _json.loads(raw_session)
                if session_data.get("document", {}).get("title"):
                    mode = "edit"
                    # Use existing filename for continuity
                    filename_hint = session_data["document"].get("filename", filename_hint)
                    title_hint    = session_data["document"].get("title", title_hint)
                    logger.info(f"chat_worker: md edit continuation detected | chat_id={chat_id!r}")
        except Exception as _se:
            logger.warning(f"chat_worker: md session check failed (treating as generate): {_se}")

    # ── Inject parsed attachment content into MD question if file was uploaded ──
    # Always use compliance-redacted text to avoid LLM proxy PCI blocks.
    _md_question = question
    if attachment_ids and mode == "generate":
        try:
            from db.database import SessionLocal
            from db.models import ChatAttachment
            from agents.compliance_engine import compliance_engine as _ce_md
            _mdb = SessionLocal()
            try:
                _att = _mdb.query(ChatAttachment).filter(
                    ChatAttachment.id == attachment_ids[0]
                ).first()
                if _att and _att.parsed_text:
                    _raw = (_att.parsed_text or "")[:9000]
                    from core.config import COMPLIANCE_SCAN_TOOL_RESULTS
                    if not COMPLIANCE_SCAN_TOOL_RESULTS:
                        _safe = _raw
                    else:
                        try:
                            _r = _ce_md.validate_input(_raw)
                            _safe = _r.get("redacted_text") or _raw
                        except Exception:
                            _safe = _raw
                    _md_question = (
                        f"Source document ({_att.file_name}):\n"
                        f"{_safe}\n\n"
                        f"User request: {question}"
                    )
            finally:
                _mdb.close()
        except Exception:
            pass

    job_id = str(_uuid_mod.uuid4())
    payload = {
        "job_id":        job_id,
        "question":      _md_question,
        "chat_id":       chat_id,
        "user_id":       user_id,
        "mode":          mode,
        "attachment_ids": attachment_ids or [],
        # This chat's conversation history (chat-scoped). The MD worker only
        # feeds it to the LLM when the intent classifier tags the doc's source
        # as the chat (source_scope="chat") — otherwise it's ignored and the
        # doc is generated from the question alone. Only meaningful on generate;
        # edits recall history from the persisted md:session instead.
        "chat_context":  (chat_context or "") if mode == "generate" else "",
    }

    try:
        from core.job_queue import enqueue_job, Q_DOC
        enqueue_job("workers.doc_worker_agent.generate_md_job", payload,
                    queue_name=Q_DOC, timeout=1800, retry_count=0)
    except Exception as exc:
        logger.error(f"chat_worker: md doc job enqueue failed: {exc}")
        _publish_chunk(stream_key, f"\n⚠️ Markdown document generation failed: {exc}")
        _publish_done(stream_key)
        return True

    if mode == "edit":
        action_text = f"Applying your edit to **\"{title_hint}\"**"
    else:
        action_text = f"Generating your **Markdown** document **\"{title_hint}\"**"

    answer_text = (
        f"{action_text}.\n\n"
        f"The download button will appear below once it's ready.\n\n"
        f"[DOCJOB:{job_id}:md:{filename_hint}]"
    )
    _publish_chunk(stream_key, answer_text)
    _publish_doc_completion(
        stream_key=stream_key,
        chat_id=chat_id,
        user_id=user_id,
        question=question,
        answer=answer_text,
        meta=None,
        job_id=job_id,
    )
    logger.info(f"chat_worker: md doc job {job_id} enqueued — mode={mode} {filename_hint!r}")
    return True


def _handle_pptx_generation(
        effective_question: str,
        raw_question: str,
        context_text: str,
        user_id: str,
        chat_id: str,
        stream_key: str,
        attachment_ids: list | None = None,
) -> bool:
    """
    Rich PPTX generation flow:
    1. Use Claude to produce detailed slide schema
    2. Cache pre-computed slides in Redis
    3. Stream slide outline + [DOC_PICKER_BEGIN]...[DOC_PICKER_END] marker
    User picks a theme in the UI -> POST /docs/generate-themed -> download card appears.
    """
    from workers.doc_worker import _build_pptx_prompt
    from tools.doc_generator import slugify, PPTX_THEMES, smart_filename

    # ── Inject parsed attachment content into PPTX prompt if file was uploaded ─
    # Always use compliance-redacted text to avoid LLM proxy PCI blocks.
    _pptx_question = effective_question
    if attachment_ids:
        try:
            from db.database import SessionLocal
            from db.models import ChatAttachment
            from agents.compliance_engine import compliance_engine as _ce_pptx
            _pdb = SessionLocal()
            try:
                _att = _pdb.query(ChatAttachment).filter(
                    ChatAttachment.id == attachment_ids[0]
                ).first()
                if _att and _att.parsed_text:
                    _raw = (_att.parsed_text or "")[:6000]
                    from core.config import COMPLIANCE_SCAN_TOOL_RESULTS
                    if not COMPLIANCE_SCAN_TOOL_RESULTS:
                        _safe = _raw
                    else:
                        try:
                            _r = _ce_pptx.validate_input(_raw)
                            _safe = _r.get("redacted_text") or _raw
                        except Exception:
                            _safe = _raw
                    _pptx_question = (
                        f"Source document content:\n{_safe}\n\n"
                        f"User request: {effective_question}"
                    )
            finally:
                _pdb.close()
        except Exception:
            pass

    _pptx_llm_meta: dict = {}
    try:
        from models.model_router import model_router as _mr
        _pptx_result  = _mr.generate(
            _build_pptx_prompt(_pptx_question[:6000]),
            model_hint="complex",
            return_meta=True,
        )
        raw           = (_pptx_result["text"] or "").strip()
        _pptx_llm_meta = _pptx_result["meta"]

        # ── Robust JSON extraction (works across all LLM providers) ──────────
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw.strip()).strip()
        if not raw.startswith("{"):
            _m = re.search(r"\{", raw)
            if _m:
                raw = raw[_m.start():]
        if raw and not raw.endswith("}"):
            _last = raw.rfind("}")
            if _last != -1:
                raw = raw[:_last + 1]

        struct = json.loads(raw)
        llm_title = (struct.get("title") or "").strip()
        from workers.doc_worker import _derive_title_from_question as _dttq
        title  = llm_title or _dttq(effective_question)
        slides = struct.get("slides") or []
    except Exception as exc:
        logger.warning(f"chat_worker: PPTX struct failed ({exc}) — fallback to simple job")
        from workers.doc_worker import _derive_title_from_question as _dttq
        title  = _dttq(effective_question)
        slides = []
        job_id   = str(_uuid_mod.uuid4())
        filename = f"{smart_filename(title=title, question=effective_question)}.pptx"
        payload  = {
            "job_id": job_id, "format": "pptx", "title": title,
            "sections": [], "content_md": title,
            "user_id": user_id, "chat_id": chat_id,
            "question": raw_question, "theme": "dark_executive",
        }
        try:
            from core.job_queue import enqueue_job, Q_DOC
            enqueue_job("workers.doc_worker_agent.generate_doc_job", payload,
                        queue_name=Q_DOC, timeout=1800, retry_count=0)
        except Exception:
            pass
        answer_text = (
            f"I'm generating the outline for your presentation **\"{title}\"**.\n\n"
            f"[DOCJOB:{job_id}:pptx:{filename}]"
        )
        _publish_chunk(stream_key, answer_text)
        _publish_doc_completion(
            stream_key=stream_key,
            chat_id=chat_id,
            user_id=user_id,
            question=raw_question,
            answer=answer_text,
            meta=_pptx_llm_meta,
            job_id=job_id,
        )
        return True

    # ── Token accounting + budget update ─────────────────────────────────────
    if _pptx_llm_meta and user_id and user_id not in ("default", ""):
        try:
            from store.budget_store import increment_usage
            increment_usage(
                user_id,
                tokens=_pptx_llm_meta.get("tokens", 0),
                cost_usd=_pptx_llm_meta.get("cost_usd", 0.0),
            )
        except Exception as _bu_err:
            logger.warning(f"chat_worker: PPTX budget update failed: {_bu_err}")

    if not slides:
        slides = [{"slide_type": "title", "heading": title}]

    # Build slide outline for the intro message
    _TYPE_EMOJI = {
        "title": "\U0001f3af", "agenda": "\U0001f4cb", "content": "\U0001f4c4",
        "stats": "\U0001f4ca", "quote": "\U0001f4ac", "two_column": "\u2696\ufe0f",
        "two_col": "\u2696\ufe0f", "closing": "\U0001f64f",
    }
    outline_lines = []
    for i, s in enumerate(slides[:10], 1):
        stype   = (s.get("slide_type") or "content").lower()
        heading = (s.get("heading") or "").strip()
        emoji   = _TYPE_EMOJI.get(stype, "\U0001f4c4")
        outline_lines.append(f"  {emoji} **{i}.** {heading}")
    outline = "\n".join(outline_lines)
    n_slides = len(slides)

    # Cache pre-computed slides in Redis
    slides_key = str(_uuid_mod.uuid4())
    try:
        _R_SLIDES.setex(
            f"doc:slides_cache:{slides_key}",
            _SLIDES_CACHE_TTL,
            json.dumps({"title": title, "slides": slides, "question": raw_question[:500]}),
        )
    except Exception as exc:
        logger.warning(f"chat_worker: slides cache write failed: {exc}")

    # Assemble DOC_PICKER marker payload
    themes_list = [
        {"id": k, "name": v["name"], "description": v["description"], "swatch": v["swatch"]}
        for k, v in PPTX_THEMES.items()
    ]
    filename    = f"{smart_filename(title=title, question=effective_question)}.pptx"
    picker_data = {
        "title":      title,
        "fmt":        "pptx",
        "filename":   filename,
        "slides_key": slides_key,
        "n_slides":   n_slides,
        "themes":     themes_list,
    }

    intro = (
        f"I've generated the outline for your presentation **\"{title}\"** with **{n_slides} slides**:\n\n"
        f"{outline}\n\n"
        f"---\n\n"
        f"Choose a visual theme below and I'll generate your PowerPoint file:"
    )
    answer_text = (
        f"{intro}\n\n"
        f"[DOC_PICKER_BEGIN]{json.dumps(picker_data)}[DOC_PICKER_END]"
    )
    _publish_chunk(stream_key, answer_text)
    _publish_doc_completion(
        stream_key=stream_key,
        chat_id=chat_id,
        user_id=user_id,
        question=raw_question,
        answer=answer_text,
        meta=_pptx_llm_meta,
        job_id=slides_key,
    )
    logger.info(
        f"chat_worker: PPTX picker sent — slides_key={slides_key} n_slides={n_slides}"
    )
    return True

# Matches questions that are specifically about THIS platform.
# Only these trigger docs_kb RAG in regular chat.
_PLATFORM_QUERY_RE = re.compile(
    r"\b(ainxt|aix.?nxt|ai.?copilot|ainxt|jpos|upi"
    r"|sdlc|pipeline|workflow|orchestrat"
    r"|agent|skill|mcp|governance|knowledge.base|codebase"
    r"|circuit.breaker|model.router|compliance.engine"
    r"|embed|retriev|vector|pgvector"
    r"|thread|inbox|marketplace|budget)\b",
    re.IGNORECASE,
)

# DB=6 chat streams, DB=0 answer cache, DB=5 doc-slides cache.
# Backends selected per-DB via REDIS_CLIENT_CONFIG_DB{6,0,5}.
_R_STREAM = get_kv(RDB_STREAM, decode_responses=True)
_R_CACHE  = get_kv(RDB_CACHE,  decode_responses=True)
_R_SLIDES = get_kv(RDB_QUEUE,  decode_responses=True)
_SLIDES_CACHE_TTL = 3600  # 1 hour — user has this window to pick a template

STREAM_TTL     = 3600     # 1 hour
STREAM_MAXLEN  = 10_000   # trim stream to prevent unbounded growth


def run_chat_job(payload: dict) -> None:
    """
    RQ job: run chat pipeline for one question, publish tokens to Redis Stream.

    payload keys:
      job_id       str  — Redis Stream key suffix
      question     str  — raw user question
      session_id   str  — for conversation memory lookup
      chat_id      str  — for memory storage
      repo_filter  str  — optional repo scope
      model        str  — optional model hint
      user_id      str  — for budget tracking
    """
    job_id     = payload["job_id"]
    stream_key = f"chat:stream:{job_id}"
    question   = payload.get("question", "").strip()

    if not question:
        _publish_done(stream_key, error="empty question")
        return

    try:
        _run_pipeline(payload, stream_key)
    except Exception as exc:
        logger.error(f"chat_worker job {job_id} failed: {exc}")
        _publish_error(stream_key, str(exc)[:500])
        raise
    finally:
        # Guarantee TTL — stream self-cleans even if gateway never reads it
        _R_STREAM.expire(stream_key, STREAM_TTL)


# ── Pipeline ──────────────────────────────────────────────────

def _run_pipeline(payload: dict, stream_key: str) -> None:
    job_id     = payload["job_id"]
    question   = payload.get("question", "").strip()
    session_id = payload.get("session_id", "")
    chat_id    = payload.get("chat_id", session_id)
    repo_filter = payload.get("repo_filter")
    model_hint  = payload.get("model")
    user_id     = payload.get("user_id", "default")
    user_ctx    = payload.get("user_ctx") or None   # {user_id, ad_level, department, is_admin, ...}
    rag_mode    = payload.get("rag_mode") or None    # context isolation: off | auto | on
    # Anchor latency at enqueue time so it includes queue-wait, not just the LLM
    # slice. Fall back to now() for legacy payloads predating enqueued_at.
    enqueued_at = float(payload.get("enqueued_at") or time.time())

    # Restore trace context from payload so all logs carry the same
    # request_id / chat_id / user_id as the originating gateway request.
    from core.logger import set_request_id, set_chat_context, set_span_id
    set_request_id(payload.get("request_id", job_id))
    set_chat_context(user_id, chat_id)
    set_span_id("chat_worker")

    # Extract W3C traceparent so this worker's OTel spans are children
    # of the gateway span that enqueued the job.
    _trace_headers = payload.get("trace_headers") or {}
    if _trace_headers:
        try:
            from core.telemetry import tracer as _tracer
            _tracer.extract_context(_trace_headers)
        except Exception:
            pass

    # ── R3: CANARY MODE — route % of users to runtime (Mode B) ──────────────
    # RUNTIME_PCT=0   → off (all users go to Python path)
    # RUNTIME_PCT=1   → 1% of users go to runtime
    # RUNTIME_PCT=100 → all users go to runtime (full cutover)
    # Rollback: set RUNTIME_PCT=0 and restart gateway — instant.
    # Python compliance stays wrapped (B2 — the enterprise plugin is not in the runtime yet).
    # ─────────────────────────────────────────────────────────────────────────
    try:
        from core.runtime_client import (
            chat_stream_sync, user_in_canary,
            ENABLE_RUNTIME as _RT_ENABLED, RUNTIME_PCT as _RT_PCT,
        )
        if _RT_ENABLED and _RT_PCT > 0 and user_in_canary(user_id, _RT_PCT):
            _dept = (user_ctx or {}).get("department") if user_ctx else None
            _data_class = payload.get("data_class", "internal")
            _caps = payload.get("caps") or ["chat.send"]
            logger.info(
                f"CANARY: routing user={user_id} session={session_id} "
                f"turn={job_id} to runtime (RUNTIME_PCT={_RT_PCT})"
            )
            try:
                _rt_chunks = []
                for _chunk in chat_stream_sync(
                    session=session_id,
                    turn=job_id,
                    message=question,
                    data_class=_data_class,
                    caps=_caps,
                    department=_dept,
                    user_id=user_id,
                ):
                    _rt_chunks.append(_chunk)
                    _publish_chunk(stream_key, _chunk)

                if _rt_chunks:
                    _publish_done(stream_key, meta={
                        "model":       "ainxt-runtime",
                        "latency":     round(time.time() - enqueued_at, 3),
                        "confidence":  1.0,
                        "chunk_count": len(_rt_chunks),
                    })
                    logger.info(
                        f"CANARY: runtime answered session={session_id} "
                        f"chunks={len(_rt_chunks)}"
                    )
                    return  # runtime handled the turn — skip Python path
                else:
                    logger.warning(
                        f"CANARY: runtime returned no chunks for session={session_id} "
                        "— falling back to Python path"
                    )
            except RuntimeError as _rt_err:
                logger.warning(
                    f"CANARY: runtime error session={session_id}: {_rt_err} "
                    "— falling back to Python path"
                )
    except Exception as _canary_err:
        logger.debug(f"chat_worker: canary block error (non-fatal): {_canary_err}")
    # ─────────────────────────────────────────────────────────────────────────

    # ── STEP 0: Budget gate — cloud models only (defense-in-depth) ───
    # Gateway already checked this, but workers run async and could race
    # with rapid requests.  In-house models (on-prem GPU, routed via
    # LiteLLM) carry no external API cost and are always exempt.
    _CLOUD_W_PFX = ("gpt-", "claude-", "gemini-", "openai/", "anthropic/", "google/", "azure/")
    _w_hint = (model_hint or "").lower().strip()
    _w_is_inhouse = (
            bool(_w_hint)
            and _w_hint not in ("auto", "default")
            and not any(_w_hint.startswith(p) for p in _CLOUD_W_PFX)
    )
    if not _w_is_inhouse and user_id and user_id != "default":
        try:
            from store.budget_store import check_budget as _w_chk_budget
            _w_bgt = _w_chk_budget(user_id)
            if _w_bgt.get("allowed") is not True:
                logger.warning(
                    f"chat_worker: budget gate BLOCKED (cloud) user={user_id} "
                    f"reason={_w_bgt.get('reason')}"
                )
                _publish_chunk(
                    stream_key,
                    f"⛔ Budget exhausted: {_w_bgt.get('reason', 'allocation exhausted')}. "
                    "Switch to an in-house model to continue — in-house models are always available."
                )
                _publish_done(stream_key)
                return
        except Exception as _w_bgt_err:
            logger.error(f"chat_worker: budget gate check failed (fail-open): {_w_bgt_err}")

    # ── STEP 1: Input compliance gate ─────────────────────────
    # Fails CLOSED. The detector is the primary PCI control on the chat path; an import error or a
    # privacy-svc timeout must refuse the turn, never degrade to "send it anyway".
    from agents.compliance_engine import compliance_engine as _ce
    from core.chat_utils import mask_pii
    try:
        chk = _ce.validate_input(question)
    except Exception as _ce_err:
        logger.critical(f"chat_worker: compliance unavailable — refusing turn: {_ce_err}")
        _publish_chunk(
            stream_key,
            "⛔ Compliance screening is unavailable — your request was refused rather than sent "
            "unscanned. Please contact your administrator.",
        )
        _publish_done(stream_key)
        return

    if chk.get("blocked"):
        blocked_types = [f["type"] for f in chk.get("findings", []) if f.get("blocked")]
        _publish_chunk(stream_key, f"⛔ Request blocked by compliance policy: {', '.join(blocked_types)}")
        _publish_done(stream_key)
        return

    # ── STEP 2: PII redaction ─────────────────────────────────
    # Use the ENGINE's redaction, not mask_pii on its own.
    #
    # `mask_pii` is four regexes — card-16, phone, email, India-PAN — with no Luhn or Verhoeff
    # validation. Using it as the only masker meant this path called the full detector, read
    # `blocked`, threw `redacted_text` away, and then forwarded AADHAAR, UPI, IFSC,
    # ACCOUNT_NUMBER, CVV, SECRET, API_KEY, ACCESS_TOKEN and private/SSH key material to the model
    # in the clear. On the flagship chat surface, across 30 RQ workers.
    #
    # `redacted_text` is the full engine pass (regex + Luhn/Verhoeff + the ML privacy-svc
    # escalation). `mask_pii` is kept only as the degenerate fallback — this is the same
    # `redacted_text or mask_pii(...)` idiom gateway.py already uses at four other call sites.
    safe_q = chk.get("redacted_text") or mask_pii(question)

    # ── STEP 2b: Engineer cross-session context injection (P1-NEW-1) ─────────
    # Prepend the engineer's rolling context so each session starts warm.
    # Non-blocking — failure is silently ignored.
    _eng_ctx = ""
    try:
        from core.context_manager import get_context_for_session
        _eng_ctx = get_context_for_session(user_id)
        logger.info("[chat worker] Engineered context for session - {_eng_ctx}")
    except Exception as _engg_err:
        logger.error(f"chat_worker: Engineered context generate failed : {_engg_err}")
        pass

    # ── STEP 3: Conversation history injection (with auto-summarisation) ────
    # If the session has > 20 messages we summarise older turns to avoid
    # blowing the context window (P1-NEW-3).
    question_with_history = safe_q
    _first_turn           = True   # flipped to False below if prior history exists
    _SUMMARY_THRESHOLD    = 20   # messages before summarisation kicks in
    _RECENT_TURNS         = 6    # always include the most recent N turns verbatim
    try:
        from memory.redis_memory import RedisMemory
        rmem  = RedisMemory()
        hist  = rmem.get_conversation(chat_id, limit=40)
        if hist:
            _first_turn = False
            if len(hist) > _SUMMARY_THRESHOLD:
                # Summarise older portion; keep last _RECENT_TURNS verbatim
                older  = hist[:-_RECENT_TURNS]
                recent = hist[-_RECENT_TURNS:]

                # Check if we already have a cached summary for this session
                _sum_key = f"chat:summary:{chat_id}"
                cached_summary = None
                _rc = _R_CACHE
                try:
                    cached_summary = _rc.get(_sum_key)
                except Exception:
                    pass

                if not cached_summary:
                    # Generate summary via LLM (simple model to keep cost low)
                    from models.model_router import model_router as _mr_sum
                    older_text = "\n".join(
                        f"{'User' if m.get('role')=='user' else 'Asst'}: "
                        f"{m.get('content','')[:300]}"
                        for m in older
                    )
                    sum_prompt = (
                            "Summarise the following conversation history concisely "
                            "(max 3 sentences, key decisions and context only):\n\n"
                            + older_text
                    )
                    try:
                        logger.info("[chat worker] : Chat summarizing call to LLM")
                        cached_summary = _mr_sum.generate(sum_prompt, model_hint="simple")
                        # Cache the summary for 30 minutes to avoid re-generating each turn
                        _rc.setex(_sum_key, 1800, cached_summary)
                    except Exception as _se:
                        logger.debug(f"chat_worker: summarisation failed: {_se}")
                        cached_summary = "[Earlier conversation summarised — details omitted]"

                lines = [f"[Session summary]\n{cached_summary}\n\n[Recent messages]"]
                for m in recent:
                    role = "User" if m.get("role") == "user" else "Assistant"
                    lines.append(f"{role}: {m.get('content', '')[:400]}")
            else:
                lines = []
                for m in hist[-6:]:
                    role = "User" if m.get("role") == "user" else "Assistant"
                    lines.append(f"{role}: {m.get('content', '')[:400]}")

            if lines:
                question_with_history = (
                        "[Conversation context]\n"
                        + "\n".join(lines)
                        + "\n\n[Current question]\n"
                        + safe_q
                )
    except Exception as e:
        logger.debug(f"chat_worker history inject failed: {e}")

    # Prepend engineer cross-session context ahead of conversation history
    if _eng_ctx:
        question_with_history = _eng_ctx + question_with_history

    # ── STEP 4: Classify + domain detect ─────────────────────
    from models.classifier import classify_query_complexity, detect_query_domain
    query_type = classify_query_complexity(safe_q)
    q_domain   = detect_query_domain(safe_q)

    # ── STEP 5: Cache check ───────────────────────────────────
    # rag_mode is part of the cache key so Generic-tab and KB-tab answers
    # for the same prompt don't collide. See core.chat_utils._cache_bucket.
    from core.chat_utils import chat_cache_key
    cache_key = chat_cache_key(safe_q, repo_filter, rag_mode)
    cached     = _R_CACHE.get(cache_key)
    if cached:
        try:
            data = json.loads(cached)
            _publish_chunk(stream_key, data["answer"])
            _publish_done(stream_key, meta={"model": "cached", "latency": 0.0, "confidence": 1.0, "chunk_count": 0})
            return
        except Exception:
            pass

    # ── STEP 5b: Semantic answer cache (L2) ──────────────────
    # Cosine-similarity search over past Q&A pairs in pgvector. Only used
    # on first turn (no prior conversation) because the cache key is
    # intent-only and ignores conversation history — using it mid-chat can
    # serve stale answers from unrelated past exchanges.
    # Defense-in-depth: checked here AND inside the store function.
    from store.semantic_cache import (
        SEMANTIC_CACHE_ENABLED as _L2_ENABLED,
        SEMANTIC_MEMORY_ENABLED as _L3_ENABLED,
    )
    if _first_turn and _L2_ENABLED:
        try:
            from store.semantic_cache import get_semantic_cached_answer
            _sem_hit = get_semantic_cached_answer(
                safe_q, repo_filter=repo_filter, user_id=user_id,
                rag_mode=rag_mode,
            )
            if _sem_hit:
                logger.info(
                    f"chat_worker: L2 semantic cache HIT  "
                    f"similarity={_sem_hit['similarity']:.3f}  user={user_id}"
                )
                _publish_chunk(stream_key, _sem_hit["answer"])
                _publish_done(stream_key, meta={
                    "model":       "semantic-cache",
                    "latency":     0.0,
                    "confidence":  float(_sem_hit["similarity"]),
                    "chunk_count": 0,
                    "source":      "semantic_cache",
                })
                return
        except Exception as _sem_err:
            logger.warning(f"chat_worker: L2 lookup failed: {_sem_err}")

    # ── STEP 6: Query rewrite (code domain only) ──────────────
    from models.query_rewriter import rewrite_query
    rewritten = (
        rewrite_query(safe_q)
        if query_type != "simple" and q_domain == "code"
        else safe_q
    )

    # ── STEP 7: Repo detection ────────────────────────────────
    if not repo_filter:
        from core.chat_utils import detect_repo
        repo_filter = detect_repo(rewritten)

    # ── STEP 8: RAG retrieval ─────────────────────────────────
    # docs_kb is searched ONLY when the question is specifically about this
    # platform (AiNxt/AiNxt/SDLC/agents/...) OR the request came from Voice mode.
    # General programming questions, greetings, and all other queries go
    # directly to the LLM with no retrieval.
    context_chunks: list[str] = []
    _retrieval_confidence: float = 0.0
    _is_voice     = payload.get("source") == "voice"
    _is_platform  = bool(_PLATFORM_QUERY_RE.search(safe_q))
    if not repo_filter and (_is_voice or _is_platform):
        # docs_kb fast-path — platform knowledge + voice only
        try:
            from models.hybrid_search import pgvector_search, keyword_search
            _rns = _R_CACHE   # DB=0 — docs:namespaces lives here

            # Build namespace list; fallback to DB discovery if KV was cleared
            _kb_ns = _rns.smembers("docs:namespaces") or set()
            if not _kb_ns:
                try:
                    from db.database import VectorSessionLocal as _VecSess
                    from sqlalchemy import text as _nsql
                    _vdb = _VecSess()
                    try:
                        _ns_rows = _vdb.execute(_nsql(
                            "SELECT DISTINCT REPLACE(repo, 'docs_kb:', '') "
                            "FROM document_embeddings WHERE repo LIKE 'docs_kb:%'"
                        )).fetchall()
                        _kb_ns = {row[0] for row in _ns_rows}
                        for _rns_n in _kb_ns:
                            _rns.sadd("docs:namespaces", _rns_n)
                    finally:
                        _vdb.close()
                except Exception:
                    pass
            _all_ns = set(_kb_ns)

            pv: list = []
            for _ns in _all_ns:
                _repo = f"docs_kb:{_ns}"
                pv += pgvector_search(_repo, safe_q, top_k=5, user_ctx=user_ctx)
                pv += keyword_search(_repo, safe_q, user_ctx=user_ctx)

            # Source-aware thresholds: pgvector cosine ≥ 0.25; BM25 ts_rank ≥ 0.005
            _filtered_pv: list = []
            for _item in pv:
                _src = _item.get("source", "pgvector")
                _thr = 0.25 if _src == "pgvector" else 0.005
                if _item.get("text", "").strip() and _item.get("score", 0) > _thr:
                    _filtered_pv.append(_item)

            # Deduplicate by text prefix, keep highest score
            _seen: set = set()
            _deduped_pv: list = []
            for _item in sorted(_filtered_pv, key=lambda x: x.get("score", 0), reverse=True):
                _pfx = (_item.get("text") or "")[:200]
                if _pfx and _pfx not in _seen:
                    _seen.add(_pfx)
                    _deduped_pv.append(_item)

            # ── Section-aware re-rank ────────────────────────────────────
            # When the query explicitly names a known KB section (e.g.
            # "Prerequisites", "Release Summary"), boost hits whose embedded
            # section_name / section_path matches so they appear in the
            # top-20 window that becomes the LLM context. The router is a
            # pure re-ranker — it never drops non-matching chunks, so a
            # detection misfire degrades gracefully to unchanged order.
            try:
                from core.section_query_router import apply_section_routing
                _deduped_pv, _detected_sections = apply_section_routing(safe_q, _deduped_pv)
                if _detected_sections:
                    logger.info(
                        f"chat_worker docs_kb: section routing detected "
                        f"{_detected_sections} — re-ranked {len(_deduped_pv)} hits"
                    )
            except Exception as _sre:
                # Failure here must never break retrieval. We keep the
                # similarity-ordered list and continue.
                logger.warning(f"chat_worker docs_kb section routing failed: {_sre}")

            # ── G3: BGE cross-encoder rerank + relevance gate ────────────────
            # The docs_kb fast-path previously skipped the BGE reranker that
            # the repo_filter path uses via hybrid_retrieve_context().  Without
            # reranking, chunks with cosine similarity as low as 0.25 reached
            # the LLM, causing hallucination.
            # We now rerank the deduped candidates through the same embed-svc
            # /rerank endpoint and apply the same RERANKER_MIN_SCORE gate
            # (default 0.30) before building context_chunks.
            _reranked_pv: list = _deduped_pv  # fallback: use as-is if rerank fails
            if _deduped_pv:
                try:
                    from models.hybrid_retriever import _rerank_via_svc as _docs_rerank
                    import os as _os_rr
                    _rr_min = float(_os_rr.getenv("RERANKER_MIN_SCORE", "0.30"))
                    _rr_result = _docs_rerank(safe_q, _deduped_pv, top_k=6)
                    # Apply relevance gate — drop chunks below BGE threshold
                    _rr_filtered = [r for r in _rr_result if float(r.get("score", 0)) >= _rr_min]
                    if _rr_filtered:
                        _reranked_pv = _rr_filtered
                        logger.info(
                            f"chat_worker docs_kb: BGE rerank "
                            f"{len(_deduped_pv)}→{len(_rr_filtered)} chunks "
                            f"(gate={_rr_min}) top_score={_rr_filtered[0].get('score', 0):.3f}"
                        )
                    else:
                        # All chunks below gate — keep top-3 from original order
                        # rather than returning empty context (graceful degradation)
                        _reranked_pv = _deduped_pv[:3]
                        logger.warning(
                            f"chat_worker docs_kb: BGE gate dropped all chunks "
                            f"(threshold={_rr_min}) — keeping top-3 originals"
                        )
                except Exception as _rr_err:
                    logger.warning(
                        f"chat_worker docs_kb: BGE rerank failed ({_rr_err}) "
                        f"— using pre-rerank order"
                    )

            context_chunks = [r["text"] for r in _reranked_pv][:20]
            if _reranked_pv:
                _retrieval_confidence = float(
                    sum(r.get("score", 0.0) for r in _reranked_pv[:max(len(context_chunks), 1)])
                    / max(len(context_chunks), 1)
                )
            _top = _reranked_pv[0].get("score", 0) if _reranked_pv else 0.0
            logger.info(
                f"chat_worker docs_kb: {len(context_chunks)} chunks from {sorted(_all_ns)}, "
                f"top_score={_top:.3f}"
            )
        except Exception as e:
            logger.warning(f"chat_worker docs_kb retrieval failed: {e}")
    elif repo_filter:
        try:
            from models.hybrid_retriever import hybrid_retrieve_context
            context_chunks, _retrieval_confidence = hybrid_retrieve_context(
                rewritten, repo_filter, return_confidence=True, user_ctx=user_ctx
            )
        except Exception as e:
            logger.debug(f"chat_worker RAG retrieval failed: {e}")

    # ── STEP 8b: Document generation intent shortcut ─────────
    # Handles both direct requests ("generate a PPT") and follow-up replies
    # ("go with option A") by checking the full conversation history.
    _context_for_doc = "\n\n".join(context_chunks[:4]) if context_chunks else ""
    _attachment_ids  = payload.get("attachment_ids") or []
    if _handle_doc_generation(
            safe_q, question_with_history, _context_for_doc, user_id, chat_id, stream_key,
            attachment_ids=_attachment_ids,
    ):
        return

    # ── STEP 8b-edit: Plain-language doc edit follow-up ──────
    # Catches natural follow-ups like "update this document", "add more
    # examples", "expand the section on X" when no slash command was typed.
    # Only fires when an md:session:{chat_id} exists (i.e. a doc was already
    # generated in this chat). Falls through to normal chat on any error or
    # when no prior doc session is found. Does NOT affect slash-command,
    # confirmation, conversion, PPTX, or normal chat flows.
    if _route_doc_edit_followup(safe_q, user_id, chat_id, stream_key):
        return

    # ── STEP 8c: Semantic memory injection (L3) ──────────────
    # Inject learned patterns relevant to this query as a preamble so the
    # LLM can reuse past successful reasoning. Safe on every turn — adds
    # context, never short-circuits.
    # Defense-in-depth: checked here AND inside the store function.
    if _L3_ENABLED:
        try:
            from store.semantic_cache import get_semantic_memory, format_memory_for_prompt
            _dept = (user_ctx or {}).get("department") if user_ctx else None
            _mem_results = get_semantic_memory(
                safe_q, user_id=user_id, department=_dept,
                rag_mode=rag_mode, source_repo=repo_filter,
            )
            if _mem_results:
                _mem_block = format_memory_for_prompt(_mem_results)
                if _mem_block:
                    question_with_history = f"{_mem_block}\n\n{question_with_history}"
                    logger.info(
                        f"chat_worker: L3 semantic memory injected "
                        f"({len(_mem_results)} patterns) user={user_id} "
                        f"dept={_dept or 'none'}"
                    )
        except Exception as _mem_err:
            logger.warning(f"chat_worker: L3 inject failed: {_mem_err}")

    # ── STEP 9: Build prompt ──────────────────────────────────
    from models.model_router import model_router as _mr
    if context_chunks:
        _ctx_text = "\n\n".join(context_chunks)
        # ── G2: KB_DOC_PROMPT for ALL docs_kb queries ────────────────────────
        # Previously KB_DOC_PROMPT (strict grounding, 8 rules, mandatory
        # citations) only fired when the query matched _PLATFORM_QUERY_RE
        # (keywords like "ainxt", "ainxt", "upi"...).  Queries like "provide
        # holidays for Chennai" or "what are the reason codes" did NOT match
        # and fell through to GROUNDED_PROMPT (synthesis-allowed), allowing
        # the LLM to blend training data with retrieved context.
        # Fix: any query hitting the docs_kb namespace (not repo_filter) gets
        # strict KB_DOC_PROMPT regardless of keyword matching.
        if not repo_filter:
            # docs_kb query — strict grounding, no synthesis from training data
            from agents.tools import KB_DOC_PROMPT
            prompt = KB_DOC_PROMPT.format(
                context=_ctx_text,
                question=question_with_history,
            )
        else:
            # Code repo query — synthesis-allowed prompt
            from agents.tools import GROUNDED_PROMPT
            prompt = GROUNDED_PROMPT.format(
                context=_ctx_text,
                question=question_with_history,
            )
        effective_hint = "medium" if not model_hint else model_hint
    else:
        # ── G1: Zero-context KB query — refuse, do not hallucinate ───────────
        # When retrieval returns 0 chunks for a docs_kb query, the previous
        # behaviour sent the raw question to the LLM with no grounding prompt.
        # The LLM answered from training memory, producing hallucinated codes,
        # dates, and values (e.g. reason codes 01-08 that don't exist in spec).
        # Fix: for KB queries with no retrieved context, return a clear refusal
        # message instead of a bare question.  Code/agent queries are unaffected.
        if not repo_filter:
            from agents.tools import KB_DOC_PROMPT as _KB_PROMPT
            _no_ctx_msg = (
                "No relevant document excerpts were found in the knowledge base "
                "for this query.\n\n"
                "Please tell the user:\n"
                "\"I could not find relevant information in the knowledge base "
                "for this query. Please try:\n"
                "• Rephrasing with more specific terms\n"
                "• Mentioning the document name, appendix, or section number\n"
                "• Checking that the document has been approved and indexed\"\n\n"
                "Do NOT answer from general knowledge or training data."
            )
            prompt = _KB_PROMPT.format(
                context=_no_ctx_msg,
                question=question_with_history,
            )
            logger.info(
                f"chat_worker docs_kb: zero-context — returning refusal prompt "
                f"(retrieval_confidence={_retrieval_confidence:.3f})"
            )
        else:
            prompt = question_with_history
        effective_hint = model_hint or ("medium" if query_type == "simple" else query_type)

    # ── STEP 10: Stream tokens ────────────────────────────────
    full_response = ""
    meta = {"model": "auto", "in_tok": 0, "out_tok": 0, "cost": 0.0, "latency": 0.0}

    # ── LLM concurrency throttle (DistributedSemaphore) ──────────
    # The semaphore uses a Redis Lua script for atomic acquire (SPEC §6.7)
    # and binds to whichever client backs DB=5. If the script handle cannot
    # be obtained we degrade to "always acquire" — the request proceeds
    # without the cross-process limit rather than failing outright.
    sem = None
    _semaphore_handle = "no_throttle"
    try:
        from core.distributed_semaphore import DistributedSemaphore
        # KVClient.register_script (redis-py EVALSHA, SPEC §6.7) is
        # resolved through the KV layer, so the semaphore engages
        # without this call site knowing the backend.
        _r_sem_kv = get_kv(RDB_QUEUE, decode_responses=False)
        sem   = DistributedSemaphore(_r_sem_kv, "llm_global", capacity=500)
        _semaphore_handle = sem.acquire(timeout=120)
        if _semaphore_handle is None:
            _publish_chunk(stream_key, "\nServer busy — too many concurrent requests. Retry in a moment.")
            _publish_done(stream_key)
            return
    except Exception as _se:
        logger.warning(f"chat_worker: DistributedSemaphore init failed: {_se} — proceeding without throttle")

    try:
        # Capture the {"__stream_meta__": {...}} sentinel emitted by
        # model_router.stream() so we get accurate in_tok/out_tok even
        # when the threadpool hops threads between yields (the
        # threading.local fallback would return 0 and cause an in_tok
        # of 3 or similar — same bug as the fast-path /ask had).
        _sm: dict = {}
        for tok in _mr.stream(prompt, model_hint=effective_hint):
            if isinstance(tok, dict):
                _sm = tok.get("__stream_meta__") or _sm
                continue
            if tok:
                full_response += tok
                _publish_chunk(stream_key, tok)

        meta["latency"]  = round(time.time() - enqueued_at, 3)
        meta["model"]    = _sm.get("model_label") or getattr(_mr, "last_model_label", "auto")
        meta["in_tok"]   = int(_sm.get("in_tok",  0) or getattr(_mr, "last_input_tokens",  0) or 0)
        meta["out_tok"]  = int(_sm.get("out_tok", 0) or getattr(_mr, "last_output_tokens", 0) or 0)

    finally:
        if sem is not None and _semaphore_handle is not None and _semaphore_handle != "no_throttle":
            try:
                sem.release(_semaphore_handle)
            except Exception:
                pass

    # ── STEP 11: Cache write ──────────────────────────────────
    if full_response and q_domain != "code":
        try:
            _R_CACHE.setex(cache_key, 86400, json.dumps({"answer": full_response}))
        except Exception as pg_err:
            logger.warning(f"chat_worker: Cache write failed: {pg_err}")
            pass

    # ── STEP 11b: Semantic answer cache write (L2) ───────────
    # Only write on first turn (matches the L2 read gate) and skip
    # code-domain answers (mirrors the L1 cache rule).
    # Defense-in-depth: checked here AND inside the store function.
    if _first_turn and full_response and q_domain != "code" and _L2_ENABLED:
        try:
            from store.semantic_cache import store_semantic_cached_answer
            store_semantic_cached_answer(
                question=safe_q,
                answer=full_response,
                repo_filter=repo_filter,
                user_id=user_id,
                confidence=1.0,
                rag_mode=rag_mode,
            )
        except Exception:
            pass

    # ── STEP 12: Save to conversation memory ─────────────────
    # FIX: add_message does not exist on RedisMemory — the correct method
    # is save_message. The old code silently swallowed the AttributeError
    # via the except clause, so worker turns were never persisted to Redis.
    _redis_mem_meta = {"rag_mode": rag_mode, "repo_filter": repo_filter}
    try:
        from memory.redis_memory import RedisMemory
        rmem = RedisMemory()
        rmem.save_message(chat_id, "user",      question,      metadata=_redis_mem_meta)
        rmem.save_message(chat_id, "assistant", full_response,  metadata=_redis_mem_meta)
    except Exception as _red_err:
        logger.warning(f"chat_worker: Redis Memory persist failed: {_red_err}")

    # # ── STEP 12a: Persist to Postgres (chats + chat_messages) ─
    # try:
    #     from db.database import SessionLocal
    #     from db.models import Chat, ChatMessage
    #     import uuid as _uuid_mod
    #     from datetime import datetime as _dt
    #     _db = SessionLocal()
    #     try:
    #         _chat = _db.query(Chat).filter(Chat.id == chat_id).first()
    #         if not _chat:
    #             _db.add(Chat(
    #                 id=chat_id,
    #                 user_id=user_id if user_id not in ("default", "") else None,
    #                 title=question[:80],
    #             ))
    #         else:
    #             if _chat.title in ("New Chat", "", None):
    #                 _chat.title = question[:80]
    #             _chat.updated_at = _dt.utcnow()
    #         _db.add(ChatMessage(
    #             id=str(_uuid_mod.uuid4()),
    #             chat_id=chat_id,
    #             role="user",
    #             content=question,
    #         ))
    #         _db.add(ChatMessage(
    #             id=str(_uuid_mod.uuid4()),
    #             chat_id=chat_id,
    #             role="assistant",
    #             content=full_response,
    #             model_used=meta.get("model"),
    #             tokens_used=meta.get("in_tok", 0) + meta.get("out_tok", 0),
    #             cost_usd=meta.get("cost", 0.0),
    #         ))
    #         _db.commit()
    #     finally:
    #         _db.close()
    # except Exception as _pg_err:
    #     logger.warning(f"chat_worker: postgres persist failed: {_pg_err}")

    # ── STEP 12b: Update engineer cross-session context (P1-NEW-1) ──────────
    try:
        from core.context_manager import update_context_after_session
        update_context_after_session(
            user_id=user_id,
            question=question,
            answer=full_response,
            repo_filter=repo_filter,
            context_chunks=context_chunks,
        )
    except Exception as _engg_ctx_err:
        logger.warning(f"chat_worker: update context after session failed: {_engg_ctx_err}")
        pass

    # ── STEP 12c: Semantic memory write (L3) ─────────────────
    # Capture this Q&A as a learned pattern when it meets quality bars:
    #   - response is substantive (≥ 80 chars)
    #   - non-trivial query (classifier returned medium/complex)
    #   - not an identity query (never store "who am I" / "my role")
    #   - either RAG-grounded (retrieval_confidence ≥ 0.35) or code-domain
    #   - authenticated user (skip anonymous / "default")
    # Confidence is anchored to retrieval quality and capped below 0.95 so
    # reinforcement via summary_hash collisions can still grow it.
    # Defense-in-depth: checked here AND inside the store function.
    if _L3_ENABLED:
        try:
            from store.semantic_cache import (
                store_semantic_memory,
                _is_identity_query,
                SEMANTIC_MEMORY_MIN_CONFIDENCE,
            )
            _eligible_mem = (
                    len(full_response) >= 80
                    and query_type != "simple"
                    and not _is_identity_query(question)
                    and (float(_retrieval_confidence or 0.0) >= 0.35 or q_domain == "code")
                    and user_id and user_id != "default"
            )
            if _eligible_mem:
                _mem_conf = min(0.90, 0.60 + float(_retrieval_confidence or 0.0) * 0.30)
                if _mem_conf >= SEMANTIC_MEMORY_MIN_CONFIDENCE:
                    _mem_summary = question.strip().splitlines()[0][:200]
                    _mem_content = {
                        "question": question[:1500],
                        "answer":   full_response[:3000],
                        "model":    meta.get("model"),
                        "repo":     repo_filter,
                        "chat_id":  chat_id,
                    }
                    # User-scope write — private to this user
                    store_semantic_memory(
                        memory_type="chat_qa",
                        summary=_mem_summary,
                        content=_mem_content,
                        source=f"chat:{job_id}",
                        confidence=_mem_conf,
                        user_id=user_id,
                        scope_type="user",
                        scope_id=user_id,
                        rag_mode=rag_mode,
                        source_repo=repo_filter,
                    )
                    # Team-scope copy only for platform-knowledge questions
                    _dept_mem = (user_ctx or {}).get("department") if user_ctx else None
                    if _dept_mem and _PLATFORM_QUERY_RE.search(question):
                        store_semantic_memory(
                            memory_type="chat_qa",
                            summary=_mem_summary,
                            content=_mem_content,
                            source=f"chat:{job_id}",
                            confidence=_mem_conf,
                            user_id=user_id,
                            scope_type="team",
                            scope_id=_dept_mem,
                            rag_mode=rag_mode,
                            source_repo=repo_filter,
                        )
        except Exception as _mem_w_err:
            logger.debug(f"chat_worker: L3 write failed: {_mem_w_err}")

    # ── STEP 13: Async Kafka publish - primary persistence path ────────
    try:
        from core.kafka_producer import produce, TOPIC_CHAT_HISTORY, TOPIC_METRICS
        produce(TOPIC_CHAT_HISTORY, {
            "chat_id":    chat_id,
            "user_id":    user_id,
            "question":   question,
            "answer":     full_response,
            "model":      meta.get("model", ""),
            "in_tok":     meta.get("in_tok", 0),
            "out_tok":    meta.get("out_tok", 0),
            "latency":    meta.get("latency", 0),
            "cost":       meta.get("cost", 0),
            "job_id":     job_id,
            "rag_mode":   rag_mode,
            "repo_filter": repo_filter,
            "request_id": payload.get("request_id", job_id),
        }, key=chat_id)
        produce(TOPIC_METRICS, {
            "user_id":           user_id,
            "model":             meta.get("model", "auto"),
            "prompt_tokens":     meta.get("in_tok", 0),
            "completion_tokens": meta.get("out_tok", 0),
            "total_tokens":      meta.get("in_tok", 0) + meta.get("out_tok", 0),
            "cost_usd":          meta.get("cost", 0.0),
        }, key=user_id)
    except Exception as pg_err:
        logger.warning(f"chat_worker: postgres persist failed: {pg_err}")
        pass  # Kafka publish must never fail the chat job

    _conf_val = float(_retrieval_confidence) if _retrieval_confidence is not None else 0.0
    meta["confidence"]   = round(_conf_val, 2)
    meta["chunk_count"]  = len(context_chunks) if context_chunks else 0

    # ── Eval: chat_worker answer quality (fire-and-forget) ───────────────────
    # Covers the /ask/submit async path (Chat, Buddy/CoWork, KB async, etc.).
    # eval_platform is set by the gateway at enqueue time using the same
    # priority logic as the fast-path eval call, so the platform tag is
    # always accurate regardless of which surface triggered the job.
    if full_response:
        try:
            import threading as _cw_eval_thread
            _cw_q   = safe_q
            _cw_ans = full_response
            _cw_ctx = list(context_chunks[:6]) if context_chunks else []
            _cw_sid = session_id
            # Read platform from payload — falls back to rag_mode-based derivation
            # for legacy payloads that predate eval_platform (backward compatible).
            _cw_platform = payload.get("eval_platform") or (
                "knowledge_base" if (rag_mode or "").strip().lower() in {"on", "auto"} else "chat"
            )
            # Source model that generated the answer — stored on groundedness rows
            # so the dashboard can show which model hallucinates more.
            _cw_model    = meta.get("model")
            def _run_cw_eval():
                try:
                    from core.evals import eval_engine as _ee
                    _ee.eval_answer_quality(_cw_q, _cw_ans, _cw_ctx, session_id=_cw_sid, platform=_cw_platform, model=_cw_model)
                except Exception:
                    pass
            _cw_eval_thread.Thread(
                target=_run_cw_eval, daemon=True, name="eval-cw-answer"
            ).start()
        except Exception:
            pass

    _publish_done(stream_key, meta=meta)

    # ── R1: SHADOW MODE ───────────────────────────────────────────────────────
    # Fire the same turn at ainxt-runtimed AFTER Python has answered the user.
    # Output is DISCARDED — never shown to the user. Logs a SHADOW_DIFF row.
    # Runs in a daemon thread so it never blocks or delays the RQ job.
    # Enable by setting ENABLE_RUNTIME=true in .env (default: false).
    # ─────────────────────────────────────────────────────────────────────────
    try:
        from core.runtime_client import shadow_turn, ENABLE_RUNTIME as _RT_ENABLED
        if _RT_ENABLED and full_response:
            import threading as _threading
            _dept = (user_ctx or {}).get("department") if user_ctx else None
            _t = _threading.Thread(
                target=shadow_turn,
                args=(
                    session_id,                          # session
                    job_id,                              # turn  (job_id is unique per turn)
                    question,                            # original user question (unmasked)
                    full_response,                       # python answer for diff comparison
                    payload.get("data_class", "internal"),  # data_class
                    None,                                # caps (runtime uses default chat.send)
                    _dept,                               # department from user_ctx
                ),
                daemon=True,
            )
            _t.start()
    except Exception as _rt_shadow_err:
        logger.debug(f"chat_worker: shadow mode error (non-fatal): {_rt_shadow_err}")
    # ─────────────────────────────────────────────────────────────────────────


# ── Stream helpers ─────────────────────────────────────────────

def _publish_chunk(stream_key: str, text: str) -> None:
    try:
        _R_STREAM.xadd(
            stream_key,
            {"type": "chunk", "data": text},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as e:
        logger.warning(f"XADD chunk failed: {e}")


def _publish_done(stream_key: str, meta: dict | None = None, error: str | None = None) -> None:
    try:
        fields: dict = {"type": "__done__"}
        if meta:
            fields["meta"] = json.dumps(meta)
        if error:
            fields["error"] = error
        _R_STREAM.xadd(stream_key, fields, maxlen=STREAM_MAXLEN, approximate=True)
    except Exception as e:
        logger.warning(f"XADD __done__ failed: {e}")


def _publish_error(stream_key: str, msg: str) -> None:
    try:
        _R_STREAM.xadd(
            stream_key,
            {"type": "__error__", "msg": msg},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as e:
        logger.warning(f"XADD __error__ failed: {e}")
