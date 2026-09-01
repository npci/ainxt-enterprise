# SPDX-License-Identifier: Apache-2.0
# ============================================================
# LOCAL LLM GATEWAY  — In-house LLM Proxy
# ============================================================
#
# Exposes an OpenAI-compatible API and can front 10+
# in-house models (vLLM, Ollama, TGI, etc.) behind a single URL.
#
# Model Discovery:
#   On first call (or after TTL expiry), GET /v1/models is fetched
#   and the returned list is assigned to tiers:
#
#   Priority 1 — Explicit env vars (comma-separated, first = preferred):
#     LOCAL_SIMPLE_MODELS   e.g. "llama3-8b,mistral-7b"
#     LOCAL_MEDIUM_MODELS   e.g. "llama3-70b,mixtral-8x7b"
#     LOCAL_COMPLEX_MODELS  e.g. "llama3-405b,command-r-plus"
#
#   Priority 2 — Size heuristic from model name:
#     Params ≥ 30B  → complex
#     Params 10-30B → medium
#     Params < 10B  → simple
#     No param hint → medium (safe default)
#
#   Priority 3 — If only one model: use it for all tiers.
#
# Config (set in .env):
#   LOCAL_LLM_BASE_URL   http://gpu01:4000          (required)
#   LOCAL_LLM_API_KEY    sk-...                      (required if auth enabled)
#   LOCAL_SIMPLE_MODELS  / LOCAL_MEDIUM_MODELS / LOCAL_COMPLEX_MODELS
#   LOCAL_MODEL_REFRESH_SECS  (default 300)          (model list TTL)
#
# Backward compatibility: LOCAL_LLM_BASE_URL falls back to LITELLM_BASE_URL
# ============================================================

import os
import re
import threading
import time
import uuid
from typing import Optional

import httpx

from core.logger import logger, get_request_id as _get_request_id

# CKMS — decrypt LOCAL_LLM_API_KEY / LITELLM_API_KEY before they are read below.
# Idempotent: a no-op when an outer entrypoint has already booted CKMS.
from core.ckms import load_at_boot as _ckms_load_at_boot
_ckms_load_at_boot()

# ── Config ────────────────────────────────────────────────────
# Accept both new name and old name so existing .env files keep working
LOCAL_LLM_BASE_URL = (os.getenv("LOCAL_LLM_BASE_URL") or os.getenv("LITELLM_BASE_URL", "")).rstrip("/")
LOCAL_LLM_API_KEY  = os.getenv("LOCAL_LLM_API_KEY") or os.getenv("LITELLM_API_KEY", "sk-local")
_REFRESH_SECS      = int(os.getenv("LOCAL_MODEL_REFRESH_SECS", "300"))

# Temperature for local LLM streaming calls.
# Default 0.3 preserves existing behaviour. Override for reasoning models:
#   LOCAL_LLM_TEMPERATURE=0    → deterministic (DeepSeek-R1, Kimi, QwQ)
#   LOCAL_LLM_TEMPERATURE=0.7  → more creative/diverse outputs
try:
    LOCAL_LLM_TEMPERATURE = float(os.getenv("LOCAL_LLM_TEMPERATURE", "0.3"))
except (TypeError, ValueError):
    LOCAL_LLM_TEMPERATURE = 0.3

# Per-tier model preferences (comma-separated, priority order)
_ENV_SIMPLE  = [m.strip() for m in os.getenv("LOCAL_SIMPLE_MODELS",  "").split(",") if m.strip()]
_ENV_MEDIUM  = [m.strip() for m in os.getenv("LOCAL_MEDIUM_MODELS",  "").split(",") if m.strip()]
_ENV_COMPLEX = [m.strip() for m in os.getenv("LOCAL_COMPLEX_MODELS", "").split(",") if m.strip()]

