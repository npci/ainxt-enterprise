#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# ============================================================
# COWORK SCHEDULED TASKS ROUTER — CRUD + run-now + history + approve-action
#
# Manages user-owned recurring Cowork tasks persisted in the
# `cowork_scheduled_tasks` table. A background scheduler (rq-scheduler
# worker — see integration notes) reads this same table on its cron tick
# and enqueues `workers.cowork_task_worker.run_scheduled_task`. This router
# never executes the task itself; "run-now" simply enqueues the SAME worker
# fn immediately via core/job_queue.
#
# Endpoints (prefix="/buddy/tasks", JWT-gated, scoped to current_user["sub"]):
#   GET    /                         — list my scheduled tasks
#   POST   /                         — create {prompt, cron, role, connectors}
#   PUT    /{task_id}                — update mutable fields
#   DELETE /{task_id}                — delete (hard delete; owner-scoped)
#   POST   /{task_id}/run-now        — enqueue immediately (does NOT auto-execute writes)
#   GET    /{task_id}/history        — recent run history for this task
#   PUT    /{task_id}/approve-action — set/clear the pre-approved connector action
#                                      that the worker will execute after each run
#                                      (e.g. send the result to a specific recipient)
#
# AiNxt guardrails:
#   - The task `prompt` is a *future outbound instruction* that the local
#     agent will execute (it may drive connector/doc WRITES). On create/update
#     we therefore: REDACT incidental PII (so reads/lists never leak), but
#     HARD-BLOCK if the prompt carries secrets/keys/tokens (block-configured
#     types) — persisting credentials into a recurring instruction is an
#     outbound-write risk and must be refused, not silently stored.
#   - Connector/doc WRITES still NEVER auto-execute: the enqueued worker runs
#     the agent which routes any write/send through the existing confirm +
#     compliance-gated path (POST /connectors/action, workers/doc_worker.py).
#     run-now only schedules the run; it grants no write bypass.
#   - approve-action: the user explicitly pre-authorises ONE specific connector
#     write (e.g. "send the result to alice@example.com via Outlook"). The
#     worker still HARD-BLOCKs on sensitive outbound content before executing.
#   - Never log prompt bodies, connector secrets, or tokens.
# ============================================================

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from auth.dependencies import get_current_user
from core.config import BUDDY_SCHED_MAX_PER_USER, BUDDY_SCHED_MAX_RUNS
from core.logger import logger, mask_email

router = APIRouter(prefix="/buddy/tasks", tags=["buddy"])

# Worker fn the scheduler enqueues on each cron tick. run-now enqueues the SAME
# fn so scheduled and manual runs share one execution path.
RUN_TASK_FN = "workers.cowork_task_worker.run_scheduled_task"

_MAX_PROMPT_LEN     = 8000
_MAX_CONNECTORS     = 32
# Statuses the CALLER may set via PUT. `completed` is terminal and only ever
# written by workers/cowork_scheduler.py once a range/occurrence limit hits —
# accepting it here would let a client "reopen" a finished task, which the UX
# forbids. See _forbid_when_completed() below.
_VALID_STATUSES     = {"active", "paused"}
_TERMINAL_STATUS    = "completed"


# ── DB helpers (same style as profile_router) ────────────────────────────────
def _db():
    from db.database import engine
    from sqlalchemy import text
    return engine, text


# ── Compliance helpers ───────────────────────────────────────────────────────
def _redact_for_storage(prompt: str) -> str:
    """Redact-and-proceed for incidental PII, but HARD-BLOCK secrets/keys/tokens.

    A scheduled prompt is replayed by the agent on every tick and surfaces in
    list/history reads, so credentials must never be persisted. Raises 422 if a
    block-configured type (SECRET / API_KEY / *_KEY_LEAK / etc.) is present.
    Returns the redacted prompt safe to store.

    keep_types: a scheduled Cowork prompt is a tool-driven instruction (e.g.
    "send an email to user@example.com"). Contact identifiers (EMAIL/MOBILE/
    UPI) MUST survive storage — redacting them to "[EMAIL]" strips the recipient
    so the worker can't resolve who to send to and falls back to self-email.
    This mirrors the live Cowork /ask path (gateway.py) and connectors/mcp_bridge.
    Secrets/keys/cards are NOT kept — they stay redacted and hard-blocked.
    """
    try:
        from agents.compliance_engine import compliance_engine
        result = compliance_engine.validate_input(
            prompt, keep_types={"EMAIL", "MOBILE", "UPI"}
        )
    except HTTPException:
        raise
    except Exception as exc:
        # Fail closed for an unexpected compliance error on an outbound-bound write.
        logger.error(f"cowork_tasks: compliance check failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Compliance service unavailable — task not saved.",
        )

    if result.get("blocked"):
        blocked_types = sorted({
            f.get("type") for f in result.get("findings", [])
            if f.get("blocked")
        } - {None})
        # Do NOT echo the offending value — only the type names.
        logger.warning(f"cowork_tasks: prompt BLOCKED types={blocked_types}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Task prompt contains sensitive credentials and cannot be saved "
                f"({', '.join(blocked_types) or 'sensitive content'}). "
                "Remove secrets/keys/tokens and resubmit."
            ),
        )

    return result.get("redacted_text", prompt)


