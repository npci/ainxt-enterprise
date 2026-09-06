# SPDX-License-Identifier: MIT
# ============================================================
# CLOUD TOOL STREAM — the ONE client for /llm/*-tools-stream
#
# Streams tool-call-capable cloud completions (OpenAI / Claude / Gemini) via
# the internal LLM proxy service (services/llm_proxy/main.py, reached through
# LLM_PROXY_URL). This is the single place that knows how to talk to those
# three endpoints — both routers/endpoint_proxy_router.py (managed endpoints)
# and gateway.py's IDE route (_tools_proxy_stream / _tools_claude_stream, both
# refactored to delegate here) call this module, so there is exactly one
# implementation of the HTTP call, the OpenAI<->Anthropic conversion, and the
# usage-extraction logic — not two independently-maintained copies.
#
# WHY THIS EXISTS RATHER THAN model_router.stream()
#   models.model_router.ModelRouter.stream() (the interface the endpoint
#   proxy's non-tool cloud path already uses) has no `tools` parameter — it
#   was never designed for tool calls. The only place tool calls work on
#   cloud models today is gateway.py's two IDE-route-specific closures, which
#   are not addressable from outside that route (they read ~14 free variables
#   from the enclosing request handler). This module extracts their
#   HTTP-call-and-response-translation logic into a standalone, route-agnostic
#   function.
#
# CONFIRMED BUG FIXED HERE (do not regress it)
#   gateway.py's _tools_proxy_stream() (OpenAI/Gemini branch) extracts the
#   `usage` block from INSIDE `for _choice in chunk_d.get("choices", []):`.
#   The proxy's dedicated usage chunk has `"choices": []` by design (the
#   OpenAI-spec-correct shape for a trailing usage-only chunk), so that loop
#   body never executes and usage is silently never captured — the caller
#   then falls back to a word-count of the raw JSON chunk it was streaming,
#   corrupting the billed cost. This module reads `usage` at the TOP LEVEL of
#   every chunk, unconditionally, and never estimates tokens from anything
#   except real accumulated text.
#
# YIELD CONVENTION
#   Structured Python data, never pre-serialized JSON:
#     - str                          — a text delta
#     - {"tool_call_delta": {...}}   — one OpenAI-shaped incremental tool-call
#                                       delta fragment (id/type/function.name
#                                       on the first fragment for an index,
#                                       function.arguments-only on continuations)
#     - {"__stream_meta__": {...}}   — exactly one, always last. Mirrors
#                                       models/model_router.py:2444's sentinel
#                                       shape so endpoint_proxy_router.py's
#                                       existing sentinel-consuming code (which
#                                       already detects "__stream_meta__" in a
#                                       yielded dict) needs no changes to adopt
#                                       this as a second producer.
#   Callers (gateway.py's IDE route, routers/endpoint_proxy_router.py) build
#   their own wire envelope (OpenAI SSE `chat.completion.chunk` JSON) from
#   these structured yields — this module never touches the wire format.
# ============================================================

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Generator, List, Optional

from core.logger import logger
from core.proxy_tool_use import llm_proxy_headers

# chars // 4 — the platform-wide token estimate heuristic (gateway_ollama
# .count_tokens, memory.chat_summarizer._count_tokens,
# endpoint_proxy_router._estimate_tokens). Used ONLY against real accumulated
# text (never against a serialized JSON chunk — see module docstring).
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


# ── Provider -> proxy endpoint mapping ───────────────────────────────────────

_ENDPOINTS = {
    "openai": "/llm/openai-tools-stream",
    "gemini": "/llm/gemini-tools-stream",
    "claude": "/llm/claude-tools-stream",
}


class CloudToolStreamError(Exception):
    """Raised for configuration/transport failures (not in-band provider errors,
    which are yielded as error-prefixed text so the caller's existing
    _is_gateway_error() detection catches them uniformly)."""


def _get_proxy_client():
    """
    The same persistent, connection-pooled httpx.Client every other LLM-proxy
    caller uses (models/model_router.py:117). Reusing it means this module
    adds no new connection pool and inherits the same timeout/keepalive/
    trust_env=False tuning already proven for the app02->web02 hop.
    """
    from models.model_router import _get_proxy_client as _gpc
    return _gpc()


# ── OpenAI <-> Anthropic conversion (moved verbatim from gateway.py's
#    _tools_claude_stream — this part has no bug; only the usage extraction
#    below the streaming loop was broken) ────────────────────────────────────

def _oai_tools_to_anthropic(tools: Optional[List[dict]]) -> List[dict]:
    """OpenAI `tools` array -> Anthropic tool definitions."""
    anthropic_tools = []
    for t in (tools or []):
        if isinstance(t, dict) and t.get("type") == "function":
            fn = t["function"]
            anthropic_tools.append({
                "name":         fn.get("name", ""),
                "description":  fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })
    return anthropic_tools


