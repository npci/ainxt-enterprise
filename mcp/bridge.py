# SPDX-License-Identifier: Apache-2.0
"""
MCPBridge — unified tool call router.

Routes an incoming tool call to:
  1. Internal MCP server (Jira, Confluence, GitLab, Database, Platform)
     — executed in-process via BaseMCPServer.handle_message()
  2. External MCP client (GitHub, Slack, Datadog, etc.)
     — routed through ExternalMCPRegistry.call_tool()
  3. Legacy ToolRegistry (existing non-MCP tools)
     — fallback for backward compatibility

Tool name format:
  "{server_name}__{tool_name}"   → internal or external MCP server
  "{tool_name}"                  → legacy ToolRegistry

Usage:
    result = MCPBridge.call(tool_name="jira__jira_create_issue", arguments={...})
    result = MCPBridge.call(tool_name="github__search_repositories", arguments={...})
    result = MCPBridge.call(tool_name="retrieve", arguments={"query": "..."})  # legacy
"""

import asyncio
import re
from typing import Any, Optional

from core.logger import logger

# SEC-F-025: server slugs come from LLM tool-call output and from the
# mcp_external_servers DB table — both are untrusted. An invalid slug could
# be used to route to unintended servers or inject into downstream calls.
_SLUG_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


class MCPBridge:
    """
    Singleton router between all tool registries.
    call() is synchronous (blocks on async if needed).
    """

    _instance: Optional["MCPBridge"] = None

    def __init__(self):
        self._internal_servers: dict = {}   # slug → BaseMCPServer instance
        self._bootstrapped = False

    @classmethod
    def get(cls) -> "MCPBridge":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def bootstrap(self) -> None:
        """Instantiate all internal MCP servers. Called once at startup."""
        if self._bootstrapped:
            return
        try:
            from mcp.servers import INTERNAL_SERVERS
            for slug, cls_ in INTERNAL_SERVERS.items():
                try:
                    self._internal_servers[slug] = cls_()
                    logger.info(f"MCPBridge: internal server '{slug}' ready")
                except Exception as e:
                    logger.error(f"MCPBridge: failed to init '{slug}' → {e}")
            self._bootstrapped = True
            logger.info(f"MCPBridge: bootstrapped {len(self._internal_servers)} internal servers")
        except Exception as e:
            logger.error(f"MCPBridge: bootstrap failed → {e}")

    def call(self, tool_name: str, arguments: dict = None) -> str:
        """
        Route a tool call. Returns the string result.
        Always returns a string — never raises (errors returned as strings).
        """
        arguments = arguments or {}

        # ── Internal MCP server (format: "jira__jira_create_issue") ─────────
        if "__" in tool_name:
            server_slug, actual_tool = tool_name.split("__", 1)

            if not _SLUG_RE.match(server_slug):
                return f"error: invalid server slug {server_slug!r}"

            internal = self._internal_servers.get(server_slug)
            if internal:
                return self._call_internal(internal, actual_tool, arguments)

            # External MCP server
            from mcp.external_registry import external_mcp_registry
            return external_mcp_registry.call_tool(server_slug, actual_tool, arguments)

        # ── Legacy ToolRegistry (no double underscore) ────────────────────
        try:
            from mcp.registry import mcp_registry
            result = mcp_registry.execute_tool(tool_name, **arguments)
            if result.success:
                return str(result.output) if result.output is not None else ""
            return f"Tool error: {result.error}"
        except Exception as e:
            return f"Bridge error: {e}"

    def _call_internal(self, server, tool_name: str, arguments: dict) -> str:
        """Call an internal MCP server tool synchronously.

        ARCH-F-001 (2026-08-26): hardened, not deferred. The EA finding was a
        sync/async deadlock risk in the run_coroutine_threadsafe() branch below.
        A worker/Redis-queue deferral would NOT fix this — the risk is an
        in-process threading hazard (same-thread self-block), not a workload
        that benefits from being distributed to a queue. Queuing this call
        would just relocate the same hazard into a worker process.

        The actual hazard: run_coroutine_threadsafe() schedules a coroutine on
        `loop` and expects the CALLER to be on a *different* thread. If a
        future caller invokes call() synchronously from code that is itself
        running ON that same event-loop thread (e.g. a sync helper called
        directly from an async route without an `asyncio.to_thread` /
        `run_in_threadpool` offload), `future.result(timeout=30.0)` blocks
        that thread — but that thread IS the only thread that can drive the
        loop forward and complete the scheduled coroutine. The result is not
        a quick error, it is a 30s hang before the timeout finally fires.
        Today's real callers avoid this because they call server.handle_message()
        directly with `await` from async routes (see routers/mcp_server_router.py,
        routers/cowork_mcp_router.py) rather than through this sync wrapper —
        this method has no confirmed production callers as of this fix.

        Fix: detect the self-block condition explicitly and fail immediately
        with a clear error instead of silently hanging for 30s. A caller that
        is not on the loop's thread still gets the original working behaviour.
        """
        message = {
            "jsonrpc": "2.0",
            "id":      1,
            "method":  "tools/call",
            "params":  {"name": tool_name, "arguments": arguments},
        }
        try:
            try:
                loop = asyncio.get_running_loop()
                running_in_this_thread = True
            except RuntimeError:
                loop = None
                running_in_this_thread = False

            if running_in_this_thread:
                # We ARE the event-loop thread calling a "synchronous" method —
                # run_coroutine_threadsafe() would deadlock (see docstring).
                # Fail fast instead of hanging for 30s.
                return (
                    f"Internal MCP error ({server.server_name}/{tool_name}): "
                    "MCPBridge.call() was invoked synchronously from the event-loop "
                    "thread itself — this would deadlock. Call "
                    "`await server.handle_message(...)` directly from async code, "
                    "or run this call via asyncio.to_thread()/run_in_threadpool() "
                    "from a genuinely separate thread."
                )

            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            if loop.is_running():
                # Loop is running on a DIFFERENT thread than this one (verified
                # above) — run_coroutine_threadsafe() is safe here.
                future = asyncio.run_coroutine_threadsafe(
                    server.handle_message(message, session_id=None), loop
                )
                response = future.result(timeout=30.0)
            else:
                response = loop.run_until_complete(server.handle_message(message))
        except Exception as e:
            return f"Internal MCP error ({server.server_name}/{tool_name}): {e}"

        if response is None:
            return ""

        if "error" in response:
            err = response["error"]
            return f"MCP error [{err.get('code')}]: {err.get('message')}"

        result = response.get("result", {})
        if isinstance(result, dict) and "content" in result:
            parts = [c.get("text", "") for c in result["content"] if c.get("type") == "text"]
            return "\n".join(parts)

        return str(result)

    def get_all_tools(self) -> list:
        """Return all tool definitions across internal + external + legacy."""
        all_tools = []

        # Internal MCP tools
        for slug, server in self._internal_servers.items():
            for tool in server._tools.values():
                all_tools.append({
                    "name":        f"{slug}__{tool.name}",
                    "description": tool.description,
                    "source":      "internal_mcp",
                    "server":      slug,
                })

        # External MCP tools (already registered in ToolRegistry with __ prefix)
        # Legacy tools (from ToolRegistry)
        try:
            from mcp.registry import mcp_registry
            for name, tool in mcp_registry.tools._tools.items():
                if "__" not in name:
                    all_tools.append({
                        "name":        name,
                        "description": tool.description,
                        "source":      "legacy",
                        "server":      "platform",
                    })
        except Exception:
            pass

        return all_tools


# Module-level singleton
mcp_bridge = MCPBridge.get()
