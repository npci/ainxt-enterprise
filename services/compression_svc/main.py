# SPDX-License-Identifier: Apache-2.0
"""
Phase 3 — LLMLingua-2 Compression Service  (port 8005)

Runs SEPARATELY from uvicorn / embed_svc — never inside the main gateway process.
Loaded at module import time (device="cpu") per platform ML model policy.

ONLY starts when ENABLE_LINGUA_COMPRESS=true.
Apply ONLY to prose namespaces (Confluence, platform docs) — NEVER to code repos.

Start:
    ENABLE_LINGUA_COMPRESS=true uvicorn services.compression_svc.main:app \
        --host 0.0.0.0 --port 8005 --workers 1

Or via PM2 (see deploy/ainxt-compression.config.js).
"""
import hashlib
import json
import logging
import os
import sys
import time
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
try:
    from core.logger import logger
    logger.info("[compression_svc] Using platform structlog logger")
except Exception:
    logger = logging.getLogger(__name__)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logger.warning(f"[compression_svc] core.logger import failed: {_log_err}")

try:
    from dotenv import load_dotenv as _ld
    _svc_dir  = os.path.dirname(os.path.abspath(__file__))
    _root_dir = os.path.dirname(os.path.dirname(_svc_dir))
    _ld(os.path.join(_svc_dir,  ".env"), override=False)
    _ld(os.path.join(_root_dir, ".env"), override=False)
except Exception:
    pass

# ── Guard: only proceed if explicitly enabled ─────────────────────────────────
_ENABLED = os.getenv("ENABLE_LINGUA_COMPRESS", "").lower() in ("true", "1", "yes")
if not _ENABLED:
    logger.warning(
        "[compression_svc] ENABLE_LINGUA_COMPRESS is not set — "
        "starting in stub mode (all /compress calls return originals)"
    )

# ── Load LLMLingua-2 at module import time (never lazy-load ML models) ────────
# Model: llmlingua-2-xlm-roberta-large-meetingbank — best recall on prose/docs
# CPU-safe: device_map="cpu", no MPS, no CUDA required
_compressor = None
if _ENABLED:
    try:
        # ── Tiktoken offline cache (air-gapped / restricted network) ─────
        # tiktoken downloads cl100k_base.tiktoken from Azure Blob at init.
        # On AiNxt's restricted network this fails with NameResolutionError.
        # Fix: pre-cache the encoding file and set TIKTOKEN_CACHE_DIR.
        #
        # One-time setup (from a machine with internet):
        #   wget -O cl100k_base.tiktoken \
        #       "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
        #   mkdir -p services/compression_svc/tiktoken_cache
        #   HASH=$(echo -n "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken" | sha1sum | cut -d' ' -f1)
        #   cp cl100k_base.tiktoken services/compression_svc/tiktoken_cache/$HASH
        # ─────────────────────────────────────────────────────────────────
        _tiktoken_cache = os.getenv(
            "TIKTOKEN_CACHE_DIR",
            os.path.join(_svc_dir, "tiktoken_cache")
        )
        if os.path.isdir(_tiktoken_cache):
            os.environ["TIKTOKEN_CACHE_DIR"] = _tiktoken_cache
            logger.info(f"[compression_svc] Tiktoken offline cache: {_tiktoken_cache}")
        else:
            logger.warning(
                f"[compression_svc] Tiktoken cache dir not found: {_tiktoken_cache} — "
                "tiktoken will attempt online download (may fail on restricted networks)"
            )
        from llmlingua import PromptCompressor
        _MODEL_NAME = os.getenv(
            "LINGUA_MODEL",
            "microsoft/llmlingua-2-xlm-roberta-large-meetingbank"
        )
        logger.info(f"[compression_svc] Loading LLMLingua-2 model: {_MODEL_NAME}")
        _t0 = time.time()
        _compressor = PromptCompressor(
            model_name=_MODEL_NAME,
            use_llmlingua2=True,
            device_map="cpu",
        )
        # ── FIX: llmlingua's is_begin_of_new_word() matches against model_name
        # string to detect tokenizer type (SentencePiece vs BPE).
        # When loading from a local path, the match fails → NotImplementedError.
        # Override to canonical HF name so XLM-RoBERTa tokenizer logic is used.
        if _MODEL_NAME.startswith("/") or _MODEL_NAME.startswith("."):
            _compressor.model_name = "microsoft/llmlingua-2-xlm-roberta-large-meetingbank"
            logger.info(
                f"[compression_svc] Overrode model_name for tokenizer detection: "
                f"{_MODEL_NAME} → {_compressor.model_name}"
            )
        logger.info(
            f"[compression_svc] LLMLingua-2 loaded in {time.time() - _t0:.1f}s"
        )
    except ImportError:
        logger.error(
            "[compression_svc] llmlingua package not installed. "
            "Run: pip install llmlingua"
        )
    except Exception:
        logger.error(f"[compression_svc] Failed to load LLMLingua-2: {exc}")

