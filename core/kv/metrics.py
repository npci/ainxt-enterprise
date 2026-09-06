# SPDX-License-Identifier: MIT
# ============================================================
# KV call metrics — Prometheus instrumentation
#
# Exposes:
#   kv_call_total{backend, op, db, outcome}     — Counter
#   kv_call_latency_seconds{backend, op, db}    — Histogram
#
# `outcome` is one of: "ok", "transient", "permanent", "error".
#
# The metrics share the global `metrics.registry` so /metrics
# emits a single unified document. We import the registry lazily
# to avoid an import cycle (metrics.py imports core.kv).
# ============================================================

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Optional

from .errors import KVError, KVPermanent, KVTransient

# Lazy singletons — created on first observe() call.
_kv_call_total = None
_kv_call_latency = None
_init_lock_taken = False


def _init() -> None:
    """Construct the Prometheus collectors once, lazily."""
    global _kv_call_total, _kv_call_latency, _init_lock_taken
    if _init_lock_taken:
        return
    _init_lock_taken = True
    try:
        from prometheus_client import Counter, Histogram

        # Share the global registry used by metrics.py so /metrics emits
        # both task_* and kv_call_* in one document. Falls back to the
        # default registry if the import fails (e.g. unit-test isolation).
        try:
            from metrics import registry as _shared_registry  # type: ignore
        except Exception:
            _shared_registry = None

        kwargs = {"registry": _shared_registry} if _shared_registry is not None else {}

        _kv_call_total = Counter(
            "kv_call_total",
            "Total KV calls by backend, operation, DB, and outcome",
            ["backend", "op", "db", "outcome"],
            **kwargs,
        )
        _kv_call_latency = Histogram(
            "kv_call_latency_seconds",
            "KV call latency in seconds, by backend, operation, and DB",
            ["backend", "op", "db"],
            **kwargs,
        )
    except Exception:
        # If prometheus_client isn't available the wrappers below silently
        # become no-ops; KV functionality must not depend on metrics.
        _kv_call_total = None
        _kv_call_latency = None


def _reset_for_tests() -> None:
    """Drop cached Prometheus collectors so each test starts clean.

    Tests that inspect counter values should call this in a fixture,
    then perform their operations, then read the new counter values.
    Not for production use — the collectors are intended to live for
    the lifetime of the process.
    """
    global _kv_call_total, _kv_call_latency, _init_lock_taken
    # Best-effort: unregister from the shared registry so the next
    # _init() can register fresh collectors with the same names.
    try:
        from prometheus_client import REGISTRY as _default_registry
        try:
            from metrics import registry as _shared_registry  # type: ignore
        except Exception:
            _shared_registry = None
        for reg in (_shared_registry, _default_registry):
            if reg is None:
                continue
            for collector in (_kv_call_total, _kv_call_latency):
                if collector is None:
                    continue
                try:
                    reg.unregister(collector)
                except Exception:
                    pass
    except Exception:
        pass
    _kv_call_total = None
    _kv_call_latency = None
    _init_lock_taken = False


def _classify_exception(exc: BaseException) -> str:
    if isinstance(exc, KVTransient):
        return "transient"
    if isinstance(exc, KVPermanent):
        return "permanent"
    if isinstance(exc, KVError):
        return "error"
    return "error"


@contextmanager
def observe(backend: str, op: str, db: int):
    """
    Context manager that records latency + outcome for one KV call.

    Usage (inside the impl's _wrap decorator):

        with observe(self.backend, op_name, self.db):
            return fn(*args, **kwargs)
    """
    _init()
    start = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException as exc:
        outcome = _classify_exception(exc)
        raise
    finally:
        elapsed = time.perf_counter() - start
        if _kv_call_total is not None:
            try:
                _kv_call_total.labels(
                    backend=backend, op=op, db=str(db), outcome=outcome,
                ).inc()
            except Exception:
                pass
        if _kv_call_latency is not None:
            try:
                _kv_call_latency.labels(
                    backend=backend, op=op, db=str(db),
                ).observe(elapsed)
            except Exception:
                pass
