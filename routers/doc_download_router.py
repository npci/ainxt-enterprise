# SPDX-License-Identifier: MIT
# ============================================================
# DOCUMENT GENERATION & DOWNLOAD ROUTER
#
# Endpoints:
#   POST /docs/generate         — submit async doc-gen job
#   GET  /docs/job/{job_id}/status — poll job status
#   GET  /docs/job/{job_id}/stream — SSE-push variant of the above (Track B)
#   GET  /docs/download/{file_id}  — download completed file
#   GET  /docs/history           — list user's generated docs
# ============================================================

import asyncio
import json
import os
import time
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional

from auth.dependencies import get_current_user as _require_auth
from core.config import RDB_STREAM, RDB_QUEUE, DOC_STORAGE_DIR
from core.kv import get_kv
from core.job_queue import Q_DOC, enqueue_job, get_job_status
from core.logger import logger
from core.security_validation import (
    validate_themed_doc_request,
    validate_doc_generate_request,
    _flatten_errors,
)

router = APIRouter(tags=["Document Generation"])

# DB=6 — document result delivery. Backend selected via REDIS_CLIENT_CONFIG_DB6.
_R = get_kv(RDB_STREAM, decode_responses=True)
# Persistent storage volume (NOT /tmp) — survives container restart and OS
# cleanup so generated docs remain downloadable after the user refreshes.
DOC_DIR = DOC_STORAGE_DIR
os.makedirs(DOC_DIR, exist_ok=True)


# ── Pydantic schemas ──────────────────────────────────────────

class DocSection(BaseModel):
    heading: str = ""
    content: str = ""
    bullets: list = []
    level: int = 2


class DocGenerateRequest(BaseModel):
    format: str                        # docx | pptx | pdf | xlsx | txt | md
    title: Optional[str] = None
    sections: list[DocSection] = []
    content_md: str = ""              # raw markdown for audit trail
    chat_id: Optional[str] = None
    use_template: bool = False        # use AiNxt pptx template if available
    question: Optional[str] = None    # raw user question (frontend path — LLM structures it)
    source_doc_name: Optional[str] = None  # uploaded file name for context-aware filename
    prev_doc_name: Optional[str] = None    # filename of the previously generated doc in this chat (for follow-up/update requests)
    mode: Optional[str] = None        # "generate" | "edit" | "auto" — md format only
    attachment_ids: list = []         # uploaded file IDs — worker fetches parsed content for preservation
    chat_context: Optional[str] = None  # last N chat turns as plain text — used when generating doc from conversation
    chat_last_response: Optional[str] = None  # verbatim last assistant reply — preserve all content incl. code blocks
    user_model_hint: Optional[str] = None  # selected chat model: "auto" | "complex" | "openai-deep" | "local:..." etc.
    artifact_id: Optional[str] = None  # revise an EXISTING logical doc (versions share this handle); NULL = new one-shot
    doc_intent: Optional[str] = None   # pre-classified intent: generate|summarize|convert|extract|revise (worker re-classifies if absent)


# ── Shared enqueue helper (used by /docs/generate AND the /ask doc router) ────

def _filename_hint_for(*, fmt: str, title: str, question: str,
                       source_doc_name: str = "", prev_doc_name: str = "",
                       from_chat: bool = False) -> tuple[str, str]:
    """Return (filename_hint, ext). Mirrors the worker's final-name logic so the
    initial [DOCJOB:…] marker shows a sensible name before polling resolves the
    real one. Never raises."""
    _ext = fmt
    try:
        from tools.doc_generator import smart_filename, FORMAT_EXTENSIONS
        _ext = FORMAT_EXTENSIONS.get(fmt, fmt)
        if prev_doc_name:
            import os as _os, re as _re
            _pbase = _os.path.splitext(_os.path.basename(prev_doc_name.strip()))[0]
            if not _pbase:
                _base = "generated-document-updated"
            elif (_m := _re.search(r"^(.*)-v(\d+)$", _pbase, _re.IGNORECASE)):
                _base = f"{_m.group(1)}-v{int(_m.group(2)) + 1}"
            elif (_m := _re.search(r"^(.*)-updated$", _pbase, _re.IGNORECASE)):
                _base = f"{_m.group(1)}-v2"
            else:
                _base = f"{_pbase}-updated"
        else:
            _base = smart_filename(
                title=title or "",
                question="" if from_chat else (question or ""),
                source_doc_name=source_doc_name or "",
                fmt_ext=_ext,
            )
        return f"{_base}.{_ext}", _ext
    except Exception as _exc:  # noqa: BLE001
        logger.warning(f"[docgen] filename_hint derivation failed: {_exc}")
        return f"document.{_ext}", _ext


def build_doc_marker(job_id: str, ext: str, filename: str) -> str:
    """Single source of truth for the [DOCJOB:job_id:ext:filename] marker string.
    Mirrors the frontend buildDocJobMarker() / DOCJOB_RE in Message.jsx so the
    gateway (multi-doc fan-out), the worker, and this router all emit identically."""
    return f"[DOCJOB:{job_id}:{ext}:{filename}]"


def doc_marker_for(*, job_id: str, fmt: str, question: str,
                   from_chat: bool = False) -> tuple[str, str, str]:
    """Compute (marker, filename_hint, ext) for a PRE-ALLOCATED sibling/extra doc
    job. Used by the gateway to mount every [DOCJOB:...] marker up front (before
    the worker fans the render jobs out) so each download card can poll on its
    own job_id immediately. Never raises."""
    _fn, _ext = _filename_hint_for(
        fmt=fmt, title="", question=question, from_chat=from_chat,
    )
    return build_doc_marker(job_id, _ext, _fn), _fn, _ext


