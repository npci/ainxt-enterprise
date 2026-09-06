# SPDX-License-Identifier: MIT
"""
workers/workflow_scheduler_worker.py — P11: Scheduled workflow dispatcher.

Runs every minute via the cron scheduler in start_workers.py.

Queries scheduled_workflows WHERE is_active=true AND next_run_at <= NOW(),
enqueues due workflows to the RQ "workflows" queue, and updates next_run_at
using croniter.

Event-triggered workflows are NOT handled here — they are triggered by
POST /webhooks/workflow-trigger/{event_name} in webhooks_router.py.
"""

import os
import sys

from core.logger import logger


def _ensure_abstudio_on_path() -> None:
    """Prepend ``<repo>/AgentStudio/backend`` to sys.path so ``app.*`` imports work.

    The gateway process does this in ``gateway.py``. Worker processes launched
    via ``workers/start_workers.py`` don't go through gateway.py, so we mirror
    the same insertion here. Idempotent — the path is only added once.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    abs_backend = os.path.join(repo_root, "AgentStudio", "backend")
    if os.path.isdir(abs_backend) and abs_backend not in sys.path:
        sys.path.insert(0, abs_backend)


def dispatch_scheduled_workflows() -> dict:
    """
    Find and dispatch all scheduled workflows that are due.

    Returns a summary dict for logging/monitoring.
    Called every 60s by the cron scheduler in start_workers.py.
    """
    result = {
        "dispatched": 0,
        "errors":     0,
        "error":      None,
    }
    try:
        from db.database import SessionLocal
        from db.models import ScheduledWorkflow
        from sqlalchemy import text as _sqlt
        from datetime import datetime

        db = SessionLocal()
        try:
            now = datetime.utcnow()
            due = (
                db.query(ScheduledWorkflow)
                .filter(
                    ScheduledWorkflow.is_active == True,
                    ScheduledWorkflow.next_run_at <= now,
                    ScheduledWorkflow.cron_expr != None,
                )
                .all()
            )

            for wf in due:
                try:
                    _enqueue_workflow(wf)
                    result["dispatched"] += 1

                    # Update next_run_at using croniter
                    next_run = _compute_next_run(wf.cron_expr)
                    wf.last_run_at = now
                    wf.next_run_at = next_run
                    db.commit()
                    logger.info(
                        f"workflow_scheduler: dispatched {wf.name} (id={wf.id}) "
                        f"next_run={next_run}"
                    )
                except Exception as e:
                    result["errors"] += 1
                    logger.error(f"workflow_scheduler: failed to dispatch {wf.name}: {e}")
        finally:
            db.close()

    except Exception as e:
        logger.error(f"workflow_scheduler_worker failed: {e}")
        result["error"] = str(e)

    return result


def _enqueue_workflow(wf) -> None:
    """Enqueue a ScheduledWorkflow to the RQ 'workflows' queue."""
    from core.job_queue import enqueue_job
    enqueue_job(
        fn_name="workers.durable_workflow_worker.execute_durable_workflow",
        payload={
            "workflow_id":  str(wf.id),
            "workflow_def": wf.workflow_def or {},
            "triggered_by": "scheduler",
        },
        queue_name="workflows",
        timeout=3600,
    )


def _compute_next_run(cron_expr: str):
    """Compute the next run time from a cron expression using croniter."""
    from datetime import datetime
    try:
        from croniter import croniter
        itr = croniter(cron_expr, datetime.utcnow())
        return itr.get_next(datetime)
    except Exception as e:
        logger.warning(f"workflow_scheduler: croniter failed for {cron_expr!r}: {e}")
        # Fallback: 1 hour from now
        from datetime import timedelta
        return datetime.utcnow() + timedelta(hours=1)


# ---------------------------------------------------------------------------
# AB Studio user-defined triggers (``triggers`` table) — one-line dispatch.
#
# Reuses the existing psycopg pool in ABStudio's ``workflow_repo``, the schedule
# parser in ``trigger_scheduler``, and ``core.job_queue.enqueue_job``. No new
# tables, no new queues, no leader election — an atomic conditional UPDATE
# (see the WHERE clause below) guarantees each due row is claimed exactly
# once, so even if two dispatchers ever run in parallel they cannot
# double-enqueue.
# ---------------------------------------------------------------------------

def dispatch_due_triggers() -> dict:
    """Poll ``triggers`` for due rows, enqueue a fire job for each.

    Called every 60 s by the cron scheduler in ``start_workers.py``. Runs in a
    single scheduler-worker process (only one is started), so the multi-worker
    duplicate-fire problem never arises.
    """
    result = {"dispatched": 0, "errors": 0, "error": None}
    try:
        # Lazy imports — ABStudio may not be on the path in every deployment,
        # so we don't want to fail this worker's import chain over it.
        # Gateway startup (gateway.py) prepends AgentStudio/backend to sys.path so
        # the same `app.*` imports the ABStudio code uses internally resolve here.
        _ensure_abstudio_on_path()
        from app.core import workflow_repo as _repo
        from app.services.trigger_scheduler import _compute_next_run_at
        from core.job_queue import enqueue_job
        from datetime import datetime, timezone

        # ABStudio's connection pool is a per-process module-level global. In
        # multi-worker gunicorn (or in workers not launched via ABStudio's
        # FastAPI lifespan), the process running this cron may not have had
        # workflow_repo.init_db() called on it, so _repo.get_pool() is None
        # even though the platform's shared pool exists in this process.
        # Bind lazily to the platform's SHARED_POOL — same object init_db
        # would assign — without repeating the (heavy, run-once) table
        # creation / template seeding that init_db does.
        pool = _repo.get_pool()
        if pool is None:
            try:
                from app.core.db_pool import SHARED_POOL
                _repo._pool = SHARED_POOL
                pool = SHARED_POOL
                logger.info(
                    "trigger_dispatcher: bound workflow_repo pool to platform "
                    "SHARED_POOL (was None in this process)"
                )
            except Exception as _e:
                logger.warning(
                    f"trigger_dispatcher: could not bind workflow_repo pool: {_e}"
                )
                return result

        now_utc = datetime.now(timezone.utc)
        with pool.connection() as conn:
            # Atomic claim: only rows still due after this UPDATE returns
            # are ours to fire. Any concurrent dispatcher's UPDATE will see
            # next_run_at moved forward and skip the row. We advance
            # next_run_at to a temporary sentinel (now + 1 minute) so if the
            # enqueue crashes mid-batch we don't spin. The real next_run_at
            # is computed and written below per row.
            rows = conn.execute(
                "SELECT id, schedule FROM triggers "
                "WHERE enabled = TRUE "
                "  AND next_run_at IS NOT NULL "
                "  AND next_run_at <= %s "
                "FOR UPDATE SKIP LOCKED",
                (now_utc,),
            ).fetchall()
            # Diagnostic — surfaces "SELECT ran, returned N rows" so the
            # difference between "no due rows" and "SELECT never executed"
            # is visible in the log without needing DB access.
            logger.info(
                f"trigger_dispatcher: tick now_utc={now_utc.isoformat()} "
                f"due_rows={len(rows)}"
            )

            # Two-phase per row: (1) advance next_run_at + commit so the row
            # can never be re-selected by another scheduler-worker host, then
            # (2) enqueue to Redis. This ordering prefers "occasionally lose
            # a fire on scheduler-host crash" over "occasionally double-fire",
            # which is the safer trade-off for user-visible triggers.
            claimed = []
            for row in rows:
                trigger_id = row[0]
                schedule = row[1] or {}
                if isinstance(schedule, str):
                    try:
                        import json as _json
                        schedule = _json.loads(schedule)
                    except Exception:
                        schedule = {}
                try:
                    next_run = _compute_next_run_at(schedule)
                    conn.execute(
                        "UPDATE triggers SET next_run_at = %s WHERE id = %s",
                        (next_run, trigger_id),
                    )
                    claimed.append((trigger_id, next_run))
                except Exception as e:
                    result["errors"] += 1
                    logger.error(
                        f"trigger_dispatcher: claim failed for {trigger_id}: {e}"
                    )
            # Commit the claims BEFORE talking to Redis. FOR UPDATE row locks
            # are released here; a second scheduler-worker on another host
            # that ticks a millisecond later will see next_run_at moved forward
            # and its SELECT will return no rows.
            conn.commit()

        # Redis enqueue happens OUTSIDE the DB transaction. If Redis is down
        # the row's next_run_at is already advanced, so we log and move on —
        # a lost fire beats a duplicate fire.
        for trigger_id, next_run in claimed:
            try:
                enqueue_job(
                    fn_name="app.services.trigger_scheduler.fire_from_queue",
                    payload={"trigger_id": trigger_id},
                    queue_name="default",
                    timeout=1800,   # fires can be slow — LLM + tools
                    retry_count=0,  # _fire_trigger already persists errors
                )
                result["dispatched"] += 1
                logger.info(
                    f"trigger_dispatcher: enqueued trigger={trigger_id} "
                    f"next_run={next_run}"
                )
            except Exception as e:
                result["errors"] += 1
                logger.error(
                    f"trigger_dispatcher: enqueue failed for {trigger_id} "
                    f"(next_run_at already advanced, fire lost): {e}"
                )
    except Exception as e:
        logger.error(f"trigger_dispatcher failed: {e}")
        result["error"] = str(e)
    return result
