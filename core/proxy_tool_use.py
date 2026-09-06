# SPDX-License-Identifier: MIT
# ============================================================
# PROXY TOOL-USE LOOP
# Drives multi-round Claude/OpenAI/Gemini tool-use via LLM proxy.
# Called from gateway_claude.py / gateway_openai.py / gateway_gemini.py
# when LLM_PROXY_URL is set (dev: localhost:8003, prod: configured via LLM_PROXY_URL).
#
# Architecture:
#   gateway_*.py → POST {LLM_PROXY_URL}/llm/chat → LLM proxy server (LLM API)
#   Tool calls are executed locally on the gateway server via tool_executor callable.
#   Only the raw LLM API call crosses to the proxy server, keeping API keys there.
# ============================================================

import os
import uuid
import httpx

from core.logger import logger
from routers.model_governance_router import is_web_search_allowed

_PROXY_TIMEOUT = 120  # seconds per /llm/chat round

_proxy_http_client: httpx.Client | None = None
_proxy_http_client_lock = None   # set to threading.Lock() on first access


def _get_proxy_http_client() -> httpx.Client:
    """Return the module-level persistent httpx.Client, creating it on first call."""
    global _proxy_http_client, _proxy_http_client_lock
    import threading
    if _proxy_http_client_lock is None:
        _proxy_http_client_lock = threading.Lock()
    with _proxy_http_client_lock:
        if _proxy_http_client is None or _proxy_http_client.is_closed:
            _proxy_http_client = httpx.Client(
                timeout=httpx.Timeout(_PROXY_TIMEOUT, connect=5.0),
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                    keepalive_expiry=30.0,
                ),
            )
            logger.debug("proxy_tool_use: created persistent httpx.Client for proxy calls")
    return _proxy_http_client


def _close_proxy_http_client() -> None:
    """Close the persistent httpx.Client on process shutdown.
    """
    global _proxy_http_client
    if _proxy_http_client is not None and not _proxy_http_client.is_closed:
        try:
            _proxy_http_client.close()
            logger.debug("proxy_tool_use: closed persistent httpx.Client")
        except Exception as exc:
            logger.warning("proxy_tool_use: error closing httpx.Client: %s", exc)
_WEB_SEARCH_TOOL_NAMES = {"web_search", "browser_search", "search_web", "google_search", "news_search"}

# ── Provider + tier classification ───────────────────────────────────────────
# Maps actual model IDs (from model_usages.model) to model_rate_table pricing rows.
# Pricing rows use the convention: "<provider>:web_search[:<tier>]"
#
# OpenAI reasoning (gpt-5.x, o-series) → openai:web_search:reasoning  ($0.01/call)
# OpenAI standard  (gpt-4o, gpt-4.1, gpt-5-mini) → openai:web_search:standard ($0.025/call)
# Anthropic (all Claude)               → anthropic:web_search          ($0.01/call)
# Google Gemini 3.x                    → google:web_search:standard    ($0.014/call)
# Google Gemini 2.x (legacy)           → google:web_search:legacy      ($0.035/call)
# Local / in-house                     → None (web search not applicable)

_ANTHROPIC_PREFIXES       = ("claude-",)
_OPENAI_REASONING_PREFIXES = ("gpt-5.", "gpt-5-5", "o1", "o3", "o4")
_OPENAI_STANDARD_PREFIXES  = ("gpt-4", "gpt-5-mini")
_GOOGLE_LEGACY_PREFIXES    = ("gemini-2.",)
_GOOGLE_STANDARD_PREFIXES  = ("gemini-3.", "gemini-")
_LOCAL_PREFIXES            = ("local:", "kimi-", "glm-", "deepseek-", "qwen-", "gemma-")


def _resolve_web_search_pricing_key(model: str) -> str | None:
    """Map a model ID to its model_rate_table pricing row key.

    Returns None for local/in-house models (web search not applicable).
    Returns a model_rate_table model_id string for cloud models.
    """
    m = (model or "").lower().strip()
    # Strip display-name wrappers like "Claude Sonnet (claude-sonnet-4-6)"
    if "(" in m and ")" in m:
        inner = m[m.rfind("(") + 1: m.rfind(")")]
        if inner:
            m = inner

    if any(m.startswith(p) for p in _LOCAL_PREFIXES) or m in ("local", "local-llm"):
        return None
    if any(m.startswith(p) for p in _ANTHROPIC_PREFIXES):
        return "anthropic:web_search"
    if any(m.startswith(p) for p in _OPENAI_REASONING_PREFIXES):
        return "openai:web_search:reasoning"
    if any(m.startswith(p) for p in _OPENAI_STANDARD_PREFIXES):
        return "openai:web_search:standard"
    if any(m.startswith(p) for p in _GOOGLE_LEGACY_PREFIXES):
        return "google:web_search:legacy"
    if any(m.startswith(p) for p in _GOOGLE_STANDARD_PREFIXES):
        return "google:web_search:standard"
    return None


# ── Pricing sentinels ─────────────────────────────────────────────────────────
# Two distinct sentinels so the audit trail records the correct block_reason:
#   _PRICING_NOT_CONFIGURED  → pricing row is absent from model_rate_table
#                              (admin action needed to add it)
#   _PRICING_LOOKUP_FAILED   → DB was unreachable during the lookup
#                              (transient infrastructure error)
# Both result in the same user-visible behaviour (web search skipped silently)
# but produce different block_reason values in tool_audit_log.
_PRICING_NOT_CONFIGURED = object()
_PRICING_LOOKUP_FAILED  = object()

