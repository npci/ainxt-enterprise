# SPDX-License-Identifier: MIT
# ============================================================
# IDE INTEGRATION ROUTER
#
# Provides simplified /ide/* endpoints for IDE plugins:
#   VSCode · PyCharm · Kilo · Cline
#
# These are thin adapters that delegate to existing platform logic.
# All endpoints return JSON (no streaming) for IDE compatibility.
# ============================================================

import asyncio
import logging
import os
import time
import uuid
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from dataclasses import asdict

from auth.dependencies import get_current_user as _require_auth
from core.logger import set_client_source, set_request_id, set_span_id, set_correlation_id
from core.logger import logger, mask_email


# Set IDE_DEBUG=true in .env (or shell) to enable per-request DEBUG traces
# without turning on DEBUG for the entire platform.
# e.g.:  IDE_DEBUG=true uvicorn gateway:app ...
if os.getenv("IDE_DEBUG", "").lower() in ("1", "true", "yes"):
    logger.setLevel(logging.DEBUG)
    logger.info("[IDE] IDE_DEBUG=true — verbose request/response tracing enabled")

router = APIRouter(prefix="/ide", tags=["ide"])


# ── Request models ─────────────────────────────────────────────

class IDEAgentRun(BaseModel):
    agent:      str                        # agent name
    message:    str
    session_id: Optional[str] = None


class IDEChat(BaseModel):
    question:       str
    model:          Optional[str]       = None   # "claude"|"gpt"|"gemini"|None
    attachment_ids: Optional[List[str]] = []
    project_id:     Optional[str]       = None
    repo_filter:    Optional[str]       = None   # direct repo slug from Kilo Code / IDE plugin
    context_mode:   Optional[str]       = "auto" # "auto" | "off" — "off" disables RAG injection


class IDEWorkflowRun(BaseModel):
    workflow: str                          # workflow name


# ── Shared debug helper ────────────────────────────────────────

def _req_id() -> str:
    """Short 8-char request ID for correlating log lines within one request."""
    return uuid.uuid4().hex[:8]


def _tag_ide_source(request: Request) -> str:
    """Detect IDE variant from header and tag logger context."""
    client_hdr = request.headers.get("x-ainxt-client", "").lower()
    if "jetbrains" in client_hdr or "pycharm" in client_hdr or "intellij" in client_hdr:
        source = "ide-jetbrains"
    else:
        source = "ide-vscode"  # default for all IDE router traffic
    set_client_source(source)
    return source


def _log_divider(req_id: str, label: str) -> None:
    logger.debug("[IDE:%s] ─── %s", req_id, label)


# ── POST /ide/agent/run ────────────────────────────────────────

