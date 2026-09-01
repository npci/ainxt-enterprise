# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AiNxt MCP MASTER REGISTRY  (Phase 10)
# Single entry point for all platform tools and skills.
#
# At startup, MCPRegistry auto-registers every tool and skill
# that ships with the platform so the agent builder and
# orchestrator can discover them by name.
#
# Usage:
#   from mcp.registry import mcp_registry
#
#   mcp_registry.tools.discover(tag="docker")
#   mcp_registry.skills.discover(tag="engineering")
#   mcp_registry.execute_tool("execute_code", code="print('hi')")
#   mcp_registry.describe()   → full catalogue dict
# ============================================================

from typing import Any, Dict, List, Optional

from core.logger import logger
from mcp.tool_registry import ToolRegistry, ToolDefinition, ToolResult
from mcp.skill_registry import SkillRegistry, SkillDefinition


# ============================================================
# MCP REGISTRY
# ============================================================

class MCPRegistry:
    """
    Master registry for the AiNxt Agentic Platform.

    Holds tool_registry and skill_registry as sub-registries.
    On init, bootstraps all built-in platform tools and skills.
    """

    def __init__(self):
        self.tools  = ToolRegistry()
        self.skills = SkillRegistry()
        self._bootstrap()
        logger.info(
            f"MCPRegistry ready — "
            f"tools={len(self.tools)} skills={len(self.skills)}"
        )

    # ========================================================
    # BOOTSTRAP — register all platform-native tools & skills
    # ========================================================

    def _bootstrap(self) -> None:
        self._register_tools()
        self._register_skills()
        # G10: load PRODUCTION MCPServer rows from Postgres at startup
        try:
            self.tools.register_db_tools()
        except Exception as e:
            logger.warning(f"MCPRegistry._bootstrap: register_db_tools failed → {e}")

    # --------------------------------------------------------
    # TOOLS
    # --------------------------------------------------------

    def _register_tools(self) -> None:

        # ---- Retrieval tool --------------------------------
        # Adapter: accepts query string → returns context as text
        try:
            from models.hybrid_retriever import hybrid_retrieve_context
            def _retrieve_adapter(query: str = "", repo_filter: str = None) -> str:
                results = hybrid_retrieve_context(query, repo_filter) if query else []
                return "\n\n".join(str(r) for r in results[:6]) if results else "No context found"
            self.tools.register(ToolDefinition(
                name="retrieve",
                description="Retrieve relevant context from the platform KB (hybrid BM25 + semantic + rerank). Pass repo_filter to scope to a domain KB.",
                fn=_retrieve_adapter,
                tags=["retrieval", "rag", "context"],
                input_schema={"type": "object", "properties": {"query": {"type": "string"}, "repo_filter": {"type": "string"}}},
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register retrieve tool → {e}")

        # ---- Compliance tool --------------------------------
        # Adapter: accepts text string → returns "CLEAN" or "BLOCKED: [types]"
        try:
            from agents.compliance_engine import compliance_engine as _ce
            def _compliance_adapter(query: str = "") -> str:
                if not query:
                    return "CLEAN"
                findings = _ce.analyze(query)
                blocking = [f.get("type", "") for f in findings if f.get("blocked", False)]
                if blocking:
                    return f"COMPLIANCE_BLOCKED: {blocking}"
                return "CLEAN"
            self.tools.register(ToolDefinition(
                name="compliance",
                description="Run PCI/PII compliance check on text. Blocks PAN, CVV, secrets, API keys.",
                fn=_compliance_adapter,
                tags=["pci", "compliance", "security"],
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register compliance tool → {e}")

        # ---- Code execution (Docker preferred, subprocess fallback) ----
        try:
            from sandbox.docker_executor import get_executor

            def _execute_code(code, language="python"):
                executor = get_executor(language)
                if executor is None:
                    return {
                        "success": False,
                        "output": "No sandbox executor available (Docker is down and "
                                  "ALLOW_SUBPROCESS_EXECUTOR is not set).",
                        "exit_code": -1,
                    }
                return executor.execute(code=code, language=language)

            self.tools.register(ToolDefinition(
                name="execute_code",
                description=(
                    "Execute Python or bash code in an isolated sandbox. "
                    "Uses Docker when available (full isolation, all languages). "
                    "Falls back to subprocess (Python only, 30s timeout) only when explicitly "
                    "enabled via ALLOW_SUBPROCESS_EXECUTOR=true."
                ),
                fn=_execute_code,
                tags=["docker", "execution", "sandbox", "code"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "code":     {"type": "string"},
                        "language": {"type": "string", "enum": ["python", "bash", "shell"]},
                    },
                    "required": ["code"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register execute_code tool → {e}")

        # ---- Self-healing execution -------------------------
        try:
            from sandbox.self_healing_engine import SelfHealingEngine
            _engine = SelfHealingEngine()
            self.tools.register(ToolDefinition(
                name="execute_and_heal",
                description="Execute code and automatically fix failures using the LLM. Retries up to 5 times.",
                fn=lambda code, language="python", context=None: _engine.execute_and_heal(
                    code=code, language=language, context=context
                ),
                tags=["docker", "execution", "self-healing", "code"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "code":     {"type": "string"},
                        "language": {"type": "string"},
                        "context":  {"type": "string"},
                    },
                    "required": ["code"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register execute_and_heal tool → {e}")

        # ---- Model router (LLM generate) -------------------
        try:
            from models.model_router import model_router
            self.tools.register(ToolDefinition(
                name="llm_generate",
                description="Generate a response from the best available LLM. Routes to GPT-5 mini / GPT-5.2 / Claude Sonnet / Gemini based on complexity.",
                fn=lambda prompt, model_hint=None: model_router.generate(
                    prompt, model_hint=model_hint
                ),
                tags=["llm", "generation", "reasoning"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "prompt":     {"type": "string"},
                        "model_hint": {"type": "string"},
                    },
                    "required": ["prompt"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register llm_generate tool → {e}")

        # ---- Workflow runner --------------------------------
        try:
            from workflows.engine import workflow_engine
            self.tools.register(ToolDefinition(
                name="run_workflow",
                description="Execute a named Workflow object via the WorkflowEngine. Returns a WorkflowResult.",
                fn=lambda workflow: workflow_engine.run(workflow),
                tags=["workflow", "orchestration", "multi-step"],
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register run_workflow tool → {e}")

        # ---- Memory: save conversation message -------------
        try:
            from memory.redis_memory import RedisMemory
            _mem = RedisMemory()
            self.tools.register(ToolDefinition(
                name="memory_save",
                description="Save a conversation message to Redis memory.",
                fn=lambda session_id, role, content: _mem.save_message(
                    session_id=session_id, role=role, content=content
                ),
                tags=["memory", "session", "redis"],
            ))
            self.tools.register(ToolDefinition(
                name="memory_get",
                description="Retrieve conversation history from Redis memory for a session.",
                fn=lambda session_id, limit=50: _mem.get_conversation(
                    session_id=session_id, limit=limit
                ),
                tags=["memory", "session", "redis"],
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register memory tools → {e}")

        # ---- n8n workflow trigger ---------------------------
        try:
            from tools.n8n_client import trigger_workflow
            self.tools.register(ToolDefinition(
                name="n8n_trigger",
                description="Trigger an n8n webhook workflow with a JSON payload.",
                fn=lambda payload: trigger_workflow(payload),
                tags=["n8n", "workflow", "automation"],
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register n8n_trigger tool → {e}")

        # ---- Agent-to-Agent (A2A) ----------------------------
        try:
            from agents.agent_builder import agent_runner as _agent_runner
            def _call_agent(
                    agent_name:   str,
                    message:      str,
                    session_id:   str = None,
                    context_json: str = None,
            ) -> str:
                """
                Delegate to a named agent.
                Pass context_json (HandoffContext.to_json()) to forward retrieved
                chunks + prior outputs so the receiving agent skips retrieval.
                """
                result = _agent_runner.run(
                    agent_name, message, session_id,
                    context_json=context_json,
                )
                return result.answer if result.success else f"[Agent error: {result.error}]"
            self.tools.register(ToolDefinition(
                name="call_agent",
                description=(
                    "Invoke another named agent with a message. "
                    "Use for delegation, escalation, or agent-to-agent collaboration. "
                    "Optionally pass context_json (HandoffContext JSON) to forward "
                    "retrieved context and prior outputs — receiving agent skips retrieval. "
                    "Returns the agent's text answer."
                ),
                fn=_call_agent,
                tags=["a2a", "delegation", "orchestration"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "agent_name":   {"type": "string", "description": "Name of the target agent"},
                        "message":      {"type": "string", "description": "Message to send to the agent"},
                        "session_id":   {"type": "string", "description": "Optional session ID for memory continuity"},
                        "context_json": {"type": "string", "description": "Optional HandoffContext JSON from agents.handoff.HandoffContext.to_json()"},
                    },
                    "required": ["agent_name", "message"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register call_agent tool → {e}")

        # ---- Jira integration tools -------------------------
        try:
            from tools.jira_tools import (
                jira_create_issue,
                jira_list_issues,
                jira_get_issue,
            )
            self.tools.register(ToolDefinition(
                name="jira_create_issue",
                description="Create a Jira issue. Args: project, summary, description, priority, issue_type.",
                fn=jira_create_issue,
                tags=["jira", "issues", "project-management"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "project":     {"type": "string"},
                        "summary":     {"type": "string"},
                        "description": {"type": "string"},
                        "priority":    {"type": "string", "enum": ["High", "Medium", "Low", "Critical"]},
                        "issue_type":  {"type": "string"},
                    },
                    "required": ["project", "summary", "description"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="jira_list_issues",
                description="List Jira issues for a project. Args: project, status.",
                fn=jira_list_issues,
                tags=["jira", "issues", "project-management"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string"},
                        "status":  {"type": "string"},
                    },
                    "required": ["project"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="jira_get_issue",
                description="Get details of a Jira issue. Args: issue_key (e.g. PROJ-123).",
                fn=jira_get_issue,
                tags=["jira", "issues", "project-management"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string"},
                    },
                    "required": ["issue_key"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register Jira tools → {e}")

        # ---- GitLab integration tools -----------------------
        try:
            from tools.gitlab_tools import (
                gitlab_read_file,
                gitlab_list_issues,
                gitlab_create_issue,
                gitlab_list_mrs,
                gitlab_create_mr,
            )
            self.tools.register(ToolDefinition(
                name="gitlab_read_file",
                description="Read a file from a GitLab repository. Args: repo (namespace/project), path, branch.",
                fn=gitlab_read_file,
                tags=["gitlab", "engineering", "code"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "repo":   {"type": "string"},
                        "path":   {"type": "string"},
                        "branch": {"type": "string"},
                    },
                    "required": ["repo", "path"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="gitlab_list_issues",
                description="List issues in a GitLab project. Args: repo (namespace/project), state (open/closed/all).",
                fn=gitlab_list_issues,
                tags=["gitlab", "engineering", "issues"],
            ))
            self.tools.register(ToolDefinition(
                name="gitlab_create_issue",
                description="Create a new GitLab issue. Args: repo (namespace/project), title, body.",
                fn=gitlab_create_issue,
                tags=["gitlab", "engineering", "issues"],
            ))
            self.tools.register(ToolDefinition(
                name="gitlab_list_mrs",
                description="List merge requests in a GitLab project. Args: repo (namespace/project), state.",
                fn=gitlab_list_mrs,
                tags=["gitlab", "engineering", "mrs"],
            ))
            self.tools.register(ToolDefinition(
                name="gitlab_create_mr",
                description="Create a GitLab merge request. Args: repo, title, body, head (source branch), base (target branch).",
                fn=gitlab_create_mr,
                tags=["gitlab", "engineering", "mrs"],
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register GitLab tools → {e}")

        # ---- Confluence integration tools -------------------
        try:
            from tools.confluence_tools import (
                confluence_create_page,
                confluence_update_page,
                confluence_get_page,
                confluence_search,
                confluence_get_page_by_title,
            )
            self.tools.register(ToolDefinition(
                name="confluence_create_page",
                description="Create a Confluence page. Args: title, body (markdown), space_key, parent_id (optional).",
                fn=confluence_create_page,
                tags=["confluence", "documentation", "knowledge-base"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "title":      {"type": "string"},
                        "body":       {"type": "string"},
                        "space_key":  {"type": "string"},
                        "parent_id":  {"type": "string"},
                    },
                    "required": ["title", "body"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="confluence_update_page",
                description="Update an existing Confluence page. Args: page_id, title, body (markdown).",
                fn=confluence_update_page,
                tags=["confluence", "documentation", "knowledge-base"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string"},
                        "title":   {"type": "string"},
                        "body":    {"type": "string"},
                    },
                    "required": ["page_id", "title", "body"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="confluence_get_page",
                description="Get a Confluence page by ID. Returns title, url, version, excerpt.",
                fn=confluence_get_page,
                tags=["confluence", "documentation", "knowledge-base"],
                input_schema={
                    "type": "object",
                    "properties": {"page_id": {"type": "string"}},
                    "required": ["page_id"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="confluence_search",
                description="Search Confluence using CQL. Args: query, space_key (optional).",
                fn=confluence_search,
                tags=["confluence", "documentation", "search"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "query":     {"type": "string"},
                        "space_key": {"type": "string"},
                    },
                    "required": ["query"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="confluence_get_page_by_title",
                description="Find a Confluence page by exact title within a space.",
                fn=confluence_get_page_by_title,
                tags=["confluence", "documentation", "search"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "title":     {"type": "string"},
                        "space_key": {"type": "string"},
                    },
                    "required": ["title"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register Confluence tools → {e}")

        # ---- Episodic memory tools (Phase 17) --------------
        try:
            from store.episodic_memory import remember, recall
            self.tools.register(ToolDefinition(
                name="memory_remember",
                description="Store a key-value memory for an agent across sessions.",
                fn=lambda agent_name, key, value, tags=None: remember(agent_name, key, value, tags),
                tags=["memory", "episodic", "cross-session"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "agent_name": {"type": "string"},
                        "key":        {"type": "string"},
                        "value":      {"type": "string"},
                        "tags":       {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["agent_name", "key", "value"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="memory_recall",
                description="Recall a stored memory value for an agent by key.",
                fn=lambda agent_name, key: recall(agent_name, key),
                tags=["memory", "episodic", "cross-session"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "agent_name": {"type": "string"},
                        "key":        {"type": "string"},
                    },
                    "required": ["agent_name", "key"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register episodic memory tools → {e}")

        # ---- Zoho People — leave management -----------------
        try:
            from integrations.zoho_people import apply_leave as _zoho_apply_leave
            self.tools.register(ToolDefinition(
                name="zoho_apply_leave",
                description=(
                    "Apply leave in Zoho People HR system. "
                    "Args: employee_id, from_date (DD-Mon-YYYY), to_date (DD-Mon-YYYY), "
                    "reason, leave_type (default: Casual Leave)."
                ),
                fn=_zoho_apply_leave,
                tags=["hr", "zoho", "leave"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "from_date":   {"type": "string", "description": "DD-Mon-YYYY"},
                        "to_date":     {"type": "string", "description": "DD-Mon-YYYY"},
                        "reason":      {"type": "string"},
                        "leave_type":  {"type": "string"},
                    },
                    "required": ["employee_id", "from_date", "to_date", "reason"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register zoho_apply_leave tool → {e}")

        # ---- GitLab SDLC extra tools (branch + file write + patch + search) --
        try:
            from tools.gitlab_tools import (
                gitlab_create_branch,
                gitlab_create_or_update_file,
                gitlab_apply_patch,
                gitlab_search_code,
            )
            self.tools.register(ToolDefinition(
                name="gitlab_create_branch",
                description="Create a new branch in a GitLab repository. Args: repo, branch, from_branch.",
                fn=gitlab_create_branch,
                tags=["gitlab", "engineering", "sdlc"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "repo":        {"type": "string"},
                        "branch":      {"type": "string"},
                        "from_branch": {"type": "string"},
                    },
                    "required": ["repo", "branch"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="gitlab_create_or_update_file",
                description="Create or update a file in a GitLab repository. Args: repo, path, content, message, branch.",
                fn=gitlab_create_or_update_file,
                tags=["gitlab", "engineering", "sdlc"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "repo":    {"type": "string"},
                        "path":    {"type": "string"},
                        "content": {"type": "string"},
                        "message": {"type": "string"},
                        "branch":  {"type": "string"},
                    },
                    "required": ["repo", "path", "content", "message"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="gitlab_apply_patch",
                description=(
                    "Apply a surgical SEARCH/REPLACE patch to an existing file. "
                    "Prefer this over gitlab_create_or_update_file for modifying files — "
                    "it preserves all unchanged code. "
                    "Args: repo, path, search (exact block to find), replace (new block), branch, message."
                ),
                fn=gitlab_apply_patch,
                tags=["gitlab", "engineering", "sdlc", "patch"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "repo":    {"type": "string", "description": "Namespace/project e.g. ainxt/payment-service"},
                        "path":    {"type": "string", "description": "File path within the repo"},
                        "search":  {"type": "string", "description": "Exact existing code block to replace (copy verbatim from gitlab_read_file output)"},
                        "replace": {"type": "string", "description": "New code to substitute in place of the search block"},
                        "branch":  {"type": "string", "description": "Branch name (default: main)"},
                        "message": {"type": "string", "description": "Git commit message"},
                    },
                    "required": ["repo", "path", "search", "replace"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="gitlab_search_code",
                description=(
                    "Search for a code pattern or symbol across a GitLab repository using GitLab's blob search. "
                    "Use to find existing implementations of similar patterns before writing new code. "
                    "Args: repo (namespace/project), query (symbol or pattern), max_results (default 10)."
                ),
                fn=gitlab_search_code,
                tags=["gitlab", "engineering", "search", "sdlc"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "repo":        {"type": "string", "description": "Namespace/project e.g. ainxt/payment-service"},
                        "query":       {"type": "string", "description": "Code pattern, symbol name, or string to search for"},
                        "max_results": {"type": "integer", "description": "Max matches to return (default 10)"},
                    },
                    "required": ["repo", "query"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register GitLab SDLC tools → {e}")

        # ---- Multi-repo SDLC: run diff inspector (Phase 4a) ----
        # Side-effecting import: tools/run_diff_tools.py self-registers
        # `get_run_diff` against tool_registry on module load. Imported here so
        # MCPRegistry._bootstrap() pulls it in alongside the rest of the SDLC
        # toolset.
        try:
            import tools.run_diff_tools  # noqa: F401 — registration runs on import
        except Exception as e:
            logger.warning(f"MCPRegistry: could not load run_diff_tools → {e}")

        # ========================================================
        # Non-engineering MCP tools (vendored from D:\MCPs\mcp_tools).
        # Each one is also exposed as a BaseMCPServer subclass under
        # mcp/servers/<slug>_server.py and is auto-mounted at
        # /ainxt/v1/api/mcp/<slug>/sse via mcp_server_router.py.
        # Dual-registering here lets the agent builder UI and
        # mcp_registry.execute_tool() reach the same functions by bare name.
        # ========================================================

        # ---- kb_search -------------------------------------
        try:
            from tools.kb_search_tools import list_namespaces as _kb_list_namespaces
            from tools.kb_search_tools import search          as _kb_search
            from tools.kb_search_tools import get_document    as _kb_get_document
            self.tools.register(ToolDefinition(
                name="list_namespaces",
                description="List configured KB namespaces and their ACL band.",
                fn=_kb_list_namespaces,
                tags=["mcp", "kb_search", "retrieval"],
                input_schema={"type": "object", "properties": {}},
            ))
            self.tools.register(ToolDefinition(
                name="search",
                description="Search a KB namespace for passages relevant to the query.",
                fn=_kb_search,
                tags=["mcp", "kb_search", "retrieval"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "query":     {"type": "string"},
                        "top_k":     {"type": "integer", "default": 0},
                    },
                    "required": ["namespace", "query"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="get_document",
                description="Fetch the full text of a specific document in a namespace by its doc_id.",
                fn=_kb_get_document,
                tags=["mcp", "kb_search", "retrieval"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "doc_id":    {"type": "string"},
                        "max_chars": {"type": "integer", "default": 20000},
                    },
                    "required": ["namespace", "doc_id"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register kb_search tools → {e}")

        # ---- document_tools --------------------------------
        try:
            from tools.document_tools_tools import list_documents     as _doc_list
            from tools.document_tools_tools import extract_text       as _doc_extract
            from tools.document_tools_tools import search_in_document as _doc_search
            self.tools.register(ToolDefinition(
                name="list_documents",
                description="List readable documents (pdf/md/txt/csv/eml) under the configured document root.",
                fn=_doc_list,
                tags=["mcp", "document_tools", "documents"],
                input_schema={
                    "type": "object",
                    "properties": {"subfolder": {"type": "string", "default": ""}},
                },
            ))
            self.tools.register(ToolDefinition(
                name="extract_text",
                description="Extract plain text from a document given its path relative to the document root.",
                fn=_doc_extract,
                tags=["mcp", "document_tools", "documents"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "path":      {"type": "string"},
                        "max_chars": {"type": "integer", "default": 20000},
                    },
                    "required": ["path"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="search_in_document",
                description="Find occurrences of a query string inside a document; returns surrounding context snippets.",
                fn=_doc_search,
                tags=["mcp", "document_tools", "documents"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "path":          {"type": "string"},
                        "query":         {"type": "string"},
                        "context_chars": {"type": "integer", "default": 300},
                    },
                    "required": ["path", "query"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register document_tools tools → {e}")

        # ---- calendar_tools --------------------------------
        try:
            from tools.calendar_tools_tools import list_calendars  as _cal_list
            from tools.calendar_tools_tools import get_busy        as _cal_busy
            from tools.calendar_tools_tools import find_free_slots as _cal_free
            from tools.calendar_tools_tools import draft_event     as _cal_draft
            self.tools.register(ToolDefinition(
                name="list_calendars",
                description="List ICS calendar files available under the configured data root.",
                fn=_cal_list,
                tags=["mcp", "calendar_tools", "calendar"],
                input_schema={"type": "object", "properties": {}},
            ))
            self.tools.register(ToolDefinition(
                name="get_busy",
                description="Return busy intervals (start, end, title) from a calendar file.",
                fn=_cal_busy,
                tags=["mcp", "calendar_tools", "calendar"],
                input_schema={
                    "type": "object",
                    "properties": {"calendar_path": {"type": "string"}},
                    "required": ["calendar_path"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="find_free_slots",
                description="Find common free slots across multiple calendars on a date (YYYY-MM-DD), within working hours.",
                fn=_cal_free,
                tags=["mcp", "calendar_tools", "calendar"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "calendar_paths": {"type": "array", "items": {"type": "string"}},
                        "date":           {"type": "string"},
                        "duration_min":   {"type": "integer", "default": 60},
                        "earliest":       {"type": "string", "default": ""},
                        "latest":         {"type": "string", "default": ""},
                    },
                    "required": ["calendar_paths", "date"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="draft_event",
                description="Create a DRAFT calendar event written to the outbox as a tentative .ics (not booked).",
                fn=_cal_draft,
                tags=["mcp", "calendar_tools", "calendar"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "title":     {"type": "string"},
                        "start_iso": {"type": "string"},
                        "end_iso":   {"type": "string"},
                        "attendees": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "start_iso", "end_iso", "attendees"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register calendar_tools tools → {e}")

        # ---- email_tools -----------------------------------
        try:
            from tools.email_tools_tools import list_messages as _email_list
            from tools.email_tools_tools import read_message  as _email_read
            from tools.email_tools_tools import draft_reply   as _email_draft
            self.tools.register(ToolDefinition(
                name="list_messages",
                description="List messages (id, from, subject, date) in the configured mailbox.",
                fn=_email_list,
                tags=["mcp", "email_tools", "email"],
                input_schema={"type": "object", "properties": {}},
            ))
            self.tools.register(ToolDefinition(
                name="read_message",
                description="Read the full body of a message by id.",
                fn=_email_read,
                tags=["mcp", "email_tools", "email"],
                input_schema={
                    "type": "object",
                    "properties": {"message_id": {"type": "string"}},
                    "required": ["message_id"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="draft_reply",
                description="Write a DRAFT reply to the outbox (never sends).",
                fn=_email_draft,
                tags=["mcp", "email_tools", "email"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string"},
                        "body":       {"type": "string"},
                    },
                    "required": ["message_id", "body"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register email_tools tools → {e}")

        # ---- task_tracker ----------------------------------
        try:
            from tools.task_tracker_tools import create_task as _task_create
            from tools.task_tracker_tools import list_tasks  as _task_list
            from tools.task_tracker_tools import update_task as _task_update
            self.tools.register(ToolDefinition(
                name="create_task",
                description="Create a task with optional owner (email) and due date (YYYY-MM-DD).",
                fn=_task_create,
                tags=["mcp", "task_tracker", "tasks"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "title":   {"type": "string"},
                        "owner":   {"type": "string", "default": ""},
                        "due":     {"type": "string", "default": ""},
                        "details": {"type": "string", "default": ""},
                    },
                    "required": ["title"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="list_tasks",
                description="List tasks, optionally filtered by status (open/done) and/or owner.",
                fn=_task_list,
                tags=["mcp", "task_tracker", "tasks"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "default": ""},
                        "owner":  {"type": "string", "default": ""},
                    },
                },
            ))
            self.tools.register(ToolDefinition(
                name="update_task",
                description="Update a task's status, owner, or due date.",
                fn=_task_update,
                tags=["mcp", "task_tracker", "tasks"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "status":  {"type": "string", "default": ""},
                        "owner":   {"type": "string", "default": ""},
                        "due":     {"type": "string", "default": ""},
                    },
                    "required": ["task_id"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register task_tracker tools → {e}")

        # ---- data_tools ------------------------------------
        try:
            from tools.data_tools_tools import list_tables      as _data_list
            from tools.data_tools_tools import describe_table   as _data_describe
            from tools.data_tools_tools import query_table      as _data_query
            from tools.data_tools_tools import variance_report  as _data_variance
            from tools.data_tools_tools import reconcile        as _data_recon
            from tools.data_tools_tools import make_chart       as _data_chart
            self.tools.register(ToolDefinition(
                name="list_tables",
                description="List CSV/XLSX tabular sources under the configured data root.",
                fn=_data_list,
                tags=["mcp", "data_tools", "data"],
                input_schema={"type": "object", "properties": {}},
            ))
            self.tools.register(ToolDefinition(
                name="describe_table",
                description="Schema + sample rows + numeric summary for a CSV/XLSX source.",
                fn=_data_describe,
                tags=["mcp", "data_tools", "data"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "path":  {"type": "string"},
                        "sheet": {"type": "string", "default": ""},
                    },
                    "required": ["path"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="query_table",
                description="Query a table: filter / group_by / aggregate (pandas style).",
                fn=_data_query,
                tags=["mcp", "data_tools", "data"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "path":        {"type": "string"},
                        "filter_expr": {"type": "string", "default": ""},
                        "group_by":    {"type": "string", "default": ""},
                        "aggregate":   {"type": "string", "default": ""},
                        "sheet":       {"type": "string", "default": ""},
                        "limit":       {"type": "integer", "default": 100},
                    },
                    "required": ["path"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="variance_report",
                description="Compute budget-vs-actual variance per row and flag rows whose abs variance %% exceeds flag_pct.",
                fn=_data_variance,
                tags=["mcp", "data_tools", "data"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "path":       {"type": "string"},
                        "budget_col": {"type": "string"},
                        "actual_col": {"type": "string"},
                        "label_col":  {"type": "string"},
                        "flag_pct":   {"type": "number", "default": 5.0},
                        "sheet":      {"type": "string", "default": ""},
                    },
                    "required": ["path", "budget_col", "actual_col", "label_col"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="reconcile",
                description="Match two transaction tables on fuzzy reference + amount tolerance; report matches and discrepancies.",
                fn=_data_recon,
                tags=["mcp", "data_tools", "data"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "left_path":        {"type": "string"},
                        "right_path":       {"type": "string"},
                        "amount_col_left":  {"type": "string"},
                        "amount_col_right": {"type": "string"},
                        "ref_col_left":     {"type": "string"},
                        "ref_col_right":    {"type": "string"},
                        "tolerance":        {"type": "number", "default": 1.0},
                    },
                    "required": [
                        "left_path", "right_path",
                        "amount_col_left", "amount_col_right",
                        "ref_col_left", "ref_col_right",
                    ],
                },
            ))
            self.tools.register(ToolDefinition(
                name="make_chart",
                description="Render a chart (line/bar/scatter) from a table to PNG in the charts outbox.",
                fn=_data_chart,
                tags=["mcp", "data_tools", "data"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "path":   {"type": "string"},
                        "chart":  {"type": "string", "enum": ["line", "bar", "scatter"]},
                        "x":      {"type": "string"},
                        "y":      {"type": "string"},
                        "series": {"type": "string", "default": ""},
                        "title":  {"type": "string", "default": ""},
                        "sheet":  {"type": "string", "default": ""},
                    },
                    "required": ["path", "chart", "x", "y"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register data_tools tools → {e}")

        # ---- ats_tools -------------------------------------
        try:
            from tools.ats_tools_tools import list_pipeline         as _ats_list
            from tools.ats_tools_tools import score_keyword_overlap as _ats_score
            from tools.ats_tools_tools import propose_stage_update  as _ats_propose
            self.tools.register(ToolDefinition(
                name="list_pipeline",
                description="List candidates in the configured requisition pipeline, optionally filtered by stage.",
                fn=_ats_list,
                tags=["mcp", "ats_tools", "ats"],
                input_schema={
                    "type": "object",
                    "properties": {"stage": {"type": "string", "default": ""}},
                },
            ))
            self.tools.register(ToolDefinition(
                name="score_keyword_overlap",
                description="Deterministic keyword-coverage score of a resume against JD requirement phrases (0-100).",
                fn=_ats_score,
                tags=["mcp", "ats_tools", "ats"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "resume_text":     {"type": "string"},
                        "jd_must_have":    {"type": "array", "items": {"type": "string"}},
                        "jd_nice_to_have": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["resume_text", "jd_must_have", "jd_nice_to_have"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="propose_stage_update",
                description="Write a PROPOSED stage change to the outbox for recruiter confirmation.",
                fn=_ats_propose,
                tags=["mcp", "ats_tools", "ats"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "new_stage":    {"type": "string"},
                        "rationale":    {"type": "string"},
                    },
                    "required": ["candidate_id", "new_stage", "rationale"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register ats_tools tools → {e}")

        # ---- doc_generator ---------------------------------
        try:
            from tools.doc_generator import write_markdown   as _dg_write_md
            from tools.doc_generator import markdown_to_docx as _dg_md_to_docx
            from tools.doc_generator import slides_to_pptx   as _dg_slides
            self.tools.register(ToolDefinition(
                name="write_markdown",
                description="Write markdown content to a .md file in the generated-docs outbox.",
                fn=_dg_write_md,
                tags=["mcp", "doc_generator", "docs"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "content":  {"type": "string"},
                    },
                    "required": ["filename", "content"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="markdown_to_docx",
                description="Render simple markdown (#/##/### headings, bullets, plain paragraphs) into a .docx file.",
                fn=_dg_md_to_docx,
                tags=["mcp", "doc_generator", "docs"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "filename":         {"type": "string"},
                        "markdown_content": {"type": "string"},
                        "title":            {"type": "string", "default": ""},
                    },
                    "required": ["filename", "markdown_content"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="slides_to_pptx",
                description="Render slides into a .pptx. Each slide: {title, bullets, notes?}.",
                fn=_dg_slides,
                tags=["mcp", "doc_generator", "docs"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "slides":   {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["filename", "slides"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register doc_generator MCP tools → {e}")

        # ---- translator ------------------------------------
        try:
            from tools.translator_tools import load_glossary      as _tx_glossary
            from tools.translator_tools import translate_segments as _tx_translate
            from tools.translator_tools import save_translation   as _tx_save
            self.tools.register(ToolDefinition(
                name="load_glossary",
                description="Load a glossary CSV (term, per-locale columns, instruction) to constrain translation.",
                fn=_tx_glossary,
                tags=["mcp", "translator", "translation"],
                input_schema={
                    "type": "object",
                    "properties": {"glossary_csv_path": {"type": "string"}},
                    "required": ["glossary_csv_path"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="translate_segments",
                description="Translate text segments to target_locale honouring glossary rules.",
                fn=_tx_translate,
                tags=["mcp", "translator", "translation"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "segments":      {"type": "array", "items": {"type": "string"}},
                        "target_locale": {"type": "string"},
                        "glossary":      {"type": "array", "items": {"type": "object"}, "default": []},
                    },
                    "required": ["segments", "target_locale"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="save_translation",
                description="Persist a translated document to the translations outbox as <filename>.<locale>.md.",
                fn=_tx_save,
                tags=["mcp", "translator", "translation"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "locale":   {"type": "string"},
                        "content":  {"type": "string"},
                    },
                    "required": ["filename", "locale", "content"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register translator tools → {e}")

        # ---- lms_tools -------------------------------------
        try:
            from tools.lms_tools_tools import list_modules       as _lms_list
            from tools.lms_tools_tools import save_learning_plan as _lms_save
            from tools.lms_tools_tools import get_learning_plan  as _lms_get
            self.tools.register(ToolDefinition(
                name="list_modules",
                description="List learning modules from the configured catalog, filterable by level and max duration.",
                fn=_lms_list,
                tags=["mcp", "lms_tools", "lms"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "level":            {"type": "string", "default": ""},
                        "max_duration_min": {"type": "integer", "default": 0},
                    },
                },
            ))
            self.tools.register(ToolDefinition(
                name="save_learning_plan",
                description="Persist a learning plan (list of {week, modules, milestone, quiz_topic}) for a learner.",
                fn=_lms_save,
                tags=["mcp", "lms_tools", "lms"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "learner_id": {"type": "string"},
                        "plan":       {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["learner_id", "plan"],
                },
            ))
            self.tools.register(ToolDefinition(
                name="get_learning_plan",
                description="Fetch a previously saved learning plan for a learner.",
                fn=_lms_get,
                tags=["mcp", "lms_tools", "lms"],
                input_schema={
                    "type": "object",
                    "properties": {"learner_id": {"type": "string"}},
                    "required": ["learner_id"],
                },
            ))
        except Exception as e:
            logger.warning(f"MCPRegistry: could not register lms_tools tools → {e}")

    # --------------------------------------------------------
    # SKILLS
    # --------------------------------------------------------

    def _register_skills(self) -> None:

        self.skills.register(SkillDefinition(
            name="answer_question",
            description="Retrieve relevant context then generate a PCI-safe answer.",
            tools=["retrieve", "compliance", "llm_generate"],
            tags=["qa", "retrieval", "generation"],
            examples=["What is the UPI payment flow?"],
        ))

        self.skills.register(SkillDefinition(
            name="fix_bug",
            description="Analyse a bug, generate a fix, execute it in Docker, and verify it passes.",
            tools=["llm_generate", "execute_and_heal"],
            tags=["engineering", "debugging", "self-healing"],
            examples=["Fix the bug with the given error details or stack trace"],
        ))

        self.skills.register(SkillDefinition(
            name="generate_code",
            description="Generate production-grade code for a given specification.",
            tools=["compliance", "llm_generate"],
            tags=["engineering", "code-generation"],
            examples=["Write a Python function to validate IFSC codes"],
        ))

        self.skills.register(SkillDefinition(
            name="run_tests",
            description="Execute a test suite in the Docker sandbox and report results.",
            tools=["execute_code"],
            tags=["engineering", "testing", "docker"],
            examples=["Run pytest on the payments module"],
        ))

        self.skills.register(SkillDefinition(
            name="code_review",
            description="Review code for bugs, security issues, and PCI compliance violations.",
            tools=["compliance", "llm_generate"],
            tags=["engineering", "review", "security"],
            examples=["Review this payment handler for security issues"],
        ))

        self.skills.register(SkillDefinition(
            name="deploy_service",
            description="Execute deployment scripts in the sandbox and verify service health.",
            tools=["execute_code", "llm_generate"],
            tags=["devops", "deployment"],
            examples=["Deploy the payment-service to staging"],
        ))

        self.skills.register(SkillDefinition(
            name="incident_response",
            description="Analyse an incident, generate a root-cause report, and propose a fix.",
            tools=["retrieve", "llm_generate"],
            tags=["sre", "incident", "monitoring"],
            examples=["Analyse the 500 error spike in the settlements API"],
        ))

        # Sync all registered skills to Postgres so GET /skills always matches
        self._sync_skills_to_db()

    def _sync_skills_to_db(self) -> None:
        """Upsert every platform skill into skills_pg so Postgres is the source of truth."""
        try:
            from db.database import SessionLocal
            from db.models import SkillRecord
            db = SessionLocal()
            try:
                for s in self.skills.list_all(enabled_only=False):
                    existing = db.query(SkillRecord).filter(SkillRecord.name == s.name).first()
                    if existing:
                        existing.description  = s.description
                        existing.tools        = s.tools
                        existing.tags         = s.tags
                        existing.status       = "PRODUCTION"
                        existing.is_production = True
                    else:
                        db.add(SkillRecord(
                            name=s.name,
                            description=s.description,
                            tools=s.tools,
                            tags=s.tags,
                            status="PRODUCTION",
                            is_production=True,
                            created_by="platform",
                        ))
                db.commit()
            finally:
                db.close()
        except Exception as e:
            from core.logger import logger
            logger.warning(f"MCPRegistry: skill DB sync failed → {e}")

    # ========================================================
    # CONVENIENCE METHODS
    # ========================================================

    def execute_tool(self, name: str, *args, **kwargs) -> ToolResult:
        """Execute a tool by name. Returns ToolResult.

        Governance enforcement: HTTP-registered (user-defined) tools that are
        not in PRODUCTION state are blocked.  Built-in platform tools have no
        status attribute → they always pass through.
        """
        tool_def = self.tools.get(name)
        if tool_def is not None:
            tool_status = getattr(tool_def, "status", None)
            # Only enforce for user-registered tools (status explicitly set)
            if tool_status is not None and tool_status != "PRODUCTION":
                from datetime import datetime as _dt
                return ToolResult(
                    tool_name=name,
                    success=False,
                    output=(
                        f"Tool '{name}' is not in PRODUCTION state "
                        f"(current: {tool_status}). "
                        "Approval required via POST /governance/mcp/{name}/submit"
                    ),
                    error="governance_blocked",
                    duration_ms=0.0,
                    executed_at=_dt.utcnow().isoformat(),
                )
        return self.tools.execute(name, *args, **kwargs)

    def describe(self) -> Dict[str, Any]:
        """Return the full catalogue as a serialisable dict."""
        return {
            "tools": [
                {
                    "name":        t.name,
                    "description": t.description,
                    "tags":        t.tags,
                    "version":     t.version,
                    "enabled":     t.enabled,
                }
                for t in self.tools.list_all(enabled_only=False)
            ],
            "skills": [
                {
                    "name":        s.name,
                    "description": s.description,
                    "tools":       s.tools,
                    "tags":        s.tags,
                    "enabled":     s.enabled,
                    "examples":    s.examples,
                }
                for s in self.skills.list_all(enabled_only=False)
            ],
        }

    def search(
            self,
            query: Optional[str] = None,
            tag:   Optional[str] = None,
    ) -> Dict[str, List]:
        """Search across both tools and skills simultaneously."""
        return {
            "tools":  self.tools.discover(tag=tag, query=query),
            "skills": self.skills.discover(tag=tag, query=query),
        }


# ============================================================
# SINGLETON
# ============================================================

mcp_registry = MCPRegistry()


# ── MCP Bridge + External Registry bootstrap ─────────────────────────────────
# Runs AFTER mcp_registry is created so internal tools are available to the bridge.

def _bootstrap_mcp_infrastructure() -> None:
    """
    Bootstrap the full MCP infrastructure at platform startup:
      1. MCPBridge: instantiates all 5 internal MCP servers (Jira, Confluence, GitLab, DB, Platform)
      2. ExternalMCPRegistry: loads + connects external MCP servers from DB (non-blocking)
    Called at bottom of this module (runs on first import by gateway.py).
    """
    try:
        from mcp.bridge import mcp_bridge
        mcp_bridge.bootstrap()
    except Exception as e:
        from core.logger import logger
        logger.error(f"MCPBridge bootstrap failed → {e}")

    try:
        from mcp.external_registry import external_mcp_registry
        external_mcp_registry.connect_all()
    except Exception as e:
        from core.logger import logger
        logger.error(f"ExternalMCPRegistry bootstrap failed → {e}")


_bootstrap_mcp_infrastructure()
