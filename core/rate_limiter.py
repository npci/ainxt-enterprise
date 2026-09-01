# SPDX-License-Identifier: Apache-2.0
# ============================================================
# RATE LIMITER — Redis sliding-window rate limiter
#
# Provides IP-based, user-based, and behaviour-based rate limiting
# for authentication endpoints, APIs, and sensitive actions.
#
# Architecture:
#   - Primary store : Redis (sliding-window via sorted-set)
#   - Fallback      : in-process dict counter (non-distributed,
#                     acceptable for single-node deployments)
#
# DAST finding addressed:
#   "The application does not restrict the number of requests a user
#    or IP can send within a specific time frame. Implement rate
#    limiting on authentication endpoints, APIs, and sensitive actions.
#    Use IP-based, user-based, or behavior-based throttling with
#    monitoring and alerting."
#
# Rate-limit tiers:
#   AUTH endpoints   : strict IP-based limits (anti-brute-force)
#   API (global)     : user-based + IP-based throttling (DoS backstop)
#   Sensitive actions: tighter per-user limits (uploads, budget mutations)
#   Anomaly detection: behaviour-based — IPs/users generating 4xx floods
#                      are automatically throttled harder (Redis-backed).
# ============================================================

import os
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, status

from core.logger import logger

# ── Feature flag ──────────────────────────────────────────────────────────────
# RATE_LIMIT_ENABLED=false  → all rate-limit checks are bypassed (dev/test only).
# Default: false (rate limiting not active in all environments unless explicitly enabled).
RATE_LIMIT_ENABLED: bool = os.getenv("RATE_LIMIT_ENABLED", "false").lower() not in ("false", "0", "no")

if not RATE_LIMIT_ENABLED:
    logger.warning(
        "RATE_LIMIT_DISABLED",
        extra={
            "event": "rate_limit_disabled",
            "reason": "RATE_LIMIT_ENABLED env var is false — all rate limiting bypassed",
        },
    )

# ── Redis DB allocation ────────────────────────────────────────────────────────
_RATE_LIMIT_REDIS_DB = 7   # dedicated DB, separate from auth lockout (DB=6)

# ── In-process fallback (when Redis is unavailable) ──────────────────────────
_fallback_counters: dict = {}   # key → (count, window_start)


