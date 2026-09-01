# SPDX-License-Identifier: Apache-2.0
# ============================================================
# KV factory — backend selection happens here, and only here.
#
# get_kv(db) → KVClient bound to the configured backend for that
# logical DB. Resolution order is defined in core.config.kv_backend_for:
#   REDIS_CLIENT_CONFIG_DB{n} > REDIS_CLIENT_CONFIG > "REDIS"
#
# Clients are cached per (db, decode_responses) to mirror existing
# redis connection-pool behaviour.
# ============================================================

from __future__ import annotations

from threading import RLock
from typing import Dict, Tuple

from core.config import KV_DB_COUNT, kv_backend_for
from core.logger import logger

from .async_base import AsyncKVClient
from .base import KVClient
from .redis_impl import RedisKVClient


_lock = RLock()
_cache: Dict[Tuple[int, bool], KVClient] = {}

# Async clients are cached separately because they may be tied to a
# specific asyncio event loop. Key is (db, decode_responses, loop_id);
# when called outside a running loop loop_id is 0.
_async_lock = RLock()
_async_cache: Dict[Tuple[int, bool, int], AsyncKVClient] = {}


def get_kv(db: int, *, decode_responses: bool = True) -> KVClient:
    """
    Return a cached KVClient for logical DB ``db``.

    The first call for a given (db, decode_responses) pair instantiates
    the client and caches it. Subsequent calls return the same instance.
    Backend selection is per-DB and resolved via
    core.config.kv_backend_for(db) at first instantiation.
    """
    key = (db, decode_responses)
    with _lock:
        existing = _cache.get(key)
        if existing is not None:
            return existing

        # kv_backend_for() validates the value and rejects the removed
        # RustyCluster backend with an actionable message.
        backend = kv_backend_for(db)
        client: KVClient = RedisKVClient(db=db, decode_responses=decode_responses)

        _cache[key] = client
        try:
            logger.info(
                "kv_client_created",
                db=db,
                backend=backend,
                decode_responses=decode_responses,
            )
        except Exception:
            pass
        return client


def close_all_kv() -> None:
    """Close every cached KV client. Call on graceful shutdown."""
    with _lock:
        for client in _cache.values():
            try:
                client.close()
            except Exception:
                pass
        _cache.clear()


async def async_get_kv(db: int, *, decode_responses: bool = True) -> AsyncKVClient:
    """
    Return a cached AsyncKVClient for logical DB ``db``.

    Mirrors get_kv() but resolves to AsyncRedisKVClient or
    AsyncRedisKVClient. The first call for a given
    (db, decode_responses, loop) instantiates and connects the client.
    """
    import asyncio

    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = 0

    key = (db, decode_responses, loop_id)
    with _async_lock:
        existing = _async_cache.get(key)
    if existing is not None:
        return existing

    backend = kv_backend_for(db)
    from .async_redis_impl import AsyncRedisKVClient
    client: AsyncKVClient = AsyncRedisKVClient(db=db, decode_responses=decode_responses)

    with _async_lock:
        # Re-check under the lock to handle the race where two coroutines
        # construct in parallel — keep the first one, drop the second.
        existing = _async_cache.get(key)
        if existing is not None:
            try:
                await client.close()
            except Exception:
                pass
            return existing
        _async_cache[key] = client

    try:
        logger.info(
            "async_kv_client_created",
            db=db,
            backend=backend,
            decode_responses=decode_responses,
        )
    except Exception:
        pass
    return client


async def async_close_all_kv() -> None:
    """Close every cached async KV client. Call on graceful shutdown."""
    with _async_lock:
        clients = list(_async_cache.values())
        _async_cache.clear()
    for client in clients:
        try:
            await client.close()
        except Exception:
            pass


def kv_backend_map() -> Dict[int, str]:
    """
    Return the current backend resolution for every logical KV DB.

    Reads kv_backend_for(db) live (i.e. picks up env-var changes
    if any have happened since import) — useful for /healthz.
    """
    return {db: kv_backend_for(db) for db in range(KV_DB_COUNT)}
