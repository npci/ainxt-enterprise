# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AUTH DEPENDENCIES — FastAPI request-level auth helpers
# ============================================================

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth.jwt_handler import decode_token
from auth.api_key_auth import is_api_key, resolve_api_key

_bearer = HTTPBearer(auto_error=False)

# ── Profile cache (Redis-backed, TTL = 5 min) ──────────────────────────────────
# DAST fix: PII fields (email, name, department) are no longer stored in the JWT.
# They are fetched from the DB on first use per request and merged into the
# payload dict transparently so all existing route handlers continue to work.
_PROFILE_CACHE_TTL = 300   # 5 minutes — short enough to pick up role changes quickly

_PROFILE_CACHE_REDIS_DB = 8  # dedicated Redis DB for profile cache

# ── HOD lookup state ──────────────────────────────────────────────────────────
# Flag: True once we've logged the "department_hod_mapping table missing"
# warning. Set on first failure; never re-logged for the process lifetime so
# we don't spam logs on every request when the manual table doesn't exist.
_HOD_TABLE_MISSING_WARNED = False


def _lookup_hod_departments(email: str) -> list:
    """
    Resolve the list of department_name values this email heads.

    Returns:
      - []            if email is empty, the table is missing/malformed,
                       or no rows match.
      - list[str]     of department_name values (case-sensitive, exactly as
                       stored in users.department).

    Wraps the SELECT in try/except: on UndefinedTable/ProgrammingError the
    application MUST keep functioning. A single warning per process lifetime
    is emitted so production is informed without log spam.

    All column references are double-quoted so Postgres does not fold them
    to lowercase (the manual table uses snake_case + spaces).
    """
    global _HOD_TABLE_MISSING_WARNED

    if not email:
        return []

    # Once the table has been confirmed missing for this process, stop hitting
    # Postgres on every cache-miss request — the answer will never change
    # without a restart.
    if _HOD_TABLE_MISSING_WARNED:
        return []

    try:
        from db.database import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            rows = db.execute(
                text(
                    'SELECT "department_name" '
                    'FROM department_hod_mapping '
                    'WHERE lower("hod_email") = lower(:email)'
                ),
                {"email": email},
            ).fetchall()
            return [r[0] for r in rows if r and r[0]]
        finally:
            db.close()
    except Exception as exc:
        if not _HOD_TABLE_MISSING_WARNED:
            try:
                from core.logger import logger
                logger.warning(
                    "HOD lookup failed (table missing or malformed?). "
                    "Continuing with is_hod=False for all users. Error: %s",
                    exc,
                )
            except Exception:
                pass
            _HOD_TABLE_MISSING_WARNED = True
        return []


