# SPDX-License-Identifier: MIT
# ============================================================
# PROJECTS ROUTER — /projects
# ============================================================

import threading
import uuid
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Header, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.logger import logger, set_request_id, set_chat_context, bind_context, set_span_id
from auth.dependencies import get_current_user
from core.model_registry import (
    OPENAI_CODING_MODEL as _OPENAI_CODING,
    MODEL_COST_PER_1M as _MODEL_COST_PER_1M,
)
from core.security_validation import (
    validate_security,
    validate_description,
    validate_product_name,
    validate_repo_name,
)

router = APIRouter(tags=["projects"])


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    repo_name: str = ""
    team: List[str] = []
    custom_instructions: str = ""
    tags: List[str] = []


class ProjectAsk(BaseModel):
    question: str
    session_id: Optional[str] = None
    attachment_ids: Optional[List[str]] = None


@router.get("/projects")
def list_projects(current_user=Depends(get_current_user)):
    from store.projects_store import list_projects
    from auth.rbac import is_admin as _is_admin
    user_id      = str(current_user.get("id", "") or current_user.get("sub", ""))
    _admin       = _is_admin(current_user)
    user_dept    = current_user.get("department") or ""
    all_projects = list_projects(user_id=user_id, is_admin=_admin, department=user_dept)
    return {"projects": all_projects}


@router.post("/projects")
def create_project(body: ProjectCreate, current_user=Depends(get_current_user)):
    from store.projects_store import create_project, get_project_by_name
    existing = get_project_by_name(body.name)
    if existing:
        raise HTTPException(status_code=409, detail=f"Project '{body.name}' already exists")
    
    # Validate all inputs for XSS/SQL injection
    # Validate name
    name_result = validate_product_name(body.name)
    if not name_result[0]:
        raise HTTPException(status_code=400, detail=name_result[1][0] if name_result[1] else "Invalid name")
    
    # Validate description
    desc_result = validate_description(body.description)
    if not desc_result[0]:
        raise HTTPException(status_code=400, detail=desc_result[1][0] if desc_result[1] else "Invalid description")
    
    # Validate custom_instructions
    instr_result = validate_description(body.custom_instructions)
    if not instr_result[0]:
        raise HTTPException(status_code=400, detail=instr_result[1][0] if instr_result[1] else "Invalid custom instructions")
    
    # Validate repo_name if provided
    if body.repo_name:
        repo_result = validate_repo_name(body.repo_name)
        if not repo_result[0]:
            raise HTTPException(status_code=400, detail=repo_result[1][0] if repo_result[1] else "Invalid repo name")
    
    data = body.dict()
    data["created_by"] = str(current_user.get("id", "") or current_user.get("sub", ""))
    data["department"]  = current_user.get("department") or ""
    project = create_project(data)
    return {"success": True, "project": project}


def _assert_project_visible(p: dict, current_user: dict) -> None:
    """Raise 404 unless `current_user` may see this project.

    Same creator/department rule `list_projects()` and `GET /projects/{id}`
    already enforce — factored out so every endpoint that reads a project by
    id (update, delete, ask) applies it too, instead of trusting the id alone
    once past `get_current_user`. A 404 (not 403) is used so a non-visible
    project's existence isn't revealed by the status code either.
    """
    from auth.rbac import is_admin as _is_admin
    if _is_admin(current_user):
        return
    user_id = str(current_user.get("id", "") or current_user.get("sub", ""))
    if p.get("created_by") != user_id:
        raise HTTPException(status_code=404, detail="Project not found")
    user_dept = current_user.get("department") or ""
    proj_dept = p.get("department") or ""
    if proj_dept and proj_dept != user_dept:
        raise HTTPException(status_code=404, detail="Project not found")


