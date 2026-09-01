# SPDX-License-Identifier: Apache-2.0
# ============================================================
# JOB QUEUE — rq-backed async job execution with priority queues
#
# Priority queues (processed in order by workers):
#   high_priority  — interactive chat callbacks, approval responses
#   default        — agent single-turn runs
#   sdlc_queue     — SDLC pipelines  (long-running, non-blocking)
#   agent_queue    — alias → default
#
# IMPORTANT: No thread fallback.  If Redis/rq is unavailable the gateway
# returns 503 immediately.  Unbounded thread spawning at 100 req/s × 120s
# = 12,000 threads → Linux limit ~1024 → OOM crash.
# ============================================================

import os
import time
import uuid
from datetime import datetime
from typing import Optional

from core.config import REDIS_HOST as _REDIS_HOST, REDIS_PORT as _REDIS_PORT, RDB_QUEUE, kv_backend_for
from core.kv import get_kv
from core.kv.queue import get_job_connection as _kv_get_job_connection, get_queue as _kv_get_queue
from core.logger import logger


def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to `default` on missing/blank/invalid.

    Mirrors the SDLC model-override convention: an invalid value logs a warning
    and falls back to the code default rather than crashing the process.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"job_queue: invalid {name}={raw!r} — using default {default}")
        return default


# ── SDLC segment timeout floor ───────────────────────────────────────────────
# An SDLC worker segment chains several ainxt-CLI subprocess calls back to back
# (PLAN, IMPLEMENT, and one REVIEW fix-round), and EACH is itself allowed to run
# up to SDLC_CLI_TIMEOUT_SECS (default 1800s) of wall-clock. If the rq job_timeout
# is <= a single CLI timeout, one CLI call can consume the whole segment and rq
# SIGKILLs the job before REVIEW / the fix-round CLI ever spawn — the observed
# "implementation timed out after 30 min, no fix-round CLI spun off" failure.
_SDLC_JOB_TIMEOUT_DEFAULT = 5400   # 90 min — fits PLAN + IMPLEMENT + fix-round + build/commit
_SDLC_JOB_CLI_MULTIPLE    = 3      # segment can chain up to 3 CLI calls, each ≤ SDLC_CLI_TIMEOUT_SECS
_SDLC_ACTIVE_TTL_DEFAULT = 28800   # 8 hours

def _effective_sdlc_job_timeout() -> int:
    """Effective rq job_timeout for an SDLC segment.

    Floors SDLC_JOB_TIMEOUT_SECS at `_SDLC_JOB_CLI_MULTIPLE × SDLC_CLI_TIMEOUT_SECS`
    so a single ainxt-CLI phase can never starve the REVIEW gate + its bounded
    fix-round CLI that run later in the SAME segment. Both env vars are read at
    call time (no worker restart to change them)."""
    configured  = _env_int("SDLC_JOB_TIMEOUT_SECS", _SDLC_JOB_TIMEOUT_DEFAULT)
    cli_timeout = _env_int("SDLC_CLI_TIMEOUT_SECS", 1800)
    floor = _SDLC_JOB_CLI_MULTIPLE * cli_timeout
    if configured < floor:
        logger.warning(
            f"job_queue: SDLC_JOB_TIMEOUT_SECS={configured}s is below the "
            f"{_SDLC_JOB_CLI_MULTIPLE}×SDLC_CLI_TIMEOUT_SECS floor ({floor}s); a single "
            f"CLI phase (PLAN/IMPLEMENT/fix-round) could starve REVIEW. Using {floor}s."
        )
        return floor
    return configured


# ── Queue name constants ──────────────────────────────────────

Q_HIGH     = "high_priority"
Q_DEFAULT  = "default"
Q_CHAT     = "chat_queue"      # interactive chat (Redis Stream SSE path)
Q_SDLC     = "sdlc_queue"      # SDLC pipelines — long-running, LLM-heavy
Q_AGENT    = "agent_queue"     # named-agent runs
Q_INDEX    = "index_queue"     # codebase indexing — CPU/IO heavy
Q_KB       = "kb_queue"        # knowledge-base doc ingest
Q_SECURITY = "security_queue"  # security scans (SonarQube/Checkmarx/PMD/CPD)
Q_DOC       = "doc_queue"           # document generation (docx/pptx/pdf/xlsx/txt/md)
Q_CODEWIKI  = "codewiki_queue"      # CodeWiki codebase documentation generation
Q_CONNECTOR = "connector_queue"    # async connector tool calls (heavy: email search, transcripts)
Q_EXEC      = "exec_queue"          # Cowork run_code sandbox (Docker) — isolated from gateway
Q_DISCUSSIONS = "discussions_queue" # Discussions module @AiNxt bot replies (own worker, services/discussions_svc/)
Q_COACH     = "coach_queue"         # Coach evaluator jobs (weekly digest, nudges)
Q_DLQ       = "dead_letter_queue"  # permanently failed jobs land here

