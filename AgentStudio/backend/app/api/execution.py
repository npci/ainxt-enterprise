# SPDX-License-Identifier: MIT
"""Workflow execution endpoints: /run, /run-stream, /resume-stream.

P2 adds a transparent promotion path: when the incoming RunRequest carries
``goal_id`` or ``budget``, ``/run-stream`` wraps the inner engine in a
LoopRunner instead of calling ``NativeEngine.execute`` directly. The same
``goal_id``-bearing payload is also accepted by ``/loops/{id}/run-stream``
in ``app.api.loops`` — both entry points share a single backend, per
PHASE_2_LOOPRUNNER.md D10.
"""
import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from app.models import RunRequest, ResumeRequest, RunResponse, AuthenticatedUser
from app.engine import get_engine
from app.engine.interface import extract_bound_tool_names
from app.api.deps import (
    require_access, to_chain, build_execution_context,
    bind_log_context, clear_log_context, enforce_budget_or_downgrade,
)
from app.loop import repo as loops_repo
from app.loop.runner import LoopRunner
from app.core import workflow_repo
from app.core.governance import (
    audit_event,
    check_tool_access,
    RunUsageTracker,
    _is_local_model,
)
from app.core.config import factory_model, verifier_model
import hashlib
from core.logger import logger
try:
    from fastapi.concurrency import run_in_threadpool as _run_in_threadpool
    from core.trace_store import add_trace as _add_trace  # type: ignore
    _TRACE_STORE_AVAILABLE = True
except ImportError:
    _TRACE_STORE_AVAILABLE = False

router = APIRouter()


# ---------------------------------------------------------------------------
# FR-T0-4 — Node-level observability
# ---------------------------------------------------------------------------
# The SSE consumer loop below already parses every engine event and feeds the
# RunUsageTracker. It is the single natural place to also persist per-node
# trace records to the platform trace store (Redis db=1) so node-level
# cost/token/latency + compliance/injection verdicts land in Grafana alongside
# run-level aggregation. We only trace the events that carry node signal and
# fail silently — observability must never break a live run.
_TRACED_EVENTS = {
    "agent_usage", "agent_complete", "compliance_verdict", "injection_detected",
}


async def _write_node_trace(tracker: "RunUsageTracker", payload: dict) -> None:
    """Persist a node-level trace record for observability. Best-effort.

    ``add_trace`` performs two SYNCHRONOUS Redis round-trips (rpush + expire),
    so it is offloaded to a threadpool to keep it off the event loop — this
    runs once per traced node event inside the live SSE stream and must never
    add per-event latency to token streaming.
    """
    try:
        etype = payload.get("event") or payload.get("type") or ""
        if etype not in _TRACED_EVENTS:
            return
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        record = {
            "event": etype,
            "node_id": data.get("node_id"),
            "agent": data.get("agent"),
            "model": data.get("model") or data.get("model_used"),
            "usage": data.get("usage"),
            "workflow_id": tracker.workflow_id,
            "department": tracker.department,
        }
        if etype == "compliance_verdict":
            record["compliance"] = {
                "direction": data.get("direction"),
                "blocked": data.get("blocked"),
                "was_redacted": data.get("was_redacted"),
                "finding_types": data.get("finding_types"),
            }
        elif etype == "injection_detected":
            record["injection"] = {
                "source": data.get("source"),
                "score": data.get("score"),
                "categories": data.get("categories"),
                "action": data.get("action"),
            }
        if not _TRACE_STORE_AVAILABLE:
            return
        await _run_in_threadpool(
            _add_trace, tracker.request_id, json.dumps(record, default=str)
        )
    except Exception:  # never break a run on a trace write
        pass


def _enforce_governance(entity_type: str, name: str):
    """No-op: running an artifact no longer requires approval.

    Governance approval is now only required to PUBLISH an artifact as a shared
    template (the "Deploy" flow), not to run it. Users can freely run their own
    saved workflows/agents. Kept as a no-op (rather than removing the call sites)
    so the run/run-stream entrypoints and imports stay stable.
    """
    return


