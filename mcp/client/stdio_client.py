# SPDX-License-Identifier: MIT
"""
StdioMCPClient — connects to an MCP server running as a subprocess over stdin/stdout.

Usage:
    client = StdioMCPClient(
        server_name="github",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..."},
    )
    await client.start()
    await client.initialize()
    tools = await client.list_tools()
    result = await client.call_tool("search_repositories", {"query": "AiNxt payment"})
    await client.close()
"""

import asyncio
import json
import os
from typing import Dict, List, Optional

from core.logger import logger
from mcp.client.base import MCPClientSession


class StdioMCPClient(MCPClientSession):
    """
    Launches an MCP server as a subprocess and communicates over stdin/stdout.
    Each line on stdout is a JSON-RPC message.
    """

    def __init__(
        self,
        server_name: str,
        command: str,
        args: List[str] = None,
        env: Dict[str, str] = None,
        cwd: str = None,
    ):
        super().__init__(server_name)
        self._command = command
        self._args    = args or []
        self._env     = env or {}
        self._cwd     = cwd
        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Launch the MCP server subprocess."""
        # Merge env: inherit current env, apply server-specific overrides
        # Do NOT pass internal credentials (ANTHROPIC_API_KEY, POSTGRES_*, etc.)
        safe_env = {
            k: v for k, v in os.environ.items()
            if not any(
                k.startswith(prefix)
                for prefix in ("ANTHROPIC", "POSTGRES", "REDIS", "JWT", "OPENAI", "GOOGLE")
            )
        }
        safe_env.update(self._env)  # server-specific keys (e.g. GITHUB_TOKEN)

        cmd = [self._command] + self._args
        logger.info(f"StdioMCPClient[{self.server_name}]: launching {' '.join(cmd)}")

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_env,
            cwd=self._cwd,
        )

        # Start background readers
        self._reader_task  = asyncio.create_task(self._stdout_reader())
        self._stderr_task  = asyncio.create_task(self._stderr_logger())
        logger.info(f"StdioMCPClient[{self.server_name}]: process started pid={self._process.pid}")

    async def close(self) -> None:
        """Terminate the subprocess and clean up."""
        if self._reader_task:
            self._reader_task.cancel()
        if hasattr(self, "_stderr_task") and self._stderr_task:
            self._stderr_task.cancel()

        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass

        # Resolve any pending futures with an error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("MCP server process closed"))
        self._pending.clear()

        logger.info(f"StdioMCPClient[{self.server_name}]: closed")

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    # ── Transport implementation ─────────────────────────────────────────────

    async def _send(self, message: dict) -> None:
        if not self._process or not self._process.stdin:
            raise ConnectionError("StdioMCPClient: process not started")
        line = json.dumps(message) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()

    async def _stdout_reader(self) -> None:
        """Background task: read stdout lines and dispatch to waiting futures."""
        try:
            while self._process and not self._process.stdout.at_eof():
                line = await self._process.stdout.readline()
                if not line:
                    break
                line = line.decode().strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                    self._dispatch_response(message)
                except json.JSONDecodeError as e:
                    logger.warning(f"StdioMCPClient[{self.server_name}]: bad JSON on stdout: {e} | {line[:200]}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"StdioMCPClient[{self.server_name}]: stdout reader error → {e}")

        # Resolve pending futures as connection lost
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("MCP server stdout closed unexpectedly"))

    async def _stderr_logger(self) -> None:
        """Log stderr from the server process at DEBUG level."""
        try:
            while self._process and not self._process.stderr.at_eof():
                line = await self._process.stderr.readline()
                if line:
                    logger.debug(f"StdioMCPClient[{self.server_name}] stderr: {line.decode().strip()}")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
