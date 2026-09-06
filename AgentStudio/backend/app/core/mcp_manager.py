# SPDX-License-Identifier: MIT
"""
MCP (Model Context Protocol) server manager.

Handles the full lifecycle of MCP tool servers for agents:
  - Starting subprocess-based MCP servers on demand per agent invocation
  - Loading the available tool list via the MCP protocol
  - Normalising tool schemas for Gemini compatibility (fixes array-type issues)
  - Deduplicating tools and truncating descriptions that are too long
  - Caching active client sessions to avoid redundant subprocess launches

Server credentials are always read from environment variables (set in .env):
  GITHUB_TOKEN, POSTGRES_HOST/PORT/DB/USER/PASSWORD, WEAVIATE_URL,
  REST_API_BASE_URL, REST_API_AUTH_TYPE, REST_API_AUTH_TOKEN, …

Key public API:
  McpSessionManager               — per-execution session manager
  McpSessionManager.get_tools_for_agent(node_id, nodes_by_id, edges)
  McpSessionManager.cleanup()     — closes all open sessions
  resolve_agent_mcp_configs()     — returns MCP configs attached to an agent node
  _preprocess_tools(tools)        — dedup + schema fix before passing tools to LLM clients

Used by: native_engine.py
"""
import os
import time

from typing import List, Dict, Any, Optional
from contextlib import AsyncExitStack

from typing import Any

from core.logger import logger
# Resolve the mcp/ directory relative to this file so the project works
# on any machine regardless of where it is cloned/unzipped.
_MCP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "mcp"))

# Registry mapping server types to their MCP server commands and config
MCP_SERVER_REGISTRY: Dict[str, Dict[str, Any]] = {
    "github": {
        "command": "python",
        "args": [os.path.join(_MCP_ROOT, "github_mcp", "server", "github_server.py")],
        "env_mapping": {
            # Core credentials
            "token":                  "GITHUB_TOKEN",
            "api_url":                "GITHUB_API",
            # Scope control
            "agent_scopes":           "MCP_AGENT_SCOPES",
            # Write safety
            "write_allowed_repos":    "GITHUB_WRITE_ALLOWED_REPOS",
            "prod_allowed_repos":     "GITHUB_PROD_ALLOWED_REPOS",
            "prod_write_approved":    "MCP_PROD_WRITE_APPROVED",
            # Branch / path protection
            "protected_branches":     "GITHUB_PROTECTED_BRANCHES",
            "allow_protected_writes": "GITHUB_ALLOW_PROTECTED_BRANCH_WRITES",
            "allow_sensitive_paths":  "GITHUB_ALLOW_SENSITIVE_PATHS",
            # Runtime environment
            "environment":            "MCP_ENVIRONMENT",
        },
        # create_or_update_file needs write+modify; default all three scopes
        "env_defaults": {
            "MCP_AGENT_SCOPES": "read,write,modify",
        },
    },
    "gitlab": {
        "command": "python",
        "args": [os.path.join(_MCP_ROOT, "gitlab_fastmcp", "server", "gitlab_server.py")],
        "env_mapping": {
            "token": "GITLAB_TOKEN",
            "url": "GITLAB_API",
        },
    },
    "postgres": {
        "command": "python",
        "args": [os.path.join(_MCP_ROOT, "postgresql_fastmcp", "postgresql_tools", "src", "server.py")],
        "env_mapping": {
            # Connection
            "host":             "POSTGRES_HOST",
            "port":             "POSTGRES_PORT",
            "database":         "POSTGRES_DB",
            "user":             "POSTGRES_USER",
            "db_credential":    "POSTGRES_PASSWORD",
            # Guardrails
            "agent_scopes":     "MCP_AGENT_SCOPES",
            "sensitive_tables": "POSTGRES_SENSITIVE_TABLES",
        },
        "cwd": os.path.join(_MCP_ROOT, "postgresql_fastmcp", "postgresql_tools", "src"),
    },
    "rest_api": {
        "command": "python",
        "args": [os.path.join(_MCP_ROOT, "restapi_fastmcp", "restapi_tools", "server.py")],
        "env_mapping": {
            "base_url":   "REST_API_BASE_URL",
            "auth_type":  "REST_API_AUTH_TYPE",
            "auth_token": "REST_API_AUTH_TOKEN",
        },
    },
    "weaviate": {
        "command": "npx",
        "args": ["-y", "mcp-server-weaviate"],
        "env_mapping": {
            "url": "WEAVIATE_URL",
            "api_key": "WEAVIATE_API_KEY",
        },
    },
    "teams": {
        "command": "python",
        "args": [os.path.join(_MCP_ROOT, "teams_mcp", "server", "teams_server.py")],
        "env_mapping": {
            # Only the refresh token comes from the UI (user-specific OAuth token)
            "refresh_token": "TEAMS_REFRESH_TOKEN",
        },
        # App credentials are hardcoded in .env — not exposed in the UI
        "env_defaults": {
            "TEAMS_TENANT_ID":     os.getenv("TEAMS_TENANT_ID", ""),
            "TEAMS_CLIENT_ID":     os.getenv("TEAMS_CLIENT_ID", ""),
            "TEAMS_CLIENT_SECRET": os.getenv("TEAMS_CLIENT_SECRET", ""),
        },
    },
}


