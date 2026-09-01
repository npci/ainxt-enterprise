# SPDX-License-Identifier: Apache-2.0
# ============================================================
# BUDGET STORE — Redis db=4  (cache)  +  Postgres  (truth)
#
# Allocation model:
#   - Each user gets a total allocation (default $50).
#   - Usage accumulates until exhausted — no automatic reset.
#   - Admin tops up via POST /budget/users/{uid}/reset-usage
#     or increases the limit via POST /budget/users.
#
# Resilience:
#   - Redis is the fast-path cache (sub-ms reads, no DB hit per request).
#   - Postgres (user_usage_totals + budget_configs) is the source of truth.
#   - On Redis outage: falls back to Postgres — slower but never blocks users.
#   - On both failing: fail-open (log error, allow request, don't halt platform).
# ============================================================

import json
import re
import time
from datetime import datetime, timezone
from typing import Final, Optional, List

from core.config import RDB_BUDGET
from core.kv import get_kv, KVError
from core.logger import logger

# ---------------------------------------------------------------------------
# Input validation for identifiers re-read from the database
#
# Values that come back out of our own tables are NOT trusted here: a row could
# have been written earlier through a different path, so anything read back and
# then reused in a later query is validated first, exactly as if it had just
# arrived from an HTTP request.
#
# The check is a positive allow-list (whitelist): the value must match the
# permitted e-mail shape in full. Every SQL metacharacter — quote, semicolon,
# comment marker, parenthesis, whitespace, backslash, wildcard — falls outside
# this pattern, so such values are REJECTED rather than escaped or cleaned up.
#
# NOTE: the approval path no longer needs this for the HOD cap charge — that
# flow now resolves the charged HOD in-database (see
# check_and_reserve_cap_for_request), so no DB-sourced e-mail is carried back
# into a later query at all. This helper is retained as a reusable guard for
# any future call site that does need to re-use a stored identifier.
# ---------------------------------------------------------------------------
_RE_HOD_EMAIL: Final = re.compile(
    r"^[A-Za-z0-9._%+-]{1,190}@[A-Za-z0-9.-]{1,60}\.[A-Za-z]{2,24}$"
)
_MAX_EMAIL_LEN: Final[int] = 254


def _validated_hod_email(value) -> Optional[str]:
    """Return `value` if it is a well-formed e-mail address, else None.

    Used to validate an HOD e-mail that was read back from the database before
    it is passed on to any code that will use it in a further query. Returning
    None (rather than a sanitised string) keeps this a strict accept/reject
    gate — callers must abort, never continue with a repaired value.
    """
    if not isinstance(value, str):
        return None
    if not value or len(value) > _MAX_EMAIL_LEN:
        return None
    if not _RE_HOD_EMAIL.fullmatch(value):
        return None
    return value

# ---------------------------------------------------------------------------
# Budget KV connection (DB=4)
# Backend selected via REDIS_CLIENT_CONFIG_DB4.
# This is the financial source-of-truth fast-path. Postgres is the
# durable backstop — if the KV is unavailable, all reads fall back to
# Postgres (see resilience chain in check_budget()).
# ---------------------------------------------------------------------------
_r = None

def _get_redis():
    """Return a cached KV client for the budget DB (DB=4).

    Name retained for backwards compatibility; returns a KVClient.
    """
    global _r
    if _r is None:
        try:
            c = get_kv(RDB_BUDGET, decode_responses=True)
            c.ping()
            _r = c
        except KVError as e:
            logger.warning(f"BudgetStore: KV backend unavailable → {e}")
    return _r


def _redis_hset_mapping(rc, key: str, mapping: dict) -> None:
    """
    Set hash fields using the lowest-common-denominator HSET protocol.

    redis-py's `hset(key, mapping=...)` convenience is not part of the
    KV client contract in core/kv/base.py, and backends without it raise
    "wrong number of arguments for 'hset' command". This helper emits one
    HSET per field, which every compatible backend accepts.
    """
    for field, value in mapping.items():
        rc.hset(key, field, value)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Postgres helpers
# ---------------------------------------------------------------------------

def _pg():
    """Return a raw psycopg2 connection to the platform database."""
    import psycopg2
    from core.config import postgres_dsn, POSTGRES_SCHEMA
    # statement_timeout bounds query EXECUTION (connect_timeout only bounds the
    # TCP/auth handshake). Budget queries are tiny; if one hangs on a lock under
    # load it must not freeze the gateway event loop — cap it at 5s so the
    # fail-open path in check_budget() engages instead of a SIGABRT.
    #
    # NOTE: postgres_dsn() embeds "?options=-csearch_path=<schema>,public" in the
    # DSN itself. psycopg2.connect(dsn, **kwargs) merges kwargs into the parsed
    # DSN via make_dsn() — an explicit `options=` kwarg here does NOT append to
    # the DSN's options, it REPLACES it entirely. Passing options= separately
    # (as before) silently dropped search_path, causing every query below to
    # resolve against the default `public` schema instead of `ainxt` — e.g.
    # "relation \"user_usage_totals\" does not exist" even though the table
    # exists (in the `ainxt` schema). Combine both into a single options string.
    return psycopg2.connect(
        postgres_dsn(), connect_timeout=5,
        options=f"-c search_path={POSTGRES_SCHEMA},public -c statement_timeout=5000",
    )


