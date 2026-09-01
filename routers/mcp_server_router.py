# SPDX-License-Identifier: Apache-2.0
"""
MCP Server Router — exposes internal AiNxt MCP servers over HTTP (SSE transport).

Endpoints:
  GET  /mcp/servers                    → list all internal + external servers
  GET  /mcp/{server}/sse               → SSE stream for an internal server
  POST /mcp/{server}/message           → JSON-RPC message to an internal server
  GET  /mcp/{server}/tools             → list tools for a server (REST shortcut)
  POST /mcp/external/register          → register a new external MCP server (admin)
  DELETE /mcp/external/{name}          → disconnect and remove external server (admin)
  GET  /mcp/external/servers           → list external server connection status

Authentication:
  - Internal SSE/message endpoints: JWT required
  - External register/delete: admin role required
  - Tools list: JWT required
"""

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Dict, List, Optional

from auth.dependencies import get_current_user
from auth.rbac import require_role
from core.logger import logger
from core.security_validation import validate_external_server_request

router = APIRouter(prefix="/mcp", tags=["mcp"])


# ── Internal server helpers ──────────────────────────────────────────────────

def _get_internal_server(name: str):
    from mcp.bridge import mcp_bridge
    server = mcp_bridge._internal_servers.get(name)
    if server is None:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found. "
                            f"Available: {list(mcp_bridge._internal_servers.keys())}")
    return server


# ── Discovery ────────────────────────────────────────────────────────────────

@router.get("/servers")
def list_servers(current_user: dict = Depends(get_current_user)):
    """List all MCP servers (internal + external) with their tools."""
    from mcp.bridge import mcp_bridge
    from mcp.external_registry import external_mcp_registry

    internal = []
    for slug, server in mcp_bridge._internal_servers.items():
        internal.append({
            "name":      slug,
            "type":      "internal",
            "transport": ["stdio", "sse"],
            "sse_url":   f"/mcp/{slug}/sse",
            "tools":     [
                {"name": t.name, "description": t.description}
                for t in server._tools.values()
            ],
        })

    external = external_mcp_registry.list_connected_servers()
    for srv in external:
        srv["type"] = "external"

    return {"internal": internal, "external": external}


# ── SSE Transport ────────────────────────────────────────────────────────────

