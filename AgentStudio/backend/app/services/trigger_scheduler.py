# SPDX-License-Identifier: MIT
"""
Trigger scheduler — APScheduler-driven Routines for workflows and agents.

All schedules are interpreted in **IST (Asia/Kolkata)** regardless of the
host machine's timezone. Every fired job runs the configured workflow or
agent and writes the full input + output + status into the
``trigger_executions`` table. The UI polls that table for notifications
("your scheduled task has been executed").

Persistence model
  - ``triggers``            : the schedule definition (cron / date)
  - ``trigger_executions``  : one row per fire — input, output, status, time

Architecture
  ``init_scheduler()`` is called from FastAPI's lifespan hook *after*
  ``workflow_repo.init_db()``. It loads every enabled trigger from postgres
  and registers it with APScheduler. ``create_trigger``/``update_trigger``/
  ``delete_trigger`` keep APScheduler in sync with the DB on every CRUD.

Job execution
  - Workflow: load ``graph_data`` from postgres, build a ``ChainDefinition``,
    and stream the engine's SSE events into a string buffer. We keep the
    last ``agent_complete`` payload as the final output.
  - Agent:    build a one-node workflow on the fly using the same engine
    so that all rendering, RAG, and tool dispatch behave identically to a
    manual run.

Only ``init_scheduler``, ``shutdown_scheduler``, ``register_trigger``,
``deregister_trigger``, ``reschedule_trigger`` are public — everything else
is implementation detail.
"""
from __future__ import annotations

import asyncio
import json

import os
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .. import workflow_repo
from ..core.config import (
    openai_compatible_api_key as _openai_compatible_api_key,
    openai_compatible_base_url as _openai_compatible_base_url,
    verifier_model as _verifier_model,
    factory_agent_model as _factory_agent_model,
)
from ..core.kb_retriever import KB_MODE_NONE
from ..core.governance import (
    audit_event,
    check_budget_allowed,
    budget_degraded_allowed,
    budget_degraded_fallback_model,
    budget_denied_detail,
    RunUsageTracker,
    _is_local_model,
)
from ..engine import get_engine, ChainDefinition, ChainEdge, ExecutionContext

from core.logger import (
    logger,
    set_request_id,
    set_chat_context,
    set_span_id,
    set_client_source,
    clear_chat_context,
)
IST = ZoneInfo("Asia/Kolkata")

_scheduler: Optional[AsyncIOScheduler] = None
# Map of trigger_id -> APScheduler job_id, so we can update/remove later.
_job_index: Dict[str, str] = {}
# Separate index for Loop Engineering's TriageSkill cron jobs (P5). Kept
# apart from ``_job_index`` so a CRUD against the regular ``triggers``
# table can't accidentally evict a triage job that shares a UUID. Key is
# loop_id, value is APScheduler job_id.
_triage_job_index: Dict[str, str] = {}


_DOW_MAP = {
    "monday":    "mon",
    "tuesday":   "tue",
    "wednesday": "wed",
    "thursday":  "thu",
    "friday":    "fri",
    "saturday":  "sat",
    "sunday":    "sun",
}


async def _resolve_owner_department(owner: str) -> str:
    """Return the trigger owner's department for KB ACL, or ``""``.

    Reuses ``auth.dependencies.enrich_user_context`` for its Redis-cached
    profile lookup (5-min TTL keyed on ``sub``). When ``owner`` is an email
    rather than a UUID, falls back to a single ORed DB query.
    """
    if not owner:
        return ""

    try:
        from auth.dependencies import enrich_user_context
        enriched = enrich_user_context({"sub": owner})
        dept = enriched.get("department") or ""
        if dept:
            return dept
    except Exception:
        pass

    try:
        from db.database import SessionLocal
        from db.models import User
        from sqlalchemy import or_
    except Exception:
        return ""

    def _lookup() -> str:
        db = SessionLocal()
        try:
            u = db.query(User).filter(or_(User.id == owner, User.email == owner)).first()
            return (u.department or "") if u else ""
        finally:
            db.close()

    try:
        return await asyncio.to_thread(_lookup)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Schedule -> APScheduler trigger conversion
# ---------------------------------------------------------------------------

def _build_apscheduler_trigger(schedule: Dict[str, Any]):
    """Translate the JSON ``schedule`` blob into an APScheduler trigger.

    Returns ``None`` if the schedule is malformed — the caller logs and
    skips registration in that case so a single bad row can't crash startup.
    """
    sched_type = (schedule.get("type") or "").lower()

    if sched_type == "once":
        run_at = schedule.get("run_at")
        if not run_at:
            return None
        try:
            # Accept both naive and tz-aware ISO strings; treat naive as IST.
            dt = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            return DateTrigger(run_date=dt, timezone=IST)
        except Exception as exc:
            logger.warning(f"[AGENT] Invalid 'once' run_at={run_at!r}: {exc}")
            return None

    if sched_type == "hourly":
        minute = int(schedule.get("at_minute") or 0)
        return CronTrigger(minute=minute, timezone=IST)

    if sched_type in ("daily", "weekdays", "weekly"):
        at_time = schedule.get("at_time") or "00:00"
        try:
            hh, mm = at_time.split(":")
            hour, minute = int(hh), int(mm)
        except Exception:
            logger.warning(f'[AGENT] Invalid at_time={at_time!r} — defaulting to 00:00')
            hour, minute = 0, 0

        if sched_type == "daily":
            return CronTrigger(hour=hour, minute=minute, timezone=IST)
        if sched_type == "weekdays":
            return CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=IST)
        # weekly
        dow = (schedule.get("day_of_week") or "monday").lower()
        return CronTrigger(
            day_of_week=_DOW_MAP.get(dow, "mon"),
            hour=hour, minute=minute, timezone=IST,
        )

    if sched_type == "custom":
        cron = schedule.get("cron") or ""
        try:
            return CronTrigger.from_crontab(cron, timezone=IST)
        except Exception as exc:
            logger.warning(f'[AGENT] Invalid custom cron={cron!r}: {exc}')
            return None

    return None