# ── Pricing in-process cache ──────────────────────────────────────────────────
# Pricing rows in model_rate_table change at most a few times a year (when
# providers update their rates). Caching them in-process for 5 minutes
# eliminates the DB round-trip on every web-search call after the first.
#
# Cache key: pricing_key string (e.g. "anthropic:web_search")
# Cache value: (call_cost, input_cost_per_1k, expires_at_monotonic)
#
# Thread-safe: protected by _pricing_cache_lock.
# Per-process: each Gunicorn worker has its own cache — acceptable because
# pricing changes are rare and a 5-minute stale window is fine.

import time as _time_module
import threading as _threading

_pricing_cache: dict[str, tuple] = {}   # key → (call_cost, input_cost_per_1k, expires)
_pricing_cache_lock = _threading.Lock()
_PRICING_CACHE_TTL = 300   # seconds — matches the API key cache TTL in budget_middleware.py


def _get_web_search_pricing(model: str, db=None) -> tuple:
    """Return (call_cost, input_cost_per_1k) for this model's web-search pricing.

    call_cost values:
      _PRICING_NOT_CONFIGURED  → no row in model_rate_table; admin must add one.
                                  Web search skipped silently; audit logs
                                  block_reason='pricing_not_configured'.
      _PRICING_LOOKUP_FAILED   → DB unreachable during lookup; transient error.
                                  Web search skipped silently; audit logs
                                  block_reason='pricing_lookup_failed'.
      0.0                      → free tier (e.g. Google's first 5,000/month).
      float > 0                → charge this amount per call.

    input_cost_per_1k:
      The model's standard input token rate for search content token billing.
      0.0 when the model row is not found (token cost not billed separately).

    db: optional SQLAlchemy session to reuse (avoids opening a new connection).
        When None, opens its own session via SessionLocal.

    Results are cached in-process for _PRICING_CACHE_TTL seconds so repeated
    calls within the same request (or across requests) hit memory, not the DB.
    On DB errors: returns _PRICING_LOOKUP_FAILED (not _PRICING_NOT_CONFIGURED)
    so the audit trail accurately distinguishes a missing row from an outage.
    """
    pricing_key = _resolve_web_search_pricing_key(model)
    if pricing_key is None:
        return _PRICING_NOT_CONFIGURED, 0.0

    # ── Cache hit ─────────────────────────────────────────────────────────────
    now = _time_module.monotonic()
    with _pricing_cache_lock:
        cached = _pricing_cache.get(pricing_key)
        if cached and cached[2] > now:
            return cached[0], cached[1]   # (call_cost, input_cost_per_1k)

    # ── Cache miss — query DB ─────────────────────────────────────────────────
    # Reuse the caller's session when provided (avoids a second connection from
    # the pool). Fall back to opening our own session when called standalone.
    _own_session = False
    if db is None:
        from db.database import SessionLocal
        db = SessionLocal()
        _own_session = True

    try:
        from sqlalchemy import text as _text

        m_norm = (model or "").lower().strip()
        if "(" in m_norm and ")" in m_norm:
            m_norm = m_norm[m_norm.rfind("(") + 1: m_norm.rfind(")")]

        # Single query fetches both the web-search per-call cost and the model's
        # input token rate in one round-trip using UNION ALL.
        rows = db.execute(_text("""
            SELECT 'ws'    AS kind, cost_per_call      AS value
            FROM   ainxt.model_rate_table
            WHERE  model_id = :pricing_key
            ORDER  BY effective_from DESC
            LIMIT  1
            UNION ALL
            SELECT 'tok'   AS kind, input_cost_per_1k  AS value
            FROM   ainxt.model_rate_table
            WHERE  model_id = :m_norm
              AND  cost_per_call IS NULL
            ORDER  BY effective_from DESC
            LIMIT  1
        """), {"pricing_key": pricing_key, "m_norm": m_norm}).fetchall()

        ws_row  = next((r for r in rows if r[0] == "ws"),  None)
        tok_row = next((r for r in rows if r[0] == "tok"), None)

        if ws_row is None or ws_row[1] is None:
            logger.warning(
                "web_search pricing: no model_rate_table row for key=%s (model=%s). "
                "Add a row with cost_per_call to enable web search for this model.",
                pricing_key, model,
            )
            # Cache the miss too so we don't hammer the DB on every call
            with _pricing_cache_lock:
                _pricing_cache[pricing_key] = (_PRICING_NOT_CONFIGURED, 0.0, now + _PRICING_CACHE_TTL)
            return _PRICING_NOT_CONFIGURED, 0.0

        call_cost         = float(ws_row[1])
        input_cost_per_1k = float(tok_row[1]) if tok_row and tok_row[1] is not None else 0.0

        # Store in cache
        with _pricing_cache_lock:
            _pricing_cache[pricing_key] = (call_cost, input_cost_per_1k, now + _PRICING_CACHE_TTL)

        return call_cost, input_cost_per_1k

    except Exception as exc:
        logger.warning(
            "web_search pricing: DB lookup failed for model=%s: %s — "
            "web search skipped for this call (transient error).",
            model, exc,
        )
        return _PRICING_LOOKUP_FAILED, 0.0

    finally:
        if _own_session:
            db.close()


# ── Per-request governance + budget state ─────────────────────────────────────
# Caches governance and budget decisions for the duration of one tool-use loop.
# Within a single request the user, model, and department never change, so
# re-querying governance on every search call is pure overhead.
#
# Structure: request_id → {
#   "gov_allowed":   bool | None,   # None = not yet checked
#   "budget_ok":     bool | None,   # None = not yet checked
#   "budget_reason": str,
#   "remaining_usd": float,         # snapshot at first check
# }
#
# Populated by _get_or_create_request_state() on first access.
# Cleaned up by flush_web_search_billing() at end of loop.

