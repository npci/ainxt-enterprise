# SPDX-License-Identifier: MIT
"""
PostgreSQL checkpoint store — production-grade chat history persistence.

Activated automatically when POSTGRES_HOST is set in .env.
Reuses the platform's single shared connection pool (db.database.engine) via
app.core.db_pool.SHARED_POOL, so chat-history I/O shares the same pool as the
rest of the process rather than opening its own.

Database schema (auto-created on startup):
  Table: chat_threads
    thread_id    TEXT  PRIMARY KEY
    workflow_id  TEXT
    messages     JSONB           array of {role, content} objects
    last_updated TIMESTAMPTZ

Falls back silently to FileCheckpointStore when the URI is not set.

Used by: native_engine.py (selected over FileCheckpointStore at startup)
"""

from __future__ import annotations

import asyncio
import json

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .postgres_schema import THREAD_DEPENDENT_TABLES, ensure_schema
from .store import ChatMessage, CheckpointStore, ThreadSummary, summarise_thread

from core.logger import logger
from app.core.config import postgres_enabled


def _owner_scope_sql(
    owner_user_id: Optional[str], params: tuple, *, prefix: str = "",
) -> tuple:
    """Build the owner-scoping WHERE fragment + extended params tuple shared
    by every owner-aware read/delete query in this module (security review
    F-06/F-10 follow-up — collapses what was six near-identical if/else
    branches into one call site each).

    Returns (sql_fragment, extended_params). ``sql_fragment`` is either ""
    (owner_user_id is None — caller skips the ownership check entirely, used
    only by internal/legacy call sites) or a clause appended to ``params``
    that allows the row ONLY when its owner matches.

    Bug fix (workflow chat cross-user visibility): the clause used to also
    allow ``legacy_no_owner = TRUE`` rows, which let every user of a shared
    workflow read/list/delete pre-migration threads created by anyone else.
    Because workflows are shared objects (many users open the same
    workflow_id), that fail-open surfaced as "anyone can see anyone's chat".
    Legacy rows carry no recorded owner, so with the ``OR legacy_no_owner``
    branch removed they are now inaccessible to everyone — matching the
    strictly owner-scoped behavior of the agent chat store, which never had
    this fail-open. ``prefix`` is an optional table alias (e.g. "t.") for
    queries that join multiple tables; the ``owner_user_id`` column name is
    fixed across all three tables this helper serves. (The ``legacy_no_owner``
    column is retained in the schema for auditing but is no longer consulted
    for access decisions.)
    """
    if owner_user_id is None:
        return "", params
    return (
        f" AND {prefix}owner_user_id = %s",
        params + (owner_user_id,),
    )


