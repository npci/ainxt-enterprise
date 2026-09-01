# SPDX-License-Identifier: Apache-2.0
"""
MCPClientSession — shared JSON-RPC 2.0 session logic for both stdio and SSE transports.

Handles:
  - initialize / initialized handshake
  - tools/list discovery
  - tools/call with timeout and error propagation
  - message ID tracking
"""

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional

from core.logger import logger

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_CLIENT_VERSION   = "1.0.0"
DEFAULT_TIMEOUT      = 30.0  # seconds per tool call


class MCPClientSession:
    """
    Protocol layer for an MCP client session.
    Concrete transports (stdio, SSE) subclass this and implement:
      _send(message: dict)     — write a JSON-RPC message
      _receive() → dict|None   — read next JSON-RPC message (blocking)
    """

    def __init__(self, server_name: str = "unknown"):
        self.server_name  = server_name
        self._id_counter  = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._initialized = False
        self._tools: List[dict] = []
        self._server_info: dict = {}
        self._read_task: Optional[asyncio.Task] = None

    # ── Must be implemented by subclass ─────────────────────────────────────

    async def _send(self, message: dict) -> None:
        raise NotImplementedError

    async def _start_reader(self) -> None:
        """Start background task that reads responses and resolves futures."""
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    # ── Public API ───────────────────────────────────────────────────────────

    async def initialize(self) -> dict:
        """Perform MCP initialize handshake. Must be called before any tool calls."""
        result = await self._rpc("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities":    {"tools": {}},
            "clientInfo": {
                "name":    "ainxt-ainxt-platform",
                "version": MCP_CLIENT_VERSION,
            },
        })
        self._server_info = result.get("serverInfo", {})
        # Send initialized notification (no response expected)
        await self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        self._initialized = True
        logger.info(
            f"MCPClient[{self.server_name}]: initialized "
            f"server={self._server_info.get('name')} "
            f"version={self._server_info.get('version')}"
        )
        return result

    async def list_tools(self) -> List[dict]:
        """Discover tools available on this server."""
        result = await self._rpc("tools/list", {})
        self._tools = result.get("tools", [])
        logger.info(f"MCPClient[{self.server_name}]: discovered {len(self._tools)} tools")
        return self._tools

    async def call_tool(self, name: str, arguments: dict = None, timeout: float = DEFAULT_TIMEOUT) -> str:
        """
        Call a tool on the remote MCP server. Returns the text content of the result.
        Raises MCPToolError on protocol or tool errors.
        """
        if not self._initialized:
            raise RuntimeError("MCPClientSession: call initialize() before call_tool()")

        result = await asyncio.wait_for(
            self._rpc("tools/call", {"name": name, "arguments": arguments or {}}),
            timeout=timeout,
        )

        # Extract text from MCP content array
        if isinstance(result, dict) and "content" in result:
            parts = [
                c.get("text", "")
                for c in result["content"]
                if c.get("type") == "text"
            ]
            text = "\n".join(parts)
            if result.get("isError"):
                raise MCPToolError(f"Tool '{name}' returned error: {text}")
            return text

        return str(result)

    @property
    def tools(self) -> List[dict]:
        return self._tools

    @property
    def server_info(self) -> dict:
        return self._server_info

    # ── Internal JSON-RPC machinery ──────────────────────────────────────────

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def _rpc(self, method: str, params: dict) -> Any:
        """Send a JSON-RPC request and await its response."""
        id_ = self._next_id()
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[id_] = future

        await self._send({
            "jsonrpc": "2.0",
            "id":      id_,
            "method":  method,
            "params":  params,
        })

        try:
            return await future
        finally:
            self._pending.pop(id_, None)

    def _dispatch_response(self, message: dict) -> None:
        """Called by the reader loop when a response arrives."""
        id_ = message.get("id")
        if id_ is None:
            return  # notification — ignore

        future = self._pending.get(id_)
        if future is None or future.done():
            return

        if "error" in message:
            err = message["error"]
            future.set_exception(MCPProtocolError(
                f"[{err.get('code')}] {err.get('message')}"
            ))
        elif "result" in message:
            future.set_result(message["result"])
        else:
            future.set_exception(MCPProtocolError("Response has neither result nor error"))


class MCPToolError(Exception):
    """Raised when a remote MCP tool returns isError=True."""

class MCPProtocolError(Exception):
    """Raised on JSON-RPC protocol violations."""