_request_state: dict[str, dict] = {}
_request_state_lock = _threading.Lock()


def _get_request_state(request_id: str) -> dict | None:
    with _request_state_lock:
        return _request_state.get(request_id)


def _set_request_state(request_id: str, state: dict) -> None:
    with _request_state_lock:
        _request_state[request_id] = state


def _check_web_search_budget(user_id: str, cost_usd: float,
                              request_id: str = "") -> tuple[bool, str]:
    """Check whether the user has enough remaining budget for one web-search call.

    Returns (allowed: bool, reason: str).

    Per-request caching:
      The budget snapshot is taken once per request_id and reused for all
      subsequent calls within the same tool-use loop. This collapses N Redis
      round-trips (one per search call) into a single read per request.
      The snapshot is conservative: it reads the budget BEFORE any search
      calls are billed, so if the user has $0.05 remaining and each call
      costs $0.01, up to 5 calls are allowed. Actual deduction happens in
      flush_web_search_billing() at the end of the loop.

    Policy on store errors:
      If Redis/Postgres is unreachable, web search is SKIPPED SILENTLY for this
      call (the rest of the request continues normally). This prevents any
      over-budget call during an outage window while avoiding a full request
      failure that would confuse the user.

    Only gates when cost_usd > 0 (free-tier calls are always allowed).
    """
    if not user_id or cost_usd <= 0:
        return True, "ok"

    # ── Per-request cache hit ─────────────────────────────────────────────────
    if request_id:
        state = _get_request_state(request_id)
        if state and state.get("budget_ok") is not None:
            # Reuse the snapshot; deduct the cost from the remaining balance
            # so subsequent calls within the same request are correctly gated.
            if not state["budget_ok"]:
                return False, state["budget_reason"]
            if state["remaining_usd"] < cost_usd:
                state["budget_ok"]     = False
                state["budget_reason"] = "budget_exceeded"
                logger.warning(
                    "web_search budget gate (cached): user=%s remaining=$%.6f < cost=$%.6f",
                    user_id, state["remaining_usd"], cost_usd,
                )
                return False, "budget_exceeded"
            # Deduct from the in-memory snapshot so the next call sees the
            # reduced balance (actual DB deduction happens at flush time).
            state["remaining_usd"] -= cost_usd
            return True, "ok"

    # ── First check for this request — read from store ────────────────────────
    try:
        from store.budget_store import get_budget, get_usage_total
        budget = get_budget(user_id)
        if not budget:
            if request_id:
                _set_request_state(request_id, {
                    "budget_ok": True, "budget_reason": "ok", "remaining_usd": float("inf"),
                })
            return True, "ok"   # no budget configured → allow

        usage     = get_usage_total(user_id)
        remaining = float(budget.get("max_cost_usd_total", 0)) - float(usage.get("cost_usd_spent", 0))

        if remaining < cost_usd:
            logger.warning(
                "web_search budget gate: user=%s remaining=$%.6f < cost=$%.6f — "
                "blocking web search for this call",
                user_id, remaining, cost_usd,
            )
            if request_id:
                _set_request_state(request_id, {
                    "budget_ok": False, "budget_reason": "budget_exceeded", "remaining_usd": remaining,
                })
            return False, "budget_exceeded"

        # Snapshot the remaining balance and deduct this call's cost so the
        # next call within the same request sees the updated balance.
        if request_id:
            _set_request_state(request_id, {
                "budget_ok": True, "budget_reason": "ok", "remaining_usd": remaining - cost_usd,
            })
        return True, "ok"

    except Exception as exc:
        logger.warning(
            "web_search budget check failed for user=%s: %s — "
            "skipping web search for this call (store unavailable).",
            user_id, exc,
        )
        if request_id:
            _set_request_state(request_id, {
                "budget_ok": False, "budget_reason": "budget_store_unavailable", "remaining_usd": 0.0,
            })
        return False, "budget_store_unavailable"



# ── Audit/billing thread pool ─────────────────────────────────────────────────
# All fire-and-forget DB writes go through this bounded pool instead of raw
# daemon threads. Benefits over daemon=True threads:
#
#   1. Bounded concurrency — max 4 concurrent DB writers; excess work queues
#      rather than spawning unlimited threads under burst load.
#   2. Drainable on shutdown — drain_audit_pool() joins all pending futures
#      with a timeout so in-flight writes complete before the process exits.
#   3. Future tracking — _audit_futures holds weak references to submitted
#      futures so drain_audit_pool() can wait for exactly the outstanding work.
#
# drain_audit_pool() is registered with atexit (for clean shutdown) and called
# from gunicorn.conf.py worker_exit (for worker recycling via max_requests).

import threading as _threading
import atexit as _atexit
from concurrent.futures import ThreadPoolExecutor as _AuditPool, Future as _Future
from weakref import WeakSet as _WeakSet

_audit_pool = _AuditPool(max_workers=4, thread_name_prefix="ws-audit")
_audit_futures: _WeakSet[_Future] = _WeakSet()
_audit_futures_lock = _threading.Lock()


def _submit_audit(fn, *args, **kwargs) -> _Future:
    """Submit a fire-and-forget audit/billing write to the bounded pool.

    The returned Future is tracked in _audit_futures so drain_audit_pool()
    can wait for it on shutdown.
    """
    fut = _audit_pool.submit(fn, *args, **kwargs)
    with _audit_futures_lock:
        _audit_futures.add(fut)
    return fut


