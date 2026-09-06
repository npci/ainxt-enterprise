# SPDX-License-Identifier: MIT
# ============================================================
# Root-level conftest — fixtures shared across all tests.
#
# Two backends are parameterized for `kv` / `async_kv`:
#   - "REDIS"        — uses the local redis-server on REDIS_HOST:REDIS_PORT
#
# Each test runs once per backend. Tests are skipped if the backend
# is not reachable so partial local setups still pass CI.
#
# These live in the ROOT conftest (not tests/kv/conftest.py) so any
# test under tests/* — including tests/store/, tests/auth/, etc. —
# can take `kv`, `key_prefix`, `async_kv`, or `clean_factory_cache`.
# ============================================================

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

from core.kv.errors import KVError
from core.kv.redis_impl import RedisKVClient


# ──────────────────────────────────────────────────────────────────────────────
# patched_module_client — small helper for tests that bind a module-level
# KV singleton (trace_store.redis_client, budget_store._r, …) to a
# test-supplied client.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def patched_module_client(monkeypatch):
    """Replace a module-level KV singleton with a test-supplied client.

    Usage::

        def test_x(kv, patched_module_client):
            patched_module_client("core.trace_store", "redis_client", kv)
            from core.trace_store import add_trace
            add_trace("rid", "step1")

    The patch is reverted automatically by monkeypatch.
    """
    def _apply(module_path: str, attr_name: str, replacement) -> None:
        import importlib
        mod = importlib.import_module(module_path)
        monkeypatch.setattr(mod, attr_name, replacement)
    return _apply


# ──────────────────────────────────────────────────────────────────────────────
# Sync KV fixtures. Parametrised over the available backends — Redis is the
# only one, but the parametrisation is what keeps these tests backend-agnostic.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def key_prefix() -> str:
    """Unique key prefix per test so parallel runs and stale data don't collide."""
    return f"kvtest:{uuid.uuid4().hex[:8]}:"


def _make_redis_client():
    # Test DB 9 — not in use by the platform (avoids touching real data).
    client = RedisKVClient(db=9, decode_responses=True)
    try:
        client.ping()
    except KVError:
        pytest.skip("Redis not reachable on configured host/port")
    return client


@pytest.fixture(params=["REDIS"])
def kv(request, key_prefix):
    """Yield a KVClient bound to whichever backend the test parametrizes.

    Parametrized rather than hard-coded so a second backend can be added to
    ``params`` without touching every test that consumes this fixture.
    """
    assert request.param == "REDIS"
    client = _make_redis_client()

    yield client
    try:
        leftover = client.keys(f"{key_prefix}*")
        if leftover:
            client.delete(*leftover)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Async KV fixtures (mirror the sync `kv` shape; see core.kv.async_*).
# ──────────────────────────────────────────────────────────────────────────────


async def _make_async_redis_client():
    try:
        from core.kv.async_redis_impl import AsyncRedisKVClient
    except ImportError:
        pytest.skip("redis.asyncio not installed")
    client = AsyncRedisKVClient(db=9, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        pytest.skip("Redis (async) not reachable on configured host/port")
    return client


@pytest_asyncio.fixture(params=["REDIS"])
async def async_kv(request, key_prefix):
    """Yield an AsyncKVClient bound to the parametrized backend.

    Cleanup uses the sync ``RedisKVClient(db=9)`` side-channel because
    the AsyncKVClient interface deliberately omits ``keys()``. Tests
    scope under ``key_prefix`` so partial cleanup is safe.
    """
    assert request.param == "REDIS"
    client = await _make_async_redis_client()

    yield client

    try:
        from core.kv.redis_impl import RedisKVClient
        cleanup = RedisKVClient(db=9, decode_responses=True)
        leftover = cleanup.keys(f"{key_prefix}*")
        if leftover:
            cleanup.delete(*leftover)
    except Exception:
        pass
    try:
        await client.close()
    except Exception:
        pass


@pytest.fixture
def clean_factory_cache():
    """Drop both the sync and async factory caches before AND after the test.

    Required by tests that monkeypatch ``REDIS_CLIENT_CONFIG_*`` env
    vars and need the next ``get_kv(db)`` call to re-resolve the
    backend instead of reusing a stale cached client.
    """
    from core.kv import factory as _factory
    with _factory._lock:
        _factory._cache.clear()
    with _factory._async_lock:
        _factory._async_cache.clear()
    yield
    with _factory._lock:
        _factory._cache.clear()
    with _factory._async_lock:
        _factory._async_cache.clear()