# Ordered list workers should consume (highest → lowest priority)
ALL_QUEUES = [Q_HIGH, Q_DEFAULT, Q_CHAT, Q_AGENT, Q_SDLC, Q_INDEX, Q_KB, Q_SECURITY, Q_DOC, Q_CODEWIKI, Q_CONNECTOR, Q_EXEC, Q_COACH]

# ── Back-pressure limits (reject enqueue if queue depth exceeds these) ──────
_QUEUE_DEPTH_LIMITS: dict[str, int] = {
    Q_HIGH:       1000,
    Q_DEFAULT:     500,
    Q_CHAT:        500,
    Q_AGENT:       100,
    Q_SDLC:        _env_int("SDLC_QUEUE_DEPTH", 100),   # max QUEUED sdlc jobs; override via SDLC_QUEUE_DEPTH (read at import — restart workers/gateway to change)
    Q_INDEX:       200,
    Q_KB:          100,
    Q_SECURITY:     50,   # one scan per PR, bounded by PR rate
    Q_DOC:         500,   # doc generation — 32 workers × deep queue for parallel complex jobs
    Q_CONNECTOR:   100,   # async connector queries (heavy email/calendar/transcript fetches)
    Q_EXEC:        200,   # run_code sandbox — queue depth; true concurrency = # exec workers
    Q_COACH:       500,   # coach ingest/evaluate — light, fire-and-forget
    Q_DISCUSSIONS: 500,   # @AiNxt mention replies (own worker) — configurable via ANSWER_QUEUE_MAX_DEPTH-style env if needed
}

# ── Queue backend connection ──────────────────────────────────
# DB=5 backend selected via REDIS_CLIENT_CONFIG_DB5.
#   REDIS  → rq.Queue / rq.Worker against redis.Redis(db=5)
# Connection plumbing lives in core.kv.queue.

_rq_available = False
_queues: dict = {}
_redis_conn = None
_queue_backend = "REDIS"

try:
    _queue_backend = kv_backend_for(RDB_QUEUE)
    _redis_conn = _kv_get_job_connection()
    # For Redis we can ping eagerly; for RC the wrapper handles connectivity.
    if hasattr(_redis_conn, "ping"):
        try:
            _redis_conn.ping()
        except Exception:
            pass
    _rq_available = _redis_conn is not None
    if _rq_available:
        logger.info(f"job_queue: queue backend ready — backend={_queue_backend} db={RDB_QUEUE}")
    else:
        logger.warning("job_queue: queue connection unavailable. All enqueue calls will return 503.")
except Exception as _e:(
    logger.warning(f"job_queue: queue backend unavailable → {_e}. All enqueue calls will return 503."))


# ── Queue factory ─────────────────────────────────────────────

def get_queue(name: str = Q_DEFAULT):
    """Return a Queue by name (cached). None if backend unavailable.

    Returns an rq.Queue. Routed through core.kv.queue rather than
    constructed here so the enqueue / __len__ surface used by the rest
    of this module stays independent of the queue backend.
    """
    if not _rq_available:
        return None
    if name not in _queues:
        q = _kv_get_queue(name)
        if q is None:
            return None
        _queues[name] = q
    return _queues[name]


# ── Atomic back-pressure check (Lua — prevents TOCTOU race) ──
#
# Both gateway instances call this concurrently under Nginx LB.
# Simple llen() check has a race: instance A reads depth=499, instance B reads
# depth=499, both enqueue → depth=501.  The Lua script runs atomically
# inside Redis (single-threaded) so only one of the two sees 499 and
# enqueues; the other sees 500 and is rejected.

_ATOMIC_ENQUEUE_SCRIPT = """
local q_key   = KEYS[1]
local limit   = tonumber(ARGV[1])
local current = redis.call('LLEN', q_key)
if current >= limit then
    return -1
end
return current
"""
_lua_check = None


def _check_depth_atomic(rq_queue) -> bool:
    """
    Returns True if enqueue is allowed (depth < limit), False if queue is full.

    The atomicity check uses Lua via core.kv.KVClient.register_script, which
    routes to redis-py EVALSHA — this eliminates the TOCTOU race
    between two gateway instances enqueueing at the same time. If the script handle
    cannot be obtained for any reason we fall back to a non-atomic LLEN.
    """
    global _lua_check
    q_name = rq_queue.name
    limit  = _QUEUE_DEPTH_LIMITS.get(q_name, 1000)
    try:
        if _lua_check is None:
            _kv = get_kv(RDB_QUEUE, decode_responses=False)
            _lua_check = _kv.register_script(_ATOMIC_ENQUEUE_SCRIPT)
        result = _lua_check(keys=[rq_queue.key], args=[limit])
        # Result is an int (redis) or possibly bytes/str (some RC SDKs).
        try:
            result = int(result)
        except (TypeError, ValueError):
            pass
        return result != -1
    except Exception:
        pass
        # Fallback: non-atomic but at least checked
    try:
        return len(rq_queue) < limit
    except Exception:
        return True   # if we cannot measure depth at all, allow the enqueue


