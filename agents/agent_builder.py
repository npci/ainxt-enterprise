# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AiNxt AGENTIC PLATFORM — AGENT BUILDER BACKEND  (Phase 11)
#
# Allows engineers to:
#   • define agents (system prompt + tool/skill assignments)
#   • persist agent definitions to Redis
#   • run agents: compliance → tools → LLM → compliance → result
#
# Usage:
#   from agents.agent_builder import agent_builder, agent_runner
#
#   defn = agent_builder.create(AgentDefinition(
#       name="bug_fixer",
#       system_prompt="You are an expert bug-fixing agent...",
#       tools=["retrieve", "execute_and_heal"],
#       skills=["fix_bug"],
#   ))
#
#   result = agent_runner.run("bug_fixer", "Fix the NPE in PaymentService")
# ============================================================

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.logger import logger
from core.config import REDIS_HOST as _REDIS_HOST, REDIS_PORT as _REDIS_PORT
from agents.compliance_engine import compliance_engine
from core.model_registry import MODEL_COST_PER_1M as _MODEL_COST_PER_1M


# ── Cost estimator (mirrors gateway.py _estimate_cost) ───────────────────────

def _estimate_usage_cost(model: str, in_tok: int, out_tok: int) -> float:
    """Cost in USD using MODEL_COST_PER_1M (per-1M rates)."""
    _m = (model or "").lower()
    if "local" in _m or "ollama" in _m or "llama" in _m:
        return 0.0
    in_rate, out_rate = _MODEL_COST_PER_1M.get(model, (2.00, 8.00))
    return round((in_tok * in_rate + out_tok * out_rate) / 1_000_000, 8)


# ============================================================
# AGENT DEFINITION
# ============================================================

@dataclass
class AgentDefinition:
    """
    A named, reusable agent configuration.

    Fields:
        name          Unique agent name (snake_case).
        description   What the agent does.
        system_prompt System-level instruction injected before every run.
        tools         Ordered list of MCP tool names to invoke per run.
        skills        Ordered list of MCP skill names (informational;
                      tools from skill definitions are resolved at runtime).
        workflows     Workflow names to chain (future).
        tags          Discovery tags.
        version       Semver string.
        author        Who created the agent.
        enabled       Disabled agents refuse to run.
        created_at    ISO timestamp.
        metadata      Arbitrary extra data.
    """
    name:          str
    description:   str
    system_prompt: str
    tools:         List[str]       = field(default_factory=list)
    skills:        List[str]       = field(default_factory=list)
    workflows:     List[str]       = field(default_factory=list)
    tags:          List[str]       = field(default_factory=list)
    version:       str             = "1.0.0"
    author:        str             = "platform"
    enabled:       bool            = True
    # Governance lifecycle: DRAFT | PENDING_APPROVAL | APPROVED | PRODUCTION | DEPRECATED
    # New user-created agents start as DRAFT (must go through approval)
    # Platform bootstrap agents are explicitly set to PRODUCTION
    status:          str             = "DRAFT"
    created_by:      str             = "platform"
    approved_by:     str             = ""
    created_at:      str             = field(default_factory=lambda: datetime.utcnow().isoformat())
    metadata:        Dict[str, Any]  = field(default_factory=dict)
    # Dynamic config
    kb_namespace:    Optional[str]   = None   # e.g. "docs_kb:hr" — scopes retrieve tool per agent
    preferred_model: Optional[str]   = None   # "auto"|"claude"|"gpt"|"ollama" — per-agent model hint


# ============================================================
# AGENT RUN RESULT
# ============================================================

@dataclass
class AgentRunResult:
    """Outcome of a single agent execution."""
    agent_name:       str
    run_id:           str
    success:          bool
    answer:           str
    tool_outputs:     List[Dict[str, Any]]
    compliance_flags: List[str]
    duration_ms:      float
    started_at:       str
    error:            Optional[str] = None


# ============================================================
# AGENT BUILDER  (CRUD + persistence)
# ============================================================