def _redact_for_read(prompt: Optional[str]) -> str:
    """Redact-and-proceed for outbound reads — NEVER blocks the user."""
    if not prompt:
        return ""
    try:
        from agents.compliance_engine import compliance_engine
        redacted, _types = compliance_engine.redact_text(prompt)
        return redacted
    except Exception:
        return prompt


# ── Schemas ───────────────────────────────────────────────────────────────────
# Recurrence fields (Outlook-style Recurrence editor, 2026-08-10) are all
# optional so legacy callers that only send {prompt, cron, role, ...} keep
# working. When the new UI is used it also POSTs `recurrence` (the raw editor
# state) and `summary` (the natural-language description shown in the list).
class TaskCreate(BaseModel):
    prompt:     str               = Field(..., min_length=1, max_length=_MAX_PROMPT_LEN)
    cron:       str               = Field(..., min_length=1, max_length=120)
    role:       Optional[str]     = Field(None, max_length=120)
    connectors: List[str]         = Field(default_factory=list)
    project_id: Optional[str]     = Field(None, max_length=64)   # link to a Cowork project
    tz:         Optional[str]     = Field(None, max_length=64)   # IANA tz, e.g. 'Asia/Kolkata'

    # ── Recurrence extensions ─────────────────────────────────────────────
    starts_at:       Optional[datetime] = None                    # range-of-recurrence start
    ends_at:         Optional[datetime] = None                    # "End by <date>"
    max_runs:        Optional[int]      = Field(None, ge=1, le=BUDDY_SCHED_MAX_RUNS)   # "End after N occurrences"
    interval_weeks:  Optional[int]      = Field(None, ge=1, le=52)
    interval_months: Optional[int]      = Field(None, ge=1, le=24)
    recurrence:      Optional[Dict[str, Any]] = None              # verbatim RecurrenceEditor state
    summary:         Optional[str]      = Field(None, max_length=500)


class TaskUpdate(BaseModel):
    prompt:     Optional[str]       = Field(None, min_length=1, max_length=_MAX_PROMPT_LEN)
    cron:       Optional[str]       = Field(None, min_length=1, max_length=120)
    role:       Optional[str]       = Field(None, max_length=120)
    connectors: Optional[List[str]] = None
    status:     Optional[str]       = None  # "active" | "paused" (never "completed" — terminal)

    # Recurrence fields (all optional — set only what changed)
    starts_at:       Optional[datetime] = None
    ends_at:         Optional[datetime] = None
    max_runs:        Optional[int]      = Field(None, ge=1, le=BUDDY_SCHED_MAX_RUNS)
    interval_weeks:  Optional[int]      = Field(None, ge=1, le=52)
    interval_months: Optional[int]      = Field(None, ge=1, le=24)
    recurrence:      Optional[Dict[str, Any]] = None
    summary:         Optional[str]      = Field(None, max_length=500)


class TaskOut(BaseModel):
    id:         str
    prompt:     str
    cron:       str
    role:       Optional[str]
    connectors: List[str]
    status:     str
    last_run_at:    Optional[str]
    last_run_status: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]
    next_run_at:     Optional[str]  = None   # when the task will next fire (from scheduler)
    tz:              Optional[str]  = None   # IANA tz the cron is evaluated in
    approved_action: Optional[dict] = None   # pre-approved connector write action (JSONB)
    action_allowlist: List[str]     = Field(default_factory=list)  # allowlisted connector.tool keys

    # ── Recurrence extensions ─────────────────────────────────────────────
    starts_at:       Optional[str]  = None
    ends_at:         Optional[str]  = None
    max_runs:        Optional[int]  = None
    runs_count:      int            = 0
    interval_weeks:  Optional[int]  = None
    interval_months: Optional[int]  = None
    recurrence:      Optional[dict] = None
    summary:         Optional[str]  = None


