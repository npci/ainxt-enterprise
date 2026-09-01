# SPDX-License-Identifier: Apache-2.0
"""
Anthropic Messages API compatibility router for ainxt-cli.

Exposes POST /messages at the /ainxt/v1/api prefix so the Anthropic SDK
(pointed at baseURL=http://localhost:8000/ainxt/v1/api) routes all SDK calls
through the AiNxt gateway with full compliance, budget, and multi-model routing.

Provider routing (model hint → backend):
  Claude  (claude-*)       → LLM proxy /llm/claude-tools-stream   (Anthropic native tool_use)
  OpenAI  (gpt-*)          → LLM proxy /llm/openai-tools-stream   (OAI function calling)
  Gemini  (gemini-*)       → LLM proxy /llm/gemini-tools-stream   (Gemini function calling)
  In-house/Local (local-*) → model_router.stream()                 (text-only, no tool_use)

All four providers return Anthropic SSE format so the ainxt-cli SDK agent
loop works identically regardless of which model is selected.

Registration in gateway.py:
    from routers.messages_compat_router import router as messages_compat_router
    app.include_router(messages_compat_router, prefix="/ainxt/v1/api")
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, AsyncGenerator, Optional

import httpx
from auth.jwt_handler import decode_token
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# Structured logger — same pipeline used by every other router (ide_router,
# projects_router, chat_router, budget_router). Records emitted through this
# logger are automatically enriched by core/logger.py's _context_processor with
# service, host, request_id, span_id, user_id, chat_id, client_source, etc.
from core.config import PLATFORM_NAME as _PLATFORM_NAME

from core.logger import (
    logger,
    set_request_id,
    get_request_id,
    set_chat_context,
    set_span_id,
    set_client_source,
    set_correlation_id,
    clear_chat_context,
    clear_bound_context,
)
from core.gemini_protocol import GEMINI_THOUGHT_SIG_KEY
from agents.hardblock_engine import hardblock_engine

router = APIRouter(tags=["messages-compat"])

# Per-request conversation id (X-AiNxt-Conv-Id), set once in messages_endpoint()
# and read by _track_budget() for the Coach thread_id. Same contextvars pattern
# as core.logger's get_request_id()/set_request_id() — anyio's threadpool copies
# the calling context, so this is visible inside run_in_threadpool(_track_budget, ...)
# without needing conv_id threaded through every intermediate stream function.
_cv_conv_id: contextvars.ContextVar[str] = contextvars.ContextVar("ainxt_cli_conv_id", default="")

# ── Multilingual translate-in / translate-out (gateway-side wrapper) ──────────
# The CLI sends header `X-AiNxt-Target-Lang: <iso>`. We translate the user's
# prose INPUT to English (detection-driven) before the model, and translate the
# assistant's English prose OUTPUT back to the user's language. Code/identifiers/
# paths are NEVER translated (core.prose_translate guarantees it). Best-effort —
# degrades to English on any error so translation can never block a response.
try:
    from core.translation_wrapper import (
        translate_text as _xl_text,
        translate_from_english as _xl_from_en,
        is_supported as _xl_supported,
    )
    from core.lang_detect import detect_language as _xl_detect
    _XLAT_AVAILABLE = True
except Exception as _xl_e:  # pragma: no cover
    logger.warning(f"messages-compat: translation wrapper unavailable ({_xl_e}) — multilingual disabled")
    _XLAT_AVAILABLE = False


# ── Pooled httpx client for upstream LLM proxy calls ──────────────────────────
# Previously a fresh `httpx.AsyncClient` was created inside every request, which
# meant a brand-new TLS handshake to the LLM proxy (LLM_PROXY_URL in prod) on every
# single agent iteration. Inside the AiNxt office network, with TLS inspection on
# the egress path, that was ~200–500ms of wasted time per iteration.
#
# The pooled client keeps idle TLS connections alive between requests. First
# call pays the handshake; subsequent calls within the keep-alive window reuse
# the open socket — eliminating handshake latency from the iteration loop.
_proxy_client: Optional[httpx.AsyncClient] = None

def _get_proxy_client() -> httpx.AsyncClient:
    global _proxy_client
    if _proxy_client is None:
        _proxy_client = httpx.AsyncClient(
            # pool=10.0: a saturated pool fails fast (→ retry / clean error)
            # instead of hanging up to 600s while nginx waits out its 3600s
            # read timeout. read stays long (600s) for slow Opus generations.
            timeout=httpx.Timeout(600.0, connect=30.0, pool=10.0),
            limits=httpx.Limits(
                max_keepalive_connections=40,
                max_connections=100,
                # keepalive_expiry 30s (was 300s): never reuse a socket older
                # than the proxy side's idle-close window (nginx default 75s,
                # uvicorn default 5s). A 300s expiry guaranteed stale-socket
                # reuse → "Server disconnected without sending a response"
                # (httpx.RemoteProtocolError). The Step-3 retry is the safety
                # net; this removes most of the race at the source.
                keepalive_expiry=30.0,
            ),
        )
    return _proxy_client


# ── ARCH-F-CORE-003 (2026-08-26): circuit breaker on the LLM proxy link ──────
#
# The report's "4-hop synchronous chain" finding on this endpoint was already
# addressed (auth/budget/compliance are offloaded via run_in_threadpool above —
# see the calls in messages_endpoint()); what remained, per the report's own
# verdict, was "the circuit breaker on the LLM proxy connection". Verified
# there was none: none of _stream_claude(), _stream_oai_format(), or
# _stream_local_tools() wrapped their client.stream(...) call to
# LLM_PROXY_URL / LOCAL_LLM_BASE_URL in core/circuit_breaker (already used
# elsewhere for exactly this purpose — agents/sdlc_pipeline, connectors, and
# now connectors/engine.py per ARCH-F-007/008). Without it, a degraded/down
# LLM proxy meant every one of these three streaming paths independently ate
# a full connect timeout (30s) per CLI request before failing — for a burst
# of concurrent CLI sessions during an outage, that is many 30s hangs in a
# row with no fast-fail, unlike the connector paths which now short-circuit.
#
# Breaker key follows the model-hint's provider ("claude" | "openai" |
# "gemini" | "local_llm") so an outage on one provider's proxy path doesn't
# fast-fail a DIFFERENT provider's calls.
def _llm_proxy_breaker(provider: str):
    from core.circuit_breaker import get_breaker
    return get_breaker(f"llm_proxy_{provider}")


# ── Phase-timing logger ───────────────────────────────────────────────────────
# A single INFO line per request summarising where every millisecond went:
#   auth → budget → compliance → TLS+TTFB → stream → total
# This is what lets us distinguish "office network is slow" from "compliance
# regex is O(N²)" from "Squid is buffering" — without it, all three look
# identical from the outside.

def _log_phase_timings(
        user_id:    str,
        model_hint: str,
        msg_count:  int,
        tokens_in:  int,
        tokens_out: int,
        t:          dict[str, float],
) -> None:
    def d(a: str, b: str) -> str:
        if a in t and b in t:
            return f"{(t[b] - t[a]) * 1000:.0f}ms"
        return "-"
    try:
        logger.info(
            f"[CLI] timing user={user_id} model={model_hint} msgs={msg_count} "
            f"in_tok={tokens_in} out_tok={tokens_out} "
            f"auth={d('t_entry','t_auth')} budget={d('t_auth','t_budget')} "
            f"compliance={d('t_budget','t_compliance')} "
            f"ttfb={d('t_stream_start','t_first_byte')} "
            f"stream={d('t_first_byte','t_last_byte')} "
            f"total={d('t_entry','t_last_byte')}"
        )
    except Exception as e:
        logger.warning(f"[CLI] timing log failed: {e}")


# ── Models ────────────────────────────────────────────────────────────────────

class Message(BaseModel):
    role: str
    content: Any  # str | list[dict]


class MessagesRequest(BaseModel):
    model:           str            = ""
    messages:        list[Message]
    system:          Optional[Any]  = None
    tools:           Optional[list[dict]] = None
    max_tokens:      int            = Field(default=8192, ge=1, le=131072)
    stream:          bool           = False
    temperature:     Optional[float] = None
    top_p:           Optional[float] = None
    stop_sequences:  Optional[list[str]] = None
    metadata:        Optional[dict] = None


# ── Auth ──────────────────────────────────────────────────────────────────────

# Platform service tokens — opaque bearer values held ONLY in the
# AINXT_PLATFORM_SERVICE_API_KEYS env var (comma-separated). Any product that
# spawns the ainxt CLI on behalf of a real user (ABStudio, SDLC, …) presents
# one of these tokens instead of a real user's key. When it does, the request
# is authorised as a SYNTHETIC caller that has NO row in `users` and NO row
# in `user_api_keys`. Budget checks and audit writes are then skipped so the
# run is billed exactly ONCE — by the owning product, on its own request
# path — rather than twice (once by the product, once by the CLI leg here).
#
# A real human running `ainxt` on their laptop presents THEIR OWN key (from
# ~/.ainxt/config.json, minted via POST /profile/api-keys) — that value is
# not in the platform-service list, so the branch below never matches for
# them and their requests continue to be budgeted and audited as today.
_PLATFORM_SERVICE_USER_ID = "__platform_service__"


def _is_platform_service_token(token: str) -> bool:
    """Constant-time membership test against AINXT_PLATFORM_SERVICE_API_KEYS.

    Read at CALL TIME (not cached at import) so an operator can rotate the
    value without a process restart. Uses hmac.compare_digest per candidate
    so timing cannot leak which token (or its length) matched. Returns False
    on an empty list, so a deployment that never sets the env var is byte-
    identical to today — no branch below can match.
    """
    import hmac
    import os
    tok = (token or "").strip()
    if not tok:
        return False
    refs = [t.strip() for t in os.getenv("AINXT_PLATFORM_SERVICE_API_KEYS", "").split(",") if t.strip()]
    if not refs:
        return False
    # Iterate every candidate so total time is independent of which one
    # (if any) matched.
    matched = False
    for ref in refs:
        if hmac.compare_digest(tok, ref):
            matched = True
    return matched


def _resolve_user(request: Request) -> dict:
    """Anthropic SDK sends JWT as x-api-key; also accept Authorization: Bearer or API key."""
    candidates = [
        request.headers.get("x-api-key", "").strip(),
        request.headers.get("authorization", "")[7:].strip()
        if request.headers.get("authorization", "").startswith("Bearer ") else "",
    ]
    for token in candidates:
        if not token:
            continue
        # 0. Platform service token — matches AINXT_PLATFORM_SERVICE_API_KEYS
        #    env only. Returns a synthetic payload with a sentinel user_id
        #    that is intentionally NOT a UUID, so it cannot collide with any
        #    real user and cannot be inserted into UUID-typed columns.
        #    Downstream code (budget gate, _track_budget, prompt/response
        #    audits) uses this sentinel to skip work — see the guards later
        #    in this file.
        if _is_platform_service_token(token):
            return {
                "sub":         _PLATFORM_SERVICE_USER_ID,
                "user_id":     _PLATFORM_SERVICE_USER_ID,
                "role":        "service",
                "name":        "Platform Service",
                "email":       "",
                "ad_username": "",
                "department":  "",
            }
        # 1. Try JWT
        payload = decode_token(token)
        if payload:
            return payload
        # 2. Try platform API key (IDE integrations)
        try:
            from auth.api_key_auth import is_api_key as _is_api_key, resolve_api_key as _resolve_key
            if _is_api_key(token):
                kp = _resolve_key(token)
                if kp:
                    return kp
        except Exception:
            pass
    raise HTTPException(status_code=401, detail="Missing or invalid authentication token")


# ── Provider detection ────────────────────────────────────────────────────────

def _detect_provider(model: str) -> str:
    m = model.lower()
    if m.startswith("claude"):
        return "claude"
    # In-house check BEFORE the gpt-* prefix check: models like "gpt-oss-120b"
    # start with "gpt" but are served by the in-house Local LLM proxy, not
    # api.openai.com. _is_in_house_model() and _is_local_catalog_model() both
    # consult the live /v1/models catalog so any in-house model is caught here
    # regardless of its name prefix.
    if (m.startswith(("local", "ollama", "inhouse", "in-house"))
            or _is_in_house_model(m) or _is_local_catalog_model(m)):
        return "local"
    if m.startswith("gpt") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4"):
        return "openai"
    if m.startswith("gemini"):
        return "gemini"
    # "default"/blank/unknown → platform-primary Claude. The local tier is
    # Claude-primary policy and is often DISABLED (base URL unset) — routing an
    # unrecognised model there returns "Error generating response" on any non-trivial
    # prompt. The CLI sends model="default" when no explicit model is set, so this
    # is the common Cowork path.
    return "claude"


def _normalise_model(model: str) -> str:
    """
    Map CLI hint → canonical Anthropic/OpenAI/Gemini model ID.

    The hint is what the user selected in /model. It is sent from the CLI as
    the `model` field in the Anthropic SDK request. We normalise short aliases
    here; full model IDs pass through unchanged so the LLM proxy calls the
    exact model the user selected.
    """
    from core.model_registry import (
        CLAUDE_PRIMARY_MODEL, CLAUDE_OPUS_MODEL,
        CLAUDE_OPUS_48_MODEL, CLAUDE_OPUS_5_MODEL, CLAUDE_HAIKU,
        OPENAI_CODING_MODEL, OPENAI_SIMPLE_MODEL,
        OPENAI_LATEST_MODEL, GEMINI_VISION_MODEL,
        GEMINI_TEXT_MODEL, GEMINI_CODING_LITE_MODEL, GEMINI_IMAGE_MODEL,
        ENABLE_OPUS,
    )
    m = model.lower().strip()

    # ── Anthropic Claude ───────────────────────────────────────────────────────
    if m in ("claude", "sonnet", "complex", "claude-sonnet-4-6"):
        return CLAUDE_PRIMARY_MODEL
    # Opus hints — resolve to the concrete model ID (blocked models are rejected
    # by the caller immediately after _normalise_model() returns).
    if m in ("opus", "solution", "opus-4-7", "claude-opus-4-7"):
        return CLAUDE_OPUS_MODEL
    if m in ("opus-4-8", "claude-opus-4-8"):
        return CLAUDE_OPUS_48_MODEL
    if m in ("opus-5", "claude-opus-5"):
        return CLAUDE_OPUS_5_MODEL
    if m == "haiku":
        return CLAUDE_HAIKU

    # ── OpenAI ─────────────────────────────────────────────────────────────────
    if m in ("gpt", "gpt-5.4", "medium", "coding", "agents"):
        return OPENAI_CODING_MODEL
    if m in ("gpt-mini", "gpt-5-mini", "mini"):
        return OPENAI_SIMPLE_MODEL
    if m in ("gpt-5-5", "deep", "latest"):
        return OPENAI_LATEST_MODEL

    # ── Google ─────────────────────────────────────────────────────────────────
    if m in ("gemini-3.5-flash", "gemini-coding", "gemini-flash"):
        return GEMINI_TEXT_MODEL
    if m in ("gemini-3.1-flash-lite", "gemini-lite", "gemini-coding-lite"):
        return GEMINI_CODING_LITE_MODEL
    if m in ("gemini-3.1-flash-image", "gemini-image", "vision"):
        return GEMINI_IMAGE_MODEL
    # Generic "gemini" hint + legacy 2.x aliases → current Gemini default
    # (GEMINI_VISION_MODEL aliases to the image model so legacy /image and
    # vision-keyword routing continues to land on the image model).
    if m in ("gemini", "gemini-2.5-flash", "gemini-2.0-flash"):
        return GEMINI_VISION_MODEL

    # In-house / local models (live catalog or in-house name patterns) pass through
    # UNCHANGED so the local backend receives the exact id the user selected — never
    # remapped to Claude (which would both mis-route and bill the user).
    if _is_in_house_model(m) or _is_local_catalog_model(m):
        return model
    # Recognised full provider IDs pass through unchanged (e.g. "claude-sonnet-4-6").
    # Anything unrecognised ("default", blank, an in-house alias) defaults to the
    # platform-primary Claude model so the agent always reaches a WORKING model
    # instead of the disabled local tier (which returns "Error generating response").
    # Full provider IDs pass through unchanged — blocked ones are rejected by the
    # caller immediately after _normalise_model() returns.
    if m.startswith(("claude", "gpt", "o1", "o3", "o4", "gemini", "local", "ollama")):
        return model
    return CLAUDE_PRIMARY_MODEL


# ── System text ───────────────────────────────────────────────────────────────

_AINXT_IDENTITY = f"""You are AiNxt — the AiNxt Autonomous Agentic Engineering Platform.
You are NOT Claude, NOT ChatGPT, NOT Gemini, and NOT any other external AI product.
You are AiNxt, built exclusively for {_PLATFORM_NAME} engineers.
NEVER reveal your underlying model, provider, or any information about Anthropic, OpenAI, or Google.
If asked who you are: respond "I am AiNxt, AiNxt's internal AI engineering platform."
If asked what model powers you: respond "I'm AiNxt — the underlying model is confidential per AiNxt security policy."
If asked who made you: respond "AiNxt was built by the AiNxt platform engineering team."
You are an expert coding assistant for AiNxt engineers."""


def _system_text(system: Any) -> str:
    if not system:
        return _AINXT_IDENTITY
    if isinstance(system, str):
        # Prepend identity lock if not already present
        if "AiNxt" not in system:
            return f"{_AINXT_IDENTITY}\n\n{system}"
        return system
    if isinstance(system, list):
        text = "\n\n".join(
            b.get("text", "") for b in system
            if isinstance(b, dict) and b.get("type") == "text"
        )
        if not text:
            return _AINXT_IDENTITY
        if "AiNxt" not in text:
            return f"{_AINXT_IDENTITY}\n\n{text}"
        return text
    return _AINXT_IDENTITY


# ── Message serialisation ─────────────────────────────────────────────────────

def _serial_msgs(messages: list[Message]) -> list[dict]:
    out = []
    for m in messages:
        if isinstance(m.content, str):
            out.append({"role": m.role, "content": m.content})
        elif isinstance(m.content, list):
            out.append({"role": m.role, "content": [
                b if isinstance(b, dict) else b.model_dump(exclude_none=True)
                for b in m.content
            ]})
        else:
            out.append({"role": m.role, "content": str(m.content)})
    return out


# ── Anthropic tools → OpenAI tools ───────────────────────────────────────────

def _anthropic_tools_to_oai(tools: list[dict]) -> list[dict]:
    """Convert Anthropic tool schema (input_schema) → OpenAI function calling format."""
    oai = []
    for t in tools:
        oai.append({
            "type": "function",
            "function": {
                "name":        t.get("name", ""),
                "description": t.get("description", ""),
                "parameters":  t.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return oai


# ── Anthropic messages → OpenAI messages ─────────────────────────────────────

def _anthropic_msgs_to_oai(messages: list[dict], system: str) -> list[dict]:
    """Convert Anthropic-format messages (with tool_use / tool_result) → OpenAI format."""
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role    = m["role"]
        content = m["content"]

        if role == "assistant":
            if isinstance(content, list):
                text_parts = [b["text"] for b in content if b.get("type") == "text"]
                tool_calls = []
                for b in content:
                    if b.get("type") == "tool_use":
                        fn_obj: dict = {
                            "name":      b.get("name", ""),
                            "arguments": json.dumps(b.get("input", {})),
                        }
                        # Lift Gemini thought_signature back into the OAI function
                        # payload so the proxy's in-payload fallback works on the
                        # next turn (see core.gemini_protocol).
                        if sig := b.get(GEMINI_THOUGHT_SIG_KEY):
                            fn_obj[GEMINI_THOUGHT_SIG_KEY] = sig
                        tool_calls.append({
                            "id":       b.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                            "type":     "function",
                            "function": fn_obj,
                        })
                msg: dict = {"role": "assistant", "content": " ".join(text_parts) or None}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                out.append(msg)
            else:
                out.append({"role": "assistant", "content": content})

        elif role == "user":
            if isinstance(content, list):
                # May contain tool_result blocks
                tool_results = [b for b in content if b.get("type") == "tool_result"]
                text_blocks  = [b.get("text") or b.get("content", "") for b in content if b.get("type") == "text"]
                for tr in tool_results:
                    out.append({
                        "role":         "tool",
                        "tool_call_id": tr.get("tool_use_id", ""),
                        "content":      tr.get("content", "") if isinstance(tr.get("content"), str)
                        else json.dumps(tr.get("content", "")),
                    })
                if text_blocks:
                    out.append({"role": "user", "content": " ".join(text_blocks)})
            else:
                out.append({"role": "user", "content": content})
        else:
            out.append({"role": role, "content": content if isinstance(content, str) else json.dumps(content)})
    return out


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _sse(event: str, data: dict) -> str:
    # ensure_ascii=False → carry real UTF-8 (e.g. Tamil) on the wire instead of
    # \uXXXX escapes. SSE is UTF-8; the CLI json-parses either form identically,
    # but this keeps payloads small and debug/log output human-readable.
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_message_start(msg_id: str, model: str, input_tokens: int = 0) -> str:
    # input_tokens populated when the gateway has a pre-computed estimate.
    # Streaming paths (Claude/OpenAI/Gemini) get it from the upstream stop
    # event and override via message_delta.usage; this is the placeholder
    # the SDK seeds its running usage with at message_start.
    return _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "content": [], "model": model,
            "stop_reason": None, "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    })


# ── CLAUDE: proxy NDJSON (tbs/tad/txt/stop) → Anthropic SSE ─────────────────

async def _stream_claude(
        req: MessagesRequest,
        model_hint: str,
        system: str,
        serial_msgs: list[dict],
        user_id: str = "",
        timings: Optional[dict[str, float]] = None,
        request_id: str = "",
        source_channel: str = "CLI",
        conv_id: str = "",
) -> AsyncGenerator[str, None]:
    # Last-resort block: the handler already rejects blocked models before calling
    # _stream_claude(), but guard here too in case of direct internal calls.
    from core.model_registry import BLOCKED_MODELS
    if model_hint in BLOCKED_MODELS:
        logger.warning(
            f"[CLI] _stream_claude: blocked model reached stream layer model={model_hint} user={user_id}"
        )
        yield _sse("error", {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": f"Model '{model_hint}' is not available. Some models are disabled.",
            },
        })
        return

    proxy_url = os.getenv("LLM_PROXY_URL", "").rstrip("/")
    if not proxy_url:
        yield _sse("error", {"type": "error", "error": {"type": "api_error", "message": "LLM_PROXY_URL not configured"}})
        return

    # ARCH-F-CORE-003: fast-fail if the Claude proxy link is already known to
    # be down, rather than paying a full 30s connect timeout on every request
    # during an outage. See _llm_proxy_breaker() docstring for context.
    _breaker = _llm_proxy_breaker("claude")
    if _breaker.is_open:
        yield _sse("error", {
            "type": "error",
            "error": {"type": "api_error",
                      "message": "LLM proxy (Claude) is temporarily unavailable — too many recent "
                                 "failures. Please retry shortly."},
        })
        return

    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    yield _sse_message_start(msg_id, req.model)
    yield _sse("ping", {"type": "ping"})

    payload = {
        "model":      model_hint,
        "messages":   serial_msgs,
        "system":     system if system else _AINXT_IDENTITY,
        "max_tokens": req.max_tokens,
        "request_id": request_id or None,  # stitch request_id into the proxy for end-to-end log correlation
    }
    if req.tools:
        payload["tools"] = req.tools

    block_idx     = 0
    in_text_block = False
    in_tool_block = False
    in_tok = out_tok = 0
    cache_read_tok = cache_creation_tok = 0
    stop_reason   = "end_turn"

    if timings is not None:
        timings["t_stream_start"] = time.monotonic()

    # One retry if the proxy drops the connection BEFORE any response byte —
    # the signature of a stale keepalive socket (httpx.RemoteProtocolError
    # "Server disconnected without sending a response") or a transient connect
    # failure. Safe to re-send: no client-visible content emitted yet, so
    # nothing is duplicated. Never retried once streaming has started.
    _proxy_headers: dict[str, str] = {}
    if request_id:
        _proxy_headers["X-Request-ID"] = request_id
    if conv_id:
        _proxy_headers["X-AiNxt-Conv-Id"] = conv_id
    first_byte_seen = False
    for _attempt in range(2):
        try:
            client = _get_proxy_client()
            async with client.stream("POST", f"{proxy_url}/llm/claude-tools-stream", json=payload, headers=_proxy_headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not first_byte_seen:
                        if timings is not None:
                            timings["t_first_byte"] = time.monotonic()
                        first_byte_seen = True
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if "error" in ev:
                        if in_text_block or in_tool_block:
                            yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_idx})
                        _err_type = ev.get("error_type") or "api_error"
                        yield _sse("error", {"type": "error", "error": {"type": _err_type, "message": ev["error"]}})
                        return

                    elif "tbs" in ev:
                        if in_text_block:
                            yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_idx})
                            block_idx += 1
                            in_text_block = False
                        tbs = ev["tbs"]
                        block_idx = tbs["index"]
                        yield _sse("content_block_start", {
                            "type": "content_block_start", "index": block_idx,
                            "content_block": {"type": "tool_use", "id": tbs["id"], "name": tbs["name"], "input": {}},
                        })
                        in_tool_block = True
                        in_text_block = False

                    elif "tad" in ev:
                        tad = ev["tad"]
                        yield _sse("content_block_delta", {
                            "type": "content_block_delta", "index": tad["index"],
                            "delta": {"type": "input_json_delta", "partial_json": tad["partial_json"]},
                        })

                    elif "txt" in ev:
                        text = ev["txt"]["text"]
                        if in_tool_block:
                            yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_idx})
                            block_idx += 1
                            in_tool_block = False
                        if not in_text_block:
                            yield _sse("content_block_start", {
                                "type": "content_block_start", "index": block_idx,
                                "content_block": {"type": "text", "text": ""},
                            })
                            in_text_block = True
                        yield _sse("content_block_delta", {
                            "type": "content_block_delta", "index": block_idx,
                            "delta": {"type": "text_delta", "text": text},
                        })

                    elif "stop" in ev:
                        in_tok             = ev.get("in_tok",  0)
                        out_tok            = ev.get("out_tok", 0)
                        cache_read_tok     = ev.get("cache_read_tok",     0)
                        cache_creation_tok = ev.get("cache_creation_tok", 0)
                        stop_reason = "tool_use" if ev["stop"] == "tool_calls" else "end_turn"
            _breaker.record_success()
            break

        except (httpx.RemoteProtocolError, httpx.ConnectError) as e:
            if _attempt == 0 and not first_byte_seen:
                logger.warning(f"[messages-compat/claude] proxy dropped connection before response; retrying once: {e}")
                continue
            _breaker.record_failure(e)
            logger.error(f"[messages-compat/claude] error: {e}", exc_info=True)
            yield _sse("error", {"type": "error", "error": {"type": "api_error", "message": str(e)}})
            return
        except Exception as e:
            # Only record as breaker signal when nothing has streamed yet —
            # a mid-stream failure (e.g. the model itself erroring after
            # tokens were already emitted) reflects that specific generation,
            # not the proxy's health.
            if not first_byte_seen:
                _breaker.record_failure(e)
            logger.error(f"[messages-compat/claude] error: {e}", exc_info=True)
            yield _sse("error", {"type": "error", "error": {"type": "api_error", "message": str(e)}})
            return

    if timings is not None:
        timings["t_last_byte"] = time.monotonic()
        _log_phase_timings(user_id, model_hint, len(req.messages), in_tok, out_tok, timings)

    logger.info(
        f"[CLI] claude stream complete user={user_id} model={model_hint} "
        f"in_tok={in_tok} out_tok={out_tok} cache_read={cache_read_tok} "
        f"cache_creation={cache_creation_tok} "
        f"total_in_tok={in_tok + cache_read_tok + cache_creation_tok} stop_reason={stop_reason}"
    )

    if in_text_block or in_tool_block:
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_idx})
    # Price the call once here so the SAME number goes out on the wire
    # (`ainxt_cost_in_usd_ticks`) and into the budget ledger below — never
    # computed twice, so the CLI's display can never drift from what gets
    # debited from the user's budget.
    _cost_fields, _cost_usd = _cost_usage_extension(
        model_hint, in_tok, out_tok, cache_read_tok, cache_creation_tok,
    )
    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        # Anthropic's SSE protocol expects BOTH input_tokens and output_tokens
        # on the final message_delta. The CLI's `getTokenCountFromUsage()` sums
        # input_tokens + cache_* + output_tokens — emitting only output_tokens
        # leaves input_tokens=0 from message_start, so agent summaries show
        # severely undercounted (often "0 tokens") at task completion.
        # Cache counts are gated by TRACK_CACHE_TOKENS: when disabled, zeroed
        # out so the CLI binary's own cost display also excludes cache charges,
        # keeping server-side and CLI-side cost accounting consistent.
        # The `ainxt_*` keys are additive: the gateway is the only component
        # that prices a call, and the CLI renders those values verbatim.
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok,
                  "cache_creation_input_tokens": cache_creation_tok if TRACK_CACHE_TOKENS else 0,
                  "cache_read_input_tokens":     cache_read_tok     if TRACK_CACHE_TOKENS else 0,
                  **_cost_fields},
    })
    yield _sse("message_stop", {"type": "message_stop"})

    await run_in_threadpool(
        _track_budget, user_id, req.model, model_hint, in_tok, out_tok, timings, request_id,
        source_channel, cache_read_tok, cache_creation_tok, req, _cost_usd,
    )

# ── Shared: OpenAI-format chunk stream → Anthropic SSE ───────────────────────
# Consumes an async iterator of lines (either the LLM-proxy's bare-JSON NDJSON,
# or a raw OpenAI-compatible endpoint's `data: {...}` / `data: [DONE]` SSE) and
# yields Anthropic-protocol SSE strings: content_block_* for text + tool_use,
# then the closing message_delta (with usage) and message_stop. Reused by BOTH
# the cloud proxy path (_stream_oai_format) and the in-house local-tools path
# (_stream_local_tools) so tool_use handling is identical everywhere.

async def _oai_chunks_to_anthropic_sse(
        lines:      AsyncGenerator[str, None],
        req:        "MessagesRequest",
        model_hint: str,
        user_id:    str,
        timings:    Optional[dict[str, float]] = None,
        source_channel: str = "CLI",
) -> AsyncGenerator[str, None]:
    block_idx      = 0
    in_text_block  = False
    tool_blocks:   dict[int, dict] = {}   # index → {"id", "name", "args"}
    in_tok = out_tok = 0
    stop_reason    = "end_turn"
    first_byte_seen = False

    async for line in lines:
        if timings is not None and not first_byte_seen:
            timings["t_first_byte"] = time.monotonic()
            first_byte_seen = True
        s = line.strip()
        if not s:
            continue
        # Raw OpenAI SSE uses a `data: ` prefix + `[DONE]` sentinel; the proxy
        # NDJSON path has neither. Strip/normalise both so one parser serves both.
        if s.startswith("data:"):
            s = s[5:].strip()
        if s == "[DONE]":
            break
        try:
            ev = json.loads(s)
        except json.JSONDecodeError:
            continue

        # Proxy NDJSON "done" sentinel
        if ev.get("done"):
            break

        # Usage chunk (OpenAI emits this as the final chunk with include_usage)
        usage = ev.get("usage") or {}
        if usage.get("total_tokens"):
            in_tok  = usage.get("prompt_tokens", 0)
            out_tok = usage.get("completion_tokens", 0)
            continue

        choices = ev.get("choices", [])
        if not choices:
            continue

        choice = choices[0]
        delta  = choice.get("delta", {})
        finish = choice.get("finish_reason")

        # ── Text delta ──
        text_content = delta.get("content")
        if text_content:
            if not in_text_block:
                # Close any open tool blocks first, and advance
                # block_idx past them so the new text block gets a
                # fresh, non-colliding index. Without this advance,
                # a text block opened after tool_use reuses the
                # first tool's index → malformed Anthropic SSE
                # stream → CLI sees no final text.
                if tool_blocks:
                    for tidx in sorted(tool_blocks.keys()):
                        yield _sse("content_block_stop", {"type": "content_block_stop", "index": tidx})
                    block_idx = max(tool_blocks.keys()) + 1
                    tool_blocks.clear()
                yield _sse("content_block_start", {
                    "type": "content_block_start", "index": block_idx,
                    "content_block": {"type": "text", "text": ""},
                })
                in_text_block = True
            yield _sse("content_block_delta", {
                "type": "content_block_delta", "index": block_idx,
                "delta": {"type": "text_delta", "text": text_content},
            })

        # ── Tool call deltas ──
        for tc in (delta.get("tool_calls") or []):
            tidx = tc.get("index", 0)
            fn   = tc.get("function", {})
            # Normalize arguments to a string. OpenAI's spec requires
            # `function.arguments` to be a JSON-encoded string, but
            # some providers (and some SDK versions) emit it already
            # parsed as a dict/list. Anthropic SSE downstream expects
            # a string for `partial_json`, and the running accumulator
            # below is a string — so coerce here once.
            _args_raw = fn.get("arguments")
            if _args_raw is None:
                _args_str = ""
            elif isinstance(_args_raw, str):
                _args_str = _args_raw
            else:
                try:
                    _args_str = json.dumps(_args_raw)
                except (TypeError, ValueError):
                    _args_str = str(_args_raw)
            if tc.get("id"):  # Tool call start (has id + name)
                # Close text block if open
                if in_text_block:
                    yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_idx})
                    block_idx += 1
                    in_text_block = False
                tool_block_idx = block_idx + tidx
                tool_blocks[tool_block_idx] = {"id": tc["id"], "name": fn.get("name", ""), "args": ""}
                # Surface Gemini thought_signature on the tool_use
                # block so the CLI round-trips it back to us next turn
                # (see core.gemini_protocol).
                content_block: dict = {
                    "type": "tool_use",
                    "id":   tc["id"],
                    "name": fn.get("name", ""),
                    "input": {},
                }
                if sig := fn.get(GEMINI_THOUGHT_SIG_KEY):
                    content_block[GEMINI_THOUGHT_SIG_KEY] = sig
                yield _sse("content_block_start", {
                    "type": "content_block_start", "index": tool_block_idx,
                    "content_block": content_block,
                })
                # Start chunks may also carry the initial args (some
                # providers — including our Gemini proxy — emit the
                # full args on the start chunk when delivery is
                # atomic). Forward as a delta so the consumer gets
                # them and our accumulator stays consistent.
                if _args_str:
                    if tool_block_idx in tool_blocks:
                        tool_blocks[tool_block_idx]["args"] += _args_str
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta", "index": tool_block_idx,
                        "delta": {"type": "input_json_delta", "partial_json": _args_str},
                    })
            elif _args_str:  # Args delta
                tool_block_idx = block_idx + tidx
                if tool_block_idx in tool_blocks:
                    tool_blocks[tool_block_idx]["args"] += fn["arguments"]
                yield _sse("content_block_delta", {
                    "type": "content_block_delta", "index": tool_block_idx,
                    "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                })

        if finish:
            stop_reason = "tool_use" if finish == "tool_calls" else "end_turn"

    if timings is not None:
        timings["t_last_byte"] = time.monotonic()
        _log_phase_timings(user_id, model_hint, len(req.messages), in_tok, out_tok, timings)

    # Close remaining open blocks
    if in_text_block:
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_idx})
    for tidx in sorted(tool_blocks.keys()):
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": tidx})
    # Priced once here so the wire figure and the budget debit below are the
    # same number (see _stream_claude for the identical pattern).
    _cost_fields, _cost_usd = _cost_usage_extension(model_hint, in_tok, out_tok)

    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        # Anthropic's SSE protocol expects BOTH input_tokens and output_tokens on
        # the final message_delta — emitting only output leaves the CLI's token
        # tally undercounted (input_tokens=0 from message_start).
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok,
                  "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0,
                  **_cost_fields},
    })
    yield _sse("message_stop", {"type": "message_stop"})
    await run_in_threadpool(
        _track_budget, user_id, req.model, model_hint, in_tok, out_tok, timings, get_request_id(),
        source_channel, 0, 0, req, _cost_usd,
    )

# ── OPENAI / GEMINI: OpenAI-format NDJSON chunks → Anthropic SSE ─────────────

async def _stream_oai_format(
        req: MessagesRequest,
        model_hint: str,
        system: str,
        serial_msgs: list[dict],
        provider: str,         # "openai" | "gemini"
        user_id: str = "",
        timings: Optional[dict[str, float]] = None,
        request_id: str = "",
        source_channel: str = "CLI",
        conv_id: str = "",
) -> AsyncGenerator[str, None]:
    proxy_url = os.getenv("LLM_PROXY_URL", "").rstrip("/")
    if not proxy_url:
        yield _sse("error", {"type": "error", "error": {"type": "api_error", "message": "LLM_PROXY_URL not configured"}})
        return

    # ARCH-F-CORE-003: fast-fail if this provider's proxy link is already
    # known to be down, rather than paying a full connect timeout on every
    # request during an outage. See _llm_proxy_breaker() docstring.
    _breaker = _llm_proxy_breaker(provider)
    if _breaker.is_open:
        yield _sse("error", {
            "type": "error",
            "error": {"type": "api_error",
                      "message": f"LLM proxy ({provider}) is temporarily unavailable — too many "
                                 "recent failures. Please retry shortly."},
        })
        return

    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    yield _sse_message_start(msg_id, req.model)
    yield _sse("ping", {"type": "ping"})

    # Convert Anthropic format → OpenAI format for the proxy
    oai_msgs  = _anthropic_msgs_to_oai(serial_msgs, system)
    oai_tools = _anthropic_tools_to_oai(req.tools) if req.tools else None

    endpoint = "/llm/openai-tools-stream" if provider == "openai" else "/llm/gemini-tools-stream"
    payload: dict = {
        "model":      model_hint,
        "messages":   oai_msgs,
        "max_tokens": req.max_tokens,
        "request_id": request_id or None,  # stitch request_id into the proxy for end-to-end log correlation
    }
    if oai_tools:
        payload["tools"] = oai_tools

    if timings is not None:
        timings["t_stream_start"] = time.monotonic()

    # One retry if the proxy drops the connection BEFORE any response byte —
    # the signature of a stale keepalive socket (httpx.RemoteProtocolError) or
    # a transient connect failure. Safe to re-send: nothing client-visible has
    # been emitted yet. Never retried once streaming has started.
    _proxy_headers: dict[str, str] = {}
    if request_id:
        _proxy_headers["X-Request-ID"] = request_id
    if conv_id:
        _proxy_headers["X-AiNxt-Conv-Id"] = conv_id
    committed = False
    last_error: Optional[Exception] = None
    for _attempt in range(2):
        try:
            client = _get_proxy_client()
            async with client.stream(
                    "POST", f"{proxy_url}{endpoint}",
                    json=payload, headers=_proxy_headers) as resp:
                resp.raise_for_status()
                async for sse in _oai_chunks_to_anthropic_sse(
                        resp.aiter_lines(), req, model_hint, user_id, timings,
                        source_channel=source_channel,
                ):
                    committed = True
                    yield sse
            _breaker.record_success()
            return
        except Exception as e:
            last_error = e
            if committed:
                # Mid-stream failure: a retry would duplicate emitted content.
                # Not recorded as breaker signal — the proxy connection itself
                # succeeded; this reflects the specific generation, not proxy health.
                break
            logger.warning(
                f"[messages-compat/{provider}] pre-stream failure on attempt "
                f"{_attempt + 1}/2 ({type(e).__name__}: {e})"
                + ("; retrying" if _attempt == 0 else "; giving up")
            )
            if _attempt == 1:  # exhausted retries — this counts as a real failure
                _breaker.record_failure(e)

    logger.error(
        f"[messages-compat/{provider}] error: {last_error}", exc_info=last_error
    )
    yield _sse("error", {
        "type": "error",
        "error": {"type": "api_error", "message": str(last_error)},
    })
    # `message_start` + `ping` were already emitted above, so the message is
    # open on the wire. Anthropic SSE requires message_delta + message_stop to
    # close it — without them a streaming client waits for a terminator that
    # never arrives (the CLI then sits in "waiting for response" until its idle
    # timeout).
    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        # A pre-stream failure never reached the model, so it is a firm $0 —
        # not "unknown" — the same way an in-house call is: the gateway knows
        # for certain nothing billable happened.
        "usage": {"input_tokens": 0, "output_tokens": 0,
                  "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0,
                  "ainxt_cost_in_usd_ticks": 0, "ainxt_cost_priced": True},
    })
    yield _sse("message_stop", {"type": "message_stop"})
    return

# ── LOCAL / IN-HOUSE: tool-calling (OpenAI-compatible) → Anthropic SSE ───────
# In-house GPU models (Kimi, Qwen, GLM, …) are served OpenAI-compatibly at
# {LOCAL_LLM_BASE_URL}/v1/chat/completions. Most modern ones support function
# calling, so we forward the CLI's tools and reuse the same OAI→Anthropic
# converter as the cloud path. If a given model rejects `tools`, we raise
# _LocalToolsUnsupported BEFORE emitting any SSE so the caller can transparently
# fall back to the legacy text-only path.

class _LocalToolsUnsupported(Exception):
    """Local endpoint cannot do tool calling for this request → fall back to text."""


# ── Auto-compaction ───────────────────────────────────────────────────────────
# When the assembled conversation exceeds COMPACTION_THRESHOLD_FRACTION of the
# model's context window, the oldest turns are replaced by a single LLM-generated
# rolling summary so the request stays within budget. Fires before provider
# dispatch, so it applies to Claude, OpenAI, Gemini, and local alike.
_COMPACTION_THRESHOLD_FRACTION: float = float(
    os.getenv("COMPACTION_THRESHOLD_FRACTION", "0.70")
)
# Fix 5: local/in-house models have smaller effective output budgets because
# tool schemas consume a large fraction of the context window. Compact earlier
# (default 0.60 = 60%) so the model always has headroom for tool calls and
# reasoning. Override with LOCAL_COMPACTION_THRESHOLD_FRACTION.
_LOCAL_COMPACTION_THRESHOLD_FRACTION: float = float(
    os.getenv("LOCAL_COMPACTION_THRESHOLD_FRACTION", "0.60")
)
# Number of most-recent messages kept verbatim after compaction.
_COMPACTION_KEEP_TURNS: int = int(os.getenv("COMPACTION_KEEP_TURNS", "10"))
# The summarisation call uses the SAME model the user requested (model_hint
# passed into _compact_messages). This is always safe because compaction fires
# at COMPACTION_THRESHOLD_FRACTION (0.70 cloud / 0.60 local) of that model's
# own context window, so the head (total minus the last COMPACTION_KEEP_TURNS
# messages) is always smaller than the window that triggered compaction — it
# fits by construction. The previous design routed the summary to a separate
# "simple" hint that resolved to the in-house gpt-oss-120b (128K window),
# which was SMALLER than the target model's window (e.g. Claude 200K) and
# rejected the head with HTTP 400
# ("Input length exceeds model's maximum context length 131072"), forcing a
# cloud fallback on every compaction. Using the input model itself eliminates
# the window mismatch and keeps summarisation on the user's chosen provider.


def _summarise_via_model(prompt: str, model_hint: str) -> str:
    """Synchronous summarisation call — intended to be run via asyncio.to_thread().

    Uses the same model the user requested (model_hint) so the summariser's
    context window always matches the model that triggered compaction — no
    smaller-window intermediary, no window-mismatch 400. Returns '' on any
    error so the caller can fall back to the original messages.
    """
    try:
        from models.model_router import model_router
        return model_router.generate(prompt, model_hint=model_hint).strip()
    except Exception as _e:
        logger.warning(f"[messages-compat] _summarise_via_model failed: {_e}")
        return ""


async def _compact_messages(
    system: str,
    serial_msgs: list[dict],
    model_hint: str,
    local_model: bool = False,
) -> list[dict]:
    """Auto-compact the message list when it exceeds the context threshold.

    Strategy — rolling summary injection:
      1. Estimate total tokens (system + all messages) via char/4 heuristic.
      2. If estimated > window * threshold_fraction, split messages into:
           head = everything except the last COMPACTION_KEEP_TURNS messages
           tail = last COMPACTION_KEEP_TURNS messages (kept verbatim)
      3. Summarise `head` via the SAME model the user requested (model_hint).
      4. Return [summary_message] + tail.

    `local_model=True` uses a lower threshold fraction (LOCAL_COMPACTION_THRESHOLD_FRACTION,
    default 0.60) so in-house models compact earlier and always have headroom for
    tool schemas and output tokens.

    Always returns the original serial_msgs unchanged on any error — compaction
    must never block or corrupt the user's request.
    """
    try:
        from gateway import _context_window_for
        window = _context_window_for(model_hint)
    except Exception:
        window = 128_000  # conservative default if gateway import fails

    # Fix 5: use a tighter threshold for local/in-house models.
    threshold_fraction = (
        _LOCAL_COMPACTION_THRESHOLD_FRACTION if local_model
        else _COMPACTION_THRESHOLD_FRACTION
    )

    # Estimate total tokens: system text + all message content
    _content_parts = [system] if system else []
    for _m in serial_msgs:
        _c = _m.get("content", "")
        _content_parts.append(_c if isinstance(_c, str) else json.dumps(_c))
    estimated = max(1, len("\n".join(_content_parts)) // 4)
    threshold = int(window * threshold_fraction)

    if estimated <= threshold:
        return serial_msgs  # well within budget — nothing to do

    logger.info(
        f"[messages-compat] auto-compaction triggered: "
        f"estimated={estimated} tokens > threshold={threshold} "
        f"(window={window}, fraction={threshold_fraction}, local={local_model}, "
        f"msgs={len(serial_msgs)}, keep_tail={_COMPACTION_KEEP_TURNS})"
    )

    # Split: keep the most recent turns verbatim, summarise the rest
    if len(serial_msgs) <= _COMPACTION_KEEP_TURNS:
        # All messages fit in the tail — nothing old enough to summarise.
        # This can happen when a single huge message fills the window.
        logger.warning(
            f"[messages-compat] compaction: only {len(serial_msgs)} msgs, "
            f"all within keep_tail={_COMPACTION_KEEP_TURNS} — skipping"
        )
        return serial_msgs

    tail = serial_msgs[-_COMPACTION_KEEP_TURNS:]
    head = serial_msgs[:-_COMPACTION_KEEP_TURNS]

    # Build the summarisation prompt from the head turns
    _history_lines = []
    for _m in head:
        _role = _m.get("role", "user").capitalize()
        _c = _m.get("content", "")
        _text = _c if isinstance(_c, str) else json.dumps(_c)
        # Truncate individual messages to 2000 chars to keep the summary prompt
        # itself from overflowing the summariser's context window.
        if len(_text) > 2000:
            _text = _text[:2000] + " …[truncated]"
        _history_lines.append(f"[{_role}]: {_text}")

    _summarise_prompt = (
        "You are a conversation summarizer for an AI coding assistant. "
        "Produce a concise but complete summary of the following conversation history. "
        "Preserve: decisions made, files changed, errors encountered, key facts, "
        "code snippets that were agreed upon, and any explicit user preferences. "
        "Omit greetings and filler. Write in third-person past tense. "
        "Maximum 600 words.\n\n"
        "<conversation>\n"
        + "\n\n".join(_history_lines)
        + "\n</conversation>"
    )

    try:
        summary = await asyncio.to_thread(_summarise_via_model, _summarise_prompt, model_hint)
    except Exception as _e:
        logger.warning(
            f"[messages-compat] compaction summarisation failed ({_e}) "
            f"— proceeding with original {len(serial_msgs)} messages"
        )
        return serial_msgs

    if not summary:
        logger.warning(
            "[messages-compat] compaction returned empty summary "
            "— proceeding with original messages"
        )
        return serial_msgs

    summary_msg: dict = {
        "role": "assistant",
        "content": (
            f"[Conversation summary — {len(head)} earlier turns compacted]\n{summary}"
        ),
    }
    compacted = [summary_msg] + tail
    logger.info(
        f"[messages-compat] compaction complete: "
        f"{len(serial_msgs)} msgs → {len(compacted)} msgs "
        f"(summary_chars={len(summary)}, tail_kept={len(tail)})"
    )
    return compacted


# ── Pre-flight context window guard ──────────────────────────────────────────
# Fraction of the model's context window we allow before refusing to dispatch.
# 0.90 leaves 10% headroom for the model's own overhead and the output budget.
# Used by BOTH the local-only tool-calling guard below AND the provider-
# agnostic guard in messages_endpoint() (Claude/OpenAI/Gemini/local alike) —
# one knob controls how conservative every pre-flight check is.
_LOCAL_CONTEXT_SAFETY_FRACTION: float = float(
    os.getenv("CONTEXT_SAFETY_FRACTION", os.getenv("LOCAL_CONTEXT_SAFETY_FRACTION", "0.90"))
)


def _estimate_prompt_tokens(prompt: str) -> int:
    """Char/4 token estimate — same heuristic used by the in_tok counter below."""
    return max(1, len(prompt) // 4)


def _estimate_messages_tokens(system: str, serial_msgs: list[dict]) -> int:
    """Char/4 token estimate across system + all messages.

    Mirrors the heuristic in `_compact_messages()` so the numbers reported by
    the auto-compaction trigger and this pre-flight guard always agree.
    """
    _parts = [system] if system else []
    for _m in serial_msgs:
        _c = _m.get("content", "")
        _parts.append(_c if isinstance(_c, str) else json.dumps(_c))
    return max(1, len("\n".join(_parts)) // 4)


def _context_preflight_exceeded(
        system: str, serial_msgs: list[dict], model_hint: str,
) -> Optional[tuple[int, int]]:
    """Return (estimated_tokens, context_window) when the assembled turn would
    exceed the target model's safe context budget, else None.

    Provider-agnostic: applies identically to Claude, OpenAI, Gemini, and
    local — this runs BEFORE any provider is dispatched, so an oversized turn
    never reaches an upstream API and never surfaces as a raw 400 or a
    generic "Error generating response". Runs AFTER auto-compaction, so a
    conversation that compaction already fixed is not blocked here — only a
    turn that is still too big (e.g. one huge attachment/diff pasted in a
    single message) trips this guard.
    """
    try:
        from gateway import _context_window_for
        limit = _context_window_for(model_hint)
    except Exception:
        limit = 128_000  # conservative default if gateway import fails
    estimated = _estimate_messages_tokens(system, serial_msgs)
    safe_limit = int(limit * _LOCAL_CONTEXT_SAFETY_FRACTION)
    if estimated > safe_limit:
        return estimated, limit
    return None


_CONTEXT_TOO_LONG_MESSAGE = (
    "\n\nThis conversation has grown too long for the selected model's "
    "context window. Please start a new chat or ask me to summarize the "
    "discussion so far."
)


async def _context_window_exceeded_stream(
        req: "MessagesRequest",
        message_text: str = _CONTEXT_TOO_LONG_MESSAGE,
) -> AsyncGenerator[str, None]:
    """Emit one complete Anthropic-format SSE turn carrying a friendly
    plain-text message, then close cleanly.

    Used by the provider-agnostic pre-flight context-window guard so a
    request that would exceed the target model's context window fails
    closed — before any bytes are sent to Claude/OpenAI/Gemini/local — with
    an actionable message instead of a raw 400 or generic error. Consumed
    identically by both the streaming (StreamingResponse) and non-streaming
    (_collect_stream_to_message) paths, same as any provider generator.
    """
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    yield _sse_message_start(msg_id, req.model)
    yield _sse("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    })
    yield _sse("content_block_delta", {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": message_text},
    })
    yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        # Rejected before dispatch — the model never saw the prompt, so this
        # is a firm, known $0 rather than "unpriced".
        "usage": {"input_tokens": 0, "output_tokens": 0,
                  "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0,
                  "ainxt_cost_in_usd_ticks": 0, "ainxt_cost_priced": True},
    })
    yield _sse("message_stop", {"type": "message_stop"})


def _check_local_context_window(
        prompt: str,
        model_hint: str,
        tools: Optional[list] = None,
) -> None:
    """Raise _LocalToolsUnsupported (with 'context' in the message) when the
    assembled prompt is estimated to exceed the local model's safe token budget.

    This converts a hard HTTP 400 ContextWindowExceededError from vLLM into a
    clean Python exception *before* any bytes are sent, so the caller's existing
    fallback machinery can handle it gracefully. Kept as a local-specific
    backstop (it estimates from the fully-rendered local text prompt, which
    includes framing overhead the generic guard above does not see) even
    though the generic `_context_preflight_exceeded()` guard in
    messages_endpoint() now catches most oversized turns earlier for every
    provider, local included.

    `tools` — the Anthropic-format tool list from the request. Tool schemas are
    serialised to JSON and their token cost is added to the estimate. Without
    this, 20 tools × ~300 tokens ≈ 6 k tokens are invisible to the guard and
    the model silently runs out of output budget after a few turns.
    """
    try:
        from gateway import _context_window_for
        limit = _context_window_for(model_hint)
    except Exception:
        limit = 128_000  # conservative default if gateway import fails
    # Base estimate from the rendered prompt text.
    estimated = _estimate_prompt_tokens(prompt)
    # Add tool-schema overhead. Each schema is serialised to JSON (the same
    # representation sent to the model) and counted at char/4 tokens.
    if tools:
        tool_overhead = sum(len(json.dumps(t)) for t in tools) // 4
        estimated += tool_overhead
    else:
        tool_overhead = 0
    safe_limit = int(limit * _LOCAL_CONTEXT_SAFETY_FRACTION)
    if estimated > safe_limit:
        logger.warning(
            f"[messages-compat/local] pre-flight context guard: "
            f"estimated={estimated} tokens (prompt={estimated - tool_overhead}, "
            f"tool_schemas={tool_overhead}) > safe_limit={safe_limit} "
            f"(window={limit}, fraction={_LOCAL_CONTEXT_SAFETY_FRACTION}, "
            f"model={model_hint!r}) — skipping local dispatch"
        )
        raise _LocalToolsUnsupported(
            f"context window exceeded: estimated {estimated} tokens exceeds "
            f"safe limit {safe_limit} (model context={limit}, hint={model_hint!r})"
        )


def _local_tools_enabled() -> bool:
    # On by default; one kill-switch (LOCAL_LLM_TOOLS=false) disables for all.
    try:
        from gateway_local_llm import LOCAL_LLM_BASE_URL
    except Exception:
        return False
    if not LOCAL_LLM_BASE_URL:
        return False
    return os.getenv("LOCAL_LLM_TOOLS", "true").strip().lower() not in ("false", "0", "no")

def _resolve_local_model(model_hint: str) -> str:
    """
    Map the CLI hint to a CONCRETE in-house model id the endpoint actually serves.
    A hint may be a real model id (pass through), a tier alias, or the generic
    "local" selector — the last two must resolve to a real model (the same way the
    text path's model_router does) or the endpoint 400s on an unknown model name.
    """
    try:
        from gateway_local_llm import _catalog
        all_models = _catalog.all_models()
        if model_hint in all_models:
            return model_hint
        if model_hint in ("simple", "medium", "complex"):
            return _catalog.pick(model_hint) or (all_models[0] if all_models else model_hint)
        # "local" / unknown alias → prefer a mid-tier real model for agent work.
        return (_catalog.pick("medium") or _catalog.pick("simple")
                or (all_models[0] if all_models else model_hint))
    except Exception:
        return model_hint

async def _stream_local_tools(
        req: MessagesRequest,
        model_hint: str,
        system: str,
        serial_msgs: list[dict],
        user_id: str = "",
        source_channel: str = "CLI",
        request_id: str = "",
        conv_id: str = "",
        timings: Optional[dict[str, float]] = None,
) -> AsyncGenerator[str, None]:
    """
    Stream a tool-calling turn from the in-house OpenAI-compatible endpoint.
    Raises _LocalToolsUnsupported (before any yield) if the endpoint rejects the
    request, so _stream_local can fall back to text-only. Any error AFTER the
    first SSE is emitted is surfaced as an Anthropic error event (no fallback).
    """
    from gateway_local_llm import LOCAL_LLM_BASE_URL, LOCAL_LLM_API_KEY
    base = (LOCAL_LLM_BASE_URL or "").rstrip("/")
    if not base:
        raise _LocalToolsUnsupported("LOCAL_LLM_BASE_URL not configured")

    payload: dict = {
        "model":       _resolve_local_model(model_hint),
        "messages":    _anthropic_msgs_to_oai(serial_msgs, system),
        "tools":       _anthropic_tools_to_oai(req.tools),
        "max_tokens":  req.max_tokens,
        "temperature": req.temperature if req.temperature is not None else 0.3,
        "stream":      True,
        "stream_options": {"include_usage": True},
    }
    headers: dict[str, str] = {}
    if LOCAL_LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LOCAL_LLM_API_KEY}"
    if request_id:
        headers["X-Request-ID"] = request_id  # stitch request_id for end-to-end log correlation
    if conv_id:
        headers["X-AiNxt-Conv-Id"] = conv_id
    url = f"{base}/v1/chat/completions"

    # ARCH-F-CORE-003: fast-fail if the local-LLM endpoint is already known to
    # be down. Raises _LocalToolsUnsupported (same contract as any other
    # pre-stream failure here) so the caller falls back to text-only via
    # _stream_local() instead of hanging on a connect attempt.
    _breaker = _llm_proxy_breaker("local_llm")
    if _breaker.is_open:
        raise _LocalToolsUnsupported("local LLM endpoint circuit breaker is OPEN — too many recent failures")

    client = _get_proxy_client()
    cm = client.stream("POST", url, json=payload, headers=headers)
    try:
        resp = await cm.__aenter__()
    except Exception as e:
        _breaker.record_failure(e)
        raise _LocalToolsUnsupported(f"pre-stream error: {e}")
    committed = False
    try:
        if resp.status_code >= 400:
            body = await resp.aread()
            # An HTTP error response IS a successful connection — record success
            # so a model rejecting tool-calling (e.g. 400) never trips the breaker.
            _breaker.record_success()
            raise _LocalToolsUnsupported(f"HTTP {resp.status_code}: {body[:200]!r}")

        msg_id = f"msg_{uuid.uuid4().hex[:24]}"
        yield _sse_message_start(msg_id, req.model)
        yield _sse("ping", {"type": "ping"})
        committed = True
        _breaker.record_success()
        async for sse in _oai_chunks_to_anthropic_sse(resp.aiter_lines(), req, model_hint, user_id, timings,
                                                      source_channel=source_channel):
            yield sse
    except _LocalToolsUnsupported:
        raise
    except Exception as e:
        if not committed:
            # Pre-commit failure (connect/TLS/etc.) → fall back to text-only.
            _breaker.record_failure(e)
            raise _LocalToolsUnsupported(f"pre-stream error: {e}")
        logger.error(f"[messages-compat/local-tools] mid-stream error: {e}", exc_info=True)
        yield _sse("error", {"type": "error", "error": {"type": "api_error", "message": str(e)}})
    finally:
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            pass


# ── LOCAL / IN-HOUSE: text streaming → Anthropic SSE (no tool_use) ───────────

async def _stream_local(
        req: MessagesRequest,
        model_hint: str,
        system: str,
        serial_msgs: list[dict],
        user_id: str = "",
        timings: Optional[dict[str, float]] = None,
        request_id: str = "",
        precleared_findings: Optional[list[dict]] = None,
        source_channel: str = "CLI",
        conv_id: str = "",
) -> AsyncGenerator[str, None]:
    """
    In-house GPU models (Kimi, Qwen, GLM, Ollama, etc.) are text-only.
    We stream via model_router and wrap as Anthropic text SSE.
    The CLI agent sees stop_reason=end_turn with only text content,
    then falls back to the legacy text-parsing path for EDIT blocks.

    precleared_findings: forwarded into model_router.stream so the OpenAI/
    Gemini fallback gateways (reached when local LLM is unavailable, or when
    the complexity classifier escalates an unmapped model hint to a cloud
    tier) skip their second-pass block decision and only redact. The first-
    pass gate in messages_endpoint() already cleared the prompt.
    """
    if req.tools and _local_tools_enabled():
        try:
            async for sse in _stream_local_tools(req, model_hint, system, serial_msgs, user_id):
                yield sse
            return
        except _LocalToolsUnsupported as e:
            logger.warning(
                f"[messages-compat/local] {model_hint} tools unavailable ({e}); "
                f"falling back to text-only"
            )
            # fall through to the text-only path (nothing has been yielded yet)

    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    yield _sse_message_start(msg_id, req.model)
    yield _sse("ping", {"type": "ping"})
    yield _sse("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    })

    # Build a simple text prompt from messages
    prompt_parts = []
    if system:
        prompt_parts.append(f"[System]\n{system}")
    for m in serial_msgs:
        role = m["role"]
        content = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"])
        prompt_parts.append(f"[{role.capitalize()}]\n{content}")
    prompt = "\n\n".join(prompt_parts)

    if timings is not None:
        timings["t_stream_start"] = time.monotonic()

    # Rough input-token estimate for local models — char/4 is the standard
    # heuristic when the model doesn't return a tokenizer count. Keeps the
    # final message_delta.usage.input_tokens non-zero so the CLI's agent
    # token tally shows real numbers.
    in_tok = max(1, len(prompt) // 4)
    out_tok = 0
    try:
        from models.model_router import model_router as _mr
        first_byte_seen = False
        # Resolve the in-house model id: strip a "local:" addressing prefix; a bare
        # "local"/"ollama"/"inhouse" hint means "router picks the default local model".
        _lid = model_hint.split(":", 1)[1] if model_hint.lower().startswith("local:") else model_hint
        _local_model = None if _lid.lower() in ("local", "ollama", "inhouse", "in-house", "") else _lid
        # model_hint="local" maps to the local (TIER_SIMPLE) backend in the router;
        # local_model pins the specific in-house model the user selected. Passing the
        # raw id as model_hint would NOT route local (it isn't in the router's hint map).
        for tok in _mr.stream(
                prompt,
                model_hint=model_hint,
                precleared=True,
                precleared_findings=list(precleared_findings or []),
        ):
            # Skip the {"__stream_meta__": {...}} sentinel — see
            # model_router.stream() docstring. Only string tokens are
            # forwarded to the messages-compat caller.
            if isinstance(tok, dict):
                continue
            if tok:
                if timings is not None and not first_byte_seen:
                    timings["t_first_byte"] = time.monotonic()
                    first_byte_seen = True
                out_tok += 1
                yield _sse("content_block_delta", {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": tok},
                })
    except Exception as e:
        logger.error(f"[CLI] local stream error user={user_id} model={model_hint}: {e}", exc_info=True)
        yield _sse("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": f"\n[Model error: {e}]"},
        })

    if timings is not None:
        timings["t_last_byte"] = time.monotonic()

    yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    # In-house model — priced at a firm $0 (not "unknown"), since self-hosted hardware
    # carries no per-token billing. Computed via the shared helper anyway so the
    # `ainxt_cost_priced` flag is set the same way as every other path.
    _cost_fields, _cost_usd = _cost_usage_extension(model_hint, in_tok, out_tok)

    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        # Anthropic's SSE protocol expects BOTH input_tokens and output_tokens
        # on the final message_delta. The CLI's `getTokenCountFromUsage()` sums
        # input_tokens + cache_* + output_tokens — emitting only output_tokens
        # leaves input_tokens=0 from message_start, so agent summaries show
        # severely undercounted (often "0 tokens") at task completion.
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok,
                  "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0,
                  **_cost_fields},
    })
    yield _sse("message_stop", {"type": "message_stop"})
    if timings is not None:
        _log_phase_timings(user_id, model_hint, len(req.messages), in_tok, out_tok, timings)
    logger.info(
        f"[CLI] local stream complete user={user_id} model={model_hint} "
        f"in_tok={in_tok} out_tok={out_tok}"
    )
    # In-house model — cost stays $0 inside _track_budget, but we still
    # record the token count for audit/quota tracking.
    await run_in_threadpool(
        _track_budget, user_id, req.model, model_hint, in_tok, out_tok, timings, request_id,
        source_channel, 0, 0, req, _cost_usd,
    )


# ── Budget helper ─────────────────────────────────────────────────────────────

# Models hosted on AiNxt's internal GPU box — no per-token billing. Mirrors the
# regex set in src/utils/modelCost.ts on the CLI side so cost displays agree.
_IN_HOUSE_MODEL_PATTERNS = (
    "local",
    "kimi",
    "qwen",
    "glm",
    "ollama",
    "llama",
    "mistral",
    "deepseek",
    "gemma",
    "phi",
    "gpt-oss",   # in-house OSS model served at LOCAL_LLM_BASE_URL, not api.openai.com
)


def _is_in_house_model(model: str) -> bool:
    """True for AiNxt-hosted models that should never debit a user's budget."""
    m = (model or "").lower().strip()
    for pat in _IN_HOUSE_MODEL_PATTERNS:
        # Match the bare hint ("local", "kimi", "ollama", …) — this is what the
        # CLI sends when the user picks an in-house model in /model — as well as
        # versioned variants ("local-1", "qwen_72b", "ollama:llama3", …).
        if m == pat or m.startswith(f"{pat}-") or m.startswith(f"{pat}_") or m.startswith(f"{pat}:") or m.startswith(f"{pat}/"):
            return True
    if m.endswith("-local") or m.endswith("-inhouse") or m.endswith("-in-house") or m.endswith("-internal"):
        return True
    # Dynamic: any model the in-house Local LLM proxy actually serves (live
    # /v1/models catalog) — covers in-house models whose names don't match the
    # static patterns above. Non-blocking: reads the cached catalog, never forces
    # a network fetch on the request/event-loop path.
    try:
        from gateway_local_llm import is_local_model as _is_local_catalog
        if _is_local_catalog(model):
            return True
    except Exception:
        pass
    return False

def _is_local_catalog_model(model: str) -> bool:
    """True if the model id is one the in-house endpoint actually serves (e.g.
    'llama3.1:8b', 'kimi-2.6') — even when its name matches no in-house pattern.
    Lets the user pick a local model by its real name and still route to local."""
    try:
        from gateway_local_llm import _catalog
        m = (model or "").strip().lower()
        return any((mid or "").lower() == m for mid in _catalog.all_models())
    except Exception:
        return False

# Anthropic prompt-cache billing ratios — mirrors gateway_claude._CACHE_READ_RATIO /
# _CACHE_WRITE_RATIO (the source of truth for the [CACHE EFFECTIVENESS] log line).
# cache_read tokens are billed at 10% of the input rate; cache_creation tokens
# (writing a new cache entry) are billed at 125% of the input rate. Without
# this, cache-heavy Claude CLI turns (where `in_tok` is near-zero because most
# of the prompt was served from cache) were being billed near $0 even though
# Anthropic charged for tens of thousands of cache tokens.
_CLAUDE_CACHE_READ_RATIO  = 0.10
_CLAUDE_CACHE_WRITE_RATIO = 1.25

# Set to "false" / "0" / "no" to stop billing AND persisting Claude
# prompt-cache token counts (cache_read_tok / cache_creation_tok) in
# _compute_cost_usd() and _persist_model_usage_async()/_bg_write(). Lets
# ops revert to the pre-cache-billing behaviour (cost ignores cache tokens,
# `cache_read_tokens` / `cache_write_tokens` columns stay 0) without a
# code rollback. Defaults to disabled.
TRACK_CACHE_TOKENS = os.getenv("TRACK_CACHE_TOKENS", "false").strip().lower() not in ("false", "0", "no")

# Ticks-per-USD scaling for the `ainxt_cost_in_usd_ticks` wire extension —
# MUST match `ainxt_chat_state::pricing::COST_TICKS_PER_USD` /
# `ainxt_pager::views::context_bar::COST_TICKS_PER_USD` on the CLI side, since
# the CLI stores and displays this value verbatim (integer ticks, not floats,
# so thousands of per-call sums never drift the way float addition would).
_COST_USD_TICKS_PER_DOLLAR = 10_000_000_000  # 1 USD = 1e10 ticks


def _usd_to_cost_ticks(cost_usd: float) -> int:
    """Convert a USD amount to integer `ainxt_cost_in_usd_ticks`, clamped at 0
    so a rounding artifact can never emit a negative (bogus credit) tick count."""
    return max(0, round(cost_usd * _COST_USD_TICKS_PER_DOLLAR))


def _compute_cost_usd(
        model:              str,
        in_tok:             int,
        out_tok:            int,
        cache_read_tok:     int = 0,
        cache_creation_tok: int = 0,
) -> tuple[float, bool]:
    """
    Resolve per-1M-token rates for `model` from the central registry and
    return `(cost_usd, priced)`. In-house GPU models are billed at $0.00 —
    they run on AiNxt hardware and have no per-token billing — but we still
    emit token counts for audit/telemetry.

    `priced` is `True` whenever this function reached a real, deliberate
    figure (including the genuine $0.00 for in-house models) and `False`
    only when the calculation itself blew up (rate lookup/import failure).
    The CLI's `ainxt_cost_priced` wire extension mirrors this exactly: a
    gateway that computed *something*, even $0 for an unrecognized model
    name, is authoritative and must never be re-estimated by the client —
    only a genuine failure here should leave the CLI to fall back to its
    own published-rate estimate.

    `cache_read_tok` / `cache_creation_tok` are Claude-only fields (Anthropic's
    `input_tokens` excludes them — they are separate, non-overlapping token
    buckets). When present, they are billed at Anthropic's documented cache
    rates instead of being silently dropped from the cost calculation.
    """
    inhouse = _is_in_house_model(model)
    if inhouse:
        logger.info(f"[CLI] In house model used - model = {model} in_house={inhouse}")
        return 0.0, True
    try:
        from core.model_registry import MODEL_COST_PER_1M
        rates = MODEL_COST_PER_1M.get(model, (0.0, 0.0))
        input_rate = rates[0]
        cost = (in_tok / 1_000_000) * input_rate + (out_tok / 1_000_000) * rates[1]
        if TRACK_CACHE_TOKENS:
            if cache_read_tok:
                cost += (cache_read_tok / 1_000_000) * input_rate * _CLAUDE_CACHE_READ_RATIO
            if cache_creation_tok:
                cost += (cache_creation_tok / 1_000_000) * input_rate * _CLAUDE_CACHE_WRITE_RATIO
        logger.info(
            f"[CLI] model = {model} in_house={inhouse} in_tok={in_tok} out_tok={out_tok} "
            f"cache_read={cache_read_tok} cache_creation={cache_creation_tok} cost={cost}"
        )
        return cost, True
    except Exception as e:
        logger.warning(f"[CLI] cost calc failed for model={model}: {e}")
        return 0.0, False


def _cost_usage_extension(
        model:              str,
        in_tok:             int,
        out_tok:            int,
        cache_read_tok:     int = 0,
        cache_creation_tok: int = 0,
) -> tuple[dict, float]:
    """
    Compute this call's cost and return `(extension_fields, cost_usd)`.

    `extension_fields` is merged straight into the outgoing `message_delta`
    `usage` dict as the `ainxt_cost_in_usd_ticks` / `ainxt_cost_priced`
    additions the CLI's Messages-API client already parses (see
    `ainxt_sampling_types::messages::MessageDeltaUsage`). `cost_usd` is
    returned alongside so callers can hand the SAME figure to `_track_budget`
    instead of recomputing it a second time — keeping the number the CLI
    displays and the number the budget ledger debits byte-for-byte identical.
    """
    cost_usd, priced = _compute_cost_usd(model, in_tok, out_tok, cache_read_tok, cache_creation_tok)
    fields = {
        "ainxt_cost_in_usd_ticks": _usd_to_cost_ticks(cost_usd) if priced else None,
        "ainxt_cost_priced":       priced,
    }
    return fields, cost_usd


def _persist_model_usage_async(
        user_id:            str,
        model:              str,
        in_tok:             int,
        out_tok:            int,
        cost_usd:           float,
        latency_ms:         float,
        request_id:         str,
        source_channel:     str = "CLI",
        cache_read_tok:     int = 0,
        cache_creation_tok: int = 0,
) -> None:
    """
    Publish one `model_usages` audit event for this CLI request onto the
    `ainxt.metrics` Kafka topic — the same topic and event shape gateway.py
    already produces to for the `/ask` and `/v1/chat/completions` paths (see
    `_kafka_produce("ainxt.metrics", ...)` there). workers/kafka_consumer.py's
    `_handle_metrics` bulk-inserts these events into `model_usages`, and falls
    back to a Redis-backed queue (drained on consumer startup) when Kafka is
    unreachable, so this event is never silently lost the way a raw
    fire-and-forget Postgres write could be. `endpoint="/v1/messages"` tags CLI
    traffic in the same audit surface as `/ask` and `/v1/chat/completions`.

    `source_channel` defaults to "CLI" (plain terminal / API traffic). The
    Cowork "Buddy" desktop drives this same endpoint but announces itself via
    the `x-ainxt-surface: cowork` header — messages_endpoint() resolves that to
    "BUDDY" and threads it down here so Buddy usage is reported as its own
    channel in the utilization pie charts / chargeback instead of being lumped
    in with raw CLI. agent_id mirrors the channel (lowercased) for the audit row.

    `cache_read_tok` / `cache_creation_tok` (Claude only) populate the event's
    `cache_read_tokens` / `cache_write_tokens` fields, which `_handle_metrics`
    maps onto the `model_usages` columns of the same name — previously always
    0 for CLI/Buddy traffic even when a request was almost entirely
    cache-served.

    Dispatched on a daemon thread (fire-and-forget) so producing to Kafka (or
    its Redis fallback) never delays the final SSE chunk reaching the client.
    """
    def _bg_write() -> None:
        try:
            from core.kafka_producer import produce, TOPIC_METRICS
            from core.time_utils import now_ist_iso as _now_ist_iso_cli_mu
            # Resolve "auto"/"default" to the platform default model so every
            # model_usages row carries a real, queryable model ID.
            _resolved_model = model
            if not _resolved_model or _resolved_model.strip().lower() in ("auto", "default", ""):
                from core.model_registry import CLAUDE_PRIMARY_MODEL
                _resolved_model = CLAUDE_PRIMARY_MODEL
            _sent_to_kafka = produce(TOPIC_METRICS, {
                "event":              "llm_cost",
                "request_id":         request_id or None,
                "user_id":            user_id or None,
                "agent_id":           (source_channel or "CLI").lower(),
                "endpoint":           "/v1/messages",
                "source_channel":     source_channel or "CLI",
                "model":              _resolved_model,
                "input_tokens":       in_tok,
                "output_tokens":      out_tok,
                "total_tokens":       in_tok + out_tok,
                "latency_ms":         latency_ms,
                "cost_usd":           cost_usd,
                "cache_read_tokens":  cache_read_tok if TRACK_CACHE_TOKENS else 0,
                "cache_write_tokens": cache_creation_tok if TRACK_CACHE_TOKENS else 0,
                "product_id":         None,
                "timestamp":          _now_ist_iso_cli_mu(),
            }, key=user_id or None)
            # Loud confirmation so a user reporting "my budget/usage never
            # shows up" can be diagnosed from the log: did this event even
            # reach Kafka, or did it fall back to the Redis queue (which
            # requires workers/kafka_consumer.py to be running to drain)?
            logger.info(
                f"[CLI] model_usages produced user={user_id} model={_resolved_model} "
                f"cost=${cost_usd:.6f} via={'kafka' if _sent_to_kafka else 'redis-fallback'}"
            )
        except Exception as e:
            # Audit-row failure must never poison the request — log and move on.
            logger.warning(
                f"[CLI] model_usages kafka produce FAILED user={user_id} model={model}: {e}"
            )

    try:
        _usage_ctx = contextvars.copy_context()
        threading.Thread(
            target = lambda: _usage_ctx.run(_bg_write),
            daemon = True,
            name   = "cli-model-usage-write",
        ).start()
    except Exception as e:
        logger.warning(f"[CLI] model_usages thread launch failed: {e}")


def _track_budget(
        user_id:            str,
        model:              str,
        model_hint:         str,
        in_tok:             int,
        out_tok:            int,
        timings:            Optional[dict[str, float]] = None,
        request_id:         str = "",
        source_channel:     str = "CLI",
        cache_read_tok:     int = 0,
        cache_creation_tok: int = 0,
        req:                Optional["MessagesRequest"] = None,
        precomputed_cost_usd: Optional[float] = None,
) -> None:
    """
    Persist this request's token usage + cost against the authenticated user's
    budget row AND the per-request `model_usages` audit table.

    Two writes happen here:

    1. **`user_usage_totals`** (via `increment_usage`) — running per-user
       aggregate of tokens / requests / cost. Used by the budget gate at the
       top of every request.

    2. **`model_usages`** (via `_persist_model_usage_async`) — one row per
       request capturing model, token split, latency, cost, request_id and
       `endpoint="/v1/messages"`. This is the same table that already
       receives rows from `/ask` and `/v1/chat/completions`, so CLI traffic
       now shows up in the same reporting / chargeback surfaces. The insert
       is dispatched on a daemon thread to keep it off the SSE close path.

    Caller is responsible for passing the JWT-derived user_id (resolved at
    the request entrypoint via `user.get("sub") or user.get("user_id") ...`).

    In-house models (Kimi, Qwen, GLM, Ollama, etc.) still record tokens for
    audit/telemetry but contribute $0.00 to the cost total — they run on
    AiNxt hardware and have no per-token billing.

    `cache_read_tok` / `cache_creation_tok` (Claude only — always 0 for other
    providers) are Anthropic's cache_read_input_tokens / cache_creation_input_tokens.
    They are disjoint from `in_tok` (Anthropic's `input_tokens` excludes cached
    tokens entirely), so they must be billed and recorded explicitly rather
    than folded into `in_tok` or dropped.
    """
    # Platform-service CLI runs (ABStudio today, SDLC and other products in
    # the future): audit + billing happen on the OWNING product's side. Skip
    # every write here (increment_usage, model_usages, coach event) so the
    # same run is not double-charged on both legs. Sentinel comes from
    # _resolve_user's platform-service branch.
    if user_id == _PLATFORM_SERVICE_USER_ID:
        return
    # Cost is derived from the *canonical* model id (model_hint) — the same
    # id used by /ask and /v1/chat/completions — so MODEL_COST_PER_1M lookups
    # hit the right key regardless of which CLI alias the user typed
    # (e.g. "sonnet" → "claude-sonnet-4-6").
    canonical_model = model_hint or model
    # `precomputed_cost_usd` lets the caller pass the exact figure it already
    # sent to the CLI on the wire (`ainxt_cost_in_usd_ticks`) so the number a
    # user sees in the CLI and the number debited from their budget can never
    # drift apart by being computed twice. Callers that don't have one yet
    # (e.g. legacy call sites) fall back to computing it here.
    if precomputed_cost_usd is not None:
        cost = precomputed_cost_usd
    else:
        cost, _priced = _compute_cost_usd(canonical_model, in_tok, out_tok, cache_read_tok, cache_creation_tok)


    # Compute end-to-end latency from the request entry timestamp captured by
    # the endpoint handler. Falls back to 0.0 when timing data is unavailable
    # (e.g. error paths or callers that don't propagate `timings`).
    latency_ms = 0.0
    if timings:
        t_entry = timings.get("t_entry")
        t_end   = timings.get("t_last_byte") or timings.get("t_first_byte")
        if t_entry and t_end:
            latency_ms = (t_end - t_entry) * 1000.0

    # Always attempt the per-request audit row, even for anonymous requests —
    # `model_usages.user_id` is nullable. This preserves traffic visibility
    # for traffic that authenticates via an unexpected token shape; it would
    # otherwise vanish entirely.
    _persist_model_usage_async(
        user_id            = user_id,
        model              = canonical_model,
        in_tok             = in_tok,
        out_tok            = out_tok,
        cost_usd           = cost,
        latency_ms         = latency_ms,
        request_id         = request_id,
        source_channel     = source_channel,
        cache_read_tok     = cache_read_tok,
        cache_creation_tok = cache_creation_tok,
    )

    # ── AiNxt Coach — emit a practice event for CLI / IDE traffic ────────────
    # Single shared point for all 4 provider backends (they all funnel through
    # _track_budget). Extraction/normalisation now lives in core.coach_events so
    # this router never owns Coach logic; emit_coach_event_from_messages() never
    # raises and is a no-op when ENABLE_COACH is off.
    if user_id:
        try:
            from core.coach_events import emit_coach_event_from_messages
            emit_coach_event_from_messages(
                user_id=user_id,
                messages=[{"role": m.role, "content": m.content} for m in (req.messages or [])],
                model=canonical_model,
                request_id=request_id,
                thread_id=_cv_conv_id.get() or None,
                tokens_in=in_tok,
                tokens_out=out_tok,
                cost_usd=cost,
                latency_ms=int(latency_ms),
                channel=(source_channel or "cli").lower(),
            )
        except Exception:
            pass

    if not user_id:
        logger.warning(f"[CLI] budget skipped: anonymous request model={canonical_model}")
        return  # Anonymous request — no aggregate row to update.

    try:
        from store.budget_store import increment_usage
        # Reuse `cost` computed above via _compute_cost_usd(canonical_model, ...) —
        # it already zeroes out in-house models and bills cache_read/cache_creation
        # tokens at Anthropic's documented rates. Previously this block recomputed
        # cost independently from `in_tok`/`out_tok` only (against the un-resolved
        # `model` alias, not `canonical_model`), silently dropping cache tokens
        # from the amount debited against the user's budget.
        total_tok    = in_tok + out_tok + cache_read_tok + cache_creation_tok
        total_in_tok = in_tok + cache_read_tok + cache_creation_tok
        increment_usage(user_id, tokens=total_tok, requests=1, cost_usd=cost)
        logger.info(
            f"[CLI] budget recorded user={user_id} model={canonical_model} "
            f"in_tok={in_tok} out_tok={out_tok} cache_read={cache_read_tok} "
            f"cache_creation={cache_creation_tok} total_in_tok={total_in_tok} "
            f"total_tok={total_tok} cost_usd={cost:.6f} latency_ms={latency_ms:.0f}"
        )
    except Exception as e:
        logger.warning(f"[CLI] _track_budget failed for user={user_id} model={canonical_model}: {e}")


# ── Compliance check ──────────────────────────────────────────────────────────

_VALIDATED_HASH_MAX = 10000
_VALIDATED_HASHES: "OrderedDict[str, bool]" = OrderedDict()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _is_validated(h: str) -> bool:
    if h in _VALIDATED_HASHES:
        _VALIDATED_HASHES.move_to_end(h)  # LRU touch
        return True
    return False


def _mark_validated(h: str) -> None:
    _VALIDATED_HASHES[h] = True
    if len(_VALIDATED_HASHES) > _VALIDATED_HASH_MAX:
        _VALIDATED_HASHES.popitem(last=False)





# ── Indirect prompt-injection defense (ADR-009) ──────────────────────────────
# Logic extracted to core/injection_guard.py — shared with gateway.py.
from core.injection_guard import (
    injection_guard as _injection_guard,
    ENABLE_INJECTION_SCAN as _ENABLE_INJECTION_SCAN,
    INJECTION_SCAN_SUBSTITUTE_USER_MESSAGE as _INJECTION_SCAN_SUBSTITUTE_USER_MESSAGE,
)





def _compliance_check(messages: list[Message]) -> tuple[Optional[str], list[dict], Optional[str]]:
    """
    Scan messages going OUT to the LLM provider for PCI/PII/secrets and apply
    deterministic hard blocks on the same message window used by /ask.

    Returns:
        (violation, findings, hardblock_category)
          - violation: str describing the blocked compliance or hardblock result,
                       or None when allowed.
          - findings:  union of non-blocking findings (PII/SECRET/etc. to be redacted)
                       collected across every newly-scanned user/tool message.
                       The downstream provider gateway uses these to redact when
                       precleared=True (avoids the double-validation false-positive
                       PCI blocks described in gateway.py /ask). When the cache short-
                       circuits a message (already validated this process), its
                       findings are NOT re-included — the upstream gate that
                       originally validated it forwarded them then, and per-process
                       hash-caching is best-effort de-dup, not a findings store.
          - hardblock_category: the first hardblock category hit, when applicable.

    Scope rules — established to avoid O(N²) re-scans on every agent iteration:

    1. **Only scan the current turn**: a CLI agent iteration re-sends the full
       conversation history every request. Messages BEFORE the last assistant
       turn have already passed (or been hard-blocked by) compliance on a prior
       request, so re-validating them is pure waste and was costing 40-107s of
       wall-clock per turn on long sessions (privacy-svc HTTP fan-out, see
       agents/compliance_engine.py:_call_privacy_svc).

       We locate the LAST assistant-role index and only scan messages AFTER it.
       For the FIRST request in a session (no assistant message yet) we scan
       the whole list — same as before. This preserves the safety invariant:
       no user/tool_result content ever reaches the upstream provider without
       a compliance check on the request that introduced it.

    2. **Skip `assistant` turns**: those are pure LLM output already streamed
       back to the user. Re-scanning here would not redact what's already on
       the wire; output-side compliance happens at generation time elsewhere.

    3. **Scan `user` turns**: this includes both the engineer's typed prompt
       AND `tool_result` content (which carries `read_file`/`bash` output
       from the user's machine — can leak `.env`, credentials, customer data
       to the upstream provider).

    4. **Hash-cache validated content**: each unique message body is scanned
       at most once per process. Acts as a second-line de-dup if the same
       tool_result body recurs (it usually doesn't).
    """
    findings_union: list[dict] = []
    try:
        from agents.compliance_engine import compliance_engine

        # Window the scan to messages AFTER the last assistant turn — see
        # docstring scope rule #1. This is the single biggest perf win on the
        # CLI path: msgs=169 turn drops from ~107s of compliance work to a
        # single-message scan (~tens of ms when privacy-svc is healthy).
        last_assistant_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].role == "assistant":
                last_assistant_idx = i
                break
        scan_window = messages[last_assistant_idx + 1:]

        from core.config import COMPLIANCE_SCAN_TOOL_RESULTS, HARDBLOCK_ENABLED

        # Log once per request, not once per message — HARDBLOCK_ENABLED is a
        # process-level constant so emitting it inside the loop produced N
        # identical warnings (one per message in scan_window).
        if not HARDBLOCK_ENABLED:
            logger.warning(
                "[CLI] HardBlock DISABLED via HARDBLOCK_ENABLED=false — skipping check for this message"
            )

        for msg in scan_window:
            if msg.role == "assistant":
                # Defensive — shouldn't appear in window, but skip if it does.
                continue
            # Tool-result messages are gated by COMPLIANCE_SCAN_TOOL_RESULTS (the
            # file-read data-breach guard). Current user turn is always scanned.
            if msg.role == "tool" and not COMPLIANCE_SCAN_TOOL_RESULTS:
                continue
            text = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
            if HARDBLOCK_ENABLED:
                is_tool_result = msg.role == "tool"
                hb = hardblock_engine.check(text, is_tool_result=is_tool_result)
                if hb.get("blocked"):
                    category = hb.get("category") or "unknown"
                    logger.warning(
                        "[CLI] HardBlockEngine TRIGGERED → category=%s score=%.3f "
                        "matched=%s is_tool_result=%s",
                        category,
                        hb.get("score", 0.0),
                        hb.get("matched_phrases", []),
                        is_tool_result,
                    )
                    return f"AI Safety policy violation: category={category}", [], category

            h = _content_hash(text)
            if _is_validated(h):
                continue
            result = compliance_engine.validate_input(text)
            blocked = [f["type"] for f in result.get("findings", []) if f.get("blocked")]
            if blocked:
                return f"Compliance violation: {', '.join(blocked)}", [], None
            # Collect non-blocking findings so the provider gateway can redact
            # them without re-running validate_input on the second pass.
            for f in result.get("findings", []):
                if f.get("value"):
                    findings_union.append({"type": f.get("type"), "value": f.get("value")})
            _mark_validated(h)
    except Exception as e:
        logger.warning(f"[CLI] compliance check skipped: {e}")
        return None, [], None
    return None, findings_union, None


