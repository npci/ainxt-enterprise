# SPDX-License-Identifier: MIT
"""
Pure Python LLM clients — zero LangChain dependency.

Provides:
  ToolCall         — a function call requested by the LLM
  Message          — universal message (system/user/assistant/tool)
  LLMStreamChunk   — single streaming delta (text or tool calls)
  BaseLLMClient    — abstract async streaming interface
  OpenAIClient     — OpenAI / any OpenAI-compatible API via openai SDK
  get_llm_client() — factory that picks the right client from LLMConfig

Used by: native_engine.py, main.py
"""

from __future__ import annotations

import asyncio
import json

import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from app.models import LLMConfig, LLMProvider

from core.logger import logger
# Transient network errors that warrant retry or graceful degradation when
# they occur mid-stream. The LLM endpoint (especially via reverse proxies
# like LiteLLM, vLLM, OpenRouter) sometimes drops the chunked-transfer
# connection before the final SSE event arrives. We treat these as
# "the model said what it said, just truncated" rather than crashing
# the whole workflow run.
_TRANSIENT_STREAM_ERRORS = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.ConnectError,
    httpx.TimeoutException,
)

# Emitted verbatim by the three stream clients when they exhaust their own
# retry budget without opening a stream. ``FallbackLLMClient`` matches on
# these prefixes to distinguish an exhausted-primary sentinel from real
# model output; keeping the strings in one place prevents the fallback from
# silently going dark if any emitter is reworded.
_UNREACHABLE_SENTINEL_DIRECT = "[LLM unreachable after"
_UNREACHABLE_SENTINEL_PROXY  = "[LLM proxy unreachable after"

# Permanent (non-retryable) errors raised by the openai SDK. 404 (wrong
# base_url or unknown model), 401/403 (bad token / governance), 400
# (malformed request) — all deterministic. Retrying them just burns the
# 5-attempt budget and surfaces a misleading "retry limit exceeded"
# message for what is really a config problem. ``RateLimitError`` (429) is
# intentionally excluded — it DOES resolve after backoff. The openai SDK
# is imported defensively so the module still loads in test environments
# without it installed.
class _ProxyPermanentError(Exception):
    """Raised by the proxy-routed clients on HTTP 4xx so the engine's
    permanent-error branch (``native_engine.py:1901``) surfaces a clean
    user-facing error instead of consuming the 5-retry budget.

    Registered in ``PERMANENT_LLM_ERRORS`` below so ``is_permanent_llm_error``
    treats it identically to the openai SDK's ``NotFoundError`` /
    ``AuthenticationError`` / ``BadRequestError``.
    """


try:
    from openai import (
        NotFoundError as _OAINotFound,
        AuthenticationError as _OAIAuth,
        PermissionDeniedError as _OAIPermission,
        BadRequestError as _OAIBadRequest,
    )
    PERMANENT_LLM_ERRORS: tuple = (
        _OAINotFound, _OAIAuth, _OAIPermission, _OAIBadRequest,
        _ProxyPermanentError,
    )
except Exception:
    PERMANENT_LLM_ERRORS = (_ProxyPermanentError,)


def _is_permanent_http_status(status: int) -> bool:
    """4xx statuses that won't be resolved by retry — same set the openai
    SDK classifies as permanent (404, 401/403, 400). 429 is intentionally
    excluded; it DOES resolve after backoff and is handled by the normal
    transient-retry path.
    """
    return status in (400, 401, 403, 404)


def is_permanent_llm_error(exc: BaseException) -> bool:
    """True when ``exc`` is an openai SDK error that won't resolve on retry."""
    return bool(PERMANENT_LLM_ERRORS) and isinstance(exc, PERMANENT_LLM_ERRORS)

# ---------------------------------------------------------------------------
# Retry policy (LLM + tool execution)
# ---------------------------------------------------------------------------
# Enterprise-grade: up to LLM_MAX_ATTEMPTS attempts on transient failures with
# exponential backoff before falling back / surfacing a clear, user-facing
# error message. Overridable per-deployment via env vars without code changes.
# Default is 3 (attempts 1,2,3 with 1s,2s backoff between them) so a flaky
# endpoint gets a few chances before the Sonnet 4.6 fallback engages.
LLM_MAX_ATTEMPTS = int(os.getenv("LLM_MAX_ATTEMPTS", "3"))
LLM_RETRY_BASE_DELAY = float(os.getenv("LLM_RETRY_BASE_DELAY", "1.0"))
LLM_RETRY_MAX_DELAY = float(os.getenv("LLM_RETRY_MAX_DELAY", "8.0"))


def _retry_backoff(attempt: int) -> float:
    """Exponential backoff per attempt index: 1s, 2s, 4s, 8s … capped at
    LLM_RETRY_MAX_DELAY. With the default 3 attempts the waits are 1s then 2s.
    """
    return min(LLM_RETRY_BASE_DELAY * (2 ** attempt), LLM_RETRY_MAX_DELAY)


# ---------------------------------------------------------------------------
# Observability helpers — compact, single-line previews for agent.log
# ---------------------------------------------------------------------------
# How many characters of any single message body to echo into the log. Keep it
# short so agent.log stays scannable and never dumps a full prompt/response.
# Override with LLM_LOG_PREVIEW_CHARS (0 disables content previews entirely).
LLM_LOG_PREVIEW_CHARS = int(os.getenv("LLM_LOG_PREVIEW_CHARS", "160"))


def _preview(text: Any, limit: int = LLM_LOG_PREVIEW_CHARS) -> str:
    """Collapse whitespace and clip ``text`` to ``limit`` chars for logging."""
    if not text:
        return ""
    s = " ".join(str(text).split())
    if limit and len(s) > limit:
        return s[:limit] + f"…(+{len(s) - limit} chars)"
    return s


def _messages_preview(messages: List["Message"]) -> str:
    """Build a one-line summary of an outbound LLM request for agent.log.

    Shows the per-role message count and a short preview of the LAST message
    (usually the freshest user/tool turn) — enough to trace what was sent
    without dumping the whole conversation or any large system prompt.
    """
    if not messages:
        return "roles=<none>"
    role_counts: Dict[str, int] = {}
    for m in messages:
        role_counts[m.role] = role_counts.get(m.role, 0) + 1
    roles = ",".join(f"{r}:{c}" for r, c in role_counts.items())
    last = messages[-1]
    if LLM_LOG_PREVIEW_CHARS:
        return f"msgs={len(messages)} roles=[{roles}] last[{last.role}]={_preview(getattr(last, 'content', ''))!r}"
    return f"msgs={len(messages)} roles=[{roles}]"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """A single function/tool call requested by the LLM."""
    id: str
    name: str
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    """
    Universal message — works across all LLM providers.

    role: "system" | "user" | "assistant" | "tool"
    """
    role: str
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""  # only for role="tool" (links to ToolCall.id)
    tool_name: str = ""     # only for role="tool"


@dataclass
class LLMStreamChunk:
    """A single delta emitted during streaming."""
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    is_final: bool = False  # True on the last chunk of each turn
    # OpenAI-style finish_reason ("stop" | "length" | "tool_calls" | "content_filter" | "").
    # Set on the final yielded chunk when the upstream provides it; empty when the
    # provider omits it (some gateways do) or when the stream was salvaged from a
    # mid-stream disconnect. Used by SwarmOrchestrator._call_llm to distinguish a
    # genuine max_tokens cap hit ("length") from a complete response ("stop") so
    # we stop wasting retries on phantom truncations.
    finish_reason: str = ""
    # Token accounting from the provider. Populated only on the final chunk
    # when the upstream emits a ``usage`` block (OpenAI sends one when the
    # request is opened with ``stream_options={"include_usage": True}``;
    # proxy-routed clients pass it through verbatim). Loop Engineering's
    # BudgetMeter reads this directly from the agent_complete SSE payload
    # to count tokens against the run's budget. Shape mirrors OpenAI:
    #   {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
    # None when the provider omitted it — callers fall back to a coarse
    # ``len(text)/4`` heuristic.
    # Provider usage metadata, populated on final chunks when available.
    # Shape: {"prompt_tokens": int, "completion_tokens": int,
    #         "total_tokens": int, "estimated": bool}
    usage: Optional[Dict[str, Any]] = None
    # Out-of-band signal attached by ``FallbackLLMClient`` to the FIRST chunk it
    # forwards from the fallback model, so the engine can inform the user that
    # the primary model failed and Sonnet 4.6 took over. Shape:
    #   {"kind": "model_fallback", "primary_model": str,
    #    "fallback_model": str, "reason": str}
    # ``None`` on every ordinary chunk. Carried on the chunk (rather than a
    # separate stream) so it can't be reordered relative to the tokens it
    # precedes. Consumers that don't understand it simply ignore it.
    notice: Optional[Dict[str, Any]] = None
    # Actual model that produced this chunk. Important when fallback routing
    # switches away from the originally requested model.
    model: str = ""


def _normalise_usage(
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    estimated: bool = False,
) -> Optional[Dict[str, Any]]:
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    total_tokens = max(0, int(total_tokens or (prompt_tokens + completion_tokens)))
    if not prompt_tokens and not completion_tokens and not total_tokens:
        return None
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "estimated": bool(estimated),
    }


def _usage_from_obj(usage_obj: Any) -> Optional[Dict[str, Any]]:
    if not usage_obj:
        return None
    if isinstance(usage_obj, dict):
        return _normalise_usage(
            prompt_tokens=usage_obj.get("prompt_tokens") or usage_obj.get("input_tokens") or 0,
            completion_tokens=usage_obj.get("completion_tokens") or usage_obj.get("output_tokens") or 0,
            total_tokens=usage_obj.get("total_tokens") or 0,
        )
    return _normalise_usage(
        prompt_tokens=getattr(usage_obj, "prompt_tokens", 0) or getattr(usage_obj, "input_tokens", 0) or 0,
        completion_tokens=getattr(usage_obj, "completion_tokens", 0) or getattr(usage_obj, "output_tokens", 0) or 0,
        total_tokens=getattr(usage_obj, "total_tokens", 0) or 0,
    )


# ---------------------------------------------------------------------------
# Model → provider-family classifier
# ---------------------------------------------------------------------------
# Mirrors ``services/llm_proxy/main.py::_provider_from_model`` (line 351) so
# ABStudio and the proxy agree on which family a given id belongs to. Used
# by ``get_llm_client`` to dispatch the right runtime client:
#   anthropic / openai / gemini → proxy /llm/*-tools-stream
#   local                       → LiteLLM directly (proxy bypassed)
# The CLI follows the same split (gateway.py:6236-6321 for local-direct,
# gateway.py:6323+ for proxy-routed).

