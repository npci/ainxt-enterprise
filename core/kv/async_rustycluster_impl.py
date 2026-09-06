# SPDX-License-Identifier: MIT
# ============================================================
# AsyncRustyClusterKVClient — AsyncKVClient backed by
# rustycluster.async_get_client (SPEC §9).
#
# RC's async client mirrors the sync surface; this wrapper just
# normalizes the method names onto the AsyncKVClient ABC.
# ============================================================

from __future__ import annotations

from typing import Any, Mapping, Optional

from .async_base import AsyncKVClient, AsyncKVPipeline
from .errors import KVError, KVPermanent, KVTransient
from .metrics import observe as _kv_observe


async def _get_rc_async():
    try:
        import rustycluster  # noqa: F401
        return rustycluster
    except ImportError as exc:
        raise KVPermanent(
            "py-rustycluster-client is not installed; "
            "either `pip install py-rustycluster-client>=1.1.4` or set "
            "REDIS_CLIENT_CONFIG=REDIS"
        ) from exc


def _wrap(coro):
    """Async exception translator + metrics for the RC backend."""
    op_name = coro.__name__

    async def _inner(self, *args, **kwargs):
        with _kv_observe(self.backend, op_name, self.db):
            try:
                return await coro(self, *args, **kwargs)
            except KVError:
                raise
            except Exception as exc:  # pragma: no cover — RC exception names vary
                cls = type(exc).__name__.lower()
                if "auth" in cls or "permission" in cls:
                    raise KVPermanent(str(exc)) from exc
                if "timeout" in cls or "connection" in cls or "unavailable" in cls:
                    raise KVTransient(str(exc)) from exc
                raise KVError(str(exc)) from exc
    _inner.__name__ = coro.__name__
    _inner.__doc__ = coro.__doc__
    return _inner


def _to_str(v):
    if v is None:
        return None
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return v
    return v


class _AsyncRustyClusterPipeline(AsyncKVPipeline):
    backend = "RUSTYCLUSTER"

    def __init__(self, client, db: int = -1):
        self._client = client
        self.db = db
        self._ops: list[tuple[str, tuple, dict]] = []

    def setex(self, key, ttl, value):
        # RC SDK shape: set_ex(key, value, ttl)
        self._ops.append(("set_ex", (key, value, ttl), {}))
        return self

    async def execute(self):
        # Prefer native batch() if present (SPEC §7); otherwise replay.
        batch = getattr(self._client, "batch", None)
        if callable(batch):
            try:
                b = batch()
                for op, args, kwargs in self._ops:
                    getattr(b, op)(*args, **kwargs)
                return await b.execute()
            except (TypeError, AttributeError):
                pass
        results = []
        for op, args, kwargs in self._ops:
            fn = getattr(self._client, op)
            results.append(await fn(*args, **kwargs))
        return results

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._ops.clear()


class AsyncRustyClusterKVClient(AsyncKVClient):
    """AsyncKVClient backed by rustycluster.async_get_client(f'DB{db}')."""

    def __init__(self, db: int):
        self._db = db
        self._client = None   # filled in by connect()

    async def connect(self):
        """Lazy async constructor — must be awaited before use."""
        if self._client is None:
            rc = await _get_rc_async()
            # SPEC §9.2 — async_get_client is itself awaitable.
            self._client = await rc.async_get_client(f"DB{self._db}")
        return self

    @property
    def backend(self) -> str:
        return "RUSTYCLUSTER"

    @property
    def db(self) -> int:
        return self._db

    @_wrap
    async def ping(self) -> bool:
        if self._client is None:
            await self.connect()
        if hasattr(self._client, "ping"):
            return bool(await self._client.ping())
        # Fallback: a cheap get on a sentinel key
        await self._client.get("__kv_ping__")
        return True

    async def close(self) -> None:
        try:
            if self._client is not None and hasattr(self._client, "close"):
                await self._client.close()
        except Exception:
            pass

    @_wrap
    async def get(self, key):
        if self._client is None:
            await self.connect()
        return _to_str(await self._client.get(key))

    @_wrap
    async def set(self, key, value, *, ex: Optional[int] = None):
        if self._client is None:
            await self.connect()
        if ex is not None:
            return bool(await self._client.set_ex(key, value, ex))
        return bool(await self._client.set(key, value))

    @_wrap
    async def setex(self, key, ttl, value):
        if self._client is None:
            await self.connect()
        return bool(await self._client.set_ex(key, value, ttl))

    @_wrap
    async def mget(self, *keys):
        if self._client is None:
            await self.connect()
        if not keys:
            return []
        return [_to_str(v) for v in await self._client.mget(*keys)]

    @_wrap
    async def xread(self, streams, *, count=None, block=None):
        if self._client is None:
            await self.connect()
        kwargs = {}
        if count is not None:
            kwargs["count"] = count
        if block is not None:
            kwargs["block_ms"] = block
        raw = await self._client.xread(dict(streams), **kwargs) or []
        out = []
        for stream_name, entries in raw:
            converted = []
            for entry_id, entry_fields in entries:
                converted.append((
                    _to_str(entry_id),
                    {_to_str(k): _to_str(v) for k, v in dict(entry_fields).items()},
                ))
            out.append((_to_str(stream_name), converted))
        return out

    def pipeline(self) -> AsyncKVPipeline:
        # NB: the underlying client must already be connected. Callers
        # typically `await get()` or `await ping()` once before pipelining.
        return _AsyncRustyClusterPipeline(self._client, db=self._db)
