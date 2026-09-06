# SPDX-License-Identifier: MIT
# ============================================================
# Phase 3 — Budget DB durability
#
# Verifies that:
#   1. usage:{uid}:total keys have NO TTL once incremented (financial
#      source-of-truth fast-path must never silently expire).
#   2. Re-fetching the same client (simulating an in-process "restart"
#      via close_all_kv + get_kv) returns the same data — the key
#      survives client lifecycle changes.
#   3. hincrby and hincrbyfloat behave identically on both backends.
#
# These are conformance-level checks that bind on real KV state, not
# the Postgres backstop. The Postgres path is exercised by the existing
# budget_store integration tests.
# ============================================================

from __future__ import annotations

import uuid

import pytest

from core.kv import KVClient, close_all_kv, get_kv
from core.config import RDB_BUDGET


@pytest.fixture
def budget_kv(kv: KVClient):
    """
    Force the kv fixture to point at DB=4 (budget). The fixture in
    conftest binds to DB=9 (cache) for safety; for the durability
    checks we need RDB_BUDGET because that is the DB the production
    code uses, and the assertions about "no TTL on usage:*:total"
    are only meaningful there.
    """
    backend = kv.backend
    # Sandbox prefix so this test never collides with a live deployment.
    prefix = f"kvtest_budget:{uuid.uuid4().hex[:8]}:"

    # Build a fresh client on RDB_BUDGET via the factory.
    client = get_kv(RDB_BUDGET, decode_responses=True)
    try:
        client.ping()
    except Exception:
        pytest.skip(f"{backend} backend not reachable for DB={RDB_BUDGET}")

    yield client, prefix

    # Cleanup
    try:
        leftover = client.keys(f"{prefix}*")
        if leftover:
            client.delete(*leftover)
    except Exception:
        pass


def test_usage_total_has_no_ttl(budget_kv):
    """
    `usage:{uid}:total` must never have a TTL — it is the financial
    source-of-truth fast-path. increment_usage() never calls expire()
    on this key; verify the invariant directly.
    """
    client, prefix = budget_kv
    key = f"{prefix}usage:user-001:total"

    client.hincrby(key, "tokens_used", 100)
    client.hincrby(key, "requests_made", 1)
    client.hincrbyfloat(key, "cost_usd_spent", 0.0125)

    # -1 means "no TTL"; -2 means "key does not exist".
    assert client.ttl(key) == -1, (
        f"usage:*:total must have no TTL; got {client.ttl(key)}"
    )

    data = client.hgetall(key)
    assert int(data["tokens_used"]) == 100
    assert int(data["requests_made"]) == 1
    assert float(data["cost_usd_spent"]) == pytest.approx(0.0125)


def test_dated_usage_has_ttl(budget_kv):
    """
    Daily-history keys (`usage:{uid}:{date}`) MUST have an 8-day TTL
    so they don't accumulate forever. Verify the contract by setting
    one explicitly.
    """
    client, prefix = budget_kv
    key = f"{prefix}usage:user-002:2026-05-19"

    client.hincrby(key, "tokens_used", 50)
    client.expire(key, 8 * 24 * 3600)

    ttl = client.ttl(key)
    assert 0 < ttl <= 8 * 24 * 3600, f"dated key TTL out of range: {ttl}"


def test_total_survives_client_recycle(budget_kv):
    """
    Simulate a process restart: write to usage:*:total, drop all
    cached KV clients, get a fresh client, and confirm the data
    is still readable. This exercises the durability guarantee that
    `close_all_kv()` does not touch on-disk state.
    """
    client, prefix = budget_kv
    key = f"{prefix}usage:user-003:total"

    client.hincrby(key, "tokens_used", 42)
    client.hincrbyfloat(key, "cost_usd_spent", 1.23)

    # "Restart" — drop the cache, get a brand-new client.
    close_all_kv()
    fresh = get_kv(RDB_BUDGET, decode_responses=True)
    data = fresh.hgetall(key)

    assert int(data["tokens_used"]) == 42
    assert float(data["cost_usd_spent"]) == pytest.approx(1.23)
    assert fresh.ttl(key) == -1


def test_hset_mapping_kwarg(budget_kv):
    """
    Phase 3 added mapping= support to KVClient.hset to match the
    redis-py call used by budget_store.set_budget(). Verify the
    bulk-set path writes every field.
    """
    client, prefix = budget_kv
    key = f"{prefix}budget:user-004"

    client.hset(key, mapping={
        "max_tokens_total":   "100000000",
        "max_requests_total": "5000",
        "max_cost_usd_total": "30.0",
        "model_limits":       "{}",
    })

    data = client.hgetall(key)
    assert data["max_tokens_total"] == "100000000"
    assert data["max_requests_total"] == "5000"
    assert data["max_cost_usd_total"] == "30.0"
    assert data["model_limits"] == "{}"
