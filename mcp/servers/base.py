# SPDX-License-Identifier: MIT
"""
BaseMCPServer — JSON-RPC 2.0 MCP server foundation.

Supports two transports:
  stdio  — reads from stdin, writes to stdout (one JSON-RPC message per line)
  SSE    — exposed via FastAPI: GET /mcp/{name}/sse + POST /mcp/{name}/message

Subclasses register tools via @tool() decorator and the base handles:
  - initialize / initialized handshake
  - tools/list discovery
  - tools/call dispatch with PCI/PII compliance gates on input + output
  - error serialization (JSON-RPC error objects)

Usage (stdio):
    server = JiraMCPServer()
    asyncio.run(server.run_stdio())

Usage (SSE via FastAPI):
    server = JiraMCPServer()
    # SSE stream: server.sse_stream(session_id)    → AsyncGenerator[str, None]
    # Message:    await server.handle_message(body) → dict
"""

import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from core.logger import logger

# MCP protocol version this server speaks
MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_SERVER_VERSION   = "1.0.0"


# ── Tool descriptor ──────────────────────────────────────────────────────────

@dataclass
class MCPTool:
    name:        str
    description: str
    fn:          Callable
    input_schema: Dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    pci_audit:   bool = False   # if True: full I/O logged to audit table


# ── JSON-RPC helpers ─────────────────────────────────────────────────────────

def _ok(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}

