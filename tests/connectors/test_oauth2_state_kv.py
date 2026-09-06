# SPDX-License-Identifier: MIT
# ============================================================
# connectors/oauth2.py PKCE state KV-contract tests (Phase 4).
#
# Risk closed: OAuth callback fails if save/load/delete of the
# state payload misbehaves on RC. We exercise the save/load
# round-trip, the consume-on-load semantics, and the 10-minute TTL.
#
# OAuth2Handler reads KV via `from core.kv import get_kv` inside
# each method; we patch the get_kv symbol on the core.kv module to
# return the test fixture client.
# ============================================================

from __future__ import annotations

import uuid

import pytest

from connectors.oauth2 import OAuth2Handler


@pytest.fixture
def handler_with_kv(monkeypatch, kv):
    """Build an OAuth2Handler whose internal get_kv() lookups point
    at the test KV (DB9)."""
    import core.kv as _ckv
    monkeypatch.setattr(_ckv, "get_kv", lambda *a, **k: kv)
    return OAuth2Handler(), kv


def _state():
    return f"state-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_save_state_then_load_state_returns_payload(handler_with_kv):
    handler, kv = handler_with_kv
    state = _state()
    handler.save_state(state, user_id="u1", connector_name="slack", pkce_verifier="abc123")
    payload = handler.load_state(state)
    assert payload == {
        "user_id": "u1",
        "connector_name": "slack",
        "pkce_verifier": "abc123",
    }


def test_load_state_consumes_the_key(handler_with_kv):
    """A second load returns None — the key must be deleted on first read."""
    handler, kv = handler_with_kv
    state = _state()
    handler.save_state(state, user_id="u", connector_name="c", pkce_verifier="v")
    assert handler.load_state(state) is not None
    assert handler.load_state(state) is None


def test_load_state_missing_returns_none(handler_with_kv):
    handler, _ = handler_with_kv
    assert handler.load_state("does-not-exist") is None


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------

def test_save_state_sets_10_minute_ttl(handler_with_kv):
    handler, kv = handler_with_kv
    state = _state()
    handler.save_state(state, user_id="u", connector_name="c", pkce_verifier="v")
    ttl = kv.ttl(f"connector:oauth:state:{state}")
    assert 0 < ttl <= 600
    assert ttl >= 540  # within a minute of the full 10-min window


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------

def test_load_state_returns_none_on_kv_error(monkeypatch):
    """If KV access raises, load_state must swallow and return None
    so the calling router can issue a clean 400."""
    handler = OAuth2Handler()

    class _BadKV:
        def get(self, *a, **k):
            raise RuntimeError("KV down")

    import core.kv as _ckv
    monkeypatch.setattr(_ckv, "get_kv", lambda *a, **k: _BadKV())
    assert handler.load_state("anything") is None