# ── Core enqueue ──────────────────────────────────────────────

def enqueue_job(
    fn_name:    str,
    payload:    dict,
    queue_name: str            = Q_DEFAULT,
    timeout:    int | None     = 900,    # 15 min hard cap; None disables rq timeout
    retry_count: int           = 2,
    retry_interval: list       = None,   # seconds between retries
    job_id:     str | None     = None,   # caller-supplied job_id (keeps RQ id == payload id)
) -> str:
    """
    Enqueue any job by dotted fn_name.

    Returns job_id.
    Raises RuntimeError with 503 code if RQ is unavailable — no thread fallback.

    If job_id is supplied (e.g. by doc_download_router so it can correlate the
    Redis result key with the RQ job), that exact id is used for the RQ job.
    Otherwise a fresh UUID is generated.  This prevents the "No such job" warning
    that occurs when the router polls get_job_status() with its own UUID while RQ
    stored the job under a different UUID.
    """
    if not _rq_available:
        raise RuntimeError(
            f"job_queue: RQ/Redis unavailable — cannot enqueue {fn_name}. "
            "Ensure Redis is running and rq workers are started."
        )

    retry_interval = retry_interval or [60, 180]  # 1min, 3min
    # Use caller-supplied job_id if provided (avoids RQ-id vs payload-id mismatch).
    # Fall back to payload["job_id"] if present, then generate a fresh UUID.
    if not job_id:
        job_id = payload.get("job_id") or str(uuid.uuid4())

    q = get_queue(queue_name)

    # Atomic back-pressure check
    if not _check_depth_atomic(q):
        limit = _QUEUE_DEPTH_LIMITS.get(queue_name, 1000)
        raise RuntimeError(
            f"job_queue: queue '{queue_name}' at capacity (limit={limit}). "
            "Try again later."
        )

    module_path, func_name = fn_name.rsplit(".", 1)
    import importlib
    mod = importlib.import_module(module_path)
    fn  = getattr(mod, func_name)

    rq_kwargs = {"job_id": job_id, "job_timeout": -1 if timeout is None else timeout}
    try:
        from rq import Retry
        if retry_count > 0:
            rq_kwargs["retry"] = Retry(max=retry_count, interval=retry_interval)
    except ImportError:
        pass  # older rq — no retry

    q.enqueue(fn, payload, **rq_kwargs)
    logger.info(f"job_queue: enqueued rq job {job_id} fn={fn_name} q={queue_name}")
    return job_id


# ── DLQ helper ────────────────────────────────────────────────

def move_to_dlq(job_id: str, fn_name: str, payload: dict, error: str) -> None:
    """
    Move a permanently failed job to the Dead Letter Queue.
    Called by workers after all retries are exhausted.
    The DLQ job carries the original payload + error for manual inspection.
    """
    if not _rq_available:
        logger.error(f"DLQ unavailable — lost job {job_id} ({fn_name}): {error}")
        return
    try:
        dlq = get_queue(Q_DLQ)
        from workers.dlq_worker import record_dlq_job  # no-op worker for inspection
        dlq.enqueue(
            record_dlq_job,
            {
                "original_job_id": job_id,
                "fn_name":         fn_name,
                "payload":         payload,
                "error":           error,
                "failed_at":       datetime.utcnow().isoformat(),
            },
            job_timeout=30,
        )
        logger.warning(f"job_queue: moved {job_id} to DLQ — {error[:200]}")
    except Exception as e:
        logger.error(f"job_queue: DLQ move failed for {job_id}: {e}")


# ── Back-pressure check (public API for routers) ──────────────

def check_queue_pressure(queue_name: str) -> dict:
    """
    Returns {"allowed": True} or {"allowed": False, "depth": N, "limit": N}.
    Call before enqueuing to enforce back-pressure.
    Uses atomic Lua check to prevent TOCTOU race on multi-server deployments.
    """
    limit = _QUEUE_DEPTH_LIMITS.get(queue_name, 1000)
    if not _rq_available:
        # RQ unavailable — any enqueue will 503 anyway, signal blocked here too
        return {"allowed": False, "depth": 0, "limit": limit, "queue": queue_name,
                "reason": "rq_unavailable"}
    try:
        q = get_queue(queue_name)
        if not _check_depth_atomic(q):
            depth = len(q)
            return {"allowed": False, "depth": depth, "limit": limit, "queue": queue_name}
    except Exception:
        pass
    return {"allowed": True}


# ── Convenience wrappers ──────────────────────────────────────

