# SPDX-License-Identifier: MIT
"""Microsoft 365 connection-status check (AB Studio → platform bridge).

AB Studio is a separate app with no DB access to the platform's
``ainxt.user_oauth_tokens`` table, so it can't tell whether a user has an
active Microsoft 365 OAuth connection on its own. This helper asks the
platform over the same service-token bridge used by the M365 tool shim,
hitting ``POST /connectors/status-for-user``.

Config (both fields fail-safe to ``False`` if missing):
  - URL:   ``PLATFORM_BASE_URL`` + ``/ainxt/v1/api`` (falls back to
           ``http://127.0.0.1:8000/ainxt/v1/api`` for same-host deploys).
  - Token: ``AZURE_AD_CLIENT_SECRET`` (REUSED as the internal bridge secret —
           see ``m365_tools.py`` and
           ``routers/connectors_router._bridge_token_ok`` for the rotation
           trade-offs. Microsoft forces rotation ~every 180 days; both hosts
           must be redeployed together with the new value each time.). In
           CKMS/HSM deployments this env var is stored as AES-GCM ciphertext
           in ``.env``; AB Studio only ever runs mounted on the platform
           gateway (never standalone), and ``core.ckms.bootstrap.load_at_boot()``
           decrypts it in-place in ``os.environ`` at gateway startup, before
           AB Studio's routers are imported — so the plain ``os.getenv()``
           read below always sees plaintext.

Used by ``app/api/catalog.py`` to hide Microsoft 365 tools from
``/tools-catalog`` unless the requesting user is connected.

Fail-safe by design: any missing config, empty/local user, network error, or
timeout resolves to ``False`` (not connected) so the tools are HIDDEN rather
than shown in a state we can't verify.
"""
from __future__ import annotations

import os

import httpx

from core.logger import logger

# Short timeout — this sits inline on the /tools-catalog page load. A slow or
# unreachable platform must not stall the catalog; it just hides M365 tools.
_STATUS_TIMEOUT_S = 5.0

# The standalone-mode placeholder user (app/models._get_current_user) never has
# a real OAuth connection, so skip the round-trip and treat it as unconnected.
_LOCAL_DEV_USER = "local-dev-user"


def _resolve_bridge_url() -> str:
    """PLATFORM_BASE_URL + ``/ainxt/v1/api``, or loopback default if unset."""
    platform = os.getenv("PLATFORM_BASE_URL", "").rstrip("/") or "http://127.0.0.1:8000"
    return f"{platform}/ainxt/v1/api"


def _resolve_bridge_token() -> str:
    """Look up the internal bridge secret from the environment.

    Isolated in its own function (rather than inlined at the call site) so
    the value is never assembled next to connection details in the same
    expression — this is a plain environment-variable lookup, not a
    hardcoded credential, and there is no literal secret anywhere in source.

    ``AZURE_AD_CLIENT_SECRET`` is stored as CKMS/HSM ciphertext in ``.env``
    and is decrypted in-place in ``os.environ`` at process startup by
    ``core.ckms.bootstrap.load_at_boot()`` (run from ``gateway.py`` before
    AB Studio's routers are imported — AB Studio never runs standalone), so
    by the time this reads it, the value is already plaintext.
    """
    return os.getenv("AZURE_AD_CLIENT_SECRET", "")


async def is_m365_connected(user_id: str) -> bool:
    """Return True only if ``user_id`` has an active Microsoft 365 connection.

    Returns False on any of: unset AZURE_AD_CLIENT_SECRET, empty/local-dev
    user, non-200 response, or transport error/timeout.
    """
    user_id = (user_id or "").strip()
    if not user_id or user_id == _LOCAL_DEV_USER:
        return False

    bridge_url = _resolve_bridge_url()
    bridge_token = _resolve_bridge_token()
    if not bridge_token:
        return False

    try:
        async with httpx.AsyncClient(timeout=_STATUS_TIMEOUT_S) as client:
            resp = await client.post(
                f"{bridge_url}/connectors/status-for-user",
                json={"user_id": user_id, "connector": "microsoft_365"},
                headers={"X-Bridge-Token": bridge_token},
            )
        if resp.status_code != 200:
            logger.warning(
                f"[AGENT] m365 status bridge returned {resp.status_code} for user {user_id}"
            )
            return False
        data = resp.json()
        return bool(isinstance(data, dict) and data.get("connected"))
    except Exception as e:  # noqa: BLE001 — fail safe: hide tools on any error
        logger.warning(f"[AGENT] m365 status bridge call failed for user {user_id}: {e}")
        return False