# ── KV cache (default db=9, shared with compress_metrics namespace) ────────────
# Backend selected per-DB via
# REDIS_CLIENT_CONFIG_DB{n}. Override the DB number with LINGUA_CACHE_DB.
_CACHE_TTL = 24 * 3600
_cache_db = int(os.getenv("LINGUA_CACHE_DB", "9"))
try:
    from core.kv import get_kv
    _rc = get_kv(_cache_db, decode_responses=True)
    _rc.ping()
    logger.info(f"[compression_svc] KV cache connected (db={_cache_db}, backend={_rc.backend})")
except Exception:
    logger.warning(f"[compression_svc] KV cache unavailable — cache disabled: {_re}")
    _rc = None


def _cache_key(chunk: str, ratio: float) -> str:
    return f"lingua:{hashlib.sha256(f'{chunk}|{ratio}'.encode()).hexdigest()[:24]}"


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(title="AiNxt Compression Service", version="1.0.0")


class CompressRequest(BaseModel):
    chunks:   List[str]
    question: Optional[str] = ""
    ratio:    float = 0.5     # fraction of tokens to KEEP (0.5 = 50% of original)


class CompressResponse(BaseModel):
    chunks:       List[str]
    ratio:        float
    before_chars: int
    after_chars:  int
    reduction_pct: float
    cached_hits:  int


@app.get("/health")
def health():
    return {
        "status":  "ok",
        "enabled": _ENABLED,
        "model_loaded": _compressor is not None,
        "cache": _rc is not None,
    }


@app.post("/compress", response_model=CompressResponse)
def compress(req: CompressRequest):
    """
    Compress a list of RAG chunks using LLMLingua-2.

    Safe to call even when model is not loaded — returns originals with a warning.
    Only applies to prose content (Confluence, platform docs).
    The caller (hybrid_retriever.py) is responsible for namespace gating.
    """
    before_chars = sum(len(c) for c in req.chunks)
    cached_hits  = 0

    if not req.chunks:
        return CompressResponse(
            chunks=[], ratio=req.ratio,
            before_chars=0, after_chars=0, reduction_pct=0.0, cached_hits=0
        )

    # Return originals if model not loaded
    if _compressor is None:
        logger.warning("[compression_svc] /compress called but model not loaded — returning originals")
        return CompressResponse(
            chunks=req.chunks, ratio=req.ratio,
            before_chars=before_chars, after_chars=before_chars,
            reduction_pct=0.0, cached_hits=0
        )

    compressed_chunks: list[str] = []

    for chunk in req.chunks:
        # Check cache first
        ck = _cache_key(chunk, req.ratio)
        if _rc:
            try:
                cached = _rc.get(ck)
                if cached:
                    compressed_chunks.append(cached)
                    cached_hits += 1
                    continue
            except Exception:
                pass

        # Skip very short chunks (< 200 chars) — compression overhead not worth it
        if len(chunk) < 200:
            compressed_chunks.append(chunk)
            continue

        try:
            result = _compressor.compress_prompt(
                context=[chunk],
                question=req.question or "",
                rate=req.ratio,
                # condition_in_question: helps preserve tokens relevant to the query
                condition_in_question=bool(req.question),
            )
            compressed = result.get("compressed_prompt", chunk)

            # Safety: if compression made it longer or empty, use original
            if not compressed or len(compressed) >= len(chunk):
                compressed = chunk

            compressed_chunks.append(compressed)

            # Cache result
            if _rc:
                try:
                    _rc.setex(ck, _CACHE_TTL, compressed)
                except Exception:
                    pass

        except Exception:
            logger.warning(
                f"[compression_svc] compress chunk failed: "
                f": {exc!r}",
                exc_info=True   # ← prints full traceback to logs
            )

    after_chars = sum(len(c) for c in compressed_chunks)
    reduction   = round(100 * (1 - after_chars / max(before_chars, 1)), 1)

    logger.info(
        f"[compression_svc] {len(req.chunks)} chunks "
        f"{before_chars:,}→{after_chars:,} chars "
        f"({reduction:.0f}% reduction) "
        f"cache_hits={cached_hits}"
    )

    return CompressResponse(
        chunks=compressed_chunks,
        ratio=req.ratio,
        before_chars=before_chars,
        after_chars=after_chars,
        reduction_pct=reduction,
        cached_hits=cached_hits,
    )