def enqueue_chat_job(
    job_id:       str,
    question:     str,
    session_id:   str,
    chat_id:      str,
    repo_filter:  str | None  = None,
    model:        str | None  = None,
    user_id:      str         = "default",
    user_ctx:     dict | None = None,
    request_id:   str         = "",
    trace_headers: dict | None = None,
    rag_mode:       str | None  = None,
    attachment_ids: list | None = None,
) -> str:
    """Enqueue a chat pipeline job (Redis Stream SSE path). Returns job_id."""
    payload = {
        "job_id":        job_id,
        "question":      question,
        "session_id":    session_id,
        "chat_id":       chat_id,
        "repo_filter":   repo_filter,
        "model":         model,
        "user_id":       user_id,
        "user_ctx":      user_ctx,
        "request_id":    request_id or job_id,
        "trace_headers": trace_headers or {},   # W3C traceparent for OTel context propagation
        "rag_mode":        rag_mode,              # context isolation: off | auto | on
        "attachment_ids":  list(attachment_ids or []),  # uploaded file IDs for doc generation
        # Enqueue time so the worker's latency includes queue-wait.
        "enqueued_at":     time.time(),
    }
    return enqueue_job(
        "workers.chat_worker.run_chat_job",
        payload,
        queue_name=Q_CHAT,
        timeout=120,
        retry_count=0,   # never retry chat — stale streams confuse users
    )


def enqueue_index_job(
    repo_name:    str,
    repo_path:    str,
    triggered_by: str  = "system",
    drop_index:   bool = False,
    file_filter:  list | None = None,
    product_id:   str  = "",
    department:   str  = "",
    request_id:   str  = "",
    branch:       str  = "",
) -> str:
    """Enqueue a codebase indexing job. Returns job_id.

    Pre-enqueue dedup: if the distributed lock for this repo is already held
    (meaning a worker is actively indexing it), we refuse to enqueue a second
    job.  This prevents duplicate jobs from piling up in the queue and avoids
    the confusing 'skipping duplicate job' log storm that arises when multiple
    jobs are enqueued for the same repo within a short window.
    """
    import json as _json
    _lock_r = get_kv(RDB_QUEUE, decode_responses=True)
    # Scope the distributed lock to (repo_name, product_id, branch) so that the
    # same repo can be indexed independently for different products or branches.
    _product_slug = (product_id or "").strip() or "default"
    _branch_slug  = (branch or "main").strip().replace("/", "_")
    lock_key = f"index:lock:{repo_name}:{_product_slug}:{_branch_slug}"
    try:
        existing = _lock_r.get(lock_key)
        if existing:
            try:
                info = _json.loads(existing)
            except Exception:
                info = {"raw": existing}
            logger.warning(
                f"job_queue: refusing to enqueue index job for '{repo_name}' — "
                f"lock already held by request_id={info.get('request_id', '?')} "
                f"triggered_by={info.get('triggered_by', '?')} "
                f"started_at={info.get('started_at', '?')}"
            )
            return ""
    except Exception as _le:
        logger.warning(f"job_queue: could not check index lock for '{repo_name}': {_le} — enqueuing anyway")

    payload = {
        "repo_name":    repo_name,
        "repo_path":    repo_path,
        "triggered_by": triggered_by,
        "drop_index":   drop_index,
        "file_filter":  file_filter,
        "product_id":   product_id,
        "department":   department,
        "request_id":   request_id,
        "branch":       branch,
    }
    return enqueue_job(
        "workers.index_worker.index_repo_job",
        payload,
        queue_name=Q_INDEX,
        timeout=86400,   # 24 hours — 100k+ vector repos run 10+ hours
        retry_count=0,   # no auto-retry — indexing runs for hours; silent retry wastes compute
                         # and creates phantom duplicate-request_id logs; re-trigger manually
    )


def check_sdlc_admission(jira_key: str, reporter: str) -> dict:
    """Read-only pre-flight mirror of enqueue_sdlc_job's two admission guards.

    Lets a router refuse (or short-circuit) BEFORE it persists a run row, so a
    rate-limited or duplicate trigger never leaves an orphan run with no job.
    Mutates NO counters — enqueue_sdlc_job's in-line guards remain authoritative
    (and also protect the webhook path that has no router pre-check).

    Returns one of:
      {"allowed": True}
      {"allowed": True, "existing_job_id": <val>, "dedup": True}   (Jira dedup hit)
      {"allowed": False, "reason": <msg>, "active_count": N, "limit": N}

    Fail-open: any Redis/KV hiccup returns {"allowed": True} so a transient blip
    never blocks a legitimate trigger (the in-enqueue guard stays authoritative).
    """
    _SDLC_USER_LIMIT = _env_int("SDLC_USER_LIMIT", 3)
    reporter = (reporter or "unknown").lower().strip()

    if not _rq_available:
        # enqueue itself will 503; do not block here spuriously.
        return {"allowed": True}

    try:
        _kv = get_kv(RDB_QUEUE, decode_responses=True)

        # ── Guard 1: Jira ticket dedup (no-op success, NOT a rejection) ──
        if jira_key:
            existing_job_id = _kv.get(f"sdlc:active:{jira_key}")
            if existing_job_id:
                return {"allowed": True, "existing_job_id": existing_job_id, "dedup": True}

        # ── Guard 2: Per-reporter active-job limit ──
        if reporter:
            current = _kv.get(f"sdlc:user_active:{reporter}")
            active_count = int(current) if current else 0
            if active_count >= _SDLC_USER_LIMIT:
                return {
                    "allowed": False,
                    "reason": (
                        f"sdlc rate-limit: {reporter!r} already has "
                        f"{active_count} active SDLC jobs (limit={_SDLC_USER_LIMIT}). "
                        "Wait for one to complete before triggering another."
                    ),
                    "active_count": active_count,
                    "limit": _SDLC_USER_LIMIT,
                }
    except Exception:
        # Fail-open: a Redis hiccup must never block a legit trigger.
        return {"allowed": True}

    return {"allowed": True}


