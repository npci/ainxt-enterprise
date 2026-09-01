# SPDX-License-Identifier: Apache-2.0
# ============================================================
# JOBS ROUTER — /jobs
# Manage async job queue: submit, inspect, cancel, list
# ============================================================

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from auth.dependencies import get_current_user as _require_auth

router = APIRouter(prefix="/jobs", tags=["jobs"])


# SEC-02: Strict allowlist of permitted worker function paths.
# Only these dotted paths may be submitted via the API.
_ALLOWED_FN_NAMES = frozenset({
    "workers.sdlc_worker.run_feature_pipeline_job",
    "workers.sdlc_worker.run_bug_pipeline_job",
    "workers.sdlc_worker.run_mr_review_job",
    "workers.sdlc_worker.run_mr_merge_job",
    "workers.sdlc_worker.run_reindex_job",
    "workers.durable_workflow_worker.execute_durable_workflow",
    "workers.chat_worker.run_chat_job",
    "workers.graph_worker.run_graph_index_job",
    "workers.memory_maintenance_worker.run_memory_maintenance",
    "workers.feedback_loop_worker.run_feedback_loop",
    "workers.workflow_scheduler_worker.dispatch_scheduled_workflows",
})


class JobSubmitRequest(BaseModel):
    fn_name:     str              # dotted function path — must be in _ALLOWED_FN_NAMES
    payload:     dict
    queue_name:  Optional[str] = "default"
    timeout:     Optional[int] = 900
    retry_count: Optional[int] = 2


@router.post("")
def submit_job(
    req: JobSubmitRequest,
    current_user: dict = Depends(_require_auth),
):
    """Submit a job to the queue. Returns job_id. Requires authentication."""
    # SEC-02: validate fn_name against allowlist to prevent arbitrary code execution
    if req.fn_name not in _ALLOWED_FN_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"fn_name {req.fn_name!r} is not in the permitted worker allowlist",
        )
    from core.job_queue import enqueue_job
    job_id = enqueue_job(
        fn_name=req.fn_name,
        payload=req.payload,
        queue_name=req.queue_name,
        timeout=req.timeout,
        retry_count=req.retry_count,
    )
    return {"job_id": job_id, "queue": req.queue_name, "status": "queued"}


@router.delete("/{job_id}")
def cancel_job(
    job_id: str,
    current_user: dict = Depends(_require_auth),
):
    """Cancel a queued or running job."""
    from core.job_queue import cancel_job as _cancel
    ok = _cancel(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found or already finished")
    return {"job_id": job_id, "cancelled": True}


@router.get("/{job_id}")
def get_job(
    job_id: str,
    current_user: dict = Depends(_require_auth),
):
    """Get status of a specific job by ID."""
    from core.job_queue import get_job_status
    status = get_job_status(job_id)
    if status.get("status") == "unknown":
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return status


@router.get("")
def list_jobs(
    queue: Optional[str] = Query(default=None, description="Filter by queue name; omit to list all queues"),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(_require_auth),
):
    """List recent jobs. Pass queue= to filter by a specific queue, or omit to get all queues."""
    if queue:
        from core.job_queue import list_jobs as _list_jobs
        return {"jobs": _list_jobs(queue_name=queue, limit=limit)}
    else:
        from core.job_queue import list_all_jobs
        return {"jobs": list_all_jobs(limit=limit)}


@router.get("/stats/queues")
def queue_stats(current_user: dict = Depends(_require_auth)):
    """Return job counts per queue (queued / started / finished / failed)."""
    from core.job_queue import queue_stats as _stats
    return _stats()


# ── P8: Workflow plan endpoints ───────────────────────────────────────────────

@router.get("/workflows/{workflow_id}/plan")
def get_workflow_plan(
    workflow_id: str,
    current_user: dict = Depends(_require_auth),
):
    """
    P8: Return the critical path analysis and risk scores for a workflow.

    Loads the workflow from workflow_history, runs PlanningEngine.analyze_plan(),
    and returns the plan analysis (critical path, risk scores, estimated duration).
    """
    try:
        from memory.postgres_memory import PostgresMemory
        mem = PostgresMemory()
        wf = mem.get_workflow_run(workflow_id)
        if not wf:
            raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")

        steps_raw = wf.get("steps") or []
        if isinstance(steps_raw, str):
            import json as _j
            steps_raw = _j.loads(steps_raw)

        # Reconstruct lightweight step objects for analysis
        from workflows.engine import WorkflowStep
        steps = []
        for s in steps_raw:
            if isinstance(s, dict):
                steps.append(WorkflowStep(
                    id=s.get("id", ""),
                    name=s.get("name", ""),
                    step_type=s.get("step_type", "tool"),
                    depends_on=s.get("depends_on", []),
                    timeout_sec=s.get("timeout_sec", 300),
                ))

        from workflows.planner import PlanningEngine
        engine = PlanningEngine()
        analysis = engine.analyze_plan(steps)

        return {
            "workflow_id":            workflow_id,
            "critical_path":          analysis.critical_path,
            "total_risk":             round(analysis.total_risk, 3),
            "estimated_duration_sec": analysis.estimated_duration_sec,
            "step_risks":             {k: round(v, 3) for k, v in analysis.step_risks.items()},
            "step_slack":             {k: round(v, 1) for k, v in analysis.step_slack.items()},
            "rollback_order":         analysis.rollback_order,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Plan analysis failed: {e}")


@router.post("/workflows/{workflow_id}/resume")
def resume_workflow(
    workflow_id: str,
    current_user: dict = Depends(_require_auth),
):
    """
    P8: Resume an interrupted workflow from its last checkpoint.

    Loads the Redis checkpoint for workflow_id and returns the saved state
    (completed steps, results). The caller is responsible for re-enqueuing
    the workflow job with the checkpoint data.
    """
    import time as _time_mod
    try:
        from workflows.planner import PlanningEngine
        engine = PlanningEngine()
        checkpoint = engine.resume_plan(workflow_id)
        if not checkpoint:
            raise HTTPException(
                status_code=404,
                detail=f"No checkpoint found for workflow {workflow_id}",
            )
        return {
            "workflow_id":      workflow_id,
            "checkpoint":       checkpoint,
            "completed_steps":  checkpoint.get("completed_steps", []),
            "checkpoint_age_sec": round(
                _time_mod.time() - checkpoint.get("_checkpoint_ts", 0), 1
            ),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Resume failed: {e}")
