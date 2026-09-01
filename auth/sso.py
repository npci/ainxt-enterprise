# SPDX-License-Identifier: Apache-2.0
# ============================================================
# SSO PROVIDER HOOK
# Supports Keycloak and Azure AD (structure ready, activation via env vars)
#
# Env vars:
#   SSO_PROVIDER            — "keycloak" | "azure_ad" | "" (disabled)
#   KEYCLOAK_URL            — https://keycloak.ainxt.org/realms/ainxt
#   KEYCLOAK_CLIENT_ID      — ainxt  (OSS default; override per deployment)
#   KEYCLOAK_CLIENT_SECRET  — secret
#
#   SSO-specific Azure AD app registration (Desktop SSO login):
#   SSO_AZURE_TENANT_ID     — tenant id for the SSO app registration
#   SSO_AZURE_CLIENT_ID     — client id for the SSO app registration
#   SSO_AZURE_CLIENT_SECRET — client secret for the SSO app registration
#   Falls back to AZURE_AD_* if SSO_AZURE_* are not set, so existing
#   single-registration deployments keep working without any change.
#
#   Connector/Graph Azure AD app registration (M365 connectors, AB Studio):
#   AZURE_AD_TENANT_ID      — azure tenant id  (unchanged — connectors use this)
#   AZURE_AD_CLIENT_ID      — azure app client id  (unchanged)
#   AZURE_AD_CLIENT_SECRET  — azure client secret  (unchanged)
#
# All HTTP calls use urllib.request only (stdlib — no httpx/requests).
# JWT validation uses the PyJWT library (already a platform dependency).
#
# FastAPI router exported as `sso_router` — includes:
#   GET  /auth/sso/provider   — returns {provider, enabled, login_url}
#   GET  /auth/sso/callback   — handles OAuth2 callback ?code=...&state=...
# ============================================================

import json
import os
import urllib.parse
from typing import Optional

import httpx as _httpx

# Shared connection pool — reused across all SSO calls (local dev / direct path).
# At 2000 users doing OAuth2, this saves ~100 ms TCP handshake per login.
_http = _httpx.Client(
    timeout=15.0,
    limits=_httpx.Limits(max_connections=20, max_keepalive_connections=10),
    follow_redirects=True,
)

# Outbound relay — routes Microsoft calls through web02 when LLM_PROXY_URL is
# set (app01 has no internet egress). Falls back to direct httpx when unset
# (local dev or any host with direct internet). Same relay used by M365 connectors.
try:
    from connectors.net_relay import relay_request as _relay_request
    _HAS_RELAY = True
except ImportError:
    _HAS_RELAY = False

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel as _BaseModel

from core.config import PLATFORM_BASE_URL as _CONFIG_PLATFORM_BASE_URL
from core.logger import logger, mask_email

# ── JWKS cache: {url: {keys}} ────────────────────────────────
_jwks_cache: dict = {}


# ── SSO-specific Azure AD credentials ────────────────────────
# SSO_AZURE_* are for the Desktop SSO app registration only.
# Falls back to AZURE_AD_* so single-registration deployments
# keep working without any .env change.
def _sso_tenant_id()     -> str: return os.getenv("SSO_AZURE_TENANT_ID")     or os.getenv("AZURE_AD_TENANT_ID", "")
def _sso_client_id()     -> str: return os.getenv("SSO_AZURE_CLIENT_ID")     or os.getenv("AZURE_AD_CLIENT_ID", "")
def _sso_client_secret() -> str: return os.getenv("SSO_AZURE_CLIENT_SECRET") or os.getenv("AZURE_AD_CLIENT_SECRET", "")


# ============================================================
# PUBLIC API
# ============================================================

def get_sso_provider() -> str:
    """Return the active SSO provider name or "none".

    Resolution order:
    1. SSO_PROVIDER env var (explicit override)
    2. Keycloak if KEYCLOAK_URL + KEYCLOAK_CLIENT_ID are set
    3. Azure AD if AZURE_AD_TENANT_ID + AZURE_AD_CLIENT_ID are set
    4. "none" (SSO disabled)
    """
    explicit = os.getenv("SSO_PROVIDER", "").strip().lower()
    if explicit in ("keycloak", "azure_ad"):
        return explicit
    if explicit and explicit not in ("", "none"):
        logger.warning(f"SSO: unknown SSO_PROVIDER value '{explicit}' — treating as disabled")

    if os.getenv("KEYCLOAK_URL") and os.getenv("KEYCLOAK_CLIENT_ID"):
        return "keycloak"
    # SSO_AZURE_* take priority over AZURE_AD_* for auto-detection
    if (os.getenv("SSO_AZURE_TENANT_ID") or os.getenv("AZURE_AD_TENANT_ID")) and \
       (os.getenv("SSO_AZURE_CLIENT_ID") or os.getenv("AZURE_AD_CLIENT_ID")):
        return "azure_ad"
    return "none"


