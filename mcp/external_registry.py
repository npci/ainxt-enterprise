# SPDX-License-Identifier: MIT
"""
ExternalMCPRegistry — manages connections to external MCP servers.

Lifecycle:
  1. Load server configs from `mcp_external_servers` Postgres table
  2. For each enabled server, create a StdioMCPClient or SSEMCPClient
  3. Call initialize() + list_tools() on each
  4. Register discovered tools into the platform's ToolRegistry
     with prefix: {server_name}__{tool_name} (double underscore)
  5. When a prefixed tool is executed, route through the right MCP client

Tool naming:
  External tool "search_repositories" from server "github"
  → registered as "github__search_repositories" in ToolRegistry
  → description prefixed: "[GitHub MCP] ..."

Adding a new server at runtime (admin API):
  ExternalMCPRegistry.register_server(config) → connects + discovers tools immediately
"""

import asyncio
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.logger import logger

# SEC-F-003: external MCP server configs are loaded from a database row and
# used to spawn arbitrary subprocesses. An attacker with DB write access could
# register a malicious `command` (e.g. bash, curl-pipe-sh) and gain code
# execution inside the platform's process space. Only well-known MCP launcher
# binaries may be spawned — the basename of `config.command` must appear in
# this allowlist.
_ALLOWED_STDIO_COMMANDS = frozenset({
    "npx", "node", "python", "python3", "uvx", "uv", "deno",
})


@dataclass
class ExternalServerConfig:
    name:       str
    transport:  str              # "stdio" or "sse"
    command:    str  = ""        # stdio only
    args:       List[str] = field(default_factory=list)
    env_vars:   Dict[str, str] = field(default_factory=dict)
    sse_url:    str  = ""        # sse only
    sse_headers: Dict[str, str] = field(default_factory=dict)
    enabled:    bool = True
    timeout:    float = 30.0


