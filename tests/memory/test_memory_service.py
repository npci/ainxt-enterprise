# SPDX-License-Identifier: Apache-2.0
# ============================================================
# P7 — MemoryService facade + sensitivity gate (pure)
# ============================================================

from memory.service import MemoryService, Scope


def _svc(**kw):
    store = {}

    def writer(scope, key, value):
        store[(scope, key)] = value
        return True

    def reader(scope, key):
        return store.get((scope, key))

    def forgetter(scope, key):
        return store.pop((scope, key), None) is not None

    return MemoryService(reader=reader, writer=writer, forgetter=forgetter, **kw), store


# ── sensitivity gate (the core new guarantee) ───────────────────────────────

def test_durable_write_refused_when_over_sensitivity_floor():
    svc, store = _svc(max_sensitivity_to_store="internal")
    # confidential/restricted must NOT persist to durable
    assert svc.write(Scope.DURABLE, "k", "secret", sensitivity="confidential") is False
    assert svc.write(Scope.DURABLE, "k", "secret", sensitivity="restricted") is False
    assert ("durable", "k") not in store


def test_durable_write_allowed_within_floor():
    svc, store = _svc(max_sensitivity_to_store="internal")
    assert svc.write(Scope.DURABLE, "k", "ok", sensitivity="internal") is True
    assert svc.write(Scope.DURABLE, "k2", "ok", sensitivity="public") is True
    assert store[("durable", "k")] == "ok"


def test_session_scope_not_sensitivity_gated():
    # ephemeral session memory is not cross-chat → not gated
    svc, store = _svc(max_sensitivity_to_store="internal")
    assert svc.write(Scope.SESSION, "k", "x", sensitivity="restricted") is True
    assert store[("session", "k")] == "x"


def test_can_store_durable_pure_gate():
    svc, _ = _svc(max_sensitivity_to_store="confidential")
    assert svc.can_store_durable("public") is True
    assert svc.can_store_durable("confidential") is True
    assert svc.can_store_durable("restricted") is False


# ── facade read/write/forget ────────────────────────────────────────────────

def test_read_write_forget_roundtrip():
    svc, _ = _svc()
    assert svc.write(Scope.SESSION, "a", 1) is True
    assert svc.read(Scope.SESSION, "a") == 1
    assert svc.forget(Scope.SESSION, "a") is True
    assert svc.read(Scope.SESSION, "a") is None


def test_missing_callables_are_safe():
    svc = MemoryService()  # no injected stores
    assert svc.read(Scope.SESSION, "k") is None
    assert svc.write(Scope.SESSION, "k", 1) is False
    assert svc.forget(Scope.SESSION, "k") is False


def test_store_errors_never_raise():
    def boom(*a, **k):
        raise RuntimeError("db down")
    svc = MemoryService(reader=boom, writer=boom, forgetter=boom)
    assert svc.read(Scope.DURABLE, "k") is None
    assert svc.write(Scope.SESSION, "k", 1) is False  # session bypasses gate, still safe
    assert svc.forget(Scope.SESSION, "k") is False