def validate_sso_token(token: str) -> Optional[dict]:
    """Validate an SSO-issued JWT and return a normalised user dict.

    Returns {user_id, email, name, role} on success, or None on failure.
    - For keycloak: validates the JWT signature against the Keycloak JWKS endpoint.
    - For azure_ad: validates the JWT signature against the Azure JWKS endpoint.
    - Falls back gracefully if the provider is not configured (returns None).
    """
    provider = get_sso_provider()
    if provider == "none":
        logger.warning("SSO: validate_sso_token called but no SSO provider is configured")
        return None

    try:
        if provider == "keycloak":
            return _validate_keycloak_token(token)
        if provider == "azure_ad":
            return _validate_azure_token(token)
    except Exception as exc:
        logger.warning(f"SSO: token validation failed for provider '{provider}' — {exc}")
    return None


def get_sso_login_url(redirect_uri: str) -> str:
    """Return the OAuth2 authorization URL for the active provider.

    Raises ValueError if no SSO provider is configured.
    """
    provider = get_sso_provider()
    if provider == "none":
        raise ValueError("No SSO provider is configured")

    if provider == "keycloak":
        base_url  = os.getenv("KEYCLOAK_URL", "").rstrip("/")
        client_id = os.getenv("KEYCLOAK_CLIENT_ID", "ainxt")
        auth_ep   = f"{base_url}/protocol/openid-connect/auth"
        params = {
            "response_type": "code",
            "client_id":     client_id,
            "redirect_uri":  redirect_uri,
            "scope":         "openid email profile",
        }
        return f"{auth_ep}?{urllib.parse.urlencode(params)}"

    if provider == "azure_ad":
        tenant_id = _sso_tenant_id()
        client_id = _sso_client_id()
        auth_ep   = (
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"
        )
        params = {
            "response_type": "code",
            "client_id":     client_id,
            "redirect_uri":  redirect_uri,
            # offline_access → refresh token for desktop silent re-login.
            # User.Read → the access token may call Graph /me for identity.
            "scope":         "openid email profile offline_access User.Read",
            "response_mode": "query",
        }
        return f"{auth_ep}?{urllib.parse.urlencode(params)}"

    raise ValueError(f"Unsupported SSO provider: {provider!r}")


def refresh_token(provider: str, refresh_token_value: str) -> Optional[dict]:
    """
    Call the provider token endpoint with grant_type=refresh_token.
    Returns {access_token, refresh_token, expires_in} or None on failure.
    """
    try:
        if provider == "keycloak":
            base_url      = os.getenv("KEYCLOAK_URL", "").rstrip("/")
            client_id     = os.getenv("KEYCLOAK_CLIENT_ID", "ainxt")
            client_secret = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
            token_ep      = f"{base_url}/protocol/openid-connect/token"
            data = _http_post_form(token_ep, {
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token_value,
                "client_id":     client_id,
                "client_secret": client_secret,
            })
        elif provider == "azure_ad":
            tenant_id     = _sso_tenant_id()
            client_id     = _sso_client_id()
            client_secret = _sso_client_secret()
            token_ep      = (
                f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
            )
            data = _http_post_form(token_ep, {
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token_value,
                "client_id":     client_id,
                "client_secret": client_secret,
                "scope":         "openid email profile offline_access",
            })
        else:
            logger.warning(f"SSO: refresh_token called for unknown provider '{provider}'")
            return None

        if not data.get("access_token"):
            logger.warning(f"SSO: refresh_token response missing access_token for {provider}")
            return None

        return {
            "access_token":  data["access_token"],
            "refresh_token": data.get("refresh_token", refresh_token_value),
            "expires_in":    data.get("expires_in", 3600),
        }
    except Exception as exc:
        logger.error(f"SSO: refresh_token failed for provider '{provider}' — {exc}")
        return None


def exchange_sso_code(code: str, redirect_uri: str) -> Optional[dict]:
    """Exchange an OAuth2 authorization code for normalised user info.

    Returns {user_id, email, name, role} on success, or None on failure.
    """
    provider = get_sso_provider()
    if provider == "none":
        logger.warning("SSO: exchange_sso_code called but no SSO provider is configured")
        return None

    try:
        if provider == "keycloak":
            return _exchange_keycloak_code(code, redirect_uri)
        if provider == "azure_ad":
            return _exchange_azure_code(code, redirect_uri)
    except Exception as exc:
        logger.warning(f"SSO: code exchange failed for provider '{provider}' — {exc}")
    return None


# ============================================================
# KEYCLOAK IMPLEMENTATION
# ============================================================

def _keycloak_jwks_url() -> str:
    base_url = os.getenv("KEYCLOAK_URL", "").rstrip("/")
    return f"{base_url}/protocol/openid-connect/certs"


def _validate_keycloak_token(token: str) -> Optional[dict]:
    """Validate a Keycloak-issued JWT against the JWKS endpoint."""
    import jwt as pyjwt
    from jwt import PyJWKClient

    jwks_url  = _keycloak_jwks_url()
    client_id = os.getenv("KEYCLOAK_CLIENT_ID", "ainxt")

    try:
        jwks_client = PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=300)
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        payload = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id,
        )
    except pyjwt.ExpiredSignatureError:
        logger.warning("SSO[keycloak]: token expired")
        return None
    except pyjwt.InvalidTokenError as exc:
        logger.warning(f"SSO[keycloak]: invalid token — {exc}")
        return None

    return {
        "user_id": payload.get("sub", ""),
        "email":   payload.get("email", ""),
        "name":    payload.get("name", payload.get("preferred_username", "")),
        "role":    _map_keycloak_role(payload),
    }