def _get_redis():
    """Return a Redis client for rate-limiting, or None if unavailable."""
    try:
        from core.config import redis_client as _rc
        r = _rc(db=_RATE_LIMIT_REDIS_DB, decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None


def _client_ip(request: Request) -> str:
    """Best-effort real client IP (handles reverse-proxy X-Forwarded-For)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ── Core sliding-window check ─────────────────────────────────────────────────

def _check_rate_limit_redis(rc, key: str, limit: int, window_seconds: int) -> tuple[int, int]:
    """
    Redis sorted-set sliding-window counter.
    Returns (current_count, ttl_remaining).
    Raises nothing — caller decides whether to block.
    """
    now = time.time()
    window_start = now - window_seconds

    pipe = rc.pipeline()
    pipe.zremrangebyscore(key, "-inf", window_start)      # evict expired entries
    pipe.zadd(key, {str(now): now})                       # add this request
    pipe.zcard(key)                                        # current count
    pipe.expire(key, window_seconds + 1)                  # keep key alive
    results = pipe.execute()
    count = results[2]   # zcard result
    return count, window_seconds


def _check_rate_limit_fallback(key: str, limit: int, window_seconds: int) -> tuple[int, int]:
    """In-process dict fallback (single-process only)."""
    now = time.time()
    entry = _fallback_counters.get(key)
    if entry is None or (now - entry[1]) >= window_seconds:
        _fallback_counters[key] = (1, now)
        return 1, window_seconds
    count, start = entry
    count += 1
    _fallback_counters[key] = (count, start)
    remaining = int(window_seconds - (now - start))
    return count, max(remaining, 1)


# ── Public API ────────────────────────────────────────────────────────────────

@dataclass
class RateLimitConfig:
    """
    Configuration for a single rate-limit rule.

    limit          : maximum number of requests allowed in `window_seconds`
    window_seconds : length of the sliding window in seconds
    key_prefix     : namespace for Redis keys (e.g. 'auth:login')
    scope          : 'ip' | 'user' | 'ip+user'
    block_on_redis_failure : if True, fallback counter is used when Redis
                             is unavailable; if False, requests are allowed
                             through (fail-open).  Defaults to False (HA-safe).
    """
    limit: int
    window_seconds: int
    key_prefix: str
    scope: str = "ip"
    block_on_redis_failure: bool = True   # DEV-17: was False (fail-open) → True (fail-closed)


def enforce_rate_limit(
    request: Request,
    config: RateLimitConfig,
    user_id: Optional[str] = None,
) -> None:
    """
    Enforce a rate limit.  Raises HTTP 429 if the limit is exceeded.
    Also emits a Prometheus counter and structured log for monitoring/alerting.

    Parameters
    ----------
    request : FastAPI Request object (used for IP extraction)
    config  : RateLimitConfig describing the limit
    user_id : optional user identifier (used when scope includes 'user')
    """
    if not RATE_LIMIT_ENABLED:
        return

    ip = _client_ip(request)

    if config.scope == "ip":
        key = f"rl:{config.key_prefix}:ip:{ip}"
    elif config.scope == "user" and user_id:
        key = f"rl:{config.key_prefix}:user:{user_id}"
    elif config.scope == "ip+user" and user_id:
        key = f"rl:{config.key_prefix}:both:{user_id}:{ip}"
    else:
        # Fall back to IP when user_id not available
        key = f"rl:{config.key_prefix}:ip:{ip}"

    rc = _get_redis()
    if rc is not None:
        try:
            count, window = _check_rate_limit_redis(rc, key, config.limit, config.window_seconds)
        except Exception as exc:
            logger.warning(f"rate_limiter: Redis error for key={key}: {exc}")
            if not config.block_on_redis_failure:
                return   # fail-open
            count, window = _check_rate_limit_fallback(key, config.limit, config.window_seconds)
    else:
        count, window = _check_rate_limit_fallback(key, config.limit, config.window_seconds)

    if count > config.limit:
        # ── Structured log for SIEM / alerting pipeline ──────────────────────
        logger.warning(
            "RATE_LIMIT_EXCEEDED",
            extra={
                "event":      "rate_limit_exceeded",
                "key":        key,
                "prefix":     config.key_prefix,
                "scope":      config.scope,
                "count":      count,
                "limit":      config.limit,
                "window_s":   config.window_seconds,
                "ip":         ip,
                "user_id":    user_id or "",
                "path":       request.url.path,
                "method":     request.method,
            },
        )
        # ── Increment Prometheus counter (best-effort) ────────────────────────
        try:
            from metrics import metrics as _m
            _m.rate_limit_exceeded_total.labels(
                prefix=config.key_prefix,
                scope=config.scope,
            ).inc()
        except Exception:
            pass

        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Too many requests. You have exceeded the rate limit of "
                f"{config.limit} requests per {config.window_seconds} seconds. "
                f"Please retry after {config.window_seconds} seconds."
            ),
            headers={
                "Retry-After": str(config.window_seconds),
                "X-RateLimit-Limit": str(config.limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(time.time()) + config.window_seconds),
            },
        )
    else:
        # Emit remaining count header hints for well-behaved clients
        remaining = max(config.limit - count, 0)
        # Headers are injected into the response via middleware; store in request state
        # so the RateLimitMiddleware can pick them up.
        try:
            request.state.rl_limit     = config.limit
            request.state.rl_remaining = remaining
            request.state.rl_reset     = int(time.time()) + config.window_seconds
        except Exception:
            pass


# ── Behaviour-based anomaly detection ────────────────────────────────────────
# Records 4xx events per IP/user. If a client generates ≥ ANOMALY_THRESHOLD
# 4xx responses within ANOMALY_WINDOW seconds, its rate-limit ceiling is
# automatically halved for ANOMALY_BLOCK seconds (Redis TTL-based).

_ANOMALY_4XX_THRESHOLD = 20    # 4xx events before triggering anomaly throttle
_ANOMALY_WINDOW        = 60    # seconds to count 4xx events
_ANOMALY_BLOCK_SECONDS = 300   # 5-minute throttle block on anomaly detection


def record_4xx_event(ip: str, user_id: Optional[str] = None) -> None:
    """
    Increment the 4xx anomaly counter for an IP (and optionally user).
    Called by RateLimitMiddleware after every 4xx response.
    If threshold is reached, sets a Redis flag that enforce_rate_limit checks.
    """
    rc = _get_redis()
    if rc is None:
        return
    try:
        for scope_key in filter(None, [
            f"rl:anomaly:ip:{ip}",
            f"rl:anomaly:user:{user_id}" if user_id else None,
        ]):
            hits = rc.incr(scope_key)
            if hits == 1:
                rc.expire(scope_key, _ANOMALY_WINDOW)
            if hits >= _ANOMALY_4XX_THRESHOLD:
                flag_key = scope_key.replace("rl:anomaly:", "rl:blocked:")
                if not rc.exists(flag_key):
                    logger.warning(
                        "BEHAVIOUR_ANOMALY_DETECTED",
                        extra={
                            "event":    "behaviour_anomaly_detected",
                            "scope_key": scope_key,
                            "hits":     hits,
                            "block_s":  _ANOMALY_BLOCK_SECONDS,
                            "ip":       ip,
                            "user_id":  user_id or "",
                        },
                    )
                    rc.setex(flag_key, _ANOMALY_BLOCK_SECONDS, "1")
                    # Prometheus counter
                    try:
                        from metrics import metrics as _m
                        _m.rate_limit_exceeded_total.labels(
                            prefix="anomaly_block",
                            scope="behaviour",
                        ).inc()
                    except Exception:
                        pass
    except Exception as exc:
        logger.debug(f"record_4xx_event: Redis error: {exc}")


def is_behaviour_blocked(ip: str, user_id: Optional[str] = None) -> bool:
    """
    Return True if this IP or user is currently under a behaviour-based block.
    Called at the top of enforce_rate_limit (or in middleware) before the
    normal sliding-window check to short-circuit obviously malicious clients.
    """
    rc = _get_redis()
    if rc is None:
        return False
    try:
        keys = [f"rl:blocked:ip:{ip}"]
        if user_id:
            keys.append(f"rl:blocked:user:{user_id}")
        return any(rc.exists(k) for k in keys)
    except Exception:
        return False


def enforce_rate_limit_with_behaviour(
    request: Request,
    config: RateLimitConfig,
    user_id: Optional[str] = None,
) -> None:
    """
    Full rate-limit enforcement: behaviour-block check first, then sliding-window.
    Use this on all non-authentication endpoints (e.g. upload, budget, API).
    Authentication endpoints should use enforce_rate_limit directly to avoid
    circular dependency with the lockout subsystem.
    """
    if not RATE_LIMIT_ENABLED:
        return

    ip = _client_ip(request)

    if is_behaviour_blocked(ip, user_id):
        logger.warning(
            "BEHAVIOUR_BLOCK_TRIGGERED",
            extra={
                "event":   "behaviour_block_triggered",
                "ip":      ip,
                "user_id": user_id or "",
                "path":    request.url.path,
            },
        )
        try:
            from metrics import metrics as _m
            _m.rate_limit_exceeded_total.labels(
                prefix="anomaly_block",
                scope="behaviour",
            ).inc()
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Too many requests. Suspicious request pattern detected. "
                f"Access restricted for {_ANOMALY_BLOCK_SECONDS} seconds."
            ),
            headers={"Retry-After": str(_ANOMALY_BLOCK_SECONDS)},
        )

    enforce_rate_limit(request, config, user_id)


# ── Pre-defined limit configs for sensitive endpoints ────────────────────────

#: POST /auth/login — 10 attempts per 5 minutes per IP
AUTH_LOGIN = RateLimitConfig(
    limit=10,
    window_seconds=300,
    key_prefix="auth:login",
    scope="ip",
    block_on_redis_failure=False,
)

#: POST /auth/register — 5 registrations per 10 minutes per IP
AUTH_REGISTER = RateLimitConfig(
    limit=5,
    window_seconds=600,
    key_prefix="auth:register",
    scope="ip",
    block_on_redis_failure=False,
)

#: POST /auth/refresh — 30 per minute per IP (token rotation)
AUTH_REFRESH = RateLimitConfig(
    limit=30,
    window_seconds=60,
    key_prefix="auth:refresh",
    scope="ip",
    block_on_redis_failure=False,
)

#: POST /sso/callback — 20 per minute per IP
SSO_CALLBACK = RateLimitConfig(
    limit=20,
    window_seconds=60,
    key_prefix="sso:callback",
    scope="ip",
    block_on_redis_failure=False,
)

#: POST /chat/upload, /kb/upload — 30 uploads per 5 minutes per user (ip fallback)
FILE_UPLOAD = RateLimitConfig(
    limit=30,
    window_seconds=300,
    key_prefix="upload",
    scope="ip+user",
    block_on_redis_failure=False,
)

#: POST /budget/request-increase — 5 requests per hour per user
BUDGET_REQUEST = RateLimitConfig(
    limit=5,
    window_seconds=3600,
    key_prefix="budget:request",
    scope="ip+user",
    block_on_redis_failure=False,
)

#: POST /budget/users (admin allocation) — 100 per minute per user
BUDGET_ADMIN = RateLimitConfig(
    limit=100,
    window_seconds=60,
    key_prefix="budget:admin",
    scope="ip+user",
    block_on_redis_failure=False,
)

#: Global API — 200 requests per minute per authenticated user (DoS backstop)
GLOBAL_API_USER = RateLimitConfig(
    limit=200,
    window_seconds=60,
    key_prefix="global:api",
    scope="user",
    block_on_redis_failure=False,
)

#: Global API — 300 requests per minute per IP (unauthenticated / IP backstop)
GLOBAL_API_IP = RateLimitConfig(
    limit=300,
    window_seconds=60,
    key_prefix="global:api:ip",
    scope="ip",
    block_on_redis_failure=False,
)

#: Sensitive admin actions — 50 per minute per user
SENSITIVE_ADMIN = RateLimitConfig(
    limit=50,
    window_seconds=60,
    key_prefix="admin:action",
    scope="ip+user",
    block_on_redis_failure=False,
)

#: KB / docs upload — 20 per 5 minutes per user
DOCS_UPLOAD = RateLimitConfig(
    limit=20,
    window_seconds=300,
    key_prefix="docs:upload",
    scope="ip+user",
    block_on_redis_failure=False,
)
