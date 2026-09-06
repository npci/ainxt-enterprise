# SPDX-License-Identifier: MIT
"""
agents/sdlc_implement_prompt.py — pure builders for the SDLC IMPLEMENT CLI prompts.

Extracted from SDLCStateMachine (2026-07-07) so the EXACT prompt the pipeline
sends to the coding CLI can be reused verbatim by an offline probe
(``scripts/sdlc_implement_probe.py``) without constructing a full state machine.
stdlib-only, import side-effect-free.

Two behaviours were added in the same change (see the SDLC CLI perf/completion
work):

1. **Termination contract.** A headless agentic CLI session ends cleanly only
   when the model stops emitting tool calls. The old prompt told the coder what
   to DO but never when to STOP, and passed no output schema — so a diligent
   Sonnet kept taking verification/polish turns until it hit ``--max-turns`` and
   the CLI reported ``error_max_turns`` even when the code was already written.
   ``implement_stop_clause()`` appends an explicit "you are done → STOP now"
   contract to every IMPLEMENT/continue prompt.

2. **skip_tests / plan reconciliation.** When ``skip_tests`` is set the guard
   says "do NOT author tests", but the injected PLAN still listed test files in
   ``new_files_needed`` and carried a full ``testing_strategy`` — a contradiction
   the coder could never cleanly resolve (it churned turns trying). Under
   ``skip_tests`` we now strip ``testing_strategy`` and any test paths from the
   injected plan so the plan and the guard agree.
"""
from __future__ import annotations

import json as _json


# ── test-path heuristic ──────────────────────────────────────────────────────

def looks_like_test_path(path: str) -> bool:
    """True if a repo-relative path is a test/spec file. Used to reconcile the
    injected PLAN with skip_tests (we must not inject "create this test file"
    into a prompt that also says "do NOT author tests"). Filename/segment-anchored
    so ordinary files that merely contain the substring "test" (e.g. ``latest.py``,
    ``contest_service.py``) are NOT stripped."""
    p = (path or "").replace("\\", "/").strip().lower()
    if not p:
        return False
    base = p.rsplit("/", 1)[-1]
    return (
        "/tests/" in p or "/test/" in p
        or p.startswith("tests/") or p.startswith("test/")
        or base.startswith("test_") or base.startswith("test-")
        or base.endswith("_test.py") or base.endswith("_tests.py")
        or ".test." in base or ".spec." in base
        or base.endswith("_spec.rb") or base.endswith("test.java")
        or base.endswith("tests.java") or base.endswith(".test.tsx")
        or base.endswith(".test.jsx") or base.endswith(".test.ts")
        or base.endswith(".test.js")
    )


# ── workspace boundary contract (the wrong-directory-edit fix) ───────────────

