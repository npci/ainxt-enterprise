# SPDX-License-Identifier: MIT
# ============================================================
# workers/thread_purge.py KV migration test.
#
# Covers Gap #8 — _write_run_record now uses get_kv(RDB_TRACE)
# instead of the legacy core.config.redis_client(db=1).
#
# Pure dependency-injection check — no real Redis required.
# ============================================================

from __future__ import annotations

import pytest


class _StubKV:
    """Records setex/expire calls so the test can assert on them."""

    def __init__(self):
        self.setex_calls: list[tuple[str, int, str]] = []

    def setex(self, key: str, ttl: int, value: str) -> bool:
        self.setex_calls.append((key, ttl, value))
        return True


def test_write_run_record_calls_get_kv_with_rdb_trace(monkeypatch):
    """_write_run_record must route through core.kv.get_kv(RDB_TRACE)."""
    stub = _StubKV()
    captured_args = {}

    def _fake_get_kv(db, *args, **kwargs):
        captured_args["db"] = db
        captured_args["decode_responses"] = kwargs.get("decode_responses")
        return stub

    # Patch get_kv in core.kv (the public re-export).
    import core.kv as _ckv
    monkeypatch.setattr(_ckv, "get_kv", _fake_get_kv)

    from workers.thread_purge import _write_run_record
    _write_run_record({"deleted": 17, "kept": 42})

    from core.config import RDB_TRACE
    assert captured_args["db"] == RDB_TRACE
    assert captured_args["decode_responses"] is True

    # setex called once with thread_purge:* key and 30-day TTL.
    assert len(stub.setex_calls) == 1
    key, ttl, value = stub.setex_calls[0]
    assert key.startswith("thread_purge:")
    assert ttl == 86400 * 30
    assert '"deleted": 17' in value or "'deleted': 17" in value
