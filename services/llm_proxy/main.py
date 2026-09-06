# SPDX-License-Identifier: MIT
# ============================================================
# LLM PROXY SERVICE  —  services/llm_proxy/main.py
# Port: 8003
#
# Purpose:
#   Runs on the web-server (the LLM proxy server) that has outbound internet
#   access via a Squid forward proxy.  The app-server cannot
#   reach external LLM APIs directly; it calls this service
#   over the internal network instead.
#
# Protocol (POST /llm/generate):
#   Request  — JSON body: { provider, prompt, model? }
#   Response — application/x-ndjson stream
#              Each line is one JSON object:
#                {"t": "<token text>"}         — a streamed token
#                {"m": {"in": N, "out": N}}    — final token-count metadata
#
# Forward proxy:
#   Set HTTPS_PROXY=http://squid-host:3128 in the environment on
#   the LLM proxy server.  All three gateway SDKs (Anthropic, OpenAI, google-genai)
#   read that env var and route through it automatically.
#   Leave HTTPS_PROXY empty (or unset) for local dev — calls go direct.
#
# Local simulation:
#   uvicorn services.llm_proxy.main:app --port 8003 --workers 2
#   # Then set LLM_PROXY_URL=http://localhost:8003 in .env
# ============================================================

from __future__ import annotations

# ── Make this folder the import root ──────────────────────────
# Ensures `from core.logger import logger` etc. resolve to the local copies
# inside services/llm_proxy/ regardless of how the process is started
# (uvicorn, gunicorn, direct python). NOTE: compliance is NOT done here — it
# lives in the backend gateway layer (Tier 1); this proxy forwards verbatim.
#
# IMPORTANT: sys.path must be patched BEFORE any `from core.logger import …`
# call. Previously the insert came after the first import, so Python resolved
# `core.logger` to the root-level core/logger.py (the gateway logger) which
# does NOT export set_conv_id. Every request to /llm/generate then crashed
# with "cannot import name 'set_conv_id' from 'core.logger'" → HTTP 500.
import sys as _sys, os as _os

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

from core.logger import logger
# Load .env from this directory (services/llm_proxy/.env) so credentials
# are available without needing them in the OS environment at launch time.
try:
    from dotenv import load_dotenv as _load_dotenv
    # override=True ensures .env values win over stale OS-level env vars
    # (e.g. rotated API keys that were exported in the shell before .env was updated)
    _load_dotenv(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".env"), override=True)
except Exception:
    pass

# CKMS — decrypt OPENAI_API_KEY / GEMINI_API_KEY / ANTHROPIC_API_KEY etc.
# before gateway_openai / gateway_gemini are imported below. This service is a
# standalone uvicorn entrypoint (port 8003) and is NOT booted via gateway.py,
# so it must initialize CKMS on its own. Idempotent if already booted.
#from core.ckms import load_at_boot as _ckms_load_at_boot
#_ckms_load_at_boot()
# ─────────────────────────────────────────────────────────────

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import List, Optional

import httpx as _httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from core.logger import logger

# ── Structured logger ──────────────────────────────────────────
'''
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | LLM-PROXY | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("llm_proxy")
'''
# ── Gateway singletons — internet LLMs only ───────────────────
# Local LLM (in-house GPU) is on the internal network — the gateway server calls
# it directly without needing this proxy. Only the three internet
# APIs (Anthropic, OpenAI, Gemini) live here.
_claude_gw  = None
_openai_gw  = None
_gemini_gw  = None

# ── Thread pool for sync gateway calls ───────────────────────────────────────
# LLM API calls are almost entirely I/O-wait (blocked on provider responses
# that can take 30–240 s). 200 threads is appropriate for this workload on
# web02 (4 uvicorn workers × ~50 concurrent streams each).
#
# Bounded queue (maxsize=400 = 2× max_workers):
#   ThreadPoolExecutor's default queue is unbounded. Under a traffic spike,
#   new work keeps queuing even when all threads are occupied waiting on slow
#   providers — memory grows without limit and latency spikes sharply before
#   any backpressure reaches the caller.
#   With maxsize=400, the 401st submission raises queue.Full immediately,
#   which we catch in _submit_to_pool() and convert to HTTP 503 so app02
#   can surface a clean "service busy" error instead of silently queuing.
#
# Observability: _pool_stats() exposes active/pending counts in /health.

import queue as _queue
import threading as _pool_threading

_POOL_MAX_WORKERS = 200
_POOL_QUEUE_MAX   = _POOL_MAX_WORKERS * 2   # 400 — reject beyond this

# Counters for /health observability (updated atomically)
_pool_active  = 0
_pool_pending = 0
_pool_stats_lock = _pool_threading.Lock()


def _pool_stats() -> dict:
    with _pool_stats_lock:
        return {"active": _pool_active, "pending": _pool_pending, "max_workers": _POOL_MAX_WORKERS}