def _map_keycloak_role(payload: dict) -> str:
    """Map Keycloak realm roles to platform roles."""
    realm_roles: list = payload.get("realm_access", {}).get("roles", [])
    if "admin" in realm_roles:
        return "admin"
    if "developer" in realm_roles:
        return "developer"
    if "operator" in realm_roles:
        return "operator"
    if "security" in realm_roles:
        return "security"
    return "viewer"


def _exchange_keycloak_code(code: str, redirect_uri: str) -> Optional[dict]:
    """Exchange a Keycloak auth code for tokens and return normalised user info."""
    base_url      = os.getenv("KEYCLOAK_URL", "").rstrip("/")
    client_id     = os.getenv("KEYCLOAK_CLIENT_ID", "ainxt")
    client_secret = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
    token_ep      = f"{base_url}/protocol/openid-connect/token"

    token_data = _http_post_form(token_ep, {
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  redirect_uri,
        "client_id":     client_id,
        "client_secret": client_secret,
    })

    access_token = token_data.get("access_token")
    if not access_token:
        logger.warning("SSO[keycloak]: token response missing access_token")
        return None

    userinfo_ep = f"{base_url}/protocol/openid-connect/userinfo"
    userinfo    = _http_get_bearer(userinfo_ep, access_token)

    return {
        "user_id": userinfo.get("sub", ""),
        "email":   userinfo.get("email", ""),
        "name":    userinfo.get("name", userinfo.get("preferred_username", "")),
        "role":    "viewer",
    }


# ============================================================
# AZURE AD IMPLEMENTATION
# ============================================================

def _azure_jwks_url() -> str:
    tenant_id = os.getenv("AZURE_AD_TENANT_ID", "")
    return (
        f"https://login.microsoftonline.com/{tenant_id}"
        "/discovery/v2.0/keys"
    )


def _validate_azure_token(token: str) -> Optional[dict]:
    """Validate an Azure AD-issued JWT against the Azure JWKS endpoint."""
    import jwt as pyjwt
    from jwt import PyJWKClient

    tenant_id = os.getenv("AZURE_AD_TENANT_ID", "")
    client_id = os.getenv("AZURE_AD_CLIENT_ID", "")
    jwks_url  = _azure_jwks_url()

    try:
        jwks_client = PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=300)
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        payload = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        )
    except pyjwt.ExpiredSignatureError:
        logger.warning("SSO[azure_ad]: token expired")
        return None
    except pyjwt.InvalidTokenError as exc:
        logger.warning(f"SSO[azure_ad]: invalid token — {exc}")
        return None

    email = payload.get("email") or payload.get("preferred_username", "")
    return {
        "user_id": payload.get("oid") or payload.get("sub", ""),
        "email":   email,
        "name":    payload.get("name", email),
        "role":    _map_azure_role(payload),
    }


def _map_azure_role(payload: dict) -> str:
    """Map Azure AD app roles to platform roles."""
    app_roles: list = payload.get("roles", [])
    if "admin" in app_roles:
        return "admin"
    if "developer" in app_roles:
        return "developer"
    if "operator" in app_roles:
        return "operator"
    if "security" in app_roles:
        return "security"
    return "viewer"


def _exchange_azure_code(code: str, redirect_uri: str) -> Optional[dict]:
    """Exchange an Azure AD auth code for tokens and return normalised user info."""
    tenant_id     = _sso_tenant_id()
    client_id     = _sso_client_id()
    client_secret = _sso_client_secret()
    token_ep      = (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    )

    token_data = _http_post_form(token_ep, {
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  redirect_uri,
        "client_id":     client_id,
        "client_secret": client_secret,
        # offline_access → Azure returns a refresh_token so the desktop app can
        # silently re-authenticate on launch (no login screen), like Outlook.
        "scope":         "openid email profile offline_access",
    })

    access_token = token_data.get("access_token")
    if not access_token:
        logger.warning("SSO[azure_ad]: token response missing access_token")
        return None

    graph_me = _http_get_bearer("https://graph.microsoft.com/v1.0/me", access_token)
    email    = graph_me.get("mail") or graph_me.get("userPrincipalName", "")

    return {
        "user_id":       graph_me.get("id", ""),
        "email":         email,
        "name":          graph_me.get("displayName", email),
        "role":          "user",   # default platform role for new SSO users
        # Surfaced so the desktop flow can persist it for silent re-login.
        "refresh_token": token_data.get("refresh_token", ""),
    }

