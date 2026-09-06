# SPDX-License-Identifier: MIT
"""
Cowork connector MCP server (SSE transport, per-user).

Exposes the gateway's connectors + Knowledge Base as MCP tools to the desktop
Cowork local agent. The agent connects with an `sse`-type MCP server pointed at
`/buddy/mcp/sse` carrying the user's JWT — every call is scoped to that user.

Endpoints (mounted under /ainxt/v1/api):
  GET  /buddy/mcp/sse                     → SSE stream; first event is `endpoint`
  POST /buddy/mcp/message?sessionId=...   → JSON-RPC message; response pushed to SSE
  POST /buddy/mcp/message                 → JSON-RPC message; response returned inline (no SSE)
  GET  /buddy/mcp/tools                   → REST shortcut: list this user's tools

SCALING (2k parallel users — feedback_scale_2k_users):
  Session routing state lives in REDIS, not in-process dicts. The gateway runs
  `uvicorn --workers 4`: the SSE GET can land on worker A while the matching
  POST /message lands on worker B. With in-process queues the reply would be
  lost. Here:
    - the SSE stream BLPOPs a per-session Redis list (async client, so a stream
      never blocks the event loop / a threadpool thread),
    - POST /message RPUSHes the JSON-RPC reply to that list from ANY worker,
    - the per-session connector allowlist + owning user are Redis keys (TTL'd).
  A single-worker in-process fallback is kept for dev (when Redis is down).

Dispatch + per-user tool logic lives in connectors/mcp_bridge.py.
"""
import asyncio
import json
import uuid
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from auth.dependencies import get_current_user
from core.config import REDIS_HOST, REDIS_PORT
from core.logger import logger
from connectors import mcp_bridge

try:
    from core.config import REDIS_PASSWORD as _REDIS_PASSWORD
except Exception:
    _REDIS_PASSWORD = None

router = APIRouter(prefix="/buddy", tags=["buddy"])

# Redis (db=5, same as the queues) routing keys.
_Q_KEY = "cowork:mcp:q:{sid}"           # list — JSON-RPC replies for the SSE stream
_A_KEY = "cowork:mcp:allowed:{sid}"     # JSON allowlist (or "null")
_U_KEY = "cowork:mcp:user:{sid}"        # owning user_id (so a guessed sid can't cross users)
_SESSION_TTL = 3600                     # refreshed on every message

# In-process fallback ONLY when Redis is unavailable (dev / single worker).
_QUEUES: Dict[str, asyncio.Queue] = {}
_ALLOWED: Dict[str, Optional[frozenset]] = {}

_async_redis = None
_redis_unavailable = False


def _get_async_redis():
    """Lazily build a process-wide redis.asyncio client (pooled). Returns None
    if redis.asyncio isn't importable / can't connect, so callers fall back to
    the in-process path."""
    global _async_redis, _redis_unavailable
    if _async_redis is not None or _redis_unavailable:
        return _async_redis
    try:
        import redis.asyncio as aioredis
        _async_redis = aioredis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=5,
            password=_REDIS_PASSWORD or None,
            decode_responses=True, socket_connect_timeout=2,
            health_check_interval=30,
        )
    except Exception as e:
        logger.warning(f"cowork_mcp: redis.asyncio unavailable ({e}) — SSE routing falls back to in-process (single-worker only)")
        _redis_unavailable = True
        _async_redis = None
    return _async_redis


def _parse_allowed(header: Optional[str]):
    """Comma-separated 'x-cowork-allowed-tools' → a set, or None if absent/empty."""
    if not header:
        return None
    items = {p.strip() for p in header.split(",") if p.strip()}
    return frozenset(items) or None


def _allowed_to_json(allowed) -> str:
    return json.dumps(sorted(allowed)) if allowed else "null"


def _allowed_from_json(raw) -> Optional[frozenset]:
    if not raw or raw == "null":
        return None
    try:
        items = json.loads(raw)
        return frozenset(items) or None
    except Exception:
        return None