def _compute_next_run_at(schedule: Dict[str, Any]) -> Optional[datetime]:
    """Compute the next fire time for a schedule blob, IST.

    Reuses ``_build_apscheduler_trigger`` so cron/date semantics stay identical
    to the previous in-process APScheduler path. Returns None when the schedule
    is malformed, one-off in the past, or event-driven (webhook/event).
    """
    sched_type = (schedule.get("type") or "").lower()
    if sched_type in ("webhook", "event"):
        return None
    ap_trigger = _build_apscheduler_trigger(schedule)
    if ap_trigger is None:
        return None
    try:
        now = datetime.now(IST)
        next_run = ap_trigger.get_next_fire_time(None, now)
        # Guard for ``once``: APScheduler's DateTrigger returns the configured
        # run_date regardless of whether it's already in the past, so a
        # once-trigger that has just been dispatched would be re-selected on
        # every subsequent 60 s tick. Force None once the moment has passed.
        if sched_type == "once" and next_run is not None and next_run <= now:
            return None
        return next_run
    except Exception:
        logger.exception('[AGENT] _compute_next_run_at failed')
        return None


# ---------------------------------------------------------------------------
# FR-T0-1 (C4) + FR-T0-2 (PI2) — trigger input gate
# ---------------------------------------------------------------------------

async def _gate_trigger_input(input_text: str, trigger_id: str) -> tuple:
    """Compliance + injection gate for a trigger payload.

    Returns (text_to_use, error|None). When error is not None the caller MUST
    reject the fire. Compliance runs first (block on PAN/CVV/…), then
    prompt-injection (block policy by default for triggers). Redacted text is
    returned for the clean-but-redacted case. Fails OPEN on gate error.
    """
    if not input_text or not input_text.strip():
        return input_text, None
    # ── Compliance (C4) ──────────────────────────────────────────────
    try:
        from agents.compliance_engine import compliance_engine  # type: ignore
        from fastapi.concurrency import run_in_threadpool

        # C5: use run_in_threadpool (consistent with the rest of the codebase)
        # rather than asyncio.to_thread to avoid potential event-loop mismatch
        # when running inside APScheduler's AsyncIOScheduler.
        check = await run_in_threadpool(compliance_engine.validate_input, input_text)
        if check.get("blocked"):
            types = sorted({
                f.get("type") for f in (check.get("findings") or []) if f.get("type")
            })
            return input_text, f"compliance: restricted data ({', '.join(types)})"
        input_text = check.get("redacted_text") or input_text
    except Exception as exc:  # fail open
        logger.warning(f'[COMPLIANCE] trigger {trigger_id} validate_input failed: {exc}')
    # ── Injection (PI2) ──────────────────────────────────────────────
    try:
        from core.prompt_injection import scan  # type: ignore

        from fastapi.concurrency import run_in_threadpool
        result = await run_in_threadpool(scan, input_text, "trigger")
        if result.get("is_suspicious"):
            policy = os.getenv("ABS_INJECTION_POLICY_TRIGGER", "block")
            cats = result.get("categories") or []
            logger.warning(
                f'[INJECTION] trigger {trigger_id} suspicious '
                f'score={result.get("score")} categories={cats} policy={policy}'
            )
            if policy == "block":
                return input_text, f"prompt-injection detected ({', '.join(cats)})"
            if policy == "sanitize":
                input_text = result.get("sanitized_text") or input_text
    except Exception as exc:  # fail open
        logger.warning(f'[INJECTION] trigger {trigger_id} scan failed: {exc}')
    return input_text, None


def _merge_generated_files(existing: list, incoming: list) -> list:
    """Union two generated-file lists, de-duplicating by download_url.

    The engine emits ``generated_files`` on every ``agent_complete`` (one per
    node) and again on ``complete``. Across a multi-node workflow those payloads
    overlap, so we merge on the stable ``download_url`` (falling back to
    ``disk_name``/``path``) to keep exactly one entry per artifact for the Inbox.
    """
    merged = list(existing)
    seen = {
        (gf.get("download_url") or gf.get("disk_name") or gf.get("path"))
        for gf in merged if isinstance(gf, dict)
    }
    for gf in incoming:
        if not isinstance(gf, dict):
            continue
        key = gf.get("download_url") or gf.get("disk_name") or gf.get("path")
        if key in seen:
            continue
        seen.add(key)
        merged.append(gf)
    return merged


# ---------------------------------------------------------------------------
# Job — runs in the asyncio loop when a trigger fires
# ---------------------------------------------------------------------------

