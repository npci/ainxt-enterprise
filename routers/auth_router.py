# SPDX-License-Identifier: MIT
# ============================================================
# AUTH ROUTER — register, login, me
# POST /auth/register
# POST /auth/login
# GET  /auth/me
# ============================================================

import os
import re
import threading
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from auth.dependencies import get_current_user, invalidate_profile_cache
from auth.rbac import require_role
from core.logger import logger
from core.security_validation import (
    validate_level_override_request,
    validate_register_request,
    validate_login_request,
)
from core.pii_crypto import encrypt_pii, decrypt_pii, pii_encryption_enabled
from core.security_validation import validate_level_override_request
from core.rate_limiter import (
    enforce_rate_limit,
    AUTH_LOGIN,
    AUTH_REGISTER,
    AUTH_REFRESH,
    AUTH_FORGOT_PASSWORD,
    SSO_CALLBACK,
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ============================================================
# UI CONFIG — public endpoint, no auth required.
# Returns feature flags the frontend needs before login.
# GET /auth/ui-config
# ============================================================

def _google_login_enabled() -> bool:
    """Whether to advertise "Continue with Google" to the login page.

    Never raises: /auth/ui-config is public, pre-auth, and is what the login
    page blocks on. google_login_enabled() reads the gmail connector row, so a
    DB hiccup must degrade to "no button", not to a 500 that leaves a user
    unable to sign in with a password either.
    """
    try:
        from auth.google_auth import google_login_enabled
        return google_login_enabled()
    except Exception as exc:
        logger.debug("ui_config: Google sign-in probe failed (non-fatal): %s", exc)
        return False


@router.get("/ui-config")
def ui_config():
    """Return UI feature flags for the frontend.

    Called by the login page and other pre-auth components.
    No authentication required — these are display-only flags,
    not sensitive data.
    """
    from store.budget_store import BASE_COST_USD
    from core.config import (
        INTERNAL_USE_ONLY, LDAP_ENABLED,
        APPROVAL_AD_LEVEL, DEFAULT_AD_LEVEL,
        ENABLE_COACH, ENABLE_DISCUSSIONS,
        ENABLE_ZOHO_HR, ENABLE_HOD_DIGEST, ENABLE_LLM_SPEND_REPORT,
        ENABLE_MONTHLY_STATEMENT, ENABLE_BROADCAST,
        ENABLE_GRAPH_WEBHOOKS, ENABLE_WEBHOOKS,
        ENABLE_SLACK, ENABLE_TEAMS,
        ENABLE_SELF_REGISTRATION,
        ENABLE_FORGOT_PASSWORD,
        SCM_PROVIDER,
        PLATFORM_NAME,
        INPUT_SANITIZATION_ENABLED,
        HOD_APPROVAL_ENABLED,
    )
    # smtp_configured tells the frontend which delivery message to show
    # on the forgot-password form ("check your email" vs "check server console").
    _smtp_configured = bool(os.getenv("AINXT_SMTP_HOST", "").strip())
    return {
        "internal_use_only":          INTERNAL_USE_ONLY,
        "ldap_enabled":               LDAP_ENABLED,
        "approval_ad_level":          APPROVAL_AD_LEVEL,
        "default_ad_level":           DEFAULT_AD_LEVEL,
        "enable_coach":               ENABLE_COACH,
        "enable_discussions":         ENABLE_DISCUSSIONS,
        "enable_zoho_hr":             ENABLE_ZOHO_HR,
        "enable_hod_digest":          ENABLE_HOD_DIGEST,
        # Master toggle for the department/HOD approval hierarchy. False (the
        # default) means flat/admin-only mode across budget, SDLC, endpoints,
        # product-catalog, and governance approvals — see core/config.py.
        "hod_approval_enabled":       HOD_APPROVAL_ENABLED,
        # Platform "base" budget allocation every user is seeded with / reset
        # to monthly (store.budget_store.BASE_COST_USD). Default $50,
        # configurable via the BUDGET_BASE_COST_USD env var. Read live so the
        # UI's hardcoded-$50 copy can be replaced with the actual value.
        "budget_base_cost_usd":       BASE_COST_USD,
        "enable_llm_spend_report":    ENABLE_LLM_SPEND_REPORT,
        "enable_monthly_statement":   ENABLE_MONTHLY_STATEMENT,
        "enable_broadcast":           ENABLE_BROADCAST,
        "enable_graph_webhooks":      ENABLE_GRAPH_WEBHOOKS,
        "enable_webhooks":            ENABLE_WEBHOOKS,
        "enable_slack":               ENABLE_SLACK,
        "enable_teams":               ENABLE_TEAMS,
        # GAP-3: self-registration — true by default; disable it for a closed deployment
        "self_registration_enabled":  ENABLE_SELF_REGISTRATION,
        # GAP-6: forgot password — true by default; disable it where identity is directory-managed
        "forgot_password_enabled":    ENABLE_FORGOT_PASSWORD,
        # "Continue with Google" — false unless ENABLE_GOOGLE_LOGIN=true AND the
        # gmail connector's OAuth client env vars are actually populated, so the
        # button never renders on a deployment that could not complete the flow.
        "google_login_enabled":       _google_login_enabled(),
        "smtp_configured":            _smtp_configured,
        "scm_provider":               SCM_PROVIDER,   # "github" (default) or "gitlab"
        # Organisation display name — shown in login page footer and UI surfaces.
        # Default: "AiNxt". Set PLATFORM_NAME to your own organisation name.
        "platform_name":              PLATFORM_NAME,
        # Same kill-switch that gates every validator in core/security_validation.py.
        # The frontend's client-side pre-checks (ai-ui/src/utils/securityValidation.js)
        # read this to decide whether to run at all — when false, the UI-side
        # checks become pure pass-throughs too, matching the backend exactly.
        "input_sanitization_enabled": INPUT_SANITIZATION_ENABLED,
        # PII payload encryption (core/pii_crypto.py) — tells the frontend whether
        # to encrypt outgoing PII fields and decrypt "pii:v1:" response fields.
        # Read live (not cached) since it's a runtime env flag, same as every
        # other flag on this endpoint.
        "pii_payload_encryption_enabled": pii_encryption_enabled(),
    }


def _decrypt_password(value: str) -> str:
    """AES-GCM decrypt a login password if LOGIN_ENCRYPT_KEY is set in env.
    Payload format: base64(iv[12] || ciphertext).
    Falls back to returning value as-is if key is absent or decryption fails."""
    import base64
    key_b64 = os.getenv("LOGIN_ENCRYPT_KEY", "")
    if not key_b64:
        return value
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        combined = base64.b64decode(value)
        iv, ciphertext = combined[:12], combined[12:]
        return AESGCM(base64.b64decode(key_b64)).decrypt(iv, ciphertext, None).decode()
    except Exception:
        return value  # not encrypted or wrong key — pass through


def _sync_user_from_org_tree(user_id: str, email: str, *, skip_department_fields: bool = False):
    """Sync ad_level + department from org_tree on login (background thread, non-blocking).
    After AD sync, re-applies any active user_level_overrides so nightly TRUNCATE+INSERT
    does not strip manually granted temporary promotions.

    `skip_department_fields`: when True, only `ad_level` (+ override re-apply) is
    synced — `department`/`ad_title`/`manager_dn` are left untouched because the
    live-AD staleness check (services/user_directory_sync.py) already wrote fresher
    values for this login. This is what prevents this org_tree snapshot (which can
    be a stale nightly CSV import) from silently clobbering the just-written live-AD
    values — see _run_post_login_sync() below for the ordering that makes this safe.
    """
    try:
        from db.database import SessionLocal
        from db.models import User, OrgTree, UserLevelOverride
        from datetime import datetime as _dt, timedelta as _td
        db = SessionLocal()
        try:
            ot = db.query(OrgTree).filter(OrgTree.mail == email.lower()).first()
            if ot:
                u = db.query(User).filter(User.id == user_id).first()
                if u:
                    u.ad_level = ot.level
                    if not skip_department_fields:
                        u.department = ot.department
                        u.ad_title   = ot.title
                        u.manager_dn = ot.manager
                    u.last_ad_sync = _dt.utcnow()
                    db.commit()
                    # Re-apply active override on top of AD sync (survives nightly truncate).
                    # expires_at is stored as literal wall-clock digits the grantor typed
                    # (no UTC conversion — see create_level_override), so compare against
                    # local wall-clock "now" (utcnow() + 5:30), not utcnow() itself.
                    _now_local = _dt.utcnow() + _td(hours=5, minutes=30)
                    override = (    
                        db.query(UserLevelOverride)
                        .filter(
                            UserLevelOverride.user_id == u.id,
                            UserLevelOverride.is_active == True,
                        )
                        .filter(
                            (UserLevelOverride.expires_at == None) |
                            (UserLevelOverride.expires_at > _now_local)
                        )
                        .order_by(UserLevelOverride.created_at.desc())
                        .first()
                    )
                    if override:
                        u.ad_level = override.ad_level_override
                        db.commit()
        finally:
            db.close()
    except Exception:
        pass  # non-blocking — never fail login due to org_tree sync errors


def _run_post_login_sync(user_id: str, email: str, ldap_attrs: Optional[dict]):
    """Run both post-login background syncs sequentially, in one thread, so
    they can never race and clobber each other's writes to the same `users`
    row (department/manager_dn/ad_title).

    Order matters: the live-AD check runs FIRST because it's the freshest
    source (a real-time LDAP lookup, vs. org_tree's nightly CSV snapshot).
    If it actually changed department/manager, the org_tree sync that follows
    is told to skip those same fields so it can't overwrite them with stale
    data — it still applies ad_level + override re-application, which is a
    separate concern org_tree remains the source of truth for.

    `ldap_attrs`: the attrs dict already returned by auth.ldap_handler.
    authenticate_user() during this login (or None when LDAP_ENABLED is
    false) — passed through so the live-AD check reuses it instead of
    issuing a second LDAP search for the same user.
    """
    from core.config import LDAP_ENABLED

    wrote_department_fields = False
    if LDAP_ENABLED:
        try:
            from services.user_directory_sync import sync_user_from_ldap_and_org_tree
            wrote_department_fields = sync_user_from_ldap_and_org_tree(
                user_id, email, ldap_attrs=ldap_attrs,
            )
        except Exception:
            pass  # non-blocking — never fail login due to live-AD sync errors

    _sync_user_from_org_tree(
        user_id, email, skip_department_fields=wrote_department_fields,
    )

# httpOnly cookie is Secure only when running in production (HTTPS).
_COOKIE_SECURE = os.getenv("DEPLOYMENT_MODE", "local") not in ("local", "development", "dev")


# ── Session helpers ───────────────────────────────────────────────────────────

def _parse_device_hint(user_agent: str) -> str:
    """
    Extract a human-readable device/browser hint from the User-Agent string.
    Returns a short label such as "Chrome / Windows" or "Safari / iPhone".
    Never raises — returns empty string on any error.
    """
    if not user_agent:
        return ""
    try:
        ua = user_agent.lower()
        # Browser
        if "edg/" in ua or "edge/" in ua:
            browser = "Edge"
        elif "opr/" in ua or "opera" in ua:
            browser = "Opera"
        elif "chrome/" in ua and "chromium" not in ua:
            browser = "Chrome"
        elif "firefox/" in ua:
            browser = "Firefox"
        elif "safari/" in ua and "chrome" not in ua:
            browser = "Safari"
        else:
            browser = "Browser"

        # OS / device
        if "iphone" in ua:
            os_name = "iPhone"
        elif "ipad" in ua:
            os_name = "iPad"
        elif "android" in ua:
            os_name = "Android"
        elif "windows" in ua:
            os_name = "Windows"
        elif "macintosh" in ua or "mac os" in ua:
            os_name = "macOS"
        elif "linux" in ua:
            os_name = "Linux"
        else:
            os_name = "Unknown OS"
        return f"{browser} / {os_name}"
    except Exception:
        return ""


def _notify_new_login(user_id: str, ip: str, device: str, user_agent: str) -> None:
    """
    Send an in-app inbox notification to alert the user about a new login.

    DAST fix: "Monitor login activity and notify users when new logins occur
    to help detect unauthorized access attempts."

    Fire-and-forget (background thread) — never blocks the login response.
    """
    def _send():
        try:
            from store.inbox_store import publish_inbox_item
            import datetime as _dt
            ts = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            device_str = device or "Unknown device"
            ip_str     = ip     or "Unknown IP"
            publish_inbox_item(
                user_id  = user_id,
                type     = "security_alert",
                title    = "New login to your account",
                body     = (
                    f"A new login was detected on your account.\n\n"
                    f"**Time:** {ts}\n"
                    f"**Device:** {device_str}\n"
                    f"**IP Address:** {ip_str}\n\n"
                    f"If this was not you, please sign out all sessions immediately "
                    f"from **Profile → Active Sessions** and change your password."
                ),
                source_id = "",
                metadata  = {
                    "entity_type": "security_alert",
                    "ip":          ip_str,
                    "device":      device_str,
                    "user_agent":  user_agent[:200] if user_agent else "",
                    "timestamp":   ts,
                },
            )
        except Exception as _e:
            logger.debug("_notify_new_login: %s", _e)

    threading.Thread(target=_send, daemon=True).start()


def _set_auth_cookie(response: Response, token: str, *, samesite: str = "strict") -> None:
    """Attach the JWT as an httpOnly cookie.

    SEC-F-008 (implemented, not deferred): default is now SameSite=Strict,
    hardened from the previous Lax. Strict was validated and found to break
    ONE specific path — the SSO browser callback (auth/sso.py::sso_callback,
    GET /auth/sso/callback), which sets this cookie on a 302 RedirectResponse
    returned directly to the identity provider's cross-site top-level
    navigation back to us. A Strict cookie is dropped by the browser on
    exactly that kind of cross-site navigation, so the SPA's follow-up
    /auth/me call found no cookie and failed to re-hydrate the session.

    Every OTHER caller of this function (/auth/login, /auth/register,
    /auth/refresh, POST /auth/sso/callback) sets this cookie on a normal
    same-origin XHR/fetch response, not a cross-site redirect landing — Strict
    works correctly for all of them, and is the safer default for a platform
    that has no legitimate cross-site cookie-sending need outside that one
    redirect. Only the SSO GET-callback redirect explicitly opts into
    samesite="lax" (still a real hardening improvement over the old
    blanket-Lax default, and the minimum relaxation needed for the redirect
    to work) — see the call site in auth/sso.py for why.
    """
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        samesite=samesite,
        secure=_COOKIE_SECURE,
        path="/",
        max_age=86400,  # 24 h — matches JWT expiry
    )


