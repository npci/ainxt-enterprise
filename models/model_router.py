# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AiNxt MODEL ROUTER
# Signal-based routing — approved models only.
#
# Routing table:
#   simple   → Local LLM (in-house)        private, free, low-latency
#   medium   → GPT-5.4                    coding, reasoning, agents
#   complex  → Claude Sonnet 4.6          complex reasoning, SDLC
#   deep     → GPT-5-5                    latest OpenAI (explicit selection)
#   solution → Claude Opus 4.7            final synthesis (explicit selection)
#   opus-4-8 → Claude Opus 4.8            CLI/IDE opt-in
#   opus-5   → Claude Opus 5              CLI/IDE opt-in (ENABLE_CLI_OPUS_5)
#   vision   → Gemini 2.5 Flash           image / visual tasks (auto-detected)
#   gemini   → Gemini 2.5 Flash           explicit Gemini selection (text)
#
# BLOCKED: claude-opus-4-5 and older, GPT-5.2 Pro, GPT-5.2
#
# Fallback chains (primary unavailable):
#   simple   → Local → GPT-5 mini → Claude Sonnet → error
#   medium   → GPT-5.4 → Claude Sonnet → error
#   complex  → Claude Sonnet → GPT-5.4 → error
#   deep     → GPT-5-5 → Claude Sonnet → error
#   solution → Claude Opus 4.7 → Claude Sonnet → error
#   opus-4-8 → Claude Opus 4.8 → Claude Sonnet → error
#   opus-5   → Claude Opus 5   → Claude Sonnet → error
#   vision   → Gemini → Claude Sonnet → error
#
# Signals evaluated (in priority order):
#   1. caller model_hint
#   2. vision keyword detection
#   3. complexity classifier (Redis-cached)
#
# Public API:
#   model_router.generate(prompt, model_hint=None) -> str
#   model_router.stream(prompt, model_hint=None)   -> Generator[str, None, None]
#   model_router.route(prompt, model_hint=None)    -> RoutingDecision
# ============================================================

import json
import os
import re
import threading
from dataclasses import dataclass
from typing import List, Optional, Union

from core.logger import logger
from core.proxy_tool_use import llm_proxy_headers as _llm_proxy_headers
from core.model_registry import (
    OPENAI_SIMPLE_MODEL,
    OPENAI_CODING_MODEL,
    OPENAI_LATEST_MODEL,
    OPENAI_TERA_MODEL,
    OPENAI_LUNA_MODEL,
    OPENAI_OSS_MODEL,
    CHAT_FALLBACK_CHAIN,
    CLAUDE_PRIMARY_MODEL,
    CLAUDE_HAIKU,
    CLAUDE_OPUS_MODEL,
    CLAUDE_OPUS_46_MODEL,
    CLAUDE_OPUS_48_MODEL,
    CLAUDE_OPUS_5_MODEL,
    CLAUDE_SONNET_5_MODEL,
    ENABLE_OPUS,
    ENABLE_GPT56_TERA,
    ENABLE_GPT56_LUNA,
    SOLUTION_MODEL,
    GEMINI_VISION_MODEL,
    GEMINI_TEXT_MODEL,
    GEMINI_CODING_LITE_MODEL,
    GEMINI_IMAGE_MODEL,
    BLOCKED_MODELS,
    CLAUDE_PRIMARY_DISPLAY,
    CLAUDE_HAIKU_DISPLAY,
    CLAUDE_OPUS_DISPLAY,
    CLAUDE_OPUS_48_DISPLAY,
    CLAUDE_OPUS_5_DISPLAY,
    CLAUDE_SONNET_5_DISPLAY,
    OPENAI_CODING_DISPLAY,
    OPENAI_SIMPLE_DISPLAY,
    OPENAI_LATEST_DISPLAY,
    OPENAI_TERA_DISPLAY,
    OPENAI_LUNA_DISPLAY,
    OPENAI_OSS_DISPLAY,
    GEMINI_DISPLAY,
    GEMINI_TEXT_DISPLAY,
    GEMINI_CODING_LITE_DISPLAY,
    GEMINI_IMAGE_DISPLAY,
    LOCAL_LLM_DISPLAY,
)
from core.circuit_breaker import get_breaker

# ── LLM Proxy config ──────────────────────────────────────────
# When set, external model calls (OpenAI / Claude / Gemini) are forwarded
# to the LLM proxy service instead of calling APIs directly.
# In prod: LLM_PROXY_URL=http://your-llm-proxy:8003
# Leave empty to call APIs directly.
def _llm_proxy_url() -> str:
    return os.getenv("LLM_PROXY_URL", "").rstrip("/")

def _llm_timeout() -> float:
    """Total read timeout for LLM calls (seconds). Set LLM_TIMEOUT_SEC=0 to disable."""
    v = os.getenv("LLM_TIMEOUT_SEC", "300")
    f = float(v)
    return None if f <= 0 else f


# Persistent connection pool for all LLM proxy calls.
# A new httpx.Client per request forces a fresh TCP handshake every time
# and hard-caps throughput at ~40–50 concurrent (proxy thread ceiling).
# This singleton keeps connections alive across requests so the proxy
# can serve hundreds of concurrent calls with no per-request setup overhead.
import httpx as _httpx_mod
_PROXY_CLIENT_LOCK = threading.Lock()
_PROXY_CLIENT: "_httpx_mod.Client | None" = None


def _get_proxy_client() -> "_httpx_mod.Client":
    """Return the module-level persistent httpx.Client for LLM proxy calls."""
    global _PROXY_CLIENT
    if _PROXY_CLIENT is None:
        with _PROXY_CLIENT_LOCK:
            if _PROXY_CLIENT is None:
                _t = _llm_timeout()
                _PROXY_CLIENT = _httpx_mod.Client(
                    headers=_llm_proxy_headers(),
                    timeout=_httpx_mod.Timeout(_t, connect=10.0),
                    # trust_env=False: the gateway→LLM-proxy hop is INTERNAL.
                    # It must NOT be routed through the Squid HTTPS_PROXY (which is
                    # for outbound cloud APIs only) — Squid buffers the SSE/ndjson
                    # response, so cloud-model tokens arrive all-at-once instead of
                    # streaming. Bypassing env proxies restores per-token streaming.
                    trust_env=False,
                    limits=_httpx_mod.Limits(
                        max_connections=200,
                        max_keepalive_connections=100,
                        keepalive_expiry=30.0,
                    ),
                )
                logger.info(
                    "ModelRouter: proxy HTTP client initialised "
                    "(max_conn=200, keepalive=100, timeout=%s)", _t
                )
    return _PROXY_CLIENT


# ── Async HTTP client lifecycle ───────────────────────────────
# RQ workers are SYNC processes; each job may run asyncio.run() which creates
# a FRESH event loop.  A module-level AsyncClient is bound to the event loop
# that created it — reusing it in a new loop raises "Event loop is closed".
#
# Fix: by default (SDLC_PER_LOOP_HTTP_CLIENT != "0") we create a fresh
# AsyncClient per async_generate() call.  Per-call clients are cheap for the
# low-volume async IDE path and are loop-agnostic.  The old shared-singleton
# path (SDLC_PER_LOOP_HTTP_CLIENT=0) is kept for easy rollback if the IDE
# endpoint ever needs high-concurrency connection reuse.
import asyncio as _asyncio

def _use_per_call_async_client() -> bool:
    """Safe default: True (per-call). Set SDLC_PER_LOOP_HTTP_CLIENT=0 to use shared."""
    return os.getenv("SDLC_PER_LOOP_HTTP_CLIENT", "1") != "0"

# Shared singleton — only used when SDLC_PER_LOOP_HTTP_CLIENT=0.
_ASYNC_PROXY_CLIENT: "_httpx_mod.AsyncClient | None" = None
_ASYNC_PROXY_CLIENT_LOCK = threading.Lock()


def _get_async_proxy_client() -> "_httpx_mod.AsyncClient":
    """Return the shared httpx.AsyncClient (legacy mode, SDLC_PER_LOOP_HTTP_CLIENT=0).

    WARNING: The returned client is bound to the event loop that created it.
    Reusing it across different event loops (e.g. RQ worker asyncio.run() calls)
    raises 'Event loop is closed'.  Use _make_async_client() instead for the
    safe per-call path.
    """
    global _ASYNC_PROXY_CLIENT
    if _ASYNC_PROXY_CLIENT is None:
        with _ASYNC_PROXY_CLIENT_LOCK:
            if _ASYNC_PROXY_CLIENT is None:
                _t = _llm_timeout()
                _ASYNC_PROXY_CLIENT = _httpx_mod.AsyncClient(
                    headers=_llm_proxy_headers(),
                    timeout=_httpx_mod.Timeout(_t, connect=10.0),
                    limits=_httpx_mod.Limits(
                        max_connections=500,
                        max_keepalive_connections=200,
                        keepalive_expiry=30.0,
                    ),
                )
                logger.info(
                    "ModelRouter: async proxy HTTP client initialised "
                    "(max_conn=500, keepalive=200, timeout=%s)", _t
                )
    return _ASYNC_PROXY_CLIENT


def _make_async_client() -> "_httpx_mod.AsyncClient":
    """Create a fresh httpx.AsyncClient bound to the CURRENT event loop.

    Always safe for RQ workers (each asyncio.run() = new loop).
    The caller must use it as an async context manager or call aclose() after use.
    """
    _t = _llm_timeout()
    return _httpx_mod.AsyncClient(
        timeout=_httpx_mod.Timeout(_t, connect=10.0),
        limits=_httpx_mod.Limits(
            max_connections=100,
            max_keepalive_connections=50,
            keepalive_expiry=30.0,
        ),
    )


