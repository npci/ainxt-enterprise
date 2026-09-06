# SPDX-License-Identifier: MIT
# ============================================================
# THREADS ROUTER — /threads
# @AiNxt autonomous bug-fix + Jira + GitLab flow
# ============================================================

import uuid
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel

from core.config import REDIS_HOST as _REDIS_HOST, REDIS_PORT as _REDIS_PORT
from core.logger import logger
from auth.dependencies import get_current_user
from core.model_registry import (
    OPENAI_CODING_MODEL as _OPENAI_CODING,
    MODEL_COST_PER_1M as _MODEL_COST_PER_1M,
)
from core.security_validation import (
    validate_create_thread_request,
    validate_thread_message_request,
    validate_hitl_request,
    validate_reaction_request,
)

router = APIRouter(tags=["threads"])


class ThreadCreate(BaseModel):
    title: str
    product_id: str
    description: str = ""
    project_id: str = ""
    repo: str = ""
    created_by: str = "user"
    labels: List[str] = []
    priority: str = "Medium"


class ThreadMessage(BaseModel):
    content: str
    author: str = "user"
    author_name: Optional[str] = None
    author_band: Optional[str] = None
    message_type: str = "text"
    parent_message_id: Optional[str] = None
    mentions: List[str] = []
    model_used: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    cost_usd: Optional[float] = None
    latency_ms: Optional[float] = None
    # "chat" = conversational Q&A via /ask (default); "pipeline" = full SDLC flow
    ainxt_intent: str = "chat"


# ============================================================
# @AiNxt BACKGROUND TASK
# ============================================================

def _gitlab_owner_repo(git_url: str) -> str:
    """Extract 'namespace/project' from a GitLab URL. Returns '' if not parseable."""
    if not git_url:
        return ""
    url = git_url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    parts = url.split("/")
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return ""


