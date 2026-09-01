# SPDX-License-Identifier: Apache-2.0
# ============================================================
# SSO SESSION STORE — KV-backed token management
# db=2 (shared with memory layer). Backend (Redis)
# is selected via REDIS_CLIENT_CONFIG_DB2.
# ============================================================

import json
import time
from typing import Optional

from core.config import RDB_WORKFLOW
from core.kv import get_kv, KVError
from core.logger import logger

_kv = None


def _get_redis():
    """Return a cached KV client for the SSO session DB.

    Name retained for backwards compatibility; the returned object is
    a KVClient (not a redis.Redis) but exposes the same set/get/setex/
    delete surface used here.
    """
    global _kv
    if _kv is None:
        try:
            client = get_kv(RDB_WORKFLOW, decode_responses=True)
            client.ping()
            _kv = client
        except KVError as e:
            logger.warning(f"sso_sessions: KV backend unavailable → {e}")
    return _kv


def _key(user_id: str) -> str:
    return f"sso_session:{user_id}"


# SEC-F-MISC-005: SSO access/refresh tokens were previously stored as plaintext
# JSON in Redis — anyone with Redis read access could harvest all active SSO
# tokens. Encrypt with the existing credential-vault infrastructure
# (AES-256-GCM as of SEC-F-020/032, transparently decrypt-compatible with
# older Fernet-encrypted sessions) before storing. Backwards compatible:
# existing plaintext sessions are still readable on first access; the next
# write rewrites them encrypted.
def _encode_session(session: dict) -> str:
    from store.credential_vault import encrypt_value
    return encrypt_value(json.dumps(session))


def _decode_session(raw: str) -> dict:
    from store.credential_vault import decrypt_value
    try:
        return json.loads(decrypt_value(raw))
    except Exception:
        return json.loads(raw)  # backwards compat with plaintext


# ── Create ────────────────────────────────────────────────────

def create_sso_session(
    user_id:       str,
    provider:      str,
    access_token:  str,
    refresh_token: str,
    expires_at:    float,   # Unix timestamp
) -> dict:
    """Store SSO session data in Redis. TTL = expires_at + 1 hour buffer."""
    session = {
        "user_id":       user_id,
        "provider":      provider,
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "expires_at":    expires_at,
        "created_at":    time.time(),
    }
    rc = _get_redis()
    if rc:
        ttl = max(int(expires_at - time.time()) + 3600, 3600)
        rc.setex(_key(user_id), ttl, _encode_session(session))
        logger.info(f"sso_sessions: created session for {user_id} provider={provider}")
    return session


# ── Get ───────────────────────────────────────────────────────

def get_sso_session(user_id: str) -> Optional[dict]:
    """Retrieve an active SSO session, or None if expired/missing."""
    rc = _get_redis()
    if not rc:
        return None
    raw = rc.get(_key(user_id))
    if not raw:
        return None
    session = _decode_session(raw)
    return session


# ── Refresh ───────────────────────────────────────────────────

def refresh_sso_token(user_id: str) -> Optional[dict]:
    """
    Refresh the access token using the stored refresh_token.
    Calls the SSO provider token endpoint and updates Redis.
    Returns updated session or None on failure.
    """
    session = get_sso_session(user_id)
    if not session:
        logger.warning(f"sso_sessions: no session found for {user_id}")
        return None

    provider = session.get("provider", "")
    refresh_token = session.get("refresh_token", "")

    if not refresh_token:
        logger.warning(f"sso_sessions: no refresh_token for {user_id}")
        return None

    try:
        from auth.sso import refresh_token as _refresh_token
        new_tokens = _refresh_token(provider, refresh_token)
        if not new_tokens:
            return None
    except Exception as e:
        logger.error(f"sso_sessions: token refresh failed → {e}")
        return None

    # Update session with new tokens
    session["access_token"]  = new_tokens.get("access_token", session["access_token"])
    session["refresh_token"] = new_tokens.get("refresh_token", refresh_token)
    expires_in = new_tokens.get("expires_in", 3600)
    session["expires_at"] = time.time() + expires_in

    rc = _get_redis()
    if rc:
        ttl = expires_in + 3600
        rc.setex(_key(user_id), ttl, _encode_session(session))
        logger.info(f"sso_sessions: refreshed token for {user_id}")

    return session


# ── Revoke ────────────────────────────────────────────────────

def revoke_sso_session(user_id: str) -> bool:
    """Delete the SSO session for a user."""
    rc = _get_redis()
    if not rc:
        return False
    deleted = rc.delete(_key(user_id))
    if deleted:
        logger.info(f"sso_sessions: revoked session for {user_id}")
    return bool(deleted)