def _classify_model(model_name: str) -> str:
    """Return ``"anthropic" | "openai" | "gemini" | "local"`` for ``model_name``.

    Unknown ids fall through to ``"local"`` because in-house GPUs host models
    whose ids don't follow any cloud naming convention (e.g. ``qwen-3.6-35B-A3B``,
    ``kimi-k2.6``, ``glm-5.1-fp8``, ``gemma-4-31B-it``). Routing them to
    ``LOCAL_LLM_BASE_URL`` (LiteLLM) is correct; routing them to the cloud
    proxy would 404.
    """
    name = (model_name or "").strip().lower()
    if not name:
        return "local"
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith("gemini"):
        return "gemini"
    if (
        name.startswith("gpt")
        or name.startswith("o1")
        or name.startswith("o3")
        or name.startswith("openai/")
        or name.startswith("openai-")
    ):
        return "openai"
    return "local"


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _fix_array_items(schema: dict) -> dict:
    """
    Recursively fix array schemas missing or empty 'items'.
    Some LLMs reject tool schemas where an array parameter lacks an 'items' definition.
    """
    if not isinstance(schema, dict):
        return schema
    if schema.get("type") == "array":
        items = schema.get("items")
        if not items or (
            isinstance(items, dict)
            and not items.get("type")
            and not items.get("$ref")
            and not items.get("anyOf")
        ):
            schema["items"] = {"type": "string"}
        else:
            schema["items"] = _fix_array_items(items)
    for k, v in list(schema.get("properties", {}).items()):
        schema["properties"][k] = _fix_array_items(v)
    for combiner in ("anyOf", "oneOf", "allOf"):
        if combiner in schema:
            schema[combiner] = [_fix_array_items(s) for s in schema[combiner]]
    return schema


def _clean_tool_schema(schema: dict) -> dict:
    """Remove unsupported JSON-schema fields before sending to any LLM."""
    schema = dict(schema)
    for key in ("title", "$defs", "$schema", "additionalProperties", "default"):
        schema.pop(key, None)
    if not schema.get("type"):
        schema["type"] = "object"
    if schema.get("type") == "object" and "properties" not in schema:
        schema["properties"] = {}
    _fix_array_items(schema)
    return schema


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseLLMClient:
    """Async streaming LLM interface."""

    async def stream(
        self,
        messages: List[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """
        Yield LLMStreamChunk objects.
        The last chunk always has is_final=True.
        Tool calls (if any) are delivered on the final chunk.

        ``response_format`` is the OpenAI-compatible structured-output
        spec — e.g. ``{"type": "json_schema", "json_schema": {...}}``
        or ``{"type": "json_object"}``. When provided, gateways that
        support it will physically constrain the model's output to the
        schema, eliminating shape drift. None = unconstrained (default).
        """
        raise NotImplementedError
        yield  # make this an async generator  # noqa: unreachable

    async def complete(
        self,
        messages: List[Message],
        *,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Non-streaming convenience wrapper — returns full text response."""
        text = ""
        async for chunk in self.stream(messages, response_format=response_format):
            text += chunk.text
        return text

    async def complete_nonstream(
        self,
        messages: List[Message],
        *,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Return the full completion via a genuine ``stream=False`` request
        where the client supports it. The base implementation just delegates to
        ``complete`` (streaming under the hood); ``OpenAIClient`` overrides this
        with a true non-streaming request, and ``FallbackLLMClient`` forwards to
        its underlying client. Factory pipelines call this to dodge gateways
        whose STREAMING endpoint returns "Error generating response" for large
        generations while the non-streaming endpoint works.
        """
        return await self.complete(messages, response_format=response_format)

    async def complete_with_finish_reason(
        self,
        messages: List[Message],
        *,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, str]:
        """Like ``complete`` but also returns the OpenAI ``finish_reason``.

        Returns ``(text, finish_reason)``. ``finish_reason`` will be one of
        ``"stop"``, ``"length"``, ``"tool_calls"``, ``"content_filter"`` or
        an empty string when the provider omitted it / the stream was
        salvaged from a disconnect. The SwarmOrchestrator uses this to
        avoid wasted retries on cap-hit responses that ``_looks_truncated``
        would mis-classify as upstream drops.
        """
        text = ""
        finish_reason = ""
        async for chunk in self.stream(messages, response_format=response_format):
            text += chunk.text
            # The final chunk is the authoritative source — earlier chunks
            # usually carry finish_reason="" until the provider closes the
            # stream. We still update on every chunk so a provider that
            # sets it early doesn't get lost.
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason
        return text, finish_reason


# ---------------------------------------------------------------------------
# OpenAI-compatible (openai SDK, no LangChain)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Shared HTTP client pool — avoids creating a new TLS connection per LLM call
# ---------------------------------------------------------------------------

_shared_http_clients: Dict[str, Any] = {}  # keyed by (base_url, ssl_verify)


# Enterprise-grade timeout profile for LLM streaming. httpx defaults
# (5s on every phase) are far too aggressive for long generations and
# for cases where one flow's "generate with AI" step is attached to a
# downstream flow — both legs need headroom or the chain aborts.
#
#   connect : 30s   — TLS handshake / cold-start gateways
#   read    : 300s  — gap between SSE chunks; long generations stall briefly
#   write   : 60s   — large prompt uploads (RAG context, chat history)
#   pool    : 30s   — wait for a free connection under burst load
#
# Override per-deployment via LLM_HTTP_* env vars without touching code.
_LLM_HTTP_TIMEOUT = httpx.Timeout(
    connect=float(os.getenv("LLM_HTTP_CONNECT_TIMEOUT", "30")),
    read=float(os.getenv("LLM_HTTP_READ_TIMEOUT", "300")),
    write=float(os.getenv("LLM_HTTP_WRITE_TIMEOUT", "60")),
    pool=float(os.getenv("LLM_HTTP_POOL_TIMEOUT", "30")),
)

# Connection-pool limits sized for 200+ concurrent LLM streams. httpx's
# defaults (max_connections=100, max_keepalive=20) cause pool starvation under
# burst load — callers then block on the 30s pool timeout. We raise both caps
# so concurrent streams have headroom while still bounding socket usage.
_LLM_HTTP_LIMITS = httpx.Limits(
    max_connections=int(os.getenv("LLM_HTTP_MAX_CONNECTIONS", "400")),
    max_keepalive_connections=int(os.getenv("LLM_HTTP_MAX_KEEPALIVE", "200")),
    keepalive_expiry=float(os.getenv("LLM_HTTP_KEEPALIVE_EXPIRY", "30")),
)


def _get_shared_http_client(base_url: str, ssl_verify: bool) -> Any:
    """Return a shared ``httpx.AsyncClient`` for the given endpoint.

    Reusing the HTTP client across calls lets the underlying connection pool
    keep TCP+TLS connections alive, avoiding the ~1-3s handshake overhead
    that dominated factory latency when every call opened a fresh socket.

    The client is configured with an enterprise-grade timeout profile (see
    ``_LLM_HTTP_TIMEOUT`` above) and pool limits tuned for 200+ concurrent
    LLM streams so long generations and chained flows aren't truncated by
    httpx's aggressive defaults.
    """
    key = f"{base_url}|{ssl_verify}"
    if key not in _shared_http_clients:
        _shared_http_clients[key] = httpx.AsyncClient(
            verify=ssl_verify,
            timeout=_LLM_HTTP_TIMEOUT,
            limits=_LLM_HTTP_LIMITS,
        )
    return _shared_http_clients[key]


# ---------------------------------------------------------------------------
# Shared OpenAI-shape converters
# ---------------------------------------------------------------------------
# Originally lived on ``OpenAIClient`` as instance methods. Lifted to module
# scope because the proxy-routed clients below (``OpenAIProxyClient`` /
# ``GeminiProxyClient``) post the same OpenAI-shape payload to a different
# URL — sharing one conversion path keeps any future tool-spec or
# message-role tweak in one place.

def _messages_to_oai(messages: List["Message"]) -> List[dict]:
    out = []
    for msg in messages:
        if msg.role in ("system", "user"):
            out.append({"role": msg.role, "content": msg.content})
        elif msg.role == "assistant":
            item: dict = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.args),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            out.append(item)
        elif msg.role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": msg.tool_call_id,
                "content": msg.content,
            })
    return out


def _tools_to_oai(tools: List[dict]) -> List[dict]:
    out = []
    for t in tools:
        params = _clean_tool_schema(dict(t.get("parameters") or {}))
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": (t.get("description") or "")[:500],
                "parameters": params,
            },
        })
    return out