def workspace_boundary_clause(workspace_root: str, *, deps_dirname: str = "") -> str:
    """The ABSOLUTE-workspace-path directive prepended to every IMPLEMENT / continue /
    fix-round prompt. Fixes the class of failure where the headless coder, given only
    repo-relative paths and a vague "the repository checkout is your working directory",
    guessed an absolute prefix (``/root/...``), fell back to a filesystem-wide
    ``find /``, discovered the LIVE application tree on the same host, ``cd``'d into it,
    and wrote every Edit there — never touching the isolated run workspace.

    Naming the one legal root explicitly, restating that plan paths are RELATIVE to it,
    and forbidding filesystem-wide search / absolute prefixes removes every step of that
    escape path. Empty ``workspace_root`` → empty clause (nothing to anchor to), so this
    is safe to call unconditionally.

    ``deps_dirname`` (keyword-only, default "") carves out ONE additional read-scope
    exception for a multi-repo run: the dependency checkouts staged by
    ``agents/multi_repo_workspace.py`` live physically INSIDE the workspace root at
    ``{deps_dirname}/``, but the language above ("the ONLY tree you may read... every
    plan path is relative to this root") would otherwise read as excluding them, since
    no PLAN path ever points there. Leave empty for a single-repo run — the returned
    string is then byte-identical to calling this function with no ``deps_dirname`` at
    all, so existing prompts/callers are unaffected."""
    ws = (workspace_root or "").strip()
    if not ws:
        return ""
    _deps_bullet = ""
    if deps_dirname:
        _deps_bullet = (
            f"- `{deps_dirname}/` inside this root holds vendored dependency-repo checkouts "
            "staged for this run — it is INSIDE the workspace boundary above, so reading it "
            "is in-scope and expected, not a violation of this contract.\n"
        )
    return (
        "\n\nWORKSPACE BOUNDARY — ABSOLUTE AND NON-NEGOTIABLE:\n"
        "Your working directory — and the ONLY tree you may read, search, create, or "
        "modify — is:\n"
        f"    {ws}\n"
        "You are already CWD'd into this directory. Every path in the PLAN below is "
        "RELATIVE to this root: a file the plan calls `db/models.py` lives at "
        "`db/models.py` INSIDE this directory — never at `/db/models.py`, `/root/...`, "
        "or any other absolute location.\n"
        "- NEVER read, edit, or create a file whose absolute path falls outside this "
        "root, and NEVER prefix a plan path with an absolute directory of your own "
        "guessing.\n"
        "- NEVER run `find /`, `cd /`, `cd ~`, `cd ..` past this root, or any command "
        "that searches or navigates outside it. To locate a file, Grep/Glob WITHIN this "
        "directory (or run shell commands relative to it — you are already here).\n"
        "- A separate copy of this application may exist elsewhere on this host: that is "
        "the LIVE production system and is STRICTLY OFF-LIMITS. If a file you expect "
        "seems missing, it is missing from THIS workspace — create it HERE per the plan; "
        "do NOT go hunting for it elsewhere on the filesystem.\n"
        "- When you already know which file holds something, grep for the specific "
        "symbol/line first and read only that range — don't read an entire large file "
        "just to locate one function or line.\n"
        f"{_deps_bullet}"
    )


# ── search discipline (CLASSIFY / PLAN read-only exploration) ────────────────

def search_discipline_clause() -> str:
    """Search-discipline contract for the read-only CLASSIFY / PLAN phases.

    Prevents the degenerate loop where a headless model re-runs the SAME failing
    exact-name search over and over (observed: a real file
    ``SecureNxtRedisClientFactory`` searched 181x as the misspelled
    ``SecureNextRedisClientFactory``, never trying a variant). Tool- and
    language-agnostic, so it is safe to append to any read-only exploration prompt
    regardless of which search tools the profile grants."""
    return (
        "\n\nSEARCH DISCIPLINE — how to locate files and symbols efficiently:\n"
        "- Use the native Grep tool for content/symbol searches and Glob for file-pattern "
        "discovery — they are faster and more precise than shelling out to bash grep/find.\n"
        "- NEVER repeat an identical search that already returned no results. Re-running the "
        "exact same query cannot succeed and only burns turns.\n"
        "- A name in a ticket may be misspelled, abbreviated, or differ in case from the real "
        "code (e.g. 'Nxt' vs 'Next', 'Svc' vs 'Service'). After ONE exact-match miss, "
        "immediately CHANGE strategy: search case-insensitively with Grep, search a distinctive "
        "SUBSTRING or stem (e.g. 'RedisClientFactory' instead of the full class name), or "
        "use Glob to list the likely directory — do not just retype the same string.\n"
        "- Give any single target AT MOST 3 attempts total (1 exact + 2 varied). If it is "
        "still not found, STOP searching for it: proceed with the best evidence you have, and "
        "if the target is essential and genuinely absent, record it as an open_question rather "
        "than looping.\n"
        "- When a search reveals a large result set, spawn a focused sub-agent to explore a "
        "specific subtree or module in parallel rather than reading everything sequentially.\n"
    )


# ── identifier / domain-qualifier fidelity (PLAN — pick the RIGHT symbol) ─────