# ============================================================
# REQUEST / RESPONSE MODELS
# ============================================================

class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str
    # Accepted for backward compat with existing clients, but register()
    # rejects any value other than "user" — self-registration can never
    # create an admin account (see register()'s role-validation guard).
    role: str = "user"
    org_id: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    # The JWT is delivered via an httpOnly cookie for browser clients (XSS protection).
    # access_token is also included in the body for non-browser clients (CLI, API).
    # Browser clients should use the cookie; CLI clients should use access_token.
    access_token: str = ""
    user_id: str
    role: str
    email: str
    name: str
    ad_level: int = 6
    department: str = ""
    can_approve: bool = False


class LoginResponse(BaseModel):
    # SECURITY (DAST fix — privilege escalation via response manipulation):
    # The login response body intentionally does NOT include role, ad_level,
    # can_approve, or any other privilege/permission fields.
    #
    # Rationale: these fields in the response body are the direct target of
    # response-manipulation attacks — an attacker can intercept and change
    # "role":"user" → "role":"admin" to escalate privileges on the client side.
    #
    # The JWT (httpOnly cookie) already carries all authorization claims,
    # cryptographically signed.  Browser clients MUST call GET /auth/me after
    # login to obtain user profile data — that response is derived from the
    # validated JWT, not from manipulable response fields.
    #
    # Non-browser clients (CLI, API integrations) receive access_token in the
    # body and must decode the JWT locally or call /auth/me.
    access_token: str = ""   # empty for browser clients (cookie-based); populated for CLI/API
    user_id: str             # safe to expose — not a privilege claim
    email: str               # safe to expose — display/greeting purpose only
    name: str                # safe to expose — display purpose only


# ============================================================
# REGISTER
# ============================================================

@router.post("/register", response_model=TokenResponse, status_code=201)
def register(body: RegisterRequest, request: Request, response: Response):
    # ── Rate limit: 5 registrations per 10 minutes per IP ────────────────────
    enforce_rate_limit(request, AUTH_REGISTER)

    # ── GAP-3 Guard 1: feature flag ───────────────────────────────────────────
    # ENABLE_SELF_REGISTRATION=false — block all self-registration.
    # Enforced here at the API level so even direct API calls are rejected,
    # not just the UI (which hides the link when the flag is false).
    from core.config import ENABLE_SELF_REGISTRATION, DEFAULT_AD_LEVEL, HOD_APPROVAL_ENABLED
    if not ENABLE_SELF_REGISTRATION:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Self-registration is disabled on this platform. Contact your administrator.",
        )

    # ── GAP-3 Guard 2: role validation ────────────────────────────────────────
    # Self-registration can only ever create a standard "user" account.
    # DAST finding "Unrestricted User Account Creation via Registration API":
    # the previous "admin" carve-out (allowed once, before any admin existed)
    # still left admin-account creation reachable from this public endpoint.
    # Admin accounts are now provisioned exclusively via the seed script
    # (scripts/seed.py) or by an existing admin through the authenticated
    # POST /auth/users endpoint (Depends(require_role("admin"))).
    if (body.role or "user").lower() != "user":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only the 'user' role can be created via self-registration.",
        )
    body.role = "user"

    # ── Input validation / sanitization (toggle: INPUT_SANITIZATION_ENABLED) ─
    # email/name/org_id are validated + sanitized here via the same shared
    # core/security_validation.py validators every other router uses.
    # password gets a minimum-length check only — it is never rendered, so
    # restricting its character set would just weaken entropy. When
    # INPUT_SANITIZATION_ENABLED=false, validate_register_request() still
    # runs (so the required/length/role checks below are unaffected) but its
    # XSS/character-allow-list checks become no-ops — see
    # core/security_validation.py's module docstring.
    is_valid, field_errors, sanitized = validate_register_request(body)
    if not is_valid:
        error_messages = [
            f"{field}: {err}"
            for field, errs in field_errors.items()
            for err in errs
        ]
        raise HTTPException(status_code=400, detail="; ".join(error_messages))
    body.email  = sanitized["email"]
    body.name   = sanitized["name"]
    body.org_id = sanitized["org_id"]

    from passlib.context import CryptContext
    from db.database import SessionLocal
    from db.models import User
    from auth.jwt_handler import encode_token

    pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
    db = SessionLocal()
    try:
        # Normalize email so registration + login agree on casing (login now
        # normalizes to lowercase up front). Stored emails are always lowercase.
        body.email = (body.email or "").strip().lower()
        existing = db.query(User).filter(User.email == body.email).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already registered",
            )

        user = User(
            email=body.email,
            name=body.name,
            role=body.role,
            org_id=body.org_id or None,
            hashed_password=pwd.hash(body.password),
            ad_level=DEFAULT_AD_LEVEL,
            # Individual/OSS mode (HOD_APPROVAL_ENABLED=false): there is no
            # org chart to assign a real department from, and GET
            # /products/departments only ever offers "USER" in that mode —
            # without this, a self-registered user's own department stays
            # blank and GET /products' department-match filter hides every
            # product/doc they create from them (confirmed via testing).
            department=("USER" if not HOD_APPROVAL_ENABLED else None),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        # PII args (email) accepted for backward compat but NOT embedded in JWT (DAST fix).
        # encode_token now silently ignores email, name, department.
        token = encode_token(str(user.id), user.role)
        _set_auth_cookie(response, token)
        # Pre-seed the profile cache so the first enrich_user_context() lookup is a Redis hit.
        try:
            from auth.dependencies import enrich_user_context as _ec
            _ec({"sub": str(user.id)})
        except Exception:
            pass
        return TokenResponse(
            access_token=token,
            user_id=str(user.id),
            role=user.role,
            email=encrypt_pii(user.email),
            name=encrypt_pii(user.name),
            ad_level=user.ad_level if user.ad_level is not None else 6,
            department=getattr(user, "department", "") or "",
        )
    finally:
        db.close()


# ============================================================
# LOGIN
# ============================================================

# ---------------------------------------------------------------------------
# Brute-force / account-lockout constants (DAST fix)
# ---------------------------------------------------------------------------
_MAX_FAILED_ATTEMPTS = int(os.getenv("LOGIN_MAX_FAILED_ATTEMPTS", "5"))
_LOCKOUT_SECONDS     = int(os.getenv("LOGIN_LOCKOUT_SECONDS", "900"))   # 15 minutes
_REDIS_LOCK_DB       = 6   # dedicated Redis DB for login rate-limiting


def _redis_rate_limiter():
    """Return a Redis client for login rate-limiting, or None if unavailable."""
    try:
        from core.config import redis_client as _rc
        r = _rc(db=_REDIS_LOCK_DB, decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None


def _check_and_record_failure(email: str, db, User) -> None:
    """
    Increment the failed-login counter for *email*.

    Primary store  : Redis  (key = login:fail:<email>, TTL = LOCKOUT_SECONDS)
    Fallback store : DB columns (failed_login_attempts, locked_until)

    Raises HTTP 429 if the attempt ceiling is reached.
    """
    import datetime as _dt
    key = f"login:fail:{email.lower()}"
    r   = _redis_rate_limiter()

    if r is not None:
        # ── Redis path ──────────────────────────────────────────────────────
        attempts = r.incr(key)
        if attempts == 1:
            r.expire(key, _LOCKOUT_SECONDS)   # start the TTL on first failure
        if attempts >= _MAX_FAILED_ATTEMPTS:
            ttl = r.ttl(key)
            retry_after = max(ttl, 1)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Account temporarily locked after {_MAX_FAILED_ATTEMPTS} failed attempts. "
                    f"Try again in {retry_after // 60} minute(s)."
                ),
                headers={"Retry-After": str(retry_after)},
            )
    else:
        # ── DB fallback ─────────────────────────────────────────────────────
        user = db.query(User).filter(User.email == email.lower()).first()
        if not user:
            return   # unknown e-mail — generic error raised by caller
        now = _dt.datetime.utcnow()
        attempts = (getattr(user, "failed_login_attempts", 0) or 0) + 1
        user.failed_login_attempts = attempts
        if attempts >= _MAX_FAILED_ATTEMPTS:
            user.locked_until = now + _dt.timedelta(seconds=_LOCKOUT_SECONDS)
        db.commit()
        if attempts >= _MAX_FAILED_ATTEMPTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Account temporarily locked after {_MAX_FAILED_ATTEMPTS} failed attempts. "
                    f"Try again in {_LOCKOUT_SECONDS // 60} minute(s)."
                ),
                headers={"Retry-After": str(_LOCKOUT_SECONDS)},
            )


