# SPDX-License-Identifier: MIT
"""cli_runtime.mcp_server — the single MCP tool plane for the spawned CLI.

This is the "one MCP server" the CLI sees. It exposes EVERY ABStudio tool —
GitLab, Jira, Confluence, ``code_executor``, ``read_skill_file``, document
reading, M365, and any user-generated catalog tool — through one JSON-RPC
surface, and executes each one by handing it to the SAME ``ToolDispatcher`` the
native engine uses.

That reuse is the whole point. ``ToolDispatcher.dispatch()`` already resolves the
caller's personal access tokens out of the credential vault, runs the tool's code
in a ``python -I`` sandbox with a sanitised env, retries transient failures with
backoff, caps output, and writes the audit trail. By routing MCP through it we add
a *transport*, not a second implementation — so there is exactly one place where
tool semantics live and no chance of the two paths drifting.

Protocol
--------
JSON-RPC 2.0, MCP protocol ``2024-11-05``: ``initialize``,
``notifications/initialized``, ``tools/list``, ``tools/call``, ``ping``.
Verified against ``ainxt 0.2.101``, which connects as
``ainxt-shell-<server-name>`` and advertises ``2025-06-18`` — we echo our own
supported version, which it accepts.

Two traps this module exists to avoid
-------------------------------------
1. **``__`` in a tool name.** The CLI namespaces MCP tools as
   ``<server>__<tool>`` and splits on ``__``. A tool whose own name contains
   ``__`` produces two delimiters and the CLI *silently drops it* — no error, the
   tool simply never appears. ``microsoft_365__outlook_send_mail`` is exactly
   such a name, so names are sanitised on the way out and restored on the way in.
2. **Manifest bloat.** There are ~150 catalog tools. Sending them all would
   swamp the model's tool manifest and degrade selection. We expose only the
   tools attached to the agent being run.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from core.logger import logger

from .config import CliRuntimeConfig
from .session import RunSession, ToolEvent, TOOL_EVENT_RESULT, TOOL_EVENT_START

# MCP protocol version we implement and advertise.
MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_VERSION = "1.0.0"

# JSON-RPC error codes (spec-defined).
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603

# Descriptions are truncated to this many chars, matching the existing
# ``mcp_manager._preprocess_tools`` budget, and warned above this tool count.
_MAX_DESCRIPTION_CHARS = 500
_TOOL_COUNT_WARN_THRESHOLD = 40

# Tools that cannot cross the MCP boundary, because they are protocol drivers
# rather than capabilities:
#   ``ask_human``   — drives the HITL interrupt/snapshot/resume state machine,
#                     which requires suspending the run; a subprocess cannot.
#   ``spawn_swarm`` — needs a live in-process ``SwarmRuntime`` + ``SwarmContext``
#                     and streams sub-agent SSE frames back through the engine.
# They stay engine-executed. Rather than hide them (which would make the model
# claim the capability does not exist), the server returns a structured sentinel
# the bridge intercepts to run the native path.
ENGINE_NATIVE_TOOLS = frozenset({"ask_human", "spawn_swarm"})

# Marker on a sentinel result so the bridge can recognise it unambiguously.
ENGINE_NATIVE_SENTINEL = "__abstudio_engine_native__"


# ════════════════════════════════════════════════════════════════════════════
# Tool-name sanitisation
# ════════════════════════════════════════════════════════════════════════════

def sanitize_tool_name(name: str) -> str:
    """Collapse ``__`` runs so the CLI's ``server__tool`` split stays unambiguous.

    ``microsoft_365__outlook_send_mail`` → ``microsoft_365_outlook_send_mail``.
    Most names are unaffected.
    """
    out = str(name or "")
    while "__" in out:
        out = out.replace("__", "_")
    return out


def build_name_maps(names: List[str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Return ``(exposed_by_real, real_by_exposed)`` for a set of tool names.

    Sanitising can collide (two real names mapping to one exposed name). On a
    collision the first wins and the loser is dropped with a warning — silently
    shadowing a tool would be worse, since a call would then be routed to the
    wrong implementation.
    """
    exposed_by_real: Dict[str, str] = {}
    real_by_exposed: Dict[str, str] = {}
    for real in names:
        if not real:
            continue
        exposed = sanitize_tool_name(real)
        if exposed in real_by_exposed and real_by_exposed[exposed] != real:
            logger.warning(
                "[CLI-MCP] tool name collision after sanitisation — dropping",
                exposed=exposed, kept=real_by_exposed[exposed], dropped=real,
            )
            continue
        exposed_by_real[real] = exposed
        real_by_exposed[exposed] = real
    return exposed_by_real, real_by_exposed


