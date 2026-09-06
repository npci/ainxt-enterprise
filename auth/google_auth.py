# SPDX-License-Identifier: MIT
# ============================================================
# GOOGLE SIGN-IN (OIDC authorization-code + PKCE)
#
# "Continue with Google" for the login AND the sign-up screen.
#
# DESIGN — this module is deliberately ADDITIVE:
#   * It does not touch auth/sso.py (Keycloak / Azure AD / desktop / Office).
#   * It does not touch /auth/login, /auth/register, LDAP or SCIM.
#   * It adds no table and no migration.
#   * ENABLE_GOOGLE_LOGIN defaults to false, so an existing deployment that
#     upgrades without changing any env var behaves exactly as before.
#
# CONFIG SOURCE — the OAuth client id/secret env-var names and the Google
# endpoints are NOT hardcoded here. They are read from the `gmail` connector
# definition in `ainxt.connector_definitions` (seeded by connectors/seed.py),
# exactly the way routers/connectors_router.py builds its OAuth2Config. One
# place to configure Google for both the Gmail connector and sign-in.
#
# Two fields from that row are deliberately OVERRIDDEN for login:
#   scopes       -> openid/email/profile only. The connector row also lists
#                   gmail.readonly / gmail.modify / gmail.send; requesting those
#                   at login would put "read and send your email" on the consent
#                   screen. Login must never ask for mailbox access.
#   extra_params -> {"prompt": "select_account"}. The connector row carries
#                   {"access_type": "offline", "prompt": "consent"}: `consent`
#                   would re-prompt on EVERY login, and `offline` asks for a
#                   refresh token we neither need nor store.
#
# Env vars:
#   ENABLE_GOOGLE_LOGIN        — "true" to enable. Default false.
#   GOOGLE_CLIENT_ID           — resolved via the connector row's client_id_env
#   GOOGLE_CLIENT_SECRET       — resolved via the connector row's client_secret_env
#   GOOGLE_LOGIN_REDIRECT_URI  — optional explicit override
#   GOOGLE_AUTO_PROVISION      — create a user on first sign-in.
#                                Defaults to ENABLE_SELF_REGISTRATION.
#   GOOGLE_POST_LOGIN_REDIRECT — where to land after success. Default /portal/
#
# Endpoints (mounted under /ainxt/v1/api by gateway.py):
#   GET /auth/google/login     — 302 to Google
#   GET /auth/google/callback  — code exchange, sign in, 302 to the SPA
# ============================================================

import base64
import json
import os
import secrets
import time
import uuid as _uuid_mod
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from core.logger import logger, mask_email

google_auth_router = APIRouter(prefix="/auth/google", tags=["auth"])

# The connector definition we borrow the Google OAuth client + endpoints from.
_CONNECTOR_NAME = "gmail"

# Login asks for identity only — never mailbox access. See module docstring.
_LOGIN_SCOPES = ["openid", "email", "profile"]

# `prompt` overrides the connector row's "consent", which would re-show the
# consent screen on every single login.
#
# `access_type` overrides a value that is NOT in the connector row: OAuth2Handler
# .generate_authorize_url hardcodes access_type=offline for every provider so
# connectors get a refresh token. Sign-in reads the ID token once and keeps
# nothing, so asking for offline access here would be requesting a durable grant
# we never use. generate_authorize_url applies extra_params last
# (params.update(config.extra_params)), so setting it here wins without changing
# the shared connector behaviour.
_LOGIN_EXTRA_PARAMS = {"prompt": "select_account", "access_type": "online"}

# Written into the OAuth state payload and asserted on the way back. The
# connector flow and this flow share the `connector:oauth:state:*` keyspace in
# the workflow KV, so without this marker a state minted by
# GET /connectors/oauth/start/gmail could be redeemed at the login callback
# (and vice versa).
_STATE_MARKER = "__google_login__"