class OpenAIClient(BaseLLMClient):
    """
    Calls any OpenAI-compatible API using the openai Python SDK.
    Compatible with OpenAI, Ollama, LM Studio, vLLM, etc.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float,
        max_tokens: int,
        top_p: float,
    ) -> None:
        import openai as _oai
        # When the caller (engine / orchestrator) didn't pass an explicit
        # base_url, fall back through the LLM_PROXY-aware helpers in
        # ``app.core.config`` rather than a hardcoded localhost. SIT has no
        # local Ollama — the only reachable OpenAI-compatible surface is
        # ``${LLM_PROXY_URL}/v1``. The previous ``or "http://localhost:11434/v1"``
        # default produced ``Connection refused`` errors that masqueraded
        # as orchestrator timeouts after retries.
        from app.core.config import (
            openai_compatible_base_url as _resolved_base_url,
            openai_compatible_api_key as _resolved_api_key,
        )
        effective_base_url = base_url or _resolved_base_url()
        effective_api_key = api_key or _resolved_api_key()
        ssl_verify = os.getenv("SSL_VERIFY", "true").lower() not in ("false", "0", "no")
        http_client = _get_shared_http_client(effective_base_url, ssl_verify)
        _proxy_token = os.getenv("LLM_PROXY_TOKEN", "")
        _resolved_token = effective_api_key or "not-needed"
        self._client = _oai.AsyncOpenAI(
            api_key=_resolved_token,
            base_url=effective_base_url,
            http_client=http_client,
            default_headers={"X-Internal-Token": _proxy_token} if _proxy_token else {},
        )
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._top_p = top_p

    async def stream(
        self,
        messages: List[Message],
        tools: Optional[List[Dict]] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        oai_messages = _messages_to_oai(messages)
        kwargs: dict = dict(
            model=self._model,
            messages=oai_messages,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            top_p=self._top_p,
            stream=True,
            # Ask the upstream to emit a `usage` block on the final delta.
            # OpenAI honours this; LiteLLM/local gateways either honour it
            # or ignore the unknown kwarg silently — both are fine.
            # Loop Engineering's BudgetMeter reads this off the final
            # LLMStreamChunk.usage to count tokens against the run budget.
            # If a gateway rejects the kwarg with a 4xx we surface that
            # via the existing PERMANENT_LLM_ERRORS path; remove the kwarg
            # in that environment.
            stream_options={"include_usage": True},
        )
        if tools:
            kwargs["tools"] = _tools_to_oai(tools)
            kwargs["tool_choice"] = "auto"
        # Structured-output: pass the json_schema / json_object spec the
        # caller wants enforced. Compatible gateways will reject any
        # token sequence that wouldn't parse against the schema, which is
        # the strongest possible defense against the SwarmOrchestrator's
        # shape drift (planner LLM wrapping the plan in {"swarm_plan":...}
        # or emitting `worker_id`/`tool_hints` instead of the contract
        # shape). Gateways that don't support response_format will reject
        # the request with a clean 4xx — SwarmOrchestrator wraps this
        # call site and disables structured output on that signal, so
        # the kwarg is opaquely passed through here.
        if response_format:
            kwargs["response_format"] = response_format
        if not any(marker in (self._model or "").lower() for marker in ("local", "llama", "ollama")):
            kwargs["stream_options"] = {"include_usage": True}

        # ── Observability: what are we sending to the LLM? ──
        # One compact line so operators can trace the request in agent.log:
        # model, sampling knobs, tool count, and a short preview of the last
        # message. Full prompts are never dumped (see _messages_preview).
        _t_send = time.perf_counter()
        logger.info(
            f"[AGENT] → LLM request model={self._model} temp={self._temperature} "
            f"max_tokens={self._max_tokens} tools={len(tools or [])} "
            f"structured={bool(response_format)} {_messages_preview(messages)}"
        )

        # Buffer for assembling fragmented tool call chunks. Lives outside
        # the retry loop so a partial buffer survives a salvage path.
        tc_buf: Dict[int, dict] = {}
        any_text_yielded = False
        # Authoritative truncation signal from the provider. Typically
        # arrives only on the final delta ("stop" / "length" /
        # "tool_calls" / "content_filter"); stays "" when the provider
        # omits it or when the stream is salvaged from a disconnect.
        final_finish_reason: str = ""
        final_usage: Optional[Dict[str, Any]] = None

        # ── Open the stream with up to LLM_MAX_ATTEMPTS (default 5) ──
        # We only retry BEFORE any tokens have been yielded — once the
        # caller has seen text, restarting would duplicate output. Risk
        # accepted: if a partial request reached the provider before
        # disconnecting, a retry may bill twice. Duplicate user-visible
        # output is impossible (only one yield path runs).
        #
        # Exponential backoff (1s → 2s → 4s → 8s → 8s) between attempts.
        # When all attempts are exhausted we surface a clear, actionable
        # error message instead of crashing the workflow run.
        response_stream = None
        last_exc: Optional[BaseException] = None
        for attempt in range(LLM_MAX_ATTEMPTS):
            try:
                logger.debug(f'[AGENT] … waiting for LLM response model={self._model} (attempt {attempt + 1}/{LLM_MAX_ATTEMPTS})')
                response_stream = await self._client.chat.completions.create(**kwargs)
                _open_ms = (time.perf_counter() - _t_send) * 1000
                if attempt > 0:
                    logger.info(f'[AGENT] LLM stream opened on attempt {attempt + 1}/{LLM_MAX_ATTEMPTS} after {_open_ms:.0f}ms')
                else:
                    logger.info(f'[AGENT] LLM stream opened model={self._model} ttfb={_open_ms:.0f}ms — streaming response')
                last_exc = None
                break
            except PERMANENT_LLM_ERRORS as exc:
                # Permanent — log enough context for UAT operators to identify
                # the misconfigured endpoint without leaking secrets, then
                # re-raise so the engine's outer loop reports the original
                # error verbatim (not "retry limit exceeded").
                _safe_base = getattr(self._client, "base_url", None) or "<unset>"
                logger.error(f"[AGENT] LLM stream open failed PERMANENTLY ({type(exc).__name__}) — base_url={_safe_base} model={self._model}. Not retrying; verify LLM_PROXY_URL points at the proxy ROOT (no trailing /v1) and the model is in the proxy's catalogue.")
                raise
            except _TRANSIENT_STREAM_ERRORS as exc:
                last_exc = exc
                if attempt < LLM_MAX_ATTEMPTS - 1:
                    delay = _retry_backoff(attempt)
                    logger.warning(f'[AGENT] LLM stream open failed ({type(exc).__name__}); attempt {attempt + 1}/{LLM_MAX_ATTEMPTS} — retrying in {delay}s')
                    # Surface the retry live so the user sees progress instead
                    # of a frozen spinner. Carried as a ``notice`` on an empty
                    # chunk (no text/tool_calls) so downstream treats it as a
                    # status signal, not model output. ``next_attempt`` is
                    # 1-based for display ("retrying attempt 2/3").
                    yield LLMStreamChunk(notice={
                        "kind": "llm_retry",
                        "model": self._model,
                        "attempt": attempt + 1,
                        "next_attempt": attempt + 2,
                        "max_attempts": LLM_MAX_ATTEMPTS,
                        "delay_s": round(delay, 1),
                        "error": type(exc).__name__,
                    })
                    await asyncio.sleep(delay)
                else:
                    logger.error(f'[AGENT] LLM stream open failed on final attempt {attempt + 1}/{LLM_MAX_ATTEMPTS} ({type(exc).__name__}); giving up')

        if response_stream is None:
            err_name = type(last_exc).__name__ if last_exc else "unknown"
            yield LLMStreamChunk(
                text=(
                    f"{_UNREACHABLE_SENTINEL_DIRECT} {LLM_MAX_ATTEMPTS} attempts "
                    f"({err_name}). Please verify the model endpoint is "
                    "online and try again.]"
                ),
                tool_calls=[],
                is_final=True,
            )
            return

        # ── Consume the stream, salvaging whatever we got on disconnect ──
        # Captured from the provider's terminal `usage` block when
        # stream_options={"include_usage": True} is honoured. OpenAI sends
        # it on a chunk *without* choices (the final summary delta); some
        # local gateways attach it to the last content chunk instead — we
        # accept either shape so the BudgetMeter sees a value where the
        # provider supplied one. Stays None on providers that omit it.
        final_usage: Optional[Dict[str, int]] = None
        # Accumulate only the first few chars of streamed text for a completion
        # preview in agent.log — bounded so we never buffer a whole response.
        _resp_head: List[str] = []
        _resp_head_len = 0
        _resp_chars = 0
        try:
            async for chunk in response_stream:
                # Usage may arrive on a "no choices" terminal chunk OR
                # piggy-backed on a content chunk depending on the gateway.
                # Normalize any provider-specific shape before forwarding it.
                usage = _usage_from_obj(getattr(chunk, "usage", None))
                if usage:
                    final_usage = usage
                choice = chunk.choices[0] if chunk.choices else None
                if not choice:
                    # OpenAI's usage-only terminal chunk has no choices —
                    # we've already grabbed `usage` above, so skip the
                    # rest of the per-choice handling.
                    continue
                delta = choice.delta

                # Capture the finish_reason as soon as the provider emits
                # it. OpenAI-compatible streams set it only on the final
                # delta; some gateways set it earlier — either way, the
                # last non-empty value wins.
                fr = getattr(choice, "finish_reason", None)
                if fr:
                    final_finish_reason = str(fr)

                if delta.content:
                    any_text_yielded = True
                    _resp_chars += len(delta.content)
                    if LLM_LOG_PREVIEW_CHARS and _resp_head_len < LLM_LOG_PREVIEW_CHARS:
                        _resp_head.append(delta.content)
                        _resp_head_len += len(delta.content)
                    yield LLMStreamChunk(text=delta.content)

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tc_buf:
                            tc_buf[idx] = {"id": "", "name": "", "args_str": ""}
                        if tc.id:
                            tc_buf[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tc_buf[idx]["name"] += tc.function.name
                            if tc.function.arguments:
                                tc_buf[idx]["args_str"] += tc.function.arguments
        except _TRANSIENT_STREAM_ERRORS as exc:
            # Upstream hung up mid-stream. Log it, keep what we already
            # received, and fall through to the finaliser so any partial
            # tool calls and accumulated text still surface. Escalate to
            # error when there's nothing to salvage — that's effectively a
            # failed turn, not a soft truncation.
            salvaged_nothing = not any_text_yielded and not tc_buf
            log = logger.error if salvaged_nothing else logger.warning
            log(
                f"[AGENT] LLM stream truncated by upstream ({type(exc).__name__}); "
                f"salvage state (text_yielded={any_text_yielded}, "
                f"buffered_tool_calls={len(tc_buf)})"
            )

        # ── Reconstruct any buffered tool calls ──
        # Drop tool calls whose JSON arguments are unparseable — running a
        # tool with garbage args is worse than skipping it. The agent will
        # see no tool was called and either retry or produce text instead.
        final_tool_calls: List[ToolCall] = []
        for idx in sorted(tc_buf.keys()):
            d = tc_buf[idx]
            if not d["args_str"]:
                args: dict = {}
            else:
                try:
                    args = json.loads(d["args_str"])
                except json.JSONDecodeError:
                    logger.warning(f"[AGENT] Dropping tool call {d.get('name') or f'idx_{idx}'} with unparseable args (likely truncated mid-stream)")
                    continue
            final_tool_calls.append(ToolCall(
                id=d["id"] or f"call_{idx}",
                name=d["name"],
                args=args,
            ))

        # ── Observability: what did the LLM return? ──
        # Elapsed time, finish_reason, token usage, tool-call names, and a short
        # preview of the response text — one line to close the request/response
        # pair opened by the "→ LLM request" log above.
        _elapsed_ms = (time.perf_counter() - _t_send) * 1000
        _tool_names = [tc.name for tc in final_tool_calls]
        _usage_str = ""
        if final_usage:
            _usage_str = (
                f" tokens(in/out/total)="
                f"{final_usage.get('prompt_tokens', '?')}/"
                f"{final_usage.get('completion_tokens', '?')}/"
                f"{final_usage.get('total_tokens', '?')}"
            )
        _preview_str = ""
        if LLM_LOG_PREVIEW_CHARS and _resp_head:
            _preview_str = f" text={_preview(''.join(_resp_head))!r}"
        logger.info(
            f"[AGENT] ← LLM response model={self._model} elapsed={_elapsed_ms:.0f}ms "
            f"finish={final_finish_reason or '<none>'} chars={_resp_chars} "
            f"tool_calls={_tool_names}{_usage_str}{_preview_str}"
        )

        yield LLMStreamChunk(
            text="",
            tool_calls=final_tool_calls,
            is_final=True,
            finish_reason=final_finish_reason,
            usage=final_usage,
            model=self._model,
        )

    async def complete_nonstream(
        self,
        messages: List[Message],
        *,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Non-streaming completion — a single ``stream=False`` request that
        returns the full assistant text in one shot.

        Some gateways return ``Error generating response`` (a 200 body with no
        real content) for LARGE-generation requests on the STREAMING endpoint
        while the same request on the NON-streaming endpoint returns valid
        output. The factory pipelines (which wait for a full JSON blob anyway
        and gain nothing from streaming) use this path to avoid that broken
        streaming behaviour. Mirrors the kwargs of ``stream`` minus stream-only
        options.
        """
        oai_messages = _messages_to_oai(messages)
        kwargs: dict = dict(
            model=self._model,
            messages=oai_messages,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            top_p=self._top_p,
            stream=False,
        )
        if response_format:
            kwargs["response_format"] = response_format

        resp = await self._client.chat.completions.create(**kwargs)
        try:
            return resp.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            # Defensive: if a gateway returns a non-standard shape, stringify so
            # the caller's parser sees *something* rather than crashing here.
            return str(resp)


