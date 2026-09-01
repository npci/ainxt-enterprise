# SPDX-License-Identifier: Apache-2.0
"""Schema bootstrap + one-time owner migrations for the Postgres checkpoint store.

Extracted from ``postgres_store.py``, where ``_init_pool`` had grown to a single
285-line method interleaving eight ``CREATE TABLE`` blocks, fourteen
``CREATE INDEX`` statements, and the owner-column migrations — making the DDL
effectively unreviewable in a file that is otherwise runtime query logic.

Only the *schema* moved. The store's query methods stay on
``PostgresCheckpointStore`` because they implement the ``CheckpointStore`` ABC
and are dispatched polymorphically by the engine; relocating them would force a
parallel set of delegating stubs and a third copy of every signature.

Ordering is load-bearing
------------------------
``ensure_schema`` runs everything on ONE connection inside ONE transaction, in a
fixed order, and the caller commits:

  1. every ``CREATE TABLE`` / ``CREATE INDEX``;
  2. the per-table owner migrations for the chat-layer tables, each backfilling
     from its own natural parent;
  3. LAST, the execution-layer owner migrations — these must follow every
     ``CREATE TABLE`` above, since a migration against a not-yet-created table
     would fail.

Do not reorder these phases.
"""
from __future__ import annotations

# Execution-layer tables carrying substantive run content (agent output
# previews, input snapshots, evaluated routing state, event payloads), now
# owner-scoped exactly like chat_threads. All five have a NOT NULL
# workflow_id, so they backfill from the owning workflow row — the same
# "a thread and its workflow share an owner by construction" assumption
# chat_threads' own backfill relies on. Backfilling from workflows rather
# than chat_threads also recovers an owner for rows orphaned by the
# pre-fix delete_thread cascade (see delete_thread).
EXECUTION_OWNED_TABLES = (
    "loop_iterations",
    "condition_routings",
    "hitl_decisions",
    "run_steps",
    "run_events",
)

# Tables the delete cascades must clear alongside a chat thread. Kept here, next
# to the DDL that creates them, so adding a thread-keyed table is a single-site
# change: miss it and deleting a conversation silently leaves the user's prompts
# and agent outputs behind (the data-erasure gap fixed in d63926d). Every table
# listed is keyed by thread_id and indexed on it (or on (thread_id, …)).
THREAD_DEPENDENT_TABLES = (
    "pending_interrupts",
    "chat_thread_node_outputs",
    "loop_iterations",
    "condition_routings",
    "hitl_decisions",
    "run_steps",
    "run_events",
)


def migrate_owner_columns(conn, table: str, *, backfill: str) -> None:
    """Add ``owner_user_id`` + ``legacy_no_owner`` to ``table`` and, on the
    FIRST run against a given database only, backfill owners and stamp the
    rows that were genuinely ownerless at that moment.

    This is the one-time-migration pattern ``chat_threads`` established
    (security review F-06/F-10); it is factored out here because eight tables
    now need it, and eight hand-copied variants is how one of them eventually
    drifts. Semantics are identical to the original inline blocks:

      - Absence of the ``owner_user_id`` column is what marks a first run.
        Keying off the column rather than a flag table keeps the migration
        idempotent across restarts with no extra bookkeeping.
      - ``legacy_no_owner`` is stamped TRUE exactly once, only for rows that
        were already NULL-owned at migration time. Every row written after
        that gets FALSE by column default, so a NULL owner arising from a
        future bug is DENIED by ``_owner_scope_sql`` rather than silently
        treated as public. That bounds the fail-open to a one-time migration
        artifact instead of a permanent hole.

    ``table`` is always a module-local literal (never caller/user input), so
    interpolating it into DDL is safe; values stay parameterised.
    """
    already_migrated = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = 'owner_user_id'",
        (table,),
    ).fetchone()
    conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS owner_user_id TEXT")
    conn.execute(
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS legacy_no_owner "
        "BOOLEAN NOT NULL DEFAULT FALSE"
    )
    if already_migrated is None:
        conn.execute(backfill)
        conn.execute(
            f"UPDATE {table} SET legacy_no_owner = TRUE WHERE owner_user_id IS NULL"
        )


def ensure_schema(conn) -> None:
    """Create every table/index the store needs and run the owner migrations.

    Idempotent: safe to run on every startup. Executes on the caller's
    connection and does NOT commit — the caller owns the transaction boundary
    so schema creation and migration land atomically.
    """
    _create_chat_threads(conn)
    _create_pending_interrupts(conn)
    _create_node_outputs(conn)
    _create_loop_iterations(conn)
    _create_loop_lessons(conn)
    _create_condition_routings(conn)
    _create_hitl_decisions(conn)
    _create_run_steps(conn)
    _create_run_events(conn)
    _migrate_execution_tables(conn)


# ── Chat layer ───────────────────────────────────────────────────────────────