class _BoundedThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor with a bounded work queue.

    Raises queue.Full when the queue is at capacity so callers can
    convert it to a 503 instead of queuing indefinitely.
    """
    def __init__(self, max_workers: int, queue_maxsize: int, **kwargs):
        super().__init__(max_workers=max_workers, **kwargs)
        # Replace the internal unbounded SimpleQueue with a bounded Queue.
        # _work_queue is the internal attribute used by ThreadPoolExecutor.
        self._work_queue = _queue.Queue(maxsize=queue_maxsize)  # type: ignore[assignment]


_pool = _BoundedThreadPoolExecutor(
    max_workers=_POOL_MAX_WORKERS,
    queue_maxsize=_POOL_QUEUE_MAX,
    thread_name_prefix="llm-proxy",
)


async def _run_in_pool(loop, fn, *args):
    """Async wrapper around loop.run_in_executor(_pool, fn, *args).

    Catches queue.Full (raised by _BoundedThreadPoolExecutor when the work
    queue is at capacity) and converts it to HTTP 503 so the caller gets
    clean backpressure instead of an unhandled exception.

    Also updates the active/pending counters used by /health.
    """
    global _pool_active, _pool_pending

    with _pool_stats_lock:
        _pool_pending += 1

    def _tracked():
        global _pool_active, _pool_pending
        with _pool_stats_lock:
            _pool_pending -= 1
            _pool_active  += 1
        try:
            return fn(*args)
        finally:
            with _pool_stats_lock:
                _pool_active -= 1

    try:
        return await loop.run_in_executor(_pool, _tracked)
    except _queue.Full:
        with _pool_stats_lock:
            _pool_pending -= 1
        raise HTTPException(
            status_code=503,
            detail="LLM proxy is at capacity — too many concurrent requests. Retry shortly.",
        )

# Persistent async HTTP client for Atlassian proxy calls.
# The previous pattern (async with httpx.AsyncClient(...)) created a fresh
# TCP connection per request — eliminated here with a module-level singleton.
_atlassian_http: Optional[_httpx.AsyncClient] = None

# Persistent async HTTP clients for the /spend/* admin-API passthroughs.
# Kept separate per-provider so connection pools don't collide on auth headers
# or get polluted by long Atlassian responses. Initialized in _lifespan().
_anthropic_admin_http: Optional[_httpx.AsyncClient] = None
_openai_admin_http:    Optional[_httpx.AsyncClient] = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Eagerly load internet LLM gateways at startup."""
    global _claude_gw, _openai_gw, _gemini_gw, _atlassian_http
    global _anthropic_admin_http, _openai_admin_http

    # ── Step 1: fetch API keys from app02 ─────────────────────────────────
    # ProxyKeyCache.load() calls GET /internal/ckms/proxy-keys on app02
    # (PROXY_KEY_FETCH_URL) and stores the plaintext keys in memory.
    # Non-fatal: if the fetch fails or PROXY_KEY_FETCH_URL is unset (local
    # dev), the gateway constructors below fall back to os.getenv() and read
    # from services/llm_proxy/.env as they do today — nothing breaks.
    try:
        from core.proxy_key_client import ProxyKeyCache
        ProxyKeyCache.load()
        _pkc = ProxyKeyCache.instance()
    except Exception as _pkc_err:
        logger.error(f"ProxyKeyCache load error (non-fatal): {_pkc_err}")
        # Create a no-op cache so the .get() calls below are safe
        class _NoopCache:
            def get(self, key: str) -> str: return ""
        _pkc = _NoopCache()

    # ── Step 2: construct gateway singletons using fetched keys ───────────
    # Each constructor receives the key from ProxyKeyCache first; if that
    # returns "" (fetch failed / local dev), it falls back to os.getenv().
    try:
        from gateway_claude import ClaudeGateway
        _claude_gw = ClaudeGateway(
            api_key=_pkc.get("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
        )
        logger.info("Claude gateway loaded ✓")
    except Exception as e:
        logger.error(f"Claude gateway unavailable: {e}")

    try:
        from gateway_openai import OpenAIGateway
        _openai_gw = OpenAIGateway(
            api_key=_pkc.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
        )
        logger.info("OpenAI gateway loaded ✓")
    except Exception as e:
        logger.error(f"OpenAI gateway unavailable: {e}")

    try:
        from gateway_gemini import GeminiGateway
        _gemini_gw = GeminiGateway(
            api_key=_pkc.get("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
        )
        logger.info("Gemini gateway loaded ✓")
    except Exception as e:
        logger.error(f"Gemini gateway unavailable: {e}")

    # ── Step 3: write CKMS-fetched env keys into os.environ ──────────────
    # These keys are consumed by downstream code via os.getenv() across
    # doc_worker, ocr_pipeline, endpoint routers, embed_svc, etc.
    # Writing them here — once, at startup — means all those callers pick up
    # the CKMS-decrypted value without any code change on their side.
    # Fallback: if _pkc.get() returns "" (local dev / fetch failed), the
    # existing os.environ value (from .env) is left untouched.
    for _env_key in ("GOOGLE_API_KEY", "LITELLM_API_KEY", "LOCAL_LLM_API_KEY", "NOMIC_EMBED_API_KEY", "FIMI_OPENAI_API_KEY", "FIMI_ANTHROPIC_API_KEY"):
        _fetched = _pkc.get(_env_key)
        if _fetched:
            os.environ[_env_key] = _fetched
            logger.info(f"ProxyKeyCache: {_env_key} written to os.environ ✓")

    # Persistent async HTTP client for Atlassian proxy — avoids per-request TCP setup
    _atlassian_http = _httpx.AsyncClient(
        timeout=30.0,
        limits=_httpx.Limits(
            max_connections=50,
            max_keepalive_connections=25,
            keepalive_expiry=30.0,
        ),
    )
    logger.info("Atlassian HTTP client initialised ✓ (max_conn=50, keepalive=25)")

    # Persistent admin-API HTTP clients for the /spend/* endpoints. Same
    # connection-pool limits as the Atlassian client; longer timeout because
    # the admin cost/usage endpoints can be slow when paginating large windows.
    _anthropic_admin_http = _httpx.AsyncClient(
        timeout=180.0,
        limits=_httpx.Limits(
            max_connections=50,
            max_keepalive_connections=25,
            keepalive_expiry=30.0,
        ),
    )
    _openai_admin_http = _httpx.AsyncClient(
        timeout=180.0,
        limits=_httpx.Limits(
            max_connections=50,
            max_keepalive_connections=25,
            keepalive_expiry=30.0,
        ),
    )
    logger.info("Spend-API HTTP clients initialised ✓ (anthropic_admin, openai_admin)")

    logger.info("LLM Proxy ready — listening for requests")
    yield
    _pool.shutdown(wait=False)
    if _atlassian_http:
        await _atlassian_http.aclose()
    if _anthropic_admin_http:
        await _anthropic_admin_http.aclose()
    if _openai_admin_http:
        await _openai_admin_http.aclose()
    logger.info("LLM Proxy shut down")


app = FastAPI(
    title="AiNxt LLM Proxy",
    description="Outbound LLM API proxy — runs on internet-accessible web server",
    version="1.0.0",
    lifespan=_lifespan,
)


# ── Internal-token authentication middleware ───────────────────
#
# DAST fix: "The application relies solely on IP-based access controls
# for protecting sensitive functionalities without additional
# authentication measure."
#
# The LLM proxy service (configured via LLM_PROXY_URL) is an internal microservice
# that proxies external LLM API calls (Claude, OpenAI, Gemini) and
# Atlassian Cloud calls.  Previously it relied solely on network-level
# IP restrictions to prevent unauthorised access.  A compromised host
# on the same network segment, or an attacker who can reach port 8003
# via a VPN / proxy, could call external APIs at the platform's cost
# without any form of identity or secret validation.
#
# Fix: every sensitive endpoint now requires a pre-shared secret in the
# "X-Internal-Token" HTTP header.  Set the same value in:
#   • LLM proxy server .env  →  LLM_PROXY_TOKEN=<32+ char random secret>
#   • gateway server .env  →  LLM_PROXY_TOKEN=<same value>
# The gateway server injects the header on every outbound call to the LLM proxy server.
# The /health endpoint is explicitly exempted (used by load-balancer
# probes and monitoring that do not carry credentials).
#
# Network-level IP restrictions remain as a defence-in-depth layer but
# are no longer the *sole* protection.

_INTERNAL_TOKEN_EXEMPT_PATHS = frozenset({"/health", "/docs", "/redoc", "/openapi.json"})


class _InternalTokenMiddleware(BaseHTTPMiddleware):
    """
    Warn when requests to sensitive endpoints do not carry the expected
    X-Internal-Token pre-shared secret.

    Behaviour:
    - /health and OpenAPI docs are always allowed through.
    - If LLM_PROXY_TOKEN is missing on this server, log a warning and allow.
    - If the incoming header is missing or mismatched, log a warning and allow.

    This is a staged rollout mode for SEC-F-004 so existing callers are not
    broken while missing header propagation is being fixed across the codebase.
    """

    async def dispatch(self, request: Request, call_next):
        # Exempt health / docs paths
        if request.url.path in _INTERNAL_TOKEN_EXEMPT_PATHS:
            return await call_next(request)

        expected = os.getenv("LLM_PROXY_TOKEN", "")
        incoming = request.headers.get("X-Internal-Token", "") or ""

        if not expected:
            logger.warning(
                "llm_proxy: LLM_PROXY_TOKEN is not configured; allowing request in warn-only mode "
                "path=%s",
                request.url.path,
            )
            return await call_next(request)

        if not incoming:
            logger.warning(
                "llm_proxy: missing X-Internal-Token header; allowing request in warn-only mode "
                "path=%s",
                request.url.path,
            )
            return await call_next(request)

        if not secrets.compare_digest(incoming, expected):
            logger.warning(
                "llm_proxy: invalid X-Internal-Token header; allowing request in warn-only mode "
                "path=%s",
                request.url.path,
            )
            return await call_next(request)

        return await call_next(request)


app.add_middleware(_InternalTokenMiddleware)


# ── Request size logging middleware ────────────────────────────
# Logs body size of every incoming request so we can diagnose
# large-prompt issues without guessing. Also catches body-too-large
# errors from h11/httptools before they become silent 400s.

_MAX_BODY_BYTES = 32 * 1024 * 1024  # 32 MB hard limit


class _BodySizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            size = int(content_length)
            logger.info(f"INCOMING {request.method} {request.url.path} | body={size} bytes")
            if size > _MAX_BODY_BYTES:
                logger.error(f"Request body too large: {size} bytes > {_MAX_BODY_BYTES} limit")
                return JSONResponse(status_code=413, content={"detail": "Request body too large"})
        try:
            response = await call_next(request)
            return response
        except Exception as exc:
            logger.error(f"Middleware caught unhandled error: {exc}")
            return JSONResponse(status_code=500, content={"detail": str(exc)})


app.add_middleware(_BodySizeMiddleware)


# ── Request schema ─────────────────────────────────────────────

class GenerateRequest(BaseModel):
    provider:       str                   # claude | openai | gemini | local_llm
    prompt:         Optional[str] = None  # flat-string prompt; mutually exclusive with content_blocks/messages
    messages:       Optional[list] = None # OpenAI-format multi-turn messages [{role, content}, ...]
    content_blocks: Optional[list] = None # structured blocks [{text, cache}]; claude only
    model:          Optional[str] = None  # override model (Claude only currently)
    request_id:     Optional[str] = None  # caller's request_id for log correlation
    chat_id:        Optional[str] = None  # caller's chat_id for conversation correlation
    conv_id:        Optional[str] = None  # stable per-conversation ID (x-ainxt-conv-id)
    # Compliance (PCI/PII detection + redaction) is performed EXCLUSIVELY in the
    # backend gateway layer (Tier 1) before the request reaches this proxy. This
    # service forwards already-validated, already-redacted text verbatim and runs
    # NO compliance of its own (it is deployed standalone on the LLM proxy server and does not
    # import gateway-only packages).
    #
    # These two fields are accepted only for backward-compat with older backend
    # callers that still send them; they are IGNORED by this service. Pydantic
    # would otherwise reject unknown fields on strict models — keeping them as
    # declared-but-unused avoids 422s during a rolling deploy.
    compliance_precleared: bool = False
    compliance_findings:   Optional[list] = None


class GenerateImageRequest(BaseModel):
    provider:    str = "gemini"        # only gemini supported for now
    prompt:      str
    image_b64:   str                   # base64-encoded image bytes (legacy single-image field — always kept populated by callers for back-compat)
    mime_type:   str = "image/jpeg"
    system_prompt: str = ""
    request_id:  Optional[str] = None # caller's request_id for log correlation
    chat_id:     Optional[str] = None # caller's chat_id for conversation correlation
    # Optional multi-image fields (additive, backward-compatible). Older
    # clients that only ever send image_b64/mime_type keep working exactly
    # as before — these are simply absent/empty on their requests.
    images_b64:  Optional[List[str]] = None
    mime_types:  Optional[List[str]] = None


class PptImageRequest(BaseModel):
    """Text → image generation for PPT slides. Routed through Gemini Imagen or DALL-E 3."""
    provider:   str = "auto"           # auto | gemini | dalle
    prompt:     str                    # image description from LLM
    request_id: Optional[str] = None
    chat_id:    Optional[str] = None

class ImagenRequest(BaseModel):
    """Text → image generation request.

    `provider` accepts only approved providers per the AiNxt policy:
       "gemini" → Imagen-3 Fast on Vertex/Gemini API
       "openai" → DALL-E 3 (HD)
    Stock photo APIs (Pexels / Unsplash / etc.) must never be added.
    """
    provider:        str   = "gemini"
    prompt:          str
    aspect_ratio:    str   = "16:9"        # 1:1 | 16:9 | 9:16 | 4:3 | 3:4
    number_of_images: int  = 1             # 1..4
    style_suffix:    str   = ""            # extra style instructions (e.g. "vector flat")


class VeoRequest(BaseModel):
    """Text → video generation request (Veo 3.1, Gemini provider only).

    Veo is a Long-Running Operation: the proxy polls until done, then
    streams the MP4 bytes back to the caller. The caller (main gateway)
    is already responsible for auth, per-user gating, and billing — this
    endpoint is purely the cloud-egress shim that wraps the Google SDK.
    """
    prompt:        str
    model:         str = ""                # caller may override; defaults to registry VEO_MODEL
    aspect_ratio:  str = "16:9"            # 16:9 | 9:16 (Veo supports a limited set)
    duration_secs: int = 8                 # 2..16 (clamped server-side)

# ── Helpers ────────────────────────────────────────────────────

def _resolve_gateway(provider: str):
    """Return (gateway_instance, supports_model_kwarg)."""
    if provider == "claude":
        if _claude_gw is None:
            raise HTTPException(503, "Claude gateway not available")
        return _claude_gw, True
    if provider == "openai":
        if _openai_gw is None:
            raise HTTPException(503, "OpenAI gateway not available")
        return _openai_gw, True   # model kwarg now supported via generate(prompt, model=...)
    if provider == "gemini":
        if _gemini_gw is None:
            raise HTTPException(503, "Gemini gateway not available")
        return _gemini_gw, True   # model kwarg supported via generate()/async_generate(model=...)
    raise HTTPException(400, f"Unknown provider: {provider!r}. Use claude|openai|gemini")


def _run_sync_generator(
        req_id: str,
        provider: str,
        gw,
        prompt,               # Optional[str] — None when content_blocks provided
        model: Optional[str],
        supports_model: bool,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        resolved_model: str = "",
        content_blocks=None,  # Optional[list] — structured content blocks (claude only)
):
    """
    Runs in a thread-pool worker.
    Iterates the sync gateway generator, pushing JSON-line strings
    into `queue` so the async StreamingResponse can yield them.
    Sends a final {"m": ...} line with real token counts, resolved model,
    Puts None as sentinel.
    """
    # Re-bind thread-local context — thread-locals do NOT cross thread boundaries
    from core.logger import set_request_id
    set_request_id(req_id)

    def _put(item):
        # Use a Future to safely propagate the put onto the event loop
        # and block the thread until the queue accepts the item.
        # This prevents QueueFull from silently dropping the sentinel.
        future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        try:
            future.result(timeout=30)  # wait up to 30s for consumer to drain
        except Exception as e:
            logger.warning(f"[{req_id}] queue put failed: {e}")

    t0 = time.monotonic()
    out_tokens = 0
    # Mutable container so _drain_async_gen can write token counts back to the
    # thread scope. asyncio.run() copies the calling thread's ContextVar context
    # into a new context for the coroutine; any ContextVar.set() calls inside
    # the coroutine are in that copy and do NOT propagate back to the thread's
    # context. Reading gw._last_* properties (which use ContextVar) OUTSIDE
    # asyncio.run() always returns 0. Fix: capture inside the coroutine.
    _async_token_data: dict = {"in": 0, "out": 0}
    if content_blocks is not None:
        _mode = "content_blocks"
    elif isinstance(prompt, list):
        _mode = "messages"
    else:
        _mode = "prompt"
    _payload_chars = (
        sum(len(b.get("text", "")) for b in content_blocks)
        if content_blocks is not None
        else (
            sum(len(str(m.get("content", ""))) for m in prompt)
            if isinstance(prompt, list)
            else len(str(prompt or ""))
        )
    )
    logger.info(
        f"[PROXY HOP-3] {req_id} {provider} generator starting "
        f"model={resolved_model!r} mode={_mode} "
        + (f"blocks={len(content_blocks)} chars={_payload_chars}"
           if content_blocks is not None else f"prompt_chars={_payload_chars}")
    )
    try:
        # content_blocks path: structured multi-block call
        if content_blocks is not None:
            gen = gw.generate(content_blocks=content_blocks, model=model)
        else:
            # Text is already validated + redacted by the backend gateway layer
            # (Tier 1); this proxy forwards it verbatim. No compliance kwargs.
            _gen_kwargs: dict = {}
            if supports_model and model:
                _gen_kwargs["model"] = model
            gen = gw.generate(prompt, **_gen_kwargs)

        # ClaudeGateway.generate() is an async generator (async def + yield).
        # _run_sync_generator runs in a thread-pool so there is no running event
        # loop here — use asyncio.run() to drain the async generator safely.
        import inspect
        if inspect.isasyncgen(gen):
            logger.info(f"[PROXY HOP-3] {req_id} asyncio.run() entering for async generator")
            async def _drain_async_gen():
                nonlocal out_tokens
                async for token in gen:
                    if not token:
                        continue
                    out_tokens += 1
                    # A dict yield is a pre-shaped ndjson payload (e.g. {"r": ...}
                    # reasoning delta). Serialize directly; else wrap as {"t": ...}.
                    if isinstance(token, dict):
                        _put(json.dumps(token) + "\n")
                    else:
                        _put(json.dumps({"t": token}) + "\n")
                # Capture token counts HERE — inside the same asyncio context
                # where ClaudeGateway's ContextVars were written during streaming.
                # Reads outside asyncio.run() see the thread's original context (0).
                _async_token_data["in"]  = getattr(gw, "_last_input_tokens",  0) or 0
                _async_token_data["out"] = getattr(gw, "_last_output_tokens", 0) or 0
            asyncio.run(_drain_async_gen())
            logger.info(
                f"[PROXY HOP-3] {req_id} asyncio.run() complete "
                f"tokens={_async_token_data}"
            )
        else:
            for token in gen:
                if not token:
                    continue
                out_tokens += 1
                # A dict yield is a pre-shaped ndjson payload (e.g. {"r": ...}
                # reasoning delta). Serialize directly; else wrap as {"t": ...}.
                if isinstance(token, dict):
                    _put(json.dumps(token) + "\n")
                else:
                    _put(json.dumps({"t": token}) + "\n")
    except Exception as e:
        import traceback as _tb
        # Structured ERROR before relaying the {"error"} line so the drain failure
        # (e.g. "Event loop is closed" from a cross-loop async client) is greppable
        # by req_id/provider. Keep the existing prefixed log for the full traceback.
        logger.error("[llm_proxy] async drain error", req_id=req_id, provider=provider, error=str(e))
        logger.error(
            f"[PROXY HOP-3] {req_id} {provider} generation failed "
            f"[{type(e).__name__}]: {e}\n{_tb.format_exc()}"
        )
        _put(json.dumps({"error": str(e)}) + "\n")
    finally:
        # For async generators: prefer token data captured inside asyncio.run() context.
        # For sync generators: fall back to gw attributes (set in the same thread).
        if _async_token_data["in"] or _async_token_data["out"]:
            in_tok  = _async_token_data["in"]
            out_tok = _async_token_data["out"]
        else:
            in_tok  = getattr(gw, "_last_input_tokens",  0) or 0
            out_tok = getattr(gw, "_last_output_tokens", 0) or 0
        elapsed = time.monotonic() - t0
        logger.info(
            f"[{req_id}] {provider} done | model={resolved_model} | "
            f"in={in_tok} out={out_tok} tokens | "
            f"latency={elapsed:.2f}s"
        )
        _put(json.dumps({"m": {
            "in":    in_tok,
            "out":   out_tok,
            "model": resolved_model,
        }}) + "\n")
        _put(None)  # sentinel — end of stream


# ── Main endpoint ──────────────────────────────────────────────

@app.post("/llm/generate")
async def generate(req: GenerateRequest, request: Request):
    """
    Stream tokens from the requested LLM provider.

    Response: application/x-ndjson
      Each newline-terminated JSON object is either:
        {"t": "token text"}              — a streamed text token
        {"m": {"in": N, "out": N}}       — final real token counts (last line)
        {"error": "message"}             — generation failed; client must raise
    """
    # ── Correlation ID binding ─────────────────────────────────────────────────
    # Accept request_id / chat_id / conv_id from the JSON body (preferred) or
    # from HTTP headers (alternative).  The local req_id (short UUID) is kept
    # for internal log prefixing; all three IDs are bound to the thread-local
    # logger so every log line in this request carries the same identifiers as
    # the originating gateway log entry.
    from core.logger import set_request_id, set_chat_context, set_conv_id
    upstream_request_id = (
            req.request_id
            or request.headers.get("x-request-id")
            or request.headers.get("X-Request-ID")
    )
    upstream_chat_id = (
            req.chat_id
            or request.headers.get("x-chat-id")
            or request.headers.get("X-Chat-ID")
    )
    upstream_conv_id = (
            req.conv_id
            or request.headers.get("x-ainxt-conv-id")
            or request.headers.get("X-AiNxt-Conv-Id")
            or ""
    )
    req_id = upstream_request_id or str(uuid.uuid4())
    set_request_id(req_id)
    if upstream_chat_id:
        set_chat_context("-", upstream_chat_id)
    if upstream_conv_id:
        set_conv_id(upstream_conv_id)
    # ──────────────────────────────────────────────────────────────────────────
    caller = request.client.host if request.client else "unknown"
    # Build a preview from whichever input mode is present so logs are useful in all paths
    if req.messages:
        try:
            _last_user_preview = next(
                (m.get("content", "") for m in reversed(req.messages) if m.get("role") == "user"),
                "",
            )
        except Exception:
            _last_user_preview = ""
        prompt_preview = (_last_user_preview or "")[:80].replace("\n", " ")
    else:
        prompt_preview = (req.prompt or "")[:80].replace("\n", " ")

    # Mutual-exclusion check across the three input modes: prompt / messages / content_blocks
    _modes_set = sum(x is not None for x in (req.prompt, req.messages, req.content_blocks))
    if _modes_set > 1:
        raise HTTPException(400, "Send exactly one of 'prompt', 'messages', or 'content_blocks'")
    if req.content_blocks is not None and req.provider != "claude":
        raise HTTPException(400, f"content_blocks is only supported for provider='claude'; got {req.provider!r}")
    if req.content_blocks is not None:
        # Diagnostic: log block sizes
        _block_summary = " | ".join(
            f"block{_bi}:{len(_b.get('text',''))}chars"
            for _bi, _b in enumerate(req.content_blocks)
        )
        logger.info(
            f"[{req_id}] content_blocks: {len(req.content_blocks)} blocks | {_block_summary}"
        )

    # Resolve the actual model ID that will be sent to the provider
    from core.model_registry import (
        CLAUDE_PRIMARY_MODEL as _DEFAULT_CLAUDE,
        OPENAI_CODING_MODEL  as _DEFAULT_OPENAI,
        # Gemini chat default = text/coding model. GEMINI_VISION_MODEL now aliases
        # to the image model and would 400 on /chat dispatch — use GEMINI_TEXT_MODEL
        # so unspecified-model callers land on gemini-3.5-flash.
        GEMINI_TEXT_MODEL    as _DEFAULT_GEMINI,
    )
    _default_map = {"claude": _DEFAULT_CLAUDE, "openai": _DEFAULT_OPENAI, "gemini": _DEFAULT_GEMINI}
    resolved_model = req.model or _default_map.get(req.provider, req.provider)

    if req.content_blocks is not None:
        _mode = "content_blocks"
    elif req.messages is not None:
        _mode = "messages"
    else:
        _mode = "prompt"
    logger.info(
        f"[{req_id}] REQUEST from {caller} | provider={req.provider} | "
        f"model={resolved_model} | mode={_mode} | prompt={prompt_preview!r}... "
        f"conv_id={upstream_conv_id or '-'}"
    )

    gw, supports_model = _resolve_gateway(req.provider)

    async def _stream():
        in_tok = out_tok = 0
        t0 = time.monotonic()
        logger.info(f"[{req_id}] LLM DISPATCH → provider={req.provider} model={resolved_model}")

        # ── Unified native-async streaming for all providers ──────────────────
        # All three gateways now expose async_generate() which runs entirely on
        # the uvicorn event loop — no thread-pool, no asyncio.Queue, no blocking
        # _put(). Each token is yielded the instant the provider pushes it,
        # giving the same per-token SSE experience as the CLI path (/v1/messages).
        #
        # Claude:  ClaudeGateway.async_generate()  — AsyncAnthropic SDK
        # OpenAI:  OpenAIGateway.async_generate()  — AsyncOpenAI SDK
        # Gemini:  GeminiGateway.async_generate()  — genai.Client.aio.models
        #
        # content_blocks is Claude-only (enforced by the request validator above).
        _gw_prompt = req.messages if req.messages is not None else req.prompt
        _gen_kwargs: dict = {}
        if supports_model and resolved_model:
            _gen_kwargs["model"] = resolved_model

        if req.provider == "claude" and req.content_blocks is not None:
            _gen_kwargs["content_blocks"] = req.content_blocks
        gen = gw.async_generate(_gw_prompt, **_gen_kwargs)

        try:
            async for token in gen:
                if not token:
                    continue
                # A dict yield is a pre-shaped ndjson payload (e.g. {"r": ...}
                # reasoning delta from OpenAI gpt-5.4). Serialize it directly so
                # the gateway can distinguish reasoning from content tokens.
                if isinstance(token, dict):
                    yield json.dumps(token) + "\n"
                else:
                    yield json.dumps({"t": token}) + "\n"
        except Exception as e:
            logger.error(
                f"[{req_id}] {req.provider} generation error [{type(e).__name__}]: {e}"
            )
            yield json.dumps({"error": str(e)}) + "\n"
        finally:
            # Token counts are set on gw by usage events inside the async generator
            # and are readable here on the same event loop (ContextVar for Claude,
            # thread-local for OpenAI/Gemini — both safe since we're on one loop).
            in_tok  = getattr(gw, "_last_input_tokens",  0) or 0
            out_tok = getattr(gw, "_last_output_tokens", 0) or 0
            elapsed = time.monotonic() - t0
            logger.info(
                f"[{req_id}] {req.provider} done | model={resolved_model} | "
                f"in={in_tok} out={out_tok} tokens | latency={elapsed:.2f}s"
            )
            yield json.dumps({"m": {
                "in":    in_tok,
                "out":   out_tok,
                "model": resolved_model,
            }}) + "\n"

    return StreamingResponse(
        _stream(), media_type="application/x-ndjson",
        # Disable any intermediary buffering so ndjson token lines flush
        # immediately across the internal gateway↔LLM-proxy hop (per-token streaming).
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ── Claude tools-stream endpoint ──────────────────────────────
#
# Used by gateway.py's _tools_claude_stream() for IDE/Kilo Code
# agentic tool-calling.  The full Anthropic messages + tools payload
# is sent here; this endpoint streams back ndjson events using a
# compact protocol so gateway.py can reconstruct OpenAI-format SSE.
#
# ndjson line formats:
#   {"tbs": {"index": N, "id": "...", "name": "..."}}  — tool block start
#   {"tad": {"index": N, "partial_json": "..."}}        — tool args delta
#   {"txt": {"text": "..."}}                            — text delta
#   {"stop": "tool_calls|stop", "in_tok": N, "out_tok": N} — stream end
# ──────────────────────────────────────────────────────────────

class ClaudeToolsRequest(BaseModel):
    messages:   list                        # Anthropic-format messages array
    tools:      list        = []            # Anthropic tool definitions
    system:     str         = ""
    model:      str         = ""
    max_tokens: int         = 64000
    request_id: Optional[str] = None       # caller's request_id for log correlation
    chat_id:    Optional[str] = None       # caller's chat_id for conversation correlation
    conv_id:    Optional[str] = None       # stable per-conversation ID (x-ainxt-conv-id)


@app.post("/llm/claude-tools-stream")
async def claude_tools_stream(req: ClaudeToolsRequest, request: Request):
    """
    Stream Anthropic tool-call events for the IDE/Kilo Code agentic path.
    Requires provider=claude (only Claude supports native tool-use streaming).
    Uses AsyncAnthropic's async context manager — no thread pool needed.
    """
    if _claude_gw is None:
        raise HTTPException(503, "Claude gateway not available")

    # ── Correlation ID binding ─────────────────────────────────────────────────
    from core.logger import set_request_id, set_chat_context, set_conv_id
    upstream_request_id = (
            req.request_id
            or request.headers.get("x-request-id")
            or request.headers.get("X-Request-ID")
    )
    upstream_chat_id = (
            req.chat_id
            or request.headers.get("x-chat-id")
            or request.headers.get("X-Chat-ID")
    )
    upstream_conv_id = (
            req.conv_id
            or request.headers.get("x-ainxt-conv-id")
            or request.headers.get("X-AiNxt-Conv-Id")
            or ""
    )
    req_id = upstream_request_id or str(uuid.uuid4())
    set_request_id(req_id)
    if upstream_chat_id:
        set_chat_context("-", upstream_chat_id)
    if upstream_conv_id:
        set_conv_id(upstream_conv_id)
    # ──────────────────────────────────────────────────────────────────────────
    from core.model_registry import CLAUDE_PRIMARY_MODEL
    model = req.model or CLAUDE_PRIMARY_MODEL
    logger.info(
        f"[{req_id}] TOOLS-STREAM | model={model} | "
        f"msgs={len(req.messages)} tools={len(req.tools)} "
        f"conv_id={upstream_conv_id or '-'}"
    )
    _bound_chat_id = upstream_chat_id or "-"

    async def _stream():
        # Re-bind thread-local context — thread-locals do NOT cross thread boundaries
        from core.logger import set_request_id as _sri, set_chat_context as _scc, set_conv_id as _scv
        _sri(req_id)
        if _bound_chat_id != "-":
            _scc("-", _bound_chat_id)
        if upstream_conv_id:
            _scv(upstream_conv_id)
        in_tok = out_tok = cache_read_tok = cache_creation_tok = 0
        try:
            logger.info(f"[{req_id}] LLM DISPATCH → provider=claude model={model}")
            # No cache breakpoints here. The gateway's httpx transport adds one
            # top-level cache_control as the request leaves for api.anthropic.com
            # (see core/claude_cache_egress.py).
            _sys_text = req.system or "You are a helpful AI coding assistant."
            _stream_kwargs: dict = {
                "model":      model,
                "max_tokens": req.max_tokens,
                "system":     [{"type": "text", "text": _sys_text}],
                "messages":   req.messages,
            }
            if req.tools:
                _stream_kwargs["tools"] = req.tools
            # Claude Opus 4+ and Sonnet 5 have deprecated the temperature parameter —
            # gate via the shared helper in gateway_claude so every new family lands in one place.
            from gateway_claude import _no_temperature_model
            if not _no_temperature_model(model):
                _stream_kwargs["temperature"] = 0
            async with _claude_gw.client.messages.stream(**_stream_kwargs) as stream:
                async for ev in stream:
                    etype = getattr(ev, "type", "")

                    if etype == "message_start":
                        # Cache token counts arrive on message_start, not message_delta
                        _ms_u = getattr(getattr(ev, "message", None), "usage", None)
                        if _ms_u:
                            in_tok             = getattr(_ms_u, "input_tokens",              0) or 0
                            cache_read_tok     = getattr(_ms_u, "cache_read_input_tokens",     0) or 0
                            cache_creation_tok = getattr(_ms_u, "cache_creation_input_tokens", 0) or 0
                            logger.info(
                                f"[{req_id}] claude tools-stream cache "
                                f"cache_read={cache_read_tok} "
                                f"cache_creation={cache_creation_tok} "
                                f"input_tokens={in_tok}"
                            )
                            from gateway_claude import _log_cache_effectiveness as _lce_claude
                            _lce_claude(
                                request_id=req_id,
                                model=model,
                                cache_read=cache_read_tok,
                                cache_created=cache_creation_tok,
                                prompt_total=in_tok,
                                context="tools-stream",
                            )

                    elif etype == "content_block_start":
                        cb = getattr(ev, "content_block", None)
                        if cb and getattr(cb, "type", "") == "tool_use":
                            idx = getattr(ev, "index", 0)
                            yield json.dumps({"tbs": {"index": idx, "id": cb.id, "name": cb.name}}) + "\n"

                    elif etype == "content_block_delta":
                        delta = getattr(ev, "delta", None)
                        idx   = getattr(ev, "index", 0)
                        if delta:
                            if getattr(delta, "type", "") == "text_delta":
                                yield json.dumps({"txt": {"text": delta.text}}) + "\n"
                            elif getattr(delta, "type", "") == "input_json_delta":
                                yield json.dumps({"tad": {"index": idx, "partial_json": delta.partial_json}}) + "\n"

                    elif etype == "message_delta":
                        d = getattr(ev, "delta", None)
                        u = getattr(ev, "usage", None)
                        if u:
                            out_tok = getattr(u, "output_tokens", 0) or 0
                        if d:
                            stop = getattr(d, "stop_reason", None)
                            if stop:
                                finish = "tool_calls" if stop == "tool_use" else "stop"
                                yield json.dumps({
                                    "stop":              finish,
                                    "in_tok":            in_tok,
                                    "out_tok":           out_tok,
                                    "cache_read_tok":    cache_read_tok,
                                    "cache_creation_tok": cache_creation_tok,
                                    "model":             model,
                                }) + "\n"

        except Exception as exc:
            logger.error(f"[{req_id}] claude tools-stream error: {exc}")
            # Anthropic SDK errors (anthropic.APIStatusError and subclasses,
            # e.g. BadRequestError) carry a real HTTP status_code and a
            # structured body — surface both so the caller (messages_compat_router)
            # can classify the failure correctly instead of collapsing every
            # error into an opaque "api_error" type. Without this, a
            # deterministic 400 (e.g. "max_tokens exceeds the model's output
            # ceiling") is indistinguishable from a transient 5xx, and the CLI
            # retries a request that can never succeed until it exhausts its
            # retry budget.
            _status_code = getattr(exc, "status_code", None)
            _err_type = "api_error"
            try:
                _body = getattr(exc, "body", None)
                if isinstance(_body, dict):
                    _err_type = (_body.get("error") or {}).get("type") or _err_type
            except Exception:
                pass
            yield json.dumps({
                "error": str(exc),
                "status_code": _status_code,
                "error_type": _err_type,
            }) + "\n"
        finally:
            logger.info(
                f"[{req_id}] tools-stream done | model={model} | "
                f"in={in_tok} out={out_tok} "
                f"cache_read={cache_read_tok} cache_creation={cache_creation_tok}"
            )

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


# ── OpenAI tools-stream endpoint ──────────────────────────────
#
# POST /llm/openai-tools-stream
#
# Used by gateway.py's _tools_proxy_stream() for IDE/Kilo Code
# agentic tool-calling when a non-Claude model is selected.
# Accepts OpenAI-format messages + tools; streams raw OpenAI
# ChatCompletionChunk JSON lines back (one JSON object per line).
#
# This endpoint exists so the gateway server (gateway.py) never needs
# OPENAI_API_KEY locally — all cloud keys live here on the LLM proxy server.
# ──────────────────────────────────────────────────────────────

class OpenAIToolsRequest(BaseModel):
    messages:       list
    tools:          Optional[list] = None
    tool_choice:    Optional[object] = None
    model:          Optional[str] = None
    max_tokens:     int = 8000
    stream_options: Optional[dict] = None
    request_id:     Optional[str] = None  # caller's request_id for log correlation
    chat_id:        Optional[str] = None  # caller's chat_id for conversation correlation
    conv_id:        Optional[str] = None  # stable per-conversation ID (x-ainxt-conv-id)


@app.post("/llm/openai-tools-stream")
async def openai_tools_stream(req: OpenAIToolsRequest, request: Request):
    """
    Stream OpenAI tool-call chunks for the IDE/Kilo Code agentic path.
    Each NDJSON line is a raw JSON-serialised ChatCompletionChunk.
    """
    if _openai_gw is None:
        raise HTTPException(503, "OpenAI gateway not available")

    # ── Correlation ID binding ─────────────────────────────────────────────────
    from core.logger import set_request_id, set_chat_context, set_conv_id
    upstream_request_id = (
            req.request_id
            or request.headers.get("x-request-id")
            or request.headers.get("X-Request-ID")
    )
    upstream_chat_id = (
            req.chat_id
            or request.headers.get("x-chat-id")
            or request.headers.get("X-Chat-ID")
    )
    upstream_conv_id = (
            req.conv_id
            or request.headers.get("x-ainxt-conv-id")
            or request.headers.get("X-AiNxt-Conv-Id")
            or ""
    )
    req_id = upstream_request_id or str(uuid.uuid4())
    set_request_id(req_id)
    if upstream_chat_id:
        set_chat_context("-", upstream_chat_id)
    if upstream_conv_id:
        set_conv_id(upstream_conv_id)
    # ──────────────────────────────────────────────────────────────────────────
    from core.model_registry import OPENAI_CODING_MODEL
    model = req.model or OPENAI_CODING_MODEL
    logger.info(
        f"[{req_id}] OAI-TOOLS-STREAM | model={model} | "
        f"msgs={len(req.messages)} tools={len(req.tools or [])} "
        f"conv_id={upstream_conv_id or '-'}"
    )

    loop  = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=4096)

    def _put(item):
        future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        try:
            future.result(timeout=30)
        except Exception as e:
            logger.warning(f"[{req_id}] queue put failed: {e}")

    def _run_oai_tools_stream():
        # Re-bind thread-local context — thread-locals do NOT cross thread boundaries.
        from core.logger import set_request_id as _sri, set_conv_id as _scv
        _sri(req_id)
        if upstream_conv_id:
            _scv(upstream_conv_id)
        try:
            kwargs: dict = {
                "model":          model,
                "messages":       req.messages,
                "stream":         True,
                "stream_options": req.stream_options or {"include_usage": True},
            }
            if req.tools:
                kwargs["tools"] = req.tools
            if req.tool_choice is not None:
                kwargs["tool_choice"] = req.tool_choice
            if req.max_tokens:
                kwargs["max_completion_tokens"] = req.max_tokens

            # Suppress reasoning_effort when tools are present — the OpenAI API
            # rejects the parameter alongside tool calls/results.
            if req.tools:
                kwargs["reasoning_effort"] = "none"

            logger.info(f"[{req_id}] LLM DISPATCH → provider=openai model={model}")
            response = _openai_gw.client.chat.completions.create(**kwargs)
            _oai_tools_cached = 0
            _oai_tools_prompt = 0
            for chunk in response:
                # Extract cache token counts from the final usage chunk and log
                # cache effectiveness — this chunk has empty choices so it is
                # not forwarded to the client, but we must process it first.
                if hasattr(chunk, "usage") and chunk.usage:
                    _oai_tools_prompt = chunk.usage.prompt_tokens or 0
                    try:
                        _oai_tools_cached = getattr(
                            getattr(chunk.usage, "prompt_tokens_details", None),
                            "cached_tokens", 0,
                        ) or 0
                    except Exception:
                        _oai_tools_cached = 0
                    from gateway_openai import _log_cache_effectiveness as _lce
                    _lce(
                        request_id=req_id,
                        model=model,
                        cache_read=_oai_tools_cached,
                        prompt_total=_oai_tools_prompt,
                        context="tools-stream",
                    )
                try:
                    _put(json.dumps(chunk.model_dump(exclude_none=True)) + "\n")
                except Exception:
                    _put(json.dumps(chunk.model_dump()) + "\n")
        except Exception as exc:
            logger.error(f"[{req_id}] OAI tools-stream error: {exc}")
            err_chunk = {
                "choices": [{
                    "index": 0,
                    "delta": {"content": f"[LLM error: {exc}]"},
                    "finish_reason": "stop",
                }]
            }
            _put(json.dumps(err_chunk) + "\n")
        finally:
            logger.info(f"[{req_id}] OAI tools-stream done | model={model}")
            _put(json.dumps({"done": True, "model": model}) + "\n")
            _put(None)  # sentinel

    try:
        loop.run_in_executor(_pool, _run_oai_tools_stream)
    except _queue.Full:
        raise HTTPException(503, "LLM proxy is at capacity — too many concurrent requests. Retry shortly.")

    async def _stream():
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


# ── Gemini tools-stream endpoint ──────────────────────────────
#
# POST /llm/gemini-tools-stream
#
# Used by gateway.py's _tools_proxy_stream() when _model_hint=="gemini".
# Accepts OpenAI-format messages + tools (same schema as openai-tools-stream);
# converts them to Gemini format internally and streams back OpenAI-compatible
# ChatCompletionChunk NDJSON lines so gateway.py handles both paths uniformly.
# ──────────────────────────────────────────────────────────────

# Gemini 2.5+/3.x require the original thought_signature to be replayed on the
# assistant function_call part across turns. We support two transports:
#
#   1) Server-side LRU cache keyed by tool_call_id. The streaming response side
#      stores `tool_id -> signature_bytes`; the inbound conversion side looks it
#      up by `tool_calls[i].id`. This is the primary path because the standard
#      OpenAI `id` field is preserved by every conformant client.
#
#   2) In-payload base64 under `_gemini_thought_signature` on the function
#      object. Used as a fallback for clients that DO round-trip unknown fields
#      (useful when the proxy is restarted mid-conversation and the cache is
#      cold). Clients that strip unknown fields are unaffected.
from core.gemini_protocol import GEMINI_THOUGHT_SIG_KEY as _GEMINI_THOUGHT_SIG_KEY

# Bounded LRU: tool_call_id -> raw thought_signature bytes. Evicts oldest on
# overflow so memory stays bounded regardless of conversation volume.
from collections import OrderedDict as _OrderedDict
from threading import Lock as _Lock
_THOUGHT_SIG_CACHE: "_OrderedDict[str, bytes]" = _OrderedDict()
_THOUGHT_SIG_CACHE_LOCK = _Lock()
_THOUGHT_SIG_CACHE_MAX = 4096


def _thought_sig_put(tool_id: str, sig: bytes) -> None:
    if not tool_id or not sig:
        return
    with _THOUGHT_SIG_CACHE_LOCK:
        _THOUGHT_SIG_CACHE[tool_id] = sig
        _THOUGHT_SIG_CACHE.move_to_end(tool_id)
        while len(_THOUGHT_SIG_CACHE) > _THOUGHT_SIG_CACHE_MAX:
            _THOUGHT_SIG_CACHE.popitem(last=False)


def _thought_sig_get(tool_id: str) -> Optional[bytes]:
    if not tool_id:
        return None
    with _THOUGHT_SIG_CACHE_LOCK:
        sig = _THOUGHT_SIG_CACHE.get(tool_id)
        if sig is not None:
            _THOUGHT_SIG_CACHE.move_to_end(tool_id)
        return sig


def _oai_msgs_to_gemini(messages: list):
    """Convert OpenAI-format message list → Gemini Content list.

    Gemini 2.5+/3.x require an encrypted thought_signature on every assistant
    function_call part across turns; missing it returns 400 INVALID_ARGUMENT.
    The Anthropic SDK that fronts this proxy on /v1/messages strips unknown
    fields from content_block, so the signature cannot round-trip through the
    client. Resolution: when we don't have a signature for an assistant
    tool_call, demote BOTH the assistant function_call AND the paired user
    function_response into plain text parts. Gemini sees no signature-less
    functionCall (so no 400), and the text-rendered call+response pair gives
    the model enough context to continue reasoning. tool_call_ids whose
    signature is available remain as real function_call/function_response
    pairs so the model gets full structured context where possible.
    """
    from google.genai import types as _gt
    import json as _j
    import base64 as _b64
    import binascii as _binascii

    # First pass: collect the set of tool_call_ids whose function_call we will
    # demote to text. The matching function_response on the next user turn
    # must then also be demoted, otherwise Gemini rejects the orphan response.
    demoted_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in (msg.get("tool_calls") or []):
            tc_id = tc.get("id") or ""
            if not tc_id:
                continue
            if _thought_sig_get(tc_id) is not None:
                continue
            fn = tc.get("function", {}) or {}
            sig_b64 = fn.get(_GEMINI_THOUGHT_SIG_KEY)
            if sig_b64:
                try:
                    _b64.b64decode(sig_b64)
                    continue  # signature is valid base64, will be used as-is
                except (ValueError, _binascii.Error):
                    pass
            demoted_ids.add(tc_id)

    contents = []
    for msg in messages:
        role    = msg.get("role", "user")
        content = msg.get("content") or ""
        if role == "system":
            continue  # handled via GenerateContentConfig.system_instruction
        parts: list = []
        if role == "assistant":
            if content:
                parts.append(_gt.Part(text=str(content)))
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function", {})
                try:
                    args = _j.loads(fn.get("arguments", "{}"))
                except Exception:
                    args = {}
                tc_id = tc.get("id") or ""
                if tc_id in demoted_ids:
                    # Frame as the assistant's own prior action so the model
                    # treats it as history (already done), not as a new
                    # instruction to perform. Without this framing Gemini 3.x
                    # tends to re-invoke the same tool on the next turn.
                    parts.append(_gt.Part(text=(
                        f"I previously called the `{fn.get('name', '')}` tool "
                        f"with arguments {_j.dumps(args)} and received the "
                        f"result shown below."
                    )))
                    continue
                # Resolve signature: LRU cache first, in-payload base64 fallback.
                sig_bytes = _thought_sig_get(tc_id)
                if sig_bytes is None:
                    sig_b64 = fn.get(_GEMINI_THOUGHT_SIG_KEY)
                    if sig_b64:
                        try:
                            sig_bytes = _b64.b64decode(sig_b64)
                        except (ValueError, _binascii.Error):
                            sig_bytes = None
                fc_part = _gt.Part(
                    function_call=_gt.FunctionCall(name=fn.get("name", ""), args=args)
                )
                if sig_bytes:
                    fc_part.thought_signature = sig_bytes
                parts.append(fc_part)
            role = "model"
        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "tool")
            if tool_call_id in demoted_ids:
                # Paired function_call was demoted to text → mirror here so
                # Gemini doesn't see an orphan function_response. Frame as a
                # tool-result transcript line so the model treats the cycle as
                # complete (don't re-call the same tool).
                parts.append(_gt.Part(text=(
                    f"Tool result for call id `{tool_call_id}`:\n{str(content)}"
                )))
                role = "user"
            else:
                parts.append(_gt.Part(
                    function_response=_gt.FunctionResponse(
                        name=tool_call_id,
                        response={"result": str(content)},
                    )
                ))
                role = "user"  # Gemini uses "user" role for function responses
        else:
            if isinstance(content, list):
                for c in content:
                    if c.get("type") == "text":
                        parts.append(_gt.Part(text=c["text"]))
            else:
                parts.append(_gt.Part(text=str(content)))
        if parts:
            contents.append(_gt.Content(role=role, parts=parts))
    return contents


def _oai_tools_to_gemini(tools: list):
    """Convert OpenAI function-tool list → Gemini Tool (FunctionDeclarations)."""
    from google.genai import types as _gt
    _type_map = {
        "string": "STRING", "integer": "INTEGER", "number": "NUMBER",
        "boolean": "BOOLEAN", "array": "ARRAY", "object": "OBJECT",
    }
    def _schema(prop: dict) -> "_gt.Schema":
        t  = _type_map.get(prop.get("type", "string"), "STRING")
        kw: dict = {"type": t}
        if prop.get("description"): kw["description"] = prop["description"]
        if prop.get("enum"):        kw["enum"]        = prop["enum"]
        if t == "ARRAY"  and prop.get("items"):
            kw["items"] = _schema(prop["items"])
        if t == "OBJECT" and prop.get("properties"):
            kw["properties"] = {k: _schema(v) for k, v in prop["properties"].items()}
            if prop.get("required"): kw["required"] = prop["required"]
        return _gt.Schema(**kw)
    declarations = []
    for t in tools:
        if t.get("type") != "function":
            continue
        fn     = t.get("function", {})
        params = fn.get("parameters", {})
        props  = params.get("properties", {})
        schema = _gt.Schema(
            type="OBJECT",
            properties={k: _schema(v) for k, v in props.items()},
            required=params.get("required", []),
        ) if props else None
        declarations.append(_gt.FunctionDeclaration(
            name=fn.get("name", ""),
            description=fn.get("description", ""),
            parameters=schema,
        ))
    return _gt.Tool(function_declarations=declarations) if declarations else None


class GeminiToolsRequest(BaseModel):
    messages:    list
    tools:       Optional[list] = None
    tool_choice: Optional[object] = None
    model:       Optional[str] = None
    max_tokens:  int = 8000
    request_id:  Optional[str] = None  # caller's request_id for log correlation
    chat_id:     Optional[str] = None  # caller's chat_id for conversation correlation
    conv_id:     Optional[str] = None  # stable per-conversation ID (x-ainxt-conv-id)


@app.post("/llm/gemini-tools-stream")
async def gemini_tools_stream(req: GeminiToolsRequest, request: Request):
    """
    Stream Gemini tool-call responses for the IDE/Kilo Code agentic path.
    Accepts OpenAI-format messages + tools; returns OpenAI-format NDJSON chunks
    so gateway.py can handle Gemini and OpenAI paths uniformly.
    """
    if _gemini_gw is None:
        raise HTTPException(503, "Gemini gateway not available")

    # ── Correlation ID binding ─────────────────────────────────────────────────
    from core.logger import set_request_id, set_chat_context, set_conv_id
    upstream_request_id = (
            req.request_id
            or request.headers.get("x-request-id")
            or request.headers.get("X-Request-ID")
    )
    upstream_chat_id = (
            req.chat_id
            or request.headers.get("x-chat-id")
            or request.headers.get("X-Chat-ID")
    )
    upstream_conv_id = (
            req.conv_id
            or request.headers.get("x-ainxt-conv-id")
            or request.headers.get("X-AiNxt-Conv-Id")
            or ""
    )
    req_id = upstream_request_id or str(uuid.uuid4())[:8]
    set_request_id(req_id)
    if upstream_chat_id:
        set_chat_context("-", upstream_chat_id)
    if upstream_conv_id:
        set_conv_id(upstream_conv_id)
    # ──────────────────────────────────────────────────────────────────────────
    from core.model_registry import GEMINI_VISION_MODEL
    model_name = req.model or GEMINI_VISION_MODEL
    logger.info(
        f"[{req_id}] GEMINI-TOOLS-STREAM | model={model_name} | "
        f"msgs={len(req.messages)} tools={len(req.tools or [])} "
        f"conv_id={upstream_conv_id or '-'}"
    )

    loop  = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=4096)

    def _put(item):
        future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        try:
            future.result(timeout=30)
        except Exception as e:
            logger.warning(f"[{req_id}] queue put failed: {e}")

    def _run_gemini_tools_stream():
        # Re-bind thread-local context — thread-locals do NOT cross thread boundaries.
        from core.logger import set_request_id as _sri, set_conv_id as _scv
        _sri(req_id)
        if upstream_conv_id:
            _scv(upstream_conv_id)
        from google.genai import types as _gt
        import json as _j
        import base64 as _b64

        # Extract system instruction
        system_instruction = None
        for msg in req.messages:
            if msg.get("role") == "system":
                c = msg.get("content", "")
                system_instruction = c if isinstance(c, str) else " ".join(
                    x.get("text", "") for x in c if x.get("type") == "text"
                )
                break

        contents  = _oai_msgs_to_gemini(req.messages)
        gemini_tool = _oai_tools_to_gemini(req.tools) if req.tools else None

        cfg_kwargs: dict = {"max_output_tokens": req.max_tokens}
        if system_instruction:
            cfg_kwargs["system_instruction"] = system_instruction
        if gemini_tool:
            cfg_kwargs["tools"] = [gemini_tool]
            cfg_kwargs["tool_config"] = _gt.ToolConfig(
                function_calling_config=_gt.FunctionCallingConfig(mode="AUTO")
            )
            # Disable thinking when tools are active. Gemini 2.5+/3.x require the
            # encrypted thought_signature to be replayed verbatim on every
            # assistant function_call across turns, but the OpenAI-format CLI
            # client we sit behind strips unknown fields, so signatures can't
            # round-trip reliably. With thinking_budget=0 the model produces no
            # signatures and none are required on subsequent turns — fully
            # sidestepping the "Function call is missing a thought_signature"
            # 400. Tool-calling quality is unaffected on Flash / Flash-Lite.
            try:
                cfg_kwargs["thinking_config"] = _gt.ThinkingConfig(thinking_budget=0)
            except Exception:
                # Older google-genai SDKs without ThinkingConfig — fall back to
                # the cache/in-payload signature carry path silently.
                pass

        completion_id = "chatcmpl-" + uuid.uuid4().hex[:8]
        created_ts    = int(time.time())

        def _oai_chunk(delta: dict, finish=None) -> str:
            choice: dict = {"index": 0, "delta": delta}
            if finish is not None:
                choice["finish_reason"] = finish
            return _j.dumps({
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created_ts, "model": model_name, "choices": [choice],
            }) + "\n"

        try:
            _fn_index    = 0
            _last_kind   = None  # "text" | "fncall" — drives finish_reason
            _text_chars  = 0
            _thought_cnt = 0
            _empty_cnt   = 0
            _prompt_tok  = 0
            _output_tok  = 0
            _gemini_tools_cached_tok = 0   # cached_content_token_count from usage_metadata
            _final_finish_reason = None

            for chunk in _gemini_gw.client.models.generate_content_stream(
                    model=model_name,
                    contents=contents,
                    config=_gt.GenerateContentConfig(**cfg_kwargs),
            ):
                # Capture usage on every chunk; the last one wins.
                try:
                    if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                        _um = chunk.usage_metadata
                        _prompt_tok = getattr(_um, "prompt_token_count", 0) or _prompt_tok
                        _output_tok = getattr(_um, "candidates_token_count", 0) or _output_tok
                        _gemini_tools_cached_tok = getattr(_um, "cached_content_token_count", 0) or _gemini_tools_cached_tok
                except Exception:
                    pass

                cand  = chunk.candidates[0] if chunk.candidates else None
                if not cand:
                    _empty_cnt += 1
                    continue
                # Capture finish_reason for diagnostics; the last non-empty wins.
                _fr = getattr(cand, "finish_reason", None)
                if _fr is not None:
                    _final_finish_reason = str(_fr)

                parts = (cand.content.parts or []) if cand.content else []
                if not parts:
                    _empty_cnt += 1

                for part in parts:
                    # Gemini 2.5+/3.x thinking models emit reasoning parts with
                    # part.thought=True. These must NOT be forwarded as
                    # user-visible content — doing so causes the CLI agent loop
                    # to mistake the chain-of-thought for the final answer and
                    # either stop early or re-invoke tools.
                    if getattr(part, "thought", False):
                        _thought_cnt += 1
                        continue
                    txt = getattr(part, "text", None)
                    fc  = getattr(part, "function_call", None)
                    if txt:
                        _last_kind  = "text"
                        _text_chars += len(txt)
                        _put(_oai_chunk({"content": txt}))
                    elif fc:
                        _last_kind = "fncall"
                        tool_id  = f"call_{uuid.uuid4().hex[:8]}"
                        args_str = _j.dumps(dict(fc.args)) if fc.args else "{}"
                        _start_fn: dict = {"name": fc.name, "arguments": ""}
                        _sig = getattr(part, "thought_signature", None)
                        if _sig:
                            _thought_sig_put(tool_id, _sig)
                            _start_fn[_GEMINI_THOUGHT_SIG_KEY] = _b64.b64encode(_sig).decode("ascii")
                        _put(_oai_chunk({"tool_calls": [{
                            "index": _fn_index, "id": tool_id, "type": "function",
                            "function": _start_fn,
                        }]}))
                        _put(_oai_chunk({"tool_calls": [{
                            "index": _fn_index,
                            "function": {"arguments": args_str},
                        }]}))
                        _fn_index += 1

            # Finish reason driven by what was LAST emitted, not by whether any
            # function_call ever happened. Gemini 3.x can emit fncall + text in
            # the same response — only when the stream actually ends on a
            # function_call should the CLI execute tools. Otherwise the CLI
            # treats the closing text as a "go execute tools" signal and loops
            # forever instead of rendering the final answer.
            if _last_kind == "fncall":
                finish_reason = "tool_calls"
            else:
                finish_reason = "stop"

            # Synthetic fallback when Gemini emits ZERO visible content (all
            # parts were thoughts, or empty candidates due to safety filter /
            # MAX_TOKENS mid-thinking). Without this the CLI sees an empty
            # message_stop and the agent loop has nothing to render, leaving
            # the user with a silent failure. Surface the underlying reason
            # instead so the user knows to retry / rephrase / raise the token
            # budget.
            if _last_kind is None:
                _reason_txt = (
                    f"[Gemini returned no visible content "
                    f"(finish_reason={_final_finish_reason}, "
                    f"thought_parts={_thought_cnt}, empty_chunks={_empty_cnt}). "
                    f"Try raising max_tokens or rephrasing the request.]"
                )
                _put(_oai_chunk({"content": _reason_txt}))

            # Emit OpenAI-format usage chunk so the gateway records non-zero
            # token counts (was always 0,0 before because we never forwarded
            # Gemini's usage_metadata).
            if _prompt_tok or _output_tok:
                _put(_j.dumps({
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created_ts, "model": model_name,
                    "choices": [],
                    "usage": {
                        "prompt_tokens":     _prompt_tok,
                        "completion_tokens": _output_tok,
                        "total_tokens":      _prompt_tok + _output_tok,
                    },
                }) + "\n")

            _put(_oai_chunk({}, finish=finish_reason))

        except Exception as exc:
            logger.error(f"[{req_id}] Gemini tools-stream error: {exc}")
            _put(_oai_chunk({"content": f"[LLM error: {exc}]"}, finish="stop"))
        finally:
            # Log cache effectiveness — Gemini exposes cached_content_token_count
            # in usage_metadata when context caching is active.
            try:
                from gateway_gemini import _log_cache_effectiveness as _lce_gemini
                _lce_gemini(
                    request_id=req_id,
                    model=model_name,
                    cache_read=_gemini_tools_cached_tok,
                    prompt_total=_prompt_tok,
                    context="tools-stream",
                )
            except Exception:
                pass
            logger.info(
                f"[{req_id}] Gemini tools-stream done "
                f"fn_calls={_fn_index} text_chars={_text_chars} "
                f"thought_parts={_thought_cnt} empty_chunks={_empty_cnt} "
                f"last_kind={_last_kind} gemini_finish={_final_finish_reason} "
                f"in_tok={_prompt_tok} out_tok={_output_tok} "
                f"cached_tok={_gemini_tools_cached_tok}"
            )
            _put(None)  # sentinel

    try:
        loop.run_in_executor(_pool, _run_gemini_tools_stream)
    except _queue.Full:
        raise HTTPException(503, "LLM proxy is at capacity — too many concurrent requests. Retry shortly.")

    async def _stream():
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


