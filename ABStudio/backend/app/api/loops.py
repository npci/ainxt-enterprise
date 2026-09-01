# SPDX-License-Identifier: Apache-2.0
"""Loops REST surface."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.logger import logger
from app.api.deps import build_execution_context, require_access, bind_log_context, clear_log_context
from app.engine import get_engine
from app.engine.interface import ChainDefinition, ChainEdge
from app.loop import repo as loops_repo
from app.loop.models import Goal, LoopRecord
from app.loop.runner import LoopRunner
from app.models import AuthenticatedUser

router = APIRouter(tags=["loops"])


# ---------------------------------------------------------------------------
# Loop execution (shared backend support only)
# ---------------------------------------------------------------------------


async def _resolve_loop_chain(
    loop: LoopRecord, current_user: AuthenticatedUser,
) -> ChainDefinition:
    """Resolve the inner workflow chain a Loop should execute.

    Only ``action.engine == 'workflow'`` is supported in P2 — the spec
    promises ``engine='agent'`` (synthesise a 1-node chain) but defers
    it until the agent-registry contract for that single-node wrap is
    finalised. Until then an agent-engine loop returns a clear 422.
    """
    if loop.action.engine != "workflow":
        raise HTTPException(
            status_code=422,
            detail={
                "error":   "unsupported_engine",
                "message": f"action.engine='{loop.action.engine}' is not "
                           "supported by /loops/run-stream in v1; only "
                           "engine='workflow' ships in this release.",
            },
        )

    target_id = (loop.action.target_id or "").strip()
    if not target_id:
        raise HTTPException(
            status_code=422,
            detail={"error": "missing_target",
                    "message": "loop.action.target_id is empty"},
        )

    # Lazy import — keeps the loops router import-light if workflow_repo
    # ever grows new heavyweight deps.
    from app.core import workflow_repo as wf_repo
    wf = await wf_repo.get_workflow(target_id, current_user.id)
    if not wf:
        raise HTTPException(
            status_code=404,
            detail={"error": "workflow_not_found",
                    "message": f"workflow '{target_id}' referenced by "
                               "loop.action.target_id is not visible to "
                               "this user"},
        )

    graph = wf.get("graphData") or {}
    nodes = graph.get("nodes") or []
    raw_edges = graph.get("edges") or []
    edges = [
        ChainEdge(
            source=e.get("source", ""),
            target=e.get("target", ""),
            source_handle=e.get("sourceHandle"),
        )
        for e in raw_edges
    ]
    return ChainDefinition(
        nodes=nodes,
        edges=edges,
        knowledge=wf.get("knowledge"),
    )


@router.post("/loops/{loop_id}/run-stream")
async def run_loop_stream_route(
    loop_id: str,
    payload: Dict[str, Any],
    http_request: Request,
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Execute a stored Loop end-to-end through the LoopRunner.

    Body shape (all optional except ``user_input``):
        {
          "user_input":   "...",
          "thread_id":    "abc",      # optional — autoderived from loop+user
          "goal_id":      "g_123",    # optional override
          "budget":       {"tokens": ..., "wall_clock_s": ..., "max_iterations": ...},
          "trigger_src":  "manual"    # default "manual"
        }
    """
    loop = await loops_repo.get_loop(loop_id)
    if not loop:
        raise HTTPException(status_code=404, detail="loop not found")
    if not loop.enabled:
        raise HTTPException(
            status_code=409,
            detail={"error": "loop_disabled",
                    "message": "loop is disabled — re-enable before running"},
        )

    user_input = str(payload.get("user_input") or "").strip()
    if not user_input:
        raise HTTPException(
            status_code=422,
            detail={"error": "missing_user_input",
                    "message": "user_input is required"},
        )

    chain = await _resolve_loop_chain(loop, current_user)

    # Optional Goal resolution — when present it adds the predicate gate
    # on top of proof. When absent the runner ships on proof alone.
    goal = None
    goal_id = payload.get("goal_id")
    if goal_id:
        goal = await loops_repo.get_goal(str(goal_id))
        if not goal:
            raise HTTPException(
                status_code=404,
                detail={"error": "goal_not_found",
                        "message": f"goal '{goal_id}' not found"},
            )

    budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else None
    trigger_src = str(payload.get("trigger_src") or "manual")
    thread_id = payload.get("thread_id") or None

    # ── Thread-ownership check (security review F-06/F-10 follow-up) ────
    # This route ultimately calls the same NativeEngine.execute() as
    # /run-stream (via LoopRunner), which writes into chat_threads keyed by
    # ``thread_id`` — the same cross-tenant read/write gap applies here: a
    # client-supplied thread_id belonging to another user must be rejected
    # before any read/write happens. See execution.py's
    # _enforce_thread_ownership docstring for full rationale; duplicated
    # here (rather than imported) to avoid a loops.py → execution.py
    # import for one small check.
    if thread_id:
        try:
            _owner = await get_engine().get_thread_owner(str(thread_id))
        except Exception:
            logger.exception('[AGENT] loops run-stream: thread ownership lookup failed; skipping check')
            _owner = None
        if _owner and _owner != current_user.id:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "THREAD_OWNERSHIP_DENIED",
                    "message": (
                        "thread_id belongs to a different user. Start a new "
                        "conversation instead of reusing another user's thread_id."
                    ),
                },
            )

    context = build_execution_context(
        current_user,
        thread_id=thread_id,
        workflow_id=loop.action.target_id,
        workflow_name=loop.name,
        loop_id=loop.id,
        goal_id=(goal.id if goal else None),
        budget=budget,
        trigger_src=trigger_src,
    )

    runner = LoopRunner()

    bind_log_context(current_user, thread_id=context.thread_id, request=http_request, span="loop_run")

    async def event_generator():
        bind_log_context(current_user, thread_id=context.thread_id, request=http_request, span="loop_run")
        try:
            async for event in runner.execute(
                loop=loop, goal=goal, chain=chain,
                user_input=user_input, ctx=context,
            ):
                if await http_request.is_disconnected():
                    logger.info(f"[AGENT] Client disconnected from /loops/{loop_id}/run-stream; cancelling thread={context.thread_id or ''}")
                    break
                yield event
        finally:
            clear_log_context()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )
