#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# ============================================================
# EMBED SERVICE — FastAPI microservice on port 8001
#
# Endpoints:
#   POST /embed   { texts: [str], provider: "ollama"|"openai" }
#                 → { embeddings: [[float]] }
#
#   POST /rerank  { query: str, candidates: [{text, ...}], top_k: int }
#                 → { results: [{text, score, ...}] }
#
#   GET  /health  → { status, ollama_ok, cache_ok, queue_depth }
#
# Start:
#   uvicorn services.embed_svc.main:app --port 8001 --workers 1
#
# One worker only — the batch accumulator is a single asyncio event loop.
# Scale horizontally by running multiple instances behind a load balancer;
# Redis cache is shared so cache hits work across instances.
# ============================================================

import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Load embed-service-specific .env first (services/embed_svc/.env),
# then fall back to root .env — do not override values already in the environment.
try:
    from dotenv import load_dotenv as _load_dotenv
    _svc_dir  = os.path.dirname(os.path.abspath(__file__))
    _root_dir = os.path.dirname(os.path.dirname(_svc_dir))
    _load_dotenv(os.path.join(_svc_dir,  ".env"),  override=False)
    _load_dotenv(os.path.join(_root_dir, ".env"),  override=False)
except Exception:
    pass

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from services.embed_svc.cache    import EmbedCache
from services.embed_svc.embedder import OllamaEmbedder, OpenAIEmbedder, NomicEmbedder
from services.embed_svc.config import (
    EMBED_SVC_PORT, OLLAMA_URL, OLLAMA_URLS, OLLAMA_MODEL, QUEUE_MAXSIZE,
    NOMIC_EMBED_URL, NOMIC_EMBED_MODEL, NOMIC_EMBED_DIMS,
    PARSE_SVC_ENABLED,
)
from core.logger import logger

# ── Singletons (created during lifespan) ──────────────────────
_cache:   EmbedCache      | None = None
_ollama:  OllamaEmbedder  | None = None
_openai:  OpenAIEmbedder  | None = None
_nomic:   NomicEmbedder   | None = None

