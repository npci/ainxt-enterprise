# SPDX-License-Identifier: MIT
"""CRUD for ``loops_pg`` / ``goals`` / ``loop_runs`` / ``loop_run_events``.

All async functions share the same shape as the rest of ``workflow_repo``:

  1. A top-level ``async def`` guards with ``workflow_repo._require_uri()``
     so the in-memory fallback path raises a clear "DB not configured"
     error instead of a confusing ``NoneType has no attribute``.
  2. The synchronous body lives in an inner ``_run()`` that opens a
     connection via ``workflow_repo._get_pool().connection()`` and uses
     the **psycopg v3 sync API** (``conn.execute(sql, params).fetchone()``).
  3. The outer wrapper calls ``await asyncio.to_thread(_run)`` so the
     FastAPI event loop is never blocked on a DB I/O.

This file deliberately re-uses the platform pool. Per D5 there is one
Postgres pool in ABStudio; introducing a second would invalidate the
sizing math in ``workflow_repo.init_db()`` (which is calibrated against
the host's ``max_connections``).
"""

from __future__ import annotations

import asyncio
import json

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core import workflow_repo
from app.loop.models import (
    Goal,
    InboxItem,
    Lesson,
    LoopRecord,
    LoopStatus,
    Reflection,
    StoppingCondition,
    TriageProposal,
)

from core.logger import logger
# ---------------------------------------------------------------------------
# Row → model helpers
# ---------------------------------------------------------------------------


# Column order used in every SELECT against ``loops_pg``. Centralised so a
# future ALTER TABLE that adds a column doesn't silently slide the indices
# of unrelated readers — every caller pulls from the same source of truth.
_LOOPS_COLUMNS = (
    "id", "name", "org_id", "category", "description",
    "trigger", "action", "proof", "memory", "stopping_condition",
    "isolation", "verify", "on_unresolved",
    "version", "status", "visibility", "department",
    "owner_user_id", "created_by", "approved_by", "approved_at",
    "enabled", "created_at", "updated_at",
)
_LOOPS_SELECT = ", ".join(_LOOPS_COLUMNS)


_GOALS_COLUMNS = (
    "id", "name", "description",
    "predicate_kind", "predicate", "stop_condition",
    "owner_user_id", "department",
    "created_at", "updated_at",
)
_GOALS_SELECT = ", ".join(_GOALS_COLUMNS)


def _coerce_jsonb(value: Any) -> Any:
    """psycopg v3 already decodes ``JSONB`` to Python; this is a no-op for
    that path. The helper exists so the caller is robust against the
    in-memory test fallback (where a fixture might shove a JSON string in
    by accident) without scattering ``isinstance`` checks across every
    row-mapper."""
    if isinstance(value, (dict, list)):
        return value
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _row_to_loop(row: tuple) -> LoopRecord:
    """Map a tuple from ``SELECT _LOOPS_SELECT`` into a ``LoopRecord``.

    Pydantic re-validates on construction so a bad row in the DB (e.g.
    a hand-edited ``stopping_condition`` with ``max_iterations=0``) will
    raise here — surfacing through the API as a 500 instead of a silent
    runtime corruption later.
    """
    return LoopRecord(
        id           = row[0],
        name         = row[1],
        org_id       = row[2],
        category     = row[3],
        description  = row[4],
        trigger            = _coerce_jsonb(row[5])  or {"type": "manual"},
        action             = _coerce_jsonb(row[6])  or {"engine": "workflow", "target_id": ""},
        proof              = _coerce_jsonb(row[7])  or [],
        memory             = _coerce_jsonb(row[8])  or {"scope": "run", "carry": []},
        stopping_condition = _coerce_jsonb(row[9])  or {"max_iterations": 1, "budget_tokens": 1},
        # row[10] is the vestigial ``isolation`` column (worktree isolation
        # was removed) — read but not mapped onto the model.
        verify             = _coerce_jsonb(row[11]) or {"independent_agent": False},
        on_unresolved      = _coerce_jsonb(row[12]) or {"route_to": "triage_inbox"},
        version       = row[13],
        status        = LoopStatus(row[14]),
        visibility    = row[15],
        department    = row[16],
        owner_user_id = row[17],
        created_by    = row[18],
        approved_by   = row[19],
        approved_at   = row[20],
        enabled       = row[21],
        created_at    = row[22],
        updated_at    = row[23],
    )