# ── Tool-use chat endpoint ─────────────────────────────────────
#
# POST /llm/chat
#
# Executes ONE round of a tool-use conversation against the requested
# provider and returns a normalized JSON response.  The calling service
# (gateway ReactOrchestrator via core/proxy_tool_use.py) drives the
# multi-round loop and executes tool calls locally; only the LLM API
# call crosses to the LLM proxy server, keeping API keys exclusively here.
#
# Request JSON:
#   provider    — "claude" | "openai" | "gemini"
#   model       — optional model override
#   system      — system prompt string
#   messages    — conversation so far in Anthropic format
#   tools       — Anthropic-format tool schemas (input_schema key)
#                 pass [] on the last round to force a plain-text response
#   max_tokens  — default 8000
#
# Response JSON (normalized, Anthropic-like):
#   stop_reason      — "tool_use" | "end_turn"
#   tool_calls       — [{id, name, input}]
#   text             — final answer (non-empty when stop_reason=="end_turn")
#   assistant_message— {role:"assistant", content:[...]} ready to append
#   input_tokens     — int
#   output_tokens    — int
# ──────────────────────────────────────────────────────────────

import json as _json


class ChatRequest(BaseModel):
    provider:   str
    model:      Optional[str] = None
    system:     str           = ""
    messages:   list
    tools:      Optional[list] = None
    max_tokens: int           = 64000
    request_id: Optional[str] = None  # caller's request_id for log correlation
    chat_id:    Optional[str] = None  # caller's chat_id for conversation correlation