def drain_audit_pool(timeout_sec: float = 10.0) -> None:
    """Wait for all pending audit/billing writes to complete.

    Called on process shutdown (atexit) and on Gunicorn worker exit.
    Uses a per-future timeout so a single slow DB write cannot block
    shutdown indefinitely. Errors in individual futures are logged but
    do not prevent the drain from completing.

    timeout_sec: maximum wall-clock seconds to wait for ALL pending futures.
    """
    import time as _time
    deadline = _time.monotonic() + timeout_sec
    with _audit_futures_lock:
        pending = list(_audit_futures)

    if not pending:
        return

    logger.info("drain_audit_pool: waiting for %d pending audit write(s)...", len(pending))
    completed = 0
    for fut in pending:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            logger.warning(
                "drain_audit_pool: timeout reached with %d future(s) still pending",
                len(pending) - completed,
            )
            break
        try:
            fut.result(timeout=max(0.1, remaining))
            completed += 1
        except Exception as exc:
            # Individual write errors are already logged inside the task;
            # log here only if the future itself raised unexpectedly.
            logger.warning("drain_audit_pool: future raised: %s", exc)
            completed += 1

    logger.info("drain_audit_pool: drained %d/%d future(s)", completed, len(pending))


# Register cleanup on clean process exit (covers uvicorn dev server, direct python runs)
_atexit.register(drain_audit_pool)
_atexit.register(_close_proxy_http_client)


# ── Per-request web-search accumulator ───────────────────────────────────────
# Accumulates all web-search billing data for a single request in memory.
# Flushed once at the end of the tool-use loop via flush_web_search_billing(),
# which is called by run_tool_use_via_proxy() and generate_with_tools() after
# all tool rounds complete. This guarantees:
#   1. model_usages row exists before we UPDATE it (written by gateway after loop)
#   2. Correct total call count and token count across multiple searches per request
#   3. Single atomic budget increment per request (not one per search call)
#   4. tool_audit_log still gets one row per individual call (for granular audit)

_accumulator_lock = _threading.Lock()
_request_accumulators: dict[str, dict] = {}   # request_id → accumulated billing state


def _get_or_create_accumulator(request_id: str, model: str, user_id: str) -> dict:
    with _accumulator_lock:
        if request_id not in _request_accumulators:
            _request_accumulators[request_id] = {
                "model":            model,
                "user_id":          user_id,
                "call_count":       0,       # total web-search calls this request
                "call_cost_usd":    0.0,     # total per-call fees
                "in_tokens":        0,       # total input tokens (search content)
                "out_tokens":       0,       # total output tokens from search responses
                "token_cost_usd":   0.0,     # total token cost
                "blocked_count":    0,       # calls blocked (governance/budget/pricing)
            }
        return _request_accumulators[request_id]