def _oai_messages_to_anthropic(messages: List[dict]) -> tuple:
    """
    OpenAI-format `messages` -> (system_text, anthropic_messages).

    Rules (identical to gateway.py's _tools_claude_stream, moved verbatim):
      system              -> collected, joined into one system string
      user                -> {"role": "user", "content": "..."}
      assistant+tool_calls -> {"role": "assistant", "content": [tool_use, ...]}
      assistant (plain)    -> {"role": "assistant", "content": "..."}
      tool                -> tool_result block, appended to/merged into the
                              adjacent user message (Anthropic requires
                              tool_result blocks inside a user-role message)

    Compliance is NOT re-run here — the caller (endpoint_proxy_router's
    _compliance_check_input, or gateway.py's own gates before calling this
    module) has already scanned/redacted `messages`. Re-scanning here would
    either double-block or silently diverge from the caller's `precleared`
    semantics.
    """
    system_parts: List[str] = []
    anthropic_msgs: List[dict] = []

    for m in messages:
        role = m.get("role")
        content = m.get("content")
        text = content if isinstance(content, str) else ""

        if role == "system":
            if text:
                system_parts.append(text)

        elif role == "user":
            if text:
                anthropic_msgs.append({"role": "user", "content": text})

        elif role == "assistant":
            tool_calls = m.get("tool_calls")
            if tool_calls:
                blocks: List[dict] = []
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in tool_calls:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    try:
                        inp = json.loads(fn.get("arguments", "{}") or "{}")
                    except Exception:
                        inp = {}
                    blocks.append({
                        "type":  "tool_use",
                        "id":    tc.get("id") or f"toolu_{len(blocks)}",
                        "name":  fn.get("name", "unknown"),
                        "input": inp,
                    })
                anthropic_msgs.append({"role": "assistant", "content": blocks})
            elif text:
                anthropic_msgs.append({"role": "assistant", "content": text})

        elif role == "tool":
            result_block = {
                "type":        "tool_result",
                "tool_use_id": m.get("tool_call_id") or "",
                "content":     text,
            }
            if (anthropic_msgs and anthropic_msgs[-1]["role"] == "user"
                    and isinstance(anthropic_msgs[-1]["content"], list)):
                anthropic_msgs[-1]["content"].append(result_block)
            elif (anthropic_msgs and anthropic_msgs[-1]["role"] == "user"
                    and isinstance(anthropic_msgs[-1]["content"], str)):
                anthropic_msgs[-1]["content"] = [
                    {"type": "text", "text": anthropic_msgs[-1]["content"]},
                    result_block,
                ]
            else:
                anthropic_msgs.append({"role": "user", "content": [result_block]})

    return "\n\n".join(system_parts).strip(), anthropic_msgs


# ── The shared client ─────────────────────────────────────────────────────