def enqueue_sdlc_job(fn_name: str, payload: dict, queue_name: str = Q_SDLC) -> str:
    """
    Enqueue an SDLC pipeline job with three safety guards:

    1. Jira ticket deduplication  — if the same jira_key already has an active
       SDLC job (Redis key sdlc:active:{jira_key} exists), return the existing
       job_id without re-enqueuing.  TTL = SDLC_ACTIVE_TTL_SECS (default 8h); the
       HITL watchdog renews it every 15 min while a run is parked at a gate, so
       it survives multi-day human review without ever growing the base TTL.

    2. Per-reporter rate limit — a single reporter (jira assignee email or
       'unknown') may not have more than _SDLC_USER_LIMIT concurrent active jobs.
       Enforced via Redis INCR counter sdlc:user_active:{reporter} (same TTL,
       likewise renewed by the watchdog via refresh_sdlc_slot).

    3. Job-level timeout — hard cap enforced by rq (see _effective_sdlc_job_timeout).
       Individual pipeline segments should complete well within this window.
    """
    # Read at call time so changes take effect without restarting the gateway.
    # SDLC_USER_LIMIT: max concurrent SDLC jobs per reporter (per-person, not global).
    # SDLC_ACTIVE_TTL_SECS: base TTL on the dedup slot + per-reporter counter. It
    #   no longer has to outlast the whole pipeline+gate — the HITL watchdog
    #   RENEWS it (refresh_sdlc_slot) every 15 min while a run waits at a gate.
    _SDLC_USER_LIMIT  = _env_int("SDLC_USER_LIMIT", 3)
    _SDLC_ACTIVE_TTL  = _env_int("SDLC_ACTIVE_TTL_SECS", _SDLC_ACTIVE_TTL_DEFAULT)

    jira_key = payload.get("key", "")
    reporter  = (
        payload.get("assignee") or payload.get("reporter")
        or payload.get("triggered_by_email") or payload.get("triggered_by_user_id")
        or "unknown"
    ).lower().strip()

    # Use the KV client for dedup bookkeeping rather than the raw rq
    # connection, which is backend-specific.
    _kv = get_kv(RDB_QUEUE, decode_responses=True)

    # ── Guard 1: Jira ticket dedup ────────────────────────────
    if _rq_available and jira_key:
        dedup_key = f"sdlc:active:{jira_key}"
        existing_job_id = _kv.get(dedup_key)
        if existing_job_id:
            logger.info(
                f"sdlc dedup: {jira_key} already has active job "
                f"{existing_job_id} — skipping enqueue"
            )
            return existing_job_id  # type: ignore[return-value]

    # ── Guard 2: Per-reporter active-job limit ────────────────
    if _rq_available and reporter:
        user_counter_key = f"sdlc:user_active:{reporter}"
        current = _kv.get(user_counter_key)
        active_count = int(current) if current else 0
        if active_count >= _SDLC_USER_LIMIT:
            raise RuntimeError(
                f"sdlc rate-limit: {reporter!r} already has "
                f"{active_count} active SDLC jobs (limit={_SDLC_USER_LIMIT}). "
                "Wait for one to complete before triggering another."
            )

    # SDLC job hard cap. Floored at a multiple of the CLI timeout so PLAN +
    # IMPLEMENT + the REVIEW fix-round (each a separate ainxt-CLI call, each ≤
    # SDLC_CLI_TIMEOUT_SECS) all fit in one segment; bump SDLC_JOB_TIMEOUT_SECS
    # further for repos with very large Maven dep graphs on a cold m2 cache.
    _sdlc_job_timeout = _effective_sdlc_job_timeout()
    job_id = enqueue_job(
        fn_name, payload, queue_name=queue_name, timeout=_sdlc_job_timeout,
        retry_count=0,  # SDLC: no retries — stale pipelines create duplicate PRs
    )

    # ── Register active job in kv (for dedup + user counter) ──
    # The slot VALUE is the run_id when the caller created the run up-front
    # (UI / Threads triggers set payload["_run_id"]); otherwise it falls back
    # to the rq job_id (webhook path creates the run lazily inside the worker).
    # cancel_run + the worker `finally` both compare-and-delete against this
    # same owner token so a stale/zombie worker can never clear a slot that has
    # since been claimed by a re-triggered run for the same Jira.
    slot_owner = payload.get("_run_id") or job_id
    if _rq_available and job_id:
        if jira_key:
            _kv.setex(f"sdlc:active:{jira_key}", _SDLC_ACTIVE_TTL, slot_owner)
        if reporter:
            with _kv.pipeline() as pipe:
                pipe.incr(f"sdlc:user_active:{reporter}")
                pipe.expire(f"sdlc:user_active:{reporter}", _SDLC_ACTIVE_TTL)
                pipe.execute()

    return job_id


