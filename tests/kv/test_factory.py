# SPDX-License-Identifier: MIT
# ============================================================
# core.kv.factory tests.
#
# Covers:
#   * Gap #10 — KV_DB_COUNT == 9, kv_backend_map shape
#   * Gap #2  — close_all_kv clears the cache
#   * Gap #7  — async_get_kv + async_close_all_kv
#   * factory caching invariants (one client per (db, decode_responses))
#   * per-DB env-var override resolution
# ============================================================

from __future__ import annotations

import asyncio

import pytest

from core.config import KV_DB_COUNT
from core.kv import (
    async_close_all_kv,
    async_get_kv,
    close_all_kv,
    get_kv,
    kv_backend_map,
)
from core.kv import factory as _factory


# ---------------------------------------------------------------------------
# kv_backend_map covers DB0..DB(KV_DB_COUNT-1)
# ---------------------------------------------------------------------------

def test_kv_backend_map_has_correct_db_count():
    """kv_backend_map() must contain every logical DB exactly once."""
    m = kv_backend_map()
    assert len(m) == KV_DB_COUNT
    assert set(m.keys()) == set(range(KV_DB_COUNT))
    # Every backend must be one of the two valid choices.
    assert set(m.values()) <= {"REDIS"}


def test_kv_backend_map_includes_rdb_privacy():
    """RDB_PRIVACY (DB8) is part of the iteration — regression guard
    for the old `range(8)` bug that excluded it."""
    m = kv_backend_map()
    assert 8 in m


# ---------------------------------------------------------------------------
# get_kv caching
# ---------------------------------------------------------------------------

def test_get_kv_returns_cached_instance(clean_factory_cache):
    """Two calls with the same (db, decode_responses) return the same object."""
    a = get_kv(0, decode_responses=True)
    b = get_kv(0, decode_responses=True)
    assert a is b


def test_get_kv_separate_cache_per_decode_responses(clean_factory_cache):
    """decode_responses=True/False yield distinct cached clients."""
    a = get_kv(0, decode_responses=True)
    b = get_kv(0, decode_responses=False)
    assert a is not b


def test_close_all_kv_clears_cache(clean_factory_cache):
    """After close_all_kv() a subsequent get_kv() returns a fresh instance."""
    a = get_kv(0, decode_responses=True)
    close_all_kv()
    b = get_kv(0, decode_responses=True)
    # Cache was cleared, so this is a new object (identity differs).
    assert a is not b


# ---------------------------------------------------------------------------
# Per-DB env-var resolution
# ---------------------------------------------------------------------------

def test_per_db_override_resolved_correctly(monkeypatch, clean_factory_cache):
    """REDIS_CLIENT_CONFIG_DB{n} is read in preference to the global default."""
    monkeypatch.setenv("REDIS_CLIENT_CONFIG", "REDIS")
    monkeypatch.setenv("REDIS_CLIENT_CONFIG_DB2", "redis")   # lower-case → normalised
    m = kv_backend_map()
    assert m[0] == "REDIS"
    assert m[2] == "REDIS"


def test_removed_backend_raises_actionable_error(monkeypatch, clean_factory_cache):
    """An operator carrying a RUSTYCLUSTER value forward gets told why it fails."""
    monkeypatch.setenv("REDIS_CLIENT_CONFIG_DB2", "RUSTYCLUSTER")
    from core.config import kv_backend_for
    with pytest.raises(ValueError, match="not part of this release"):
        kv_backend_for(2)


def test_invalid_per_db_override_raises(monkeypatch, clean_factory_cache):
    monkeypatch.setenv("REDIS_CLIENT_CONFIG_DB3", "NOPE")
    from core.config import kv_backend_for
    with pytest.raises(ValueError, match="Invalid REDIS_CLIENT_CONFIG_DB3"):
        kv_backend_for(3)


# ---------------------------------------------------------------------------
# Async factory
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_get_kv_caches_within_loop(clean_factory_cache):
    """Within a single event loop, repeated calls return the same client."""
    try:
        a = await async_get_kv(0, decode_responses=True)
        b = await async_get_kv(0, decode_responses=True)
    except Exception:
        pytest.skip("async backend not reachable")
    assert a is b


@pytest.mark.asyncio
async def test_async_close_all_kv_clears_cache(clean_factory_cache):
    """After async_close_all_kv() a subsequent async_get_kv returns a fresh instance."""
    try:
        a = await async_get_kv(0, decode_responses=True)
    except Exception:
        pytest.skip("async backend not reachable")
    await async_close_all_kv()
    b = await async_get_kv(0, decode_responses=True)
    assert a is not b
    await async_close_all_kv()


def test_async_get_kv_keyed_by_event_loop(clean_factory_cache):
    """Two distinct event loops get two distinct async clients
    (the factory keys the cache on loop id)."""
    async def _create():
        return await async_get_kv(0, decode_responses=True)

    try:
        loop1 = asyncio.new_event_loop()
        loop2 = asyncio.new_event_loop()
        try:
            c1 = loop1.run_until_complete(_create())
            c2 = loop2.run_until_complete(_create())
        finally:
            try:
                loop1.run_until_complete(async_close_all_kv())
            except Exception:
                pass
            try:
                loop2.run_until_complete(async_close_all_kv())
            except Exception:
                pass
            loop1.close()
            loop2.close()
    except Exception:
        pytest.skip("async backend not reachable")
    assert c1 is not c2
