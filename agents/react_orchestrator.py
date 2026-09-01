# SPDX-License-Identifier: Apache-2.0
# ============================================================
# ReAct ORCHESTRATOR — Think → Act → Observe → Repeat
# ============================================================
# Uses gateway_claude.generate_with_tools() (Claude native tool-use)
# so Claude decides WHICH tools to call and WHEN it's done.
# The existing Orchestrator stays for pure Q&A; this handles
# action-mode requests (fix, create, update, find bug, etc.)
# ============================================================

import json
import re
from typing import Optional, Generator, Dict, Any, List

import time as _time

from core.logger import logger
from core.telemetry import tracer as _tracer

# ============================================================
# TASK-MODE DETECTION
# ============================================================

_TASK_VERBS = re.compile(
    r"\b(fix|create|add|update|write|implement|generate|build|deploy|"
    r"refactor|migrate|delete|remove|find\s+(?:the\s+)?bug|debug|"
    r"make\s+a\s+pr|open\s+a\s+(?:pr|mr|ticket)|"
    r"raise\s+(?:a\s+)?(?:jira|ticket|issue)|"
    r"index|analyse|analyze|review|test|run|execute)\b",
    re.IGNORECASE,
)
_TASK_REFS = re.compile(
    r"(?:in\s+(?:file|repo|class|method|function)\s+\S+|"
    r"jira\s+(?:ticket\s+)?[A-Z]+-\d+|"
    r"gitlab|merge\s+request|pull\s+request|branch\s+\w+)",
    re.IGNORECASE,
)
_DIAG_RE = re.compile(
    r"\b(why\s+is|why\s+does|why\s+did|what\s+is\s+wrong|what\s+went\s+wrong|"
    r"what\s+caused|how\s+does\s+.{0,30}\s+work|where\s+is\s+.{0,30}\s+defined|"
    r"show\s+me|find\s+(all|where|the)|trace\s+(the|through)|"
    r"walk\s+me\s+through|explain\s+the\s+(flow|code|logic|bug)|"
    r"what\s+does\s+\S+\s+(do|return|mean|handle)|how\s+is\s+\S+\s+(implemented|called|used)|"
    r"can\s+you\s+explain|describe\s+the)\b",
    re.IGNORECASE,
)
_CAMEL_RE = re.compile(r"\b[A-Z][a-z]+[A-Z][a-zA-Z]+\b")
_SNAKE_RE = re.compile(r"\b[a-z]{2,}(?:_[a-z0-9]{2,}){2,}\b")  # 3+ snake_case segments
_FILE_EXT_RE = re.compile(
    r"\b\w[\w/.-]+\.(?:java|py|go|kt|scala|js|ts|yaml|yml|xml|sql|sh|json|properties|gradle|maven)\b",
    re.IGNORECASE,
)
_PKG_PATH_RE = re.compile(
    r"\b(?:com|org|io|net|in)\.[a-z]{2,}\.[a-zA-Z]",  # com.ainxt.*, org.springframework.*
)
_ERROR_RE = re.compile(
    r"\b(NullPointerException|ClassNotFoundException|StackOverflowError|OutOfMemoryError|"
    r"IllegalArgumentException|IllegalStateException|IndexOutOfBoundsException|"
    r"ConcurrentModificationException|timeout|deadlock|OOM|stacktrace|stack.trace|"
    r"HTTP\s+(?:4\d\d|5\d\d)|error\s+code\s+\d{3,}|exception\s+in\s+thread)\b",
    re.IGNORECASE,
)
_ENV_RE = re.compile(
    r"\b(in\s+(?:prod|production|staging|uat|preprod|dev|sandbox)|"
    r"(?:prod|staging|uat)\s+(?:env|environment|cluster|server|instance))\b",
    re.IGNORECASE,
)

# ── Query risk classifier ─────────────────────────────────────────────────
_HIGH_RISK_RE = re.compile(
    r"\b(settlement|clearing|reconcil|payment.*amount|transfer.*fund|"
    r"pan.*number|cvv|aadhaar|pin.*block|otp.*generat|"
    r"prod(?:uction)?\s+(?:deploy|rollback|hotfix|incident)|"
    r"drop\s+(?:table|db|database)|delete\s+(?:all|from\s+\w+)|"
    r"compliance\s+(?:violation|breach|audit\s+fail)|pci\s+(?:breach|fail)|"
    r"security\s+vuln|data\s+(?:leak|breach|loss))\b",
    re.IGNORECASE,
)

def _classify_query_risk(goal: str) -> str:
    """Return 'HIGH', 'MEDIUM', or 'LOW' risk for this goal."""
    if _HIGH_RISK_RE.search(goal):
        return "HIGH"
    if is_task_request(goal):
        return "MEDIUM"
    return "LOW"


def _classify_critique_severity(critique: str) -> str:
    """
    Classify adversarial critique as HIGH / MEDIUM / LOW / NONE.
    HIGH → force answer regeneration.
    """
    if not critique or "NO_ISSUES" in critique.upper()[:20]:
        return "NONE"
    c = critique.lower()
    high_signals = [
        "incorrect", "wrong", "hallucin", "factually", "fabricat",
        "not true", "false claim", "does not exist", "never happens",
        "contradicts", "opposite", "no such",
    ]
    medium_signals = [
        "incomplete", "missing", "partial", "unclear",
        "ambiguous", "could be wrong", "unverified",
    ]
    if any(s in c for s in high_signals):
        return "HIGH"
    if any(s in c for s in medium_signals):
        return "MEDIUM"
    return "LOW"


def is_task_request(question: str) -> bool:
    """
    Return True when the question looks like an action request vs pure Q&A.
    Errs on the side of ReAct — it's better to run the full agent loop
    than to answer a task question from stale training data.
    """
    q = question.strip()

    # Explicit imperative verbs
    if _TASK_VERBS.search(q):   return True
    # Direct references to external systems or file paths
    if _TASK_REFS.search(q):    return True
    # Diagnostic / explanatory questions about named things
    if _DIAG_RE.search(q):      return True
    # Any CamelCase entity reference (class name, method name) — likely codebase question
    if _CAMEL_RE.search(q):     return True
    # Any Jira key pattern
    if re.search(r"\b[A-Z]{2,8}-\d{1,6}\b", q): return True
    # snake_case identifiers (method/variable names like process_upi_payment)
    if _SNAKE_RE.search(q):     return True
    # File extension references (.java, .py, .go, etc.)
    if _FILE_EXT_RE.search(q):  return True
    # Java/Go/Kotlin package path patterns (com.ainxt.*, org.springframework.*)
    if _PKG_PATH_RE.search(q):  return True
    # Error/exception name patterns and HTTP error codes
    if _ERROR_RE.search(q):     return True
    # Environment-specific references (prod, staging, uat, preprod)
    if _ENV_RE.search(q):       return True

    return False


# ============================================================
# TOOL SCHEMAS — Anthropic tool-use format
# ============================================================

TOOL_SCHEMAS: List[Dict] = [
    {
        "name": "rag_search",
        "description": (
            "Search the indexed codebase and knowledge base for relevant context. "
            "Use this FIRST before answering any question about code, architecture, "
            "processes, or documentation. Returns ranked text chunks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query",
                },
                "repo_filter": {
                    "type": "string",
                    "description": "Optional: restrict search to a specific repo name",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "gitlab_read_file",
        "description": (
            "Read the actual content of a specific file from GitLab. "
            "Use when you need to see exact code, not just indexed chunks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "GitLab repo name or path"},
                "path": {"type": "string", "description": "File path within the repo"},
                "branch": {
                    "type": "string",
                    "description": "Branch name (default: main)",
                    "default": "main",
                },
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "gitlab_search_code",
        "description": "Search for a code pattern, symbol, or string across a GitLab repo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "query": {"type": "string", "description": "Code pattern or symbol to search for"},
            },
            "required": ["repo", "query"],
        },
    },
    {
        "name": "gitlab_create_or_update_file",
        "description": (
            "Write or update a file in a GitLab repo. "
            "Use for code changes after you have read and understood the existing file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "path": {"type": "string"},
                "content": {"type": "string", "description": "Full file content to write"},
                "message": {"type": "string", "description": "Commit message"},
                "branch": {"type": "string", "description": "Branch to commit to"},
            },
            "required": ["repo", "path", "content", "message"],
        },
    },
    {
        "name": "jira_get_issue",
        "description": "Get full details of a Jira issue by key (e.g. AiNxt-123).",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Jira issue key, e.g. AiNxt-123"},
            },
            "required": ["issue_key"],
        },
    },
    {
        "name": "jira_create_issue",
        "description": "Create a new Jira issue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Jira project key, e.g. AiNxt"},
                "summary": {"type": "string"},
                "description": {"type": "string"},
                "issue_type": {
                    "type": "string",
                    "description": "Issue type: Bug, Task, Story, etc.",
                    "default": "Task",
                },
            },
            "required": ["project", "summary", "description"],
        },
    },
    {
        "name": "jira_add_comment",
        "description": "Add a comment to an existing Jira issue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "comment": {"type": "string"},
            },
            "required": ["key", "comment"],
        },
    },
    {
        "name": "run_code",
        "description": (
            "Execute code in a sandboxed Docker container. "
            "Returns stdout, stderr, and exit code. "
            "Use for validation, testing, or data processing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "language": {
                    "type": "string",
                    "enum": ["python", "bash", "javascript", "java"],
                },
            },
            "required": ["code", "language"],
        },
    },
    {
        "name": "compliance_check",
        "description": (
            "Check text for PCI/PII violations (PAN, CVV, AADHAAR, API keys, etc.) "
            "before sending sensitive data anywhere. Always call this on user-provided "
            "content before writing it to GitLab or Jira."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    # P7: Clarification tool — agent pauses and asks user for more context
    {
        "name": "request_clarification",
        "description": (
            "Use this tool ONLY when the question is genuinely ambiguous and you cannot "
            "proceed without more information from the user. Do NOT use this as a default — "
            "try to answer with available tools first. When called, the conversation pauses "
            "and the user is asked the clarification question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The specific clarifying question to ask the user",
                },
                "context": {
                    "type": "string",
                    "description": "Brief explanation of why clarification is needed",
                },
            },
            "required": ["question"],
        },
    },
]


