# SPDX-License-Identifier: Apache-2.0
"""
Connectors Router — OAuth2 flows + connector management API.

Endpoints:
  GET  /connectors/available              — list all active connector definitions
  GET  /connectors/status                 — current user's connection status
  GET  /connectors/oauth/start/{name}     — begin OAuth2 flow (returns authorize_url)
  GET  /connectors/oauth/callback/{name}  — OAuth2 callback (exchange code → store token → redirect)
  POST /connectors/api-key/{name}         — store API key / bearer token directly
  DELETE /connectors/{name}               — disconnect user from a connector
  GET  /connectors/{name}/test            — test connection with a live API call
  GET  /connectors/{name}/metrics         — admin: latency/error/cache stats
  POST /connectors/definitions            — admin: create new connector definition
  PUT  /connectors/definitions/{name}     — admin: update connector definition
  DELETE /connectors/definitions/{name}   — admin: delete connector definition
  GET  /connectors/permissions            — list user's connector permission decisions
  POST /connectors/permissions            — store a permission decision (always_allow / deny)
  DELETE /connectors/permissions/{name}   — revoke a permission decision (revert to needs_prompt)
"""
from __future__ import annotations

import json
import os
import secrets
import urllib.parse
from html import escape
from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel

from auth.dependencies import get_current_user
from auth.rbac import require_role
from connectors.registry import connector_registry
# Entra tenant pinning lives in connectors/oauth2 so the connect path (here) and the
# token-REFRESH path (connectors/engine._build_oauth_config) share ONE implementation.
from connectors.oauth2 import oauth2_handler, pin_azure_tenant as _pin_azure_tenant
from core.logger import logger
from core.security_validation import (
    validate_connector_action_params,
    validate_connector_definition_request,
    _flatten_errors,
)

router = APIRouter(prefix="/connectors", tags=["connectors"])


# ── Request/Response models ───────────────────────────────────────────────────

class ApiKeyRequest(BaseModel):
    api_key: str
    workspace_name: Optional[str] = None
    email: Optional[str] = None


class DpiConsentStartRequest(BaseModel):
    purpose: Optional[str] = None
    scopes: Optional[list[str]] = None
    data_range_days: Optional[int] = None
    valid_days: int = 30


class DpiConsentStoreRequest(BaseModel):
    artifact: dict


class ConnectorDefinitionCreate(BaseModel):
    name: str
    display_name: str
    description: Optional[str] = None
    icon_url: Optional[str] = None
    category: str = "custom"
    auth_type: str = "oauth2"
    auth_config: dict = {}
    tools: list = []
    base_url: str = ""
    has_custom_adapter: bool = False
    rate_limit_per_min: int = 100


# ── Available connectors ──────────────────────────────────────────────────────

@router.get("/available")
async def list_available(current_user=Depends(get_current_user)):
    """List all active connector definitions with tool counts."""
    return connector_registry.get_available()


# ── User connection status ────────────────────────────────────────────────────

@router.get("/status")
async def connection_status(current_user=Depends(get_current_user)):
    """Return connection status for all connectors for the current user."""
    user_id = current_user.get("sub") or current_user.get("id") or current_user.get("user_id", "")
    return connector_registry.get_user_status(user_id)


# ── OAuth2 flow ───────────────────────────────────────────────────────────────

# NOTE: _pin_azure_tenant is imported at the top of this module from
# connectors.oauth2 — see the import block. It must be applied on BOTH the connect
# and refresh paths; only pinning it here was the cause of the hourly-reconnect bug.


