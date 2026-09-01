# SPDX-License-Identifier: Apache-2.0
# ============================================================
# EMBED SERVICE — batch embedder
#
# Architecture (corrected for multi-worker Ollama):
#
#   ONE _loop() task accumulates texts from the shared queue into a
#   "mega-batch" of up to OLLAMA_WORKERS × BATCH_SIZE texts, then splits
#   it into OLLAMA_WORKERS sub-batches and fires them as concurrent HTTP
#   calls via asyncio.gather().
#
#   Why ONE loop, not N:
#     With N loops all doing queue.get(), each grabs only 1-4 texts before
#     their 50ms window expires (convoy effect). Ollama gets N tiny calls
#     instead of N full batches. This overwhelms Ollama even for small repos.
#
#   With one loop + concurrent dispatch:
#     OLLAMA_WORKERS=1  → accumulate 64 texts  → 1 Ollama call  (original)
#     OLLAMA_WORKERS=4  → accumulate 256 texts → 4 concurrent calls
#     OLLAMA_WORKERS=16 → accumulate 1024 texts → 16 concurrent calls
#
#   Redis SHA256 cache means repeat texts (re-indexing) hit cache and never
#   reach Ollama.
# ============================================================

import asyncio
import time
import os
import json

import httpx

from services.embed_svc.cache import EmbedCache
from services.embed_svc.config import (
    OLLAMA_URL, OLLAMA_URLS, OLLAMA_MODEL, OLLAMA_TIMEOUT,
    OPENAI_API_KEY, OPENAI_MODEL, OPENAI_DIMS, OPENAI_TIMEOUT,
    NOMIC_EMBED_URL, NOMIC_EMBED_API_KEY, NOMIC_EMBED_MODEL,
    NOMIC_EMBED_DIMS, NOMIC_EMBED_TIMEOUT, NOMIC_EMBED_BATCH,
    BATCH_SIZE, BATCH_WAIT_MS, QUEUE_MAXSIZE, OLLAMA_WORKERS,
)
from core.logger import logger

# Hard ceiling per text before sending to Ollama.  index_worker already caps
# at 1000 chars (ENRICH_CONTENT_MAX / _EMBED_MAX_CHARS), so this is a
# last-resort safety net.  Lower value means fewer tokens per call → Ollama
# never rejects with context-overflow errors.
_MAX_TEXT_CHARS = 1000


