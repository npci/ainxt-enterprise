#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# ============================================================
# TRANSLATE SERVICE — FastAPI microservice on port 8006
#
# Endpoints:
#   POST /translate        { text, source_lang, target_lang }
#                          → { translation, source_lang, target_lang, cached }
#
#   POST /translate_batch  { texts: [str], source_lang, target_lang }
#                          → { translations: [str] }
#
#   GET  /health           → { status, models_loaded, cache_ok, device }
#
# Start:
#   uvicorn services.translate_svc.main:app --port 8006 --workers 1
#
# One worker only — the IndicTrans2 models are loaded at module import time
# and are not thread-safe for concurrent generate() calls.
# ============================================================

import os
import sys
import time
from contextlib import asynccontextmanager

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Load service-specific .env first, then root .env — do not override env vars
# already set in the environment (same pattern as embed_svc).
try:
    from dotenv import load_dotenv as _load_dotenv
    _svc_dir  = os.path.dirname(os.path.abspath(__file__))
    _root_dir = os.path.dirname(os.path.dirname(_svc_dir))
    _load_dotenv(os.path.join(_svc_dir,  ".env"), override=False)
    _load_dotenv(os.path.join(_root_dir, ".env"), override=False)
except Exception:
    pass

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from services.translate_svc.cache  import TranslateCache
from services.translate_svc.config import (
    TRANSLATE_SVC_PORT,
    TRANSLATE_DEVICE,
    is_supported,
    to_flores,
)

try:
    from core.logger import logger  # type: ignore
except Exception:
    import logging
    logger = logging.getLogger("translate_svc")

# ── Singletons (created during lifespan) ──────────────────────
_cache: TranslateCache | None = None

# Thread pool for CPU-bound translate_batch() calls — keeps the asyncio
# event loop unblocked while IndicTrans2 model.generate() is running.
_translate_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="translator")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cache

    # 1. Connect Redis translation cache
    _cache = TranslateCache()
    try:
        await _cache.connect()
        logger.info("TranslateSvc: Redis cache connected (db=9)")
    except Exception:
        logger.warning("TranslateSvc: Redis cache unavailable — running without cache")

    # 2. Import translator to confirm models are loaded (they load at import time)
    from services.translate_svc import translator as _tr  # noqa: F401
    logger.info(
        f"TranslateSvc: ready on :{type(TRANSLATE_SVC_PORT).__name__} "
        f"(device={type(TRANSLATE_DEVICE).__name__})"
    )

    yield

    # Shutdown — thread pool graceful drain
    _translate_pool.shutdown(wait=False)


app = FastAPI(title="AiNxt Translate Service", version="1.0.0", lifespan=lifespan)

# ── OpenTelemetry auto-instrumentation (optional) ─────────────
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

        _svc_name = _os.getenv("SERVICE_NAME", "ainxt-translate-svc")
        _provider = _TP(resource=_Res(attributes={_SN: _svc_name}))
        _otlp_ep  = _os.getenv("OTLP_ENDPOINT", "")
        _exporter = _Exp(endpoint=_otlp_ep, insecure=not _otlp_ep.startswith("https"))
        _provider.add_span_processor(_BSP(_exporter))
        _otel_trace.set_tracer_provider(_provider)
        _sgt(_CP([_TCP(), _WBP()]))
        _FAI.instrument_app(app, excluded_urls="health")
        logger.info(f"TranslateSvc: OpenTelemetry active → {type(_otlp_ep).__name__}")
except Exception:
    logger.warning(f"TranslateSvc: OTel init failed (non-fatal)")

