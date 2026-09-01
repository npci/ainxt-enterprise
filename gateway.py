# SPDX-License-Identifier: Apache-2.0
# ============================================================
# DISABLE TELEMETRY
# ============================================================

import os

# Load .env file before anything else (does not override existing shell env vars)
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=True)
except ImportError:
    pass

os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["CHROMA_TELEMETRY"] = "False"
os.environ["POSTHOG_DISABLED"] = "1"
os.environ["LLAMA_INDEX_TELEMETRY"] = "False"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"


# ============================================================
# CKMS — decrypt every protected env var BEFORE any module
# that calls os.getenv() (core.config, db.database, auth.*, etc.)
# is imported. Fail-fast on any HSM / DB / crypto error.
# ============================================================
from core.ckms import load_at_boot as _ckms_load_at_boot
_ckms_load_at_boot()
# Internal key-delivery endpoint for the LLM Proxy (web02).
# Mounted immediately after CKMS boot so os.environ already holds
# decrypted values when the endpoint is first called.
from routers.internal_ckms_router import router as _internal_ckms_router


# ============================================================
# IMPORTS
# ============================================================

import uuid
import json
import time
import re
import hashlib

# Module-level document-signal regexes (compiled once at import time for better perf)
_DOC_SLASH_RE = re.compile(
    r"^/(?:pdf|docx?|word|pptx?|pptagent|xlsx?|excel|csv|txt|text|md|convert)\b",
    re.IGNORECASE,
)
_DOC_FORMAT_NOUN_RE = re.compile(
    r"\b(?:pdfs?|docx?s?|word\s+doc(?:ument)?s?|documents?|pptx?s?|powerpoints?|presentations?|"
    r"xlsx?s?|excels?|spreadsheets?|csvs?|markdowns?|\.md)\b",
    re.IGNORECASE,
)
# Alias for CIL-side checks (reuse same compiled patterns)
_CIL_DOC_SLASH_RE = _DOC_SLASH_RE
_CIL_DOC_FORMAT_NOUN_RE = _DOC_FORMAT_NOUN_RE

# Trivial queries (greetings, small-talk, simple arithmetic) that can never
# benefit from KB retrieval.  Everything else gets a KB probe; the score
# threshold (0.35) filters out low-relevance results automatically.
_TRIVIAL_QUERY_RE = re.compile(
    r"^(hi+|hello+|hey+|thanks?|thank\s+you|bye+|good\s+(morning|afternoon|evening|night)|"
    r"what\s+is\s+\d+\s*[\+\-\*\/]\s*\d+|how\s+are\s+you|"
    r"who\s+are\s+you|what\s+(can|do)\s+you\s+do|okay|ok|sure|cool|got\s+it)\??\.?\s*$",
    re.IGNORECASE,
)

class _SkipKBProbe(Exception):
    """Internal sentinel — raise inside the fast-path KB probe to short-circuit
    the block when rag_mode='off' or the query is trivial. The probe's
    existing except chain converts it to a no-op skip."""
    pass

from datetime import datetime
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

from dataclasses import asdict
from typing import List, Optional, Union, Any

from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request
from auth.dependencies import get_current_user as _require_auth, get_current_user
from auth.rbac import require_role
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from middleware.budget_middleware import BudgetMiddleware
from middleware.client_source_middleware import ClientSourceMiddleware
from middleware.rate_limit_middleware import RateLimitMiddleware
from middleware.request_id_middleware import RequestIdMiddleware

from core.config import REDIS_HOST, REDIS_PORT, RDB_CACHE, RDB_BUDGET
from core.config import ENABLE_PLATFORM_KILLSWITCH_API as _ENABLE_PLATFORM_KILLSWITCH_API
from core.kv import get_kv
from core.logger import (
    logger,
    set_request_id,
    set_chat_context,
    set_span_id,
    clear_chat_context,
    bind_context,
    set_correlation_id,
    clear_bound_context,
)
from core.trace_store import add_trace, get_trace
from core.security_validation import validate_agent_request, validate_workflow_request
from core.kafka_producer import (
    produce as _kafka_produce,
    TOPIC_CHAT_HISTORY as _TOPIC_CHAT_HISTORY,
    TOPIC_METRICS as _TOPIC_METRICS,
    TOPIC_BUDGET_EVENTS as _TOPIC_BUDGET_EVENTS,
)
from core.generation_registry import (
    register   as _gen_register,
    deregister as _gen_deregister,
    should_stop as _gen_should_stop,
)

from metrics import metrics

from models.query_rewriter import rewrite_query
from models.classifier import classify_query_complexity

from agents.orchestrator import OrchestratorAgent
from memory.postgres_memory import PostgresMemory
from core.telemetry import telemetry_metrics, tracer, span_store

agent = OrchestratorAgent()
_postgres_memory = PostgresMemory()

from core.model_registry import (
    OPENAI_SIMPLE_MODEL as _OPENAI_SIMPLE,
    OPENAI_CODING_MODEL as _OPENAI_CODING,
    OPENAI_LATEST_MODEL as _OPENAI_LATEST,
    OPENAI_DEEP_RESEARCH_MINI as _DR_MINI,
    OPENAI_DEEP_RESEARCH as _DR_FULL,
    CLAUDE_PRIMARY_MODEL as _CLAUDE_PRIMARY,
    CLAUDE_HAIKU as _CLAUDE_HAIKU,
    CLAUDE_OPUS_MODEL as _CLAUDE_OPUS,
    CLAUDE_OPUS_48_MODEL as _CLAUDE_OPUS_48,
    CLAUDE_OPUS_5_MODEL as _CLAUDE_OPUS_5,
    CLAUDE_SONNET_5_MODEL as _CLAUDE_SONNET_5,
    ENABLE_OPUS as _ENABLE_OPUS,
    ENABLE_SONNET_5 as _ENABLE_SONNET_5,
    ENABLE_CHAT_OPUS as _ENABLE_CHAT_OPUS,
    ENABLE_CLI_OPUS_48 as _ENABLE_CLI_OPUS_48,
    ENABLE_CLI_OPUS_5 as _ENABLE_CLI_OPUS_5,
    ENABLE_RAW_OPENAI_API as _ENABLE_RAW_OPENAI_API,
    GEMINI_VISION_MODEL as _GEMINI_VISION,
    GEMINI_TEXT_MODEL as _GEMINI_TEXT,
    GEMINI_CODING_LITE_MODEL as _GEMINI_CODING_LITE,
    GEMINI_IMAGE_MODEL as _GEMINI_IMAGE,
    MODEL_COST_PER_1M as _MODEL_COST_PER_1M,
    CLAUDE_PRIMARY_DISPLAY as _CLAUDE_PRIMARY_DISPLAY,
    CLAUDE_HAIKU_DISPLAY as _CLAUDE_HAIKU_DISPLAY,
    CLAUDE_OPUS_DISPLAY as _CLAUDE_OPUS_DISPLAY,
    CLAUDE_OPUS_48_DISPLAY as _CLAUDE_OPUS_48_DISPLAY,
    CLAUDE_OPUS_5_DISPLAY as _CLAUDE_OPUS_5_DISPLAY,
    CLAUDE_SONNET_5_DISPLAY as _CLAUDE_SONNET_5_DISPLAY,
    OPENAI_CODING_DISPLAY as _OPENAI_CODING_DISPLAY,
    OPENAI_SIMPLE_DISPLAY as _OPENAI_SIMPLE_DISPLAY,
    OPENAI_LATEST_DISPLAY as _OPENAI_LATEST_DISPLAY,
    OPENAI_TERA_MODEL as _OPENAI_TERA,
    OPENAI_LUNA_MODEL as _OPENAI_LUNA,
    OPENAI_TERA_DISPLAY as _OPENAI_TERA_DISPLAY,
    OPENAI_LUNA_DISPLAY as _OPENAI_LUNA_DISPLAY,
    ENABLE_GPT56_TERA as _ENABLE_GPT56_TERA,
    ENABLE_GPT56_LUNA as _ENABLE_GPT56_LUNA,
    GEMINI_DISPLAY as _GEMINI_DISPLAY,
    GEMINI_TEXT_DISPLAY as _GEMINI_TEXT_DISPLAY,
    GEMINI_CODING_LITE_DISPLAY as _GEMINI_CODING_LITE_DISPLAY,
    GEMINI_IMAGE_DISPLAY as _GEMINI_IMAGE_DISPLAY,
    OPENAI_CODING_MODEL,
)

def _write_request_audit(
    *,
    request_id: str,
    user_id: str,
    email: str,
    department: str,
    client_source: str,
    endpoint: str,
    question: str,
    model_used: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    latency_ms: int,
    cache_hit: str,
    compliance_blocked: bool,
    error: str = "",
) -> None:
    """Fire-and-forget: write one row to request_audit_log."""
    import hashlib
    import threading
    from db.database import SessionLocal
    from db.models import RequestAuditLog

    def _write():
        db = SessionLocal()
        try:
            q_hash = hashlib.sha256(question.encode()).hexdigest() if question else None
            row = RequestAuditLog(
                request_id=request_id,
                user_id=user_id,
                email=email,
                department=department,
                client_source=client_source,
                endpoint=endpoint,
                question_hash=q_hash,
                model_used=model_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                cache_hit=cache_hit or "none",
                compliance_blocked=compliance_blocked,
                error=error or None,
            )
            db.add(row)
            db.commit()
        except Exception:
            pass  # audit failure must never affect the user response
        finally:
            db.close()

    threading.Thread(target=_write, daemon=True).start()


# ── Passthrough compliance scan ledger ────────────────────────────
# History messages that ALREADY passed the compliance block-gate in a prior
# turn. Immutable history need not be re-scanned, bounding browser-agent ML
# calls to O(N) per session instead of O(N²).
#
# The ledger VALUE is the exact redacted text produced by validate_input() when
# the message first passed. This matters for tool results: validate_input()
# redacts BOTH regex-detected AND ML-detected values, whereas mask_pii() is
# regex-only. Reusing the stored redacted_text keeps the upstream payload
# byte-identical to a fresh scan (no ML-only value, e.g. an ML-detected
# ACCOUNT_NUMBER, silently leaks in the clear on turn 2+).
#
# The key is salted with the user id so one user's passed content can never
# cause another user's byte-identical message to be skipped.
#
# Bounded LRU; per-process (per Gunicorn worker), which is fine — a single
# agent session's turns stack on the same worker/connection pool.
from collections import OrderedDict as _PTOrderedDict

_PT_SCAN_LEDGER_MAX = int(os.getenv("PASSTHRU_SCAN_LEDGER_SIZE", "4096"))
_PT_SCAN_LEDGER_ENABLED = os.getenv("PASSTHRU_SCAN_LEDGER", "true").lower() == "true"
# F3: force scan flags on for the passthrough lane only (config-drift hardening).
_PT_ENFORCE_SCAN = os.getenv("PASSTHRU_ENFORCE_SCAN", "true").lower() == "true"
# key -> redacted_text of the message that passed the block-gate.
_pt_scan_ledger: "_PTOrderedDict[str, str]" = _PTOrderedDict()
_pt_scan_ledger_lock = threading.Lock()


def _pt_ledger_key(user_id: str, role: str, text: str) -> str:
    return hashlib.sha256(
        f"{user_id or ''}\x00{role}\x00{text}".encode("utf-8", "ignore")
    ).hexdigest()


def _pt_ledger_get(key: str):
    """Return the stored redacted_text for an already-passed message, else None."""
    with _pt_scan_ledger_lock:
        val = _pt_scan_ledger.get(key)
        if val is not None:
            _pt_scan_ledger.move_to_end(key)
        return val


def _pt_ledger_mark(key: str, redacted_text: str) -> None:
    with _pt_scan_ledger_lock:
        _pt_scan_ledger[key] = redacted_text
        _pt_scan_ledger.move_to_end(key)
        while len(_pt_scan_ledger) > _PT_SCAN_LEDGER_MAX:
            _pt_scan_ledger.popitem(last=False)


def _pt_scan_tool_result(user_id, raw, is_current, validate_input, mask_fn):
    """Decide the safe (redacted) content for a passthrough tool result and
    manage the scan ledger. Extracted from _build_passthrough_messages so the
    compliance-critical invariants can be unit-tested against the shipped code.

    Args:
        user_id:        salts the ledger key (guaranteed non-null on this path).
        raw:            the raw tool-result text.
        is_current:     True if this is the current turn (always scanned fresh).
        validate_input: callable(raw) -> dict with "findings" and "redacted_text"
                        (compliance_engine.validate_input in production).
        mask_fn:        regex-only fallback redactor (mask_pii in production).

    Returns:
        (safe_content, blocked_types)

    INVARIANTS (guarded by tests):
      * The current turn is ALWAYS scanned fresh (never served from the ledger).
      * A result with block findings is NEVER added to the ledger, so it is
        re-scanned and re-blocks on every subsequent turn (no silent un-block).
      * The skip path reuses the EXACT stored redacted_text (not mask_fn(raw)),
        so an ML-only-detected value cannot leak in the clear from turn 2 on.
    """
    key = _pt_ledger_key(user_id, "tool", raw)
    cached = None if (not _PT_SCAN_LEDGER_ENABLED or is_current) else _pt_ledger_get(key)
    if cached is not None:
        return cached, []

    chk = validate_input(raw)
    btypes = [f["type"] for f in chk.get("findings", []) if f.get("blocked")]
    safe_content = chk.get("redacted_text") or mask_fn(raw)
    if btypes:
        # blocked → never laddered, so it always re-blocks on later turns
        return safe_content, btypes
    _pt_ledger_mark(key, safe_content)
    return safe_content, []


def _resolve_model_id(model: str) -> str:
    """Resolve a model value to a concrete model ID for model_usages logging.

    When the model router hasn't been called (cache hits, early exits, paths
    that bypass routing), _meta["model"] stays as the initial "auto" sentinel.
    "auto" is not a real model ID — replace it with the platform default so
    every model_usages row carries a queryable, meaningful model ID.

    Also strips display-label prefixes left over from last_model_label values
    that slipped through (e.g. "GPT-5.4 (gpt-5.4)" → "gpt-5.4").
    """
    if not model or model.strip().lower() in ("auto", "default", ""):
        from core.model_registry import CLAUDE_PRIMARY_MODEL, LOCAL_LLM_MODEL_NAME
        # Return whichever is configured — prefer cloud primary, fall back to
        # local display name. If neither is set, return "unknown" so audit rows
        # are queryable rather than carrying an empty string.
        return CLAUDE_PRIMARY_MODEL or LOCAL_LLM_MODEL_NAME or "unknown"
    _stripped = model.strip()
    # A BARE display constant carries no embedded model ID — the parentheses are
    # part of the NAME. model_router._tier_label() returns LOCAL_LLM_DISPLAY
    # unadorned whenever the local catalog is empty (no model discovered), and
    # the wrapper regex below then read "Local (In-house)" as the ID "In-house",
    # inventing a model that exists nowhere and writing it to the chat footer and
    # to model_usages.model. Recognise those names first and pass them through.
    # Labels that DO carry an ID ("Local (In-house) (llama-guard3:1b)") are not
    # matched here and still resolve via the regex, since it anchors on the LAST
    # parenthesised group.
    try:
        from core.model_registry import LOCAL_LLM_DISPLAY as _LOCAL_DISPLAY
        if _stripped == (_LOCAL_DISPLAY or "").strip():
            return _stripped
    except Exception:
        pass
    # Strip display-label wrapper: "Display Name (model-id)" → "model-id"
    import re as _re
    m = _re.search(r'\(([^)]+)\)\s*(?:\[[^\]]*\])?\s*$', model)
    if m:
        return m.group(1).strip()
    return model


def _estimate_cost(model: str, input_tok: int, output_tok: int) -> float:
    """Return the estimated USD cost for a single LLM call.

    Local/in-house models are always free ($0.00).  Two checks are applied so
    that bare model names (e.g. "Kimi-k2.5", "kimi-k2.7-code", "glm-5.2")
    that don't carry a "local:" prefix are still recognised as in-house:

      1. Fast string heuristic — "local" anywhere in the model string.
      2. Dynamic catalog lookup via gateway_local_llm.is_local_model(), which
         consults the live /v1/models catalog cached by the local-LLM gateway.
         This is the authoritative check for bare model IDs served in-house.
    """
    _m = (model or "").lower()
    # 1. Fast heuristic: "local" in the model string covers "local:Kimi-k2.5",
    #    "local-llm", display labels like "Local (Kimi-k2.5)", etc.
    if "local" in _m:
        return 0.0
    # 2. Dynamic catalog check: catches bare model IDs (e.g. "Kimi-k2.5",
    #    "kimi-k2.7-code", "glm-5.2") that are served by the in-house LLM
    #    proxy but whose names don't contain the word "local".
    try:
        from gateway_local_llm import is_local_model as _is_local_model
        if _is_local_model(model):
            return 0.0
    except Exception:
        pass  # fail-open: fall through to cost table lookup
    # MODEL_COST_PER_1M is keyed by raw model IDs (e.g. "gpt-5.4", "claude-sonnet-4-6").
    # `model` may be a display label like "GPT-5.4 (Coding) (gpt-5.4)" when it comes
    # from last_model_label — try direct lookup first, then scan for a matching ID substring.
    rates = _MODEL_COST_PER_1M.get(model)
    if rates is None:
        for _mid, _r in _MODEL_COST_PER_1M.items():
            if _mid.lower() in _m:
                rates = _r
                break
    if rates is None:
        rates = (2.00, 8.00)  # conservative default (gpt-5.4 rate)
    return (input_tok * rates[0] + output_tok * rates[1]) / 1_000_000


# Phase 2 (context management): per-model working context window, so the
# client can render a live "% used" meter. Keys are matched as case-insensitive
# substrings against the model hint / label; first match wins. Falls back to a
# conservative 128 K when the model is unknown or auto-routed.
#
# These built-in dicts are the fallback. At startup the loader below tries to
# read config/model_context_windows.json (or MODEL_CONTEXT_CONFIG env var path)
# so operators can add new model families without code changes.
_DEFAULT_CONTEXT_WINDOWS = {
    "claude":  200_000,   # Claude Sonnet/Opus 4.x
    "sonnet":  200_000,
    "opus":    200_000,
    "haiku":   200_000,
    "gpt-5":   256_000,   # GPT-5.x family
    "gpt-4":   128_000,
    "gpt":     128_000,
    "gemini":  1_000_000, # Gemini 1.5/2.x long-context
    # ── In-house / local vLLM models ─────────────────────────────────────────
    "kimi":    262_144,   # kimi-k2.7-code vLLM deployment (256 K)
    "glm":     131_072,   # GLM-4 / GLM-5 family (128 K)
    "qwen":    131_072,   # Qwen-2.x / Qwen-3.x family (128 K)
    "deepseek": 65_536,   # DeepSeek-Coder / DeepSeek-V2 (64 K)
    "llama":   131_072,   # Llama-3.x family (128 K)
    "gemma":   131_072,   # Gemma-2 / Gemma-3 family (128 K)
    "mistral": 131_072,   # Mistral / Mixtral family (128 K)
    "gpt-oss": 131_072,   # in-house gpt-oss-120b (128 K)
    "local":   128_000,   # generic fallback for any unrecognised local model
}

# Phase C1 (Tier 5 — output reservation): built-in fallback dict.
_DEFAULT_RESERVED_OUTPUT = {
    "claude":  8_000,
    "sonnet":  8_000,
    "opus":    8_000,
    "haiku":   4_000,
    "gpt-5":   16_000,
    "gpt-4":   4_000,
    "gpt":     4_000,
    "gemini":  8_000,
    # ── In-house / local vLLM models ─────────────────────────────────────────
    "kimi":    8_000,   # kimi-k2.7-code — larger reserve for long agentic outputs
    "glm":     4_000,   # GLM-4 / GLM-5 family
    "qwen":    4_000,   # Qwen-2.x / Qwen-3.x family
    "deepseek": 4_000,  # DeepSeek family
    "llama":   4_000,   # Llama-3.x family
    "gemma":   4_000,   # Gemma family
    "mistral": 4_000,   # Mistral / Mixtral family
    "gpt-oss": 4_000,   # in-house gpt-oss-120b
    "local":   2_000,   # generic fallback
}


def _load_context_config() -> "tuple[dict, dict]":
    """Load context window and reserved-output tables from a JSON config file.

    Tries MODEL_CONTEXT_CONFIG env var first, then the bundled
    config/model_context_windows.json.  Falls back silently to the built-in
    Python dicts above when the file is absent or malformed — existing
    deployments are completely unaffected.
    """
    import json as _json
    import pathlib as _pathlib
    _default_path = str(_pathlib.Path(__file__).parent / "config" / "model_context_windows.json")
    _config_path  = os.getenv("MODEL_CONTEXT_CONFIG", _default_path)
    try:
        with open(_config_path, encoding="utf-8") as _f:
            _data = _json.load(_f)
        _cw = {str(k): int(v) for k, v in _data.get("context_windows", {}).items()}
        _ro = {str(k): int(v) for k, v in _data.get("reserved_output",  {}).items()}
        if _cw and _ro:
            logger.info(
                "gateway: loaded model context config from %r "
                "(%d context-window entries, %d reserved-output entries)",
                _config_path, len(_cw), len(_ro),
            )
            return _cw, _ro
    except FileNotFoundError:
        pass   # bundled file missing — use built-in defaults (normal for bare checkouts)
    except Exception as _ctx_err:
        logger.warning(
            "gateway: failed to load MODEL_CONTEXT_CONFIG from %r: %s — "
            "using built-in defaults (no behaviour change)",
            _config_path, _ctx_err,
        )
    return _DEFAULT_CONTEXT_WINDOWS, _DEFAULT_RESERVED_OUTPUT


_MODEL_CONTEXT_WINDOW, _MODEL_RESERVED_OUTPUT = _load_context_config()


def _context_window_for(model_hint: Optional[str]) -> int:
 """Return the working context window (tokens) for a model hint/label."""
 _m = (model_hint or "").lower().strip()
 if _m:
     for _key, _win in _MODEL_CONTEXT_WINDOW.items():
         if _key in _m:
             return _win
 return 128_000  # conservative default for auto / unknown routing


def _reserved_output_for(model_hint: Optional[str]) -> int:
 """Tokens reserved for the model's answer (Tier 5). Conservative default."""
 _m = (model_hint or "").lower().strip()
 if _m:
     for _key, _res in _MODEL_RESERVED_OUTPUT.items():
         if _key in _m:
             return _res
 return 2_000


# Phase C1 (Tier 2 — model-aware usable budget). Fraction of the window we fill
# before compacting; 0.75 stays under the ~70–80% "lost in the middle" cliff
# while staying completeness-first. Env-overridable for A/B.
try:
 _CONTEXT_USABLE_FRACTION = float(os.getenv("CHAT_CONTEXT_USABLE_FRACTION", "0.75"))
except (TypeError, ValueError):
 _CONTEXT_USABLE_FRACTION = 0.75


def _usable_history_budget(model_hint: Optional[str], flat_floor: int,
                           fraction: Optional[float] = None) -> int:
 """Compaction trigger (tokens) for the assembled history.

 Tier 2 = window*fraction - reserved_output. CRITICAL safety floor (proven in
 tests/context_benchmark C0): never trigger compaction EARLIER than today's
 flat behaviour, or small-window models regress into a lossy summary sooner
 than before. So the budget may only RAISE the ceiling for large-window
 models, never lower it below the historical flat trigger.

 `fraction` lets a resolved DomainProfile (PIPELINE_V2) override the module
 default; when None (the default/off path) it uses _CONTEXT_USABLE_FRACTION —
 byte-identical to historical behaviour. The flat_floor still protects against
 early compaction regardless of fraction.
 """
 _frac = _CONTEXT_USABLE_FRACTION if fraction is None else fraction
 _window = _context_window_for(model_hint)
 _reserved = _reserved_output_for(model_hint)
 _model_usable = int(_window * _frac) - _reserved
 return max(flat_floor, _model_usable)


def _compute_eval_score(question: str, answer: str, chunks: list) -> dict:
    """
    Compute per-response quality scores.
    - grounding:    fraction of answer sentences that cite content from retrieved chunks
    - completeness: heuristic — answer length relative to question length
    """
    has_context = bool(chunks)
    chunk_count = len(chunks)

    # Grounding: how many answer sentences can be traced back to a chunk.
    # Bug-fix: replaced the brittle exact-prefix substring match (which scored
    # paraphrased-but-correct answers as 0.0) with a keyword-overlap heuristic.
    # For each answer sentence we extract meaningful tokens (≥4 chars, alpha-only)
    # and check whether at least 30 % of them appear anywhere in any chunk.
    # This correctly handles paraphrasing while still catching hallucinated content.
    grounding = 0.0
    if chunks and answer:
        import re as _re
        sentences = [s.strip() for s in answer.split(".") if len(s.strip()) > 20]
        if sentences:
            # Pre-build a single lowercased string of all chunk content for fast lookup.
            chunk_text = " ".join(
                (c.get("content") or c if isinstance(c, str) else "").lower()
                for c in chunks
            )
            grounded = 0
            for s in sentences:
                # Extract meaningful tokens from the sentence (alpha, ≥4 chars).
                tokens = [t for t in _re.findall(r"[a-zA-Z]{4,}", s.lower())]
                if not tokens:
                    continue
                # A sentence is grounded if ≥30 % of its tokens appear in chunks.
                hits = sum(1 for t in tokens if t in chunk_text)
                if hits / len(tokens) >= 0.30:
                    grounded += 1
            grounding = round(grounded / len(sentences), 3)

    # Completeness: answer length vs expected minimum (question_len * 8 chars)
    completeness = round(
        min(len(answer) / max(len(question) * 8, 1), 1.0), 3
    )

    result = {
        "grounding":    grounding,
        "completeness": completeness,
        "chunk_count":  chunk_count,
        "has_context":  has_context,
    }

    # ── Phase 3 grounding DRIVING (PIPELINE_V2_GROUNDING, default OFF) ────────
    # Advisory upgrade over the substring `grounding` heuristic above: run the
    # per-claim verifier (decompose→align→NLI→label) and attach a calibrated
    # `grounding_confidence` + `unsupported_claims`. Never raises, never blocks
    # — purely additive telemetry. Off by default → result is byte-identical.
    if globals().get("_PIPELINE_V2_GROUNDING") and chunks and answer:
        try:
            from grounding.evidence import Chunk as _GChunk
            from grounding.verifier import verify as _gverify
            from grounding.nli_local import make_local_nli as _make_nli
            _ev = [
                _GChunk.from_dict(c) if isinstance(c, dict)
                else _GChunk(id=str(i), text=(c if isinstance(c, str) else str(c)))
                for i, c in enumerate(chunks)
            ]
            # Thread the real LOCAL-ONLY NLI invoker (Phase 3). It classifies each
            # claim with the in-house local model and NEVER egresses to a cloud
            # provider; if the local model is unavailable it returns "" and
            # make_local_nli degrades to the deterministic keyword heuristic. On
            # any failure building the invoker we pass model_call=None (pure
            # keyword fallback) so this stays byte-safe.
            try:
                from grounding.nli_invoker import make_local_model_call as _mk_call
                _nli_model_call = _mk_call()
            except Exception:  # noqa: BLE001 — advisory only
                _nli_model_call = None
            # Gap #3: LLM-decompose the answer into atomic claims (LOCAL-ONLY).
            # Falls back to verify()'s deterministic sentence splitter when the
            # local model is unavailable or the call fails (claims stays None).
            _llm_claims = None
            try:
                from grounding.nli_invoker import decompose_claims_llm as _dc_llm
                _llm_claims = _dc_llm(answer)
            except Exception:  # noqa: BLE001 — advisory only
                _llm_claims = None
            _report = _gverify(answer, _ev, nli=_make_nli(model_call=_nli_model_call),
                               claims=_llm_claims)
            result["grounding_confidence"] = _report.grounding_confidence
            result["unsupported_claims"] = len(_report.unsupported)
            result["contradicted_claims"] = len(_report.contradicted)
            # Hedge surfacing (Phase 14 G4, non-blocking form): when grounding
            # confidence is low, expose an advisory hedge + the unsupported claim
            # texts. This is additive telemetry consumed by the __meta__ line — it
            # NEVER blocks, mutates, or delays the streamed answer.
            try:
                _gc = float(_report.grounding_confidence)
                if _gc < 0.5 or _report.unsupported or _report.contradicted:
                    # GroundingReport.unsupported / .contradicted are List[str]
                    # (the claim texts themselves — see grounding/verifier.py).
                    result["grounding_hedge"] = True
                    result["unsupported_claim_texts"] = (
                        list(_report.unsupported) + list(_report.contradicted)
                    )[:5]
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 — advisory only
            pass

    return result


def _preflush_grounding_hedge(answer: str, chunks: list) -> tuple:
    """Gap #3 pre-flush gate: synchronously verify `answer` against `chunks` and
    return (hedge_text, contributing_doc_ids).

    hedge_text — a short notice to prepend BEFORE the answer when grounding
    confidence is low, else "". Reuses the same local-only grounding machinery
    as the post-hoc path. Never raises — returns ("", []) on any failure so
    the answer is emitted unchanged. Only called when GROUNDING_PREFLUSH_GATE
    is on; otherwise grounding stays the post-hoc advisory path.

    contributing_doc_ids — the set of `doc_id`s (as a list, insertion order,
    no duplicates) whose evidence actually supported at least one claim in
    the final answer. `chunks` entries must carry a real "doc_id" key (not a
    throwaway index) for this to be meaningful — see the call site, which
    builds `chunks` from `_fp_sources_meta` and now passes doc_id through as
    the Chunk id specifically so this attribution is possible. Used by the
    caller to narrow the multi-doc "Sources" panel down to only the
    document(s) genuinely used, instead of every document the user selected
    from the DocPickerCard. Empty list on any failure/no-claims/no-evidence
    case — the caller's existing fallback (show every selected doc) applies
    exactly as it did before this fix whenever this comes back empty.
    """
    try:
        if not (answer and chunks):
            return "", []
        from grounding.evidence import Chunk as _GChunk
        from grounding.verifier import verify as _gverify, SUPPORTED as _GSUPPORTED
        from grounding.nli_local import make_local_nli as _make_nli
        # id = the chunk's real doc_id when the caller provided one (see
        # call site) — falls back to the array index only for chunks that
        # genuinely have no doc_id (never happens for KB sources, but keeps
        # this function safe for any other future caller).
        _ev = [
            _GChunk.from_dict(c) if isinstance(c, dict)
            else _GChunk(id=str(i), text=(c if isinstance(c, str) else str(c)))
            for i, c in enumerate(chunks)
        ]
        try:
            from grounding.nli_invoker import (
                make_local_model_call as _mk_call,
                decompose_claims_llm as _dc_llm,
            )
            _mc = _mk_call()
            _claims = _dc_llm(answer)
        except Exception:  # noqa: BLE001
            _mc, _claims = None, None
        _rep = _gverify(answer, _ev, nli=_make_nli(model_call=_mc), claims=_claims)
        _gc = float(_rep.grounding_confidence)

        # ── Doc-level attribution ──────────────────────────────────────
        # For every claim the verifier marked SUPPORTED, evidence_id holds
        # the id of the Chunk that supported it — which is now the real
        # doc_id (see _ev construction above). Collect the distinct doc_ids
        # that backed at least one claim; this is what the caller uses to
        # narrow the Sources panel. Order preserved (first-seen), duplicates
        # removed — a doc supporting 5 claims only appears once.
        _contributing_doc_ids: list = []
        _seen_docs: set = set()
        for _verdict in (_rep.verdicts or []):
            if _verdict.label == _GSUPPORTED and _verdict.evidence_id:
                _did = str(_verdict.evidence_id)
                if _did not in _seen_docs:
                    _seen_docs.add(_did)
                    _contributing_doc_ids.append(_did)

        if _rep.contradicted:
            return (
                "⚠️ Note: parts of this answer could not be verified against the "
                "retrieved sources and may be inaccurate.\n\n",
                _contributing_doc_ids,
            )
        if _gc < 0.5 or _rep.unsupported:
            return (
                "ℹ️ Note: some statements below could not be fully confirmed from "
                "the available sources.\n\n",
                _contributing_doc_ids,
            )
        return "", _contributing_doc_ids
    except Exception:  # noqa: BLE001 — gate must never break the answer
        return "", []

# ── Adaptive LLM semaphore ────────────────────────────────────────────────────
# Tuned for 1000 concurrent chat users on a mixed local-LLM + OpenAI/Claude setup.
#
# Reasoning:
#   - Local LLM requests are fast (< 2 s) and unlimited, so they spend very little
#     time holding the semaphore. Under 1000 users ~60-70 % are expected to be
#     routed locally → effective external API pressure is ~300-400 requests at peak.
#   - OpenAI / Claude Tier-2 accounts support ~500 RPM combined. With avg latency
#     of 4-6 s, steady-state concurrency = 500 * 5 / 60 ≈ 41 slots. Headroom × 5
#     = 200 slots keeps the queue short without hammering rate limits.
#   - _SEM_INITIAL = 500: allows the burst to be absorbed immediately on startup;
#     the adaptive monitor will tighten it within 30 s if latency climbs.
#   - _SEM_MIN = 50: never drop below 50 even under extreme load — prevents a
#     latency spike from completely stalling the service.
#   - _SEM_MAX = 1000: hard ceiling; 1 slot per concurrent user in the worst case.
#   - acquire timeout = 60 s: gives queued requests two full LLM round-trips worth
#     of wait time before giving up, drastically reducing false "busy" rejections
#     during short bursts.
#   - Adaptive thresholds relaxed: shrink only if p95 > 15 s (genuine overload),
#     grow if p95 < 5 s (plenty of headroom). Keeps the cap stable during normal
#     mixed-load operation.
#   - Latency window widened to 500 samples for a more stable p95 signal.

_SEM_MIN     = 50       # floor — never starve the service
_SEM_MAX     = 1000     # ceiling — 1 slot per concurrent user
_SEM_INITIAL = 500      # startup cap; adaptive monitor adjusts from here
_SEM_ACQUIRE_TIMEOUT = 120  # seconds a request waits before "busy" is returned

_SEM_CAP = _SEM_INITIAL          # current cap (mutable)
_LLM_SEMAPHORE = threading.Semaphore(_SEM_INITIAL)

# Rolling latency samples (last 500 requests) for a stable p95 signal
_latency_samples: list = []
_latency_lock = threading.Lock()

def _record_latency(ms: float):
    """Record a completed /ask latency sample."""
    global _latency_samples
    with _latency_lock:
        _latency_samples.append(ms)
        if len(_latency_samples) > 500:
            _latency_samples = _latency_samples[-500:]

def _p95_latency() -> float:
    """Return p95 of recent latency samples, or 0 if insufficient data."""
    with _latency_lock:
        if len(_latency_samples) < 20:
            return 0.0
        sorted_s = sorted(_latency_samples)
        idx = int(len(sorted_s) * 0.95)
        return sorted_s[min(idx, len(sorted_s) - 1)]

def _adaptive_semaphore_monitor():
    """Background thread: adjust _SEM_CAP every 30 s based on p95 latency."""
    global _SEM_CAP, _LLM_SEMAPHORE
    import time
    while True:
        time.sleep(30)
        try:
            p95 = _p95_latency()
            if p95 == 0:
                continue
            old_cap = _SEM_CAP
            if p95 > 15000 and _SEM_CAP > _SEM_MIN:    # genuine overload → shrink 15 %
                _SEM_CAP = max(_SEM_MIN, int(_SEM_CAP * 0.85))
            elif p95 < 5000 and _SEM_CAP < _SEM_MAX:   # healthy headroom → grow 10 %
                _SEM_CAP = min(_SEM_MAX, int(_SEM_CAP * 1.10))
            if _SEM_CAP != old_cap:
                # Replace semaphore with new cap (atomic enough for our use)
                _LLM_SEMAPHORE = threading.Semaphore(_SEM_CAP)
                logger.info(
                    f"AdaptiveSemaphore: cap {old_cap} → {_SEM_CAP} "
                    f"(p95={p95:.0f}ms)"
                )
        except Exception as _e:
            logger.debug(f"AdaptiveSemaphore monitor error: {_e}")

# Start the adaptive monitor as a daemon thread
threading.Thread(target=_adaptive_semaphore_monitor, daemon=True, name="sem-monitor").start()

# ============================================================
# KV INIT
# Backends selected per-DB via REDIS_CLIENT_CONFIG_DB{0,4}.
# ============================================================

redis_client = get_kv(RDB_CACHE, decode_responses=True)

CACHE_TTL_SECONDS = 86400

# ── LLM bypass metrics (DB=4 — budget/usage tracking) ──────────────────
# Keys:  ainxt:bypass:{YYYYMMDD}:{source}          → total count
#        ainxt:bypass:{YYYYMMDD}:user:{uid}:{source} → per-user count
#        ainxt:bypass:{YYYYMMDD}:repo:{repo}:{source} → per-repo count
# TTL: 8 days (covers a rolling week of daily buckets)
_bypass_redis = get_kv(RDB_BUDGET, decode_responses=True)
_BYPASS_TTL   = 8 * 86400   # 8 days


def _record_bypass_metric(source: str, user_id: str = "", repo: str = "") -> None:
    """
    Increment Redis counters for LLM bypass rate tracking.
    source: "redis" | "semantic" | "llm"
    Fire-and-forget — never raises.
    """
    try:
        from datetime import datetime as _dt
        _date = _dt.utcnow().strftime("%Y%m%d")
        pipe = _bypass_redis.pipeline()
        # Total per source per day
        _k = f"ainxt:bypass:{_date}:{source}"
        pipe.incr(_k)
        pipe.expire(_k, _BYPASS_TTL)
        # Per user
        if user_id:
            _ku = f"ainxt:bypass:{_date}:user:{user_id}:{source}"
            pipe.incr(_ku)
            pipe.expire(_ku, _BYPASS_TTL)
        # Per repo
        if repo:
            _kr = f"ainxt:bypass:{_date}:repo:{repo}:{source}"
            pipe.incr(_kr)
            pipe.expire(_kr, _BYPASS_TTL)
        pipe.execute()
    except Exception:
        pass


# ============================================================
# BUDGET DISPLAY CACHE (per-process, 60s)
# ------------------------------------------------------------
# Cached-response paths and post-stream "__meta__" emissions need
# the user's daily token/request totals. Hitting Postgres for that
# on every request adds 200-500 ms of pure overhead. The numbers
# only need to be display-accurate; 60s staleness is fine.
# ============================================================
_BUDGET_INFO_TTL_S = 60.0
_budget_info_cache: dict[str, tuple[float, dict]] = {}

def _get_budget_info_cached(user_id: str) -> dict:
    if not user_id:
        return {}
    _now = time.monotonic()
    _hit = _budget_info_cache.get(user_id)
    if _hit and (_now - _hit[0]) < _BUDGET_INFO_TTL_S:
        return _hit[1]
    info: dict = {}
    try:
        from store.budget_store import get_usage_today, get_budget
        _u = get_usage_today(user_id) or {}
        _b = get_budget(user_id) or {}
        info = {
            "tokens_today":   _u.get("tokens_used", 0),
            "requests_today": _u.get("requests_made", 0),
        }
        if _b:
            info["max_tokens_total"]   = _b.get("max_tokens_total", 0)
            info["max_requests_total"] = _b.get("max_requests_total", 0)
            info["cost_today"]         = _u.get("cost_usd_spent", 0.0)
            info["max_cost_total"]     = _b.get("max_cost_usd_total", 0.0)
    except Exception:
        pass
    _budget_info_cache[user_id] = (_now, info)
    return info


# ============================================================
# FASTAPI INIT
# ============================================================

app = FastAPI(
    title="AiNxt Enterprise — AiNxt AI Platform",
    version="4.0",
    description=(
        "## AiNxt Autonomous Agentic Engineering Platform\n\n"
        "Enterprise-grade multi-agent AI platform for internal engineering teams.\n\n"
        "### Features\n"
        "- **RAG Chat** — vector + BM25 + reranked retrieval over indexed codebases\n"
        "- **Agent Builder** — create and run custom AI agents with tools and skills\n"
        "- **Workflow Engine** — DAG-based multi-step automation\n"
        "- **PCI/DSS Compliance** — every request/response scanned\n"
        "- **Model Routing** — local Ollama → OpenAI → Claude based on complexity\n"
        "- **Observability** — Prometheus metrics at `/metrics/prometheus`\n\n"
        "### Authentication\n"
        "Use `POST /auth/login` to obtain a JWT. Pass as `Authorization: Bearer <token>`."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {"name": "auth",         "description": "Authentication — register, login, user profile"},
        {"name": "ai",           "description": "Core AI — RAG chat, traces, metrics"},
        {"name": "agents",       "description": "Agent Builder — create and run agents"},
        {"name": "workflows",    "description": "Workflow Engine — DAG pipelines"},
        {"name": "skills",       "description": "Skills — reusable Python functions"},
        {"name": "projects",     "description": "Projects — scoped AI workspaces"},
        {"name": "inbox",        "description": "Inbox — notifications"},
        {"name": "marketplace",  "description": "MCP Marketplace — tool registry"},
        {"name": "codebase",     "description": "Codebase Index — repo management"},
        {"name": "budget",       "description": "Budget — cost tracking"},
        {"name": "observability","description": "Metrics, traces, health"},
        {"name": "sdlc",        "description": "SDLC Pipeline — AI-driven feature/bug lifecycle"},
        {"name": "vault",        "description": "Credential Vault — Fernet-encrypted secret storage"},
        {"name": "security",     "description": "Security — SSO, credentials, access control"},
        {"name": "jobs",         "description": "Async Job Queue — rq-backed background jobs"},
        {"name": "slack",        "description": "Slack — bidirectional agent interface + HITL"},
        {"name": "teams",        "description": "Microsoft Teams — @AiNxt bot + SDLC HITL via Adaptive Cards"},
        {"name": "memory",       "description": "Episodic Memory — cross-session agent memory"},
        {"name": "compliance",   "description": "Compliance — signed audit reports and export"},
        {"name": "chat",         "description": "Chat — file/document/image upload for multimodal queries"},
        {"name": "governance",   "description": "Governance — DRAFT→APPROVAL→PRODUCTION lifecycle for agents, skills, MCP tools"},
    ],
)

# SEC-12: CORS — read approved origins from env var. No hardcoded fallback:
# an unset CORS_ALLOWED_ORIGINS (and unset CORS_DEFAULT_ORIGINS below) means
# no cross-origin browser requests are allowed — same-origin traffic and
# direct API calls (curl, server-to-server) are unaffected either way. Set
# CORS_ALLOWED_ORIGINS explicitly for any deployment that needs a browser
# frontend on a different origin, e.g.:
#   CORS_ALLOWED_ORIGINS=https://${AINXT_BASE_URL},https://${AINXT_BASE_URL_STAGING}
# Wildcard "*" is NOT used to prevent API key theft via malicious sites.
#
# CORS_DEFAULT_ORIGINS: optional local-dev convenience — set it in your own
# .env to your frontend's origin(s) so you don't have to set
# CORS_ALLOWED_ORIGINS every time. Empty/unset means no fallback origins.
_cors_env = os.getenv("CORS_ALLOWED_ORIGINS", "")
_cors_default_env = os.getenv("CORS_DEFAULT_ORIGINS", "")
_cors_origins: list = (
    [o.strip() for o in _cors_env.split(",") if o.strip()]
    if _cors_env.strip()
    else [o.strip() for o in _cors_default_env.split(",") if o.strip()]
)
# In development mode, optionally allow one extra origin (e.g. "http://localhost"
# to cover all localhost ports at once) via CORS_DEV_EXTRA_ORIGIN. Unset means
# no extra origin is added — no hardcoded fallback.
if os.getenv("APP_ENV", "production").lower() in ("development", "dev", "local"):
    _cors_dev_extra = os.getenv("CORS_DEV_EXTRA_ORIGIN", "").strip()
    if _cors_dev_extra:
        _cors_origins.append(_cors_dev_extra)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,    # safe now that origins are explicit (not wildcard)
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.add_middleware(RequestIdMiddleware)
app.add_middleware(BudgetMiddleware)
app.add_middleware(ClientSourceMiddleware)
# Global sliding-window rate limiter — IP-based (unauthenticated) and user-based
# (authenticated via JWT/API key).  Must be added AFTER ClientSourceMiddleware so
# that the caller-identity resolution in RateLimitMiddleware can read the
# already-populated X-Client-Source header if needed.
app.add_middleware(RateLimitMiddleware)

# ── OpenTelemetry auto-instrumentation ───────────────────────────────────────
# Must be called AFTER app is created and BEFORE the first request.
# Instruments FastAPI (per-request spans), httpx (outbound HTTP spans),
# and psycopg2 (DB query spans) — all linked by the same trace_id.
from core.telemetry import instrument_app as _instrument_app
_instrument_app(app)

# ── No-cache middleware ────────────────────────────────────────────────────────
# Prevents browsers from caching API responses. Without this, GET calls to the
# same URL (e.g. /agents, /skills, /sdlc/runs) return stale browser-cached data
# even when the backend has newer data — the user then needs a hard refresh.
#
# Static assets under /dist/ are intentionally excluded so Vite's hashed bundles
# are still served with long-lived cache headers (the hash changes on rebuild).

from starlette.middleware.base import BaseHTTPMiddleware

class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/assets"):
            # Vite content-hashed bundles: safe to cache forever.
            # The filename hash changes on every rebuild so stale files
            # are never served after a new deployment.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            # All API responses and index.html: must never be cached.
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"]        = "no-cache"
            response.headers["Expires"]       = "0"
        return response

app.add_middleware(NoCacheMiddleware)

# ── Security headers middleware ───────────────────────────────────────────────
# Mirrors ABStudio's SecurityHeadersMiddleware (ABStudio/backend/app/main.py
# lines 804-814) — same class name, same header set, same values — so the two
# apps present an identical security-header posture.
#
# Why it is needed here too: ABStudio's copy only runs when ABStudio is started
# standalone on :8002 (development, see the CORS note below it). In production
# this gateway serves the built ABStudio SPA at /build-studio and the ai-ui SPA
# at /, so those pages previously went out with no anti-framing header at all —
# they could be embedded in a hostile page and clickjacked (CWE-1021). The
# gateway is also the process that actually serves ABStudio's routes in the
# embedded/production deployment (see "ABStudio (Build Studio) — direct router
# mount" below — those routers are included directly on THIS `app`, not on
# ABStudio's own standalone FastAPI object in app/main.py), so ABStudio's copy
# never runs for requests like CatalogPicker.jsx's `/ainxt/v1/api/abs/*`
# fetches unless this gateway also sets the header — hence the
# duplicate-looking middleware here.
#
# HSTS is set UNCONDITIONALLY (not gated on request scheme). Behind a
# TLS-terminating reverse proxy, the scheme uvicorn sees on the underlying
# connection is "http" even though the real client connection is HTTPS, so a
# scheme check here would silently drop the header in production — the exact
# deployment shape HSTS exists to protect. Browsers ignore the header on a
# genuine (unproxied) plain-HTTP connection, so sending it unconditionally is
# also safe for local/dev HTTP use.
#
# Anti-framing is likewise safe to set unconditionally: nothing frames these
# pages. All in-app <iframe>s use srcDoc or blob: URLs (browser-local
# documents that never carry a gateway response header), the Electron
# renderer loads the SPA top-level via mainWindow.loadURL(), and there are no
# embed/widget endpoints and no Teams/SharePoint tab manifest in the repo.
#
# Desktop note: the Electron renderer replaces the CSP header outright in
# _installCspEnforcement() (desktop/src/main.js), so a CSP frame-ancestors set
# here would be dropped inside the desktop app. X-Frame-Options is not touched
# by that override, so anti-framing is enforced on both the browser and desktop
# paths — which is why this relies on X-Frame-Options rather than CSP.

# DAST fix — Missing Security Headers: the only header the report found
# missing that was NOT already sent above is Content-Security-Policy. It is
# added below rather than to the docstring-listed set because it needs a
# path-based exception: /docs and /redoc (FastAPI's built-in Swagger UI /
# ReDoc pages) load their JS/CSS from cdn.jsdelivr.net, which a same-origin
# default-src would block. Those two dev/API-explorer routes keep the
# baseline security headers above but are excluded from CSP; every other
# route — including the actual SPA the DAST scan targeted — gets the header.
_CSP_EXEMPT_PATH_PREFIXES = ("/docs", "/redoc", "/openapi.json")
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' blob: data:; "
    "font-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Emit HSTS and other security headers on every response (Checkmarx: Missing HSTS Header, Potential Clickjacking)."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), payment=()"
        if not request.url.path.startswith(_CSP_EXEMPT_PATH_PREFIXES):
            response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
        return response

app.add_middleware(SecurityHeadersMiddleware)

# Kill backend coroutine at 118 s — matches Nginx client_read_timeout of 120 s.
# TimeoutMiddleware removed from starlette 0.21+; enforce via anyio move_on_after in handlers.

# ── Rate limiting (slowapi) ────────────────────────────────────────────────────
# Per-user JWT: 60 req/min. Per-IP (unauthenticated): 200 req/min (DoS backstop).
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    def _rate_key(request: Request) -> str:
        """Use JWT sub (user id) when authenticated, IP otherwise.

        Only *valid, non-expired* tokens contribute a user-scoped rate-limit
        key.  Expired or tampered tokens fall back to the IP-based key so that
        an attacker cannot bypass the per-IP limit by replaying old tokens.
        """
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            try:
                from auth.jwt_handler import decode_token as _rl_decode
                payload = _rl_decode(auth[7:])
                if payload and payload.get("sub"):
                    return f"user:{payload['sub']}"
            except Exception:
                pass
        return f"ip:{get_remote_address(request)}"

    limiter = Limiter(key_func=_rate_key, default_limits=["60/minute"])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    logger.info(
        "Rate limiter active: slowapi 60 req/min per user JWT | "
        "RateLimitMiddleware 200/min per user, 300/min per IP | "
        "Behaviour-anomaly detection enabled"
    )
except ImportError:
    logger.warning(
        "slowapi not installed — slowapi decorator limits disabled. "
        "RateLimitMiddleware (global) is still active. Run: pip install slowapi"
    )

# ============================================================
# ROUTERS
# ============================================================

from routers.skills_router import router as skills_router
from routers.index_router import router as index_router
from routers.codewiki_router import router as codewiki_router
from routers.budget_router import router as budget_router
from routers.inbox_router import router as inbox_router
from routers.mailbox_router import router as mailbox_router
from routers.projects_router import router as projects_router
from routers.marketplace_router import router as marketplace_router
from routers.auth_router import router as auth_router
from routers.session_router import router as session_router   # DAST fix — concurrent session endpoints
from routers.notifications_router import router as notifications_router
from routers.mcp_governance_router import router as mcp_governance_router
from core.config import ENABLE_WEBHOOKS as _ENABLE_WEBHOOKS
if _ENABLE_WEBHOOKS:
    from routers.webhooks_router import router as webhooks_router
from core.config import ENABLE_GRAPH_WEBHOOKS as _ENABLE_GRAPH_WEBHOOKS
if _ENABLE_GRAPH_WEBHOOKS:
    from routers.graph_webhooks_router import router as graph_webhooks_router
from routers.sdlc_router import router as sdlc_router
from routers.vault_router import router as vault_router
from auth.sso import sso_router
from routers.jobs_router import router as jobs_router
from core.config import ENABLE_SLACK as _ENABLE_SLACK
if _ENABLE_SLACK:
    from routers.slack_router import router as slack_router
from routers.memory_router import router as memory_router, analytics_router as agent_analytics_router
from routers.compliance_router import router as compliance_router
from routers.chat_router import router as chat_router
from routers.governance_router import router as governance_router
from routers.ide_router import router as ide_router
from routers.evals_router import router as evals_router

# Discussions module (Apache Answer) — self-contained feature, only mounted when
# ENABLE_DISCUSSIONS is on. This import + the include_router call below are the
# ONLY places this module touches gateway.py — see routers/discussions_router.py.
from core.config import ENABLE_DISCUSSIONS as _ENABLE_DISCUSSIONS
if _ENABLE_DISCUSSIONS:
    from routers.discussions_router import router as discussions_router
# AiNxt Coach — self-contained feature, only mounted when ENABLE_COACH is on
from core.config import ENABLE_COACH as _ENABLE_COACH
if _ENABLE_COACH:
    from routers.coach_router import router as coach_router
    from routers.coach_admin_router import router as coach_admin_router

from core.config import ENABLE_TEAMS as _ENABLE_TEAMS
if _ENABLE_TEAMS:
    from routers.teams_router import router as teams_router
from routers.docs_router import router as docs_router
from routers.kb_router import router as kb_router
from routers.kb_ask_router import router as kb_ask_router
from core.config import ENABLE_ZOHO_HR as _ENABLE_ZOHO_HR
if _ENABLE_ZOHO_HR:
    from routers.zoho_router import router as zoho_router
from routers.n8n_router import router as n8n_router
from routers.feedback_router import router as feedback_router
from routers.profile_router  import router as profile_router
from routers.products_router import router as products_router
from routers.admin_router    import admin_router
from core.config import ENABLE_BROADCAST as _ENABLE_BROADCAST
if _ENABLE_BROADCAST:
    from routers.broadcast_router import router as broadcast_router
from routers.mcp_server_router import router as mcp_server_router
from routers.cowork_mcp_router import router as cowork_mcp_router
from routers.compliance_scan_router import router as compliance_scan_router
from routers.knowledge_graph_router import router as knowledge_graph_router
from routers.secure_code_gate_router import router as secure_code_gate_router
from routers.cowork_tasks_router import router as cowork_tasks_router
from routers.cowork_admin_router import router as cowork_admin_router
from routers.cowork_usage_router import router as cowork_usage_router
from routers.cowork_policy_router import router as cowork_policy_router
from routers.cowork_dispatch_router import router as cowork_dispatch_router
from routers.cowork_projects_router import router as cowork_projects_router
from routers.cowork_conversations_router import router as cowork_conversations_router
from routers.code_conversations_router import router as code_conversations_router
from routers.scim_router import router as scim_router
from routers.agents_router import router as agents_catalog_router
from routers.model_governance_router import router as model_governance_router
from routers.dept_metrics_router import router as dept_metrics_router
from routers.api_keys_router import router as api_keys_router
from routers.doc_download_router import router as doc_download_router
from routers.connectors_router import router as connectors_router
from routers.desktop_router    import router as desktop_router
from routers.presenton_router  import router as presenton_router
from routers.messages_compat_router import router as messages_compat_router
# AiNxt CLI: new endpoints exposed for /audit and /sandbox CLI commands.
from routers.cli_updates_router import router as cli_updates_router
from routers.cli_updates_router import cli_fleet_router
from routers.audit_router import router as audit_router
from routers.sandbox_router import router as sandbox_router
# Monthly usage statement (HTML email + DB archive)
from core.config import ENABLE_MONTHLY_STATEMENT as _ENABLE_MONTHLY_STATEMENT
if _ENABLE_MONTHLY_STATEMENT:
    from routers.monthly_statement_router import monthly_statement_router
# Enterprise LLM spend tracking (admin endpoints; cron jobs registered in startup)
from core.config import ENABLE_LLM_SPEND_REPORT as _ENABLE_LLM_SPEND_REPORT
if _ENABLE_LLM_SPEND_REPORT:
    from routers.llm_spend_report_router import llm_spend_report_router
# HOD monthly usage digest (per-department roll-up email)
# Manager monthly usage digest (per-reporting-manager team roll-up email)
from core.config import ENABLE_HOD_DIGEST as _ENABLE_HOD_DIGEST
if _ENABLE_HOD_DIGEST:
    from routers.digest_hod_router import digest_hod_router
    from routers.digest_manager_router import digest_manager_router
from routers.chat_router import router as chat_router, _public_share_router as chat_public_share_router
from routers.templates_router import (
    router as templates_router,
    admin_router as templates_admin_router,
)
from routers.cached_ask_router import router as cached_ask_router
from routers.endpoint_mgmt_router import router as endpoint_mgmt_router
from routers.endpoint_proxy_router import proxy_router as endpoint_proxy_router
# P10: Prompt version management
from routers.prompt_mgmt_router import router as prompt_mgmt_router
# P12: Code review engine
from routers.review_router import router as review_router

# ── ABStudio (Build Studio) routers ──────────────────────────────────────────
# ABStudio/backend is added to sys.path so its `app.*` packages resolve without
# moving any files. The try/except guard means a missing package or import error
# degrades gracefully — gateway starts normally, Build Studio routes unavailable.
try:
    import sys as _sys
    _abs_backend_path = os.path.join(os.path.dirname(__file__), "ABStudio", "backend")
    if _abs_backend_path not in _sys.path:
        _sys.path.insert(0, _abs_backend_path)

    from app.api.execution        import router as _abs_execution_router
    from app.api.chat             import router as _abs_chat_router
    from app.api.generation       import router as _abs_generation_router
    from app.api.documents        import router as _abs_documents_router
    from app.api.workflows        import router as _abs_workflows_router
    from app.api.templates        import router as _abs_templates_router
    from app.api.agents           import router as _abs_agents_router
    from app.api.agent_templates  import router as _abs_agent_templates_router
    from app.api.mcp              import router as _abs_mcp_router
    from app.api.catalog          import router as _abs_catalog_router
    from app.api.triggers         import router as _abs_triggers_router
    from app.api.factories        import router as _abs_factories_router
    from app.api.agent_chat       import router as _abs_agent_chat_router
    from app.api import agent_chat as _abs_agent_chat
    # The MCP tool plane the spawned ainxt CLI calls back into. Mounted at ROOT
    # (no /abs prefix) because the per-run config.toml points the CLI at
    # ``/abstudio-mcp/{run_id}``. Without this the CLI's MCP handshake 404s and
    # the agent silently gets zero tools (no code_executor).
    from app.cli_runtime.mcp_router import router as _abs_cli_mcp_router
    _ABS_ROUTERS_LOADED = True
except Exception as _abs_import_err:
    logger.warning(f"[ABStudio] routers not loaded — Build Studio unavailable: {_abs_import_err}")
    _ABS_ROUTERS_LOADED = False

# ── ABStudio (Build Studio) routers ──────────────────────────────────────────
# ABStudio/backend is added to sys.path so its `app.*` packages resolve without
# moving any files. The try/except guard means a missing package or import error
# degrades gracefully — gateway starts normally, Build Studio routes unavailable.
try:
    import sys as _sys
    _abs_backend_path = os.path.join(os.path.dirname(__file__), "ABStudio", "backend")
    if _abs_backend_path not in _sys.path:
        _sys.path.insert(0, _abs_backend_path)

    from app.api.execution        import router as _abs_execution_router
    from app.api.chat             import router as _abs_chat_router
    from app.api.generation       import router as _abs_generation_router
    from app.api.documents        import router as _abs_documents_router
    from app.api.workflows        import router as _abs_workflows_router
    from app.api.templates        import router as _abs_templates_router
    from app.api.agents           import router as _abs_agents_router
    from app.api.agent_templates  import router as _abs_agent_templates_router
    from app.api.mcp              import router as _abs_mcp_router
    from app.api.catalog          import router as _abs_catalog_router
    from app.api.triggers         import router as _abs_triggers_router
    from app.api.factories        import router as _abs_factories_router
    from app.api.agent_chat       import router as _abs_agent_chat_router
    from app.api.agent_sample     import router as _abs_agent_sample_router  # Per-agent Sample Document.
    from app.api.kb               import router as _abs_kb_router
    # Optional feature-flagged template editor (env: TEMPLATES_EDITABLE).
    # To remove: delete this import + the corresponding entry in the mount
    # loop below.
    from app.api.template_admin   import router as _abs_template_admin_router
    from app.api.governance        import router as _abs_governance_router
    from app.api import agent_chat as _abs_agent_chat
    # MCP tool plane for the spawned ainxt CLI — mounted at ROOT (see below).
    from app.cli_runtime.mcp_router import router as _abs_cli_mcp_router

    # Optional feature-flagged Pattern Library (env: PATTERN_LIBRARY_ENABLED).
    # Imports are guarded so a rollback is `unset PATTERN_LIBRARY_ENABLED` +
    # restart; the router variable stays None so the mount loop below skips
    # it silently.
    _abs_patterns_router = None
    _abs_pattern_repo    = None
    if os.getenv("PATTERN_LIBRARY_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from app.api.patterns import router as _abs_patterns_router
            from app.core import pattern_repo as _abs_pattern_repo
        except Exception as _pat_err:
            logger.warning(f"[ABStudio] pattern library imports failed: {_pat_err}")
            _abs_patterns_router = None
            _abs_pattern_repo    = None
    _ABS_ROUTERS_LOADED = True
except Exception as _abs_import_err:
    logger.warning(f"[ABStudio] routers not loaded — Build Studio unavailable: {_abs_import_err}")
    _ABS_ROUTERS_LOADED = False

app.include_router(admin_router,            prefix="/ainxt/v1/api")
if _ENABLE_BROADCAST:
    app.include_router(broadcast_router,    prefix="/ainxt/v1/api")
app.include_router(api_keys_router,         prefix="/ainxt/v1/api")
app.include_router(profile_router,          prefix="/ainxt/v1/api")
app.include_router(products_router,         prefix="/ainxt/v1/api")
app.include_router(skills_router,           prefix="/ainxt/v1/api")
app.include_router(index_router,            prefix="/ainxt/v1/api")
app.include_router(codewiki_router,         prefix="/ainxt/v1/api")
app.include_router(budget_router,           prefix="/ainxt/v1/api")
app.include_router(inbox_router,            prefix="/ainxt/v1/api")
app.include_router(mailbox_router,          prefix="/ainxt/v1/api")
app.include_router(projects_router,         prefix="/ainxt/v1/api")
app.include_router(marketplace_router,      prefix="/ainxt/v1/api")
app.include_router(auth_router,             prefix="/ainxt/v1/api")
app.include_router(session_router,          prefix="/ainxt/v1/api")   # DAST fix — concurrent session endpoints
app.include_router(notifications_router,    prefix="/ainxt/v1/api")
app.include_router(mcp_governance_router,   prefix="/ainxt/v1/api")
if _ENABLE_WEBHOOKS:
    app.include_router(webhooks_router,     prefix="/ainxt/v1/api")
if _ENABLE_GRAPH_WEBHOOKS:
    app.include_router(graph_webhooks_router, prefix="/ainxt/v1/api")
app.include_router(sdlc_router,             prefix="/ainxt/v1/api")
app.include_router(vault_router,            prefix="/ainxt/v1/api")
app.include_router(sso_router,              prefix="/ainxt/v1/api")
app.include_router(jobs_router,             prefix="/ainxt/v1/api")
if _ENABLE_SLACK:
    app.include_router(slack_router,        prefix="/ainxt/v1/api")
app.include_router(memory_router,           prefix="/ainxt/v1/api")
app.include_router(agent_analytics_router,  prefix="/ainxt/v1/api")
app.include_router(compliance_router,       prefix="/ainxt/v1/api")
app.include_router(chat_router,             prefix="/ainxt/v1/api")
# Public read-only share endpoint — NO auth, NO /ainxt/v1/api prefix.
# Mounted at root so /shared/{token} works in incognito / external links.
app.include_router(chat_public_share_router)
app.include_router(governance_router,       prefix="/ainxt/v1/api")
app.include_router(ide_router,              prefix="/ainxt/v1/api")

if _ENABLE_DISCUSSIONS:
    app.include_router(discussions_router, prefix="/ainxt/v1/api")
if _ENABLE_COACH:
    app.include_router(coach_router,        prefix="/ainxt/v1/api")
    app.include_router(coach_admin_router,  prefix="/ainxt/v1/api")

app.include_router(evals_router,            prefix="/ainxt/v1/api")
if _ENABLE_TEAMS:
    app.include_router(teams_router,        prefix="/ainxt/v1/api")
app.include_router(docs_router,             prefix="/ainxt/v2/api")
app.include_router(kb_router,               prefix="/ainxt/v2/api")
app.include_router(kb_ask_router,           prefix="/ainxt/v2/api")
if _ENABLE_ZOHO_HR:
    app.include_router(zoho_router,         prefix="/ainxt/v1/api")
app.include_router(templates_router,         prefix="/ainxt/v1/api")
app.include_router(templates_admin_router,   prefix="/ainxt/v1/api")
app.include_router(n8n_router,              prefix="/ainxt/v1/api")
app.include_router(feedback_router,         prefix="/ainxt/v1/api")
app.include_router(mcp_server_router,       prefix="/ainxt/v1/api")
app.include_router(cowork_mcp_router,       prefix="/ainxt/v1/api")
app.include_router(compliance_scan_router,  prefix="/ainxt/v1/api")
app.include_router(knowledge_graph_router,  prefix="/ainxt/v1/api")
app.include_router(secure_code_gate_router,  prefix="/ainxt/v1/api")
app.include_router(cowork_tasks_router,     prefix="/ainxt/v1/api")
app.include_router(cowork_admin_router,     prefix="/ainxt/v1/api")
app.include_router(cowork_usage_router,     prefix="/ainxt/v1/api")
app.include_router(cowork_policy_router,    prefix="/ainxt/v1/api")
app.include_router(cowork_dispatch_router,  prefix="/ainxt/v1/api")
app.include_router(cowork_projects_router,  prefix="/ainxt/v1/api")
app.include_router(cowork_conversations_router, prefix="/ainxt/v1/api")
app.include_router(code_conversations_router, prefix="/ainxt/v1/api")
# SCIM 2.0 provisioning — mounted BOTH at the API prefix and at root, so an IdP
# can be pointed at either /scim/v2 or /ainxt/v1/api/scim/v2. Bearer-token gated.
app.include_router(scim_router,             prefix="/ainxt/v1/api")
app.include_router(scim_router)
app.include_router(agents_catalog_router,   prefix="/ainxt/v1/api")
app.include_router(prompt_mgmt_router,      prefix="/ainxt/v1/api")  # P10
app.include_router(review_router,           prefix="/ainxt/v1/api")  # P12
app.include_router(model_governance_router, prefix="/ainxt/v1/api")
app.include_router(dept_metrics_router,     prefix="/ainxt/v1/api")
app.include_router(doc_download_router,     prefix="/ainxt/v1/api")
app.include_router(connectors_router,       prefix="/ainxt/v1/api")
app.include_router(presenton_router,        prefix="/ainxt/v1/api")
app.include_router(messages_compat_router,  prefix="/ainxt/v1/api")
# AiNxt CLI routers
app.include_router(cli_updates_router,       prefix="/ainxt/v1/api")
app.include_router(cli_fleet_router,         prefix="/ainxt/v1/api")
app.include_router(audit_router,            prefix="/ainxt/v1/api")
app.include_router(sandbox_router,          prefix="/ainxt/v1/api")
if _ENABLE_MONTHLY_STATEMENT:
    app.include_router(monthly_statement_router, prefix="/ainxt/v1/api")
if _ENABLE_LLM_SPEND_REPORT:
    app.include_router(llm_spend_report_router,  prefix="/ainxt/v1/api")
if _ENABLE_HOD_DIGEST:
    app.include_router(digest_hod_router,        prefix="/ainxt/v1/api")
    app.include_router(digest_manager_router,    prefix="/ainxt/v1/api")
app.include_router(cached_ask_router,       prefix="/ainxt/v1/api")
app.include_router(desktop_router,          prefix="")

# ── Managed Endpoints — admin CRUD + OpenAI-compatible proxy ─────────────────
# Admin CRUD:  /ainxt/v1/api/endpoint-mgmt/...
# Proxy:       /ainxt/v1/api/{slug}/v1/chat/completions
#              /ainxt/v1/api/{slug}/v1/models
# IMPORTANT: endpoint_mgmt_router must be registered BEFORE endpoint_proxy_router
# so that the fixed /endpoint-mgmt/ path takes precedence over the dynamic /{slug}/ path.
app.include_router(endpoint_mgmt_router,    prefix="/ainxt/v1/api")
app.include_router(endpoint_proxy_router,   prefix="/ainxt/v1/api")
# Internal key-delivery endpoint — no /ainxt/v1/api prefix, no user auth.
# Reachable at /internal/ckms/proxy-keys. Protected by X-Proxy-Key-Token
# header and should be IP-restricted to web02 at the firewall layer.
app.include_router(_internal_ckms_router)

# ── ABStudio (Build Studio) — direct router mount ────────────────────────────
if _ABS_ROUTERS_LOADED:
    _abs_prefix = "/ainxt/v1/api/abs"
    for _r in [
        _abs_execution_router,  _abs_chat_router,        _abs_generation_router,
        _abs_documents_router,  _abs_workflows_router,   _abs_templates_router,
        _abs_agents_router,     _abs_agent_templates_router, _abs_mcp_router,
        _abs_catalog_router,    _abs_triggers_router,    _abs_factories_router,
        _abs_agent_chat_router,
        _abs_agent_sample_router,   # Per-agent Sample Document (look-and-feel reference).
        _abs_kb_router,         # Build-Studio-only KB upload proxy (auto-approve, multi-file).
        _abs_template_admin_router,  # Optional feature-flagged template editor (env: TEMPLATES_EDITABLE).
        _abs_governance_router,  # Governance/approval submit + status for Build Studio artifacts.
    ] + ([_abs_patterns_router] if _abs_patterns_router is not None else []):
        # Pattern Library router is only present when PATTERN_LIBRARY_ENABLED
        # was set at import time. See the try/except in the imports block.
        app.include_router(_r, prefix=_abs_prefix)

    # The CLI MCP tool plane must be mounted at the ROOT, NOT under /abs: the
    # per-run config.toml points the spawned ainxt CLI at
    # ``http://<host>:8000/abstudio-mcp/{run_id}``. In standalone mode this route
    # lives on ABStudio's own app (app/main.py); when the gateway serves :8000 it
    # must include the router here too, or the CLI's MCP handshake 404s and the
    # agent silently loses all its tools (including code_executor).
    try:
        app.include_router(_abs_cli_mcp_router)
        logger.info("[ABStudio] mounted CLI MCP router at /abstudio-mcp/{run_id}")
    except Exception as _cli_mcp_err:
        logger.warning(f"[ABStudio] could not mount CLI MCP router — CLI tools will 404: {_cli_mcp_err}")

    # /generated-files/{run_id}/{filename} and /generated-files/{filename} are
    # defined directly on ABStudio's app object (not a router), so we
    # re-register them here under the /abs prefix.
    #
    # Commit 18dc0a42 changed platform_tools.py to store generated files in a
    # per-run subdirectory (GENERATED_FILES_DIR/<run_id>/<filename>) and updated
    # main.py with a matching two-segment route, but gateway.py was not updated.
    # This caused a 404 "Not Found" whenever a workflow agent (e.g. Shortlister,
    # Decline Drafter) generated a .docx file and the user clicked Download.
    import pathlib as _pathlib
    import time as _time
    from fastapi.responses import FileResponse as _FileResponse

    # Ownership gate for generated-file downloads (IDOR fix). Imported from
    # ABStudio's stdlib-only ``app.owner_tag`` — the single definition shared
    # with app.main and cli_runtime.workspace. This replaces a hand-copied
    # duplicate that was kept in sync only by a comment and had no test coverage.
    #
    # ``app.owner_tag`` imports nothing but hashlib, and ABStudio/backend is
    # already on sys.path (see the _ABS_ROUTERS_LOADED block above), so this
    # costs nothing at startup.
    #
    # FAIL CLOSED: if the import fails, every ownership check must DENY rather
    # than fall back to a local re-implementation. A packaging error must not be
    # able to silently disable a cross-tenant access control. Downloads break
    # loudly (and are logged as an error) instead of serving other users' files.
    try:
        from app.owner_tag import is_generated_path_allowed as _abs_generated_path_allowed
    except Exception as _abs_owner_tag_err:
        logger.error(
            "[ABStudio] could not import app.owner_tag — generated-file downloads "
            f"will be DENIED (failing closed on the ownership gate): {_abs_owner_tag_err}"
        )

        def _abs_generated_path_allowed(rel_parts, user_id: str) -> bool:  # type: ignore[misc]
            return False

    def _abs_serve_generated_file(relative_path: str, user_id: str = ""):
        """Shared helper: resolve, authorize, TTL-check, and serve a generated
        artifact.

        Mirrors the logic in ABStudio/backend/app/main.py::download_generated_file
        so both the standalone (main.py) and gateway-embedded serving paths
        behave identically — including the per-user ownership gate that stops
        one authenticated user from downloading another user's artifact (IDOR).
        """
        _base = _pathlib.Path(
            os.getenv("GENERATED_FILES_DIR")
            or os.getenv("ABS_GENERATED_FILES_DIR")
            or os.path.join(os.path.dirname(__file__), "ABStudio", "tmp")
        ).resolve()
        _base.mkdir(parents=True, exist_ok=True)
        _target = (_base / relative_path).resolve()
        try:
            _rel = _target.relative_to(_base)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid filename")

        # Ownership gate. 404 (not 403) on a cross-user request so we never
        # confirm the existence of another user's artifact.
        if not _abs_generated_path_allowed(_rel.parts, user_id):
            raise HTTPException(status_code=404, detail="File not found")
        # TTL check — mirrors ABStudio main.py's _is_expired logic.
        _ttl = int(os.getenv("GENERATED_FILES_TTL_SECONDS", "86400"))
        _expired = False
        try:
            _age = _time.time() - _target.stat().st_mtime
            _expired = _age > _ttl
        except FileNotFoundError:
            _expired = True
        if not _target.is_file() or _expired:
            try:
                _target.unlink(missing_ok=True)
            except Exception:
                pass
            raise HTTPException(
                status_code=410,
                detail=f"File '{relative_path}' has expired and is no longer available.",
            )
        _ext = _target.suffix.lower()
        _media_type = {
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".pdf": "application/pdf",
            ".md": "text/markdown",
            ".txt": "text/plain",
        }.get(_ext, "application/octet-stream")
        return _FileResponse(str(_target), media_type=_media_type, filename=_target.name)

    @app.get(
        "/ainxt/v1/api/abs/generated-files/{owner_or_run}/{filename}",
        include_in_schema=False,
        tags=["build-studio"],
    )
    async def _abs_generated_file_per_run(
        owner_or_run: str,
        filename: str,
        current_user: dict = Depends(get_current_user),
    ):
        """Download a generated artifact stored in a two-segment subdirectory.

        Files are organized as ``GENERATED_FILES_DIR/<owner_tag>/<filename>``
        (per-user isolation — see app.main.owner_tag) so the original filename
        stays clean in URLs while the endpoint scopes access to the owner.
        Authentication is REQUIRED and the caller may only fetch files inside
        their own owner-dir (IDOR fix).
        """
        _uid = current_user.get("userId") or current_user.get("id") or current_user.get("sub", "")
        return _abs_serve_generated_file(f"{owner_or_run}/{filename}", _uid)

    @app.get(
        "/ainxt/v1/api/abs/generated-files/{filename}",
        include_in_schema=False,
        tags=["build-studio"],
    )
    async def _abs_generated_file(
        filename: str,
        current_user: dict = Depends(get_current_user),
    ):
        """Legacy download route for flat-layout files created before the
        per-user owner-dir change. Authentication is REQUIRED; flat files are
        readable by any authenticated user and age out via the TTL.
        """
        _uid = current_user.get("userId") or current_user.get("id") or current_user.get("sub", "")
        return _abs_serve_generated_file(filename, _uid)

    # Per-run layout: files now land in GENERATED_FILES_DIR/<run_id>/<name> and
    # download_url is /generated-files/{run_id}/{filename}. FastAPI's single

    # /health is defined on ABStudio's own app object (not a router),
    # so it must be registered explicitly here under the /abs prefix.
    @app.get("/ainxt/v1/api/abs/health", include_in_schema=False, tags=["build-studio"])
    async def _abs_health():
        import asyncio as _abs_asyncio
        try:
            from app.engine import get_engine as _abs_get_engine
            from app.core import workflow_repo as _abs_repo
            _health = await _abs_get_engine().health()
        except Exception:
            _health = {}
        _pool = None
        try:
            from app.core import workflow_repo as _abs_repo
            _pool = _abs_repo.get_pool()
        except Exception:
            pass
        if _pool is None:
            _health["db"] = "ok"
            _health["db_mode"] = "memory"
        else:
            try:
                def _check():
                    with _pool.connection() as _conn:
                        _conn.execute("SELECT 1").fetchone()
                await _abs_asyncio.to_thread(_check)
                _health["db"] = "ok"
                _health["db_mode"] = "postgres"
            except Exception as _e:
                _health["db"] = "error"
                _health["db_error"] = str(_e)
        return _health

    logger.info(f"[ABStudio] routers mounted at {_abs_prefix}")

# ── Core API routes (versioned) ───────────────────────────────────────────────
_v1 = APIRouter()


# SPA static frontend — registered at BOTTOM of file after all API routes.
# See end of this file.


# ============================================================
# STARTUP
# ============================================================
#

def _start_cli_mcp_loopback_listener() -> None:
    """Bring up a per-worker private HTTP listener for CLI MCP callbacks.

    The CLI-runtime session registry is a per-process singleton
    (``app/cli_runtime/session.py``: ``_REGISTRY``), and the tool-event bus
    on each ``RunSession`` is an in-memory asyncio ``Queue``. With more than
    one worker, a callback from a spawned ``ainxt`` child to the shared
    public listen socket is routed by the kernel to an arbitrary worker,
    which typically does not hold the run and returns
    401 ``unknown or expired run``.

    To fix that without a shared registry, every worker binds an extra HTTP
    listener on ``127.0.0.1`` (kernel-picked ephemeral port), mounted with
    only the CLI-MCP router, and publishes its URL via
    ``ABSTUDIO_CLI_MCP_LOOPBACK_URL`` in this worker's ``os.environ``.
    ``cli_runtime.config.mcp_base_url`` prefers that override on every spawn,
    so each child's ``config.toml`` embeds the exact URL of the worker that
    spawned it. The public listener on port 8000 is untouched.

    The listener runs in a daemon thread with its own asyncio loop so it
    cannot interact with the main worker's loop. Because it lives in the
    same process, it shares ``_REGISTRY`` naturally.

    Idempotent: called once per worker from the FastAPI startup event.
    Failure to bring the listener up is logged loudly but does not block
    gateway startup — the operator sees a clear error rather than the
    gateway refusing to serve public traffic.
    """
    # Guard against a re-entrant startup (e.g. multiple ``@app.on_event``
    # invocations in tests) so we do not stack listeners in one process.
    if os.environ.get("ABSTUDIO_CLI_MCP_LOOPBACK_URL", "").strip():
        return

    import socket as _socket
    import threading as _threading
    import time as _time
    import asyncio as _asyncio

    host = os.getenv("ABSTUDIO_CLI_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    ready_timeout_s = 5.0
    try:
        ready_timeout_s = max(
            0.5, float(os.getenv("ABSTUDIO_CLI_MCP_READY_TIMEOUT_S", "5"))
        )
    except (TypeError, ValueError):
        ready_timeout_s = 5.0

    # Pre-bind port 0 to let the kernel pick a free ephemeral port, then
    # release it and hand the number to uvicorn. There is a race window
    # between close() and uvicorn's own bind() where another process could
    # grab the port, but on loopback with a fresh ephemeral this is
    # vanishingly rare; the ready-check below catches it if it happens.
    try:
        probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        probe.bind((host, 0))
        port = probe.getsockname()[1]
        probe.close()
    except OSError as exc:
        logger.error(
            f"[CLI-MCP] could not reserve loopback port on {host}: {exc!r} "
            f"— CLI callbacks will keep landing on the shared public port"
        )
        return

    url = f"http://{host}:{port}"

    def _serve() -> None:
        try:
            import uvicorn
            from fastapi import FastAPI as _FastAPI
            from app.cli_runtime.mcp_router import router as _cli_mcp_router

            # A minimal ASGI app so we do not re-run the main gateway's
            # startup events (DB pools, schedulers, model discovery). Only
            # the CLI-MCP router is needed; it depends solely on the
            # in-process ``_REGISTRY`` and ``cli_runtime_config()``.
            slim = _FastAPI(
                title="AiNxt CLI-MCP loopback",
                docs_url=None, redoc_url=None, openapi_url=None,
            )
            slim.include_router(_cli_mcp_router)

            cfg = uvicorn.Config(
                slim,
                host=host,
                port=port,
                log_level="warning",
                access_log=False,
                loop="asyncio",
                lifespan="on",  # this slim app has no startup handlers
            )
            server = uvicorn.Server(cfg)

            loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(loop)
            loop.run_until_complete(server.serve())
        except Exception as exc:
            logger.error(
                f"[CLI-MCP] loopback listener crashed pid={os.getpid()} "
                f"url={url} error={exc!r}"
            )

    t = _threading.Thread(
        target=_serve, name="cli-mcp-loopback", daemon=True,
    )
    t.start()

    # Block briefly until the listener accepts, so the first spawn on this
    # worker does not race the port opening. Only publish the URL once the
    # port is proven up: a silent failure here would surface much later as
    # ``unknown or expired run`` on the child's first MCP call.
    deadline = _time.monotonic() + ready_timeout_s
    ready = False
    while _time.monotonic() < deadline:
        try:
            with _socket.create_connection((host, port), timeout=0.2):
                ready = True
                break
        except OSError:
            _time.sleep(0.1)

    if ready:
        os.environ["ABSTUDIO_CLI_MCP_LOOPBACK_URL"] = url
        logger.info(
            f"[CLI-MCP] loopback listener up pid={os.getpid()} url={url} "
            f"— CLI children will dial back into this worker"
        )
    else:
        logger.error(
            f"[CLI-MCP] loopback listener NOT ready within {ready_timeout_s:.1f}s "
            f"pid={os.getpid()} url={url} — CLI callbacks will keep landing on "
            f"the shared public port and 401"
        )


@app.on_event("startup")
async def startup():
    # Advertised-model / routing-hint coverage. Runs here rather than at import
    # time so every model constant in list_oai_models() is defined.
    try:
        _audit_model_hint_coverage()
    except Exception as _mhc_err:
        logger.warning("Model hint coverage audit failed: %s", _mhc_err)

    # ------------------------------------------------------------
    # PLATFORM VERSION
    # ------------------------------------------------------------
    try:
        _version_file = os.path.join(os.path.dirname(__file__), "VERSION")
        _platform_version = open(_version_file).read().strip() if os.path.exists(_version_file) else "dev"
    except Exception:
        _platform_version = "dev"
    logger.info(f"Gateway startup — platform version: {_platform_version}")

    # ------------------------------------------------------------
    # CLI-MCP PER-WORKER LOOPBACK LISTENER
    # Must run in every worker before the first CLI spawn. Publishes
    # ABSTUDIO_CLI_MCP_LOOPBACK_URL to this worker's env, which
    # cli_runtime.config.mcp_base_url picks up on every spawn.
    # ------------------------------------------------------------
    try:
        _start_cli_mcp_loopback_listener()
    except Exception as _mcp_loop_exc:
        logger.error(
            f"[CLI-MCP] loopback listener bootstrap raised: {_mcp_loop_exc!r} "
            f"— CLI callbacks will keep landing on the shared public port"
        )

    # ------------------------------------------------------------
    # ANYIO THREADPOOL SIZE — raise the per-worker thread limiter.
    # Starlette/FastAPI run sync work (def endpoints, run_in_threadpool,
    # asyncio.to_thread) on AnyIO's default thread limiter, which defaults to
    # only 40 threads per process. The CLI path (/v1/messages) offloads its
    # mandatory auth/budget/compliance gates — all blocking I/O — to this pool,
    # so 40 becomes the per-worker concurrency ceiling for in-flight gates.
    # These threads are I/O-waiting (Redis/DB/privacy-svc), not CPU-bound, so a
    # higher cap is safe on multi-core hosts. Env-tunable; default 200.
    # ------------------------------------------------------------
    try:
        import anyio.to_thread
        _tp_size = int(os.getenv("GATEWAY_THREADPOOL_SIZE", "200"))
        anyio.to_thread.current_default_thread_limiter().total_tokens = _tp_size
        logger.info(f"Gateway startup — AnyIO threadpool size set to {_tp_size}")
    except Exception as _tpe:
        logger.warning(f"Gateway startup — could not set AnyIO threadpool size: {_tpe}")

    # ------------------------------------------------------------
    # KV BACKEND MAP — log the per-DB backend resolution so that
    # incident postmortems can see the deployed topology at a glance.
    # ------------------------------------------------------------
    try:
        from core.kv import kv_backend_map
        _bm = kv_backend_map()
        _summary = " ".join(f"DB{db}={backend}" for db, backend in _bm.items())
        logger.info(f"[KV] backends: {_summary}")
    except Exception as _bme:
        logger.warning(f"[KV] backend map unavailable: {_bme}")

    # ------------------------------------------------------------
    # REQUIRED ENV VAR VALIDATION (warn, don't crash)
    # ------------------------------------------------------------
    _required_env_vars = ["JWT_SECRET", "POSTGRES_HOST", "REDIS_HOST"]
    for _env_var in _required_env_vars:
        if not os.getenv(_env_var):
            logger.critical(
                f"STARTUP WARNING: required env var {_env_var!r} is not set — "
                "platform may behave incorrectly"
            )

    # ------------------------------------------------------------
    # DB MIGRATIONS — only when RUN_MIGRATIONS_ON_STARTUP=true
    # Run migrations as a deploy step: python db/migrate.py
    # Do not enable this flag in normal startup — it hits Postgres
    # with dozens of DDL statements on every worker process.
    # ------------------------------------------------------------
    if os.getenv("RUN_MIGRATIONS_ON_STARTUP", "false").lower() == "true":
        try:
            from db.migrate import run_migrations
            run_migrations()
            logger.info("DB migrations complete")
        except (Exception, SystemExit) as _me:
            logger.warning(f"DB migrations skipped: {_me}")

    # ------------------------------------------------------------
    # AUTO-SEED ADMIN (OSS only — skipped when AUTO_SEED_ADMIN=false)
    # If the users table is empty and AUTO_SEED_ADMIN=true, create a
    # default admin user and print the credentials to the console once.
    # Directory-backed deployments: the users table is never empty
    # (LDAP-populated) → no-op even if the flag were true. Set
    # AUTO_SEED_ADMIN=false in your .env to be
    # explicit.
    # ------------------------------------------------------------
    try:
        from core.config import AUTO_SEED_ADMIN, SEED_ADMIN_EMAIL
        if AUTO_SEED_ADMIN:
            from db.database import SessionLocal as _SL
            from db.models import User as _User
            from passlib.context import CryptContext as _CryptContext

            with _SL() as _db:
                _user_count = _db.query(_User).count()

            if _user_count == 0:
                # Use SEED_ADMIN_PASSWORD from .env. When it is not set, mint a
                # cryptographically random one and print it once, rather than
                # falling back to a shared literal — a well-known default in a
                # public repo is a valid credential on every deployment whose
                # operator did not override it (CWE-798). Mirrors the behaviour
                # scripts/seed.py already uses for the same account.
                _admin_pass = os.getenv("SEED_ADMIN_PASSWORD")
                _generated  = not _admin_pass
                if _generated:
                    import secrets as _secrets
                    import string as _string
                    _alphabet   = _string.ascii_letters + _string.digits + "!@#$%^&*"
                    _admin_pass = "".join(_secrets.choice(_alphabet) for _ in range(20))
                _pwd_ctx = _CryptContext(schemes=["bcrypt"], deprecated="auto")

                with _SL() as _db:
                    _admin = _User(
                        email=SEED_ADMIN_EMAIL,
                        name="Platform Admin",
                        role="admin",
                        org_id="AiNxt",
                        hashed_password=_pwd_ctx.hash(_admin_pass),
                        # A generated password is a first-login credential, not
                        # the operator's chosen one, so flag it: Profile shows the
                        # "temporary password" banner until it is changed, and
                        # change_password() clears the flag.
                        is_temp_password=_generated,
                        ad_level=0,
                        department="",
                        is_active=True,
                        account_status="active",
                    )
                    _db.add(_admin)
                    _db.commit()

                # Always print to console — visible in terminal and docker logs
                print("\n" + "=" * 60)
                print("  AiNxt — Default admin user created")
                print("=" * 60)
                print(f"  Email    : {SEED_ADMIN_EMAIL}")
                print(f"  Password : {_admin_pass}")
                print("  Role     : admin")
                print("-" * 60)
                if _generated:
                    print("  ⚠  This password was generated and is shown ONCE.")
                    print("  Save it now, then change it after your first login")
                    print("  in Profile → Security.")
                    print("  To choose your own instead, set SEED_ADMIN_PASSWORD")
                    print("  in .env before first boot.")
                    print("  Lost it? Use 'Forgot password' on the login page.")
                print("=" * 60 + "\n")
                logger.info(
                    "auto_seed.admin_created",
                    email=SEED_ADMIN_EMAIL,
                    generated_password=_generated,
                )
            else:
                logger.debug("auto_seed.skipped — users table is not empty")
    except Exception as _ase:
        logger.warning(f"auto_seed failed (non-fatal): {_ase}")

    # ------------------------------------------------------------
    # STARTUP CONFIG CHECK — runs in ALL modes (local + prod).
    # Prints a human-readable summary of what is configured and
    # what is missing. Non-fatal — never prevents startup.
    # ------------------------------------------------------------
    try:
        from core.config import startup_config_check
        startup_config_check()
    except Exception as _scc_e:
        logger.warning(f"startup_config_check failed (non-fatal): {_scc_e}")

    # ------------------------------------------------------------
    # PRODUCTION CONFIG VALIDATION (fail-fast before any requests)
    # ------------------------------------------------------------
    try:
        from core.config import validate_prod_config
        validate_prod_config()
    except RuntimeError as exc:
        logger.critical(f"PRODUCTION CONFIG INVALID — refusing to start:\n{exc}")
        raise SystemExit(1) from exc

    # ------------------------------------------------------------
    # INJECTION SIDECAR HEALTH CHECK (non-blocking, informational)
    # Controlled by ENABLE_INJECTION_SCAN + INJECTION_SCAN_URL.
    # ------------------------------------------------------------
    _inj_scan_enabled_startup = os.getenv("ENABLE_INJECTION_SCAN", "true").strip().lower() not in ("0", "false", "no")
    _inj_svc_url = os.getenv("INJECTION_SCAN_URL", "").rstrip("/")
    if _inj_scan_enabled_startup and _inj_svc_url:
        try:
            import httpx as _httpx_startup
            from core.config import injection_scan_verify as _inj_verify_startup
            _inj_health = _httpx_startup.get(
                f"{_inj_svc_url}/health", timeout=3.0, verify=_inj_verify_startup()
            )
            _inj_data   = _inj_health.json()
            logger.info(
                f"ainxt-injection-svc connected — "
                f"url={_inj_svc_url} mode={_inj_data.get('mode')} "
                f"layers: policy_ingress={_inj_data.get('layer_policy_ingress')} "
                f"heuristic={_inj_data.get('layer_heuristic')} "
                f"llm_judges={_inj_data.get('layer_llm_judges')} "
                f"policy_egress={_inj_data.get('layer_policy_egress')}"
            )
        except Exception as _inj_err:
            logger.warning(
                f"ainxt-injection-svc unreachable at {_inj_svc_url} — "
                f"injection scanning disabled ({_inj_err}). "
                f"Start the sidecar or set ENABLE_INJECTION_SCAN=false."
            )
    elif not _inj_scan_enabled_startup:
        logger.info("ainxt-injection-svc: ENABLE_INJECTION_SCAN=false — injection scanning disabled.")
    else:
        logger.warning(
            "ainxt-injection-svc: ENABLE_INJECTION_SCAN=true but INJECTION_SCAN_URL not set — "
            "injection scanning disabled. Set INJECTION_SCAN_URL=http://127.0.0.1:8007."
        )

    # ------------------------------------------------------------
    # SAFE MODEL WARMUP (non-blocking background thread)
    # ------------------------------------------------------------

    def _warm_models():
        try:
            logger.info("Background model warmup starting")

            # Trigger Local LLM model discovery in background (non-blocking)
            try:
                from gateway_local_llm import get_local_gateway
                gw = get_local_gateway()
                models = gw.list_models()
                tiers  = gw.models_by_tier()
                logger.info(
                    f"Local LLM warmup complete — "
                    f"{len(models)} model(s): {models} | tiers: {tiers}"
                )
            except Exception as e:
                logger.info(f"Local LLM warmup skipped — not configured ({e.__class__.__name__})")

            logger.info("Model warmup complete")

        except Exception as e:
            logger.error(f"Warmup failed: {e}")

    try:
        import threading
        threading.Thread(
            target=_warm_models,
            daemon=True,
            name="model-warmup"
        ).start()
        logger.info("Model warmup scheduled (background)")
    except Exception as e:
        logger.warning(f"Model warmup scheduling failed: {e}")

    # ------------------------------------------------------------
    # SEED PLATFORM SKILLS
    # ------------------------------------------------------------

    try:
        from routers.skills_router import seed_platform_skills
        seed_platform_skills()
        logger.info("Platform skills seeded")
    except Exception as e:
        logger.warning(f"Platform skills seed skipped: {e}")

    # ------------------------------------------------------------
    # SEED AiNxt DOMAIN SKILLS + AGENT TEMPLATES (idempotent)
    # Seeds on every startup — upserts only, never overwrites custom changes.
    # ------------------------------------------------------------

    def _seed_ainxt_data():
        try:
            from scripts.seed import seed as _seed_fn
            result = _seed_fn()
            if result:
                logger.info(f"AiNxt seed complete: {len(result)} items upserted")
            else:
                logger.info("AiNxt seed complete (no new items)")
        except Exception as _se:
            logger.warning(f"AiNxt seed skipped (non-critical): {_se}")

    try:
        import threading as _seed_thread
        _seed_thread.Thread(
            target=_seed_ainxt_data,
            daemon=True,
            name="ainxt-seed"
        ).start()
        logger.info("AiNxt domain seed scheduled (background)")
    except Exception as _e:
        logger.warning(f"AiNxt seed scheduling failed: {_e}")

    # ------------------------------------------------------------
    # SDLC STALE RUN CLEANUP
    # ------------------------------------------------------------

    try:
        import threading
        from store.sdlc_store import cancel_stale_runs

        threading.Thread(
            target=cancel_stale_runs,
            args=(4,),
            daemon=True,
            name="sdlc-cleanup"
        ).start()

        logger.info("SDLC stale run cleanup scheduled")

    except Exception as e:
        logger.warning(f"SDLC stale run cleanup skipped: {e}")

    # ------------------------------------------------------------
    # ABSTUDIO — DB init, catalog seed, scheduler
    # ------------------------------------------------------------
    if _ABS_ROUTERS_LOADED:
        # Run all ABStudio startup work directly on uvicorn's event loop so
        # that the AsyncIOScheduler (and any other async resources) stay bound
        # to the loop that will remain alive for the lifetime of the process.

        # NOTE: ABStudio's tables must live in the ``ainxt`` schema (the shared
        # platform pool uses search_path=ainxt,public). Consolidating any legacy
        # ``public`` copies into ``ainxt`` is a ONE-TIME, OPERATOR-RUN step —
        # see db/sql/consolidate_abstudio_public_to_ainxt.sql. The application
        # deliberately does NOT migrate schema at startup, to avoid ever
        # touching live data automatically.

        # 1. Engine startup
        try:
            from app.engine import get_engine as _abs_get_engine
            await _abs_get_engine().startup()
            logger.info("[ABStudio] engine started")
        except Exception as _e:
            logger.warning(f"[ABStudio] engine startup skipped: {_e}")

        # 2. DB tables
        try:
            from app.core import workflow_repo as _abs_repo
            await _abs_repo.init_db()
            # Loud, terminal-visible confirmation so we don't have to grep
            # structlog files to know whether ABStudio's seeding ran.
            try:
                import sys as _abs_sys
                _abs_sys.stderr.write("[ABStudio] init_db OK -- templates seeded\n")
                _abs_sys.stderr.flush()
            except Exception:
                pass
        except Exception as _e:
            logger.warning(f"[ABStudio] DB init skipped: {_e}")
            # Loud, terminal-visible failure so we can actually see what went
            # wrong without digging through structlog rotating files.
            try:
                import sys as _abs_sys, traceback as _abs_tb
                _abs_sys.stderr.write(f"[ABStudio] init_db FAILED: {_e!r}\n")
                _abs_tb.print_exc(file=_abs_sys.stderr)
                _abs_sys.stderr.flush()
            except Exception:
                pass

        # 2b. Agent chat history store (creates agent_chat_threads table)
        try:
            await _abs_agent_chat.startup()
            logger.info("[ABStudio] agent chat store ready")
        except Exception as _e:
            logger.warning(f"[ABStudio] agent chat store startup skipped: {_e}")

        # 2c. Pattern Library — creates patterns / pattern_versions /
        # pattern_usages tables and seeds the four canonical presets
        # (Planner→Executor→Validator, etc). No-op when the feature flag
        # is off, so this block is safe to leave in.
        if _abs_pattern_repo is not None:
            try:
                await _abs_pattern_repo.init_pattern_tables()
                await _abs_pattern_repo.seed_canonical_patterns()
                logger.info("[ABStudio] pattern library tables ready + seeded")
            except Exception as _e:
                logger.warning(f"[ABStudio] pattern library init skipped: {_e}")

        # 3. Canonical tools/skills seed
        try:
            from app.tools.canonical_tools import (
                seed_canonical_tools  as _abs_seed_tools,
                seed_canonical_skills as _abs_seed_skills,
            )
            await _abs_seed_tools()
            await _abs_seed_skills()
            logger.info("[ABStudio] canonical tools/skills seeded")
        except Exception as _e:
            logger.warning(f"[ABStudio] tool/skill seed skipped: {_e}")

        # 4. Legacy catalog migration
        try:
            from agent_factory.pipeline import seed_catalogs_from_legacy as _abs_seed_legacy
            await _abs_seed_legacy()
        except Exception as _e:
            logger.warning(f"[ABStudio] legacy catalog migration skipped: {_e}")

        # 5. Platform skills seed
        try:
            from app.main import _seed_bundled_skills as _abs_seed_platform
            await _abs_seed_platform()
            logger.info("[ABStudio] Platform skills seeded")
        except Exception as _e:
            logger.warning(f"[ABStudio] Platform skills seed skipped: {_e}")

        # 6. Orphaned agent migration (legacy agents.json → postgres)
        try:
            from app.main import _migrate_orphaned_agents_from_registry as _abs_migrate_agents
            await _abs_migrate_agents()
            logger.info("[ABStudio] orphan agent migration complete")
        except Exception as _e:
            logger.warning(f"[ABStudio] orphan agent migration skipped: {_e}")

        # 7. Trigger scheduler — must share the uvicorn event loop so that
        #    APScheduler jobs are not cancelled when a temporary loop exits.
        try:
            from app.services import trigger_scheduler as _abs_sched
            await _abs_sched.init_scheduler()
            logger.info("[ABStudio] trigger scheduler started")
        except Exception as _e:
            logger.warning(f"[ABStudio] scheduler init skipped: {_e}")

    # ── Enterprise LLM spend tracking ────────────────────────────
    # Nightly fetcher + four exec-digest cron jobs (daily/weekly/
    # monthly/quarterly). Times default to 01:30 IST (fetch) and
    # 10:00 IST (digests); all overridable via env. Also performs a
    # one-shot 90-day backfill if llm_spend_daily is empty.
    try:
        from services.llm_spend import gateway_bootstrap as _spend_boot
        _spend_boot.start()
        logger.info("[llm_spend] scheduler + backfill started")
    except Exception as _e:
        logger.warning(f"[llm_spend] startup skipped: {_e}")

    # ── HOD + Manager monthly usage digest cron ──────────────────
    # Default schedule: 18:00 IST on the last day of every month.
    # Configurable via TEAM_USAGE_DIGEST_CRON_TIME / _TZ / _DAY / _ENABLED.
    # See services/digest_service.py for the full env contract.
    try:
        from services import digest_service as _digest_cron
        _digest_cron.start_scheduler()
        logger.info("[digest_cron] scheduler started")
    except Exception as _e:
        logger.warning(f"[digest_cron] startup skipped: {_e}")


@app.on_event("startup")
async def _mount_teams_sdk():
    """Mount the official Teams SDK messaging endpoint onto this FastAPI app.

    No-op unless TEAMS_SDK_ENABLED=true. Registers the route via the SDK's
    App.initialize() (does not start a second server). Runs as a separate
    async startup handler because App.initialize() is a coroutine.
    """
    try:
        from integrations.teams_sdk_app import mount_teams_sdk_app
        await mount_teams_sdk_app(app)
    except Exception as e:
        logger.warning(f"Teams SDK mount skipped: {e}")

@app.on_event("shutdown")
async def shutdown():
    """
    Graceful shutdown: allow in-flight requests up to 10 s to drain,
    then close DB connection pools so Postgres sees clean disconnects.
    """
    import asyncio
    logger.info("Gateway shutdown — draining in-flight requests (max 10 s)")
    await asyncio.sleep(2)   # brief window for load-balancer health-check to fail over

    try:
        from db.database import engine, vector_engine
        engine.dispose()
        vector_engine.dispose()
        logger.info("DB connection pools closed")
    except Exception as exc:
        logger.warning(f"DB pool dispose failed: {exc}")

    try:
        from core.kafka_producer import flush as _kafka_flush
        _kafka_flush()
        logger.info("Kafka producer flushed")
    except Exception as exc:
        logger.warning(f"Kafka flush failed: {exc}")

    # ── Enterprise LLM spend scheduler ───────────────────────────
    try:
        from services.llm_spend import gateway_bootstrap as _spend_boot
        _spend_boot.stop()
        logger.info("[llm_spend] scheduler stopped")
    except Exception as _e:
        logger.warning(f"[llm_spend] scheduler shutdown skipped: {_e}")

    # ── HOD + Manager digest scheduler ───────────────────────────
    try:
        from services import digest_service as _digest_cron
        _digest_cron.stop_scheduler()
        logger.info("[digest_cron] scheduler stopped")
    except Exception as _e:
        logger.warning(f"[digest_cron] scheduler shutdown skipped: {_e}")

    # ── ABStudio — stop scheduler, engine, and close DB pool ─────
    if _ABS_ROUTERS_LOADED:
        try:
            from app.services import trigger_scheduler as _abs_sched
            await _abs_sched.shutdown_scheduler()
            logger.info("[ABStudio] scheduler stopped")
        except Exception as _e:
            logger.warning(f"[ABStudio] scheduler shutdown skipped: {_e}")
        try:
            from app.engine import get_engine as _abs_get_engine
            await _abs_get_engine().shutdown()
            logger.info("[ABStudio] engine stopped")
        except Exception as _e:
            logger.warning(f"[ABStudio] engine shutdown skipped: {_e}")
        try:
            await _abs_agent_chat.shutdown()
            logger.info("[ABStudio] agent chat store stopped")
        except Exception as _e:
            logger.warning(f"[ABStudio] agent chat store shutdown skipped: {_e}")
        try:
            from app.core import workflow_repo as _abs_repo
            await _abs_repo.close_db()
            logger.info("[ABStudio] DB pool closed")
        except Exception as _e:
            logger.warning(f"[ABStudio] DB pool close skipped: {_e}")

    # ── Close all KV clients (sync + async) ──────────────────────
    # Drains the Redis connection pools so the next process start
    # sees no half-open connections.
    try:
        from core.kv import close_all_kv, async_close_all_kv
        close_all_kv()
        try:
            await async_close_all_kv()
        except Exception as _aexc:
            logger.warning(f"Async KV close failed: {_aexc}")
        logger.info("KV clients closed")
    except Exception as exc:
        logger.warning(f"KV close failed: {exc}")

# @app.on_event("startup")
# def startup():
#
#     logger.info("Gateway startup")
#
#     try:
#
#         # warm_load_model()
#
#         try:
#
#             from gateway_local_llm import warm_load_local_llm
#
#             warm_load_local_llm()
#
#             logger.info("Local LLM warmup complete")
#
#         except Exception:
#
#             logger.warning("Local LLM warmup skipped")
#
#         logger.info("Model warmup complete")
#
#     except Exception as e:
#
#         logger.error(f"Warmup failed: {e}")
#
#     # ── Seed platform skills ──────────────────────────────────────────────
#     try:
#         from routers.skills_router import seed_platform_skills
#         seed_platform_skills()
#         logger.info("Platform skills seeded")
#     except Exception as _seed_err:
#         logger.warning(f"Platform skills seed skipped: {_seed_err}")
#
#     # ── Auto-cancel stale SDLC runs (runs stuck >4 hours) ────────────────
#     try:
#         import threading
#         from store.sdlc_store import cancel_stale_runs
#         threading.Thread(target=cancel_stale_runs, args=(4,), daemon=True).start()
#         logger.info("SDLC stale run cleanup scheduled")
#     except Exception as _stale_err:
#         logger.warning(f"SDLC stale run cleanup skipped: {_stale_err}")
#

# ============================================================
# REQUEST MODEL
# ============================================================

class Question(BaseModel):

    question:       str
    model:          Optional[str]      = None   # "claude"|"gpt"|"gemini"|None (auto)
    attachment_ids: Optional[List[str]] = []
    project_id:     Optional[str]      = None   # project scope for budget tracking
    chat_id:        Optional[str]      = None   # existing chat; None = create new
    repo_filter:    Optional[str]      = None   # explicit repo scope (Threads, IDE)
    voice_platform: bool               = False  # voice mode + AiNxt Platform toggle
    tone:           Optional[str]      = None   # "casual" | None — adaptive tone
    user_name:      Optional[str]      = None   # user's display name for personalization
    login_id:       Optional[str]      = None   # user's login/AD identifier for audit logging
    local_model:    Optional[str]      = None   # local Ollama model override
    agent_id:       Optional[str]      = None   # agent name to scope chat to (Agent Catalog)
    cli_mode:       bool               = False  # CLI agent path: skip orchestrator, direct model call
    system_prompt:  Optional[str]      = None   # CLI agent system prompt (prepended before conversation)
    session_id:     Optional[str]      = None   # CLI session ID — used as chat_id so context persists across turns
    cli_messages:   Optional[List[dict]] = None  # Client-side history [{role, content}] — bypasses DB lookup
    images:         Optional[List[dict]] = None  # CLI image input: [{data: base64, media_type: 'image/png'|'image/jpeg'|...}] → forced to vision model
    rag_mode:       Optional[str]      = None   # "off" (generic, default) | "auto" (low-threshold KB) | "on" (force KB probe). None falls back to chat record's stored mode, else "off".
    # ── KB scope (inline fallback) ────────────────────────────────────────
    # Normally the gateway loads the chat's KB scope from the Chat row in
    # Postgres (set by PATCH /chats/{id}/scope from KbChatPanel). But on the
    # very first /ask of a freshly-handed-off KB chat, the row doesn't exist
    # yet — chat_persist creates it after the response. That race meant turn
    # 1 retrieval was unscoped (searched the entire KB instead of the picked
    # document). These four fields let the client send the scope inline as a
    # fallback for that first turn; the DB row takes precedence on later turns.
    product_id:     Optional[str]      = None   # KB scope: product UUID (from KbDrillGraph)
    domain:         Optional[str]      = None   # KB scope: domain label
    spec_version:   Optional[str]      = None   # KB scope: spec version string
    kb_doc_id:      Optional[str]      = None   # KB scope: specific KB doc UUID (drives coverage_retriever)
    kb_doc_ids:     Optional[List[str]] = None  # KB disambig: user-selected doc UUIDs from DocPickerCard (multi-select re-query)
    ephemeral:      bool               = False  # True = skip chat-history Kafka produce. Used by frontend intent classifier to avoid polluting the sidebar with orphan chats.
    mode:           Optional[str]      = None   # UI surface: None/"chat" (default) | "office" (Cowork — connector/KB-aware planner persona)


def _save_chat_messages(chat_id: str, user_id: str, question: str, answer: str,
                        model: str, in_tok: int, out_tok: int, cost: float,
                        language: str, attachment_ids: list, project_id: str,
                        latency: float = None, title_hint: str = None,
                        agent_id: str = None, client_source: str = "platform",
                        coverage_trace: dict = None,
                        rag_mode: str = None, repo_filter: str = None):
    """Persist user + assistant messages to Postgres. Called in a background thread."""
    try:
        from db.database import SessionLocal
        from db.models import Chat, ChatMessage
        import uuid as _uuid_mod
        from datetime import datetime as _dt

        logger.info(f"[chat_persist] Saving messages chat_id={chat_id} user_id={user_id} and model = {model}")

        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id).first()
            if not chat:
                chat = Chat(
                    id=chat_id,
                    user_id=user_id if user_id not in ("default", "") else None,
                    title=(title_hint or question)[:80],
                    project_id=project_id or None,
                    agent_id=agent_id or None,
                    client_source=client_source,
                )
                logger.info(f"[chat_persist] Creating new chat chat_id={chat_id} user_id={user_id} and model = {model}")
                db.add(chat)
            else:
                if chat.title in ("New Chat", "", None) and title_hint:
                    logger.info(f"[chat_persist] Not persisting because of new chat with title={chat.title}")
                    chat.title = title_hint[:80]
                # Backfill agent_id if chat was created without it
                if agent_id and not chat.agent_id:
                    logger.info(f"[chat_persist] Found existing chat record with title={chat.title}")
                    chat.agent_id = agent_id

            db.add(ChatMessage(
                id=str(_uuid_mod.uuid4()),
                chat_id=chat_id,
                role="user",
                content=question,
                attachment_ids=attachment_ids,
                rag_mode=rag_mode,
            ))
            db.add(ChatMessage(
                id=str(_uuid_mod.uuid4()),
                chat_id=chat_id,
                role="assistant",
                content=answer,
                model_used=model,
                tokens_used=in_tok + out_tok,
                in_tok=in_tok or None,
                out_tok=out_tok or None,
                latency=float(latency) if latency else None,
                cost_usd=cost,
                language=language,
                # Phase 3 transparency persistence — kn_rewrite.md §8x.
                # Stored verbatim from hybrid_retriever's coverage_trace dict
                # so a reload of this chat shows the same badge that streamed
                # during the live answer.
                coverage_trace=coverage_trace or None,
                rag_mode=rag_mode,
            ))
            chat.updated_at = _dt.utcnow()
            logger.info("Commiting chat into db")
            db.commit()
        finally:
            db.close()

        # Update rolling per-chat summary (used when history exceeds threshold)
        try:
            from memory.chat_summarizer import update_chat_summary
            update_chat_summary(chat_id, question, answer)
        except Exception as _sum_err:
            logger.debug(f"chat summary update skipped: {_sum_err}")

        # Extract + persist verbatim structured facts (JSON/YAML/CSV/tables and
        # exact key:value values) from the user turn. Re-injected into context
        # later so exact values survive summarisation. Best-effort — never breaks
        # turn-save.
        try:
            from memory import structured_facts as _sf
            _sf.extract_and_store(chat_id, question, answer)
        except Exception as _sf_err:
            logger.debug(f"structured facts store skipped: {_sf_err}")

        # Persist cross-chat user memory using the piggybacked <!--MEMORY:{...}-->
        # footer that the LLM appended to its response. No extra LLM call needed.
        try:
            import re as _re_orch
            from memory.postgres_memory import PostgresMemory as _PM
            _orch_mem: dict = {}
            _mem_pat = _re_orch.compile(r'\n?<!--MEMORY:(\{.*?\})-->\s*$', _re_orch.DOTALL)
            _orch_match = _mem_pat.search(answer)
            if _orch_match:
                try:
                    _orch_mem = json.loads(_orch_match.group(1))
                except Exception:
                    pass
            if (
                _orch_mem.get("store") is True
                and _orch_mem.get("summary", "").strip()
                and user_id and user_id not in ("", "default")
            ):
                _PM().save_user_memory(
                    user_id,
                    _orch_mem["summary"].strip(),
                    metadata={"model": model, "chat_id": chat_id},
                    rag_mode=rag_mode,
                    source_repo=repo_filter,
                    context_hint=_orch_mem.get("context_key", "").strip(),
                )
        except Exception as _um_err:
            logger.debug(f"user memory save skipped: {_um_err}")

    except Exception as _e:
        logger.warning(f"_save_chat_messages failed: {_e}")


# ============================================================
# AGENT BUILDER REQUEST MODELS
# ============================================================

class AgentCreate(BaseModel):
    name: str
    description: str
    system_prompt: str
    tools: List[str] = []
    skills: List[str] = []
    workflows: List[str] = []
    tags: List[str] = []
    version: str = "1.0.0"
    author: str = "platform"
    visibility: str = "private"
    department: str = ""
    kb_namespace: Optional[str] = None     # e.g. "docs_kb:hr" — scopes retrieve tool
    preferred_model: Optional[str] = None  # auto|claude|gpt|ollama

class AgentRun(BaseModel):
    message: str
    session_id: Optional[str] = None
    stream: bool = False
    attachment_ids: Optional[List[str]] = None


# ============================================================
# PII MASKING
# ============================================================

# Pre-compiled PII masking patterns (mirrors pii_detector.py — keep in sync).
# Applied to all outbound text (logs, prompts forwarded to LLM).
_MASK_CARD16_RE = re.compile(r'\b\d{16}\b')
_MASK_PHONE_RE  = re.compile(
    r'(?<!\d)'
    r'(?:'
        r'(?:\+|00)\d{1,3}'
        r'[\s\-\.]?(?:\(?\d{1,4}\)?[\s\-\.]?)?'
        r'\d{3,6}[\s\-\.]?\d{3,6}'
        r'|'
        r'[6-9]\d{9}'
    r')'
    r'(?!\d)',
    re.ASCII
)
_MASK_EMAIL_RE  = re.compile(r'\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b')
_MASK_IPAN_RE   = re.compile(r'\b[A-Z]{5}[0-9]{4}[A-Z]\b', re.IGNORECASE)


def mask_pii(text):
    """Mask PII before forwarding text to an LLM or writing to logs.

    Covers: 16-digit card numbers, phone numbers (Indian + international with
    country-code prefix), email addresses, Indian PAN card numbers.
    This is a best-effort pre-send mask — the compliance engine (which runs
    first) is the authoritative block gate.
    """
    if not text:
        return text
    text = _MASK_CARD16_RE.sub("XXXX-XXXX-XXXX-XXXX", text)
    text = _MASK_PHONE_RE.sub("XXXXXXXXXX", text)
    text = _MASK_EMAIL_RE.sub("***@***.***", text)
    text = _MASK_IPAN_RE.sub("XXXXX0000X", text)
    return text


# ============================================================
# DYNAMIC REPO DETECTION (PRODUCTION SAFE)
# ============================================================

_REPO_CACHE = None

def detect_repo(question: str):
    """
    Detect which indexed codebase repo a question is about.
    Repo list is loaded from document_embeddings (pgvector) — the single source of truth.
    ChromaDB is fully removed.
    """
    if not question:
        return None

    try:
        global _REPO_CACHE

        if not _REPO_CACHE:
            # Load all repo_* namespaces that have actual embeddings
            from db.database import VectorSessionLocal
            from sqlalchemy import text as _sql
            vdb = VectorSessionLocal()
            try:
                rows = vdb.execute(_sql(
                    "SELECT DISTINCT repo FROM document_embeddings "
                    "WHERE repo LIKE 'repo_%'"
                )).fetchall()
            finally:
                vdb.close()

            # Normalise: 'repo_upi_stats_git' → store both 'upi_stats_git' and 'upi-stats'
            _names: list[str] = []
            for (repo,) in rows:
                name = repo[len("repo_"):]          # strip 'repo_' prefix
                _names.append(name.lower())
                _names.append(name.lower().replace("_", "-"))
                # Strip trailing _git / -git for shorter alias matching
                if name.lower().endswith("_git"):
                    short = name.lower()[:-4]
                    _names.append(short)
                    _names.append(short.replace("_", "-"))
            _REPO_CACHE = list(dict.fromkeys(_names))  # dedup, preserve order

            logger.info(f"Loaded repo cache: {_REPO_CACHE}")

        if not _REPO_CACHE:
            return None

        tokens = set(re.findall(r"[a-zA-Z0-9_\-]{3,}", question.lower()))

        for repo in _REPO_CACHE:
            if repo in tokens:
                # Return the canonical repo name (strip any -git alias back to base)
                logger.info(f"Detected repo: {repo}")
                return repo

        logger.info("No explicit repo detected in query — returning None")
        return None

    except Exception as e:
        logger.error(f"Repo detection failed: {e}")
        return None


# Fixed markers emitted by the browser-automation-agent's prompt template in
# every Auto/Ask turn (the live DOM snapshot section headers). These MUST stay
# byte-for-byte in sync with the extension side that produces them:
#   browser-automation-agent (Chrome extension) — AGENT_SYSTEM_PROMPT /
#   planNextAction prompt builder (the "PAGE SNAPSHOT:" / "INTERACTIVE ELEMENTS:"
#   section headers of the serialized page context).
# If the extension renames or reformats these headers, update them here too —
# otherwise looks_like_browser_agent_prompt() silently stops matching and the
# RAG-injection bug (see docs/BROWSER_AGENT_AUTO-MODE_FIX_PLAN.md) returns.
# tests/test_browser_agent_prompt.py pins this behaviour.
_BROWSER_AGENT_PROMPT_MARKERS = ("PAGE SNAPSHOT:", "INTERACTIVE ELEMENTS:")


def looks_like_browser_agent_prompt(text: str) -> bool:
    """Header-independent detection of a browser-automation-agent turn.

    The browser extension is tagged via `X-AiNxt-Client: browser-agent`, but that
    header can be stripped in transit (proxy hops), leaving the request classified
    as `platform`. When that happens the prompt would wrongly flow through the IDE
    plain-chat path and get RAG-injected (detect_repo can match a bare token like
    'ainxt-platform' anywhere in the ~28K agent prompt), which corrupts/oversizes
    the prompt and triggers an upstream 400.

    Every browser-agent turn embeds a live DOM snapshot with the fixed markers in
    ``_BROWSER_AGENT_PROMPT_MARKERS`` from the extension's prompt template; a
    genuine IDE/platform codebase question never does. ALL markers must be present
    to avoid false positives (a user could otherwise suppress RAG by mentioning a
    single marker verbatim).
    """
    if not text:
        return False
    return all(marker in text for marker in _BROWSER_AGENT_PROMPT_MARKERS)


# ============================================================
# CACHE KEY
# ============================================================

# Semantic cache flag — controls both L2 read and L2 write on the sync /ask
# path. Honors the SEMANTIC_CACHE_ENABLED env var so the same toggle governs
# the async chat_worker path (which reads the env var directly via
# store/semantic_cache.py). Defaults to off because the cache key is intent-
# only and can serve stale answers on follow-up turns in an ongoing
# conversation — re-enable once context-awareness is in place.
_SEMANTIC_CACHE_ENABLED = os.getenv("SEMANTIC_CACHE_ENABLED", "false").lower() == "true"

# ── ainxt-api session map ─────────────────────────────────────────────────────
# Maps portal chat_id → ainxt-api session_id so conversation history is
# preserved across multiple /ask calls within the same chat thread.
#
# ainxt-api session config — all values from core.config (driven by .env)
from core.config import (
    AINXT_API_URL     as _AINXT_API_URL,
    AINXT_API_BEARER  as _AINXT_API_KEY,
    AINXT_SESSION_TTL as _AINXT_SESS_TTL_CFG,
    AINXT_TIER_MAP    as _AINXT_TIER_MAP_CFG,
)
_AINXT_SESS_KEY_PREFIX = "ainxt:sess:"
_AINXT_SESS_TTL        = _AINXT_SESS_TTL_CFG or 86400  # fallback 24h if not set in .env
_AINXT_API_ENABLED     = os.getenv("AINXT_API_ENABLED", "true").lower().strip() in ("1", "true", "yes")

# Key prefix for pending doc-generation notifications to the CLI session.
# When the gateway generates a document via its own pipeline (Sonnet path),
# it stores a brief context note here so the CLI is informed on the user's
# NEXT message — giving it awareness of the doc for follow-up turns.
_AINXT_DOC_CTX_PREFIX = "ainxt:doc_ctx:"
_AINXT_DOC_CTX_TTL    = 3600  # 1h — long enough to cover any follow-up


def _ainxt_doc_ctx_put(chat_id: str, question: str, title: str, fmt: str, job_id: str) -> None:
    """Store a one-shot doc-context note for the CLI session on this chat."""
    if not chat_id or not _AINXT_API_ENABLED:
        return
    try:
        import json as _dcj
        redis_client.setex(
            f"{_AINXT_DOC_CTX_PREFIX}{chat_id}",
            _AINXT_DOC_CTX_TTL,
            _dcj.dumps({
                "question": (question or "")[:400],
                "title":    (title    or "")[:200],
                "fmt":      (fmt      or ""),
                "job_id":   (job_id   or ""),
            }),
        )
    except Exception as _e:
        logger.warning(f"[ainxt-api] doc_ctx store failed: {_e}")


def _ainxt_doc_ctx_pop(chat_id: str) -> "str | None":
    """Retrieve and DELETE the pending doc-context note (one-shot).
    Returns a formatted system note string, or None if nothing is pending."""
    if not chat_id:
        return None
    try:
        import json as _dcj
        key = f"{_AINXT_DOC_CTX_PREFIX}{chat_id}"
        raw = redis_client.get(key)
        if raw is None:
            return None
        redis_client.delete(key)
        obj = _dcj.loads(raw)
        question = obj.get("question", "")
        title    = obj.get("title",    "")
        fmt      = obj.get("fmt",      "")
        job_id   = obj.get("job_id",   "")
        fmt_label = {
            "pdf": "PDF", "docx": "Word document", "pptx": "PowerPoint presentation",
            "xlsx": "Excel spreadsheet", "csv": "CSV file", "md": "Markdown document",
        }.get(fmt, fmt.upper() if fmt else "document")
        note = (
            f"\n\n[System context — a document was just generated for this chat:\n"
            f"  User asked: {question!r}\n"
            f"  Generated:  {fmt_label} titled {title!r} (job: {job_id})\n"
            f"  If the user asks about 'the document', 'that file', or asks to edit/"
            f"summarize/extend it, this is what they mean. Do not re-generate it "
            f"unless explicitly asked — just reference it.]"
        )
        return note
    except Exception as _e:
        logger.warning(f"[ainxt-api] doc_ctx pop failed: {_e}")
        return None


def _ainxt_sess_get(chat_id):
    """Return (session_id, model, cwd) for chat_id, or (None, None, None) on miss."""
    if not chat_id:
        return None, None, None
    try:
        import json as _sj
        raw = redis_client.get(f"{_AINXT_SESS_KEY_PREFIX}{chat_id}")
        if raw is None:
            return None, None, None
        try:
            obj = _sj.loads(raw)
            return obj.get("sid"), obj.get("model"), obj.get("cwd", "")
        except (ValueError, TypeError):
            return raw, None, None  # legacy plain-string value
    except Exception as _e:
        logger.warning(f"[ainxt-api] session lookup failed: {_e}")
        return None, None, None


def _ainxt_sess_put(chat_id, session_id, model="", cwd=""):
    if not chat_id or not session_id:
        return
    try:
        import json as _sj
        redis_client.setex(
            f"{_AINXT_SESS_KEY_PREFIX}{chat_id}",
            _AINXT_SESS_TTL,
            _sj.dumps({"sid": session_id, "model": model or "", "cwd": cwd or ""}),
        )
    except Exception as _e:
        logger.warning(f"[ainxt-api] session store failed: {_e}")


def _ainxt_sess_drop(chat_id):
    if not chat_id:
        return
    try:
        redis_client.delete(f"{_AINXT_SESS_KEY_PREFIX}{chat_id}")
    except Exception as _e:
        logger.warning(f"[ainxt-api] session evict failed: {_e}")


def _ainxt_register_generated_file(*, path: str, filename: str,
                                    user_id, chat_id) -> "str | None":
    """Register an agent-written file the same way doc workers do: insert a
    GeneratedDocument row pointing at the file's existing path on disk (the
    agent's cwd is DOC_STORAGE_DIR/{user_id}/{chat_id}/, set at session
    creation — see user_doc_dir()), then return the [DOCJOB:...] marker.
    Reuses the existing /docs/download/{file_id} path — no new proxy needed
    since gateway and ainxt-api share the same filesystem/volume."""
    import os as _os
    import uuid as _uuid2
    import datetime as _dt
    from routers.doc_download_router import build_doc_marker

    try:
        if not _os.path.isfile(path):
            logger.warning(f"[ainxt-api] generated file not found on disk: {path!r}")
            return None

        _ext = _os.path.splitext(filename)[1].lstrip(".").lower() or "txt"
        _file_id = str(_uuid2.uuid4())

        try:
            from db.database import SessionLocal
            from db.models import GeneratedDocument
            db = SessionLocal()
            try:
                db.add(GeneratedDocument(
                    id=_file_id,
                    job_id=_file_id,  # no RQ job for agent-written files
                    user_id=str(user_id or "unknown"),
                    chat_id=chat_id or None,
                    format=_ext,
                    title=_os.path.splitext(filename)[0] or filename,
                    filename=filename,
                    file_path=path,
                    created_at=_dt.datetime.utcnow(),
                ))
                db.commit()
            finally:
                db.close()
        except Exception as _db_err:
            logger.error(f"[ainxt-api] GeneratedDocument insert failed: {_db_err}")
            return None

        # Write doc:result to Redis immediately (DB=6, same as doc_worker*.py)
        # so /docs/job/{job_id}/status returns "done" on the very first poll
        # instead of falling through to the RQ-propagation-lag guards, which
        # assume a real queued job and don't apply to agent-written files.
        try:
            import json as _rj
            from core.config import RDB_STREAM as _RDB_STREAM
            from core.kv import get_kv as _get_kv
            _doc_r = _get_kv(_RDB_STREAM, decode_responses=True)
            _doc_r.setex(
                f"doc:result:{_file_id}",
                86400,  # 24h, matches RESULT_TTL in doc_worker*.py
                _rj.dumps({
                    "status":   "done",
                    "file_id":  _file_id,
                    "user_id":  str(user_id or "unknown"),
                    "filename": filename,
                    "format":   _ext,
                }),
            )
        except Exception as _redis_err:
            logger.warning(f"[ainxt-api] doc:result Redis write failed (non-fatal): {_redis_err}")

        _marker = build_doc_marker(_file_id, _ext, filename)
        logger.info(
            f"[ainxt-api] file registered | file_id={_file_id} name={filename} "
            f"ext={_ext} path={path!r} marker={_marker}"
        )
        return _marker

    except Exception as _exc:
        logger.error(f"[ainxt-api] _ainxt_register_generated_file error: {_exc}")
        return None

# ── Wave 1 strangler-fig: RequestContext shadow-capture (default OFF) ────────
# When PIPELINE_V2 is enabled, the /ask handler ALSO builds a RequestContext
# object (docs/architecture/03-request-lifecycle.md L1) and emits per-stage
# telemetry. In Wave 1 this object is written-but-never-read — nothing
# downstream consumes it — so with the flag OFF (the default) the request path
# is byte-identical to before. See docs/architecture/20-production-readiness.md.
_PIPELINE_V2 = os.getenv("PIPELINE_V2", "true").lower() == "true"
from pipeline.request_context import RequestContext  # pure stdlib module
from profiles.resolver import PolicyResolver          # pure stdlib module
from core import otel as _otel                        # record_event never raises
from cil.analyze import analyze as _cil_analyze        # never raises (fail-safe to defaults)
from pipeline.dispatch import (
    DispatchDecision as _DispatchDecision,
    Lane as _Lane,
    decide_fork as _decide_fork,
    FORK_ORCHESTRATOR as _FORK_ORCHESTRATOR,
)
_POLICY_RESOLVER = PolicyResolver()                   # stateless — one instance
# separate sub-flags: DRIVING behaviors are gated independently of the shadow
# layer so recording can ship first. All require PIPELINE_V2 as the master gate.
_PIPELINE_V2_DISPATCH = os.getenv("PIPELINE_V2_DISPATCH", "true").lower() == "true"
_PIPELINE_V2_ROUTING = os.getenv("PIPELINE_V2_ROUTING", "true").lower() == "true"
_PIPELINE_V2_GROUNDING = os.getenv("PIPELINE_V2_GROUNDING", "true").lower() == "true"
# Streaming enrichments (Phase 5, docs/architecture/16): a user-visible plan
# panel + first-class tool events in the chat SSE envelope. Purely additive —
# old clients ignore the new keys — and every emission is wrapped so a stream
# failure can never break the answer. Default on; env opt-out.
_PIPELINE_V2_STREAM = os.getenv("PIPELINE_V2_STREAM", "true").lower() == "true"
# Gap #3 (7/7): pre-flush grounding gate — DEFAULT ON. The fast-path answer is
# BUFFERED (not token-streamed), grounded-checked before flush, and a hedge
# notice is streamed FIRST when confidence is low, so the answer is a function
# of verified evidence (frontier pattern #3). Trade-off: buffering the answer
# removes token-by-token streaming and adds the grounding latency to the turn.
# Set GROUNDING_PREFLUSH_GATE=false to revert to the post-hoc advisory path
# (byte-identical streaming). Requires PIPELINE_V2_GROUNDING to have any effect.
# Fail-safe: on any gate error the buffered answer is still flushed.
_GROUNDING_PREFLUSH_GATE = os.getenv("GROUNDING_PREFLUSH_GATE", "true").lower() == "true"
# KB answers are already grounded by retrieval + grounded prompt, so skip the
# extra preflush verifier for KB mode by default. Regular chat remains governed
# only by GROUNDING_PREFLUSH_GATE / PIPELINE_V2_GROUNDING.
_KB_SKIP_PREFLUSH_GROUNDING = os.getenv("KB_SKIP_PREFLUSH_GROUNDING", "true").lower() == "true"
# Model-based skill/agent routing (regex-free chat path). When ON, the gateway
# routes to a skill/agent from the MODEL-derived conv_state.skill_hint/agent_hint
# instead of keyword matching. @mention always wins; the DB PRODUCTION-existence
# check remains the authoritative guard, so an unknown model hint falls through
# to the orchestrator. Default ON; set CIL_MODEL_ROUTING=false to disable.
_CIL_MODEL_ROUTING = os.getenv("CIL_MODEL_ROUTING", "true").lower() == "true"
# Persona & Style layer: when ON, a persona system-prompt (casual-buddy baseline,
# mirroring the user's detected tone/language, dialing DOWN to professional on
# sensitive domains) is composed from the CIL state + user memory + learned
# feedback prefs, and injected FIRST into the prompt preface. When OFF, the
# legacy static tone prefix / custom-instructions assembly is used unchanged.
_CHAT_PERSONA = os.getenv("CHAT_PERSONA", "true").lower() == "true"
# KV-cache system-message hoisting for local models.
# When ON, stable persona/agent/memory-instruction content is placed in a
# dedicated {"role":"system"} message at index 0 of _fp_messages so vLLM
# APC can cache those KV blocks across turns instead of recomputing them.
# Requires --enable-prefix-caching on the vLLM/Ollama server.
# Set LOCAL_KV_CACHE_HOIST=false to disable without redeploying.
_LOCAL_KV_CACHE_HOIST = os.getenv("LOCAL_KV_CACHE_HOIST", "true").lower() == "true"

# ── KV-cache helpers (extracted to core/kv_cache_hoist.py for testability) ───
# Imported here so the rest of gateway.py can use the short names
# _MEMORY_INSTRUCTION and _build_local_system_message unchanged.
from core.kv_cache_hoist import (          # noqa: E402
    MEMORY_INSTRUCTION  as _MEMORY_INSTRUCTION,
    build_local_system_message as _build_local_system_message,
)
# CIL complexity → router tier hints. Derive from the router's canonical
# _HINT_MAP so a future rename there can't silently drift this gate; fall back
# to the known tier set if the import shape changes.
try:
    from models.model_router import _HINT_MAP as _MR_HINT_MAP
    _PV2_TIER_HINTS = {"simple", "medium", "complex", "deep", "solution"} & set(_MR_HINT_MAP)
    if not _PV2_TIER_HINTS:  # unexpected shape — use the known-good set
        _PV2_TIER_HINTS = {"simple", "medium", "complex", "deep", "solution"}
except Exception:  # noqa: BLE001
    _PV2_TIER_HINTS = {"simple", "medium", "complex", "deep", "solution"}

def cache_key(question, repo_filter, model_hint=None, user_id=None, rag_mode=None):

    repo  = repo_filter or "global"
    model = model_hint or "auto"
    uid   = user_id or "anon"
    mode  = rag_mode or "off"

    # v3: includes rag_mode so a KB answer can never be served from L1
    # to a Generic question with identical prompt text (context isolation).
    raw = f"v3:{uid}:{repo}:{model}:{mode}:{question}"

    return hashlib.sha256(
        raw.strip().lower().encode()
    ).hexdigest()



# ============================================================
# PROMPT ENHANCER ENDPOINT
# ============================================================

from fastapi import Header as _Header


class EnhanceRequest(BaseModel):
    prompt: str

def _enhance_core(prompt: str, include_followups: bool = True) -> dict:
   """Compliance check + model call for prompt enhancement.
   Shared by the /enhance HTTP endpoint (Chat.jsx / Projects.jsx) and the
   openai_chat_completions Kilocode magic-wand path so the logic lives in one place.
   include_followups=True  — full prompt with JSON response; used by UI callers
                             that render the follow-up questions panel.
   include_followups=False — plain-text prompt, no JSON parsing; used by the
                             Kilocode path where follow-ups are not renderable
                             and would waste tokens.
   Returns  {"enhanced": str, "followups": list[str]}.
   Raises HTTPException(422) if compliance blocks the input.
   """
   import json as _json
   from agents.compliance_engine import compliance_engine as _ce_enhance
   from models.model_router import model_router as _mr_enhance
   _chk = _ce_enhance.validate_input(prompt.strip())
   if _chk.get("blocked"):
       _blocked = [f["type"] for f in _chk.get("findings", []) if f.get("blocked")]
       raise HTTPException(status_code=422, detail=f"Input blocked by compliance: {', '.join(_blocked)}")
   _safe = _chk.get("redacted_text") or prompt.strip()
   # Forward first-pass findings so the downstream OpenAI/Gemini gateway skips
   # its redundant second-pass compliance check. Without this, the system
   # instructions (which mention "email", URL patterns, etc.) trigger false-
   # positive PCI blocks on every enhance request.  Same pattern as /ask
   # (see precleared handling ~line 3930).
   _precleared_findings = _chk.get("findings", [])
   _allowed_hints = {"simple", "mini", "medium", "complex", "haiku", "gemini", "deep", "solution"}
   _hint = os.getenv("ENHANCE_MODEL_HINT", "mini").strip().lower()
   _hint = _hint if _hint in _allowed_hints else "mini"
   if include_followups:
       _system = (
            "You are a prompt quality assistant for an enterprise AI platform serving "
            "both technical users (engineers, data scientists, analysts) and non-technical "
            "users (business stakeholders, operations, product, HR, finance).\n\n"

            "Your Job\n"
            "Given a raw user query, transform it into a well-structured prompt and "
            "suggest follow-up questions that would sharpen the response.\n\n"

            "Audience Detection (do this first, silently)\n"
            "Classify the query as TECHNICAL, NON_TECHNICAL, or MIXED based on signals "
            "like terminology, tools mentioned, and intent:\n"
            "- TECHNICAL: code, systems, APIs, infrastructure, data pipelines, debugging, "
            "architecture, SQL, ML, etc.\n"
            "- NON_TECHNICAL: business strategy, communications, policy, planning, "
            "summaries, writing, HR/finance/ops workflows.\n"
            "- MIXED: business problems with technical implications (e.g., 'why are "
            "sales reports slow').\n"
            "Adapt vocabulary, depth, and section emphasis to the detected audience. "
            "Never use jargon the user did not introduce unless it is essential.\n\n"

            "Core Principles\n"
            "- Preserve the user's original intent exactly. Do not invent requirements, "
            "constraints, or facts not present or clearly implied.\n"
            "- Expand only to resolve genuine ambiguity or to add scope that demonstrably "
            "helps the downstream LLM respond better.\n"
            "- Prefer plain, direct language. Avoid filler, restatements, and hedging.\n"
            "- Keep length proportional to the query. A one-line question should not "
            "become a one-page brief.\n"
            "- If the query is already specific and well-formed, make minimal changes.\n\n"

            "Output Structure\n"
            "Produce a plain-text prompt using ONLY the sections below, in this order. "
            "Omit any section that would be empty or speculative.\n\n"

            "Objective\n"
            "One or two sentences stating exactly what the user wants. Mandatory.\n\n"

            "Requirements\n"
            "A numbered or bulleted list of discrete, actionable requirements, "
            "constraints, deliverables, or success criteria derived from the query. "
            "Use sub-bullets for grouping. Mandatory unless the query is purely "
            "conversational (e.g., a greeting or open-ended brainstorm).\n\n"

            "Context (What's Known)\n"
            "Bulleted facts that were explicit or clearly implicit in the query: "
            "environment, tech stack, audience, tone, business domain, timeframe, "
            "symptoms, prior attempts. Omit entirely if nothing meaningful surfaces. "
            "Never fabricate context.\n\n"

            "Assumptions (optional)\n"
            "Include only if you had to make a non-obvious assumption to resolve "
            "ambiguity. Label each assumption clearly so the user can correct it.\n\n"

            "Expected Output Format\n"
            "Describe how the downstream response should be structured: code blocks, "
            "tables, step-by-step instructions, email draft, executive summary, "
            "bullet points, slide outline, etc. Match the format to the audience "
            "(e.g., code for engineers, prose or tables for business users). "
            "Omit if format is genuinely flexible.\n\n"

            "Follow-up Questions\n"
            "Provide up to 3 short, high-leverage questions whose answers would "
            "materially improve the response. Each should target a distinct gap "
            "(scope, constraint, audience, format, data, success criteria). "
            "Phrase them in language the user will understand — plain English for "
            "non-technical users, precise terms for technical ones. "
            "Return an empty list if the prompt is already specific enough.\n\n"

            "Formatting Rules\n"
            "- Use plain text with clear section labels, - bullets, 1. 2. 3. numbered lists, "
            "and sub-bullets where helpful. Do NOT use ## or ### markdown headings.\n"
            "- NEVER collapse multiple points into a single paragraph.\n"
            "- Do not include meta-commentary about your process.\n"
            "- Do not address the user directly in the enhanced prompt; write it as "
            "an instruction to the downstream LLM.\n\n"

            "Response Format (STRICT)\n"
            "Return ONLY valid JSON, no wrapper text, no markdown fences around the JSON:\n"
            '{"enhanced": "<full plain-text string>", "followups": ["<q1>", "<q2>", "<q3>"]}\n'
            "- `enhanced` must be the complete prompt as a single string "
            "with \\n for line breaks. Do NOT use ## or ### in the enhanced value.\n"
            "- `followups` must be an array of 0 to 3 strings.\n"
            "- Escape all internal quotes and newlines correctly so the JSON parses."
                  )
       _full = f"{_system}\n\nUser query:\n{_safe}"
       try:
           result = _mr_enhance.generate(_full, model_hint=_hint,
                                         precleared=True, precleared_findings=_precleared_findings)
           # e.g. ```json\n{...}\n``` — strip it before parsing.
           _result_clean = result.strip() if isinstance(result, str) else ""
           if _result_clean.startswith("```"):
               _lines = _result_clean.splitlines()
               _lines = _lines[1:]  # drop ```json line
               if _lines and _lines[-1].strip() == "```":
                   _lines = _lines[:-1]  # drop closing ```
               _result_clean = "\n".join(_lines).strip()
           parsed = _json.loads(_result_clean)
           return {
               "enhanced":  parsed.get("enhanced", prompt),
               "followups": parsed.get("followups", [])[:3],
           }
       except Exception:
           return {"enhanced": result if isinstance(result, str) else prompt, "followups": []}
   else:
       _system = (
           "Rewrite the following prompt to be precise, clear, and well-scoped for an AI coding assistant. "
           "Preserve the user's exact intent. You may expand — but only to add what genuinely helps: "
           "missing scope, ambiguous terms, implicit assumptions the LLM needs to answer well. "
           "No filler, no restatements, no padding — every added word must improve the response. "
           "Return ONLY the rewritten prompt — no explanation, no quotes, no bullet points, no preamble."
       )
       _full = f"{_system}\n\nPrompt:\n{_safe}"
       try:
           result = _mr_enhance.generate(_full, model_hint=_hint,
                                         precleared=True, precleared_findings=_precleared_findings)
           return {"enhanced": result.strip() if isinstance(result, str) else prompt, "followups": []}
       except Exception:
           return {"enhanced": prompt, "followups": []}

@_v1.post("/enhance", tags=["ai"])
async def enhance_prompt(body: EnhanceRequest, authorization: Optional[str] = _Header(default=None)):
   if not body.prompt or not body.prompt.strip():
       raise HTTPException(status_code=400, detail="prompt is required")
   return _enhance_core(body.prompt)


# ============================================================
# CONTINUE GENERATION ENDPOINT
#
# Re-streams from a truncated assistant message. Pulls the prior
# context (last user question + the partial assistant reply) from
# chat_messages, prepends "[CONTINUE]" instruction so the model
# resumes seamlessly, and APPENDS the new tokens to the existing
# message via SSE.
# ============================================================

class _ContinueReq(BaseModel):
    chat_id:  Optional[str] = None
    rag_mode: Optional[str] = None


@_v1.post("/ask/continue/{message_id}", tags=["ai"])
def continue_generation(
        message_id: str,
        body: _ContinueReq,
        request: Request,
        authorization: Optional[str] = _Header(default=None),
):
    """Resume a stopped/truncated assistant message."""
    import json as _json_cont

    # Resolve user
    _jwt_token = (authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ")
                  else request.cookies.get("auth_token"))
    if not _jwt_token:
        raise HTTPException(status_code=401, detail="auth required")
    try:
        from auth.jwt_handler import decode_token as _decode
        _payload = _decode(_jwt_token) or {}
    except Exception:
        _payload = {}
    user_id = _payload.get("sub") or _payload.get("email") or ""
    if not user_id:
        raise HTTPException(status_code=401, detail="auth required")

    try:
        from db.database import SessionLocal
        from db.models import Chat, ChatMessage
        from models.model_router import model_router as _mr_cont
        from sqlalchemy import desc

        db = SessionLocal()
        try:
            asst = db.query(ChatMessage).filter(ChatMessage.id == message_id).first()
            if not asst or asst.role != "assistant":
                raise HTTPException(status_code=404, detail="assistant message not found")
            chat = db.query(Chat).filter(Chat.id == asst.chat_id, Chat.user_id == user_id).first()
            if not chat:
                raise HTTPException(status_code=403, detail="not your chat")

            # Pull the prior user question (most recent user message before this assistant)
            prior_user = (
                db.query(ChatMessage)
                .filter(
                    ChatMessage.chat_id == asst.chat_id,
                    ChatMessage.role == "user",
                    ChatMessage.created_at < asst.created_at,
                    )
                .order_by(desc(ChatMessage.created_at))
                .first()
            )
            partial = asst.content or ""
            user_q  = (prior_user.content if prior_user else "").strip()

            _continue_prompt = (
                f"USER QUESTION:\n{user_q}\n\n"
                f"PARTIAL ASSISTANT ANSWER (truncated):\n{partial}\n\n"
                "Resume the assistant answer from where it was cut off. Do not repeat "
                "any text already produced. Continue directly with the next characters."
            )

            asst_id = str(asst.id)
            current_content = partial

            def _stream():
                nonlocal current_content
                _gmeta = {"model": "auto", "latency": 0.0}
                _t0 = time.time()
                _cont_meta: dict = {}
                try:
                    for _tok in _mr_cont.stream(
                            [{"role": "user", "content": _continue_prompt}],
                            model_hint="medium",
                    ):
                        if isinstance(_tok, dict):
                            _sm = _tok.get("__stream_meta__")
                            if _sm:
                                _cont_meta = _sm
                            continue
                        if _tok:
                            current_content += _tok
                            yield "data: " + _json_cont.dumps({"t": _tok}) + "\n\n"
                except Exception as _e:
                    yield "data: " + _json_cont.dumps({"t": f"\n[continue failed: {str(_e)[:80]}]"}) + "\n\n"
                _gmeta["latency"] = time.time() - _t0
                _gmeta["model"]   = _resolve_model_id(_cont_meta.get("model_id") or _cont_meta.get("model_label") or getattr(_mr_cont, "last_model_id", None) or getattr(_mr_cont, "last_model_label", ""))
                _gmeta["in_tok"]  = int(_cont_meta.get("in_tok", 0) or 0)
                _gmeta["out_tok"] = int(_cont_meta.get("out_tok", 0) or 0)
                _gmeta["message_id"] = asst_id
                _gmeta["continued"]  = True
                yield "data: " + _json_cont.dumps({"__meta__": _gmeta}) + "\n\n"

                # Persist the now-complete response
                try:
                    from db.database import SessionLocal as _PSL
                    _psl = _PSL()
                    try:
                        _row = _psl.query(ChatMessage).filter(ChatMessage.id == asst_id).first()
                        if _row:
                            _row.content = current_content
                            _psl.commit()
                    finally:
                        _psl.close()
                except Exception:
                    pass

            from fastapi.responses import StreamingResponse as _SR
            return _SR(
                _stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# FOLLOW-UP SUGGESTIONS ENDPOINT
#
# Given the last Q+A pair, return up to 3 short follow-up prompts the user
# is likely to ask next. Used by Chat.jsx to render clickable chips after
# every assistant message — mirrors Claude/ChatGPT suggestion behaviour.
#
# Uses Claude Haiku (cheap, fast) via the model router. Returns silently on
# any error so the UI never blocks on a failed suggestion call.
# ============================================================

class _FollowupReq(BaseModel):
    question: str
    answer:   str


@_v1.post("/chat/followups", tags=["ai"])
async def chat_followups(body: _FollowupReq, authorization: Optional[str] = _Header(default=None)):
    import json as _json
    if not body.question or not body.answer:
        return {"followups": []}
    try:
        _system = (
            "You suggest short follow-up questions a curious user might ask next, "
            "given the previous Q&A. Output ONLY a JSON array of 2-3 strings, no prose. "
            "Each suggestion must be a single sentence, under 12 words, conversational. "
            "Skip suggestions that the prior answer already covers. "
            "Return [] if nothing meaningful to suggest."
        )
        _prompt = (
            f"{_system}\n\n"
            f"USER QUESTION:\n{body.question[:600]}\n\n"
            f"ASSISTANT ANSWER:\n{body.answer[:1500]}\n\n"
            f"Follow-up JSON array:"
        )
        # Route through the Rust runtime first (via /v1/chat/completions).
        # The Python model_router path calls /llm/generate on the LLM proxy,
        # which is empty in local dev. Use a dedicated followups session so
        # it doesn't pollute the user's chat history.
        _raw = ""
        try:
            from core.runtime_client import ENABLE_RUNTIME as _RT_ON_FU, RUNTIME_PCT as _RT_PCT_FU
            if _RT_ON_FU and _RT_PCT_FU > 0:
                from core.runtime_client import chat_stream_sync as _rt_fu_sync
                _fu_session = f"followups-{uuid.uuid4()}"
                _fu_turn = f"followups-t1"
                _fu_chunks = []
                for _tok in _rt_fu_sync(
                    session=_fu_session,
                    turn=_fu_turn,
                    message=_prompt,
                    data_class="internal",
                    caps=["chat.send"],
                ):
                    _fu_chunks.append(_tok)
                _raw = "".join(_fu_chunks)
        except Exception:
            _raw = ""
        # Fallback: use the Python model_router (works when LLM_PROXY_URL points
        # at a real proxy service, or when API keys are configured for direct calls).
        if not _raw:
            from models.model_router import model_router as _mr_fu
            _raw = _mr_fu.generate(_prompt, model_hint="haiku")
        # Guard against error strings — model_router.generate returns "Error: …" on failure.
        if _raw and isinstance(_raw, str) and _raw.strip().lower().startswith("error"):
            _raw = ""
        _txt = (_raw or "").strip()
        # Strip code fences if the model wrapped the array in ```json … ```
        if _txt.startswith("```"):
            _txt = _txt.strip("`")
            if _txt.lower().startswith("json"):
                _txt = _txt[4:].strip()
        # Extract the JSON array if there's surrounding prose
        _lb = _txt.find("[")
        _rb = _txt.rfind("]")
        if _lb >= 0 and _rb > _lb:
            _txt = _txt[_lb:_rb + 1]
        _arr = _json.loads(_txt)
        if not isinstance(_arr, list):
            return {"followups": []}
        _out = [str(s).strip() for s in _arr if str(s).strip()][:3]
        return {"followups": _out}
    except Exception as _fu_err:
        logger.debug(f"followups endpoint failed (returning empty): {_fu_err}")
        return {"followups": []}

# ============================================================
# MAIN ASK ENDPOINT
# ============================================================

def _cowork_memory_block(user_id: str) -> str:
    """Render the user's Cowork personalization prefs (signature, tone, doc format,
    aliases) as a system-prompt block. Empty string if none. Wired into office mode."""
    try:
        from memory.cowork_memory import build_memory_prompt
        snippet = build_memory_prompt(user_id) or ""
        return f"[USER PREFERENCES]\n{snippet}\n\n" if snippet.strip() else ""
    except Exception:
        return ""

@_v1.post("/ask", tags=["ai"])
async def ask_ai(q: Question, request: Request, authorization: Optional[str] = _Header(default=None)):

    # Prefer client-supplied x-client-request-id for end-to-end tracing
    request_id = (request.headers.get("x-client-request-id") or "").strip() or str(uuid.uuid4())

    set_request_id(request_id)
    # Unconditionally overwrite — do NOT use bind_context() here, whose
    # `or getattr(...)` guard would keep a stale correlation_id left on this
    # reused worker thread by a previous request whose finally-block cleanup
    # ran on a different (anyio streaming) thread.
    set_correlation_id(request_id)

    logger.info(f"[ask] request start | corr={request_id}")

    start_time = time.time()

    # ── Wave 1 shadow-capture (PIPELINE_V2, default OFF; written-never-read) ──
    _rc = None
    if _PIPELINE_V2:
        _rc = RequestContext(request_id=request_id, start_time=start_time)
        _otel.record_event("stage.tracing", request_id=request_id)

    # ========================================================
    # FIX 5: PLATFORM KILL-SWITCH (admin governance)
    # Set Redis key "platform:disabled" = "1" to suspend all /ask requests.
    # ========================================================
    try:
        _ks = redis_client.get("platform:disabled")
        if _ks == "1":
            from fastapi.responses import JSONResponse as _JR_ks
            return _JR_ks(
                status_code=503,
                content={"error": "platform_disabled",
                         "detail": "Platform is temporarily suspended by administrator. Contact your admin."},
            )
    except Exception:
        pass

    # ── Context isolation: reject contradictory Generic + repo/project ────
    # A client cannot send rag_mode="off" (Generic) while also specifying
    # a repo_filter or project_id. This prevents a malicious client from
    # bypassing read-side isolation by smuggling repo context into a
    # nominally Generic request.
    if (q.rag_mode or "").strip().lower() == "off" and (q.repo_filter or q.project_id):
        return _JR_ks(
            status_code=400,
            content={"error": "invalid_request",
                     "detail": "rag_mode='off' (Generic) cannot be combined with repo_filter or project_id."},
        )

    if _PIPELINE_V2 and _rc is not None:
        _rc.rag_mode = (q.rag_mode or "off").strip().lower()

    # Resolve identity: JWT (browser/CLI) → API key (IDE) → 401.
    # No anonymous fallback — every request must be authenticated.
    _user_id = None
    _jwt_token = None
    if authorization and authorization.lower().startswith("bearer "):
        _jwt_token = authorization[7:].strip()
    else:
        _jwt_token = request.cookies.get("auth_token")

    _user_dept = ""
    _user_ctx: dict = {}

    if _jwt_token:
        # 1. Try JWT
        # Note: "email" and "department" are no longer in the JWT (DAST fix — PII removed).
        # "department" is fetched from the server-side profile cache via enrich_user_context().
        try:
            from auth.jwt_handler import decode_token as _decode
            from auth.dependencies import enrich_user_context as _enrich
            _payload = _decode(_jwt_token)
            if _payload:
                _payload = _enrich(_payload)   # adds email, name, department from DB/cache
                _user_id = _payload.get("sub")
                _user_dept = _payload.get("department", "") or ""
                _user_ctx = {
                    "user_id":    _user_id,
                    "user_role":  _payload.get("role", "user"),
                    "ad_level":   int(_payload.get("ad_level") or 6),
                    "department": _user_dept,
                    "is_admin":   _payload.get("role") == "admin",
                    "can_approve": bool(_payload.get("can_approve", False)),
                    "org_id":     _payload.get("org_id", ""),
                    "session_id": "",
                    "name":       _payload.get("name", ""),
                    "ad_username": _payload.get("ad_username", ""),
                    "email":      _payload.get("email", ""),
                }
                if _PIPELINE_V2 and _rc is not None:
                    _rc.auth_method = "jwt"
        except Exception:
            pass

        # 2. JWT failed — try platform API key (IDE integrations: Kilo Code, Cursor, etc.)
        if not _user_id:
            try:
                from auth.api_key_auth import is_api_key as _is_api_key, resolve_api_key as _resolve_key
                if _is_api_key(_jwt_token):
                    _kp = _resolve_key(_jwt_token)
                    if _kp:
                        _user_id   = _kp["sub"]
                        _user_dept = _kp.get("department", "") or ""
                        _user_ctx  = {
                            "user_id":    _user_id,
                            "user_role":  _kp.get("role", "user"),
                            "ad_level":   int(_kp.get("ad_level") or 6),
                            "department": _user_dept,
                            "is_admin":   _kp.get("role") == "admin",
                            "can_approve": bool(_kp.get("can_approve", False)),
                            "org_id":     _kp.get("org_id", ""),
                            "session_id": "",
                            "name":       _kp.get("name", ""),
                            "ad_username": _kp.get("ad_username", ""),
                            "email":      _kp.get("email", ""),
                        }
                        if _PIPELINE_V2 and _rc is not None:
                            _rc.auth_method = "api_key"
            except Exception:
                pass

    # 3. No valid auth — reject. No anonymous access to cloud models.
    if not _user_id:
        from fastapi.responses import JSONResponse as _JR_auth
        return _JR_auth(
            status_code=401,
            content={"error": "unauthorized", "detail": "Valid JWT or platform API key required."},
        )

    # ── CLI detection — computed ONCE here, reused throughout /ask ─────────
    # True when the request comes from ainxt-cli (X-AiNxt-Client: cli/*)
    # or has cli_mode=True set by the web frontend's CLI-mode toggle.
    # Avoids three separate header/attr reads scattered across the handler.
    _is_cli = (
        getattr(q, 'cli_mode', False)
        or getattr(request.state, "client_source", "platform") == "cli"
        or request.headers.get("x-ainxt-client", "").lower().startswith("cli")
    )

    # ── Runtime canary detection — computed ONCE, reused by all skip checks ──
    # When the Rust runtime is enabled AND this user is in the canary cohort, the
    # runtime handles classification + routing + RAG internally. The Python-side
    # preprocessing (CIL, classify_query_complexity, KB reranker) is redundant —
    # it calls /llm/generate on the LLM proxy (which 404s on the runtime path)
    # and its results are discarded by the canary block.
    # IMPORTANT: when ENABLE_RUNTIME=false or RUNTIME_PCT=0, this is False and
    # the full Python preprocessing pipeline runs normally (fallback path).
    _runtime_will_handle = False
    try:
        from core.runtime_client import (
            ENABLE_RUNTIME as _RT_ON_EARLY, RUNTIME_PCT as _RT_PCT_EARLY,
            user_in_canary as _user_in_canary_early,
        )
        _runtime_will_handle = (
            _RT_ON_EARLY
            and _RT_PCT_EARLY > 0
            and _user_in_canary_early(_user_id or "anon", _RT_PCT_EARLY)
        )
    except Exception:
        pass

    # ── Wave 1 shadow-capture: identity converged, resolve policy ──────────
    if _PIPELINE_V2 and _rc is not None:
        _rc.user_id = _user_id
        _rc.user_dept = _user_dept
        _rc.user_ctx = dict(_user_ctx)
        _rc.policy = _POLICY_RESOLVER.resolve(user_ctx=_rc.user_ctx)
        # Skip the intent model round-trip when the user has FORCED a specific
        # model in the UI (q.model is not auto/default) — routing is already
        # decided for plain chat, so the extra latency buys nothing.
        #
        # EXCEPTION: never skip when there is a doc/image/video generation
        # signal. Even if the user picked a specific chat model, they may still
        # want "generate a PDF", "improve this image", or "make a video" — the
        # CIL is the ONLY thing that detects those intents and routes to the
        # right generator. Skipping it here means those requests silently fall
        # through to plain chat regardless of what the user asked for.
        #
        # The signal pre-checks (_cil_has_doc_signal, _cil_has_img_signal,
        # _cil_has_vid_signal) are computed just below; we re-evaluate them
        # inline here using the same lightweight logic so we can decide before
        # the full pre-check block runs. This is intentionally conservative —
        # false positives (running CIL when not needed) cost one small-model
        # call; false negatives (skipping CIL when needed) silently break
        # doc/image/video generation for users who have a model selected.
        _q_lower = (q.question or "").lower()
        _has_att_early = bool(getattr(q, "attachment_ids", None))
        # Any attachment OR any generation-flavoured keyword → must run CIL.
        _has_generation_signal = (
            _has_att_early
            or any(kw in _q_lower for kw in (
                "pdf", "docx", "word", "pptx", "powerpoint", "excel", "xlsx",
                "spreadsheet", "csv", "report", "document", "presentation",
                "draw", "image", "picture", "photo", "logo", "icon", "poster",
                "banner", "illustration", "render", "sketch", "paint", "depict",
                "improve", "enhance", "redesign", "modernise", "modernize",
                "restyle", "variation", "generate", "create", "make", "produce",
                "video", "clip", "reel", "animation", "animate", "film",
            ))
        )
        # Also skip when the Rust runtime will handle this turn (canary path):
        # the runtime does its own intent classification internally, so the CIL
        # LLM call is redundant. Generation signal check still applies — if the
        # user wants doc/image/video, CIL must run even on the runtime path.
        _skip_intent_model = bool(
            (q.model or "").strip()
            and (q.model or "").lower().strip() not in ("auto", "default", "")
            and not _has_generation_signal
        ) or (_runtime_will_handle and not _has_generation_signal)
        # CIL analysis is deferred until after chat_id is resolved (see below)
        # so that document-intent context (attachments, prior docs, recent turns)
        # can be passed in a single local-LLM call.
        _rc.conv_state = None

    # Populate product_ids from dept_product_mappings — cached in Redis db=0
    # for 1 hour. Department-product membership changes only via admin CSV
    # upload — 5-minute TTL forced a Postgres round-trip on most requests.
    # The CSV upload endpoint should invalidate this key on write
    # (POST /admin/sync/org-tree).
    if _user_dept and _user_ctx:
        try:
            _pid_cache_key = f"dept:pids:{_user_dept}"
            _cached_pids = redis_client.get(_pid_cache_key)
            if _cached_pids:
                _user_ctx["product_ids"] = json.loads(_cached_pids)
            else:
                from db.database import SessionLocal as _PidPgSession
                from sqlalchemy import text as _pid_sql
                _pid_sess = _PidPgSession()
                try:
                    _pid_rows = _pid_sess.execute(
                        _pid_sql("SELECT product_id::text FROM dept_product_mappings WHERE department = :dept"),
                        {"dept": _user_dept},
                    ).fetchall()
                    _pids = [r[0] for r in _pid_rows]
                    _user_ctx["product_ids"] = _pids
                    redis_client.setex(_pid_cache_key, 3600, json.dumps(_pids))
                finally:
                    _pid_sess.close()
        except Exception:
            _user_ctx["product_ids"] = []

    # Resolve chat_id — use provided or generate a new one for this conversation.
    # CLI sends session_id to keep context across turns; honour it when chat_id
    # is not provided so Coach grouping works for CLI sessions.
    _chat_id = q.chat_id or q.session_id or str(uuid.uuid4())
    set_chat_context(_user_id, _chat_id)
    set_span_id("gateway.ask")

    # ── Wave 1b: CIL understanding (now that chat_id is known) ─────────────
    # For Auto model selection, run ONE local-LLM call that classifies both
    # conversation intent/task_complexity AND document-generation intent.
    # The doc-intent context (attachments, prior docs, recent turns) is gathered
    # here and passed into the same classifier, letting us skip the separate
    # models/doc_intent.py LLM call later.
    #
    # PERF: Compute artifact signal BEFORE CIL so we can conditionally include
    # doc-intent schema fields only when the prompt contains a document signal.
    # Plain-chat prompts get a shorter CIL prompt (no doc-intent fields), saving
    # tokens on the local LLM.
    import re as _re_cil_pre
    _CIL_DOC_SLASH_RE = _re_cil_pre.compile(
        r"^/(?:pdf|docx?|word|pptx?|pptagent|xlsx?|excel|csv|txt|text|md|convert)\b",
        _re_cil_pre.IGNORECASE,
    )
    _CIL_DOC_FORMAT_NOUN_RE = _re_cil_pre.compile(
        r"\b(?:pdfs?|docx?s?|word\s+doc(?:ument)?s?|documents?|pptx?s?|powerpoints?|presentations?|"
        r"xlsx?s?|excels?|spreadsheets?|csvs?|markdowns?|\.md)\b",
        _re_cil_pre.IGNORECASE,
    )
    def _cil_has_doc_signal(text: str, has_att: bool, doc_mem: str) -> bool:
        if has_att:
            return True
        if doc_mem:
            return True
        if _CIL_DOC_SLASH_RE.search(text):
            return True
        if _CIL_DOC_FORMAT_NOUN_RE.search(text):
            return True
        return False

    _cil_doc_mem = ""
    try:
        from services.doc_context import list_docs_for_chat as _ldfc_cil
        _cil_doc_mem = _ldfc_cil(_chat_id, _user_id).summary_for_llm()
    except Exception:  # noqa: BLE001
        _cil_doc_mem = ""
    _cil_has_att = bool(getattr(q, "attachment_ids", None))
    _cil_has_doc_signal = _cil_has_doc_signal(q.question or "", _cil_has_att, _cil_doc_mem)

    # Image-intent schema is always included in the CIL prompt so the LLM can
    # classify any image-generation request regardless of phrasing (e.g.
    # "generate elephant image", "cat photo", "draw a tiger").
    # The routing gate (img_confidence > 0.5) is the real safety net — the
    # regex pre-check was a token-saving optimisation that caused misses for
    # short/subject-noun prompts and has been removed.

    # ── Video signal pre-check (parallel to image signal pre-check above) ────
    # Always True — the CIL LLM decides whether vid_intent is "generate" or
    # "none" based on the full prompt. The routing gate (vid_confidence > 0.5)
    # is the real safety net against false positives.
    # The regex pre-check was removed for the same reason as the image one:
    # it caused misses for natural phrasings like "make a video of a 60-year-old
    # woman" and any other phrasing the regex didn't anticipate.

    # Fetch the `kind` column for each attachment so we can distinguish image
    # attachments (kind="image") from document attachments (kind="document"/etc.).
    # Used later in the routing block.
    _cil_att_kinds: list = []
    if _cil_has_att:
        try:
            from db.database import SessionLocal as _CilAttDB
            from db.models import ChatAttachment as _CilAttCA
            _cil_att_db = _CilAttDB()
            try:
                _cil_att_kinds = [
                    str(row.kind or "")
                    for row in _cil_att_db.query(_CilAttCA.kind).filter(
                        _CilAttCA.id.in_(list(getattr(q, "attachment_ids", None) or []))
                    ).all()
                ]
            finally:
                _cil_att_db.close()
        except Exception as _cil_att_kind_err:  # noqa: BLE001
            logger.debug(f"[cil] att-kind fetch failed (non-fatal): {_cil_att_kind_err}")

    # Always True — the CIL LLM decides whether img_intent is "generate" or
    # "none" based on the full prompt. The routing gate (img_confidence > 0.5)
    # is the real safety net against false positives.
    _cil_has_img_signal = True

    # Always True — see comment above (_CIL_VID_KEYWORD_RE removed).
    _cil_has_vid_signal = True

    _cil_probe = []
    _cil_has_chat_ctx = False
    try:
        from memory.redis_memory import RedisMemory as _RMProbeCil
        _raw_probe_cil = _RMProbeCil().get_conversation(_chat_id, limit=5) or []
        _cil_probe = [
            e for e in _raw_probe_cil
            if e.get("role") in ("user", "assistant")
            and str((e.get("metadata") or {}).get("rag_mode") or "off") == "off"
        ]
        _cil_has_chat_ctx = bool(_cil_probe)
    except Exception:  # noqa: BLE001
        _cil_has_chat_ctx = False

    if _PIPELINE_V2 and _rc is not None and _rc.conv_state is None:
        _rc.conv_state = _cil_analyze(
            q.question or "",
            rag_mode=_rc.rag_mode,
            skip_model=_skip_intent_model,
            has_attachments=_cil_has_att,
            doc_memory_summary=_cil_doc_mem,
            has_chat_context=_cil_has_chat_ctx,
            recent_turns=_cil_probe,
            include_doc_intent=_cil_has_doc_signal,
            include_img_intent=_cil_has_img_signal,
            include_vid_intent=_cil_has_vid_signal,
            # Attachment KIND only (e.g. ["image"]) — never content. Lets the
            # classifier say "Attachments present: yes (kind: image)" instead
            # of a bare yes/no, without ever seeing parsed_text/
            # image_description/image_caption. See cil/intent.py::classify().
            attachment_kinds=_cil_att_kinds,
        )
        logger.info(
            f"[CIL] request_id={request_id!r} chat_id={_chat_id!r} "
            f"task_complexity={_rc.conv_state.task_complexity!r} "
            f"intent={_rc.conv_state.intent!r} "
            f"domain={_rc.conv_state.domain!r} "
            f"wants_brief={_rc.conv_state.wants_brief} "
            f"doc_intent={_rc.conv_state.doc_intent!r} "
            f"doc_format={_rc.conv_state.doc_format!r} "
            f"doc_confidence={_rc.conv_state.doc_confidence:.2f} "
            f"img_intent={_rc.conv_state.img_intent!r} "
            f"img_source_scope={_rc.conv_state.img_source_scope!r} "
            f"img_confidence={_rc.conv_state.img_confidence:.2f} "
            f"vid_intent={_rc.conv_state.vid_intent!r} "
            f"vid_source_scope={_rc.conv_state.vid_source_scope!r} "
            f"vid_confidence={_rc.conv_state.vid_confidence:.2f} "
            f"analyze_ms={_rc.conv_state.analyze_ms} "
            f"skip_model={_skip_intent_model} "
            f"user_model={q.model!r}"
        )
        _otel.record_event("stage.identity", user_id=_user_id, auth=_rc.auth_method)
        _otel.record_event("stage.cil", **_rc.conv_state.snapshot())

    if _PIPELINE_V2 and _rc is not None:
        _rc.product_ids = list(_user_ctx.get("product_ids", []))
        _rc.chat_id = _chat_id
        _otel.record_event("stage.products", product_id_count=len(_rc.product_ids))

    # ── DOCUMENT-INTENT ROUTING (backend authority, ONE call, NO regex) ───────
    # Every non-ephemeral prompt is classified by the SMALL local model
    # (models.doc_intent.classify → DOC_INTENT_MODEL). If it's a document
    # request, we enqueue the platform skillset generation job and return a
    # {route:"doc"} signal INSTEAD of streaming a prose answer — so "create a
    # ppt on X" reliably produces a document (with live updates) rather than a
    # chat reply. Non-doc prompts fall through to normal chat below.
    # Skipped for: the classifier's own ephemeral call, CLI/agent turns (Buddy
    # owns its own doc path), and image-capable model selections handled later.
    #
    # PERF: Fast artifact-signal pre-check — skip the LLM entirely when the
    # prompt contains no document signal at all. The LLM call costs ~6–7 s on
    # every plain-chat request and is immediately vetoed when there is no signal.
    # Signals: slash command, format noun, uploaded attachment, prior doc session.
    import re as _re_doc_pre
    _DOC_SLASH_RE = _re_doc_pre.compile(
        r"^/(?:pdf|docx?|word|pptx?|pptagent|xlsx?|excel|csv|txt|text|md|convert)\b",
        _re_doc_pre.IGNORECASE,
    )
    _DOC_FORMAT_NOUN_RE = _re_doc_pre.compile(
        r"\b(?:pdfs?|docx?s?|word\s+doc(?:ument)?s?|documents?|pptx?s?|powerpoints?|presentations?|"
        r"xlsx?s?|excels?|spreadsheets?|csvs?|markdowns?|\.md)\b",
        _re_doc_pre.IGNORECASE,
    )

    def _has_doc_artifact_signal(text: str, has_att: bool, doc_mem: str) -> bool:
        """Return True when at least one artifact signal is present in the request.
        When False, the doc_intent LLM call is skipped entirely — the prompt is
        guaranteed to be a plain-chat request and no document routing is needed."""
        if has_att:
            return True
        if doc_mem:
            return True
        if _DOC_SLASH_RE.search(text):
            return True
        if _DOC_FORMAT_NOUN_RE.search(text):
            return True
        return False

    if (not q.ephemeral
            and (q.question or "").strip()
            and not _is_cli):
        try:
            from models.doc_intent import classify as _doc_classify, DocIntent as _DocIntent
            _has_att = bool(getattr(q, "attachment_ids", None))
            _doc_mem = ""
            try:
                from services.doc_context import list_docs_for_chat as _ldfc
                _doc_mem = _ldfc(_chat_id, _user_id).summary_for_llm()
            except Exception:  # noqa: BLE001
                _doc_mem = ""

            # ── PIPELINE_V2: reuse CIL doc-intent (single LLM call) ────────────
            # When CIL already ran and classified doc-intent, skip the separate
            # models/doc_intent.py LLM call entirely — the result is already in
            # conv_state. Only applies when:
            #   • CIL was not skipped (_skip_intent_model=False), so conv_state
            #     was populated by the model (not the safe static default).
            #   • The doc-signal gate was open (_cil_has_doc_signal=True), so
            #     the CIL prompt included the doc-intent schema fields.
            #   • The model actually produced a result (doc_reason="model"), not
            #     the static default ("default").
            # When any condition is False, fall through to _doc_classify() below.
            _use_cil_doc_intent = (
                _PIPELINE_V2
                and _rc is not None
                and _rc.conv_state is not None
                and not _skip_intent_model
                and _cil_has_doc_signal
                and _rc.conv_state.doc_reason == "model"
            )
            if _use_cil_doc_intent:
                _cs = _rc.conv_state
                _di = _DocIntent(
                    intent=_cs.doc_intent,
                    format=_cs.doc_format,
                    source_scope=_cs.doc_source_scope,
                    target_artifact_id=_cs.doc_target_artifact_id,
                    is_doc=(_cs.doc_intent != "none"),
                    needs_topic=_cs.doc_needs_topic,
                    topic=_cs.doc_topic,
                    confidence=_cs.doc_confidence,
                    reason="cil_merged",
                )
                # Reuse the chat-context probe already gathered for CIL so the
                # docgen route has _has_chat_ctx / _probe available for
                # summarize/convert/extract chat-source documents.
                _has_chat_ctx = _cil_has_chat_ctx
                _probe = list(_cil_probe)
                _doc_intent_ms = 0.0
                logger.info(
                    "[doc_intent] using CIL merged result — skipping separate LLM call "
                    f"request_id={request_id!r} chat_id={_chat_id!r}"
                )
            # ── Fast pre-check: skip LLM when no artifact signal present ──────
            # Plain-chat questions (no slash command, no format noun, no file,
            # no prior doc) are guaranteed non-doc — no LLM call needed.
            elif not _has_doc_artifact_signal(q.question, _has_att, _doc_mem):
                logger.info(
                    "[doc_intent] no artifact signal detected — skipping LLM classifier "
                    f"(fast-path)"
                )
                _di = _DocIntent(is_doc=False, intent="none", confidence=1.0,
                                 reason="no_artifact_signal_fast_path")
                _has_chat_ctx = False
                _probe = []
                _doc_intent_ms = 0.0
            else:
                _has_chat_ctx = False
                _probe = []  # recent chat-origin turns; stays [] if the probe fails
                try:
                    from memory.redis_memory import RedisMemory as _RMProbe
                    _raw_probe = _RMProbe().get_conversation(_chat_id, limit=5) or []
                    _probe = [
                        e for e in _raw_probe
                        if e.get("role") in ("user", "assistant")
                        and str((e.get("metadata") or {}).get("rag_mode") or "off") == "off"
                    ]
                    _has_chat_ctx = bool(_probe)
                    if not _probe:
                        from db.database import SessionLocal as _ProbeDB
                        from db.models import ChatMessage as _ProbeCM
                        _pdb = _ProbeDB()
                        try:
                            _has_chat_ctx = _pdb.query(_ProbeCM.id).filter(
                                _ProbeCM.chat_id == _chat_id,
                                _ProbeCM.role.in_(["user", "assistant"]),
                                _ProbeCM.rag_mode == "off",
                            ).first() is not None
                        finally:
                            _pdb.close()
                except Exception:  # noqa: BLE001
                    _has_chat_ctx = False
                _doc_intent_t0 = time.time()
                _di = _doc_classify(
                    q.question, has_attachments=_has_att,
                    doc_memory_summary=_doc_mem, chat_id=_chat_id, user_id=_user_id,
                    has_chat_context=_has_chat_ctx,
                    recent_turns=_probe,
                )
                _doc_intent_ms = round((time.time() - _doc_intent_t0) * 1000, 2)
            logger.info(
                f"[doc_intent] request_id={request_id!r} chat_id={_chat_id!r} "
                f"intent={_di.intent!r} format={_di.format!r} "
                f"is_doc={_di.is_doc} confidence={_di.confidence:.2f} "
                f"reason={_di.reason!r} elapsed_ms={_doc_intent_ms} "
                f"artifact_signal={_has_doc_artifact_signal(q.question, _has_att, _doc_mem)}"
            )
            if _PIPELINE_V2 and _rc is not None:
                # read-only copy; must not perturb the doc early-return below
                _rc.doc_intent = getattr(_di, "raw", None) or dict(_di.__dict__)
                _otel.record_event("stage.intent", is_doc=bool(_di.is_doc), intent=_di.intent)
            # ── Unsupported downloadable format → answer in chat ─────────────
            # The user explicitly asked to CREATE a file whose type we cannot
            # build as a downloadable document (source/config/script files like
            # .py, .sql, .sh, .yaml, Dockerfile, …). The classifier already set
            # intent='none' so this request falls through to a normal chat
            # answer below; here we prepend a small instruction so the answering
            # model TELLS the user we can't produce that as a downloadable file
            # and returns the requested content inline in a fenced code block
            # (matches the "give a chat response with the requested content"
            # behaviour). No hardcoded format list — _di.unsupported_format was
            # decided by the intent LLM.
            _unsupported_fmt = getattr(_di, "unsupported_format", "") or ""
            if (not _di.is_doc) and _unsupported_fmt:
                logger.info(
                    f"[ask] unsupported downloadable format {_unsupported_fmt!r} "
                    f"→ answering in chat with code block"
                )
                q.question = (
                    "[SYSTEM NOTE — follow exactly] The user asked to generate a "
                    f".{_unsupported_fmt} file, but the document generator can only "
                    "produce pdf, docx, pptx, xlsx, csv, md and txt files. Do NOT "
                    "attempt to create a downloadable file. Instead, briefly tell "
                    f"the user you can't generate a downloadable .{_unsupported_fmt} "
                    "file, then provide the full requested content directly in your "
                    "reply inside a single fenced code block with the correct "
                    "language tag so they can copy it.\n\n"
                    f"USER REQUEST:\n{q.question}"
                )
            # ── Ask, don't fabricate: a doc request with NO topic ────────────
            # If the user clearly wants a document but named no subject (and there
            # is nothing attached / no prior doc to work from), ASK what it should
            # be about instead of generating a useless "general-purpose" filler
            # document (which wasted ~$0.12 + 2 min). Zero-cost streamed clarify —
            # no generation job, no LLM answer call.
            if _di.is_doc and getattr(_di, "needs_topic", False):
                from fastapi.responses import StreamingResponse as _SR_clar
                _fmt_word = {
                    "pdf": "PDF", "docx": "Word document", "pptx": "presentation",
                    "xlsx": "spreadsheet", "csv": "CSV", "md": "markdown doc",
                    "txt": "text file",
                }.get((_di.format or "pdf"), "document")
                _clar_q = (
                    f"Happy to make that {_fmt_word} — what should it be about? "
                    "A quick line on the topic (and any sections, audience, or "
                    "length you want) and I'll put it together."
                )
                logger.info(f"[ask] doc-intent needs topic → clarifying (no job) | fmt={_di.format}")
                if _PIPELINE_V2 and _rc is not None:
                    _rc.dispatch = _DispatchDecision(lane=_Lane.DOC_ROUTE, reason="needs_topic_clarify")
                    _otel.record_event("dispatch", lane="doc_clarify")

                # Save the clarify exchange to Redis so the NEXT turn's
                # recent_turns probe sees the full context:
                #   user: "generate a doc"
                #   assistant: "Happy to make that … what should it be about?"
                # Without this, the clarifying question is a ghost — it exists
                # only in the UI. The doc-intent classifier on the next turn
                # sees only the user's prior message and misreads the user's
                # topic answer as a standalone chat message, routing to chat
                # instead of doc-gen. Fire-and-forget; never blocks the stream.
                try:
                    from memory.redis_memory import RedisMemory as _RM_clar
                    _rms_clar = _RM_clar()
                    _clar_meta = {"rag_mode": _rag_mode, "source": "doc_clarify"}
                    _rms_clar.save_message(_chat_id, "user", q.question, metadata=_clar_meta)
                    _rms_clar.save_message(_chat_id, "assistant", _clar_q, metadata=_clar_meta)
                    logger.info(
                        f"[ask] doc-clarify exchange saved to Redis | "
                        f"chat={_chat_id} fmt={_di.format}"
                    )
                except Exception as _clar_mem_err:
                    logger.debug(f"[ask] doc-clarify Redis save skipped: {_clar_mem_err}")

                def _doc_clarify_stream(_msg=_clar_q, _cid=_chat_id):
                    yield "data: " + json.dumps({"t": _msg}) + "\n\n"
                    yield "data: " + json.dumps({"__meta__": {
                        "out_tok": 0, "in_tok": 0, "model": "doc-clarify",
                        "cost": 0.0, "latency": 0.0, "source": "doc_clarify",
                        "llm_used": False, "chat_id": _cid,
                    }}) + "\n\n"
                return _SR_clar(_doc_clarify_stream(), media_type="text/event-stream")

            # ── P7 Ambiguity gate: bare deictic after multi-topic chat ────────
            # "give me this in pdf format" after Java/JS/UPI answers is ambiguous.
            # When scope=chat, the user used a bare deictic ("this", "that", "it")
            # without naming a specific topic, AND the conversation has multiple
            # distinct topics, ask which one they mean instead of guessing.
            if (
                _di.is_doc
                and getattr(_di, "source_scope", "none") == "chat"
                and not getattr(_di, "needs_topic", False)
                and not (getattr(_di, "topic", "") or "").strip()
                and _has_chat_ctx
                and _cil_probe
            ):
                import re as _re_deictic
                _bare_deictic = bool(_re_deictic.search(
                    r'\b(this|that|it|itha|ithine|iske|ithil|ithine|intha)\b',
                    q.question, _re_deictic.IGNORECASE
                ))
                _has_own_topic = bool(_re_deictic.search(
                    r'\b[A-Z][a-z]{3,}\b', q.question
                ) or _re_deictic.search(
                    r'\b(java|python|javascript|js|upi|history|science|math|'
                    r'finance|health|climate|global\s+warming|machine\s+learning|'
                    r'ai|blockchain|crypto)\b',
                    q.question, _re_deictic.IGNORECASE
                ))
                if _bare_deictic and not _has_own_topic:
                    # Count distinct user-initiated topics in recent turns
                    _user_turns = [
                        t for t in _cil_probe if t.get("role") == "user"
                    ]
                    _non_followup = [
                        t for t in _user_turns
                        if not (t.get("content") or "").lower().strip().startswith(
                            ("and ", "also ", "what about", "how about", "ok ", "okay ",
                             "yes", "no ", "sure", "thanks", "thank you")
                        )
                    ]
                    if len(_non_followup) > 1:
                        # Multiple distinct topics — ask which one
                        from fastapi.responses import StreamingResponse as _SR_amb
                        _topic_labels = []
                        for _ut in _non_followup[-5:]:  # last 5 distinct questions
                            _utxt = (_ut.get("content") or "").strip()
                            if _utxt and len(_utxt) < 120:
                                _topic_labels.append(_utxt[:80])
                        _amb_candidates = [
                            {"label": _tl, "value": _tl} for _tl in _topic_labels
                        ]
                        _amb_candidates.append(
                            {"label": "All of the above (entire conversation)", "value": "__all__"}
                        )
                        logger.info(
                            f"[ask] doc-intent P7 ambiguity gate fired — "
                            f"bare deictic + {len(_non_followup)} topics | "
                            f"intent={_di.intent} fmt={_di.format}"
                        )
                        if _PIPELINE_V2 and _rc is not None:
                            _rc.dispatch = _DispatchDecision(
                                lane=_Lane.DOC_ROUTE, reason="deictic_ambiguity_clarify"
                            )

                        # Save the deictic-ambiguity clarify exchange to Redis
                        # so the next turn's recent_turns probe sees the full
                        # context (user's deictic + our "which topic?" question).
                        # Same ghost-turn fix as the needs_topic clarify above.
                        try:
                            from memory.redis_memory import RedisMemory as _RM_amb
                            _rms_amb = _RM_amb()
                            _amb_clar_msg = (
                                f"I see we discussed several topics. "
                                f"Which one should I create the document about?"
                            )
                            _amb_meta = {"rag_mode": _rag_mode, "source": "deictic_clarify"}
                            _rms_amb.save_message(_chat_id, "user", q.question, metadata=_amb_meta)
                            _rms_amb.save_message(_chat_id, "assistant", _amb_clar_msg, metadata=_amb_meta)
                            logger.info(
                                f"[ask] deictic-clarify exchange saved to Redis | "
                                f"chat={_chat_id} topics={len(_amb_candidates)}"
                            )
                        except Exception as _amb_mem_err:
                            logger.debug(f"[ask] deictic-clarify Redis save skipped: {_amb_mem_err}")

                        def _amb_clarify_stream(_cands=_amb_candidates, _cid=_chat_id,
                                                _fmt=(_di.format or "pdf")):
                            _fmt_word = {
                                "pdf": "PDF", "docx": "Word document", "pptx": "presentation",
                                "xlsx": "spreadsheet", "csv": "CSV", "md": "markdown doc",
                                "txt": "text file",
                            }.get(_fmt, "document")
                            yield "data: " + json.dumps({
                                "__clarify__": {
                                    "question": f"Which topic should the {_fmt_word} cover?",
                                    "message": (
                                        f"I see we discussed several topics. "
                                        f"Which one should I create the {_fmt_word} about?"
                                    ),
                                    "candidates": _cands,
                                    "multi_select": False,
                                }
                            }) + "\n\n"
                            yield "data: " + json.dumps({"__meta__": {
                                "out_tok": 0, "in_tok": 0, "model": "doc-clarify",
                                "cost": 0.0, "latency": 0.0, "source": "deictic_clarify",
                                "llm_used": False, "chat_id": _cid,
                            }}) + "\n\n"
                        return _SR_amb(_amb_clarify_stream(), media_type="text/event-stream")

            if _di.is_doc and _di.intent != "none":
                from fastapi.responses import JSONResponse as _JR_doc
                from routers.doc_download_router import (
                    enqueue_doc_job as _enqueue_doc,
                    doc_marker_for  as _doc_marker_for,
                )
                import uuid as _uuid_doc

                # ── Single-prompt → MULTIPLE documents ────────────────────────
                # _di.docs is the per-deliverable breakdown from the classifier
                # (≥1 entry for any doc request, capped at 3). Fallback to a
                # single entry synthesised from the scalar fields for robustness.
                _docs = list(getattr(_di, "docs", None) or [])
                if not _docs:
                    _docs = [{"intent": _di.intent,
                              "format": (_di.format or "pdf"),
                              "topic":  (getattr(_di, "topic", "") or "")}]

                _att_ids   = list(getattr(q, "attachment_ids", None) or [])
                _model_hint = (getattr(q, "model", None) or "auto")

                # ── Ownership gate (IDOR guard) ──────────────────────────────
                _caller_role = str((_user_ctx or {}).get("role") or getattr(q, "role", "") or "").lower()
                _chat_owned = True
                try:
                    from db.database import SessionLocal as _OwnDB
                    from db.models import Chat as _OwnChat
                    _odb = _OwnDB()
                    try:
                        _crow = _odb.query(_OwnChat.user_id).filter(_OwnChat.id == _chat_id).first()
                        if _crow is not None and _crow[0] and str(_crow[0]) != str(_user_id) and _caller_role != "admin":
                            _chat_owned = False
                    finally:
                        _odb.close()
                except Exception as _own_err:  # noqa: BLE001
                    logger.warning(f"[docgen] chat ownership check failed (fail-closed): {_own_err}")
                    _chat_owned = False
                if not _chat_owned:
                    logger.warning(
                        f"[docgen] chat ownership denied | chat={_chat_id} user={_user_id} "
                        f"— skipping chat-history context"
                    )

                if _att_ids:
                    try:
                        from db.database import SessionLocal as _AttOwnDB
                        from db.models import ChatAttachment as _AttOwnModel
                        _aodb = _AttOwnDB()
                        try:
                            _rows = (
                                _aodb.query(_AttOwnModel.id, _AttOwnModel.user_id)
                                .filter(_AttOwnModel.id.in_(_att_ids))
                                .all()
                            )
                            _owned_ids = {
                                str(r[0]) for r in _rows
                                if (r[1] is None) or (str(r[1]) == str(_user_id)) or (_caller_role == "admin")
                            }
                            _dropped = [a for a in _att_ids if str(a) not in _owned_ids]
                            if _dropped:
                                logger.warning(
                                    f"[docgen] dropping {len(_dropped)} attachment(s) not owned "
                                    f"by user={_user_id}"
                                )
                            _att_ids = [a for a in _att_ids if str(a) in _owned_ids]
                        finally:
                            _aodb.close()
                    except Exception as _att_own_err:  # noqa: BLE001
                        logger.warning(
                            f"[docgen] attachment ownership check failed (fail-closed): {_att_own_err}"
                        )
                        _att_ids = []

                _doc_chat_context = ""
                _doc_chat_last_response = ""
                # P8: detect "all conversation" requests — load more turns and
                # use a larger context cap so no topic is truncated.
                import re as _re_doc
                _all_conversation = bool(
                    (getattr(_di, "topic", "") or "") and any(
                        w in (getattr(_di, "topic", "") or "").lower()
                        for w in ("all", "entire", "whole", "everything", "complete")
                    )
                    or _re_doc.search(
                        r'\b(all|entire|whole|complete|full)\b.{0,20}\b(conversation|chat|discussion|history)\b',
                        q.question, _re_doc.IGNORECASE
                    )
                )
                _ctx_turn_limit = 80 if _all_conversation else 40
                _ctx_char_cap   = 20000 if _all_conversation else 8000
                if _chat_owned and _has_chat_ctx and _di.intent in ("summarize", "convert", "extract", "generate"):
                    try:
                        _turns: list = []   # [{role, content}] oldest→newest
                        from memory.redis_memory import RedisMemory as _RMDoc
                        _rhist = _RMDoc().get_conversation(_chat_id, limit=_ctx_turn_limit) or []
                        for _e in _rhist:
                            _erole = _e.get("role")
                            if _erole not in ("user", "assistant"):
                                continue
                            # Exclude KB / codebase / RAG turns (plain chat only).
                            if str((_e.get("metadata") or {}).get("rag_mode") or "off") != "off":
                                continue
                            _ec = (_e.get("content") or "").strip()
                            if _ec:
                                _turns.append({"role": _erole, "content": _ec})
                        if not _turns:
                            # Redis cold → Postgres, same rag_mode=="off" filter.
                            from sqlalchemy import or_
                            from db.database import SessionLocal as _DocHDB
                            from db.models import Chat as _DocChat
                            from db.models import ChatMessage as _DocCM
                            _dhdb = _DocHDB()
                            try:
                                _rows = (
                                    _dhdb.query(_DocCM)
                                    .join(_DocChat, _DocChat.id == _DocCM.chat_id)
                                    .filter(
                                        _DocCM.chat_id == _chat_id,
                                        _DocCM.role.in_(["user", "assistant"]),
                                        _DocCM.rag_mode == "off",
                                        or_(
                                            _DocChat.user_id == _user_id,
                                            _DocChat.user_id.is_(None),
                                        ),
                                    )
                                    .order_by(_DocCM.created_at.desc())
                                    .limit(_ctx_turn_limit)
                                    .all()
                                )
                            finally:
                                _dhdb.close()
                            for _r in reversed(_rows):
                                _rc_txt = (_r.content or "").strip()
                                if _rc_txt:
                                    _turns.append({"role": _r.role, "content": _rc_txt})
                        # Verbatim last assistant reply (worker caps at 40 K chars).
                        for _t in reversed(_turns):
                            if _t["role"] == "assistant":
                                _doc_chat_last_response = _t["content"]
                                break
                        # Plain-text transcript — cap raised for all_conversation.
                        _lines = [
                            f"{'User' if _t['role'] == 'user' else 'Assistant'}: {_t['content']}"
                            for _t in _turns
                        ]
                        _doc_chat_context = "\n\n".join(_lines)[:_ctx_char_cap]
                        logger.info(
                            f"[docgen] chat-source context loaded | chat={_chat_id} "
                            f"turns={len(_turns)} ctx_chars={len(_doc_chat_context)} "
                            f"last_resp_chars={len(_doc_chat_last_response)} "
                            f"intent={_di.intent} all_conv={_all_conversation}"
                        )
                    except Exception as _dctx_err:  # noqa: BLE001
                        logger.warning(f"[docgen] chat-source context load failed (fail-open): {_dctx_err}")
                        _doc_chat_context = ""
                        _doc_chat_last_response = ""

                # MULTI-FORMAT mode: every deliverable is the SAME content in a
                # different format (same intent + same topic, distinct formats).
                # One authoring pass + N-1 verbatim renders (cheap; no re-author).
                # Otherwise DISTINCT mode: each deliverable is authored on its own.
                _fmts        = [d["format"] for d in _docs]
                _same_intent = len({d["intent"] for d in _docs}) == 1
                _same_topic  = len({(d.get("topic") or "").strip().lower() for d in _docs}) == 1
                _distinct_fmts = len(set(_fmts)) == len(_fmts)
                _multi_format  = (len(_docs) > 1 and _same_intent
                                  and _same_topic and _distinct_fmts)

                _jobs: list = []   # [{job_id, format, filename_hint, marker}, …]

                if len(_docs) == 1:
                    # Fast path — identical to the pre-multi-doc behaviour.
                    _res = _enqueue_doc(
                        user_id=_user_id, question=q.question,
                        fmt=_docs[0]["format"], chat_id=_chat_id,
                        attachment_ids=_att_ids, doc_intent=_docs[0]["intent"],
                        doc_confidence=_di.confidence,
                        doc_source_scope=_di.source_scope,
                        all_conversation=_all_conversation,
                        user_model_hint=_model_hint,
                        chat_context=_doc_chat_context,
                        chat_last_response=_doc_chat_last_response,
                        correlation_id=request_id,
                    )
                    _jobs.append({
                        "job_id":        _res["job_id"],
                        "format":        _res["ext"],
                        "filename_hint": _res["filename_hint"],
                        "marker":        _res["marker"],
                    })
                elif _multi_format:
                    # Pre-allocate sibling job_ids so the frontend can mount every
                    # marker up front and each card polls on its own id. The worker
                    # renders these exact ids from the primary's content_md.
                    _sibling_job_ids = [str(_uuid_doc.uuid4()) for _ in _docs[1:]]
                    _sibling_formats = [d["format"] for d in _docs[1:]]
                    # Primary authoring job — suppress its own chat-history publish;
                    # we publish ONE combined message with all markers below.
                    _res = _enqueue_doc(
                        user_id=_user_id, question=q.question,
                        fmt=_docs[0]["format"], chat_id=_chat_id,
                        attachment_ids=_att_ids, doc_intent=_docs[0]["intent"],
                        doc_confidence=_di.confidence,
                        doc_source_scope=_di.source_scope,
                        all_conversation=_all_conversation,
                        user_model_hint=_model_hint,
                        chat_context=_doc_chat_context,
                        chat_last_response=_doc_chat_last_response,
                        publish_chat_history=False,
                        sibling_formats=_sibling_formats,
                        sibling_job_ids=_sibling_job_ids,
                        correlation_id=request_id,
                    )
                    _jobs.append({
                        "job_id":        _res["job_id"],
                        "format":        _res["ext"],
                        "filename_hint": _res["filename_hint"],
                        "marker":        _res["marker"],
                    })
                    for _sjid, _sfmt in zip(_sibling_job_ids, _sibling_formats):
                        _smarker, _sfn, _sext = _doc_marker_for(
                            job_id=_sjid, fmt=_sfmt, question=q.question,
                        )
                        _jobs.append({
                            "job_id":        _sjid,
                            "format":        _sext,
                            "filename_hint": _sfn,
                            "marker":        _smarker,
                        })
                else:
                    # DISTINCT mode — each deliverable has a different topic/intent
                    # and must be authored independently. Pre-allocate all job IDs
                    # up front so the frontend can mount every download card in one
                    # response, then enqueue ONLY the primary job. The worker fans
                    # out the remaining jobs sequentially (one at a time) after each
                    # completes, preventing queue starvation when workers are busy.
                    _distinct_job_ids = [str(_uuid_doc.uuid4()) for _ in _docs]
                    _distinct_intents = [d["intent"] for d in _docs]
                    _distinct_formats = [d["format"] for d in _docs]

                    # Write Redis "pending" placeholders for jobs 2..N so the
                    # status endpoint returns "running" (not "error") while they
                    # await enqueueing. DB=6 matches doc_download_router's _R client.
                    if len(_distinct_job_ids) > 1:
                        try:
                            from core.kv import get_kv as _get_kv_ph
                            import json as _json_ph
                            _R_ph = _get_kv_ph(6)
                            for _pjid in _distinct_job_ids[1:]:
                                _R_ph.setex(
                                    f"doc:result:{_pjid}",
                                    7200,  # 2-hour TTL — covers any realistic chain
                                    _json_ph.dumps({
                                        "status":  "running",
                                        "user_id": str(_user_id),
                                    }),
                                )
                        except Exception as _ph_err:
                            logger.warning(
                                f"[docgen] failed to write pending placeholders: {_ph_err}"
                            )

                    # Enqueue only the primary job (index 0)
                    _res = _enqueue_doc(
                        user_id=_user_id, question=q.question,
                        fmt=_distinct_formats[0], chat_id=_chat_id,
                        attachment_ids=_att_ids, doc_intent=_distinct_intents[0],
                        doc_confidence=_di.confidence,
                        doc_source_scope=_di.source_scope,
                        all_conversation=_all_conversation,
                        user_model_hint=_model_hint,
                        chat_context=_doc_chat_context,
                        chat_last_response=_doc_chat_last_response,
                        publish_chat_history=False,
                        correlation_id=request_id,
                        job_id_override=_distinct_job_ids[0],
                        pending_sibling_job_ids=_distinct_job_ids[1:],
                        pending_sibling_formats=_distinct_formats[1:],
                        pending_sibling_intents=_distinct_intents[1:],
                    )
                    _jobs.append({
                        "job_id":        _distinct_job_ids[0],
                        "format":        _res["ext"],
                        "filename_hint": _res["filename_hint"],
                        "marker":        _res["marker"],
                    })
                    # Mount markers for pending siblings (not yet enqueued)
                    for _sjid, _sfmt in zip(_distinct_job_ids[1:], _distinct_formats[1:]):
                        _smarker, _sfn, _sext = _doc_marker_for(
                            job_id=_sjid, fmt=_sfmt, question=q.question,
                        )
                        _jobs.append({
                            "job_id":        _sjid,
                            "format":        _sext,
                            "filename_hint": _sfn,
                            "marker":        _smarker,
                        })

                # ── ONE combined chat-history publish (avoids duplicate turns) ──
                # The Kafka consumer inserts a user+assistant pair per record with
                # question+answer; publishing per-job would duplicate the turn.
                # For len==1 the fast path already published inside enqueue_doc_job.
                if len(_jobs) > 1 and _chat_id and q.question:
                    try:
                        from core.kafka_producer import produce as _produce, TOPIC_CHAT_HISTORY as _TCH
                        _combined_answer = "".join(j["marker"] for j in _jobs)
                        _produce(_TCH, {
                            "chat_id":              _chat_id,
                            "user_id":              str(_user_id),
                            "question":             q.question,
                            "answer":               _combined_answer,
                            "assistant_message_id": str(_uuid_doc.uuid4()),
                            "job_id":               _jobs[0]["job_id"],
                            "request_id":           request_id,
                            "title_hint":           q.question[:400],
                            "attachment_ids":       _att_ids,
                        }, key=_chat_id)
                    except Exception as _hexc:  # noqa: BLE001
                        logger.warning(f"[docgen] route multi-doc chat_history publish failed | corr={request_id} error={_hexc}")

                logger.info(
                    f"[docgen] route | corr={request_id} intent={_di.intent} "
                    f"mode={'multi_format' if _multi_format else ('single' if len(_docs)==1 else 'distinct')} "
                    f"docs={len(_jobs)} fmts={[j['format'] for j in _jobs]} "
                    f"jobs={[j['job_id'] for j in _jobs]} conf={_di.confidence:.2f}"
                )
                if _PIPELINE_V2 and _rc is not None:
                    _rc.dispatch = _DispatchDecision(lane=_Lane.DOC_ROUTE, reason=_di.intent)
                    _otel.record_event("dispatch", lane="doc_route")
                # ── Doc plan preview — shown to user before generation starts ──
                # Builds a human-readable summary of what will be generated so
                # the user can see intent/format/source before the doc card appears.
                _scope_desc = {
                    "chat":     "your conversation",
                    "uploaded": "the uploaded file",
                    "artifact": "a previously generated document",
                    "none":     "your request",
                }.get(_di.source_scope or "none", "your request")
                _fmt_word_plan = {
                    "pdf": "PDF", "docx": "Word document", "pptx": "presentation",
                    "xlsx": "spreadsheet", "csv": "CSV", "md": "Markdown document",
                    "txt": "text file",
                }.get((_docs[0]["format"] or "pdf"), "document")
                _intent_verb = {
                    "generate":  "Generating",
                    "summarize": "Summarizing",
                    "extract":   "Extracting",
                    "convert":   "Converting",
                    "revise":    "Revising",
                    "compare":   "Comparing",
                }.get(_di.intent, "Generating")
                _plan_topic = (getattr(_di, "topic", "") or "").strip()
                _plan_msg = (
                    f"{_intent_verb} a {_fmt_word_plan}"
                    + (f" about **{_plan_topic}**" if _plan_topic else "")
                    + f" from {_scope_desc}"
                    + (" (full conversation)" if _all_conversation else "")
                    + "…"
                )
                _doc_plan_preview = {
                    "intent":  _di.intent,
                    "format":  _docs[0]["format"],
                    "source":  _di.source_scope or "none",
                    "topic":   _plan_topic,
                    "message": _plan_msg,
                    "all_conversation": _all_conversation,
                }
                # ── Notify the CLI session about this doc generation ──────────
                # The CLI runs in a separate session and has no visibility into
                # the gateway's doc pipeline. Store a one-shot context note in
                # Redis so the CLI is informed on the user's NEXT message —
                # giving it awareness of the doc for follow-up turns ("summarize
                # that", "add a section", etc.). Non-fatal: a Redis failure here
                # must never block the doc response.
                try:
                    _notify_title = (
                        _jobs[0].get("filename_hint", "")
                        or _plan_topic
                        or q.question[:80]
                    )
                    _ainxt_doc_ctx_put(
                        chat_id=_chat_id or "",
                        question=q.question or "",
                        title=_notify_title,
                        fmt=_jobs[0]["format"],
                        job_id=_jobs[0]["job_id"],
                    )
                except Exception as _notify_err:
                    logger.warning(f"[ainxt-api] doc_ctx notify failed (non-fatal): {_notify_err}")

                return _JR_doc(status_code=200, content={
                    "route":         "doc",
                    # Back-compat top-level fields mirror the FIRST job so older
                    # frontends that read job_id/marker still work.
                    "job_id":        _jobs[0]["job_id"],
                    "format":        _jobs[0]["format"],
                    "filename_hint": _jobs[0]["filename_hint"],
                    "marker":        _jobs[0]["marker"],
                    # New multi-doc array — the frontend iterates this to mount
                    # every download card.
                    "jobs":          _jobs,
                    "chat_id":       _chat_id,
                    # Doc plan preview — frontend renders this as an info card
                    # before the doc generation card appears.
                    "doc_plan":      _doc_plan_preview,
                })
        except Exception as _dexc:  # noqa: BLE001
            # Never break normal chat because doc-routing failed — fall through.
            logger.warning(f"[docgen] route skipped | corr={request_id} error={_dexc}")

    # ── IMAGE-INTENT ROUTING ──────────────────────────────────────────────────
    # When the CIL classified the turn as an image-generation request, build an
    # enriched prompt (optionally prepending the uploaded image's Vision
    # description and/or recent chat context) and return {route:"image", prompt}
    # as JSON. The frontend calls /chat/image-generate with that prompt so the
    # user gets a new image that is actually informed by their input.
    #
    # Parallel to the doc-intent block above. Same gate: PIPELINE_V2 + CIL
    # produced a model result. Falls through to normal chat if CIL is off,
    # skipped, or img_intent="none".
    if (not q.ephemeral
            and not _is_cli
            and _PIPELINE_V2
            and _rc is not None
            and _rc.conv_state is not None
            and _rc.conv_state.img_intent == "generate"
            and _rc.conv_state.img_confidence > 0.5):
        try:
            _img_prompt = (_rc.conv_state.img_prompt or q.question or "").strip()
            _img_scope  = _rc.conv_state.img_source_scope

            # ── Enrich with uploaded attachment content ───────────────────
            # Handles both image attachments (Vision description) and document
            # attachments (parsed text excerpt). The original block only fetched
            # Vision descriptions filtered by kind="image", so "make an image
            # based on this PDF" silently produced an unenriched prompt.
            if _img_scope in ("uploaded", "uploaded_and_chat"):
                _att_ids_img = list(getattr(q, "attachment_ids", None) or [])
                if _att_ids_img:
                    try:
                        from db.database import SessionLocal as _ImgIntDB
                        from db.models import ChatAttachment as _ImgIntCA
                        _iidb = _ImgIntDB()
                        try:
                            # No kind filter — fetch the first attachment regardless
                            # of type and branch on kind below.
                            _img_att = (
                                _iidb.query(_ImgIntCA)
                                .filter(_ImgIntCA.id == _att_ids_img[0])
                                .first()
                            )
                            if _img_att and _img_att.parsed_text:
                                if _img_att.kind == "image":
                                    # Vision description of an uploaded image —
                                    # prepend so the model knows what to improve/vary.
                                    _vision_desc = _img_att.parsed_text[:2000].strip()
                                    _img_prompt = (
                                        f"Reference image description: {_vision_desc}\n\n"
                                        f"Improvement/variation request: {_img_prompt}"
                                    )
                                    logger.debug(
                                        f"[img_intent] enriched with image Vision desc "
                                        f"chars={len(_vision_desc)}"
                                    )
                                else:
                                    # Document attachment (PDF, DOCX, etc.) — use the
                                    # first 1000 chars of parsed text as source material
                                    # context so the image reflects the document content.
                                    _doc_ctx = _img_att.parsed_text[:1000].strip()
                                    _img_prompt = (
                                        f"Document context (source material for the image):\n"
                                        f"{_doc_ctx}\n\n"
                                        f"Image request: {_img_prompt}"
                                    )
                                    logger.debug(
                                        f"[img_intent] enriched with doc attachment "
                                        f"kind={_img_att.kind!r} chars={len(_doc_ctx)}"
                                    )
                        finally:
                            _iidb.close()
                    except Exception as _vd_err:  # noqa: BLE001
                        logger.debug(f"[img_intent] attachment fetch failed: {_vd_err}")

            # ── Enrich with recent chat context ───────────────────────────
            if _img_scope in ("chat", "uploaded_and_chat"):
                if _cil_probe:
                    _chat_ctx_lines = "\n".join(
                        f"{t.get('role', '?')}: {str(t.get('content') or '')[:300]}"
                        for t in _cil_probe[-3:]
                    )
                    _img_prompt = (
                        f"Conversation context:\n{_chat_ctx_lines}\n\n"
                        f"Image request: {_img_prompt}"
                    )

            # ── Quality suffix ─────────────────────────────────────────────
            # Appended at the gateway level so it is always present regardless
            # of whether the proxy or the direct SDK path handles the call.
            # Placed after all semantic enrichment so the model sees:
            #   1. What to draw (semantic content + context)
            #   2. How to render it (quality directive)
            #
            # IMPORTANT: the suffix is context-aware.
            # When the user is improving/varying a UI screenshot (login page,
            # dashboard, form, etc.) the model MUST preserve all text labels,
            # buttons, and UI elements — "no text" would destroy the content.
            # We detect a UI/screenshot context by checking whether the enriched
            # prompt contains vision-description markers (login, form, button,
            # field, UI, screen, page, dashboard, interface, menu, icon, tab,
            # navbar, sidebar, modal, dialog, input, label, placeholder, etc.)
            # OR whether the user's question contains improvement/variation verbs
            # (improve, enhance, redesign, update, modernise, refine, etc.).
            # In that case we use a UI-preserving suffix instead of the generic
            # photorealistic/no-text one.
            _ui_signal_re = _re_cil_pre.compile(
                r"\b(?:login|sign.?in|sign.?up|register|dashboard|form|button|"
                r"field|input|label|placeholder|navbar|sidebar|menu|tab|modal|"
                r"dialog|dropdown|checkbox|radio|toggle|icon|tooltip|card|"
                r"header|footer|panel|widget|screen|page|interface|ui|ux|"
                r"improve|enhance|redesign|update|modernise|modernize|refine|"
                r"make.*better|better.*version|upgrade|restyle|rework)\b",
                _re_cil_pre.IGNORECASE,
            )
            _is_ui_context = bool(_ui_signal_re.search(_img_prompt))
            if _is_ui_context:
                # UI / screenshot improvement — preserve all text and layout.
                _img_prompt = (
                    f"{_img_prompt}\n\n"
                    f"Rendering style: high quality, professional, clean modern design, "
                    f"sharp focus, well-composed. Preserve all text labels, buttons, "
                    f"input fields, and UI elements exactly as described. "
                    f"Do NOT remove or replace any text content."
                )
            else:
                # Creative / scene generation — photorealistic is appropriate.
                _img_prompt = (
                    f"{_img_prompt}\n\n"
                    f"Rendering style: photorealistic, high quality, professional, "
                    f"sharp focus, well-composed."
                )

            logger.info(
                f"[img_intent] routing to image-generate | corr={request_id} "
                f"scope={_img_scope!r} prompt_chars={len(_img_prompt)} "
                f"conf={_rc.conv_state.img_confidence:.2f}"
            )

            # ── Persist original user turn to Redis before early return ───────
            # The /ask handler returns here without going through the normal
            # streaming path, so the original user question (with attachment_ids
            # referencing the uploaded image) is never written to Redis or Kafka.
            # On the very next turn ("explain the image I attached") the L2-img
            # block queries ChatMessage.attachment_ids from Postgres — but that
            # row doesn't exist yet (Kafka hasn't consumed it). Writing the user
            # turn to Redis here ensures the history loader sees it immediately,
            # and the L2-img block can inject the image caption into context.
            # The /chat/image-generate endpoint writes its own user/assistant
            # pair to Redis (the generation prompt + "[Generated image based on:]")
            # — this write covers the ORIGINAL user question with the attachment.
            _orig_att_ids = list(getattr(q, "attachment_ids", None) or [])
            # Use q.question directly here — safe_question is not yet defined at
            # this point in ask_ai (it is assigned ~1100 lines later after PII
            # checks). Using q.question is correct: the Redis write is for the
            # original user turn before any enrichment, which is exactly what
            # q.question holds.
            _orig_user_question = (q.question or "").strip()
            if _chat_id and _orig_user_question and _orig_att_ids:
                try:
                    from memory.redis_memory import RedisMemory as _RMImgRoute
                    _rm_img_route = _RMImgRoute()
                    # Build a compact caption from the uploaded image's parsed_text
                    # so the history entry carries enough context for follow-up turns.
                    _orig_img_caption = _orig_user_question
                    try:
                        from db.database import SessionLocal as _OrigAttDB
                        from db.models import ChatAttachment as _OrigAttCA
                        _oadb = _OrigAttDB()
                        try:
                            _orig_att = (
                                _oadb.query(_OrigAttCA)
                                .filter(_OrigAttCA.id == _orig_att_ids[0])
                                .first()
                            )
                            if _orig_att and (_orig_att.image_caption or _orig_att.image_description):
                                _orig_img_caption = (
                                    _orig_att.image_caption
                                    or (_orig_att.image_description or "")[:600]
                                ).strip() or _orig_user_question
                        finally:
                            _oadb.close()
                    except Exception:
                        pass
                    # Write the original user turn so history loaders see it.
                    _rm_img_route.save_message(
                        _chat_id, "user",
                        f"{_orig_user_question}\n[Attached image: {_orig_img_caption[:400]}]",
                        metadata={"rag_mode": "off", "source": "image_upload",
                                  "attachment_ids": _orig_att_ids},
                    )
                    logger.info(
                        f"[img_intent] original user turn written to Redis "
                        f"chat_id={_chat_id!r} att_ids={_orig_att_ids}"
                    )
                except Exception as _redis_orig_err:
                    logger.debug(f"[img_intent] Redis original-turn write skipped: {_redis_orig_err}")

            from fastapi.responses import JSONResponse as _JR_img
            return _JR_img(status_code=200, content={
                "route":        "image",
                "prompt":       _img_prompt,
                "chat_id":      _chat_id,
                # The original user question (e.g. "improve this image") before
                # enrichment. Forwarded to /chat/image-generate so it can store
                # this as the user message content in Postgres instead of the
                # long enriched prompt ("Reference image description: …").
                "original_question": q.question or "",
                # Pass the original uploaded-image attachment_ids through so
                # /chat/image-generate can store them on the user ChatMessage row
                # in Postgres (via Kafka). The L2-img block then finds them on
                # follow-up turns ("explain the image I attached") and injects
                # the image caption into context.
                "attachment_ids": _orig_att_ids,
            })
        except Exception as _img_route_err:  # noqa: BLE001
            # Never break normal chat because image-routing failed — fall through.
            logger.warning(f"[img_intent] route skipped | corr={request_id} error={_img_route_err}")

    # ── VIDEO-INTENT ROUTING ──────────────────────────────────────────────────
    # When the CIL classified the turn as a video-generation request, build an
    # enriched prompt (optionally prepending uploaded attachment content and/or
    # recent chat context) and return {route:"video", prompt, aspect_ratio,
    # duration_secs} as JSON. The frontend calls /chat/video-generate with that
    # payload so the user gets a Veo 3.1-generated MP4.
    #
    # Works for ALL input types (text, doc attachment, image attachment) because
    # the enrichment logic mirrors the image-intent block: attachment parsed_text
    # is prepended regardless of kind (image Vision desc or doc text excerpt).
    #
    # Supported Veo parameters (from llmproxy /llm/veo):
    #   aspect_ratio: 16:9 | 9:16 | 1:1 | 4:3 | 3:4  (default: 16:9)
    #   duration_secs: 2–16 seconds                    (default: 8)
    # CIL infers both from the user's phrasing; the routing block clamps them.
    #
    # Gating: same as image-intent — PIPELINE_V2 + CIL produced a model result.
    # Falls through to normal chat if CIL is off, skipped, or vid_intent="none".
    # CLI is excluded (video is chat-UI-only; /chat/video-generate enforces this
    # too, but we skip early here to avoid a useless round-trip).
    if (not q.ephemeral
            and not _is_cli
            and _PIPELINE_V2
            and _rc is not None
            and _rc.conv_state is not None
            and _rc.conv_state.vid_intent == "generate"
            and _rc.conv_state.vid_confidence > 0.5):
        try:
            _vid_prompt   = (_rc.conv_state.vid_prompt or q.question or "").strip()
            _vid_scope    = _rc.conv_state.vid_source_scope
            _vid_aspect   = _rc.conv_state.vid_aspect_ratio or "16:9"
            _vid_duration = int(_rc.conv_state.vid_duration_secs or 8)
            # Clamp to Veo-supported range (mirrors /chat/video-generate server-side clamp)
            _vid_duration = max(2, min(16, _vid_duration))

            # ── Enrich with uploaded attachment content ───────────────────
            # Handles image attachments (Vision description) and document
            # attachments (parsed text excerpt) — same pattern as img_intent.
            #
            # Previously this block only ran when _vid_scope == "uploaded".
            # That was wrong: the CIL may set vid_scope="none" or "chat" even
            # when an image is attached (e.g. because img_intent fired first
            # and the mutual exclusivity guard zeroed vid_scope, or because the
            # LLM didn't set scope correctly). We now enrich whenever
            # attachment_ids are present, regardless of scope — the image
            # description is always relevant context for the video model.
            _att_ids_vid = list(getattr(q, "attachment_ids", None) or [])
            if _att_ids_vid:
                try:
                    from db.database import SessionLocal as _VidIntDB
                    from db.models import ChatAttachment as _VidIntCA
                    _vidb = _VidIntDB()
                    try:
                        _vid_att = (
                            _vidb.query(_VidIntCA)
                            .filter(_VidIntCA.id == _att_ids_vid[0])
                            .first()
                        )
                        if _vid_att and _vid_att.parsed_text:
                            if _vid_att.kind == "image":
                                # Vision description of an uploaded image —
                                # prepend so the video model knows the visual reference.
                                _vision_desc = _vid_att.parsed_text[:2000].strip()
                                _vid_prompt = (
                                    f"Reference image description: {_vision_desc}\n\n"
                                    f"Video request: {_vid_prompt}"
                                )
                                logger.debug(
                                    f"[vid_intent] enriched with image Vision desc "
                                    f"chars={len(_vision_desc)}"
                                )
                            else:
                                # Document attachment (PDF, DOCX, etc.) — use the
                                # first 1000 chars of parsed text as source material.
                                _doc_ctx = _vid_att.parsed_text[:1000].strip()
                                _vid_prompt = (
                                    f"Document context (source material for the video):\n"
                                    f"{_doc_ctx}\n\n"
                                    f"Video request: {_vid_prompt}"
                                )
                                logger.debug(
                                    f"[vid_intent] enriched with doc attachment "
                                    f"kind={_vid_att.kind!r} chars={len(_doc_ctx)}"
                                )
                    finally:
                        _vidb.close()
                except Exception as _vd_vid_err:  # noqa: BLE001
                    logger.debug(f"[vid_intent] attachment fetch failed: {_vd_vid_err}")

            # ── Enrich with recent chat context ───────────────────────────
            if _vid_scope in ("chat", "uploaded_and_chat"):
                if _cil_probe:
                    _chat_ctx_lines = "\n".join(
                        f"{t.get('role', '?')}: {str(t.get('content') or '')[:300]}"
                        for t in _cil_probe[-3:]
                    )
                    _vid_prompt = (
                        f"Conversation context:\n{_chat_ctx_lines}\n\n"
                        f"Video request: {_vid_prompt}"
                    )

            logger.info(
                f"[vid_intent] routing to video-generate | corr={request_id} "
                f"scope={_vid_scope!r} aspect={_vid_aspect!r} duration={_vid_duration}s "
                f"prompt_chars={len(_vid_prompt)} "
                f"conf={_rc.conv_state.vid_confidence:.2f}"
            )
            from fastapi.responses import JSONResponse as _JR_vid
            return _JR_vid(status_code=200, content={
                "route":         "video",
                "prompt":        _vid_prompt,
                "aspect_ratio":  _vid_aspect,
                "duration_secs": _vid_duration,
                "chat_id":       _chat_id,
                # The original user question (e.g. "generate a video from this image")
                # before enrichment. Forwarded to /chat/video-generate so it can store
                # this as the user message content in Postgres instead of the long
                # enriched prompt ("Reference image description: …").
                "original_question": q.question or "",
            })
        except Exception as _vid_route_err:  # noqa: BLE001
            # Never break normal chat because video-routing failed — fall through.
            logger.warning(f"[vid_intent] route skipped | corr={request_id} error={_vid_route_err}")

    # ── AiNxt Coach — per-completion emit helper ─────────────────────────────
    # Fire-and-forget coach event for the many /ask streaming branches that
    # bypass the orchestrator audit path (_write_request_audit). Called once,
    # right after each generator yields its __meta__ frame. emit_coach_event()
    # never raises and is a no-op when ENABLE_COACH is off; only one branch runs
    # per request, so there is no double emit with the orchestrator path.
    def _emit_coach(meta: dict):
        # Skip ephemeral calls such as the frontend intent classifier — these
        # are internal platform hops, not real user prompts.
        if q.ephemeral:
            return
        _cs_ask = getattr(request.state, "client_source", "platform")
        _coach_prompt = q.question or ""
        # CLI agent mode: q.question contains injected file context blobs
        # (e.g. "<file contents>\n---\n\nTask: write a function…"). The
        # compliance engine redacts the entire blob (code files can contain
        # PCI-like patterns), leaving an empty or unreadable prompt in Coach.
        # Extract only the task text — the same way _hist_question does for
        # chat history storage — so Coach shows the real user intent.
        _is_cli_emit = _is_cli  # captured from outer scope; avoids redundant header/state read
        if _is_cli_emit:
            def _extract_cli_task(text: str) -> str:
                raw = (text or "").strip()
                if not raw:
                    return ""
                if "---\n\nTask:" in raw:
                    return raw.split("---\n\nTask:", 1)[-1].strip()
                if "\n\nTask:" in raw:
                    return raw.split("\n\nTask:", 1)[-1].strip()
                if "[USER QUESTION]" in raw:
                    return raw.split("[USER QUESTION]", 1)[-1].strip()
                # Reject context blobs / assembled system-preface payloads. Coach
                # should store the user-authored task, not 10K+ tokens of CLI context.
                if len(raw) > 4000:
                    return ""
                if raw.startswith(("<environment_details>", "<repo_map>", "<file_list>")):
                    return ""
                return raw

            _coach_prompt = _extract_cli_task(_coach_prompt)

            # Some CLI versions send the actual task in cli_messages.
            if not _coach_prompt and q.cli_messages:
                for _cm in reversed(q.cli_messages):
                    if isinstance(_cm, dict) and _cm.get("role") == "user" and _cm.get("content"):
                        _coach_prompt = _extract_cli_task(str(_cm.get("content") or ""))
                        if _coach_prompt:
                            break

            # Other CLI versions only expose the final user turn after gateway
            # assembly in _messages. Use that last user message as a fallback.
            if not _coach_prompt:
                for _m in reversed(_messages):
                    if isinstance(_m, dict) and _m.get("role") == "user" and _m.get("content"):
                        _coach_prompt = _extract_cli_task(str(_m.get("content") or ""))
                        if _coach_prompt:
                            break

            # Last fallback: safe_question may hold the redacted task text.
            if not _coach_prompt:
                _coach_prompt = _extract_cli_task(safe_question)

        # Do not persist/evaluate empty prompts. Empty CLI context turns are not
        # coachable and can trigger misleading context rules.
        if not _coach_prompt.strip():
            logger.info(f"[Coach] skipping empty prompt event user={_user_id} channel={_cs_ask}")
            return
        try:
            from core.coach_events import emit_coach_event, channel_from_client_source
            _coach_channel = channel_from_client_source(_cs_ask)
            # Derive eval_platform from q.rag_mode (explicit client signal),
            # NOT _rag_mode which may be overridden by the stored Chat DB row.
            # Chat.jsx always sends "off" → "chat".
            # KbChat.jsx sends "on"/"auto" → "knowledge_base".
            _coach_rag_explicit = (q.rag_mode or "").strip().lower()
            if getattr(q, "agent_id", None):
                _coach_eval_platform = "agent_studio"
            elif _coach_rag_explicit in {"on", "auto"}:
                _coach_eval_platform = "knowledge_base"
            else:
                _coach_eval_platform = "chat"
            logger.info(f"[Coach] emitting /ask event user={_user_id} client_source={_cs_ask} channel={_coach_channel} thread_id={_chat_id} prompt_len={len(_coach_prompt)}")
            emit_coach_event(
                user_id=_user_id or "anonymous",
                channel=_coach_channel,
                model=meta.get("model"),
                prompt=_coach_prompt,
                tokens_in=meta.get("in_tok", 0),
                tokens_out=meta.get("out_tok", 0),
                cost_usd=meta.get("cost", 0.0),
                latency_ms=int(meta.get("latency", 0) * 1000),
                request_id=request_id,
                thread_id=_chat_id,
                department=(_user_ctx or {}).get("department"),
                # `accepted` = did the user ACCEPT the suggestion (an IDE/completion
                # signal), NOT "was the LLM used". The gateway has no acceptance
                # signal, so leave it unknown (None). Passing meta["llm_used"] here
                # would falsely mark ~every web/CLI turn as accepted and corrupt the
                # downstream acceptance-rate metric + "unreviewed apply" rule
                # (agents/coach_evaluator.py). Cache vs LLM is already conveyed by
                # meta["source"]/model, not `accepted`.
                accepted=None,
                eval_platform=_coach_eval_platform,
            )
        except Exception as _coach_emit_err:
            logger.warning(f"[Coach] /ask emit failed: {_coach_emit_err}")

    # ─── RAG mode resolution ────────────────────────────────────────────────
    # Priority: explicit q.rag_mode > stored chat record > default ("off").
    # voice_platform and cli_mode preserve their existing behaviour and
    # do not gate on rag_mode (voice forces KB, cli bypasses retrieval).
    # repo_filter/project_id paths go through the orchestrator and are
    # unaffected by this toggle.
    _rag_mode = (q.rag_mode or "").strip().lower() if q.rag_mode else ""
    if _rag_mode not in {"off", "auto", "on"}:
        _rag_mode = ""  # unset → fall through to chat-record lookup

    # ─── Per-chat KB scope (Phase 1 wiring) ──────────────────────────────────
    # Fetched alongside rag_mode in a single Chat-row read. Server-derived:
    # we re-validate product_id against the user's dept-mapped products
    # (already in _user_ctx['product_ids']) and DROP the entire scope on
    # mismatch — never proxy a client-spoofable scope through to retrieval.
    # The hard scope filter in models/hybrid_search.py reads this same key
    # from user_ctx['scope_filter'] and turns it into the WHERE clauses that
    # eliminate cross-product hallucination (kn_rewrite.md §2 #2).
    _chat_scope_pid = None
    _chat_scope_dom = None
    _chat_scope_ver = None
    _chat_scope_did = None
    if q.chat_id:
        try:
            from db.database import SessionLocal as _RMSL
            from db.models import Chat as _RMChat
            _rmdb = _RMSL()
            try:
                _rm_row = _rmdb.query(_RMChat).filter(_RMChat.id == q.chat_id).first()
                if _rm_row is not None:
                    if not _rag_mode:
                        _stored = (getattr(_rm_row, "rag_mode", None) or "").strip().lower()
                        if _stored in {"off", "auto", "on"}:
                            _rag_mode = _stored
                    _chat_scope_pid = getattr(_rm_row, "product_id",   None)
                    _chat_scope_dom = getattr(_rm_row, "domain",       None)
                    _chat_scope_ver = getattr(_rm_row, "spec_version", None)
                    _chat_scope_did = getattr(_rm_row, "kb_doc_id",    None)
            finally:
                _rmdb.close()
        except Exception:
            pass
    # ── Inline-scope fallback for KB chats' first turn ───────────────────
    # KbChatPanel hands off with kbScopePending=true: the Chat row is created
    # lazily by chat_persist AFTER this request, so the DB lookup above finds
    # nothing on turn 1. The client sends the scope inline in q.product_id /
    # q.domain / q.spec_version / q.kb_doc_id; we fill any NULL slot from
    # those values so retrieval is scoped from the very first message.
    # DB-loaded values always win — inline is a fallback, not an override.
    if _chat_scope_pid is None and q.product_id:   _chat_scope_pid = q.product_id
    if _chat_scope_dom is None and q.domain:       _chat_scope_dom = q.domain
    if _chat_scope_ver is None and q.spec_version: _chat_scope_ver = q.spec_version
    if _chat_scope_did is None and q.kb_doc_id:    _chat_scope_did = q.kb_doc_id

    # Multi-doc selection from DocPickerCard disambiguation — user explicitly
    # chose which documents to search. When set, the disambiguation gate is
    # bypassed and full_file coverage runs on exactly these doc_ids.
    _chat_scope_doc_ids: list = [str(d) for d in (q.kb_doc_ids or []) if d]

    if not _rag_mode:
        _rag_mode = "off"

    # Bypass safety filters ONLY when the user has explicitly selected a local
    # model in KB chat (rag on/auto). A stray `local_model` field alongside a
    # cloud model selection (e.g. Claude Haiku) MUST NOT trigger bypass — that
    # was leaking user data past compliance/redaction to cloud LLMs.
    _bypass_model_raw = (q.model or "").strip().lower()
    _bypass_safety_filters = bool(
        _rag_mode in {"auto", "on"}
        and (
            _bypass_model_raw == "local"
            or _bypass_model_raw.startswith("local:")
        )
    )

    # Inject scope into _user_ctx so hybrid_retrieve_context picks it up via
    # user_ctx['scope_filter'] (and user_ctx['kb_doc_id'] for the coverage
    # tier trigger). Empty values are dropped so partial scopes still work.
    if _user_ctx is not None and (_chat_scope_pid or _chat_scope_dom or _chat_scope_ver or _chat_scope_did):
        _scope_pid_str = str(_chat_scope_pid) if _chat_scope_pid else None
        # Server-side product membership check: admins bypass, everyone else
        # must have the product in their dept-mapped set. Fail-closed = drop
        # the whole scope rather than silently retrieve unscoped (which would
        # re-introduce the cross-product hallucination this wiring closes).
        _is_admin = (_user_ctx.get("user_role") or "").lower() == "admin" or _user_ctx.get("is_admin")
        _allowed_pids = set(_user_ctx.get("product_ids") or [])
        if _scope_pid_str and not _is_admin and _allowed_pids and _scope_pid_str not in _allowed_pids:
            logger.warning(
                f"/ask: dropping chat scope — product_id={_scope_pid_str} not in "
                f"user's dept-mapped products (user={_user_id} dept={_user_dept})"
            )
        else:
            _scope_dict = {}
            if _scope_pid_str:    _scope_dict["product_id"]   = _scope_pid_str
            if _chat_scope_dom:   _scope_dict["domain"]       = _chat_scope_dom
            if _chat_scope_ver:   _scope_dict["spec_version"] = _chat_scope_ver
            if _scope_dict:
                _user_ctx["scope_filter"] = _scope_dict
            if _chat_scope_did:
                _user_ctx["kb_doc_id"] = str(_chat_scope_did)
            logger.info(
                f"/ask: injected chat scope → scope_filter={_scope_dict} "
                f"kb_doc_id={_chat_scope_did}"
            )


    original = q.question.strip()

    # Immutable copy of the user's ACTUAL prompt (pre-framing, pre-attachment,
    # pre-agent/office system-prompt injection). `clean_question` is derived into a
    # compliance-redacted + PII-masked `stored_question` further down and is the
    # ONLY thing written to chat_messages.content / the chat title. 
    clean_question = original

    logger.info(f"ask prompt")
    # ========================================================
    # STEP 0b: PROJECT BUDGET GATE (USD)
    # ========================================================
    if q.project_id:
        try:
            from db.database import SessionLocal as _PgSession
            from db.models import ProjectRecord as _ProjRec
            _ps = _PgSession()
            try:
                _proj = _ps.query(_ProjRec).filter(_ProjRec.id == q.project_id).first()
                if _proj and _proj.budget_limit_usd is not None:
                    _used = _proj.budget_used_usd or 0.0
                    if _used >= _proj.budget_limit_usd:
                        # Budget exceeded — block request and notify
                        try:
                            from store.inbox_store import publish_inbox_item
                            publish_inbox_item(
                                user_id=_user_id,
                                type="budget_alert",
                                title=f"Budget exceeded for project '{_proj.name}'",
                                body=f"Spent ${_used:.4f} of ${_proj.budget_limit_usd:.2f} limit. Request blocked. Please increase budget or get approval.",
                                source_id=str(_proj.id),
                                metadata={"project_id": str(_proj.id), "used_usd": _used, "limit_usd": _proj.budget_limit_usd},
                            )
                        except Exception:
                            pass
                        from fastapi.responses import JSONResponse
                        return JSONResponse(
                            status_code=402,
                            content={"error": "project_budget_exceeded",
                                     "detail": f"Project '{_proj.name}' budget limit ${_proj.budget_limit_usd:.2f} exceeded (used ${_used:.4f}). Requires approval to continue."},
                        )
            finally:
                _ps.close()
        except Exception as _budget_err:
            logger.warning(f"Budget gate error: {_budget_err}")

    # ========================================================
    # USER BUDGET HARD GATE
    # Cloud API models (OpenAI, Claude, Gemini) are blocked when
    # the user's allocation is exhausted.  In-house models (on-prem
    # GPU cluster, self-hosted, routed via LiteLLM) carry no external
    # API cost and are always allowed through.
    # Auto/empty model hints may route to cloud — check budget.
    # ========================================================
    _CLOUD_PFX = ("gpt-", "claude-", "gemini-", "openai/", "anthropic/", "google/", "azure/")
    _req_model_hint = (q.model or "").lower().strip()
    _req_is_inhouse = (
            bool(_req_model_hint)
            and _req_model_hint not in ("auto", "default")
            and not any(_req_model_hint.startswith(p) for p in _CLOUD_PFX)
    )
    if not _req_is_inhouse:
        try:
            from store.budget_store import check_budget as _chk_budget
            _ubget = _chk_budget(_user_id)
            if _ubget.get("allowed") is not True:
                from fastapi.responses import JSONResponse as _JR_bgt
                _bgt_reason = _ubget.get("reason", "Budget allocation exhausted")
                logger.warning(f"Budget gate BLOCKED (cloud): user={_user_id} reason={_bgt_reason}")
                try:
                    from core.coach_events import emit_coach_event
                    _cs_bgt = getattr(request.state, "client_source", "web")
                    emit_coach_event(
                        user_id=_user_id or "anonymous",
                        channel="web" if _cs_bgt == "platform" else ("mcp" if _cs_bgt.startswith("ide-") else _cs_bgt),
                        model="budget_blocked",
                        prompt=q.question,
                        tokens_in=0,
                        tokens_out=0,
                        cost_usd=0.0,
                        latency_ms=0,
                        request_id=request_id,
                        thread_id=q.chat_id or q.session_id,
                        department=(_user_ctx or {}).get("department"),
                    )
                except Exception:
                    pass
                return _JR_bgt(
                    status_code=429,
                    content={
                        "error":          "budget_exceeded",
                        "detail":         _bgt_reason,
                        "code":           "BUDGET_EXCEEDED",
                        "inhouse_ok":     True,   # tells UI: in-house models still work
                    },
                )
        except Exception as _ubget_err:
            logger.error(f"Budget gate check FAILED (fail-open): user={_user_id} err={_ubget_err}")
    else:
        logger.debug(f"Budget gate SKIPPED (in-house model): user={_user_id} model={q.model!r}")

    # ========================================================
    # STEP 0: FETCH ATTACHMENT CONTEXT (multimodal)
    # ========================================================

    if q.attachment_ids:
        try:
            from db.database import SessionLocal
            from db.models import ChatAttachment
            _session = SessionLocal()
            try:
                attachments = _session.query(ChatAttachment).filter(
                    ChatAttachment.id.in_(q.attachment_ids)
                ).all()
                # Per-attachment injection budget (chars). Historically hard-capped
                # at 10_000, which silently dropped the tail of any large document.
                # Env-tunable via ASK_ATTACH_CHAR_CAP; default 0 = no cap (send the
                # full stored parsed_text, up to the 2M-char parser cap already
                # applied in core/document_parser.py). The model's context window +
                # context-size routing (model_router.py) are the real ceiling from
                # here on.
                #
                # NOTE: commit 6e4aeba4 ("Upper limit for env", Aug 4 2026) silently
                # changed the default back to "1000000" (1 MB), which reintroduced
                # exactly the truncation this comment says was fixed — a large
                # workbook with many sheets/wide columns easily exceeds 1 MB of
                # parsed text and got re-truncated on every /ask turn that
                # referenced the attachment (not just the first upload). Restored
                # to "0" (no cap) here; if a cap is ever needed again for cost
                # control, prefer a much larger value and keep it env-driven so it
                # doesn't silently regress like this again.
                try:
                    _attach_cap = int(os.getenv("ASK_ATTACH_CHAR_CAP", "0") or "0")
                except Exception:
                    _attach_cap = 0
                blocks = []
                for a in attachments:
                    if not a.parsed_text:
                        continue
                    _truncated = _attach_cap > 0 and len(a.parsed_text) > _attach_cap
                    _ptext = a.parsed_text if _attach_cap <= 0 else a.parsed_text[:_attach_cap]
                    # Surface truncation explicitly to the model (as a stated fact)
                    # instead of silently cutting it — a model that gets a
                    # truncated-without-warning file tends to conclude the read
                    # failed and re-request it, which is what trips the desktop's
                    # runaway-loop circuit breaker on large Excel files.
                    _warn = (
                        f"\n[NOTE: this file is {len(a.parsed_text):,} characters; only the first "
                        f"{_attach_cap:,} are shown here ({len(a.parsed_text) - _attach_cap:,} characters were "
                        f"NOT included]. Do not re-request this file — tell the user the data is partial.\n"
                    ) if _truncated else ""
                    blocks.append(f"[File: {a.file_name}]{_warn}\n{_ptext}")
                    logger.info(
                        f"[DOCTRACE] L2 attach-inject | corr={request_id} "
                        f"attachment_id={a.id} file={a.file_name!r} "
                        f"stored_chars={len(a.parsed_text)} injected_chars={len(_ptext)} "
                        f"cap={_attach_cap or 'none'} "
                        f"truncated={_truncated}"
                    )
                if blocks:
                    original = "\n\n".join(blocks) + "\n\nUser question: " + original
                    logger.info(
                        f"[DOCTRACE] L2 attach-merged | corr={request_id} "
                        f"n_files={len(blocks)} prompt_chars_after_merge={len(original)}"
                    )
            finally:
                _session.close()
        except Exception as _att_err:
            logger.warning(f"attachment fetch failed: {_att_err}")

    # Pass model hint through for future model_router integration
    # Normalize "Auto", "auto", "default" to None (auto-routing)
    _model_hint = q.model if q.model and q.model.lower() not in ("auto", "default", "") else None
    _local_model = q.local_model  # explicit local model override (e.g. "Kimi-k2.5")
    if _local_model and _model_hint is None:
        _model_hint = "local"

    # ── MODEL GOVERNANCE ENFORCEMENT ─────────────────────────────────────────
    # Block the request early if the user's department (or a user-level
    # override) disallows the requested model.  Only fires when an explicit
    # model hint is given — auto-routing is not blocked here because the
    # model_router will pick an allowed model anyway (the UI already filters
    # the picker via /my-models).  Fails open on any DB / import error so a
    # governance misconfiguration never takes down the chat service.
    if _model_hint:
        try:
            from routers.model_governance_router import filter_allowed_models as _gov_filter
            from models.model_router import hint_to_model_id as _gov_hint_to_model

            # hint_to_model_id resolves any hint string → concrete model ID
            # using _HINT_MAP + model_registry constants (all .env-driven).
            # Returns None for "simple"/"local" (local LLM), "local:<id>" as-is.
            _gov_model = _gov_hint_to_model(_model_hint)

            # "local" / "simple" hint — the actual model name is in q.local_model
            if _gov_model is None and q.local_model:
                _gov_model = f"local:{q.local_model}"

            if _gov_model:
                from db.database import SessionLocal as _GovSessionLocal
                _gov_db = _GovSessionLocal()
                try:
                    _gov_allowed = _gov_filter(
                        [_gov_model], _user_id, _user_dept, _gov_db
                    )
                finally:
                    _gov_db.close()

                if not _gov_allowed:
                    logger.warning(
                        f"[governance] BLOCKED | user={_user_id} "
                        f"dept={_user_dept!r} model={_gov_model!r} "
                        f"hint={_model_hint!r}"
                    )
                    from fastapi.responses import JSONResponse as _GovJSONResponse
                    return _GovJSONResponse(
                        status_code=403,
                        content={
                            "error": "model_not_allowed",
                            "detail": (
                                f"Your department does not have access to the "
                                f"requested model. Please contact your administrator."
                            ),
                            "code": "MODEL_GOVERNANCE_BLOCKED",
                        },
                    )
        except Exception as _gov_err:
            # Fail open — a governance check error must never block a request.
            logger.warning(f"[governance] check error (fail-open): {_gov_err}")
    # ── END MODEL GOVERNANCE ENFORCEMENT ─────────────────────────────────────

    # ========================================================
    # STEP 0b2: AGENT CONTEXT LOOKUP (no injection yet)
    # When agent_id is provided (Agent Catalog chat), look up the agent's
    # system_prompt and KB namespace. The system_prompt will be injected
    # AFTER the compliance gate (see STEP 1a below) to avoid false-positive
    # PCI/PII blocks on platform-controlled agent instructions.
    # ========================================================
    _agent_system_prompt = None
    _agent_kb_namespace  = None
    if q.agent_id:
        try:
            from db.database import SessionLocal as _AgDB
            from db.models import AgentRecord as _AgRec
            _agdb = _AgDB()
            try:
                _ag = _agdb.query(_AgRec).filter(
                    _AgRec.name    == q.agent_id,
                    _AgRec.enabled == True,
                ).first()
                if _ag and _ag.system_prompt:
                    _agent_system_prompt = _ag.system_prompt.strip()
                if _ag:
                    _agent_kb_namespace = f"agent_kb:{q.agent_id}"
            finally:
                _agdb.close()
        except Exception as _ag_err:
            logger.warning(f"Agent context lookup failed: {_ag_err}")

    # ── Cowork office persona ────────────────────────────────────────────────
    # When the request comes from the Cowork tab (mode="office") and no specific
    # agent persona is set, frame the assistant as a non-technical office helper.
    # Connector results / attached documents arrive as context; this just shapes
    # tone + proactivity. The connector planning happens in orchestrator._plan_office.
    #
    # _cowork_role_prompt and _cowork_memory_str are extracted as named variables
    # so the KV-cache hoisting path (_build_local_system_message) can reference
    # them without re-computing or re-calling _cowork_memory_block.
    _cowork_role_prompt: str = ""
    _cowork_memory_str: str = ""
    if q.mode == "office" and not q.agent_id:
        _cowork_role_prompt = (
            "[ASSISTANT ROLE — AiNxt Cowork, an AI office assistant for an AiNxt employee]\n"
            "You help with everyday office work: reading and summarizing documents, drafting "
            "emails/updates, preparing reports, and pulling information from the user's connected "
            "apps (Outlook, Teams, GitLab, Jira, Confluence). Write for a non-technical audience — clear, "
            "concise, no code unless asked. When a deliverable (Word/Excel/PowerPoint/PDF) would help, "
            "offer to create it. Use only information from the provided context and tool results; "
            "never invent data from connected apps.\n"
            "CONNECTORS FIRST: the user's work lives in those remote apps, and they will almost never "
            "name the system — \"my open MRs\" is a GitLab question, \"the status of ABC-123\" is a Jira "
            "question, \"any tickets assigned to me\" is a Jira question. Treat such requests as "
            "connector requests and answer from the connector results provided to you. NEVER answer them "
            "from your own knowledge, from a shell/terminal, from `git`, or from the local filesystem — "
            "GitLab and Jira are remote servers that only the connectors can reach. If the needed "
            "connector results are absent, say the connector isn't connected and point the user to "
            "Profile → API Token Vault (GitLab needs a Personal Access Token; Jira needs "
            "email:api_token) — do not guess or fabricate issue/MR data.\n"
            "SENDING (email / Teams): you must NEVER send on your own. When the user asks to send an "
            "email or post to Teams, FIRST write the draft, then on a new line emit EXACTLY one marker "
            "the app turns into a confirm-and-send card:\n"
            "  [SENDPROPOSAL:{\"connector\":\"microsoft_365\",\"tool\":\"outlook_send_mail\",\"params\":{\"to\":\"\",\"subject\":\"\",\"body\":\"<draft>\"}}]\n"
            "  or for Teams group/1:1 chats: [SENDPROPOSAL:{\"connector\":\"microsoft_365\",\"tool\":\"teams_send_chat_message\",\"params\":{\"chat_id\":\"<id from teams_list_chats>\",\"message\":\"<draft>\"}}]\n"
            "For Teams CHANNELS only, use: [SENDPROPOSAL:{\"connector\":\"microsoft_365\",\"tool\":\"teams_send_message\",\"params\":{\"team_id\":\"\",\"channel_id\":\"\",\"message\":\"<draft>\"}}]\n"
            "If the user asks to send to a Teams group chat by name, first use teams_list_chats with name_contains and put the returned chat_id in the proposal; do NOT use teams_start_chat for group chats. "
            "ATTACHMENTS — you CAN and MUST attach files when the user asks. Add the appropriate key(s) to the proposal params:\n"
            "  • User uploaded a file in Buddy chat → use attachment_ids: [\"<ChatAttachment id>\"]\n"
            "  • User names a local file (e.g. 'AiNxt-Buddy.xlsx', 'report.pdf') → use attachment_file_path: \"<filename>\" — the system resolves it from Downloads/Desktop/Documents automatically. NEVER refuse or offer to rebuild it; just pass the filename as-is.\n"
            "  • Multiple local files → use attachment_file_paths: [\"file1.xlsx\", \"file2.pdf\"]\n"
            "  • File built with build_document → use attachment_job_id: \"<job id from DOCJOB marker>\"\n"
            "Example with a local file: [SENDPROPOSAL:{\"connector\":\"microsoft_365\",\"tool\":\"outlook_send_mail\",\"params\":{\"to\":\"someone@ainxt.com\",\"subject\":\"Report\",\"body\":\"<draft>\",\"attachment_file_path\":\"AiNxt-Buddy.xlsx\"}}]\n"
            "Fill the body/message with your draft; leave unknown recipient/channel fields blank for the user to "
            "complete in the card. Emit the marker ONLY when the user explicitly asks to send/post.\n"
            "CALENDAR WRITES (cancel/reschedule): first find the exact event via calendar_list_events. "
            "If multiple meetings match, ask the user to choose. If exactly one matches and the user asked "
            "to cancel or reschedule, show the matched subject/time and emit EXACTLY one marker the app turns "
            "into a review-and-confirm card:\n"
            "  [ACTIONPROPOSAL:{\"connector\":\"microsoft_365\",\"tool\":\"calendar_update_event\",\"params\":{\"event_id\":\"<id>\",\"start\":\"YYYY-MM-DDTHH:MM:SS\",\"end\":\"YYYY-MM-DDTHH:MM:SS\"}}]\n"
            "  or for cancellation: [ACTIONPROPOSAL:{\"connector\":\"microsoft_365\",\"tool\":\"calendar_cancel_event\",\"params\":{\"event_id\":\"<id>\",\"comment\":\"\"}}]\n"
            "Never emit a calendar action marker without an event_id from calendar_list_events."
        )
        _cowork_memory_str = _cowork_memory_block(_user_id)
        original = (
            f"{_cowork_role_prompt}\n\n"
            f"{_cowork_memory_str}"
            f"[USER REQUEST]\n{original}"
        )

    # ── Word/Excel/PowerPoint document-editing mode ──────────────────────────
    # When the request comes from the Office add-in task pane (mode="doc_edit")
    # and no specific agent persona is set, frame the assistant as a lean
    # document editor with no file-system access. The document text is already
    # embedded in the prompt by the frontend; no connector planning is needed.
    elif q.mode == "doc_edit" and not q.agent_id:
        original = (
            "[ASSISTANT ROLE — AiNxt, editing content from a Word/Excel/"
            "PowerPoint task pane]\n"
            "The text below was selected or read directly from the open "
            "document. You have NO file system access and there is no "
            "\"working directory\" — the text given to you is the only "
            "source of truth. Never claim to read, open, list, or write "
            "any file, folder, or working directory; if you cannot see "
            "something, say so in terms of \"the document\"/\"the "
            "selection\", not files.\n"
            "Respond with ONLY the requested content, ready to be "
            "inserted directly back into the document as-is — no "
            "preamble, no closing remarks, no meta-commentary about "
            "what you did.\n\n"
            f"[USER REQUEST]\n{original}"
        )

    # ========================================================
    # STEP 0c: PCI / PII COMPLIANCE GATE  (raw question)
    # Must run on `original` BEFORE mask_pii() so Luhn / regex
    # sees the real digits — masked text defeats detection.
    #
    # CLI mode is a developer tool — developers query their own codebase
    # with full freedom. Compliance gates are for the platform (web/Slack/Teams)
    # where real end-user payment data flows. Skip entirely for CLI.
    # ========================================================

    from agents.compliance_engine import compliance_engine as _ce_ask
    # CLI mode: run compliance for redaction but never block — replace sensitive
    # substrings with placeholders so the user's flow is not interrupted.
    # See feedback_redact_dont_block.md — ainxt-cli replaces Kilo Code, hard
    # blocks would be a Day-1 abandonment trigger.
    logger.info(f"Step 1: compliance_engine check started ...")
    # CLI/Cowork is a tool-driven assistant over the user's OWN data: keep contact
    # identifiers (email/phone/UPI) in the prompt so connector tool calls resolve —
    # redacting "emails from x@ainxt.com" to "[EMAIL]" returned empty Graph
    # results. Card/secret/account types stay redacted + blocked as before.
    #
    # This applies to Buddy (the browser office assistant) too — it posts to /ask
    # with mode="office" (NOT cli_mode), so it must be included here or the Outlook
    # reply/send tool receives "[EMAIL]" and Graph resolves 0 recipients.
    _tool_driven = bool(q.cli_mode or q.mode == "office")
    _ask_keep = {"EMAIL", "MOBILE", "UPI"} if _tool_driven else None
    if _bypass_safety_filters:
        _ask_chk = {
            "blocked": False,
            "blocked_types": [],
            "findings": [],
            "redacted_text": original,
            "was_redacted": False,
            "redacted_types": [],
        }
        logger.info("COMPLIANCE SKIPPED — explicit local model selected")
    else:
        _ask_chk = _ce_ask.validate_input(original, keep_types=_ask_keep)
        if q.cli_mode and _ask_chk.get("blocked"):
            _ask_chk = {**_ask_chk, "blocked": False, "blocked_types": []}
            _redacted_types_cli = _ask_chk.get("redacted_types", [])
            if _redacted_types_cli:
                logger.info(f"COMPLIANCE REDACT (cli) → {_redacted_types_cli}")

    logger.info(f"compliance engine output")
    # ── Gate 2: HardBlock Engine (deterministic keyword/regex matching) ────────
    # Instant (<1ms), purely deterministic, zero false positives. Blocks all
    # prompts that contain explicit harmful keywords/patterns without any LLM call.
    #
    # Environment flags:
    #   SKIP_HARDBLOCK=true       — disable this gate entirely (dev/test)
    #   SKIP_HARDBLOCK=true       — disable this gate entirely (dev/test)
    #   HARDBLOCK_FAIL_OPEN=true  — on HardBlock engine error, allow the
    #                               request through instead of fail-closed.
    #                               Use only in dev; never in production.
    _hb_findings = []
    _skip_hbe = os.getenv("SKIP_HARDBLOCK", "").strip().lower() in ("1", "true", "yes")
    _hbe_fail_open = os.getenv("HARDBLOCK_FAIL_OPEN", "").strip().lower() in ("1", "true", "yes")
    logger.info(f"Step 2: HardBlockEngine check started ...")
    if _bypass_safety_filters:
        logger.info("HardBlockEngine SKIPPED — explicit local model selected")
    elif _skip_hbe:
        logger.warning("HardBlockEngine SKIPPED — SKIP_HARDBLOCK is set")
    else:
        try:
            from agents.hardblock_engine import hardblock_engine as _hbe
            # /ask prompt is always a direct user message, never a tool_result.
            hb = _hbe.check(original, is_tool_result=False)
            if hb["blocked"]:
                logger.warning(
                    "HardBlockEngine TRIGGERED → category=%s score=%.3f matched=%s",
                    hb["category"], hb.get("score", 0.0), hb["matched_phrases"],
                )
                _hb_findings.append({
                    "type":     "HARDBLOCK",
                    "value":    hb["matched_phrases"],
                    "category": hb["category"],
                    "score":    hb.get("score", 0.0),
                    "severity": "CRITICAL",
                    "blocked":  True,
                })
            else:
                logger.debug(
                    "HardBlockEngine: no match — score=%.3f",
                    hb.get("score", 0.0),
                )
        except Exception as _hbe_err:
            # Never let HardBlock failure silently pass a prompt —
            # fail closed: treat as blocked if the engine errors.
            # Set HARDBLOCK_FAIL_OPEN=true to override (dev only).
            logger.error(f"HardBlockEngine error → {_hbe_err} (fail_open={_hbe_fail_open})")
            if not _hbe_fail_open:
                _hb_findings.append({
                    "type":     "HARDBLOCK_ENGINE_ERROR",
                    "value":    str(_hbe_err),
                    "category": "internal",
                    "severity": "CRITICAL",
                    "blocked":  True,
                })

    # Merge HardBlock findings into compliance findings
    _ask_chk_findings = _ask_chk.get("findings", []) or []
    _ask_chk_findings.extend(_hb_findings)
    _ask_chk["findings"] = _ask_chk_findings

    # ── Compliance gate diagnostics (logs only) ─────────────────
    # Helps debug cases where a prompt "should" hardblock but doesn't.
    try:
        _blocked_flag = bool(_ask_chk.get("blocked")) or bool(_hb_findings)
        _findings = _ask_chk.get("findings", []) or []
        # Use blocked_types from compliance engine (authoritative list)
        _blocked_types = _ask_chk.get("blocked_types", [])
        _hb = [f for f in _findings if f.get("type") in ("HARDBLOCK", "HARDBLOCK_ENGINE_ERROR")]
        _hb_cat = _hb[0].get("category") if _hb else None
        logger.info(
            "COMPLIANCE_GATE_RESULT /ask blocked=%s blocked_types=%s hb_category=%s prompt_len=%d",
            _blocked_flag,
            _blocked_types,
            _hb_cat,
            len(original or ""),
        )
    except Exception:
        pass

    if _ask_chk.get("blocked") or _hb_findings:
        _all_findings = _ask_chk.get("findings", []) or []
        # Use blocked_types from compliance engine (authoritative list of types that caused block)
        # instead of filtering by f.get("blocked") which is redundant and error-prone.
        _ask_blocked = _ask_chk.get("blocked_types", [])

        # ── Classify block type: PCI/PII vs HardBlock (AI Safety) ──────
        _hardblock_findings = [
            f for f in _all_findings
            if f.get("type") in _ask_blocked and f.get("type") in ("HARDBLOCK", "HARDBLOCK_ENGINE_ERROR")
        ]
        _pci_findings = [
            f for f in _all_findings
            if f.get("type") in _ask_blocked and f.get("type") not in ("HARDBLOCK", "HARDBLOCK_ENGINE_ERROR")
        ]

        if _hardblock_findings:
            _block_category = _hardblock_findings[0].get("category", "unknown")
            _block_policy   = "AI Safety policy"
            _block_detail   = f"category={_block_category}"
            logger.warning(
                f"HARDBLOCK /ask → category={_block_category} types={_ask_blocked}"
            )
        else:
            # PCI/PII violation — derive a category from the violation types so the
            # audit log always carries a non-empty block_category for blocked prompts.
            _pci_types = [f.get("type", "") for f in _pci_findings if f.get("type")]
            _block_category = ",".join(_pci_types) if _pci_types else "pci_pii"
            _block_policy   = "compliance policy"
            _block_detail   = ", ".join(_pci_types)
            logger.warning(f"COMPLIANCE BLOCK /ask → {_ask_blocked}")

        # Block/allow decisions are fully deterministic via Gate 1 (compliance)
        # + Gate 2 (hardblock). Confidence is fixed at 0.0 (no probabilistic signal).
        _block_confidence = 0.0

        # ── Produce a Kafka event so compliance-blocked prompts are captured
        # in user_prompts.log by the kafka_consumer for monitoring/auditing.
        # This is fire-and-forget — failure must never affect the user response.
        try:
            import datetime as _dt_blk
            if not q.ephemeral:
              _kafka_produce(_TOPIC_CHAT_HISTORY, {
                "chat_id":             _chat_id,
                "user_id":             _user_id,
                "user_name":           (q.user_name or _user_ctx.get("name") or "").strip(),
                "login_id":            (q.login_id or _user_ctx.get("ad_username") or _user_ctx.get("email") or "").strip(),
                "request_id":          request_id,
                "question":            original,          # raw (pre-mask) prompt for audit
                "answer":              "",                 # no answer — blocked
                "compliance_blocked":  True,
                "block_reason":        ", ".join(_ask_blocked),
                "block_policy":        _block_policy,     # "compliance policy" | "AI Safety policy"
                "block_category":      _block_category,   # e.g. "criminal_justice" (HardBlock) or "pci_pan,pii_mobile" (PCI/PII)
                "confidence_score":    _block_confidence, # 0.0 – 1.0 (noisy-OR ensemble)
                "ts":                  _dt_blk.datetime.utcnow().isoformat(),
              }, key=_chat_id)
        except Exception:
            pass

        # ── Direct audit-log write (only when Kafka is disabled) ───
        # When KAFKA_ENABLED=true, the Kafka event above is consumed
        # by workers/kafka_consumer.py which writes the row into
        # user_prompts.log — running both paths would duplicate every
        # blocked entry. When Kafka is disabled (dev/local or planned
        # outage), Kafka emission is a no-op, so we write directly
        # here to guarantee the audit row still lands on disk.
        try:
            from core.kafka_producer import KAFKA_ENABLED as _KAFKA_ENABLED
        except Exception:
            _KAFKA_ENABLED = False

        if not _KAFKA_ENABLED:
            try:
                import datetime as _dt_audit
                from core.prompt_audit import log_user_prompt as _log_prompt
                _log_prompt(
                    timestamp          = _dt_audit.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                    user_id            = _user_id or "",
                    user_name          = (q.user_name or _user_ctx.get("name") or "").strip(),
                    login_id           = (q.login_id or _user_ctx.get("ad_username") or _user_ctx.get("email") or "").strip(),
                    chat_id            = _chat_id or "",
                    request_id         = request_id or "",
                    prompt             = original,
                    compliance_blocked = True,
                    block_reason       = ", ".join(_ask_blocked),
                    block_policy       = _block_policy,
                    block_category     = _block_category,
                    confidence_score   = _block_confidence,
                )
            except Exception as _pae:
                logger.warning(f"prompt_audit direct-write failed: {_pae}")

        # ── AiNxt Coach — record the compliance-blocked turn so users see it
        # in Query Explorer (observational, fire-and-forget).
        try:
            from core.coach_events import emit_coach_event
            _cs_cmp = getattr(request.state, "client_source", "web")
            if _cs_cmp == "platform":
                _ch_cmp = "web"
            elif _cs_cmp.startswith("ide-"):
                _ch_cmp = "mcp"
            else:
                _ch_cmp = _cs_cmp
            emit_coach_event(
                user_id=_user_id or "anonymous",
                channel=_ch_cmp,
                model=q.model or "compliance_blocked",
                prompt=q.question,
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
                latency_ms=0,
                request_id=request_id,
                thread_id=_chat_id,
                department=(_user_ctx or {}).get("department"),
                compliance_flags=sorted(set(_ask_blocked)) if _ask_blocked else ["compliance_blocked"],
            )
        except Exception:
            pass

        def _compliance_block_stream():
            yield "data: " + json.dumps({"t": f"⛔ Request blocked by {_block_policy}: {_block_detail}"}) + "\n\n"
            yield "data: " + json.dumps({"__meta__": {
                "tokens": 0, "in_tok": 0, "out_tok": 0,
                "cost": 0.0, "model": "compliance-gate", "latency": 0.0,
            }}) + "\n\n"

        return StreamingResponse(
            _compliance_block_stream(),
            media_type="text/event-stream",
            headers={
                "X-Request-ID":      request_id,
                "Cache-Control":     "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ========================================================
    # STEP 1a: INJECTION SCAN (ainxt-injection-svc)
    # Scan the user message for prompt injection attacks before it reaches
    # the LLM. Delegates to core.injection_guard which provides connection
    # reuse, LRU caching, and consistent block-reason formatting.
    # ========================================================
    from core.injection_guard import (
        ENABLE_INJECTION_SCAN as _inj_scan_enabled,
        _INJECTION_SCAN_FAIL_CLOSED as _inj_fail_closed,
        _scan_once as _inj_scan_once,
        _format_block_reason as _inj_format_block_reason,
        _injection_cache_get as _inj_cache_get,
        _injection_cache_put as _inj_cache_put,
    )
    if _inj_scan_enabled:
        _inj_text = (original or "").strip()
        if _inj_text:
            # ── Cache read (Redis → in-process LRU) ──────────────────────────
            _inj_cached = await _inj_cache_get("user", _inj_text)
            if _inj_cached is not None:
                logger.debug(
                    f"[injection-guard] cache HIT /ask req_id={request_id} user={_user_id}"
                )
                _inj_data = None  # skip scan — message is known-safe
            else:
                _inj_data = await _inj_scan_once(
                    chunks=[_inj_text],
                    provenance="user",
                    tool_names=[],
                    user_id=_user_id,
                    request_id=request_id,
                    raise_on_fail_closed=False,
                )
            # _inj_data is None on svc failure OR cache hit. Fail-closed →
            # refuse with SSE only on svc failure (cache hit sets _inj_cached).
            if _inj_data is None and _inj_cached is None and _inj_fail_closed:
                def _injection_unavail_stream():
                    yield "data: " + json.dumps({"t": "⛔ Security screening is unavailable — request refused. Contact your administrator."}) + "\n\n"
                    yield "data: " + json.dumps({"__meta__": {
                        "tokens": 0, "in_tok": 0, "out_tok": 0,
                        "cost": 0.0, "model": "injection-gate", "latency": 0.0,
                    }}) + "\n\n"
                return StreamingResponse(
                    _injection_unavail_stream(),
                    media_type="text/event-stream",
                    headers={
                        "X-Request-ID":      request_id,
                        "Cache-Control":     "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )
            if _inj_data is not None and not _inj_data.get("allowed", True):
                _inj_reason, _inj_display = _inj_format_block_reason(
                    _inj_data,
                    default_message_prefix="Your message was blocked by the security policy",
                )
                _inj_layer = _inj_data.get("blocked_by") or "injection-svc"
                logger.critical(
                    f"[injection-guard] BLOCKED /ask req_id={request_id} user={_user_id} "
                    f"layer={_inj_layer} reason={_inj_reason!r}"
                )

                def _injection_block_stream():
                    yield "data: " + json.dumps({"t": _inj_display}) + "\n\n"
                    yield "data: " + json.dumps({"__meta__": {
                        "tokens": 0, "in_tok": 0, "out_tok": 0,
                        "cost": 0.0, "model": "injection-gate", "latency": 0.0,
                    }}) + "\n\n"

                return StreamingResponse(
                    _injection_block_stream(),
                    media_type="text/event-stream",
                    headers={
                        "X-Request-ID":      request_id,
                        "Cache-Control":     "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )
            if _inj_data is not None and _inj_data.get("tainted"):
                for _inj_entry in (_inj_data.get("audit") or []):
                    logger.warning(
                        f"[injection-guard] TAINTED /ask user={_user_id} "
                        f"layer={_inj_entry.get('layer')} msg={_inj_entry.get('message', '')!r}"
                    )
            # ── Cache write (allowed scan result) ────────────────────────────
            if _inj_data is not None and _inj_data.get("allowed", True):
                await _inj_cache_put("user", _inj_text, "")

    # ========================================================
    # STEP 1: PII MASK
    # CLI agent mode: mask only the task text, preserve file context as-is.
    # Masking code that contains PII-like regex patterns (e.g. mobile number
    # matchers in compliance_engine.py) would corrupt the code sent to the LLM.
    # ========================================================

    if _bypass_safety_filters:
        safe_question = original
    elif q.cli_mode:
        # Use compliance-redacted text (PCI/secrets replaced with placeholders).
        # Falls back to original if redaction produced nothing — never null.
        safe_question = _ask_chk.get("redacted_text") or original
    else:
        safe_question = _ask_chk.get("redacted_text") or mask_pii(original)

    # ========================================================
    # STEP 1a: AGENT SYSTEM PROMPT INJECTION (after compliance gate)
    # Now that the compliance gate has passed on the user's question alone,
    # prepend the agent's system_prompt to original. This ensures:
    #   1. Compliance scanning runs on user content only (no false positives
    #      from platform-controlled agent instructions)
    #   2. The LLM still receives the full agent context (system prompt + question)
    #   3. safe_question (used for storage/retrieval) includes the agent context
    # ========================================================
    if _agent_system_prompt:
        original = (
            f"[AGENT INSTRUCTIONS — follow exactly]\n{_agent_system_prompt}\n\n"
            f"[USER QUESTION]\n{original}"
        )
        # Re-derive safe_question from the updated original (with agent prompt)
        if _bypass_safety_filters:
            safe_question = original
        elif q.cli_mode:
            safe_question = _ask_chk.get("redacted_text") or original
        else:
            safe_question = _ask_chk.get("redacted_text") or mask_pii(original)

    # ── Persona / tone injection ──────────────────────────────
    # _tone_pfx is applied ONLY to _question_with_history (the LLM prompt).
    # safe_question stays clean so storage, retrieval, cache keys are unaffected.
    _tone_pfx = ""
    if q.tone or q.user_name:
        _uname = (q.user_name or "").strip().split()[0] if q.user_name else ""
        if q.tone == "casual":
            _tone_pfx = (f"[STYLE INSTRUCTION: The user's name is {_uname}. Respond warmly and conversationally. Address them by name when natural. Keep responses human and friendly.]\n\n" if _uname
                         else "[STYLE INSTRUCTION: Respond warmly and conversationally. Be human and friendly.]\n\n")
        elif _uname:
            _tone_pfx = f"[CONTEXT: The user's name is {_uname}.]\n\n"

    add_trace(request_id, "PII masked")

    # ========================================================
    # FIX 2: CONVERSATION MEMORY — inject prior turns into
    # the question so the orchestrator has context from the
    # ongoing chat session (last 3 user/assistant pairs).
    # Cache key is still derived from the clean rewritten
    # question so the same question still hits cache.
    # ========================================================
    # ── Build multi-turn messages array ───────────────────────────────────────
    # _messages is the canonical context passed to all gateways.
    # It uses OpenAI-format [{role, content}] so every gateway (Claude, GPT,
    # Gemini, local) receives proper multi-turn context — not a flat text block.
    #
    # Structure:
    #   [optional cross-chat memory prefix in first user message]
    #   [history turns: alternating user / assistant]
    #   [current user turn (last entry, always)]
    # ──────────────────────────────────────────────────────────────────────────
    _messages: list = []                  # final multi-turn payload
    _question_with_history = safe_question  # kept as string for orchestrator path

    # History/memory redaction helper — gated by COMPLIANCE_SCAN_HISTORY.
    # Default OFF: prior turns and injected memory pass through unredacted (the
    # current user prompt is already scanned at Gate 1, line ~2708). Flip the flag
    # ON to redact PII/secrets carried in history (e.g. @file inclusions).
    def _hist_redact(_text: str) -> str:
        if _bypass_safety_filters:
            return _text
        from core.config import COMPLIANCE_SCAN_HISTORY as _SH
        if not _SH:
            return _text
        try:
            return _ce_ask.redact_text(_text)[0]
        except Exception:
            return _text

    # LLM-output redaction helper — gated by COMPLIANCE_SCAN_LLM_OUTPUT.
    # Default OFF: model output streams through unredacted. Flip ON to redact
    # PII/secrets from the model's response (streamed chunks + persisted history).
    # Cloud-model KB chat: always redact output regardless of the env flag —
    # KB chunks fed to cloud LLMs may contain PANs/cards/PII that must not
    # echo back verbatim in the answer. Local model selection bypasses this
    # entirely (_bypass_safety_filters=True).
    def _out_redact(_text: str) -> str:
        if _bypass_safety_filters:
            return _text
        _force_kb_out = (_rag_mode in {"auto", "on"})
        if not _force_kb_out:
            from core.config import COMPLIANCE_SCAN_LLM_OUTPUT as _SO
            if not _SO:
                return _text
        try:
            return _ce_ask.redact_text(_text)[0]
        except Exception:
            return _text

    # CLI client-side history: if the CLI sent explicit history, use it directly —
    # this is always authoritative and bypasses DB lookup (avoids timing/failure issues).
    # Each message is compliance-redacted: @file inclusions can carry .env / ssh keys
    # / credentials, and we must never forward those raw to the LLM.
    if q.cli_messages and isinstance(q.cli_messages, list):
        for _cm in q.cli_messages:
            if isinstance(_cm, dict) and _cm.get("role") in ("user", "assistant") and _cm.get("content"):
                _cli_content_raw = str(_cm["content"])[:2000]
                from core.config import COMPLIANCE_SCAN_HISTORY as _SCAN_HIST
                if _bypass_safety_filters or not _SCAN_HIST:
                    _cli_content_safe = _cli_content_raw
                else:
                    try:
                        _cli_content_safe, _cli_redacted_types = _ce_ask.redact_text(_cli_content_raw)
                        if _cli_redacted_types:
                            logger.info(f"COMPLIANCE REDACT (cli_messages) → {_cli_redacted_types}")
                    except Exception:
                        _cli_content_safe = _cli_content_raw
                _messages.append({"role": _cm["role"], "content": _cli_content_safe})
        if _messages:
            add_trace(request_id, f"cli_history={len(_messages)} msgs (client-provided, redacted)")

    try:
        from db.database import SessionLocal as _HistDB
        from db.models import ChatMessage as _CM
        from memory.chat_summarizer import get_chat_summary
        import re as _re

        # Phase 2: track whether history was compacted into a rolling summary
        # so the client can show a "earlier messages were summarized" notice.
        _used_summary = False
        _CHARS_PER_TOKEN  = 4
        # History budget bumped from 2 K → 150 K tokens. Modern models
        # (Claude Sonnet 4.6 = 200 K, GPT-5.4 = 256 K) handle this comfortably;
        # the old 2 K cap forced summarisation after ~8 turns and made long
        # chats feel amnesiac. Summarisation now triggers only when the full
        # history would otherwise exceed the model's working budget.
        # Phase C1 (Tier 2 + Tier 5): the compaction trigger is now model-aware.
        # _FLAT_TRIGGER_TOKENS is the historical flat floor; the effective
        # trigger raises it to window*fraction-reserved for large-window models
        # but NEVER lowers it below the flat floor (proven in C0 that a lower
        # trigger regresses small-window models into a lossy summary sooner).
        _FLAT_TRIGGER_TOKENS = 150_000
        # PIPELINE_V2: read the compaction fraction from the resolved DomainProfile
        # so a profile can tune it; off-path passes None → historical constant.
        # The flat_floor still guards against early compaction either way.
        _pv2_fraction = None
        if _PIPELINE_V2 and _rc is not None and _rc.policy is not None:
            _pv2_fraction = _rc.policy.usable_fraction
        _TRIGGER_TOKENS   = _usable_history_budget(_model_hint or _local_model, _FLAT_TRIGGER_TOKENS, _pv2_fraction)
        _RAW_TURNS        = 200      # max messages to load (covers 100 user/assistant pairs)
        _SUMMARY_TURNS    = 40       # recent raw turns kept on top of summary when over budget

        def _count_tokens(text: str) -> int:
            return max(1, len(text) // _CHARS_PER_TOKEN)

        def _clean_for_history(content: str, role: str = "assistant") -> str:
            """
            Return the message content ready for history injection.

            For USER messages: keep the text VERBATIM (only length-capped). The user's
            own words — including any JSON / YAML / CSV / tables / exact numeric values
            they injected — are the single most important context clue and must never be
            stripped. Destroying them here is what caused the model to answer "I don't
            have the JSON content" for structured-data recall.

            For ASSISTANT messages: strip code blocks / JSON / tables to prose only, so
            the model understands the outcome without re-processing large blocks.

            Cap length to bound token cost per historical turn (user cap is higher so
            injected payloads survive intact).
            """
            if role == "user":
                # Verbatim — collapse only runaway whitespace, then length-cap generously.
                text = _re.sub(r'\n{3,}', '\n\n', content)
                text = _re.sub(r'[ \t]{2,}', ' ', text).strip()
                if len(text) > 4000:
                    text = text[:3600] + " … " + text[-360:]
                return text

            # Assistant: compact structured blocks to prose.
            # Strip fenced code blocks → compact tag
            def _code_tag(m):
                lang  = (m.group(1) or "code").strip() or "code"
                lines = m.group(2).strip().splitlines()
                return f"[{lang} code: {len(lines)} lines]"
            text = _re.sub(r'```(\w*)\n?([\s\S]*?)```', _code_tag, content)
            text = _re.sub(r'(?m)^(    |\t).+', '', text)          # indented code
            text = _re.sub(r'\{[^}]{0,500}\}', '[data]', text)     # JSON/dict
            text = _re.sub(r'\[[^\]]{20,}\]',  '[list]',  text)    # large arrays
            text = _re.sub(r'https?://\S+', '', text)               # URLs
            text = _re.sub(r'^#{1,6}\s+', '', text, flags=_re.MULTILINE)
            text = _re.sub(r'\*{1,2}([^*\n]+)\*{1,2}', r'\1', text)
            text = _re.sub(r'`([^`\n]+)`', r'\1', text)
            text = _re.sub(r'^\s*\|.+', '', text, flags=_re.MULTILINE)  # tables
            text = _re.sub(r'\n{2,}', '\n', text)
            text = _re.sub(r'\s{2,}', ' ', text).strip()
            if len(text) > 1200:
                text = text[:1000] + " … " + text[-180:]
            return text

        # ── Load raw history turns (skip when CLI provided its own history) ──────
        # Priority: Redis (zero Kafka lag, hot cache) → Postgres (durable fallback).
        # Redis is written by both _general_stream() and response_stream() immediately
        # after each turn, so it is always fresher than Postgres (which goes via Kafka).
        if not _messages and not q.cli_mode:
            # ── 1. Redis first ────────────────────────────────────────────────
            try:
                from memory.redis_memory import RedisMemory as _RMhist
                _rmh = _RMhist()
                _redis_hist = _rmh.get_conversation(_chat_id, limit=_RAW_TURNS)
                if _redis_hist:
                    for _rm in _redis_hist:
                        _r_role = _rm.get("role", "")
                        if _r_role not in ("user", "assistant"):
                            continue
                        # Context isolation: when current request is Generic,
                        # exclude history turns that originated from KB/codebase.
                        if _rag_mode == "off":
                            _r_meta = _rm.get("metadata") or {}
                            _r_origin = _r_meta.get("rag_mode")
                            if _r_origin and _r_origin != "off":
                                continue
                        _r_content = _clean_for_history((_rm.get("content") or "").strip(), role=_r_role)
                        if not _r_content:
                            continue
                        if _r_role == "user":
                            _r_content = _hist_redact(_r_content)
                        _messages.append({"role": _r_role, "content": _r_content})
                    add_trace(request_id, f"memory=redis {len(_messages)} turns")
            except Exception as _redis_hist_err:
                logger.debug(f"Redis history fetch failed, falling back to Postgres: {_redis_hist_err}")

            # ── 2. Postgres fallback (Redis empty, expired, or failed) ────────
            if not _messages:
                _hdb = _HistDB()
                try:
                    _pg_q = _hdb.query(_CM).filter(
                        _CM.chat_id == _chat_id,
                        _CM.role.in_(["user", "assistant"]),
                    )
                    # Context isolation: Generic reads exclude KB/codebase turns
                    if _rag_mode == "off":
                        _pg_q = _pg_q.filter(_CM.rag_mode == "off")
                    _pg_hist = (
                        _pg_q
                        .order_by(_CM.created_at.desc())
                        .limit(_RAW_TURNS)
                        .all()
                    )
                finally:
                    _hdb.close()

                if _pg_hist:
                    _raw_tokens = sum(_count_tokens(_m.content or "") for _m in _pg_hist)

                    if _raw_tokens < _TRIGGER_TOKENS:
                        # ── BELOW THRESHOLD: use last N raw turns directly ────
                        for _m in reversed(_pg_hist):
                            _cleaned = _clean_for_history((_m.content or "").strip(), role=_m.role)
                            if _cleaned:
                                if _m.role == "user":
                                    _cleaned = _hist_redact(_cleaned)
                                _messages.append({"role": _m.role, "content": _cleaned})
                        add_trace(request_id, f"memory=pg-raw {len(_messages)} turns (~{_raw_tokens} tokens)")
                    else:
                        # ── ABOVE THRESHOLD: rolling summary + recent raw turns
                        # Context isolation: the rolling summary is a single merged
                        # text blob built from ALL turns (KB + Generic) — it cannot
                        # be filtered by rag_mode. For Generic requests, skip the
                        # summary and rely on the rag_mode-filtered recent raw turns.
                        _summary = get_chat_summary(_chat_id) if _rag_mode != "off" else ""
                        _hdb2 = _HistDB()
                        try:
                            _pg_q2 = _hdb2.query(_CM).filter(
                                _CM.chat_id == _chat_id,
                                _CM.role.in_(["user", "assistant"]),
                            )
                            if _rag_mode == "off":
                                _pg_q2 = _pg_q2.filter(_CM.rag_mode == "off")
                            _recent = (
                                _pg_q2
                                .order_by(_CM.created_at.desc())
                                .limit(_SUMMARY_TURNS)
                                .all()
                            )
                        finally:
                            _hdb2.close()

                    if _summary:
                        _used_summary = True  # Phase 2: history was compacted
                        _messages.append({"role": "user",      "content": "[Previous conversation summary] Please keep this context in mind for our ongoing discussion."})
                        _messages.append({"role": "assistant", "content": _summary})

                        for _m in reversed(_recent):
                            _cleaned = _clean_for_history((_m.content or "").strip(), role=_m.role)
                            if _cleaned:
                                if _m.role == "user":
                                    _cleaned = _hist_redact(_cleaned)
                                _messages.append({"role": _m.role, "content": _cleaned})

                        _mode = "pg-summary+recent" if _summary else "pg-recent-only"
                        add_trace(request_id, f"memory={_mode} (~{_raw_tokens} raw tokens)")

    except Exception as _mem_err:
        logger.debug(f"Memory inject failed: {_mem_err}")

    # ── Persistent image context (multi-turn image memory) ───────────────────
    # SCOPE: this block runs ONLY inside the /ask handler (ask_ai). It replays a
    # COMPACT caption for images shared earlier in THIS chat so follow-up turns
    # keep some context of the picture without re-sending image bytes. WEBCHAT/
    # Buddy ONLY: those uploads go through routers/chat_router.py, which persists
    # image_description/image_caption on the ChatAttachment row at upload time.
    # CLI images are untouched by this fix (CLI sends base64 images inline via
    # q.images and never gets a ChatAttachment row), so this block is a no-op
    # for CLI traffic — it just won't find any rows to replay.
    #
    # Why it is needed: the web frontend clears attachment_ids after the upload
    # turn, so /ask only injects attachment text for the CURRENT request — an
    # image only influenced turn 1; later turns lost it entirely. Here we look up
    # image ChatAttachment rows referenced by past chat_messages.attachment_ids
    # and inject their short image_caption.
    #
    # Guardrails: only image-kind rows; last _IMG_CTX_MAX images; caption capped
    # at _IMG_CTX_CHARS; redacted via _hist_redact; fully best-effort (never
    # breaks the turn). Does NOT re-send bytes and does NOT touch other endpoints.
    try:
        if _messages:   # same-chat history turns present (web/Buddy AND cli)
            _IMG_CTX_MAX   = 5     # most-recent N images to remember
            _IMG_CTX_CHARS = 600   # per-image caption budget
            from db.database import SessionLocal as _ImgDB
            from db.models import ChatMessage as _ImgCM, ChatAttachment as _ImgCA
            _img_db = _ImgDB()
            try:
                # Gather attachment_ids referenced by this chat's messages, newest first.
                _img_msgs = (
                    _img_db.query(_ImgCM.attachment_ids, _ImgCM.created_at)
                    .filter(
                        _ImgCM.chat_id == _chat_id,
                        _ImgCM.attachment_ids.isnot(None),
                    )
                    .order_by(_ImgCM.created_at.desc())
                    .limit(200)   # bound the scan; same order as history load
                    .all()
                )
                _att_ids_seen: list = []
                for _row in _img_msgs:
                    for _aid in (_row[0] or []):
                        if _aid and _aid not in _att_ids_seen:
                            _att_ids_seen.append(_aid)
                if _att_ids_seen:
                    # Resolve image rows that actually carry a caption.
                    _img_rows = (
                        _img_db.query(_ImgCA)
                        .filter(
                            _ImgCA.id.in_(_att_ids_seen),
                            _ImgCA.kind == "image",
                        )
                        .all()
                    )
                    # Preserve newest-first ordering from _att_ids_seen, cap to N.
                    _img_by_id = {r.id: r for r in _img_rows}
                    _ordered = [
                        _img_by_id[_aid] for _aid in _att_ids_seen
                        if _aid in _img_by_id
                    ][:_IMG_CTX_MAX]
                    _img_blocks = []
                    for _r in _ordered:
                        _cap = (_r.image_caption or (_r.image_description or "")[:_IMG_CTX_CHARS]).strip()
                        if not _cap:
                            continue
                        _cap = _hist_redact(_cap[:_IMG_CTX_CHARS])
                        _img_blocks.append(f"[Image previously shared: {_r.file_name}] {_cap}")
                        logger.info(
                            f"[DOCTRACE] L2-img history-image-inject | corr={request_id} "
                            f"attachment_id={_r.id} file={_r.file_name!r} caption_chars={len(_cap)}"
                        )
                    if _img_blocks:
                        _img_ctx = (
                            "[Earlier in this conversation the user shared image(s). "
                            "Descriptions for reference:]\n" + "\n".join(_img_blocks)
                        )
                        _messages.append({"role": "user", "content": _img_ctx})
                        add_trace(request_id, f"image_context injected ({len(_ordered)} images)")
            finally:
                _img_db.close()
    except Exception as _img_ctx_err:
        logger.debug(f"image context inject skipped: {_img_ctx_err}")

    # ── Structured-fact injection ────────────────────────────────────────────
    # Re-inject verbatim exact values (JSON/YAML/CSV fields, ratios, ordered
    # value histories) captured from earlier user turns. This guarantees exact
    # recall even after the rolling summary has compacted the raw history — the
    # root cause of structured-data / conversion-ratio / contradiction misses.
    # Skipped for Generic (rag off) requests only if history itself was skipped.
    try:
        if _messages:  # only when we actually have same-chat history
            from memory.structured_facts import get_facts_block as _get_facts_block
            _facts_block = _get_facts_block(_chat_id)
            if _facts_block:
                _facts_block = _hist_redact(_facts_block)
                _messages.append({"role": "user", "content": _facts_block})
                add_trace(request_id, f"structured_facts injected ({len(_facts_block)} chars)")
    except Exception as _sf_inj_err:
        logger.debug(f"structured facts inject skipped: {_sf_inj_err}")

    # ── Topic-shift detector — REMOVED (2026-05-29) ─────────────────────────
    # Previous implementation computed cosine similarity between the new
    # question and the last 3 turns; if max-sim < 0.25 it injected a
    # synthetic user/assistant pair telling the model "user shifted topic".
    #
    # Live measurement on a 6-turn convo with a deliberate Kafka→DNS shift:
    #   topic_sim = 0.479, 0.597, 0.598, 0.526, 0.606
    # The detector NEVER fired at any realistic shift because nomic-embed-text
    # vector space puts all technical conversation above 0.4 cosine. Raising
    # the threshold to fire would also fire on every legitimate follow-up.
    #
    # The approach is fundamentally wrong: forcing a system note + fake
    # acknowledgement turn into the conversation is more harmful than the
    # problem it tries to solve. Modern LLMs handle topic shifts natively.
    # Removed; rely on the model's own context handling.


    # ── Cross-chat user memory (persistent across all chats) ─────────────────
    # Retrieve distilled summaries from previous sessions for this user.
    # Injected as background context in the first message — separate from
    # the same-chat history above so the model knows what it is.
    # Skipped for CLI: client manages its own context via cli_messages, and the
    # Postgres get_user_memory + redact loop adds 100-300 ms per request.
    _cross_chat_ctx = ""
    _is_cli_q = _is_cli  # reuse top-level detection; avoids redundant header read
    if not _is_cli_q and _user_id and _user_id not in ("", "default"):
        try:
            _user_turns = _postgres_memory.get_user_memory(
                _user_id, limit=8,
                rag_mode_filter="off" if _rag_mode == "off" else None,
            )
            if _user_turns:
                _safe_turns = [_hist_redact(t) for t in _user_turns[-5:]]
                _cross_chat_ctx = (
                    "[Context from your previous sessions with this user]\n"
                    + "\n".join(f"- {t}" for t in _safe_turns)
                )
                add_trace(request_id, f"cross_chat_memory={len(_user_turns)} entries")
        except Exception as _xc_err:
            logger.debug(f"cross-chat memory fetch failed: {_xc_err}")

    # ── Custom Instructions (ChatGPT-style persona) ──────────────────────────
    # Two free-text blobs configured per user in /profile/custom-instructions.
    # Prepended to the first message so they read as a system-level steer
    # without us having to mutate the model_router's system_prompt path.
    _custom_about = ""
    _custom_style = ""
    if not _is_cli_q and _user_id and _user_id not in ("", "default"):
        try:
            logger.debug(f"persona detection in")
            from db.database import SessionLocal as _CISL
            from sqlalchemy import text as _ci_sql
            _cidb = _CISL()
            try:
                _ci_row = _cidb.execute(
                    _ci_sql("SELECT custom_about_user, custom_response_style FROM users WHERE id = :uid"),
                    {"uid": _user_id},
                ).fetchone()
                if _ci_row:
                    _custom_about = (_ci_row[0] or "").strip()[:2000]
                    _custom_style = (_ci_row[1] or "").strip()[:2000]
            finally:
                _cidb.close()
        except Exception as _ci_err:
            logger.debug(f"custom_instructions lookup failed: {_ci_err}")

    # ── Persona & Style layer ────────────────────────────────────────────────
    # Compose ONE persona block (casual-buddy baseline, mirroring the user's
    # detected tone/language from the CIL, professional dial-down on sensitive
    # domains) from the model-understood state + the memory we already fetched +
    # any learned feedback preference. Falls back to the legacy static assembly
    # when CHAT_PERSONA is off or on any error. Reuses _user_turns (no new query).

    # _fb_hint and _is_sensitive_domain are hoisted outside the CHAT_PERSONA
    # guard so the KV-cache hoisting path (_build_local_system_message) can
    # use them regardless of whether the full persona layer is active.
    _fb_hint = ""
    try:
        for _t in (_user_turns or []):
            _tl = str(_t).lower()
            if "prefer" in _tl and ("concise" in _tl or "short" in _tl
                                    or "casual" in _tl or "detail" in _tl
                                    or "formal" in _tl):
                _fb_hint = str(_t).strip()
                break
    except Exception:
        _fb_hint = ""

    # Sensitive-domain flag: used by compose_stable_persona to pick
    # professional vs casual baseline without re-running the full CIL.
    _is_sensitive_domain: bool = False
    try:
        from cil.persona import _is_sensitive as _persona_sensitive
        _is_sensitive_domain = _persona_sensitive(
            getattr(getattr(_rc, "conv_state", None), "domain", "general") if _rc else "general",
            safe_question,
        )
    except Exception:
        _is_sensitive_domain = False

    _ci_prefix = ""
    if _CHAT_PERSONA:
        try:
            from cil.persona import compose_persona as _compose_persona
            # Learned feedback preference (loop C) is stored in user memory with
            # a response_style_pref hint; surface it as the highest-signal nudge.
            _fb_hint = ""
            try:
                for _t in (_user_turns or []):
                    _tl = str(_t).lower()
                    if "prefer" in _tl and ("concise" in _tl or "short" in _tl
                                            or "casual" in _tl or "detail" in _tl
                                            or "formal" in _tl):
                        _fb_hint = str(_t).strip()
                        break
            except Exception:
                _fb_hint = ""
            _ci_prefix = _compose_persona(
                conv_state=(_rc.conv_state if (_rc is not None) else None),
                question=safe_question,
                user_name=(q.user_name or ""),
                custom_about=_custom_about,
                custom_style=_custom_style,
                memory_facts=list(_user_turns or []),
                feedback_hint=_fb_hint,
            )
        except Exception as _persona_err:
            logger.debug(f"persona compose failed, using legacy prefix: {_persona_err}")
            _ci_prefix = ""

    if not _ci_prefix:
        # Legacy static assembly (CHAT_PERSONA off or compose failed).
        _ci_prefix_parts: list = []
        if _custom_about:
            _ci_prefix_parts.append(f"[About the user]\n{_custom_about}")
        if _custom_style:
            _ci_prefix_parts.append(f"[Preferred response style]\n{_custom_style}")
        _ci_prefix = "\n\n".join(_ci_prefix_parts)

    # ── Assemble final current-turn user message ──────────────────────────────
    # Tone prefix goes into the current user message content only. When the
    # persona layer is active it OWNS tone (mirrors the user), so the legacy
    # static tone prefix is suppressed to avoid conflicting instructions.
    _persona_active = bool(_CHAT_PERSONA and _ci_prefix)
    _current_user_content = ("" if _persona_active else (_tone_pfx or "")) + safe_question

    # Cross-chat context prefix: prepend to the first history message if history
    # exists, otherwise prepend to the current message so it is never lost.
    # Custom Instructions go in front of cross-chat memory so the model sees
    # the persona first. When the persona layer is active it ALREADY folded the
    # user memory in, so we skip _cross_chat_ctx to avoid duplicating it.
    _system_preface_parts: list = []
    if _ci_prefix:
        _system_preface_parts.append(_ci_prefix)
    if _cross_chat_ctx and not _persona_active:
        _system_preface_parts.append(_cross_chat_ctx)

    # ── Piggyback memory decision on this LLM call ───────────────────────────
    # The LLM appends a hidden JSON footer at the very end of its response.
    # Gateway strips it before showing the user — zero extra LLM calls needed.
    # Uses the module-level _MEMORY_INSTRUCTION constant (single source of truth
    # shared with _build_local_system_message for the KV-cache hoisting path).
    _system_preface_parts.append(_MEMORY_INSTRUCTION)

    _system_preface = "\n\n".join(_system_preface_parts)

    if _system_preface:
        if _messages:
            _messages[0]["content"] = _system_preface + "\n\n" + _messages[0]["content"]
        else:
            _current_user_content = _system_preface + "\n\n" + _current_user_content

    # Append current user question as the final message
    _messages.append({"role": "user", "content": _current_user_content})

    # ── Phase 2: context-window telemetry ────────────────────────────────────
    # Estimate the tokens the assembled prompt occupies and pair it with the
    # target model's working window so the client can render a live meter and,
    # when history was compacted, a "earlier messages were summarized" notice.
    try:
        _ctx_used_tokens = max(
            1,
            sum(len(m.get("content") or "") for m in _messages) // 4,
        )
        _ctx_window = _context_window_for(_model_hint or _local_model)
        _context_info = {
            "tokens_used":    _ctx_used_tokens,
            "context_window": _ctx_window,
            "pct_used":       round(min(100.0, _ctx_used_tokens / _ctx_window * 100), 1),
            "recent_turns":   len(_messages),
            "compacted":      bool(_used_summary),
        }
    except Exception:
        _context_info = None

    # ── Orchestrator string path (agent.run still takes a str) ───────────────
    # Serialise _messages back to a flat string for the orchestrator so no
    # orchestrator internals need to change.
    _question_with_history = "\n".join(
        f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
        for m in _messages
    )
    # Apply legacy tone prefix to orchestrator string only when the persona
    # layer is NOT active (persona owns tone and is already in _messages[0]).
    if (not _persona_active) and _tone_pfx and not _question_with_history.startswith(_tone_pfx):
        _question_with_history = _tone_pfx + _question_with_history

    # ── Language detection ────────────────────────────────────
    _detected_lang = "unknown"
    try:
        from core.lang_detect import detect_language as _detect_lang
        _detected_lang = _detect_lang(original)
        add_trace(request_id, f"lang={_detected_lang}")
    except Exception:
        pass

    # ========================================================
    # STEP 2: CLASSIFY
    # ========================================================
    # CLI mode: skip classification entirely.
    # The CLI sends its own model hint — routing is already decided by the client.
    # classify_query_complexity makes a Redis call and, on cache miss, a Claude Haiku
    # LLM proxy call (up to 15 s) — completely wasted work for every CLI request.
    #
    # Generic chat mode (rag_mode="off", no repo/project scope): classify + rewrite
    # exist solely to route retrieval. With KB probe disabled they're dead weight
    # (+200–1300 ms LLM cost per turn). Skip them too, mirroring CLI behavior.
    _is_cli_early = _is_cli  # reuse top-level detection; avoids redundant header read
    _skip_classify_rewrite = (
            _is_cli_early
            or _runtime_will_handle
            or (
                    _rag_mode == "off"
                    and not q.project_id
                    and not q.repo_filter
                    and not q.voice_platform
            )
    )

    if _skip_classify_rewrite:
        query_type = "medium"          # placeholder — unused on direct paths
        add_trace(request_id, "type=skip_classify")
    else:
        query_type = classify_query_complexity(safe_question)
        add_trace(request_id, f"type={query_type}")
    logger.info(f"_skip_classify_rewrite={_skip_classify_rewrite}")


    # ========================================================
    # STEP 3: REWRITE
    # ========================================================

    # CLI mode + Generic chat: skip rewrite — same reason as classify.
    # Otherwise: only rewrite code-domain questions.
    if _skip_classify_rewrite:
        rewritten = safe_question
    else:
        from models.classifier import detect_query_domain as _detect_domain
        _q_domain = _detect_domain(safe_question)
        rewritten = (
            rewrite_query(safe_question)
            if query_type != "simple" and _q_domain == "code"
            else safe_question
        )

    logger.info(f"REWRITTEN={rewritten}")


    # ========================================================
    # STEP 4: DETECT REPO (SAFE)
    # ========================================================

    # Explicit repo_filter from request body takes highest priority (Threads, IDE).
    # Do NOT fall back to text-based detection on plain chat — repo context must
    # be explicitly scoped (via repo_filter or project_id), never inferred from
    # keyword matches in the user's message.
    repo_filter = q.repo_filter or None

    # If a project with a repo is scoped, use the project repo as the
    # authoritative repo_filter (overrides text-based detection).
    if q.project_id and not repo_filter:
        try:
            from db.database import SessionLocal as _PrjSess
            from db.models import ProjectRecord as _PrjRec
            _prj_sess = _PrjSess()
            try:
                _prj = _prj_sess.query(_PrjRec).filter(_PrjRec.id == q.project_id).first()
                if _prj and _prj.repo_name:
                    repo_filter = _prj.repo_name
                    logger.info(f"repo_filter set from project {q.project_id}: {repo_filter}")
            finally:
                _prj_sess.close()
        except Exception as _prj_err:
            logger.warning(f"Could not fetch project repo: {_prj_err}")

    add_trace(request_id, f"repo={repo_filter}")


    # ========================================================
    # STEP 5: CACHE CHECK
    # ========================================================

    key = cache_key(rewritten, repo_filter, _model_hint, user_id=_user_id, rag_mode=_rag_mode)

    # CLI requests inject live file context — never serve from cache (content changes
    # per turn and stale blocked responses would be re-served incorrectly)
    cached = None if q.cli_mode else redis_client.get(key)

    if cached:

        logger.info("CACHE HIT")
        logger.info(
            f"[CacheMetric] source=redis  llm_bypassed=true  user={_user_id}  "
            f"request_id={request_id}"
        )
        _record_bypass_metric("redis", _user_id, repo_filter)

        try:

            data = json.loads(cached)

            def _cached_stream():
                yield "data: " + json.dumps({"t": data["answer"]}) + "\n\n"

                # Budget info for cached response — pulled from a per-process
                # 60s in-memory cache. Without this we hit Postgres on every
                # cached answer, adding 200-500ms to a request that should be
                # near-zero latency. Stale-by-60s is fine for display purposes.
                _budget_info = _get_budget_info_cached(_user_id)

                _cached_meta = {
                    "tokens":  0,
                    "in_tok":  0,
                    "out_tok": 0,
                    "cost":    0.0,
                    "model":   "cached",
                    "latency": round(time.time() - start_time, 3),
                    "source":  "redis",
                    "llm_used": False,
                    **_budget_info,
                }
                yield "data: " + json.dumps({"__meta__": _cached_meta}) + "\n\n"
                _emit_coach(_cached_meta)

            return StreamingResponse(

                _cached_stream(),

                media_type="text/event-stream",

                headers={
                    "X-Cache":           "HIT",
                    "X-Request-ID":      request_id,
                    "Cache-Control":     "no-cache",
                    "X-Accel-Buffering": "no",
                }

            )

        except Exception:

            pass


    add_trace(request_id, "cache miss")

    # ========================================================
    # STEP 5.2: SEMANTIC ANSWER CACHE (L2)
    # Cosine-similarity search over past Q&A pairs in pgvector.
    # Threshold: 0.92 — near-identical intent required to return
    # a cached answer. Falls through to RAG+LLM on miss.
    # ========================================================
    try:
        from store.semantic_cache import (
            get_semantic_cached_answer,
            get_semantic_memory,
            format_memory_for_prompt,
        )
        _sem_hit = None if (not _SEMANTIC_CACHE_ENABLED or q.cli_mode) else get_semantic_cached_answer(
            rewritten,
            repo_filter=repo_filter,
            user_id=_user_id,
            rag_mode=_rag_mode,
        )
        if _sem_hit:
            add_trace(request_id, "semantic cache hit")
            logger.info(
                f"[SemanticCache] STEP 5.2 HIT  "
                f"similarity={_sem_hit['similarity']:.3f}  user={_user_id}"
            )
            logger.info(
                f"[CacheMetric] source=semantic  llm_bypassed=true  "
                f"similarity={_sem_hit['similarity']:.3f}  user={_user_id}  "
                f"request_id={request_id}"
            )
            _record_bypass_metric("semantic", _user_id, repo_filter)
            _sem_answer = _sem_hit["answer"]

            def _sem_cached_stream():
                yield "data: " + json.dumps({"t": _sem_answer}) + "\n\n"
                _sem_meta = {
                    "tokens":  0, "in_tok": 0, "out_tok": 0,
                    "cost":    0.0, "model": "semantic-cache",
                    "latency": round(time.time() - start_time, 3),
                    "source":  "semantic_cache",
                    "llm_used": False,
                }
                yield "data: " + json.dumps({"__meta__": _sem_meta}) + "\n\n"
                _emit_coach(_sem_meta)

            return StreamingResponse(
                _sem_cached_stream(),
                media_type="text/event-stream",
                headers={
                    "X-Cache":           "SEMANTIC-HIT",
                    "X-Similarity":      str(round(_sem_hit["similarity"], 3)),
                    "X-Request-ID":      request_id,
                    "Cache-Control":     "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
    except Exception as _sem_exc:
        logger.warning(f"[SemanticCache] STEP 5.2 error (skipping): {_sem_exc}")

    # ========================================================
    # STEP 5.3: SEMANTIC MEMORY INJECTION (L3)
    # Retrieve learned patterns relevant to this query and
    # inject them as a preamble into the question so the LLM
    # can reuse past successful reasoning.
    # Skipped for CLI mode — CLI manages its own context;
    # the pgvector similarity search adds 200-500 ms per request.
    # ========================================================
    if not _is_cli_early:
        try:
            _mem_results = get_semantic_memory(
                rewritten,
                user_id=_user_id,
                department=_user_dept or None,
                rag_mode=_rag_mode,
            )
            if _mem_results:
                _mem_block = format_memory_for_prompt(_mem_results)
                if _mem_block:
                    rewritten = f"{_mem_block}\n\n{rewritten}"
                    add_trace(request_id, f"semantic memory injected ({len(_mem_results)} patterns)")
                    logger.info(
                        f"[SemanticMemory] STEP 5.3 injected {len(_mem_results)} patterns "
                        f"into prompt  user={_user_id}  dept={_user_dept or 'none'}"
                    )
        except Exception as _mem_exc:
            logger.warning(f"[SemanticMemory] STEP 5.3 error (skipping): {_mem_exc}")

    # ========================================================
    # STEP 5.5: GENERIC FAST-PATH
    # All non-trivial chat requests (no repo_filter, no project_id) probe the
    # knowledge base first.  If relevant chunks are found (score ≥ 0.35) they
    # are injected as grounded context; otherwise the LLM answers from priors.
    # Trivial queries (greetings, small-talk) skip the probe entirely.
    #
    # EXCLUDED from fast-path:
    #   - requests with repo_filter set (codebase-scoped, handled by orchestrator)
    #   - requests with project_id set (project-scoped, handled by orchestrator)
    #   - trivial queries (greetings, small-talk, simple arithmetic)
    #   - Office/Buddy requests (need connector planning)
    # ========================================================
    _is_voice_platform = bool(q.voice_platform)
    _is_trivial_q      = bool(_TRIVIAL_QUERY_RE.match(safe_question.strip()))

    # ── KB probe gate ──────────────────────────────────────────────────────
    # rag_mode="off" (default for Chat) skips the probe entirely — no
    # classification, no retrieval, no source pollution. Generic LLM only.
    # voice_platform is always treated as "on" (its prompt depends on KB).
    # rag_mode="auto"/"on" → probe runs.
    # The outer fast-path branch must still execute for rag_mode="off" and
    # for trivial queries, so we always stream a direct model response
    # without dropping through to the orchestrator (which adds plan() cost).
    #
    # Computed HERE (moved up from its previous spot just below the
    # follow-up block) so the follow-up block below can gate its expensive
    # LLM-based condensation on it — see that block's comment for why.
    _kb_probe_enabled = bool(_is_voice_platform) or (_rag_mode in {"auto", "on"})
    _fp_sources_meta: list = []  # populated when KB probe runs; emitted in __meta__

    # ── Follow-up detection + condensation ──────────────────────────────────
    # Two DIFFERENT signals, kept deliberately separate:
    #
    # 1. _is_followup (cheap, regex-based, ALWAYS computed) — a lightweight,
    #    zero-LLM-cost heuristic: short question + no new named technical
    #    entity anchoring it to a new topic. This is a GENERAL-PURPOSE signal
    #    consumed outside the KB path too (e.g. the CIL "clarification_needed"
    #    gate further down uses it to avoid asking "please clarify" when a
    #    user just replies "yes"/"tell me more" in ANY chat, KB or not) —
    #    this is the exact heuristic that lived here before the KB-specific
    #    condensation work, restored so non-KB chats are completely
    #    unaffected by anything KB-related, exactly as before.
    #
    # 2. The LLM-based standalone-question CONDENSATION (below, KB-only) —
    #    only runs when _kb_probe_enabled is True (i.e. this is genuinely a
    #    KB Chat / voice-platform request). It asks a cheap/fast LLM to
    #    decide for itself whether the question is self-contained or
    #    depends on the conversation, and rewrites it into a standalone
    #    question when it does. This is deliberately NOT gated purely on
    #    "_has_history" any more — running it for every non-KB chat turn
    #    with history (as an earlier version of this fix did) meant regular
    #    Chat was silently paying for an LLM call whose result it never
    #    used for retrieval, only to feed the same clarification-gate signal
    #    the cheap heuristic above already provides for free. When
    #    condensation runs and succeeds, its (more accurate) verdict
    #    OVERRIDES the cheap heuristic's _is_followup value for this turn —
    #    KB Chat keeps the full benefit of LLM-based judgment; non-KB chats
    #    never invoke it at all.
    #
    # _bm25_query mirrors _rag_query for BM25 (keyword_search). The condensed
    # standalone question is always short/single-sentence (capped — see
    # followup_condenser.py's _MAX_STANDALONE_LEN) so it's safe for BM25's
    # tsquery builder in both the changed and unchanged case; no separate
    # bare-question handling is needed for BM25 any more.
    def _is_followup_question(q_text: str, history: list) -> bool:
        """Return True when the question is a short follow-up with no new entities.

        This is the ORIGINAL follow-up heuristic (unchanged from before KB
        Chat's LLM-based condensation existed) — cheap, zero-LLM-cost,
        general-purpose. Used for EVERY chat turn regardless of KB status
        (see block comment above). KB Chat's more expensive LLM-based
        condensation is layered on top of this, KB-only, further below."""
        _prior = [m for m in history if m.get("role") == "assistant"]
        if not _prior:
            return False
        if len(q_text.strip()) > 120:
            return False
        # Named technical entities that anchor the question to a NEW topic
        _has_entity = bool(re.search(
            r'[@/\\]|\.py\b|\.ts\b|\.js\b|\.java\b|\.go\b|\.rs\b'  # file refs
            r'|https?://'                                              # URLs
            r'|[A-Z][a-z]+[A-Z]'                                      # CamelCase
            r'|`[^`]+`'                                                # backtick names
            r'|\b[A-Z_]{3,}\b',                                       # CONST_NAMES
            q_text,
        ))
        return not _has_entity

    _has_history = len([m for m in _messages if m.get("role") == "assistant"]) > 0
    _is_followup = _is_followup_question(safe_question, _messages)
    _rag_query   = safe_question
    _bm25_query  = safe_question
    if _has_history and _kb_probe_enabled:
        from core.config import KB_FOLLOWUP_CONDENSE_ENABLED as _KB_CONDENSE_ON
        if _KB_CONDENSE_ON:
            try:
                from models.followup_condenser import condense_followup
                _condensed = condense_followup(safe_question, _messages, chat_id=q.chat_id)
            except Exception as _condense_exc:
                logger.warning(f"followup condense call failed ({_condense_exc}) — using bare question")
                _condensed = None
            if _condensed and _condensed.strip() and _condensed.strip() != safe_question.strip():
                _rag_query   = _condensed.strip()
                _bm25_query  = _rag_query
                _is_followup = True  # LLM-verified — overrides the cheap heuristic above
                add_trace(request_id, f"followup=true standalone_q={_rag_query[:150]!r}")
        # KB_FOLLOWUP_CONDENSE_ENABLED=false → condensation is skipped (kill-
        # switch): KB retrieval uses the bare question, but _is_followup still
        # carries the cheap heuristic's answer from above (unaffected by the
        # kill-switch — that heuristic isn't part of what the switch disables).

    # ── Phase 3 dispatch DRIVING (PIPELINE_V2_DISPATCH, default OFF) ──────────
    # When enabled and the CIL produced a shape, let a genuinely-agentic
    # no-repo/no-project turn (tool_use/decompose) SKIP the fast-path block
    # below. This can ONLY push toward more processing — it never diverts a
    # repo/project request (guard requires neither set; decide_fork also
    # enforces repo/project→orchestrator) and never forces the fast path.
    #
    # SEMANTICS OF SKIPPING THE FAST PATH: skipping the `if` block does NOT jump
    # straight to response_stream — it falls through the intent-route and CLI
    # lanes first. This is INTENDED: an explicit skill/agent intent-route or a
    # CLI turn is a MORE SPECIFIC lane and correctly outranks a heuristic shape
    # promotion. A promoted turn only reaches the orchestrator (response_stream)
    # when it matches neither of those lanes. With the flag OFF or no conv_state
    # this whole block is skipped and the fork is byte-identical to today.
    _pv2_force_orchestrator = False
    # Pre-initialize KB probe outputs so downstream closures (e.g. _general_stream)
    # never see an unassigned name when the KB probe branch is skipped (repo/project
    # requests, office mode, or forced-orchestrator dispatch).
    _docs_context = ""
    if (_PIPELINE_V2 and _PIPELINE_V2_DISPATCH and _rc is not None
            and _rc.conv_state is not None and not repo_filter and not q.project_id):
        try:
            if _decide_fork(_rc.conv_state, repo_filter=repo_filter,
                            project_id=q.project_id) == _FORK_ORCHESTRATOR:
                _pv2_force_orchestrator = True
                _otel.record_event("dispatch.driven", to="orchestrator")
        except Exception:  # noqa: BLE001 — never break dispatch
            _pv2_force_orchestrator = False

    if not repo_filter and not q.project_id and q.mode != "office" and not _pv2_force_orchestrator:
        # ── KB retrieval (delegated to core/kb_retrieval.py) ─────────────
        # All namespace discovery, pgvector+BM25 search, BGE reranking,
        # disambiguation gate, and coverage retrieval live in that module.
        # This keeps the Chat path (ainxt-api, canary, doc-ctx) completely
        # isolated from KB retrieval — Chat PRs never touch kb_retrieval.py.
        _docs_context    = ""
        _fp_sources_meta = []   # re-initialised here; also set at line 6298 above
        try:
            from core.kb_retrieval import run_kb_retrieval as _run_kb_retrieval
            _redis_ns_client = get_kv(RDB_CACHE, decode_responses=True)
            _kb_result = _run_kb_retrieval(
                safe_question       = safe_question,
                rag_query           = _rag_query,
                bm25_query          = _bm25_query,
                is_trivial_q        = _is_trivial_q,
                kb_probe_enabled    = _kb_probe_enabled,
                runtime_will_handle = _runtime_will_handle,
                is_followup         = _is_followup,
                has_history         = _has_history,
                user_ctx            = _user_ctx,
                chat_scope_doc_ids  = _chat_scope_doc_ids,
                agent_kb_namespace  = _agent_kb_namespace,
                rag_mode            = _rag_mode,
                request_id          = request_id,
                redis_ns_client     = _redis_ns_client,
            )
            # Disambiguation: return __clarify__ SSE immediately (no LLM call)
            if _kb_result.disambig_payload:
                _dp = _kb_result.disambig_payload
                def _disambig_stream(
                    _msg=_dp["message"],
                    _cands=_dp["candidates"],
                    _q=_dp["question"],
                    _rm=_dp["rag_mode"],
                ):
                    yield "data: " + json.dumps({
                        "__clarify__": {
                            "question":    _q,
                            "message":     _msg,
                            "candidates":  _cands,
                            "multi_select": True,
                        }
                    }) + "\n\n"
                    yield "data: " + json.dumps({
                        "__meta__": {
                            "out_tok":  0,
                            "in_tok":   0,
                            "model":    "kb-disambig",
                            "cost":     0.0,
                            "latency":  0.0,
                            "source":   "kb_disambig",
                            "llm_used": False,
                            "rag_mode": _rm,
                        }
                    }) + "\n\n"
                return StreamingResponse(
                    _disambig_stream(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control":     "no-cache",
                        "X-Accel-Buffering": "no",
                        "X-Request-ID":      request_id,
                    },
                )
            # Set closure variables that _general_stream() reads
            _docs_context    = _kb_result.docs_context
            _fp_sources_meta = _kb_result.sources_meta
        except Exception as _kb_err:
            logger.warning(f"[ask] KB retrieval failed (non-fatal): {_kb_err}")
            # _docs_context and _fp_sources_meta stay as empty defaults above —
            # the LLM answers from general knowledge (graceful degradation).

        # Effective model hint for fast-path streaming.
        # Voice platform always uses "complex" (Claude) for best natural language quality.
        _fp_hint = "complex" if _is_voice_platform else (_model_hint or "medium")

        # ── Phase 3 router DRIVING (PIPELINE_V2_ROUTING, default OFF) ─────────
        # When the user did NOT force a model (_model_hint is None → the flat
        # "medium" default above) and this is not a voice turn, let the CIL's
        # task_complexity pick the router tier instead of always "medium". The
        # CIL vocabulary (simple|medium|complex|deep|solution) is identical to
        # the router's _HINT_MAP, so we pass it straight through. This only
        # applies when the user chose "auto" — an explicit model is never
        # overridden. Flag OFF / forced-model / no conv_state → unchanged.
        if (_PIPELINE_V2 and _PIPELINE_V2_ROUTING and not _is_voice_platform
                and not _model_hint and _rc is not None and _rc.conv_state is not None):
            _cil_tier = getattr(_rc.conv_state, "task_complexity", None)
            if _cil_tier in _PV2_TIER_HINTS:
                _fp_hint = _cil_tier
                _otel.record_event("routing.driven", tier=_cil_tier)

        # ── AUTO-ROUTING GOVERNANCE FILTER ───────────────────────────────────
        # When the user chose Auto (_model_hint is None), the router has now
        # resolved _fp_hint to a tier (e.g. "medium" → gpt-5.4, "complex" →
        # claude-sonnet-4-6).  Check whether that resolved model is allowed for
        # this user/dept.  If it is blocked, walk down a fallback chain to the
        # next allowed model instead of hard-blocking (the user asked for "Auto",
        # not for a specific model, so a silent fallback is the right UX).
        #
        # When ALL cloud models are blocked the chain ends at "simple" (local).
        # In that case _gov_local_only is set to True so that if the local model
        # is also unavailable we return a clear error instead of silently
        # escaping to a governance-blocked cloud provider.
        # Fails open on any DB / import error — governance never takes down chat.
        _gov_local_only = False   # set True when all cloud models are blocked
        if not _model_hint:
            try:
                from routers.model_governance_router import filter_allowed_models as _gov_filter_auto
                from db.database import SessionLocal as _GovSessionLocal
                from models.model_router import hint_to_model_id as _auto_hint_to_model
                from core.model_registry import CHAT_FALLBACK_CHAIN as _AUTO_FALLBACK_CHAIN

                # Helper: get all local model IDs available on this server
                def _get_local_model_ids():
                    try:
                        from gateway_local_llm import get_local_gateway as _glg
                        _lgw = _glg()
                        return [f"local:{m}" for m in (_lgw.list_models() or [])]
                    except Exception:
                        return []

                _auto_model = _auto_hint_to_model(_fp_hint)
                # For "simple"/"local" tier _auto_model is None — resolve local IDs now
                if _auto_model is None and _fp_hint in ("simple", "local"):
                    _local_ids = _get_local_model_ids()
                    if _local_ids:
                        _loc_db = _GovSessionLocal()
                        try:
                            _loc_allowed = _gov_filter_auto(
                                _local_ids, _user_id, _user_dept, _loc_db
                            )
                        finally:
                            _loc_db.close()
                        if not _loc_allowed:
                            # All local models also blocked — hard block
                            _gov_local_only = True
                            _auto_model = "__all_local_blocked__"  # sentinel to trigger error below
                        # else: at least one local model allowed — proceed normally
                    # If no local models configured, proceed (local gateway unavailable)

                if _auto_model and _auto_model != "__all_local_blocked__":
                    _auto_db = _GovSessionLocal()
                    try:
                        _auto_allowed = _gov_filter_auto(
                            [_auto_model], _user_id, _user_dept, _auto_db
                        )
                    finally:
                        _auto_db.close()

                    if not _auto_allowed:
                        logger.warning(
                            f"[governance/auto] auto-picked model blocked | "
                            f"user={_user_id} dept={_user_dept!r} "
                            f"tier={_fp_hint!r} model={_auto_model!r} "
                            f"— trying fallback chain"
                        )
                        _fallback_found = False
                        for _fb_tier in _AUTO_FALLBACK_CHAIN:
                            if _fb_tier == _fp_hint:
                                continue  # skip the already-blocked tier
                            _fb_model = _auto_hint_to_model(_fb_tier)
                            if _fb_model is None:
                                # "simple" tier — check local models against governance
                                _local_ids = _get_local_model_ids()
                                if _local_ids:
                                    _fb_loc_db = _GovSessionLocal()
                                    try:
                                        _fb_loc_allowed = _gov_filter_auto(
                                            _local_ids, _user_id, _user_dept, _fb_loc_db
                                        )
                                    finally:
                                        _fb_loc_db.close()
                                    if _fb_loc_allowed:
                                        # At least one local model is allowed
                                        _fp_hint = _fb_tier
                                        _gov_local_only = True  # cloud exhausted, local only
                                        _fallback_found = True
                                        logger.warning(
                                            f"[governance/auto] all cloud models blocked for "
                                            f"user={_user_id} dept={_user_dept!r} "
                                            f"— falling back to local (fail-closed if local down)"
                                        )
                                        break
                                    else:
                                        # All local models also blocked
                                        logger.warning(
                                            f"[governance/auto] all cloud AND local models blocked | "
                                            f"user={_user_id} dept={_user_dept!r}"
                                        )
                                        _gov_local_only = True
                                        # _fallback_found stays False → error returned below
                                        break
                                else:
                                    # No local models configured — treat as allowed (local gateway absent)
                                    _fp_hint = _fb_tier
                                    _gov_local_only = True
                                    _fallback_found = True
                                    break
                            else:
                                _fb_db = _GovSessionLocal()
                                try:
                                    _fb_allowed = _gov_filter_auto(
                                        [_fb_model], _user_id, _user_dept, _fb_db
                                    )
                                finally:
                                    _fb_db.close()
                                if _fb_allowed:
                                    _fp_hint = _fb_tier
                                    _fallback_found = True
                                    logger.info(
                                        f"[governance/auto] fallback → "
                                        f"tier={_fb_tier!r} model={_fb_model!r}"
                                    )
                                    break

                        if not _fallback_found:
                            _gov_local_only = True  # signal error block below

                # Sentinel: all local models were blocked before even entering fallback chain
                if _auto_model == "__all_local_blocked__":
                    _gov_local_only = True

            except Exception as _gov_auto_err:
                logger.warning(
                    f"[governance/auto] check error (fail-open): {_gov_auto_err}"
                )

        # When _gov_local_only=True all cloud models were exhausted by the
        # fallback chain AND all local models are also governance-blocked.
        # Return a clear 403 immediately — do NOT let the request reach the LLM.
        # Note: _fp_hint may still be "medium"/"complex"/etc. here (it is only
        # updated to "simple" when a cloud fallback succeeds), so we must NOT
        # gate this block on _fp_hint value.
        if _gov_local_only:
            logger.warning(
                f"[governance/auto] all models blocked (cloud + local) | "
                f"user={_user_id} dept={_user_dept!r} — returning 403"
            )
            from fastapi.responses import JSONResponse as _GovLocalJSONResponse
            return _GovLocalJSONResponse(
                status_code=403,
                content={
                    "error": "no_model_available",
                    "detail": (
                        "All AI models have been restricted for your department. "
                        "Please contact your administrator."
                    ),
                    "code": "GOVERNANCE_NO_MODEL_AVAILABLE",
                },
            )
        # ── END AUTO-ROUTING GOVERNANCE FILTER ───────────────────────────────

        # ── Static platform capability summary ────────────────────────────
        # Always injected for voice_platform so the model has facts even when
        # pgvector returns nothing. Covers all major platform capabilities.
        _PLATFORM_STATIC_CONTEXT = (
            "AiNxt is an enterprise-grade, PCI/DSS-compliant autonomous agentic AI platform "
            "built for engineering teams at scale. Key capabilities: "
            "Multi-agent orchestration with intelligent routing across Claude Sonnet, GPT, and Gemini models; "
            "enterprise PCI/DSS compliance engine with automatic detection and redaction of PAN, Aadhaar, "
            "API keys, and 20+ sensitive data types on every input and output; "
            "hybrid RAG retrieval combining pgvector semantic search, BM25 keyword search, and "
            "cross-encoder reranking for highly accurate, grounded answers; "
            "voice-first interface with real-time speech recognition, streaming AI responses, and "
            "natural OpenAI TTS with sentence-level pre-fetching for zero-delay speech; "
            "SDLC automation pipeline for AI-powered code generation, code review, test generation, "
            "and self-healing bug fixing; "
            "visual workflow builder with drag-and-drop multi-step AI workflows, branching, and "
            "parallel DAG execution; "
            "Docker-isolated secure code sandbox with network isolation, memory caps, and "
            "self-healing error correction; "
            "document knowledge base ingesting PDFs, Word docs, and URLs with instant semantic search; "
            "role-based access control with a full governance lifecycle: Draft → Review → Approved → Production; "
            "scales to 200+ concurrent users via RQ worker pools, Redis streaming, and GPU-backed Ollama inference; "
            "built and operated entirely in-house — zero vendor lock-in, full data sovereignty."
        )

        # ── Detect if the audience is giving a recommendation/suggestion ──
        _suggest_keywords = (
            "why don't you", "you should", "have you considered", "what about adding",
            "can you add", "could you add", "suggestion", "recommend", "i think you should",
            "why not add", "what if you", "maybe you could", "you might want to"
        )
        _is_recommendation = any(kw in safe_question.lower() for kw in _suggest_keywords)

        # ── Platform voice prompt ──────────────────────────────────────────
        _rec_instruction = (
            "The audience member is giving a RECOMMENDATION or SUGGESTION. "
            "Respond warmly: acknowledge it as a great insight, say we've noted it, "
            "and pivot to a related strength we already have. Never be defensive.\n"
        ) if _is_recommendation else ""

        _PLATFORM_VOICE_PROMPT = (
            "You are the enthusiastic, proud spokesperson for AiNxt — an AI platform your team built. "
            "You're in a live conversation at a tech conclave. Real humans are listening. Be human.\n\n"
            + _rec_instruction +
            "ABSOLUTE RULES — no exceptions:\n"
            "1. ZERO negative language. Never say: not available, gap, limitation, missing, can't, "
            "don't have, unable, unfortunately, however (as a negative pivot), but (as a negative), "
            "we're still working on, roadmap, planned, future release. "
            "If something is asked that isn't in context — pivot confidently to a related strength.\n"
            "2. 2 to 3 SHORT sentences only. Audience is listening, not reading. No walls of text.\n"
            "3. NO bullet points. NO numbered lists. NO markdown. NO headers. Natural prose only.\n"
            "4. Use contractions — we've, it's, you'll, that's, we're — sounds human.\n"
            "5. Speak on behalf of the team: 'we built', 'our platform', 'we've designed'.\n"
            "6. Lead with the most impressive, specific fact you can find in the context.\n"
            "7. Be warm and proud — this is your team's work and it's excellent.\n"
            "8. Occasionally (not every turn) end with a natural invitation: "
            "'Happy to dive deeper on that!' or 'What aspect would you like to explore further?'\n\n"
            "Platform context for reference:\n"
            f"{_PLATFORM_STATIC_CONTEXT}\n\n"
            "INPUT NOTE: The question below was transcribed from live speech and may contain "
            "speech-recognition errors. Interpret charitably using the platform context — "
            "e.g. 'HDFC' in a tech question likely means 'SDLC'; 'HR integration' likely means "
            "'human in the loop'; 'rack' likely means 'RAG'. Always interpret in the context of "
            "software engineering and AI platforms.\n\n"
            "Now answer in that warm, natural, spoken style:\n\n"
        )

        # For voice_platform: always build a grounded prompt merging static context + pgvector hits.
        # This guarantees the model has rich platform facts even if pgvector returns nothing.
        def _build_voice_prompt(question: str, pgvector_context: str) -> str:
            combined = pgvector_context.strip()
            # Static context is already in _PLATFORM_VOICE_PROMPT; pgvector adds specifics on top
            if combined:
                return (
                    _PLATFORM_VOICE_PROMPT
                    + "Additional specific context:\n" + combined + "\n\n"
                    + "Question: " + question
                )
            return _PLATFORM_VOICE_PROMPT + question

        async def _general_stream():
            # `_fp_sources_meta` belongs to the enclosing ask_ai scope. The
            # source-narrowing block further down assigns to it, and without
            # this declaration that assignment made the name local to this
            # generator for its whole body — so the read on the normal
            # completion path raised
            #   UnboundLocalError: cannot access local variable
            #   '_fp_sources_meta' where it is not associated with a value
            # whenever the narrowing branch had not run, which is the common
            # case. That surfaced in the UI as "Error generating response" for
            # essentially every chat request. Narrowing is meant to replace the
            # outer value so the final __meta__ reports only the contributing
            # sources, so nonlocal is the intended semantics, not a workaround.
            nonlocal _fp_sources_meta
            # ── Async generator: ContextVars are inherited from the request ───
            # Unlike the old sync generator (which ran in an anyio thread-pool
            # worker and had to re-bind threading.local() on every next() call),
            # this async generator runs on the event loop and inherits the
            # ContextVar snapshot set by the request handler — request_id,
            # user_id, chat_id, span_id, and correlation_id are all correct
            # without any explicit re-binding.
            # ─────────────────────────────────────────────────────────────────
            # ─────────────────────────────────────────────────────────────────
            _full   = ""
            _gmeta  = {"out_tok": 0, "in_tok": 0, "model": "auto", "cost": 0.0, "latency": 0.0}
            _fp_sources_meta = []  # prevents UnboundLocalError on paths that skip the narrowing branch
            # SSE streaming: always emit tokens live as they arrive for all models
            # (local and cloud). Buffered-flush mode is disabled globally so the
            # client receives per-token SSE chunks instead of one large frame.
            # Grounding checks and PCI/PII redaction still run post-stream on
            # _full for history persistence — they no longer gate the live stream.
            _effective_preflush_gate = False
            _is_local_route = bool(_local_model or _fp_hint in ("local", "simple"))
            logger.info(
                "[SSE] live-stream mode active for all models "
                "(local_route=%s fp_hint=%s local_model=%s) — "
                "tokens will be emitted per-chunk as they arrive",
                _is_local_route, _fp_hint, _local_model,
            )
            # Anchor at the true request start so latency covers the pre-LLM
            # work (auth, compliance, retrieval), not just the token slice.
            _gt0    = start_time
            # Acquire the threading.Semaphore without blocking the event loop.
            import asyncio as _asyncio_gs
            _sem_acquired = await _asyncio_gs.get_event_loop().run_in_executor(
                None, lambda: _LLM_SEMAPHORE.acquire(timeout=_SEM_ACQUIRE_TIMEOUT)
            )
            if not _sem_acquired:
                yield "data: " + json.dumps({"t": "\nServer busy — too many concurrent AI requests. Please retry in a moment."}) + "\n\n"
                return

            # ── R3: RUNTIME CANARY (Mode B) ───────────────────────────────
            # Route RUNTIME_PCT % of users to ainxt-runtimed.
            # Streams runtime SSE text.delta frames directly to the UI.
            # Falls back to Python path on any error (fail-open).
            # Rollback: set RUNTIME_PCT=0 in env and restart gateway.
            try:
                from core.runtime_client import (
                    chat_stream_sync, user_in_canary,
                    ENABLE_RUNTIME as _RT_ON, RUNTIME_PCT as _RT_PCT,
                )
                _rt_dept = (_user_ctx or {}).get("department") or _user_dept or "payments"  # default dept for canary
                _rt_dc   = getattr(q, "data_class", "internal") if hasattr(q, "data_class") else "internal"
                _rt_caps = getattr(q, "caps", None) or ["chat.send"]
                _rt_session = _chat_id or str(uuid.uuid4())
                _rt_turn    = request_id or str(uuid.uuid4())
                _rt_msg     = safe_question or original or ""
                if _RT_ON and _RT_PCT > 0 and user_in_canary(_user_id or "anon", _RT_PCT) and _rt_msg:
                    logger.info(
                        f"CANARY: routing user={_user_id} session={_rt_session} "
                        f"to runtime (RUNTIME_PCT={_RT_PCT})"
                    )
                    _rt_chunks = []
                    try:
                        for _rt_tok in chat_stream_sync(
                            session=_rt_session,
                            turn=_rt_turn,
                            message=_rt_msg,
                            data_class=_rt_dc,
                            caps=_rt_caps,
                            department=_rt_dept,
                        ):
                            _rt_chunks.append(_rt_tok)
                            yield "data: " + json.dumps({"t": _rt_tok}) + "\n\n"
                        if _rt_chunks:
                            _rt_full = "".join(_rt_chunks)
                            _rt_user_msg_id = str(uuid.uuid4())
                            _rt_ast_msg_id  = str(uuid.uuid4())
                            # Persist the turn to Postgres so it survives page reloads
                            # and appears in the sidebar. Without this the canary path
                            # returned early and the chat was never saved — so the next
                            # /chats fetch returned [] and the UI minted a NEW chat_id,
                            # breaking multi-turn session context on the Rust daemon.
                            try:
                                import threading as _rt_th
                                _rt_th.Thread(
                                    target=_save_chat_messages,
                                    kwargs={
                                        "chat_id":        _rt_session,
                                        "user_id":        _user_id or "default",
                                        "question":       _rt_msg,
                                        "answer":         _rt_full,
                                        "model":          "ainxt-runtime",
                                        "in_tok":         0,
                                        "out_tok":        len(_rt_chunks),
                                        "cost":           0.0,
                                        "latency":        round(time.time() - start_time, 3),
                                        "language":       _detected_lang or "unknown",
                                        "attachment_ids": q.attachment_ids or [],
                                        "project_id":     q.project_id or "",
                                        "agent_id":       q.agent_id or "",
                                        "title_hint":     _rt_msg[:80] if not q.chat_id else None,
                                        "rag_mode":       _rag_mode,
                                        "repo_filter":    repo_filter,
                                        "client_source":  getattr(request.state, "client_source", "platform"),
                                    },
                                ).start()
                            except Exception as _rt_persist_err:
                                logger.warning(f"CANARY: chat persist failed: {_rt_persist_err}")
                            yield "data: " + json.dumps({"__meta__": {
                                "tokens": len(_rt_chunks), "in_tok": 0, "out_tok": len(_rt_chunks),
                                "cost": 0.0, "model": "ainxt-runtime",
                                "latency": round(time.time() - start_time, 3),
                                "source": "runtime_canary",
                                "chat_id":       _rt_session,
                                "message_id":    _rt_ast_msg_id,
                                "user_message_id": _rt_user_msg_id,
                            }}) + "\n\n"
                            _LLM_SEMAPHORE.release()
                            return
                        logger.warning("CANARY: runtime returned no chunks — falling back to Python")
                    except RuntimeError as _rt_err:
                        logger.warning(f"CANARY: runtime error: {_rt_err} — falling back to Python")
            except Exception as _canary_err:
                logger.debug(f"gateway: canary block error (non-fatal): {_canary_err}")
            # ─────────────────────────────────────────────────────────────

            # ── Live status line (Phase 1.4) ─────────────────────────────
            # Emit a structured {status} event so the client shows a
            # meaningful "Thinking…/Reading sources…" line immediately —
            # before the first token — matching Claude/ChatGPT aliveness.
            # Backward-compatible: old clients ignore unknown keys.
            try:
                if _docs_context:
                    # Count retrieved snippets for a truthful "Reading N sources…"
                    # (context blocks are joined with double newlines upstream).
                    try:
                        _src_n = max(1, len([_b for _b in str(_docs_context).split("\n\n") if _b.strip()]))
                        _status0 = f"Reading {_src_n} source{'s' if _src_n != 1 else ''}…"
                    except Exception:
                        _status0 = "Reading sources…"
                else:
                    _status0 = "Thinking…"
                yield "data: " + json.dumps({"status": _status0}) + "\n\n"
            except Exception:
                pass
            # ── Phase 5: user-visible plan panel (additive; fail-safe) ────
            # When PIPELINE_V2 produced a conv_state, surface the planner shape
            # so the client can render a "here's my plan" panel. Only emitted
            # for genuinely multi-step shapes (tool_use/decompose/retrieve) so a
            # trivial Q&A stays clean. Old clients ignore the `plan` key.
            try:
                if (_PIPELINE_V2 and _PIPELINE_V2_STREAM and _rc is not None
                        and _rc.conv_state is not None):
                    from planner.shape import (
                        select_shape as _sel_shape,
                        DECOMPOSE as _SH_DECOMP, TOOL_USE as _SH_TOOL,
                        RETRIEVE as _SH_RETR,
                    )
                    from pipeline.stream_events import plan_event as _plan_evt
                    _sd = _sel_shape(_rc.conv_state)
                    if _sd.shape in (_SH_DECOMP, _SH_TOOL, _SH_RETR):
                        yield "data: " + json.dumps(
                            _plan_evt(_sd.shape, reason=_sd.reason)
                        ) + "\n\n"
            except Exception:
                pass
            # Phase 2: emit the context-window telemetry + compaction notice.
            try:
                if _context_info:
                    yield "data: " + json.dumps({"context": _context_info}) + "\n\n"
                    if _context_info.get("compacted"):
                        yield "data: " + json.dumps({"compaction": {
                            "message": "Earlier messages were summarized to keep the conversation within context.",
                        }}) + "\n\n"
            except Exception:
                pass
            # Register this request so POST /chat/stop can signal cancellation.
            _gen_register(request_id)
            try:
                from models.model_router import model_router as _mr

                # Build the final messages payload for this fast-path call.
                # _messages already contains history + current user question.
                # For voice/docs paths we replace the last user message content
                # with the enriched prompt — preserving the full history context.
                _fp_messages = list(_messages)  # shallow copy — safe to mutate last entry

                # PERF FIX: strip messages with empty/whitespace-only content before
                # sending to the LLM. Empty turns (e.g. placeholder history entries,
                # empty persona/tone prefixes) consume context-window tokens and inflate
                # the message count logged by [ContextIsolation]. Always preserve the
                # last message (current user turn) even if somehow empty.
                if len(_fp_messages) > 1:
                    _fp_messages = [
                        m for m in _fp_messages[:-1]
                        if isinstance(m, dict) and str(m.get("content") or "").strip()
                    ] + [_fp_messages[-1]]

                # ── KV-cache system-message hoisting (local models only) ──────
                # For local model routes, place all session-stable content in a
                # dedicated {"role":"system"} message at index 0 so vLLM APC
                # caches those KV blocks across turns instead of recomputing them.
                #
                # The existing _system_preface was already fused into
                # _messages[0]["content"] above (cloud-compatible path). For local
                # routes we prepend a clean system message on top of that so the
                # stable prefix is always at position 0 regardless of history length.
                #
                # Guarded by _LOCAL_KV_CACHE_HOIST (default ON) and only fires when
                # the request is actually routed to a local model.
                # _is_local_route is already set above (preflush gate block).
                if _LOCAL_KV_CACHE_HOIST and _is_local_route:
                    try:
                        _kv_sys_content = _build_local_system_message(
                            agent_system_prompt=_agent_system_prompt or "",
                            cowork_role_prompt=_cowork_role_prompt or "",
                            cowork_memory=_cowork_memory_str or "",
                            custom_about=_custom_about or "",
                            custom_style=_custom_style or "",
                            memory_facts=list(_user_turns or []),
                            feedback_hint=_fb_hint or "",
                            user_name=(q.user_name or ""),
                            tone_pfx=_tone_pfx or "",
                            sensitive=_is_sensitive_domain,
                        )
                        if _kv_sys_content:
                            _fp_messages = [
                                {"role": "system", "content": _kv_sys_content}
                            ] + _fp_messages
                            logger.debug(
                                "[KV-hoist] local system message injected "
                                "chars=%d request_id=%s",
                                len(_kv_sys_content), request_id,
                            )
                    except Exception as _kv_err:
                        logger.debug("[KV-hoist] skipped: %s", _kv_err)

                # ── KB context redaction (cloud models only) ─────────────────
                # Retrieved KB chunks may contain PANs / card numbers / other
                # PCI/PII. For cloud LLMs (Claude/OpenAI/Gemini) we MUST redact
                # them before injecting into the grounded prompt.
                # Local model selection bypasses this (_bypass_safety_filters=True).
                #
                # NOTE: we deliberately read the enclosing `_docs_context` into a
                # fresh local `_docs_ctx_final` and DO NOT reassign the enclosing
                # name here. Reassigning it inside this nested function would
                # cause Python to treat every read of `_docs_context` in this
                # closure as a local variable — raising UnboundLocalError on
                # paths (e.g. project_id / repo_filter) where the outer scope
                # never assigned it.
                try:
                    _docs_ctx_final = _docs_context
                except NameError:
                    _docs_ctx_final = ""
                if _docs_ctx_final and not _bypass_safety_filters:
                    try:
                        _ctx_redacted, _ctx_types = _ce_ask.redact_text(_docs_ctx_final)
                        if _ctx_types:
                            logger.info(
                                f"[KB_CONTEXT REDACT] cloud model — types={_ctx_types}"
                            )
                        _docs_ctx_final = _ctx_redacted
                    except Exception as _ctx_red_err:
                        logger.warning(
                            f"[KB_CONTEXT REDACT] failed: {_ctx_red_err} — using raw context"
                        )

                if _is_voice_platform:
                    _voice_prompt = _build_voice_prompt(safe_question, _docs_ctx_final)
                    _fp_messages[-1] = {"role": "user", "content": _voice_prompt}
                elif _docs_ctx_final:
                    # Build grounded prompt via shared utility (core/ask_utils.py).
                    # Same logic used by kb_ask_router — single source of truth,
                    # no duplication between Chat and KB paths.
                    from core.ask_utils import build_kb_grounded_prompt as _build_grounded
                    _grounded = _build_grounded(
                        safe_question       = safe_question,
                        docs_ctx            = _docs_ctx_final,
                        is_followup         = _is_followup,
                        has_history         = _has_history,
                        chat_scope_doc_ids  = _chat_scope_doc_ids,
                    )
                    _fp_messages[-1] = {"role": "user", "content": _grounded}
                # else: _fp_messages[-1] is already the plain safe_question message

                # ── Context isolation assertion (belt-and-suspenders) ────
                # If the current request is Generic, log that the prompt was
                # assembled.  Logger may be structlog (no isEnabledFor) or
                # stdlib; we just log unconditionally — INFO level is cheap.
                if _rag_mode == "off":
                    try:
                        logger.info(
                            f"[ContextIsolation] Generic prompt assembled  "
                            f"messages={len(_fp_messages)}  request_id={request_id}"
                        )
                    except Exception:
                        pass

                # Capture meta via the new sentinel emitted by
                # model_router.stream() — see its docstring. This avoids
                # the thread-local race where anyio's threadpool would
                # hop threads between the final _propagate_tokens write
                # and the post-loop read of last_input_tokens, producing
                # in_tok=3 for every turn after a transient failure.
                _stream_meta: dict = {}
                _stream_thinking = ""
                # True only once the ainxt-api hop has actually STREAMED this
                # turn's answer. `_ainxt_model` is bound before the ainxt-api
                # try block, so its mere existence says nothing about who
                # served the response — see the _gmeta["model"] resolution below.
                _ainxt_served = False
                # /ask has already run compliance_engine.validate_input() on
                # `original` at line 1799 (_ask_chk). The OpenAI/Gemini gateways
                # used to re-validate the LAST message of _fp_messages — which
                # the first pass never saw because it carries the tone prefix
                # and (on first-turn chats) cross-chat memory + custom
                # instructions. That second pass was the source of intermittent
                # "Request blocked due to PCI violation" false positives:
                # benign prompts that the gateway cleared got blocked by the
                # provider gateway because (a) the appended metadata tripped a
                # regex/ML detector or (b) the stochastic ML privacy service
                # returned a different result on the second call. Forward the
                # precleared flag + first-pass findings so the provider gateway
                # skips the block decision but still redacts.
                #
                # ── Memory footer suppression buffer ─────────────────────────
                # The LLM appends <!--MEMORY:{...}--> as the very last token(s).
                # We buffer tokens once the footer sentinel is detected so it is
                # never yielded to the client. The buffer is flushed only if the
                # sentinel turns out to be a false alarm (i.e. the footer never
                # closes before the stream ends).
                _mem_buf        = ""          # accumulates tokens once "<!--" seen
                _mem_buffering  = False       # True while we are holding back tokens
                _MEM_SENTINEL   = "<!--MEMORY:"
                # Transition the status line to "Generating response…" just
                # before the first token starts flowing (Phase 1.4).
                try:
                    yield "data: " + json.dumps({"status": "Generating response…"}) + "\n\n"
                except Exception:
                    pass
                _cil_tier_log = _cil_tier if "_cil_tier" in locals() else "n/a"
                logger.info(
                    f"[ROUTER] request_id={request_id!r} chat_id={_chat_id!r} "
                    f"model_hint={_fp_hint!r} local_model={_local_model!r} "
                    f"user_model={q.model!r} cil_tier={_cil_tier_log!r}"
                )

                # ── ainxt-api integration ─────────────────────────────────────
                # Route all chat requests through ainxt-api instead of directly
                # to the LLM proxy. ainxt-api handles session management,
                # tool use, and streaming back to the client.
                # Controlled by AINXT_API_ENABLED env var (default: true).
                # Set AINXT_API_ENABLED=false to bypass and use model_router directly.
                import httpx as _httpx
                import json as _json_ainxt
                logger.info(f"[ainxt-api] routing flag: AINXT_API_ENABLED={_AINXT_API_ENABLED} (raw env={os.getenv('AINXT_API_ENABLED', 'NOT SET')!r})")

                # Build the prompt text — only the current user message.
                # ainxt-api maintains conversation history per session so we
                # don't need to send the full _fp_messages history.
                # Use `original` (not q.question) — `original` has already been
                # enriched by Step 0 with attachment content (document text) so
                # the agent sees doc context. q.question is the raw user text.
                #
                # IMAGE ATTACHMENTS: `original` is NOT enriched for images because
                # the Step-0 attachment loop only injects parsed_text (empty for
                # images). We must inject image_description/image_caption here so
                # the CLI agent actually sees the image content.
                _ainxt_prompt = original
                try:
                    _img_att_ids = list(getattr(q, "attachment_ids", None) or [])
                    if _img_att_ids and _AINXT_API_ENABLED:
                        from db.database import SessionLocal as _AinxtImgDB
                        from db.models import ChatAttachment as _AinxtImgCA
                        _aimg_db = _AinxtImgDB()
                        try:
                            _img_blocks = []
                            for _aimg in (
                                _aimg_db.query(_AinxtImgCA)
                                .filter(
                                    _AinxtImgCA.id.in_(_img_att_ids),
                                    _AinxtImgCA.kind == "image",
                                )
                                .all()
                            ):
                                _img_desc = (
                                    _aimg.image_description
                                    or _aimg.image_caption
                                    or ""
                                ).strip()
                                if _img_desc:
                                    _img_blocks.append(
                                        f"[Image: {_aimg.file_name}]\n{_img_desc}"
                                    )
                                    logger.info(
                                        f"[ainxt-api] injecting image description "
                                        f"attachment_id={_aimg.id} file={_aimg.file_name!r} "
                                        f"desc_chars={len(_img_desc)}"
                                    )
                            if _img_blocks:
                                _ainxt_prompt = (
                                    "\n\n".join(_img_blocks)
                                    + "\n\nUser question: "
                                    + _ainxt_prompt
                                )
                        finally:
                            _aimg_db.close()
                except Exception as _aimg_err:
                    logger.warning(f"[ainxt-api] image attachment inject failed (non-fatal): {_aimg_err}")

                # ── Determine the CLI session model ──────────────────────────
                # The user's selection is authoritative:
                #   • The frontend sends the FULL model ID (e.g. "claude-sonnet-4-6",
                #     "deepseek-v4-flash") in q.model for an explicit pick, or
                #     "auto" (or empty) when the user wants the system to choose.
                #   • q.local_model still carries an explicit local model when set
                #     (legacy dropdown path) and wins if present.
                #
                # Resolution:
                #   1. q.local_model set          → use it verbatim.
                #   2. q.model is a real model ID → use it verbatim (whatever the
                #      user picked is what answers).
                #   3. q.model is "auto"/empty    → SMART AUTO: use the CIL-classified
                #      task_complexity tier (simple|medium|complex|…) mapped through
                #      the tier config, so a simple ask gets a fast model and a
                #      complex ask gets a strong one, per request.
                #
                # Tier-alias values ("claude"/"gpt"/"auto"/tier names) are only
                # ever produced now by the "auto" path (CIL tier) or legacy
                # clients; they resolve through _AINXT_TIER_MAP_CFG. A concrete
                # model ID passes through unchanged.
                _AUTO_MODEL_VALUES = {"", "auto", "default", None}
                _raw_model = (q.model or "").strip()
                if _local_model:
                    _ainxt_model = _local_model
                elif _raw_model and _raw_model.lower() not in _AUTO_MODEL_VALUES \
                        and _raw_model.lower() not in _AINXT_TIER_MAP_CFG:
                    # A concrete, non-alias model ID → use exactly what the user picked.
                    _ainxt_model = _raw_model
                else:
                    # AUTO: let the CIL task-complexity tier choose the model.
                    # _fp_hint was already set from conv_state.task_complexity above
                    # (Phase 3 router driving); fall back to a tier alias or default.
                    _tier = (_fp_hint or _raw_model or "auto").lower().strip()
                    _ainxt_model = (
                        _AINXT_TIER_MAP_CFG.get(_tier)
                        or _AINXT_TIER_MAP_CFG.get("default", "")
                    )
                logger.info(
                    f"[ainxt-api] model resolved: user_pick={q.model!r} "
                    f"local={_local_model!r} cil_tier={_fp_hint!r} → {_ainxt_model!r}"
                )

                try:
                    if not _AINXT_API_ENABLED:
                        raise Exception("ainxt-api disabled via AINXT_API_ENABLED=false")

                    # ── Knowledge-base bypass ────────────────────────────────
                    # KB requests must NOT go through ainxt-api (RUST): the CLI
                    # path has no KB retrieval/grounding wiring, so a KB turn
                    # routed there loses its retrieved context and answers from
                    # general knowledge. Any KB-mode turn (rag_mode on/auto) — OR
                    # any turn that already has retrieved context — stays on
                    # model_router, which injects the grounded context into
                    # _fp_messages. Fires regardless of whether retrieval returned
                    # anything, so an empty-context KB turn still never falls to
                    # RUST. Raising the disabled-sentinel here is caught by the
                    # except-block below → clean fallback to model_router.
                    if _rag_mode in ("on", "auto") or _docs_context:
                        logger.info(
                            f"[ainxt-api] skipping — KB request "
                            f"(rag_mode={_rag_mode!r}, "
                            f"context={'yes' if _docs_context else 'none'}), "
                            f"routing to model_router"
                        )
                        raise Exception("ainxt-api disabled via AINXT_API_ENABLED=false")

                    try:
                        from core.config import user_doc_dir as _user_doc_dir
                        _ainxt_sess_dir = _user_doc_dir(_user_id, _chat_id)
                    except Exception as _cwd_err:
                        logger.warning(f"[ainxt-api] could not compute session cwd: {_cwd_err}")
                        _ainxt_sess_dir = ""

                    with _httpx.Client(timeout=120.0) as _ainxt_client:

                        def _ainxt_new_session():
                            """Create ainxt-api session with DOC_STORAGE_DIR cwd."""
                            _sess_cwd = _ainxt_sess_dir
                            _r = _ainxt_client.post(
                                f"{_AINXT_API_URL}/sessions",
                                headers={
                                    "Authorization": f"Bearer {_AINXT_API_KEY}",
                                    "Content-Type": "application/json",
                                },
                                json={
                                    "model": _ainxt_model,
                                    "mode": "always_approve",
                                    "cwd":   _sess_cwd,
                                },
                            )
                            if _r.status_code not in (200, 201):
                                logger.error(
                                    f"[ainxt-api] session create failed: "
                                    f"{_r.status_code} {_r.text}"
                                )
                                raise Exception("ainxt-api session creation failed")
                            _sid = _r.json().get("session_id")
                            _ainxt_sess_put(_chat_id, _sid, _ainxt_model, _sess_cwd)
                            logger.info(
                                f"[ainxt-api] new session={_sid} model={_ainxt_model!r} "
                                f"cwd={_sess_cwd!r} for chat_id={_chat_id!r}"
                            )
                            return _sid

                        def _ainxt_send_prompt(_sid, _is_new_session=False):
                            _text = _ainxt_prompt
                            # ── Doc-context notification (one-shot) ───────────────
                            # If the gateway generated a document for this chat since
                            # the last CLI prompt, prepend a system note so the CLI
                            # knows about it for follow-up turns. Consumed and deleted
                            # from Redis on first read (one-shot, not repeated).
                            _doc_ctx_note = _ainxt_doc_ctx_pop(_chat_id)
                            if _doc_ctx_note:
                                _text = _doc_ctx_note + "\n\n" + _text
                            if _is_new_session:
                                _, _, _sess_cwd = _ainxt_sess_get(_chat_id)
                                _cwd_note = (
                                    f" Your working directory is {_sess_cwd!r}."
                                    f" Save any files you create directly into this directory"
                                    f" without creating subdirectories."
                                ) if _sess_cwd else ""
                                # ainxt-api sessions live in memory only (die on
                                # restart / model switch / stale-session recovery),
                                # so a brand-new session has zero knowledge of prior
                                # turns unless we replay them here. _fp_messages
                                # already holds the full loaded history with the
                                # current question as the LAST entry — replay
                                # everything BEFORE that as a transcript.
                                _hist_turns = [
                                    m for m in (_fp_messages[:-1] if _fp_messages else [])
                                    if m.get("role") in ("user", "assistant")
                                ]
                                _hist_block = ""
                                if _hist_turns:
                                    _hist_lines = []
                                    for _hm in _hist_turns[-20:]:  # cap: last 20 turns
                                        _hrole = "User" if _hm.get("role") == "user" else "Assistant"
                                        _hcontent = (_hm.get("content") or "").strip()
                                        if _hcontent:
                                            _hist_lines.append(f"{_hrole}: {_hcontent}")
                                    if _hist_lines:
                                        _hist_block = (
                                            "\n\n[Earlier conversation in this chat, for context "
                                            "only — do not repeat it back to the user]\n"
                                            + "\n\n".join(_hist_lines)
                                        )
                                _text = (
                                    f"[System: You are a chat assistant in a web portal.{_cwd_note}"
                                    f" IMPORTANT: Never reveal your internal name, model, or that"
                                    f" you are a CLI/agent. On a casual greeting (hi/hello), reply"
                                    f" only with something generic like 'Hi, how can I help you"
                                    f" today?' — do not introduce yourself."
                                    f" Do NOT narrate tool usage or upcoming steps, before, during,"
                                    f" or after acting. Never say things like 'Let me install...',"
                                    f" 'Now I will create...', 'Okay, let me create a document for"
                                    f" you...', 'Give me a moment while I...' etc. Emit no text while"
                                    f" a tool call is in progress — only respond once the task is"
                                    f" fully complete. Just perform the task silently and report the"
                                    f" result concisely.]"
                                    f"{_hist_block}\n\n{_ainxt_prompt}"
                                )
                            return _ainxt_client.post(
                                f"{_AINXT_API_URL}/sessions/{_sid}/prompt",
                                headers={
                                    "Authorization": f"Bearer {_AINXT_API_KEY}",
                                    "Content-Type": "application/json",
                                },
                                json={"text": _text},
                            )

                        def _ainxt_switch_model(_sid, _new_model):
                            """Switch an EXISTING session's model in place via
                            PUT /sessions/{id}/model (ACP SetSessionModel). The
                            CLI keeps all session state (history, compaction) —
                            no recreation, no history replay. Returns True on
                            success; caller falls back to recreate on failure."""
                            try:
                                _r = _ainxt_client.put(
                                    f"{_AINXT_API_URL}/sessions/{_sid}/model",
                                    headers={
                                        "Authorization": f"Bearer {_AINXT_API_KEY}",
                                        "Content-Type": "application/json",
                                    },
                                    json={"model": _new_model},
                                )
                                if _r.status_code == 200:
                                    _ainxt_sess_put(_chat_id, _sid, _new_model, _ainxt_sess_dir)
                                    return True
                                logger.warning(
                                    f"[ainxt-api] set-model failed "
                                    f"{_r.status_code} {_r.text} — will recreate session"
                                )
                                return False
                            except Exception as _sm_err:
                                logger.warning(
                                    f"[ainxt-api] set-model error: {_sm_err} — will recreate session"
                                )
                                return False

                        _ainxt_session_id, _ainxt_sess_model, _ainxt_sess_cwd = _ainxt_sess_get(_chat_id)
                        _ainxt_is_new_session = False
                        if _ainxt_session_id:
                            if _ainxt_sess_model and _ainxt_sess_model != _ainxt_model:
                                # Switch the live session's model IN PLACE (the CLI
                                # supports this natively via ACP SetSessionModel) so
                                # the conversation + context is preserved. Only fall
                                # back to drop+recreate if the in-place switch fails.
                                if _ainxt_switch_model(_ainxt_session_id, _ainxt_model):
                                    logger.info(
                                        f"[ainxt-api] model switched in place "
                                        f"{_ainxt_sess_model!r} → {_ainxt_model!r} "
                                        f"session={_ainxt_session_id} chat_id={_chat_id!r}"
                                    )
                                else:
                                    logger.info(
                                        f"[ainxt-api] model change "
                                        f"{_ainxt_sess_model!r} → {_ainxt_model!r} "
                                        f"for chat_id={_chat_id!r} — recreating session (switch failed)"
                                    )
                                    _ainxt_sess_drop(_chat_id)
                                    _ainxt_session_id = _ainxt_new_session()
                                    _ainxt_is_new_session = True
                            else:
                                logger.info(
                                    f"[ainxt-api] reusing session={_ainxt_session_id} "
                                    f"model={_ainxt_model!r} for chat_id={_chat_id!r}"
                                )
                        else:
                            _ainxt_session_id = _ainxt_new_session()
                            _ainxt_is_new_session = True

                        # Send the prompt.
                        _prompt_resp = _ainxt_send_prompt(_ainxt_session_id, _ainxt_is_new_session)

                        # ainxt-api holds sessions in memory only, so a restart
                        # invalidates every cached id. A stale id returns 404 —
                        # evict it, open a fresh session, and retry once. Without
                        # this, every pre-existing chat thread falls back to
                        # model_router permanently until the gateway restarts too.
                        if _prompt_resp.status_code == 404:
                            logger.warning(
                                f"[ainxt-api] stale session={_ainxt_session_id} "
                                f"(404) for chat_id={_chat_id!r} — recreating"
                            )
                            _ainxt_sess_drop(_chat_id)
                            _ainxt_session_id = _ainxt_new_session()
                            _prompt_resp = _ainxt_send_prompt(_ainxt_session_id, _is_new_session=True)

                        if _prompt_resp.status_code not in (200, 202):
                            logger.error(f"[ainxt-api] prompt failed: {_prompt_resp.status_code} {_prompt_resp.text}")
                            raise Exception("ainxt-api prompt failed")

                    # Step 3: Stream the response using a streaming client
                    with _httpx.Client(timeout=120.0) as _ainxt_stream_client:
                        with _ainxt_stream_client.stream(
                            "GET",
                            f"{_AINXT_API_URL}/sessions/{_ainxt_session_id}/stream",
                            headers={"Authorization": f"Bearer {_AINXT_API_KEY}"},  # noqa
                        ) as _ainxt_stream:
                            # Guard the status explicitly. Without this a 404
                            # (session died between prompt and stream) yields an
                            # empty body, the loop below parses zero chunks, and
                            # the user gets a blank answer with no fallback —
                            # the except-block never fires on a silent 200-less
                            # response.
                            if _ainxt_stream.status_code != 200:
                                _ainxt_stream.read()
                                if _ainxt_stream.status_code == 404:
                                    _ainxt_sess_drop(_chat_id)
                                logger.error(
                                    f"[ainxt-api] stream failed: "
                                    f"{_ainxt_stream.status_code} {_ainxt_stream.text}"
                                )
                                raise Exception("ainxt-api stream failed")

                            # Collect file_written events during streaming and process
                            # them AFTER the SSE stream closes. Downloading the file
                            # from ainxt-api while the SSE stream is still open would
                            # deadlock — ainxt-api can't serve a second request on the
                            # same session while it's busy streaming events.
                            _pending_file_events: list = []

                            for _line in _ainxt_stream.iter_lines():
                                if not _line:
                                    continue
                                if _line.startswith("data:"):
                                    _data_str = _line[5:].strip()
                                    try:
                                        _data = _json_ainxt.loads(_data_str)
                                        _type = _data.get("type", "")
                                        if _type == "chunk":
                                            _tok = _data.get("text", "")
                                            if _tok:
                                                _full += _tok
                                                if not _effective_preflush_gate:
                                                    yield "data: " + _json_ainxt.dumps({"t": _tok}) + "\n\n"
                                        elif _type == "file_written":
                                            # Queue for post-stream processing — do NOT
                                            # make HTTP calls while the stream is open.
                                            _fw_path = _data.get("path", "")
                                            _fw_name = _data.get("filename", "file.txt")
                                            if _fw_path:
                                                _pending_file_events.append(
                                                    {"path": _fw_path, "filename": _fw_name}
                                                )
                                        elif _type == "done":
                                            break
                                        elif _type == "status":
                                            logger.info(f"[ainxt-api] status: {_data.get('message', '')}")
                                    except Exception:
                                        pass
                                # Cooperative stop-check
                                if _gen_should_stop(request_id):
                                    logger.info(f"[ainxt-api] stream stopped by user request_id={request_id}")
                                    break

                    # ── Post-stream: process any file_written events ──────────
                    # File already sits on the shared filesystem (agent cwd =
                    # DOC_STORAGE_DIR/{user}/{chat}/) — just register it in the DB.
                    for _pfe in _pending_file_events:
                        try:
                            _fw_marker = _ainxt_register_generated_file(
                                path=_pfe["path"],
                                filename=_pfe["filename"],
                                user_id=_user_id,
                                chat_id=_chat_id,
                            )
                            if _fw_marker:
                                _full += _fw_marker
                                yield "data: " + _json_ainxt.dumps({"t": _fw_marker}) + "\n\n"
                        except Exception as _pfe_err:
                            logger.warning(f"[ainxt-api] post-stream file register error: {_pfe_err}")

                    # Populate _gmeta with the model used so chat_messages.model_used
                    # is saved correctly (otherwise it stays "auto" from the initializer).
                    _gmeta["model"] = _ainxt_model
                    # Reached only when ainxt-api streamed the answer itself. The
                    # post-stream metadata block below keys off this rather than
                    # off `_ainxt_model` being bound.
                    _ainxt_served = True

                    # Success marker. Without this the happy path is silent and
                    # indistinguishable from a turn that quietly produced nothing,
                    # so "no error in the log" was never evidence the hop worked.
                    logger.info(
                        f"[ainxt-api] stream complete session={_ainxt_session_id} "
                        f"chars={len(_full)} files={len(_pending_file_events)} chat_id={_chat_id!r}"
                    )

                except Exception as _ainxt_err:
                    _ainxt_disabled = str(_ainxt_err) == "ainxt-api disabled via AINXT_API_ENABLED=false"
                    if _ainxt_disabled:
                        logger.info("[ainxt-api] disabled — routing directly via model_router")
                    else:
                        logger.error(f"[ainxt-api] error: {_ainxt_err}, falling back to model_router")
                    # Fallback to model_router if ainxt-api fails or is disabled.
                    # Uses async_stream (UAT) with ReasoningMarker support.
                    async for _tok in _mr.async_stream(
                        _fp_messages,
                        model_hint=_fp_hint,
                        local_model=_local_model,
                        precleared=True,
                        precleared_findings=_ask_chk.get("findings", []),
                    ):
                        if isinstance(_tok, dict):
                            _sm = _tok.get("__stream_meta__")
                            if _sm:
                                _stream_meta = _sm
                                _stream_thinking = _sm.get("thinking", "") or ""
                            continue
                        # ── Phase 5+: live reasoning deltas (additive; fail-safe) ──
                        try:
                            from pipeline.stream_events import ReasoningMarker as _RMk
                            if isinstance(_tok, _RMk):
                                if _PIPELINE_V2 and _PIPELINE_V2_STREAM:
                                    yield "data: " + json.dumps(_tok.to_event()) + "\n\n"
                                continue
                        except Exception:
                            pass
                        if _gen_should_stop(request_id):
                            break
                        if not _tok:
                            continue
                        _full += _tok
                        if _mem_buffering:
                            _mem_buf += _tok
                        elif _MEM_SENTINEL in (_full[-len(_MEM_SENTINEL) - len(_tok):]):
                            _sentinel_pos = _full.rfind(_MEM_SENTINEL)
                            _clean_part   = _full[:_sentinel_pos].rstrip()
                            _already_yielded = len(_full) - len(_tok) - len(_mem_buf)
                            _safe_prefix = _clean_part[_already_yielded:]
                            if _safe_prefix and not _effective_preflush_gate:
                                yield "data: " + json.dumps({"t": _safe_prefix}) + "\n\n"
                            _mem_buf       = _full[_sentinel_pos:]
                            _mem_buffering = True
                        elif not _effective_preflush_gate:
                            yield "data: " + json.dumps({"t": _tok}) + "\n\n"

                # ── end ainxt-api integration ─────────────────────────────────

                # ── Extract piggybacked memory footer from buffered tail ───
                # The LLM appends <!--MEMORY:{...}--> as the last line.
                # It was suppressed from streaming above; parse it here for
                # backend use only — the user never sees it.
                _piggybacked_memory: dict = {}
                try:
                    import re as _re_mem
                    _mem_pattern = _re_mem.compile(
                        r'\n?<!--MEMORY:(\{.*?\})-->\s*$', _re_mem.DOTALL
                    )
                    # Search buffered footer first (fast path), then full text
                    _mem_match = _mem_pattern.search(_mem_buf or _full)
                    if not _mem_match:
                        _mem_match = _mem_pattern.search(_full)
                    if _mem_match:
                        _piggybacked_memory = json.loads(_mem_match.group(1))
                        # Ensure _full is clean (strip footer if buffering missed it)
                        _full = _full[:_full.rfind(_MEM_SENTINEL)].rstrip() if _MEM_SENTINEL in _full else _full
                        logger.debug(
                            f"[memory-piggyback] extracted store={_piggybacked_memory.get('store')} "
                            f"context_key={_piggybacked_memory.get('context_key')!r} "
                            f"summary={_piggybacked_memory.get('summary', '')!r}"
                        )
                    else:
                        # Footer was buffered but malformed — flush buffer to _full only
                        # (already in _full via _full += _tok above); nothing to yield
                        logger.debug("[memory-piggyback] no valid footer found in stream")
                except Exception as _mp_err:
                    logger.debug(f"[memory-piggyback] footer parse failed: {_mp_err}")

                # ── Post-stream grounding advisory (non-blocking) ─────────────
                # Tokens were already streamed live above. Run the grounding check
                # on the completed _full text and emit a hedge notice as a trailing
                # SSE frame if confidence is low. This is purely advisory — it never
                # delays or replaces the live stream. Fail-safe: any error is ignored.
                #
                # Doc-attribution narrowing (multi-doc DocPickerCard selections
                # only): _gate_chunks now carries each source's REAL doc_id as
                # its Chunk id (not a throwaway array index) so
                # _preflush_grounding_hedge can tell us which of the user's
                # SELECTED documents actually contributed evidence to this
                # specific answer. When the user picked N docs from the picker
                # but the answer only genuinely drew on a subset of them, the
                # Sources panel is narrowed to just that subset — otherwise it
                # keeps showing every doc the user selected even though only
                # one of them was actually used, which is confusing (a real
                # UAT-reported issue). Only applies when 2+ docs were selected
                # (nothing to narrow with 0 or 1); falls back to the full
                # selected-docs list on any failure/empty result — a user is
                # never left with zero sources.
                if _full and globals().get("_PIPELINE_V2_GROUNDING"):
                    try:
                        _gate_chunks = [
                            {"id": (_s.get("doc_id") or str(_i)), "text": (_s.get("snippet") or ""),
                             "score": _s.get("score", 0)}
                            for _i, _s in enumerate(_fp_sources_meta or [])
                            if _s.get("snippet")
                        ]
                        _hedge, _contributing_doc_ids = _preflush_grounding_hedge(_full, _gate_chunks)
                        if len(_chat_scope_doc_ids) > 1 and _contributing_doc_ids:
                            _contrib_set = set(_contributing_doc_ids)
                            _narrowed_sources = [
                                s for s in (_fp_sources_meta or [])
                                if s.get("doc_id") in _contrib_set
                            ]
                            if _narrowed_sources:
                                logger.info(
                                    f"[KB_SOURCES] narrowed to contributing docs — "
                                    f"selected={_chat_scope_doc_ids} "
                                    f"contributing={_contributing_doc_ids} "
                                    f"sources_before={len(_fp_sources_meta)} "
                                    f"sources_after={len(_narrowed_sources)}"
                                )
                                _fp_sources_meta = _narrowed_sources
                            # else: contributing doc_ids matched no entry in
                            # _fp_sources_meta (shouldn't happen, but never
                            # narrow to an empty list) — keep the full
                            # selected-docs list untouched.
                        if _hedge:
                            yield "data: " + json.dumps({"t": _hedge}) + "\n\n"
                    except Exception:
                        pass
                # Redact _full in-place for history persistence (cloud KB chat).
                # The live stream was already sent unredacted; this only affects
                # what gets written to the conversation store.
                _full = _out_redact(_full)

                # Capture model metadata BEFORE releasing semaphore —
                # prevents another Ollama call from overwriting last_model_label.
                _gmeta["latency"] = time.time() - _gt0
                # Prefer sentinel values (carried as data through the
                # generator) over the router's thread-local fallbacks.
                # PERF FIX: the thread-local fallback (last_input_tokens) is
                # unreliable when anyio's threadpool hops threads between the
                # final _propagate_tokens write and this read — producing
                # in_tok=0 or in_tok=1 in the budget log. When the sentinel
                # is absent (in_tok=0), estimate from prompt char count rather
                # than reading a potentially stale thread-local.
                # Prefer: ainxt-api model > stream_meta sentinel > router thread-local > "auto"
                #
                # The preference is valid ONLY when ainxt-api actually served this
                # turn. `_ainxt_model` is assigned before the ainxt-api try block
                # (the user's picked model, verbatim), so it stays bound even when
                # that hop raises — disabled, unreachable, or no AINXT_API_URL — and
                # the `except` falls back to _mr.async_stream(). Keying off
                # locals() therefore pinned the footer to the PICKED model and
                # discarded the real one from _stream_meta, so a turn that fell
                # back to Claude still reported whatever the user had selected.
                # _ainxt_served is set only on the ainxt-api streaming success path.
                _ainxt_model_used = locals().get("_ainxt_model", "") if _ainxt_served else ""
                _gmeta["model"]   = (
                    _ainxt_model_used
                    or _resolve_model_id(_stream_meta.get("model_id") or _stream_meta.get("model_label") or getattr(_mr, "last_model_id", None) or getattr(_mr, "last_model_label", ""))
                )
                _sentinel_in  = int(_stream_meta.get("in_tok",  0) or 0)
                _sentinel_out = int(_stream_meta.get("out_tok", 0) or 0)
                # Use sentinel when available; fall back to char-based estimate
                # (never to the stale thread-local which produces in_tok=1).
                _prompt_chars = sum(
                    len(str(m.get("content") or "")) for m in _fp_messages
                ) if _fp_messages else len(safe_question)
                _real_in  = _sentinel_in  if _sentinel_in  > 1 else max(int(_prompt_chars / 4), 1)
                _real_out = _sentinel_out if _sentinel_out > 0 else int(len(_full.split()) * 1.3)
                _gmeta["in_tok"]  = _real_in
                _gmeta["out_tok"] = _real_out
                _gmeta["cost"]    = _estimate_cost(_gmeta["model"], _gmeta["in_tok"], _gmeta["out_tok"])
                if _stream_thinking:
                    # Stash for the __meta__ emit later (existing code paths
                    # already read getattr(_mr, "last_thinking_text") — keep
                    # working alongside).
                    _gmeta["thinking_text_captured"] = _stream_thinking
            except Exception as _ge:
                logger.exception(
                    f"Generic fast-path stream failed request_id={request_id} "
                    f"chat_id={_chat_id} {repr(_ge)[:1500]}"
                )
                # Phase 6.4: graceful degradation when the provider rejects the
                # request because the assembled prompt exceeds the model's
                # context window. Give the user an actionable next step instead
                # of a bare "Error generating response".
                _err_s = repr(_ge).lower()
                if any(k in _err_s for k in (
                    "context_length", "context length", "maximum context",
                    "too many tokens", "context window", "reduce the length",
                    "prompt is too long", "string too long",
                )):
                    yield "data: " + json.dumps({"t":
                        "\nThis conversation has grown too long for the current model's context "
                        "window. Try starting a new chat, or ask me to summarize the discussion "
                        "so far so we can continue with a smaller context."
                    }) + "\n\n"
                else:
                    yield "data: " + json.dumps({"t": "\nError generating response"}) + "\n\n"
                _gmeta["latency"] = time.time() - _gt0
                # Populate metadata from sentinel / router fallbacks even on
                # failure so the UI still shows model name, token counts, and
                # cost instead of blank chips.
                try:
                    _gmeta["model"]   = _resolve_model_id(_stream_meta.get("model_id") or _stream_meta.get("model_label") or getattr(_mr, "last_model_id", None) or getattr(_mr, "last_model_label", ""))
                    _real_in  = int(_stream_meta.get("in_tok",  0) or getattr(_mr, "last_input_tokens",  0) or 0)
                    _real_out = int(_stream_meta.get("out_tok", 0) or getattr(_mr, "last_output_tokens", 0) or 0)
                    _gmeta["in_tok"]  = _real_in  if _real_in  > 0 else _gmeta.get("in_tok", 0)
                    _gmeta["out_tok"] = _real_out if _real_out > 0 else _gmeta.get("out_tok", 0)
                    _gmeta["cost"]    = _estimate_cost(_gmeta["model"], _gmeta["in_tok"], _gmeta["out_tok"])
                except Exception:
                    pass
            finally:
                _LLM_SEMAPHORE.release()
                _gen_deregister(request_id)   # clean up stop-flag entry
            # ── Eval: general fast-path response (fire-and-forget) ────────
            # platform: use q.rag_mode (what the client EXPLICITLY sent),
            #   NOT _rag_mode which may have been overridden by the stored
            #   Chat DB row value (e.g. a chat previously used as KB chat
            #   has rag_mode="on" in DB — _rag_mode picks that up and would
            #   wrongly tag the eval row as "knowledge_base" even though the
            #   user is in the generic Chat tab).
            #   Chat.jsx always sends "off" → "chat".
            #   KbChat.jsx sends "on"/"auto" → "knowledge_base".
            # model: _gmeta["model"] is already resolved above (~line 7380)
            #        from last_model_label / stream_meta — pass it through
            #        so groundedness rows record which model was judged.
            if _full:
                try:
                    from core.evals import eval_engine as _eval_eng
                    _fp_rag_explicit = (q.rag_mode or "").strip().lower()
                    # Platform derivation — strict priority order:
                    #   1. agent_id present → Agent Studio web path via orchestrator
                    #   2. rag_mode on/auto → knowledge_base
                    #   3. default          → chat
                    if getattr(q, "agent_id", None):
                        _fp_platform = "agent_studio"
                    elif _fp_rag_explicit in {"on", "auto"}:
                        _fp_platform = "knowledge_base"
                    else:
                        _fp_platform = "chat"
                    _fp_model    = _gmeta.get("model") or None
                    _eval_eng.eval_answer_quality(
                        safe_question, _full, [],
                        session_id=_chat_id,
                        platform=_fp_platform,
                        model=_fp_model,
                    )
                    # ── Prompt Quality (coach_prompt) eval ────────────────
                    # eval_answer_quality covers groundedness + relevance.
                    # coach_prompt must be triggered here directly — it cannot
                    # rely solely on the emit_coach_event → ingestor chain
                    # because the ingestor may short-circuit (dedup, DB error,
                    # ENABLE_COACH=false) before reaching its own eval dispatch.
                    # Calling it here guarantees a coach_prompt EvalResult row
                    # is written for every Chat response regardless of the
                    # Coach pipeline state.
                    _eval_eng.eval_coach_prompt(
                        prompt=safe_question,
                        session_id=_chat_id,
                        run_id=request_id,
                        platform=_fp_platform,
                        model=_fp_model,
                    )
                except Exception:
                    pass
            # ── Save chat history ─────────────────────────────────────────
            # Fast-path responses must persist conversation turns so the next
            # request's Postgres history load (lines 1537-1590) sees prior context.
            # Without this, _messages is always empty → every turn is a new session.
            # Mirrors response_stream() at lines 2461 + 2648.
            logger.info(f"[chat-history] reached save block _full_len={len(_full)} chat_id={_chat_id!r}")
            if _full:
                try:
                    from memory.redis_memory import RedisMemory as _RM_fp
                    _rms_fp = _RM_fp()
                    _fp_mem_meta = {"rag_mode": _rag_mode, "repo_filter": repo_filter}
                    _rms_fp.save_message(_chat_id, "user", safe_question, metadata=_fp_mem_meta)
                    _rms_fp.save_message(_chat_id, "assistant", _full[:2000], metadata=_fp_mem_meta)
                except Exception:
                    pass
                # Pin assistant message id so the client can later call
                # /ask/continue/{message_id} or /chats/.../messages/{id}/edit.
                import uuid as _uuid_fp
                _fp_user_msg_id = str(_uuid_fp.uuid4())
                _fp_ast_msg_id  = str(_uuid_fp.uuid4())
                _gmeta["message_id"]      = _fp_ast_msg_id
                _gmeta["user_message_id"] = _fp_user_msg_id
                try:
                    import datetime as _dt_fp
                    if not q.ephemeral:
                      logger.info(f"[chat-history] publishing to kafka chat_id={_chat_id!r} model={_gmeta.get('model')!r} chars={len(_full)}")
                      _kafka_produce(_TOPIC_CHAT_HISTORY, {
                        "chat_id":              _chat_id,
                        "user_id":              _user_id,
                        "question":             safe_question,
                        "answer":               _full,
                        "model":                _gmeta.get("model", ""),
                        "in_tok":               _gmeta.get("in_tok", 0),
                        "out_tok":              _gmeta.get("out_tok", 0),
                        "cost":                 _gmeta.get("cost", 0.0),
                        "latency":              _gmeta.get("latency"),
                        "language":             _detected_lang or "unknown",
                        "attachment_ids":       list(q.attachment_ids or []),
                        "project_id":           q.project_id or "",
                        "agent_id":             q.agent_id or "",
                        "title_hint":           safe_question[:80] if not q.chat_id else None,
                        "user_message_id":      _fp_user_msg_id,
                        "assistant_message_id": _fp_ast_msg_id,
                        "rag_mode":             _rag_mode,
                        "repo_filter":          repo_filter,
                        "ts":                   _dt_fp.datetime.utcnow().isoformat(),
                      }, key=_chat_id)
                      logger.info(f"[chat-history] kafka publish done chat_id={_chat_id!r}")
                except Exception as _kp_err:
                    logger.error(f"[chat-history] kafka publish failed: {_kp_err}")
                # ── Cross-chat user memory — piggybacked from LLM response ──
                # The LLM already decided store/summary/context_key inside the
                # <!--MEMORY:{...}--> footer we stripped above. Use those values
                # directly — zero extra LLM calls.
                try:
                    if (
                        _user_id and _user_id not in ("", "default")
                        and _piggybacked_memory.get("store") is True
                        and _piggybacked_memory.get("summary", "").strip()
                    ):
                        import threading as _mem_th
                        _pb_summary     = _piggybacked_memory["summary"].strip()
                        _pb_context_key = _piggybacked_memory.get("context_key", "").strip()
                        _pb_model       = _gmeta.get("model", "")
                        _pb_chat_id     = _chat_id
                        def _save_xchat_memory(
                            _s=_pb_summary, _ck=_pb_context_key,
                            _m=_pb_model, _cid=_pb_chat_id
                        ):
                            try:
                                from memory.postgres_memory import PostgresMemory as _PM_fp
                                _PM_fp().save_user_memory(
                                    _user_id,
                                    _s,
                                    metadata={"model": _m, "chat_id": _cid},
                                    rag_mode=_rag_mode,
                                    source_repo=repo_filter,
                                    context_hint=_ck,
                                )
                            except Exception as _xc_err:
                                logger.debug(f"fast-path cross-chat memory save skipped: {_xc_err}")
                        _mem_ctx = contextvars.copy_context()
                        _mem_th.Thread(target=lambda: _mem_ctx.run(_save_xchat_memory), daemon=True).start()
                except Exception:
                    pass
                # ── Rolling per-chat summary — also previously skipped on
                # fast-path. Required so long chats fall back to summary
                # gracefully when history exceeds the 150 K-token budget.
                try:
                    from memory.chat_summarizer import update_chat_summary as _ucs
                    import threading as _sum_th
                    _sum_ctx = contextvars.copy_context()
                    _sum_th.Thread(
                        target=lambda: _sum_ctx.run(_ucs, _chat_id, safe_question, _full),
                        daemon=True,
                    ).start()
                except Exception:
                    pass
            # ── Budget: record actual token + cost usage ──────────────────
            # CRITICAL: this must run on every fast-path response so that
            # check_budget() sees non-zero tokens_used on subsequent requests.
            # Without this, token/cost budgets never trigger on this path.
            # Note: _estimate_cost() already returns 0.0 for local models, so
            # cost_usd is always 0 here for local routes — tokens are still logged.
            try:
                from store.budget_store import (
                    increment_usage as _bu_inc,
                    get_usage_today as _bu_gut,
                    get_budget as _bu_gb,
                )
                _bu_tok = _gmeta["in_tok"] + _gmeta["out_tok"]
                _bu_inc(_user_id, tokens=_bu_tok, requests=0, cost_usd=_gmeta["cost"])
                # Attach live budget state to __meta__ so Chat.jsx updates the bar
                # — but ONLY for cloud models. Local models are free and have no
                # budget allocation, so showing budget chips is misleading.
                if not _is_local_route:
                    _bu_usage  = _bu_gut(_user_id)
                    _bu_limits = _bu_gb(_user_id)
                    _gmeta["budget"] = {
                        "tokens_today":   _bu_usage.get("tokens_used", 0),
                        "requests_today": _bu_usage.get("requests_made", 0),
                        "cost_today":     _bu_usage.get("cost_usd_spent", 0.0),
                    }
                    if _bu_limits:
                        _gmeta["budget"]["max_tokens_total"]   = _bu_limits.get("max_tokens_total", 0)
                        _gmeta["budget"]["max_requests_total"] = _bu_limits.get("max_requests_total", 0)
                        _gmeta["budget"]["max_cost_total"]     = _bu_limits.get("max_cost_usd_total", 0.0)
            except Exception:
                pass
            # ── model_usages audit row (ainxt.metrics) ────────────────────────
            # _general_stream() (fast-path CHAT) was previously missing this
            # produce call — budget_store above got the tokens/cost, but no
            # per-request model_usages row was written for the majority of CHAT
            # traffic. Added so CHAT turns show up in the same audit + chargeback
            # surface as orchestrator / CLI / IDE paths.
            try:
                from core.time_utils import now_ist_iso as _now_ist_iso_gs
                _gs_cs = getattr(request.state, "client_source", "platform")
                # Buddy surface (mode="office") is reached ONLY through the desktop
                # app's Electron BrowserWindow — the sidebar entry is desktopOnly and
                # every request from that window is tagged x-ainxt-surface: desktop by
                # main.js's webRequest interceptor. There is no web-based Buddy client,
                # so tag every mode="office" request DESKTOP-BUDDY unconditionally
                # (previously a WEB-BUDDY fallback existed here for a "future web
                # Buddy" that never shipped and never fires the DESKTOP-BUDDY branch
                # correctly, it just misclassified real desktop Buddy usage).
                _gs_channel = (
                    "CLI"           if _is_cli else
                    "DESKTOP-BUDDY" if q.mode == "office" else
                    # Non-Buddy: desktop app → DESKTOP-CHAT, browser → WEB-CHAT.
                    "DESKTOP-CHAT"  if _gs_cs == "desktop" else
                    "WEB-CHAT"
                )
                _kafka_produce("ainxt.metrics", {
                    "event":          "llm_cost",
                    "request_id":     request_id,
                    "user_id":        _user_id,
                    "agent_id":       "orchestrator",
                    "endpoint":       "/ask",
                    "source_channel": _gs_channel,
                    "model":          _resolve_model_id(_gmeta.get("model", "")),
                    "input_tokens":   _gmeta.get("in_tok", 0),
                    "output_tokens":  _gmeta.get("out_tok", 0),
                    "total_tokens":   _gmeta.get("in_tok", 0) + _gmeta.get("out_tok", 0),
                    "latency_ms":     _gmeta.get("latency", 0.0) * 1000,
                    "cost_usd":       _gmeta.get("cost", 0.0),
                    "product_id":     None,
                    "timestamp":      _now_ist_iso_gs(),
                })
                logger.info(
                    f"[ask] fast-path model_usages produced request_id={request_id} user={_user_id} "
                    f"channel={_gs_channel} model={_gmeta.get('model')} cost=${_gmeta.get('cost', 0.0):.6f}"
                )
            except Exception as _gs_mu_err:
                logger.warning(
                    f"[ask] fast-path ainxt.metrics produce FAILED "
                    f"request_id={request_id} user={_user_id}: {_gs_mu_err}"
                )
            _gmeta["source"]   = "llm"
            _gmeta["llm_used"] = True
            _gmeta["rag_mode"] = _rag_mode
            if _fp_sources_meta:
                _gmeta["sources"] = _fp_sources_meta
                _gmeta["chunk_count"] = len(_fp_sources_meta)
            # Phase 3 transparency — surface coverage tier decisions to the UI
            # (kn_rewrite.md §8x). Populated by hybrid_retriever when the
            # escalation gate evaluated this query. Chat.jsx renders a small
            # badge ("Read all 312 sections" / "Fast tier sufficient") under
            # the answer.
            try:
                _cov_out = (_user_ctx or {}).get("_coverage_trace_out")
                if _cov_out:
                    _gmeta["coverage_trace"] = _cov_out
            except Exception:
                pass
            # Forward extended-thinking content (Claude reasoning) if any.
            try:
                _thinking = getattr(_mr, "last_thinking_text", "") or ""
                if _thinking:
                    _gmeta["thinking"] = _thinking[:8000]
            except Exception:
                pass
            yield "data: " + json.dumps({"__meta__": _gmeta}) + "\n\n"
            _emit_coach(_gmeta)

        # ── STEP 5.6: DOC GENERATION INTENT INTERCEPT ────────────────────────
        # Intercept BEFORE the LLM call when the user wants to generate a document.
        # Three cases handled:
        #   1. File uploaded + doc intent → convert/manipulate the uploaded file
        #   2. No file + doc intent + chat history → generate doc from conversation
        #   3. No file + doc intent + no history → pure generation (falls through)
        # ─────────────────────────────────────────────────────────────────────────
        try:
            from workers.chat_worker import _is_doc_intent, _is_convert_intent, _detect_doc_format, _CONVERT_COMMAND_RE
            # /convert <fmt> commands must always go through chat_worker's
            # _handle_doc_conversion path (which calls convert_doc_job).
            # Exclude them from the gateway's doc-intent shortcut so the
            # explicit file conversion logic is not bypassed.
            #
            # IMPORTANT: use clean_question (the raw user turn, pre-attachment-
            # injection) rather than safe_question here. safe_question has the
            # full parsed attachment text prepended to it (STEP 0), so regex
            # patterns in _is_convert_intent / _is_doc_intent can match phrases
            # inside the uploaded document (e.g. "pdf ( mandatory ) (max file …)"
            # triggers _DOC_CONVERT_RE) and falsely fire doc-generation for a
            # plain "explain this document" request. clean_question is always the
            # user's own words only — the correct signal for intent detection.
            _intent_q = clean_question
            _is_explicit_convert = bool(_CONVERT_COMMAND_RE.search(_intent_q))
            _doc_intent_fired = (
                not _is_explicit_convert
                and (
                    _is_doc_intent(_intent_q, has_attachment=bool(q.attachment_ids))
                    or (q.attachment_ids and _is_convert_intent(_intent_q))
                )
            )
        except Exception:
            _doc_intent_fired = False

        # ── STEP 5.6-pre: DOC EDIT FOLLOW-UP ─────────────────────────────────
        # When a document has already been generated in this chat
        # (md:session:{chat_id} exists) AND the user references the file or
        # uses an edit verb against a document noun, route to the edit
        # pipeline instead of replying as plain chat. The edit re-runs
        # agents.doc_generator_agent.edit_md_doc on the persisted sections
        # and republishes a fresh [DOCJOB:...] marker in the original format.
        #
        # Skipped when:
        #   - a slash command fired (handled by 5.6 / 5.6b below — user is
        #     explicit and we honor it)
        #   - an attachment is present (handled by 5.6 — attachments imply
        #     a new generation/conversion, not an edit of the prior doc)
        if (not _AINXT_API_ENABLED) and (not _doc_intent_fired) and (not q.attachment_ids) and q.chat_id:
            try:
                from workers.chat_worker import _is_doc_edit_followup
                _edit_is, _edit_session = _is_doc_edit_followup(
                    safe_question, q.chat_id
                )
            except Exception as _eup_err:
                logger.warning(
                    f"gateway STEP 5.6-pre: edit-followup check failed: {_eup_err}"
                )
                _edit_is, _edit_session = (False, None)

            if _edit_is and _edit_session:
                try:
                    _edit_doc = (_edit_session.get("document") or {})
                    _edit_filename = _edit_doc.get("filename") or "document.md"
                    _edit_title    = _edit_doc.get("title")    or "Document"
                    _edit_ext      = (_edit_doc.get("original_format") or "md").lower().strip(".") or "md"

                    _edit_job_id = str(uuid.uuid4())
                    from core.job_queue import enqueue_job, Q_DOC
                    enqueue_job(
                        "workers.doc_worker_agent.generate_md_job",
                        {
                            "job_id":         _edit_job_id,
                            "question":       safe_question,
                            "chat_id":        q.chat_id or "",
                            "user_id":        _user_id or "unknown",
                            "mode":           "edit",
                            "attachment_ids": [],
                        },
                        queue_name=Q_DOC,
                        timeout=1800,
                        retry_count=0,
                    )

                    _edit_answer = (
                        f"Applying your edit to **\"{_edit_title}\"**.\n\n"
                        f"The updated download button will appear below once it's ready.\n\n"
                        f"[DOCJOB:{_edit_job_id}:{_edit_ext}:{_edit_filename}]"
                    )

                    logger.info(
                        f"gateway STEP 5.6-pre: doc-edit job={_edit_job_id} "
                        f"ext={_edit_ext} filename={_edit_filename!r} user={_user_id}"
                    )

                    # ── Persist the chat turn (user prompt + assistant marker) ──
                    # Parity with routers/doc_download_router.py:213-236 — without
                    # this publish the edit bubble vanishes on refresh because the
                    # doc_worker_agent only writes generated_documents + the file,
                    # never chat_messages. The kafka consumer
                    # (workers/kafka_consumer.py:_handle_chat_history) inserts the
                    # user + assistant rows from this event.
                    try:
                        from core.kafka_producer import produce, TOPIC_CHAT_HISTORY
                        produce(TOPIC_CHAT_HISTORY, {
                            "chat_id":              q.chat_id,
                            "user_id":              _user_id or "unknown",
                            "question":             safe_question,
                            "answer":               _edit_answer,
                            "assistant_message_id": str(uuid.uuid4()),
                            "job_id":               _edit_job_id,
                            "request_id":           request_id,
                            "title_hint":           safe_question[:400],
                        }, key=q.chat_id)
                        logger.info(
                            f"gateway STEP 5.6-pre: doc-edit chat_history published "
                            f"job={_edit_job_id} chat_id={q.chat_id}"
                        )
                    except Exception as _persist_err:
                        # Non-fatal: the doc still generates; only refresh loses
                        # the bubble. Matches doc_download_router's posture.
                        logger.warning(
                            f"gateway STEP 5.6-pre: chat_history publish failed: {_persist_err}"
                        )

                    def _edit_doc_stream():
                        yield "data: " + json.dumps({"t": _edit_answer}) + "\n\n"
                        _edit_meta = {
                            "tokens": 0, "in_tok": 0, "out_tok": 0,
                            "cost": 0.0, "model": "doc_generator",
                            "latency": 0.0, "source": "doc_edit",
                        }
                        yield "data: " + json.dumps({"__meta__": _edit_meta}) + "\n\n"
                        _emit_coach(_edit_meta)

                    return StreamingResponse(
                        _edit_doc_stream(),
                        media_type="text/event-stream",
                        headers={
                            "X-Request-ID":      request_id,
                            "Cache-Control":     "no-cache",
                            "X-Accel-Buffering": "no",
                        },
                    )
                except Exception as _edit_err:
                    logger.error(
                        f"gateway STEP 5.6-pre: doc-edit enqueue failed "
                        f"(falling through to chat): {_edit_err}",
                        exc_info=True,
                    )
                    # Fall through to normal chat — non-fatal

        if (not _AINXT_API_ENABLED) and _doc_intent_fired and q.attachment_ids:
            try:
                from workers.chat_worker import (
                    _is_doc_intent, _is_convert_intent, _detect_doc_format,
                )
                # Use clean_question (user's words only) — same reason as the
                # _doc_intent_fired check above: safe_question contains injected
                # attachment text that can produce false-positive matches.
                _att_has_doc_intent = (
                    _is_doc_intent(clean_question, has_attachment=True)
                    or _is_convert_intent(clean_question)
                )
                if _att_has_doc_intent:
                    _doc_fmt = _detect_doc_format(clean_question)
                    # Fetch source filename + extension for smart naming and format inference
                    _src_doc_name = ""
                    _src_ext = ""
                    try:
                        from db.database import SessionLocal as _DocSL
                        from db.models import ChatAttachment as _DocAtt
                        _ddb = _DocSL()
                        try:
                            _datt = _ddb.query(_DocAtt).filter(
                                _DocAtt.id == q.attachment_ids[0]
                            ).first()
                            if _datt:
                                _src_doc_name = _datt.file_name or ""
                                _src_ext = (_datt.file_type or "").lower().strip(".")
                        finally:
                            _ddb.close()
                    except Exception:
                        pass

                    # BUG #1 FIX: When format detection falls back to "pdf" but the
                    # uploaded file has a more specific format, infer from source extension.
                    # e.g. user uploads CSV + says "add rows" → format should be xlsx not pdf.
                    _EXT_FMT_MAP = {
                        "csv": "xlsx", "xlsx": "xlsx", "xls": "xlsx",
                        "docx": "docx", "doc": "docx",
                        "pptx": "pptx", "ppt": "pptx",
                        "txt": "txt", "md": "md",
                    }
                    if _doc_fmt == "pdf" and _src_ext in _EXT_FMT_MAP:
                        _inferred = _EXT_FMT_MAP[_src_ext]
                        if _inferred != "pdf":
                            logger.info(
                                f"gateway STEP 5.6: format inferred from source ext "
                                f"pdf→{_inferred} (src_ext={_src_ext!r})"
                            )
                            _doc_fmt = _inferred

                    # Enqueue doc generation job
                    _doc_job_id = str(uuid.uuid4())
                    try:
                        from core.job_queue import enqueue_job, Q_DOC
                        enqueue_job(
                            "workers.doc_worker_agent.generate_doc_from_question",
                            {
                                "job_id":          _doc_job_id,
                                "question":        safe_question,
                                "format":          _doc_fmt,
                                "user_id":         _user_id or "unknown",
                                "chat_id":         q.chat_id or "",
                                "source_doc_name": _src_doc_name,
                                "attachment_ids":  list(q.attachment_ids or []),
                            },
                            queue_name=Q_DOC,
                            timeout=1800,
                            retry_count=0,
                        )
                        from tools.doc_generator import FORMAT_EXTENSIONS as _FMT_EXT
                        _doc_ext = _FMT_EXT.get(_doc_fmt, _doc_fmt)
                        _doc_filename = (
                            f"{_src_doc_name.rsplit('.', 1)[0]}.{_doc_ext}"
                            if _src_doc_name else f"document.{_doc_ext}"
                        )
                        _doc_answer = (
                            f"I'm generating your **{_doc_ext.upper()}** from "
                            f"**{_src_doc_name or 'the uploaded file'}**.\n\n"
                            f"The download button will appear below once it's ready.\n\n"
                            f"[DOCJOB:{_doc_job_id}:{_doc_ext}:{_doc_filename}]"
                        )
                        logger.info(
                            f"gateway: doc intent intercepted — fmt={_doc_fmt} "
                            f"job={_doc_job_id} src={_src_doc_name!r} "
                            f"user={_user_id}"
                        )

                        def _doc_intent_stream():
                            yield "data: " + json.dumps({"t": _doc_answer}) + "\n\n"
                            _doc_meta = {
                                "tokens": 0, "in_tok": 0, "out_tok": 0,
                                "cost": 0.0, "model": "doc_generator",
                                "latency": 0.0, "source": "doc_generation",
                            }
                            yield "data: " + json.dumps({"__meta__": _doc_meta}) + "\n\n"
                            _emit_coach(_doc_meta)

                        return StreamingResponse(
                            _doc_intent_stream(),
                            media_type="text/event-stream",
                            headers={
                                "X-Request-ID":      request_id,
                                "Cache-Control":     "no-cache",
                                "X-Accel-Buffering": "no",
                            },
                        )
                    except Exception as _doc_enq_err:
                        logger.error(f"gateway: doc intent enqueue failed: {_doc_enq_err}")
                        # Fall through to normal LLM response on enqueue failure
            except Exception as _doc_att_err:
                logger.warning(f"gateway STEP 5.6: attachment doc intent failed (continuing): {_doc_att_err}")

        # ── STEP 5.6b: DOC FROM SLASH COMMAND OR CHAT CONTEXT ────────────────
        # Handles two sub-cases:
        #   b1. Explicit slash command (/pdf, /docx, /xlsx, /pptx) with no attachment
        #       → pure generation from the user's question (no chat context needed)
        #   b2. Natural-language doc intent with chat history
        #       → generate doc from conversation context
        # Both sub-cases enqueue generate_doc_from_question and return immediately.
        # This prevents the /pdf command from falling through to the agent path
        # which causes an unhandled ASGI exception.
        _SLASH_DOC_RE = re.compile(
            r"^/(pdf|docx?|word|xlsx?|excel|csv|pptx?|pptagent|ppt|md|txt|text)\b",
            re.IGNORECASE,
        )
        if (not _AINXT_API_ENABLED) and _doc_intent_fired and not q.attachment_ids:
            try:
                _doc_fmt_ctx = _detect_doc_format(safe_question)

                # Strip slash command prefix from question for clean LLM prompt
                _clean_question = re.sub(
                    r"^/(pdf|docx?|word|xlsx?|excel|csv|pptx?|pptagent|ppt|md|txt|text)\s*",
                    "", safe_question, flags=re.IGNORECASE,
                ).strip() or safe_question

                # Collect conversation context: rolling summary (older history,
                # already compacted) + last ~6 raw turns. 
                # STRICT ISOLATION: _messages is keyed by this chat_id and is
                # already rag_mode-filtered (KB/codebase turns excluded when
                # Generic). The rolling summary is a single merged blob that
                # CANNOT be rag_mode-filtered, so — exactly like the chat path —
                # we skip it for Generic (rag off) requests and rely on the
                # filtered raw turns. Result: a chat-sourced doc only ever sees
                # THIS chat's history, never the knowledge base or codebase.
                _ctx_turns = []
                for _m in (_messages or [])[-12:]:   # last 12 messages = ~6 turns
                    _role = _m.get("role", "")
                    _content = (_m.get("content") or "").strip()
                    if _role in ("user", "assistant") and _content:
                        _label = "User" if _role == "user" else "Assistant"
                        _ctx_turns.append(f"{_label}: {_content[:2000]}")

                _ctx_recent = "\n\n".join(_ctx_turns) if _ctx_turns else ""

                _ctx_summary = ""
                if _rag_mode != "off":
                    try:
                        from memory.chat_summarizer import get_chat_summary as _get_chat_summary
                        _ctx_summary = (_get_chat_summary(_chat_id) or "").strip()[:4000] if _chat_id else ""
                    except Exception as _sum_err:
                        logger.debug(f"gateway STEP 5.6b: rolling summary fetch skipped: {_sum_err}")

                if _ctx_summary and _ctx_recent:
                    _chat_context = (
                        f"[Earlier conversation summary]\n{_ctx_summary}\n\n"
                        f"[Recent conversation]\n{_ctx_recent}"
                    )
                elif _ctx_summary:
                    _chat_context = f"[Earlier conversation summary]\n{_ctx_summary}"
                else:
                    _chat_context = _ctx_recent
                _doc_job_id_ctx = str(uuid.uuid4())

                from core.job_queue import enqueue_job, Q_DOC
                from workers.doc_worker import MIME_TYPES as _DW_MIME
                _FMT_EXT_MAP = {
                    "docx": "docx", "doc": "docx", "word": "docx",
                    "pptx": "pptx", "ppt": "pptx",
                    "pdf":  "pdf",
                    "xlsx": "xlsx", "xls": "xlsx", "excel": "xlsx",
                    "csv":  "csv",  "txt": "txt",  "md":    "md",
                }
                _ctx_ext = _FMT_EXT_MAP.get(_doc_fmt_ctx, _doc_fmt_ctx)

                enqueue_job(
                    "workers.doc_worker_agent.generate_doc_from_question",
                    {
                        "job_id":         _doc_job_id_ctx,
                        "question":       _clean_question,
                        "format":         _doc_fmt_ctx,
                        "user_id":        _user_id or "unknown",
                        "chat_id":        q.chat_id or "",
                        "chat_context":   _chat_context,
                        "attachment_ids": [],
                    },
                    queue_name=Q_DOC,
                    timeout=1800,
                    retry_count=0,
                )

                # Derive a clean filename from the question
                _ctx_slug = re.sub(r"[^\w\s-]", "", _clean_question.lower())
                _ctx_slug = re.sub(r"[\s_]+", "_", _ctx_slug.strip())[:50] or "document"
                _ctx_filename = f"{_ctx_slug}.{_ctx_ext}"

                _is_slash = bool(_SLASH_DOC_RE.match(safe_question))
                if _is_slash:
                    _ctx_answer = (
                        f"I'm generating your **{_ctx_ext.upper()}** document.\n\n"
                        f"The download button will appear below once it's ready.\n\n"
                        f"[DOCJOB:{_doc_job_id_ctx}:{_ctx_ext}:{_ctx_filename}]"
                    )
                else:
                    _ctx_answer = (
                        f"I'm generating your **{_ctx_ext.upper()}** from this conversation.\n\n"
                        f"The download button will appear below once it's ready.\n\n"
                        f"[DOCJOB:{_doc_job_id_ctx}:{_ctx_ext}:{_ctx_filename}]"
                    )

                logger.info(
                    f"gateway STEP 5.6b: doc job={_doc_job_id_ctx} "
                    f"fmt={_doc_fmt_ctx} slash={_is_slash} turns={len(_ctx_turns)} user={_user_id}"
                )

                def _ctx_doc_stream():
                    yield "data: " + json.dumps({"t": _ctx_answer}) + "\n\n"
                    _ctx_meta = {
                        "tokens": 0, "in_tok": 0, "out_tok": 0,
                        "cost": 0.0, "model": "doc_generator",
                        "latency": 0.0, "source": "doc_generation",
                    }
                    yield "data: " + json.dumps({"__meta__": _ctx_meta}) + "\n\n"
                    _emit_coach(_ctx_meta)

                return StreamingResponse(
                    _ctx_doc_stream(),
                    media_type="text/event-stream",
                    headers={
                        "X-Request-ID":      request_id,
                        "Cache-Control":     "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )
            except Exception as _ctx_err:
                logger.error(
                    f"gateway STEP 5.6b: doc generation routing failed: {_ctx_err}",
                    exc_info=True,
                )
                # Return a clean error response — do NOT fall through to agent
                # which would cause an unhandled ASGI exception for /pdf commands.
                def _doc_err_stream():
                    yield "data: " + json.dumps({"t": f"⚠️ Document generation failed: {_ctx_err}"}) + "\n\n"
                    yield "data: " + json.dumps({"__meta__": {
                        "tokens": 0, "in_tok": 0, "out_tok": 0,
                        "cost": 0.0, "model": "doc_generator",
                        "latency": 0.0, "source": "doc_generation_error",
                    }}) + "\n\n"
                return StreamingResponse(
                    _doc_err_stream(),
                    media_type="text/event-stream",
                    headers={
                        "X-Request-ID":      request_id,
                        "Cache-Control":     "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )

        # ── CIL ambiguity clarification gate ───────────────────────────────────
        # When the model-only classifier signals the turn is too vague to answer
        # safely, ask the user to elaborate instead of routing to a model and
        # getting a hallucinated answer. Skipped when the user explicitly picked
        # a model (they want an answer) or when a doc-topic clarification already
        # fired above.
        #
        # Also skipped when the user has already provided KB document context:
        #   • _chat_scope_did      — a single specific KB doc UUID is in scope
        #   • _chat_scope_doc_ids  — user selected one or more docs via DocPickerCard
        # In both cases the query is sufficiently scoped; the classifier's
        # clarification_needed flag (set before KB retrieval) is no longer valid.
        #
        # Also skipped when the turn is a follow-up reply to the LLM's own
        # question (e.g. user replies "yes", "no", "tell me more").
        # _is_followup is True when the condenser (models/followup_condenser.py)
        # judged the current question to depend on the conversation and
        # rewrote it into a standalone question (i.e. its output differs
        # from the original question) — see the "Follow-up detection +
        # condensation" block earlier in this function. In this case the
        # pipeline already anchors the RAG query to the conversation via that
        # rewritten question — firing the clarification gate here would
        # discard that and return a useless generic prompt to the user.
        _kb_doc_already_selected = bool(_chat_scope_did or _chat_scope_doc_ids)
        if (_PIPELINE_V2
                and _rc is not None
                and _rc.conv_state is not None
                and _rc.conv_state.clarification_needed
                and not _model_hint
                and not _kb_doc_already_selected
                and not _is_followup
                and not _has_history):
            _clar_msg = (
                "I'm not sure what you'd like me to do — could you give me a bit "
                "more detail? For example, what topic or task you have in mind."
            )
            logger.info(
                f"[ask] CIL clarification_needed=true → clarifying (no LLM) | "
                f"request_id={request_id} chat_id={_chat_id}"
            )
            _rc.dispatch = _DispatchDecision(lane=_Lane.CLARIFY, reason="cil_clarification_needed")
            _otel.record_event("dispatch", lane="clarify")

            def _cil_clarify_stream(_msg=_clar_msg, _cid=_chat_id):
                yield "data: " + json.dumps({"t": _msg}) + "\n\n"
                yield "data: " + json.dumps({"__meta__": {
                    "out_tok": 0, "in_tok": 0, "model": "cil-clarify",
                    "cost": 0.0, "latency": 0.0, "source": "cil_clarify",
                    "llm_used": False, "chat_id": _cid,
                }}) + "\n\n"

            return StreamingResponse(
                _cil_clarify_stream(),
                media_type="text/event-stream",
                headers={
                    "X-Request-ID":      request_id,
                    "Cache-Control":     "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        if _PIPELINE_V2 and _rc is not None:
            _rc.dispatch = _DispatchDecision(lane=_Lane.GENERAL, reason="fast-path tail")
            _otel.record_event("dispatch", lane="general")
        # _general_stream() is now an async generator — StreamingResponse
        # accepts both sync and async generators; async generators run directly
        # on the event loop so each yielded chunk is flushed to the client
        # the instant it is produced, matching the per-token SSE delivery of
        # the CLI path (/v1/messages).
        return StreamingResponse(
            _general_stream(),
            media_type="text/event-stream",
            headers={
                "X-Request-ID":      request_id,
                "Cache-Control":     "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ========================================================
    # STEP 5.7: INTENT ROUTING (model-based; regex retired on the chat path)
    # Route to a specific skill / named agent instead of the OrchestratorAgent.
    # Precedence:
    #   1. @mention  — explicit user override, always wins (via _mention_route).
    #   2. MODEL hint — conv_state.skill_hint / agent_hint from cil/intent.py.
    #   3. None       — normal orchestrator path.
    # The keyword matcher (_intent_route_question / _SKILL_KEYWORDS) is NO LONGER
    # consulted for routing — the model decides. The DB PRODUCTION check below
    # stays the authoritative guard, so a bad/unknown hint falls through safely.
    # ========================================================
    _intent = None
    # 1. explicit @mention override (still deterministic — a user typing @name
    #    is an explicit instruction, not a heuristic guess).
    try:
        import re as _re_mention
        _mention = _re_mention.search(r"@([\w\-]+)", safe_question or "")
        if _mention:
            _intent = {"type": "agent", "name": _mention.group(1).lower()}
    except Exception:
        _intent = None
    # 2. model-derived skill/agent hint from the CIL.
    if _intent is None and _CIL_MODEL_ROUTING and _rc is not None and _rc.conv_state is not None:
        try:
            _cs = _rc.conv_state
            if getattr(_cs, "intent", "chat") == "agent" and getattr(_cs, "agent_hint", None):
                _intent = {"type": "agent", "name": str(_cs.agent_hint).lower()}
            elif getattr(_cs, "intent", "chat") == "skill" and getattr(_cs, "skill_hint", None):
                _intent = {"type": "skill", "name": str(_cs.skill_hint)}
        except Exception:
            _intent = None
    if _intent:
        # Verify entity exists and is PRODUCTION before committing
        _intent_ok = False
        try:
            from db.database import SessionLocal as _IESL
            from db.models import SkillRecord as _IESR, AgentRecord as _IEAR
            _iedb = _IESL()
            try:
                if _intent["type"] == "skill":
                    _ier = _iedb.query(_IESR).filter(
                        _IESR.name == _intent["name"], _IESR.status == "PRODUCTION"
                    ).first()
                else:
                    _ier = _iedb.query(_IEAR).filter(
                        _IEAR.name == _intent["name"], _IEAR.status == "PRODUCTION"
                    ).first()
                _intent_ok = _ier is not None
            finally:
                _iedb.close()
        except Exception:
            pass

        if _intent_ok:
            def _intent_stream():
                _it_full = ""
                _it_t0 = time.time()
                if not _LLM_SEMAPHORE.acquire(timeout=_SEM_ACQUIRE_TIMEOUT):
                    yield "data: " + json.dumps({"t": "\nServer busy — too many concurrent requests."}) + "\n\n"
                    return
                try:
                    if _intent["type"] == "skill":
                        from db.database import SessionLocal as _ISL
                        from db.models import SkillRecord as _ISR
                        _idb = _ISL()
                        try:
                            _srec = _idb.query(_ISR).filter(_ISR.name == _intent["name"]).first()
                            if _srec:
                                _ns: dict = {}
                                exec(_srec.code, _ns)   # noqa: S102
                                _sfn = _ns.get("run")
                                if _sfn:
                                    _res = _sfn(safe_question)
                                    _it_full = (
                                        _res.get("output", str(_res))
                                        if isinstance(_res, dict) else str(_res)
                                    )
                        finally:
                            _idb.close()
                    else:  # agent
                        from db.database import SessionLocal as _IAL
                        from db.models import AgentRecord as _IAR
                        from agents.agent_builder import AgentRunner as _IRunner
                        _idb2 = _IAL()
                        try:
                            _arec = _idb2.query(_IAR).filter(_IAR.name == _intent["name"]).first()
                            if _arec:
                                _runner = _IRunner(_arec)
                                _ires = _runner.run(safe_question)
                                _it_full = _ires.answer or ""
                        finally:
                            _idb2.close()
                except Exception as _ie:
                    logger.warning(f"Intent route failed ({_intent}): {_ie}")
                    _it_full = ""
                finally:
                    _LLM_SEMAPHORE.release()

                if not _it_full:
                    # Empty result — nothing to yield, frontend will show blank
                    yield "data: " + json.dumps({"t": f"⚠ Could not get a response from {_intent['name']}."}) + "\n\n"
                else:
                    # Emit structured tool-call events for UI cards. The legacy
                    # `tool_call: str` field is kept for back-compat with older
                    # frontends; new field `tool_event` carries the structured
                    # payload the UI renders as <ToolCallCard>.
                    if hasattr(_ires, "tool_outputs"):
                        for _tc in (_ires.tool_outputs or []):
                            _tc_name = _tc.get("tool", "unknown")
                            _tc_success = bool(_tc.get("success"))
                            _tc_ok = "✓" if _tc_success else "✗"
                            _tc_args = _tc.get("args") or _tc.get("input") or {}
                            _tc_out = _tc.get("output") or _tc.get("result") or ""
                            try:
                                _tc_out_str = json.dumps(_tc_out) if not isinstance(_tc_out, str) else _tc_out
                            except Exception:
                                _tc_out_str = str(_tc_out)
                            yield "data: " + json.dumps({
                                "tool_call":  f"🔧 Tool: {_tc_name} → {_tc_ok}",
                                "tool_event": {
                                    "name":    _tc_name,
                                    "status":  "success" if _tc_success else "error",
                                    "args":    _tc_args,
                                    "output":  _tc_out_str[:4000],
                                    "ts":      time.time(),
                                },
                            }) + "\n\n"
                    yield "data: " + json.dumps({"t": _it_full}) + "\n\n"
                    # Save to conversation memory
                    try:
                        from memory.redis_memory import RedisMemory as _IRM
                        _irm = _IRM()
                        _it_mem_meta = {"rag_mode": _rag_mode, "repo_filter": repo_filter}
                        _irm.save_message(_chat_id, "user", safe_question, metadata=_it_mem_meta)
                        _irm.save_message(_chat_id, "assistant", _it_full[:2000], metadata=_it_mem_meta)
                    except Exception:
                        pass

                _it_cost = _estimate_cost(
                    _OPENAI_CODING,
                    int(len(safe_question.split()) * 1.3),
                    int(len(_it_full.split()) * 1.3),
                )
                _it_meta = {
                    "tokens": 0, "in_tok": 0, "out_tok": 0,
                    "cost": _it_cost,
                    "model": f"intent:{_intent['type']}:{_intent['name']}",
                    "latency": time.time() - _it_t0,
                    "source": "llm",
                    "llm_used": True,
                }
                yield "data: " + json.dumps({"__meta__": _it_meta}) + "\n\n"
                _emit_coach(_it_meta)

            return StreamingResponse(
                _intent_stream(),
                media_type="text/event-stream",
                headers={
                    "X-Request-ID":      request_id,
                    "Cache-Control":     "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

    # ========================================================
    # STEP 5.8: REACT AGENTIC ROUTING
    # When the question is action-mode (fix, create, implement…),
    # route to ReactOrchestrator (Claude native tool-use loop)
    # instead of the classic pipeline-based OrchestratorAgent.
    # ========================================================
    # STEP 5.9: CLI DIRECT PATH
    # When the request comes from ainxt-cli (X-AiNxt-Client: cli/*)
    # or has cli_mode=True, skip ALL orchestration (no RAG, no React,
    # no OrchestratorAgent). The CLI handles its own agent loop
    # locally — the gateway is just a direct model relay.
    # _is_cli was computed once after auth and is reused here.
    # _outer_cs is kept for client_source tagging in history save.
    # ========================================================
    _outer_cs = getattr(request.state, "client_source", "platform")

    if _is_cli:
        logger.info(f"[CLI] direct model path (skip orchestrator) model_hint={_model_hint!r}")

        def _cli_direct_stream():
            _full  = ""
            _t0    = time.time()
            _cmeta = {"out_tok": 0, "in_tok": 0, "model": "auto", "cost": 0.0, "latency": 0.0}
            # Output compliance: stream tokens through a rolling redaction buffer.
            # PCI patterns (PAN, Aadhaar, mobile) are all <= 20 chars, so a 32-char
            # look-behind ensures we never split a pattern across a yielded chunk.
            _OUT_FLUSH_AT = 80
            _OUT_LOOKBEHIND = 32
            _out_buf = ""
            if not _LLM_SEMAPHORE.acquire(timeout=_SEM_ACQUIRE_TIMEOUT):
                yield "data: " + json.dumps({"t": "\nServer busy — please retry."}) + "\n\n"
                return
            try:
                from models.model_router import model_router as _mr_cli
                _cli_msgs = list(_messages)
                # Inject CLI system prompt at the front if provided
                if q.system_prompt:
                    _cli_msgs = [{"role": "system", "content": q.system_prompt}] + _cli_msgs

                # ── Image input branch ────────────────────────────────────
                # IMPORTANT: routes through services/llm_proxy/main.py
                # `POST /llm/generate-image` — never call Gemini directly.
                # The streaming-text path can't carry image blocks
                # (_ProxyGateway.generate at model_router.py:165 flattens
                # list-of-blocks to a useless string). So when images are
                # present we use the non-streaming proxy endpoint and yield
                # the result as one chunk.
                _images_payload = q.images if (q.images and isinstance(q.images, list)) else []
                _vision_handled = False
                if _images_payload:
                    _prompt_text = ""
                    if _cli_msgs:
                        _last = _cli_msgs[-1]
                        if isinstance(_last, dict) and _last.get("role") == "user":
                            _prompt_text = _last.get("content") if isinstance(_last.get("content"), str) else ""
                    # Compliance: redact at the gateway BEFORE the proxy call.
                    # Proxy's compliance check hard-blocks; CLI policy is
                    # redact-and-proceed.
                    try:
                        _prompt_safe, _ = _ce_ask.redact_text(_prompt_text or "describe this image")
                    except Exception:
                        _prompt_safe = _prompt_text or "describe this image"
                    _sys_safe = q.system_prompt or ""
                    if _sys_safe:
                        try:
                            _sys_safe, _ = _ce_ask.redact_text(_sys_safe)
                        except Exception:
                            pass
                    # /llm/generate-image supports one image. If multiple are
                    # attached we use the first and log a warning.
                    _first = next(
                        (im for im in _images_payload
                         if isinstance(im, dict) and im.get("data")),
                        None,
                    )
                    if _first is None:
                        yield "data: " + json.dumps({"t": "\n[Error: image payload missing 'data' field]"}) + "\n\n"
                        _cmeta["latency"] = time.time() - _t0
                        _vision_handled = True
                    else:
                        if len(_images_payload) > 1:
                            logger.info(
                                f"[CLI] {len(_images_payload)} images attached — "
                                f"proxy /llm/generate-image accepts 1; using first only"
                            )
                        _gem = _mr_cli._get_gemini()
                        if _gem is None or not hasattr(_gem, "generate_image"):
                            yield "data: " + json.dumps({"t": "\n[Error: vision gateway unavailable]"}) + "\n\n"
                            _cmeta["latency"] = time.time() - _t0
                            _vision_handled = True
                        else:
                            try:
                                _text_out, _in_tok, _out_tok, _actual_model = _gem.generate_image(
                                    prompt=_prompt_safe,
                                    image_b64=_first["data"],
                                    mime_type=_first.get("media_type") or "image/png",
                                    system_prompt=_sys_safe,
                                )
                                _text_out_safe = _out_redact(_text_out or "")
                                _full = _text_out_safe
                                if _text_out_safe:
                                    yield "data: " + json.dumps({"t": _text_out_safe}) + "\n\n"
                                _cmeta["model"]   = _actual_model or "vision-via-proxy"
                                _cmeta["in_tok"]  = _in_tok or int(len(_prompt_safe.split()) * 1.3)
                                _cmeta["out_tok"] = _out_tok or int(len((_text_out_safe or "").split()) * 1.3)
                                _cmeta["cost"]    = _estimate_cost(_cmeta["model"], _cmeta["in_tok"], _cmeta["out_tok"])
                                _cmeta["latency"] = time.time() - _t0
                                _vision_handled = True
                            except Exception as _img_err:
                                logger.error(f"[CLI] generate_image via proxy failed: {_img_err}")
                                yield "data: " + json.dumps({"t": f"\n[Vision error: {_img_err}]"}) + "\n\n"
                                _cmeta["latency"] = time.time() - _t0
                                _vision_handled = True

                if _vision_handled:
                    # Skip the text-streaming block — image call already
                    # produced output and set _cmeta. Fall through to history
                    # save + budget + meta below.
                    pass
                else:
                    # Default hint: respect user's explicit model choice; for
                    # trivial greetings/acks, downgrade to "mini" (gpt-5-mini)
                    # so "hi" doesn't burn 2-3s of Claude Sonnet latency.
                    # The user can still force claude via /model.
                    _hint_for_stream = _model_hint or "complex"
                    if not _model_hint and _TRIVIAL_QUERY_RE.match(safe_question.strip() or ""):
                        _hint_for_stream = "mini"
                        logger.info("[CLI] trivial query → routing to mini (gpt-5-mini)")
                    for _tok in _mr_cli.stream(_cli_msgs, model_hint=_hint_for_stream, local_model=_local_model):
                        if _tok:
                            _full += _tok
                            _out_buf += _tok
                            # Flush when buffer is large enough or we hit a line boundary.
                            # Keep last 32 chars in the buffer to avoid splitting a PCI
                            # pattern across the chunk boundary.
                            if len(_out_buf) >= _OUT_FLUSH_AT or "\n" in _out_buf:
                                if len(_out_buf) > _OUT_LOOKBEHIND:
                                    _emit_part = _out_buf[:-_OUT_LOOKBEHIND]
                                    _out_buf   = _out_buf[-_OUT_LOOKBEHIND:]
                                else:
                                    _emit_part = _out_buf
                                    _out_buf   = ""
                                _emit_red = _out_redact(_emit_part)
                                if _emit_red:
                                    yield "data: " + json.dumps({"t": _emit_red}) + "\n\n"
                    # Flush remaining buffer (final pass — apply redaction once more).
                    if _out_buf:
                        _emit_red = _out_redact(_out_buf)
                        if _emit_red:
                            yield "data: " + json.dumps({"t": _emit_red}) + "\n\n"
                        _out_buf = ""
                # Skip clobbering vision _cmeta values when we already populated
                # them in the image branch above.
                if not _vision_handled:
                    _cmeta["latency"] = time.time() - _t0
                    _cmeta["model"]   = _resolve_model_id(getattr(_mr_cli, "last_model_id", None) or getattr(_mr_cli, "last_model_label", ""))
                    _ri = getattr(_mr_cli, "last_input_tokens",  0) or 0
                    _ro = getattr(_mr_cli, "last_output_tokens", 0) or 0
                    _cmeta["in_tok"]  = _ri if _ri > 0 else int(len(safe_question.split()) * 1.3)
                    _cmeta["out_tok"] = _ro if _ro > 0 else int(len(_full.split()) * 1.3)
                    _cmeta["cost"]    = _estimate_cost(_cmeta["model"], _cmeta["in_tok"], _cmeta["out_tok"])
            except Exception as _ce:
                logger.error(f"[CLI] direct stream error: {_ce}")
                yield "data: " + json.dumps({"t": f"\n[Error: {_ce}]"}) + "\n\n"
                _cmeta["latency"] = time.time() - _t0
            finally:
                _LLM_SEMAPHORE.release()
            if _full:
                try:
                    import threading as _ct
                    # CLI agent mode: store only the task text in history, not the
                    # injected file context blobs. Storing the full file content
                    # (which may include PCI pattern definitions) contaminates session
                    # history and causes compliance blocks on future unrelated queries.
                    _hist_question = safe_question
                    if q.cli_mode and "---\n\nTask:" in safe_question:
                        _hist_question = safe_question.split("---\n\nTask:", 1)[-1].strip()
                    # Persist redacted response when COMPLIANCE_SCAN_LLM_OUTPUT is ON;
                    # default OFF stores the raw response (output is not scanned).
                    _full_for_hist = _out_redact(_full)
                    # client_source is REQUIRED here — without it the save
                    # defaults to "platform" and CLI prompts leak into the
                    # web Chat UI sidebar. _outer_cs is set by
                    # ClientSourceMiddleware from the X-AiNxt-Client header
                    # (CLI sends "cli/1.0.0").
                    _cli_hist_ctx = contextvars.copy_context()
                    _cli_hist_kw = {
                        "chat_id":        _chat_id,
                        "user_id":        _user_id,
                        "question":       _hist_question,
                        "answer":         _full_for_hist,
                        "model":          _cmeta["model"],
                        "in_tok":         _cmeta["in_tok"],
                        "out_tok":        _cmeta["out_tok"],
                        "cost":           _cmeta["cost"],
                        "language":       _detected_lang,
                        "attachment_ids": q.attachment_ids or [],
                        "project_id":     q.project_id,
                        "latency":        None,
                        "client_source":  _outer_cs,
                        # Phase 3 — persist coverage decision per turn so
                        # the badge survives a chat reload (Fix #1 from
                        # the screens audit). None on Generic/no-scope chats.
                        "coverage_trace": (_user_ctx or {}).get("_coverage_trace_out"),
                        "rag_mode":       _rag_mode,
                        "repo_filter":    repo_filter,
                    }
                    _ct.Thread(
                        target=lambda: _cli_hist_ctx.run(_save_chat_messages, **_cli_hist_kw),
                        daemon=True,
                    ).start()
                except Exception:
                    pass
            # ── Budget: record token + cost usage for CLI path ───────────
            # _estimate_cost() returns 0.0 for local models, so cost_usd is
            # always 0 for local routes — tokens are still logged for auditing.
            try:
                from store.budget_store import increment_usage as _bu_inc
                _bu_inc(_user_id, tokens=_cmeta["in_tok"] + _cmeta["out_tok"],
                        requests=0, cost_usd=_cmeta["cost"])
            except Exception:
                pass
            # ── model_usages audit row (ainxt.metrics) ────────────────────
            # This direct-CLI path (X-AiNxt-Client: cli/*, hitting /ask and
            # skipping the orchestrator) was previously missing this produce
            # call entirely — budget_store above got the tokens/cost, but no
            # per-request model_usages row was ever written for it, unlike
            # every other chat path (/ask via orchestrator, /v1/chat/completions,
            # /v1/messages). Added so CLI-via-/ask traffic shows up in the same
            # audit + chargeback surface as the rest.
            try:
                from core.time_utils import now_ist_iso as _now_ist_iso_cli
                _kafka_produce("ainxt.metrics", {
                    "event":         "llm_cost",
                    "request_id":    request_id,
                    "user_id":       _user_id,
                    "agent_id":      "cli",
                    "endpoint":      "/ask",
                    "source_channel": "CLI",
                    "model":         _resolve_model_id(_cmeta["model"]),
                    "input_tokens":  _cmeta["in_tok"],
                    "output_tokens": _cmeta["out_tok"],
                    "total_tokens":  _cmeta["in_tok"] + _cmeta["out_tok"],
                    "latency_ms":    _cmeta["latency"] * 1000,
                    "cost_usd":      _cmeta["cost"],
                    "product_id":    None,
                    "timestamp":     _now_ist_iso_cli(),
                })
                logger.info(
                    f"[CLI] direct model_usages produced request_id={request_id} user={_user_id} "
                    f"model={_cmeta['model']} cost=${_cmeta['cost']:.6f}"
                )
            except Exception as _cli_mu_err:
                logger.warning(f"[CLI] direct ainxt.metrics produce FAILED request_id={request_id} user={_user_id}: {_cli_mu_err}")
            _cmeta["source"]   = "llm"
            _cmeta["llm_used"] = True

            # ── eval_results (LLM-as-judge) ───────────────────────────────
            # When cli_mode=True the request goes here instead of _general_stream,
            # so the agent_id branch in _general_stream is never reached.
            # Derive platform the same way: agent_id → agent_studio, else cli.
            if _full:
                try:
                    from core.evals import eval_engine as _cli_ee, EVAL_ENABLED as _cli_ee_on
                    if _cli_ee_on:
                        if getattr(q, "agent_id", None):
                            _cli_eval_plat = "agent_studio"
                        else:
                            _cli_eval_plat = "cli"
                        _cli_q2  = safe_question[:500]
                        _cli_a2  = _full[:1000]
                        _cli_s2  = session_id
                        _cli_m2  = _cmeta.get("model") or None
                        import contextvars as _cli_cv2
                        def _run_cli_eval2(_q=_cli_q2, _a=_cli_a2, _s=_cli_s2, _p=_cli_eval_plat, _m=_cli_m2):
                            try:
                                _cli_ee.eval_answer_quality(_q, _a, [], session_id=_s, platform=_p, model=_m)
                            except Exception:
                                pass
                        _cli_ev_ctx = _cli_cv2.copy_context()
                        threading.Thread(
                            target=lambda: _cli_ev_ctx.run(_run_cli_eval2),
                            daemon=True, name="eval-cli-direct",
                        ).start()
                except Exception:
                    pass

            yield "data: " + json.dumps({"__meta__": _cmeta}) + "\n\n"
            _emit_coach(_cmeta)
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _cli_direct_stream(),
            media_type="text/event-stream",
            headers={
                "X-Request-ID":      request_id,
                "Cache-Control":     "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ========================================================
    # STEP 6: AGENT ORCHESTRATOR (REPLACES PIPELINE EXECUTION)
    # ========================================================

    # Build orchestrator question with conversation history injected
    # (cache key was already computed from clean `rewritten`, so cache
    # correctness is not affected by the prepended history).
    _orch_question = (
        _question_with_history.replace(
            f"[Current question]\n{safe_question}",
            f"[Current question]\n{rewritten}",
        )
        if _question_with_history != safe_question
        else rewritten
    )

    def response_stream():
        # Re-bind ContextVar on the anyio thread-pool worker that
        # StreamingResponse uses to iterate this sync generator.
        # Without this, _get_request_id() inside OpenAIGateway.generate()
        # returns "-" and the proxy mints a new UUID, breaking end-to-end
        # request_id stitching.  Mirrors the same pattern in _general_stream().
        set_request_id(request_id)
        set_correlation_id(request_id)

        full_answer = ""
        scope = "agent"
        retrieval_score = 1.0
        _span = tracer.trace_request(request_id, "/ask")
        telemetry_metrics.inc("requests_total")
        telemetry_metrics.inc("agent_executions")

        # Shared dict populated in finally; read by post-finally __meta__ yield.
        _meta = {"out_tok": 0, "in_tok": 0, "model": _OPENAI_CODING, "cost": 0.0, "latency": 0.0}

        # Acquire LLM concurrency slot — prevents >120 simultaneous agent.run() calls
        # under high load, protecting upstream API rate limits.
        if not _LLM_SEMAPHORE.acquire(timeout=_SEM_ACQUIRE_TIMEOUT):
            yield "data: " + json.dumps({"t": "\nServer busy — too many concurrent AI requests. Please retry in a moment."}) + "\n\n"
            return

        # Register this request so POST /chat/stop can signal cancellation.
        _gen_register(request_id)

        # ── Live status line (Phase 1.4) ─────────────────────────────────
        # The orchestrator path runs tools + RAG, so it can be slow to first
        # token. Emit a status line immediately so the UI feels alive.
        # Per-tool status transitions are emitted by tool_event handling below.
        try:
            yield "data: " + json.dumps({"status": "Thinking…"}) + "\n\n"
        except Exception:
            pass
        # Phase 2: emit context-window telemetry + compaction notice.
        try:
            if _context_info:
                yield "data: " + json.dumps({"context": _context_info}) + "\n\n"
                if _context_info.get("compacted"):
                    yield "data: " + json.dumps({"compaction": {
                        "message": "Earlier messages were summarized to keep the conversation within context.",
                    }}) + "\n\n"
        except Exception:
            pass

        try:

            _model_span = tracer.trace_model_call(request_id, "orchestrator", "stream")

            iterator = agent.run(
                _orch_question,
                repo_filter,
                model_hint=_model_hint,
                request_id=request_id,
                raw_question=safe_question,
                user_ctx=_user_ctx or None,
                messages=list(_messages),
                # Gateway already ran validate_input() above (line ~1512).
                # Skip the orchestrator's redundant ML compliance call.
                compliance_passed=True,
                rag_mode=_rag_mode,
                mode=q.mode,
            )

            _emitted_generating = False  # Phase 1.4: flip status on first token
            for token in iterator:

                # ── Cooperative stop-check ────────────────────────────────────
                if _gen_should_stop(request_id):
                    logger.info(f"[gen_registry] agent stream stopped by user request_id={request_id}")
                    break

                if token is None:
                    continue

                # ── Phase 5: first-class tool events (additive; fail-safe) ────
                # The orchestrator may yield a ToolMarker sentinel to signal tool
                # start/result. Translate it to a structured {tool:{...}} SSE
                # frame and skip it — it is NEVER part of the answer text. Old
                # clients ignore the `tool` key. Any failure falls through to the
                # normal token handling (the marker str()s to "" harmlessly).
                try:
                    from pipeline.stream_events import (
                        ToolMarker as _ToolMarker,
                        ReasoningMarker as _ReasoningMarker,
                    )
                    if isinstance(token, _ToolMarker):
                        if _PIPELINE_V2 and _PIPELINE_V2_STREAM:
                            yield "data: " + json.dumps(token.to_event()) + "\n\n"
                        continue
                    # Live reasoning deltas from the orchestrator's model stream.
                    if isinstance(token, _ReasoningMarker):
                        if _PIPELINE_V2 and _PIPELINE_V2_STREAM:
                            yield "data: " + json.dumps(token.to_event()) + "\n\n"
                        continue
                except Exception:
                    pass

                # Ensure token is string (agent may return structured objects)
                if not isinstance(token, str):

                    if hasattr(token, "token"):
                        token = token.token

                    elif hasattr(token, "text"):
                        token = token.text

                    else:
                        token = str(token)

                # Transition the status line to "Generating response…" the
                # moment the first real token arrives (Phase 1.4). Cheap
                # one-shot guard; ignored by old clients.
                if not _emitted_generating and token:
                    _emitted_generating = True
                    try:
                        yield "data: " + json.dumps({"status": "Generating response…"}) + "\n\n"
                    except Exception:
                        pass

                full_answer += token

                yield "data: " + json.dumps({"t": token}) + "\n\n"

        except Exception as e:

            logger.exception(
                f"Agent execution failed request_id={request_id} {repr(e)[:1500]}"
            )
            telemetry_metrics.inc("errors_total")
            telemetry_metrics.inc("agent_failure")
            if "_model_span" in dir():
                tracer.end_span(_model_span, error=str(e))
            tracer.end_span(_span, error=str(e))

            yield "data: " + json.dumps({"t": "\nError generating response"}) + "\n\n"

        finally:

            # Release semaphore as soon as LLM is done — allow next request in.
            _LLM_SEMAPHORE.release()
            # Deregister stop-flag entry — request is complete or cancelled.
            _gen_deregister(request_id)
            # Clear per-request thread-local context so next request starts clean
            clear_bound_context()
            clear_chat_context()

            # FIX 2: Save conversation turn to Redis memory
            if full_answer:
                try:
                    from memory.redis_memory import RedisMemory as _RM_save
                    _rms = _RM_save()
                    _orch_mem_meta = {"rag_mode": _rag_mode, "repo_filter": repo_filter}
                    _rms.save_message(_chat_id, "user", safe_question, metadata=_orch_mem_meta)
                    _rms.save_message(_chat_id, "assistant", full_answer[:2000], metadata=_orch_mem_meta)
                except Exception:
                    pass

            _meta["latency"] = time.time() - start_time
            # Feed the adaptive semaphore monitor with this request's latency
            _record_latency(_meta["latency"] * 1000)
            logger.info(
                f"[CacheMetric] source=llm  llm_bypassed=false  "
                f"latency={_meta['latency']:.2f}s  user={_user_id}  "
                f"request_id={request_id}"
            )
            _record_bypass_metric("llm", _user_id, repo_filter)

            try:
                # Read snapshotted model ID from the agent (set before eval threads start)
                # to avoid race condition where eval threads overwrite last_model_label.
                # Prefer last_model_id (bare ID) over last_model_label (display string).
                from models.model_router import model_router as _mr
                _meta["model"] = _resolve_model_id(
                    getattr(agent, "last_run_model_label", None)
                    or getattr(_mr, "last_model_id", None)
                    or _mr.last_model_label
                )
                _real_in  = _mr.last_input_tokens
                _real_out = _mr.last_output_tokens
                if _real_in > 0 or _real_out > 0:
                    _meta["in_tok"]  = _real_in
                    _meta["out_tok"] = _real_out
                else:
                    # Ollama / fallback — estimate from word count
                    _meta["in_tok"]  = int(len(rewritten.split()) * 1.3)
                    _meta["out_tok"] = int(len(full_answer.split()) * 1.3)
            except Exception:
                _meta["in_tok"]  = int(len(rewritten.split()) * 1.3)
                _meta["out_tok"] = int(len(full_answer.split()) * 1.3)

            _meta["cost"] = _estimate_cost(_meta["model"], _meta["in_tok"], _meta["out_tok"])

            metrics.record(scope, retrieval_score)
            telemetry_metrics.record_latency(_meta["latency"] * 1000)
            telemetry_metrics.inc("agent_success")
            if "_model_span" in dir():
                tracer.end_span(_model_span)
            tracer.end_span(_span)

            add_trace(
                request_id,
                f"scope={scope} latency={_meta['latency']:.2f}"
            )

            # Fire-and-forget the usage write — this happens in the streaming
            # generator's finally block. A synchronous Postgres write here would
            # delay the final SSE chunk hitting the client by 50-200 ms for no
            # user-visible reason.
            # _estimate_cost() returns 0.0 for local models, so cost_usd is
            # always 0 in the event payload for local routes.
            try:
                # Derive source_channel: CLI (standalone terminal) beats
                # DESKTOP-CHAT (Electron app) beats WEB-CHAT (browser).
                # _is_cli is computed once at the top of ask_ai() from the
                # x-ainxt-client header / cli_mode body flag / client_source state.
                # ClientSourceMiddleware sets request.state.client_source="desktop"
                # when the Electron app injects x-ainxt-surface: desktop.
                _ask_cs = getattr(request.state, "client_source", "platform")
                _ask_channel = (
                    "CLI"          if _is_cli else
                    "DESKTOP-CHAT" if _ask_cs == "desktop" else
                    "WEB-CHAT"
                )
                from core.time_utils import now_ist_iso as _now_ist_iso_ask
                _kafka_produce("ainxt.metrics", {
                    "event":         "llm_cost",
                    "request_id":    request_id,
                    "user_id":       _user_id,
                    "agent_id":      "orchestrator",
                    "endpoint":      "/ask",
                    "source_channel": _ask_channel,
                    "model":         _resolve_model_id(_meta["model"]),
                    "input_tokens":  _meta["in_tok"],
                    "output_tokens": _meta["out_tok"],
                    "total_tokens":  _meta["in_tok"] + _meta["out_tok"],
                    "latency_ms":    _meta["latency"] * 1000,
                    "cost_usd":      _meta["cost"],
                    "product_id":    None,
                    "timestamp":     _now_ist_iso_ask(),
                })
                logger.info(
                    f"[ask] model_usages produced request_id={request_id} user={_user_id} "
                    f"channel={_ask_channel} model={_meta['model']} cost=${_meta['cost']:.6f}"
                )
            except Exception as _ask_mu_err:
                logger.warning(f"[ask] ainxt.metrics produce FAILED request_id={request_id} user={_user_id}: {_ask_mu_err}")

            if full_answer:
                # ── L1: Redis exact cache ────────────────────────────
                try:
                    redis_client.setex(
                        key,
                        CACHE_TTL_SECONDS,
                        json.dumps({
                            "answer": full_answer,
                            "scope": scope,
                            "latency": _meta["latency"],
                        })
                    )
                except Exception:
                    pass

                # ── L2: Semantic answer cache (fire-and-forget) ──────
                # Disabled via _SEMANTIC_CACHE_ENABLED — see flag near cache_key()
                if _SEMANTIC_CACHE_ENABLED:
                    try:
                        from store.semantic_cache import store_semantic_cached_answer
                        store_semantic_cached_answer(
                            question=rewritten,
                            answer=full_answer,
                            repo_filter=repo_filter,
                            user_id=_user_id,
                            confidence=1.0,
                            rag_mode=_rag_mode,
                        )
                    except Exception:
                        pass

                # ── L3: Semantic memory write (fire-and-forget) ──────
                # Capture this Q&A as a learned pattern when it meets
                # quality bars. Mirrors STEP 12c in workers/chat_worker.py
                # so sync and async chat paths populate L3 symmetrically.
                # Gated by SEMANTIC_MEMORY_ENABLED inside the store module.
                try:
                    from store.semantic_cache import (
                        store_semantic_memory,
                        _is_identity_query,
                        SEMANTIC_MEMORY_MIN_CONFIDENCE,
                    )
                    _eligible_l3 = (
                            len(full_answer) >= 80
                            and query_type != "simple"
                            and not _is_identity_query(safe_question)
                            and (float(retrieval_score or 0.0) >= 0.35 or _q_domain == "code")
                            and _user_id and _user_id != "default"
                    )
                    if _eligible_l3:
                        _l3_conf = min(0.90, 0.60 + float(retrieval_score or 0.0) * 0.30)
                        if _l3_conf >= SEMANTIC_MEMORY_MIN_CONFIDENCE:
                            _l3_summary = safe_question.strip().splitlines()[0][:200]
                            _l3_content = {
                                "question": safe_question[:1500],
                                "answer":   full_answer[:3000],
                                "model":    _meta.get("model"),
                                "repo":     repo_filter,
                                "chat_id":  getattr(q, "chat_id", "") or "",
                            }
                            store_semantic_memory(
                                memory_type="chat_qa",
                                summary=_l3_summary,
                                content=_l3_content,
                                source=f"ask:{request_id}",
                                confidence=_l3_conf,
                                user_id=_user_id,
                                scope_type="user",
                                scope_id=_user_id,
                                rag_mode=_rag_mode,
                                source_repo=repo_filter,
                            )
                            _PLATFORM_KEYWORDS_RE = re.compile(
                                r"\b(ainxt|aix.?nxt|ai.?copilot|ainxt|jpos|upi"
                                r"|sdlc|pipeline|workflow|orchestrat"
                                r"|agent|skill|mcp|governance|knowledge.base|codebase"
                                r"|circuit.breaker|model.router|compliance.engine"
                                r"|embed|retriev|vector|pgvector"
                                r"|thread|inbox|marketplace|budget)\b",
                                re.IGNORECASE,
                            )
                            if _user_dept and _PLATFORM_KEYWORDS_RE.search(safe_question):
                                store_semantic_memory(
                                    memory_type="chat_qa",
                                    summary=_l3_summary,
                                    content=_l3_content,
                                    source=f"ask:{request_id}",
                                    confidence=_l3_conf,
                                    user_id=_user_id,
                                    scope_type="team",
                                    scope_id=_user_dept,
                                    rag_mode=_rag_mode,
                                    source_repo=repo_filter,
                                )
                except Exception as _l3_err:
                    logger.debug(f"gateway: L3 memory write failed: {_l3_err}")

                # ── Eval scoring (fire-and-forget) ───────────────────
                try:
                    _eval_q      = rewritten
                    _eval_ans    = full_answer
                    _eval_chunks = list(agent.last_context)  # snapshot before next request clears it
                    _eval_model  = _meta.get("model", "")
                    _eval_lat    = _meta.get("latency", 0.0)
                    _eval_uid    = _user_id
                    _eval_rid    = request_id
                    def _run_eval_score():
                        try:
                            _score = _compute_eval_score(_eval_q, _eval_ans, _eval_chunks)
                            # PIPELINE_V2_GROUNDING: the verifier adds these keys.
                            # eval_scores has no column for them (no schema change),
                            # so surface them as telemetry to make the flag observable.
                            if "grounding_confidence" in _score:
                                _otel.record_event(
                                    "grounding.verified",
                                    grounding_confidence=_score.get("grounding_confidence"),
                                    unsupported_claims=_score.get("unsupported_claims"),
                                    contradicted_claims=_score.get("contradicted_claims"),
                                    hedge=bool(_score.get("grounding_hedge")),
                                )
                            from db.database import engine as _eng
                            from sqlalchemy import text as _sqlt
                            import hashlib as _hl
                            _qhash = _hl.sha256(_eval_q[:500].encode()).hexdigest()[:16]
                            with _eng.connect() as _c:
                                _c.execute(_sqlt("""
                                    INSERT INTO eval_scores
                                        (request_id, user_id, question_hash,
                                         grounding, completeness, chunk_count,
                                         has_context, model, latency_ms)
                                    VALUES (:rid, :uid, :qh,
                                            :gr, :co, :cc,
                                            :hc, :mo, :lat)
                                """), {
                                    "rid": _eval_rid, "uid": _eval_uid, "qh": _qhash,
                                    "gr": _score["grounding"], "co": _score["completeness"],
                                    "cc": _score["chunk_count"], "hc": _score["has_context"],
                                    "mo": _eval_model, "lat": _eval_lat * 1000,
                                })
                                _c.commit()
                        except Exception:
                            pass
                    _eval_ctx = contextvars.copy_context()
                    threading.Thread(target=lambda: _eval_ctx.run(_run_eval_score), daemon=True, name="eval-score").start()
                except Exception:
                    pass

        # Trailing __meta__ JSON line — parsed by Chat.jsx to display
        # model, token count, cost, latency, and budget info in the message footer.
        # Sent after the finally block so all fields are fully populated.
        import json as _json

        # Fetch budget + usage for this user and record token consumption.
        # _estimate_cost() returns 0.0 for local models, so cost_usd is always
        # 0 for local routes — tokens are still logged for auditing purposes.
        _budget_info = {}
        # Used below to suppress budget UI chips for local models (free, no allocation).
        _orch_is_local = bool(
            _local_model
            or (_model_hint or "").lower() in ("local", "simple")
            or "local" in (_meta.get("model") or "").lower()
        )
        try:
            from store.budget_store import get_budget, get_usage_today, increment_usage
            _tot_tok = _meta["in_tok"] + _meta["out_tok"]
            # requests=0 because BudgetMiddleware already counted this request
            increment_usage(_user_id, tokens=_tot_tok, requests=0, cost_usd=_meta.get("cost", 0.0))
            try:
                from core.time_utils import now_ist_iso as _now_ist_iso_ask_b
                # Re-use the same channel derived above (CLI / DESKTOP-CHAT / WEB-CHAT).
                # _ask_channel is set in the first Kafka produce block above;
                # fall back to WEB-CHAT if somehow that block was skipped.
                _ask_channel_b = locals().get("_ask_channel", "WEB-CHAT")
                _kafka_produce("ainxt.metrics", {
                    "event":         "llm_cost",
                    "request_id":    request_id,
                    "user_id":       _user_id,
                    "source_channel": _ask_channel_b,
                    "model":         _resolve_model_id(_meta.get("model", "")),
                    "input_tokens":  _meta.get("in_tok", 0),
                    "output_tokens": _meta.get("out_tok", 0),
                    "cost_usd":      _meta.get("cost", 0.0),
                    "product_id":    str(q.project_id) if getattr(q, "project_id", None) else None,
                    "timestamp":     _now_ist_iso_ask_b(),
                })
                logger.info(
                    f"[ask] budget-block model_usages produced request_id={request_id} "
                    f"user={_user_id} channel={_ask_channel_b}"
                )
            except Exception as _ask_mu_err_b:
                logger.warning(
                    f"[ask] budget-block ainxt.metrics produce FAILED request_id={request_id} "
                    f"user={_user_id}: {_ask_mu_err_b}"
                )
            _usage = get_usage_today(_user_id)
            _budget = get_budget(_user_id)
            if not _orch_is_local:
                _budget_info = {
                    "tokens_today":   _usage.get("tokens_used", 0),
                    "requests_today": _usage.get("requests_made", 0),
                    "cost_today":     _usage.get("cost_usd_spent", 0.0),
                }
                if _budget:
                    _budget_info["max_tokens_total"]   = _budget.get("max_tokens_total", 0)
                    _budget_info["max_requests_total"] = _budget.get("max_requests_total", 0)
                    _budget_info["max_cost_total"]     = _budget.get("max_cost_usd_total", 0.0)
        except Exception:
            pass

        # ── Project USD budget increment (fire-and-forget via Kafka) ──
        # Consumer on App03 applies the delta to project_records.budget_used_usd
        # and fires inbox alerts at 80%/100% thresholds — zero DB round-trip here.
        if q.project_id and _meta["cost"] > 0:
            try:
                import datetime as _dt_bud
                _kafka_produce(_TOPIC_BUDGET_EVENTS, {
                    "event":      "project_budget_incremented",
                    "project_id": str(q.project_id),
                    "cost_usd":   _meta["cost"],
                    "user_id":    _user_id,
                    "ts":         _dt_bud.datetime.utcnow().isoformat(),
                }, key=str(q.project_id))
            except Exception:
                pass

        # ── Save chat messages (fire-and-forget via Kafka, with DB fallback) ──
        # Primary path: Kafka → consumer on App03 → Postgres. Replaces the
        # previous daemon thread for production scale.
        #
        # Fallback path: when `_kafka_produce` returns False (broker unreachable
        # AND the event went to the Redis KV fallback list only), spawn the
        # thread-based DB writer so the messages still land in Postgres even
        # if no Kafka consumer is online to drain the queue. Without this,
        # dev environments (no Kafka) silently lose chat history on every
        # /ask call — the chat row exists but /chats/{id}/messages returns []
        # after refresh.
        #
        # Dedup safety: if Kafka comes back later and a consumer drains the
        # fallback list, the handler's `if not chat: insert` guard makes the
        # Chat upsert idempotent. ChatMessage rows would technically duplicate
        # then, but the dev fallback only matters when there's no consumer
        # to drain — once a consumer exists, the fallback list gets drained
        # in normal Kafka order anyway, this branch having already persisted.
        # In dev (the only place this branch runs), the consumer is absent,
        # so no duplicates can occur.
        try:
            import datetime as _dt_ch
            if not q.ephemeral:
                # ── Sanitized copy of the user's RAW prompt, for storage only ──
                if _bypass_safety_filters:
                    stored_question = clean_question
                else:
                    _stored_chk = _ce_ask.validate_input(clean_question, keep_types=_ask_keep)
                    if q.cli_mode:
                        stored_question = _stored_chk.get("redacted_text") or clean_question
                    else:
                        stored_question = _stored_chk.get("redacted_text") or mask_pii(clean_question)
                _kafka_ok = _kafka_produce("ainxt.chat_history", {
                    "chat_id":            _chat_id,
                    "user_id":            _user_id,
                    "request_id":         request_id,
                    # Persist the sanitized RAW user prompt (stored_question)
                    "question":           stored_question,
                    "answer":             full_answer,
                    "model":              _meta.get("model", ""),
                    "in_tok":             _meta.get("in_tok", 0),
                    "out_tok":            _meta.get("out_tok", 0),
                    "cost":               _meta.get("cost", 0.0),
                    "latency":            _meta.get("latency"),
                    "language":           _detected_lang or "unknown",
                    "attachment_ids":     list(q.attachment_ids or []),
                    "project_id":         q.project_id or "",
                    "agent_id":           q.agent_id or "",
                    "title_hint":         stored_question[:80] if not q.chat_id else None,
                    # Channel isolation: office (Buddy) turns are tagged
                    # client_source="office"
                    "client_source":      getattr(request.state, "client_source", "platform"),
                    "compliance_blocked": False,
                    "block_reason":       "",
                    "rag_mode":           _rag_mode,
                    "repo_filter":        repo_filter,
                    "ts":                 _dt_ch.datetime.utcnow().isoformat(),
                }, key=_chat_id)
                if not _kafka_ok:
                    # Broker unavailable → event sits in Redis fallback list with
                    # no consumer to drain it. Persist directly to Postgres so the
                    # chat history survives a refresh.
                    import threading as _ct_hist
                    _kafka_hist_ctx = contextvars.copy_context()
                    _kafka_hist_kw = {
                        "chat_id":        _chat_id,
                        "user_id":        _user_id,
                        # Sanitized RAW prompt (no system-prompt framing)
                        "question":       stored_question,
                        "answer":         full_answer,
                        "model":          _meta.get("model", ""),
                        "in_tok":         _meta.get("in_tok", 0),
                        "out_tok":        _meta.get("out_tok", 0),
                        "cost":           _meta.get("cost", 0.0),
                        "language":       _detected_lang or "unknown",
                        "attachment_ids": list(q.attachment_ids or []),
                        "project_id":     q.project_id or "",
                        "agent_id":       q.agent_id or "",
                        "latency":        _meta.get("latency"),
                        "title_hint":     stored_question[:80] if not q.chat_id else None,
                        "client_source":  getattr(request.state, "client_source", "platform"),
                        "coverage_trace": (_user_ctx or {}).get("_coverage_trace_out"),
                        "rag_mode":       _rag_mode,
                        "repo_filter":    repo_filter,
                    }
                    _ct_hist.Thread(
                        target=lambda: _kafka_hist_ctx.run(_save_chat_messages, **_kafka_hist_kw),
                        daemon=True,
                    ).start()
        except Exception:
            pass

        # ── Request audit log (fire-and-forget) ──────────────────────────────
        # Always written for all models. cost_usd is 0.0 for local models
        # because _estimate_cost() returns 0.0 for them.
        _cs = getattr(request.state, "client_source", "platform")
        try:
            _write_request_audit(
                request_id=request_id,
                user_id=_user_id or "anonymous",
                email=_user_email or "",
                department=getattr(_user_obj, "department", "") if "_user_obj" in dir() else "",
                client_source=_cs,
                endpoint="/ask",
                question=safe_question,
                model_used=_meta.get("model", ""),
                tokens_in=_meta.get("in_tok", 0),
                tokens_out=_meta.get("out_tok", 0),
                cost_usd=_meta.get("cost", 0.0),
                latency_ms=int(_meta.get("latency", 0) * 1000),
                cache_hit=_meta.get("source", "none") if _meta.get("source") != "llm" else "none",
                compliance_blocked=False,
            )
        except Exception:
            pass

        # Phase 3 transparency — surface coverage tier decisions (orchestrator path).
        _cov_out_orch = None
        try:
            _cov_out_orch = (_user_ctx or {}).get("_coverage_trace_out")
        except Exception:
            _cov_out_orch = None
        _orch_meta = {
            "chat_id": _chat_id,
            "tokens":  _meta["out_tok"] + _meta["in_tok"],
            "in_tok":  _meta["in_tok"],
            "out_tok": _meta["out_tok"],
            "cost":    round(_meta["cost"], 6),
            "model":   _meta["model"],
            "latency": round(_meta["latency"], 2),
            "language": _detected_lang,
            "source":  "llm",
            "llm_used": True,
            "client_source": _cs,
            **_budget_info,
        }
        if _cov_out_orch:
            _orch_meta["coverage_trace"] = _cov_out_orch
        yield "data: " + _json.dumps({"__meta__": _orch_meta}) + "\n\n"
        _emit_coach(_orch_meta)  # Coach — mirrors _general_stream(); covers web+project/KB/orchestrator turns


    # Estimate token counts for response headers
    _in_tok_est  = int(len(safe_question.split()) * 1.3)

    if _PIPELINE_V2 and _rc is not None:
        _rc.dispatch = _DispatchDecision(lane=_Lane.ORCHESTRATOR, reason="fallthrough")
        _otel.record_event("dispatch", lane="orchestrator")
    return StreamingResponse(

        response_stream(),

        media_type="text/event-stream",

        headers={
            "X-Request-ID":              request_id,
            "X-Model-Hint":              str(_model_hint or "auto"),
            "Cache-Control":             "no-cache",
            "X-Accel-Buffering":         "no",
            "Access-Control-Expose-Headers": "X-Request-ID,X-Token-Usage,X-Cost-USD,X-Model-Hint",
        }

    )


# ============================================================
# OPENAI-COMPATIBLE ENDPOINT  (Kilo Code / Continue / VS Code extensions)
# POST /v1/chat/completions
# Speaks standard OpenAI SSE format so any IDE extension works out-of-the-box.
# Ephemeral — no chat_messages persistence; model usage + budget still tracked.
# ============================================================

class _OAIMessage(BaseModel):
    role: str
    # OpenAI spec allows content to be either a plain string OR an array of
    # content-part objects: [{"type":"text","text":"..."},{"type":"image_url",...}]
    # Kilo Code always sends the array form, so we accept both.
    content: Union[str, List[Any], None] = None
    # Tool-calling fields (agent mode — assistant messages may carry these)
    tool_calls:    Optional[List[Any]] = None
    tool_call_id:  Optional[str]       = None
    name:          Optional[str]       = None

    def text(self) -> str:
        """Return the plain-text portion of the message, regardless of content format."""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            parts = []
            for part in self.content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text", ""))
            return "\n".join(parts)
        return ""

class _OAIChatRequest(BaseModel):
    model: str = _OPENAI_CODING
    messages: List[_OAIMessage]
    stream: bool = True
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    # Tool / function-calling fields passed through by agent IDE extensions
    tools:         Optional[List[Any]] = None
    tool_choice:   Optional[Any]       = None
    # Structured output — when set, inject a JSON schema instruction into the
    # system message so the LLM responds in the requested format.
    # OpenAI sends {"type":"json_schema","json_schema":{...}} or {"type":"json_object"}.
    # Presenton (llmai client) sends JSONSchemaResponse objects serialised this way.
    response_format: Optional[Any] = None
    # Optional session identifier for Coach grouping. Most IDE clients don't
    # send this, in which case IDE turns are grouped as "Unthreaded prompts".
    session_id: Optional[str] = None

# Map common OpenAI / Anthropic model names to AiNxt model hints.
# Static keys cover well-known IDs sent by IDE extensions.
# Dynamic entries below ensure env-var model overrides are also matched.
_OAI_MODEL_MAP = {
    "gpt-4o":              "gpt",
    "gpt-4-turbo":         "gpt",
    "gpt-4":               "gpt",
    "gpt-3.5-turbo":       "gpt",
    "gpt-5.4":             "gpt",
    # Advertised by GET /v1/models but previously unmapped, so a request for any
    # of them was auto-routed by prompt complexity instead of honoured. The
    # router already has these tiers (TIER_MINI / TIER_TERA / TIER_LUNA).
    "gpt-5-mini":          "mini",
    "gpt-5.6-terra":       "tera",
    "gpt-5.6-luna":        "luna",
    "gpt-5.5":             "deep",
    "gpt-5.2":             "gpt",   # retired — kept for backward compat with stored hints
    "claude-sonnet-5":     "sonnet-5",
    "claude-sonnet-4-6":   "claude",
    # Haiku BEFORE the generic "claude" entry below: "claude-haiku-4-5" matched
    # "claude" and was served by Claude Sonnet, so a request for the cheapest
    # Claude model silently billed for the most capable one.
    "claude-haiku-4-5":    "haiku",
    "claude-haiku":        "haiku",
    # Opus 4-7 and 4-8/5 must come before the generic "claude-opus" prefix so that
    # startswith() matches the specific entry first (dict is ordered in Python 3.7+).
    "claude-opus-4-7":     "solution",
    "claude-opus-4-8":     "opus-4-8",
    "claude-opus-5":       "opus-5",
    "claude-sonnet":       "claude",
    "claude-opus":         "solution", # generic "claude-opus" → latest Opus (4.7)
    "claude":              "claude",
    # Specific Gemini IDs first so prefix matching in _oai_model_hint picks them
    # over the generic "gemini" entry. Hint = the literal model ID; model_router
    # resolves it via _GEMINI_SPECIFIC_HINTS to the registry constant.
    "gemini-3.5-flash":       "gemini-3.5-flash",
    "gemini-3.1-flash-lite":  "gemini-3.1-flash-lite",
    "gemini-3.1-flash-image": "gemini-3.1-flash-image",
    "gemini-3.1-flash":       "gemini-3.1-flash-lite",
    "gemini-2.5-flash":    "gemini",
    "gemini-pro":          "gemini",
    "gemini-flash":        "gemini",
    "gemini":              "gemini",
}
# Ensure env-var-configured model IDs are also covered
_OAI_MODEL_MAP[_OPENAI_CODING]  = "gpt"
_OAI_MODEL_MAP[_OPENAI_SIMPLE]  = "mini"   # direct GPT-5-mini, not medium tier
_OAI_MODEL_MAP[_OPENAI_LATEST]  = "deep"
_OAI_MODEL_MAP[_CLAUDE_PRIMARY] = "claude"
_OAI_MODEL_MAP[_CLAUDE_HAIKU]   = "haiku"
_OAI_MODEL_MAP[_GEMINI_VISION]      = "gemini"
_OAI_MODEL_MAP[_GEMINI_TEXT]        = _GEMINI_TEXT          # explicit model ID → identical hint
_OAI_MODEL_MAP[_GEMINI_CODING_LITE] = _GEMINI_CODING_LITE
_OAI_MODEL_MAP[_GEMINI_IMAGE]       = _GEMINI_IMAGE
_OAI_MODEL_MAP[_CLAUDE_OPUS]    = "solution"
_OAI_MODEL_MAP[_CLAUDE_OPUS_48] = "opus-4-8"
_OAI_MODEL_MAP[_CLAUDE_OPUS_5]  = "opus-5"
_OAI_MODEL_MAP[_CLAUDE_SONNET_5] = "sonnet-5"

# Hints that route to the Anthropic tools-stream (_tools_claude_stream).
# NOTE: _tools_claude_stream flattens content to text and CANNOT carry images.
# Complete, drift-proof set (includes "opus-4-8" and "opus-5"). Used ONLY by
# the browser-agent passthrough lane.
_CLAUDE_TOOL_HINTS = frozenset({"claude", "solution", "haiku", "opus-4-8", "opus-5", "sonnet-5"})

# Deep research models — require `tools` to be supplied by the caller
_DEEP_RESEARCH_MODELS: set[str] = {_DR_MINI, _DR_FULL, "o4-mini-deep-research", "o3-deep-research"}

def _oai_model_hint(model_name: str) -> Optional[str]:
    """Translate an OpenAI-style model name to a AiNxt routing hint."""
    name = (model_name or "").lower().strip()
    if name.startswith("local:"):
        return "local"   # route local:xxx to in-house LLM tier
    # Bare "local" is the id GET /v1/models publishes for the in-house model
    # ({"id": "local", "provider": "inhouse", "label": "Local (In-house)"}).
    # It was the one id this function did not map, so a request for it fell
    # through to `return None` and the router auto-routed by prompt complexity —
    # sending a turn the caller asked to keep in-house to a cloud provider.
    if name in ("local", "inhouse", "in-house"):
        return "local"
    for prefix, hint in _OAI_MODEL_MAP.items():
        if name.startswith(prefix):
            return hint
    # Unrecognised name: the router picks by complexity. That is intended for
    # "auto"/"default"/empty, but for anything else the caller asked for a
    # specific model and is silently getting a different one, so say so.
    if name and name not in ("auto", "default"):
        logger.warning(
            "Unrecognised model %r on the OpenAI-compatible endpoint — no routing "
            "hint matched, so the request will be auto-routed by prompt complexity "
            "and may run on a different provider than requested. Known ids: %s",
            model_name, ", ".join(sorted(_OAI_MODEL_MAP)) + ", local",
        )
    return None  # let model_router auto-route


def _audit_model_hint_coverage() -> list:
    """Every model id this server advertises must map to a routing hint.

    An id with no mapping falls through _oai_model_hint() to `return None`, which
    means "auto-route by prompt complexity" — so a caller asking for that model
    silently gets a different one. Four advertised ids were in that state
    (gpt-5-mini, gpt-5.6-terra, gpt-5.6-luna) or mapped to the wrong tier
    (claude-haiku-4-5 -> "claude", i.e. served by Sonnet). This runs at startup so
    adding a model to the catalogue without a hint is caught immediately instead
    of becoming a silent, billable substitution.

    Returns the list of unmapped ids (empty when healthy).
    """
    try:
        advertised = [m["id"] for m in list_oai_models().get("data", [])]
    except Exception as exc:            # never block boot on a self-check
        logger.warning("Model hint coverage audit skipped: %s", exc)
        return []
    unmapped = [mid for mid in advertised if _oai_model_hint(mid) is None]
    if unmapped:
        logger.error(
            "MODEL ROUTING GAP: %d advertised model id(s) have no routing hint and "
            "will be auto-routed by prompt complexity instead of honoured: %s. "
            "Add them to _OAI_MODEL_MAP.",
            len(unmapped), ", ".join(unmapped),
        )
    else:
        logger.info(
            "Model hint coverage: all %d advertised model id(s) map to a routing hint",
            len(advertised),
        )
    return unmapped


def _messages_have_image(msgs) -> bool:
    """True if any message carries an image_url content part (multimodal turn)."""
    for m in msgs:
        if isinstance(m.content, list):
            if any(isinstance(p, dict) and p.get("type") == "image_url" for p in m.content):
                return True
    return False


@_v1.get("/v1/models", tags=["ai"])
@_v1.get("/models", tags=["ai"])
def list_oai_models():
    """Return available models in OpenAI format so IDE extensions can populate their model picker.
    Includes both cloud and local models so Kilo Code / Continue.dev show the full list.
    owned_by is driven by APP_OWNER (default "ainxt"; set it to your own
    organisation slug to attribute the models to you).
    """
    from core.config import APP_OWNER as _APP_OWNER
    _models = [
        {"id": _OPENAI_LATEST,  "object": "model", "created": 1700000000, "owned_by": _APP_OWNER, "apiBackend": "messages"},
        {"id": _OPENAI_CODING,  "object": "model", "created": 1700000000, "owned_by": _APP_OWNER, "apiBackend": "messages"},
        {"id": _OPENAI_SIMPLE,  "object": "model", "created": 1700000000, "owned_by": _APP_OWNER, "apiBackend": "messages"},
        {"id": _CLAUDE_PRIMARY, "object": "model", "created": 1700000000, "owned_by": _APP_OWNER, "apiBackend": "messages"},
        {"id": _CLAUDE_HAIKU,   "object": "model", "created": 1700000000, "owned_by": _APP_OWNER, "apiBackend": "messages"},
        # Gemini 3.x split — text/coding, lightweight coding, image generation
        {"id": _GEMINI_TEXT,        "object": "model", "created": 1700000000, "owned_by": _APP_OWNER, "apiBackend": "messages"},
        {"id": _GEMINI_CODING_LITE, "object": "model", "created": 1700000000, "owned_by": _APP_OWNER, "apiBackend": "messages"},
        {"id": _GEMINI_IMAGE,       "object": "model", "created": 1700000000, "owned_by": _APP_OWNER, "apiBackend": "messages"},
        # Note: deep research models (o4-mini-deep-research, o3-deep-research) are intentionally
        # excluded — they are only accessible via POST /v1/responses, not /chat/completions.
        *([
            {"id": _CLAUDE_OPUS,    "object": "model", "created": 1700000000, "owned_by": _APP_OWNER, "apiBackend": "messages"},
            # Opus 4.6 is deliberately NOT advertised: core/model_registry.py marks
            # CLAUDE_OPUS_46_MODEL "RETIRED — always blocked", so listing it offered
            # callers a model that can never serve a request. The name it referenced
            # (_CLAUDE_OPUS_46) was also never defined anywhere in this module, so
            # GET /v1/models raised NameError outright whenever ENABLE_OPUS was set.
        ] if _ENABLE_OPUS else []),
        # Claude Opus 4.8 — CLI / IDE-plugin only. Intentionally NOT added to
        # /v1/all-models (web Chat picker). Gated by ENABLE_CLI_OPUS_48 so ops
        # can disable without code changes.
        *([
            {"id": _CLAUDE_OPUS_48, "object": "model", "created": 1700000000, "owned_by": _APP_OWNER, "apiBackend": "messages"},
        ] if (_ENABLE_OPUS and _ENABLE_CLI_OPUS_48) else []),
        # Claude Opus 5 — CLI / IDE-plugin only. Opt-in via ENABLE_CLI_OPUS_5.
        *([
            {"id": _CLAUDE_OPUS_5, "object": "model", "created": 1700000000, "owned_by": _APP_OWNER, "apiBackend": "messages"},
        ] if (_ENABLE_OPUS and _ENABLE_CLI_OPUS_5) else []),
        # Claude Sonnet 5 — available on ALL channels (Chat picker, IDE / OpenAI-
        # compat picker, CLI, SDLC). Only gated by the global ENABLE_SONNET_5
        # kill-switch. Not restricted by ENABLE_OPUS / channel checks by design.
        *([
            {"id": _CLAUDE_SONNET_5, "object": "model", "created": 1700000000, "owned_by": _APP_OWNER, "apiBackend": "messages"},
        ] if _ENABLE_SONNET_5 else []),
        # GPT-5.6 Tera — high-capacity variant, Chat + CLI. Gated by ENABLE_GPT56_TERA.
        *([
            {"id": _OPENAI_TERA, "object": "model", "created": 1700000000, "owned_by": _APP_OWNER, "apiBackend": "messages"},
        ] if _ENABLE_GPT56_TERA else []),
        # GPT-5.6 Luna — efficient variant, Chat + CLI. Gated by ENABLE_GPT56_LUNA.
        *([
            {"id": _OPENAI_LUNA, "object": "model", "created": 1700000000, "owned_by": _APP_OWNER, "apiBackend": "messages"},
        ] if _ENABLE_GPT56_LUNA else []),
    ]
    # Append in-house hosted models from the local LLM proxy
    try:
        from gateway_local_llm import get_local_gateway as _get_local_gw
        for _m in _get_local_gw().list_models():
            _models.append({
                "id":        f"local:{_m}",
                "object":    "model",
                "created":   1700000000,
                "owned_by":  "local",
                "apiBackend": "messages",
            })
    except Exception:
        pass

    # Attach the hard per-model output-token ceiling when one exists (e.g.
    # Claude Haiku 4.5 caps at 64K output tokens despite a 256K context
    # window). Without this, IDE/CLI clients that clamp `max_tokens` only
    # against context_window will send an oversized value that the provider
    # hard-rejects with a 400 on every request. See
    # core.model_registry.MODEL_MAX_OUTPUT_TOKENS for the source of truth.
    try:
        from core.model_registry import max_output_tokens_for as _max_out_for_oai
        for _m in _models:
            _ceiling = _max_out_for_oai(_m["id"])
            if _ceiling:
                _m["max_completion_tokens"] = _ceiling
    except Exception:
        pass

    return {"object": "list", "data": _models}


@_v1.post("/v1/chat/completions", tags=["ai"])
@_v1.post("/chat/completions", tags=["ai"])
def openai_chat_completions(
    req: _OAIChatRequest,
    request: Request,
    authorization: Optional[str] = _Header(default=None),
):
    """OpenAI-compatible chat completions endpoint.

    Translates the OpenAI messages array into a single question, routes it
    through the full AiNxt OrchestratorAgent, and streams the response back
    as standard OpenAI SSE chunks (``data: {...}\\n\\n`` / ``data: [DONE]``).

    Compatible with: Kilo Code, Continue, Cursor, any OpenAI-SDK client.
    Base URL to configure in the extension: ``http://localhost:8000``
    """
    # ── Kill-switch: direct/raw access disabled → force managed endpoints ──
    # Default OFF (ENABLE_RAW_OPENAI_API). Blocks curl / SDK / IDE / CLI direct
    # callers on this raw generation route; those callers must use a managed
    # endpoint (/ainxt/v1/api/{slug}/v1/chat/completions).
    #
    # EXEMPTION — browser-agent lane (Chrome extension):
    #   The extension identifies itself via X-AiNxt-Client: browser-agent
    #   (parsed into request.state.client_source by ClientSourceMiddleware).
    #   The entire _passthrough branch below (message builder, RAG-skip,
    #   Claude pinning, browser ledger key, [BROWSER] logs) is designed
    #   specifically for it. The 401 identity gate directly below still runs,
    #   so this lane remains authenticated AiNxt users only — never anonymous.
    #
    # Managed endpoints and GET /v1/models are unaffected by this switch.
    from middleware.client_source_middleware import CLIENT_BROWSER_AGENT
    _is_browser_agent = getattr(request.state, "client_source", "") == CLIENT_BROWSER_AGENT
    logger.info(f"Chat-completion request _is_browser_agent={_is_browser_agent}")
    if not _ENABLE_RAW_OPENAI_API and not _is_browser_agent:
        from fastapi.responses import JSONResponse as _JR_oai_disabled
        return _JR_oai_disabled(
            status_code=403,
            content={"error": {
                "message": (
                    "Direct API access to this endpoint is disabled. "
                    "To use an OpenAI-compatible endpoint, request a managed endpoint "
                    "from your AiNxt admin."
                ),
                "type": "invalid_request_error",
                "code": "direct_access_disabled",
            }},
        )

    # Prefer client-supplied x-client-request-id for end-to-end tracing
    request_id = (request.headers.get("x-client-request-id") or "").strip() or str(uuid.uuid4())
    set_request_id(request_id)
    set_correlation_id(request_id)  # unconditional: avoid stale value on reused thread
    start_time = time.time()

    # Stable Coach thread_id for IDE sessions. Prefer the client's session_id;
    # if absent, compute a user-scoped time bucket after identity resolution.
    _ide_thread_id: Optional[str] = req.session_id or None

    # ── Resolve identity: JWT → API key → 401 (no anonymous access) ──
    _user_id = None
    if authorization and authorization.lower().startswith("bearer "):
        _token = authorization[7:].strip()
        try:
            from auth.jwt_handler import decode_token as _decode
            _payload = _decode(_token)
            if _payload:
                # JWT no longer contains "email" (DAST fix — PII removed from JWT)
                _user_id = _payload.get("sub")
        except Exception:
            pass
        if not _user_id:
            try:
                from auth.api_key_auth import is_api_key as _is_ak, resolve_api_key as _res_ak
                if _is_ak(_token):
                    _kp = _res_ak(_token)
                    if _kp:
                        _user_id = _kp["sub"]
            except Exception:
                pass
    if not _user_id:
        from fastapi.responses import JSONResponse as _JR_oai
        return _JR_oai(
            status_code=401,
            content={"error": {"message": "Valid JWT or platform API key required.", "type": "invalid_request_error", "code": "unauthorized"}},
        )
    # ── Browser-agent passthrough lane — resolved early (F4) ──────
    # request.state.client_source is set by client_source_middleware BEFORE the
    # handler body, so _passthrough can be derived up front to label logs/traces
    # distinctly. IDE/CLI (_passthrough=False) keeps the exact tags used today.
    #
    # INVARIANT: this is reached only AFTER the 401 identity guard above, so
    # _user_id is guaranteed non-null here. The passthrough ledger key therefore
    # always has a real user salt (the '' fallback in _pt_ledger_key is defensive
    # and unreachable on this path — there is no anonymous access to this route).
    _passthrough = getattr(request.state, "client_source", "") == "browser-agent"
    _log_tag  = "[BROWSER]" if _passthrough else "[IDE]"
    _span_tag = "gateway.browser" if _passthrough else "gateway.ide"
    _cid_pfx  = "browser" if _passthrough else "ide"

    if not _ide_thread_id:
        _ide_thread_id = f"{_cid_pfx}:{_user_id}:{int(time.time() // 1800)}"
    set_chat_context(_user_id, _ide_thread_id)
    set_span_id(_span_tag)

    # ── Budget gate — cloud API models only; in-house models always pass through ──
    # In-house = any explicitly named model that doesn't start with a known cloud prefix.
    # Auto/empty model hints are treated as potentially cloud-bound — check budget.
    _CLOUD_IDE_PFX = ("gpt-", "claude-", "gemini-", "openai/", "anthropic/", "google/", "azure/")
    _ide_model_hint = (req.model or "").lower().strip()
    _ide_is_inhouse = (
        bool(_ide_model_hint)
        and _ide_model_hint not in ("auto", "default")
        and not any(_ide_model_hint.startswith(p) for p in _CLOUD_IDE_PFX)
    )
    # Raw last user message for Coach (used if the request is blocked before
    # the cleaned last_user is computed below).
    _ide_raw_last_user = ""
    try:
        _ide_raw_last_user = next(
            (str(m.text() or "").strip() for m in reversed(req.messages) if m.role == "user"), ""
        )
    except Exception:
        pass
    if not _ide_is_inhouse:
        try:
            from store.budget_store import check_budget as _chk_oai
            _oai_budget = _chk_oai(_user_id)
            if _oai_budget.get("allowed") is not True:
                _deny_reason = _oai_budget.get("reason", "Budget allocation exhausted")
                logger.warning(f"[IDE] BUDGET BLOCKED (cloud) user={_user_id} reason={_deny_reason}")
                try:
                    from core.coach_events import emit_coach_event, channel_from_client_source
                    emit_coach_event(
                        user_id=_user_id or "anonymous",
                        channel=channel_from_client_source(getattr(request.state, "client_source", "mcp")),
                        model="budget_blocked",
                        prompt=_ide_raw_last_user,
                        tokens_in=0,
                        tokens_out=0,
                        cost_usd=0.0,
                        latency_ms=0,
                        request_id=request_id,
                        thread_id=_ide_thread_id,
                        department=None,
                    )
                except Exception:
                    pass
                try:
                    from store.inbox_store import publish_inbox_item as _pub_inbox
                    _pub_inbox(
                        user_id=_user_id,
                        type="budget_alert",
                        title="Budget limit reached",
                        body=_deny_reason + " — request an increase from your admin.",
                        priority="High",
                    )
                except Exception:
                    pass
                from fastapi.responses import JSONResponse as _JR_oai_bgt
                return _JR_oai_bgt(
                    status_code=429,
                    content={
                        "error": {
                            "message": _deny_reason,
                            "type":    "insufficient_quota",
                            "code":    "BUDGET_EXCEEDED",
                        }
                    },
                )
        except Exception as _oai_bgt_err:
            logger.error(f"[IDE] budget gate FAILED (fail-open): user={_user_id} err={_oai_bgt_err}")
    else:
        logger.debug(f"[IDE] budget gate SKIPPED (in-house model): user={_user_id} model={req.model!r}")

    # ── Build question from OpenAI messages array ─────────────────
    # System messages → prepended context
    # Conversation history (all but last user) → few-shot context
    # Last user message → the actual task text

    import re as _re
    _KILO_FINGERPRINT = ("read_file", "write_to_file", "search_files", "execute_command")
    _KILO_MINIMAL_SYSTEM = (
        "You are a coding assistant running inside the user's IDE via KiloCode. "
        "Use available tools only when the user's request explicitly requires it. "
        "Call read_file only for files directly relevant to the task — "
        "do not explore the codebase proactively."
    )

    def _clean_ide_message(text: str) -> str:
        """Strip and compress IDE-injected boilerplate that inflates prompt size.

        Kilo Code / Continue inject large <environment_details> blocks containing
        full workspace file listings, repo maps, and file contents.  These are
        useless to our LLM (we have our own codebase index) and can push a single
        prompt past 200K tokens on 10-lakh-line codebases — costing $30+ per call.

        Strategy:
        - Remove <environment_details> / <repo_map> entirely (we have pgvector index)
        - Remove <repo_map> entirely
        - Unwrap <task>/<attempt_completion>/<result>/<feedback> — keep inner text
        - Compress <file_content> blocks > 4K chars using head+tail truncation
        """
        # Remove workspace boilerplate entirely (our codebase index replaces this)
        text = _re.sub(r"<environment_details>.*?</environment_details>", "", text,
                       flags=_re.DOTALL)
        # Unwrap <task>...</task> — keep the content, drop the tags
        text = _re.sub(r"<task>\s*", "", text)
        text = _re.sub(r"\s*</task>", "", text)
        text = _re.sub(r"<repo_map>.*?</repo_map>", "", text, flags=_re.DOTALL)
        # Remove large file listing blocks (Cline/Kilo format)
        text = _re.sub(r"<file_list>.*?</file_list>", "", text, flags=_re.DOTALL)
        # Unwrap task/completion wrapper tags — keep inner content
        text = _re.sub(r"</?(?:attempt_completion|result|feedback)>", "", text)
        # Compress large <file_content>...</file_content> blocks inline
        # (Kilo Code injects these when it reads files during the agentic loop)
        def _compress_file_block(m: _re.Match) -> str:
            inner = m.group(1)
            if len(inner) <= 4000:
                return f"<file_content>{inner}</file_content>"
            from core.context_compressor import compress_ide_tool_result
            return f"<file_content>{compress_ide_tool_result(inner, max_chars=4000)}</file_content>"
        text = _re.sub(
            r"<file_content>(.*?)</file_content>",
            _compress_file_block,
            text,
            flags=_re.DOTALL,
        )
        return text.strip()

    def _resolve_system(text: str) -> str:
        """Replace KiloCode's bloated system prompt with a minimal string.
        Non-KiloCode system messages pass through with standard tag-stripping.
        """
        if any(k in text for k in _KILO_FINGERPRINT):
            return _KILO_MINIMAL_SYSTEM
        return _clean_ide_message(text)

    system_parts = [_resolve_system(m.text()) for m in req.messages if m.role == "system"]
    history_parts: list = []
    for m in req.messages[:-1]:
        cleaned = _clean_ide_message(m.text())
        if not cleaned:
            continue
        if m.role == "user":
            history_parts.append(f"User: {cleaned}")
        elif m.role == "assistant":
            history_parts.append(f"Assistant: {cleaned}")

    # ── FIX A: graceful empty-input handling ──────────────────────
    # Whitespace-only / empty user messages ("", "   ", "\n") previously raised
    # HTTP 400 ("No user message found in messages array"), which a top-tier
    # assistant must never do — it should ask what the user needs. We short-circuit
    # here with a valid OpenAI-shaped completion (same shape as the enhance path
    # below), respecting stream vs non-stream so every client stays happy.
    last_user = next(
        (_clean_ide_message(m.text()) for m in reversed(req.messages) if m.role == "user"), ""
    )
    # ── FIX A: graceful empty-input handling ──────────────────────
    # Whitespace-only / empty user messages ("", "   ", "\n") previously raised
    # HTTP 400 ("No user message found in messages array"), which a top-tier
    # assistant must never do — it should ask what the user needs. We short-circuit
    # here with a valid OpenAI-shaped completion (same shape as the enhance path
    # below), respecting stream vs non-stream so every client stays happy.
    if not last_user:
        _empty_msg = (
            "It looks like your message came through empty. What would you like "
            "help with? Feel free to type your question or describe what you need."
        )
        _empty_id = f"chatcmpl-{request_id[:8]}"
        _empty_ts = int(time.time())
        if req.stream:
            from fastapi.responses import StreamingResponse as _SR_empty

            def _empty_stream():
                _chunk = {
                    "id": _empty_id, "object": "chat.completion.chunk",
                    "created": _empty_ts, "model": req.model or _OPENAI_CODING,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": _empty_msg}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(_chunk)}\n\n"
                _done = {
                    "id": _empty_id, "object": "chat.completion.chunk",
                    "created": _empty_ts, "model": req.model or _OPENAI_CODING,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(_done)}\n\n"
                yield "data: [DONE]\n\n"

            return _SR_empty(_empty_stream(), media_type="text/event-stream")
        return {
            "id": _empty_id, "object": "chat.completion", "created": _empty_ts,
            "model": req.model or _OPENAI_CODING,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": _empty_msg}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    question_parts: list = []
    if system_parts:
        question_parts.append("System context:\n" + "\n".join(system_parts))

    # ── FIX C-F: response-calibration + grounding steering ────────
    # Placed in the stable prefix (before volatile history/task) so it stays
    # prompt-cacheable. Addresses the dominant benchmark failure modes:
    #   C over-verbosity, D placeholder hallucination, E over-refusal,
    #   F unhandled contradictions, plus reinforcing B (never deny a stated fact).
    _CHAT_STEERING = (
        "Response guidance:\n"
        "- Match response length and formatting to the request. Keep simple "
        "acknowledgments and short answers brief — no tables, headers, or emoji "
        "padding unless the user asks for them.\n"
        "- Do not invent meaning for vague or placeholder tokens (e.g. 'item-3'); "
        "acknowledge them plainly and, if truly needed, ask one short clarifying "
        "question. Never fabricate code, files, or details not present in context.\n"
        "- Do not over-refuse: answer simple recall, arithmetic, and on-topic "
        "follow-ups helpfully. A value the user themselves provided earlier in this "
        "conversation is not secret and is safe to repeat.\n"
        "- Never claim you have no record of a fact the user stated earlier in this "
        "same conversation — recall it from the history above rather than guessing "
        "or apologizing for 'making it up'.\n"
        "- When instructions conflict (e.g. 'be detailed in one word'), briefly name "
        "the tension and give a sensible resolution instead of ignoring it."
    )
    question_parts.append(_CHAT_STEERING)

    # ── FIX B: budget-aware conversation history ──────────────────
    # Previously `history_parts[-6:]` hard-dropped everything older than the last
    # 6 messages, so a fact stated in turn 0 (project code, deadline, budget…)
    # scrolled out of view and the model "forgot" it — the root cause of the
    # recall/override critical failures. Keep the FULL history; the existing
    # _MAX_PROMPT_CHARS guard + _truncate_middle (below) bound the total size and,
    # on overflow, trim from the middle so the earliest facts AND latest turns both
    # survive.
    if history_parts:
        question_parts.append("Conversation history:\n" + "\n".join(history_parts))
    question_parts.append(last_user)
    full_question = "\n\n".join(question_parts).strip()

    # ── Kilocode magic wand: route to _enhance_core() ────────────────
    # KiloCode's ENHANCE (magic-wand) feature sends the enhance instruction
    # as a USER message — NOT a system message — confirmed from live gateway
    # logs (messages=1, tools=NO, single user role message containing the
    # marker text followed by the user's raw prompt after the colon+newline).
    # We detect on ANY role so this works regardless of future KiloCode changes.
    # The raw prompt to enhance is extracted by stripping the instruction prefix.
    _KILO_ENHANCE_MARKER = "Generate an enhanced version of this prompt"
    _is_kilo_enhance = any(
        _KILO_ENHANCE_MARKER in m.text()
        for m in req.messages  # check ALL roles — KiloCode sends this as "user"
    )
    if _is_kilo_enhance:
        # Extract only the user's raw prompt text that follows the colon+newline
        # in the enhance instruction, e.g.:
        #   "Generate an enhanced version of this prompt (...):\n\nThis is a test prompt"
        # → "This is a test prompt"
        _enh_raw = last_user  # default: already extracted by the message parser
        for _m in req.messages:
            _mt = _m.text()
            if _KILO_ENHANCE_MARKER in _mt:
                # Split on the double-newline that separates instruction from prompt
                _parts = _mt.split("\n\n", 1)
                if len(_parts) == 2 and _parts[1].strip():
                    _enh_raw = _parts[1].strip()
                break
        _enh_result = _enhance_core(_enh_raw, include_followups=False)
        _enh_text   = _enh_result.get("enhanced", _enh_raw)
        _enh_id     = f"chatcmpl-{request_id[:8]}"
        _enh_ts     = int(time.time())
        _was_enhanced = "enhanced" in _enh_result
        _safe_enh_text = mask_pii(_enh_text)
        logger.info(f"[IDE] ENHANCE_RESULT req_id={request_id}  enhanced={_was_enhanced}  result_chars={len(_enh_text)}  (masked chars={len(_safe_enh_text)})")
        # ── ALWAYS return non-streaming JSON for enhance ──────────
        # Kilo Code's magic-wand uses AI SDK's generateText() which
        # parses the response as a single JSON object, even when the
        # provider is globally configured with stream=true.
        # Streaming here causes: AI_APICallError: Invalid JSON response
        return {
            "id": _enh_id, "object": "chat.completion", "created": _enh_ts, "model": req.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": _enh_text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    # Hard cap: ~32K chars — smart head+tail truncation preserves system context
    # at the top and the user's actual task at the bottom.  Naive [:N] slicing
    # was cutting the user's request entirely on large codebases.
    logger.info(f"[IDE] Character length of Full Question : {len(full_question)} chars")
    _MAX_PROMPT_CHARS = 32_000
    if len(full_question) > _MAX_PROMPT_CHARS:
        from core.context_compressor import _truncate_middle
        original_len = len(full_question)  # capture before mutation
        full_question = _truncate_middle(full_question, _MAX_PROMPT_CHARS)
        logger.warning(
            f"[IDE] prompt smart-truncated to {_MAX_PROMPT_CHARS} chars "
            f"(original: {original_len} chars)"
        )

    # ── Structured output: inject JSON schema constraint ─────────
    # When a client (e.g. Presenton) sends response_format with a json_schema,
    # append a compact schema instruction to the prompt so the LLM returns valid
    # JSON matching the schema. This is our compatibility shim for clients that
    # use OpenAI structured output but our gateway doesn't natively forward it.
    _rf = req.response_format
    if _rf and isinstance(_rf, dict):
        _rf_type = _rf.get("type", "")
        if _rf_type == "json_schema":
            _js = _rf.get("json_schema", {})
            _schema_str = json.dumps(_js.get("schema") or _js, separators=(",", ":"))
            full_question += (
                f"\n\n[CRITICAL INSTRUCTION: Output ONLY raw JSON — absolutely no markdown, "
                f"no ```json fences, no backticks, no code blocks, no explanation, no preamble, "
                f"no trailing text. Your response MUST start with the character {{ and end with }}. "
                f"Any character before {{ or after }} will cause a fatal parse error.]\n"
                f"Schema: {_schema_str[:3000]}"
            )
        elif _rf_type == "json_object":
            full_question += (
                "\n\n[CRITICAL INSTRUCTION: Output ONLY raw JSON — no ```json fences, "
                "no backticks, no code blocks. Start directly with { and end with }.]"
            )

    # ── Full compliance redaction on the assembled prompt ────────
    # mask_pii() only covers cards/phones/emails — run the full
    # compliance engine so Aadhaar, India PAN, IFSC, account numbers,
    # secrets, private keys etc. are all redacted before any LLM call.
    from agents.compliance_engine import compliance_engine as _ce_ide
    _ide_chk   = _ce_ide.validate_input(full_question)
    safe_question = _ide_chk.get("redacted_text") or full_question

    # Resolve model hint from the requested model name
    _model_hint = _oai_model_hint(req.model)
    # Extract bare model name when IDE sends "local:Kimi-k2.5" so the local
    # gateway knows which specific model to call.
    _local_model_name = (req.model.split(":", 1)[1] if (req.model or "").lower().startswith("local:") else None)

    # ── Browser-agent passthrough lane ────────────────────────────
    # Tagged by the extension via X-AiNxt-Client: browser-agent (parsed into
    # request.state.client_source by client_source_middleware). When true, the
    # tools proxy path preserves the system prompt, forwards images, and skips
    # session/tool compression. IDE traffic is untouched when this is False.
    # Computed once at handler scope so the sibling closures oai_stream() and
    # _tools_proxy_stream() both capture these flags. _passthrough itself is now
    # resolved earlier (right after identity resolution) for F4 log/trace labeling.
    # Image-bearing passthrough turns MUST use the gpt-4o proxy — the Claude
    # tools-stream flattens content to text and would drop the image_url part.
    # F2: steer per-turn — only a CURRENT-turn image forces the proxy; a stale
    # image buried in history must not sticky-pin the whole session away from the
    # user's chosen model.
    def _last_user_message(msgs):
        for m in reversed(msgs):
            if m.role == "user":
                return m
        return None

    _cur = _last_user_message(req.messages)
    _force_proxy_for_image = _passthrough and _cur is not None and _messages_have_image([_cur])

    # ── REQUEST ENTRY LOG ─────────────────────────────────────────
    # Log the user's actual message, NOT the system prompt.
    # full_question includes the Kilo Code system prompt (16K chars) which is
    # noise — we log last_user (the real user intent) instead.
    _sep = "─" * 60
    _safe_last_user = mask_pii(last_user)
    logger.info(f"{_log_tag} {_sep}")
    logger.info(f"{_log_tag} REQUEST   req_id={request_id}")
    logger.info(f"{_log_tag}           user_id={_user_id}")
    logger.info(f"{_log_tag}           model_requested={req.model!r}  hint={_model_hint!r}  stream={req.stream}")
    logger.info(f"{_log_tag}           messages={len(req.messages)}  tools={'YES (' + str(len(req.tools)) + ')' if req.tools else 'NO'}")
    logger.info(f"{_log_tag} USER_MSG  chars={len(last_user)}  (masked chars={len(_safe_last_user)})")
    logger.info(f"{_log_tag} USER_MSG↓ {_safe_last_user[:400]!r}{'...[truncated]' if len(_safe_last_user) > 400 else ''}")

    # Shared metadata dict
    _meta = {"out_tok": 0, "in_tok": 0, "model": _OPENAI_CODING, "cost": 0.0, "latency": 0.0}

    completion_id = f"chatcmpl-{request_id[:8]}"
    created_ts    = int(time.time())

    def _record_usage(full: str):
        """Increment budget + write model_usage row, then log the full summary."""
        _meta["latency"] = time.time() - start_time
        # Use actual token counts from OpenAI if captured, otherwise estimate
        if _meta["out_tok"] == 0:
            _meta["out_tok"] = int(len(full.split()) * 1.3)
        if _meta["in_tok"] == 0:
            _meta["in_tok"]  = int(len(safe_question.split()) * 1.3)
        if not _meta["model"]:
            _meta["model"] = req.model or _OPENAI_CODING
        _meta["cost"] = _estimate_cost(_meta["model"], _meta["in_tok"], _meta["out_tok"])

        # ── Budget: read before + increment + read after ──────────
        _budget_before = {}
        _budget_after  = {}
        _budget_limits = {}
        try:
            from store.budget_store import increment_usage as _inc, get_usage_today as _gut, get_budget as _gb
            _before = _gut(_user_id)
            _budget_before = {
                "tokens":   _before.get("tokens_used", 0),
                "requests": _before.get("requests_made", 0),
                "cost_usd": _before.get("cost_usd_spent", 0.0),
            }
            _inc(_user_id, tokens=_meta["in_tok"] + _meta["out_tok"], cost_usd=_meta["cost"])
            try:
                from core.time_utils import now_ist_iso as _now_ist_iso_ide
                # Derive source_channel from the client_source detected by
                # ClientSourceMiddleware. The /v1/chat/completions endpoint is
                # primarily used by IDE plugins, but browser-agent, direct API
                # callers, CLI, and the Electron desktop app also hit it.
                # Desktop gets DESKTOP-IDE (IDE tab inside the desktop app).
                # Plain browser (platform) gets WEB-IDE.
                # Standalone clients (CLI, IDE plugins, browser-agent, API) keep
                # their own unprefixed channel — they are not web or desktop.
                _cs_ide = getattr(request.state, "client_source", "ide-vscode")
                _ide_channel = {
                    "ide-vscode":    "IDE",
                    "ide-jetbrains": "IDE",
                    "browser-agent": "BROWSER-AGENT",
                    "api":           "API",
                    "cli":           "CLI",
                    "desktop":       "DESKTOP-IDE",
                    "buddy":         "WEB-BUDDY",    # Buddy surface on this endpoint — treat as Buddy
                    "platform":      "WEB-IDE",
                }.get(_cs_ide, "IDE")
                _kafka_produce("ainxt.metrics", {
                    "event":         "llm_cost",
                    "request_id":    request_id,
                    "user_id":       _user_id,
                    "agent_id":      "ide_direct",
                    "endpoint":      "/v1/chat/completions",
                    "source_channel": _ide_channel,
                    "model":         _resolve_model_id(_meta.get("model", "")),
                    "input_tokens":  _meta.get("in_tok", 0),
                    "output_tokens": _meta.get("out_tok", 0),
                    "total_tokens":  _meta.get("in_tok", 0) + _meta.get("out_tok", 0),
                    "latency_ms":    _meta.get("latency", 0.0) * 1000,
                    "cost_usd":      _meta.get("cost", 0.0),
                    "product_id":    None,
                    "timestamp":     _now_ist_iso_ide(),
                })
                logger.info(
                    f"[IDE] model_usages produced request_id={request_id} user={_user_id} "
                    f"channel={_ide_channel} model={_meta.get('model')} cost=${_meta.get('cost', 0.0):.6f}"
                )
            except Exception as _ide_mu_err:
                logger.warning(f"[IDE] ainxt.metrics produce FAILED request_id={request_id} user={_user_id}: {_ide_mu_err}")
            _after = _gut(_user_id)
            _budget_after = {
                "tokens":   _after.get("tokens_used", 0),
                "requests": _after.get("requests_made", 0),
                "cost_usd": _after.get("cost_usd_spent", 0.0),
            }
            _bdg = _gb(_user_id)
            if _bdg:
                _budget_limits = {
                    "max_tokens":   _bdg.get("max_tokens_total", 0),
                    "max_requests": _bdg.get("max_requests_total", 0),
                    "max_cost_usd": _bdg.get("max_cost_usd_total", 0.0),
                }
        except Exception as _be:
            logger.warning(f"[IDE] budget tracking error: {_be}")

        # ── AiNxt Coach — emit practice event for IDE path ───────────────────────
        # Fire-and-forget; no-op when ENABLE_COACH is false. Use `last_user`
        # (the actual current user request) rather than the assembled full_question
        # so Coach stores only the real prompt, not system context + history.
        _coach_last_user = (last_user or "").strip()
        try:
            from core.coach_events import emit_coach_event, channel_from_client_source, _extract_coach_task as _coach_extract
            _coach_last_user = _coach_extract(_coach_last_user) or _coach_last_user  # strip browser-agent page context
            _cs_ide = getattr(request.state, "client_source", "mcp")
            _coach_ide_channel = channel_from_client_source(_cs_ide)
            # Use the real session_id as thread_id only when the client sent one.
            # The synthetic time-bucket fallback (_ide_thread_id when req.session_id
            # is absent) groups unrelated prompts into one session — pass None so
            # each prompt is treated as unthreaded and grouped independently.
            _coach_ide_thread = req.session_id or None
            logger.info(f"[Coach] emitting IDE event user={_user_id} client_source={_cs_ide} channel={_coach_ide_channel} thread_id={_coach_ide_thread} prompt_len={len(_coach_last_user)}")
            emit_coach_event(
                user_id=_user_id or "anonymous",
                channel=_coach_ide_channel,
                model=_meta.get("model"),
                prompt=_coach_last_user,
                completion=full,
                tokens_in=_meta.get("in_tok", 0),
                tokens_out=_meta.get("out_tok", 0),
                cost_usd=_meta.get("cost", 0.0),
                latency_ms=int(_meta.get("latency", 0) * 1000),
                request_id=request_id,
                thread_id=_coach_ide_thread,
                department=None,
                accepted=None,
            )
        except Exception as _coach_ide_err:
            logger.warning(f"[Coach] IDE emit failed: {_coach_ide_err}")

        # ── Final summary log ─────────────────────────────────────
        logger.info(f"{_log_tag} {_sep}")
        logger.info(f"{_log_tag} RESPONSE  req_id={request_id}")
        logger.info(f"{_log_tag}           model_used={_meta['model']!r}")
        logger.info(f"{_log_tag}           tokens  in={_meta['in_tok']}  out={_meta['out_tok']}  total={_meta['in_tok']+_meta['out_tok']}")
        logger.info(f"{_log_tag}           cost    this_call=${_meta['cost']:.6f} USD")
        logger.info(f"{_log_tag}           latency {_meta['latency']:.2f}s")
        if _budget_before:
            logger.info(f"{_log_tag} BUDGET    user_id={_user_id}")
            logger.info(f"{_log_tag}           before → tokens={_budget_before['tokens']}  reqs={_budget_before['requests']}  cost=${_budget_before['cost_usd']:.6f}")
            logger.info(f"{_log_tag}           after  → tokens={_budget_after['tokens']}  reqs={_budget_after['requests']}  cost=${_budget_after['cost_usd']:.6f}")
            if _budget_limits:
                def _pct(used, limit):
                    return f"{used}/{limit} ({100*used/limit:.1f}%)" if limit else f"{used}/∞"
                logger.info(f"{_log_tag}           limits → tokens={_pct(_budget_after['tokens'], _budget_limits['max_tokens'])}  "
                            f"reqs={_pct(_budget_after['requests'], _budget_limits['max_requests'])}  "
                            f"cost={_pct(_budget_after['cost_usd'], _budget_limits['max_cost_usd'])}")
        logger.info(f"{_log_tag} {_sep}")

    def _build_oai_messages() -> tuple:
        """Convert _OAIMessage list → OpenAI API message dicts with full compliance gate.

        Every user and tool-result message is:
          1. Scanned by compliance_engine.validate_input() on raw (pre-mask) content.
          2. PII-masked before being forwarded to the LLM.

        System messages contain only tool definitions — they are never sent to
        external users, so they pass through unchanged.
        Assistant messages preserve tool_calls; content is forwarded as-is.

        Returns:
            (messages: list, history_blocked: list)
            history_blocked is a deduplicated list of blocked PII/PCI type names
            found anywhere in the conversation history.  Empty = all clear.
        """
        from agents.compliance_engine import compliance_engine
        history_blocked: list = []
        out = []

        for i, m in enumerate(req.messages):
            if m.role == "system":
                # Tool definitions — pass through, do not scan or mask
                out.append({"role": "system", "content": m.text()})

            elif m.role == "user":
                raw = _clean_ide_message(m.text())
                # Full compliance on raw content BEFORE masking. Gated by
                # COMPLIANCE_SCAN_HISTORY — the current user turn is already scanned
                # by the caller (Gate 1). When OFF (default), prior turns are masked
                # with the fast regex layer only, not re-scanned by the ML engine.
                from core.config import COMPLIANCE_SCAN_HISTORY
                if COMPLIANCE_SCAN_HISTORY:
                    chk    = compliance_engine.validate_input(raw)
                    btypes = [f["type"] for f in chk.get("findings", []) if f.get("blocked")]
                    if btypes:
                        logger.warning(f"[IDE] HISTORY VIOLATION msg[{i}] role=user  types={btypes}")
                        history_blocked.extend(btypes)
                    masked = chk.get("redacted_text") or mask_pii(raw)
                else:
                    masked = mask_pii(raw)
                if masked:
                    out.append({"role": "user", "content": masked})

            elif m.role == "assistant":
                msg: dict = {"role": "assistant"}
                if m.tool_calls:
                    msg["tool_calls"] = m.tool_calls
                    msg["content"] = m.content if isinstance(m.content, str) else None
                else:
                    msg["content"] = m.text() or None
                out.append(msg)

            elif m.role == "tool":
                raw = m.text()
                # Full compliance on raw tool result (file contents, cmd output, etc.).
                # Gated by COMPLIANCE_SCAN_TOOL_RESULTS — the file-read data-breach
                # guard. When OFF (default), tool output is forwarded as-is.
                from core.config import COMPLIANCE_SCAN_TOOL_RESULTS
                if COMPLIANCE_SCAN_TOOL_RESULTS:
                    chk    = compliance_engine.validate_input(raw)
                    btypes = [f["type"] for f in chk.get("findings", []) if f.get("blocked")]
                    if btypes:
                        logger.warning(f"[IDE] HISTORY VIOLATION msg[{i}] role=tool  types={btypes}")
                        history_blocked.extend(btypes)
                    safe_content = chk.get("redacted_text") or mask_pii(raw)
                else:
                    logger.info("[IDE] COMPLIANCE SKIP msg[%s] role=tool reason=tool_results_disabled", i)
                    safe_content = raw
                # Compress large tool results (file reads, search outputs) to 6K chars.
                # This is the primary source of token explosion on large codebases —
                # every file read by Kilo Code lands here as raw file content.
                if len(safe_content) > 6000:
                    _orig_tool_len = len(safe_content)
                    from core.context_compressor import compress_ide_tool_result
                    safe_content = compress_ide_tool_result(safe_content, max_chars=6000)
                    _saved_tool = _orig_tool_len - len(safe_content)
                    try:
                        from core.compress_metrics import record as _cm_rec
                        _cm_rec("ide_tool", _orig_tool_len, len(safe_content))
                    except Exception:
                        pass
                msg = {"role": "tool", "content": safe_content}
                if m.tool_call_id:
                    msg["tool_call_id"] = m.tool_call_id
                if m.name:
                    msg["name"] = m.name
                out.append(msg)

        # Deduplicate while preserving first-occurrence order
        seen: set = set()
        deduped: list = []
        for t in history_blocked:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        return out, deduped

    def _build_passthrough_messages() -> tuple:
        """Browser-agent passthrough builder — mirrors _build_oai_messages() but
        preserves the agent's own request faithfully:

          • system : forwarded VERBATIM (raw m.content, not m.text()) — no
                      _resolve_system, no _clean_ide_message. Keeps the plugin's
                      carefully-tuned AGENT_SYSTEM_PROMPT (and any image parts) intact.
          • user   : forwarded AS-IS. If content is a list (text + image_url), the
                      list is kept so the multimodal model receives the image.
                      Compliance block-gate honored via COMPLIANCE_SCAN_HISTORY
                      (same flag as the IDE path) — no extra masking is added.
          • assistant : tool_calls + content preserved exactly (same as IDE path).
          • tool   : honors COMPLIANCE_SCAN_TOOL_RESULTS (same flag as IDE path);
                      the 6K compress_ide_tool_result cap is SKIPPED so the agent
                      keeps full DOM snapshots.

        Returns (messages, history_blocked) — identical shape to _build_oai_messages,
        so the existing block-gate handling in _tools_proxy_stream works unchanged.
        """
        from agents.compliance_engine import compliance_engine
        from core.config import COMPLIANCE_SCAN_HISTORY as _CFG_SCAN_HIST, \
                                COMPLIANCE_SCAN_TOOL_RESULTS as _CFG_SCAN_TOOL
        # F3 — Passthrough lane always enforces history + tool-result scanning; the
        # ledger (F1) keeps the cost O(N). Scoped to these local names inside this
        # builder (only called when _passthrough), so IDE/CLI config reads elsewhere
        # are unaffected. An operator can still fully disable via PASSTHRU_ENFORCE_SCAN.
        COMPLIANCE_SCAN_HISTORY      = True if _PT_ENFORCE_SCAN else _CFG_SCAN_HIST
        COMPLIANCE_SCAN_TOOL_RESULTS = True if _PT_ENFORCE_SCAN else _CFG_SCAN_TOOL
        history_blocked: list = []
        out = []

        # Indices of the most-recent user/tool messages — always scanned fresh.
        _last_user_idx = max((j for j, mm in enumerate(req.messages) if mm.role == "user"),
                             default=-1)
        _last_tool_idx = max((j for j, mm in enumerate(req.messages) if mm.role == "tool"),
                             default=-1)

        def _is_current(role: str, idx: int) -> bool:
            return (role == "user" and idx == _last_user_idx) or \
                   (role == "tool" and idx == _last_tool_idx)

        def _text_of(content) -> str:
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "\n".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            return ""

        for i, m in enumerate(req.messages):
            if m.role == "system":
                # Forward the raw system content verbatim (preserve list/image parts).
                out.append({"role": "system", "content": m.content})

            elif m.role == "user":
                # Forward content as-is (keep the list so image_url survives).
                # Run the block-gate on text parts only when history scanning is on.
                # F1: skip the ML re-scan for history messages that already passed.
                # NOTE: user content is forwarded verbatim (redaction is not applied
                # to user turns here), so the ledger only serves to skip the block
                # re-scan — the current turn is always scanned fresh.
                txt = _text_of(m.content)
                if COMPLIANCE_SCAN_HISTORY:
                    _u_key = _pt_ledger_key(_user_id, "user", txt)
                    _scan_user = (not _PT_SCAN_LEDGER_ENABLED
                                  or _is_current("user", i)
                                  or _pt_ledger_get(_u_key) is None)
                    if _scan_user:
                        chk    = compliance_engine.validate_input(txt)
                        btypes = [f["type"] for f in chk.get("findings", []) if f.get("blocked")]
                        if btypes:
                            logger.warning(f"[PASSTHRU] HISTORY VIOLATION msg[{i}] role=user  types={btypes}")
                            history_blocked.extend(btypes)
                        else:
                            # Passed → record so history isn't re-scanned next turn.
                            # NOTE: unlike the tool branch, the stored VALUE is never
                            # read back — user content is forwarded verbatim (no
                            # redaction reuse), so only the KEY's presence matters
                            # here. We store `txt` merely to satisfy the signature.
                            _pt_ledger_mark(_u_key, txt)
                if m.content is not None:
                    out.append({"role": "user", "content": m.content})

            elif m.role == "assistant":
                msg: dict = {"role": "assistant"}
                if m.tool_calls:
                    msg["tool_calls"] = m.tool_calls
                    msg["content"] = m.content if isinstance(m.content, str) else None
                else:
                    msg["content"] = m.text() or None
                out.append(msg)

            elif m.role == "tool":
                raw = m.text()
                # Tool-result secret/PII scan is the CRITICAL data-breach guard but
                # fires a blocking ML HTTP call per result — gated off by default.
                # Honor the same flag as the IDE path; skip the 6K compression so the
                # agent retains full DOM snapshots.
                # F1: skip the ML re-scan for history results that already passed.
                # On the skip path we reuse the EXACT redacted_text stored when the
                # result first passed — NOT mask_pii(raw), which is regex-only and
                # would leak ML-only-detected values (e.g. an ML-detected
                # ACCOUNT_NUMBER) in the clear from turn 2 onward.
                if COMPLIANCE_SCAN_TOOL_RESULTS:
                    safe_content, btypes = _pt_scan_tool_result(
                        _user_id, raw, _is_current("tool", i),
                        compliance_engine.validate_input, mask_pii,
                    )
                    if btypes:
                        logger.warning(f"[PASSTHRU] HISTORY VIOLATION msg[{i}] role=tool  types={btypes}")
                        history_blocked.extend(btypes)
                else:
                    safe_content = raw
                msg = {"role": "tool", "content": safe_content}
                if m.tool_call_id:
                    msg["tool_call_id"] = m.tool_call_id
                if m.name:
                    msg["name"] = m.name
                out.append(msg)

        # Deduplicate while preserving first-occurrence order
        seen: set = set()
        deduped: list = []
        for t in history_blocked:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        return out, deduped

    def _tools_proxy_stream():
        """Stream tokens + tool_calls from the appropriate OpenAI model when the request includes tools."""
        from agents.compliance_engine import compliance_engine
        from core.model_registry import OPENAI_CODING_MODEL, OPENAI_LATEST_MODEL, OPENAI_SIMPLE_MODEL

        # ── Helper: yield a properly-formatted OpenAI SSE JSON chunk ──
        # _tools_proxy_stream() yields pre-serialised JSON that oai_stream()
        # forwards verbatim as  data: <chunk>\n\ndata: [DONE]``.
        def _make_chunk(content: str, finish: str = "stop") -> str:
            return json.dumps({
                "id":      completion_id,
                "object":  "chat.completion.chunk",
                "created": created_ts,
                "model":   req.model,
                "choices": [{
                    "index":         0,
                    "delta":         {"role": "assistant", "content": content},
                    "finish_reason": finish,
                }],
            })

        def _is_openai_sse_chunk(chunk_d: dict) -> bool:
            """Return True only for chunks accepted by strict OpenAI SSE parsers."""
            return isinstance(chunk_d.get("choices"), list) or isinstance(chunk_d.get("error"), dict)

        def _log_skipped_non_oai_chunk(source: str, chunk_d: dict) -> None:
            logger.warning(f"[IDE] ← SKIP_NON_OPENAI_SSE_CHUNK ({source}) {chunk_d!r}")

        # ── Gate 1: current user message ─────────────────────────
        # Scan last_user raw (pre-mask) — Luhn / regex must see the original digits.
        findings_all = compliance_engine.validate_input(last_user).get("findings", [])
        blocked_types = [f["type"] for f in findings_all if f.get("blocked")]
        allowed_types = [f["type"] for f in findings_all if not f.get("blocked")]
        logger.info(f"[IDE] COMPLIANCE current-message (tools-proxy)")
        logger.info(f"[IDE]           status={'BLOCKED' if blocked_types else 'ALLOWED'}")
        logger.info(f"[IDE]           blocked_findings={blocked_types or 'none'}")
        logger.info(f"[IDE]           non-blocking_findings={allowed_types or 'none'}")
        if blocked_types:
            logger.warning(f"[IDE] *** REQUEST BLOCKED (current message) — {blocked_types} ***")
            yield _make_chunk(f"[Request blocked by compliance policy: {', '.join(blocked_types)}]")
            return

        # ── Gate 2: full conversation history ─────────────────────
        # _build_oai_messages() runs compliance_engine on every user and tool
        # message in the history (raw, before masking), then applies mask_pii().
        # Individual tool messages are already compressed to 6K chars inside
        # _build_oai_messages().  Here we apply session-level history compression:
        # older tool-call rounds have their results replaced with summaries so
        # long IDE sessions don't accumulate 100K+ tokens of file contents.
        oai_messages, history_blocked = (_build_passthrough_messages() if _passthrough
                                         else _build_oai_messages())
        if history_blocked:
            logger.warning(f"[IDE] *** REQUEST BLOCKED (conversation history) — {history_blocked} ***")
            yield _make_chunk(f"[Request blocked by compliance policy in conversation history: {', '.join(history_blocked)}]")
            return
        logger.info(f"[IDE] COMPLIANCE conversation history (tools-proxy) — PASSED")

        if not oai_messages:
            yield _make_chunk("Error: no valid messages to send")
            return

        # Session history compression: keep last 4 tool-call rounds verbatim;
        # collapse older rounds to action-only (drops massive file-read payloads).
        # SKIPPED for the browser-agent passthrough lane — agents need their full
        # DOM-snapshot history across every round.
        if not _passthrough:
            _pre_compress_chars = sum(len(m.get("content", "") or "") for m in oai_messages if isinstance(m.get("content"), str))
            from core.context_compressor import compress_ide_messages
            oai_messages = compress_ide_messages(oai_messages, keep_recent_rounds=4)
            _post_compress_chars = sum(len(m.get("content", "") or "") for m in oai_messages if isinstance(m.get("content"), str))
            if _pre_compress_chars != _post_compress_chars:
                logger.info(
                    f"[IDE] session history compressed: {_pre_compress_chars:,} → {_post_compress_chars:,} chars "
                    f"({100 * (1 - _post_compress_chars / max(_pre_compress_chars, 1)):.0f}% reduction)"
                )
                try:
                    from core.compress_metrics import record as _cm_record
                    _cm_record("ide_session", _pre_compress_chars, _post_compress_chars)
                except Exception:
                    pass

        # Route through LLM proxy (the LLM proxy server) for tool-call streaming.
        # API keys live exclusively in services/llm_proxy/.env on the LLM proxy server.
        # Use Gemini endpoint when _model_hint=="gemini", OpenAI otherwise.
        _proxy_url = os.getenv("LLM_PROXY_URL", "").rstrip("/")
        if not _proxy_url:
            logger.error("[IDE-TOOLS] LLM_PROXY_URL not set — cannot stream tools via proxy")
            yield _make_chunk("Configuration error: LLM proxy not configured")
            return

        import httpx as _httpx
        import json as _json2

        _use_gemini = (_model_hint == "gemini")
        _use_local  = (_model_hint == "local")

        if _use_local:
            # Route directly to the local LLM (OpenAI-compat) — bypass cloud proxy.
            from gateway_local_llm import (
                LOCAL_LLM_BASE_URL as _local_url,
                LOCAL_LLM_API_KEY  as _local_key,
                _catalog           as _lcat,
            )
            if not _local_url:
                logger.error("[IDE-TOOLS] LOCAL_LLM_BASE_URL not set — cannot route tool calls to local model")
                yield _make_chunk("Configuration error: local LLM not configured")
                return
            _tools_model = _local_model_name or _lcat.pick("medium") or ""
            _local_payload: dict = {
                "model":      _tools_model,
                "messages":   oai_messages,
                "tools":      req.tools if req.tools else [],
                "max_tokens": req.max_tokens or 8000,
                "stream":     True,
            }
            if req.tool_choice is not None:
                _local_payload["tool_choice"] = req.tool_choice
            _meta["model"] = req.model  # "local:Kimi-k2.5" — carries "local" prefix for zero-cost tracking
            logger.info(f"[IDE] → SENDING TO LOCAL MODEL (tools, direct): {_tools_model}")
            _chunk_count = 0
            _tool_calls_seen: dict = {}
            try:
                _local_hdrs = {
                    "Authorization": f"Bearer {_local_key}",
                    "Content-Type":  "application/json",
                }
                with _httpx.stream(
                    "POST",
                    f"{_local_url}/v1/chat/completions",
                    json=_local_payload,
                    headers=_local_hdrs,
                    timeout=120.0,
                ) as _resp:
                    _resp.raise_for_status()
                    for _line in _resp.iter_lines():
                        if not _line or _line == "data: [DONE]":
                            continue
                        # Local LLM returns SSE format ("data: {...}"); strip prefix.
                        _raw = _line[6:] if _line.startswith("data: ") else _line
                        try:
                            chunk_d = _json2.loads(_raw)
                        except _json2.JSONDecodeError:
                            continue
                        if not _is_openai_sse_chunk(chunk_d):
                            _log_skipped_non_oai_chunk("local", chunk_d)
                            continue
                        _chunk_count += 1
                        for _choice in chunk_d.get("choices", []):
                            _delta  = _choice.get("delta", {})
                            _finish = _choice.get("finish_reason")
                            _tcs    = _delta.get("tool_calls", [])
                            for _tc in _tcs:
                                _idx = _tc.get("index", 0)
                                _fn  = _tc.get("function", {})
                                if _idx not in _tool_calls_seen:
                                    _tool_calls_seen[_idx] = {"name": "", "args": ""}
                                    if _fn.get("name"):
                                        _tool_calls_seen[_idx]["name"] = _fn["name"]
                                        logger.info(f"[IDE] ← TOOL_CALL [{_idx}] name={_fn['name']!r}")
                                if _fn.get("arguments"):
                                    _tool_calls_seen[_idx]["args"] += _fn["arguments"]
                            if _finish:
                                logger.info(f"[IDE] ← FINISH finish_reason={_finish!r}")
                        # FIX: read `usage` at the TOP LEVEL of the
                        # chunk, unconditionally — moved OUT of the
                        # `for _choice in chunk_d.get("choices", [])` loop above.
                        # The dedicated usage-only chunk has "choices": [] by
                        # design (the OpenAI-spec-correct shape for a trailing
                        # usage chunk), so gating this read behind that loop meant
                        # it silently never ran — _meta["in_tok"]/["out_tok"]
                        # stayed 0. Local models are billed $0 regardless, so this
                        # only ever corrupted logged token counts here, but it is
                        # the identical pattern that DID corrupt real cloud
                        # billing in the sibling branch below (now fixed via
                        # services.cloud_tool_stream) — fixed here too since the
                        # bug is adjacent and the pattern is the same.
                        _usage = chunk_d.get("usage")
                        if _usage:
                            _meta["in_tok"]  = _usage.get("prompt_tokens",     _meta["in_tok"])
                            _meta["out_tok"] = _usage.get("completion_tokens", _meta["out_tok"])
                        _raw_out = _json2.dumps(chunk_d)
                        yield _raw_out
            except Exception as _e:
                logger.exception(
                    f"[IDE] local tools call failed request_id={request_id} {repr(_e)[:1500]}"
                )
                yield _make_chunk("Error generating response")
            for _idx, _tc_info in _tool_calls_seen.items():
                logger.info(
                    f"[IDE] ← TOOL_CALL_RAW [{_idx}] "
                    f"name={_tc_info['name']!r} args={_tc_info['args']!r}"
                )
                logger.info(f"[IDE] ← TOOL_CALL [{_idx}] {_tc_info['name']}  args={_tc_info['args'][:200]!r}")
            logger.info(f"[IDE] ← DONE  chunks_received={_chunk_count}")
            return

        _tools_endpoint  = "/llm/gemini-tools-stream" if _use_gemini else "/llm/openai-tools-stream"
        if _use_gemini:
            _tools_model = _GEMINI_VISION
        elif _model_hint == "deep":
            _tools_model = OPENAI_LATEST_MODEL
        elif _model_hint == "mini":
            _tools_model = OPENAI_SIMPLE_MODEL
        else:
            _tools_model = OPENAI_CODING_MODEL

        # Passthrough image turn steered here from a Claude hint: pin a
        # vision-capable gpt-4o-class model explicitly (don't rely on fall-through).
        if _force_proxy_for_image:
            _tools_model = OPENAI_CODING_MODEL

        _payload: dict = {
            "messages":    oai_messages,
            "tools":       req.tools if req.tools else [],
            "model":       _tools_model,
            "max_tokens":  req.max_tokens or 8000,
            "request_id":  request_id,   # propagate for end-to-end log correlation
        }
        if req.tool_choice is not None:
            _payload["tool_choice"] = req.tool_choice

        _meta["model"] = _tools_model
        # ── Log what we're about to send ──────────────────────────
        logger.info(f"{_log_tag} → SENDING TO MODEL (via proxy): {_tools_model}")
        logger.info(f"{_log_tag}   messages ({len(oai_messages)} total):")
        for _i, _m in enumerate(oai_messages):
            _role    = _m.get("role", "?")
            # Passthrough messages may carry list content (text + image_url). Render
            # text parts and elide images so we never dump a base64 data URL into logs.
            _raw_content = _m.get("content")
            if isinstance(_raw_content, list):
                _content = " ".join(
                    "[image]" if isinstance(p, dict) and p.get("type") == "image_url"
                    else (p.get("text", "") if isinstance(p, dict) else str(p))
                    for p in _raw_content
                )
            else:
                _content = str(_raw_content or "")
            _tc      = _m.get("tool_calls")
            _tcid    = _m.get("tool_call_id", "")
            if _role == "system":
                logger.info(f"[IDE]     [{_i}] system")
            elif _role == "user":
                logger.info(f"[IDE]     [{_i}] user")
            elif _role == "assistant" and _tc:
                for _t in _tc:
                    _fn = _t.get("function", {})
                    logger.info(f"[IDE]     [{_i}] asst    : tool_call → {_fn.get('name','?')}({_fn.get('arguments','')[:80]})")
            elif _role == "tool":
                logger.info(f"[IDE]     [{_i}] tool    : id={_tcid!r} ")
            else:
                logger.info(f"[IDE]     [{_i}] {_role:8}")
        logger.info(f"[IDE]   tools_count={len(req.tools) if req.tools else 0}  tool_choice={req.tool_choice!r}")

        # ── Stream via the shared cloud-tools client and log response ──────
        #
        # REFACTOR: the direct httpx call + ndjson parsing that
        # used to live here has moved into services.cloud_tool_stream
        # .stream_cloud_tools(), which is now the single implementation shared
        # with routers/endpoint_proxy_router.py's managed-endpoint proxy. This
        # function still builds the exact same pre-serialised OpenAI SSE JSON
        # chunks it always has (oai_stream() forwards them verbatim as
        # `data: <chunk>\n\n`) — only the HTTP-call-and-translate internals
        # moved out. Model/provider selection above (_tools_model,
        # _use_gemini, _force_proxy_for_image) is unchanged.
        #
        # Side effect: this also fixes the confirmed usage-extraction bug that
        # lived in the old inline code (usage was read from INSIDE
        # `for _choice in chunk_d.get("choices", [])`, but the proxy's
        # dedicated usage chunk has "choices": [] by design, so it silently
        # never fired). stream_cloud_tools reads usage at the top level,
        # unconditionally — see its module docstring.
        def _make_tc_chunk(delta: dict, finish: str = None) -> str:
            return _json2.dumps({
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created_ts, "model": req.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            })

        _chunk_count = 0
        _tool_calls_seen: dict = {}
        try:
            from services.cloud_tool_stream import stream_cloud_tools
            _provider = "gemini" if _use_gemini else "openai"
            _tc_finish_reason = "stop"

            for _tok in stream_cloud_tools(
                oai_messages, req.tools, req.tool_choice, _tools_model, _provider,
                max_tokens=req.max_tokens or 8000, request_id=request_id,
            ):
                if isinstance(_tok, dict):
                    if "__stream_meta__" in _tok:
                        _sm = _tok["__stream_meta__"] or {}
                        _meta["in_tok"]  = _sm.get("in_tok",  _meta["in_tok"])
                        _meta["out_tok"] = _sm.get("out_tok", _meta["out_tok"])
                        # Real provider finish_reason ("stop"/"tool_calls"/
                        # "length"/"content_filter"/...) — never guessed.
                        _tc_finish_reason = _sm.get("finish_reason") or "stop"
                        logger.info(
                            f"{_log_tag} ← USAGE   prompt_tokens={_meta['in_tok']}  "
                            f"completion_tokens={_meta['out_tok']}"
                        )
                        continue
                    if "tool_call_delta" in _tok:
                        _tc  = _tok["tool_call_delta"]
                        _idx = _tc.get("index", 0)
                        _fn  = _tc.get("function", {})
                        if _idx not in _tool_calls_seen:
                            _tool_calls_seen[_idx] = {"name": "", "args": ""}
                            if _fn.get("name"):
                                _tool_calls_seen[_idx]["name"] = _fn["name"]
                                logger.info(f"[IDE] ← TOOL_CALL [{_idx}] name={_fn['name']!r}")
                        if _fn.get("arguments"):
                            _tool_calls_seen[_idx]["args"] += _fn["arguments"]
                        _chunk_count += 1
                        yield _make_tc_chunk({"tool_calls": [_tc]})
                    continue
                if isinstance(_tok, str) and _tok:
                    _chunk_count += 1
                    yield _make_tc_chunk({"content": _tok})

            logger.info(f"{_log_tag} ← FINISH finish_reason={_tc_finish_reason!r}")
            yield _make_tc_chunk({}, finish=_tc_finish_reason)

        except Exception as _e:
            logger.exception(
                f"[IDE] proxy tools call failed request_id={request_id} {repr(_e)[:1500]}"
            )
            yield _make_chunk("Error generating response")

        for _idx, _tc_info in _tool_calls_seen.items():
            logger.info(
                f"[IDE] ← TOOL_CALL_RAW [{_idx}] "
                f"name={_tc_info['name']!r} args={_tc_info['args']!r}"
            )
            logger.info(f"[IDE] ← TOOL_CALL [{_idx}] {_tc_info['name']}  args={_tc_info['args'][:200]!r}")
        logger.info(f"[IDE] ← DONE  chunks_received={_chunk_count}")

    # Lazy-load model_router singleton once for use in both inner functions
    from models.model_router import model_router as _mr_instance

    def _tools_claude_stream():
        """
        Stream tool-call responses from Claude Sonnet when _model_hint=='claude'.

        REFACTOR: the OpenAI<->Anthropic message/tool conversion,
        the HTTP call to /llm/claude-tools-stream, and the tbs/tad/txt/stop
        event translation all moved into
        services.cloud_tool_stream.stream_cloud_tools() — the same shared
        implementation routers/endpoint_proxy_router.py's managed-endpoint
        proxy now uses. This function keeps everything that is genuinely
        IDE-route-specific: compliance gating/masking per message (which must
        run on the caller's raw Pydantic message objects, not generic OpenAI
        dicts, so it stays here) and building the OpenAI-format message list
        the shared module expects as input. Wire output (this route's SSE
        envelope, via `_c()`) and model selection are unchanged.
        """
        from agents.compliance_engine import compliance_engine
        from core.model_registry import (
            CLAUDE_PRIMARY_MODEL, SOLUTION_MODEL,
            CLAUDE_HAIKU,
            CLAUDE_OPUS_48_MODEL, CLAUDE_OPUS_5_MODEL, CLAUDE_SONNET_5_MODEL,
        )

        # ── Helper: make an OpenAI-format SSE chunk ───────────────
        def _c(content: str, finish: str = None, tool_calls_delta: list = None) -> str:
            delta: dict = {}
            if content is not None:
                delta["content"] = content
            if tool_calls_delta:
                delta["tool_calls"] = tool_calls_delta
            ch = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created_ts, "model": req.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return json.dumps(ch)

        # ── Gate: current user message compliance ─────────────────
        findings_all  = compliance_engine.validate_input(last_user).get("findings", [])
        blocked_types = [f["type"] for f in findings_all if f.get("blocked")]
        if blocked_types:
            logger.warning(f"[IDE-Claude] BLOCKED {blocked_types}")
            yield _c(f"[Request blocked: {', '.join(blocked_types)}]", finish="stop")
            return

        # ── Build OpenAI-format messages (compliance-scanned/masked here —
        # this is caller responsibility, exactly as endpoint_proxy_router's
        # _compliance_check_input runs before calling the shared module).
        # The Anthropic conversion itself now lives in
        # services.cloud_tool_stream._oai_messages_to_anthropic /
        # _oai_tools_to_anthropic.
        oai_msgs: list = []
        for m in req.messages:
            if m.role == "system":
                txt = _resolve_system(m.text())
                if txt:
                    oai_msgs.append({"role": "system", "content": txt})

            elif m.role == "user":
                raw = _clean_ide_message(m.text())
                # Gated by COMPLIANCE_SCAN_HISTORY — the current user turn is already
                # scanned at Gate 1 above. When OFF (default), prior turns are masked
                # with the fast regex layer only, not re-scanned by the ML engine.
                from core.config import COMPLIANCE_SCAN_HISTORY
                if COMPLIANCE_SCAN_HISTORY:
                    chk    = compliance_engine.validate_input(raw)
                    btypes = [f["type"] for f in chk.get("findings", []) if f.get("blocked")]
                    if btypes:
                        logger.warning(f"[IDE-Claude] HISTORY HARD-BLOCKED {btypes}")
                        yield _c(f"[Request blocked: {', '.join(btypes)}]", finish="stop")
                        return
                    masked = chk.get("redacted_text") or mask_pii(raw)
                else:
                    masked = mask_pii(raw)
                if masked:
                    oai_msgs.append({"role": "user", "content": masked})

            elif m.role == "assistant":
                if m.tool_calls:
                    oai_msgs.append({
                        "role": "assistant", "content": m.text() or "",
                        "tool_calls": m.tool_calls,
                    })
                else:
                    txt = m.text()
                    if txt:
                        oai_msgs.append({"role": "assistant", "content": txt})

            elif m.role == "tool":
                # Tool output may contain secrets read from disk (.env,
                # credentials, ssh keys); redact before injecting into context.
                _tool_text = m.text() or ""
                from core.config import COMPLIANCE_SCAN_TOOL_RESULTS
                if COMPLIANCE_SCAN_TOOL_RESULTS:
                    try:
                        from agents.compliance_engine import compliance_engine as _ce_tr
                        _tool_text_safe, _tool_redacted = _ce_tr.redact_text(_tool_text)
                        if _tool_redacted:
                            logger.info(f"tool_result redacted types={_tool_redacted}")
                    except Exception:
                        _tool_text_safe = _tool_text
                else:
                    logger.info("[IDE-Claude] COMPLIANCE SKIP role=tool reason=tool_results_disabled")
                    _tool_text_safe = _tool_text
                oai_msgs.append({
                    "role": "tool", "tool_call_id": m.tool_call_id or "",
                    "content": _tool_text_safe,
                })

        if not oai_msgs:
            yield _c("Error: no messages to send", finish="stop")
            return

        # ── Select Claude model for tool-use stream ──────────────────────────────
        # Routes through services/cloud_tool_stream.stream_cloud_tools()
        _claude_tools_model = (
            SOLUTION_MODEL          if _model_hint == "solution" else
            CLAUDE_OPUS_48_MODEL    if _model_hint == "opus-4-8" else
            CLAUDE_OPUS_5_MODEL     if _model_hint == "opus-5" else
            CLAUDE_SONNET_5_MODEL   if _model_hint == "sonnet-5" else
            CLAUDE_HAIKU            if _model_hint == "haiku" else
            CLAUDE_PRIMARY_MODEL
        )
        _meta["model"] = _claude_tools_model

        try:
            from services.cloud_tool_stream import stream_cloud_tools
            _cl_finish_reason = "stop"

            for _tok in stream_cloud_tools(
                oai_msgs, req.tools, req.tool_choice, _claude_tools_model, "claude",
                max_tokens=64000, request_id=request_id,
            ):
                if isinstance(_tok, dict):
                    if "__stream_meta__" in _tok:
                        _sm = _tok["__stream_meta__"] or {}
                        _meta["in_tok"]  = _sm.get("in_tok",  _meta["in_tok"])
                        _meta["out_tok"] = _sm.get("out_tok", _meta["out_tok"])
                        _cl_finish_reason = _sm.get("finish_reason") or "stop"
                        continue
                    if "tool_call_delta" in _tok:
                        yield _c(None, tool_calls_delta=[_tok["tool_call_delta"]])
                    continue
                if isinstance(_tok, str) and _tok:
                    yield _c(_tok)

            yield _c(None, finish=_cl_finish_reason)
            logger.info(f"[IDE-Claude] stream done  in={_meta['in_tok']}  out={_meta['out_tok']}")

        except Exception as _e:
            logger.exception(
                f"[IDE-Claude] stream failed request_id={request_id} {repr(_e)[:1500]}"
            )
            yield _c("Error generating response", finish="stop")

    def _gateway_stream():
        """Yield tokens directly from the appropriate gateway."""
        nonlocal safe_question   # reassigned in RAG injection below; must be nonlocal
        from agents.compliance_engine import compliance_engine
        from models.model_router import model_router as _mr, TIER_SIMPLE, TIER_MEDIUM, TIER_COMPLEX, TIER_VISION, TIER_GEMINI, TIER_HAIKU

        # ── Gate 1: current user message ─────────────────────────
        findings_all = compliance_engine.validate_input(last_user).get("findings", [])
        blocked_types = [f["type"] for f in findings_all if f.get("blocked")]
        allowed_types = [f["type"] for f in findings_all if not f.get("blocked")]
        logger.info(f"[IDE] COMPLIANCE current-message (chat)")
        logger.info(f"[IDE]           status={'BLOCKED' if blocked_types else 'ALLOWED'}")
        logger.info(f"[IDE]           blocked_findings={blocked_types or 'none'}")
        logger.info(f"[IDE]           non-blocking_findings={allowed_types or 'none'}")
        if blocked_types:
            logger.warning(f"[IDE] *** REQUEST BLOCKED (current message) — {blocked_types} ***")
            yield f"[Request blocked by compliance policy: {', '.join(blocked_types)}]"
            return

        # ── Gate 2: conversation history ──────────────────────────
        # Scan prior user messages (COMPLIANCE_SCAN_HISTORY) and tool results
        # (COMPLIANCE_SCAN_TOOL_RESULTS) raw (pre-mask). Both default OFF — the
        # current turn is already gated above, so history scanning is opt-in.
        from core.config import COMPLIANCE_SCAN_HISTORY, COMPLIANCE_SCAN_TOOL_RESULTS
        history_blocked: list = []
        _seen_htypes: set = set()
        for _hi, _hm in enumerate(req.messages):
            _scan_this = (
                (_hm.role == "user" and COMPLIANCE_SCAN_HISTORY) or
                (_hm.role == "tool" and COMPLIANCE_SCAN_TOOL_RESULTS)
            )
            if _scan_this:
                _raw = _clean_ide_message(_hm.text()) if _hm.role == "user" else _hm.text()
                _chk = compliance_engine.validate_input(_raw)
                for _f in _chk.get("findings", []):
                    if _f.get("blocked") and _f["type"] not in _seen_htypes:
                        _seen_htypes.add(_f["type"])
                        history_blocked.append(_f["type"])
                        logger.warning(f"[IDE] HISTORY VIOLATION msg[{_hi}] role={_hm.role}  type={_f['type']}")
        if not COMPLIANCE_SCAN_HISTORY and not COMPLIANCE_SCAN_TOOL_RESULTS:
            logger.info("[IDE] COMPLIANCE SKIP gate2 reason=history+tool_scanning_disabled")
        if history_blocked:
            logger.warning(f"[IDE] *** REQUEST BLOCKED (conversation history) — {history_blocked} ***")
            yield f"[Request blocked by compliance policy in conversation history: {', '.join(history_blocked)}]"
            return
        logger.info(f"[IDE] COMPLIANCE conversation history (chat) — PASSED")

        # ── Incremental RAG injection (mirrors /ide/chat behaviour) ──────
        # Detect which indexed repo this question is about and prepend 2
        # chunks of codebase context so the LLM has grounded knowledge.
        # context_mode is implicitly "auto" for OpenAI-compat requests.
        #
        # IMPORTANT: use a local '_prompt' variable here rather than
        # reassigning 'safe_question'.  If this function ever assigns to
        # 'safe_question', Python marks it as a local throughout the entire
        # function body — which means the *reads* of safe_question above
        # (detect_repo, compliance gates) raise UnboundLocalError before the
        # assignment is reached.  Using a fresh local avoids that entirely.
        _prompt = safe_question  # start with the masked outer question
        # Skip RAG/codebase injection for browser-automation-agent turns. The
        # agent prompt embeds a live DOM snapshot and can contain repo-name tokens
        # (e.g. when browsing a GitLab page), which would make detect_repo match
        # and inject codebase chunks — corrupting/oversizing the prompt and causing
        # an upstream 400. Gate on BOTH the explicit header (_passthrough) and a
        # header-independent request-shape heuristic (the header may be stripped in
        # transit, leaving client_source=platform).
        _shape_match = looks_like_browser_agent_prompt(_prompt)
        _skip_rag = _passthrough or _shape_match
        _rag_repo = None if _skip_rag else detect_repo(_prompt)
        if _rag_repo:
            try:
                from models.hybrid_retriever import hybrid_retrieve_context as _hrc
                from models.classifier import classify_query_complexity as _cqc
                _rag_cplx = _cqc(_prompt)
                _ide_cplx = "simple" if _rag_cplx == "simple" else "medium"
                _rag_chunks = _hrc(_prompt, _rag_repo, complexity=_ide_cplx, max_chunks=2)
                if _rag_chunks:
                    _prompt = (
                        f"[Codebase context — {_rag_repo}]\n"
                        + "\n\n".join(_rag_chunks)
                        + f"\n\n[Question]\n{_prompt}"
                    )
                    logger.info(
                        f"[IDE] RAG injected  repo={_rag_repo}  chunks={len(_rag_chunks)}  "
                        f"complexity={_rag_cplx}→{_ide_cplx}"
                    )
            except Exception as _re:
                logger.debug(f"[IDE] RAG skipped: {_re}")
        elif _skip_rag:
            logger.info("[IDE] RAG skipped — browser-agent request (passthrough=%s, shape_match=%s)"
                        % (_passthrough, _shape_match))

        # Route: hint → vision → complexity/confidence (use masked text for routing)
        # Fix #33: on a browser-automation turn the prompt embeds a large DOM snapshot.
        # Complexity classification of that blob was mis-tiering the request to the
        # MEDIUM (OpenAI/GPT-5 Mini) tier even when the user had explicitly selected a
        # Claude model — silently downgrading their choice. So (a) honour an explicit
        # model hint verbatim, and (b) when there is NO explicit hint on a browser turn,
        # pin to the capable Claude tier instead of letting the DOM size pick a mini.
        _route_hint = _model_hint
        if _shape_match and not _route_hint:
            _route_hint = "claude"
            logger.info("[IDE] browser-agent turn with no explicit model — pinning to Claude "
                        "tier to avoid DOM-size complexity downgrade (#33)")
        decision = _mr.route(_prompt, model_hint=_route_hint)
        _mr.last_model_label = decision.model
        _mr.last_tier        = decision.tier
        # Use bare model ID for model_usages; _resolve_model_id handles "auto" fallback.
        _meta["model"]       = _resolve_model_id(_mr.last_model_id or decision.model)
        logger.info(f"[IDE] ROUTING   tier={decision.tier!r}  model={decision.model!r}  "
                    f"hint={decision.hint!r}  complexity={decision.complexity!r}  fallback={decision.fallback}")
        logger.info(f"[IDE] → SENDING TO MODEL (plain chat, no tools)")

        try:
            if decision.tier == TIER_MEDIUM:
                gw = _mr._get_openai()
                if gw:
                    yield from gw.generate(_prompt)
                    return
            if decision.tier == TIER_COMPLEX:
                gw = _mr._get_claude()
                if gw:
                    yield from gw.generate(_prompt)
                    return
            if decision.tier in (TIER_VISION, TIER_GEMINI):
                # Both explicit Gemini selection and vision-detected queries route here
                gw = _mr._get_gemini()
                if gw:
                    yield from gw.generate(_prompt)
                    return
            if decision.tier == TIER_HAIKU:
                gw = _mr._get_claude()
                if gw:
                    yield from gw.generate(_prompt, model=_CLAUDE_HAIKU)
                    return
            if decision.tier == TIER_SIMPLE:
                # Local in-house LLM — routes from the gateway server directly (no proxy)
                local = _mr._get_local()
                if local and local.available:
                    yield from local.generate(_prompt, model=_local_model_name, tier="simple")
                    return
            # Gateway unavailable or unknown tier — blocking fallback with model_router
            result = _mr.generate(_prompt, model_hint=_model_hint)
            logger.info(f"[IDE] ← RESPONSE (fallback) chars={len(result)}  preview={result[:120]!r}")
            yield result
            return
        except Exception as _e:
            logger.exception(
                f"[IDE] gateway stream failed request_id={request_id} {repr(_e)[:1500]}"
            )
            yield "Error generating response"

    # ── Non-streaming response ────────────────────────────────────
    if not req.stream:
        from fastapi.responses import JSONResponse as _JSONResponse
        if not _LLM_SEMAPHORE.acquire(timeout=_SEM_ACQUIRE_TIMEOUT):
            logger.warning(f"[IDE] NON-STREAM: semaphore timeout (busy) req_id={request_id}")
            raise HTTPException(status_code=503, detail="Server busy — too many concurrent requests")
        full_answer = ""
        try:
            for token in _gateway_stream():
                full_answer += token
        except Exception as _e:
            logger.exception(
                f"oai_completions (non-stream) request_id={request_id} {repr(_e)[:1500]}"
            )
            full_answer = "Error generating response"
        finally:
            _LLM_SEMAPHORE.release()
            _record_latency((time.time() - start_time) * 1000)
            _record_usage(full_answer)

        # Strip markdown code fences for json_schema / json_object clients
        # (e.g. Presenton's llmai library calls json.loads() directly on content)
        _rf2 = req.response_format
        if _rf2 and isinstance(_rf2, dict) and _rf2.get("type") in ("json_schema", "json_object"):
            import re as _re
            _stripped = full_answer.strip()
            _stripped = _re.sub(r"^```[a-z]*\s*", "", _stripped)
            _stripped = _re.sub(r"\s*```\s*$", "", _stripped.strip())
            full_answer = _stripped.strip()

        return {
            "id":      completion_id,
            "object":  "chat.completion",
            "created": created_ts,
            "model":   req.model,
            "choices": [{
                "index":         0,
                "message":       {"role": "assistant", "content": full_answer},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens":     _meta["in_tok"],
                "completion_tokens": _meta["out_tok"],
                "total_tokens":      _meta["in_tok"] + _meta["out_tok"],
            },
        }
        # Return explicit JSONResponse to guarantee Content-Type: application/json
        # and prevent any middleware from wrapping this as SSE/streaming.
        return _JSONResponse(content=_non_stream_body)

    # ── Streaming response ────────────────────────────────────────
    def oai_stream():
        full_answer = ""

        if not _LLM_SEMAPHORE.acquire(timeout=_SEM_ACQUIRE_TIMEOUT):
            busy = json.dumps({
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created_ts, "model": req.model,
                "choices": [{"index": 0, "delta": {"content": "Server busy — please retry."}, "finish_reason": "stop"}],
            })
            yield f"data: {busy}\n\ndata: [DONE]\n\n"
            return

        try:
            if req.tools:
                # ── Agent / tool-call mode ───────────────────────────────
                # Route to Claude for agentic tool-calling when the model hint
                # is "claude" (much better multi-step reasoning).
                # Fall back to OpenAI proxy for other hints/models.
                if _passthrough:
                    # Browser-agent: complete, drift-proof hint set (includes opus-4-8).
                    _use_claude = _model_hint in _CLAUDE_TOOL_HINTS
                    # Image turns MUST use the proxy — Claude stream drops image_url parts.
                    if _force_proxy_for_image:
                        _use_claude = False
                else:
                    # IDE path — unchanged, byte-identical to today.
                    _use_claude = _model_hint in ("claude", "solution", "haiku")
                _tool_gen = _tools_claude_stream() if _use_claude else _tools_proxy_stream()
                for raw_chunk in _tool_gen:
                    if not raw_chunk:
                        continue
                    full_answer += raw_chunk  # for usage accounting only
                    yield f"data: {raw_chunk}\n\n"
            else:
                # ── Plain chat mode ──────────────────────────────────────
                # Opening role delta so clients know speaker before first token
                _open_chunk = {
                    "id":      completion_id,
                    "object":  "chat.completion.chunk",
                    "created": created_ts,
                    "model":   req.model,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(_open_chunk)}\n\n"

                _token_count = 0
                for token in _gateway_stream():
                    if not token:
                        continue
                    full_answer += token
                    _token_count += 1
                    chunk = {
                        "id":      completion_id,
                        "object":  "chat.completion.chunk",
                        "created": created_ts,
                        "model":   req.model,
                        "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                logger.info(f"[IDE] ← RESPONSE (plain chat) tokens_streamed={_token_count}  "
                            f"total_chars={len(full_answer)}  "
                            f"preview={full_answer[:120]!r}{'...' if len(full_answer)>120 else ''}")

                # Final stop chunk
                stop_chunk = {
                    "id":      completion_id,
                    "object":  "chat.completion.chunk",
                    "created": created_ts,
                    "model":   req.model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(stop_chunk)}\n\n"

        except Exception as _e:
            logger.exception(
                f"oai_completions (stream) request_id={request_id} {repr(_e)[:1500]}"
            )
            err = json.dumps({
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created_ts, "model": req.model,
                "choices": [{"index": 0, "delta": {"content": "\nError generating response"}, "finish_reason": "stop"}],
            })
            yield f"data: {err}\n\n"

        finally:
            _LLM_SEMAPHORE.release()
            _record_latency((time.time() - start_time) * 1000)
            _record_usage(full_answer)

        # Emit a final usage chunk so KiloCode / Cline can update their token
        # counters.  This follows the OpenAI streaming spec: a chunk with
        # empty choices and a populated "usage" object, sent immediately before
        # the [DONE] sentinel.  _record_usage() has already run (in the finally
        # block above) and has populated _meta with real token counts.
        usage_chunk = {
            "id":      completion_id,
            "object":  "chat.completion.chunk",
            "created": created_ts,
            "model":   req.model,
            "choices": [],
            "usage": {
                "prompt_tokens":     _meta["in_tok"],
                "completion_tokens": _meta["out_tok"],
                "total_tokens":      _meta["in_tok"] + _meta["out_tok"],
            },
        }
        yield f"data: {json.dumps(usage_chunk)}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        oai_stream(),
        media_type="text/event-stream",
        headers={
            "X-Request-ID":  request_id,
            "Cache-Control": "no-cache",
            "Connection":    "keep-alive",
            "Access-Control-Expose-Headers": "X-Request-ID",
        },
    )


# ============================================================
# TRACE ENDPOINT
# ============================================================

# SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
# this endpoint previously had no auth dependency at all, exposing an
# internal request trace (spans, timings, internal call graph) for any
# request id to any anonymous caller.
# Fix: added `_caller: dict = Depends(require_role("admin"))` as a
# function parameter. `require_role("admin")` (auth/rbac.py) rejects the
# request with 401 if there's no valid JWT, and with 403 if the caller's
# role is below admin — so only admins can reach the handler body below.
# Same restriction applied to /traces and /traces/{request_id} below.
@_v1.get("/trace/{request_id}")
def trace(request_id, _caller: dict = Depends(require_role("admin"))):

    return {
        "request_id": request_id,
        "trace": get_trace(request_id)
    }


# ============================================================
# METRICS ENDPOINTS
#
# SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
# All four routes below previously carried no authentication or
# authorization dependency, making internal operational data (request
# counts, model usage, cost, cache-hit rates, compliance-block counts)
# readable by anyone who found the path, with no rate limiting either.
# Restricted to admin role only, using the same require_role() pattern
# already established for the /system/platform/* admin endpoints later in
# this file.
# ============================================================

# ── Shared DB-augmentation helper ────────────────────────────
# `telemetry_metrics` (core/telemetry.py) is an in-process, per-gunicorn-
# worker object — nothing in it is persisted. On a multi-worker deployment
# (deploy/ainxt.service: --workers 4; gunicorn.conf.py default 2×CPU+1, with
# max_requests=1000 periodically recycling workers) a `GET /metrics` call
# only ever sees ONE worker's counters, so requests_total/errors_total/etc.
# frequently read as near-zero even on a busy platform.
#
# This helper raises the in-memory counters to max(in_memory, db_value) from
# real, persisted tables so both /metrics and /metrics/prometheus report the
# same cross-worker-consistent numbers. Previously this logic lived only in
# get_prometheus_metrics() and did not cover cache_hits/compliance_blocks —
# those two fields were dead counters (no code path anywhere ever
# incremented them), so they read 0 unconditionally regardless of traffic.
# A short Redis cache bounds the DB load under the UI's 30s poll interval
# across however many tabs/screens call /metrics concurrently.
_METRICS_DB_AUGMENT_CACHE_KEY = "gateway:metrics:db_augment:v1"
_METRICS_DB_AUGMENT_CACHE_TTL = 20  # seconds — below the UI's 30s poll interval


def _augment_telemetry_from_db() -> None:
    """Best-effort: raise telemetry_metrics counters to real, persisted floors.

    Mutates the shared `telemetry_metrics` object in place. Each field group
    below runs in its OWN try/except — a failure in one (e.g. a bad query
    against a newly-added table) must never take down the others, since this
    single function now backs both /metrics and /metrics/prometheus.
    """
    try:
        _kv = get_kv(RDB_CACHE, decode_responses=True)
        if _kv.get(_METRICS_DB_AUGMENT_CACHE_KEY):
            # Another request already refreshed the counters within the TTL
            # window on this worker (or a shared cache backend) — skip the
            # round-trips but still let callers read the current (already
            # augmented) telemetry_metrics state.
            return
    except Exception:
        _kv = None

    # ── 1. model_usages / sdlc_runs aggregates (pre-existing logic) ────────
    try:
        from db.database import SessionLocal as _PrSL
        from db.models import ModelUsage as _MU, SDLCRun as _SR
        from sqlalchemy import func as _fn

        _db = _PrSL()
        try:
            _db_req      = _db.query(_fn.count(_MU.id)).scalar() or 0
            _db_agent    = _db.query(_fn.count(_MU.id)).filter(_MU.agent_id.isnot(None)).scalar() or 0
            _db_wf       = _db.query(_fn.count(_SR.id)).scalar() or 0
            _db_avg_lat  = _db.query(_fn.avg(_MU.latency_ms)).filter(_MU.latency_ms > 0).scalar() or 0.0
            # Agent successes: model_usages rows with an agent + positive latency
            _db_agent_ok = _db.query(_fn.count(_MU.id)).filter(
                _MU.agent_id.isnot(None), _MU.latency_ms > 0
            ).scalar() or 0
            # Errors: SDLC runs that ended in FAILED state
            _db_errors   = _db.query(_fn.count(_SR.id)).filter(
                _SR.state == "FAILED"
            ).scalar() or 0

            # Use the larger of (in-memory counter, DB row count) so a fresh
            # restart never wipes out the historical total.
            telemetry_metrics.requests_total      = max(telemetry_metrics.requests_total,      _db_req)
            telemetry_metrics.agent_executions    = max(telemetry_metrics.agent_executions,    _db_agent)
            telemetry_metrics.workflow_executions = max(telemetry_metrics.workflow_executions, _db_wf)
            telemetry_metrics.agent_success       = max(telemetry_metrics.agent_success,       _db_agent_ok)
            telemetry_metrics.errors_total        = max(telemetry_metrics.errors_total,        _db_errors)
            # Seed latency list from DB avg if in-process list is empty
            if not telemetry_metrics._latencies and _db_avg_lat > 0:
                telemetry_metrics._latencies = [_db_avg_lat]
            # Augment model-level counters from DB if empty
            if not telemetry_metrics.model_calls:
                rows = _db.query(_MU.model, _fn.count(_MU.id), _fn.sum(_MU.total_tokens), _fn.sum(_MU.cost_usd)
                                 ).group_by(_MU.model).all()
                for row in rows:
                    mdl = row[0] or "unknown"
                    telemetry_metrics.model_calls[mdl]    = row[1] or 0
                    telemetry_metrics.model_tokens[mdl]   = row[2] or 0
                    telemetry_metrics.model_cost_usd[mdl] = float(row[3] or 0)
        finally:
            _db.close()
    except Exception as _e:
        logger.warning(f"metrics: model_usages/sdlc_runs augmentation failed → {_e}")

    # ── 2. compliance_blocks from coach_event.compliance_flags ─────────────
    # coach_event.compliance_flags is a JSONB array, populated with real
    # violation types on a compliance block (see the ask-compliance-block
    # path around gateway.py's emit_coach_event(..., compliance_flags=...)
    # call). Empty array ("[]") = no violation. `ts` is indexed
    # (idx_coach_event_user_ts / idx_coach_event_dept_ts). Isolated in its
    # own try/except from group 1 above and group 3 below.
    try:
        from db.database import SessionLocal as _PrSL2
        from sqlalchemy import text as _mtext

        _db2 = _PrSL2()
        try:
            _db_compliance_blocks = _db2.execute(_mtext("""
                SELECT COUNT(*) FROM ainxt.coach_event
                WHERE compliance_flags IS NOT NULL
                  AND compliance_flags != '[]'::jsonb
                  AND ts >= NOW() - INTERVAL '1 day'
            """)).scalar() or 0
            telemetry_metrics.compliance_blocks = max(
                telemetry_metrics.compliance_blocks, _db_compliance_blocks
            )
        finally:
            _db2.close()
    except Exception as _e:
        logger.warning(f"metrics: compliance_blocks augmentation failed → {_e}")

    # ── 3. cache_hits from the LLM-bypass Redis counters ────────────────────
    # request_audit_log.cache_hit is NOT a usable source here — every current
    # write path (_write_request_audit's only caller, and both callers of
    # core.request_audit.record_audit) leaves it as "none": the redis-cache-hit
    # and semantic-cache-hit branches in ask_ai() return a StreamingResponse
    # directly and never call _write_request_audit at all, so the column is
    # never populated with a real cache-hit value by anything in the codebase.
    # The counters _record_bypass_metric() writes on every cache hit
    # (gateway.py's redis/semantic-cache branches) ARE real and already used
    # by GET /metrics/llm-bypass — reuse them here instead. Isolated in its
    # own try/except, independent of Postgres entirely.
    try:
        from datetime import datetime as _dt_ch, timedelta as _td_ch

        _cache_hit_total = 0
        for _d in range(7):
            _date = (_dt_ch.utcnow() - _td_ch(days=_d)).strftime("%Y%m%d")
            _cache_hit_total += int(_bypass_redis.get(f"ainxt:bypass:{_date}:redis") or 0)
            _cache_hit_total += int(_bypass_redis.get(f"ainxt:bypass:{_date}:semantic") or 0)
        telemetry_metrics.cache_hits = max(telemetry_metrics.cache_hits, _cache_hit_total)
    except Exception as _e:
        logger.warning(f"metrics: cache_hits augmentation failed → {_e}")

    if _kv is not None:
        try:
            _kv.set(_METRICS_DB_AUGMENT_CACHE_KEY, "1", ex=_METRICS_DB_AUGMENT_CACHE_TTL)
        except Exception:
            pass


@_v1.get("/metrics", tags=["observability"])
def get_metrics(_caller: dict = Depends(require_role("admin"))):
    """JSON metrics summary (legacy + telemetry).

    telemetry_metrics is per-worker/in-memory (see _augment_telemetry_from_db
    docstring above) — DB-augment it here so this endpoint, which the
    Analytics "System Health" strip and Platform Monitoring screen actually
    poll, reports real cross-worker numbers instead of raw per-worker zeros.

    Admin-only (AppSec finding — Information Disclosure): see security note
    above the METRICS ENDPOINTS section header.
    """
    _augment_telemetry_from_db()
    base = metrics.summary()
    base["telemetry"] = telemetry_metrics.to_json()
    return base


from fastapi.responses import PlainTextResponse as _PlainTextResponse

@_v1.get("/metrics/prometheus", response_class=_PlainTextResponse, tags=["observability"])
def get_prometheus_metrics(_caller: dict = Depends(require_role("admin"))):
    """Prometheus text-format metrics endpoint.
    In-memory counters reset on restart; augment with DB-backed values so the
    Monitoring screen always shows real, meaningful numbers.

    Admin-only (AppSec finding — Information Disclosure): see security note
    above the METRICS ENDPOINTS section header. NOTE: any external Prometheus
    scrape job hitting this path must be updated with a valid admin-role
    credential (e.g. bearer token) or its scrape will start failing with 401.
    """
    _augment_telemetry_from_db()

    return _PlainTextResponse(
        content=telemetry_metrics.to_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )

@_v1.get("/metrics/compression", tags=["observability"])
def get_compression_metrics(days: int = 7, _caller: dict = Depends(require_role("admin"))):
    """
    Phase 2 — Context compression telemetry.
    Returns per-source token reduction stats for the last N days.
    Sources: ide_session, ide_tool, sdlc_build, sdlc_test, rag_phase1, lingua_rag

    Admin-only (AppSec finding — Information Disclosure): see security note
    above the METRICS ENDPOINTS section header.
    """
    try:
        from core.compress_metrics import get_stats
        return get_stats(days=days)
    except Exception as exc:
        return {"error": str(exc), "days": days, "totals": {}, "daily": []}

# SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
# /traces and /traces/{request_id} previously had no auth dependency at
# all, exposing recent execution traces / telemetry spans (internal call
# graphs, timings) across the whole platform to any anonymous caller.
# Fix: added `_caller: dict = Depends(require_role("admin"))` as a
# function parameter to both routes below, so `require_role("admin")`
# rejects unauthenticated callers with 401 and non-admin callers with
# 403 before either handler body runs — same restriction as
# /trace/{request_id} above.
@_v1.get("/traces", tags=["observability"])
def list_traces(limit: int = 50, _caller: dict = Depends(require_role("admin"))):
    """List recent execution traces (telemetry spans)."""
    return {"traces": span_store.list_recent(limit)}


@_v1.get("/traces/{request_id}", tags=["observability"])
def get_request_trace(request_id: str, _caller: dict = Depends(require_role("admin"))):
    """Get all telemetry spans for a specific request."""
    return {
        "request_id": request_id,
        "spans": span_store.get_by_request(request_id),
        "trace": get_trace(request_id),
    }


@app.get("/observability/loki-probe", tags=["observability"])
def loki_probe(
    agent_id: str = "probe-agent",
    pipeline_stage: str = "observability",
    task_id: str = "loki-probe",
    correlation_id: str = "probe-correlation",
):
    """Emit a deterministic structured event to validate Promtail->Loki ingestion."""
    probe_ts = datetime.utcnow().isoformat() + "Z"
    bind_context(
        agent_id=agent_id,
        pipeline_stage=pipeline_stage,
        task_id=task_id,
        correlation_id=correlation_id,
    )
    try:
        logger.info(
            "loki_probe",
            probe=True,
            probe_timestamp=probe_ts,
            route="/observability/loki-probe",
        )
    finally:
        clear_bound_context()

    return {
        "status": "emitted",
        "event": "loki_probe",
        "probe_timestamp": probe_ts,
        "agent_id": agent_id,
        "pipeline_stage": pipeline_stage,
        "task_id": task_id,
        "correlation_id": correlation_id,
    }


# ============================================================
# LOCAL LLM MODEL DISCOVERY
# ============================================================

@_v1.get("/local-models", tags=["models"])
def get_local_models():
    """
    Return the list of models available on the Local LLM proxy.
    Used by the Chat UI to populate the model selector.
    Returns {"models": [...], "by_tier": {...}, "available": bool}
    """
    try:
        from gateway_local_llm import get_local_gateway
        gw = get_local_gateway()
        return {
            "models":    gw.list_models(),
            "by_tier":   gw.models_by_tier(),
            "available": gw.available,
        }
    except Exception as e:
        logger.warning(f"local-models endpoint error: {e}")
        return {"models": [], "by_tier": {}, "available": False}



@_v1.get("/all-models", tags=["models"])
def get_all_models(request: Request):
    """
    Return all available models grouped by provider.
    Used by Chat UI + CLI to show a comprehensive model selector.
    Auto = routing logic. Specific selection = bypass routing.

    Opus 4.8 is visible ONLY to CLI / IDE clients (client_source != "platform").
    The web Chat UI never sees it, regardless of ENABLE_CHAT_OPUS.

    Veo 3.1 is visible ONLY to the web Chat UI (client_source == "platform")
    when VEO_ENABLED=true. Per-user access is governed by the model governance
    tables (dept_model_permissions / user_model_permissions) — the same
    mechanism used for every other model. CLI / IDE never see it.
    """
    from core.model_registry import (
        CLAUDE_PRIMARY_MODEL, CLAUDE_HAIKU,
        OPENAI_CODING_MODEL, OPENAI_SIMPLE_MODEL,
        GEMINI_VISION_MODEL,
        GEMINI_TEXT_MODEL, GEMINI_CODING_LITE_MODEL, GEMINI_IMAGE_MODEL,
        GEMINI_TEXT_DISPLAY, GEMINI_CODING_LITE_DISPLAY, GEMINI_IMAGE_DISPLAY,
        VEO_MODEL, VEO_DISPLAY, VEO_ENABLED,
    )
    _show_opus_in_chat = _ENABLE_OPUS and _ENABLE_CHAT_OPUS
    # Opus 4.8: CLI/IDE only. ClientSourceMiddleware tags every request with
    # client_source in {"platform","cli","ide-vscode","ide-jetbrains","api"}.
    # "platform" = web Chat UI → suppress Opus 4.8.
    _cs = getattr(request.state, "client_source", "platform")
    _show_opus_48 = (
        _ENABLE_OPUS
        and _ENABLE_CLI_OPUS_48
        and _cs in ("cli", "ide-vscode", "ide-jetbrains", "api")
    )

    # Veo: visible in the web Chat UI picker when VEO_ENABLED=true.
    # Per-user access is controlled by model governance tables, exactly like
    # every other model — no additional per-user check needed here.
    _show_veo = VEO_ENABLED and _cs == "platform"

    # modelId = the full concrete model ID used by governance (dept_model_permissions.model_id).
    # id      = the short alias sent as the "model" hint in POST /ask.
    # The UI uses modelId to match against /model-governance/my-models (which returns full IDs)
    # so that governance filtering works correctly even when id != modelId (e.g. "claude" vs
    # "claude-sonnet-4-6").
    providers = [
        {
            "provider": "Auto",
            "models": [
                {"id": "auto", "modelId": "auto", "label": "Auto (Routing)", "hint": "auto"},
            ],
        },
        {
            "provider": "Anthropic (Claude)",
            "models": [
                {"id": "claude",    "modelId": CLAUDE_PRIMARY_MODEL,  "label": f"{_CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL})", "hint": "claude"},
                {"id": "haiku",     "modelId": CLAUDE_HAIKU,          "label": f"{_CLAUDE_HAIKU_DISPLAY} ({CLAUDE_HAIKU})",           "hint": "haiku"},
                # Opus is CLI/IDE-only — ENABLE_CHAT_OPUS=false keeps it out of the web Chat picker.
                *([
                      {"id": "opus",     "modelId": _CLAUDE_OPUS,    "label": f"{_CLAUDE_OPUS_DISPLAY} ({_CLAUDE_OPUS})",         "hint": "opus"},
                  ] if _show_opus_in_chat else []),
                # Opus 4.8 — CLI/IDE only (never shown in web Chat picker).
                *([
                      {"id": "opus-4-8", "modelId": _CLAUDE_OPUS_48, "label": f"{_CLAUDE_OPUS_48_DISPLAY} ({_CLAUDE_OPUS_48})", "hint": "opus-4-8"},
                  ] if _show_opus_48 else []),
                # Opus 5 — CLI/IDE opt-in only (never shown in web Chat picker).
                *([
                      {"id": "opus-5", "modelId": _CLAUDE_OPUS_5, "label": f"{_CLAUDE_OPUS_5_DISPLAY} ({_CLAUDE_OPUS_5})", "hint": "opus-5"},
                  ] if (_ENABLE_CLI_OPUS_5 and _cs != "platform") else []),
                # Sonnet 5 — available on ALL channels (web Chat, CLI, IDE).
                # Only gated by the global ENABLE_SONNET_5 kill-switch.
                *([
                      {"id": "sonnet-5", "modelId": _CLAUDE_SONNET_5, "label": f"{_CLAUDE_SONNET_5_DISPLAY} ({_CLAUDE_SONNET_5})", "hint": "sonnet-5"},
                  ] if _ENABLE_SONNET_5 else []),
            ],
        },
        {
            "provider": "OpenAI",
            "models": [
                {"id": "gpt",  "modelId": OPENAI_CODING_MODEL,  "label": f"{_OPENAI_CODING_DISPLAY} ({OPENAI_CODING_MODEL})",  "hint": "gpt"},
                {"id": "mini", "modelId": OPENAI_SIMPLE_MODEL,  "label": f"{_OPENAI_SIMPLE_DISPLAY} ({OPENAI_SIMPLE_MODEL})",  "hint": "mini"},
                {"id": "deep", "modelId": _OPENAI_LATEST,       "label": f"{_OPENAI_LATEST_DISPLAY} ({_OPENAI_LATEST})",       "hint": "deep"},
                # GPT-5.6 Tera — high-capacity variant, Chat + CLI.
                *([
                      {"id": "tera", "modelId": _OPENAI_TERA, "label": f"{_OPENAI_TERA_DISPLAY} ({_OPENAI_TERA})", "hint": "tera"},
                  ] if _ENABLE_GPT56_TERA else []),
                # GPT-5.6 Luna — efficient variant, Chat + CLI.
                *([
                      {"id": "luna", "modelId": _OPENAI_LUNA, "label": f"{_OPENAI_LUNA_DISPLAY} ({_OPENAI_LUNA})", "hint": "luna"},
                  ] if _ENABLE_GPT56_LUNA else []),
            ],
            # deep research models excluded — accessible only via POST /v1/responses
        },
        {
            # Gemini 3.x split — image model also handles /image and vision-keyword auto-routing
            "provider": "Google (Gemini)",
            "models": [
                {"id": GEMINI_TEXT_MODEL,        "modelId": GEMINI_TEXT_MODEL,        "label": f"{GEMINI_TEXT_DISPLAY} ({GEMINI_TEXT_MODEL})",               "hint": GEMINI_TEXT_MODEL},
                {"id": GEMINI_CODING_LITE_MODEL, "modelId": GEMINI_CODING_LITE_MODEL, "label": f"{GEMINI_CODING_LITE_DISPLAY} ({GEMINI_CODING_LITE_MODEL})", "hint": GEMINI_CODING_LITE_MODEL},
                {"id": GEMINI_IMAGE_MODEL,       "modelId": GEMINI_IMAGE_MODEL,       "label": f"{GEMINI_IMAGE_DISPLAY} ({GEMINI_IMAGE_MODEL})",             "hint": GEMINI_IMAGE_MODEL},
                # Veo: chat-UI-only, per-user allowlisted. "modality": "video" tells the UI
                # to route to /chat/video-generate and render a <video> tag.
                *([
                      {"id": VEO_MODEL, "modelId": VEO_MODEL, "label": f"{VEO_DISPLAY} ({VEO_MODEL})", "hint": VEO_MODEL, "modality": "video"},
                  ] if _show_veo else []),
            ],
        },
    ]

    # Append in-house hosted models from the local LLM proxy
    try:
        from gateway_local_llm import get_local_gateway as _get_local_gw_am
        _local_list = _get_local_gw_am().list_models()
        if _local_list:
            providers.append({
                "provider": "Local (In-house)",
                "models": [
                    {"id": f"local:{m}", "modelId": f"local:{m}", "label": f"Local: {m}", "hint": f"local:{m}"}
                    for m in _local_list
                ],
            })
    except Exception:
        pass

    # ── Price tier stamping (single source of truth for the UI) ──────────────
    # Third-party vendor models (Anthropic / OpenAI / Google) are billed →
    # "paid". Everything else (local / in-house) is "free". "Auto" is routing,
    # not a billable model, so it carries no tier. The UI reads this field and
    # must NOT hardcode any tier mapping of its own.
    _PAID_PROVIDER_KEYS = ("anthropic", "openai", "google", "gemini", "claude")
    for _grp in providers:
        _pname = (_grp.get("provider") or "").lower()
        if _pname == "auto":
            _tier = None
        elif any(_k in _pname for _k in _PAID_PROVIDER_KEYS):
            _tier = "paid"
        else:
            _tier = "free"
        for _mdl in _grp.get("models", []):
            if _tier and "tier" not in _mdl:
                _mdl["tier"] = _tier

    return {"providers": providers}


# ============================================================
# OPENAI RESPONSES API  (deep-research + GPT-5.4)
# POST /v1/responses  /responses
#
# Mirrors the OpenAI Responses API so callers can use:
#   client = OpenAI(base_url="http://localhost:8000", api_key=...)
#   client.responses.create(model="gpt-5.4", input="...")
#   client.responses.create(model="o4-mini-deep-research", input="...",
#                           tools=[{"type": "web_search_preview"}])
#
# tools is MANDATORY for deep-research models.
# ============================================================

class _OAIResponsesRequest(BaseModel):
    model:             str
    input:             Union[str, List[Any]]
    stream:            bool          = True
    tools:             Optional[List[Any]] = None
    max_output_tokens: Optional[int]  = None
    temperature:       Optional[float] = None


@_v1.post("/v1/responses", tags=["ai"])
@_v1.post("/responses", tags=["ai"])
def openai_responses(
        req: _OAIResponsesRequest,
        request: Request,
        authorization: Optional[str] = _Header(default=None),
):
    """OpenAI Responses API — routes gpt-5.4 and deep-research models.

    tools is mandatory when model is o4-mini-deep-research or o3-deep-research.
    Streams back OpenAI-compatible SSE events.
    """
    import time as _time_r
    import json as _json_r

    # Prefer client-supplied x-client-request-id for end-to-end tracing
    request_id = (request.headers.get("x-client-request-id") or "").strip() or str(uuid.uuid4())
    set_request_id(request_id)
    set_correlation_id(request_id)  # unconditional: avoid stale value on reused thread
    start_time = _time_r.time()

    # ── Auth ──────────────────────────────────────────────────────
    _user_id = None
    if authorization and authorization.lower().startswith("bearer "):
        _token = authorization[7:].strip()
        try:
            from auth.jwt_handler import decode_token as _decode_r
            _payload = _decode_r(_token)
            if _payload:
                _user_id = _payload.get("sub") or _payload.get("email")
        except Exception:
            pass
        if not _user_id:
            try:
                from auth.api_key_auth import is_api_key as _is_ak_r, resolve_api_key as _res_ak_r
                if _is_ak_r(_token):
                    _kp = _res_ak_r(_token)
                    if _kp:
                        _user_id = _kp["sub"]
            except Exception:
                pass
    if not _user_id:
        from fastapi.responses import JSONResponse as _JR_r
        return _JR_r(
            status_code=401,
            content={"error": {"message": "Valid JWT or platform API key required.", "type": "invalid_request_error", "code": "unauthorized"}},
        )

    # ── Validate tools mandatory for deep-research models ─────────
    if req.model in _DEEP_RESEARCH_MODELS and not req.tools:
        from fastapi.responses import JSONResponse as _JR_dr
        return _JR_dr(
            status_code=422,
            content={"error": {
                "message": f"tools is required for deep-research model '{req.model}'. "
                           f"Pass at minimum: tools=[{{\"type\": \"web_search_preview\"}}]",
                "type": "invalid_request_error", "code": "missing_tools",
            }},
        )

    # ── Compliance on input ───────────────────────────────────────
    # Scan the CURRENT user turn always. When COMPLIANCE_SCAN_HISTORY is OFF
    # (default) and input is a multi-turn list, narrow the scan to the last turn
    # only so prior turns are not re-scanned (mirrors messages_compat windowing).
    from agents.compliance_engine import compliance_engine as _ce_r
    from core.config import COMPLIANCE_SCAN_HISTORY as _SCAN_HIST_R
    if isinstance(req.input, str):
        _raw_input = req.input
    elif COMPLIANCE_SCAN_HISTORY:
        _raw_input = " ".join(
            m.get("content", "") if isinstance(m, dict) else str(m) for m in req.input
        )
    else:
        _last = req.input[-1] if req.input else ""
        _raw_input = _last.get("content", "") if isinstance(_last, dict) else str(_last)
    _chk = _ce_r.validate_input(_raw_input)
    if _chk.get("blocked"):
        from fastapi.responses import JSONResponse as _JR_c
        return _JR_c(
            status_code=400,
            content={"error": {"message": "Request blocked: PCI/DSS compliance violation.", "type": "compliance_error"}},
        )

    # Apply the redaction. `_chk` was previously read for `blocked` only and `redacted_text` was
    # discarded, so `req.input` went to the provider raw at every send site below (payload/_kwargs,
    # streaming and non-streaming). The OUTPUT side of this handler was already redacted, which
    # made the asymmetry easy to miss. Each element is redacted individually rather than
    # substituting the joined `_raw_input`, so the message structure is preserved.
    if isinstance(req.input, str):
        req.input = _chk.get("redacted_text") or req.input
    else:
        for _m in req.input:
            if isinstance(_m, dict) and isinstance(_m.get("content"), str):
                _m["content"] = (
                    _ce_r.validate_input(_m["content"]).get("redacted_text") or _m["content"]
                )

    # ── Budget gate ───────────────────────────────────────────────
    try:
        from store.budget_store import check_budget as _chk_budget_r
        _budget = _chk_budget_r(_user_id)
        if _budget.get("allowed") is not True:
            from fastapi.responses import JSONResponse as _JR_b
            return _JR_b(
                status_code=429,
                content={"error": {"message": _budget.get("reason", "Budget exhausted"), "type": "insufficient_quota", "code": "BUDGET_EXCEEDED"}},
            )
    except Exception:
        pass

    _proxy_url = os.getenv("LLM_PROXY_URL", "").rstrip("/")

    # ── Non-streaming ─────────────────────────────────────────────
    if not req.stream:
        if _proxy_url:
            import httpx as _httpx_r
            from core.proxy_tool_use import llm_proxy_headers as _lph_r
            payload = {"model": req.model, "input": req.input, "stream": False}
            if req.tools:             payload["tools"] = req.tools
            if req.max_output_tokens: payload["max_output_tokens"] = req.max_output_tokens
            try:
                _hc = _get_proxy_client()
                resp = _hc.post(f"{_proxy_url}/llm/responses", json=payload,
                                headers=_lph_r(extra={"X-Request-ID": request_id}))
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                from fastapi.responses import JSONResponse as _JR_pe
                return _JR_pe(status_code=502, content={"error": {"message": str(exc)}})
        else:
            try:
                from gateway_openai import openai_gateway as _oai_gw_r
                client = _oai_gw_r.client
                _kwargs = {"model": req.model, "input": req.input}
                if req.tools:             _kwargs["tools"] = req.tools
                if req.max_output_tokens: _kwargs["max_output_tokens"] = req.max_output_tokens
                _resp = client.responses.create(**_kwargs)
                _usage = getattr(_resp, "usage", None)
                data = {
                    "output_text": _resp.output_text,
                    "in_tok":  getattr(_usage, "input_tokens",  0) or 0 if _usage else 0,
                    "out_tok": getattr(_usage, "output_tokens", 0) or 0 if _usage else 0,
                }
            except Exception as exc:
                from fastapi.responses import JSONResponse as _JR_de
                return _JR_de(status_code=502, content={"error": {"message": str(exc)}})

        # Output compliance disabled — output returned as-is for real-time streaming.
        # Re-enable by uncommenting below and replacing output_text reference.
        # _out_chk = _ce_r.validate_output(data.get("output_text", ""))
        # output_text = _out_chk.get("redacted_text", data.get("output_text", ""))
        return {
            "id":          f"resp-{request_id[:8]}",
            "object":      "response",
            "model":       req.model,
            "output_text": data.get("output_text", ""),
            "usage":       {"input_tokens": data.get("in_tok", 0), "output_tokens": data.get("out_tok", 0)},
        }

    # ── Streaming ─────────────────────────────────────────────────
    response_id   = f"resp-{request_id[:8]}"
    created_ts    = int(_time_r.time())
    _meta_r: dict = {"out_tok": 0, "in_tok": 0, "output_text": ""}

    def _stream_responses():
        nonlocal _meta_r

        if _proxy_url:
            # Forward to llm_proxy /llm/responses as ndjson, re-emit as SSE
            payload = {"model": req.model, "input": req.input, "stream": True}
            if req.tools:             payload["tools"] = req.tools
            if req.max_output_tokens: payload["max_output_tokens"] = req.max_output_tokens
            try:
                from models.model_router import _get_proxy_client as _gpc_r
                from core.proxy_tool_use import llm_proxy_headers as _lph_rs
                _hc = _gpc_r()
                with _hc.stream("POST", f"{_proxy_url}/llm/responses", json=payload,
                                headers=_lph_rs(extra={"X-Request-ID": request_id})) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        try:
                            obj = _json_r.loads(line)
                        except Exception:
                            continue
                        if "error" in obj:
                            yield f"data: {_json_r.dumps({'type': 'error', 'message': obj['error']})}\n\n"
                            return
                        if "delta" in obj:
                            _meta_r["output_text"] += obj["delta"]
                            yield f"data: {_json_r.dumps({'type': 'response.output_text.delta', 'delta': obj['delta']})}\n\n"
                        if "output_text" in obj:
                            _meta_r["in_tok"]  = obj.get("in_tok", 0)
                            _meta_r["out_tok"] = obj.get("out_tok", 0)
            except Exception as exc:
                yield f"data: {_json_r.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        else:
            # Direct path — call OpenAI Responses API locally
            try:
                from gateway_openai import openai_gateway as _oai_gw_rs
                _kwargs = {"model": req.model, "input": req.input}
                if req.tools:             _kwargs["tools"] = req.tools
                if req.max_output_tokens: _kwargs["max_output_tokens"] = req.max_output_tokens
                with _oai_gw_rs.client.responses.stream(**_kwargs) as stream:
                    for event in stream:
                        etype = getattr(event, "type", "")
                        if etype == "response.output_text.delta":
                            delta = getattr(event, "delta", "")
                            if delta:
                                _meta_r["output_text"] += delta
                                yield f"data: {_json_r.dumps({'type': 'response.output_text.delta', 'delta': delta})}\n\n"
                    final = stream.get_final_response()
                    _usage = getattr(final, "usage", None)
                    _meta_r["in_tok"]  = getattr(_usage, "input_tokens",  0) or 0 if _usage else 0
                    _meta_r["out_tok"] = getattr(_usage, "output_tokens", 0) or 0 if _usage else 0
            except Exception as exc:
                yield f"data: {_json_r.dumps({'type': 'error', 'message': str(exc)})}\n\n"
                return

        # Output compliance disabled — output returned as-is for real-time streaming.
        # Re-enable by uncommenting below and replacing _meta_r["output_text"] with _safe_out.
        # _out_chk = _ce_r.validate_output(_meta_r["output_text"])
        # _safe_out = _out_chk.get("redacted_text", _meta_r["output_text"])

        # Final completed event
        yield f"data: {_json_r.dumps({'type': 'response.completed', 'response': {'id': response_id, 'model': req.model, 'output_text': _meta_r['output_text'], 'usage': {'input_tokens': _meta_r['in_tok'], 'output_tokens': _meta_r['out_tok']}}})}\n\n"
        yield "data: [DONE]\n\n"

        # Budget increment
        try:
            from store.budget_store import increment_usage as _inc_r
            from core.model_registry import MODEL_COST_PER_1M as _costs
            _rate = _costs.get(req.model, (2.0, 8.0))
            _cost = (_meta_r["in_tok"] * _rate[0] + _meta_r["out_tok"] * _rate[1]) / 1_000_000
            _inc_r(_user_id, tokens=_meta_r["in_tok"] + _meta_r["out_tok"], cost_usd=_cost)
        except Exception:
            pass

    from fastapi.responses import StreamingResponse as _SR_r
    return _SR_r(_stream_responses(), media_type="text/event-stream")


# ============================================================
# HEALTH
# ============================================================

class _CodebaseSearchReq(BaseModel):
    query:      str
    repo:       Optional[str] = None     # if None, gateway tries to detect from query
    max_chunks: int           = 6
    complexity: Optional[str] = "medium"

@app.post("/codebase/search", tags=["codebase"])
async def codebase_search(req: _CodebaseSearchReq, http: Request, user_payload=Depends(_require_auth)):
    """
    Semantic + BM25 search over indexed codebases. Returns the same chunk set
    the orchestrator would pull, but exposed as a first-class tool the CLI can
    call without going through /ask.
    Used by the ainxt-cli `semantic_search` tool.
    """
    from models.hybrid_retriever import hybrid_retrieve_context as _hrc
    try:
        _repo = req.repo or detect_repo(req.query) or ""
        if not _repo:
            return {"chunks": [], "repo": None, "note": "no indexed repo detected — pass {repo: 'owner/name'} to scope"}
        _cplx = req.complexity if req.complexity in ("simple", "medium", "complex") else "medium"
        _n    = max(1, min(req.max_chunks, 20))
        _chunks = _hrc(req.query, _repo, complexity=_cplx, max_chunks=_n) or []
        return {"chunks": _chunks, "repo": _repo, "count": len(_chunks)}
    except Exception as e:
        logger.warning(f"[codebase/search] error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health", tags=["observability"])
def health():
    checks = {}
    # Critical failures (postgres, redis) → "unhealthy"
    # Non-critical failures (ollama, embed_svc) → "degraded"
    _critical_fail = False
    _non_critical_fail = False

    # ── Postgres check (critical) ────────────────────────────────
    try:
        from db.database import SessionLocal as _HL_SL
        from sqlalchemy import text as _sql_text
        _hdb = _HL_SL()
        try:
            _hdb.execute(_sql_text("SELECT 1"))
            checks["postgres"] = "ok"
            checks["database"] = "connected"   # backward compat alias
        finally:
            _hdb.close()
    except Exception as _he:
        checks["postgres"] = f"error: {_he}"
        checks["database"] = f"error: {_he}"   # backward compat alias
        _critical_fail = True

    # ── Redis check (critical, legacy alias for dashboards) ──────
    try:
        redis_client.ping()
        checks["redis"] = "ok"
    except Exception as _re:
        checks["redis"] = f"error: {_re}"
        _critical_fail = True

    # ── KV per-DB check ───────────────────────────────────────────
    # Pings every logical DB through its configured backend. A REDIS
    # failure is already covered by the legacy `redis` field above;
    # this section additionally surfaces per-logical-DB detail.
    # The helper is defined in core.kv.health so it can be unit-tested
    # without importing the full FastAPI app.
    try:
        from core.kv import kv_health_status
        _kv_status = kv_health_status()
        checks["kv"] = _kv_status
        # Per-DB failures are reported but not escalated: a Redis outage is
        # already accounted for by the legacy `redis` probe above, so counting
        # it again here would double-weight the same fault.
    except Exception as _kvbe:
        checks["kv"] = f"error: {_kvbe}"

    # ── Embed service check (non-critical) ───────────────────────
    try:
        import httpx as _httpx
        from core.config import EMBED_SVC_URL as _EMBED_SVC_URL
        _r = _httpx.get(f"{_EMBED_SVC_URL}/health", timeout=2.0)
        if _r.status_code == 200:
            checks["embed_svc"] = "ok"
        else:
            checks["embed_svc"] = f"degraded: HTTP {_r.status_code}"
            _non_critical_fail = True
    except Exception as _ee:
        checks["embed_svc"] = f"error: {_ee}"
        _non_critical_fail = True

    # ── Ollama check (non-critical) ──────────────────────────────
    try:
        import httpx as _httpx
        from core.config import OLLAMA_URL as _OLLAMA_URL, loopback_tls_verify as _lb_verify
        _r = _httpx.get(
            f"{_OLLAMA_URL}/api/tags", timeout=2.0, verify=_lb_verify(_OLLAMA_URL)
        )
        if _r.status_code == 200:
            checks["ollama"] = "ok"
        else:
            checks["ollama"] = f"degraded: HTTP {_r.status_code}"
            _non_critical_fail = True
    except Exception as _oe:
        checks["ollama"] = f"error: {_oe}"
        _non_critical_fail = True

    # ── Docker sandbox check (non-critical) ─────────────────────
    try:
        from sandbox.docker_executor import docker_executor as _de
        if _de.is_available():
            _img_status = _de.verify_images()
            _missing = [img for img, ok in _img_status.items() if not ok]
            if _missing:
                checks["docker"] = f"connected, missing images: {', '.join(_missing)}"
                _non_critical_fail = True
            else:
                checks["docker"] = "connected, all images cached"
        else:
            checks["docker"] = "unavailable (fallback: subprocess executor)"
            _non_critical_fail = True
    except Exception as _dke:
        checks["docker"] = f"error: {_dke}"
        _non_critical_fail = True

    # ── Circuit breakers ─────────────────────────────────────────
    try:
        from core.circuit_breaker import all_breaker_states as _abs
        _bks = _abs()
        _open = [b["name"] for b in _bks if b.get("state") == "OPEN"]
        checks["circuit_breakers"] = f"{len(_bks)} monitored, {len(_open)} open"
        if _open:
            checks["open_breakers"] = _open
        if len(_open) >= 2:
            _non_critical_fail = True
    except Exception:
        pass

    # ── ainxt-injection-svc check (non-critical) ────────────────
    _inj_url = os.getenv("INJECTION_SVC_URL", "").rstrip("/")
    if _inj_url:
        try:
            import httpx as _httpx_h
            from core.config import injection_scan_verify as _inj_verify_h
            _inj_r = _httpx_h.get(f"{_inj_url}/health", timeout=2.0, verify=_inj_verify_h())
            if _inj_r.status_code == 200:
                _inj_d = _inj_r.json()
                checks["injection_svc"] = f"ok (mode={_inj_d.get('mode', '?')})"
            else:
                checks["injection_svc"] = f"degraded: HTTP {_inj_r.status_code}"
                _non_critical_fail = True
        except Exception as _inj_he:
            checks["injection_svc"] = f"error: {_inj_he}"
            _non_critical_fail = True
    else:
        checks["injection_svc"] = "not configured (INJECTION_SVC_URL not set)"

    # ── Overall status ───────────────────────────────────────────
    if _critical_fail:
        overall = "unhealthy"
    elif _non_critical_fail:
        overall = "degraded"
    else:
        overall = "healthy"

    return {
        "status": overall,
        "time":   datetime.utcnow().isoformat(),
        "checks": checks,
    }


# ============================================================
# SANDBOX HEALTH ENDPOINT
# ============================================================

@app.get("/sandbox/health", tags=["observability"])
def sandbox_health():
    """
    Returns Docker daemon status and per-image cache state.
    The AiNxt services (gateway, workers) run under pm2 — NOT in Docker.
    Docker is used only to execute AI-generated code snippets in isolation.
    Each execution spins up one ephemeral container and destroys it immediately.
    """
    from sandbox.docker_executor import docker_executor as _de, LANGUAGE_CONFIG, SubprocessExecutor

    result = {
        "architecture": {
            "ainxt_services": "pm2 (gateway :8000, embed_svc :8001, RQ workers)",
            "code_execution": "Docker — ephemeral containers, one per execution, auto-destroyed",
            "fallback":       "SubprocessExecutor (Python only, 30s timeout, no Docker required)",
        },
        "docker": {},
        "images": {},
        "executor_in_use": None,
    }

    docker_available = _de.is_available()
    result["docker"]["daemon"] = "connected" if docker_available else "unavailable"

    if docker_available:
        result["executor_in_use"] = "DockerExecutor"
        img_status = _de.verify_images()
        result["images"] = {
            img: ("cached" if ok else "NOT cached — will auto-pull on first use")
            for img, ok in img_status.items()
        }
        # Languages and their mapped images
        result["language_image_map"] = {
            lang: cfg["image"] for lang, cfg in LANGUAGE_CONFIG.items()
        }
        result["limits"] = {
            "timeout_seconds": 60,
            "memory":          "512m",
            "cpu_quota":       "50% of 1 core (cpu_quota=50000)",
            "network":         "disabled",
            "privileges":      "no-new-privileges",
            "filesystem":      f"temp dir only — /tmp/{config.SANDBOX_PREFIX}{{uuid}}/",
        }
    else:
        result["executor_in_use"] = "SubprocessExecutor (fallback)"
        result["images"] = {"note": "Docker unavailable — subprocess used for Python only"}

    return result


# ============================================================
# MEMORY RECENT ENDPOINT (CLI /memory command)
# ============================================================

@app.get("/memory/recent", tags=["memory"])
def memory_recent(limit: int = 10, current_user: dict = Depends(_require_auth)):
    """
    Returns the most recently accessed semantic memories (L3).
    Used by the CLI /memory command to show the user what the backend knows.
    """
    try:
        from store.semantic_cache import get_semantic_memory as _gsm
        from db.database import get_db as _get_db
        import sqlalchemy as _sa

        db = next(_get_db())
        rows = db.execute(_sa.text(
            "SELECT type, summary, confidence, hit_count, last_used, scope_type "
            "FROM ainxt.semantic_memory "
            "ORDER BY last_used DESC "
            "LIMIT :limit"
        ), {"limit": min(limit, 50)}).fetchall()
        db.close()

        return [
            {
                "type":       row[0],
                "summary":    row[1],
                "confidence": round(float(row[2]), 3),
                "hit_count":  row[3],
                "last_used":  row[4].isoformat() if row[4] else None,
                "scope_type": row[5],
            }
            for row in rows
        ]
    except Exception as e:
        logger.warning(f"memory_recent failed: {e}")
        return []


# ============================================================
# LLM BYPASS METRICS ENDPOINT
#
# SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
# This route previously carried no authentication or authorization
# dependency. Restricted to admin only — same rationale/pattern as the
# METRICS ENDPOINTS section above.
# ============================================================

@app.get("/metrics/llm-bypass", tags=["observability"])
def llm_bypass_metrics(days: int = 7, _caller: dict = Depends(require_role("admin"))):
    """
    Returns daily LLM bypass rate breakdown for the last N days.
    Sources: redis (L1 exact cache), semantic (L2 similarity cache), llm (full inference).
    Bypass rate = (redis + semantic) / total × 100

    Admin-only (AppSec finding — Information Disclosure).
    """
    from datetime import datetime as _dt, timedelta as _td

    sources   = ("redis", "semantic", "llm")
    daily     = []
    total_all = {s: 0 for s in sources}

    for d in range(days):
        date = (_dt.utcnow() - _td(days=d)).strftime("%Y%m%d")
        row  = {"date": date}
        day_total = 0
        for src in sources:
            try:
                val = int(_bypass_redis.get(f"ainxt:bypass:{date}:{src}") or 0)
            except Exception:
                val = 0
            row[src]         = val
            total_all[src]  += val
            day_total       += val
        row["total"]      = day_total
        bypassed          = row["redis"] + row["semantic"]
        row["bypass_pct"] = round(bypassed / day_total * 100, 1) if day_total else 0.0
        daily.append(row)

    grand_total  = sum(total_all.values())
    grand_bypass = total_all["redis"] + total_all["semantic"]
    return {
        "period_days":      days,
        "totals":           {**total_all, "total": grand_total},
        "bypass_rate_pct":  round(grand_bypass / grand_total * 100, 1) if grand_total else 0.0,
        "daily":            daily,
        "interpretation": {
            "redis":    "L1 exact cache hits — zero LLM cost",
            "semantic": "L2 similarity cache hits — zero LLM cost",
            "llm":      "Full LLM inference — cost incurred",
            "target":   "≥ 30% bypass rate indicates healthy cache utilisation",
        },
    }


# ============================================================
# PLATFORM KILL-SWITCH ADMIN ENDPOINTS (Fix 5)
# POST /system/platform/disable  — suspend all /ask requests
# POST /system/platform/enable   — re-enable platform
# GET  /system/platform/status   — check current status
#
# SECURITY (SEC-2026-0142 — OWASP API5:2023 BFLA / CWE-285 / CWE-862):
# All three routes are ADMIN ONLY. They previously carried no authorization
# guard, which let any authenticated non-admin user take the entire platform
# offline for every tenant (self-service, platform-wide DoS).
#
# Access is enforced by _require_platform_admin below, which combines:
#   1. ENABLE_PLATFORM_KILLSWITCH_API — feature gate; when false the whole
#      API surface returns 404, including for administrators.
#   2. get_current_user  — 401 when the caller is unauthenticated.
#   3. role == "admin"   — 403 "Unauthorized access" for every other role.
# ============================================================

def _require_platform_admin(_caller: dict = Depends(_require_auth)) -> dict:
    """Authorize a platform kill-switch call: feature-gated + admin only.

    Why this is a local dependency instead of ``auth.rbac.require_admin_flag``:
      - The API must be switchable per environment via
        ENABLE_PLATFORM_KILLSWITCH_API, which the shared helper does not do.
      - These routes return a deliberately generic "Unauthorized access"
        message that does not disclose *which* role would grant access.
        require_admin_flag is shared by 14+ routes across admin_router and
        others, so changing its wording there would be an unrelated,
        repo-wide behaviour change.

    Ordering note: the feature gate is evaluated BEFORE the role check so a
    disabled API looks identical (404) to callers of every privilege level and
    does not advertise that an admin-only endpoint exists here.
    """
    if not _ENABLE_PLATFORM_KILLSWITCH_API:
        raise HTTPException(status_code=404, detail="Not Found")
    if _caller.get("role") != "admin":
        # Generic message on purpose — do not leak the required role.
        raise HTTPException(status_code=403, detail="Unauthorized access")
    return _caller


@_v1.post("/system/platform/disable", tags=["observability"])
def platform_disable(
    reason: str = "Suspended by administrator",
    _caller: dict = Depends(_require_platform_admin),
):
    """Admin only: suspend the platform (sets platform:disabled=1 in Redis)."""
    redis_client.set("platform:disabled", "1")
    redis_client.set("platform:disabled_reason", reason)
    logger.warning(f"PLATFORM DISABLED — reason: {reason}")
    return {"status": "disabled", "reason": reason}


@_v1.post("/system/platform/enable", tags=["observability"])
def platform_enable(_caller: dict = Depends(_require_platform_admin)):
    """Admin only: re-enable the platform (clears platform:disabled from Redis)."""
    redis_client.delete("platform:disabled")
    redis_client.delete("platform:disabled_reason")
    logger.info("PLATFORM RE-ENABLED")
    return {"status": "enabled"}


@_v1.get("/system/platform/status", tags=["observability"])
def platform_status(_caller: dict = Depends(_require_platform_admin)):
    """Admin only: return current platform kill-switch state."""
    disabled = redis_client.get("platform:disabled") == "1"
    reason   = redis_client.get("platform:disabled_reason") or ""
    return {"disabled": disabled, "reason": reason}


@_v1.get("/health/circuit-breakers", tags=["observability"])
def circuit_breaker_health():
    """Return the current state of all registered circuit breakers."""
    # Importing model_router ensures its module-level get_breaker() calls have run,
    # registering all four breakers (ollama_local, openai, claude, gemini) even
    # if no chat request has been processed yet in this process lifetime.
    import models.model_router  # noqa: F401 — side-effect import
    from core.circuit_breaker import all_breaker_states
    breakers = all_breaker_states()
    overall = "healthy" if all(b["state"] != "OPEN" for b in breakers) else "degraded"
    return {
        "status":    overall,
        "breakers":  breakers,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ============================================================
# AGENT BUILDER ENDPOINTS  (Phase 12)
# ============================================================

def _load_agent_builder():
    from agents.agent_builder import agent_builder, agent_runner
    return agent_builder, agent_runner


def _agent_row_to_dict(r) -> dict:
    """Normalise an AgentRecord ORM row to a stable API dict."""
    return {
        "name":            r.name,
        "description":     r.description or "",
        "system_prompt":   r.system_prompt or "",
        "tools":           r.tools or [],
        "skills":          r.skills or [],
        "workflows":       [],
        "tags":            [],
        "version":         r.version or "1.0.0",
        "author":          r.owner or "platform",
        "enabled":         r.enabled,
        "status":          r.status or "PRODUCTION",
        "stage":           r.stage or "production",
        "created_at":      r.created_at.isoformat() if r.created_at else "",
        "visibility":      r.visibility or "public",
        "department":      r.department or "",
        "created_by":      r.created_by or "platform",
        "kb_namespace":    getattr(r, "kb_namespace", None),
        "preferred_model": getattr(r, "preferred_model", None),
        "metadata":        {},
    }


def _pg_agents():
    from db.database import SessionLocal
    from db.models import AgentRecord
    db = SessionLocal()
    try:
        return db.query(AgentRecord).order_by(AgentRecord.name).all()
    finally:
        db.close()


@_v1.get("/agents", tags=["agents"])
def list_agents(_u: dict = Depends(_require_auth)):
    from db.database import SessionLocal
    from db.models import AgentRecord, ModelUsage
    from sqlalchemy import or_, and_
    from sqlalchemy import func
    db = SessionLocal()
    try:
        _uid  = _u.get("sub")
        _dept = _u.get("department", "")
        from auth.rbac import is_admin
        # Admin sees everything; others get visibility/dept filter
        if is_admin(_u):
            rows = db.query(AgentRecord).order_by(AgentRecord.name).all()
        else:
            rows = db.query(AgentRecord).filter(
                or_(
                    AgentRecord.created_by == _uid,
                    AgentRecord.created_by.is_(None),          # legacy: no creator → visible to all
                    AgentRecord.visibility.is_(None),           # legacy: no visibility → visible to all
                    and_(AgentRecord.visibility == "public",  AgentRecord.status.in_(["APPROVED", "PRODUCTION"])),
                    and_(AgentRecord.visibility == "private", AgentRecord.department == _dept),
                )
            ).order_by(AgentRecord.name).all()

        # Build usage counts per agent_id so the UI can show which agents have data
        usage_counts: dict = {}
        usage_costs: dict = {}
        try:
            usage_rows = db.query(
                ModelUsage.agent_id,
                func.count(ModelUsage.id).label("cnt"),
                func.sum(ModelUsage.cost_usd).label("cost"),
            ).group_by(ModelUsage.agent_id).all()
            for r in usage_rows:
                key = r.agent_id or "orchestrator"
                usage_counts[key] = r.cnt
                usage_costs[key]  = round(float(r.cost or 0), 4)
        except Exception:
            pass

        agents_out = []
        for r in rows:
            d = _agent_row_to_dict(r)
            d["total_runs"] = usage_counts.get(r.name, 0)
            d["total_cost_usd"] = usage_costs.get(r.name, 0.0)
            agents_out.append(d)

        # Build system_metrics separately — orchestrator/ide_direct are call-path
        # aggregators, not named agents. They go in a dedicated section so the
        # agent list only shows real, runnable, prompt-backed agents.
        system_metrics = []
        for virtual_name, alias_keys, label in [
            ("orchestrator", ["orchestrator", None], "Chat Gateway (OrchestratorAgent + RAG pipeline)"),
            ("ide_direct",   ["ide_direct"],          "IDE / OpenAI-compat endpoint (direct model calls)"),
        ]:
            cnt  = sum(usage_counts.get(k or "orchestrator", 0) for k in alias_keys)
            cost = sum(usage_costs.get(k or "orchestrator", 0.0) for k in alias_keys)
            system_metrics.append({
                "name":          virtual_name,
                "label":         label,
                "total_runs":    cnt,
                "total_cost_usd": round(cost, 4),
            })

        # Sort named agents: those with explicit runs first, then alphabetical
        agents_out.sort(key=lambda a: (-a.get("total_runs", 0), a["name"]))
        return {"agents": agents_out, "system_metrics": system_metrics}
    finally:
        db.close()


@_v1.post("/agents", tags=["agents"])
def create_agent(body: AgentCreate, _u: dict = Depends(_require_auth)):
    # Validate and sanitize all inputs
    is_valid, field_errors, sanitized = validate_agent_request(body)
    if not is_valid:
        error_messages = []
        for field, errors in field_errors.items():
            for e in errors:
                error_messages.append(f"{field}: {e}")
        raise HTTPException(status_code=400, detail="; ".join(error_messages))

    from db.database import SessionLocal
    from db.models import AgentRecord
    db = SessionLocal()
    try:
        existing = db.query(AgentRecord).filter(AgentRecord.name == sanitized["name"]).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"Agent '{sanitized['name']}' already exists")
        rec = AgentRecord(
            name=sanitized["name"],
            description=sanitized["description"],
            system_prompt=sanitized["system_prompt"],
            tools=body.tools,
            skills=body.skills,
            version=sanitized["version"],
            owner=sanitized["author"],
            status="DRAFT",
            enabled=True,
            created_by=_u.get("name") or _u.get("email") or _u.get("sub", "") or sanitized["author"] or "platform",
            visibility=getattr(body, "visibility", "private") or "private",
            department=_u.get("department", "") or body.department or "",
            kb_namespace=sanitized["kb_namespace"],
            preferred_model=body.preferred_model or None,
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return {"success": True, "agent": _agent_row_to_dict(rec)}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@_v1.get("/agents/{name}", tags=["agents"])
def get_agent(name: str, _u: dict = Depends(_require_auth)):
    from db.database import SessionLocal
    from db.models import AgentRecord
    db = SessionLocal()
    try:
        r = db.query(AgentRecord).filter(AgentRecord.name == name).first()
        if not r:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        return _agent_row_to_dict(r)
    finally:
        db.close()


@_v1.delete("/agents/{name}", tags=["agents"])
def delete_agent(name: str, _u: dict = Depends(_require_auth)):
    from db.database import SessionLocal
    from db.models import AgentRecord
    db = SessionLocal()
    try:
        r = db.query(AgentRecord).filter(AgentRecord.name == name).first()
        if not r:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        db.delete(r)
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@_v1.post("/agents/{name}/enable", tags=["agents"])
def enable_agent(name: str, _u: dict = Depends(_require_auth)):
    from db.database import SessionLocal
    from db.models import AgentRecord
    db = SessionLocal()
    try:
        r = db.query(AgentRecord).filter(AgentRecord.name == name).first()
        if not r:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        r.enabled = True
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@_v1.post("/agents/{name}/disable", tags=["agents"])
def disable_agent(name: str, _u: dict = Depends(_require_auth)):
    from db.database import SessionLocal
    from db.models import AgentRecord
    db = SessionLocal()
    try:
        r = db.query(AgentRecord).filter(AgentRecord.name == name).first()
        if not r:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        r.enabled = False
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@_v1.put("/agents/{name}", tags=["agents"])
def update_agent(name: str, body: AgentCreate, _u: dict = Depends(_require_auth)):
    # Validate and sanitize all inputs
    is_valid, field_errors, sanitized = validate_agent_request(body)
    if not is_valid:
        error_messages = []
        for field, errors in field_errors.items():
            for e in errors:
                error_messages.append(f"{field}: {e}")
        raise HTTPException(status_code=400, detail="; ".join(error_messages))

    from db.database import SessionLocal
    from db.models import AgentRecord
    db = SessionLocal()
    try:
        r = db.query(AgentRecord).filter(AgentRecord.name == name).first()
        if not r:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        r.name            = sanitized["name"]
        r.description     = sanitized["description"]
        r.system_prompt   = sanitized["system_prompt"]
        r.tools           = body.tools
        r.skills          = body.skills
        r.version         = sanitized["version"]
        r.owner           = sanitized["author"]
        r.visibility      = body.visibility or r.visibility
        if body.kb_namespace is not None:
            r.kb_namespace = sanitized["kb_namespace"]
        if body.preferred_model is not None:
            r.preferred_model = body.preferred_model or None
        db.commit()
        db.refresh(r)
        return {"success": True, "agent": _agent_row_to_dict(r)}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@_v1.post("/agents/{name}/run", tags=["agents"])
def run_agent(name: str, body: AgentRun, _u: dict = Depends(_require_auth)):
    from db.database import SessionLocal
    from db.models import AgentRecord
    db = SessionLocal()
    agent_rec = None
    try:
        rec = db.query(AgentRecord).filter(AgentRecord.name == name).first()
        if not rec:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        if rec.status not in ("PRODUCTION", "APPROVED"):
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{name}' is in '{rec.status}' state. "
                       f"Only PRODUCTION or APPROVED agents can run. "
                       f"Submit for approval via POST /governance/agents/{name}/submit"
            )
        # Capture values before session closes
        agent_rec = {
            "name":            rec.name,
            "description":     rec.description or "",
            "system_prompt":   rec.system_prompt or "",
            "tools":           list(rec.tools or []),
            "skills":          list(rec.skills or []),
            "version":         rec.version or "1.0.0",
            "author":          rec.owner or "platform",
            "enabled":         rec.enabled,
            "status":          rec.status or "PRODUCTION",
            "created_by":      rec.created_by or "platform",
            "kb_namespace":    getattr(rec, "kb_namespace", None),
            "preferred_model": getattr(rec, "preferred_model", None),
        }
    finally:
        db.close()

    builder, runner = _load_agent_builder()

    # Agents created via UI live in Postgres only (not Redis).
    # If the builder (Redis-backed) doesn't know about this agent, register it
    # from the Postgres snapshot so the runner can execute it.
    if builder.get(name) is None:
        from agents.agent_builder import AgentDefinition
        defn = AgentDefinition(
            name=agent_rec["name"],
            description=agent_rec["description"],
            system_prompt=agent_rec["system_prompt"],
            tools=agent_rec["tools"],
            skills=agent_rec["skills"],
            version=agent_rec["version"],
            author=agent_rec["author"],
            enabled=agent_rec["enabled"],
            status=agent_rec["status"],
            created_by=agent_rec["created_by"],
            kb_namespace=agent_rec.get("kb_namespace"),
            preferred_model=agent_rec.get("preferred_model"),
        )
        builder.create(defn)

    _caller_uid = _u.get("sub") or _u.get("user_id") or _u.get("email")
    # Stable session: caller-supplied > per-user-per-agent default.
    # This ensures conversation history accumulates correctly across turns.
    _session = body.session_id or (
        f"agent_{name}_{_caller_uid}" if _caller_uid else None
    )

    # AgentRunner is text-only — prepend parsed file content so attachments reach the LLM.
    # This used to hard-truncate to 10,000 chars unconditionally with no env
    # override — any Excel content routed through this endpoint lost everything
    # past ~10K characters, always. Now consistent with the /ask path: env-tunable
    # via ASK_ATTACH_CHAR_CAP (default 0 = no cap), and truncation (if the cap is
    # ever set) is surfaced explicitly to the model instead of silently applied.
    _message = body.message
    if body.attachment_ids:
        try:
            from db.database import SessionLocal as _AttSessionLocal
            from db.models import ChatAttachment
            _att_session = _AttSessionLocal()
            try:
                _attachments = _att_session.query(ChatAttachment).filter(
                    ChatAttachment.id.in_(body.attachment_ids)
                ).all()
                try:
                    _agent_attach_cap = int(os.getenv("ASK_ATTACH_CHAR_CAP", "0") or "0")
                except Exception:
                    _agent_attach_cap = 0
                _blocks = []
                for a in _attachments:
                    if not a.parsed_text:
                        continue
                    _truncated = _agent_attach_cap > 0 and len(a.parsed_text) > _agent_attach_cap
                    _ptext = a.parsed_text if _agent_attach_cap <= 0 else a.parsed_text[:_agent_attach_cap]
                    _warn = (
                        f"\n[NOTE: this file is {len(a.parsed_text):,} characters; only the first "
                        f"{_agent_attach_cap:,} are shown here ({len(a.parsed_text) - _agent_attach_cap:,} characters "
                        f"were NOT included]. Do not re-request this file — tell the user the data is partial.\n"
                    ) if _truncated else ""
                    _blocks.append(f"[File: {a.file_name}]{_warn}\n{_ptext}")
                if _blocks:
                    _message = "\n\n".join(_blocks) + "\n\nUser question: " + _message
            finally:
                _att_session.close()
        except Exception as _att_err:
            logger.warning(f"agent attachment fetch failed: {_att_err}")

    if body.stream:
        # SSE path — used by AgentsCatalog; runs agent then emits answer as token chunks.
        # No chat_history write; conversation is scoped to this agent session only.
        def _agent_sse():
            r = runner.run(name, _message, _session, user_id=_caller_uid)
            answer = r.answer if r.success else (r.error or "Agent run failed.")
            words = answer.split(" ")
            for i in range(0, len(words), 8):
                chunk = " ".join(words[i:i + 8])
                if i + 8 < len(words):
                    chunk += " "
                yield "data: " + json.dumps({"t": chunk}) + "\n\n"
            yield "data: " + json.dumps({"__meta__": {
                "run_id": r.run_id,
                "success": r.success,
                "duration_ms": round(r.duration_ms, 1),
            }}) + "\n\n"
            # ── eval_results (LLM-as-judge) for Agent Studio ──────────────
            # Fire groundedness + relevance after the agent run completes.
            # Gated to successful runs with a non-empty answer only.
            if r.success and answer:
                try:
                    from core.evals import eval_engine as _as_eval_eng, EVAL_ENABLED as _as_eval_on
                    if _as_eval_on:
                        _as_q   = (_message or "")[:500]
                        _as_a   = answer[:1000]
                        _as_sid = _session
                        _as_m   = None  # model not available on streaming path
                        import threading as _as_thread
                        import contextvars as _as_cv
                        def _run_as_eval(_q=_as_q, _a=_as_a, _s=_as_sid, _m=_as_m):
                            try:
                                _as_eval_eng.eval_answer_quality(
                                    _q, _a, [],
                                    session_id=_s,
                                    platform="agent_studio",
                                    model=_m,
                                )
                            except Exception:
                                pass
                        _as_ctx = _as_cv.copy_context()
                        _as_thread.Thread(
                            target=lambda: _as_ctx.run(_run_as_eval),
                            daemon=True, name="eval-agent-studio",
                        ).start()
                except Exception:
                    pass

        return StreamingResponse(
            _agent_sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    result = runner.run(name, _message, _session, user_id=_caller_uid)
    # ── eval_results for non-streaming agent run ──────────────────────────
    if result.success and result.answer:
        try:
            from core.evals import eval_engine as _as_eval_eng2, EVAL_ENABLED as _as_eval_on2
            if _as_eval_on2:
                import threading as _as_t2, contextvars as _as_cv2
                _as_q2, _as_a2, _as_s2 = (_message or "")[:500], result.answer[:1000], _session
                _as_m2 = None
                def _run_as_eval2(_q=_as_q2, _a=_as_a2, _s=_as_s2, _m=_as_m2):
                    try:
                        _as_eval_eng2.eval_answer_quality(_q, _a, [], session_id=_s, platform="agent_studio", model=_m)
                    except Exception:
                        pass
                _as_ctx2 = _as_cv2.copy_context()
                _as_t2.Thread(target=lambda: _as_ctx2.run(_run_as_eval2), daemon=True, name="eval-agent-studio-sync").start()
        except Exception:
            pass
    return asdict(result)

class AgentTalk(BaseModel):
    message: str
    system_prompt: Optional[str] = None   # override for mid-build testing
    tools: Optional[List[str]] = None
    skills: Optional[List[str]] = None
    kb_namespace: Optional[str] = None
    preferred_model: Optional[str] = None


@_v1.post("/agents/{name}/talk", tags=["agents"])
def talk_to_agent(name: str, body: AgentTalk, _u: dict = Depends(_require_auth)):
    """
    Test an agent mid-build without requiring PRODUCTION status.
    Accepts optional overrides so the builder can preview changes before saving.
    """
    from db.database import SessionLocal
    from db.models import AgentRecord
    from agents.agent_builder import AgentDefinition, AgentRunner, AgentBuilder

    db = SessionLocal()
    try:
        rec = db.query(AgentRecord).filter(AgentRecord.name == name).first()
        base = rec if rec else None
        defn = AgentDefinition(
            name=name,
            description=(base.description or "") if base else "Draft agent",
            system_prompt=body.system_prompt or (base.system_prompt or "") if base else "",
            tools=body.tools if body.tools is not None else (list(base.tools or []) if base else []),
            skills=body.skills if body.skills is not None else (list(base.skills or []) if base else []),
            enabled=True,
            status="PRODUCTION",  # bypass governance for talk-mode testing
            created_by=_u.get("sub", "platform"),
            kb_namespace=body.kb_namespace or (getattr(base, "kb_namespace", None) if base else None),
            preferred_model=body.preferred_model or (getattr(base, "preferred_model", None) if base else None),
        )
    finally:
        db.close()

    _builder = AgentBuilder.__new__(AgentBuilder)
    _builder._agents = {name: defn}
    _runner = AgentRunner(_builder)
    result = _runner.run(name, body.message)
    return {"answer": result.answer, "tool_outputs": result.tool_outputs, "success": result.success}


@_v1.post("/agents/{name}/run/async", tags=["agents"])
def run_agent_async(name: str, body: AgentRun, _u: dict = Depends(_require_auth)):
    """
    Enqueue an agent run as an async job (non-blocking).

    Returns immediately with a job_id. Poll GET /jobs/{job_id} for the result.
    Useful for long-running agents (research, multi-tool) that would otherwise
    hold the HTTP connection open.
    """
    from db.database import SessionLocal
    from db.models import AgentRecord
    db = SessionLocal()
    try:
        rec = db.query(AgentRecord).filter(AgentRecord.name == name).first()
        if not rec:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        if rec.status not in ("PRODUCTION", "APPROVED"):
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{name}' is in '{rec.status}' state. "
                       "Only PRODUCTION or APPROVED agents can run async.",
            )
    finally:
        db.close()

    from core.job_queue import enqueue_agent_job
    job_id = enqueue_agent_job(
        agent_name=name,
        message=body.message,
        session_id=body.session_id,
    )
    return {
        "job_id":    job_id,
        "agent":     name,
        "status":    "queued",
        "poll_url":  f"/jobs/{job_id}",
        "message":   f"Agent '{name}' run enqueued. Poll /jobs/{job_id} for the result.",
    }


@_v1.get("/system/semaphore", tags=["system"])
def semaphore_stats():
    """Return adaptive semaphore stats (current cap, p95 latency)."""
    return {
        "current_cap":  _SEM_CAP,
        "min_cap":       _SEM_MIN,
        "max_cap":       _SEM_MAX,
        "p95_latency_ms": round(_p95_latency(), 1),
        "sample_count":  len(_latency_samples),
    }


# ============================================================
# TOOLS & SKILLS CATALOGUE  (Phase 12)
# ============================================================

@_v1.get("/tools")
def list_tools(_u: dict = Depends(_require_auth)):
    from mcp.registry import mcp_registry
    from auth.rbac import is_admin
    uid  = _u.get("sub", "")
    dept = _u.get("department", "")
    all_tools = mcp_registry.tools.list_all(enabled_only=False)

    # Filter: admin sees all; others see public+PRODUCTION tools OR same-dept private tools OR own tools
    if is_admin(_u):
        visible = all_tools
    else:
        visible = [
            t for t in all_tools
            if (
                (getattr(t, "visibility", "public") == "public" and getattr(t, "status", "PRODUCTION") in ("APPROVED", "PRODUCTION"))
                or (getattr(t, "visibility", None) == "private" and getattr(t, "department", "") == dept)
                or getattr(t, "created_by", "") == uid
                or getattr(t, "visibility", None) is None   # legacy tool without visibility
            )
        ]

    return {
        "tools": [
            {
                "name":        t.name,
                "description": t.description,
                "tags":        t.tags,
                "enabled":     t.enabled,
                "visibility":  getattr(t, "visibility",  "public"),
                "department":  getattr(t, "department",  ""),
                "status":      getattr(t, "status",      "PRODUCTION"),
            }
            for t in visible
        ]
    }


# GET /skills is now handled by routers/skills_router.py


# ============================================================
# WORKFLOW STORE  (Phase 12 — Workflow Builder)
# Stores Workflow definitions (JSON) in Redis.
# ============================================================

def _wf_row_to_dict(r) -> dict:
    return {
        "name":            r.name,
        "description":     r.description or "",
        "stop_on_failure": True,
        "steps":           r.steps or [],
        "status":          r.status or "DRAFT",
        "created_by":      r.created_by or "platform",
        "visibility":      getattr(r, "visibility", "private") or "private",
        "department":      getattr(r, "department", "") or "",
        "enabled":         r.is_production,
        "created_at":      r.created_at.isoformat() if r.created_at else None,
    }


# ============================================================
# WORKFLOW REQUEST MODELS
# ============================================================

class WorkflowStepBody(BaseModel):
    id:         str
    name:       str
    step_type:  str                  # llm | code | shell | tool
    input:      str = ""
    depends_on: List[str] = []

class WorkflowBody(BaseModel):
    name:            str
    description:     str = ""
    stop_on_failure: bool = True
    steps:           List[WorkflowStepBody] = []
    visibility:      str = "private"
    department:      str = ""


# ============================================================
# WORKFLOW ENDPOINTS  (Phase 12)
# ============================================================

@_v1.get("/workflows")
def list_workflows(_u: dict = Depends(_require_auth)):
    from db.database import SessionLocal
    from db.models import WorkflowRecord
    from sqlalchemy import or_, and_
    uid  = _u.get("sub")
    dept = _u.get("department", "")
    from auth.rbac import is_admin
    db = SessionLocal()
    try:
        if is_admin(_u):
            rows = db.query(WorkflowRecord).order_by(WorkflowRecord.name).all()
        else:
            rows = db.query(WorkflowRecord).filter(
                or_(
                    WorkflowRecord.created_by == uid,
                    and_(WorkflowRecord.visibility == "public",  WorkflowRecord.status == "PRODUCTION"),
                    and_(WorkflowRecord.visibility == "private", WorkflowRecord.department == dept),
                )
            ).order_by(WorkflowRecord.name).all()
        return {"workflows": [_wf_row_to_dict(r) for r in rows]}
    finally:
        db.close()


@_v1.post("/workflows")
def save_workflow(body: WorkflowBody, _u: dict = Depends(_require_auth)):
    # Validate and sanitize all inputs
    is_valid, field_errors, sanitized = validate_workflow_request(body)
    if not is_valid:
        error_messages = []
        for field, errors in field_errors.items():
            for e in errors:
                error_messages.append(f"{field}: {e}")
        raise HTTPException(status_code=400, detail="; ".join(error_messages))

    from db.database import SessionLocal
    from db.models import WorkflowRecord
    uid       = _u.get("sub", "")
    _display  = _u.get("name") or _u.get("email") or uid
    dept      = _u.get("department", "")
    db = SessionLocal()
    try:
        existing = db.query(WorkflowRecord).filter(WorkflowRecord.name == sanitized["name"]).first()
        if existing:
            existing.description = sanitized["description"]
            existing.steps       = sanitized["steps"]
            existing.visibility  = body.visibility
            if existing.created_by in (uid, _display) or not existing.created_by:
                existing.department = body.department or dept
        else:
            rec = WorkflowRecord(
                name=sanitized["name"],
                description=sanitized["description"],
                steps=sanitized["steps"],
                status="DRAFT",
                is_production=False,
                visibility=body.visibility,
                department=body.department or dept,
                created_by=_display,
            )
            db.add(rec)
        db.commit()
        return {"success": True, "name": sanitized["name"]}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
# this endpoint previously had no auth dependency and no visibility check
# at all, letting any anonymous caller read the full definition (steps,
# department, creator) of ANY workflow by name — even ones marked private
# or department-scoped — while its sibling GET /workflows (list) already
# requires auth and applies a creator/public-production/department ACL.
# Fix, in two parts:
#  1. Added `_u: dict = Depends(_require_auth)` as a function parameter,
#     so FastAPI rejects unauthenticated requests with 401.
#  2. Added the `if not is_admin(_u): ...` block below (new code — it did
#     not exist before), which recomputes the exact same
#     creator/public-production/department visibility rule that
#     `list_workflows()` above already applies via its SQLAlchemy filter,
#     but evaluated in Python against the single fetched row. A non-admin
#     caller now gets a 404 (not the workflow) unless they created it, or
#     it's public+PRODUCTION, or it's private and in their own department
#     — i.e. a workflow can only be fetched here if it would also appear
#     in that caller's own GET /workflows list.
@_v1.get("/workflows/{name}")
def get_workflow(name: str, _u: dict = Depends(_require_auth)):
    from db.database import SessionLocal
    from db.models import WorkflowRecord
    from auth.rbac import is_admin
    uid  = _u.get("sub")
    dept = _u.get("department", "")
    db = SessionLocal()
    try:
        r = db.query(WorkflowRecord).filter(WorkflowRecord.name == name).first()
        if not r:
            raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
        if not is_admin(_u):
            visibility = getattr(r, "visibility", "private") or "private"
            status     = getattr(r, "status", "")
            r_dept     = getattr(r, "department", "") or ""
            visible = (
                r.created_by == uid
                or (visibility == "public" and status == "PRODUCTION")
                or (visibility == "private" and r_dept == dept)
            )
            if not visible:
                raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
        return _wf_row_to_dict(r)
    finally:
        db.close()


@_v1.delete("/workflows/{name}")
def delete_workflow(name: str):
    from db.database import SessionLocal
    from db.models import WorkflowRecord
    db = SessionLocal()
    try:
        r = db.query(WorkflowRecord).filter(WorkflowRecord.name == name).first()
        if not r:
            raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
        db.delete(r)
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@_v1.post("/workflows/{name}/run")
def run_workflow(name: str):
    from workflows.engine import workflow_engine, Workflow, WorkflowStep
    from mcp.registry import mcp_registry

    from db.database import SessionLocal
    from db.models import WorkflowRecord
    _db = SessionLocal()
    try:
        _r = _db.query(WorkflowRecord).filter(WorkflowRecord.name == name).first()
        if not _r:
            raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
        if _r.status not in ("PRODUCTION", "APPROVED"):
            raise HTTPException(
                status_code=403,
                detail=f"Workflow '{name}' is in '{_r.status}' state. "
                       f"Only PRODUCTION or APPROVED workflows can run. "
                       f"Submit for approval via POST /governance/workflows/{name}/submit"
            )
        defn = _wf_row_to_dict(_r)
    finally:
        _db.close()

    steps = []
    for s in defn.get("steps", []):
        tool_fn = None
        if s["step_type"] == "tool":
            # Resolve tool callable from MCP registry at run time
            tool_name = s["input"]
            tool_fn = lambda inp, tn=tool_name: mcp_registry.execute_tool(tn, question=inp).output

        steps.append(WorkflowStep(
            id=s["id"],
            name=s["name"],
            step_type=s["step_type"],
            input=s["input"],
            depends_on=s.get("depends_on", []),
            tool_fn=tool_fn,
        ))

    wf = Workflow(
        name=defn["name"],
        description=defn.get("description", ""),
        stop_on_failure=defn.get("stop_on_failure", True),
        steps=steps,
    )

    result = workflow_engine.run(wf)
    return asdict(result)

# ============================================================
# AUDIT LOG  — GET /audit
# Returns last 200 governance + SDLC events as log lines for TracePanel
#
# SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
# this endpoint previously had no auth dependency at all, exposing recent
# governance and SDLC audit events (who changed what, when) to any
# anonymous caller.
# Fix: added `_caller: dict = Depends(require_role("admin"))` as a
# function parameter. `require_role("admin")` (auth/rbac.py) rejects
# unauthenticated requests with 401 and non-admin requests with 403 —
# restricting this endpoint to admins, the same restriction already used
# for /metrics, /metrics/prometheus, /metrics/compression, /trace/*, and
# /traces/* elsewhere in this file.
# ============================================================

@_v1.get("/audit", tags=["audit"])
def get_audit_log(limit: int = 200, _caller: dict = Depends(require_role("admin"))):
    from db.database import SessionLocal
    from db.models import GovernanceEvent, SDLCRunEvent
    db = SessionLocal()
    try:
        gov_rows = (db.query(GovernanceEvent)
                    .order_by(GovernanceEvent.created_at.desc())
                    .limit(limit).all())
        sdlc_rows = (db.query(SDLCRunEvent)
                     .order_by(SDLCRunEvent.created_at.asc())
                     .limit(limit).all())
        logs = []
        for r in gov_rows:
            ts = r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""
            logs.append(f"[{ts}] GOVERNANCE {r.entity_type}/{r.name}: {r.action} ({r.from_status} → {r.to_status}) by {r.actor}")
        for r in sdlc_rows:
            ts = r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""
            logs.append(f"[{ts}] SDLC {r.run_id}: [{r.stage or ''}] {r.from_state} → {r.to_state} by {r.actor or 'system'}")
        logs.sort(reverse=True)
        return {"logs": logs[:limit]}
    finally:
        db.close()


# ============================================================
# CLIENT ACTIVITY AUDIT — GET /audit/client-activity
# Breakdown of platform usage by client: web | cli | ide
# Admin / security only.
# ============================================================

@_v1.get("/audit/client-activity", tags=["audit"])
def get_client_activity(
    days: int = 7,
    _caller=Depends(_require_auth),
):
    """
    Returns per-client request counts, unique users, avg latency, and
    recent 50 requests — so admins can see who is using platform/cli/ide.
    """
    from db.database import SessionLocal
    from db.models import RequestAuditLog
    from sqlalchemy import func, text as _text
    import datetime as _dt

    db = SessionLocal()
    try:
        since = _dt.datetime.utcnow() - _dt.timedelta(days=days)

        # Per-client summary
        rows = (
            db.query(
                RequestAuditLog.client_source,
                func.count(RequestAuditLog.id).label("requests"),
                func.count(func.distinct(RequestAuditLog.user_id)).label("unique_users"),
                func.avg(RequestAuditLog.latency_ms).label("avg_latency_ms"),
                func.sum(RequestAuditLog.cost_usd).label("total_cost_usd"),
            )
            .filter(RequestAuditLog.created_at >= since)
            .group_by(RequestAuditLog.client_source)
            .all()
        )

        summary = [
            {
                "client_source":  r.client_source,
                "requests":       r.requests,
                "unique_users":   r.unique_users,
                "avg_latency_ms": round(r.avg_latency_ms or 0, 1),
                "total_cost_usd": round(r.total_cost_usd or 0, 4),
            }
            for r in rows
        ]

        # Recent 50 requests (newest first)
        recent = (
            db.query(RequestAuditLog)
            .filter(RequestAuditLog.created_at >= since)
            .order_by(RequestAuditLog.created_at.desc())
            .limit(50)
            .all()
        )

        recent_list = [
            {
                "ts":                r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "request_id":        r.request_id,
                "email":             r.email or r.user_id,
                "department":        r.department or "",
                "client_source":     r.client_source,
                "endpoint":          r.endpoint,
                "model_used":        r.model_used or "",
                "latency_ms":        r.latency_ms,
                "cache_hit":         r.cache_hit or "none",
                "compliance_blocked": r.compliance_blocked,
                "error":             r.error or "",
            }
            for r in recent
        ]

        return {
            "period_days": days,
            "summary":     summary,
            "recent":      recent_list,
        }
    finally:
        db.close()


# ============================================================
# MY CHATS — GET /chats
# Returns the authenticated user's chat list (for sidebar restore)
# ============================================================

@_v1.get("/chats", tags=["chat"])
def list_my_chats(
    request: Request,
    limit: int = 200,
    _caller=Depends(_require_auth),
):
    """
    Return the current user's chats ordered by most recently updated.

    Scoped by client_source: the web UI never sees CLI/IDE chats and vice
    versa. The 'channel' isolation is non-negotiable — engineers' CLI
    prompts often contain code-paste content that doesn't belong in a
    shared sidebar list. ClientSourceMiddleware sets request.state from
    the X-AiNxt-Client header (or User-Agent fallback).
    """
    from db.database import SessionLocal
    from db.models import Chat, ChatMessage
    from sqlalchemy import func

    user_id = _caller.get("sub") if isinstance(_caller, dict) else None
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    requester_src = getattr(request.state, "client_source", "platform")

    db = SessionLocal()
    try:
        # Subquery: count messages per chat
        msg_counts = (
            db.query(
                ChatMessage.chat_id,
                func.count(ChatMessage.id).label("cnt"),
            )
            .group_by(ChatMessage.chat_id)
            .subquery()
        )
        q = (
            db.query(Chat, msg_counts.c.cnt)
            .outerjoin(msg_counts, Chat.id == msg_counts.c.chat_id)
            .filter(Chat.user_id == user_id)
            .filter(Chat.client_source == requester_src)
            .order_by(Chat.updated_at.desc())
            .limit(limit)
        )
        rows = q.all()
        chats = [
            {
                "id":            c.id,
                "title":         c.title or "New Chat",
                "message_count": cnt or 0,
                "updated_at":    c.updated_at.isoformat() if c.updated_at else None,
                "client_source": c.client_source,
            }
            for c, cnt in rows
        ]
        return {"chats": chats, "client_source": requester_src}
    finally:
        db.close()


# ============================================================
# CHAT MESSAGES — GET /chats/{chat_id}/messages
# Returns messages for a single chat (owned by the caller)
# ============================================================

@_v1.get("/chats/{chat_id}/messages", tags=["chat"])
def get_chat_messages(
    request: Request,
    chat_id: str,
    limit: int = 500,
    _caller=Depends(_require_auth),
):
    """
    Return all messages for a chat the caller owns.

    In addition to user-id ownership, we enforce channel isolation: the
    requesting client must match the chat's client_source. A web user
    cannot pull a CLI conversation by guessing its UUID, and the reverse.
    """
    from db.database import SessionLocal
    from db.models import Chat, ChatMessage

    caller_id = _caller.get("sub") if isinstance(_caller, dict) else None
    if not caller_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    requester_src = getattr(request.state, "client_source", "platform")

    db = SessionLocal()
    try:
        chat = db.query(Chat).filter(Chat.id == chat_id).first()
        if not chat:
            raise HTTPException(status_code=404, detail="Chat not found")
        # Owners and admins can read; no one else
        caller_role = _caller.get("role", "user") if isinstance(_caller, dict) else "user"
        if chat.user_id and chat.user_id != caller_id and caller_role != "admin":
            raise HTTPException(status_code=403, detail="Not your chat")
        # Channel isolation: the chat must have been created on the same
        # client_source as the requester. Admins can cross channels for
        # debugging (their tools need to see everything).
        if caller_role != "admin" and (chat.client_source or "platform") != requester_src:
            raise HTTPException(
                status_code=404,
                detail=f"Chat not found in {requester_src} channel "
                       f"(belongs to {chat.client_source})",
            )

        rows = (
            db.query(ChatMessage)
            .filter(ChatMessage.chat_id == chat_id)
            .order_by(ChatMessage.created_at.asc())
            .limit(limit)
            .all()
        )
        return {
            "chat_id": chat_id,
            "messages": [
                {
                    "id":         m.id,
                    "role":       m.role,
                    "content":    m.content,
                    "model_used": m.model_used,
                    "tokens_used": m.tokens_used,
                    "cost_usd":   m.cost_usd,
                    "in_tok":     m.in_tok,
                    "out_tok":    m.out_tok,
                    "latency":    m.latency,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in rows
            ],
        }
    finally:
        db.close()


# ============================================================
# CHAT HISTORY — GET /chats/{user_id}/history
# Admin/security endpoint: full prompt+response audit trail per user
# ============================================================

@_v1.get("/chats/{user_id}/history", tags=["audit"])
def get_user_chat_history(
    user_id: str,
    limit: int = 100,
    offset: int = 0,
    chat_id: Optional[str] = None,
    _caller=Depends(_require_auth),
):
    """
    Return full prompt/response history for a given user.
    Requires operator or admin role.
    Optional ?chat_id= to scope to a single conversation.
    """
    from db.database import SessionLocal
    from db.models import Chat, ChatMessage
    from auth.rbac import _ROLE_LEVEL

    caller_role = _caller.get("role", "viewer") if isinstance(_caller, dict) else "viewer"
    if _ROLE_LEVEL.get(caller_role, 0) < _ROLE_LEVEL["operator"]:
        raise HTTPException(status_code=403, detail="operator or admin role required")

    db = SessionLocal()
    try:
        query = (
            db.query(ChatMessage, Chat)
            .join(Chat, Chat.id == ChatMessage.chat_id)
            .filter(Chat.user_id == user_id)
        )
        if chat_id:
            query = query.filter(ChatMessage.chat_id == chat_id)

        query = query.order_by(ChatMessage.created_at.asc())
        total = query.count()
        rows = query.offset(offset).limit(limit).all()

        messages = []
        for msg, chat in rows:
            messages.append({
                "chat_id":    chat.id,
                "chat_title": chat.title,
                "message_id": msg.id,
                "role":       msg.role,
                "content":    msg.content,
                "model_used": msg.model_used,
                "tokens":     msg.tokens_used,
                "cost_usd":   msg.cost_usd,
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
            })

        return {
            "user_id":  user_id,
            "total":    total,
            "offset":   offset,
            "limit":    limit,
            "messages": messages,
        }
    finally:
        db.close()


# ============================================================
# SECURITY SCAN RESULTS — GET /security/scans
# Query scan history: by repo, PR, or blocked-only
# ============================================================

@_v1.get("/security/scans", tags=["security"])
def list_security_scans(
    repo:       Optional[str] = None,
    pr_number:  Optional[int] = None,
    blocked:    Optional[bool] = None,
    limit:      int = 50,
    _caller=Depends(_require_auth),
):
    """
    Query security scan results.
    ?repo=owner/repo  — filter by repo
    ?pr_number=42     — filter by PR
    ?blocked=true     — show only blocked scans
    Requires developer+ role.
    """
    from db.database import SessionLocal
    from sqlalchemy import text as _sqlt

    db = SessionLocal()
    try:
        where = ["1=1"]
        params: dict = {"limit": limit}
        if repo:
            where.append("repo = :repo")
            params["repo"] = repo
        if pr_number is not None:
            where.append("pr_number = :pr_number")
            params["pr_number"] = pr_number
        if blocked is not None:
            where.append("blocked = :blocked")
            params["blocked"] = blocked

        where_clause = " AND ".join(where)
        rows = db.execute(_sqlt(f"""
            SELECT id, repo, branch, pr_number, run_id,
                   max_cvss, critical_count, high_count,
                   total_findings, blocked, scanned_at
            FROM security_scan_results
            WHERE {where_clause}
            ORDER BY scanned_at DESC
            LIMIT :limit
        """), params).fetchall()

        return {
            "scans": [
                {
                    "id":             str(r[0]),
                    "repo":           r[1],
                    "branch":         r[2],
                    "pr_number":      r[3],
                    "run_id":         str(r[4]) if r[4] else None,
                    "max_cvss":       r[5],
                    "critical_count": r[6],
                    "high_count":     r[7],
                    "total_findings": r[8],
                    "blocked":        r[9],
                    "scanned_at":     r[10].isoformat() if r[10] else None,
                }
                for r in rows
            ]
        }
    finally:
        db.close()


@_v1.get("/security/scans/{scan_id}", tags=["security"])
def get_security_scan(scan_id: str, _caller=Depends(_require_auth)):
    """Get full findings detail for a specific scan (including findings_json)."""
    from db.database import SessionLocal
    from sqlalchemy import text as _sqlt

    db = SessionLocal()
    try:
        row = db.execute(_sqlt("""
            SELECT id, repo, branch, pr_number, run_id,
                   max_cvss, critical_count, high_count,
                   total_findings, blocked, findings_json, scanned_at
            FROM security_scan_results WHERE id = :id
        """), {"id": scan_id}).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Scan not found")

        return {
            "id":             str(row[0]),
            "repo":           row[1],
            "branch":         row[2],
            "pr_number":      row[3],
            "run_id":         str(row[4]) if row[4] else None,
            "max_cvss":       row[5],
            "critical_count": row[6],
            "high_count":     row[7],
            "total_findings": row[8],
            "blocked":        row[9],
            "findings":       row[10] or [],
            "scanned_at":     row[11].isoformat() if row[11] else None,
        }
    finally:
        db.close()


# ============================================================
# VOICE TTS ENDPOINT
# Proxies text → OpenAI TTS → returns audio/mpeg stream.
# Keeps the OpenAI API key server-side (never exposed to browser).
# ============================================================

class _TTSRequest(BaseModel):
    text:  str
    voice: str  = "nova"    # alloy | echo | fable | onyx | nova | shimmer
    model: str  = "tts-1-hd"  # tts-1 | tts-1-hd
    speed: float = 0.92    # 0.25–4.0; slightly relaxed sounds more natural


@_v1.post("/voice/tts", tags=["voice"])
async def voice_tts(_req: _TTSRequest, _user=Depends(_require_auth)):
    
    """Convert text to speech via OpenAI TTS. Returns audio/mpeg.

    Routing:
      - LLM_PROXY_URL set  → forwards to llm_proxy /llm/tts on the LLM proxy server
        (the LLM proxy server has outbound internet; the gateway server does not)
      - LLM_PROXY_URL unset → calls OpenAI directly (local dev only)
    """
    import httpx
    from fastapi.responses import Response as _Resp

    text = _req.text.strip()[:2000]
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    _proxy_url = os.getenv("LLM_PROXY_URL", "").rstrip("/")

    # ── Route through llm_proxy on the LLM proxy server (production path) ──────
    if _proxy_url:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{_proxy_url}/llm/tts",
                    json={
                        "text":  text,
                        "voice": _req.voice,
                        "model": _req.model,
                        "speed": _req.speed,
                    },
                )
                resp.raise_for_status()
                return _Resp(content=resp.content, media_type="audio/mpeg")
        except httpx.HTTPStatusError as e:
            _body = e.response.text[:300]
            logger.error(f"[TTS] llm_proxy error {e.response.status_code}: {_body}")
            raise HTTPException(status_code=502, detail=f"TTS proxy error: {e.response.status_code} — {_body}")
        except httpx.ConnectError as e:
            logger.error(f"[TTS] Cannot reach llm_proxy at {_proxy_url}: {e}")
            raise HTTPException(status_code=502, detail="TTS proxy unreachable — check LLM_PROXY_URL")
        except httpx.TimeoutException as e:
            logger.error(f"[TTS] Timeout waiting for llm_proxy TTS: {e}")
            raise HTTPException(status_code=504, detail="TTS request timed out")
        except Exception as e:
            logger.error(f"[TTS] Unexpected proxy error: {type(e).__name__}: {e}")
            raise HTTPException(status_code=502, detail="TTS request failed")

    # ── Direct OpenAI call (local dev — no proxy) ────────────────

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY not configured")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": _req.model, "voice": _req.voice, "input": text, "speed": _req.speed},
            )
            resp.raise_for_status()
            return _Resp(content=resp.content, media_type="audio/mpeg")
    except httpx.HTTPStatusError as e:
        logger.error(f"[TTS] OpenAI error {e.response.status_code}: {e.response.text[:300]}")
        raise HTTPException(status_code=502, detail=f"TTS service error: {e.response.status_code}")
    except httpx.ConnectError as e:
        logger.error(f"[TTS] ConnectError to OpenAI — outbound blocked? {e}")
        raise HTTPException(status_code=502, detail="TTS connect failed — set LLM_PROXY_URL for production")
    except httpx.TimeoutException as e:
        logger.error(f"[TTS] Timeout calling OpenAI TTS: {e}")
        raise HTTPException(status_code=504, detail="TTS request timed out")
    except Exception as e:
        logger.error(f"[TTS] Unexpected error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail="TTS request failed")

from fastapi import File as _SttFile, UploadFile as _SttUpload


@_v1.post("/voice/stt", tags=["voice"])
async def voice_stt(
        file: _SttUpload = _SttFile(...),
        _user=Depends(_require_auth),
):
    """Speech-to-text (P3 whisper wire) — completes the Voice Mode input loop.

    ML never runs in the uvicorn process (the no-lazy-load rule). Resolution order:
      1. WHISPER_SVC_URL set  → proxy the audio to the local whisper microservice
         (services/whisper_svc, faster-whisper, CPU, out-of-process — air-gap safe).
      2. else OPENAI_API_KEY  → OpenAI whisper-1 transcription (connected envs only).
      3. else                 → 503 (feature simply unavailable; nothing else breaks).
    Returns {"text": "..."} in all success cases. Additive + default-OFF: with neither
    configured, behavior is unchanged from today.
    """
    if file is None:
        raise HTTPException(status_code=400, detail="audio file is required")
    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=400, detail="empty audio")
    fname = getattr(file, "filename", "audio.wav") or "audio.wav"
    ctype = getattr(file, "content_type", "audio/wav") or "audio/wav"

    import httpx

    svc_url = os.getenv("WHISPER_SVC_URL", "").rstrip("/")
    if svc_url:
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{svc_url}/transcribe",
                    files={"file": (fname, audio, ctype)},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.error(f"whisper_svc STT failed: {e}")
            raise HTTPException(status_code=502, detail="STT service error")

    api_key = os.getenv("OPENAI_API_KEY", "")
    if api_key:
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (fname, audio, ctype)},
                    data={"model": "whisper-1"},
                )
                resp.raise_for_status()
                return {"text": resp.json().get("text", "")}
        except Exception as e:
            logger.error(f"OpenAI STT failed: {e}")
            raise HTTPException(status_code=502, detail="STT request failed")

    raise HTTPException(
        status_code=503,
        detail="STT not configured (set WHISPER_SVC_URL for local whisper, or OPENAI_API_KEY)",
    )


# ============================================================
# ASYNC CHAT ENDPOINTS  (submit + stream)
#
# POST /ask/submit  — enqueue job, return {job_id} immediately (<10ms)
# GET  /ask/stream/{job_id}  — XREAD Redis Stream → SSE to client
#
# Client flow:
#   const { job_id } = await fetch('/ask/submit', {...}).then(r => r.json())
#   const es = new EventSource(`/ask/stream/${job_id}`)
#   es.onmessage = e => { const d = JSON.parse(e.data); ... }
#
# Hanging prevention (see architecture doc):
#   - XREAD BLOCK 5s (short), loop checks disconnect + job status
#   - Hard 120s wall-clock cap
#   - Worker publishes __done__/__error__ in finally block (guaranteed)
#   - EXPIRE 1h on stream key (last-resort TTL if gateway misses sentinel)
# ============================================================

# ============================================================
# IMAGE ASK ENDPOINT — multipart/form-data with optional image
# Routes vision queries directly to Gemini (GEMINI_VISION_MODEL —
# defaults to gemini-3.1-flash-image; env-overridable).
# Falls back to /ask SSE stream when no image is attached.
# ============================================================

from fastapi import Form as _Form, File as _File, UploadFile as _UploadFile

@_v1.post("/ask/image", tags=["chat"])
async def ask_with_image(
    request:      Request,
    question:     str              = _Form(...),
    # Frontend appends every attached file under the repeated "image" form
    # key (fd.append("image", file) once per file). List[UploadFile] with
    # alias="image" binds ALL of them — previously this was a single
    # UploadFile, so only the first of N attached images was ever bound and
    # the rest were silently dropped by Starlette (this was the "only 1
    # image uploaded" bug). Scoped to this endpoint only.
    images:       List[_UploadFile] = _File(default=[], alias="image"),
    repo_filter:  str              = _Form(default=""),
    session_id:   str              = _Form(default=""),
    chat_id:      str              = _Form(default=""),
    model:        str              = _Form(default=""),   # "local" | provider name | ""
    local_model:  str              = _Form(default=""),   # actual local model name when model="local"
    image_ids:    List[str]        = _Form(default=[]),   # client-generated uuids: preview-cache key + attachment_id
    authorization: Optional[str]  = _Header(default=None),
):
    """
    Multipart endpoint for chat messages that may include an image attachment.

    Vision routing (configurable via env vars):
      - model is a LOCAL_VISION_MODELS entry (e.g. Kimi-k2.5, glm-4.5v)
          → in-house GPU proxy via OpenAI-compat API (no data leaves the network)
      - model is any other value / empty
          → PRIMARY_VISION_PROVIDER (default: gemini) with FALLBACK_VISION_PROVIDER
          → if LLM_PROXY_URL set: routed through proxy service (the LLM proxy server)
          → otherwise: called directly from this process (dev mode)

    If no images are attached: identical to POST /ask.
    Accepted image formats: image/jpeg, image/png, image/gif, image/webp.
    Max size: 10 MB per image.
    Multiple images may be attached (all under the repeated "image" form
    field); all are validated and persisted, but only the first is sent to
    the vision model — the local/Gemini/OpenAI vision call and the shared
    LLM proxy's /llm/generate-image endpoint all take a single inline image.
    """
    import base64 as _base64

    _MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB

    _ALLOWED_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}

    # Prefer client-supplied x-client-request-id for end-to-end tracing
    request_id = (request.headers.get("x-client-request-id") or "").strip() or str(uuid.uuid4())
    set_request_id(request_id)
    set_correlation_id(request_id)  # unconditional: avoid stale value on reused thread

    # ── Resolve identity: JWT → API key → 401 (no anonymous access) ──
    _user_id = None
    _jwt_tok = None
    if authorization and authorization.lower().startswith("bearer "):
        _jwt_tok = authorization[7:].strip()
    else:
        _jwt_tok = request.cookies.get("auth_token")
    if _jwt_tok:
        try:
            from auth.jwt_handler import decode_token as _dt_img
            _payload = _dt_img(_jwt_tok)
            if _payload:
                # JWT no longer contains "email" (DAST fix — PII removed from JWT)
                _user_id = _payload.get("sub")
        except Exception:
            pass
        if not _user_id:
            try:
                from auth.api_key_auth import is_api_key as _is_ak_img, resolve_api_key as _res_ak_img
                if _is_ak_img(_jwt_tok):
                    _kp = _res_ak_img(_jwt_tok)
                    if _kp:
                        _user_id = _kp["sub"]
            except Exception:
                pass
    if not _user_id:
        from fastapi.responses import JSONResponse as _JR_img
        return _JR_img(
            status_code=401,
            content={"error": "unauthorized", "detail": "Valid JWT or platform API key required."},
        )

    # ── Compliance gate on question text ──────────────────────
    from agents.compliance_engine import compliance_engine as _ce_img
    _chk = _ce_img.validate_input(question)
    question = _chk.get("redacted_text") or question  # use redacted version for LLM call
    if _chk.get("blocked"):
        _blocked_types = [f["type"] for f in _chk.get("findings", []) if f.get("blocked")]

        async def _compliance_stream():
            yield "data: " + json.dumps({"t": f"⛔ Request blocked: {', '.join(_blocked_types)}"}) + "\n\n"
            yield "data: " + json.dumps({"__meta__": {"tokens": 0, "in_tok": 0, "out_tok": 0, "cost": 0.0, "model": "compliance-gate", "latency": 0.0}}) + "\n\n"

        return StreamingResponse(
            _compliance_stream(),
            media_type="text/event-stream",
            headers={"X-Request-ID": request_id, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── No image → delegate to /ask JSON flow ─────────────────
    _images = [img for img in (images or []) if img is not None and img.filename]
    if not _images:
        class _FakeQ:
            pass
        _q = Question(
            question=question,
            repo_filter=repo_filter or None,
            session_id=session_id or None,
            chat_id=chat_id or None,
        )
        return ask_ai(_q, authorization=authorization)

    # ── Read + validate every attached image ───────────────────
    # SECURITY: content_type is set by the client and can be spoofed.
    # validate_image_upload() also inspects the actual file bytes (magic
    # header) so a renamed executable (e.g. evil.exe → evil.png) is rejected.
    from core.file_validator import validate_image_upload as _vimg

    _validated: list[dict] = []   # [{bytes, vr, mime, filename}, ...] — all valid attachments
    for _img in _images:
        _bytes = await _img.read()
        _vr = _vimg(
            filename=_img.filename or "image",
            content=_bytes,
            content_type=_img.content_type or "",
            max_size_bytes=_MAX_IMAGE_BYTES,
            caller="gateway/ask-image",
        )
        if not _vr.valid:
            from fastapi.responses import JSONResponse as _JR_mime
            _status = 413 if "size" in (_vr.error or "").lower() else 415
            return _JR_mime(
                status_code=_status,
                content={"error": "invalid_image", "detail": f"{_img.filename}: {_vr.error}"},
            )
        _validated.append({
            "bytes":    _bytes,
            "vr":       _vr,
            "mime":     _img.content_type or f"image/{_vr.extension}",
            "filename": _img.filename or f"image.{_vr.extension}",
        })

    # All attached images are validated and persisted (fixes the "only 1
    # image uploaded" bug — every image now survives refresh/reload).
    # Multi-image ANALYSIS (_all_img_b64s / _all_mimes below) is only wired
    # up for the LLM-proxy path (models/model_router.py's _ProxyGateway +
    # services/llm_proxy/*) — that's the only vision code path UAT/prod
    # actually execute, since they always run with LLM_PROXY_URL set. The
    # direct-mode gateway_gemini.py/gateway_openai.py functions (used only
    # when LLM_PROXY_URL is unset — true local/dev mode) and the in-house
    # local vision model path remain single-image-only intentionally: local
    # models were confirmed (during testing) to not genuinely support
    # multi-image/vision input reliably, and neither direct-mode path runs
    # in any environment this change targets.
    if len(_validated) > 1:
        logger.info(f"ask_with_image: {len(_validated)} images attached")
    _primary = _validated[0]
    _img_bytes = _primary["bytes"]
    _vr        = _primary["vr"]
    _mime      = _primary["mime"]

    _img_b64 = _base64.b64encode(_img_bytes).decode()
    _all_img_b64s  = [_base64.b64encode(_item["bytes"]).decode() for _item in _validated]
    _all_mimes     = [_item["mime"] for _item in _validated]
    _all_filenames = [_item["filename"] for _item in _validated]

    # ── Multi-image labelling ─────────────────────────────────
    # When more than one image is attached we augment the prompt with
    # per-image labels so the model can reference each image by name in its
    # response. This lets us parse the answer back into per-image captions
    # and store them on the correct ChatAttachment row for history replay.
    #
    # Label format: [Image 1: filename.png], [Image 2: filename.jpg], …
    # The system instruction asks the model to prefix each image's description
    # with the same label so we can split on it later.
    #
    # Single-image path is unchanged — no label injection, no parsing.
    _multi_image = len(_validated) > 1
    if _multi_image:
        _img_labels = [
            f"[Image {i+1}: {_all_filenames[i]}]"
            for i in range(len(_validated))
        ]
        _label_list = ", ".join(_img_labels)
        # Build a numbered list so the model sees the mapping clearly.
        _label_lines = "\n".join(
            f"  {i+1}. {_img_labels[i]}"
            for i in range(len(_validated))
        )
        # Build the example block showing ALL N images so the model knows
        # it must produce a labeled section for every one of them.
        _example_lines = "\n\n".join(
            f"{_img_labels[i]}\n<your description of image {i+1} here>"
            for i in range(len(_validated))
        )
        _labeled_question = (
            f"SYSTEM INSTRUCTION (follow exactly):\n"
            f"You are analysing {len(_validated)} images. "
            f"Each image has been assigned a unique label:\n"
            f"{_label_lines}\n\n"
            f"MANDATORY FORMAT RULE: You MUST begin your response for each image "
            f"with its exact label on its own line, then immediately follow with "
            f"your description of that image. Do NOT skip any label. "
            f"Do NOT merge descriptions across images. "
            f"Required format (all {len(_validated)} images must appear):\n"
            f"{_example_lines}\n\n"
            f"After describing all images individually, answer the user's question below.\n\n"
            f"User question: {question}"
        )
    else:
        _labeled_question = question

    # ── Route vision call ─────────────────────────────────────
    from core.model_registry import (
        PRIMARY_VISION_PROVIDER,
        FALLBACK_VISION_PROVIDER,
        LOCAL_VISION_MODELS,
        GEMINI_VISION_MODEL as _GEMINI_MODEL,
        MODEL_COST_PER_1M,
    )

    # Resolve the actual model name — mirrors the text /ask path convention:
    #   model="local" + local_model="Kimi-k2.5"  → _model_hint = "Kimi-k2.5"
    #   model="gemini-2.0-flash"                  → _model_hint = "gemini-2.0-flash"
    #   model="" (auto)                           → _model_hint = ""
    _raw_model  = (model or "").strip()
    _model_hint = (local_model or "").strip() if _raw_model == "local" else _raw_model
    _vision_label = _model_hint or PRIMARY_VISION_PROVIDER
    _in_tok = _out_tok = 0
    _start  = time.time()

    def _call_local_vision() -> tuple[str, int, int]:
        from gateway_local_llm import generate_with_image_local
        return generate_with_image_local(
            question, _img_b64, mime_type=_mime, model=_model_hint or None,
        )

    def _call_internet_vision(provider: str) -> tuple[str, int, int, str]:
        """Route to proxy (prod/UAT) or direct gateway (dev).
        Returns (text, in_tok, out_tok, actual_model) — actual_model may differ
        from provider when the proxy falls back internally (e.g. Gemini → OpenAI).

        Multi-image (_all_img_b64s/_all_mimes) is only wired up on the PROXY
        branch below — that's the only branch UAT/prod actually execute,
        since they always have LLM_PROXY_URL set (confirmed: whenever
        LLM_PROXY_URL is configured, this `if` is taken unconditionally and
        the direct-gateway `else` below is dead code for those environments).
        gateway_gemini.py / gateway_openai.py (the direct/dev-mode functions
        below) are intentionally left single-image-only — multi-image there
        would be dead code for any environment with LLM_PROXY_URL set.
        """
        _multi = len(_all_img_b64s) > 1
        _proxy = os.getenv("LLM_PROXY_URL", "").rstrip("/")
        if _proxy:
            from models.model_router import _ProxyGateway
            return _ProxyGateway(provider).generate_image(
                _labeled_question, _img_b64, mime_type=_mime,
                images_b64=_all_img_b64s if _multi else None,
                mime_types=_all_mimes if _multi else None,
            )
        # Dev / direct mode — no internal fallback, actual == requested.
        # Single-image only (see docstring above).
        if provider == "openai":
            from gateway_openai import generate_with_image_openai
            text, in_t, out_t = generate_with_image_openai(question, _img_b64, mime_type=_mime)
            return text, in_t, out_t, "openai"
        # Default: gemini
        from gateway_gemini import generate_with_image as _gwi_g
        text = _gwi_g(question, _img_b64, mime_type=_mime)
        return text, 0, 0, "gemini"

    try:
        if _model_hint and _model_hint in LOCAL_VISION_MODELS:
            # User explicitly selected a local vision model (Kimi-k2.5, glm-4.5v, …)
            _answer, _in_tok, _out_tok = _call_local_vision()
            _vision_label = _model_hint
        else:
            # Configurable internet provider — primary then fallback
            try:
                _answer, _in_tok, _out_tok, _actual = _call_internet_vision(PRIMARY_VISION_PROVIDER)
                _vision_label = _actual  # use what the proxy actually ran, not what we requested
            except Exception as _primary_err:
                if FALLBACK_VISION_PROVIDER and FALLBACK_VISION_PROVIDER.lower() != "none":
                    logger.warning(
                        f"ask_with_image primary ({PRIMARY_VISION_PROVIDER}) failed: "
                        f"{_primary_err!r} — trying fallback ({FALLBACK_VISION_PROVIDER})"
                    )
                    _answer, _in_tok, _out_tok, _actual = _call_internet_vision(FALLBACK_VISION_PROVIDER)
                    _vision_label = _actual
                else:
                    raise
    except Exception as _vision_err:
        logger.error(f"ask_with_image vision failed: {_vision_err}", exc_info=True)
        _answer = "Error processing image. Please try again."

    _latency = round(time.time() - _start, 3)

    # Cost estimation
    _cost_in, _cost_out = MODEL_COST_PER_1M.get(_GEMINI_MODEL, (0.075, 0.30))
    _img_cost = round((_in_tok * _cost_in + _out_tok * _cost_out) / 1_000_000, 6)

    # ── Persist ALL uploaded images SERVER-SIDE so previews survive
    # re-login / browser restart / cross-device (not just the browser cache).
    # Written to the SEPARATE uploads/images tree via ObjectStorage and
    # recorded as a ChatAttachment row keyed by the client's image_id, so
    # GET /chat/attachments/{id}/raw can serve it later. Fire-and-forget:
    # mirrors services/image_store.persist_generated_image — a failure here
    # must never break the vision response. Runs in a daemon thread so it
    # doesn't block the SSE stream.
    # NOTE: previously only the first image (and its id) was ever persisted
    # here — this loop now persists every validated attachment, zipped by
    # index with image_ids (frontend appends "image" and its matching
    # "image_ids" entry pairwise in the same loop iteration, so the two
    # lists stay index-aligned end to end).
    _persist_ids = [i for i in (image_ids or []) if i]
    if _persist_ids:
        _img_owner = _user_id or ""
        _img_chat  = chat_id or None

        # ── Build per-image description / caption from the vision answer ────
        # Single-image: the full answer is the description for that one image.
        # Multi-image: the labeled prompt asked the model to prefix each
        # image's section with its label ("[Image N: filename]"), so we split
        # the answer on those labels to extract per-image descriptions.
        # Each image gets its own image_description + image_caption stored on
        # its ChatAttachment row so the history-replay block in ask_ai can
        # inject the right caption for the right image on later turns.
        # Only populated when the answer looks like a real description (not an error).
        import re as _re_awicap

        def _make_caption(_text: str) -> str:
            """First 2 sentences of _text, hard-capped at 600 chars."""
            _src = " ".join(_text.split())
            _sents = _re_awicap.split(r'(?<=[.!?])\s+', _src)
            return (" ".join(_sents[:2]).strip() or _src)[:600]

        _vision_answer_for_caption = _answer or ""
        _is_error_answer = (
            not _vision_answer_for_caption
            or _vision_answer_for_caption.startswith("Error ")
        )

        # Build a list of (description, caption) aligned with _validated / _persist_ids.
        # Index i → description/caption for _validated[i].
        _per_img_desc_cap: list[tuple[str | None, str | None]] = []

        if _is_error_answer:
            # Error path: store nothing for any image.
            _per_img_desc_cap = [(None, None)] * len(_validated)
        elif not _multi_image:
            # Single image: the whole answer describes this one image.
            _desc = _vision_answer_for_caption
            _cap  = _make_caption(_desc)
            _per_img_desc_cap = [(_desc, _cap)]
            logger.info(
                f"[DOCTRACE] L1-img ask_with_image caption (single) | "
                f"desc_chars={len(_desc)} caption_chars={len(_cap)}"
            )
        else:
            # Multi-image: split the answer on the per-image labels we injected.
            # Label pattern: "[Image N: filename]" — escape the filename for regex.
            _label_pattern = "|".join(
                _re_awicap.escape(f"[Image {i+1}: {_all_filenames[i]}]")
                for i in range(len(_validated))
            )
            _sections = _re_awicap.split(f"({_label_pattern})", _vision_answer_for_caption)
            # _sections alternates: [pre-label-text, label, body, label, body, ...]
            # Build a map: label → body text.
            _label_to_body: dict[str, str] = {}
            _i = 0
            while _i < len(_sections):
                _tok = _sections[_i].strip()
                # Check if this token is one of our labels.
                _matched_idx = None
                for _li in range(len(_validated)):
                    if _tok == f"[Image {_li+1}: {_all_filenames[_li]}]":
                        _matched_idx = _li
                        break
                if _matched_idx is not None and _i + 1 < len(_sections):
                    _label_to_body[_matched_idx] = _sections[_i + 1].strip()
                    _i += 2
                else:
                    _i += 1

            for _li in range(len(_validated)):
                _body = _label_to_body.get(_li, "").strip()
                if _body:
                    _desc = _body
                    _cap  = _make_caption(_desc)
                    _per_img_desc_cap.append((_desc, _cap))
                    logger.info(
                        f"[DOCTRACE] L1-img ask_with_image caption (multi img {_li+1}/{len(_validated)}) | "
                        f"file={_all_filenames[_li]!r} desc_chars={len(_desc)} caption_chars={len(_cap)}"
                    )
                else:
                    # Model didn't produce a labeled section for this image —
                    # fall back to storing the full answer as a shared description.
                    _desc = _vision_answer_for_caption
                    _cap  = _make_caption(_desc)
                    _per_img_desc_cap.append((_desc, _cap))
                    logger.info(
                        f"[DOCTRACE] L1-img ask_with_image caption (multi fallback img {_li+1}) | "
                        f"file={_all_filenames[_li]!r} using full answer"
                    )

        def _persist_uploaded_images(
            _items: list[dict],
            _desc_caps: list[tuple[str | None, str | None]],
        ):
            from core.storage import storage as _obj_storage, UPLOAD_SUBDIR_IMAGE
            from db.database import SessionLocal as _SL
            from db.models import ChatAttachment as _CA
            for _idx, (_img_id, _item) in enumerate(_items):
                _desc, _cap = _desc_caps[_idx] if _idx < len(_desc_caps) else (None, None)
                try:
                    _fname = _item["filename"]
                    _sp = _obj_storage.save(
                        _item["bytes"],
                        _fname,
                        _item["mime"],
                        UPLOAD_SUBDIR_IMAGE,
                        _img_owner,
                        (_img_chat or ""),
                    )
                    _db = _SL()
                    try:
                        # Idempotent: skip if this id was already persisted.
                        if not _db.query(_CA).filter(_CA.id == _img_id).first():
                            _db.add(_CA(
                                id=_img_id,
                                chat_id=(_img_chat or ""),
                                user_id=_img_owner,
                                file_name=_fname,
                                file_type=_item["vr"].extension or "png",
                                file_size=len(_item["bytes"]),
                                kind="image",
                                storage_path=_sp,
                                parsed_text=_desc,
                                image_description=_desc,
                                image_caption=_cap,
                                created_by=_img_owner,
                            ))
                            _db.commit()
                    finally:
                        _db.close()
                    logger.info(
                        f"ask_with_image: persisted uploaded image {_img_id} "
                        f"({len(_item['bytes'])} bytes) -> {_sp}"
                    )
                except Exception as _pe:
                    logger.warning(f"ask_with_image: uploaded-image persist failed for {_img_id}: {_pe}")

        # Zip ids with validated items by index — pairs beyond the shorter
        # list's length are dropped (mirrors prior single-image behaviour
        # when id/file counts ever mismatched).
        _pairs = list(zip(_persist_ids, _validated))
        threading.Thread(
            target=_persist_uploaded_images,
            args=(_pairs, _per_img_desc_cap),
            daemon=True,
        ).start()

    # ── Persist the image turn so it survives a page refresh (mirrors the
    # text /ask path via _save_chat_messages). image_ids are stored in
    # ChatMessage.attachment_ids so the frontend rehydrates thumbnails from
    # the browser preview cache; the 🖼 marker lets the loader classify the
    # ids as images. Daemon thread so it doesn't block the SSE stream.
    if chat_id:
        _img_ids = [i for i in (image_ids or []) if i]
        _n_img = len(_img_ids) or 1
        _user_content = f"🖼 {_n_img} image{'s' if _n_img != 1 else ''}"
        if question:
            _user_content = f"{question}\n\n{_user_content}"
        threading.Thread(
            target=_save_chat_messages,
            kwargs=dict(
                chat_id=chat_id,
                user_id=_user_id or "",
                question=_user_content,
                answer=_answer,
                model=_vision_label,
                in_tok=_in_tok,
                out_tok=_out_tok,
                cost=_img_cost,
                language="",
                attachment_ids=_img_ids,
                project_id="",
                latency=_latency,
                title_hint=question or "Image chat",
                client_source="platform",
            ),
            daemon=True,
        ).start()

    # ── Stream result as SSE ──────────────────────────────────
    async def _image_stream():
        yield "data: " + json.dumps({"t": _answer}) + "\n\n"
        yield "data: " + json.dumps({
            "__meta__": {
                "tokens":  _in_tok + _out_tok,
                "in_tok":  _in_tok,
                "out_tok": _out_tok,
                "cost":    _img_cost,
                "model":   _vision_label,
                "latency": _latency,
            }
        }) + "\n\n"

    return StreamingResponse(
        _image_stream(),
        media_type="text/event-stream",
        headers={
            "X-Request-ID":      request_id,
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


class SubmitRequest(BaseModel):
    question:    str
    session_id:  Optional[str] = None
    chat_id:     Optional[str] = None
    repo_filter: Optional[str] = None
    model:       Optional[str] = None
    project_id:  Optional[str] = None
    attachment_ids: List[str]  = []
    rag_mode:    Optional[str] = None   # context isolation: "off" | "auto" | "on"


@_v1.post("/ask/submit", tags=["ai"])
def ask_submit(
    req: SubmitRequest,
    authorization: Optional[str] = _Header(default=None),
):
    """
    Enqueue a chat job and return job_id.  Returns in <10ms.
    Use GET /ask/stream/{job_id} to read the SSE token stream.
    """
    # ── Resolve identity: JWT → API key → 401 (no anonymous access) ──
    user_id  = None
    _uctx_submit: dict = {}
    _tok_submit = None
    if authorization and authorization.lower().startswith("bearer "):
        _tok_submit = authorization[7:].strip()
    if _tok_submit:
        try:
            from auth.jwt_handler import decode_token as _dt
            pl = _dt(_tok_submit)
            if pl:
                user_id = pl.get("sub") or pl.get("email")
                _uctx_submit = {
                    "user_id":    user_id,
                    "user_role":  pl.get("role", "user"),
                    "ad_level":   int(pl.get("ad_level") or 6),
                    "department": pl.get("department", "") or "",
                    "is_admin":   pl.get("role") == "admin",
                    "can_approve": bool(pl.get("can_approve", False)),
                    "org_id":     pl.get("org_id", ""),
                    "session_id": "",
                }
        except Exception:
            pass
        if not user_id:
            try:
                from auth.api_key_auth import is_api_key as _is_ak_sub, resolve_api_key as _res_ak_sub
                if _is_ak_sub(_tok_submit):
                    _kp = _res_ak_sub(_tok_submit)
                    if _kp:
                        user_id = _kp["sub"]
                        _uctx_submit = {
                            "user_id":    user_id,
                            "user_role":  _kp.get("role", "user"),
                            "ad_level":   int(_kp.get("ad_level") or 6),
                            "department": _kp.get("department", "") or "",
                            "is_admin":   _kp.get("role") == "admin",
                            "can_approve": bool(_kp.get("can_approve", False)),
                            "org_id":     _kp.get("org_id", ""),
                            "session_id": "",
                        }
            except Exception:
                pass
    if not user_id:
        from fastapi.responses import JSONResponse as _JR_sub
        return _JR_sub(
            status_code=401,
            content={"error": "unauthorized", "detail": "Valid JWT or platform API key required."},
        )

    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    # ── Back-pressure gate ────────────────────────────────────
    from core.job_queue import check_queue_pressure, Q_CHAT
    pressure = check_queue_pressure(Q_CHAT)
    if not pressure["allowed"]:
        raise HTTPException(
            status_code=503,
            detail={
                "error":       "system_busy",
                "queue":       Q_CHAT,
                "depth":       pressure.get("depth"),
                "retry_after": 10,
            },
        )

    # ── Enqueue ────────────────────────────────────────────────
    job_id     = str(uuid.uuid4())
    session_id = req.session_id or str(uuid.uuid4())
    chat_id    = req.chat_id    or session_id

    # ─── Per-chat KB scope (Phase 1 wiring, mirror of /ask path) ─────────────
    # Read product/domain/version/kb_doc_id off the Chat row and inject into
    # _uctx_submit so the async chat_worker uses the same scoped retrieval the
    # sync /ask path does. Server-side product validation is intentionally
    # lighter here (worker re-checks) — we don't fail-closed at enqueue.
    if req.chat_id and _uctx_submit:
        try:
            from db.database import SessionLocal as _SbSL
            from db.models import Chat as _SbChat
            _sbdb = _SbSL()
            try:
                _sb_row = _sbdb.query(_SbChat).filter(_SbChat.id == req.chat_id).first()
                if _sb_row is not None:
                    _sb_pid = getattr(_sb_row, "product_id",   None)
                    _sb_dom = getattr(_sb_row, "domain",       None)
                    _sb_ver = getattr(_sb_row, "spec_version", None)
                    _sb_did = getattr(_sb_row, "kb_doc_id",    None)
                    _sb_scope = {}
                    if _sb_pid: _sb_scope["product_id"]   = str(_sb_pid)
                    if _sb_dom: _sb_scope["domain"]       = _sb_dom
                    if _sb_ver: _sb_scope["spec_version"] = _sb_ver
                    if _sb_scope:
                        _uctx_submit["scope_filter"] = _sb_scope
                    if _sb_did:
                        _uctx_submit["kb_doc_id"] = str(_sb_did)
            finally:
                _sbdb.close()
        except Exception:
            pass

    from core.job_queue import enqueue_chat_job
    # Inject W3C traceparent so worker can attach its spans to this trace
    from core.telemetry import tracer as _tracer
    _trace_headers = _tracer.inject_headers()
    # Resolve rag_mode: body → stored chat record → default "off"
    _submit_rag_mode = (req.rag_mode or "").strip().lower() if req.rag_mode else ""
    if _submit_rag_mode not in {"off", "auto", "on"}:
        _submit_rag_mode = ""
    if not _submit_rag_mode and chat_id:
        try:
            from db.database import SessionLocal as _SL_sub
            from db.models import Chat as _Chat_sub
            _sdb = _SL_sub()
            try:
                _sc = _sdb.query(_Chat_sub).filter(_Chat_sub.id == chat_id).first()
                if _sc:
                    _stored_sub = (getattr(_sc, "rag_mode", None) or "").strip().lower()
                    if _stored_sub in {"off", "auto", "on"}:
                        _submit_rag_mode = _stored_sub
            finally:
                _sdb.close()
        except Exception:
            pass
    if not _submit_rag_mode:
        _submit_rag_mode = "off"

    enqueue_chat_job(
        job_id=job_id,
        question=question,
        session_id=session_id,
        chat_id=chat_id,
        repo_filter=req.repo_filter,
        model=req.model,
        user_id=user_id,
        user_ctx=_uctx_submit or None,
        request_id=job_id,
        trace_headers=_trace_headers,
        rag_mode=_submit_rag_mode,
        attachment_ids=list(req.attachment_ids or []),
    )

    return {"job_id": job_id, "session_id": session_id}


@_v1.get("/ask/stream/{job_id}", tags=["ai"])
async def ask_stream(job_id: str, request: Request):
    """
    Read the Redis Stream for job_id and forward as SSE.

    Terminates on:
      - __done__ sentinel from worker
      - __error__ sentinel from worker
      - client disconnect
      - 120s hard timeout
    """
    from fastapi import Request as _Req
    from fastapi.responses import StreamingResponse as _SR
    import asyncio
    from core.kv import async_get_kv
    from core.config import RDB_STREAM

    # Backend-agnostic async stream client, resolved at runtime from
    # REDIS_CLIENT_CONFIG_DB6 (REDIS → redis.asyncio).
    _r_stream = await async_get_kv(RDB_STREAM, decode_responses=True)
    stream_key = f"chat:stream:{job_id}"

    async def _sse_gen():
        last_id    = "0"
        deadline   = asyncio.get_event_loop().time() + 120   # 120s hard cap
        block_ms   = 5000                                     # 5s per XREAD call

        try:
            while True:
                # ── Client disconnect check ─────────────────────────────
                if await request.is_disconnected():
                    break

                # ── Hard timeout ────────────────────────────────────────
                if asyncio.get_event_loop().time() > deadline:
                    yield "data: " + json.dumps({"t": "\n[response timeout]"}) + "\n\n"
                    break

                # ── XREAD with short block ───────────────────────────────
                try:
                    results = await _r_stream.xread(
                        {stream_key: last_id}, count=100, block=block_ms
                    )
                except Exception as _re:
                    logger.warning(f"ask_stream XREAD error: {_re}")
                    await asyncio.sleep(0.5)
                    continue

                if not results:
                    # No new messages in block_ms — check if job is still alive
                    try:
                        from core.job_queue import get_job_status, _rq_available
                        if _rq_available:
                            st = get_job_status(job_id)
                            status_val = st.get("status", "")
                            if status_val in ("failed", "stopped", "canceled", "cancelled"):
                                yield "data: " + json.dumps({"t": "\n[worker error — job did not complete]"}) + "\n\n"
                                break
                    except Exception:
                        pass
                    continue   # still waiting — loop back

                _, messages = results[0]
                for msg_id, fields in messages:
                    last_id = msg_id
                    msg_type = fields.get("type", "")

                    if msg_type == "__done__":
                        # Emit __meta__ if present
                        if fields.get("meta"):
                            try:
                                meta = json.loads(fields["meta"])
                                yield "data: " + json.dumps({"__meta__": meta}) + "\n\n"
                            except Exception:
                                pass
                        return

                    if msg_type == "__error__":
                        err_msg = fields.get("msg", "unknown error")
                        yield "data: " + json.dumps({"t": f"\n[error: {err_msg}]"}) + "\n\n"
                        return

                    if msg_type == "chunk":
                        chunk = fields.get("data", "")
                        if chunk:
                            yield "data: " + json.dumps({"t": chunk}) + "\n\n"

        finally:
            await _r_stream.aclose()

    return StreamingResponse(
        _sse_gen(),
        media_type="text/event-stream",
        headers={
            "X-Job-ID":          job_id,
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Index endpoints ───────────────────────────────────────────

class IndexRequest(BaseModel):
    repo_name:    str
    repo_path:    str
    drop_index:   bool      = False
    file_filter:  List[str] = []


@_v1.post("/index/submit", tags=["index"])
def index_submit(
    req: IndexRequest,
    authorization: Optional[str] = _Header(default=None),
    _user: dict = Depends(_require_auth),
):
    """Enqueue a codebase indexing job. Returns {job_id}."""
    user_id = "system"
    if authorization and authorization.lower().startswith("bearer "):
        try:
            from auth.jwt_handler import decode_token as _dt2
            pl = _dt2(authorization[7:].strip())
            if pl:
                user_id = pl.get("sub") or pl.get("email") or "system"
        except Exception:
            pass

    # RBAC — operator or higher can trigger indexing
    from auth.rbac import require_permission as _rp
    _caller_role = _user.get("role", "viewer")
    from auth.rbac import has_permission as _hp
    if not _hp(_caller_role, "codebase:write"):
        raise HTTPException(status_code=403, detail="codebase:write permission required (operator+)")

    from core.job_queue import check_queue_pressure, enqueue_index_job, Q_INDEX
    pressure = check_queue_pressure(Q_INDEX)
    if not pressure["allowed"]:
        raise HTTPException(status_code=503, detail={"error": "index_queue_full", **pressure})

    job_id = enqueue_index_job(
        repo_name=req.repo_name,
        repo_path=req.repo_path,
        triggered_by=user_id,
        drop_index=req.drop_index,
        file_filter=req.file_filter or None,
    )
    return {"job_id": job_id, "repo_name": req.repo_name}


# SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
# this endpoint previously had no auth dependency at all, exposing internal
# codebase-indexing status/metadata for any repo name to any anonymous
# caller, while its sibling POST /index/submit already requires a verified
# caller (plus codebase:write).
# Fix: added `_user: dict = Depends(_require_auth)` as a function
# parameter so FastAPI rejects unauthenticated requests with 401 before
# the handler runs. No extra permission check added (read-only, unlike
# the write endpoint above) — any authenticated user may still poll status.
@_v1.get("/index/{repo_name}/status", tags=["index"])
def index_status(repo_name: str, _user: dict = Depends(_require_auth)):
    """Return indexing status for a repo (polls repo_index_status table)."""
    try:
        from db.database import SessionLocal as _ISess
        from sqlalchemy import text as _sqlt
        db = _ISess()
        try:
            row = db.execute(
                _sqlt("SELECT * FROM repo_index_status WHERE repo_name = :r"),
                {"r": repo_name},
            ).fetchone()
            if not row:
                return {"repo_name": repo_name, "status": "not_indexed"}
            keys = row._fields if hasattr(row, "_fields") else row.keys()
            return dict(zip(keys, row))
        finally:
            db.close()
    except Exception as exc:
        return {"repo_name": repo_name, "status": "unknown", "error": str(exc)}


app.include_router(_v1, prefix="/ainxt/v1/api")

# ============================================================
# STATIC FRONTEND  (MUST be last — registered after ALL API routes)
#
# @app.get("/{full_path:path}") matches every GET path.
# FastAPI matches routes in registration order, so the SPA catch-all
# MUST come after every specific API route or it will shadow them.
# ============================================================

import os as _os

# ── ABStudio (Build Studio) — serve built frontend ───────────────────────────
_abs_dist = _os.path.join(_os.path.dirname(__file__), "ABStudio", "frontend", "dist")
if _os.path.isdir(_abs_dist):
    from fastapi.staticfiles import StaticFiles as _SF_abs
    from fastapi.responses import FileResponse as _FR_abs

    _abs_assets = _os.path.join(_abs_dist, "assets")
    if _os.path.isdir(_abs_assets):
        app.mount("/ainxt/v1/api/abs/assets", _SF_abs(directory=_abs_assets, html=False), name="abs-assets")

    @app.get("/build-studio", include_in_schema=False)
    @app.get("/build-studio/{full_path:path}", include_in_schema=False)
    async def _serve_abs_spa(full_path: str = ""):
        candidate = _os.path.join(_abs_dist, full_path)
        if full_path and _os.path.isfile(candidate):
            return _FR_abs(candidate)
        resp = _FR_abs(_os.path.join(_abs_dist, "index.html"))
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"]        = "no-cache"
        resp.headers["Expires"]       = "0"
        return resp

    logger.info(f"[ABStudio] frontend served from {_abs_dist} at /build-studio")

# ── AiNxt CLI binary distribution (/static/ainxt-*, /static/install-ainxt.sh)
_static_dir = _os.path.join(_os.path.dirname(__file__), "static")
if _os.path.isdir(_static_dir):
    from fastapi.staticfiles import StaticFiles as _SF_static
    app.mount("/static", _SF_static(directory=_static_dir, html=False), name="cli-static")
    logger.info(f"Serving AiNxt CLI binaries from {_static_dir}")
else:
    _os.makedirs(_static_dir, exist_ok=True)

_ui_dist = _os.path.join(_os.path.dirname(__file__), "ai-ui", "dist")
if _os.path.isdir(_ui_dist):
    from fastapi.staticfiles import StaticFiles as _SF
    from fastapi.responses import FileResponse as _FR

    _assets_dir = _os.path.join(_ui_dist, "assets")
    if _os.path.isdir(_assets_dir):
        app.mount("/assets", _SF(directory=_assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _serve_spa(full_path: str):
        from fastapi.responses import Response as _Resp
        candidate = _os.path.join(_ui_dist, full_path)
        if full_path and _os.path.isfile(candidate):
            # Named static file (e.g. sw.js, manifest.json) — let
            # NoCacheMiddleware decide the headers based on the path.
            return _FR(candidate)
        # SPA fallback → always serve fresh index.html.
        # NoCacheMiddleware applies no-store, but set it explicitly too
        # so it's impossible to accidentally bypass.
        resp = _FR(_os.path.join(_ui_dist, "index.html"))
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"]        = "no-cache"
        resp.headers["Expires"]       = "0"
        return resp

    logger.info(f"Serving built frontend from {_ui_dist}")