# ── Endpoint ──────────────────────────────────────────────────────────────────

# ── Multilingual helpers ──────────────────────────────────────────────────────

def _translate_in_msgs(serial_msgs: list[dict]) -> list[dict]:
    """
    Translate non-English PROSE in message text → English (detection-driven), so
    the model always reasons in English. Code/paths/tool blocks are never touched.
    Runs SYNC (blocking httpx) — call via asyncio.to_thread from the async handler.
    Best-effort: any failure keeps the original text.
    """
    if not _XLAT_AVAILABLE:
        return serial_msgs

    def _xl(text: str) -> str:
        if not text or not text.strip():
            return text
        try:
            src = _xl_detect(text)
        except Exception:
            return text
        # Skip English / undecided / mixed, and anything outside the supported set.
        if src in ("en", "unknown", "mixed") or not _xl_supported(src):
            return text
        return _xl_text(text, src, "en")

    out: list[dict] = []
    for m in serial_msgs:
        c = m.get("content")
        if isinstance(c, str):
            out.append({**m, "content": _xl(c)})
        elif isinstance(c, list):
            blocks = []
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                    blocks.append({**b, "text": _xl(b["text"])})
                else:
                    blocks.append(b)  # tool_use / tool_result / images — never translate
            out.append({**m, "content": blocks})
        else:
            out.append(m)
    return out