def identifier_fidelity_clause() -> str:
    """Domain-qualifier binding contract for the PLAN phase.

    Prevents choosing a look-alike symbol whose qualifier contradicts the ticket —
    the failure where the target scope holds two symbols differing only by a domain
    qualifier and the plan/coder picks the one biased by the enclosing class name
    rather than the one the ticket describes. Tool- and language-agnostic; the prompt
    text is deliberately generic (no example identifiers baked in)."""
    return (
        "\n\nIDENTIFIER & DOMAIN-QUALIFIER FIDELITY — pick the symbol the ticket actually means:\n"
        "- Tickets describe intent with domain terms and QUALIFIERS (a role, a direction, a "
        "side, a phase, an entity). The code symbols you read and modify MUST carry the same "
        "qualifier the ticket uses — even when the ticket names the concept rather than the "
        "exact identifier.\n"
        "- Use Grep to search for candidate symbols by qualifier substring and Glob to scope "
        "the search to the relevant package/module — do not rely on a single exact-name lookup.\n"
        "- When the target scope holds TWO OR MORE symbols that differ only by such a "
        "qualifier, choose the one matching the ticket's terminology. Record the CHOSEN "
        "symbol, the REJECTED look-alike, and a one-line reason inside implementation_spec, "
        "and restate the qualifier in solution_approach so downstream review can verify it.\n"
        "- NEVER select a symbol whose qualifier is absent from — or contradicts — the ticket, "
        "and NEVER let the enclosing class/file name (which may carry a different qualifier) "
        "override the ticket's own terminology.\n"
    )


# ── governance awareness (PART 1 — always-on, fail-safe) ─────────────────────

def governance_pointer_clause(governance_block: str = "") -> str:
    """Appended to PLAN / IMPLEMENT / continue / fix prompts with the EA/IS/DPDP
    governance skills' standards INLINED (PART 1 of the governance work).
    ``governance_block`` already carries each selected skill's SKILL.md content
    (see ``agents.sdlc_governance.engine.resolve_awareness``) — there is no CLI
    plugin/skill-loading mechanism (confirmed 2026-07-20: neither a `--plugin`/
    `--skill` flag nor a `/plugin`/`/skill` slash command loads anything on the
    deployed binary in headless mode), so the prompt text is the only channel
    that reaches the session.

    Fail-safe by construction: ``governance_block`` is empty whenever no bundle
    resolves or awareness is disabled, so the clause collapses to "" and the
    prompt is byte-identical to a run with no governance — keeping the offline
    probe (scripts/sdlc_implement_probe.py) and every existing run unaffected."""
    gb = (governance_block or "").strip()
    if not gb:
        return ""
    return (
        "\n\nGOVERNANCE AWARENESS:\n"
        "EA / IS / DPDP governance standards are inlined below for this session. Honour "
        "them while you plan and write code — treat them as binding constraints, not "
        "suggestions:\n"
        f"{gb}\n"
    )


# ── dependent-repo awareness (multi-repo CLI visibility) ──────────────────────

def dependent_repos_clause(dep_rows: list, deps_dirname: str = ".sdlc_deps") -> str:
    """Tells PLAN/IMPLEMENT that OTHER repos this change depends on are checked out
    inside the workspace, at ``{deps_dirname}/{slug}`` (``slug`` = ``repo`` with "/"
    replaced by "__" — replicated locally; this module never imports
    ``agents.multi_repo_workspace`` to stay pure/side-effect-free).

    Why this exists: ``workspace_boundary_clause()`` tells the coder the workspace
    root is "the ONLY tree you may read... every plan path is relative to this
    root". Read literally (and it must be read literally — that clause guards
    against a real production incident), a model would treat anything the PLAN
    never names — including the physically-present ``{deps_dirname}/`` tree — as
    out of scope. Without this clause, a multi-repo run's staged dependency
    checkouts are invisible to the model and it invents/guesses a dependent repo's
    API instead of reading the real one.

    ``dep_rows`` are ``sdlc_run_repos`` dicts (``repo``, ``kind``, optionally
    ``ref``/``ref_sha``). Rows with ``kind == "primary"`` are the main repo itself
    and are filtered out. Returns "" when nothing remains — single-repo prompts
    must stay byte-identical to today."""
    rows = [
        r for r in (dep_rows or [])
        if isinstance(r, dict) and r.get("repo") and r.get("kind") != "primary"
    ]
    if not rows:
        return ""
    lines = []
    for r in rows:
        repo = str(r.get("repo"))
        kind = str(r.get("kind") or "editable")
        # Must match agents/multi_repo_workspace.py's `_slug_for` exactly (the
        # authority to keep this in sync with) — not imported because this module
        # is deliberately pure and dependency-free.
        slug = repo.replace("/", "__").replace("..", "_").strip()
        ref = r.get("ref") or r.get("ref_sha") or ""
        ref_part = f" @ {ref}" if ref else ""
        lines.append(f"  - {repo}{ref_part} ({kind}) -> {deps_dirname}/{slug}")
    listing = "\n".join(lines)
    return (
        "\n\nDEPENDENT REPOSITORIES:\n"
        "This change depends on other repositories. Read-only or editable checkouts of "
        f"each are staged INSIDE your workspace root, under `{deps_dirname}/`, one "
        "directory per dependency:\n"
        f"{listing}\n"
        "Rules:\n"
        "- These directories contain the real source of the repos this change depends "
        "on. Read them to obtain REAL signatures, types, constants, and behavior — "
        "never infer or invent a dependent API. If you need to know what a dependent "
        "class/method does, open its source here.\n"
        "- `compile-only` deps are READ-ONLY, enforced at the filesystem level — a "
        "write attempt will fail with EACCES. Do not try.\n"
        "- `editable` deps MAY be modified, and any edits you make there become a "
        "separate sibling merge request.\n"
        "- The dep tree is NOT part of the primary change. Never restructure, "
        "refactor, or \"fix\" a dep just to make the primary compile.\n"
    )