def _ainxt_flow(thread_id: str, message_content: str, repo: str, product_id: str = None, user_id: str = "",
                user_email: str = ""):
    """
    Autonomous @AiNxt invocation flow:
    1. Set status = running
    2. Load thread context
    3. Retrieve code via RAG
    4. AgentRunner.run() → fix
    5. Classify priority + impact
    6. Create Jira issue
    7. Create GitLab Issue (tracking — no code committed in this flow)
    8. Post summary to thread messages
    9. Set status = complete
    10. Publish inbox notification
    """
    from store.threads_store import (
        get_thread, get_messages, add_message,
        set_agent_status,
    )
    from store.inbox_store import publish_inbox_item

    set_agent_status(thread_id, "running")

    try:
        thread = get_thread(thread_id)
        if not thread:
            return

        # Load thread context
        messages = get_messages(thread_id, limit=20)
        context_text = "\n".join(
            f"{m['author']}: {m['content']}" for m in messages[-20:]
        )

        # Retrieve relevant code — multi-repo if product_id set and no specific repo
        code_context = ""
        file_tree_hint = ""

        # Resolve which repos to search
        _repos_to_search = []
        if repo:
            _repos_to_search = [repo]
        elif product_id:
            try:
                from db.database import SessionLocal as _ThrSess
                from db.models import ProductRepo as _ProdRepo, IndexRequest as _IdxReq
                _tdb = _ThrSess()
                try:
                    # explicit product_repos links
                    _prs = _tdb.query(_ProdRepo).filter(_ProdRepo.product_id == product_id).all()
                    _repo_set = {pr.repo_name for pr in _prs}
                    # also include repos indexed with this product_id (index_requests)
                    _irs = _tdb.query(_IdxReq).filter(
                        _IdxReq.product_id == product_id, _IdxReq.status == "done"
                    ).all()
                    for ir in _irs:
                        _repo_set.add(ir.repo_name)
                    _repos_to_search = list(_repo_set)
                    logger.info(f"AiNxt: multi-repo search across {len(_repos_to_search)} repos for product {product_id}")
                finally:
                    _tdb.close()
            except Exception as _pr_err:
                logger.warning(f"AiNxt: product repos fetch failed: {_pr_err}")

        # Collect repo git-URL hints for display (retrieval is handled inside ReactEngine)
        _file_hints = []
        for _repo_name in _repos_to_search:
            try:
                from core.config import RDB_REGISTRY
                from core.kv import get_kv
                _rc = get_kv(RDB_REGISTRY, decode_responses=True)
                _git_url = _rc.get(f"index:repo:{_repo_name}:url") or ""
                if _git_url:
                    _file_hints.append(f"Repository: {_git_url}")
            except Exception:
                pass

        file_tree_hint = " | ".join(_file_hints)
        repos_label = ", ".join(_repos_to_search) if _repos_to_search else "no repos indexed"

        # ReACT retrieve function — searches all resolved repos
        def _react_retrieve(query: str) -> list[str]:
            parts = []
            for _rn in _repos_to_search:
                try:
                    from models.hybrid_retriever import hybrid_retrieve_context
                    _c = hybrid_retrieve_context(query, _rn)
                    if _c:
                        parts.extend(_c[:4])
                except Exception as _e:
                    logger.warning(f"AiNxt: RAG failed for {_rn}: {_e}")
            return parts[:8]

        # Build the task description for the ReACT engine
        react_task = (
                f"Analyse this engineering issue and propose a precise fix.\n\n"
                f"Thread: {thread.get('title', '')}\n"
                f"Repos: {repos_label}\n"
                + (f"{file_tree_hint}\n" if file_tree_hint else "")
                + f"\nIssue:\n{message_content}\n\n"
                  f"Thread context:\n{context_text[-800:]}"
        )

        import time as _time
        _t0 = _time.time()

        from agents.react_engine import ReactEngine
        _react = ReactEngine(
            task=react_task,
            retrieve_fn=_react_retrieve,
            synthesis_hint="solution",   # Opus if ENABLE_OPUS=true, else Sonnet
            iteration_hint="complex",    # Sonnet for reasoning iterations (cost control)
        )
        _react_result = _react.run()
        fix_analysis       = _react_result.answer
        _analysis_latency  = round(_time.time() - _t0, 2)
        _actual_model_label = _react_result.model_used
        logger.info(
            f"AiNxt ReACT: iterations={_react_result.iterations} "
            f"confidence={_react_result.confidence:.2f} model={_actual_model_label}"
        )

        # ── Eval: threads answer quality (fire-and-forget) ───────────────────
        # Threads already fires Check 1 (retrieval) via hybrid_retriever.
        # This adds Check 2 (hallucination) + Check 3 (usefulness) on the
        # ReactEngine answer — previously ungraded.
        if fix_analysis:
            try:
                import threading as _tr_eval_thread
                _tr_q   = message_content
                _tr_ans = fix_analysis
                def _run_tr_eval():
                    try:
                        from core.evals import eval_engine as _ee
                        _ee.eval_answer_quality(_tr_q, _tr_ans, [], session_id=None)
                    except Exception:
                        pass
                _tr_eval_thread.Thread(
                    target=_run_tr_eval, daemon=True, name="eval-threads-answer"
                ).start()
            except Exception:
                pass

        # Priority classification — GPT-5.2, never Ollama
        priority_prompt = (
            f"Based on this engineering issue, respond with ONLY one word: High, Medium, or Low.\n\n"
            f"Issue: {message_content}\n"
            f"Analysis summary: {fix_analysis}"
        )
        try:
            priority_raw = model_router.generate(priority_prompt, model_hint="medium").strip()
        except Exception:
            priority_raw = "Medium"
        priority = priority_raw if priority_raw in ("High", "Medium", "Low") else "Medium"

        # Create Jira issue
        jira_url = ""
        jira_key_extracted = ""
        try:
            from tools.jira_tools import jira_create_issue, _default_project
            import os
            if os.environ.get("JIRA_URL") or os.environ.get("LLM_PROXY_URL"):
                # Use thread's project_id as Jira key only if it looks like a valid
                # all-uppercase project key; otherwise fall back to JIRA_PROJECT env var.
                raw_proj = thread.get("project_id", "") or ""
                jira_project = raw_proj.upper() if (raw_proj and raw_proj.isalpha()) else _default_project()
                _jira_desc = (
                    f"h2. @AiNxt Automated Analysis\n\n"
                    f"*Priority:* {priority}  |  *Repos:* {repos_label}  |  *Thread:* {thread.get('title', '')}\n\n"
                    f"----\n\n"
                    f"{fix_analysis}\n\n"
                    f"----\n\n"
                    f"*Original report:*\n{message_content}\n\n"
                    f"_Triaged by AiNxt AI — SDLC pipeline started automatically._"
                )
                jira_url = jira_create_issue(
                    project=jira_project,
                    summary=f"[AiNxt] {thread.get('title', 'Bug')}",
                    description=_jira_desc,
                    priority=priority,
                    issue_type="Bug",
                    user_id=user_id,
                    user_email=user_email,
                )
                # Extract the issue key from the returned URL (e.g. .../browse/PROJ-123)
                if jira_url and "/browse/" in jira_url and not jira_url.startswith("Error"):
                    jira_key_extracted = jira_url.rstrip("/").split("/")[-1]
        except Exception as e:
            logger.warning(f"AiNxt: Jira failed → {e}")

        # Derive a stable Jira key for the SDLC run (fall back to synthetic key using JIRA_PROJECT env var)
        try:
            from tools.jira_tools import _default_project as _djp
            _fallback_proj = _djp()
        except Exception:
            _fallback_proj = "AINRPY"
        _sdlc_jira_key = jira_key_extracted or f"{_fallback_proj}-{thread_id[:6].upper()}"

        # Create GitLab Issue (analysis tracking — no code is committed in this flow)
        gh_url = ""
        try:
            from tools.gitlab_tools import gitlab_create_issue
            import os
            if os.environ.get("GITLAB_TOKEN") and repo:
                # Resolve full namespace/project from the stored git URL in the KV (DB=3)
                owner_repo = ""
                try:
                    from core.config import RDB_REGISTRY
                    from core.kv import get_kv
                    _rc = get_kv(RDB_REGISTRY, decode_responses=True)
                    git_url = _rc.get(f"index:repo:{repo}:url") or ""
                    owner_repo = _gitlab_owner_repo(git_url)
                except Exception:
                    pass

                if not owner_repo:
                    owner_repo = os.environ.get("GITLAB_REPO", "")

                if owner_repo:
                    _gl_body = (
                        f"## 🤖 @AiNxt Automated Analysis\n\n"
                        f"> **Priority:** `{priority}` | **Repos:** `{repos_label}` | **Thread:** {thread.get('title', '')}\n\n"
                        f"---\n\n"
                        f"{fix_analysis}\n\n"
                        f"---\n\n"
                        f"### Original Report\n\n"
                        f"{message_content}\n\n"
                        f"---\n\n"
                        f"_Triaged by [AiNxt AI] · SDLC pipeline started automatically_"
                    )
                    gh_url = gitlab_create_issue(
                        repo=owner_repo,
                        title=f"[AiNxt] {thread.get('title', 'Bug')}",
                        body=_gl_body,
                        labels=["ainxt", "bug"],
                    )
                else:
                    logger.warning(f"AiNxt: GitLab skipped — no namespace/project for '{repo}'")
        except Exception as e:
            logger.warning(f"AiNxt: GitLab Issue failed → {e}")

        # Create SDLC run record (before building summary so we can link to it)
        _sdlc_run_id = None
        _sdlc_issue_dict = None
        try:
            from store.sdlc_store import create_run as _create_sdlc_run
            _sdlc_run = _create_sdlc_run(
                run_type="bug",
                jira_key=_sdlc_jira_key,
                jira_summary=thread.get("title", ""),
                repo=repo or "",
                triggered_by="ainxt",
            )
            _sdlc_run_id = _sdlc_run["id"]
            _sdlc_issue_dict = {
                "key":                    _sdlc_jira_key,
                "summary":                thread.get("title", ""),
                "description":            message_content + "\n\n" + fix_analysis,
                "issue_type":             "Bug",
                "priority":               priority,
                "repo":                   repo or "",
                "assignee":               "",
                "_run_id":                _sdlc_run_id,
                "language_override":      "",  # empty = let pipeline auto-detect from GitLab tree
                "triggered_by_user_id":   user_id,
                "triggered_by_email":     "",
            }
            logger.info(f"AiNxt: created SDLC run {_sdlc_run_id} for thread {thread_id}")
        except Exception as e:
            logger.warning(f"AiNxt: SDLC run creation failed → {e}")

        # Build proposal card — pipeline does NOT start yet; awaiting approval
        parts = [
            f"## @AiNxt Analysis — Awaiting Your Approval",
            f"**Priority:** {priority}",
            "",
            fix_analysis,
        ]
        if jira_url:
            parts.append(f"\n**Jira:** [{jira_url}]({jira_url})")
        if gh_url:
            parts.append(f"**GitLab Issue:** [{gh_url}]({gh_url})")
        if _sdlc_run_id:
            parts.append(
                f"\n> ⚠️ **The SDLC pipeline is paused.** "
                f"Review the analysis above and click **Approve** to start the fix pipeline, "
                f"or **Reject** to cancel."
            )

        summary = "\n".join(parts)

        # Compute stats for persistent storage
        try:
            _in_tok  = int(len(react_task.split()) * 1.3)
            _out_tok = int(len(fix_analysis.split()) * 1.3)
            _model = _actual_model_label
            _rates = _MODEL_COST_PER_1M.get(_model, (3.00, 15.00))
            _cost  = round((_in_tok * _rates[0] + _out_tok * _rates[1]) / 1_000_000, 6)
        except Exception:
            _in_tok, _out_tok, _cost = 0, 0, 0.0
            _model = _actual_model_label or "unknown"

        # Store pending SDLC issue_dict in the workflow KV (DB=2) so HITL approval can start the pipeline
        if _sdlc_run_id and _sdlc_issue_dict:
            try:
                import json as _json_mod
                from core.config import RDB_WORKFLOW
                from core.kv import get_kv
                _rc_hitl = get_kv(RDB_WORKFLOW, decode_responses=True)
                _rc_hitl.set(
                    f"thread:hitl_pending:{_sdlc_run_id}",
                    _json_mod.dumps({"issue_dict": _sdlc_issue_dict, "run_type": "bug"}),
                    ex=604800,  # 7 days
                )
                logger.info(f"AiNxt: stored pending SDLC issue_dict in KV for run {_sdlc_run_id}")
            except Exception as _re:
                logger.warning(f"AiNxt: KV pending store failed → {_re}")

        # Post analysis message with hitl_status=pending — UI shows Approve/Reject buttons
        add_message(thread_id, {
            "content":      summary,
            "author":       "@AiNxt",
            "message_type": "ainxt_analysis",
            "hitl_status":  "pending",
            "ainxt_run_id": _sdlc_run_id,
            "mentions":     [],
            "model_used":   _model,
            "tokens_in":    _in_tok,
            "tokens_out":   _out_tok,
            "cost_usd":     _cost,
            "latency_ms":   round(_analysis_latency * 1000, 1),
        })

        # Notify inbox — pipeline pending approval
        _inbox_body_parts = [
            f"**Priority:** {priority}",
            "",
            "Analysis complete. **Pipeline is awaiting your approval** — open the thread to approve.",
            "",
        ]
        if jira_url:
            _inbox_body_parts.append(f"- **Jira:** {jira_url}")
        if gh_url:
            import re as _re
            _gh_match = _re.search(r'https?://[^\s]+', gh_url)
            _gh_clean = _gh_match.group(0) if _gh_match else gh_url
            _inbox_body_parts.append(f"- **GitLab:** {_gh_clean}")
        if _sdlc_run_id:
            _inbox_body_parts.append(f"- **SDLC Run:** `{_sdlc_run_id[:8]}` (pending approval)")
        _inbox_item_id = publish_inbox_item(
            user_id=thread.get("created_by", "user"),
            type="thread_response",
            title=f"@AiNxt: {thread.get('title', '')} — Approval Required",
            body="\n".join(_inbox_body_parts),
            source_id=thread_id,
            metadata={
                "jira_url":    jira_url,
                "gh_url":      gh_url,
                "sdlc_run_id": _sdlc_run_id,
                "priority":    priority,
            },
        )
        if _sdlc_run_id and _inbox_item_id:
            try:
                from store.sdlc_store import update_run_state as _urs2
                _urs2(_sdlc_run_id, "PENDING_APPROVAL",
                      context_patch={"inbox_item_id": _inbox_item_id})
            except Exception:
                pass

        logger.info(f"AiNxt: analysis posted for thread {thread_id}, run {_sdlc_run_id} — awaiting approval")

        set_agent_status(thread_id, "complete")
        logger.info(f"AiNxt flow complete for thread {thread_id}")

    except Exception as e:
        logger.error(f"AiNxt flow failed for thread {thread_id}: {e}")
        set_agent_status(thread_id, "error")


# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/threads")
def list_threads(
        project_id: Optional[str] = None,
        product_id: Optional[str] = None,
        status: Optional[str] = None,
        label: Optional[str] = None,
        current_user: dict = Depends(get_current_user),
):
    from store.threads_store import list_threads
    from auth.rbac import is_admin

    if is_admin(current_user):
        # Admin sees all threads
        return {"threads": list_threads(
            project_id=project_id, product_id=product_id,
            status=status, label=label,
        )}

    # Non-admin: resolve which product IDs the user can access (via dept_product_mappings)
    user_dept = current_user.get("department") or ""
    accessible_product_ids = []
    try:
        from db.database import SessionLocal as _TLS
        from db.models import DeptProductMapping as _DPM
        _db = _TLS()
        try:
            rows = _db.query(_DPM.product_id).filter(_DPM.department == user_dept).all()
            accessible_product_ids = [str(r.product_id) for r in rows]
        finally:
            _db.close()
    except Exception as _e:
        logger.warning(f"list_threads: product lookup failed: {_e}")

    return {"threads": list_threads(
        project_id=project_id, product_id=product_id,
        status=status, label=label,
        department=user_dept,
        accessible_product_ids=accessible_product_ids,
    )}


@router.post("/threads")
def create_thread(body: ThreadCreate, current_user: dict = Depends(get_current_user)):
    from store.threads_store import create_thread

    # ── Field-level validation & sanitization ────────────────────────────────
    is_valid, field_errors, sanitized = validate_create_thread_request(body)
    if not is_valid:
        error_messages = [
            f"{field}: {err}"
            for field, errs in field_errors.items()
            for err in errs
        ]
        raise HTTPException(status_code=400, detail="; ".join(error_messages))

    data = body.dict()
    data.update(sanitized)                                  # overwrite with clean values
    data["department"] = current_user.get("department") or ""
    thread = create_thread(data)
    return {"success": True, "thread": thread}


