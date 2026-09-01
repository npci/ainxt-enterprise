# SPDX-License-Identifier: Apache-2.0
# ============================================================
# core/circuit_breaker.py KV-contract tests (Phase 4).
#
# Risk closed: state transitions persist via incr / set / expire
# on DB=5. If any of these semantics drift between backends, a
# tripped breaker may forget its open state, causing thundering-herd
# failures when the next request hits.
#
# Each test scopes a unique breaker name so cases don't bleed state.
# The cb._redis singleton is patched to the parametrized kv fixture.
# ============================================================

from __future__ import annotations

import time
import uuid

import pytest

from core import circuit_breaker as cb


def _name(tag: str) -> str:
    return f"{tag}_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def patched_cb(monkeypatch, kv):
    """Bind circuit_breaker's KV singleton to the test fixture."""
    monkeypatch.setattr(cb, "_redis", kv)
    monkeypatch.setattr(cb, "_redis_available", True)
    monkeypatch.setattr(cb, "_get_redis", lambda: kv)
    yield kv


# ---------------------------------------------------------------------------
# CLOSED → normal operation
# ---------------------------------------------------------------------------

def test_closed_breaker_passes_calls_through(patched_cb):
    name = _name("closed")
    breaker = cb.CircuitBreaker(name=name, failure_threshold=5, recovery_timeout=30)
    assert breaker.is_open is False
    result = breaker.call(lambda: 42)
    assert result == 42


# ---------------------------------------------------------------------------
# Threshold trip → OPEN
# ---------------------------------------------------------------------------

def test_threshold_trips_breaker_open(patched_cb):
    name = _name("trip")
    breaker = cb.CircuitBreaker(name=name, failure_threshold=3, recovery_timeout=30)

    def _fail():
        raise ValueError("boom")

    # 3 failures → breaker opens.
    for _ in range(3):
        with pytest.raises(ValueError):
            breaker.call(_fail)

    assert breaker.is_open is True
    # Next call must fast-fail without invoking the fn.
    sentinel = {"called": False}
    def _should_not_run():
        sentinel["called"] = True
    with pytest.raises(RuntimeError, match="OPEN"):
        breaker.call(_should_not_run)
    assert sentinel["called"] is False


def test_failure_count_persists_across_instances(patched_cb):
    """State lives in KV, so a fresh breaker instance with the same
    name observes the previous instance's failure count."""
    name = _name("persist")
    breaker1 = cb.CircuitBreaker(name=name, failure_threshold=10, recovery_timeout=30)

    def _fail():
        raise RuntimeError("nope")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker1.call(_fail)

    # Drop the first instance entirely; create a new one with same name.
    breaker2 = cb.CircuitBreaker(name=name, failure_threshold=10, recovery_timeout=30)
    assert breaker2._get_failures() == 2


# ---------------------------------------------------------------------------
# Recovery → HALF_OPEN → CLOSED
# ---------------------------------------------------------------------------

def test_recovery_timeout_transitions_to_half_open_then_closed(patched_cb):
    name = _name("recover")
    breaker = cb.CircuitBreaker(name=name, failure_threshold=2, recovery_timeout=1)

    def _fail():
        raise RuntimeError("x")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(_fail)
    assert breaker.is_open is True

    # Rewind opened_at so the recovery_timeout has elapsed.
    patched_cb.set(breaker._opened_at_key, str(time.time() - 60), ex=86400)

    # is_open getter transitions OPEN → HALF_OPEN if timeout elapsed.
    assert breaker.is_open is False
    # Successful probe should reset to CLOSED.
    result = breaker.call(lambda: "ok")
    assert result == "ok"
    assert breaker._get_failures() == 0


# ---------------------------------------------------------------------------
# KV unavailable — observe documented internals
# ---------------------------------------------------------------------------
#
# Per the docstring of CircuitBreaker._get_state:
#     "Redis unavailable: fail open (safe default prevents split-brain)"
# i.e. when no KV is reachable, _get_state() returns OPEN.
#
# However, the high-level `is_open` property layers a recovery-timeout
# check on top of `_get_state()`: because `_get_opened_at()` also
# returns 0.0 when the KV is down, the timeout always appears elapsed
# and `is_open` transitions OPEN → HALF_OPEN → returns False.
#
# We assert the internal state (which IS "fail-open" at the KV layer)
# AND document the observable is_open value so future readers
# understand why the high-level behaviour differs from the docstring.

def test_breaker_internal_state_is_open_when_kv_unavailable(monkeypatch):
    name = _name("nokv-state")
    monkeypatch.setattr(cb, "_get_redis", lambda: None)
    breaker = cb.CircuitBreaker(name=name, failure_threshold=2, recovery_timeout=30)
    # _get_state honours the docstring — fail-open at the KV layer.
    assert breaker._get_state() == cb.OPEN
    # Failures and opened_at can't be read either; they degrade to defaults.
    assert breaker._get_failures() == 0
    assert breaker._get_opened_at() == 0.0


def test_breaker_is_open_returns_false_when_kv_unavailable(monkeypatch):
    """Observable consequence of the KV-down + recovery-timeout
    interaction: is_open reports False because opened_at=0.0 makes
    the recovery window appear elapsed. Documented here so a future
    regression that flips this value is caught."""
    name = _name("nokv-isopen")
    monkeypatch.setattr(cb, "_get_redis", lambda: None)
    breaker = cb.CircuitBreaker(name=name, failure_threshold=2, recovery_timeout=30)
    assert breaker.is_open is False


# ---------------------------------------------------------------------------
# record_success() / record_failure() — ARCH-F-007 / ARCH-F-008
#
# Fine-grained variants of call() used by ConnectorEngine._execute_with_retry
# so a caller can decide per-outcome whether something counts as
# circuit-breaker signal (e.g. skip recording on a legitimate 404/403
# business error, but record on a 5xx/connection failure).
# ---------------------------------------------------------------------------

def test_record_failure_trips_breaker_open(patched_cb):
    name = _name("record_fail")
    breaker = cb.CircuitBreaker(name=name, failure_threshold=3, recovery_timeout=30)

    for _ in range(3):
        breaker.record_failure(RuntimeError("boom"))

    assert breaker.is_open is True
    assert breaker._get_failures() == 3


def test_record_success_resets_failures_and_closes(patched_cb):
    name = _name("record_success")
    breaker = cb.CircuitBreaker(name=name, failure_threshold=3, recovery_timeout=30)

    breaker.record_failure(RuntimeError("one"))
    breaker.record_failure(RuntimeError("two"))
    assert breaker._get_failures() == 2

    breaker.record_success()
    assert breaker._get_failures() == 0
    assert breaker._get_state() == cb.CLOSED


def test_record_failure_below_threshold_stays_closed(patched_cb):
    name = _name("record_below_threshold")
    breaker = cb.CircuitBreaker(name=name, failure_threshold=5, recovery_timeout=30)

    breaker.record_failure(RuntimeError("one"))
    breaker.record_failure(RuntimeError("two"))

    assert breaker.is_open is False
    assert breaker._get_failures() == 2