# ════════════════════════════════════════════════════════════════════════════
# Catalog loading
# ════════════════════════════════════════════════════════════════════════════

def _normalise_schema(schema: Any) -> dict:
    """Coerce a catalog ``input_schema`` into a valid JSON Schema object.

    The catalog stores shorthand in places (e.g. ``{"query": "string"}``), and a
    malformed schema makes the CLI reject the tool outright, so anything
    unusable degrades to a permissive object rather than breaking the manifest.
    """
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}}
    if "type" not in schema and "properties" not in schema:
        # Shorthand form: {"field": "string", ...}
        props: Dict[str, Any] = {}
        for key, val in schema.items():
            if isinstance(val, str):
                props[key] = {"type": val if val in (
                    "string", "number", "integer", "boolean", "object", "array",
                ) else "string"}
            elif isinstance(val, dict):
                props[key] = val
        return {"type": "object", "properties": props}
    out = dict(schema)
    out.setdefault("type", "object")
    out.setdefault("properties", {})
    return out


async def load_tool_specs(
    *,
    allowed_tools: List[str],
    expose_draft_tools: bool = False,
) -> List[dict]:
    """Return ``[{name, description, input_schema}]`` for the agent's tools.

    Merged from three sources, in priority order:

    1. ``PLATFORM_TOOLS`` — ``code_executor`` and ``read_skill_file`` live here,
       not in the database, so a DB-only lookup would silently lose them (this is
       one of the failure modes of the earlier attempt).
    2. ``tools_catalog`` (DB) — the ~150 seeded connector tools plus anything a
       user has generated.
    3. ``CANONICAL_TOOLS`` — in-memory, and the only place ``draft: True`` tools
       (``confluence_*``, ``zoho_*``, ``n8n_*``, ``memory_*``) exist at all, since
       drafts are never seeded. Consulted only when explicitly enabled.

    Order matters: a name found earlier wins, so the platform definition of
    ``code_executor`` is never shadowed by a stale DB row.
    """
    wanted = [t for t in dict.fromkeys(allowed_tools) if t]
    if not wanted:
        return []
    wanted_set = set(wanted)
    by_name: Dict[str, dict] = {}

    # (1) platform tools
    try:
        from app.tools.platform_tools import PLATFORM_TOOLS
        for spec in PLATFORM_TOOLS:
            if isinstance(spec, dict) and spec.get("name") in wanted_set:
                by_name.setdefault(spec["name"], spec)
    except Exception as exc:
        logger.warning("[CLI-MCP] could not load PLATFORM_TOOLS", error=str(exc))

    # (2) DB catalog
    try:
        from app.core import workflow_repo
        for row in await workflow_repo.list_tools():
            if isinstance(row, dict) and row.get("name") in wanted_set:
                by_name.setdefault(row["name"], row)
    except Exception as exc:
        logger.error("[CLI-MCP] catalog load failed", error=str(exc))

    # (3) canonical / draft tools
    if expose_draft_tools:
        try:
            from app.tools.canonical_tools import CANONICAL_TOOLS
            for spec in CANONICAL_TOOLS:
                if isinstance(spec, dict) and spec.get("name") in wanted_set:
                    by_name.setdefault(spec["name"], spec)
        except Exception as exc:
            logger.warning("[CLI-MCP] could not load CANONICAL_TOOLS", error=str(exc))

    specs: List[dict] = []
    missing: List[str] = []
    for name in wanted:
        if name in ENGINE_NATIVE_TOOLS:
            # Advertised, but executed by the engine — see ENGINE_NATIVE_TOOLS.
            specs.append({
                "name": name,
                "description": _engine_native_description(name),
                "input_schema": _engine_native_schema(name),
            })
            continue
        row = by_name.get(name)
        if not row:
            missing.append(name)
            continue
        desc = str(row.get("description") or "")[:_MAX_DESCRIPTION_CHARS]
        specs.append({
            "name": name,
            "description": desc,
            "input_schema": _normalise_schema(row.get("input_schema")),
        })

    if missing:
        # Match the native engine, which drops unresolvable tools with a warning
        # rather than failing the run.
        logger.warning("[CLI-MCP] tools not found in any source", missing=missing)
    if len(specs) > _TOOL_COUNT_WARN_THRESHOLD:
        logger.warning(
            "[CLI-MCP] large tool manifest may degrade model tool selection",
            tool_count=len(specs), threshold=_TOOL_COUNT_WARN_THRESHOLD,
        )
    return specs


