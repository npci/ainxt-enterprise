# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AsyncKVClient ABC
#
# Async-capable subset of KVClient — only the methods actually used
# by the async call sites in the codebase (gateway SSE consumer,
# embed_svc cache, privacy_svc cache). All methods are coroutines.
# ============================================================

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple


class AsyncKVPipeline(ABC):
    """Async batched-write context. Mirrors the sync KVPipeline shape."""

    @abstractmethod
    def setex(self, key: str, ttl: int, value: Any) -> "AsyncKVPipeline": ...

    @abstractmethod
    async def execute(self) -> list[Any]: ...

    @abstractmethod
    async def __aenter__(self) -> "AsyncKVPipeline": ...

    @abstractmethod
    async def __aexit__(self, exc_type, exc, tb) -> None: ...


class AsyncKVClient(ABC):
    """
    Backend-agnostic async key-value client.

    Only the methods touched by async code today are abstract. Add more
    as new async call sites land; do not duplicate the entire sync ABC.
    """

    # ---- introspection ----
    @property
    @abstractmethod
    def backend(self) -> str: ...

    @property
    @abstractmethod
    def db(self) -> int: ...

    @abstractmethod
    async def ping(self) -> bool: ...

    @abstractmethod
    async def close(self) -> None: ...

    # ---- strings ----
    @abstractmethod
    async def get(self, key: str) -> Optional[Any]: ...

    @abstractmethod
    async def set(
        self,
        key: str,
        value: Any,
        *,
        ex: Optional[int] = None,
    ) -> bool: ...

    @abstractmethod
    async def setex(self, key: str, ttl: int, value: Any) -> bool: ...

    @abstractmethod
    async def mget(self, *keys: str) -> list[Optional[Any]]: ...

    # ---- streams (DB6) ----
    @abstractmethod
    async def xread(
        self,
        streams: Mapping[str, str],
        *,
        count: Optional[int] = None,
        block: Optional[int] = None,
    ) -> list[Tuple[str, list[Tuple[str, dict]]]]: ...

    # ---- pipelines ----
    @abstractmethod
    def pipeline(self) -> AsyncKVPipeline: ...