# ===========================================================================
# Proxy-routed clients — speak the platform llm_proxy's /llm/* surface
# ===========================================================================
#
# Why these exist:
#   The deployed llm_proxy at LLM_PROXY_URL (the LLM proxy server in prod) does NOT expose
#   POST /v1/chat/completions. It exposes a legacy ``/llm/*`` family that
#   ABStudio's OpenAI-SDK-based ``OpenAIClient`` cannot speak. The CLI works
#   because gateway.py posts to ``/llm/openai-tools-stream`` /
#   ``/llm/claude-tools-stream`` / ``/llm/gemini-tools-stream`` directly
#   (see gateway.py:6323+); local models are routed direct to LiteLLM,
#   bypassing the proxy entirely (gateway.py:6236-6321).
#
# These three clients give ABStudio the same split so cloud model selections
# (Claude/OpenAI/Gemini) and skill/tool calls work end-to-end against the
# proxy as-deployed, with no proxy-side changes required.
#
# Parameters dropped at the proxy (matches CLI behaviour):
#   * temperature, top_p — proxy doesn't forward them
#     (services/llm_proxy/main.py:872-885)
#   * response_format — proxy doesn't forward it; swarm orchestrator already
#     has runtime auto-fallback when gateway rejects this kwarg
#     (app/swarm/orchestrator.py:121-130).


class _BaseProxyClient(BaseLLMClient):
    """Shared plumbing for ``OpenAIProxyClient`` / ``GeminiProxyClient`` /
    ``ClaudeProxyClient``.

    Holds the model + max_tokens (the only knobs the proxy honors), the
    shared httpx pool, and the ``X-Internal-Token`` header injection.
    """

    # Subclasses override.
    _ENDPOINT_SUFFIX: str = ""

    def __init__(
        self,
        model: str,
        max_tokens: int,
        temperature: float = 0.0,
        top_p: float = 1.0,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        # Stored only for parity with ``OpenAIClient.__init__``; the proxy
        # does not forward these to the upstream provider.
        self._temperature = temperature
        self._top_p = top_p

    def _proxy_root(self) -> str:
        """Return ``LLM_PROXY_URL`` (no trailing ``/v1``) via the shared helper."""
        from app.core.config import llm_proxy_root
        return llm_proxy_root()

    def _proxy_headers(self) -> Dict[str, str]:
        """Inject ``X-Internal-Token`` when ``LLM_PROXY_TOKEN`` is set.

        Matches ``core/proxy_tool_use.py::llm_proxy_headers`` so the auth
        contract is identical to the CLI's.
        """
        headers: Dict[str, str] = {"Accept": "application/x-ndjson"}
        token = os.getenv("LLM_PROXY_TOKEN", "").strip()
        if token:
            headers["X-Internal-Token"] = token
        return headers

    def _http_client(self) -> Any:
        ssl_verify = os.getenv("SSL_VERIFY", "true").lower() not in ("false", "0", "no")
        return _get_shared_http_client(self._proxy_root(), ssl_verify)

    def _endpoint_url(self) -> str:
        root = self._proxy_root()
        if not root:
            raise _ProxyPermanentError(
                "LLM_PROXY_URL is not set — cloud models require the proxy. "
                "Either set LLM_PROXY_URL or pick a local model."
            )
        return f"{root}{self._ENDPOINT_SUFFIX}"


# ---------------------------------------------------------------------------
# OpenAI-shape NDJSON parser (shared by OpenAI + Gemini proxy clients)
# ---------------------------------------------------------------------------
# Both ``/llm/openai-tools-stream`` (services/llm_proxy/main.py:845) and
# ``/llm/gemini-tools-stream`` emit raw OpenAI ``ChatCompletionChunk`` JSON
# objects, one per NDJSON line, followed by a ``{"done": true}`` sentinel.
# The chunk shape is identical to what the openai SDK yields, so we walk
# the same ``choices[0].delta`` structure ``OpenAIClient.stream`` already
# handles.

class OpenAIProxyClient(_BaseProxyClient):
    """Stream chat completions through the proxy's ``/llm/openai-tools-stream``.

    Used for any model classified as ``openai`` by ``_classify_model``
    (``gpt-*``, ``o1-*``, ``o3-*``, ``openai/*``). Wire format is OpenAI
    ChatCompletionChunk NDJSON — same shape ``OpenAIClient.stream`` parses
    when speaking ``/v1/chat/completions`` directly.
    """

    _ENDPOINT_SUFFIX = "/llm/openai-tools-stream"

    def _build_payload(
        self,
        messages: List[Message],
        tools: Optional[List[Dict]],
    ) -> dict:
        payload: dict = {
            "model": self._model,
            "messages": _messages_to_oai(messages),
            "max_tokens": self._max_tokens,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = _tools_to_oai(tools)
            payload["tool_choice"] = "auto"
        return payload

    async def stream(
        self,
        messages: List[Message],
        tools: Optional[List[Dict]] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        # ``response_format`` is not forwarded by the proxy endpoint
        # (services/llm_proxy/main.py:872-885). Raise TypeError so the swarm
        # orchestrator except-TypeError catch block (orchestrator.py:992-1002)
        # fires, sets _gateway_supports_json_schema = False, and retries
        # without the kwarg - preventing silent cache-poisoning.
        if response_format is not None:
            raise TypeError(
                "unexpected keyword argument 'response_format': "
                "OpenAIProxyClient forwards to the LLM proxy which does not support "
                "structured-output constraints. The caller should retry without "
                "response_format and use prompt-level JSON instructions."
            )

        payload = self._build_payload(messages, tools)
        url = self._endpoint_url()
        http = self._http_client()
        headers = self._proxy_headers()

        # ── Observability: what are we sending to the LLM proxy? ──
        _t_send = time.perf_counter()
        logger.info(
            f"[AGENT] → LLM proxy request model={self._model} max_tokens={self._max_tokens} "
            f"tools={len(tools or [])} url={url} {_messages_preview(messages)}"
        )
        logger.debug(f'[AGENT] … waiting for LLM proxy response model={self._model}')

        tc_buf: Dict[int, dict] = {}
        any_text_yielded = False
        final_finish_reason: str = ""
        final_usage: Optional[Dict[str, Any]] = None
        # Bounded response-text preview accumulator (see OpenAIClient.stream).
        _resp_head: List[str] = []
        _resp_head_len = 0
        _resp_chars = 0

        # Open the stream with up to LLM_MAX_ATTEMPTS retries — same policy
        # as ``OpenAIClient.stream``. Only retry before any token is yielded;
        # once the caller has seen text, restarting would duplicate output.
        last_exc: Optional[BaseException] = None
        stream_started_outside_retry = False
        for attempt in range(LLM_MAX_ATTEMPTS):
            try:
                async with http.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        # Read body for the error message before deciding
                        # permanent vs. transient.
                        try:
                            err_body = (await resp.aread()).decode("utf-8", errors="replace")
                        except Exception:
                            err_body = ""
                        if _is_permanent_http_status(resp.status_code):
                            logger.error(f'[AGENT] LLM proxy {url} returned {resp.status_code} (permanent) body={err_body[:300]}')
                            raise _ProxyPermanentError(
                                f"{resp.status_code} from {url}: {err_body[:300]}"
                            )
                        # Transient — let the retry loop catch via raise_for_status.
                        resp.raise_for_status()

                    # Consume the NDJSON stream.
                    if attempt > 0:
                        logger.info(f'[AGENT] LLM proxy stream opened on attempt {attempt + 1}/{LLM_MAX_ATTEMPTS} ({url})')
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            # Tolerate the rare malformed line — same posture
                            # as gateway.py:6379-6385.
                            continue
                        usage = _usage_from_obj(data.get("usage"))
                        if usage:
                            final_usage = usage
                        if data.get("done") is True:
                            continue
                        if "error" in data and not data.get("choices"):
                            raise _ProxyPermanentError(str(data.get("error")))
                        for choice in data.get("choices", []):
                            delta = choice.get("delta") or {}
                            fr = choice.get("finish_reason")
                            if fr:
                                final_finish_reason = str(fr)
                            content = delta.get("content")
                            if content:
                                stream_started_outside_retry = True
                                any_text_yielded = True
                                _resp_chars += len(content)
                                if LLM_LOG_PREVIEW_CHARS and _resp_head_len < LLM_LOG_PREVIEW_CHARS:
                                    _resp_head.append(content)
                                    _resp_head_len += len(content)
                                yield LLMStreamChunk(text=content)
                            for tc in (delta.get("tool_calls") or []):
                                stream_started_outside_retry = True
                                idx = tc.get("index", 0)
                                if idx not in tc_buf:
                                    tc_buf[idx] = {"id": "", "name": "", "args_str": ""}
                                if tc.get("id"):
                                    tc_buf[idx]["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    tc_buf[idx]["name"] += fn["name"]
                                if fn.get("arguments"):
                                    tc_buf[idx]["args_str"] += fn["arguments"]
                # Stream finished cleanly — exit the retry loop.
                last_exc = None
                break
            except _ProxyPermanentError:
                # Permanent — engine surfaces verbatim; no retry.
                raise
            except _TRANSIENT_STREAM_ERRORS as exc:
                last_exc = exc
                if stream_started_outside_retry:
                    # Already yielded text — salvage what we have rather
                    # than duplicate output by retrying.
                    logger.warning(f'[AGENT] LLM proxy stream truncated mid-flight ({type(exc).__name__}); salvaging')
                    break
                if attempt < LLM_MAX_ATTEMPTS - 1:
                    delay = _retry_backoff(attempt)
                    logger.warning(f'[AGENT] LLM proxy stream open failed ({type(exc).__name__}); attempt {attempt + 1}/{LLM_MAX_ATTEMPTS} — retrying in {delay}s')
                    await asyncio.sleep(delay)
                else:
                    logger.error(f'[AGENT] LLM proxy stream open failed on final attempt {attempt + 1}/{LLM_MAX_ATTEMPTS} ({type(exc).__name__}); giving up')

        if last_exc is not None and not any_text_yielded and not tc_buf:
            err_name = type(last_exc).__name__
            yield LLMStreamChunk(
                text=(
                    f"{_UNREACHABLE_SENTINEL_PROXY} {LLM_MAX_ATTEMPTS} attempts "
                    f"({err_name}). Please verify LLM_PROXY_URL and the model "
                    "endpoint are online, then try again.]"
                ),
                tool_calls=[],
                is_final=True,
            )
            return

        # Reconstruct buffered tool calls — drop unparseable args, same
        # policy as ``OpenAIClient.stream`` (line 606-628).
        final_tool_calls: List[ToolCall] = []
        for idx in sorted(tc_buf.keys()):
            d = tc_buf[idx]
            if not d["args_str"]:
                args: dict = {}
            else:
                try:
                    args = json.loads(d["args_str"])
                except json.JSONDecodeError:
                    logger.warning(f"[AGENT] Dropping tool call {d.get('name') or f'idx_{idx}'} with unparseable args (likely truncated mid-stream)")
                    continue
            final_tool_calls.append(ToolCall(
                id=d["id"] or f"call_{idx}",
                name=d["name"],
                args=args,
            ))

        # ── Observability: what did the LLM proxy return? ──
        _elapsed_ms = (time.perf_counter() - _t_send) * 1000
        _tool_names = [tc.name for tc in final_tool_calls]
        _usage_str = ""
        if final_usage:
            _usage_str = (
                f" tokens(in/out/total)="
                f"{final_usage.get('prompt_tokens', '?')}/"
                f"{final_usage.get('completion_tokens', '?')}/"
                f"{final_usage.get('total_tokens', '?')}"
            )
        _preview_str = ""
        if LLM_LOG_PREVIEW_CHARS and _resp_head:
            _preview_str = f" text={_preview(''.join(_resp_head))!r}"
        logger.info(
            f"[AGENT] ← LLM proxy response model={self._model} elapsed={_elapsed_ms:.0f}ms "
            f"finish={final_finish_reason or '<none>'} chars={_resp_chars} "
            f"tool_calls={_tool_names}{_usage_str}{_preview_str}"
        )

        yield LLMStreamChunk(
            text="",
            tool_calls=final_tool_calls,
            is_final=True,
            finish_reason=final_finish_reason,
            usage=final_usage,
            model=self._model,
        )


class GeminiProxyClient(OpenAIProxyClient):
    """Stream chat completions through the proxy's ``/llm/gemini-tools-stream``.

    The proxy converts Gemini's native response shape to OpenAI-compatible
    ``ChatCompletionChunk`` NDJSON server-side (services/llm_proxy/main.py
    ~line 929+), so the parsing logic is identical to ``OpenAIProxyClient`` —
    only the URL suffix changes.
    """

    _ENDPOINT_SUFFIX = "/llm/gemini-tools-stream"


# ---------------------------------------------------------------------------
# Anthropic-shape clients
# ---------------------------------------------------------------------------
# ``/llm/claude-tools-stream`` (services/llm_proxy/main.py:727) uses a compact
# NDJSON format distinct from the OpenAI one:
#   {"tbs": {index, id, name}}     — tool block start
#   {"tad": {index, partial_json}} — tool args delta
#   {"txt": {text}}                — text delta
#   {"stop": "stop"|"tool_calls", in_tok, out_tok}  — terminal
# Messages and tools must also be translated to Anthropic shape.

def _messages_to_anthropic(messages: List[Message]) -> tuple[str, List[dict]]:
    """Convert ABStudio's ``Message`` list into ``(system_text, messages_list)``
    in Anthropic format.

    System messages are extracted into the top-level ``system`` field
    (Anthropic doesn't accept ``role="system"`` inside ``messages``).
    Tool calls become ``tool_use`` content blocks on assistant turns;
    tool results become ``tool_result`` blocks on user turns — mirrors
    ``core/proxy_tool_use.py:149-153``.
    """
    system_text = ""
    out: List[dict] = []
    for msg in messages:
        if msg.role == "system":
            # Concatenate multiple system prompts in order — rare, but the
            # swarm orchestrator can emit nested directives.
            system_text = (
                f"{system_text}\n\n{msg.content}".strip()
                if system_text else (msg.content or "")
            )
            continue
        if msg.role == "user":
            out.append({"role": "user", "content": msg.content})
            continue
        if msg.role == "assistant":
            blocks: List[dict] = []
            if msg.content:
                blocks.append({"type": "text", "text": msg.content})
            for tc in (msg.tool_calls or []):
                blocks.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.args or {},
                })
            # If no blocks were produced (empty assistant turn), skip — an
            # empty assistant message would be rejected by Anthropic.
            if blocks:
                out.append({"role": "assistant", "content": blocks})
            continue
        if msg.role == "tool":
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content,
                }],
            })
            continue
    return system_text, out


