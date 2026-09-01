# SPDX-License-Identifier: Apache-2.0
# ============================================================
# BUDGET MIDDLEWARE
# Enforces per-user total allocation budgets (tokens, requests, cost).
# Reads X-User-Id header on /ask and /projects/ endpoints.
# ============================================================

import json
import re
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.logger import logger


ENFORCED_PATHS = ("/ask", "/projects/", "/chat/upload", "/agents/", "/ide/", "/v1/chat/completions", "/chat/completions")

# Managed-endpoint proxy paths: /{slug}/v1/chat/completions and /{slug}/v1/models.
#
# These are budget-governed IN-HANDLER by services/endpoint_budget_governor
# (HOD-funded, per-endpoint) and must be skipped here. Two reasons:
#
#   1. Their Bearer token is a SHARED endpoint key whose user_api_keys.user_id is
#      the ADMIN who created the endpoint (endpoint_mgmt_router._generate_endpoint_key).
#      _extract_user_id would therefore resolve every caller to that admin and
#      charge their personal budget — blocking the whole endpoint once that one
#      admin's budget ran out.
#   2. Budget for these endpoints is owned by a HOD, not an individual user, so
#      per-user enforcement is the wrong model entirely.
#
# NOTE: these paths never matched ENFORCED_PATHS anyway (the {slug} sits in front,
# so neither == nor startswith() hits "/v1/chat/completions"). This makes that
# previously-accidental exclusion explicit and documented.
# The slug pattern mirrors _SLUG_RE in routers/endpoint_mgmt_router.py.
_ENDPOINT_PROXY_RE = re.compile(
    r"^/[a-z0-9][a-z0-9\-]{2,49}/v1/(chat/completions|models)$"
)

# Cloud API model prefixes — these carry real API cost and are subject to budget enforcement.
# Anything else is assumed to be an explicitly named in-house model (GPU cluster, self-hosted)
# routed via LiteLLM, and is always budget-exempt.
# Empty/auto/default values are treated as cloud-routing (safe conservative default).
_CLOUD_PREFIXES = ("gpt-", "claude-", "gemini-", "openai/", "anthropic/", "google/", "azure/")
_AUTO_HINTS     = frozenset({"", "auto", "default"})


def _is_inhouse_model(hint: str) -> bool:
    """
    Return True if 'hint' is an explicitly named in-house model (non-cloud, non-auto).
    Budget enforcement is SKIPPED for in-house models — they carry no external API cost.
    """
    h = (hint or "").lower().strip()
    if not h or h in _AUTO_HINTS:
        return False   # empty/auto might route to a cloud model — check budget
    return not any(h.startswith(p) for p in _CLOUD_PREFIXES)


def _is_local_model_request(body_bytes: bytes) -> bool:
    """Return True if the request body specifies an in-house (non-cloud) model."""
    try:
        body = json.loads(body_bytes)
        hint = (body.get("model") or body.get("hint") or "").lower()
        return _is_inhouse_model(hint)
    except Exception:
        return False


# In-process cache: SHA-256(api_key) → (user_id, expires_epoch)
# Avoids 2 DB queries per request in the middleware (resolve_api_key hits DB
# every call; the route handler's Depends(_require_auth) also resolves, so
# without caching we'd do 4 DB hits per Kilo Code request).
# TTL 5 min — revoked keys become invalid in the middleware within 5 minutes.
import time as _time
_API_KEY_UID_CACHE: dict = {}
_API_KEY_CACHE_TTL = 300  # seconds