# ── unplanned-change declaration (scope-guard → REVIEW handoff) ───────────────

# Delimiters the pipeline scans for to extract the coder's structured declaration
# of any file it touched OUTSIDE the plan. Kept as module constants so the parser
# in the state machine (agents/sdlc_state_machine._parse_unplanned_changes) and the
# prompt text below can never drift apart. Deliberately unique/unlikely-in-code so
# the extraction never collides with ordinary braces in the coder's prose summary.
UNPLANNED_CHANGES_BEGIN = "<<<SDLC_UNPLANNED_CHANGES>>>"
UNPLANNED_CHANGES_END = "<<<END_SDLC_UNPLANNED_CHANGES>>>"


def unplanned_changes_clause() -> str:
    """Contract that turns SCOPE rule 7 ("edit an unlisted file if the spec requires
    it, and note it") into a MACHINE-READABLE declaration the pipeline can act on.

    The deterministic scope guard hard-blocks any file touched outside the plan's
    ``files_to_change`` / ``new_files_needed``. This clause gives the coder the one
    sanctioned way to keep such a necessary change: DECLARE it — path, whether it is
    a new file or a modification, and WHY it is required for this change — in a
    uniquely-delimited JSON block at the very end of the final summary. A declared,
    justified change is allowed through to the code reviewer (which judges it on
    merit); an UNDECLARED unplanned change is bounced back for one correction round
    and then blocked. Emit an EMPTY list (or omit the block) when every file you
    touched is already in the plan — that is the normal case and is not penalised."""
    return (
        "\n\nUNPLANNED-CHANGE DECLARATION — REQUIRED WHEN YOU GO OUTSIDE THE PLAN:\n"
        "The plan's `files_to_change` and `new_files_needed` define your sanctioned scope. "
        "If — and only if — correctly implementing the plan FORCES you to create or modify a "
        "file that is in NEITHER list (a caller, a shared helper, a config, a new module), you "
        "MUST declare each such file so the code reviewer can approve it. Do NOT silently leave "
        "an unlisted file changed: an undeclared out-of-scope change is bounced back to you.\n"
        "Declare them as the LAST thing in your final summary, as a single JSON object between "
        "these exact delimiter lines (nothing else on those lines):\n"
        f"{UNPLANNED_CHANGES_BEGIN}\n"
        '{"unplanned_changes": [{"path": "<repo-relative path>", "kind": "new|modify", '
        '"reason": "<why this file MUST change to implement the plan>"}]}\n'
        f"{UNPLANNED_CHANGES_END}\n"
        "Rules: use repo-relative paths exactly as they appear in the workspace; `kind` is "
        "\"new\" for a file you created and \"modify\" for an existing file you changed; every "
        "entry MUST carry a concrete `reason` tied to the plan. If you did not touch any file "
        "outside the plan, emit an empty list (`{\"unplanned_changes\": []}`) or omit the block "
        "entirely — never invent entries to pad it."
    )


# ── termination contract (the core completion fix) ───────────────────────────