# ============================================================
# REACT SYSTEM PROMPT
# ============================================================

REACT_SYSTEM_PROMPT = """You are AiNxt — the AiNxt Autonomous Agentic Engineering Platform.

You operate as a ReAct agent: think step by step, call tools to gather real information, observe results, then decide your next action. Never guess — always read actual code or data before making changes.

## Core Rules
1. ALWAYS search (rag_search) before answering anything about code or architecture.
2. ALWAYS read the actual file (gitlab_read_file) before suggesting or making code changes.
3. Run compliance_check on any user-provided content before writing it to external systems.
4. When creating GitLab changes, include a clear commit message and create on a feature branch (not main/master).
5. When referencing Jira issues, always get_issue first to understand the full context.
6. Be precise about file paths, repo names, and branch names — never guess these.

## Tool Usage Priority (follow this order — cheaper/faster tools first)
1. rag_search — ALWAYS try first (indexed, cached, fastest). Never skip this.
2. gitlab_read_file — only after rag_search identifies the file path
3. gitlab_search_code — to find where a symbol/pattern is used across a repo
4. jira_get_issue — only when a Jira key is explicitly mentioned
5. compliance_check — before writing any user-provided content externally
6. run_code — only when execution/validation is explicitly needed (slow: Docker startup)
7. jira_create_issue / jira_add_comment — write operations, use only when confirmed
8. gitlab_create_or_update_file — most expensive write; always read the file first

## Tool Trust Rules
- Low-quality tool results are annotated with [TOOL_QUALITY: LOW]. Treat these with caution.
- [RATE_LIMITED] signals mean the API is throttled — do not retry immediately.
- [COMPLIANCE BLOCK] on a file means it contains committed secrets — do not proceed with that file.

## Output Format
After gathering enough context, provide:
1. A clear summary of what you found/did
2. Specific file paths and line references
3. Code changes with full context (not just snippets)
4. Next steps if applicable

You operate within AiNxt's PCI/DSS compliance framework. Never expose PAN, CVV, AADHAAR, API keys, or any payment credentials.
"""


# ============================================================
# GOAL VERIFIER PROMPT
# ============================================================

_VERIFIER_PROMPT = """You are a goal-completion verifier for an AI agent system at AiNxt.

You are given:
1. The original GOAL the agent was asked to achieve
2. The STEPS the agent took (tool calls + results)
3. The ANSWER the agent produced

Assess whether the goal was FULLY achieved. Be strict — partial answers, "I could not find it", and vague conclusions are NOT complete.

Respond ONLY in this exact JSON format (no markdown, no extra text):
{"complete": true, "confidence": 0.92, "evidence": "specific quote from answer proving completion", "missing": ""}

Rules:
- complete=true only when the goal is FULLY satisfied with specific evidence
- confidence is 0.0–1.0 (0.0=totally unsure, 1.0=certain)
- evidence must be a specific fact/quote from the answer, not a generic statement
- missing must be non-empty if complete=false — state exactly what is still unresolved
- If agent said "I cannot find X" — that is complete=false unless the goal was to check if X exists
"""


# ============================================================
# TOOL EXECUTOR
# ============================================================

def _build_tool_executor(
        repo_filter: Optional[str],
        user_ctx: Optional[Dict],
        query_risk: str = "LOW",
):
    """Returns a closure that executes tool calls and maintains observation log."""

    observations:   List[Dict] = []
    _write_ops_log: List[Dict] = []      # tracks write ops for consistency awareness
    _step_counter = [0]
    _query_risk_level = query_risk        # used by tool trust enforcement

    # Write tools — tracked for rollback/consistency awareness
    _WRITE_TOOLS = {"gitlab_create_or_update_file", "jira_create_issue", "jira_add_comment"}

    def _quality_score(tool_name: str, result: str) -> tuple:
        """
        Score the quality of a tool result (0.0–1.0).
        Returns (score, signal_label).
        Low scores are annotated in the result so Claude treats them with caution.
        """
        r = str(result)
        if tool_name == "rag_search":
            if "No relevant context found" in r:
                return 0.0, "no_rag_results"
            chunk_count = r.count("---") + 1
            return min(1.0, chunk_count / 4.0), "rag_results"
        if tool_name == "gitlab_read_file":
            if any(x in r for x in ("[COMPLIANCE BLOCK]", "[Error", "not found or empty")):
                return 0.2, "file_error"
            return 1.0, "file_ok"
        if tool_name == "gitlab_search_code":
            if "No results" in r:
                return 0.1, "no_code_results"
            return 0.9, "code_results"
        if tool_name == "jira_get_issue":
            if "not found" in r.lower():
                return 0.1, "jira_not_found"
            return 1.0, "jira_ok"
        return 0.8, "ok"

    def _execute(tool_name: str, inputs: Dict) -> str:
        _step_counter[0] += 1
        step = _step_counter[0]
        logger.info(f"[ReAct] step={step} tool={tool_name}")

        try:
            result = _dispatch(tool_name, inputs, repo_filter, user_ctx)
        except Exception as e:
            err_str = str(e)
            # Rate-limit detection — signal the agent to wait, not blindly retry
            _is_rate_limit = (
                    "429" in err_str
                    or ("rate" in err_str.lower() and "limit" in err_str.lower())
                    or "too many requests" in err_str.lower()
            )
            if _is_rate_limit:
                rate_msg = (
                    f"[RATE_LIMITED] {tool_name} API rate limit hit. "
                    f"Wait at least 30 seconds before retrying this tool. "
                    f"Consider reducing context size or using cached results from rag_search instead."
                )
                observations.append({
                    "step": step, "tool": tool_name, "inputs": inputs,
                    "error": err_str, "ok": False, "quality": 0.0, "signal": "rate_limited",
                })
                logger.warning(f"[ReAct] step={step} tool={tool_name} RATE_LIMITED")
                return rate_msg

            observations.append({
                "step": step, "tool": tool_name, "inputs": inputs,
                "error": err_str, "ok": False, "quality": 0.0, "signal": "error",
            })
            # Track failed write ops so consistency checker can detect partial state
            if tool_name in _WRITE_TOOLS:
                _write_ops_log.append({
                    "step": step, "tool": tool_name, "inputs": inputs, "failed": True
                })
            logger.error(f"[ReAct] step={step} tool={tool_name} error: {e}")
            return f"[Tool error] {tool_name}: {e}"

        # Track write operations for consistency / rollback awareness
        if tool_name in _WRITE_TOOLS:
            _write_ops_log.append({"step": step, "tool": tool_name, "inputs": inputs})
            # P9: Record reversible write ops on the undo stack
            try:
                from agents.recovery_engine import recovery_engine as _re
                _session_id = (user_ctx or {}).get("session_id", "")
                if _session_id:
                    _re.record_action(tool_name, inputs, str(result)[:500], _session_id)
            except Exception:
                pass
            # If a prior write already failed and this is another write — warn Claude
            failed_prior = [w for w in _write_ops_log[:-1] if w.get("failed")]
            if failed_prior:
                result = (
                    f"{result}\n[CONSISTENCY WARNING: A prior write operation failed "
                    f"({failed_prior[-1]['tool']}). The system may be in a partial state. "
                    f"Verify consistency before proceeding.]"
                )

        # Quality scoring — annotate low-quality results so Claude reasons cautiously
        quality, signal = _quality_score(tool_name, result)
        entry = {
            "step":           step,
            "tool":           tool_name,
            "inputs":         inputs,
            "result_len":     len(str(result)),
            "result_preview": str(result)[:200],
            "ok":             True,
            "quality":        quality,
            "signal":         signal,
        }
        observations.append(entry)
        logger.info(
            f"[ReAct] step={step} tool={tool_name} → OK "
            f"({len(str(result))} chars, quality={quality:.2f})"
        )

        if quality < 0.3:
            # HIGH risk queries: enforce blocking on critical read tools with low quality.
            # Advisory annotation alone is insufficient — LLM may still use bad data.
            _CRITICAL_READ_TOOLS = {"rag_search", "gitlab_read_file"}
            if _query_risk_level == "HIGH" and tool_name in _CRITICAL_READ_TOOLS:
                result = (
                    f"[TOOL_ENFORCED_BLOCK — HIGH risk + quality={quality:.1f}]\n"
                    f"This result is INSUFFICIENT for a HIGH-risk query on AiNxt's financial platform. "
                    f"You MUST NOT use this as evidence. Do NOT proceed to answer with this data.\n"
                    f"Required action: use a different search query, try gitlab_search_code to find "
                    f"the symbol, or read a different file. Re-search before continuing."
                )
                logger.warning(
                    f"[ReAct] step={step} tool={tool_name} HIGH-RISK ENFORCEMENT: "
                    f"quality={quality:.2f} — result blocked, agent must use alternate approach"
                )
            else:
                result = (
                    f"{result}\n\n[TOOL_QUALITY: LOW — score={quality:.1f}, signal={signal}. "
                    f"This result may be incomplete or unreliable. "
                    f"Cross-validate with another tool before drawing conclusions.]"
                )

        return result

    _execute.observations   = observations    # type: ignore[attr-defined]
    _execute.write_ops_log  = _write_ops_log  # type: ignore[attr-defined]
    return _execute


