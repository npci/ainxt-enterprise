#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# ============================================================
# LDAP / Active Directory Integration
#
# Direct LDAP — no Keycloak or Azure AD dependency.
# Configurable via env vars (LDAP_URL, LDAP_BIND_DN, etc.)
#
# Key responsibilities:
#   1. authenticate_user()    — bind-validate credentials at login
#   2. get_user_attributes()  — fetch AD profile for a given email / samAccountName
#   3. sync_all_users()       — paged bulk fetch for nightly AD sync
#   4. resolve_band()         — map AD 'title' field → (band, band_level)
#                               via title_band_map ILIKE patterns in Postgres
#   5. build_user_hierarchy() — fetch reporting chain (4 levels) for product auto-membership
# ============================================================

from __future__ import annotations

import os
from typing import Any, Optional

from core.logger import logger, mask_email
from core.config import (
    LDAP_ENABLED,
    LDAP_URL,
    LDAP_BIND_DN,
    LDAP_BIND_PASSWORD,
    LDAP_BASE_DN,
    LDAP_USER_FILTER,
    LDAP_USE_SSL,
    LDAP_USE_STARTTLS,
    SECURITY_AD_GROUP,
    APPROVER_AD_GROUP,
)

# ldap3 is optional and is NOT in requirements.txt: it is LGPL-3.0-only, and a
# default install deliberately carries no LGPL-3.0.  See requirements-ldap.txt.
try:
    import ldap3
    import ldap3.utils.conv as _ldap_conv
    _LDAP3_AVAILABLE = True
except ImportError:
    _LDAP3_AVAILABLE = False
    logger.warning(
        "ldap3 not installed - LDAP authentication is disabled. "
        "Enable it with: pip install -r requirements-ldap.txt"
    )

# LDAP_ATTR_MAP: JSON object mapping AiNxt attribute names to your directory's
# attribute names. Defaults to Active Directory schema. Override for OpenLDAP,
# 389-DS, or other directories.
# Example: LDAP_ATTR_MAP='{"username":"uid","email":"mail","display_name":"cn"}'
_DEFAULT_ATTR_MAP = {
    "username":     "sAMAccountName",
    "email":        "mail",
    "display_name": "cn",
    "full_name":    "displayName",
    "title":        "title",
    "department":   "department",
    "manager":      "manager",
    "dn":           "distinguishedName",
    "groups":       "memberOf",
    "upn":          "userPrincipalName",
    "employee_id":  "employeeID",
}

import json as _json
_LDAP_ATTR_MAP: dict = _json.loads(os.getenv("LDAP_ATTR_MAP", "{}")) or _DEFAULT_ATTR_MAP

# The actual LDAP attribute names to fetch (values from the map)
_USER_ATTRS: list = list(_LDAP_ATTR_MAP.values())

# LDAP_LOGIN_ATTRS: comma-separated list of LDAP attributes to match against
# the login input. Defaults to AD's sAMAccountName and mail.
# Example: LDAP_LOGIN_ATTRS=uid,mail  (for OpenLDAP)
_login_attrs_raw = os.getenv("LDAP_LOGIN_ATTRS", "sAMAccountName,mail").split(",")
_LOGIN_ATTRS = [a.strip() for a in _login_attrs_raw if a.strip()]


def _mask_login(value: str) -> str:
    """Mask a submitted login identifier before it reaches the logs.

    Failed-login lines are written on every bad attempt, and users routinely
    type a password into the username box — so logging the raw value turns the
    application log into a store of mistyped credentials and a full list of
    attempted account names. Keeping the first and last character preserves
    enough shape to correlate repeated attempts during support triage.
    """
    text = (value or "").strip()
    if not text:
        return "<empty>"
    if len(text) <= 2:
        return "*" * len(text)
    return f"{text[0]}{'*' * (len(text) - 2)}{text[-1]}"


# ── Connection helpers ────────────────────────────────────────────────────────

# Module-level cached server AND service connection.
# Server:     created once per process — avoids repeated schema fetches.
# Connection: kept alive and reused across logins — eliminates TCP+TLS+bind
#             overhead on every login request (was adding 1-3s per login).
# get_info=NONE: never fetch AD schema on connect (was causing 30-50s delays).

_ldap_server:      "ldap3.Server | None"     = None
_ldap_service_conn: "ldap3.Connection | None" = None
_ldap_conn_lock = __import__("threading").Lock()