async def _build_server_params(
    server_type: str,
    config: dict,
    *,
    user_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
    workflow_run_id: Optional[str] = None,
):
    """Build StdioServerParameters from server type and user config.

    ``env_mapping`` keys are read directly from ``config`` as plain values
    (e.g. ``{"token": "ghp_..."}``) and copied into the subprocess env under
    the mapped variable name.
    """
    from mcp import StdioServerParameters

    registry_entry = MCP_SERVER_REGISTRY.get(server_type)
    if not registry_entry:
        raise ValueError(f"Unknown MCP server type: {server_type}")

    # Build environment variables: start with OS env, apply defaults, then config overrides
    env = {**os.environ}
    for env_var, default_val in registry_entry.get("env_defaults", {}).items():
        if env_var not in env or not env[env_var]:
            env[env_var] = default_val

    env_mapping = registry_entry.get("env_mapping", {})
    for config_key, env_var in env_mapping.items():
        if config_key in config and config[config_key]:
            env[env_var] = str(config[config_key]).strip()

    # Build command args - start with base args
    args = list(registry_entry["args"])
    # Append config values that go as command-line arguments
    for config_key in registry_entry.get("args_from_config", []):
        if config_key in config and config[config_key]:
            args.append(config[config_key])

    kwargs = dict(command=registry_entry["command"], args=args, env=env)
    if "cwd" in registry_entry:
        kwargs["cwd"] = registry_entry["cwd"]
    return StdioServerParameters(**kwargs)