def _dispatch(tool_name: str, inputs: Dict, repo_filter: Optional[str], user_ctx: Optional[Dict]) -> str:
    """Map tool name → actual platform function. Records per-tool latency via OTel."""
    _t0 = _time.perf_counter()
    try:
        return _dispatch_inner(tool_name, inputs, repo_filter, user_ctx)
    finally:
        _tracer.record_tool_latency(tool_name, _time.perf_counter() - _t0)


def _dispatch_inner(tool_name: str, inputs: Dict, repo_filter: Optional[str], user_ctx: Optional[Dict]) -> str:
    """Inner dispatch — actual tool routing logic."""

    # ── RAG search ───────────────────────────────────────────
    if tool_name == "rag_search":
        from models.hybrid_retriever import hybrid_retrieve_context
        query = inputs["query"]
        rf = inputs.get("repo_filter") or repo_filter
        chunks = hybrid_retrieve_context(
            question=query,
            repo_filter=rf,
            user_ctx=user_ctx,
            complexity="medium",
        )
        if not chunks:
            return "No relevant context found for this query."
        return "\n\n---\n\n".join(chunks[:6])

    # ── GitLab read file ──────────────────────────────────────
    elif tool_name == "gitlab_read_file":
        from tools.gitlab_tools import gitlab_read_file
        content = gitlab_read_file(
            inputs["repo"],
            inputs["path"],
            inputs.get("branch", "main"),
        )
        if not content:
            return f"File not found or empty: {inputs['path']}"
        # Truncate very large files to avoid token explosion
        if len(content) > 20_000:
            content = content[:20_000] + "\n\n[... truncated at 20k chars ...]"
        # Compliance scan: catch accidentally committed credentials before
        # they enter the LLM's context window.
        # Only scan for hard secrets (keys/tokens) — not PAN/email which
        # legitimately appear in test fixtures and config comments.
        _SECRET_TYPES = {
            "PRIVATE_KEY_LEAK", "CERTIFICATE_LEAK", "SSH_KEY_LEAK",
            "KEY_ASSIGNMENT_LEAK", "PAYMENT_KEY_LEAK", "API_KEY",
            "ACCESS_TOKEN", "SECRET",
        }
        from core.config import COMPLIANCE_SCAN_TOOL_RESULTS
        try:
            if not COMPLIANCE_SCAN_TOOL_RESULTS:
                logger.debug("[ReAct] gitlab_read_file compliance scan skipped reason=tool_results_disabled")
                return content
            from agents.compliance_engine import compliance_engine
            _scan = compliance_engine.analyze(content)
            _hard = [f for f in _scan if f.get("type") in _SECRET_TYPES]
            if _hard:
                _types = list({f["type"] for f in _hard})
                logger.warning(
                    f"[ReAct] gitlab_read_file compliance block: {inputs['path']} "
                    f"contains {_types} — redacting before LLM"
                )
                return (
                    f"[COMPLIANCE BLOCK] File `{inputs['path']}` contains committed secrets "
                    f"({', '.join(_types)}). Content withheld to prevent credential exposure. "
                    f"Remove the secrets from the file first, then retry."
                )
        except Exception as _ce:
            logger.debug(f"[ReAct] gitlab_read_file compliance scan skipped: {_ce}")
        return content

    # ── GitLab code search ────────────────────────────────────
    elif tool_name == "gitlab_search_code":
        from tools.gitlab_tools import gitlab_search_code
        results = gitlab_search_code(inputs["repo"], inputs["query"])
        if not results:
            return f"No results for '{inputs['query']}' in {inputs['repo']}"
        return results

    # ── GitLab create/update file ─────────────────────────────
    elif tool_name == "gitlab_create_or_update_file":
        import time as _time
        from tools.gitlab_tools import gitlab_create_or_update_file
        # Transient write retry — up to 2 retries with exponential backoff
        # Handles network glitches on GitLab API without propagating to agent as failure
        _last_write_err: Optional[Exception] = None
        for _attempt in range(3):
            try:
                result = gitlab_create_or_update_file(
                    repo=inputs["repo"],
                    path=inputs["path"],
                    content=inputs["content"],
                    commit_message=inputs.get("message", "AiNxt automated change"),
                    branch=inputs.get("branch", "ainxt-automated"),
                )
                return f"File written successfully: {inputs['path']} → {result}"
            except Exception as _we:
                _last_write_err = _we
                _err_s = str(_we).lower()
                # Only retry on transient errors (network, timeout, 5xx)
                _transient = any(k in _err_s for k in (
                    "timeout", "connection", "503", "502", "500", "network",
                ))
                if not _transient or _attempt == 2:
                    raise
                _backoff = 2 ** _attempt  # 1s, 2s
                logger.warning(
                    f"[ReAct] gitlab_create_or_update_file attempt {_attempt + 1} "
                    f"failed (transient): {_we} — retrying in {_backoff}s"
                )
                _time.sleep(_backoff)
        raise _last_write_err  # should not reach here

    # ── Jira get issue ────────────────────────────────────────
    elif tool_name == "jira_get_issue":
        from tools.jira_tools import jira_get_issue
        issue_key = inputs.get("issue_key") or inputs.get("key")
        issue = jira_get_issue(issue_key)
        if not issue:
            return f"Jira issue not found: {issue_key}"
        return json.dumps(issue, default=str, indent=2)

    # ── Jira create issue ─────────────────────────────────────
    elif tool_name == "jira_create_issue":
        import time as _time
        from tools.jira_tools import jira_create_issue
        for _attempt in range(3):
            try:
                key = jira_create_issue(
                    project=inputs["project"],
                    summary=inputs["summary"],
                    description=inputs["description"],
                    issue_type=inputs.get("issue_type", "Task"),
                )
                return f"Jira issue created: {key}"
            except Exception as _we:
                _err_s = str(_we).lower()
                _transient = any(k in _err_s for k in ("timeout", "connection", "503", "502", "500"))
                if not _transient or _attempt == 2:
                    raise
                _time.sleep(2 ** _attempt)

    # ── Jira add comment ──────────────────────────────────────
    elif tool_name == "jira_add_comment":
        import time as _time
        from tools.jira_tools import jira_add_comment
        for _attempt in range(3):
            try:
                jira_add_comment(inputs["key"], inputs["comment"])
                return f"Comment added to {inputs['key']}"
            except Exception as _we:
                _err_s = str(_we).lower()
                _transient = any(k in _err_s for k in ("timeout", "connection", "503", "502", "500"))
                if not _transient or _attempt == 2:
                    raise
                _time.sleep(2 ** _attempt)

    # ── Run code ──────────────────────────────────────────────
    elif tool_name == "run_code":
        from sandbox.docker_executor import DockerExecutor
        executor = DockerExecutor()
        result = executor.execute(
            code=inputs["code"],
            language=inputs.get("language", "python"),
        )
        output = result.get("output", "")
        exit_code = result.get("exit_code", -1)
        success = result.get("success", False)
        status = "SUCCESS" if success else f"FAILED (exit {exit_code})"
        return f"[{status}]\n{output}"

    # ── Compliance check ──────────────────────────────────────
    elif tool_name == "compliance_check":
        from agents.compliance_engine import compliance_engine
        findings = compliance_engine.analyze(inputs["text"])
        if not findings:
            return "PASS — no compliance violations found."
        summary = [f"BLOCK — {len(findings)} violation(s) found:"]
        for f in findings[:10]:
            summary.append(f"  • {f.get('type', 'UNKNOWN')}: {f.get('value', '')[:40]}")
        return "\n".join(summary)

    # P7: Clarification tool — signals the orchestrator to pause and ask the user
    elif tool_name == "request_clarification":
        question = inputs.get("question", "Could you provide more details?")
        context = inputs.get("context", "")
        # Return a structured signal that chat_worker.py can detect and forward to the user
        import json as _json_clar
        return _json_clar.dumps({
            "__clarification_needed__": True,
            "question": question,
            "context": context,
        })

    else:
        return f"[Unknown tool: {tool_name}]"