# Thread pool for CPU-bound reranking — keeps asyncio event loop unblocked.
# bge-reranker-large.predict() is blocking; running it in the pool lets the
# event loop continue serving embed requests while reranking is in progress.
# max_workers=8 → 8 × 16 OMP threads = 128 cores fully utilised.
_rerank_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="reranker")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cache, _ollama, _openai, _nomic

    # 1. Connect cache
    _cache = EmbedCache()
    try:
        await _cache.connect()
        logger.info("EmbedSvc: Redis embed cache connected (db=7)")
    except Exception as e:
        logger.warning(f"EmbedSvc: Redis cache unavailable ({e}) — running without cache")

    # 2. Start batch embedders
    _ollama = OllamaEmbedder(_cache)
    await _ollama.start()
    logger.info("EmbedSvc: OllamaEmbedder started")

    # 3. Startup probe — actually embed a test text to confirm nomic is loaded and
    #    returning 768-dim vectors.  Logs the exact error if Ollama/nomic is broken
    #    so you don't have to wait for the first real indexing job to discover it.
    try:
        probe_result = await asyncio.wait_for(_ollama.embed(["startup probe: nomic connectivity check"]), timeout=30.0)
        dim = len(probe_result[0]) if probe_result and probe_result[0] else 0
        if dim == 768:
            logger.info(f"EmbedSvc: startup probe OK — nomic returning {dim}-dim vectors")
        else:
            logger.error(f"EmbedSvc: startup probe WRONG DIM — expected 768 got {dim}. Check OLLAMA_EMBED_MODEL.")
    except asyncio.TimeoutError:
        logger.error("EmbedSvc: startup probe TIMEOUT (30s) — Ollama is not responding. Check OLLAMA_URL and that nomic-embed-text is loaded.")
    except Exception as probe_exc:
        logger.error(f"EmbedSvc: startup probe FAILED — {type(probe_exc).__name__}: {probe_exc}. Embedding will fail until Ollama is fixed.")

    _openai = OpenAIEmbedder(_cache)
    logger.info("EmbedSvc: OpenAIEmbedder ready")

    # 4. NomicEmbedder — only started when NOMIC_EMBED_URL is configured.
    #    Skipped silently when the env var is absent (local dev / Ollama-only nodes).
    if NOMIC_EMBED_URL:
        try:
            _nomic = NomicEmbedder(_cache)
            # Startup probe — one real embed call to confirm the Neuron endpoint
            # is reachable and returning the expected vector dimension.
            probe_nomic = await asyncio.wait_for(
                _nomic.embed(["startup probe: nomic neuron connectivity check"]),
                timeout=30.0,
            )
            nomic_dim = len(probe_nomic[0]) if probe_nomic and probe_nomic[0] else 0
            if nomic_dim == NOMIC_EMBED_DIMS:
                logger.info(
                    f"EmbedSvc: NomicEmbedder probe OK — {NOMIC_EMBED_URL} "
                    f"returning {nomic_dim}-dim vectors (model={NOMIC_EMBED_MODEL})"
                )
            else:
                logger.error(
                    f"EmbedSvc: NomicEmbedder probe WRONG DIM — "
                    f"expected {NOMIC_EMBED_DIMS} got {nomic_dim}. "
                    f"Check NOMIC_EMBED_MODEL and NOMIC_EMBED_DIMS."
                )
        except asyncio.TimeoutError:
            logger.error(
                f"EmbedSvc: NomicEmbedder probe TIMEOUT (30s) — "
                f"{NOMIC_EMBED_URL} is not responding. "
                f"provider=nomic requests will return zero vectors."
            )
        except Exception as nomic_exc:
            logger.error(
                f"EmbedSvc: NomicEmbedder probe FAILED — "
                f"{type(nomic_exc).__name__}: {nomic_exc}. "
                f"provider=nomic requests will return zero vectors."
            )
    else:
        logger.info(
            "EmbedSvc: NOMIC_EMBED_URL not set — NomicEmbedder disabled. "
            "Set NOMIC_EMBED_URL to enable provider=nomic."
        )

    # 5. Reranker is loaded at module import time (sentence_transformers)
    from services.embed_svc import reranker as _rr  # noqa: F401 — triggers load
    logger.info("EmbedSvc: reranker module loaded")

    # 6. Document parse service (Docling + PaddleOCR) — only when PARSE_SVC_ENABLED=1.
    #    warm_up() pre-loads the Docling DocumentConverter so the first /parse call
    #    does not pay the cold-start cost (~1-3 s for DocLayNet + TableFormer).
    if PARSE_SVC_ENABLED:
        try:
            from services.embed_svc.parser import warm_up as _parser_warm_up
            _parser_warm_up()
        except Exception as _pe:
            logger.error(f"EmbedSvc: DoclingParser warm-up failed (non-fatal): {_pe}")

    logger.info(f"EmbedSvc: ready on :{EMBED_SVC_PORT}")
    yield

    # Shutdown — close all Ollama instance clients + OpenAI client + Nomic client
    if _ollama:
        for _c in _ollama._clients:
            await _c.aclose()
    if _openai and _openai._client:
        await _openai._client.aclose()
    if _nomic:
        await _nomic.close()


app = FastAPI(title="AiNxt Embed Service", version="1.0.0", lifespan=lifespan)

# ── OpenTelemetry auto-instrumentation ────────────────────────
# Instruments FastAPI (per-request spans) so embed_svc spans appear
# as children of the gateway span that called /embed or /rerank.
# Trace context is propagated via W3C traceparent headers from gateway httpx calls.
try:
    import os as _os
    if _os.getenv("OTLP_ENDPOINT") and _os.getenv("ENABLE_TRACING", "1") == "1":
        from opentelemetry import trace as _otel_trace
        from opentelemetry.sdk.trace import TracerProvider as _TP
        from opentelemetry.sdk.trace.export import BatchSpanProcessor as _BSP
        from opentelemetry.sdk.resources import Resource as _Res, SERVICE_NAME as _SN
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as _Exp
        from opentelemetry.propagate import set_global_textmap as _sgt
        from opentelemetry.propagators.composite import CompositePropagator as _CP
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator as _TCP
        from opentelemetry.baggage.propagation import W3CBaggagePropagator as _WBP
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor as _FAI

        _svc_name = _os.getenv("SERVICE_NAME", "ainxt-embed-svc")
        _provider = _TP(resource=_Res(attributes={_SN: _svc_name}))
        _otlp_ep  = _os.getenv("OTLP_ENDPOINT", "")
        _exporter = _Exp(endpoint=_otlp_ep, insecure=not _otlp_ep.startswith("https"))
        _provider.add_span_processor(_BSP(_exporter))
        _otel_trace.set_tracer_provider(_provider)
        _sgt(_CP([_TCP(), _WBP()]))
        _FAI.instrument_app(app, excluded_urls="health")
        logger.info(f"EmbedSvc: OpenTelemetry active → {_otlp_ep}")