async def _fire_trigger(trigger_id: str) -> None:
    """Top-level job entry point. Loads the trigger fresh from postgres so
    edits to the input_text or target take effect on the next fire.
    """
    try:
        trigger = await workflow_repo.get_trigger_by_id(trigger_id)
    except Exception as exc:
        logger.exception(f'[AGENT] trigger {trigger_id}: failed to load: {exc}')
        return

    if not trigger:
        logger.warning(f'[AGENT] trigger {trigger_id}: missing — removing from scheduler')
        deregister_trigger(trigger_id)
        return
    if not trigger.get("enabled"):
        return

    target_kind = trigger["target_kind"]
    target_id   = trigger["target_id"]
    owner       = trigger["owner_user_id"]
    input_text  = trigger.get("input_text") or ""

    # Stamp this background run's agent.log lines (no HTTP request here, so we
    # bind the core.logger thread-local directly). request_id = trigger_id keeps
    # every line for one fire correlatable.
    set_request_id(trigger_id)
    set_chat_context(user_id=str(owner or "-"), chat_id=str(target_id or "-"))
    set_span_id("trigger")
    set_client_source("abstudio")
    logger.info(
        f"[AGENT] ⏰ Trigger fired trigger_id={trigger_id} target_kind={target_kind} "
        f"target_id={target_id} owner={owner} input_preview={input_text[:120]!r}"
    )

    # Resolve a display name for nicer notifications.
    target_name = ""
    try:
        if target_kind == "workflow":
            wf = await workflow_repo.get_workflow(target_id, owner)
            if wf:
                target_name = wf.get("name") or ""
        elif target_kind == "agent":
            ag = await workflow_repo.get_agent(target_id, owner) \
                 or await workflow_repo.get_agent_by_id(target_id)
            if ag:
                target_name = ag.get("name") or ""
    except Exception:
        pass

    # Resolve owner department for KB ACL and audit context
    owner_department = await _resolve_owner_department(owner)

    execution_id = await workflow_repo.insert_trigger_execution(
        trigger_id=trigger_id,
        target_kind=target_kind,
        target_id=target_id,
        target_name=target_name,
        input_text=input_text,
        owner_user_id=owner,
    )

    # ── Audit trigger fire start ────────────────────────────────────────
    audit_event(
        user_id=owner,
        endpoint="abstudio.trigger.fire",
        action="start",
        workflow_id=target_id,
        workflow_name=target_name,
        department=owner_department,
        extra={
            "trigger_id": trigger_id,
            "target_kind": target_kind,
            "execution_id": execution_id,
        },
    )

    # ── FR-T0-1 (C4) + FR-T0-2 (PI2): trigger payload gate ──────────────
    # Trigger inputs (schedule text, and later webhook payloads) are an
    # untrusted ingestion point. Enforce compliance first (block on
    # PAN/CVV/…), then prompt-injection (block by default for triggers).
    input_text, _gate_error = await _gate_trigger_input(input_text, trigger_id)
    if _gate_error is not None:
        logger.warning(f'[AGENT] trigger {trigger_id}: input gate rejected — {_gate_error}')
        audit_event(
            user_id=owner,
            endpoint="abstudio.trigger.fire",
            action="rejected",
            workflow_id=target_id,
            workflow_name=target_name,
            department=owner_department,
            error=_gate_error,
            extra={"trigger_id": trigger_id, "execution_id": execution_id},
        )
        try:
            await workflow_repo.finalize_trigger_execution(
                execution_id=execution_id,
                status="error",
                output=None,
                error=_gate_error,
            )
            await workflow_repo.update_trigger_run_metadata(
                trigger_id=trigger_id,
                last_run_at=datetime.now(IST),
                last_status="error",
                next_run_at=_compute_next_run(trigger),
            )
        except Exception:
            logger.exception(f'[AGENT] trigger {trigger_id}: failed to persist gate rejection')
        return

    # ── Budget preflight ────────────────────────────────────────────────
    # Budget store down → fail closed on paid models, but keep the schedule
    # alive by running on the no-cost local fallback model instead of skipping
    # the tick entirely. The downgrade is recorded on the execution row + audit
    # trail (``model_downgraded``) so the owner can see that this run used a
    # different model than configured.
    budget_result = check_budget_allowed(owner)
    budget_model_override = ""
    _fallback = budget_degraded_fallback_model(budget_result)
    if _fallback:
        budget_model_override = _fallback
        logger.warning(
            f'[AGENT] trigger {trigger_id}: budget store unavailable for owner={owner} — '
            f'downgrading this run to the no-cost local model {_fallback!r}'
        )
        audit_event(
            user_id=owner,
            endpoint="abstudio.trigger.fire",
            action="budget_degraded_downgrade",
            workflow_id=target_id,
            workflow_name=target_name,
            department=owner_department,
            error=f"budget store unavailable — downgraded to {_fallback}",
            extra={
                "trigger_id": trigger_id,
                "execution_id": execution_id,
                "fallback_model": _fallback,
            },
        )
        budget_result = budget_degraded_allowed(_fallback)
    if not budget_result.get("allowed"):
        # Same structured verdict the HTTP endpoints return as a 429 detail, so
        # a scheduled run and an interactive run report an outage identically.
        _detail = budget_denied_detail(budget_result)
        budget_deny_reason = _detail["message"]
        _degraded = bool(_detail.get("degraded"))
        _label = "budget store unavailable" if _degraded else "budget denied"
        logger.warning(
            f'[AGENT] trigger {trigger_id}: {_label} for owner={owner} — {budget_deny_reason}'
        )
        audit_event(
            user_id=owner,
            endpoint="abstudio.trigger.fire",
            action="budget_denied",
            workflow_id=target_id,
            workflow_name=target_name,
            department=owner_department,
            error=budget_deny_reason,
            extra={
                "trigger_id": trigger_id,
                "execution_id": execution_id,
                "code": _detail["code"],
                "degraded": _degraded,
            },
        )
        try:
            await workflow_repo.finalize_trigger_execution(
                execution_id=execution_id,
                status="error",
                output=None,
                error=(
                    f"Skipped — {budget_deny_reason} The trigger will retry on its "
                    f"next scheduled run."
                    if _degraded
                    else f"Budget denied: {budget_deny_reason}"
                ),
            )
        except Exception:
            logger.exception(f'[AGENT] trigger {trigger_id}: failed to persist budget-denied execution')
        try:
            await workflow_repo.update_trigger_run_metadata(
                trigger_id=trigger_id,
                last_run_at=datetime.now(IST),
                last_status="error",
                next_run_at=_compute_next_run(trigger),
            )
        except Exception:
            logger.exception(f'[AGENT] trigger {trigger_id}: failed to update run metadata after budget deny')
        return

    tracker = RunUsageTracker(
        user_id=owner,
        endpoint="abstudio.trigger.fire",
        workflow_id=target_id,
        workflow_name=target_name,
        department=owner_department,
    )

    started_ist = datetime.now(IST)
    output: Optional[str] = None
    error: Optional[str] = None
    generated_files: list = []
    status = "success"
    try:
        if target_kind == "workflow":
            output, generated_files = await _execute_workflow(
                target_id, owner, input_text, trigger_id,
                node_id=trigger.get("node_id"),
                tracker=tracker,
                owner_department=owner_department,
                model_override=budget_model_override,
            )
        elif target_kind == "agent":
            output, generated_files = await _execute_agent(
                target_id, owner, input_text, trigger_id,
                tracker=tracker,
                owner_department=owner_department,
                model_override=budget_model_override,
            )
        else:
            raise ValueError(f"Unknown target_kind: {target_kind}")
    except Exception as exc:
        status = "error"
        error = str(exc)
        logger.exception(f'[AGENT] trigger {trigger_id} fire failed')

    tracker.finalize(status, error=error or "")

    try:
        await workflow_repo.finalize_trigger_execution(
            execution_id=execution_id,
            status=status,
            output=output,
            error=error,
            generated_files=generated_files,
        )
    except Exception:
        logger.exception(f'[AGENT] trigger {trigger_id}: failed to persist execution result')

    try:
        await workflow_repo.update_trigger_run_metadata(
            trigger_id=trigger_id,
            last_run_at=started_ist,
            last_status=status,
            next_run_at=_compute_next_run(trigger),
        )
    except Exception:
        logger.exception(f'[AGENT] trigger {trigger_id}: failed to update run metadata')

    logger.info(
        f"[AGENT] ⏰ Trigger finished trigger_id={trigger_id} status={status} "
        f"output_preview={(output or '')[:160]!r}"
        + (f" error={error!r}" if error else "")
    )
    # Reset the per-run logging context so the next job on this thread starts clean.
    clear_chat_context()