def _strip_tool_result_name(messages: list) -> list:
    """
    Anthropic's API rejects extra fields in tool_result blocks.
    The 'name' key is added by proxy_tool_use.py for Gemini's FunctionResponse
    but must be stripped before forwarding to Claude.
    """
    cleaned = []
    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            content = [
                {k: v for k, v in block.items() if not (block.get("type") == "tool_result" and k == "name")}
                for block in msg["content"]
            ]
            msg = {**msg, "content": content}
        cleaned.append(msg)
    return cleaned


async def _chat_claude(req: ChatRequest) -> dict:
    from core.model_registry import CLAUDE_PRIMARY_MODEL
    # No cache breakpoints here. The gateway's httpx transport adds one top-level
    # cache_control as the request leaves for api.anthropic.com (see
    # core/claude_cache_egress.py).
    model = req.model or CLAUDE_PRIMARY_MODEL
    logger.info(f"[LLM DISPATCH] provider=claude model={model} (chat/tool-use)")
    _sys_text = req.system or "You are a helpful AI coding assistant."
    _create_kwargs: dict = {
        "model":      model,
        "max_tokens": req.max_tokens,
        "system":     [{"type": "text", "text": _sys_text}],
        "messages":   req.messages,
        "tools":      None,
    }
    if req.tools:
        _create_kwargs["tools"] = req.tools
    # Claude Opus 4+ and Sonnet 5 have deprecated the temperature parameter —
    # gate via the shared helper in gateway_claude so every new family lands in one place.
    from gateway_claude import _no_temperature_model
    if not _no_temperature_model(model):
        _create_kwargs["temperature"] = 0
    response = await _claude_gw.client.messages.create(**_create_kwargs)
    tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
    text = " ".join(b.text for b in response.content if hasattr(b, "text") and b.text).strip()
    content_dicts = []
    for b in response.content:
        if b.type == "text":
            content_dicts.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            content_dicts.append({"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input)})
    usage = response.usage
    _cache_read     = getattr(usage, "cache_read_input_tokens",     0) or 0 if usage else 0
    _cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0 if usage else 0
    from core.logger import get_request_id as _get_req_id
    logger.info(
        f"[CLAUDE CACHE] request_id={_get_req_id() or '_chat_claude'} model={model} "
        f"cache_read={_cache_read} cache_creation={_cache_creation} "
        f"input_tokens={getattr(usage, 'input_tokens', 0) if usage else 0}"
    )
    from gateway_claude import _log_cache_effectiveness as _lce_claude
    _lce_claude(
        request_id=_get_req_id() or "_chat_claude",
        model=model,
        cache_read=_cache_read,
        cache_created=_cache_creation,
        prompt_total=getattr(usage, "input_tokens", 0) if usage else 0,
        context="chat",
    )
    return {
        "stop_reason":        response.stop_reason,
        "tool_calls":         [{"id": b.id, "name": b.name, "input": dict(b.input)} for b in tool_use_blocks],
        "text":               text,
        "assistant_message":  {"role": "assistant", "content": content_dicts},
        "input_tokens":       getattr(usage, "input_tokens",  0) if usage else 0,
        "output_tokens":      getattr(usage, "output_tokens", 0) if usage else 0,
        "cache_read_tokens":  _cache_read,
        "cache_creation_tokens": _cache_creation,
        "model":              model,
    }


def _anthropic_msgs_to_openai(system: str, messages: list) -> list:
    result = []
    if system:
        result.append({"role": "system", "content": system})
    for msg in messages:
        role, content = msg["role"], msg["content"]
        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue
        if role == "user":
            tool_results = [c for c in content if c.get("type") == "tool_result"]
            if tool_results:
                for tr in tool_results:
                    result.append({
                        "role":         "tool",
                        "tool_call_id": tr["tool_use_id"],
                        "content":      str(tr.get("content", "")),
                    })
            else:
                text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
                result.append({"role": "user", "content": text})
        elif role == "assistant":
            tool_uses  = [c for c in content if c.get("type") == "tool_use"]
            text       = " ".join(c.get("text", "") for c in content if c.get("type") == "text").strip()
            if tool_uses:
                result.append({
                    "role":       "assistant",
                    "content":    text,
                    "tool_calls": [
                        {"id": tu["id"], "type": "function",
                         "function": {"name": tu["name"], "arguments": _json.dumps(tu["input"])}}
                        for tu in tool_uses
                    ],
                })
            else:
                result.append({"role": "assistant", "content": text})
    return result


def _anthropic_tools_to_openai(tools: list) -> list:
    return [
        {"type": "function", "function": {
            "name":        t["name"],
            "description": t.get("description", ""),
            "parameters":  t.get("input_schema", {"type": "object", "properties": {}}),
        }}
        for t in tools
    ]


def _chat_openai(req: ChatRequest) -> dict:
    from core.model_registry import OPENAI_CODING_MODEL
    model    = req.model or OPENAI_CODING_MODEL
    messages = _anthropic_msgs_to_openai(req.system, req.messages)
    tools    = _anthropic_tools_to_openai(req.tools) if req.tools else None
    kwargs: dict = {"model": model, "messages": messages, "max_completion_tokens": req.max_tokens}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    logger.info(f"[LLM DISPATCH] provider=openai model={model} (chat/tool-use)")
    response      = _openai_gw.client.chat.completions.create(**kwargs)
    msg           = response.choices[0].message
    finish_reason = response.choices[0].finish_reason
    usage         = response.usage
    # Log cache effectiveness for the non-streaming chat/tool-use path.
    _chat_prompt  = usage.prompt_tokens if usage else 0
    try:
        _chat_cached = getattr(
            getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0
        ) or 0 if usage else 0
    except Exception:
        _chat_cached = 0
    from gateway_openai import _log_cache_effectiveness as _lce_chat
    _lce_chat(
        request_id=str(uuid.uuid4()),
        model=model,
        cache_read=_chat_cached,
        prompt_total=_chat_prompt,
        context="chat",
    )
    if not msg.tool_calls or finish_reason == "stop":
        return {
            "stop_reason":       "end_turn",
            "tool_calls":        [],
            "text":              msg.content or "",
            "assistant_message": {"role": "assistant", "content": [{"type": "text", "text": msg.content or ""}]},
            "input_tokens":      usage.prompt_tokens     if usage else 0,
            "output_tokens":     usage.completion_tokens if usage else 0,
            "model":             model,
        }
    tool_calls_norm = [
        {"id": tc.id, "name": tc.function.name, "input": _json.loads(tc.function.arguments or "{}")}
        for tc in msg.tool_calls
    ]
    return {
        "stop_reason":       "tool_use",
        "tool_calls":        tool_calls_norm,
        "text":              "",
        "assistant_message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}
            for tc in tool_calls_norm
        ]},
        "input_tokens":      usage.prompt_tokens     if usage else 0,
        "output_tokens":     usage.completion_tokens if usage else 0,
        "model":             model,
    }


def _anthropic_msgs_to_gemini(messages: list):
    from google.genai import types as _gt
    contents = []
    for msg in messages:
        role    = "user" if msg["role"] == "user" else "model"
        content = msg["content"]
        parts   = []
        if isinstance(content, str):
            parts.append(_gt.Part(text=content))
        else:
            for c in content:
                ctype = c.get("type")
                if ctype == "text":
                    parts.append(_gt.Part(text=c["text"]))
                elif ctype == "tool_use":
                    parts.append(_gt.Part(
                        function_call=_gt.FunctionCall(name=c["name"], args=c["input"])
                    ))
                elif ctype == "tool_result":
                    parts.append(_gt.Part(
                        function_response=_gt.FunctionResponse(
                            name=c.get("name", c.get("tool_use_id", "tool")),
                            response={"result": str(c.get("content", ""))},
                        )
                    ))
        if parts:
            contents.append(_gt.Content(role=role, parts=parts))
    return contents


def _anthropic_tools_to_gemini(tools: list):
    from google.genai import types as _gt
    _type_map = {
        "string": "STRING", "integer": "INTEGER", "number": "NUMBER",
        "boolean": "BOOLEAN", "array": "ARRAY", "object": "OBJECT",
    }
    def _schema(prop: dict):
        t      = _type_map.get(prop.get("type", "string"), "STRING")
        kwargs: dict = {"type": t}
        if prop.get("description"): kwargs["description"] = prop["description"]
        if prop.get("enum"):        kwargs["enum"]        = prop["enum"]
        if t == "ARRAY"  and prop.get("items"):      kwargs["items"]      = _schema(prop["items"])
        if t == "OBJECT" and prop.get("properties"):
            kwargs["properties"] = {k: _schema(v) for k, v in prop["properties"].items()}
            if prop.get("required"): kwargs["required"] = prop["required"]
        return _gt.Schema(**kwargs)
    declarations = []
    for t in tools:
        inp   = t.get("input_schema", {})
        props = inp.get("properties", {})
        params = _gt.Schema(
            type="OBJECT",
            properties={k: _schema(v) for k, v in props.items()},
            required=inp.get("required", []),
        ) if props else None
        declarations.append(_gt.FunctionDeclaration(
            name=t["name"], description=t.get("description", ""), parameters=params,
        ))
    return _gt.Tool(function_declarations=declarations)