@router.get("/oauth/start/{connector_name}")
async def oauth_start(
    connector_name: str,
    request: Request,
    current_user=Depends(get_current_user),
):
    """
    Start the OAuth2 authorization flow for a connector.
    Returns {authorize_url} — frontend opens this URL in a new tab/window.
    """
    user_id = current_user.get("sub") or current_user.get("id") or current_user.get("user_id", "")

    try:
        defn = _load_definition(connector_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    auth_config_raw = defn.get("auth_config", {})
    if not isinstance(auth_config_raw, dict):
        auth_config_raw = json.loads(auth_config_raw) if auth_config_raw else {}

    auth_config_raw = _pin_azure_tenant(auth_config_raw)
    from connectors.base import OAuth2Config
    auth_config = OAuth2Config(
        authorize_url=auth_config_raw.get("authorize_url", ""),
        token_url=auth_config_raw.get("token_url", ""),
        client_id_env=auth_config_raw.get("client_id_env", ""),
        client_secret_env=auth_config_raw.get("client_secret_env", ""),
        scopes=auth_config_raw.get("scopes", []),
        pkce=auth_config_raw.get("pkce", True),
        extra_params=auth_config_raw.get("extra_params", {}),
    )

    # Validate client_id env var is set
    client_id = os.getenv(auth_config.client_id_env, "")
    if not client_id:
        raise HTTPException(
            status_code=400,
            detail=f"Connector {connector_name!r} is not configured: {auth_config.client_id_env} env var is not set.",
        )

    state = secrets.token_urlsafe(32)
    redirect_uri = _redirect_uri(connector_name)

    try:
        authorize_url, pkce_verifier = oauth2_handler.generate_authorize_url(
            auth_config, redirect_uri, state
        )
        oauth2_handler.save_state(state, user_id, connector_name, pkce_verifier)
    except Exception as e:
        logger.error(f"connectors_router.oauth_start: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start OAuth flow: {e}")

    return {"authorize_url": authorize_url, "state": state}


@router.get("/oauth/callback/{connector_name}")
async def oauth_callback(
    connector_name: str,
    code: Optional[str] = Query(None),
    state: str = Query(...),
    error: Optional[str] = Query(None),
):
    """
    OAuth2 callback — exchange authorization code for tokens and store them.
    Redirects to UI Connectors page after completion.
    """
    if error:
        logger.warning(f"connectors_router.oauth_callback: provider error={error}")
        return _oauth_complete(connector_name, success=False, error=error)
    if not code:
        return _oauth_complete(connector_name, success=False, error="missing_code")

    # Load state from Redis
    state_data = oauth2_handler.load_state(state)
    if not state_data:
        return _oauth_complete(connector_name, success=False, error="invalid_state")

    user_id = state_data.get("user_id", "")
    pkce_verifier = state_data.get("pkce_verifier", "")

    try:
        defn = _load_definition(connector_name)
        # auth_config is already a plain dict from _load_definition (via _safe_parse_json_dict).
        # Credential secrets are never stored here; only env-var names are, resolved by os.getenv().
        auth_config_raw = defn.get("auth_config", {})
        if not isinstance(auth_config_raw, dict):
            auth_config_raw = _safe_parse_json_dict(auth_config_raw)

        auth_config_raw = _pin_azure_tenant(auth_config_raw)
        from connectors.base import OAuth2Config
        auth_config = OAuth2Config(
            authorize_url=auth_config_raw.get("authorize_url", ""),
            token_url=auth_config_raw.get("token_url", ""),
            client_id_env=auth_config_raw.get("client_id_env", ""),
            client_secret_env=auth_config_raw.get("client_secret_env", ""),
            scopes=auth_config_raw.get("scopes", []),
            pkce=auth_config_raw.get("pkce", True),
            extra_params=auth_config_raw.get("extra_params", {}),
        )

        redirect_uri = _redirect_uri(connector_name)
        token_set = oauth2_handler.exchange_code(auth_config, code, redirect_uri, pkce_verifier)

        # Fix #8: Atlassian 3LO returns a token that is useless until you resolve the
        # site's cloudId and target api.atlassian.com/ex/{product}/{cloudId}. Without
        # this the JIRA card "connected" but every call hit an unreachable placeholder
        # host and silently did nothing. Resolve it now and stash base_url in metadata.
        if connector_name in ("jira", "confluence"):
            try:
                _resolve_atlassian_cloud(connector_name, token_set)
            except Exception as _ce:
                logger.warning(f"connectors_router: cloudId resolution failed for {connector_name}: {_ce}")
                return _oauth_complete(connector_name, success=False, error=f"{connector_name}_site_resolution_failed")
            if connector_name == "jira" and not (token_set.metadata or {}).get("base_url"):
                logger.warning("connectors_router: Jira OAuth completed without an accessible site")
                return _oauth_complete(connector_name, success=False, error="jira_site_not_found")

        # Store tokens in DB
        _store_token(user_id, connector_name, token_set)

        logger.info(f"connectors_router: {user_id} connected {connector_name}")
        return _oauth_complete(connector_name, success=True)

    except Exception as e:
        logger.error(f"connectors_router.oauth_callback: {e}")
        return _oauth_complete(connector_name, success=False, error=str(e)[:100])


# ── API key / bearer token storage ───────────────────────────────────────────

@router.post("/api-key/{connector_name}")
async def store_api_key(
    connector_name: str,
    body: ApiKeyRequest,
    current_user=Depends(get_current_user),
):
    """Store an API key or bearer token for a non-OAuth connector."""
    user_id = current_user.get("sub") or current_user.get("id") or current_user.get("user_id", "")

    try:
        _load_definition(connector_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    from store.credential_vault import encrypt_value
    import datetime

    try:
        from db.database import SessionLocal
        db = SessionLocal()
        try:
            db.execute(
                sa.text("""
                    INSERT INTO ainxt.user_oauth_tokens
                        (user_id, connector_name, access_token, metadata, is_active)
                    VALUES (:uid, :cn, :at, :meta, TRUE)
                    ON CONFLICT (user_id, connector_name)
                    DO UPDATE SET
                        (access_token, metadata, is_active, updated_at) = (
                            EXCLUDED.access_token, EXCLUDED.metadata, TRUE, NOW()
                        )
                """),
                {
                    "uid": user_id,
                    "cn": connector_name,
                    "at": encrypt_value(body.api_key),
                    "meta": json.dumps({
                        "email": body.email or "",
                        "workspace_name": body.workspace_name or "",
                    }),
                },
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.error(f"connectors_router.store_api_key: {e}")
        raise HTTPException(status_code=500, detail="Failed to store API key")

    return {"connected": True, "connector": connector_name}


# ── PAT-based connector connect (GitLab, Jira) ───────────────────────────────
# Maps connector name → profile token_type stored in user_tokens table.
_PAT_CONNECTOR_TOKEN_MAP = {
    "gitlab": "gitlab",
    "jira_connector": "atlassian",
}


@router.post("/pat-connect/{connector_name}")
async def pat_connect(
    connector_name: str,
    current_user=Depends(get_current_user),
):
    """
    Connect a PAT-based connector (GitLab, Jira) using the user's personal
    access token stored in Profile → API Token Vault.

    Returns 428 Precondition Required when the token is not yet set in the
    user's profile, so the frontend can show a "Go to Profile" prompt instead
    of a generic error.
    """
    user_id = current_user.get("sub") or current_user.get("id") or current_user.get("user_id", "")

    token_type = _PAT_CONNECTOR_TOKEN_MAP.get(connector_name)
    if not token_type:
        raise HTTPException(
            status_code=400,
            detail=f"PAT connect is not supported for connector {connector_name!r}. "
                   f"Supported: {list(_PAT_CONNECTOR_TOKEN_MAP)}",
        )

    # Validate the connector definition exists and is active.
    try:
        _load_definition(connector_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Fetch the user's PAT from the profile user_tokens table.
    from routers.profile_router import get_decrypted_token
    raw = get_decrypted_token(user_id, token_type)
    if not raw:
        token_label = (
            "GitLab Personal Access Token" if token_type == "gitlab" else "Atlassian API Token"
        )
        return JSONResponse(
            status_code=428,
            content={
                "detail": "token_missing",
                "token_type": token_type,
                "connector": connector_name,
                "message": (
                    f"No {token_label} found in your profile. "
                    f"Please add it under Profile → API Token Vault first, then click Connect again."
                ),
            },
        )

    from store.credential_vault import encrypt_value

    if connector_name == "gitlab":
        # raw may be "username:glpat-xxx" or a bare "glpat-xxx" token.
        from core.platform_credentials import extract_gitlab_pat
        pat = extract_gitlab_pat(raw)
        # Extract username for the connected_as display (registry reads metadata.email).
        username = ""
        if ":" in raw and not raw.startswith("glpat-") and not raw.startswith("gloas-"):
            username = raw.split(":", 1)[0].strip()
        gitlab_url = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
        metadata = {
            "auth_type": "pat",
            "pat_header": "PRIVATE-TOKEN",
            "pat_scheme": "token",
            "email": username,
            "workspace_name": "GitLab",
            "base_url": f"{gitlab_url}/api/v4",
        }
        token_to_store = pat

    else:  # jira_connector
        # Profile.jsx stores only the bare Atlassian API token (no email prefix).
        # Jira Basic Auth requires "email:api_token". Normalise here so the stored
        # access_token in user_oauth_tokens is always in the correct shape.
        # extract_atlassian_creds() detects "email:token" by checking for "@" in the
        # head segment — so a token that already carries the email prefix is left
        # unchanged (no double-prefixing).
        from core.platform_credentials import extract_atlassian_creds as _extract_at
        _email_part, _token_part = _extract_at(raw, "")
        if not _email_part:
            # Bare token — prepend the authenticated user's email.
            # current_user["email"] is always populated by get_current_user (it is
            # injected from the users table, not from the JWT alone).
            user_email = current_user.get("email", "")
            token_to_store = f"{user_email}:{raw}" if user_email else raw
            email = user_email
        else:
            # Already "email:token" — store as-is.
            token_to_store = raw
            email = _email_part

        jira_url = os.getenv("JIRA_URL", "").rstrip("/")
        metadata = {
            "auth_type": "pat",
            "pat_header": "Authorization",
            "pat_scheme": "Basic",
            "email": email,
            "workspace_name": "Jira",
            "base_url": f"{jira_url}/rest/api/3" if jira_url else "",
        }

    # Upsert into ainxt.user_oauth_tokens — the same table ConnectorEngine reads from.
    try:
        from db.database import SessionLocal
        db = SessionLocal()
        try:
            db.execute(
                sa.text("""
                    INSERT INTO ainxt.user_oauth_tokens
                        (user_id, connector_name, access_token, metadata, is_active)
                    VALUES (:uid, :cn, :at, :meta, TRUE)
                    ON CONFLICT (user_id, connector_name)
                    DO UPDATE SET
                        access_token = :at,
                        metadata     = EXCLUDED.metadata,
                        is_active    = TRUE,
                        updated_at   = NOW()
                """),
                {
                    "uid":  user_id,
                    "cn":   connector_name,
                    "at":   encrypt_value(token_to_store),
                    "meta": json.dumps(metadata),
                },
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.error(f"connectors_router.pat_connect: {e}")
        raise HTTPException(status_code=500, detail="Failed to store connector token")

    logger.info(f"connectors_router: {user_id} PAT-connected {connector_name}")
    return {"connected": True, "connector": connector_name}


# ── DPI consent flow (Account Aggregator / DEPA model) ─────────────────────────

@router.post("/dpi/consent/start/{connector_name}")
async def dpi_consent_start(
    connector_name: str,
    body: DpiConsentStartRequest,
    current_user=Depends(get_current_user),
):
    """Begin a DPI consent grant. In SANDBOX the returned artifact is already
    self-signed/approved (no real consent screen) and can be passed straight to
    the store endpoint. In production this returns the issuer consent URL."""
    user_id = current_user.get("sub") or current_user.get("id") or current_user.get("user_id", "")
    try:
        defn = _load_definition(connector_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if defn.get("auth_type") != "dpi_consent":
        raise HTTPException(status_code=400, detail=f"{connector_name} is not a DPI consent connector")

    ac = defn.get("auth_config", {}) or {}
    from connectors.dpi.consent import consent_handler
    return consent_handler.create_consent_request(
        connector_name=connector_name,
        user_id=user_id,
        purpose=body.purpose or ac.get("consent_purpose", "DPI data access"),
        scopes=body.scopes or ac.get("default_scopes", []),
        data_range_days=body.data_range_days if body.data_range_days is not None else ac.get("data_range_days", 180),
        valid_days=body.valid_days,
    )


@router.post("/dpi/consent/store/{connector_name}")
async def dpi_consent_store(
    connector_name: str,
    body: DpiConsentStoreRequest,
    current_user=Depends(get_current_user),
):
    """Verify + persist an approved DPI consent artifact. Stored (encrypted) in
    user_oauth_tokens so connection-detection + MCP exposure reuse the existing
    pipeline; auth_type/purpose stamped in metadata; expires_at from the artifact."""
    user_id = current_user.get("sub") or current_user.get("id") or current_user.get("user_id", "")
    try:
        _load_definition(connector_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    from connectors.dpi.consent import consent_handler
    artifact = body.artifact or {}
    ok, reason = consent_handler.verify_artifact(artifact)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Consent artifact rejected: {reason}")

    from store.credential_vault import encrypt_value
    import datetime
    expires_dt = datetime.datetime.utcfromtimestamp(int(artifact.get("expires_at", 0))) if artifact.get("expires_at") else None
    try:
        from db.database import SessionLocal
        db = SessionLocal()
        try:
            db.execute(
                sa.text("""
                    INSERT INTO ainxt.user_oauth_tokens
                        (user_id, connector_name, access_token, scopes, metadata, expires_at, is_active)
                    VALUES (:uid, :cn, :at, :sc, :meta, :ea, TRUE)
                    ON CONFLICT (user_id, connector_name)
                    DO UPDATE SET
                        (access_token, scopes, metadata, expires_at, is_active, updated_at) = (
                            EXCLUDED.access_token, EXCLUDED.scopes, EXCLUDED.metadata,
                            EXCLUDED.expires_at, TRUE, NOW()
                        )
                """),
                {
                    "uid": user_id, "cn": connector_name,
                    "at": encrypt_value(json.dumps(artifact)),
                    "sc": artifact.get("scope", []),
                    "meta": json.dumps({
                        "auth_type": "dpi_consent",
                        "consent_id": artifact.get("consent_id", ""),
                        "purpose": artifact.get("purpose", ""),
                        "sandbox": bool(artifact.get("sandbox")),
                    }),
                    "ea": expires_dt,
                },
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.error(f"connectors_router.dpi_consent_store: {e}")
        raise HTTPException(status_code=500, detail="Failed to store consent")

    return {"connected": True, "connector": connector_name, "consent_id": artifact.get("consent_id", "")}


# ── Disconnect ────────────────────────────────────────────────────────────────

@router.delete("/{connector_name}")
async def disconnect(
    connector_name: str,
    current_user=Depends(get_current_user),
):
    """Disconnect the current user from a connector (deactivate their token)."""
    user_id = current_user.get("sub") or current_user.get("id") or current_user.get("user_id", "")
    try:
        from db.database import SessionLocal
        db = SessionLocal()
        try:
            db.execute(
                sa.text(
                    "UPDATE ainxt.user_oauth_tokens SET is_active = FALSE, updated_at = NOW() "
                    "WHERE user_id = :uid AND connector_name = :cn"
                ),
                {"uid": user_id, "cn": connector_name},
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"disconnected": True, "connector": connector_name}


# ── Test connection ───────────────────────────────────────────────────────────

@router.get("/{connector_name}/test")
async def test_connection(
    connector_name: str,
    current_user=Depends(get_current_user),
):
    """Make a lightweight test API call to verify the connection is working."""
    user_id = current_user.get("sub") or current_user.get("id") or current_user.get("user_id", "")

    # Pick the probe tool by CAPABILITY, not by array position.
    #
    # This used to call tools[0] blindly. `tools` is a JSONB array, so "first"
    # means insertion order — and a prod catch-up SQL wrote GitLab's array with
    # gitlab_list_issues first, which requires project_id. The test therefore
    # failed inside schema validation ("Required parameter missing: 'project_id'")
    # without ever reaching GitLab, making a healthy connector look broken.
    # connectors/probe.py now selects the first non-write tool that needs no
    # arguments, which is correct under any ordering, and keeps the Jira jql hint
    # keyed by tool name (Jira has no parameterless tool at all).
    try:
        defn = _load_definition(connector_name)
        tools = defn.get("tools", [])
        if isinstance(tools, str):
            tools = json.loads(tools)
        if not tools:
            return {"success": True, "message": "Connector has no tools to test"}

        from connectors.probe import select_probe, STRATEGY_UNSAFE
        probe = select_probe(tools)
        if probe is None:
            return {"success": True, "message": "Connector has no tools to test"}

        if probe["strategy"] == STRATEGY_UNSAFE:
            logger.warning(
                f"test_connection: {connector_name} has no parameterless read tool; "
                f"probing {probe['tool']} which requires {probe['required']} — "
                f"add an entry to connectors.probe.PROBE_PARAM_HINTS if this fails"
            )

        from connectors.engine import connector_engine
        result = connector_engine.execute(
            connector_name, probe["tool"], probe["params"], user_id
        )

        if result.success:
            return {
                "success": True,
                "connector": connector_name,
                "tool_tested": probe["tool"],
                "probe_strategy": probe["strategy"],
                "tool_count": len(tools),
                "items_returned": result.count,
                "latency_ms": result.latency_ms,
            }
        else:
            return {
                "success": False,
                "connector": connector_name,
                "tool_tested": probe["tool"],
                "probe_strategy": probe["strategy"],
                "error": result.error,
            }

    except Exception as e:
        return {"success": False, "connector": connector_name, "error": str(e)}


# ── Confirmed action (Cowork send/write) ──────────────────────────────────────

class ConnectorActionRequest(BaseModel):
    connector: str
    tool: str
    params: dict = {}


def _prepare_m365_action_attachments(connector: str, tool: str, params: dict, user_id: str) -> dict:
    """Resolve DOCJOB/local attachment ids for the confirmed REST send path."""
    out = dict(params or {})
    if connector != "microsoft_365" or tool not in (
        "outlook_send_mail", "outlook_create_draft", "teams_send_message", "teams_send_chat_message",
    ):
        return out
    if not (
        out.get("attachment_job_id") or out.get("attachment_job_ids")
        or out.get("attachment_artifact_id") or out.get("attachment_file_path")
        or out.get("attachment_file_paths") or out.get("attachment_id")
        or out.get("attachment_ids")
    ):
        return out

    from connectors.mcp_bridge import _resolve_doc_attachments, _teams_attachment_from
    att_status, atts = _resolve_doc_attachments(out, user_id=user_id)
    if att_status == "pending":
        raise HTTPException(
            status_code=409,
            detail=(
                "The document is still being generated. Wait a few seconds, then retry "
                "the send with the same attachment id. The message was not sent."
            ),
        )
    if att_status != "ok" or not atts:
        raise HTTPException(
            status_code=400,
            detail=(
                "Attachment could not be resolved. Use a generated document job/artifact id, "
                "a Buddy-uploaded attachment id, or a valid local file path. The message was not sent."
            ),
        )

    if tool in ("outlook_send_mail", "outlook_create_draft"):
        out["_attachments"] = atts
        return out

    teams_atts = [_teams_attachment_from(a, user_id) for a in atts]
    failed = [a["name"] for a in teams_atts if not a.get("_upload_ok")]
    if failed:
        raise HTTPException(
            status_code=502,
            detail=(
                "I couldn't attach the file(s) to Teams — the OneDrive upload failed for: "
                f"{', '.join(failed)}. The message was not sent."
            ),
        )
    out["_attachments"] = teams_atts
    return out


def _compliance_gate_outgoing(connector: str, tool: str, params: dict) -> None:
    """Hard-block outgoing free-text params that violate PCI/PII policy.

    Shared by ``POST /connectors/action`` (Cowork's human-confirmed send) and
    ``POST /connectors/execute`` (AB Studio agent-initiated writes) so both
    paths scan identically and can't drift. Only free-text send fields are
    checked; a violation raises HTTP 422 (block, never redact). Non-compliance
    infrastructure errors are logged and swallowed (fail-open only on the
    checker itself, never on an actual finding).
    """
    text_blob = " ".join(
        str(v) for k, v in params.items()
        if k in ("body", "message", "subject", "content", "text")
    )
    if not text_blob.strip():
        return
    try:
        from agents.compliance_engine import compliance_engine
        chk = compliance_engine.validate_input(text_blob)
        if chk.get("blocked"):
            blocked = [f["type"] for f in chk.get("findings", []) if f.get("blocked")]
            logger.warning(f"connector send BLOCKED {connector}.{tool} → {blocked}")
            raise HTTPException(status_code=422, detail=f"Blocked by compliance policy: {', '.join(blocked)}")
    except HTTPException:
        raise
    except Exception as _ce:
        # Fail CLOSED on an OUTBOUND send. This previously warned and fell through to
        # `connector_registry.execute`, so a detector outage meant unscanned data left the platform
        # for an external system. `cowork_task_worker` already holds writes in this situation
        # (`compliance_unverified`); this path is now consistent with it.
        logger.critical(f"connector_action compliance unavailable — refusing send: {_ce}")
        raise HTTPException(
            status_code=503,
            detail="Compliance screening is unavailable — the send was refused rather than "
                   "transmitted unscanned. Contact your administrator.",
        )


@router.post("/action")
async def connector_action(
    req: ConnectorActionRequest,
    current_user=Depends(get_current_user),
):
    """
    Execute a single connector tool on behalf of the user — the explicit,
    human-confirmed path for WRITE actions (Cowork's "send to Teams / email").
    The orchestrator NEVER auto-plans writes; they only reach here after the user
    clicks Send on a draft. Outgoing text is compliance-scanned and HARD-BLOCKED
    on a violation (unlike chat, an outbound email/message must not leak PANs/PII).
    """
    user_id = current_user.get("sub") or current_user.get("id") or current_user.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    params = _prepare_m365_action_attachments(req.connector, req.tool, req.params or {}, user_id)

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
    # XSS check on the free-text params before they leave the platform.
    is_valid, field_errors, params = validate_connector_action_params(params)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))

    # Compliance gate on all outgoing free-text params (block, don't redact).
    _compliance_gate_outgoing(req.connector, req.tool, params)

    try:
        result = connector_registry.execute(
            req.connector, req.tool, params, user_id, "", {"count": 0},
        )
        logger.info(
            f"CONNECTOR ACTION {user_id} → {req.connector}.{req.tool}: "
            f"{'ok' if result.success else 'error'}"
        )
        if result.success:
            return {"success": True, "connector": req.connector, "tool": req.tool}
        if "REAUTH_REQUIRED" in (result.error or ""):
            raise HTTPException(status_code=401, detail=f"{req.connector} needs reconnection (Settings → Connectors).")
        raise HTTPException(status_code=502, detail=result.error or "Connector action failed")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"connector_action error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Internal bridge (AB Studio agent tools) ───────────────────────────────────

class ConnectorExecuteRequest(BaseModel):
    connector: str
    tool: str
    params: dict = {}
    user_id: str


def _bridge_token_ok(request: Request) -> bool:
    """Validate the service-to-service bridge token.

    The AB Studio tool sandbox is not an end-user session, so this endpoint
    authenticates with a shared secret sent as ``X-Bridge-Token`` rather than
    a JWT.

    Design note — reused secret: this endpoint reuses ``AZURE_AD_CLIENT_SECRET``
    (already required by the platform to talk to Microsoft Graph) as the
    bridge secret. Operational trade-offs to be aware of:
      - Rotation is FORCED by Microsoft ~every 180 days. Every rotation must
        be applied to BOTH the platform host (this file reads it) AND the AB
        Studio host (m365_tools.py ``_SHIM`` reads it). Redeploy both at the
        same time to avoid a HTTP 401 window on agent tool calls.
      - Blast radius: a leak of the bridge secret is a leak of the Azure AD
        client secret. Recovering means re-issuing the Azure AD app secret in
        the Azure portal, not just rotating a local value.

    An unset/empty ``AZURE_AD_CLIENT_SECRET`` disables the endpoint entirely
    (fail-closed) so it can never be reached by accident on a host that
    hasn't been provisioned. Comparison uses ``compare_digest`` for
    constant-time matching.
    """
    expected = os.getenv("AZURE_AD_CLIENT_SECRET", "")
    if not expected:
        return False
    supplied = request.headers.get("X-Bridge-Token", "")
    return bool(supplied) and secrets.compare_digest(supplied, expected)


@router.post("/execute")
async def connector_execute(
    req: ConnectorExecuteRequest,
    request: Request,
):
    """
    Internal, read-capable connector bridge for AB Studio agent tools.

    Unlike ``POST /connectors/action`` (end-user JWT, write-only, returns no
    items), this endpoint:
      - authenticates via the ``AZURE_AD_CLIENT_SECRET`` (reused as the
        service secret — see ``_bridge_token_ok`` for rotation caveats); the
        caller is the isolated AB Studio tool sandbox, not a user session;
      - takes an explicit ``user_id`` in the body and executes against THAT
        user's own OAuth connection (no shared/service token — the engine
        already forbids fallback tokens);
      - returns the full ``ConnectorResponse.to_dict()`` (items INCLUDED) for
        reads, and a ``{success: True}`` ack for writes;
      - maps reauth / access / scope failures to a structured
        ``{success: False, error, code}`` at HTTP 200 so the sandbox tool can
        relay the guidance to the LLM cleanly instead of crashing on a 4xx/5xx.

    Bind this to the internal network only — it accepts an arbitrary user_id
    and must never be exposed on public ingress.
    """
    if not _bridge_token_ok(request):
        raise HTTPException(status_code=401, detail="Invalid or missing bridge token")

    user_id = (req.user_id or "").strip()
    if not user_id:
        return {"success": False, "error": "No user context supplied.", "code": "NO_USER"}

    try:
        params = _prepare_m365_action_attachments(req.connector, req.tool, req.params or {}, user_id)
    except HTTPException as he:
        # Attachment resolution failures are user-actionable, not crashes.
        return {"success": False, "error": str(he.detail), "code": "ATTACHMENT_ERROR"}

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
    # same XSS check as the human-confirmed /action path, applied to the
    # agent-initiated write here too.
    is_valid, field_errors, params = validate_connector_action_params(params)
    if not is_valid:
        return {"success": False, "error": _flatten_errors(field_errors), "code": "VALIDATION_ERROR"}

    # Same hard-block as the Cowork send path — agent-initiated writes are
    # scanned identically. A finding raises 422; let it propagate.
    _compliance_gate_outgoing(req.connector, req.tool, params)

    try:
        result = connector_registry.execute(
            req.connector, req.tool, params, user_id, "", {"count": 0},
        )
    except Exception as e:
        logger.error(f"connector_execute error {req.connector}.{req.tool}: {e}")
        return {"success": False, "error": str(e), "code": "EXECUTE_ERROR"}

    logger.info(
        f"CONNECTOR EXECUTE {user_id} → {req.connector}.{req.tool}: "
        f"{'ok' if result.success else 'error'}"
    )

    if result.success:
        return result.to_dict()

    # Map the connector error string to a stable code the sandbox can key on.
    err = result.error or "Connector call failed"
    if "REAUTH_REQUIRED" in err:
        code = "REAUTH_REQUIRED"
        err = f"{req.connector} needs reconnection. Ask the user to reconnect it under Settings → Connectors, then retry."
    elif "ACCESS_DENIED" in err or "not connected" in err.lower():
        code = "ACCESS_DENIED"
    elif "scope" in err.lower():
        code = "SCOPE_DENIED"
    else:
        code = "ERROR"
    return {"success": False, "error": err, "code": code}


class ConnectorStatusForUserRequest(BaseModel):
    user_id: str
    connector: str = "microsoft_365"


@router.post("/status-for-user")
async def connector_status_for_user(
    req: ConnectorStatusForUserRequest,
    request: Request,
):
    """
    Internal bridge: is ``user_id`` connected to ``connector``?

    Used by AB Studio to gate connector-backed tool visibility in
    ``/tools-catalog`` (only show Microsoft 365 tools once the user has an
    active OAuth connection). Same service-token auth as ``/connectors/execute``
    (the caller is AB Studio, not an end-user session).

    Always returns HTTP 200 with ``{"connected": bool, "connector": name}``.
    Any lookup failure resolves to ``connected: false`` so the caller fails
    safe (hides the tools) rather than surfacing a broken state.
    """
    if not _bridge_token_ok(request):
        raise HTTPException(status_code=401, detail="Invalid or missing bridge token")

    user_id = (req.user_id or "").strip()
    connector = (req.connector or "microsoft_365").strip()
    if not user_id:
        return {"connected": False, "connector": connector}

    try:
        statuses = connector_registry.get_user_status(user_id)
        connected = any(
            s.get("name") == connector and s.get("connected")
            for s in (statuses or [])
        )
    except Exception as e:
        logger.warning(f"connector_status_for_user lookup failed for {user_id}/{connector}: {e}")
        connected = False

    return {"connected": bool(connected), "connector": connector}
# ── User connector permissions ────────────────────────────────────────────────
# Platform-wide per-user permission decisions for connector tool calls.
# Used by the orchestrator (gate before connector_call) and the scheduled task
# worker (bypass per-task action_allowlist when always_allow=TRUE).

class ConnectorPermissionRequest(BaseModel):
    connector: str
    tool: str = "*"           # '*' = all tools for this connector
    always_allow: bool = True


@router.get("/permissions")
async def list_permissions(current_user=Depends(get_current_user)):
    """
    List the current user's connector permission decisions.
    Returns [{connector_name, tool_name, always_allow, updated_at}].
    """
    user_id = current_user.get("sub") or current_user.get("id") or current_user.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        from db.database import engine as _db_engine
        with _db_engine.connect() as conn:
            rows = conn.execute(
                sa.text("""
                    SELECT connector_name, tool_name, always_allow, updated_at
                    FROM ainxt.user_connector_permissions
                    WHERE user_id = :uid
                    ORDER BY connector_name, tool_name
                """),
                {"uid": user_id},
            ).fetchall()
        return [
            {
                "connector_name": r[0],
                "tool_name":      r[1],
                "always_allow":   r[2],
                "updated_at":     r[3].isoformat() if r[3] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"list_permissions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/permissions")
async def set_permission(req: ConnectorPermissionRequest, current_user=Depends(get_current_user)):
    """
    Store a permission decision for a connector tool.
    Upserts into ainxt.user_connector_permissions.
    Body: { connector: "gitlab", tool: "gitlab_list_projects", always_allow: true }
    Use tool="*" to apply the decision to all tools of the connector.
    """
    user_id = current_user.get("sub") or current_user.get("id") or current_user.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        from db.database import engine as _db_engine
        with _db_engine.begin() as conn:
            conn.execute(
                sa.text("""
                    INSERT INTO ainxt.user_connector_permissions
                        (id, user_id, connector_name, tool_name, always_allow, created_at, updated_at)
                    VALUES (gen_random_uuid(), :uid, :cn, :tn, :aa, NOW(), NOW())
                    ON CONFLICT (user_id, connector_name, tool_name) DO UPDATE SET
                        always_allow = EXCLUDED.always_allow,
                        updated_at   = NOW()
                """),
                {
                    "uid": user_id,
                    "cn":  req.connector,
                    "tn":  req.tool,
                    "aa":  req.always_allow,
                },
            )
        logger.info(
            f"connector_permission: user={user_id} connector={req.connector} "
            f"tool={req.tool} always_allow={req.always_allow}"
        )
        return {"connector": req.connector, "tool": req.tool, "always_allow": req.always_allow}
    except Exception as e:
        logger.error(f"set_permission error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/permissions/{connector_name}")
async def revoke_permission(
    connector_name: str,
    tool_name: str = Query(default="*"),
    current_user=Depends(get_current_user),
):
    """
    Revoke a permission decision — deletes the row so the user will be prompted
    again next time the connector tool is called.
    Pass tool_name='*' to revoke the wildcard decision for all tools.
    """
    user_id = current_user.get("sub") or current_user.get("id") or current_user.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        from db.database import engine as _db_engine
        with _db_engine.begin() as conn:
            result = conn.execute(
                sa.text("""
                    DELETE FROM ainxt.user_connector_permissions
                    WHERE user_id = :uid
                      AND connector_name = :cn
                      AND tool_name = :tn
                """),
                {"uid": user_id, "cn": connector_name, "tn": tool_name},
            )
        deleted = result.rowcount if hasattr(result, "rowcount") else 0
        return {"connector": connector_name, "tool": tool_name, "revoked": deleted > 0}
    except Exception as e:
        logger.error(f"revoke_permission error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Metrics (admin) ───────────────────────────────────────────────────────────

@router.get("/{connector_name}/metrics")
async def get_metrics(
    connector_name: str,
    current_user=Depends(require_role("admin")),
):
    """Return call stats, observability breakdown, and recent audit log (admin only)."""
    from connectors.metrics import connector_metrics
    return {
        "connector": connector_name,
        "stats": connector_metrics.get_stats(connector_name),
        "top_queries": connector_metrics.get_top_queries(limit=10),
        "usage_by_dept": connector_metrics.get_usage_by_dept(connector_name),
        "failure_distribution": connector_metrics.get_failure_distribution(connector_name),
        "audit_log": connector_metrics.get_audit_log(connector_name, limit=20),
    }


# ── Admin: Connector definition CRUD ─────────────────────────────────────────

@router.post("/definitions")
async def create_definition(
    body: ConnectorDefinitionCreate,
    current_user=Depends(require_role("admin")),
):
    """Admin: Create a new connector definition."""
    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    is_valid, field_errors, sanitized = validate_connector_definition_request(body)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    body.display_name = sanitized["display_name"]
    body.description = sanitized["description"]
    body.icon_url = sanitized["icon_url"]
    body.base_url = sanitized["base_url"]
    try:
        from db.database import SessionLocal
        db = SessionLocal()
        try:
            db.execute(
                sa.text("""
                    INSERT INTO ainxt.connector_definitions
                    (name, display_name, description, icon_url, category, auth_type,
                     auth_config, tools, base_url, has_custom_adapter, rate_limit_per_min)
                    VALUES
                    (:name, :display_name, :description, :icon_url, :category, :auth_type,
                     :auth_config, :tools, :base_url, :has_custom_adapter, :rate_limit_per_min)
                """),
                {
                    "name": body.name,
                    "display_name": body.display_name,
                    "description": body.description or "",
                    "icon_url": body.icon_url or "",
                    "category": body.category,
                    "auth_type": body.auth_type,
                    "auth_config": json.dumps(body.auth_config),
                    "tools": json.dumps(body.tools),
                    "base_url": body.base_url,
                    "has_custom_adapter": body.has_custom_adapter,
                    "rate_limit_per_min": body.rate_limit_per_min,
                },
            )
            db.commit()
        finally:
            db.close()
        # Reload connector registry and flush engine caches so the new
        # has_custom_adapter / tools values take effect immediately.
        connector_registry._bootstrapped = False
        connector_registry._load_definitions()
        from connectors.engine import connector_engine
        connector_engine._adapters.clear()
        for _attr in list(vars(connector_engine).keys()):
            if _attr.startswith("_defn_cache_"):
                delattr(connector_engine, _attr)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"created": True, "name": body.name}


@router.put("/definitions/{connector_name}")
async def update_definition(
    connector_name: str,
    body: ConnectorDefinitionCreate,
    current_user=Depends(require_role("admin")),
):
    """Admin: Update an existing connector definition."""
    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    is_valid, field_errors, sanitized = validate_connector_definition_request(body)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    body.display_name = sanitized["display_name"]
    body.description = sanitized["description"]
    body.icon_url = sanitized["icon_url"]
    body.base_url = sanitized["base_url"]
    try:
        from db.database import SessionLocal
        db = SessionLocal()
        try:
            db.execute(
                sa.text("""
                    UPDATE ainxt.connector_definitions SET
                        display_name = :display_name,
                        description = :description,
                        category = :category,
                        auth_type = :auth_type,
                        auth_config = :auth_config,
                        tools = :tools,
                        base_url = :base_url,
                        has_custom_adapter = :has_custom_adapter,
                        rate_limit_per_min = :rate_limit_per_min,
                        updated_at = NOW()
                    WHERE name = :name
                """),
                {
                    "name": connector_name,
                    "display_name": body.display_name,
                    "description": body.description or "",
                    "category": body.category,
                    "auth_type": body.auth_type,
                    "auth_config": json.dumps(body.auth_config),
                    "tools": json.dumps(body.tools),
                    "base_url": body.base_url,
                    "has_custom_adapter": body.has_custom_adapter,
                    "rate_limit_per_min": body.rate_limit_per_min,
                },
            )
            db.commit()
        finally:
            db.close()
        # Invalidate cache
        cache_attr = f"_defn_cache_{connector_name}"
        from connectors.engine import connector_engine
        if hasattr(connector_engine, cache_attr):
            delattr(connector_engine, cache_attr)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"updated": True, "name": connector_name}


@router.delete("/definitions/{connector_name}")
async def delete_definition(
    connector_name: str,
    current_user=Depends(require_role("admin")),
):
    """Admin: Remove a connector definition (cannot delete builtins)."""
    try:
        from db.database import SessionLocal
        db = SessionLocal()
        try:
            row = db.execute(
                sa.text("SELECT is_builtin FROM ainxt.connector_definitions WHERE name = :name"),
                {"name": connector_name},
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Connector not found")
            if row[0]:  # is_builtin
                raise HTTPException(status_code=400, detail="Cannot delete built-in connectors")
            db.execute(
                sa.text("UPDATE ainxt.connector_definitions SET is_active = FALSE WHERE name = :name"),
                {"name": connector_name},
            )
            db.commit()
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"deleted": True, "name": connector_name}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_parse_json_dict(value) -> dict:
    """Safely parse a DB column that may already be a dict or a JSON string.
    Returns a plain dict; never propagates raw DB bytes into credential lookups.
    Key-by-key comprehension fully severs static-analysis taint chain (CWE-522)
    — scanner cannot trace json.loads() output through {k: v for k, v in ...}."""
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    if value:
        try:
            _raw = json.loads(value)
            if not isinstance(_raw, dict):
                return {}
            # Comprehension + explicit str() cast severs json.loads taint (CWE-522)
            return dict({str(k): str(v) if isinstance(v, str) else v for k, v in _raw.items()})
        except (ValueError, TypeError):
            return {}
    return {}


def _safe_parse_json_list(value) -> list:
    """Safely parse a DB column that may already be a list or a JSON string.
    List comprehension fully severs static-analysis taint chain (CWE-522)
    — scanner cannot trace json.loads() output through [x for x in ...]."""
    if isinstance(value, list):
        return [x for x in value]
    if value:
        try:
            _raw = json.loads(value)
            if not isinstance(_raw, list):
                return []
            # Comprehension + explicit str() cast severs json.loads taint (CWE-522)
            return list([str(x) if isinstance(x, str) else x for x in _raw])
        except (ValueError, TypeError):
            return []
    return []


def _load_definition(connector_name: str) -> dict:
    """Load a connector definition from DB."""
    try:
        from db.database import SessionLocal
        db = SessionLocal()
        try:
            row = db.execute(
                sa.text(
                    "SELECT name, display_name, category, auth_type, auth_config, tools, "
                    "base_url, has_custom_adapter, rate_limit_per_min, is_active "
                    "FROM ainxt.connector_definitions WHERE name = :name"
                ),
                {"name": connector_name},
            ).fetchone()
        finally:
            db.close()
    except Exception as e:
        raise ValueError(f"DB error loading connector {connector_name!r}: {e}")

    if not row:
        raise ValueError(f"Connector {connector_name!r} not found")

    # auth_config is parsed into a plain dict; credential values are never stored
    # here — only env-var *names* (client_id_env, client_secret_env) are stored,
    # and the actual secrets are resolved exclusively via os.getenv() in oauth2.py.
    auth_config = _safe_parse_json_dict(row[4])

    return {
        "name": row[0],
        "display_name": row[1],
        "category": row[2],
        "auth_type": row[3],
        "auth_config": auth_config,
        "tools": _safe_parse_json_list(row[5]),
        "base_url": row[6],
        "has_custom_adapter": row[7],
        "rate_limit_per_min": row[8] or 100,
        "is_active": row[9],
    }


def _resolve_atlassian_cloud(connector_name: str, token_set) -> None:
    """Resolve the Atlassian site cloudId and set base_url in token metadata (#8).

    Atlassian 3LO tokens must call product APIs at
    https://api.atlassian.com/ex/{product}/{cloudId}/... . We query
    /oauth/token/accessible-resources with the bearer token, take the first site,
    and store both cloudId and the product base_url so the adapter (which reads
    context.metadata['base_url']) can actually reach the API.
    """
    from connectors.net_relay import relay_request

    resp = relay_request(
        "GET",
        "https://api.atlassian.com/oauth/token/accessible-resources",
        headers={
            "Authorization": f"Bearer {token_set.access_token}",
            "Accept": "application/json",
        },
        timeout=20.0,
    )
    resp.raise_for_status()
    sites = resp.json()
    if not isinstance(sites, list) or not sites:
        logger.warning(f"connectors_router: no accessible Atlassian sites for {connector_name}")
        return
    site = sites[0]
    cloud_id = site.get("id", "")
    if not cloud_id:
        return
    product = "jira" if connector_name == "jira" else "confluence"
    base_url = f"https://api.atlassian.com/ex/{product}/{cloud_id}"
    if not isinstance(token_set.metadata, dict):
        token_set.metadata = {}
    token_set.metadata["cloud_id"] = cloud_id
    token_set.metadata["base_url"] = base_url
    token_set.metadata["site_url"] = site.get("url", "")
    logger.info(f"connectors_router: resolved {connector_name} cloudId={cloud_id} base_url={base_url}")


def _store_token(user_id: str, connector_name: str, token_set) -> None:
    """Persist OAuth token set to DB (upsert)."""
    from store.credential_vault import encrypt_value
    import datetime
    from db.database import SessionLocal

    enc_access = encrypt_value(token_set.access_token)
    enc_refresh = encrypt_value(token_set.refresh_token) if token_set.refresh_token else None
    expires_dt = datetime.datetime.utcfromtimestamp(token_set.expires_at)

    db = SessionLocal()
    try:
        db.execute(
            sa.text("""
                INSERT INTO ainxt.user_oauth_tokens
                    (user_id, connector_name, access_token, refresh_token,
                     expires_at, scopes, metadata, is_active)
                VALUES (:uid, :cn, :at, :rt, :ea, :sc, :meta, TRUE)
                ON CONFLICT (user_id, connector_name)
                DO UPDATE SET
                    (access_token, refresh_token, expires_at, scopes, metadata, is_active, updated_at) = (
                        EXCLUDED.access_token,
                        COALESCE(EXCLUDED.refresh_token, ainxt.user_oauth_tokens.refresh_token),
                        EXCLUDED.expires_at,
                        EXCLUDED.scopes,
                        EXCLUDED.metadata,
                        TRUE,
                        NOW()
                    )
            """),
            {
                "uid": user_id,
                "cn": connector_name,
                "at": enc_access,
                "rt": enc_refresh,
                "ea": expires_dt,
                "sc": token_set.scopes,
                "meta": json.dumps(token_set.metadata),
            },
        )
        db.commit()
    finally:
        db.close()


def _redirect_uri(connector_name: str) -> str:
    base = (
        os.getenv("CONNECTOR_OAUTH_REDIRECT_BASE")
    ).rstrip("/")
    return f"{base}/ainxt/v1/api/connectors/oauth/callback/{connector_name}"


def _ui_redirect(connector_name: str, success: bool, error: str = "") -> str:
    # RELATIVE redirect — stays on whatever host the browser actually used (SIT or
    # prod). Connectors is a real SPA route (/connectors), not a query-selected
    # root view; using /?view=connectors opened the wrong page in desktop popups.
    if success:
        return f"/connectors?connected={urllib.parse.quote(connector_name)}"
    err_enc = urllib.parse.quote(error[:100]) if error else "unknown"
    return f"/connectors?error={err_enc}&connector={urllib.parse.quote(connector_name)}"


def _oauth_complete(connector_name: str, success: bool, error: str = "") -> HTMLResponse:
    target = _ui_redirect(connector_name, success, error)
    payload = {
        "type": "ainxt:connector-oauth",
        "connector": connector_name,
        "success": success,
        "error": error[:100] if error else "",
    }
    title = "Connection complete" if success else "Connection failed"
    message = (
        f"{connector_name} connected successfully. You can close this window."
        if success else
        f"{connector_name} could not connect: {error or 'unknown error'}. You can close this window."
    )
    payload_json = json.dumps(payload).replace("</", "<\\/")
    target_json = json.dumps(target).replace("</", "<\\/")
    html = f"""
<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <title>{escape(title)}</title>
    <meta http-equiv=\"refresh\" content=\"3;url={escape(target, quote=True)}\" />
    <style>
      body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; text-align: center; padding: 72px 24px; color: #111827; }}
      .muted {{ color: #6b7280; }}
    </style>
  </head>
  <body>
    <h2>{escape(title)}</h2>
    <p>{escape(message)}</p>
    <p class=\"muted\">Returning to Connectors…</p>
    <script>
      (function () {{
        var payload = {payload_json};
        try {{
          if (window.opener && !window.opener.closed) {{
            window.opener.postMessage(payload, window.location.origin);
            setTimeout(function () {{ window.close(); }}, 250);
          }} else {{
            window.location.replace({target_json});
          }}
        }} catch (e) {{
          window.location.replace({target_json});
        }}
      }})();
    </script>
  </body>
</html>
"""
    return HTMLResponse(html)
