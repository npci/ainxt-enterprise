# SPDX-License-Identifier: Apache-2.0
# ============================================================
# MULTI-AGENT RUNNER — RouterAgent + Specialist Dispatch
# ============================================================
# Domain-aware routing: a lightweight router classifies the
# request, then a specialist ReactOrchestrator runs with a
# domain-specific system prompt and tool subset.
#
# Specialists:
#   CodingAgent  — read code, write code, run tests
#   JiraAgent    — get/create/comment on Jira issues
#   OpsAgent     — logs, infra, diagnostics
#   ComplianceAgent — PCI/DSS policy lookups
#   SDLCAgent    — full SDLC: code → test → self-heal → MR
# ============================================================

import json
import re
from typing import Optional, Dict, List, Generator

from core.logger import logger
from agents.react_orchestrator import TOOL_SCHEMAS, REACT_SYSTEM_PROMPT, _build_tool_executor

# ============================================================
# DOMAIN SYSTEM PROMPTS
# ============================================================

_CODING_PROMPT = """You are a Senior Software Engineer on the AiNxt platform.

Your workflow:
1. Search (rag_search) to understand the codebase context
2. Read the actual files (gitlab_read_file) before suggesting any changes
3. Search for usages (gitlab_search_code) to understand impact
4. Run code to validate (run_code) if appropriate
5. Write changes with a clear commit message on a feature branch

Rules:
- Never guess file paths — always search first
- Always read before you write
- Ensure every change is PCI-safe (compliance_check on user inputs)
- Write complete file contents, not partial diffs
- Reference exact line numbers and file paths in your response
"""

_JIRA_PROMPT = """You are a Project Manager on the AiNxt platform.

Your workflow:
1. Always get_issue before acting on a ticket
2. Understand the full context (description, comments, status) before creating sub-tasks
3. When creating issues, write clear acceptance criteria
4. When adding comments, be specific and actionable

Rules:
- Cite the exact issue key in all responses (e.g. AiNxt-123)
- Never create duplicate issues — search first if unsure
- Keep descriptions in JIRA wiki format (h1. heading, * bullet, {code} blocks)
"""

_OPS_PROMPT = """You are an Operations Engineer on the AiNxt platform.

Your workflow:
1. Search knowledge base for runbooks and past incidents (rag_search)
2. Read config files to understand current state (gitlab_read_file)
3. Run diagnostic scripts to gather live data (run_code with bash)
4. Create Jira tickets for issues you cannot fix autonomously

Rules:
- Always check existing runbooks before suggesting manual steps
- Prefer non-destructive read operations first
- Never run code that modifies production state without explicit confirmation
- Attach logs and diagnostic output to Jira tickets
"""

_COMPLIANCE_PROMPT = """You are a PCI/DSS Compliance Officer on the AiNxt platform.

Your workflow:
1. Search compliance policies (rag_search with repo_filter=compliance)
2. Read specific policy documents (gitlab_read_file)
3. Check code/text for violations (compliance_check)

Rules:
- Always cite the specific policy clause (e.g. PCI DSS v4.0 Req 3.5.1)
- Be precise — "this MIGHT violate" is not acceptable; either it does or it doesn't
- Run compliance_check on any code before confirming it's clean
- Escalate to human reviewer for ambiguous cases
"""

_SDLC_PROMPT = """You are an Autonomous SDLC Engineer on the AiNxt platform.

Your workflow (full autonomous loop):
1. Read the Jira ticket to understand requirements (jira_get_issue)
2. Search the codebase for relevant files (rag_search + gitlab_search_code)
3. Read the files you will modify (gitlab_read_file)
4. Generate the code changes
5. Run tests to validate (run_code)
6. If tests fail, analyse the error and fix (up to 3 iterations)
7. Write the final code to a feature branch (gitlab_create_or_update_file)
8. Comment the MR URL on the Jira ticket (jira_add_comment)

Rules:
- The Jira ticket is the source of truth for requirements
- Never commit to main/master — always a feature branch named feat/AiNxt-XXX-slug
- Write complete file contents, not snippets
- All generated code must pass compliance_check before commit
"""


# ============================================================
# DOMAIN TOOL SUBSETS
# (filter TOOL_SCHEMAS to only relevant tools per specialist)
# ============================================================

_TOOL_NAMES_BY_DOMAIN = {
    "coding":     {"rag_search", "gitlab_read_file", "gitlab_search_code",
                   "gitlab_create_or_update_file", "run_code", "compliance_check"},
    "jira":       {"rag_search", "jira_get_issue", "jira_create_issue",
                   "jira_add_comment"},
    "ops":        {"rag_search", "run_code", "jira_create_issue", "compliance_check"},
    "compliance": {"rag_search", "gitlab_read_file", "compliance_check"},
    "sdlc":       {t["name"] for t in TOOL_SCHEMAS},  # all tools
    "general":    {t["name"] for t in TOOL_SCHEMAS},  # all tools
}


def _tool_subset(domain: str) -> List[Dict]:
    allowed = _TOOL_NAMES_BY_DOMAIN.get(domain, {t["name"] for t in TOOL_SCHEMAS})
    return [t for t in TOOL_SCHEMAS if t["name"] in allowed]


# ============================================================
# DOMAIN CLASSIFIER
# ============================================================

