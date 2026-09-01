# SPDX-License-Identifier: Apache-2.0
"""
ConnectorRegistry — DB-backed singleton that:
1. Loads all active connector_definitions from DB at startup
2. Registers their tools into MCPRegistry as LLM-callable tool_use functions
3. Dispatches tool execution via ConnectorEngine
4. Tracks max_connector_calls_per_request to prevent tool misuse
"""
from __future__ import annotations

import json
import threading
from typing import Optional

from connectors.base import ConnectorResponse
from connectors.engine import connector_engine
from core.logger import logger

# Max connector tool calls per /ask request (prevents runaway loops)
MAX_CONNECTOR_CALLS_PER_REQUEST = 3


class ConnectorRegistry:
    """
    Central registry for all connector tools.
    Thread-safe singleton — use `connector_registry` module-level instance.
    """

    def __init__(self):
        self._definitions: list[dict] = []
        self._lock = threading.Lock()
        self._bootstrapped = False

    def bootstrap(self, mcp_tools_registry=None) -> None:
        """
        Load all active connector definitions from DB and optionally register
        tools into the MCPRegistry for LLM tool_use calling.

        Called by mcp/registry.py after _register_tools().
        """
        with self._lock:
            if self._bootstrapped:
                return
            try:
                self._load_definitions()
                if mcp_tools_registry:
                    self._register_to_mcp(mcp_tools_registry)
                self._bootstrapped = True
                logger.info(
                    f"ConnectorRegistry: bootstrapped {len(self._definitions)} connectors, "
                    f"{sum(len(d.get('tools', [])) for d in self._definitions)} tools total"
                )
            except Exception as e:
                logger.warning(f"ConnectorRegistry.bootstrap: {e}")

    def _load_definitions(self) -> None:
        """Load active connector definitions from DB."""
        try:
            from db.database import SessionLocal
            import sqlalchemy as sa
            db = SessionLocal()
            try:
                rows = db.execute(
                    sa.text(
                        "SELECT name, display_name, category, auth_type, tools, base_url, "
                        "has_custom_adapter, rate_limit_per_min, is_active "
                        "FROM ainxt.connector_definitions WHERE is_active = TRUE"
                    )
                ).fetchall()
                self._definitions = [
                    {
                        "name": r[0],
                        "display_name": r[1],
                        "category": r[2],
                        "auth_type": r[3],
                        "tools": r[4] or [],
                        "base_url": r[5],
                        "has_custom_adapter": r[6],
                        "rate_limit_per_min": r[7] or 100,
                    }
                    for r in rows
                ]
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"ConnectorRegistry._load_definitions: {e}")
            self._definitions = []

    def _register_to_mcp(self, tools_registry) -> None:
        """
        Register each connector tool into the MCPRegistry ToolRegistry.
        Tool naming: {connector}__{tool} e.g., microsoft_365__outlook_search_emails
        """
        from mcp.tool_registry import ToolDefinition

        for defn in self._definitions:
            connector_name = defn["name"]
            for tool in defn.get("tools", []):
                tool_name = tool.get("name", "")
                if not tool_name:
                    continue

                mcp_tool_name = f"{connector_name}__{tool_name}"
                description = (
                    f"[{defn['display_name']} connector] {tool.get('description', '')}"
                )

                # Capture loop vars for closure
                _cn = connector_name
                _tn = tool_name

                def _make_fn(cn, tn):
                    def _connector_fn(user_id: str = "", query_text: str = "", **kwargs) -> dict:
                        result = connector_engine.execute(cn, tn, kwargs, user_id, query_text)
                        return result.to_dict()
                    return _connector_fn

                try:
                    tools_registry.register(ToolDefinition(
                        name=mcp_tool_name,
                        description=description,
                        fn=_make_fn(_cn, _tn),
                        tags=["connector", defn["category"], connector_name],
                        input_schema=tool.get("input_schema", {"type": "object", "properties": {}}),
                    ))
                    logger.debug(f"ConnectorRegistry: registered {mcp_tool_name}")
                except Exception as e:
                    logger.warning(f"ConnectorRegistry: failed to register {mcp_tool_name} → {e}")

    # ── Public execution API ───────────────────────────────────────────────────

    def execute(
        self,
        connector_name: str,
        tool_name: str,
        params: dict,
        user_id: str,
        query_text: str = "",
        call_counter: Optional[dict] = None,
    ) -> ConnectorResponse:
        """
        Execute a connector tool.
        call_counter is a mutable dict {count: N} for per-request misuse protection.
        """
        # Lazy bootstrap: RQ workers (scheduled tasks, dispatch) don't bootstrap the
        # registry at startup like the gateway does — without this, connector calls
        # in a worker see zero definitions and the agent reports "no connectivity".
        if not self._bootstrapped:
            self.bootstrap()
        # Guard: max calls per request
        if call_counter is not None:
            call_counter["count"] = call_counter.get("count", 0) + 1
            if call_counter["count"] > MAX_CONNECTOR_CALLS_PER_REQUEST:
                return ConnectorResponse(
                    success=False, items=[], count=0,
                    source=connector_name, tool=tool_name,
                    error=f"Max connector calls per request ({MAX_CONNECTOR_CALLS_PER_REQUEST}) exceeded.",
                )

        return connector_engine.execute(connector_name, tool_name, params, user_id, query_text)

    def get_user_tools(self, user_id: str) -> list[dict]:
        """
        Return LLM-facing tool definitions for connectors the user has connected.
        Only tools for connectors with active tokens are returned.
        """
        if not self._bootstrapped:
            self.bootstrap()
        connected = self._get_connected_connectors(user_id)
        tools = []
        for defn in self._definitions:
            if defn["name"] not in connected:
                continue
            for tool in defn.get("tools", []):
                tools.append({
                    "name": f"{defn['name']}__{tool['name']}",
                    "description": f"[{defn['display_name']}] {tool.get('description', '')}",
                    "input_schema": tool.get("input_schema", {"type": "object", "properties": {}}),
                })
        return tools

    def get_available(self) -> list[dict]:
        """Return all active connector definitions (no auth tokens needed)."""
        # Self-heal: a gunicorn worker that bootstrapped BEFORE the connectors                                                    
        # were seeded holds an empty cache. Reload from DB so every worker returns                                                
        # a consistent list (otherwise the UI flickers as requests round-robin                                                    
        # across workers — some empty, some populated).                                                                           
        if not self._definitions:                                                                                                 
            self._load_definitions()
        result = []
        for defn in self._definitions:
            result.append({
                "name": defn["name"],
                "display_name": defn["display_name"],
                "category": defn["category"],
                "auth_type": defn["auth_type"],
                "tool_count": len(defn.get("tools", [])),
                "tools": [
                    {"name": t["name"], "description": t.get("description", "")}
                    for t in defn.get("tools", [])
                ],
            })
        return result

    def get_user_status(self, user_id: str) -> list[dict]:
        """Return connection status for all connectors for the given user."""
        # Self-heal: in an RQ worker / fresh process the registry may not have
        # bootstrapped yet, leaving self._definitions EMPTY. Without this guard the
        # loop below iterates nothing and reports EVERY connector as "not connected"
        # even when the user has a valid, active token — which is exactly why a
        # scheduled task said "connector could not connect" and never delivered.
        # Mirrors execute / get_user_tools / list_connected_tools, which already guard.
        if not self._bootstrapped:
            self.bootstrap()
        if not self._definitions:
            self._load_definitions()
        connected = self._get_connected_connectors(user_id)
        result = []
        for defn in self._definitions:
            meta = connected.get(defn["name"], {})
            result.append({
                "name": defn["name"],
                "display_name": defn["display_name"],
                "category": defn["category"],
                "auth_type": defn["auth_type"],
                "connected": defn["name"] in connected,
                "connected_as": meta.get("email", ""),
                "workspace": meta.get("workspace_name", ""),
                "tool_count": len(defn.get("tools", [])),
            })
        return result

    def list_connected_tools(self, user_id: str) -> list:
        """
        For the Cowork office planner: returns the tools the user can actually
        use right now — i.e. tools belonging to connectors this user has an
        active OAuth token for. Shape:
          [{connector, tool, description, required: [param,...]}]
        Empty list if nothing is connected (planner then skips connector_call).
        """
        if not self._bootstrapped:
            self.bootstrap()
        connected = set(self._get_connected_connectors(user_id).keys())
        if not connected:
            return []
        out = []
        for d in self._definitions:
            if d.get("name") not in connected:
                continue
            for t in (d.get("tools") or []):
                schema = t.get("input_schema") or {}
                out.append({
                    "connector": d["name"],
                    "tool": t.get("name", ""),
                    "description": t.get("description", ""),
                    "is_write": bool(t.get("is_write", False)),
                    "required": list(schema.get("required") or []),
                })
        return out

    def _get_connected_connectors(self, user_id: str) -> dict:
        """Returns {connector_name: metadata} for user's active tokens."""
        try:
            from db.database import SessionLocal
            import sqlalchemy as sa
            db = SessionLocal()
            try:
                rows = db.execute(
                    sa.text(
                        "SELECT connector_name, metadata FROM ainxt.user_oauth_tokens "
                        "WHERE user_id = :uid AND is_active = TRUE"
                    ),
                    {"uid": user_id},
                ).fetchall()
                return {r[0]: r[1] or {} for r in rows}
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"ConnectorRegistry._get_connected_connectors: {e}")
            return {}


# Module-level singleton
connector_registry = ConnectorRegistry()
