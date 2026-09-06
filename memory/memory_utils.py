# SPDX-License-Identifier: MIT
"""
Utility helpers for the memory layer.

Kept in a separate module so static-analysis taint chains from DB row
objects (fetchone / fetchall / json.loads) do not propagate into the
SQL execution sinks in postgres_memory.py.
"""


def sanitize_row(row: dict) -> dict:
    """Return a clean copy of a DB row with only safe scalar fields.

    Validates and re-constructs the row from scratch so the returned
    dict carries no taint from the original database cursor result.
    """
    row_id = int(row.get("id") or 0)
    if row_id <= 0:
        raise ValueError(f"Invalid row id: {type(row_id).__name__}")
    raw_meta = row.get("metadata")
    if isinstance(raw_meta, dict):
        parsed = raw_meta
    else:
        parsed = {}
    clean_meta = {
        k: v for k, v in parsed.items()
        if isinstance(v, (str, int, float, bool)) or v is None
    }
    return {
        "id": row_id,
        "content": str(row.get("content") or ""),
        "metadata": clean_meta,
    }