# The connector row is admin-editable via PUT /connectors/definitions/gmail,
# which would otherwise let an admin repoint the login page at an arbitrary
# host. Pin the hosts we are willing to send a user (or an auth code) to.
_ALLOWED_HOSTS = {"accounts.google.com", "oauth2.googleapis.com"}

# Accepted `iss` values in Google's ID token.
_ALLOWED_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}


# Set once the "flag on but credentials missing" warning has been emitted, so
# it does not repeat on every /auth/ui-config request.
_WARNED_MISSING_CREDENTIALS = False


def _flag_on() -> bool:
    return os.getenv("ENABLE_GOOGLE_LOGIN", "false").strip().lower() == "true"


# ── Feature flag ──────────────────────────────────────────────

def google_login_enabled() -> bool:
    """True when the flag is on AND a usable client id is configured.

    Called by GET /auth/ui-config, so the button never renders on a deployment
    that could not complete the flow anyway.
    """
    if not _flag_on():
        return False
    try:
        return _load_oauth_config() is not None
    except Exception:
        return False


def _auto_provision_enabled() -> bool:
    """Create a user row on first Google sign-in?

    Defaults to ENABLE_SELF_REGISTRATION: a deployment that has closed
    self-registration should not get an open back door via Google.
    """
    raw = os.getenv("GOOGLE_AUTO_PROVISION", "").strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    from core.config import ENABLE_SELF_REGISTRATION
    return ENABLE_SELF_REGISTRATION


# ── Config, loaded from the gmail connector definition ────────

def _load_connector_auth_config() -> dict:
    """Read `auth_config` off the `gmail` row in ainxt.connector_definitions.

    A local reader rather than routers.connectors_router._load_definition so
    that the auth package never imports a router module.
    """
    import sqlalchemy as sa
    from db.database import SessionLocal

    db = SessionLocal()
    try:
        row = db.execute(
            sa.text(
                "SELECT auth_config FROM ainxt.connector_definitions "
                "WHERE name = :name"
            ),
            {"name": _CONNECTOR_NAME},
        ).fetchone()
    finally:
        db.close()

    if not row or not row[0]:
        raise RuntimeError(
            f"connector {_CONNECTOR_NAME!r} has no auth_config — run connectors/seed.py"
        )
    raw = row[0]
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"connector {_CONNECTOR_NAME!r} auth_config is not valid JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"connector {_CONNECTOR_NAME!r} auth_config is not an object")
    return parsed


