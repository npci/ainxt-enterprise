# SPDX-License-Identifier: MIT
# ============================================================
# KVClient ABC
#
# Backend-agnostic interface for all key-value operations used
# by the AiNxt platform. Implemented by:
#   - RedisKVClient (wraps redis.Redis)
#
# The method surface is deliberately narrow — a documented
# subset rather than all of redis-py — so a second backend can
# be added without auditing every call site. It mirrors
# SPEC §6 (Strings / Hashes / Sets / Sorted
# Sets / Lists / Streams). pub/sub, WATCH/MULTI, and Lua
# scripting are intentionally NOT in the interface because
# they are not used in the codebase (audit performed in
# the KV client contract).
# ============================================================

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple


class KVScript(ABC):
    """Server-side Lua script handle returned by ``KVClient.register_script``.

    Behaves like ``redis.commands.core.Script``: callable with ``keys`` and
    ``args`` lists, returns whatever the script returns. The underlying
    backend may use EVALSHA (    SPEC §6.7); both are equivalent from the caller's perspective.
    """

    @abstractmethod
    def __call__(self, *, keys: list, args: list) -> Any: ...


class KVPipeline(ABC):
    """Batched / pipelined write context returned by KVClient.pipeline().

    Used as a context manager:

        with kv.pipeline() as pipe:
            pipe.set("a", "1")
            pipe.incr_by("b", 1)
            results = pipe.execute()

    Both backends batch the operations and flush on execute().
    No transactional guarantees are provided — call sites that
    needed WATCH/MULTI do not exist in the codebase.
    """

    @abstractmethod
    def set(self, key: str, value: Any, *, ex: Optional[int] = None) -> "KVPipeline": ...

    @abstractmethod
    def setex(self, key: str, ttl: int, value: Any) -> "KVPipeline":
        """Pipelined SETEX (alias for set(..., ex=ttl) with positional TTL)."""

    @abstractmethod
    def delete(self, *keys: str) -> "KVPipeline": ...

    @abstractmethod
    def sadd(self, key: str, *members: Any) -> "KVPipeline": ...

    @abstractmethod
    def zadd(self, key: str, mapping: Mapping[str, float]) -> "KVPipeline": ...

    @abstractmethod
    def incr(self, key: str, amount: int = 1) -> "KVPipeline":
        """Pipelined integer increment. Alias for incr_by."""

    @abstractmethod
    def incr_by(self, key: str, amount: int = 1) -> "KVPipeline": ...

    @abstractmethod
    def incrbyfloat(self, key: str, amount: float) -> "KVPipeline": ...

    @abstractmethod
    def hset(self, key: str, field: str, value: Any) -> "KVPipeline": ...

    @abstractmethod
    def hincrby(self, key: str, field: str, amount: int = 1) -> "KVPipeline": ...

    @abstractmethod
    def zincrby(self, key: str, amount: float, member: Any) -> "KVPipeline":
        """Pipelined sorted-set score increment."""

    @abstractmethod
    def expire(self, key: str, ttl: int) -> "KVPipeline": ...

    @abstractmethod
    def execute(self) -> list[Any]: ...

    @abstractmethod
    def __enter__(self) -> "KVPipeline": ...

    @abstractmethod
    def __exit__(self, exc_type, exc, tb) -> None: ...