def _compute_next_run(trigger: Dict[str, Any]) -> Optional[datetime]:
    """After a fire, compute the next scheduled run time from the trigger row.

    The in-process APScheduler is retired — fires now come from the external
    scheduler worker via RQ. We derive the next run directly from the DB row's
    schedule blob using the same schedule-parsing helper the dispatcher uses,
    so both stay in lockstep. Pure function, no I/O.
    """
    if not trigger:
        return None
    return _compute_next_run_at(trigger.get("schedule") or {})


# ---------------------------------------------------------------------------
# RQ entry point — called by the scheduler worker via core.job_queue
# ---------------------------------------------------------------------------

def fire_from_queue(payload: Dict[str, Any]) -> None:
    """Synchronous RQ job function. Delegates to the async ``_fire_trigger``.

    Called by workers.workflow_scheduler_worker.dispatch_due_triggers after
    it has picked a due trigger from Postgres and enqueued it to Redis. Runs
    inside an RQ worker process (never inside gunicorn). Advances next_run_at
    after the fire completes so the dispatcher won't re-enqueue on the next
    tick even if last_run_at is written mid-fire.

    Payload shape: ``{"trigger_id": "<id>"}``.
    """
    trigger_id = (payload or {}).get("trigger_id")
    if not trigger_id:
        logger.warning('[AGENT] fire_from_queue: missing trigger_id in payload')
        return

    # RQ worker processes don't run the FastAPI lifespan, so ABStudio's
    # workflow_repo._pool may be None here. Bind lazily to the platform's
    # shared pool — same object workflow_repo.init_db would assign.
    #
    # IMPORTANT: we must write _pool on ``app.core.workflow_repo`` — the
    # real module — not on the ``app.workflow_repo`` back-compat shim that
    # this file imports via ``from .. import workflow_repo``. The shim does
    # ``from app.core.workflow_repo import *``, which does NOT re-export
    # names starting with ``_``. So writing to the shim's ``_pool`` would
    # go into a dead namespace and every subsequent DB call (which reads
    # ``_pool`` from ``app.core.workflow_repo`` via ``_get_pool``) would
    # still raise "DB pool not ready".
    from ..core import workflow_repo as _wr_core
    if _wr_core.get_pool() is None:
        try:
            from ..core.db_pool import SHARED_POOL
            _wr_core._pool = SHARED_POOL
            logger.info(
                f'[AGENT] fire_from_queue: bound app.core.workflow_repo._pool '
                f'to platform SHARED_POOL for trigger {trigger_id}'
            )
        except Exception:
            logger.exception(
                f'[AGENT] fire_from_queue: could not bind workflow_repo pool '
                f'for trigger {trigger_id} — aborting'
            )
            return

    async def _run():
        await _fire_trigger(trigger_id)
        # _fire_trigger already updates next_run_at via update_trigger_run_metadata
        # on both success and error paths, so no extra work needed here.

    try:
        asyncio.run(_run())
    except RuntimeError:
        # If we're somehow already inside a running loop (dev-mode oddity),
        # fall back to a fresh loop rather than crashing the worker.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()