def _assert_allowed(url: str, field: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise RuntimeError(
            f"{field} host {host!r} is not an allowed Google endpoint "
            f"(expected one of {sorted(_ALLOWED_HOSTS)})"
        )


def _load_oauth_config():
    """Build an OAuth2Config for the LOGIN flow, or None if unusable.

    Same construction as routers/connectors_router.py::oauth_start, with the
    scope and extra_params overrides described in the module docstring.
    """
    from connectors.base import OAuth2Config

    raw = _load_connector_auth_config()

    authorize_url = (raw.get("authorize_url") or "").strip()
    token_url = (raw.get("token_url") or "").strip()
    if not authorize_url or not token_url:
        raise RuntimeError(
            f"connector {_CONNECTOR_NAME!r} is missing authorize_url/token_url"
        )
    _assert_allowed(authorize_url, "authorize_url")
    _assert_allowed(token_url, "token_url")

    client_id_env = (raw.get("client_id_env") or "").strip()
    client_secret_env = (raw.get("client_secret_env") or "").strip()
    if not client_id_env or not client_secret_env:
        raise RuntimeError(
            f"connector {_CONNECTOR_NAME!r} is missing client_id_env/client_secret_env"
        )

    # Only the env-var NAMES live in the DB; the values are resolved here.
    missing = [
        name for name in (client_id_env, client_secret_env)
        if not os.getenv(name, "").strip()
    ]
    if missing:
        # Warn LOUDLY, but only once per process. Without this the failure is
        # completely silent: /auth/ui-config just reports the feature as
        # unavailable and the button never renders, with nothing anywhere
        # saying why. The usual cause is a `docker compose up` issued from a
        # terminal opened before the variables were set — Compose then
        # interpolates them to "" and the container starts with blank
        # credentials. Warn-once because /auth/ui-config is hit on every
        # single load of the login page.
        global _WARNED_MISSING_CREDENTIALS
        if _flag_on() and not _WARNED_MISSING_CREDENTIALS:
            _WARNED_MISSING_CREDENTIALS = True
            logger.warning(
                "google_auth: ENABLE_GOOGLE_LOGIN=true but %s %s not set in the "
                "gateway environment — the Google sign-in button will NOT be "
                "shown. These are read from the system environment, not .env; "
                "run `docker compose up -d gateway` from a terminal opened "
                "AFTER setting them (see .env.example).",
                " and ".join(missing),
                "is" if len(missing) == 1 else "are",
            )
        return None

    return OAuth2Config(
        authorize_url=authorize_url,
        token_url=token_url,
        client_id_env=client_id_env,
        client_secret_env=client_secret_env,
        scopes=list(_LOGIN_SCOPES),
        pkce=bool(raw.get("pkce", True)),
        extra_params=dict(_LOGIN_EXTRA_PARAMS),
    )


def _redirect_uri() -> str:
    """The redirect URI registered with Google for the LOGIN flow.

    Must be added to the same OAuth client as an additional authorized
    redirect URI. Note the /ainxt/v1/api prefix: ai-ui/nginx.conf only proxies
    that path to the gateway.
    """
    explicit = os.getenv("GOOGLE_LOGIN_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    base = (
        os.getenv("CONNECTOR_OAUTH_REDIRECT_BASE", "").strip()
        or os.getenv("PLATFORM_BASE_URL", "").strip()
    ).rstrip("/")
    if not base:
        raise RuntimeError(
            "Set GOOGLE_LOGIN_REDIRECT_URI (or CONNECTOR_OAUTH_REDIRECT_BASE / "
            "PLATFORM_BASE_URL) so the Google redirect URI can be built"
        )
    return f"{base}/ainxt/v1/api/auth/google/callback"


def _post_login_redirect() -> str:
    # /portal/ — ai-ui builds with base '/portal/' (ai-ui/vite.config.js) and
    # nginx only serves the SPA under /portal/*.
    return os.getenv("GOOGLE_POST_LOGIN_REDIRECT", "/portal/")


def _fail(reason: str) -> RedirectResponse:
    """Send the browser back to the login page with a machine-readable reason."""
    target = _post_login_redirect()
    joiner = "&" if "?" in target else "?"
    return RedirectResponse(url=f"{target}{joiner}auth_error={reason}", status_code=302)


# ── ID token ──────────────────────────────────────────────────

def _decode_id_token(id_token: str, client_id: str) -> dict:
    """Decode and validate Google's ID token claims.

    The signature is NOT verified locally, and that is deliberate. This token
    was just received in the body of a direct, server-to-server TLS response
    from Google's token endpoint, in exchange for a single-use code plus our
    client_secret. OIDC Core section 3.1.3.7 makes signature validation
    optional in exactly that case. It also keeps us off jwt.PyJWKClient, which
    fetches JWKS over its own urllib connection and would therefore bypass
    connectors/net_relay on a host with no direct internet egress (the same
    reason auth/sso.py routes its calls through the relay).

    iss / aud / exp / sub are all still enforced below; the caller additionally
    enforces email_verified.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("id_token is not a JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))

    if claims.get("iss") not in _ALLOWED_ISSUERS:
        raise ValueError(f"unexpected issuer {claims.get('iss')!r}")

    if claims.get("aud") != client_id:
        raise ValueError("id_token audience does not match our client id")

    exp = int(claims.get("exp", 0))
    if exp and exp < int(time.time()):
        raise ValueError("id_token has expired")

    if not claims.get("sub"):
        raise ValueError("id_token has no subject")

    return claims


# ── User lookup / provisioning ────────────────────────────────

def _upsert_google_user(sub: str, email: str, name: str) -> dict:
    """Find, or optionally create, the platform user behind a Google identity.

    Matching order:
      1. (sso_provider='google', sso_subject=<sub>) — the stable Google user id
      2. email — safe here only because the caller has already required
         email_verified=true, and Google is the authoritative issuer for the
         address it verified.

    On an email match the SSO columns are backfilled ONLY when both are NULL.
    They are single-valued and already claimed by Keycloak, Azure AD and SCIM
    (routers/scim_router.py writes sso_provider='scim'), so overwriting them
    would silently unlink an existing identity.
    """
    import datetime as _dt

    from db.database import SessionLocal
    from db.models import User
    from core.config import DEFAULT_AD_LEVEL

    email = (email or "").strip().lower()
    now = _dt.datetime.utcnow()
    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.sso_provider == "google", User.sso_subject == sub)
            .first()
        )
        if not user and email:
            user = db.query(User).filter(User.email == email).first()

        if user:
            if not user.is_active:
                raise HTTPException(status_code=403, detail="account_disabled")
            if not user.sso_provider and not user.sso_subject:
                user.sso_provider = "google"
                user.sso_subject = sub
            if name and not user.name:
                user.name = name
            if not user.email_verified:
                user.email_verified = True
            # Same stamp the password path writes (routers/auth_router.py), so a
            # Google user does not show as "never logged in" in the admin user
            # list and session views. Always written, so this commits every time.
            user.last_login_at = now
            db.commit()
            db.refresh(user)
        else:
            if not _auto_provision_enabled():
                raise HTTPException(status_code=403, detail="not_registered")
            # Match POST /auth/register's defaults, not auth/sso.py's: role
            # "user" (auth/rbac.py ranks "viewer" strictly lower) and an
            # explicit DEFAULT_AD_LEVEL rather than the column default of 6.
            user = User(
                email=email,
                name=name or email,
                role="user",
                sso_provider="google",
                sso_subject=sub,
                ad_level=DEFAULT_AD_LEVEL,
                email_verified=True,
                is_active=True,
                last_login_at=now,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            logger.info("google_auth: provisioned new user %s", mask_email(email))

        return {
            "id": str(user.id),
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "org_id": user.org_id or "",
            "ad_level": user.ad_level if user.ad_level is not None else 6,
            "is_security_team": bool(getattr(user, "is_security_team", False)),
        }
    finally:
        db.close()


# ── Endpoints ─────────────────────────────────────────────────

@google_auth_router.get("/login")
def google_login(request: Request):
    """Begin the Google sign-in flow — 302 to Google's consent screen."""
    if not _flag_on():
        raise HTTPException(status_code=404, detail="Google sign-in is not enabled")

    from connectors.oauth2 import oauth2_handler

    try:
        config = _load_oauth_config()
    except Exception as exc:
        logger.error("google_auth: cannot load Google OAuth config — %s", exc)
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    if config is None:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")

    try:
        redirect_uri = _redirect_uri()
        state = secrets.token_urlsafe(32)
        authorize_url, verifier = oauth2_handler.generate_authorize_url(
            config, redirect_uri, state
        )
        # user_id is empty — nobody is signed in yet. _STATE_MARKER goes in the
        # connector_name slot so the connector callback cannot redeem this state.
        oauth2_handler.save_state(state, "", _STATE_MARKER, verifier)
    except Exception as exc:
        logger.error("google_auth: failed to start Google sign-in — %s", exc)
        raise HTTPException(status_code=500, detail="Failed to start Google sign-in")

    return RedirectResponse(url=authorize_url, status_code=302)


@google_auth_router.get("/callback")
def google_callback(
    request: Request,
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
):
    """Complete the Google sign-in flow and establish a platform session.

    The JWT mint / session registration / cookie / redirect tail is the same
    sequence as auth/sso.py::sso_callback, including samesite="lax" — a Strict
    cookie is dropped by the browser on this cross-site top-level navigation
    back from Google, which would leave the SPA's /auth/me call unauthenticated.
    """
    if not _flag_on():
        raise HTTPException(status_code=404, detail="Google sign-in is not enabled")

    from core.rate_limiter import enforce_rate_limit, SSO_CALLBACK
    enforce_rate_limit(request, SSO_CALLBACK)

    if error:
        logger.warning("google_auth: provider returned error=%s", error)
        return _fail("google_denied")
    if not code or not state:
        return _fail("google_bad_request")

    from connectors.oauth2 import oauth2_handler

    # Consume-on-read: this is the CSRF defence for the whole flow.
    saved = oauth2_handler.load_state(state)
    if not saved:
        logger.warning("google_auth: unknown or expired OAuth state")
        return _fail("google_state_expired")
    if saved.get("connector_name") != _STATE_MARKER:
        logger.warning("google_auth: state belongs to another OAuth flow — rejected")
        return _fail("google_state_invalid")

    try:
        config = _load_oauth_config()
        if config is None:
            raise RuntimeError("Google OAuth client is not configured")
        token_set = oauth2_handler.exchange_code(
            config, code, _redirect_uri(), saved.get("pkce_verifier", "")
        )
    except Exception as exc:
        logger.error("google_auth: code exchange failed — %s", exc)
        return _fail("google_exchange_failed")

    id_token = (token_set.metadata or {}).get("id_token", "")
    if not id_token:
        logger.error("google_auth: token response carried no id_token")
        return _fail("google_no_identity")

    try:
        claims = _decode_id_token(id_token, os.getenv(config.client_id_env, ""))
    except Exception as exc:
        logger.warning("google_auth: id_token rejected — %s", exc)
        return _fail("google_invalid_token")

    email = (claims.get("email") or "").strip().lower()
    if not email:
        return _fail("google_no_email")
    # Never match an existing account on an address Google has not verified.
    if claims.get("email_verified") not in (True, "true"):
        logger.warning("google_auth: refused unverified address %s", mask_email(email))
        return _fail("google_email_unverified")

    try:
        db_user = _upsert_google_user(
            sub=claims["sub"], email=email, name=(claims.get("name") or "").strip()
        )
    except HTTPException as exc:
        return _fail(str(exc.detail))
    except Exception as exc:
        logger.error("google_auth: user upsert failed — %s", exc)
        return _fail("google_user_error")

    # Best-effort org_tree enrichment, same as the SSO callback.
    try:
        from routers.auth_router import _sync_user_from_org_tree
        _sync_user_from_org_tree(db_user["id"], db_user["email"])
    except Exception as exc:
        logger.debug("google_auth: org_tree sync skipped — %s", exc)

    from auth.jwt_handler import encode_token

    session_id = str(_uuid_mod.uuid4())
    token = encode_token(
        user_id=db_user["id"],
        role=db_user["role"],
        org_id=db_user["org_id"],
        is_security_team=db_user["is_security_team"],
        ad_level=db_user["ad_level"],
        session_id=session_id,
    )
    try:
        import jwt as _jwtlib
        from auth.session_manager import register_session
        jti = _jwtlib.decode(token, options={"verify_signature": False}).get("jti", "")
        register_session(
            user_id=db_user["id"],
            session_id=session_id,
            jti=jti,
            ip_address=(request.client.host if request and request.client else "") or "",
            user_agent=request.headers.get("User-Agent", "")[:512] if request else "",
        )
    except Exception as exc:
        logger.warning("google_auth: session registration error — %s", exc)

    try:
        from auth.dependencies import enrich_user_context
        enrich_user_context({"sub": db_user["id"]})
    except Exception:
        pass

    logger.info("google_auth: %s signed in via Google", mask_email(db_user["email"]))

    from routers.auth_router import _set_auth_cookie
    redirect = RedirectResponse(url=_post_login_redirect(), status_code=302)
    _set_auth_cookie(redirect, token, samesite="lax")
    return redirect