def _get_profile_cache():
    """Return a Redis client for the profile cache, or None if unavailable."""
    try:
        from core.config import redis_client as _rc
        r = _rc(db=_PROFILE_CACHE_REDIS_DB, decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None


def enrich_user_context(payload: dict) -> dict:
    """
    Merge server-side profile data (email, name, department) into a JWT payload dict.

    DAST fix: PII fields are NOT stored inside the JWT (base64-encoded, not encrypted).
    Instead, they are loaded here from a Redis-cached DB lookup keyed on the user's
    sub (UUID) and injected into the in-memory payload dict that route handlers receive.

    Call-sites are unchanged — they continue to access current_user["email"] etc.
    and always get a fresh, server-authoritative value.

    AUTHORIZATION CLAIMS (role, ad_level, is_security_team) are ALSO refreshed
    from the database here, deliberately OVERWRITING whatever the JWT carried.

    Why: a JWT is minted once at login and lives for JWT_EXPIRE_HOURS (24h by
    default), so its `role`/`ad_level` claims are a snapshot. ~27 authorization
    checks across the routers read these values via current_user.get("role") /
    ("ad_level"). That produced two concrete bugs:

      1. A user promoted to admin (or granted a level override) kept being
         refused for up to 24h. GET /auth/me reads the DB directly, so the UI
         rendered them as an admin and showed admin-only controls, while
         endpoints like GET /auth/users returned 403 — the user-search boxes on
         Level Overrides and Budget appeared broken with no visible reason.
      2. The dangerous direction: a user whose admin rights were REVOKED kept
         those rights in-token until it expired.

    The DB is the authoritative source for authorization, so it wins. Values are
    read in the same query and cached under the same 5-minute TTL as the profile
    fields, so this adds no extra round trip; a role change now takes effect
    within the cache TTL instead of requiring a re-login.

    Cache invalidation: The cache TTL is 5 minutes.  On role/profile changes the
    admin can wait up to 5 min, or the cache key can be explicitly deleted via
    DELETE rl:profile:<user_id> (see invalidate_profile_cache()).
    """
    import json as _json

    user_id = payload.get("sub")
    if not user_id:
        return payload

    # 1. Try Redis cache first
    rc = _get_profile_cache()
    cache_key = f"profile:{user_id}"
    if rc is not None:
        try:
            cached = rc.get(cache_key)
            if cached:
                profile = _json.loads(cached)
                payload.setdefault("email",      profile.get("email", ""))
                payload.setdefault("name",       profile.get("name", ""))
                payload.setdefault("department", profile.get("department", ""))
                payload.setdefault("is_hod",          bool(profile.get("is_hod", False)))
                payload.setdefault("hod_departments", list(profile.get("hod_departments") or []))
                # Authorization claims: assign, don't setdefault — the DB value
                # must override the JWT's stale snapshot. Only apply when the
                # cache entry actually carries them, so an entry written by an
                # older build (before these keys existed) falls back to the JWT
                # rather than silently demoting the caller to role="user".
                if profile.get("role") is not None:
                    payload["role"] = profile["role"]
                if profile.get("ad_level") is not None:
                    payload["ad_level"] = profile["ad_level"]
                    payload["can_approve"] = (
                        int(profile["ad_level"]) <= 3 or profile.get("role") == "admin"
                    )
                if profile.get("is_security_team") is not None:
                    payload["is_security_team"] = profile["is_security_team"]
                return payload
        except Exception:
            pass   # cache miss — fall through to DB

    # 2. DB lookup
    try:
        from db.database import SessionLocal
        from db.models import User
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.id == user_id).first()
            if u:
                email = u.email or ""
                # is_hod: primary check — at least one active user has
                # users.hod_email pointing at this email (direct reports assigned).
                # Secondary check — presence in department_hod_mapping as a
                # double-check / catch for HODs whose reports aren't yet populated
                # in users.hod_email (e.g. the 641 NULL users).
                # True if either condition holds.
                from sqlalchemy import text as _text
                hod_check = db.execute(
                    _text(
                        "SELECT 1 FROM users "
                        "WHERE lower(hod_email) = lower(:email) AND is_active = TRUE "
                        "LIMIT 1"
                    ),
                    {"email": email},
                ).fetchone()
                hod_departments = _lookup_hod_departments(email)
                is_hod_flag = (hod_check is not None) or bool(hod_departments)

                profile = {
                    "email":           email,
                    "name":            getattr(u, "name", "") or "",
                    "department":      getattr(u, "department", "") or "",
                    "is_hod":          is_hod_flag,
                    "hod_departments": hod_departments,
                    # Authorization claims — server-authoritative, see docstring.
                    "role":             getattr(u, "role", None) or "user",
                    "ad_level":         (getattr(u, "ad_level", None)
                                         if getattr(u, "ad_level", None) is not None else 6),
                    "is_security_team": bool(getattr(u, "is_security_team", False)),
                }
                # Populate cache
                if rc is not None:
                    try:
                        rc.setex(cache_key, _PROFILE_CACHE_TTL, _json.dumps(profile))
                    except Exception:
                        pass
                payload.setdefault("email",           profile["email"])
                payload.setdefault("name",            profile["name"])
                payload.setdefault("department",     profile["department"])
                payload.setdefault("is_hod",          profile["is_hod"])
                payload.setdefault("hod_departments", profile["hod_departments"])
                # Assign (not setdefault) so the DB overrides the stale JWT claim.
                payload["role"]             = profile["role"]
                payload["ad_level"]         = profile["ad_level"]
                payload["is_security_team"] = profile["is_security_team"]
                payload["can_approve"] = (
                    int(profile["ad_level"]) <= 3 or profile["role"] == "admin"
                )
        finally:
            db.close()
    except Exception:
        # Non-blocking — never fail auth due to profile enrichment errors
        pass

    # Final safety net: guarantee the keys exist even if the DB lookup failed.
    payload.setdefault("is_hod",          False)
    payload.setdefault("hod_departments", [])
    return payload


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    """
    Extract and validate the identity from:
      1. Authorization: Bearer <jwt>     — browser/CLI sessions
      2. Authorization: Bearer <api-key> — IDE integrations (Kilo Code, Cursor, etc.)
      3. auth_token httpOnly cookie      — browser sessions

    Returns a payload dict enriched with server-side profile data (email, name,
    department) fetched from a Redis-cached DB lookup.  The JWT itself contains
    only minimal authorization claims (sub, role, org_id, ad_level, etc.) — no PII.

    Raises HTTP 401 if token is missing or invalid.
    """
    token: Optional[str] = None

    if credentials and credentials.scheme.lower() == "bearer":
        token = credentials.credentials
    else:
        token = request.cookies.get("auth_token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )

    # ── Try JWT first ──────────────────────────────────────────────────────
    payload = decode_token(token)
    if payload is not None:
        # Enrich with PII from server-side cache/DB (not stored in JWT)
        return enrich_user_context(payload)

    # ── Fall back to API key (IDE integrations) ────────────────────────────
    if is_api_key(token):
        api_payload = resolve_api_key(token)
        if api_payload is not None:
            # API key payloads may already carry email/name from their own store
            return api_payload

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token expired or invalid",
    )


def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """
    Like get_current_user but additionally enforces the admin role.
    Raises HTTP 403 for non-admin users.
    """
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


def invalidate_profile_cache(user_id: str) -> None:
    """
    Evict the profile cache entry for a user.
    Call this whenever email, name, or department is updated in the DB so that
    the stale cached value is not served for up to the TTL window.
    """
    rc = _get_profile_cache()
    if rc is not None:
        try:
            rc.delete(f"profile:{user_id}")
        except Exception:
            pass