# ============================================================
# REACT ORCHESTRATOR
# ============================================================

class ReactOrchestrator:
    """
    Claude-native ReAct agent loop.

    Claude decides what tools to call, in what order, and when it's done.
    We execute the tools and return results. Claude synthesises the final answer.
    """

    def run(
            self,
            goal: str,
            user_ctx: Optional[Dict] = None,
            repo_filter: Optional[str] = None,
            agent_id: Optional[str] = None,
            max_tool_rounds: int = 8,
    ) -> str:
        """
        Run the ReAct loop for the given goal.
        Returns the final answer string (non-streaming).
        """
        logger.info(f"[ReactOrchestrator] START goal={goal[:120]!r} repo={repo_filter}")

        # P9: Check for existing ReAct checkpoint (crash recovery / resume)
        _session_id_for_ckpt = (user_ctx or {}).get("session_id", "")
        _react_ckpt = None
        if _session_id_for_ckpt:
            try:
                from agents.recovery_engine import recovery_engine as _re_ckpt
                _react_ckpt = _re_ckpt.load_react_checkpoint(_session_id_for_ckpt, goal)
            except Exception:
                pass

        # Compute risk level early — needed by tool executor for enforcement decisions
        _query_risk = _classify_query_risk(goal)
        goal_state_risk_pre: str = _query_risk   # capture before goal_state dict exists

        executor = _build_tool_executor(repo_filter, user_ctx, query_risk=_query_risk)

        # P10: Load system prompt from registry (versioned + A/B testable)
        # Falls back to hardcoded REACT_SYSTEM_PROMPT if registry unavailable.
        _session_id_for_prompt = (user_ctx or {}).get("session_id", "")
        try:
            from core.prompt_registry import prompt_registry as _pr
            _registered_prompt = _pr.get("react_system_prompt", session_id=_session_id_for_prompt)
            system_prompt = _registered_prompt or REACT_SYSTEM_PROMPT
        except Exception:
            system_prompt = REACT_SYSTEM_PROMPT

        # If an agent_id is provided, inject the agent's system prompt
        if agent_id:
            system_prompt = self._agent_system_prompt(agent_id) or system_prompt

        # Inject memory hint from past successful runs
        try:
            from memory.postgres_memory import postgres_memory
            hint = postgres_memory.get_tool_sequence_hint(goal)
            if hint:
                system_prompt = system_prompt + hint
        except Exception:
            pass

        # P2: rank tool schemas by relevance to the goal (TF-IDF, no LLM call)
        # Caps at 15 tools sent to Claude to reduce prompt size and improve selection.
        try:
            from mcp.tool_registry import tool_registry as _tr
            _ranked_defs = _tr.rank_tools(goal)
            _ranked_names = {d.name for d in _ranked_defs}
            # Keep schemas whose name appears in the ranked set; preserve original order
            # for schemas not in the registry (they are always included).
            _ranked_schemas = [s for s in TOOL_SCHEMAS if s["name"] in _ranked_names]
            _unranked_schemas = [s for s in TOOL_SCHEMAS if s["name"] not in _ranked_names]
            _active_tool_schemas = (_ranked_schemas + _unranked_schemas)[:15]
        except Exception:
            _active_tool_schemas = TOOL_SCHEMAS

        # Structured goal state — tracks what was achieved
        goal_state: Dict[str, Any] = {
            "goal":            goal,
            "status":          "in_progress",
            "completed_steps": [],
            "confidence":      0.0,
            "evidence":        "",
            "missing":         "",
        }

        # P7: Advanced reasoning strategy selection
        # ToT: HIGH-risk + complex (>15 words + conjunction signals)
        # SC:  LOW-risk + factual (no task verbs, short question)
        # CoVe: applied post-verifier when confidence < 0.75
        import os as _os_ar
        _ar_enabled = _os_ar.getenv("ADVANCED_REASONING_ENABLED", "false").lower() in ("1", "true", "yes")
        _word_count_goal = len(goal.split())
        _is_complex_goal = (
            _word_count_goal >= 15
            and bool(__import__("re").search(r'\b(and|or|also|as well as|both)\b', goal, __import__("re").IGNORECASE))
        )
        _is_factual_goal = (
            not is_task_request(goal)
            and _word_count_goal <= 20
            and not bool(__import__("re").search(r'\b(create|update|delete|fix|implement|write|run|execute)\b', goal, __import__("re").IGNORECASE))
        )

        if _ar_enabled and _query_risk == "HIGH" and _is_complex_goal:
            try:
                from agents.advanced_reasoning import TreeOfThoughts
                logger.info("[ReactOrchestrator] P7: using TreeOfThoughts (HIGH-risk + complex)")
                answer = TreeOfThoughts().run(goal, system_prompt, executor)
                goal_state["reasoning_strategy"] = "tree_of_thoughts"
            except Exception as _tot_e:
                logger.warning(f"[ReactOrchestrator] ToT failed ({_tot_e}), falling back to ReAct")
                answer = self._run_with_fallback(
                    system_prompt=system_prompt, goal=goal, executor=executor,
                    max_tool_rounds=max_tool_rounds, tool_schemas=_active_tool_schemas,
                )
        elif _ar_enabled and _query_risk == "LOW" and _is_factual_goal:
            try:
                from agents.advanced_reasoning import SelfConsistency
                logger.info("[ReactOrchestrator] P7: using SelfConsistency (LOW-risk + factual)")
                answer = SelfConsistency().run(goal, system_prompt)
                goal_state["reasoning_strategy"] = "self_consistency"
            except Exception as _sc_e:
                logger.warning(f"[ReactOrchestrator] SC failed ({_sc_e}), falling back to ReAct")
                answer = self._run_with_fallback(
                    system_prompt=system_prompt, goal=goal, executor=executor,
                    max_tool_rounds=max_tool_rounds, tool_schemas=_active_tool_schemas,
                )
        else:
            answer = self._run_with_fallback(
                system_prompt=system_prompt,
                goal=goal,
                executor=executor,
                max_tool_rounds=max_tool_rounds,
                tool_schemas=_active_tool_schemas,
            )

        obs = getattr(executor, "observations", [])
        goal_state["completed_steps"] = [o["tool"] for o in obs]
        # Record ReAct tool-round count immediately after the main run
        _tracer.record_react_iteration(len(obs))

        # ── RISK-AWARE MULTI-STEP VERIFIER LOOP ─────────────────────────
        # Risk classification drives verification intensity:
        #   HIGH risk  → up to 5 verify+recover loops, multi-source required
        #   MEDIUM risk → up to 3 loops
        #   LOW risk    → 1 loop (fast path)
        # Gap #7 (7/7): source the verify-loop ceiling from the UNIFIED loop
        # policy (single source of truth for agentic depth) rather than a private
        # hard-coded dict. Fail-safe to the historical mapping on any import error.
        try:
            from agents.loop_policy import verify_loops_for_risk as _vlfr
            _MAX_VERIFY_LOOPS = _vlfr(_query_risk)
        except Exception:  # noqa: BLE001
            _MAX_VERIFY_LOOPS = {"HIGH": 5, "MEDIUM": 3, "LOW": 1}[_query_risk]
        goal_state["query_risk"] = _query_risk
        _critique_severity = "NONE"

        if obs:
            try:
                for _vloop in range(_MAX_VERIFY_LOOPS):
                    vstate = self._verify_goal_completion(goal, answer, obs)
                    goal_state.update(vstate)
                    _verifier_conf = float(goal_state.get("confidence", 0.70))

                    # Claim-level grounding check — not just "has long result" but
                    # "each claim in the answer is traceable to retrieved data"
                    _grounding_ratio, _ungrounded = self._check_claim_grounding(answer, obs)
                    if _grounding_ratio < 0.50:
                        _verifier_conf = max(0.0, _verifier_conf - 0.20)
                        goal_state["confidence"] = _verifier_conf
                        goal_state["ungrounded_claims"] = _ungrounded
                        goal_state["missing"] = (
                                (goal_state.get("missing") or "")
                                + f" [Claims not grounded in retrieved data: "
                                  f"{', '.join(_ungrounded[:3])}]"
                        ).strip()
                        logger.info(
                            f"[ReactOrchestrator] claim grounding={_grounding_ratio:.2f} "
                            f"ungrounded={_ungrounded[:3]}"
                        )

                    logger.info(
                        f"[ReactOrchestrator] verify loop={_vloop + 1}/{_MAX_VERIFY_LOOPS} "
                        f"risk={_query_risk} complete={goal_state.get('complete')} "
                        f"conf={_verifier_conf:.2f} missing={goal_state.get('missing','')[:60]!r}"
                    )

                    if goal_state.get("complete") and _verifier_conf > 0.85:
                        break
                    if not goal_state.get("missing"):
                        break

                    # Recovery pass — target exactly what is missing
                    _recovery_goal = (
                        f"{goal}\n\n"
                        f"[RECOVERY {_vloop + 1}/{_MAX_VERIFY_LOOPS} | "
                        f"Risk={_query_risk}]: Previous attempt incomplete. "
                        f"Missing: {goal_state.get('missing','')}. "
                        f"Address ONLY the missing part. Do not repeat prior work."
                    )
                    _recovery = self._run_with_fallback(
                        system_prompt=system_prompt,
                        goal=_recovery_goal,
                        executor=executor,
                        max_tool_rounds=max(2, max_tool_rounds // 2),
                    )
                    if _recovery and not _recovery.startswith("[ERROR"):
                        answer = answer + "\n\n---\n\n" + _recovery
                        goal_state["confidence"] = min(_verifier_conf + 0.10, 1.0)
                    else:
                        break

                # Adversarial critique (only when confidence warrants it)
                if goal_state.get("confidence", 0) > 0.50:
                    try:
                        _critique = self._critique_answer(goal, answer)
                        if _critique:
                            goal_state["critique"] = _critique
                            _critique_severity = _classify_critique_severity(_critique)
                            goal_state["critique_severity"] = _critique_severity
                            logger.info(
                                f"[ReactOrchestrator] critique severity={_critique_severity}: "
                                f"{_critique[:80]!r}"
                            )
                            # BINDING: HIGH severity critique forces full regeneration
                            if _critique_severity == "HIGH":
                                logger.warning(
                                    "[ReactOrchestrator] HIGH critique severity — "
                                    "forcing answer regeneration"
                                )
                                _regen_goal = (
                                    f"{goal}\n\n"
                                    f"[CRITIQUE FOUND FACTUAL ERROR: {_critique}\n"
                                    f"The previous answer was incorrect. "
                                    f"Regenerate with verified correct information only.]"
                                )
                                _obs_count_before_regen = len(obs)
                                _regen = self._run_with_fallback(
                                    system_prompt=system_prompt,
                                    goal=_regen_goal,
                                    executor=executor,
                                    max_tool_rounds=max(2, max_tool_rounds // 2),
                                )
                                if _regen and not _regen.startswith("[ERROR"):
                                    _new_obs_added = len(obs) > _obs_count_before_regen
                                    if _new_obs_added:
                                        # New evidence gathered — safe to replace
                                        answer = _regen
                                        goal_state["status"] = "regenerated_after_critique"
                                    else:
                                        # Same context — appending avoids "wrong → refined wrong"
                                        # loop that could still be confidently incorrect
                                        answer = (
                                                answer
                                                + "\n\n---\n\n"
                                                + "**Revised perspective (critique applied, same evidence):**\n"
                                                + _regen
                                        )
                                        goal_state["status"] = "critique_appended_same_context"
                                        logger.info(
                                            "[ReactOrchestrator] regen used same evidence — "
                                            "appended rather than replaced to prevent "
                                            "'wrong → refined wrong' loop"
                                        )
                    except Exception as _ce:
                        logger.debug(f"[ReactOrchestrator] critique skipped: {_ce}")

                # Cross-tool contradiction detection
                _contradiction = self._detect_contradictions(obs)
                if _contradiction:
                    goal_state["contradiction"] = _contradiction
                    logger.warning(f"[ReactOrchestrator] {_contradiction[:80]}")

                # Multi-run consistency check (cross-session divergence detection)
                _consistency_warning = self._check_run_consistency(goal, answer)
                if _consistency_warning:
                    goal_state["consistency_warning"] = _consistency_warning
                    logger.info(f"[ReactOrchestrator] {_consistency_warning[:100]}")
                    # Append consistency warning to answer so user sees it
                    if _consistency_warning not in answer:
                        answer += f"\n\n> {_consistency_warning}"

                # Hybrid confidence — data-driven, not LLM self-estimation
                _hybrid_conf = self._compute_hybrid_confidence(
                    verifier_conf=float(goal_state.get("confidence", 0.70)),
                    observations=obs,
                    critique_severity=_critique_severity,
                )
                goal_state["confidence"] = _hybrid_conf
                goal_state["confidence_breakdown"] = {
                    "verifier": float(goal_state.get("confidence", 0.70)),
                    "query_risk": _query_risk,
                    "critique_severity": _critique_severity,
                }
                # Record verifier loop count and final confidence score
                _tracer.record_verifier_loops(_vloop + 1)
                _tracer.record_confidence(_hybrid_conf)

                # Hard-stop safety layer: refuse to answer rather than hallucinate.
                # Threshold is risk-aware — HIGH-risk queries on a financial platform
                # require a higher evidence bar before answering.
                _HARD_STOP_THRESHOLD = {"HIGH": 0.75, "MEDIUM": 0.60, "LOW": 0.50}.get(
                    _query_risk, 0.50
                )
                if _hybrid_conf < _HARD_STOP_THRESHOLD and obs:
                    _searched = ", ".join(dict.fromkeys(o["tool"] for o in obs[:6]))
                    answer = (
                        f"> ⚠️ **AiNxt Hard Stop** (confidence: {_hybrid_conf:.0%} "
                        f"< {_HARD_STOP_THRESHOLD:.0%} threshold for {_query_risk} risk)\n\n"
                        f"Insufficient verified data to answer this reliably. "
                        f"Providing a low-confidence answer on AiNxt's financial platform "
                        f"would be worse than no answer.\n\n"
                        f"**What was searched:** {_searched}\n\n"
                        f"**What to do:**\n"
                        f"- Verify the codebase has been indexed (`/index` command)\n"
                        f"- Add more specific context: file name, Jira key, class name\n"
                        f"- For production issues, contact the team owner directly"
                    )
                    goal_state["status"] = "hard_stopped"
                else:
                    # Final status
                    if goal_state.get("complete") and _hybrid_conf > 0.85:
                        goal_state["status"] = goal_state.get("status") or "completed"
                    elif _hybrid_conf > 0.70:
                        goal_state["status"] = goal_state.get("status") or "completed_partial"
                    else:
                        goal_state["status"] = goal_state.get("status") or "partial"

                    # Uncertainty propagation for sub-85% confidence
                    if _hybrid_conf < 0.85:
                        _risk_label = "HIGH" if _hybrid_conf < 0.65 else "MEDIUM"
                        _extras = []
                        if goal_state.get("missing"):
                            _extras.append(f"Gap: {goal_state['missing'][:80]}")
                        if goal_state.get("critique") and _critique_severity in ("MEDIUM", "HIGH"):
                            _extras.append(f"Critique: {goal_state['critique'][:80]}")
                        if goal_state.get("contradiction"):
                            _extras.append("Source conflict detected — see above")
                        _extra_str = " | ".join(_extras)
                        answer += (
                                f"\n\n> **AiNxt Confidence:** {_hybrid_conf:.0%} | "
                                f"**Risk:** {_risk_label}"
                                + (f" | {_extra_str}" if _extra_str else "")
                        )

            except Exception as _ve:
                logger.debug(f"[ReactOrchestrator] verifier block error: {_ve}")
                goal_state["status"] = "completed"
                goal_state["confidence"] = 0.80
        else:
            goal_state["status"] = "completed"
            goal_state["confidence"] = 0.85

        # P9: Partial completion — when answer signals max_rounds exceeded or timeout
        if answer and (
            answer.startswith("[ERROR: max tool-use rounds exceeded]")
            or answer.startswith("[ERROR generating response")
        ):
            try:
                from agents.recovery_engine import recovery_engine as _re_partial
                answer = _re_partial.handle_partial_completion(
                    goal=goal,
                    observations=obs,
                    partial_answer=answer,
                    reason="max_rounds_exceeded",
                )
                goal_state["status"] = "partial_completion"
            except Exception:
                pass

        # P7: Chain of Verification — when confidence < 0.75 and AR enabled
        _final_conf = float(goal_state.get("confidence", 0.80))
        if _ar_enabled and _final_conf < 0.75 and obs and not answer.startswith("> ⚠️"):
            try:
                from agents.advanced_reasoning import ChainOfVerification
                logger.info(
                    f"[ReactOrchestrator] P7: using ChainOfVerification "
                    f"(confidence={_final_conf:.2f} < 0.75)"
                )
                answer = ChainOfVerification().verify(
                    goal=goal, answer=answer, observations=obs, repo_filter=repo_filter
                )
                goal_state["reasoning_strategy"] = (
                    goal_state.get("reasoning_strategy", "react") + "+cove"
                )
            except Exception as _cov_e:
                logger.warning(f"[ReactOrchestrator] CoVe failed ({_cov_e}), keeping original answer")

        # Persist tool history + goal state to memory
        if obs:
            try:
                self._save_run_memory(goal, answer, obs, user_ctx, goal_state)
            except Exception as _me:
                logger.warning(f"[ReactOrchestrator] memory save failed: {_me}")

        logger.info(
            f"[ReactOrchestrator] DONE tools_called={len(obs)} "
            f"status={goal_state['status']} confidence={goal_state['confidence']:.2f}"
        )
        return answer

    def _run_with_fallback(
            self,
            system_prompt: str,
            goal: str,
            executor,
            max_tool_rounds: int,
            tool_schemas: Optional[List[Dict]] = None,
    ) -> str:
        """
        Try Claude → OpenAI → Gemini in order.
        Each provider runs the full ReAct tool-use loop.
        Falls back only on hard failures (exception / error prefix), not on
        model refusing to call tools.

        All three providers receive identical system_prompt, goal, tool_schemas,
        and the same tool_executor closure — behaviorally equivalent.

        tool_schemas: ranked/filtered schema list (P2). Defaults to TOOL_SCHEMAS.
        """
        _schemas = tool_schemas if tool_schemas is not None else TOOL_SCHEMAS
        providers = [
            ("claude",  self._try_claude),
            ("openai",  self._try_openai),
            ("gemini",  self._try_gemini),
        ]

        last_error: Optional[str] = None
        for name, fn in providers:
            try:
                logger.info(f"[ReactOrchestrator] attempting provider={name}")
                result = fn(system_prompt, goal, executor, max_tool_rounds, _schemas)
                if result and not result.startswith("[ERROR"):
                    logger.info(f"[ReactOrchestrator] provider={name} succeeded")
                    return result
                logger.warning(f"[ReactOrchestrator] provider={name} returned error result — trying next")
                last_error = result
            except Exception as e:
                logger.warning(f"[ReactOrchestrator] provider={name} raised: {e} — trying next")
                last_error = str(e)

        return f"[ReAct: all providers failed. Last error: {last_error}]"

    def _try_claude(self, system_prompt, goal, executor, max_tool_rounds,
                    tool_schemas: Optional[List[Dict]] = None) -> str:
        from gateway_claude import ClaudeGateway
        gw = ClaudeGateway()
        return gw.generate_with_tools(
            system_prompt=system_prompt,
            user_message=goal,
            context="",
            tools=tool_schemas if tool_schemas is not None else TOOL_SCHEMAS,
            tool_executor=executor,
            max_tool_rounds=max_tool_rounds,
        )

    def _try_openai(self, system_prompt, goal, executor, max_tool_rounds,
                    tool_schemas: Optional[List[Dict]] = None) -> str:
        from gateway_openai import OpenAIGateway
        gw = OpenAIGateway()
        return gw.generate_with_tools(
            system_prompt=system_prompt,
            user_message=goal,
            context="",
            tools=tool_schemas if tool_schemas is not None else TOOL_SCHEMAS,
            tool_executor=executor,
            max_tool_rounds=max_tool_rounds,
        )

    def _try_gemini(self, system_prompt, goal, executor, max_tool_rounds,
                    tool_schemas: Optional[List[Dict]] = None) -> str:
        from gateway_gemini import GeminiGateway
        gw = GeminiGateway()
        return gw.generate_with_tools(
            system_prompt=system_prompt,
            user_message=goal,
            context="",
            tools=tool_schemas if tool_schemas is not None else TOOL_SCHEMAS,
            tool_executor=executor,
            max_tool_rounds=max_tool_rounds,
        )

    def stream(
            self,
            goal: str,
            user_ctx: Optional[Dict] = None,
            repo_filter: Optional[str] = None,
            agent_id: Optional[str] = None,
            max_tool_rounds: int = 8,
    ) -> Generator[str, None, None]:
        """
        Streaming wrapper: runs the full ReAct loop (non-streaming internally),
        then yields the answer in chunks for SSE compatibility.

        Tool execution is not streamed (Claude native tool-use is non-streaming).
        The final answer is yielded token by token.
        """
        # Only show thinking indicator for task-mode questions (not plain Q&A)
        if is_task_request(goal):
            yield "[ReAct agent — gathering context...]\n\n"

        try:
            answer = self.run(
                goal=goal,
                user_ctx=user_ctx,
                repo_filter=repo_filter,
                agent_id=agent_id,
                max_tool_rounds=max_tool_rounds,
            )
        except Exception as e:
            logger.error(f"[ReactOrchestrator.stream] error: {e}")
            yield f"[ReAct agent error: {e}]"
            return

        # Yield in ~50-char chunks for smooth streaming appearance
        chunk_size = 50
        for i in range(0, len(answer), chunk_size):
            yield answer[i : i + chunk_size]

    def _agent_system_prompt(self, agent_id: str) -> Optional[str]:
        """Load system prompt from DB for a named agent."""
        try:
            from db.database import SessionLocal
            from db.models import AgentRecord
            db = SessionLocal()
            try:
                rec = db.query(AgentRecord).filter(AgentRecord.name == agent_id).first()
                if rec and rec.system_prompt:
                    return f"{REACT_SYSTEM_PROMPT}\n\n## Agent-Specific Instructions\n{rec.system_prompt}"
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"[ReactOrchestrator] agent prompt load failed: {e}")
        return None

    def _check_claim_grounding(self, answer: str, observations: List[Dict]) -> tuple:
        """
        Claim-level grounding: extract technical identifiers from the answer and
        verify each appears in tool results or tool inputs.

        Returns (grounding_ratio 0.0–1.0, ungrounded_claims list).
        A ratio < 0.50 triggers a confidence penalty in run().

        This replaces the naive '>100 char = evidence' check with per-claim verification:
        long ≠ relevant ≠ correct. Each claim must be traceable to retrieved data.
        """
        if not observations:
            return 1.0, []  # No tool calls — pure knowledge answer, don't penalise

        # Build evidence corpus from tool results AND inputs (file paths queried, etc.)
        evidence_parts = []
        for o in observations:
            if not o.get("ok"):
                continue
            evidence_parts.append(o.get("result_preview", ""))
            for v in (o.get("inputs") or {}).values():
                evidence_parts.append(str(v))
        evidence = " ".join(evidence_parts).lower()

        if not evidence.strip():
            return 1.0, []

        # Extract technical claims from the answer (first 2000 chars)
        answer_sample = answer[:2000]
        candidates: set = set()
        # CamelCase class/service/controller names (5+ chars)
        candidates.update(re.findall(r'\b[A-Z][a-zA-Z]{4,}\b', answer_sample))
        # snake_case identifiers (at least one underscore, 5+ chars total)
        candidates.update(re.findall(r'\b[a-z][a-z0-9]{2,}_[a-z0-9_]{2,}\b', answer_sample))
        # File references with known extensions
        candidates.update(re.findall(
            r'\b[\w/.-]{3,}\.(?:java|py|go|kt|scala|js|ts|sql|yaml|yml)\b',
            answer_sample,
        ))
        # Jira keys (e.g. AiNxt-123)
        candidates.update(re.findall(r'\b[A-Z]{2,8}-\d{1,6}\b', answer_sample))
        # Domain-specific suffixes that indicate class/component names
        candidates.update(re.findall(
            r'\b\w+(?:Exception|Error|Service|Controller|Handler|Repository|Manager|Factory|Impl)\b',
            answer_sample,
        ))

        # Filter noise: stop-words, very short tokens, generic terms
        _STOP = {
            'True', 'False', 'None', 'Error', 'Note', 'This', 'That', 'The', 'AiNxt',
            'APIs', 'HTTP', 'Java', 'Python', 'Spring', 'Service', 'Controller',
            'Handler', 'Manager', 'Factory', 'Repository',
        }
        claims = [c for c in candidates if len(c) >= 5 and c not in _STOP]

        if not claims:
            return 1.0, []  # No extractable technical claims — can't assess grounding

        ungrounded = [c for c in claims if c.lower() not in evidence]

        # Semantic re-check for lexically ungrounded claims.
        # "c.lower() in evidence" is token presence, not meaning.
        # A claim like "UPIHandler.validate()" may be lexically absent but semantically grounded
        # if the evidence contains equivalent logic ("upi_handler.validate" or an alias).
        # Use embed svc cosine similarity to rescue false "ungrounded" verdicts.
        if ungrounded and evidence.strip():
            try:
                import httpx as _hx
                import math as _math
                import os as _os
                _embed_url = _os.getenv("EMBED_SVC_URL", "").replace("//localhost:", "//127.0.0.1:")
                # Batch: embed each ungrounded claim + the evidence corpus summary
                _batch = [c for c in ungrounded] + [evidence[:600]]
                _resp = _hx.post(
                    f"{_embed_url}/embed",
                    json={"texts": _batch},
                    timeout=5.0,
                )
                if _resp.status_code == 200:
                    _embs = _resp.json().get("embeddings", [])
                    if len(_embs) == len(_batch):
                        _ev_emb = _embs[-1]   # evidence corpus embedding
                        _ne = _math.sqrt(sum(x * x for x in _ev_emb)) or 1.0
                        _semantically_grounded = []
                        for i, claim in enumerate(ungrounded):
                            _c_emb = _embs[i]
                            _nc = _math.sqrt(sum(x * x for x in _c_emb)) or 1.0
                            _sim = sum(x * y for x, y in zip(_c_emb, _ev_emb)) / (_nc * _ne)
                            if _sim >= 0.75:
                                _semantically_grounded.append(claim)
                        # Remove semantically grounded claims — token absence ≠ unsupported
                        ungrounded = [c for c in ungrounded if c not in _semantically_grounded]
                        logger.debug(
                            f"[ClaimGrounding] semantic re-check: "
                            f"{len(_semantically_grounded)} claims rescued, "
                            f"{len(ungrounded)} remain ungrounded"
                        )
            except Exception as _emb_err:
                logger.debug(f"[ClaimGrounding] semantic re-check skipped: {_emb_err}")

        # Recompute ratio after semantic re-check
        grounding_ratio = round(1.0 - (len(ungrounded) / len(claims)), 3) if claims else 1.0
        return grounding_ratio, ungrounded[:5]

    def _detect_contradictions(self, observations: List[Dict]) -> Optional[str]:
        """
        Detect when RAG index and direct GitLab reads disagree about the same file.
        Returns a warning string if contradiction found, else None.
        """
        rag_files: Dict[str, str] = {}
        gitlab_files: Dict[str, str] = {}

        for o in observations:
            if not o.get("ok"):
                continue
            preview = o.get("result_preview", "")
            if o["tool"] == "rag_search":
                # Extract file paths mentioned in RAG chunks
                for path in re.findall(
                        r'\b[\w/.-]+\.(?:java|py|go|kt|scala|js|ts|sql)\b', preview
                ):
                    rag_files[path.split("/")[-1]] = preview[:80]   # basename → snippet
            elif o["tool"] == "gitlab_read_file":
                path = (o.get("inputs") or {}).get("path", "")
                if path:
                    gitlab_files[path.split("/")[-1]] = preview[:80]

        # Files appearing in both RAG and GitLab reads — check for semantic contradiction
        conflicts = []
        for f in set(rag_files) & set(gitlab_files):
            rag_snippet = rag_files[f]
            gl_snippet  = gitlab_files[f]
            _contradicts = False

            # Try semantic similarity via embed svc (preferred — catches paraphrases)
            try:
                import httpx as _hx
                import math as _math
                import os as _os
                _embed_url = _os.getenv("EMBED_SVC_URL", "").replace("//localhost:", "//127.0.0.1:")
                _resp = _hx.post(
                    f"{_embed_url}/embed",
                    json={"texts": [rag_snippet, gl_snippet]},
                    timeout=3.0,
                )
                if _resp.status_code == 200:
                    _embs = _resp.json().get("embeddings", [])
                    if len(_embs) == 2:
                        _a, _b = _embs[0], _embs[1]
                        _dot   = sum(x * y for x, y in zip(_a, _b))
                        _na    = _math.sqrt(sum(x * x for x in _a))
                        _nb    = _math.sqrt(sum(x * x for x in _b))
                        _sim   = _dot / (_na * _nb) if _na and _nb else 1.0
                        # < 0.70 cosine similarity → semantically different content for same file
                        _contradicts = _sim < 0.70
                        logger.debug(
                            f"[Contradiction] {f}: RAG vs GitLab cosine_sim={_sim:.3f} "
                            f"contradicts={_contradicts}"
                        )
            except Exception:
                # Fallback to string comparison when embed svc unavailable
                _contradicts = rag_snippet[:40].lower() not in gl_snippet.lower()

            if _contradicts:
                # Second-stage: LLM confirmation to eliminate false positives.
                # cosine_sim < 0.70 is necessary but not sufficient — same logic
                # written differently (comments, formatting) can score < 0.70.
                # A quick LLM call distinguishes "different style" from "different logic".
                try:
                    from gateway_openai import OpenAIGateway as _OAI2
                    _gw2 = _OAI2()
                    _confirm_prompt = (
                        f"Do these two code/text snippets for the same file '{f}' "
                        f"LOGICALLY CONTRADICT each other?\n\n"
                        f"SNIPPET A (from search index):\n{rag_snippet[:300]}\n\n"
                        f"SNIPPET B (from direct file read):\n{gl_snippet[:300]}\n\n"
                        f"Reply with exactly one word: CONTRADICTS or CONSISTENT\n"
                        f"CONTRADICTS = different logic, different values, incompatible content\n"
                        f"CONSISTENT = same logic, just different style/formatting/comments"
                    )
                    _confirm_raw = "".join(_gw2.generate([
                        {"role": "user", "content": _confirm_prompt}
                    ]))
                    _llm_contradicts = "CONTRADICTS" in _confirm_raw.upper()[:30]
                    logger.debug(
                        f"[Contradiction] {f}: LLM second-stage={_confirm_raw.strip()[:20]!r} "
                        f"→ confirmed={_llm_contradicts}"
                    )
                    _contradicts = _llm_contradicts
                except Exception as _llm_err:
                    logger.debug(f"[Contradiction] LLM second-stage skipped: {_llm_err}")
                    # Keep cosine-only result as fallback
                if _contradicts:
                    conflicts.append(f)

        if conflicts:
            return (
                f"[CROSS-TOOL CONFLICT] File(s) {conflicts[:3]} are logically contradictory "
                f"between RAG index and direct GitLab read (confirmed by cosine similarity + LLM). "
                f"RAG index may be stale. Prefer the direct GitLab read as ground truth."
            )
        return None

    def _compute_hybrid_confidence(
            self,
            verifier_conf: float,
            observations: List[Dict],
            critique_severity: str,
    ) -> float:
        """
        Data-driven confidence formula (not LLM-only):
          0.40 × verifier_score
        + 0.30 × average tool quality
        + 0.20 × RAG relevance score
        + 0.10 × critique score (1.0=no issues, 0.4=MEDIUM, 0.0=HIGH)

        This replaces pure LLM self-estimation with an observable metric.
        """
        # Average tool quality (all successful calls)
        qualities = [o.get("quality", 0.8) for o in observations if o.get("ok")]
        if qualities:
            tool_quality = sum(qualities) / len(qualities)
            # Variance penalty: high spread signals conflicting tool results
            # e.g. tool1=0.95, tool2=0.20 → avg=0.575 looks OK but system has conflicting signals
            if len(qualities) >= 2:
                _mean = tool_quality
                _variance = sum((q - _mean) ** 2 for q in qualities) / len(qualities)
                if _variance > 0.09:  # std_dev > 0.30 → conflicting signals
                    _var_penalty = min(0.25, _variance)
                    tool_quality = max(0.0, tool_quality - _var_penalty)
                    logger.debug(
                        f"[Confidence] variance={_variance:.3f} "
                        f"→ tool_quality penalised by {_var_penalty:.2f}"
                    )
        else:
            tool_quality = 0.50

        # RAG-specific quality (primary evidence source)
        rag_obs = [
            o for o in observations
            if o.get("tool") == "rag_search" and o.get("ok")
        ]
        rag_quality = (
            sum(o.get("quality", 0.5) for o in rag_obs) / len(rag_obs)
            if rag_obs else 0.60
        )

        # Critique score
        critique_map = {"NONE": 1.0, "LOW": 0.80, "MEDIUM": 0.50, "HIGH": 0.10}
        critique_score = critique_map.get(critique_severity, 0.80)

        hybrid = (
                0.40 * verifier_conf
                + 0.30 * tool_quality
                + 0.20 * rag_quality
                + 0.10 * critique_score
        )
        return round(min(1.0, max(0.0, hybrid)), 3)

    def _critique_answer(self, goal: str, answer: str) -> str:
        """
        Adversarial critique using OpenAI (different model from main Claude answer).
        Returns a short problem description, or empty string if no significant issues.
        This implements Model A → Model B critique → Model A refine pattern.
        """
        _CRITIQUE_SYS = (
            "You are a strict adversarial reviewer for an AI agent at AiNxt — India's largest "
            "financial payment system. Your job: find flaws, contradictions, hallucinations, "
            "or critically missing information in the agent's answer.\n"
            "Be adversarial — do NOT be polite. Assume the answer might be wrong.\n"
            "If the answer is correct and complete, respond with exactly: NO_ISSUES\n"
            "Otherwise, describe the problem in 1–2 sentences. Be specific about what is wrong."
        )
        prompt = (
            f"GOAL: {goal}\n\n"
            f"AGENT ANSWER (first 1500 chars):\n{answer[:1500]}\n\n"
            f"Find flaws or missing critical information. Be adversarial."
        )
        try:
            from gateway_openai import OpenAIGateway as _OAI
            _gw = _OAI()
            raw = "".join(_gw.generate([
                {"role": "system", "content": _CRITIQUE_SYS},
                {"role": "user",   "content": prompt},
            ]))
            if "NO_ISSUES" in raw.upper()[:30]:
                return ""
            return raw.strip()[:300]
        except Exception as _e:
            logger.debug(f"[Critique] OpenAI critique failed: {_e}")
            return ""

    def _verify_goal_completion(
            self,
            goal: str,
            answer: str,
            observations: List[Dict],
    ) -> Dict[str, Any]:
        """
        Run a lightweight LLM call (no tools) to verify goal completion.
        Returns goal_state dict with: complete, confidence, evidence, missing,
        completed_steps, status.
        """
        steps_summary = "\n".join(
            f"  Step {o.get('step', i + 1)}: {o['tool']}"
            f"({json.dumps({k: str(v)[:60] for k, v in (o.get('inputs') or {}).items()})[:120]})"
            f" → {str(o.get('result_preview', ''))[:100]}"
            for i, o in enumerate(observations)
        ) or "  (no tools called — answered from knowledge)"

        prompt = (
            f"GOAL: {goal}\n\n"
            f"STEPS TAKEN:\n{steps_summary}\n\n"
            f"ANSWER (first 1200 chars):\n{answer[:1200]}\n\n"
            f"Is the goal FULLY achieved? Reply with JSON only, no markdown."
        )

        _default = {
            "complete": True,
            "confidence": 0.80,
            "evidence": "",
            "missing": "",
            "completed_steps": [o["tool"] for o in observations],
            "status": "completed",
        }

        try:
            from gateway_claude import ClaudeGateway as _CG
            _gw = _CG()
            raw = "".join(_gw.generate([
                {"role": "user", "content": _VERIFIER_PROMPT + "\n\n" + prompt}
            ]))
            json_match = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                complete = bool(parsed.get("complete", True))
                return {
                    "complete": complete,
                    "confidence": float(parsed.get("confidence", 0.80)),
                    "evidence":   str(parsed.get("evidence", ""))[:300],
                    "missing":    str(parsed.get("missing",  ""))[:300],
                    "completed_steps": [o["tool"] for o in observations],
                    "status": "completed" if complete else "partial",
                }
        except Exception as _e:
            logger.debug(f"[Verifier] error: {_e}")

        return _default

    def _save_run_memory(
            self,
            goal: str,
            answer: str,
            observations: List[Dict],
            user_ctx: Optional[Dict],
            goal_state: Optional[Dict] = None,
    ):
        """Persist successful tool sequences for memory-augmented future calls."""
        import uuid as _uuid
        try:
            from memory.postgres_memory import postgres_memory
            tool_names = [o["tool"] for o in observations]
            tool_sequence = [
                {"tool": o["tool"], "inputs": o.get("inputs", {}), "ok": o.get("ok", True)}
                for o in observations
            ]
            user_id = (user_ctx or {}).get("user_id", "system")
            postgres_memory.save_agent_run(
                run_id=str(_uuid.uuid4()),
                agent_name="react",
                question=goal,
                answer=answer[:2000],
                tool_history=tool_names,
                compliance_flags=[],
                metadata={
                    "tool_sequence": tool_sequence,
                    "user_id": user_id,
                    "goal_state": goal_state or {},
                    "scratchpad": [
                        {
                            "step": o.get("step", i+1),
                            "tool": o["tool"],
                            "inputs": {k: str(v)[:100] for k, v in o.get("inputs", {}).items()},
                            "result_preview": o.get("result_preview", ""),
                            "ok": o.get("ok", True),
                        }
                        for i, o in enumerate(observations)
                    ],
                },
            )
        except Exception as e:
            logger.debug(f"[ReactOrchestrator] memory save skipped: {e}")

    def _check_run_consistency(self, goal: str, answer: str) -> Optional[str]:
        """
        Check if recent runs for a semantically similar goal produced answers
        with very different tool sequences (signals unreliable reasoning).
        Returns a warning string when divergence is detected, else None.
        This addresses the cross-run consistency gap:
        same query → different answers → both high confidence → platform untrustworthy.
        """
        try:
            from memory.postgres_memory import postgres_memory
            from db.database import SessionLocal
            from db.models import AgentRun  # noqa: F401 — may not exist, guarded below

            db = SessionLocal()
            try:
                # Query recent runs (last 24h) for the same agent
                import datetime as _dt
                _since = _dt.datetime.utcnow() - _dt.timedelta(hours=24)
                _runs = (
                    db.execute(
                        "SELECT question, metadata FROM agent_runs "
                        "WHERE agent_name = 'react' AND created_at > :since "
                        "ORDER BY created_at DESC LIMIT 20",
                        {"since": _since},
                    ).fetchall()
                )
                if not _runs:
                    return None

                # Use embed svc cosine similarity for goal matching.
                # Keyword overlap ("settlement timeout" vs "UPI settlement delay") misses
                # semantically identical queries with different surface forms.
                # Falls back to keyword overlap when embed svc is unavailable.
                similar_runs = []
                try:
                    import httpx as _hx2
                    import math as _math2
                    import os as _os2
                    _embed_url2 = _os2.getenv("EMBED_SVC_URL", "").replace("//localhost:", "//127.0.0.1:")
                    _all_qs = [goal] + [row[0] or "" for row in _runs]
                    _emb_resp = _hx2.post(
                        f"{_embed_url2}/embed",
                        json={"texts": _all_qs[:21]},  # cap at 21 (goal + 20 runs)
                        timeout=5.0,
                    )
                    if _emb_resp.status_code == 200:
                        _all_embs = _emb_resp.json().get("embeddings", [])
                        if len(_all_embs) >= 2:
                            _goal_emb = _all_embs[0]
                            _ng = _math2.sqrt(sum(x * x for x in _goal_emb)) or 1.0
                            for i, row in enumerate(_runs):
                                if i + 1 >= len(_all_embs):
                                    break
                                _r_emb = _all_embs[i + 1]
                                _nr = _math2.sqrt(sum(x * x for x in _r_emb)) or 1.0
                                _sim = sum(x * y for x, y in zip(_goal_emb, _r_emb)) / (_ng * _nr)
                                if _sim >= 0.75:  # semantic similarity threshold
                                    similar_runs.append(row)
                        logger.debug(
                            f"[Consistency] embed-based similarity: "
                            f"{len(similar_runs)}/{len(_runs)} runs matched"
                        )
                    else:
                        raise ValueError("embed svc returned non-200")
                except Exception as _emb2_err:
                    logger.debug(
                        f"[Consistency] embed svc unavailable ({_emb2_err}), "
                        f"falling back to keyword overlap"
                    )
                    goal_words = set(goal.lower().split()) - {
                        "the", "a", "an", "is", "in", "of", "for", "to", "and", "or",
                    }
                    for row in _runs:
                        _q_words = set((row[0] or "").lower().split())
                        _overlap = len(goal_words & _q_words) / max(len(goal_words), 1)
                        if _overlap >= 0.60:
                            similar_runs.append(row)

                if len(similar_runs) < 2:
                    return None  # Need at least 2 similar runs to detect divergence

                # Compare tool sequences across similar runs
                sequences = []
                for row in similar_runs[:5]:
                    _meta = row[1] or {}
                    if isinstance(_meta, str):
                        import json as _json
                        try:
                            _meta = _json.loads(_meta)
                        except Exception:
                            continue
                    _seq = [t["tool"] for t in _meta.get("tool_sequence", [])]
                    if _seq:
                        sequences.append(_seq)

                if len(sequences) < 2:
                    return None

                # Check if sequences are substantively different
                _seq_sets = [set(s) for s in sequences]
                _union     = set.union(*_seq_sets)
                _intersect = set.intersection(*_seq_sets)
                _divergence = 1.0 - (len(_intersect) / max(len(_union), 1))
                if _divergence > 0.60:
                    return (
                        f"[CONSISTENCY WARNING] {len(similar_runs)} recent runs for "
                        f"similar goals used divergent tool sequences "
                        f"(divergence={_divergence:.0%}). "
                        f"Results may be inconsistent across sessions."
                    )
                return None
            finally:
                db.close()
        except Exception as _e:
            logger.debug(f"[ReactOrchestrator] consistency check skipped: {_e}")
            return None


# ============================================================
# SINGLETON
# ============================================================

react_orchestrator = ReactOrchestrator()
