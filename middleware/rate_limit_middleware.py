# SPDX-License-Identifier: MIT
# ============================================================
# RATE LIMIT MIDDLEWARE
#
# Global Starlette middleware that enforces request throttling
# on every API path using IP-based and user-based sliding-window
# counters stored in Redis (with in-process fallback).
#
# DAST finding addressed:
#   "The application does not restrict the number of requests a user
#    or IP can send within a specific time frame. Implement rate
#    limiting on authentication endpoints, APIs, and sensitive actions.
#    Use IP-based, user-based, or behavior-based throttling with
#    monitoring and alerting."
#
# Behaviour
# ---------
#  1. Skips health-check / metrics / static-asset paths.
#  2. Resolves caller identity from JWT or API key (falls back to IP).
#  3. Applies GLOBAL_API_USER (200/min) for authenticated callers.
#  4. Applies GLOBAL_API_IP   (300/min) for unauthenticated/IP callers.
#  5. After every 4xx response, calls record_4xx_event() so the
#     behaviour-anomaly detector can block repeat offenders.
#  6. Injects standard rate-limit headers (X-RateLimit-*) into every
#     response so clients can self-throttle.
#
# Path-level rate limits (tighter) are applied inside individual
# route handlers via enforce_rate_limit / enforce_rate_limit_with_behaviour.
# This middleware is the last-resort DoS backstop.
# ============================================================

import json
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.logger import logger

# Paths that are exempt from global rate-limiting:
#   - health / readiness probes (k8s)
#   - Prometheus metrics scrape
#   - Vite / SPA static assets
#   - OpenTelemetry collector endpoint
_EXEMPT_PREFIXES = (
    "/health",
    "/ready",
    "/metrics",
    "/assets",
    "/favicon",
    "/dist",
    "/static",
    "/opentelemetry",
)


def _is_exempt(path: str) -> bool:
    return any(path.startswith(p) for p in _EXEMPT_PREFIXES)


# Endpoints that already enforce their own dedicated, path-specific rate
# limiter (see routers/auth_router.py's AUTH_LOGIN / AUTH_REGISTER /
# AUTH_REFRESH / SSO_CALLBACK — all IP-scoped and *tighter* than the global
# backstop below, e.g. AUTH_LOGIN = 10 requests / 5 min / IP).
#
# Also applying GLOBAL_API_IP (300 req/min/IP) on top of them is redundant
# at best, and a source of false positives at worst: GLOBAL_API_IP counts
# EVERY unauthenticated request from an IP — not just login attempts — so
# a shared/NAT corporate IP with many concurrent users (chat polling, asset
# fetches, other people's login attempts, etc.) can exhaust the 300/60s IP
# bucket on its own, and then intermittently reject a login request that
# AUTH_LOGIN itself would still have happily allowed. Exempting these paths
# from the global IP backstop removes that false-positive path while
# leaving their own dedicated limiter — and every other endpoint's global
# backstop — fully intact.
_GLOBAL_BACKSTOP_EXEMPT_PATHS = {
    ("POST", "/ainxt/v1/api/auth/login"),
    ("POST", "/ainxt/v1/api/auth/register"),
    ("POST", "/ainxt/v1/api/auth/refresh"),
    ("POST", "/ainxt/v1/api/auth/sso/callback"),
}


def _is_global_backstop_exempt(method: str, path: str) -> bool:
    return (method.upper(), path) in _GLOBAL_BACKSTOP_EXEMPT_PATHS


def _resolve_caller(request: Request) -> tuple[str, str]:
    """
    Return (user_id, ip) for the calling client.
    user_id is empty string when the caller is unauthenticated.
    """
    ip = _get_client_ip(request)
    user_id = ""

    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()

        # Try JWT (browser / CLI)
        # Note: JWT no longer contains "email" (DAST fix — PII removed from JWT).
        # Use only "sub" (user UUID) as the identity key for rate-limiting.
        try:
            from auth.jwt_handler import decode_token
            payload = decode_token(token)
            if payload:
                uid = payload.get("sub") or ""
                if uid:
                    user_id = uid
                    return user_id, ip
        except Exception:
            pass

        # Try platform API key (Kilo Code, Cursor, JetBrains)
        try:
            from auth.api_key_auth import is_api_key as _iak
            if _iak(token):
                from auth.api_key_auth import resolve_api_key as _rak
                kp = _rak(token)
                if kp:
                    uid = kp.get("sub") or kp.get("email") or ""
                    if uid:
                        user_id = uid
        except Exception:
            pass

    return user_id, ip