def _pg_get_budget(user_id: str) -> Optional[dict]:
    """Read budget limits from budget_configs (Postgres fallback for Redis miss)."""
    try:
        conn = _pg()
        cur  = conn.cursor()
        cur.execute("""
            SELECT max_cost_usd_total, max_tokens_total, max_requests_total,
                   monthly_limit_usd, base_cost_usd, extra_cost_usd,
                   winner_extra_usd, winner_origin_period
            FROM   budget_configs
            WHERE  user_id = %s
            LIMIT  1
        """, (user_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if not row:
            return None
        # max_cost_usd_total column may be NULL on old rows — fall back to monthly_limit_usd
        cost  = float(row[0]) if row[0] is not None else float(row[3] or 50.0)
        tok   = int(row[1])   if row[1] is not None else 500_000
        reqs  = int(row[2])   if row[2] is not None else 1_000
        base  = float(row[4]) if row[4] is not None else 50.0
        extra = float(row[5]) if row[5] is not None else max(0.0, cost - 50.0)
        winner_extra = float(row[6]) if row[6] is not None else 0.0
        return {
            "user_id":              user_id,
            "max_cost_usd_total":   cost,
            "max_tokens_total":     tok,
            "max_requests_total":   reqs,
            "base_cost_usd":        base,
            "extra_cost_usd":       extra,
            # Winner-origin slice of `extra_cost_usd` (see BudgetConfig model).
            "winner_extra_usd":     winner_extra,
            "winner_origin_period": row[7],
        }
    except Exception as e:
        logger.warning(f"BudgetStore._pg_get_budget({user_id}): {e}")
        return None


def _pg_get_usage(user_id: str) -> dict:
    """Read cumulative usage from user_usage_totals (Postgres fallback)."""
    _zero = {"tokens_used": 0, "requests_made": 0, "cost_usd_spent": 0.0}
    try:
        conn = _pg()
        cur  = conn.cursor()
        cur.execute("""
            SELECT tokens_used, requests_made, cost_usd_spent
            FROM   user_usage_totals
            WHERE  user_id = %s
        """, (user_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if not row:
            return _zero
        return {
            "tokens_used":    int(row[0]),
            "requests_made":  int(row[1]),
            "cost_usd_spent": round(float(row[2]), 6),
        }
    except Exception as e:
        logger.warning(f"BudgetStore._pg_get_usage({user_id}): {e}")
        return _zero


def _pg_increment(user_id: str, tokens: int, requests: int, cost_usd: float) -> None:
    """
    Atomic upsert into user_usage_totals.
    Uses ON CONFLICT DO UPDATE so it's safe under concurrent workers.
    """
    try:
        conn = _pg()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO user_usage_totals (user_id, tokens_used, requests_made, cost_usd_spent, last_updated)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE
                SET tokens_used    = user_usage_totals.tokens_used    + EXCLUDED.tokens_used,
                    requests_made  = user_usage_totals.requests_made  + EXCLUDED.requests_made,
                    cost_usd_spent = user_usage_totals.cost_usd_spent + EXCLUDED.cost_usd_spent,
                    last_updated   = NOW()
        """, (user_id, tokens, requests, cost_usd))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        logger.warning(f"BudgetStore._pg_increment({user_id}): {e}")


def _pg_upsert_budget(user_id: str, max_tokens: int, max_requests: int,
                      max_cost_usd: float, model_limits: dict,
                      base_cost_usd: Optional[float] = None,
                      extra_cost_usd: Optional[float] = None,
                      winner_extra_usd: Optional[float] = None,
                      winner_origin_period: Optional[str] = None) -> None:
    """Write budget limits to budget_configs (Postgres source of truth).

    base_cost_usd / extra_cost_usd: if not explicitly provided, derive them
    so max_cost_usd_total stays consistent — base defaults to max_cost_usd
    (whole amount treated as base) and extra to 0, UNLESS an existing row
    already has an extra_cost_usd, in which case that split is only touched
    when the caller explicitly passes new base/extra values (see set_budget).

    winner_extra_usd / winner_origin_period: deliberately PRESERVE-on-omit.
    Unlike base/extra above, passing None here never zeroes the stored value —
    it leaves whatever is already on the row untouched. A winner's carried
    balance must survive incidental writes from callers (auto-seed, token/
    request tweaks) that know nothing about the winner fields. The only paths
    that legitimately clear them are the monthly reset (via the ORM) and
    apply_winner_grants (via its own transaction).
    """
    if base_cost_usd is None and extra_cost_usd is None:
        base_cost_usd, extra_cost_usd = max_cost_usd, 0.0
    elif base_cost_usd is None:
        base_cost_usd = max(0.0, max_cost_usd - (extra_cost_usd or 0.0))
    elif extra_cost_usd is None:
        extra_cost_usd = max(0.0, max_cost_usd - base_cost_usd)
    try:
        conn = _pg()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO budget_configs
                (id, user_id, monthly_limit_usd, max_tokens_total,
                 max_requests_total, max_cost_usd_total, model_allowlist,
                 base_cost_usd, extra_cost_usd,
                 winner_extra_usd, winner_origin_period, created_at, updated_at)
            VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
                    COALESCE(%s::numeric, 0.0), %s, NOW(), NOW())
            ON CONFLICT (user_id) DO UPDATE
                SET monthly_limit_usd  = EXCLUDED.monthly_limit_usd,
                    max_tokens_total   = EXCLUDED.max_tokens_total,
                    max_requests_total = EXCLUDED.max_requests_total,
                    max_cost_usd_total = EXCLUDED.max_cost_usd_total,
                    model_allowlist    = EXCLUDED.model_allowlist,
                    base_cost_usd      = EXCLUDED.base_cost_usd,
                    extra_cost_usd     = EXCLUDED.extra_cost_usd,
                    -- Preserve-on-omit: the raw parameter is re-referenced here
                    -- rather than EXCLUDED.*, because EXCLUDED already had the
                    -- INSERT-side COALESCE(..., 0.0) applied to it.
                    winner_extra_usd     = COALESCE(%s::numeric, budget_configs.winner_extra_usd),
                    winner_origin_period = COALESCE(%s::varchar, budget_configs.winner_origin_period),
                    updated_at         = NOW()
        """, (user_id, max_cost_usd, max_tokens, max_requests,
              max_cost_usd, json.dumps(model_limits or []),
              base_cost_usd, extra_cost_usd,
              winner_extra_usd, winner_origin_period,
              winner_extra_usd, winner_origin_period))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        logger.warning(f"BudgetStore._pg_upsert_budget({user_id}): {e}")


# ============================================================
# BUDGET CONFIG
# ============================================================

# ── Store-layer validation constants (defence-in-depth) ──────────────────────
_STORE_MAX_TOKENS_PER_DAY:   int   = 50_000_000
_STORE_MAX_REQUESTS_PER_DAY: int   = 100_000
_STORE_MAX_COST_USD_PER_DAY: float = 10_000.0


def _validate_budget_values(
    user_id: str,
    max_tokens_per_day: int,
    max_requests_per_day: int,
    max_cost_usd_per_day: float,
    context: str = "set_budget",
) -> None:
    """
    Defence-in-depth guard: rejects negative or out-of-range budget values and
    logs any anomalous attempt for fraud/monitoring purposes.
    Raises ValueError on constraint violation.
    """
    anomalies = []

    if max_tokens_per_day < 0:
        anomalies.append(f"negative max_tokens_per_day={max_tokens_per_day}")
    if max_requests_per_day < 0:
        anomalies.append(f"negative max_requests_per_day={max_requests_per_day}")
    if max_cost_usd_per_day < 0:
        anomalies.append(f"negative max_cost_usd_per_day={max_cost_usd_per_day}")
    if max_tokens_per_day > _STORE_MAX_TOKENS_PER_DAY:
        anomalies.append(f"max_tokens_per_day={max_tokens_per_day} exceeds ceiling {_STORE_MAX_TOKENS_PER_DAY}")
    if max_requests_per_day > _STORE_MAX_REQUESTS_PER_DAY:
        anomalies.append(f"max_requests_per_day={max_requests_per_day} exceeds ceiling {_STORE_MAX_REQUESTS_PER_DAY}")
    if max_cost_usd_per_day > _STORE_MAX_COST_USD_PER_DAY:
        anomalies.append(f"max_cost_usd_per_day={max_cost_usd_per_day} exceeds ceiling {_STORE_MAX_COST_USD_PER_DAY}")

    if anomalies:
        logger.warning(
            f"BUDGET_ANOMALY [{context}] user_id={user_id} violations={anomalies} "
            f"timestamp={datetime.now(timezone.utc).isoformat()}"
        )
        raise ValueError(f"Invalid budget values: {'; '.join(anomalies)}")


def set_budget(user_id: str, max_cost_usd_per_day: float = 0.0,
               max_tokens_total: int = 100_000_000,
               max_requests_total: int = 5_000,
               max_cost_usd_total: float = 30.0,
               model_limits: Optional[dict] = None,
               base_cost_usd: Optional[float] = None,
               extra_cost_usd: Optional[float] = None,
               winner_extra_usd: Optional[float] = None,
               winner_origin_period: Optional[str] = None) -> dict:
    """Set or update a user's total allocation. Writes to Redis + Postgres.

    base_cost_usd / extra_cost_usd: optional explicit split of the cost
    allocation (see db/models.py BudgetConfig docstring). When omitted,
    max_cost_usd_total is treated entirely as base with extra=0 (preserves
    behaviour for simple/legacy callers such as auto-seeding). Callers that
    need to set/adjust the split explicitly (winner-allocation, approval
    increments, monthly reset) should pass both.

    winner_extra_usd / winner_origin_period: PRESERVED when omitted — passing
    None leaves the stored values alone rather than zeroing them, so a
    caller that knows nothing about the winner carryover cannot silently
    destroy a winner's balance. Contrast with base/extra above, which DO
    default when omitted.
    """
    # Defence-in-depth: validate even if the router already validated
    from routers.budget_router import _MAX_TOKENS_PER_DAY as max_tokens_per_day, _MAX_REQUESTS_PER_DAY as max_requests_per_day
    _validate_budget_values(
        user_id=user_id,
        max_tokens_per_day=int(max_tokens_per_day),
        max_requests_per_day=int(max_requests_per_day),
        max_cost_usd_per_day=float(max_cost_usd_per_day),
        context="set_budget",
    )
    if base_cost_usd is None and extra_cost_usd is None:
        base_cost_usd, extra_cost_usd = max_cost_usd_total, 0.0
    elif base_cost_usd is None:
        base_cost_usd = max(0.0, max_cost_usd_total - (extra_cost_usd or 0.0))
    elif extra_cost_usd is None:
        extra_cost_usd = max(0.0, max_cost_usd_total - base_cost_usd)

    budget = {
        "user_id":            user_id,
        "max_tokens_per_day": max_tokens_per_day,
        "max_requests_per_day": max_requests_per_day,
        "max_cost_usd_per_day": max_cost_usd_per_day,
        "max_tokens_total":   max_tokens_total,
        "max_requests_total": max_requests_total,
        "max_cost_usd_total": max_cost_usd_total,
        "base_cost_usd":      base_cost_usd,
        "extra_cost_usd":     extra_cost_usd,
        "model_limits":       model_limits or {},
        "created_at":         time.time(),
    }
    if winner_extra_usd is not None:
        budget["winner_extra_usd"] = winner_extra_usd
    if winner_origin_period is not None:
        budget["winner_origin_period"] = winner_origin_period

    # ── 1. Redis (fast path) ──
    rc = _get_redis()
    if rc:
        try:
            mapping = {
                "max_tokens_total":   max_tokens_total,
                "max_requests_total": max_requests_total,
                "max_cost_usd_total": max_cost_usd_total,
                "max_tokens_per_day": max_tokens_per_day,
                "max_requests_per_day": max_requests_per_day,
                "max_cost_usd_per_day": max_cost_usd_per_day,
                "base_cost_usd":      base_cost_usd,
                "extra_cost_usd":     extra_cost_usd,
                "model_limits":       json.dumps(model_limits or {}),
            }
            # Mirror the preserve-on-omit contract in the cache: only write the
            # winner fields when the caller actually supplied them, so an
            # unrelated set_budget() can't blank a winner's cached balance and
            # make get_budget() serve $0 until the next Postgres fallback.
            if winner_extra_usd is not None:
                mapping["winner_extra_usd"] = winner_extra_usd
            if winner_origin_period is not None:
                mapping["winner_origin_period"] = winner_origin_period
            _redis_hset_mapping(rc, f"budget:{user_id}", mapping)
            rc.sadd("budget:users:index", user_id)
        except Exception as e:
            logger.warning(f"BudgetStore.set_budget Redis write failed: {e}")

    # ── 2. Postgres (source of truth) ──
    _pg_upsert_budget(user_id, max_tokens_total, max_requests_total,
                      max_cost_usd_total, model_limits or {},
                      base_cost_usd=base_cost_usd, extra_cost_usd=extra_cost_usd,
                      winner_extra_usd=winner_extra_usd,
                      winner_origin_period=winner_origin_period)
    return budget


def get_budget(user_id: str) -> Optional[dict]:
    """
    Read budget limits. Tries Redis first; falls back to Postgres on miss/failure.
    """
    # ── 1. Try Redis ──
    rc = _get_redis()
    if rc:
        try:
            data = rc.hgetall(f"budget:{user_id}")
            if data:
                _cost_total = float(data.get("max_cost_usd_total") or data.get("max_cost_usd_per_day", 30.0))
                _base  = data.get("base_cost_usd")
                _extra = data.get("extra_cost_usd")
                _wextra = data.get("winner_extra_usd")
                base_cost_usd  = float(_base)  if _base  is not None else _cost_total
                extra_cost_usd = float(_extra) if _extra is not None else 0.0
                return {
                    "user_id":            user_id,
                    # Support both new field names and old _per_day names
                    "max_tokens_per_day": int(data.get("max_tokens_per_day", 0)),
                    "max_requests_per_day": int(data.get("max_requests_per_day", 0)),
                    "max_cost_usd_per_day": float(data.get("max_cost_usd_per_day", 0.0)),
                    "max_tokens_total":   int(data.get("max_tokens_total")   or data.get("max_tokens_per_day", 100_00_000)),
                    "max_requests_total": int(data.get("max_requests_total") or data.get("max_requests_per_day", 5_000)),
                    "max_cost_usd_total": _cost_total,
                    "base_cost_usd":      base_cost_usd,
                    "extra_cost_usd":     extra_cost_usd,
                    # Absent on hashes written before the carryover change —
                    # 0.0/None matches a non-winner, and the next award or
                    # reset repopulates the field.
                    "winner_extra_usd":   float(_wextra) if _wextra is not None else 0.0,
                    "winner_origin_period": data.get("winner_origin_period") or None,
                    "model_limits":       json.loads(data.get("model_limits", "{}")),
                }
        except Exception as e:
            logger.warning(f"BudgetStore.get_budget Redis read failed: {e}")

    # ── 2. Fallback: Postgres ──
    return _pg_get_budget(user_id)


def delete_budget(user_id: str) -> bool:
    rc = _get_redis()
    if rc:
        try:
            rc.delete(f"budget:{user_id}")
            rc.srem("budget:users:index", user_id)
        except Exception:
            pass
    try:
        conn = _pg()
        cur  = conn.cursor()
        cur.execute("DELETE FROM budget_configs WHERE user_id = %s", (user_id,))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        logger.warning(f"BudgetStore.delete_budget PG: {e}")
    return True


def list_budget_users() -> List[str]:
    rc = _get_redis()
    if rc:
        try:
            return list(rc.smembers("budget:users:index"))
        except Exception:
            pass
    # Fallback: Postgres
    try:
        conn = _pg()
        cur  = conn.cursor()
        cur.execute("SELECT user_id FROM budget_configs WHERE user_id IS NOT NULL")
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


# ============================================================
# USAGE TRACKING
# ============================================================

def increment_usage(user_id: str, tokens: int = 0, requests: int = 1,
                    cost_usd: float = 0.0, product_id: str = "") -> None:
    """
    Increment cumulative usage for a user.

    Writes to THREE destinations:
      1. Redis usage:{uid}:total  — no TTL, fast-path for check_budget().
      2. Postgres user_usage_totals — durable source of truth. If Redis is down,
         the Postgres total is what check_budget() falls back to.
      3. Redis usage:{uid}:{date} — 8-day TTL, for history/chargeback display only.
    """
    # ── 1 & 3. Redis ──
    rc = _get_redis()
    if rc:
        try:
            total_key = f"usage:{user_id}:total"
            if tokens:
                rc.hincrby(total_key, "tokens_used", tokens)
            if requests:
                rc.hincrby(total_key, "requests_made", requests)
            if cost_usd:
                rc.hincrbyfloat(total_key, "cost_usd_spent", cost_usd)

            dated_key = f"usage:{user_id}:{_today()}"
            if tokens:
                rc.hincrby(dated_key, "tokens_used", tokens)
            if requests:
                rc.hincrby(dated_key, "requests_made", requests)
            if cost_usd:
                rc.hincrbyfloat(dated_key, "cost_usd_spent", cost_usd)
            rc.expire(dated_key, 8 * 24 * 3600)
            rc.sadd("budget:users:index", user_id)
        except Exception as e:
            logger.warning(f"BudgetStore.increment_usage Redis failed: {e}")

    # ── 2. Postgres (source of truth, always written) ──
    if tokens or requests or cost_usd:
        _pg_increment(user_id, tokens, requests, cost_usd)

    # ── Per-product daily cost (chargeback, Redis only — best-effort) ──
    if product_id and cost_usd and rc:
        try:
            prod_key = f"usage:product:{product_id}:{_today()}"
            rc.hincrbyfloat(prod_key, "cost_usd_spent", cost_usd)
            if tokens:
                rc.hincrby(prod_key, "tokens_used", tokens)
            rc.hincrby(prod_key, "requests_made", requests)
            rc.expire(prod_key, 35 * 24 * 3600)
        except Exception:
            pass


def get_usage_total(user_id: str) -> dict:
    """
    Return cumulative (all-time) usage. Used by check_budget() for enforcement.
    Tries Redis first; falls back to Postgres on miss/failure.
    """
    _zero = {"tokens_used": 0, "requests_made": 0, "cost_usd_spent": 0.0}

    # ── 1. Try Redis ──
    rc = _get_redis()
    if rc:
        try:
            data = rc.hgetall(f"usage:{user_id}:total")
            if data:
                return {
                    "tokens_used":    int(data.get("tokens_used", 0)),
                    "requests_made":  int(data.get("requests_made", 0)),
                    "cost_usd_spent": round(float(data.get("cost_usd_spent", 0.0)), 6),
                }
        except Exception as e:
            logger.warning(f"BudgetStore.get_usage_total Redis failed: {e}")

    # ── 2. Fallback: Postgres ──
    return _pg_get_usage(user_id)


def get_usage_today(user_id: str) -> dict:
    """Return today-only usage (for history display — not used for enforcement)."""
    rc = _get_redis()
    if rc:
        try:
            data = rc.hgetall(f"usage:{user_id}:{_today()}")
            return {
                "tokens_used":    int(data.get("tokens_used", 0)),
                "requests_made":  int(data.get("requests_made", 0)),
                "cost_usd_spent": round(float(data.get("cost_usd_spent", 0.0)), 6),
            }
        except Exception:
            pass
    # Fallback: sum model_usages for today from Postgres
    try:
        conn = _pg()
        cur  = conn.cursor()
        cur.execute("""
            SELECT COALESCE(SUM(total_tokens), 0),
                   COUNT(*),
                   COALESCE(SUM(cost_usd), 0.0)
            FROM   model_usages
            WHERE  user_id::text = %s
              AND  created_at >= CURRENT_DATE
        """, (user_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return {
            "tokens_used":    int(row[0]) if row else 0,
            "requests_made":  int(row[1]) if row else 0,
            "cost_usd_spent": round(float(row[2]), 6) if row else 0.0,
        }
    except Exception:
        return {"tokens_used": 0, "requests_made": 0, "cost_usd_spent": 0.0}


def get_usage_history(user_id: str, days: int = 7,
                      month_to_date: bool = False) -> List[dict]:
    """Per-day usage history (newest first).

    Redis dated keys (usage:{uid}:{date}) expire after 8 days, so for any date
    older than that we backfill from the durable Postgres model_usages table.
    Without this backfill, dates beyond the Redis TTL incorrectly render as
    zero even when real usage exists in the database.

    When month_to_date=True the range is the 1st of the current month → today
    (inclusive) instead of a rolling `days` window.
    """
    from datetime import timedelta
    rc = _get_redis()
    today = datetime.now(timezone.utc)

    # ── Build the ordered list of dates (newest first) ──
    span = today.day if month_to_date else days   # today.day → 1st-of-month..today
    span = max(1, span)
    dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span)]

    # 1) Seed every date to zero so gaps are explicit, not missing.
    by_date = {
        d: {"date": d, "tokens_used": 0, "requests_made": 0, "cost_usd_spent": 0.0}
        for d in dates
    }

    # 2) Fast path: Redis (only the most recent ~8 days will hit).
    if rc:
        for d in dates:
            try:
                data = rc.hgetall(f"usage:{user_id}:{d}") or {}
            except Exception:
                data = {}
            if data:
                by_date[d] = {
                    "date":           d,
                    "tokens_used":    int(data.get("tokens_used", 0)),
                    "requests_made":  int(data.get("requests_made", 0)),
                    "cost_usd_spent": round(float(data.get("cost_usd_spent", 0.0)), 6),
                }

    # 3) Backfill dates still at zero (evicted from Redis / Redis down) from
    #    the durable Postgres model_usages table.
    missing = [
        d for d in dates
        if by_date[d]["tokens_used"] == 0
        and by_date[d]["requests_made"] == 0
        and by_date[d]["cost_usd_spent"] == 0.0
    ]
    if missing:
        try:
            conn = _pg()
            cur  = conn.cursor()
            cur.execute("""
                SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS d,
                       COALESCE(SUM(total_tokens), 0),
                       COUNT(*),
                       COALESCE(SUM(cost_usd), 0.0)
                FROM   model_usages
                WHERE  user_id::text = %s
                  AND  created_at >= %s::date
                GROUP  BY 1
            """, (user_id, dates[-1]))   # dates[-1] = oldest date in the range
            for row in cur.fetchall():
                d = row[0]
                if d in missing and d in by_date:
                    by_date[d] = {
                        "date":           d,
                        "tokens_used":    int(row[1]),
                        "requests_made":  int(row[2]),
                        "cost_usd_spent": round(float(row[3]), 6),
                    }
            cur.close(); conn.close()
        except Exception as e:
            logger.warning(f"BudgetStore.get_usage_history PG backfill({user_id}): {e}")

    # newest-first, identical shape/order to the previous implementation
    return [by_date[d] for d in dates]


def reset_usage(user_id: str) -> dict:
    """
    Admin: zero out a user's cumulative usage so they start fresh.
    Clears both Redis and Postgres. Returns old totals for audit trail.
    """
    old_totals = get_usage_total(user_id)

    # Clear Redis
    rc = _get_redis()
    if rc:
        try:
            rc.delete(f"usage:{user_id}:total")
        except Exception as e:
            logger.warning(f"BudgetStore.reset_usage Redis: {e}")

    # Clear Postgres
    try:
        conn = _pg()
        cur  = conn.cursor()
        cur.execute("""
            UPDATE user_usage_totals
               SET tokens_used = 0, requests_made = 0, cost_usd_spent = 0.0,
                   last_updated = NOW()
             WHERE user_id = %s
        """, (user_id,))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        logger.warning(f"BudgetStore.reset_usage PG: {e}")

    logger.info(f"BudgetStore: reset_usage user={user_id} cleared={old_totals}")
    return {"success": True, "user_id": user_id, "cleared": old_totals}


def update_cached_cost_limit(user_id: str, new_cost_usd_total: float,
                              new_base_cost_usd: Optional[float] = None,
                              new_extra_cost_usd: Optional[float] = None,
                              new_winner_extra_usd: Optional[float] = None,
                              new_winner_origin_period: Optional[str] = None,
                              clear_winner_origin_period: bool = False) -> None:
    """Refresh max_cost_usd_total (and optionally base/extra/winner) in the
    Redis budget:{uid} hash.

    Postgres budget_configs remains the source of truth; this keeps the
    Redis fast-path cache (read Redis-first by get_budget) in sync after a
    monthly reset or a base/extra mutation. Leaves token/request/per-day
    fields untouched. Best-effort: never raises. No-op if the hash does not
    already exist (get_budget will then miss and fall back to Postgres anyway).

    clear_winner_origin_period: HDELs the field, used when a reset drains the
    winner balance to zero. A plain None for new_winner_origin_period means
    "leave as-is"; there is no way to express "set to NULL" in a hash write,
    hence the explicit flag.
    """
    rc = _get_redis()
    if not rc:
        return
    try:
        if rc.exists(f"budget:{user_id}"):
            mapping = {"max_cost_usd_total": new_cost_usd_total}
            if new_base_cost_usd is not None:
                mapping["base_cost_usd"] = new_base_cost_usd
            if new_extra_cost_usd is not None:
                mapping["extra_cost_usd"] = new_extra_cost_usd
            if new_winner_extra_usd is not None:
                mapping["winner_extra_usd"] = new_winner_extra_usd
            if new_winner_origin_period is not None:
                mapping["winner_origin_period"] = new_winner_origin_period
            _redis_hset_mapping(rc, f"budget:{user_id}", mapping)
            if clear_winner_origin_period:
                try:
                    rc.hdel(f"budget:{user_id}", "winner_origin_period")
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"BudgetStore.update_cached_cost_limit({user_id}): {e}")


# ============================================================
# BUDGET CHECK
# ============================================================

# Platform defaults — applied to users with no explicit budget row.
# Tuneable via env vars:
#   BUDGET_DEFAULT_COST_USD  — total $ allocation (default $50)
#   BUDGET_DEFAULT_TOKENS    — total token allocation (default 500 000)
#   BUDGET_DEFAULT_REQUESTS  — total request allocation (default 1 000)
import os as _os
DEFAULT_COST_LIMIT_USD    = float(_os.getenv("BUDGET_DEFAULT_COST_USD",    "50.0"))
DEFAULT_TOKEN_LIMIT       = int(_os.getenv("BUDGET_DEFAULT_TOKENS",        "500000"))
DEFAULT_REQUEST_LIMIT     = int(_os.getenv("BUDGET_DEFAULT_REQUESTS",      "1000"))

# Backward-compat aliases (some gateway.py code imports these by old name)
DEFAULT_DAILY_COST_LIMIT_USD = DEFAULT_COST_LIMIT_USD
DEFAULT_DAILY_TOKEN_LIMIT    = DEFAULT_TOKEN_LIMIT
DEFAULT_DAILY_REQUEST_LIMIT  = DEFAULT_REQUEST_LIMIT


def check_budget(user_id: str) -> dict:
    """
    Returns dict with allowed=True/False and reason.

    Enforces ONLY the total cost budget (for paid/cloud models). Token and
    request limits — both cumulative and daily — are intentionally NOT checked.
    The sole gate is: does the user still have spendable budget?

    When BUDGET_ENFORCEMENT_ENABLED=false, always returns allowed=True.
    Useful for OSS users running Ollama locally (zero API cost).

    Note: local / in-house models carry no external API cost and are exempted
    upstream by the callers (middleware / gateway skip check_budget entirely for
    in-house model hints), so this function is only ever reached for paid models.

    Resilience chain:
      1. Redis usage total + Redis budget config (fast, no DB hit)
      2. Postgres usage total + Postgres budget config (Redis down)
      3. Fail-open if both unavailable (log error, never block platform)
    """
    from core.config import BUDGET_ENFORCEMENT_ENABLED
    if not BUDGET_ENFORCEMENT_ENABLED:
        return {"allowed": True, "reason": "budget enforcement disabled (BUDGET_ENFORCEMENT_ENABLED=false)"}

    try:
        budget = get_budget(user_id)   # Redis → Postgres
        usage  = get_usage_total(user_id)  # Redis → Postgres
    except Exception as e:
        logger.error(f"BudgetStore.check_budget: both Redis and Postgres failed for "
                     f"user={user_id} → fail-open. Error: {e}")
        return {"allowed": True, "reason": "budget-check-unavailable (fail-open)"}

    if not budget:
        # No explicit budget row — enforce only the platform default cost limit.
        if usage["cost_usd_spent"] >= DEFAULT_COST_LIMIT_USD:
            return {
                "allowed": False,
                "reason": (
                    f"Total spend limit reached "
                    f"(${usage['cost_usd_spent']:.2f} of ${DEFAULT_COST_LIMIT_USD:.2f} allocated) "
                    f"— contact your admin to top up your budget"
                ),
            }
        return {"allowed": True, "reason": "default allocation"}

    # Explicit budget row — enforce only the total cost allocation.
    max_cost = budget.get("max_cost_usd_total", DEFAULT_COST_LIMIT_USD)
    if max_cost and usage["cost_usd_spent"] >= max_cost:
        return {
            "allowed": False,
            "reason": (
                f"Total spend limit reached "
                f"(${usage['cost_usd_spent']:.2f} of ${max_cost:.2f} allocated) "
                f"— contact your admin to top up your budget"
            ),
        }

    return {"allowed": True, "reason": "ok"}


# ============================================================
# BUDGET INCREASE REQUESTS  (durable in ainxt.hod_allocation_ledger)
#
# A user's single "increase request" fans out to one ledger row PER HOD
# mapped to their department (a department can map to multiple HODs),
# all sharing one `request_id`. Only one row may ever resolve to
# 'approved' — the first HOD to act (approve or reject) wins:
#   - approve: winner's row -> 'approved'; every sibling -> 'superseded'.
#   - reject:  EVERY row (including the actor's) -> 'rejected'.
# Concurrency is guarded by SELECT ... FOR UPDATE across all sibling rows
# before any status transition, so a race between two HODs can only ever
# apply the increase once. See routers/budget_router.py for the
# authorization/notification layer built on top of these functions.
# ============================================================

import uuid as _uuid


def _current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def resolve_hod_for_request(requester_email: str) -> Optional[str]:
    """Resolve the HOD who should approve `requester_email`'s budget request.

    Resolved automatically from the requester's `users.department`, joined
    against the DBA/seed-managed `department_hod_mapping` table (one row per
    department -> hod_email). This requires zero manual per-user setup: as
    soon as `users.department` is populated (already kept current
    automatically by workers/ad_sync.py and the login-time org_tree/live-AD
    sync in services/user_directory_sync.py) and the department has a row in
    department_hod_mapping, every user in that department resolves to the
    correct HOD with no admin action needed.

    `users.hod_email` is intentionally NOT consulted here — it has no
    automatic write path anywhere in the codebase (not AD sync, not any
    admin UI/API), so relying on it required a manual, per-user assignment
    that nobody could actually perform. department_hod_mapping is the
    platform's real HOD source of truth (also used by
    routers/governance_router.py, services/sdlc_budget_tracker.py, etc.).

    Returns None if requester_email is missing, the user is not found, the
    user has no department set, or the department has no HOD mapped yet —
    caller must treat this as a blocking error.
    """
    if not requester_email:
        return None
    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _text
        db = SessionLocal()
        try:
            # department_name match is case-sensitive exact (mirrors the
            # convention documented on db.models.DepartmentHodMapping and
            # used by routers/governance_router.py's HOD resolution);
            # hod_email/email matches stay case-insensitive.
            row = db.execute(
                _text(
                    'SELECT dhm."hod_email" '
                    "FROM users u "
                    'JOIN department_hod_mapping dhm '
                    '    ON dhm."department_name" = u.department '
                    "WHERE lower(u.email) = lower(:email) "
                    "AND u.department IS NOT NULL AND u.department <> '' "
                    'AND dhm."hod_email" IS NOT NULL AND dhm."hod_email" <> \'\' '
                    "LIMIT 1"
                ),
                {"email": requester_email},
            ).fetchone()
        finally:
            db.close()
        if not row:
            return None
        return (row[0] or "").strip() or None
    except Exception as e:
        logger.warning(f"BudgetStore.resolve_hod_for_request({requester_email}): {e}")
        return None


def _parse_delegated_to(raw: Optional[str]) -> List[str]:
    """Split a `department_hod_mapping.delegated_to` cell into a normalised
    list of lowercased, de-duplicated delegatee emails. Accepts either a
    comma- or semicolon-separated string (comma is the write format; semicolon
    is tolerated for legacy hand-edits). Empty / whitespace-only entries are
    dropped. Returns an empty list on NULL / empty input.
    """
    if not raw:
        return []
    parts = [p.strip().lower() for p in raw.replace(";", ",").split(",")]
    seen: set = set()
    out: List[str] = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def resolve_delegates_for_hod(hod_email: str) -> List[str]:
    """Return the list of delegatee emails an HOD has nominated. Delegation is
    per-HOD (not per-department): every department_hod_mapping row belonging
    to this HOD stores the same delegated_to string. We read them all and
    return the union so a stray hand-edit or partial update on one row never
    silently drops a delegatee, and de-dupe the result.

    Emails are lowercased. Never raises; returns [] on any DB failure so a
    routing lookup can't be blocked by a stray SELECT error (the caller falls
    back to the HOD alone).
    """
    if not hod_email:
        return []
    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _text
        db = SessionLocal()
        try:
            rows = db.execute(
                _text(
                    'SELECT "delegated_to" FROM ainxt.department_hod_mapping '
                    'WHERE lower("hod_email") = lower(:email)'
                ),
                {"email": hod_email},
            ).fetchall()
        finally:
            db.close()
        seen: set = set()
        out: List[str] = []
        for r in rows:
            for e in _parse_delegated_to(r[0] if r else None):
                if e not in seen:
                    seen.add(e)
                    out.append(e)
        return out
    except Exception as e:
        logger.warning(f"BudgetStore.resolve_delegates_for_hod({hod_email}): {e}")
        return []


def resolve_delegating_hods_for(email: str) -> List[str]:
    """Inverse lookup: given `email`, return every HOD who has nominated
    this address as a budget-approval delegatee. Used by the router to
    gate the delegatee-only endpoints — an empty result means the caller
    is NOT a delegatee for anyone and must not see any HOD-scoped budget
    surface.

    Emails are returned lowercased and de-duplicated (an HOD can appear
    on multiple department rows, but delegation is per-HOD so we merge).
    Never raises; returns [] on any DB failure.
    """
    if not email:
        return []
    e_lc = email.strip().lower()
    if not e_lc:
        return []
    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _text
        db = SessionLocal()
        try:
            # LIKE with lower() to catch the email as a comma-list entry.
            # We over-select here (may pick up a false positive when one
            # delegatee email is a substring of another) and filter in
            # Python via _parse_delegated_to to be exact.
            like_pat = f"%{e_lc}%"
            rows = db.execute(
                _text(
                    'SELECT DISTINCT lower("hod_email") AS hod, "delegated_to" '
                    'FROM ainxt.department_hod_mapping '
                    'WHERE "delegated_to" IS NOT NULL '
                    '  AND lower("delegated_to") LIKE :pat'
                ),
                {"pat": like_pat},
            ).fetchall()
        finally:
            db.close()
        seen: set = set()
        out: List[str] = []
        for hod, deleg in rows:
            if not hod or hod in seen:
                continue
            if e_lc in _parse_delegated_to(deleg):
                seen.add(hod)
                out.append(hod)
        return out
    except Exception as e:
        logger.warning(f"BudgetStore.resolve_delegating_hods_for({email}): {e}")
        return []


def resolve_approvers_for_request(requester_email: str) -> dict:
    """Resolve the full approver set for `requester_email`'s budget request:
    the HOD (from users.hod_email) plus every delegatee the HOD has nominated.

    A delegatee who is also the requester is filtered out here — a user must
    never be able to approve their own request. If that filter empties the
    delegatee list, the request routes to the HOD alone.

    Returns:
        {
          "hod_email":   str  | None,  # None if the requester has no HOD
          "delegatees":  list[str],    # possibly empty
        }
    """
    if not requester_email:
        return {"hod_email": None, "delegatees": []}

    hod_email = resolve_hod_for_request(requester_email)
    if not hod_email:
        return {"hod_email": None, "delegatees": []}

    delegatees = resolve_delegates_for_hod(hod_email)
    # Never route a request back to the requester themselves.
    delegatees = [d for d in delegatees if d.lower() != requester_email.lower()]
    return {"hod_email": hod_email, "delegatees": delegatees}


def set_hod_delegates(hod_email: str,
                      delegatee_emails: List[str]) -> dict:
    """Overwrite the HOD's delegatee list across EVERY department_hod_mapping
    row for this HOD. Delegation is per-HOD, not per-department — writing the
    same list to all rows keeps resolve_delegates_for_hod stable regardless
    of which department a requester belongs to, and matches how the HOD's
    single "Delegation" UI slot works. Stored as a comma-separated string of
    lowercased emails (empty string when the list is empty).

    Returns {"delegatees": [...], "rows_updated": int}. Raises ValueError if
    the HOD has no rows in department_hod_mapping at all — a non-HOD cannot
    invent a delegation slot for themselves.
    """
    if not hod_email:
        raise ValueError("hod_email is required")

    normalised: List[str] = []
    seen: set = set()
    for e in delegatee_emails or []:
        e2 = (e or "").strip().lower()
        if e2 and e2 not in seen and e2 != hod_email.strip().lower():
            seen.add(e2)
            normalised.append(e2)
    serialised = ",".join(normalised) if normalised else ""

    try:
        conn = _pg()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE ainxt.department_hod_mapping
               SET delegated_to = %s
             WHERE lower(hod_email) = lower(%s)
            """,
            (serialised, hod_email),
        )
        updated = cur.rowcount
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        logger.error(f"BudgetStore.set_hod_delegates({hod_email}): {e}")
        raise

    if not updated:
        raise ValueError(
            f"No department_hod_mapping rows found for HOD {hod_email!r} — "
            "cannot set delegates."
        )

    return {"delegatees": normalised, "rows_updated": int(updated)}


def resolve_direct_report_emails(hod_email: str) -> List[dict]:
    """Return the HOD's direct reports as [{"email": ..., "name": ...}, ...],
    used to populate the delegation multi-select dropdown.

    org_tree.direct_reports stores a comma/semicolon-separated list of
    display-name (or DN) strings, NOT emails — resolving to an actual email
    requires matching each entry against another org_tree row's node_id
    (primary) or display_name (fallback), per the AD export format. Entries
    that cannot be resolved to any org_tree row are silently dropped — the
    HOD can only delegate to someone with a known AiNxt account/email.

    Returns [] if hod_email has no org_tree row, has no direct_reports, or
    on any DB error — never raises, so a broken lookup can't break the
    Team page.
    """
    if not hod_email:
        return []
    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _text
        db = SessionLocal()
        try:
            hod_row = db.execute(
                _text(
                    "SELECT direct_reports FROM org_tree "
                    "WHERE lower(mail) = lower(:email) LIMIT 1"
                ),
                {"email": hod_email},
            ).fetchone()
            raw_reports = (hod_row[0] if hod_row else None) or ""
            # direct_reports uses ", " as the AD-export separator (names may
            # contain semicolons in rare cases) — reuse the same comma/
            # semicolon-tolerant splitter used for delegated_to, but without
            # lowercasing (names are case-sensitive for the node_id match).
            entries = [
                p.strip() for p in raw_reports.replace(";", ",").split(",")
                if p.strip()
            ]
            if not entries:
                return []

            out: List[dict] = []
            for entry in entries:
                row = db.execute(
                    _text(
                        "SELECT mail, display_name FROM org_tree "
                        "WHERE node_id = :entry OR display_name = :entry "
                        "LIMIT 1"
                    ),
                    {"entry": entry},
                ).fetchone()
                if row and row[0]:
                    out.append({"email": row[0].lower(), "name": row[1] or row[0]})
            # De-dupe by email (a direct report could theoretically appear
            # twice in a malformed CSV import).
            seen: set = set()
            deduped: List[dict] = []
            for r in out:
                if r["email"] not in seen:
                    seen.add(r["email"])
                    deduped.append(r)
            return deduped
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"BudgetStore.resolve_direct_report_emails({hod_email}): {e}")
        return []


def request_budget_increase(user_id: str, requested_extra_cost_usd: float,
                             justification: str,
                             requester_email: str = "",
                             requester_name: str = "",
                             requester_department: str = "",
                             hod_emails: Optional[List[str]] = None,
                             delegatee_emails: Optional[List[str]] = None,
                             is_winner_grant: bool = False) -> dict:
    """
    User requests an extra-budget increase (added on top of base once
    approved). Fans out one 'pending' row per approver into
    ainxt.hod_allocation_ledger, all sharing one request_id.

    Approver fan-out layout:
      - One row per HOD in `hod_emails`, with delegated_to = NULL.
      - Plus one row per email in `delegatee_emails`, with
        hod_email = hod_emails[0] and delegated_to = <delegatee email>.
      Every row shares the same request_id, so the existing row-lock in
      approve_budget_request still guarantees only one approver wins.
      hod_email on delegatee rows stays the ORIGINAL HOD, so the cap
      charge on approve always lands on the HOD's cap regardless of
      which approver acted.

    is_winner_grant=True: the row is inserted with action='winner_approve_request'
    instead of 'approve_request'. The approve endpoint detects this and skips
    the HOD monthly cap check — 10x winner grants are admin-nominated awards
    and must NOT be charged against the HOD's allocation cap.

    Raises ValueError if requested_extra_cost_usd is invalid, or if
    hod_emails is empty (no department→HOD mapping — caller must resolve
    and pass this in; this function does not silently fall back to admin).
    """
    if requested_extra_cost_usd <= 0:
        raise ValueError("requested_extra_cost_usd must be > 0")
    if requested_extra_cost_usd > _STORE_MAX_COST_USD_PER_DAY:
        raise ValueError(
            f"requested_extra_cost_usd exceeds ceiling ${_STORE_MAX_COST_USD_PER_DAY:,.2f}"
        )
    if not justification or not justification.strip():
        raise ValueError("justification is required")
    if not hod_emails:
        raise ValueError("no HOD mapping found for this department — request cannot be routed")

    # Normalise and de-dupe the approver set. Delegatees who happen to also
    # be the requester are dropped upstream in resolve_approvers_for_request,
    # but defend in depth here too — a self-approval slot must never be
    # created no matter which caller invoked this.
    requester_lc = (requester_email or "").strip().lower()
    hod_list: List[str] = []
    seen: set = set()
    for e in hod_emails:
        e2 = (e or "").strip().lower()
        if e2 and e2 not in seen:
            seen.add(e2)
            hod_list.append(e2)

    delegatee_list: List[str] = []
    for e in (delegatee_emails or []):
        e2 = (e or "").strip().lower()
        if not e2 or e2 in seen:
            continue
        if requester_lc and e2 == requester_lc:
            # Self-approval guard: a delegatee who is the requester never
            # gets their own approver row. Other delegatees are still routed.
            continue
        seen.add(e2)
        delegatee_list.append(e2)

    # The delegatee rows all carry hod_email = the primary HOD so a
    # delegatee approval charges the HOD's cap, not the delegatee's.
    primary_hod = hod_list[0]

    # Winner grants use a distinct action so approve_budget_request can skip
    # the HOD cap check for them without touching any other code path.
    ledger_action = "winner_approve_request" if is_winner_grant else "approve_request"

    existing = get_budget(user_id) or {}
    current_base  = float(existing.get("base_cost_usd", 50.0))
    current_extra = float(existing.get("extra_cost_usd", 0.0))

    period = _current_period()

    # Prevent duplicate pending requests from the same user in the same period.
    # Check both regular and winner actions so a winner request also blocks a
    # duplicate winner request for the same period.
    try:
        conn = _pg()
        cur = conn.cursor()
        cur.execute("""
            SELECT request_id, justification
            FROM   ainxt.hod_allocation_ledger
            WHERE  target_user_id = %s
              AND  action = %s
              AND  status = 'pending'
              AND  period_yyyymm = %s
            LIMIT  1
        """, (user_id, ledger_action, period))
        pending_row = cur.fetchone()
        cur.close(); conn.close()
        if pending_row:
            raise ValueError(
                "You already have a pending budget increase request. "
                f"Wait for it to be approved or rejected before requesting again."
            )
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"BudgetStore.request_budget_increase pending check failed: {e}")
        # Don't block the request if the check itself fails; continue to insert.

    request_id = str(_uuid.uuid4())

    # Build the fan-out: (hod_email_on_row, delegated_to_or_None) pairs.
    approver_rows: List[tuple] = [(h, None) for h in hod_list]
    for d in delegatee_list:
        approver_rows.append((primary_hod, d))

    try:
        conn = _pg()
        cur  = conn.cursor()
        for row_hod, row_delegatee in approver_rows:
            cur.execute("""
                INSERT INTO ainxt.hod_allocation_ledger (
                    id, hod_email, period_yyyymm, target_user_id, target_user_email,
                    action, amount_usd, previous_limit_usd, new_limit_usd,
                    request_id, cap_at_time_usd, consumed_after_usd, shadow_mode,
                    justification, status, requested_extra_cost_usd,
                    requester_email, requester_name, requester_department,
                    current_base_cost_usd, current_extra_cost_usd, delegated_to,
                    created_at
                ) VALUES (
                    gen_random_uuid(), %s, %s, %s, %s,
                    %s, NULL, NULL, NULL,
                    %s, NULL, NULL, FALSE,
                    %s, 'pending', %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    NOW()
                )
            """, (
                row_hod, period, user_id, requester_email or None,
                ledger_action,
                request_id,
                justification.strip(), requested_extra_cost_usd,
                requester_email, requester_name, requester_department,
                current_base, current_extra, row_delegatee,
            ))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        logger.error(f"BudgetStore.request_budget_increase insert failed: {e}")
        raise

    return {
        "id":                        request_id,
        "request_id":                request_id,
        "user_id":                   user_id,
        "requested_extra_cost_usd":  requested_extra_cost_usd,
        "justification":             justification.strip(),
        "hod_emails":                hod_list,
        "delegatee_emails":          delegatee_list,
        "current_base_cost_usd":     current_base,
        "current_extra_cost_usd":    current_extra,
        "status":                    "pending",
        "created_at":                time.time(),
    }


def _row_to_request_dict(row, cols) -> dict:
    d = dict(zip(cols, row))
    for k in ("amount_usd", "previous_limit_usd", "new_limit_usd", "cap_at_time_usd",
              "consumed_after_usd", "requested_extra_cost_usd",
              "current_base_cost_usd", "current_extra_cost_usd"):
        if d.get(k) is not None:
            d[k] = float(d[k])
    d["target_user_id"] = str(d.get("target_user_id") or "")
    d["created_at"]  = d["created_at"].timestamp()  if d.get("created_at")  else 0
    d["resolved_at"] = d["resolved_at"].isoformat() if d.get("resolved_at") else None
    # Back-compat aliases used by older UI/consumers.
    d["user_id"] = d.get("target_user_id", "")
    d["reason"]  = d.get("justification") or ""
    if d.get("requested_extra_cost_usd") is not None:
        d["requested_cost_usd"] = d["requested_extra_cost_usd"]
    return d


_REQUEST_COLUMNS = (
    "id", "request_id", "hod_email", "status", "target_user_id",
    "requester_email", "requester_name", "requester_department",
    "requested_extra_cost_usd", "justification",
    "current_base_cost_usd", "current_extra_cost_usd",
    "amount_usd", "previous_limit_usd", "new_limit_usd",
    "created_at", "resolved_at", "approved_by", "approved_by_name",
    "delegated_to",
)


def get_pending_budget_requests(hod_email: Optional[str] = None,
                                 include_approved: bool = False,
                                 target_user_id: Optional[str] = None,
                                 approver_email: Optional[str] = None) -> List[dict]:
    """
    List budget-increase requests from ainxt.hod_allocation_ledger.

    hod_email:        if given, scoped to that HOD's own rows only
                       (status='pending' — their actionable queue).
    include_approved:  when True (admin view), also include 'approved' and
                       'rejected' rows, deduplicated by request_id (one line
                       item per request, not one per fanned-out HOD row).
    target_user_id:    if given, scoped to that requester's own rows only
                       (used by the "My Budget" screen so a regular user can
                       see their own pending request — status='pending' only,
                       since approved/rejected outcomes are already surfaced
                       via the inbox and the my-increases history table).
    approver_email:    if given, scoped to rows this email can act on — either
                       as the HOD (hod_email match with delegated_to IS NULL)
                       or as a delegatee (delegated_to match). Used by the
                       Team → Pending Requests view so both HODs and their
                       delegatees see the same actionable queue.
    """
    try:
        conn = _pg()
        cur  = conn.cursor()
        cols_sql = ", ".join(_REQUEST_COLUMNS)
        statuses = ["pending", "approved", "rejected"] if include_approved else ["pending"]
        where = ["action IN ('approve_request', 'winner_approve_request')", "status = ANY(%s)"]
        params: list = [statuses]
        if hod_email:
            where.append("lower(hod_email) = %s")
            params.append(hod_email.strip().lower())
        if approver_email:
            where.append(
                "((lower(hod_email) = %s AND delegated_to IS NULL) "
                "OR lower(delegated_to) = %s)"
            )
            _e = approver_email.strip().lower()
            params.append(_e)
            params.append(_e)
        if target_user_id:
            where.append("target_user_id = %s")
            params.append(target_user_id)
        cur.execute(f"""
            SELECT {cols_sql}
            FROM   ainxt.hod_allocation_ledger
            WHERE  {' AND '.join(where)}
            ORDER  BY created_at DESC
        """, params)
        rows = cur.fetchall()
        cur.close(); conn.close()
        result = [_row_to_request_dict(r, _REQUEST_COLUMNS) for r in rows]
    except Exception as e:
        logger.warning(f"BudgetStore.get_pending_budget_requests: {e}")
        return []

    # Dedupe fan-out rows by request_id. A single request now produces N+1
    # ledger rows (HOD + one per delegatee), so within the same status class
    # we still need to pick a canonical representative — prefer the HOD's own
    # row (delegated_to IS NULL) so the returned hod_email is the ORIGINAL
    # HOD, not one of the delegatees. Status precedence is unchanged: a
    # resolved (approved/rejected) row beats any still-pending sibling; if
    # both approved and rejected rows exist for a group, approved wins.
    def _prefers(new_row: dict, existing: dict) -> bool:
        old_s, new_s = existing["status"], new_row["status"]
        if old_s == "pending" and new_s in ("approved", "rejected"):
            return True
        if old_s == "rejected" and new_s == "approved":
            return True
        if old_s == new_s:
            # Same status: prefer the HOD's own row (delegated_to empty).
            old_del = (existing.get("delegated_to") or "")
            new_del = (new_row.get("delegated_to") or "")
            return bool(old_del) and not new_del
        return False

    by_request: dict = {}
    for r in result:
        rid = r["request_id"]
        existing = by_request.get(rid)
        if existing is None or _prefers(r, existing):
            by_request[rid] = r

    deduped = sorted(by_request.values(), key=lambda x: x["created_at"], reverse=True)

    # Attach the full delegatee list for each request so the UI can display
    # "Routed to <HOD> + delegatees: <email>, <email>" regardless of which
    # scoping filter reduced the raw result set above.
    try:
        rids = [r["request_id"] for r in deduped if r.get("request_id")]
        if rids:
            conn2 = _pg()
            cur2 = conn2.cursor()
            cur2.execute("""
                SELECT request_id, delegated_to
                FROM   ainxt.hod_allocation_ledger
                WHERE  request_id = ANY(%s)
                  AND  delegated_to IS NOT NULL
            """, (rids,))
            grouped: dict = {}
            for req_id, deleg in cur2.fetchall():
                if deleg:
                    grouped.setdefault(req_id, []).append(str(deleg).lower())
            cur2.close(); conn2.close()
            for r in deduped:
                r["delegatees"] = sorted(set(grouped.get(r.get("request_id"), [])))
    except Exception as e:
        logger.warning(f"BudgetStore.get_pending_budget_requests delegatee enrichment failed: {e}")
        for r in deduped:
            r.setdefault("delegatees", [])

    # Attach current utilisation for each unique requester.
    try:
        user_ids = list({r["target_user_id"] for r in deduped})
        usage_by_user = {uid: get_usage_total(uid) for uid in user_ids}
        for r in deduped:
            r["usage_total"] = usage_by_user.get(r["target_user_id"], {"cost_usd_spent": 0.0})
    except Exception as e:
        logger.warning(f"BudgetStore.get_pending_budget_requests usage enrichment failed: {e}")

    return deduped


def get_request_group(request_id: str) -> List[dict]:
    """Return all ledger rows (one per fanned-out HOD) sharing this request_id."""
    try:
        conn = _pg()
        cur  = conn.cursor()
        cols_sql = ", ".join(_REQUEST_COLUMNS)
        cur.execute(f"""
            SELECT {cols_sql}
            FROM   ainxt.hod_allocation_ledger
            WHERE  request_id = %s
              AND  action IN ('approve_request', 'winner_approve_request')
            ORDER  BY hod_email
        """, (request_id,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [_row_to_request_dict(r, _REQUEST_COLUMNS) for r in rows]
    except Exception as e:
        logger.warning(f"BudgetStore.get_request_group({request_id}): {e}")
        return []


def approve_budget_request(request_id: str, acting_hod_email: str,
                            acting_hod_name: str = "",
                            check_hod_cap: bool = True,
                            is_hod_actor: bool = True) -> dict:
    """
    Approve one row within a (possibly multi-HOD) request group.

    Locks every row sharing request_id (SELECT ... FOR UPDATE) so a
    concurrent approval/rejection by another HOD cannot race past this one.
    On success: the resolved row -> 'approved' (with amount_usd/
    previous_limit_usd/new_limit_usd/resolved_at set), every sibling row ->
    'superseded'. The user's extra_cost_usd is incremented by the requested
    amount; base_cost_usd is left untouched.

    Atomicity: the ledger status transition, the HOD cap charge, AND the
    budget_configs UPDATE (extra_cost_usd += requested, max_cost_usd_total
    = base + new_extra) all happen inside ONE Postgres transaction. If any
    step fails, everything rolls back — the request stays 'pending' and no
    cap is charged. Redis cache sync runs post-commit as a best-effort
    (a Redis failure will NOT undo the DB commit — the cache re-populates
    on the next get_budget() read/miss).

    is_hod_actor: kept for backward-compat with older call sites, but the
    router now only ever invokes this with True (only routed HODs may
    approve; admins have read-only visibility). When False, the historical
    "admin override" path resolves the first pending row and skips the
    HOD cap check — retained defensively so an accidental False cannot
    over-charge a random HOD's cap.
    """
    acting_email_lc = (acting_hod_email or "").strip().lower()
    if not acting_email_lc:
        return {"success": False, "error": "acting_hod_email is required"}
    if not is_hod_actor:
        check_hod_cap = False

    try:
        conn = _pg()
        conn.autocommit = False
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT id, hod_email, status, target_user_id, requested_extra_cost_usd,
                       requester_email, requester_name, action, period_yyyymm,
                       delegated_to
                FROM   ainxt.hod_allocation_ledger
                WHERE  request_id = %s
                  AND  action IN ('approve_request', 'winner_approve_request')
                ORDER  BY hod_email
                FOR UPDATE
            """, (request_id,))
            rows = cur.fetchall()
            if not rows:
                conn.rollback(); cur.close(); conn.close()
                return {"success": False, "error": "Request not found"}

            # Self-approval guard (defence in depth — also enforced at fan-out
            # time by resolve_approvers_for_request / request_budget_increase).
            # A delegatee who is also the requester must never be able to
            # approve their own request even if a stale row ended up in the DB.
            requester_lc = (rows[0][5] or "").strip().lower()
            if requester_lc and acting_email_lc == requester_lc:
                conn.rollback(); cur.close(); conn.close()
                return {
                    "success": False,
                    "error":   "You cannot approve your own budget-increase request.",
                }

            # Detect winner grants — skip HOD cap check for these rows since
            # the $1,000 award is admin-nominated and must not count against
            # the HOD's monthly allocation cap.
            is_winner_row = any(r[7] == "winner_approve_request" for r in rows)
            new_winner_extra = 0.0   # set properly below when is_winner_row=True
            if is_winner_row:
                check_hod_cap = False

            if is_hod_actor:
                # Delegation: a caller may be authorised on a
                # row either as the HOD (hod_email match, delegated_to NULL)
                # or as a delegatee (delegated_to match). Both are valid
                # approvers; the cap charge always uses the row's hod_email,
                # not the acting email.
                mine = next(
                    (r for r in rows
                     if (r[1] or "").lower() == acting_email_lc
                         and (r[9] or "") == ""),
                    None,
                )
                if mine is None:
                    mine = next(
                        (r for r in rows
                         if (r[9] or "").lower() == acting_email_lc),
                        None,
                    )
                if mine is None:
                    conn.rollback(); cur.close(); conn.close()
                    return {"success": False, "error": "This request was not routed to you"}
            else:
                # Admin/senior-approver override: resolve the first pending row.
                mine = next((r for r in rows if r[2] == "pending"), rows[0])

            already_resolved = next((r for r in rows if r[2] != "pending"), None)
            if already_resolved is not None:
                # Prefer showing the acting approver (approved_by) rather than
                # the row's hod_email when a delegatee resolved it, since
                # hod_email on a delegatee row is still the ORIGINAL HOD.
                # Fall back to hod_email if approved_by isn't populated.
                cur.execute(
                    "SELECT approved_by FROM ainxt.hod_allocation_ledger WHERE id = %s",
                    (already_resolved[0],),
                )
                _approved_by_row = cur.fetchone()
                actor = (_approved_by_row and _approved_by_row[0]) or already_resolved[1]
                verb  = "approved" if already_resolved[2] == "approved" else already_resolved[2]
                conn.rollback(); cur.close(); conn.close()
                return {"success": False, "error": f"This request was already {verb} by {actor}"}

            (_row_id, _hod, _status, target_user_id, requested_extra,
             req_email, req_name, _action, _period_yyyymm, _delegated_to) = mine
            # The cap charge always lands on the row's hod_email (the ORIGINAL
            # HOD), never on the acting delegatee — a delegatee approves on
            # the HOD's behalf and spends from the HOD's monthly allocation.
            #
            # `_hod` came out of the SELECT above and is deliberately NOT passed
            # to the cap check: that call re-resolves the charged HOD
            # in-database from the request_id, so a value read from the DB is
            # never carried back into a subsequent query. See
            # services.hod_budget_governor.check_and_reserve_cap_for_request.
            requested_extra = float(requested_extra or 0.0)
            # period_yyyymm is the award month stored at request-creation time.
            # Used below to stamp winner_origin_period on winner grants.
            winner_period = (_period_yyyymm or "").strip() or None

            cur.execute("""
                SELECT base_cost_usd, extra_cost_usd, max_cost_usd_total,
                       max_tokens_total, max_requests_total, winner_extra_usd
                FROM   ainxt.budget_configs
                WHERE  user_id = %s
                FOR UPDATE
            """, (str(target_user_id),))
            cfg_row = cur.fetchone()

            if cfg_row is not None:
                old_base   = float(cfg_row[0]) if cfg_row[0] is not None else 50.0
                old_extra  = float(cfg_row[1]) if cfg_row[1] is not None else 0.0
                cur_tokens = int(cfg_row[3])   if cfg_row[3] is not None else 500_000
                cur_reqs   = int(cfg_row[4])   if cfg_row[4] is not None else 1_000
                # Read-only here: an HOD top-up raises the POOLED extra only.
                # winner_extra_usd is never written by this path, so the HOD
                # money lands in the (extra - winner_extra) remainder and is
                # the first thing drained at the monthly reset. Carried so the
                # post-commit Redis sync below doesn't drop the field.
                cur_winner_extra = float(cfg_row[5]) if cfg_row[5] is not None else 0.0
            else:
                # No budget_configs row yet — happens for users who've never
                # been auto-seeded via /budget/me. Fall back to the same
                # defaults set_budget() would have applied and INSERT below.
                old_base, old_extra = 50.0, 0.0
                cur_tokens, cur_reqs = 500_000, 1_000
                cur_winner_extra = 0.0

            new_extra = old_extra + requested_extra
            new_total = old_base + new_extra

            # HOD monthly cap check — inside the same transaction/lock so a
            # 409 here rolls back and leaves the request pending, unchanged.
            #
            # check_and_reserve_cap returns the projected cap state AFTER
            # charging `requested_extra`:
            #   consumed_usd  = previously-approved consumption + this amount
            #   cap_usd       = the HOD's cap at charge time
            # We MUST persist these onto the row being approved, otherwise the
            # approved amount never lands in consumed_after_usd and the HOD's
            # "Consumed"/"Remaining" (which read MAX(consumed_after_usd)) never
            # move — the whole point of charging the request against the cap.
            cap_at_time = None
            consumed_after = None
            if check_hod_cap:
                from services.hod_budget_governor import check_and_reserve_cap_for_request
                # The charge still lands on the row's ORIGINAL HOD, never on
                # acting_email_lc — a delegatee spends from the HOD's monthly
                # allocation, not their own. The difference is HOW that HOD is
                # identified: it is resolved in-database from request_id inside
                # the cap statements, instead of being read into Python here and
                # passed back down. Same target, same money, one fewer hop for
                # database-sourced data.
                try:
                    cap_state = check_and_reserve_cap_for_request(
                        cur, request_id, acting_email_lc, requested_extra,
                    )   # raises HTTPException(409) on overrun
                except LookupError:
                    # The authorisation subquery found no actionable row. The
                    # Python-side checks above already proved one exists, so
                    # this means the row changed under us — fail closed.
                    conn.rollback(); cur.close(); conn.close()
                    logger.error(
                        "approve_budget_request(%s): cap resolution found no "
                        "actionable row for actor=%s", request_id, acting_email_lc,
                    )
                    return {"success": False, "error": "This request was not routed to you"}
                cap_at_time    = cap_state.get("cap_usd")
                consumed_after = cap_state.get("consumed_usd")

            # Mark the winning row approved. Stamp cap_at_time_usd /
            # consumed_after_usd so the running-total consumption query picks
            # this charge up (they were NULL while pending).
            cur.execute("""
                UPDATE ainxt.hod_allocation_ledger
                   SET status = 'approved',
                       amount_usd = %s,
                       previous_limit_usd = %s,
                       new_limit_usd = %s,
                       cap_at_time_usd = %s,
                       consumed_after_usd = %s,
                       resolved_at = NOW(),
                       approved_by = %s,
                       approved_by_name = %s
                 WHERE id = %s
            """, (requested_extra, old_base + old_extra, new_total,
                  cap_at_time, consumed_after,
                  acting_email_lc, acting_hod_name or None, _row_id))

            # Supersede every sibling row for the same request_id.
            sibling_ids = [r[0] for r in rows if r[0] != _row_id]
            if sibling_ids:
                cur.execute("""
                    UPDATE ainxt.hod_allocation_ledger
                       SET status = 'superseded', resolved_at = NOW()
                     WHERE id = ANY(%s::uuid[])
                """, (sibling_ids,))

            # Apply the increase to budget_configs in the SAME transaction
            # (fix for the previous non-atomic tail): ledger + wallet either
            # both commit or both roll back. If we ever fail below, both the
            # 'approved' status transition and any HOD-cap charge get undone
            # by conn.rollback() in the except branch.
            # For winner grants, compute the new winner slice and stamp both
            # winner_extra_usd and winner_origin_period. COALESCE keeps the
            # existing value for regular HOD approvals (non-winner path).
            new_winner_extra = (cur_winner_extra + requested_extra) if is_winner_row else cur_winner_extra

            if cfg_row is not None:
                cur.execute("""
                    UPDATE ainxt.budget_configs
                       SET base_cost_usd      = %s,
                           extra_cost_usd     = %s,
                           max_cost_usd_total = %s,
                           monthly_limit_usd  = %s,
                           winner_extra_usd     = CASE WHEN %s THEN %s ELSE winner_extra_usd END,
                           winner_origin_period = CASE WHEN %s THEN %s ELSE winner_origin_period END,
                           updated_at         = NOW()
                     WHERE user_id = %s
                """, (old_base, new_extra, new_total, new_total,
                      is_winner_row, new_winner_extra,
                      is_winner_row, winner_period,
                      str(target_user_id)))
            else:
                cur.execute("""
                    INSERT INTO ainxt.budget_configs
                        (id, user_id, monthly_limit_usd, max_tokens_total,
                         max_requests_total, max_cost_usd_total,
                         model_allowlist, base_cost_usd, extra_cost_usd,
                         winner_extra_usd, winner_origin_period,
                         created_at, updated_at)
                    VALUES (gen_random_uuid(), %s, %s, %s, %s, %s,
                            '[]'::jsonb, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (user_id) DO UPDATE
                       SET base_cost_usd      = EXCLUDED.base_cost_usd,
                           extra_cost_usd     = EXCLUDED.extra_cost_usd,
                           max_cost_usd_total = EXCLUDED.max_cost_usd_total,
                           monthly_limit_usd  = EXCLUDED.monthly_limit_usd,
                           winner_extra_usd   = CASE WHEN %s THEN EXCLUDED.winner_extra_usd ELSE budget_configs.winner_extra_usd END,
                           winner_origin_period = CASE WHEN %s THEN EXCLUDED.winner_origin_period ELSE budget_configs.winner_origin_period END,
                           updated_at         = NOW()
                """, (str(target_user_id), new_total, cur_tokens, cur_reqs,
                      new_total, old_base, new_extra,
                      new_winner_extra, winner_period,
                      is_winner_row, is_winner_row))

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close(); conn.close()
    except Exception as e:
        # Let HTTPException (e.g. 409 cap overrun from check_and_reserve_cap)
        # propagate as-is so the router returns the correct status code —
        # the transaction has already been rolled back above, leaving the
        # request untouched/still pending.
        from fastapi import HTTPException as _HTTPException
        if isinstance(e, _HTTPException):
            raise
        logger.error(f"BudgetStore.approve_budget_request({request_id}): {e}")
        return {"success": False, "error": "Approval failed"}

    # ── Post-commit Redis sync (best-effort) ─────────────────────────────
    # The authoritative write to Postgres has already committed above. Now
    # keep the Redis fast-path cache aligned so get_budget()/check_budget()
    # don't serve stale limits until the hash naturally expires. This is
    # best-effort — a Redis failure here does NOT undo the DB commit; the
    # cache will re-populate on the next miss/read.
    rc = _get_redis()
    if rc is not None:
        try:
            redis_mapping = {
                "max_tokens_total":   cur_tokens,
                "max_requests_total": cur_reqs,
                "max_cost_usd_total": new_total,
                "base_cost_usd":      old_base,
                "extra_cost_usd":     new_extra,
                "winner_extra_usd":   new_winner_extra if is_winner_row else cur_winner_extra,
            }
            # Stamp winner_origin_period in Redis for winner grants so
            # get_budget() returns it immediately without a DB round-trip.
            if is_winner_row and winner_period:
                redis_mapping["winner_origin_period"] = winner_period
            _redis_hset_mapping(rc, f"budget:{target_user_id}", redis_mapping)
            rc.sadd("budget:users:index", str(target_user_id))
        except Exception as e:
            logger.warning(f"BudgetStore.approve_budget_request Redis sync failed for user={target_user_id}: {e}")

    return {
        "success":            True,
        "request_id":         request_id,
        "user_id":            str(target_user_id),
        "approved_by":        acting_email_lc,
        "approved_by_name":   acting_hod_name,
        "requested_extra_usd": requested_extra,
        "new_base_cost_usd":  old_base,
        "new_extra_cost_usd": new_extra,
        "new_cost_usd":       new_total,
        # back-compat fields for existing callers that read new_tokens/new_requests
        "new_tokens":         cur_tokens,
        "new_requests":       cur_reqs,
        # winner fields — populated only for winner_approve_request rows
        "is_winner_grant":      is_winner_row,
        "winner_extra_usd":     new_winner_extra if is_winner_row else cur_winner_extra,
        "winner_origin_period": winner_period    if is_winner_row else None,
    }


def reject_budget_request(request_id: str, acting_hod_email: str,
                           is_hod_actor: bool = True) -> dict:
    """
    Reject a request group. A rejection by ANY one HOD kills the request for
    ALL fanned-out HODs — every row (including the actor's) moves to
    'rejected'. No budget change occurs.

    is_hod_actor: True enforces that acting_hod_email must be one of the
    fanned-out HOD rows; False (admin/senior approver override) skips that
    check — any authorized actor may reject the whole group.
    """
    acting_email_lc = (acting_hod_email or "").strip().lower()
    try:
        conn = _pg()
        conn.autocommit = False
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT id, hod_email, status, target_user_id, requester_email,
                       delegated_to
                FROM   ainxt.hod_allocation_ledger
                WHERE  request_id = %s
                  AND  action IN ('approve_request', 'winner_approve_request')
                ORDER  BY hod_email
                FOR UPDATE
            """, (request_id,))
            rows = cur.fetchall()
            if not rows:
                conn.rollback(); cur.close(); conn.close()
                return {"success": False, "error": "Request not found"}

            # Self-approval guard: rejecting one's own request is also blocked
            # (a delegatee-requester must go through the HOD or another
            # delegatee even to close their own request).
            requester_lc = (rows[0][4] or "").strip().lower()
            if requester_lc and acting_email_lc and acting_email_lc == requester_lc:
                conn.rollback(); cur.close(); conn.close()
                return {
                    "success": False,
                    "error":   "You cannot reject your own budget-increase request.",
                }

            if is_hod_actor and acting_email_lc and not any(
                ((r[1] or "").lower() == acting_email_lc and (r[5] or "") == "")
                or (r[5] or "").lower() == acting_email_lc
                for r in rows
            ):
                conn.rollback(); cur.close(); conn.close()
                return {"success": False, "error": "This request was not routed to you"}

            already_resolved = next((r for r in rows if r[2] != "pending"), None)
            if already_resolved is not None:
                conn.rollback(); cur.close(); conn.close()
                return {"success": False, "error": f"This request was already {already_resolved[2]} by {already_resolved[1]}"}

            target_user_id = rows[0][3]
            all_ids = [r[0] for r in rows]
            cur.execute("""
                UPDATE ainxt.hod_allocation_ledger
                   SET status = 'rejected', resolved_at = NOW(),
                       approved_by = %s
                 WHERE id = ANY(%s::uuid[])
            """, (acting_email_lc or None, all_ids))
            conn.commit()
        finally:
            cur.close(); conn.close()
    except Exception as e:
        logger.error(f"BudgetStore.reject_budget_request({request_id}): {e}")
        return {"success": False, "error": str(e)}

    return {"success": True, "request_id": request_id, "user_id": str(target_user_id)}


# ============================================================
# 10x WINNER GRANTS  (atomic batch)
#
# Award model: base stays $50 for everyone, always. A winner receives
# WINNER grant dollars of *extra* budget which carries over month to month,
# depleting only as they spend above their base. The pooled extra_cost_usd
# and the winner-origin slice winner_extra_usd both rise by the grant;
# base_cost_usd is untouched.
#
# Atomicity: the whole batch is ONE transaction. Every target row is locked
# with SELECT ... FOR UPDATE before any write, and a single commit publishes
# all of them. A failure on user N rolls back users 1..N-1 too, so the admin
# can safely retry the entire batch rather than being left with a partially
# applied award and no way to tell which half landed.
# ============================================================

WINNER_GRANT_USD: float = 1000.0


def apply_winner_grants(user_ids: List[str], actor_email: str = "",
                        period: Optional[str] = None,
                        grant_usd: float = WINNER_GRANT_USD) -> List[dict]:
    """Grant the 10x-winner extra allocation to every user in `user_ids`.

    All mutations for the batch happen in ONE Postgres transaction. Returns a
    per-user result list (in the order given) for the caller to notify from
    AFTER the commit — this function deliberately sends no email and touches
    no inbox, so a delivery failure can never unwind a committed grant.

    Raises ValueError if any user was already awarded in `period` (the caller
    should have pre-checked and returned 409; this is the race-safe backstop,
    since the check here runs under the row lock). Raises on any DB error
    after rolling back — nothing is left half-applied.

    Redis is synced best-effort post-commit; a cache failure does not undo
    the DB write, and get_budget() re-populates on the next miss.
    """
    if not user_ids:
        return []
    period = period or _current_period()
    grant = float(grant_usd)

    results: List[dict] = []
    try:
        conn = _pg()
        conn.autocommit = False
        cur = conn.cursor()
        try:
            # Lock every target row up front. Ordering by user_id gives a
            # deterministic lock sequence so two concurrent batches sharing
            # users can't deadlock against each other.
            for uid in sorted(str(u) for u in user_ids):
                cur.execute("""
                    SELECT base_cost_usd, extra_cost_usd, winner_extra_usd,
                           winner_origin_period, max_tokens_total, max_requests_total
                    FROM   ainxt.budget_configs
                    WHERE  user_id = %s
                    FOR UPDATE
                """, (uid,))
                row = cur.fetchone()

                if row is not None:
                    base          = float(row[0]) if row[0] is not None else 50.0
                    old_extra     = float(row[1]) if row[1] is not None else 0.0
                    old_winner    = float(row[2]) if row[2] is not None else 0.0
                    origin_period = row[3]
                    tokens        = int(row[4]) if row[4] is not None else 500_000
                    reqs          = int(row[5]) if row[5] is not None else 1_000
                else:
                    # Never seeded (no /budget/me hit yet) — insert below with
                    # the same defaults set_budget() would have applied.
                    base, old_extra, old_winner, origin_period = 50.0, 0.0, 0.0, None
                    tokens, reqs = 500_000, 1_000

                # Double-award guard, evaluated under the row lock so two
                # concurrent batches cannot both pass it for the same user.
                if origin_period == period:
                    raise ValueError(
                        f"User {uid} was already awarded in period {period}"
                    )

                new_winner = old_winner + grant
                new_extra  = old_extra + grant   # stacks on any carryover
                new_total  = base + new_extra

                if row is not None:
                    cur.execute("""
                        UPDATE ainxt.budget_configs
                           SET extra_cost_usd       = %s,
                               winner_extra_usd     = %s,
                               winner_origin_period = %s,
                               max_cost_usd_total   = %s,
                               monthly_limit_usd    = %s,
                               updated_at           = NOW()
                         WHERE user_id = %s
                    """, (new_extra, new_winner, period, new_total, new_total, uid))
                else:
                    cur.execute("""
                        INSERT INTO ainxt.budget_configs
                            (id, user_id, monthly_limit_usd, max_tokens_total,
                             max_requests_total, max_cost_usd_total,
                             model_allowlist, base_cost_usd, extra_cost_usd,
                             winner_extra_usd, winner_origin_period,
                             created_at, updated_at)
                        VALUES (gen_random_uuid(), %s, %s, %s, %s, %s,
                                '[]'::jsonb, %s, %s, %s, %s, NOW(), NOW())
                    """, (uid, new_total, tokens, reqs, new_total,
                          base, new_extra, new_winner, period))

                results.append({
                    "user_id":              uid,
                    "base_cost_usd":        base,
                    "previous_extra_usd":   old_extra,
                    "granted_usd":          grant,
                    "extra_cost_usd":       new_extra,
                    "winner_extra_usd":     new_winner,
                    "max_cost_usd_total":   new_total,
                    "winner_origin_period": period,
                    "max_tokens_total":     tokens,
                    "max_requests_total":   reqs,
                })

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close(); conn.close()
    except Exception as e:
        if isinstance(e, ValueError):
            raise
        logger.error(
            f"BudgetStore.apply_winner_grants actor={actor_email} "
            f"count={len(user_ids)} period={period}: {e}"
        )
        raise

    # ── Post-commit Redis sync (best-effort) ─────────────────────────────
    rc = _get_redis()
    if rc is not None:
        for r in results:
            try:
                _redis_hset_mapping(rc, f"budget:{r['user_id']}", {
                    "max_tokens_total":     r["max_tokens_total"],
                    "max_requests_total":   r["max_requests_total"],
                    "max_cost_usd_total":   r["max_cost_usd_total"],
                    "base_cost_usd":        r["base_cost_usd"],
                    "extra_cost_usd":       r["extra_cost_usd"],
                    "winner_extra_usd":     r["winner_extra_usd"],
                    "winner_origin_period": r["winner_origin_period"],
                })
                rc.sadd("budget:users:index", r["user_id"])
            except Exception as e:
                logger.warning(
                    f"BudgetStore.apply_winner_grants Redis sync failed "
                    f"user={r['user_id']}: {e}"
                )

    # Preserve the caller's input order for the response/emails.
    by_id = {r["user_id"]: r for r in results}
    return [by_id[str(u)] for u in user_ids if str(u) in by_id]


def get_winner_origin_periods(user_ids: List[str]) -> dict:
    """Return {user_id: winner_origin_period} for the given users.

    Used by the router's pre-flight 409 check so an admin gets a clean
    "already awarded" error listing emails, instead of hitting the
    race-safe ValueError inside apply_winner_grants.
    """
    if not user_ids:
        return {}
    try:
        conn = _pg()
        cur  = conn.cursor()
        cur.execute("""
            SELECT user_id, winner_origin_period
            FROM   ainxt.budget_configs
            WHERE  user_id = ANY(%s)
        """, ([str(u) for u in user_ids],))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return {r[0]: r[1] for r in rows}
    except Exception as e:
        logger.warning(f"BudgetStore.get_winner_origin_periods: {e}")
        return {}


def get_all_usage_today(limit: int = 50) -> list:
    """Return top users by token usage today (admin activity overview — KV only)."""
    try:
        rc = _get_redis()
        if not rc:
            return []
        today = datetime.now(timezone.utc).date().isoformat()
        keys = list(rc.keys(f"usage:*:{today}"))
        rows = []
        for key in keys[:limit * 2]:
            parts = key.split(":")
            if len(parts) != 3 or parts[2] == "total":
                continue
            user_id = parts[1]
            data = rc.hgetall(key)
            rows.append({
                "user_id":        user_id,
                "tokens_used":    int(data.get("tokens_used", 0)),
                "requests_made":  int(data.get("requests_made", 0)),
                "cost_usd_spent": round(float(data.get("cost_usd_spent", 0.0)), 6),
            })
        rows.sort(key=lambda x: x["tokens_used"], reverse=True)
        return rows[:limit]
    except Exception:
        return []


def get_all_usage_totals(limit: int = 50) -> list:
    """
    Return all users' cumulative usage — for admin budget overview.
    Reads from Postgres user_usage_totals (always accurate, Redis-independent).
    """
    try:
        conn = _pg()
        cur  = conn.cursor()
        cur.execute("""
            SELECT u.user_id, u.tokens_used, u.requests_made, u.cost_usd_spent,
                   bc.max_cost_usd_total, bc.max_tokens_total, bc.max_requests_total
            FROM   user_usage_totals u
            LEFT JOIN budget_configs bc ON bc.user_id = u.user_id
            ORDER BY u.cost_usd_spent DESC
            LIMIT  %s
        """, (limit,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        result = []
        for row in rows:
            result.append({
                "user_id":            row[0],
                "tokens_used":        int(row[1]),
                "requests_made":      int(row[2]),
                "cost_usd_spent":     round(float(row[3]), 6),
                "max_cost_usd_total": float(row[4]) if row[4] else DEFAULT_COST_LIMIT_USD,
                "max_tokens_total":   int(row[5])   if row[5] else DEFAULT_TOKEN_LIMIT,
                "max_requests_total": int(row[6])   if row[6] else DEFAULT_REQUEST_LIMIT,
            })
        return result
    except Exception as e:
        logger.warning(f"BudgetStore.get_all_usage_totals PG: {e}")
        return []