class PostgresCheckpointStore(CheckpointStore):
    """
    Stores chat history in a `chat_threads` table.

    Schema:
        CREATE TABLE chat_threads (
            thread_id    TEXT PRIMARY KEY,
            workflow_id  TEXT NOT NULL,
            messages     JSONB NOT NULL DEFAULT '[]',
            last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """

    def __init__(self, uri: str = "") -> None:
        # ``uri`` is accepted for backwards compatibility but ignored: the store
        # borrows from the shared platform pool, not a per-store connection.
        self._pool = None

    async def startup(self) -> None:
        if not postgres_enabled():
            logger.warning('[AGENT] PostgresCheckpointStore: POSTGRES_HOST not set — store disabled')
            return
        await asyncio.to_thread(self._init_pool)

    def _init_pool(self) -> None:
        # Reuse the platform's single shared connection pool instead of opening
        # a separate psycopg pool. Chat-history I/O now shares the same pool as
        # every other subsystem in the process. Pool sizing lives in
        # db/database.py; the legacy CHECKPOINT_PG_POOL_* env vars are unused.
        from app.core.db_pool import SHARED_POOL
        self._pool = SHARED_POOL
        # All DDL + one-time owner migrations live in postgres_schema. They run
        # on this single connection and are committed here, so schema creation
        # and migration land atomically — a partial apply would leave tables
        # without their owner_user_id column and break every scoped query.
        with self._pool.connection() as conn:
            ensure_schema(conn)
            conn.commit()
        logger.info('[AGENT] PostgresCheckpointStore: tables ready')

    async def shutdown(self) -> None:
        # ``_pool`` is the shared platform pool (owned by db.database.engine);
        # it must outlive ABStudio, so only drop the reference — never close it.
        self._pool = None

    def _require_pool(self):
        if not self._pool:
            raise RuntimeError("PostgresCheckpointStore not initialised — check POSTGRES_HOST")
        return self._pool

    async def save_messages(
        self, thread_id: str, workflow_id: str, messages: List[ChatMessage],
        owner_user_id: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        def _serialize(m: ChatMessage) -> dict:
            payload = {"role": m.role, "content": m.content}
            # Persist file attachments so download chips survive reload.
            if m.generated_files:
                payload["generated_files"] = m.generated_files
            if m.usage:
                payload["usage"] = m.usage
            if m.duration_s is not None:
                payload["duration_s"] = m.duration_s
            return payload

        msgs_json = json.dumps([_serialize(m) for m in messages])

        def _run():
            with self._require_pool().connection() as conn:
                # Security review F-06/F-10: record owner_user_id on first
                # INSERT; on conflict, COALESCE so a later call (including
                # legacy call sites that pass owner_user_id=None) never
                # clobbers an already-recorded owner.
                conn.execute("""
                    INSERT INTO chat_threads (thread_id, workflow_id, messages, last_updated, owner_user_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (thread_id) DO UPDATE
                        SET messages = EXCLUDED.messages,
                            last_updated = EXCLUDED.last_updated,
                            owner_user_id = COALESCE(chat_threads.owner_user_id, EXCLUDED.owner_user_id)
                """, (thread_id, workflow_id, msgs_json, now, owner_user_id))
                conn.commit()
        await asyncio.to_thread(_run)

    async def load_messages(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> List[ChatMessage]:
        def _run():
            owner_sql, params = _owner_scope_sql(owner_user_id, (thread_id,))
            with self._require_pool().connection() as conn:
                row = conn.execute(
                    f"SELECT messages FROM chat_threads WHERE thread_id = %s{owner_sql}",
                    params,
                ).fetchone()
            return row[0] if row else []
        rows = await asyncio.to_thread(_run)
        return [
            ChatMessage(
                role=r["role"],
                content=r["content"],
                generated_files=r.get("generated_files") or None,
                usage=r.get("usage") or None,
                duration_s=r.get("duration_s"),
            )
            for r in (rows or [])
        ]

    async def get_thread_owner(self, thread_id: str) -> Optional[str]:
        def _run():
            with self._require_pool().connection() as conn:
                return conn.execute(
                    "SELECT owner_user_id FROM chat_threads WHERE thread_id = %s",
                    (thread_id,),
                ).fetchone()
        row = await asyncio.to_thread(_run)
        if row is None:
            return None
        return row[0] or ""

    async def list_threads(
        self, workflow_id: str, owner_user_id: Optional[str] = None,
    ) -> List[ThreadSummary]:
        def _run():
            owner_sql, params = _owner_scope_sql(owner_user_id, (workflow_id,), prefix="t.")
            with self._require_pool().connection() as conn:
                # LEFT JOIN so a thread without a pending interrupt still
                # appears; pi.thread_id IS NULL → no pause. Also project
                # the snapshot ``reason`` (via JSONB `->>` accessor) so the
                # sidebar can distinguish a node_failed pause from an HITL
                # pause without a second /chat-pending fetch per thread.
                rows = conn.execute(
                    f"""
                    SELECT t.thread_id, t.messages, t.last_updated,
                           (pi.thread_id IS NOT NULL) AS has_pending,
                           COALESCE(pi.snapshot->>'reason', '') AS pending_reason
                    FROM chat_threads t
                    LEFT JOIN pending_interrupts pi
                           ON pi.thread_id = t.thread_id
                    WHERE t.workflow_id = %s{owner_sql}
                    ORDER BY t.last_updated DESC
                    """,
                    params,
                ).fetchall()
            return rows
        rows = await asyncio.to_thread(_run)
        return [
            summarise_thread(
                tid, msgs or [], lu.isoformat() if lu else None,
                has_pending_interrupt=bool(has_pending),
                pending_reason=reason or "",
            )
            for tid, msgs, lu, has_pending, reason in (rows or [])
        ]

    async def delete_thread(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> bool:
        def _run() -> bool:
            owner_sql, params = _owner_scope_sql(owner_user_id, (thread_id,))
            with self._require_pool().connection() as conn:
                if owner_user_id is not None:
                    owned = conn.execute(
                        f"SELECT 1 FROM chat_threads WHERE thread_id = %s{owner_sql}",
                        params,
                    ).fetchone()
                    if not owned:
                        return False
                # Drop dependent rows first so we don't leave dangling
                # records pointing at a deleted thread.
                #
                # Security review (execution-layer tenant isolation): this
                # cascade previously covered only pending_interrupts and
                # chat_thread_node_outputs, so deleting a conversation left
                # the user's prompts and agent outputs behind in
                # run_events.payload, run_steps.input_snapshot and
                # loop_iterations.output_preview — a data-erasure gap, and a
                # silent disagreement with delete_threads_for_workflow, which
                # has always cleared all seven. The table list lives next to the
                # DDL that creates them (postgres_schema.THREAD_DEPENDENT_TABLES)
                # so both cascades derive from one source and cannot drift apart
                # again. Every table listed is keyed by thread_id and indexed on
                # it (or on (thread_id, …)).
                for _table in THREAD_DEPENDENT_TABLES:
                    conn.execute(
                        f"DELETE FROM {_table} WHERE thread_id = %s", (thread_id,)
                    )
                cur = conn.execute("DELETE FROM chat_threads WHERE thread_id = %s", (thread_id,))
                conn.commit()
                return bool(cur.rowcount)
        return await asyncio.to_thread(_run)

    async def delete_threads_for_workflow(self, workflow_id: str) -> int:
        """Cascade-delete every chat thread + audit row for a workflow.

        ``pending_interrupts`` has no workflow_id column, so its rows are
        removed via a subquery on the threads being deleted — and that must
        happen BEFORE chat_threads is emptied, or the subquery matches nothing.
        """
        def _run() -> int:
            with self._require_pool().connection() as conn:
                conn.execute(
                    """
                    DELETE FROM pending_interrupts
                    WHERE thread_id IN (
                        SELECT thread_id FROM chat_threads WHERE workflow_id = %s
                    )
                    """,
                    (workflow_id,),
                )
                # The remaining tables all carry workflow_id directly. Derived
                # from the same THREAD_DEPENDENT_TABLES list delete_thread uses
                # (minus pending_interrupts, handled above) so the two cascades
                # cannot fall out of step — the disagreement that let run-state
                # rows survive a thread delete before d63926d.
                for _table in THREAD_DEPENDENT_TABLES:
                    if _table == "pending_interrupts":
                        continue
                    conn.execute(
                        f"DELETE FROM {_table} WHERE workflow_id = %s", (workflow_id,)
                    )
                cur = conn.execute("DELETE FROM chat_threads WHERE workflow_id = %s", (workflow_id,))
                deleted = cur.rowcount or 0
                conn.commit()
                return deleted
        return await asyncio.to_thread(_run)

    # ---------------- HITL pending interrupts ----------------

    async def save_pending_interrupt(
        self, thread_id: str, snapshot: Dict[str, Any],
        owner_user_id: Optional[str] = None,
    ) -> None:
        snap_json = json.dumps(snapshot, default=str)

        def _run():
            with self._require_pool().connection() as conn:
                conn.execute(
                    """
                    INSERT INTO pending_interrupts (thread_id, snapshot, created_at, owner_user_id)
                    VALUES (%s, %s, NOW(), %s)
                    ON CONFLICT (thread_id) DO UPDATE
                        SET snapshot      = EXCLUDED.snapshot,
                            created_at    = NOW(),
                            owner_user_id = COALESCE(pending_interrupts.owner_user_id, EXCLUDED.owner_user_id)
                    """,
                    (thread_id, snap_json, owner_user_id),
                )
                conn.commit()
        await asyncio.to_thread(_run)

    async def load_pending_interrupt(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        def _run():
            owner_sql, params = _owner_scope_sql(owner_user_id, (thread_id,))
            with self._require_pool().connection() as conn:
                row = conn.execute(
                    f"SELECT snapshot FROM pending_interrupts WHERE thread_id = %s{owner_sql}",
                    params,
                ).fetchone()
            return row[0] if row else None
        return await asyncio.to_thread(_run)

    async def delete_pending_interrupt(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> None:
        def _run():
            owner_sql, params = _owner_scope_sql(owner_user_id, (thread_id,))
            with self._require_pool().connection() as conn:
                conn.execute(
                    f"DELETE FROM pending_interrupts WHERE thread_id = %s{owner_sql}",
                    params,
                )
                conn.commit()
        await asyncio.to_thread(_run)

    # ---------------- Per-node last outputs ----------------

    async def save_node_output(
        self,
        thread_id: str,
        workflow_id: str,
        node_id: str,
        agent: str,
        output: str,
        owner_user_id: Optional[str] = None,
    ) -> None:
        def _run():
            with self._require_pool().connection() as conn:
                conn.execute(
                    """
                    INSERT INTO chat_thread_node_outputs
                        (thread_id, node_id, workflow_id, agent, output, updated_at, owner_user_id)
                    VALUES (%s, %s, %s, %s, %s, NOW(), %s)
                    ON CONFLICT (thread_id, node_id) DO UPDATE
                        SET agent         = EXCLUDED.agent,
                            output        = EXCLUDED.output,
                            updated_at    = NOW(),
                            owner_user_id = COALESCE(chat_thread_node_outputs.owner_user_id, EXCLUDED.owner_user_id)
                    """,
                    (thread_id, node_id, workflow_id, agent, output, owner_user_id),
                )
                conn.commit()
        await asyncio.to_thread(_run)

    async def load_node_output(
        self, thread_id: str, node_id: str, owner_user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        def _run():
            owner_sql, params = _owner_scope_sql(owner_user_id, (thread_id, node_id))
            with self._require_pool().connection() as conn:
                row = conn.execute(
                    f"""
                    SELECT agent, output, updated_at
                    FROM chat_thread_node_outputs
                    WHERE thread_id = %s AND node_id = %s{owner_sql}
                    """,
                    params,
                ).fetchone()
            return row
        row = await asyncio.to_thread(_run)
        if not row:
            return None
        agent, output, updated_at = row
        return {
            "agent":      agent,
            "output":     output,
            "updated_at": updated_at.isoformat() if updated_at else None,
        }

    # ---------------- Loop / Condition / HITL audit trails ----------------

    async def _insert(self, sql: str, params: tuple) -> None:
        """Shared INSERT helper for append-only audit tables."""
        def _run():
            with self._require_pool().connection() as conn:
                conn.execute(sql, params)
                conn.commit()
        await asyncio.to_thread(_run)

    # ---------------- Loop cross-run memory (lessons) ----------------

    async def save_loop_lesson(
        self, workflow_id: str, node_id: str, digest: str
    ) -> None:
        if not digest:
            return
        await self._insert(
            """
            INSERT INTO loop_lessons (workflow_id, node_id, digest)
            VALUES (%s, %s, %s)
            """,
            (workflow_id, node_id, str(digest)),
        )

    async def load_loop_lessons(
        self, workflow_id: str, node_id: str
    ) -> Optional[str]:
        def _run():
            with self._require_pool().connection() as conn:
                rows = conn.execute(
                    """
                    SELECT digest FROM loop_lessons
                    WHERE workflow_id = %s AND node_id = %s
                    ORDER BY created_at DESC
                    LIMIT 5
                    """,
                    (workflow_id, node_id),
                ).fetchall()
            return rows
        rows = await asyncio.to_thread(_run)
        if not rows:
            return None
        # Rows come back newest-first; present oldest-first so the agent reads
        # lessons in chronological order. Cap total length to protect the prompt.
        digests = [r[0] for r in reversed(rows) if r and r[0]]
        if not digests:
            return None
        return "\n---\n".join(digests)[:4000]

    async def save_loop_iteration(
        self,
        thread_id: str,
        workflow_id: str,
        node_id: str,
        index: int,
        mode: str,
        total: Optional[int] = None,
        score: Optional[float] = None,
        changes: Optional[str] = None,
        will_continue: Optional[bool] = None,
        case_results: Optional[list] = None,
        output_preview: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ) -> None:
        await self._insert(
            """
            INSERT INTO loop_iterations
                (thread_id, workflow_id, node_id, iteration, mode,
                 total, score, changes, will_continue, case_results,
                 output_preview, owner_user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                thread_id, workflow_id, node_id, index, mode,
                total, score, changes, will_continue,
                json.dumps(case_results or [], default=str),
                output_preview, owner_user_id,
            ),
        )

    async def save_condition_routing(
        self,
        thread_id: str,
        workflow_id: str,
        node_id: str,
        matched_case_id: Optional[str],
        matched_label: Optional[str],
        matched_expression: Optional[str],
        upstream_output_preview: Optional[str],
        evaluated_state: Optional[Dict[str, Any]],
        target_node_id: Optional[str],
        owner_user_id: Optional[str] = None,
    ) -> None:
        await self._insert(
            """
            INSERT INTO condition_routings
                (thread_id, workflow_id, node_id, matched_case_id,
                 matched_label, matched_expression,
                 upstream_output_preview, evaluated_state,
                 target_node_id, owner_user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                thread_id, workflow_id, node_id, matched_case_id,
                matched_label, matched_expression,
                upstream_output_preview,
                json.dumps(evaluated_state or {}, default=str),
                target_node_id, owner_user_id,
            ),
        )

    async def save_hitl_decision(
        self,
        thread_id: str,
        workflow_id: str,
        node_id: str,
        reason: str,
        hitl_mode: str,
        decision: str,
        human_input: str,
        user_id: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ) -> None:
        # ``user_id`` and ``owner_user_id`` are deliberately distinct: the
        # former is the ACTING user (who approved/rejected this interrupt),
        # the latter is the thread's owner, used for tenant scoping. They are
        # usually the same person but need not be — an admin resuming another
        # user's paused run must be audited as the actor without re-homing the
        # row into their own tenant scope.
        await self._insert(
            """
            INSERT INTO hitl_decisions
                (thread_id, workflow_id, node_id, reason, hitl_mode,
                 decision, human_input, user_id, owner_user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                thread_id, workflow_id, node_id, reason, hitl_mode,
                decision, human_input or "", user_id, owner_user_id,
            ),
        )

    # ---------------- FR-T0-3: durable replay (run_steps / run_events) ----

    async def save_run_step(
        self,
        thread_id: str,
        workflow_id: str,
        step_index: int,
        node_id: str,
        node_type: str,
        status: str,
        *,
        attempt: int = 0,
        input_snapshot: Optional[Dict[str, Any]] = None,
        output_ref: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ) -> None:
        """Upsert authoritative per-step state (REQ-D1). Keyed
        (thread_id, step_index) so the same step re-writes in place across
        attempts. input_snapshot enables deterministic re-drive on resume.
        """
        await self._insert(
            """
            INSERT INTO run_steps
                (thread_id, workflow_id, step_index, node_id, node_type,
                 status, attempt, input_snapshot, output_ref, idempotency_key,
                 updated_at, owner_user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
            ON CONFLICT (thread_id, step_index) DO UPDATE
                SET node_id         = EXCLUDED.node_id,
                    node_type       = EXCLUDED.node_type,
                    status          = EXCLUDED.status,
                    attempt         = EXCLUDED.attempt,
                    input_snapshot  = EXCLUDED.input_snapshot,
                    output_ref      = COALESCE(EXCLUDED.output_ref, run_steps.output_ref),
                    idempotency_key = COALESCE(EXCLUDED.idempotency_key, run_steps.idempotency_key),
                    updated_at      = NOW(),
                    owner_user_id   = COALESCE(run_steps.owner_user_id, EXCLUDED.owner_user_id)
            """,
            (
                thread_id, workflow_id, step_index, node_id, node_type,
                status, attempt,
                json.dumps(input_snapshot or {}, default=str),
                output_ref, idempotency_key, owner_user_id,
            ),
        )

    async def load_run_state(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Load all steps for a run ordered by step_index (REQ-D1/D3/D5).

        Used by /resume-stream and crash recovery to find the last completed
        step and re-drive downstream from each step's input_snapshot.

        ``owner_user_id`` scopes the read to the caller's own run. Pass it
        from any request-handling path: ``input_snapshot`` contains verbatim
        node input, so an unscoped read keyed only on a client-supplied
        thread_id would be a cross-tenant disclosure.
        """
        def _run():
            owner_sql, params = _owner_scope_sql(owner_user_id, (thread_id,))
            with self._require_pool().connection() as conn:
                rows = conn.execute(
                    f"""
                    SELECT step_index, node_id, node_type, status, attempt,
                           input_snapshot, output_ref, idempotency_key
                    FROM run_steps
                    WHERE thread_id = %s{owner_sql}
                    ORDER BY step_index ASC
                    """,
                    params,
                ).fetchall()
            return rows
        rows = await asyncio.to_thread(_run)
        return [
            {
                "step_index":      r[0],
                "node_id":         r[1],
                "node_type":       r[2],
                "status":          r[3],
                "attempt":         r[4],
                "input_snapshot":  r[5] or {},
                "output_ref":      r[6],
                "idempotency_key": r[7],
            }
            for r in (rows or [])
        ]

    async def append_run_event(
        self,
        thread_id: str,
        workflow_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        step_index: Optional[int] = None,
        owner_user_id: Optional[str] = None,
    ) -> None:
        """Append one row to the ordered event log (REQ-D2)."""
        await self._insert(
            """
            INSERT INTO run_events
                (thread_id, workflow_id, step_index, event_type, payload,
                 owner_user_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                thread_id, workflow_id, step_index, event_type,
                json.dumps(payload or {}, default=str), owner_user_id,
            ),
        )

    async def replay_events(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return the full ordered event log for a run (REQ-D2/D3).

        Deterministic replay: rows come back in (step_index, id) order so the
        original routing decisions can be reproduced exactly.

        ``owner_user_id`` scopes the read to the caller's own run — see
        ``load_run_state``. Event payloads embed node output, so any future
        HTTP replay endpoint MUST pass the authenticated user's id here.
        """
        def _run():
            owner_sql, params = _owner_scope_sql(owner_user_id, (thread_id,))
            with self._require_pool().connection() as conn:
                rows = conn.execute(
                    f"""
                    SELECT step_index, event_type, payload, created_at
                    FROM run_events
                    WHERE thread_id = %s{owner_sql}
                    ORDER BY step_index ASC NULLS FIRST, id ASC
                    """,
                    params,
                ).fetchall()
            return rows
        rows = await asyncio.to_thread(_run)
        return [
            {
                "step_index": r[0],
                "event_type": r[1],
                "payload":    r[2] or {},
                "created_at": r[3].isoformat() if r[3] else None,
            }
            for r in (rows or [])
        ]