def _get_client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Application-level rate-limit middleware.

    Applies two global sliding-window checks:
      - Authenticated user : GLOBAL_API_USER  (200 req/min per user_id)
      - IP / anonymous     : GLOBAL_API_IP    (300 req/min per IP)

    Behaviour-anomaly detection is triggered on every 4xx response.

    All responses receive X-RateLimit-* headers so clients can self-pace.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # 1. Exempt paths bypass rate limiting entirely
        if _is_exempt(path):
            return await call_next(request)

        from core.rate_limiter import (
            GLOBAL_API_USER,
            GLOBAL_API_IP,
            enforce_rate_limit,
            record_4xx_event,
            _client_ip,
        )

        user_id, ip = _resolve_caller(request)

        # 2. Apply the appropriate global limit ───────────────────────────────
        # Skip the global backstop for endpoints that already enforce their
        # own dedicated, tighter limiter (see _GLOBAL_BACKSTOP_EXEMPT_PATHS
        # above) — otherwise a shared/NAT IP's unrelated traffic can trip the
        # 300 req/60s IP bucket and reject requests the endpoint's own limiter
        # would still allow.
        try:
            if _is_global_backstop_exempt(request.method, path):
                pass
            elif user_id:
                enforce_rate_limit(request, GLOBAL_API_USER, user_id=user_id)
            else:
                enforce_rate_limit(request, GLOBAL_API_IP)
        except Exception as exc:
            # Re-raise HTTPException (429); absorb any unexpected error (fail-open)
            from fastapi import HTTPException
            if isinstance(exc, HTTPException):
                _inject_rl_headers(
                    Response(
                        content=json.dumps({"detail": exc.detail}),
                        status_code=exc.status_code,
                        media_type="application/json",
                        headers=exc.headers or {},
                    ),
                    request,
                )
                response = Response(
                    content=json.dumps({"detail": exc.detail}),
                    status_code=exc.status_code,
                    media_type="application/json",
                    headers=dict(exc.headers or {}),
                )
                # Record anomaly event for 429 (attacker tripping limits counts as 4xx)
                record_4xx_event(ip, user_id or None)
                return response
            # Unexpected error in rate-limiter — fail open, log warning
            logger.warning(f"RateLimitMiddleware: unexpected error: {exc}")

        # 3. Execute the actual handler ────────────────────────────────────────
        response = await call_next(request)

        # 4. Behaviour anomaly tracking ────────────────────────────────────────
        if 400 <= response.status_code < 500:
            try:
                record_4xx_event(ip, user_id or None)
            except Exception:
                pass

        # 5. Inject X-RateLimit-* headers into every response ─────────────────
        _inject_rl_headers(response, request)

        return response


def _inject_rl_headers(response: Response, request: Request) -> None:
    """
    Copy X-RateLimit-* values stashed in request.state (by enforce_rate_limit)
    into the response headers so clients can self-throttle.
    """
    try:
        limit     = getattr(request.state, "rl_limit",     None)
        remaining = getattr(request.state, "rl_remaining", None)
        reset_at  = getattr(request.state, "rl_reset",     None)
        if limit is not None:
            response.headers["X-RateLimit-Limit"]     = str(limit)
        if remaining is not None:
            response.headers["X-RateLimit-Remaining"] = str(remaining)
        if reset_at is not None:
            response.headers["X-RateLimit-Reset"]     = str(reset_at)
        response.headers["X-RateLimit-Policy"]        = "sliding-window"
    except Exception:
        pass
