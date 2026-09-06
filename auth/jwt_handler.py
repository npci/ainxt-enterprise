# SPDX-License-Identifier: MIT
# ============================================================
# JWT HANDLER — encode / decode HS256 tokens
# ============================================================
#
# DAST fix (concurrent sessions): Each token now carries a `sid` (session ID)
# claim.  decode_token() validates the sid against the session registry
# (auth/session_manager.py) to reject tokens whose session has been revoked
# or evicted by the concurrent-session limiter.

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from core.logger import logger

# No default secret — fail loudly in all environments.
# Prod startup is protected by validate_prod_config() in core/config.py.
_IDENTIFIER = os.getenv("JWT_SECRET") or os.getenv("SECRET_KEY")
if not _IDENTIFIER:
    # Raise at import time so misconfigured deployments crash immediately
    # rather than issuing tokens with a known default secret.
    raise ValueError(
        "JWT_SECRET environment variable must be set. "
        "No default is allowed — generate a 32+ char random secret."
    )
if len(_IDENTIFIER) < 32:
    # SEC-F-038: HS256 with a short secret is trivially brute-forceable
    # offline once a single valid token leaks.
    raise ValueError("JWT_SECRET is too short (must be at least 32 characters).")

ALGORITHM    = "HS256"
EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "24"))


# ── Token revocation (Redis blacklist) ───────────────────────
#
# On logout, store the jti (JWT ID) with TTL = remaining token lifetime.
# decode_token() checks this blacklist before returning a payload.
# Prevents stolen tokens from remaining valid after logout.

def _get_revocation_store():
    """Return a KV client for the JWT revocation blacklist (DB=5).

    Backend selected via REDIS_CLIENT_CONFIG_DB5.
    """
    try:
        from core.config import RDB_QUEUE
        from core.kv import get_kv
        return get_kv(RDB_QUEUE, decode_responses=True)
    except Exception:
        return None


