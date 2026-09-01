# SPDX-License-Identifier: Apache-2.0
# ============================================================
# TRANSLATE SERVICE — Redis translation cache
# Key: "xl:" + sha256(f"{src}|{tgt}|{text}")[:32]
# Value: translated string (UTF-8)   TTL: 24h
# ============================================================

import hashlib
import redis.asyncio as aioredis

from services.translate_svc.config import (
    REDIS_HOST,
    REDIS_PORT,
    REDIS_TRANSLATE_DB,
    TRANSLATE_CACHE_TTL,
)


class TranslateCache:
    def __init__(self):
        self._r: aioredis.Redis | None = None

    async def connect(self) -> None:
        self._r = aioredis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=REDIS_TRANSLATE_DB,
            decode_responses=True,
            socket_connect_timeout=2,
        )
        await self._r.ping()

    def _key(self, text: str, src: str, tgt: str) -> str:
        return "xl:" + hashlib.sha256(
            f"{src}|{tgt}|{text}".encode()
        ).hexdigest()[:32]

    async def get(self, text: str, src: str, tgt: str) -> str | None:
        if not self._r:
            return None
        try:
            return await self._r.get(self._key(text, src, tgt))
        except Exception:
            return None

    async def set(self, text: str, src: str, tgt: str, translation: str) -> None:
        if not self._r:
            return
        try:
            await self._r.setex(
                self._key(text, src, tgt),
                TRANSLATE_CACHE_TTL,
                translation,
            )
        except Exception:
            pass

    async def get_many(
        self,
        texts: list[str],
        src: str,
        tgt: str,
    ) -> dict[str, str | None]:
        """Batch get. Returns mapping text → translation (None if not cached)."""
        if not self._r or not texts:
            return {t: None for t in texts}
        try:
            keys   = [self._key(t, src, tgt) for t in texts]
            values = await self._r.mget(*keys)
            return {
                text: (v if v else None)
                for text, v in zip(texts, values)
            }
        except Exception:
            return {t: None for t in texts}

    async def set_many(
        self,
        items: dict[str, str],
        src: str,
        tgt: str,
    ) -> None:
        """Batch set. items is mapping text → translation."""
        if not self._r or not items:
            return
        try:
            pipe = self._r.pipeline()
            for text, translation in items.items():
                pipe.setex(
                    self._key(text, src, tgt),
                    TRANSLATE_CACHE_TTL,
                    translation,
                )
            await pipe.execute()
        except Exception:
            pass