class KVClient(ABC):
    """
    Backend-agnostic key-value client. One instance is bound to one
    logical DB (0..7). Construct via core.kv.get_kv(db).
    """

    # ---- introspection -------------------------------------------------
    @property
    @abstractmethod
    def backend(self) -> str:
        """Backend label (e.g. 'REDIS') — for metrics / logging."""

    @property
    @abstractmethod
    def db(self) -> int: ...

    @abstractmethod
    def ping(self) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...

    # ---- Strings -------------------------------------------------------
    @abstractmethod
    def get(self, key: str) -> Optional[str]: ...

    @abstractmethod
    def set(
        self,
        key: str,
        value: Any,
        *,
        ex: Optional[int] = None,
        nx: bool = False,
    ) -> bool: ...

    @abstractmethod
    def setex(self, key: str, ttl: int, value: Any) -> bool: ...

    @abstractmethod
    def setnx(self, key: str, value: Any) -> bool: ...

    @abstractmethod
    def delete(self, *keys: str) -> int: ...

    @abstractmethod
    def exists(self, *keys: str) -> int: ...

    @abstractmethod
    def expire(self, key: str, ttl: int) -> bool: ...

    @abstractmethod
    def ttl(self, key: str) -> int: ...

    @abstractmethod
    def mget(self, *keys: str) -> list[Optional[str]]: ...

    @abstractmethod
    def keys(self, pattern: str) -> list[str]: ...

    @abstractmethod
    def incr(self, key: str, amount: int = 1) -> int: ...

    @abstractmethod
    def decr(self, key: str, amount: int = 1) -> int: ...

    @abstractmethod
    def incrbyfloat(self, key: str, amount: float) -> float: ...

    # ---- Hashes --------------------------------------------------------
    @abstractmethod
    def hset(
        self,
        key: str,
        field: Optional[str] = None,
        value: Any = None,
        *,
        mapping: Optional[Mapping[str, Any]] = None,
    ) -> int:
        """Set one field, or many via ``mapping={field: value, ...}``."""

    @abstractmethod
    def hget(self, key: str, field: str) -> Optional[str]: ...

    @abstractmethod
    def hmget(self, key: str, *fields: str) -> list[Optional[str]]: ...

    @abstractmethod
    def hgetall(self, key: str) -> dict[str, str]: ...

    @abstractmethod
    def hmset(self, key: str, mapping: Mapping[str, Any]) -> bool: ...

    @abstractmethod
    def hexists(self, key: str, field: str) -> bool: ...

    @abstractmethod
    def hdel(self, key: str, *fields: str) -> int: ...

    @abstractmethod
    def hlen(self, key: str) -> int: ...

    @abstractmethod
    def hsetnx(self, key: str, field: str, value: Any) -> bool: ...

    @abstractmethod
    def hincrby(self, key: str, field: str, amount: int = 1) -> int: ...

    @abstractmethod
    def hincrbyfloat(self, key: str, field: str, amount: float) -> float:
        """Increment a hash field by a float amount; returns the new value."""

    @abstractmethod
    def hscan(
        self,
        key: str,
        cursor: int = 0,
        match: Optional[str] = None,
        count: Optional[int] = None,
    ) -> Tuple[int, dict[str, str]]: ...

    # ---- Sets ----------------------------------------------------------
    @abstractmethod
    def sadd(self, key: str, *members: Any) -> int: ...

    @abstractmethod
    def srem(self, key: str, *members: Any) -> int: ...

    @abstractmethod
    def smembers(self, key: str) -> set[str]: ...

    @abstractmethod
    def sismember(self, key: str, member: Any) -> bool: ...

    @abstractmethod
    def scard(self, key: str) -> int: ...

    # ---- Sorted Sets ---------------------------------------------------
    @abstractmethod
    def zadd(self, key: str, mapping: Mapping[str, float]) -> int: ...

    @abstractmethod
    def zrem(self, key: str, *members: Any) -> int: ...

    @abstractmethod
    def zremrangebyscore(self, key: str, min_: float, max_: float) -> int:
        """Remove sorted-set entries whose score is in the inclusive range [min_, max_]."""

    @abstractmethod
    def zrange(
        self,
        key: str,
        start: int,
        end: int,
        *,
        withscores: bool = False,
    ) -> list: ...

    @abstractmethod
    def zrevrange(
        self,
        key: str,
        start: int,
        end: int,
        *,
        withscores: bool = False,
    ) -> list:
        """Sorted-set range, descending by score."""

    @abstractmethod
    def zincrby(self, key: str, amount: float, member: Any) -> float:
        """Increment the score of ``member`` in sorted set ``key`` by ``amount``."""

    @abstractmethod
    def zrangebyscore(
        self,
        key: str,
        min_: float,
        max_: float,
        *,
        withscores: bool = False,
    ) -> list: ...

    @abstractmethod
    def zscore(self, key: str, member: Any) -> Optional[float]: ...

    @abstractmethod
    def zcard(self, key: str) -> int: ...

    # ---- Lists ---------------------------------------------------------
    @abstractmethod
    def lpush(self, key: str, *values: Any) -> int: ...

    @abstractmethod
    def rpush(self, key: str, *values: Any) -> int: ...

    @abstractmethod
    def lpop(self, key: str) -> Optional[str]: ...

    @abstractmethod
    def rpop(self, key: str) -> Optional[str]: ...

    @abstractmethod
    def blpop(
        self,
        keys: Sequence[str],
        timeout: int = 0,
    ) -> Optional[Tuple[str, str]]: ...

    @abstractmethod
    def brpop(
        self,
        keys: Sequence[str],
        timeout: int = 0,
    ) -> Optional[Tuple[str, str]]: ...

    @abstractmethod
    def lrange(self, key: str, start: int, end: int) -> list[str]: ...

    @abstractmethod
    def ltrim(self, key: str, start: int, end: int) -> bool:
        """Trim list ``key`` to the inclusive range [start, end]."""

    @abstractmethod
    def llen(self, key: str) -> int: ...

    # ---- Streams -------------------------------------------------------
    @abstractmethod
    def xadd(
        self,
        key: str,
        fields: Mapping[str, Any],
        *,
        maxlen: Optional[int] = None,
        approximate: bool = True,
    ) -> str: ...

    @abstractmethod
    def xread(
        self,
        streams: Mapping[str, str],
        *,
        count: Optional[int] = None,
        block: Optional[int] = None,
    ) -> list[Tuple[str, list[Tuple[str, dict[str, str]]]]]: ...

    @abstractmethod
    def xlen(self, key: str) -> int: ...

    @abstractmethod
    def xdel(self, key: str, *ids: str) -> int: ...

    # ---- Server-side scripting -----------------------------------------
    @abstractmethod
    def register_script(self, source: str) -> KVScript:
        """Register a Lua script with the server and return a callable handle.

        The handle can be invoked any number of times with different
        ``keys`` and ``args``. Backends cache the script SHA, so repeat
        invocations avoid re-uploading the source.
        """

    # ---- Pipelines / batches -------------------------------------------
    @abstractmethod
    def pipeline(self) -> KVPipeline: ...