class _ProxyGateway:
    """
    Drop-in replacement for ClaudeGateway / OpenAIGateway / GeminiGateway.
    Forwards all LLM calls to the LLM proxy service over the internal network.
    """

    def __init__(self, provider: str):
        self.provider = provider
        self._last_input_tokens          = 0
        self._last_output_tokens         = 0
        self._last_cache_read_tokens     = 0
        self._last_cache_creation_tokens = 0
        logger.info(f"ModelRouter: {provider} → LLM proxy ({_llm_proxy_url()})")

    def generate(
            self,
            prompt=None,
            model: str = None,
            content_blocks: list = None,
            precleared: bool = False,
            precleared_findings: list = None,
    ):
        """precleared / precleared_findings:
            Compliance (detection + redaction) is performed HERE in the backend
            gateway layer (Tier 1). The LLM proxy performs no compliance and
            forwards text verbatim. `precleared=True` is forwarded as the
            `compliance_precleared` body field, which tells the proxy's
            /llm/generate endpoint to skip its minimal HardBlock safety net
            (that net exists only for un-precleared callers, e.g. the ABStudio
            sandbox tool). `precleared_findings` is accepted for backward compat
            but no longer forwarded — the text is already redacted upstream."""
        import httpx

        self._last_input_tokens          = 0
        self._last_output_tokens         = 0
        self._last_cache_read_tokens     = 0
        self._last_cache_creation_tokens = 0

        proxy_url = _llm_proxy_url()

        if content_blocks is not None:
            # Structured path: send provider + content_blocks (no prompt key)
            payload: dict = {"provider": self.provider, "content_blocks": content_blocks}
        elif isinstance(prompt, list):
            # Multi-turn path: send the structured messages list as-is so the
            # downstream gateway can compliance-check only the last user turn
            # (not the entire flattened conversation history). Flattening to a
            # single string caused gateway_*.py compliance to re-validate prior
            # turns and produce false-positive PCI blocks on benign new prompts.
            payload = {"provider": self.provider, "messages": prompt}
        else:
            payload = {"provider": self.provider, "prompt": prompt}
        if model:
            payload["model"] = model

        from core.logger import get_request_id as _get_req_id
        _rid = _get_req_id()
        if _rid:
            payload["request_id"] = _rid

        # Compliance preclear signal. Compliance (detection + redaction) is done
        # here in the backend gateway layer (Tier 1); the proxy performs NO
        # compliance and forwards verbatim. Setting this flag tells the proxy's
        # /llm/generate endpoint to skip its minimal HardBlock safety net (which
        # exists only for un-precleared callers such as the ABStudio sandbox
        # tool). Sent for ALL providers since Tier-1 already validated the prompt.
        if precleared:
            payload["compliance_precleared"] = True

        _mode = (
            "content_blocks" if content_blocks is not None
            else ("messages" if isinstance(prompt, list) else "prompt")
        )
        _payload_chars = (
            sum(len(b.get("text", "")) for b in content_blocks)
            if content_blocks is not None
            else (
                sum(len(str(m.get("content", ""))) for m in prompt)
                if isinstance(prompt, list)
                else len(str(prompt or ""))
            )
        )
        _n_cached = sum(1 for b in (content_blocks or []) if b.get("cache"))
        logger.info(
            f"[PROXY HOP-2] {self.provider} → {proxy_url}/llm/generate "
            f"model={model!r} mode={_mode} "
            + (f"blocks={len(content_blocks)} cached={_n_cached} chars={_payload_chars}"
               if content_blocks is not None else f"prompt_chars={_payload_chars}")
        )

        # Detailed outbound-payload diagnostics — shape, role breakdown, last-user preview,
        # and full content lengths so a compliance/PCI block can be triaged from logs alone
        # without needing to re-trace the call. Previews are truncated to keep log volume sane.
        try:
            if content_blocks is not None:
                _block_summary = ", ".join(
                    f"block{_i}={len(b.get('text', ''))}c/cache={'Y' if b.get('cache') else 'N'}"
                    for _i, b in enumerate(content_blocks)
                )
                logger.info(
                    f"[PROXY HOP-2 PAYLOAD] shape=content_blocks count={len(content_blocks)} "
                    f"total_chars={_payload_chars} | {_block_summary}"
                )
            elif isinstance(prompt, list):
                _role_counts: dict = {}
                for _m in prompt:
                    _role_counts[_m.get("role", "?")] = _role_counts.get(_m.get("role", "?"), 0) + 1
                _last_user = next(
                    (m.get("content", "") for m in reversed(prompt) if m.get("role") == "user"),
                    "",
                )
                _last_user_str = _last_user if isinstance(_last_user, str) else str(_last_user)
                _preview = _last_user_str[:200].replace("\n", " ")
                logger.info(
                    f"[PROXY HOP-2 PAYLOAD] shape=list turns={len(prompt)} roles={_role_counts} "
                    f"total_chars={_payload_chars} last_user_len={len(_last_user_str)} "
                    f"last_user_preview={_preview!r}"
                )
            else:
                _prompt_str = prompt if isinstance(prompt, str) else str(prompt or "")
                _preview = _prompt_str[:200].replace("\n", " ")
                logger.info(
                    f"[PROXY HOP-2 PAYLOAD] shape=str chars={len(_prompt_str)} preview={_preview!r}"
                )
        except Exception as _diag_err:
            logger.debug(f"[PROXY HOP-2 PAYLOAD] diagnostic log failed: {_diag_err}")

        _lines_received = 0

        try:
            client = _get_proxy_client()
            with client.stream(
                    "POST",
                    f"{proxy_url}/llm/generate",
                    json=payload,
            ) as resp:
                logger.info(
                    f"[PROXY HOP-2] {self.provider} stream opened status={resp.status_code}"
                )
                resp.raise_for_status()
                # Read RAW bytes and split on newlines ourselves so each ndjson
                # line is emitted the instant it arrives. httpx's iter_lines()
                # adds an internal buffer layer that can delay per-token flush on
                # the internal proxy hop, defeating streaming for cloud models.
                _buf = ""

                def _handle(_line: str):
                    nonlocal _lines_received
                    if not _line:
                        return None
                    _lines_received += 1
                    try:
                        obj = json.loads(_line)
                    except json.JSONDecodeError:
                        return ("raw", _line)
                    if "error" in obj:
                        logger.error(
                            f"[PROXY HOP-2] {self.provider} proxy returned error "
                            f"after {_lines_received} lines: {obj['error']}"
                        )
                        raise RuntimeError(f"LLM proxy error: {obj['error']}")
                    if "r" in obj:
                        # Reasoning delta from a reasoning model (e.g. gpt-5.4).
                        # Return a ReasoningMarker so the gateway emits a live
                        # {reasoning:{delta}} SSE frame instead of dropping it.
                        try:
                            from pipeline.stream_events import ReasoningMarker as _RM
                            return ("r", _RM(delta=obj["r"]))
                        except Exception:
                            return None
                    if "t" in obj:
                        return ("t", obj["t"])
                    if "m" in obj:
                        self._last_input_tokens          = obj["m"].get("in",           0)
                        self._last_output_tokens         = obj["m"].get("out",          0)
                        self._last_cache_read_tokens     = obj["m"].get("cache_read",   0)
                        self._last_cache_creation_tokens = obj["m"].get("cache_created", 0)
                        logger.info(
                            f"[PROXY HOP-2] {self.provider} metadata received "
                            f"in={self._last_input_tokens} out={self._last_output_tokens} "
                            f"cache_read={self._last_cache_read_tokens} "
                            f"cache_created={self._last_cache_creation_tokens}"
                        )
                    return None

                for chunk in resp.iter_raw():
                    if not chunk:
                        continue
                    _buf += chunk.decode("utf-8", "replace")
                    while "\n" in _buf:
                        _line, _buf = _buf.split("\n", 1)
                        _res = _handle(_line.strip())
                        if _res is not None:
                            yield _res[1]
                # Flush any trailing partial line (no terminating newline).
                if _buf.strip():
                    _res = _handle(_buf.strip())
                    if _res is not None:
                        yield _res[1]
        except Exception as e:
            logger.error(
                f"[PROXY HOP-2] {self.provider}: call failed after {_lines_received} lines "
                f"[{type(e).__name__}] → {e}"
            )
            raise RuntimeError(f"LLM proxy call failed: {e}") from e

    def generate_image(
            self,
            prompt: str,
            image_b64: str,
            mime_type: str = "image/jpeg",
            system_prompt: str = "",
            images_b64: "list[str] | None" = None,
            mime_types: "list[str] | None" = None,
    ) -> tuple[str, int, int, str]:
        """
        Forward an image+prompt to the LLM proxy's /llm/generate-image endpoint.
        The proxy handles primary (Gemini) + fallback (OpenAI) internally.
        Returns (text, in_tok, out_tok, actual_model) where actual_model reflects
        which provider actually ran (gemini or openai fallback).

        Multi-image (optional, backward-compatible): pass `images_b64` (list
        of base64 strings) + matching `mime_types` to analyse multiple images
        in one call. `image_b64`/`mime_type` are still sent as the legacy
        singular fields (first image) so an older, not-yet-upgraded proxy
        deployment ignores the extra fields and keeps working exactly as
        before (analyses the first image only).
        """
        import httpx

        proxy_url = _llm_proxy_url()
        if not proxy_url:
            raise RuntimeError("LLM_PROXY_URL not set — cannot route image call through proxy")

        payload = {
            "provider":      self.provider,
            "prompt":        prompt,
            "image_b64":     image_b64,
            "mime_type":     mime_type,
            "system_prompt": system_prompt,
        }
        if images_b64:
            payload["images_b64"] = images_b64
            payload["mime_types"] = mime_types or [mime_type] * len(images_b64)
        try:
            client = _get_proxy_client()
            resp = client.post(f"{proxy_url}/llm/generate-image", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return (
                data.get("text", ""),
                data.get("in_tok", 0),
                data.get("out_tok", 0),
                data.get("actual_model", self.provider),  # proxy tells us who actually ran
            )
        except Exception as e:
            logger.error(f"_ProxyGateway.generate_image({self.provider}): failed → {e}")
            raise RuntimeError(f"LLM proxy image call failed: {e}") from e

    async def async_generate(self, prompt, model: str = None,
                              _override_client=None) -> str:
        """Async version of generate() — caller supplies an AsyncClient.

        Parameters
        ----------
        _override_client : httpx.AsyncClient | None
            When provided, this client is used for the request instead of the
            shared singleton.  ModelRouter.async_generate() passes a fresh
            per-call client here (SDLC_PER_LOOP_HTTP_CLIENT != "0", default)
            so this method is safe to call from any event loop, including RQ
            worker asyncio.run() contexts.

            When None, falls back to the shared singleton from
            _get_async_proxy_client() — legacy behaviour, only safe under a
            single long-running event loop (e.g. uvicorn).
        """
        proxy_url = _llm_proxy_url()
        if isinstance(prompt, list):
            prompt = "\n".join(
                f"{m['role'].title()}: {m.get('content', '')}" for m in prompt
            )
        payload: dict = {"provider": self.provider, "prompt": prompt}
        if model:
            payload["model"] = model

        from core.logger import get_request_id as _get_req_id
        _rid = _get_req_id()
        if _rid:
            payload["request_id"] = _rid

        _client = _override_client if _override_client is not None else _get_async_proxy_client()
        try:
            chunks = []
            async with _client.stream(
                    "POST",
                    f"{proxy_url}/llm/generate",
                    json=payload,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        chunks.append(line)
                        continue
                    if "error" in obj:
                        raise RuntimeError(f"LLM proxy error: {obj['error']}")
                    elif "t" in obj:
                        chunks.append(obj["t"])
                    elif "m" in obj:
                        self._last_input_tokens  = obj["m"].get("in", 0)
                        self._last_output_tokens = obj["m"].get("out", 0)
            return "".join(chunks)
        except Exception as e:
            logger.error(f"_ProxyGateway({self.provider}).async_generate: failed → {e}")
            raise RuntimeError(f"LLM proxy async call failed: {e}") from e

    async def async_stream(
            self,
            prompt,
            model: str = None,
            precleared: bool = False,
            precleared_findings: list = None,
            _override_client=None,
    ):
        """Async streaming generator — yields str tokens as they arrive from the proxy.

        Mirrors _call_proxy_stream() but runs entirely on the event loop so
        FastAPI's async StreamingResponse can flush each token to the client
        the instant it arrives, without blocking a thread-pool worker.

        This is the async counterpart of generate() (which is sync + blocking).
        Use this from async generators (e.g. _general_stream_async in gateway.py)
        so the /ask SSE path matches the per-token delivery of the CLI path.
        """
        proxy_url = _llm_proxy_url()
        if not proxy_url:
            # No proxy configured — fall back to sync generate() in a thread.
            import asyncio as _asyncio
            loop = _asyncio.get_event_loop()
            for tok in self.generate(prompt, model=model, precleared=precleared,
                                     precleared_findings=precleared_findings):
                yield tok
            return

        _mode = "messages" if isinstance(prompt, list) else "prompt"
        _payload_chars = (
            sum(len(str(m.get("content", ""))) for m in prompt)
            if isinstance(prompt, list)
            else len(str(prompt or ""))
        )
        logger.info(
            f"[PROXY HOP-2] {self.provider} → {proxy_url}/llm/generate "
            f"model={model!r} mode={_mode} prompt_chars={_payload_chars} [async]"
        )

        if isinstance(prompt, list):
            payload: dict = {"provider": self.provider, "messages": prompt}
        else:
            payload = {"provider": self.provider, "prompt": prompt}
        if model:
            payload["model"] = model

        from core.logger import get_request_id as _get_req_id
        _rid = _get_req_id()
        if _rid:
            payload["request_id"] = _rid
        if precleared:
            payload["compliance_precleared"] = True

        _per_call = _use_per_call_async_client()
        _owned = _make_async_client() if (_per_call and _override_client is None) else None
        _client = _override_client or _owned or _get_async_proxy_client()
        _buf = ""
        _lines_received = 0
        try:
            async with _client.stream(
                    "POST",
                    f"{proxy_url}/llm/generate",
                    json=payload,
            ) as resp:
                logger.info(
                    f"[PROXY HOP-2] {self.provider} stream opened status={resp.status_code} [async]"
                )
                resp.raise_for_status()
                # Read raw bytes and split on newlines — same strategy as the
                # sync generate() so each ndjson line is processed the instant
                # it arrives without httpx's internal line buffer delay.
                async for chunk in resp.aiter_raw():
                    if not chunk:
                        continue
                    _buf += chunk.decode("utf-8", "replace")
                    while "\n" in _buf:
                        _line, _buf = _buf.split("\n", 1)
                        _line = _line.strip()
                        if not _line:
                            continue
                        _lines_received += 1
                        try:
                            obj = json.loads(_line)
                        except json.JSONDecodeError:
                            yield _line
                            continue
                        if "error" in obj:
                            logger.error(
                                f"[PROXY HOP-2] {self.provider} proxy returned error "
                                f"after {_lines_received} lines: {obj['error']} [async]"
                            )
                            raise RuntimeError(f"LLM proxy error: {obj['error']}")
                        if "r" in obj:
                            # Reasoning delta from a reasoning model (e.g. gpt-5.4).
                            # Yield a ReasoningMarker so the gateway's _general_stream
                            # emits a live {reasoning:{delta}} SSE frame instead of
                            # dropping the chunk (the cause of the 25 s hang).
                            try:
                                from pipeline.stream_events import ReasoningMarker as _RM
                                yield _RM(delta=obj["r"])
                            except Exception:
                                pass
                        elif "t" in obj:
                            yield obj["t"]
                        elif "m" in obj:
                            self._last_input_tokens          = obj["m"].get("in",           0)
                            self._last_output_tokens         = obj["m"].get("out",          0)
                            self._last_cache_read_tokens     = obj["m"].get("cache_read",   0)
                            self._last_cache_creation_tokens = obj["m"].get("cache_created", 0)
                            logger.info(
                                f"[PROXY HOP-2] {self.provider} metadata received "
                                f"in={self._last_input_tokens} out={self._last_output_tokens} "
                                f"cache_read={self._last_cache_read_tokens} "
                                f"cache_created={self._last_cache_creation_tokens} [async]"
                            )
                # Flush any trailing partial line.
                if _buf.strip():
                    try:
                        obj = json.loads(_buf.strip())
                        if "r" in obj:
                            try:
                                from pipeline.stream_events import ReasoningMarker as _RM
                                yield _RM(delta=obj["r"])
                            except Exception:
                                pass
                        elif "t" in obj:
                            yield obj["t"]
                        elif "m" in obj:
                            self._last_input_tokens  = obj["m"].get("in",  0)
                            self._last_output_tokens = obj["m"].get("out", 0)
                    except json.JSONDecodeError:
                        yield _buf.strip()
        except Exception as e:
            logger.error(
                f"[PROXY HOP-2] {self.provider}: async_stream failed after "
                f"{_lines_received} lines [{type(e).__name__}] → {e}"
            )
            raise RuntimeError(f"LLM proxy async stream failed: {e}") from e
        finally:
            if _owned is not None:
                await _owned.aclose()


# ── Circuit breakers — one per provider ───────────────────────
_CB_LOCAL  = get_breaker("local",  failure_threshold=3, recovery_timeout=30)
_CB_OPENAI = get_breaker("openai", failure_threshold=5, recovery_timeout=60)
_CB_CLAUDE = get_breaker("claude", failure_threshold=5, recovery_timeout=60)
_CB_GEMINI = get_breaker("gemini", failure_threshold=5, recovery_timeout=60)

# Minimum confidence to trust the regex classifier.
# Below this, classify_with_confidence_llm() escalates to Claude Haiku.
# This constant is kept for logging context only — routing logic uses LLM directly.
_CONFIDENCE_LOG_THRESHOLD = 0.7


# ============================================================
# TIER CONSTANTS
# ============================================================

TIER_SIMPLE    = "simple"
TIER_MINI      = "mini"       # direct GPT-5-mini (no local LLM hop)
TIER_LOCAL_MINI = "local_mini"  # in-house hosted GPT-OSS-120B (OpenAI-compat, no cloud egress); lightweight fast tier
TIER_MEDIUM    = "medium"
TIER_COMPLEX   = "complex"
TIER_HAIKU     = "haiku"      # explicit Haiku selection → Claude Haiku (lightweight, fast)
TIER_VISION    = "vision"     # auto-detected image/visual queries → Gemini
TIER_GEMINI    = "gemini"     # explicit Gemini selection → Gemini (text, no Vision label)
TIER_SOLUTION  = "solution"   # final synthesis — Opus 4.7 if ENABLE_OPUS=true, else Sonnet
TIER_OPUS_48   = "opus-4-8"   # CLI/IDE Claude Opus 4.8 selection (not shown in chat picker, not used by SDLC)
TIER_OPUS_5    = "opus-5"     # CLI/IDE Claude Opus 5 selection (opt-in, ENABLE_CLI_OPUS_5)
TIER_SONNET_5  = "sonnet-5"   # explicit Claude Sonnet 5 selection (available on ALL channels)
TIER_DEEP      = "deep"       # explicit GPT-5-5 latest tier
TIER_TERA      = "tera"       # GPT-5.6 Terra — high-capacity variant (Chat + CLI, ENABLE_GPT56_TERA)
TIER_LUNA      = "luna"       # GPT-5.6 Luna — efficient variant (Chat + CLI, ENABLE_GPT56_LUNA)

# Caller hint → tier mapping.
# Static entries cover shorthand hints; dynamic entries cover full model IDs
# so env-var-configured model names are automatically routed to the right tier.
_HINT_MAP = {
    "simple":        TIER_SIMPLE,    # auto-routing: local LLM first, gpt-5-mini fallback
    "local":         TIER_SIMPLE,
    "mini":          TIER_MINI,      # direct GPT-5-mini, no local LLM hop
    "gpt-mini":      TIER_MINI,
    "gpt-5-mini":    TIER_MINI,
    "local_mini":    TIER_LOCAL_MINI,  # in-house GPT-OSS-120B (used by CIL intent classifier)
    "gpt-oss":       TIER_LOCAL_MINI,  # legacy alias
    "gpt-oss-120b":  TIER_LOCAL_MINI,
    OPENAI_OSS_MODEL: TIER_LOCAL_MINI,
    "medium":        TIER_MEDIUM,
    "coding":        TIER_MEDIUM,
    "agents":        TIER_MEDIUM,
    "gpt":           TIER_MEDIUM,
    "gpt-5.4":       TIER_MEDIUM,    # explicit gpt-5.4 coding hint
    "complex":       TIER_COMPLEX,
    "sonnet":        TIER_COMPLEX,
    "claude":        TIER_COMPLEX,
    "haiku":         TIER_HAIKU,
    "vision":        TIER_VISION,
    # Explicit Gemini selection — does NOT show "Vision" label
    "gemini":        TIER_GEMINI,
    # Legacy aliases — route to current Gemini default via _GEMINI_SPECIFIC_HINTS
    "gemini-2.5-flash":       TIER_GEMINI,
    "gemini-2.0-flash":       TIER_GEMINI,
    "gemini-3.5-flash":       TIER_GEMINI,
    "gemini-3.1-flash-lite":  TIER_GEMINI,
    "gemini-3.1-flash-image": TIER_VISION,
    # Solution tier — Opus 4.7 if ENABLE_OPUS, else Sonnet
    "solution":      TIER_SOLUTION,
    "opus":          TIER_SOLUTION,
    # Explicit Opus 4.8 selection (CLI/IDE only)
    "opus-4-8":      TIER_OPUS_48,
    "claude-opus-4-8": TIER_OPUS_48,
    # Explicit Opus 5 selection (CLI/IDE opt-in)
    "opus-5":        TIER_OPUS_5,
    "claude-opus-5": TIER_OPUS_5,
    # Explicit Sonnet 5 selection (all channels)
    "sonnet-5":       TIER_SONNET_5,
    "claude-sonnet-5": TIER_SONNET_5,
    # Dynamic — ensures env-var model ID overrides are also mapped
    OPENAI_SIMPLE_MODEL:  TIER_MINI,      # explicit model ID → direct access
    OPENAI_CODING_MODEL:  TIER_MEDIUM,
    OPENAI_LATEST_MODEL:  TIER_DEEP,
    CLAUDE_PRIMARY_MODEL: TIER_COMPLEX,
    CLAUDE_HAIKU:         TIER_HAIKU,
    GEMINI_VISION_MODEL:       TIER_VISION,   # vision analysis model (gemini-3.5-flash by default)
    GEMINI_TEXT_MODEL:         TIER_GEMINI,
    GEMINI_CODING_LITE_MODEL:  TIER_GEMINI,
    GEMINI_IMAGE_MODEL:        TIER_VISION,
    CLAUDE_OPUS_MODEL:    TIER_SOLUTION,
    CLAUDE_OPUS_48_MODEL: TIER_OPUS_48,
    CLAUDE_OPUS_5_MODEL:  TIER_OPUS_5,
    CLAUDE_SONNET_5_MODEL: TIER_SONNET_5,
    # Deep tier explicit hints
    "deep":               TIER_DEEP,
    "gpt-5-5":            TIER_DEEP,
    # GPT-5.6 Tera — high-capacity variant (Chat + CLI)
    "tera":               TIER_TERA,
    "gpt-5.6-terra":      TIER_TERA,
    OPENAI_TERA_MODEL:    TIER_TERA,
    # GPT-5.6 Luna — efficient variant (Chat + CLI)
    "luna":               TIER_LUNA,
    "gpt-5.6-luna":       TIER_LUNA,
    OPENAI_LUNA_MODEL:    TIER_LUNA,
}

# ============================================================
# PRIVACY FLOOR (hard enterprise safety invariant)
# ============================================================
# docs/architecture/10-model-router.md §10.2 + core/rag_acl.py classification
# ladder. When a request carries data at/above CONFIDENTIAL sensitivity, it must
# NEVER egress to a cloud provider (OpenAI/Claude/Gemini) — it is pinned to the
# in-house Local model (TIER_SIMPLE). profiles/routing.py implements the pure
# decision logic; this is the LIVE enforcement point in the router itself.
#
# This is a HARD invariant, not best-effort: the override runs BEFORE hint /
# vision / complexity routing so nothing downstream can re-route restricted data
# to the cloud. Enforcement can be disabled only via an explicit env opt-out
# (default ON) and every enforcement is logged for audit/alerting.
_PRIVACY_FLOOR_ENFORCE = os.getenv("PRIVACY_FLOOR_ENFORCE", "true").lower() == "true"

# Classifications that must stay on-prem (local-only). Ascending ladder from
# core/rag_acl.py: PUBLIC < INTERNAL < CONFIDENTIAL < RESTRICTED < PCI_SENSITIVE.
# INTERNAL and PUBLIC may use cloud models; everything above stays local.
_LOCAL_ONLY_CLASSIFICATIONS = frozenset({"CONFIDENTIAL", "RESTRICTED", "PCI_SENSITIVE"})

# ============================================================
# CONTEXT-SIZE ROUTING (frontier pattern #5 — "context size = routing")
# ============================================================
# docs/architecture/02 §2.5/§2.8. Context size is a first-class routing
# dimension: when a turn's estimated token footprint would not fit (or barely
# fits) a tier's context window, the router promotes to a larger-window model
# rather than risking truncation/compaction. Fail-safe: on any error or when no
# larger tier is warranted, the complexity-derived tier is unchanged.
_CONTEXT_SIZE_ROUTING = os.getenv("CONTEXT_SIZE_ROUTING", "true").lower() == "true"

# Approx working context window per tier (tokens). Mirrors gateway._MODEL_
# CONTEXT_WINDOW but keyed by TIER so route() can reason about fit without a
# gateway import (keeps the router importable in isolation).
_TIER_CONTEXT_WINDOW = {
    # local tier: use the largest window in the in-house fleet (kimi-k2.7-code
    # at 256 K). The pre-flight guard in messages_compat_router uses the
    # per-model _MODEL_CONTEXT_WINDOW table for precise per-model limits;
    # this value is used only for context-promotion decisions in route().
    "simple":   262_144,   # local fleet ceiling (kimi-k2.7-code 256 K)
    "mini":     128_000,
    "local_mini": 131_072,  # in-house gpt-oss-120b / GLM-5.2 (128 K)
    "medium":   128_000,   # gpt coding
    "deep":     256_000,   # gpt-5.x
    "complex":  200_000,   # claude sonnet
    "haiku":    200_000,
    "solution": 200_000,   # claude opus/sonnet
    "opus-4-8": 200_000,
    "opus-5":   200_000,
    "sonnet-5": 200_000,
    "vision":   1_000_000, # gemini
    "gemini":   1_000_000,
    "tera":     256_000,   # gpt-5.6-terra
    "luna":     256_000,   # gpt-5.6-luna
}
# Fraction of a window a turn may occupy before we promote (headroom for the
# answer + safety). 0.8 => promote once the input alone would exceed 80% window.
_CONTEXT_FIT_FRACTION = float(os.getenv("CONTEXT_FIT_FRACTION", "0.8"))
# Promotion ladder by ascending window: try these tiers (that we can reach on
# the cloud path) when the current tier can't fit the context.
_CONTEXT_PROMOTION_LADDER = ("deep", "gemini")  # 256K then 1M


def _tier_window(tier: str) -> int:
    return _TIER_CONTEXT_WINDOW.get(tier, 128_000)


def _promote_for_context(tier: str, context_tokens: int) -> str:
    """Return a tier whose window fits `context_tokens` (with headroom), or the
    original tier when it already fits / nothing larger helps. Never raises."""
    try:
        if not _CONTEXT_SIZE_ROUTING or not context_tokens or context_tokens <= 0:
            return tier
        needed = context_tokens / max(0.1, _CONTEXT_FIT_FRACTION)
        if _tier_window(tier) >= needed:
            return tier  # already fits with headroom
        for _cand in _CONTEXT_PROMOTION_LADDER:
            if _tier_window(_cand) >= needed and _tier_window(_cand) > _tier_window(tier):
                return _cand
        # nothing fully fits — pick the largest-window candidate available
        _largest = max(_CONTEXT_PROMOTION_LADDER, key=_tier_window)
        if _tier_window(_largest) > _tier_window(tier):
            return _largest
        return tier
    except Exception:  # noqa: BLE001 — routing must never break
        return tier


def classification_from_policy(policy) -> Optional[str]:
    """Derive a request data_classification from a resolved policy/profile.

    Maps the profile's RoutingPolicy.privacy_floor (public|internal|confidential|
    restricted, per profiles/schema.py) into the router's classification
    vocabulary so callers have ONE correct way to feed the privacy floor into
    route()/generate(). Returns None when no floor is set (→ no override). Never
    raises. The floor is a *minimum* handling tier: a 'confidential' floor means
    even otherwise-unclassified traffic on that profile stays local.
    """
    try:
        if policy is None:
            return None
        routing = getattr(policy, "routing", None)
        floor = getattr(routing, "privacy_floor", None) if routing is not None else None
        if not floor:
            return None
        f = str(floor).strip().lower()
        if f in ("confidential", "restricted"):
            return f.upper()
        return None  # public/internal floors do not force local
    except Exception:  # noqa: BLE001
        return None


def _privacy_requires_local(data_classification: Optional[str]) -> bool:
    """True when the given data classification must be handled by a local model
    only (never egress to a cloud provider). Unknown/None → False (no override),
    matching today's behavior for unclassified traffic. Never raises."""
    try:
        if not _PRIVACY_FLOOR_ENFORCE or not data_classification:
            return False
        return str(data_classification).strip().upper() in _LOCAL_ONLY_CLASSIFICATIONS
    except Exception:  # noqa: BLE001 — safety check must never break routing
        return False

# Hints that resolve to a specific Gemini model ID. Covers both the well-known
# literal (used by CLI / IDE clients) and the registry constant (used when an
# env override changes the resolved ID) — both must reach the same target.
_GEMINI_SPECIFIC_HINTS: dict = {m: m for m in (GEMINI_TEXT_MODEL, GEMINI_CODING_LITE_MODEL, GEMINI_IMAGE_MODEL)}
_GEMINI_SPECIFIC_HINTS.update({
    "gemini-3.5-flash":       GEMINI_TEXT_MODEL,
    "gemini-3.1-flash-lite":  GEMINI_CODING_LITE_MODEL,
    "gemini-3.1-flash-image": GEMINI_IMAGE_MODEL,
})

def _as_str(prompt) -> str:
    """Return the text content of prompt regardless of whether it is a str or messages list.
    Used internally for routing decisions (vision detection, complexity classification).
    """
    if isinstance(prompt, list):
        # Use the last user message content for routing signals
        for m in reversed(prompt):
            if m.get("role") == "user":
                return m.get("content") or ""
        return ""
    return prompt or ""


# Vision keyword detector
_VISION_RE = re.compile(
    r"\b(image|picture|photo|screenshot|diagram|chart|graph|"
    r"visual|figure|pixel|ocr|thumbnail|render|canvas|drawing)\b",
    re.IGNORECASE,
)

# Model-hint prefixes that indicate a non-vision local/code model. When the
# caller has already pinned one of these, vision auto-detection is skipped so
# that code-heavy conversations containing words like "render" or "canvas" are
# not silently rerouted to the Gemini image model.
_NON_VISION_HINT_PREFIXES = (
    "local", "kimi", "glm", "qwen", "deepseek", "llama", "gemma", "mistral",
    "mini", "medium", "complex", "deep", "solution", "haiku", "oss",
)


def _prompt_has_image(prompt: object) -> bool:
    """Return True only when the prompt contains a real image content block.

    Checks for Anthropic-style ``{"type": "image", "source": {...}}`` blocks
    and OpenAI-style ``{"type": "image_url", ...}`` blocks inside any message's
    content list. A plain string prompt never contains an image.
    """
    if not isinstance(prompt, list):
        return False
    for m in prompt:
        if not isinstance(m, dict):
            continue
        content = m.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type in ("image_url", "image"):
                return True
            # Anthropic source block: {"type": "image", "source": {"type": "base64"|"url", ...}}
            src = block.get("source", {})
            if isinstance(src, dict) and src.get("type") in ("base64", "url"):
                return True
    return False


# ============================================================
# ROUTING DECISION
# ============================================================

@dataclass
class RoutingDecision:
    tier:       str           # simple | medium | complex | vision
    model:      str           # display label
    complexity: str           # raw complexity from classifier
    is_vision:  bool
    hint:       Optional[str]
    fallback:   bool          # True if primary was unavailable
    # Optional provider-specific model ID override. When set, the dispatcher
    # forwards this to the chosen gateway instead of letting the gateway use
    # its module-level default. Provider-agnostic so future OpenAI/Claude
    # multi-model splits can reuse the same channel.
    provider_model_override: Optional[str] = None


@dataclass
class FallbackInfo:
    """Describes the routing decision made by the last generate() call.

    Exported so callers can do:
        from models.model_router import FallbackInfo
        fi: FallbackInfo = model_router.last_decision

    Fields
    ------
    fallback_occurred : bool
        True when the primary gateway was unavailable and a different one was
        selected.  False when the primary was used successfully.
    from_tier : str
        The tier that was originally requested (matches RoutingDecision.tier).
    from_label : str
        Human-readable display label for the original tier (e.g.
        "Claude Sonnet 4.6 (claude-sonnet-4-6)").
    to_tier : str
        The tier that was actually used.  Equals from_tier when no fallback.
    to_label : str
        Human-readable display label for the tier that was actually used.
        When no fallback this is the same as from_label.
    reason : str
        Short machine-readable reason tag.
        "primary"       — normal path, no fallback.
        "unavailable"   — primary gateway was None / circuit breaker open.
        "error"         — primary call raised an exception.
        "empty"         — primary returned an empty or Error: response.
        "not_set"       — sentinel: generate() was never called on this thread.

    Notes
    -----
    * Set by generate() after every blocking call.
    * NOT set by stream() — stream() does not collect a complete response, so
      we cannot reliably detect the fallback point mid-stream.  last_decision
      is left as the previous call's value (or the thread-local default) during
      streaming.  If you need fallback tracking for streaming, check
      last_model_label for the "[fallback]" suffix.
    * Thread-safe: backed by threading.local() on ModelRouter._tl.
    """
    fallback_occurred: bool
    from_tier:         str
    from_label:        str
    to_tier:           str
    to_label:          str
    reason:            str


# Sentinel used when generate() has not yet been called on a thread.
_FALLBACK_INFO_NOT_SET = FallbackInfo(
    fallback_occurred=False,
    from_tier="",
    from_label="",
    to_tier="",
    to_label="",
    reason="not_set",
)


def _local_display_label(tier: str = "simple") -> str:
    """Human-readable label for the local model currently selected for a tier."""
    try:
        from gateway_local_llm import get_local_gateway
        gw = get_local_gateway()
        mid = gw._catalog_pick(tier) if hasattr(gw, "_catalog_pick") else None
        if mid:
            return f"Local ({mid})"
    except Exception:
        pass
    return LOCAL_LLM_DISPLAY


# Display label for a specific Gemini model ID. Used so that the streaming
# meta and chat "model name" footer reflect the EXACT model the user picked
# (e.g. gemini-3.1-flash-lite) instead of collapsing to the generic
# TIER_GEMINI label (which would always show gemini-3.5-flash).
def _gemini_model_label(model_id: str) -> str:
    if model_id == GEMINI_TEXT_MODEL:
        return f"{GEMINI_TEXT_DISPLAY} ({GEMINI_TEXT_MODEL})"
    if model_id == GEMINI_CODING_LITE_MODEL:
        return f"{GEMINI_CODING_LITE_DISPLAY} ({GEMINI_CODING_LITE_MODEL})"
    if model_id == GEMINI_IMAGE_MODEL:
        return f"{GEMINI_IMAGE_DISPLAY} ({GEMINI_IMAGE_MODEL})"
    return f"{GEMINI_DISPLAY} ({model_id})"


# Model display labels per tier — evaluated at routing time so the label
# reflects the live model list rather than a boot-time snapshot.
def _tier_label(tier: str) -> str:
    if tier == TIER_MINI:
        return f"{OPENAI_SIMPLE_DISPLAY} ({OPENAI_SIMPLE_MODEL})"
    if tier == TIER_LOCAL_MINI:
        return f"{OPENAI_OSS_DISPLAY} ({OPENAI_OSS_MODEL})"
    if tier == TIER_SIMPLE:
        try:
            from gateway_local_llm import _catalog
            mid = _catalog.pick("simple")
            return f"{LOCAL_LLM_DISPLAY} ({mid})" if mid else LOCAL_LLM_DISPLAY
        except Exception:
            return LOCAL_LLM_DISPLAY
    if tier == TIER_MEDIUM:
        return f"{OPENAI_CODING_DISPLAY} ({OPENAI_CODING_MODEL})"
    if tier == TIER_COMPLEX:
        return f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL})"
    if tier == TIER_HAIKU:
        return f"{CLAUDE_HAIKU_DISPLAY} ({CLAUDE_HAIKU})"
    if tier == TIER_VISION:
        return f"{GEMINI_IMAGE_DISPLAY} ({GEMINI_IMAGE_MODEL})"
    if tier == TIER_GEMINI:
        return f"{GEMINI_TEXT_DISPLAY} ({GEMINI_TEXT_MODEL})"
    if tier == TIER_SOLUTION:
        if ENABLE_OPUS:
            return f"{CLAUDE_OPUS_DISPLAY} ({CLAUDE_OPUS_MODEL})"
        return f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL}) [solution]"
    if tier == TIER_OPUS_48:
        return f"{CLAUDE_OPUS_48_DISPLAY} ({CLAUDE_OPUS_48_MODEL})"
    if tier == TIER_OPUS_5:
        return f"{CLAUDE_OPUS_5_DISPLAY} ({CLAUDE_OPUS_5_MODEL})"
    if tier == TIER_SONNET_5:
        return f"{CLAUDE_SONNET_5_DISPLAY} ({CLAUDE_SONNET_5_MODEL})"
    if tier == TIER_DEEP:
        return f"{OPENAI_LATEST_DISPLAY} ({OPENAI_LATEST_MODEL})"
    if tier == TIER_TERA:
        return f"{OPENAI_TERA_DISPLAY} ({OPENAI_TERA_MODEL})"
    if tier == TIER_LUNA:
        return f"{OPENAI_LUNA_DISPLAY} ({OPENAI_LUNA_MODEL})"
    return "Unknown"


