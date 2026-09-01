# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AiNxt / RBI PRODUCTION CLAUDE GATEWAY
# CLASS BASED • PCI SAFE • ENTERPRISE READY
# ============================================================

import os
import uuid
from typing import Generator

from anthropic import Anthropic

from core.logger import logger, get_request_id as _get_request_id
from core.claude_cache_egress import (
    build_cached_sync_client as _build_cached_sync_client,
    _cache_enabled,
)
from agents.compliance_engine import compliance_engine

from core.model_registry import CLAUDE_PRIMARY_MODEL

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# CKMS — decrypt protected env vars before any os.getenv() below fires.
# Idempotent: a no-op when gateway.py has already booted CKMS.
from core.ckms import load_at_boot as _ckms_load_at_boot
_ckms_load_at_boot()

CLAUDE_MODEL = CLAUDE_PRIMARY_MODEL

# Stream extended-thinking (reasoning) deltas live as first-class events, not
# only as a post-hoc __meta__.thinking blob. Yields a ReasoningMarker per
# thinking_delta so the gateway SSE layer can emit reasoning_event frames while
# the model is still deliberating. Default-on; env opt-out. The marker str()s
# to "" so it can never pollute the answer text even for non-aware consumers.
_STREAM_REASONING_DELTAS = os.getenv("STREAM_REASONING_DELTAS", "true").lower() == "true"

# Anthropic prompt caching is applied in exactly ONE place: the egress transport
# in core/claude_cache_egress.py, which stamps a single top-level cache_control
# onto the request body at the moment it leaves for api.anthropic.com.
#
# Do NOT add cache_control to system blocks, tool definitions, or message content
# anywhere in this file — payload builders here stay marker-free and the transport
# is the sole injection point (it also strips any nested markers it finds).
#
# Toggle caching with ANTHROPIC_PROMPT_CACHE=false; optional ANTHROPIC_CACHE_TTL=1h.

# Cache pricing ratios (provider-defined, not model-specific):
#   cache_read    → billed at 10% of the model's normal input rate
#   cache_created → billed at 125% of the model's normal input rate (write surcharge)
# These ratios are stable Anthropic policy; the actual per-token dollar amount
# is derived at call time from MODEL_COST_PER_1M so it stays in sync with the
# registry without any code change when model pricing is updated.
_CACHE_READ_RATIO    = 0.10   # 10% of full input price
_CACHE_WRITE_RATIO   = 1.25   # 125% of full input price


def _log_cache_effectiveness(
    *,
    request_id: str,
    model: str,
    cache_read: int,
    cache_created: int,
    prompt_total: int,
    context: str = "",          # e.g. "stream", "non-stream", "tool-use"
) -> None:
    """Emit a structured [CACHE EFFECTIVENESS] log line for Anthropic/Claude calls.

    Derives the per-token cost from MODEL_COST_PER_1M (the single source of truth)
    so savings estimates stay accurate when model pricing changes in the registry.
    Local/in-house models have (0.0, 0.0) rates → savings_est_usd is always 0.
    Always emitted (even when all values are 0) so the absence of caching is explicit.
    """
    from core.model_registry import MODEL_COST_PER_1M
    input_rate_per_1m, _ = MODEL_COST_PER_1M.get(model, (0.0, 0.0))
    # `prompt_total` (Anthropic's `input_tokens`) excludes cache_read/cache_created —
    # they are disjoint token buckets, not overlapping subsets. The true total
    # prompt size processed by the model is the sum of all three, so the hit
    # rate must be computed against that sum, not against `prompt_total` alone
    # (which is often near-zero on a cache hit, blowing the ratio past 100%).
    _full_prompt = prompt_total + cache_read + cache_created
    hit_rate = (cache_read / _full_prompt * 100) if _full_prompt > 0 else 0.0
    # Savings: cache_read tokens billed at 10% instead of 100% of input rate
    savings_usd = cache_read * input_rate_per_1m * (1.0 - _CACHE_READ_RATIO) / 1_000_000
    # Write surcharge: cache_created tokens billed at 125% instead of 100%
    write_surcharge_usd = cache_created * input_rate_per_1m * (_CACHE_WRITE_RATIO - 1.0) / 1_000_000
    ctx_tag = f" context={context}" if context else ""
    logger.info(
        f"[CACHE EFFECTIVENESS] provider=claude request_id={request_id} model={model}{ctx_tag} "
        f"cache_read={cache_read} cache_created={cache_created} prompt_total={prompt_total} "
        f"full_prompt={_full_prompt} hit_rate={hit_rate:.1f}% savings_tokens={cache_read} "
        f"savings_est_usd={savings_usd:.6f} write_surcharge_est_usd={write_surcharge_usd:.6f} "
        f"cache_enabled={_cache_enabled()}"
    )