def _exchange_azure_obo(assertion: str, scopes: Optional[str] = None) -> dict:
    """On-Behalf-Of token exchange (scope §6.3, Office add-in SSO).

    The Office add-in calls Office.auth.getAccessToken() to obtain a token for
    THIS app's exposed API (access_as_user). We exchange it (jwt-bearer grant,
    requested_token_use=on_behalf_of) for a downstream Microsoft Graph token —
    so the add-in never holds a Graph token directly and no end-user consent UI
    is needed beyond the centralized admin consent (§7.2).

    Returns the raw token response {access_token, refresh_token?, expires_in, scope}.
    """
    tenant_id     = os.getenv("AZURE_AD_TENANT_ID", "")
    client_id     = os.getenv("AZURE_AD_CLIENT_ID", "")
    client_secret = os.getenv("AZURE_AD_CLIENT_SECRET", "")
    token_ep      = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    # offline_access (NOT .default) so Azure returns a refresh token we can store
    # for delegated connector calls. Admins widen via AZURE_AD_OBO_SCOPES.
    obo_scopes    = scopes or os.getenv(
        "AZURE_AD_OBO_SCOPES",
        "openid profile offline_access https://graph.microsoft.com/User.Read",
    )

    return _http_post_form(token_ep, {
        "grant_type":           "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "client_id":            client_id,
        "client_secret":        client_secret,
        "assertion":            assertion,
        "scope":                obo_scopes,
        "requested_token_use":  "on_behalf_of",
    })


