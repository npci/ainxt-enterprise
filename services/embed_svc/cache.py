# SPDX-License-Identifier: MIT
# ============================================================
# EMBED SERVICE — KV-backed embedding cache (DB=7)
# Key: sha256(text)[:32]  Value: JSON float array  TTL: 1h
#
# Routes through core.kv.async_get_kv so the backend is selected
# per the platform-wide REDIS_CLIENT_CONFIG_DB7 env var, matching
# the sync KV layer.
# ============================================================

import hashlib
import json

from core.config import RDB_EMBED
from core.kv import async_get_kv

from services.embed_svc.config import EMBED_CACHE_TTL


class EmbedCache:
    def __init__(self):
        self._kv = None

    async def connect(self) -> None:
        self._kv = await async_get_kv(RDB_EMBED, decode_responses=False)
        await self._kv.ping()

    def _key(self, text: str) -> str:
        return "emb:" + hashlib.sha256(text.encode()).hexdigest()[:32]

    async def get(self, text: str) -> list[float] | None:
        if not self._kv:
            return None
        try:
            raw = await self._kv.get(self._key(text))
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def set(self, text: str, embedding: list[float]) -> None:
        if not self._kv:
            return
        try:
            await self._kv.setex(self._key(text), EMBED_CACHE_TTL, json.dumps(embedding))
        except Exception:
            pass

    async def get_many(self, texts: list[str]) -> dict[str, list[float] | None]:
        """Batch get. Returns mapping text → embedding (None if not cached)."""
        if not self._kv or not texts:
            return {t: None for t in texts}
        try:
            keys   = [self._key(t) for t in texts]
            values = await self._kv.mget(*keys)
            return {
                text: (json.loads(v) if v else None)
                for text, v in zip(texts, values)
            }
        except Exception:
            return {t: None for t in texts}

    async def set_many(self, items: dict[str, list[float]]) -> None:
        """Batch set."""
        if not self._kv or not items:
            return
        try:
            pipe = self._kv.pipeline()
            for text, emb in items.items():
                pipe.setex(self._key(text), EMBED_CACHE_TTL, json.dumps(emb))
            await pipe.execute()
        except Exception:
            pass
