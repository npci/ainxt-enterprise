# SPDX-License-Identifier: MIT
# ============================================================
# core.kv.queue — backend-agnostic RQ job-queue factory.
#
# Picks rq.Queue / rq.Worker (and rq_scheduler.Scheduler if ever
# needed) when DB5 is configured for REDIS; picks the SPEC §12
#
# All worker / enqueue code should import from this module rather
# than `rq` / `redis` directly. The actual swap of call sites
# (core/job_queue.py, workers/*.py) happens in Phase 4.
# ============================================================

from __future__ import annotations

from threading import RLock
from typing import Any, Optional

from core.config import RDB_QUEUE, kv_backend_for, REDIS_HOST, REDIS_PORT, REDIS_PASSWORD
from core.logger import logger

_lock = RLock()
_queue_cache: dict[str, Any] = {}
_connection_singleton: Any = None


def _get_redis_connection():
    """Return a redis-py connection bound to DB5 (the queue DB)."""
    global _connection_singleton
    if _connection_singleton is None:
        import redis as _redis
        _connection_singleton = _redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=RDB_QUEUE,
            password=REDIS_PASSWORD or None,
            socket_connect_timeout=2,
        )
    return _connection_singleton



def get_job_connection() -> Any:
    """
    Return the underlying connection object suitable for passing to
    rq.Queue / rq.Worker / rq_scheduler.Scheduler.

    Returns a redis.Redis instance. Kept as a factory so a future backend can
    be added here rather than at every call site.
    """
    kv_backend_for(RDB_QUEUE)   # validates configuration, raises on removed backends
    return _get_redis_connection()


def get_queue(name: str = "default") -> Any:
    """
    Return a cached Queue object for ``name``. None if the backend is
    unavailable — callers treat a missing queue as "queueing disabled".
    """
    with _lock:
        existing = _queue_cache.get(name)
        if existing is not None:
            return existing

        backend = kv_backend_for(RDB_QUEUE)
        try:
            import rq
            q = rq.Queue(name, connection=_get_redis_connection())
        except Exception as exc:
            logger.warning(
                "kv_queue_init_failed",
                queue=name,
                backend=backend,
                error=str(exc),
            )
            return None

        _queue_cache[name] = q
        return q


def get_worker(
    queues: list[str],
    *,
    job_execution_timeout: int | None = None,
) -> Any:
    """
    Construct an rq.Worker (or RC equivalent) bound to the given queues.
    Returns None if the backend cannot be initialized.

    ``job_execution_timeout`` (seconds) is a hard cap per job inside the
    work-horse. RQ sends SIGALRM cleanly when exceeded, which lets the
    worker report the failure and pick up the next job. If the backend's
    Worker class does not accept this kwarg the call falls back to
    constructing the worker without it.
    """
    backend = kv_backend_for(RDB_QUEUE)
    try:
        if backend == "REDIS":
            import rq
            conn = _get_redis_connection()
            q_objs = [rq.Queue(n, connection=conn) for n in queues]
            kwargs = {"connection": conn}
            if job_execution_timeout is not None:
                kwargs["job_execution_timeout"] = job_execution_timeout
            try:
                return rq.Worker(q_objs, **kwargs)
            except TypeError:
                # Older rq versions don't know about job_execution_timeout
                return rq.Worker(q_objs, connection=conn)
    except Exception as exc:
        logger.warning(
            "kv_worker_init_failed",
            queues=queues,
            backend=backend,
            error=str(exc),
        )
        return None


def get_scheduler(queue_name: str) -> Any:
    """
    Return a Scheduler instance compatible with rq-scheduler's API.

    Backed by rq_scheduler.Scheduler.

    The codebase does not yet use rq-scheduler; this factory exists so
    new scheduling features can be added without backend-specific code.
    """
    backend = kv_backend_for(RDB_QUEUE)
    try:
        from rq_scheduler import Scheduler
        return Scheduler(queue_name=queue_name, connection=_get_redis_connection())
    except ImportError as exc:
        logger.warning(
            "kv_scheduler_unavailable",
            queue=queue_name,
            backend=backend,
            error=str(exc),
        )
        return None