@router.get("/threads/{thread_id}")
def get_thread(thread_id: str, current_user: dict = Depends(get_current_user)):
    from store.threads_store import get_thread, get_messages, get_reply_counts
    from auth.rbac import is_admin
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    # Dept gate: non-admins can only read threads in their department (or unscoped threads)
    if not is_admin(current_user):
        user_dept = current_user.get("department") or ""
        thread_dept = thread.get("department") or ""
        if thread_dept and user_dept and thread_dept != user_dept:
            raise HTTPException(status_code=403, detail="Access denied: thread belongs to a different department")
    messages = get_messages(thread_id)
    reply_counts = get_reply_counts(thread_id)
    # Annotate each top-level message with its reply count
    for msg in messages:
        msg["reply_count"] = reply_counts.get(msg["id"], 0)
    return {**thread, "messages": messages}


@router.post("/threads/{thread_id}/messages")
def add_message(
        thread_id: str,
        body: ThreadMessage,
        background_tasks: BackgroundTasks,
        current_user: dict = Depends(get_current_user),
):
    from store.threads_store import get_thread
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Validate and sanitize message content
    is_valid, field_errors, sanitized = validate_thread_message_request(body)
    if not is_valid:
        error_messages = [
            f"{field}: {err}"
            for field, errs in field_errors.items()
            for err in errs
        ]
        raise HTTPException(status_code=400, detail="; ".join(error_messages))

    clean_content = sanitized["content"]   # sanitized content — fixes undefined variable bug

    # Build message dict locally — no DB write on the hot path.
    # Postgres persistence happens async via Kafka consumer on App03.
    import uuid as _uuid
    from datetime import datetime as _dt_now
    msg_data = body.dict()
    msg_id   = str(_uuid.uuid4())
    now_iso  = _dt_now.utcnow().isoformat()
    author   = current_user.get("id") or msg_data.get("author", "")
    author_name = (
            sanitized.get("author_name")
            or current_user.get("name")
            or current_user.get("email")
            or author
    )
    message = {
        "id":                msg_id,
        "thread_id":         thread_id,
        "content":           clean_content,
        "author":            author,
        "author_name":       author_name,
        "author_band":       current_user.get("ad_level"),
        "message_type":      body.message_type if hasattr(body, "message_type") else msg_data.get("message_type", "text"),
        "mentions":          body.mentions if hasattr(body, "mentions") else msg_data.get("mentions", []),
        "parent_message_id": msg_data.get("parent_message_id"),
        "reactions":         {},
        "created_at":        now_iso,
    }

    # Fire-and-forget: produce full payload to Kafka → consumer writes to DB async
    def _publish_kafka():
        try:
            from core.kafka_producer import produce, TOPIC_THREAD_EVENTS
            produce(TOPIC_THREAD_EVENTS, {
                "event":             "message_added",
                "thread_id":         thread_id,
                "message_id":        msg_id,
                "content":           clean_content,
                "author":            author,
                "author_name":       author_name,
                "author_band":       current_user.get("ad_level"),
                "message_type":      message["message_type"],
                "mentions":          message["mentions"],
                "parent_message_id": message.get("parent_message_id"),
                "model_used":        msg_data.get("model_used"),
                "tokens_in":         msg_data.get("tokens_in"),
                "tokens_out":        msg_data.get("tokens_out"),
                "cost_usd":          msg_data.get("cost_usd"),
                "latency_ms":        msg_data.get("latency_ms"),
                "ts":                now_iso,
            }, key=thread_id)
        except Exception as _ke:
            logger.debug(f"kafka publish thread_event: {_ke}")

    background_tasks.add_task(_publish_kafka)

    # Trigger @AiNxt pipeline only when intent is explicitly "pipeline"
    has_ainxt = "@AiNxt" in body.content or "@AiNxt" in body.mentions
    ainxt_triggered = has_ainxt and getattr(body, "ainxt_intent", "chat") == "pipeline"
    if ainxt_triggered:
        background_tasks.add_task(
            _ainxt_flow,
            thread_id,
            clean_content,
            thread.get("repo", ""),
            thread.get("product_id"),
            current_user.get("id") or current_user.get("sub",""),
            current_user.get("email",""),
            )

    return {"success": True, "message": message, "ainxt_triggered": ainxt_triggered}