# ============================================================
# CLAUDE GATEWAY CLASS
# ============================================================

class ClaudeGateway:
    from core.model_registry import BLOCKED_MODELS

    if CLAUDE_MODEL in BLOCKED_MODELS:
        raise Exception("Blocked Claude model attempted")

    def __init__(self):

        api_key = os.getenv("ANTHROPIC_API_KEY")

        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")

        # No proxy needed — this gateway runs on the LLM proxy server which has direct
        # outbound access to api.anthropic.com via firewall allowlist.
        _t = float(os.getenv("LLM_TIMEOUT_SEC", "300"))
        # The cache-control marker is injected by this client's transport at the
        # moment the request leaves for api.anthropic.com — see
        # core/claude_cache_egress.py. Payload builders below stay marker-free.
        self.client = Anthropic(
            api_key=api_key,
            timeout=None if _t <= 0 else _t,
            http_client=_build_cached_sync_client(),
        )

        # Real token counts from the last API call — read by model_router
        self._last_input_tokens  = 0
        self._last_output_tokens = 0
        # Extended thinking content captured from the last streaming call.
        # Read by gateway.py SSE layer to emit a "thinking" panel in the UI.
        self._last_thinking_text = ""

        logger.info("Claude Gateway initialized")


    # ========================================================
    # REDACTION ENGINE
    # ========================================================

    def redact_sensitive_data(self, text: str, findings: list) -> str:

        redacted = text

        for finding in findings:

            value = finding.get("value")

            if value and value in redacted:

                redacted = redacted.replace(value, "[REDACTED]")

        return redacted


    # ========================================================
    # GENERATE METHOD
    # ========================================================

    def generate_with_tools(
        self,
        system_prompt: str,
        user_message: str,
        context: str,
        tools: list,
        tool_executor,
        model: str = CLAUDE_MODEL,
        max_tokens: int = 32000,
        max_tool_rounds: int = 5,
    ) -> str:
        """Non-streaming call with Claude tool-use API.

        Drives a multi-round loop:
          1. Claude receives message + tool schemas
          2. If Claude emits tool_use blocks → execute each via tool_executor
          3. Send tool_result messages back → Claude continues
          4. Repeat until Claude emits end_turn (no tool calls) or max rounds

        Returns the final text answer as a plain string.
        tool_executor: callable(tool_name: str, inputs: dict) -> str
        """
        from core.proxy_tool_use import _execute_with_web_search_governance, _WebSearchBudgetExhausted, flush_web_search_billing
        _upstream = _get_request_id()
        request_id = _upstream if _upstream and _upstream != "-" else str(uuid.uuid4())
        tool_names = [t["name"] for t in tools]
        logger.info(f"{request_id} → CLAUDE TOOL-USE START tools={tool_names}")

        # Reset and accumulate real token counts across multi-turn loop
        self._last_input_tokens  = 0
        self._last_output_tokens = 0

        from core.prompt_sanitizer import sanitize as _sanitize
        system_prompt = _sanitize(system_prompt)
        user_message  = _sanitize(user_message)
        context       = _sanitize(context) if context else context
        user_content  = f"{context}\n\n## User Request\n{user_message}" if context else user_message

        # ── Proxy path: LLM_PROXY_URL set → forward to the LLM proxy server ───
        if os.getenv("LLM_PROXY_URL"):
            from core.proxy_tool_use import run_tool_use_via_proxy
            return run_tool_use_via_proxy(
                provider="claude", model=model, system_prompt=system_prompt,
                user_content=user_content, tools=tools, tool_executor=tool_executor,
                max_tokens=max_tokens, max_tool_rounds=max_tool_rounds,
                request_id=request_id, current_user=getattr(self, "_current_user", None),
            )
        # ── Direct path: LLM proxy server calls Anthropic API directly ────────────

        # No cache_control here — the egress transport stamps a single top-level
        # marker onto the outbound request (see core/claude_cache_egress.py).
        system_blocks = [{"type": "text", "text": system_prompt}]
        first_msg_content = user_content

        messages = [{"role": "user", "content": first_msg_content}]

        for round_num in range(max_tool_rounds + 1):
            try:
                # On the last round, strip tools so Claude is forced to answer
                call_tools = tools if round_num < max_tool_rounds else []
                _tool_kw = dict(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_blocks,
                    messages=messages,
                    tools=call_tools if call_tools else None,
                )
                response = self.client.messages.create(**_tool_kw)
                # Accumulate real token counts across all tool-use rounds
                if hasattr(response, "usage") and response.usage:
                    self._last_input_tokens  += response.usage.input_tokens or 0
                    self._last_output_tokens += response.usage.output_tokens or 0
                    _cr = getattr(response.usage, "cache_read_input_tokens", 0) or 0
                    _cc = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
                    _round_in = response.usage.input_tokens or 0
                    _round_hit_rate = (_cr / _round_in * 100) if _round_in > 0 else 0.0
                    logger.info(
                        f"[CLAUDE CACHE ROUND] request_id={request_id} model={model} "
                        f"round={round_num} cache_read={_cr} cache_created={_cc} "
                        f"prompt_tokens={_round_in} hit_rate={_round_hit_rate:.1f}%"
                    )
            except Exception as e:
                logger.error(f"{request_id} → tool-use round {round_num} failed: {e}")
                return f"[ERROR generating response: {e}]"

            # Collect tool_use blocks from response
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            # If no tool calls or Claude is done, extract text and return
            if not tool_use_blocks or response.stop_reason == "end_turn":
                text = " ".join(
                    b.text for b in response.content if hasattr(b, "text") and b.text
                ).strip()
                # Final accumulated usage across all tool-use rounds
                _cr_total  = getattr(response.usage, "cache_read_input_tokens",    0) or 0
                _cc_total  = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
                _billed_in = self._last_input_tokens - _cr_total
                logger.info(
                    f"[CLAUDE USAGE] request_id={request_id} model={model} rounds={round_num} "
                    f"in={self._last_input_tokens} out={self._last_output_tokens} "
                    f"cache_read={_cr_total} cache_created={_cc_total} "
                    f"billed_in={_billed_in}"
                )
                _log_cache_effectiveness(
                    request_id=request_id,
                    model=model,
                    cache_read=_cr_total,
                    cache_created=_cc_total,
                    prompt_total=self._last_input_tokens,
                    context="tool-use",
                )
                logger.info(f"{request_id} → CLAUDE TOOL-USE DONE after {round_num} round(s)")
                flush_web_search_billing(request_id)
                return text

            # Append assistant turn
            messages.append({"role": "assistant", "content": response.content})

            # Execute each tool call and collect results.
            # P2: When ≥2 tool_use blocks have different names and no shared input keys,
            # execute them in parallel via ToolRegistry.execute_parallel() to reduce latency.
            logger.info(f"{request_id} → round {round_num}: {len(tool_use_blocks)} tool call(s)")
            tool_results = []

            def _exec_one(block):
                """Execute a single tool block, return (block_id, result_text, error)."""
                try:
                    logger.info(f"{request_id} → executing {block.name}")
                    result_text = _execute_with_web_search_governance(
                        request_id=request_id,
                        model=model,
                        tool_name=block.name,
                        tool_inputs=block.input,
                        tool_executor=tool_executor,
                        current_user=getattr(self, "_current_user", None),
                    )
                    logger.info(f"{request_id} → {block.name} → OK: {str(result_text)[:120]}")
                    return block.id, str(result_text), None
                except _WebSearchBudgetExhausted:
                    raise   # propagate up to generate_with_tools to abort the request
                except Exception as e:
                    logger.error(f"{request_id} → {block.name} failed: {e}")
                    return block.id, None, e

            def _can_parallelize(blocks) -> bool:
                """True when all blocks have distinct names and no shared input keys."""
                if len(blocks) < 2:
                    return False
                names = [b.name for b in blocks]
                if len(set(names)) < len(names):
                    return False  # duplicate tool names → sequential (order matters)
                # Check for shared input keys (potential data dependency)
                all_keys = [set((b.input or {}).keys()) for b in blocks]
                for i in range(len(all_keys)):
                    for j in range(i + 1, len(all_keys)):
                        if all_keys[i] & all_keys[j]:
                            return False  # shared key → possible dependency
                return True

            if _can_parallelize(tool_use_blocks):
                from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _asc
                _futures_map = {}
                with _TPE(max_workers=min(len(tool_use_blocks), 4),
                          thread_name_prefix="gw-parallel-tool") as _pool:
                    for _blk in tool_use_blocks:
                        _futures_map[_pool.submit(_exec_one, _blk)] = _blk
                    # Collect in submission order (preserve tool_use_id ordering)
                    _results_by_id = {}
                    for _fut in _asc(_futures_map):
                        try:
                            _bid, _rtxt, _rerr = _fut.result()
                        except _WebSearchBudgetExhausted as _bexc:
                            logger.warning(f"{request_id} → budget exhausted in parallel tool, aborting")
                            flush_web_search_billing(request_id)
                            return str(_bexc)
                        _results_by_id[_bid] = (_rtxt, _rerr)
                for _blk in tool_use_blocks:
                    _rtxt, _rerr = _results_by_id[_blk.id]
                    if _rerr is None:
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": _blk.id,
                            "content":     _rtxt,
                        })
                    else:
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": _blk.id,
                            "content":     f"Error executing {_blk.name}: {_rerr}",
                            "is_error":    True,
                        })
                logger.info(f"{request_id} → parallel execution: {len(tool_use_blocks)} tools completed")
            else:
                for block in tool_use_blocks:
                    try:
                        _bid, _rtxt, _rerr = _exec_one(block)
                    except _WebSearchBudgetExhausted as _bexc:
                        logger.warning(f"{request_id} → budget exhausted during {block.name}, aborting")
                        flush_web_search_billing(request_id)
                        return str(_bexc)
                    if _rerr is None:
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": _bid,
                            "content":     _rtxt,
                        })
                    else:
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": _bid,
                            "content":     f"Error executing {block.name}: {_rerr}",
                            "is_error":    True,
                        })

            # Append tool results as next user turn
            messages.append({"role": "user", "content": tool_results})

        flush_web_search_billing(request_id)
        return "[ERROR: max tool-use rounds exceeded]"

    def generate(
        self,
        prompt,
        model: str = CLAUDE_MODEL,
        temperature: float = 0,
        max_tokens: int = 32000,
        stream: bool = True
    ) -> Generator[str, None, None]:
        """prompt: str (single turn) OR list[dict] (multi-turn OpenAI-format messages array)."""

        _upstream = _get_request_id()
        request_id = _upstream if _upstream and _upstream != "-" else str(uuid.uuid4())

        logger.info(f"{request_id} → CLAUDE GATEWAY START")

        # Reset real token counts for this call
        self._last_input_tokens  = 0
        self._last_output_tokens = 0

        _current_content = prompt[-1]["content"] if isinstance(prompt, list) else prompt

        try:

            from core.prompt_sanitizer import sanitize as _sanitize

            # Build Anthropic messages array. Sanitize each message content; no
            # cache_control is applied here — the egress transport stamps a single
            # top-level marker (see core/claude_cache_egress.py).
            if isinstance(prompt, list):
                messages_payload = [
                    {"role": m["role"], "content": _sanitize(m.get("content") or "")}
                    for m in prompt
                ]
            else:
                messages_payload = [{"role": "user", "content": _sanitize(prompt)}]

            # ============================================
            # STEP 3 — MODEL CALL (with retry + circuit breaker)
            # ============================================

            from core.retry import retry_llm
            from core.circuit_breaker import get_breaker

            # Models that reject the `temperature` parameter, as a configurable
            # prefix list.  This was `model.startswith("claude-opus-4")` -- a
            # provider capability encoded as one vendor's naming convention, so an
            # adopter whose model also rejects temperature had no way to say so,
            # and every request to it failed on an unsupported parameter.
            _no_temp_prefixes = tuple(
                part.strip()
                for part in os.getenv("MODELS_WITHOUT_TEMPERATURE", "").split(",")
                if part.strip()
            )
            _supports_temp = not model.startswith(_no_temp_prefixes)
            _base_kwargs: dict = {
                "model":        model,
                "max_tokens":   max_tokens,
                "messages":     messages_payload,
                "stream":       stream,
            }
            if _supports_temp:
                _base_kwargs["temperature"] = temperature

            logger.info(f"[LLM DISPATCH] provider=claude model={model} request_id={request_id}")

            def _call():
                return self.client.messages.create(**_base_kwargs)

            breaker = get_breaker("claude")
            response = breaker.call(retry_llm, _call)


            # ============================================
            # STEP 4 — STREAM OUTPUT
            # ============================================

            if stream:

                # Anthropic streaming yields raw SSE events.
                # message_start  → input token count (usage.input_tokens)
                # message_delta  → output token count (usage.output_tokens)
                # content_block_delta/text_delta → actual text tokens
                _cache_read    = 0
                _cache_created = 0
                # Accumulate the full response — per-token compliance misses
                # multi-token PCI patterns (e.g. a PAN split across chunks).
                # _output_buf = ""
                _thinking_buf = ""
                # Reset thinking for this call
                # self._last_thinking_text = ""

                for event in response:

                    # Capture real input token count from message_start
                    if event.type == "message_start":
                        try:
                            _u = event.message.usage
                            self._last_input_tokens = _u.input_tokens or 0
                            _cache_read    = getattr(_u, "cache_read_input_tokens",    0) or 0
                            _cache_created = getattr(_u, "cache_creation_input_tokens", 0) or 0
                        except Exception:
                            pass
                        continue

                    # Capture real output token count from message_delta
                    if event.type == "message_delta":
                        try:
                            if hasattr(event, "usage") and event.usage:
                                self._last_output_tokens = event.usage.output_tokens or 0
                        except Exception:
                            pass
                        continue

                    if event.type == "message_stop":
                        # Flush accumulated thinking text so gateway.py SSE layer
                        # can emit a "Reasoning" panel in the UI.
                        if _thinking_buf:
                            self._last_thinking_text = _thinking_buf
                        # Log full usage summary once stream is complete
                        _billed_in = self._last_input_tokens - _cache_read
                        logger.info(
                            f"[CLAUDE USAGE] request_id={request_id} model={model} "
                            f"in={self._last_input_tokens} out={self._last_output_tokens} "
                            f"cache_read={_cache_read} cache_created={_cache_created} "
                            f"billed_in={_billed_in}"
                        )
                        _log_cache_effectiveness(
                            request_id=request_id,
                            model=model,
                            cache_read=_cache_read,
                            cache_created=_cache_created,
                            prompt_total=self._last_input_tokens,
                            context="stream",
                        )
                        continue

                    if event.type != "content_block_delta":
                        continue

                    _delta_type = getattr(event.delta, "type", "")
                    # Extended-thinking output (Claude reasoning) — buffer
                    # separately and expose via self._last_thinking_text so
                    # the gateway can emit a "Reasoning" UI panel.
                    if _delta_type == "thinking_delta":
                        _t = getattr(event.delta, "thinking", "") or ""
                        if _t:
                            _thinking_buf += _t
                            # Stream the reasoning delta live as a typed marker.
                            # Wrapped + flagged so it can never break the token
                            # stream; the marker str()s to "" for non-aware
                            # consumers so it never leaks into the answer text.
                            if _STREAM_REASONING_DELTAS:
                                try:
                                    from pipeline.stream_events import ReasoningMarker as _RM
                                    yield _RM(delta=_t)
                                except Exception:
                                    pass
                        continue

                    if _delta_type != "text_delta":
                        continue

                    token = event.delta.text

                    if not token:
                        continue

                    # Per-token streaming — yield immediately without buffering.
                    yield token

            else:

                output = response.content[0].text

                # Capture real token counts from non-streaming response
                if hasattr(response, "usage") and response.usage:
                    self._last_input_tokens  = response.usage.input_tokens or 0
                    self._last_output_tokens = response.usage.output_tokens or 0
                    _cache_read    = getattr(response.usage, "cache_read_input_tokens",    0) or 0
                    _cache_created = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
                    _billed_in = self._last_input_tokens - _cache_read
                    logger.info(
                        f"[CLAUDE USAGE] request_id={request_id} model={model} "
                        f"in={self._last_input_tokens} out={self._last_output_tokens} "
                        f"cache_read={_cache_read} cache_created={_cache_created} "
                        f"billed_in={_billed_in}"
                    )
                    _log_cache_effectiveness(
                        request_id=request_id,
                        model=model,
                        cache_read=_cache_read,
                        cache_created=_cache_created,
                        prompt_total=self._last_input_tokens,
                        context="non-stream",
                    )

                yield output


        except Exception as e:

            logger.exception(
                f"{request_id} → Claude generation failed → {repr(e)[:1500]}"
            )

            yield "Error generating response"


# ============================================================
# SINGLETON INSTANCE (IMPORTANT)
# ============================================================

claude_gateway = ClaudeGateway()