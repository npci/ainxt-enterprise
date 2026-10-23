# SPDX-License-Identifier: MIT
# ============================================================
# MANAGED-ENDPOINT DIRECT LLM CALLING (LLM_PROXY_URL unset)
#
# Deliberately NOT part of services/cloud_tool_stream.py. That module is
# shared with gateway.py's IDE/Kilo-Code tool-calling closures
# (_tools_proxy_stream / _tools_claude_stream) — changing its behavior there
# was out of scope for this feature, and gateway.py's own tool-calling path
# has an identical hard requirement on LLM_PROXY_URL today (confirmed:
# gateway.py's _tools_proxy_stream() checks `os.getenv("LLM_PROXY_URL")`
# itself and returns "Configuration error: LLM proxy not configured" before
# ever reaching stream_cloud_tools() if it's unset — there is no fallback
# there either). This module gives ONLY routers/endpoint_proxy_router.py
# (managed endpoints) a direct-to-provider fallback, so a single-host install
# with no LLM_PROXY_URL configured can still serve a managed endpoint's
# tool-calling traffic (e.g. CodeWiki) without that also silently changing
# anything about how the IDE/chat integration behaves.
#
# Public entry point: llm_proxy_configured() + direct_stream_cloud_tools(),
# with the EXACT same signature and yield convention as
# services.cloud_tool_stream.stream_cloud_tools() (str |
# {"tool_call_delta": {...}} | {"__stream_meta__": {...}}) — see
# routers/endpoint_proxy_router.py's two call sites for the one-line `if`
# that picks between them.
#
# _oai_tools_to_anthropic is IMPORTED from services.cloud_tool_stream, not
# copied — reading it here cannot change that module's behavior for its
# other caller. Message conversion (_oai_messages_to_anthropic_local below)
# is a LOCAL fork, not imported: the shared version only reads `content`
# when it's a plain string (`text = content if isinstance(content, str) else
# ""`), silently discarding OpenAI's other spec-valid shape — a list of
# content-part objects (`[{"type": "text", "text": "..."}]`) — as empty text.
# Confirmed live: a managed-endpoint tool-calling client (CodeWiki) sends
# every message that way, so the shared conversion produced an empty
# anthropic_msgs array and Claude rejected the call with "messages: at least
# one message is required". Forking it here (rather than fixing it in
# cloud_tool_stream.py) keeps that fix scoped to managed endpoints only, per
# the same isolation goal as the rest of this module. The
# chunk-accumulation/sentinel wrapper is duplicated for the same reason.
# ============================================================

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Generator, List, Optional

from core.logger import logger
from services.cloud_tool_stream import (
    CloudToolStreamError,
    _estimate_tokens,
    _oai_tools_to_anthropic,
)

