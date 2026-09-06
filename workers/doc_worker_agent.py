# SPDX-License-Identifier: MIT
# ============================================================
# DOC WORKER AGENT — unified RQ job entry point for all
# document generation formats (PDF, DOCX, PPTX, XLSX, TXT, MD).
#
# PDF / DOCX / PPTX / XLSX / TXT:
#   generate_doc_job()            — pre-structured sections path
#   generate_doc_from_question()  — raw question path (LLM structures)
#   Both delegate to workers.doc_worker (rich prompt builders,
#   image enrichment, multi-pass LLM structuring).
#
# Markdown (.md):
#   generate_md_job()  — session-aware generate + edit flow
#   Delegates to agents.doc_generator_agent (GFM renderer,
#   callout boxes, section patch engine).
#
# Flow (MD):
#   1. Compliance gate on question
#   2. Load session context from Redis (md:session:{chat_id})
#   3. Detect mode: "generate" (new doc) vs "edit" (continuation)
#   4. Call agents.doc_generator_agent.generate_md_doc() or edit_md_doc()
#   5. Write binary to /tmp/ainxt_docs/{file_id}.md
#   6. Save audit record to generated_documents (Postgres)
#   7. Save session context to Redis (md:session:{chat_id}, TTL 24h)
#   8. Publish {status, file_id, filename} to Redis doc:result:{job_id}
#   9. Token accounting + budget update
#
# Session key: md:session:{chat_id}
# Result key:  doc:result:{job_id}
# Progress key: doc:progress:{job_id}
# ============================================================

import json
import os
import uuid as _uuid_mod

from core.config import RDB_STREAM, DOC_STORAGE_DIR, user_doc_dir
from core.kv import get_kv
from core.logger import logger
from workers.doc_worker import _safe_log   # PCI/PII-safe log redaction helper

# DB=6 — document result delivery (same as doc_worker.py)
_R = get_kv(RDB_STREAM, decode_responses=True)

# Persistent storage (see core.config.DOC_STORAGE_DIR). NOT /tmp — files must
# survive container restart so refresh-then-download keeps working.
DOC_DIR        = DOC_STORAGE_DIR
os.makedirs(DOC_DIR, exist_ok=True)
RESULT_TTL     = 86400   # 24 h — matches doc:result:* TTL (binary lives forever)
SESSION_TTL    = 86400   # 24 h — session context
PROGRESS_TTL   = 600     # 10 min — progress key


# ══════════════════════════════════════════════════════════════
# SESSION HELPERS
# ══════════════════════════════════════════════════════════════

def _session_key(chat_id: str) -> str:
    return f"md:session:{chat_id}"


def _load_session(chat_id: str) -> dict | None:
    """Load Markdown session context from Redis. Returns None if not found."""
    if not chat_id:
        return None
    try:
        raw = _R.get(_session_key(chat_id))
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.warning(f"[docgen] worker kind=md session load failed for chat_id={chat_id!r}: {exc}")
    return None


def _save_session(chat_id: str, session: dict) -> None:
    """Persist Markdown session context to Redis with TTL."""
    if not chat_id:
        return
    try:
        _R.setex(_session_key(chat_id), SESSION_TTL, json.dumps(session, ensure_ascii=False))
    except Exception as exc:
        logger.warning(f"[docgen] worker kind=md session save failed for chat_id={chat_id!r}: {exc}")


def _new_session(chat_id: str) -> dict:
    """Create a fresh session dict."""
    return {
        "schema_version": "1.0",
        "chat_id":        chat_id,
        "document":       {},
        "sections":       [],
        "conversation":   [],
        "llm_meta":       {},
    }


def _append_turn(session: dict, role: str, content: str,
                 turn_type: str, meta: dict | None = None) -> None:
    """Append a conversation turn to the session."""
    turns = session.setdefault("conversation", [])
    turn_num = (turns[-1]["turn"] if turns else 0)
    if role == "user":
        turn_num += 1
    turns.append({
        "turn":    turn_num,
        "role":    role,
        "type":    turn_type,
        "content": content,
        **({"meta": meta} if meta else {}),
    })


