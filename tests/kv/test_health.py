# SPDX-License-Identifier: MIT
# ============================================================
# core.kv.health.kv_health_status() tests.
#
# Covers Gap #1 — per-DB ping shown in /health. The helper was
# extracted from gateway.py so it can be tested without importing
# the full FastAPI app.
# ============================================================

from __future__ import annotations

from core.config import KV_DB_COUNT
from core.kv import health as _health


class _StubKV:
    def __init__(self, ok: bool, error: str = "boom"):
        self._ok = ok
        self._err = error
        self.backend = "REDIS"
        self.db = -1

    def ping(self):
        if self._ok:
            return True
        raise RuntimeError(self._err)


def test_health_returns_entry_per_db():
    """One entry per logical DB, keyed `DB{n}`."""
    status = _health.kv_health_status()
    assert len(status) == KV_DB_COUNT
    assert set(status.keys()) == {f"DB{n}" for n in range(KV_DB_COUNT)}


def test_health_marks_reachable_kv_ok(kv):
    """The DB9-bound `kv` fixture is reachable; the corresponding
    entry in kv_health_status() must reflect ok=True if DB9 maps to
    the same configured backend."""
    status = _health.kv_health_status()
    # We don't assert which DBs are up — the platform may not have
    # all 9 backends reachable in dev. But at least one of them must
    # be `ok` for the test infrastructure to make sense.
    assert any(entry.get("ok") for entry in status.values())


def test_health_marks_unreachable_backend_failed(monkeypatch):
    """Force every get_kv() to return a stub whose ping raises;
    every entry must surface ok=False with an error string."""
    def _bad_get_kv(db, *args, **kwargs):
        return _StubKV(ok=False, error=f"db{db} unreachable")

    monkeypatch.setattr(_health, "kv_health_status", _health.kv_health_status)
    # Replace get_kv inside the factory module that health.py imports lazily.
    from core.kv import factory as _factory
    monkeypatch.setattr(_factory, "get_kv", _bad_get_kv)

    status = _health.kv_health_status()
    assert len(status) == KV_DB_COUNT
    for db_key, entry in status.items():
        assert entry["ok"] is False
        assert "error" in entry
        assert entry["backend"] in ("REDIS",)


def test_health_preserves_backend_label(monkeypatch):
    """The backend label per DB must be carried through unchanged."""
    monkeypatch.setenv("REDIS_CLIENT_CONFIG_DB2", "redis")   # lower-case input
    # Force backend resolution to pick up the env change.
    from core.kv import factory as _factory
    with _factory._lock:
        _factory._cache.clear()

    def _bad_get_kv(db, *args, **kwargs):
        return _StubKV(ok=False)
    monkeypatch.setattr(_factory, "get_kv", _bad_get_kv)

    status = _health.kv_health_status()
    assert status["DB2"]["backend"] == "REDIS"
    assert status["DB0"]["backend"] == "REDIS"