def _extract_user_id(request: Request) -> str:
    """
    Resolve user_id from:
      1. JWT Bearer token (browser/CLI users)
      2. Platform API key (IDE plugins: Kilo Code, Cursor, etc.)
         — result cached in-process for 5 min to avoid a DB hit per request
      3. X-User-Id header (last fallback)
    Returns empty string when no identity can be resolved — caller skips budget check.
    """
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()

        # 1. Try JWT (browser/CLI sessions — no DB hit)
        # Note: JWT no longer contains "email" (DAST fix — PII removed from JWT).
        # Use only "sub" (user UUID) as the identity key.
        try:
            from auth.jwt_handler import decode_token
            payload = decode_token(token)
            if payload:
                uid = payload.get("sub") or ""
                if uid:
                    return uid
        except Exception:
            pass

        # 2. Try platform API key (Kilo Code, Cursor, JetBrains IDE plugins)
        #    Cache the hash→user_id mapping for 5 min to avoid DB on every request.
        try:
            from auth.api_key_auth import is_api_key as _iak, _sha256_hex
            if _iak(token):
                _key_hash = _sha256_hex(token)
                _cached = _API_KEY_UID_CACHE.get(_key_hash)
                if _cached and _cached[1] > _time.time():
                    return _cached[0]   # cache hit — no DB query
                # Cache miss: full DB resolve (2 queries)
                from auth.api_key_auth import resolve_api_key as _rak
                kp = _rak(token)
                if kp:
                    uid = kp.get("sub") or kp.get("email") or ""
                    if uid:
                        _API_KEY_UID_CACHE[_key_hash] = (uid, _time.time() + _API_KEY_CACHE_TTL)
                        return uid
        except Exception:
            pass

    return request.headers.get("X-User-Id", "")


class BudgetMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next):

        path = request.url.path

        # Strip the platform API prefix so ENFORCED_PATHS bare strings match
        # regardless of whether the router is mounted under /ainxt/v1/api or not.
        _API_PREFIX = "/ainxt/v1/api"
        _match_path = path[len(_API_PREFIX):] if path.startswith(_API_PREFIX) else path

        # Managed-endpoint proxy — governed by the HOD cap in-handler, not per-user.
        # See _ENDPOINT_PROXY_RE above for why this must be skipped explicitly.
        if _ENDPOINT_PROXY_RE.match(_match_path):
            return await call_next(request)

        # Only enforce on LLM-generating endpoints
        is_enforced = any(_match_path == p or _match_path.startswith(p) for p in ENFORCED_PATHS)

        if not is_enforced:
            return await call_next(request)

        # Skip agent-level OPTIONS / non-run paths under /agents/
        if _match_path.startswith("/agents/") and not _match_path.endswith("/run"):
            return await call_next(request)

        # Skip non-LLM IDE paths — /ide/models and /ide/threads carry no API cost.
        # Only the three generation endpoints actually spend tokens.
        _LLM_IDE_PATHS = {"/ide/chat", "/ide/agent/run", "/ide/workflow/run"}
        if _match_path.startswith("/ide/") and _match_path not in _LLM_IDE_PATHS:
            return await call_next(request)

        # Read body once to check model hint — in-house (non-cloud) models bypass budget enforcement
        body_bytes = await request.body()
        if _is_local_model_request(body_bytes):
            logger.debug("BudgetMiddleware: in-house model request — skipping budget check")
            return await call_next(request)

        user_id = _extract_user_id(request)

        if not user_id:
            return await call_next(request)

        # Lazy import to avoid circular deps at module load
        try:
            from store.budget_store import check_budget, increment_usage
            result = check_budget(user_id)
        except Exception as e:
            logger.warning(f"BudgetMiddleware: check failed → {e}")
            return await call_next(request)

        if not result.get("allowed", True):
            logger.warning(
                f"BudgetMiddleware: BLOCKED user={user_id} reason={result['reason']}"
            )
            # Notify user via inbox (fire-and-forget; ignore errors)
            try:
                from store.inbox_store import publish_inbox_item
                publish_inbox_item(
                    user_id=user_id,
                    type="budget_alert",
                    title="Budget limit reached",
                    body=result["reason"] + " — request an increase from your admin.",
                    priority="High",
                )
            except Exception:
                pass
            return Response(
                content=json.dumps({
                    "error": "Budget limit exceeded",
                    "reason": result["reason"],
                    "code": "BUDGET_EXCEEDED",
                }),
                status_code=429,
                media_type="application/json",
            )

        # Inject user_id into request state for downstream token counting
        request.state.budget_user_id = user_id

        response = await call_next(request)

        # Count this request
        try:
            increment_usage(user_id, requests=1)
        except Exception as e:
            logger.warning(f"BudgetMiddleware: increment failed → {e}")

        return response