def encode_token(
    user_id: str,
    role: str,
    # PII fields accepted for backward-compat call-sites but NOT embedded in the JWT.
    # They are stored server-side and fetched on demand via enrich_user_context().
    # DAST fix: "JWT payload includes sensitive data such as personal identifiers
    # (email, name, department). JWTs are only base64-encoded, not encrypted."
    email: str = "",        # accepted, ignored — not stored in JWT
    org_id: str = "",
    is_security_team: bool = False,
    ad_level: int = 6,
    department: str = "",   # accepted, ignored — not stored in JWT
    name: str = "",         # accepted, ignored — not stored in JWT
    # DAST fix (concurrent sessions): caller may supply a pre-generated session_id
    # so that the same ID is used in both the token and the session registry.
    session_id: Optional[str] = None,
) -> str:
    """
    Create a signed JWT for the given user.

    SECURITY (DAST fix — sensitive data in JWT):
    The JWT carries ONLY the minimum authorization claims required for
    stateless RBAC/ABAC decisions:
        sub, role, org_id, is_security_team, ad_level, can_approve, jti, sid, iat, exp

    PII fields (email, name, department) are intentionally OMITTED from the
    JWT payload.  JWTs are base64-encoded, not encrypted, so any data inside
    is readable by anyone who intercepts the token.

    Profile data (email, name, department) is stored exclusively in the
    database and served from the server-authoritative /auth/me endpoint or
    fetched via enrich_user_context() (Redis-cached DB lookup) when needed
    by request handlers.

    SECURITY (DAST fix — concurrent sessions):
    The `sid` claim is a unique session identifier registered in the session
    registry on login.  decode_token() validates the sid on every request so
    that evicted or explicitly-revoked sessions are rejected even if the JWT
    itself has not yet expired.
    """
    now = datetime.now(timezone.utc)
    sid = session_id or str(uuid.uuid4())
    payload = {
        "sub":              user_id,
        "role":             role,
        "org_id":           org_id,
        "is_security_team": is_security_team,
        # AD-level ABAC claims — authorization, not PII
        "ad_level":         ad_level,
        "can_approve":      ad_level <= int(os.getenv("APPROVAL_AD_LEVEL", "6")) or role == "admin",
        "jti":              str(uuid.uuid4()),
        # Session ID — validated against session registry on every request
        # (DAST fix: concurrent session control)
        "sid":              sid,
        "iat":              now,
        "exp":              now + timedelta(hours=EXPIRE_HOURS),
    }
    return jwt.encode(payload, _IDENTIFIER, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """
    Decode and validate a JWT.
    Returns the payload dict or None if invalid/expired/revoked/session-invalid.

    Security guarantees:
    - Signature is always verified (algorithm pinned to HS256).
    - Expiry (exp) is always verified by the library.
    - Required claims (sub, iat, exp, jti) must be present.
    - Revoked tokens (Redis blacklist) are rejected; if Redis is
      unavailable the token is DENIED to avoid fail-open on auth.
    - Session validity is checked via the session registry (DAST fix:
      concurrent session control).  Evicted/revoked sessions are denied
      even if the JWT has not yet expired.
    """
    try:
        payload = jwt.decode(
            token,
            _IDENTIFIER,
            algorithms=[ALGORITHM],
            options={"require": ["sub", "iat", "exp", "jti"]},
        )
    except jwt.ExpiredSignatureError:
        return None
    except jwt.MissingRequiredClaimError:
        return None
    except jwt.InvalidTokenError:
        return None

    # Reject tokens whose algorithm header has been tampered
    # (extra defence: PyJWT already rejects non-HS256 via algorithms=[])
    # Belt-and-suspenders: ensure sub is non-empty
    if not payload.get("sub"):
        return None

    # Check revocation blacklist (fail-closed: deny if Redis is unreachable)
    jti = payload.get("jti")
    if jti:
        try:
            r = _get_revocation_store()
            if r is None:
                # Redis unavailable — deny to prevent revoked-token bypass
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "jwt_handler: Redis revocation store unavailable; "
                    "denying token jti=%s to fail-closed", jti
                )
                return None
            if r.exists(f"jwt:revoked:{jti}"):
                return None   # token has been explicitly logged out
        except Exception:
            # Any unexpected error → deny (fail-closed)
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "jwt_handler: revocation check error; denying token jti=%s", jti
            )
            return None

    # ── DAST fix: concurrent session check ────────────────────────────────────
    # Tokens issued before the session registry was deployed will not carry
    # a `sid` claim.  We allow those through to avoid breaking existing
    # logged-in users on deployment; new logins always get a `sid`.
    sid = payload.get("sid")
    if sid:
        try:
            from auth.session_manager import is_session_active
            if not is_session_active(payload["sub"], sid):
                import logging as _logging
                _logging.getLogger(__name__).info(
                    "jwt_handler: session %s for user %s is no longer active "
                    "(evicted or explicitly revoked)", sid, payload["sub"]
                )
                return None   # session was evicted or explicitly revoked
        except Exception as _exc:
            # session_manager import/runtime error — log and continue
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "jwt_handler: session check error for sid=%s: %s; allowing token", sid, _exc
            )

    return payload


def revoke_token(token: str) -> bool:
    """
    Revoke a JWT by adding its jti to the Redis blacklist with TTL = remaining lifetime.
    Called on logout. Returns True if successfully revoked.
    """
    try:
        payload = jwt.decode(
            token, _IDENTIFIER, algorithms=[ALGORITHM],
            options={"verify_exp": False},  # decode even if expired
        )
        jti = payload.get("jti")
        exp = payload.get("exp")
        if not jti:
            return False

        ttl = max(1, int(exp - datetime.now(timezone.utc).timestamp())) if exp else EXPIRE_HOURS * 3600
        r = _get_revocation_store()
        if r:
            r.setex(f"jwt:revoked:{jti}", ttl, "1")
            return True
    except Exception:
        pass
    return False
