# SPDX-License-Identifier: Apache-2.0
"""cli_runtime.mcp_router — the HTTP endpoint the spawned CLI calls back into.

One route:

    POST /abstudio-mcp/{run_id}      Authorization: Bearer <run token>

This is *streamable HTTP* transport: the CLI posts a JSON-RPC request and reads
the response inline from the same request/response cycle — no SSE stream needed
for ``initialize`` / ``tools/list`` / ``tools/call``. Verified working against
``ainxt 0.2.101``, and the same shape ``routers/cowork_mcp_router.py`` already
serves for the desktop client.

Authentication is deliberately NOT the platform JWT dependency used by every
other ABStudio route. The caller is a subprocess we spawned ourselves, and the
per-run bearer token is a strictly narrower credential: it is minted per run,
scoped to one agent's tools and skills, revoked when the process exits, and only
ever travels over loopback. Requiring a user JWT here would mean handing a real
user credential to a subprocess — strictly worse.

Because ``run_id`` is used to look up a session in an in-process registry (never
to build a filesystem path), path traversal is not reachable through it.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Header, Path, Request
from fastapi.responses import JSONResponse, Response

from core.logger import logger

from .config import cli_runtime_config
from .mcp_server import AbstudioMcpServer, ERR_PARSE
from .session import get_registry

router = APIRouter(tags=["cli-runtime"])


def _bearer(header_value: Optional[str]) -> str:
    """Extract a bearer token, tolerating case and missing scheme."""
    raw = (header_value or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw


@router.post("/abstudio-mcp/{run_id}")
async def abstudio_mcp(
    request: Request,
    run_id: str = Path(..., description="Per-run MCP session id"),
    authorization: Optional[str] = Header(default=None),
) -> Response:
    """Serve one JSON-RPC message for a live CLI run."""
    try:
        body: Any = await request.json()
    except Exception:
        return JSONResponse(
            status_code=200,
            content={
                "jsonrpc": "2.0", "id": None,
                "error": {"code": ERR_PARSE, "message": "invalid JSON body"},
            },
        )

    session, reason = get_registry().authenticate(run_id, _bearer(authorization))
    if session is None:
        # 401 with a coarse reason: a probe must not be able to tell "no such
        # run" from "wrong token" and enumerate live runs.
        logger.warning("[CLI-MCP] auth rejected", run_id=run_id, reason=reason)
        return JSONResponse(
            status_code=401,
            content={
                "jsonrpc": "2.0",
                "id": body.get("id") if isinstance(body, dict) else None,
                "error": {"code": ERR_PARSE, "message": reason},
            },
        )

    server = AbstudioMcpServer(session=session, config=cli_runtime_config())
    response = await server.handle(body)

    if response is None:
        # A notification (e.g. ``notifications/initialized``) has no reply.
        return Response(status_code=202)
    return JSONResponse(status_code=200, content=response)


__all__ = ["router"]
