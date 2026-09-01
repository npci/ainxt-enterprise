# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Connectors-first routing — GitLab/Jira must never fall to the shell
# ============================================================
#
# REGRESSION GUARD for the "Buddy went to the command line instead of GitLab"
# bug. Three independent defects combined to produce it:
#
#   1. agents/orchestrator.py::_FILE_QUERY_RE matched the BARE verbs
#      show|list|find|open|search, so "show me my open merge requests" matched on
#      "show" and was routed to a local filesystem/shell call (list_directory on
#      the home dir, or a `grep` via search_files) instead of a connector.
#   2. That local-MCP fast-path ran BEFORE the `mode == "office"` branch, so the
#      connector planner never got a chance to see the question.
#   3. Every GitLab MR/issue tool required a `repo` param, so a repo-less question
#      ("my open MRs") had no valid call available and the model improvised.
#
# These tests pin all three fixes. agents/orchestrator.py cannot be imported in a
# bare test env (core.logger pulls in structlog), so the regexes are extracted
# from the real source via AST and exec'd in isolation — that means the tests run
# against the ACTUAL definitions, not a hand-copied mirror that could drift.
# ============================================================

import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ORCHESTRATOR_PATH = os.path.join(ROOT, "agents", "orchestrator.py")
SEED_PATH = os.path.join(ROOT, "connectors", "seed.py")
ADAPTER_PATH = os.path.join(ROOT, "connectors", "adapters", "gitlab.py")
GITLAB_TOOLS_PATH = os.path.join(ROOT, "tools", "gitlab_tools.py")
COWORK_SESSION_PATH = os.path.join(
    ROOT, "desktop", "src", "cowork", "coworkSession.js"
)


def _load_module_symbols(path, names):
    """Exec only the named top-level assignments/functions from `path`.

    Lets us test the REAL regexes and helpers without importing the module (which
    would drag in structlog/redis/HSM). Anything that fails to exec in isolation
    is skipped, so unrelated module-level code cannot break these tests.
    """
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"re": re, "os": os}
    wanted = set(names)
    for node in tree.body:
        target = None
        if isinstance(node, ast.FunctionDef):
            target = node.name
        elif isinstance(node, ast.Assign) and node.targets:
            t = node.targets[0]
            target = t.id if isinstance(t, ast.Name) else None
        if target in wanted:
            exec(compile(ast.Module([node], []), path, "exec"), ns)
    missing = wanted - set(ns)
    assert not missing, f"{path} no longer defines: {sorted(missing)}"
    return ns


@pytest.fixture(scope="module")
def orch():
    return _load_module_symbols(
        ORCHESTRATOR_PATH,
        ["_FILE_QUERY_RE", "_REMOTE_SYSTEM_RE", "_JIRA_KEY_RE",
         "_LOCAL_PATH_EVIDENCE_RE", "_is_remote_system_query"],
    )


def _takes_local_shell_path(orch, question):
    """Mirror of the plan() guard: the local-MCP filesystem/shell fast-path is
    taken only when the question is NOT about a remote work system AND it looks
    like a filesystem request. See agents/orchestrator.py::plan.
    """
    if orch["_is_remote_system_query"](question):
        return False
    return bool(orch["_FILE_QUERY_RE"].search(question))


# ── 1. The reported failures: connector questions must NOT hit the shell ─────

# Every one of these previously matched _FILE_QUERY_RE on a bare verb and was
# answered with a directory listing / grep of the user's home folder.
CONNECTOR_QUERIES = [
    "show me my open merge requests",
    "what's the status of ABC-123",
    "list my repos",
    "any tickets assigned to me?",
    "find the DB config in the payments repo",
    "show me my MRs",
    "list open issues in the upi project",
    "search the payments repo for the retry logic",
    "open PAY-4521",
    "PAY-4521",
    "what changed in MR !88",
    "show me recent commits on main",
    "find my pull requests that need review",
    "view the pipeline status for my branch",
    "explore the settlement repository",
    "list the tickets in my sprint",
]