def _store_entra_oauth_token(user_id: str, token_data: dict, email: str = "") -> None:
    """Persist the OBO-minted Entra tokens in ainxt.user_oauth_tokens so the
    connector engine can make delegated Graph calls for this user (unifying the
    add-in user with the Microsoft 365 connector path). Best-effort — never
    fails the login.
    """
    access_token = token_data.get("access_token", "")
    if not access_token:
        return
    try:
        from store.credential_vault import encrypt_value
        from db.database import SessionLocal, DB_SCHEMA
        from sqlalchemy import text as _text

        enc_access  = encrypt_value(access_token)
        enc_refresh = encrypt_value(token_data["refresh_token"]) if token_data.get("refresh_token") else None
        scopes      = (token_data.get("scope") or "").split()
        expires_in  = int(token_data.get("expires_in", 3600))
        tenant_id   = os.getenv("AZURE_AD_TENANT_ID", "")

        db = SessionLocal()
        try:
            db.execute(
                _text(
                    f"INSERT INTO {DB_SCHEMA}.user_oauth_tokens "
                    f"(user_id, connector_name, access_token, refresh_token, expires_at, scopes, metadata, is_active) "
                    f"VALUES (:uid, 'microsoft_365', :at, :rt, NOW() + (:exp || ' seconds')::interval, "
                    f"        :sc, CAST(:meta AS JSONB), TRUE) "
                    f"ON CONFLICT (user_id, connector_name) DO UPDATE SET "
                    f"  access_token=:at, "
                    f"  refresh_token=COALESCE(:rt, {DB_SCHEMA}.user_oauth_tokens.refresh_token), "
                    f"  expires_at=NOW() + (:exp || ' seconds')::interval, "
                    f"  scopes=:sc, is_active=TRUE, updated_at=NOW()"
                ),
                {
                    "uid": user_id, "at": enc_access, "rt": enc_refresh,
                    "exp": str(expires_in), "sc": scopes,
                    "meta": json.dumps({"tenant_id": tenant_id, "email": email, "source": "office_obo"}),
                },
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.warning(f"SSO[office]: failed to store Entra OAuth token for {user_id} — {exc}")

# ============================================================
# HTTP HELPERS  (relay-aware — routes through web02 on app01, direct elsewhere)
# ============================================================

def _http_post_form(url: str, data: dict) -> dict:
    """POST application/x-www-form-urlencoded and return parsed JSON.

    Routes through the net_relay (web02) when LLM_PROXY_URL is set so that
    app01 — which has no internet egress — can reach login.microsoftonline.com
    and graph.microsoft.com. Falls back to direct httpx when relay is unavailable
    (local dev or any host with direct internet access).
    """
    try:
        if _HAS_RELAY and os.getenv("LLM_PROXY_URL", "").strip():
            resp = _relay_request("POST", url, data=data,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
        else:
            resp = _http.post(url, data=data,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp.raise_for_status()
        return resp.json()
    except _httpx.HTTPStatusError as exc:
        body = exc.response.text[:500]
        logger.error(f"SSO HTTP POST {exc.response.status_code} to {url}: {body}")
        raise RuntimeError(f"HTTP {exc.response.status_code}: {body}") from exc
    except Exception as exc:
        logger.error(f"SSO HTTP POST failed ({url}): {exc}")
        raise


def _http_get_bearer(url: str, access_token: str) -> dict:
    """GET with Bearer token and return parsed JSON.

    Routes through the net_relay (web02) when LLM_PROXY_URL is set so that
    app01 — which has no internet egress — can reach graph.microsoft.com.
    Falls back to direct httpx when relay is unavailable.
    """
    try:
        headers = {"Authorization": f"Bearer {access_token}"}
        if _HAS_RELAY and os.getenv("LLM_PROXY_URL", "").strip():
            resp = _relay_request("GET", url, headers=headers)
        else:
            resp = _http.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except _httpx.HTTPStatusError as exc:
        body = exc.response.text[:500]
        logger.error(f"SSO HTTP GET {exc.response.status_code} to {url}: {body}")
        raise RuntimeError(f"HTTP {exc.response.status_code}: {body}") from exc
    except Exception as exc:
        logger.error(f"SSO HTTP GET failed ({url}): {exc}")
        raise


# ============================================================
# FASTAPI SSO ROUTER  (exported as sso_router)
# Mounted at /auth/sso via gateway.py include
# ============================================================

sso_router = APIRouter(prefix="/auth/sso", tags=["auth"])


@sso_router.get("/provider")
def sso_provider_info():
    """Return the active SSO provider and a ready-to-use login URL.

    Response: {provider, enabled, login_url}
    """
    provider = get_sso_provider()
    enabled  = provider != "none"
    login_url = None
    if enabled:
        # No localhost default: reuses the canonical (also no-default) core.config value.
        _base = os.getenv("PLATFORM_BASE_URL", _CONFIG_PLATFORM_BASE_URL)
        redirect_uri = os.getenv(
            "SSO_REDIRECT_URI", f"{_base}/auth/sso/callback"
        )
        try:
            login_url = get_sso_login_url(redirect_uri=redirect_uri)
        except Exception:
            login_url = None
    return {
        "provider":  provider,
        "enabled":   enabled,
        "login_url": login_url,
    }


@sso_router.get("/callback")
def sso_callback(
    request: Request,
    code:  Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
):
    """Handle the OAuth2 callback after the user authenticates with the provider.

    Query params:
      code  — authorization code returned by the provider
      state — opaque CSRF state value
      error — set by the provider on auth failure

    On success: exchanges the code for user info, upserts + org-syncs the user,
    registers a session, sets the httpOnly `auth_token` cookie (SAME session model
    as password login), and **302-redirects the browser to the SPA (`/ui`)** —
    which then hydrates via /auth/me. This is the single authoritative SSO
    callback; the duplicate in auth_router.py delegates here.
    """
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"SSO provider returned error: {error}",
        )

    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing authorization code in SSO callback",
        )

    # No localhost default: reuses the canonical (also no-default) core.config value.
    _base = os.getenv("PLATFORM_BASE_URL", _CONFIG_PLATFORM_BASE_URL)
    redirect_uri = os.getenv(
        "SSO_REDIRECT_URI", f"{_base}/auth/sso/callback"
    )

    user_info = exchange_sso_code(code=code, redirect_uri=redirect_uri)
    if not user_info:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="SSO code exchange failed or provider not configured",
        )

    provider    = get_sso_provider()
    sso_subject = user_info.get("user_id", "")
    email       = user_info.get("email", "")
    name        = user_info.get("name", email)
    role        = user_info.get("role", "viewer")

    db_user = _upsert_sso_user(
        sso_subject=sso_subject,
        sso_provider=provider,
        email=email,
        name=name,
        role=role,
    )

    if not db_user:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upsert SSO user in database",
        )

    # Sync ad_level + department from org_tree (non-blocking, best-effort)
    try:
        from routers.auth_router import _sync_user_from_org_tree
        _sync_user_from_org_tree(db_user["id"], db_user["email"])
        from db.database import SessionLocal as _sl
        from db.models import User as _User
        _db = _sl()
        try:
            _u = _db.query(_User).filter(_User.id == db_user["id"]).first()
            if _u:
                db_user["ad_level"]   = _u.ad_level if _u.ad_level is not None else 6
                db_user["department"] = _u.department or ""
        finally:
            _db.close()
    except Exception as _se:
        logger.debug(f"SSO: org_tree sync failed (non-fatal): {_se}")

    from auth.jwt_handler import encode_token
    import uuid as _uuid_mod
    _ad_level   = db_user.get("ad_level", 6)
    _department = db_user.get("department", "")

    # Register a session (SAME model as password login) so decode_token's
    # is_session_active() check passes. Without a registered sid the cookie would
    # 401 on the first request. We generate the sid up-front and thread it into
    # BOTH the token and the session registry.
    session_id = str(_uuid_mod.uuid4())
    # DAST fix: encode_token no longer embeds PII in the JWT.
    token = encode_token(
        user_id=db_user["id"],
        role=db_user["role"],
        ad_level=_ad_level,
        session_id=session_id,
    )
    try:
        from auth.session_manager import register_session as _reg_session
        import jwt as _jwtlib
        _jti = _jwtlib.decode(token, options={"verify_signature": False}).get("jti", "")
        _ip  = (request.client.host if request and request.client else "") or ""
        _ua  = request.headers.get("User-Agent", "")[:512] if request else ""
        _reg_session(user_id=str(db_user["id"]), session_id=session_id, jti=_jti,
                     ip_address=_ip, user_agent=_ua)
    except Exception as _se:
        logger.warning("SSO callback: session registration error: %s", _se)

    # Pre-seed the profile cache so the first authenticated request hits Redis
    try:
        from auth.dependencies import enrich_user_context as _enrich_ctx
        _enrich_ctx({"sub": db_user["id"]})
    except Exception:
        pass

    logger.info(
        f"SSO callback: user '{email}' authenticated via '{provider}' "
        f"(subject={sso_subject}, ad_level={_ad_level}, dept={_department})"
    )

    # Browser flow: set the httpOnly cookie and redirect to the SPA. The SPA
    # restores the session via /auth/me (no token handling needed client-side).
    #
    # SEC-F-008: this is the ONE call site that must stay SameSite=Lax rather
    # than the platform's new Strict default. This response IS a cross-site
    # top-level navigation landing (the identity provider redirects the
    # browser back to us after authentication) — a Strict cookie set here
    # would be dropped by the browser, breaking the follow-up /auth/me
    # session re-hydration. Lax still permits the cookie on this kind of
    # top-level GET navigation, which is exactly what's needed here, while
    # remaining a real hardening improvement over the old blanket-Lax
    # default everywhere else in routers/auth_router.py::_set_auth_cookie.
    from routers.auth_router import _set_auth_cookie
    _ui_url = os.getenv("SSO_POST_LOGIN_REDIRECT", "/ui")
    redirect = RedirectResponse(url=_ui_url, status_code=302)
    _set_auth_cookie(redirect, token, samesite="lax")
    return redirect

