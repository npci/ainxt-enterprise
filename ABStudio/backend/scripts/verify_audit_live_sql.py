# SPDX-License-Identifier: Apache-2.0
"""
Live SQL verification: monkey-patches PostgresCheckpointStore's connection
pool with an in-memory fake to capture the exact (SQL, params) tuples
emitted by every save_* method. Asserts:

  - column count matches placeholder count
  - JSONB params are JSON-serializable strings
  - NOT NULL columns receive non-None values for the call-site shapes
    actually used by the engine

Run from backend/:
    python -m scripts.verify_audit_live_sql
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any, List, Tuple


class _FakeConn:
    def __init__(self, sink: list):
        self._sink = sink

    def execute(self, sql: str, params: tuple = ()) -> "_FakeConn":
        self._sink.append((sql, params))
        return self

    def commit(self) -> None:
        pass

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    # Context-manager protocol so `with pool.connection() as conn:` works.
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakePool:
    def __init__(self):
        self.statements: List[Tuple[str, tuple]] = []

    def connection(self):
        return _FakeConn(self.statements)

    def close(self):
        pass


def _placeholder_count(sql: str) -> int:
    return len(re.findall(r"%s", sql))


def _column_list(sql: str) -> List[str]:
    m = re.search(r"INSERT INTO\s+\w+\s*\(([^)]+)\)", sql, re.IGNORECASE)
    if not m:
        return []
    return [c.strip() for c in m.group(1).split(",")]


NOT_NULL = {
    "loop_iterations": {
        "thread_id", "workflow_id", "node_id", "iteration", "mode",
        "case_results",
    },
    "condition_routings": {
        "thread_id", "workflow_id", "node_id", "evaluated_state",
    },
    "hitl_decisions": {
        "thread_id", "workflow_id", "node_id", "reason", "hitl_mode",
        "decision", "human_input",
    },
}

JSONB = {
    "loop_iterations": "case_results",
    "condition_routings": "evaluated_state",
}


async def run() -> int:
    # Defer imports until after sys.path setup.
    sys.path.insert(0, ".")
    from app.checkpoint.postgres_store import PostgresCheckpointStore

    store = PostgresCheckpointStore(uri="postgresql://fake")
    store._pool = _FakePool()

    # --- loop iterations -----------------------------------------------
    await store.save_loop_iteration(
        "t1", "wf1", "loop-1", index=0, mode="for_each", total=3,
        score=None, changes=None, will_continue=None,
        case_results=None, output_preview="hi",
    )
    await store.save_loop_iteration(
        "t1", "wf1", "loop-1", index=2, mode="while", total=None,
        score=0.91, changes="rewrote", will_continue=True,
        case_results=[{"case_index": 0, "matched": True}],
        output_preview="...",
    )

    # --- condition routings --------------------------------------------
    await store.save_condition_routing(
        "t1", "wf1", "cond-1",
        matched_case_id="case-A", matched_label="Tech",
        matched_expression="input.intent == 'tech'",
        upstream_output_preview="{\"intent\":\"tech\"}",
        evaluated_state={"intent": "tech"},
        target_node_id="agent-2",
    )
    await store.save_condition_routing(
        "t1", "wf1", "cond-1",
        matched_case_id="else", matched_label="else",
        matched_expression=None, upstream_output_preview=None,
        evaluated_state=None, target_node_id="end",
    )

    # --- hitl decisions ------------------------------------------------
    await store.save_hitl_decision(
        "t1", "wf1", "agent-1",
        reason="before_tool", hitl_mode="approve",
        decision="approve", human_input="",
        user_id="local-dev-user",
    )
    await store.save_hitl_decision(
        "t1", "wf1", "agent-1",
        reason="ask_human", hitl_mode="",
        decision="reject", human_input="no, cancel",
        user_id=None,
    )

    errors: List[str] = []
    for sql, params in store._pool.statements:
        cols = _column_list(sql)
        placeholders = _placeholder_count(sql)
        if placeholders != len(params):
            errors.append(
                f"placeholder count {placeholders} != params {len(params)}\n"
                f"SQL: {sql.strip()[:120]}..."
            )
            continue
        if placeholders != len(cols):
            errors.append(
                f"column count {len(cols)} != placeholder count {placeholders}\n"
                f"SQL: {sql.strip()[:120]}..."
            )
            continue

        table_match = re.search(r"INSERT INTO\s+(\w+)", sql, re.IGNORECASE)
        if not table_match:
            continue
        table = table_match.group(1)

        for col, val in zip(cols, params):
            if table in NOT_NULL and col in NOT_NULL[table] and val is None:
                errors.append(
                    f"[{table}] NOT NULL column {col!r} got None — "
                    f"would fail at insert time."
                )
            if table in JSONB and col == JSONB[table]:
                if not isinstance(val, str):
                    errors.append(
                        f"[{table}] JSONB column {col!r} not a JSON string "
                        f"(got {type(val).__name__}: {val!r})"
                    )
                else:
                    try:
                        json.loads(val)
                    except Exception as e:
                        errors.append(
                            f"[{table}] JSONB column {col!r} not valid JSON: {e}"
                        )

    if errors:
        print("FAILED — live SQL verification:")
        for e in errors:
            print(" -", e)
        return 1

    print(f"OK — {len(store._pool.statements)} INSERTs verified across 3 tables.")
    for sql, params in store._pool.statements:
        table = re.search(r"INSERT INTO\s+(\w+)", sql).group(1)
        print(f"  {table}: {len(params)} params, JSONB={table in JSONB}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