@pytest.mark.parametrize("q", CONNECTOR_QUERIES)
def test_connector_query_never_routes_to_shell(orch, q):
    assert not _takes_local_shell_path(orch, q), (
        f"{q!r} would be answered from the local filesystem/shell. GitLab and Jira "
        f"are remote servers — this must fall through to the connector planner."
    )


@pytest.mark.parametrize("q", CONNECTOR_QUERIES)
def test_connector_query_is_recognised_as_remote(orch, q):
    assert orch["_is_remote_system_query"](q), (
        f"{q!r} was not recognised as a remote work-system question, so nothing "
        f"stops the filesystem fast-path from claiming it."
    )


# ── 2. Genuine filesystem questions must STILL work (no over-correction) ────

FILESYSTEM_QUERIES = [
    "show me the files on my Desktop",
    "list the files in ~/Downloads",
    "read ~/notes.txt",
    "cat config.yaml",
    "what's in my Documents folder",
    "list the contents of /tmp",
    "open /etc/hosts",
    "ls",
]


# Mixed signals: work-system vocabulary AND explicit local-path evidence. An actual
# path or filename is decisive — the user is pointing at a file on this machine.
MIXED_LOCAL_QUERIES = [
    "find the bug in ~/scripts/settle.py",
    "read the commit message in /tmp/msg.txt",
    "search ~/Downloads for the issue report",
    "show me the pipeline config in ~/ci/config.yml",
]


@pytest.mark.parametrize("q", MIXED_LOCAL_QUERIES)
def test_local_path_evidence_wins_over_work_vocabulary(orch, q):
    """Guards against over-correcting: a question naming a real path or filename is
    a local request even if it also says "bug", "commit", "issue" or "pipeline"."""
    assert _takes_local_shell_path(orch, q), (
        f"{q!r} names an explicit local path and must still reach local_mcp_call"
    )


@pytest.mark.parametrize("q", ["find the bug in the code", "fix this bug",
                              "tell me a story"])
def test_generic_words_are_not_work_system_signals(orch, q):
    """"bug" and "story" are ordinary English and must NOT be treated as tracker
    vocabulary — "issue"/"ticket" already cover that."""
    assert not orch["_REMOTE_SYSTEM_RE"].search(q), (
        f"{q!r} was misread as a work-system question"
    )


@pytest.mark.parametrize("q", FILESYSTEM_QUERIES)
def test_filesystem_query_still_routes_to_local(orch, q):
    assert _takes_local_shell_path(orch, q), (
        f"{q!r} is a real filesystem request and must still reach local_mcp_call — "
        f"the connectors-first guard must not break local file access."
    )


# ── 3. Bare verbs alone must not select the shell ───────────────────────────

@pytest.mark.parametrize("q", [
    "show me the summary",
    "list everything",
    "find out who owns this",
    "open the discussion",
    "search for context",
    "view my schedule",
    "explore the options",
])
def test_bare_verb_alone_is_not_a_filesystem_query(orch, q):
    """A verb with no path, extension or filesystem noun is not evidence of a
    filesystem question. These are the most common verbs in the language and must
    not hijack the router."""
    assert not orch["_FILE_QUERY_RE"].search(q), (
        f"{q!r} matched the filesystem regex on a bare verb — the original bug."
    )


# ── 4. Jira key detection, without false positives ──────────────────────────

@pytest.mark.parametrize("s", ["PAY-4521", "ABC-123", "ORG-1", "UPI2-99"])
def test_jira_key_detected(orch, s):
    assert orch["_JIRA_KEY_RE"].search(s), f"{s!r} should be read as a Jira key"


@pytest.mark.parametrize("s", [
    "utf-8", "covid-19", "top-10", "base-64", "sha-256", "x-2",
    "encode this as utf-8 please",
])
def test_jira_key_no_false_positives(orch, s):
    """The key pattern must stay CASE-SENSITIVE: with re.IGNORECASE it also
    matches utf-8 / covid-19 / top-10 and would misroute ordinary questions."""
    assert not orch["_JIRA_KEY_RE"].search(s), (
        f"{s!r} was misread as a Jira issue key"
    )