def _sse_data(chunk: str) -> Optional[dict]:
    """Parse the JSON object from an SSE chunk's first `data:` line."""
    for line in chunk.split("\n"):
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if not payload:
                return None
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return None
    return None


async def _translate_out_stream(
        inner: AsyncGenerator[str, None],
        target_lang: str,
) -> AsyncGenerator[str, None]:
    """
    Wrap an Anthropic-SSE generator: buffer text_delta fragments per content
    block and, at content_block_stop, translate the FULL English text block to
    `target_lang` (prose-only; code verbatim) and emit it as ONE text_delta.
    Tool blocks, structure events and usage pass through untouched. Buffering the
    whole block (not per-token) is what lets the segmenter keep code fences intact.
    Degrades to English on any error.
    """
    if not (_XLAT_AVAILABLE and target_lang and target_lang != "en"):
        async for chunk in inner:
            yield chunk
        return

    buffers: dict[int, str] = {}

    async def _emit_translated(idx: int, eng: str) -> str:
        try:
            translated = await asyncio.to_thread(_xl_from_en, eng, target_lang)
        except Exception:
            translated = eng
        return _sse("content_block_delta", {
            "type": "content_block_delta", "index": idx,
            "delta": {"type": "text_delta", "text": translated},
        })

    async for chunk in inner:
        ev = _sse_data(chunk)
        if ev is None:
            yield chunk
            continue
        etype = ev.get("type") or ev.get("event") or ""
        if etype == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
            idx = ev.get("index", 0)
            buffers[idx] = buffers.get(idx, "") + ((ev.get("delta") or {}).get("text") or "")
            continue  # hold — emit a single translated delta at block stop
        if etype == "content_block_stop":
            idx = ev.get("index", 0)
            if idx in buffers:
                yield await _emit_translated(idx, buffers.pop(idx))
            yield chunk
            continue
        yield chunk

    # Safety: flush any unclosed text buffers (malformed upstream stream)
    for idx in list(buffers.keys()):
        yield await _emit_translated(idx, buffers.pop(idx))