# ---------------------------------------------------------------------------
# Tool-binding integrity check (security review F-03)
# ---------------------------------------------------------------------------
# /run, /run-stream, and /resume-stream all execute the ``workflow`` graph
# from the REQUEST BODY, not the persisted row — this is intentional (see
# _enforce_governance above: the editor legitimately needs to test unsaved
# graph edits before Save). The gap the security review calls out isn't
# "unsaved runs are allowed" (that's an existing, unchanged privilege the
# user already has in the editor) — it's that a caller who bypasses the UI
# can POST a graph for an *existing saved* workflow_id that silently attaches
# extra/sensitive tools never present in what was actually saved, effectively
# smuggling in tool bindings that never went through the normal
# attach-a-tool-in-the-editor flow or policy review.
#
# The check below is intentionally narrow: it only fires when workflow_id
# resolves to a workflow this user owns, and it only blocks NEW tool/server
# identifiers that (a) aren't in the saved graph AND (b) would be denied by
# the same policy engine (app.core.governance.check_tool_access) already
# enforced at tool-dispatch time inside the engine. It does not touch
# prompts, node wiring, or non-sensitive tool changes — those are normal
# editor iteration.
#
# Tool-name extraction is delegated to app.engine.interface.extract_bound_tool_names
# — the SAME accessor the engine itself will eventually need to reason about
# a graph's tool surface (catalog tools via data.tools, MCP servers via edge
# wiring). Duplicating that walk here would risk this check silently missing
# a tool the engine actually dispatches (e.g. an MCP-attached tool), which
# would make the integrity check bypassable rather than merely incomplete.
async def _enforce_tool_binding_integrity(
    *, workflow_id: str, posted_nodes: list, posted_edges: list,
    current_user: "AuthenticatedUser",
) -> Optional[str]:
    """Reject a posted graph that adds tools/MCP servers not present in the
    saved workflow when those would be denied by policy for this user.

    Returns ``None`` when the run may proceed, or a denial-reason string.
    No-ops (returns ``None``) for unsaved/draft workflows — see module note
    above; this is not a new restriction on editor draft-testing.
    """
    if not workflow_id:
        return None
    try:
        saved = await workflow_repo.get_workflow(workflow_id, current_user.id)
    except Exception:
        # Repo hiccup must never block a run the user could otherwise make —
        # this check is defense-in-depth, not the primary access boundary.
        logger.exception('[AGENT] _enforce_tool_binding_integrity: lookup failed; skipping check')
        return None
    if not saved:
        # workflow_id doesn't resolve to a saved row this user owns — either
        # an unsaved draft (session id) or a workflow the user doesn't own.
        # Ownership access to the run itself is unaffected by this check.
        return None

    saved_graph  = saved.get("graphData") or saved.get("graph_data") or {}
    saved_tools  = extract_bound_tool_names(
        saved_graph.get("nodes"), saved_graph.get("edges")
    )
    posted_tools = extract_bound_tool_names(posted_nodes, posted_edges)
    new_tools    = posted_tools - saved_tools
    if not new_tools:
        return None

    for tool_name in sorted(new_tools):
        # Deliberately do NOT pass ``available_tools=saved_tools`` here: that
        # would make check_tool_access's "is this tool attached" check (#4)
        # reject every new tool by construction, since by definition it's
        # new relative to the saved graph — that would block ordinary editor
        # iteration (attach a tool, test-run before saving), not just the
        # exploit this check targets. Leaving available_tools unset means
        # only the POLICY checks apply: global ABSTUDIO_BLOCKED_TOOLS and
        # the ad_level/RESTRICTED_TOOLS_MID/READONLY_TOOLS hierarchy rules —
        # i.e. "would this user be allowed to use this tool at all", which is
        # the actual gap F-03 describes (smuggling in tools the user's role
        # wouldn't otherwise be allowed to attach).
        deny = check_tool_access(
            tool_name,
            user_id=current_user.id,
            is_admin=(current_user.role or "").lower() == "admin",
            ad_level=current_user.ad_level,
            is_hod=current_user.is_hod,
            is_security_team=current_user.is_security_team,
            endpoint="abstudio.workflow.tool_binding_integrity",
            workflow_id=workflow_id,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
        )
        if deny:
            kind = "MCP server" if tool_name.startswith("mcp:") else "tool"
            return (
                f"posted graph adds {kind} '{tool_name}', which is not present in the "
                f"saved workflow and is not permitted for your access level ({deny}). "
                f"Save the workflow first if this is an intentional change."
            )
    return None