_DOMAIN_PATTERNS = [
    ("sdlc",       re.compile(
        r"\b(implement\s+.{0,30}jira|build\s+.{0,30}ticket|"
        r"sdlc|auto.?generate|full\s+pipeline)\b", re.IGNORECASE)),
    ("jira",       re.compile(
        r"\b([A-Z]{2,8}-\d{1,6}|jira|ticket|issue|sprint|story|epic|"
        r"create\s+(?:a\s+)?(?:jira|ticket|issue|bug)|"
        r"add\s+comment)\b", re.IGNORECASE)),
    ("coding",     re.compile(
        r"\b(fix|bug|code|function|class|method|file|repo|gitlab|"
        r"refactor|test|pr|mr|merge.?request|commit|branch|"
        r"null.?pointer|exception|error\s+in)\b", re.IGNORECASE)),
    ("compliance", re.compile(
        r"\b(pci|dss|pii|compliance|pan|cvv|aadhaar|gdpr|rbi|"
        r"regulation|policy|audit|encrypt)\b", re.IGNORECASE)),
    ("ops",        re.compile(
        r"\b(log|monitor|cpu|memory|disk|latency|deploy|infra|"
        r"server|container|docker|pod|kubernetes|crash|down|"
        r"outage|incident|alert|queue|redis|postgres)\b", re.IGNORECASE)),
]


def _classify_domain(question: str) -> str:
    """Rule-based domain classifier. Returns one of the domain keys."""
    for domain, pattern in _DOMAIN_PATTERNS:
        if pattern.search(question):
            return domain
    return "general"


# ============================================================
# MULTI-AGENT RUNNER
# ============================================================

class MultiAgentRunner:
    """
    Routes questions to specialist ReAct agents.

    Usage:
        runner = MultiAgentRunner()
        answer = runner.run(goal, user_ctx=..., repo_filter=...)
    """

    def run(
        self,
        goal: str,
        user_ctx: Optional[Dict] = None,
        repo_filter: Optional[str] = None,
        agent_id: Optional[str] = None,
        max_tool_rounds: int = 8,
    ) -> str:
        domain = _classify_domain(goal)
        logger.info(f"[MultiAgent] domain={domain} goal={goal[:100]!r}")

        system_prompt = self._system_prompt(domain)
        tools = _tool_subset(domain)

        # If a specific agent_id is provided, try to load its system prompt from DB
        if agent_id:
            db_prompt = self._agent_db_prompt(agent_id)
            if db_prompt:
                system_prompt = f"{system_prompt}\n\n## Agent Persona\n{db_prompt}"

        executor = _build_tool_executor(repo_filter, user_ctx)

        # Use the ReactOrchestrator's fallback chain so multi-agent also
        # gets Claude → OpenAI → Gemini resilience automatically.
        from agents.react_orchestrator import react_orchestrator as _ro
        answer = _ro._run_with_fallback(
            system_prompt=system_prompt,
            goal=goal,
            executor=executor,
            max_tool_rounds=max_tool_rounds,
        )

        # Handoff: if answer references another domain, do a second pass
        answer = self._check_handoff(goal, answer, domain, user_ctx, repo_filter)

        obs = getattr(executor, "observations", [])
        logger.info(f"[MultiAgent] domain={domain} done tools_called={len(obs)}")
        return answer

    def stream(
        self,
        goal: str,
        user_ctx: Optional[Dict] = None,
        repo_filter: Optional[str] = None,
        agent_id: Optional[str] = None,
        max_tool_rounds: int = 8,
    ) -> Generator[str, None, None]:
        domain = _classify_domain(goal)
        yield f"[{domain.upper()} agent activated...]\n\n"

        try:
            answer = self.run(
                goal=goal,
                user_ctx=user_ctx,
                repo_filter=repo_filter,
                agent_id=agent_id,
                max_tool_rounds=max_tool_rounds,
            )
        except Exception as e:
            logger.error(f"[MultiAgent.stream] error: {e}")
            yield f"[Agent error: {e}]"
            return

        chunk_size = 50
        for i in range(0, len(answer), chunk_size):
            yield answer[i : i + chunk_size]

    def _system_prompt(self, domain: str) -> str:
        return {
            "coding":     _CODING_PROMPT,
            "jira":       _JIRA_PROMPT,
            "ops":        _OPS_PROMPT,
            "compliance": _COMPLIANCE_PROMPT,
            "sdlc":       _SDLC_PROMPT,
            "general":    REACT_SYSTEM_PROMPT,
        }.get(domain, REACT_SYSTEM_PROMPT)

    def _check_handoff(
        self,
        original_goal: str,
        first_answer: str,
        first_domain: str,
        user_ctx: Optional[Dict],
        repo_filter: Optional[str],
    ) -> str:
        """
        If the first agent's answer mentions needing Jira data but it was a
        coding agent, do a quick Jira lookup and append.
        Kept simple — only one level of handoff to avoid infinite loops.
        """
        if first_domain == "coding" and re.search(r"\b[A-Z]{2,8}-\d{1,6}\b", first_answer):
            # Answer mentions a Jira key — enrich with ticket context
            keys = re.findall(r"\b([A-Z]{2,8}-\d{1,6})\b", first_answer)[:2]
            if keys:
                try:
                    from tools.jira_tools import jira_get_issue
                    additions = []
                    for k in keys:
                        issue = jira_get_issue(k)
                        if issue:
                            additions.append(f"\n\n**Jira {k}:** {issue.get('summary','')}")
                    if additions:
                        return first_answer + "".join(additions)
                except Exception as e:
                    logger.debug(f"[MultiAgent] handoff jira lookup failed: {e}")
        return first_answer

    def _agent_db_prompt(self, agent_id: str) -> Optional[str]:
        try:
            from db.database import SessionLocal
            from db.models import AgentRecord
            db = SessionLocal()
            try:
                rec = db.query(AgentRecord).filter(AgentRecord.name == agent_id).first()
                return rec.system_prompt if rec else None
            finally:
                db.close()
        except Exception:
            return None


# ============================================================
# SINGLETON
# ============================================================

multi_agent_runner = MultiAgentRunner()
