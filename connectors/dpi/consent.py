# SPDX-License-Identifier: MIT
"""
DPI consent handler — the Account-Aggregator / DEPA consent model for agents.

This is the architectural novelty vs. OAuth: instead of a bearer token, a user
grants a signed CONSENT ARTIFACT scoped to a PURPOSE, a DATA RANGE, and an
EXPIRY. The agent acts strictly within that mandate. Parallel to
`connectors/oauth2.py::OAuth2Handler`.

Verification is PLUGGABLE:
  - SANDBOX (DPI_SANDBOX=true): artifacts are self-signed; verify checks shape +
    expiry only — no real crypto, no upstream. Fully offline + open-source-safe.
  - PRODUCTION: `verify_artifact` would check the AA/issuer signature against a
    DPI public key (env-injected cert). Left as the Phase-2 plug point.

The artifact is persisted as the encrypted `access_token` in
`ainxt.user_oauth_tokens` (auth_type stamped in metadata) so connection
detection + MCP exposure reuse the existing pipeline unchanged. A dedicated
consent registry can replace this in Phase 2 without touching callers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Optional

from core.logger import logger

# Dev-only signing key for the SANDBOX self-signed artifacts. NOT a security
# boundary — sandbox data is synthetic. Production uses real DPI issuer signatures.
_SANDBOX_SIGNING_KEY = os.getenv("DPI_SANDBOX_SIGNING_KEY", "ainxt-dpi-sandbox-dev-key").encode()


def is_sandbox() -> bool:
    return os.getenv("DPI_SANDBOX", "").strip().lower() in ("1", "true", "yes", "on")


class ConsentHandler:
    """Create, store, and verify DPI consent artifacts."""

    # ── Create ────────────────────────────────────────────────────────────────
    def create_consent_request(
        self,
        connector_name: str,
        user_id: str,
        purpose: str,
        scopes: Optional[list[str]] = None,
        data_range_days: int = 180,
        valid_days: int = 30,
    ) -> dict:
        """Build a consent request. In SANDBOX the artifact is returned already
        'approved' (self-signed). In production this would redirect the user to
        the AA/issuer consent screen and the signed artifact would come back via
        a callback. Returns {request_id, consent_url, artifact}."""
        now = int(time.time())
        consent_id = "consent-" + uuid.uuid4().hex[:16]
        artifact = {
            "consent_id": consent_id,
            "connector": connector_name,
            "user_id": user_id,
            "purpose": purpose,                       # e.g. "Personal finance review"
            "scope": scopes or [],
            "data_range_days": data_range_days,       # e.g. last 180 days of statements
            "issued_at": now,
            "expires_at": now + valid_days * 86400,
            "sandbox": is_sandbox(),
        }
        artifact["signature"] = self._sign(artifact)
        consent_url = (
            f"about:dpi-sandbox-consent/{consent_id}" if is_sandbox()
            else f"<issuer consent screen for {connector_name}>"
        )
        return {"request_id": consent_id, "consent_url": consent_url, "artifact": artifact}

    # ── Verify ──────────────────────────────────────────────────────────────────
    def verify_artifact(self, artifact: dict) -> tuple[bool, str]:
        """Return (ok, reason). Honours expiry always; signature check is
        sandbox-self-signed or (production) issuer-signature. Purpose-limitation
        is enforced by the caller using artifact['purpose']/['scope']."""
        if not isinstance(artifact, dict) or not artifact.get("consent_id"):
            return False, "malformed consent artifact"
        if int(artifact.get("expires_at", 0)) < int(time.time()):
            return False, "consent expired — please re-grant"
        sig = artifact.get("signature", "")
        if artifact.get("sandbox"):
            if not hmac.compare_digest(sig, self._sign(artifact)):
                return False, "sandbox signature mismatch"
            return True, "ok (sandbox, self-signed)"
        # PRODUCTION plug point: verify against the DPI issuer public key.
        # Deliberately fail-closed until real verification is wired (Phase 2).
        return False, "production consent verification not configured (set DPI_SANDBOX=true to use the open sandbox)"

    # ── Sign (sandbox only) ───────────────────────────────────────────────────
    def _sign(self, artifact: dict) -> str:
        payload = json.dumps(
            {k: artifact[k] for k in sorted(artifact) if k != "signature"},
            separators=(",", ":"), default=str,
        ).encode()
        return hmac.new(_SANDBOX_SIGNING_KEY, payload, hashlib.sha256).hexdigest()


consent_handler = ConsentHandler()