except Exception as _otel_err:
    logger.warning(f"EmbedSvc: OTel init failed (non-fatal): {_otel_err}")

cors_origins = [
    _o.strip() for _o in _os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if _o.strip()
]
logger.info("EmbedSvc: CORS origins → %s", cors_origins or "none (same-origin only)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ── Request / Response models ──────────────────────────────────

class EmbedRequest(BaseModel):
    texts:    list[str]
    provider: str = "ollama"   # "ollama" | "openai" | "nomic"


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    cached:     int = 0   # number of cache hits (informational)
    latency_ms: float = 0.0


class RerankRequest(BaseModel):
    query:      str
    candidates: list[dict]
    top_k:      int = 6


class RerankResponse(BaseModel):
    results:    list[dict]
    latency_ms: float = 0.0


class ParseRequest(BaseModel):
    file_bytes_b64: str   # base64-encoded raw file bytes (PDF / DOCX / HTML / PPTX)
    filename:       str   # original filename — used for logging and temp-file suffix
    file_type:      str   # extension without dot: "pdf" | "docx" | "html" | "pptx"


class ParseResponse(BaseModel):
    content:    str         # parsed markdown text; "" means parse failed → gateway falls back
    latency_ms: float = 0.0


# ── Endpoints ─────────────────────────────────────────────────

@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest):
    if not req.texts:
        return EmbedResponse(embeddings=[])

    if len(req.texts) > 2000:
        raise HTTPException(status_code=400, detail="Max 2000 texts per request")

    lens = [len(t) for t in req.texts]
    q_depth = _ollama._q.qsize() if _ollama else -1
    logger.info(
        f"embed_svc [→]: provider={req.provider} texts={len(req.texts)} "
        f"chars min={min(lens)} max={max(lens)} avg={sum(lens)//len(lens)} "
        f"queue_depth={q_depth}"
    )

    t0 = time.perf_counter()

    try:
        if req.provider == "openai":
            if _openai is None:
                raise HTTPException(status_code=503, detail="OpenAI embedder not ready")
            embeddings = await _openai.embed(req.texts)
        elif req.provider == "nomic":
            if _nomic is None:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Nomic embedder not ready — set NOMIC_EMBED_URL in .env "
                        "and restart the embed service"
                    ),
                )
            embeddings = await _nomic.embed(req.texts)
        else:
            if _ollama is None:
                logger.info(f"Request length received - {len(req.texts)}")
                raise HTTPException(status_code=503, detail="Ollama embedder not ready")
            embeddings = await _ollama.embed(req.texts)

    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        logger.error(
            f"embed_svc [✗]: {type(exc).__name__}: {exc} | "
            f"texts={len(req.texts)} max_chars={max(lens)} latency={latency_ms:.0f}ms"
        )
        raise HTTPException(status_code=500, detail=str(exc))

    latency_ms = (time.perf_counter() - t0) * 1000
    logger.info(
        f"embed_svc [✓]: texts={len(req.texts)} latency={latency_ms:.0f}ms "
        f"queue_depth_after={_ollama._q.qsize() if _ollama else -1}"
    )
    return EmbedResponse(
        embeddings=embeddings,
        latency_ms=round(latency_ms, 2),
    )


@app.post("/rerank", response_model=RerankResponse)
async def rerank(req: RerankRequest):
    if not req.candidates:
        return RerankResponse(results=[])

    t0 = time.perf_counter()

    from services.embed_svc.reranker import rerank as _rerank

    # Run blocking CrossEncoder.predict() in thread pool — never blocks event loop
    loop    = asyncio.get_event_loop()
    results = await loop.run_in_executor(
        _rerank_pool,
        functools.partial(_rerank, req.query, req.candidates, req.top_k),
    )

    latency_ms = (time.perf_counter() - t0) * 1000
    return RerankResponse(results=results, latency_ms=round(latency_ms, 2))