class OllamaEmbedder:
    """
    Batch-accumulating embedder via Ollama HTTP API.
    Single _loop() task accumulates texts; fires N concurrent Ollama calls.
    """

    def __init__(self, cache: EmbedCache):
        self._cache  = cache
        self._q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        # One client per Ollama instance — sub-batches are dispatched round-robin.
        # Each client holds BATCH_SIZE concurrent connections + 4 spare for per-text fallback.
        self._clients: list[httpx.AsyncClient] = [
            httpx.AsyncClient(
                base_url=url,
                timeout=OLLAMA_TIMEOUT,
                limits=httpx.Limits(
                    max_connections=BATCH_SIZE + 4,
                    max_keepalive_connections=BATCH_SIZE,
                ),
            )
            for url in OLLAMA_URLS
        ]
        # backward-compat alias used by health check / shutdown (first client)
        self._client = self._clients[0]

    async def start(self) -> None:
        """Start ONE accumulator loop. It fires up to OLLAMA_WORKERS concurrent
        Ollama sub-batch calls per iteration — no convoy effect, full batches
        guaranteed. Sub-batches are round-robined across all OLLAMA_URLS instances."""
        urls_str = ", ".join(OLLAMA_URLS)
        logger.info(
            f"OllamaEmbedder: starting (OLLAMA_WORKERS={OLLAMA_WORKERS} "
            f"BATCH_SIZE={BATCH_SIZE} instances={len(self._clients)}) → [{urls_str}]"
        )
        asyncio.create_task(self._loop(worker_id=0))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """
        Embed a batch of texts.  Returns embeddings in the same order.
        Cache-first: texts with a cached embedding skip the queue entirely.
        """
        if not texts:
            return []

        # ── Cache lookup ───────────────────────────────────────
        cached_map = await self._cache.get_many(texts)
        results    = [None] * len(texts)
        pending    = []   # (original_index, text, future)

        for i, text in enumerate(texts):
            if cached_map[text] is not None:
                results[i] = cached_map[text]
            else:
                loop = asyncio.get_running_loop()
                fut  = loop.create_future()
                pending.append((i, text, fut))

        cache_hits = len(texts) - len(pending)
        logger.debug(
            f"OllamaEmbedder [cache]: hits={cache_hits}/{len(texts)} "
            f"({100*cache_hits//len(texts)}%) pending={len(pending)} queue_depth={self._q.qsize()}"
        )

        if not pending:
            return results  # 100% cache hit

        # ── Enqueue pending texts ──────────────────────────────
        # Use put() not put_nowait() — blocks until queue has space instead of
        # raising QueueFull and returning a 500. This provides natural backpressure:
        # index_worker slows down rather than crashing with embed errors.
        for _, text, fut in pending:
            await self._q.put((text, fut))

        # ── Await all futures ──────────────────────────────────
        embeddings = await asyncio.gather(
            *[fut for _, _, fut in pending],
            return_exceptions=True,
        )

        for (idx, text, _), emb in zip(pending, embeddings):
            if isinstance(emb, Exception):
                raise emb
            results[idx] = emb

        return results

    async def _send_sub_batch(
            self,
            sub_texts:   list[str],
            sub_futures: list[asyncio.Future],
            label:       str,
            client:      httpx.AsyncClient,
    ) -> None:
        """
        Send one sub-batch of texts to a specific Ollama instance, resolve its futures.
        Called concurrently by _loop() for up to OLLAMA_WORKERS sub-batches.
        Each sub-batch is routed to a different Ollama instance (round-robin by index).
        Retries 3× with 1s/2s backoff. Per-text fallback if all attempts fail.
        Logs include HTTP status + response body so you can tell gateway/embed/nomic apart.
        """
        # Truncate texts that exceed the context window before sending to Ollama.
        # Original texts are kept as cache keys — only the payload is shortened.
        safe_texts  = [t[:_MAX_TEXT_CHARS] for t in sub_texts]
        n_truncated = sum(1 for o, s in zip(sub_texts, safe_texts) if len(o) != len(s))
        total_chars = sum(len(t) for t in safe_texts)
        ollama_url  = str(client.base_url).rstrip("/")
        if n_truncated:
            logger.warning(
                f"OllamaEmbedder {label} [truncated {n_truncated}/{len(sub_texts)} texts "
                f"to {_MAX_TEXT_CHARS} chars — originals exceeded nomic context window]"
            )
        logger.debug(
            f"OllamaEmbedder {label} [→{ollama_url}]: "
            f"sub_batch={len(safe_texts)} total_chars={total_chars} truncated={n_truncated}"
        )

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = await client.post(
                    "/api/embed",
                    json={"model": OLLAMA_MODEL, "input": safe_texts},
                )
                resp.raise_for_status()
                embeddings = resp.json()["embeddings"]

                to_cache: dict[str, list[float]] = {}
                for orig_text, emb, fut in zip(sub_texts, embeddings, sub_futures):
                    to_cache[orig_text] = emb   # cache keyed by original text
                    if not fut.done():
                        fut.set_result(emb)
                await self._cache.set_many(to_cache)

                logger.debug(
                    f"OllamaEmbedder {label} [←{ollama_url} ✓]: "
                    f"sub_batch={len(sub_texts)} HTTP={resp.status_code}"
                )
                return  # success

            except httpx.HTTPStatusError as exc:
                body      = exc.response.text[:600]
                last_exc  = exc
                logger.warning(
                    f"OllamaEmbedder {label} [←{ollama_url} HTTP {exc.response.status_code}] "
                    f"attempt={attempt + 1}/3 sub_batch={len(sub_texts)} "
                    f"total_chars={total_chars} | ollama_body={body!r}"
                )
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning(
                    f"OllamaEmbedder {label} [←{ollama_url} TIMEOUT] "
                    f"attempt={attempt + 1}/3 sub_batch={len(sub_texts)} "
                    f"total_chars={total_chars}"
                )
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    f"OllamaEmbedder {label} [←{ollama_url} ERROR] "
                    f"{type(exc).__name__}: {exc} | "
                    f"attempt={attempt + 1}/3 sub_batch={len(sub_texts)}"
                )
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

        # All 3 attempts failed — per-text fallback to isolate bad chunk.
        # Uses safe_texts (already truncated) so per-text calls also respect the limit.
        logger.warning(
            f"OllamaEmbedder {label} [sub_batch ✗ → per-text fallback on {ollama_url}]: "
            f"{len(safe_texts)} texts failed 3 attempts — last_err={last_exc}"
        )
        _ZERO = [0.0] * 768
        for i, (orig_text, safe_text, fut) in enumerate(zip(sub_texts, safe_texts, sub_futures)):
            if fut.done():
                continue
            try:
                r = await client.post(
                    "/api/embed",
                    json={"model": OLLAMA_MODEL, "input": [safe_text]},
                )
                r.raise_for_status()
                emb = r.json()["embeddings"][0]
                fut.set_result(emb)
                await self._cache.set_many({orig_text: emb})
                logger.debug(f"OllamaEmbedder {label} [per-text ✓]: idx={i} chars={len(safe_text)}")
            except Exception as per_exc:
                logger.error(
                    f"OllamaEmbedder {label} [per-text ✗]: idx={i} chars={len(safe_text)} "
                    f"err={type(per_exc).__name__}: {per_exc} | "
                    f"text_preview={safe_text[:120]!r} — zero vector assigned"
                )
                fut.set_result(_ZERO)

    async def _loop(self, worker_id: int = 0) -> None:
        """
        Single accumulator loop — collects up to OLLAMA_WORKERS × BATCH_SIZE texts,
        splits into OLLAMA_WORKERS sub-batches, fires them concurrently.

        Sub-batch N is routed to self._clients[N % len(self._clients)] so load
        spreads evenly across all Ollama instances when multiple URLs are configured.

        WHY ONE LOOP (not N):
        With N loops all awaiting queue.get(), each grabs 1-4 texts before the 50ms
        window expires (convoy effect). Result: N tiny Ollama calls instead of N full
        batches. One loop with concurrent dispatch gives full batches every time.

        Log prefix [w0/sN] = loop worker 0 / sub-batch N (for filtering in log files).
        """
        W = f"[w{worker_id}]"
        n_instances = len(self._clients)
        urls_str    = ", ".join(OLLAMA_URLS)
        logger.info(
            f"OllamaEmbedder {W}: accumulator loop started "
            f"(OLLAMA_WORKERS={OLLAMA_WORKERS} mega_batch={OLLAMA_WORKERS * BATCH_SIZE} "
            f"instances={n_instances}) → [{urls_str}]"
        )

        while True:
            mega_texts:   list[str]            = []
            mega_futures: list[asyncio.Future] = []

            try:
                # ── Wait for first item ────────────────────────────
                text, fut = await self._q.get()
                mega_texts.append(text)
                mega_futures.append(fut)

                # ── Drain up to OLLAMA_WORKERS × BATCH_SIZE in BATCH_WAIT_MS ──
                target   = OLLAMA_WORKERS * BATCH_SIZE
                deadline = asyncio.get_running_loop().time() + BATCH_WAIT_MS / 1000.0
                while len(mega_texts) < target:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        text, fut = await asyncio.wait_for(self._q.get(), timeout=remaining)
                        mega_texts.append(text)
                        mega_futures.append(fut)
                    except asyncio.TimeoutError:
                        break

                total_chars = sum(len(t) for t in mega_texts)
                logger.debug(
                    f"OllamaEmbedder {W} [mega]: accumulated={len(mega_texts)} "
                    f"total_chars={total_chars} queue_remaining={self._q.qsize()}"
                )

                # ── Split into sub-batches of BATCH_SIZE, fire concurrently ──
                # Each sub-batch goes to a different Ollama instance (round-robin by index).
                sub_batches: list[tuple[list[str], list[asyncio.Future]]] = []
                for i in range(0, len(mega_texts), BATCH_SIZE):
                    sub_batches.append((
                        mega_texts[i:i + BATCH_SIZE],
                        mega_futures[i:i + BATCH_SIZE],
                    ))

                await asyncio.gather(*[
                    self._send_sub_batch(
                        sub_t, sub_f,
                        f"{W}/s{idx}",
                        self._clients[idx % n_instances],
                    )
                    for idx, (sub_t, sub_f) in enumerate(sub_batches)
                ])

            except Exception as unexpected:
                # Unexpected error outside Ollama calls (queue teardown, etc.).
                # Fail in-progress futures so callers don't hang, restart loop.
                logger.error(
                    f"OllamaEmbedder {W} [unexpected crash]: "
                    f"{type(unexpected).__name__}: {unexpected} — restarting in 1s"
                )
                for fut in mega_futures:
                    if not fut.done():
                        fut.set_exception(unexpected)
                await asyncio.sleep(1)