def _row_to_goal(row: tuple) -> Goal:
    return Goal(
        id             = row[0],
        name           = row[1],
        description    = row[2],
        predicate_kind = row[3],
        predicate      = _coerce_jsonb(row[4]) or {},
        stop_condition = _coerce_jsonb(row[5]) or {"max_iterations": 1, "budget_tokens": 1},
        owner_user_id  = row[6],
        department     = row[7],
        created_at     = row[8],
        updated_at     = row[9],
    )


# ---------------------------------------------------------------------------
# Loop reads
# ---------------------------------------------------------------------------


async def get_loop(loop_id: str) -> Optional[LoopRecord]:
    """Read a Loop by id. Returns ``None`` when not found (the API layer
    translates that to 404)."""
    workflow_repo._require_uri()

    def _run():
        with workflow_repo._get_pool().connection() as conn:
            row = conn.execute(
                f"SELECT {_LOOPS_SELECT} FROM loops_pg WHERE id = %s",
                (loop_id,),
            ).fetchone()
        return _row_to_loop(row) if row else None

    return await asyncio.to_thread(_run)


async def get_goal(goal_id: str) -> Optional[Goal]:
    workflow_repo._require_uri()

    def _run():
        with workflow_repo._get_pool().connection() as conn:
            row = conn.execute(
                f"SELECT {_GOALS_SELECT} FROM goals WHERE id = %s",
                (goal_id,),
            ).fetchone()
        return _row_to_goal(row) if row else None

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Run-level audit writers
# ---------------------------------------------------------------------------
# These ship in P1 so the LoopRunner in P2 can land without a follow-up
# migration. Each writer is a tight ``INSERT … ; conn.commit()`` because
# loop_runs / loop_run_events / budget_ledger are append-only.


async def insert_run(
    *,
    run_id:        str,
    loop_id:       Optional[str],
    goal_id:       Optional[str],
    workflow_id:   Optional[str],
    thread_id:     Optional[str],
    trigger_src:   str,
    owner_user_id: Optional[str],
) -> None:
    """Create the ``loop_runs`` row at the top of LoopRunner.execute()."""
    workflow_repo._require_uri()

    def _run():
        with workflow_repo._get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO loop_runs "
                "(id, loop_id, goal_id, workflow_id, thread_id, trigger_src, "
                " owner_user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (run_id, loop_id, goal_id, workflow_id, thread_id,
                 trigger_src, owner_user_id),
            )
            conn.commit()

    return await asyncio.to_thread(_run)


# Columns we let ``update_run`` touch from the LoopRunner.
_UPDATABLE_RUN_FIELDS = {
    "status",
    "iterations",
    "tokens_used",
    "wall_clock_s",
    "termination",
    "outcome",
    "initial_score",
    "final_score",
    "ended_at",
}


async def update_run(run_id: str, **fields: Any) -> None:
    """Patch an in-flight or terminated ``loop_runs`` row.

    Only fields in ``_UPDATABLE_RUN_FIELDS`` are honoured; anything else
    is silently dropped so a buggy caller can't overwrite ``id`` /
    ``started_at`` / ``owner_user_id``.
    """
    workflow_repo._require_uri()
    sanitised = {k: v for k, v in fields.items() if k in _UPDATABLE_RUN_FIELDS}
    if not sanitised:
        return

    set_pairs: List[str] = []
    values:    List[Any] = []
    for key, value in sanitised.items():
        if key == "outcome":
            set_pairs.append("outcome = %s::jsonb")
            values.append(json.dumps(value or {}))
        else:
            set_pairs.append(f"{key} = %s")
            values.append(value)
    values.append(run_id)

    def _run():
        with workflow_repo._get_pool().connection() as conn:
            conn.execute(
                f"UPDATE loop_runs SET {', '.join(set_pairs)} WHERE id = %s",
                values,
            )
            conn.commit()

    return await asyncio.to_thread(_run)