def enqueue_doc_job(
    *, user_id: str, question: str, fmt: str, chat_id: str | None = None,
    attachment_ids: list | None = None, source_doc_name: str = "",
    prev_doc_name: str = "", chat_context: str = "", chat_last_response: str = "",
    doc_intent: str | None = None, doc_confidence: float | None = None,
    doc_source_scope: str | None = None,
    all_conversation: bool = False,
    artifact_id: str | None = None,
    user_model_hint: str = "auto", publish_chat_history: bool = True,
    sibling_formats: list | None = None, sibling_job_ids: list | None = None,
    job_id_override: str | None = None,
    pending_sibling_job_ids: list | None = None,
    pending_sibling_formats: list | None = None,
    pending_sibling_intents: list | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Enqueue a QUESTION-MODE document job (the LLM structures + authors it via
    the platform skillset) and return {job_id, filename_hint, ext, marker}.

    Single code path shared by the /docs/generate REST endpoint and the /ask
    backend doc-router so intent→generation behaves identically everywhere.
    Raises RuntimeError only if the queue is unavailable.

    job_id_override: caller-supplied UUID (used by webchat distinct-mode to
        pre-allocate job IDs before enqueueing so all markers can be returned
        to the frontend in a single response).
    pending_sibling_*: distinct-mode sequential fan-out — the worker enqueues
        the next job after this one completes, preventing queue starvation.
    doc_confidence: the CIL/doc_intent classifier's confidence (0.0-1.0) for
        `doc_intent`, when the gateway already ran that classification. Passed
        through so services.doc_router.resolve_doc_plan can trust a
        high-confidence hint and skip re-running models.doc_intent.classify()
        inside the worker — see PERF note in resolve_doc_plan's docstring.
        None (the default) preserves the old behaviour of always classifying."""
    job_id               = job_id_override or str(_uuid.uuid4())
    assistant_message_id = str(_uuid.uuid4())
    request_id           = correlation_id or str(_uuid.uuid4())

    payload = {
        "job_id":             job_id,
        "question":           question,
        "format":             fmt,
        "user_id":            str(user_id),
        "chat_id":            chat_id,
        "source_doc_name":    source_doc_name or "",
        "prev_doc_name":      prev_doc_name or "",
        "attachment_ids":     list(attachment_ids or []),
        "chat_context":       chat_context or "",
        "chat_last_response": chat_last_response or "",
        "assistant_message_id": assistant_message_id,
        "request_id":         request_id,
        "correlation_id":     request_id,
        "job_kind":           "doc",
        "user_model_hint":    user_model_hint or "auto",
    }
    if doc_intent:
        payload["doc_intent"] = doc_intent
    if doc_confidence is not None:
        payload["doc_confidence"] = float(doc_confidence)
    if doc_source_scope:
        payload["doc_source_scope"] = doc_source_scope
    if all_conversation:
        payload["all_conversation"] = True
    if artifact_id:
        payload["artifact_id"] = artifact_id
    _sib_fmts = [str(f).lower().strip() for f in (sibling_formats or []) if f]
    if _sib_fmts:
        payload["sibling_formats"] = _sib_fmts
        payload["sibling_job_ids"] = list(sibling_job_ids or [])

    # Distinct-mode sequential fan-out (webchat only). The worker enqueues the
    # next pending sibling after this job completes, preventing queue starvation.
    _pend_fmts = [str(f).lower().strip() for f in (pending_sibling_formats or []) if f]
    if _pend_fmts:
        payload["pending_sibling_formats"] = _pend_fmts
        payload["pending_sibling_job_ids"] = list(pending_sibling_job_ids or [])
        payload["pending_sibling_intents"] = list(pending_sibling_intents or [])

    enqueue_job(
        "workers.doc_worker_agent.generate_doc_from_question",
        payload, queue_name=Q_DOC, timeout=1800, retry_count=0,
    )

    _fn, _ext = _filename_hint_for(
        fmt=fmt, title="", question=question,
        source_doc_name=source_doc_name, prev_doc_name=prev_doc_name,
        from_chat=bool(chat_last_response or chat_context),
    )
    marker = build_doc_marker(job_id, _ext, _fn)

    if publish_chat_history and chat_id and question:
        try:
            from core.kafka_producer import produce, TOPIC_CHAT_HISTORY
            produce(TOPIC_CHAT_HISTORY, {
                "chat_id":              chat_id,
                "user_id":              str(user_id),
                "question":             question,
                "answer":               marker,
                "assistant_message_id": assistant_message_id,
                "job_id":               job_id,
                "request_id":           request_id,
                "title_hint":           question[:400],
                "attachment_ids":       list(attachment_ids or []),
            }, key=chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[docgen] enqueue chat_history publish failed | "
                           f"job={job_id} corr={request_id} error={exc}")

    logger.info(f"[docgen] enqueue | kind=doc job={job_id} corr={request_id} "
                f"fmt={fmt} intent={doc_intent!r} chat={chat_id} user={user_id}")
    return {"job_id": job_id, "filename_hint": _fn, "ext": _ext, "marker": marker}


# ── POST /docs/generate ───────────────────────────────────────

@router.post("/docs/generate")
def generate_document(req: DocGenerateRequest, request: Request, user=Depends(_require_auth)):
    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
    # free-text fields only get an XSS check; they're either rendered back
    # in chat or fed into an LLM, never used as filesystem paths.
    is_valid, field_errors, _sanitized_doc = validate_doc_generate_request(req)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    req.title = _sanitized_doc["title"]
    req.content_md = _sanitized_doc["content_md"]
    req.question = _sanitized_doc["question"] or None
    req.source_doc_name = _sanitized_doc["source_doc_name"] or None
    req.prev_doc_name = _sanitized_doc["prev_doc_name"] or None

    # Question-driven path: frontend sends raw question, LLM structures it in the worker
    is_question_mode = bool(req.question and not req.sections)

    # Compliance gate on question or title + content
    check_text = req.question or f"{req.title or ''} {req.content_md}"
    try:
        from agents.compliance_engine import compliance_engine
        chk = compliance_engine.validate_input(check_text[:4000])
        if chk.get("blocked"):
            blocked = [f["type"] for f in chk.get("findings", []) if f.get("blocked")]
            raise HTTPException(status_code=403, detail=f"Content blocked: {', '.join(blocked)}")
    except HTTPException:
        raise
    except Exception:
        pass  # compliance failure → fail-open for doc generation

    if not is_question_mode and not req.title:
        raise HTTPException(status_code=422, detail="Either 'question' or 'title' is required")

    job_id = str(_uuid.uuid4())
    # ID baked into the assistant chat row we publish below so the worker can
    # later locate it by primary key (see workers.doc_worker._update_chat_metadata).
    assistant_message_id = str(_uuid.uuid4())
    request_id           = (request.headers.get("x-client-request-id") or "").strip() or str(_uuid.uuid4())
    user_id_str          = str(user.get("sub") or user.get("user_id", "unknown"))

    if req.format == "md":
        # MD format always uses the session-aware generate_md_job path which
        # supports both generate (new doc) and edit (continuation) modes via
        # Redis session persistence (md:session:{chat_id}).
        worker_fn = "workers.doc_worker_agent.generate_md_job"
        payload = {
            "job_id":        job_id,
            "question":      req.question or "",
            "format":        "md",
            "user_id":       user_id_str,
            "chat_id":       req.chat_id,
            "mode":          req.mode or "auto",  # "generate" | "edit" | "auto"
            "prev_doc_name": req.prev_doc_name or "",
        }
        label = repr((req.question or "")[:60])
    elif is_question_mode:
        worker_fn = "workers.doc_worker_agent.generate_doc_from_question"
        payload = {
            "job_id":          job_id,
            "question":        req.question,
            "format":          req.format,
            "user_id":         user_id_str,
            "chat_id":         req.chat_id,
            "source_doc_name": req.source_doc_name or "",
            "prev_doc_name":   req.prev_doc_name or "",
            "attachment_ids":  list(req.attachment_ids or []),
            "chat_context":    req.chat_context or "",
            "chat_last_response": req.chat_last_response or "",
        }
        label = repr(req.question[:60])
    else:
        worker_fn = "workers.doc_worker_agent.generate_doc_job"
        payload = {
            "job_id":       job_id,
            "format":       req.format,
            "title":        req.title,
            "sections":     [s.model_dump() for s in req.sections],
            "content_md":   req.content_md,
            "user_id":      user_id_str,
            "chat_id":      req.chat_id,
            "use_template": req.use_template,
            "source_doc_name": req.source_doc_name or "",
            "prev_doc_name":   req.prev_doc_name or "",
            # Forward the original user question so smart_filename can detect
            # explicit filename instructions (e.g. "name should be env.pdf")
            # even when sections are pre-structured by the caller.
            "question":     req.question or "",
        }
        label = repr(req.title)

    payload["assistant_message_id"] = assistant_message_id
    payload["request_id"]           = request_id
    payload["correlation_id"]       = request_id
    payload["job_kind"]             = (
        "md" if req.format == "md"
        else ("doc" if is_question_mode else "structured")
    )
    # Carry the artifact handle so this build versions an existing logical doc
    # (revise/convert follow-ups) instead of always creating a one-shot.
    if getattr(req, "artifact_id", None):
        payload["artifact_id"] = req.artifact_id
    # Pre-classified intent (summarize/convert/extract/generate) from the client;
    # the worker re-classifies on the fast local model if this is absent.
    if getattr(req, "doc_intent", None):
        payload["doc_intent"] = req.doc_intent
    # User's selected chat model — workers use it as the primary model hint;
    # "auto" (or unset) defers to DOC_MODEL_PROVIDER env var, then falls back
    # to "complex" (Claude Sonnet). See workers/doc_worker._resolve_doc_model_hint.
    payload["user_model_hint"]      = (req.user_model_hint or "auto")

    try:
        enqueue_job(
            worker_fn,
            payload,
            queue_name=Q_DOC,
            timeout=1800,  # 30 min — multi-pass LLM structuring (4 passes × ~3 min) + file generation
            retry_count=0,  # no retry — a killed work-horse means OOM/timeout; retrying immediately
                            # wastes another worker slot and produces the same failure.
                            # User should re-submit if needed.
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    _job_mode = "md" if req.format == "md" else ("question" if is_question_mode else "structured")
    _mode_suffix = f" mode={req.mode!r}" if req.format == "md" else ""
    logger.info(
        f"[docgen] enqueue | kind={_job_mode} job={job_id} corr={request_id} "
        f"fmt={req.format}{_mode_suffix} label={label} user={user.get('sub','?')}"
    )

    # ── Compute a meaningful filename hint to return to the frontend ─────────
    # The frontend uses this as the initial display name in the [DOCJOB:...]
    # marker so the download button shows a sensible title immediately (before
    # the worker finishes and polling overwrites it with the LLM-generated name).
    _filename_hint: str | None = None
    _ext: str = req.format  # fallback; overwritten below if import succeeds
    try:
        from tools.doc_generator import smart_filename, FORMAT_EXTENSIONS
        _ext  = FORMAT_EXTENSIONS.get(req.format, req.format)
        # Mirror the worker's final-name logic (workers/doc_worker.py) so the
        # initial marker name matches what polling later resolves to:
        #   • UPDATE/follow-up (prev_doc_name)  → version the previous name.
        #   • NEW doc from CHAT CONTENT         → question="" so the LLM title
        #     (Priority 4) wins over the generic prompt topic (Priority 3).
        if req.prev_doc_name:
            import os as _os, re as _re
            _pbase = _os.path.splitext(_os.path.basename(req.prev_doc_name.strip()))[0]
            if not _pbase:
                _base = "generated-document-updated"
            elif (_m := _re.search(r"^(.*)-v(\d+)$", _pbase, _re.IGNORECASE)):
                _base = f"{_m.group(1)}-v{int(_m.group(2)) + 1}"
            elif (_m := _re.search(r"^(.*)-updated$", _pbase, _re.IGNORECASE)):
                _base = f"{_m.group(1)}-v2"
            else:
                _base = f"{_pbase}-updated"
        else:
            _from_chat = bool(req.chat_last_response or req.chat_context)
            _base = smart_filename(
                title=req.title or "",
                question="" if _from_chat else (req.question or ""),
                source_doc_name=req.source_doc_name or "",
                fmt_ext=_ext,
            )
        _filename_hint = f"{_base}.{_ext}"
    except Exception as _exc:
        logger.warning(f"[docgen] filename_hint derivation failed: {_exc}")

    # ── Persist the chat turn (user question + assistant marker) ────────────
    # The frontend optimistically renders an assistant bubble with the
    # [DOCJOB:job_id:ext:filename] marker, but unless we round-trip that
    # bubble to chat_messages here, a page refresh wipes it. The chat-history
    # Kafka consumer (workers/kafka_consumer.py:_handle_chat_history) inserts
    # user + assistant rows from the same {chat_id, user_id, question, answer}
    # event shape used by chat_worker.
    if req.chat_id and (req.question or req.title):
        try:
            _fn      = _filename_hint or f"document.{_ext}"
            _marker  = f"[DOCJOB:{job_id}:{_ext}:{_fn}]"

            from core.kafka_producer import produce, TOPIC_CHAT_HISTORY
            # model_used is left unset so the row starts with NULL; the worker
            # fills in the real model name (e.g. "Claude Sonnet (...)") via the
            # post-completion metadata update. A literal "doc_generator" string
            # would otherwise render as the model badge in the chat header.
            produce(TOPIC_CHAT_HISTORY, {
                "chat_id":              req.chat_id,
                "user_id":              user_id_str,
                "question":             req.question or req.title or "",
                "answer":               _marker,
                "assistant_message_id": assistant_message_id,
                "job_id":               job_id,
                "request_id":           request_id,
                "title_hint":           (req.question or req.title or "")[:400],
            }, key=req.chat_id)
        except Exception as exc:
            # Persistence failure is non-fatal — the doc still generates; only
            # the chat bubble won't survive refresh.
            logger.warning(f"[docgen] status chat_history publish failed | job={job_id} error={exc}")

    return {"job_id": job_id, "status": "queued", "filename_hint": _filename_hint}


# ── GET /docs/job/{job_id}/status ────────────────────────────

@router.get("/docs/job/{job_id}/status")
def doc_job_status(
    job_id: str,
    user=Depends(_require_auth),
    started_at: Optional[int] = Query(
        default=None,
        description="Epoch-ms the job's DOCJOB marker was first created (client clock)",
    ),
):
    def _attach_meta(result: dict) -> dict:
        if result.get("status") != "done":
            return result

        meta = result.get("meta") or {}
        result["meta"] = meta
        # Fold the cheap summary call into the primary totals so the user
        # sees the true cost/tokens of the document operation.
        _sum_tokens = int(meta.get("summary_tokens", 0) or 0)
        _sum_cost   = float(meta.get("summary_cost", 0.0) or 0.0)
        meta["tokens"] = int(meta.get("tokens", 0) or 0) + _sum_tokens
        meta["in_tok"] = int(meta.get("in_tok", 0) or 0)
        meta["out_tok"] = int(meta.get("out_tok", 0) or 0) + _sum_tokens
        meta["cost_usd"] = float(meta.get("cost_usd", 0.0) or 0.0) + _sum_cost
        meta["latency"] = float(meta.get("latency", 0.0) or 0.0)
        # Observability: log whether summary/preview made it through.
        logger.info(
            f"[docgen] status=done | job={job_id} "
            f"summary_bullets={len(result.get('summary') or [])} "
            f"preview_sections={len((result.get('preview') or {}).get('sections') or [])} "
            f"summary_source={meta.get('summary_source')!r}"
        )

        user_id = str(user.get("sub") or user.get("user_id", "unknown"))
        if user_id and user_id != "unknown":
            try:
                from store.budget_store import get_budget, get_usage_today
                usage = get_usage_today(user_id)
                budget = get_budget(user_id) or {}
                meta["tokens_today"] = usage.get("tokens_used", 0)
                meta["requests_today"] = usage.get("requests_made", 0)
                meta["cost_today"] = usage.get("cost_usd_spent", 0.0)
                meta["max_tokens_today"] = budget.get("max_tokens_per_day", 0)
                meta["max_requests_today"] = budget.get("max_requests_per_day", 0)
                meta["max_cost_today"] = budget.get("max_cost_usd_per_day", 0.0)
                meta["max_tokens_total"] = budget.get("max_tokens_total", 0)
                meta["max_requests_total"] = budget.get("max_requests_total", 0)
                meta["max_cost_total"] = budget.get("max_cost_usd_total", 0.0)
            except Exception as exc:
                logger.warning(f"[docgen] status budget meta attach failed | job={job_id} error={exc}")
        return result

    _caller_uid = str(user.get("sub") or user.get("user_id", "unknown"))
    _caller_role = str(user.get("role") or "").lower()

    def _owns_result(result: dict) -> bool:
        _owner = str((result or {}).get("user_id") or "").strip()
        if not _owner:
            return True
        return _owner == _caller_uid or _caller_role == "admin"

    raw = _R.get(f"doc:result:{job_id}")
    if raw:
        result = json.loads(raw)
        if not _owns_result(result):
            logger.warning(
                f"[docgen] status ownership denied | job={job_id} "
                f"caller={_caller_uid} owner={result.get('user_id')!r}"
            )
            raise HTTPException(status_code=404, detail="Job not found")
        # A "redirect" result means the worker delegated this job to another
        # (e.g. a revise spawned a versioned rebuild). Transparently follow the
        # chain so the frontend keeps polling the SAME job_id it started with.
        # Bounded to avoid a pathological loop.
        _hops = 0
        while result.get("status") == "redirect" and result.get("job_id") and _hops < 5:
            _next = result["job_id"]
            _raw2 = _R.get(f"doc:result:{_next}")
            if not _raw2:
                # Target not finished yet — surface its live progress instead.
                _p = _R.get(f"doc:progress:{_next}")
                _lp = _R.get(f"doc:live_preview:{_next}")
                _r: dict = {"status": "running"}
                if _p:
                    try: _r["progress"] = json.loads(_p)
                    except Exception: pass
                if _lp:
                    try: _r["live_preview"] = json.loads(_lp)
                    except Exception: pass
                return _r
            result = json.loads(_raw2)
            if not _owns_result(result):
                logger.warning(
                    f"[docgen] status redirect ownership denied | job={job_id} "
                    f"next={_next} caller={_caller_uid}"
                )
                raise HTTPException(status_code=404, detail="Job not found")
            _hops += 1
        if result.get("status") == "done":
            fid = result["file_id"]
            result["download_url"] = f"/ainxt/v1/api/docs/download/{fid}"
        # "clarify" (and any other) status flows through unchanged — _attach_meta
        # no-ops for non-"done" results.
        return _attach_meta(result)

    # ── Read live progress + live preview published by the doc worker ────
    progress = None
    progress_raw = _R.get(f"doc:progress:{job_id}")
    if progress_raw:
        try:
            progress = json.loads(progress_raw)
        except Exception:
            pass

    live_preview = None
    live_preview_raw = _R.get(f"doc:live_preview:{job_id}")
    if live_preview_raw:
        try:
            live_preview = json.loads(live_preview_raw)
        except Exception:
            pass

    def _running_resp() -> dict:
        r: dict = {"status": "running"}
        if progress:
            r["progress"] = progress
        if live_preview:
            r["live_preview"] = live_preview
        return r

    # Minimum file size (bytes) to consider a generated document complete.
    # Even the smallest valid .md document is several KB; anything under this
    # threshold is an empty or partial write and must not be served as "done".
    _MIN_VALID_FILE_BYTES = 2048

    def _db_fallback():
        """Resolve a 'done' response from the permanent GeneratedDocument row.

        Used ONLY when the job is no longer in RQ's view (page refresh hours/
        days after the original turn, Redis TTL expired). Must NOT short-circuit
        an in-flight job — there's a ~5s window after _save_audit (DB row exists)
        but before _R.setex(doc:result) (Redis result with summary written) when
        Postgres would say "done" while the worker is still building the summary.
        Returning during that window strips the summary from the response and
        stops the polling loop — the user never sees the summary card until
        they refresh.

        Guard: file must exist AND be at least _MIN_VALID_FILE_BYTES to be
        considered complete. A 0-byte or 1KB file means the worker hasn't
        finished writing yet — return None so the caller keeps polling.
        """
        try:
            from db.database import SessionLocal
            from db.models import GeneratedDocument
            _uid = str(user.get("sub") or user.get("user_id", "unknown"))
            db = SessionLocal()
            try:
                rec = (
                    db.query(GeneratedDocument)
                    .filter(
                        GeneratedDocument.job_id == job_id,
                        GeneratedDocument.user_id == _uid,
                    )
                    .first()
                )
                if rec is None:
                    return None
                if os.path.exists(rec.file_path):
                    # Guard: reject incomplete files — keeps polling until the
                    # worker finishes writing the full document to disk.
                    try:
                        _fsize = os.path.getsize(rec.file_path)
                    except OSError:
                        _fsize = 0
                    if _fsize < _MIN_VALID_FILE_BYTES:
                        logger.debug(
                            f"[docgen] status DB fallback skipped — file too small "
                            f"| job={job_id} size={_fsize}B min={_MIN_VALID_FILE_BYTES}B"
                        )
                        return None
                    return {
                        "status":       "done",
                        "file_id":      rec.id,
                        "artifact_id":  rec.artifact_id or rec.id,
                        "filename":     rec.filename,
                        "format":       rec.format,
                        "download_url": f"/ainxt/v1/api/docs/download/{rec.id}",
                    }
                # The audit row exists but the binary is gone from disk — the
                # nightly retention sweep (workers.purge_worker.run_doc_purge,
                # DOC_RETAIN_DAYS) already removed it. This is NOT a failure;
                # surface a distinct "expired" status so the frontend renders
                # a disabled/expired chip (matching AttachmentChip/ImageChip in
                # Message.jsx) instead of a scary generation-failed error.
                logger.debug(
                    f"[docgen] status DB fallback — file missing (purged) "
                    f"| job={job_id} file_id={rec.id} path={rec.file_path!r}"
                )
                return {
                    "status":   "expired",
                    "file_id":  rec.id,
                    "filename": rec.filename,
                    "format":   rec.format,
                }
            finally:
                db.close()
        except Exception as exc:
            logger.warning(f"[docgen] status DB fallback failed | job={job_id} error={exc}")
        return None

    def _db_row_age_seconds() -> float | None:
        """Return how many seconds ago the GeneratedDocument row for this job
        was created, or None if no row exists. Used to distinguish a job that
        is still propagating through the queue (row very recent, RQ unknown)
        from a job whose RQ entry has simply expired (row old, RQ unknown).
        Never raises.
        """
        try:
            from db.database import SessionLocal
            from db.models import GeneratedDocument
            from datetime import datetime, timezone
            _uid = str(user.get("sub") or user.get("user_id", "unknown"))
            db = SessionLocal()
            try:
                rec = (
                    db.query(GeneratedDocument.created_at)
                    .filter(
                        GeneratedDocument.job_id == job_id,
                        GeneratedDocument.user_id == _uid,
                    )
                    .first()
                )
                if rec and rec.created_at:
                    _created = rec.created_at
                    # Normalise to UTC-aware for safe subtraction
                    if _created.tzinfo is None:
                        _created = _created.replace(tzinfo=timezone.utc)
                    _now_utc = datetime.now(timezone.utc)
                    return (_now_utc - _created).total_seconds()
            finally:
                db.close()
        except Exception:
            pass
        return None

    rq_status = get_job_status(job_id)
    status = rq_status.get("status", "unknown")

    if status in ("queued", "started", "deferred", "scheduled"):
        # G7: an OOM/SIGKILL of the work-horse leaves RQ stuck on "started" forever
        # (the process died without recording a failure), so without this the UI
        # would poll until its 30-min ceiling. If a job has been "started" beyond a
        # hard wall-clock ceiling (well past the sandbox+LLM budget) with NO result
        # in Redis, treat it as dead and surface an error now.
        if status == "started" and not _R.get(f"doc:result:{job_id}"):
            _started = rq_status.get("started_at")
            if _started:
                try:
                    import datetime as _dt
                    _t0 = _dt.datetime.fromisoformat(_started)
                    if _t0.tzinfo is None:
                        _t0 = _t0.replace(tzinfo=_dt.timezone.utc)
                    _age = (_dt.datetime.now(_dt.timezone.utc) - _t0).total_seconds()
                    _ceiling = int(os.getenv("DOC_JOB_STALL_SECONDS", "1800"))
                    if _age > _ceiling:
                        logger.warning(f"[docgen] status stalled | job={job_id} "
                                       f"age={_age:.0f}s state='started' no_result → dead worker")
                        return {"status": "error",
                                "error": "Document generation stopped unexpectedly "
                                         "(the worker didn't finish). Please try again."}
                except Exception as _se:
                    logger.debug(f"[docgen] status stall-check failed | job={job_id} error={_se}")
        # Job is in-flight — never short-circuit to the DB row. The worker
        # writes Redis with the full result (including summary) AFTER any
        # post-build LLM steps. Keep polling.
        return _running_resp()
    if status == "failed":
        _err = rq_status.get("error", "job failed")
        _uid = str(user.get("sub") or user.get("user_id", "unknown"))
        logger.error(
            f"[docgen] status=error | job={job_id} user={_uid} "
            f"rq_status=failed error={_err!r}"
        )
        return {"status": "error", "error": _err}
    if status == "finished":
        # RQ says the worker process exited. Re-check Redis — the worker
        # writes doc:result AFTER _attach_summary_preview (an LLM call that
        # can take 3-8 seconds), so "finished" does NOT mean the file is
        # ready for download yet.
        #
        # If Redis has the result → return it (file is fully ready).
        # If Redis does not have it yet → keep polling. The worker is still
        # running its final steps. Do NOT fall back to the DB row here —
        # the Postgres row is written BEFORE the summary LLM call, so
        # surfacing it now would show the download button before the file
        # is completely done and before the summary/preview is available.
        raw2 = _R.get(f"doc:result:{job_id}")
        if raw2:
            result = json.loads(raw2)
            if not _owns_result(result):
                logger.warning(
                    f"[docgen] status ownership denied | job={job_id} "
                    f"caller={_caller_uid} owner={result.get('user_id')!r}"
                )
                raise HTTPException(status_code=404, detail="Job not found")
            if result.get("status") == "done":
                fid = result["file_id"]
                result["download_url"] = f"/ainxt/v1/api/docs/download/{fid}"
            return _attach_meta(result)
        # Redis result not yet written — worker still finalising. Keep polling.
        return _running_resp()

    # ── 3. "unknown" means RQ doesn't know this job yet (enqueue
    #       propagation lag < 50 ms) OR the job expired from RQ's
    #       finished-job registry (default TTL = 500 s).
    #       - propagation lag → keep polling (DB row doesn't exist yet,
    #         OR row is very recent meaning the job just started).
    #       - finished-job expiry → surface from the permanent DB row.
    #
    #       Recency guard: if a DB row exists but was created less than
    #       30 seconds ago AND RQ has no record of the job, the job is
    #       still propagating through the queue — keep polling rather than
    #       returning a stale/previous row as "done". After 30 seconds any
    #       legitimate job would be visible to RQ (queued/started/finished),
    #       so "unknown + row < 30s" unambiguously means propagation lag.
    if status == "unknown":
        fb = _db_fallback()
        if fb is not None:
            _age = _db_row_age_seconds()
            if _age is not None and _age < 30:
                # Row is too fresh — job is still propagating, not done yet.
                logger.debug(
                    f"[docgen] status unknown+fresh row — keeping polling "
                    f"| job={job_id} age={_age:.1f}s"
                )
                return _running_resp()
            return fb

        PROPAGATION_WINDOW_MS = 60_000  # 60 s — generous vs. RQ enqueue lag
        if started_at is not None:
            try:
                import time as _time
                _now_ms = int(_time.time() * 1000)
                _sa = int(started_at)
                _sa = max(_now_ms - 24 * 60 * 60 * 1000, min(_sa, _now_ms))
                _age_ms = _now_ms - _sa
            except Exception:
                _age_ms = None
            if _age_ms is not None and _age_ms > PROPAGATION_WINDOW_MS:
                logger.warning(
                    f"[docgen] status=error | job={job_id} "
                    f"unknown+no_row age_ms={_age_ms} > {PROPAGATION_WINDOW_MS} "
                    f"→ treating as expired/dead (not restarting)"
                )
                return {
                    "status": "error",
                    "error": "This document could not be recovered — the "
                             "generation job is no longer available. Please try "
                             "again.",
                }
        # started_at was not provided (e.g. page refresh after React state loss)
        # AND there is no Redis result, no DB row, and RQ has no record of this
        # job. There is zero evidence the job exists or is running — returning
        # "running" here would cause the frontend to spin forever after a failure.
        logger.warning(
            f"[docgen] status=error | job={job_id} unknown+no_row+no_started_at "
            f"→ treating as expired/dead (page refresh after failure)"
        )
        return {
            "status": "error",
            "error": "This document could not be recovered — the generation job "
                     "is no longer available. Please try again.",
        }

    return {"status": status}


# ── GET /docs/job/{job_id}/stream ────────────────────────────
#
# SSE-push variant of doc_job_status. Track B of the /ask flow optimization:
# doc generation today is fire-and-forget — /ask returns a [DOCJOB:...] marker
# immediately and the client polls doc_job_status on an interval (Message.jsx),
# so the user sees a static bubble for the full ~1-3 min generation time with
# no visible progress until the final "done" poll.
#
# The worker ALREADY publishes step-by-step progress (doc:progress:{job_id},
# 6 labelled steps — see workers/doc_worker.py's _publish_progress calls) and
# incremental section previews (doc:live_preview:{job_id} — see
# workers/_doc_preview.py) to Redis DB=6 on every job, completely independent
# of how the client consumes them. This endpoint is pure delivery: it tails
# those same keys plus doc:result:{job_id} and pushes each change over SSE,
# so a client can render live progress instead of polling blind. No worker
# changes were needed — the publisher side already existed.
#
# Envelope mirrors the gateway's existing chat-stream convention (see
# gateway.py's continue-generation SSE handler): each frame is
# `data: {"t": <event-name>, ...}\n\n`, and the terminal frame is
# `data: {"__meta__": {...}}\n\n` carrying the same JSON doc_job_status()
# would have returned for a terminal state (done/error/clarify), so an
# existing client can switch from polling to this endpoint with a one-line
# change on the response-handling side.
#
# This is additive — doc_job_status (polling) is untouched and keeps working
# for any client that doesn't opt into streaming.
@router.get("/docs/job/{job_id}/stream")
async def doc_job_stream(
    job_id: str,
    request: Request,
    user=Depends(_require_auth),
):
    _caller_uid = str(user.get("sub") or user.get("user_id", "unknown"))
    _caller_role = str(user.get("role") or "").lower()

    def _owns(result: dict) -> bool:
        _owner = str((result or {}).get("user_id") or "").strip()
        if not _owner:
            return True
        return _owner == _caller_uid or _caller_role == "admin"

    # Poll interval — fast enough to feel live, slow enough that N concurrent
    # SSE connections don't hammer Redis. Progress/preview are small JSON
    # blobs so a GET per tick is cheap; only the payload is pushed on change.
    _POLL_SEC = float(os.getenv("DOC_STREAM_POLL_SEC", "1.0") or "1.0")
    # Hard ceiling so a connection can't be held open forever if the worker
    # never reaches a terminal state (matches DOC_JOB_STALL_SECONDS intent).
    _MAX_STREAM_SEC = int(os.getenv("DOC_STREAM_MAX_SEC", "1800") or "1800")

    async def _events():
        _t0 = time.monotonic()
        _last_progress_raw = None
        _last_preview_raw = None
        _sent_open = False
        try:
            while True:
                if await request.is_disconnected():
                    logger.debug(f"[docgen] stream client disconnected | job={job_id}")
                    return
                if time.monotonic() - _t0 > _MAX_STREAM_SEC:
                    yield "data: " + json.dumps({
                        "__meta__": {"status": "error",
                                     "error": "Stream timed out — the job may still "
                                              "be running; poll /docs/job/{id}/status."}
                    }) + "\n\n"
                    return

                if not _sent_open:
                    yield "data: " + json.dumps({"t": "open", "job_id": job_id}) + "\n\n"
                    _sent_open = True

                # ── Terminal result — same source doc_job_status reads ────────
                raw = _R.get(f"doc:result:{job_id}")
                if raw:
                    try:
                        result = json.loads(raw)
                    except Exception:
                        result = None
                    if result is not None:
                        # Follow "redirect" chains exactly like doc_job_status,
                        # capped the same way (bounded, never an infinite loop).
                        _hops = 0
                        while result.get("status") == "redirect" and result.get("job_id") and _hops < 5:
                            _next = result["job_id"]
                            _raw2 = _R.get(f"doc:result:{_next}")
                            if not _raw2:
                                break
                            try:
                                result = json.loads(_raw2)
                            except Exception:
                                break
                            _hops += 1
                        if result.get("status") != "redirect":
                            if not _owns(result):
                                logger.warning(
                                    f"[docgen] stream ownership denied | job={job_id} "
                                    f"caller={_caller_uid} owner={result.get('user_id')!r}"
                                )
                                yield "data: " + json.dumps({
                                    "__meta__": {"status": "error", "error": "Job not found"}
                                }) + "\n\n"
                                return
                            if result.get("status") == "done":
                                fid = result.get("file_id")
                                if fid:
                                    result["download_url"] = f"/ainxt/v1/api/docs/download/{fid}"
                            yield "data: " + json.dumps({"__meta__": result}) + "\n\n"
                            return

                # ── Progress / live-preview — push only on change ─────────────
                progress_raw = _R.get(f"doc:progress:{job_id}")
                if progress_raw and progress_raw != _last_progress_raw:
                    _last_progress_raw = progress_raw
                    try:
                        yield "data: " + json.dumps({"t": "progress", "progress": json.loads(progress_raw)}) + "\n\n"
                    except Exception:
                        pass

                preview_raw = _R.get(f"doc:live_preview:{job_id}")
                if preview_raw and preview_raw != _last_preview_raw:
                    _last_preview_raw = preview_raw
                    try:
                        yield "data: " + json.dumps({"t": "live_preview", "live_preview": json.loads(preview_raw)}) + "\n\n"
                    except Exception:
                        pass

                # RQ-level failure with no doc:result yet written (mirrors the
                # "failed" branch in doc_job_status) — surface it and stop.
                rq_status = get_job_status(job_id)
                if rq_status.get("status") == "failed":
                    yield "data: " + json.dumps({
                        "__meta__": {"status": "error", "error": rq_status.get("error", "job failed")}
                    }) + "\n\n"
                    return

                await asyncio.sleep(_POLL_SEC)
        except asyncio.CancelledError:
            logger.debug(f"[docgen] stream cancelled | job={job_id}")
            raise

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── POST /docs/job/{job_id}/cancel ───────────────────────────

@router.post("/docs/job/{job_id}/cancel")
async def cancel_doc_job(job_id: str, _user=Depends(_require_auth)):
    """Cancel a queued or in-progress document generation job."""
    from core.job_queue import cancel_job
    from core.generation_registry import request_stop_redis

    # Guard: never overwrite a completed result. If the job is already done
    # (e.g. the user pressed Cancel during the brief "checking" flash on reload),
    # silently ignore the request so the download button remains intact.
    _existing_raw = _R.get(f"doc:result:{job_id}")
    if _existing_raw:
        try:
            if json.loads(_existing_raw).get("status") == "done":
                logger.info(f"[docgen] cancel | job={job_id} already_done — cancel ignored")
                return {"cancelled": False, "job_id": job_id, "reason": "already_done"}
        except Exception:
            pass

    # Level 1: cancel if still queued (not yet picked up by a worker)
    cancelled = cancel_job(job_id)

    # Level 2: set Redis stop flag so an in-progress worker bails out
    request_stop_redis(job_id)

    # Write a cancelled result so the frontend polling loop terminates cleanly
    _R.setex(
        f"doc:result:{job_id}",
        3600,
        json.dumps({"status": "error", "error": "Cancelled by user"}),
    )

    logger.info(f"[docgen] cancel | job={job_id} queued_cancel={cancelled}")
    return {"cancelled": True, "job_id": job_id}


# ── GET /docs/download/{file_id} ─────────────────────────────

@router.get("/docs/download/{file_id}")
def download_document(file_id: str, user=Depends(_require_auth)):
    _user_id = str(user.get("sub") or user.get("user_id", "unknown"))

    try:
        from db.database import SessionLocal
        from db.models import GeneratedDocument
        db = SessionLocal()
        try:
            # Scope by user_id so only the owner can download. Returning 404
            # (not 403) on mismatch avoids leaking file existence to other users.
            rec = db.query(GeneratedDocument).filter(
                GeneratedDocument.id == file_id,
                GeneratedDocument.user_id == _user_id,
            ).first()
        finally:
            db.close()
    except Exception as exc:
        logger.error(f"[docgen] download DB error | file={file_id} error={exc}")
        raise HTTPException(status_code=500, detail="Database error")

    if not rec:
        raise HTTPException(status_code=404, detail="Document not found")

    if not os.path.exists(rec.file_path):
        raise HTTPException(status_code=410, detail="Document has expired or been deleted")

    # ── OLD tools.doc_generator.MIME_TYPES DISABLED — use skillset constants ──
    from workers.doc_worker import MIME_TYPES
    mime = MIME_TYPES.get(rec.format, "application/octet-stream")

    return FileResponse(
        path=rec.file_path,
        media_type=mime,
        filename=rec.filename,
        headers={"Content-Disposition": f'attachment; filename="{rec.filename}"'},
    )


# ── GET /docs/templates ──────────────────────────────────────

@router.get("/docs/templates")
def list_pptx_templates(_user=Depends(_require_auth)):
    """Return the list of available PPTX presentation themes."""
    # ── OLD tools.doc_generator.PPTX_THEMES DISABLED — use skillset constants ──
    from workers.doc_worker import PPTX_THEMES
    return list(PPTX_THEMES.values())


# ── POST /docs/generate-themed ────────────────────────────────

class ThemedDocRequest(BaseModel):
    slides_key: str
    theme_id:   str = "dark_executive"
    fmt:        str = "pptx"
    title:      str = ""
    filename:   str = ""
    chat_id:    Optional[str] = None


@router.post("/docs/generate-themed")
def generate_themed(req: ThemedDocRequest, request: Request, user=Depends(_require_auth)):
    """
    Trigger PPTX generation from pre-computed slides (stored in Redis by chat_worker)
    with a specific visual theme.  Returns {job_id, filename} so the client can poll.
    """
    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
    # `filename` is used verbatim to name the file written to disk, so it's
    # checked against the identifier allow-list (blocks path-traversal chars).
    is_valid, field_errors, sanitized = validate_themed_doc_request(req)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    req.filename = sanitized["filename"]
    req.title = sanitized["title"]

    # Slide structure is cached on DB=5 by chat_worker (queue/state DB).
    _slides_redis = get_kv(RDB_QUEUE, decode_responses=True)

    raw = _slides_redis.get(f"doc:slides_cache:{req.slides_key}")
    if not raw:
        raise HTTPException(status_code=404, detail="Slide structure not found or expired")

    try:
        cached = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=422, detail="Corrupt slide cache")

    slides = cached.get("slides") or []
    title  = req.title or cached.get("title") or "Presentation"

    # ── OLD tools.doc_generator DISABLED — use skillset constants ──
    from workers.doc_worker import PPTX_THEMES
    from tools.doc_generator import slugify, FORMAT_EXTENSIONS
    if req.theme_id not in PPTX_THEMES:
        raise HTTPException(status_code=422, detail=f"Unknown theme: {req.theme_id}")

    ext      = FORMAT_EXTENSIONS.get(req.fmt, "pptx")
    filename = req.filename or f"{slugify(title)}_{req.theme_id}.{ext}"
    job_id   = str(_uuid.uuid4())
    request_id = (request.headers.get("x-client-request-id") or "").strip() or str(_uuid.uuid4())

    payload = {
        "job_id":     job_id,
        "format":     req.fmt,
        "title":      title,
        "sections":   slides,
        "content_md": f"# {title}",
        "user_id":    str(user.get("sub") or user.get("user_id", "unknown")),
        "chat_id":    req.chat_id,
        "theme":      req.theme_id,
        "use_template": False,
        "request_id":     request_id,
        "correlation_id": request_id,
        "job_kind":       "themed",
    }

    try:
        enqueue_job(
            "workers.doc_worker.generate_doc_job",
            payload,
            queue_name=Q_DOC,
            timeout=1800,  # 30 min — matches primary enqueue timeout for consistency
            retry_count=0,  # no retry — killed work-horse means OOM/timeout; retry wastes a slot
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    logger.info(
        f"[docgen] enqueue | kind=themed job={job_id} corr={request_id} "
        f"theme={req.theme_id} fmt={ext} title={repr(title[:60])}"
    )
    return {"job_id": job_id, "filename": filename, "status": "queued"}


# ── GET /docs/history ─────────────────────────────────────────

@router.get("/docs/history")
def doc_history(limit: int = 20, user=Depends(_require_auth)):
    try:
        from db.database import SessionLocal
        from db.models import GeneratedDocument
        db = SessionLocal()
        try:
            rows = (
                db.query(GeneratedDocument)
                .filter(GeneratedDocument.user_id == str(user.get("sub") or user.get("user_id", "unknown")))
                .order_by(GeneratedDocument.created_at.desc())
                .limit(max(1, min(limit, 100)))
                .all()
            )
            return [
                {
                    "id":           r.id,
                    "title":        r.title,
                    "format":       r.format,
                    "filename":     r.filename,
                    "download_url": f"/ainxt/v1/api/docs/download/{r.id}",
                    "created_at":   r.created_at.isoformat() if r.created_at else None,
                    "exists":       os.path.exists(r.file_path),
                }
                for r in rows
            ]
        finally:
            db.close()
    except Exception as exc:
        logger.error(f"[docgen] history error: {exc}")
        raise HTTPException(status_code=500, detail="Could not fetch history")


# ── GET /docs/preview/{file_id} ──────────────────────────────
# Returns the number of available JPEG preview pages for a file.

@router.get("/docs/preview/{file_id}")
def doc_preview_info(file_id: str, user=Depends(_require_auth)):
    """Return the number of JPEG preview pages available for a generated document."""
    import re as _re
    if not _re.match(r"^[0-9a-f-]{36}$", file_id):
        raise HTTPException(status_code=400, detail="Invalid file_id")
    # Count page-N.jpg files alongside the document
    from db.database import SessionLocal
    from db.models import GeneratedDocument
    db = SessionLocal()
    try:
        rec = db.query(GeneratedDocument).filter(GeneratedDocument.id == file_id).first()
        if not rec:
            raise HTTPException(status_code=404, detail="Document not found")
        user_id = str(user.get("sub") or user.get("user_id", ""))
        if rec.user_id != user_id and user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Access denied")
    finally:
        db.close()

    # Count page files: {file_id}.page-N.jpg
    doc_dir = os.path.dirname(rec.file_path)
    pages = 0
    i = 1
    while os.path.isfile(os.path.join(doc_dir, f"{file_id}.page-{i}.jpg")):
        pages = i
        i += 1
    return {"file_id": file_id, "pages": pages}


# ── GET /docs/preview/{file_id}/{page} ───────────────────────
# Serves a single JPEG preview page.

@router.get("/docs/preview/{file_id}/{page}")
def doc_preview_page(file_id: str, page: int, user=Depends(_require_auth)):
    """Serve a JPEG preview page for a generated document."""
    import re as _re
    from fastapi.responses import FileResponse as _FR
    if not _re.match(r"^[0-9a-f-]{36}$", file_id):
        raise HTTPException(status_code=400, detail="Invalid file_id")
    if page < 1 or page > 200:
        raise HTTPException(status_code=400, detail="Page must be between 1 and 200")

    from db.database import SessionLocal
    from db.models import GeneratedDocument
    db = SessionLocal()
    try:
        rec = db.query(GeneratedDocument).filter(GeneratedDocument.id == file_id).first()
        if not rec:
            raise HTTPException(status_code=404, detail="Document not found")
        user_id = str(user.get("sub") or user.get("user_id", ""))
        if rec.user_id != user_id and user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Access denied")
    finally:
        db.close()

    doc_dir = os.path.dirname(rec.file_path)
    page_path = os.path.join(doc_dir, f"{file_id}.page-{page}.jpg")
    if not os.path.isfile(page_path):
        raise HTTPException(status_code=404, detail=f"Preview page {page} not found")
    return _FR(page_path, media_type="image/jpeg")


# ── GET /docs/by-chat/{chat_id} — conversation document memory ─
@router.get("/docs/by-chat/{chat_id}")
def docs_by_chat(chat_id: str, include_source: bool = False, user=Depends(_require_auth)):
    """List the latest version of every document generated in a conversation
    (newest first). Powers "recall the doc I made earlier" + follow-up revisions
    in both Chat and Buddy. Set include_source=true to also return content_md."""
    uid = str(user.get("sub") or user.get("user_id", "unknown"))
    try:
        from services.doc_context import list_docs_for_chat
        mem = list_docs_for_chat(chat_id, uid, include_source=include_source)
        return [
            {
                "artifact_id":  d.artifact_id,
                "id":           d.doc_id,
                "title":        d.title,
                "format":       d.format,
                "version":      d.version,
                "filename":     d.filename,
                "download_url": f"/ainxt/v1/api/docs/download/{d.doc_id}",
                "created_at":   d.created_at,
                **({"content_md": d.content_md} if include_source else {}),
            }
            for d in mem.docs
        ]
    except Exception as exc:
        logger.error(f"[docgen] by-chat error: {exc}")
        raise HTTPException(status_code=500, detail="Could not fetch conversation documents")


# ── GET /docs/{artifact_id}/versions — version history for the Canvas ─
@router.get("/docs/{artifact_id}/versions")
def docs_versions(artifact_id: str, user=Depends(_require_auth)):
    """Version history of one logical document (all builds sharing artifact_id),
    oldest→newest. Powers the CoworkCanvas version rail + preview. Each version's
    `file_id` is the GeneratedDocument row id used by /docs/preview and
    /docs/download.

    Note: one-shot docs default artifact_id to their own file_id, so this also
    resolves when the caller passes a plain file_id."""
    uid = str(user.get("sub") or user.get("user_id", "unknown"))
    artifact_id = (artifact_id or "").strip()
    if not artifact_id:
        raise HTTPException(status_code=400, detail="artifact_id required")

    try:
        from db.database import SessionLocal
        from db.models import GeneratedDocument
        db = SessionLocal()
        try:
            rows = (
                db.query(GeneratedDocument)
                .filter(
                    # Match either the shared artifact handle OR a one-shot doc
                    # whose artifact_id was left NULL (its id IS the handle).
                    ((GeneratedDocument.artifact_id == artifact_id) |
                     (GeneratedDocument.id == artifact_id)),
                    GeneratedDocument.user_id == uid,
                )
                .order_by(GeneratedDocument.version.asc(),
                          GeneratedDocument.created_at.asc())
                .all()
            )
        finally:
            db.close()
    except Exception as exc:
        logger.error(f"[docgen] versions error: {exc}")
        raise HTTPException(status_code=500, detail="Could not load version history")

    if not rows:
        raise HTTPException(status_code=404, detail="No document found for this artifact.")

    title = rows[-1].title or "Document"
    return {
        "artifact_id": artifact_id,
        "title":       title,
        "versions": [
            {
                "version":    int(r.version or 1),
                "format":     (r.format or "").lower(),
                "file_id":    r.id,
                "filename":   r.filename or "",
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "exists":     os.path.exists(r.file_path),
                # Markdown source — the Canvas renders this as a fallback when a
                # version has no rasterized page images (e.g. renderer absent).
                # Capped to keep the payload reasonable for very large docs.
                "content_md": (r.content_md or "")[:200000],
            }
            for r in rows
        ],
    }


# ── POST /docs/revise — edit an existing doc into a new version ─
class DocReviseRequest(BaseModel):
    artifact_id: Optional[str] = None      # if omitted, resolved from chat_id + instruction
    instruction: str                       # natural-language change ("make the intro shorter")
    chat_id: Optional[str] = None
    target_format: Optional[str] = None    # set to convert format ("convert that to PDF")
    user_model_hint: Optional[str] = None


@router.post("/docs/revise")
def docs_revise(req: DocReviseRequest, user=Depends(_require_auth)):
    """Revise a previously generated document into a NEW version — loading its
    stored source, not regenerating from scratch. Shared engine used by Chat;
    Buddy uses its own MCP revise. If artifact_id is omitted, we resolve the
    user's reference ("that doc") from the conversation's document memory."""
    uid = str(user.get("sub") or user.get("user_id", "unknown"))
    artifact_id = (req.artifact_id or "").strip()

    # Resolve a fuzzy reference when the caller didn't pin an artifact_id.
    if not artifact_id and req.chat_id:
        try:
            from services.doc_context import resolve_reference
            ref = resolve_reference(req.chat_id, uid, req.instruction)
            if ref:
                artifact_id = ref.artifact_id
        except Exception as exc:
            logger.warning(f"[docgen] revise reference resolve failed: {exc}")

    if not artifact_id:
        raise HTTPException(status_code=404,
                            detail="No matching document found to revise in this conversation.")

    from services.doc_reviser import revise
    result = revise(
        artifact_id=artifact_id, instruction=req.instruction, user_id=uid,
        chat_id=req.chat_id, target_format=req.target_format,
        user_model_hint=(req.user_model_hint or "auto"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Revision failed"))
    return result


# ── POST /docs/clarify-resume — user answered the clarify question ─
class DocClarifyResumeRequest(BaseModel):
    question: str                          # the ORIGINAL user request
    choice_value: str                      # artifact_id, or "__new__" for a fresh doc
    chat_id: Optional[str] = None
    format: Optional[str] = None           # format hint from the original request
    attachment_ids: list = []
    user_model_hint: Optional[str] = None
    doc_intent: Optional[str] = None       # original intent (e.g. "compare") so the
                                           # resume keeps the same behaviour


@router.post("/docs/clarify-resume")
def docs_clarify_resume(req: DocClarifyResumeRequest, request: Request, user=Depends(_require_auth)):
    """Resume a document request after the user answered a clarify question.

    choice_value == "__new__"  → force a brand-new document (skip artifact
                                  targeting; the worker classifies fresh).
    choice_value == artifact_id → pin that artifact so the worker revises/
                                   converts THAT document without re-asking.

    Re-enqueues the same question path with the ambiguity removed — the doc
    plan sees a resolved target (or an explicit new-doc signal) and proceeds."""
    uid = str(user.get("sub") or user.get("user_id", "unknown"))
    choice = (req.choice_value or "").strip()
    is_new = (choice == "__new__" or not choice)

    job_id = str(_uuid.uuid4())
    fmt = (req.format or "pdf").lower()
    request_id = (request.headers.get("x-client-request-id") or "").strip() or str(_uuid.uuid4())
    payload = {
        "job_id":         job_id,
        "question":       req.question,
        "format":         fmt,
        "user_id":        uid,
        "chat_id":        req.chat_id,
        "attachment_ids": list(req.attachment_ids or []),
        "user_model_hint": (req.user_model_hint or "auto"),
        "assistant_message_id": str(_uuid.uuid4()),
        "request_id":     request_id,
        "correlation_id": request_id,
        "job_kind":       "doc",
    }
    _orig_intent = (req.doc_intent or "").lower().strip()
    if is_new:
        # "__new__" → user opted out of targeting a prior doc.
        # For compare this means "I'll upload the second file" — keep the compare
        # intent so the worker still produces a comparison once both are present.
        # Otherwise force a fresh generate.
        payload["doc_intent"] = _orig_intent if _orig_intent == "compare" else "generate"
    elif _orig_intent == "compare":
        # Compare against the chosen prior doc: keep compare intent and pin the
        # picked artifact as the comparison target (worker loads its content_md).
        payload["doc_intent"] = "compare"
        payload["compare_artifact_id"] = choice
    else:
        # Pin the chosen artifact — the worker versions this exact document.
        payload["artifact_id"] = choice

    try:
        enqueue_job(
            "workers.doc_worker_agent.generate_doc_from_question",
            payload, queue_name=Q_DOC, timeout=1800, retry_count=0,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    logger.info(
        f"[docgen] clarify-resume | job={job_id} corr={request_id} "
        f"choice={'new' if is_new else choice} fmt={fmt} user={user.get('sub','?')}"
    )
    return {"job_id": job_id, "status": "queued"}


# ── Register MCP tool on module load ─────────────────────────

def _register_doc_tool() -> None:
    try:
        from mcp.tool_registry import tool_registry, ToolDefinition
        tool_registry.register(ToolDefinition(
            name="generate_document",
            description=(
                "Generate a downloadable document (Word .docx, PowerPoint .pptx, PDF, "
                "Excel .xlsx, Markdown .md, or plain text .txt) from structured content. "
                "Use when the user asks to create, generate, export, or produce a "
                "document, report, presentation, or spreadsheet."
            ),
            fn=None,
            tags=["document", "generation", "export", "report", "download"],
            input_schema={
                "type": "object",
                "properties": {
                    "format": {
                        "type": "string",
                        "enum": ["docx", "pptx", "pdf", "xlsx", "txt", "md"],
                        "description": "Output format",
                    },
                    "title": {"type": "string", "description": "Document title"},
                    "sections": {
                        "type": "array",
                        "description": "List of {heading, content, bullets, level}",
                    },
                    "content_md": {
                        "type": "string",
                        "description": "Raw markdown content for audit trail",
                    },
                    "use_template": {
                        "type": "boolean",
                        "description": "Use AiNxt branded template (pptx only)",
                    },
                },
                "required": ["format", "title"],
            },
        ))
    except Exception:
        pass  # non-fatal if tool_registry not yet available


_register_doc_tool()
