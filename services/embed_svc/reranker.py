# SPDX-License-Identifier: Apache-2.0
# ============================================================
# EMBED SERVICE — reranker (BGE CrossEncoder)
#
# Model selection via RERANKER_MODEL env var:
#   BAAI/bge-reranker-large  — prod default, best accuracy (~560MB, nDCG@10 ≈ 0.60)
#   BAAI/bge-reranker-base   — dev / memory-constrained nodes (~279MB, nDCG@10 ≈ 0.57)
#
# Loaded at module IMPORT TIME (startup), never during a request.
# device=cpu, TOKENIZERS_PARALLELISM=false — crash-safe.
#
# Falls back to RRF scoring if model load fails.
# ============================================================

import math
import os
from core.logger import logger

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# ── CPU thread tuning ──────────────────────────────────────────
# On 128-core server: give PyTorch 16 threads per rerank call.
# Keeps one rerank ~300-500ms while leaving cores for other workers.
# Override with RERANKER_THREADS env var if needed.
# 8 pool workers × 12 threads = 96 cores for reranking
# Leaves ~32 cores for Ollama (nomic-embed-text) + OS + gateway workers
# Tune RERANKER_THREADS down if Ollama embed latency degrades under load
_THREADS      = int(os.getenv("RERANKER_THREADS", "12"))
_BATCH_SIZE   = int(os.getenv("RERANKER_BATCH_SIZE", "8"))

os.environ.setdefault("OMP_NUM_THREADS",  str(_THREADS))
os.environ.setdefault("MKL_NUM_THREADS",  str(_THREADS))

# top_k sent into reranker — cap incoming candidates to avoid latency spike
_MAX_CANDIDATES = int(os.getenv("RERANKER_MAX_CANDIDATES", "12"))

# ── Model selection ────────────────────────────────────────────
# Large = best accuracy for prod. Base = fallback for low-memory nodes.
# Override with RERANKER_MODEL env var before starting the embed service.
_DEFAULT_MODEL = "BAAI/bge-reranker-large"
_MODEL_NAME    = os.getenv("RERANKER_MODEL", _DEFAULT_MODEL).strip()

# Phase 6: A/B candidates. Add new models to this set so the embed svc
# accepts them without warnings; the actual A/B traffic split is owned
# upstream by the gateway (or a feature flag in routers/admin).
_VALID_MODELS = {
    "BAAI/bge-reranker-large",
    "BAAI/bge-reranker-base",
    # Phase 6 A/B candidates — opt-in via RERANKER_MODEL or RERANKER_VARIANT
    "BAAI/bge-reranker-v2-m3",
    "Alibaba-NLP/gte-reranker-modernbert-base",
    "jinaai/jina-reranker-v1-tiny-en",
    "jinaai/jina-reranker-v2-base-multilingual",
    "Qwen/Qwen3-Reranker-0.6B",
    # Phase 6 baseline (TinyBERT) — for A/B comparison
    "cross-encoder/ms-marco-TinyBERT-L-2-v2",
}

# RERANKER_VARIANT is a friendly alias system: "bge_large" / "bge_base" /
# "tinybert" / "jina_tiny" / "qwen_06b" map to canonical model names so the
# gateway can route traffic without knowing the underlying HF id.
_VARIANT_MAP = {
    "bge_large":      "BAAI/bge-reranker-large",
    "bge_base":       "BAAI/bge-reranker-base",
    "bge_v2_m3":      "BAAI/bge-reranker-v2-m3",
    "tinybert":       "cross-encoder/ms-marco-TinyBERT-L-2-v2",
    "jina_tiny":      "jinaai/jina-reranker-v1-tiny-en",
    "jina_v2":        "jinaai/jina-reranker-v2-base-multilingual",
    "qwen_06b":       "Qwen/Qwen3-Reranker-0.6B",
    "gte_modernbert": "Alibaba-NLP/gte-reranker-modernbert-base",
}
_VARIANT = os.getenv("RERANKER_VARIANT", "").strip().lower()
if _VARIANT and _VARIANT in _VARIANT_MAP:
    _MODEL_NAME = _VARIANT_MAP[_VARIANT]
    logger.info(f"EmbedSvc reranker: RERANKER_VARIANT={_VARIANT!r} → using {_MODEL_NAME}")

# Allow local directory paths (air-gapped servers with no HuggingFace access),
# but only under an operator-configured base directory — RERANKER_MODEL must
# not be able to point at an arbitrary path and skip the _VALID_MODELS
# allow-list just because that path happens to exist.
_LOCAL_MODEL_BASE_DIR = os.getenv("RERANKER_LOCAL_MODEL_BASE_DIR", "").strip()
_is_local_path = False
if _LOCAL_MODEL_BASE_DIR and os.path.isdir(_MODEL_NAME):
    _base = os.path.realpath(_LOCAL_MODEL_BASE_DIR)
    _candidate = os.path.realpath(_MODEL_NAME)
    _is_local_path = _candidate == _base or _candidate.startswith(_base + os.sep)
    if not _is_local_path:
        logger.warning(
            f"EmbedSvc reranker: RERANKER_MODEL='{_MODEL_NAME}' is a local directory but is "
            f"not under RERANKER_LOCAL_MODEL_BASE_DIR='{_LOCAL_MODEL_BASE_DIR}'. Ignoring."
        )