@router.delete("/threads/{thread_id}")
def delete_thread(thread_id: str):
    from store.threads_store import delete_thread
    ok = delete_thread(thread_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Thread not found")
    return {"success": True}


@router.get("/threads/{thread_id}/agent/status")
def get_agent_status(thread_id: str):
    from store.threads_store import get_agent_status
    status = get_agent_status(thread_id)
    return {"thread_id": thread_id, "status": status}


class HitlAction(BaseModel):
    action: str   # approved | modified | rejected
    note:   Optional[str] = ""


@router.post("/threads/{thread_id}/messages/{message_id}/hitl")
def hitl_action(thread_id: str, message_id: str, body: HitlAction):
    """
    Record a HITL decision on an ainxt_analysis message.
    - Updates hitl_status in DB
    - On "approved": retrieves pending SDLC run from Redis and launches pipeline
    """
    # Validate action and note
    is_valid, field_errors, sanitized = validate_hitl_request(body)
    if not is_valid:
        error_messages = [
            f"{field}: {err}"
            for field, errs in field_errors.items()
            for err in errs
        ]
        raise HTTPException(status_code=400, detail="; ".join(error_messages))

    if body.action not in ("approved", "modified", "rejected"):
        raise HTTPException(status_code=400, detail="action must be approved|modified|rejected")

    import psycopg2
    from core.config import postgres_dsn
    conn = psycopg2.connect(postgres_dsn())
    ainxt_run_id = None
    try:
        with conn.cursor() as cur:
            # Fetch ainxt_run_id so we can start the pipeline on approval
            cur.execute(
                "SELECT ainxt_run_id FROM thread_messages WHERE id=%s AND thread_id=%s",
                (message_id, thread_id)
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Message not found")
            ainxt_run_id = row[0]

            cur.execute("""
                        UPDATE thread_messages
                        SET hitl_status=%s, edited_at=NOW()
                        WHERE id=%s AND thread_id=%s
                        """, (body.action, message_id, thread_id))
        conn.commit()
    finally:
        conn.close()

    logger.info(f"ThreadsRouter: HITL {body.action} on msg {message_id} run={ainxt_run_id}")

    # On approval: retrieve pending issue_dict from the workflow KV (DB=2)
    # and start the SDLC pipeline.
    if body.action == "approved" and ainxt_run_id:
        import threading as _threading
        import json as _json
        from core.config import RDB_WORKFLOW
        from core.kv import get_kv

        pending = None
        try:
            _rc = get_kv(RDB_WORKFLOW, decode_responses=True)
            raw = _rc.get(f"thread:hitl_pending:{ainxt_run_id}")
            if raw:
                pending = _json.loads(raw)
                _rc.delete(f"thread:hitl_pending:{ainxt_run_id}")
        except Exception as _re:
            logger.warning(f"ThreadsRouter: KV pending fetch failed → {_re}")

        if pending:
            _issue_dict = pending["issue_dict"]
            _run_type   = pending.get("run_type", "bug")
            _fn_name    = f"workers.sdlc_worker.run_{_run_type}_pipeline_job"

            _enqueued = False
            try:
                from core.job_queue import enqueue_sdlc_job
                enqueue_sdlc_job(_fn_name, _issue_dict)
                _enqueued = True
                logger.info(f"ThreadsRouter: SDLC job enqueued via RQ run={ainxt_run_id} fn={_fn_name}")
            except Exception as _rq_err:
                logger.warning(f"ThreadsRouter: RQ unavailable ({_rq_err}), falling back to daemon thread")

            if not _enqueued:
                def _launch_sdlc():
                    try:
                        from store.sdlc_store import update_run_state
                        update_run_state(ainxt_run_id, "CREATED",
                                         context_patch={"approved_via": "threads"})
                        if _run_type == "feature":
                            from agents.sdlc_pipeline import run_feature_pipeline
                            run_feature_pipeline(_issue_dict, ainxt_run_id)
                        else:
                            from agents.sdlc_pipeline import run_bug_pipeline
                            run_bug_pipeline(_issue_dict, ainxt_run_id)
                    except Exception as _exc:
                        logger.error(f"ThreadsRouter: SDLC pipeline error run={ainxt_run_id} → {_exc}")
                        try:
                            from store.sdlc_store import update_run_state
                            update_run_state(ainxt_run_id, "FAILED", error=str(_exc))
                        except Exception:
                            pass

                _t = _threading.Thread(target=_launch_sdlc, daemon=True,
                                       name=f"sdlc-approved-{ainxt_run_id[:8]}")
                _t.start()
                logger.info(f"ThreadsRouter: SDLC pipeline started via daemon thread run={ainxt_run_id}")
            return {"success": True, "message_id": message_id, "hitl_status": body.action,
                    "pipeline": "started", "run_id": ainxt_run_id}
        else:
            logger.warning(f"ThreadsRouter: no pending SDLC data found for run={ainxt_run_id}")

    return {"success": True, "message_id": message_id, "hitl_status": body.action}


class ReactionBody(BaseModel):
    emoji:   str
    user_id: str


@router.post("/threads/{thread_id}/messages/{message_id}/react")
def toggle_reaction(thread_id: str, message_id: str, body: ReactionBody):
    """Toggle an emoji reaction on a thread message (add if not present, remove if present)."""
    # Validate emoji and user_id
    is_valid, field_errors, sanitized = validate_reaction_request(body)
    if not is_valid:
        error_messages = [
            f"{field}: {err}"
            for field, errs in field_errors.items()
            for err in errs
        ]
        raise HTTPException(status_code=400, detail="; ".join(error_messages))

    import psycopg2
    import json as _json
    from core.config import postgres_dsn

    conn = psycopg2.connect(postgres_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT reactions FROM thread_messages WHERE id=%s AND thread_id=%s",
                (message_id, thread_id)
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Message not found")

            reactions = row[0] if isinstance(row[0], dict) else (_json.loads(row[0]) if row[0] else {})
            users     = reactions.get(body.emoji, [])

            if body.user_id in users:
                users = [u for u in users if u != body.user_id]
            else:
                users = users + [body.user_id]

            reactions[body.emoji] = users
            cur.execute(
                "UPDATE thread_messages SET reactions=%s::jsonb WHERE id=%s",
                (_json.dumps(reactions), message_id)
            )
        conn.commit()
    finally:
        conn.close()

    return {"success": True, "reactions": reactions}