def implement_stop_clause(
    done_condition: str = "the plan is fully implemented and the code compiles",
) -> str:
    """The explicit STOP contract appended to every IMPLEMENT / continue / fix-round
    prompt. This is what lets a session end as soon as the work is done instead of
    running to the ``--max-turns`` cap.

    ``done_condition`` names the terminal state in the coder's own terms so the same
    contract fits both a full IMPLEMENT ("the plan is fully implemented …") and a
    REVIEW fix round ("the flagged issues are fixed …")."""
    return (
        "\n\nTERMINATION CONTRACT — READ CAREFULLY:\n"
        f"The moment {done_condition} (for interpreted "
        "languages with no build/compile step, once the files you changed import/parse "
        "cleanly), you are DONE. Immediately STOP: emit a one- or two-sentence summary of "
        "the files you changed and END your turn. Do NOT re-run the build 'just to confirm', "
        "do NOT re-read files you already edited, do NOT hunt for extra improvements, "
        "refactors, or edge cases, and do NOT expand scope. Finishing in as few turns as "
        "possible is the correct, expected outcome — leaving turns unused is a success, not a "
        "failure. Continuing to take tool-call turns after the work is complete is a bug."
    )


# ── execution disciplines (the timeout / dropped-work fix) ────────────────────

def _efficiency_disciplines() -> str:
    """Generic, language-agnostic execution disciplines prepended to every IMPLEMENT
    prompt. They target the dominant failure mode of a headless coding session under a
    hard wall-clock cap: re-processing large files (repeated whole-file reads) and one
    round-trip per single edit, which inflate the per-turn context until the run is
    SIGKILLed mid-change with the last-scheduled file (often the UI) left unfinished.

    Nothing here is task- or repo-specific — all specifics come from the injected PLAN,
    so the block is safe to prepend to every run regardless of language or toolchain.
    Responsibilities are partitioned so the three prompt sections never contradict:
    termination is owned by ``implement_stop_clause()``, build depth by ``_guard_text()``,
    and read/edit/sequencing discipline by this block."""
    return (
        "EXECUTION DISCIPLINE — you run under a HARD wall-clock time limit; wall time and "
        "turn count, is the binding constraint. Most timeouts come from re-processing large "
        "files, not from hard reasoning. Derive every specific (language, files, edit sites) "
        "from the PLAN below, and follow these exactly:\n\n"
        "READ\n"
        "0. Budget AT MOST ~8-10 read-only calls (Read/Grep/Glob) before your FIRST Edit/Write. "
        "The PLAN below already carries the edit_anchors, snippets, and signatures_needed it "
        "discovered — do NOT re-derive from scratch what the plan already gives you. When the plan "
        "supplies a snippet/signature for an edit site, edit against it directly instead of "
        "opening the file to rediscover it.\n"
        "1. Read each file AT MOST ONCE before editing it. Do NOT re-read a file to reconfirm "
        "positions after an edit — trust anchor strings, not line numbers. Any line numbers in "
        "the plan are approximate hints only and may be wrong against the live tree.\n"
        "2. For a large file do NOT read the whole file: Grep for a distinctive anchor near your "
        "target, then read a narrow (~40-line) window around it. If a file's contents are already "
        "inlined above, use those and do not re-read it.\n"
        "3. Open only files the plan requires. Do not browse for context you already have from the "
        "spec. If the plan says a file needs no code change, do not open it.\n\n"
        "EDIT\n"
        "4. Use parallel edits: issue ALL independent file changes in parallel in a single turn. "
        "Only sequence edits when a later edit depends on the result of an earlier one. "
        "Parallel edits save multiple round-trip turns and are the primary way to stay within "
        "the wall-clock cap on multi-file changes.\n"
        "5. Anchor every edit on a unique surrounding string, not a line number. When the plan "
        "provides `edit_anchors` for a file, use those exact strings as your match targets.\n"
        "6. Make the smallest change that fully satisfies the spec — no refactors, reformatting, "
        "or behavior beyond what the plan states.\n\n"
        "SCOPE\n"
        "7. The plan's `files_to_change` may be incomplete: if the spec logically requires editing "
        "a file not listed (a caller, a shared helper, a config), make that change and note it. Do "
        "not skip a required change because the file is unlisted; do not add changes the spec does "
        "not require.\n\n"
        "SELF-CHECK\n"
        "8. Before finishing, do ONE Grep pass to confirm each required change from the spec is "
        "present. Do NOT re-read edited files to verify — a targeted Grep per change is enough.\n\n"
    )


# ── guard text (verification depth) ──────────────────────────────────────────

