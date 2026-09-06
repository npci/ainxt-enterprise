# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt / RBI PRODUCTION GEMINI GATEWAY
# ============================================================

import os
import uuid
from typing import Generator

from google import genai

from core.logger import logger, get_request_id as _get_request_id
from agents.compliance_engine import compliance_engine
from core.model_registry import GEMINI_VISION_MODEL, GEMINI_IMAGE_MODEL, veo_model as _veo_model


# Default model for generate() when caller passes no `model`.
# GEMINI_VISION_MODEL now defaults to GEMINI_TEXT_MODEL (gemini-3.5-flash) —
# a multimodal model that can analyse images and return text. Previously
# aliased to GEMINI_IMAGE_MODEL (gemini-3.1-flash-image) which caused empty
# responses for vision-analysis calls (response.text = "").
# Text/coding callers (model_router for TIER_GEMINI) pass model= explicitly.
MODEL = GEMINI_VISION_MODEL

# Gap #2 (7/7): surface Gemini "thought" parts (2.5 thinking models) as
# first-class reasoning deltas instead of silently discarding them. Emitted
# before the answer output. Default-on; env opt-out; fail-safe.
_STREAM_REASONING_DELTAS = os.getenv("STREAM_REASONING_DELTAS", "true").lower() == "true"

# Gemini context caching: cached tokens are billed at 25% of the model's normal input rate.
# Context cache storage is billed separately per hour; not modelled here.
_GEMINI_CACHE_READ_RATIO = 0.25   # 25% of full input price


def _log_cache_effectiveness(
    *,
    request_id: str,
    model: str,
    cache_read: int,
    prompt_total: int,
    context: str = "",          # e.g. "generate", "tool-use", "vision"
) -> None:
    """Emit a structured [CACHE EFFECTIVENESS] log line for Gemini calls.

    Derives the per-token cost from MODEL_COST_PER_1M (the single source of truth)
    so savings estimates stay accurate when model pricing changes in the registry.
    Gemini context caching (cached_content_token_count in usage_metadata) is
    explicit — callers must create a CachedContent object. Always emitted so
    zero-cache calls are visible and cache effectiveness can be tracked over time.
    """
    from core.model_registry import MODEL_COST_PER_1M
    input_rate_per_1m, _ = MODEL_COST_PER_1M.get(model, (0.0, 0.0))
    hit_rate = (cache_read / prompt_total * 100) if prompt_total > 0 else 0.0
    # Savings: cache_read tokens billed at 25% instead of 100% of input rate
    savings_usd = cache_read * input_rate_per_1m * (1.0 - _GEMINI_CACHE_READ_RATIO) / 1_000_000
    ctx_tag = f" context={context}" if context else ""
    logger.info(
        f"[CACHE EFFECTIVENESS] provider=gemini request_id={request_id} model={model}{ctx_tag} "
        f"cache_read={cache_read} prompt_total={prompt_total} "
        f"hit_rate={hit_rate:.1f}% savings_tokens={cache_read} savings_est_usd={savings_usd:.6f}"
    )


