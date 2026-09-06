# SPDX-License-Identifier: MIT
"""
Trusted internal identity assertion for the Discussions module (Apache Answer).

No external IdP, no OIDC, no internet call. Apache Answer only ever receives
connections from this gateway process (bound to 127.0.0.1) — so a local
HMAC-signed, 60-second-fresh assertion is enough for Answer's ainxtbridge
UserCenter plugin to trust the caller's identity without a second login.

See docs/DISCUSSIONS_MODULE_IMPLEMENTATION_PLAN.md §3 and
docs/DISCUSSIONS_MODULE_DB_SCRIPTS_CONFIG.md §3 for the full design.
"""

import base64
import hashlib
import hmac
import json
import time

from core.config import ANSWER_ASSERTION_SECRET

ASSERTION_HEADER = "X-AiNxt-Assertion"


def make_assertion(user_claims: dict) -> str:
    """Build a signed assertion from the caller's already-verified JWT claims.

    Format is deliberately JWT-shaped (base64url-payload "." hex-hmac): the
    JSON payload is base64url-encoded BEFORE joining with "." — raw JSON can
    itself contain "." (e.g. an email address or a decimal), which would break
    a naive split on the verifying side if the payload were joined unencoded.
    """
    payload = {
        "sub": user_claims.get("sub"),
        "email": user_claims.get("email"),
        "display_name": user_claims.get("display_name") or user_claims.get("name"),
        "role": user_claims.get("role"),
        "department": user_claims.get("department"),
        "ad_level": user_claims.get("ad_level"),
        "iat": int(time.time()),
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    body_b64 = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    sig = hmac.new(ANSWER_ASSERTION_SECRET.encode(), body_b64.encode(), hashlib.sha256).hexdigest()
    return f"{body_b64}.{sig}"