class AgentBuilder:
    """
    Create, retrieve, list, update, and delete AgentDefinitions.

    Source of truth: Postgres `agents_pg` table.
    Redis db=2 is a fast runtime cache only — it is populated FROM Postgres,
    never the other way around.  Prompt updates in Postgres take effect on
    the next call to reload_from_db() (called automatically by the governance
    approve endpoint) — no restart required.
    """

    _REDIS_PREFIX = "agent_builder:agent:"
    _REDIS_INDEX  = "agent_builder:index"

    def __init__(self):
        self._agents: Dict[str, AgentDefinition] = {}
        self._redis = None
        self._connect_redis()
        # ── Postgres is the source of truth — load it first ──────
        _pg_count = self._load_from_postgres()
        # Redis fills any agents that exist in cache but not yet synced to Postgres
        # (backward-compat for agents created before the Postgres-first migration)
        self._load_from_redis()
        logger.info(
            f"AgentBuilder initialised — "
            f"{_pg_count} agent(s) from Postgres, {len(self._agents)} total"
        )

    # --------------------------------------------------------
    # REDIS SETUP
    # --------------------------------------------------------

    def _connect_redis(self) -> None:
        try:
            from core.config import RDB_WORKFLOW
            from core.kv import get_kv
            self._redis = get_kv(RDB_WORKFLOW, decode_responses=True)
            self._redis.ping()
            logger.info(f"AgentBuilder: KV connected (db=2, backend={self._redis.backend})")
        except Exception as e:
            logger.warning(f"AgentBuilder: KV unavailable → {e}; running in-memory only")
            self._redis = None

    # --------------------------------------------------------
    # POSTGRES — source of truth
    # --------------------------------------------------------

    def _load_from_postgres(self) -> int:
        """Load all enabled PRODUCTION agents from Postgres into self._agents.
        Returns the count of agents loaded. Silently skips on DB errors."""
        loaded = 0
        try:
            from db.database import SessionLocal
            from db.models import AgentRecord
            import dataclasses
            known = {f.name for f in dataclasses.fields(AgentDefinition)}
            db = SessionLocal()
            try:
                rows = (
                    db.query(AgentRecord)
                    .filter(
                        AgentRecord.enabled == True,  # noqa: E712
                        AgentRecord.status == "PRODUCTION",
                        )
                    .all()
                )
                for row in rows:
                    data = {
                        "name":            row.name,
                        "description":     row.description or "",
                        "system_prompt":   row.system_prompt or "",
                        "tools":           list(row.tools or []),
                        "skills":          list(row.skills or []),
                        "enabled":         row.enabled,
                        "status":          row.status,
                        "created_by":      row.created_by or "platform",
                        "approved_by":     row.approved_by or "",
                        "visibility":      row.visibility or "public",
                        "kb_namespace":    getattr(row, "kb_namespace", None),
                        "preferred_model": getattr(row, "preferred_model", None),
                    }
                    data = {k: v for k, v in data.items() if k in known}
                    self._agents[row.name] = AgentDefinition(**data)
                    loaded += 1
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"AgentBuilder: Postgres load failed ({e}) — will fall back to Redis")
        return loaded

    def _upsert_to_postgres(self, definition: AgentDefinition, on_conflict: str = "update") -> None:
        """Write an AgentDefinition to Postgres agents_pg.
        on_conflict='update'  → update all mutable columns (default for admin changes)
        on_conflict='ignore'  → skip if name+org_id already exists (for bootstrap seeding)
        """
        try:
            from db.database import SessionLocal
            from db.models import AgentRecord
            db = SessionLocal()
            try:
                existing = (
                    db.query(AgentRecord)
                    .filter(AgentRecord.name == definition.name,
                            AgentRecord.org_id == "default")
                    .first()
                )
                if existing is None:
                    rec = AgentRecord(
                        name=definition.name,
                        org_id="default",
                        description=definition.description,
                        system_prompt=definition.system_prompt,
                        tools=definition.tools,
                        skills=definition.skills,
                        enabled=definition.enabled,
                        status=definition.status,
                        created_by=definition.created_by or "platform",
                        approved_by=definition.approved_by or "platform",
                        is_production=True,
                        visibility=getattr(definition, "visibility", "public"),
                    )
                    db.add(rec)
                    db.commit()
                    logger.info(f"AgentBuilder: inserted agent {definition.name!r} into Postgres")
                elif on_conflict == "update":
                    existing.description  = definition.description
                    existing.system_prompt = definition.system_prompt
                    existing.tools        = definition.tools
                    existing.skills       = definition.skills
                    existing.enabled      = definition.enabled
                    existing.status       = definition.status
                    existing.approved_by  = definition.approved_by or existing.approved_by
                    db.commit()
                    logger.info(f"AgentBuilder: updated agent {definition.name!r} in Postgres")
                # on_conflict='ignore': existing row found → do nothing
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"AgentBuilder: Postgres upsert failed for {definition.name!r} ({e})")

    def reload_from_db(self, name: Optional[str] = None) -> int:
        """Hot-reload agent(s) from Postgres into the in-memory cache + Redis.
        Call this after updating a prompt in DB — takes effect immediately, no restart.
        name=None reloads ALL agents; name=<str> reloads just that one agent.
        Returns number of agents reloaded.
        """
        try:
            from db.database import SessionLocal
            from db.models import AgentRecord
            import dataclasses
            known = {f.name for f in dataclasses.fields(AgentDefinition)}
            db = SessionLocal()
            reloaded = 0
            try:
                query = db.query(AgentRecord).filter(
                    AgentRecord.enabled == True,  # noqa: E712
                    AgentRecord.status == "PRODUCTION",
                    )
                if name:
                    query = query.filter(AgentRecord.name == name)
                for row in query.all():
                    data = {
                        "name":            row.name,
                        "description":     row.description or "",
                        "system_prompt":   row.system_prompt or "",
                        "tools":           list(row.tools or []),
                        "skills":          list(row.skills or []),
                        "enabled":         row.enabled,
                        "status":          row.status,
                        "created_by":      row.created_by or "platform",
                        "approved_by":     row.approved_by or "",
                        "visibility":      row.visibility or "public",
                        "kb_namespace":    getattr(row, "kb_namespace", None),
                        "preferred_model": getattr(row, "preferred_model", None),
                    }
                    data = {k: v for k, v in data.items() if k in known}
                    defn = AgentDefinition(**data)
                    self._agents[row.name] = defn
                    self._persist(defn)   # sync to Redis cache
                    reloaded += 1
            finally:
                db.close()
            logger.info(
                f"AgentBuilder: reloaded {reloaded} agent(s) from Postgres"
                + (f" (name={name!r})" if name else " (all)")
            )
            return reloaded
        except Exception as e:
            logger.error(f"AgentBuilder: reload_from_db failed ({e})")
            return 0

    # --------------------------------------------------------
    # CRUD
    # --------------------------------------------------------

    def create(self, definition: AgentDefinition, _pg_on_conflict: str = "update") -> AgentDefinition:
        """Register a new agent definition (overwrites if name exists).
        Also upserts to Postgres (source of truth).
        Pass _pg_on_conflict='ignore' for bootstrap seeding to avoid overwriting admin-customized prompts.
        """
        if definition.name in self._agents:
            logger.debug(f"AgentBuilder: overwriting agent {definition.name!r} in memory cache")
        self._agents[definition.name] = definition
        self._persist(definition)
        self._upsert_to_postgres(definition, on_conflict=_pg_on_conflict)
        logger.info(
            f"AgentBuilder: created agent {definition.name!r} "
            f"tools={definition.tools} skills={definition.skills}"
        )
        return definition

    def get(self, name: str) -> Optional[AgentDefinition]:
        return self._agents.get(name)

    def list_all(self, enabled_only: bool = False) -> List[AgentDefinition]:
        agents = list(self._agents.values())
        if enabled_only:
            agents = [a for a in agents if a.enabled]
        return agents

    def delete(self, name: str) -> bool:
        if name not in self._agents:
            return False
        del self._agents[name]
        self._delete_from_redis(name)
        logger.info(f"AgentBuilder: deleted agent {name!r}")
        return True

    def enable(self, name: str) -> None:
        self._get_or_raise(name).enabled = True
        self._persist(self._agents[name])

    def disable(self, name: str) -> None:
        self._get_or_raise(name).enabled = False
        self._persist(self._agents[name])

    def names(self) -> List[str]:
        return list(self._agents.keys())

    def discover(
            self,
            tag: Optional[str] = None,
            query: Optional[str] = None,
    ) -> List[AgentDefinition]:
        results = self.list_all()
        if tag:
            results = [a for a in results if tag.lower() in a.tags]
        if query:
            q = query.lower()
            results = [
                a for a in results
                if q in a.name.lower() or q in a.description.lower()
            ]
        return results

    # --------------------------------------------------------
    # REDIS PERSISTENCE
    # --------------------------------------------------------

    def _persist(self, definition: AgentDefinition) -> None:
        if self._redis is None:
            return
        try:
            key = f"{self._REDIS_PREFIX}{definition.name}"
            self._redis.set(key, json.dumps(asdict(definition)))
            self._redis.sadd(self._REDIS_INDEX, definition.name)
        except Exception as e:
            logger.warning(f"AgentBuilder: Redis persist failed → {e}")

    def _delete_from_redis(self, name: str) -> None:
        if self._redis is None:
            return
        try:
            self._redis.delete(f"{self._REDIS_PREFIX}{name}")
            self._redis.srem(self._REDIS_INDEX, name)
        except Exception as e:
            logger.warning(f"AgentBuilder: Redis delete failed → {e}")

    def _load_from_redis(self) -> None:
        if self._redis is None:
            return
        try:
            names = self._redis.smembers(self._REDIS_INDEX)
            for name in names:
                raw = self._redis.get(f"{self._REDIS_PREFIX}{name}")
                if raw:
                    data = json.loads(raw)
                    # Backward-compat: default governance fields for old records
                    data.setdefault("status", "PRODUCTION")
                    data.setdefault("created_by", "platform")
                    data.setdefault("approved_by", "")
                    # Filter to only known fields
                    import dataclasses
                    known = {f.name for f in dataclasses.fields(AgentDefinition)}
                    data = {k: v for k, v in data.items() if k in known}
                    self._agents[name] = AgentDefinition(**data)
        except Exception as e:
            logger.warning(f"AgentBuilder: Redis load failed → {e}")

    # --------------------------------------------------------
    # HELPERS
    # --------------------------------------------------------

    def _get_or_raise(self, name: str) -> AgentDefinition:
        agent = self._agents.get(name)
        if agent is None:
            raise KeyError(f"Agent {name!r} not found")
        return agent

    def __len__(self) -> int:
        return len(self._agents)

    def __contains__(self, name: str) -> bool:
        return name in self._agents


# ============================================================
# CLAUDE TOOL-USE SCHEMAS
# Maps MCP tool names → Claude input_schema definitions.
# Only action tools (ones that make external calls) are listed here.
# Context tools (retrieve, compliance) are handled pre-LLM.
# ============================================================

