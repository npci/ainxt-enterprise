# SPDX-License-Identifier: Apache-2.0
# ============================================================
# core/trace_store.py KV-contract tests (Phase 4).
#
# Risk closed: rpush + expire ordering produces the wrong list view
# on RC, delete drops a different key family, or get_trace fails on
# a corrupt entry. All tested explicitly against both backends.
#
# trace_store binds its KV singleton at import time
# (`redis_client = get_kv(RDB_TRACE, decode_responses=True)`) so we
# monkeypatch trace_store.redis_client with the test fixture.
# ============================================================

from __future__ import annotations

import uuid

import pytest

from core import trace_store as _ts


@pytest.fixture
def patched_trace(monkeypatch, kv):
    """Repoint trace_store.redis_client at the test KV (DB9)."""
    monkeypatch.setattr(_ts, "redis_client", kv)
    return kv


def _rid() -> str:
    return f"req-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# add / get round trip
# ---------------------------------------------------------------------------

def test_add_trace_appends_entries_in_order(patched_trace):
    rid = _rid()
    _ts.add_trace(rid, "step-1")
    _ts.add_trace(rid, "step-2")
    _ts.add_trace(rid, "step-3")
    entries = _ts.get_trace(rid)
    assert len(entries) == 3
    assert [e["message"] for e in entries] == ["step-1", "step-2", "step-3"]
    # Each entry must carry a timestamp.
    assert all("timestamp" in e for e in entries)


def test_add_trace_sets_24h_ttl(patched_trace):
    rid = _rid()
    _ts.add_trace(rid, "single")
    ttl = patched_trace.ttl(f"trace:{rid}")
    assert 0 < ttl <= 86400
    # Within a minute of the full 24h window.
    assert ttl >= 86400 - 60


def test_get_trace_missing_returns_empty_list(patched_trace):
    assert _ts.get_trace("does-not-exist-" + uuid.uuid4().hex) == []


def test_delete_trace_removes_key(patched_trace):
    rid = _rid()
    _ts.add_trace(rid, "step")
    assert _ts.get_trace(rid) != []
    _ts.delete_trace(rid)
    assert _ts.get_trace(rid) == []


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------

def test_add_trace_swallows_empty_request_id(patched_trace):
    """add_trace must be a no-op (no crash) when request_id is empty."""
    _ts.add_trace("", "ignored")
    # No key created.
    assert patched_trace.keys("trace:") == []


def test_get_trace_returns_empty_when_corrupt_entry(patched_trace):
    """If an invalid JSON blob lands in the list, get_trace returns []
    (the module catches the exception and logs)."""
    rid = _rid()
    # Bypass add_trace and write garbage directly.
    patched_trace.rpush(f"trace:{rid}", "not-json-{}")
    result = _ts.get_trace(rid)
    # Either empty or contains the JSONDecodeError-handled fallback —
    # current implementation logs and returns [].
    assert result == []