def test_jira_key_regex_is_case_sensitive(orch):
    assert not (orch["_JIRA_KEY_RE"].flags & re.IGNORECASE), (
        "_JIRA_KEY_RE must NOT use IGNORECASE — it would match utf-8, covid-19, etc."
    )


# ── 5. Ordering: office mode must win over the filesystem fast-path ─────────

def test_office_branch_runs_before_local_mcp_fastpath():
    """Defect 2. Even a perfect regex cannot help if the filesystem fast-path is
    evaluated first, so pin the ORDER of the two branches inside plan()."""
    src = open(ORCHESTRATOR_PATH, encoding="utf-8").read()
    plan_at = src.index("    def plan(self, state: AgentState)")
    body = src[plan_at:]

    office_at = body.index('if state.mode == "office":')
    fastpath_at = body.index("_FILE_QUERY_RE.search(state.question)")

    assert office_at < fastpath_at, (
        "The `mode == \"office\"` branch must run BEFORE the local-MCP filesystem "
        "fast-path in plan(); otherwise connector questions are claimed by the "
        "filesystem path and never reach _plan_office."
    )


def test_local_mcp_fastpath_is_guarded_by_remote_check():
    src = open(ORCHESTRATOR_PATH, encoding="utf-8").read()
    plan_at = src.index("    def plan(self, state: AgentState)")
    body = src[plan_at:]
    guard_at = body.index("_is_remote_system_query(state.question)")
    fastpath_at = body.index("_FILE_QUERY_RE.search(state.question)")
    assert guard_at < fastpath_at, (
        "plan() must check _is_remote_system_query BEFORE the _FILE_QUERY_RE "
        "fast-path, so GitLab/Jira questions can never take the shell route."
    )


def test_plan_has_exactly_one_office_branch():
    """Guards against a copy left behind when the branch was moved."""
    src = open(ORCHESTRATOR_PATH, encoding="utf-8").read()
    assert src.count("return self._plan_office(state)") == 1


# ── 6. Defect 3: a repo-less "my work" question must have a valid tool ──────

def _seed_gitlab_block():
    src = open(SEED_PATH, encoding="utf-8").read()
    start = src.index('"name": "gitlab",')
    return src[start:src.index("\n]", start)]


def test_cross_project_my_work_tools_exist_in_seed():
    """Without a no-project tool, "show me my open merge requests" has NO valid
    connector call — which is why the model fell back to a shell guess."""
    block = _seed_gitlab_block()
    for tool in ("gitlab_list_my_mrs", "gitlab_list_my_issues"):
        assert f'"name": "{tool}"' in block, f"{tool} missing from the seed catalog"


@pytest.mark.parametrize("tool", ["gitlab_list_my_mrs", "gitlab_list_my_issues"])
def test_cross_project_tools_require_no_params(tool):
    """These must be callable with NO arguments — that is the entire point."""
    block = _seed_gitlab_block()
    at = block.index(f'"name": "{tool}"')
    seg = block[at:at + 2000]
    req = re.search(r'"required":\s*\[([^\]]*)\]', seg)
    assert req is not None, f"{tool} has no `required` key"
    assert req.group(1).strip() == "", (
        f"{tool} must have required=[] so it answers a question that names no "
        f"project; it currently requires: {req.group(1)}"
    )


@pytest.mark.parametrize("tool", ["gitlab_list_my_mrs", "gitlab_list_my_issues"])
def test_cross_project_tools_wired_end_to_end(tool):
    """Seed definition, adapter dispatch map, and implementation must agree —
    a tool advertised but not dispatchable is worse than a missing one."""
    adapter = open(ADAPTER_PATH, encoding="utf-8").read()
    tools_src = open(GITLAB_TOOLS_PATH, encoding="utf-8").read()

    amap = dict(re.findall(r'"(gitlab_\w+)":\s*\("(gitlab_\w+)"', adapter))
    assert tool in amap, f"{tool} is not in GitLabAdapter._TOOL_MAP"

    fns = {
        n.name for n in ast.parse(tools_src).body
        if isinstance(n, ast.FunctionDef)
    }
    assert amap[tool] in fns, (
        f"_TOOL_MAP points {tool} at {amap[tool]}(), which tools/gitlab_tools.py "
        f"does not define"
    )