_CLAUDE_TOOL_SCHEMAS: Dict[str, Dict] = {
    "jira_create_issue": {
        "name":        "jira_create_issue",
        "description": "Create a Jira issue or ticket. Use when the user wants to log a bug, task, story, or action item.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary":     {"type": "string", "description": "Short one-line issue title"},
                "description": {"type": "string", "description": "Detailed description of the issue"},
                "project":     {"type": "string", "description": "Jira project key. Use SCRUM unless the user specifies a different project. Do NOT infer the project key from the content being discussed."},
                "issue_type":  {"type": "string", "enum": ["Bug", "Task", "Story", "Epic"], "description": "Issue type"},
                "priority":    {"type": "string", "enum": ["Highest", "High", "Medium", "Low", "Lowest"], "description": "Priority level"},
            },
            "required": ["summary", "description"],
        },
    },
    "confluence_create_page": {
        "name":        "confluence_create_page",
        "description": "Create a Confluence documentation page. Use when the user wants to document findings, analysis, or decisions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":     {"type": "string", "description": "Page title"},
                "body":      {"type": "string", "description": "Full page content in markdown format"},
                "space_key": {"type": "string", "description": "Confluence space key, e.g. SD or ENG. Use SD if unsure."},
            },
            "required": ["title", "body"],
        },
    },
    "gitlab_create_or_update_file": {
        "name":        "gitlab_create_or_update_file",
        "description": "Create or update a file in a GitLab repository. Use to push analysis results, reports, or generated code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":    {"type": "string", "description": "Repository in namespace/project format, e.g. ainxt/payment-service"},
                "path":    {"type": "string", "description": "File path within the repo, e.g. reports/analysis.md"},
                "content": {"type": "string", "description": "Full file content as plain text"},
                "message": {"type": "string", "description": "Git commit message describing the change"},
                "branch":  {"type": "string", "description": "Target branch (default: main)"},
            },
            "required": ["repo", "path", "content", "message"],
        },
    },
    "gitlab_create_issue": {
        "name":        "gitlab_create_issue",
        "description": "Create a GitLab issue in a project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string", "description": "Namespace/project"},
                "title": {"type": "string", "description": "Issue title"},
                "body":  {"type": "string", "description": "Issue description"},
            },
            "required": ["repo", "title"],
        },
    },
    "gitlab_create_mr": {
        "name":        "gitlab_create_mr",
        "description": "Create a GitLab merge request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string", "description": "Namespace/project"},
                "title": {"type": "string", "description": "MR title"},
                "head":  {"type": "string", "description": "Source branch"},
                "base":  {"type": "string", "description": "Target branch (default: main)"},
                "body":  {"type": "string", "description": "MR description"},
            },
            "required": ["repo", "title", "head"],
        },
    },
    "execute_code": {
        "name":        "execute_code",
        "description": "Execute Python or bash code in an isolated Docker sandbox and return stdout/stderr.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code":     {"type": "string", "description": "Code to execute"},
                "language": {"type": "string", "enum": ["python", "bash"], "description": "Language (default: python)"},
            },
            "required": ["code"],
        },
    },
    "call_agent": {
        "name":        "call_agent",
        "description": "Delegate a subtask to another specialised agent by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "description": "Name of the target agent"},
                "message":    {"type": "string", "description": "Task or question to send to the agent"},
            },
            "required": ["agent_name", "message"],
        },
    },
    "gitlab_read_file": {
        "name":        "gitlab_read_file",
        "description": "Read a file from a GitLab repository. Use to inspect source code, configs, build files, or test files before making changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string", "description": "Repository in namespace/project format, e.g. ainxt/payment-service"},
                "path":   {"type": "string", "description": "File path within the repo, e.g. src/main/java/App.java or package.json"},
                "branch": {"type": "string", "description": "Branch name to read from (default: main)"},
            },
            "required": ["repo", "path"],
        },
    },
    "gitlab_apply_patch": {
        "name":        "gitlab_apply_patch",
        "description": (
            "Apply a surgical SEARCH/REPLACE patch to an existing file in GitLab. "
            "ALWAYS prefer this over gitlab_create_or_update_file when modifying existing files — "
            "it preserves all unchanged code and avoids accidental deletions. "
            "IMPORTANT: Call gitlab_read_file first and copy the search block verbatim (exact whitespace)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":    {"type": "string", "description": "Repository in namespace/project format, e.g. ainxt/payment-service"},
                "path":    {"type": "string", "description": "File path within the repo, e.g. src/main/java/UpiController.java"},
                "search":  {"type": "string", "description": "Exact existing code block to replace. Copy verbatim from gitlab_read_file — whitespace and indentation must match exactly."},
                "replace": {"type": "string", "description": "New code to substitute in place of the search block"},
                "branch":  {"type": "string", "description": "Branch to read from and commit to (default: main)"},
                "message": {"type": "string", "description": "Git commit message for the patch"},
            },
            "required": ["repo", "path", "search", "replace"],
        },
    },
    "gitlab_search_code": {
        "name":        "gitlab_search_code",
        "description": (
            "Search for a code pattern, symbol, or string across a GitLab repository using blob search. "
            "Use to find existing implementations of similar patterns (e.g. 'validate UPI', 'authenticate user') "
            "before writing new code, so your implementation matches repo conventions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string", "description": "Repository in namespace/project format, e.g. ainxt/payment-service"},
                "query":       {"type": "string", "description": "Code pattern, symbol name, or string to search for"},
                "max_results": {"type": "integer", "description": "Max number of matches to return (default: 10)"},
            },
            "required": ["repo", "query"],
        },
    },
    "gitlab_create_branch": {
        "name":        "gitlab_create_branch",
        "description": "Create a new branch in a GitLab repository from an existing branch. Use before pushing code changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string", "description": "Repository in namespace/project format"},
                "branch":      {"type": "string", "description": "New branch name, e.g. ainxt/JIRA-42-fix-auth"},
                "from_branch": {"type": "string", "description": "Source branch to branch from (default: main)"},
            },
            "required": ["repo", "branch"],
        },
    },
    "jira_get_issue": {
        "name":        "jira_get_issue",
        "description": "Retrieve a Jira issue by its key. Returns summary, description, status, priority, assignee, and linked components.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Jira issue key, e.g. AiNxt-42 or SCRUM-7"},
            },
            "required": ["issue_key"],
        },
    },
    "jira_list_issues": {
        "name":        "jira_list_issues",
        "description": "List Jira issues in a project, optionally filtered by status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Jira project key, e.g. SCRUM or AiNxt"},
                "status":  {"type": "string", "description": "Filter by status: Open, In Progress, Done, etc."},
            },
            "required": ["project"],
        },
    },
    "jira_add_comment": {
        "name":        "jira_add_comment",
        "description": "Add a comment to a Jira issue. Use to post analysis results, code review findings, pipeline status, or implementation notes back to the ticket.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Jira issue key, e.g. AiNxt-42"},
                "comment":   {"type": "string", "description": "Comment text. Supports markdown formatting."},
            },
            "required": ["issue_key", "comment"],
        },
    },
    "confluence_update_page": {
        "name":        "confluence_update_page",
        "description": "Update an existing Confluence page with new content (increments version automatically).",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Confluence page ID (numeric string)"},
                "title":   {"type": "string", "description": "Updated page title"},
                "body":    {"type": "string", "description": "New page content in markdown format"},
            },
            "required": ["page_id", "title", "body"],
        },
    },
    "confluence_get_page": {
        "name":        "confluence_get_page",
        "description": "Retrieve a Confluence page by its ID. Returns title, URL, and content excerpt.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Confluence page ID (numeric string)"},
            },
            "required": ["page_id"],
        },
    },
    "confluence_search": {
        "name":        "confluence_search",
        "description": "Search Confluence pages by keyword. Returns matching pages with titles and URLs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":     {"type": "string", "description": "Search query string"},
                "space_key": {"type": "string", "description": "Confluence space key to narrow search (optional)"},
            },
            "required": ["query"],
        },
    },
}


# ============================================================
# TOOL ALIASES
# Maps legacy / seed-data tool names → canonical MCP registry names.
# Seed agents historically used retrieve_tool / compliance_tool but the
# MCP registry registers them as retrieve / compliance.
# ============================================================

_TOOL_ALIASES: Dict[str, str] = {
    "retrieve_tool":       "retrieve",
    "compliance_tool":     "compliance",
    "generate_answer_tool": "llm_generate",
}


# ============================================================
# AGENT RUNNER  (execution engine)
# ============================================================

