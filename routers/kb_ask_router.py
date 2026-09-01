# SPDX-License-Identifier: Apache-2.0
"""
routers/kb_ask_router.py
========================
Dedicated POST /kb/ask endpoint for Knowledge-Base chat.

WHY A SEPARATE ENDPOINT
-----------------------
gateway.py's /ask is edited by multiple teams simultaneously. Chat-path
changes (ainxt-api routing, canary cohort, doc-context notifications,
model-switching logic) land in the same file and function as the KB
retrieval logic, causing unintended regressions. This router owns the
entire KB ask flow so Chat PRs never affect it.

SHARED UTILITIES (no duplication)
----------------------------------
  core/kb_retrieval.py   — full retrieval pipeline (namespace→search→rerank→
                           disambiguation→coverage). Returns KBRetrievalResult.
  core/ask_utils.py      — hist_redact, out_redact, clean_for_history,
                           is_followup_question, build_kb_grounded_prompt.
  gateway.cache_key      — L1 Redis cache key (same function, same key space).
  gateway.mask_pii       — PII masking (same function).
  core/kv_cache_hoist.py — _MEMORY_INSTRUCTION, _build_local_system_message.

WHAT THIS ROUTER DOES NOT DO
------------------------------
  - ainxt-api / Rust runtime routing  (KB never goes to Rust)
  - Doc-intent routing (PDF/PPTX/DOCX generation)
  - Image / video generation
  - Orchestrator / repo-scoped queries
  - CIL classification (rag_mode is already known from the request)
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time
import uuid
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.security_validation import validate_free_text, _flatten_errors

from core.logger import logger, set_request_id, set_chat_context, set_span_id, set_correlation_id
from core.kb_retrieval import run_kb_retrieval, TRIVIAL_QUERY_RE
from core.ask_utils import (
    hist_redact,
    out_redact,
    clean_for_history,
    is_followup_question,
    build_kb_grounded_prompt,
)
from core.kv_cache_hoist import (
    MEMORY_INSTRUCTION as _MEMORY_INSTRUCTION,
    build_local_system_message as _build_local_system_message,
)

router = APIRouter(tags=["knowledge_base_ask"])


# ---------------------------------------------------------------------------
# Request model — KB-relevant fields only (subset of gateway.py's Question)
# ---------------------------------------------------------------------------

class KbQuestion(BaseModel):
    question:       str
    model:          Optional[str]        = None
    attachment_ids: Optional[List[str]]  = []
    chat_id:        Optional[str]        = None
    voice_platform: bool                 = False
    tone:           Optional[str]        = None
    user_name:      Optional[str]        = None
    login_id:       Optional[str]        = None
    local_model:    Optional[str]        = None
    agent_id:       Optional[str]        = None
    session_id:     Optional[str]        = None
    cli_messages:   Optional[List[dict]] = None
    images:         Optional[List[dict]] = None
    rag_mode:       Optional[str]        = None   # "off"|"auto"|"on"
    # KB scope (inline fallback for first turn before Chat row exists in DB)
    product_id:     Optional[str]        = None
    domain:         Optional[str]        = None
    spec_version:   Optional[str]        = None
    kb_doc_id:      Optional[str]        = None
    kb_doc_ids:     Optional[List[str]]  = None
    ephemeral:      bool                 = False
    mode:           Optional[str]        = None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/kb/ask", tags=["knowledge_base_ask"])
async def kb_ask_ai(
    q: KbQuestion,
    request: Request,
    authorization: Optional[str] = Header(default=None),
):
    """
    KB-dedicated ask endpoint.

    Identical retrieval + LLM behaviour to gateway.py's /ask when
    rag_mode="on"/"auto", but completely isolated from Chat-path changes.
    KB never goes through ainxt-api (Rust runtime) — always model_router.
    """

    # ── Correlation / tracing ──────────────────────────────────────────────
    request_id = (request.headers.get("x-client-request-id") or "").strip() or str(uuid.uuid4())
    set_request_id(request_id)
    set_correlation_id(request_id)
    _q_preview = (q.question or "").strip()[:120]
    logger.info(
        f"[kb_ask] request start | corr={request_id} "
    )
    start_time = time.time()

    # ── Platform kill-switch ───────────────────────────────────────────────
    try:
        from core.kv import get_kv
        from core.config import RDB_CACHE
        _ks_redis = get_kv(RDB_CACHE, decode_responses=True)
        if _ks_redis.get("platform:disabled") == "1":
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=503,
                content={"error": "platform_disabled",
                         "detail": "Platform is temporarily suspended by administrator."},
            )
    except Exception:
        pass

    # ── Auth: JWT → API key → 401 ──────────────────────────────────────────
    _user_id   = None
    _user_dept = ""
    _user_ctx: dict = {}
    _jwt_token = None

    if authorization and authorization.lower().startswith("bearer "):
        _jwt_token = authorization[7:].strip()
    else:
        _jwt_token = request.cookies.get("auth_token")

    if _jwt_token:
        # 1. Try JWT
        try:
            from auth.jwt_handler import decode_token as _decode
            from auth.dependencies import enrich_user_context as _enrich
            _payload = _decode(_jwt_token)
            if _payload:
                _payload   = _enrich(_payload)
                _user_id   = _payload.get("sub")
                _user_dept = _payload.get("department", "") or ""
                _user_ctx  = {
                    "user_id":     _user_id,
                    "user_role":   _payload.get("role", "user"),
                    "ad_level":    int(_payload.get("ad_level") or 6),
                    "department":  _user_dept,
                    "is_admin":    _payload.get("role") == "admin",
                    "can_approve": bool(_payload.get("can_approve", False)),
                    "org_id":      _payload.get("org_id", ""),
                    "session_id":  "",
                    "name":        _payload.get("name", ""),
                    "ad_username": _payload.get("ad_username", ""),
                    "email":       _payload.get("email", ""),
                }
        except Exception:
            pass

        # 2. JWT failed → try platform API key (IDE integrations)
        if not _user_id:
            try:
                from auth.api_key_auth import is_api_key as _is_api_key, resolve_api_key as _resolve_key
                if _is_api_key(_jwt_token):
                    _kp = _resolve_key(_jwt_token)
                    if _kp:
                        _user_id   = _kp["sub"]
                        _user_dept = _kp.get("department", "") or ""
                        _user_ctx  = {
                            "user_id":     _user_id,
                            "user_role":   _kp.get("role", "user"),
                            "ad_level":    int(_kp.get("ad_level") or 6),
                            "department":  _user_dept,
                            "is_admin":    _kp.get("role") == "admin",
                            "can_approve": bool(_kp.get("can_approve", False)),
                            "org_id":      _kp.get("org_id", ""),
                            "session_id":  "",
                            "name":        _kp.get("name", ""),
                            "ad_username": _kp.get("ad_username", ""),
                            "email":       _kp.get("email", ""),
                        }
            except Exception:
                pass

    if not _user_id:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized", "detail": "Valid JWT or platform API key required."},
        )

    # ── CLI detection ──────────────────────────────────────────────────────
    _is_cli = (
        getattr(q, "cli_mode", False)
        or getattr(request.state, "client_source", "platform") == "cli"
        or request.headers.get("x-ainxt-client", "").lower().startswith("cli")
    )

    # ── Product IDs from dept mapping (for scope ACL) ──────────────────────
    if _user_dept and _user_ctx:
        try:
            from core.kv import get_kv
            from core.config import RDB_CACHE
            _pid_redis = get_kv(RDB_CACHE, decode_responses=True)
            _pid_cache_key = f"dept:pids:{_user_dept}"
            _cached_pids = _pid_redis.get(_pid_cache_key)
            if _cached_pids:
                _user_ctx["product_ids"] = json.loads(_cached_pids)
            else:
                from db.database import SessionLocal as _PidPgSession
                from sqlalchemy import text as _pid_sql
                _pid_sess = _PidPgSession()
                try:
                    _pid_rows = _pid_sess.execute(
                        _pid_sql("SELECT product_id::text FROM dept_product_mappings WHERE department = :dept"),
                        {"dept": _user_dept},
                    ).fetchall()
                    _pids = [r[0] for r in _pid_rows]
                    _user_ctx["product_ids"] = _pids
                    _pid_redis.setex(_pid_cache_key, 3600, json.dumps(_pids))
                finally:
                    _pid_sess.close()
        except Exception:
            _user_ctx["product_ids"] = []

    # ── chat_id resolution ─────────────────────────────────────────────────
    _chat_id = q.chat_id or q.session_id or str(uuid.uuid4())
    set_chat_context(_user_id, _chat_id)
    set_span_id("kb_ask")
    logger.info(
        f"[kb_ask] context resolved | user_id={_user_id} chat_id={_chat_id}"
    )

    # ── rag_mode resolution ────────────────────────────────────────────────
    _rag_mode = (q.rag_mode or "").strip().lower()
    if _rag_mode not in {"off", "auto", "on"}:
        _rag_mode = ""

    # Load KB scope from Chat row (DB is authoritative; inline fields are fallback)
    _chat_scope_pid = None
    _chat_scope_dom = None
    _chat_scope_ver = None
    _chat_scope_did = None
    if q.chat_id:
        try:
            from db.database import SessionLocal as _RMSL
            from db.models import Chat as _RMChat
            _rmdb = _RMSL()
            try:
                _rm_row = _rmdb.query(_RMChat).filter(_RMChat.id == q.chat_id).first()
                if _rm_row is not None:
                    if not _rag_mode:
                        _stored = (getattr(_rm_row, "rag_mode", None) or "").strip().lower()
                        if _stored in {"off", "auto", "on"}:
                            _rag_mode = _stored
                    _chat_scope_pid = getattr(_rm_row, "product_id",   None)
                    _chat_scope_dom = getattr(_rm_row, "domain",       None)
                    _chat_scope_ver = getattr(_rm_row, "spec_version", None)
                    _chat_scope_did = getattr(_rm_row, "kb_doc_id",    None)
            finally:
                _rmdb.close()
        except Exception:
            pass

    # Inline-scope fallback for first turn (Chat row not yet created by chat_persist)
    if _chat_scope_pid is None and q.product_id:   _chat_scope_pid = q.product_id
    if _chat_scope_dom is None and q.domain:       _chat_scope_dom = q.domain
    if _chat_scope_ver is None and q.spec_version: _chat_scope_ver = q.spec_version
    if _chat_scope_did is None and q.kb_doc_id:    _chat_scope_did = q.kb_doc_id

    # Multi-doc selection from DocPickerCard disambiguation
    _chat_scope_doc_ids: list = [str(d) for d in (q.kb_doc_ids or []) if d]

    if not _rag_mode:
        _rag_mode = "on"   # /kb/ask always means KB mode

    # Bypass safety filters only when local model is explicitly selected in KB mode
    _bypass_model_raw = (q.model or "").strip().lower()
    _bypass_safety_filters = bool(
        _rag_mode in {"auto", "on"}
        and (_bypass_model_raw == "local" or _bypass_model_raw.startswith("local:"))
    )

    # Inject scope into _user_ctx so hybrid_search SQL ACL can filter
    if _user_ctx is not None and (_chat_scope_pid or _chat_scope_dom or _chat_scope_ver or _chat_scope_did):
        _scope_pid_str = str(_chat_scope_pid) if _chat_scope_pid else None
        _is_admin      = (_user_ctx.get("user_role") or "").lower() == "admin" or _user_ctx.get("is_admin")
        _allowed_pids  = set(_user_ctx.get("product_ids") or [])
        if _scope_pid_str and not _is_admin and _allowed_pids and _scope_pid_str not in _allowed_pids:
            logger.warning(
                f"[kb_ask] dropping chat scope — product_id={_scope_pid_str} not in "
                f"user's dept-mapped products (user={_user_id} dept={_user_dept})"
            )
        else:
            _scope_dict = {}
            if _scope_pid_str:    _scope_dict["product_id"]   = _scope_pid_str
            if _chat_scope_dom:   _scope_dict["domain"]       = _chat_scope_dom
            if _chat_scope_ver:   _scope_dict["spec_version"] = _chat_scope_ver
            if _scope_dict:
                _user_ctx["scope_filter"] = _scope_dict
            if _chat_scope_did:
                _user_ctx["kb_doc_id"] = str(_chat_scope_did)
            logger.info(
                f"[kb_ask] injected chat scope → scope_filter={_scope_dict} "
                f"kb_doc_id={_chat_scope_did}"
            )

    _ok_q, _errs_q, _san_q = validate_free_text(q.question)
    if not _ok_q:
        raise HTTPException(status_code=400, detail=_flatten_errors({"question": _errs_q}))
    q.question = _san_q

    original = q.question.strip()

    # ── Attachment context ─────────────────────────────────────────────────
    if q.attachment_ids:
        try:
            from db.database import SessionLocal
            from db.models import ChatAttachment
            _session = SessionLocal()
            try:
                attachments = _session.query(ChatAttachment).filter(
                    ChatAttachment.id.in_(q.attachment_ids)
                ).all()
                try:
                    _attach_cap = int(os.getenv("ASK_ATTACH_CHAR_CAP", "0") or "0")
                except Exception:
                    _attach_cap = 0
                blocks = []
                for a in attachments:
                    if not a.parsed_text:
                        continue
                    _truncated = _attach_cap > 0 and len(a.parsed_text) > _attach_cap
                    _ptext = a.parsed_text if _attach_cap <= 0 else a.parsed_text[:_attach_cap]
                    _warn = (
                        f"\n[NOTE: this file is {len(a.parsed_text):,} characters; only the first "
                        f"{_attach_cap:,} are shown here. Do not re-request this file.]\n"
                    ) if _truncated else ""
                    blocks.append(f"[File: {a.file_name}]{_warn}\n{_ptext}")
                if blocks:
                    original = "\n\n".join(blocks) + "\n\nUser question: " + original
            finally:
                _session.close()
        except Exception as _att_err:
            logger.warning(f"[kb_ask] attachment fetch failed: {_att_err}")

    # ── Model hint ─────────────────────────────────────────────────────────
    _model_hint  = q.model if q.model and q.model.lower() not in ("auto", "default", "") else None
    _local_model = q.local_model
    if _local_model and _model_hint is None:
        _model_hint = "local"

    # ── Agent context ──────────────────────────────────────────────────────
    _agent_system_prompt = None
    _agent_kb_namespace  = None
    if q.agent_id:
        try:
            from db.database import SessionLocal as _AgDB
            from db.models import AgentRecord as _AgRec
            _agdb = _AgDB()
            try:
                _ag = _agdb.query(_AgRec).filter(
                    _AgRec.name    == q.agent_id,
                    _AgRec.enabled == True,
                ).first()
                if _ag and _ag.system_prompt:
                    _agent_system_prompt = _ag.system_prompt.strip()
                if _ag:
                    _agent_kb_namespace = f"agent_kb:{q.agent_id}"
            finally:
                _agdb.close()
        except Exception as _ag_err:
            logger.warning(f"[kb_ask] Agent context lookup failed: {_ag_err}")

    # ── Compliance gate (PCI/PII + HardBlock) ─────────────────────────────
    from agents.compliance_engine import compliance_engine as _ce_ask
    _t_compliance = time.time()
    logger.info("[kb_ask] compliance check started")

    if _bypass_safety_filters:
        _ask_chk = {
            "blocked": False, "blocked_types": [], "findings": [],
            "redacted_text": original, "was_redacted": False, "redacted_types": [],
        }
        logger.info(
            f"[kb_ask] compliance SKIPPED — local model selected "
            f"| elapsed={time.time() - _t_compliance:.3f}s"
        )
    else:
        _ask_chk = _ce_ask.validate_input(original)
        if not _ask_chk.get("blocked"):
            logger.info(
                f"[kb_ask] compliance PASSED | elapsed={time.time() - _t_compliance:.3f}s"
            )

    # HardBlock engine
    _hb_findings = []
    _skip_hbe    = os.getenv("SKIP_NEMO_GUARDRAIL", "").strip().lower() in ("1", "true", "yes")
    _hbe_fail_open = os.getenv("HARDBLOCK_FAIL_OPEN", "").strip().lower() in ("1", "true", "yes")
    if not _bypass_safety_filters and not _skip_hbe:
        try:
            from agents.hardblock_engine import hardblock_engine as _hbe
            hb = _hbe.check(original, is_tool_result=False)
            if hb["blocked"]:
                _hb_findings.append({
                    "type": "HARDBLOCK", "value": hb["matched_phrases"],
                    "category": hb["category"], "score": hb.get("score", 0.0),
                    "severity": "CRITICAL", "blocked": True,
                })
        except Exception as _hbe_err:
            logger.error(f"[kb_ask] HardBlockEngine error → {_hbe_err}")
            if not _hbe_fail_open:
                _hb_findings.append({
                    "type": "HARDBLOCK_ENGINE_ERROR", "value": str(_hbe_err),
                    "category": "internal", "severity": "CRITICAL", "blocked": True,
                })

    _ask_chk_findings = _ask_chk.get("findings", []) or []
    _ask_chk_findings.extend(_hb_findings)
    _ask_chk["findings"] = _ask_chk_findings

    if _ask_chk.get("blocked") or _hb_findings:
        _ask_blocked  = _ask_chk.get("blocked_types", [])
        _block_detail = ", ".join(_ask_blocked) if _ask_blocked else "compliance policy"
        logger.warning(
            f"[kb_ask] compliance BLOCKED | types={_ask_blocked}"
        )

        def _compliance_block_stream():
            yield "data: " + json.dumps({"t": f"⛔ Request blocked by compliance policy: {_block_detail}"}) + "\n\n"
            yield "data: " + json.dumps({"__meta__": {
                "tokens": 0, "in_tok": 0, "out_tok": 0,
                "cost": 0.0, "model": "compliance-gate", "latency": 0.0,
            }}) + "\n\n"

        return StreamingResponse(
            _compliance_block_stream(),
            media_type="text/event-stream",
            headers={"X-Request-ID": request_id, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── PII mask → safe_question ───────────────────────────────────────────
    if _bypass_safety_filters:
        safe_question = original
    else:
        from gateway import mask_pii as _mask_pii
        safe_question = _ask_chk.get("redacted_text") or _mask_pii(original)

    # Agent system prompt injection (after compliance gate — avoids false positives)
    if _agent_system_prompt:
        original = (
            f"[AGENT INSTRUCTIONS — follow exactly]\n{_agent_system_prompt}\n\n"
            f"[USER QUESTION]\n{original}"
        )
        if _bypass_safety_filters:
            safe_question = original
        else:
            safe_question = _ask_chk.get("redacted_text") or _mask_pii(original)

    # ── History loading ────────────────────────────────────────────────────
    _messages: list = []
    _RAW_TURNS = 200
    _hist_source = "none"
    _t_history = time.time()

    # CLI client-side history (authoritative — bypasses DB lookup)
    if q.cli_messages and isinstance(q.cli_messages, list):
        for _cm in q.cli_messages:
            if isinstance(_cm, dict) and _cm.get("role") in ("user", "assistant") and _cm.get("content"):
                _cli_content_raw = str(_cm["content"])[:2000]
                _messages.append({"role": _cm["role"], "content": _cli_content_raw})
        if _messages:
            _hist_source = "cli"

    # Redis → Postgres fallback
    if not _messages:
        try:
            from memory.redis_memory import RedisMemory as _RMhist
            _rmh = _RMhist()
            _redis_hist = _rmh.get_conversation(_chat_id, limit=_RAW_TURNS)
            if _redis_hist:
                for _rm in _redis_hist:
                    _r_role = _rm.get("role", "")
                    if _r_role not in ("user", "assistant"):
                        continue
                    # KB context isolation: skip Generic (rag_mode="off") turns
                    _r_meta   = _rm.get("metadata") or {}
                    _r_origin = _r_meta.get("rag_mode")
                    if _r_origin and _r_origin == "off":
                        continue
                    _r_content = clean_for_history((_rm.get("content") or "").strip(), role=_r_role)
                    if not _r_content:
                        continue
                    if _r_role == "user":
                        _r_content = hist_redact(_r_content, _bypass_safety_filters, _ce_ask)
                    _messages.append({"role": _r_role, "content": _r_content})
                if _messages:
                    _hist_source = "redis"
        except Exception as _redis_hist_err:
            logger.debug(f"[kb_ask] Redis history fetch failed: {_redis_hist_err}")

    if not _messages:
        try:
            from db.database import SessionLocal as _HistDB
            from db.models import ChatMessage as _CM
            _hdb = _HistDB()
            try:
                _pg_hist = (
                    _hdb.query(_CM)
                    .filter(
                        _CM.chat_id == _chat_id,
                        _CM.role.in_(["user", "assistant"]),
                        _CM.rag_mode != "off",   # KB context isolation
                    )
                    .order_by(_CM.created_at.desc())
                    .limit(_RAW_TURNS)
                    .all()
                )
            finally:
                _hdb.close()
            for _m in reversed(_pg_hist):
                _cleaned = clean_for_history((_m.content or "").strip(), role=_m.role)
                if _cleaned:
                    if _m.role == "user":
                        _cleaned = hist_redact(_cleaned, _bypass_safety_filters, _ce_ask)
                    _messages.append({"role": _m.role, "content": _cleaned})
            if _messages:
                _hist_source = "postgres"
        except Exception as _pg_hist_err:
            logger.debug(f"[kb_ask] Postgres history fetch failed: {_pg_hist_err}")

    _hist_turns = len([m for m in _messages if m.get("role") == "assistant"])
    logger.info(
        f"[kb_ask] history loaded | turns={_hist_turns} source={_hist_source} "
        f"elapsed={time.time() - _t_history:.3f}s"
    )

    # Assemble current user message with memory instruction
    _system_preface = _MEMORY_INSTRUCTION
    _current_user_content = safe_question
    if _system_preface:
        if _messages:
            _messages[0]["content"] = _system_preface + "\n\n" + _messages[0]["content"]
        else:
            _current_user_content = _system_preface + "\n\n" + _current_user_content

    _messages.append({"role": "user", "content": _current_user_content})

    # ── Follow-up detection ────────────────────────────────────────────────
    _has_history = len([m for m in _messages if m.get("role") == "assistant"]) > 0
    _is_followup = is_followup_question(safe_question, _messages)
    _rag_query   = safe_question
    _bm25_query  = safe_question

    # ── Follow-up condenser (KB-only LLM call) ─────────────────────────────
    if _has_history and _rag_mode in {"auto", "on"}:
        try:
            from core.config import KB_FOLLOWUP_CONDENSE_ENABLED as _KB_CONDENSE_ON
            if _KB_CONDENSE_ON:
                from models.followup_condenser import condense_followup
                _t_condenser = time.time()
                _condensed = condense_followup(safe_question, _messages, chat_id=q.chat_id)
                if _condensed and _condensed.strip() and _condensed.strip() != safe_question.strip():
                    _rag_query   = _condensed.strip()
                    _bm25_query  = _rag_query
                    _is_followup = True
                    logger.info(
                        f"[kb_ask] followup condensed → {_rag_query[:150]!r} "
                        f"| elapsed={time.time() - _t_condenser:.3f}s"
                    )
                    try:
                        from core.trace_store import add_trace as _add_trace
                        _add_trace(request_id, f"followup=true standalone_q={_rag_query[:150]!r}")
                    except Exception:
                        pass
        except Exception as _condense_exc:
            logger.warning(f"[kb_ask] followup condense failed ({_condense_exc}) — using bare question")

    # ── L1 cache check ─────────────────────────────────────────────────────
    from gateway import cache_key as _cache_key
    _cache_key_val = _cache_key(safe_question, None, _model_hint, user_id=_user_id, rag_mode=_rag_mode)
    _cached = None
    _t_cache = time.time()
    try:
        from core.kv import get_kv
        from core.config import RDB_CACHE
        _l1_redis = get_kv(RDB_CACHE, decode_responses=True)
        _cached = _l1_redis.get(_cache_key_val)
    except Exception:
        pass

    if _cached:
        try:
            _data = json.loads(_cached)
            logger.info(
                f"[kb_ask] L1 cache HIT — returning cached answer "
                f"| elapsed={time.time() - _t_cache:.3f}s"
            )

            def _cached_stream():
                yield "data: " + json.dumps({"t": _data["answer"]}) + "\n\n"
                yield "data: " + json.dumps({"__meta__": {
                    "tokens": 0, "in_tok": 0, "out_tok": 0,
                    "cost": 0.0, "model": "cached",
                    "latency": round(time.time() - start_time, 3),
                    "source": "redis", "llm_used": False, "rag_mode": _rag_mode,
                }}) + "\n\n"

            return StreamingResponse(
                _cached_stream(),
                media_type="text/event-stream",
                headers={"X-Cache": "HIT", "X-Request-ID": request_id,
                         "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        except Exception:
            pass

    logger.info(f"[kb_ask] L1 cache MISS | elapsed={time.time() - _t_cache:.3f}s")

    # ── KB retrieval ───────────────────────────────────────────────────────
    _is_trivial_q     = bool(TRIVIAL_QUERY_RE.match(safe_question.strip()))
    _kb_probe_enabled = bool(q.voice_platform) or (_rag_mode in {"auto", "on"})

    _t_retrieval = time.time()
    logger.info(
        f"[kb_ask] retrieval started | rag_mode={_rag_mode!r} "
        f"is_followup={_is_followup} trivial={_is_trivial_q}"
    )

    _ns_redis = None
    try:
        from core.kv import get_kv
        from core.config import RDB_CACHE
        _ns_redis = get_kv(RDB_CACHE, decode_responses=True)
    except Exception:
        pass

    _kb_result = run_kb_retrieval(
        safe_question       = safe_question,
        rag_query           = _rag_query,
        bm25_query          = _bm25_query,
        is_trivial_q        = _is_trivial_q,
        kb_probe_enabled    = _kb_probe_enabled,
        runtime_will_handle = False,   # /kb/ask never uses the Rust runtime
        is_followup         = _is_followup,
        has_history         = _has_history,
        user_ctx            = _user_ctx,
        chat_scope_doc_ids  = _chat_scope_doc_ids,
        agent_kb_namespace  = _agent_kb_namespace,
        rag_mode            = _rag_mode,
        request_id          = request_id,
        redis_ns_client     = _ns_redis,
    )
    logger.info(
        f"[kb_ask] retrieval done | elapsed={time.time() - _t_retrieval:.3f}s "
        f"kb_hit={bool(_kb_result.docs_context)} sources={len(_kb_result.sources_meta)}"
    )

    # ── Disambiguation: return __clarify__ SSE if triggered ────────────────
    if _kb_result.disambig_payload:
        _dp = _kb_result.disambig_payload

        def _disambig_stream(
            _msg=_dp["message"],
            _cands=_dp["candidates"],
            _q=_dp["question"],
            _rm=_dp["rag_mode"],
        ):
            yield "data: " + json.dumps({
                "__clarify__": {
                    "question":     _q,
                    "message":      _msg,
                    "candidates":   _cands,
                    "multi_select": True,
                }
            }) + "\n\n"
            yield "data: " + json.dumps({
                "__meta__": {
                    "out_tok": 0, "in_tok": 0, "model": "kb-disambig",
                    "cost": 0.0, "latency": 0.0, "source": "kb_disambig",
                    "llm_used": False, "rag_mode": _rm,
                }
            }) + "\n\n"

        return StreamingResponse(
            _disambig_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": request_id},
        )

    _docs_context    = _kb_result.docs_context
    _fp_sources_meta = _kb_result.sources_meta

    # ── Model hint for routing ─────────────────────────────────────────────
    # Voice always uses "complex" (Claude) for best natural language quality.
    _fp_hint = "complex" if q.voice_platform else (_model_hint or "medium")

    # ── Detected language (for Kafka payload) ─────────────────────────────
    _detected_lang = "unknown"
    try:
        from core.lang_detect import detect_language as _detect_lang
        _detected_lang = _detect_lang(original)
    except Exception:
        pass

    # ── Async streaming generator ──────────────────────────────────────────
    async def _kb_stream():
        _full  = ""
        _gmeta = {"out_tok": 0, "in_tok": 0, "model": "auto", "cost": 0.0, "latency": 0.0}
        _gt0   = time.time()

        # Status SSE
        try:
            if _docs_context:
                yield "data: " + json.dumps({"status": f"Reading {len(_fp_sources_meta)} source(s)…"}) + "\n\n"
            else:
                yield "data: " + json.dumps({"status": "Thinking…"}) + "\n\n"
        except Exception:
            pass

        # ── Build _fp_messages ─────────────────────────────────────────────
        _fp_messages = list(_messages)
        # Strip empty-content history entries (same logic as gateway._general_stream)
        if len(_fp_messages) > 1:
            _fp_messages = [
                m for m in _fp_messages[:-1]
                if isinstance(m, dict) and str(m.get("content") or "").strip()
            ] + [_fp_messages[-1]]

        # KV-cache hoist for local models
        _LOCAL_KV_CACHE_HOIST = os.getenv("LOCAL_KV_CACHE_HOIST", "true").lower() == "true"
        _is_local_route = bool(_local_model or _fp_hint in ("local", "simple"))
        if _LOCAL_KV_CACHE_HOIST and _is_local_route:
            try:
                _kv_sys_content = _build_local_system_message(
                    agent_system_prompt = _agent_system_prompt or "",
                    cowork_role_prompt  = "",
                    cowork_memory       = "",
                    custom_about        = "",
                    custom_style        = "",
                    memory_facts        = [],
                    feedback_hint       = "",
                    user_name           = (q.user_name or ""),
                    tone_pfx            = "",
                    sensitive           = False,
                )
                if _kv_sys_content:
                    _fp_messages = [{"role": "system", "content": _kv_sys_content}] + _fp_messages
            except Exception as _kv_err:
                logger.debug(f"[kb_ask] KV-hoist skipped: {_kv_err}")

        # ── KB context redaction + prompt injection ────────────────────────
        # Retrieved KB chunks may contain PANs/card numbers/PII.
        # For cloud LLMs we MUST redact before injecting into the grounded prompt.
        _docs_ctx_final = _docs_context
        if _docs_ctx_final and not _bypass_safety_filters:
            try:
                _ctx_redacted, _ctx_types = _ce_ask.redact_text(_docs_ctx_final)
                if _ctx_types:
                    logger.info(f"[kb_ask] KB context redacted — types={_ctx_types}")
                _docs_ctx_final = _ctx_redacted
            except Exception as _ctx_red_err:
                logger.warning(f"[kb_ask] KB context redaction failed: {_ctx_red_err}")

        if q.voice_platform:
            # Voice: build grounded prompt with static context + pgvector hits
            _combined = _docs_ctx_final.strip()
            if _combined:
                _voice_prompt = (
                    "You are AiNxt, an AI assistant. Answer using ONLY the context below.\n\n"
                    "Context:\n" + _combined + "\n\nQuestion: " + safe_question
                )
            else:
                _voice_prompt = safe_question
            _fp_messages[-1] = {"role": "user", "content": _voice_prompt}
        elif _docs_ctx_final:
            # Use shared utility — same logic as gateway._general_stream
            _grounded = build_kb_grounded_prompt(
                safe_question      = safe_question,
                docs_ctx           = _docs_ctx_final,
                is_followup        = _is_followup,
                has_history        = _has_history,
                chat_scope_doc_ids = _chat_scope_doc_ids,
            )
            _fp_messages[-1] = {"role": "user", "content": _grounded}
        # else: _fp_messages[-1] is already the plain safe_question message

        # ── LLM call via model_router (NEVER ainxt-api) ────────────────────
        logger.info(
            f"[kb_ask] LLM dispatch | model_hint={_fp_hint!r} rag_mode={_rag_mode!r} "
            f"kb_hit={bool(_docs_ctx_final)} sources={len(_fp_sources_meta)}"
        )
        try:
            yield "data: " + json.dumps({"status": "Generating response…"}) + "\n\n"
        except Exception:
            pass

        # ── Concurrency semaphore (same cap as gateway.py Chat path) ─────────
        # Prevents KB requests from bypassing the LLM concurrency limit.
        # Acquire before the LLM call; release in the finally block below.
        _t_sem = time.time()
        try:
            from gateway import _LLM_SEMAPHORE as _kb_sem
            await _kb_sem.acquire()
            _kb_sem_acquired = True
        except Exception:
            _kb_sem_acquired = False
        _sem_wait = round(time.time() - _t_sem, 3)
        if _sem_wait > 0.05:
            logger.info(f"[kb_ask] semaphore wait | elapsed={_sem_wait}s")

        # ── Generation registry (cooperative stop via POST /chat/stop) ─────
        try:
            from core.generation_registry import (
                register   as _gen_register,
                deregister as _gen_deregister,
                should_stop as _gen_should_stop,
            )
            _gen_register(request_id)
            _gen_registered = True
        except Exception:
            _gen_registered = False
            # Fallback: define a no-op should_stop so the loop below still works
            def _gen_should_stop(_rid): return False  # noqa: E731

        _stream_meta     = {}
        _stream_thinking = ""
        _mem_buf         = ""
        _mem_buffering   = False
        _MEM_SENTINEL    = "<!--MEMORY:"
        _t_llm_start     = time.time()
        _ttft            = None   # time-to-first-token (seconds)

        try:
            from models.model_router import model_router as _mr

            async for _tok in _mr.async_stream(
                _fp_messages,
                model_hint  = _fp_hint,
                local_model = _local_model,
                precleared  = True,
                precleared_findings = _ask_chk.get("findings", []),
            ):
                if isinstance(_tok, dict):
                    _sm = _tok.get("__stream_meta__")
                    if _sm:
                        _stream_meta     = _sm
                        _stream_thinking = _sm.get("thinking", "") or ""
                    continue
                if _gen_should_stop(request_id):
                    break
                if not _tok:
                    continue
                if _ttft is None:
                    _ttft = round(time.time() - _t_llm_start, 3)
                    logger.info(f"[kb_ask] first token | ttft={_ttft}s")
                _full += _tok
                if _mem_buffering:
                    _mem_buf += _tok
                elif _MEM_SENTINEL in (_full[-len(_MEM_SENTINEL) - len(_tok):]):
                    _sentinel_pos    = _full.rfind(_MEM_SENTINEL)
                    _clean_part      = _full[:_sentinel_pos].rstrip()
                    _already_yielded = len(_full) - len(_tok) - len(_mem_buf)
                    _safe_prefix     = _clean_part[_already_yielded:]
                    if _safe_prefix:
                        yield "data: " + json.dumps({"t": _safe_prefix}) + "\n\n"
                    _mem_buf       = _full[_sentinel_pos:]
                    _mem_buffering = True
                else:
                    yield "data: " + json.dumps({"t": _tok}) + "\n\n"

        except Exception as _llm_err:
            logger.error(f"[kb_ask] LLM stream error: {_llm_err}")
            yield "data: " + json.dumps({"t": "⚠️ An error occurred while generating the response."}) + "\n\n"
        else:
            _llm_gen_elapsed = round(time.time() - _t_llm_start, 3)
            logger.info(
                f"[kb_ask] LLM generation done | elapsed={_llm_gen_elapsed}s "
                f"ttft={_ttft}s tokens={len(_full.split())}"
            )
        finally:
            # Release semaphore and deregister from generation registry
            # regardless of success, error, or client disconnect.
            if _kb_sem_acquired:
                try:
                    _kb_sem.release()
                except Exception:
                    pass
            if _gen_registered:
                try:
                    _gen_deregister(request_id)
                except Exception:
                    pass

        # ── Memory footer extraction (two-pass, matches gateway.py) ───────
        # Search _mem_buf first (the buffered tail after the sentinel was
        # detected). If the footer regex doesn't match there (e.g. the
        # closing --> ended up in _full after the buffer boundary), fall
        # back to searching _full so the footer is never silently missed.
        _piggybacked_memory: dict = {}
        _mem_footer_re = re.compile(r'\n?<!--MEMORY:(\{.*?\})-->\s*$', re.DOTALL)
        _mem_match = _mem_footer_re.search(_mem_buf or _full)
        if not _mem_match:
            _mem_match = _mem_footer_re.search(_full)
        if _mem_match:
            try:
                _piggybacked_memory = json.loads(_mem_match.group(1))
            except Exception:
                pass
            _full = _mem_footer_re.sub("", _full).rstrip()

        # ── Output redaction for history persistence ───────────────────────
        # Use shared utility — same logic as gateway._general_stream's _out_redact
        _full = out_redact(_full, _bypass_safety_filters, _rag_mode, _ce_ask)

        # ── Model metadata ─────────────────────────────────────────────────
        _gmeta["latency"]  = round(time.time() - _gt0, 3)
        _sentinel_in  = int(_stream_meta.get("input_tokens",  0) or _stream_meta.get("in_tok",  0) or 0)
        _sentinel_out = int(_stream_meta.get("output_tokens", 0) or _stream_meta.get("out_tok", 0) or 0)
        _prompt_chars = sum(len(str(m.get("content") or "")) for m in _fp_messages)
        _real_in  = _sentinel_in  if _sentinel_in  > 1 else max(int(_prompt_chars / 4), 1)
        _real_out = _sentinel_out if _sentinel_out > 0 else int(len(_full.split()) * 1.3)
        _gmeta["in_tok"]  = _real_in
        _gmeta["out_tok"] = _real_out
        _gmeta["model"]   = (
            _stream_meta.get("model_id")
            or _stream_meta.get("model_label")
            or _model_hint
            or "auto"
        )
        try:
            # Use the same _estimate_cost() as gateway.py — includes the
            # local-model catalog check via gateway_local_llm.is_local_model()
            # so in-house model costs are computed identically on both paths.
            from gateway import _estimate_cost as _kb_estimate_cost
            _gmeta["cost"] = _kb_estimate_cost(_gmeta["model"], _real_in, _real_out)
        except Exception:
            _gmeta["cost"] = 0.0

        # ── Persistence: Redis short-term memory ───────────────────────────
        _persist_redis = "skip"
        _persist_kafka = "skip"
        _t_persist = time.time()
        if _full:
            try:
                from memory.redis_memory import RedisMemory as _RM_fp
                _rms_fp = _RM_fp()
                _fp_mem_meta = {"rag_mode": _rag_mode}
                _rms_fp.save_message(_chat_id, "user",      safe_question,   metadata=_fp_mem_meta)
                _rms_fp.save_message(_chat_id, "assistant", _full[:2000],    metadata=_fp_mem_meta)
                _persist_redis = "ok"
            except Exception:
                _persist_redis = "error"

            # Pin message IDs so /ask/continue works
            import uuid as _uuid_fp
            _fp_user_msg_id = str(_uuid_fp.uuid4())
            _fp_ast_msg_id  = str(_uuid_fp.uuid4())
            _gmeta["message_id"]      = _fp_ast_msg_id
            _gmeta["user_message_id"] = _fp_user_msg_id

            # ── Persistence: Kafka (primary) — matches gateway.py payload exactly
            if not q.ephemeral:
                try:
                    import datetime as _dt_fp
                    from core.kafka_producer import produce as _kafka_produce
                    _kafka_produce("ainxt.chat_history", {
                        "chat_id":              _chat_id,
                        "user_id":              _user_id,
                        "question":             safe_question,
                        "answer":               _full,
                        "model":                _gmeta.get("model", ""),
                        "in_tok":               _gmeta.get("in_tok", 0),
                        "out_tok":              _gmeta.get("out_tok", 0),
                        "cost":                 _gmeta.get("cost", 0.0),
                        "latency":              _gmeta.get("latency"),
                        "language":             _detected_lang or "unknown",
                        "attachment_ids":       list(q.attachment_ids or []),
                        "project_id":           "",
                        "agent_id":             q.agent_id or "",
                        "title_hint":           safe_question[:80] if not q.chat_id else None,
                        "user_message_id":      _fp_user_msg_id,
                        "assistant_message_id": _fp_ast_msg_id,
                        "rag_mode":             _rag_mode,
                        "repo_filter":          None,
                        "ts":                   _dt_fp.datetime.utcnow().isoformat(),
                    }, key=_chat_id)
                    _persist_kafka = "ok"
                except Exception as _kp_err:
                    logger.error(f"[kb_ask] Kafka publish failed: {_kp_err}")
                    _persist_kafka = "error"
            else:
                _persist_kafka = "ephemeral"

            logger.info(
                f"[kb_ask] persisted | redis={_persist_redis} kafka={_persist_kafka} "
                f"elapsed={time.time() - _t_persist:.3f}s"
            )

            # ── Cross-chat user memory (piggybacked footer) ────────────────
            if (
                _piggybacked_memory.get("store") is True
                and _piggybacked_memory.get("summary", "").strip()
                and _user_id and _user_id not in ("", "default")
            ):
                _pb_summary     = _piggybacked_memory["summary"].strip()
                _pb_context_key = _piggybacked_memory.get("context_key", "").strip()
                _pb_model       = _gmeta.get("model", "")
                _pb_chat_id     = _chat_id

                def _save_xchat_memory(
                    _s=_pb_summary, _ck=_pb_context_key,
                    _m=_pb_model, _cid=_pb_chat_id,
                ):
                    try:
                        from memory.postgres_memory import PostgresMemory as _PM_fp
                        _PM_fp().save_user_memory(
                            _user_id, _s,
                            metadata={"model": _m, "chat_id": _cid},
                            rag_mode=_rag_mode,
                            source_repo=None,
                            context_hint=_ck,
                        )
                    except Exception as _xc_err:
                        logger.debug(f"[kb_ask] cross-chat memory save skipped: {_xc_err}")

                _mem_ctx = contextvars.copy_context()
                threading.Thread(
                    target=lambda: _mem_ctx.run(_save_xchat_memory), daemon=True
                ).start()

            # ── Rolling per-chat summary ───────────────────────────────────
            try:
                from memory.chat_summarizer import update_chat_summary as _ucs
                _sum_ctx = contextvars.copy_context()
                threading.Thread(
                    target=lambda: _sum_ctx.run(_ucs, _chat_id, safe_question, _full),
                    daemon=True,
                ).start()
            except Exception:
                pass

        # ── Budget tracking ────────────────────────────────────────────────
        try:
            from store.budget_store import (
                increment_usage as _bu_inc,
                get_usage_today as _bu_gut,
                get_budget      as _bu_gb,
            )
            _bu_tok = _gmeta["in_tok"] + _gmeta["out_tok"]
            _bu_inc(_user_id, tokens=_bu_tok, requests=0, cost_usd=_gmeta["cost"])
            if not _is_local_route:
                _bu_usage  = _bu_gut(_user_id)
                _bu_limits = _bu_gb(_user_id)
                _gmeta["budget"] = {
                    "tokens_today":   _bu_usage.get("tokens_used", 0),
                    "requests_today": _bu_usage.get("requests_made", 0),
                    "cost_today":     _bu_usage.get("cost_usd_spent", 0.0),
                }
                if _bu_limits:
                    _gmeta["budget"]["max_tokens_total"]   = _bu_limits.get("max_tokens_total", 0)
                    _gmeta["budget"]["max_requests_total"] = _bu_limits.get("max_requests_total", 0)
                    _gmeta["budget"]["max_cost_total"]     = _bu_limits.get("max_cost_usd_total", 0.0)
        except Exception:
            pass

        # ── ainxt.metrics audit row (matches gateway.py channel logic) ─────
        try:
            from core.time_utils import now_ist_iso as _now_ist_iso
            from core.kafka_producer import produce as _kafka_produce
            _gs_cs = getattr(request.state, "client_source", "platform")
            _gs_channel = (
                "CLI"        if _is_cli else
                "DESKTOP-CHAT" if _gs_cs == "desktop" else
                "WEB-CHAT"
            )
            _kafka_produce("ainxt.metrics", {
                "event":          "llm_cost",
                "request_id":     request_id,
                "user_id":        _user_id,
                "agent_id":       "orchestrator",
                "endpoint":       "/kb/ask",
                "source_channel": _gs_channel,
                "model":          _gmeta.get("model", ""),
                "input_tokens":   _gmeta.get("in_tok", 0),
                "output_tokens":  _gmeta.get("out_tok", 0),
                "total_tokens":   _gmeta.get("in_tok", 0) + _gmeta.get("out_tok", 0),
                "latency_ms":     _gmeta.get("latency", 0.0) * 1000,
                "cost_usd":       _gmeta.get("cost", 0.0),
                "product_id":     None,
                "timestamp":      _now_ist_iso(),
            })
        except Exception as _mu_err:
            logger.warning(f"[kb_ask] ainxt.metrics produce failed: {_mu_err}")

        # ── DocPickerCard source attribution ──────────────────────────────
        # When user selects N docs, sources_meta has chunks from all N docs.
        # Filter to only docs whose name appears in the LLM response so the
        # Sources panel shows only what actually contributed to the answer.
        # Falls back to all selected-doc sources if no names match.
        # NOTE: use _sources_to_show (new local) — never reassign _fp_sources_meta
        # inside this generator or Python treats it as local everywhere and raises
        # UnboundLocalError at the earlier reads (line ~801).
        _sources_to_show = _fp_sources_meta
        if _chat_scope_doc_ids and _fp_sources_meta and _full:
            _resp_lower = _full.lower()
            _contrib_ids: set = set()
            for _s in _fp_sources_meta:
                _dn = (_s.get("doc_name") or "").strip().lower()
                _did = _s.get("doc_id") or ""
                if _dn and any(
                    w in _resp_lower for w in _dn.split() if len(w) >= 5
                ):
                    _contrib_ids.add(_did)
            if _contrib_ids:
                _filtered = [s for s in _fp_sources_meta if s.get("doc_id") in _contrib_ids]
                if _filtered:
                    _sources_to_show = _filtered
                    logger.info(
                        f"[kb_ask] sources filtered to contributing docs | "
                        f"selected={len(_chat_scope_doc_ids)} "
                        f"contributing={len(_contrib_ids)} "
                        f"sources={len(_sources_to_show)}"
                    )

        # ── __meta__ SSE frame ─────────────────────────────────────────────
        _gmeta["source"]   = "kb_ask"
        _gmeta["llm_used"] = True
        _gmeta["rag_mode"] = _rag_mode
        if _sources_to_show:
            _gmeta["sources"]     = _sources_to_show
            _gmeta["chunk_count"] = len(_sources_to_show)
        # Coverage trace (populated by hybrid_retriever in _user_ctx)
        try:
            _cov_out = (_user_ctx or {}).get("_coverage_trace_out")
            if _cov_out:
                _gmeta["coverage_trace"] = _cov_out
        except Exception:
            pass
        if _stream_thinking:
            _gmeta["thinking"] = _stream_thinking[:8000]

        yield "data: " + json.dumps({"__meta__": _gmeta}) + "\n\n"

        logger.info(
            f"[kb_ask] request complete | "
            f"total={round(time.time() - start_time, 3)}s "
            f"llm={_gmeta['latency']}s "
            f"ttft={_ttft}s "
            f"model={_gmeta.get('model', 'auto')} "
            f"in_tok={_gmeta.get('in_tok', 0)} out_tok={_gmeta.get('out_tok', 0)} "
            f"kb_hit={bool(_docs_ctx_final)} sources={len(_sources_to_show)}"
        )

        # ── Eval: groundedness + relevance (fire-and-forget) ──────────────
        # kb_ask_router owns the entire KB streaming path since the KB
        # separation commit (2026-08-14). gateway.py's eval_answer_quality
        # call for knowledge_base is no longer reached for KB requests, so
        # eval rows were never written — causing the Eval Observatory to show
        # nothing when filtering by Knowledge Base.
        #
        # Bug-fixes applied here:
        #   1. session_id → _chat_id  (session_id is not defined in this scope;
        #      _chat_id is the resolved chat/session identifier used everywhere else)
        #   2. list(_docs_ctx_final) → [_docs_ctx_final]  (wrapping a string in
        #      list() produces a list of individual characters, not a list of chunks;
        #      eval_answer_quality expects a list of context strings)
        #   3. run_id is now passed (was silently omitted, leaving every KB eval
        #      row with run_id=NULL in the EvalResult table)
        try:
            import threading as _kb_eval_thread
            _kb_eval_q   = q.question
            _kb_eval_ans = _full
            # _docs_ctx_final is a single string of concatenated KB chunks.
            # Wrap it in a list so eval_answer_quality receives a list of
            # context strings (its expected type), not a list of characters.
            _kb_eval_ctx = [_docs_ctx_final] if _docs_ctx_final else []
            _kb_eval_sid = _chat_id   # FIX: was bare `session_id` (NameError)
            _kb_eval_rid = q.session_id or None   # run_id — best proxy available
            _kb_eval_mdl = _gmeta.get("model") or None
            def _run_kb_eval():
                try:
                    from core.evals import eval_engine as _ee
                    _ee.eval_answer_quality(
                        _kb_eval_q, _kb_eval_ans, _kb_eval_ctx,
                        session_id=_kb_eval_sid,
                        run_id=_kb_eval_rid,
                        platform="knowledge_base",
                        model=_kb_eval_mdl,
                    )
                except Exception:
                    pass
            _kb_eval_thread.Thread(
                target=_run_kb_eval, daemon=True, name="eval-kb-answer"
            ).start()
        except Exception:
            pass

        # ── Coach event (fire-and-forget) ──────────────────────────────────
        # eval_platform="knowledge_base" was missing — the ingestor fell back
        # to channel-based derivation which could resolve to "chat", causing
        # coach_prompt rows to appear under Chat instead of Knowledge Base in
        # the Eval Observatory Prompt Quality card.
        try:
            from core.coach_events import emit_coach_event
            _cs = getattr(request.state, "client_source", "platform")
            _ch = "web" if _cs == "platform" else ("mcp" if _cs.startswith("ide-") else _cs)
            emit_coach_event(
                user_id    = _user_id or "anonymous",
                channel    = _ch,
                model      = _gmeta["model"],
                prompt     = q.question,
                tokens_in  = _real_in,
                tokens_out = _real_out,
                cost_usd   = float(_gmeta.get("cost", 0.0)),
                latency_ms = int(_gmeta["latency"] * 1000),
                request_id = request_id,
                thread_id  = _chat_id,
                department = (_user_ctx or {}).get("department"),
                eval_platform= "knowledge_base",
            )
        except Exception:
            pass

    return StreamingResponse(
        _kb_stream(),
        media_type="text/event-stream",
        headers={
            "X-Request-ID":      request_id,
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