def hint_to_model_id(hint: str) -> Optional[str]:
    """Resolve a model hint string to a concrete model ID, using the same
    _HINT_MAP + model_registry constants that the router uses at runtime.

    All model IDs are read from model_registry (which reads from .env), so
    changing CLAUDE_PRIMARY_MODEL, OPENAI_CODING_MODEL, etc. in .env is
    automatically reflected here — no code change needed.

    Returns None for "simple" / "local" hints (local LLM — caller must
    resolve via q.local_model).  Returns the hint as-is for "local:<id>"
    prefixed hints so the caller can pass it straight to filter_allowed_models.
    """
    if not hint:
        return None

    key = hint.lower().strip()

    # "local:<model-id>" — pass through as-is (e.g. "local:Kimi-k2.5")
    if key.startswith("local:"):
        return key

    # "simple" / "local" — local LLM, no concrete cloud model ID
    if key in ("simple", "local"):
        return None

    # Resolve via _HINT_MAP → tier → concrete model ID
    tier = _HINT_MAP.get(key)
    if tier is None:
        return None

    # tier → concrete model ID (mirrors _tier_label but returns just the ID)
    _tier_map = {
        TIER_MINI:      OPENAI_SIMPLE_MODEL,
        TIER_MEDIUM:    OPENAI_CODING_MODEL,
        TIER_DEEP:      OPENAI_LATEST_MODEL,
        TIER_COMPLEX:   CLAUDE_PRIMARY_MODEL,
        TIER_HAIKU:     CLAUDE_HAIKU,
        TIER_SOLUTION:  CLAUDE_OPUS_MODEL if ENABLE_OPUS else CLAUDE_PRIMARY_MODEL,
        # TIER_OPUS_46 removed — claude-opus-4-6 is retired and always in BLOCKED_MODELS
        TIER_OPUS_48:   CLAUDE_OPUS_48_MODEL,
        TIER_OPUS_5:    CLAUDE_OPUS_5_MODEL,
        TIER_SONNET_5:  CLAUDE_SONNET_5_MODEL,
        TIER_VISION:    GEMINI_IMAGE_MODEL,
        TIER_GEMINI:    GEMINI_TEXT_MODEL,
        TIER_TERA:      OPENAI_TERA_MODEL,
        TIER_LUNA:      OPENAI_LUNA_MODEL,
    }
    return _tier_map.get(tier)