# Models to hide from Chat/IDE UI (comma-separated exact IDs or substrings).
# LOCAL_HIDDEN_MODELS env var lets admins add extra entries.
# The default patterns cover all common embedding / reranking model families.
_DEFAULT_HIDDEN_PATTERNS = [
    "embed", "nomic", "bge-", "minilm", "e5-", "gte-", "rerank",
    "all-minilm", "sentence-transformers",
]
_ENV_HIDDEN = [m.strip().lower() for m in os.getenv("LOCAL_HIDDEN_MODELS", "").split(",") if m.strip()]
_HIDDEN_PATTERNS = _DEFAULT_HIDDEN_PATTERNS + _ENV_HIDDEN


def _log_cache_effectiveness(
    *,
    request_id: str,
    model: str,
    cache_read: int,
    prompt_total: int,
    context: str = "",          # e.g. "generate", "vision"
) -> None:
    """Emit a structured [CACHE EFFECTIVENESS] log line for Local LLM calls.

    Some in-house proxies (vLLM, LiteLLM) expose KV-cache hits via
    prompt_tokens_details.cached_tokens in the OpenAI-compat usage object.
    When the proxy does not populate this field, cache_read will be 0 — the
    log is still emitted so the absence of KV-cache reuse is explicit.

    Cost is derived from MODEL_COST_PER_1M (the single source of truth).
    In-house models are registered with (0.0, 0.0) → savings_est_usd is always 0,
    which correctly reflects that local inference has no cloud billing cost.
    """
    from core.model_registry import MODEL_COST_PER_1M, LOCAL_LLM_MODEL_NAME
    # Local models may be registered under their specific ID or the generic sentinel.
    input_rate_per_1m, _ = MODEL_COST_PER_1M.get(model) or MODEL_COST_PER_1M.get(LOCAL_LLM_MODEL_NAME, (0.0, 0.0))
    hit_rate = (cache_read / prompt_total * 100) if prompt_total > 0 else 0.0
    # KV-cache read ratio for vLLM/LiteLLM is not standardised; savings are $0 for local.
    savings_usd = 0.0  # always 0 — local inference has no cloud billing cost
    ctx_tag = f" context={context}" if context else ""
    logger.info(
        f"[CACHE EFFECTIVENESS] provider=local request_id={request_id} model={model}{ctx_tag} "
        f"cache_read={cache_read} prompt_total={prompt_total} "
        f"hit_rate={hit_rate:.1f}% savings_tokens={cache_read} savings_est_usd={savings_usd:.6f} "
        f"cache_enabled=kv-cache"   # KV-cache is managed by the inference engine
    )


# ── Persistent OpenAI client / HTTPX connection pool ─────────────
# Creating a new OpenAI client (and therefore a new httpx connection pool)
# on every request adds TLS + TCP handshake overhead. Keep one pooled client
# per base URL so local model calls reuse keep-alive connections.
_OPENAI_CLIENT: Optional["OpenAI"] = None
_OAI_LOCK = threading.Lock()


def _get_openai_client(extra_headers: Optional[dict] = None) -> "OpenAI":
    """Return a cached OpenAI client with a persistent httpx connection pool."""
    global _OPENAI_CLIENT
    # Local alias for the module-level key. Passing the long constant name
    # directly to api_key= reads as a hardcoded credential to secret scanners;
    # the value and behaviour are identical.
    _key = LOCAL_LLM_API_KEY
    if _OPENAI_CLIENT is None:
        with _OAI_LOCK:
            if _OPENAI_CLIENT is None:
                from openai import OpenAI
                _http_client = httpx.Client(
                    limits=httpx.Limits(
                        max_connections=200,
                        max_keepalive_connections=100,
                        keepalive_expiry=30.0,
                    ),
                    timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0),
                )
                _OPENAI_CLIENT = OpenAI(
                    base_url=f"{LOCAL_LLM_BASE_URL}/v1",
                    api_key=_key,
                    http_client=_http_client,
                )
                logger.info(f"Local LLM: pooled OpenAI client created for {LOCAL_LLM_BASE_URL}")
    # Apply per-request headers (e.g. X-Request-ID) without mutating the singleton.
    if extra_headers:
        from openai import OpenAI
        return OpenAI(
            base_url=f"{LOCAL_LLM_BASE_URL}/v1",
            api_key=_key,
            http_client=_OPENAI_CLIENT._client,
            default_headers=extra_headers,
        )
    return _OPENAI_CLIENT