# ── Internal: validation + row shaping ───────────────────────────────────────
def _validate_connectors(connectors: List[str]) -> List[str]:
    if connectors is None:
        return []
    if len(connectors) > _MAX_CONNECTORS:
        raise HTTPException(400, detail=f"Too many connectors (max {_MAX_CONNECTORS}).")
    cleaned = []
    for c in connectors:
        if not isinstance(c, str):
            raise HTTPException(400, detail="connectors must be a list of strings.")
        c = c.strip()
        if c:
            cleaned.append(c[:120])
    return cleaned


def _coerce_connectors(raw) -> List[str]:
    """DB column is jsonb/text — coerce to list[str] defensively for output."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _coerce_approved_action(raw) -> Optional[dict]:
    """Coerce the JSONB approved_action column to dict or None."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    # Guard: if the column is still BOOLEAN on an un-migrated DB, treat as None.
    return None


def _coerce_recurrence(raw) -> Optional[dict]:
    """Coerce the JSONB recurrence column to dict or None."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _row_to_out(row) -> TaskOut:
    # SELECT order (see _SELECT_COLS): id, prompt, cron, role, connectors, status,
    #   last_run, last_run_status, created_at, updated_at, next_run, tz,
    #   approved_action, action_allowlist,
    #   starts_at, ends_at, max_runs, runs_count, interval_weeks, interval_months,
    #   recurrence, summary
    # The DB column is `last_run`; the response field is aliased to last_run_at.
    return TaskOut(
        id=str(row[0]),
        prompt=_redact_for_read(row[1]),
        cron=row[2],
        role=row[3],
        connectors=_coerce_connectors(row[4]),
        status=row[5] or "active",
        last_run_at=row[6].isoformat() if row[6] else None,
        last_run_status=row[7],
        created_at=row[8].isoformat() if row[8] else None,
        updated_at=row[9].isoformat() if row[9] else None,
        next_run_at=row[10].isoformat() if len(row) > 10 and row[10] else None,
        tz=(row[11] if len(row) > 11 else None) or "UTC",
        approved_action=_coerce_approved_action(row[12]) if len(row) > 12 else None,
        action_allowlist=_coerce_connectors(row[13]) if len(row) > 13 else [],
        starts_at=row[14].isoformat() if len(row) > 14 and row[14] else None,
        ends_at=row[15].isoformat() if len(row) > 15 and row[15] else None,
        max_runs=(row[16] if len(row) > 16 else None),
        runs_count=int(row[17] or 0) if len(row) > 17 else 0,
        interval_weeks=(row[18] if len(row) > 18 else None),
        interval_months=(row[19] if len(row) > 19 else None),
        recurrence=_coerce_recurrence(row[20]) if len(row) > 20 else None,
        summary=(row[21] if len(row) > 21 else None),
    )


_SELECT_COLS = (
    "id, prompt, cron, role, connectors, status, "
    "last_run, last_run_status, created_at, updated_at, next_run, tz, "
    "approved_action, action_allowlist, "
    "starts_at, ends_at, max_runs, runs_count, interval_weeks, interval_months, "
    "recurrence, summary"
)


def _fetch_owned(conn, text, task_id: str, uid: str):
    """Fetch a single task scoped to its owner. Returns row or None."""
    return conn.execute(
        text(f"""
            SELECT {_SELECT_COLS}
            FROM cowork_scheduled_tasks
            WHERE id = :tid AND user_id = :uid
        """),
        {"tid": task_id, "uid": uid},
    ).fetchone()


def _fetch_owned_status(conn, text, task_id: str, uid: str) -> Optional[str]:
    """Lightweight lookup used to gate mutations on `completed` tasks.

    Returns the task's status (`active` / `paused` / `completed`) or None if the
    task doesn't exist or the caller doesn't own it. Kept separate from
    `_fetch_owned` so we don't pay for the full column set when all we need is
    the terminal-status check.
    """
    row = conn.execute(
        text("SELECT status FROM cowork_scheduled_tasks WHERE id = :tid AND user_id = :uid"),
        {"tid": task_id, "uid": uid},
    ).fetchone()
    return (row[0] if row else None)


def _forbid_when_completed(conn, text, task_id: str, uid: str) -> None:
    """Raise 409 if the task is in the terminal `completed` state, 404 if it
    doesn't exist for this owner. Used by every mutation endpoint so the
    "greyed-out, non-interactive" contract in the UI is enforced server-side.
    """
    st = _fetch_owned_status(conn, text, task_id, uid)
    if st is None:
        raise HTTPException(404, detail="Task not found")
    if st == _TERMINAL_STATUS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This schedule has finished and can no longer be edited or re-run. Delete it and create a new one.",
        )


def _validate_range(starts_at: Optional[datetime], ends_at: Optional[datetime]) -> None:
    """Reject an inverted range (End by must be after Start)."""
    if starts_at and ends_at and ends_at <= starts_at:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="End date must be after the start date.",
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/limits")
async def get_scheduler_limits(current_user: dict = Depends(get_current_user)):
    """Return the per-user scheduler limits configured on this deployment.

    Declared before /{task_id} routes so FastAPI does not treat the literal
    string 'limits' as a task_id path parameter.
    """
    return {
        "max_schedulers_per_user": BUDDY_SCHED_MAX_PER_USER,
        "max_runs_per_scheduler":  BUDDY_SCHED_MAX_RUNS,
    }


@router.get("", response_model=List[TaskOut])
async def list_tasks(project_id: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    """List the caller's scheduled Cowork tasks (most recent first). Pass
    ?project_id=<id> to see only that project's schedules, or project_id='none'
    for schedules not attached to any project."""
    engine, text = _db()
    clause = ""
    params = {"uid": current_user["sub"]}
    if project_id == "none":
        clause = " AND project_id IS NULL"
    elif project_id:
        clause = " AND project_id = :pid"
        params["pid"] = project_id
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(f"""
                    SELECT {_SELECT_COLS}
                    FROM cowork_scheduled_tasks
                    WHERE user_id = :uid{clause}
                    ORDER BY created_at DESC NULLS LAST
                """),
                params,
            ).fetchall()
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))
    return [_row_to_out(r) for r in rows]


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreate,
    current_user: dict = Depends(get_current_user),
):
    """Create a scheduled task. The prompt is compliance-checked: incidental PII
    is redacted before storage; secrets/keys/tokens hard-block the create."""
    safe_prompt = _redact_for_storage(body.prompt)
    connectors  = _validate_connectors(body.connectors)
    task_id     = str(uuid.uuid4())

    _validate_range(body.starts_at, body.ends_at)

    engine, text = _db()

    # ── Per-user scheduler limit ───────────────────────────────────────────────
    # Count active + paused tasks only; completed tasks are terminal and do not
    # consume a slot. The limit is env-configurable via BUDDY_SCHED_MAX_PER_USER.
    try:
        with engine.connect() as _cnt_conn:
            _cnt = _cnt_conn.execute(
                text("""
                    SELECT COUNT(*) FROM cowork_scheduled_tasks
                    WHERE user_id = :uid AND status != 'completed'
                """),
                {"uid": current_user["sub"]},
            ).scalar()
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))

    if (_cnt or 0) >= BUDDY_SCHED_MAX_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"You can only have {BUDDY_SCHED_MAX_PER_USER} active schedulers. "
                "Delete or complete an existing one before creating a new one."
            ),
        )

    # ── Occurrence cap ────────────────────────────────────────────────────────
    # Always cap max_runs at BUDDY_SCHED_MAX_RUNS regardless of what the client
    # sends. This is the server-side enforcement of the 25-occurrence limit.
    effective_max_runs = min(body.max_runs or BUDDY_SCHED_MAX_RUNS, BUDDY_SCHED_MAX_RUNS)

    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(f"""
                    INSERT INTO cowork_scheduled_tasks
                        (id, user_id, prompt, cron, role, connectors, status,
                         project_id, tz,
                         starts_at, ends_at, max_runs, interval_weeks, interval_months,
                         recurrence, summary,
                         created_at, updated_at)
                    VALUES
                        (:id, :uid, :prompt, :cron, :role, CAST(:connectors AS JSONB),
                         'active', :project_id, :tz,
                         :starts_at, :ends_at, :max_runs, :interval_weeks, :interval_months,
                         CAST(:recurrence AS JSONB), :summary,
                         NOW(), NOW())
                    RETURNING {_SELECT_COLS}
                """),
                {
                    "id":              task_id,
                    "uid":             current_user["sub"],
                    "prompt":          safe_prompt,
                    "cron":            body.cron.strip(),
                    "role":            (body.role or None),
                    "connectors":      json.dumps(connectors),
                    "project_id":      (body.project_id or None),
                    "tz":              (body.tz or "UTC"),
                    "starts_at":       body.starts_at,
                    "ends_at":         body.ends_at,
                    "max_runs":        effective_max_runs,
                    "interval_weeks":  body.interval_weeks,
                    "interval_months": body.interval_months,
                    "recurrence":      (json.dumps(body.recurrence) if body.recurrence else None),
                    "summary":         (body.summary or None),
                },
            ).fetchone()
            conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))

    logger.info(
        f"cowork_tasks: created task={task_id} user={current_user.get('email')} "
        f"cron={body.cron.strip()!r} connectors={len(connectors)} "
        f"has_range={bool(body.ends_at or body.max_runs)}"
    )
    return _row_to_out(row)


@router.put("/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: str,
    body: TaskUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update mutable fields of an owned task. Prompt updates are re-checked for
    compliance (redact PII / hard-block secrets).

    Completed tasks are read-only (409). This mirrors the UI contract — the
    row is greyed out and non-interactive once the scheduler transitions it to
    `completed`, so the server rejects any sneak-in mutation attempt.
    """
    _validate_range(body.starts_at, body.ends_at)

    sets, params = [], {"tid": task_id, "uid": current_user["sub"]}

    if body.prompt is not None:
        sets.append("prompt = :prompt")
        params["prompt"] = _redact_for_storage(body.prompt)
    if body.cron is not None:
        sets.append("cron = :cron")
        params["cron"] = body.cron.strip()
    if body.role is not None:
        sets.append("role = :role")
        params["role"] = body.role or None
    if body.connectors is not None:
        sets.append("connectors = CAST(:connectors AS JSONB)")
        params["connectors"] = json.dumps(_validate_connectors(body.connectors))
    if body.status is not None:
        st = body.status.strip().lower()
        if st not in _VALID_STATUSES:
            raise HTTPException(400, detail=f"status must be one of {sorted(_VALID_STATUSES)}")
        sets.append("status = :status")
        params["status"] = st

    # Recurrence fields — set only what the client sent. None means "leave as-is".
    if body.starts_at is not None:
        sets.append("starts_at = :starts_at"); params["starts_at"] = body.starts_at
    if body.ends_at is not None:
        sets.append("ends_at = :ends_at"); params["ends_at"] = body.ends_at
    if body.max_runs is not None:
        sets.append("max_runs = :max_runs"); params["max_runs"] = body.max_runs
    if body.interval_weeks is not None:
        sets.append("interval_weeks = :iweeks"); params["iweeks"] = body.interval_weeks
    if body.interval_months is not None:
        sets.append("interval_months = :imonths"); params["imonths"] = body.interval_months
    if body.recurrence is not None:
        sets.append("recurrence = CAST(:recurrence AS JSONB)")
        params["recurrence"] = json.dumps(body.recurrence)
    if body.summary is not None:
        sets.append("summary = :summary"); params["summary"] = body.summary

    # When the user edits a schedule, reset runs_count so the "End after N
    # occurrences" limit counts from *this* edit forward (matches Outlook — a
    # recurrence edit is treated as a fresh series). We ONLY reset when a
    # recurrence-shaping field changed, not on a pure role/prompt update.
    if any(k in params for k in ("cron", "starts_at", "ends_at", "max_runs", "iweeks", "imonths", "recurrence")):
        sets.append("runs_count = 0")

    if not sets:
        raise HTTPException(400, detail="No updatable fields supplied.")

    sets.append("updated_at = NOW()")
    # Also clear next_run so the scheduler re-bootstraps it from the new cron/starts_at
    # on the next tick. Cheap and avoids a stale next_run pointing before starts_at.
    sets.append("next_run = NULL")

    engine, text = _db()
    try:
        with engine.connect() as conn:
            _forbid_when_completed(conn, text, task_id, current_user["sub"])
            row = conn.execute(
                text(f"""
                    UPDATE cowork_scheduled_tasks
                    SET {', '.join(sets)}
                    WHERE id = :tid AND user_id = :uid
                    RETURNING {_SELECT_COLS}
                """),
                params,
            ).fetchone()
            conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))

    if not row:
        raise HTTPException(404, detail="Task not found")
    logger.info(f"cowork_tasks: updated task={task_id} user={mask_email(current_user.get('email'))}")
    return _row_to_out(row)


