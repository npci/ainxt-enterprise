# SPDX-License-Identifier: MIT
# ============================================================
# validate_prod_config() tests.
#
# Covers Gap #3 — prod startup must fail fast on incomplete
# configuration rather than booting into a broken state.
#
# Because core.config sets module-level constants (IS_PROD,
# KV_BACKEND_MAP) at import time, each test toggles env vars and
# uses importlib.reload(core.config) to get a fresh snapshot.
# A finally block resets DEPLOYMENT_MODE=local and reloads again
# so the next test (and the rest of the suite) sees default
# non-prod behaviour.
# ============================================================

from __future__ import annotations

import importlib
import os

import pytest


def _reload_config():
    import core.config as _cfg
    return importlib.reload(_cfg)


def _reload_to_local():
    """Restore non-prod state so later tests see defaults."""
    os.environ["DEPLOYMENT_MODE"] = "local"
    os.environ.pop("REDIS_CLIENT_CONFIG", None)
    for n in range(9):
        os.environ.pop(f"REDIS_CLIENT_CONFIG_DB{n}", None)
    _reload_config()


@pytest.fixture
def prod_env(monkeypatch, tmp_path):
    """Set DEPLOYMENT_MODE=prod and a baseline JWT_SECRET.

    Tests that exercise the prod-validation paths use this fixture,
    then layer their own env vars on top. After the test, the module
    is reloaded with local defaults so subsequent tests are unaffected.
    """
    monkeypatch.setenv("DEPLOYMENT_MODE", "prod")
    monkeypatch.setenv("JWT_SECRET", "test-secret-not-used-for-signing-just-startup")
    # Default: every DB on REDIS unless an individual test overrides.
    monkeypatch.delenv("REDIS_CLIENT_CONFIG", raising=False)
    for n in range(9):
        monkeypatch.delenv(f"REDIS_CLIENT_CONFIG_DB{n}", raising=False)
    # Move to a tmp dir + tmp HOME so nothing in the repo or the
    # developer's home directory leaks into the validated config.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))   # Windows
    yield tmp_path
    _reload_to_local()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_passes_when_all_redis_and_jwt_set(prod_env):
    cfg = _reload_config()
    # Should NOT raise.
    cfg.validate_prod_config()


def test_passes_with_explicit_redis_backend(prod_env, monkeypatch):
    monkeypatch.setenv("REDIS_CLIENT_CONFIG", "REDIS")
    cfg = _reload_config()
    cfg.validate_prod_config()


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------

def test_fails_when_jwt_missing_in_prod(prod_env, monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    cfg = _reload_config()
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        cfg.validate_prod_config()


def test_removed_kv_backend_fails_before_validation(prod_env, monkeypatch):
    """RUSTYCLUSTER is not part of this release. A prod deployment that still
    sets it must fail at config load — before validate_prod_config() is even
    reachable — so it can never boot pointed at a backend that isn't there."""
    monkeypatch.setenv("REDIS_CLIENT_CONFIG", "RUSTYCLUSTER")
    with pytest.raises(ValueError, match="not part of this release"):
        _reload_config()


def test_removed_kv_backend_detected_via_per_db_override(prod_env, monkeypatch):
    """A single DB pinned to the removed backend is caught too, and the error
    names the DB so the operator knows which variable to change."""
    monkeypatch.setenv("REDIS_CLIENT_CONFIG_DB4", "RUSTYCLUSTER")
    with pytest.raises(ValueError) as excinfo:
        _reload_config()
    assert "REDIS_CLIENT_CONFIG_DB4" in str(excinfo.value)


# ---------------------------------------------------------------------------
# PROXY_KEY_TOKEN opt-in hard requirement (EA Finding 1 / Finding 7)
# ---------------------------------------------------------------------------

def test_proxy_key_token_not_required_by_default(prod_env, monkeypatch):
    """Without REQUIRE_PROXY_KEY_TOKEN=true, a missing PROXY_KEY_TOKEN must
    NOT fail startup — this is the existing warn-only staged-rollout
    behaviour, unchanged for deployments that haven't opted in yet."""
    monkeypatch.delenv("REQUIRE_PROXY_KEY_TOKEN", raising=False)
    monkeypatch.delenv("PROXY_KEY_TOKEN", raising=False)
    cfg = _reload_config()
    cfg.validate_prod_config()  # must not raise


def test_proxy_key_token_required_when_opted_in_and_missing(prod_env, monkeypatch):
    """REQUIRE_PROXY_KEY_TOKEN=true + no PROXY_KEY_TOKEN must hard-fail."""
    monkeypatch.setenv("REQUIRE_PROXY_KEY_TOKEN", "true")
    monkeypatch.delenv("PROXY_KEY_TOKEN", raising=False)
    cfg = _reload_config()
    with pytest.raises(RuntimeError, match="PROXY_KEY_TOKEN"):
        cfg.validate_prod_config()


def test_proxy_key_token_required_and_present_passes(prod_env, monkeypatch):
    """REQUIRE_PROXY_KEY_TOKEN=true + PROXY_KEY_TOKEN set must NOT raise."""
    monkeypatch.setenv("REQUIRE_PROXY_KEY_TOKEN", "true")
    monkeypatch.setenv("PROXY_KEY_TOKEN", "a-real-token-value")
    cfg = _reload_config()
    cfg.validate_prod_config()  # must not raise
