# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt / RBI PRODUCTION OPENAI GATEWAY
# ============================================================

import os
import threading
import uuid
from typing import Generator

from openai import OpenAI

from core.logger import logger, get_request_id as _get_request_id

# NOTE: Compliance (PCI/PII detection + redaction) lives EXCLUSIVELY in the
# backend gateway layer (Tier 1). This proxy forwards already-validated,
# already-redacted text verbatim. Do NOT reintroduce a compliance engine here.

from core.model_registry import OPENAI_PRIMARY_MODEL, OPENAI_IMAGE_MODEL

MODEL = OPENAI_PRIMARY_MODEL

# Thread-local storage so concurrent requests don't overwrite each other's token counts
_tl = threading.local()

# OpenAI automatic prompt caching: cached tokens are billed at 50% of the model's input rate.
# Caching is transparent — no explicit flag; OpenAI decides what to cache automatically.
_OAI_CACHE_READ_RATIO = 0.50   # 50% of full input price


def _log_cache_effectiveness(
    *,
    request_id: str,
    model: str,
    cache_read: int,
    prompt_total: int,
    context: str = "",          # e.g. "stream", "tool-use"
) -> None:
    """Emit a structured [CACHE EFFECTIVENESS] log line for OpenAI calls.

    Derives the per-token cost from MODEL_COST_PER_1M (the single source of truth)
    so savings estimates stay accurate when model pricing changes in the registry.
    Local/in-house models (e.g. OPENAI_OSS_MODEL) have (0.0, 0.0) rates → savings = 0.
    OpenAI has no explicit cache_creation concept — caching is automatic and transparent.
    Always emitted so zero-cache calls are also visible in logs.
    """
    try:
        from core.model_registry import MODEL_COST_PER_1M
        input_rate_per_1m, _ = MODEL_COST_PER_1M.get(model, (0.0, 0.0))
        hit_rate = (cache_read / prompt_total * 100) if prompt_total > 0 else 0.0
        # Savings: cache_read tokens billed at 50% instead of 100% of input rate
        savings_usd = cache_read * input_rate_per_1m * (1.0 - _OAI_CACHE_READ_RATIO) / 1_000_000
        ctx_tag = f" context={context}" if context else ""
        logger.info(
            f"[CACHE EFFECTIVENESS] provider=openai request_id={request_id} model={model}{ctx_tag} "
            f"cache_read={cache_read} prompt_total={prompt_total} "
            f"hit_rate={hit_rate:.1f}% savings_tokens={cache_read} savings_est_usd={savings_usd:.6f} "
            f"cache_enabled=auto"   # OpenAI caches automatically; no explicit flag
        )
    except Exception:
        pass