async def append_event(
    run_id:  str,
    seq:     int,
    kind:    str,
    payload: Dict[str, Any],
) -> None:
    """Append a row to ``loop_run_events``. ``kind`` is one of
    ``iteration|proof|verifier|inbox|compliance_block|reflection|budget|gate``.

    No validation on ``kind`` here — the table accepts any TEXT and the
    runtime owners know the vocabulary. Keeps the writer cheap on the
    hot path."""
    workflow_repo._require_uri()

    def _run():
        with workflow_repo._get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO loop_run_events (run_id, seq, kind, payload) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                (run_id, seq, kind, json.dumps(payload or {})),
            )
            conn.commit()

    return await asyncio.to_thread(_run)


async def record_budget(
    run_id:       str,
    *,
    tokens:       int,
    wall_clock_s: float,
    cost_usd:     Optional[float],
    source:       str,
) -> None:
    """Append a row to ``budget_ledger``."""
    workflow_repo._require_uri()

    def _run():
        with workflow_repo._get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO budget_ledger "
                "(run_id, tokens, wall_clock_s, cost_usd, source) "
                "VALUES (%s, %s, %s, %s, %s)",
                (run_id, int(tokens), float(wall_clock_s), cost_usd, source),
            )
            conn.commit()

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# P4 — verification_gate_runs writers
# ---------------------------------------------------------------------------
# Schema lives in P1 migrations: (id PK, loop_run_id FK, outer_iteration,
# verdict, risk_class, reasons JSONB, confidence, evidence JSONB, model,
# temperature, elapsed_ms, tokens_in, tokens_out, raw_response, created_at).


async def record_verification_gate(
    *,
    loop_run_id:     str,
    outer_iteration: int,
    verdict:         str,
    risk_class:      str,
    reasons:         List[str],
    confidence:      float,
    evidence:        List[Dict[str, Any]],
    model:           str,
    temperature:     float,
    elapsed_ms:      int,
    tokens_in:       int,
    tokens_out:      int,
    raw_response:    Optional[str] = None,
) -> None:
    """Append one verifier verdict row.

    ``raw_response`` is persisted only when the caller chose to record it
    (gate enforced by ``app.core.config.verifier_debug()``). The writer
    itself doesn't second-guess the caller — it just persists what it's
    given, with the appropriate JSONB casts.
    """
    workflow_repo._require_uri()

    def _run():
        with workflow_repo._get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO verification_gate_runs "
                "(loop_run_id, outer_iteration, verdict, risk_class, "
                " reasons, confidence, evidence, model, temperature, "
                " elapsed_ms, tokens_in, tokens_out, raw_response) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s, "
                "        %s, %s, %s, %s)",
                (
                    loop_run_id, int(outer_iteration), verdict, risk_class,
                    json.dumps(list(reasons or [])),
                    float(confidence),
                    json.dumps(list(evidence or [])),
                    model, float(temperature),
                    int(elapsed_ms), int(tokens_in), int(tokens_out),
                    raw_response,
                ),
            )
            conn.commit()

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# P5 — Reflection / Triage / Lessons / Memory
# ---------------------------------------------------------------------------
# Anchored on the P1 ``reflections`` table (scope_kind / scope_id / tag /
# content / source_run / created_at) with the P5 additions in
# workflow_repo.init_db (``loop_run_id`` / ``outer_iteration`` columns).
# Every writer here is best-effort — the caller (LoopRunner / TriageSkill)
# logs and continues so a Postgres outage never poisons a live run.


async def insert_reflection(r: Reflection) -> None:
    """Append one reflection row.

    Maps the loop-shaped Pydantic model onto the generic scope/content
    columns: ``scope_kind='loop'``, ``scope_id=loop_id``, ``content=lesson``,
    ``tag=kind.value``, ``source_run=loop_run_id``, ``loop_run_id`` /
    ``outer_iteration`` go into the P5-added columns.

    ``tags`` (Pydantic list) collapses into the single ``tag`` column for
    the index; the full list is preserved by serialising the first
    element. Callers that need the full list back can re-derive it from
    the lesson body — v1 only consults the scalar ``tag`` for the
    ``WHERE tag = 'verifier_fail'`` analytics query.
    """
    workflow_repo._require_uri()
    rid = r.id or str(uuid.uuid4())
    primary_tag = r.kind.value
    created_at = r.created_at or datetime.now(timezone.utc)

    def _run():
        with workflow_repo._get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO reflections "
                "(id, scope_kind, scope_id, tag, content, "
                " source_run, loop_run_id, outer_iteration, created_at) "
                "VALUES (%s, 'loop', %s, %s, %s, %s, %s, %s, %s)",
                (
                    rid, r.loop_id, primary_tag, r.lesson,
                    r.loop_run_id, r.loop_run_id, r.outer_iteration,
                    created_at,
                ),
            )
            conn.commit()

    return await asyncio.to_thread(_run)


