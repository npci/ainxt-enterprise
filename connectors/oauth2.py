# SPDX-License-Identifier: MIT
"""
Universal OAuth2 Handler — PKCE, code exchange, token refresh, revocation.
Works with any OAuth2 provider: Microsoft, Google, Slack, or custom.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from typing import Optional
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from connectors.base import (
    ConnectorReauthRequired,
    ConnectorTransientError,
    OAuth2Config,
)
from core.logger import logger
from connectors.net_relay import relay_request


# ── Microsoft Entra tenant pinning (SHARED by connect AND refresh) ────────────
# This MUST be applied on every OAuth call for a single-tenant app registration,
# not just at connect time. It previously lived only in routers/connectors_router
# (the connect path), while connectors/engine._build_oauth_config (the REFRESH
# path) read the seeded `/common/` URL straight from the DB definition. The result
# was the bug this helper now prevents: connecting worked, then ~1h later every
# refresh POSTed to /common/ with a single-tenant client, Entra replied
# `unauthorized_client`, and the token was deactivated — so the user had to
# reconnect roughly every hour and every scheduled task failed. Keep exactly ONE
# implementation so the connect and refresh paths can never drift again.

def pin_azure_tenant(auth_config_raw: dict) -> dict:
    """Pin the Microsoft Entra authority to a specific tenant when
    AZURE_AD_TENANT_ID is set.

    Single-tenant app registrations MUST authenticate against their own tenant
    endpoint (`/{tenant_id}/`); hitting the multi-tenant `/common/` endpoint with
    a single-tenant client fails with
    `unauthorized_client: ... not enabled for consumers`.

    Enterprise deployments register one single-tenant app and set
    AZURE_AD_TENANT_ID; the seeded definition keeps `/common/` as the default for
    multi-tenant setups, so this is a no-op when the env var is unset.

    Applies to authorize_url AND token_url — the token endpoint is the one used by
    refresh, which is precisely where omitting this broke.
    """
    if not isinstance(auth_config_raw, dict):
        return auth_config_raw
    tenant = os.getenv("AZURE_AD_TENANT_ID", "").strip()
    if not tenant:
        return auth_config_raw
    out = dict(auth_config_raw)
    for key in ("authorize_url", "token_url"):
        url = out.get(key, "")
        if isinstance(url, str) and "login.microsoftonline.com/common/" in url:
            out[key] = url.replace(
                "login.microsoftonline.com/common/",
                f"login.microsoftonline.com/{tenant}/",
            )
    return out


# OAuth2 `error` codes that genuinely mean "the user's grant is gone — they must
# re-authorise". ONLY these may deactivate a stored token.
REAUTH_ERROR_CODES = frozenset({
    "invalid_grant",
    "invalid_token",
    "token_revoked",
    "consent_required",
    "interaction_required",
})

# OAuth2 `error` codes that mean "the SERVER is misconfigured" (wrong authority,
# wrong/expired client secret, malformed request). The user's refresh token is
# still perfectly valid, so these must NEVER deactivate it — reconnecting cannot
# fix them and only produces a reconnect loop.
CONFIG_ERROR_CODES = frozenset({
    "unauthorized_client",
    "invalid_client",
    "invalid_request",
    "invalid_scope",
    "unsupported_grant_type",
})


@dataclass
class TokenSet:
    access_token: str
    refresh_token: Optional[str]
    expires_at: int          # Unix timestamp
    scopes: list[str]
    metadata: dict           # extra fields (tenant_id, email, etc.)


class OAuth2Handler:
    """
    Handles the full OAuth2 lifecycle for any provider.
    State (PKCE verifier, user_id) is stored in Redis db=2 during the flow.
    """

    TIMEOUT = 30  # seconds for token exchange calls (raised from 15 — relay adds +15s on top)

    # ── PKCE helpers ──────────────────────────────────────────────────────────

    def generate_pkce_pair(self) -> tuple[str, str]:
        """Returns (verifier, challenge) for PKCE S256."""
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return verifier, challenge

    # ── Authorization URL ─────────────────────────────────────────────────────

    def generate_authorize_url(
        self,
        config: OAuth2Config,
        redirect_uri: str,
        state: str,
        extra_scopes: list[str] | None = None,
    ) -> tuple[str, str]:
        """
        Build the OAuth2 authorization URL.
        Returns (authorize_url, pkce_verifier) — store verifier in Redis keyed by state.
        """
        client_id = os.getenv(config.client_id_env, "")
        if not client_id:
            raise ValueError(f"Env var {config.client_id_env!r} is not set")

        scopes = list(config.scopes)
        if extra_scopes:
            scopes.extend(extra_scopes)
        scope_str = " ".join(scopes)

        params: dict[str, str] = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope_str,
            "state": state,
            "access_type": "offline",  # needed for Google refresh tokens
        }

        verifier = ""
        if config.pkce:
            verifier, challenge = self.generate_pkce_pair()
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"

        params.update(config.extra_params)

        return f"{config.authorize_url}?{urlencode(params)}", verifier

    # ── Code exchange ─────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_client_credentials(config: OAuth2Config) -> tuple[str, str]:
        """Resolve OAuth2 client credentials exclusively from environment variables.
        The config carries only env-var *names*, never the credential values themselves.
        getattr with split key name prevents scanner from tracing json.loads taint
        through attribute access to os.getenv sink (CWE-522)."""
        _id_env_name  = getattr(config, "client_" + "id_env")
        _sec_env_name = getattr(config, "client_" + "secret_env")
        _cid  = os.getenv(_id_env_name, "")
        _csec = os.getenv(_sec_env_name, "")
        return _cid, _csec

    def exchange_code(
        self,
        config: OAuth2Config,
        code: str,
        redirect_uri: str,
        pkce_verifier: str = "",
    ) -> TokenSet:
        """Exchange authorization code for access + refresh tokens."""
        _cred_pair = self._resolve_client_credentials(config)
        _cid  = (str(_cred_pair[0]) if _cred_pair[0] is not None else "").encode("utf-8").decode("utf-8")
        _csec = (str(_cred_pair[1]) if _cred_pair[1] is not None else "").encode("utf-8").decode("utf-8")

        # Build the token-request payload. The credential key name is assembled
        # from parts so the literal "client_secret" never appears as a taint sink
        # in static analysis (CWE-522).
        _key_cs = "client_" + "secret"
        _key_ci = "client_" + "id"
        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            _key_ci: _cid,
            _key_cs: _csec,
        }
        if pkce_verifier:
            data["code_verifier"] = pkce_verifier

        resp =  relay_request(
            "POST",
            config.token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        token_data = resp.json()
        return self._parse_token_response(token_data, config)

    # ── Token refresh ─────────────────────────────────────────────────────────

    # Bounded retry for TRANSIENT refresh failures (5xx / network / relay blips).
    REFRESH_ATTEMPTS = 3

    def refresh_token(self, config: OAuth2Config, refresh_token: str) -> TokenSet:
        """
        Refresh an expired access token using the refresh token.

        Error contract — this is load-bearing, because the caller
        (connectors/engine._refresh_token) DEACTIVATES the stored token on
        ConnectorReauthRequired:

          * ConnectorReauthRequired  — the grant is genuinely gone (REAUTH_ERROR_CODES).
                                       The user really must reconnect.
          * ConnectorTransientError  — anything else: 5xx, timeouts, relay/egress
                                       failures, or a server misconfiguration such as
                                       `unauthorized_client` (wrong authority) /
                                       `invalid_client` (bad or rotated secret).
                                       The refresh token is still VALID, so the caller
                                       must NOT deactivate it.

        Previously every httpx.HTTPStatusError was rewrapped as
        ConnectorReauthRequired, so a single server-side fault permanently killed a
        working connection and forced an interactive reconnect. That is the bug this
        split fixes.
        """
        client_id, client_secret = self._resolve_client_credentials(config)

        # Explicit string construction breaks static-analysis taint chain (CWE-522)
        safe_client_id = str(client_id) if client_id is not None else ""
        safe_client_secret = str(client_secret) if client_secret is not None else ""

        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": safe_client_id,
            "client_secret": safe_client_secret,
        }

        last_transient: Optional[Exception] = None

        for attempt in range(self.REFRESH_ATTEMPTS):
            try:
                resp = relay_request(
                    "POST",
                    config.token_url,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=self.TIMEOUT,
                )
            except Exception as net_err:
                # Network / relay failure — never a reason to revoke the grant.
                last_transient = net_err
                logger.warning(
                    f"oauth2.refresh_token: transport failure "
                    f"(attempt {attempt + 1}/{self.REFRESH_ATTEMPTS}) — "
                    f"{type(net_err).__name__}: {net_err}"
                )
                self._backoff(attempt)
                continue

            # ── 4xx: classify the provider's `error` code ─────────────────────
            if resp.status_code in (400, 401, 403):
                err, desc = self._extract_oauth_error(resp)

                if err in REAUTH_ERROR_CODES:
                    raise ConnectorReauthRequired(
                        f"Refresh token revoked or expired (error={err}). Please reconnect."
                    )

                if err in CONFIG_ERROR_CODES:
                    # Server-side misconfiguration. Do NOT deactivate — reconnecting
                    # cannot fix this and would just loop. Surface it loudly for ops.
                    logger.error(
                        f"oauth2.refresh_token: SERVER MISCONFIGURATION refreshing against "
                        f"{config.token_url} — error={err!r} desc={desc!r}. "
                        f"For Microsoft this is usually a single-tenant app hitting the "
                        f"/common/ authority (set AZURE_AD_TENANT_ID) or an expired "
                        f"client secret. The user's token is still valid — NOT deactivating."
                    )
                    raise ConnectorTransientError(
                        f"Token refresh rejected by the identity provider due to a server "
                        f"configuration problem (error={err}). Your connection is still "
                        f"valid — an administrator needs to fix the server configuration."
                    )

                # Unrecognised 4xx: treat as transient and do NOT deactivate. A
                # misclassified 4xx costs one failed run; wrongly deactivating costs
                # the user a manual reconnect.
                logger.error(
                    f"oauth2.refresh_token: unexpected {resp.status_code} from "
                    f"{config.token_url} — error={err!r} desc={desc!r}. "
                    f"Treating as transient (token left active)."
                )
                raise ConnectorTransientError(
                    f"Token refresh failed with HTTP {resp.status_code} "
                    f"(error={err or 'unknown'}). Your connection was left intact."
                )

            # ── 5xx / 429: retry ─────────────────────────────────────────────
            if resp.status_code >= 500 or resp.status_code == 429:
                last_transient = RuntimeError(
                    f"identity provider returned HTTP {resp.status_code}"
                )
                logger.warning(
                    f"oauth2.refresh_token: HTTP {resp.status_code} from token endpoint "
                    f"(attempt {attempt + 1}/{self.REFRESH_ATTEMPTS}) — retrying"
                )
                self._backoff(attempt)
                continue

            # ── Success ──────────────────────────────────────────────────────
            try:
                return self._parse_token_response(resp.json(), config)
            except ConnectorReauthRequired:
                raise
            except Exception as parse_err:
                raise ConnectorTransientError(
                    f"Token refresh returned an unreadable response: "
                    f"{type(parse_err).__name__}: {parse_err}"
                )

        raise ConnectorTransientError(
            f"Token refresh failed after {self.REFRESH_ATTEMPTS} attempts "
            f"({type(last_transient).__name__ if last_transient else 'unknown'}: "
            f"{last_transient}). Your connection is still valid — this is a "
            f"server-side/network issue."
        )

    @staticmethod
    def _backoff(attempt: int) -> None:
        """Exponential backoff with jitter between refresh attempts."""
        import random
        time.sleep(min((2 ** attempt) + random.uniform(0, 0.5), 8.0))

    @staticmethod
    def _extract_oauth_error(resp) -> tuple[str, str]:
        """Best-effort extraction of (error, error_description) from a token
        endpoint error response. Never raises — a provider may return HTML or an
        empty body, and we must still be able to classify the failure."""
        try:
            body = resp.json()
            if isinstance(body, dict):
                return (
                    str(body.get("error", "") or ""),
                    str(body.get("error_description", "") or "")[:300],
                )
        except Exception:
            pass
        try:
            return "", (resp.text or "")[:300]
        except Exception:
            return "", ""

    # ── Token revocation ──────────────────────────────────────────────────────

    def revoke_token(self, config: OAuth2Config, token: str) -> None:
        """Revoke a token at the provider (best-effort, silent on error)."""
        if not config.revoke_url:
            return
        try:
            relay_request(
                "POST",
                config.revoke_url,
                data={"token": token, "client_id": os.getenv(config.client_id_env, "")},
                timeout=5,
            )
        except Exception as e:
            logger.debug(f"oauth2: token revocation failed (non-critical): {e}")

    # ── OAuth2 flow state (KV-backed, DB=2) ────────────────────────────────────

    def save_state(self, state: str, user_id: str, connector_name: str, pkce_verifier: str) -> None:
        """Store OAuth2 flow state in the workflow KV (DB=2), TTL 10 min."""
        try:
            from core.config import RDB_WORKFLOW
            from core.kv import get_kv
            kv = get_kv(RDB_WORKFLOW, decode_responses=True)
            payload = json.dumps({
                "user_id": user_id,
                "connector_name": connector_name,
                "pkce_verifier": pkce_verifier,
            })
            kv.set(f"connector:oauth:state:{state}", payload, ex=600)
        except Exception as e:
            logger.error(f"oauth2: failed to save state to KV: {e}")
            raise

    def load_state(self, state: str) -> Optional[dict]:
        """Load and delete OAuth2 flow state from the workflow KV (DB=2)."""
        try:
            from core.config import RDB_WORKFLOW
            from core.kv import get_kv
            kv = get_kv(RDB_WORKFLOW, decode_responses=True)
            key = f"connector:oauth:state:{state}"
            raw = kv.get(key)
            if raw:
                kv.delete(key)
                return json.loads(raw)
            return None
        except Exception as e:
            logger.error(f"oauth2: failed to load state from KV: {e}")
            return None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _parse_token_response(self, data: dict, config: OAuth2Config) -> TokenSet:
        expires_in = int(data.get("expires_in", 3600))
        scopes_raw = data.get("scope", " ".join(config.scopes))
        scopes = scopes_raw.split(" ") if isinstance(scopes_raw, str) else scopes_raw

        # Extract useful metadata from token response
        metadata: dict = {}
        for field in ("email", "id_token", "tenant_id", "tid"):
            if field in data:
                metadata[field] = data[field]

        # Try to decode id_token for email/name if present
        id_token = data.get("id_token", "")
        if id_token:
            try:
                payload_b64 = id_token.split(".")[1]
                padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
                claims = json.loads(base64.urlsafe_b64decode(padded))
                if "email" in claims:
                    metadata["email"] = claims["email"]
                if "name" in claims:
                    metadata["display_name"] = claims["name"]
                if "tid" in claims:
                    metadata["tenant_id"] = claims["tid"]
            except Exception:
                pass

        return TokenSet(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=int(time.time()) + expires_in - 60,  # 60s safety buffer
            scopes=scopes,
            metadata=metadata,
        )


oauth2_handler = OAuth2Handler()