class OpenAIGateway:

    def generate_with_model(self, prompt, model):

        from core.model_registry import BLOCKED_MODELS

        if model in BLOCKED_MODELS:
            raise Exception(f"Blocked model attempted: {model}")

        return self.generate(prompt)

    def __init__(self, api_key: str = None):
        """Initialise the OpenAI gateway.

        Args:
            api_key: Plaintext OpenAI API key.  When provided (Option A
                     key-delivery path via ProxyKeyCache), this key is used
                     directly.  When ``None`` (local dev / fallback), the key
                     is read from the ``OPENAI_API_KEY`` environment variable.
        """
        api_key = api_key or os.getenv("OPENAI_API_KEY")

        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")

        # the LLM proxy server has direct outbound internet access — no proxy needed.
        _t = float(os.getenv("LLM_TIMEOUT_SEC", "600"))
        self.client = OpenAI(api_key=api_key, timeout=None if _t <= 0 else _t)

        # Persistent async client — shared across all async_generate() calls on
        # this gateway instance.  A module-level singleton avoids the per-call
        # TCP+TLS handshake (~100 ms) and, more importantly, keeps the httpx
        # connection pool alive so the SDK's internal transport streams SSE
        # chunks token-by-token instead of buffering the full response body
        # before yielding the first chunk (the root cause of the 10 s delay
        # observed in the 2026-08-04 production log).
        from openai import AsyncOpenAI as _AsyncOpenAI
        self.async_client = _AsyncOpenAI(
            api_key=api_key,
            timeout=None if _t <= 0 else _t,
        )

        logger.info("OpenAI Gateway initialized")

    @property
    def _last_input_tokens(self):
        return getattr(_tl, "openai_in", 0)

    @_last_input_tokens.setter
    def _last_input_tokens(self, v):
        _tl.openai_in = v

    @property
    def _last_output_tokens(self):
        return getattr(_tl, "openai_out", 0)

    @_last_output_tokens.setter
    def _last_output_tokens(self, v):
        _tl.openai_out = v


    def generate(
        self,
        prompt,                              # str | list[dict] (OpenAI multi-turn format)
        model: str = None,
    ) -> Generator[str, None, None]:
        """Stream tokens from OpenAI.

        Compliance (PCI/PII detection + redaction) is handled by the backend
        gateway layer (Tier 1) BEFORE the request reaches this proxy. The text
        received here is already validated and redacted, so it is forwarded to
        the provider verbatim — this proxy performs NO compliance itself.

        prompt accepts either a plain string (single turn) or an OpenAI-format
        messages array (list of {"role": ..., "content": str | parts-list}).
        Parts-list content (vision / tool calls) is passed through unchanged —
        the caller owns that shape."""

        _model     = model or MODEL
        _upstream = _get_request_id()
        request_id = _upstream if _upstream and _upstream != "-" else str(uuid.uuid4())

        # Reset real token counts for this call
        self._last_input_tokens  = 0
        self._last_output_tokens = 0

        try:
            from core.retry import retry_llm
            from core.circuit_breaker import get_breaker

            logger.info(f"[LLM DISPATCH] provider=openai model={_model} request_id={request_id}")

            # Build the OpenAI messages payload:
            #   - str         → wrap as single user message
            #   - list[dict]  → forward as-is
            # All content is forwarded unchanged (already redacted upstream).
            if isinstance(prompt, list):
                messages_payload = [
                    {"role": m.get("role", "user"), "content": m.get("content")}
                    for m in prompt
                ]
            else:
                messages_payload = [{"role": "user", "content": prompt}]

            _OAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "low").strip().lower()
            _VALID_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh"}
            if _OAI_REASONING_EFFORT not in _VALID_REASONING_EFFORTS:
                _OAI_REASONING_EFFORT = ""

            _OAI_MAX_COMPLETION_TOKENS = int(os.getenv("OPENAI_MAX_COMPLETION_TOKENS", "0") or 0)

            # Suppress reasoning_effort when the payload already contains tool
            # calls/results — the OpenAI API rejects the parameter in that case.
            _has_tools = any(
                m.get("role") == "tool" or m.get("tool_calls")
                for m in messages_payload
            )
            _is_gpt5 = isinstance(_model, str) and _model.startswith("gpt-5")

            def _call():
                kwargs: dict = {
                    "model":          _model,
                    "stream":         True,
                    "stream_options": {"include_usage": True},
                    "messages":       messages_payload,
                }
                if _OAI_MAX_COMPLETION_TOKENS > 0:
                    kwargs["max_completion_tokens"] = _OAI_MAX_COMPLETION_TOKENS
                if _is_gpt5 and _OAI_REASONING_EFFORT and not _has_tools:
                    kwargs["reasoning_effort"] = _OAI_REASONING_EFFORT
                return self.client.chat.completions.create(**kwargs)

            breaker  = get_breaker("openai")
            response = breaker.call(retry_llm, _call)

            # Stream tokens to the client as they arrive. Input is already
            # redacted upstream (validate_input), so no output-side redaction.
            for chunk in response:

                if hasattr(chunk, "usage") and chunk.usage:
                    self._last_input_tokens  = chunk.usage.prompt_tokens or 0
                    self._last_output_tokens = chunk.usage.completion_tokens or 0
                    try:
                        logger.info(f"[OPENAI RAW USAGE] model={_model} {chunk.model_dump_json()}")
                    except Exception:
                        logger.info(f"[OPENAI RAW USAGE] model={_model} prompt={self._last_input_tokens} completion={self._last_output_tokens}")
                    _log_cache_effectiveness(
                        request_id=request_id,
                        model=_model,
                        cache_read=getattr(getattr(chunk.usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
                        prompt_total=self._last_input_tokens,
                        context="stream",
                    )

                if not chunk.choices:
                    continue

                _delta = chunk.choices[0].delta

                # Reasoning models (gpt-5.4 with reasoning_effort) stream
                # reasoning text in delta.reasoning_content BEFORE any content
                # tokens. Forward each reasoning delta as a {"r": ...} ndjson
                # line so the gateway can surface it live (as a reasoning SSE
                # frame) instead of dropping the chunk — which was the root
                # cause of the 25 s "hang then burst" on the /ask path.
                # Mirrors the root gateway_openai.py ReasoningMarker path.
                try:
                    _rz = (getattr(_delta, "reasoning", None)
                           or getattr(_delta, "reasoning_content", None)
                           or (getattr(_delta, "model_extra", None) or {}).get("reasoning_content"))
                    if _rz:
                        yield {"r": str(_rz)}
                except Exception:
                    pass

                token = _delta.content
                if not token:
                    continue
                yield token

        except Exception as e:
            logger.exception(
                f"{request_id} → OpenAI ({_model}) failed → {repr(e)[:1500]}"
            )
            yield "\nError generating response"

    async def async_generate(
        self,
        prompt,                              # str | list[dict] (OpenAI multi-turn format)
        model: str = None,
    ):
        """Async streaming generator — yields str tokens.

        Mirrors generate() but uses self.async_client (a persistent AsyncOpenAI
        singleton initialised in __init__) so the entire call runs on the uvicorn
        event loop without blocking a thread-pool worker.

        Using a persistent client is critical for true per-token SSE delivery:
        a per-call AsyncOpenAI instance has no connection pool and its httpx
        transport buffers the full SSE response body before yielding the first
        chunk, causing all tokens to arrive in one batch after the full LLM
        latency (10+ s observed in production on 2026-08-04).  The singleton
        keeps the httpx connection pool alive across requests, which both
        eliminates the per-call TLS handshake and ensures chunked streaming.

        Called from the /llm/generate endpoint's _stream() coroutine so all
        three providers (Claude, OpenAI, Gemini) share the same native-async
        token delivery path.
        """
        _model = model or MODEL
        _upstream = _get_request_id()
        request_id = _upstream if _upstream and _upstream != "-" else str(uuid.uuid4())

        self._last_input_tokens  = 0
        self._last_output_tokens = 0

        try:
            from core.circuit_breaker import get_breaker

            logger.info(f"[LLM DISPATCH async] provider=openai model={_model} request_id={request_id}")

            if isinstance(prompt, list):
                messages_payload = [
                    {"role": m.get("role", "user"), "content": m.get("content")}
                    for m in prompt
                ]
            else:
                messages_payload = [{"role": "user", "content": prompt}]

            kwargs: dict = {
                "model":          _model,
                "stream":         True,
                "stream_options": {"include_usage": True},
                "messages":       messages_payload,
            }

            # Suppress reasoning_effort when the payload already contains tool
            # calls/results — the OpenAI API rejects the parameter in that case.
            _OAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "low").strip().lower()
            _VALID_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh"}
            if _OAI_REASONING_EFFORT not in _VALID_REASONING_EFFORTS:
                _OAI_REASONING_EFFORT = ""
            _has_tools = any(
                m.get("role") == "tool" or m.get("tool_calls")
                for m in messages_payload
            )
            _is_gpt5 = isinstance(_model, str) and _model.startswith("gpt-5")
            if _is_gpt5 and _OAI_REASONING_EFFORT and not _has_tools:
                kwargs["reasoning_effort"] = _OAI_REASONING_EFFORT

            breaker = get_breaker("openai")
            # IMPORTANT: do NOT await breaker.async_call() here.
            # async_call() does `result = await coro_fn()` which fully materialises
            # the AsyncStream before returning it — all tokens buffer server-side
            # for the full LLM latency and flush in one shot, breaking per-token
            # streaming. Call create() directly inside a try/except so the circuit
            # breaker still trips on connection/auth errors, but the async iterator
            # is consumed token-by-token as OpenAI pushes each SSE chunk.
            try:
                response = await self.async_client.chat.completions.create(**kwargs)
                with breaker._lock:
                    breaker._failures = 0   # successful connection resets counter
            except Exception as _create_exc:
                with breaker._lock:
                    breaker._failures += 1
                    if breaker._failures >= breaker.failure_threshold:
                        import time as _time
                        breaker._opened_at = _time.time()
                        logger.warning(
                            f"CircuitBreaker(openai): OPENED after {breaker._failures} failures"
                        )
                raise

            async for chunk in response:
                if hasattr(chunk, "usage") and chunk.usage:
                    self._last_input_tokens  = chunk.usage.prompt_tokens or 0
                    self._last_output_tokens = chunk.usage.completion_tokens or 0
                    _log_cache_effectiveness(
                        request_id=request_id,
                        model=_model,
                        cache_read=getattr(getattr(chunk.usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
                        prompt_total=self._last_input_tokens,
                        context="stream",
                    )
                if not chunk.choices:
                    continue
                _delta = chunk.choices[0].delta

                # Reasoning models (gpt-5.4 with reasoning_effort) stream
                # reasoning text in delta.reasoning_content BEFORE any content
                # tokens. Forward each reasoning delta as a {"r": ...} ndjson
                # line so the gateway can surface it live (as a reasoning SSE
                # frame) instead of dropping the chunk — which was the root
                # cause of the 25 s "hang then burst" on the /ask path.
                # Mirrors the root gateway_openai.py ReasoningMarker path.
                try:
                    _rz = (getattr(_delta, "reasoning", None)
                           or getattr(_delta, "reasoning_content", None)
                           or (getattr(_delta, "model_extra", None) or {}).get("reasoning_content"))
                    if _rz:
                        yield {"r": str(_rz)}
                except Exception:
                    pass

                token = _delta.content
                if not token:
                    continue
                yield token

        except Exception as e:
            logger.exception(
                f"{request_id} → OpenAI async ({_model}) failed → {repr(e)[:1500]}"
            )
            yield "\nError generating response"
        # NOTE: no finally/close() — self.async_client is a persistent singleton
        # that must remain open for the lifetime of the gateway instance.

    # ------------------------------------------------------------------
    # RESPONSES API  (OpenAI Responses SDK — client.responses.*)
    # Used for: gpt-5.4, o4-mini-deep-research, o3-deep-research
    # Requires openai >= 1.50.0 on the LLM proxy server.
    # ------------------------------------------------------------------

    def responses_create(
            self,
            model: str,
            input,           # str | list[dict]
            tools: list = None,
            max_output_tokens: int = None,
    ) -> dict:
        """Non-streaming Responses API call. Returns dict with output_text + token counts."""
        kwargs: dict = {"model": model, "input": input}
        if tools:             kwargs["tools"] = tools
        if max_output_tokens: kwargs["max_output_tokens"] = max_output_tokens

        try:
            resp = self.client.responses.create(**kwargs)
        except AttributeError:
            raise RuntimeError("openai SDK does not support responses API — upgrade to openai>=1.50.0")

        usage   = getattr(resp, "usage", None)
        in_tok  = getattr(usage, "input_tokens",  0) or 0 if usage else 0
        out_tok = getattr(usage, "output_tokens", 0) or 0 if usage else 0
        self._last_input_tokens  = in_tok
        self._last_output_tokens = out_tok
        logger.info(f"[OPENAI RESPONSES] model={model} in={in_tok} out={out_tok}")
        _log_cache_effectiveness(
            request_id=_get_request_id(),
            model=model,
            cache_read=0,   # Responses API does not expose cached_tokens
            prompt_total=in_tok,
            context="non-stream",
        )
        return {"output_text": resp.output_text, "in_tok": in_tok, "out_tok": out_tok}

    def responses_stream(
            self,
            model: str,
            input,           # str | list[dict]
            tools: list = None,
            max_output_tokens: int = None,
    ) -> Generator[dict, None, None]:
        """Streaming Responses API call. Yields dicts:
            {"delta": "text chunk"}
            {"output_text": "full", "in_tok": N, "out_tok": N}   ← final line
        """
        kwargs: dict = {"model": model, "input": input}
        if tools:             kwargs["tools"] = tools
        if max_output_tokens: kwargs["max_output_tokens"] = max_output_tokens

        in_tok = out_tok = 0
        output_text = ""
        try:
            with self.client.responses.stream(**kwargs) as stream:
                for event in stream:
                    etype = getattr(event, "type", "")
                    if etype == "response.output_text.delta":
                        delta = getattr(event, "delta", "")
                        if delta:
                            output_text += delta
                            yield {"delta": delta}
                final = stream.get_final_response()
                usage   = getattr(final, "usage", None)
                in_tok  = getattr(usage, "input_tokens",  0) or 0 if usage else 0
                out_tok = getattr(usage, "output_tokens", 0) or 0 if usage else 0
        except AttributeError:
            raise RuntimeError("openai SDK does not support responses API — upgrade to openai>=1.50.0")

        self._last_input_tokens  = in_tok
        self._last_output_tokens = out_tok
        logger.info(f"[OPENAI RESPONSES STREAM] model={model} in={in_tok} out={out_tok}")
        _log_cache_effectiveness(
            request_id=_get_request_id(),
            model=model,
            cache_read=0,   # Responses API does not expose cached_tokens
            prompt_total=in_tok,
            context="stream",
        )
        yield {"output_text": output_text, "in_tok": in_tok, "out_tok": out_tok}


    def generate_image_dalle(self, prompt: str, size: str = "1792x1024") -> bytes | None:
        """
        Generate an image via DALL-E 3 (text → image bytes).
        Returns raw PNG bytes or None on failure.
        Compliance is enforced upstream in the backend gateway layer (Tier 1);
        the prompt received here is already validated/redacted.
        Called by /llm/generate-ppt-image proxy endpoint.
        """
        from core.retry import retry_llm
        from core.circuit_breaker import get_breaker
        import base64 as _b64

        try:
            def _call():
                return self.client.images.generate(
                    model=OPENAI_IMAGE_MODEL,
                    prompt=prompt,
                    size=size,
                    quality="standard",
                    n=1,
                    response_format="b64_json",
                )

            breaker  = get_breaker("openai")
            response = breaker.call(retry_llm, _call)
            if response.data:
                b64 = response.data[0].b64_json
                if b64:
                    return _b64.b64decode(b64)
        except Exception as exc:
            logger.error(f"llm_proxy: DALL-E generate_image failed: {exc}")
        return None


# LAZY singleton — must NOT be constructed at import time.
# On web02 the API key is delivered at runtime by ProxyKeyCache and is
# deliberately NOT in os.environ, so eagerly calling OpenAIGateway() here
# would raise RuntimeError("OPENAI_API_KEY not set") during
# `from gateway_openai import OpenAIGateway` in _lifespan() and take down
# every OpenAI endpoint. The gateway that actually serves traffic is
# `_openai_gw` in main.py, built with the ProxyKeyCache-sourced key.
_openai_gateway_singleton = None


def _get_openai_gateway() -> "OpenAIGateway":
    """Build (once) and return the module-level fallback singleton."""
    global _openai_gateway_singleton
    if _openai_gateway_singleton is None:
        _openai_gateway_singleton = OpenAIGateway()
    return _openai_gateway_singleton


def __getattr__(name):
    """PEP 562 — resolve `openai_gateway` lazily on first access."""
    if name == "openai_gateway":
        return _get_openai_gateway()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def generate_with_image_openai(
        prompt: str,
        image_b64: str,
        mime_type: str = "image/jpeg",
        system_prompt: str = "",
        _gateway: "OpenAIGateway | None" = None,
        images_b64: "list[str] | None" = None,
        mime_types: "list[str] | None" = None,
) -> tuple[str, int, int]:
    """
    Send a prompt + inline base64 image(s) to OpenAI vision.
    Returns (text, in_tok, out_tok).
    Used as fallback when Gemini vision is unavailable.

    Multi-image (optional, backward-compatible): pass `images_b64` (list of
    base64 strings) + matching `mime_types` to analyse multiple images in a
    single call. When omitted, falls back to the single `image_b64`/
    `mime_type` pair (original behaviour, unchanged for every existing
    caller).
    """
    from core.model_registry import OPENAI_CODING_MODEL
    from core.retry import retry_llm
    from core.circuit_breaker import get_breaker

    # _gateway is the proxy's already-initialised instance (has the
    # ProxyKeyCache key). Only fall back to the lazy module singleton
    # when no gateway was passed in.
    gw = _gateway or _get_openai_gateway()

    # Normalise to a list — single-image callers keep working unchanged.
    _imgs  = images_b64 if images_b64 else ([image_b64] if image_b64 else [])
    _mimes = mime_types  if mime_types  else [mime_type] * len(_imgs)
    if len(_mimes) < len(_imgs):
        _mimes = _mimes + [mime_type] * (len(_imgs) - len(_mimes))

    # Compliance is enforced upstream (Tier 1); prompt is already validated/redacted.
    messages: list = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    _content: list = [{"type": "text", "text": prompt}]
    for _img, _mt in zip(_imgs, _mimes):
        _content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{_mt};base64,{_img}"},
        })
    messages.append({"role": "user", "content": _content})

    def _call():
        return gw.client.chat.completions.create(
            model=OPENAI_CODING_MODEL,
            messages=messages,
        )

    breaker  = get_breaker("openai")
    response = breaker.call(retry_llm, _call)

    in_tok  = 0
    out_tok = 0
    if hasattr(response, "usage") and response.usage:
        in_tok  = response.usage.prompt_tokens     or 0
        out_tok = response.usage.completion_tokens or 0

    output = ""
    if response.choices:
        output = response.choices[0].message.content or ""

    logger.info(
        f"[OPENAI VISION] model={OPENAI_CODING_MODEL} "
        f"in={in_tok} out={out_tok}"
    )
    return output, in_tok, out_tok