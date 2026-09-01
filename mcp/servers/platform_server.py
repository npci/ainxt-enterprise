# SPDX-License-Identifier: Apache-2.0
"""
Platform MCP Server — exposes AiNxt AI platform capabilities as MCP tools.

Allows external MCP clients (other Claude instances, tools, CI/CD pipelines)
to use the platform's RAG, agent execution, and health APIs over the MCP protocol.

Tools exposed:
  platform_ask           — RAG-powered Q&A against indexed codebases/docs
  platform_agent_run     — invoke a named platform agent
  platform_list_agents   — list available production agents
  platform_index_status  — get indexing status for a repo
  platform_health        — platform health check
"""

import asyncio
import json
import os

from mcp.servers.base import BaseMCPServer, MCPTool
from core.config import PLATFORM_BASE_URL as _CONFIG_PLATFORM_BASE_URL
from core.logger import logger

# No localhost default: reuses the canonical (also no-default) core.config
# value; every call site below is already wrapped in try/except.
_BASE_URL = os.getenv("PLATFORM_BASE_URL", _CONFIG_PLATFORM_BASE_URL)

# SEC-F-005: single module-level constant instead of four inline getenv() calls.
# Emits a WARNING at import time when the token is unset, so misconfigured
# deployments are visible in startup logs rather than silently sending
# unauthenticated requests to the platform.
_PLATFORM_SERVICE_TOKEN = os.getenv("PLATFORM_SERVICE_TOKEN", "")
if not _PLATFORM_SERVICE_TOKEN:
    logger.warning(
        "platform_server: PLATFORM_SERVICE_TOKEN is not set — MCP tool calls "
        "to the platform will be sent without an Authorization header."
    )


def _platform_ask(question: str, repo_filter: str = None, department: str = None) -> str:
    """Call /ask endpoint with a system service token."""
    try:
        import httpx
        payload = {"question": question}
        if repo_filter:
            payload["repo_filter"] = repo_filter
        if department:
            payload["department"] = department

        # Use service-to-service token (set in env)
        token = _PLATFORM_SERVICE_TOKEN  # SEC-F-005
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        chunks = []
        with httpx.stream(
            "POST", f"{_BASE_URL}/ask",
            json=payload, headers=headers, timeout=60.0
        ) as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                        if "t" in data:
                            chunks.append(data["t"])
                    except Exception:
                        pass
        return "".join(chunks) or "No answer returned."
    except Exception as e:
        return f"Platform ask error: {e}"


def _platform_agent_run(agent_name: str, message: str, session_id: str = None) -> str:
    """Invoke a named platform agent via the agents API."""
    try:
        import httpx
        token = _PLATFORM_SERVICE_TOKEN  # SEC-F-005
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"} if token else {}
        payload = {"message": message}
        if session_id:
            payload["session_id"] = session_id

        resp = httpx.post(
            f"{_BASE_URL}/agents/{agent_name}/run",
            json=payload, headers=headers, timeout=90.0,
        )
        data = resp.json()
        return data.get("answer", str(data))
    except Exception as e:
        return f"Agent run error: {e}"


def _platform_list_agents() -> str:
    """List all PRODUCTION agents available on the platform."""
    try:
        import httpx
        token = _PLATFORM_SERVICE_TOKEN  # SEC-F-005
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = httpx.get(f"{_BASE_URL}/agents", headers=headers, timeout=10.0)
        agents = resp.json()
        if isinstance(agents, list):
            lines = [f"- {a.get('name', '?')}: {a.get('description', '')}" for a in agents]
            return "\n".join(lines)
        return str(agents)
    except Exception as e:
        return f"Error listing agents: {e}"


def _platform_index_status(repo: str) -> str:
    """Get the indexing status of a repository."""
    try:
        import httpx
        token = _PLATFORM_SERVICE_TOKEN  # SEC-F-005
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = httpx.get(f"{_BASE_URL}/index/{repo}/status", headers=headers, timeout=10.0)
        return json.dumps(resp.json(), indent=2)
    except Exception as e:
        return f"Index status error: {e}"


def _platform_health() -> str:
    """Check platform health."""
    try:
        import httpx
        resp = httpx.get(f"{_BASE_URL}/health", timeout=5.0)
        return json.dumps(resp.json(), indent=2)
    except Exception as e:
        return f"Health check error: {e}"


class PlatformMCPServer(BaseMCPServer):

    server_name = "platform"

    def _setup_tools(self):
        self._register(MCPTool(
            name="platform_ask",
            description=(
                "Query the AiNxt AI platform's RAG system. "
                "Searches indexed codebases and knowledge bases to answer engineering questions. "
                "Optionally scope to a specific repo or department."
            ),
            fn=_platform_ask,
            input_schema={
                "type": "object",
                "properties": {
                    "question":    {"type": "string", "description": "The question to ask"},
                    "repo_filter": {"type": "string", "description": "Limit search to a specific repo (e.g. 'org/payment-service')"},
                    "department":  {"type": "string", "description": "Department context for scoping (e.g. 'payments', 'hr')"},
                },
                "required": ["question"],
            },
        ))

        self._register(MCPTool(
            name="platform_agent_run",
            description=(
                "Invoke a named AiNxt platform agent (e.g. 'compliance-checker', 'jira-triage-bot'). "
                "Returns the agent's structured answer. Use platform_list_agents to discover available agents."
            ),
            fn=_platform_agent_run,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Agent name as registered in the platform"},
                    "message":    {"type": "string", "description": "Message to send to the agent"},
                    "session_id": {"type": "string", "description": "Session ID for memory continuity (optional)"},
                },
                "required": ["agent_name", "message"],
            },
        ))

        self._register(MCPTool(
            name="platform_list_agents",
            description="List all PRODUCTION agents available on the AiNxt AI platform.",
            fn=_platform_list_agents,
            input_schema={"type": "object", "properties": {}},
        ))

        self._register(MCPTool(
            name="platform_index_status",
            description="Get the indexing status, chunk count, and last-indexed date for a repository.",
            fn=_platform_index_status,
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository name or 'org/project' path"},
                },
                "required": ["repo"],
            },
        ))

        self._register(MCPTool(
            name="platform_health",
            description="Check the health of the AiNxt AI platform (all services).",
            fn=_platform_health,
            input_schema={"type": "object", "properties": {}},
        ))


if __name__ == "__main__":
    asyncio.run(PlatformMCPServer().run_stdio())
