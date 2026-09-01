# SPDX-License-Identifier: Apache-2.0
# ============================================================
# DistributedSemaphore tests — Lua script via the KV layer.
#
# Covers Gap #6 — the semaphore uses KVClient.register_script rather
# than reaching for redis-py directly, so it must hold on any backend
# that implements the script contract (EVALSHA on REDIS, SPEC §6.7).
# ============================================================

from __future__ import annotations

import time
import uuid

import pytest

from core.distributed_semaphore import DistributedSemaphore


def _name(prefix: str) -> str:
    """Unique semaphore name per test so parallel tests don't collide."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def test_semaphore_acquires_below_capacity(kv):
    sem = DistributedSemaphore(kv, _name("acq"), capacity=2, ttl_ms=10_000)
    token = sem.acquire(timeout=2)
    assert token is not None
    assert sem.current_count() == 1
    sem.release(token)


def test_semaphore_blocks_at_capacity_and_returns_none(kv):
    """Once capacity is exhausted, acquire(timeout=…) must return None."""
    sem = DistributedSemaphore(kv, _name("cap"), capacity=1, ttl_ms=10_000)
    first = sem.acquire(timeout=2)
    assert first is not None
    t0 = time.monotonic()
    second = sem.acquire(timeout=0.3)
    elapsed = time.monotonic() - t0
    assert second is None
    # Should have waited approximately the timeout window (allow scheduler slack).
    assert 0.2 <= elapsed <= 2.0
    sem.release(first)


def test_semaphore_release_frees_slot(kv):
    sem = DistributedSemaphore(kv, _name("rel"), capacity=1, ttl_ms=10_000)
    first = sem.acquire(timeout=2)
    assert first is not None
    sem.release(first)
    second = sem.acquire(timeout=2)
    assert second is not None
    sem.release(second)


def test_semaphore_expired_slots_auto_released(kv):
    """When a slot's TTL passes, the next acquire evicts it via Lua."""
    sem = DistributedSemaphore(kv, _name("exp"), capacity=1, ttl_ms=200)
    first = sem.acquire(timeout=2)
    assert first is not None
    # Wait past the TTL — the next acquire's ZREMRANGEBYSCORE will evict.
    time.sleep(0.35)
    second = sem.acquire(timeout=2)
    assert second is not None
    sem.release(second)


def test_semaphore_current_count_reflects_active_holders(kv):
    sem = DistributedSemaphore(kv, _name("cnt"), capacity=3, ttl_ms=10_000)
    tokens = []
    for _ in range(3):
        t = sem.acquire(timeout=2)
        assert t is not None
        tokens.append(t)
    assert sem.current_count() == 3
    for t in tokens:
        sem.release(t)
    assert sem.current_count() == 0