def _guard_text(*, skip_tests: bool) -> str:
    """The verification-depth guard. The 'run the build' phrasing is softened so a
    repo with no build/compile step (e.g. plain Python) does not give the coder a
    meaningless never-satisfied loop — the STOP contract makes the terminal condition
    explicit regardless."""
    if skip_tests:
        return (
            "Write the code per the plan. Then, IF the project has a build/compile step, run it "
            "until it is GREEN (a repo with no build step needs only a clean import/parse of the "
            "files you changed). Tests are skipped for this run — do NOT author or run tests. "
            "Make the smallest change that fully satisfies the plan; do not touch unrelated files."
        )
    # Default: compile-green only. Author the tests the plan calls for but do NOT
    # run/iterate the suite here — the post-gate TEST_VERIFY phase runs it.
    return (
        "Write the code per the plan and author any NEW test files the plan's testing "
        "strategy calls for. Then, IF the project has a build/compile step, run ONLY the build "
        "until it is GREEN (a repo with no build step needs only a clean import/parse of the "
        "files you changed). Do NOT run or iterate the test suite — a dedicated verification "
        "phase runs the tests after this step. "
        "CRITICAL: never weaken, delete, or edit EXISTING tests. You MAY add NEW test files. "
        "Make the smallest change that fully satisfies the plan; do not touch unrelated files."
    )


# ── plan spec assembly ───────────────────────────────────────────────────────

_SPEC_KEYS = [
    "files_to_change", "new_files_needed", "edit_anchors", "snippets",
    "signatures_needed", "implementation_spec",
    "solution_approach", "implementation_plan", "code_structure",
    "testing_strategy",
]


def _spec_from_plan(plan: dict, *, skip_tests: bool) -> dict:
    """Project the plan down to the keys the coder needs. Under skip_tests, drop
    the testing strategy and any test paths so the injected plan does not
    contradict the 'do NOT author tests' guard."""
    spec = {k: plan.get(k) for k in _SPEC_KEYS if plan.get(k)}
    if skip_tests and isinstance(spec, dict):
        spec.pop("testing_strategy", None)
        for _lk in ("files_to_change", "new_files_needed"):
            _vals = spec.get(_lk)
            if isinstance(_vals, list):
                _kept = [p for p in _vals
                         if not (isinstance(p, str) and looks_like_test_path(p))]
                if _kept:
                    spec[_lk] = _kept
                else:
                    spec.pop(_lk, None)
    return spec


def build_implement_prompt(
    plan: dict,
    *,
    language: str,
    skip_tests: bool,
    drives_tests_green: bool = False,
    feedback: str = "",
    files_block: str = "",
    workspace_root: str = "",
    governance_block: str = "",
    dep_block: str = "",
) -> str:
    """Assemble the holistic IMPLEMENT prompt from the approved PLAN. Pure — the
    caller supplies ``feedback`` (corrective engineer feedback on a go-back),
    ``files_block`` (B(ii) warm-start file inlining), ``workspace_root`` (the
    ABSOLUTE path of the isolated run checkout), ``governance_block`` (PART 1
    awareness pointer text; empty when no governance bundle is loaded), and
    ``dep_block`` (pre-rendered ``dependent_repos_clause()`` output; empty for a
    single-repo run) already resolved."""
    plan = plan or {}
    spec = _spec_from_plan(plan, skip_tests=skip_tests)
    fb_block = (
        "=== CORRECTIVE ENGINEER FEEDBACK FROM THE PREVIOUS ATTEMPT — HIGHEST PRIORITY ===\n"
        f"{feedback.strip()}\n"
        "=== END FEEDBACK ===\n\n"
    ) if (feedback or "").strip() else ""
    _deps_dirname = ".sdlc_deps" if dep_block else ""
    return (
        f"{fb_block}"
        "You are an autonomous senior engineer implementing an APPROVED implementation plan.\n"
        f"Language: {language or 'unknown'}."
        f"{workspace_boundary_clause(workspace_root, deps_dirname=_deps_dirname)}"
        f"{dep_block}"
        f"{governance_pointer_clause(governance_block)}\n\n"
        f"{_efficiency_disciplines()}"
        f"APPROVED PLAN:\n{_json.dumps(spec, indent=2, default=str)}\n\n"
        f"{files_block}"
        f"{_guard_text(skip_tests=skip_tests)}"
        f"{unplanned_changes_clause()}"
        f"{implement_stop_clause()}"
    )