def _chat_gemini(req: ChatRequest) -> dict:
    from google.genai import types as _gt
    # Default to the Gemini text/coding model for chat — GEMINI_VISION_MODEL now
    # aliases to gemini-3.1-flash-image, which would 400 on text-only contents.
    from core.model_registry import GEMINI_TEXT_MODEL
    model    = req.model or GEMINI_TEXT_MODEL
    logger.info(f"[LLM DISPATCH] provider=gemini model={model} (chat/tool-use)")
    contents = _anthropic_msgs_to_gemini(req.messages)
    cfg_kwargs: dict = {}
    if req.system: cfg_kwargs["system_instruction"] = req.system
    if req.tools:
        cfg_kwargs["tools"] = [_anthropic_tools_to_gemini(req.tools)]
        cfg_kwargs["tool_config"] = _gt.ToolConfig(
            function_calling_config=_gt.FunctionCallingConfig(mode="AUTO")
        )
    response  = _gemini_gw.client.models.generate_content(
        model=model, contents=contents,
        config=_gt.GenerateContentConfig(**cfg_kwargs) if cfg_kwargs else None,
    )
    candidate = response.candidates[0] if response.candidates else None
    parts     = (candidate.content.parts or []) if candidate else []
    fn_parts  = [p for p in parts if getattr(p, "function_call", None) is not None]
    finish    = str(getattr(candidate, "finish_reason", "")).upper() if candidate else "STOP"
    um        = getattr(response, "usage_metadata", None)
    in_tok    = getattr(um, "prompt_token_count",          0) or 0 if um else 0
    out_tok   = getattr(um, "candidates_token_count",      0) or 0 if um else 0
    cached_tok = getattr(um, "cached_content_token_count", 0) or 0 if um else 0
    # Log cache effectiveness — Gemini exposes cached_content_token_count when
    # context caching is active; zero means no cache hit (always emitted).
    from gateway_gemini import _log_cache_effectiveness as _lce_gemini
    _lce_gemini(
        request_id=str(uuid.uuid4()),
        model=model,
        cache_read=cached_tok,
        prompt_total=in_tok,
        context="chat",
    )
    if not fn_parts or finish in ("STOP", "1"):
        text = " ".join(p.text for p in parts if getattr(p, "text", None)).strip()
        return {
            "stop_reason":       "end_turn",
            "tool_calls":        [],
            "text":              text,
            "assistant_message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            "input_tokens":      in_tok,
            "output_tokens":     out_tok,
            "model":             model,
        }
    tool_calls_norm = [
        {"id": f"gemini_{i}", "name": p.function_call.name, "input": dict(p.function_call.args)}
        for i, p in enumerate(fn_parts)
    ]
    return {
        "stop_reason":       "tool_use",
        "tool_calls":        tool_calls_norm,
        "text":              "",
        "assistant_message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}
            for tc in tool_calls_norm
        ]},
        "input_tokens":      in_tok,
        "output_tokens":     out_tok,
        "model":             model,
    }


@app.post("/llm/chat")
async def chat(req: ChatRequest, request: Request):
    """Single tool-use round — called by the gateway server's proxy_tool_use loop."""
    if req.provider not in ("claude", "openai", "gemini"):
        raise HTTPException(400, f"Unknown provider: {req.provider!r}")
    _gw_map = {"claude": _claude_gw, "openai": _openai_gw, "gemini": _gemini_gw}
    if _gw_map[req.provider] is None:
        raise HTTPException(503, f"{req.provider} gateway not available")

    # ── Correlation ID binding ─────────────────────────────────────────────────
    from core.logger import set_request_id, set_chat_context
    upstream_request_id = (
            req.request_id
            or request.headers.get("x-request-id")
            or request.headers.get("X-Request-ID")
    )
    upstream_chat_id = (
            req.chat_id
            or request.headers.get("x-chat-id")
            or request.headers.get("X-Chat-ID")
    )
    req_id = upstream_request_id or str(uuid.uuid4())
    set_request_id(req_id)
    if upstream_chat_id:
        set_chat_context("-", upstream_chat_id)
    # ──────────────────────────────────────────────────────────────────────────
    caller = request.client.host if request.client else "unknown"
    logger.info(
        f"[{req_id}] CHAT from {caller} | provider={req.provider} "
        f"model={req.model or 'default'} tools={len(req.tools or [])} msgs={len(req.messages)}"
    )
    loop  = asyncio.get_running_loop()
    try:
        if req.provider == "claude":
            result = await _chat_claude(req)
        else:
            _fn = {"openai": _chat_openai, "gemini": _chat_gemini}[req.provider]
            result = await _run_in_pool(loop, _fn, req)
    except Exception as e:
        logger.error(f"[{req_id}] /llm/chat {req.provider} error: {e}")
        raise HTTPException(500, str(e))
    logger.info(
        f"[{req_id}] CHAT DONE | stop={result['stop_reason']} "
        f"tools={len(result['tool_calls'])} in={result['input_tokens']} out={result['output_tokens']}"
    )
    return result


# ── Image generation endpoint ──────────────────────────────────

@app.post("/llm/generate-image")
async def generate_image(req: GenerateImageRequest, request: Request):
    """
    Non-streaming image+text → text call via Gemini vision (primary).
    Falls back to OpenAI vision if Gemini fails.
    Returns JSON: {"text": "...", "in_tok": N, "out_tok": N}
    """
    if req.provider != "gemini":
        raise HTTPException(400, f"Image generation only supported for gemini, got {req.provider!r}")
    if _gemini_gw is None and _openai_gw is None:
        raise HTTPException(503, "No vision gateway available (both Gemini and OpenAI unavailable)")

    # ── Correlation ID binding ─────────────────────────────────────────────────
    from core.logger import set_request_id, set_chat_context
    upstream_request_id = (
            req.request_id
            or request.headers.get("x-request-id")
            or request.headers.get("X-Request-ID")
    )
    upstream_chat_id = (
            req.chat_id
            or request.headers.get("x-chat-id")
            or request.headers.get("X-Chat-ID")
    )
    req_id = upstream_request_id or str(uuid.uuid4())
    set_request_id(req_id)
    if upstream_chat_id:
        set_chat_context("-", upstream_chat_id)
    # ──────────────────────────────────────────────────────────────────────────
    logger.info(
        f"[{req_id}] IMAGE REQUEST | mime={req.mime_type} | "
        f"prompt={req.prompt[:60].replace(chr(10),' ')!r}..."
    )

    loop = asyncio.get_running_loop()

    def _call_image_gemini() -> tuple[str, int, int]:
        # Re-bind thread-local context — thread-locals do NOT cross thread boundaries
        from core.logger import set_request_id as _sri, set_chat_context as _scc
        _sri(req_id)
        if (upstream_chat_id or "-") != "-":
            _scc("-", upstream_chat_id)

        import base64 as _b64
        from google.genai import types as _gtypes
        from core.retry import retry_llm
        from core.circuit_breaker import get_breaker
        from core.model_registry import GEMINI_VISION_MODEL as _MODEL

        # Compliance is enforced upstream (Tier 1); prompt is already validated/redacted.
        safe_prompt = req.prompt

        # Multi-image (optional, backward-compatible): use images_b64/mime_types
        # when the caller populated them (newer client), else fall back to the
        # single image_b64/mime_type pair (older client — unchanged behaviour).
        _imgs  = req.images_b64 if req.images_b64 else [req.image_b64]
        _mimes = req.mime_types if req.mime_types else [req.mime_type] * len(_imgs)
        if len(_mimes) < len(_imgs):
            _mimes = _mimes + [req.mime_type] * (len(_imgs) - len(_mimes))

        parts = []
        if req.system_prompt:
            parts.append(_gtypes.Part(text=req.system_prompt + "\n\n"))
        parts.append(_gtypes.Part(text=safe_prompt))
        for _img, _mt in zip(_imgs, _mimes):
            parts.append(_gtypes.Part(
                inline_data=_gtypes.Blob(
                    mime_type=_mt,
                    data=_b64.b64decode(_img),
                )
            ))

        breaker = get_breaker("gemini")
        response = breaker.call(
            retry_llm,
            lambda: _gemini_gw.client.models.generate_content(
                model=_MODEL,
                contents=_gtypes.Content(parts=parts, role="user"),
            ),
        )

        in_tok = out_tok = 0
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            _um = response.usage_metadata
            in_tok  = getattr(_um, "prompt_token_count",    0) or 0
            out_tok = getattr(_um, "candidates_token_count", 0) or 0
            logger.info(
                f"[{req_id}] gemini image tokens | in={in_tok} out={out_tok} "
                f"total={getattr(_um, 'total_token_count', 0) or 0}"
            )

        output = response.text or ""
        # Output redaction is handled by the backend gateway layer (Tier 1).
        return output, in_tok, out_tok

    def _call_image_openai() -> tuple[str, int, int]:
        from gateway_openai import generate_with_image_openai
        return generate_with_image_openai(
            prompt=req.prompt,
            image_b64=req.image_b64,
            mime_type=req.mime_type,
            system_prompt=req.system_prompt,
            _gateway=_openai_gw,
            images_b64=req.images_b64,
            mime_types=req.mime_types,
        )

    if _gemini_gw is None:
        # Gemini not loaded — go straight to OpenAI fallback
        logger.warning(f"[{req_id}] Gemini gateway not loaded — using OpenAI vision directly")
        try:
            text, in_tok, out_tok = await _run_in_pool(loop, _call_image_openai)
            logger.info(f"[{req_id}] image done (openai direct) | in={in_tok} out={out_tok}")
            return {"text": text, "in_tok": in_tok, "out_tok": out_tok, "actual_model": "openai"}
        except Exception as exc:
            logger.error(f"[{req_id}] OpenAI vision failed: {exc}", exc_info=True)
            raise HTTPException(500, f"Image generation failed: {exc}")

    try:
        text, in_tok, out_tok = await _run_in_pool(loop, _call_image_gemini)
        logger.info(f"[{req_id}] image done (gemini) | in={in_tok} out={out_tok}")
        return {"text": text, "in_tok": in_tok, "out_tok": out_tok, "actual_model": "gemini"}
    except Exception as gemini_exc:
        logger.warning(
            f"[{req_id}] Gemini vision failed ({gemini_exc!r}) — "
            f"falling back to OpenAI vision",
            exc_info=True,
        )
        if _openai_gw is None:
            logger.error(f"[{req_id}] OpenAI fallback unavailable — no gateway")
            raise HTTPException(500, f"Image generation failed (Gemini: {gemini_exc}; OpenAI: unavailable)")
        try:
            text, in_tok, out_tok = await _run_in_pool(loop, _call_image_openai)
            logger.info(f"[{req_id}] image done (openai fallback) | in={in_tok} out={out_tok}")
            return {"text": text, "in_tok": in_tok, "out_tok": out_tok, "actual_model": "openai"}
        except Exception as openai_exc:
            logger.error(
                f"[{req_id}] OpenAI vision fallback also failed: {openai_exc}",
                exc_info=True,
            )
            raise HTTPException(
                500,
                f"Image generation failed — Gemini: {gemini_exc}; OpenAI: {openai_exc}",
            )


# ── Text → Image generation (Imagen / DALL-E) ──────────────────
#
# Approved providers only:
#   - gemini  → Imagen-3 Fast
#   - openai  → DALL-E 3 HD
# No third-party stock photo APIs.
#
# Returned media: bytes streamed as `image/png` (or content type set by
# the provider). Errors come back as JSON.

from fastapi.responses import Response as _Response


def _image_cost(model: str, in_tok: int, out_tok: int) -> float:
    """Per-token image-generation cost, using the SAME formula and rate table
    that chat/doc responses use (core.model_registry.MODEL_COST_PER_1M).

    rates = (input_per_1M, output_per_1M) → cost = (in*rate_in + out*rate_out)/1e6.
    Falls back to the gemini image rate when the model isn't in the table.
    """
    try:
        from core.model_registry import MODEL_COST_PER_1M, GEMINI_IMAGE_MODEL
        rates = MODEL_COST_PER_1M.get(model) or MODEL_COST_PER_1M.get(GEMINI_IMAGE_MODEL) or (0.075, 0.30)
    except Exception:
        rates = (0.075, 0.30)  # gemini-3.1-flash-image carry-over rate
    return (int(in_tok or 0) * rates[0] + int(out_tok or 0) * rates[1]) / 1_000_000


@app.post("/llm/imagen")
async def imagen(req: ImagenRequest):
    req_id = str(uuid.uuid4())[:8]
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(400, "prompt is required")
    if req.provider not in {"gemini", "openai"}:
        raise HTTPException(400, f"unsupported provider: {req.provider}")
    aspect = (req.aspect_ratio or "16:9").strip() or "16:9"
    n_imgs = max(1, min(4, int(req.number_of_images or 1)))
    style  = (req.style_suffix or "").strip()

    full_prompt = req.prompt.strip()
    if style:
        full_prompt = f"{full_prompt}. Style: {style}"
    full_prompt = (
        f"{full_prompt}. Cinematic professional rendering, ultra high resolution, "
        f"aspect ratio {aspect}, no text, no watermarks, photorealistic."
    )
    logger.info(
        f"[{req_id}] IMAGEN REQUEST | provider={req.provider} | aspect={aspect} | "
        f"n={n_imgs} | prompt={req.prompt[:60].replace(chr(10),' ')!r}..."
    )

    loop = asyncio.get_running_loop()

    # Per-request metadata holder written by whichever provider succeeds and
    # read after the executor completes. A plain dict (not the gateway's
    # thread-locals) so values written on the pool worker thread are visible
    # when read back on the event loop thread.
    _meta: dict = {"model": "", "in_tok": 0, "out_tok": 0}

    def _call_gemini() -> bytes:
        if _gemini_gw is None:
            raise RuntimeError("Gemini gateway not available")
        from google.genai import types as _gtypes
        # Image-generation model — sourced from the registry so the env override
        # (GEMINI_IMAGE_MODEL) is respected without code changes.
        from core.model_registry import GEMINI_IMAGE_MODEL as _GEMINI_MULTIMODAL

        # Image generation prompt fed to generate_content with IMAGE modality.
        # The model returns inline image data in candidates[0].content.parts.
        resp = _gemini_gw.client.models.generate_content(
            model=_GEMINI_MULTIMODAL,
            contents=full_prompt,
            config=_gtypes.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=_gtypes.ImageConfig(
                    aspect_ratio=aspect,
                ),
            ),
        )

        # Capture real token counts + model so the endpoint can surface
        # (model, in_tok, out_tok, cost, latency) like chat/doc responses.
        _meta["model"] = _GEMINI_MULTIMODAL
        try:
            if getattr(resp, "usage_metadata", None):
                _um = resp.usage_metadata
                _meta["in_tok"]  = getattr(_um, "prompt_token_count",     0) or 0
                _meta["out_tok"] = getattr(_um, "candidates_token_count", 0) or 0
                logger.info(
                    f"[{req_id}] [GEMINI RAW usage_metadata] imagen model={_GEMINI_MULTIMODAL} "
                    f"prompt={_meta['in_tok']} candidates={_meta['out_tok']}"
                )
        except Exception:
            pass

        # Extract inline image bytes from response
        if not resp.candidates:
            raise RuntimeError(f"{_GEMINI_MULTIMODAL}: no candidates in response")

        parts = (resp.candidates[0].content.parts or []) if resp.candidates[0].content else []
        for part in parts:
            if hasattr(part, "inline_data") and part.inline_data and part.inline_data.data:
                logger.info(
                    f"[{req_id}] image model={_GEMINI_MULTIMODAL} OK | "
                    f"mime={getattr(part.inline_data, 'mime_type', 'unknown')} "
                    f"bytes={len(part.inline_data.data)}"
                )
                return part.inline_data.data

        raise RuntimeError(f"{_GEMINI_MULTIMODAL}: response has no inline image data (parts={len(parts)})")

    def _call_openai() -> bytes:
        if _openai_gw is None:
            raise RuntimeError("OpenAI gateway not available")

        # OpenAI Images size — both gpt-image-1 and dall-e-3 accept these.
        size_map = {
            "1:1":  "1024x1024",
            "16:9": "1536x1024",
            "9:16": "1024x1536",
            "4:3":  "1024x1024",
            "3:4":  "1024x1024",

        }
        size = size_map.get(aspect, "1024x1024")

        import openai as _openai
        import base64 as _b64
        import urllib.request as _urllib_req

        client = _openai_gw.client if hasattr(_openai_gw, "client") else _openai.OpenAI()

        # NOTE: response_format has been REMOVED.  Newer OpenAI SDKs reject
        # the parameter outright ("Unknown parameter: 'response_format'") and
        # the API picks the field automatically (URL or b64_json).
        def _to_bytes(r):
            if not getattr(r, "data", None):
                raise RuntimeError("OpenAI Images returned empty data")
            item = r.data[0]
            b64 = getattr(item, "b64_json", None)
            if b64:
                return _b64.b64decode(b64)
            url = getattr(item, "url", None)
            if url:
                with _urllib_req.urlopen(url, timeout=60) as fh:
                    return fh.read()
            raise RuntimeError("OpenAI Images returned neither b64_json nor url")

        # Try gpt-image-1 first (current OpenAI image model). Only fall
        # through to dall-e-3 if gpt-image-1 failed with "model not found"
        # — any other error means gpt-image-1 IS available and we shouldn't
        # mask a real failure by switching models.
        try:
            r = client.images.generate(
                model="gpt-image-1",
                prompt=full_prompt,
                size=size,
                n=1,
            )
            _meta["model"] = "gpt-image-1"   # OpenAI images have no token usage
            return _to_bytes(r)
        except Exception as e_new:
            msg = str(e_new).lower()
            is_model_missing = (
                    "does not exist" in msg
                    or "model_not_found" in msg
                    or "not found" in msg
                    or "no access" in msg
            )
            if not is_model_missing:
                # gpt-image-1 exists but the request itself failed (auth,
                # rate limit, prompt block, …). Re-raise so the caller sees
                # the real error.
                logger.warning(f"[{req_id}] gpt-image-1 hard-failed: {e_new}")
                raise
            logger.info(
                f"[{req_id}] gpt-image-1 unavailable on this key, trying dall-e-3"
            )

        # Fall back to OPENAI_IMAGE_MODEL (configurable, defaults to dall-e-3).
        try:
            from core.model_registry import OPENAI_IMAGE_MODEL as _OPENAI_IMG_MODEL
            r = client.images.generate(
                model=_OPENAI_IMG_MODEL,
                prompt=full_prompt,
                size=size,
                quality="hd",
                n=1,
            )
            return _to_bytes(r)
        except Exception as e_old:
            msg = str(e_old).lower()
            if "does not exist" in msg or "not found" in msg:
                # Neither model available on this account.
                raise RuntimeError(
                    "OpenAI account has neither gpt-image-1 nor dall-e-3 enabled. "
                    "Enable an image model at platform.openai.com → Limits → Models."
                ) from e_old
            raise

    primary, fallback = ((_call_gemini, _call_openai)
                         if req.provider == "gemini"
                         else (_call_openai, _call_gemini))
    _primary_provider  = req.provider
    _fallback_provider = "openai" if req.provider == "gemini" else "gemini"

    # Wall-clock latency around the generation call — same source docs/chat use.
    _img_t0 = time.time()
    _actual_provider = _primary_provider

    # Run blocking SDK calls in thread pool
    try:
        img_bytes = await _run_in_pool(loop, primary)
        logger.info(f"[{req_id}] imagen done (primary) | bytes={len(img_bytes)}")
    except Exception as primary_exc:
        # Log the FULL primary error with traceback so we can see why Imagen
        # failed instead of only seeing the fallback's error later.
        logger.warning(
            f"[{req_id}] primary imagen provider ({req.provider}) failed: "
            f"{type(primary_exc).__name__}: {primary_exc}",
            exc_info=True,
        )
        try:
            img_bytes = await _run_in_pool(loop, fallback)
            _actual_provider = _fallback_provider
            logger.info(f"[{req_id}] imagen done (fallback) | bytes={len(img_bytes)}")
        except Exception as fb_exc:
            logger.error(
                f"[{req_id}] both imagen providers failed | "
                f"primary({req.provider})={type(primary_exc).__name__}: {primary_exc} | "
                f"fallback={type(fb_exc).__name__}: {fb_exc}",
                exc_info=True,
            )
            raise HTTPException(
                500,
                {
                    "error":    "image_generation_failed",
                    "primary":  f"{type(primary_exc).__name__}: {primary_exc}",
                    "fallback": f"{type(fb_exc).__name__}: {fb_exc}",
                },
            )

    # ── Metadata (model, in_tok, out_tok, cost, latency) ──────────
    # Same mechanism chat/doc responses use: token counts come from the
    # provider's usage_metadata (captured into _meta by whichever _call_*
    # succeeded); cost = _image_cost(model, in, out) via
    # core.model_registry.MODEL_COST_PER_1M. OpenAI image models expose no
    # token usage, so their in/out tokens stay 0.
    _latency  = time.time() - _img_t0
    _img_model = _meta["model"]
    _in_tok    = int(_meta["in_tok"]  or 0)
    _out_tok   = int(_meta["out_tok"] or 0)
    _img_cost  = _image_cost(_img_model, _in_tok, _out_tok)
    logger.info(
        f"[{req_id}] imagen meta | provider={_actual_provider} model={_img_model} "
        f"in={_in_tok} out={_out_tok} cost={_img_cost:.6f} latency={_latency:.2f}s"
    )

    return _Response(
        content=img_bytes,
        media_type="image/png",
        headers={
            "X-Imagen-Provider": _actual_provider,
            "X-Imagen-Aspect":   aspect,
            # X-Imagen-* names consumed by gateway_gemini.generate_imagen():
            # it relays the ACTUAL post-fallback model id via _img_meta["model"]
            # and populates its _last_input_tokens / _last_output_tokens from
            # these (0/0 for OpenAI images, which have no token accounting).
            # These MUST match the reader in the root gateway — the older
            # gateway only read X-Imagen-Provider/X-Imagen-Model.
            "X-Imagen-Model":         _img_model,
            "X-Imagen-Input-Tokens":  str(_in_tok),
            "X-Imagen-Output-Tokens": str(_out_tok),
            # Standard metadata chips (mirrors routers/chat_router.py image
            # response so MessageMeta renders model/token/cost like chat/doc).
            "X-Model-Label":     _img_model,
            "X-Provider":        _actual_provider,
            "X-Input-Tokens":    str(_in_tok),
            "X-Output-Tokens":   str(_out_tok),
            "X-Token-Usage":     str(_in_tok + _out_tok),
            "X-Cost-USD":        f"{_img_cost:.6f}",
            "X-Latency-Ms":      str(int(_latency * 1000)),
        },
    )