# ══════════════════════════════════════════════════════════════
# PROGRESS + FAIL HELPERS
# ══════════════════════════════════════════════════════════════

def _publish_progress(job_id: str, step: int, total_steps: int,
                      label: str, detail: str = "") -> None:
    """Publish doc-generation progress to Redis (same format as doc_worker.py)."""
    _R.setex(
        f"doc:progress:{job_id}", PROGRESS_TTL,
        json.dumps({
            "step":        step,
            "total_steps": total_steps,
            "label":       label,
            "detail":      detail,
        }),
    )


def _fail(job_id: str, error: str) -> None:
    """Publish error result to Redis."""
    _R.setex(
        f"doc:result:{job_id}", 3600,
        json.dumps({"status": "error", "error": error})
    )


# ══════════════════════════════════════════════════════════════
# AUDIT RECORD
# ══════════════════════════════════════════════════════════════

def _save_audit(
        *,
        file_id: str,
        job_id: str,
        user_id: str,
        chat_id,
        title: str,
        filename: str,
        file_path: str,
        content_md: str,
        format: str = "md",
) -> None:
    """Save GeneratedDocument audit record to Postgres (mirrors doc_worker._save_audit).

    `format` defaults to "md" for backwards compatibility but callers SHOULD pass
    the real artifact format (e.g. "docx", "pdf", "xlsx") so the download
    endpoint serves the correct MIME type and the UI shows the correct badge.
    """
    try:
        from db.database import SessionLocal
        from db.models import GeneratedDocument
        db = SessionLocal()
        try:
            db.add(GeneratedDocument(
                id=file_id,
                job_id=job_id,
                user_id=user_id,
                chat_id=chat_id or None,
                format=format,
                title=title,
                filename=filename,
                file_path=file_path,
                content_md=content_md,
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.warning(f"[docgen] worker kind=md audit save failed for {file_id}: {exc}")


# ══════════════════════════════════════════════════════════════
# MAIN RQ JOB
# ══════════════════════════════════════════════════════════════

def generate_md_job(payload: dict) -> None:
    """
    RQ job entry point for Markdown document generation and editing.

    payload keys:
      job_id    str  — Redis result key suffix
      question  str  — user's prompt (generate) or edit request (edit)
      chat_id   str  — session key for context persistence
      user_id   str  — requesting user id
      mode      str  — "generate" | "edit" | "auto" (default: "auto")
                       "auto" → checks for existing session to decide
    """
    job_id   = payload.get("job_id", "unknown")
    user_id  = payload.get("user_id", "unknown")
    chat_id  = payload.get("chat_id") or ""

    from core.log_job_context import job_log_context
    with job_log_context(
        job_id=job_id, user_id=user_id, chat_id=chat_id,
        request_id=payload.get("request_id") or "",
        correlation_id=payload.get("correlation_id") or payload.get("request_id") or "",
        job_kind=payload.get("job_kind") or "md",
        agent_id="doc_worker_agent.generate_md_job",
    ):
        return _generate_md_job_impl(payload)


def _generate_md_job_impl(payload: dict) -> None:
    job_id   = payload.get("job_id", "unknown")
    question = (payload.get("question") or "").strip()
    chat_id  = payload.get("chat_id") or ""
    user_id  = payload.get("user_id", "unknown")
    mode     = payload.get("mode", "auto").lower().strip()
    # This chat's history (chat-scoped). Fed to the LLM only when classified as
    # chat-sourced — see the source_scope decision in the generate branch below.
    chat_context = (payload.get("chat_context") or "").strip()

    logger.info(f"[docgen] worker kind=md START generate_md_job | job={job_id} "
                f"mode={mode} user={user_id} chat_id={chat_id!r}")
    logger.info(f"[docgen] worker kind=md question preview: {_safe_log(question)}")

    if not question:
        logger.warning(f"[docgen] worker kind=md ABORT job={job_id} — no question provided")
        _fail(job_id, "No question provided")
        return

    _user_dir = user_doc_dir(user_id, chat_id)

    # ── Step 1: Compliance gate ────────────────────────────────
    logger.info(f"[docgen] worker kind=md STEP 1/7 — compliance gate | job={job_id}")
    _publish_progress(job_id, 1, 7, "Compliance Check", "Validating content safety…")
    try:
        from agents.compliance_engine import compliance_engine as _ce
        chk = _ce.validate_input(question[:4000])
        if chk.get("blocked"):
            logger.warning(f"[docgen] worker kind=md BLOCKED by compliance | job={job_id}")
            _fail(job_id, "Content blocked by compliance policy")
            return
        logger.info(f"[docgen] worker kind=md compliance gate PASSED | job={job_id}")
    except Exception as _ce_err:
        logger.warning(f"[docgen] worker kind=md compliance check failed (fail-open): {_ce_err}")

    # ── Step 2: Load session context ───────────────────────────
    logger.info(f"[docgen] worker kind=md STEP 2/7 — loading session | job={job_id} chat_id={chat_id!r}")
    _publish_progress(job_id, 2, 7, "Loading Session", "Reading document context from session…")
    session = _load_session(chat_id) if chat_id else None

    # ── Step 3: Determine mode ─────────────────────────────────
    if mode == "auto":
        # If a session exists with a document, treat as edit continuation
        if session and session.get("document", {}).get("title"):
            mode = "edit"
            logger.info(f"[docgen] worker kind=md auto-detected mode=edit (session exists) | job={job_id}")
        else:
            mode = "generate"
            logger.info(f"[docgen] worker kind=md auto-detected mode=generate (no session) | job={job_id}")

    if not session:
        session = _new_session(chat_id)

    # ── Step 4: Generate or Edit ───────────────────────────────
    from agents.doc_generator_agent import generate_md_doc, edit_md_doc

    if mode == "generate":
        logger.info(f"[docgen] worker kind=md STEP 3/7 — LLM structuring (generate) | job={job_id}")
        _publish_progress(job_id, 3, 7, "Drafting", "Streaming sections…")

        _append_turn(session, "user", question, "generate")

        # Stream sections to Redis as they materialize so the chat UI can
        # render them inline (CoWorker-style) instead of an opaque spinner.
        # See workers/_doc_preview.py and ai-ui/src/components/DocLivePreview.jsx.
        from workers._doc_preview import make_preview_callbacks
        _on_title, _on_section, _preview_done = make_preview_callbacks(job_id)

        # Resolve the model hint the same way the binary doc path does:
        # explicit user choice > DOC_MODEL_PROVIDER env > "complex".
        from workers.doc_worker import _resolve_doc_model_hint
        _model_hint = _resolve_doc_model_hint(payload.get("user_model_hint"))
        logger.info(
            f"[docgen] worker kind=md model resolution | job={job_id} "
            f"user_choice={payload.get('user_model_hint')!r} → effective={_model_hint!r}"
        )

        # ── Decide whether the doc's SOURCE is this conversation ──────────
        # The LLM intent classifier is authoritative (no keyword/regex): it
        # returns source_scope="chat" for prompts like "summarize this chat
        # session into a document". Only then do we feed the conversation into
        # the generation prompt; a topic-only request ("write a report on UPI")
        # stays a clean fresh authoring with no prior-turn bleed. Fail-open: any
        # error → no context (safe default = topic-only generation).
        _md_chat_context = ""
        if chat_context:
            try:
                from services.doc_router import resolve_doc_plan as _resolve_doc_plan
                _md_plan = _resolve_doc_plan(
                    question,
                    has_attachments=False,
                    chat_id=chat_id or "", user_id=user_id,
                    format_hint="md",
                    intent_hint=(payload.get("doc_intent") or "").lower().strip() or None,
                    has_chat_context=True,
                )
                if _md_plan.source_scope == "chat":
                    _md_chat_context = chat_context
                logger.info(
                    f"[docgen] worker kind=md chat-source decision | job={job_id} "
                    f"intent={_md_plan.intent!r} scope={_md_plan.source_scope!r} "
                    f"feed_context={bool(_md_chat_context)}"
                )
            except Exception as _plan_err:
                logger.warning(
                    f"[docgen] worker kind=md source_scope classification failed "
                    f"(no chat context fed): {_plan_err}"
                )

        try:
            result = generate_md_doc(
                prompt=question,
                chat_id=chat_id,
                model_hint=_model_hint,
                user_id=user_id,
                on_section=_on_section,
                on_title=_on_title,
                chat_context=_md_chat_context,
            )
        except Exception as exc:
            logger.error(f"[docgen] worker kind=md generate_md_doc FAILED | job={job_id} error={exc}",
                         exc_info=True)
            _fail(job_id, str(exc))
            return

        _preview_done()

        file_id    = result["file_id"]
        title      = result["title"]
        domain     = result["domain"]
        sections   = result["sections"]
        content    = result["content"]
        word_count = result["word_count"]
        llm_meta   = result["meta"]

        # ── Check point A (generate): bail out if cancelled during LLM call ──
        from core.generation_registry import is_stopped_redis
        if is_stopped_redis(job_id):
            logger.info(f"[docgen] worker kind=md job {job_id} cancelled after generate_md_doc — stopping")
            return

        # Determine final file path (already written by generate_md_doc)
        src_path = result["output_path"]

        # Also write to the canonical per-user path for download serving.
        # Use atomic write so refresh-then-download never sees a partial file
        # (same race the binary doc path was vulnerable to).
        canonical_path = os.path.join(_user_dir, f"{file_id}.md")
        if src_path != canonical_path:
            try:
                from workers.doc_worker import _atomic_write_text
                _atomic_write_text(canonical_path, content)
            except Exception as exc:
                logger.warning(f"[docgen] worker kind=md canonical path write failed: {exc}")
                canonical_path = src_path

        from tools.doc_generator import smart_filename
        _base    = smart_filename(title=title, question=question, fmt_ext="md")
        filename = f"{_base}.md"

        # ── BUGFIX: honour payload["original_format"] so update-flows that go
        # through this generator (e.g. callers that intend a non-md target)
        # don't lock the session into `"md"`. Falls back to "md" — preserving
        # the historical behaviour for the genuine `/md` / `format=md` paths.
        _payload_orig_fmt = (payload.get("original_format") or "md").lower().strip().lstrip(".") or "md"

        # Update session
        session["document"] = {
            "title":             title,
            "domain":            domain,
            "file_id":           file_id,
            "filename":          filename,
            "file_path":         canonical_path,
            "word_count":        word_count,
            "section_count":     len(sections),
            "content_snapshot":  content,
            # Records the format the user actually requested. The edit branch
            # uses this to regenerate in the same format (Step 4b).
            "original_format":   _payload_orig_fmt,
            "original_question": question,
        }
        session["sections"] = sections
        session["llm_meta"] = {
            "model":        llm_meta.get("model"),
            "total_tokens": int(llm_meta.get("tokens") or 0),
            "total_cost_usd": float(llm_meta.get("cost_usd") or 0.0),
            "total_calls":  1,
        }
        _append_turn(
            session, "assistant",
            f"Generated document: {filename} ({len(sections)} sections, {word_count:,} words)",
            "generate",
            meta={
                "model":    llm_meta.get("model"),
                "tokens":   llm_meta.get("tokens"),
                "cost_usd": llm_meta.get("cost_usd"),
                "latency":  llm_meta.get("latency"),
            },
        )

    else:  # mode == "edit"
        logger.info(f"[docgen] worker kind=md STEP 3/7 — LLM edit (continuation) | job={job_id}")
        _publish_progress(job_id, 3, 7, "Applying Edit", "LLM is applying your changes…")

        doc_meta         = session.get("document", {})
        current_sections = session.get("sections", [])
        current_content  = doc_meta.get("content_snapshot", "")
        original_question = doc_meta.get("original_question") or ""
        if not original_question and session.get("conversation"):
            for turn in session["conversation"]:
                if turn.get("type") == "generate" and turn.get("role") == "user":
                    original_question = turn.get("content", "")
                    break
        title           = doc_meta.get("title", "Document")
        domain          = doc_meta.get("domain")
        file_id         = doc_meta.get("file_id") or str(_uuid_mod.uuid4())
        filename        = doc_meta.get("filename", f"{file_id}.md")
        # original_format: the format the user first requested (pdf/docx/xlsx/etc.)
        # Edits regenerate in this format so the user always gets the same file type.
        original_format = (doc_meta.get("original_format") or "md").lower().strip()

        canonical_path = doc_meta.get("file_path") or os.path.join(_user_dir, f"{file_id}.md")

        # ── BUGFIX: never write the edited Markdown back into the OLD file ──
        # When the original doc was rendered as docx/pdf/xlsx, `canonical_path`
        # still points at that binary artifact (e.g. {ORIG_FILE_ID}.docx). If we
        # passed it to edit_md_doc, _atomic_write_text would clobber the valid
        # DOCX zip with raw Markdown bytes — that's what was making the OLD
        # download corrupt and grow in size after a refresh. Instead, route the
        # MD render to a dedicated sidecar path keyed on a fresh UUID so the
        # original binary at canonical_path stays untouched. The regenerated
        # docx/pdf/xlsx is still produced separately in Step 4b below at its
        # own NEW file_id path. For original_format == "md" we keep the
        # historical behaviour (in-place rewrite of the .md file) because there
        # is no binary artifact at risk.
        if original_format and original_format != "md":
            _md_sidecar_id = str(_uuid_mod.uuid4())
            md_write_path  = os.path.join(_user_dir, f"{_md_sidecar_id}.md")
            logger.info(
                f"[docgen] worker kind=md edit: routing MD render to sidecar "
                f"path={md_write_path} (original_format={original_format}, "
                f"old_canonical={canonical_path}) — old file preserved"
            )
        else:
            md_write_path = canonical_path

        _append_turn(session, "user", question, "edit")

        # Same model resolution as the generate branch so edits honour the
        # user's selected model / DOC_MODEL_PROVIDER override.
        from workers.doc_worker import _resolve_doc_model_hint
        _edit_model_hint = _resolve_doc_model_hint(payload.get("user_model_hint"))
        try:
            result = edit_md_doc(
                edit_request=question,
                chat_id=chat_id,
                current_sections=current_sections,
                current_content=current_content,
                original_question=original_question,
                conversation_history=session.get("conversation", []),
                title=title,
                domain=domain,
                output_path=md_write_path,  # sidecar when original is binary; canonical when .md
                model_hint=_edit_model_hint,
            )
        except Exception as exc:
            logger.error(f"[docgen] worker kind=md edit_md_doc FAILED | job={job_id} error={exc}",
                         exc_info=True)
            _fail(job_id, str(exc))
            return

        sections   = result["sections"]
        content    = result["content"]
        word_count = result["word_count"]
        llm_meta   = result["meta"]
        edit_summary = result["edit_summary"]

        # ── Check point A (edit): bail out if cancelled during LLM call ──
        from core.generation_registry import is_stopped_redis
        if is_stopped_redis(job_id):
            logger.info(f"[docgen] worker kind=md job {job_id} cancelled after edit_md_doc — stopping")
            return

        # Update session — preserve original_format and original_question across edits
        session["document"]["content_snapshot"]  = content
        session["document"]["word_count"]        = word_count
        session["document"]["section_count"]     = len(sections)
        session["document"]["original_format"]   = original_format
        session["document"]["original_question"] = original_question
        session["sections"] = sections

        # ── Bump filename for this edit revision ──────────────────────────────
        # Derive a versioned name from the previous filename so each edit is
        # distinct and content-derived, NOT named after the edit prompt.
        # Pattern: base → base-updated → base-v2 → base-v3 → …
        # Inlined from chat_worker._versioned_basename to avoid cross-import.
        _prev_base = os.path.splitext(os.path.basename(filename))[0]
        _ext_part  = os.path.splitext(filename)[1].lstrip(".")  # e.g. "docx"
        _ext_part  = _ext_part or original_format or "md"
        import re as _re_vb
        _vm = _re_vb.search(r"^(.*)-v(\d+)$", _prev_base, _re_vb.IGNORECASE)
        if _vm:
            _new_base = f"{_vm.group(1)}-v{int(_vm.group(2)) + 1}"
        elif _re_vb.search(r"^(.*)-updated$", _prev_base, _re_vb.IGNORECASE):
            _um = _re_vb.search(r"^(.*)-updated$", _prev_base, _re_vb.IGNORECASE)
            _new_base = f"{_um.group(1)}-v2"
        else:
            _new_base = f"{_prev_base}-updated"
        filename = f"{_new_base}.{_ext_part}"
        session["document"]["filename"] = filename
        logger.info(
            f"[docgen] worker kind=md edit filename bumped | "
            f"prev={_prev_base!r} new={filename!r} chat_id={chat_id!r}"
        )

        # Accumulate token counts
        prev_meta = session.get("llm_meta", {})
        session["llm_meta"] = {
            "model":          llm_meta.get("model") or prev_meta.get("model"),
            "total_tokens":   int(prev_meta.get("total_tokens") or 0) + int(llm_meta.get("tokens") or 0),
            "total_cost_usd": float(prev_meta.get("total_cost_usd") or 0.0) + float(llm_meta.get("cost_usd") or 0.0),
            "total_calls":    int(prev_meta.get("total_calls") or 0) + 1,
        }
        _append_turn(
            session, "assistant",
            f"Applied edit: {edit_summary} (sections: {result['sections_before']} → {result['sections_after']})",
            "edit",
            meta={
                "model":             llm_meta.get("model"),
                "tokens":            llm_meta.get("tokens"),
                "cost_usd":          llm_meta.get("cost_usd"),
                "latency":           llm_meta.get("latency"),
                "edit_type":         result["edit_type"],
                "sections_affected": result["sections_affected"],
                "sections_before":   result["sections_before"],
                "sections_after":    result["sections_after"],
            },
        )

    # ── Step 4: Write MD file (always — used as session snapshot) ─
    logger.info(f"[docgen] worker kind=md STEP 4/7 — writing MD file | job={job_id}")
    _publish_progress(job_id, 4, 7, "Saving MD Snapshot", "Writing Markdown session file to disk…")
    # original_format is only set in the edit branch; default to "md" for generate path
    if mode == "generate":
        original_format   = "md"
        original_question = question

    # For "edit" mode with a binary original_format the MD render lives at the
    # sidecar `md_write_path`; for "generate" mode and md-original edits the
    # write target is canonical_path. Either way, never touch the original
    # binary file here.
    _md_target_path = md_write_path if (mode == "edit") else canonical_path
    if not os.path.exists(_md_target_path):
        try:
            from workers.doc_worker import _atomic_write_text
            _atomic_write_text(_md_target_path, content)
            logger.info(f"[docgen] worker kind=md MD file written | path={_md_target_path}")
        except Exception as exc:
            logger.error(f"[docgen] worker kind=md MD file write FAILED | job={job_id} error={exc}")
            _fail(job_id, f"File write error: {exc}")
            return

    # ── Step 4b: Regenerate in original_format if not md ──────
    # When the user originally requested PDF/DOCX/XLSX/etc., the edit
    # result should be delivered in that same format, not as a .md file.
    # NOTE: for edit-mode the defaults below point at the MD sidecar path
    # (md_write_path), NOT at the original binary canonical_path. If
    # regeneration runs, these get overwritten with the regen artifact. If
    # regeneration fails, the audit row still points to a valid file and
    # the original binary is preserved untouched.
    result_file_id  = file_id
    result_filename = filename
    result_format   = "md"
    result_size     = len(content.encode("utf-8"))
    result_path     = md_write_path if (mode == "edit") else canonical_path

    # ── Check point B: bail out before expensive format conversion ──
    from core.generation_registry import is_stopped_redis
    if is_stopped_redis(job_id):
        logger.info(f"[docgen] worker kind=md job {job_id} cancelled before format conversion — stopping")
        return

    if mode == "edit" and original_format and original_format != "md":
        logger.info(
            f"[docgen] worker kind=md regenerating {original_format} from edited sections | job={job_id}"
        )
        _publish_progress(
            job_id, 4, 7, "Generating File",
            f"Rebuilding {original_format.upper()} from edited content…",
        )
        try:
            # ── OLD tools.doc_generator.generate() DISABLED — use platform skillset ──
            from workers.doc_worker import _skill_generate as _gen_doc
            from workers.doc_worker import _merge_llm_cost as _merge_cost
            from tools.doc_generator import smart_filename as _sfn
            # Capture the (dominant) code-writer cost of the format rebuild so it
            # is folded into the budget deduction below — previously dropped.
            _regen_cost: dict = {}
            _skill_result = _gen_doc(
                job_id=job_id, fmt=original_format,
                question=original_question, title=title,
                sections=sections, domain=domain, theme="dark_executive",
                cost_sink=_regen_cost,
            )
            if _skill_result is None:
                raise RuntimeError("_skill_generate returned None")
            _regen_data, _regen_ext, _regen_mime = _skill_result
            if llm_meta is None:
                llm_meta = {}
            _merge_cost(llm_meta, _regen_cost, job_id=job_id, phase="md_edit_regen")
            # Use the already-versioned filename base (set by the version-bump
            # block above) so the regenerated file gets the correct -updated /
            # -v2 / -v3 suffix rather than recomputing from the original question
            # (which would produce the same name as the first doc).
            _versioned_base = os.path.splitext(os.path.basename(filename))[0]
            _regen_filename = f"{_versioned_base}.{_regen_ext}"
            _regen_file_id  = str(_uuid_mod.uuid4())
            _regen_path     = os.path.join(_user_dir, f"{_regen_file_id}.{_regen_ext}")
            from workers.doc_worker import _atomic_write_bytes
            _atomic_write_bytes(_regen_path, _regen_data)
            result_file_id  = _regen_file_id
            result_filename = _regen_filename
            result_format   = _regen_ext
            result_size     = len(_regen_data)
            result_path     = _regen_path
            # Keep session pointing at the regenerated file
            session["document"]["file_id"]   = _regen_file_id
            session["document"]["filename"]  = _regen_filename
            session["document"]["file_path"] = _regen_path
            logger.info(
                f"[docgen] worker kind=md {_regen_ext} regeneration DONE | job={job_id} "
                f"path={_regen_path} size={len(_regen_data):,} bytes"
            )
        except Exception as exc:
            logger.warning(
                f"[docgen] worker kind=md {original_format} regeneration failed, "
                f"falling back to md | job={job_id}: {exc}"
            )
            # result_* vars already default to md — no further action needed

    # ── Step 5: Postgres audit record ─────────────────────────
    logger.info(f"[docgen] worker kind=md STEP 5/7 — saving audit record | job={job_id}")
    _publish_progress(job_id, 5, 7, "Finalizing", "Recording audit trail…")
    _save_audit(
        file_id=result_file_id,
        job_id=job_id,
        user_id=user_id,
        chat_id=chat_id or None,
        title=title,
        filename=result_filename,
        file_path=result_path,
        content_md=content[:5000],
        format=result_format,  # honours regen ext (docx/pdf/xlsx); falls back to "md"
    )

    # ── Step 6: Save session to Redis ─────────────────────────
    logger.info(f"[docgen] worker kind=md STEP 6/7 — saving session | job={job_id}")
    _publish_progress(job_id, 6, 7, "Saving Session", "Persisting document context for future edits…")
    if chat_id:
        _save_session(chat_id, session)
        logger.info(f"[docgen] worker kind=md session saved | chat_id={chat_id!r}")

    # ── Build summary + preview (never blocks the download) ───
    summary: list = []
    preview: dict = {}
    summary_meta: dict = {"tokens": 0, "cost_usd": 0.0, "source": "skipped"}
    try:
        from agents.doc_generator_agent import build_summary_and_preview
        summary, preview, summary_meta = build_summary_and_preview(
            title=title,
            sections=sections,
            prompt=question,
            chat_id=chat_id,
        )
        # For edits, surface the LLM's edit_summary as the first bullet
        # so the user sees "what changed" at a glance.
        if mode == "edit":
            _edit_sum = (locals().get("edit_summary") or "").strip()
            if _edit_sum:
                summary = [_edit_sum] + [b for b in summary if b != _edit_sum]
                summary = summary[:5]
    except Exception as _sp_err:
        logger.warning(
            f"[docgen] worker kind=md summary/preview build failed | job={job_id} error={_sp_err}"
        )

    # ── Token accounting + budget update ───────────────────────
    _summary_tokens = int(summary_meta.get("tokens") or 0)
    _summary_cost   = float(summary_meta.get("cost_usd") or 0.0)
    if llm_meta and user_id and user_id != "unknown":
        try:
            from store.budget_store import increment_usage
            increment_usage(
                user_id,
                tokens=int(llm_meta.get("tokens", 0) or 0) + _summary_tokens,
                cost_usd=float(llm_meta.get("cost_usd", 0.0) or 0.0) + _summary_cost,
            )
        except Exception as _bu_err:
            logger.warning(f"[docgen] worker kind=md budget update failed for job {job_id}: {_bu_err}")

    # ── Publish result ─────────────────────────────────────────
    in_tok  = int(llm_meta.get("in_tok") or 0)
    out_tok = int(llm_meta.get("out_tok") or 0)
    if not (in_tok or out_tok):
        total   = int(llm_meta.get("tokens") or 0)
        in_tok  = total
        out_tok = 0
    tokens  = in_tok + out_tok
    cost    = float(llm_meta.get("cost_usd") or 0.0)
    latency = float(llm_meta.get("latency") or 0.0)
    model   = llm_meta.get("model") or "unknown"

    doc_result = {
        "status":   "done",
        "file_id":  result_file_id,
        "user_id":  str(user_id),   # owner — enforced by doc_job_status IDOR guard
        "filename": result_filename,
        "format":   result_format,
        "size":     result_size,
        "summary":  summary,
        "preview":  preview,
        "meta": {
            "model":          model,
            "tokens":         tokens,
            "in_tok":         in_tok,
            "out_tok":        out_tok,
            "cost_usd":       cost,
            "latency":        latency,
            "mode":           mode,
            "summary_tokens": _summary_tokens,
            "summary_cost":   _summary_cost,
            "summary_source": summary_meta.get("source"),
        },
    }
    _R.setex(f"doc:result:{job_id}", RESULT_TTL, json.dumps(doc_result))
    logger.info(
        f"[docgen] worker kind=md COMPLETE job={job_id} mode={mode} fmt={result_format} "
        f"— {result_filename} ({result_size:,} bytes)"
    )


# ══════════════════════════════════════════════════════════════
# PDF / DOCX / PPTX / XLSX / TXT — proxy to doc_worker
#
# All non-MD formats are handled by workers.doc_worker which
# contains the rich multi-pass LLM prompt builders, PPTX image
# enrichment, and format-specific generators.  These thin
# wrappers allow all doc formats to be enqueued under a single
# worker module name (workers.doc_worker_agent) while keeping
# the heavy implementation in doc_worker.py.
# ══════════════════════════════════════════════════════════════

def generate_doc_job(payload: dict) -> None:
    """
    RQ entry point for pre-structured document generation
    (PDF / DOCX / PPTX / XLSX / TXT).

    payload keys: job_id, format, title, sections, content_md,
                  user_id, chat_id, use_template, theme, llm_meta,
                  question, source_doc_name
    """
    from workers.doc_worker import generate_doc_job as _generate_doc_job
    _generate_doc_job(payload)


def generate_doc_from_question(payload: dict) -> None:
    """
    RQ entry point for question-driven document generation
    (PDF / DOCX / PPTX / XLSX / TXT).
    LLM structures the document from the raw user question.

    payload keys: job_id, question, format, user_id, chat_id,
                  source_doc_name

    Note: format="md" is redirected to generate_md_job() which uses the
    session-aware generate + edit flow (Redis md:session:{chat_id}).
    """
    fmt = (payload.get("format") or "").lower().strip()
    if fmt == "md":
        # MD format uses the session-aware generate_md_job path.
        # This guard handles any legacy callers that bypass the router's
        # md-specific routing (e.g. direct RQ enqueue with format="md").
        logger.info(
            f"[doc_worker_agent] generate_doc_from_question: redirecting md format "
            f"to generate_md_job | job={payload.get('job_id', 'unknown')}"
        )
        generate_md_job({**payload, "mode": payload.get("mode", "auto")})
        return

    from workers.doc_worker import generate_doc_from_question as _generate_doc_from_question
    _generate_doc_from_question(payload)