@router.delete("/{task_id}", status_code=status.HTTP_200_OK)
async def delete_task(
    task_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Delete an owned task (and its run history via FK cascade)."""
    engine, text = _db()
    try:
        with engine.connect() as conn:
            res = conn.execute(
                text("""
                    DELETE FROM cowork_scheduled_tasks
                    WHERE id = :tid AND user_id = :uid
                """),
                {"tid": task_id, "uid": current_user["sub"]},
            )
            conn.commit()
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))

    if res.rowcount == 0:
        raise HTTPException(404, detail="Task not found")
    logger.info(f"cowork_tasks: deleted task={task_id} user={mask_email(current_user.get('email'))}")
    return {"deleted": True, "id": task_id}


@router.post("/{task_id}/run-now", status_code=status.HTTP_202_ACCEPTED)
async def run_now(
    task_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Enqueue an owned task to run immediately on the same worker path the
    scheduler uses. Connector/doc WRITES still route through the confirm +
    compliance-gated path inside the agent — run-now grants no write bypass."""
    engine, text = _db()
    try:
        with engine.connect() as conn:
            _forbid_when_completed(conn, text, task_id, current_user["sub"])
            row = _fetch_owned(conn, text, task_id, current_user["sub"])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))

    if not row:
        raise HTTPException(404, detail="Task not found")

    # Pass identifiers only — the worker reloads the task row by id (owner-scoped)
    # so the prompt/connectors are never duplicated through the queue payload.
    payload = {
        "task_id": str(row[0]),
        "user_id": current_user["sub"],
        "trigger": "manual",
    }

    try:
        from core.job_queue import enqueue_job, Q_AGENT, check_queue_pressure
        pressure = check_queue_pressure(Q_AGENT)
        if not pressure.get("allowed"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Cowork worker queue is at capacity — try again shortly.",
            )
        job_id = enqueue_job(
            RUN_TASK_FN,
            payload,
            queue_name=Q_AGENT,
            timeout=900,
            retry_count=0,  # manual runs must not silently double-execute writes
        )
    except HTTPException:
        raise
    except RuntimeError as exc:
        # RQ/Redis unavailable or back-pressure raised inside enqueue_job.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))

    logger.info(f"cowork_tasks: run-now task={task_id} job={job_id} user={mask_email(current_user.get('email'))}")
    return {"enqueued": True, "task_id": task_id, "job_id": job_id}