def _check_lockout(email: str, db, User) -> None:
    """
    Raise HTTP 429 if the account is currently locked.
    Checks Redis first; falls back to DB columns.
    """
    import datetime as _dt
    key = f"login:fail:{email.lower()}"
    r   = _redis_rate_limiter()

    if r is not None:
        attempts = int(r.get(key) or 0)
        if attempts >= _MAX_FAILED_ATTEMPTS:
            ttl = r.ttl(key)
            retry_after = max(ttl, 1)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Account temporarily locked after {_MAX_FAILED_ATTEMPTS} failed attempts. "
                    f"Try again in {retry_after // 60} minute(s)."
                ),
                headers={"Retry-After": str(retry_after)},
            )
    else:
        user = db.query(User).filter(User.email == email.lower()).first()
        if user:
            locked_until = getattr(user, "locked_until", None)
            if locked_until and locked_until > _dt.datetime.utcnow():
                remaining = int((locked_until - _dt.datetime.utcnow()).total_seconds())
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=(
                        f"Account temporarily locked. "
                        f"Try again in {remaining // 60} minute(s)."
                    ),
                    headers={"Retry-After": str(remaining)},
                )


def _clear_failure_counter(email: str, db, User) -> None:
    """Reset the failure counter on successful authentication."""
    import datetime as _dt
    key = f"login:fail:{email.lower()}"
    r   = _redis_rate_limiter()

    if r is not None:
        r.delete(key)
    else:
        user = db.query(User).filter(User.email == email.lower()).first()
        if user:
            user.failed_login_attempts = 0
            user.locked_until          = None
            db.commit()


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, response: Response):
    from passlib.context import CryptContext
    from db.database import SessionLocal
    from db.models import User
    from auth.jwt_handler import encode_token
    from core.config import LDAP_ENABLED, LDAP_AUTO_PROVISION, DEFAULT_AD_LEVEL

    # ── IP-based rate limit: 10 login attempts per 5 minutes ─────────────────
    enforce_rate_limit(request, AUTH_LOGIN)

    # ── Normalize the email once, up front, so every downstream use (both auth
    #    paths, lockout, failure counters, provisioning) is consistent and
    #    case-insensitive. The client no longer constructs the address (the old
    #    "domain-id + @ainxt.com" logic was removed), so the server must own
    #    normalization and must not assume any particular email domain.
    body.email = (body.email or "").strip().lower()

    # ── Input validation / sanitization (toggle: INPUT_SANITIZATION_ENABLED) ─
    # Lightweight on purpose — see validate_login_request()'s docstring. Runs
    # before password decryption/verification so a malformed email is
    # rejected without touching the DB or the lockout counter. When
    # INPUT_SANITIZATION_ENABLED=false this still enforces "email/password
    # required" but skips the XSS/format checks — see
    # core/security_validation.py's module docstring.
    is_valid, field_errors, _sanitized_login = validate_login_request(body)
    if not is_valid:
        error_messages = [
            f"{field}: {err}"
            for field, errs in field_errors.items()
            for err in errs
        ]
        raise HTTPException(status_code=400, detail="; ".join(error_messages))
    body.email = _sanitized_login["email"]

    body.password = _decrypt_password(body.password)
    pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
    db = SessionLocal()
    try:
        # ── Lockout pre-check (before any credential verification) ──────────
        _check_lockout(body.email, db, User)

        # ── LDAP authentication path ────────────────────────────────────────
        # When LDAP_ENABLED, validate credentials against AD first.
        # The DB user record is used for role/band/ABAC — synced nightly by ad_sync.py.
        if LDAP_ENABLED:
            # Pre-check: look up the user in the local DB.
            # Behaviour depends on LDAP_AUTO_PROVISION:
            #   false — controlled rollout gate: user must be pre-registered.
            #                  Returns 403 ACCOUNT_NOT_PROVISIONED if not found.
            #   true  (OSS)  — skip the gate; auto-provision on first successful login.
            _pre_check = db.query(User).filter(User.email == body.email.lower()).first()
            if not _pre_check and not LDAP_AUTO_PROVISION:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="ACCOUNT_NOT_PROVISIONED",
                )

            from auth.ldap_handler import authenticate_user as ldap_auth
            ldap_result = ldap_auth(body.email, body.password)
            if ldap_result is None:
                _check_and_record_failure(body.email, db, User)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid credentials",
                )

            if _pre_check:
                # User already in DB — reuse the existing record
                user = _pre_check
                # Refresh AD fields (including employee_id) on every login
                _emp_id = ldap_result.get("employee_id")
                if _emp_id and not user.employee_id:
                    user.employee_id = _emp_id
                    db.commit()
            else:
                # First LDAP login + LDAP_AUTO_PROVISION=true — provision now.
                # Use DEFAULT_AD_LEVEL as the starting level — same as self-registration.
                # _sync_user_from_org_tree() will override this with the real AD level
                # if org_tree is populated. Without an org_tree, this
                # default stays and gives the user a sensible starting level.
                import uuid as _uuid_mod
                ad_title = ldap_result.get("ad_title") or ""
                user = User(
                    id=str(_uuid_mod.uuid4()),
                    email=body.email.lower(),
                    name=ldap_result.get("name") or body.email,
                    role="user",
                    ad_level=DEFAULT_AD_LEVEL,
                    ad_username=ldap_result.get("ad_username"),
                    employee_id=ldap_result.get("employee_id"),
                    ad_dn=ldap_result.get("ad_dn"),
                    ad_title=ad_title,
                    department=ldap_result.get("department"),
                    manager_dn=ldap_result.get("manager_dn"),
                    is_active=True,
                )
                db.add(user)
                db.commit()
                db.refresh(user)
                logger.info(
                    "ldap.auto_provision",
                    email=body.email,
                    name=user.name,
                )
        else:
            # ── Local password authentication path ──────────────────────────
            user = db.query(User).filter(User.email == body.email).first()
            if not user or not user.hashed_password:
                _check_and_record_failure(body.email, db, User)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid credentials",
                )
            if not pwd.verify(body.password, user.hashed_password):
                _check_and_record_failure(body.email, db, User)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid credentials",
                )

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account disabled",
            )

        # ── Successful auth — reset failure counter ─────────────────────────
        _clear_failure_counter(body.email, db, User)

        # Stamp last_login_at
        import datetime as _dt
        user.last_login_at = _dt.datetime.utcnow()
        db.commit()

        # ── DAST fix: concurrent session control ─────────────────────────────
        # Generate a session_id, embed it in the JWT, and register it in the
        # session registry (Redis + Postgres).  The session manager enforces
        # MAX_CONCURRENT_SESSIONS by evicting the oldest session when the limit
        # is reached.  decode_token() validates the sid on every request.
        import uuid as _uuid_mod
        session_id = str(_uuid_mod.uuid4())

        # Build JWT with minimal RBAC/ABAC claims (no PII — DAST fix).
        # PII (email, name, department) is accepted by encode_token for call-site
        # backward compatibility but is silently ignored and NOT embedded in the JWT.
        token = encode_token(
            user_id=str(user.id),
            role=user.role,
            org_id=user.org_id or "",
            is_security_team=bool(getattr(user, "is_security_team", False)),
            ad_level=user.ad_level if user.ad_level is not None else 6,
            session_id=session_id,
        )
        _set_auth_cookie(response, token)

        # Extract the jti from the token we just issued so the session record
        # can be linked to the correct revocation key
        import jwt as _jwt_mod
        from auth.jwt_handler import _IDENTIFIER as _JWT_SECRET, ALGORITHM as _JWT_ALG
        try:
            _raw_payload = _jwt_mod.decode(
                token, _JWT_SECRET, algorithms=[_JWT_ALG],
                options={"verify_exp": False}
            )
            _issued_jti = _raw_payload.get("jti", "")
        except Exception:
            _issued_jti = ""

        # Collect device/location hints for session metadata
        _ip  = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() \
               or getattr(request.client, "host", "")
        _ua  = request.headers.get("User-Agent", "")[:512]
        _dev = _parse_device_hint(_ua)

        # Explicitly discard credential from scope before session registration
        # so no taint path from body.password reaches register_session or its logs.
        body.password = ""  # noqa: S105 — intentional credential wipe

        # Register session (also enforces concurrent-session limit via eviction)
        try:
            from auth.session_manager import register_session as _reg_session
            _reg_session(
                user_id=str(user.id),
                session_id=session_id,
                jti=_issued_jti,
                ip_address=_ip,
                user_agent=_ua,
                device_hint=_dev,
            )
        except Exception as _se:
            logger.warning("login: session registration error: %s", _se)

        # ── DAST fix: new-login inbox notification ────────────────────────────
        # Alert the user via the in-app inbox so they can detect unauthorized
        # logins from unfamiliar devices or locations.
        _notify_new_login(
            user_id=str(user.id),
            ip=_ip,
            device=_dev,
            user_agent=_ua,
        )

        # Pre-seed the profile cache so the first enrich_user_context() lookup
        # is a Redis hit (no DB round-trip on the very next authenticated request).
        try:
            from auth.dependencies import enrich_user_context as _enrich_ctx
            _cache_payload = {"sub": str(user.id)}
            _enrich_ctx(_cache_payload)   # triggers DB lookup + cache write
        except Exception:
            pass

        # Background post-login sync (non-blocking — best effort). Runs the
        # live-AD staleness check and the org_tree snapshot sync sequentially
        # in a single thread (see _run_post_login_sync docstring) so the two
        # can never race and clobber each other's writes to the same `users`
        # row. `ldap_result` (already fetched during authentication above,
        # when LDAP_ENABLED) is passed through so the live-AD check doesn't
        # issue a second LDAP search for the same user.
        threading.Thread(
            target=_run_post_login_sync,
            args=(str(user.id), user.email, ldap_result if LDAP_ENABLED else None),
            daemon=True,
        ).start()

        # SECURITY: Return only non-privilege fields in the login response body.
        # role, ad_level, can_approve are intentionally omitted — they are
        # already embedded in the signed JWT (httpOnly cookie) and are re-fetched
        # from the server-authoritative /auth/me endpoint by the browser client.
        # Exposing them here creates the "privilege escalation via response
        # manipulation" attack surface (DAST finding).
        return LoginResponse(
            access_token=token,   # populated for CLI/API clients; cookie is used by browsers
            user_id=str(user.id),
            email=encrypt_pii(user.email),
            name=encrypt_pii(user.name),
        )
    finally:
        db.close()