def _normalise_workflow_nodes(raw_nodes: list) -> list:
    """Convert React-Flow stored nodes into the engine's expected shape."""
    nodes = []
    for n in raw_nodes:
        if n.get("type") in ("start", "end"):
            nodes.append({"id": n["id"], "type": n["type"]})
            continue
        if n.get("type") == "condition":
            nodes.append({
                "id": n["id"], "type": "condition",
                "cases": (n.get("data") or {}).get("cases", []),
            })
            continue
        if n.get("type") == "mcp":
            data = n.get("data") or {}
            nodes.append({
                "id": n["id"], "type": "mcp",
                "server_type": data.get("serverType"),
                "config": data.get("serverConfig"),
            })
            continue
        data = n.get("data") or {}
        nodes.append({
            "id": n["id"], "type": n.get("type"),
            "name": data.get("name"),
            "instructions": data.get("instructions"),
            "hitlMode": data.get("hitlMode", "off"),
            "llm_config": {
                "provider":    "custom",
                # Proxy-first endpoint resolution (LLM_PROXY_URL →
                # OPENAI_COMPATIBLE_BASE_URL → LOCAL_LLM_BASE_URL → localhost)
                # via the shared helpers, so triggered runs resolve the LLM
                # endpoint identically to interactive runs.
                "api_key":     _openai_compatible_api_key(),
                "model_name":  data.get("modelName") or _factory_agent_model(),
                "temperature": data.get("temperature", 0.7),
                "max_tokens":  data.get("maxTokens", 2048),
                "top_p":       data.get("topP", 1.0),
                "base_url":    data.get("baseUrl") or _openai_compatible_base_url(),
            },
            "tools":  data.get("tools") or [],
            "skills": data.get("skills") or [],
            "knowledge": data.get("knowledge") or {"mode": "none"},
        })
    return nodes


def _slice_chain_from_node(
    nodes: list, edges: list, start_node_id: str,
) -> tuple[list, list]:
    """Return (nodes, edges) for the sub-chain that begins at ``start_node_id``.

    The graph keeps a real Start node so the engine has a unique entry point
    we re-target with a synthetic edge: ``start → start_node_id``. Every
    node reachable from ``start_node_id`` downstream is preserved with its
    outgoing edges; nodes that are *only* reachable upstream of
    ``start_node_id`` are dropped because they don't run.
    """
    by_id = {n["id"]: n for n in nodes}
    if start_node_id not in by_id:
        # Node was deleted from the workflow since the trigger was created.
        # Fall through to a full run rather than fail.
        return nodes, edges

    # BFS forward from the target node to find every reachable node.
    reachable: set = set()
    queue = [start_node_id]
    while queue:
        cur = queue.pop(0)
        if cur in reachable:
            continue
        reachable.add(cur)
        for e in edges:
            if e.get("source") == cur and e.get("target") not in reachable:
                queue.append(e["target"])

    # Find the original start node — we keep it as the engine entry point.
    start_node = next((n for n in nodes if n.get("type") == "start"), None)
    if start_node is None:
        return nodes, edges

    kept_nodes = [start_node] + [by_id[nid] for nid in reachable if nid != start_node["id"]]
    kept_node_ids = {n["id"] for n in kept_nodes}

    # Keep only edges entirely inside the kept set, then drop every edge that
    # *originated* at the start node (we'll replace it with a single direct
    # edge into start_node_id below).
    kept_edges = [
        e for e in edges
        if e.get("source") in kept_node_ids
        and e.get("target") in kept_node_ids
        and e.get("source") != start_node["id"]
    ]
    kept_edges.insert(0, {
        "source": start_node["id"],
        "target": start_node_id,
        "sourceHandle": None,
    })
    return kept_nodes, kept_edges


