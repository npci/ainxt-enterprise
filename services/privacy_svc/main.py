#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# ============================================================
# PRIVACY FILTER SERVICE — FastAPI microservice on port 8004
#
# Runs the openai/privacy-filter ML model for context-aware
# PII detection that augments the regex-based compliance engine.
#
# Implementation uses onnxruntime + tokenizers directly to avoid
# trust_remote_code requirement (custom architecture not yet in
# transformers). ONNX q4f16 quantized model for fast CPU inference.
#
# Endpoints:
#   POST /filter  { texts: [str] }
#                 → { results: [[{entity_group, word, score, start, end}]], cached: [bool], latency_ms: float }
#
#   POST /screen  { text: str }
#                 → { pii_found: bool, entities: [{entity_group, word, score}] }
#
#   GET  /health  → { status, model_loaded, cache_connected }
#
# Start:
#   uvicorn services.privacy_svc.main:app --port 8004 --workers 1
# ============================================================

import os
import sys
import time
import json
import shutil
import hashlib
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from typing import List

import asyncio
import functools
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from dotenv import load_dotenv as _ld
    _svc_dir  = os.path.dirname(os.path.abspath(__file__))
    _root_dir = os.path.dirname(os.path.dirname(_svc_dir))
    _ld(os.path.join(_svc_dir,  ".env"), override=False)
    _ld(os.path.join(_root_dir, ".env"), override=False)
except Exception:
    pass

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.config import RDB_PRIVACY
from core.kv import AsyncKVClient, async_get_kv
from core.logger import logger, set_request_id, set_chat_context, set_span_id

# ── Config ────────────────────────────────────────────────────
_DEFAULT_MODEL_PATH = os.path.expanduser(
    "~/.cache/huggingface/hub/models--openai--privacy-filter/snapshots/"
    "7ffa9a043d54d1be65afb281eddf0ffbe629385b"
)
MODEL_PATH       = os.getenv("PRIVACY_MODEL_PATH", _DEFAULT_MODEL_PATH)
PRIVACY_SVC_PORT = int(os.getenv("PRIVACY_SVC_PORT", "8004"))

# Resolved ONNX cache — ort rejects symlinks that escape the model dir
_ONNX_CACHE_DIR  = os.path.expanduser("~/.cache/ainxt/privacy_onnx")
_ONNX_FILENAME   = "model_fp16.onnx"    # FP16 — better recall than q4f16, still CPU-friendly

_CACHE_TTL    = 3600
_CACHE_PREFIX = "priv:"

# Audit log — append-only JSONL written when PRIVACY_AUDIT_LOG is set.
# Each line: {ts, input, entities} — what the ML model received and detected.
_PRIVACY_AUDIT_LOG = os.getenv("PRIVACY_AUDIT_LOG")
_audit_lock = __import__("threading").Lock()


def _write_privacy_audit(texts: list, results: list) -> None:
    if not _PRIVACY_AUDIT_LOG:
        return
    try:
        ts = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
        lines = []
        for text, entities in zip(texts, results):
            lines.append(json.dumps({
                "ts":       ts,
                "input":    text,
                "entities": entities,
            }))
        with _audit_lock:
            with open(_PRIVACY_AUDIT_LOG, "a") as f:
                f.write("\n".join(lines) + "\n")
    except Exception as e:
        logger.warning(f"PrivacySvc: audit log write failed: {e}")