class GeminiGateway:

    def __init__(self):

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            # Fallback: an admin-configured Gemini provider with no .env key at
            # all (see core.llm_provider_registry). No-op when the env var is
            # already set — the common case is unaffected. This is also the
            # family models/model_router.py's registry hook actually dispatches
            # to for admin-added Gemini models, so this path matters for Phase 8.
            try:
                from core.llm_provider_registry import resolve_credential_for_family
                api_key = resolve_credential_for_family("gemini")
            except Exception:
                pass

        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")

        # No proxy needed — this gateway runs on the LLM proxy server which has direct
        # outbound access to generativelanguage.googleapis.com via firewall allowlist.
        self.client = genai.Client(api_key=api_key)

        # Real token counts from the last API call — read by model_router
        self._last_input_tokens  = 0
        self._last_output_tokens = 0

        logger.info("Gemini Gateway initialized")


    def redact(self, text, findings):

        for f in findings:

            v = f.get("value")

            if v:
                text = text.replace(v, "[REDACTED]")

        return text


    @staticmethod
    def _to_gemini_contents(prompt, sanitize_fn, redact_fn, findings):
        """Convert str or OpenAI messages list → Gemini contents format."""
        from google.genai import types as _gtypes
        if isinstance(prompt, list):
            role_map = {"user": "user", "assistant": "model"}
            contents = []
            for m in prompt:
                role    = role_map.get(m["role"], "user")
                content = sanitize_fn(redact_fn(m.get("content") or "", findings))
                contents.append(_gtypes.Content(
                    role=role, parts=[_gtypes.Part(text=content)]
                ))
            return contents
        # Plain string path
        return sanitize_fn(redact_fn(prompt, findings))

    def generate(
        self,
        prompt,
        precleared: bool = False,
        precleared_findings: list = None,
        model: str | None = None,
    ) -> Generator[str, None, None]:
        """prompt: str (single turn) OR list[dict] (multi-turn OpenAI-format messages array).

        precleared / precleared_findings: see OpenAIGateway.generate() for the
        full contract. Set by the /ask handler in gateway.py after it has
        already run compliance_engine.validate_input() on the current user
        turn. When True, the block decision is skipped here (avoids
        false-positive PCI blocks caused by ML nondeterminism + gateway-
        injected tone/cross-chat prefixes on the last message); redaction
        still runs using the precleared_findings the first pass produced.
        Default False preserves full second-pass safety for non-/ask callers.

        model: optional explicit Gemini model ID. When None, falls back to the
        module-level MODEL constant (GEMINI_VISION_MODEL, which aliases to the
        image model by default — see model_registry).
        """

        _upstream = _get_request_id()
        request_id = _upstream if _upstream and _upstream != "-" else str(uuid.uuid4())

        # Compliance check on current user question only — never on the flattened
        # multi-turn history, which would re-evaluate prior turns and produce
        # false-positive PCI blocks on benign new prompts.
        _current_content = prompt[-1]["content"] if isinstance(prompt, list) else prompt

        if precleared:
            # Gateway already validated; suppress block, keep findings for redact.
            validation = {
                "blocked":  False,
                "findings": list(precleared_findings or []),
            }
            logger.info(
                f"[GEMINI COMPLIANCE SKIP] request_id={request_id} reason=precleared "
                f"findings={len(validation['findings'])}"
            )
        else:
            validation = compliance_engine.validate_input(_current_content)

            if validation["blocked"]:
                # Diagnostic: surface block_types and which input shape was checked
                # so future false-positive blocks can be triaged without re-tracing.
                logger.warning(
                    f"[GEMINI COMPLIANCE BLOCK] request_id={request_id} "
                    f"blocked_types={validation.get('blocked_types')} "
                    f"input_shape={'messages' if isinstance(prompt, list) else 'prompt'} "
                    f"last_user_len={len(_current_content or '')}"
                )

                yield "Request blocked due to PCI violation"

                return


        from core.prompt_sanitizer import sanitize as _sanitize
        contents = self._to_gemini_contents(
            prompt, _sanitize, self.redact, validation["findings"]
        )

        # Reset real token counts for this call
        self._last_input_tokens  = 0
        self._last_output_tokens = 0

        try:
            from core.retry import retry_llm
            from core.circuit_breaker import get_breaker

            _effective_model = model or MODEL

            def _call():
                return self.client.models.generate_content(
                    model=_effective_model,
                    contents=contents,
                )

            breaker  = get_breaker("gemini")
            response = breaker.call(retry_llm, _call)

            # Capture real token counts from Gemini usage_metadata and log full breakdown
            try:
                if hasattr(response, "usage_metadata") and response.usage_metadata:
                    _um = response.usage_metadata
                    self._last_input_tokens  = getattr(_um, "prompt_token_count",      0) or 0
                    self._last_output_tokens = getattr(_um, "candidates_token_count",   0) or 0
                    _cached_in   = getattr(_um, "cached_content_token_count", 0) or 0
                    _total       = getattr(_um, "total_token_count",          0) or 0
                    _thoughts    = getattr(_um, "thoughts_token_count",       0) or 0
                    _billed_in   = self._last_input_tokens - _cached_in
                    logger.info(
                        f"[GEMINI USAGE] request_id={request_id} model={_effective_model} "
                        f"prompt={self._last_input_tokens} candidates={self._last_output_tokens} "
                        f"cached_in={_cached_in} billed_in={_billed_in} "
                        f"thoughts={_thoughts} total={_total}"
                    )
                    _log_cache_effectiveness(
                        request_id=request_id,
                        model=_effective_model,
                        cache_read=_cached_in,
                        prompt_total=self._last_input_tokens,
                        context="generate",
                    )
            except Exception:
                pass

            # gemini-2.5-flash is a thinking model: response.text skips
            # thought parts and may return None when only thinking parts
            # exist or when content is safety-filtered.  Extract text from
            # parts directly to handle edge cases robustly.
            text_parts = []
            _thought_parts = []
            if (
                response.candidates
                and response.candidates[0].content
                and response.candidates[0].content.parts
            ):
                for part in response.candidates[0].content.parts:
                    # Thinking / thought parts (part.thought is True): surface as
                    # reasoning instead of discarding (Gap #2), keep out of answer.
                    if getattr(part, "thought", False):
                        _pt = getattr(part, "text", None)
                        if _pt:
                            _thought_parts.append(_pt)
                        continue
                    if getattr(part, "text", None) is not None:
                        text_parts.append(part.text)

            # Emit reasoning BEFORE the answer, as a first-class marker. Wrapped
            # so it can never break the answer path.
            if _STREAM_REASONING_DELTAS and _thought_parts:
                try:
                    from pipeline.stream_events import ReasoningMarker as _RM
                    yield _RM(delta="".join(_thought_parts))
                except Exception:
                    pass

            output = "".join(text_parts) if text_parts else None

            if output:
                yield output
            else:
                logger.warning(
                    f"[GEMINI EMPTY] request_id={request_id} model={_effective_model} "
                    f"candidates={bool(response.candidates)} "
                    f"finish_reason={getattr(response.candidates[0], 'finish_reason', 'N/A') if response.candidates else 'no_candidates'}"
                )
                yield "I'm sorry, I couldn't generate a response. Please try rephrasing your question."


        except Exception as e:

            logger.exception(
                f"{request_id} → Gemini failed → {repr(e)[:1500]}"
            )

            yield "\nError generating response"

    # ========================================================
    # TOOL-USE LOOP  (mirrors gateway_claude.generate_with_tools)
    # ========================================================

    def generate_with_tools(
            self,
            system_prompt: str,
            user_message: str,
            context: str,
            tools: list,
            tool_executor,
            model: str = MODEL,
            max_tokens: int = 8000,
            max_tool_rounds: int = 8,
    ) -> str:
        """
        Non-streaming tool-use loop using Gemini function calling.

        Accepts the same arguments as ClaudeGateway.generate_with_tools() so
        ReactOrchestrator can swap gateways without changing call sites.

        Tool schemas must be in Anthropic format (input_schema key) —
        this method converts them to Gemini FunctionDeclaration format internally.

        tool_executor: callable(tool_name: str, inputs: dict) -> str
        """
        import json as _json
        from core.proxy_tool_use import _execute_with_web_search_governance, _WebSearchBudgetExhausted, flush_web_search_billing
        _upstream = _get_request_id()
        request_id = _upstream if _upstream and _upstream != "-" else str(uuid.uuid4())
        tool_names = [t["name"] for t in tools]
        logger.info(f"{request_id} → GEMINI TOOL-USE START tools={tool_names}")

        from core.prompt_sanitizer import sanitize as _sanitize
        from google.genai import types as _gtypes

        system_prompt = _sanitize(system_prompt)
        user_message  = _sanitize(user_message)

        # ── Proxy path: LLM_PROXY_URL set → forward to the LLM proxy server ───
        if os.getenv("LLM_PROXY_URL"):
            from core.proxy_tool_use import run_tool_use_via_proxy
            context       = _sanitize(context) if context else context
            user_content  = (
                f"{context}\n\n## User Request\n{user_message}" if context else user_message
            )
            return run_tool_use_via_proxy(
                provider="gemini", model=model, system_prompt=system_prompt,
                user_content=user_content, tools=tools, tool_executor=tool_executor,
                max_tokens=max_tokens, max_tool_rounds=max_tool_rounds,
                request_id=request_id, current_user=getattr(self, "_current_user", None),
            )
        # ── Direct path: LLM proxy server calls Gemini API directly ───────────────
        context       = _sanitize(context) if context else context
        user_content  = (
            f"{context}\n\n## User Request\n{user_message}" if context else user_message
        )

        # Build Gemini tool config from Anthropic-format schemas
        gemini_tool = _anthropic_to_gemini_tool(tools)

        # Gemini uses a flat contents list; roles alternate user/model
        contents = [
            _gtypes.Content(role="user", parts=[_gtypes.Part(text=user_content)])
        ]

        self._last_input_tokens  = 0
        self._last_output_tokens = 0
        _total_cached_tokens = 0   # accumulated cache_read across all tool-use rounds

        for round_num in range(max_tool_rounds + 1):
            try:
                call_tool_cfg = (
                    _gtypes.GenerateContentConfig(
                        system_instruction=system_prompt,
                        tools=[gemini_tool],
                        tool_config=_gtypes.ToolConfig(
                            function_calling_config=_gtypes.FunctionCallingConfig(
                                mode="AUTO"
                            )
                        ),
                    )
                    if round_num < max_tool_rounds
                    else _gtypes.GenerateContentConfig(
                        system_instruction=system_prompt,
                    )
                )

                from core.retry import retry_llm
                from core.circuit_breaker import get_breaker
                breaker  = get_breaker("gemini")
                response = breaker.call(
                    retry_llm,
                    lambda: self.client.models.generate_content(
                        model=model,
                        contents=contents,
                        config=call_tool_cfg,
                    ),
                )

                # Accumulate token counts including cache hits
                try:
                    if hasattr(response, "usage_metadata") and response.usage_metadata:
                        _um = response.usage_metadata
                        _round_in  = getattr(_um, "prompt_token_count",           0) or 0
                        _round_out = getattr(_um, "candidates_token_count",        0) or 0
                        _round_cached = getattr(_um, "cached_content_token_count", 0) or 0
                        self._last_input_tokens  += _round_in
                        self._last_output_tokens += _round_out
                        _total_cached_tokens     += _round_cached
                        _round_hit_rate = (_round_cached / _round_in * 100) if _round_in > 0 else 0.0
                        logger.info(
                            f"[GEMINI CACHE ROUND] request_id={request_id} model={model} "
                            f"round={round_num} cache_read={_round_cached} "
                            f"prompt_tokens={_round_in} hit_rate={_round_hit_rate:.1f}%"
                        )
                except Exception:
                    pass

            except Exception as e:
                logger.error(f"{request_id} → Gemini tool-use round {round_num} failed: {e}")
                return f"[ERROR generating response: {e}]"

            # Collect function_call parts from the response
            candidate = response.candidates[0] if response.candidates else None
            if not candidate:
                return "[ERROR: Gemini returned no candidates]"

            parts         = candidate.content.parts or []
            fn_call_parts = [p for p in parts if getattr(p, "function_call", None) is not None]
            finish_reason = str(getattr(candidate, "finish_reason", "")).upper()

            # No function calls or model finished → extract text and return
            if not fn_call_parts or finish_reason in ("STOP", "1"):
                text = " ".join(
                    p.text for p in parts if getattr(p, "text", None)
                ).strip()
                logger.info(
                    f"[GEMINI TOOL USAGE] request_id={request_id} model={model} "
                    f"rounds={round_num} in={self._last_input_tokens} out={self._last_output_tokens} "
                    f"total_cached={_total_cached_tokens}"
                )
                _log_cache_effectiveness(
                    request_id=request_id,
                    model=model,
                    cache_read=_total_cached_tokens,
                    prompt_total=self._last_input_tokens,
                    context="tool-use",
                )
                logger.info(f"{request_id} → GEMINI TOOL-USE DONE after {round_num} round(s)")
                flush_web_search_billing(request_id)
                return text

            # Append model's turn to contents
            contents.append(candidate.content)

            # Execute each function call and send back results
            logger.info(f"{request_id} → round {round_num}: {len(fn_call_parts)} function call(s)")
            result_parts = []
            for part in fn_call_parts:
                fc = part.function_call
                try:
                    inputs = dict(fc.args) if fc.args else {}
                    result_text = _execute_with_web_search_governance(
                        request_id=request_id,
                        model=model,
                        tool_name=fc.name,
                        tool_inputs=inputs,
                        tool_executor=tool_executor,
                        current_user=getattr(self, "_current_user", None),
                    )
                    logger.info(f"{request_id} → {fc.name} → OK: {str(result_text)[:120]}")
                except _WebSearchBudgetExhausted as budget_exc:
                    logger.warning(f"{request_id} → budget exhausted during {fc.name}, aborting")
                    flush_web_search_billing(request_id)
                    return str(budget_exc)
                except Exception as e:
                    logger.error(f"{request_id} → {fc.name} failed: {e}")
                    result_text = f"Error executing {fc.name}: {e}"

                result_parts.append(
                    _gtypes.Part(
                        function_response=_gtypes.FunctionResponse(
                            name=fc.name,
                            response={"result": str(result_text)},
                        )
                    )
                )

            contents.append(
                _gtypes.Content(role="user", parts=result_parts)
            )

        flush_web_search_billing(request_id)
        return "[ERROR: max tool-use rounds exceeded]"

    # ============================================================
    # TEXT → IMAGE  (Imagen-3 Fast, DALL-E fallback via LLM proxy)
    # ============================================================
    def generate_imagen(
            self,
            prompt: str,
            aspect_ratio: str = "16:9",
            number_of_images: int = 1,
            style_suffix: str = "",
            provider: str = "gemini",
            return_meta: bool = False,
    ) -> "bytes | None | tuple[bytes | None, dict]":
        """
        Generate an image. Primary attempt uses the gemini image model
        (gemini-3.1-flash-image by default), with automatic fallback to
        OpenAI's gpt-image-1 (then dall-e-3) implemented inside the LLM
        proxy's /llm/imagen handler. If BOTH providers are unavailable
        the proxy returns HTTP 503 with an "image_model_unavailable"
        payload, which we surface to the caller as None + a recognisable
        _last_imagen_error so the chat router can render a friendly
        "Image generation model not available" reply instead of a 5xx.

        Args:
            return_meta: when True, returns (bytes | None, meta_dict) so
              the caller can read which provider/model ACTUALLY produced
              the image (post-fallback). Default False preserves the
              legacy bytes-only return shape used by older call sites.

        Routing policy (matches gateway_claude / gateway_openai / text path
        in this same module — see line ~177):

          • LLM_PROXY_URL set (PROD / DEV-with-proxy):
              Hard-route through {LLM_PROXY_URL}/llm/imagen. The proxy is
              the ONLY process with cloud egress. The proxy implements
              the gemini→openai fallback chain internally.

          • LLM_PROXY_URL unset (true local dev only, OR we ARE the proxy):
              Direct SDK call to Imagen. Fallback is NOT attempted on this
              branch (the proxy is the canonical fallback site).
        """
        from core.retry import retry_llm
        from core.circuit_breaker import get_breaker

        validation = compliance_engine.validate_input(prompt[:2000])
        if validation.get("blocked"):
            logger.warning("generate_imagen: prompt blocked by compliance")
            return None

        safe_prompt = self.redact(prompt, validation.get("findings", []))
        _proxy = os.getenv("LLM_PROXY_URL", "").rstrip("/")

        # Stash the most recent proxy-error detail on the instance so the
        # HTTP caller (routers/chat_router.py) can surface it to the UI
        # instead of opaque "image generation failed". Also stash a typed
        # error flag so the chat router can distinguish "model unavailable
        # at all providers" (which becomes a friendly chat reply) from
        # generic proxy hiccups (which become a 5xx).
        self._last_imagen_error    = None
        self._last_imagen_unavail  = False
        self._last_imagen_provider = None   # actual provider that produced bytes
        self._last_imagen_model    = None   # actual model id that produced bytes

        # Reset the shared token accessors at the START of every call — same
        # convention as the text path (line ~137) and gateway_claude
        # (~110). The chat router reads these AFTER generate_imagen() to
        # build the token + cost chips. Populated from the proxy's
        # X-Imagen-Input/Output-Tokens headers (proxy path) or the direct
        # SDK usage_metadata (dev path). Never faked: OpenAI-fallback images
        # legitimately report 0/0 here.
        self._last_input_tokens  = 0
        self._last_output_tokens = 0

        def _ret(b: "bytes | None"):
            if return_meta:
                return b, {
                    "provider":    self._last_imagen_provider,
                    "model":       self._last_imagen_model,
                    "unavailable": self._last_imagen_unavail,
                    "error":       self._last_imagen_error,
                }
            return b

        # ── Proxy path (PROD + DEV-with-proxy) ──────────────────
        # All outbound cloud calls go through the proxy. This is non-
        # negotiable per the architecture rule in CLAUDE.md.
        if _proxy:
            try:
                import requests as _rq
                resp = _rq.post(
                    f"{_proxy}/llm/imagen",
                    json={
                        "provider":         provider,
                        "prompt":           safe_prompt[:4000],
                        "aspect_ratio":     aspect_ratio,
                        "number_of_images": number_of_images,
                        "style_suffix":     style_suffix or "",
                    },
                    # Imagen + fallback model retries can take 20–60 s each;
                    # 180 s budgets two attempts comfortably.
                    timeout=180,
                )
                if resp.status_code == 200 and resp.content:
                    # The proxy reports the ACTUAL provider/model that
                    # produced the bytes (post-fallback). Stash so the
                    # caller can surface it to the UI via response headers.
                    self._last_imagen_provider = (
                        resp.headers.get("X-Imagen-Provider") or provider
                    )
                    self._last_imagen_model = (
                        resp.headers.get("X-Imagen-Model") or ""
                    )
                    # Real token usage relayed by the proxy. Follows the
                    # gateway convention: the chat router reads
                    # _last_input_tokens / _last_output_tokens off this
                    # instance to compute the per-token Gemini cost. OpenAI
                    # fallbacks report 0/0 (no token accounting) — we do NOT
                    # fake them.
                    def _hdr_int(_name: str) -> int:
                        try:
                            return int(resp.headers.get(_name, "0") or 0)
                        except (TypeError, ValueError):
                            return 0
                    self._last_input_tokens  = _hdr_int("X-Imagen-Input-Tokens")
                    self._last_output_tokens = _hdr_int("X-Imagen-Output-Tokens")
                    return _ret(resp.content)
                # 503 from the proxy = both providers unavailable. Tag the
                # error so the chat router can render the friendly
                # "Image generation model not available" message.
                if resp.status_code == 503:
                    self._last_imagen_unavail = True
                # Surface the failure — never silently mask with a direct call.
                # this host has no cloud egress; a "fallback" would just fail later
                # with a confusing TLS / DNS error.
                self._last_imagen_error = (
                    f"proxy {resp.status_code}: {resp.text[:400]}"
                )
                logger.error(
                    f"generate_imagen proxy returned {resp.status_code}: "
                    f"{resp.text[:400]}"
                )
            except Exception as proxy_exc:
                self._last_imagen_error = f"proxy unreachable: {proxy_exc}"
                logger.error(f"generate_imagen proxy call failed: {proxy_exc}")
            return _ret(None)

        # ── Direct path (LLM_PROXY_URL unset) ───────────────────
        # Only reachable when (a) running inside the llm_proxy service itself,
        # or (b) true offline-dev with no proxy. Production gateways always
        # have LLM_PROXY_URL set and never enter this branch.
        # The gateway (gateway.py img_intent routing) already appended a
        # context-aware quality suffix before calling generate_imagen(), so
        # we must NOT add "no text, photorealistic" here — that would destroy
        # UI content (login page labels, buttons, etc.) for improvement requests.
        # We DO keep the aspect ratio as a text hint (models respond better when
        # the target ratio is stated in the prompt as well as the API parameter)
        # and the optional style_suffix from the caller.
        full_prompt = (
            f"{safe_prompt}"
            f"{('. Style: ' + style_suffix) if style_suffix else ''}"
            f". Target aspect ratio: {aspect_ratio}."
        )
        try:
            from google.genai import types as _gtypes

            # Image-generation model — sourced from the registry so env override
            # (GEMINI_IMAGE_MODEL) is respected without code changes.
            _GEMINI_MULTIMODAL = GEMINI_IMAGE_MODEL

            def _call():
                return self.client.models.generate_content(
                    model=_GEMINI_MULTIMODAL,
                    contents=full_prompt,
                    config=_gtypes.GenerateContentConfig(
                        response_modalities=["IMAGE"],
                        image_config=_gtypes.ImageConfig(
                            aspect_ratio=aspect_ratio,
                        ),
                    ),
                )

            breaker  = get_breaker("gemini")
            response = breaker.call(retry_llm, _call)

            # Capture REAL token usage from Gemini usage_metadata — same
            # fields the text path reads (line ~159). Never faked: absent
            # usage_metadata leaves the accessors at their reset 0 value.
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                _um = response.usage_metadata
                self._last_input_tokens  = getattr(_um, "prompt_token_count",     0) or 0
                self._last_output_tokens = getattr(_um, "candidates_token_count", 0) or 0

            # Extract inline image data from response candidates
            if not response.candidates:
                logger.warning("generate_imagen: no candidates in response")
                return _ret(None)

            parts = (response.candidates[0].content.parts or []) if response.candidates[0].content else []
            for part in parts:
                if hasattr(part, "inline_data") and part.inline_data and part.inline_data.data:
                    logger.info(
                        f"generate_imagen: OK model={_GEMINI_MULTIMODAL} "
                        f"mime={getattr(part.inline_data, 'mime_type', 'unknown')} "
                        f"bytes={len(part.inline_data.data)}"
                    )
                    # Direct-path success → provider = gemini, model = the
                    # registry-resolved image model.
                    self._last_imagen_provider = "gemini"
                    self._last_imagen_model    = _GEMINI_MULTIMODAL
                    return _ret(part.inline_data.data)

            logger.warning(f"generate_imagen: response has no inline image data (parts={len(parts)})")
        except Exception as exc:
            logger.error(f"Gemini generate_imagen failed: {exc}")
            self._last_imagen_error = f"{type(exc).__name__}: {exc}"
        return _ret(None)

    # ============================================================
    # TEXT → VIDEO  (Veo 3.1 — long-running operation, returns MP4)
    # ============================================================
    def generate_veo_video(
            self,
            prompt: str,
            aspect_ratio: str = "16:9",
            duration_secs: int = 8,
            poll_interval_secs: int = 5,
            max_wait_secs: int = 300,
            model: str | None = None,
    ) -> tuple[bytes | None, dict]:
        """
        Generate a short video via Google Veo 3.1 (preview).

        Returns (mp4_bytes, meta) on success or (None, meta) on failure.
        `meta` contains {mime, duration, model, error?}.

        `model` lets the caller pass an already-resolved model id (e.g.
        `routers/chat_router.py` resolves it once up front for its budget
        check and reuses the same value here) — when omitted, resolves via
        `core.model_registry.veo_model()` (env override → an enabled
        "gemini"-family registry model tagged "video" → ""). Always the
        actual value dispatched, never the raw possibly-blank VEO_MODEL
        constant, so `meta["model"]` is accurate for cost/audit callers.

        Veo is a Long-Running Operation:
          1. Submit `models.generate_videos(...)` → returns an Operation handle.
          2. Poll `operations.get(op)` until `op.done`.
          3. Download bytes from the returned File handle.

        Routing policy mirrors generate_imagen:
          • LLM_PROXY_URL set → hard-route through {LLM_PROXY_URL}/llm/veo.
          • LLM_PROXY_URL unset → direct SDK (llm_proxy service itself / dev only).

        Compliance gate runs on the prompt. Total wall-clock capped by
        `max_wait_secs` to bound LRO polling on errant operations.
        """
        import time
        from core.retry import retry_llm
        from core.circuit_breaker import get_breaker

        resolved_model = model or _veo_model()
        meta: dict = {
            "mime":     "video/mp4",
            "duration": duration_secs,
            "model":    resolved_model,
        }

        validation = compliance_engine.validate_input(prompt[:2000])
        if validation.get("blocked"):
            logger.warning("generate_veo_video: prompt blocked by compliance")
            meta["error"] = "blocked_by_compliance"
            return None, meta

        safe_prompt = self.redact(prompt, validation.get("findings", []))
        proxy = os.getenv("LLM_PROXY_URL", "").rstrip("/")
        self._last_veo_error = None

        # Proxy path (PROD + DEV-with-proxy): cloud egress goes through the proxy.
        if proxy:
            try:
                import requests
                resp = requests.post(
                    f"{proxy}/llm/veo",
                    json={
                        "prompt":         safe_prompt[:4000],
                        "model":          resolved_model,
                        "aspect_ratio":   aspect_ratio,
                        "duration_secs":  duration_secs,
                    },
                    # Veo LROs can take 60–180 s; allow generous budget.
                    timeout=max(max_wait_secs + 30, 240),
                )
                if resp.status_code == 200 and resp.content:
                    logger.info(
                        f"generate_veo_video: OK via proxy bytes={len(resp.content)}"
                    )
                    return resp.content, meta
                self._last_veo_error = f"proxy {resp.status_code}: {resp.text[:400]}"
                logger.error(
                    f"generate_veo_video proxy returned {resp.status_code}: "
                    f"{resp.text[:400]}"
                )
            except Exception as proxy_exc:
                self._last_veo_error = f"proxy unreachable: {proxy_exc}"
                logger.error(f"generate_veo_video proxy call failed: {proxy_exc}")
            meta["error"] = self._last_veo_error or "proxy_failed"
            return None, meta

        # Direct SDK path: only when we ARE the llm_proxy service or in offline dev.
        try:
            from google.genai import types as gtypes

            def _start():
                return self.client.models.generate_videos(
                    model=resolved_model,
                    prompt=safe_prompt,
                    config=gtypes.GenerateVideosConfig(
                        aspect_ratio=aspect_ratio,
                        duration_seconds=duration_secs,
                    ),
                )

            breaker = get_breaker("gemini")
            operation = breaker.call(retry_llm, _start)

            t0 = time.time()
            while not getattr(operation, "done", False):
                if (time.time() - t0) > max_wait_secs:
                    self._last_veo_error = f"timeout after {max_wait_secs}s"
                    logger.error(f"generate_veo_video: {self._last_veo_error}")
                    meta["error"] = self._last_veo_error
                    return None, meta
                time.sleep(poll_interval_secs)
                try:
                    operation = self.client.operations.get(operation)
                except Exception as poll_exc:
                    self._last_veo_error = f"poll failed: {poll_exc}"
                    logger.error(f"generate_veo_video poll error: {poll_exc}")
                    meta["error"] = self._last_veo_error
                    return None, meta

            response = getattr(operation, "response", None)
            gen_videos = getattr(response, "generated_videos", None) if response else None
            if not gen_videos:
                self._last_veo_error = "no generated_videos in response"
                logger.warning(f"generate_veo_video: {self._last_veo_error}")
                meta["error"] = self._last_veo_error
                return None, meta

            video_handle = gen_videos[0].video
            try:
                self.client.files.download(file=video_handle)
            except Exception as dl_exc:
                # Some SDK revisions return inline bytes without a download step.
                logger.warning(f"generate_veo_video: files.download fallthrough: {dl_exc}")

            # Attribute name varies across google-genai revisions.
            video_bytes = (
                getattr(video_handle, "video_bytes", None)
                or getattr(video_handle, "data", None)
                or getattr(video_handle, "bytes", None)
            )

            if not video_bytes:
                self._last_veo_error = "video bytes unavailable after download"
                logger.error(f"generate_veo_video: {self._last_veo_error}")
                meta["error"] = self._last_veo_error
                return None, meta

            logger.info(
                f"generate_veo_video: OK model={resolved_model} duration={duration_secs}s "
                f"bytes={len(video_bytes)}"
            )
            return video_bytes, meta

        except Exception as exc:
            self._last_veo_error = str(exc)
            logger.error(f"Gemini generate_veo_video failed: {exc}")
            meta["error"] = str(exc)
            return None, meta