def test_every_seeded_gitlab_tool_is_dispatchable():
    """Drift guard: nothing may be advertised to the model that the adapter
    cannot route (it would fail at call time and push the model to the shell)."""
    block = _seed_gitlab_block()
    adapter = open(ADAPTER_PATH, encoding="utf-8").read()
    amap = dict(re.findall(r'"(gitlab_\w+)":\s*\("(gitlab_\w+)"', adapter))
    seeded = set(re.findall(r'"name": "(gitlab_\w+)"', block))
    assert seeded, "no GitLab tools found in the seed catalog — parser drift?"
    assert not (seeded - set(amap)), (
        f"seeded but not dispatchable: {sorted(seeded - set(amap))}"
    )


# ── 7. The cross-project tools behave correctly ─────────────────────────────

@pytest.fixture(scope="module")
def gl():
    return _load_module_symbols(
        GITLAB_TOOLS_PATH,
        ["_ref_project", "_my_scoped", "gitlab_list_my_mrs",
         "gitlab_list_my_issues"],
    )


def test_my_mrs_hits_instance_wide_endpoint(gl):
    """Must call /merge_requests (instance-wide, user-scoped), NOT
    /projects/:id/merge_requests — there is no project to put in the path."""
    seen = []
    gl["_get"] = lambda p: (seen.append(p) or [])
    gl["gitlab_list_my_mrs"]()
    assert seen and seen[0].startswith("/merge_requests?"), seen
    assert "/projects/" not in seen[0]
    assert "scope=assigned_to_me" in seen[0]
    assert "state=opened" in seen[0]


def test_my_issues_hits_instance_wide_endpoint(gl):
    seen = []
    gl["_get"] = lambda p: (seen.append(p) or [])
    gl["gitlab_list_my_issues"]()
    assert seen and seen[0].startswith("/issues?"), seen
    assert "/projects/" not in seen[0]
    assert "scope=assigned_to_me" in seen[0]


def test_my_mrs_state_all_omits_state_filter(gl):
    seen = []
    gl["_get"] = lambda p: (seen.append(p) or [])
    gl["gitlab_list_my_mrs"](state="all")
    assert "state=" not in seen[0], seen


def test_my_mrs_clamps_limit(gl):
    seen = []
    gl["_get"] = lambda p: (seen.append(p) or [])
    gl["gitlab_list_my_mrs"](limit=9999)
    assert "per_page=50" in seen[0], seen


def test_my_mrs_rejects_unknown_scope(gl):
    """scope goes straight into the query string — an unvetted value must not."""
    seen = []
    gl["_get"] = lambda p: (seen.append(p) or [])
    gl["gitlab_list_my_mrs"](scope="../../admin")
    assert "scope=assigned_to_me" in seen[0], seen
    assert ".." not in seen[0]


def test_my_mrs_surfaces_api_errors(gl):
    gl["_get"] = lambda p: {"error": "HTTP 401: Unauthorized"}
    with pytest.raises(RuntimeError, match="401"):
        gl["gitlab_list_my_mrs"]()


@pytest.mark.parametrize("row,expected", [
    ({"references": {"full": "acme/payments!7"}}, "acme/payments"),
    ({"references": {"full": "acme/payments#7"}}, "acme/payments"),
    ({"references": {"full": "grp/sub/proj!12"}}, "grp/sub/proj"),
    ({"web_url": "https://git.example.com/org/repo/-/merge_requests/3"}, "org/repo"),
    ({"project_id": 4412}, "4412"),
])
def test_ref_project_yields_usable_project_path(gl, row, expected):
    """The instance-wide endpoints return references.full with an item suffix
    ("acme/payments!7"). That is not a usable project_id for a follow-up call, so
    it must be stripped."""
    assert gl["_ref_project"](row) == expected


def test_my_mrs_returns_clean_project_for_followup(gl):
    gl["_get"] = lambda p: [{
        "iid": 7, "title": "Fix rounding", "state": "opened",
        "references": {"full": "acme/payments!7"},
        "author": {"username": "anshuman"},
        "web_url": "https://git/x/-/merge_requests/7",
    }]
    row = gl["gitlab_list_my_mrs"]()[0]
    assert row["project"] == "acme/payments"
    assert row["iid"] == 7