async def _execute_workflow(
    workflow_id: str,
    owner: str,
    user_input: str,
    trigger_id: str,
    node_id: Optional[str] = None,
    tracker: Optional["RunUsageTracker"] = None,
    owner_department: str = "",
    model_override: str = "",
) -> tuple[str, list]:
    """Run a saved workflow for a trigger.

    ``model_override`` re-points every model-bearing node at a single model.
    It is set only when the budget store is unavailable and the run has been
    downgraded to a no-cost local model (see the budget preflight in
    ``_fire_trigger``), so a store outage degrades the schedule instead of
    skipping it.
    """
    wf = await workflow_repo.get_workflow(workflow_id, owner)
    if not wf:
        raise ValueError(f"Workflow {workflow_id} not found")

    graph = wf.get("graphData") or {}
    raw_nodes = graph.get("nodes") or []
    raw_edges = graph.get("edges") or []

    nodes = _normalise_workflow_nodes(raw_nodes)
    if model_override:
        # An evaluation_gate / loop node also runs an LLM *judge* on the
        # env-global verifier model, which a per-run node rewrite cannot
        # change. Downgrading only the node models would leave that judge
        # spending on a cloud model during the very outage we are guarding
        # against, so refuse the run instead of half-downgrading it.
        _judge = _verifier_model()
        if not _is_local_model(_judge):
            _judge_types = {"evaluation_gate", "loop"}
            _offending = [
                n.get("id") for n in nodes
                if (n.get("type") or "").strip().lower() in _judge_types
            ]
            if _offending:
                raise RuntimeError(
                    f"Budget service is unavailable, so this run was downgraded to the "
                    f"no-cost local model '{model_override}'. It cannot run because "
                    f"node(s) {_offending} use an LLM judge on the cloud model "
                    f"'{_judge}', which cannot be overridden per run. The trigger will "
                    f"retry on its next scheduled run."
                )
        _n = 0
        for _node in nodes:
            _cfg = _node.get("llm_config")
            if isinstance(_cfg, dict):
                _cfg["model_name"] = model_override
                _n += 1
        logger.info(
            f"[AGENT] trigger {trigger_id}: budget-degraded downgrade re-pointed "
            f"{_n} node(s) at local model {model_override!r}"
        )
    edges_raw = list(raw_edges)

    # If the trigger is bound to a specific node, splice the chain so it
    # starts there. The original Start node stays as the engine entry but
    # routes straight to the target — everything downstream runs normally.
    if node_id:
        nodes, edges_raw = _slice_chain_from_node(nodes, edges_raw, node_id)

    edges = [
        ChainEdge(
            source=e.get("source"),
            target=e.get("target"),
            source_handle=e.get("sourceHandle"),
        )
        for e in edges_raw
    ]
    chain = ChainDefinition(nodes=nodes, edges=edges, knowledge=wf.get("knowledge"))

    # Department resolved by _fire_trigger (REQ-P2-3). If the upstream
    # lookup failed (Redis/DB down) it passes "" — attempt a single
    # best-effort re-resolve here so the KB ACL and audit context are
    # populated when possible. Still fails-open (empty string) on error.
    dept = owner_department
    if not dept:
        try:
            dept = await _resolve_owner_department(owner)
        except Exception:
            pass
        if not dept:
            logger.warning(
                f"[AGENT] _execute_workflow: owner_department is empty for owner={owner!r} "
                f"workflow={workflow_id!r} — KB ACL and audit context will be unscoped"
            )
    context = ExecutionContext(
        thread_id=f"trigger-{trigger_id}-{int(datetime.now().timestamp())}",
        workflow_id=workflow_id,
        workflow_name=wf.get("name") or "",
        user_id=owner,
        department=dept,
    )

    final_output = ""
    generated_files: list = []
    async for raw in get_engine().execute(chain, user_input, context):
        if not raw.startswith("data:"):
            continue
        try:
            payload = json.loads(raw[5:].strip())
        except json.JSONDecodeError:
            continue
        if tracker is not None:
            tracker.observe_event(payload)
        evt = (payload.get("event") or payload.get("type") or "")
        data = payload.get("data") or payload
        # Collect any generated-file download references the engine emits
        # alongside the text output; the interactive path streams these to the
        # UI, but a trigger has no live stream so we persist them on the
        # execution row for the Inbox to render (see finalize_trigger_execution).
        _gf = data.get("generated_files")
        if isinstance(_gf, list) and _gf:
            generated_files = _merge_generated_files(generated_files, _gf)
        if evt == "agent_complete":
            final_output = data.get("output", final_output) or final_output
        elif evt == "complete" and data.get("output"):
            final_output = data["output"]
        elif evt == "error":
            raise RuntimeError(data.get("message") or "Workflow execution error")
    return final_output or "(no output produced)", generated_files