def _err(id_: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": err}

def _text_content(text: str) -> dict:
    return {"content": [{"type": "text", "text": str(text)}]}


# ── Compliance check ─────────────────────────────────────────────────────────

def _compliance_check(text: str) -> Optional[str]:
    """Returns block reason if PCI/PII detected, else None.
    Uses ComplianceEngine.validate_input — the real API (there is no `.check`,
    which silently failed and skipped the gate for every MCP tool input)."""
    try:
        from agents.compliance_engine import ComplianceEngine
        res = ComplianceEngine().validate_input(text or "")
        if res and res.get("blocked"):
            types = res.get("blocked_types") or res.get("types") or []
            return f"Compliance violation: {types[0] if types else 'BLOCKED'}"
    except Exception as e:
        # Fail CLOSED. This previously warned and returned None, i.e. "no violation", so an import
        # error or a privacy-svc timeout silently skipped the gate for every MCP tool input —
        # the exact failure this function's own docstring says it exists to prevent.
        logger.critical(f"MCPServer: compliance unavailable — refusing tool input → {e}")
        return "Compliance screening unavailable — input refused rather than passed unscanned."
    return None


# ── Base server ──────────────────────────────────────────────────────────────

class BaseMCPServer:
    """
    Spec-compliant MCP server base class.
    Subclasses call self._register(MCPTool(...)) in __init__.
    """

    server_name:    str = "base"
    server_version: str = MCP_SERVER_VERSION

    def __init__(self):
        self._tools: Dict[str, MCPTool] = {}
        self._sessions: Dict[str, dict] = {}  # session_id → {initialized: bool}
        self._setup_tools()

    def _setup_tools(self):
        """Override in subclass to register tools."""

    def _register(self, tool: MCPTool):
        self._tools[tool.name] = tool
        logger.debug(f"MCPServer[{self.server_name}]: registered tool '{tool.name}'")

    # ── Protocol dispatch ──────────────────────────────────────────────────

    async def handle_message(self, body: dict, session_id: str = None) -> Optional[dict]:
        """
        Process one JSON-RPC 2.0 message. Returns response dict or None (for notifications).
        """
        if not isinstance(body, dict):
            return _err(None, -32700, "Parse error")

        jsonrpc = body.get("jsonrpc")
        if jsonrpc != "2.0":
            return _err(body.get("id"), -32600, "Invalid Request: jsonrpc must be '2.0'")

        method = body.get("method", "")
        id_    = body.get("id")          # None for notifications
        params = body.get("params") or {}

        # Notifications have no id — process but return nothing
        is_notification = id_ is None

        try:
            if method == "initialize":
                return await self._handle_initialize(id_, params, session_id)
            elif method == "initialized":
                return None  # notification — no response
            elif method == "tools/list":
                if session_id and session_id not in self._sessions:
                    return _err(id_, -32001, "Session not initialised — call initialize first")
                return await self._handle_tools_list(id_, params)
            elif method == "tools/call":
                if session_id and session_id not in self._sessions:
                    return _err(id_, -32001, "Session not initialised — call initialize first")
                return await self._handle_tools_call(id_, params)
            elif method == "ping":
                return _ok(id_, {})
            elif method.startswith("notifications/"):
                return None  # ignore server notifications
            else:
                if is_notification:
                    return None
                return _err(id_, -32601, f"Method not found: {method}")
        except Exception as e:
            logger.error(f"MCPServer[{self.server_name}]: unhandled error in {method} → {e}")
            return _err(id_, -32603, "Internal error", str(e))

    # ── Method handlers ────────────────────────────────────────────────────

    async def _handle_initialize(self, id_: Any, params: dict, session_id: str) -> dict:
        client_version = params.get("protocolVersion", "")
        if session_id:
            self._sessions[session_id] = {"initialized": True, "client_version": client_version}
        return _ok(id_, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools":     {"listChanged": False},
                "resources": {},
                "prompts":   {},
            },
            "serverInfo": {
                "name":    self.server_name,
                "version": self.server_version,
            },
        })

    async def _handle_tools_list(self, id_: Any, params: dict) -> dict:
        tools = []
        for t in self._tools.values():
            tools.append({
                "name":        t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            })
        return _ok(id_, {"tools": tools})

    async def _handle_tools_call(self, id_: Any, params: dict) -> dict:
        tool_name  = params.get("name", "")
        arguments  = params.get("arguments") or {}

        tool = self._tools.get(tool_name)
        if tool is None:
            return _err(id_, -32602, f"Unknown tool: {tool_name}")

        # Compliance gate — input
        input_str = json.dumps(arguments)
        block = _compliance_check(input_str)
        if block:
            return _ok(id_, {"content": [{"type": "text", "text": f"[BLOCKED] {block}"}], "isError": True})

        # Execute
        try:
            t0 = time.time()
            if asyncio.iscoroutinefunction(tool.fn):
                result = await tool.fn(**arguments)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: tool.fn(**arguments))
            duration_ms = (time.time() - t0) * 1000
            result_str = str(result) if not isinstance(result, str) else result
        except TypeError as e:
            return _err(id_, -32602, f"Invalid arguments for {tool_name}: {e}")
        except Exception as e:
            logger.error(f"MCPServer[{self.server_name}]: tool '{tool_name}' error → {e}")
            return _ok(id_, {"content": [{"type": "text", "text": f"Tool error: {e}"}], "isError": True})

        # Compliance gate — output
        block = _compliance_check(result_str)
        if block:
            return _ok(id_, {"content": [{"type": "text", "text": f"[OUTPUT BLOCKED] {block}"}], "isError": True})

        # Audit log for PCI tools
        if tool.pci_audit:
            self._audit(tool_name, arguments, result_str, duration_ms)

        return _ok(id_, _text_content(result_str))

    # ── Stdio transport ────────────────────────────────────────────────────

    async def run_stdio(self):
        """Run as a stdio MCP server. Reads from stdin, writes to stdout."""
        session_id = str(uuid.uuid4())
        logger.info(f"MCPServer[{self.server_name}]: starting stdio transport session={session_id}")

        loop = asyncio.get_event_loop()

        while True:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue

                try:
                    body = json.loads(line)
                except json.JSONDecodeError as e:
                    resp = _err(None, -32700, f"Parse error: {e}")
                    sys.stdout.write(json.dumps(resp) + "\n")
                    sys.stdout.flush()
                    continue

                response = await self.handle_message(body, session_id)
                if response is not None:
                    sys.stdout.write(json.dumps(response) + "\n")
                    sys.stdout.flush()

            except EOFError:
                break
            except Exception as e:
                logger.error(f"MCPServer[{self.server_name}]: stdio loop error → {e}")
                break

        logger.info(f"MCPServer[{self.server_name}]: stdio session ended")

    # ── SSE transport ──────────────────────────────────────────────────────

    async def sse_stream(self, session_id: str) -> AsyncGenerator[str, None]:
        """
        SSE stream for a session. Sends keep-alive pings every 15s.
        Actual responses are sent via the message queue for this session.
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._sessions[session_id] = {"initialized": False, "queue": queue}

        # Send endpoint event (MCP SSE protocol requires this)
        yield f"event: endpoint\ndata: /mcp/{self.server_name}/message?sessionId={session_id}\n\n"

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # keep-alive
        except asyncio.CancelledError:
            pass
        finally:
            self._sessions.pop(session_id, None)

    async def handle_sse_message(self, body: dict, session_id: str) -> None:
        """
        Handle a message POSTed to /mcp/{name}/message.
        Response is pushed onto the session's SSE queue.
        """
        session = self._sessions.get(session_id, {})
        response = await self.handle_message(body, session_id)
        if response is not None:
            queue = session.get("queue")
            if queue:
                await queue.put(response)

    async def handle_streamable_http(
        self,
        body: dict,
        session_id: str = None,
        user_id: str = None,
    ) -> tuple:
        """
        Streamable HTTP transport handler (MCP spec 2024-11-05).

        Used by CLI v0.2.101+ which POSTs JSON-RPC directly to /sse and expects
        the response inline (no persistent SSE stream required). The caller
        (mcp_server_router) must echo the returned session_id as the
        Mcp-Session-Id response header on every reply — the CLI's
        StreamableHttpClientWorker validates this header and fails the handshake
        without it.

        Returns (response_dict, mcp_session_id). This is a thin wrapper over
        handle_message() so all dispatch, compliance, and audit logic is shared
        with the legacy SSE path.
        """
        sid = session_id or str(uuid.uuid4())
        response = await self.handle_message(body, session_id=sid, user_id=user_id)
        return response, sid

    # ── Helpers ────────────────────────────────────────────────────────────

    def _audit(self, tool_name: str, inputs: dict, output: str, duration_ms: float):
        """Write a PCI-audited tool call to tool_audit_log.

        SEC-F-018: inputs/output are redacted before being written to the
        audit table. This table previously stored raw tool inputs and
        outputs verbatim — since these are only the PCI/high-sensitivity
        tools (tool.pci_audit=True), that meant the exact data this gate is
        meant to protect was being persisted unredacted into a queryable
        table, defeating the purpose of gating the tool in the first place.
        Uses ComplianceEngine.redact_text() (the same redaction used
        elsewhere in the platform, e.g. connector read results) rather than
        the input-blocking validate_input() — an audit record should still
        capture that a call happened and roughly what it concerned, just
        with PAN/secret/PII values masked, not refuse to log at all.
        Redaction failure never blocks the tool call or the audit write —
        it fails open to the ORIGINAL text so an audit gap is never silently
        created by a redaction bug; only a redaction SUCCESS changes what
        gets stored.
        """
        try:
            from agents.compliance_engine import ComplianceEngine
            _engine = ComplianceEngine()
            inputs_str = json.dumps(inputs, default=str)
            try:
                inputs_str, _ = _engine.redact_text(inputs_str)
            except Exception as e:
                logger.debug(f"MCPServer: audit input redaction failed, logging unredacted → {e}")
            try:
                output, _ = _engine.redact_text(output)
            except Exception as e:
                logger.debug(f"MCPServer: audit output redaction failed, logging unredacted → {e}")

            from db.database import SessionLocal
            from sqlalchemy import text
            with SessionLocal() as db:
                db.execute(text("""
                    INSERT INTO tool_audit_log (tool_name, inputs, output, duration_ms, created_at)
                    VALUES (:tool, :inp, :out, :dur, NOW())
                """), {"tool": tool_name, "inp": inputs_str, "out": output[:2000], "dur": duration_ms})
                db.commit()
        except Exception as e:
            logger.debug(f"MCPServer: audit log failed → {e}")
