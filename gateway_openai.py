# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt / RBI PRODUCTION OPENAI GATEWAY
# ============================================================

import hashlib
import os
import uuid
from typing import Generator

from openai import OpenAI

from core.logger import logger, get_request_id as _get_request_id
from agents.compliance_engine import compliance_engine


from core.model_registry import OPENAI_PRIMARY_MODEL, OPENAI_IMAGE_MODEL

MODEL = OPENAI_PRIMARY_MODEL

# Gap #2 (7/7): stream reasoning deltas live when the model actually exposes
# reasoning text (o-series / providers that set delta.reasoning or
# reasoning_content). We NEVER fabricate reasoning — the marker is emitted only
# when a real reasoning field is present. Default-on; env opt-out; fail-safe.
_STREAM_REASONING_DELTAS = os.getenv("STREAM_REASONING_DELTAS", "true").lower() == "true"

# ── Cost-control knobs ────────────────────────────────────────────────────────
# Hard cap on completion tokens, matching gateway_claude.py:225 so a runaway
# OpenAI call cannot quietly bill 10× more than its Claude peer. Set
# OPENAI_MAX_COMPLETION_TOKENS=0 in .env to drop the cap for rollback.
try:
    _OAI_MAX_COMPLETION_TOKENS = max(0, int(os.getenv("OPENAI_MAX_COMPLETION_TOKENS", "8000")))
except (TypeError, ValueError):
    _OAI_MAX_COMPLETION_TOKENS = 8000

# "low" keeps reasoning_tokens minimal (billed at full output rate, $15/1M for
# gpt-5.4) without inflating routine CLI calls. Empty string disables entirely.
# Valid values per OpenAI API: none, low, medium, high, xhigh ("minimal" was
# removed from the API and now returns a 400 BadRequestError).
_OAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "low").strip().lower()
_VALID_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh"}
if _OAI_REASONING_EFFORT and _OAI_REASONING_EFFORT not in _VALID_REASONING_EFFORTS:
    logger.warning(
        f"OPENAI_REASONING_EFFORT={_OAI_REASONING_EFFORT!r} is not one of "
        f"{_VALID_REASONING_EFFORTS} — ignoring."
    )
    _OAI_REASONING_EFFORT = ""


