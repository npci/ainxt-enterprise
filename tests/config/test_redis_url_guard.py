# SPDX-License-Identifier: Apache-2.0
# ============================================================
# redis_url(db) guard tests.
#
# Covers Gap #9 — redis_url(db) must build a correct per-logical-DB
# URL from REDIS_CLIENT_CONFIG_DB{n}, and an unsupported backend value
# must be rejected rather than silently producing a redis:// URL.
# ============================================================

from __future__ import annotations

import importlib
import os

import pytest


def _reload_config():
    import core.config as _cfg
    return importlib.reload(_cfg)


@pytest.fixture(autouse=True)
def _isolate_config_env(monkeypatch):
    """Strip all REDIS_CLIENT_CONFIG_* env vars and reload core.config
    before AND after every test so the suite is hermetic."""
    monkeypatch.delenv("REDIS_CLIENT_CONFIG", raising=False)
    for n in range(9):
        monkeypatch.delenv(f"REDIS_CLIENT_CONFIG_DB{n}", raising=False)
    cfg = _reload_config()
    yield cfg
    # Restore defaults after the test.
    os.environ.pop("REDIS_CLIENT_CONFIG", None)
    for n in range(9):
        os.environ.pop(f"REDIS_CLIENT_CONFIG_DB{n}", None)
    _reload_config()


def test_redis_url_returns_url_in_redis_mode(_isolate_config_env):
    cfg = _isolate_config_env
    url = cfg.redis_url(0)
    assert url.startswith("redis://")
    assert url.endswith("/0")


def test_removed_backend_rejected_at_import(_isolate_config_env, monkeypatch):
    """RUSTYCLUSTER was dropped from this release; carrying the value forward
    must fail loudly at config load with an actionable message, not fall back
    to Redis behind the operator's back."""
    monkeypatch.setenv("REDIS_CLIENT_CONFIG_DB4", "RUSTYCLUSTER")
    with pytest.raises(ValueError, match="not part of this release"):
        _reload_config()


def test_unknown_backend_rejected_at_import(_isolate_config_env, monkeypatch):
    monkeypatch.setenv("REDIS_CLIENT_CONFIG_DB4", "NOPE")
    with pytest.raises(ValueError, match="Invalid REDIS_CLIENT_CONFIG_DB4"):
        _reload_config()


def test_redis_url_unaffected_by_other_dbs(_isolate_config_env, monkeypatch):
    """A per-DB override must not disturb redis_url() for other DBs."""
    monkeypatch.setenv("REDIS_CLIENT_CONFIG_DB4", "redis")   # lower-case → normalised
    cfg = _reload_config()
    assert cfg.redis_url(0).endswith("/0")
    assert cfg.redis_url(4).endswith("/4")


def test_redis_url_covers_every_logical_db(_isolate_config_env):
    cfg = _isolate_config_env
    for db in range(cfg.KV_DB_COUNT):
        assert cfg.redis_url(db).endswith(f"/{db}")