# ── Resolve ONNX symlinks on first use ────────────────────────
def _prepare_onnx_dir() -> str:
    """Copy ONNX + data file(s) with resolved symlinks to a flat cache dir.

    HuggingFace hub stores files as symlinks to content-addressed blobs.
    onnxruntime validates that external data files don't escape the model
    directory — but resolved symlinks do. This function copies once.

    Different model variants use different data file naming:
      q4f16: model_q4f16.onnx_data          (single file)
      fp16:  model_fp16.onnx_data_1, _data_2 (numbered shards)
    We glob for all matching data files so this works for any variant.
    """
    import glob

    dst_onnx = os.path.join(_ONNX_CACHE_DIR, _ONNX_FILENAME)

    # Validate cache: onnx file must exist AND at least one data shard must exist.
    # Checking only the .onnx file misses the case where a previous start copied
    # the graph file but the data shards (the actual weights, ~900MB) were absent.
    if os.path.exists(dst_onnx):
        existing_shards = glob.glob(os.path.join(_ONNX_CACHE_DIR, _ONNX_FILENAME + "_data*"))
        src_shards      = glob.glob(os.path.join(MODEL_PATH, "onnx", _ONNX_FILENAME + "_data*"))
        if existing_shards or not src_shards:
            # Cache is complete, or source has no data files (model is self-contained)
            return _ONNX_CACHE_DIR
        # Data shards exist in source but not in cache — fall through to copy
        logger.warning(
            f"PrivacySvc: cache incomplete — {_ONNX_FILENAME} present but data shards missing. "
            f"Re-copying {len(src_shards)} shard(s)..."
        )

    logger.info(f"PrivacySvc: resolving ONNX symlinks → {_ONNX_CACHE_DIR} (one-time copy)...")
    os.makedirs(_ONNX_CACHE_DIR, exist_ok=True)

    src_onnx = os.path.join(MODEL_PATH, "onnx", _ONNX_FILENAME)
    shutil.copy2(os.path.realpath(src_onnx), dst_onnx)

    # Copy all companion data shards: _data OR _data_1, _data_2, ...
    src_data_files = glob.glob(os.path.join(MODEL_PATH, "onnx", _ONNX_FILENAME + "_data*"))
    logger.info(f"TS- src_data_files model path to check - {src_data_files}")
    if not src_data_files:
        logger.warning(f"PrivacySvc: no external data files found for {_ONNX_FILENAME}")
    for src_data in sorted(src_data_files):
        fname = os.path.basename(src_data)
        shutil.copy2(os.path.realpath(src_data), os.path.join(_ONNX_CACHE_DIR, fname))
        logger.info(f"PrivacySvc: copied {fname}")

    logger.info(f"PrivacySvc: ONNX files ready - {_ONNX_FILENAME}")
    return _ONNX_CACHE_DIR


# ── Load model at module import time (NEVER lazy-load) ────────
# Uses onnxruntime + tokenizers directly — no trust_remote_code needed.
_sess       = None
_tok        = None
_id2label   = {}
_model_loaded = False

try:
    import onnxruntime as ort
    from tokenizers import Tokenizer

    _onnx_dir = _prepare_onnx_dir()

    with open(os.path.join(MODEL_PATH, "config.json")) as _f:
        _cfg = json.load(_f)
    _id2label = _cfg["id2label"]   # {"0": "O", "1": "B-account_number", ...}

    # Prefer CoreML on Apple Silicon (offloads to Neural Engine, frees CPU RAM).
    # Falls back to CPU automatically if CoreML is unavailable.
    _providers = (
        ["CoreMLExecutionProvider", "CPUExecutionProvider"]
        if "CoreMLExecutionProvider" in ort.get_available_providers()
        else ["CPUExecutionProvider"]
    )
    _sess = ort.InferenceSession(
        os.path.join(_onnx_dir, _ONNX_FILENAME),
        providers=_providers,
    )
    logger.info(f"PrivacySvc: using providers={_providers}")
    _tok = Tokenizer.from_file(os.path.join(MODEL_PATH, "tokenizer.json"))
    _model_loaded = True
    logger.info(f"PrivacySvc: ONNX model and tokenizer loaded - {_ONNX_FILENAME}")

except Exception as _load_err:
    logger.error(f"PrivacySvc: FATAL — model load failed: {_load_err}")
    _model_loaded = False

# Thread pool for CPU inference — keeps asyncio event loop non-blocking
_infer_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="privacy-infer")

# KV cache (DB=8 — RDB_PRIVACY). Backend selected via REDIS_CLIENT_CONFIG_DB8.
_redis: AsyncKVClient | None = None


def _cache_key(text: str) -> str:
    return _CACHE_PREFIX + hashlib.sha256(text.encode()).hexdigest()[:32]


# ── BIOES entity decoding ─────────────────────────────────────

def _decode_bioes(tokens: list, labels: list, offsets: list, text: str) -> list:
    """Convert per-token BIOES predictions into entity spans."""
    entities = []
    current_type  = None
    current_start = None
    current_end   = None

    def _flush():
        if current_type and current_start is not None:
            word = text[current_start:current_end]
            entities.append({
                "entity_group": current_type,
                "word":         word.strip(),
                "start":        current_start,
                "end":          current_end,
            })

    for i, (label, (tok_start, tok_end)) in enumerate(zip(labels, offsets)):
        if label == "O":
            _flush()
            current_type = current_start = current_end = None
            continue

        prefix, etype = label.split("-", 1) if "-" in label else ("O", label)

        if prefix == "B":
            _flush()
            current_type  = etype
            current_start = tok_start
            current_end   = tok_end
        elif prefix == "I" and current_type == etype:
            current_end = tok_end
        elif prefix == "E" and current_type == etype:
            current_end = tok_end
            _flush()
            current_type = current_start = current_end = None
        elif prefix == "S":
            _flush()
            word = text[tok_start:tok_end]
            entities.append({
                "entity_group": etype,
                "word":         word.strip(),
                "start":        tok_start,
                "end":          tok_end,
            })
            current_type = current_start = current_end = None
        else:
            # Label mismatch mid-span — close current and restart
            _flush()
            current_type  = etype
            current_start = tok_start
            current_end   = tok_end

    _flush()
    return entities


