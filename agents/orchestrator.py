# SPDX-License-Identifier: Apache-2.0
# ============================================================
# ENTERPRISE ORCHESTRATOR AGENT V4.2 (AiNxt PCI SAFE FINAL FIXED)
# ============================================================

import json
import os
import re
from typing import Generator, Optional

from core.logger import logger
from core.telemetry import tracer
from agents.state import AgentState

# Local-filesystem query detector — gates the deterministic local_mcp_call
# fast-path in plan().
#
# HISTORY (connectors-first fix): this used to match the bare verbs
# list|read|open|find|show|search on their own, which are the most common verbs in
# the language. "Show me my open merge requests" matched on "show" and was routed
# to a local filesystem/shell call on the home directory, never reaching the
# connector planner. A verb alone is NOT evidence of a filesystem question, so the
# verb branch now REQUIRES filesystem evidence alongside it: a filesystem noun, a
# known folder name, or a path/extension. Shell-only verbs (ls, cat) still stand
# alone because nothing else uses those words.
_FILE_QUERY_RE = re.compile(
    # (a) shell-only verbs — unambiguous on their own
    r'\b(?:ls|cat)\b'
    # (b) a filesystem noun / known folder — unambiguous on its own
    r'|\b(?:files?|folders?|director(?:y|ies)|Desktop|Downloads|Documents|home dir)\b'
    # (c) a generic verb, but ONLY with a path or a file extension in the query
    r'|\b(?:list|read|open|find|show|search|view|explore)\b(?=.*(?:'
    r'~[/\\]|(?:^|\s)[/\\][\w./\\-]+|[\w-]+\.[A-Za-z0-9]{1,6}\b'
    r'))',
    re.IGNORECASE,
)

# Remote work-system vocabulary — GitLab / Jira / Confluence questions. These live
# behind CONNECTORS and are NOT reachable from the local filesystem or a shell, so
# a query carrying any of this vocabulary must never take the local_mcp_call
# fast-path (nor be answered with `git`/`curl`).
# NOTE: deliberately EXCLUDES the generic English words "bug" and "story" — they
# appear in ordinary local requests ("find the bug in ~/script.py") and
# "issue"/"ticket" already cover tracker questions.
_REMOTE_SYSTEM_RE = re.compile(
    r'\b(?:merge[ _-]?requests?|pull[ _-]?requests?|MRs?|PRs?'
    r'|git ?lab|jira|confluence'
    r'|repos?|repositor(?:y|ies)|branch(?:es)?|commits?|pipelines?'
    r'|tickets?|issues?|backlog|sprint|epics?)\b',
    re.IGNORECASE,
)

# Unambiguous LOCAL-filesystem evidence: a ~/ or absolute path, a file with an
# extension, or a well-known user folder. When BOTH this and the remote vocabulary
# are present the query really is about a local file ("find the bug in
# ~/notes.txt"), so the filesystem path should win.
_LOCAL_PATH_EVIDENCE_RE = re.compile(
    r'~[/\\]'
    r'|(?:^|\s)[/\\][\w./\\-]+'
    r'|[\w-]+\.[A-Za-z0-9]{1,6}\b'
    r'|\b(?:Desktop|Downloads|Documents)\b',
    re.IGNORECASE,
)

# Jira issue key, e.g. PAY-4521 / ABC-123. Deliberately CASE-SENSITIVE: with
# IGNORECASE this shape also matches "utf-8", "covid-19", "top-10" and similar,
# which would wrongly classify ordinary questions as Jira lookups. Nothing else in
# the codebase recognises this shape (models/classifier.py matches CamelCase only).
_JIRA_KEY_RE = re.compile(r'\b[A-Z][A-Z0-9]{1,9}-\d+\b')


def _is_remote_system_query(question: str) -> bool:
    """True when the question is about a remote work system (GitLab/Jira/Confluence).

    Used to keep connector-backed questions away from the local filesystem/shell
    fast-path. "Show me my open merge requests" is a connector question, not an
    `ls ~` question.

    A Jira issue key is decisive on its own. Otherwise, explicit local-path evidence
    wins the tie: "grep ~/repo/app.py for the retry logic" mentions a repo but is
    plainly a local file request.
    """
    q = question or ""
    if _JIRA_KEY_RE.search(q):
        return True
    if not _REMOTE_SYSTEM_RE.search(q):
        return False
    return not _LOCAL_PATH_EVIDENCE_RE.search(q)

from agents.tools import (
    retrieve_tool,
    compliance_tool,
    local_llm_tool,
    generate_answer_tool
)


def _try_gitlab_direct_api(tool_name: str, params: dict, user_id: str) -> Optional[str]:
    """
    Fallback: call gitlab_tools directly when the GitLab connector fails.
    Resolves the user's token from the profile vault (user_tokens) first,
    then calls the matching gitlab_tools function.

    Returns a formatted context string on success, or None on failure.
    This is the "connector first → direct API fallback" path.
    """
    try:
        from core.platform_credentials import get_gitlab_token
        from tools.gitlab_tools import set_token
        token = get_gitlab_token(user_id=user_id)
        set_token(token)
    except Exception as _te:
        logger.warning(f"GitLab direct API fallback: token resolution failed — {_te}")
        return None

    try:
        if tool_name == "gitlab_list_projects":
            from tools.gitlab_tools import gitlab_list_projects
            items = gitlab_list_projects(limit=params.get("limit", 50))
            return f"[GitLab Direct API — gitlab_list_projects]\n{json.dumps(items, indent=2)}"
        elif tool_name == "gitlab_list_issues":
            from tools.gitlab_tools import gitlab_list_issues
            result = gitlab_list_issues(
                repo=params.get("project_id", ""),
                state=params.get("state", "opened"),
                limit=params.get("limit", 25),
            )
            return f"[GitLab Direct API — gitlab_list_issues]\n{result}"
        elif tool_name == "gitlab_list_mrs":
            from tools.gitlab_tools import gitlab_list_mrs
            result = gitlab_list_mrs(
                repo=params.get("project_id", ""),
                state=params.get("state", "opened"),
                limit=params.get("limit", 25),
            )
            return f"[GitLab Direct API — gitlab_list_mrs]\n{result}"
        elif tool_name == "gitlab_get_project":
            from tools.gitlab_tools import _get, _proj
            result = _get(f"/projects/{_proj(params.get('project_id', ''))}")
            return f"[GitLab Direct API — gitlab_get_project]\n{json.dumps(result, indent=2)}"
        elif tool_name == "gitlab_list_commits":
            from tools.gitlab_tools import _get, _proj
            proj = params.get("project_id", "")
            ref = params.get("ref_name", "")
            limit = params.get("limit", 25)
            path = f"/projects/{_proj(proj)}/repository/commits?per_page={limit}"
            if ref:
                path += f"&ref_name={ref}"
            result = _get(path)
            return f"[GitLab Direct API — gitlab_list_commits]\n{json.dumps(result, indent=2)}"
    except Exception as _fe:
        logger.warning(f"GitLab direct API fallback failed for {tool_name}: {_fe}")
    return None