@router.get("/projects/{project_id}")
def get_project(project_id: str, current_user=Depends(get_current_user)):
    # SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    # this endpoint previously had no auth dependency and returned ANY
    # project (name, description, repo, custom instructions, department,
    # creator) to any anonymous caller who guessed/enumerated an id.
    # Fix, in two parts:
    #  1. Added `current_user=Depends(get_current_user)` as a function
    #     parameter so FastAPI rejects unauthenticated requests with 401.
    #  2. `_assert_project_visible()` below, which replicates the exact
    #     creator/department visibility rule the sibling GET /projects
    #     (list) endpoint already enforces in `list_projects()` — a
    #     non-admin caller now gets a 404 (not the project) unless they
    #     are its creator or it's in their own department.
    from store.projects_store import get_project
    p = get_project(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    _assert_project_visible(p, current_user)
    return p

@router.get("/projects/{project_id}/messages")
def get_project_messages(
    project_id: str,
    limit: int = 60,
    current_user=Depends(get_current_user),
):
    """
    Return the server-side chat history for a project/user pair.
    Replaces localStorage as the source of truth (Option B).
    Messages are ordered oldest-first for display in the chat panel.
    """
    from store.workspace_messages_store import get_messages as _get_msgs
    user_id = str(current_user.get("id", "") or current_user.get("sub", ""))
    messages = _get_msgs(project_id=project_id, user_id=user_id, limit=limit)
    return {"messages": messages}

@router.put("/projects/{project_id}")
def update_project(project_id: str, body: ProjectCreate, current_user=Depends(get_current_user)):
    from store.projects_store import get_project, update_project
    p = get_project(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    # 404 first for a project the caller can't even see (different dept,
    # not the creator) — avoids confirming its existence via a 403 — then
    # 403 for a project they can see but don't own.
    _assert_project_visible(p, current_user)
    user_id = str(current_user.get("id", "") or current_user.get("sub", ""))
    if current_user.get("role") != "admin" and p.get("created_by") and p["created_by"] != user_id:
        raise HTTPException(status_code=403, detail="Only the project creator or admin can update this project")
    
    # Validate all inputs for XSS/SQL injection
    # Validate name
    name_result = validate_product_name(body.name)
    if not name_result[0]:
        raise HTTPException(status_code=400, detail=name_result[1][0] if name_result[1] else "Invalid name")
    
    # Validate description
    desc_result = validate_description(body.description)
    if not desc_result[0]:
        raise HTTPException(status_code=400, detail=desc_result[1][0] if desc_result[1] else "Invalid description")
    
    # Validate custom_instructions
    instr_result = validate_description(body.custom_instructions)
    if not instr_result[0]:
        raise HTTPException(status_code=400, detail=instr_result[1][0] if instr_result[1] else "Invalid custom instructions")
    
    # Validate repo_name if provided
    if body.repo_name:
        repo_result = validate_repo_name(body.repo_name)
        if not repo_result[0]:
            raise HTTPException(status_code=400, detail=repo_result[1][0] if repo_result[1] else "Invalid repo name")
    
    updated = update_project(project_id, body.dict())
    return {"success": True, "project": updated}


@router.delete("/projects/{project_id}")
def delete_project(project_id: str, current_user=Depends(get_current_user)):
    from store.projects_store import get_project, delete_project
    p = get_project(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    _assert_project_visible(p, current_user)
    user_id = str(current_user.get("id", "") or current_user.get("sub", ""))
    if current_user.get("role") != "admin" and p.get("created_by") and p["created_by"] != user_id:
        raise HTTPException(status_code=403, detail="Only the project creator or admin can delete this project")
    delete_project(project_id)
    # Issue 1 fix: cascade delete all workspace_messages for this project.
    # Fire-and-forget — user does not need to wait for message cleanup.
    def _bg_delete_messages(_pid=project_id):
        try:
            from store.workspace_messages_store import delete_project_messages
            delete_project_messages(_pid)
        except Exception as _e:
            logger.debug(f"Project delete: workspace_messages cleanup failed: {_e}")
    threading.Thread(target=_bg_delete_messages, daemon=True).start()
    return {"success": True}


@router.post("/projects/{project_id}/ask")
def ask_project(
    project_id: str,
    body: ProjectAsk,
    current_user=Depends(get_current_user),
):
    from store.projects_store import get_project
    from store.budget_store import check_budget

    # ── Tracing: generate and bind request / chat / user IDs ─────────────────
    # These are set on the request thread via threading.local() so every
    # logger.info() call in this handler carries them automatically.
    # They are also captured as closure variables and re-applied at the top
    # of response_stream() because FastAPI iterates the generator on a
    # different thread where the thread-local context would otherwise be empty.
    chat_id = str(uuid.uuid4())
    request_id = body.session_id or str(uuid.uuid4())   # stable per conversation
    user_id    = str(current_user.get("id", "") or current_user.get("sub", ""))

    set_request_id(request_id)
    set_chat_context(user_id, chat_id)
    bind_context(correlation_id=request_id)
    set_span_id("projects.ask")
    # ─────────────────────────────────────────────────────────────────────────

    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # Without this, any authenticated user could ask questions against any
    # other user's/department's project id and have its name, repo,
    # description and custom_instructions injected into the LLM context and
    # reflected back in the answer — the same visibility rule GET enforces.
    _assert_project_visible(project, current_user)

    # Budget check — always enforced now that user_id is guaranteed from JWT
    result = check_budget(user_id)
    if not result.get("allowed", True):
        raise HTTPException(
            status_code=429,
            detail={"error": "Budget exceeded", "reason": result["reason"]},
        )

    # Build scoped question — always inject project + repo context so the
    # orchestrator never classifies a project-scoped question as "general"
    # and skips RAG retrieval.
    repo_filter = project.get("repo_name") or None
    question = body.question

    _proj_ctx_parts = [f"[Project: {project['name']}]"]
    if repo_filter:
        _proj_ctx_parts.append(f"[Codebase: {repo_filter}]")
    if project.get("description"):
        _proj_ctx_parts.append(f"[Description: {project['description']}]")
    _proj_prefix = " ".join(_proj_ctx_parts)
    question = f"{_proj_prefix}\n\n{question}"

    if project.get("custom_instructions"):
        question = f"{project['custom_instructions']}\n\n{question}"

    # ── Inject uploaded document content ─────────────────────────────────────
    # Fetch parsed text from chat_attachments and prepend to question
    if body.attachment_ids:
        try:
            from db.database import SessionLocal
            from db.models import ChatAttachment
            db = SessionLocal()
            try:
                attachments = db.query(ChatAttachment).filter(
                    ChatAttachment.id.in_(body.attachment_ids)
                ).all()
                if attachments:
                    doc_blocks = []
                    for att in attachments:
                        if att.parsed_text:
                            # Limit each document to 10K chars to avoid token overflow
                            doc_text = att.parsed_text[:10000]
                            doc_blocks.append(f"[File: {att.file_name}]\n{doc_text}")
                    if doc_blocks:
                        doc_context = "\n\n".join(doc_blocks)
                        question = f"{doc_context}\n\n{question}"
                        logger.info(f"ProjectAsk: injected {len(doc_blocks)} document(s) into prompt")
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"ProjectAsk: failed to load attachments: {e}")

    session_id = body.session_id or str(uuid.uuid4())
    raw_question = body.question  # bare user question — for compliance + history save

    # ── Inject conversation history from workspace_messages (Option B) ────────
    # Replaces postgres_memory (conversations table) as the history source.
    # project_id is the stable key — history persists across page reloads and
    # is scoped per-user so teammates don't see each other's messages.
    orch_question = question
    try:
        from store.workspace_messages_store import get_history_for_injection as _get_hist
        _hist = _get_hist(project_id=project_id, user_id=user_id, limit=6)
        if _hist:
            _lines = []
            for _m in _hist:
                _r = "User" if _m["role"] == "user" else "Assistant"
                _lines.append(f"{_r}: {_m['content']}")
            orch_question = (
                "[Conversation context]\n"
                + "\n".join(_lines)
                + "\n\n[Current question]\n"
                + question
            )
    except Exception as _he:
        logger.debug(f"ProjectAsk: history inject failed: {_he}")

    def response_stream():
        # ── Re-seed thread-local logging context on the streaming thread ──────
        # FastAPI iterates this generator on a thread different from the one
        # that executed ask_project(), so threading.local() values set above
        # are invisible here.  Re-applying them from closure vars ensures
        # every log line inside response_stream() carries the correct
        # request_id, chat_id, and user_id.
        set_request_id(request_id)
        set_chat_context(user_id, chat_id)
        bind_context(correlation_id=request_id)
        set_span_id("projects.ask")
        # ──────────────────────────────────────────────────────────────────────

        import time, json as _json
        start = time.time()
        full = ""
        local_agent = None
        try:
            from agents.orchestrator import OrchestratorAgent
            local_agent = OrchestratorAgent()
            for token in local_agent.run(
                orch_question,
                repo_filter,
                raw_question=raw_question,
            ):
                if token:
                    full += token
                    yield "data: " + _json.dumps({"t": token}) + "\n\n"
        except Exception as e:
            logger.warning(f"ProjectAsk: orchestrator failed — falling back to GPT direct: {e}")
            try:
                from models.model_router import model_router as _fallback_mr
                for _token in _fallback_mr.stream(orch_question, model_hint="medium"):
                    # Skip dict sentinel (see model_router.stream docstring)
                    if isinstance(_token, dict):
                        continue
                    if _token:
                        full += _token
                        yield "data: " + _json.dumps({"t": _token}) + "\n\n"
            except Exception as _fe:
                logger.error(f"ProjectAsk: GPT fallback also failed: {_fe}")
                yield "data: " + _json.dumps({"t": f"\nError: {e}"}) + "\n\n"
        finally:
            latency = round(time.time() - start, 2)
            try:
                from models.model_router import model_router as _mr
                # Use snapshotted label (set before eval threads start) — avoids race condition
                model   = getattr(local_agent, "last_run_model_label", None) or getattr(_mr, "last_model_label", _OPENAI_CODING)
                _real_in  = getattr(_mr, "last_input_tokens",  0) or 0
                _real_out = getattr(_mr, "last_output_tokens", 0) or 0
                if _real_in > 0 or _real_out > 0:
                    in_tok  = _real_in
                    out_tok = _real_out
                else:
                    in_tok  = int(len(question.split()) * 1.3)
                    out_tok = int(len(full.split()) * 1.3)
            except Exception:
                model   = _OPENAI_CODING
                in_tok  = int(len(question.split()) * 1.3)
                out_tok = int(len(full.split()) * 1.3)

            # Local/Ollama models are free — check before applying paid rates
            _ml = (model or "").lower()
            if "ollama" in _ml or "local" in _ml or "llama" in _ml:
                cost = 0.0
            else:
                rates = _MODEL_COST_PER_1M.get(model, (2.00, 8.00))
                cost  = round((in_tok * rates[0] + out_tok * rates[1]) / 1_000_000, 6)

            meta = {
                "chat_id":  chat_id,
                "tokens":   in_tok + out_tok,
                "in_tok":   in_tok,
                "out_tok":  out_tok,
                "cost":     cost,
                "model":    model,
                "latency":  latency,
            }
            yield "data: " + _json.dumps({"__meta__": meta}) + "\n\n"

            # Issue 2 fix: fire-and-forget save with full metadata.
            # Launched here (in finally) so model/cost/token values are computed.
            _msgs_to_save = [{"role": "user", "content": raw_question}]
            if full:
                _msgs_to_save.append({
                    "role":       "assistant",
                    "content":    full,
                    "modelLabel": model,
                    "costUsd":    cost,
                    "latency":    latency,
                    "inTok":      in_tok,
                    "outTok":     out_tok,
                })

            def _save_workspace_messages(_pid=project_id, _uid=user_id, _msgs=_msgs_to_save):
                try:
                    from store.workspace_messages_store import save_messages as _save
                    _save(project_id=_pid, user_id=_uid, messages=_msgs)
                except Exception as _se:
                    logger.debug(f"ProjectAsk: workspace_messages save failed: {_se}")

            threading.Thread(target=_save_workspace_messages, daemon=True).start()

            # ── Eval Observatory: LLM-as-judge for My Workspace path ─────
            # eval_answer_quality  → groundedness + relevance rows in eval_results
            # emit_coach_event     → coach_prompt row via Kafka → coach_ingestor
            # Both were missing; projects_router never called either.
            if full:
                try:
                    from core.evals import eval_engine as _proj_eval_eng
                    _proj_eval_eng.eval_answer_quality(
                        raw_question, full,
                        list(local_agent.last_context) if local_agent and hasattr(local_agent, "last_context") and local_agent.last_context else [],
                        platform="my_workspace",
                        model=model,
                    )
                except Exception:
                    pass

            # Coach prompt quality eval (fires coach_prompt → eval_results)
            try:
                from core.coach_events import emit_coach_event as _proj_emit_coach
                # channel="my_workspace" so the ingestor's channel→platform
                # fallback map also resolves correctly even if eval_platform
                # is somehow dropped (e.g. older Kafka consumer version).
                _proj_emit_coach(
                    user_id=user_id,
                    channel="my_workspace",
                    model=model,
                    prompt=raw_question,
                    completion=full,
                    tokens_in=in_tok,
                    tokens_out=out_tok,
                    cost_usd=cost,
                    latency_ms=int(latency * 1000),
                    thread_id=project_id,
                    request_id=request_id,
                    eval_platform="my_workspace",
                )
            except Exception:
                pass

    return StreamingResponse(
        response_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "X-Request-ID":      request_id,
        },
    )