# Reduced timeouts — if AD can't respond in 3s it won't respond in 10s.
_CONNECT_TIMEOUT  = 3
_RECEIVE_TIMEOUT  = 5

# Socket errors that indicate the persistent connection was silently dropped
# by the AD server or an intermediate firewall/load-balancer.
# errno 104 = ECONNRESET (Connection reset by peer)
# errno  32 = EPIPE      (Broken pipe)
_STALE_CONN_ERRNOS = {32, 104}

# ── Profile cache (Redis db=0, TTL 1h) ───────────────────────────────────────
# Avoids repeat group/attribute LDAP round-trips on every request.
# Key: ldap_profile:{user_dn}   Value: JSON attrs dict

_PROFILE_CACHE_TTL = 3600  # 1 hour


def _profile_cache_key(user_dn: str) -> str:
    return f"ldap_profile:{__import__('hashlib').sha256(user_dn.encode()).hexdigest()[:32]}"


def _cache_user_profile(user_dn: str, attrs: dict) -> None:
    try:
        import json as _json
        from core.config import RDB_CACHE
        from core.kv import get_kv
        _r = get_kv(RDB_CACHE, decode_responses=True)
        _r.setex(_profile_cache_key(user_dn), _PROFILE_CACHE_TTL, _json.dumps(attrs))
    except Exception:
        pass


def _get_cached_profile(user_dn: str) -> "dict | None":
    try:
        import json as _json
        from core.config import RDB_CACHE
        from core.kv import get_kv
        _r = get_kv(RDB_CACHE, decode_responses=True)
        raw = _r.get(_profile_cache_key(user_dn))
        return _json.loads(raw) if raw else None
    except Exception:
        return None


def _get_server() -> "ldap3.Server":
    """Return a cached ldap3.Server object (created once per process)."""
    global _ldap_server
    if _ldap_server is None and _LDAP3_AVAILABLE:
        _ldap_server = ldap3.Server(
            LDAP_URL,
            use_ssl=LDAP_USE_SSL,
            get_info=ldap3.NONE,
            connect_timeout=_CONNECT_TIMEOUT,
        )
    return _ldap_server


def _resolve_auto_bind():
    """Resolve which ldap3 auto_bind mode to use, driven by LDAP_USE_SSL /
    LDAP_USE_STARTTLS rather than sniffing the URL scheme.

    LDAP_USE_SSL takes precedence when both are set: LDAPS already encrypts
    the transport, so negotiating StartTLS on top of it is meaningless.
    """
    if LDAP_USE_SSL:
        if LDAP_USE_STARTTLS:
            logger.warning(
                "LDAP: both LDAP_USE_SSL and LDAP_USE_STARTTLS are true — "
                "LDAP_USE_SSL takes precedence (LDAPS already encrypts the "
                "transport; StartTLS on top of it is meaningless)."
            )
        return ldap3.AUTO_BIND_NONE
    if LDAP_USE_STARTTLS:
        return ldap3.AUTO_BIND_TLS_BEFORE_BIND
    return ldap3.AUTO_BIND_NONE


def _close_service_conn() -> None:
    """Unbind and discard the cached service connection (call with lock held)."""
    global _ldap_service_conn
    if _ldap_service_conn is not None:
        try:
            _ldap_service_conn.unbind()
        except Exception:
            pass
        _ldap_service_conn = None


def _open_service_conn() -> "ldap3.Connection":
    """Create, bind, and cache a fresh service-account connection (call with lock held)."""
    global _ldap_service_conn
    conn = ldap3.Connection(
        _get_server(),
        user=LDAP_BIND_DN,
        password=LDAP_BIND_PASSWORD,
        authentication=ldap3.SIMPLE,
        auto_bind=_resolve_auto_bind(),
        raise_exceptions=True,
        receive_timeout=_RECEIVE_TIMEOUT,
    )
    if not conn.bound:
        conn.bind()
    _ldap_service_conn = conn
    logger.info("LDAP: service connection established")
    return _ldap_service_conn


