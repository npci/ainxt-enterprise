# SPDX-License-Identifier: Apache-2.0
# ============================================================
# RedisKVClient — KVClient implementation backed by redis.Redis
#
# Thin pass-through wrapper around the existing redis-py client.
# Behaviour identical to redis.Redis(...) for every method;
# exists only so call sites can depend on the KVClient interface
# rather than the concrete redis library.
# ============================================================

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, Tuple

import redis as _redis
from redis import exceptions as _redis_exc

from core.config import REDIS_HOST, REDIS_PORT, REDIS_PASSWORD
from .base import KVClient, KVPipeline, KVScript
from .errors import KVError, KVPermanent, KVTransient
from .metrics import observe as _kv_observe


# ------------------------------------------------------------------
# Exception mapping
# ------------------------------------------------------------------
_TRANSIENT_TYPES = (
    _redis_exc.ConnectionError,
    _redis_exc.TimeoutError,
    _redis_exc.BusyLoadingError,
)


def _wrap(fn):
    """Decorator: convert redis.exceptions.* into core.kv.errors.* and
    record kv_call_total / kv_call_latency_seconds metrics."""
    op_name = fn.__name__

    def _inner(self, *args, **kwargs):
        with _kv_observe(self.backend, op_name, self.db):
            try:
                return fn(self, *args, **kwargs)
            except _TRANSIENT_TYPES as exc:
                raise KVTransient(str(exc)) from exc
            except _redis_exc.AuthenticationError as exc:
                raise KVPermanent(f"auth: {exc}") from exc
            except _redis_exc.RedisError as exc:
                raise KVError(str(exc)) from exc
    _inner.__name__ = fn.__name__
    _inner.__doc__ = fn.__doc__
    return _inner


# ------------------------------------------------------------------
# Server-side scripting (Lua)
# ------------------------------------------------------------------
class _RedisScript(KVScript):
    """Wraps a redis-py registered Script as a KVScript."""

    def __init__(self, script):
        self._script = script

    def __call__(self, *, keys: list, args: list):
        # redis-py's Script is callable with keys/args kwargs and returns
        # whatever EVAL/EVALSHA returns (int, bytes, list, …).
        try:
            return self._script(keys=list(keys), args=list(args))
        except _TRANSIENT_TYPES as exc:
            raise KVTransient(str(exc)) from exc
        except _redis_exc.RedisError as exc:
            raise KVError(str(exc)) from exc


# ------------------------------------------------------------------
# Pipeline
# ------------------------------------------------------------------
class _RedisPipeline(KVPipeline):
    # Pipeline ops inherit backend/db from the parent client so the same
    # _wrap decorator (which reads self.backend / self.db) can instrument
    # `execute()` without further plumbing.
    backend = "REDIS"

    def __init__(self, pipe, db: int = -1):
        self._pipe = pipe
        self.db = db

    def set(self, key, value, *, ex=None):
        self._pipe.set(key, value, ex=ex)
        return self

    def setex(self, key, ttl, value):
        self._pipe.setex(key, ttl, value)
        return self

    def delete(self, *keys):
        self._pipe.delete(*keys)
        return self

    def sadd(self, key, *members):
        if members:
            self._pipe.sadd(key, *members)
        return self

    def zadd(self, key, mapping):
        if mapping:
            self._pipe.zadd(key, dict(mapping))
        return self

    def incr(self, key, amount=1):
        self._pipe.incrby(key, amount)
        return self

    def incr_by(self, key, amount=1):
        self._pipe.incrby(key, amount)
        return self

    def incrbyfloat(self, key, amount):
        self._pipe.incrbyfloat(key, amount)
        return self

    def hset(self, key, field, value):
        self._pipe.hset(key, field, value)
        return self

    def hincrby(self, key, field, amount=1):
        self._pipe.hincrby(key, field, amount)
        return self

    def zincrby(self, key, amount, member):
        self._pipe.zincrby(key, amount, member)
        return self

    def expire(self, key, ttl):
        self._pipe.expire(key, ttl)
        return self

    @_wrap
    def execute(self):
        return self._pipe.execute()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._pipe.reset()
        except Exception:
            pass


