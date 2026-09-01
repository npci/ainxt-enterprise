# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AsyncRedisKVClient — AsyncKVClient backed by redis.asyncio.Redis
#
# Thin async pass-through so call sites can depend on the
# AsyncKVClient interface rather than the concrete redis library.
# ============================================================

from __future__ import annotations

from typing import Any, Mapping, Optional

import redis.asyncio as _aioredis
from redis import exceptions as _redis_exc

from core.config import REDIS_HOST, REDIS_PORT, REDIS_PASSWORD
from .async_base import AsyncKVClient, AsyncKVPipeline
from .errors import KVError, KVPermanent, KVTransient
from .metrics import observe as _kv_observe


_TRANSIENT_TYPES = (
    _redis_exc.ConnectionError,
    _redis_exc.TimeoutError,
    _redis_exc.BusyLoadingError,
)


def _wrap(coro):
    """Async _wrap: error translation + metrics, mirrors the sync wrapper."""
    op_name = coro.__name__

    async def _inner(self, *args, **kwargs):
        with _kv_observe(self.backend, op_name, self.db):
            try:
                return await coro(self, *args, **kwargs)
            except _TRANSIENT_TYPES as exc:
                raise KVTransient(str(exc)) from exc
            except _redis_exc.AuthenticationError as exc:
                raise KVPermanent(f"auth: {exc}") from exc
            except _redis_exc.RedisError as exc:
                raise KVError(str(exc)) from exc
    _inner.__name__ = coro.__name__
    _inner.__doc__ = coro.__doc__
    return _inner


class _AsyncRedisPipeline(AsyncKVPipeline):
    backend = "REDIS"

    def __init__(self, pipe, db: int = -1):
        self._pipe = pipe
        self.db = db

    def setex(self, key, ttl, value):
        self._pipe.setex(key, ttl, value)
        return self

    async def execute(self):
        try:
            return await self._pipe.execute()
        except _TRANSIENT_TYPES as exc:
            raise KVTransient(str(exc)) from exc
        except _redis_exc.RedisError as exc:
            raise KVError(str(exc)) from exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            await self._pipe.reset()
        except Exception:
            pass


class AsyncRedisKVClient(AsyncKVClient):
    """AsyncKVClient backed by redis.asyncio.Redis."""

    def __init__(self, db: int, *, decode_responses: bool = True):
        self._db = db
        self._client = _aioredis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=db,
            password=REDIS_PASSWORD or None,
            decode_responses=decode_responses,
            socket_connect_timeout=2,
        )

    @property
    def backend(self) -> str:
        return "REDIS"

    @property
    def db(self) -> int:
        return self._db

    @_wrap
    async def ping(self) -> bool:
        return bool(await self._client.ping())

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except AttributeError:
            # redis-py < 5 used `close()`; fall back for compatibility.
            try:
                await self._client.close()
            except Exception:
                pass
        except Exception:
            pass

    @_wrap
    async def get(self, key):
        return await self._client.get(key)

    @_wrap
    async def set(self, key, value, *, ex: Optional[int] = None):
        return bool(await self._client.set(key, value, ex=ex))

    @_wrap
    async def setex(self, key, ttl, value):
        return bool(await self._client.setex(key, ttl, value))

    @_wrap
    async def mget(self, *keys):
        if not keys:
            return []
        return list(await self._client.mget(*keys))

    @_wrap
    async def xread(self, streams, *, count=None, block=None):
        return await self._client.xread(dict(streams), count=count, block=block) or []

    def pipeline(self) -> AsyncKVPipeline:
        return _AsyncRedisPipeline(self._client.pipeline(transaction=False), db=self._db)
