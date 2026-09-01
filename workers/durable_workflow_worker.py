# SPDX-License-Identifier: Apache-2.0
"""
workers/durable_workflow_worker.py — P11: Durable workflow execution.

Provides:
  1. execute_durable_workflow() — execute a workflow with checkpoint/resume
  2. resume_interrupted_workflows() — called at worker startup to re-enqueue
     workflows that were interrupted (status='running', updated_at > 5min ago)

Uses PlanningEngine (P8) for checkpoint/resume and rollback-aware execution.
"""

from core.logger import logger


def execute_durable_workflow(workflow_id: str, workflow_def: dict, triggered_by: str = "manual") -> dict:
    """
    Execute a workflow durably with checkpoint/resume support.

    Algorithm:
      1. Load checkpoint if exists (PlanningEngine.resume_plan())
      2. Reconstruct WorkflowStep objects from workflow_def
      3. Execute via PlanningEngine.execute_with_rollback()
      4. Checkpoint after each step (handled by PlanningEngine)
      5. Update workflow_history status on completion

    Returns execution summary dict.
    """
    result = {
        "workflow_id": workflow_id,
        "completed":   [],
        "failed":      [],
        "error":       None,
    }
    try:
        from workflows.engine import WorkflowStep
        from workflows.planner import PlanningEngine

        # Reconstruct steps from workflow_def
        steps_raw = workflow_def.get("steps", [])
        steps = []
        for s in steps_raw:
            if isinstance(s, dict):
                steps.append(WorkflowStep(
                    id=s.get("id", ""),
                    name=s.get("name", ""),
                    step_type=s.get("step_type", "tool"),
                    input=s.get("input", ""),
                    depends_on=s.get("depends_on", []),
                    timeout_sec=s.get("timeout_sec", 300),
                ))

        if not steps:
            logger.warning(f"durable_workflow_worker: no steps in workflow {workflow_id}")
            return result

        # Update workflow status to 'running'
        _update_workflow_status(workflow_id, "running")

        engine = PlanningEngine()

        def _step_executor(step):
            """Execute a single workflow step."""
            from workflows.engine import WorkflowEngine
            wf_engine = WorkflowEngine()
            # SEC-06 fix: correct method name is _run_step (not _execute_step)
            return wf_engine._run_step(step, outputs={})

        execution = engine.execute_with_rollback(
            workflow_id=workflow_id,
            steps=steps,
            executor_fn=_step_executor,
            stop_on_failure=workflow_def.get("stop_on_failure", True),
        )

        result["completed"] = execution.get("completed", [])
        result["failed"] = execution.get("failed", [])
        result["error"] = execution.get("error")

        final_status = "completed" if not execution.get("failed") else "failed"
        _update_workflow_status(workflow_id, final_status)

        logger.info(
            f"durable_workflow_worker: {workflow_id} {final_status} "
            f"completed={len(result['completed'])} failed={len(result['failed'])}"
        )

    except Exception as e:
        logger.error(f"durable_workflow_worker: {workflow_id} failed: {e}")
        result["error"] = str(e)
        _update_workflow_status(workflow_id, "failed")

    return result


def resume_interrupted_workflows() -> int:
    """
    Called at worker startup. Finds workflows stuck in 'running' status
    for more than 5 minutes and re-enqueues them if a checkpoint exists.

    Returns count of workflows re-enqueued.
    """
    re_enqueued = 0
    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _sqlt
        from datetime import datetime, timedelta

        db = SessionLocal()
        try:
            cutoff = datetime.utcnow() - timedelta(minutes=5)
            rows = db.execute(
                _sqlt(
                    """
                    SELECT workflow_id, workflow_name, metadata
                    FROM workflow_history
                    WHERE status = 'running'
                      AND created_at < :cutoff
                    LIMIT 50
                    """
                ),
                {"cutoff": cutoff},
            ).fetchall()
        finally:
            db.close()

        if not rows:
            return 0

        from workflows.planner import PlanningEngine
        engine = PlanningEngine()

        for row in rows:
            wf_id = row[0]
            wf_name = row[1]
            meta = row[2] or {}
            if isinstance(meta, str):
                import json
                meta = json.loads(meta)

            # Check if a checkpoint exists
            checkpoint = engine.resume_plan(wf_id)
            if not checkpoint:
                logger.info(
                    f"resume_interrupted_workflows: {wf_id} ({wf_name}) "
                    f"has no checkpoint — skipping"
                )
                continue

            # Re-enqueue
            try:
                from core.job_queue import enqueue_job
                workflow_def = meta.get("workflow_def") or {}
                enqueue_job(
                    fn_name="workers.durable_workflow_worker.execute_durable_workflow",
                    payload={
                        "workflow_id":  wf_id,
                        "workflow_def": workflow_def,
                        "triggered_by": "resume_on_startup",
                    },
                    queue_name="workflows",
                    timeout=3600,
                )
                re_enqueued += 1
                logger.info(
                    f"resume_interrupted_workflows: re-enqueued {wf_id} ({wf_name}) "
                    f"from checkpoint (completed_steps={checkpoint.get('completed_steps', [])})"
                )
            except Exception as e:
                logger.error(f"resume_interrupted_workflows: failed to re-enqueue {wf_id}: {e}")

    except Exception as e:
        logger.error(f"resume_interrupted_workflows failed: {e}")

    return re_enqueued


def _update_workflow_status(workflow_id: str, status: str) -> None:
    """Update workflow_history status for a workflow."""
    try:
        from memory.postgres_memory import PostgresMemory
        mem = PostgresMemory()
        if not mem.available:
            return
        with mem._cursor() as cur:
            cur.execute(
                "UPDATE workflow_history SET status = %s WHERE workflow_id = %s",
                (status, workflow_id),
            )
        mem._conn.commit()
    except Exception as e:
        logger.debug(f"_update_workflow_status failed (non-fatal): {e}")