async def _enforce_thread_ownership(
    *, thread_id: str, current_user: "AuthenticatedUser",
) -> Optional[str]:
    """Reject a client-supplied thread_id that belongs to a different user.

    Security review F-06/F-10 follow-up: chat.py's read/delete routes were
    scoped by owner, and /resume-stream's HITL-snapshot lookup was scoped
    by owner, but /run, /run-stream, and /resume-stream themselves accepted
    a client-supplied ``thread_id`` and built the run's ExecutionContext
    from it with NO ownership check before this function existed. Since
    save_messages()'s first-write COALESCE only refuses to *overwrite* an
    existing owner, a second user posting the same thread_id would still
    have their turn appended into the first user's message history, and
    would see the first user's prior turns loaded into their own prompt —
    a cross-tenant read AND write, not merely a metadata leak.

    Returns ``None`` when the run may proceed (new thread, ownerless
    pre-migration thread, or the caller's own thread), or a denial-reason
    string when the thread is confirmed to belong to someone else.
    """
    if not thread_id:
        return None
    try:
        owner = await get_engine().get_thread_owner(thread_id)
    except Exception:
        # A lookup hiccup must never block a run the user could otherwise
        # make — same defense-in-depth posture as _enforce_tool_binding_integrity.
        logger.exception('[AGENT] _enforce_thread_ownership: lookup failed; skipping check')
        return None
    if not owner:
        # None = brand new thread_id; "" = pre-migration/legacy row with no
        # recorded owner. Both are allowed to proceed — the first save
        # stamps (or has already stamped) an owner via the normal
        # save_messages() first-write rule.
        return None
    if owner != current_user.id:
        return (
            "thread_id belongs to a different user. Start a new conversation "
            "instead of reusing another user's thread_id."
        )
    return None


def _graph_hash(nodes, edges) -> str:
    """Canonical sha256 of a graph's nodes/edges for audit purposes.

    Lets every run's audit_event record exactly what graph executed,
    independent of whether it matched the persisted row — addresses the
    security review's "invalidates any audit narrative" note for F-03.
    Never raises; returns "" on any serialization failure.
    """
    try:
        canonical = json.dumps(
            {"nodes": nodes, "edges": edges}, sort_keys=True, default=str
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Greeting short-circuit for deployed-workflow RUNTIME execution
# ---------------------------------------------------------------------------
#
# A bare greeting ("hi", "hello", "good morning", "thanks"…) sent to a DEPLOYED
# workflow should get a lightweight reply instead of executing the whole
# multi-agent pipeline (which would waste tokens/tools and produce a report for
# "hi"). Reuses the same rule as chat + the factory chats
# (``models.classifier.GREETING_PATTERN``) and the same reply behaviour as the
# Agent Builder / factory short-circuits: a Local-LLM (simple tier) response
# with a canned fallback. Multi-word inputs ("hi, summarize this…") do NOT match
# (fullmatch) and run the workflow normally — no regression.

_RUNTIME_GREETING_FALLBACK = "Hi! What would you like this workflow to do?"


def _is_greeting(message: str) -> bool:
    """True when ``message`` is a bare greeting (whole-message exact match)."""
    try:
        from models.classifier import GREETING_PATTERN
        return bool(GREETING_PATTERN.fullmatch((message or "").strip()))
    except Exception:
        return False


def _greeting_reply(user_message: str) -> str:
    """Local-LLM (simple tier) greeting reply with a canned fallback."""
    try:
        from models.model_router import model_router
        answer = model_router.generate(user_message, model="simple")
    except Exception as exc:
        logger.warning(f"[AGENT] workflow-runtime greeting generate failed → {exc}")
        answer = ""
    return answer.strip() if (answer and answer.strip()) else _RUNTIME_GREETING_FALLBACK


# Node types that invoke an LLM directly via a node-configured model
# (mirrors native_engine's model-bearing dispatch branches).
_MODEL_BEARING_NODE_TYPES = {"agent", "subflow", "loop"}
# Node types that run an LLM judge using the ENV-default verifier/factory
# model rather than a per-node model (the in-graph evaluation gate; a loop
# whose body has no local model also falls back to this judge model).
_JUDGE_NODE_TYPES = {"evaluation_gate", "loop"}


def _workflow_is_all_local(workflow) -> bool:
    """True only when EVERY LLM the workflow can invoke resolves to a local model.

    Cloud usage must NEVER bypass the budget check, so we return True (skip the
    preflight) only when there is no cloud model configured anywhere in the
    graph — agent/subflow/loop node models AND the LLM-judge model used by
    evaluation-gate / loop nodes.

    Model resolution mirrors ``native_engine._extract_llm_config`` for node
    models (``data.llm_config.model_name`` → ``data.modelName`` → ``factory_model()``)
    and ``loop.runner.evaluate_llm_judge`` for the judge (``verifier_model()``).
    A blank/unset node model is NOT ignored — it resolves to the env default,
    which is typically cloud. Fails safe (returns False → enforce budget) on any
    error or when there are no nodes.
    """
    try:
        nodes = getattr(workflow, "nodes", None) or []
        if not nodes:
            logger.debug("[AGENT] _workflow_is_all_local: no nodes → enforce budget")
            return False
        default_model = factory_model()
        for node in nodes:
            if not isinstance(node, dict):
                logger.debug("[AGENT] _workflow_is_all_local: non-dict node → enforce budget")
                return False
            ntype = (node.get("type") or "").strip().lower()
            node_id = node.get("id") or "<no-id>"
            data = node.get("data") or node or {}

            if ntype in _MODEL_BEARING_NODE_TYPES:
                llm_cfg = data.get("llm_config") or {}
                model = (
                    (llm_cfg.get("model_name") or data.get("modelName") or "").strip()
                    or default_model
                )
                if not _is_local_model(model):
                    logger.info(
                        f"[AGENT] _workflow_is_all_local: cloud model on node "
                        f"id={node_id} type={ntype} model={model!r} → ENFORCE budget"
                    )
                    return False  # any cloud node model → enforce budget

            if ntype in _JUDGE_NODE_TYPES:
                # The evaluation gate (and a loop's fallback judge) run the
                # LLM judge on the env-default verifier model, independent of
                # any node model. If that judge model is cloud, enforce budget.
                judge_model = verifier_model()
                if not _is_local_model(judge_model):
                    logger.info(
                        f"[AGENT] _workflow_is_all_local: cloud judge model on node "
                        f"id={node_id} type={ntype} judge_model={judge_model!r} → ENFORCE budget"
                    )
                    return False
        logger.info(
            f"[AGENT] _workflow_is_all_local: all {len(nodes)} nodes local → SKIP budget"
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[AGENT] _workflow_is_all_local: scan failed → enforce budget: {exc}")
        return False


def _downgrade_workflow_to_model(workflow, fallback_model: str) -> bool:
    """Rewrite every model-bearing node in ``workflow`` to ``fallback_model``.

    Used when the budget store is down: rather than refuse the run outright we
    re-point the graph at a no-cost local model so the user still gets an
    answer while cloud spend stays blocked (see
    ``governance.check_budget_allowed``).

    Returns True only when the resulting graph is genuinely all-local. It
    returns False — meaning "deny the run" — when the graph contains an
    ``evaluation_gate``/``loop`` judge, because that judge runs on the
    env-global ``verifier_model()`` which a per-request rewrite cannot change:
    silently letting it through would keep spending on a cloud judge during the
    very outage we are protecting against.
    """
    if not fallback_model:
        return False
    try:
        nodes = getattr(workflow, "nodes", None) or []
        if not nodes:
            return False
        for node in nodes:
            if not isinstance(node, dict):
                return False
            ntype = (node.get("type") or "").strip().lower()

            # A cloud judge cannot be overridden per-request → cannot downgrade.
            if ntype in _JUDGE_NODE_TYPES and not _is_local_model(verifier_model()):
                logger.warning(
                    f"[AGENT] budget-degraded downgrade refused: node type={ntype} runs the "
                    f"env-global judge model {verifier_model()!r} (cloud) which a per-request "
                    f"override cannot change → denying instead of half-downgrading"
                )
                return False

            if ntype in _MODEL_BEARING_NODE_TYPES:
                data = node.get("data")
                if not isinstance(data, dict):
                    data = {}
                    node["data"] = data
                llm_cfg = data.get("llm_config")
                if isinstance(llm_cfg, dict):
                    llm_cfg["model_name"] = fallback_model
                else:
                    data["llm_config"] = {"model_name": fallback_model}
                data["modelName"] = fallback_model
        logger.info(
            f"[AGENT] budget-degraded downgrade: rewrote {len(nodes)} node(s) to "
            f"local model {fallback_model!r}"
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[AGENT] budget-degraded downgrade failed → deny: {exc}")
        return False


@router.post("/run", response_model=RunResponse)
async def run_workflow(
    request: RunRequest,
    http_request: Request,
    current_user: AuthenticatedUser = Depends(require_access),
):
    workflow_id   = request.workflow_id or ""
    workflow_name = request.workflow_name or ""
    _enforce_governance("workflows", workflow_name)

    # ── Tool-binding integrity check (security review F-03) ─────────────
    # Only fires for a saved workflow this user owns; unsaved/draft graphs
    # (editor test-runs) are unaffected. See helper docstring for rationale.
    _binding_deny = await _enforce_tool_binding_integrity(
        workflow_id=workflow_id,
        posted_nodes=request.workflow.nodes,
        posted_edges=request.workflow.edges,
        current_user=current_user,
    )
    if _binding_deny:
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.workflow.run",
            action="tool_binding_denied",
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error=_binding_deny,
        )
        raise HTTPException(
            status_code=409,
            detail={"code": "TOOL_BINDING_DENIED", "message": _binding_deny},
        )

    # ── Thread-ownership check (security review F-06/F-10 follow-up) ────
    # Rejects a client-supplied thread_id that belongs to a different user
    # BEFORE any read/write happens. See helper docstring for rationale.
    _thread_deny = await _enforce_thread_ownership(
        thread_id=request.thread_id or "", current_user=current_user,
    )
    if _thread_deny:
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.workflow.run",
            action="thread_ownership_denied",
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            thread_id=request.thread_id or "",
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error=_thread_deny,
        )
        raise HTTPException(
            status_code=403,
            detail={"code": "THREAD_OWNERSHIP_DENIED", "message": _thread_deny},
        )

    # ── Budget preflight ────────────────────────────────────────────────
    # Skip budget only when the workflow uses no cloud model anywhere (all
    # node models + judge models are local, which incur no spend). Shared
    # policy — see deps.enforce_budget_or_downgrade.
    _budget = await enforce_budget_or_downgrade(
        user_id=current_user.id,
        endpoint="abstudio.workflow.run",
        skip_check=_workflow_is_all_local(request.workflow),
        downgrade=lambda m: _downgrade_workflow_to_model(request.workflow, m),
        audit_kwargs=dict(
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
        ),
    )

    chain   = to_chain(request.workflow)
    context = build_execution_context(
        current_user,
        thread_id=request.thread_id,
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        subagents_enabled=getattr(request, "subagents_enabled", None),
        attachments=request.attachments,
    )

    tracker = RunUsageTracker(
        user_id=current_user.id,
        endpoint="abstudio.workflow.run",
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        thread_id=context.thread_id or "",
        email=current_user.email,
        department=getattr(current_user, "department", "") or "",
    )

    # ── Audit run start ─────────────────────────────────────────────────
    # graph_hash records exactly which graph executed, independent of
    # whether it matched the persisted row (F-03 audit-trail remediation).
    audit_event(
        user_id=current_user.id,
        endpoint="abstudio.workflow.run",
        action="start",
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        thread_id=context.thread_id or "",
        request_id=tracker.request_id,
        email=current_user.email,
        department=getattr(current_user, "department", "") or "",
        extra={"graph_hash": _graph_hash(request.workflow.nodes, request.workflow.edges)},
    )

    output    = ""
    thread_id = context.thread_id or ""
    bind_log_context(
        current_user, thread_id=thread_id, request=http_request,
        request_id=tracker.request_id, span="workflow_run",
    )
    try:
        async for raw_event in get_engine().execute(chain, request.user_input, context):
            if not raw_event.startswith("data:"):
                continue
            try:
                payload = json.loads(raw_event[5:].strip())
            except json.JSONDecodeError:
                continue
            etype = payload.get("event") or payload.get("type") or ""
            data  = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            tracker.observe_event(payload)
            await _write_node_trace(tracker, payload)  # FR-T0-4
            if etype == "agent_complete":
                output = data.get("output", output)
            elif etype == "complete":
                output    = data.get("output", output) or output
                thread_id = data.get("thread_id", thread_id)
            elif etype == "error":
                err_msg = data.get("message", "Unknown error")
                tracker.finalize("error", error=err_msg)
                return RunResponse(
                    status="error",
                    message=err_msg,
                    thread_id=thread_id or None,
                )
        tracker.thread_id = thread_id
        tracker.finalize("success")
        return RunResponse(status="success", output=output, thread_id=thread_id or None)
    except Exception as e:
        tracker.finalize("error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        clear_log_context()


@router.post("/run-stream")
async def run_workflow_stream(
    request: RunRequest,
    http_request: Request,
    current_user: AuthenticatedUser = Depends(require_access),
):
    workflow_id   = request.workflow_id or ""
    workflow_name = request.workflow_name or ""
    _enforce_governance("workflows", workflow_name)

    # ── Tool-binding integrity check (security review F-03) ─────────────
    # This is the primary fix target: /run-stream is the endpoint the report
    # calls out as executing a client-posted graph with no integrity check.
    # Only fires for a saved workflow this user owns; unsaved/draft graphs
    # (editor test-runs) are unaffected. See helper docstring for rationale.
    _binding_deny = await _enforce_tool_binding_integrity(
        workflow_id=workflow_id,
        posted_nodes=request.workflow.nodes,
        posted_edges=request.workflow.edges,
        current_user=current_user,
    )
    if _binding_deny:
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.workflow.run_stream",
            action="tool_binding_denied",
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error=_binding_deny,
        )
        raise HTTPException(
            status_code=409,
            detail={"code": "TOOL_BINDING_DENIED", "message": _binding_deny},
        )

    # ── Thread-ownership check (security review F-06/F-10 follow-up) ────
    # /run-stream is the primary write path into chat_threads (via
    # _save_user_prompt / _save_history), so this is the primary fix target
    # for the cross-tenant write gap: without this check, a client-supplied
    # thread_id belonging to another user would have this run's turn
    # appended into their history and their prior turns loaded into this
    # run's prompt. See helper docstring for full rationale.
    _thread_deny = await _enforce_thread_ownership(
        thread_id=request.thread_id or "", current_user=current_user,
    )
    if _thread_deny:
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.workflow.run_stream",
            action="thread_ownership_denied",
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            thread_id=request.thread_id or "",
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error=_thread_deny,
        )
        raise HTTPException(
            status_code=403,
            detail={"code": "THREAD_OWNERSHIP_DENIED", "message": _thread_deny},
        )

    # ── Budget preflight ────────────────────────────────────────────────
    # Skip budget only when the workflow uses no cloud model anywhere (all
    # node models + judge models are local, which incur no spend). Shared
    # policy — see deps.enforce_budget_or_downgrade.
    _budget = await enforce_budget_or_downgrade(
        user_id=current_user.id,
        endpoint="abstudio.workflow.run_stream",
        skip_check=_workflow_is_all_local(request.workflow),
        downgrade=lambda m: _downgrade_workflow_to_model(request.workflow, m),
        audit_kwargs=dict(
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
        ),
    )

    chain = to_chain(request.workflow)
    promote_to_loop = bool(request.goal_id or request.loop_id or request.budget)
    context = build_execution_context(
        current_user,
        thread_id=request.thread_id,
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        subagents_enabled=getattr(request, "subagents_enabled", None),
        goal_id=request.goal_id,
        loop_id=request.loop_id,
        budget=request.budget,
        trigger_src="manual",
        allowed_connections=request.allowed_connections,
        attachments=request.attachments,
    )

    tracker = RunUsageTracker(
        user_id=current_user.id,
        endpoint="abstudio.workflow.run_stream",
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        thread_id=context.thread_id or "",
        email=current_user.email,
        department=getattr(current_user, "department", "") or "",
    )

    # graph_hash records exactly which graph executed, independent of
    # whether it matched the persisted row (F-03 audit-trail remediation).
    audit_event(
        user_id=current_user.id,
        endpoint="abstudio.workflow.run_stream",
        action="start",
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        thread_id=context.thread_id or "",
        request_id=tracker.request_id,
        email=current_user.email,
        department=getattr(current_user, "department", "") or "",
        extra={"graph_hash": _graph_hash(request.workflow.nodes, request.workflow.edges)},
    )

    bind_log_context(
        current_user, thread_id=context.thread_id, request=http_request,
        request_id=tracker.request_id, span="workflow_run",
    )

    goal = None
    if request.goal_id:
        try:
            goal = await loops_repo.get_goal(request.goal_id)
        except Exception:
            logger.exception('[AGENT] /run-stream: get_goal failed; proceeding without goal')
        if not goal:
            raise HTTPException(
                status_code=404,
                detail={"error": "goal_not_found",
                        "message": f"goal '{request.goal_id}' not found"},
            )

    loop = None
    if request.loop_id:
        try:
            loop = await loops_repo.get_loop(request.loop_id)
        except Exception:
            logger.exception('[AGENT] /run-stream: get_loop failed; proceeding without loop')
        if not loop:
            raise HTTPException(
                status_code=404,
                detail={"error": "loop_not_found",
                        "message": f"loop '{request.loop_id}' not found"},
            )

    runner = LoopRunner() if promote_to_loop else None

    async def event_generator():
        bind_log_context(
            current_user, thread_id=context.thread_id, request=http_request,
            request_id=tracker.request_id, span="workflow_run",
        )

        # Budget store was down and we downgraded the graph to a no-cost local
        # model — tell the user before anything runs. Reuses `agent_fallback`,
        # which the ChatPanel already renders as a transient "switched to
        # fallback model" notice plus a Debug Log row.
        if _budget.notice:
            from app.engine.interface import make_sse
            yield make_sse("agent_fallback", {
                "agent": "Budget guard",
                "node_id": None,
                "primary_model": "the selected cloud model",
                "fallback_model": _budget.fallback_model,
                "reason": _budget.notice,
            })

        # ---- Greeting short-circuit -----------------------------------------
        # A bare greeting shouldn't execute the whole deployed pipeline. Emit a
        # minimal start → complete pair (the frontend renders complete.output as
        # the assistant bubble) and return WITHOUT invoking the engine. Skipped
        # for Loop/Goal runs (promote_to_loop) — those are explicit, budgeted
        # executions where a greeting short-circuit would be surprising.
        if runner is None and _is_greeting(request.user_input or ""):
            _thread_id = context.thread_id or ""
            logger.info(
                f"[AGENT] /run-stream greeting short-circuit | "
                f"workflow={context.workflow_id or 'anon'} thread={_thread_id}"
            )
            from app.engine.interface import make_sse
            _reply = _greeting_reply(request.user_input or "")
            yield make_sse("start", {"message": "Starting workflow", "thread_id": _thread_id})
            yield make_sse("complete", {
                "output":          _reply,
                "execution_trace": [],
                "thread_id":       _thread_id,
                "generated_files": [],
                "usage":           {},
            })
            tracker.finalize("success")
            clear_log_context()
            return

        disconnected = False
        if runner is None:
            stream = get_engine().execute(chain, request.user_input, context)
        else:
            stream = runner.execute(
                loop=loop, goal=goal, chain=chain,
                user_input=request.user_input, ctx=context,
            )
        try:
            try:
                async for event in stream:
                    if await http_request.is_disconnected():
                        logger.info(f"[AGENT] Client disconnected from /run-stream{(' (loop mode)' if runner is not None else '')}; cancelling workflow={context.workflow_id or 'anon'} thread={context.thread_id or ''}")
                        disconnected = True
                        break
                    if event.startswith("data:"):
                        try:
                            payload = json.loads(event[5:].strip())
                            tracker.observe_event(payload)
                            # C3: fire-and-forget so the trace write does not add
                            # per-event latency to the live SSE token stream.
                            asyncio.create_task(_write_node_trace(tracker, payload))  # FR-T0-4
                            etype = payload.get("event") or payload.get("type") or ""
                            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                            if etype == "complete":
                                tid = data.get("thread_id") or ""
                                if tid:
                                    tracker.thread_id = tid
                        except Exception:
                            pass
                    yield event
            except Exception as exc:
                tracker.finalize("error", error=str(exc))
                raise
            else:
                tracker.finalize("disconnected" if disconnected else "success")
        finally:
            # Force the engine's async generator through its own GeneratorExit
            # handler so the user_cancelled snapshot is persisted before the
            # ASGI task ends. Without an explicit aclose() the generator is
            # closed by GC at an unpredictable time (or the surrounding task
            # cancellation kills it before the finally block runs).
            try:
                await stream.aclose()
            except Exception:  # noqa: BLE001
                logger.debug('[AGENT] stream aclose after cancel raised', exc_info=True)
            clear_log_context()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/resume-stream")