# ============================================================
# MODEL ROUTER
# ============================================================

class ModelRouter:
    """
    Routes every prompt to the approved LLM gateway.
    Gateways are lazily initialised — never crashes on import.
    """

    _tl = threading.local()   # thread-local storage — prevents cross-request label bleed

    def __init__(self):
        self._local   = None
        self._openai  = None
        self._claude  = None
        self._gemini  = None
        logger.info("ModelRouter initialised")

    # ── Thread-local per-request state ────────────────────────
    # These are properties backed by threading.local() so concurrent
    # requests can't overwrite each other's in-flight model label.
    @property
    def last_model_label(self) -> str:
        return getattr(self._tl, "last_model_label", "auto")
    @last_model_label.setter
    def last_model_label(self, v: str):
        self._tl.last_model_label = v

    @property
    def last_model_id(self) -> str:
        """Return the bare model ID from last_model_label.

        last_model_label is a display string like:
          "GPT-5.4 (gpt-5.4)"
          "Claude Sonnet 4.6 (claude-sonnet-4-6) [fallback]"
          "Local (local:Kimi-k2.5)"
          "auto"  ← default when no call has been made yet

        This property extracts the content of the last parenthesised group
        (before any trailing [fallback] / [solution] suffix) so callers that
        write to model_usages always store a clean, queryable model ID rather
        than a human-readable display string.

        Falls back to last_model_label as-is when no parenthesised group is
        found (e.g. "auto", "Unknown", plain IDs that were set directly).
        """
        import re as _re
        label = self.last_model_label
        # Find the last (...) group — strip trailing whitespace and [tag] suffixes
        m = _re.search(r'\(([^)]+)\)\s*(?:\[[^\]]*\])?\s*$', label)
        if m:
            return m.group(1).strip()
        return label

    @property
    def last_tier(self) -> str:
        return getattr(self._tl, "last_tier", "auto")
    @last_tier.setter
    def last_tier(self, v: str):
        self._tl.last_tier = v

    @property
    def last_input_tokens(self) -> int:
        return getattr(self._tl, "last_input_tokens", 0)
    @last_input_tokens.setter
    def last_input_tokens(self, v: int):
        self._tl.last_input_tokens = v

    @property
    def last_output_tokens(self) -> int:
        return getattr(self._tl, "last_output_tokens", 0)
    @last_output_tokens.setter
    def last_output_tokens(self, v: int):
        self._tl.last_output_tokens = v

    @property
    def last_thinking_text(self) -> str:
        """Extended thinking content from the last Claude call, if any.
        Set by gateway_claude.py during streaming when thinking_delta events
        are observed. Read by gateway.py to emit a `thinking` field in the
        SSE __meta__ event so the UI can render a Reasoning panel."""
        return getattr(self._tl, "last_thinking_text", "") or ""
    @last_thinking_text.setter
    def last_thinking_text(self, v: str):
        self._tl.last_thinking_text = v or ""

    @property
    def last_cache_read_tokens(self) -> int:
        return getattr(self._tl, "last_cache_read_tokens", 0)
    @last_cache_read_tokens.setter
    def last_cache_read_tokens(self, v: int):
        self._tl.last_cache_read_tokens = v

    @property
    def last_cache_creation_tokens(self) -> int:
        return getattr(self._tl, "last_cache_creation_tokens", 0)
    @last_cache_creation_tokens.setter
    def last_cache_creation_tokens(self, v: int):
        self._tl.last_cache_creation_tokens = v

    @property
    def last_decision(self) -> "FallbackInfo":
        """FallbackInfo from the most recent generate() call on this thread.

        Returns the _FALLBACK_INFO_NOT_SET sentinel when generate() has not
        been called yet on the current thread (reason == "not_set").

        NOTE: NOT updated by stream() — see FallbackInfo docstring for details.
        """
        return getattr(self._tl, "last_decision", _FALLBACK_INFO_NOT_SET)
    @last_decision.setter
    def last_decision(self, v: "FallbackInfo"):
        self._tl.last_decision = v

    # _last_actual_tier: set by _try_* methods alongside last_model_label so that
    # generate() can build FallbackInfo with accurate from/to tier information.
    @property
    def _last_actual_tier(self) -> str:
        return getattr(self._tl, "_last_actual_tier", "")
    @_last_actual_tier.setter
    def _last_actual_tier(self, v: str):
        self._tl._last_actual_tier = v

    # --------------------------------------------------------
    # LAZY GATEWAY LOADERS
    # --------------------------------------------------------

    def _get_local(self) -> Optional[object]:
        """Return the LiteLLM gateway (in-house Local models)."""
        if self._local is None:
            try:
                from gateway_local_llm import get_local_gateway
                self._local = get_local_gateway()
            except Exception as e:
                logger.warning(f"ModelRouter: Local gateway unavailable → {e}")
        return self._local

    def _get_openai(self) -> Optional[object]:
        # If no model is configured for this provider, treat it as unavailable.
        # Model IDs come entirely from env — an empty value means the operator
        # has not configured OpenAI, so skip it rather than sending an empty
        # model ID to the API (which would return an error).
        if not OPENAI_SIMPLE_MODEL and not OPENAI_CODING_MODEL:
            return None
        proxy = _llm_proxy_url()
        if self._openai is not None and isinstance(self._openai, _ProxyGateway) != bool(proxy):
            self._openai = None
        if self._openai is None:
            if proxy:
                self._openai = _ProxyGateway("openai")
            else:
                try:
                    from gateway_openai import OpenAIGateway
                    self._openai = OpenAIGateway()
                except Exception as e:
                    logger.warning(f"ModelRouter: OpenAI gateway unavailable → {e}")
        return self._openai

    def _get_claude(self) -> Optional[object]:
        # If no model is configured for this provider, treat it as unavailable.
        if not CLAUDE_PRIMARY_MODEL and not CLAUDE_HAIKU:
            return None
        proxy = _llm_proxy_url()
        if self._claude is not None and isinstance(self._claude, _ProxyGateway) != bool(proxy):
            self._claude = None
        if self._claude is None:
            if proxy:
                self._claude = _ProxyGateway("claude")
            else:
                try:
                    from gateway_claude import ClaudeGateway
                    self._claude = ClaudeGateway()
                except Exception as e:
                    logger.warning(f"ModelRouter: Claude gateway unavailable → {e}")
        return self._claude

    def _get_gemini(self) -> Optional[object]:
        # If no model is configured for this provider, treat it as unavailable.
        if not GEMINI_TEXT_MODEL and not GEMINI_IMAGE_MODEL:
            return None
        proxy = _llm_proxy_url()
        if self._gemini is not None and isinstance(self._gemini, _ProxyGateway) != bool(proxy):
            self._gemini = None
        if self._gemini is None:
            if proxy:
                self._gemini = _ProxyGateway("gemini")
            else:
                try:
                    from gateway_gemini import GeminiGateway
                    self._gemini = GeminiGateway()
                except Exception as e:
                    logger.warning(f"ModelRouter: Gemini gateway unavailable → {e}")
        return self._gemini

    # --------------------------------------------------------
    # SIGNAL DETECTION
    # --------------------------------------------------------

    @staticmethod
    def _detect_vision(prompt: str) -> bool:
        return bool(_VISION_RE.search(prompt))

    @staticmethod
    def _classify_complexity(prompt: str) -> str:
        try:
            from models.classifier import classify_query_complexity
            return classify_query_complexity(prompt)
        except Exception as e:
            logger.warning(f"ModelRouter: classifier failed → {e}")
            return TIER_MEDIUM

    # --------------------------------------------------------
    # ROUTING
    # --------------------------------------------------------

    def route(self, prompt, model_hint: Optional[str] = None,
              data_classification: Optional[str] = None,
              context_tokens: int = 0) -> RoutingDecision:
        """Return the RoutingDecision for this prompt.
        prompt: str OR list[dict] (multi-turn messages array).
        data_classification: optional sensitivity tag (PUBLIC/INTERNAL/
            CONFIDENTIAL/RESTRICTED/PCI_SENSITIVE, per core/rag_acl.py). When it
            is at/above CONFIDENTIAL the PRIVACY FLOOR forces the local model.
        context_tokens: optional estimated token footprint of the whole turn.
            When it would not fit the complexity-derived tier's window (with
            headroom), CONTEXT-SIZE ROUTING promotes to a larger-window model.
            Never overrides the privacy floor or an explicit model_hint.
        """
        prompt_str = _as_str(prompt)  # routing signals always derived from text

        # 0. PRIVACY FLOOR (hard enterprise invariant) — runs FIRST so nothing
        #    downstream (hint, vision, complexity) can re-route restricted data to
        #    a cloud provider. When the request carries CONFIDENTIAL+ data it is
        #    pinned to the in-house Local model (TIER_SIMPLE, which _dispatch maps
        #    to _try_local_simple). This override even supersedes an explicit
        #    model_hint: a user cannot opt restricted data onto the cloud.
        if _privacy_requires_local(data_classification):
            _cls = str(data_classification).strip().upper()
            # AUDIT/ALERT: every enforcement is logged at WARNING so it surfaces
            # in SIEM/alerting — a restricted turn hitting the cloud would be a
            # compliance incident, so we make the on-prem pin explicit.
            logger.warning(
                "ModelRouter: PRIVACY FLOOR enforced — data_classification=%s → "
                "pinned to LOCAL (TIER_SIMPLE); cloud providers bypassed "
                "(hint=%r ignored for privacy)", _cls, model_hint,
            )
            return RoutingDecision(
                tier=TIER_SIMPLE, model=_tier_label(TIER_SIMPLE),
                complexity=TIER_SIMPLE, is_vision=False,
                hint=model_hint, fallback=False,
            )

        # 1. Caller hint
        if model_hint:
            key = model_hint.lower()
            # "local:<model-id>" pins a SPECIFIC in-house model on the simple/local
            # tier (e.g. DOC_INTENT_MODEL=local:gemma or local:kimi-k2.7). The id
            # after the colon is forwarded to the local gateway's generate(model=…).
            if key.startswith("local:"):
                _local_model = model_hint.split(":", 1)[1].strip()
                if _local_model:
                    logger.info(f"ModelRouter: hint={model_hint!r} → tier=simple model={_local_model!r}")
                    return RoutingDecision(
                        tier=TIER_SIMPLE, model=f"Local ({_local_model})",
                        complexity=TIER_SIMPLE, is_vision=False,
                        hint=model_hint, fallback=False,
                        provider_model_override=_local_model,
                    )
            if key in _HINT_MAP:
                tier = _HINT_MAP[key]
                logger.info(f"ModelRouter: hint={model_hint!r} → tier={tier}")
                # When the hint resolves to a specific Gemini model ID, forward
                # it so the dispatcher hits THAT model instead of the gateway
                # default. Non-Gemini hints stay None and dispatch unchanged.
                _gemini_override = _GEMINI_SPECIFIC_HINTS.get(key)
                # Use the model-specific label when the user picked a specific
                # Gemini ID — otherwise the chat footer/meta would always show
                # the tier's default model (e.g. gemini-3.5-flash) regardless
                # of which Gemini model actually ran.
                _label = _gemini_model_label(_gemini_override) if _gemini_override else _tier_label(tier)
                return RoutingDecision(
                    tier=tier, model=_label,
                    complexity=tier, is_vision=(tier == TIER_VISION),  # TIER_GEMINI is not vision
                    hint=model_hint, fallback=False,
                    provider_model_override=_gemini_override,
                )

        # 2. Vision detection — only fires when:
        #    a) the caller did NOT pin a non-vision model hint (e.g. kimi, local, glm), AND
        #    b) the prompt actually contains an image content block (not just vision-adjacent
        #       words like "render" or "canvas" that appear naturally in code conversations).
        _hint_lower = (model_hint or "").lower()
        _hint_is_non_vision = any(_hint_lower.startswith(p) or p in _hint_lower
                                  for p in _NON_VISION_HINT_PREFIXES)
        if not _hint_is_non_vision and _prompt_has_image(prompt) and self._detect_vision(prompt_str):
            logger.info("ModelRouter: vision keywords + image attachment → Gemini")
            return RoutingDecision(
                tier=TIER_VISION, model=_tier_label(TIER_VISION),
                complexity="N/A", is_vision=True, hint=None, fallback=False,
            )

        # 3. Complexity classification — always use LLM-backed classifier.
        #    classify_with_confidence_llm() runs regex first; if regex confidence
        #    is below 0.7 it delegates to Claude Haiku for highest accuracy.
        #    No mechanical tier-bumping — the LLM result is the authoritative label.
        try:
            from models.classifier import classify_with_confidence_llm
            complexity, confidence = classify_with_confidence_llm(prompt_str)
        except Exception as _e:
            logger.warning(f"ModelRouter: classify_with_confidence_llm failed → {_e}")
            complexity, confidence = self._classify_complexity(prompt_str), 0.75

        tier = complexity
        logger.info(
            f"ModelRouter: classified complexity={complexity} confidence={confidence:.2f}"
            + (" (LLM)" if confidence >= 0.9 else " (regex)")
        )

        # 3b. Code-domain guard — upgrade code queries from simple to medium
        if tier == TIER_SIMPLE:
            try:
                from models.classifier import detect_query_domain
                if detect_query_domain(prompt_str) == "code":
                    logger.info("ModelRouter: code domain with simple complexity → upgrade to TIER_MEDIUM")
                    tier = TIER_MEDIUM
            except Exception as _e:
                logger.warning(f"ModelRouter: domain detection failed → {_e}")

        # 3c. CONTEXT-SIZE ROUTING (frontier pattern #5) — promote to a larger-
        #     window tier when the turn's token footprint won't fit. Runs last so
        #     it can lift any complexity-derived tier, but only for auto turns
        #     (explicit hints returned earlier) and never for privacy-pinned
        #     turns (returned earlier). Fail-safe: unchanged on any error.
        # When the caller did not pass an explicit count, estimate from the
        # prompt text (~4 chars/token) so context-size routing works even on the
        # existing call sites that only pass prompt+model_hint.
        if (not context_tokens or context_tokens <= 0) and _CONTEXT_SIZE_ROUTING:
            try:
                context_tokens = int(len(prompt_str) / 4)
            except Exception:  # noqa: BLE001
                context_tokens = 0
        if context_tokens and context_tokens > 0:
            _promoted = _promote_for_context(tier, context_tokens)
            if _promoted != tier:
                logger.info(
                    "ModelRouter: CONTEXT-SIZE ROUTING — context_tokens=%d exceeds "
                    "tier=%s window (%d); promoting to tier=%s window(%d)",
                    context_tokens, tier, _tier_window(tier),
                    _promoted, _tier_window(_promoted),
                )
                tier = _promoted

        logger.info(f"ModelRouter: final tier={tier} (complexity={complexity} confidence={confidence:.2f})")
        # Fix 2: for TIER_SIMPLE, pin the catalog pick NOW (once per request) so
        # route(), _try_local_simple_stream(), and generate() all see the same model
        # ID — even if the catalog refreshes between these three call sites.
        _simple_override: Optional[str] = None
        if tier == TIER_SIMPLE:
            try:
                from gateway_local_llm import _catalog as _lcat
                _simple_override = _lcat.pick("simple")
            except Exception:
                pass
        return RoutingDecision(
            tier=tier, model=_tier_label(tier),
            complexity=complexity, is_vision=False, hint=None, fallback=False,
            provider_model_override=_simple_override,
        )

    # --------------------------------------------------------
    # TOKEN PROPAGATION
    # --------------------------------------------------------

    def _propagate_tokens(self, tier: str) -> None:
        _gw_map = {
            TIER_SIMPLE:    self._local,
            TIER_MINI:      self._openai,
            TIER_LOCAL_MINI: self._openai,
            TIER_MEDIUM:    self._openai,
            TIER_DEEP:      self._openai,
            TIER_COMPLEX:   self._claude,
            TIER_HAIKU:     self._claude,
            TIER_VISION:    self._gemini,
            TIER_GEMINI:    self._gemini,
            TIER_SOLUTION:  self._claude,
            TIER_OPUS_48:   self._claude,
            TIER_OPUS_5:    self._claude,
            TIER_SONNET_5:  self._claude,
            TIER_TERA:      self._openai,
            TIER_LUNA:      self._openai,
        }
        gw = _gw_map.get(tier)
        self.last_input_tokens          = getattr(gw, "_last_input_tokens",          0) or 0
        self.last_output_tokens         = getattr(gw, "_last_output_tokens",         0) or 0
        self.last_cache_read_tokens     = getattr(gw, "_last_cache_read_tokens",     0) or 0
        self.last_cache_creation_tokens = getattr(gw, "_last_cache_creation_tokens", 0) or 0

    # --------------------------------------------------------
    # DISPATCH  (blocking — collects full response)
    # --------------------------------------------------------

    def _dispatch(self, tier: str, prompt: str, provider_model: Optional[str] = None, **kwargs) -> tuple[str, bool]:
        # kwargs carries precleared / precleared_findings when the upstream
        # caller has already run compliance_engine.validate_input().
        # privacy_local_only is consumed ONLY by the local path (a privacy-pinned
        # turn is always TIER_SIMPLE); pop it so it never leaks into cloud
        # gateways' generate() signatures.
        _privacy_local_only = kwargs.pop("privacy_local_only", False)
        if tier == TIER_SIMPLE:
            return self._try_local_simple(prompt, local_model=provider_model,
                                          privacy_local_only=_privacy_local_only, **kwargs)
        if tier == TIER_MINI:
            return self._try_openai_mini(prompt, **kwargs)
        if tier == TIER_LOCAL_MINI:
            # local_mini routes to the in-house GPU server (LOCAL_LLM_BASE_URL)
            # via the same local gateway used by TIER_SIMPLE. OPENAI_OSS_MODEL
            # carries the specific model ID to request (e.g. kimi-k2.7-code).
            return self._try_local_simple(prompt, local_model=OPENAI_OSS_MODEL or None, **kwargs)
        if tier == TIER_MEDIUM:
            return self._try_openai_coding(prompt, **kwargs)
        if tier == TIER_DEEP:
            return self._try_openai_deep(prompt, **kwargs)
        if tier == TIER_COMPLEX:
            return self._try_claude_sonnet(prompt, **kwargs)
        if tier == TIER_HAIKU:
            return self._try_claude_haiku(prompt, **kwargs)
        if tier in (TIER_VISION, TIER_GEMINI):
            return self._try_gemini(prompt, model=provider_model, **kwargs)
        if tier == TIER_SOLUTION:
            return self._try_claude_solution(prompt, **kwargs)
        if tier == TIER_OPUS_48:
            return self._try_claude_opus48(prompt, **kwargs)
        if tier == TIER_OPUS_5:
            return self._try_claude_opus5(prompt, **kwargs)
        if tier == TIER_SONNET_5:
            return self._try_claude_sonnet5(prompt, **kwargs)
        if tier == TIER_TERA:
            return self._try_openai_tera(prompt, **kwargs)
        if tier == TIER_LUNA:
            return self._try_openai_luna(prompt, **kwargs)
        # Fail SAFE rather than returning an error string as the model's answer.
        # An unrecognised tier reaching dispatch is a routing gap (e.g. a local
        # model id that never got a tier). Rather than surfacing "Error: unknown
        # routing tier" verbatim to the user, fall back to the local/simple tier
        # (forwarding the requested model as the local override) and log loudly so
        # the real gap is captured. Verified: local ids like glm-5.2-fp8 route
        # correctly through _try_local_simple.
        logger.warning(
            "ModelRouter: UNKNOWN TIER %r reached _dispatch (model=%r) — falling "
            "back to TIER_SIMPLE/local so the run does not fail with an error "
            "string. Add this tier/model to _HINT_MAP if this recurs.",
            tier, provider_model,
        )
        return self._try_local_simple(prompt, local_model=provider_model, **kwargs)

    def _try_local_simple(self, prompt: str, local_model: Optional[str] = None,
                          privacy_local_only: bool = False,
                          **kwargs) -> tuple[str, bool]:
        local = self._get_local()
        if local and local.available and not _CB_LOCAL.is_open:
            try:
                # local_model pins a SPECIFIC in-house model (from a
                # "local:<model>" hint); otherwise the gateway picks the tier default.
                if local_model:
                    result = self._collect(_CB_LOCAL.call(
                        local.generate, prompt, model=local_model, tier="simple"))
                else:
                    result = self._collect(_CB_LOCAL.call(local.generate, prompt, tier="simple"))
                if result and not result.startswith("Error"):
                    # Fix 1+2: read the model ID that generate() actually resolved.
                    _actual = (
                        local_model
                        or getattr(local, "_last_selected_model", None)
                    )
                    self.last_model_label = (
                        f"Local ({_actual})" if _actual else _tier_label(TIER_SIMPLE)
                    )
                    self._last_actual_tier = TIER_SIMPLE
                    return result, False
                logger.warning("ModelRouter: Local model returned error → fallback GPT-5 mini")
            except Exception as e:
                logger.warning(f"ModelRouter: Local failed → {e}")
        # PRIVACY FLOOR: for CONFIDENTIAL+ data we must FAIL CLOSED rather than
        # egress to a cloud provider. A local outage on restricted data returns
        # an explicit error (never OpenAI/Claude). This is the hard invariant.
        if privacy_local_only:
            logger.error(
                "ModelRouter: PRIVACY FLOOR — local model unavailable for "
                "restricted data; FAILING CLOSED (cloud fallback suppressed)."
            )
            self.last_model_label = _tier_label(TIER_SIMPLE)
            self._last_actual_tier = TIER_SIMPLE
            return ("Error: the in-house (local) model was requested but is not "
                    "available, and this request may not be sent to a cloud "
                    "provider. Check that LOCAL_LLM_BASE_URL points at a running "
                    "OpenAI-compatible server (e.g. http://localhost:11434 for "
                    "Ollama) and that it has at least one model pulled."), False
        logger.info("ModelRouter: Local unavailable → fallback GPT-5 mini")
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                result = self._collect(_CB_OPENAI.call(openai.generate, prompt, **kwargs))
                if not result.startswith("Error"):
                    self.last_model_label = f"{OPENAI_SIMPLE_DISPLAY} ({OPENAI_SIMPLE_MODEL}) [fallback]"
                    self._last_actual_tier = TIER_MINI
                    return result, True
            except Exception as e:
                logger.warning(f"ModelRouter: GPT-5 mini fallback failed → {e}")
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                result = self._collect(_CB_CLAUDE.call(
                    claude.generate, prompt, model=CLAUDE_PRIMARY_MODEL
                ))
                self.last_model_label = f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL}) [fallback]"
                self._last_actual_tier = TIER_COMPLEX
                return result, True
            except Exception as e:
                logger.warning(f"ModelRouter: Claude fallback failed → {e}")
        return "Error: no gateway available", False

    def _try_openai_mini(self, prompt: str, **kwargs) -> tuple[str, bool]:
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                result = self._collect(_CB_OPENAI.call(openai.generate, prompt, model=OPENAI_SIMPLE_MODEL, **kwargs))
                if not result.startswith("Error"):
                    self.last_model_label = f"{OPENAI_SIMPLE_DISPLAY} ({OPENAI_SIMPLE_MODEL})"
                    self._last_actual_tier = TIER_MINI
                    return result, False
                logger.warning("ModelRouter: GPT-5-mini failed → fallback Claude Sonnet")
            except Exception as e:
                logger.warning(f"ModelRouter: GPT-5-mini circuit-breaker rejected → {e}")
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                result = self._collect(_CB_CLAUDE.call(
                    claude.generate, prompt, model=CLAUDE_PRIMARY_MODEL
                ))
                self.last_model_label = f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL}) [fallback]"
                self._last_actual_tier = TIER_COMPLEX
                return result, True
            except Exception as e:
                logger.warning(f"ModelRouter: Claude fallback for mini failed → {e}")
        return "Error: no gateway available", False

    def _try_openai_oss(self, prompt: str, **kwargs) -> tuple[str, bool]:
        """Route to the in-house GPT-OSS-120B model (OpenAI-compat endpoint).

        Falls back to GPT-5-mini (cloud) if the OSS model is unavailable, so
        intent classification always gets an answer even during local outages.
        """
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                result = self._collect(_CB_OPENAI.call(
                    openai.generate, prompt, model=OPENAI_OSS_MODEL, **kwargs
                ))
                if not result.startswith("Error"):
                    self.last_model_label = f"{OPENAI_OSS_DISPLAY} ({OPENAI_OSS_MODEL})"
                    self._last_actual_tier = TIER_LOCAL_MINI
                    return result, False
                logger.warning("ModelRouter: GPT-OSS-120B failed → fallback GPT-5-mini")
            except Exception as e:
                logger.warning(f"ModelRouter: GPT-OSS-120B circuit-breaker rejected → {e}")
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                result = self._collect(_CB_OPENAI.call(
                    openai.generate, prompt, model=OPENAI_SIMPLE_MODEL, **kwargs
                ))
                if not result.startswith("Error"):
                    self.last_model_label = f"{OPENAI_SIMPLE_DISPLAY} ({OPENAI_SIMPLE_MODEL}) [fallback]"
                    self._last_actual_tier = TIER_MINI
                    return result, True
            except Exception as e:
                logger.warning(f"ModelRouter: GPT-5-mini fallback for OSS failed → {e}")
        return "Error: no gateway available", False

    def _try_openai_coding(self, prompt: str, **kwargs) -> tuple[str, bool]:
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                result = self._collect(_CB_OPENAI.call(openai.generate, prompt, **kwargs))
                if not result.startswith("Error"):
                    self.last_model_label = f"{OPENAI_CODING_DISPLAY} ({OPENAI_CODING_MODEL})"
                    self._last_actual_tier = TIER_MEDIUM
                    return result, False
                logger.warning("ModelRouter: GPT-5.4 failed → fallback Claude Sonnet")
            except Exception as e:
                logger.warning(f"ModelRouter: OpenAI circuit-breaker rejected → {e}")
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                result = self._collect(_CB_CLAUDE.call(
                    claude.generate, prompt, model=CLAUDE_PRIMARY_MODEL
                ))
                self.last_model_label = f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL}) [fallback]"
                self._last_actual_tier = TIER_COMPLEX
                return result, True
            except Exception as e:
                logger.warning(f"ModelRouter: Claude fallback failed → {e}")
        return "Error: no gateway available", False

    def _try_openai_deep(self, prompt: str, **kwargs) -> tuple[str, bool]:
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                result = self._collect(_CB_OPENAI.call(openai.generate, prompt, model=OPENAI_LATEST_MODEL, **kwargs))
                if not result.startswith("Error"):
                    self.last_model_label = f"{OPENAI_LATEST_DISPLAY} ({OPENAI_LATEST_MODEL})"
                    self._last_actual_tier = TIER_DEEP
                    return result, False
                logger.warning("ModelRouter: GPT-5.4 failed → fallback Claude Sonnet")
            except Exception as e:
                logger.warning(f"ModelRouter: GPT-5.4 circuit-breaker rejected → {e}")
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                result = self._collect(_CB_CLAUDE.call(
                    claude.generate, prompt, model=CLAUDE_PRIMARY_MODEL
                ))
                self.last_model_label = f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL}) [fallback]"
                self._last_actual_tier = TIER_COMPLEX
                return result, True
            except Exception as e:
                logger.warning(f"ModelRouter: Claude fallback for deep failed → {e}")
        return "Error: no gateway available", False

    def _try_openai_tera(self, prompt: str, **kwargs) -> tuple[str, bool]:
        """Dispatch to GPT-5.6 Tera (high-capacity variant). Falls back to Claude Sonnet."""
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                result = self._collect(_CB_OPENAI.call(openai.generate, prompt, model=OPENAI_TERA_MODEL, **kwargs))
                if not result.startswith("Error"):
                    self.last_model_label = f"{OPENAI_TERA_DISPLAY} ({OPENAI_TERA_MODEL})"
                    self._last_actual_tier = TIER_TERA
                    return result, False
                logger.warning("ModelRouter: GPT-5.6 Tera failed → fallback Claude Sonnet")
            except Exception as e:
                logger.warning(f"ModelRouter: GPT-5.6 Tera circuit-breaker rejected → {e}")
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                result = self._collect(_CB_CLAUDE.call(
                    claude.generate, prompt, model=CLAUDE_PRIMARY_MODEL
                ))
                self.last_model_label = f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL}) [fallback]"
                self._last_actual_tier = TIER_COMPLEX
                return result, True
            except Exception as e:
                logger.warning(f"ModelRouter: Claude fallback for Tera failed → {e}")
        return "Error: no gateway available", False

    def _try_openai_luna(self, prompt: str, **kwargs) -> tuple[str, bool]:
        """Dispatch to GPT-5.6 Luna (efficient variant). Falls back to Claude Sonnet."""
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                result = self._collect(_CB_OPENAI.call(openai.generate, prompt, model=OPENAI_LUNA_MODEL, **kwargs))
                if not result.startswith("Error"):
                    self.last_model_label = f"{OPENAI_LUNA_DISPLAY} ({OPENAI_LUNA_MODEL})"
                    self._last_actual_tier = TIER_LUNA
                    return result, False
                logger.warning("ModelRouter: GPT-5.6 Luna failed → fallback Claude Sonnet")
            except Exception as e:
                logger.warning(f"ModelRouter: GPT-5.6 Luna circuit-breaker rejected → {e}")
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                result = self._collect(_CB_CLAUDE.call(
                    claude.generate, prompt, model=CLAUDE_PRIMARY_MODEL
                ))
                self.last_model_label = f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL}) [fallback]"
                self._last_actual_tier = TIER_COMPLEX
                return result, True
            except Exception as e:
                logger.warning(f"ModelRouter: Claude fallback for Luna failed → {e}")
        return "Error: no gateway available", False

    def _try_claude_sonnet(self, prompt: str, **kwargs) -> tuple[str, bool]:
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                result = self._collect(_CB_CLAUDE.call(
                    claude.generate, prompt, model=CLAUDE_PRIMARY_MODEL
                ))
                if not result.startswith("Error"):
                    self.last_model_label = f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL})"
                    self._last_actual_tier = TIER_COMPLEX
                    return result, False
                logger.warning("ModelRouter: Claude Sonnet failed → fallback GPT-5.4")
            except Exception as e:
                logger.warning(f"ModelRouter: Claude circuit-breaker rejected → {e}")
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                result = self._collect(_CB_OPENAI.call(openai.generate, prompt, **kwargs))
                self.last_model_label = f"{OPENAI_CODING_DISPLAY} ({OPENAI_CODING_MODEL}) [fallback]"
                self._last_actual_tier = TIER_MEDIUM
                return result, True
            except Exception as e:
                logger.warning(f"ModelRouter: OpenAI fallback failed → {e}")
        return "Error: no gateway available", False

    def _try_claude_haiku(self, prompt: str, **kwargs) -> tuple[str, bool]:
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                result = self._collect(_CB_CLAUDE.call(
                    claude.generate, prompt, model=CLAUDE_HAIKU
                ))
                if not result.startswith("Error"):
                    self.last_model_label = f"{CLAUDE_HAIKU_DISPLAY} ({CLAUDE_HAIKU})"
                    self._last_actual_tier = TIER_HAIKU
                    return result, False
                logger.warning("ModelRouter: Claude Haiku failed → fallback GPT-5.4")
            except Exception as e:
                logger.warning(f"ModelRouter: Claude Haiku circuit-breaker rejected → {e}")
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                result = self._collect(_CB_OPENAI.call(openai.generate, prompt, **kwargs))
                self.last_model_label = f"{OPENAI_CODING_DISPLAY} ({OPENAI_CODING_MODEL}) [fallback]"
                self._last_actual_tier = TIER_MEDIUM
                return result, True
            except Exception as e:
                logger.warning(f"ModelRouter: OpenAI fallback for Haiku failed → {e}")
        return "Error: no gateway available", False

    def _try_claude_solution(self, prompt: str, **kwargs) -> tuple[str, bool]:
        """Solution-tier: Opus if ENABLE_OPUS=true, otherwise Sonnet. Falls back to Sonnet."""
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                result = self._collect(_CB_CLAUDE.call(
                    claude.generate, prompt, model=SOLUTION_MODEL
                ))
                if not result.startswith("Error"):
                    self.last_model_label = _tier_label(TIER_SOLUTION)
                    self._last_actual_tier = TIER_SOLUTION
                    return result, False
                logger.warning("ModelRouter: solution model failed → fallback Claude Sonnet")
            except Exception as e:
                logger.warning(f"ModelRouter: solution circuit-breaker rejected → {e}")
        # Fallback: Sonnet — force was_fallback=True regardless of Sonnet's own result.
        result, _ = self._try_claude_sonnet(prompt, **kwargs)
        return result, True

    def _try_claude_opus48(self, prompt: str, **kwargs) -> tuple[str, bool]:
        """Explicit Claude Opus 4.8 selection (CLI/IDE only). Falls back to Sonnet."""
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                result = self._collect(_CB_CLAUDE.call(
                    claude.generate, prompt, model=CLAUDE_OPUS_48_MODEL
                ))
                if not result.startswith("Error"):
                    self.last_model_label = f"{CLAUDE_OPUS_48_DISPLAY} ({CLAUDE_OPUS_48_MODEL})"
                    return result, False
                logger.warning("ModelRouter: Claude Opus 4.8 failed → fallback Claude Sonnet")
            except Exception as e:
                logger.warning(f"ModelRouter: Claude Opus 4.8 circuit-breaker rejected → {e}")
        return self._try_claude_sonnet(prompt, **kwargs)

    def _try_claude_opus5(self, prompt: str, **kwargs) -> tuple[str, bool]:
        """Explicit Claude Opus 5 selection (CLI/IDE opt-in). Falls back to Sonnet."""
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                result = self._collect(_CB_CLAUDE.call(
                    claude.generate, prompt, model=CLAUDE_OPUS_5_MODEL
                ))
                if not result.startswith("Error"):
                    self.last_model_label = f"{CLAUDE_OPUS_5_DISPLAY} ({CLAUDE_OPUS_5_MODEL})"
                    self._last_actual_tier = TIER_OPUS_5
                    return result, False
                logger.warning("ModelRouter: Claude Opus 5 failed → fallback Claude Sonnet")
            except Exception as e:
                logger.warning(f"ModelRouter: Claude Opus 5 circuit-breaker rejected → {e}")
        return self._try_claude_sonnet(prompt, **kwargs)

    def _try_claude_sonnet5(self, prompt: str, **kwargs) -> tuple[str, bool]:
        """Explicit Claude Sonnet 5 selection (all channels). Falls back to Sonnet 4.6."""
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                result = self._collect(_CB_CLAUDE.call(
                    claude.generate, prompt, model=CLAUDE_SONNET_5_MODEL
                ))
                if not result.startswith("Error"):
                    self.last_model_label = f"{CLAUDE_SONNET_5_DISPLAY} ({CLAUDE_SONNET_5_MODEL})"
                    self._last_actual_tier = TIER_SONNET_5
                    return result, False
                logger.warning("ModelRouter: Claude Sonnet 5 failed → fallback Claude Sonnet 4.6")
            except Exception as e:
                logger.warning(f"ModelRouter: Claude Sonnet 5 circuit-breaker rejected → {e}")
        return self._try_claude_sonnet(prompt, **kwargs)

    def _try_gemini(self, prompt: str, model: Optional[str] = None, **kwargs) -> tuple[str, bool]:
        gemini = self._get_gemini()
        if gemini and not _CB_GEMINI.is_open:
            try:
                # model=None → gateway uses its module-level MODEL default.
                _kw = {"model": model} if model else {}
                _kw.update(kwargs)  # forward precleared / precleared_findings
                result = self._collect(_CB_GEMINI.call(gemini.generate, prompt, **_kw))
                if not result.startswith("Error"):
                    # last_model_label already set to tier-appropriate label by route()
                    # _last_actual_tier mirrors the original vision/gemini tier
                    self._last_actual_tier = TIER_VISION  # set here; route() already has the exact tier
                    return result, False
                logger.warning("ModelRouter: Gemini failed → fallback Claude Sonnet")
            except Exception as e:
                logger.warning(f"ModelRouter: Gemini circuit-breaker rejected → {e}")
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                result = self._collect(_CB_CLAUDE.call(
                    claude.generate, prompt, model=CLAUDE_PRIMARY_MODEL
                ))
                self.last_model_label = f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL}) [fallback]"
                self._last_actual_tier = TIER_COMPLEX
                return result, True
            except Exception as e:
                logger.warning(f"ModelRouter: Claude fallback failed → {e}")
        return "Error: no gateway available", False

    # --------------------------------------------------------
    # STREAMING DISPATCH  (yields tokens, never collects)
    # --------------------------------------------------------

    def _dispatch_stream(
            self,
            tier: str,
            prompt: str,
            local_model: str = None,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
            provider_model: Optional[str] = None,
    ):
        # precleared / precleared_findings are only meaningful for providers
        # that run a second-pass compliance gate inside their generate()
        # (OpenAI + Gemini, direct or via LLM proxy). Claude and Local LLM do
        # not re-validate, so they ignore the flag.
        if tier == TIER_SIMPLE:
            # Fix: honor an explicit local model override on the STREAMING path.
            # Previously provider_model (from a "local:<id>" hint) was dropped
            # here — only local_model= was forwarded — so forcing kimi-k2.7/
            # glm-5.2 on chat streaming silently fell back to the tier default.
            yield from self._try_local_simple_stream(
                prompt, local_model=(local_model or provider_model),
                precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_MINI:
            yield from self._try_openai_mini_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_LOCAL_MINI:
            # local_mini routes to the in-house GPU server (LOCAL_LLM_BASE_URL)
            # via the same local gateway used by TIER_SIMPLE. OPENAI_OSS_MODEL
            # carries the specific model ID to request (e.g. kimi-k2.7-code).
            yield from self._try_local_simple_stream(
                prompt,
                local_model=OPENAI_OSS_MODEL or None,
                precleared=precleared,
                precleared_findings=precleared_findings,
            )
        elif tier == TIER_MEDIUM:
            yield from self._try_openai_coding_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_DEEP:
            yield from self._try_openai_deep_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_COMPLEX:
            # Sonnet fallback can land on OpenAI — forward flag for that case.
            yield from self._try_claude_sonnet_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_HAIKU:
            yield from self._try_claude_haiku_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier in (TIER_VISION, TIER_GEMINI):
            yield from self._try_gemini_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
                model=provider_model,
            )
        elif tier == TIER_SOLUTION:
            yield from self._try_claude_solution_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_OPUS_48:
            yield from self._try_claude_opus48_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_OPUS_5:
            yield from self._try_claude_opus5_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_SONNET_5:
            yield from self._try_claude_sonnet5_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_TERA:
            yield from self._try_openai_tera_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_LUNA:
            yield from self._try_openai_luna_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        else:
            # Fail SAFE (see _dispatch): an unknown tier falls back to the local/
            # simple stream forwarding the requested model, rather than yielding
            # "Error: unknown routing tier" as the streamed answer.
            logger.warning(
                "ModelRouter: UNKNOWN TIER %r reached _dispatch_stream (model=%r) "
                "— falling back to TIER_SIMPLE/local. Add this tier/model to "
                "_HINT_MAP if this recurs.",
                tier, provider_model,
            )
            yield from self._try_local_simple_stream(
                prompt, local_model=(local_model or provider_model),
                precleared=precleared, precleared_findings=precleared_findings,
            )

    def _try_local_simple_stream(
            self,
            prompt: str,
            local_model: str = None,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        # Fix 5: emit at WARNING so this line survives log-level filters and
        # appears in per-request exports even when INFO is suppressed.
        # Cross-check: if [LLM DISPATCH]/[LOCAL USAGE] are absent from an export
        # but this line IS present, the request was served from the Redis/semantic
        # cache before reaching generate() — check bypass metrics
        # (ainxt:bypass:{date}:redis / :semantic) to confirm.
        from core.logger import get_request_id as _gri
        logger.warning(
            "[LOCAL STREAM ENTRY] request_id=%s local_model=%r cb_open=%s",
            _gri() or "n/a",
            local_model,
            _CB_LOCAL.is_open,
        )
        local = self._get_local()
        if local and local.available and not _CB_LOCAL.is_open:
            try:
                token_yielded = False
                for tok in local.generate(prompt, model=local_model, tier="simple"):
                    if tok and not tok.startswith("Error"):
                        token_yielded = True
                        yield tok
                if token_yielded:
                    # Fix 1+2: use the model ID that generate() actually resolved
                    # (_last_selected_model) rather than re-calling _tier_label(TIER_SIMPLE),
                    # which invokes _catalog.pick() again and can return a different entry
                    # if the catalog refreshed between the generate() call and here.
                    _actual = (
                        local_model
                        or getattr(local, "_last_selected_model", None)
                    )
                    self.last_model_label = (
                        f"Local ({_actual})" if _actual else _tier_label(TIER_SIMPLE)
                    )
                    return
                logger.info("ModelRouter stream: Local empty/error → fallback GPT-5 mini")
            except Exception as e:
                logger.warning(f"ModelRouter stream: Local failed → {e}")
        logger.info("ModelRouter: Local unavailable → fallback GPT-5 mini")
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                self.last_model_label = f"{OPENAI_SIMPLE_DISPLAY} ({OPENAI_SIMPLE_MODEL}) [fallback]"
                yield from openai.generate(
                    prompt,
                    model=OPENAI_SIMPLE_MODEL,
                    precleared=precleared,
                    precleared_findings=precleared_findings,
                )
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: GPT-5 mini fallback failed → {e}")
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                self.last_model_label = f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL}) [fallback]"
                yield from claude.generate(prompt, model=CLAUDE_PRIMARY_MODEL)
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: Claude fallback failed → {e}")
        yield "Error: no gateway available"

    def _try_openai_mini_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                self.last_model_label = f"{OPENAI_SIMPLE_DISPLAY} ({OPENAI_SIMPLE_MODEL})"
                yield from openai.generate(
                    prompt,
                    model=OPENAI_SIMPLE_MODEL,
                    precleared=precleared, precleared_findings=precleared_findings,
                )
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: GPT-5-mini failed → {e}")
        # Env-configurable fallback chain: haiku → local:kimi-k2.7 → local:glm-5.2
        # (CHAT_FALLBACK_CHAIN). Each hop is circuit-breaker gated; first hop that
        # yields tokens wins. Replaces the old hard-coded Claude-Sonnet fallback.
        _yielded = yield from self._walk_fallback_chain_stream(
            prompt, precleared=precleared, precleared_findings=precleared_findings,
        )
        if _yielded:
            return
        yield "Error: no gateway available"

    def _walk_fallback_chain_stream(
            self, prompt: str, *, precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """Walk CHAT_FALLBACK_CHAIN, yielding from the first reachable hop.

        Returns True (via StopIteration value) if any hop produced tokens, else
        False so the caller can emit the terminal sentinel. Each hop respects its
        circuit breaker; both local hops share _CB_LOCAL (skipped once open).
        Never raises — a failing hop is logged and the walk continues.
        """
        for _hop in CHAT_FALLBACK_CHAIN:
            try:
                if _hop == "haiku":
                    if _CB_CLAUDE.is_open:
                        continue
                    self.last_model_label = f"{CLAUDE_HAIKU_DISPLAY} ({CLAUDE_HAIKU}) [fallback]"
                    _got = False
                    for _tok in self._try_claude_haiku_stream(
                            prompt, precleared=precleared,
                            precleared_findings=precleared_findings):
                        if isinstance(_tok, str) and _tok.startswith("Error:"):
                            break
                        _got = True
                        yield _tok
                    if _got:
                        return True
                elif _hop.startswith("local:"):
                    if _CB_LOCAL.is_open:
                        continue  # local breaker open → skip all local hops
                    _lid = _hop.split(":", 1)[1].strip()
                    _got = False
                    for _tok in self._try_local_simple_stream(
                            prompt, local_model=_lid,
                            precleared=precleared,
                            precleared_findings=precleared_findings):
                        if isinstance(_tok, str) and _tok.startswith("Error:"):
                            break
                        _got = True
                        yield _tok
                    if _got:
                        return True
                else:
                    logger.warning(f"ModelRouter: unknown fallback hop {_hop!r} — skipping")
            except Exception as e:  # noqa: BLE001 — a bad hop must not break the walk
                logger.warning(f"ModelRouter: fallback hop {_hop!r} failed → {e}")
        return False

    def _try_openai_oss_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """Streaming dispatch to in-house GPT-OSS model (TIER_LOCAL_MINI).

        Mirrors the blocking _try_openai_oss() path: primary is OPENAI_OSS_MODEL
        (in-house hosted, OpenAI-compat API, zero cloud cost); falls back to
        OPENAI_SIMPLE_MODEL (GPT-5-mini) when the in-house endpoint is down or
        the circuit breaker is open.
        """
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                self.last_model_label = f"{OPENAI_OSS_DISPLAY} ({OPENAI_OSS_MODEL})"
                yield from openai.generate(
                    prompt,
                    model=OPENAI_OSS_MODEL,
                    precleared=precleared, precleared_findings=precleared_findings,
                )
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: GPT-OSS failed → {e}")
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                self.last_model_label = f"{OPENAI_SIMPLE_DISPLAY} ({OPENAI_SIMPLE_MODEL}) [fallback]"
                yield from openai.generate(
                    prompt,
                    model=OPENAI_SIMPLE_MODEL,
                    precleared=precleared, precleared_findings=precleared_findings,
                )
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: GPT-5-mini fallback for OSS failed → {e}")
        yield "Error: no gateway available"

    def _try_openai_coding_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                self.last_model_label = f"{OPENAI_CODING_DISPLAY} ({OPENAI_CODING_MODEL})"
                yield from openai.generate(
                    prompt,
                    model=OPENAI_CODING_MODEL,
                    precleared=precleared, precleared_findings=precleared_findings,
                )
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: GPT-5.4 failed → {e}")
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                self.last_model_label = f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL}) [fallback]"
                yield from claude.generate(prompt, model=CLAUDE_PRIMARY_MODEL)
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: Claude fallback failed → {e}")
        yield "Error: no gateway available"

    def _try_openai_deep_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                self.last_model_label = f"{OPENAI_LATEST_DISPLAY} ({OPENAI_LATEST_MODEL})"
                yield from openai.generate(
                    prompt, model=OPENAI_LATEST_MODEL,
                    precleared=precleared, precleared_findings=precleared_findings,
                )
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: GPT-5.4 failed → {e}")
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                self.last_model_label = f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL}) [fallback]"
                yield from claude.generate(prompt, model=CLAUDE_PRIMARY_MODEL)
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: Claude fallback for deep failed → {e}")
        yield "Error: no gateway available"

    def _try_openai_tera_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """Streaming dispatch to GPT-5.6 Tera. Falls back to Claude Sonnet."""
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                self.last_model_label = f"{OPENAI_TERA_DISPLAY} ({OPENAI_TERA_MODEL})"
                yield from openai.generate(
                    prompt, model=OPENAI_TERA_MODEL,
                    precleared=precleared, precleared_findings=precleared_findings,
                )
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: GPT-5.6 Tera failed → {e}")
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                self.last_model_label = f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL}) [fallback]"
                yield from claude.generate(prompt, model=CLAUDE_PRIMARY_MODEL)
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: Claude fallback for Tera failed → {e}")
        yield "Error: no gateway available"

    def _try_openai_luna_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """Streaming dispatch to GPT-5.6 Luna. Falls back to Claude Sonnet."""
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                self.last_model_label = f"{OPENAI_LUNA_DISPLAY} ({OPENAI_LUNA_MODEL})"
                yield from openai.generate(
                    prompt, model=OPENAI_LUNA_MODEL,
                    precleared=precleared, precleared_findings=precleared_findings,
                )
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: GPT-5.6 Luna failed → {e}")
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                self.last_model_label = f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL}) [fallback]"
                yield from claude.generate(prompt, model=CLAUDE_PRIMARY_MODEL)
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: Claude fallback for Luna failed → {e}")
        yield "Error: no gateway available"

    def _try_claude_sonnet_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                self.last_model_label = f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL})"
                yield from claude.generate(prompt, model=CLAUDE_PRIMARY_MODEL)
                # Forward any extended-thinking content captured by the Claude
                # gateway during streaming so the UI can render it.
                try:
                    self.last_thinking_text = getattr(claude, "_last_thinking_text", "") or ""
                except Exception:
                    pass
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: Claude Sonnet failed → {e}")
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                self.last_model_label = f"{OPENAI_CODING_DISPLAY} ({OPENAI_CODING_MODEL}) [fallback]"
                # OpenAI fallback path: forward precleared so /ask false-positive
                # blocks don't reappear when Claude trips a circuit breaker.
                yield from openai.generate(
                    prompt,
                    model=OPENAI_CODING_MODEL,
                    precleared=precleared, precleared_findings=precleared_findings,
                )
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: OpenAI fallback failed → {e}")
        yield "Error: no gateway available"

    def _try_claude_haiku_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                self.last_model_label = f"{CLAUDE_HAIKU_DISPLAY} ({CLAUDE_HAIKU})"
                yield from claude.generate(prompt, model=CLAUDE_HAIKU)
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: Claude Haiku failed → {e}")
        openai = self._get_openai()
        if openai and not _CB_OPENAI.is_open:
            try:
                self.last_model_label = f"{OPENAI_CODING_DISPLAY} ({OPENAI_CODING_MODEL}) [fallback]"
                yield from openai.generate(
                    prompt,
                    model=OPENAI_CODING_MODEL,
                    precleared=precleared, precleared_findings=precleared_findings,
                )
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: OpenAI fallback for Haiku failed → {e}")
        yield "Error: no gateway available"

    def _try_claude_solution_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """Solution-tier streaming: Opus if ENABLE_OPUS=true, else Sonnet."""
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                self.last_model_label = _tier_label(TIER_SOLUTION)
                yield from claude.generate(prompt, model=SOLUTION_MODEL)
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: solution model failed → {e}")
        yield from self._try_claude_sonnet_stream(
            prompt, precleared=precleared, precleared_findings=precleared_findings,
        )

    def _try_claude_opus5_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """Explicit Claude Opus 5 streaming (CLI/IDE opt-in). Falls back to Sonnet."""
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                self.last_model_label = f"{CLAUDE_OPUS_5_DISPLAY} ({CLAUDE_OPUS_5_MODEL})"
                yield from claude.generate(prompt, model=CLAUDE_OPUS_5_MODEL)
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: Claude Opus 5 failed → {e}")
        yield from self._try_claude_sonnet_stream(
            prompt, precleared=precleared, precleared_findings=precleared_findings,
        )

    def _try_claude_opus48_stream(
        self,
        prompt: str,
        precleared: bool = False,
        precleared_findings: Optional[list] = None,
    ):
        """Explicit Claude Opus 4.8 streaming (CLI-only). Falls back to Sonnet."""
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                self.last_model_label = f"{CLAUDE_OPUS_48_DISPLAY} ({CLAUDE_OPUS_48_MODEL})"
                yield from claude.generate(prompt, model=CLAUDE_OPUS_48_MODEL)
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: Claude Opus 4.8 failed → {e}")
        yield from self._try_claude_sonnet_stream(
            prompt, precleared=precleared, precleared_findings=precleared_findings,
        )

    def _try_claude_sonnet5_stream(
        self,
        prompt: str,
        precleared: bool = False,
        precleared_findings: Optional[list] = None,
    ):
        """Explicit Claude Sonnet 5 streaming (all channels). Falls back to Sonnet 4.6."""
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                self.last_model_label = f"{CLAUDE_SONNET_5_DISPLAY} ({CLAUDE_SONNET_5_MODEL})"
                yield from claude.generate(prompt, model=CLAUDE_SONNET_5_MODEL)
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: Claude Sonnet 5 failed → {e}")
        yield from self._try_claude_sonnet_stream(
            prompt, precleared=precleared, precleared_findings=precleared_findings,
        )

    def _try_gemini_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
            model: Optional[str] = None,
    ):
        gemini = self._get_gemini()
        if gemini and not _CB_GEMINI.is_open:
            try:
                # last_model_label already set to tier-appropriate label by stream().
                # model=None → gateway uses its module-level MODEL default.
                _kw = {"model": model} if model else {}
                yield from gemini.generate(
                    prompt, precleared=precleared, precleared_findings=precleared_findings, **_kw,
                )
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: Gemini failed → {e}")
        claude = self._get_claude()
        if claude and not _CB_CLAUDE.is_open:
            try:
                self.last_model_label = f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL}) [fallback]"
                yield from claude.generate(prompt, model=CLAUDE_PRIMARY_MODEL)
                return
            except Exception as e:
                logger.warning(f"ModelRouter stream: Claude fallback failed → {e}")
        yield "Error: no gateway available"

    # --------------------------------------------------------
    # HELPERS
    # --------------------------------------------------------

    @staticmethod
    def _collect(gen) -> str:
        if isinstance(gen, str):
            return gen
        try:
            # Only join real text tokens. The stream may also yield non-string
            # sentinels — __stream_meta__ dicts (token counts) and ReasoningMarker
            # / ToolMarker objects (Gap #2/Phase 5). These MUST be skipped, not
            # coerced, so a blocking generate() never picks up reasoning text or
            # raises inside join(). (Markers str() to "" anyway, but skipping is
            # explicit and safe.)
            return "".join(t for t in gen if isinstance(t, str) and t)
        except Exception as e:
            return f"Error collecting response: {e}"

    # --------------------------------------------------------
    # PUBLIC API
    # --------------------------------------------------------

    def generate_structured(self, blocks: list, model_hint: str = "solution") -> str:
        """
        Claude-only call with structured content_blocks for block-level prompt caching.

        In production (LLM_PROXY_URL set), routes via _ProxyGateway which forwards
        the structured payload to services/llm_proxy/main.py on the LLM proxy server.

        In dev mode (no LLM_PROXY_URL) or when Claude is unavailable, flattens all
        blocks to a single string and delegates to generate() — no behavior change.

        Returns the model's text output (same shape as generate()).
        Never raises — falls back to flat generate() on any error.
        """
        claude = self._get_claude()

        # Dev mode or Claude unavailable: flatten to flat-string generate()
        if claude is None or not isinstance(claude, _ProxyGateway):
            flat = "\n\n".join(b.get("text", "") for b in blocks if b.get("text"))
            return self.generate(flat, model_hint=model_hint)

        # Resolve model for the given hint
        decision = self.route("placeholder", model_hint=model_hint)
        _model: Optional[str] = None
        if decision.tier == TIER_SOLUTION:
            _model = SOLUTION_MODEL
        elif decision.tier == TIER_OPUS_48:
            _model = CLAUDE_OPUS_48_MODEL
        elif decision.tier == TIER_OPUS_5:
            _model = CLAUDE_OPUS_5_MODEL
        elif decision.tier == TIER_SONNET_5:
            _model = CLAUDE_SONNET_5_MODEL
        elif decision.tier in (TIER_COMPLEX, TIER_HAIKU):
            _model = CLAUDE_PRIMARY_MODEL
        else:
            _model = CLAUDE_PRIMARY_MODEL  # default Claude for any other tier

        self.last_tier                  = decision.tier
        self.last_model_label           = decision.model
        self.last_cache_read_tokens     = 0
        self.last_cache_creation_tokens = 0

        try:
            result = self._collect(
                _CB_CLAUDE.call(claude.generate, model=_model, content_blocks=blocks)
            )
            if not result or result.startswith("Error"):
                raise ValueError(f"generate_structured: bad result: {result!r}")
            self._propagate_tokens(decision.tier)
            logger.info(
                f"ModelRouter.generate_structured → {decision.model} "
                f"cache_read={self.last_cache_read_tokens} "
                f"cache_created={self.last_cache_creation_tokens}"
            )
            return result
        except Exception as e:
            _flat_len = sum(len(b.get("text", "")) for b in blocks)
            logger.warning(
                f"ModelRouter.generate_structured: Claude failed ({type(e).__name__}: {e}) — "
                f"flattening {len(blocks)} blocks (~{_flat_len} chars) to generate()"
            )
            flat = "\n\n".join(b.get("text", "") for b in blocks if b.get("text"))
            return self.generate(flat, model_hint=model_hint)

    @staticmethod
    def _hint_is_explicit_local(model_hint: Optional[str]) -> bool:
        """True when the caller explicitly asked for the in-house/local model.

        Distinct from the "simple" hint, which means "auto-route: try local, fall
        back to cloud" and must keep that behaviour. "local", "local:<model>" and
        "inhouse" are a deliberate choice of the model the API labels
        "Local (In-house) - In-house GPU, free, private", so falling back to a
        paid cloud provider would silently send that turn off the machine.
        """
        h = (model_hint or "").lower().strip()
        return h in ("local", "inhouse", "in-house") or h.startswith("local:")

    def generate(self, prompt, model_hint: Optional[str] = None, return_meta=False,
                 precleared: bool = False, precleared_findings: Optional[list] = None,
                 data_classification: Optional[str] = None):
        """Route prompt to the correct gateway. Never raises — returns error str on failure.
        prompt: str OR list[dict] (multi-turn messages array).

        precleared / precleared_findings:
            When True, downstream OpenAI/Gemini gateways skip their second-pass
            compliance_engine.validate_input() block decision and re-use the
            provided findings for redaction only.  Used by callers (e.g.
            _enhance_core) that have already run an upstream compliance gate on
            the raw user input.
        data_classification:
            Optional sensitivity tag. When at/above CONFIDENTIAL the PRIVACY
            FLOOR pins routing to the local model AND disables cloud fallback in
            _dispatch — restricted data must never egress, even if local is down.
        """
        if not prompt:
            return ""
        decision = self.route(prompt, model_hint=model_hint,
                              data_classification=data_classification)
        logger.info(f"ModelRouter → {decision.model} (tier={decision.tier})")
        # PRIVACY FLOOR: when enforced, no-cloud-fallback is propagated into
        # _dispatch so a local outage fails closed instead of egressing.
        _privacy_local_only = _privacy_requires_local(data_classification)
        # An explicit local request is local-only for the same reason restricted
        # data is: the caller chose a model advertised as in-house and private.
        if self._hint_is_explicit_local(model_hint):
            if not _privacy_local_only:
                logger.info(
                    "ModelRouter: explicit local hint %r — cloud fallback disabled "
                    "for this turn", model_hint,
                )
            _privacy_local_only = True

        # Seed the label from the decision so the meta footer reflects the
        # routed model (incl. the specific Gemini variant), even when no _try_*
        # method overrides it. _try_* fallbacks (e.g. Claude on Gemini failure)
        # still overwrite this with their own "[fallback]" label.
        self.last_model_label = decision.model

        # Start latency timer BEFORE the LLM call so elapsed reflects real wall-clock time.
        import time
        _t0 = time.perf_counter()

        # Build compliance kwargs to forward through _dispatch → _try_* → gateway.generate()
        _compliance_kw = {}
        if precleared:
            _compliance_kw = {"precleared": True, "precleared_findings": precleared_findings or []}

        if _privacy_local_only:
            _compliance_kw["privacy_local_only"] = True

        output, was_fallback = self._dispatch(
            decision.tier, prompt, provider_model=decision.provider_model_override,
            **_compliance_kw,
        )

        elapsed = time.perf_counter() - _t0

        if was_fallback:
            logger.warning(f"ModelRouter: fallback used for tier={decision.tier}")
        self.last_tier = decision.tier
        self._propagate_tokens(decision.tier)

        if not return_meta:
            return output

        # elapsed was already measured above (line _t0 → after _dispatch)
        # Do NOT reset the timer here — that would always give latency=0.000
        in_tok  = getattr(self, "last_input_tokens",  0) or 0
        out_tok = getattr(self, "last_output_tokens", 0) or 0

        from gateway import _estimate_cost
        meta = {
            "model":    self.last_model_label,
            "in_tok":   in_tok,
            "out_tok":  out_tok,
            "tokens":   in_tok + out_tok,
            "cost_usd": _estimate_cost(self.last_model_label, in_tok, out_tok),
            "latency":  round(elapsed, 3),
        }
        return {"text": output, "meta": meta}

    def stream(
            self,
            prompt,
            model_hint: Optional[str] = None,
            local_model: Optional[str] = None,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """Route prompt and yield tokens directly (true token streaming).
        prompt: str OR list[dict] (multi-turn messages array).

        precleared / precleared_findings:
            Forwarded to the OpenAI / Gemini / proxy gateways so that callers
            (the FastAPI /ask handler) which have already run
            compliance_engine.validate_input() on the current user turn can
            instruct the downstream provider gateway to skip its second-pass
            block decision and re-use the gateway's findings for redaction.
            Default False preserves full second-pass validation for callers
            that have not run an upstream compliance gate.

        ── Final sentinel ───────────────────────────────────────────────
        After token streaming completes, this generator yields ONE extra
        dict of shape:
            {"__stream_meta__": {
                "in_tok":      int,
                "out_tok":     int,
                "model_label": str,
                "tier":        str,
                "thinking":    str,   # extended-thinking text if any
            }}

        Why a sentinel and not properties?
            Under uvicorn + FastAPI's StreamingResponse, the sync generator
            is driven via anyio.iterate_in_threadpool, which can resume each
            next() call on a DIFFERENT worker thread. The previous design
            stored token counts in `self._tl.last_input_tokens` (a
            threading.local) and the gateway read them after the loop —
            but if the final _propagate_tokens write landed on thread T2
            and the gateway's read happened on T3, T3's local was 0, the
            fallback word-count gave a tiny number (~3), and observed
            in_tok was wrong. Carrying the values *as data* through the
            yield protocol bypasses thread state entirely.

        Callers that need the meta should detect the sentinel:
            for tok in mr.stream(...):
                if isinstance(tok, dict) and "__stream_meta__" in tok:
                    meta = tok["__stream_meta__"]
                    continue
                ...handle string token...
        Older callers that don't check for it will harmlessly ignore the
        dict (assuming they typecheck or no-op on non-string tokens).
        """
        if not prompt:
            return
        decision = self.route(prompt, model_hint=model_hint)
        logger.info(f"ModelRouter.stream → {decision.model} (tier={decision.tier})"
                    + (f" [local_model={local_model}]" if local_model else ""))
        self.last_model_label   = decision.model
        self.last_tier          = decision.tier
        self.last_input_tokens  = 0
        self.last_output_tokens = 0
        yield from self._dispatch_stream(
            decision.tier, prompt,
            local_model=local_model,
            precleared=precleared,
            precleared_findings=precleared_findings,
            provider_model=decision.provider_model_override,
        )
        self._propagate_tokens(decision.tier)
        # Sentinel — read the values RIGHT NOW (same thread frame as
        # _propagate_tokens) so the dict carries snapshot data, not thread
        # state. The caller captures this dict inside its for-loop.
        try:
            _thinking_text = ""
            try:
                _claude_gw = self._claude
                if _claude_gw is not None:
                    _thinking_text = getattr(_claude_gw, "_last_thinking_text", "") or ""
            except Exception:
                pass
            yield {
                "__stream_meta__": {
                    "in_tok":      int(self.last_input_tokens or 0),
                    "out_tok":     int(self.last_output_tokens or 0),
                    "model_label": str(self.last_model_label or ""),
                    "model_id":    str(self.last_model_id or ""),   # bare model ID (no display prefix)
                    "tier":        str(self.last_tier or ""),
                    "thinking":    _thinking_text,
                }
            }
        except Exception as _meta_err:
            logger.debug(f"stream() meta sentinel skipped: {_meta_err}")

    async def async_generate(self, prompt, model_hint: Optional[str] = None) -> str:
        """Async route + generate. Uses persistent AsyncClient — no thread held during LLM I/O.
        Falls back to sync generate() when LLM_PROXY_URL is not set (local dev / direct gateway).
        prompt: str OR list[dict] (multi-turn messages array).
        """
        if not prompt:
            return ""
        proxy = _llm_proxy_url()
        if not proxy:
            # Local dev: no proxy, fall back to sync (run in threadpool via caller)
            return self.generate(prompt, model_hint=model_hint)
        decision = self.route(prompt, model_hint=model_hint)
        logger.info(f"ModelRouter.async_generate → {decision.model} (tier={decision.tier})")
        gw_map = {
            TIER_SIMPLE:     self._get_local(),    # local stays sync
            TIER_MINI:       self._get_openai(),
            TIER_LOCAL_MINI: self._get_openai(),
            TIER_MEDIUM:     self._get_openai(),
            TIER_DEEP:       self._get_openai(),
            TIER_COMPLEX:    self._get_claude(),
            TIER_HAIKU:      self._get_claude(),
            TIER_VISION:     self._get_gemini(),
            TIER_GEMINI:     self._get_gemini(),
            TIER_SOLUTION:   self._get_claude(),
            TIER_OPUS_48:    self._get_claude(),
            TIER_OPUS_5:     self._get_claude(),
            TIER_SONNET_5:   self._get_claude(),
            TIER_TERA:       self._get_openai(),
            TIER_LUNA:       self._get_openai(),
        }
        gw = gw_map.get(decision.tier)
        if gw is None:
            logger.warning(f"ModelRouter.async_generate: no gateway for tier={decision.tier}")
            return self.generate(prompt, model_hint=model_hint)
        if not hasattr(gw, "async_generate"):
            # Local LLM or direct gateway without async support — sync fallback
            return self.generate(prompt, model_hint=model_hint)
        self.last_tier        = decision.tier
        self.last_model_label = decision.model

        # Build the client to pass to async_generate().
        # Per-call (default): fresh client each call — loop-agnostic.
        # Shared (legacy):    singleton — only safe under a single event loop.
        _per_call = _use_per_call_async_client()
        _owned_client = _make_async_client() if _per_call else None
        try:
            kwargs: dict = {}
            if _per_call and _owned_client is not None:
                kwargs["_override_client"] = _owned_client
            result = await gw.async_generate(prompt, **kwargs)
            self._propagate_tokens(decision.tier)
            return result
        except Exception as e:
            if decision.tier in (TIER_COMPLEX, TIER_HAIKU, TIER_SOLUTION, TIER_OPUS_48, TIER_OPUS_5, TIER_SONNET_5):
                logger.warning(
                    f"ModelRouter.async_generate: Claude failed (tier={decision.tier}) "
                    f"→ GPT fallback: {e}"
                )
                openai_gw = self._get_openai()
                if openai_gw and hasattr(openai_gw, "async_generate") and not _CB_OPENAI.is_open:
                    _fb_client = _make_async_client() if _per_call else None
                    try:
                        self.last_model_label = (
                            f"{OPENAI_CODING_DISPLAY} ({OPENAI_CODING_MODEL}) [fallback]"
                        )
                        fb_kwargs: dict = {}
                        if _per_call and _fb_client is not None:
                            fb_kwargs["_override_client"] = _fb_client
                        result = await openai_gw.async_generate(prompt, **fb_kwargs)
                        self._propagate_tokens(TIER_MEDIUM)
                        return result
                    except Exception as fe:
                        logger.warning(
                            f"ModelRouter.async_generate: GPT fallback also failed: {fe}"
                        )
                    finally:
                        if _fb_client is not None:
                            await _fb_client.aclose()
            raise
        finally:
            if _owned_client is not None:
                await _owned_client.aclose()

    async def async_stream(
            self,
            prompt,
            model_hint: Optional[str] = None,
            local_model: Optional[str] = None,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
            conv_id: Optional[str] = None,
    ):
        """Async streaming generator — yields str tokens then a sentinel dict.

        Mirrors stream() but runs entirely on the event loop so FastAPI's async
        StreamingResponse can flush each token to the client the instant it
        arrives, without blocking a thread-pool worker.

        Yields the same sentinel as stream():
            {"__stream_meta__": {"in_tok": int, "out_tok": int,
                                  "model_label": str, "tier": str}}

        Falls back to running the sync stream() in a thread when the gateway
        does not support async_stream() (e.g. local LLM, direct gateway without
        proxy).
        """
        if not prompt:
            return

        decision = self.route(prompt, model_hint=model_hint)
        logger.info(
            f"ModelRouter.async_stream → {decision.model} (tier={decision.tier})"
            + (f" [local_model={local_model}]" if local_model else "")
        )
        self.last_model_label = decision.model
        self.last_tier        = decision.tier
        self.last_input_tokens  = 0
        self.last_output_tokens = 0

        gw = None
        if decision.tier == TIER_SIMPLE:
            gw = self._get_local()
        elif decision.tier in (TIER_MINI, TIER_LOCAL_MINI, TIER_MEDIUM, TIER_DEEP,
                               TIER_TERA, TIER_LUNA):
            gw = self._get_openai()
        elif decision.tier in (TIER_COMPLEX, TIER_HAIKU, TIER_SOLUTION,
                               TIER_OPUS_48, TIER_OPUS_5, TIER_SONNET_5):
            gw = self._get_claude()
        elif decision.tier in (TIER_VISION, TIER_GEMINI):
            gw = self._get_gemini()

        if gw is not None and hasattr(gw, "async_stream"):
            # Native async streaming path — no thread held.
            _model_override = decision.provider_model_override or None
            async for tok in gw.async_stream(
                    prompt,
                    model=_model_override,
                    precleared=precleared,
                    precleared_findings=precleared_findings,
            ):
                yield tok
            self._propagate_tokens(decision.tier)
        else:
            # Fallback: run the sync stream() in a thread so we don't block
            # the event loop. Tokens are collected and yielded one by one.
            import asyncio as _asyncio
            _loop = _asyncio.get_event_loop()
            _queue: "asyncio.Queue[object]" = _asyncio.Queue()
            _SENTINEL = object()

            # Capture the caller's ContextVar snapshot so the sync generator
            # thread inherits request_id / user_id / chat_id / correlation_id.
            # Without this, plain threading.Thread starts with an empty context
            # and [LOCAL USAGE] / [LLM DISPATCH] logs show "-" for every field.
            import contextvars as _cv
            _ctx_snapshot = _cv.copy_context()

            # Marker used to ship the post-dispatch label back across the
            # thread boundary — see _LabelHandoff below.
            class _LabelHandoff:
                __slots__ = ("label", "tier")
                def __init__(self, label, tier):
                    self.label, self.tier = label, tier

            def _run_sync():
                def _inner():
                    try:
                        for tok in self._dispatch_stream(
                                decision.tier, prompt,
                                local_model=local_model,
                                precleared=precleared,
                                precleared_findings=precleared_findings,
                                provider_model=decision.provider_model_override,
                        ):
                            _loop.call_soon_threadsafe(_queue.put_nowait, tok)
                    except Exception as _e:
                        _loop.call_soon_threadsafe(_queue.put_nowait, _e)
                    finally:
                        # last_model_label is a threading.local() property
                        # (see ModelRouter._tl). _dispatch_stream runs HERE, on
                        # this worker thread, so any label the _try_*_stream
                        # helpers write — notably the Claude "[fallback]" label
                        # when the selected provider's gateway is unavailable —
                        # lands in THIS thread's storage and is invisible to the
                        # event-loop thread that emits the __stream_meta__
                        # sentinel below. That thread still holds the label set
                        # from `decision.model` before dispatch, so a turn that
                        # fell back to Claude was reported (and priced) as the
                        # originally selected model. contextvars are copied into
                        # this thread by _ctx_snapshot, but thread-locals are
                        # not, and neither propagates back out — so hand the
                        # value across the queue explicitly.
                        try:
                            # Read the RAW thread-locals, not the properties:
                            # last_model_label/last_tier default to "auto" when
                            # unset, which is indistinguishable from a value the
                            # dispatch actually chose. A helper that never
                            # touched them (no fallback taken) must hand back
                            # None so the event-loop thread keeps the label and
                            # tier it already holds from the routing decision.
                            _loop.call_soon_threadsafe(
                                _queue.put_nowait,
                                _LabelHandoff(
                                    getattr(self._tl, "last_model_label", None),
                                    getattr(self._tl, "last_tier", None),
                                ),
                            )
                        except Exception:
                            pass
                        _loop.call_soon_threadsafe(_queue.put_nowait, _SENTINEL)
                _ctx_snapshot.run(_inner)

            import threading as _threading
            _t = _threading.Thread(target=_run_sync, daemon=True)
            _t.start()
            while True:
                item = await _queue.get()
                if item is _SENTINEL:
                    break
                if isinstance(item, Exception):
                    raise item
                if isinstance(item, _LabelHandoff):
                    # Adopt the label the dispatch thread actually served under,
                    # onto THIS thread, so the sentinel below reports the real
                    # model rather than the pre-dispatch routing decision.
                    if item.label:
                        self.last_model_label = item.label
                    if item.tier:
                        self.last_tier = item.tier
                    continue
                yield item
            self._propagate_tokens(decision.tier)

        # Emit the same sentinel dict as stream() so callers can capture meta.
        # Fix 3: include model_id (bare ID, no display prefix) for parity with
        # stream() — callers that read meta["model_id"] no longer need to parse
        # the label string via _resolve_model_id().
        try:
            yield {
                "__stream_meta__": {
                    "in_tok":      getattr(self, "_tl_in",  0) or self.last_input_tokens  or 0,
                    "out_tok":     getattr(self, "_tl_out", 0) or self.last_output_tokens or 0,
                    "model_label": self.last_model_label or "",
                    "model_id":    str(self.last_model_id or ""),  # bare model ID (no display prefix)
                    "tier":        str(self.last_tier or ""),
                }
            }
        except Exception:
            pass


# ============================================================
# SINGLETON
# ============================================================

model_router = ModelRouter()