def _tools_to_anthropic(tools: List[dict]) -> List[dict]:
    """Translate ABStudio's ``{name, description, parameters}`` tool spec
    (from ``to_function_spec`` at ``native_engine.py:602``) into Anthropic's
    ``{name, description, input_schema}`` shape.
    """
    out: List[dict] = []
    for t in tools:
        params = _clean_tool_schema(dict(t.get("parameters") or {}))
        out.append({
            "name": t["name"],
            "description": (t.get("description") or "")[:500],
            "input_schema": params,
        })
    return out


class ClaudeProxyClient(_BaseProxyClient):
    """Stream chat completions through the proxy's ``/llm/claude-tools-stream``.

    Used for any model classified as ``anthropic`` by ``_classify_model``
    (``claude-*``). Translates ABStudio's internal types to Anthropic's
    message + tool shape and parses the proxy's compact NDJSON
    (``tbs`` / ``tad`` / ``txt`` / ``stop`` lines — see
    ``services/llm_proxy/main.py:710-714``).
    """

    _ENDPOINT_SUFFIX = "/llm/claude-tools-stream"

    def _build_payload(
        self,
        messages: List[Message],
        tools: Optional[List[Dict]],
    ) -> dict:
        system_text, anth_messages = _messages_to_anthropic(messages)
        payload: dict = {
            "model": self._model,
            "messages": anth_messages,
            "tools": _tools_to_anthropic(tools) if tools else [],
            "system": system_text,
            "max_tokens": self._max_tokens,
        }
        return payload

    async def stream(
        self,
        messages: List[Message],
        tools: Optional[List[Dict]] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        # ``response_format`` is not forwarded by the Claude proxy endpoint.
        # Raise TypeError so the swarm orchestrator except-TypeError catch
        # block (orchestrator.py:992-1002) fires, sets
        # _gateway_supports_json_schema = False, and retries without the kwarg
        # - preventing silent cache-poisoning.
        if response_format is not None:
            raise TypeError("unexpected keyword argument 'response_format': ClaudeProxyClient forwards to the LLM proxy which does not support structured-output constraints. The caller should retry without response_format and use prompt-level JSON instructions.")

        payload = self._build_payload(messages, tools)
        url = self._endpoint_url()
        http = self._http_client()
        headers = self._proxy_headers()

        # ── Observability: what are we sending to the Claude proxy? ──
        _t_send = time.perf_counter()
        logger.info(
            f"[AGENT] → Claude proxy request model={self._model} max_tokens={self._max_tokens} "
            f"tools={len(tools or [])} url={url} {_messages_preview(messages)}"
        )
        logger.debug(f'[AGENT] … waiting for Claude proxy response model={self._model}')

        tc_buf: Dict[int, dict] = {}
        any_text_yielded = False
        final_finish_reason: str = ""
        final_usage: Optional[Dict[str, Any]] = None
        last_exc: Optional[BaseException] = None
        stream_started = False
        # Bounded response-text preview accumulator (see OpenAIClient.stream).
        _resp_head: List[str] = []
        _resp_head_len = 0
        _resp_chars = 0

        for attempt in range(LLM_MAX_ATTEMPTS):
            try:
                async with http.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        try:
                            err_body = (await resp.aread()).decode("utf-8", errors="replace")
                        except Exception:
                            err_body = ""
                        if _is_permanent_http_status(resp.status_code):
                            logger.error(f'[AGENT] Claude proxy {url} returned {resp.status_code} (permanent) body={err_body[:300]}')
                            raise _ProxyPermanentError(
                                f"{resp.status_code} from {url}: {err_body[:300]}"
                            )
                        resp.raise_for_status()

                    if attempt > 0:
                        logger.info(f'[AGENT] Claude proxy stream opened on attempt {attempt + 1}/{LLM_MAX_ATTEMPTS}')

                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if "tbs" in evt:
                            tbs = evt["tbs"]
                            idx = tbs.get("index", 0)
                            stream_started = True
                            tc_buf[idx] = {
                                "id": tbs.get("id", "") or f"call_{idx}",
                                "name": tbs.get("name", "") or "",
                                "args_str": "",
                            }
                            continue
                        if "tad" in evt:
                            tad = evt["tad"]
                            idx = tad.get("index", 0)
                            if idx not in tc_buf:
                                tc_buf[idx] = {"id": f"call_{idx}", "name": "", "args_str": ""}
                            tc_buf[idx]["args_str"] += tad.get("partial_json", "") or ""
                            continue
                        if "txt" in evt:
                            text = (evt["txt"] or {}).get("text", "")
                            if text:
                                stream_started = True
                                any_text_yielded = True
                                _resp_chars += len(text)
                                if LLM_LOG_PREVIEW_CHARS and _resp_head_len < LLM_LOG_PREVIEW_CHARS:
                                    _resp_head.append(text)
                                    _resp_head_len += len(text)
                                yield LLMStreamChunk(text=text)
                            continue
                        if "stop" in evt:
                            final_finish_reason = str(evt.get("stop") or "")
                            final_usage = _normalise_usage(
                                prompt_tokens=evt.get("in_tok") or evt.get("input_tokens") or 0,
                                completion_tokens=evt.get("out_tok") or evt.get("output_tokens") or 0,
                            ) or final_usage
                            # Don't break — wait for stream to end naturally
                            # so any trailing usage info is consumed.
                            continue
                        if "error" in evt:
                            raise _ProxyPermanentError(str(evt.get("error")))
                last_exc = None
                break
            except _ProxyPermanentError:
                raise
            except _TRANSIENT_STREAM_ERRORS as exc:
                last_exc = exc
                if stream_started:
                    logger.warning(f'[AGENT] Claude proxy stream truncated mid-flight ({type(exc).__name__}); salvaging')
                    break
                if attempt < LLM_MAX_ATTEMPTS - 1:
                    delay = _retry_backoff(attempt)
                    logger.warning(f'[AGENT] Claude proxy stream open failed ({type(exc).__name__}); attempt {attempt + 1}/{LLM_MAX_ATTEMPTS} — retrying in {delay}s')
                    await asyncio.sleep(delay)
                else:
                    logger.error(f'[AGENT] Claude proxy stream open failed on final attempt {attempt + 1}/{LLM_MAX_ATTEMPTS} ({type(exc).__name__}); giving up')

        if last_exc is not None and not any_text_yielded and not tc_buf:
            err_name = type(last_exc).__name__
            yield LLMStreamChunk(
                text=(
                    f"{_UNREACHABLE_SENTINEL_PROXY} {LLM_MAX_ATTEMPTS} attempts "
                    f"({err_name}). Please verify LLM_PROXY_URL and the Claude "
                    "gateway are online, then try again.]"
                ),
                tool_calls=[],
                is_final=True,
            )
            return

        final_tool_calls: List[ToolCall] = []
        for idx in sorted(tc_buf.keys()):
            d = tc_buf[idx]
            if not d["args_str"]:
                args: dict = {}
            else:
                try:
                    args = json.loads(d["args_str"])
                except json.JSONDecodeError:
                    logger.warning(f"[AGENT] Dropping Claude tool call {d.get('name') or f'idx_{idx}'} with unparseable args")
                    continue
            final_tool_calls.append(ToolCall(
                id=d["id"] or f"call_{idx}",
                name=d["name"],
                args=args,
            ))

        # ── Observability: what did the Claude proxy return? ──
        _elapsed_ms = (time.perf_counter() - _t_send) * 1000
        _tool_names = [tc.name for tc in final_tool_calls]
        _usage_str = ""
        if final_usage:
            _usage_str = (
                f" tokens(in/out/total)="
                f"{final_usage.get('prompt_tokens', '?')}/"
                f"{final_usage.get('completion_tokens', '?')}/"
                f"{final_usage.get('total_tokens', '?')}"
            )
        _preview_str = ""
        if LLM_LOG_PREVIEW_CHARS and _resp_head:
            _preview_str = f" text={_preview(''.join(_resp_head))!r}"
        logger.info(
            f"[AGENT] ← Claude proxy response model={self._model} elapsed={_elapsed_ms:.0f}ms "
            f"finish={final_finish_reason or '<none>'} chars={_resp_chars} "
            f"tool_calls={_tool_names}{_usage_str}{_preview_str}"
        )

        yield LLMStreamChunk(
            text="",
            tool_calls=final_tool_calls,
            is_final=True,
            finish_reason=final_finish_reason,
            usage=final_usage,
            model=self._model,
        )


def _resolve_direct_api_key(env_var: str, family: str) -> Optional[str]:
    """API key for calling a cloud provider directly (no llm_proxy microservice).

    ``env_var`` first (matches how the main platform gateway / CLI / Buddy
    resolve credentials — e.g. ``gateway_claude.py``), then the admin's "LLM
    Providers" registry, so ABStudio agrees with the rest of the platform on
    which key serves a given provider without requiring llm_proxy to be
    deployed. Returns ``None`` when neither source has a key.
    """
    key = os.getenv(env_var, "").strip()
    if key:
        return key
    try:
        from core.llm_provider_registry import resolve_credential_for_family
        return resolve_credential_for_family(family)
    except Exception as exc:
        logger.warning(f"[AGENT] llm_provider_registry unavailable while resolving "
                        f"a {family} credential: {exc}")
        return None


class ClaudeDirectClient(BaseLLMClient):
    """Calls the Anthropic API directly — no llm_proxy microservice required.

    The main platform gateway, CLI, and Buddy have always talked to Claude
    this way (see ``gateway_claude.py``). ABStudio historically only reached
    Claude through the separate llm_proxy microservice (``LLM_PROXY_URL``),
    which is an optional production deployment — nothing in this OSS
    quickstart runs it. When it's unset, ``_build_llm_client_for_model()``
    used to fall through to whatever ``LOCAL_LLM_BASE_URL`` was (Ollama in a
    typical dev setup) and send it a cloud model id, which 404s (missing the
    ``/v1`` prefix ``openai_compatible_base_url()`` doesn't normalise) and
    then silently degrades to an actual local model via the fallback client —
    so setting Claude as the admin default appeared to do nothing. This class
    is the direct-dispatch alternative, used as the primary path for
    anthropic-family models whenever ``LLM_PROXY_URL`` isn't configured.
    """

    def __init__(self, model: str, max_tokens: int = 4096, temperature: float = 0.7, top_p: float = 1.0):
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._top_p = top_p

    @staticmethod
    def has_credential() -> bool:
        return bool(_resolve_direct_api_key("ANTHROPIC_API_KEY", "anthropic"))

    async def stream(
        self,
        messages: List[Message],
        tools: Optional[List[Dict]] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        if response_format is not None:
            raise TypeError(
                "unexpected keyword argument 'response_format': the direct Anthropic "
                "client does not support structured-output constraints. The caller "
                "should retry without response_format and use prompt-level JSON "
                "instructions."
            )

        api_key = _resolve_direct_api_key("ANTHROPIC_API_KEY", "anthropic")
        if not api_key:
            yield LLMStreamChunk(
                text=(
                    f"{_UNREACHABLE_SENTINEL_DIRECT} no Anthropic credential configured. "
                    "Set ANTHROPIC_API_KEY, or add an Anthropic provider in "
                    "Admin → LLM Providers.]"
                ),
                is_final=True,
            )
            return

        import anthropic as _anthropic

        system_text, anth_messages = _messages_to_anthropic(messages)
        anth_tools = _tools_to_anthropic(tools) if tools else []

        kwargs: dict = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=anth_messages,
        )
        if system_text:
            kwargs["system"] = system_text
        if anth_tools:
            kwargs["tools"] = anth_tools
        # Some Claude models reject `temperature` outright — see
        # core.model_registry.models_without_temperature() for the
        # maintained default list (opus-5, sonnet-5, opus-4-7/4-8, ...),
        # shared with gateway_claude.py so both dispatch paths agree.
        from core.model_registry import models_without_temperature
        if not self._model.startswith(models_without_temperature()):
            kwargs["temperature"] = self._temperature

        logger.info(
            f"[AGENT] → Claude direct request model={self._model} max_tokens={self._max_tokens} "
            f"tools={len(anth_tools)} {_messages_preview(messages)}"
        )
        _t_send = time.perf_counter()
        _resp_chars = 0

        client = _anthropic.AsyncAnthropic(api_key=api_key)
        stream_cm = client.messages.stream(**kwargs)
        try:
            stream = await stream_cm.__aenter__()
        except _anthropic.BadRequestError as exc:
            # Defense-in-depth for a model not yet in
            # models_without_temperature()'s list: a 400 mentioning
            # `temperature` at this point (before any content has streamed)
            # is safe to retry once without the param.
            if "temperature" in kwargs and "temperature" in str(exc).lower():
                logger.warning(f"[AGENT] Claude direct model={self._model} rejected temperature — retrying without it")
                kwargs.pop("temperature", None)
                stream_cm = client.messages.stream(**kwargs)
                try:
                    stream = await stream_cm.__aenter__()
                except Exception as exc2:
                    logger.error(f"[AGENT] Claude direct request failed after temperature retry: {exc2}")
                    yield LLMStreamChunk(
                        text=f"{_UNREACHABLE_SENTINEL_DIRECT} Claude API error: {exc2}]",
                        is_final=True,
                    )
                    return
            else:
                logger.error(f"[AGENT] Claude direct request failed ({exc.status_code}): {exc}")
                yield LLMStreamChunk(
                    text=f"{_UNREACHABLE_SENTINEL_DIRECT} Claude API error {exc.status_code}: {exc}]",
                    is_final=True,
                )
                return
        except _anthropic.APIStatusError as exc:
            logger.error(f"[AGENT] Claude direct request failed ({exc.status_code}): {exc}")
            yield LLMStreamChunk(
                text=f"{_UNREACHABLE_SENTINEL_DIRECT} Claude API error {exc.status_code}: {exc}]",
                is_final=True,
            )
            return
        except Exception as exc:
            logger.error(f"[AGENT] Claude direct stream failed: {exc}")
            yield LLMStreamChunk(text=f"{_UNREACHABLE_SENTINEL_DIRECT} {exc}]", is_final=True)
            return

        try:
            async for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    _resp_chars += len(event.delta.text)
                    yield LLMStreamChunk(text=event.delta.text)
            # get_final_message() returns the fully-assembled message —
            # tool_use blocks arrive pre-parsed (block.input is already a
            # dict), so there's no need to hand-accumulate partial_json
            # deltas the way the proxy's NDJSON parser above has to.
            final = await stream.get_final_message()
        except _anthropic.APIStatusError as exc:
            logger.error(f"[AGENT] Claude direct request failed ({exc.status_code}): {exc}")
            yield LLMStreamChunk(
                text=f"{_UNREACHABLE_SENTINEL_DIRECT} Claude API error {exc.status_code}: {exc}]",
                is_final=True,
            )
            return
        except Exception as exc:
            logger.error(f"[AGENT] Claude direct stream failed: {exc}")
            yield LLMStreamChunk(text=f"{_UNREACHABLE_SENTINEL_DIRECT} {exc}]", is_final=True)
            return
        finally:
            await stream_cm.__aexit__(None, None, None)

        final_tool_calls: List[ToolCall] = [
            ToolCall(id=block.id, name=block.name, args=block.input or {})
            for block in final.content
            if block.type == "tool_use"
        ]
        finish_reason = {
            "end_turn": "stop", "max_tokens": "length",
            "tool_use": "tool_calls", "stop_sequence": "stop",
        }.get(final.stop_reason or "", final.stop_reason or "")
        usage = _normalise_usage(
            prompt_tokens=final.usage.input_tokens,
            completion_tokens=final.usage.output_tokens,
        )

        _elapsed_ms = (time.perf_counter() - _t_send) * 1000
        logger.info(
            f"[AGENT] ← Claude direct response model={self._model} elapsed={_elapsed_ms:.0f}ms "
            f"finish={finish_reason or '<none>'} chars={_resp_chars} "
            f"tool_calls={[tc.name for tc in final_tool_calls]}"
        )

        yield LLMStreamChunk(
            tool_calls=final_tool_calls,
            is_final=True,
            finish_reason=finish_reason,
            usage=usage,
            model=self._model,
        )


# ---------------------------------------------------------------------------
# Fallback model — Sonnet 4.6
# ---------------------------------------------------------------------------
# When the primary (user-selected) model fails — transient network errors on
# stream open, permanent 4xx from the proxy for that specific model, or an
# empty salvage (no text and no tool calls) — the request is transparently
# re-issued against a fallback model. The fallback follows the exact same
# routing rules as any other model:
#   claude-* → ClaudeProxyClient / LLM proxy /llm/claude-tools-stream
#   gpt-*    → OpenAIProxyClient  / LLM proxy /llm/openai-tools-stream
#   gemini-* → GeminiProxyClient  / LLM proxy /llm/gemini-tools-stream
#   local    → OpenAIClient       / LiteLLM direct (proxy bypassed)
# so the fallback obeys the local integration when no proxy is configured
# and the proxy integration when it is — identical to primary invocation.
#
# The identifier ``claude-sonnet-4-6`` matches the CLI's
# ``core.model_registry.CLAUDE_PRIMARY_MODEL`` (see
# ``app/api/generation.py:51`` and ``app/core/factory_utils.py:46``), the
# same value baked into every workflow_repo template default.

def resolve_fallback_model() -> str:
    """Resolve the auto-failover model at CALL time.

    Read lazily so a live ``.env`` fix (``ABSTUDIO_FALLBACK_LLM_MODEL``) takes
    effect on the next ``--reload`` without a full restart. This MUST be a
    cheap, reliably-available model — the historical hardcoded default
    (``claude-sonnet-4-6``) rejected requests with a 403 on any deployment
    that hadn't configured Anthropic, which surfaced as a confusing factory
    error whenever the primary call had a transient blip and the wrapper
    failed over. Resolution order: explicit env override → the admin's
    configured default in core.llm_provider_registry, preferring a free/
    self-hosted model → "" (the caller then classifies blank as "local" and
    resolves against whatever's enabled — see _build_llm_client_for_model).
    """
    explicit = os.getenv("ABSTUDIO_FALLBACK_LLM_MODEL", "").strip()
    if explicit:
        return explicit
    try:
        from core.llm_provider_registry import get_default_model_id
        return get_default_model_id(prefer_free=True) or ""
    except Exception as exc:
        logger.warning(f"[AGENT] llm_provider_registry unavailable while resolving "
                        f"the fallback model: {exc}")
        return ""


# Back-compat snapshot for importers; prefer ``resolve_fallback_model()``.
FALLBACK_LLM_MODEL = resolve_fallback_model()


def _same_model(a: str, b: str) -> bool:
    """Case-insensitive whitespace-tolerant model-id equality."""
    return (a or "").strip().lower() == (b or "").strip().lower()


class FallbackLLMClient(BaseLLMClient):
    """Wraps a primary ``BaseLLMClient`` and transparently retries the request
    on a fallback client (Sonnet 4.6 by default) when the primary either:

      * Raises a permanent error (``PERMANENT_LLM_ERRORS`` — 400/401/403/404
        from the proxy or the openai SDK). This is the CRITICAL case:
        the primary model is misconfigured / not in the catalogue, so
        retrying against the same model is guaranteed to fail. The fallback
        model is expected to be in the catalogue.
      * Raises a transient error (network drops, timeouts) AFTER the primary
        client already exhausted its own ``LLM_MAX_ATTEMPTS`` retry budget.
        In that case the primary client returns a sentinel ``[LLM unreachable
        after N attempts …]`` chunk with ``is_final=True`` — the fallback
        catches that and switches models.
      * Yields ONLY the sentinel chunk with no useful text or tool calls
        (empty salvage) — same signal as above but delivered as data instead
        of an exception.

    The fallback is a *distinct* client built via ``_build_llm_client_for_model``,
    so it follows the same LLM Proxy / local integration routing as any other
    model. When the primary is already the fallback model (case-insensitive),
    wrapping is skipped — the caller just gets the primary client verbatim.

    Streaming semantics: the fallback only kicks in when NO tokens have been
    yielded from the primary yet, or when the only chunk yielded was the
    sentinel ``[LLM unreachable …]`` message. Once real content has streamed
    to the caller we cannot retry without duplicating output, so we surface
    whatever the primary returned. This mirrors the same "don't retry after
    first yield" invariant already enforced inside ``OpenAIClient.stream``
    and the proxy clients.
    """

    # Prefix used by the primary clients when they exhaust their retry budget
    # (``OpenAIClient.stream`` line ~547 and the proxy clients ~874, ~1145).
    # Detecting this sentinel in the primary's output is the trigger for
    # switching to the fallback model when the primary returned "cleanly"
    # (i.e. yielded is_final=True) rather than raising.
    _PRIMARY_SENTINEL_PREFIXES = (
        "[LLM unreachable after",
        "[LLM proxy unreachable after",
    )

    def __init__(
        self,
        primary: BaseLLMClient,
        fallback_config: "LLMConfig",
        primary_model: str,
        fallback_model: str,
    ) -> None:
        self._primary = primary
        self._fallback_config = fallback_config
        self._primary_model = primary_model
        self._fallback_model = fallback_model
        # Lazy-built so a healthy primary never constructs the fallback
        # client (avoids opening a second httpx pool that will never be
        # used in the happy path).
        self._fallback: Optional[BaseLLMClient] = None

    def _build_fallback(self) -> BaseLLMClient:
        if self._fallback is None:
            # Build via the unwrapped factory so we don't accidentally nest
            # another FallbackLLMClient around the fallback (which would try
            # to fall back the fallback → infinite regress on a proxy outage).
            self._fallback = _build_llm_client_for_model(self._fallback_config)
        return self._fallback

    @classmethod
    def _looks_like_primary_sentinel(cls, text: str) -> bool:
        if not text:
            return False
        stripped = text.lstrip()
        return any(stripped.startswith(p) for p in cls._PRIMARY_SENTINEL_PREFIXES)

    async def complete_nonstream(
        self,
        messages: List[Message],
        *,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Forward a genuine non-streaming completion to the primary, falling
        back to the fallback model on a permanent error or an empty/sentinel
        body. Mirrors the fallback semantics of ``stream`` for the factory's
        non-streaming path.
        """
        try:
            text = await self._primary.complete_nonstream(
                messages, response_format=response_format,
            )
        except PERMANENT_LLM_ERRORS as exc:
            logger.warning(f'[AGENT] Primary LLM ({self._primary_model}) complete_nonstream permanent error ({type(exc).__name__}); falling back to {self._fallback_model}')
            return await self._build_fallback().complete_nonstream(
                messages, response_format=response_format,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f'[AGENT] Primary LLM ({self._primary_model}) complete_nonstream error ({type(exc).__name__}); falling back to {self._fallback_model}')
            return await self._build_fallback().complete_nonstream(
                messages, response_format=response_format,
            )
        # Empty or a primary "unreachable" sentinel → try the fallback model.
        if not (text or "").strip() or self._looks_like_primary_sentinel(text):
            logger.warning(f'[AGENT] Primary LLM ({self._primary_model}) complete_nonstream produced no usable content; falling back to {self._fallback_model}')
            return await self._build_fallback().complete_nonstream(
                messages, response_format=response_format,
            )
        return text

    async def stream(
        self,
        messages: List[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        # Buffer chunks from the primary until we either yield real content
        # (commit to primary) or determine we need to fall back.
        buffered: List[LLMStreamChunk] = []
        committed_to_primary = False
        real_content_seen = False
        primary_error: Optional[BaseException] = None
        # Human-readable cause of the switch, stamped onto the notice the
        # engine turns into a user-facing "using fallback model" message.
        fallback_reason = "primary model unavailable"

        try:
            async for chunk in self._primary.stream(
                messages, tools=tools, response_format=response_format
            ):
                # Retry-progress notices are live status signals, not model
                # output. Forward them immediately (so the user sees each
                # attempt) without committing to the primary — the fallback
                # must still engage if the primary ultimately fails.
                if chunk.notice and chunk.notice.get("kind") == "llm_retry":
                    yield chunk
                    continue
                # A chunk with real text or tool calls means the primary
                # produced usable output — commit and pass through.
                has_text = bool(chunk.text) and not self._looks_like_primary_sentinel(chunk.text)
                has_tools = bool(chunk.tool_calls)
                if has_text or has_tools:
                    # Flush any buffered pre-content chunks (empty deltas)
                    # before we start streaming.
                    if not committed_to_primary:
                        for b in buffered:
                            yield b
                        buffered = []
                        committed_to_primary = True
                    real_content_seen = True
                    yield chunk
                else:
                    # Empty / sentinel-only chunk. If we've already committed,
                    # forward it (final chunks with finish_reason still matter).
                    if committed_to_primary:
                        yield chunk
                    else:
                        buffered.append(chunk)
        except PERMANENT_LLM_ERRORS as exc:
            primary_error = exc
            fallback_reason = f"primary model returned a permanent error ({type(exc).__name__})"
            logger.warning(f'[AGENT] Primary LLM ({self._primary_model}) raised permanent error ({type(exc).__name__}); falling back to {self._fallback_model}')
        except _TRANSIENT_STREAM_ERRORS as exc:
            # Primary bubbled a transient error past its own retry budget
            # (rare — usually it returns a sentinel chunk instead). Only
            # safe to fall back if nothing real streamed yet.
            if committed_to_primary:
                logger.error(f'[AGENT] Primary LLM ({self._primary_model}) transient error AFTER content streamed ({type(exc).__name__}); cannot fall back without duplicating output — surfacing partial result')
                return
            primary_error = exc
            fallback_reason = f"primary model was unreachable ({type(exc).__name__})"
            logger.warning(f'[AGENT] Primary LLM ({self._primary_model}) transient error before any content ({type(exc).__name__}); falling back to {self._fallback_model}')

        # If we committed to the primary, we're done — either it succeeded
        # or it raised mid-stream and we surfaced the partial.
        if committed_to_primary and real_content_seen:
            return

        # Otherwise the primary produced no usable output (sentinel-only,
        # empty stream, or raised before first yield). Fall back to Sonnet.
        if primary_error is None:
            # The primary "cleanly" returned nothing useful — most commonly
            # the ``[LLM (proxy) unreachable after N attempts …]`` sentinel.
            # Log the salvage state so operators can distinguish primary
            # exhaustion from a truly empty prompt.
            sentinel_texts = [c.text for c in buffered if c.text]
            fallback_reason = "primary model exhausted its retry budget without responding"
            logger.warning(f"[AGENT] Primary LLM ({self._primary_model}) produced no usable content ({len(buffered)} buffered chunks, sentinel={(sentinel_texts[0][:120] if sentinel_texts else '')!r}); falling back to {self._fallback_model}")

        # Announce the switch IMMEDIATELY on its own empty chunk — before
        # building the fallback client or waiting on its first token — so the
        # user sees "switching to fallback…" live during processing rather
        # than only once the fallback's first token lands (which can lag on a
        # cold connection / slow first-token). The chunk carries no text/tool
        # calls, so it's a pure status signal.
        yield LLMStreamChunk(notice={
            "kind": "model_fallback",
            "primary_model": self._primary_model,
            "fallback_model": self._fallback_model,
            "reason": fallback_reason,
        })
        fallback = self._build_fallback()
        async for chunk in fallback.stream(
            messages, tools=tools, response_format=response_format
        ):
            # The switch was already announced above; fallback chunks flow
            # through unmodified so the user sees the fallback model's answer.
            yield chunk


def _make_fallback_config(primary: "LLMConfig") -> "LLMConfig":
    """Build the ``LLMConfig`` for the fallback client.

    Inherits ``max_tokens`` / ``temperature`` / ``top_p`` from the primary
    so behaviour stays consistent across the switch, but overrides the model
    name (and clears any primary-specific base_url / api_key so the fallback
    resolves its endpoint through the same env-driven precedence the factory
    uses for any other model).
    """
    return LLMConfig(
        provider=primary.provider,
        api_key="",  # let factory pick from env (proxy token or local key)
        model_name=resolve_fallback_model(),
        temperature=primary.temperature,
        max_tokens=primary.max_tokens,
        top_p=primary.top_p,
        base_url=None,  # resolved by factory routing (proxy vs LiteLLM)
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _local_llm_base_url() -> str:
    """Resolve the in-house Local LLM (LiteLLM) base URL from raw env vars.

    Order mirrors the CLI's ``gateway_local_llm.py:49``:
      LOCAL_LLM_BASE_URL → LITELLM_BASE_URL → OPENAI_COMPATIBLE_BASE_URL
    The OpenAI SDK expects the ``/v1`` suffix to already be present in
    ``base_url`` (it appends ``/chat/completions`` etc.), so we normalise.

    Critically we DO NOT call ``app.core.config.openai_compatible_base_url()``
    here — that helper prefers ``LLM_PROXY_URL`` and would route local
    traffic to the cloud proxy, which 404s on ``/v1/chat/completions``.
    """
    raw = (
        os.getenv("LOCAL_LLM_BASE_URL")
        or os.getenv("LITELLM_BASE_URL")
        or os.getenv("OPENAI_COMPATIBLE_BASE_URL")
        or "http://localhost:11434"
    ).rstrip("/")
    # Only append ``/v1`` when the URL path has no ``/v1`` segment at all
    # (e.g. a bare ``http://host:11434``). A naive ``endswith("/v1")`` check
    # mis-fires on URLs where ``/v1`` is an interior segment — e.g.
    # ``https://host/ainxt/v1/api`` would wrongly become ``.../v1/api/v1``.
    from urllib.parse import urlsplit
    path_segments = urlsplit(raw).path.strip("/").split("/")
    return raw if "v1" in path_segments else f"{raw}/v1"


def _local_llm_api_key() -> str:
    """Local LLM key from raw env (LiteLLM). Avoids the proxy-token fallback
    used by ``app.core.config.openai_compatible_api_key`` so local traffic
    doesn't pick up the cloud ``X-Internal-Token`` semantics.
    """
    return (
        os.getenv("LOCAL_LLM_API_KEY")
        or os.getenv("LITELLM_API_KEY")
        or os.getenv("OPENAI_COMPATIBLE_API_KEY")
        or "not-needed"
    )


def _build_llm_client_for_model(llm_config: LLMConfig) -> BaseLLMClient:
    """Return the right LLM client for the configured model (UNWRAPPED).

    This is the raw factory — it dispatches to ``OpenAIClient`` /
    ``OpenAIProxyClient`` / ``ClaudeProxyClient`` / ``GeminiProxyClient``
    based on the model family, but does NOT apply the Sonnet 4.6 fallback
    wrapper. The public entry point (``get_llm_client``) layers
    ``FallbackLLMClient`` on top of whatever this returns.

    Keeping this split lets ``FallbackLLMClient`` construct its fallback
    client via the same factory without recursing into another fallback
    layer (which would produce an infinite regress on a full proxy outage).

    Routing mirrors the CLI (gateway.py:6236-6321 for local-direct,
    gateway.py:6323+ for proxy-routed):
      * Cloud models (Claude / OpenAI / Gemini) → proxy
        ``/llm/{provider}-tools-stream`` via the new ProxyClient classes.
      * Local in-house models → LiteLLM at ``LOCAL_LLM_BASE_URL``
        DIRECTLY via the legacy ``OpenAIClient`` — proxy is bypassed
        because llm_proxy does not front the internal LLM cluster.

    NOTE on local routing: we **deliberately ignore** ``llm_config.base_url``
    and ``llm_config.api_key`` for local models. The engine's
    ``_extract_llm_config`` (native_engine.py:268) pre-fills blank fields
    with ``openai_compatible_base_url()`` which prefers
    ``{LLM_PROXY_URL}/v1`` — so by the time we get here, ``base_url`` may
    already be the proxy URL even for a local model selection. Reading
    raw env via ``_local_llm_base_url`` sidesteps that, matching how the
    CLI's ``gateway_local_llm`` resolves its endpoint.
    """
    model = (llm_config.model_name or "").strip()
    family = _classify_model(model)

    if family == "local":
        local_llm_token = _local_llm_api_key()
        local_model = model
        if not local_model:
            # Blank model_name reaching this point means every upstream caller
            # (factory_model(), an explicit node config, ...) had nothing to
            # offer — resolve against what's actually enabled rather than an
            # env var this deployment never set (LOCAL_LLM_MODEL, distinct
            # from the documented LOCAL_LLM_MODEL_NAME) or the literal
            # placeholder "local-llm", neither of which Ollama can serve
            # (see db/migrate.py's Part AC3 removing that exact bogus seed).
            try:
                from core.llm_provider_registry import get_enabled_models, get_default_model_id
                _local_models = [m for m in get_enabled_models() if m["family"] == "ollama"]
                local_model = _local_models[0]["model_id"] if _local_models else get_default_model_id(prefer_free=True)
            except Exception as exc:
                logger.warning(f"[LLM] llm_provider_registry unavailable while resolving a "
                                f"blank local model: {exc}")
                local_model = None
            local_model = local_model or os.getenv("LOCAL_LLM_MODEL_NAME", "").strip() or os.getenv("LOCAL_LLM_MODEL", "").strip()
            if not local_model:
                # Nothing enabled anywhere (no provider configured at all yet) —
                # sending model="" would just get a cryptic "model is required"
                # 400 back from Ollama/LiteLLM. Fail with a message an admin can
                # actually act on instead.
                raise RuntimeError(
                    "No LLM model is configured. Ask an admin to add and enable "
                    "at least one model in Admin → LLM Providers (and optionally "
                    "mark one as the default)."
                )
        return OpenAIClient(
            local_llm_token,
            base_url=_local_llm_base_url(),
            model=local_model,
            temperature=llm_config.temperature,
            max_tokens=llm_config.max_tokens,
            top_p=llm_config.top_p,
        )

    # Cloud — try the LLM proxy microservice first when configured (an
    # optional production deployment that isn't part of this OSS quickstart),
    # else dispatch directly to the provider using the same credential
    # resolution the main platform gateway/CLI/Buddy already use (env var,
    # else the admin's "LLM Providers" registry) — this needs no extra
    # service running, and is why Buddy/CLI were never affected by this bug.
    from app.core.config import llm_proxy_root as _llm_proxy_root
    common = dict(
        model=model,
        max_tokens=llm_config.max_tokens,
        temperature=llm_config.temperature,
        top_p=llm_config.top_p,
    )
    if _llm_proxy_root():
        if family == "anthropic":
            return ClaudeProxyClient(**common)
        if family == "gemini":
            return GeminiProxyClient(**common)
        return OpenAIProxyClient(**common)

    if family == "anthropic":
        if ClaudeDirectClient.has_credential():
            return ClaudeDirectClient(**common)
        # No proxy AND no Anthropic credential — fail loudly instead of the
        # old behavior of silently routing a Claude model id at whatever
        # LOCAL_LLM_BASE_URL happens to be (Ollama in a typical dev setup),
        # which 404s (missing /v1) and then quietly degrades to an actual
        # local model via FallbackLLMClient — making the admin's Claude
        # default appear to have no effect.
        raise RuntimeError(
            f"Cannot reach Claude for model '{model}': no LLM_PROXY_URL configured "
            "and no Anthropic credential found (ANTHROPIC_API_KEY, or an Anthropic "
            "provider in Admin → LLM Providers)."
        )

    if family == "gemini":
        gemini_key = _resolve_direct_api_key("GEMINI_API_KEY", "gemini")
        if gemini_key:
            return OpenAIClient(gemini_key, base_url="https://generativelanguage.googleapis.com/v1beta/openai/", **common)
        raise RuntimeError(
            f"Cannot reach Gemini for model '{model}': no LLM_PROXY_URL configured "
            "and no Gemini credential found (GEMINI_API_KEY, or a Gemini provider "
            "in Admin → LLM Providers)."
        )

    # family == "openai"
    openai_key = _resolve_direct_api_key("OPENAI_API_KEY", "openai")
    if openai_key:
        return OpenAIClient(openai_key, base_url="https://api.openai.com/v1", **common)
    raise RuntimeError(
        f"Cannot reach OpenAI for model '{model}': no LLM_PROXY_URL configured "
        "and no OpenAI credential found (OPENAI_API_KEY, or an OpenAI provider "
        "in Admin → LLM Providers)."
    )


def get_llm_client(llm_config: LLMConfig) -> BaseLLMClient:
    """Public factory — returns a fallback-aware LLM client.

    Wraps the primary client (built via ``_build_llm_client_for_model``)
    in a ``FallbackLLMClient`` so that any transient / permanent failure
    on the user-selected model is automatically re-routed to the fallback
    (``FALLBACK_LLM_MODEL`` = ``claude-sonnet-4-6``). The wrapper is
    transparent — it exposes the same ``BaseLLMClient`` interface
    (``stream`` / ``complete`` / ``complete_with_finish_reason``), so
    every existing call site (Build Studio Agent tab via native_engine,
    Build Studio Workflow tab via workflow_factory / factory_utils,
    swarm orchestrator, loop evaluator, agent-factory pipeline,
    generation.py's instruction / meta LLM calls) picks up the
    fallback behavior automatically.

    The fallback client is built via the SAME factory as the primary,
    so it follows the exact same LLM Proxy vs local integration routing
    already in place — no separate code path or proxy is introduced.

    When the selected model is already the fallback (i.e. the user
    picked Sonnet 4.6 directly), the wrapper is skipped so we don't add
    a redundant second attempt against the same endpoint on failure.
    """
    primary = _build_llm_client_for_model(llm_config)
    selected_model = (llm_config.model_name or "").strip()
    fallback_model = resolve_fallback_model()

    # If the user already selected the fallback model, wrapping adds no
    # value — the primary client's own retry budget is the ceiling.
    if not selected_model or _same_model(selected_model, fallback_model):
        return primary

    fallback_config = _make_fallback_config(llm_config)
    return FallbackLLMClient(
        primary=primary,
        fallback_config=fallback_config,
        primary_model=selected_model,
        fallback_model=fallback_model,
    )