class OfficeSSORequest(_BaseModel):
    assertion: str   # token from Office.auth.getAccessToken()


@sso_router.post("/office")
def sso_office(body: OfficeSSORequest):
    """Office add-in SSO (scope §6.3).

    Exchanges the Office.auth.getAccessToken() assertion for a Graph token via
    On-Behalf-Of, derives the user identity from Graph /me (authoritative — the
    OBO only succeeds for a valid assertion issued to THIS app), upserts the
    user, stores the Entra refresh token for connector reuse, and returns an
    AiNxt platform JWT in the SAME shape as /auth/sso/callback.
    """
    if get_sso_provider() != "azure_ad":
        raise HTTPException(status_code=400, detail="Azure AD SSO is not configured")
    if not body.assertion:
        raise HTTPException(status_code=400, detail="Missing Office SSO assertion")

    # 1. OBO exchange (Azure cryptographically validates the assertion)
    try:
        token_data = _exchange_azure_obo(body.assertion)
    except Exception as exc:
        logger.warning(f"SSO[office]: OBO exchange failed — {exc}")
        raise HTTPException(status_code=401, detail="Office SSO token exchange failed")

    graph_token = token_data.get("access_token", "")
    if not graph_token:
        raise HTTPException(status_code=401, detail="OBO response missing access_token")

    # 2. Authoritative identity from Graph /me
    try:
        graph_me = _http_get_bearer("https://graph.microsoft.com/v1.0/me", graph_token)
    except Exception as exc:
        logger.warning(f"SSO[office]: Graph /me failed — {exc}")
        raise HTTPException(status_code=502, detail="Could not resolve user from Microsoft Graph")

    sso_subject = graph_me.get("id", "")
    email       = graph_me.get("mail") or graph_me.get("userPrincipalName", "")
    name        = graph_me.get("displayName", email)

    db_user = _upsert_sso_user(
        sso_subject=sso_subject, sso_provider="azure_ad",
        email=email, name=name, role="viewer",
    )
    if not db_user:
        raise HTTPException(status_code=500, detail="Failed to upsert SSO user")

    # 3. Store Entra refresh token for delegated connector calls (best-effort)
    _store_entra_oauth_token(db_user["id"], token_data, email=email)

    # 4. Audit the boundary crossing — hash only, never the assertion (§7.4)
    try:
        from core import graph_audit
        graph_audit.record(
            f"obo:{db_user['id']}", graph_audit.EVENT_OBO_EXCHANGE,
            data=body.assertion, user_id=db_user["id"], resource=email,
            meta={"scopes": (token_data.get("scope") or "").split()},
        )
    except Exception:
        pass

    # 5. Sync ad_level + department (best-effort), then issue the platform JWT
    try:
        from routers.auth_router import _sync_user_from_org_tree
        _sync_user_from_org_tree(db_user["id"], db_user["email"])
        from db.database import SessionLocal as _sl
        from db.models import User as _User
        _db = _sl()
        try:
            _u = _db.query(_User).filter(_User.id == db_user["id"]).first()
            if _u:
                db_user["ad_level"]   = _u.ad_level if _u.ad_level is not None else 6
                db_user["department"] = _u.department or ""
        finally:
            _db.close()
    except Exception as _se:
        logger.debug(f"SSO[office]: org_tree sync failed (non-fatal): {_se}")

    from auth.jwt_handler import encode_token
    _ad_level   = db_user.get("ad_level", 6)
    _department = db_user.get("department", "")
    token = encode_token(
        user_id=db_user["id"], role=db_user["role"], email=db_user["email"],
        ad_level=_ad_level, department=_department,
    )
    logger.info(f"SSO[office]: user '{mask_email(email)}' authenticated via Office SSO (subject={sso_subject})")

    return {
        "access_token": token,
        "token_type":   "bearer",
        "user_id":      db_user["id"],
        "email":        db_user["email"],
        "name":         db_user["name"],
        "role":         db_user["role"],
        "ad_level":     _ad_level,
        "department":   _department,
        "can_approve":  _ad_level <= int(os.getenv("APPROVAL_AD_LEVEL", "6")) or db_user["role"] == "admin",
        "provider":     "azure_ad",
    }

# ============================================================
# DESKTOP SSO  (system-browser + loopback, silent re-login)
# ============================================================

class DesktopExchangeRequest(_BaseModel):
    code: str
    redirect_uri: str


class DesktopRefreshRequest(_BaseModel):
    refresh_token: str