def _create_chat_threads(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_threads (
            thread_id    TEXT PRIMARY KEY,
            workflow_id  TEXT NOT NULL,
            messages     JSONB NOT NULL DEFAULT '[]',
            last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    # Security review F-06/F-10: chat_threads had no ownership column,
    # so any caller who knew/guessed a thread_id could read, delete,
    # or resume another user's thread.
    #
    # ``owner_user_id`` is backfilled from the owning workflow row
    # (chat threads and their workflow share the same owner by
    # construction). Rows for a workflow_id that no longer resolves
    # (deleted workflow) stay NULL after backfill.
    #
    # ``legacy_no_owner`` (follow-up hardening) bounds how long a
    # NULL owner is treated as "accessible" instead of "denied".
    # Without it, the NULL-is-accessible rule in every store method
    # would be a PERMANENT fail-open: any row that is NULL for
    # any reason — including a future bug that forgets to pass
    # owner_user_id on write — would stay globally readable forever,
    # indistinguishable from a legitimate pre-migration row.
    # ``migrate_owner_columns`` implements the one-time stamp; see
    # its docstring for the full rationale.
    migrate_owner_columns(
        conn, "chat_threads",
        backfill="""
            UPDATE chat_threads t
            SET owner_user_id = w.owner_user_id
            FROM workflows w
            WHERE t.workflow_id = w.id
              AND t.owner_user_id IS NULL
              AND w.owner_user_id IS NOT NULL
        """,
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_threads_workflow ON chat_threads(workflow_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_threads_owner ON chat_threads(owner_user_id)")


def _create_pending_interrupts(conn) -> None:
    # HITL pending interrupts. Separate table so chat history can be
    # written without locking the snapshot row, and so a thread can
    # exist with messages but no pending interrupt (the common case).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_interrupts (
            thread_id  TEXT PRIMARY KEY,
            snapshot   JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    # owner_user_id / legacy_no_owner here are denormalised from
    # chat_threads (rather than joined at read time) so load/delete
    # stay single-table lookups on the hot HITL resume path.
    migrate_owner_columns(
        conn, "pending_interrupts",
        backfill="""
            UPDATE pending_interrupts pi
            SET owner_user_id = t.owner_user_id
            FROM chat_threads t
            WHERE pi.thread_id = t.thread_id
              AND pi.owner_user_id IS NULL
              AND t.owner_user_id IS NOT NULL
        """,
    )


def _create_node_outputs(conn) -> None:
    # Per-node last outputs. Composite primary key so a single
    # statement upserts (thread_id, node_id). Powers the Loop node's
    # connection-aware list picker — the frontend reads the upstream
    # node's last output to render click-to-pick options instead of
    # demanding a typed dotted path.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_thread_node_outputs (
            thread_id   TEXT NOT NULL,
            node_id     TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            agent       TEXT NOT NULL,
            output      TEXT NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (thread_id, node_id)
        )
    """)
    migrate_owner_columns(
        conn, "chat_thread_node_outputs",
        backfill="""
            UPDATE chat_thread_node_outputs n
            SET owner_user_id = w.owner_user_id
            FROM workflows w
            WHERE n.workflow_id = w.id
              AND n.owner_user_id IS NULL
              AND w.owner_user_id IS NOT NULL
        """,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_node_outputs_thread "
        "ON chat_thread_node_outputs(thread_id)"
    )
    # workflow_id indexes back the per-workflow cascade delete
    # (delete_threads_for_workflow). The audit tables below are
    # append-only and grow unbounded, so without these the cascade
    # would full-scan them.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_node_outputs_workflow "
        "ON chat_thread_node_outputs(workflow_id)"
    )


# ── Execution layer ──────────────────────────────────────────────────────────

def _create_loop_iterations(conn) -> None:
    # Per-iteration diagnostics for Loop nodes. The engine emits
    # loop_iteration_start / loop_iteration_summary / loop_condition_eval
    # SSE events during a run; this table is a best-effort persistent
    # record of those so loop traces survive reload. Append-only; one
    # row per iteration. case_results stores the per-case match shape
    # used by while-mode condition evaluation.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS loop_iterations (
            id            BIGSERIAL PRIMARY KEY,
            thread_id     TEXT NOT NULL,
            workflow_id   TEXT NOT NULL,
            node_id       TEXT NOT NULL,
            iteration     INT NOT NULL,
            mode          TEXT NOT NULL,
            total         INT,
            score         DOUBLE PRECISION,
            changes       TEXT,
            will_continue BOOLEAN,
            case_results  JSONB NOT NULL DEFAULT '[]',
            output_preview TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_loop_iterations_thread_node "
        "ON loop_iterations(thread_id, node_id, iteration)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_loop_iterations_workflow "
        "ON loop_iterations(workflow_id)"
    )


def _create_loop_lessons(conn) -> None:
    # Loop cross-run memory. When a Loop node has memory.write enabled,
    # the engine persists a compact reflection ("lesson") after each
    # run keyed by (workflow_id, node_id). A later run of the SAME loop
    # with memory.read enabled fetches recent lessons and injects them
    # into the body agents' prompt via {{loop.prior_lessons}}.
    # Append-only; one row per run.
    #
    # NOTE: deliberately NOT in EXECUTION_OWNED_TABLES — lessons are keyed by
    # (workflow_id, node_id) with no thread_id, are cross-run by design, and
    # hold a distilled digest rather than verbatim user content.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS loop_lessons (
            id          BIGSERIAL PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            node_id     TEXT NOT NULL,
            digest      TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_loop_lessons_wf_node "
        "ON loop_lessons(workflow_id, node_id, created_at DESC)"
    )


def _create_condition_routings(conn) -> None:
    # Condition routing audit. Records the matched case + branch
    # target each time a ConditionNode is evaluated during a thread.
    # Append-only; one row per evaluation.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS condition_routings (
            id                      BIGSERIAL PRIMARY KEY,
            thread_id               TEXT NOT NULL,
            workflow_id             TEXT NOT NULL,
            node_id                 TEXT NOT NULL,
            matched_case_id         TEXT,
            matched_label           TEXT,
            matched_expression      TEXT,
            upstream_output_preview TEXT,
            evaluated_state         JSONB NOT NULL DEFAULT '{}',
            target_node_id          TEXT,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_condition_routings_thread_node "
        "ON condition_routings(thread_id, node_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_condition_routings_workflow "
        "ON condition_routings(workflow_id)"
    )


def _create_hitl_decisions(conn) -> None:
    # HITL decision audit. ``pending_interrupts`` stores the in-flight
    # snapshot (cleared on resume); this table is the durable record
    # of every resume so an admin can reconstruct who approved /
    # rejected / edited which interrupt and when.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hitl_decisions (
            id           BIGSERIAL PRIMARY KEY,
            thread_id    TEXT NOT NULL,
            workflow_id  TEXT NOT NULL,
            node_id      TEXT NOT NULL,
            reason       TEXT NOT NULL,
            hitl_mode    TEXT NOT NULL DEFAULT '',
            decision     TEXT NOT NULL,
            human_input  TEXT NOT NULL DEFAULT '',
            user_id      TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hitl_decisions_thread "
        "ON hitl_decisions(thread_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hitl_decisions_workflow "
        "ON hitl_decisions(workflow_id)"
    )


def _create_run_steps(conn) -> None:
    # ── FR-T0-3 (REQ-D1): authoritative per-run / per-step state ──
    # One upserted row per executed step, keyed (thread_id, step_index).
    # input_snapshot enables deterministic re-drive on resume/crash;
    # output_ref points at chat_thread_node_outputs for the body.
    # idempotency_key (REQ-D4) dedupes side-effecting node replays.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_steps (
            thread_id       TEXT NOT NULL,
            workflow_id     TEXT NOT NULL,
            step_index      INT  NOT NULL,
            node_id         TEXT NOT NULL,
            node_type       TEXT NOT NULL,
            status          TEXT NOT NULL,
            attempt         INT  NOT NULL DEFAULT 0,
            input_snapshot  JSONB NOT NULL DEFAULT '{}',
            output_ref      TEXT,
            idempotency_key TEXT,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (thread_id, step_index)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_steps_workflow "
        "ON run_steps(workflow_id)"
    )


def _create_run_events(conn) -> None:
    # ── FR-T0-3 (REQ-D2): append-only ordered event log ──────────
    # One row per emitted SSE event; replay reproduces routing exactly.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_events (
            id          BIGSERIAL PRIMARY KEY,
            thread_id   TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            step_index  INT,
            event_type  TEXT NOT NULL,
            payload     JSONB NOT NULL DEFAULT '{}',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_events_thread "
        "ON run_events(thread_id, step_index, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_events_workflow "
        "ON run_events(workflow_id)"
    )


def _migrate_execution_tables(conn) -> None:
    """Owner-scope the five execution-layer tables.

    MUST run after every ``CREATE TABLE`` above — a migration against a
    not-yet-created table would fail.
    """
    # Security review (execution-layer tenant isolation): the five
    # tables above record substantive run content — output previews,
    # input snapshots, evaluated routing state, event payloads — but
    # originally had no ownership column. Nothing reads them over HTTP
    # today (replay_events / load_run_state have no API callers yet),
    # so this closes the gap BEFORE a replay endpoint makes it
    # reachable, rather than after.
    for _table in EXECUTION_OWNED_TABLES:
        migrate_owner_columns(
            conn, _table,
            backfill=f"""
                UPDATE {_table} x
                SET owner_user_id = w.owner_user_id
                FROM workflows w
                WHERE x.workflow_id = w.id
                  AND x.owner_user_id IS NULL
                  AND w.owner_user_id IS NOT NULL
            """,
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_table}_owner "
            f"ON {_table}(owner_user_id)"
        )
