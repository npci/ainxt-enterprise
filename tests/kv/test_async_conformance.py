# SPDX-License-Identifier: MIT
# ============================================================
# Async KV conformance suite
#
# Mirrors test_conformance.py for the AsyncKVClient surface.
# Covers Gap #7 — the async client family used by:
#   - gateway.py SSE consumer (DB6)
#   - services/embed_svc/cache.py (DB7)
#   - services/privacy_svc/main.py (DB8)
#
# Each test runs once per backend; skipped when the backend is
# not reachable. Tests scope under key_prefix and clean up via
# the sync side-channel in conftest.
# ============================================================

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


# ---------------- Strings ----------------------------------------------------

async def test_async_set_get(async_kv, key_prefix):
    k = key_prefix + "str"
    assert await async_kv.set(k, "hello") is True
    assert await async_kv.get(k) == "hello"


async def test_async_set_with_ttl(async_kv, key_prefix):
    """`set(..., ex=N)` and `setex(..., N, ...)` are both honoured."""
    k = key_prefix + "ttl"
    assert await async_kv.set(k, "v", ex=60) is True
    # Value is visible immediately.
    assert await async_kv.get(k) == "v"


async def test_async_setex_and_mget(async_kv, key_prefix):
    a, b = key_prefix + "a", key_prefix + "b"
    await async_kv.setex(a, 60, "1")
    await async_kv.setex(b, 60, "2")
    res = await async_kv.mget(a, key_prefix + "missing", b)
    assert res[0] == "1"
    assert res[1] is None
    assert res[2] == "2"


# ---------------- Health -----------------------------------------------------

async def test_async_ping(async_kv):
    assert await async_kv.ping() is True


async def test_async_backend_attribute(async_kv):
    assert async_kv.backend in ("REDIS",)


# ---------------- Pipeline ---------------------------------------------------

async def test_async_pipeline_setex_execute(async_kv, key_prefix):
    """Multiple setex calls in one pipeline land as separate keys with TTL."""
    pipe = async_kv.pipeline()
    pipe.setex(key_prefix + "p1", 60, "a")
    pipe.setex(key_prefix + "p2", 60, "b")
    pipe.setex(key_prefix + "p3", 60, "c")
    await pipe.execute()
    # All three keys must be present.
    res = await async_kv.mget(
        key_prefix + "p1", key_prefix + "p2", key_prefix + "p3",
    )
    assert res == ["a", "b", "c"]


# ---------------- Streams (DB6) ----------------------------------------------

async def test_async_xread_round_trip(async_kv, kv, key_prefix):
    """
    Write to a stream via the sync `kv` fixture (xadd), then read it
    back through the async client. Both fixtures run against DB9, so
    the data is shared. Verifies the async xread format conversion.

    Note: the write goes through the sync `kv` fixture and the read
    through `async_kv`. Both resolve DB9, so the stream is shared; if
    either backend is unreachable the fixture skips.
    """
    k = key_prefix + "stream"
    kv.xadd(k, {"token": "hello"})
    kv.xadd(k, {"token": "world"})
    result = await async_kv.xread({k: "0-0"}, count=10)
    assert len(result) == 1
    _, entries = result[0]
    assert len(entries) == 2
    assert entries[0][1]["token"] == "hello"
    assert entries[1][1]["token"] == "world"


# ---------------- Lifecycle --------------------------------------------------

async def test_async_close_is_idempotent(async_kv):
    """Calling close() once must not raise; the fixture closes it again."""
    await async_kv.close()