# Gemini's OpenAI-compatible endpoint — lets the direct path reuse the exact
# same OpenAI-chunk parsing as the real OpenAI provider instead of a third
# bespoke integration. Known limitation: Gemini's native multi-turn
# tool-calling requires replaying an opaque `thought_signature` across turns
# (see services/llm_proxy/main.py's _oai_msgs_to_gemini for the full
# mechanism) which this OpenAI-compat endpoint does not expose — a single
# tool-call round-trips correctly but a SECOND tool call in the same
# conversation may be rejected. Configure LLM_PROXY_URL for reliable
# multi-round Gemini tool use; Claude and OpenAI have no such limitation here.
_GEMINI_OPENAI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _extract_text(content: Any) -> str:
    """
    OpenAI `content` can be a plain string OR a list of content-part objects
    (`[{"type": "text", "text": "..."}, ...]`, optionally mixed with
    `{"type": "image_url", ...}` parts) — both are valid, spec-compliant
    shapes a real OpenAI client may send. cloud_tool_stream's
    _oai_messages_to_anthropic() only handles the string case
    (`text = content if isinstance(content, str) else ""`), so any caller
    sending the list form gets silently treated as having NO content —
    confirmed live: a managed-endpoint tool-calling client (CodeWiki) sends
    every message's content as a single-element text-part list, so every
    message converted to empty text, `anthropic_msgs` ended up empty, and
    Claude rejected the call with "messages: at least one message is
    required". This local, list-aware version handles both shapes; image
    parts are skipped (Claude image support is out of scope here).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
                parts.append(p["text"])
        return "\n".join(parts)
    return ""


def _oai_messages_to_anthropic_local(messages: List[dict]) -> tuple:
    """
    List-content-aware OpenAI-format `messages` -> (system_text,
    anthropic_messages), for THIS module only. Same turn-handling rules as
    services.cloud_tool_stream._oai_messages_to_anthropic (system collected
    separately; user/assistant/tool converted to Anthropic shape; tool_result
    blocks merged into the adjacent user message) — the only difference is
    using _extract_text() above instead of a bare `isinstance(content, str)`
    check, so list-form content is read correctly instead of discarded.
    """
    import json as _json

    system_parts: List[str] = []
    anthropic_msgs: List[dict] = []

    for m in messages:
        role = m.get("role")
        text = _extract_text(m.get("content"))

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
                        inp = _json.loads(fn.get("arguments", "{}") or "{}")
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

    system_text = "\n\n".join(system_parts).strip()

    # Anthropic's Messages API hard-rejects an empty `messages` array — a
    # caller can legitimately send a request with only system-role content
    # and no user/assistant/tool turn. Fold it into a synthetic user turn
    # (the most faithful reading of "no other turn exists") instead of
    # sending nothing.
    if not anthropic_msgs and system_text:
        anthropic_msgs = [{"role": "user", "content": system_text}]
        system_text = ""

    return system_text, anthropic_msgs


def llm_proxy_configured() -> bool:
    """
    True when LLM_PROXY_URL is set. Callers (routers/endpoint_proxy_router.py)
    use this to choose between services.cloud_tool_stream.stream_cloud_tools()
    (proxy path, unchanged) and direct_stream_cloud_tools() (this module).
    """
    return bool(os.getenv("LLM_PROXY_URL", "").rstrip("/"))


def _consume_openai_shaped_chunk(chunk_d: dict) -> Generator[Any, None, None]:
    """
    Parser for one OpenAI ChatCompletionChunk-shaped dict (from the `openai`
    SDK's chunk.model_dump()). Yields str | {"tool_call_delta": {...}} |
    {"__usage__": {...}} | {"__finish_reason__": ...} — consumed by
    direct_stream_cloud_tools() below.
    """
    usage = chunk_d.get("usage")
    if usage:
        yield {"__usage__": {
            "in_tok":  usage.get("prompt_tokens"),
            "out_tok": usage.get("completion_tokens"),
        }}

    if chunk_d.get("error"):
        err = chunk_d["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        logger.error("[managed-endpoint-direct] provider error: %s", msg)
        yield f"Error generating response: {msg}"
        return

    for choice in chunk_d.get("choices", []):
        delta = choice.get("delta", {})
        content = delta.get("content")
        if content:
            yield content
        for tc in (delta.get("tool_calls") or []):
            yield {"tool_call_delta": tc}
        cf = choice.get("finish_reason")
        if cf:
            yield {"__finish_reason__": cf}


def _direct_claude(
    system_text: str,
    anthropic_messages: List[dict],
    anthropic_tools: List[dict],
    model: str,
    max_tokens: int,
) -> Generator[Any, None, None]:
    """Native Anthropic SDK tool-use streaming, in-process — mirrors
    services/llm_proxy/main.py's claude_tools_stream() event-by-event."""
    import anthropic as _anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise CloudToolStreamError(
            "Neither LLM_PROXY_URL nor ANTHROPIC_API_KEY is configured — cannot call Claude."
        )
    if not anthropic_messages:
        # _oai_messages_to_anthropic already folds system-only content into a
        # synthetic user turn — reaching here with truly nothing means the
        # caller's original `messages` array had no textual content at all.
        raise CloudToolStreamError(
            "No usable message content to send to Claude (the request's "
            "`messages` array was empty or contained no text)."
        )

    _t = float(os.getenv("LLM_TIMEOUT_SEC", "300") or 0)
    client = _anthropic.Anthropic(api_key=api_key, timeout=None if _t <= 0 else _t)

    stream_kwargs: Dict[str, Any] = {
        "model":      model,
        "max_tokens": max_tokens,
        "system":     [{"type": "text", "text": system_text or "You are a helpful AI assistant."}],
        "messages":   anthropic_messages,
    }
    if anthropic_tools:
        stream_kwargs["tools"] = anthropic_tools

    in_tok = out_tok = 0
    finish_reason = "stop"
    model_label = model

    with client.messages.stream(**stream_kwargs) as stream:
        for ev in stream:
            etype = getattr(ev, "type", "")

            if etype == "message_start":
                u = getattr(getattr(ev, "message", None), "usage", None)
                if u:
                    in_tok = getattr(u, "input_tokens", 0) or 0

            elif etype == "content_block_start":
                cb = getattr(ev, "content_block", None)
                if cb and getattr(cb, "type", "") == "tool_use":
                    idx = getattr(ev, "index", 0)
                    yield {"tool_call_delta": {
                        "index":    idx,
                        "id":       cb.id,
                        "type":     "function",
                        "function": {"name": cb.name, "arguments": ""},
                    }}

            elif etype == "content_block_delta":
                delta = getattr(ev, "delta", None)
                idx   = getattr(ev, "index", 0)
                if delta:
                    if getattr(delta, "type", "") == "text_delta":
                        yield delta.text
                    elif getattr(delta, "type", "") == "input_json_delta":
                        yield {"tool_call_delta": {
                            "index":    idx,
                            "function": {"arguments": delta.partial_json},
                        }}

            elif etype == "message_delta":
                u = getattr(ev, "usage", None)
                if u:
                    out_tok = getattr(u, "output_tokens", 0) or 0
                d = getattr(ev, "delta", None)
                if d:
                    stop = getattr(d, "stop_reason", None)
                    if stop:
                        finish_reason = "tool_calls" if stop == "tool_use" else "stop"

    yield {"__direct_meta__": {
        "in_tok": in_tok, "out_tok": out_tok,
        "model_label": model_label, "finish_reason": finish_reason,
    }}


def _direct_openai_compatible(
    messages: List[dict],
    tools: Optional[List[dict]],
    tool_choice: Any,
    model: str,
    provider: str,
    max_tokens: int,
) -> Generator[Any, None, None]:
    """
    Direct OpenAI-SDK tool-call streaming. `provider="openai"` talks to
    OPENAI_BASE_URL (default api.openai.com); `provider="gemini"` talks to
    Gemini's own OpenAI-compatible endpoint with GEMINI_API_KEY.
    """
    from openai import OpenAI as _OpenAI

    if provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise CloudToolStreamError(
                "Neither LLM_PROXY_URL nor GEMINI_API_KEY is configured — cannot call Gemini."
            )
        base_url = _GEMINI_OPENAI_COMPAT_BASE
    else:
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise CloudToolStreamError(
                "Neither LLM_PROXY_URL nor OPENAI_API_KEY is configured — cannot call OpenAI."
            )
        base_url = os.getenv("OPENAI_BASE_URL") or None

    if not messages:
        raise CloudToolStreamError(
            f"No usable message content to send to {provider} (the request's "
            "`messages` array was empty)."
        )

    _t = float(os.getenv("LLM_TIMEOUT_SEC", "300") or 0)
    client = _OpenAI(api_key=api_key, base_url=base_url, timeout=None if _t <= 0 else _t)

    kwargs: Dict[str, Any] = {
        "model":          model,
        "messages":       messages,
        "stream":         True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        kwargs["tools"] = tools
        # The OpenAI API rejects reasoning_effort alongside tool calls/results
        # — mirrors services/llm_proxy/main.py's openai_tools_stream.
        kwargs["reasoning_effort"] = "none"
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    if max_tokens:
        kwargs["max_completion_tokens"] = max_tokens

    in_tok = out_tok = 0
    finish_reason = "stop"

    response = client.chat.completions.create(**kwargs)
    for chunk in response:
        try:
            chunk_d = chunk.model_dump(exclude_none=True)
        except Exception:
            chunk_d = chunk.model_dump()
        for item in _consume_openai_shaped_chunk(chunk_d):
            if isinstance(item, dict) and "__usage__" in item:
                u = item["__usage__"]
                in_tok  = u.get("in_tok")  or in_tok
                out_tok = u.get("out_tok") or out_tok
            elif isinstance(item, dict) and "__finish_reason__" in item:
                finish_reason = item["__finish_reason__"]
            else:
                yield item

    yield {"__direct_meta__": {
        "in_tok": in_tok, "out_tok": out_tok,
        "model_label": model, "finish_reason": finish_reason,
    }}


def direct_stream_cloud_tools(
    messages: List[dict],
    tools: Optional[List[dict]],
    tool_choice: Any,
    model: str,
    provider: str,
    max_tokens: int = 8000,
    request_id: Optional[str] = None,
) -> Generator[Any, None, None]:
    """
    Managed-endpoint-only direct-to-provider tool-call streaming — call this
    INSTEAD of services.cloud_tool_stream.stream_cloud_tools() when
    llm_proxy_configured() is False. Same yield convention: str |
    {"tool_call_delta": {...}} | {"__stream_meta__": {...}} (exactly one,
    last).

    Raises CloudToolStreamError for configuration/transport failures (no
    provider API key, unknown provider, empty message content, connection
    failure) — not billable, caller should not write a model_usages row.
    In-band provider errors are yielded as a string starting with "Error
    generating response", matching stream_cloud_tools()'s convention so the
    caller's existing _is_gateway_error() check catches them uniformly.
    """
    if provider not in ("openai", "gemini", "claude"):
        raise CloudToolStreamError(f"No direct handler for provider={provider!r}")

    rid = request_id or str(uuid.uuid4())  # noqa: F841 (kept for parity/logging symmetry)

    text_parts: List[str] = []
    tool_arg_parts: List[str] = []
    in_tok = 0
    out_tok = 0
    usage_seen = False
    model_label = model
    finish_reason = "stop"

    def _sentinel():
        nonlocal in_tok, out_tok
        if not usage_seen:
            if not in_tok:
                joined_in = "\n".join(
                    _extract_text(m.get("content")) for m in messages if isinstance(m, dict)
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
        if provider == "claude":
            system_text, anthropic_messages = _oai_messages_to_anthropic_local(messages)
            anthropic_tools = _oai_tools_to_anthropic(tools)
            gen = _direct_claude(system_text, anthropic_messages, anthropic_tools, model, max_tokens)
        else:
            gen = _direct_openai_compatible(messages, tools, tool_choice, model, provider, max_tokens)

        for item in gen:
            if isinstance(item, dict) and "__direct_meta__" in item:
                meta = item["__direct_meta__"]
                usage_seen    = True
                in_tok        = meta["in_tok"]
                out_tok       = meta["out_tok"]
                model_label   = meta["model_label"]
                finish_reason = meta["finish_reason"]
            elif isinstance(item, dict) and "tool_call_delta" in item:
                tc = item["tool_call_delta"]
                fn = tc.get("function") or {}
                if fn.get("arguments"):
                    tool_arg_parts.append(fn["arguments"])
                yield item
            else:
                text_parts.append(item)
                yield item
    except CloudToolStreamError:
        raise
    except Exception as exc:
        logger.error(
            "[managed-endpoint-direct] transport failure provider=%s model=%s: %s",
            provider, model, exc,
        )
        from core.error_utils import safe_error_detail
        yield f"Error generating response: {safe_error_detail(exc)}"

    yield _sentinel()