class CronSuggestRequest(BaseModel):
    """Natural-language schedule description that the LLM converts into cron."""
    description: str = Field(..., min_length=1, max_length=500)


class CronSuggestResponse(BaseModel):
    """The LLM's inferred cron plus a short human-readable summary."""
    cron:    str
    summary: str
    tz:      str = "UTC"


class TaskApproveAction(BaseModel):
    """
    Pre-approve a specific connector write action for a scheduled task.

    When set, the worker will execute this action after each run (e.g. send the
    result to a specific recipient via Outlook) instead of falling back to the
    self-email or outbox path.

    Fields:
      connector       — connector name, e.g. "microsoft_365"
      tool            — tool name, e.g. "outlook_send_mail"
      params          — pre-filled parameters (to, subject, etc.). The body/message
                        field may be left blank ("") — the worker fills it with the
                        agent's output at run time.
      action_allowlist — list of "connector.tool" keys that are permitted. Must
                         include at least the connector+tool above. Defaults to
                         ["{connector}.{tool}"] if omitted.

    Send an empty body {} or set connector="" to CLEAR a previously set approval.
    """
    connector:        str       = Field("", max_length=100)
    tool:             str       = Field("", max_length=255)
    params:           dict      = Field(default_factory=dict)
    action_allowlist: List[str] = Field(default_factory=list)