async def list_top_reflections(loop_id: str, limit: int) -> List[Lesson]:
    """Return the most recent ``limit`` reflections for ``loop_id``.

    Ordered ``created_at DESC`` so the maker prompt sees the freshest
    lessons first (P5 v1 ranking — vector recall is explicitly deferred
    per §16). The ``tag`` column drives a single-element ``tags`` list on
    the returned ``Lesson`` for backwards compatibility with callers that
    expect a list.
    """
    workflow_repo._require_uri()
    safe_limit = max(1, min(int(limit), 500))

    def _run():
        with workflow_repo._get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT content, tag, created_at "
                "FROM reflections "
                "WHERE scope_kind = 'loop' AND scope_id = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (loop_id, safe_limit),
            ).fetchall()
        return [
            Lesson(
                lesson=row[0] or "",
                tags=[row[1]] if row[1] else [],
                created_at=row[2],
            )
            for row in rows
        ]

    return await asyncio.to_thread(_run)


async def insert_triage_proposal(p: TriageProposal) -> str:
    """Insert a triage-proposed goal with status pinned to PENDING_APPROVAL.

    Returns the new goal id. Relies on the partial unique index
    ``uniq_goals_loop_source`` for dedup — a duplicate raises
    ``psycopg.errors.UniqueViolation`` which the caller (TriageSkill)
    catches and counts as ``duplicates_skipped``.

    ``stop_condition`` defaults to env-driven budget caps because a
    triage proposal is, by definition, half a goal — the human approver
    decides the predicate before promotion. The schema column is NOT NULL
    so we always write *something* here.
    """
    workflow_repo._require_uri()
    goal_id = str(uuid.uuid4())
    # Mirror app.core.config.budget_defaults() shape so the StoppingCondition
    # round-trip stays lossless on PUT.
    default_stop = StoppingCondition(
        measure="",
        max_iterations=10,
        budget_tokens=200_000,
        wall_clock_s=3600,
    )

    payload = (
        goal_id,
        p.title[:200],
        p.description[:4000],
        "llm_judge",
        json.dumps({"goal_text": p.title}),
        json.dumps(default_stop.model_dump()),
        p.loop_id,
        p.title[:200],
        "PENDING_APPROVAL",
        p.source_item.source,
        p.source_item.external_id,
        float(p.confidence),
    )

    def _run():
        with workflow_repo._get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO goals "
                "(id, name, description, predicate_kind, predicate, "
                " stop_condition, loop_id, title, status, "
                " source, source_external_id, confidence) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, "
                "        %s, %s, %s, %s, %s, %s)",
                payload,
            )
            conn.commit()
        return goal_id

    return await asyncio.to_thread(_run)


async def find_open_goals_for_loop(loop_id: str) -> List[Dict[str, Any]]:
    """Read goals owned by ``loop_id`` that are still actionable.

    "Open" means ``PENDING_APPROVAL`` or ``APPROVED`` — these are the two
    statuses the triage dedup guard should treat as "we already opened a
    proposal for this external_id; skip it".
    """
    workflow_repo._require_uri()

    def _run():
        with workflow_repo._get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT id, title, source, source_external_id, status "
                "FROM goals "
                "WHERE loop_id = %s "
                "  AND status IN ('PENDING_APPROVAL', 'APPROVED')",
                (loop_id,),
            ).fetchall()
        return [
            {
                "id":                 row[0],
                "title":              row[1] or "",
                "source":             row[2] or "",
                "source_external_id": row[3] or "",
                "status":             row[4] or "",
            }
            for row in rows
        ]

    return await asyncio.to_thread(_run)


# ── triage inbox source: failed loop_runs in the last 24h ───────────────────