async def test_mcp_connection(
    server_type: str,
    config: dict,
    *,
    user_id: Optional[str] = None,
) -> dict:
    """
    Test MCP server connection and return available tools.
    Starts the server, discovers tools, then closes the connection.
    """
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    try:
        server_params = await _build_server_params(
            server_type, config, user_id=user_id,
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_response = await session.list_tools()
                return {
                    "status": "success",
                    "tools": [
                        {"name": t.name, "description": t.description or ""}
                        for t in (tools_response.tools or [])
                    ],
                }
    except Exception as e:
        logger.error(f'[AGENT] MCP connection test failed for {server_type}: {e}')
        return {
            "status": "error",
            "message": str(e),
        }


def resolve_agent_mcp_configs(
    agent_id: str, nodes_by_id: dict, edges: list
) -> List[Dict[str, Any]]:
    """
    Find all MCP nodes connected to a given agent via edges.
    Handles both edge directions:
      - MCP → Agent  (source=mcp_id,   target=agent_id)  — MCP in the main flow
      - Agent → MCP  (source=agent_id, target=mcp_id)    — MCP as a side attachment
    Returns list of dicts: [{server_type, config, node_id}, ...]
    """
    mcp_configs = []
    seen_node_ids: set = set()

    for edge in edges:
        source = edge.source if hasattr(edge, "source") else edge.get("source")
        target = edge.target if hasattr(edge, "target") else edge.get("target")

        mcp_node = None

        # Direction 1: MCP → Agent
        if target == agent_id:
            candidate = nodes_by_id.get(source)
            if candidate and candidate.get("type") == "mcp":
                mcp_node = candidate

        # Direction 2: Agent → MCP
        elif source == agent_id:
            candidate = nodes_by_id.get(target)
            if candidate and candidate.get("type") == "mcp":
                mcp_node = candidate

        if mcp_node and mcp_node.get("id") not in seen_node_ids:
            seen_node_ids.add(mcp_node.get("id"))
            mcp_configs.append(
                {
                    "server_type": mcp_node.get("server_type"),
                    "config": mcp_node.get("config", {}),
                    "node_id": mcp_node.get("id"),
                }
            )

    return mcp_configs


def _fix_array_items(schema: dict) -> dict:
    """
    Recursively fix JSON schemas that have array types without valid 'items' defined.
    Gemini rejects tool schemas where an array parameter is missing 'items' OR
    where 'items' is an empty dict {} (no type specified).
    Defaults missing/empty items to {"type": "string"}.
    Also fixes nested arrays (array of arrays) missing items.items.
    """
    if not isinstance(schema, dict):
        return schema

    schema_type = schema.get("type")

    if schema_type == "array":
        items = schema.get("items")
        # Fix: missing items, empty dict items, or items with no 'type' and no '$ref'
        if not items or (isinstance(items, dict) and not items.get("type") and not items.get("$ref") and not items.get("anyOf")):
            schema["items"] = {"type": "string"}
        else:
            schema["items"] = _fix_array_items(schema["items"])

    # Recurse into object properties
    if "properties" in schema:
        for key, value in schema["properties"].items():
            schema["properties"][key] = _fix_array_items(value)

    # Recurse into anyOf / oneOf / allOf
    for combiner in ("anyOf", "oneOf", "allOf"):
        if combiner in schema:
            schema[combiner] = [_fix_array_items(s) for s in schema[combiner]]

    return schema


def _fix_tool_schemas(tools: list) -> list:
    """
    Fix tool input schemas so they are accepted by Gemini's function calling API.
    Gemini requires all array parameters to have an 'items' field with a type
    (missing or empty {} items both cause 400 errors).

    Operates directly on McpTool.input_schema (a plain dict) — no LangChain
    dependency.
    """
    fixed_count = 0
    for tool in tools:
        try:
            if not isinstance(getattr(tool, "input_schema", None), dict):
                continue
            fixed = _fix_array_items(tool.input_schema)
            if fixed != tool.input_schema:
                tool.input_schema = fixed
                fixed_count += 1
        except Exception as e:
            logger.debug(f"[AGENT] Could not fix schema for tool '{getattr(tool, 'name', '?')}': {e}")

    if fixed_count:
        logger.info(f"[AGENT] Fixed array 'items' in schemas for {fixed_count} tool(s) (Gemini compatibility)")

    return tools


def _preprocess_tools(tools: list, max_description_length: int = 500) -> list:
    """
    Preprocess MCP tools before passing them to LLM clients.

    - Deduplicates tools by name (keeps first occurrence). Prevents Gemini 400
      "Duplicate function declaration" errors when two MCP nodes expose tools
      with the same name (e.g. two REST API nodes both providing http_request).
    - Truncates long descriptions to stay within LLM function-calling limits.
      Many MCP servers (especially GitLab with 50+ tools) have multi-paragraph
      descriptions that can overwhelm the Gemini/OpenAI function calling payload,
      causing the model to dump tool schemas as text instead of invoking them.
    - Fixes array parameters missing 'items' field (Gemini API requirement).
    """
    # Deduplicate by tool name — keeps first occurrence
    seen = set()
    unique_tools = []
    duplicate_names = []
    for tool in tools:
        if tool.name in seen:
            duplicate_names.append(tool.name)
        else:
            seen.add(tool.name)
            unique_tools.append(tool)
    if duplicate_names:
        logger.warning(f"[AGENT] Removed {len(duplicate_names)} duplicate tool(s): {', '.join(sorted(set(duplicate_names)))}")

    truncated_count = 0
    for tool in unique_tools:
        if tool.description and len(tool.description) > max_description_length:
            tool.description = tool.description[:max_description_length].rstrip() + "..."
            truncated_count += 1

    if truncated_count:
        logger.info(f'[AGENT] Truncated descriptions for {truncated_count}/{len(unique_tools)} tools (max {max_description_length} chars)')

    if len(unique_tools) > 40:
        logger.warning(f'[AGENT] Large number of tools loaded ({len(unique_tools)}). Some LLMs may struggle with many function declarations. Consider filtering to essential tools if issues persist.')

    # Fix array schemas for Gemini compatibility (must have 'items' defined)
    unique_tools = _fix_tool_schemas(unique_tools)

    return unique_tools


class McpTool:
    """
    A tool backed by a live MCP server session.
    Pure Python — no LangChain BaseTool dependency.
    """

    def __init__(self, tool_def: Any, session: Any) -> None:
        self.name: str = tool_def.name
        self.description: str = (tool_def.description or "")[:500]
        raw_schema = tool_def.inputSchema
        if raw_schema is None:
            self.input_schema: dict = {"type": "object", "properties": {}}
        elif isinstance(raw_schema, dict):
            self.input_schema = raw_schema
        else:
            try:
                self.input_schema = dict(raw_schema)
            except Exception:
                self.input_schema = {"type": "object", "properties": {}}
        self._session = session

    async def call(self, arguments: dict) -> str:
        """Execute the tool and return a string result."""
        try:
            result = await self._session.call_tool(self.name, arguments)
            parts = []
            for item in (result.content or []):
                if hasattr(item, "text") and item.text is not None:
                    parts.append(item.text)
                elif hasattr(item, "data"):
                    parts.append(str(item.data))
                else:
                    parts.append(str(item))
            return "\n".join(parts) if parts else "(no result)"
        except Exception as e:
            return f"Tool '{self.name}' error: {e}"

    def to_function_spec(self) -> dict:
        """Convert to the standard function declaration format used by LLM clients."""
        from app.core.llm_handler import _clean_tool_schema
        schema = _clean_tool_schema(dict(self.input_schema))
        return {
            "name": self.name,
            "description": self.description,
            "parameters": schema,
        }


class McpSessionManager:
    """
    Manages MCP server sessions for the lifetime of a workflow execution.
    Keeps subprocess sessions alive so agents can make multiple tool calls.
    Deduplicates sessions when the same MCP node is shared by multiple agents.
    No LangChain dependency — tools are returned as McpTool instances.
    """

    def __init__(
        self,
        *,
        user_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        workflow_run_id: Optional[str] = None,
    ) -> None:
        self._exit_stack = AsyncExitStack()
        self._sessions: Dict[str, List[McpTool]] = {}  # node_id -> tools
        # Held for the duration of the workflow run so every MCP server we
        # spawn receives the correct caller context.
        self._user_id = user_id
        self._workflow_id = workflow_id
        self._workflow_run_id = workflow_run_id

    async def get_tools_for_agent(
        self, agent_id: str, nodes_by_id: dict, edges: list
    ) -> List[McpTool]:
        """Return McpTool objects for all MCP nodes connected to an agent."""
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        all_tools: List[McpTool] = []
        mcp_configs = resolve_agent_mcp_configs(agent_id, nodes_by_id, edges)

        for mcp_config in mcp_configs:
            node_id = mcp_config["node_id"]

            # Reuse existing session if already started for this node
            if node_id in self._sessions:
                all_tools.extend(self._sessions[node_id])
                continue

            server_type = mcp_config["server_type"]
            config = mcp_config["config"]

            try:
                logger.info(f'[AGENT] Spawning MCP server subprocess: {server_type} (node {node_id})')
                spawn_start = time.time()

                server_params = await _build_server_params(
                    server_type, config,
                    user_id=self._user_id,
                    workflow_id=self._workflow_id,
                    workflow_run_id=self._workflow_run_id,
                )

                transport = await self._exit_stack.enter_async_context(
                    stdio_client(server_params)
                )
                read, write = transport
                logger.info(f'[AGENT] MCP subprocess started for {server_type}, initializing session...')

                session = await self._exit_stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
                logger.info(f'[AGENT] MCP session initialized for {server_type}, loading tools...')

                # Discover tools directly via MCP protocol — no LangChain adapter needed
                tools_response = await session.list_tools()
                tools: List[McpTool] = []
                seen_names: set = set()
                for tool_def in (tools_response.tools or []):
                    if tool_def.name in seen_names:
                        continue
                    seen_names.add(tool_def.name)
                    tools.append(McpTool(tool_def, session))

                if len(tools) > 40:
                    logger.warning(f"[AGENT] Large tool set from '{server_type}' ({len(tools)} tools). Consider filtering to essential tools.")

                self._sessions[node_id] = tools
                all_tools.extend(tools)

                spawn_elapsed = time.time() - spawn_start
                logger.info(f"[AGENT] Loaded {len(tools)} tools from MCP server '{server_type}' (node {node_id}) in {spawn_elapsed:.2f}s")
            except Exception as e:
                logger.error(f"[AGENT] Failed to load MCP tools from '{server_type}' (node {node_id}): {e}")

        # Deduplicate across nodes (same tool name from different MCP nodes)
        seen: set = set()
        unique: List[McpTool] = []
        for t in all_tools:
            if t.name not in seen:
                seen.add(t.name)
                unique.append(t)
        return unique

    async def cleanup(self):
        """Terminate all MCP server subprocesses."""
        try:
            await self._exit_stack.aclose()
        except RuntimeError as e:
            # anyio cancel scope can't be closed from a different task (e.g. SSE generator's finally block)
            # This is harmless — subprocesses will be cleaned up on exit
            logger.debug(f'[AGENT] MCP session cleanup (cross-task scope): {e}')
        except Exception as e:
            logger.error(f'[AGENT] Error during MCP session cleanup: {e}')
        self._sessions.clear()