# Compare-and-delete: release the dedup slot only if it still belongs to the
# given owner. Prevents a zombie/stale worker (or a cancel) from clearing a
# slot that a re-triggered run for the same Jira has already claimed.
_CAD_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
_cad_release_lua = None


def _cad_release(key: str, owner: str) -> None:
    """Delete `key` only if its current value equals `owner` (atomic via Lua)."""
    global _cad_release_lua
    try:
        if _cad_release_lua is None:
            _cad_release_lua = _redis_conn.register_script(_CAD_RELEASE_SCRIPT)
        _cad_release_lua(keys=[key], args=[owner])
    except Exception:
        # Fallback: best-effort GET+DEL (tiny race window, only on Lua failure)
        try:
            cur = _redis_conn.get(key)
            if cur is not None and cur in (owner, owner.encode()):
                _redis_conn.delete(key)
        except Exception:
            pass


def release_sdlc_slot(jira_key: str, reporter: str = None, owner: str = None) -> None:
    """
    Release the SDLC active-job slot.

    Called by sdlc_worker at the end of every pipeline (success/failure) and by
    the cancel endpoint when a run is cancelled.

    - `owner` (run_id or job_id): when given, the dedup key is compare-and-deleted
      (released only if it still points at this owner). When None, the dedup key
      is deleted unconditionally (legacy behaviour).
    - `reporter`: when truthy, the per-reporter active-job counter is decremented.
      Pass None (the cancel path) to leave the counter alone — the worker's own
      `finally` decrements it with the correct reporter once it runs/bails.
    """
    if not _rq_available:
        return
    try:
        _kv = get_kv(RDB_QUEUE, decode_responses=True)
        if jira_key:
            dedup_key = f"sdlc:active:{jira_key}"
            if owner:
                _cad_release(dedup_key, str(owner))
            else:
                _kv.delete(dedup_key)
        if reporter:
            r = reporter.lower().strip()
            if r:
                count = _kv.decr(f"sdlc:user_active:{r}")
                if count <= 0:
                    _kv.delete(f"sdlc:user_active:{r}")
    except Exception as _e:
        logger.warning(f"release_sdlc_slot: failed for {jira_key}/{reporter}: {_e}")

def refresh_sdlc_slot(jira_key: str, reporter: str = None, ttl: int = None) -> None:
    """Renew the TTL on a live gate run's dedup slot + per-reporter counter.

    Called by the HITL watchdog for every run still legitimately parked at an
    approval gate, so its dedup/rate-limit lease survives a multi-day human
    review without growing the base TTL.

    Pure EXPIRE — never INCR / SET. Redis `EXPIRE` is a no-op on a missing key,
    so this can neither double-count the reporter counter nor resurrect a slot
    that was already released. Best-effort; never raises. Uses the db=5 conn.
    """
    if not _rq_available:
        return
    _ttl = ttl if (ttl and ttl > 0) else _env_int("SDLC_ACTIVE_TTL_SECS", _SDLC_ACTIVE_TTL_DEFAULT)
    try:
        if jira_key:
            _redis_conn.expire(f"sdlc:active:{jira_key}", _ttl)
        if reporter:
            r = reporter.lower().strip()
            if r:
                _redis_conn.expire(f"sdlc:user_active:{r}", _ttl)
        logger.debug(
            "job_queue: refresh_sdlc_slot",
            jira_key=jira_key, reporter=reporter, ttl=_ttl,
        )
    except Exception as _e:
        logger.warning(f"refresh_sdlc_slot: failed for {jira_key}/{reporter}: {_e}")


def enqueue_agent_job(agent_name: str, message: str, session_id: Optional[str] = None) -> str:
    """Enqueue an agent run job. Returns job_id."""
    payload = {
        "agent_name": agent_name,
        "message":    message,
        "session_id": session_id or str(uuid.uuid4()),
    }
    return enqueue_job(
        "workers.agent_worker.run_agent_job",
        payload,
        queue_name=Q_AGENT,
        timeout=300,   # 5 min per agent run
        retry_count=1,
    )


def enqueue_coach_job(fn_name: str, payload: dict) -> str:
    """Enqueue an AiNxt Coach job (ingest/evaluate or weekly digest). Returns
    job_id. Coach work is light and idempotent; one retry is sufficient."""
    return enqueue_job(
        fn_name,
        payload,
        queue_name=Q_COACH,
        timeout=300,
        retry_count=1,
    )