def _infer_single(text: str) -> list:
    """Run token classification on one text. Returns list of entity dicts."""
    if not _sess or not _tok:
        return []
    try:
        enc            = _tok.encode(text)
        input_ids      = np.array([enc.ids],             dtype=np.int64)
        attention_mask = np.array([enc.attention_mask],  dtype=np.int64)

        logits = _sess.run(
            ["logits"],
            {"input_ids": input_ids, "attention_mask": attention_mask},
        )[0]  # shape [1, seq, 33]

        pred_ids = logits[0].argmax(-1)
        labels   = [_id2label[str(p)] for p in pred_ids]
        offsets  = enc.offsets

        entities = _decode_bioes(enc.tokens, labels, offsets, text)
        # Add confidence scores from softmax max probability
        probs = _softmax(logits[0])
        for ent in entities:
            start = ent["start"]
            # Find token index by character offset
            tok_idx = next(
                (i for i, (s, e) in enumerate(offsets) if s == start),
                0,
            )
            ent["score"] = round(float(probs[tok_idx].max()), 4)
        return entities
    except Exception as e:
        logger.error(f"PrivacySvc inference error: {e}")
        return []


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max(axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)


def _run_inference(texts: List[str]) -> List[List[dict]]:
    """Blocking batch inference — called inside ThreadPoolExecutor."""
    return [_infer_single(t) for t in texts]


# ── Lifespan ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis

    try:
        _redis = await async_get_kv(RDB_PRIVACY, decode_responses=True)
        await _redis.ping()
        logger.info("PrivacySvc: KV cache connected (DB=8)")
    except Exception as e:
        logger.warning(f"PrivacySvc: KV unavailable ({e}) — running without cache")
        _redis = None

    status = "ready" if _model_loaded else "model_load_failed"
    logger.info(f"PrivacySvc: {status} on :{PRIVACY_SVC_PORT}")
    yield

    _infer_pool.shutdown(wait=False)
    if _redis:
        try:
            await _redis.close()
        except Exception:
            pass


# ── App ───────────────────────────────────────────────────────

app = FastAPI(title="AiNxt Privacy Filter Service", version="1.0.0", lifespan=lifespan)