# ── Text → Video generation (Veo 3.1) ─────────────────────────
#
# Cloud-egress shim for Google Veo 3.1. The main gateway has already
# enforced:
#   - JWT auth
#   - VEO_ENABLED flag
#   - per-user budget
# Per-user access is governed by model governance tables
# (dept_model_permissions / user_model_permissions) — same as every other model.
# This endpoint only knows how to:
#   1. Submit the LRO via the Google GenAI SDK
#   2. Poll until done (60–180 s typical)
#   3. Download and return raw MP4 bytes
#
# Errors surface as JSON with HTTP 5xx so the gateway can re-raise a
# meaningful 502 to the UI.
@app.post("/llm/veo")
async def veo(req: VeoRequest):
    req_id = str(uuid.uuid4())[:8]
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(400, "prompt is required")

    # Clamp duration server-side to match gateway-side caps
    # (gateway uses 2..16). Defence-in-depth: never trust the upstream.
    _VEO_MIN, _VEO_MAX = 2, 16
    duration_secs = max(_VEO_MIN, min(_VEO_MAX, int(req.duration_secs or 8)))
    aspect        = (req.aspect_ratio or "16:9").strip() or "16:9"

    logger.info(
        f"[{req_id}] VEO REQUEST | aspect={aspect} | duration={duration_secs}s | "
        f"prompt={req.prompt[:60].replace(chr(10),' ')!r}..."
    )

    if _gemini_gw is None:
        raise HTTPException(503, {"error": "veo_unavailable", "detail": "Gemini gateway not initialised"})

    loop = asyncio.get_running_loop()

    # Veo LROs typically take 60–180 s. Run the blocking SDK call in the
    # thread pool so the proxy's event loop stays responsive.
    def _call_veo():
        return _gemini_gw.generate_veo_video(
            prompt=req.prompt.strip(),
            aspect_ratio=aspect,
            duration_secs=duration_secs,
        )

    try:
        video_bytes, err = await _run_in_pool(loop, _call_veo)
    except Exception as exc:
        logger.error(f"[{req_id}] veo exec failure: {exc}", exc_info=True)
        raise HTTPException(
            500,
            {"error": "veo_generation_failed", "detail": f"{type(exc).__name__}: {exc}"},
        )

    if not video_bytes:
        logger.error(f"[{req_id}] veo returned no bytes | err={err!r}")
        raise HTTPException(
            502,
            {"error": "veo_generation_failed", "detail": err or "empty response"},
        )

    logger.info(f"[{req_id}] veo done | bytes={len(video_bytes)} duration={duration_secs}s")
    return _Response(
        content=video_bytes,
        media_type="video/mp4",
        headers={
            "X-Veo-Duration": str(duration_secs),
            "X-Veo-Aspect":   aspect,
        },
    )


# ── Atlassian proxy ────────────────────────────────────────────
#
# Jira and Confluence are Atlassian Cloud — only reachable from the LLM proxy server
# (which has outbound internet access via the Squid forward proxy).
# the main gateway cannot reach Atlassian directly, so it sends
# all Jira/Confluence HTTP calls here.
#
# Protocol (POST /atlassian/proxy):
#   Request JSON:
#     service  — "jira" | "confluence"
#     method   — "GET" | "POST" | "PUT" | "DELETE"
#     path     — API path, e.g. "/rest/api/3/issue/SCRUM-1"
#                or "/rest/api/content?spaceKey=ENG&limit=50"
#     body     — optional JSON payload dict
#     email    — required per-user Atlassian email
#     token    — required per-user Atlassian API token
#                (service-account credentials are never used; 403 if omitted)
#   Response:
#     Passes the Atlassian HTTP response back verbatim (same status code + JSON body).
#
# Env vars required on the LLM proxy server (.env):
#   JIRA_URL            — https://your-org.atlassian.net
#   CONFLUENCE_URL      — https://your-org.atlassian.net/wiki
# ──────────────────────────────────────────────────────────────

import base64 as _b64
import httpx as _httpx


class AtlassianProxyRequest(BaseModel):
    service: str                      # jira | confluence
    method:  str                      # GET | POST | PUT | DELETE
    path:    str                      # /rest/api/3/issue/SCRUM-1
    body:    Optional[dict] = None
    email:   Optional[str] = None     # per-user override (resolved on the gateway)
    token:   Optional[str] = None     # per-user override
    request_id: Optional[str] = None # caller's request_id for log correlation
    chat_id:    Optional[str] = None  # caller's chat_id for conversation correlation


def _atlassian_base(service: str) -> str:
    if service == "jira":
        return os.getenv("JIRA_URL", "").rstrip("/")
    if service == "confluence":
        return os.getenv("CONFLUENCE_URL", "").rstrip("/")
    return ""