@app.post("/parse", response_model=ParseResponse)
async def parse_doc(req: ParseRequest):
    """
    Parse a document (PDF / DOCX / HTML / PPTX) using Docling + PaddleOCR.

    Accepts base64-encoded file bytes, returns parsed markdown text.
    Returns content="" when Docling parsing fails — the gateway interprets
    this as a signal to fall back to its legacy parser chain (markitdown,
    python-docx, etc.) so uploads never break because of this service.

    Only active when PARSE_SVC_ENABLED=1 in the embed service .env.
    """
    if not PARSE_SVC_ENABLED:
        raise HTTPException(
            status_code=503,
            detail=(
                "Parse service not enabled on this instance. "
                "Set PARSE_SVC_ENABLED=1 in the embed service .env and restart."
            ),
        )

    import base64 as _b64
    from services.embed_svc.parser import parse as _parse, is_ready as _parse_ready

    if not _parse_ready():
        raise HTTPException(
            status_code=503,
            detail=(
                "Parse service is not ready — Docling models failed to load at startup. "
                "Check DOCLING_ARTIFACTS_PATH and USE_DOCLING_PARSER in the embed service .env."
            ),
        )

    t0 = time.perf_counter()

    try:
        file_bytes = _b64.b64decode(req.file_bytes_b64)
    except Exception as _de:
        raise HTTPException(status_code=400, detail=f"Invalid base64 in file_bytes_b64: {_de}")

    ft = (req.file_type or "").lower().strip(".")
    logger.info(
        f"parse_svc [→]: '{req.filename}' file_type={ft} "
        f"size={len(file_bytes):,} bytes"
    )

    # Run blocking Docling conversion in the thread pool — keeps the asyncio
    # event loop unblocked (same pattern as the /rerank endpoint).
    from core.docling_parser import PageConversionError

    loop = asyncio.get_event_loop()
    try:
        content = await loop.run_in_executor(
            _rerank_pool,
            functools.partial(_parse, file_bytes, req.filename, ft),
        )
    except PageConversionError as _pce:
        # Specific page batches failed even after retry. Return 422 with the
        # exact message (which lists the failed page ranges and a total page
        # count) so the gateway can store it verbatim in knowledge_docs.parse_error
        # and show it to the user in the KB request/status tab.
        # 422 is used rather than 500 because the request was well-formed —
        # the document itself could not be fully converted.
        latency_ms = (time.perf_counter() - t0) * 1000
        logger.error(
            f"parse_svc [✗]: '{req.filename}' ({ft}) page conversion failed "
            f"latency={latency_ms:.0f}ms detail='{_pce}'"
        )
        raise HTTPException(status_code=422, detail=str(_pce))

    latency_ms = (time.perf_counter() - t0) * 1000
    status_icon = "✓" if content else "∅"
    logger.info(
        f"parse_svc [{status_icon}]: '{req.filename}' ({ft}) "
        f"latency={latency_ms:.0f}ms chars={len(content):,}"
    )
    return ParseResponse(content=content, latency_ms=round(latency_ms, 2))


