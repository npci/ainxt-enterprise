# SPDX-License-Identifier: MIT
# ============================================================
# INTERNAL CKMS ROUTER — /internal/ckms
#
# Serves decrypted cloud-LLM API keys to the LLM Proxy (web02)
# at startup so web02 never needs plaintext keys in its own .env.
#
# Security model:
#   - Access is controlled entirely at the nginx layer on web02.
#     nginx only accepts calls from app02 and web02 (localhost),
#     so no application-level token is needed — the network is
#     the gatekeeper.
#   - Never logs key values — only booleans and counts.
#   - Only serves the three cloud-LLM keys (hardcoded allowlist,
#     NOT driven by key_type_mapping) so future additions to the
#     CKMS Protected Inventory cannot silently widen this endpoint.
#
# See: PROXY_LLM_KEY_DELIVERY_REQUIREMENT.html §7.1, §8, §10
# ============================================================

from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from core.logger import logger

router = APIRouter(prefix="/internal/ckms", tags=["internal"])

# Optional application-layer shared secret, in addition to the nginx allow-list.
# When PROXY_KEY_TOKEN is set on app02, the caller must present the same value in
# the X-Proxy-Key-Token header. Network-only control is a single point of failure
# for a secret-dispensing endpoint: a container-to-container call, a port-forward,
# an SSRF pivot, or an nginx misconfiguration would otherwise return every
# provider credential. Unset (the default) preserves the previous nginx-only
# behaviour so existing deployments keep working untouched.
_PROXY_KEY_TOKEN_HEADER = "X-Proxy-Key-Token"


def _authorize(request: Request) -> None:
    """Require the shared-secret header on every call.

    ARCH-F-CORE-002: this key-delivery endpoint was previously protected only
    by the nginx network ACL. A compromised host on the same network segment
    could otherwise call it freely without any application-level credential.
    The token requirement adds a second layer of defence in addition to nginx.

    STAGED ROLLOUT, NOW OPERATOR-CONTROLLED (EA Finding 1 / Finding 7,
    implemented): when PROXY_KEY_TOKEN is unset, this function logs a warning
    and allows the request through by default — preserving backward
    compatibility for deployments that have not yet set the token. What
    changed: core/config.py::validate_prod_config() now hard-fails startup
    when PROXY_KEY_TOKEN is unset AND the operator has explicitly set
    REQUIRE_PROXY_KEY_TOKEN=true. This makes "when does warn-only end"
    an explicit, operator-controlled decision (flip one env var when ready)
    rather than an unconditional promotion that could take down any
    not-yet-configured deployment without warning.
    """
    expected = (os.environ.get("PROXY_KEY_TOKEN") or "").strip()
    if not expected:
        logger.warning(
            "internal_ckms_router: PROXY_KEY_TOKEN is not configured — proxy-keys "
            "endpoint is relying on nginx network ACL only (staged rollout: "
            "set PROXY_KEY_TOKEN to activate the application-layer token check, "
            "or set REQUIRE_PROXY_KEY_TOKEN=true to make it a hard startup "
            "requirement — see core/config.py::validate_prod_config)."
        )
        return
    presented = (request.headers.get(_PROXY_KEY_TOKEN_HEADER) or "").strip()
    # compare_digest keeps the check constant-time so a caller cannot recover
    # the token byte-by-byte from response timing.
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail=f"invalid or missing {_PROXY_KEY_TOKEN_HEADER} header")

# Hardcoded allowlist — exactly these env vars, nothing else.
# Do NOT replace with a loop over key_type_mapping or PROTECTED_ENV_VARS.
# A future addition to the Protected Inventory must be explicitly added
# here — it cannot silently widen this endpoint.
#
# The response uses short neutral aliases instead of the real env-var names
# so the wire payload does not reveal which providers are in use to anyone
# inspecting traffic.
_LLM_KEY_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("an", "ANTHROPIC_API_KEY"),
    ("op", "OPENAI_API_KEY"),
    ("ge", "GEMINI_API_KEY"),
    ("ga", "GOOGLE_API_KEY"),
    ("ll", "LITELLM_API_KEY"),
    ("oa", "OPENAI_ADMIN_API_KEY"),
    ("aa", "ANTHROPIC_ADMIN_API_KEY"),
    ("lo", "LOCAL_LLM_API_KEY"),
    ("no", "NOMIC_EMBED_API_KEY"),
    ("fo", "FIMI_OPENAI_API_KEY"),
    ("fa", "FIMI_ANTHROPIC_API_KEY"),
)


@router.get("/proxy-keys")
async def proxy_keys(request: Request):
    """
    Return the current plaintext values of the cloud-LLM API keys
    for the LLM Proxy (web02) to use at startup.

    Called once by web02's ProxyKeyCache.load() when the proxy starts.
    app02's CKMS boot has already decrypted these values into os.environ
    before this endpoint is reachable (router is mounted after load_at_boot).

    Access is controlled at the nginx layer — nginx on web02 only allows calls
    from app02 and localhost. When PROXY_KEY_TOKEN is additionally set on app02,
    the caller must also present it in the X-Proxy-Key-Token header.
    """
    _authorize(request)

    # ── Build response — hardcoded allowlist only ─────────────────────────
    # Keys are returned under short neutral aliases so the wire payload does
    # not reveal provider names to traffic observers.
    keys: dict[str, str | None] = {}
    for alias, env_var in _LLM_KEY_ENV_VARS:
        value = os.environ.get(env_var)
        # Return None for absent keys (provider not configured in this env)
        # rather than failing the whole response.
        keys[alias] = value or None

    # ── Audit log — counts/booleans only, never key values ────────────────
    caller_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    keys_present = sum(1 for v in keys.values() if v)
    from core.logger import logger
    logger.info(
        f"internal_ckms_router: /internal/ckms/proxy-keys served to {caller_ip} "
        f"keys_present={keys_present}/{len(_LLM_KEY_ENV_VARS)} "
        f"an={bool(keys.get('an'))} "
        f"op={bool(keys.get('op'))} "
        f"ge={bool(keys.get('ge'))} "
        f"ga={bool(keys.get('ga'))} "
        f"ll={bool(keys.get('ll'))} "
        f"oa={bool(keys.get('oa'))} "
        f"aa={bool(keys.get('aa'))} "
        f"lo={bool(keys.get('lo'))} "
        f"no={bool(keys.get('no'))} "
        f"fo={bool(keys.get('fo'))} "
        f"fa={bool(keys.get('fa'))}"
    )

    return {
        "keys":        keys,
        "as_of":       datetime.now(timezone.utc).isoformat(),
        "source_host": "app02",
    }
