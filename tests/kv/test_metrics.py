# SPDX-License-Identifier: MIT
# ============================================================
# core.kv.metrics tests.
#
# Covers Gap #5 — Prometheus kv_call_total + kv_call_latency_seconds.
#
# Each test resets the collector singletons via _reset_for_tests()
# so the counter values don't bleed across cases.
# ============================================================

from __future__ import annotations

import pytest

from core.kv import metrics as _kv_metrics
from core.kv.errors import KVError, KVPermanent, KVTransient


@pytest.fixture(autouse=True)
def _reset_metrics_between_tests():
    """Clean Prometheus state before AND after every test in this module."""
    _kv_metrics._reset_for_tests()
    yield
    _kv_metrics._reset_for_tests()


def _counter_value(backend: str, op: str, db: int, outcome: str) -> float:
    """Read the current value of kv_call_total for the given label set."""
    c = _kv_metrics._kv_call_total
    if c is None:
        return 0.0
    return c.labels(backend=backend, op=op, db=str(db), outcome=outcome)._value.get()


def _histogram_sample_count(backend: str, op: str, db: int) -> float:
    """Read the *_count sample of the kv_call_latency_seconds histogram.

    prometheus_client 0.25 doesn't expose a `._count` attribute on the
    labelled child; instead we walk `collect()` and pick the
    `kv_call_latency_seconds_count` sample whose label set matches.
    """
    h = _kv_metrics._kv_call_latency
    if h is None:
        return 0.0
    want = {"backend": backend, "op": op, "db": str(db)}
    for metric in h.collect():
        for sample in metric.samples:
            if sample.name.endswith("_count") and sample.labels == want:
                return float(sample.value)
    return 0.0


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------

def test_observe_records_ok_outcome():
    with _kv_metrics.observe("REDIS", "set", 0):
        pass   # success path
    assert _counter_value("REDIS", "set", 0, "ok") == 1.0


def test_observe_records_transient_outcome():
    with pytest.raises(KVTransient):
        with _kv_metrics.observe("REDIS", "get", 1):
            raise KVTransient("conn refused")
    assert _counter_value("REDIS", "get", 1, "transient") == 1.0
    # No accidental increment of the ok bucket.
    assert _counter_value("REDIS", "get", 1, "ok") == 0.0


def test_observe_records_permanent_outcome():
    with pytest.raises(KVPermanent):
        with _kv_metrics.observe("REDIS", "delete", 2):
            raise KVPermanent("auth failed")
    assert _counter_value("REDIS", "delete", 2, "permanent") == 1.0


def test_observe_records_generic_kverror_as_error():
    with pytest.raises(KVError):
        with _kv_metrics.observe("REDIS", "hget", 3):
            raise KVError("misc")
    assert _counter_value("REDIS", "hget", 3, "error") == 1.0


def test_observe_records_unknown_exception_as_error():
    with pytest.raises(RuntimeError):
        with _kv_metrics.observe("REDIS", "xadd", 6):
            raise RuntimeError("not a KVError")
    assert _counter_value("REDIS", "xadd", 6, "error") == 1.0


# ---------------------------------------------------------------------------
# Latency histogram
# ---------------------------------------------------------------------------

def test_observe_records_latency_sample():
    """Each observe() must add one observation to the histogram."""
    for _ in range(3):
        with _kv_metrics.observe("REDIS", "ping", 0):
            pass
    assert _kv_metrics._kv_call_latency is not None
    assert _histogram_sample_count("REDIS", "ping", 0) == 3.0


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_observe_is_safe_without_prometheus(monkeypatch):
    """If prometheus_client is unavailable, observe() must not raise."""
    import builtins
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "prometheus_client":
            raise ImportError("simulated: prometheus_client missing")
        return real_import(name, *args, **kwargs)

    _kv_metrics._reset_for_tests()
    monkeypatch.setattr(builtins, "__import__", _fake_import)

    # Should not raise even though the collectors can't be created.
    with _kv_metrics.observe("REDIS", "set", 0):
        pass
    assert _kv_metrics._kv_call_total is None
    assert _kv_metrics._kv_call_latency is None


# ---------------------------------------------------------------------------
# Integration with real KV client
# ---------------------------------------------------------------------------

def test_kv_call_increments_via_real_client(kv, key_prefix):
    """A real .set() + .get() through the KVClient layer must
    increment the metric. Uses whichever backend the kv fixture chose."""
    # Pre-condition: counters at zero for this label set.
    before_set = _counter_value(kv.backend, "set", kv.db, "ok")
    before_get = _counter_value(kv.backend, "get", kv.db, "ok")
    kv.set(key_prefix + "m1", "x")
    kv.get(key_prefix + "m1")
    assert _counter_value(kv.backend, "set", kv.db, "ok") == before_set + 1
    assert _counter_value(kv.backend, "get", kv.db, "ok") == before_get + 1