class OpenAIEmbedder:
    """
    Embedder via OpenAI text-embedding-3-small.
    Used ONLY for docs_kb:* repos.  Also batched but no accumulator needed
    (OpenAI handles up to 2048 inputs per call natively).
    Cache-first same as OllamaEmbedder.
    """

    def __init__(self, cache: EmbedCache):
        self._cache  = cache
        self._client = httpx.AsyncClient(
            # OPENAI_BASE_URL without the /v1 suffix: request paths in this
            # client already include it. Default is unchanged.
            base_url=os.getenv(
                "OPENAI_BASE_URL", "https://api.openai.com/v1"
            ).rstrip("/").removesuffix("/v1") or "https://api.openai.com",
            timeout=OPENAI_TIMEOUT,
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        cached_map = await self._cache.get_many(texts)
        results    = [None] * len(texts)
        uncached_idx, uncached_texts = [], []

        for i, text in enumerate(texts):
            if cached_map[text] is not None:
                results[i] = cached_map[text]
            else:
                uncached_idx.append(i)
                uncached_texts.append(text)

        if not uncached_texts:
            return results

        resp = await self._client.post(
            "/v1/embeddings",
            json={"model": OPENAI_MODEL, "input": uncached_texts, "dimensions": OPENAI_DIMS},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        embeddings = [d["embedding"] for d in sorted(data, key=lambda x: x["index"])]

        to_cache: dict[str, list[float]] = {}
        for i, emb in zip(uncached_idx, embeddings):
            results[i] = emb
            to_cache[uncached_texts[uncached_idx.index(i)]] = emb

        await self._cache.set_many(to_cache)
        return results


class NomicEmbedder:
    """
    Embedder via AiNxt Neuron (or any OpenAI-compatible /v1/embeddings endpoint).

    Configured via env vars:
        NOMIC_EMBED_URL     — base URL, e.g. https://<YOUR_EMBEDDING_SERVICE_URL>/nomicembed
        NOMIC_EMBED_API_KEY — Bearer token
        NOMIC_EMBED_MODEL   — model name, e.g. nomic-embed-text-v1.5
        NOMIC_EMBED_BATCH   — max texts per HTTP call (default 64)
        NOMIC_EMBED_TIMEOUT — request timeout in seconds (default 60)

    Request format (OpenAI-compatible):
        POST <NOMIC_EMBED_URL>/v1/embeddings
        Authorization: Bearer <NOMIC_EMBED_API_KEY>
        { "model": "<NOMIC_EMBED_MODEL>", "input": ["text1", ...] }

    Response format (OpenAI-compatible):
        { "data": [{"index": 0, "embedding": [...]}, ...] }

    Features:
        - Cache-first (Redis SHA256 cache, same as OllamaEmbedder / OpenAIEmbedder)
        - Batches texts into NOMIC_EMBED_BATCH-sized chunks
        - 3 retries with 1s/2s backoff on HTTP errors and timeouts
        - Zero-vector fallback per text if all retries fail (never raises to caller)
        - Raises RuntimeError at construction time if NOMIC_EMBED_URL is not set
    """

    def __init__(self, cache: EmbedCache):
        if not NOMIC_EMBED_URL:
            raise RuntimeError(
                "NomicEmbedder: NOMIC_EMBED_URL is not set. "
                "Set it in .env, e.g. NOMIC_EMBED_URL=https://<YOUR_EMBEDDING_SERVICE_URL>/nomicembed"
            )
        self._cache  = cache
        self._client = httpx.AsyncClient(
            base_url=NOMIC_EMBED_URL,
            timeout=NOMIC_EMBED_TIMEOUT,
            headers={
                "Authorization": f"Bearer {NOMIC_EMBED_API_KEY}",
                "Content-Type":  "application/json",
            },
        )
        logger.info(
            f"NomicEmbedder: configured → {NOMIC_EMBED_URL} "
            f"model={NOMIC_EMBED_MODEL} batch={NOMIC_EMBED_BATCH} "
            f"timeout={NOMIC_EMBED_TIMEOUT}s"
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """
        Embed a list of texts.  Returns embeddings in the same order.
        Cache-first: cached texts skip the HTTP call entirely.
        Texts are sent in batches of NOMIC_EMBED_BATCH to respect gateway limits.
        """
        if not texts:
            return []

        # ── Cache lookup ───────────────────────────────────────
        cached_map   = await self._cache.get_many(texts)
        results      = [None] * len(texts)
        uncached_idx: list[int] = []
        uncached_texts: list[str] = []

        for i, text in enumerate(texts):
            if cached_map[text] is not None:
                results[i] = cached_map[text]
            else:
                uncached_idx.append(i)
                uncached_texts.append(text)

        cache_hits = len(texts) - len(uncached_texts)
        logger.debug(
            f"NomicEmbedder [cache]: hits={cache_hits}/{len(texts)} "
            f"({100 * cache_hits // len(texts)}%) pending={len(uncached_texts)}"
        )

        if not uncached_texts:
            return results  # 100% cache hit

        # ── Batch and call ─────────────────────────────────────
        for batch_start in range(0, len(uncached_texts), NOMIC_EMBED_BATCH):
            batch_texts = uncached_texts[batch_start: batch_start + NOMIC_EMBED_BATCH]
            batch_idx   = uncached_idx[batch_start: batch_start + NOMIC_EMBED_BATCH]
            label       = f"[batch {batch_start // NOMIC_EMBED_BATCH}]"

            embeddings = await self._call_with_retry(batch_texts, label)

            to_cache: dict[str, list[float]] = {}
            for orig_idx, text, emb in zip(batch_idx, batch_texts, embeddings):
                results[orig_idx] = emb
                to_cache[text]    = emb
            await self._cache.set_many(to_cache)

        return results

    async def _call_with_retry(
        self,
        texts: list[str],
        label: str,
    ) -> list[list[float]]:
        """
        POST /v1/embeddings for a single batch.  Retries 3× with backoff.
        Returns a zero-vector per text if all attempts fail — never raises.
        """
        _ZERO = [0.0] * NOMIC_EMBED_DIMS
        safe_texts  = [t[:_MAX_TEXT_CHARS] for t in texts]
        n_truncated = sum(1 for o, s in zip(texts, safe_texts) if len(o) != len(s))
        if n_truncated:
            logger.warning(
                f"NomicEmbedder {label}: truncated {n_truncated}/{len(texts)} texts "
                f"to {_MAX_TEXT_CHARS} chars"
            )

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.post(
                    "/v1/embeddings",
                    json={"model": NOMIC_EMBED_MODEL, "input": safe_texts},
                )
                resp.raise_for_status()
                data       = resp.json()["data"]
                embeddings = [
                    d["embedding"]
                    for d in sorted(data, key=lambda x: x["index"])
                ]
                logger.debug(
                    f"NomicEmbedder {label} [✓]: "
                    f"texts={len(texts)} HTTP={resp.status_code} "
                    f"dim={len(embeddings[0]) if embeddings else 0}"
                )
                return embeddings

            except httpx.HTTPStatusError as exc:
                body     = exc.response.text[:400]
                last_exc = exc
                logger.warning(
                    f"NomicEmbedder {label} [HTTP {exc.response.status_code}] "
                    f"attempt={attempt + 1}/3 texts={len(texts)} | body={body!r}"
                )
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning(
                    f"NomicEmbedder {label} [TIMEOUT] "
                    f"attempt={attempt + 1}/3 texts={len(texts)}"
                )
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    f"NomicEmbedder {label} [ERROR] "
                    f"{type(exc).__name__}: {exc} | attempt={attempt + 1}/3"
                )
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

        # All 3 attempts failed — return zero vectors so callers don't hang
        logger.error(
            f"NomicEmbedder {label} [✗]: all 3 attempts failed for {len(texts)} texts "
            f"— last_err={last_exc} — zero vectors assigned"
        )
        return [_ZERO] * len(texts)

    async def close(self) -> None:
        await self._client.aclose()