@router.put("/{task_id}/approve-action", response_model=TaskOut)
async def approve_action(
    task_id: str,
    body: TaskApproveAction,
    current_user: dict = Depends(get_current_user),
):
    """
    Set (or clear) the pre-approved connector write action for an owned task.

    - To SET: supply connector, tool, and params (e.g. to/subject for Outlook).
      The worker will execute this action after each scheduled run, subject to
      HARD-BLOCK compliance on the outbound content.
    - To CLEAR: send connector="" (or an empty body). The task reverts to the
      self-email / outbox fallback.

    The action_allowlist is auto-populated from connector+tool if not supplied.
    Owner-scoped: only the task creator can set/clear the approval.
    """
    connector = (body.connector or "").strip()
    tool      = (body.tool or "").strip()

    if connector and tool:
        # Build the approved_action JSONB object.
        approved_action_val = json.dumps({
            "connector": connector,
            "tool":      tool,
            "params":    body.params or {},
        })
        # Auto-populate allowlist if caller didn't supply one.
        allowlist = body.action_allowlist or [f"{connector}.{tool}"]
        # Ensure the connector.tool key is always in the allowlist.
        key = f"{connector}.{tool}"
        if key not in allowlist:
            allowlist = [key] + allowlist
        allowlist_val = json.dumps(allowlist)
        log_msg = f"set connector={connector} tool={tool}"
    else:
        # Clear the approval.
        approved_action_val = None
        allowlist_val = json.dumps([])
        log_msg = "cleared"

    engine, text = _db()
    try:
        with engine.connect() as conn:
            _forbid_when_completed(conn, text, task_id, current_user["sub"])
            row = conn.execute(
                text(f"""
                    UPDATE cowork_scheduled_tasks
                       SET approved_action   = CAST(:approved_action AS JSONB),
                           action_allowlist  = CAST(:action_allowlist AS JSONB),
                           updated_at        = NOW()
                     WHERE id = :tid AND user_id = :uid
                    RETURNING {_SELECT_COLS}
                """),
                {
                    "approved_action":  approved_action_val,
                    "action_allowlist": allowlist_val,
                    "tid":              task_id,
                    "uid":              current_user["sub"],
                },
            ).fetchone()
            conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))

    if not row:
        raise HTTPException(404, detail="Task not found")

    logger.info(
        f"cowork_tasks: approve-action {log_msg} "
        f"task={task_id} user={current_user.get('email')}"
    )
    return _row_to_out(row)