@app.post("/atlassian/proxy")
async def atlassian_proxy(req: AtlassianProxyRequest, request: Request):
    """
    Forward a Jira or Confluence API call from the gateway to Atlassian Cloud.
    The gateway MUST supply per-user credentials (email + token); service-account
    credentials are never used as a fallback.
    """
    # ── Correlation ID binding ─────────────────────────────────────────────────
    from core.logger import set_request_id, set_chat_context
    upstream_request_id = (
            req.request_id
            or request.headers.get("x-request-id")
            or request.headers.get("X-Request-ID")
    )
    upstream_chat_id = (
            req.chat_id
            or request.headers.get("x-chat-id")
            or request.headers.get("X-Chat-ID")
    )
    if upstream_request_id:
        set_request_id(upstream_request_id)
    if upstream_chat_id:
        set_chat_context("-", upstream_chat_id)
    # ──────────────────────────────────────────────────────────────────────────
    service = req.service.lower()
    if service not in ("jira", "confluence"):
        raise HTTPException(400, f"Unknown service {req.service!r} — use 'jira' or 'confluence'")

    base = _atlassian_base(service)
    if not base:
        raise HTTPException(
            503,
            f"{service.upper()}_URL not configured on LLM proxy — "
            f"add it to the LLM proxy server .env",
        )

    # Auth: per-user credentials are required — no service-account fallback.
    email = req.email
    token = req.token

    if not email or not token:
        raise HTTPException(
            403,
            f"No Atlassian personal access token provided for {service}. "
            f"Please add your Atlassian token under Profile → Atlassian Token.",
        )

    creds   = _b64.b64encode(f"{email}:{token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    url     = f"{base}{req.path}"
    content = json.dumps(req.body).encode() if req.body is not None else None

    try:
        import ssl
        _ssl_verify = os.getenv("SSL_VERIFY", "true").lower() == "true"
        if not _ssl_verify:
            logger.warning(
                "SSL_VERIFY is disabled — TLS certificate verification is OFF. "
                "Only enable this for internal/dev environments."
            )
        _ssl_ca = os.getenv("SSL_CA_BUNDLE")
        _httpx_verify: bool | str = _ssl_ca if _ssl_ca else _ssl_verify
        async with _httpx.AsyncClient(timeout=30.0, verify=_httpx_verify) as client:
            atlassian_resp = await client.request(
                method  = req.method.upper(),
                url     = url,
                content = content,
                headers = headers,
            )

        logger.info(
            f"Atlassian proxy [{service}] {req.method.upper()} {req.path} "
            f"→ HTTP {atlassian_resp.status_code} | "
            f"upstream_request_id={upstream_request_id or '-'} | "
            f"upstream_chat_id={upstream_chat_id or '-'}"
        )

        # Parse body — Atlassian always returns JSON for API calls
        try:
            result = atlassian_resp.json()
        except Exception:
            result = {"raw": atlassian_resp.text}

        # Return verbatim — let the caller (jira_tools / confluence_tools) handle
        # 4xx / 5xx the same way it handles direct HTTP errors.
        return JSONResponse(content=result, status_code=atlassian_resp.status_code)

    except _httpx.TimeoutException:
        logger.error(f"Atlassian proxy timeout [{service}] {req.method} {req.path}")
        raise HTTPException(504, f"Atlassian {service} request timed out")
    except Exception as exc:
        logger.error(f"Atlassian proxy error [{service}] {req.method} {req.path}: {exc}")
        raise HTTPException(502, f"Atlassian proxy failed: {exc}")

class AtlassianAttachmentRequest(BaseModel):
    service:      str                     # jira (only jira supports attachments here)
    issue_key:    str                     # SCRUM-1
    filename:     str
    content_b64:  str                     # base64-encoded file bytes
    content_type: Optional[str] = "application/octet-stream"
    email:        Optional[str] = None    # per-user override (resolved on app02)
    token:        Optional[str] = None    # per-user override
    request_id:   Optional[str] = None    # caller's request_id for log correlation
    chat_id:      Optional[str] = None    # caller's chat_id for conversation correlation


@app.post("/atlassian/attachment")
async def atlassian_attachment(req: AtlassianAttachmentRequest, request: Request):
    """
    Forward a Jira ATTACHMENT upload from app02 to Atlassian Cloud.

    The generic /atlassian/proxy relay is JSON-only (it sets Content-Type:
    application/json and sends json.dumps(body)), which cannot carry a Jira
    attachment. Jira attachment upload requires multipart/form-data plus the
    header `X-Atlassian-Token: no-check`, so it MUST physically execute here on
    web02 (the only host with egress). This is the sole prod path for evidence
    bundle attachments (governance evidence → linked Change ticket, V7).

    app02 MUST supply per-user credentials (email + token); service-account
    credentials are never used as a fallback — identical to /atlassian/proxy.
    """
    # ── Correlation ID binding (mirror atlassian_proxy) ─────────────────────────
    from core.logger import set_request_id, set_chat_context
    upstream_request_id = (
            req.request_id
            or request.headers.get("x-request-id")
            or request.headers.get("X-Request-ID")
    )
    upstream_chat_id = (
            req.chat_id
            or request.headers.get("x-chat-id")
            or request.headers.get("X-Chat-ID")
    )
    if upstream_request_id:
        set_request_id(upstream_request_id)
    if upstream_chat_id:
        set_chat_context("-", upstream_chat_id)
    # ──────────────────────────────────────────────────────────────────────────
    service = (req.service or "jira").lower()
    if service != "jira":
        raise HTTPException(400, f"Attachment relay only supports 'jira', got {req.service!r}")

    base = _atlassian_base("jira")
    if not base:
        raise HTTPException(
            503,
            "JIRA_URL not configured on LLM proxy — add it to web02 .env",
        )

    # Auth: per-user credentials are required — no service-account fallback.
    email = req.email
    token = req.token
    if not email or not token:
        raise HTTPException(
            403,
            "No Atlassian personal access token provided for jira. "
            "Please add your Atlassian token under Profile → Atlassian Token.",
        )

    try:
        content_bytes = _b64.b64decode(req.content_b64)
    except Exception as exc:
        raise HTTPException(400, f"Invalid base64 attachment content: {exc}")

    creds   = _b64.b64encode(f"{email}:{token}".encode()).decode()
    headers = {
        "Authorization":       f"Basic {creds}",
        "X-Atlassian-Token":   "no-check",   # REQUIRED for Jira attachment upload
        "Accept":              "application/json",
        # NOTE: do NOT set Content-Type — httpx sets the multipart boundary.
    }
    # Host is fixed to JIRA_URL (not an open relay).
    url = f"{base}/rest/api/3/issue/{req.issue_key}/attachments"
    files = {"file": (req.filename, content_bytes, req.content_type or "application/octet-stream")}

    try:
        _ssl_verify = os.getenv("SSL_VERIFY", "true").lower() == "true"
        if not _ssl_verify:
            logger.warning(
                "SSL_VERIFY is disabled — TLS certificate verification is OFF. "
                "Only enable this for internal/dev environments."
            )
        _ssl_ca = os.getenv("SSL_CA_BUNDLE")
        _httpx_verify: bool | str = _ssl_ca if _ssl_ca else _ssl_verify
        async with _httpx.AsyncClient(timeout=30.0, verify=_httpx_verify) as client:
            atlassian_resp = await client.post(url, headers=headers, files=files)

        logger.info(
            f"atlassian_attachment [jira] {req.issue_key} file={req.filename} "
            f"→ HTTP {atlassian_resp.status_code} | "
            f"upstream_request_id={upstream_request_id or '-'} | "
            f"upstream_chat_id={upstream_chat_id or '-'}"
        )

        try:
            result = atlassian_resp.json()
        except Exception:
            result = {"raw": atlassian_resp.text}

        # Return verbatim — the caller (jira_tools.jira_add_attachment) handles
        # 4xx / 5xx the same way it handles direct HTTP errors.
        return JSONResponse(content=result, status_code=atlassian_resp.status_code)

    except _httpx.TimeoutException:
        logger.error(f"atlassian_attachment timeout [jira] {req.issue_key} file={req.filename}")
        raise HTTPException(504, "Atlassian jira attachment request timed out")
    except Exception as exc:
        logger.error(f"atlassian_attachment error [jira] {req.issue_key} file={req.filename}: {exc}")
        raise HTTPException(502, f"Atlassian attachment relay failed: {exc}")


# ── OpenAI Responses API endpoint ─────────────────────────────
#
# POST /llm/responses
#
# Calls OpenAI's Responses API (client.responses.create / .stream)
# for deep-research and GPT-5.4 use-cases.
# Wire format (ndjson, streaming):
#   {"delta": "text chunk"}                   — output_text delta
#   {"output_text": "full", "in_tok": N, "out_tok": N}  — final line
#   {"error": "message"}                      — on failure
# Non-streaming returns JSON directly.
# ──────────────────────────────────────────────────────────────

from typing import Union as _Union

class ResponsesRequest(BaseModel):
    model:             str
    input:             _Union[str, list]
    stream:            bool          = True
    tools:             Optional[list] = None
    max_output_tokens: Optional[int]  = None


@app.post("/llm/responses")
async def responses_endpoint(req: ResponsesRequest, request: Request):
    """
    Call OpenAI Responses API (responses.create / responses.stream).
    Requires openai>=1.50.0 on the LLM proxy server.
    """
    if _openai_gw is None:
        raise HTTPException(503, "OpenAI gateway not available")

    req_id = str(uuid.uuid4())[:8]
    caller = request.client.host if request.client else "unknown"
    _input_preview = req.input[:80] if isinstance(req.input, str) else str(req.input)[:80]
    logger.info(
        f"[{req_id}] RESPONSES from {caller} | model={req.model} "
        f"stream={req.stream} tools={len(req.tools or [])} | {_input_preview!r}..."
    )

    if not req.stream:
        def _run_sync():
            return _openai_gw.responses_create(
                model=req.model,
                input=req.input,
                tools=req.tools or None,
                max_output_tokens=req.max_output_tokens,
            )

        loop = asyncio.get_running_loop()
        try:
            result = await _run_in_pool(loop, _run_sync)
            logger.info(f"[{req_id}] RESPONSES done | in={result['in_tok']} out={result['out_tok']}")
            return result
        except Exception as exc:
            logger.error(f"[{req_id}] RESPONSES non-stream error: {exc}")
            raise HTTPException(500, str(exc))

    # Streaming path — thread pool + queue (responses_stream() is sync)
    queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
    loop = asyncio.get_running_loop()

    def _put(item):
        future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        try:
            future.result(timeout=30)
        except Exception as e:
            logger.warning(f"[{req_id}] queue put failed: {e}")

    def _run_stream():
        try:
            for chunk in _openai_gw.responses_stream(
                    model=req.model,
                    input=req.input,
                    tools=req.tools or None,
                    max_output_tokens=req.max_output_tokens,
            ):
                _put(json.dumps(chunk) + "\n")
        except Exception as exc:
            logger.error(f"[{req_id}] RESPONSES stream error: {exc}")
            _put(json.dumps({"error": str(exc)}) + "\n")
        finally:
            _put(None)

    try:
        loop.run_in_executor(_pool, _run_stream)
    except _queue.Full:
        raise HTTPException(503, "LLM proxy is at capacity — too many concurrent requests. Retry shortly.")

    async def _stream():
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


# ── PPT image generation endpoint (text → image bytes) ────────

@app.post("/llm/generate-ppt-image")
async def generate_ppt_image(req: PptImageRequest, request: Request):
    """
    Text-prompt → image bytes for PPTX slide backgrounds.
    Returns JSON: {"image_b64": "<base64-encoded PNG/JPEG>", "mime_type": "image/png"}
    Primary: Gemini Imagen 3 Fast
    Fallback: DALL-E 3 (if OPENAI_API_KEY available)
    Fails with 503 if both are unavailable.
    """
    # ── Correlation ID binding ─────────────────────────────────
    from core.logger import set_request_id, set_chat_context
    upstream_request_id = (
            req.request_id
            or request.headers.get("x-request-id")
            or request.headers.get("X-Request-ID")
    )
    upstream_chat_id = (
            req.chat_id
            or request.headers.get("x-chat-id")
            or request.headers.get("X-Chat-ID")
    )
    req_id = upstream_request_id or str(uuid.uuid4())
    set_request_id(req_id)
    if upstream_chat_id:
        set_chat_context("-", upstream_chat_id)
    # ──────────────────────────────────────────────────────────

    import base64 as _b64

    provider = (req.provider or "auto").lower().strip()
    logger.info(
        f"[{req_id}] PPT-IMAGE REQUEST | provider={provider} | "
        f"prompt={req.prompt[:80].replace(chr(10),' ')!r}..."
    )

    loop = asyncio.get_running_loop()
    img_bytes: bytes | None = None
    mime_type = "image/png"
    # Metadata (model, in_tok, out_tok, cost, latency) — same mechanism
    # chat/doc responses use. Populated from the gateway's captured
    # usage_metadata for Gemini; DALL-E exposes no token usage so it stays 0.
    _img_t0          = time.time()
    _actual_provider = None
    _img_model       = ""
    _in_tok          = 0
    _out_tok         = 0

    # ── Try Gemini Imagen first (unless explicitly 'dalle') ────
    if provider in ("auto", "gemini") and _gemini_gw is not None:
        # Read the gateway's captured token/model metadata INSIDE the worker
        # thread (the gateway stores them thread-locally, so they must be read
        # on the same thread that ran the SDK call) and return them alongside
        # the bytes — mirrors the _call_image_gemini vision pattern above.
        def _run_gemini() -> tuple[bytes | None, str, int, int]:
            from core.logger import set_request_id as _sri, set_chat_context as _scc
            _sri(req_id)
            if (upstream_chat_id or "-") != "-":
                _scc("-", upstream_chat_id)
            _bytes = _gemini_gw.generate_imagen(req.prompt)
            return (
                _bytes,
                getattr(_gemini_gw, "_last_imagen_model", None) or "",
                int(getattr(_gemini_gw, "_last_input_tokens",  0) or 0),
                int(getattr(_gemini_gw, "_last_output_tokens", 0) or 0),
            )

        try:
            img_bytes, _img_model, _in_tok, _out_tok = await _run_in_pool(loop, _run_gemini)
            if img_bytes:
                _actual_provider = "gemini"
                logger.info(f"[{req_id}] PPT-IMAGE DONE via Gemini Imagen ({len(img_bytes)} bytes)")
        except Exception as exc:
            logger.warning(f"[{req_id}] PPT-IMAGE Gemini failed: {exc}")
            img_bytes = None

    # ── Fallback to DALL-E 3 ───────────────────────────────────
    if img_bytes is None and provider in ("auto", "dalle") and _openai_gw is not None:
        def _run_dalle():
            from core.logger import set_request_id as _sri, set_chat_context as _scc
            _sri(req_id)
            if (upstream_chat_id or "-") != "-":
                _scc("-", upstream_chat_id)
            return _openai_gw.generate_image_dalle(req.prompt)

        try:
            img_bytes = await _run_in_pool(loop, _run_dalle)
            if img_bytes:
                mime_type = "image/png"
                # DALL-E exposes no token usage, so tokens stay 0.
                _actual_provider = "openai"
                from core.model_registry import OPENAI_IMAGE_MODEL as _PPT_IMG_MODEL
                _img_model = _PPT_IMG_MODEL
                logger.info(f"[{req_id}] PPT-IMAGE DONE via DALL-E ({len(img_bytes)} bytes)")
        except Exception as exc:
            logger.warning(f"[{req_id}] PPT-IMAGE DALL-E failed: {exc}")
            img_bytes = None

    if img_bytes is None:
        logger.error(f"[{req_id}] PPT-IMAGE FAILED: no provider available or all failed")
        raise HTTPException(503, "Image generation unavailable — no provider succeeded")

    _latency  = time.time() - _img_t0
    _img_cost = _image_cost(_img_model, _in_tok, _out_tok)
    logger.info(
        f"[{req_id}] PPT-IMAGE meta | provider={_actual_provider} model={_img_model} "
        f"in={_in_tok} out={_out_tok} cost={_img_cost:.6f} latency={_latency:.2f}s"
    )

    return {
        "image_b64": _b64.b64encode(img_bytes).decode(),
        "mime_type": mime_type,
        # Metadata footer — mirrors chat/doc responses so the caller can render
        # model/token/cost/latency chips instead of a blank image chip.
        "model":     _img_model,
        "provider":  _actual_provider,
        "in_tok":    _in_tok,
        "out_tok":   _out_tok,
        "cost":      _img_cost,
        "latency":   _latency,
    }

    # ── TTS endpoint ───────────────────────────────────────────────
# Proxies text → OpenAI TTS → returns audio/mpeg bytes.
# Lives here (the LLM proxy server) because the gateway has no outbound internet access.
# Called by gateway.py voice_tts when LLM_PROXY_URL is set.

class _TtsRequest(BaseModel):
    text:  str
    voice: str   = "nova"      # alloy | echo | fable | onyx | nova | shimmer
    model: str   = "tts-1-hd"  # tts-1 | tts-1-hd
    speed: float = 0.92        # 0.25–4.0


@app.post("/llm/tts")
async def llm_tts(req: _TtsRequest):
    """Convert text to speech via OpenAI TTS. Returns audio/mpeg.

    This endpoint lives on llm_proxy (the LLM proxy server) which has outbound internet.
    It calls OpenAI directly, using HTTPS_PROXY (Squid) if set for
    environments where outbound traffic must go through a forward proxy.
    gateway.py routes TTS requests here when LLM_PROXY_URL is configured.
    """
    from fastapi.responses import Response as _Resp

    text = req.text.strip()[:2000]
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    # ── Direct OpenAI call (uses HTTPS_PROXY / Squid on the LLM proxy server for outbound)

    # Use ProxyKeyCache first (key fetched from app02 at startup),
    # fall back to os.getenv() for local dev / backward compat.
    from core.proxy_key_client import ProxyKeyCache as _PKC_tts
    api_key = _PKC_tts.instance().get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        logger.error("[TTS] OPENAI_API_KEY not available — cannot serve TTS")
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY not configured on proxy")

    # Respect HTTPS_PROXY env var (Squid forward proxy on the LLM proxy server)
    proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or None
    transport = _httpx.AsyncHTTPTransport(proxy=proxy_url) if proxy_url else None

    try:
        async with _httpx.AsyncClient(timeout=60.0, transport=transport) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": req.model,
                    "voice": req.voice,
                    "input": text,
                    "speed": req.speed,
                },
            )
            resp.raise_for_status()
            logger.info(f"[TTS] OK — {len(resp.content)} bytes, voice={req.voice}, model={req.model}")
            return _Resp(content=resp.content, media_type="audio/mpeg")
    except _httpx.HTTPStatusError as e:
        logger.error(f"[TTS] OpenAI error {e.response.status_code}: {e.response.text[:300]}")
        raise HTTPException(status_code=502, detail=f"OpenAI TTS error: {e.response.status_code}")
    except _httpx.ConnectError as e:
        logger.error(f"[TTS] ConnectError — outbound blocked or proxy misconfigured: {e}")
        raise HTTPException(status_code=502, detail="TTS connect failed — check HTTPS_PROXY on the LLM proxy server")
    except _httpx.TimeoutException as e:
        logger.error(f"[TTS] Timeout calling OpenAI TTS: {e}")
        raise HTTPException(status_code=504, detail="TTS request timed out")
    except Exception as e:
        logger.error(f"[TTS] Unexpected error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail="TTS request failed")


# ── Generic outbound relay (POST /net/forward) ────────────────────────────────
# this host has no internet egress — only the LLM proxy server does. Connectors that must
# reach the internet (Microsoft 365 / Graph, OAuth token endpoints) relay through
# this endpoint, same idea as /atlassian/proxy but for arbitrary URLs. Host-
# allowlisted so it is NOT an open proxy. httpx here uses the LLM proxy server's HTTPS_PROXY
# (Squid) automatically via trust_env.
class NetForwardRequest(BaseModel):
    method:    str
    url:       str
    headers:   Optional[dict]  = None
    params:    Optional[dict]  = None
    data:      Optional[dict]  = None   # form-encoded body (OAuth token swap)
    json_body: Optional[dict]  = None   # JSON body (Graph writes)
    content_b64: Optional[str] = None   # base64 raw binary body (e.g. OneDrive upload)
    timeout:   Optional[float] = 30.0


# Hosts the relay is permitted to reach. Extend as new connectors are added.
_NET_FORWARD_ALLOW = (
    "login.microsoftonline.com",
    "graph.microsoft.com",
    "api.atlassian.com",   # Atlassian 3LO cloudId resolution (accessible-resources)
    "auth.atlassian.com",  # Atlassian OAuth token endpoint
)


@app.post("/net/forward")
async def net_forward(req: NetForwardRequest):
    """Relay one outbound HTTPS call from the LLM proxy server for an app-server caller with no
    egress. Returns {status, content_type, text} — the caller reconstructs the
    response (see connectors/net_relay.py)."""
    from urllib.parse import urlparse as _urlparse
    host = (_urlparse(req.url).hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in _NET_FORWARD_ALLOW):
        raise HTTPException(403, f"host not allowed for /net/forward: {host!r}")

    try:
        async with _httpx.AsyncClient(timeout=req.timeout or 30.0) as client:
            kwargs: dict = {"headers": req.headers or {}}
            if req.params is not None:    kwargs["params"] = req.params
            if req.data is not None:      kwargs["data"] = req.data
            if req.json_body is not None: kwargs["json"] = req.json_body
            if req.content_b64 is not None:
                import base64 as _b64
                kwargs["content"] = _b64.b64decode(req.content_b64)
            resp = await client.request(req.method.upper(), req.url, **kwargs)

        logger.info(f"net_forward {req.method.upper()} {host} → HTTP {resp.status_code}")
        return JSONResponse(
            content={
                "status":       resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "text":         resp.text,
            },
            status_code=200,
        )
    except _httpx.TimeoutException:
        logger.error(f"net_forward timeout {req.method} {req.url}")
        raise HTTPException(504, "forward request timed out")
    except Exception as exc:
        logger.error(f"net_forward error {req.method} {req.url}: {exc}")
        raise HTTPException(502, f"forward failed: {exc}")


# ── Spend-API passthroughs ────────────────────────────────────
#
# Used by services/llm_spend/fetchers/* on the gateway. Three providers, three
# upstreams:
#
#   POST /spend/anthropic/{cost_report,usage_report}
#       → https://api.anthropic.com/v1/organizations/{cost_report,usage_report/messages}
#       auth: x-api-key (ANTHROPIC_ADMIN_API_KEY)
#
#   POST /spend/openai/{costs,usage}
#       → https://api.openai.com/v1/organization/{costs,usage/completions}
#       auth: Authorization: Bearer (OPENAI_ADMIN_API_KEY)
#
#   POST /spend/gcp/bigquery
#       → BigQuery client (GCP_BILLING_SA_JSON service-account)
#       Runs a fixed parameterized SQL — caller only supplies the date window.
#       The SQL template / project / table live server-side so a compromised
#       caller cannot exfiltrate arbitrary rows the SA can read.
#
# All three reuse the existing _InternalTokenMiddleware (anything not in
# _INTERNAL_TOKEN_EXEMPT_PATHS is covered automatically) and the 32 MB
# _BodySizeMiddleware cap.
# ──────────────────────────────────────────────────────────────

# BigQuery SDK is optional — only required if /spend/gcp/bigquery is called.
# Guarded import lets the proxy still serve LLM traffic without the dep.
try:
    from google.cloud import bigquery as _bq  # type: ignore
    from google.cloud.bigquery import (  # type: ignore
        ScalarQueryParameter as _BqScalarQueryParameter,
        QueryJobConfig       as _BqQueryJobConfig,
    )
except ImportError:
    _bq = None
    _BqScalarQueryParameter = None
    _BqQueryJobConfig = None


# ── GCP service-account materialisation (lifted from gcp_billing_bq.py) ──
import atexit as _atexit
import stat as _stat
import tempfile as _tempfile
import threading as _threading

_gcp_cred_lock = _threading.Lock()
_gcp_cred_path: Optional[str] = None


def _gcp_materialise_sa_credentials() -> Optional[str]:
    """Decrypt GCP_BILLING_SA_JSON env → 0600 tempfile, export GOOGLE_APPLICATION_CREDENTIALS.

    Cached; only writes once per process. Tempfile is unlinked at process exit.
    """
    global _gcp_cred_path
    with _gcp_cred_lock:
        if _gcp_cred_path and os.path.exists(_gcp_cred_path):
            return _gcp_cred_path

        blob = os.getenv("GCP_BILLING_SA_JSON", "")
        if not blob:
            logger.warning("[spend/gcp] GCP_BILLING_SA_JSON not set")
            return None
        try:
            json.loads(blob)  # sanity check
        except Exception as e:
            logger.error(f"[spend/gcp] GCP_BILLING_SA_JSON is not valid JSON: {e}")
            return None

        fd, path = _tempfile.mkstemp(prefix="gcp_billing_sa_", suffix=".json")
        try:
            os.write(fd, blob.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(path, _stat.S_IRUSR | _stat.S_IWUSR)
        except Exception:
            pass

        def _cleanup(p: str = path) -> None:
            try:
                os.remove(p)
            except Exception:
                pass

        _atexit.register(_cleanup)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
        _gcp_cred_path = path
        logger.info(f"[spend/gcp] SA credentials materialised at {path}")
        return path


# Canonical GCP billing-export SQL. Same shape as the previous in-fetcher copy
# (services/llm_spend/fetchers/gcp_billing_bq.py); kept server-side so callers
# can never inject arbitrary SQL via the proxy.
#
# CHANGED (was missing real spend — see both fixes below):
#
#   1. Added 'Gemini API' to service.description. The two real Niveus/GCP
#      billing exports we have show ALL Gemini API
#      spend and MOST Vertex AI spend under service='Gemini API' — the
#      original 'Vertex AI' / 'Generative Language API' pair never matches
#      it. This was silently dropping nearly all Gemini spend from the live
#      BQ path (confirmed against the CSV exports).
#
#   2. Dropped the `LOWER(sku.description) LIKE '%gemini%'` filter. Real
#      SKUs under these services also cover Veo video generation, Imagen
#      image generation, Lyria audio, Agent Platform compute/memory, and
#      Grounding (Search/Maps) — none of which have "gemini" in the SKU
#      description, so this filter silently excluded them. In the June
#      export these non-Gemini-named SKUs were 86% of that month's spend
#      (Veo alone). They ARE real GCP invoice line items under the deployment's
#      account, so excluding them made llm_spend_daily's Gemini total
#      permanently understate the actual bill. The Python side now
#      classifies these as token_type='non_token' (cost tracked, no token
#      semantics) rather than dropping them at the SQL layer — see
#      services/llm_spend/fetchers/gcp_billing_bq.py classify_sku().
_GCP_SPEND_SQL_TEMPLATE = """
SELECT
  DATE(usage_start_time)            AS usage_date,
  service.description               AS service_description,
  sku.description                   AS sku_description,
  SUM(cost)                         AS cost_usd,
  SUM(usage.amount)                 AS usage_amount,
  COUNT(*)                          AS line_count
FROM `{table}`
WHERE service.description IN ('Vertex AI', 'Generative Language API', 'Gemini API')
  AND DATE(usage_start_time) BETWEEN @window_start AND @window_end
GROUP BY usage_date, service_description, sku_description
ORDER BY usage_date
"""


# ── Request schemas ──────────────────────────────────────────

class AnthropicSpendRequest(BaseModel):
    # Query params for the Anthropic admin endpoint. The fetcher builds these
    # (starting_at, ending_at, bucket_width, group_by, limit, optional page);
    # the proxy passes them through unmodified.
    params:     dict
    request_id: Optional[str] = None
    chat_id:    Optional[str] = None


class OpenAISpendRequest(BaseModel):
    # Query params for the OpenAI organization endpoint (start_time, end_time,
    # bucket_width, group_by, limit, optional page).
    params:     dict
    request_id: Optional[str] = None
    chat_id:    Optional[str] = None


class GcpBigQueryRequest(BaseModel):
    # ISO date strings — proxy parses with date.fromisoformat. Caller cannot
    # supply SQL, project, or table; those all live in env on the LLM proxy server.
    window_start: str
    window_end:   str
    request_id:   Optional[str] = None
    chat_id:      Optional[str] = None


# ── Anthropic admin passthrough ──────────────────────────────

_ANTHROPIC_ADMIN_BASE = "https://api.anthropic.com/v1/organizations"
# Map URL-suffix the fetcher hits to the upstream path. cost_report stays
# 'cost_report'; usage_report → 'usage_report/messages' on Anthropic's side.
_ANTHROPIC_REPORT_PATHS = {
    "cost_report":  "cost_report",
    "usage_report": "usage_report/messages",
}
_ANTHROPIC_MAX_429_RETRIES = 5


@app.post("/spend/anthropic/{report}")
async def spend_anthropic(report: str, req: AnthropicSpendRequest, request: Request):
    """
    Proxy a single page of the Anthropic admin cost_report or usage_report
    endpoint. Returns the upstream JSON verbatim so the caller can read
    `data` and `next_page` exactly as it would from a direct call.
    """
    upstream_path = _ANTHROPIC_REPORT_PATHS.get(report)
    if upstream_path is None:
        raise HTTPException(400, f"Unknown anthropic report: {report!r}. Use cost_report|usage_report")
    if _anthropic_admin_http is None:
        raise HTTPException(503, "Anthropic admin HTTP client not initialised")

    from core.proxy_key_client import ProxyKeyCache as _PKC_aa
    api_key = _PKC_aa.instance().get("ANTHROPIC_ADMIN_API_KEY") or os.getenv("ANTHROPIC_ADMIN_API_KEY", "")
    if not api_key:
        raise HTTPException(503, "ANTHROPIC_ADMIN_API_KEY not set on llm_proxy")

    # ── Correlation ID binding ────────────────────────────────────────────
    from core.logger import set_request_id, set_chat_context
    upstream_request_id = (
        req.request_id
        or request.headers.get("x-request-id")
        or request.headers.get("X-Request-ID")
    )
    upstream_chat_id = (
        req.chat_id
        or request.headers.get("x-chat-id")
        or request.headers.get("X-Chat-ID")
    )
    if upstream_request_id:
        set_request_id(upstream_request_id)
    if upstream_chat_id:
        set_chat_context("-", upstream_chat_id)
    # ──────────────────────────────────────────────────────────────────────

    url = f"{_ANTHROPIC_ADMIN_BASE}/{upstream_path}"
    headers = {
        "x-api-key":         api_key,
        "anthropic-version": "2023-06-01",
    }
    # Anthropic admin params arrive as a dict from the fetcher. Some upstream
    # params are array-typed (e.g. `group_by`) and Anthropic now requires the
    # bracketed key form (`group_by[]=model`) or repeated keys. httpx serialises
    # list values as repeated keys by default, so we:
    #   1. Preserve list-typed values (don't stringify the list itself).
    #   2. Rewrite known array-typed param names to the `name[]` form Anthropic
    #      expects. Currently: `group_by`.
    _ANTHROPIC_ARRAY_PARAMS = {"group_by"}
    params: list[tuple[str, str]] = []
    for k, v in (req.params or {}).items():
        key = f"{k}[]" if k in _ANTHROPIC_ARRAY_PARAMS else k
        if isinstance(v, (list, tuple)):
            for item in v:
                params.append((key, str(item)))
        else:
            # Scalar value for an array-typed param is still emitted under
            # `name[]` so a single string `group_by="model"` still works.
            params.append((key, str(v)))

    attempts = 0
    while True:
        try:
            resp = await _anthropic_admin_http.get(url, headers=headers, params=params)
        except _httpx.TimeoutException:
            logger.error(f"[spend/anthropic/{report}] upstream timeout")
            raise HTTPException(504, "Anthropic admin request timed out")
        except Exception as exc:
            logger.error(f"[spend/anthropic/{report}] upstream error: {exc}")
            raise HTTPException(502, f"Anthropic admin proxy failed: {exc}")

        if resp.status_code == 429 and attempts < _ANTHROPIC_MAX_429_RETRIES:
            backoff = min(2 ** attempts, 30)
            attempts += 1
            logger.warning(
                f"[spend/anthropic/{report}] 429 from upstream — "
                f"retry {attempts}/{_ANTHROPIC_MAX_429_RETRIES} in {backoff}s"
            )
            await asyncio.sleep(backoff)
            continue
        break

    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text}

    logger.info(
        f"[spend/anthropic/{report}] {resp.status_code} | "
        f"params_keys={[k for k, _ in params]} | rid={upstream_request_id or '-'}"
    )
    return JSONResponse(content=body, status_code=resp.status_code)


# ── OpenAI admin passthrough ─────────────────────────────────

_OPENAI_ADMIN_BASE = "https://api.openai.com/v1/organization"
_OPENAI_REPORT_PATHS = {
    "costs": "costs",
    "usage": "usage/completions",
}


@app.post("/spend/openai/{report}")
async def spend_openai(report: str, req: OpenAISpendRequest, request: Request):
    """
    Proxy a single page of the OpenAI organization costs or usage endpoint.
    """
    upstream_path = _OPENAI_REPORT_PATHS.get(report)
    if upstream_path is None:
        raise HTTPException(400, f"Unknown openai report: {report!r}. Use costs|usage")
    if _openai_admin_http is None:
        raise HTTPException(503, "OpenAI admin HTTP client not initialised")

    from core.proxy_key_client import ProxyKeyCache as _PKC_oa
    api_key = _PKC_oa.instance().get("OPENAI_ADMIN_API_KEY") or os.getenv("OPENAI_ADMIN_API_KEY", "")
    if not api_key:
        raise HTTPException(503, "OPENAI_ADMIN_API_KEY not set on llm_proxy")

    # ── Correlation ID binding ────────────────────────────────────────────
    from core.logger import set_request_id, set_chat_context
    upstream_request_id = (
        req.request_id
        or request.headers.get("x-request-id")
        or request.headers.get("X-Request-ID")
    )
    upstream_chat_id = (
        req.chat_id
        or request.headers.get("x-chat-id")
        or request.headers.get("X-Chat-ID")
    )
    if upstream_request_id:
        set_request_id(upstream_request_id)
    if upstream_chat_id:
        set_chat_context("-", upstream_chat_id)
    # ──────────────────────────────────────────────────────────────────────

    url = f"{_OPENAI_ADMIN_BASE}/{upstream_path}"
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {k: str(v) for k, v in (req.params or {}).items()}

    # OpenAI's original fetcher used a constant 2-second sleep on 429 with
    # implicit unlimited retries. Bound it to 5 to avoid request-tail latency
    # blowing up under sustained throttling.
    attempts = 0
    while True:
        try:
            resp = await _openai_admin_http.get(url, headers=headers, params=params)
        except _httpx.TimeoutException:
            logger.error(f"[spend/openai/{report}] upstream timeout")
            raise HTTPException(504, "OpenAI admin request timed out")
        except Exception as exc:
            logger.error(f"[spend/openai/{report}] upstream error: {exc}")
            raise HTTPException(502, f"OpenAI admin proxy failed: {exc}")

        if resp.status_code == 429 and attempts < 5:
            attempts += 1
            logger.warning(
                f"[spend/openai/{report}] 429 from upstream — retry {attempts}/5 in 2s"
            )
            await asyncio.sleep(2)
            continue
        break

    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text}

    logger.info(
        f"[spend/openai/{report}] {resp.status_code} | "
        f"params_keys={list(params.keys())} | rid={upstream_request_id or '-'}"
    )
    return JSONResponse(content=body, status_code=resp.status_code)