async def _execute_agent(
    agent_id: str,
    owner: str,
    user_input: str,
    trigger_id: str,
    tracker: Optional["RunUsageTracker"] = None,
    owner_department: str = "",
    model_override: str = "",
) -> tuple[str, list]:
    """Run a saved agent by wrapping it in a one-node Start→Agent→End workflow.

    This routes through the same engine, history, and RAG plumbing that an
    interactive /run does. The agent's stored ``model_name`` is used; when
    blank it falls back to ``factory_agent_model()``.

    ``model_override`` wins over the stored model. It is set only when the
    budget store is unavailable and the run has been downgraded to a no-cost
    local model (see the budget preflight in ``_fire_trigger``), so a store
    outage degrades the schedule instead of skipping it.
    """
    agent = await workflow_repo.get_agent(agent_id, owner) or \
            await workflow_repo.get_agent_by_id(agent_id)
    if not agent:
        raise ValueError(f"Agent {agent_id} not found")

    model = (agent.get("model_name") or "").strip() or _factory_agent_model()
    if model_override:
        logger.info(
            f"[AGENT] trigger {trigger_id}: budget-degraded downgrade re-pointed agent "
            f"{agent_id} from {model!r} to local model {model_override!r}"
        )
        model = model_override

    nodes = [
        {"id": "start-1", "type": "start"},
        {
            "id": "agent-1",
            "type": "agent",
            "name": agent.get("name") or "Agent",
            "instructions": agent.get("instructions") or "",
            "hitlMode": "off",
            "llm_config": {
                "provider":    "custom",
                # Proxy-first endpoint resolution (LLM_PROXY_URL →
                # OPENAI_COMPATIBLE_BASE_URL → LOCAL_LLM_BASE_URL → localhost)
                # via the shared helpers, matching the interactive engine path.
                "api_key":     _openai_compatible_api_key(),
                "model_name":  model,
                "temperature": agent.get("temperature", 0.7),
                "max_tokens":  agent.get("max_tokens", 2048),
                "top_p":       agent.get("top_p", 1.0),
                "base_url":    _openai_compatible_base_url(),
            },
            "tools":  agent.get("tools") or [],
            "skills": agent.get("skills") or [],
            "knowledge": agent.get("knowledge") or {"mode": KB_MODE_NONE},
        },
        {"id": "end-1", "type": "end"},
    ]
    edges = [
        ChainEdge(source="start-1", target="agent-1"),
        ChainEdge(source="agent-1", target="end-1"),
    ]
    chain = ChainDefinition(nodes=nodes, edges=edges)
    # Department resolved by _fire_trigger (REQ-P2-3). If the upstream
    # lookup failed (Redis/DB down) it passes "" — attempt a single
    # best-effort re-resolve here so the KB ACL and audit context are
    # populated when possible. Still fails-open (empty string) on error.
    dept = owner_department
    if not dept:
        try:
            dept = await _resolve_owner_department(owner)
        except Exception:
            pass
        if not dept:
            logger.warning(
                f"[AGENT] _execute_agent: owner_department is empty for owner={owner!r} "
                f"agent={agent_id!r} — KB ACL and audit context will be unscoped"
            )
    context = ExecutionContext(
        thread_id=f"trigger-{trigger_id}-{int(datetime.now().timestamp())}",
        workflow_id=f"trigger-agent-{agent_id}",
        workflow_name=agent.get("name") or "",
        user_id=owner,
        department=dept,
    )

    final_output = ""
    generated_files: list = []
    async for raw in get_engine().execute(chain, user_input, context):
        if not raw.startswith("data:"):
            continue
        try:
            payload = json.loads(raw[5:].strip())
        except json.JSONDecodeError:
            continue
        if tracker is not None:
            tracker.observe_event(payload)
        evt = (payload.get("event") or payload.get("type") or "")
        data = payload.get("data") or payload
        # See _execute_workflow — collect download references for the Inbox.
        _gf = data.get("generated_files")
        if isinstance(_gf, list) and _gf:
            generated_files = _merge_generated_files(generated_files, _gf)
        if evt == "agent_complete":
            final_output = data.get("output", final_output) or final_output
        elif evt == "complete" and data.get("output"):
            final_output = data["output"]
        elif evt == "error":
            raise RuntimeError(data.get("message") or "Agent execution error")
    return final_output or "(no output produced)", generated_files


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def init_scheduler() -> None:
    """Startup hook — kept for backwards-compatible callers in main.py.

    User-defined triggers (``triggers`` table) are no longer scheduled
    in-process. They are dispatched by the dedicated scheduler worker
    (``workers.workflow_scheduler_worker.dispatch_due_triggers``) which polls
    Postgres every 60 s and enqueues fires onto Redis, so exactly one worker
    picks up each fire regardless of how many gunicorn workers are running.
    This fixes the multi-worker duplicate-fire bug where every gunicorn
    worker held its own APScheduler and fired the same trigger N times.

    APScheduler is still started here for P5 TriageSkill jobs which are
    independent of the user-defined trigger path and untouched by this
    change.
    """
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler(timezone=IST)
    _scheduler.start()
    logger.info('[AGENT] APScheduler started for triage jobs only '
                '(user triggers now dispatched by workers.workflow_scheduler_worker)')

    # ── P5: bootstrap one TriageSkill cron job per active loop ──
    await _bootstrap_triage_jobs()


async def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception:
        pass
    _scheduler = None
    _job_index.clear()
    _triage_job_index.clear()


# ---------------------------------------------------------------------------
# P5 — TriageSkill cron jobs
# ---------------------------------------------------------------------------


async def _fire_triage(loop_id: str) -> None:
    """APScheduler callback — run TriageSkill once for ``loop_id``.

    Imports lazily so the module load order stays:
    ``trigger_scheduler`` → ``loop.repo`` → ``loop.runner``, not
    the other way around (avoids any chance of import cycles when
    ``loop`` modules pull in engine helpers).

    Never raises — TriageSkill.run() is documented to always return a
    :class:`TriageRunResult`, but we still wrap in try/except so a
    rogue change in the skill can't kill the scheduler thread.
    """
    try:
        from ..loop import repo as loops_repo
        from ..loop.runner import TriageSkill

        loop = await loops_repo.get_loop(loop_id)
        if loop is None:
            logger.warning(f'[AGENT] TriageSkill: loop {loop_id} no longer exists; skipping fire')
            return
        if not loop.enabled:
            logger.info(f'[AGENT] TriageSkill: loop {loop_id} disabled; skipping fire')
            return

        def _sink(frame: str) -> None:
            # The cron path has no live SSE consumer — we log a one-line
            # summary per event so the audit trail still records what
            # happened. The manual /run-now endpoint pushes the same
            # frames into a real SSE queue.
            try:
                logger.debug(f'[AGENT] triage SSE {loop_id}: {frame.strip()[:240]}')
            except Exception:
                pass

        result = await TriageSkill().run(loop=loop, sink=_sink)
        logger.info(f"[AGENT] TriageSkill fired: loop={loop_id} inbox={result.inbox_size} accepted={result.proposals_accepted} elapsed_ms={result.elapsed_ms} failed={result.failed_reason or 'no'}")
    except Exception:
        logger.exception(f'[AGENT] TriageSkill: _fire_triage({loop_id}) failed')


