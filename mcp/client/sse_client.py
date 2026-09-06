# SPDX-License-Identifier: MIT
"""
SSEMCPClient — connects to an MCP server over HTTP+SSE transport.

Protocol:
  GET  {base_url}/sse          → Server-Sent Events stream (server → client)
  POST {base_url}/message      → JSON-RPC messages (client → server)

Usage:
    client = SSEMCPClient(
        server_name="datadog",
        base_url="http://your-mcp-server-host:8080",
        headers={"X-DD-API-KEY": "..."},
    )
    await client.start()
    await client.initialize()
    tools = await client.list_tools()
    result = await client.call_tool("query_metrics", {"query": "avg:system.cpu.user{*}"})
    await client.close()
"""

import asyncio
import json
from typing import Dict, Optional

import httpx

from core.logger import logger
from mcp.client.base import MCPClientSession


class SSEMCPClient(MCPClientSession):
    """
    Connects to an SSE-based MCP server.
    Maintains a persistent SSE stream for responses and POSTs messages to /message.
    """

    def __init__(
        self,
        server_name: str,
        base_url: str,
        headers: Dict[str, str] = None,
        timeout: float = 30.0,
    ):
        super().__init__(server_name)
        self._base_url     = base_url.rstrip("/")
        self._headers      = headers or {}
        self._timeout      = timeout
        self._http_client: Optional[httpx.AsyncClient] = None
        self._sse_task: Optional[asyncio.Task] = None
        self._message_url: Optional[str] = None  # populated from 'endpoint' SSE event
        self._session_id: Optional[str] = None

    async def start(self) -> None:
        """Open HTTP client and start SSE listener."""
        self._http_client = httpx.AsyncClient(
            headers=self._headers,
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=True,
        )
        self._sse_task = asyncio.create_task(self._sse_reader())
        # Wait for the endpoint event before proceeding
        for _ in range(50):  # up to 5 seconds
            if self._message_url:
                break
            await asyncio.sleep(0.1)
        if not self._message_url:
            logger.warning(f"SSEMCPClient[{self.server_name}]: no endpoint event received — using default")
            self._message_url = f"{self._base_url}/message"

        logger.info(f"SSEMCPClient[{self.server_name}]: started message_url={self._message_url}")

    async def close(self) -> None:
        if self._sse_task:
            self._sse_task.cancel()
        if self._http_client:
            await self._http_client.aclose()

        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("SSE MCP client closed"))
        self._pending.clear()

        logger.info(f"SSEMCPClient[{self.server_name}]: closed")

    @property
    def is_alive(self) -> bool:
        return self._sse_task is not None and not self._sse_task.done()

    # ── Transport implementation ─────────────────────────────────────────────

    async def _send(self, message: dict) -> None:
        if not self._http_client:
            raise ConnectionError("SSEMCPClient: not started")
        url = self._message_url or f"{self._base_url}/message"
        params = {"sessionId": self._session_id} if self._session_id else {}
        resp = await self._http_client.post(
            url,
            json=message,
            params=params,
        )
        resp.raise_for_status()

    async def _sse_reader(self) -> None:
        """Background task: read SSE stream and dispatch events."""
        url = f"{self._base_url}/sse"
        logger.info(f"SSEMCPClient[{self.server_name}]: connecting SSE → {url}")

        try:
            async with self._http_client.stream("GET", url) as resp:
                resp.raise_for_status()
                event_type = None
                data_lines = []

                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif line.startswith(":"):
                        pass  # keep-alive comment
                    elif line == "":
                        # End of event block
                        if data_lines:
                            raw = "\n".join(data_lines)
                            self._handle_sse_event(event_type, raw)
                        event_type = None
                        data_lines = []

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"SSEMCPClient[{self.server_name}]: SSE reader error → {e}")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError(f"SSE stream error: {e}"))

    def _handle_sse_event(self, event_type: Optional[str], data: str) -> None:
        if event_type == "endpoint":
            # MCP SSE spec: first event gives the message endpoint URL
            endpoint = data.strip()
            if endpoint.startswith("/"):
                self._message_url = f"{self._base_url}{endpoint}"
            else:
                self._message_url = endpoint
            # Extract session ID from URL if present
            if "sessionId=" in self._message_url:
                self._session_id = self._message_url.split("sessionId=")[-1].split("&")[0]
            logger.info(f"SSEMCPClient[{self.server_name}]: endpoint={self._message_url}")
            return

        # Regular JSON-RPC response event
        try:
            message = json.loads(data)
            self._dispatch_response(message)
        except json.JSONDecodeError as e:
            logger.warning(f"SSEMCPClient[{self.server_name}]: bad JSON event: {e}")