class AgentRunner:
    """
    Runs an agent against a user message.

    Execution flow:
      1. Compliance check on user_message (block PAN/CVV/secrets)
      2. Execute each tool listed in AgentDefinition (via MCP registry)
      3. Build combined context from tool outputs
      4. Compliance check on assembled prompt
      5. LLM generate (via model_router — routes by complexity)
      6. Compliance check on LLM answer
      7. Persist run to Redis (via memory_save)
      8. Return AgentRunResult
    """

    # Hard limits — prevent runaway agent executions
    _AGENT_TIMEOUT_SECS  = 120   # wall-clock timeout per agent run
    _MAX_TOOL_ITERATIONS = 10    # max LLM↔tool back-and-forth cycles

    def __init__(self, builder: AgentBuilder):
        self._builder = builder

    def run(
            self,
            agent_name:   str,
            user_message: str,
            session_id:   Optional[str] = None,
            context_json: Optional[str] = None,
            user_id:      Optional[str] = None,
            timeout_secs: Optional[int] = None,
    ) -> AgentRunResult:
        """Execute an agent and return a structured result.

        context_json: optional HandoffContext JSON string.  When provided,
        the receiving agent skips the retrieval step (Step 3) and uses the
        pre-fetched chunks + prior_outputs instead.

        user_id: caller's authenticated user ID — used to scope conversation
        history (stable session_id) and attribute model usage in analytics.

        Enforces _AGENT_TIMEOUT_SECS — raises TimeoutError if exceeded.
        """
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

        run_id    = str(uuid.uuid4())
        started   = datetime.utcnow()
        _timeout  = timeout_secs if timeout_secs is not None else self._AGENT_TIMEOUT_SECS

        def _inner():
            return self._run_inner(agent_name, user_message, session_id, run_id, started,
                                   context_json=context_json, user_id=user_id)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_inner)
            try:
                return future.result(timeout=_timeout)
            except FutureTimeoutError:
                logger.error(
                    f"AgentRunner: {agent_name!r} run {run_id} timed out "
                    f"after {_timeout}s"
                )
                return self._error_result(
                    run_id, agent_name, started,
                    f"Agent run timed out after {_timeout}s",
                )

    def _run_inner(
            self,
            agent_name:   str,
            user_message: str,
            session_id:   Optional[str],
            run_id:       str,
            started:      datetime,
            context_json: Optional[str] = None,
            user_id:      Optional[str] = None,
    ) -> AgentRunResult:
        """Internal execution — called inside a timeout-guarded thread."""
        # Stable per-user-per-agent session: history accumulates across turns.
        # Falls back to run_id only when both session_id and user_id are absent
        # (e.g. internal pipeline calls) — those are ephemeral and expected.
        session_id = session_id or (
            f"agent_{agent_name}_{user_id}" if user_id
            else f"agent_{agent_name}_{run_id[:8]}"
        )

        # Deserialize HandoffContext if provided
        handoff = None
        if context_json:
            try:
                from agents.handoff import HandoffContext
                handoff = HandoffContext.from_json(context_json)
                if handoff.session_id and not session_id.startswith("agent_"):
                    session_id = handoff.session_id
            except Exception as _he:
                logger.debug(f"AgentRunner: could not deserialize HandoffContext → {_he}")
                handoff = None

        # Always refresh from Postgres before running so preferred_model / kb_namespace /
        # system_prompt changes take effect immediately without a server restart.
        # Falls back to the in-memory cache if the DB reload fails.
        try:
            self._builder.reload_from_db(name=agent_name)
        except Exception as _rld_err:
            logger.warning(f"AgentRunner: DB refresh failed for {agent_name!r} — using cached config ({_rld_err})")

        definition = self._builder.get(agent_name)
        if definition is None:
            logger.error(f"AgentRunner: {agent_name!r} not found in builder — run {run_id[:8]}")
            return self._error_result(
                run_id, agent_name, started,
                f"Agent {agent_name!r} not found",
            )

        if not definition.enabled:
            logger.warning(f"AgentRunner: {agent_name!r} is disabled — run {run_id[:8]} aborted")
            return self._error_result(
                run_id, agent_name, started,
                f"Agent {agent_name!r} is disabled",
            )

        if definition.status != "PRODUCTION":
            logger.warning(
                f"AgentRunner: {agent_name!r} status={definition.status!r} — "
                f"run {run_id[:8]} aborted (not PRODUCTION)"
            )
            return self._error_result(
                run_id, agent_name, started,
                f"Agent '{agent_name}' is not in PRODUCTION state "
                f"(current: {definition.status}). "
                "Submit via POST /governance/agents/{name}/submit",
            )

        logger.info(
            f"AgentRunner: starting {agent_name!r} | run={run_id[:8]} | session={session_id} | "
            f"tools={definition.tools} | skills={definition.skills} | "
            f"kb_namespace={definition.kb_namespace!r} | model={definition.preferred_model!r}"
        )

        compliance_flags: List[str] = []
        tool_outputs:     List[Dict[str, Any]] = []

        # ---- Step 1: Compliance check on user input ---------
        input_flags = self._compliance_check(user_message, stage="input")
        compliance_flags.extend(input_flags)
        if input_flags:
            logger.warning(
                f"AgentRunner: {agent_name!r} run {run_id} — "
                f"input compliance flags: {input_flags}"
            )

        # ---- Greeting short-circuit (parity with chat /ask) -----------------
        # A bare greeting ("hi", "hello", "thanks"…) should return a lightweight
        # response WITHOUT running tools, loading history, building the agent
        # system prompt, or entering the tool-use loop — mirroring how chat forces
        # action="generate" for `simple` greetings (orchestrator). Reuses the exact
        # same rule as chat (classifier.GREETING_PATTERN) so both surfaces behave
        # identically. Multi-word inputs ("Hi, build an agent that…") do NOT match
        # fullmatch and fall through to the full workflow (no regression).
        # Placed AFTER the input compliance check so compliance still runs first.
        from models.classifier import GREETING_PATTERN
        if GREETING_PATTERN.fullmatch(user_message.strip()):
            logger.info(
                f"AgentRunner: {agent_name!r} run {run_id[:8]} — greeting short-circuit"
            )
            return self._greeting_result(
                run_id, agent_name, session_id, started,
                user_message, compliance_flags,
            )

        # ---- Step 2: Load conversation history (persistent memory) ----------
        # Inject last 3 turns so the agent remembers prior conversation context.
        conversation_history = []
        try:
            from memory.postgres_memory import PostgresMemory as _PM
            _pm = _PM()
            _recent = _pm.get_conversation(session_id, limit=6)   # 3 turns = 6 messages
            if _recent:
                # Keep last 3 turns (user+assistant pairs); truncate each to 500 chars
                conversation_history = [
                    {"role": m["role"], "content": str(m["content"])[:500]}
                    for m in _recent[-6:]
                ]
            logger.debug(
                f"AgentRunner: {agent_name!r} history loaded — "
                f"{len(conversation_history)} messages from session {session_id!r}"
            )
        except Exception as _mem_err:
            logger.debug(f"AgentRunner: could not load conversation history: {_mem_err}")

        # ---- Step 3: Execute tools (or use handoff context) -------
        if handoff and handoff.retrieved_chunks:
            # Handoff carries pre-fetched context — skip retrieval, run action tools only
            action_tools = [
                t for t in definition.tools
                if t not in ("retrieve", "retrieve_tool", "compliance", "compliance_tool")
            ]
            _skip_def = type("_D", (), {"tools": action_tools, "skills": definition.skills,
                                        "name": definition.name})()
            tool_outputs = self._run_tools(_skip_def, user_message)
            # Prepend handoff context as a synthetic "retrieve" output
            tool_outputs.insert(0, {
                "tool":    "retrieve",
                "output":  handoff.build_context_text(),
                "success": True,
            })
            logger.info(
                f"AgentRunner: {agent_name!r} using HandoffContext "
                f"({len(handoff.retrieved_chunks)} chunks, "
                f"{len(handoff.prior_outputs)} prior outputs)"
            )
        else:
            tool_outputs = self._run_tools(definition, user_message)

        _ok_tools   = [o["tool"] for o in tool_outputs if o.get("success")]
        _fail_tools = [o["tool"] for o in tool_outputs if not o.get("success")]
        logger.info(
            f"AgentRunner: {agent_name!r} tools done — "
            f"ok={_ok_tools} | failed={_fail_tools}"
        )

        # Collector for actual action-tool results from the Claude tool-use loop
        action_tool_outputs: List[Dict[str, Any]] = []

        # ---- Step 4: Build LLM prompt -----------------------
        # Behavioral SOP skills inject into system_prompt; exclude from context
        behavioral_instructions = "\n\n".join(
            o["output"] for o in tool_outputs
            if o.get("tool") == "behavioral_sop" and o.get("success") and o.get("output")
        )
        context_outputs = [o for o in tool_outputs if o.get("tool") != "behavioral_sop"]
        context_text = self._build_context(context_outputs)
        if not context_text.strip():
            logger.warning(
                f"AgentRunner: {agent_name!r} context is EMPTY — "
                f"LLM has no KB data; check that 'retrieve' is in tools and KB docs are activated"
            )
        else:
            logger.debug(
                f"AgentRunner: {agent_name!r} context built — {len(context_text)} chars"
            )
        prompt = self._build_prompt(
            definition.system_prompt, user_message, context_text,
            conversation_history=conversation_history,
            behavioral_instructions=behavioral_instructions,
        )

        # ---- Step 4: Compliance check on assembled prompt ---
        prompt_flags = self._compliance_check(prompt, stage="prompt")
        compliance_flags.extend(prompt_flags)
        if prompt_flags:
            logger.warning(
                f"AgentRunner: {agent_name!r} — prompt flags: {prompt_flags}"
            )
        else:
            logger.debug(f"AgentRunner: {agent_name!r} prompt compliance clean | {len(prompt)} chars")

        # ---- Step 5: LLM generate ---------------------------
        logger.info(f"AgentRunner: {agent_name!r} → LLM generate (preferred_model={definition.preferred_model!r})")
        answer = self._generate(prompt, definition, action_tool_outputs)

        # Merge action tool results (populated by the Claude tool-use loop)
        tool_outputs.extend(action_tool_outputs)
        logger.debug(
            f"AgentRunner: {agent_name!r} answer={len(answer)} chars | "
            f"action_tool_calls={len(action_tool_outputs)}"
        )

        # ---- Step 6: Compliance check on answer -------------
        answer_flags = self._compliance_check(answer, stage="answer")
        compliance_flags.extend(answer_flags)
        if answer_flags:
            answer = "[BLOCKED: compliance violation detected in output]"
            logger.warning(
                f"AgentRunner: {agent_name!r} — answer blocked: {answer_flags}"
            )
        else:
            logger.debug(f"AgentRunner: {agent_name!r} answer compliance clean")

        # ---- Step 7: Persist run record (Redis — fast, stays inline) -----------
        duration_ms = (datetime.utcnow() - started).total_seconds() * 1000
        self._save_run(
            run_id, session_id, agent_name,
            user_message, answer, tool_outputs, compliance_flags,
        )

        # ---- Step 7b: Produce to Kafka — consumer writes conversation + usage to Postgres
        # Mirrors the ainxt.chat_history pattern used for regular chat turns.
        # Falls back to Redis list automatically if broker is unavailable.
        try:
            from core.kafka_producer import produce as _kproduce, TOPIC_AGENT_EVENTS as _TOPIC_AGENT_EVENTS
            from models.model_router import model_router as _mr
            from core.model_registry import OPENAI_CODING_MODEL as _ocm
            _model_label = getattr(_mr, "last_model_label", _ocm)
            _in_tok  = int(len(user_message.split()) * 1.3)
            _out_tok = int(len(answer.split()) * 1.3)
            _kproduce(_TOPIC_AGENT_EVENTS, {
                "event":        "conversation_turn",
                "session_id":   session_id,
                "agent_name":   agent_name,
                "run_id":       run_id,
                "user_id":      user_id,
                "user_message": user_message,
                "answer":       answer if "[BLOCKED" not in answer else None,
                "blocked":      "[BLOCKED" in answer,
                "model":        _model_label,
                "in_tok":       _in_tok,
                "out_tok":      _out_tok,
                "cost_usd":     _estimate_usage_cost(_model_label, _in_tok, _out_tok),
                "duration_ms":  round(duration_ms, 1),
            }, key=session_id)
        except Exception as _kpe:
            logger.debug(f"AgentRunner: Kafka produce failed: {_kpe}")

        # ---- Step 7c: Inbox notification (Redis pub/sub — fast, stays inline) --
        try:
            from store.inbox_store import publish_inbox_item as _pub_inbox
            _blocked = "[BLOCKED" in answer
            _pub_inbox(
                user_id="platform",
                type="agent_failure" if _blocked else "agent_completion",
                title=f"Agent '{agent_name}' {'blocked by compliance' if _blocked else 'completed'}",
                body=answer[:300] if not _blocked else answer,
                source_id=run_id,
                metadata={"agent_name": agent_name, "run_id": run_id,
                          "duration_ms": round(duration_ms, 1)},
            )
        except Exception:
            pass
        # ---- Step 7d: Self-improving skill loop — record successful run -------
        # Gated + disabled by default; agent_run is NOT in the v1 source set.
        # The user message is REDACTED before it touches the loop store.
        try:
            from core.config import ENABLE_SKILL_LOOP, SKILL_LOOP_SOURCES
            if (ENABLE_SKILL_LOOP and "agent_run" in SKILL_LOOP_SOURCES
                    and "[BLOCKED" not in answer):
                from store.skill_loop_store import record_run_signature
                _safe_msg = user_message
                try:
                    from agents.compliance_engine import compliance_engine as _ce
                    _safe_msg = _ce.validate_input(user_message).get("redacted_text") or user_message
                except Exception:
                    pass
                record_run_signature(
                    "agent_run", agent_name, _safe_msg,
                    [o.get("tool") for o in tool_outputs if o.get("tool")],
                    department=getattr(definition, "department", "") or "",
                )
        except Exception as _sl_err:
            logger.debug(f"AgentRunner: skill-loop capture skipped: {_sl_err}")

        logger.info(
            f"AgentRunner: {agent_name!r} run {run_id} complete "
            f"in {duration_ms:.1f}ms success=True"
        )
        return AgentRunResult(
            agent_name=agent_name,
            run_id=run_id,
            success=True,
            answer=answer,
            tool_outputs=tool_outputs,
            compliance_flags=compliance_flags,
            duration_ms=duration_ms,
            started_at=started.isoformat(),
        )

    # --------------------------------------------------------
    # TOOL EXECUTION
    # --------------------------------------------------------

    # Tools that can be called pre-LLM with just the user query as context gathering.
    # Action tools (jira, gitlab, confluence, execute_code, call_agent, etc.) need
    # LLM-structured parameters and must NOT be blindly called with a raw question.
    _CONTEXT_TOOLS = {"retrieve", "compliance", "memory_get", "memory_recall"}

    def _run_tools(
            self,
            definition: AgentDefinition,
            user_message: str,
    ) -> List[Dict[str, Any]]:
        """Call each context-gathering tool pre-LLM via the MCP registry.

        Only tools in _CONTEXT_TOOLS are executed here — they accept a plain
        query string and return text that enriches the LLM prompt.

        Action tools (execute_code, call_agent, jira_*, gitlab_*, confluence_*)
        require structured parameters decided by the LLM and are therefore
        skipped at this stage; Claude calls them via the tool-use loop.

        Tool aliases (retrieve_tool → retrieve, etc.) are resolved before lookup.
        """
        from mcp.registry import mcp_registry  # lazy import

        _ctx_tools    = [_TOOL_ALIASES.get(t, t) for t in definition.tools if _TOOL_ALIASES.get(t, t) in self._CONTEXT_TOOLS]
        _deferred     = [_TOOL_ALIASES.get(t, t) for t in definition.tools if _TOOL_ALIASES.get(t, t) not in self._CONTEXT_TOOLS]
        logger.debug(
            f"AgentRunner: _run_tools {definition.name!r} — "
            f"pre-LLM context_tools={_ctx_tools} | deferred action_tools={_deferred}"
        )

        outputs: List[Dict[str, Any]] = []
        for tool_name in definition.tools:
            # Resolve legacy/alias names to canonical MCP names
            canonical = _TOOL_ALIASES.get(tool_name, tool_name)
            if canonical not in self._CONTEXT_TOOLS:
                # Unknown tool: inspect MCP registry to decide if it's context-style.
                # A tool whose only required param is "query" can be pre-executed here.
                # Tools with richer structured params are action tools — defer to the
                # Claude tool-use loop so the LLM can supply the correct arguments.
                try:
                    from mcp.registry import mcp_registry as _reg
                    _tdef = _reg.tools.get(canonical)
                    _required = set((_tdef.input_schema or {}).get("required", [])) if _tdef else set()
                    _is_context = _tdef is not None and bool(_required) and _required <= {"query"}
                except Exception:
                    _is_context = False
                if not _is_context:
                    logger.debug(f"AgentRunner: skipping action tool {canonical!r} (deferred to LLM tool-use loop)")
                    continue

            try:
                tool_kwargs: Dict[str, Any] = {"query": user_message}
                if canonical == "retrieve":
                    # Normalize agent name: spaces/hyphens → underscores, lowercase.
                    # Must match the normalization applied at activate_doc time so
                    # queried repo == stored repo even for display names with spaces.
                    _agent_ns = (
                        f"agent_kb:{definition.name.strip().lower().replace(' ', '_').replace('-', '_')}"
                    )
                    _repo = definition.kb_namespace or _agent_ns
                    tool_kwargs["repo_filter"] = _repo
                    logger.info(f"AgentRunner: retrieve scope → {_repo!r}")
                result = mcp_registry.execute_tool(canonical, **tool_kwargs)
                outputs.append({
                    "tool":    canonical,
                    "success": result.success,
                    "output":  result.output,
                    "error":   result.error,
                })
                logger.info(
                    f"AgentRunner: tool {canonical!r} (alias: {tool_name!r}) → success={result.success}"
                )
            except Exception as e:
                logger.error(f"AgentRunner: tool {canonical!r} raised {e}")
                outputs.append({
                    "tool":    canonical,
                    "success": False,
                    "output":  None,
                    "error":   str(e),
                })

        # Execute skills registered on this agent
        if definition.skills:
            outputs.extend(self._run_skills(definition.skills, user_message))

        return outputs

    def _run_skills(
            self,
            skill_names: List[str],
            user_message: str,
    ) -> List[Dict[str, Any]]:
        """Execute each skill by loading its code from the SkillRecord DB table.

        Skill code must define a `run(input: str) -> dict` function.
        Non-PRODUCTION skills and skills with errors are skipped gracefully.
        """
        import inspect
        outputs: List[Dict[str, Any]] = []
        try:
            from db.database import SessionLocal
            from db.models import SkillRecord
            db = SessionLocal()
            try:
                for skill_name in skill_names:
                    rec = db.query(SkillRecord).filter(
                        SkillRecord.name == skill_name,
                        SkillRecord.is_production == True,  # noqa: E712
                    ).first()
                    if not rec:
                        # Try to auto-generate the skill from its name
                        rec = self._autogenerate_skill(db, skill_name)
                        if not rec:
                            logger.warning(
                                f"AgentRunner: skill {skill_name!r} not found — skipped"
                            )
                            continue
                    skill_type = getattr(rec, "skill_type", "execution") or "execution"

                    if skill_type == "behavioral":
                        # Behavioral skills: plain-text SOP injected into system_prompt
                        instructions = (rec.code or rec.description or "").strip()
                        if instructions:
                            outputs.append({
                                "tool":    "behavioral_sop",
                                "success": True,
                                "output":  instructions,
                                "error":   None,
                            })
                            logger.info(f"AgentRunner: behavioral skill {skill_name!r} queued for system_prompt injection")
                        continue

                    try:
                        ns: Dict[str, Any] = {}
                        exec(rec.code, ns)  # noqa: S102
                        run_fn = ns.get("run")
                        if not callable(run_fn):
                            logger.warning(
                                f"AgentRunner: skill {skill_name!r} has no run() function"
                            )
                            continue
                        # Call run() — pass user_message as first positional arg if accepted
                        sig = inspect.signature(run_fn)
                        params = list(sig.parameters.keys())
                        result = run_fn(user_message) if params else run_fn()
                        outputs.append({
                            "tool":    f"skill:{skill_name}",
                            "success": True,
                            "output":  json.dumps(result) if isinstance(result, dict) else str(result),
                            "error":   None,
                        })
                        logger.info(f"AgentRunner: skill {skill_name!r} executed")
                    except Exception as e:
                        logger.warning(f"AgentRunner: skill {skill_name!r} exec failed → {e}")
                        outputs.append({
                            "tool":    f"skill:{skill_name}",
                            "success": False,
                            "output":  None,
                            "error":   str(e),
                        })
            finally:
                db.close()
        except Exception as e:
            logger.error(f"AgentRunner: skill execution setup failed → {e}")
        return outputs

    def _autogenerate_skill(self, db, skill_name: str):
        """Generate a skill's Python code from its name using the LLM, save as PRODUCTION.
        Called when an agent references a skill that doesn't exist yet.
        Returns the SkillRecord on success, None on failure."""
        try:
            from models.model_router import model_router
            from db.models import SkillRecord
            human_name = skill_name.replace("_", " ").replace("-", " ")
            prompt = (
                f"You are an AiNxt AI Platform skill generator.\n"
                f"Generate a Python skill function for: '{human_name}'.\n\n"
                f"Rules:\n"
                f"- Define EXACTLY one function: def run(input: str) -> dict\n"
                f"- The function receives a user request as a string and returns a dict with at least an 'output' key\n"
                f"- Use only Python standard library (no external imports except json, re, datetime)\n"
                f"- Make it genuinely useful — infer what '{human_name}' should do from the name\n"
                f"- Return ONLY the Python code, no explanation\n\n"
                f"Example for 'incident_triage':\n"
                f"```python\n"
                f"import re\n"
                f"def run(input: str) -> dict:\n"
                f"    severity = 'P1' if any(w in input.lower() for w in ['critical','down','outage']) else 'P2' if 'degraded' in input.lower() else 'P3'\n"
                f"    return {{'output': f'Severity: {{severity}}. Immediate action required for P1/P2.', 'severity': severity}}\n"
                f"```\n\n"
                f"Now generate for: '{human_name}'"
            )
            raw = model_router.generate(prompt, model_hint="gpt")
            # Extract code block
            import re as _re
            match = _re.search(r"```python\s*(.*?)```", raw, _re.DOTALL)
            code = match.group(1).strip() if match else raw.strip()
            if not code or "def run(" not in code:
                return None
            rec = SkillRecord(
                name=skill_name,
                description=f"Auto-generated skill for: {human_name}",
                code=code,
                tools=[],
                tags=["auto-generated"],
                status="PRODUCTION",
                created_by="platform-autogen",
                is_production=True,
                visibility="public",
            )
            db.add(rec)
            db.commit()
            db.refresh(rec)
            logger.info(f"AgentRunner: auto-generated skill {skill_name!r}")
            return rec
        except Exception as e:
            logger.warning(f"AgentRunner: _autogenerate_skill {skill_name!r} failed → {e}")
            return None

    # --------------------------------------------------------
    # CONTEXT BUILDING
    # --------------------------------------------------------

    def _build_context(self, tool_outputs: List[Dict[str, Any]]) -> str:
        """Flatten successful tool outputs into a single context string."""
        parts: List[str] = []
        for out in tool_outputs:
            if out["success"] and out["output"] is not None:
                raw = out["output"]
                # Handle various output shapes
                if isinstance(raw, str):
                    parts.append(f"[{out['tool']}]\n{raw}")
                elif isinstance(raw, list):
                    text = "\n".join(str(item) for item in raw)
                    parts.append(f"[{out['tool']}]\n{text}")
                elif isinstance(raw, dict):
                    parts.append(f"[{out['tool']}]\n{json.dumps(raw, indent=2)}")
                else:
                    parts.append(f"[{out['tool']}]\n{raw}")
        return "\n\n".join(parts)

    def _build_prompt(
            self,
            system_prompt: str,
            user_message: str,
            context: str,
            conversation_history: list = None,
            behavioral_instructions: str = "",
    ) -> str:
        if behavioral_instructions:
            system_section = (system_prompt.strip()
                              + "\n\n## Domain Rules & SOPs\n"
                              + behavioral_instructions)
        else:
            system_section = system_prompt.strip()
        parts = [system_section]
        if conversation_history:
            history_text = "\n".join(
                f"{m['role'].upper()}: {m['content']}"
                for m in conversation_history
            )
            parts.append(f"## Conversation History (last {len(conversation_history)} messages)\n{history_text}")
        if context:
            parts.append(f"## Context\n{context}")
        parts.append(f"## User Request\n{user_message}")
        return "\n\n".join(parts)

    # --------------------------------------------------------
    # LLM GENERATION
    # --------------------------------------------------------

    def _generate(
            self,
            prompt: str,
            definition: AgentDefinition,
            action_tool_outputs: List[Dict[str, Any]] = None,
    ) -> str:
        """Generate LLM response.

        If the agent has action tools (jira, gitlab, confluence, execute_code,
        call_agent) use Claude's tool-use API so Claude can call them with
        structured parameters.  Otherwise fall back to the standard model_router.

        action_tool_outputs: mutable list; actual tool results are appended here
        by the tool executor so run() can surface them in the final AgentRunResult.
        """
        # Build action tool list — hardcoded schemas first, then dynamic lookup
        # from the MCP registry for any tool not known at write-time.
        action_tools: List[str] = []
        _extra_schemas: Dict[str, Dict] = {}
        for _t in definition.tools:
            _canon = _TOOL_ALIASES.get(_t, _t)
            if _canon in _CLAUDE_TOOL_SCHEMAS:
                action_tools.append(_canon)
            elif _canon not in self._CONTEXT_TOOLS:
                # Not a known platform tool — build Claude schema from MCP registry.
                try:
                    from mcp.registry import mcp_registry as _reg
                    _tdef = _reg.tools.get(_canon)
                    if _tdef and _tdef.enabled:
                        _extra_schemas[_canon] = {
                            "name":         _canon,
                            "description":  _tdef.description or f"Execute {_canon}",
                            "input_schema": _tdef.input_schema or {
                                "type": "object", "properties": {}, "required": []
                            },
                        }
                        action_tools.append(_canon)
                        logger.info(
                            f"AgentRunner: {definition.name!r} — "
                            f"dynamic schema loaded for {_canon!r}"
                        )
                except Exception as _dyn_err:
                    logger.debug(
                        f"AgentRunner: dynamic schema lookup failed for {_canon!r}: {_dyn_err}"
                    )

        if action_tools:
            logger.info(
                f"AgentRunner: {definition.name!r} LLM path=tool-use | action_tools={action_tools}"
            )
            return self._generate_with_tool_use(
                prompt, definition, action_tools, action_tool_outputs,
                extra_schemas=_extra_schemas,
            )

        from models.model_router import model_router
        _model_hint = definition.preferred_model if definition.preferred_model and definition.preferred_model != "auto" else None
        logger.info(
            f"AgentRunner: {definition.name!r} LLM path=standard | model_hint={_model_hint!r}"
        )
        _output = None
        _last_err = None
        # Primary attempt with agent's preferred model (or router default)
        try:
            _output = model_router.generate(prompt, model_hint=_model_hint)
        except Exception as e:
            _last_err = e
            logger.warning(
                f"AgentRunner: {definition.name!r} preferred model {_model_hint!r} failed ({e}) "
                "— retrying with router default"
            )
        # Fallback: let model_router choose freely (only runs when preferred model failed)
        if _output is None and _model_hint is not None:
            try:
                _output = model_router.generate(prompt, model_hint=None)
                logger.info(f"AgentRunner: {definition.name!r} fallback generation succeeded")
            except Exception as e2:
                _last_err = e2
                logger.error(f"AgentRunner: fallback generation also failed ({e2})")
        if _output is None:
            return f"[ERROR: LLM generation failed — {_last_err}]"
        # ── Eval: all agents covered dynamically (fire-and-forget) ────────
        try:
            from core.evals import eval_engine as _ee
            _ee.eval_answer_quality(
                prompt[:400], _output or "", [],
                platform="agent_studio",
                model=getattr(model_router, "last_model_label", None) or None,
            )
        except Exception:
            pass
        return _output

    def _generate_with_tool_use(
        self,
        prompt: str,
        definition: AgentDefinition,
        action_tool_names: List[str],
        action_tool_outputs: List[Dict[str, Any]] = None,
        extra_schemas: Dict[str, Dict] = None,
    ) -> str:
        """Drive Claude's tool-use loop for agents that have action tools."""
        try:
            from gateway_claude import claude_gateway

            # Merge hardcoded platform schemas with any dynamically resolved ones.
            _schema_map = {**_CLAUDE_TOOL_SCHEMAS, **(extra_schemas or {})}
            tool_schemas = [_schema_map[t] for t in action_tool_names if t in _schema_map]

            # Split the assembled prompt back into system + user parts.
            # _build_prompt() format: system_prompt \n\n## Context\n...\n\n## User Request\n...
            system_prompt = definition.system_prompt.strip()
            context = ""
            user_message = prompt

            # Extract ## Context and ## User Request sections if present
            if "## Context\n" in prompt and "## User Request\n" in prompt:
                parts = prompt.split("## User Request\n", 1)
                user_message = parts[1].strip() if len(parts) > 1 else prompt
                ctx_section = parts[0]
                if "## Context\n" in ctx_section:
                    context = ctx_section.split("## Context\n", 1)[1].strip()

            logger.info(
                f"AgentRunner: tool-use path — "
                f"tools={action_tool_names}"
            )

            _tool_output = claude_gateway.generate_with_tools(
                system_prompt=system_prompt,
                user_message=user_message,
                context=context,
                tools=tool_schemas,
                tool_executor=self._make_tool_executor(action_tool_outputs),
                max_tool_rounds=self._MAX_TOOL_ITERATIONS,
            )
            # ── Eval: tool-use agent response (fire-and-forget) ───────────────
            try:
                from core.evals import eval_engine as _ee
                from models.model_router import model_router as _mr_tool
                _ee.eval_answer_quality(
                    user_message or prompt[:400], _tool_output or "", [],
                    platform="agent_studio",
                    model=getattr(_mr_tool, "last_model_label", None) or None,
                )
            except Exception:
                pass
            return _tool_output
        except Exception as e:
            logger.exception(f"AgentRunner: tool-use generate failed → {e}")
            # Fallback to standard generation
            from models.model_router import model_router
            return model_router.generate(prompt)

    def _make_tool_executor(self, outputs: List[Dict[str, Any]] = None):
        """Return a callable(tool_name, inputs_dict) -> str that executes via MCP registry.

        outputs: mutable list; each tool call result is appended so run() can
        surface it in AgentRunResult.tool_outputs shown in the UI.
        """
        from mcp.registry import mcp_registry

        def execute(name: str, inputs: dict) -> str:
            result = mcp_registry.execute_tool(name, **inputs)
            if outputs is not None:
                outputs.append({
                    "tool":    name,
                    "success": result.success,
                    "output":  str(result.output) if result.output is not None else None,
                    "error":   result.error,
                })
            if result.success:
                return str(result.output or "Done")
            return f"Error: {result.error}"

        return execute

    # --------------------------------------------------------
    # COMPLIANCE
    # --------------------------------------------------------

    def _compliance_check(self, text: str, stage: str) -> List[str]:
        """Return only the finding types that are in BLOCKING_TYPES.

        Non-blocking findings (EMAIL, MOBILE detected in jPOS code) are logged
        but NOT included in the returned list so they don't trigger a block.
        """
        try:
            result = compliance_engine.validate_input(text)
            # Only surface findings that actually trigger a block
            blocking_flags = [
                f["type"] for f in result.get("findings", [])
                if f.get("blocked", False)
            ]
            all_types = [f["type"] for f in result.get("findings", [])]
            non_blocking = [t for t in all_types if t not in blocking_flags]
            if non_blocking:
                logger.info(
                    f"AgentRunner: compliance non-blocking at {stage}: {non_blocking}"
                )
            if blocking_flags:
                logger.warning(
                    f"AgentRunner: compliance BLOCKING flags at {stage}: {blocking_flags}"
                )
            return blocking_flags
        except Exception as e:
            logger.error(f"AgentRunner: compliance check error at {stage}: {e}")
            return []

    # --------------------------------------------------------
    # REDIS PERSISTENCE
    # --------------------------------------------------------

    def _save_run(
            self,
            run_id: str,
            session_id: str,
            agent_name: str,
            question: str,
            answer: str,
            tool_outputs: List[Dict],
            compliance_flags: List[str],
    ) -> None:
        try:
            from memory.redis_memory import RedisMemory
            mem = RedisMemory()
            mem.save_agent_run(
                run_id=run_id,
                agent_name=agent_name,
                question=question,
                answer=answer,
                tool_history=[o["tool"] for o in tool_outputs],
                compliance_flags=compliance_flags,
                metadata={"session_id": session_id},
            )
        except Exception as e:
            logger.warning(f"AgentRunner: could not persist run to Redis → {e}")

    # --------------------------------------------------------
    # HELPERS
    # --------------------------------------------------------

    @staticmethod
    def _error_result(
            run_id: str,
            agent_name: str,
            started: datetime,
            error: str,
    ) -> AgentRunResult:
        duration_ms = (datetime.utcnow() - started).total_seconds() * 1000
        logger.error(f"AgentRunner: {error}")
        return AgentRunResult(
            agent_name=agent_name,
            run_id=run_id,
            success=False,
            answer="",
            tool_outputs=[],
            compliance_flags=[],
            duration_ms=duration_ms,
            started_at=started.isoformat(),
            error=error,
        )

    def _greeting_result(
            self,
            run_id: str,
            agent_name: str,
            session_id: Optional[str],
            started: datetime,
            user_message: str,
            compliance_flags: List[str],
    ) -> AgentRunResult:
        """Build a lightweight success result for a bare greeting.

        Mirrors chat's greeting behaviour: a quick Local-LLM (simple tier)
        response with a safe canned fallback if the model call fails. Unlike
        ``_error_result`` this is an instance method because it persists the run
        via ``self._save_run``. Never raises — persistence failures are logged
        and swallowed so a greeting always returns a usable result.
        """
        try:
            from models.model_router import model_router
            answer = model_router.generate(user_message, model="simple")
        except Exception as e:
            logger.warning(f"AgentRunner: greeting generate failed → {e}")
            answer = ""
        if not answer or not answer.strip():
            answer = "Hi! What would you like to build today?"

        duration_ms = (datetime.utcnow() - started).total_seconds() * 1000
        try:
            self._save_run(
                run_id, session_id, agent_name,
                user_message, answer, [], compliance_flags,
            )
        except Exception as e:
            logger.warning(f"AgentRunner: greeting _save_run failed → {e}")

        # Eval: grade greeting responses too (fire-and-forget)
        try:
            from core.evals import eval_engine as _ee
            from models.model_router import model_router as _mr_ab_gr
            _ee.eval_answer_quality(
                user_message, answer, [],
                session_id=session_id,
                run_id=run_id,
                platform="agent_studio",
                model=getattr(_mr_ab_gr, "last_model_label", None) or None,
            )
        except Exception:
            pass

        return AgentRunResult(
            agent_name=agent_name,
            run_id=run_id,
            success=True,
            answer=answer,
            tool_outputs=[],
            compliance_flags=compliance_flags,
            duration_ms=duration_ms,
            started_at=started.isoformat(),
        )