def register_triage_job(loop_id: str, *, cron: Optional[str] = None) -> Optional[datetime]:
    """Register (or replace) one TriageSkill cron job.

    ``cron`` defaults to ``triage_interval_cron()`` (env-configurable,
    default ``"*/30 * * * *"`` IST). Idempotent — calling twice replaces
    the prior job rather than duplicating it.
    """
    if _scheduler is None:
        logger.warning('[AGENT] register_triage_job called before scheduler init')
        return None
    if not loop_id:
        return None

    from ..core.config import triage_interval_cron, loop_triage_enabled
    if not loop_triage_enabled():
        logger.info(f'[AGENT] LOOP_TRIAGE_ENABLED=false — refusing to register triage for {loop_id}')
        return None

    cron_expr = (cron or triage_interval_cron()).strip() or "*/30 * * * *"
    try:
        ap_trigger = CronTrigger.from_crontab(cron_expr, timezone=IST)
    except Exception:
        logger.warning(f'[AGENT] Invalid triage cron {cron_expr!r} for loop {loop_id} — skipping')
        return None

    deregister_triage_job(loop_id)
    job_id = f"loop-triage-job-{loop_id}"
    job = _scheduler.add_job(
        _fire_triage,
        trigger=ap_trigger,
        args=[loop_id],
        id=job_id,
        replace_existing=True,
        # Triage is best-effort + idempotent; grace + coalesce match the
        # regular trigger path so a missed tick (host pause / restart)
        # doesn't trigger a thundering herd of catch-up runs.
        misfire_grace_time=300,
        max_instances=1,
        coalesce=True,
    )
    _triage_job_index[loop_id] = job_id
    return job.next_run_time


def deregister_triage_job(loop_id: str) -> None:
    """Remove a previously-registered triage job. No-op when not present."""
    if _scheduler is None:
        return
    job_id = _triage_job_index.pop(loop_id, None)
    if not job_id:
        return
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass


async def _bootstrap_triage_jobs() -> None:
    """Register one triage job per active loop on startup.

    Called from ``init_scheduler`` once the regular trigger registration
    block has finished. Wrapped in its own try/except so a P5 install
    failure (e.g. missing agent_memory migration) can't stop the
    workflow-trigger scheduler from coming up.
    """
    try:
        from ..core.config import loop_triage_enabled
        if not loop_triage_enabled():
            logger.info('[AGENT] LOOP_TRIAGE_ENABLED=false — skipping triage bootstrap')
            return
        from ..loop import repo as loops_repo
        loops = await loops_repo.list_active_loops()
        for lp in loops:
            try:
                register_triage_job(lp.id or "")
            except Exception:
                logger.exception(f'[AGENT] Failed to register triage job for loop {lp.id} on startup')
        if loops:
            logger.info(f'[AGENT] Registered {len(loops)} triage job(s) from DB')
    except Exception:
        logger.exception('[AGENT] Triage bootstrap failed')


def register_trigger(trigger: Dict[str, Any]) -> Optional[datetime]:
    """Compute the next fire time for a newly-created / updated trigger.

    Since the dedicated scheduler worker polls the ``triggers`` table every
    60 s and dispatches whatever has ``next_run_at <= now``, all this function
    has to do at CRUD time is compute the initial ``next_run_at``. The caller
    (api/triggers.py) then persists it via ``update_trigger_run_metadata``.

    Kept as a sync function so existing callers don't need to change.
    Returns None for event-driven (webhook) triggers or malformed schedules.
    """
    schedule = trigger.get("schedule") or {}
    if (schedule.get("type") or "").lower() in ("webhook", "event"):
        logger.info(
            f"[AGENT] trigger {trigger.get('id')}: type="
            f"{schedule.get('type')} — event-driven, not scheduled"
        )
        return None
    return _compute_next_run_at(schedule)


def deregister_trigger(trigger_id: str) -> None:
    """No-op. The scheduler worker only enqueues rows where enabled=TRUE and
    next_run_at<=now, so setting enabled=false or deleting the row in the DB
    is sufficient — no in-memory state to clean up."""
    return


def reschedule_trigger(trigger: Dict[str, Any]) -> Optional[datetime]:
    """Compute the new next_run_at after a trigger update. Mirrors the
    original semantics: disabled triggers return None."""
    if not trigger.get("enabled"):
        return None
    return register_trigger(trigger)


def get_next_run(trigger_id: str) -> Optional[datetime]:
    """Legacy shim — previously read the next fire time from APScheduler's
    in-memory job. The DB row is now the source of truth (persisted by the
    CRUD path and the dispatcher), so callers should read
    ``trigger["next_run_at"]`` directly. Returns None here so the
    ``_trigger_to_out`` fallback in api/triggers.py degrades gracefully
    for legacy rows without a persisted next_run_at.
    """
    return None