@router.post("/agent/run")
def ide_run_agent(body: IDEAgentRun, request: Request, _u: dict = Depends(_require_auth)):
    """
    Run a named agent and return the result as JSON.
    Equivalent to POST /agents/{name}/run but with a flat body shape
    that's easier to consume from IDE plugins.
    """
    req_id = _req_id()
    set_request_id(req_id)
    set_correlation_id(req_id)  # unconditional: avoid stale value on reused thread
    set_span_id("ide.agent_run")
    t_start = time.time()
    _ide_source = _tag_ide_source(request)

    # ── 1. Log full incoming request ──────────────────────────
    logger.info(
        "[IDE:%s] ▶ POST /ide/agent/run  user=%s  agent=%r  session=%s  client=%s",
        req_id, _u.get("email", "?"), body.agent, body.session_id or "-", _ide_source,
                                                  )
    logger.info(
        "[IDE:%s] PROMPT (full):\n%s",
        req_id, body.message,
    )

    # ── 2. DB lookup ───────────────────────────────────────────
    _log_divider(req_id, "DB lookup")
    from db.database import SessionLocal
    from db.models import AgentRecord
    from agents.agent_builder import AgentDefinition

    db = SessionLocal()
    agent_rec = None
    try:
        rec = db.query(AgentRecord).filter(AgentRecord.name == body.agent).first()
        if not rec:
            logger.warning("[IDE:%s] agent=%r not found in DB", req_id, body.agent)
            raise HTTPException(status_code=404, detail=f"Agent '{body.agent}' not found")
        if rec.status not in ("PRODUCTION", "APPROVED"):
            logger.warning(
                "[IDE:%s] agent=%r blocked — status=%r (not PRODUCTION/APPROVED)",
                req_id, body.agent, rec.status,
            )
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{body.agent}' is in '{rec.status}' state — only PRODUCTION or APPROVED agents can run.",
            )
        agent_rec = {
            "name":          rec.name,
            "description":   rec.description or "",
            "system_prompt": rec.system_prompt or "",
            "tools":         list(rec.tools or []),
            "skills":        list(rec.skills or []),
            "version":       rec.version or "1.0.0",
            "author":        rec.owner or "platform",
            "enabled":       rec.enabled,
            "status":        rec.status or "PRODUCTION",
            "created_by":    rec.created_by or "platform",
        }
        logger.debug(
            "[IDE:%s] agent found  status=%s  tools=%s  skills=%s",
            req_id, rec.status, rec.tools, rec.skills,
        )
        logger.debug(
            "[IDE:%s] agent system_prompt:\n%s",
            req_id, rec.system_prompt or "(none)",
                    )
    finally:
        db.close()

    # ── 3. Register & run agent ────────────────────────────────
    _log_divider(req_id, "AgentRunner.run()")
    from agents.agent_builder import AgentBuilder, AgentRunner
    builder = AgentBuilder()
    runner  = AgentRunner(builder)

    if builder.get(body.agent) is None:
        logger.debug("[IDE:%s] agent not in memory — registering from DB record", req_id)
        defn = AgentDefinition(
            name=agent_rec["name"],
            description=agent_rec["description"],
            system_prompt=agent_rec["system_prompt"],
            tools=agent_rec["tools"],
            skills=agent_rec["skills"],
            version=agent_rec["version"],
            author=agent_rec["author"],
            enabled=agent_rec["enabled"],
            status=agent_rec["status"],
            created_by=agent_rec["created_by"],
        )
        builder.create(defn)
    else:
        logger.debug("[IDE:%s] agent already in memory — skipping re-register", req_id)

    logger.debug("[IDE:%s] calling runner.run(agent=%r, message_len=%d)", req_id, body.agent, len(body.message))
    try:
        result = runner.run(body.agent, body.message, body.session_id)
    except Exception as exc:
        logger.exception("[IDE:%s] runner.run() raised: %s", req_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    latency_ms = int((time.time() - t_start) * 1000)
    result_dict = asdict(result)

    # ── 4. Log response ────────────────────────────────────────
    output_preview = str(result_dict.get("output", ""))[:300]
    logger.info(
        "[IDE:%s] ◀ /ide/agent/run  latency=%dms  success=%s  output_preview=%.300s",
        req_id, latency_ms, result_dict.get("success"), output_preview,
    )

    # ── Request audit (client_source telemetry; powers the 10x AiNxt-built gate) ──
    try:
        from core.request_audit import record_audit
        record_audit(
            user_id=_u.get("sub") or _u.get("email") or "",
            client_source=_tag_ide_source(request),
            endpoint="/ide/agent/run",
            request_id=req_id,
            email=_u.get("email", ""),
            model_used=str(body.agent or ""),
            latency_ms=latency_ms,
        )
    except Exception:
        pass

    return result_dict


# ── GET /ide/models ────────────────────────────────────────────

@router.get("/models")
def ide_get_models(_u: dict = Depends(_require_auth)):
    """
    Return all available models grouped by provider.
    Same data as GET /v1/all-models — used by IDE plugins to populate
    the model selector dropdown (VSCode, PyCharm, Kilo, Cline, etc.).

    Sourced from core.llm_provider_registry (the same admin-managed "LLM
    Providers" data GET /v1/all-models reads exclusively) — this endpoint used
    to build its Claude/OpenAI/Gemini groups from core.model_registry env
    vars, so a provider configured purely through the admin screen (DB row,
    no matching env var) never appeared here even though /v1/all-models
    showed it correctly.
    """
    logger.info("[IDE] GET /ide/models  user=%s", mask_email(_u.get("email", "?")))

    providers = [
        {
            "provider": "Auto",
            "models": [
                {"id": "auto", "label": "Auto (Routing)", "hint": "auto"},
            ],
        },
    ]

    try:
        from core.llm_provider_registry import get_enabled_models
        by_provider: dict = {}
        for m in get_enabled_models():
            if m["family"] == "ollama":
                continue  # local models are appended separately below
            by_provider.setdefault(m["provider_name"], []).append({
                "id": m["model_id"],
                "label": f"{m['display_name']} ({m['model_id']})",
                "hint": m["model_id"],
            })
        for name, entries in by_provider.items():
            providers.append({"provider": name, "models": entries})
    except Exception as exc:
        logger.warning("[IDE] /ide/models registry read failed: %s", exc)

    # Append in-house hosted models from the local LLM proxy
    try:
        from gateway_local_llm import get_local_gateway as _get_local_gw_ide
        _local_list = _get_local_gw_ide().list_models()
        if _local_list:
            providers.append({
                "provider": "Local (In-house)",
                "models": [
                    {"id": f"local:{m}", "label": f"Local: {m}", "hint": f"local:{m}"}
                    for m in _local_list
                ],
            })
            logger.debug("[IDE] /ide/models — local models: %s", _local_list)
    except Exception:
        pass

    total = sum(len(p["models"]) for p in providers)
    logger.debug("[IDE] /ide/models — returning %d providers, %d models total", len(providers), total)
    return {"providers": providers}


# ── POST /ide/chat ─────────────────────────────────────────────

@router.post("/chat")
async def ide_chat(body: IDEChat, request: Request, _u: dict = Depends(_require_auth)):
    """
    Non-streaming chat endpoint for IDE plugins.
    Runs PCI compliance checks and returns the model answer as JSON.
    Equivalent to POST /ask but synchronous (no SSE stream).
    """
    req_id = _req_id()
    set_request_id(req_id)
    set_correlation_id(req_id)  # unconditional: avoid stale value on reused thread
    set_span_id("ide.chat")
    t_start = time.time()

    # ── 1. Log full incoming prompt ────────────────────────────
    logger.info(
        "[IDE:%s] ▶ POST /ide/chat  user=%s  model=%s  project=%s  attachments=%d  prompt_len=%d",
        req_id,
        _u.get("email", "?"),
        body.model or "auto",
        body.project_id or "-",
        len(body.attachment_ids or []),
        len(body.question),
        )
    logger.info(
        "[IDE:%s] PROMPT (full):\n%s",
        req_id, body.question,
    )

    # ── 1a. Budget gate (defense-in-depth) ────────────────────
    # BudgetMiddleware is the primary gate, but it may skip if user-id
    # extraction from the token fails.  Here we use _u — the fully
    # authenticated user dict from Depends(_require_auth) — so this
    # check is authoritative and cannot be bypassed.
    # Local / in-house models carry no external API cost, so they are
    # budget-exempt here as well as in BudgetMiddleware.
    # _budget_uid: prefer sub (UUID) → email. Both JWT and API key payloads put
    # the user UUID in "sub". "id" does not exist in either payload shape.
    _CLOUD_IDE_ROUTER_PFX = ("gpt-", "claude-", "gemini-", "openai/", "anthropic/", "google/", "azure/")
    _ide_router_model = (body.model or "").lower().strip()
    _ide_router_is_inhouse = (
        bool(_ide_router_model)
        and _ide_router_model not in ("auto", "default")
        and not any(_ide_router_model.startswith(p) for p in _CLOUD_IDE_ROUTER_PFX)
    )
    _budget_uid = _u.get("sub") or _u.get("email") or ""
    if _budget_uid and not _ide_router_is_inhouse:
        try:
            from store.budget_store import check_budget as _check_budget
            _bres = _check_budget(_budget_uid)
            # Explicit is-not-True check: treats missing key as blocked (fail-closed)
            if _bres.get("allowed") is not True:
                logger.warning(
                    "[IDE:%s] BUDGET BLOCKED  user=%s  reason=%s",
                    req_id, _budget_uid, _bres.get("reason"),
                )
                try:
                    from store.inbox_store import publish_inbox_item
                    publish_inbox_item(
                        user_id=_budget_uid,
                        type="budget_alert",
                        title="Budget limit reached",
                        body=_bres.get("reason", "Budget exceeded") + " — request an increase from your admin.",
                        priority="High",
                    )
                except Exception:
                    pass
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "Budget limit exceeded",
                        "reason": _bres.get("reason", "Budget allocation exhausted"),
                        "code": "BUDGET_EXCEEDED",
                    },
                )
        except HTTPException:
            raise
        except Exception as _be:
            logger.error("[IDE:%s] budget check FAILED (fail-open): %s", req_id, _be)

    # ── 2. PCI compliance — input ──────────────────────────────
    _log_divider(req_id, "PCI compliance check — INPUT")
    t_compliance = time.time()
    from agents.compliance_engine import ComplianceEngine
    compliance = ComplianceEngine()
    check = compliance.check(body.question)
    logger.debug(
        "[IDE:%s] compliance input  blocked=%s  findings=%s  elapsed=%.0fms",
        req_id, check.get("blocked"), check.get("findings", []),
        (time.time() - t_compliance) * 1000,
        )
    if check.get("blocked"):
        blocked_types = [f['type'] for f in check.get('findings', []) if f.get('blocked')]
        logger.warning("[IDE:%s] INPUT BLOCKED by compliance  types=%s", req_id, blocked_types)
        raise HTTPException(
            status_code=422,
            detail=f"Input blocked by compliance: {blocked_types}",
        )

    # ── 3. Attachment expansion ────────────────────────────────
    question = body.question
    if body.attachment_ids:
        _log_divider(req_id, "attachment expansion")
        from db.database import SessionLocal
        from db.models import ChatAttachment
        db = SessionLocal()
        try:
            rows = db.query(ChatAttachment).filter(
                ChatAttachment.id.in_(body.attachment_ids)
            ).all()
            logger.debug(
                "[IDE:%s] attachments requested=%d  found=%d  ids=%s",
                req_id, len(body.attachment_ids), len(rows), body.attachment_ids,
            )
            if rows:
                _ATTACH_MAX_CHARS = int(os.getenv("IDE_ATTACH_MAX_CHARS", "8000"))
                for r in rows:
                    logger.debug(
                        "[IDE:%s] attachment  name=%r  text_len=%d  cap=%d",
                        req_id, r.file_name, len(r.parsed_text or ""), _ATTACH_MAX_CHARS,
                    )
                ctx = "\n\n".join(
                    f"[Attachment: {r.file_name}]\n{(r.parsed_text or '')[:_ATTACH_MAX_CHARS]}"
                    for r in rows
                )
                question = f"{ctx}\n\n{question}"
                logger.debug(
                    "[IDE:%s] prompt after attachment expansion  total_len=%d",
                    req_id, len(question),
                )
        finally:
            db.close()

    # ── 4. Model routing ───────────────────────────────────────
    _log_divider(req_id, "model routing")
    raw_hint = body.model or "auto"
    if raw_hint.startswith("local:"):
        model_hint = "local"
    else:
        _hint_map = {
            "auto":     "simple",   # Ollama (free/local); user must explicitly choose GPT/Claude
            "claude":   "complex",
            "haiku":    "haiku",
            "gpt":      "medium",
            "gpt-5.4":  "medium",
            "gpt-mini": "simple",
            "deep":     "deep",
            "gpt-5-5":  "deep",
            "opus":     "solution",
            "opus-4-8": "opus-4-8",
            "opus-5":   "opus-5",
            "sonnet-5": "sonnet-5",
            "gemini":   "vision",
            # Specific Gemini IDs pass through to model_router._HINT_MAP unchanged
            # so the correct model is selected (text/coding vs lite vs image).
            "gemini-3.5-flash":       "gemini-3.5-flash",
            "gemini-3.1-flash-lite":  "gemini-3.1-flash-lite",
            "gemini-3.1-flash-image": "gemini-3.1-flash-image",
        }
        model_hint = _hint_map.get(raw_hint, "simple")

    logger.info(
        "[IDE:%s] model routing  raw_hint=%r  →  model_hint=%r",
        req_id, raw_hint, model_hint,
    )

    # ── 5. Log the exact prompt going to the model ─────────────
    _log_divider(req_id, "prompt → model_router")
    logger.info(
        "[IDE:%s] PROMPT → model_router (hint=%s, len=%d):\n%s",
        req_id, model_hint, len(question), question,
    )

    # ── 5a. Incremental RAG injection ──────────────────────────
    # Resolve repo: explicit field takes priority, else derive from project_id.
    # 2 lean chunks (~3K chars) give Kilo Code targeted context so it skips
    # 5–8 file-read tool calls per task, cutting token spend without downgrading
    # model quality.  context_mode="off" lets the plugin bypass RAG when the
    # user is already providing the full file via attachment.
    _repo = body.repo_filter or None
    if not _repo and body.project_id:
        try:
            from store.projects_store import get_project as _gp
            _proj = _gp(body.project_id)
            _repo = (_proj or {}).get("repo_name") or None
        except Exception:
            pass

    if _repo and body.context_mode != "off":
        try:
            from models.hybrid_retriever import hybrid_retrieve_context as _hrc
            from models.classifier import classify_query_complexity as _cqc
            _cplx = _cqc(body.question)
            # IDE always uses 2 chunks and caps complexity at "medium" so that
            # hybrid_retriever never fires the query-expansion LLM call (complex-only).
            _ide_complexity = "simple" if _cplx == "simple" else "medium"
            _chunks = _hrc(body.question, _repo, complexity=_ide_complexity, max_chunks=2)
            if _chunks:
                _ctx_block = "\n\n".join(_chunks)
                question   = (
                    f"[Codebase context — {_repo}]\n{_ctx_block}\n\n"
                    f"[Question]\n{question}"
                )
                logger.info(
                    "[IDE:%s] RAG  repo=%s  chunks=%d  complexity=%s→%s",
                    req_id, _repo, len(_chunks), _cplx, _ide_complexity,
                )
        except Exception as _re:
            logger.debug("[IDE:%s] RAG skipped: %s", req_id, _re)

    # ── 5b. Hard context cap ───────────────────────────────────
    _MAX_CTX = int(os.getenv("IDE_MAX_CONTEXT_CHARS", "24000"))
    if len(question) > _MAX_CTX:
        logger.warning(
            "[IDE:%s] context trimmed  original=%d  limit=%d",
            req_id, len(question), _MAX_CTX,
        )
        question = question[:_MAX_CTX]

    # ── 6. Model call ──────────────────────────────────────────
    from models.model_router import model_router
    t_model = time.time()
    try:
        answer = await model_router.async_generate(question, model_hint=model_hint)
    except Exception as exc:
        logger.warning(
            "[IDE:%s] primary model failed (hint=%s) — falling back to GPT: %s",
            req_id, model_hint, exc,
        )
        if model_hint == "medium":
            # Already tried GPT — nothing left to fall back to
            logger.exception("[IDE:%s] GPT call also failed: %s", req_id, exc)
            raise HTTPException(status_code=500, detail=f"Model error: {exc}")
        try:
            answer = await model_router.async_generate(question, model_hint="medium")
            logger.info("[IDE:%s] GPT fallback succeeded", req_id)
        except Exception as fallback_exc:
            logger.exception("[IDE:%s] GPT fallback also failed: %s", req_id, fallback_exc)
            raise HTTPException(status_code=500, detail=f"Model error: {exc}")

    # Capture token usage immediately after generate() so that
    # _propagate_tokens() values are not overwritten by a subsequent request.
    input_tokens  = model_router.last_input_tokens
    output_tokens = model_router.last_output_tokens
    used_model    = model_router.last_model_label

    model_latency_ms = int((time.time() - t_model) * 1000)
    logger.info(
        "[IDE:%s] model responded  latency=%dms  answer_len=%d  "
        "input_tokens=%d  output_tokens=%d  model=%s",
        req_id, model_latency_ms, len(answer or ""),
        input_tokens, output_tokens, used_model,
    )
    logger.debug(
        "[IDE:%s] ANSWER (full):\n%s",
        req_id, answer or "(empty)",
                )

    # ── 6a. Track cost for budget enforcement ─────────────────
    # Without this, IDE calls were invisible to the budget store —
    # cost_usd_spent stayed 0.0 and the $30 cap never fired.
    try:
        from store.budget_store import increment_usage as _inc_usage
        from core.model_registry import MODEL_COST_PER_1M
        from models.model_router import model_router as _mr
        _uid     = _u.get("sub") or _u.get("email") or ""
        _in_tok  = getattr(_mr, "last_input_tokens",  0) or max(1, len(question) // 4)
        _out_tok = getattr(_mr, "last_output_tokens", 0) or max(1, len(answer or "") // 4)
        # last_model_id does not exist on model_router — use last_model_label.
        # The label is a display string like "GPT-5.4 (Coding) (gpt-5.4)"; local
        # models produce "Local (In-house) (...)" and must never be charged.
        _model_lbl = (getattr(_mr, "last_model_label", "") or "").lower()
        if "local" in _model_lbl:
            _cost = 0.0
        else:
            # Direct lookup by label, then scan for a known model-ID substring.
            # MODEL_COST_PER_1M keys are raw IDs (e.g. "gpt-5.4", "claude-sonnet-4-6").
            _rates = MODEL_COST_PER_1M.get(_model_lbl)
            if _rates is None:
                for _mid, _r in MODEL_COST_PER_1M.items():
                    if _mid.lower() in _model_lbl:
                        _rates = _r
                        break
            _rates = _rates or (2.00, 8.00)  # conservative default (gpt-5.4 rate)
            _cost  = round((_in_tok * _rates[0] + _out_tok * _rates[1]) / 1_000_000, 6)
        if _uid:
            _inc_usage(_uid, tokens=_in_tok + _out_tok, requests=0, cost_usd=_cost)
        logger.info(
            "[IDE:COST] req=%s user=%s model=%s in_tok=%d out_tok=%d cost_usd=$%.4f",
            req_id, _uid, _model_id or model_hint, _in_tok, _out_tok, _cost,
        )
        if _cost > 1.0:
            logger.warning(
                "[IDE:COST:ALERT] req=%s cost=$%.4f exceeds $1 — check context size",
                req_id, _cost,
            )
    except Exception as _be:
        logger.warning("[IDE:%s] budget increment failed: %s", req_id, _be)

    # ── 7. Eval (fire-and-forget) ──────────────────────────────
    try:
        from core.evals import eval_engine as _ee
        _ee.eval_answer_quality(question, answer or "", [])
    except Exception:
        pass

    # ── 8. Final response ──────────────────────────────────────
    total_latency_ms = int((time.time() - t_start) * 1000)
    logger.info(
        "[IDE:%s] ◀ /ide/chat  total_latency=%dms  model_latency=%dms  answer_len=%d  "
        "input_tokens=%d  output_tokens=%d",
        req_id, total_latency_ms, model_latency_ms,
        len(answer or ""), input_tokens, output_tokens,
    )

    # ── Request audit (client_source telemetry; powers the 10x AiNxt-built gate) ──
    try:
        from core.request_audit import record_audit
        record_audit(
            user_id=_budget_uid or _u.get("sub") or _u.get("email") or "",
            client_source=_tag_ide_source(request),
            endpoint="/ide/chat",
            request_id=req_id,
            email=_u.get("email", ""),
            question=body.question,
            model_used=str(locals().get("_model_id") or body.model or ""),
            tokens_in=int(locals().get("_in_tok", 0) or 0),
            tokens_out=int(locals().get("_out_tok", 0) or 0),
            latency_ms=total_latency_ms,
        )
    except Exception:
        pass

    return {
        "answer":        answer,
        "latency_ms":    total_latency_ms,
        "model":         used_model,
        "usage": {
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "total_tokens":  input_tokens + output_tokens,
        },
    }


# ── GET /ide/threads ───────────────────────────────────────────

@router.get("/threads")
def ide_list_threads(
    project_id: Optional[str] = None,
    _u: dict = Depends(_require_auth),
):
    """
    List threads. Equivalent to GET /threads.
    Returns [{id, title, status, label, created_at, ...}].
    """
    logger.info(
        "[IDE] GET /ide/threads  user=%s  project_id=%s",
        _u.get("email", "?"), project_id or "-",
    )
    from store.threads_store import list_threads
    threads = list_threads(project_id=project_id)
    logger.debug("[IDE] /ide/threads  returning %d threads", len(threads))
    return {"threads": threads}


# ── POST /ide/workflow/run ─────────────────────────────────────

@router.post("/workflow/run")
def ide_run_workflow(body: IDEWorkflowRun, _u: dict = Depends(_require_auth)):
    """
    Run a named workflow and return the result as JSON.
    Equivalent to POST /workflows/{name}/run.
    """
    req_id = _req_id()
    set_request_id(req_id)
    set_correlation_id(req_id)  # unconditional: avoid stale value on reused thread
    set_span_id("ide.workflow_run")
    t_start = time.time()

    logger.info(
        "[IDE:%s] ▶ POST /ide/workflow/run  user=%s  workflow=%r",
        req_id, _u.get("email", "?"), body.workflow,
    )

    # ── 1. DB lookup ───────────────────────────────────────────
    _log_divider(req_id, "DB lookup")
    from db.database import SessionLocal
    from db.models import WorkflowRecord
    from workflows.engine import workflow_engine, Workflow, WorkflowStep
    from mcp.registry import mcp_registry

    db = SessionLocal()
    try:
        rec = db.query(WorkflowRecord).filter(WorkflowRecord.name == body.workflow).first()
        if not rec:
            logger.warning("[IDE:%s] workflow=%r not found in DB", req_id, body.workflow)
            raise HTTPException(status_code=404, detail=f"Workflow '{body.workflow}' not found")
        defn = {
            "name":            rec.name,
            "description":     rec.description or "",
            "stop_on_failure": True,
            "steps":           rec.steps or [],
            "status":          rec.status or "PRODUCTION",
        }
        logger.debug(
            "[IDE:%s] workflow found  status=%s  steps=%d",
            req_id, rec.status, len(rec.steps or []),
        )
        for i, s in enumerate(rec.steps or []):
            logger.debug(
                "[IDE:%s]   step[%d]  id=%s  name=%r  type=%s  depends_on=%s",
                req_id, i, s.get("id"), s.get("name"), s.get("step_type"), s.get("depends_on"),
            )
    finally:
        db.close()

    # ── 2. Build workflow object ───────────────────────────────
    _log_divider(req_id, "build Workflow object")
    steps = []
    for s in defn.get("steps", []):
        tool_fn = None
        if s["step_type"] == "tool":
            tool_name = s["input"]
            tool_fn = lambda inp, tn=tool_name: mcp_registry.execute_tool(tn, question=inp).output
            logger.debug("[IDE:%s] step id=%s wired to tool=%r", req_id, s["id"], tool_name)
        steps.append(WorkflowStep(
            id=s["id"],
            name=s["name"],
            step_type=s["step_type"],
            input=s["input"],
            depends_on=s.get("depends_on", []),
            tool_fn=tool_fn,
        ))

    wf = Workflow(
        name=defn["name"],
        description=defn.get("description", ""),
        stop_on_failure=defn.get("stop_on_failure", True),
        steps=steps,
    )

    # ── 3. Execute ─────────────────────────────────────────────
    _log_divider(req_id, "workflow_engine.run()")
    logger.info("[IDE:%s] executing workflow=%r  steps=%d", req_id, wf.name, len(steps))
    try:
        result = workflow_engine.run(wf)
    except Exception as exc:
        logger.exception("[IDE:%s] workflow_engine.run() raised: %s", req_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    latency_ms = int((time.time() - t_start) * 1000)
    result_dict = asdict(result)
    logger.info(
        "[IDE:%s] ◀ /ide/workflow/run  latency=%dms  success=%s",
        req_id, latency_ms, result_dict.get("success"),
    )
    logger.debug("[IDE:%s] workflow result: %s", req_id, result_dict)


# ── GET /ide/repo/resolve ─────────────────────────────────────
#
# Kilo Code / IDE plugins call this at workspace open to map their local
# git remote URL (or folder name) to the platform's repo_filter slug.
#
# Resolution order:
#   1. Exact git_url match (most reliable — set at index time)
#   2. Normalised URL match: strip .git suffix, compare lower-case
#   3. Folder-name slug match: last segment of URL or repo_name contains folder_name
#
# Returns:
#   { repo_filter, status, project_id, indexed_at }   on success
#   404  when no match found (plugin should prompt user to index the repo first)

@router.get("/repo/resolve")
def ide_repo_resolve(
    git_url:     Optional[str] = None,
    folder_name: Optional[str] = None,
    _u: dict = Depends(_require_auth),
):
    """
    Resolve a workspace git remote URL (or folder name) to the platform repo_filter slug.

    Called by IDE plugins at workspace open.  The plugin reads .git/config to get
    the remote URL, then calls this endpoint to get the correct repo_filter for RAG.

    Query params (at least one required):
      git_url     — git remote origin URL (https:// or git@)
      folder_name — workspace folder name (fallback when git_url not available)

    Returns JSON:
      { found: true, repo_filter, status, project_id, indexed_at }
      { found: false, hint: "index this repo first" }
    """
    if not git_url and not folder_name:
        raise HTTPException(
            status_code=400,
            detail="At least one of git_url or folder_name is required",
        )

    try:
        from db.database import engine
        from sqlalchemy import text as _sql

        with engine.connect() as conn:
            # ── Pass 1: exact git_url match ───────────────────────────────────
            row = None
            if git_url:
                clean_url = git_url.strip().rstrip("/")
                row = conn.execute(_sql("""
                    SELECT repo_name, status, git_url, completed_at
                    FROM repo_index_status
                    WHERE git_url = :url
                    ORDER BY completed_at DESC NULLS LAST
                    LIMIT 1
                """), {"url": clean_url}).fetchone()

            # ── Pass 2: normalised URL match (strip .git, lower-case) ─────────
            if not row and git_url:
                norm = git_url.strip().lower().rstrip("/").removesuffix(".git")
                row = conn.execute(_sql("""
                    SELECT repo_name, status, git_url, completed_at
                    FROM repo_index_status
                    WHERE lower(git_url) = :norm
                       OR lower(regexp_replace(git_url, '\\.git$', '', 'i')) = :norm
                    ORDER BY completed_at DESC NULLS LAST
                    LIMIT 1
                """), {"norm": norm}).fetchone()

            # ── Pass 3: folder_name slug match ────────────────────────────────
            if not row and folder_name:
                slug = folder_name.strip().lower().replace("-", "_").replace(" ", "_")
                row = conn.execute(_sql("""
                    SELECT repo_name, status, git_url, completed_at
                    FROM repo_index_status
                    WHERE lower(repo_name) = :slug
                       OR lower(repo_name) LIKE :slug_pct
                    ORDER BY completed_at DESC NULLS LAST
                    LIMIT 1
                """), {"slug": slug, "slug_pct": f"%{slug}%"}).fetchone()

        if not row:
            logger.info(
                "[IDE] /ide/repo/resolve  NOT FOUND  git_url=%r  folder=%r  user=%s",
                git_url, folder_name, _u.get("email", "?"),
            )
            return {
                "found": False,
                "hint": (
                    "Repository not indexed on this platform. "
                    "Go to Codebase Manager and index the repo first, "
                    "or set repo_filter manually in IDE settings."
                ),
            }

        repo_name = row.repo_name
        # Repo filter for pgvector queries uses "repo_" prefix convention
        repo_filter = repo_name if repo_name.startswith("repo_") else f"repo_{repo_name}"

        # Resolve optional project_id from projects table
        project_id: Optional[str] = None
        try:
            with engine.connect() as conn2:
                proj_row = conn2.execute(_sql("""
                    SELECT id FROM projects
                    WHERE repo_name = :rn OR repo_name = :rn2
                    LIMIT 1
                """), {"rn": repo_name, "rn2": repo_name.removeprefix("repo_")}).fetchone()
                if proj_row:
                    project_id = str(proj_row.id)
        except Exception:
            pass

        indexed_at = row.completed_at.isoformat() if row.completed_at else None
        logger.info(
            "[IDE] /ide/repo/resolve  FOUND  git_url=%r  repo_filter=%r  status=%s  user=%s",
            git_url, repo_filter, row.status, _u.get("email", "?"),
        )
        return {
            "found":       True,
            "repo_filter": repo_filter,
            "repo_name":   repo_name,
            "status":      row.status,
            "project_id":  project_id,
            "indexed_at":  indexed_at,
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[IDE] /ide/repo/resolve failed: %s", exc)
        raise HTTPException(status_code=500, detail="Repo resolution failed")

    return result_dict