def _service_conn() -> "ldap3.Connection":
    """Return a cached, reusable service account connection.
    Reconnects automatically if the connection has dropped.
    Thread-safe via module-level lock.
    """
    if not _LDAP3_AVAILABLE:
        raise RuntimeError("ldap3 not installed — run: pip install ldap3")
    with _ldap_conn_lock:
        # Reuse if still bound; reconnect if dropped or never created.
        # Note: .bound only tracks ldap3's internal state — it does NOT
        # detect a silently-dropped TCP socket.  Stale-socket errors are
        # caught at the call sites below and trigger an explicit reconnect.
        if _ldap_service_conn is None or not _ldap_service_conn.bound:
            _close_service_conn()
            _open_service_conn()
        return _ldap_service_conn


def _reset_service_conn() -> "ldap3.Connection":
    """Force-close the cached connection and open a fresh one.
    Called after a stale-socket error (ECONNRESET / EPIPE).
    Thread-safe via module-level lock.
    """
    with _ldap_conn_lock:
        _close_service_conn()
        return _open_service_conn()


def _safe_val(entry, attr: str) -> Optional[str]:
    """Extract a string value from an ldap3 entry attribute safely."""
    try:
        v = getattr(entry, attr, None)
        if v is None:
            return None
        return v.value if hasattr(v, "value") else str(v)
    except Exception:
        return None


def _safe_list(entry, attr: str) -> list[str]:
    """
    Extract a multi-value attribute from an ldap3 entry as a list of strings.
    Returns [] if the attribute is absent or on any error (e.g. memberOf).
    """
    try:
        v = getattr(entry, attr, None)
        if v is None:
            return []
        # ldap3 multi-value attributes expose .values as a list
        if hasattr(v, "values"):
            return [str(x) for x in v.values]
        # Scalar fallback — wrap in a list
        val = v.value if hasattr(v, "value") else str(v)
        return [str(val)] if val else []
    except Exception:
        return []


def _entry_to_dict(entry) -> dict[str, Any]:
    """Convert an ldap3 Entry to a plain dict with canonical field names."""
    return {
        "ad_username": _safe_val(entry, "sAMAccountName"),
        "email":       _safe_val(entry, "mail"),
        "name":        _safe_val(entry, "cn") or _safe_val(entry, "displayName"),
        "ad_title":    _safe_val(entry, "title"),
        "department":  _safe_val(entry, "department"),
        "manager_dn":  _safe_val(entry, "manager"),
        "ad_dn":       _safe_val(entry, "distinguishedName"),
        "upn":         _safe_val(entry, "userPrincipalName"),
        "member_of":   _safe_list(entry, "memberOf"),   # list of group DNs
        "employee_id": _safe_val(entry, "employeeID"),  # numeric employee ID from AD
    }


# ── Public API ────────────────────────────────────────────────────────────────

def _do_authenticate(username: str, password: str, conn: "ldap3.Connection") -> Optional[dict[str, Any]]:
    """
    Inner logic for authenticate_user — separated so the caller can retry
    with a fresh connection after a stale-socket error without duplicating code.
    """
    safe_user = _ldap_conv.escape_filter_chars(username)
    _attr_clauses = "".join(f"({attr}={safe_user})" for attr in _LOGIN_ATTRS)
    search_filter = (
        f"(&{LDAP_USER_FILTER}"
        f"(|{_attr_clauses}))"
    )
    conn.search(
        search_base=LDAP_BASE_DN,
        search_filter=search_filter,
        attributes=_USER_ATTRS,
    )
    if not conn.entries:
        logger.info(f"LDAP auth: user not found — {_mask_login(username)}")
        return None

    entry = conn.entries[0]
    user_dn = _safe_val(entry, "distinguishedName")
    if not user_dn:
        return None

    # Check profile cache BEFORE doing the user bind — avoids group lookup on repeat logins
    _cached = _get_cached_profile(user_dn)

    # Validate password by binding as the user — reuse cached server (no new TCP handshake)
    user_conn = ldap3.Connection(
        _get_server(),
        user=user_dn,
        password=password,
        authentication=ldap3.SIMPLE,
        raise_exceptions=False,
        receive_timeout=_RECEIVE_TIMEOUT,
    )
    if LDAP_USE_STARTTLS and not LDAP_USE_SSL:
        user_conn.open()
        if not user_conn.start_tls():
            logger.error("LDAP auth: StartTLS negotiation failed for user bind")
            # user_conn.open() above already established a live socket; if we
            # bail out here without closing it, that socket leaks for the
            # lifetime of the process (CWE-772 Improper Resource Shutdown or
            # Release). Close it, best-effort, before returning.
            try:
                user_conn.unbind()
            except Exception:  # pragma: no cover - best-effort close
                pass
            return None
    if not user_conn.bind():
        logger.info(f"LDAP auth: invalid password for {_mask_login(username)}")
        # Same rationale as above: a failed bind still leaves the socket
        # opened by user_conn.open()/start_tls() (or by bind() itself when
        # STARTTLS is not in use) alive unless we explicitly unbind it.
        try:
            user_conn.unbind()
        except Exception:  # pragma: no cover - best-effort close
            pass
        return None

    # Use cached profile if available (avoids slow group/attr fetch on repeat logins)
    if _cached:
        attrs = _cached
    else:
        attrs = _entry_to_dict(entry)
        # Resolve IS team and approver membership from AD groups
        member_of = attrs.get("member_of", [])
        attrs["is_security_team"] = any(
            SECURITY_AD_GROUP.lower() in g.lower() for g in member_of
        )
        attrs["is_approver"] = any(
            APPROVER_AD_GROUP.lower() in g.lower() for g in member_of
        )
        _cache_user_profile(user_dn, attrs)

    # ARCH-F-CORE-007: raw email addresses in INFO-level logs create a PII
    # exposure vector in log aggregators and SIEM systems. A SHA-256 prefix
    # is sufficient for correlation without persisting PII.
    import hashlib as _hashlib
    _email_raw = attrs.get("email") or ""
    _email_hint = _hashlib.sha256(_email_raw.encode()).hexdigest()[:16]
    logger.info(
        f"LDAP auth: success for {_mask_login(username)!r} (email_sha256_prefix={_email_hint}) "
        f"is_security_team={attrs['is_security_team']} "
        f"is_approver={attrs['is_approver']}"
    )
    return attrs