@app.get("/health")
async def health():
    from services.embed_svc.config import OLLAMA_WORKERS as _OW, BATCH_SIZE as _BS
    from services.embed_svc.reranker import reranker_info

    # ── Ollama connectivity — probe each instance individually ─────
    instance_status: list[dict] = []
    async with httpx.AsyncClient(timeout=5.0) as _hc:
        for _url in OLLAMA_URLS:
            _inst: dict = {"url": _url, "ok": False, "error": ""}
            try:
                _r = await _hc.get(f"{_url}/api/tags")
                _inst["ok"]   = _r.status_code == 200
                _inst["body"] = _r.text[:120]
            except Exception as _e:
                _inst["error"] = str(_e)[:120]
            instance_status.append(_inst)

    ollama_ok   = any(i["ok"] for i in instance_status)
    ollama_body = str(instance_status[0].get("body", instance_status[0].get("error", "")))

    # ── Ollama embed probe (actual nomic call via accumulator) ─────
    ollama_embed_ok  = False
    ollama_embed_dim = 0
    ollama_embed_err = ""
    try:
        if _ollama:
            probe = await asyncio.wait_for(_ollama.embed(["health check"]), timeout=10.0)
            ollama_embed_dim = len(probe[0]) if probe and probe[0] else 0
            ollama_embed_ok  = ollama_embed_dim == 768
            if not ollama_embed_ok:
                ollama_embed_err = f"wrong dim: {ollama_embed_dim}"
    except asyncio.TimeoutError:
        ollama_embed_err = "timeout 10s — Ollama not responding"
    except Exception as e:
        ollama_embed_err = f"{type(e).__name__}: {e}"

    # ── Nomic (Neuron) embed probe ─────────────────────────────────
    nomic_svc_ok  = False
    nomic_svc_dim = 0
    nomic_svc_err = ""
    if _nomic:
        try:
            probe_n = await asyncio.wait_for(_nomic.embed(["health check"]), timeout=10.0)
            nomic_svc_dim = len(probe_n[0]) if probe_n and probe_n[0] else 0
            nomic_svc_ok  = nomic_svc_dim == NOMIC_EMBED_DIMS
            if not nomic_svc_ok:
                nomic_svc_err = f"wrong dim: {nomic_svc_dim} (expected {NOMIC_EMBED_DIMS})"
        except asyncio.TimeoutError:
            nomic_svc_err = f"timeout 10s — {NOMIC_EMBED_URL} not responding"
        except Exception as e:
            nomic_svc_err = f"{type(e).__name__}: {e}"
    else:
        nomic_svc_err = "disabled (NOMIC_EMBED_URL not set)"

    # ── Redis cache ────────────────────────────────────────────────
    cache_ok = False
    try:
        if _cache and _cache._r:
            await _cache._r.ping()
            cache_ok = True
    except Exception:
        pass

    queue_depth = _ollama._q.qsize() if _ollama else -1
    rr          = reranker_info()

    # ── Parse service (Docling + PaddleOCR) ────────────────────────
    parse_svc_ready = False
    if PARSE_SVC_ENABLED:
        try:
            from services.embed_svc.parser import is_ready as _parse_ready
            parse_svc_ready = _parse_ready()
        except Exception:
            pass

    status = "ok" if (ollama_ok and ollama_embed_ok) else "degraded"
    return {
        "status":            status,
        # Ollama server reachability — per-instance detail
        "ollama_ok":         ollama_ok,
        "ollama_instances":  instance_status,
        "ollama_url":        OLLAMA_URL,        # backward-compat (first instance)
        "ollama_tags_body":  ollama_body,
        # Ollama embed probe (nomic-embed-text via local Ollama)
        "ollama_embed_ok":   ollama_embed_ok,
        "ollama_embed_dim":  ollama_embed_dim,
        "ollama_embed_error": ollama_embed_err,
        # Nomic Neuron embed probe (AiNxt Neuron / remote OpenAI-compatible endpoint)
        "nomic_svc_enabled": _nomic is not None,
        "nomic_svc_url":     NOMIC_EMBED_URL or "not configured",
        "nomic_svc_model":   NOMIC_EMBED_MODEL,
        "nomic_svc_ok":      nomic_svc_ok,
        "nomic_svc_dim":     nomic_svc_dim,
        "nomic_svc_error":   nomic_svc_err,
        # embed svc internals
        "cache_ok":          cache_ok,
        "queue_depth":       queue_depth,
        "queue_maxsize":     QUEUE_MAXSIZE,
        "ollama_workers":    _OW,
        "batch_size":        _BS,
        "mega_batch_size":   _OW * _BS,
        # reranker
        "reranker_model":    rr["model"],
        "reranker_loaded":   rr["loaded"],
        "reranker_fallback": rr["fallback"],
        # parse service (Docling + PaddleOCR)
        "parse_svc_enabled": PARSE_SVC_ENABLED,
        "parse_svc_ready":   parse_svc_ready,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("services.embed_svc.main:app", host="0.0.0.0", port=EMBED_SVC_PORT, workers=1)
