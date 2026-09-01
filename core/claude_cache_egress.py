# SPDX-License-Identifier: Apache-2.0
# ============================================================
# ANTHROPIC CACHE-CONTROL EGRESS TRANSPORT
# SINGLE INJECTION POINT • PCI SAFE • ENTERPRISE READY
# ============================================================
#
# WHY THIS EXISTS
# ---------------
# Prompt-cache markers used to be built by every call site that assembled an
# Anthropic payload (gateway_claude.generate, gateway_claude.generate_with_tools,
# llm_proxy.main._chat_claude / claude_tools_stream / _web_search_via_claude, and
# the LLM proxy gateway_claude). Five-plus builders meant five chances to drift apart,
# and any new endpoint silently opted out of caching.
#
# Now cache_control is added in exactly ONE place: the moment the request leaves
# the process for https://api.anthropic.com/v1/messages. Every SDK call —
# streaming, non-streaming, with_raw_response, tool-use — funnels through this
# transport, so the marker is always present and always identical.
#
# NO call site adds cache_control any more, so there is nothing to strip or
# reconcile here: this simply inserts one top-level key.
#
# WHY A TRANSPORT AND NOT AN EVENT HOOK
# -------------------------------------
# httpx request event hooks cannot change the outgoing body — by the time a hook
# runs the request stream is already bound, so mutating request.content has no
# effect on the bytes written to the wire. A transport is the only supported
# interception point that can rewrite a request body.
#
# WIRE SHAPE PRODUCED
# -------------------
#   {
#     "model": "...", "max_tokens": 100,
#     "cache_control": {"type": "ephemeral"},     <-- added here, top level
#     "system":   [ {"type": "text", "text": "..."} ],
#     "tools":    [ {...} ],
#     "messages": [ {"role": "user", "content": "..."} ]
#   }

import json
import os
from typing import Optional

import httpx

from core.logger import logger, get_request_id as _get_request_id


# ── Configuration ────────────────────────────────────────────────────────────
# Caching is on by default. ANTHROPIC_PROMPT_CACHE=false ships requests with no
# cache_control at all (Anthropic then caches nothing and bills no cache tokens).
def _cache_enabled() -> bool:
    return os.getenv("ANTHROPIC_PROMPT_CACHE", "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _build_marker() -> dict:
    """The cache_control object placed at the top level of the payload.

    ttl is omitted by default, which is what the Anthropic reference payload
    does (the provider then applies its 5m default). ANTHROPIC_CACHE_TTL=1h
    switches to the long-lived cache.
    """
    ttl = os.getenv("ANTHROPIC_CACHE_TTL", "").strip()
    return {"type": "ephemeral", "ttl": ttl} if ttl in ("5m", "1h") else {"type": "ephemeral"}


# Only these paths carry a cacheable Messages payload. count_tokens is included
# so token estimates are computed against the same body the real call sends.
_CACHEABLE_PATHS = ("/v1/messages", "/v1/messages/count_tokens")


def _is_messages_request(request: httpx.Request) -> bool:
    """True only for a POST of a Messages payload to the Anthropic API host."""
    return (
        request.method == "POST"
        and request.url.host.endswith("anthropic.com")
        and request.url.path.rstrip("/") in _CACHEABLE_PATHS
    )


def _inject(request: httpx.Request) -> None:
    """Add the top-level cache_control key to an outbound Messages request.

    Idempotent: the SDK re-sends the same Request object on retry, and setting
    the same key again leaves the body byte-identical.
    """
    # A streaming request body has no buffered .content; the Anthropic SDK always
    # sends a materialised JSON body, so bail out rather than guess.
    try:
        original = request.content
    except httpx.RequestNotRead:
        return
    if not original:
        return

    payload = json.loads(original)
    if not isinstance(payload, dict):
        return

    payload["cache_control"] = _build_marker()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # Rebind the buffered content AND the stream the transport reads from, then
    # correct Content-Length or the request is truncated / hangs.
    request._content = body
    request.stream = httpx.ByteStream(body)
    request.headers["content-length"] = str(len(body))

    logger.info(
        f"[CLAUDE CACHE EGRESS] {_get_request_id() or '-'} "
        f"url={request.url} cache_control={payload['cache_control']} "
        f"bytes={len(original)}→{len(body)}"
    )


def _maybe_inject(request: httpx.Request) -> None:
    """Inject on Anthropic Messages calls only; never raise.

    Failure policy: this is billing optimisation, never correctness. Any error is
    logged and the ORIGINAL request is forwarded, so a caching bug can never take
    down the inference path.
    """
    if not _cache_enabled() or not _is_messages_request(request):
        return
    try:
        _inject(request)
    except Exception as exc:
        logger.error(
            f"[CLAUDE CACHE EGRESS] {_get_request_id() or '-'} "
            f"injection skipped [{type(exc).__name__}]: {exc}"
        )


class AnthropicCacheControlTransport(httpx.AsyncBaseTransport):
    """Async transport that adds cache_control to outbound Messages calls.

    Wraps a real transport. Non-Anthropic requests, non-Messages endpoints, and
    bodies that are not JSON objects are forwarded byte-for-byte untouched.
    """

    def __init__(self, inner: Optional[httpx.AsyncBaseTransport] = None, **kwargs):
        self._inner = inner or httpx.AsyncHTTPTransport(**kwargs)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        _maybe_inject(request)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class SyncAnthropicCacheControlTransport(httpx.BaseTransport):
    """Sync counterpart for the blocking `Anthropic` client (LLM proxy gateway)."""

    def __init__(self, inner: Optional[httpx.BaseTransport] = None, **kwargs):
        self._inner = inner or httpx.HTTPTransport(**kwargs)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        _maybe_inject(request)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def build_cached_async_client(**kwargs) -> httpx.AsyncClient:
    """An httpx.AsyncClient whose Anthropic Messages calls carry cache_control.

    Pass as AsyncAnthropic(http_client=...) so every SDK method — create, stream,
    with_raw_response — is covered. Caller event_hooks are preserved for logging.
    """
    return httpx.AsyncClient(transport=AnthropicCacheControlTransport(), **kwargs)


def build_cached_sync_client(**kwargs) -> httpx.Client:
    """An httpx.Client whose Anthropic Messages calls carry cache_control.

    Pass as Anthropic(http_client=...) for the blocking SDK.
    """
    return httpx.Client(transport=SyncAnthropicCacheControlTransport(), **kwargs)