def stream_cloud_tools(
    messages: List[dict],
    tools: Optional[List[dict]],
    tool_choice: Any,
    model: str,
    provider: str,
    max_tokens: int = 8000,
    request_id: Optional[str] = None,
) -> Generator[Any, None, None]:
    """
    Stream a tool-call-capable completion from a cloud provider via the
    internal LLM proxy. `messages`/`tools` are OpenAI-format; `provider` must
    be one of "openai" | "claude" | "gemini" (see
    services.endpoint_model_catalog.provider_of).

    Yields (see module docstring): str | {"tool_call_delta": {...}} |
    {"__stream_meta__": {...}} (exactly one, last).

    Raises CloudToolStreamError for configuration/transport failures
    (LLM_PROXY_URL unset, unknown provider, connection failure) — these are
    NOT billable and the caller should not write a model_usages row for them.
    In-band provider errors (the provider itself signalled failure) are
    yielded as a string starting with "Error generating response" so the
    caller's existing _is_gateway_error() check catches them uniformly with
    the non-tool cloud path — a `__stream_meta__` sentinel is still yielded
    afterward, carrying whatever real usage was captured before the error, so
    the caller can bill genuine partial generation rather than forcing $0.
    """
    if provider not in _ENDPOINTS:
        raise CloudToolStreamError(f"No tools-stream endpoint for provider={provider!r}")

    proxy_url = os.getenv("LLM_PROXY_URL", "").rstrip("/")
    if not proxy_url:
        raise CloudToolStreamError(
            "LLM_PROXY_URL is not configured — cannot stream cloud tool calls."
        )

    endpoint = _ENDPOINTS[provider]
    rid = request_id or str(uuid.uuid4())

    # Real generated content, accumulated separately from anything forwarded
    # over the wire — this is what a fallback token estimate (if ever needed)
    # is computed from. NEVER estimate from a serialized JSON chunk string.
    text_parts: List[str] = []
    tool_arg_parts: List[str] = []
    in_tok = 0
    out_tok = 0
    usage_seen = False
    model_label = model
    # The provider's own finish_reason ("stop", "tool_calls", "length",
    # "content_filter", ...). Callers building an SSE envelope need the REAL
    # value, not a guess — a response cut short by "length" or blocked by
    # "content_filter" must not be reported to the caller as a clean "stop".
    finish_reason = "stop"

    def _sentinel():
        nonlocal in_tok, out_tok
        if not usage_seen:
            # Defensive fallback — none of the three providers should reach
            # this in normal operation; all three always emit a usage/`stop`
            # event. If one ever regresses, estimate from REAL text only —
            # never from a serialized JSON chunk (the confirmed bug's cause).
            #
            # Mirrors endpoint_proxy_router._resolve_token_counts exactly:
            # input is always estimated (a request always has SOME prompt, so
            # _estimate_tokens' max(1, ...) floor is correct there), but output
            # is estimated ONLY when real text/tool-arg content was actually
            # accumulated — an in-band provider error with zero generation
            # (scenario 7a) must resolve to out_tok=0, not a floored-to-1
            # estimate, or the caller would bill a "failed, no output" request
            # as if a token had been produced.
            if not in_tok:
                joined_in = "\n".join(
                    m.get("content", "") for m in messages
                    if isinstance(m, dict) and isinstance(m.get("content"), str)
                )
                in_tok = _estimate_tokens(joined_in)
            if not out_tok:
                generated = "".join(text_parts) + "".join(tool_arg_parts)
                out_tok = _estimate_tokens(generated) if generated else 0
        return {
            "__stream_meta__": {
                "in_tok":        int(in_tok or 0),
                "out_tok":       int(out_tok or 0),
                "model_label":   model_label,
                "provider":      provider,
                "finish_reason": finish_reason,
            }
        }

    try:
        client = _get_proxy_client()
    except Exception as exc:
        raise CloudToolStreamError(f"Could not obtain LLM proxy client: {exc}")

    if provider == "claude":
        system_text, anthropic_messages = _oai_messages_to_anthropic(messages)
        anthropic_tools = _oai_tools_to_anthropic(tools)
        payload: Dict[str, Any] = {
            "messages":   anthropic_messages,
            "tools":      anthropic_tools,
            "system":     system_text,
            "model":      model,
            "max_tokens": max_tokens,
            "request_id": rid,
        }
    else:
        payload = {
            "messages":   messages,
            "tools":      tools or [],
            "model":      model,
            "max_tokens": max_tokens,
            "request_id": rid,
        }
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

    try:
        with client.stream(
            "POST", f"{proxy_url}{endpoint}",
            json=payload,
            headers=llm_proxy_headers(extra={"X-Request-ID": rid}),
        ) as resp:
            resp.raise_for_status()

            if provider == "claude":
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if "error" in ev:
                        logger.error("[cloud-tool-stream] claude proxy error: %s", ev["error"])
                        yield f"Error generating response: {ev['error']}"
                        continue

                    if "tbs" in ev:
                        tbs = ev["tbs"]
                        yield {"tool_call_delta": {
                            "index":    tbs["index"],
                            "id":       tbs["id"],
                            "type":     "function",
                            "function": {"name": tbs["name"], "arguments": ""},
                        }}
                    elif "tad" in ev:
                        tad = ev["tad"]
                        tool_arg_parts.append(tad.get("partial_json", ""))
                        yield {"tool_call_delta": {
                            "index":    tad["index"],
                            "function": {"arguments": tad["partial_json"]},
                        }}
                    elif "txt" in ev:
                        txt = ev["txt"]["text"]
                        text_parts.append(txt)
                        yield txt
                    elif "stop" in ev:
                        usage_seen = True
                        in_tok  = ev.get("in_tok", 0) or 0
                        out_tok = ev.get("out_tok", 0) or 0
                        model_label = ev.get("model") or model
                        finish_reason = ev["stop"] or "stop"
            else:
                # OpenAI / Gemini — both proxy endpoints already speak
                # OpenAI-wire chat.completion.chunk shapes.
                for line in resp.iter_lines():
                    if not line or line == "data: [DONE]":
                        continue
                    raw = line[6:] if line.startswith("data: ") else line
                    try:
                        chunk_d = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    # ── THE FIX: read usage at the TOP LEVEL, unconditionally.
                    # The proxy's dedicated usage chunk has "choices": [] by
                    # design — gating this behind a `for _choice in choices`
                    # loop (as gateway.py's _tools_proxy_stream does) means it
                    # never fires. Read it here, before touching choices.
                    usage = chunk_d.get("usage")
                    if usage:
                        usage_seen = True
                        in_tok  = usage.get("prompt_tokens",     in_tok) or in_tok
                        out_tok = usage.get("completion_tokens", out_tok) or out_tok

                    if chunk_d.get("error"):
                        err = chunk_d["error"]
                        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                        logger.error("[cloud-tool-stream] %s proxy error: %s", provider, msg)
                        yield f"Error generating response: {msg}"
                        continue

                    for choice in chunk_d.get("choices", []):
                        delta = choice.get("delta", {})
                        content = delta.get("content")
                        if content:
                            text_parts.append(content)
                            yield content
                        for tc in (delta.get("tool_calls") or []):
                            fn = tc.get("function") or {}
                            if fn.get("arguments"):
                                tool_arg_parts.append(fn["arguments"])
                            yield {"tool_call_delta": tc}
                        cf = choice.get("finish_reason")
                        if cf:
                            finish_reason = cf

    except CloudToolStreamError:
        raise
    except Exception as exc:
        logger.error(
            "[cloud-tool-stream] transport failure provider=%s model=%s: %s",
            provider, model, exc,
        )
        yield f"Error generating response: {exc}"

    yield _sentinel()
