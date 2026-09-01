# SPDX-License-Identifier: Apache-2.0
# ============================================================
# auth/jwt_handler.py KV-contract tests (Phase 4).
#
# Risk closed: revoke_token writes `jwt:revoked:{jti}` with TTL =
# remaining token lifetime. If a backend's exists()/setex() semantics
# diverge from the KV contract, revoked tokens could be silently
# accepted — these tests pin the semantics we depend on.
#
# jwt_handler raises at import time if JWT_SECRET is unset, so we
# guarantee it via env var, then importlib.reload(). Each test uses
# a unique payload (uuid) to avoid jti collisions in parallel runs.
# ============================================================

from __future__ import annotations

import importlib
import os
import uuid

import pytest


@pytest.fixture
def jwt_handler(monkeypatch, kv):
    """Reload auth.jwt_handler with JWT_SECRET set and bind the
    revocation store to the test KV (kv fixture is on DB9)."""
    monkeypatch.setenv("JWT_SECRET", "test-secret-" + uuid.uuid4().hex)
    import auth.jwt_handler as jh
    jh = importlib.reload(jh)
    # Force _get_revocation_store to return the test KV.
    monkeypatch.setattr(jh, "_get_revocation_store", lambda: kv)
    yield jh


def _payload_kwargs():
    """Distinct values per test so jti, sub, etc. don't collide."""
    return dict(
        user_id=f"user-{uuid.uuid4().hex[:8]}",
        role="user",
        email="x@example.com",
    )


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_encode_decode_round_trip(jwt_handler):
    tok = jwt_handler.encode_token(**_payload_kwargs())
    payload = jwt_handler.decode_token(tok)
    assert payload is not None
    assert payload["sub"].startswith("user-")
    assert payload["role"] == "user"


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------

def test_revoke_then_decode_returns_none(jwt_handler, kv):
    tok = jwt_handler.encode_token(**_payload_kwargs())
    assert jwt_handler.revoke_token(tok) is True
    # Same token now fails to decode.
    assert jwt_handler.decode_token(tok) is None


def test_revoke_sets_remaining_lifetime_ttl(jwt_handler, kv):
    """The blacklist key TTL must be ≤ EXPIRE_HOURS*3600 and > 0."""
    tok = jwt_handler.encode_token(**_payload_kwargs())
    assert jwt_handler.revoke_token(tok) is True

    # Recover jti from the freshly issued token.
    import jwt
    payload = jwt.decode(tok, jwt_handler._IDENTIFIER,
                         algorithms=[jwt_handler.ALGORITHM],
                         options={"verify_exp": False})
    ttl = kv.ttl(f"jwt:revoked:{payload['jti']}")
    upper = jwt_handler.EXPIRE_HOURS * 3600
    assert 0 < ttl <= upper, f"jwt:revoked:* ttl {ttl} out of (0, {upper}]"


def test_decode_fails_open_when_revocation_store_down(jwt_handler, monkeypatch):
    """If the blacklist KV is unreachable, decode_token still accepts
    valid tokens (fail-open per the docstring)."""
    tok = jwt_handler.encode_token(**_payload_kwargs())
    # Simulate KV down.
    monkeypatch.setattr(jwt_handler, "_get_revocation_store", lambda: None)
    assert jwt_handler.decode_token(tok) is not None


def test_revoke_returns_false_for_malformed_token(jwt_handler):
    assert jwt_handler.revoke_token("not.a.jwt") is False


def test_revoke_idempotent(jwt_handler, kv):
    """Revoking the same token twice does not break anything."""
    tok = jwt_handler.encode_token(**_payload_kwargs())
    assert jwt_handler.revoke_token(tok) is True
    assert jwt_handler.revoke_token(tok) is True
    assert jwt_handler.decode_token(tok) is None