# ============================================================
# SCHEMA CONVERTER  — Anthropic → Gemini FunctionDeclaration
# ============================================================

def _anthropic_to_gemini_tool(tools: list):
    """
    Convert Anthropic tool schemas (input_schema) to a single Gemini Tool
    object containing all FunctionDeclarations.
    """
    from google.genai import types as _gtypes

    _type_map = {
        "string":  "STRING",
        "integer": "INTEGER",
        "number":  "NUMBER",
        "boolean": "BOOLEAN",
        "array":   "ARRAY",
        "object":  "OBJECT",
    }

    def _build_schema(prop_def: dict) -> "_gtypes.Schema":
        t = _type_map.get(prop_def.get("type", "string"), "STRING")
        kwargs: dict = {"type": t}
        if prop_def.get("description"):
            kwargs["description"] = prop_def["description"]
        if prop_def.get("enum"):
            kwargs["enum"] = prop_def["enum"]
        if t == "ARRAY" and prop_def.get("items"):
            kwargs["items"] = _build_schema(prop_def["items"])
        if t == "OBJECT" and prop_def.get("properties"):
            kwargs["properties"] = {
                k: _build_schema(v) for k, v in prop_def["properties"].items()
            }
            if prop_def.get("required"):
                kwargs["required"] = prop_def["required"]
        return _gtypes.Schema(**kwargs)

    declarations = []
    for t in tools:
        input_schema = t.get("input_schema", {})
        props_raw    = input_schema.get("properties", {})
        required     = input_schema.get("required", [])

        parameters = None
        if props_raw:
            parameters = _gtypes.Schema(
                type="OBJECT",
                properties={k: _build_schema(v) for k, v in props_raw.items()},
                required=required,
            )

        declarations.append(
            _gtypes.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=parameters,
            )
        )

    return _gtypes.Tool(function_declarations=declarations)