# ------------------------------------------------------------------
# Client
# ------------------------------------------------------------------
class RedisKVClient(KVClient):
    """KVClient backed by redis.Redis."""

    def __init__(self, db: int, *, decode_responses: bool = True):
        self._db = db
        self._decode = decode_responses
        self._client = _redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=db,
            password=REDIS_PASSWORD or None,
            decode_responses=decode_responses,
            socket_connect_timeout=2,
        )

    # ---- introspection ----
    @property
    def backend(self) -> str:
        return "REDIS"

    @property
    def db(self) -> int:
        return self._db

    @_wrap
    def ping(self) -> bool:
        return bool(self._client.ping())

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    # ---- Strings ----
    @_wrap
    def get(self, key): return self._client.get(key)

    @_wrap
    def set(self, key, value, *, ex=None, nx=False):
        return bool(self._client.set(key, value, ex=ex, nx=nx))

    @_wrap
    def setex(self, key, ttl, value):
        return bool(self._client.setex(key, ttl, value))

    @_wrap
    def setnx(self, key, value):
        return bool(self._client.setnx(key, value))

    @_wrap
    def delete(self, *keys):
        if not keys:
            return 0
        return int(self._client.delete(*keys))

    @_wrap
    def exists(self, *keys):
        if not keys:
            return 0
        return int(self._client.exists(*keys))

    @_wrap
    def expire(self, key, ttl):
        return bool(self._client.expire(key, ttl))

    @_wrap
    def ttl(self, key):
        return int(self._client.ttl(key))

    @_wrap
    def mget(self, *keys):
        if not keys:
            return []
        return list(self._client.mget(*keys))

    @_wrap
    def keys(self, pattern):
        return list(self._client.keys(pattern))

    @_wrap
    def incr(self, key, amount=1):
        return int(self._client.incrby(key, amount))

    @_wrap
    def decr(self, key, amount=1):
        return int(self._client.decrby(key, amount))

    @_wrap
    def incrbyfloat(self, key, amount):
        return float(self._client.incrbyfloat(key, amount))

    # ---- Hashes ----
    @_wrap
    def hset(self, key, field=None, value=None, *, mapping=None):
        if mapping is not None:
            if field is None:
                return int(self._client.hset(key, mapping=dict(mapping)))
            return int(self._client.hset(key, field, value, mapping=dict(mapping)))
        return int(self._client.hset(key, field, value))

    @_wrap
    def hget(self, key, field):
        return self._client.hget(key, field)

    @_wrap
    def hmget(self, key, *fields):
        return list(self._client.hmget(key, *fields))

    @_wrap
    def hgetall(self, key):
        return dict(self._client.hgetall(key))

    @_wrap
    def hmset(self, key, mapping):
        if not mapping:
            return True
        # redis-py deprecated hmset → use hset with mapping kwarg
        return bool(self._client.hset(key, mapping=dict(mapping)))

    @_wrap
    def hexists(self, key, field):
        return bool(self._client.hexists(key, field))

    @_wrap
    def hdel(self, key, *fields):
        if not fields:
            return 0
        return int(self._client.hdel(key, *fields))

    @_wrap
    def hlen(self, key):
        return int(self._client.hlen(key))

    @_wrap
    def hsetnx(self, key, field, value):
        return bool(self._client.hsetnx(key, field, value))

    @_wrap
    def hincrby(self, key, field, amount=1):
        return int(self._client.hincrby(key, field, amount))

    @_wrap
    def hincrbyfloat(self, key, field, amount):
        return float(self._client.hincrbyfloat(key, field, amount))

    @_wrap
    def hscan(self, key, cursor=0, match=None, count=None):
        next_cursor, mapping = self._client.hscan(
            key, cursor=cursor, match=match, count=count,
        )
        return int(next_cursor), dict(mapping)

    # ---- Sets ----
    @_wrap
    def sadd(self, key, *members):
        if not members:
            return 0
        return int(self._client.sadd(key, *members))

    @_wrap
    def srem(self, key, *members):
        if not members:
            return 0
        return int(self._client.srem(key, *members))

    @_wrap
    def smembers(self, key):
        return set(self._client.smembers(key))

    @_wrap
    def sismember(self, key, member):
        return bool(self._client.sismember(key, member))

    @_wrap
    def scard(self, key):
        return int(self._client.scard(key))

    # ---- Sorted Sets ----
    @_wrap
    def zadd(self, key, mapping):
        if not mapping:
            return 0
        return int(self._client.zadd(key, dict(mapping)))

    @_wrap
    def zrem(self, key, *members):
        if not members:
            return 0
        return int(self._client.zrem(key, *members))

    @_wrap
    def zremrangebyscore(self, key, min_, max_):
        return int(self._client.zremrangebyscore(key, min_, max_))

    @_wrap
    def zrange(self, key, start, end, *, withscores=False):
        return list(self._client.zrange(key, start, end, withscores=withscores))

    @_wrap
    def zrevrange(self, key, start, end, *, withscores=False):
        return list(self._client.zrevrange(key, start, end, withscores=withscores))

    @_wrap
    def zincrby(self, key, amount, member):
        return float(self._client.zincrby(key, amount, member))

    @_wrap
    def zrangebyscore(self, key, min_, max_, *, withscores=False):
        return list(self._client.zrangebyscore(key, min_, max_, withscores=withscores))

    @_wrap
    def zscore(self, key, member):
        v = self._client.zscore(key, member)
        return float(v) if v is not None else None

    @_wrap
    def zcard(self, key):
        return int(self._client.zcard(key))

    # ---- Lists ----
    @_wrap
    def lpush(self, key, *values):
        if not values:
            return 0
        return int(self._client.lpush(key, *values))

    @_wrap
    def rpush(self, key, *values):
        if not values:
            return 0
        return int(self._client.rpush(key, *values))

    @_wrap
    def lpop(self, key):
        return self._client.lpop(key)

    @_wrap
    def rpop(self, key):
        return self._client.rpop(key)

    @_wrap
    def blpop(self, keys, timeout=0):
        res = self._client.blpop(list(keys), timeout=timeout)
        return tuple(res) if res else None

    @_wrap
    def brpop(self, keys, timeout=0):
        res = self._client.brpop(list(keys), timeout=timeout)
        return tuple(res) if res else None

    @_wrap
    def lrange(self, key, start, end):
        return list(self._client.lrange(key, start, end))

    @_wrap
    def ltrim(self, key, start, end):
        return bool(self._client.ltrim(key, start, end))

    @_wrap
    def llen(self, key):
        return int(self._client.llen(key))

    # ---- Streams ----
    @_wrap
    def xadd(self, key, fields, *, maxlen=None, approximate=True):
        kwargs = {}
        if maxlen is not None:
            kwargs["maxlen"] = maxlen
            kwargs["approximate"] = approximate
        return self._client.xadd(key, dict(fields), **kwargs)

    @_wrap
    def xread(self, streams, *, count=None, block=None):
        return self._client.xread(dict(streams), count=count, block=block) or []

    @_wrap
    def xlen(self, key):
        return int(self._client.xlen(key))

    @_wrap
    def xdel(self, key, *ids):
        if not ids:
            return 0
        return int(self._client.xdel(key, *ids))

    # ---- Server-side scripting ----
    @_wrap
    def register_script(self, source: str) -> KVScript:
        return _RedisScript(self._client.register_script(source))

    # ---- Pipeline ----
    def pipeline(self) -> KVPipeline:
        return _RedisPipeline(self._client.pipeline(transaction=False), db=self._db)