if not _is_local_path and _MODEL_NAME not in _VALID_MODELS:
    logger.warning(
        f"EmbedSvc reranker: RERANKER_MODEL='{_MODEL_NAME}' is not a recognised reranker model. "
        f"Falling back to {_DEFAULT_MODEL}. Valid values: {sorted(_VALID_MODELS)}"
    )
    _MODEL_NAME = _DEFAULT_MODEL

# ── Load at import time ────────────────────────────────────────
_cross_encoder = None

try:
    from sentence_transformers import CrossEncoder
    logger.info(f"EmbedSvc reranker: loading {_MODEL_NAME} on cpu …")
    # trust_remote_code explicitly False: this project never loads custom
    # modelling code. (CVE-2026-68770's vulnerable dynamic-module loader lives
    # in SentenceTransformer, not in CrossEncoder's transformers-native load
    # path used here — but pin the flag anyway to make the intent explicit.)
    _cross_encoder = CrossEncoder(_MODEL_NAME, device="cpu", max_length=512, trust_remote_code=False)
    # Warmup — avoids cold-start latency on first real request
    _cross_encoder.predict([("warmup query", "warmup document")])
    logger.info(f"EmbedSvc reranker: {_MODEL_NAME} loaded and warmed up")
except Exception as _e:
    logger.warning(
        f"EmbedSvc reranker: failed to load {_MODEL_NAME} ({_e}). "
        f"RRF fallback active — reranking quality will be lower."
    )


# ── Public API ─────────────────────────────────────────────────

# Noise file patterns — filtered BEFORE reranking to save compute
_NOISE_PATTERNS = (
    "license", "licence", "changelog", "contributing", "readme",
    ".min.js", ".min.css", "package-lock.json", "yarn.lock",
    ".gitignore", ".gitattributes", "__pycache__", ".pyc",
)


def _is_noise(item: dict) -> bool:
    """Return True if this candidate is a known low-signal file.
    Must check file_path — 'source' is always 'pgvector'/'bm25'/'symbol' and
    never contains file names, so the old `source or file_path` logic never fired.
    """
    fp = (item.get("file_path") or "").lower()
    return bool(fp) and any(p in fp for p in _NOISE_PATTERNS)


def rerank(query: str, candidates: list[dict], top_k: int = 6) -> list[dict]:
    """
    Rerank candidate dicts (must have 'text' key) for query.

    Pipeline:
      1. Deduplicate
      2. Pre-filter noise files (LICENSE, lock files, minified assets)
      3. Cap to RERANKER_MAX_CANDIDATES before scoring (default 12)
      4. CrossEncoder.predict with batch_size=RERANKER_BATCH_SIZE (default 8)
      5. Return top_k

    Falls back to RRF if CrossEncoder is unavailable.
    """
    if not candidates:
        return []

    logger.info("Inside rerank method")
    # 1. Deduplicate
    seen, unique = set(), []
    for item in candidates:
        txt = (item.get("text") or "").strip()
        if txt and txt not in seen:
            seen.add(txt)
            unique.append(item)

    # 2. Pre-filter noise — keeps reranker focused on signal
    filtered = [c for c in unique if not _is_noise(c)]
    if not filtered:
        filtered = unique   # don't return empty if everything was "noise"

    # 3. Cap candidates — prevents latency spike on large retrieval sets
    if len(filtered) > _MAX_CANDIDATES:
        filtered = filtered[:_MAX_CANDIDATES]

    if _cross_encoder is None:
        logger.debug("Reranker: CrossEncoder not loaded — using RRF fallback")
        return _rrf(filtered, top_k)

    try:
        pairs  = [(query, item["text"]) for item in filtered]
        logits = _cross_encoder.predict(
            pairs,
            batch_size=_BATCH_SIZE,
            show_progress_bar=False,
        )
        scored = [
            {**item, "score": float(1 / (1 + math.exp(-float(s))))}
            for item, s in zip(filtered, logits)
        ]
        scored.sort(key=lambda x: x["score"], reverse=True)
        logger.debug(
            f"Reranker ({_MODEL_NAME}): {len(unique)}→filter {len(filtered)}→top {top_k}, "
            f"best={scored[0]['score']:.4f}"
        )
        return scored[:top_k]
    except Exception as exc:
        logger.warning(f"Reranker predict failed ({exc}), RRF fallback")
        return _rrf(filtered, top_k)


def reranker_info() -> dict:
    """Return current reranker status — used by /health endpoint."""
    return {
        "model":     _MODEL_NAME,
        "loaded":    _cross_encoder is not None,
        "fallback":  _cross_encoder is None,
    }


def _rrf(candidates: list[dict], top_k: int) -> list[dict]:
    """Reciprocal Rank Fusion — pure Python, no ML."""
    from collections import defaultdict
    by_source: dict = defaultdict(list)
    for item in candidates:
        by_source[item.get("source", "x")].append(item)

    K       = 60
    rrf_map: dict[str, float] = {}
    item_map: dict[str, dict] = {}

    for src, items in by_source.items():
        ranked = sorted(items, key=lambda x: float(x.get("score", 0)), reverse=True)
        for rank, item in enumerate(ranked, 1):
            t = item["text"]
            rrf_map[t]  = rrf_map.get(t, 0.0) + 1.0 / (K + rank)
            item_map[t] = item

    return [
        {**item_map[t], "score": s}
        for t, s in sorted(rrf_map.items(), key=lambda x: x[1], reverse=True)
    ][:top_k]