# ============================================================
# CONFIGURATION
# ============================================================

MAX_ITERATIONS = 3
MIN_CONTEXT_THRESHOLD = 3
HIGH_CONFIDENCE_THRESHOLD = 6


# Gap #7: adaptive loop depth. When ON, the iteration ceiling is derived from
# task complexity (+verification/tool signals) via agents.loop_policy instead of
# the flat MAX_ITERATIONS, so complex/unresolved turns get more try→check→
# recover depth. The policy floors at MAX_ITERATIONS so this can only match or
# deepen today's behavior, never regress. Default-on; env opt-out; fail-safe.
import os as _os_lp
_ADAPTIVE_LOOP_DEPTH = _os_lp.getenv("ADAPTIVE_LOOP_DEPTH", "true").lower() == "true"


# ============================================================
# ORCHESTRATOR
# ============================================================

class OrchestratorAgent:

    def __init__(self):
        # LLM routing is handled entirely by model_router (Local LLM → GPT → Claude).
        # No Ollama / LlamaIndex LLM object needed at the orchestrator level.
        self.llm = None  # legacy param kept for generate_answer_tool signature compatibility
        self.last_run_model_label = "auto"  # snapshotted after each run before eval threads start
        self.last_context: list = []  # chunks from the most recent run — read by gateway for eval scoring
        logger.info("AGENT INIT → model_router (Local LLM/GPT/Claude)")


    # ========================================================
    # MULTI-STEP PLANNER
    # ========================================================
    def _plan_office(self, state: AgentState) -> list[dict]:
        """
        Cowork (office assistant) planner. Lets the model use the user's CONNECTED
        apps via connector_call, the knowledge base via retrieve, and always ends
        with generate. Document *generation* is handled by the frontend doc-intent
        path (Office.jsx → /docs/generate), so it is not a planner action here.
        """
        user_id = (state.user_ctx or {}).get("user_id", "")
        catalog = []
        if user_id:
            try:
                from connectors.registry import connector_registry
                catalog = connector_registry.list_connected_tools(user_id)
                # Enterprise per-tool connector controls: gate the catalog by the
                # user's role + org/dept allow-deny policy (cowork_connector_policy).
                try:
                    from services.cowork_policy import filter_office_catalog
                    catalog = filter_office_catalog(catalog, user_id, getattr(state, "role", "") or "user")
                except Exception as _pe:
                    logger.debug(f"office plan: policy filter skipped → {_pe}")
            except Exception as e:
                logger.warning(f"office plan: connector catalog failed → {e}")

        # No connected apps → answer from attachments/KB only.
        if not catalog:
            return [
                {"step": 1, "action": "retrieve", "query": state.question},
                {"step": 2, "action": "generate"},
            ]

        # Build a compact, read-only-by-default tool catalogue for the planner.
        lines = []
        for c in catalog:
            if c.get("is_write"):
                continue  # writes require an explicit confirm path — never auto-plan them
            req = f" (requires: {', '.join(c['required'])})" if c.get("required") else ""
            lines.append(f'- connector="{c["connector"]}" tool="{c["tool"]}": {c["description"]}{req}')
        tool_catalog = "\n".join(lines) or "(no read tools available)"

        prompt = f"""You are AiNxt Cowork, an AI office assistant for an AiNxt employee.
Plan how to answer the user's request by gathering data from their connected apps
and/or the knowledge base, then generating an answer.

AVAILABLE CONNECTOR TOOLS (the user is authenticated for these):
{tool_catalog}

RULES:
1. Output ONLY a JSON array of steps. No prose.
2. For data in a connected app, use:
   {{"step": N, "action": "connector_call", "connector": "<name>", "tool": "<tool>", "params": {{...}}}}
   Fill "params" using the user's request; include every required param.
3. Use {{"step": N, "action": "retrieve", "query": "..."}} for AiNxt knowledge-base questions.
4. To open or read a WEBSITE/web app the user names (a URL or "portal"/"dashboard"),
   use the desktop browser:
   {{"step": N, "action": "local_mcp_call", "tool": "browser_navigate", "input": {{"url": "https://..."}}}}
   then {{"step": N+1, "action": "local_mcp_call", "tool": "browser_extract", "input": {{}}}}.
   (Only works in the desktop app; the user confirms any clicks/typing.)
5. ALWAYS end with {{"step": N, "action": "generate"}}.
6. Max 4 steps. If the request is just conversation or uses attached documents only,
   return [{{"step": 1, "action": "generate"}}].
7. Never invent a connector/tool not listed above.
8. Calendar cancel/reschedule rule: first call microsoft_365.calendar_list_events with the narrowest date/title/attendee/organizer params available. Use returned event ids only. If multiple meetings match, generate a short disambiguation list. Do not plan calendar_update_event/calendar_cancel_event here; writes are handled by the confirmation proposal path after the match is known.
9. CONNECTORS FIRST. The user will almost NEVER name the system — they say "my open
   MRs", not "use the GitLab connector". Treat work-system vocabulary as a
   connector_call regardless, and prefer a connector_call over retrieve/generate
   whenever a listed tool can answer the question. NEVER plan a filesystem or shell
   step (local_mcp_call with list_directory/read_file/search_files/execute_terminal)
   for data that lives in GitLab, Jira, Outlook, Teams or Confluence — those are
   remote servers and are not on this machine. rule 4's browser is ONLY for a
   website the user explicitly names, never as a substitute for a connector.
10. GitLab / Jira vocabulary → tool mapping:
   - "merge request", "MR", "PR", "needs my review", "ready to merge"
       → gitlab.gitlab_list_my_mrs (the user's own MRs across ALL projects; needs no
         project) or gitlab.gitlab_list_mrs when the user named a project.
   - "repo", "repository", "project", "which repos do I have"
       → gitlab.gitlab_list_projects. "commits"/"recent changes" → gitlab_list_commits.
       A file inside a repo → gitlab.gitlab_read_file (NOT a local file read).
   - An issue key like ABC-123 / PAY-4521 → jira_connector.jira_get_issue with that key.
   - "ticket", "bug", "story", "sprint", "backlog", "assigned to me", "my open issues"
       → jira_connector.jira_search_issues with JQL. For the user's own work use
         "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC".
       For GitLab issues → gitlab.gitlab_list_my_issues (all projects) or gitlab_list_issues.
11. MISSING PARAMS ARE NOT A REASON TO GIVE UP. If a tool needs a project/repo the
   user did not name, either use the cross-project variant (gitlab_list_my_mrs /
   gitlab_list_my_issues) or plan gitlab.gitlab_list_projects as an earlier step to
   discover it. Never invent a project path, and never downgrade to retrieve/generate
   just because one param is unknown.

User request: {state.question}

Return JSON array only:"""

        try:
            from models.model_router import model_router
            # Cowork model policy: Claude Sonnet primary, never the local "simple"
            # tier — the simple model errors/ignores the tool catalogue and the
            # planner falls back to retrieve+generate (so connectors like Outlook
            # are never called from scheduled tasks / server office mode).
            raw = model_router.generate(prompt, model_hint="complex").strip()
            logger.info(f"OFFICE PLAN RAW → {raw[:200]}")
            start, end = raw.find("["), raw.rfind("]") + 1
            if start >= 0 and end > start:
                raw = raw[start:end]
            steps = json.loads(raw)
            valid = {"connector_call", "retrieve", "generate", "local_mcp_call"}
            validated = [s for s in steps if isinstance(s, dict) and s.get("action") in valid]
            if not validated:
                raise ValueError("empty office plan")
            if validated[-1].get("action") != "generate":
                validated.append({"step": len(validated) + 1, "action": "generate"})
            logger.info(f"OFFICE PLAN → {validated}")
            return validated
        except Exception as e:
            logger.warning(f"office plan failed ({e}) → retrieve+generate fallback")
            return [
                {"step": 1, "action": "retrieve", "query": state.question},
                {"step": 2, "action": "generate"},
            ]

    def plan(self, state: AgentState) -> list[dict]:
        """
        Multi-step planner: decompose the question into an ordered list of actions.

        Produces a JSON array of steps, e.g.:
          [
            {"step": 1, "action": "symbol_lookup", "query": "ISOMsg"},
            {"step": 2, "action": "retrieve",      "query": "how BaseChannel processes ISO8583"},
            {"step": 3, "action": "generate"}
          ]

        Valid actions:
          symbol_lookup  — look up a specific code symbol by name
          retrieve       — semantic+BM25 search with optional specific query
          generate       — produce the final answer (always last step)
          compliance     — re-run PCI compliance check (only if triggered by content)
        """
        from models.classifier import classify_query_complexity, detect_query_domain

        try:
            _complexity = classify_query_complexity(state.question)
            _domain     = detect_query_domain(state.question)
        except Exception:
            _complexity = "medium"
            _domain     = "code"

        # ── Cowork "office" mode ────────────────────────────────────────────
        # The non-engineer office assistant: bias the planner toward the user's
        # connected apps (Outlook/Teams/Jira/GitLab/Confluence) + KB.
        #
        # ORDERING (connectors-first fix): this MUST run before BOTH the local-MCP
        # filesystem fast-path below AND the simple/general fast-path further down.
        #   - vs the simple/general fast-path: office questions like "how many
        #     unread emails do I have?" classify as general but still need a
        #     connector_call step.
        #   - vs the local-MCP fast-path: that path used to win on a bare verb, so
        #     "show me my open merge requests" was answered with a directory listing
        #     of the home folder instead of a GitLab connector call. In office mode
        #     the connector planner always gets first refusal.
        if state.mode == "office":
            return self._plan_office(state)

        # ── Word/Excel/PowerPoint document-editing fast-path ─────────────────
        # doc_edit requests come from the Office add-in task pane. The document
        # text is already embedded in the prompt; no retrieval, no connector
        # calls, and no local-filesystem access are needed or permitted.
        # This MUST run before the _FILE_QUERY_RE / local_mcp_call block below
        # so that words like "open", "read", "document", "file" in the user's
        # message can never accidentally route a doc_edit request to the Desktop
        # app's local filesystem tools.
        if state.mode == "doc_edit":
            return [{"step": 1, "action": "generate"}]

        # ── Local MCP deterministic routing ─────────────────────────────────
        # When the desktop app is connected and the question is a file-system
        # operation, route directly to local_mcp_call — no LLM planning needed.
        #
        # Guarded by _is_remote_system_query(): GitLab/Jira/Confluence live behind
        # CONNECTORS and are not on the local disk, so a question about merge
        # requests, tickets, repos or a PAY-123 key must never be answered by
        # listing/grepping the filesystem — it falls through to the planner, which
        # can emit a connector_call.
        _plan_user_id = (state.user_ctx or {}).get("user_id", "")
        if _is_remote_system_query(state.question):
            logger.info(
                "local_mcp fast-path SKIPPED — remote work-system query "
                "(GitLab/Jira/Confluence); connectors must handle it"
            )
        elif _plan_user_id and _FILE_QUERY_RE.search(state.question):
            try:
                from routers.desktop_router import _mcp_get
                _mcp_entry = _mcp_get(_plan_user_id)
                if _mcp_entry:
                    _q = state.question
                    # Extract target path — tilde and absolute paths take priority over shortcuts
                    _tilde_m = re.search(r'~[/\w.\-]+', _q)
                    _abs_m   = re.search(r'(/[\w/.\-]+(?:\.\w{1,6})?)', _q)
                    if _tilde_m:
                        _fpath = os.path.expanduser(_tilde_m.group(0))
                    elif _abs_m:
                        _fpath = _abs_m.group(1).strip()
                    elif re.search(r'\bDesktop\b', _q, re.I):
                        _fpath = os.path.expanduser("~/Desktop")
                    elif re.search(r'\bDownloads\b', _q, re.I):
                        _fpath = os.path.expanduser("~/Downloads")
                    elif re.search(r'\bDocuments\b', _q, re.I):
                        _fpath = os.path.expanduser("~/Documents")
                    else:
                        _fpath = os.path.expanduser("~")

                    # Determine tool: read_file only when the extracted PATH has a file extension
                    # (i.e. user asked about a specific file, not a directory)
                    _path_has_ext = bool(re.search(r'\.\w{1,6}$', _fpath.rstrip()))
                    if _path_has_ext and re.search(r'\b(read|cat|open|view)\b', _q, re.I):
                        _tool = "read_file"
                    elif re.search(r'\b(search|grep|find.*pattern|look for)\b', _q, re.I):
                        _pattern = re.sub(r'.*(search|grep|find|look for)\s+', '', _q, flags=re.I).strip()
                        _mcp_plan = [
                            {"step": 1, "action": "local_mcp_call", "tool": "search_files",
                             "input": {"folder": _fpath, "pattern": _pattern or ""}},
                            {"step": 2, "action": "generate"},
                        ]
                        logger.info(f"AGENT PLAN (local_mcp/search) → {_mcp_plan}")
                        return _mcp_plan
                    else:
                        _tool = "list_directory"

                    _mcp_plan = [
                        {"step": 1, "action": "local_mcp_call", "tool": _tool, "input": {"path": _fpath}},
                        {"step": 2, "action": "generate"},
                    ]
                    logger.info(f"AGENT PLAN (local_mcp/{_tool}) → {_mcp_plan}")
                    return _mcp_plan
            except Exception as _mcp_plan_err:
                logger.warning(f"local_mcp plan extraction failed: {_mcp_plan_err}")

        # NOTE: the `state.mode == "office"` branch used to live here. It now runs
        # ABOVE the local-MCP fast-path so connector questions win — see the comment
        # at the top of this method.

        # Fast path: no repo scope → never retrieve. The /ask API is a general chat
        # endpoint; pgvector/BM25/BGE retrieval only makes sense when the user has
        # scoped the question to a codebase or KB. Without repo_filter there is
        # nothing to retrieve from, so go straight to generate regardless of
        # complexity or domain.
        if not state.repo_filter:
            return [{"step": 1, "action": "generate"}]

        # Deterministic fast path — repo-scoped code questions always need [retrieve → generate].
        # Skip the LLM planning call entirely: saves 1 GPT-5.2 call per query, zero quality loss
        # because the plan is always the same for this category (the vast majority of /ask traffic).
        if state.repo_filter and _domain == "code":
            _det = [
                {"step": 1, "action": "retrieve", "query": state.question},
                {"step": 2, "action": "generate"},
            ]
            logger.info(f"AGENT PLAN (deterministic/repo) → {_det}[:300]")
            return _det

        prompt = f"""You are a code intelligence orchestrator for AiNxt's engineering platform.
Given a question, produce a JSON array of retrieval steps to gather maximum context before answering.

RULES:
1. Always end with {{"step": N, "action": "generate"}}
2. Use "symbol_lookup" when the question mentions a specific class/method/interface name (CamelCase words)
3. Use "retrieve" for semantic search — include a focused "query" field
4. Use "local_mcp_call" for file system queries (list files, read file, find files, etc.)
   Example: {{"step": 1, "action": "local_mcp_call", "tool": "list_directory", "input": {{"path": "/absolute/path/to/directory"}}}}
5. Maximum 4 steps total (lookup/retrieve/mcp steps + generate)
6. Return ONLY valid JSON array, no explanation

Available actions: symbol_lookup, retrieve, generate, local_mcp_call

Question: {state.question}
Repo scope: {state.repo_filter or 'none'}
Context already gathered: {len(state.context)} chunks

Return JSON array only:"""

        try:
            from models.model_router import model_router
            raw = model_router.generate(prompt, model_hint="simple").strip()
            logger.info(f"AGENT PLAN RAW → {raw[:200]}")

            # Extract JSON array from response
            start = raw.find("[")
            end   = raw.rfind("]") + 1
            if start >= 0 and end > start:
                raw = raw[start:end]

            steps = json.loads(raw)
            if not isinstance(steps, list) or not steps:
                raise ValueError("empty plan")

            # Validate each step
            valid_actions = {"symbol_lookup", "retrieve", "generate", "compliance", "connector_call", "local_mcp_call"}
            validated = []
            for s in steps:
                if isinstance(s, dict) and s.get("action") in valid_actions:
                    validated.append(s)

            if not validated:
                raise ValueError("no valid steps")

            # Ensure last step is always generate
            if validated[-1].get("action") != "generate":
                validated.append({"step": len(validated) + 1, "action": "generate"})

            logger.info(f"AGENT PLAN → {validated}")
            return validated

        except Exception as e:
            logger.warning(f"Planner failed ({e}) — fallback to retrieve+generate")
            if state.repo_filter:
                return [
                    {"step": 1, "action": "retrieve", "query": state.question},
                    {"step": 2, "action": "generate"},
                ]
            return [{"step": 1, "action": "generate"}]

    # ========================================================
    # DECISION ENGINE (legacy compatibility wrapper)
    # ========================================================

    def decide(self, state: AgentState) -> dict:
        """
        Single-step decision (legacy compatibility wrapper).
        Returns the FIRST step from plan() if context not yet gathered.
        For the multi-step loop, call plan() directly.
        """
        steps = self.plan(state)
        if steps:
            return steps[0]
        return {"action": "retrieve"}


    # ========================================================
    # CONTEXT CONFIDENCE
    # ========================================================

    def evaluate_context(self, state: AgentState) -> float:

        size = len(state.context)

        if size >= HIGH_CONFIDENCE_THRESHOLD:
            confidence = 0.95

        elif size >= MIN_CONTEXT_THRESHOLD:
            confidence = 0.7

        elif size > 0:
            confidence = 0.4

        else:
            confidence = 0.0

        state.confidence = confidence

        logger.info(
            f"AGENT CONTEXT CONFIDENCE → size={size} confidence={confidence}"
        )

        return confidence


    # ========================================================
    # MAIN EXECUTION LOOP
    # ========================================================

    def run(
        self,
        question: str,
        repo_filter: Optional[str],
        model_hint: Optional[str] = None,
        request_id: str = "",
        raw_question: Optional[str] = None,
        user_ctx: dict = None,
        messages: list = None,
        compliance_passed: bool = False,
        rag_mode: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Generator[str, None, None]:

        # rag_mode and repo_filter are captured by closures below (L3 memory
        # extraction thread)
        _run_rag_mode   = rag_mode
        _run_repo_filter = repo_filter

        logger.info("AGENT START")

        state = AgentState(
            question=question,
            repo_filter=repo_filter,
            model_hint=model_hint,
            raw_question=raw_question or question,
            user_ctx=user_ctx,
            messages=messages or [],
            mode=mode,
        )

        # FIX: define temp_state early to prevent scope crash
        temp_state: Optional[AgentState] = None

        try:

            # ====================================================
            # INPUT PCI CHECK — scan bare user question ONLY,
            # not the history-injected blob (avoids false positives
            # from prior assistant code examples in the conversation).
            #
            # Skipped when compliance_passed=True: the gateway already
            # validated input compliance (ML call up to 200 ms) before
            # invoking the orchestrator. Running it twice is redundant.
            # ====================================================

            if not compliance_passed:
                _compliance_state = AgentState(
                    question=state.raw_question or state.question,
                    repo_filter=None,
                )
                _compliance_state = compliance_tool(_compliance_state)
                state.compliance_flags = _compliance_state.compliance_flags

                if any(flag.get("blocked", False) for flag in state.compliance_flags):

                    logger.critical(
                        f"PCI BLOCKED INPUT → {state.compliance_flags}"
                    )

                    yield "Request blocked due to PCI compliance violation"

                    return


            # ====================================================
            # AUTONOMOUS LOOP
            # ====================================================

            # ── Trivial-query fast-skip ─────────────────────────────────────
            # Plan() is an LLM call (Claude Haiku, 800–2 000 ms). For greetings,
            # small-talk, simple-math, etc. there's nothing to plan — emit a
            # single generate step and skip the LLM call entirely.
            import re as _re_triv
            _TRIV_RE = _re_triv.compile(
                r"^(hi+|hello+|hey+|thanks?|thank\s+you|bye+|good\s+(morning|afternoon|evening|night)|"
                r"what\s+is\s+\d+\s*[\+\-\*\/]\s*\d+|how\s+are\s+you|"
                r"who\s+are\s+you|what\s+(can|do)\s+you\s+do|okay|ok|sure|cool|got\s+it)\??\.?\s*$",
                _re_triv.IGNORECASE,
            )
            _is_trivial = bool(_TRIV_RE.match((state.raw_question or state.question or "").strip()))

            # ── Generate execution plan once (before the iteration loop) ──────
            _decide_span = tracer.start_span("orchestrator.decide", request_id)
            try:
                if _is_trivial:
                    _plan = [{"action": "generate", "query": state.question}]
                    logger.info("orchestrator.plan skipped — trivial query")
                else:
                    _plan = self.plan(state)
            finally:
                tracer.end_span(_decide_span)
            logger.info(f"AGENT EXECUTION PLAN → {_plan}")
            _plan_idx = 0   # which step we're executing

            # Classify once for routing guard and post-generation model bump
            try:
                from models.classifier import (
                    classify_query_complexity,
                    detect_query_domain,
                    CLASS_NAME_PATTERN,
                )
                _complexity = classify_query_complexity(state.question)
                _domain     = detect_query_domain(state.question)
            except Exception:
                _complexity = "medium"
                _domain     = "code"   # safe default: retrieve
                CLASS_NAME_PATTERN = None  # can't import — be conservative

            # Gap #7: adaptive loop depth. Derive the iteration ceiling from the
            # task complexity (+tool usage) instead of the flat MAX_ITERATIONS.
            # Floors at MAX_ITERATIONS so behavior can only match or deepen.
            _max_iters = MAX_ITERATIONS
            if _ADAPTIVE_LOOP_DEPTH:
                try:
                    from agents.loop_policy import decide_loop_budget as _dlb
                    _has_tools = any(
                        (s.get("action") or "") not in ("", "generate")
                        for s in (_plan or []) if isinstance(s, dict)
                    )
                    _budget = _dlb(_complexity, has_tools=_has_tools)
                    _max_iters = _budget.max_iterations
                    logger.info(
                        f"AGENT LOOP BUDGET → {_max_iters} iters "
                        f"({_budget.reason})"
                    )
                except Exception as _lp_err:  # noqa: BLE001 — fail-safe to flat
                    logger.debug(f"adaptive loop depth fell back to flat: {_lp_err}")
                    _max_iters = MAX_ITERATIONS

            for iteration in range(_max_iters):

                state.iterations = iteration + 1
                _iter_span = tracer.start_span(
                    "orchestrator.iteration",
                    request_id,
                    {"iteration": iteration + 1},
                )

                logger.info(
                    f"AGENT ITERATION → {state.iterations}/{_max_iters}"
                )

                # Execute the next planned step
                if _plan_idx < len(_plan):
                    _step    = _plan[_plan_idx]
                    action   = _step.get("action", "generate")
                    _step_q  = _step.get("query") or state.question
                    _plan_idx += 1
                else:
                    action   = "generate"
                    _step_q  = state.question

                # ====================================================
                # ROUTING GUARD — classify before executing any tool.
                #
                # simple or general → always "generate" (skip RAG entirely).
                #   Covers greetings, general knowledge, etc.
                #   Overrides even when planner chose "retrieve".
                #
                # code + medium/complex + no context → force "retrieve".
                #   Planner may return "generate" on first iteration;
                #   we override to load relevant code chunks first.
                # ====================================================
                if not state.context:

                    if (_complexity == "simple" or _domain == "general") and not state.repo_filter:
                        # Never override tool-dispatch actions — they have real work to do
                        if action not in ("local_mcp_call", "connector_call"):
                            if action in ("retrieve", "symbol_lookup"):
                                logger.info(
                                    f"OVERRIDING RETRIEVE → generate "
                                    f"(domain={_domain} complexity={_complexity})"
                                )
                            action = "generate"
                    elif (_complexity == "simple" or _domain == "general") and state.repo_filter:
                        # Even for simple/general questions, if a repo is scoped
                        # (project context), force RAG so the answer comes from the codebase.
                        logger.info(
                            f"FORCING RETRIEVE despite domain={_domain} complexity={_complexity} "
                            f"— repo_filter={state.repo_filter!r} is set (project scope)"
                        )
                        action = "retrieve"

                    elif action not in ("retrieve", "symbol_lookup"):
                        # Force retrieve ONLY when there is a specific codebase
                        # signal: either an explicit repo_filter was passed in
                        # (user is working in a project/thread with a named repo)
                        # OR the question contains a CamelCase class/method name
                        # that implies a specific code entity.
                        # Generic code questions ("write a Python function",
                        # "explain recursion") should NOT be forced into RAG —
                        # doing so pulls unrelated codebase context (MERN, jPOS)
                        # and produces wrong, repo-contaminated answers.
                        _has_entity = CLASS_NAME_PATTERN and CLASS_NAME_PATTERN.search(state.question)
                        if state.repo_filter or _has_entity:
                            logger.warning(
                                f"FORCING RETRIEVE → "
                                f"domain={_domain} complexity={_complexity} "
                                f"repo_filter={state.repo_filter!r} "
                                f"entity={'yes' if _has_entity else 'no'} "
                                f"(planner chose: {action})"
                            )
                            action = "retrieve"
                        else:
                            logger.info(
                                f"Generic code question — allowing generate "
                                f"(no repo_filter, no specific entity) "
                                f"(planner chose: {action})"
                            )
                            # leave action as planner decided (generate)


                # ====================================================
                # RETRIEVE / SYMBOL_LOOKUP
                # ====================================================

                if action in ("retrieve", "symbol_lookup"):

                    logger.info(f"AGENT TOOL → retrieve_tool (query={_step_q[:60]})")

                    # Phase 5: emit a first-class tool START marker. Wrapped so a
                    # failure here can never break the agent loop; consumers that
                    # don't understand it str() it to "" (see ToolMarker).
                    try:
                        from pipeline.stream_events import ToolMarker as _TM
                        yield _TM(tool_id=f"{action}-{getattr(state, 'iterations', 0)}",
                                  name=action, phase="start",
                                  summary=f"Searching sources for: {_step_q[:80]}")
                    except Exception:
                        pass

                    _tool_span = tracer.start_span(
                        "orchestrator.tool",
                        request_id,
                        {"tool": action},
                    )
                    try:
                        # Override question for this retrieval step if planner specified a focused query
                        if _step_q != state.question:
                            _orig_q = state.question
                            state.question = _step_q
                            state = retrieve_tool(state)
                            state.question = _orig_q   # restore original question for generation
                        else:
                            state = retrieve_tool(state)
                    except Exception as _tool_err:
                        tracer.end_span(_tool_span, error=str(_tool_err))
                        try:
                            from store.learning_store import record_tool_failure
                            record_tool_failure(
                                tool=action,
                                error=str(_tool_err),
                                request_id=request_id,
                                user_id=getattr(state, "user_id", ""),
                                plan=getattr(state, "plan", []),
                            )
                        except Exception:
                            pass
                        raise
                    else:
                        tracer.end_span(_tool_span)
                        # Phase 5: first-class tool RESULT marker (fail-safe).
                        try:
                            from pipeline.stream_events import ToolMarker as _TM
                            _n_ctx = len(getattr(state, "context", []) or [])
                            yield _TM(tool_id=f"{action}-{getattr(state, 'iterations', 0)}",
                                      name=action, phase="result", ok=True,
                                      summary=f"Found {_n_ctx} source{'s' if _n_ctx != 1 else ''}")
                        except Exception:
                            pass

                    # NOTE: Do not run compliance on retrieved code chunks.
                    # jPOS / Java source code contains patterns (secret=, key=)
                    # that are false-positive hits.  The output streaming
                    # compliance check (every 200 chars) catches any actual
                    # PCI data in the generated response.

                    # ── Point A: Eval retrieval quality (true fire-and-forget) ──
                    if state.context:
                        import threading as _threading
                        _q_a  = state.question
                        _ctx_a = [c for c in state.context if c]
                        _sid_a = getattr(state, "session_id", None)
                        def _run_eval_a():
                            try:
                                from core.evals import eval_engine
                                eval_engine.eval_retrieval_quality(_q_a, _ctx_a, session_id=_sid_a)
                            except Exception as _eval_err:
                                logger.debug(f"eval_retrieval_quality failed (non-critical): {_eval_err}")
                        _threading.Thread(target=_run_eval_a, daemon=True, name="eval-retrieval").start()

                    confidence = self.evaluate_context(state)
                    # Snapshot context for external eval scoring (gateway reads agent.last_context)
                    if state.context:
                        self.last_context = list(state.context)

                    # Record low-confidence retrieval for learning loop analysis
                    if confidence < 0.3:
                        try:
                            from store.learning_store import record_low_confidence
                            record_low_confidence(
                                question=state.question,
                                confidence=confidence,
                                user_id=getattr(state, "user_id", ""),
                                request_id=request_id,
                            )
                        except Exception:
                            pass

                    # Only break early if we've finished the plan OR confidence is very high
                    if _plan_idx >= len(_plan) - 1:
                        tracer.end_span(_iter_span)
                        break   # last step was a retrieve — proceed to generate
                    if confidence >= 0.95:
                        tracer.end_span(_iter_span)
                        break   # exceptional confidence — skip remaining retrieval steps
                    tracer.end_span(_iter_span)
                    continue


                # ====================================================
                # COMPLIANCE
                # ====================================================

                elif action == "compliance":

                    _tool_span = tracer.start_span(
                        "orchestrator.tool",
                        request_id,
                        {"tool": "compliance"},
                    )
                    try:
                        state = compliance_tool(state)
                    except Exception as _tool_err:
                        tracer.end_span(_tool_span, error=str(_tool_err))
                        raise
                    else:
                        tracer.end_span(_tool_span)

                    if any(flag.get("blocked", False) for flag in state.compliance_flags):

                        tracer.end_span(_iter_span, error="compliance_blocked")
                        yield "Response blocked due to PCI violation"

                        return

                    tracer.end_span(_iter_span)
                    continue


                # ====================================================
                # NEURON
                # ====================================================

                elif action == "local_llm":

                    logger.info("AGENT TOOL → local_llm_tool")

                    _tool_span = tracer.start_span(
                        "orchestrator.tool",
                        request_id,
                        {"tool": "local_llm"},
                    )
                    try:
                        state = local_llm_tool(state)
                    except Exception as _tool_err:
                        tracer.end_span(_tool_span, error=str(_tool_err))
                        raise
                    else:
                        tracer.end_span(_tool_span)

                    state = compliance_tool(state)

                    if any(flag.get("blocked", False) for flag in state.compliance_flags):

                        tracer.end_span(_iter_span, error="compliance_blocked")
                        yield "Response blocked due to PCI violation"

                        return

                    tracer.end_span(_iter_span)
                    break


                # ====================================================
                # CONNECTOR CALL — query enterprise data sources
                # ====================================================

                elif action == "connector_call":

                    _tool_span = tracer.start_span(
                        "orchestrator.tool",
                        request_id,
                        {"tool": "connector_call"},
                    )
                    try:
                        connector_name = _step.get("connector", "")
                        tool_name = _step.get("tool", "")
                        conn_params = _step.get("params", {})
                        # user id may live on state.user_id (gateway path) OR in
                        # state.user_ctx (worker / scheduled-task path) — check both,
                        # else the connector_call is silently skipped and the agent
                        # hallucinates "no connector access".
                        user_id_for_conn = (getattr(state, "user_id", "") or
                                            (state.user_ctx or {}).get("user_id", ""))

                        if connector_name and tool_name and user_id_for_conn:

                            # ── PERMISSION GATE ──────────────────────────────
                            # For gated connectors, check if the user has pre-approved
                            # this tool before executing. Scheduled tasks (flagged via
                            # user_ctx.scheduled=True) bypass this gate — they use the
                            # platform-wide permissions table directly in the task worker.
                            from connectors.engine import connector_engine, PERMISSION_GATED_CONNECTORS
                            _is_scheduled = bool((state.user_ctx or {}).get("scheduled", False))
                            _once_allowed = set((state.user_ctx or {}).get("connector_permissions_once", []))
                            _perm_key = f"{connector_name}:{tool_name}"

                            if connector_name in PERMISSION_GATED_CONNECTORS and not _is_scheduled:
                                if _perm_key not in _once_allowed:
                                    _perm = connector_engine._check_user_permission(
                                        user_id_for_conn, connector_name, tool_name
                                    )
                                    if _perm == "denied":
                                        tracer.end_span(_tool_span, error="permission_denied")
                                        tracer.end_span(_iter_span, error="permission_denied")
                                        yield json.dumps({
                                            "__type": "connector_permission_denied",
                                            "connector": connector_name,
                                            "tool": tool_name,
                                            "message": (
                                                f"You have previously denied access to **{connector_name}** "
                                                f"(`{tool_name}`). Go to Settings → Connectors → Permissions to change this."
                                            ),
                                        })
                                        return
                                    elif _perm == "needs_prompt":
                                        tracer.end_span(_tool_span, error="needs_permission")
                                        tracer.end_span(_iter_span, error="needs_permission")
                                        # Yield a structured permission-request event.
                                        # The frontend renders a dialog; the user's choice is
                                        # stored via POST /connectors/permissions, then the
                                        # query is re-submitted with connector_permissions_once
                                        # or the stored always_allow decision takes effect.
                                        yield json.dumps({
                                            "__type": "connector_permission_request",
                                            "connector": connector_name,
                                            "tool": tool_name,
                                            "description": (
                                                f"AiNxt wants to access your **{connector_name}** account "
                                                f"to run: `{tool_name}`"
                                            ),
                                            "options": ["allow_once", "always_allow", "deny"],
                                        })
                                        return
                                    # else: 'always_allow' → proceed without interruption
                            # ── END PERMISSION GATE ──────────────────────────

                            from connectors.registry import connector_registry
                            _call_counter = getattr(state, "_connector_call_counter", {"count": 0})
                            result = connector_registry.execute(
                                connector_name, tool_name, conn_params,
                                user_id_for_conn, state.question, _call_counter,
                            )
                            setattr(state, "_connector_call_counter", _call_counter)

                            if result.success:
                                context_str = result.to_context_str()
                                from core.context_compressor import compress_tool_output, _source_key
                                # Calendar/meeting tools use a compact line-per-event format
                                # (see ConnectorResponse._CALENDAR_TOOLS) — give them a much
                                # larger budget so all meetings survive the compression pass.
                                # The old 3 000-char ceiling was the second truncation point
                                # that caused only ~3 meetings to reach the LLM (Fix #meetings).
                                _CALENDAR_TOOLS = {
                                    "calendar_list_events", "calendar_create_event",
                                    "calendar_update_event", "calendar_cancel_event",
                                    "calendar_delete_event", "calendar_accept_event",
                                    "calendar_decline_event", "calendar_tentative_event",
                                }
                                _compress_budget = 16_000 if tool_name in _CALENDAR_TOOLS else 3000
                                _compressed = compress_tool_output(tool_name, context_str, max_chars=_compress_budget)
                                if isinstance(state.context, list):
                                    # Dedup: skip if this source is already represented
                                    _new_key = _source_key(_compressed)
                                    if not any(_source_key(c) == _new_key for c in state.context):
                                        state.context.append(_compressed)
                                else:
                                    state.context = [_compressed]
                                logger.info(
                                    f"CONNECTOR → {connector_name}.{tool_name}: "
                                    f"{result.count} items in {result.latency_ms}ms"
                                )
                            elif "REAUTH_REQUIRED" in (result.error or ""):
                                tracer.end_span(_tool_span, error="reauth_required")
                                tracer.end_span(_iter_span, error="reauth_required")
                                yield f"\n⚠️ Your **{connector_name}** connection needs to be renewed. Please go to Settings → Connectors to reconnect."
                                return
                            else:
                                logger.warning(f"CONNECTOR error: {result.error}")
                                # Fallback: try direct GitLab API when the connector fails.
                                # This is the "connector first → direct API" priority order.
                                if connector_name == "gitlab":
                                    _fallback = _try_gitlab_direct_api(
                                        tool_name, conn_params, user_id_for_conn
                                    )
                                    if _fallback is not None:
                                        if isinstance(state.context, list):
                                            state.context.append(_fallback)
                                        else:
                                            state.context = [_fallback]
                                        logger.info(
                                            f"CONNECTOR fallback → direct GitLab API "
                                            f"succeeded for {tool_name}"
                                        )
                        else:
                            logger.warning(f"CONNECTOR step missing connector/tool/user_id: {_step}")
                    except Exception as _conn_err:
                        tracer.end_span(_tool_span, error=str(_conn_err))
                        logger.error(f"CONNECTOR dispatch error: {_conn_err}")
                    else:
                        tracer.end_span(_tool_span)

                    tracer.end_span(_iter_span)
                    continue


                # ====================================================
                # LOCAL MCP CALL — user's desktop file system tools
                # ====================================================

                elif action == "local_mcp_call":

                    _mcp_span = tracer.start_span(
                        "orchestrator.tool",
                        request_id,
                        {"tool": "local_mcp_call"},
                    )
                    try:
                        _mcp_uid  = (state.user_ctx or {}).get("user_id", "")
                        _mcp_tool = _step.get("tool", "")
                        _mcp_inp  = _step.get("input", {})
                        if _mcp_uid and _mcp_tool:
                            from routers.desktop_router import execute_local_mcp_tool
                            _mcp_res = execute_local_mcp_tool(_mcp_uid, _mcp_tool, _mcp_inp)
                            if _mcp_res.get("success"):
                                _ctx_raw = (
                                    f"[Local Filesystem — {_mcp_tool}]\n"
                                    + json.dumps(_mcp_res.get("result", {}), indent=2)
                                )
                                from core.context_compressor import compress_tool_output, _source_key
                                _ctx = compress_tool_output(_mcp_tool, _ctx_raw, max_chars=3000)
                                _new_key = _source_key(_ctx)
                                if not any(_source_key(c) == _new_key for c in state.context):
                                    state.context.append(_ctx)
                                logger.info(f"LOCAL MCP → {_mcp_tool}: success")
                            else:
                                state.context.append(
                                    f"[Local Filesystem Error — {_mcp_tool}]\n"
                                    + _mcp_res.get("error", "Unknown error")
                                )
                        else:
                            logger.warning(f"LOCAL MCP step missing tool/user_id: {_step}")
                    except RuntimeError as _mcp_err:
                        state.context.append(f"[Local Filesystem]\n{_mcp_err}")
                        tracer.end_span(_mcp_span, error=str(_mcp_err))
                    except Exception as _mcp_err:
                        tracer.end_span(_mcp_span, error=str(_mcp_err))
                        logger.error(f"LOCAL MCP dispatch error: {_mcp_err}")
                    else:
                        tracer.end_span(_mcp_span)

                    tracer.end_span(_iter_span)
                    continue


                # ====================================================
                # GENERATE
                # ====================================================

                elif action == "generate":

                    tracer.end_span(_iter_span)
                    break


            # ====================================================
            # PRE-GENERATION PCI CHECK (question only)
            # Scan the user question again — not the code context.
            # Code chunks contain legitimate Java "secret = ..." patterns
            # that would trigger false positives.
            # ====================================================

            _q_state = AgentState(question=state.raw_question or state.question, repo_filter=None)
            _q_state = compliance_tool(_q_state)

            if any(flag.get("blocked", False) for flag in _q_state.compliance_flags):

                logger.critical(
                    f"PCI BLOCKED PRE-GENERATION → {_q_state.compliance_flags}"
                )

                yield "Response blocked due to PCI violation"

                return


            # ====================================================
            # GENERATE STREAM
            # ====================================================

            # Fix A3: bump complexity after retrieval so grounded answers
            # use GPT-5.2 rather than local LLM (local LLM ignores injected context).
            # _complexity is always defined above (before the plan/loop).
            if state.context and _complexity == "simple":
                _complexity = "medium"

            # ====================================================
            # EMPTY RAG WARNING — explicit, honest, non-negotiable
            # If we are answering a codebase question with NO retrieved
            # context, tell the user immediately. Silent hallucination
            # in a financial platform is worse than admitting uncertainty.
            # ====================================================
            _no_context_warning = ""
            if not state.context and state.repo_filter:
                _no_context_warning = (
                    f"> ⚠️ **No codebase context found** for repo `{state.repo_filter}`. "
                    f"This answer is based on AI training data only — NOT your actual codebase. "
                    f"Verify against the real code before acting on it.\n\n"
                )
                logger.warning(
                    f"EMPTY RAG WARNING — no context retrieved for repo={state.repo_filter!r}. "
                    f"Answer will be from model priors — flagging to user."
                )

            logger.info("AGENT TOOL → generate_answer_tool")

            stream = generate_answer_tool(state, self.llm)

            if stream is None:

                yield "Error generating response"

                return


            logger.info("AGENT STREAM START")

            # Emit the empty-context warning BEFORE the first token so it
            # appears at the top of the answer, not buried below.
            if _no_context_warning:
                yield _no_context_warning

            token_count = 0
            answer_buffer = []   # Point B: collect full answer for eval

            for token in stream:

                if not token:
                    continue

                if not isinstance(token, str):
                    token = str(token)

                answer_buffer.append(token)
                token_count += 1

                yield token


            logger.info(
                f"AGENT STREAM COMPLETE → tokens={token_count}"
            )

            # Snapshot the actual model label NOW — before eval threads can
            # overwrite model_router.last_model_label.  Routers read this
            # attribute instead of model_router.last_model_label directly.
            try:
                from models.model_router import model_router as _snap_mr
                self.last_run_model_label = getattr(_snap_mr, "last_model_label", "auto")
            except Exception:
                self.last_run_model_label = "auto"

            # ── Point B: Eval answer quality (true fire-and-forget via thread) ──
            # MUST run in a background thread — eval calls model_router which
            # overwrites last_model_label, corrupting the model badge in the UI.
            if answer_buffer:
                import threading as _threading
                _ans = "".join(answer_buffer)
                _ctx = [c for c in state.context if c] if state.context else []
                _sid = getattr(state, "session_id", None)
                _q   = state.question
                _tools_used = list(getattr(state, "tool_calls_made", []))
                _iters      = state.iterations

                # Snapshot model label before the thread starts — last_run_model_label
                # is already captured above (line ~1227) before any eval thread runs.
                _eval_model = self.last_run_model_label or None

                def _run_eval_b():
                    try:
                        from core.evals import eval_engine
                        eval_engine.eval_answer_quality(
                            _q, _ans, _ctx, session_id=_sid,
                            platform="agent_studio",
                            model=_eval_model,
                        )
                    except Exception as _eval_err:
                        logger.debug(f"eval_answer_quality failed (non-critical): {_eval_err}")
                _threading.Thread(target=_run_eval_b, daemon=True, name="eval-answer").start()

                # ── L3: Semantic memory extraction (fire-and-forget) ──────────
                # Extract a learned pattern from this agent run and store it
                # in semantic_memory so future similar queries benefit from it.
                def _extract_and_store_memory():
                    try:
                        from store.semantic_cache import store_semantic_memory
                        # Only store if there's meaningful content to learn from
                        if not _ans or len(_ans) < 50:
                            return
                        # Build summary — first 2 sentences of the answer or question
                        _q_short = _q[:200].strip()
                        _a_short = _ans[:400].strip()
                        _summary = f"Query: {_q_short[:100]} → {_a_short[:200]}"

                        _mtype = "tool_sequence" if _tools_used else "design_pattern"
                        _content = {
                            "question_preview": _q_short[:300],
                            "tools_used":       _tools_used[:10],
                            "iterations":       _iters,
                            "answer_len":       len(_ans),
                            "had_context":      bool(_ctx),
                        }
                        # Confidence is higher when answer came with RAG context
                        _conf = 0.85 if _ctx else 0.75
                        _u_id = (user_ctx or {}).get("user_id") if user_ctx else None
                        # Tag with rag_mode + source_repo so Generic reads
                        # can exclude codebase/KB-originated memories.
                        # These are closure-captured from run()'s locals —
                        # safe under concurrency (no shared-instance mutation).
                        _orch_rag_mode = _run_rag_mode
                        _orch_repo = _run_repo_filter
                        store_semantic_memory(
                            memory_type=_mtype,
                            summary=_summary,
                            content=_content,
                            source=_sid,
                            confidence=_conf,
                            user_id=_u_id,
                            scope_type="org",   # tool/design patterns are org-wide knowledge
                            scope_id="global",
                            rag_mode=_orch_rag_mode,
                            source_repo=_orch_repo,
                        )
                    except Exception as _me:
                        logger.debug(f"semantic memory extraction failed (non-critical): {_me}")

                _threading.Thread(
                    target=_extract_and_store_memory,
                    daemon=True,
                    name="semantic-memory-extract",
                ).start()


        except Exception as e:

            logger.error(f"AGENT FATAL ERROR → {e}")

            yield "System error occurred"


# ============================================================
# SINGLETON
# ============================================================

agent = OrchestratorAgent()