def _stable_prompt_cache_key(messages_payload: list) -> str:
    """Stable 64-char key over the system prompt (and only the system prompt).

    OpenAI's implicit prompt cache hits when the *prefix* of the request is
    byte-identical across calls. Hashing the system message — which is the
    stable, repeated, high-cost portion across CLI turns — gives OpenAI a
    routing hint so cached prefix lookups succeed even when the user message
    changes. Returns "" when there is no system message (skip the field).
    """
    if not messages_payload:
        return ""
    first = messages_payload[0]
    if not isinstance(first, dict) or first.get("role") != "system":
        return ""
    content = first.get("content") or ""
    if isinstance(content, list):
        # System message could be content-blocks; flatten text parts only.
        content = "".join(
            (c.get("text") or "") for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    if not isinstance(content, str) or not content.strip():
        return ""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


# OpenAI prompt caching is automatic for prompts >= 1 024 tokens; no explicit opt-in needed.
# Cached input tokens are billed at 50% of the model's normal input rate (OpenAI policy).
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


class OpenAIGateway:

    def generate_with_model(self, prompt, model):

        from core.model_registry import BLOCKED_MODELS

        if model in BLOCKED_MODELS:
            raise Exception(f"Blocked model attempted: {model}")

        # compliance check already exists
        return self.generate(prompt)

    def __init__(self):

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            # Fallback: an admin-configured OpenAI provider with no .env key at
            # all (see core.llm_provider_registry). No-op when the env var is
            # already set — the common case is unaffected.
            try:
                from core.llm_provider_registry import resolve_credential_for_family
                api_key = resolve_credential_for_family("openai")
            except Exception:
                pass

        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")

        # No proxy needed — this gateway runs on the LLM proxy server which has direct
        # outbound access to api.openai.com via firewall allowlist.
        _t = float(os.getenv("LLM_TIMEOUT_SEC", "300"))
        self.client = OpenAI(api_key=api_key, timeout=None if _t <= 0 else _t)

        # Real token counts from the last API call — read by model_router
        self._last_input_tokens  = 0
        self._last_output_tokens = 0

        logger.info("OpenAI Gateway initialized")


    def redact(self, text, findings):

        for f in findings:

            v = f.get("value")

            if v:
                text = text.replace(v, "[REDACTED]")

        return text


    def generate(
        self,
        prompt,
        model: str = None,
        precleared: bool = False,
        precleared_findings: list = None,
    ) -> Generator[str, None, None]:
        """prompt: str (single turn) OR list[dict] (multi-turn OpenAI messages array).

        precleared:
            When True, the caller (the FastAPI /ask handler in gateway.py) has
            already run compliance_engine.validate_input() on the current user
            turn and decided to allow the request. We MUST NOT re-validate here
            because:
              (a) the ML privacy service is stochastic — two calls on the same
                  text can disagree, producing false-positive PCI blocks for
                  prompts the gateway already cleared;
              (b) the last message in `prompt` carries gateway-injected
                  metadata (tone prefix, cross-chat context, custom
                  instructions) that the first-pass gate never saw, and that
                  content can match ML/regex detectors on its own.
            Block decision is suppressed; redaction still runs using
            precleared_findings so any SECRET/PII the first pass detected is
            still masked before it leaves this process.
        precleared_findings:
            Findings list from the gateway's validate_input result. Used to
            drive the redact() pass when precleared=True.

        Default (precleared=False) preserves full defence-in-depth for any
        non-/ask caller (image-gen, follow-up suggestion, SDLC, etc.).
        """

        _model = model or MODEL
        _upstream = _get_request_id()
        request_id = _upstream if _upstream and _upstream != "-" else str(uuid.uuid4())

        # CLI mode = developer tool with full freedom; platform mode = full PCI/DSS gates
        _current_content = prompt[-1]["content"] if isinstance(prompt, list) else prompt
        _is_cli = isinstance(_current_content, str) and "---\n\nTask:" in _current_content

        if _is_cli:
            validation = {"blocked": False, "findings": []}
        elif precleared:
            # Gateway gate already cleared this prompt. Skip the block decision
            # but keep the findings the first pass produced so redact() still
            # masks anything sensitive before sending to the provider.
            validation = {
                "blocked":  False,
                "findings": list(precleared_findings or []),
            }
            logger.info(
                f"[OPENAI COMPLIANCE SKIP] request_id={request_id} reason=precleared "
                f"findings={len(validation['findings'])}"
            )
        else:
            validation = compliance_engine.validate_input(_current_content)
            if validation["blocked"]:
                # Diagnostic: surface block_types and which input shape was checked
                # so future false-positive blocks can be triaged without re-tracing.
                # We validate ONLY the last user turn (line above) — never the full
                # flattened history — to prevent stale prior-turn PII from blocking
                # benign new prompts.
                logger.warning(
                    f"[OPENAI COMPLIANCE BLOCK] request_id={request_id} "
                    f"blocked_types={validation.get('blocked_types')} "
                    f"input_shape={'messages' if isinstance(prompt, list) else 'prompt'} "
                    f"last_user_len={len(_current_content or '')}"
                )
                yield "Request blocked due to PCI violation"
                return


        from core.prompt_sanitizer import sanitize as _sanitize

        # Build final messages payload with sanitization applied to every message
        if isinstance(prompt, list):
            messages_payload = [
                {"role": m["role"], "content": _sanitize(self.redact(m.get("content") or "", validation["findings"]))}
                for m in prompt
            ]
        else:
            safe_prompt = _sanitize(self.redact(prompt, validation["findings"]))
            messages_payload = [{"role": "user", "content": safe_prompt}]

        # Reset real token counts for this call
        self._last_input_tokens  = 0
        self._last_output_tokens = 0

        try:
            from core.retry import retry_llm
            from core.circuit_breaker import get_breaker

            logger.info(f"[LLM DISPATCH] provider=openai model={_model} request_id={request_id}")

            # Hoisted out of _call() so retries reuse the cached key.
            _prefix_cache_key = _stable_prompt_cache_key(messages_payload)
            _is_gpt5 = isinstance(_model, str) and _model.startswith("gpt-5")
            # Suppress reasoning_effort when the payload already contains tool
            # calls/results — the OpenAI API rejects the parameter in that case.
            _has_tools = any(
                m.get("role") in ("tool",) or m.get("tool_calls")
                for m in messages_payload
            )

            def _call():
                kwargs = {
                    "model":          _model,
                    "stream":         True,
                    "stream_options": {"include_usage": True},
                    "messages":       messages_payload,
                }
                # 0 disables the cap (operator opt-out); omit the field entirely.
                if _OAI_MAX_COMPLETION_TOKENS > 0:
                    kwargs["max_completion_tokens"] = _OAI_MAX_COMPLETION_TOKENS
                # Prompt caching is DISABLED platform-wide by policy: never send the
                # prompt_cache_key routing hint. (Retained _prefix_cache_key computation
                # above is inert; kept only to avoid churn in the surrounding logic.)
                # if _prefix_cache_key:
                #     kwargs["prompt_cache_key"] = _prefix_cache_key
                # reasoning_effort is suppressed only when tool messages are present
                # (the API rejects it in that case). No model-specific exclusions.
                if _is_gpt5 and _OAI_REASONING_EFFORT and not _has_tools:
                    kwargs["reasoning_effort"] = _OAI_REASONING_EFFORT
                return self.client.chat.completions.create(**kwargs)

            breaker  = get_breaker("openai")
            response = breaker.call(retry_llm, _call)


            for chunk in response:

                # Capture real token counts from the final usage chunk
                # (sent with empty choices and usage populated)
                # OpenAI auto-caches prompts >= 1 024 tokens — no API flag needed.
                if hasattr(chunk, "usage") and chunk.usage:
                    self._last_input_tokens  = chunk.usage.prompt_tokens or 0
                    self._last_output_tokens = chunk.usage.completion_tokens or 0
                    _cached_stream = 0
                    try:
                        _details       = chunk.usage.prompt_tokens_details
                        _cached_stream = getattr(_details, "cached_tokens",       0) or 0
                        _audio_in      = getattr(_details, "audio_tokens",        0) or 0
                        _out_details   = getattr(chunk.usage, "completion_tokens_details", None)
                        _reasoning_out = getattr(_out_details, "reasoning_tokens", 0) or 0 if _out_details else 0
                        _billed_in     = self._last_input_tokens - _cached_stream
                        logger.info(
                            f"[OPENAI USAGE] request_id={request_id} model={_model} "
                            f"prompt={self._last_input_tokens} completion={self._last_output_tokens} "
                            f"cached={_cached_stream} billed_prompt={_billed_in} "
                            f"reasoning_out={_reasoning_out} audio_in={_audio_in} "
                            f"total={self._last_input_tokens + self._last_output_tokens}"
                        )
                    except Exception:
                        logger.info(
                            f"[OPENAI USAGE] request_id={request_id} model={_model} "
                            f"prompt={self._last_input_tokens} completion={self._last_output_tokens}"
                        )
                    _log_cache_effectiveness(
                        request_id=request_id,
                        model=_model,
                        cache_read=_cached_stream,
                        prompt_total=self._last_input_tokens,
                        context="stream",
                    )

                if not chunk.choices:
                    continue

                _delta = chunk.choices[0].delta

                # Stream reasoning text as a first-class marker WHEN the provider
                # actually exposes it (o-series / reasoning models). Never
                # fabricated. Wrapped so it can't break the token stream.
                if _STREAM_REASONING_DELTAS:
                    try:
                        _rz = (getattr(_delta, "reasoning", None)
                               or getattr(_delta, "reasoning_content", None))
                        if _rz:
                            from pipeline.stream_events import ReasoningMarker as _RM
                            yield _RM(delta=str(_rz))
                    except Exception:
                        pass

                token = _delta.content

                if not token:
                    continue

                # Per-token streaming — yield immediately without buffering.
                yield token


        except Exception as e:

            logger.exception(
                f"{request_id} → OpenAI failed → {repr(e)[:1500]}"
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
        Non-streaming tool-use loop using OpenAI function calling.

        Accepts the same arguments as ClaudeGateway.generate_with_tools() so
        ReactOrchestrator can swap gateways without changing call sites.

        Tool schemas must be in Anthropic format (input_schema key) —
        this method converts them to OpenAI format internally.

        tool_executor: callable(tool_name: str, inputs: dict) -> str
        """
        import json as _json
        from core.proxy_tool_use import _execute_with_web_search_governance, _WebSearchBudgetExhausted, flush_web_search_billing
        _upstream = _get_request_id()
        request_id = _upstream if _upstream and _upstream != "-" else str(uuid.uuid4())
        tool_names = [t["name"] for t in tools]
        logger.info(f"{request_id} → OPENAI TOOL-USE START tools={tool_names}")

        from core.prompt_sanitizer import sanitize as _sanitize
        system_prompt = _sanitize(system_prompt)
        user_message  = _sanitize(user_message)
        context       = _sanitize(context) if context else context
        user_content  = (
            f"{context}\n\n## User Request\n{user_message}" if context else user_message
        )

        # ── Proxy path: LLM_PROXY_URL set → forward to the LLM proxy server ───
        if os.getenv("LLM_PROXY_URL"):
            from core.proxy_tool_use import run_tool_use_via_proxy
            return run_tool_use_via_proxy(
                provider="openai", model=model, system_prompt=system_prompt,
                user_content=user_content, tools=tools, tool_executor=tool_executor,
                max_tokens=max_tokens, max_tool_rounds=max_tool_rounds,
                request_id=request_id, current_user=getattr(self, "_current_user", None),
            )
        # ── Direct path: LLM proxy server calls OpenAI API directly ───────────────

        oai_tools = _anthropic_to_openai_tools(tools)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ]

        self._last_input_tokens  = 0
        self._last_output_tokens = 0
        _total_cached_tokens = 0   # accumulated cache_read across all tool-use rounds

        for round_num in range(max_tool_rounds + 1):
            try:
                # Last round: strip tools so the model is forced to answer in text
                call_tools = oai_tools if round_num < max_tool_rounds else None
                kwargs: dict = {
                    "model":                  model,
                    "messages":               messages,
                    "max_completion_tokens":  max_tokens,
                }
                if call_tools:
                    kwargs["tools"]       = call_tools
                    kwargs["tool_choice"] = "auto"

                from core.retry import retry_llm
                from core.circuit_breaker import get_breaker
                logger.info(f"[LLM DISPATCH] provider=openai model={model} request_id={request_id} round={round_num}")
                breaker  = get_breaker("openai")
                response = breaker.call(retry_llm, lambda: self.client.chat.completions.create(**kwargs))

                if response.usage:
                    _round_in  = response.usage.prompt_tokens     or 0
                    _round_out = response.usage.completion_tokens  or 0
                    self._last_input_tokens  += _round_in
                    self._last_output_tokens += _round_out
                    # Read per-round cached tokens (OpenAI auto-cache)
                    try:
                        _round_cached = getattr(response.usage.prompt_tokens_details, "cached_tokens", 0) or 0
                    except Exception:
                        _round_cached = 0
                    _total_cached_tokens += _round_cached
                    _round_hit_rate = (_round_cached / _round_in * 100) if _round_in > 0 else 0.0
                    logger.info(
                        f"[OPENAI CACHE ROUND] request_id={request_id} model={model} "
                        f"round={round_num} cache_read={_round_cached} "
                        f"prompt_tokens={_round_in} hit_rate={_round_hit_rate:.1f}%"
                    )

            except Exception as e:
                logger.error(f"{request_id} → OpenAI tool-use round {round_num} failed: {e}")
                return f"[ERROR generating response: {e}]"

            msg           = response.choices[0].message
            finish_reason = response.choices[0].finish_reason

            # No tool calls or model finished → extract text and return
            if not msg.tool_calls or finish_reason == "stop":
                text = msg.content or ""
                logger.info(
                    f"[OPENAI TOOL USAGE] request_id={request_id} model={model} "
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
                logger.info(f"{request_id} → OPENAI TOOL-USE DONE after {round_num} round(s)")
                flush_web_search_billing(request_id)
                return text

            # Append assistant turn (must include tool_calls for the API to accept next turn)
            messages.append({
                "role":       "assistant",
                "content":    msg.content or "",
                "tool_calls": [
                    {
                        "id":       tc.id,
                        "type":     "function",
                        "function": {
                            "name":      tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            })

            # Execute each tool call and collect results
            logger.info(f"{request_id} → round {round_num}: {len(msg.tool_calls)} tool call(s)")
            for tc in msg.tool_calls:
                try:
                    inputs = _json.loads(tc.function.arguments or "{}")
                    result_text = _execute_with_web_search_governance(
                        request_id=request_id,
                        model=model,
                        tool_name=tc.function.name,
                        tool_inputs=inputs,
                        tool_executor=tool_executor,
                        current_user=getattr(self, "_current_user", None),
                    )
                    messages.append({
                        "role":         "tool",
                        "tool_call_id": tc.id,
                        "content":      str(result_text),
                    })
                    logger.info(f"{request_id} → {tc.function.name} → OK: {str(result_text)[:120]}")
                except _WebSearchBudgetExhausted as budget_exc:
                    logger.warning(f"{request_id} → budget exhausted during {tc.function.name}, aborting")
                    flush_web_search_billing(request_id)
                    return str(budget_exc)
                except Exception as e:
                    logger.error(f"{request_id} → {tc.function.name} failed: {e}")
                    messages.append({
                        "role":         "tool",
                        "tool_call_id": tc.id,
                        "content":      f"Error executing {tc.function.name}: {e}",
                    })

        flush_web_search_billing(request_id)
        return "[ERROR: max tool-use rounds exceeded]"


# ============================================================
# SCHEMA CONVERTER  — Anthropic → OpenAI
# ============================================================

def _anthropic_to_openai_tools(tools: list) -> list:
    """
    Convert Anthropic tool schemas (input_schema key) to OpenAI function
    calling format (parameters key under function object).
    """
    result = []
    for t in tools:
        result.append({
            "type": "function",
            "function": {
                "name":        t["name"],
                "description": t.get("description", ""),
                "parameters":  t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return result


    def generate_image(self, prompt: str, size: str = "1792x1024") -> bytes | None:
        """
        Generate an image via DALL-E 3. Returns raw PNG/JPEG bytes or None on failure.
        Routes through compliance gate; uses existing self.client (no separate proxy needed —
        gateway runs on the internet-accessible machine).
        """
        import base64
        from core.retry import retry_llm
        from core.circuit_breaker import get_breaker

        validation = compliance_engine.validate_input(prompt[:2000])
        if validation.get("blocked"):
            logger.warning("generate_image: prompt blocked by compliance")
            return None

        safe_prompt = self.redact(prompt, validation.get("findings", []))

        def _call():
            return self.client.images.generate(
                model=OPENAI_IMAGE_MODEL,
                prompt=f"{safe_prompt}. Photorealistic, professional quality, landscape orientation, no text, no watermarks.",
                size=size,
                quality="standard",
                response_format="b64_json",
                n=1,
            )

        try:
            breaker  = get_breaker("openai")
            response = breaker.call(retry_llm, _call)
            return base64.b64decode(response.data[0].b64_json)
        except Exception as exc:
            logger.error(f"OpenAI generate_image failed: {exc}")
            return None


openai_gateway = OpenAIGateway()


def generate_image_dalle(prompt: str, size: str = "1792x1024") -> bytes | None:
    """Module-level helper — generates image via DALL-E 3 through the gateway."""
    return openai_gateway.generate_image(prompt, size)


def generate_with_image_openai(
        prompt: str,
        image_b64: str,
        mime_type: str = "image/jpeg",
        system_prompt: str = "",
        _gateway: "OpenAIGateway | None" = None,
) -> tuple[str, int, int]:
    """
    Send a prompt + inline base64 image to OpenAI vision.
    Returns (text, in_tok, out_tok).

    NOTE: this function is only reached from gateway.py's ask_with_image()
    when LLM_PROXY_URL is UNSET (true direct/dev mode) — gateway.py checks
    LLM_PROXY_URL itself and calls models.model_router._ProxyGateway instead
    whenever it's set. Multi-image vision support lives in _ProxyGateway
    (models/model_router.py) and services/llm_proxy/ instead; left
    single-image here since this code path is unused in any environment
    that has LLM_PROXY_URL configured (e.g. UAT/prod).
    """
    from core.retry import retry_llm
    from core.circuit_breaker import get_breaker

    gw = _gateway or openai_gateway

    validation = compliance_engine.validate_input(prompt)
    if validation["blocked"]:
        return "Request blocked due to PCI violation", 0, 0

    safe_prompt = gw.redact(prompt, validation["findings"])

    messages: list = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
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

    def _call():
        return gw.client.chat.completions.create(model=MODEL, messages=messages)

    breaker  = get_breaker("openai")
    response = breaker.call(retry_llm, _call)

    in_tok  = getattr(response.usage, "prompt_tokens",     0) if response.usage else 0
    out_tok = getattr(response.usage, "completion_tokens", 0) if response.usage else 0
    output  = response.choices[0].message.content or "" if response.choices else ""

    logger.info(f"[OPENAI VISION] model={MODEL} in={in_tok} out={out_tok}")
    return output, in_tok, out_tok