# ============================================================
# ME
# ============================================================

@router.post("/logout")
def logout(request: Request, response: Response):
    """
    Revoke the current JWT (adds jti to Redis blacklist) and clear
    the auth_token httpOnly cookie.  Works for both cookie-based and
    Bearer-header sessions.
    """
    from auth.jwt_handler import revoke_token

    token = request.cookies.get("auth_token")
    if not token:
        # Try Bearer header as fallback (API clients)
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            token = auth_hdr[7:]

    if token:
        revoke_token(token)
        # DAST fix — concurrent session control:
        # Decode the token (ignoring expiry) to extract user_id + session_id
        # and remove the entry from the session registry so the slot is freed.
        try:
            import jwt as _jwt_lib
            from auth.jwt_handler import _IDENTIFIER as _JS, ALGORITHM as _JA
            _p = _jwt_lib.decode(
                token, _JS, algorithms=[_JA], options={"verify_exp": False}
            )
            _uid = _p.get("sub")
            _sid = _p.get("sid")
            if _uid and _sid:
                from auth.session_manager import revoke_session as _rev_sess
                _rev_sess(_uid, _sid, also_blacklist_jwt=False)
        except Exception as _le:
            logger.debug("logout: session registry cleanup: %s", _le)

    response.delete_cookie(key="auth_token", path="/")
    return {"success": True}


# ============================================================
# SESSION MANAGEMENT  (DAST fix — concurrent session control)
# ============================================================

@router.get("/sessions")
def list_sessions(payload: dict = Depends(get_current_user)):
    """
    List all active sessions for the authenticated user.

    Returns each session's IP address, device/browser, and creation timestamp
    so the user can identify logins from unfamiliar devices or locations.

    DAST fix: "Monitor login activity and notify users when new logins occur
    to help detect unauthorized access attempts."
    """
    from auth.session_manager import get_active_sessions
    import datetime as _dt
    from datetime import timezone as _tz

    user_id  = payload.get("sub")
    sessions = get_active_sessions(user_id)
    result   = []
    for s in sessions:
        ts = s.get("created_at", 0)
        created_str = (
            _dt.datetime.fromtimestamp(ts, tz=_tz.utc).strftime("%Y-%m-%d %H:%M UTC")
            if ts else ""
        )
        result.append({
            "session_id": s["session_id"],
            "ip":         s.get("ip", ""),
            "device":     s.get("device", ""),
            "user_agent": s.get("user_agent", ""),
            "created_at": created_str,
            "is_current": s["session_id"] == payload.get("sid", ""),
        })
    return {
        "sessions":    result,
        "total":       len(result),
        "max_allowed": int(os.getenv("MAX_CONCURRENT_SESSIONS", "5")),
    }


@router.delete("/sessions")
def revoke_other_sessions(
    request: Request,
    response: Response,
    payload: dict = Depends(get_current_user),
):
    """
    Revoke ALL other active sessions while keeping the current one.

    "Sign out everywhere else" — gives users a single-click way to terminate
    sessions on all other devices after detecting an unexpected login.

    DAST fix: "Restrict the number of concurrent sessions per account or
    invalidate previous sessions upon new login."
    """
    from auth.session_manager import revoke_all_sessions
    user_id       = payload.get("sub")
    current_sid   = payload.get("sid")
    revoked_count = revoke_all_sessions(user_id, except_session_id=current_sid)
    logger.info(
        "sessions: user %s revoked %d other session(s) via /auth/sessions",
        user_id, revoked_count,
    )
    return {
        "success":       True,
        "revoked_count": revoked_count,
        "message": (
            f"Revoked {revoked_count} other session(s). "
            "Your current session remains active."
        ),
    }


@router.delete("/sessions/{session_id}")
def revoke_specific_session(
    session_id: str,
    payload: dict = Depends(get_current_user),
):
    """
    Revoke a specific session by its session_id.

    The session must belong to the authenticated user.  Any device still
    carrying the revoked token will receive 401 on its next request.

    DAST fix: fine-grained session termination — users can remove individual
    suspicious sessions without signing out everywhere.
    """
    from auth.session_manager import revoke_session, get_active_sessions
    user_id = payload.get("sub")
    active  = get_active_sessions(user_id)
    owned   = [s for s in active if s["session_id"] == session_id]
    if not owned:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found or already expired",
        )
    revoke_session(user_id, session_id, also_blacklist_jwt=True)
    logger.info(
        "sessions: user %s revoked session %s via /auth/sessions/{id}",
        user_id, session_id,
    )
    return {
        "success":    True,
        "session_id": session_id,
        "message":    "Session revoked. The device using that session will be signed out.",
    }


# ============================================================
# REFRESH
# ============================================================

class RefreshRequest(BaseModel):
    refresh_token: Optional[str] = None  # Bearer token body field (alternative to header)