gemini_gateway = GeminiGateway()


def generate_image_gemini(prompt: str) -> bytes | None:
    """Module-level helper — generates image via Gemini Imagen 3 through the gateway."""
    return gemini_gateway.generate_imagen(prompt)


def generate_with_image(
        prompt: str,
        image_b64: str,
        mime_type: str = "image/jpeg",
        system_prompt: str = "",
        _gateway: "GeminiGateway | None" = None,
) -> str:
    """
    Send a prompt + inline image to Gemini vision (model: GEMINI_VISION_MODEL —
    defaults to gemini-3.5-flash; env-overridable via GEMINI_VISION_MODEL).
    Returns the full response text (not streamed).
    Compliance checks run on input.

    When LLM_PROXY_URL is set (and _gateway is not injected by the proxy itself),
    the call is forwarded to the LLM proxy's /llm/generate-image endpoint so that
    outbound traffic goes through the LLM proxy server instead of directly from the gateway server.

    NOTE: this function is only reached from gateway.py's ask_with_image()
    when LLM_PROXY_URL is UNSET (true direct/dev mode) — gateway.py checks
    LLM_PROXY_URL itself and calls models.model_router._ProxyGateway instead
    whenever it's set. Multi-image vision support lives in _ProxyGateway
    (models/model_router.py) and services/llm_proxy/ instead; left
    single-image here since this code path is unused in any environment
    that has LLM_PROXY_URL configured (e.g. UAT/prod).
    """
    import base64 as _b64

    # ── Route through LLM proxy when configured ────────────────────────────
    # _gateway is only set when *this* function is called BY the proxy itself,
    # which means we are already on the LLM proxy server — skip proxy routing in that case.
    if _gateway is None:
        _proxy_url = os.getenv("LLM_PROXY_URL", "").rstrip("/")
        if _proxy_url:
            try:
                import httpx as _httpx
                from core.proxy_tool_use import llm_proxy_headers as _lph
                resp = _httpx.post(
                    f"{_proxy_url}/llm/generate-image",
                    json={
                        "provider":      "gemini",
                        "prompt":        prompt,
                        "image_b64":     image_b64,
                        "mime_type":     mime_type,
                        "system_prompt": system_prompt,
                    },
                    headers=_lph(),
                    timeout=float(os.getenv("LLM_TIMEOUT_SEC", "300")) or None,
                )
                resp.raise_for_status()
                data = resp.json()
                # Update token counters on the local singleton for observability
                gemini_gateway._last_input_tokens  = data.get("in_tok",  0)
                gemini_gateway._last_output_tokens = data.get("out_tok", 0)
                return data.get("text", "")
            except Exception as _proxy_err:
                logger.error(f"generate_with_image proxy call failed: {_proxy_err}")
                return "Error generating response from image"

    # ── Direct path (runs on the LLM proxy server inside the proxy, or when no proxy configured) ──
    gw = _gateway or gemini_gateway

    # Compliance gate on the text prompt
    validation = compliance_engine.validate_input(prompt)
    if validation["blocked"]:
        return "Request blocked due to PCI violation"

    safe_prompt = gw.redact(prompt, validation["findings"])

    try:
        from google.genai import types as _gtypes

        # Build a multi-part content with text + inline image
        parts = []
        if system_prompt:
            parts.append(_gtypes.Part(text=system_prompt + "\n\n"))
        parts.append(_gtypes.Part(text=safe_prompt))
        parts.append(
            _gtypes.Part(
                inline_data=_gtypes.Blob(
                    mime_type=mime_type,
                    data=_b64.b64decode(image_b64),
                )
            )
        )

        from core.retry import retry_llm
        from core.circuit_breaker import get_breaker

        def _call():
            return gw.client.models.generate_content(
                model=MODEL,
                contents=_gtypes.Content(parts=parts, role="user"),
            )

        breaker  = get_breaker("gemini")
        response = breaker.call(retry_llm, _call)

        try:
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                _um = response.usage_metadata
                _p  = getattr(_um, "prompt_token_count",      0) or 0
                _c  = getattr(_um, "candidates_token_count",   0) or 0
                _ci = getattr(_um, "cached_content_token_count", 0) or 0
                gw._last_input_tokens  = _p
                gw._last_output_tokens = _c
                logger.info(
                    f"[GEMINI USAGE] vision model={MODEL} "
                    f"prompt={_p} candidates={_c} cached_in={_ci} "
                    f"billed_in={_p - _ci} total={getattr(_um, 'total_token_count', 0) or 0}"
                )
                _log_cache_effectiveness(
                    request_id="vision",
                    model=MODEL,
                    cache_read=_ci,
                    prompt_total=_p,
                    context="vision",
                )
        except Exception:
            pass

        output = response.text or ""

        return output

    except Exception as exc:
        logger.error(f"generate_with_image failed: {exc}")
        return "Error generating response from image"