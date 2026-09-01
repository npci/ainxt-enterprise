# SPDX-License-Identifier: Apache-2.0
# ============================================================
# store/budget_store.py KV-contract tests (Phase 4).
#
# Risk closed: the financial fast-path on DB=4 has different TTL
# requirements per key family — `usage:{uid}:total` must NEVER expire,
# `usage:{uid}:{date}` must have an 8-day TTL, product chargeback keys
# must have a 35-day TTL. We assert these invariants explicitly so a
# regression on either backend is caught.
#
# Tests bind to DB9 by monkeypatching budget_store._r with the
# parametrized `kv` fixture. Postgres helpers are monkeypatched to
# return fixed values so the tests don't require a live PG.
# ============================================================

from __future__ import annotations

import uuid

import pytest

from store import budget_store as _bs


def _uid() -> str:
    return f"kvtest-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def budget_kv(kv, monkeypatch):
    """Bind budget_store to the test kv client and return (kv, uid)."""
    # The module caches a singleton at _r; replace it with the fixture.
    monkeypatch.setattr(_bs, "_r", kv)
    # _get_redis() returns _r if non-None, but it also has a path that
    # pings then caches. We short-circuit by setting _r directly.
    monkeypatch.setattr(_bs, "_get_redis", lambda: kv)
    uid = _uid()
    yield kv, uid
    # Cleanup keys this user touched.
    try:
        for pattern in (f"usage:{uid}:*", f"budget:{uid}", f"usage:product:*"):
            leftover = kv.keys(pattern)
            if leftover:
                kv.delete(*leftover)
        kv.srem("budget:users:index", uid)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# TTL invariants
# ---------------------------------------------------------------------------

def test_increment_usage_total_has_no_ttl(budget_kv, monkeypatch):
    """`usage:{uid}:total` is the financial source-of-truth fast-path —
    it MUST NOT have a TTL, ever."""
    monkeypatch.setattr(_bs, "_pg_increment", lambda *a, **k: None)
    kv, uid = budget_kv
    _bs.increment_usage(uid, tokens=100, cost_usd=0.05)

    ttl_total = kv.ttl(f"usage:{uid}:total")
    assert ttl_total == -1, f"usage:{uid}:total must have no TTL; got {ttl_total}"


def test_increment_usage_dated_has_8_day_ttl(budget_kv, monkeypatch):
    """`usage:{uid}:{today}` must have ~8-day TTL for history rollup."""
    monkeypatch.setattr(_bs, "_pg_increment", lambda *a, **k: None)
    kv, uid = budget_kv
    _bs.increment_usage(uid, tokens=50, cost_usd=0.02)

    today_key = f"usage:{uid}:{_bs._today()}"
    ttl = kv.ttl(today_key)
    eight_days = 8 * 24 * 3600
    assert 0 < ttl <= eight_days, f"dated TTL out of range: {ttl}"
    # Within 60 seconds of the full 8-day window.
    assert ttl >= eight_days - 60


def test_increment_usage_indexes_user(budget_kv, monkeypatch):
    """The user must be added to the budget:users:index set."""
    monkeypatch.setattr(_bs, "_pg_increment", lambda *a, **k: None)
    kv, uid = budget_kv
    _bs.increment_usage(uid, tokens=10)
    assert kv.sismember("budget:users:index", uid) is True


def test_product_chargeback_key_has_35_day_ttl(budget_kv, monkeypatch):
    monkeypatch.setattr(_bs, "_pg_increment", lambda *a, **k: None)
    kv, uid = budget_kv
    product_id = f"prod-{uuid.uuid4().hex[:6]}"
    _bs.increment_usage(uid, tokens=10, cost_usd=0.1, product_id=product_id)

    prod_key = f"usage:product:{product_id}:{_bs._today()}"
    ttl = kv.ttl(prod_key)
    expected = 35 * 24 * 3600
    assert 0 < ttl <= expected
    assert ttl >= expected - 60


# ---------------------------------------------------------------------------
# Hash field semantics
# ---------------------------------------------------------------------------

def test_increment_usage_writes_correct_fields(budget_kv, monkeypatch):
    """hincrby and hincrbyfloat both land on the same hash key."""
    monkeypatch.setattr(_bs, "_pg_increment", lambda *a, **k: None)
    kv, uid = budget_kv
    _bs.increment_usage(uid, tokens=42, requests=3, cost_usd=0.75)

    data = kv.hgetall(f"usage:{uid}:total")
    assert int(data["tokens_used"]) == 42
    assert int(data["requests_made"]) == 3
    assert float(data["cost_usd_spent"]) == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Fail-open contract
# ---------------------------------------------------------------------------

def test_check_budget_fail_open_when_kv_and_pg_down(monkeypatch):
    """If both Redis and Postgres are unreachable, check_budget MUST
    return allowed=True so the platform doesn't refuse all traffic."""
    monkeypatch.setattr(_bs, "_get_redis", lambda: None)
    def _raise(*a, **k):
        raise RuntimeError("PG down")
    monkeypatch.setattr(_bs, "_pg_get_usage", _raise)
    monkeypatch.setattr(_bs, "_pg_get_budget", _raise)

    result = _bs.check_budget("any-user")
    assert result["allowed"] is True
    assert "unavailable" in result["reason"].lower() or "fail-open" in result["reason"].lower()


def test_check_budget_uses_kv_first(budget_kv, monkeypatch):
    """When KV has the data, Postgres is NOT consulted."""
    kv, uid = budget_kv
    # Seed a budget directly in KV with a tiny cost cap.
    kv.hset(f"budget:{uid}", mapping={
        "max_cost_usd_total": "1.00",
        "max_tokens_total":   "1000000",
        "max_requests_total": "10000",
        "model_limits":       "{}",
    })
    # Make Postgres fall over so the test fails if budget_store goes there.
    def _boom(*a, **k):
        raise AssertionError("PG should not be consulted when KV has data")
    monkeypatch.setattr(_bs, "_pg_get_budget", _boom)
    # Usage well under cap.
    kv.hset(f"usage:{uid}:total", "cost_usd_spent", "0.50")
    kv.hset(f"usage:{uid}:total", "tokens_used", "100")
    kv.hset(f"usage:{uid}:total", "requests_made", "1")
    # Block PG for usage too.
    monkeypatch.setattr(_bs, "_pg_get_usage", _boom)

    result = _bs.check_budget(uid)
    assert result["allowed"] is True

    # Push over the cap and re-check.
    kv.hset(f"usage:{uid}:total", "cost_usd_spent", "1.50")
    result = _bs.check_budget(uid)
    assert result["allowed"] is False
    assert "spend" in result["reason"].lower() or "limit" in result["reason"].lower()