def _refresh_azure_tokens(refresh_token: str) -> dict:
    """Exchange an Entra refresh_token for a fresh token set (silent re-login)."""
    tenant_id     = _sso_tenant_id()
    client_id     = _sso_client_id()
    client_secret = _sso_client_secret()
    token_ep      = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    return _http_post_form(token_ep, {
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "client_id":     client_id,
        "client_secret": client_secret,
        "scope":         "openid email profile offline_access",
    })


def _desktop_identity_from_graph(access_token: str) -> Optional[dict]:
    """Resolve the user from a Graph access token against the allowlist; return
    db_user dict, or None on a transient Graph failure (caller returns 502 — the
    desktop keeps its stored token and retries next launch). This is NOT the
    allowlist-rejection case, which raises HTTPException(403) directly."""
    try:
        graph_me = _http_get_bearer("https://graph.microsoft.com/v1.0/me", access_token)
    except Exception as exc:
        logger.warning(f"SSO[desktop]: Graph /me failed — {exc}")
        return None
    email = graph_me.get("mail") or graph_me.get("userPrincipalName", "")
    if not email:
        return None
    return _find_registered_sso_user(
        sso_subject=graph_me.get("id", ""), sso_provider="azure_ad", email=email,
    )


def _desktop_login_payload(db_user: dict, refresh_token: str) -> dict:
    """Mint the durable CLI API key (Path B) AND a session JWT (Path A).

    Raises 502 if the key cannot be minted — otherwise the desktop would persist
    an empty key and appear "logged in" while every agent call 401s.
    """
    from routers.api_keys_router import mint_api_key_for_user
    import socket as _socket
    label = f"desktop:{_socket.gethostname()[:40]}"
    minted_token = mint_api_key_for_user(db_user["id"], db_user.get("email", ""), label)
    if not minted_token:
        raise HTTPException(status_code=502, detail="api_key_mint_failed")

    # ── Session JWT for the Electron RENDERER (Path A) ────────────────────────
    # The spawned CLI keeps using api_key (no sid/exp -> immune to session
    # eviction and 24h expiry). This JWT exists solely so the React UI's
    # authFetch calls (credentials:'include') work identically to a web login.
    # Best-effort: if it fails, the desktop still gets api_key and behaves
    # exactly as it does today.
    session_jwt = ""
    try:
        from auth.jwt_handler import encode_token
        from auth.session_manager import register_session as _reg_session
        import uuid as _uuid_mod
        import jwt as _jwtlib
        session_id = str(_uuid_mod.uuid4())
        session_jwt = encode_token(
            user_id=db_user["id"], role=db_user.get("role", "user"),
            ad_level=db_user.get("ad_level", 6), session_id=session_id,
        )
        _jti = _jwtlib.decode(session_jwt, options={"verify_signature": False}).get("jti", "")
        _reg_session(user_id=str(db_user["id"]), session_id=session_id,
                     jti=_jti, ip_address="", user_agent="ainxt-desktop")
        # Pre-seed the profile cache so the first UI request is a Redis hit.
        try:
            from auth.dependencies import enrich_user_context as _enrich
            _enrich({"sub": db_user["id"]})
        except Exception:
            pass
        logger.info(
            f"SSO[desktop]: session JWT issued for '{db_user.get('email','')}' (sid={session_id})"
        )
    except Exception as exc:
        logger.warning(f"SSO[desktop]: session JWT issuance failed (non-fatal) — {exc}")

    return {
        "api_key":       minted_token,     # Path B — spawned CLI agent (no expiry)
        "session_jwt":   session_jwt,      # Path A — Electron renderer cookie
        "refresh_token": refresh_token,    # store encrypted for silent re-login
        "user_id":       db_user["id"],
        "email":         db_user.get("email", ""),
        "name":          db_user.get("name", ""),
        "role":          db_user.get("role", "user"),
        "provider":      "azure_ad",
    }


@sso_router.post("/desktop/exchange")
def sso_desktop_exchange(body: DesktopExchangeRequest):
    """Desktop first-login: exchange the auth code (from the system-browser +
    loopback flow) for user identity + an Entra refresh token, mint a long-lived
    CLI API key, and return both so the Electron app can persist them (encrypted
    via safeStorage). No cookie — the desktop uses the API key/refresh token."""
    if get_sso_provider() != "azure_ad":
        raise HTTPException(status_code=400, detail="Azure AD SSO not configured")
    info = exchange_sso_code(code=body.code, redirect_uri=body.redirect_uri)
    if not info:
        raise HTTPException(status_code=401, detail="SSO code exchange failed")
    db_user = _find_registered_sso_user(
        sso_subject=info.get("user_id", ""), sso_provider="azure_ad",
        email=info.get("email", ""),
    )
    logger.info(f"SSO[desktop]: first-login exchange succeeded for '{mask_email(db_user['email'])}'")
    return _desktop_login_payload(db_user, info.get("refresh_token", ""))