async def list_recent_run_failures(loop_id: str, limit: int) -> List[InboxItem]:
    """Read ``loop_runs`` rows whose terminal state was non-success in the
    last 24 hours. Used by the triage skill's only v1 inbox source.

    The ``loop_runs.status`` column carries the canonical run termination
    text — ``FAILED`` / ``BUDGET_EXHAUSTED`` / ``MAX_ITERATIONS`` per
    runner.py:475. The status is mapped here onto the InboxItem's
    severity scale so the LLM triage prompt can prioritise.
    """
    workflow_repo._require_uri()
    safe_limit = max(1, min(int(limit), 500))

    def _run():
        with workflow_repo._get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT id, status, termination, outcome, started_at "
                "FROM loop_runs "
                "WHERE loop_id = %s "
                "  AND status IN ('FAILED','BUDGET_EXHAUSTED','MAX_ITERATIONS') "
                "  AND started_at > NOW() - INTERVAL '24 hours' "
                "ORDER BY started_at DESC LIMIT %s",
                (loop_id, safe_limit),
            ).fetchall()
        items: list[InboxItem] = []
        for row in rows:
            run_id     = row[0] or ""
            status     = row[1] or ""
            terminate  = row[2] or status or ""
            outcome    = _coerce_jsonb(row[3]) or {}
            snippet    = ""
            if isinstance(outcome, dict):
                snippet = str(outcome.get("final_output_preview") or "")[:1024]
            severity = "high" if status == "FAILED" else "med"
            items.append(InboxItem(
                source="loop_runs_failure",
                external_id=run_id,
                title=f"Run {run_id[:8]} ended {terminate or status}",
                snippet=snippet,
                severity=severity,
                discovered_at=row[4],
            ))
        return items

    return await asyncio.to_thread(_run)


# ── agent_memory wrapper (P5 §6) ────────────────────────────────────────────


async def memory_get(scope: str, key: str) -> Optional[Dict[str, Any]]:
    """Read one ``agent_memory`` value. Returns ``None`` when missing.

    Best-effort decode: the column is JSONB so psycopg already returns a
    Python dict, but the helper guards against the legacy text shape some
    tests use by JSON-parsing strings before returning.
    """
    workflow_repo._require_uri()

    def _run():
        with workflow_repo._get_pool().connection() as conn:
            row = conn.execute(
                "SELECT value FROM loop_agent_memory WHERE scope = %s AND key = %s",
                (scope, key),
            ).fetchone()
        if row is None:
            return None
        value = _coerce_jsonb(row[0])
        return value if isinstance(value, dict) else None

    return await asyncio.to_thread(_run)


async def memory_put(scope: str, key: str, value: Dict[str, Any]) -> None:
    """Upsert one ``agent_memory`` row. Mutates ``updated_at`` on conflict."""
    workflow_repo._require_uri()

    def _run():
        with workflow_repo._get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO loop_agent_memory (scope, key, value, updated_at) "
                "VALUES (%s, %s, %s::jsonb, NOW()) "
                "ON CONFLICT (scope, key) DO UPDATE SET "
                "    value = EXCLUDED.value, updated_at = NOW()",
                (scope, key, json.dumps(value or {})),
            )
            conn.commit()

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# P5 — scheduler helper
# ---------------------------------------------------------------------------


async def list_active_loops() -> List[LoopRecord]:
    """Return every enabled loop that has not been deprecated.

    Used by ``trigger_scheduler.init_scheduler`` to register one
    ``LoopTriageJob`` per active loop on FastAPI startup.
    """
    workflow_repo._require_uri()

    def _run():
        with workflow_repo._get_pool().connection() as conn:
            rows = conn.execute(
                f"SELECT {_LOOPS_SELECT} FROM loops_pg "
                f"WHERE status <> %s AND enabled = TRUE "
                f"ORDER BY updated_at DESC",
                (LoopStatus.DEPRECATED.value,),
            ).fetchall()
        return [_row_to_loop(r) for r in rows]

    return await asyncio.to_thread(_run)


# Re-export sentinel-style helpers so test fixtures don't have to reach
# into private workflow_repo internals.
__all__ = [
    "get_loop", "get_goal",
    "insert_run", "update_run", "append_event", "record_budget",
    "record_verification_gate",
    "insert_reflection", "list_top_reflections",
    "insert_triage_proposal", "find_open_goals_for_loop",
    "list_recent_run_failures",
    "memory_get", "memory_put",
    "list_active_loops",
]