# ============================================================
# BUILT-IN PLATFORM AGENTS
# ============================================================

def _bootstrap_platform_agents(builder: AgentBuilder) -> None:
    """Seed the 5 built-in platform agents to Postgres on first boot.

    Uses _pg_on_conflict='ignore' — if an admin has already customised the
    system_prompt in Postgres, their version is kept.  The hardcoded prompts
    here are only the initial defaults, not a runtime override.

    The in-memory cache is populated from Postgres at startup (before this
    function runs), so agents already in DB will already be in builder._agents.
    We still call builder.create() for any that are missing (e.g. first boot).
    """

    _defaults = [
        AgentDefinition(
            name="question_answerer",
            status="PRODUCTION",
            description="Retrieve relevant codebase context and answer engineering questions accurately.",
            system_prompt=(
                "You are an expert AiNxt platform engineer. "
                "Retrieve relevant context from the platform knowledge base (pgvector HNSW + BM25) before answering. "
                "Ground every answer in retrieved documentation — never guess at AiNxt-specific rules or standards. "
                "If no relevant context is found, say so clearly rather than fabricating an answer. "
                "Never reveal PAN, CVV, API keys, secrets, or any PCI-sensitive data."
            ),
            tools=["retrieve", "compliance"],
            skills=["answer_question"],
            tags=["qa", "retrieval"],
        ),
        AgentDefinition(
            name="bug_fixer",
            status="PRODUCTION",
            description="Analyse a bug, generate a minimal fix, execute it in Docker sandbox, and verify it passes.",
            system_prompt=(
                "You are an autonomous bug-fixing agent for AiNxt engineering teams. "
                "Step 1: Retrieve the relevant source file(s) and understand the existing code structure. "
                "Step 2: Identify the root cause — be precise, name the file and line. "
                "Step 3: Generate the minimal fix — no refactoring beyond the bug scope. "
                "Step 4: Run compliance check on the generated code before execution. "
                "Step 5: Execute the fix in the Docker sandbox and confirm tests pass. "
                "Never expose PAN, CVV, API keys, or secrets in generated code."
            ),
            tools=["retrieve", "execute_and_heal"],
            skills=["fix_bug"],
            tags=["engineering", "debugging"],
        ),
        AgentDefinition(
            name="code_generator",
            status="PRODUCTION",
            description="Generate production-grade code for a given AiNxt specification.",
            system_prompt=(
                "You are a senior AiNxt software engineer. "
                "Before generating code, retrieve relevant examples and patterns from the knowledge base. "
                "Generate clean, well-commented, production-ready code that matches the target repository's language and conventions. "
                "Enforce PCI compliance: never include hardcoded secrets, raw PAN, CVV, Aadhaar, or UPI VPA in generated code. "
                "Always include appropriate error handling, logging, and unit tests."
            ),
            tools=["retrieve", "compliance"],
            skills=["generate_code"],
            tags=["engineering", "code-generation"],
        ),
        AgentDefinition(
            name="code_reviewer",
            status="PRODUCTION",
            description="Review code for bugs, security vulnerabilities, and PCI/DSS compliance violations.",
            system_prompt=(
                "You are a senior AiNxt security-focused code reviewer with deep knowledge of PCI DSS and OWASP Top 10. "
                "Retrieve relevant AiNxt coding standards from the knowledge base before reviewing. "
                "Review for: (1) Security vulnerabilities — injection, XSS, insecure crypto, missing auth. "
                "(2) PCI/DSS violations — exposed PAN, CVV, Aadhaar, API keys in code or comments. "
                "(3) AiNxt standards — audit columns on financial tables, soft deletes, idempotent payment APIs. "
                "(4) Code quality — dead code, missing error handling, N+1 queries, memory leaks. "
                "Cite exact file names and line numbers for every finding. "
                "Distinguish blocking (must fix before merge) vs non-blocking (suggestions)."
            ),
            tools=["retrieve", "compliance"],
            skills=["code_review"],
            tags=["engineering", "review", "security"],
        ),
        AgentDefinition(
            name="incident_responder",
            status="PRODUCTION",
            description="Analyse an incident, identify root cause, and propose a concrete remediation plan.",
            system_prompt=(
                "You are an AiNxt SRE incident response agent. "
                "Retrieve relevant runbooks, past incidents, and architecture docs from the knowledge base. "
                "Analyse the provided incident details: error messages, stack traces, metrics, and logs. "
                "Identify the root cause precisely — name the service, endpoint, and failure mode. "
                "Propose a concrete remediation plan with: immediate mitigation steps, root-cause fix, "
                "and post-incident review items. "
                "Flag any PCI/payment data exposure risks immediately."
            ),
            tools=["retrieve", "compliance"],
            skills=["incident_response"],
            tags=["sre", "incident"],
        ),
    ]

    for defn in _defaults:
        # Only seed to Postgres if not already there (admin customizations are preserved).
        # The in-memory cache is already populated from Postgres at this point, so
        # agents already in DB will already be in builder._agents — we skip them.
        if defn.name not in builder._agents:
            builder.create(defn, _pg_on_conflict="ignore")
            logger.info(f"_bootstrap_platform_agents: seeded {defn.name!r} (first boot)")


# ============================================================
# SINGLETONS
# ============================================================

agent_builder = AgentBuilder()
_bootstrap_platform_agents(agent_builder)

agent_runner = AgentRunner(agent_builder)