@sso_router.post("/desktop/refresh")
def sso_desktop_refresh(body: DesktopRefreshRequest):
    """Desktop silent re-login: swap a stored Entra refresh token for a fresh set,
    re-mint the CLI API key, and return the rotated refresh token. Called on every
    app launch BEFORE showing the window → user never sees a login screen."""
    if get_sso_provider() != "azure_ad":
        raise HTTPException(status_code=400, detail="Azure AD SSO not configured")
    if not body.refresh_token:
        raise HTTPException(status_code=400, detail="Missing refresh_token")
    try:
        tokens = _refresh_azure_tokens(body.refresh_token)
    except Exception as exc:
        # Network/5xx to Microsoft — transient. NOT invalid_grant, so the desktop
        # keeps the stored refresh token and retries next launch.
        raise HTTPException(status_code=502, detail=f"refresh_unavailable: {exc}")
    access_token = tokens.get("access_token", "")
    if not access_token:
        # The refresh token itself was rejected (expired/revoked). This is the ONE
        # case the desktop treats as terminal → clears the token and re-prompts SSO.
        raise HTTPException(status_code=401, detail="invalid_grant")
    db_user = _desktop_identity_from_graph(access_token)
    if not db_user:
        # Token was fine but Graph/DB lookup failed — transient, don't wipe.
        raise HTTPException(status_code=502, detail="identity_unavailable")
    # Azure rotates refresh tokens — persist the new one (fallback to the old).
    new_refresh = tokens.get("refresh_token") or body.refresh_token
    return _desktop_login_payload(db_user, new_refresh)


# ── User upsert helper ────────────────────────────────────────

def _upsert_sso_user(
    sso_subject: str,
    sso_provider: str,
    email: str,
    name: str,
    role: str,
) -> Optional[dict]:
    """Find-or-create a User row matched by sso_subject.
    Falls back to email lookup if sso_subject lookup misses.
    Returns a plain dict {id, email, name, role} or None on DB failure.
    """
    try:
        from db.database import SessionLocal
        from db.models import User
        import uuid as _uuid_mod

        db = SessionLocal()
        try:
            # 1. Look up by sso_subject + provider
            user = (
                db.query(User)
                .filter(
                    User.sso_subject  == sso_subject,
                    User.sso_provider == sso_provider,
                )
                .first()
            )

            # 2. Fall back to email match
            if not user and email:
                user = db.query(User).filter(User.email == email).first()

            if user:
                # Update stale profile fields without downgrading manually-set role
                user.sso_subject  = sso_subject
                user.sso_provider = sso_provider
                if email:
                    user.email = email
                if name:
                    user.name = name
                if not user.role or user.role == "viewer":
                    user.role = role
            else:
                user = User(
                    id=str(_uuid_mod.uuid4()),
                    email=email,
                    name=name,
                    role=role,
                    sso_provider=sso_provider,
                    sso_subject=sso_subject,
                    is_active=True,
                )
                db.add(user)

            db.commit()
            db.refresh(user)
            return {
                "id":    str(user.id),
                "email": user.email,
                "name":  user.name,
                "role":  user.role,
            }
        finally:
            db.close()

    except Exception as exc:
        logger.error(f"SSO: failed to upsert user '{mask_email(email)}' — {exc}")
        return None


# ── Registered-only lookup for DESKTOP SSO (no auto-create) ──────────────────
# Unlike _upsert_sso_user (used by the browser/Office flows), this NEVER creates
# a row. A user must already exist in `users` (provisioned by /auth/register, an
# admin, or the nightly ad_sync job) before desktop SSO can log them in. Only
# sso_subject/sso_provider are updated on an existing row — role/ad_level are
# left entirely to the platform's own onboarding process.
def _find_registered_sso_user(
    sso_subject: str,
    sso_provider: str,
    email: str,
) -> dict:
    """Look up an existing, ACTIVE User by sso_subject then email.

    Raises HTTPException(403, detail="not_registered") if no row exists, or
    HTTPException(403, detail="account_disabled") if the row exists but is
    inactive. Never creates a row. Returns a plain dict
    {id, email, name, role, ad_level, department} on success.
    """
    from db.database import SessionLocal
    from db.models import User

    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.sso_subject == sso_subject, User.sso_provider == sso_provider)
            .first()
        )
        if not user and email:
            user = db.query(User).filter(User.email == email).first()

        if not user:
            logger.warning(f"SSO[desktop]: rejected — '{mask_email(email)}' has no registered AiNxt account")
            raise HTTPException(status_code=403, detail="not_registered")

        if not user.is_active:
            logger.warning(f"SSO[desktop]: rejected — '{mask_email(email)}' account is disabled")
            raise HTTPException(status_code=403, detail="account_disabled")

        # Link the Azure identity to the existing row. Do NOT touch role/ad_level —
        # onboarding/role assignment stays entirely in the platform's own process.
        changed = False
        if user.sso_subject != sso_subject:
            user.sso_subject = sso_subject
            changed = True
        if user.sso_provider != sso_provider:
            user.sso_provider = sso_provider
            changed = True
        if changed:
            db.commit()
            db.refresh(user)

        logger.info(f"SSO[desktop]: allowlist OK — '{mask_email(email)}' (user_id={user.id})")
        return {
            "id":         str(user.id),
            "email":      user.email,
            "name":       user.name,
            "role":       user.role,
            "ad_level":   user.ad_level if user.ad_level is not None else 6,
            "department": getattr(user, "department", "") or "",
        }
    finally:
        db.close()
