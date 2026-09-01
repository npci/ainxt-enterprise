# SPDX-License-Identifier: Apache-2.0
"""
core/generation_registry.py
============================
Thread-safe, in-process registry for active AI generation requests.

Design
------
Each active /ask request registers itself here with its request_id.
The registry stores a threading.Event (stop_event) per request_id.

When the user clicks "Stop Generating":
  1. Frontend calls  POST /chat/stop  { "request_id": "<id>" }
  2. The stop endpoint calls  generation_registry.request_stop(request_id)
  3. This sets the threading.Event for that request_id.
  4. The streaming generator in gateway.py checks  stop_event.is_set()
     on every token iteration and breaks out of the loop when True.

Why threading.Event instead of asyncio.Event?
  The streaming generators in gateway.py are synchronous generator
  functions (def response_stream(), def _general_stream(), etc.) that
  run inside FastAPI's StreamingResponse.  They execute in a thread
  pool, not in the async event loop, so threading.Event is the correct
  primitive.  asyncio.Event would require the same event loop, which
  is not guaranteed across threads.

Multi-worker note
-----------------
This registry is in-process only.  If you run multiple Gunicorn/Uvicorn
workers (processes), a stop request may land on a different worker than
the one holding the active stream.  For single-worker deployments (the
common case with async Uvicorn) this is transparent.

For multi-worker deployments, replace the in-memory dict with a Redis
key (e.g. "gen:stop:<request_id>" with a short TTL) and check it in
the generator loop.  The Redis-backed variant is provided as
`request_stop_redis()` / `is_stopped_redis()` below and can be
activated by setting the env var  STOP_BACKEND=redis.
"""

import os
import threading
import time
from typing import Dict, Optional

from core.logger import logger

# ── In-process registry ───────────────────────────────────────────────────────

_lock: threading.Lock = threading.Lock()
_registry: Dict[str, threading.Event] = {}   # request_id → stop_event
_registered_at: Dict[str, float] = {}        # request_id → epoch seconds

# Auto-expire entries older than this to prevent unbounded growth
_TTL_SECONDS = 600   # 10 minutes — well beyond any realistic LLM response


def register(request_id: str) -> threading.Event:
    """
    Register a new active generation request.
    Returns the threading.Event the generator should poll.
    Call deregister() in the generator's finally block.
    """
    event = threading.Event()
    with _lock:
        _registry[request_id] = event
        _registered_at[request_id] = time.monotonic()
        _evict_expired_locked()
    logger.debug(f"[gen_registry] registered request_id={request_id}")
    return event


def deregister(request_id: str) -> None:
    """Remove a completed/cancelled request from the registry."""
    with _lock:
        _registry.pop(request_id, None)
        _registered_at.pop(request_id, None)
    logger.debug(f"[gen_registry] deregistered request_id={request_id}")


def request_stop(request_id: str) -> bool:
    """
    Signal the generator for *request_id* to stop.
    Returns True if the request was found and signalled, False if not found
    (already completed, wrong worker, or invalid id).
    """
    with _lock:
        event = _registry.get(request_id)
    if event is None:
        logger.info(f"[gen_registry] stop requested but request_id not found: {request_id}")
        return False
    event.set()
    logger.info(f"[gen_registry] stop signalled for request_id={request_id}")
    return True


def is_stopped(request_id: str) -> bool:
    """Return True if a stop has been requested for this request_id."""
    with _lock:
        event = _registry.get(request_id)
    return event is not None and event.is_set()


def _evict_expired_locked() -> None:
    """Remove stale entries (called while _lock is held)."""
    now = time.monotonic()
    expired = [
        rid for rid, ts in _registered_at.items()
        if now - ts > _TTL_SECONDS
    ]
    for rid in expired:
        _registry.pop(rid, None)
        _registered_at.pop(rid, None)
    if expired:
        logger.debug(f"[gen_registry] evicted {len(expired)} expired entries")


# ── Redis-backed variant (for multi-worker deployments) ───────────────────────

_STOP_BACKEND = os.getenv("STOP_BACKEND", "memory").lower().strip()
_REDIS_STOP_TTL = 120   # seconds — stop flag lives 2 min in Redis


def request_stop_redis(request_id: str) -> bool:
    """
    Write a stop flag to the KV store (DB=7).  Works across multiple
    Gunicorn workers.  Activate by setting STOP_BACKEND=redis in the
    environment.  Backend (Redis) is chosen via
    REDIS_CLIENT_CONFIG_DB7.
    """
    try:
        from core.config import RDB_EMBED
        from core.kv import get_kv
        _r = get_kv(RDB_EMBED, decode_responses=True)
        _r.setex(f"gen:stop:{request_id}", _REDIS_STOP_TTL, "1")
        logger.info(f"[gen_registry/redis] stop flag set for request_id={request_id}")
        return True
    except Exception as exc:
        logger.warning(f"[gen_registry/redis] failed to set stop flag: {exc}")
        return False


def is_stopped_redis(request_id: str) -> bool:
    """Check the KV store for a stop flag.  Used by generators when STOP_BACKEND=redis."""
    try:
        from core.config import RDB_EMBED
        from core.kv import get_kv
        _r = get_kv(RDB_EMBED, decode_responses=True)
        return bool(_r.get(f"gen:stop:{request_id}"))
    except Exception:
        return False


# ── Unified API (auto-selects backend) ───────────────────────────────────────

def stop(request_id: str) -> bool:
    """
    Signal stop for *request_id* using the configured backend.
    Always also sets the in-memory flag so same-worker generators
    are interrupted even when STOP_BACKEND=redis.
    """
    in_mem = request_stop(request_id)
    if _STOP_BACKEND == "redis":
        request_stop_redis(request_id)
    return in_mem


def should_stop(request_id: str) -> bool:
    """
    Check whether generation should stop for *request_id*.
    Checks in-memory first (fast path), then Redis if configured.
    """
    if is_stopped(request_id):
        return True
    if _STOP_BACKEND == "redis":
        return is_stopped_redis(request_id)
    return False