@router.get("/{server_name}/sse")
async def sse_stream(
    server_name: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """
    Open an SSE connection to an internal MCP server.
    The first event is 'endpoint' with the POST URL for sending messages.
    Keep-alive pings sent every 15 seconds.
    """
    server  = _get_internal_server(server_name)
    session_id = str(uuid.uuid4())

    async def event_generator():
        async for chunk in server.sse_stream(session_id):
            if await request.is_disconnected():
                break
            yield chunk

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.post("/{server_name}/sse")
async def streamable_http(
    server_name: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """
    Streamable HTTP transport (MCP spec 2024-11-05) — used by CLI v0.2.101+.

    The CLI sends POST /{server}/sse with a JSON-RPC body and expects the
    response inline (initialize / tools/list / tools/call all work in a single
    request/response cycle — no persistent SSE stream required).

    The MCP Streamable HTTP spec requires the server to assign a session ID on
    `initialize` and echo it back via the `Mcp-Session-Id` response header on
    EVERY subsequent response. The CLI's StreamableHttpClientWorker (rmcp)
    validates this header — without it the handshake fails immediately with
    "Send message error Transport".

    The legacy GET /{server}/sse + POST /{server}/message transport is unchanged
    and continues to work for older clients. Both transports are active
    simultaneously — FastAPI routes them by HTTP method so they never conflict.
    """
    server = _get_internal_server(server_name)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    user_id: Optional[str] = (
        current_user.get("sub")
        or current_user.get("id")
        or current_user.get("user_id")
    )
    # MCP Streamable HTTP spec (2024-11-05): echo Mcp-Session-Id on every response.
    # On `initialize` the client sends no session id — we generate one and the CLI
    # stores it, then sends it back on every subsequent request so we can correlate.
    # On all other calls the client echoes the id we assigned; we reflect it back.
    mcp_session_id = (
        request.headers.get("mcp-session-id")
        or request.headers.get("Mcp-Session-Id")
        or f"{server_name}-{uuid.uuid4()}"
    )

    response, mcp_session_id = await server.handle_streamable_http(
        body, session_id=mcp_session_id, user_id=user_id
    )

    from fastapi.responses import JSONResponse
    return JSONResponse(
        content=response or {},
        headers={"Mcp-Session-Id": mcp_session_id},
    )


@router.post("/{server_name}/message")
async def post_message(
    server_name: str,
    request: Request,
    session_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """
    Send a JSON-RPC message to an internal MCP server.
    For SSE transport: response is pushed onto the SSE stream.
    Returns 202 Accepted.

    user_id is forwarded to handle_message() so servers that need per-user
    credentials (e.g. GitLabMCPServer) can inject the correct token before
    dispatching tool calls.
    """
    server = _get_internal_server(server_name)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Resolve user_id — prefer sub claim, fall back to id/user_id fields
    user_id: Optional[str] = (
        current_user.get("sub")
        or current_user.get("id")
        or current_user.get("user_id")
    )

    if session_id:
        # Async SSE path — push response to SSE queue
        await server.handle_sse_message(body, session_id)
        return {"accepted": True}
    else:
        # Sync path — return response directly (useful for testing).
        # Pass user_id so per-user credential injection works (GitLab, etc.).
        response = await server.handle_message(body, user_id=user_id)
        return response or {}


# ── Tools shortcut ───────────────────────────────────────────────────────────

@router.get("/{server_name}/tools")
async def list_server_tools(
    server_name: str,
    current_user: dict = Depends(get_current_user),
):
    """List all tools available on an internal MCP server (REST shortcut, no SSE needed)."""
    server = _get_internal_server(server_name)
    response = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        session_id=None,
    )
    return response.get("result", {}).get("tools", []) if response else []


# ── External Server Management ───────────────────────────────────────────────

class ExternalServerRegisterRequest(BaseModel):
    name:        str
    transport:   str                      # "stdio" or "sse"
    command:     Optional[str] = ""       # stdio only
    args:        Optional[List[str]] = [] # stdio only
    env_vars:    Optional[Dict[str, str]] = {}
    sse_url:     Optional[str] = ""       # sse only
    sse_headers: Optional[Dict[str, str]] = {}
    timeout:     Optional[float] = 30.0


@router.post("/external/register")
def register_external_server(
    body: ExternalServerRegisterRequest,
    current_user: dict = Depends(require_role("admin")),
):
    """
    Register and connect a new external MCP server.
    Saves to DB, connects immediately, discovers tools.
    Admin only.
    """
    # Validate and sanitize inputs
    is_valid, field_errors, sanitized = validate_external_server_request(body)
    if not is_valid:
        error_messages = []
        for field, errors in field_errors.items():
            for e in errors:
                error_messages.append(f"{field}: {e}")
        raise HTTPException(status_code=400, detail="; ".join(error_messages))

    from mcp.external_registry import external_mcp_registry, ExternalServerConfig

    config = ExternalServerConfig(
        name=sanitized["name"],
        transport=sanitized["transport"],
        command=sanitized["command"] or "",
        args=body.args or [],
        env_vars=body.env_vars or {},
        sse_url=sanitized.get("sse_url", "") or "",
        sse_headers=body.sse_headers or {},
        timeout=body.timeout or 30.0,
    )

    success = external_mcp_registry.register_server(config)
    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to connect to MCP server '{sanitized['name']}'")

    return {
        "connected": True,
        "name":      sanitized["name"],
        "transport": sanitized["transport"],
        "message":   f"External MCP server '{sanitized['name']}' connected and tools registered.",
    }


@router.delete("/external/{name}")
def remove_external_server(
    name: str,
    current_user: dict = Depends(require_role("admin")),
):
    """Disconnect and remove an external MCP server. Admin only."""
    from mcp.external_registry import external_mcp_registry
    from db.database import SessionLocal
    from sqlalchemy import text

    # Disconnect client
    client = external_mcp_registry._clients.pop(name, None)
    if client:
        try:
            loop = external_mcp_registry._loop
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(client.close(), loop)
        except Exception as e:
            logger.warning(f"MCP remove: close error for {name} → {e}")

    # Remove from DB
    try:
        with SessionLocal() as db:
            db.execute(text("UPDATE mcp_external_servers SET enabled=false WHERE name=:name"), {"name": name})
            db.commit()
    except Exception as e:
        logger.warning(f"MCP remove: DB update failed → {e}")

    return {"removed": True, "name": name}


@router.get("/external/servers")
def list_external_servers(current_user: dict = Depends(get_current_user)):
    """List all external MCP server connections and their status."""
    from mcp.external_registry import external_mcp_registry
    return external_mcp_registry.list_connected_servers()