cors_origins = [
    _o.strip() for _o in _os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if _o.strip()
]
logger.info("PrivacySvc: CORS origins → %s", cors_origins or "none (same-origin only)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ── Request / Response models ──────────────────────────────────

class FilterRequest(BaseModel):
    texts:      list[str]
    request_id: str = "-"
    chat_id:    str = "-"
    user_id:    str = "-"
    span_id:    str = "-"


class FilterResponse(BaseModel):
    results:    list[list[dict]]
    cached:     list[bool]
    latency_ms: float = 0.0


class ScreenRequest(BaseModel):
    text:       str
    request_id: str = "-"
    chat_id:    str = "-"
    user_id:    str = "-"
    span_id:    str = "-"


class ScreenResponse(BaseModel):
    pii_found:  bool
    entities:   list[dict]
    latency_ms: float = 0.0


# ── Endpoints ─────────────────────────────────────────────────

@app.post("/filter", response_model=FilterResponse)
async def filter_texts(req: FilterRequest):
    if not req.texts:
        return FilterResponse(results=[], cached=[], latency_ms=0.0)

    if len(req.texts) > 500:
        raise HTTPException(status_code=400, detail="Max 500 texts per request")

    # Propagate caller context into structured logs for this request
    set_request_id(req.request_id)
    set_chat_context(req.user_id, req.chat_id)
    set_span_id(req.span_id)

    total_chars = sum(len(t) for t in req.texts)
    logger.info(
        "PrivacySvc /filter: received",
        text_count=len(req.texts),
        total_chars=total_chars,
    )
    # Full text at DEBUG only — keeps PII out of INFO-level prod logs
    for i, t in enumerate(req.texts):
        logger.debug("PrivacySvc /filter: input_text", index=i, text=t)

    t0 = time.perf_counter()

    results: list = [None] * len(req.texts)
    cached:  list = [False] * len(req.texts)

    # Cache check
    uncached_indices, uncached_texts = [], []
    if _redis:
        try:
            keys        = [_cache_key(t) for t in req.texts]
            cached_vals = await _redis.mget(*keys)
            for i, val in enumerate(cached_vals):
                if val is not None:
                    results[i] = json.loads(val)
                    cached[i]  = True
                    logger.debug("PrivacySvc /filter: cache_hit", index=i)
                else:
                    uncached_indices.append(i)
                    uncached_texts.append(req.texts[i])
        except Exception as e:
            logger.warning("PrivacySvc: cache read error", error=str(e))
            uncached_indices = list(range(len(req.texts)))
            uncached_texts   = list(req.texts)
    else:
        uncached_indices = list(range(len(req.texts)))
        uncached_texts   = list(req.texts)

    # Inference for uncached texts
    infer_start = time.perf_counter()
    if uncached_texts:
        loop     = asyncio.get_event_loop()
        inferred = await loop.run_in_executor(
            _infer_pool,
            functools.partial(_run_inference, uncached_texts),
        )
        for idx, entities in zip(uncached_indices, inferred):
            results[idx] = entities

        if _redis:
            try:
                pipe = _redis.pipeline()
                for idx, entities in zip(uncached_indices, inferred):
                    pipe.setex(_cache_key(req.texts[idx]), _CACHE_TTL, json.dumps(entities))
                await pipe.execute()
            except Exception as e:
                logger.warning("PrivacySvc: cache write error", error=str(e))

    infer_ms   = (time.perf_counter() - infer_start) * 1000
    latency_ms = (time.perf_counter() - t0) * 1000

    # Write audit log (non-blocking best-effort)
    _write_privacy_audit(req.texts, [r or [] for r in results])

    all_entities = [e for r in results if r for e in r]
    entity_types = list(dict.fromkeys(e.get("entity_group", "") for e in all_entities))

    logger.info(
        "PrivacySvc /filter: done",
        text_count=len(req.texts),
        total_chars=total_chars,
        cache_hits=sum(cached),
        inferred=len(uncached_texts),
        entity_count=len(all_entities),
        entity_types=entity_types,
        infer_ms=round(infer_ms, 1),
        latency_ms=round(latency_ms, 1),
    )
    # Full entity details (matched values + scores) at DEBUG only
    logger.debug("PrivacySvc /filter: entities", results=results)

    return FilterResponse(
        results=[r or [] for r in results],
        cached=cached,
        latency_ms=round(latency_ms, 2),
    )


@app.post("/screen", response_model=ScreenResponse)
async def screen_text(req: ScreenRequest):
    set_request_id(req.request_id)
    set_chat_context(req.user_id, req.chat_id)
    set_span_id(req.span_id)

    t0 = time.perf_counter()
    logger.info("PrivacySvc /screen: received", text_len=len(req.text))
    logger.debug("PrivacySvc /screen: input_text", text=req.text)

    resp = await filter_texts(FilterRequest(
        texts=[req.text],
        request_id=req.request_id,
        chat_id=req.chat_id,
        user_id=req.user_id,
        span_id=req.span_id,
    ))
    entities   = resp.results[0] if resp.results else []
    latency_ms = (time.perf_counter() - t0) * 1000

    logger.info(
        "PrivacySvc /screen: done",
        pii_found=bool(entities),
        entity_count=len(entities),
        entity_types=list(dict.fromkeys(e.get("entity_group", "") for e in entities)),
        latency_ms=round(latency_ms, 1),
    )
    logger.debug("PrivacySvc /screen: entities", entities=entities)

    return ScreenResponse(
        pii_found=bool(entities),
        entities=entities,
        latency_ms=round(latency_ms, 2),
    )


@app.get("/health")
async def health():
    cache_connected = False
    if _redis:
        try:
            await _redis.ping()
            cache_connected = True
        except Exception:
            pass

    return {
        "status":          "ok" if _model_loaded else "degraded",
        "model_loaded":    _model_loaded,
        "model_path":      MODEL_PATH,
        "onnx_cache":      _ONNX_CACHE_DIR,
        "cache_connected": cache_connected,
        "port":            PRIVACY_SVC_PORT,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("services.privacy_svc.main:app", host="0.0.0.0", port=PRIVACY_SVC_PORT, workers=1)