class ExternalMCPRegistry:
    """
    Singleton that manages external MCP server connections.
    Thread-safe — connect_all() runs in its own event loop thread.
    """

    _instance: Optional["ExternalMCPRegistry"] = None

    def __init__(self):
        self._clients: Dict[str, object] = {}      # server_name → MCPClient
        self._configs: Dict[str, ExternalServerConfig] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    @classmethod
    def get(cls) -> "ExternalMCPRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Public API (called from sync context) ────────────────────────────────

    def connect_all(self) -> None:
        """
        Load all enabled servers from DB and connect.
        Runs in a background thread with its own event loop.
        Non-blocking — platform starts even if external servers are slow.
        """
        configs = self._load_from_db()
        if not configs:
            logger.info("ExternalMCPRegistry: no external servers configured")
            self._ready.set()
            return

        self._thread = threading.Thread(
            target=self._run_loop, args=(configs,), daemon=True, name="mcp-external"
        )
        self._thread.start()
        logger.info(f"ExternalMCPRegistry: connecting to {len(configs)} external servers")

    def register_server(self, config: ExternalServerConfig) -> bool:
        """Register and connect a server at runtime (admin API). Blocking up to 30s."""
        self._save_to_db(config)
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._connect_one(config), self._loop
            )
            try:
                future.result(timeout=35.0)
                return True
            except Exception as e:
                logger.error(f"ExternalMCPRegistry: failed to connect {config.name} → {e}")
                return False
        return False

    def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> str:
        """
        Call a tool on a connected external server. Blocking (routes to async loop).
        Returns tool result string or error message.

        ARCH-F-007 / ARCH-F-008 (2026-08-26): wrapped in a per-server circuit
        breaker (core/circuit_breaker.get_breaker), matching the fix already
        applied to connectors/engine.py._execute_with_retry(). External MCP
        servers are arbitrary third-party processes/endpoints (GitHub, Slack,
        Datadog, etc.) — if one is down or hanging, every call to it
        previously paid the full 35s future.result() timeout before failing.
        Fast-failing once a server is known to be unhealthy avoids piling up
        35s waits during an outage on any ONE external server, without
        affecting other, healthy external servers (breaker is keyed per
        server_name, so one server's outage never fast-fails another).
        """
        client = self._clients.get(server_name)
        if client is None:
            return f"[ExternalMCP] Server '{server_name}' not connected"

        if self._loop is None or not self._loop.is_running():
            return "[ExternalMCP] Event loop not running"

        from core.circuit_breaker import get_breaker
        breaker = get_breaker(f"external_mcp_{server_name}")
        if breaker.is_open:
            return (
                f"[ExternalMCP] Server '{server_name}' circuit breaker is OPEN — "
                f"too many recent failures; fast-failing {tool_name} instead of "
                f"waiting on a likely-unresponsive server. It will automatically "
                f"retry after the recovery window."
            )

        future = asyncio.run_coroutine_threadsafe(
            client.call_tool(tool_name, arguments, timeout=client._timeout),
            self._loop,
        )
        try:
            result = future.result(timeout=35.0)
            breaker.record_success()
            return result
        except Exception as e:
            breaker.record_failure(e)
            return f"[ExternalMCP] Tool error from {server_name}/{tool_name}: {e}"

    def list_connected_servers(self) -> List[dict]:
        return [
            {
                "name":      name,
                "transport": cfg.transport,
                "tools":     [t["name"] for t in self._clients[name].tools] if name in self._clients else [],
                "connected": name in self._clients,
            }
            for name, cfg in self._configs.items()
        ]

    # ── Async internals ──────────────────────────────────────────────────────

    def _run_loop(self, configs: List[ExternalServerConfig]) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_all(configs))
        finally:
            self._loop.close()

    async def _connect_all(self, configs: List[ExternalServerConfig]) -> None:
        tasks = [self._connect_one(cfg) for cfg in configs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for cfg, result in zip(configs, results):
            if isinstance(result, Exception):
                logger.error(f"ExternalMCPRegistry: {cfg.name} failed → {result}")
        self._ready.set()
        # Keep loop alive for future call_tool() calls
        await asyncio.Event().wait()

    async def _connect_one(self, config: ExternalServerConfig) -> None:
        from mcp.client.stdio_client import StdioMCPClient
        from mcp.client.sse_client import SSEMCPClient
        from mcp.tool_registry import ToolDefinition

        logger.info(f"ExternalMCPRegistry: connecting {config.name} ({config.transport})")
        self._configs[config.name] = config

        try:
            if config.transport == "stdio":
                cmd_basename = os.path.basename(config.command)
                if cmd_basename not in _ALLOWED_STDIO_COMMANDS:
                    raise ValueError(
                        f"ExternalMCPRegistry: command '{cmd_basename}' is not in the stdio "
                        f"allowlist ({sorted(_ALLOWED_STDIO_COMMANDS)}). Update "
                        f"_ALLOWED_STDIO_COMMANDS deliberately."
                    )
                client = StdioMCPClient(
                    server_name=config.name,
                    command=config.command,
                    args=config.args,
                    env=config.env_vars,
                )
                await client.start()
            elif config.transport == "sse":
                client = SSEMCPClient(
                    server_name=config.name,
                    base_url=config.sse_url,
                    headers=config.sse_headers,
                    timeout=config.timeout,
                )
                await client.start()
            else:
                raise ValueError(f"Unknown transport: {config.transport}")

            await client.initialize()
            tools = await client.list_tools()
            self._clients[config.name] = client

            # Register tools into platform ToolRegistry
            self._register_discovered_tools(config.name, tools)
            self._update_db_status(config.name, "connected")
            logger.info(f"ExternalMCPRegistry: {config.name} connected — {len(tools)} tools registered")

        except Exception as e:
            logger.error(f"ExternalMCPRegistry: {config.name} connection failed → {e}")
            self._update_db_status(config.name, "error")
            raise

    def _register_discovered_tools(self, server_name: str, tools: List[dict]) -> None:
        """Register each discovered tool into the platform ToolRegistry with server prefix."""
        try:
            from mcp.tool_registry import ToolRegistry
            from mcp.tool_registry import ToolDefinition

            # We need the registry instance — import from registry.py
            from mcp.registry import mcp_registry

            for tool in tools:
                original_name = tool.get("name", "")
                prefixed_name = f"{server_name}__{original_name}"
                description   = f"[{server_name.upper()} MCP] {tool.get('description', '')}"
                input_schema  = tool.get("inputSchema", {})

                # Create a closure that captures server_name and original_name
                def make_fn(sname, tname):
                    def _call(**kwargs):
                        return self.call_tool(sname, tname, kwargs)
                    _call.__name__ = f"{sname}__{tname}"
                    return _call

                mcp_registry.tools.register(ToolDefinition(
                    name=prefixed_name,
                    description=description,
                    fn=make_fn(server_name, original_name),
                    tags=["external-mcp", server_name],
                    input_schema=input_schema,
                    author=f"mcp:{server_name}",
                ))
            logger.info(f"ExternalMCPRegistry: registered {len(tools)} tools from {server_name}")
        except Exception as e:
            logger.error(f"ExternalMCPRegistry: tool registration failed for {server_name} → {e}")

    # ── DB persistence ───────────────────────────────────────────────────────

    def _load_from_db(self) -> List[ExternalServerConfig]:
        try:
            from db.database import SessionLocal
            from sqlalchemy import text
            configs = []
            with SessionLocal() as db:
                rows = db.execute(text(
                    "SELECT name, transport, command, args, env_vars, sse_url, sse_headers, timeout "
                    "FROM mcp_external_servers WHERE enabled = true ORDER BY name"
                )).fetchall()
                for row in rows:
                    configs.append(ExternalServerConfig(
                        name=row[0],
                        transport=row[1],
                        command=row[2] or "",
                        args=json.loads(row[3] or "[]"),
                        env_vars=json.loads(row[4] or "{}"),
                        sse_url=row[5] or "",
                        sse_headers=json.loads(row[6] or "{}"),
                        timeout=float(row[7] or 30.0),
                    ))
            return configs
        except Exception as e:
            logger.warning(f"ExternalMCPRegistry: could not load from DB → {e}")
            return []

    def _save_to_db(self, config: ExternalServerConfig) -> None:
        try:
            from db.database import SessionLocal
            from sqlalchemy import text
            with SessionLocal() as db:
                db.execute(text("""
                    INSERT INTO mcp_external_servers
                      (name, transport, command, args, env_vars, sse_url, sse_headers, timeout, enabled, status)
                    VALUES (:name, :transport, :command, :args, :env_vars, :sse_url, :sse_headers, :timeout, true, 'connecting')
                    ON CONFLICT (name) DO UPDATE SET
                      transport=EXCLUDED.transport, command=EXCLUDED.command,
                      args=EXCLUDED.args, env_vars=EXCLUDED.env_vars,
                      sse_url=EXCLUDED.sse_url, sse_headers=EXCLUDED.sse_headers,
                      timeout=EXCLUDED.timeout, enabled=true, status='connecting'
                """), {
                    "name":       config.name,
                    "transport":  config.transport,
                    "command":    config.command,
                    "args":       json.dumps(config.args),
                    "env_vars":   json.dumps(config.env_vars),
                    "sse_url":    config.sse_url,
                    "sse_headers": json.dumps(config.sse_headers),
                    "timeout":    config.timeout,
                })
                db.commit()
        except Exception as e:
            logger.warning(f"ExternalMCPRegistry: DB save failed for {config.name} → {e}")

    def _update_db_status(self, name: str, status: str) -> None:
        try:
            from db.database import SessionLocal
            from sqlalchemy import text
            with SessionLocal() as db:
                db.execute(text("""
                    UPDATE mcp_external_servers
                    SET status=:status, last_connected_at=NOW()
                    WHERE name=:name
                """), {"status": status, "name": name})
                db.commit()
        except Exception:
            pass


# Module-level singleton — safe even if mcp_external_servers table doesn't exist yet
# (table is created by db/migrate.py Part P4; _load_from_db() returns [] on any DB error)
try:
    external_mcp_registry = ExternalMCPRegistry.get()
except Exception as _ext_reg_err:
    from core.logger import logger as _ext_logger
    _ext_logger.warning(f"ExternalMCPRegistry: init failed (DB migration pending?) → {_ext_reg_err}")

    class _NoopRegistry:
        """Fallback when mcp_external_servers table doesn't exist yet."""
        def connect_all(self): pass
        def register_server(self, *a, **kw): return False
        def call_tool(self, *a, **kw): return "[ExternalMCP] Registry unavailable — run db/migrate.py"
        def list_connected_servers(self): return []
        def update_server_status(self, *a, **kw): pass

    external_mcp_registry = _NoopRegistry()