def authenticate_user(username: str, password: str) -> Optional[dict[str, Any]]:
    """
    Authenticate a user against LDAP (bind-validate pattern).
    1. Service account finds the user's DN by sAMAccountName or mail.
    2. Re-bind as the user with their password to validate credentials.
    Returns user attribute dict on success, None on failure.

    Stale-connection handling: if the cached service connection was silently
    dropped by the AD server or a network device (ECONNRESET / EPIPE), the
    socket error is caught, the connection is rebuilt once, and the attempt
    is retried transparently before giving up.
    """
    if not LDAP_ENABLED or not _LDAP3_AVAILABLE:
        return None

    for attempt in range(2):          # attempt 0 = normal; attempt 1 = after reconnect
        try:
            conn = _service_conn()
            return _do_authenticate(username, password, conn)

        except Exception as exc:
            # Detect stale-socket errors by errno embedded in the exception
            exc_errno = getattr(exc, "errno", None)
            if exc_errno is None:
                # ldap3 wraps socket errors in LDAPSocketSendError /
                # LDAPSocketReceiveError; the original OSError is the cause.
                cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
                exc_errno = getattr(cause, "errno", None)

            if attempt == 0 and exc_errno in _STALE_CONN_ERRNOS:
                logger.warning(
                    f"LDAP authenticate_user: stale connection detected "
                    f"(errno {exc_errno}), reconnecting and retrying for {username!r}"
                )
                try:
                    _reset_service_conn()
                except Exception as reconn_exc:
                    logger.error(f"LDAP authenticate_user: reconnect failed: {reconn_exc}")
                    return None
                continue   # retry with the fresh connection

            logger.error(f"LDAP authenticate_user failed for {mask_email(username)!r}: {exc}")
            return None


def get_user_attributes(identifier: str) -> Optional[dict[str, Any]]:
    """
    Fetch AD attributes for a user by email or sAMAccountName.
    Used during JWT refresh and profile sync.
    """
    if not LDAP_ENABLED or not _LDAP3_AVAILABLE:
        return None

    try:
        conn = _service_conn()
        safe_id = _ldap_conv.escape_filter_chars(identifier)
        _attr_clauses_id = "".join(f"({attr}={safe_id})" for attr in _LOGIN_ATTRS)
        search_filter = (
            f"(&{LDAP_USER_FILTER}"
            f"(|{_attr_clauses_id}))"
        )
        conn.search(
            search_base=LDAP_BASE_DN,
            search_filter=search_filter,
            attributes=_USER_ATTRS,
        )
        if not conn.entries:
            return None

        entry = conn.entries[0]
        user_dn = _safe_val(entry, "distinguishedName") or ""

        # Return cached profile if fresh
        if user_dn:
            _cached = _get_cached_profile(user_dn)
            if _cached:
                return _cached

        attrs = _entry_to_dict(entry)
        member_of = attrs.get("member_of", [])
        attrs["is_security_team"] = any(
            SECURITY_AD_GROUP.lower() in g.lower() for g in member_of
        )
        attrs["is_approver"] = any(
            APPROVER_AD_GROUP.lower() in g.lower() for g in member_of
        )
        if user_dn:
            _cache_user_profile(user_dn, attrs)
        return attrs
    except Exception as exc:
        logger.error(f"LDAP get_user_attributes failed for {identifier!r}: {exc}")
        return None