# ── REVIEW fix-round prompt ───────────────────────────────────────────────────

def _fix_round_guard_text(*, skip_tests: bool) -> str:
    """Verification-depth guard for a REVIEW fix round. Mirrors ``_guard_text``:
    compile-only by default (the suite runs post-gate), so a fix round does not
    churn verification turns until ``--max-turns``."""
    if skip_tests:
        return (
            "Then, IF the project has a build/compile step, run it until it is GREEN (a repo "
            "with no build step needs only a clean import/parse of the files you changed). "
            "Tests are skipped for this run — do NOT author or run tests."
        )
    return (
        "Then, IF the project has a build/compile step, run ONLY the build until it is GREEN "
        "(a repo with no build step needs only a clean import/parse of the files you changed). "
        "Do NOT run or iterate the test suite — a dedicated verification phase runs the tests "
        "after this step. Never weaken, delete, or edit EXISTING tests; you MAY add NEW test files."
    )


def build_fix_round_prompt(
    feedback: str,
    *,
    solution_approach: str = "",
    skip_tests: bool,
    drives_tests_green: bool = False,
    workspace_root: str = "",
    governance_block: str = "",
    dep_block: str = "",
) -> str:
    """The one bounded REVIEW fix-round prompt. Mirrors the IMPLEMENT prompt: a
    verification-depth guard that respects skip_tests, the workspace-boundary
    contract, plus the explicit STOP/termination contract so the fix round ends as
    soon as the flagged issues are resolved and the build is green — instead of
    running to ``--max-turns``.

    ``feedback`` is the pre-rendered reviewer-feedback block (structured blocking
    issues and/or free-text notes); the caller guarantees it is non-empty.
    ``governance_block`` carries PART 1 awareness (empty when no bundle loaded).
    ``dep_block`` carries ``dependent_repos_clause()`` output (empty for a
    single-repo run) so a REVIEW fix round does not lose dep awareness."""
    _deps_dirname = ".sdlc_deps" if dep_block else ""
    return (
        "A senior code reviewer flagged BLOCKING issues in your diff. Fix ONLY these issues "
        "in the CODE; do NOT weaken or edit existing tests, and do NOT expand scope."
        f"{workspace_boundary_clause(workspace_root, deps_dirname=_deps_dirname)}"
        f"{dep_block}"
        f"{governance_pointer_clause(governance_block)}\n"
        f"{_fix_round_guard_text(skip_tests=skip_tests)}\n\n"
        f"{feedback}\n\n"
        f"Solution approach (context):\n{solution_approach or ''}"
        f"{implement_stop_clause(done_condition='the flagged issues are fixed and the code compiles')}"
    )


def build_continue_prompt(*, skip_tests: bool, drives_tests_green: bool = False, workspace_root: str = "", governance_block: str = "", dep_block: str = "") -> str:
    """The bounded auto-continue / manual-resume prompt: finish what's left on the
    UNTOUCHED workspace, honour skip_tests (the old prompt hard-coded 'author the
    tests' even when tests were skipped), and STOP as soon as the plan is done.
    ``governance_block`` carries PART 1 awareness (empty when no bundle loaded).
    ``dep_block`` carries ``dependent_repos_clause()`` output (empty for a
    single-repo run)."""
    if skip_tests:
        body = (
            "Continue implementing the approved plan in THIS workspace. Some files may already "
            "be written — inspect the current state and finish any remaining files from "
            "files_to_change / new_files_needed. Do NOT author or run tests. Do not restart "
            "from scratch or expand scope."
        )
    else:
        body = (
            "Continue implementing the approved plan in THIS workspace. Some files may already "
            "be written — inspect the current state, finish any remaining files from "
            "files_to_change / new_files_needed, and author any NEW test files the plan's "
            "testing strategy calls for. Do NOT run the test suite (a later phase does). Never "
            "weaken or edit EXISTING tests. Do not restart from scratch or expand scope."
        )
    _deps_dirname = ".sdlc_deps" if dep_block else ""
    return (
        workspace_boundary_clause(workspace_root, deps_dirname=_deps_dirname).lstrip("\n")
        + dep_block
        + governance_pointer_clause(governance_block)
        + body
        + unplanned_changes_clause()
        + implement_stop_clause()
    )