@router.get("/mcp/sse")
async def cowork_mcp_sse(request: Request, current_user: dict = Depends(get_current_user)):
    """Open the SSE stream. Emits a (relative) `endpoint` event, then pushes
    JSON-RPC responses and 15s keep-alive pings — replies arrive via Redis so any
    worker's POST reaches this stream."""
    session_id = str(uuid.uuid4())
    user_id = current_user["sub"]
    allowed = _parse_allowed(request.headers.get("x-cowork-allowed-tools"))
    r = _get_async_redis()

    if r is not None:
        try:
            await r.set(_A_KEY.format(sid=session_id), _allowed_to_json(allowed), ex=_SESSION_TTL)
            await r.set(_U_KEY.format(sid=session_id), str(user_id), ex=_SESSION_TTL)
        except Exception as e:
            logger.warning(f"cowork_mcp: redis set failed ({e}); using in-process fallback")
            r = None

    if r is None:
        _QUEUES[session_id] = asyncio.Queue()
        _ALLOWED[session_id] = allowed

    async def gen():
        yield f"event: endpoint\ndata: message?sessionId={session_id}\n\n"
        qkey = _Q_KEY.format(sid=session_id)
        try:
            while True:
                if await request.is_disconnected():
                    break
                if r is not None:
                    # Async BLPOP — does not block the event loop or a thread.
                    popped = await r.blpop(qkey, timeout=15)
                    if popped:
                        yield f"data: {popped[1]}\n\n"
                    else:
                        # keep the session keys warm while the stream is open
                        try:
                            await r.expire(_A_KEY.format(sid=session_id), _SESSION_TTL)
                            await r.expire(_U_KEY.format(sid=session_id), _SESSION_TTL)
                        except Exception:
                            pass
                        yield ": ping\n\n"
                else:
                    try:
                        msg = await asyncio.wait_for(_QUEUES[session_id].get(), timeout=15.0)
                        yield f"data: {json.dumps(msg)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if r is not None:
                try:
                    await r.delete(_Q_KEY.format(sid=session_id),
                                   _A_KEY.format(sid=session_id),
                                   _U_KEY.format(sid=session_id))
                except Exception:
                    pass
            else:
                _QUEUES.pop(session_id, None)
                _ALLOWED.pop(session_id, None)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/mcp/message")
async def cowork_mcp_message(
    request: Request,
    sessionId: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """Handle a JSON-RPC message. With sessionId → route the reply to the SSE
    stream (via Redis, so any worker reaches it); without → return inline."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    user_id = current_user["sub"]
    r = _get_async_redis()

    # Role/plugin scope: prefer this request's header; else the value captured
    # when the SSE stream opened.
    allowed = _parse_allowed(request.headers.get("x-cowork-allowed-tools"))
    if allowed is None and sessionId:
        if r is not None:
            try:
                allowed = _allowed_from_json(await r.get(_A_KEY.format(sid=sessionId)))
            except Exception:
                allowed = None
        else:
            allowed = _ALLOWED.get(sessionId)

    response = await mcp_bridge.handle(body, user_id, allowed)

    if sessionId:
        if r is not None:
            try:
                # Only route to a stream OWNED by the same user (sid is a uuid4,
                # but verify so a guessed id can never push into another's stream).
                owner = await r.get(_U_KEY.format(sid=sessionId))
                if owner and owner != str(user_id):
                    raise HTTPException(status_code=403, detail="Session does not belong to this user")
                if response is not None:
                    qkey = _Q_KEY.format(sid=sessionId)
                    await r.rpush(qkey, json.dumps(response))
                    await r.expire(qkey, _SESSION_TTL)
            except HTTPException:
                raise
            except Exception as e:
                logger.warning(f"cowork_mcp: redis rpush failed ({e})")
        else:
            queue = _QUEUES.get(sessionId)
            if queue is not None and response is not None:
                await queue.put(response)
        return {"accepted": True}
    return response or {}


@router.get("/mcp/tools")
async def cowork_mcp_tools(request: Request, current_user: dict = Depends(get_current_user)):
    """REST shortcut — list the tools available to this user (no SSE needed)."""
    allowed = _parse_allowed(request.headers.get("x-cowork-allowed-tools"))
    return mcp_bridge.list_tools(current_user["sub"], allowed)


@router.post("/mcp/sse")
async def cowork_mcp_sse_post(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Streamable HTTP transport (MCP 2024-11-05) — used by CLI v0.2.101+.

    The new CLI sends POST /mcp/sse with a JSON-RPC body and expects the
    response inline (no SSE stream required for initialize / tools/list /
    tools/call). This endpoint handles the full MCP lifecycle in a single
    request/response cycle.

    The old SSE transport (GET /mcp/sse + POST /mcp/message) is unchanged
    and continues to work for older clients (CLI v3.0.0-beta, web app).
    Both transports are active simultaneously — FastAPI routes them by
    HTTP method so they never conflict.

    IMPORTANT: The MCP Streamable HTTP spec (2024-11-05) requires the server
    to assign a session ID on `initialize` and echo it back via the
    `Mcp-Session-Id` response header on EVERY subsequent response.
    The CLI's StreamableHttpClientWorker (rmcp) validates this header —
    without it the handshake fails immediately with "Send message error
    Transport", which is why connector tools never appear in the session.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    user_id = current_user["sub"]
    allowed = _parse_allowed(request.headers.get("x-cowork-allowed-tools"))

    # MCP Streamable HTTP spec (2024-11-05): echo Mcp-Session-Id on every response.
    # On `initialize` the client sends no session id — we generate one and the CLI
    # stores it, then sends it back on every subsequent request so we can correlate.
    # On all other calls the client echoes the id we assigned; we reflect it back.
    mcp_session_id = (
        request.headers.get("mcp-session-id")
        or request.headers.get("Mcp-Session-Id")
        or f"cowork-{uuid.uuid4()}"
    )

    response = await mcp_bridge.handle(body, user_id, allowed)

    from fastapi.responses import JSONResponse
    return JSONResponse(
        content=response or {},
        headers={"Mcp-Session-Id": mcp_session_id},
    )