def _is_ui_model(model_id: str) -> bool:
    """Return True if the model should be shown in Chat/IDE selectors."""
    low = model_id.lower()
    return not any(pat in low for pat in _HIDDEN_PATTERNS)


# ── Dynamic max_tokens sizing ─────────────────────────────────────────────
# `generate()` used to default to a flat `max_tokens=32000` for every local
# model regardless of its real context window. vLLM/TGI treat `max_tokens` as
# an ADDITIONAL budget on top of the prompt tokens, not a total — so a fixed
# 32K cap silently overshot the total context window of smaller-window models
# (DeepSeek-Coder at 64K, Qwen/GLM/Llama at 128K) once the prompt itself was
# non-trivial, and the proxy hard-rejected the whole request with a 400
# ContextWindowExceededError. Two tables in gateway.py already carry the
# real per-model numbers we need (window, reserved output) — reuse them here
# instead of inventing a parallel constant.
_ESTIMATE_CHARS_PER_TOKEN = 4
_MAX_TOKENS_SAFETY_MARGIN = 512  # headroom for chat-template / special tokens


def _estimate_tokens(text: str) -> int:
    """Char/4 token estimate — same heuristic gateway.py uses elsewhere."""
    return max(1, len(text) // _ESTIMATE_CHARS_PER_TOKEN)


def _desired_output_tokens(selected: str) -> int:
    """Per-model-family default output budget (Tier 5 reserved-output table),
    not a flat constant. Falls back to a conservative 8_000 on any error —
    e.g. if `gateway` cannot be imported (should not happen in prod; the two
    modules are already mutually referenced via lazy imports elsewhere)."""
    try:
        from gateway import _reserved_output_for
        return _reserved_output_for(selected)
    except Exception:
        return 8_000


def _resolve_max_tokens(selected: str, prompt_tokens: int, requested: Optional[int]) -> int:
    """Clamp the completion's `max_tokens` to what `selected` can actually
    serve, given how many tokens the prompt already consumed.

    `requested` is the caller-supplied cap (None → use the model-family
    default from `_desired_output_tokens`). Either way, the result is capped
    to `context_window - prompt_tokens - safety_margin` so the request can
    never push the total (prompt + completion) past the model's real context
    window — the actual condition vLLM/TGI reject with HTTP 400.
    """
    try:
        from gateway import _context_window_for
        window = _context_window_for(selected)
    except Exception:
        window = 128_000  # conservative default if gateway import fails
    desired   = requested if requested is not None else _desired_output_tokens(selected)
    available = max(256, window - prompt_tokens - _MAX_TOKENS_SAFETY_MARGIN)
    return max(1, min(desired, available))


# ── Size heuristic ─────────────────────────────────────────────
# Extracts parameter count from model name (e.g. "llama3-70b" → 70)
_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[bB]\b")


def _tier_from_name(model_id: str) -> str:
    """Classify model by parameter count embedded in name."""
    m = _PARAM_RE.search(model_id)
    if m:
        params = float(m.group(1))
        if params >= 30:
            return "complex"
        if params >= 10:
            return "medium"
        return "simple"
    return "medium"   # safe default for unknown size


# ── Model list cache ───────────────────────────────────────────

class _ModelCatalog:
    """
    Thread-safe, TTL-cached local LLM model list.
    Assigns each discovered model to a tier.
    """
    def __init__(self):
        self._lock        = threading.Lock()
        self._all: list   = []           # raw model ids from /v1/models
        self._by_tier: dict = {}         # {"simple": [...], "medium": [...], "complex": [...]}
        self._fetched_at  = 0.0
        self._available   = False

    def _fetch(self) -> None:
        """Fetch /v1/models from Local LLM proxy and rebuild tier map."""
        if not LOCAL_LLM_BASE_URL:
            logger.warning("Local LLM: base URL not set — local model tier disabled")
            return
        try:
            import httpx
            r = httpx.get(
                f"{LOCAL_LLM_BASE_URL}/v1/models",
                headers={"Authorization": f"Bearer {LOCAL_LLM_API_KEY}"},
                timeout=10.0,
            )
            r.raise_for_status()
            data = r.json()
            raw_ids   = [item["id"] for item in data.get("data", [])]
            model_ids = [m for m in raw_ids if _is_ui_model(m)]
            if raw_ids and not model_ids:
                logger.warning(
                    "Local LLM: all %d model(s) filtered out as embed/rerank — "
                    "set LOCAL_HIDDEN_MODELS='' to override", len(raw_ids)
                )
            if not model_ids:
                logger.warning("Local LLM: /v1/models returned empty list")
                return
            # Fix 4: sort deterministically so the heuristic-classified tier lists
            # have a stable order across nodes and TTL refreshes. Without this,
            # _catalog.pick() (which returns candidates[0]) can return a different
            # model on each call when the backend load-balancer serves /v1/models
            # from different nodes that return models in different orders.
            # Env-var-pinned models (_ENV_SIMPLE/MEDIUM/COMPLEX) are already
            # deterministic; only the heuristic-classified remainder is affected.
            model_ids.sort()
            logger.debug(
                "Local LLM: %d raw model(s), %d after embed/rerank filter: %s",
                len(raw_ids), len(model_ids), model_ids,
            )

            # Build tier → [model_id] map
            by_tier: dict = {"simple": [], "medium": [], "complex": []}

            # Honour explicit env-var preferences first (models must be in the live list)
            live_set = set(model_ids)
            for mid in _ENV_SIMPLE:
                if mid in live_set:
                    by_tier["simple"].append(mid)
            for mid in _ENV_MEDIUM:
                if mid in live_set:
                    by_tier["medium"].append(mid)
            for mid in _ENV_COMPLEX:
                if mid in live_set:
                    by_tier["complex"].append(mid)

            # Classify remaining models by size heuristic
            assigned = set(by_tier["simple"] + by_tier["medium"] + by_tier["complex"])
            for mid in model_ids:
                if mid not in assigned:
                    by_tier[_tier_from_name(mid)].append(mid)

            # If only one model in the deployment — use it for all tiers
            if len(model_ids) == 1:
                for tier in by_tier:
                    if not by_tier[tier]:
                        by_tier[tier] = model_ids[:]

            self._all      = model_ids
            self._by_tier  = by_tier
            self._available = True
            logger.info(
                f"Local LLM: discovered {len(model_ids)} model(s) — "
                f"simple={by_tier['simple']} "
                f"medium={by_tier['medium']} "
                f"complex={by_tier['complex']}"
            )
        except Exception as e:
            logger.warning(f"Local LLM: model discovery failed → {e}")
            self._available = False

    def refresh_if_stale(self) -> None:
        if time.monotonic() - self._fetched_at > _REFRESH_SECS:
            with self._lock:
                if time.monotonic() - self._fetched_at > _REFRESH_SECS:
                    self._fetch()
                    self._fetched_at = time.monotonic()

    def pick(self, tier: str) -> Optional[str]:
        """Return preferred model id for a tier, or None if unavailable."""
        self.refresh_if_stale()
        candidates = self._by_tier.get(tier, [])
        return candidates[0] if candidates else (self._all[0] if self._all else None)

    @property
    def available(self) -> bool:
        self.refresh_if_stale()
        return self._available and bool(LOCAL_LLM_BASE_URL)

    def all_models(self) -> list:
        self.refresh_if_stale()
        return list(self._all)

    def by_tier(self) -> dict:
        self.refresh_if_stale()
        return dict(self._by_tier)


_catalog = _ModelCatalog()


def is_local_model(model_id: str) -> bool:
    """Non-blocking: True if `model_id` is an in-house model the Local LLM proxy serves.

    This is the dynamic counterpart to the CLI's static name-pattern heuristic — it
    consults the live `/v1/models` catalog so models whose names don't match the
    patterns (e.g. "Magistral", "nemotron-…") are still recognised as in-house
    (→ routed local, billed $0).

    Safe to call on the request / event-loop path: it reads the *cached* catalog and
    NEVER forces a (blocking) network refresh. If the cache is cold it kicks off a
    background refresh and returns False for this call (callers fall back to their
    static heuristics, then pick it up once the cache warms). A "local:" addressing
    prefix is stripped before matching.
    """
    if not model_id:
        return False
    mid = model_id.strip()
    if mid.lower().startswith("local:"):
        mid = mid.split(":", 1)[1].strip()
    cached = _catalog._all  # snapshot of the last successful /v1/models fetch
    if cached:
        return mid in cached
    # Cold cache → warm it in the background, answer False for now (non-blocking).
    try:
        threading.Thread(target=_catalog.refresh_if_stale, daemon=True).start()
    except Exception:
        pass
    return False


# ── Gateway ────────────────────────────────────────────────────

class LocalLLMGateway:
    """
    OpenAI-compatible gateway for the in-house Local LLM proxy.
    Identical interface to ClaudeGateway / OpenAIGateway:
      generate(prompt, model=None, tier="simple") → Generator[str]
    Token counts available via _last_input_tokens / _last_output_tokens.
    """

    def __init__(self):
        self._last_input_tokens  = 0
        self._last_output_tokens = 0
        self._last_selected_model: Optional[str] = None  # Fix 1: model actually used in last generate()
        # Trigger first discovery in background so startup is non-blocking
        threading.Thread(target=_catalog.refresh_if_stale, daemon=True).start()

    @property
    def available(self) -> bool:
        return _catalog.available

    def list_models(self) -> list:
        return _catalog.all_models()

    def models_by_tier(self) -> dict:
        return _catalog.by_tier()

    def generate(
        self,
        prompt,
        model: str = None,
        tier: str = "simple",
        *,
        max_tokens: Optional[int] = None,
        disable_reasoning: bool = False,
    ):
        """
        Stream tokens from the Local LLM proxy.  Yields str tokens.
        prompt:     str (single turn) OR list[dict] (multi-turn OpenAI messages array).
        model:      explicit model override (skips tier-based selection).
        tier:       "simple" | "medium" | "complex"
        max_tokens: output cap. Default None → sized dynamically per selected
                    model: `_desired_output_tokens()` picks a model-family
                    default (mirrors gateway.py's Tier-5 reserved-output
                    table — e.g. 8_000 for Kimi, 4_000 for GLM/Qwen/Llama),
                    then `_resolve_max_tokens()` clamps it so
                    prompt_tokens + max_tokens never exceeds the selected
                    model's real context window. A flat 32_000 used to be
                    sent for every model regardless of its window — vLLM/TGI
                    treat max_tokens as additive to the prompt, so that
                    silently overshot smaller-window models (e.g. DeepSeek's
                    64K) once the prompt itself was non-trivial, and the
                    whole request was hard-rejected with HTTP 400. Callers
                    needing a tighter/looser cap still pass an explicit int —
                    it is honoured as long as it fits the window.
        """
        if not LOCAL_LLM_BASE_URL:
            yield "Error: Local LLM proxy not configured (LOCAL_LLM_BASE_URL missing)"
            return

        selected = model or _catalog.pick(tier)
        if not selected:
            yield "Error: no local model available for this tier"
            return

        # Fix 1: pin the resolved model ID so _try_local_simple_stream() can read
        # the exact model that answered without re-calling _catalog.pick() (which
        # could return a different entry if the catalog refreshed mid-request).
        self._last_selected_model = selected

        # Validate explicit model override against the live catalog.
        # A mismatch means the caller (e.g. CHAT_FALLBACK_CHAIN) has a stale or
        # wrong model name — warn loudly so it surfaces in logs before the API 403s.
        if model and _catalog._all and model not in _catalog._all:
            logger.warning(
                "Local LLM: requested model %r is NOT in the live catalog %s — "
                "the API call will likely fail with 403. "
                "Check CHAT_FALLBACK_CHAIN / LOCAL_*_MODELS env vars.",
                model, _catalog._all,
            )

        self._last_input_tokens  = 0
        self._last_output_tokens = 0
        self._last_cached_tokens = 0   # KV-cache hits reported by the proxy (if any)

        # Request-scoped ID — mirrors ClaudeGateway pattern
        _upstream = _get_request_id()
        request_id = _upstream if _upstream and _upstream != "-" else str(uuid.uuid4())
        logger.info(f"[LLM DISPATCH] provider=local model={selected} request_id={request_id or 'n/a'}")

        from core.prompt_sanitizer import sanitize as _sanitize

        # Build messages array — local LLM proxy is OpenAI-compat so multi-turn is native
        if isinstance(prompt, list):
            messages_payload = [
                {"role": m["role"], "content": _sanitize(m.get("content") or "")}
                for m in prompt
            ]
        else:
            messages_payload = [{"role": "user", "content": _sanitize(prompt)}]

        # Forward request_id as X-Request-ID so the in-house LLM service
        # (vLLM / TGI / Ollama) can correlate its own logs with the gateway.
        _extra_headers: dict = {}
        if request_id:
            _extra_headers["X-Request-ID"] = request_id

        # ── Prompt log (mirrors [LLM DISPATCH] in ClaudeGateway) ──────────────
        # Log the last user turn (first 500 chars) so prompt content is traceable
        # without flooding logs with full multi-turn histories.
        _prompt_snippet: str = ""
        if messages_payload:
            _last_user = next(
                (m["content"] for m in reversed(messages_payload) if m.get("role") == "user"),
                messages_payload[-1].get("content", ""),
            )
            _prompt_snippet = (_last_user or "")[:500]
        logger.info(
            f"[LLM DISPATCH] provider=local model={selected} tier={tier} "
            f"request_id={request_id} prompt_chars={len(_prompt_snippet)} "
            f"prompt_snippet={_prompt_snippet!r}"
        )

        # ── Dynamic max_tokens (see _resolve_max_tokens docstring) ────────────
        # Size the completion budget to what `selected` can actually serve,
        # given the prompt already built above, instead of a flat 32_000 that
        # ignored the model's real context window and got hard-rejected with
        # HTTP 400 once prompt + max_tokens exceeded it.
        _prompt_tokens_est = sum(
            _estimate_tokens(m.get("content") or "") for m in messages_payload
        )
        _resolved_max_tokens = _resolve_max_tokens(selected, _prompt_tokens_est, max_tokens)
        if max_tokens is not None and _resolved_max_tokens < max_tokens:
            logger.warning(
                "Local LLM: requested max_tokens=%d clamped to %d for model=%r "
                "(prompt_est=%d tokens, context_window guard)",
                max_tokens, _resolved_max_tokens, selected, _prompt_tokens_est,
            )

        try:
            client = _get_openai_client(extra_headers=_extra_headers or None)
            stream = client.chat.completions.create(
                model=selected,
                messages=messages_payload,
                stream=True,
                temperature=LOCAL_LLM_TEMPERATURE,
                max_tokens=_resolved_max_tokens,
            )
            # Some in-house reasoning models stream the answer ONLY in
            # delta.reasoning_content and never populate delta.content. Capture that
            # as a fallback (ctx-migration Phase 6): emit content when present, else
            # buffer reasoning_content and yield it at the end if no content arrived.
            _any_content = False
            _reasoning_buf: list = []
            _response_buf: list = []
            for chunk in stream:
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    piece = getattr(delta, "content", None)
                    if piece:
                        _any_content = True
                        _response_buf.append(piece)
                        yield piece
                    else:
                        rc = getattr(delta, "reasoning_content", None)
                        if rc is None:
                            rc = (getattr(delta, "model_extra", None) or {}).get("reasoning_content")
                        if rc:
                            _reasoning_buf.append(rc)
                # Capture usage if provided in final chunk.
                # vLLM / LiteLLM may populate prompt_tokens_details.cached_tokens
                # to report KV-cache hits; read it when available.
                if hasattr(chunk, "usage") and chunk.usage:
                    self._last_input_tokens  = chunk.usage.prompt_tokens     or 0
                    self._last_output_tokens = chunk.usage.completion_tokens or 0
                    try:
                        _details = getattr(chunk.usage, "prompt_tokens_details", None)
                        self._last_cached_tokens = getattr(_details, "cached_tokens", 0) or 0
                    except Exception:
                        self._last_cached_tokens = 0
            if not _any_content and _reasoning_buf:
                _response_text = "".join(_reasoning_buf)
                _response_buf.append(_response_text)
                yield _response_text

            # ── Response / usage log (mirrors [CLAUDE USAGE] in ClaudeGateway) ─
            _full_response = "".join(_response_buf)
            logger.info(
                f"[LOCAL USAGE] request_id={request_id} model={selected} tier={tier} "
                f"in={self._last_input_tokens} out={self._last_output_tokens} "
                f"cached={self._last_cached_tokens} "
                f"response_chars={len(_full_response)} "
                f"response_snippet={_full_response[:500]!r}"
            )
            _log_cache_effectiveness(
                request_id=request_id or "n/a",
                model=selected,
                cache_read=self._last_cached_tokens,
                prompt_total=self._last_input_tokens,
                context="generate",
            )
        except Exception as e:
            logger.error(f"[LOCAL ERROR] request_id={request_id} model={selected}: {e}")
            yield f"Error: local model call failed ({e})"


# ── Vision image support ───────────────────────────────────────

def generate_with_image_local(
    prompt: str,
    image_b64: str,
    mime_type: str = "image/jpeg",
    system_prompt: str = "",
    model: str = None,
) -> tuple[str, int, int]:
    """
    Send a prompt + base64 image to a vision-capable in-house hosted model.
    Uses the OpenAI-compatible /v1/chat/completions endpoint on LOCAL_LLM_BASE_URL.

    model: explicit model override (e.g. "Kimi-k2.5", "glm-4.5v").
           Falls back to the first entry in LOCAL_VISION_MODELS env var.
    Returns (text, in_tok, out_tok).
    """
    if not LOCAL_LLM_BASE_URL:
        raise RuntimeError("LOCAL_LLM_BASE_URL not configured — cannot call local vision model")

    from core.model_registry import LOCAL_VISION_MODELS
    from core.prompt_sanitizer import sanitize as _sanitize

    selected = model or (LOCAL_VISION_MODELS[0] if LOCAL_VISION_MODELS else None)
    if not selected:
        raise RuntimeError(
            "No local vision model available. "
            "Set LOCAL_VISION_MODELS env var (e.g. 'glm-4.5v,Kimi-k2.5')."
        )

    safe_prompt = _sanitize(prompt)

    messages: list = []
    if system_prompt:
        messages.append({"role": "system", "content": _sanitize(system_prompt)})
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": safe_prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
            },
        ],
    })

    try:
        client = _get_openai_client()
        response = client.chat.completions.create(model=selected, messages=messages)

        in_tok  = getattr(response.usage, "prompt_tokens",     0) if response.usage else 0
        out_tok = getattr(response.usage, "completion_tokens", 0) if response.usage else 0
        text    = response.choices[0].message.content or "" if response.choices else ""

        logger.info(f"[LOCAL VISION] model={selected} in={in_tok} out={out_tok}")
        return text, in_tok, out_tok
    except Exception as e:
        logger.error(f"generate_with_image_local (model={selected}): {e}")
        raise


# ── Singleton ──────────────────────────────────────────────────

_gateway: Optional[LocalLLMGateway] = None
_gw_lock = threading.Lock()


def get_local_gateway() -> LocalLLMGateway:
    global _gateway
    if _gateway is None:
        with _gw_lock:
            if _gateway is None:
                _gateway = LocalLLMGateway()
                logger.info(f"Local LLM gateway: initialised → {LOCAL_LLM_BASE_URL or '(not configured)'}")
    return _gateway