def enqueue_discussions_job(run_id: str, mention_event: dict) -> str:
    """Enqueue a Discussions module @AiNxt bot reply job. Returns job_id.

    Consumed ONLY by services/discussions_svc/worker.py (own dedicated worker,
    not the shared pool) — see docs/DISCUSSIONS_MODULE_IMPLEMENTATION_PLAN.md.
    """
    payload = {"run_id": run_id, "mention_event": mention_event}
    return enqueue_job(
        "services.discussions_svc.agent_bridge.run_discussions_bot_job",
        payload,
        queue_name=Q_DISCUSSIONS,
        timeout=180,   # AgentRunner's own _AGENT_TIMEOUT_SECS=120 + margin
        retry_count=1,
    )

def enqueue_hitl_resume_job(
    fn_name: str,
    run_id: str,
    feedback: str = "",
    extra: Optional[dict] = None,
) -> str:
    """
    Enqueue a HITL resume / revision / stage-resume job onto the SDLC worker queue.
    All pipeline continuation after human approval MUST go through here —
    never run in the gateway process so that multi-instance deployments
    don't split pipeline state across hosts.

    A resume is a *continuation* of an existing run, NOT a new admission: it
    deliberately bypasses enqueue_sdlc_job's per-reporter rate-limit counter
    (sdlc:user_active:{reporter}) and Jira dedup slot. Routing resumes through
    enqueue_sdlc_job would (a) credit the continuation to reporter "unknown"
    (the resume payload carries no assignee) and (b) leak that counter — it is
    never decremented for resumes — eventually tripping the rate limit and
    blocking all further resumes for that pseudo-user.

    `extra` is merged into the worker payload for resume variants that need more
    than {run_id, feedback} (e.g. resume_from_stage_job: target_stage, mode,
    actor, reason, override_payload). It must not override run_id.
    """
    payload = {"run_id": run_id, "feedback": feedback}
    if extra:
        payload.update({k: v for k, v in extra.items() if k != "run_id"})
    # Honor the same hard cap as the initial trigger (enqueue_sdlc_job). The
    # post-approval feature/bug CODING→COMMITTING segment (and revision re-runs
    # of IMPLEMENT + REVIEW fix-round) run here, so the CLI-timeout floor applies
    # equally. Read at call time — no worker restart to change it.
    _sdlc_job_timeout = _effective_sdlc_job_timeout()
    return enqueue_job(
        fn_name,
        payload,
        queue_name=Q_SDLC,
        timeout=_sdlc_job_timeout,
        retry_count=0,   # no retries on HITL resumes — double-execution creates duplicate PRs
    )


def enqueue_pr_comments_job(run_id: str) -> str:
    """Enqueue an SDLC PR-review-address job."""
    return enqueue_job(
        "workers.sdlc_worker.address_pr_comments_job",
        {"run_id": run_id},
        queue_name=Q_SDLC,
        timeout=900,
        retry_count=2,
        retry_interval=[30, 120, 300],
    )


def enqueue_merge_pr_job(run_id: str) -> str:
    """Enqueue an SDLC merge-PR job."""
    return enqueue_job(
        "workers.sdlc_worker.merge_pr_job",
        {"run_id": run_id},
        queue_name=Q_HIGH,   # fast, merge should complete quickly
        timeout=120,
        retry_count=2,
    )


def enqueue_connector_job(
    connector_name: str,
    tool_name: str,
    params: dict,
    user_id: str,
) -> str:
    """
    Enqueue a heavy connector tool call (email search, transcripts, calendar) to the
    connector_queue. The worker publishes results to Redis Stream db=6 for SSE delivery.
    Returns job_id.
    """
    return enqueue_job(
        "workers.connector_worker.run_connector_tool",
        {
            "connector_name": connector_name,
            "tool_name":      tool_name,
            "params":         params,
            "user_id":        user_id,
        },
        queue_name=Q_CONNECTOR,
        timeout=120,    # 2 min max per connector call
        retry_count=2,
        retry_interval=[5, 30],
    )


def enqueue_security_scan_job(pr_dict: dict) -> str:
    """
    Enqueue a security scan (SonarQube + Checkmarx + PMD/CPD) for a PR.
    pr_dict must include: repo, branch, number, clone_url.
    Returns job_id.
    """
    return enqueue_job(
        "workers.security_scan_worker.run_security_scan_job",
        pr_dict,
        queue_name=Q_SECURITY,
        timeout=1800,   # 30 min — Checkmarx can be slow
        retry_count=1,
        retry_interval=[60, 300],
    )