# ── 8. The prompts must name GitLab and Jira ────────────────────────────────

def _office_prompt():
    src = open(COWORK_SESSION_PATH, encoding="utf-8").read()
    start = src.index("const OFFICE_PROMPT =")
    return src[start:src.index("const FULL_POWER_PROMPT =")]


def _full_power_prompt():
    src = open(COWORK_SESSION_PATH, encoding="utf-8").read()
    start = src.index("const FULL_POWER_PROMPT =")
    return src[start:src.index("const COWORK_MCP_PATH")]


@pytest.mark.parametrize("term", [
    "gitlab_list_my_mrs", "jira_get_issue", "jira_search_issues",
    "gitlab_list_projects", "currentUser()", "merge request", "ABC-123",
])
def test_office_prompt_names_the_dev_connectors(term):
    """Defect 1's root cause: OFFICE_PROMPT gave Outlook/Teams explicit named
    rules but never mentioned GitLab or Jira at all, so the model had no reason
    to prefer those tools over its built-in shell."""
    assert term in _office_prompt(), (
        f"OFFICE_PROMPT does not mention {term!r} — the model needs the vocabulary "
        f"→ tool mapping spelled out, as it is for Outlook/Teams."
    )


@pytest.mark.parametrize("term", ["GitLab", "Jira", "gitlab_list_my_mrs"])
def test_full_power_prompt_names_the_dev_connectors(term):
    """FULL_POWER_PROMPT hands the agent a real shell and says "use these freely",
    so it especially needs the connectors-first rule."""
    assert term in _full_power_prompt(), (
        f"FULL_POWER_PROMPT does not mention {term!r}"
    )


def test_office_prompt_forbids_shell_for_remote_systems():
    prompt = _office_prompt()
    assert "NEVER use Bash" in prompt
    # It must not claim the tool is absent — it may well be in the tool list, and
    # a prompt the model can verify as false gets discounted wholesale.
    assert "You have NO terminal, NO shell, NO Bash" not in prompt, (
        "OFFICE_PROMPT must not assert the shell does not exist: on desktop the "
        "CLI's built-in Bash IS in the tool list, and a checkably-false claim "
        "undermines the rest of the prompt. Forbid its USE instead."
    )


def test_planner_prompt_has_gitlab_jira_rules():
    """The server-side planner named Outlook and Calendar but had no GitLab/Jira
    rule, so it never emitted connector_call for those systems."""
    src = open(ORCHESTRATOR_PATH, encoding="utf-8").read()
    at = src.index("AVAILABLE CONNECTOR TOOLS")
    prompt = src[at:at + 5000]
    for term in ("gitlab_list_my_mrs", "jira_get_issue", "jira_search_issues",
                 "currentUser()", "CONNECTORS FIRST"):
        assert term in prompt, f"planner prompt is missing {term!r}"


# ── 9. Silent-failure guards ────────────────────────────────────────────────

def test_pat_autoconnect_failure_is_logged_loudly():
    """A swallowed debug log here meant "GitLab tools missing" was undiagnosable:
    the agent saw an empty tool list and improvised."""
    src = open(os.path.join(ROOT, "connectors", "mcp_bridge.py"), encoding="utf-8").read()
    at = src.index("def _ensure_pat_connectors_connected")
    body = src[at:at + 3000]
    assert "logger.warning" in body, (
        "PAT auto-connect failures must log at WARNING — at debug they are "
        "invisible, and the only symptom is Buddy improvising with a shell."
    )


def test_connector_registration_failure_is_surfaced_to_user():
    src = open(COWORK_SESSION_PATH, encoding="utf-8").read()
    assert "_connectorsUnavailable = false" in src, "flag must be initialised"
    assert "this._connectorsUnavailable = true" in src, "flag must be set on failure"
    assert "if (this._connectorsUnavailable)" in src, (
        "the flag must reach the system prompt so the agent reports the outage "
        "instead of falling back to a shell/guess"
    )