# ── GCP BigQuery passthrough ─────────────────────────────────

def _run_gcp_spend_query(window_start_iso: str, window_end_iso: str) -> list[dict]:
    """
    Blocking helper — runs in the shared thread pool. Returns a list of dicts
    ready to JSON-serialise. Decimal/date values are stringified so the wire
    format is stable across Python versions.
    """
    if _bq is None:
        raise RuntimeError(
            "google-cloud-bigquery not installed on llm_proxy host — "
            "add it to services/llm_proxy/requirements.txt"
        )

    cred_path = _gcp_materialise_sa_credentials()
    if not cred_path:
        raise RuntimeError("GCP credentials unavailable — cannot query billing export")

    project = os.getenv("GCP_BILLING_BQ_PROJECT", "")
    table   = os.getenv("GCP_BILLING_BQ_TABLE",   "")
    if not project or not table:
        raise RuntimeError("GCP_BILLING_BQ_PROJECT / GCP_BILLING_BQ_TABLE not set")

    client = _bq.Client(project=project)
    sql = _GCP_SPEND_SQL_TEMPLATE.format(table=table)
    cfg = _BqQueryJobConfig(query_parameters=[
        _BqScalarQueryParameter("window_start", "DATE", window_start_iso),
        _BqScalarQueryParameter("window_end",   "DATE", window_end_iso),
    ])
    rows = client.query(sql, job_config=cfg).result()

    out: list[dict] = []
    for r in rows:
        usage_date = r["usage_date"]
        out.append({
            "usage_date":          usage_date.isoformat() if hasattr(usage_date, "isoformat") else str(usage_date),
            "service_description": r["service_description"] or "",
            "sku_description":     r["sku_description"]     or "",
            # Stringify to preserve Decimal precision across the JSON boundary.
            "cost_usd":            str(r["cost_usd"] if r["cost_usd"] is not None else 0),
            "usage_amount":        int(r["usage_amount"] or 0),
            "line_count":          int(r["line_count"]   or 0),
        })
    return out


@app.post("/spend/gcp/bigquery")
async def spend_gcp_bigquery(req: GcpBigQueryRequest, request: Request):
    """
    Execute the canonical GCP billing-export query for [window_start, window_end]
    (inclusive of both endpoints) and return aggregated rows.
    """
    if _bq is None:
        raise HTTPException(503, "google-cloud-bigquery not installed on llm_proxy host")

    # Validate date inputs server-side — never trust the caller's strings.
    try:
        from datetime import date as _date
        _date.fromisoformat(req.window_start)
        _date.fromisoformat(req.window_end)
    except Exception:
        raise HTTPException(400, "window_start and window_end must be ISO dates (YYYY-MM-DD)")

    # ── Correlation ID binding ────────────────────────────────────────────
    from core.logger import set_request_id, set_chat_context
    upstream_request_id = (
        req.request_id
        or request.headers.get("x-request-id")
        or request.headers.get("X-Request-ID")
    )
    upstream_chat_id = (
        req.chat_id
        or request.headers.get("x-chat-id")
        or request.headers.get("X-Chat-ID")
    )
    if upstream_request_id:
        set_request_id(upstream_request_id)
    if upstream_chat_id:
        set_chat_context("-", upstream_chat_id)
    # ──────────────────────────────────────────────────────────────────────

    loop = asyncio.get_running_loop()
    try:
        rows = await _run_in_pool(loop, _run_gcp_spend_query, req.window_start, req.window_end)
    except RuntimeError as e:
        logger.error(f"[spend/gcp/bigquery] config error: {e}")
        raise HTTPException(503, str(e))
    except Exception as e:
        logger.error(f"[spend/gcp/bigquery] query failed: {e}")
        raise HTTPException(502, f"BigQuery query failed: {e}")

    logger.info(
        f"[spend/gcp/bigquery] {req.window_start}..{req.window_end} "
        f"| rows={len(rows)} | rid={upstream_request_id or '-'}"
    )
    return JSONResponse(content={"rows": rows})


# ── Web Search endpoint ────────────────────────────────────────
#
# POST /llm/web-search
#
# The ONLY endpoint that executes web-search tool calls. Runs exclusively
# on web02 which has outbound internet access via the Squid forward proxy.
# app02 (no internet egress) calls this endpoint after governance, budget,
# and audit checks have already passed on its side.
#
# Request JSON:
#   tool_name   — one of: web_search | browser_search | search_web |
#                          google_search | news_search
#   inputs      — dict of tool inputs (query, num_results, etc.)
#   model       — the calling model ID (for logging/audit)
#   request_id  — caller's request_id for log correlation
#   user_id     — caller's user_id for log correlation
#
# Response JSON:
#   result      — string result from the search tool
#   tool_name   — echoed back
#   request_id  — echoed back
#   error       — present only on failure (result will be an empty string)
#
# Security:
#   - Requires X-Internal-Token header (same as all other proxy endpoints)
#   - Only executes tool names in the approved _WEB_SEARCH_TOOL_NAMES set
#   - Compliance (PCI/PII) is enforced upstream on app02 before this call
# ──────────────────────────────────────────────────────────────

_WEB_SEARCH_TOOL_NAMES_PROXY = {
    "web_search", "browser_search", "search_web", "google_search", "news_search"
}

# ── Provider-native search helpers ────────────────────────────────────────────
# Each function makes a single model API call with the provider's built-in
# web-search tool enabled. No third-party search API keys required.
# The provider executes the search internally and returns a search-augmented
# response. Token usage (including search content tokens) is captured and
# returned for billing.

def _web_search_via_openai(query: str, model: str) -> tuple[str, int, int]:
    """Execute web search using OpenAI's native web_search_preview tool.

    Uses the Responses API (client.responses.create) which is the only
    OpenAI API that supports the built-in web_search_preview tool.
    Returns (result_text, input_tokens, output_tokens).
    """
    if _openai_gw is None:
        raise RuntimeError("OpenAI gateway not available")

    from core.model_registry import OPENAI_CODING_MODEL
    _model = model or OPENAI_CODING_MODEL

    result = _openai_gw.responses_create(
        model=_model,
        input=query,
        tools=[{"type": "web_search_preview"}],
    )
    return result["output_text"], result["in_tok"], result["out_tok"]


async def _web_search_via_claude(query: str, model: str) -> tuple[str, int, int]:
    """Execute web search using Claude's native web_search tool.

    Passes the built-in web_search tool schema to messages.create().
    Claude autonomously decides when to invoke it and returns the
    search-augmented answer. Returns (result_text, input_tokens, output_tokens).
    """
    if _claude_gw is None:
        raise RuntimeError("Claude gateway not available")

    from core.model_registry import CLAUDE_PRIMARY_MODEL
    _model = model or CLAUDE_PRIMARY_MODEL

    # Claude's built-in web search tool — no custom implementation needed
    web_search_tool = {
        "type": "web_search_20250305",
        "name": "web_search",
    }

    response = await _claude_gw.client.messages.create(
        model=_model,
        max_tokens=4096,
        tools=[web_search_tool],
        messages=[{"role": "user", "content": query}],
    )

    in_tok  = getattr(response.usage, "input_tokens",  0) or 0 if response.usage else 0
    out_tok = getattr(response.usage, "output_tokens", 0) or 0 if response.usage else 0

    text = " ".join(
        b.text for b in response.content if hasattr(b, "text") and b.text
    ).strip()

    return text, in_tok, out_tok


def _web_search_via_gemini(query: str, model: str) -> tuple[str, int, int]:
    """Execute web search using Gemini's native Google Search grounding tool.

    Passes google_search in GenerateContentConfig which enables Gemini's
    built-in grounding with Google Search. Returns (result_text, input_tokens, output_tokens).
    """
    if _gemini_gw is None:
        raise RuntimeError("Gemini gateway not available")

    from google.genai import types as _gtypes
    from core.model_registry import GEMINI_TEXT_MODEL
    _model = model or GEMINI_TEXT_MODEL

    response = _gemini_gw.client.models.generate_content(
        model=_model,
        contents=query,
        config=_gtypes.GenerateContentConfig(
            tools=[_gtypes.Tool(google_search=_gtypes.GoogleSearch())],
        ),
    )

    um = getattr(response, "usage_metadata", None)
    in_tok  = getattr(um, "prompt_token_count",    0) or 0 if um else 0
    out_tok = getattr(um, "candidates_token_count", 0) or 0 if um else 0

    text_parts = []
    if (response.candidates
            and response.candidates[0].content
            and response.candidates[0].content.parts):
        for part in response.candidates[0].content.parts:
            if not getattr(part, "thought", False) and getattr(part, "text", None):
                text_parts.append(part.text)

    return " ".join(text_parts).strip(), in_tok, out_tok


def _resolve_provider_for_model(model: str) -> str:
    """Infer the provider from the model ID string."""
    m = (model or "").lower().strip()
    if "(" in m and ")" in m:
        m = m[m.rfind("(") + 1: m.rfind(")")]
    if m.startswith("claude-"):
        return "anthropic"
    if any(m.startswith(p) for p in ("gpt-", "o1", "o3", "o4")):
        return "openai"
    if m.startswith("gemini-"):
        return "google"
    return "openai"   # safe default — has web_search_preview on all current models


class WebSearchRequest(BaseModel):
    tool_name:  str
    inputs:     dict          = {}
    model:      Optional[str] = None
    request_id: Optional[str] = None
    user_id:    Optional[str] = None


@app.post("/llm/web-search")
async def web_search(req: WebSearchRequest, request: Request):
    """Execute a web-search call using the model's native search tool.

    This is the ONLY endpoint that performs web searches. It runs exclusively
    on the LLM proxy server which has outbound internet access. The gateway
    (no internet egress) calls this endpoint AFTER governance, budget gating,
    and audit checks have already passed.

    The search is executed by calling the requesting model's provider with
    its built-in web-search tool enabled:
      - OpenAI  → web_search_preview (Responses API)
      - Claude  → web_search_20250305 (Messages API)
      - Gemini  → google_search grounding (GenerateContent)

    Response JSON:
      result      — search-augmented answer text
      tool_name   — echoed back
      request_id  — echoed back
      in_tok      — input tokens consumed (including search content tokens)
      out_tok     — output tokens consumed
      provider    — which provider executed the search
      error       — present only on failure
    """
    from core.logger import set_request_id

    req_id = req.request_id or str(uuid.uuid4())
    set_request_id(req_id)
    caller = request.client.host if request.client else "unknown"

    # Allowlist — only approved tool names accepted
    if req.tool_name not in _WEB_SEARCH_TOOL_NAMES_PROXY:
        logger.warning(
            f"[{req_id}] /llm/web-search REJECTED unknown tool_name={req.tool_name!r} from {caller}"
        )
        raise HTTPException(400, f"Unknown web-search tool: {req.tool_name!r}")

    # Extract the query from whichever input key the caller used
    query = (
        req.inputs.get("query")
        or req.inputs.get("q")
        or req.inputs.get("search_query")
        or req.inputs.get("text")
        or ""
    ).strip()

    if not query:
        return {"result": "No search query provided.", "tool_name": req.tool_name,
                "request_id": req_id, "in_tok": 0, "out_tok": 0, "provider": "none"}

    provider = _resolve_provider_for_model(req.model or "")

    logger.info(
        f"[{req_id}] WEB-SEARCH | tool={req.tool_name} provider={provider} "
        f"model={req.model or '-'} user={req.user_id or '-'} "
        f"query={query[:80]!r}"
    )

    loop = asyncio.get_running_loop()
    try:
        if provider == "anthropic":
            # Claude's generate_with_tools is async — run directly in the event loop
            result_text, in_tok, out_tok = await _web_search_via_claude(query, req.model or "")
        elif provider == "google":
            result_text, in_tok, out_tok = await _run_in_pool(
                loop, _web_search_via_gemini, query, req.model or ""
            )
        else:
            # OpenAI — default and fallback
            result_text, in_tok, out_tok = await _run_in_pool(
                loop, _web_search_via_openai, query, req.model or ""
            )

        logger.info(
            f"[{req_id}] WEB-SEARCH DONE | provider={provider} tool={req.tool_name} "
            f"result_len={len(result_text)} in_tok={in_tok} out_tok={out_tok}"
        )
        return {
            "result":     result_text,
            "tool_name":  req.tool_name,
            "request_id": req_id,
            "in_tok":     in_tok,
            "out_tok":    out_tok,
            "provider":   provider,
        }

    except RuntimeError as exc:
        logger.error(f"[{req_id}] WEB-SEARCH UNAVAILABLE | provider={provider} error={exc}")
        raise HTTPException(503, str(exc))

    except Exception as exc:
        logger.error(f"[{req_id}] WEB-SEARCH FAILED | provider={provider} tool={req.tool_name} error={exc}")
        return {
            "result":     "",
            "tool_name":  req.tool_name,
            "request_id": req_id,
            "in_tok":     0,
            "out_tok":    0,
            "provider":   provider,
            "error":      str(exc),
        }


# ── Health check ───────────────────────────────────────────────

@app.get("/health")
async def health():
    stats = _pool_stats()
    # Saturation flag: warn when active threads exceed 80% of max_workers.
    # At 100% the pool queue starts filling; at queue capacity (2× max_workers)
    # new requests receive 503. Surfacing this in /health lets monitoring
    # alert before the queue fills.
    saturated = stats["active"] >= int(stats["max_workers"] * 0.8)
    return {
        "status": "ok",
        "providers": {
            "claude":      _claude_gw is not None,
            "openai":      _openai_gw is not None,
            "gemini":      _gemini_gw is not None,
            "jira":        bool(os.getenv("JIRA_URL")),
            "confluence":  bool(os.getenv("CONFLUENCE_URL")),
        },
        "thread_pool": {
            "active":      stats["active"],
            "pending":     stats["pending"],
            "max_workers": stats["max_workers"],
            "queue_max":   _POOL_QUEUE_MAX,
            "saturated":   saturated,
        },
        "note": "local LLM (in-house GPU) called directly from the gateway — not proxied",
        "https_proxy": os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or None,
    }