@router.post("/refresh")
def refresh_token(
    body: RefreshRequest,
    request: Request,
    response: Response,
):
    """
    Issue a fresh JWT with a new expiry, preserving all existing claims.

    Accepts the current token via:
      - Authorization: Bearer <token> header  (preferred)
      - {"refresh_token": "<token>"} request body  (fallback for clients without header access)

    The existing token must be either still valid OR within a 1-hour grace window
    past expiry. A new token is issued with a fresh expiry. The old token is revoked
    to prevent replay.

    Returns: {"access_token": "...", "token_type": "bearer", "expires_in": 86400}
    """
    # ── IP-based rate limit: 30 refresh calls per minute ─────────────────────
    enforce_rate_limit(request, AUTH_REFRESH)

    import jwt as _jwt
    from datetime import timezone as _tz
    from auth.jwt_handler import (
        _IDENTIFIER as _SECRET_KEY, ALGORITHM, EXPIRE_HOURS, encode_token,
        revoke_token,
    )

    # ── Extract raw token ────────────────────────────────────────────────────
    raw_token: Optional[str] = None
    auth_hdr = request.headers.get("Authorization", "")
    if auth_hdr.startswith("Bearer "):
        raw_token = auth_hdr[7:]
    elif body.refresh_token:
        raw_token = body.refresh_token

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No token provided — supply Authorization: Bearer <token> or refresh_token in body",
        )

    # ── Decode with 1-hour grace window past expiry ──────────────────────────
    try:
        payload = _jwt.decode(
            raw_token,
            _SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"verify_exp": False},  # we do manual expiry check with grace window
        )
    except _jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        )

    # Manual expiry check: allow up to 1 hour past expiry for refresh
    import time as _time
    exp = payload.get("exp")
    if exp is not None:
        grace_seconds = 3600  # 1-hour grace window
        if _time.time() > exp + grace_seconds:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired beyond the 1-hour refresh grace window — please log in again",
            )

    # ── Check revocation blacklist (revoked tokens cannot be refreshed) ──────
    # SECURITY: fail-closed — if Redis is unavailable we deny the refresh
    # rather than allowing potentially revoked tokens to be refreshed.
    jti = payload.get("jti")
    if jti:
        try:
            from core.config import RDB_QUEUE
            from core.kv import get_kv
            _r = get_kv(RDB_QUEUE, decode_responses=True)
            if _r is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Token refresh temporarily unavailable — please try again shortly",
                )
            if _r.exists(f"jwt:revoked:{jti}"):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token has been revoked — please log in again",
                )
        except HTTPException:
            raise
        except Exception:
            # KV error — deny refresh to prevent revoked-token bypass
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Token refresh temporarily unavailable — please try again shortly",
            )

    # ── Issue new token with minimal claims (no PII — DAST fix) ──────────────
    # PII fields (email, name, department) are NOT forwarded into the new token.
    # They are fetched on-demand by enrich_user_context() in auth/dependencies.py.
    # Reuse the caller's existing (already-registered) session id so the refreshed
    # token continues to pass is_session_active(). Only the JWT id (jti) rotates;
    # the session stays the same. Without this the new token would carry an
    # unregistered random sid → 401 on the next request.
    new_token = encode_token(
        user_id=str(payload.get("sub", "")),
        role=payload.get("role", "user"),
        org_id=payload.get("org_id", ""),
        is_security_team=bool(payload.get("is_security_team", False)),
        ad_level=int(payload.get("ad_level", 6) or 6),
        session_id=payload.get("sid"),
    )

    # ── Revoke old token so it cannot be reused ───────────────────────────────
    revoke_token(raw_token)

    # Set httpOnly cookie as well (mirrors login behaviour)
    _set_auth_cookie(response, new_token)

    return {
        "access_token": new_token,
        "token_type": "bearer",
        "expires_in": EXPIRE_HOURS * 3600,
    }

@router.post("/cli-token")
def cli_token(payload: dict = Depends(get_current_user)):
    """Mint a fresh JWT for the local CLI / Cowork office agent from the caller's
    EXISTING authenticated session (httpOnly cookie OR bearer header).

    This lets the desktop reuse the web-app login instead of forcing a second
    "sign in to the CLI" step: the renderer is already authenticated via the
    session cookie (which JS cannot read), so it calls this endpoint to obtain a
    raw token string the CLI can write to ~/.ainxt/config.json. Same claims as
    the current session, fresh expiry. Authorization is identical to /auth/me —
    no extra privilege is granted."""
    from auth.jwt_handler import encode_token, EXPIRE_HOURS
    # Reuse the caller's ALREADY-REGISTERED session id so the minted token passes
    # is_session_active() on subsequent requests (e.g. the desktop's /auth/me
    # pre-flight). Without this, encode_token() generates a fresh random sid that
    # was never written to the session registry → decode_token → 401 →
    # "CLI login failure" when the session store (Redis) is enforced.
    token = encode_token(
        user_id=str(payload.get("sub", "")),
        role=payload.get("role", "user"),
        email=payload.get("email", ""),
        name=payload.get("name", ""),
        org_id=payload.get("org_id", ""),
        is_security_team=bool(payload.get("is_security_team", False)),
        ad_level=int(payload.get("ad_level", 6) or 6),
        department=payload.get("department", ""),
        session_id=payload.get("sid"),
    )
    return {"access_token": token, "token_type": "bearer", "expires_in": EXPIRE_HOURS * 3600}


@router.get("/me")
def get_me(payload: dict = Depends(get_current_user)):
    """Return the authenticated user's profile."""
    from db.database import SessionLocal
    from db.models import User
    from core.config import APPROVAL_AD_LEVEL

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == payload["sub"]).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        _level = (getattr(user, "ad_level", None) or 6)
        # HOD info comes from the enriched payload.
        _is_hod          = bool(payload.get("is_hod", False))
        _hod_departments = list(payload.get("hod_departments") or []) if _is_hod else []

        # Reporting-manager flag: non-HOD user who has reports in hierarchy_table.
        # HODs already have a team view via the HOD button, so they get False here.
        _is_reporting_manager = False
        if not _is_hod:
            try:
                from services.hierarchy_service import has_direct_reports
                _is_reporting_manager = has_direct_reports(user.email)
            except Exception:
                pass  # fail-open: default False

        # If employee_id is missing, attempt a live LDAP fetch and backfill DB.
        # This covers users who registered before the employeeID attribute was
        # mapped, without requiring a logout/login or a manual backfill script.
        if not getattr(user, "employee_id", None):
            try:
                import os as _os
                if _os.getenv("LDAP_ENABLED", "false").lower() == "true":
                    from auth.ldap_handler import get_user_attributes
                    _attrs = get_user_attributes(user.ad_username or user.email)
                    _emp_id = (_attrs or {}).get("employee_id")
                    if _emp_id:
                        user.employee_id = _emp_id
                        db.commit()
            except Exception:
                db.rollback()  # keep session clean; never block /me on LDAP failure

        return {
            "id":              str(user.id),
            "email":           encrypt_pii(user.email),
            "name":            encrypt_pii(user.name),
            "role":            user.role,
            "org_id":          user.org_id,
            "is_active":       user.is_active,
            "ad_level":        _level,
            "department":      getattr(user, "department", "") or "",
            "can_approve":     _level <= APPROVAL_AD_LEVEL or user.role == "admin",
            "is_hod":          _is_hod,
            "hod_departments": _hod_departments,
            "is_reporting_manager": _is_reporting_manager,
            "created_at":      user.created_at.isoformat() if user.created_at else None,
            "ad_username":     getattr(user, "ad_username", "") or "",
            "employee_id":     getattr(user, "employee_id", "") or "",
            # Forced password-change gate: a temp password (forgot-password
            # flow) is a fully valid password, so login already succeeds and a
            # real session is established. The frontend uses THIS flag — read
            # right after login via the existing re-verification call to
            # /auth/me — to hold the user on the login screen in a "set new
            # password" view instead of granting entry to the app, and again
            # on any later page load so a mid-flow refresh cannot skip it.
            "is_temp_password": bool(getattr(user, "is_temp_password", False)),
        }
    finally:
        db.close()


# ============================================================
# SSO ENDPOINTS
# ============================================================

class SSOCallbackRequest(BaseModel):
    code: str
    state: str
    redirect_uri: str
    provider: str


@router.get("/sso/provider")
def sso_provider_info():
    """
    Return which SSO provider is configured (if any).
    Clients use this to decide whether to show the SSO login button.
    """
    from auth.sso import get_sso_provider

    provider = get_sso_provider()
    return {
        "provider": provider,
        "enabled":  provider is not None,
    }


@router.get("/sso/authorize")
def sso_authorize(redirect_uri: str):
    """
    Generate an OAuth2 authorization URL for the configured SSO provider.
    The client should redirect the browser to the returned URL.
    Query param: redirect_uri (required)
    """
    import secrets as _secrets
    from auth.sso import get_sso_provider, get_sso_login_url

    provider = get_sso_provider()
    if provider == "none":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No SSO provider is configured",
        )

    state = _secrets.token_urlsafe(32)
    base_url = get_sso_login_url(redirect_uri)
    # Append state for CSRF protection
    url = f"{base_url}&state={state}" if "?" in base_url else f"{base_url}?state={state}"
    return {"url": url, "state": state, "provider": provider}


@router.post("/sso/callback", response_model=TokenResponse)
def sso_callback(body: SSOCallbackRequest, response: Response):
    """
    Handle the OAuth2 callback.
    Exchanges the authorization code for tokens, fetches user info,
    upserts the user in the database, and returns a platform JWT.
    """
    from auth.sso import exchange_sso_code
    from auth.jwt_handler import encode_token
    from db.database import SessionLocal
    from db.models import User

    try:
        user_info = exchange_sso_code(code=body.code, redirect_uri=body.redirect_uri)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"SSO token exchange failed: {exc}",
        )

    if not user_info:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="SSO code exchange failed — provider not configured or credentials invalid",
        )

    sso_sub = user_info.get("user_id", "")
    email = user_info.get("email", "")
    name = user_info.get("name") or email

    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SSO provider did not return an email address",
        )

    db = SessionLocal()
    try:
        # Look up by SSO subject first, then fall back to email
        user = (
            db.query(User)
            .filter(User.sso_provider == body.provider, User.sso_subject == sso_sub)
            .first()
        )
        if user is None:
            user = db.query(User).filter(User.email == email).first()

        if user is None:
            # First SSO login — provision the user with role="user"
            user = User(
                email=email,
                name=name,
                role="user",
                sso_provider=body.provider,
                sso_subject=sso_sub,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            # Update SSO linkage if not already set
            changed = False
            if not user.sso_provider:
                user.sso_provider = body.provider
                changed = True
            if not user.sso_subject:
                user.sso_subject = sso_sub
                changed = True
            if changed:
                db.commit()
                db.refresh(user)

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account disabled",
            )

        # Register a session so the minted token passes is_session_active().
        # (Programmatic SSO callback; the browser flow uses the GET handler in
        # auth/sso.py which redirects to /ui.)
        import uuid as _uuid_mod
        import jwt as _jwt
        _sso_sid = str(_uuid_mod.uuid4())
        platform_token = encode_token(
            str(user.id), user.role, user.email, session_id=_sso_sid,
        )
        try:
            from auth.session_manager import register_session as _reg_session
            _issued_jti = _jwt.decode(
                platform_token, options={"verify_signature": False}
            ).get("jti", "")
            _reg_session(user_id=str(user.id), session_id=_sso_sid, jti=_issued_jti)
        except Exception as _se:
            logger.warning("sso/callback(POST): session registration error: %s", _se)
        _set_auth_cookie(response, platform_token)
        return TokenResponse(
            access_token=platform_token,
            user_id=str(user.id),
            role=user.role,
            email=encrypt_pii(user.email),
            name=encrypt_pii(user.name),
        )
    finally:
        db.close()