cors_origins = [
    _o.strip() for _o in _os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if _o.strip()
]
logger.info("TranslateSvc: CORS origins → %s", cors_origins or "none (same-origin only)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ── Request / Response models ──────────────────────────────────

class TranslateRequest(BaseModel):
    text:        str
    source_lang: str   # ISO code, e.g. "hi", "en", "ta"
    target_lang: str   # ISO code, e.g. "en", "hi", "bn"


class TranslateResponse(BaseModel):
    translation: str
    source_lang: str
    target_lang: str
    cached:      bool = False


class TranslateBatchRequest(BaseModel):
    texts:       list[str]
    source_lang: str
    target_lang: str


class TranslateBatchResponse(BaseModel):
    translations: list[str]
    latency_ms:   float = 0.0


# ── Endpoints ─────────────────────────────────────────────────

@app.post("/translate", response_model=TranslateResponse)
async def translate(req: TranslateRequest):
    # Validate language codes
    if not is_supported(req.source_lang):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported source_lang '{req.source_lang}'",
        )
    if not is_supported(req.target_lang):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported target_lang '{req.target_lang}'",
        )

    # Same-language echo
    if req.source_lang == req.target_lang:
        return TranslateResponse(
            translation=req.text,
            source_lang=req.source_lang,
            target_lang=req.target_lang,
            cached=False,
        )

    # Cache lookup
    if _cache:
        cached_val = await _cache.get(req.text, req.source_lang, req.target_lang)
        if cached_val is not None:
            return TranslateResponse(
                translation=cached_val,
                source_lang=req.source_lang,
                target_lang=req.target_lang,
                cached=True,
            )

    # Translate (blocking model.generate — run in thread pool)
    from services.translate_svc.translator import translate as _translate
    loop = asyncio.get_event_loop()
    try:
        translation = await loop.run_in_executor(
            _translate_pool,
            functools.partial(_translate, req.text, req.source_lang, req.target_lang),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.error("TranslateSvc /translate error")
        raise HTTPException(status_code=500, detail="Translation failed.")

    # Cache result
    if _cache:
        await _cache.set(req.text, req.source_lang, req.target_lang, translation)

    return TranslateResponse(
        translation=translation,
        source_lang=req.source_lang,
        target_lang=req.target_lang,
        cached=False,
    )


@app.post("/translate_batch", response_model=TranslateBatchResponse)
async def translate_batch(req: TranslateBatchRequest):
    if not req.texts:
        return TranslateBatchResponse(translations=[], latency_ms=0.0)

    if not is_supported(req.source_lang):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported source_lang '{req.source_lang}'",
        )
    if not is_supported(req.target_lang):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported target_lang '{req.target_lang}'",
        )

    t0 = time.perf_counter()

    # Same-language short-circuit
    if req.source_lang == req.target_lang:
        latency_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            f"TranslateSvc /translate_batch: {len(req.texts)} text(s) "
            f"{req.source_lang}→{req.target_lang} (same-lang) "
            f"latency={round(latency_ms, 2)}ms"
        )
        return TranslateBatchResponse(
            translations=list(req.texts),
            latency_ms=round(latency_ms, 2),
        )

    # Batch cache lookup
    cache_map: dict[str, str | None] = (
        await _cache.get_many(req.texts, req.source_lang, req.target_lang)
        if _cache
        else {t: None for t in req.texts}
    )

    # Identify misses (preserve order via index)
    misses: list[tuple[int, str]] = [
        (i, text) for i, text in enumerate(req.texts) if cache_map[text] is None
    ]
    miss_texts = [text for _, text in misses]

    if miss_texts:
        src_flores = to_flores(req.source_lang)
        tgt_flores = to_flores(req.target_lang)

        from services.translate_svc.translator import translate_batch as _translate_batch
        loop = asyncio.get_event_loop()
        try:
            translated_misses: list[str] = await loop.run_in_executor(
                _translate_pool,
                functools.partial(_translate_batch, miss_texts, src_flores, tgt_flores),
            )
        except Exception:
            logger.error("TranslateSvc /translate_batch error")
            raise HTTPException(status_code=500, detail="Translation failed.")

        # Write misses back to cache and update the local map
        new_cache_items: dict[str, str] = {}
        for (orig_idx, text), translation in zip(misses, translated_misses):
            cache_map[text] = translation
            new_cache_items[text] = translation

        if _cache and new_cache_items:
            await _cache.set_many(new_cache_items, req.source_lang, req.target_lang)

    # Reassemble in original order
    translations = [cache_map[text] or "" for text in req.texts]

    latency_ms = (time.perf_counter() - t0) * 1000
    cache_hits = len(req.texts) - len(miss_texts)
    logger.info(
        f"TranslateSvc /translate_batch: {len(req.texts)} text(s) "
        f"{req.source_lang}→{req.target_lang} "
        f"cache_hits={cache_hits} misses={len(miss_texts)} "
        f"latency={round(latency_ms, 2)}ms"
    )
    return TranslateBatchResponse(
        translations=translations,
        latency_ms=round(latency_ms, 2),
    )


@app.get("/health")
async def health():
    # Check whether translator models are loaded
    models_loaded = False
    try:
        from services.translate_svc import translator as _tr  # noqa: F401
        # If both model objects exist and are in eval mode, we're good
        models_loaded = (
            _tr._indic_en_model is not None
            and _tr._en_indic_model is not None
        )
    except Exception:
        models_loaded = False

    # Check Redis cache connectivity
    cache_ok = False
    try:
        if _cache and _cache._r:
            await _cache._r.ping()
            cache_ok = True
    except Exception:
        pass

    status = "ok" if models_loaded else "degraded"
    return {
        "status":        status,
        "models_loaded": models_loaded,
        "cache_ok":      cache_ok,
        "device":        TRANSLATE_DEVICE,
        "port":          TRANSLATE_SVC_PORT,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "services.translate_svc.main:app",
        host="0.0.0.0",
        port=TRANSLATE_SVC_PORT,
        workers=1,
    )