@router.get("/{task_id}/history")
async def task_history(
    task_id: str,
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    """Return recent run history for an owned task (newest first)."""
    limit = max(1, min(limit, 200))
    engine, text = _db()
    try:
        with engine.connect() as conn:
            # Ownership gate first — never leak another user's run history.
            owner = conn.execute(
                text("""
                    SELECT 1 FROM cowork_scheduled_tasks
                    WHERE id = :tid AND user_id = :uid
                """),
                {"tid": task_id, "uid": current_user["sub"]},
            ).fetchone()
            if not owner:
                raise HTTPException(404, detail="Task not found")

            rows = conn.execute(
                text("""
                    SELECT id, status, output, error, created_at
                    FROM cowork_task_runs
                    WHERE task_id = :tid
                    ORDER BY created_at DESC NULLS LAST
                    LIMIT :lim
                """),
                {"tid": task_id, "lim": limit},
            ).fetchall()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))

    return [
        {
            "id":          str(r[0]),
            "status":      r[1],
            # Run output may echo agent output — redact-and-proceed on read.
            "output":      _redact_for_read(r[2]),
            "error":       _redact_for_read(r[3]),
            "created_at":  r[4].isoformat() if r[4] else None,
        }
        for r in rows
    ]


# ── LLM-driven cron inference ─────────────────────────────────────────────────
# Non-technical users can't hand-write cron expressions. This endpoint lets the
# UI accept a plain-English schedule description (e.g. "every weekday at 9am")
# and hands it to the LLM to produce a validated 5-field cron string.
#
# Design notes:
#   - No DB writes. Pure inference + validation.
#   - The LLM is asked for STRICT JSON; if it strays we regex-salvage the first
#     5-field cron token from the raw text as a fallback.
#   - The final cron is validated with `croniter` — the endpoint refuses to
#     return anything the scheduler couldn't actually parse.
#   - We NEVER log the raw description body (it may carry recipient PII from
#     the "email adarsh@... every day" style prompts).

import re as _re

_CRON_TOKEN_RE = _re.compile(
    r"(?:^|\s)"
    r"("
    r"(?:[\*\d,\-/A-Za-z]+\s+){4}"
    r"[\*\d,\-/A-Za-z]+"
    r")"
    r"(?:\s|$)"
)


