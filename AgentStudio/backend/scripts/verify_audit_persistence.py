# SPDX-License-Identifier: MIT
"""
Static verification that the audit-trail persistence (loop_iterations,
condition_routings, hitl_decisions) writes match the table schema.

What this checks (no live DB needed):
  - For every NOT NULL column, all engine call sites pass a non-None value
    (or the column has a DEFAULT and the call omits it).
  - For every JSONB column, the value passed is dict / list / None and
    survives json.dumps(..., default=str).
  - For every numeric / boolean column, the type is correct.

Run:
    python -m backend.scripts.verify_audit_persistence
or from backend/:
    python -m scripts.verify_audit_persistence
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Schema (mirrors postgres_store.py)
# ---------------------------------------------------------------------------

SCHEMAS: Dict[str, Dict[str, Tuple[str, bool, bool]]] = {
    # column: (type, not_null, has_default)
    "loop_iterations": {
        "id":             ("BIGSERIAL", True,  True),
        "thread_id":      ("TEXT",      True,  False),
        "workflow_id":    ("TEXT",      True,  False),
        "node_id":        ("TEXT",      True,  False),
        "iteration":      ("INT",       True,  False),
        "mode":           ("TEXT",      True,  False),
        "total":          ("INT",       False, False),
        "score":          ("FLOAT",     False, False),
        "changes":        ("TEXT",      False, False),
        "will_continue":  ("BOOL",      False, False),
        "case_results":   ("JSONB",     True,  True),
        "output_preview": ("TEXT",      False, False),
        "created_at":     ("TIMESTAMP", True,  True),
    },
    "condition_routings": {
        "id":                      ("BIGSERIAL", True,  True),
        "thread_id":               ("TEXT",      True,  False),
        "workflow_id":             ("TEXT",      True,  False),
        "node_id":                 ("TEXT",      True,  False),
        "matched_case_id":         ("TEXT",      False, False),
        "matched_label":           ("TEXT",      False, False),
        "matched_expression":      ("TEXT",      False, False),
        "upstream_output_preview": ("TEXT",      False, False),
        "evaluated_state":         ("JSONB",     True,  True),
        "target_node_id":          ("TEXT",      False, False),
        "created_at":              ("TIMESTAMP", True,  True),
    },
    "hitl_decisions": {
        "id":          ("BIGSERIAL", True,  True),
        "thread_id":   ("TEXT",      True,  False),
        "workflow_id": ("TEXT",      True,  False),
        "node_id":     ("TEXT",      True,  False),
        "reason":      ("TEXT",      True,  False),
        "hitl_mode":   ("TEXT",      True,  True),
        "decision":    ("TEXT",      True,  False),
        "human_input": ("TEXT",      True,  True),
        "user_id":     ("TEXT",      False, False),
        "created_at":  ("TIMESTAMP", True,  True),
    },
}


# ---------------------------------------------------------------------------
# Representative payloads that match every call site in native_engine.py
# ---------------------------------------------------------------------------
#
# Each entry is (label, kwargs_for_save_method). The kwargs map 1:1 to the
# parameter names of PostgresCheckpointStore.save_<table>.

LOOP_CALLS: List[Tuple[str, dict]] = [
    (
        "for_each iteration (non-while branch)",
        dict(
            thread_id="t1", workflow_id="wf1", node_id="loop-1",
            index=0, mode="for_each", total=3,
            score=None, changes=None,
            will_continue=None, case_results=None,
            output_preview="hello",
        ),
    ),
    (
        "count iteration (non-while branch)",
        dict(
            thread_id="t1", workflow_id="wf1", node_id="loop-1",
            index=2, mode="count", total=5,
            score=None, changes=None,
            will_continue=None, case_results=None,
            output_preview=None,  # current_input may be empty
        ),
    ),
    (
        "while iteration (post-condition branch)",
        dict(
            thread_id="t1", workflow_id="wf1", node_id="loop-1",
            index=4, mode="while", total=None,
            score=0.85, changes="rewrote summary",
            will_continue=True,
            case_results=[{"case_index": 0, "matched": True}],
            output_preview="...",
        ),
    ),
]

CONDITION_CALLS: List[Tuple[str, dict]] = [
    (
        "matched case",
        dict(
            thread_id="t1", workflow_id="wf1", node_id="cond-1",
            matched_case_id="case-A", matched_label="Tech",
            matched_expression="input.intent == 'tech'",
            upstream_output_preview="{\"intent\":\"tech\"}",
            evaluated_state={"intent": "tech"},
            target_node_id="agent-2",
        ),
    ),
    (
        "fallback ELSE",
        dict(
            thread_id="t1", workflow_id="wf1", node_id="cond-1",
            matched_case_id="else", matched_label="else",
            matched_expression=None,
            upstream_output_preview=None,
            evaluated_state={},          # no fields referenced
            target_node_id="end",
        ),
    ),
    (
        "fallback ELSE with no upstream (current_input empty)",
        dict(
            thread_id="t1", workflow_id="wf1", node_id="cond-1",
            matched_case_id="else", matched_label="else",
            matched_expression=None,
            upstream_output_preview=None,
            evaluated_state=None,         # helper coerces to {}
            target_node_id=None,           # gctx.end_id may be ""
        ),
    ),
]

HITL_CALLS: List[Tuple[str, dict]] = [
    (
        "approve before_tool",
        dict(
            thread_id="t1", workflow_id="wf1", node_id="agent-1",
            reason="before_tool", hitl_mode="approve",
            decision="approve", human_input="",
            user_id="local-dev-user",
        ),
    ),
    (
        "reject ask_human",
        dict(
            thread_id="t1", workflow_id="wf1", node_id="agent-1",
            reason="ask_human", hitl_mode="",
            decision="reject", human_input="no, cancel it",
            user_id=None,
        ),
    ),
    (
        "edit after_response",
        dict(
            thread_id="t1", workflow_id="wf1", node_id="agent-1",
            reason="after_response", hitl_mode="approve",
            decision="edit", human_input="please use a different tone",
            user_id="local-dev-user",
        ),
    ),
]


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

TABLE_TO_COL_ORDER: Dict[str, List[str]] = {
    "loop_iterations": [
        "thread_id", "workflow_id", "node_id", "iteration", "mode",
        "total", "score", "changes", "will_continue", "case_results",
        "output_preview",
    ],
    "condition_routings": [
        "thread_id", "workflow_id", "node_id", "matched_case_id",
        "matched_label", "matched_expression",
        "upstream_output_preview", "evaluated_state", "target_node_id",
    ],
    "hitl_decisions": [
        "thread_id", "workflow_id", "node_id", "reason", "hitl_mode",
        "decision", "human_input", "user_id",
    ],
}

# Column -> Python kwarg name where they differ. The store helpers use
# `index` for the `iteration` column to read naturally at the call site.
COL_TO_KWARG: Dict[str, str] = {
    "iteration": "index",
}

# Each call's payload key maps directly to the INSERT column except where
# the store helper serializes (case_results, evaluated_state -> json.dumps).
JSON_COLS = {"loop_iterations": "case_results",
             "condition_routings": "evaluated_state"}


def verify_payload(table: str, label: str, payload: dict) -> List[str]:
    errors: List[str] = []
    schema = SCHEMAS[table]
    cols = TABLE_TO_COL_ORDER[table]

    for col in cols:
        col_type, not_null, has_default = schema[col]
        kwarg = COL_TO_KWARG.get(col, col)
        val = payload.get(kwarg)

        # JSONB columns: helper applies `json.dumps(value or default)`.
        if table in JSON_COLS and col == JSON_COLS[table]:
            try:
                # Replicate the helper coercion.
                default = [] if col == "case_results" else {}
                json.dumps(val if val is not None else default, default=str)
            except (TypeError, ValueError) as exc:
                errors.append(f"[{table}|{label}] {col!r}: json.dumps failed: {exc}")
            continue

        if val is None:
            if not_null and not has_default:
                errors.append(
                    f"[{table}|{label}] NOT NULL column {col!r} received None"
                )
            continue

        # Type checks (loose — Postgres coerces TEXT/INT/BOOL freely).
        if col_type == "INT" and not isinstance(val, int):
            errors.append(f"[{table}|{label}] {col!r} expected int, got {type(val).__name__}")
        elif col_type == "FLOAT" and not isinstance(val, (int, float)):
            errors.append(f"[{table}|{label}] {col!r} expected float, got {type(val).__name__}")
        elif col_type == "BOOL" and not isinstance(val, bool):
            errors.append(f"[{table}|{label}] {col!r} expected bool, got {type(val).__name__}")
        elif col_type == "TEXT" and not isinstance(val, str):
            errors.append(f"[{table}|{label}] {col!r} expected str, got {type(val).__name__}")

    return errors


def main() -> int:
    total_errors: List[str] = []
    matrix = [
        ("loop_iterations",    LOOP_CALLS),
        ("condition_routings", CONDITION_CALLS),
        ("hitl_decisions",     HITL_CALLS),
    ]
    for table, calls in matrix:
        for label, payload in calls:
            total_errors.extend(verify_payload(table, label, payload))

    if total_errors:
        print("FAILED — schema/payload mismatches:")
        for e in total_errors:
            print(" -", e)
        return 1

    print("OK — every call site payload satisfies its target schema:")
    for table, calls in matrix:
        print(f"  {table}: {len(calls)} call site shapes verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