# ============================================================
# USER MANAGEMENT  (security+ read, admin write)
# ============================================================

@router.get("/users")
def list_users(
    page: int = 1,
    page_size: int = 50,
    search: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """List all users. Requires admin, security role, OR ad_level <= 2 (for level override user search)."""
    _caller_level = int(current_user.get("ad_level") or 6)
    _caller_role  = current_user.get("role", "user")
    if _caller_role not in ("admin", "security") and _caller_level > 2:
        from fastapi import HTTPException as _HTTPEx
        raise _HTTPEx(status_code=403, detail="Requires admin, security, or director-level access")
    from db.database import SessionLocal
    from db.models import User as UserModel

    db = SessionLocal()
    try:
        q = db.query(UserModel).order_by(UserModel.created_at.desc())
        if search:
            like = f"%{search}%"
            q = q.filter((UserModel.email.ilike(like)) | (UserModel.name.ilike(like)))
        total = q.count()
        rows  = q.offset((page - 1) * page_size).limit(page_size).all()
        return {
            "total": total,
            "users": [
                {
                    "id":         str(u.id),
                    "email":      encrypt_pii(u.email),
                    "name":       encrypt_pii(u.name),
                    "role":       u.role,
                    "ad_level":   getattr(u, "ad_level", 6) or 6,
                    "department": getattr(u, "department", "") or "",
                    "is_active":  getattr(u, "is_active", True),
                    "created_at": u.created_at.isoformat() if u.created_at else None,
                }
                for u in rows
            ],
        }
    finally:
        db.close()


class CreateUserRequest(BaseModel):
    email:    str
    name:     str
    password: str
    role:     str = "viewer"
    org_id:   str = ""


@router.post("/users", status_code=201)
def create_user(
    body: CreateUserRequest,
    current_user: dict = Depends(require_role("admin")),
):
    """Create a new user. Requires admin role."""
    from passlib.context import CryptContext
    from db.database import SessionLocal
    from db.models import User as UserModel

    valid_roles = ["viewer", "developer", "operator", "security", "admin"]
    if body.role not in valid_roles:
        raise HTTPException(400, f"role must be one of {valid_roles}")

    pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
    db  = SessionLocal()
    try:
        if db.query(UserModel).filter(UserModel.email == body.email).first():
            raise HTTPException(409, "Email already registered")
        new_user = UserModel(
            email=body.email,
            name=body.name,
            role=body.role,
            org_id=body.org_id or None,
            hashed_password=pwd.hash(body.password),
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        return {"id": str(new_user.id), "email": encrypt_pii(new_user.email), "role": new_user.role}
    finally:
        db.close()


class UpdateUserRequest(BaseModel):
    role:      Optional[str]  = None
    is_active: Optional[bool] = None


@router.patch("/users/{user_id}")
def update_user(
    user_id: str,
    body: UpdateUserRequest,
    current_user: dict = Depends(require_role("admin")),
):
    """Update user role or active status. Requires admin role."""
    from db.database import SessionLocal
    from db.models import User as UserModel

    valid_roles = ["viewer", "developer", "operator", "security", "admin"]
    if body.role and body.role not in valid_roles:
        raise HTTPException(400, f"role must be one of {valid_roles}")

    db = SessionLocal()
    try:
        target = db.query(UserModel).filter(UserModel.id == user_id).first()
        if not target:
            raise HTTPException(404, "User not found")
        if str(target.id) == current_user.get("sub") and body.role and body.role != "admin":
            raise HTTPException(400, "Cannot demote your own account")
        if body.role      is not None: target.role      = body.role
        if body.is_active is not None: target.is_active = body.is_active
        db.commit()
        # The role/is_active flip just committed above is not visible to the
        # target user's other in-flight/future requests until the 5-minute
        # profile cache TTL expires (get_current_user() serves the cached
        # role from Redis). Evict it now so a newly-granted admin can see
        # admin-only data (user list, budget requests, etc.) immediately.
        invalidate_profile_cache(str(target.id))
        return {"id": str(target.id), "role": target.role, "is_active": target.is_active}
    finally:
        db.close()


# ============================================================
# LEVEL OVERRIDE ENDPOINTS
# Only ad_level <= 2 (Director+) can grant/revoke overrides.
# Granters cannot grant a level MORE senior (lower number) than their own.
# ============================================================

class LevelOverrideCreate(BaseModel):
    user_id:          str
    ad_level_override: int
    reason:           str
    expires_at:       Optional[str] = None   # ISO datetime string or None for permanent


def _require_director(current_user: dict = Depends(get_current_user)):
    """Gate level-override management.

    HOD_APPROVAL_ENABLED=true  (HOD-hierarchy mode, unchanged): admin OR any
      ad_level <= 2 (Director+) user may manage overrides.
    HOD_APPROVAL_ENABLED=false (flat/admin-only mode, the default): there is
      no real department/seniority hierarchy to delegate this to, so only
      admin may manage overrides — the ad_level <= 2 path is dropped.
    """
    from core.config import HOD_APPROVAL_ENABLED
    if current_user.get("role") == "admin":
        return current_user
    if not HOD_APPROVAL_ENABLED:
        raise HTTPException(status_code=403, detail="Only admins can manage level overrides in this deployment")
    _level = int(current_user.get("ad_level") or 6)
    if _level > 2:
        raise HTTPException(status_code=403, detail="Only director-level users (ad_level ≤ 2) can manage level overrides")
    return current_user


@router.get("/level-overrides")
def list_level_overrides(current_user: dict = Depends(_require_director)):
    """List all active level overrides."""
    from db.database import SessionLocal
    from db.models import UserLevelOverride, User as UserModel, OrgTree
    from datetime import datetime as _dt, timedelta as _td
    db = SessionLocal()
    try:
        # expires_at is stored as literal wall-clock digits the grantor typed
        # (no UTC conversion — see create_level_override), so compare against
        # local wall-clock "now" (utcnow() + 5:30), not utcnow() itself.
        _now_local = _dt.utcnow() + _td(hours=5, minutes=30)

        # ── Auto-expire stale overrides ──────────────────────────────────────
        # Overrides whose expires_at has passed are still is_active=True in the
        # DB (there is no background worker). Expire them here so that:
        #   1. The Active tab never shows them again.
        #   2. is_active is flipped to False so the History tab can correctly
        #      display "Expired" (is_active=False, revoked_at=None) vs "Active".
        #   3. The user's ad_level is immediately restored — not deferred to
        #      their next login — fixing the "level not reverted" bug.
        # Note: revoked_at is intentionally left NULL for expired overrides so
        # the History tab can distinguish "Expired" from "Revoked" (revoked_at set).
        stale_overrides = (
            db.query(UserLevelOverride)
            .filter(UserLevelOverride.is_active == True)
            .filter(UserLevelOverride.expires_at != None)
            .filter(UserLevelOverride.expires_at <= _now_local)
            .all()
        )
        for stale_ov in stale_overrides:
            stale_ov.is_active = False
            # Restore the user's ad_level from org_tree (most authoritative source),
            # falling back to the snapshotted original_level, then to 6 (most junior).
            stale_target = db.query(UserModel).filter(UserModel.id == stale_ov.user_id).first()
            if stale_target:
                ot = db.query(OrgTree).filter(OrgTree.mail == stale_target.email.lower()).first()
                stale_target.ad_level = ot.level if ot else (stale_ov.original_level or 6)
                # Flat-mode Elevated grants (see create_level_override) also
                # flipped role to 'admin' — restore the pre-grant role
                # snapshot on expiry too, not just on manual revoke.
                if stale_ov.original_role:
                    stale_target.role = stale_ov.original_role
        if stale_overrides:
            db.commit()
            # ad_level/role were just restored above — evict each affected
            # user's cached profile so the reverted role/ad_level (e.g. an
            # expired admin grant) takes effect immediately instead of
            # waiting out the 5-minute profile cache TTL.
            for stale_ov in stale_overrides:
                invalidate_profile_cache(str(stale_ov.user_id))
        # ────────────────────────────────────────────────────────────────────

        overrides = (
            db.query(UserLevelOverride)
            .filter(UserLevelOverride.is_active == True)
            .filter(
                (UserLevelOverride.expires_at == None) |
                (UserLevelOverride.expires_at > _now_local)
            )
            .order_by(UserLevelOverride.created_at.desc())
            .all()
        )
        results = []
        for ov in overrides:
            target_user = db.query(UserModel).filter(UserModel.id == ov.user_id).first()
            grantor     = db.query(UserModel).filter(UserModel.id == ov.overridden_by).first()
            results.append({
                "id":               str(ov.id),
                "user_id":          str(ov.user_id),
                "user_email":       encrypt_pii(target_user.email) if target_user else "",
                "user_name":        encrypt_pii(target_user.name)  if target_user else "",
                "ad_level_override": ov.ad_level_override,
                "original_level":   ov.original_level,
                "overridden_by":    str(ov.overridden_by),
                "grantor_email":    encrypt_pii(grantor.email) if grantor else "",
                "reason":           ov.reason,
                "department":       target_user.department if target_user else "",
                "expires_at":       ov.expires_at.isoformat() if ov.expires_at else None,
                "created_at":       ov.created_at.isoformat(),
            })
        return {"overrides": results}
    finally:
        db.close()


@router.get("/level-overrides/user/{target_user_id}")
def get_user_override_history(target_user_id: str, current_user: dict = Depends(_require_director)):
    """Get override history for a specific user (all including revoked)."""
    import uuid as _uuid_mod
    try:
        _uuid_mod.UUID(target_user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID — must be a valid UUID")

    from db.database import SessionLocal
    from db.models import UserLevelOverride, User as UserModel
    db = SessionLocal()
    try:
        overrides = (
            db.query(UserLevelOverride)
            .filter(UserLevelOverride.user_id == target_user_id)
            .order_by(UserLevelOverride.created_at.desc())
            .all()
        )
        results = []
        for ov in overrides:
            grantor = db.query(UserModel).filter(UserModel.id == ov.overridden_by).first()
            results.append({
                "id":               str(ov.id),
                "ad_level_override": ov.ad_level_override,
                "original_level":   ov.original_level,
                "grantor_email":    encrypt_pii(grantor.email) if grantor else "",
                "reason":           ov.reason,
                "is_active":        ov.is_active,
                "expires_at":       ov.expires_at.isoformat() if ov.expires_at else None,
                "revoked_at":       ov.revoked_at.isoformat() if ov.revoked_at else None,
                "created_at":       ov.created_at.isoformat(),
            })
        return {"history": results}
    finally:
        db.close()


@router.post("/level-overrides", status_code=201)
def create_level_override(body: LevelOverrideCreate, current_user: dict = Depends(_require_director)):
    """Grant a temporary level override to a user."""
    # Validate user_id and reason for XSS / SQL injection
    is_valid, field_errors, sanitized = validate_level_override_request(body)
    if not is_valid:
        error_messages = []
        for field, errors in field_errors.items():
            for e in errors:
                error_messages.append(f"{field}: {e}")
        raise HTTPException(status_code=400, detail="; ".join(error_messages))

    from db.database import SessionLocal
    from db.models import UserLevelOverride, User as UserModel
    from datetime import datetime as _dt
    import uuid as _uuid

    granter_level = int(current_user.get("ad_level") or 6)
    granter_id    = current_user.get("sub") or current_user.get("id")

    # Cannot grant a level more senior (lower number) than yourself
    if body.ad_level_override < granter_level and current_user.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail=f"You (level {granter_level}) cannot grant level {body.ad_level_override} — cannot elevate beyond your own level",
        )
    if not 0 <= body.ad_level_override <= 6:
        raise HTTPException(status_code=400, detail="ad_level_override must be 0–6")

    # Flat/admin-only mode (HOD_APPROVAL_ENABLED=false, the default): there is
    # no department/seniority hierarchy, so overrides collapse to a binary
    # grant — L0 ("Elevated" / admin-equivalent access) or L6 ("Standard" /
    # the platform's most-junior default). HOD-hierarchy mode (true) keeps
    # the full L0–L6 range unchanged.
    from core.config import HOD_APPROVAL_ENABLED as _hod_mode
    if not _hod_mode and body.ad_level_override not in (0, 6):
        raise HTTPException(
            status_code=400,
            detail="This deployment only supports two override levels: 0 (Elevated) or 6 (Standard)",
        )

    db = SessionLocal()
    try:
        target = db.query(UserModel).filter(UserModel.id == sanitized["user_id"]).first()
        if not target:
            raise HTTPException(status_code=404, detail="Target user not found")

        expires = None
        if body.expires_at:
            # By design, expires_at is stored VERBATIM — exactly the digits the
            # grantor typed in the datetime-local picker (e.g. "2026-08-04T15:30"),
            # with NO UTC/IST conversion in either direction. That's what the UI
            # displays back unchanged, and it's what the org wants: DB value ==
            # picked value == displayed value, always.
            #
            # This deliberately breaks from every other timestamp column on this
            # table (created_at, revoked_at), which store naive datetime.utcnow()
            # and get a +5:30 IST conversion on display. expires_at is the one
            # exception, kept intentionally simple at the cost of not being a
            # real, comparable UTC instant — see the "Active" filter and the
            # AD-sync re-apply query below, which compare against local wall-clock
            # "now" (not utcnow()) to stay consistent with this convention.
            try:
                expires = _dt.fromisoformat(body.expires_at)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid expires_at format — use ISO datetime")
            # Safety net only: if a caller still sends a tz-aware string (older
            # cached frontend build, direct API client, etc.), strip the offset
            # WITHOUT shifting the clock — we want the literal digits kept, not
            # a converted instant.
            if expires.tzinfo is not None:
                expires = expires.replace(tzinfo=None)

        # Deactivate any existing active overrides for this user first. If one
        # of them had elevated the target's `role` (flat-mode Elevated grant —
        # see below), restore the role to its pre-grant baseline first so the
        # new override snapshots the correct "true" role, not the
        # currently-elevated one.
        existing = (
            db.query(UserLevelOverride)
            .filter(UserLevelOverride.user_id == sanitized["user_id"], UserLevelOverride.is_active == True)
            .all()
        )
        for ex in existing:
            ex.is_active  = False
            ex.revoked_at = _dt.utcnow()
            if ex.original_role:
                target.role = ex.original_role

        # Flat/admin-only mode (HOD_APPROVAL_ENABLED=false) only: an
        # "Elevated" (ad_level=0) grant also flips users.role to 'admin' for
        # the duration of the override — ad_level alone doesn't unlock
        # role=="admin"-gated endpoints (user management, model governance
        # writes, org-cap admin config, etc.) in flat mode. Snapshot the
        # pre-grant role so revoke_level_override / the auto-expiry branch in
        # list_level_overrides can restore it exactly. HOD-hierarchy mode and
        # flat-mode "Standard" (ad_level=6) grants never touch role.
        role_snapshot = None
        if not _hod_mode and body.ad_level_override == 0 and target.role != "admin":
            role_snapshot = target.role
            target.role = "admin"

        override = UserLevelOverride(
            id=str(_uuid.uuid4()),
            user_id=sanitized["user_id"],
            ad_level_override=body.ad_level_override,
            original_level=target.ad_level or 6,
            original_role=role_snapshot,
            overridden_by=granter_id,
            reason=sanitized["reason"],
            expires_at=expires,
            is_active=True,
        )
        db.add(override)

        # Apply override immediately to user record
        target.ad_level = body.ad_level_override
        db.commit()
        # ad_level (and possibly role, via the flat-mode Elevated-grant flip
        # above) just changed for `target` — evict their cached profile so
        # the new access level/role is honored on their very next request
        # instead of waiting out the 5-minute profile cache TTL.
        invalidate_profile_cache(str(target.id))

        # Notify target user
        try:
            from store.inbox_store import publish_inbox_item
            # `expires` holds the literal digits the grantor typed (no UTC/IST
            # conversion — see the comment where `expires` is parsed above).
            # Render those same digits verbatim so the notification text matches
            # exactly what's stored and what the UI shows.
            expires_str = expires.strftime("%d %b %Y, %I:%M %p") if expires else "no expiry set"
            _role_note = " You have also been granted **admin** access for the duration of this override." if role_snapshot else ""
            publish_inbox_item(
                user_id  = str(target.id),
                type     = "level_override",
                title    = "Temporary access level granted",
                body     = (
                    f"Your access level has been temporarily elevated to **L{body.ad_level_override}** "
                    f"by `{current_user.get('email', 'admin')}`.{_role_note} Expires: {expires_str}.\n\n"
                    f"Reason: {sanitized['reason'] or 'Not specified'}"
                ),
                source_id= str(override.id),
                metadata = {"entity_type": "level_override", "entity_id": str(override.id),
                            "original_level": override.original_level,
                            "granted_level": body.ad_level_override,
                            "granted_by": current_user.get("email", "admin"),
                            "expires_at": expires_str},
            )
        except Exception as _e:
            logger.warning(f"level override inbox notify failed: {_e}")

        return {
            "id":               str(override.id),
            "user_id":          str(target.id),
            "user_email":       encrypt_pii(target.email),
            "ad_level_override": override.ad_level_override,
            "original_level":   override.original_level,
            "reason":           override.reason,
            "expires_at":       override.expires_at.isoformat() if override.expires_at else None,
            "created_at":       override.created_at.isoformat(),
        }
    finally:
        db.close()


@router.delete("/level-overrides/{override_id}")
def revoke_level_override(override_id: str, current_user: dict = Depends(_require_director)):
    """Revoke an active level override and restore the user's AD level."""
    from db.database import SessionLocal
    from db.models import UserLevelOverride, User as UserModel, OrgTree
    from datetime import datetime as _dt
    db = SessionLocal()
    try:
        override = db.query(UserLevelOverride).filter(UserLevelOverride.id == override_id).first()
        if not override:
            raise HTTPException(status_code=404, detail="Override not found")
        if not override.is_active:
            raise HTTPException(status_code=400, detail="Override is already inactive")

        override.is_active  = False
        override.revoked_at = _dt.utcnow()

        # Restore original AD level from org_tree (most authoritative source)
        target = db.query(UserModel).filter(UserModel.id == override.user_id).first()
        if target:
            ot = db.query(OrgTree).filter(OrgTree.mail == target.email.lower()).first()
            target.ad_level = ot.level if ot else (override.original_level or 6)
            # Flat-mode Elevated grants (see create_level_override) also
            # flipped role to 'admin' — restore the pre-grant role snapshot.
            if override.original_role:
                target.role = override.original_role

        db.commit()
        # ad_level (and possibly role) were just restored above — evict the
        # cached profile so the revocation takes effect immediately instead
        # of waiting out the 5-minute profile cache TTL.
        if target:
            invalidate_profile_cache(str(target.id))

        # Notify target user their override was revoked
        if target:
            try:
                from store.inbox_store import publish_inbox_item
                restored_level = target.ad_level
                _role_note = " Your admin access has also been revoked." if override.original_role else ""
                publish_inbox_item(
                    user_id  = str(target.id),
                    type     = "level_override",
                    title    = "Temporary access level revoked",
                    body     = (
                        f"Your temporary access elevation has been revoked by "
                        f"`{current_user.get('email', 'admin')}`. "
                        f"Your access level is restored to **L{restored_level}**.{_role_note}"
                    ),
                    source_id= str(override.id),
                    metadata = {"entity_type": "level_override", "entity_id": str(override.id),
                                "action": "revoke", "restored_level": restored_level},
                )
            except Exception as _e:
                logger.warning(f"level override revoke inbox notify failed: {_e}")

        return {"success": True, "override_id": override_id}
    finally:
        db.close()


# ============================================================
# CHANGE PASSWORD
# POST /auth/change-password
# Available to all authenticated users (local accounts only).
# LDAP/SSO users don't have a hashed_password — rejected gracefully.
# ============================================================

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password:     str

@router.post("/change-password")
def change_password(
    body:    ChangePasswordRequest,
    payload: dict = Depends(get_current_user),
):
    """Change the current user's password.

    - Requires the correct current password (prevents session hijack).
    - Rejects LDAP/SSO accounts that have no local password.
    - Enforces password complexity (core/security_validation.py::
      password_strength_errors — length, uppercase, digit, special char). The
      one place this applies to is called via THIS endpoint whether the caller
      is the Profile page or the forced-reset screen (Login.jsx
      "reset-required" view) — both hit change-password, not a separate path.
    - LDAP-backed users have no hashed_password → 400 returned, no change made.
    """
    from passlib.context import CryptContext
    from db.database import SessionLocal
    from db.models import User
    from core.security_validation import password_strength_errors

    if not body.current_password or not body.new_password:
        raise HTTPException(status_code=400, detail="current_password and new_password are required")

    _pw_errors = password_strength_errors(body.new_password)
    if _pw_errors:
        raise HTTPException(status_code=400, detail="; ".join(_pw_errors))

    if body.current_password == body.new_password:
        raise HTTPException(status_code=400, detail="New password must be different from current password")

    pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
    db  = SessionLocal()
    try:
        user = db.query(User).filter(User.id == payload["sub"]).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # LDAP/SSO accounts have no local password — cannot change via this endpoint
        if not user.hashed_password:
            raise HTTPException(
                status_code=400,
                detail="Password change is not available for LDAP/SSO accounts. "
                       "Use your organisation's identity provider to change your password.",
            )

        # Verify current password
        if not pwd.verify(body.current_password, user.hashed_password):
            raise HTTPException(status_code=400, detail="Current password is incorrect")

        # Update to new password
        user.hashed_password = pwd.hash(body.new_password)
        # GAP-6: clear temp-password flag — user has now set their own password
        user.is_temp_password = False
        db.commit()

        logger.info("auth.change_password.ok", user_id=str(user.id))
        return {"success": True, "message": "Password changed successfully"}

    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        logger.error("auth.change_password.error", error=str(exc))
        raise HTTPException(status_code=500, detail="Password change failed")
    finally:
        db.close()


# ============================================================
# FORGOT PASSWORD  (GAP-6)
# POST /auth/forgot-password
#
# OSS flow:
#   1. User submits their email on the login page.
#   2. Backend generates a secure 12-char temporary password.
#   3. Hashes it, stores it on the user row, sets is_temp_password=True.
#   4. SMTP configured  → sends email with the temp password.
#      SMTP not configured → prints temp password to server console log.
#   5. Always returns 200 (never reveals whether email exists).
#
# After login with temp credential:
#   - Profile page detects is_temp_password=True → shows amber banner.
#   - User changes password via Profile → Change Password.
#   - change_password endpoint clears is_temp_password=False.
#
# ENABLE_FORGOT_PASSWORD=false → 403 returned, feature hidden in UI.
# ============================================================

class ForgotPasswordRequest(BaseModel):
    email: str


@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordRequest, request: Request):
    """
    Generate a temporary password for the given email and deliver it.

    Always returns 200 — never reveals whether the email exists in the DB
    (prevents user enumeration attacks).
    """
    # Unauthenticated and destructive: it overwrites the account password with
    # no token or ownership proof, so without a limiter anyone who knows an
    # email address can lock that user out repeatedly. login and register were
    # already throttled; this endpoint was not.
    enforce_rate_limit(request, AUTH_FORGOT_PASSWORD)

    # ── Feature flag guard ────────────────────────────────────────────────
    from core.config import ENABLE_FORGOT_PASSWORD
    if not ENABLE_FORGOT_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forgot password is disabled on this platform.",
        )

    import secrets
    import string
    from passlib.context import CryptContext
    from db.database import SessionLocal
    from db.models import User
    from services.smtp_service import send_html_email, SMTP_HOST

    # Normalize email
    email = (body.email or "").strip().lower()
    if not email:
        # Return 200 even for empty email — consistent response.
        # temp_password is always present in this endpoint's schema (present-
        # but-null here, real value only on the one qualifying branch below)
        # so its mere presence/absence can never be used to distinguish these
        # cases from a real reset — see SHOW_TEMP_PASSWORD_IN_UI in core/config.py.
        return {"success": True, "temp_password": None}

    pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()

        if not user:
            # User not found — return 200 silently (no enumeration)
            logger.info("auth.forgot_password.email_not_found", email=email)
            return {"success": True, "temp_password": None}

        if not user.hashed_password:
            # LDAP/SSO user — no local password, cannot reset this way
            logger.info("auth.forgot_password.ldap_user_skipped", email=email)
            return {"success": True, "temp_password": None}

        # ── Generate a secure temporary password ──────────────────────────
        # 12 chars: letters + digits + safe special chars
        # Readable alphabet — avoids ambiguous chars (0/O, 1/l/I)
        alphabet = (
            "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz"
            "23456789!@#$"
        )
        temp_password = "".join(secrets.choice(alphabet) for _ in range(12))

        # ── Store hashed temp password + set flag ─────────────────────────
        user.hashed_password  = pwd_ctx.hash(temp_password)
        user.is_temp_password = True
        db.commit()

        # Clear the failed-login counter. _check_lockout() runs before any
        # credential check on login, and only a *successful* login used to
        # clear it — so the very users who need a reset (they just failed
        # enough times to trip the lockout) were handed a valid temp password
        # and then rejected with 429 until the window expired.
        try:
            _clear_failure_counter(email, db, User)
        except Exception as _lockout_exc:
            logger.warning("auth.forgot_password.lockout_clear_failed", error=str(_lockout_exc))

        # ── Deliver the temp password ─────────────────────────────────────
        smtp_on = bool(SMTP_HOST)
        # SHOW_TEMP_PASSWORD_IN_UI (core/config.py) only ever applies to the
        # no-SMTP fallback below. Email is the preferred, already-authenticated
        # channel (only the account owner's inbox receives it); returning the
        # password over this UNAUTHENTICATED HTTP response as well would widen
        # exposure for no benefit whenever email delivery is already working.
        from core.config import SHOW_TEMP_PASSWORD_IN_UI
        response_temp_password = None

        if smtp_on:
            # Send email with temp password
            html_body = f"""
            <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:24px">
              <h2 style="color:#4f46e5;margin-bottom:8px">AiNxt — Temporary Password</h2>
              <p style="color:#374151;font-size:14px">
                A temporary password has been generated for your account.
              </p>
              <div style="background:#f3f4f6;border-radius:8px;padding:16px;margin:16px 0;text-align:center">
                <span style="font-size:22px;font-weight:700;letter-spacing:3px;color:#111827;font-family:monospace">
                  {temp_password}
                </span>
              </div>
              <p style="color:#374151;font-size:13px">
                Log in with this temporary password, then go to
                <strong>Profile → Security</strong> to set a new password immediately.
              </p>
              <p style="color:#9ca3af;font-size:12px;margin-top:16px">
                If you did not request this, please contact your administrator.
              </p>
            </div>
            """
            text_body = (
                f"AiNxt — Temporary Password\n\n"
                f"Your temporary password is: {temp_password}\n\n"
                f"Log in with this password, then go to Profile → Security "
                f"to set a new password immediately.\n\n"
                f"If you did not request this, contact your administrator."
            )
            sent = send_html_email(
                to=[email],
                subject="AiNxt — Your Temporary Password",
                html_body=html_body,
                text_body=text_body,
            )
            if sent:
                logger.info("auth.forgot_password.email_sent", email=email)
            else:
                logger.warning("auth.forgot_password.email_send_failed", email=email)
        else:
            # SMTP not configured — the OSS default, so this is the primary
            # delivery path, not a fallback.
            #
            # print(), not logger: the "ainxt" logger sets propagate=False and
            # attaches only a rotating FILE handler (core/logger.py), so this
            # block used to land in log/app/agent.log and never on stdout —
            # while the login page told the user it had been "printed to the
            # server console log". Same pattern as the seed-admin credentials
            # in gateway.py, which is visible in `docker compose logs gateway`.
            print("\n" + "=" * 60)
            print("  AiNxt — Temporary password issued")
            print("=" * 60)
            print(f"  Email    : {email}")
            print(f"  Password : {temp_password}")
            print("-" * 60)
            print("  ⚠  Shown ONCE. Any earlier temp password is now invalid.")
            print("     Log in, then change it in Profile → Security.")
            print("=" * 60 + "\n", flush=True)

            # Audit trail without the secret: the password now lives only in
            # the console stream (plus the API response below when the admin
            # has explicitly opted into that — see SHOW_TEMP_PASSWORD_IN_UI),
            # not in the persisted application log.
            logger.warning("auth.forgot_password.printed_to_console", email=email)

            if SHOW_TEMP_PASSWORD_IN_UI:
                response_temp_password = temp_password

        return {"success": True, "temp_password": response_temp_password}

    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        logger.error("auth.forgot_password.error", error=str(exc))
        # Still return 200 — never reveal internal errors to the caller
        return {"success": True, "temp_password": None}
    finally:
        db.close()