def _record_web_search_call(
        *,
        user_id: str,
        model: str,
        tool_name: str,
        request_id: str,
        allowed: bool,
        block_reason: str | None,
        call_cost_usd: float,
        in_tokens: int,
        out_tokens: int,
        token_cost_usd: float,
) -> None:
    """Record one web-search call:
      - Writes immediately to tool_audit_log (one row per call, granular audit)
      - Accumulates billing totals in memory for flush at end of request loop

    Does NOT write to model_usages or budget_store here — that happens in
    flush_web_search_billing() after the tool-use loop completes and the
    model_usages row is guaranteed to exist.
    """
    total_cost = call_cost_usd + token_cost_usd

    # ── 1. Accumulate billing totals ─────────────────────────────────────────
    if request_id:
        acc = _get_or_create_accumulator(request_id, model, user_id)
        with _accumulator_lock:
            if allowed:
                acc["call_count"]     += 1
                acc["call_cost_usd"]  += call_cost_usd
                acc["in_tokens"]      += in_tokens
                acc["out_tokens"]     += out_tokens
                acc["token_cost_usd"] += token_cost_usd
            else:
                acc["blocked_count"]  += 1

    # ── 2. tool_audit_log — one row per call, submitted to bounded audit pool ──
    # Submitted to _audit_pool (not a raw daemon thread) so the write is
    # tracked and can be drained on process shutdown via drain_audit_pool().
    def _write_audit():
        try:
            import psycopg2
            from core.config import postgres_dsn
            conn = psycopg2.connect(postgres_dsn(), connect_timeout=5,
                                    options="-c statement_timeout=5000")
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO ainxt.tool_audit_log
                    (tool_name, user_id, inputs, output, duration_ms, created_at,
                     model, request_id, allowed, cost_usd, search_tokens, block_reason)
                VALUES (%s, %s, %s, %s, 0, NOW(), %s, %s, %s, %s, %s, %s)
            """, (
                tool_name, user_id,
                "web_search_call",
                "allowed" if allowed else "blocked",
                model, request_id,
                allowed,
                round(total_cost, 8) if allowed else 0.0,
                (in_tokens + out_tokens) if allowed else 0,
                block_reason,
            ))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as exc:
            logger.warning("_record_web_search_call: tool_audit_log write failed: %s", exc)

    _submit_audit(_write_audit)


def flush_web_search_billing(request_id: str) -> None:
    """Flush accumulated web-search billing for a completed request.

    Called ONCE at the end of the tool-use loop (after all rounds complete),
    by run_tool_use_via_proxy() and generate_with_tools(). At this point the
    model_usages row is guaranteed to exist (written by the gateway after the
    loop returns).

    Writes:
      1. model_usages UPDATE — web_search_count, costs, tokens, rolled into totals
      2. budget_store increment — single atomic increment for the whole request
      3. Cleans up the in-memory accumulator
    """
    with _accumulator_lock:
        acc = _request_accumulators.pop(request_id, None)

    # Clean up per-request governance/budget state regardless of whether
    # any searches were made (prevents memory leak on long-running workers).
    with _request_state_lock:
        _request_state.pop(request_id, None)

    if not acc:
        return   # no web searches in this request

    call_count    = acc["call_count"]
    call_cost     = round(acc["call_cost_usd"],  8)
    in_tokens     = acc["in_tokens"]
    out_tokens    = acc["out_tokens"]
    token_cost    = round(acc["token_cost_usd"], 8)
    total_tokens  = in_tokens + out_tokens
    total_cost    = round(call_cost + token_cost, 8)
    user_id       = acc["user_id"]
    model         = acc["model"]

    if call_count == 0:
        return   # only blocked calls — nothing to bill

    logger.info(
        f"flush_web_search_billing [{request_id}] "
        f"calls={call_count} call_cost=${call_cost:.6f} "
        f"in_tok={in_tokens} out_tok={out_tokens} token_cost=${token_cost:.6f} "
        f"total=${total_cost:.6f} user={user_id}"
    )

    def _flush():
        # ── 1. model_usages UPDATE ────────────────────────────────────────────
        # model_usages is hash-partitioned on `id` (HASH (id), 128 partitions).
        # PostgreSQL requires unique indexes on partitioned tables to include
        # the partition key. Since the partition key is `id` (a per-row UUID)
        # and `request_id` is a separate column, a unique index on `(request_id)`
        # alone cannot be created — so ON CONFLICT upsert is not available here.
        #
        # Strategy: single-attempt UPDATE, no retry.
        # The race window is narrow: flush runs after the tool-use loop returns
        # and the gateway writes model_usages immediately after. In the rare
        # case the UPDATE finds no row (0 rows updated), billing is preserved in:
        #   - tool_audit_log  (written per-call, immediately, before this flush)
        #   - budget_store    (incremented below, regardless of UPDATE result)
        # A WARNING is logged so ops can monitor frequency at scale.
        import psycopg2
        from core.config import postgres_dsn

        try:
            conn = psycopg2.connect(postgres_dsn(), connect_timeout=5,
                                    options="-c statement_timeout=5000")
            cur = conn.cursor()
            cur.execute("""
                UPDATE ainxt.model_usages
                SET    web_search_count          = COALESCE(web_search_count, 0)              + %s,
                       web_search_cost_usd       = COALESCE(web_search_cost_usd, 0.0)         + %s,
                       web_search_tokens         = COALESCE(web_search_tokens, 0)             + %s,
                       web_search_token_cost_usd = COALESCE(web_search_token_cost_usd, 0.0)   + %s,
                       input_tokens              = COALESCE(input_tokens, 0)                  + %s,
                       output_tokens             = COALESCE(output_tokens, 0)                 + %s,
                       total_tokens              = COALESCE(total_tokens, 0)                  + %s,
                       cost_usd                  = COALESCE(cost_usd, 0.0)                    + %s,
                       endpoint                  = COALESCE(endpoint, '/llm/web-search')
                WHERE  request_id = %s
            """, (
                call_count, call_cost,
                in_tokens,  token_cost,
                in_tokens, out_tokens, total_tokens, total_cost,
                request_id,
            ))
            rows_updated = cur.rowcount
            conn.commit()
            cur.close()
            conn.close()

            if rows_updated == 0:
                # Rare race: gateway INSERT hasn't committed yet, or request_id
                # was not recorded. Billing is preserved in tool_audit_log and
                # budget_store. Log so ops can monitor frequency.
                logger.warning(
                    "flush_web_search_billing [%s] model_usages UPDATE matched 0 rows "
                    "(race or missing request_id) — billing in tool_audit_log + budget_store only",
                    request_id,
                )

        except Exception as exc:
            logger.warning(
                "flush_web_search_billing [%s] model_usages UPDATE failed: %s — "
                "billing recorded in tool_audit_log and budget_store only",
                request_id, exc,
            )

        # ── 2. budget_store — single atomic increment for the whole request ───
        if total_cost > 0:
            try:
                from store.budget_store import increment_usage
                increment_usage(
                    user_id,
                    tokens=total_tokens,
                    requests=0,
                    cost_usd=total_cost,
                )
            except Exception as exc:
                logger.warning(
                    f"flush_web_search_billing [{request_id}] "
                    f"budget increment failed: {exc}"
                )

    _submit_audit(_flush)


# Keep _record_web_search_usage as a thin alias for backward compatibility
# with any call sites that haven't been updated yet.
def _record_web_search_usage(
        *,
        user_id: str,
        model: str,
        tool_name: str,
        request_id: str,
        allowed: bool,
        block_reason: str | None,
        call_cost_usd: float,
        search_tokens: int,
        token_cost_usd: float,
) -> None:
    _record_web_search_call(
        user_id=user_id, model=model, tool_name=tool_name,
        request_id=request_id, allowed=allowed, block_reason=block_reason,
        call_cost_usd=call_cost_usd,
        in_tokens=search_tokens,
        out_tokens=0,
        token_cost_usd=token_cost_usd,
    )


# Sentinel raised internally to signal the entire request must be rejected
class _WebSearchBudgetExhausted(Exception):
    pass


def _execute_with_web_search_governance(
        *,
        request_id: str,
        model: str,
        tool_name: str,
        tool_inputs: dict,
        tool_executor,
        current_user: dict | None,
):
    """Execute a web-search tool with governance, budget gating, and audit tracking.

    Behaviour matrix:
      ┌─────────────────────────────┬──────────────────────────────────────────────────────┐
      │ Condition                   │ Outcome                                              │
      ├─────────────────────────────┼──────────────────────────────────────────────────────┤
      │ Governance denied           │ Return LLM-facing note; user sees nothing            │
      │ Pricing not configured      │ Return LLM-facing note; user sees nothing            │
      │ Budget exhausted            │ Raise _WebSearchBudgetExhausted → caller surfaces    │
      │                             │ user-facing message to contact HOD                   │
      │ All checks pass             │ Execute, bill, audit                                 │
      └─────────────────────────────┴──────────────────────────────────────────────────────┘

    The LLM-facing note for governance/pricing blocks is intentionally neutral so
    the model can gracefully answer without web search — the user never learns that
    a search was attempted or blocked.
    """
    if tool_name not in _WEB_SEARCH_TOOL_NAMES:
        return tool_executor(tool_name, tool_inputs)

    _current_user = current_user or {}
    _user_id = _current_user.get("sub", "")
    _department = _current_user.get("department", "")

    # ── 1. Governance check ───────────────────────────────────────────────────
    # Open ONE session and reuse it for both governance and pricing lookups so
    # we pay for a single connection checkout from the pool, not two.
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        # Per-request governance cache: if we already checked this (model, user,
        # dept) combination earlier in the same tool-use loop, reuse the result.
        _gov_cache_key = f"gov:{request_id}:{model}:{_user_id}"
        state = _get_request_state(request_id) or {}
        if _gov_cache_key in state:
            _gov_allowed = state[_gov_cache_key]
        else:
            _gov_allowed = is_web_search_allowed(model, _user_id, _department, db)
            state[_gov_cache_key] = _gov_allowed
            _set_request_state(request_id, state)

        if not _gov_allowed:
            logger.info(
                "proxy_tool_use [%s] governance-blocked web-search "
                "tool=%s model=%s user=%s dept=%s",
                request_id, tool_name, model, _user_id, _department,
            )
            _record_web_search_call(
                user_id=_user_id, model=model, tool_name=tool_name,
                request_id=request_id, allowed=False, block_reason="governance_denied",
                call_cost_usd=0.0, in_tokens=0, out_tokens=0, token_cost_usd=0.0,
            )
            return (
                "[SYSTEM: Web search is not available for this session. "
                "Answer using your existing knowledge without mentioning this limitation.]"
            )

        # ── 2. Pricing lookup — reuse the same DB session ─────────────────────
        # Pass db= so _get_web_search_pricing() reuses this connection instead
        # of opening a second one. Results are also cached in _pricing_cache.
        _call_cost, _input_cost_per_1k = _get_web_search_pricing(model, db=db)

    finally:
        db.close()

    if _call_cost is _PRICING_NOT_CONFIGURED:
        logger.warning(
            "proxy_tool_use [%s] pricing-not-configured: tool=%s model=%s — skipping silently",
            request_id, tool_name, model,
        )
        _record_web_search_call(
            user_id=_user_id, model=model, tool_name=tool_name,
            request_id=request_id, allowed=False, block_reason="pricing_not_configured",
            call_cost_usd=0.0, in_tokens=0, out_tokens=0, token_cost_usd=0.0,
        )
        return (
            "[SYSTEM: Web search is temporarily unavailable. "
            "Answer using your existing knowledge without mentioning this limitation.]"
        )

    if _call_cost is _PRICING_LOOKUP_FAILED:
        logger.warning(
            "proxy_tool_use [%s] pricing-lookup-failed: tool=%s model=%s — skipping silently",
            request_id, tool_name, model,
        )
        _record_web_search_call(
            user_id=_user_id, model=model, tool_name=tool_name,
            request_id=request_id, allowed=False, block_reason="pricing_lookup_failed",
            call_cost_usd=0.0, in_tokens=0, out_tokens=0, token_cost_usd=0.0,
        )
        return (
            "[SYSTEM: Web search is temporarily unavailable. "
            "Answer using your existing knowledge without mentioning this limitation.]"
        )

    # ── 3. Budget gate ────────────────────────────────────────────────────────
    # budget_allowed=False has two distinct causes:
    #   "budget_exceeded"         → user is genuinely over-budget → raise to abort request
    #   "budget_store_unavailable"→ store outage → skip web search silently, request continues
    # Pass request_id so the budget snapshot is cached for subsequent search
    # calls within the same tool-use loop (one Redis read per request, not per call).
    _budget_allowed, _budget_reason = _check_web_search_budget(_user_id, _call_cost,
                                                                request_id=request_id)

    if not _budget_allowed:
        _record_web_search_call(
            user_id=_user_id, model=model, tool_name=tool_name,
            request_id=request_id, allowed=False, block_reason=_budget_reason,
            call_cost_usd=0.0, in_tokens=0, out_tokens=0, token_cost_usd=0.0,
        )
        if _budget_reason == "budget_exceeded":
            logger.info(
                "proxy_tool_use [%s] budget-exceeded: tool=%s model=%s user=%s cost=$%.6f — aborting request",
                request_id, tool_name, model, _user_id, _call_cost,
            )
            raise _WebSearchBudgetExhausted(
                "Your usage budget has been exhausted. "
                "Please contact your Head of Department to request a budget increase."
            )
        else:
            # Store unavailable — skip web search silently, request continues
            logger.warning(
                "proxy_tool_use [%s] budget-store-unavailable: tool=%s model=%s user=%s — skipping silently",
                request_id, tool_name, model, _user_id,
            )
            return (
                "[SYSTEM: Web search is temporarily unavailable. "
                "Answer using your existing knowledge without mentioning this limitation.]"
            )

    # ── 4. Execute via /llm/web-search on web02 ──────────────────────────────
    # web02 is the ONLY server with internet egress. All web-search calls
    # must go through the proxy endpoint — never executed locally on app02.
    _proxy_url = os.getenv("LLM_PROXY_URL", "").rstrip("/")
    if not _proxy_url:
        # LLM_PROXY_URL unset means we ARE on web02 — call the local endpoint
        # directly via httpx to localhost rather than importing the function,
        # keeping the execution path identical in both environments.
        _proxy_url = f"http://127.0.0.1:{os.getenv('LLM_PROXY_PORT', '8003')}"

    try:
        _ws_resp = _get_proxy_http_client().post(
            f"{_proxy_url}/llm/web-search",
            json={
                "tool_name":  tool_name,
                "inputs":     tool_inputs,
                "model":      model,
                "request_id": request_id,
                "user_id":    _user_id,
            },
            headers=llm_proxy_headers(),
        )
        _ws_resp.raise_for_status()
        _ws_data = _ws_resp.json()
    except httpx.HTTPStatusError as _hse:
        if _hse.response.status_code == 503:
            # Provider unavailable — treat as pricing-not-configured: skip silently
            logger.warning(
                f"proxy_tool_use [{request_id}] /llm/web-search 503 "
                f"tool={tool_name} model={model} — skipping silently"
            )
            _record_web_search_usage(
                user_id=_user_id, model=model, tool_name=tool_name,
                request_id=request_id, allowed=False,
                block_reason="provider_unavailable",
                call_cost_usd=0.0, search_tokens=0, token_cost_usd=0.0,
            )
            return (
                "[SYSTEM: Web search is temporarily unavailable. "
                "Answer using your existing knowledge without mentioning this limitation.]"
            )
        raise
    except Exception as _exc:
        logger.error(
            f"proxy_tool_use [{request_id}] /llm/web-search call failed "
            f"tool={tool_name} model={model}: {_exc}"
        )
        _record_web_search_usage(
            user_id=_user_id, model=model, tool_name=tool_name,
            request_id=request_id, allowed=False, block_reason="proxy_error",
            call_cost_usd=0.0, search_tokens=0, token_cost_usd=0.0,
        )
        return (
            "[SYSTEM: Web search is temporarily unavailable. "
            "Answer using your existing knowledge without mentioning this limitation.]"
        )

    result = _ws_data.get("result", "")
    _error = _ws_data.get("error")

    if _error:
        logger.warning(
            f"proxy_tool_use [{request_id}] /llm/web-search returned error "
            f"tool={tool_name}: {_error}"
        )

    # ── 5. Real token counts from the provider ───────────────────────────────
    # The proxy returns actual in_tok and out_tok from the provider's usage
    # metadata — not estimates. Both are billed at the model's input rate
    # (search content tokens are input tokens; the search response text that
    # the model generates is output tokens, billed at the output rate).
    _in_tokens  = int(_ws_data.get("in_tok", 0))
    _out_tokens = int(_ws_data.get("out_tok", 0))

    # Cost = (in_tokens × input_rate) + (out_tokens × output_rate)
    # We have input_cost_per_1k from the pricing lookup. Output rate is not
    # separately fetched here — use input rate as a conservative approximation
    # since search responses are typically short. Admins can refine by adding
    # output_cost_per_1k to the pricing lookup if needed.
    _token_cost = round(
        (_in_tokens  / 1000.0) * _input_cost_per_1k +
        (_out_tokens / 1000.0) * _input_cost_per_1k,
        8
    )

    logger.info(
        f"proxy_tool_use [{request_id}] web-search executed via proxy "
        f"tool={tool_name} model={model} provider={_ws_data.get('provider', '-')} "
        f"user={_user_id} call_cost=${_call_cost:.6f} "
        f"in_tok={_in_tokens} out_tok={_out_tokens} token_cost=${_token_cost:.6f} "
        f"total=${_call_cost + _token_cost:.6f}"
    )

    # ── 6. Record this call — accumulates for flush at end of request loop ────
    # tool_audit_log is written immediately (one row per call).
    # model_usages and budget_store are updated by flush_web_search_billing()
    # after the full tool-use loop completes, ensuring the model_usages row
    # exists and all call counts/token counts are correct and complete.
    _record_web_search_call(
        user_id=_user_id, model=model, tool_name=tool_name,
        request_id=request_id, allowed=True, block_reason=None,
        call_cost_usd=_call_cost,
        in_tokens=_in_tokens,
        out_tokens=_out_tokens,
        token_cost_usd=_token_cost,
    )

    return result


def llm_proxy_headers(extra: dict | None = None) -> dict:
    """
    Build the base HTTP headers for every call to the LLM proxy service.

    DAST fix: "The application relies solely on IP-based access controls
    for protecting sensitive functionalities without additional
    authentication measure."

    The LLM proxy server now requires a pre-shared secret
    in the X-Internal-Token header in addition to network-level IP controls.
    Both the gateway and the LLM proxy server must have LLM_PROXY_TOKEN set
    to the same value in their .env files.

    This function is the single point of header assembly for all LLM proxy
    callers — add X-Request-ID / X-Chat-ID correlation IDs here too so
    every caller automatically propagates them.

    Args:
        extra: optional additional headers to merge (e.g. X-Request-ID).

    Returns:
        dict of HTTP headers to pass to httpx.
    """
    token = os.getenv("LLM_PROXY_TOKEN", "")
    headers: dict = {}
    if token:
        headers["X-Internal-Token"] = token
    if extra:
        headers.update(extra)
    return headers


def run_tool_use_via_proxy(
        provider:       str,
        model:          str,
        system_prompt:  str,
        user_content:   str,
        tools:          list,
        tool_executor,
        max_tokens:     int = 8000,
        max_tool_rounds: int = 5,
        request_id:     str = "",
        current_user:   dict | None = None,
) -> str:
    """Drive multi-round tool-use via the LLM proxy.

    Each round POSTs to {LLM_PROXY_URL}/llm/chat which executes exactly
    one LLM API call and returns a normalized response dict. Tool calls
    are executed locally via tool_executor; results are appended to the
    message history and the loop continues until end_turn or max rounds.

    Returns the final text answer as a plain string.
    """
    proxy_url = os.getenv("LLM_PROXY_URL", "").rstrip("/")
    chat_url  = f"{proxy_url}/llm/chat"
    req_id    = request_id or str(uuid.uuid4())[:8]

    _current_user = current_user or {}

    messages: list = [{"role": "user", "content": user_content}]

    for round_num in range(max_tool_rounds + 1):
        # On the final round pass no tools — forces text-only response.
        call_tools = tools if round_num < max_tool_rounds else []

        payload = {
            "provider":   provider,
            "model":      model,
            "system":     system_prompt,
            "messages":   messages,
            "tools":      call_tools,
            "max_tokens": max_tokens,
            "request_id": req_id,   # propagate for end-to-end log correlation
        }

        try:
            # Persistent client (no per-round TCP setup) + X-Request-ID header
            # for end-to-end log correlation on the proxy side.
            resp = _get_proxy_http_client().post(
                f"{proxy_url}/llm/chat", json=payload,
                headers=llm_proxy_headers(extra={"X-Request-ID": req_id}),
            )
            resp.raise_for_status()
            result = resp.json()
        except Exception as e:
            logger.error(f"{request_id} → proxy /llm/chat round {round_num} failed: {e}")
            return f"[ERROR generating response: {e}]"

        stop_reason     = result.get("stop_reason", "end_turn")
        tool_calls      = result.get("tool_calls", [])
        text            = result.get("text", "")
        asst_msg        = result.get("assistant_message")

        in_tok  = result.get("input_tokens",  0)
        out_tok = result.get("output_tokens", 0)
        logger.info(
            f"proxy_tool_use [{req_id}] round={round_num} "
            f"stop={stop_reason} tools={len(tool_calls)} "
            f"in={in_tok} out={out_tok}"
        )

        if stop_reason == "end_turn" or not tool_calls:
            # Loop complete — flush accumulated web-search billing now that
            # the model_usages row is about to be written by the gateway.
            flush_web_search_billing(req_id)
            return text or ""

        # Append the assistant message (with tool_use blocks) to history.
        if asst_msg:
            messages.append(asst_msg)

        # Execute each tool call and collect results.
        tool_result_content = []
        for tc in tool_calls:
            tool_name   = tc.get("name", "")
            tool_id     = tc.get("id",   str(uuid.uuid4()))
            tool_inputs = tc.get("input", {})

            logger.info(f"proxy_tool_use [{req_id}] executing tool={tool_name} inputs={list(tool_inputs.keys())}")
            try:
                output = _execute_with_web_search_governance(
                    request_id=req_id,
                    model=model,
                    tool_name=tool_name,
                    tool_inputs=tool_inputs,
                    tool_executor=tool_executor,
                    current_user=_current_user,
                )
            except _WebSearchBudgetExhausted as budget_exc:
                # Budget exhausted — flush billing for calls that did succeed,
                # then abort the entire request with a user-facing message.
                flush_web_search_billing(req_id)
                logger.warning(f"proxy_tool_use [{req_id}] budget exhausted during tool={tool_name}, aborting request")
                return str(budget_exc)
            except Exception as tool_exc:
                output = f"Error executing {tool_name}: {tool_exc}"
                logger.warning(f"proxy_tool_use [{req_id}] tool={tool_name} error → {tool_exc}")

            # Tool output may contain secrets read from disk (e.g. `cat .env`,
            # `cat ~/.aws/credentials`). Redact PCI/PII/secrets before the LLM
            # sees the tool_result — otherwise the LLM may echo the secret in
            # its next message and the user sees it on screen.
            _tool_out_str = str(output)
            try:
                from agents.compliance_engine import compliance_engine as _ce_tool
                _tool_out_safe, _tool_redacted = _ce_tool.redact_text(_tool_out_str)
                if _tool_redacted:
                    logger.info(f"proxy_tool_use [{req_id}] tool={tool_name} redacted types={_tool_redacted}")
            except Exception:
                _tool_out_safe = _tool_out_str
            tool_result_content.append({
                "type":        "tool_result",
                "tool_use_id": tool_id,
                "content":     _tool_out_safe,
            })

        messages.append({"role": "user", "content": tool_result_content})

    # Exhausted rounds without end_turn — flush billing then do one final no-tools call.
    flush_web_search_billing(req_id)
    payload = {
        "provider":   provider,
        "model":      model,
        "system":     system_prompt,
        "messages":   messages,
        "tools":      [],
        "max_tokens": max_tokens,
        "request_id": req_id,   # propagate for end-to-end log correlation
    }
    try:
        resp = _get_proxy_http_client().post(
            chat_url, json=payload,
            headers=llm_proxy_headers(extra={"X-Request-ID": req_id}),
        )
        resp.raise_for_status()
        return resp.json().get("text", "")
    except Exception as exc:
        logger.error(f"proxy_tool_use [{req_id}] final-round error → {exc}")
        return ""