@router.post("/v1/messages")
@router.post("/messages")
async def messages_endpoint(req: MessagesRequest, request: Request):
    """
    Anthropic Messages API compatible endpoint for ainxt-cli.

    The Anthropic SDK (in ainxt-cli) points its baseURL at this gateway and
    sends its JWT as x-api-key. Every request goes through:
      1. JWT authentication
      2. Budget gate
      3. Compliance check (PCI/PII blocking)
      4. Provider routing (Claude / OpenAI / Gemini / In-house)
      5. Anthropic SSE format response (unified regardless of provider)
    """
    # Phase-timing breakdown — feeds _log_phase_timings at stream end so each
    # request emits one INFO line of: auth → budget → compliance → TTFB → stream.
    timings: dict[str, float] = {"t_entry": time.monotonic()}

    # ── Tracing: bind per-request context so every downstream logger.info() ──
    # call automatically carries request_id / user_id / chat_id / client_source
    # via core.logger._context_processor. Matches the pattern used in
    # projects_router.py, ide_router.py, chat_router.py.

    #
    # Prefer the client-supplied `x-client-request-id` header so the same id
    # threads through the CLI → gateway → upstream logs (makes cross-process
    # correlation trivial). Fall back to a fresh uuid4 hex when the client
    # didn't send one.
    req_id = (request.headers.get("x-client-request-id") or "").strip() or uuid.uuid4().hex[:8]
    set_request_id(req_id)
    # Stable per-conversation ID sent by ainxt-cli on every turn of a session.
    # Forwarded to the LLM proxy as X-AiNxt-Conv-Id so the proxy can correlate
    # all turns of a multi-turn conversation end-to-end.
    conv_id = (request.headers.get("x-ainxt-conv-id") or "").strip()
    _cv_conv_id.set(conv_id)
    # Unconditionally set correlation_id so this CLI request never inherits a
    # stale value left on a reused worker thread. Prefer a client-supplied
    # x-correlation-id (cross-process tracing); otherwise use req_id itself.
    _corr_id = (request.headers.get("x-correlation-id") or "").strip() or req_id
    set_correlation_id(_corr_id)
    set_span_id("messages_compat.v1")
    set_client_source("cli")
    logger.info(f"[CLI] ▶ POST /v1/messages req_id={req_id}")

    try:
        # Offload to a worker thread: _resolve_user → decode_token does
        # blocking Redis (revocation + session check). Running it on the event
        # loop freezes every other CLI/web request on this worker. Threadpool
        # offload keeps the gate mandatory and in-order while freeing the loop.
        user = await run_in_threadpool(_resolve_user, request)
    except HTTPException as e:
        logger.warning(f"[CLI] auth failed req_id={req_id} status={e.status_code} detail={e.detail}")
        raise
    user_id = user.get("sub") or user.get("user_id") or user.get("id") or "cli"
    # Bind user_id (and a synthetic chat_id derived from request metadata when
    # the CLI provides one) onto the thread-local logger context so every
    # subsequent log line in this request carries them automatically.
    explicit_chat_id = ""
    if isinstance(req.metadata, dict):
        explicit_chat_id = str(req.metadata.get("session_id") or req.metadata.get("chat_id") or "")
    chat_id = explicit_chat_id.strip()
    set_chat_context(user_id, chat_id or req_id)
    timings["t_auth"] = time.monotonic()
    logger.info(f"[CLI] auth ok user={user_id} req_id={req_id} chat_id={chat_id}")

    # Budget gate — check_budget() returns {"allowed": bool, "reason": str}.
    # The previous code checked the non-existent "blocked" key, so the gate
    # was a silent no-op and every CLI user passed regardless of spend.

    # Budget gate — in-house / local models are FREE: never budget-checked and
    # never billed. Same predicate _track_budget uses to charge $0, so the gate
    # and the cost accounting stay consistent. Only cloud models hit check_budget.
    # Platform-service CLI runs (sentinel user_id) also skip the gate — those
    # requests are budgeted by the owning product (ABStudio, SDLC, …), not here
    # (see _resolve_user above and _track_budget below).
    if not _is_in_house_model(req.model) and user_id != _PLATFORM_SERVICE_USER_ID:
        try:
            from store.budget_store import check_budget
            # Offload: check_budget does blocking DB/Redis. Same loop-freeze
            # concern as auth above — wrap in threadpool, ordering unchanged.
            budget = await run_in_threadpool(check_budget, user_id)
            if not budget.get("allowed", True):
                reason = budget.get("reason") or "Budget limit exceeded — contact your administrator"
                logger.warning(f"[CLI] budget blocked user={user_id} reason={reason}")
                raise HTTPException(429, reason)
            logger.info(f"[CLI] budget ok user={user_id} reason={budget.get('reason', '')}")
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"[CLI] budget gate skipped user={user_id}: {e}")
    timings["t_budget"] = time.monotonic()

    # Compliance — first-pass gate. Findings flow downstream so the local
    # fallback path (model_router.stream → openai.generate when local LLM is
    # unavailable, or when complexity classifier upgrades to a cloud tier)
    # can redact without re-running validate_input(). Without precleared
    # plumbing, the second pass intermittently false-positive-blocked benign
    # CLI prompts on local-model selections — same root cause as the /ask
    # fix in gateway.py:3016.
    # Offload: _compliance_check → compliance_engine does a blocking httpx POST
    # to privacy-svc (read timeout 2.0s) + regex scans over the growing message
    # history. This is the single largest event-loop stall on the CLI path.
    # Threadpool offload preserves the mandatory before-model verdict and order.
    violation, precleared_findings, hardblock_category = await run_in_threadpool(_compliance_check, req.messages)
    if violation:
        logger.warning(f"[CLI] compliance blocked user={user_id} violation={violation}")
        raise HTTPException(400, violation)
    timings["t_compliance"] = time.monotonic()
    logger.info(
        f"[CLI] compliance ok user={user_id} msgs={len(req.messages)} "
        f"precleared_findings={len(precleared_findings)}"
    )

    # Indirect prompt-injection defense on untrusted `tool_result` content (ADR-009).
    safe_messages = await _injection_guard(req.messages, req.tools, user_id, request_id=req_id)
    timings["t_injection"] = time.monotonic()
    logger.info(
        f"[CLI] injection-guard done user={user_id} "
        f"elapsed={(timings['t_injection'] - timings['t_compliance']) * 1000:.0f}ms"
    )

    model_hint  = _normalise_model(req.model)

    # ── Blocked-model gate ────────────────────────────────────────────────────
    # Reject immediately if the requested model is disabled (e.g. any Opus model
    # when ENABLE_OPUS=false). This fires before the routing log so a blocked
    # request never appears as "routing … provider=claude" in the logs.
    from core.model_registry import BLOCKED_MODELS as _BLOCKED_MODELS
    if model_hint in _BLOCKED_MODELS:
        logger.warning(
            f"[CLI] blocked model rejected user={user_id} "
            f"requested={req.model!r} resolved={model_hint!r}"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Model '{req.model}' is not available. Some models are disabled. Please select a different model.",
        )

    # ── G10: server-side model lock for Buddy/cowork ─────────────────────────────
    # The desktop UI pins Buddy to BUDDY_FORCED_MODEL, but a UI-only lock is
    # bypassable by a crafted client. When the request is tagged as a cowork surface
    # (x-ainxt-surface: cowork, or x-ainxt-client: cowork — sent by the Buddy CLI)
    # AND the lock is enabled, we OVERRIDE the requested model server-side so the
    # policy cannot be bypassed. Non-cowork traffic (Code tab, IDE, API) is untouched.
    _surface = (request.headers.get("x-ainxt-surface")
                or request.headers.get("x-ainxt-client") or "").strip().lower().split("/")[0]
    if _surface == "cowork" and os.getenv("BUDDY_MODEL_LOCKED", "true").strip().lower() in ("1", "true", "yes"):
        _forced = _normalise_model(os.getenv("BUDDY_FORCED_MODEL", "").strip())
        if _forced and _forced != model_hint:
            logger.info(f"[CLI] cowork model lock: overriding {model_hint} → {_forced}")
            model_hint = _forced

    # Utilization channel tag for the model_usages audit row.
    #
    # This endpoint is only ever reached by an ainxt-cli process (old streamjson
    # or new ACP), never a browser — there is no web-based Buddy client that
    # speaks the /v1/messages wire format. So any request tagged with the cowork
    # surface here IS the Electron desktop app's Buddy CLI subprocess: tag it
    # DESKTOP-BUDDY unconditionally. (Previously this also required an
    # `x-ainxt-client: cli/*` header to confirm DESKTOP-BUDDY, falling back to a
    # WEB-BUDDY tag otherwise — that fallback never reflected a real web client
    # and only misclassified genuine desktop Buddy sessions whenever the CLI
    # binary didn't also attach that header on a given request.)
    #
    # Non-cowork traffic on this endpoint is "CLI" (standalone terminal or
    # direct API call).
    source_channel = "DESKTOP-BUDDY" if _surface == "cowork" else "CLI"

    provider    = _detect_provider(model_hint)
    system      = _system_text(req.system)
    # Use the REDACTED messages — this substitution is the whole fix.
    serial_msgs = _serial_msgs(safe_messages)

    # ── Buddy (cowork surface): Redis-backed history pipeline ─────────────────
    # Mirrors the web Chat history pipeline (gateway.py ask_ai) so Buddy gets
    # the same context quality regardless of whether the CLI's own on-disk
    # session is intact:
    #   1. Load last 200 turns from Redis, keyed on x-ainxt-conv-id.
    #   2. If > 150K tokens, keep only the last 40 turns verbatim.
    #   3. Save user+assistant turns to Redis after each turn (in finally:).
    # Guarded by _surface == "cowork" and conv_id — non-Buddy CLI traffic
    # (Code tab, IDE, plain API) is completely unaffected.
    _BUDDY_TRIGGER_TOKENS = 150_000   # same as web Chat _FLAT_TRIGGER_TOKENS
    _BUDDY_SUMMARY_TURNS  = 40        # same as web Chat _SUMMARY_TURNS
    _buddy_used_summary   = False     # set True when history is trimmed (→ SSE notice)

    if _surface == "cowork" and conv_id:
        # Load Redis history for this conversation (conv_id from x-ainxt-conv-id).
        try:
            from memory.redis_memory import RedisMemory as _RM_load
            _rm_load = _RM_load()
            _redis_hist = _rm_load.get_conversation(conv_id, limit=200, user_id=user_id)
            if _redis_hist:
                _hist_msgs: list[dict] = []
                for _m in _redis_hist:
                    _r_role = _m.get("role", "")
                    if _r_role not in ("user", "assistant"):
                        continue
                    _r_content = (_m.get("content") or "").strip()
                    if _r_content:
                        _hist_msgs.append({"role": _r_role, "content": _r_content})

                # If Redis has more messages than the CLI sent, the CLI's own
                # session was lost (resume failure) — use Redis as the base
                # and append the current turn.
                _cli_msg_count   = len(serial_msgs)
                _redis_msg_count = len(_hist_msgs)

                if _redis_msg_count > _cli_msg_count:
                    _current_user_msgs = [m for m in serial_msgs if m.get("role") == "user"]
                    _current_turn = _current_user_msgs[-1] if _current_user_msgs else None
                    serial_msgs = _hist_msgs
                    if _current_turn:
                        _last_redis_user = next(
                            (m for m in reversed(_hist_msgs) if m.get("role") == "user"),
                            None,
                        )
                        if (
                            not _last_redis_user
                            or _last_redis_user.get("content", "").strip()
                            != _current_turn.get("content", "").strip()
                        ):
                            serial_msgs = _hist_msgs + [_current_turn]
                    logger.info(
                        f"[CLI] Buddy resume-failure recovery: "
                        f"replaced {_cli_msg_count} CLI msgs with "
                        f"{len(serial_msgs)} Redis msgs for conv_id={conv_id}"
                    )
        except Exception as _re:
            logger.warning(f"[CLI] Buddy Redis load failed conv_id={conv_id}: {_re}")

        # Trim to last 40 turns when over 150K tokens.
        _buddy_token_est = _estimate_messages_tokens(system, serial_msgs)
        if _buddy_token_est > _BUDDY_TRIGGER_TOKENS:
            serial_msgs = serial_msgs[-_BUDDY_SUMMARY_TURNS:]
            _buddy_used_summary = True
            logger.info(
                f"[CLI] Buddy history trimmed: estimated={_buddy_token_est} "
                f"> {_BUDDY_TRIGGER_TOKENS}, kept last {_BUDDY_SUMMARY_TURNS} "
                f"turns for conv_id={conv_id}"
            )

    # Multilingual: CLI sends header X-AiNxt-Target-Lang. Translate the user's
    # prose input → English so the model reasons in English (code stays verbatim).
    _target_lang = (request.headers.get("x-ainxt-target-lang") or "").strip().lower() or None
    if _XLAT_AVAILABLE and _target_lang and _target_lang != "en":
        try:
            serial_msgs = await asyncio.to_thread(_translate_in_msgs, serial_msgs)
        except Exception as _e:
            logger.warning(f"messages-compat: translate-in failed ({_e}) — using original input")

    logger.info(
        f"[CLI] routing user={user_id} model={req.model}→{model_hint} "
        f"provider={provider} msgs={len(req.messages)} "
        f"tools={len(req.tools or [])} stream={req.stream} "
        f"conv_id={conv_id or '-'}"
    )

    if provider == "claude":
        gen = _stream_claude(
            req, model_hint, system, serial_msgs,
            user_id=user_id, timings=timings, request_id=req_id,
            source_channel=source_channel,
            conv_id=conv_id,
        )
    elif provider == "openai":
        gen = _stream_oai_format(
            req, model_hint, system, serial_msgs, "openai",
            user_id=user_id, timings=timings, request_id=req_id,
            source_channel=source_channel,
            conv_id=conv_id,
        )
    elif provider == "gemini":
        gen = _stream_oai_format(
            req, model_hint, system, serial_msgs, "gemini",
            user_id=user_id, timings=timings, request_id=req_id,
            source_channel=source_channel,
            conv_id=conv_id,
        )
    else:
        # In-house / local / Ollama — `timings` is now plumbed through so
        # latency_ms reaches the model_usages row alongside other providers.
        # precleared_findings forwards the first-pass findings so when the
        # local tier silently falls back to OpenAI (local LLM down, or the
        # complexity classifier upgrades a hint not in _HINT_MAP from
        # TIER_SIMPLE → TIER_MEDIUM), the OpenAI gateway skips its second-
        # pass block decision and only redacts.
        gen = _stream_local(
            req, model_hint, system, serial_msgs,
            user_id=user_id, timings=timings, request_id=req_id,
            precleared_findings=precleared_findings,
            source_channel=source_channel,
            conv_id=conv_id,
        )

    # Multilingual translate-out: wrap the SSE stream so the assistant's English
    # prose is translated back to the user's language (code verbatim). Applies to
    # BOTH the streaming and non-streaming (_collect_stream_to_message) paths.
    if _XLAT_AVAILABLE and _target_lang and _target_lang != "en":
        gen = _translate_out_stream(gen, _target_lang)

    # If the Buddy Redis pipeline trimmed history above, prepend an SSE notice
    # so the Buddy pane can show a "history was summarized" banner (existing
    # type:"notice" handler in coworkSession.js — no UI change needed).
    if _buddy_used_summary and req.stream:
        async def _buddy_notice_then(inner: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
            _notice = json.dumps({
                "type":  "content_block_delta",
                "index": 0,
                "delta": {
                    "type":  "notice",
                    "level": "info",
                    "msg":   (
                        "Earlier parts of this conversation were summarized "
                        "to fit the model\u2019s context window."
                    ),
                },
            })
            yield f"data: {_notice}\n\n"
            async for _chunk in inner:
                yield _chunk
        gen = _buddy_notice_then(gen)


    # Non-streaming path: agent loops occasionally fire one-shot calls
    # (title generation, summarisation, classification, side-quests) without
    # the SDK's .stream() wrapper. We consume the SSE generator internally,
    # assemble a complete Anthropic-format message, and return it as JSON.
    # Previously rejected with 400, which hung CLI flows that hit those code
    # paths (e.g. after ~50 tool calls when auto-compaction triggered).
    if not req.stream:
        logger.info(f"[CLI] non-streaming collect start user={user_id} provider={provider}")
        try:
            message = await _collect_stream_to_message(gen)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[CLI] non-streaming collect failed user={user_id}: {e}", exc_info=True)
            raise HTTPException(502, f"Upstream LLM error: {e}")
        logger.info(f"[CLI] non-streaming collect done user={user_id} provider={provider}")
        return JSONResponse(message, headers={"anthropic-version": "2023-06-01"})

    logger.info(f"[CLI] streaming response start user={user_id} provider={provider}")
    try:
        return StreamingResponse(
            gen,
            media_type="text/event-stream",
            headers={
                "Cache-Control":     "no-cache",
                "X-Accel-Buffering": "no",
                "anthropic-version": "2023-06-01",
            },
        )
    finally:
        # Buddy (cowork surface): persist this turn to Redis so the next turn
        # can recover full history even if the CLI's on-disk session is lost.
        if _surface == "cowork" and conv_id:
            try:
                from memory.redis_memory import RedisMemory as _RM_save
                _rm_save = _RM_save()
                _buddy_user_msgs = [m for m in serial_msgs if m.get("role") == "user"]
                _buddy_user_text = (_buddy_user_msgs[-1].get("content", "") if _buddy_user_msgs else "")
                if not isinstance(_buddy_user_text, str):
                    _buddy_user_text = json.dumps(_buddy_user_text, default=str)
                if _buddy_user_text:
                    _rm_save.save_message(
                        session_id=conv_id,
                        role="user",
                        content=_buddy_user_text,
                        metadata={"surface": "cowork"},
                        user_id=user_id,
                    )
                # Assistant turn — scan req.messages for the last assistant content.
                _buddy_asst_text = ""
                for _bm in reversed(req.messages or []):
                    if _bm.role == "assistant":
                        _bc = _bm.content
                        if isinstance(_bc, str):
                            _buddy_asst_text = _bc
                        elif isinstance(_bc, list):
                            _buddy_asst_text = " ".join(
                                b.get("text", "") for b in _bc
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        break
                if _buddy_asst_text:
                    _rm_save.save_message(
                        session_id=conv_id,
                        role="assistant",
                        content=_buddy_asst_text[:4000],
                        metadata={"surface": "cowork"},
                        user_id=user_id,
                    )
            except Exception as _rs_err:
                logger.warning(
                    f"[CLI] Buddy Redis save failed conv_id={conv_id}: {_rs_err}"
                )

        # Clear thread-local logger context so a reused worker thread does not
        # carry this request's request_id/chat_id into the next request.
        clear_chat_context()
        clear_bound_context()


async def _collect_stream_to_message(
        gen: AsyncGenerator[str, None],
) -> dict:
    """
    Consume an Anthropic-format SSE generator and assemble a single
    non-streaming `Message` response dict matching the Anthropic Messages
    API shape. Used by the non-streaming branch of /v1/messages so SDK
    callers that use .create() (not .stream()) get a normal JSON body.

    Parses these SSE events in order:
      message_start         → seeds id/model/usage
      content_block_start   → opens a text or tool_use block at index N
      content_block_delta   → text_delta appends to text; input_json_delta
                              concatenates partial_json fragments for tools
      content_block_stop    → finalises that block (JSON-parses tool input)
      message_delta         → final stop_reason + authoritative usage
      message_stop          → end marker
      error                 → raises HTTPException(502)
    """
    msg_id = ""
    model = ""
    stop_reason = "end_turn"
    blocks: dict[int, dict] = {}  # index → content block (text or tool_use)
    tool_json_buf: dict[int, list[str]] = {}  # index → partial_json fragments
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        # ainxt extension — carried through from the underlying message_delta
        # so non-streaming (.create()) SDK callers see the same gateway-priced
        # cost the streaming path already reports.
        "ainxt_cost_in_usd_ticks": None,
        "ainxt_cost_priced": None,
    }
    async for chunk in gen:
        # SSE chunks look like:  "event: <name>\ndata: <json>\n\n"
        # Extract every `data: ...` line (a chunk may contain multiple).
        for line in chunk.split("\n"):
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            ev_type = ev.get("type") or ev.get("event") or ""
            if ev_type == "message_start":
                m = ev.get("message", {})
                msg_id = m.get("id", msg_id)
                model = m.get("model", model)
                u = m.get("usage", {}) or {}
                # Seed usage with anything the upstream supplied — message_delta
                # may overwrite below.
                for k in usage:
                    # `ainxt_cost_priced` legitimately carries `False` — unlike
                    # every other key here, that is a meaningful value to copy
                    # through, not a "missing" sentinel — so it needs its own
                    # "was this key present at all" check instead of `is not None`.
                    if k == "ainxt_cost_priced":
                        if "ainxt_cost_priced" in u:
                            usage[k] = u[k]
                    elif u.get(k) is not None:
                        usage[k] = u[k]
            elif ev_type == "content_block_start":
                idx = ev.get("index", 0)
                cb = ev.get("content_block", {})
                if cb.get("type") == "text":
                    blocks[idx] = {"type": "text", "text": cb.get("text", "")}
                elif cb.get("type") == "tool_use":
                    blocks[idx] = {
                        "type": "tool_use",
                        "id": cb.get("id", ""),
                        "name": cb.get("name", ""),
                        "input": {},
                    }
                    tool_json_buf[idx] = []
            elif ev_type == "content_block_delta":
                idx = ev.get("index", 0)
                delta = ev.get("delta", {}) or {}
                if delta.get("type") == "text_delta" and idx in blocks:
                    blocks[idx]["text"] = blocks[idx].get("text", "") + delta.get("text", "")
                elif delta.get("type") == "input_json_delta" and idx in tool_json_buf:
                    tool_json_buf[idx].append(delta.get("partial_json", ""))
            elif ev_type == "content_block_stop":
                idx = ev.get("index", 0)
                # If this was a tool_use block, parse the accumulated JSON fragments.
                if idx in tool_json_buf and idx in blocks:
                    joined = "".join(tool_json_buf[idx])
                    try:
                        blocks[idx]["input"] = json.loads(joined) if joined else {}
                    except json.JSONDecodeError:
                        # Best-effort: keep raw fragments so caller still sees something.
                        blocks[idx]["input"] = {"_raw": joined}
                    del tool_json_buf[idx]
            elif ev_type == "message_delta":
                delta = ev.get("delta", {}) or {}
                if "stop_reason" in delta and delta["stop_reason"]:
                    stop_reason = delta["stop_reason"]
                u = ev.get("usage", {}) or {}
                for k in usage:
                    if u.get(k) is not None:
                        usage[k] = u[k]
            elif ev_type == "error":
                err = ev.get("error", {}) or {}
                raise HTTPException(502, err.get("message", "Upstream error"))
            # message_stop has nothing to merge — terminal marker.

    content = [blocks[i] for i in sorted(blocks.keys())]
    return {
        "id":            msg_id or f"msg_{uuid.uuid4().hex[:24]}",
        "type":          "message",
        "role":          "assistant",
        "model":         model,
        "content":       content,
        "stop_reason":   stop_reason,
        "stop_sequence": None,
        "usage":         usage,
    }


# ── Models list endpoint (for /model command in CLI) ─────────────────────────

@router.post("/messages/count_tokens")
async def count_tokens_compat(req: MessagesRequest, request: Request):
    """Anthropic `count_tokens` compatibility — the Agent SDK calls this to budget
    context before sending. We return a fast, dependency-free ESTIMATE (≈chars/4
    over the serialized system + messages + tools). Precision isn't required: the
    SDK uses it for context management, not billing. Authenticated like /v1/messages.
    Without this the SDK gets 405 and its context handling can break a turn."""
    # Offload blocking Redis auth off the event loop — the SDK calls this before
    # most turns, so a synchronous _resolve_user here is a per-iteration stall.
    await run_in_threadpool(_resolve_user, request)
    import json as _json
    parts: list[str] = []
    try:
        parts.append(_system_text(req.system) or "")
        for m in _serial_msgs(req.messages or []):
            c = m.get("content")
            parts.append(c if isinstance(c, str) else _json.dumps(c, default=str))
        if req.tools:
            parts.append(_json.dumps(req.tools, default=str))
    except Exception:
        pass
    chars = sum(len(p) for p in parts if p)
    return {"input_tokens": max(1, chars // 4)}


@router.get("/v1/models")
@router.get("/models")
async def list_models_compat(request: Request):
    """
    Returns all available models in a format compatible with the CLI /model command.
    Includes Claude, OpenAI, Gemini, and in-house GPU models.
    """
    _resolve_user(request)
    try:
        from core.model_registry import (
            CLAUDE_PRIMARY_MODEL, CLAUDE_OPUS_MODEL,
            CLAUDE_OPUS_48_MODEL, CLAUDE_OPUS_5_MODEL, CLAUDE_HAIKU,
            OPENAI_CODING_MODEL, OPENAI_SIMPLE_MODEL,
            OPENAI_LATEST_MODEL, GEMINI_VISION_MODEL, LOCAL_LLM_DISPLAY,
            GEMINI_TEXT_MODEL, GEMINI_CODING_LITE_MODEL, GEMINI_IMAGE_MODEL,
            ENABLE_OPUS, ENABLE_CLI_OPUS_48, ENABLE_CLI_OPUS_5,
            CLAUDE_SONNET_5_MODEL, ENABLE_SONNET_5,
            OPENAI_TERA_MODEL, OPENAI_LUNA_MODEL,
            OPENAI_TERA_DISPLAY, OPENAI_LUNA_DISPLAY,
            ENABLE_GPT56_TERA, ENABLE_GPT56_LUNA,
        )
    except ImportError:
        # Registry unavailable — read env vars directly with no hardcoded defaults.
        # Operators must set these vars; empty strings are filtered out of the
        # model list so unconfigured models are simply not offered.
        CLAUDE_PRIMARY_MODEL     = os.getenv("CLAUDE_PRIMARY_MODEL", "")
        CLAUDE_OPUS_MODEL        = os.getenv("CLAUDE_OPUS_MODEL", "")
        CLAUDE_OPUS_48_MODEL     = os.getenv("CLAUDE_OPUS_48_MODEL", "")
        CLAUDE_OPUS_5_MODEL      = os.getenv("CLAUDE_OPUS_5_MODEL", "")
        CLAUDE_HAIKU             = os.getenv("CLAUDE_HAIKU", "")
        OPENAI_CODING_MODEL      = os.getenv("OPENAI_CODING_MODEL", "")
        OPENAI_SIMPLE_MODEL      = os.getenv("OPENAI_SIMPLE_MODEL", "")
        OPENAI_LATEST_MODEL      = os.getenv("OPENAI_LATEST_MODEL", "")
        GEMINI_TEXT_MODEL        = os.getenv("GEMINI_TEXT_MODEL", "")
        GEMINI_CODING_LITE_MODEL = os.getenv("GEMINI_CODING_LITE_MODEL", "")
        GEMINI_IMAGE_MODEL       = os.getenv("GEMINI_IMAGE_MODEL", "")
        GEMINI_VISION_MODEL      = os.getenv("GEMINI_VISION_MODEL", GEMINI_IMAGE_MODEL)
        LOCAL_LLM_DISPLAY        = os.getenv("LOCAL_LLM_DISPLAY", "Local (In-house)")
        ENABLE_OPUS              = os.getenv("ENABLE_OPUS", "true").lower() in ("true", "1", "yes")
        ENABLE_CLI_OPUS_48       = os.getenv("ENABLE_CLI_OPUS_48", "true").lower() in ("true", "1", "yes")
        ENABLE_CLI_OPUS_5        = os.getenv("ENABLE_CLI_OPUS_5", "false").lower() in ("true", "1", "yes")
        CLAUDE_SONNET_5_MODEL    = os.getenv("CLAUDE_SONNET_5_MODEL", "")
        ENABLE_SONNET_5          = os.getenv("ENABLE_SONNET_5", "true").lower() in ("true", "1", "yes")
        OPENAI_TERA_MODEL        = os.getenv("OPENAI_TERA_MODEL", "")
        OPENAI_LUNA_MODEL        = os.getenv("OPENAI_LUNA_MODEL", "")
        OPENAI_TERA_DISPLAY      = os.getenv("OPENAI_TERA_DISPLAY", "GPT-5.6 Terra")
        OPENAI_LUNA_DISPLAY      = os.getenv("OPENAI_LUNA_DISPLAY", "GPT-5.6 Luna")
        ENABLE_GPT56_TERA        = os.getenv("ENABLE_GPT56_TERA", "true").lower() in ("true", "1", "yes")
        ENABLE_GPT56_LUNA        = os.getenv("ENABLE_GPT56_LUNA", "true").lower() in ("true", "1", "yes")

    models = [
        # ── Anthropic Claude ──────────────────────────────────────────────────
        {
            "id": CLAUDE_PRIMARY_MODEL, "hint": CLAUDE_PRIMARY_MODEL,
            "provider": "anthropic", "label": "Claude Sonnet 4.6",
            "tag": "Complex reasoning · SDLC · Primary",
        },
    ]

    # Opus models — only add when ENABLE_OPUS=true (env-controlled)
    if ENABLE_OPUS:
        models += [
            {
                "id": CLAUDE_OPUS_MODEL, "hint": CLAUDE_OPUS_MODEL,
                "provider": "anthropic", "label": "Claude Opus 4.7",
                "tag": "Deepest reasoning · most capable",
            },
        ]
    if ENABLE_OPUS and ENABLE_CLI_OPUS_48:
        models += [
            {
                "id": CLAUDE_OPUS_48_MODEL, "hint": CLAUDE_OPUS_48_MODEL,
                "provider": "anthropic", "label": "Claude Opus 4.8",
                "tag": "Latest Opus · CLI/IDE opt-in",
            },
        ]
    if ENABLE_CLI_OPUS_5:
        models += [
            {
                "id": CLAUDE_OPUS_5_MODEL, "hint": CLAUDE_OPUS_5_MODEL,
                "provider": "anthropic", "label": "Claude Opus 5",
                "tag": "Next-gen Opus · CLI/IDE opt-in",
            },
        ]

    # Sonnet 5 — available on all channels, gated by ENABLE_SONNET_5
    if ENABLE_SONNET_5:
        models.append({
            "id": CLAUDE_SONNET_5_MODEL, "hint": "sonnet-5",
            "provider": "anthropic", "label": "Claude Sonnet 5",
            "tag": "Next-gen Sonnet · all channels · Anthropic",
        })

    models += [
        {
            "id": CLAUDE_HAIKU, "hint": "haiku",
            "provider": "anthropic", "label": "Claude Haiku",
            "tag": "Fast · lightweight tasks",
        },
        # ── OpenAI ────────────────────────────────────────────────────────────
        {
            "id": OPENAI_CODING_MODEL, "hint": OPENAI_CODING_MODEL,
            "provider": "openai", "label": "GPT-5.4",
            "tag": "Coding · agents · OpenAI",
        },
        {
            "id": OPENAI_SIMPLE_MODEL, "hint": "gpt-5-mini",
            "provider": "openai", "label": "GPT-5-mini",
            "tag": "Fast · simple Q&A · OpenAI",
        },
        {
            "id": OPENAI_LATEST_MODEL, "hint": "gpt-5-5",
            "provider": "openai", "label": "GPT-5-5",
            "tag": "Latest OpenAI · explicit selection",
        },
    ]

    # GPT-5.6 Tera — high-capacity variant, gated by ENABLE_GPT56_TERA
    if ENABLE_GPT56_TERA:
        models.append({
            "id": OPENAI_TERA_MODEL, "hint": "tera",
            "provider": "openai", "label": OPENAI_TERA_DISPLAY,
            "tag": "High-capacity · GPT-5.6 Tera · OpenAI",
        })

    # GPT-5.6 Luna — efficient variant, gated by ENABLE_GPT56_LUNA
    if ENABLE_GPT56_LUNA:
        models.append({
            "id": OPENAI_LUNA_MODEL, "hint": "luna",
            "provider": "openai", "label": OPENAI_LUNA_DISPLAY,
            "tag": "Efficient · GPT-5.6 Luna · OpenAI",
        })

    models += [
        # ── Google Gemini ─────────────────────────────────────────────────────
        {
            "id": GEMINI_TEXT_MODEL, "hint": GEMINI_TEXT_MODEL,
            "provider": "google", "label": "Gemini 3.5 Flash",
            "tag": "Coding · text · Google",
        },
        {
            "id": GEMINI_CODING_LITE_MODEL, "hint": GEMINI_CODING_LITE_MODEL,
            "provider": "google", "label": "Gemini 3.1 Flash-Lite",
            "tag": "Lightweight coding · fast · Google",
        },
        # Gemini 3.1 Flash Image model intentionally hidden from CLI /v1/models
        # ── In-house GPU ──────────────────────────────────────────────────────
        {
            "id": "local", "hint": "local",
            "provider": "inhouse", "label": LOCAL_LLM_DISPLAY,
            "tag": "In-house GPU · free · private",
        },
    ]

    # Append dynamically discovered in-house models.
    # IDs are prefixed with "local:" so the CLI can unambiguously route them
    # to the in-house gateway (consistent with /ide/models and /model-governance/models).
    try:
        from gateway_local_llm import _catalog
        for mid in _catalog.all_models():
            prefixed_id = f"local:{mid}"
            if not any(m["id"] == prefixed_id for m in models):
                models.append({
                    "id": prefixed_id, "hint": prefixed_id, "provider": "inhouse",
                    "label": f"Local: {mid}", "tag": "In-house GPU · free · private",
                })
    except Exception:
        pass

    # Attach the hard per-model output-token ceiling when one exists (e.g.
    # Claude Haiku 4.5 caps at 64K output tokens despite a 256K context
    # window). The CLI's `max_tokens` default only clamps against
    # `context_window` (see ainxt-sampler's `default_messages_max_tokens`),
    # so without this the CLI sends an oversized `max_tokens` that the
    # provider hard-rejects with a 400 on every retry. See
    # core.model_registry.MODEL_MAX_OUTPUT_TOKENS for the source of truth.
    try:
        from core.model_registry import max_output_tokens_for as _max_out_for
    except Exception:
        _max_out_for = lambda _m: None  # noqa: E731 — fail-open on import error

    def _with_max_output(m: dict) -> dict:
        ceiling = _max_out_for(m["id"])
        if ceiling:
            return {**m, "max_completion_tokens": ceiling}
        return m

        # Attach `contextWindow` + `autoCompactThresholdPercent` so the CLI's
    # auto-compact trigger fires against each model's REAL context window
    # instead of falling back to its hardcoded 256K default for everything.
    #
    # In-house/local models (provider == "inhouse") always report a flat 128K
    # regardless of the model's actual deployed window (kimi-k2.7-code is
    # really 262K, glm/qwen ~131K, etc.) — deliberately conservative so a CLI
    # user on a local model compacts a bit earlier rather than risk clipping.
    # Cloud models keep their real per-family window from
    # `gateway._context_window_for` (Claude 200K, GPT-5 256K, Gemini 1M, …).
    #
    # `autoCompactThresholdPercent` mirrors the ainxt-cli built-in default
    # (85%, see `DEFAULT_AUTO_COMPACT_THRESHOLD_PERCENT` in both
    # core.model_registry and ainxt-shell) so a client that reads it from the
    # catalog and one that falls back to its own default trigger identically.
    LOCAL_MODEL_CONTEXT_WINDOW = 128_000
    try:
        from gateway import _context_window_for as _cloud_context_window_for
    except Exception:
        _cloud_context_window_for = lambda _m: 128_000  # noqa: E731 — fail-open

    try:
        from core.model_registry import (
            DEFAULT_AUTO_COMPACT_THRESHOLD_PERCENT as _auto_compact_pct,
        )
    except Exception:
        _auto_compact_pct = 85

    def _with_context_window(m: dict) -> dict:
        is_local = m.get("provider") == "inhouse" or str(m["id"]).startswith("local")
        context_window = (
            LOCAL_MODEL_CONTEXT_WINDOW if is_local else _cloud_context_window_for(m["id"])
        )
        return {
            **m,
            "contextWindow": context_window,
            "autoCompactThresholdPercent": _auto_compact_pct,
        }

    return {
        "object": "list",
        "data": [
            _with_context_window(_with_max_output({"id": m["id"], "apiBackend": "messages", **m}))
            for m in models
        ],
    }