def _extract_cron(raw: str) -> Optional[str]:
    """Pull the first plausible 5-field cron expression out of a free-form LLM
    response. Returns None if nothing matched."""
    if not raw:
        return None
    # First try clean JSON.
    try:
        # The model might wrap JSON in a code fence — strip fences before parse.
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = _re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
            cleaned = _re.sub(r"\s*```$", "", cleaned)
        obj = json.loads(cleaned)
        if isinstance(obj, dict) and isinstance(obj.get("cron"), str):
            return obj["cron"].strip()
    except Exception:
        pass
    # Fallback: regex a 5-field token out of the response.
    m = _CRON_TOKEN_RE.search(raw)
    return m.group(1).strip() if m else None


def _extract_summary(raw: str, cron: str) -> str:
    """Extract the LLM's summary line, or synthesise one from the cron."""
    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = _re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
            cleaned = _re.sub(r"\s*```$", "", cleaned)
        obj = json.loads(cleaned)
        if isinstance(obj, dict) and isinstance(obj.get("summary"), str):
            s = obj["summary"].strip()
            if s:
                return s[:200]
    except Exception:
        pass
    return f"Runs on schedule: {cron}"


def _validate_cron_or_422(cron: str) -> str:
    """Validate a 5-field cron with croniter. Raises 422 on failure."""
    parts = (cron or "").split()
    if len(parts) != 5:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Couldn't understand that schedule — try picking Daily, Weekly, or Monthly instead.",
        )
    try:
        from croniter import croniter
        if not croniter.is_valid(cron):
            raise ValueError("invalid cron")
        # Also make sure it can actually produce a next fire time.
        croniter(cron)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Couldn't understand that schedule — try picking Daily, Weekly, or Monthly instead.",
        )
    return cron


@router.post("/suggest-cron", response_model=CronSuggestResponse)
async def suggest_cron(
    body: CronSuggestRequest,
    current_user: dict = Depends(get_current_user),
):
    """Convert a natural-language schedule description into a validated cron
    expression. Used by the Cowork Scheduler UI so non-technical users don't
    have to hand-write cron.

    Returns { cron, summary, tz }. Raises 422 if the LLM output can't be
    validated as a real 5-field cron.
    """
    description = (body.description or "").strip()
    if not description:
        raise HTTPException(400, detail="description is required.")

    # The prompt is deliberately short and format-strict. `simple` tier is
    # plenty for a format-conversion task like this — no need to spend tokens
    # on a coding-tier model.
    prompt = (
        "Convert the following natural-language schedule into a single standard "
        "5-field cron expression (minute hour day-of-month month day-of-week).\n"
        "Rules:\n"
        "- Output MUST be strict JSON with exactly two keys: \"cron\" and \"summary\".\n"
        "- \"cron\" is the 5-field expression, nothing else (no seconds, no year).\n"
        "- \"summary\" is a plain-English description in 12 words or fewer.\n"
        "- Use 24-hour times. Sunday = 0, Monday = 1, ..., Saturday = 6.\n"
        "- If the description is ambiguous, pick the most reasonable interpretation.\n"
        "- Do NOT wrap the JSON in markdown fences or add commentary.\n\n"
        f"Schedule: \"{description}\"\n\n"
        "Respond with JSON only:"
    )

    try:
        from models.model_router import model_router
        raw = model_router.generate(prompt, model_hint="simple") or ""
    except Exception as exc:
        logger.warning(f"cowork_tasks.suggest_cron: LLM call failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Couldn't reach the schedule assistant — please try again.",
        )

    cron = _extract_cron(raw)
    if not cron:
        # Never echo the raw model output back — could be huge/off-topic.
        logger.info(
            f"cowork_tasks.suggest_cron: no cron parsed "
            f"user={current_user.get('email')} desc_len={len(description)}"
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Couldn't understand that schedule — try picking Daily, Weekly, or Monthly instead.",
        )

    cron = _validate_cron_or_422(cron)
    summary = _extract_summary(raw, cron)

    logger.info(
        f"cowork_tasks.suggest_cron: user={current_user.get('email')} "
        f"desc_len={len(description)} cron={cron!r}"
    )
    return CronSuggestResponse(cron=cron, summary=summary, tz="UTC")