def sync_all_users(batch_size: int = 500) -> list[dict[str, Any]]:
    """
    Paged bulk fetch of ALL users from AD — called by workers/ad_sync.py nightly.
    Returns a list of user attribute dicts. Returns [] if LDAP disabled/unavailable.
    """
    if not LDAP_ENABLED or not _LDAP3_AVAILABLE:
        return []

    results: list[dict[str, Any]] = []
    try:
        conn = _service_conn()
        conn.search(
            search_base=LDAP_BASE_DN,
            search_filter=LDAP_USER_FILTER,
            attributes=_USER_ATTRS,
            paged_size=batch_size,
        )
        while True:
            for entry in conn.entries:
                d = _entry_to_dict(entry)
                if d.get("email"):   # skip entries with no email
                    results.append(d)

            # Continue paged search if server returned a continuation cookie
            cookie = (
                conn.result
                .get("controls", {})
                .get("1.2.840.113556.1.4.319", {})
                .get("value", {})
                .get("cookie")
            )
            if not cookie:
                break
            conn.search(
                search_base=LDAP_BASE_DN,
                search_filter=LDAP_USER_FILTER,
                attributes=_USER_ATTRS,
                paged_size=batch_size,
                paged_cookie=cookie,
            )

        logger.info(f"LDAP sync_all_users: fetched {len(results)} users")
        return results

    except Exception as exc:
        logger.error(f"LDAP sync_all_users failed: {exc}")
        return []


def build_manager_chain(user_dn: str, depth: int = 4) -> list[str]:
    """
    Walk the AD manager chain up to `depth` levels.
    Returns a list of email addresses [L1_manager, L2, L3, L4].
    Used to auto-assign product membership.
    """
    if not LDAP_ENABLED or not _LDAP3_AVAILABLE or not user_dn:
        return []

    chain: list[str] = []
    current_dn = user_dn

    try:
        conn = _service_conn()
        for _ in range(depth):
            conn.search(
                search_base=current_dn,
                search_filter="(objectClass=*)",
                search_scope=ldap3.BASE,
                attributes=["manager", "mail"],
            )
            if not conn.entries:
                break
            entry = conn.entries[0]
            manager_dn = _safe_val(entry, "manager")
            if not manager_dn or manager_dn == current_dn:
                break
            # Fetch manager's email
            conn.search(
                search_base=manager_dn,
                search_filter="(objectClass=*)",
                search_scope=ldap3.BASE,
                attributes=["mail", "sAMAccountName"],
            )
            if not conn.entries:
                break
            mgr_email = (
                _safe_val(conn.entries[0], "mail")
                or _safe_val(conn.entries[0], "sAMAccountName")
            )
            if mgr_email:
                chain.append(mgr_email)
            current_dn = manager_dn

    except Exception as exc:
        logger.warning(f"build_manager_chain failed for {user_dn!r}: {exc}")

    return chain


def resolve_band(title: str) -> tuple[str, int]:
    """
    Look up band and band_level for an AD 'title' field value.
    Queries title_band_map using ILIKE patterns, highest band_level first.
    Returns ("A1", 1) when no match — safe default for lowest band.
    """
    if not title:
        return "A1", 1

    try:
        from db.database import engine
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT band, band_level
                    FROM title_band_map
                    WHERE :title ILIKE title_pattern
                    ORDER BY band_level DESC
                    LIMIT 1
                """),
                {"title": title},
            ).fetchone()
        if row:
            return row[0], row[1]
    except Exception as exc:
        logger.warning(f"resolve_band: DB lookup failed for title={title!r}: {exc}")

    return "A1", 1