def _engine_native_description(name: str) -> str:
    if name == "ask_human":
        return (
            "Ask the human operator a question and pause until they answer. "
            "Use when you need a decision, approval, or missing information."
        )
    return (
        "Delegate a complex goal to a swarm of specialist sub-agents that plan "
        "and execute in parallel, then return an aggregated result."
    )


def _engine_native_schema(name: str) -> dict:
    if name == "ask_human":
        return {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question to ask."},
                "options": {"type": "array", "items": {"type": "string"}},
                "context": {"type": "string"},
            },
            "required": ["question"],
        }
    return {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "The goal to delegate."},
            "hints": {"type": "object"},
        },
        "required": ["goal"],
    }


# ════════════════════════════════════════════════════════════════════════════
# The server
# ════════════════════════════════════════════════════════════════════════════

class AbstudioMcpServer:
    """Serves MCP JSON-RPC for ONE run session.

    Cheap to construct (one per request); all expensive state — the DB pool, the
    dispatcher's caches — is process-global and shared.
    """

    def __init__(self, *, session: RunSession, config: CliRuntimeConfig) -> None:
        self._session = session
        self._config = config

    # ── dispatch ───────────────────────────────────────────────────────────
    async def handle(self, body: Any) -> Optional[dict]:
        """Handle one JSON-RPC message. Returns ``None`` for notifications.

        Never raises: an unhandled error becomes a JSON-RPC error object, because
        a 500 here would look to the CLI like a dead server and abort the run.
        """
        if not isinstance(body, dict):
            return _error(None, ERR_INVALID_REQUEST, "request must be a JSON object")
        if body.get("jsonrpc") != "2.0":
            return _error(body.get("id"), ERR_INVALID_REQUEST, "jsonrpc must be '2.0'")

        method = body.get("method") or ""
        msg_id = body.get("id")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}

        try:
            if method == "initialize":
                return self._initialize(msg_id, params)
            if method == "notifications/initialized" or method == "initialized":
                self._session.handshake_done = True
                return None
            if method.startswith("notifications/"):
                return None
            if method == "ping":
                return _ok(msg_id, {})
            if method == "tools/list":
                return await self._tools_list(msg_id)
            if method == "tools/call":
                return await self._tools_call(msg_id, params)
            return _error(msg_id, ERR_METHOD_NOT_FOUND, f"method not found: {method}")
        except Exception as exc:
            logger.exception(
                "[CLI-MCP] handler crashed",
                run_id=self._session.run_id, method=method, error=str(exc),
            )
            return _error(msg_id, ERR_INTERNAL, f"internal error: {exc}")

    # ── initialize ─────────────────────────────────────────────────────────
    def _initialize(self, msg_id: Any, params: dict) -> dict:
        client = params.get("clientInfo") or {}
        logger.info(
            "[CLI-MCP] initialize",
            run_id=self._session.run_id,
            client=client.get("name") or "?",
            client_protocol=params.get("protocolVersion") or "?",
        )
        return _ok(msg_id, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": self._config.mcp_server_name,
                "version": SERVER_VERSION,
            },
        })

    # ── tools/list ─────────────────────────────────────────────────────────
    async def _tools_list(self, msg_id: Any) -> dict:
        session = self._session
        specs = await load_tool_specs(
            allowed_tools=session.allowed_tools,
            expose_draft_tools=self._config.expose_draft_tools,
        )
        exposed_by_real, _ = build_name_maps([s["name"] for s in specs])

        tools = [
            {
                "name": exposed_by_real[s["name"]],
                "description": s["description"],
                "inputSchema": s["input_schema"],
            }
            for s in specs if s["name"] in exposed_by_real
        ]
        session.listed_tool_count = len(tools)

        if not tools and session.allowed_tools:
            # Loud, because a silently empty manifest is the exact failure the
            # earlier attempt spent six commits chasing: the CLI would report a
            # healthy handshake and then behave as if the agent had no tools.
            logger.error(
                "[CLI-MCP] tools/list resolved to ZERO tools despite an allow-list "
                "— the agent will behave as though it has no capabilities",
                run_id=session.run_id, requested=session.allowed_tools,
            )
        else:
            logger.info(
                "[CLI-MCP] tools/list",
                run_id=session.run_id, exposed=len(tools),
                names=[t["name"] for t in tools][:40],
            )
        return _ok(msg_id, {"tools": tools})

    # ── tools/call ─────────────────────────────────────────────────────────
    async def _tools_call(self, msg_id: Any, params: dict) -> dict:
        session = self._session
        exposed_name = str(params.get("name") or "")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}

        _, real_by_exposed = build_name_maps(session.allowed_tools)
        real_name = real_by_exposed.get(exposed_name, exposed_name)

        if not session.allows_tool(real_name):
            # Out of scope. Returned as a tool result (not a protocol error) so
            # the model can recover by choosing a different tool.
            logger.warning(
                "[CLI-MCP] tool call denied — not attached to this agent",
                run_id=session.run_id, tool=exposed_name,
            )
            return _tool_text(msg_id, json.dumps({
                "error": f"Tool '{exposed_name}' is not available to this agent.",
            }), is_error=True)

        session.tool_calls += 1
        started = time.monotonic()

        session.publish(ToolEvent(
            kind=TOOL_EVENT_START,
            tool_name=real_name,
            arguments=_redact(arguments),
        ))

        # Engine-native tools: hand back a sentinel for the bridge to intercept.
        if real_name in ENGINE_NATIVE_TOOLS:
            payload = {
                ENGINE_NATIVE_SENTINEL: True,
                "tool": real_name,
                "arguments": arguments,
            }
            logger.info(
                "[CLI-MCP] engine-native tool requested",
                run_id=session.run_id, tool=real_name,
            )
            session.publish(ToolEvent(
                kind=TOOL_EVENT_RESULT, tool_name=real_name,
                result=payload, duration_ms=0,
            ))
            return _tool_text(msg_id, json.dumps(payload))

        # ``read_skill_file`` is fail-closed on an empty allow-list, so the scope
        # guard must run here with the session's skills or every call is denied.
        if real_name == "read_skill_file":
            scope_error = _check_skill_scope(arguments, session.allowed_skills)
            if scope_error:
                session.publish(ToolEvent(
                    kind=TOOL_EVENT_RESULT, tool_name=real_name,
                    error=scope_error,
                    duration_ms=int((time.monotonic() - started) * 1000),
                ))
                return _tool_text(msg_id, json.dumps({"error": scope_error}), is_error=True)

        result = await self._dispatch(real_name, arguments)
        duration_ms = int((time.monotonic() - started) * 1000)

        files = _extract_generated_files(result)
        if files:
            session.record_files(files)

        session.publish(ToolEvent(
            kind=TOOL_EVENT_RESULT,
            tool_name=real_name,
            result=result,
            error=str(result.get("error") or "") if isinstance(result, dict) else "",
            duration_ms=duration_ms,
            generated_files=files,
        ))

        payload = _encode_result(result, self._config.max_tool_result_bytes)
        logger.info(
            "[CLI-MCP] tools/call done",
            run_id=session.run_id, tool=real_name, duration_ms=duration_ms,
            bytes=len(payload), files=len(files),
            is_error=isinstance(result, dict) and bool(result.get("error")),
        )
        return _tool_text(
            msg_id, payload,
            is_error=isinstance(result, dict) and bool(result.get("error")),
        )

    async def _dispatch(self, tool_name: str, arguments: dict) -> Any:
        """Execute a tool through the shared ``ToolDispatcher``.

        Errors are returned as ``{"error": ...}`` rather than raised — the same
        contract the native engine relies on, so the model can read and react to
        the failure instead of the run dying.
        """
        session = self._session
        try:
            from agent_factory.pipeline import ToolDispatcher
        except Exception as exc:
            logger.error("[CLI-MCP] ToolDispatcher import failed", error=str(exc))
            return {"error": f"tool runtime unavailable: {exc}"}

        artifact_dir = session.workflow_artifact_dir if tool_name == "code_executor" else ""
        # Per-agent Sample Document: expose SAMPLE_DOC_* env vars inside
        # the code_executor sandbox so LLM code can open the user's
        # look-and-feel reference (see app/core/skill_manifest.
        # sample_doc_directive for the prompt block that mirrors these
        # env var names). Forwarded only for code_executor because
        # other tools have no reason to read the sample.
        sample_path = ""
        sample_kind = ""
        if tool_name == "code_executor":
            sample_path = getattr(session, "sample_doc_path", "") or ""
            sample_kind = getattr(session, "sample_doc_kind", "") or ""
        try:
            return await ToolDispatcher().dispatch(
                tool_name=tool_name,
                inputs=arguments,
                user_id=session.user_id,
                email=session.email,
                workflow_artifact_dir=artifact_dir,
                sample_doc_path=sample_path,
                sample_doc_kind=sample_kind,
            )
        except PermissionError as exc:
            # Raised by the credential resolvers when a user has not configured a
            # required token. Surfaced verbatim: the message already tells the
            # user where to add it (Profile → GitLab Token).
            return {"error": str(exc)}
        except Exception as exc:
            logger.exception(
                "[CLI-MCP] dispatch raised",
                run_id=session.run_id, tool=tool_name, error=str(exc),
            )
            return {"error": f"tool execution failed: {exc}"}


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _check_skill_scope(arguments: dict, allowed_skills: List[str]) -> str:
    """Return an error string when a ``read_skill_file`` call escapes its scope.

    Delegates to ``app.core.skill_manifest.enforce_read_skill_file_scope``, which
    is fail-closed: an empty allow-list blocks every call, so "attached = only
    accessible" holds even under prompt injection.

    The module is loaded BY FILE PATH rather than as ``app.core.skill_manifest``
    because importing it as a package member executes ``app/core/__init__.py``,
    which pulls in the entire data layer (and FastAPI). ``skill_manifest`` itself
    is stdlib-only, so a direct load is both cheaper and keeps this guard
    available in contexts where the full package cannot be imported — and if the
    guard could not be loaded we deny the call rather than allow it.
    """
    global _SKILL_SCOPE_GUARD
    if _SKILL_SCOPE_GUARD is None:
        try:
            import importlib.util
            import os as _os

            path = _os.path.join(
                _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                "core", "skill_manifest.py",
            )
            spec = importlib.util.spec_from_file_location("_abstudio_skill_manifest", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            _SKILL_SCOPE_GUARD = module.enforce_read_skill_file_scope
        except Exception as exc:
            logger.error(
                "[CLI-MCP] could not load the read_skill_file scope guard — "
                "denying the call (fail closed)", error=str(exc),
            )
            return ("ERROR: skill access could not be verified for this agent, "
                    "so the request was refused.")
    try:
        return _SKILL_SCOPE_GUARD(arguments, allowed_skills) or ""
    except Exception as exc:
        logger.error("[CLI-MCP] skill scope guard raised — denying", error=str(exc))
        return "ERROR: skill access could not be verified, so the request was refused."


# Cached guard callable; resolved once per process on first use.
_SKILL_SCOPE_GUARD = None


def _ok(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_text(msg_id: Any, text: str, *, is_error: bool = False) -> dict:
    """Wrap a payload in MCP's ``content`` envelope."""
    return _ok(msg_id, {
        "content": [{"type": "text", "text": text}],
        "isError": bool(is_error),
    })


def _encode_result(result: Any, max_bytes: int) -> str:
    """JSON-encode a tool result, truncating rather than overflowing.

    ``default=str`` keeps non-serialisable values (dates, Decimals) from turning
    a successful tool call into a failure.
    """
    try:
        payload = json.dumps(result, default=str)
    except Exception:
        payload = json.dumps({"error": "tool result was not JSON-serialisable"})
    if len(payload) > max_bytes:
        payload = payload[:max_bytes] + '... "[truncated]"'
    return payload


def _extract_generated_files(result: Any) -> List[dict]:
    """Pull ``generated_files`` out of a tool result.

    Handles both shapes the platform produces: the canonical list, and the
    single-file shape some tools return.
    """
    if not isinstance(result, dict):
        return []
    files = result.get("generated_files")
    if isinstance(files, list):
        return [f for f in files if isinstance(f, dict)]
    if result.get("filename") and result.get("download_url"):
        return [{
            "filename": result.get("filename"),
            "disk_name": result.get("disk_name") or result.get("filename"),
            "download_url": result.get("download_url"),
            "format": result.get("format") or "",
            "path": result.get("path") or "",
        }]
    return []


_SECRET_HINTS = ("token", "secret", "password", "api_key", "apikey", "auth", "credential")


def _redact(inputs: Any) -> dict:
    """Mask secret-looking values before they reach a log or an SSE frame."""
    if not isinstance(inputs, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, val in inputs.items():
        if any(hint in str(key).lower() for hint in _SECRET_HINTS):
            out[key] = "***"
        else:
            out[key] = val
    return out


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "ENGINE_NATIVE_TOOLS",
    "ENGINE_NATIVE_SENTINEL",
    "AbstudioMcpServer",
    "load_tool_specs",
    "sanitize_tool_name",
    "build_name_maps",
]