def enqueue_codewiki_job(
    job_id: str, codebase_name: str, repo_url: str, branch: str
) -> str:
    """
    Enqueue a CodeWiki documentation-generation job.
    The job row in codewiki_doc_jobs must already exist.
    Returns the rq job_id.

    timeout=None -> no RQ job_timeout at all (job_timeout=-1 under the hood).
    A full CodeWiki generation on a large repo can legitimately take up to
    ~2 days (thousands of modules, each its own multi-step LLM agent run);
    any finite RQ timeout here would kill an otherwise still-progressing job
    partway through. workers/codewiki_worker.py's run_codewiki_doc_job()
    already catches every exception internally and marks the DB row
    'failed' without re-raising, so retry_count/retry_interval below never
    actually fire for this job type (RQ's Retry only triggers when the job
    function itself raises) -- kept at their previous values only for
    forward-compatibility if that ever changes; they are not relied on.
    """
    return enqueue_job(
        "workers.codewiki_worker.run_codewiki_doc_job",
        {
            "job_id":        job_id,
            "codebase_name": codebase_name,
            "repo_url":      repo_url,
            "branch":        branch,
        },
        queue_name=Q_CODEWIKI,
        timeout=None,     # no cap -- generation can take up to ~2 days
        retry_count=1,
        retry_interval=[30, 120],
    )


# ── Cancel ────────────────────────────────────────────────────

def cancel_job(job_id: str) -> bool:
    """Cancel a queued or started job. Returns True if cancelled."""
    if _rq_available:
        try:
            import rq
            job = rq.job.Job.fetch(job_id, connection=_redis_conn)
            job.cancel()
            logger.info(f"job_queue: cancelled rq job {job_id}")
            return True
        except Exception as e:
            logger.warning(f"job_queue: cancel job {job_id} failed → {e}")
    return False


# ── Job status ────────────────────────────────────────────────

def get_job_status(job_id: str) -> dict:
    """Return status dict for a job."""
    if _rq_available:
        try:
            import rq
            job = rq.job.Job.fetch(job_id, connection=_redis_conn)
            status_val = job.get_status()
            status_str = status_val.value if hasattr(status_val, "value") else str(status_val)
            return {
                "id":          job_id,
                "status":      status_str,
                "queue":       job.origin if hasattr(job, "origin") else "unknown",
                "result":      str(job.result) if job.result is not None else None,
                "error":       str(job.exc_info) if job.exc_info else None,
                "enqueued_at": job.enqueued_at.isoformat() if job.enqueued_at else None,
                "started_at":  job.started_at.isoformat()  if job.started_at  else None,
                "ended_at":    job.ended_at.isoformat()    if job.ended_at    else None,
            }
        except Exception as e:
            logger.warning(f"job_queue: fetch job {job_id} failed → {e}")

    return {"id": job_id, "status": "unknown", "error": "Job not found"}


# ── List jobs ─────────────────────────────────────────────────

def list_jobs(queue_name: str = Q_SDLC, limit: int = 50) -> list:
    """List recent jobs from a single queue."""
    if _rq_available:
        try:
            q = get_queue(queue_name)
            result = []
            for jid in (q.job_ids or [])[:limit]:
                result.append(get_job_status(jid))
            return result
        except Exception:
            pass
    return []


def list_all_jobs(limit: int = 100) -> list:
    """List jobs across all queues, most-recent first."""
    if _rq_available:
        result = []
        for q_name in ALL_QUEUES:
            try:
                q = get_queue(q_name)
                for jid in (q.job_ids or [])[:limit]:
                    j = get_job_status(jid)
                    j["queue"] = q_name
                    result.append(j)
            except Exception:
                pass
        # Also fetch finished/failed registries
        try:
            from rq.registry import FinishedJobRegistry, FailedJobRegistry
            for q_name in ALL_QUEUES:
                q = get_queue(q_name)
                for jid in FinishedJobRegistry(queue=q).get_job_ids()[:20]:
                    j = get_job_status(jid)
                    if j.get("status") != "unknown":
                        result.append(j)
                for jid in FailedJobRegistry(queue=q).get_job_ids()[:20]:
                    j = get_job_status(jid)
                    if j.get("status") != "unknown":
                        result.append(j)
        except Exception:
            pass
        return sorted(result, key=lambda j: j.get("enqueued_at") or "", reverse=True)[:limit]

    return []


def list_failed_jobs(limit: int = 100) -> list:
    """Return all jobs currently in the Dead Letter Queue."""
    if not _rq_available:
        return []
    try:
        dlq = get_queue(Q_DLQ)
        result = []
        for jid in (dlq.job_ids or [])[:limit]:
            result.append(get_job_status(jid))
        return result
    except Exception as e:
        logger.warning(f"job_queue: list_failed_jobs error: {e}")
        return []


def queue_stats() -> dict:
    """Return counts for all queues."""
    stats = {}
    if _rq_available:
        for q_name in ALL_QUEUES + [Q_DLQ]:
            try:
                q = get_queue(q_name)
                from rq.registry import StartedJobRegistry, FailedJobRegistry, FinishedJobRegistry
                stats[q_name] = {
                    "queued":   len(q),
                    "started":  len(StartedJobRegistry(queue=q)),
                    "finished": len(FinishedJobRegistry(queue=q)),
                    "failed":   len(FailedJobRegistry(queue=q)),
                }
            except Exception:
                stats[q_name] = {"queued": 0}
    return stats