async def resume_workflow_stream_endpoint(
    request: ResumeRequest,
    http_request: Request,
    current_user: AuthenticatedUser = Depends(require_access),
):
    workflow_id   = request.workflow_id or ""
    workflow_name = request.workflow_name or ""
    thread_id     = request.thread_id or ""

    # ── Tool-binding integrity check (security review F-03) ─────────────
    # Only fires for a saved workflow this user owns; unsaved/draft graphs
    # (editor test-runs) are unaffected. See helper docstring for rationale.
    # Reviewer-added tool overrides on resume (pending_tool_calls_override)
    # are already checked separately, per-call, inside the engine
    # (native_engine.py's "Policy check for reviewer-added override tools").
    _binding_deny = await _enforce_tool_binding_integrity(
        workflow_id=workflow_id,
        posted_nodes=request.workflow.nodes,
        posted_edges=request.workflow.edges,
        current_user=current_user,
    )
    if _binding_deny:
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.workflow.resume_stream",
            action="tool_binding_denied",
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            thread_id=thread_id,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error=_binding_deny,
        )
        raise HTTPException(
            status_code=409,
            detail={"code": "TOOL_BINDING_DENIED", "message": _binding_deny},
        )

    # ── Thread-ownership check (security review F-06/F-10 follow-up) ────
    # NativeEngine.resume() already scopes its internal HITL-snapshot
    # lookup by owner (_load_interrupt(thread_id, context.user_id)), so a
    # cross-tenant resume was already blocked before this check existed —
    # but it surfaced as a generic "no paused interrupt found" 200 rather
    # than an explicit, audited 403. Adding the same check here gives a
    # consistent denial code/audit trail with /run and /run-stream and
    # fails fast before any budget/tracker work for a request that's going
    # to be rejected anyway.
    _thread_deny = await _enforce_thread_ownership(
        thread_id=thread_id, current_user=current_user,
    )
    if _thread_deny:
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.workflow.resume_stream",
            action="thread_ownership_denied",
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            thread_id=thread_id,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error=_thread_deny,
        )
        raise HTTPException(
            status_code=403,
            detail={"code": "THREAD_OWNERSHIP_DENIED", "message": _thread_deny},
        )

    # ── Budget preflight ────────────────────────────────────────────────
    # Skip budget only when the workflow uses no cloud model anywhere (all
    # node models + judge models are local, which incur no spend). Shared
    # policy — see deps.enforce_budget_or_downgrade.
    _budget = await enforce_budget_or_downgrade(
        user_id=current_user.id,
        endpoint="abstudio.workflow.resume_stream",
        skip_check=_workflow_is_all_local(request.workflow),
        downgrade=lambda m: _downgrade_workflow_to_model(request.workflow, m),
        audit_kwargs=dict(
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            thread_id=thread_id,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
        ),
    )

    tracker = RunUsageTracker(
        user_id=current_user.id,
        endpoint="abstudio.workflow.resume_stream",
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        thread_id=thread_id,
        email=current_user.email,
        department=getattr(current_user, "department", "") or "",
    )

    # ── Audit HITL resume request ───────────────────────────────────────
    # graph_hash records exactly which graph executed, independent of
    # whether it matched the persisted row (F-03 audit-trail remediation).
    audit_event(
        user_id=current_user.id,
        endpoint="abstudio.workflow.resume_stream",
        action="resume_requested",
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        thread_id=thread_id,
        request_id=tracker.request_id,
        email=current_user.email,
        department=getattr(current_user, "department", "") or "",
        extra={
            "has_override": bool(getattr(request, "pending_tool_calls_override", None)),
            "graph_hash": _graph_hash(request.workflow.nodes, request.workflow.edges),
        },
    )

    chain   = to_chain(request.workflow)
    context = build_execution_context(
        current_user,
        thread_id=thread_id,
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        subagents_enabled=getattr(request, "subagents_enabled", None),
        attachments=getattr(request, "attachments", None),
    )
    # Forward the reviewer-edited tool list into the engine's resume()
    # call. None = use the snapshot's original pending_tool_calls. Defined
    # on ExecutionContext so engine adapters can pick it up without
    # changing the resume() signature.
    context.pending_tool_calls_override = request.pending_tool_calls_override

    bind_log_context(
        current_user, thread_id=context.thread_id, request=http_request,
        request_id=tracker.request_id, span="workflow_resume",
    )

    async def event_generator():
        bind_log_context(
            current_user, thread_id=context.thread_id, request=http_request,
            request_id=tracker.request_id, span="workflow_resume",
        )
        # Budget store was down and we downgraded the graph to a no-cost local
        # model — tell the user before the resumed run continues.
        if _budget.notice:
            from app.engine.interface import make_sse
            yield make_sse("agent_fallback", {
                "agent": "Budget guard",
                "node_id": None,
                "primary_model": "the selected cloud model",
                "fallback_model": _budget.fallback_model,
                "reason": _budget.notice,
            })
        disconnected = False
        stream = get_engine().resume(chain, request.human_input, context)
        try:
            try:
                async for event in stream:
                    if await http_request.is_disconnected():
                        logger.info(f"[AGENT] Client disconnected from /resume-stream; cancelling workflow={context.workflow_id or 'anon'} thread={context.thread_id or ''}")
                        disconnected = True
                        break
                    if event.startswith("data:"):
                        try:
                            payload = json.loads(event[5:].strip())
                            tracker.observe_event(payload)
                            # C3: fire-and-forget so the trace write does not add
                            # per-event latency to the live SSE token stream.
                            asyncio.create_task(_write_node_trace(tracker, payload))  # FR-T0-4
                            etype = payload.get("event") or payload.get("type") or ""
                            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                            if etype == "complete":
                                tid = data.get("thread_id") or ""
                                if tid:
                                    tracker.thread_id = tid
                        except Exception:
                            pass
                    yield event
            except Exception as exc:
                tracker.finalize("error", error=str(exc))
                raise
            else:
                if disconnected:
                    tracker.finalize("disconnected")
                else:
                    tracker.finalize("success")
        finally:
            try:
                await stream.aclose()
            except Exception:  # noqa: BLE001
                logger.debug('[AGENT] resume stream aclose after cancel raised', exc_info=True)
            clear_log_context()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
