# SPDX-License-Identifier: Apache-2.0
# ============================================================
# CODING STATE MACHINE (three-phase CLI cutover, 2026-07-01)
#
# Pre-gate (mode="pregate"):
#   IMPLEMENT  (one CLI Sonnet session: code + tests + drive green)
#     └─► REVIEW  (platform reviewer over the diff)
#     └─► finalize → write VERIFIED_DIFF → HITL code/solution-approval gate
#
# Post-gate (mode="postgate", after approval):
#   APPLYING   (deterministic apply + base_sha re-verify + auto-rebase)
#     └─► TEST_VERIFY  (re-run the unit tests; red → SUSPEND / regate)
#     └─► SLT_RUNNING  (service-level tests; red → SUSPEND)
#     └─► COMMITTING ─► AWAITING_PR_APPROVAL
#             (if merge conflict) ─► MERGE_CONFLICT (HITL) ─► COMMITTING
#
# An optional GOVERNANCE end-gate (author-triggered) runs after the MR.
# The legacy IDLE→CODING→REVIEWING→REVIEW_GATE→TESTING→COMPLETION_REVIEW flow
# with parallel coder/reviewer teams was removed in the cutover.
# ============================================================

import os
import re
import json
import time

from core.logger import logger, bind_context, clear_bound_context
from core.config import (
    REDIS_HOST as _REDIS_HOST,
    REDIS_PORT as _REDIS_PORT,
    SDLC_BRANCH_PREFIX as _BRANCH_PREFIX,
    SDLC_RUN_ID_PREFIX as _RUN_ID_PREFIX,
)
from core.model_registry import CLAUDE_PRIMARY_MODEL as _CLAUDE_PRIMARY_MODEL
from store.sdlc_store import get_run, update_run_state, add_run_event


def _s(item) -> str:
    """Coerce any LLM output item (str, dict, list, None) to a plain string.
    Identical to _s() in sdlc_pipeline — duplicated here to avoid circular imports."""
    if item is None:
        return ""
    if isinstance(item, str):
        return item
    if isinstance(item, list):
        return "; ".join(_s(x) for x in item if x is not None and x != "")
    if isinstance(item, dict):
        return (item.get("path") or item.get("file") or item.get("name")
                or item.get("step") or item.get("description")
                or item.get("text") or item.get("value") or item.get("content")
                or item.get("change") or str(item))
    return str(item)


def _sanitize_for_api(text: str) -> str:
    """Strip control/breaking characters before sending to GitLab/Jira/Confluence."""
    try:
        from core.prompt_sanitizer import sanitize
        return sanitize(text)
    except Exception:
        return text

_CODE_FIRST_LINE_RE = __import__("re").compile(
    r"^\s*("
    r"package\s+|import\s+|from\s+\S+\s+import\s+|require\s*\(|using\s+|namespace\s+"
    r"|#[!#]|#\s+|//|/\*\*?|\*[\s/]|<!--"
    r"|@\w+"
    r"|(?:public|private|protected|internal|static|abstract|final|override"
    r"|sealed|readonly|async|class|interface|enum|struct|record|trait|impl"
    r"|fn|func|def|function|sub|const|let|var|val|export|module|pragma)\s"
    r"|\{|\[|<\?|<!"
    r"|#!/"
    r'|"""|\'\'\')'
)


# ── LLM shortcut ──────────────────────────────────────────────

def _llm(prompt: str, hint: str = "solution") -> str:
    """
    SDLC model shortcut. Routes to the caller's tier (the `hint`) — pass a
    stage-resolved hint, e.g. sdlc_stage_hint("coder"). Default "solution"
    (Opus 4.7 when ENABLE_OPUS=true, else Sonnet). Cross-provider fallback is
    GPT-5.4 (medium tier), matching CLAUDE.md and sdlc_pipeline._llm. The hint
    may legitimately be a local/text tier for mechanical stages.
    Tracks tokens and cost for HOD budget deduction via sdlc_budget_tracker.
    """
    from models.model_router import model_router
    # Honor the caller's hint (R2 — previously this dead-ignored `hint` and
    # forced every call onto the solution/Opus tier). GPT-5.4 (medium) fallback.
    _model_used = hint or "solution"
    try:
        result = model_router.generate(prompt, model_hint=_model_used)
        if result and result.strip():
            pass
        else:
            raise ValueError("empty response from Claude")
    except Exception as _claude_err:
        logger.warning(f"[SDLC] primary tier '{_model_used}' unavailable ({_claude_err}) — falling back to GPT-5.4 (medium)")
        result = model_router.generate(prompt, model_hint="medium")  # GPT-5.4
        _model_used = "medium"

    # Token + cost (char/4 estimate, same as sdlc_pipeline._llm for HOD-rollup
    # consistency; rate from the single-source-of-truth helper — R3).
    from core.model_registry import tier_cost_per_1m
    _tokens_in  = len(prompt) // 4
    _tokens_out = len(result) // 4 if result else 0
    _rate_in, _rate_out = tier_cost_per_1m(_model_used)
    _cost = (_tokens_in / 1_000_000 * _rate_in) + (_tokens_out / 1_000_000 * _rate_out)
    try:
        from agents.sdlc_pipeline import _cv_run_id as _sm_cv_run_id
        from services.sdlc_budget_tracker import record_llm_cost as _rec_cost
        _rec_cost(_tokens_in, _tokens_out, round(_cost, 6), run_id=_sm_cv_run_id.get())
    except Exception:
        pass
    return result


def _run_sdlc_agent(agent_name: str, task: str) -> str:
    """Run a named SDLC agent via AgentRunner — real Claude tool-use loop."""
    from agents.sdlc_pipeline import _run_sdlc_agent as _pipeline_run
    return _pipeline_run(agent_name, task)


def _parse_json(text: str) -> dict:
    """
    Robust JSON extractor — handles:
      1. Plain JSON string
      2. JSON inside ```json ... ``` fences
      3. JSON inside any ``` ... ``` fence
      4. Raw {...} block scan (largest first)
    Falls back to {"raw": text} so callers can still access the LLM output.
    """

    text = text.strip()

    # 1 — plain JSON
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except Exception:
        pass

    # 2 — JSON inside ```json...``` or ``` ... ``` fence
    for fence_re in [r"```json\s*(\{[\s\S]+?\})\s*```", r"```\w*\s*(\{[\s\S]+?\})\s*```"]:
        m = re.search(fence_re, text)
        if m:
            try:
                result = json.loads(m.group(1))
                if isinstance(result, dict):
                    return result
            except Exception:
                pass

    # 3 — scan for all {...} blocks, try largest first
    candidates = re.findall(r"\{[\s\S]+\}", text)
    for candidate in sorted(candidates, key=len, reverse=True)[:3]:
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except Exception:
            pass

    return {"raw": text}


def _parse_unplanned_changes(text: str) -> list:
    """Extract the coder's structured out-of-scope declarations from its final
    IMPLEMENT/continue message.

    The coder is instructed (agents.sdlc_implement_prompt.unplanned_changes_clause)
    to emit, at the END of its summary, a JSON object between the
    ``UNPLANNED_CHANGES_BEGIN`` / ``UNPLANNED_CHANGES_END`` delimiter lines. Parsing
    is anchored on those unique delimiters — NOT ``_parse_json``'s largest-brace
    scan — because a coding session's final message is prose about code and routinely
    contains stray ``{...}`` that a bare scan would mis-grab.

    Returns a normalized list of ``{"path", "kind", "reason"}`` dicts (only entries
    carrying a non-empty path AND a non-empty reason survive — an entry the coder
    left unjustified is not a valid excuse). Returns ``[]`` on absence, malformed
    JSON, or any error: a missing/garbled declaration must degrade to "nothing
    declared" (→ the guard's undeclared path), never raise."""
    from agents.sdlc_implement_prompt import (
        UNPLANNED_CHANGES_BEGIN, UNPLANNED_CHANGES_END,
    )
    try:
        t = text or ""
        start = t.rfind(UNPLANNED_CHANGES_BEGIN)
        if start < 0:
            return []
        start += len(UNPLANNED_CHANGES_BEGIN)
        end = t.find(UNPLANNED_CHANGES_END, start)
        block = (t[start:end] if end >= 0 else t[start:]).strip()
        if not block:
            return []
        parsed = _parse_json(block)
        raw_items = parsed.get("unplanned_changes") if isinstance(parsed, dict) else None
        if not isinstance(raw_items, list):
            return []
        out = []
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            path = str(it.get("path") or "").strip()
            reason = str(it.get("reason") or "").strip()
            if not path or not reason:
                continue
            kind = str(it.get("kind") or "").strip().lower()
            out.append({
                "path": path,
                "kind": kind if kind in ("new", "modify") else "modify",
                "reason": reason,
            })
        return out
    except Exception:
        return []


# Directory segments that mark a file as a test/SLT by convention (any ancestor
# segment matching → the file is a test). Compared case-insensitively.
_TEST_DIR_SEGMENTS = {
    "test", "tests", "__tests__", "spec", "specs", "testing",
    "slt", "e2e", "integration-test", "integration-tests",
}


def _is_test_path(path: str) -> bool:
    """Convention-based test/SLT file detector across the languages the pipeline
    supports (python / java / kotlin / go / js / ts + SLT).

    Replaces the old naive ``any(kw in path.lower() for kw in ("test","spec","slt"))``
    substring scan, which mislabeled PRODUCTION files like ``latest_config.py``,
    ``contest.py``, ``manifest.py`` and ``inspector.py`` as tests — tripping the
    IMPLEMENT test-scrutiny / guardrail path on code that is not a test at all.
    Detection is by DIRECTORY convention (a test dir anywhere in the path) or
    FILENAME convention (language-specific test naming), never a bare substring."""
    raw = (path or "").replace("\\", "/").strip()
    if not raw:
        return False
    segs = [s for s in raw.split("/") if s]
    if not segs:
        return False
    raw_name = segs[-1]
    name = raw_name.lower()
    # 1. Directory convention — any ancestor segment is a known test/SLT dir.
    if any(seg.lower() in _TEST_DIR_SEGMENTS for seg in segs[:-1]):
        return True
    # 2. Filename convention.
    stem = name.rsplit(".", 1)[0] if "." in name else name
    # bare conventional names: test.py / tests.py / spec.js / conftest.py …
    if stem in ("test", "tests", "spec", "specs", "slt", "conftest"):
        return True
    # python/pytest prefix, and slt prefix
    if stem.startswith("test_") or stem.startswith("slt_"):
        return True
    # <name>_test / _tests / _spec / _slt  (python, go, ruby, elixir, …)
    if any(stem.endswith(suf) for suf in ("_test", "_tests", "_spec", "_slt")):
        return True
    # js/ts infix:  foo.test.ts / foo.spec.tsx / foo.slt.js
    if ".test." in name or ".spec." in name or ".slt." in name:
        return True
    # Java/Kotlin/C# CamelCase suffix — matched in ORIGINAL case so lowercase
    # words like "latest"/"contest" do NOT trip. FooTest / FooTests /
    # FooTestCase / FooIT (failsafe integration test) / FooITCase.
    raw_stem = raw_name.rsplit(".", 1)[0] if "." in raw_name else raw_name
    if any(raw_stem.endswith(suf) for suf in
           ("Test", "Tests", "TestCase", "TestCases", "IT", "ITCase")):
        return True
    return False


def _extract_code_files_from_markdown(text: str) -> list:
    """
    Extract file-path + code pairs from LLM output written as Markdown.

    Handles patterns like:

        ### `src/components/App.jsx`
        ```jsx
        <code here>
        ```

        **File: src/utils/helper.js**
        ```javascript
        <code here>
        ```

        ```python
        # src/utils/helper.py
        <code here>
        ```
    """

    files = []
    seen_paths = set()

    def add(path: str, code: str):
        path = path.strip().lstrip("/")
        code = code.strip()
        if path and code and len(code) > 20 and path not in seen_paths:
            seen_paths.add(path)
            files.append({
                "path": path,
                "content": code,
                "is_test": _is_test_path(path),
            })

    # Strategy A: heading + code fence pairs
    # e.g. "### `src/App.jsx`\n```jsx\n...\n```"
    heading_fence = re.compile(
        r"(?:^|\n)#{1,4}[^\n]*?[`'\"]([a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]+)[`'\"]\s*\n\s*```\w*\n([\s\S]+?)```",
        re.MULTILINE,
    )
    for m in heading_fence.finditer(text):
        add(m.group(1), m.group(2))

    # Strategy B: bold/italic file annotation + code fence
    # e.g. "**File: src/App.jsx**\n```jsx\n...\n```"
    bold_fence = re.compile(
        r"(?:\*{1,2}|__)(?:File:|filename:)?\s*([a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]+)(?:\*{1,2}|__)\s*\n\s*```\w*\n([\s\S]+?)```",
        re.MULTILINE | re.IGNORECASE,
    )
    for m in bold_fence.finditer(text):
        add(m.group(1), m.group(2))

    # Strategy C: code fence preceded by a bare file path on its own line
    bare_path_fence = re.compile(
        r"(?:^|\n)([a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]+)\s*\n\s*```\w*\n([\s\S]+?)```",
        re.MULTILINE,
    )
    for m in bare_path_fence.finditer(text):
        candidate = m.group(1).strip()
        # Must look like a path (has '/' or reasonable extension)
        if "/" in candidate or candidate.count(".") == 1:
            add(candidate, m.group(2))

    # Strategy D: first-line comment inside code fence is the file path
    # e.g. ```javascript\n// src/utils/helper.js\n<code>```
    comment_path_fence = re.compile(r"```(\w*)\n([\s\S]+?)```", re.MULTILINE)
    for m in comment_path_fence.finditer(text):
        block = m.group(2)
        first_line = _s(block).split("\n")[0].strip()
        # Look for // path or # path at top of block
        cp = re.match(r"(?://|#|/\*)\s*([a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]+)", first_line)
        if cp:
            path = cp.group(1)
            code = "\n".join(_s(block).split("\n")[1:]).strip()
            add(path, code)

    return files


# ── Sandbox compile commands (used by self-healing + build check) ─────────────
# Maps language → shell command to run inside the repo sandbox image.
# Each command reads from /sandbox/{filename} and exits non-zero on compile error.
# These run with --network none inside a repo-specific image that has all project
# dependencies pre-installed (Maven jars, node_modules, Go modules, etc.).
_SANDBOX_COMPILE_COMMANDS: dict[str, str] = {
    # ast.parse does pure syntax validation without resolving imports.
    # py_compile would fail on every project-relative import (core/, db/, routers/)
    # because only the single generated file is mounted at /sandbox — not the full project.
    "python":     "python3 -c \"import ast, sys; ast.parse(open('/sandbox/main.py').read()); print('OK')\"",
    # Plain JS: node --check understands ES modules fine
    "javascript": "node --check /sandbox/main.js",
    "js":         "node --check /sandbox/main.js",
    # TypeScript / JSX / TSX / React / Angular / Vue:
    # node --check FAILS on JSX (<div/>) and TypeScript syntax (generics, decorators).
    # check_jsx.js (baked into the npm/yarn/pnpm image) uses @babel/parser or TS
    # transpileModule — both understand JSX + TypeScript syntax correctly.
    "typescript": "node /usr/local/bin/check_jsx.js /sandbox/main.ts 2>&1",
    "ts":         "node /usr/local/bin/check_jsx.js /sandbox/main.ts 2>&1",
    "jsx":        "node /usr/local/bin/check_jsx.js /sandbox/main.jsx 2>&1",
    "tsx":        "node /usr/local/bin/check_jsx.js /sandbox/main.tsx 2>&1",
    "react":      "node /usr/local/bin/check_jsx.js /sandbox/main.jsx 2>&1",
    # Angular: check_jsx.js handles TypeScript decorators (@Component etc.)
    "angular":    "node /usr/local/bin/check_jsx.js /sandbox/main.ts 2>&1",
    # Vue: check_vue.js parses .vue SFCs using @vue/compiler-sfc
    "vue":        "node /usr/local/bin/check_vue.js /sandbox/main.vue 2>&1",
    # Java: compile against project classpath (Maven deps + compiled project classes)
    "java": (
        "sh -c 'DEPS=$(find /workspace/target/dependency -name \"*.jar\" 2>/dev/null"
        " | tr \"\\n\" \":\" | sed \"s/:$//\");"
        " CLS=/workspace/target/classes;"
        " CP=\"${DEPS}:${CLS}\";"
        " javac -cp \"${CP}\" /sandbox/Main.java 2>&1; exit $?'"
    ),
    # Kotlin: kotlinc is installed in gradle-kotlin images via `apk add kotlin`
    "kotlin": (
        "sh -c 'kotlinc /sandbox/Main.kt -include-runtime -d /sandbox/out.jar 2>&1; exit $?'"
    ),
    "kt": (
        "sh -c 'kotlinc /sandbox/Main.kt -include-runtime -d /sandbox/out.jar 2>&1; exit $?'"
    ),
    "scala":   "scala-cli compile /sandbox/Main.scala 2>&1",
    "go":      "gofmt -e /sandbox/main.go",
    "golang":  "gofmt -e /sandbox/main.go",
    "rust":    "sh -c 'rustc --edition 2021 --crate-type lib /sandbox/main.rs 2>&1; exit $?'",
    "rs":      "sh -c 'rustc --edition 2021 --crate-type lib /sandbox/main.rs 2>&1; exit $?'",
    "csharp":  "sh -c 'dotnet-script /sandbox/Main.cs 2>&1; exit $?'",
    "cs":      "sh -c 'dotnet-script /sandbox/Main.cs 2>&1; exit $?'",
    "dotnet":  "sh -c 'dotnet-script /sandbox/Main.cs 2>&1; exit $?'",
    "ruby":    "ruby -c /sandbox/main.rb 2>&1",
    "rb":      "ruby -c /sandbox/main.rb 2>&1",
    "php":     "php -l /sandbox/main.php 2>&1",
    "cpp":     "sh -c 'g++ -fsyntax-only /sandbox/main.cpp 2>&1; exit $?'",
    "c":       "sh -c 'gcc -fsyntax-only /sandbox/main.c 2>&1; exit $?'",
    # Swift: swiftc syntax check
    "swift":   "sh -c 'swiftc -parse /sandbox/main.swift 2>&1; exit $?'",
    "bash":    "bash -n /sandbox/main.sh",
    "shell":   "sh -n /sandbox/main.sh",
    "sh":      "sh -n /sandbox/main.sh",
}

# Language → filename written to /sandbox for syntax/compile checking.
_SANDBOX_FILENAMES: dict[str, str] = {
    "python":     "main.py",
    "javascript": "main.js",
    "typescript": "main.ts",
    "js":         "main.js",
    "ts":         "main.ts",
    "jsx":        "main.jsx",
    "tsx":        "main.tsx",
    "react":      "main.jsx",
    "angular":    "main.ts",
    "vue":        "main.vue",   # Vue SFCs are .vue files, NOT .js
    "java":       "Main.java",
    "kotlin":     "Main.kt",
    "kt":         "Main.kt",
    "scala":      "Main.scala",
    "go":         "main.go",
    "golang":     "main.go",
    "rust":       "main.rs",
    "rs":         "main.rs",
    "csharp":     "Main.cs",
    "cs":         "Main.cs",
    "dotnet":     "Main.cs",
    "ruby":       "main.rb",
    "rb":         "main.rb",
    "php":        "main.php",
    "cpp":        "main.cpp",
    "c":          "main.c",
    "swift":      "main.swift",
    "bash":       "main.sh",
    "shell":      "main.sh",
    "sh":         "main.sh",
}


# File-extension → compile-language key for sandbox validation.
# Takes priority over the repo's primary language so that .jsx/.tsx files in a
# Python repo are not syntax-checked with `python3 ast.parse()`.
_EXT_TO_COMPILE_LANG: dict[str, str] = {
    ".py":    "python",
    ".js":    "javascript",
    ".jsx":   "jsx",
    ".ts":    "typescript",
    ".tsx":   "tsx",
    ".java":  "java",
    ".kt":    "kotlin",
    ".scala": "scala",
    ".go":    "go",
    ".rs":    "rust",
    ".cs":    "csharp",
    ".rb":    "ruby",
    ".php":   "php",
    ".cpp":   "cpp",
    ".c":     "c",
    ".swift": "swift",
    ".sh":    "bash",
    ".vue":   "vue",
}


def _compile_lang_for_file(file_path: str, fallback_lang: str) -> str:
    """Return the compile-language key for a given file path.
    Derived from file extension so mixed-language repos (e.g. Python + React)
    get the correct sandbox command for each file."""
    if file_path and "." in file_path:
        ext = "." + file_path.rsplit(".", 1)[-1].lower()
        lang = _EXT_TO_COMPILE_LANG.get(ext)
        if lang:
            return lang
    return (fallback_lang or "").lower()


def _get_sandbox_filename(lang: str, file_path: str) -> str:
    """
    Return the best filename for a generated file in /sandbox.
    Uses the original filename if it has the right extension,
    otherwise falls back to the _SANDBOX_FILENAMES default.
    """
    if file_path:
        orig_name = _s(file_path).split("/")[-1]
        if "." in orig_name:
            return orig_name
    return _SANDBOX_FILENAMES.get(lang, "main.py")


def _is_compile_error_line(line: str, lang: str) -> bool:
    """Return True if a build output line looks like a compiler error."""
    line_lower = line.lower()
    if lang in ("python",):
        return "syntaxerror" in line_lower or "error:" in line_lower
    if lang == "java":
        return "error:" in line_lower
    if lang in ("go", "golang"):
        return "syntax error" in line_lower or "undefined:" in line_lower or "cannot use" in line_lower
    if lang in ("javascript", "typescript", "js", "ts", "jsx", "tsx", "react", "angular", "vue"):
        return "syntaxerror" in line_lower or "error ts" in line_lower
    if lang in ("rust", "rs"):
        return "error[" in line_lower or "error:" in line_lower
    if lang in ("csharp", "cs", "dotnet"):
        return "error cs" in line_lower
    return "error:" in line_lower or "error[" in line_lower


def build_semantic_check_prompt(
    jira_key: str,
    requirements: str,
    summary: str,
    changed_paths: list,
    changed_block: str,
) -> str:
    """Assemble the SEMANTIC_CHECK prompt from the run's DIFF (`changed_block`,
    produced by CodeContextService.changed_regions()) + the design/implementation
    summary — NOT every full file body.

    Pure string assembly (no IO) so it unit-tests over a stub diff. Phase 1 of the
    context-architecture migration: the old prompt stuffed every changed file in
    full (~585k chars, incl. a 248k migrate.py, for a few-line change). The diff
    scales with the change, not the repo, and the verdict logic is unchanged."""
    _files_line = ", ".join(p for p in (changed_paths or []) if p) or "(none)"
    return (
        f"You are an SDLC requirement validator for AiNxt.\n\n"
        f"JIRA: {jira_key}\n"
        f"Stated requirement:\n{requirements}\n\n"
        f"Implementation summary: {summary}\n\n"
        f"Files changed in this run: {_files_line}\n\n"
        f"{changed_block}\n\n"
        f"Evaluate whether these changes reasonably address the stated requirement.\n"
        f"For simple requirements (e.g. 'add exception handler', 'create endpoint'), "
        f"focus on whether the core task was completed — do NOT invent acceptance criteria "
        f"that were not explicitly stated. Only return FAIL if the changes clearly do NOT "
        f"address what was asked.\n"
        f"Return JSON only:\n"
        f'{{"requirements_met": true or false, "confidence": 0.0-1.0, '
        f'"unmet_requirements": ["..."], "verdict": "PASS" or "FAIL", "reason": "one sentence"}}'
    )



# ── per-file diff slicing (ctx-migration Phase 3) ─────────────────────────────
# Pure string helpers (no IO) so they unit-test over stub diffs. They turn the
# run's unified diff into a PER-FILE "changed hunks + surrounding region" view, so
# the code reviewer reasons about the change in context instead of the full file.

_REVIEW_CONTEXT_LINES = 15      # surrounding new-file lines shown around each hunk
_REVIEW_VIEW_MAX_CHARS = 24_000  # cap per-file review view (compacted if exceeded)


class CodingStateMachine:
    """
    Persistent state machine for the AI Coding Agent (three-phase CLI cutover).
    Pre-gate: IMPLEMENT → REVIEW → VERIFIED_DIFF → HITL gate.
    Post-gate: APPLYING → TEST_VERIFY → SLT_RUNNING → COMMITTING → AWAITING_PR_APPROVAL.
    """

    MAX_FIX_ATTEMPTS  = 2
    # NOTE (W-E / fixes C1): the old MAX_FILES_PER_RUN=10 cap and the
    # _derive_file_plan items[:8] truncation were removed. Full scope is
    # approved at the HITL design gate, so planned new files are no longer
    # silently dropped. Coding/review/commit run per-file with no list cap.
    # Max auto-fix-and-retry cycles before suspending on a compile failure.
    # 0 = suspend on first failure (no auto-fix), 1 = one auto-fix attempt, 2 = two (default).
    # Override via SDLC_MAX_BUILD_ATTEMPTS env var.
    MAX_BUILD_ATTEMPTS = int(__import__("os").getenv("SDLC_MAX_BUILD_ATTEMPTS", "2"))

    # Languages for which per-file syntax checking (`_validate`) works without the
    # full project workspace mounted — used by `_load_sandbox_image_info` to decide
    # whether to resolve a sandbox image. JVM/Rust/C#/C/C++ are deliberately EXCLUDED:
    # per-file `javac`-style compile false-negatives on sibling symbols that aren't
    # built yet, so those languages rely on the whole-workspace `_build_check()`
    # oracle instead.
    _SYNTAX_CHECK_LANGUAGES = frozenset({
        "python", "javascript", "typescript", "go", "ruby", "php", "bash",
    })

    # Maps internal phase names that are NOT in stage_sequence_for() to their
    # nearest valid resume target.  _suspend() uses this to normalise the DB
    # record so every suspended run is always resumable through the API.
    # The original phase name is preserved in the log and suspend reason.
    _SUSPEND_STAGE_MAP: dict = {
        "APPLYING":          "COMMITTING",     # no VERIFIED_DIFF → resume at codegen gate
        "GOVERNANCE_REVIEW": "GOVERNANCE_SCAN", # legacy mid-tail name; tail is GOVERNANCE_SCAN
    }

    def __init__(
        self,
        run_id:        str,
        jira_key:      str,
        repo:          str,        # project name only — "ainxt-platform" (for RAG / sandbox lookup)
        language:      str,        # "python" | "java" | "go" | "typescript" | etc.
        design:        dict,
        analysis:      dict,
        base_branch:   str = "",   # branch to read existing files from (authoritative)
        working_branch: str = "",  # branch to commit changes to; MR targets base_branch
        gitlab_repo:   str = "",   # full GitLab path — "ainxt/ainxt-platform" (for API calls)
        skip_tests:    bool = False,  # bypass TESTING + SLT_RUNNING and proceed directly to commit/MR
        skip_slt:      bool = False,  # bypass SLT *creation* in CODING (independent of skip_tests)
        compile_skipped: bool = False,  # skip ALL compilation (set when baseline build was skipped on error)
        user_id:       str = "",   # JWT sub of the triggering user — for per-user GitLab/Jira creds
        user_email:    str = "",   # display email of the triggering user
        mode:          str = "postgate",  # "pregate" = generate+compile+test, store VERIFIED_DIFF,
                                          #   STOP before the HITL gate. "postgate" = deterministically
                                          #   APPLY the approved VERIFIED_DIFF → TEST_VERIFY → SLT → commit.
    ):
        self.run_id        = run_id
        bind_context(correlation_id=self.run_id, pipeline_stage="sdlc_state_machine")
        from core.logger import set_request_id as _set_rid
        _set_rid(self.run_id)
        self.jira_key      = jira_key
        self.repo          = repo          # project name — used for RAG / sandbox image lookup
        self.gitlab_repo   = gitlab_repo or repo  # namespace/project — used for all GitLab API calls
        self.language      = language
        self.design        = design
        self.analysis      = analysis
        self.base_branch   = base_branch
        self.working_branch = working_branch
        self.skip_tests    = skip_tests
        self.skip_slt      = skip_slt
        self.compile_skipped = compile_skipped
        self.user_id       = user_id
        self.user_email    = user_email
        self.mode          = (mode or "postgate").lower()

        # Mutable coding context
        self.code_output: dict  = {}
        self.slt_output:  dict  = {}
        self.fix_attempts: int  = 0
        self._fix_history: list = []   # compact retry log for context compression
        # Corrective feedback supplied when resuming / going-back into a state-machine
        # stage (set by resume_from_stage_job). Injected decisively into the coder
        # prompt. Empty for normal forward runs → zero behaviour change.
        self._resume_feedback: str = ""

        # Confidence aggregation — scores populated during pipeline execution
        self._conf_build:    float = 0.0   # 1.0 = build passed
        self._conf_tests:    float = 0.0   # passed/(passed+failed) from test runner
        self._conf_review:   float = 1.0   # 1.0 = no issues; lower for high/critical

        # Optional builder image for per-file syntax checking during CODING.
        # Resolved from BuildManifestResolver — same ainxt-builder-* images used by
        # _build_check() and _run_tests(). None = skip per-file check; full compile
        # still runs via WorkspaceBuilder at the end of CODING.
        self.sandbox_image: str = None
        self.build_root:    str = "."
        self._load_sandbox_image_info(repo)

        # Per-run workspace path — lazily materialized on first build/test.
        # Each SDLC run gets its own isolated checkout of the working branch
        # so leftover modifications from prior runs cannot pollute the build.
        self._run_workspace_path: str = ""

        # Multi-repo workspace handle — populated by _setup_multi_repo_workspace()
        # at the start of _phase_implement when ENABLE_MULTI_REPO_SDLC is on and
        # sdlc_run_repos has non-primary rows. None for single-repo runs. (PLAN
        # stages its own copy via the module-level
        # sdlc_pipeline._setup_multi_repo_workspace_for_plan — not stored here.)
        self._mr_workspace = None
        # List of compile-only dep repos for the scope-expansion safety valve.
        self._compile_only_repos: list = []
        # Map of {gitlab_repo: mr_url} for sibling MRs opened against editable
        # deps in _phase_commit. Read by _build_pr_description to embed cross-
        # links in the primary MR body. Empty for single-repo runs.
        self._sibling_mr_urls: dict = {}
        # {gitlab_repo: [rich edit dicts]} — the EDITABLE deps' captured diffs, set
        # by _collect_dep_edits(). Review/approval-VISIBILITY channel only: these
        # entries are folded into the REVIEW diff text and into the VERIFIED_DIFF
        # artifact's separate `dep_edits_by_repo` section so Opus and the human
        # approver both see them. They are NEVER merged into code_output["files"]
        # / the VERIFIED_DIFF `edits` list — those drive the PRIMARY repo's apply +
        # commit path, and a dep path there would commit dep source into the
        # primary repo. Sibling MRs are opened from run.context.code_output_by_repo
        # by _create_sibling_mrs(). Empty for single-repo runs. Re-assigned on
        # EVERY _collect_dep_edits() call including an empty one, so a REVIEW fix
        # round that reverted a dep edit clears it instead of showing Opus / the
        # approver a hunk that no longer exists on disk.
        self._dep_edits: dict = {}
        # Optional URL of the follow-up MR opened by manifest_writer after the
        # primary MR succeeds. Embedded in the primary MR body as a note.
        self._manifest_update_url: str = ""

        self._artifact_cache = {}      # stage->payload hot-path cache
        self._last_findings_hash = None  # convergence detection
        # Scoped re-review (SDLC_SCOPED_REREVIEW): after a review_gate FIXING pass,
        # only re-review the files the fixer targeted and reuse cached verdicts for
        # the rest (the per-file reviewer judges each file in isolation, so unchanged
        # files yield identical verdicts; cross-file breakage is caught by the build/
        # TESTING phase). _last_fixed_paths is the scope; _prev_* are the cached
        # aggregates to merge against.
        self._last_fixed_paths: set = set()
        self._prev_code_review: dict | None = None
        self._prev_slt_review:  dict | None = None
        # Step 5: per-run dependency-manifest cache. The coder used to re-fetch
        # requirements.txt / pom.xml from GitLab once PER generated file (~10x in a
        # multi-file run). Read it from the pinned workspace and memoize the
        # rendered constraint block ONCE per (lang) so later files reuse it.
        # Maps lang_key -> rendered pom_block string ("" = checked, none found).
        self._dep_manifest_cache: dict = {}

        # ── SDLC governance (2026-07-17) ─────────────────────────────────────
        # PART 2 gate opt-in + subset — set by the worker after construction
        # (sm.run_governance_review / sm.governance_subset). Default OFF so runs
        # that don't opt in behave exactly as before. PART 1 awareness is
        # independent of these (always-on, gated only by SDLC_GOVERNANCE_AWARENESS
        # + bundle availability) and is cached per run in _gov_awareness_cache.
        self.run_governance_review: bool = False
        self.governance_subset = None
        self._gov_awareness_cache = None

    # ── Per-user credential helpers ───────────────────────────
    def _set_user_scm_token(self) -> None:
        """
        Resolve the triggering user's SCM PAT (GitHub or GitLab, depending on
        SCM_PROVIDER) from user_tokens and install it into the tools thread-local
        so all subsequent SCM API calls in this thread use the user's own
        credentials instead of the env-var default.
        Falls back to GITHUB_TOKEN / GITLAB_TOKEN env var when no per-user token found.
        """
        import os as _os
        from core.config import SCM_PROVIDER as _SCM
        token = ""
        if self.user_id:
            try:
                from core.platform_credentials import get_scm_token as _get_scm
                token = _get_scm(user_id=self.user_id)
                logger.info(
                    f"[SM {self.run_id}] SCM token resolved for user_id={self.user_id!r} "
                    f"({self.user_email}) provider={_SCM} — using per-user PAT"
                )
            except PermissionError:
                logger.warning(
                    f"[SM {self.run_id}] No SCM token in user_tokens for "
                    f"user_id={self.user_id!r} ({self.user_email}) — falling back to env var"
                )
        if not token:
            _env_key = "GITHUB_TOKEN" if _SCM == "github" else "GITLAB_TOKEN"
            token = _os.getenv(_env_key, "")
        if token:
            if _SCM == "github":
                from tools.github_tools import set_token as _set_tok
            else:
                from tools.gitlab_tools import set_token as _set_tok
            _set_tok(token)

    # Keep the old name as an alias so any external callers (workers, tests) still work
    _set_user_gitlab_token = _set_user_scm_token

    # ── Per-run workspace lifecycle ───────────────────────────
    def _ensure_run_workspace(self, repo_slug: str, resume_in_place: bool = False) -> str:
        """
        Materialize a per-run workspace by cloning the working branch fresh.
        Mirrors what a developer does locally (`git clone -b <branch>`) so
        the build/test runs against the exact same state a human reviewer
        would see when pulling the branch.
        Cached for the rest of the run.

        resume_in_place: when True (IMPLEMENT manual resume), an existing run
        workspace on local disk is reused AS-IS — no wipe/reset — so a coding
        session's uncommitted in-progress files survive for a `--resume`
        continuation. Threaded to prepare_run_workspace. Default False.
        """
        if self._run_workspace_path and __import__("os").path.isdir(self._run_workspace_path):
            return self._run_workspace_path

        from db.database import engine as _eng
        from sqlalchemy import text as _txt
        from workers.workspace_sync_worker import prepare_run_workspace as _prw
        from agents.sdlc_context import normalize_repo_index_key_without_prefix as _nrik

        # repo_index_status is keyed by the slug the INDEXER registered:
        # routers/index_router._extract_repo_name() = last path segment, lowercase,
        # hyphens/dots → underscores. So a repo entered in the trigger form as
        # 'nts/nts' (namespace path) or 'nts-2.0' was indexed as 'nts' / 'nts_2_0'.
        # The workspace lookup must normalize self.repo the SAME way or it misses
        # ("No git_url in repo_index_status…") even though the repo is indexed.
        # Try the normalized slug first, then the raw value for backward-compat;
        # both must carry a non-null git_url to be usable.
        _canon_slug = _nrik(self.repo)
        _row = None
        for _slug in (_canon_slug, self.repo):
            if not _slug:
                continue
            with _eng.connect() as _c:
                _cand = _c.execute(_txt(
                    "SELECT git_url, branch FROM repo_index_status WHERE repo_name=:slug"
                ), {"slug": _slug}).fetchone()
            if _cand and _cand.git_url:
                _row = _cand
                if _slug != self.repo:
                    logger.info(
                        f"[SM {self.run_id}] workspace: resolved repo_index_status via "
                        f"normalized slug '{_slug}' (trigger value was '{self.repo}')"
                    )
                break

        # Indexing is NO LONGER a prerequisite. When the repo has a repo_index_status
        # row we PREFER it (honors the dev GitLab mock's file:// URL and clones the
        # exact registered origin); when it was never indexed we fall back below to
        # building the clone URL from GITLAB_URL + slug + the triggering user's token.

        # Prefer the working branch (so prior commits on this run's branch are
        # reflected) and fall back to base / the indexed branch / default.
        branch = (
            self.working_branch or self.base_branch
            or (_row.branch if _row else None) or "main"
        )

        # Workspace consistency (SDLC_REUSE_RUN_WORKSPACE): pin this run to one base
        # commit. The first materialization captures the exact SHA cloned; every later
        # stage / instance re-checks-out that same SHA, so a reused checkout and a
        # fresh clone on another host are byte-identical (closes the "different code at
        # different times" gap across HITL-gate resumes). Flag off → unchanged.
        _reuse = self._reuse_workspace_enabled()
        _pin = self._get_run_base_sha() if _reuse else ""
        # 2026-07-07 workspace-path consistency: key the run workspace on the CANONICAL
        # slug (_canon_slug = normalize_repo_index_key_without_prefix(self.repo)) so this
        # stage lands on the SAME directory as the pipeline early-checkout (which now also
        # normalizes). Idempotent when repo_slug is already canonical (e.g. manifest.repo_slug);
        # convergent when a caller passes the raw "group/repo" self.repo. Cleanup derives the
        # slug from the actual path basename, so it stays consistent either way.
        _path_slug = _canon_slug or repo_slug
        # Clone as the user who TRIGGERED this run — never with whatever token the
        # indexer baked into repo_index_status.git_url (a per-repo shared value).
        # Strip any embedded credentials and re-inject this user's own PAT.
        from core.platform_credentials import build_run_clone_url as _build_clone_url
        if _row and _row.git_url:
            _clone_url = _build_clone_url(
                _row.git_url, user_id=self.user_id or "", email=self.user_email or ""
            )
        else:
            # Repo never indexed → build the clone URL from GITLAB_URL directly,
            # authenticated with the triggering user's own token (user_id → email →
            # GITLAB_TOKEN env). Mirrors the governance/baseline-build clone path.
            from agents.sdlc_pipeline import _authenticated_clone_url as _auth_url
            _gl_url = os.getenv("GITLAB_URL", "")
            _gl_token = ""
            if self.user_id or self.user_email:
                try:
                    from core.platform_credentials import get_gitlab_token as _get_gl
                    _gl_token = _get_gl(
                        user_id=self.user_id or "", email=self.user_email or ""
                    )
                except PermissionError:
                    _gl_token = ""
            if not _gl_token:
                _gl_token = os.getenv("GITLAB_TOKEN", "")
            _clone_url = _auth_url(self.repo, _gl_url, _gl_token)
            logger.info(
                f"[SM {self.run_id}] workspace: '{self.repo}' not in repo_index_status "
                f"— cloning from GITLAB_URL (indexing not required)"
            )
        self._run_workspace_path = _prw(
            self.run_id, _path_slug, _clone_url, branch,
            pin_sha=_pin, reuse=bool(_reuse and _pin),
            resume_in_place=bool(resume_in_place),
        )
        if _reuse and not _pin:
            # First materialization DEFINES the pin: persist the exact commit cloned
            # so every later stage/instance materializes byte-identical code.
            try:
                from workers.workspace_sync_worker import _git_head as _gh
                _captured = _gh(self._run_workspace_path)
                if _captured:
                    self._set_run_base_sha(_captured)
                    logger.info(f"[SM {self.run_id}] pinned run base_sha={_captured[:8]}")
            except Exception as _e:
                logger.debug(f"[SM {self.run_id}] base_sha capture failed: {_e}")
        logger.info(
            f"[SM {self.run_id}] run-workspace ready at {self._run_workspace_path} "
            f"(branch={branch}, pin={_pin[:8] if _pin else 'none'})"
        )
        return self._run_workspace_path

    def _setup_multi_repo_workspace(self, stage: str) -> None:
        """
        Stage dependency-repo checkouts into the primary workspace for the CLI
        PLAN/IMPLEMENT phases. Revived hook: this used to fire at the start of
        the now-deleted `_phase_coding` (pre 2026-07-01 three-phase cutover);
        it is now called from `_phase_implement` here, and PLAN stages its own
        copy via the module-level `sdlc_pipeline._setup_multi_repo_workspace_for_plan`.

        No-op (zero overhead, no logging) when multi-repo is disabled or the run
        has no non-primary `sdlc_run_repos` rows — a single-repo run must never
        do any extra work or hit the except/suspend path below.

        Idempotent — safe to call once per phase. `prepare_and_install_deps` /
        `_clone_one` already short-circuit the clone when the checkout is
        already at the pinned SHA, so a second call (e.g. on IMPLEMENT resume)
        is cheap.
        """
        # Local import to avoid a circular import at module load
        # (sdlc_pipeline imports from sdlc_state_machine elsewhere).
        from agents.sdlc_pipeline import _is_multi_repo_enabled
        if not _is_multi_repo_enabled():
            return

        from store.sdlc_store import list_run_repos
        rows = list_run_repos(self.run_id) or []
        if not rows or all(r.get("kind") == "primary" for r in rows):
            return

        if not self._run_workspace_path:
            logger.warning(
                "[SM] multi-repo dep staging skipped — no primary workspace",
                run_id=self.run_id, stage=stage,
            )
            return

        try:
            import os as _os
            from urllib.parse import urlsplit, urlunsplit
            from agents.multi_repo_workspace import prepare_and_install_deps

            gl_url = _os.getenv("GITLAB_URL", "https://gitlab.com")

            # NOTE: dep repos don't necessarily have a row in repo_index_status,
            # so we cannot reuse core.platform_credentials.build_run_clone_url
            # (it requires a STORED url from that table). Build the clone URL
            # directly from GITLAB_URL + the resolved per-user/env token instead,
            # mirroring the inline `_dep_clone_url` helper at
            # sdlc_pipeline._setup_multi_repo_workspace_for_plan.
            #
            # Resolve the identity EXACTLY like PLAN / the baseline gate do:
            #   ctx["user_id"] → run["triggered_by"] → env, with email as the
            #   secondary key. self.user_id/self.user_email are populated from the
            #   run context at construction, but a run whose stored context lost
            #   user_id/user_email (e.g. persisted without it) would leave BOTH
            #   empty here — and the old `if self.user_id or self.user_email`
            #   guard then SKIPPED get_gitlab_token entirely and dropped straight
            #   to the env GITLAB_TOKEN. PLAN, which falls back to run.triggered_by,
            #   still resolved the triggering user's PAT and cloned the dep fine, so
            #   the dep clone succeeded in PLAN/baseline/classify but 403'd once
            #   IMPLEMENT re-cloned with the (dep-unauthorized) env token. Mirroring
            #   PLAN's triggered_by fallback closes that asymmetry; the env token
            #   stays the last resort, not the first for an empty-context run.
            _uid = self.user_id or ""
            _email = self.user_email or ""
            if not (_uid or _email):
                try:
                    _run = get_run(self.run_id) or {}
                    _rctx = _run.get("context") or {}
                    _uid = _rctx.get("user_id") or _run.get("triggered_by", "") or ""
                    _email = _rctx.get("user_email") or ""
                except Exception as _id_e:
                    logger.warning(
                        f"[SM {self.run_id}] dep-clone identity fallback lookup failed "
                        f"(non-fatal): {_id_e}"
                    )
            gl_token = ""
            _tok_src = "env"
            if _uid or _email:
                try:
                    from core.platform_credentials import get_gitlab_token as _get_gl
                    gl_token = _get_gl(user_id=_uid, email=_email)
                    _tok_src = "per-user-pat"
                except PermissionError:
                    gl_token = ""
            if not gl_token:
                gl_token = _os.getenv("GITLAB_TOKEN", "")
                _tok_src = "env"
            logger.info(
                f"[SM {self.run_id}] dep-clone token resolved stage={stage} "
                f"source={_tok_src} uid={'set' if _uid else 'empty'} "
                f"email={'set' if _email else 'empty'} has_token={bool(gl_token)}"
            )

            def _clone_url_resolver(gitlab_path: str) -> str:
                _sp = urlsplit(gl_url)
                _netloc = f"oauth2:{gl_token}@{_sp.netloc}" if gl_token else _sp.netloc
                return urlunsplit((_sp.scheme or "https", _netloc, f"/{gitlab_path}.git", "", ""))

            # compile_skipped ("Skip compilation & continue" at BASELINE_BUILD):
            # clone deps so the CLI can read them under .sdlc_deps/, but skip the
            # compile-only `mvn install` — that install is the dependent-repo build
            # the operator opted out of. Re-running it here would fail again and
            # this method's except-clause would SUSPEND the run; skipping it keeps
            # the dep checkouts staged and lets IMPLEMENT proceed. Mirrors the PLAN
            # side (sdlc_pipeline._setup_multi_repo_workspace_for_plan).
            ws = prepare_and_install_deps(
                self.run_id, self._run_workspace_path, rows, _clone_url_resolver,
                skip_install=bool(self.compile_skipped),
            )
            self._mr_workspace = ws
            self._compile_only_repos = [
                r.get("repo") for r in rows if r.get("kind") == "compile-only"
            ]

            _dep_rows = [r for r in rows if r.get("kind") != "primary"]
            _editable_count = sum(1 for r in _dep_rows if r.get("kind") == "editable")
            logger.info(
                "[SM] multi-repo dep staging complete", run_id=self.run_id, stage=stage,
                dep_count=len(_dep_rows), editable_count=_editable_count,
                compile_only_count=len(self._compile_only_repos),
            )

            # Persist each staged dep path so the run manifest/UI can surface it.
            # upsert_run_repo only overwrites the fields whose args are not None,
            # so passing the row's own ref/kind/ref_sha back alongside
            # workspace_path is a safe partial update — no other column is
            # touched or clobbered.
            try:
                from store.sdlc_store import upsert_run_repo
                for row in _dep_rows:
                    dep_path = (getattr(ws, "dep_paths", None) or {}).get(row.get("repo"))
                    if dep_path:
                        upsert_run_repo(
                            self.run_id, row.get("repo"), row.get("ref"), row.get("kind"),
                            ref_sha=row.get("ref_sha"), workspace_path=dep_path,
                        )
            except Exception as _persist_e:
                logger.warning(
                    "[SM] multi-repo workspace_path persist failed (non-fatal)",
                    run_id=self.run_id, stage=stage, error=str(_persist_e),
                )
        except Exception as e:
            logger.error(
                "[SM] multi-repo dep staging failed — suspending",
                run_id=self.run_id, stage=stage, error=str(e),
            )
            self._suspend(stage, f"multi-repo dep staging failed: {e}")

    @staticmethod
    def _reuse_workspace_enabled() -> bool:
        import os as _o
        return _o.getenv("SDLC_REUSE_RUN_WORKSPACE", "false").strip().lower() in (
            "1", "true", "yes", "on")

    def _get_run_base_sha(self) -> str:
        """Read the run's pinned base commit (sdlc_runs.base_sha), '' if unset."""
        try:
            from db.database import engine as _eng
            from sqlalchemy import text as _txt
            with _eng.connect() as _c:
                row = _c.execute(
                    _txt("SELECT base_sha FROM sdlc_runs WHERE id=:id"),
                    {"id": self.run_id},
                ).fetchone()
            return (row.base_sha or "") if row else ""
        except Exception as _e:
            logger.debug(f"[SM {self.run_id}] base_sha read failed: {_e}")
            return ""

    def _set_run_base_sha(self, sha: str) -> None:
        """Persist the run's base commit once (first-writer-wins). Non-fatal."""
        if not sha:
            return
        try:
            from db.database import engine as _eng
            from sqlalchemy import text as _txt
            with _eng.connect() as _c:
                _c.execute(
                    _txt("UPDATE sdlc_runs SET base_sha=:s "
                         "WHERE id=:id AND (base_sha IS NULL OR base_sha='')"),
                    {"s": sha, "id": self.run_id},
                )
                _c.commit()
        except Exception as _e:
            logger.warning(f"[SM {self.run_id}] base_sha persist failed (non-fatal): {_e}")

    def _cleanup_run_workspace(self) -> None:
        """Remove the per-run workspace once the run terminates. Best-effort."""
        import os as _os
        if not self._run_workspace_path or _os.getenv("AINXT_KEEP_FAILED_WORKSPACE") == "1":
            return
        try:
            from workers.workspace_sync_worker import cleanup_run_workspace as _crw
            # Derive repo_slug from path: /opt/ainxt/workspaces/runs/{run_id}_{repo_slug}
            import os as _os
            base = _os.path.basename(self._run_workspace_path)
            slug = base[len(self.run_id) + 1:] if base.startswith(self.run_id + "_") else self.repo
            _crw(self.run_id, slug)
        except Exception as _e:
            logger.warning(f"[SM {self.run_id}] run-workspace cleanup failed: {_e}")
        finally:
            self._run_workspace_path = ""

    # ── Multi-repo workspace lifecycle (Phase 4a) ─────────────
    def _get_run_repos_info(self) -> list:
        """
        Return all `sdlc_run_repos` rows for this run, ordered by build_order.

        Empty list when the multi-repo flag is off or Phase 2 preflight did not
        populate the table (single-repo Jira-triggered runs). Callers should
        treat empty as "single-repo mode, no extra work to do".
        """
        try:
            from store.sdlc_store import list_run_repos
            return list_run_repos(self.run_id) or []
        except Exception as exc:
            logger.warning(f"[SM {self.run_id}] _get_run_repos_info failed: {exc}")
            return []

    def _create_sibling_mrs(self) -> dict:
        """
        Open one MR per editable dep repo (not primary) using the per-repo
        coder output from `run.context.code_output_by_repo`.

        Deletions count as real work: a `deleted: True` entry becomes an
        `action: "delete"` in the atomic batch commit, so a dep change that is
        ONLY deletions still opens a sibling MR. (REVIEW and the human approver
        are both shown dep deletions — an approved deletion must actually land.)

        Returns {gitlab_repo: mr_url} for successfully opened siblings. Empty
        dict for single-repo runs or when no editable deps produced files.
        Failures on individual sibling repos are logged but never raised —
        the primary MR creation continues regardless. Each successful sibling
        MR is recorded in `sdlc_run_repos.pr_url` so the UI / get_run_diff /
        manifest_writer can find them.

        Branch naming: sibling branches reuse the primary's working branch name
        (e.g. `feature/JIRA-123-...`) inside the dep repo. Same name across
        repos makes manual review / merge less confusing for engineers who
        switch between repos in their IDE.
        """
        rows = self._get_run_repos_info()
        if not rows:
            return {}
        editable = [r for r in rows if r.get("kind") == "editable"]
        if not editable:
            return {}

        try:
            run = get_run(self.run_id) or {}
            code_by_repo = (run.get("context") or {}).get("code_output_by_repo") or {}
        except Exception:
            code_by_repo = {}
        if not code_by_repo:
            logger.warning(
                f"[SM {self.run_id}] _create_sibling_mrs: no code_output_by_repo in run context — "
                f"sibling MRs cannot be opened. Either _collect_dep_edits() never ran, or it ran "
                f"and found no dep changes (e.g. a fix round reverted them), which clears the key."
            )
            return {}

        try:
            # gitlab_batch_commit (the SAME helper the primary commit path uses)
            # rather than gitlab_create_or_update_file: it is the only commit helper
            # that can carry an `action: "delete"` entry, and a dep whose diff
            # contains a deletion must actually delete the file — REVIEW and the
            # human approver were both shown that deletion.
            from core.config import SCM_PROVIDER as _SCM
            if _SCM == "github":
                from tools.github_tools import (
                    github_create_branch as gitlab_create_branch,
                    github_batch_commit as gitlab_batch_commit,
                    github_create_pr as gitlab_create_mr,
                )
            else:
                from tools.gitlab_tools import (
                    gitlab_create_branch, gitlab_batch_commit, gitlab_create_mr
                )
            from store.sdlc_store import upsert_run_repo
        except Exception as exc:
            logger.error(f"[SM {self.run_id}] sibling MR imports failed: {exc}")
            return {}

        import re as _re
        sibling_urls: dict = {}
        sibling_branch = self.working_branch or f"{_BRANCH_PREFIX}/{self.jira_key.lower()}-ai-impl"

        for row in editable:
            gp = row.get("repo", "")
            if not gp:
                continue
            files = (code_by_repo.get(gp) or {}).get("files") or []
            # Build the GitLab actions[] array. A `deleted` entry is REAL WORK —
            # it must become an `action: "delete"`, not be filtered out for having
            # empty content. (It used to be dropped here, so a deletion the human
            # approved silently never happened, and a deletions-only dep produced
            # no sibling MR at all.)
            actions: list = []
            write_paths: list = []
            del_paths: list = []
            for f in files:
                p = (f.get("path") or "").strip()
                if not p:
                    continue
                if f.get("deleted"):
                    actions.append({"action": "delete", "file_path": p})
                    del_paths.append(p)
                elif (f.get("content") or "").strip():
                    actions.append({
                        "action":    "create" if f.get("is_new") else "update",
                        "file_path": p,
                        "content":   f.get("content") or "",
                    })
                    write_paths.append(p)
            if not actions:
                logger.warning(
                    f"[SM {self.run_id}] sibling MR skipped for {gp!r}: no files in code_output_by_repo"
                )
                continue

            sibling_base = row.get("ref") or "main"
            try:
                br = gitlab_create_branch(gp, sibling_branch, from_branch=sibling_base)
                if br.startswith("[Error") and "already exists" not in br and "409" not in br:
                    logger.error(f"[SM {self.run_id}] sibling branch failed for {gp!r}: {br}")
                    continue

                # ONE atomic commit for the whole dep (writes + deletes together),
                # exactly as the primary path commits. Atomic beats the old
                # per-file loop here: a sibling repo can no longer end up with the
                # new code committed but the deletion missing (or vice versa).
                msg = (
                    f"[{self.jira_key}] AI implementation (cross-repo): "
                    f"{len(write_paths)} file(s) written, {len(del_paths)} deleted"
                )
                rs = gitlab_batch_commit(gp, sibling_branch, actions, msg)
                if rs.startswith("[Error"):
                    logger.error(
                        "[SM] sibling commit failed — no MR opened for this dep",
                        run_id=self.run_id, repo=gp,
                        written=len(write_paths), deleted=len(del_paths), error=rs,
                    )
                    continue

                # Parse the ACTUAL applied file count out of gitlab_batch_commit's
                # success string ("Batch commit OK: N file(s) on ...") instead of
                # trusting the pre-drop `actions` list — its pre-flight probe may
                # have dropped confirmed-absent deletes (idempotent retry), so N
                # can be less than len(actions). N==0 means every action was
                # dropped (e.g. a deletions-only dep where every path was already
                # gone) — no POST was even issued, so opening an MR here would
                # create an empty one for a reviewer to look at.
                _cm = _re.search(r"Batch commit OK:\s*(\d+)\s*file", rs)
                if _cm:
                    committed = int(_cm.group(1))
                    _exact = True
                else:
                    committed = len(actions)  # fallback — pre-drop count, flagged below
                    _exact = False

                if committed == 0:
                    logger.warning(
                        "[SM] sibling commit applied zero files — skipping empty MR",
                        run_id=self.run_id, repo=gp,
                        reason=rs, written=len(write_paths), deleted=len(del_paths),
                    )
                    continue

                # Writes are never dropped by the pre-flight (only deletes can be),
                # so len(write_paths) is always accurate; back out the real
                # deleted count from the parsed total when we have one.
                if _exact:
                    actual_deleted = max(committed - len(write_paths), 0)
                else:
                    actual_deleted = len(del_paths)

                if actual_deleted:
                    logger.info(
                        "[SM] sibling dep deletions pushed",
                        run_id=self.run_id, repo=gp,
                        deleted_count=actual_deleted, branch=sibling_branch,
                        paths=del_paths[:20], exact=_exact,
                    )

                title = f"[{self.jira_key}] AI cross-repo change (dep of {self.gitlab_repo})"
                body = (
                    f"This MR is part of a multi-repo SDLC run for Jira `{self.jira_key}`.\n\n"
                    f"- **Primary repo:** `{self.gitlab_repo}`\n"
                    f"- **This (dep) repo:** `{gp}`\n"
                    f"- **Files committed:** {committed} "
                    f"({len(write_paths)} written, {actual_deleted} deleted)\n\n"
                    f"See the primary MR for the full design and other sibling MRs.\n"
                    f"_Generated by AiNxt SDLC pipeline (run `{self.run_id}`)._"
                )
                mr_res = gitlab_create_mr(
                    repo=gp, title=title, body=body,
                    head=sibling_branch, base=sibling_base,
                )
                if mr_res.startswith("[Error"):
                    logger.error(f"[SM {self.run_id}] sibling MR creation failed for {gp!r}: {mr_res}")
                    continue

                num_match = _re.search(r"\(!(\d+)\)", mr_res)
                url_match = _re.search(r"MR created:\s*(https://\S+)", mr_res)
                pr_url    = url_match.group(1).rstrip(")") if url_match else ""
                pr_number = int(num_match.group(1)) if num_match else None
                sibling_urls[gp] = pr_url

                try:
                    upsert_run_repo(
                        run_id=self.run_id, repo=gp,
                        ref=row.get("ref", ""), kind="editable",
                        working_branch=sibling_branch,
                        pr_url=pr_url, pr_number=pr_number,
                        state="MR_OPENED",
                    )
                except Exception as exc:
                    logger.warning(f"[SM {self.run_id}] sibling MR record update failed for {gp!r}: {exc}")

                self._add_event(
                    "COMMITTING", "ai-committer",
                    f"Sibling MR opened: {gp} -> {pr_url}",
                    {"repo": gp, "pr_url": pr_url, "files": committed,
                     "written": len(write_paths), "deleted": actual_deleted},
                )
                logger.info(f"[SM {self.run_id}] sibling MR opened for {gp!r}: {pr_url}")
            except Exception as exc:
                logger.error(f"[SM {self.run_id}] sibling MR for {gp!r} failed: {exc}")
                continue

        return sibling_urls

    def _cleanup_failed_branch(self) -> None:
        """Delete the per-run working branch on GitLab when the run ends in FAILED.

        Run-ID branches are unique per run, so deleting them on terminal failure
        prevents orphan branches from accumulating on GitLab. Skipped when:
          - state is not FAILED (e.g. AWAITING_PR_APPROVAL must keep the branch)
          - AINXT_KEEP_FAILED_BRANCH=1 (engineer wants to inspect)
          - no working_branch or gitlab_repo on the run
        Best-effort: failures are logged but never re-raised.
        """
        import os as _os
        if _os.getenv("AINXT_KEEP_FAILED_BRANCH") == "1":
            logger.info(
                f"[SM {self.run_id}] working branch kept for inspection "
                f"(AINXT_KEEP_FAILED_BRANCH=1)"
            )
            return
        if not self.working_branch or not self.gitlab_repo:
            return
        try:
            _run = get_run(self.run_id)
            _state = (_run.get("state") if _run else "") or ""
            if _state.upper() != "FAILED":
                return
        except Exception as _e:
            logger.warning(f"[SM {self.run_id}] branch-cleanup state check failed: {_e}")
            return
        try:
            from core.config import SCM_PROVIDER as _SCM
            if _SCM == "github":
                from tools.github_tools import github_delete_branch as _gldb
            else:
                from tools.gitlab_tools import gitlab_delete_branch as _gldb
            _result = _gldb(self.gitlab_repo, self.working_branch)
            logger.info(f"[SM {self.run_id}] {_result}")
        except Exception as _e:
            logger.warning(
                f"[SM {self.run_id}] SCM branch cleanup failed for "
                f"{self.working_branch!r}: {_e}"
            )

    def _llm_traced(self, phase: str, prompt: str, hint: str = "complex") -> str:
        """Call _llm() and record the prompt+output to the replay log for this run."""
        result = _llm(prompt, hint)
        self._record_replay_entry(phase, prompt, result or "")
        return result

    def _resolve_product_id(self) -> str:
        """Product scope for this run (mandatory) — read once from run.context and
        cached. Threaded into BuildManifestResolver.resolve() so a human-confirmed
        (product, repo) build version selects the right builder image."""
        cached = getattr(self, "_product_id_cache", None)
        if cached is not None:
            return cached
        pid = ""
        try:
            from store.sdlc_store import get_run
            ctx = (get_run(self.run_id) or {}).get("context") or {}
            pid = (ctx.get("product_id") or "").strip()
        except Exception:
            pid = ""
        self._product_id_cache = pid
        return pid

    def _load_sandbox_image_info(self, repo_name: str) -> None:
        """
        Resolve the ainxt-builder-* image for per-file syntax checking via
        BuildManifestResolver (new design). Only set for languages where per-file
        syntax checking works without the full project workspace mounted.
        """
        lang = (self.language or "").lower()
        if lang not in self._SYNTAX_CHECK_LANGUAGES:
            logger.debug(
                f"[SM {self.run_id}] per-file syntax check skipped for lang={lang!r} "
                f"(needs workspace — _build_check() handles compile validation)"
            )
            return
        try:
            from core.build_manifest_resolver import BuildManifestResolver
            manifest = BuildManifestResolver().resolve(
                repo_name, gitlab_path=self.gitlab_repo,
                workspace_path=self._run_workspace_path or "",
                product_id=self._resolve_product_id(),
            )
            if manifest and manifest.image:
                self.sandbox_image = manifest.image
                logger.debug(
                    f"[SM {self.run_id}] per-file syntax image={self.sandbox_image!r} "
                    f"lang={lang!r} (from BuildManifestResolver)"
                )
        except Exception as _e:
            logger.debug(f"[SM {self.run_id}] _load_sandbox_image_info failed: {_e}")

    # ── Public entry point ────────────────────────────────────

    def run(self):
        """Execute the state machine. Blocks until terminal state.

        Two modes (the HITL gate sits between them — "decide before the gate"):
          • pregate  → CODING → REVIEWING → REVIEW_GATE → TESTING(author+green),
            then _finalize_pregate() stores the VERIFIED_DIFF and RETURNS (no
            commit / no MR). The pipeline caller transitions to the approval gate.
          • postgate → APPLYING (deterministic re-apply + staleness rebase) →
            TEST_VERIFY → SLT_RUNNING → COMMITTING → MR_CREATION.
        """
        bind_context(correlation_id=self.run_id, pipeline_stage="sdlc_state_machine")
        try:
            if self.mode == "postgate":
                self._set_state("APPLYING")
                self._phase_applying()
            else:
                # CLI three-phase engine (hard cutover): the pre-gate path is now a
                # single merged IMPLEMENT (fresh Sonnet CLI session: code + tests +
                # drive-to-green) followed by the platform REVIEW gate. The old
                # per-file coder/test loops (_phase_coding/_phase_testing/_run_coder)
                # were removed by the 2026-07-01 three-phase CLI cutover.
                self._phase_implement()
        except Exception as _run_exc:
            from store.sdlc_store import SDLCCancelled as _SDLCCancelled
            if not isinstance(_run_exc, _SDLCCancelled):
                logger.error(
                    f"[SM {self.run_id}] run() {self.mode} exception: {_run_exc}",
                    run_id=self.run_id, mode=self.mode, error=str(_run_exc),
                )
            raise
        finally:
            # Always clean up the per-run workspace — terminal states for this
            # path are AWAITING_PR_APPROVAL / FAILED / MERGE_CONFLICT, all of
            # which leave run() before the next exception bubble. AI_ADDRESSING_COMMENTS
            # is a separate entry point and manages its own cleanup.
            self._cleanup_run_workspace()
            self._cleanup_failed_branch()

    # ── Post-gate workspace read/write + build-oracle helpers ──

    def _workspace_read(self, path: str):
        import os as _o
        ws = self._run_workspace_path or ""
        full = _o.path.join(ws, path) if ws else ""
        if full and _o.path.isfile(full):
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    return fh.read()
            except Exception:
                return None
        return None

    def _workspace_write(self, path: str, content: str) -> None:
        import os as _o
        ws = self._run_workspace_path or ""
        if not ws:
            raise RuntimeError("no run workspace to write into")
        full = _o.path.join(ws, path)
        _o.makedirs(_o.path.dirname(full) or ws, exist_ok=True)
        with open(full, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(content)

    def _build_oracle(self) -> dict:
        """Adapt _build_check() to the loop's run_build contract."""
        r = self._build_check()
        _status = r.get("_build_status") or ""
        return {"success": bool(r.get("success")),
                "errors": r.get("errors") or [],
                "output": r.get("output") or "",
                "transient": _status in ("INFRA_FAILURE", "TEST_INFRA_FAILURE",
                                         "BUILD_TIMEOUT", "UNKNOWN_ERROR")}

    def _build_check(self) -> dict:
        """
        Compile all generated impl files using the universal builder image.
        Generated files are written into the synced workspace before building.
        Returns {success, errors, output, _build_status, _missing_artifact}.
        """
        # Compilation globally skipped (baseline build was skipped on error via the
        # SUSPENDED gate's "Skip compilation & continue"). Report green so the
        # pipeline runs end-to-end without compiling — noted as a run event.
        if self.compile_skipped:
            logger.warning(f"[SM {self.run_id}] BUILD_CHECK skipped — compile_skipped=True")
            self._add_event("TESTING", "build-checker", "SKIPPED (compile_skipped=True)",
                            {"skipped": True, "reason": "compilation skipped on user request"})
            return {"success": True, "errors": [], "output": "compilation skipped",
                    "_build_status": "SKIPPED", "_missing_artifact": ""}

        import os as _os
        from core.build_manifest_resolver import BuildManifestResolver
        from sandbox.workspace_builder import WorkspaceBuilder

        impl_files = [f for f in self.code_output.get("files", []) if not f.get("is_test")]
        if not impl_files:
            return {"success": True, "errors": [], "output": "no impl files"}

        # Per-run isolated workspace — clone of the working branch.
        # Equivalent to a developer running `git clone -b <feature-branch>` on
        # their machine: no leftover files from other runs, concurrent-safe.
        # Materialize it BEFORE resolving the manifest so the build pattern is
        # detected from the real build files on the clone (no indexing required).
        try:
            workspace = self._ensure_run_workspace(self.repo)
        except Exception as _ws_exc:
            logger.error(f"[SM {self.run_id}] BUILD_CHECK: run-workspace prepare failed: {_ws_exc}")
            return {
                "success": False,
                "errors":  [f"Workspace prepare failed: {_ws_exc}"],
                "output":  "",
                "_build_status": "INFRA_FAILURE",
                "_missing_artifact": "",
            }

        manifest = BuildManifestResolver().resolve(
            self.repo, gitlab_path=self.gitlab_repo, workspace_path=workspace,
            product_id=self._resolve_product_id(),
        )
        if manifest is None or not manifest.image:
            return {
                "success": False,
                "errors":  [
                    f"No build manifest for '{self.repo}' — no recognizable build "
                    "files (pom.xml / build.gradle / package.json / .sdlc.yml) found "
                    "in the repo"
                ],
                "output":  "",
                "_build_status": "UNKNOWN_BUILD_PATTERN",
                "_missing_artifact": "",
            }

        # When multi-repo deps were installed during CODING, pass the per-run
        # m2 cache so the compile container sees the locally-built jars instead
        # of trying to pull them from Nexus (which would fail with DEPENDENCY_MISSING).
        # The per-run cache is seeded from the shared cache via hardlinks at
        # workspace prep, so public deps are warm; only the locally-built
        # internal jars are unique to this run. Without that seeding, Maven
        # re-downloads every transitive dep from Nexus and BUILD_CHECK can
        # blow past the 30-min RQ job timeout.
        _m2_override = None
        # The override only makes sense when compile-only deps were actually installed
        # (m2_cache is always created by _make_workspace_dirs, so checking it alone would
        # also fire for editable-only runs, pointing compile at a thin per-run cache and
        # suppressing the pom-hash content-addressed cache branch below for no reason).
        if self._mr_workspace and getattr(self._mr_workspace, "m2_cache", None) and self._compile_only_repos:
            import os as _mos
            _m2_override = self._mr_workspace.m2_cache
            if not _mos.path.isdir(_m2_override):
                _m2_override = None
            else:
                try:
                    _seeded_count = sum(1 for _ in _mos.scandir(_m2_override))
                except Exception:
                    _seeded_count = -1
                logger.info(
                    f"[SM {self.run_id}] BUILD_CHECK: using per-run m2 cache "
                    f"{_m2_override!r} for multi-repo dep visibility "
                    f"(top-level entries={_seeded_count} — should be >0 if shared-cache seeding worked)"
                )

        # Remember the MULTI-REPO per-run cache specifically: `_m2_override` may be
        # reassigned below to the single-repo pom-hash cache, and only the per-run
        # cache holds deps this run resolved itself (the pom-hash dir is already a
        # copy OF the shared cache, so merging it back would be a no-op at best).
        _mr_m2_for_writeback = _m2_override

        # ── Phase 8: pom-hash Maven dependency cache (JVM single-repo only) ──
        # Content-addressed by SHA-256 of pom.xml so that re-runs against the
        # same dependency manifest skip Maven's slow resolution phase entirely.
        # Only active for single-repo runs (multi-repo has its own _mr_workspace
        # cache seeded with locally-built internal jars — don't overwrite that).
        _pom_cache_pending = ""
        if _m2_override is None and (self.language or "").lower() in {"java", "kotlin", "scala"}:
            try:
                import hashlib as _hl
                from pathlib import Path as _PL
                _pom = _PL(workspace) / "pom.xml"
                if _pom.exists():
                    _pom_bytes = _pom.read_bytes()
                    if b"SNAPSHOT" in _pom_bytes:
                        # SNAPSHOT deps resolve to different jars over time even
                        # when pom.xml is unchanged — skip content-addressed cache
                        # entirely to avoid serving stale artifacts.
                        logger.info(
                            f"[SM {self.run_id}] m2 pom cache skipped "
                            f"— SNAPSHOT deps detected in pom.xml"
                        )
                    else:
                        _pom_hash = _hl.sha256(_pom_bytes).hexdigest()[:16]
                        _dep_cache_root = _os.getenv(
                            "M2_DEP_CACHE_ROOT", "/opt/ainxt/dep_cache"
                        )
                        _cache_dir = (
                            f"{_dep_cache_root}/{manifest.repo_slug}/{_pom_hash}/m2"
                        )
                        if _os.path.isdir(_cache_dir) and any(True for _ in _os.scandir(_cache_dir)):
                            _m2_override = _cache_dir
                            logger.info(
                                f"[SM {self.run_id}] m2 pom cache HIT "
                                f"pom_hash={_pom_hash} dir={_cache_dir!r}"
                            )
                        else:
                            _pom_cache_pending = _cache_dir
                            logger.info(
                                f"[SM {self.run_id}] m2 pom cache MISS "
                                f"pom_hash={_pom_hash} — will populate on success"
                            )
            except Exception as _pce:
                logger.debug(f"[SM {self.run_id}] m2 pom cache resolve error: {_pce}")

        logger.info(
            f"[SM {self.run_id}] BUILD_CHECK: {self.repo} "
            f"files={len(impl_files)} image={manifest.image} cmd={manifest.compile_cmd!r}"
        )

        self._write_generated_files_to_workspace(impl_files, workspace)

        try:
            result = WorkspaceBuilder().compile(
                manifest, self.run_id,
                workspace_path=workspace,
                m2_cache_override=_m2_override,
            )
        except Exception as exc:
            logger.error(f"[SM {self.run_id}] BUILD_CHECK exception: {exc}")
            return {
                "success": False,
                "errors":  [str(exc)],
                "output":  f"build check failed: {exc}",
                "_build_status": "UNKNOWN_ERROR",
                "_missing_artifact": "",
            }

        BuildManifestResolver().update_after_run(
            self.repo, result.status, result.missing_artifact or ""
        )

        success = result.status == "BUILD_SUCCESS"

        # ── Merge newly downloaded PUBLIC deps back into the shared m2 cache ──
        # The per-run cache is seeded FROM the shared cache but nothing wrote
        # back, so the shared cache stayed cold and every run re-fetched the same
        # third-party jars from Nexus. Only on green (a failed build's cache may
        # hold half-resolved state) and only for the multi-repo per-run cache;
        # internal org.* artifacts are excluded by the merge itself.
        if success and _mr_m2_for_writeback:
            try:
                from agents.multi_repo_workspace import merge_m2_cache_to_shared
                merge_m2_cache_to_shared(_mr_m2_for_writeback, label=self.run_id)
            except Exception as _wb:
                logger.warning(f"[SM {self.run_id}] shared m2 write-back failed: {_wb}")

        # ── Phase 8: populate pom cache after first successful build ──────────
        # Copy the shared .m2 repo to the content-addressed dir so future runs
        # with the same pom.xml get a warm cache and skip Maven resolution.
        if _pom_cache_pending and success:
            try:
                import shutil as _shu
                from pathlib import Path as _PL2
                from core import config as _cfg
                _shared_m2 = _os.path.join(_cfg.BUILDER_CACHE_ROOT, "root_.m2_repository")
                if _os.path.isdir(_shared_m2):
                    _PL2(_pom_cache_pending).mkdir(parents=True, exist_ok=True)
                    _shu.copytree(_shared_m2, _pom_cache_pending, dirs_exist_ok=True)
                    logger.info(
                        f"[SM {self.run_id}] m2 pom cache populated: "
                        f"{_pom_cache_pending!r}"
                    )
                else:
                    logger.debug(
                        f"[SM {self.run_id}] m2 pom cache: shared .m2 not found "
                        f"at {_shared_m2!r} — skipping populate"
                    )
            except Exception as _pcp:
                logger.warning(
                    f"[SM {self.run_id}] m2 pom cache populate failed: {_pcp}"
                )

        return {
            "success":           success,
            "errors":            result.error_lines[:10] if not success else [],
            "output":            result.output_tail or "",
            "_build_status":     result.status,
            "_missing_artifact": result.missing_artifact or "",
        }

    def _write_generated_files_to_workspace(self, files: list, workspace: str) -> None:
        """Write LLM-generated files into the synced workspace at their declared paths."""
        import os as _os
        for f in files:
            rel = (f.get("path") or "").lstrip("/")
            if not rel:
                continue
            abs_path = _os.path.join(workspace, rel)
            _os.makedirs(_os.path.dirname(abs_path), exist_ok=True)
            try:
                with open(abs_path, "w", encoding="utf-8") as fp:
                    fp.write(f.get("content", ""))
            except OSError as exc:
                logger.warning(
                    f"[SM {self.run_id}] _write_generated_files_to_workspace: "
                    f"{rel}: {exc}"
                )

    # ── Phase: TESTING ────────────────────────────────────────

    def _execute_tests(self, test_files: list, impl_files: list) -> dict:
        """
        Execute generated tests inside the universal builder container.
        impl + test files are written into the synced workspace, then the
        builder image runs the manifest's test_cmd via WorkspaceBuilder.
        Returns dict with test results: success (bool), passed/failed counts, output text, error lines, and internal status fields.
        """
        from core.build_manifest_resolver import BuildManifestResolver
        from sandbox.workspace_builder import WorkspaceBuilder

        # Reuse the per-run workspace materialized by _build_check (or create it
        # now if test execution runs without a prior build_check). Materialize it
        # BEFORE resolving so the manifest is read from the clone (no indexing).
        try:
            workspace = self._ensure_run_workspace(self.repo)
        except Exception as _ws_exc:
            logger.error(f"[SM {self.run_id}] EXECUTE_TESTS: run-workspace prepare failed: {_ws_exc}")
            return {
                "success": False, "output": f"Workspace prepare failed: {_ws_exc}",
                "passed": 0, "failed": 1,
                "_build_status": "INFRA_FAILURE", "_missing_artifact": "",
            }

        manifest = BuildManifestResolver().resolve(
            self.repo, gitlab_path=self.gitlab_repo, workspace_path=workspace,
            product_id=self._resolve_product_id(),
        )
        if manifest is None or not manifest.image:
            return {
                "success": False, "output": "No build manifest",
                "passed": 0, "failed": 1,
                "_build_status": "UNKNOWN_BUILD_PATTERN", "_missing_artifact": "",
            }

        logger.info(
            f"[SM {self.run_id}] EXECUTE_TESTS: {self.repo} "
            f"impl={len(impl_files)} tests={len(test_files)} image={manifest.image}"
        )

        # Log file contents for debugging
        for f in impl_files:
            logger.info(
                f"[SM {self.run_id}] IMPL FILE: {f.get('path','?')} "
                f"({len(f.get('content',''))} chars)\n{f.get('content','')}"
            )
        for f in test_files:
            logger.info(
                f"[SM {self.run_id}] TEST FILE: {f.get('path','?')} "
                f"({len(f.get('content',''))} chars)\n{f.get('content','')}"
            )

        self._write_generated_files_to_workspace(impl_files + test_files, workspace)

        _m2_override = None
        # See the matching comment above (BUILD_CHECK site): only override when
        # compile-only deps were actually installed for this run.
        if self._mr_workspace and getattr(self._mr_workspace, "m2_cache", None) and self._compile_only_repos:
            import os as _mos2
            _m2_override = self._mr_workspace.m2_cache
            if not _mos2.path.isdir(_m2_override):
                _m2_override = None

        # ── Scope the test phase to just this run's changed/added test files ──
        # Compile runs the whole-project compile_cmd (unchanged); the test phase
        # runs ONLY the tests this run authored, via the same containerized path,
        # by overriding the manifest's whole-suite test_cmd. None from the builder
        # (unknown/custom runner) → whole suite. Kill-switch: SDLC_SCOPE_TESTS_TO_CHANGED=false.
        import os as _osc
        from core.build_manifest_resolver import scoped_test_command
        _test_cmd_override = None
        if _osc.getenv("SDLC_SCOPE_TESTS_TO_CHANGED", "true").strip().lower() not in ("false", "0", "no"):
            _rel_paths = [f.get("path") for f in test_files if f.get("path")]
            _test_cmd_override = scoped_test_command(manifest.test_cmd, _rel_paths)
            if _test_cmd_override:
                logger.info(
                    f"[SM {self.run_id}] EXECUTE_TESTS: scoped to {len(_rel_paths)} "
                    f"changed test file(s) — cmd={_test_cmd_override!r}"
                )
            else:
                logger.info(
                    f"[SM {self.run_id}] EXECUTE_TESTS: no scoping applied "
                    f"(runner not scopable) — running full suite {manifest.test_cmd!r}"
                )

        try:
            result = WorkspaceBuilder().test(
                manifest, self.run_id,
                workspace_path=workspace,
                m2_cache_override=_m2_override,
                test_cmd_override=_test_cmd_override,
            )
        except Exception as exc:
            logger.warning(f"[SM {self.run_id}] test execution failed: {exc}")
            return {
                "success": False, "output": str(exc),
                "passed": 0, "failed": 1,
                "_build_status": "UNKNOWN_ERROR", "_missing_artifact": "",
            }

        BuildManifestResolver().update_after_run(
            self.repo, result.status, result.missing_artifact or ""
        )

        # Same shared-cache write-back as BUILD_CHECK — the test phase resolves
        # test-scope artifacts (surefire providers, JUnit, mocking libs) that the
        # compile phase never touches, so they too would be re-fetched every run.
        if result.status == "BUILD_SUCCESS" and _m2_override:
            try:
                from agents.multi_repo_workspace import merge_m2_cache_to_shared
                merge_m2_cache_to_shared(_m2_override, label=self.run_id)
            except Exception as _wb2:
                logger.warning(f"[SM {self.run_id}] shared m2 write-back failed: {_wb2}")

        td      = result.test_details
        passed  = td.passed if td else 0
        failed  = td.failed if td else 0
        success = result.status == "BUILD_SUCCESS"
        zero_tests = (td is None or td.total == 0) and not success

        if zero_tests:
            logger.error(
                f"[SM {self.run_id}] _execute_tests: zero test output — treating as FAIL"
            )

        logger.info(
            f"[SM {self.run_id}] test_runner: status={result.status} "
            f"passed={passed} failed={failed} duration={result.duration_secs}s"
        )

        return {
            "success":           success,
            "output":            result.output_tail or "",
            "passed":            passed,
            "failed":            failed,
            "zero_tests":        zero_tests,
            "error_lines":       result.error_lines or [],
            "failed_tests":      td.failed_tests if td else [],
            "_build_status":     result.status,
            "_missing_artifact": result.missing_artifact or "",
        }

    # ── Test file generation (when coder omits tests) ─────────

    def _record_replay_entry(self, phase: str, prompt: str, output: str) -> None:
        """
        Append a replay entry to Redis for this run.
        Stored at sdlc:replay:{run_id} as a Redis list (RPUSH).
        Entries are JSON: {ts, phase, prompt_hash, prompt_preview, output_preview, chars_in, chars_out}.
        TTL: 7 days — enough for post-mortem, not forever.
        """
        try:
            import hashlib, json as _json, time as _t
            from core.config import REDIS_HOST as _H, REDIS_PORT as _P
            import redis as _r
            rc  = _r.Redis(host=_H, port=_P, db=2, decode_responses=True,
                           socket_connect_timeout=1)
            key = f"sdlc:replay:{self.run_id}"
            entry = _json.dumps({
                "ts":             int(_t.time()),
                "phase":          phase,
                "prompt_hash":    hashlib.sha256(prompt.encode()).hexdigest()[:16],
                "prompt_preview": prompt[:200],
                "output_preview": output[:200],
                "chars_in":       len(prompt),
                "chars_out":      len(output),
            })
            rc.rpush(key, entry)
            rc.expire(key, 7 * 24 * 3600)  # 7-day TTL
        except Exception:
            pass  # replay is best-effort — never block the pipeline

    # ── Phase: SLT_RUNNING ────────────────────────────────────

    def _phase_slt_running(self):
        """Execute Service Level Tests. On pass → COMMITTING (COMPLETION_REVIEW is unreachable)."""
        logger.info(f"[SM {self.run_id}] SLT_RUNNING")
        slt_files = self.slt_output.get("slt_files", [])

        if self.skip_tests:
            # skip_tests=True means the caller already decided to skip; go straight to commit.
            logger.warning(f"[SM {self.run_id}] SLT_RUNNING: skip_tests=True — bypassing SLT → COMMITTING")
            self._add_event("SLT_RUNNING", "slt-runner", "SKIPPED (skip_tests=True)",
                            {"skipped": True})
            self._set_state("COMMITTING")
            self._phase_commit()
            return

        if not slt_files:
            logger.warning(f"[SM {self.run_id}] SLT_RUNNING: no SLT files → COMMITTING")
            self._set_state("COMMITTING")
            self._phase_commit()
            return

        slt_result = self._execute_tests(slt_files, [])
        self._add_event("SLT_RUNNING", "slt-runner",
                        f"success={slt_result['success']}",
                        slt_result)

        if slt_result.get("success"):
            logger.info(f"[SM {self.run_id}] SLT_RUNNING: PASS → COMMITTING")
            self._set_state("COMMITTING")
            self._phase_commit()
            return

        # POST-GATE deterministic verification (three-phase cutover): the old pre-gate
        # FIXING→TESTING→REVIEWING recovery chain is gone. On a red SLT the run
        # SUSPENDS to the approval state for human re-review.
        logger.warning(f"[SM {self.run_id}] SLT_RUNNING: FAIL → SUSPENDED")
        self._suspend(
            self._approval_state(),
            f"Service-level tests failed on the applied tree — re-review required. "
            f"Output: {slt_result.get('output', '')[:300]}"
        )
        return

    # ══════════════════════════════════════════════════════════════════════
    # Decide-before-the-gate: pre-gate finalize + post-gate deterministic apply
    # ══════════════════════════════════════════════════════════════════════

    def _proceed_post_tests(self, *, tests_skipped: bool = False):
        """Single chokepoint reached once code is review-clean and unit tests are
        green (or intentionally skipped).

        PRE-GATE  → store the VERIFIED_DIFF and STOP (the human approves the real,
                    compiled, test-green diff — never a JSON plan).
        POST-GATE / legacy → run SLT then commit (skip_tests bypasses SLT).
        """
        if self.mode == "pregate":
            self._finalize_pregate(tests_skipped=tests_skipped)
            return
        if self.skip_tests or tests_skipped:
            self._set_state("COMMITTING")
            self._phase_commit()
        else:
            self._set_state("SLT_RUNNING")
            self._phase_slt_running()

    def _pregate_compile_summary(self) -> dict:
        """Compile status for the VERIFIED_DIFF. compile_skipped degrades to a
        green-SKIPPED summary (mirrors _build_check) so a compile-waived run still
        reaches the gate with a real-but-waived diff."""
        if self.compile_skipped:
            return {"passed": True, "skipped": True,
                    "summary": "compilation skipped (compile_skipped=True)"}
        return {"passed": self._conf_build >= 1.0, "skipped": False,
                "summary": f"conf_build={self._conf_build:.2f}"}

    # ══════════════════════════════════════════════════════════════════════
    # CLI three-phase engine — merged IMPLEMENT + REVIEW (pre-gate) + diff capture
    # ══════════════════════════════════════════════════════════════════════

    def _git(self, args: list) -> str:
        """Run a git command in the run workspace and return stdout ('' on error).
        Used for diff/show plus `add -A` staging (index only — never edits file
        contents or commits) so newly-created files surface in the diff."""
        import subprocess
        try:
            p = subprocess.run(
                ["git", "-C", self._run_workspace_path] + list(args),
                capture_output=True, text=True, timeout=120,
            )
            return p.stdout or ""
        except Exception as _e:
            logger.debug(f"[SM {self.run_id}] git {args[:2]} failed: {_e}")
            return ""

    def _git_in(self, cwd: str, args: list, *, check: bool = False) -> str:
        """Run a git command in an EXPLICIT directory and return stdout ('' on error).

        Sibling of `_git`, which is hardcoded to `self._run_workspace_path` (the
        primary repo) and must stay that way — every existing caller depends on
        it. This variant is what lets `_collect_dep_edits` diff each staged dep
        checkout against its OWN pinned ref inside `.sdlc_deps/{slug}/`.

        `check` (keyword-only, default False) preserves the historical fail-open
        behaviour for every existing caller — a git failure returns "" ,
        indistinguishable from "no changes". Pass `check=True` for a structural
        git call whose failure must NOT be silently swallowed as "no changes":
        on a non-zero exit this raises `subprocess.CalledProcessError` (or
        `subprocess.TimeoutExpired` on timeout) instead of returning "".
        """
        import subprocess
        if check:
            p = subprocess.run(
                ["git", "-C", cwd] + list(args),
                capture_output=True, text=True, timeout=120, check=True,
            )
            return p.stdout or ""
        try:
            p = subprocess.run(
                ["git", "-C", cwd] + list(args),
                capture_output=True, text=True, timeout=120,
            )
            return p.stdout or ""
        except Exception as _e:
            logger.debug(f"[SM {self.run_id}] git {args[:2]} in {cwd} failed: {_e}")
            return ""

    def _compliance_scan_edits(self, edits: list, *, stage: str, repo_label: str = "") -> bool:
        """Compliance-on-diff over a rich edit list. Returns True when clean,
        False when a violation blocked (the run is already SUSPENDED).

        Extracted verbatim from `_collect_workspace_edits` so the dep write-back
        path (`_collect_dep_edits`) runs the IDENTICAL scan — no dep code may
        reach a customer MR without passing `compliance_engine` (CLAUDE.md: all
        input and output).

        The messages endpoint only scans what the CLI SENDS, not what it WRITES,
        so the platform must scan the written delta itself. Scan the ADDED delta
        only (new files = whole body; modified = added lines) — re-scanning a
        whole modified file would re-flag legacy banking content this change
        never introduced (the false-positive that once dropped
        postgres/03_tables.sql — see project_sdlc_run_bf7a6a8d).

        `repo_label` only enriches the log/suspend message for dep scans; when
        empty the messages stay byte-identical to the historical primary path.
        """
        import difflib as _difflib
        from agents.compliance_engine import compliance_engine
        for e in (edits or []):
            if e["deleted"] or not e["new_body"]:
                continue
            if e["is_new"] or not e["base_body"]:
                _scan_text = e["new_body"]
            else:
                _added = [ln[1:] for ln in _difflib.ndiff(
                    e["base_body"].splitlines(), e["new_body"].splitlines())
                    if ln.startswith("+ ")]
                _scan_text = "\n".join(_added)
            if not _scan_text.strip():
                continue
            try:
                comp = compliance_engine.validate_input(_scan_text)
            except Exception as _ce:
                logger.debug(f"[SM {self.run_id}] compliance scan error for {e['path']}: {_ce}")
                continue
            # Authoritative block signal is the top-level `blocked`/`blocked_types`
            # (validate_input Step 4), not a per-finding flag.
            blocked = comp.get("blocked_types") or (
                [f.get("type") for f in (comp.get("findings") or []) if f.get("blocked")]
                if comp.get("blocked") else []
            )
            if comp.get("blocked") or blocked:
                if repo_label:
                    logger.warning(
                        "[SM] compliance-on-diff block on dep diff", run_id=self.run_id,
                        repo=repo_label, violation=blocked, path=e["path"],
                    )
                    self._suspend(stage, f"compliance: {blocked} in {repo_label}:{e['path']}")
                else:
                    logger.warning(
                        "[SM] compliance-on-diff block", run_id=self.run_id,
                        stage=stage, violation=blocked,
                    )
                    self._suspend(stage, f"compliance: {blocked} in {e['path']}")
                return False
        return True

    def _collect_workspace_edits(self, *, stage: str = "IMPLEMENT", base_override: str = None):
        """Step 4: capture the CLI's workspace changes as VERIFIED_DIFF edits via
        `git diff` vs the pinned base_sha, run compliance-on-diff on the added/
        changed delta, and feed the entries into self.code_output['files'] so the
        existing _finalize_pregate() → _build_verified_edits() → VERIFIED_DIFF path
        works unchanged (base_sha pin intact).

        `stage` labels the compliance-block suspend so the resume restarts the phase
        that actually ran the collection. It defaults to IMPLEMENT (the original and
        most common caller); the REVIEW fix round and the two governance loops pass
        their own stage, otherwise a block during e.g. a governance fixer round would
        suspend at IMPLEMENT and the resume would re-run the whole implement phase.

        `base_override` (scan-unify 2026-07-28): when set, diff against THIS ref instead
        of the pinned base_sha. The governance END-GATE runs on a freshly-cloned branch
        whose change is ALREADY committed at HEAD — with `SDLC_REUSE_RUN_WORKSPACE` off,
        the pinned base_sha is empty → falls back to `"HEAD"` → `git diff --cached HEAD`
        on a clean tree = an EMPTY diff (a false-green gate). The end-gate passes the
        merge-base against the MR base branch here so the full committed MR diff is seen.
        `git add -A` still makes the index equal the committed HEAD tree, so compliance-
        on-diff runs on the same delta.

        Returns the rich edit list (each {path, kind, is_new, is_test, new_body,
        base_body, deleted}); [] when there are no changes; or None when a
        compliance violation blocked the diff (the run is already SUSPENDED)."""
        from agents.sdlc_patch_engine import restore_missing_imports
        ws = self._run_workspace_path
        if not ws:
            logger.warning(f"[SM {self.run_id}] _collect_workspace_edits: no workspace")
            return []
        base_sha = base_override or self._get_run_base_sha() or "HEAD"
        # Stage everything first (intent-to-add) so NEWLY-CREATED files the CLI wrote
        # are visible to `git diff` — a plain `git diff <base>` omits untracked files,
        # which would silently drop every new file (and skip compliance on them).
        self._git(["add", "-A"])
        name_status = self._git(["diff", "--cached", "--name-status", base_sha])

        edits: list = []
        for line in (name_status or "").splitlines():
            line = line.rstrip("\n")
            if not line.strip():
                continue
            parts = line.split("\t")
            status = parts[0].strip()
            # Rename (Rxx) reports old\tnew — take the new path; treat as add of new.
            path = parts[-1].strip()
            if not path:
                continue
            # ── Defence-in-depth (Step 4.5): `.sdlc_deps/` is appended to
            #    .git/info/exclude BEFORE the staging dir is created
            #    (agents/multi_repo_workspace._make_workspace_dirs), so a vendored
            #    dep tree must never appear in the primary diff. If one ever does,
            #    DROP the entry — dep source reaching the primary VERIFIED_DIFF /
            #    customer MR is not recoverable. This guards the exclude; it does
            #    not replace it.
            _unq = path[1:] if path.startswith('"') else path
            if _unq.startswith(".sdlc_deps/"):
                logger.warning(
                    "[SM] .sdlc_deps/ path in primary diff — exclude leaked, entry dropped",
                    run_id=self.run_id, path=path,
                )
                continue
            is_new = status.startswith("A") or status.startswith("R") or status.startswith("C")
            deleted = status.startswith("D")
            is_test = _is_test_path(path)
            new_body = "" if deleted else self._read_ws_file(path)
            base_body = "" if is_new else self._git(["show", f"{base_sha}:{path}"])
            # Restore imports the full-file regen may have silently dropped (mirrors
            # the post-gate _apply_verified_edits guard — see CLAUDE.md).
            if (not deleted) and (not is_new) and new_body and base_body:
                try:
                    new_body = restore_missing_imports(new_body, base_body, self.language)
                except Exception as _rie:
                    logger.debug(f"[SM {self.run_id}] restore_missing_imports skipped for {path}: {_rie}")
            edits.append({
                "path": path,
                "kind": "slt" if "slt" in path.lower() else "code",
                "is_new": is_new,
                "is_test": is_test,
                "new_body": new_body,
                "base_body": base_body,
                "deleted": deleted,
            })

        # ── Compliance-on-diff (shared helper — see _compliance_scan_edits for the
        #    added-delta-only rationale). Returns None on a block so the three
        #    `if edits is None` call sites keep short-circuiting unchanged.
        if not self._compliance_scan_edits(edits, stage=stage):
            return None

        # ── Feed into code_output['files'] so _build_verified_edits (which reads
        #    f['content'] and re-reads base_body from GitLab@base_branch) builds the
        #    VERIFIED_DIFF unchanged. Deletes carry content='' + deleted flag.
        self.code_output = {**self.code_output, "files": [
            {
                "path": e["path"],
                "content": e["new_body"],
                "is_new": e["is_new"],
                "is_test": e["is_test"],
                "deleted": e["deleted"],
            }
            for e in edits
        ]}

        _code_n = sum(1 for e in edits if e["kind"] == "code")
        _slt_n = sum(1 for e in edits if e["kind"] == "slt")
        _new_n = sum(1 for e in edits if e["is_new"])
        _del_n = sum(1 for e in edits if e["deleted"])
        logger.info(
            "[SM] diff capture", run_id=self.run_id, changed=len(edits),
            code_n=_code_n, slt_n=_slt_n, new_n=_new_n, deleted_n=_del_n,
        )
        return edits

    def _read_ws_file(self, path: str) -> str:
        """Read a working-tree file (best-effort, '' on any error)."""
        import os as _os
        try:
            _full = _os.path.join(self._run_workspace_path, path.replace("\\", "/"))
            with open(_full, "r", encoding="utf-8", errors="replace") as _fh:
                return _fh.read()
        except Exception:
            return ""

    def _read_dep_file(self, root: str, path: str) -> str:
        """Read a file from a staged dep checkout (best-effort, '' on any error).
        Dep-scoped mirror of `_read_ws_file`, which is hardcoded to the primary
        workspace."""
        import os as _os
        try:
            _full = _os.path.join(root, path.replace("\\", "/"))
            with open(_full, "r", encoding="utf-8", errors="replace") as _fh:
                return _fh.read()
        except Exception:
            return ""

    def _collect_dep_edits(self, *, stage: str = "IMPLEMENT"):
        """Step 4: capture each EDITABLE dep repo's workspace changes, run the
        IDENTICAL compliance-on-diff scan over them, and persist the result to run
        context as `code_output_by_repo`.

        This is the missing PRODUCER for three consumers that were written but had
        never once executed: `_create_sibling_mrs` (reads
        `run.context["code_output_by_repo"][repo]["files"]`), the `_dep_has_files`
        COMMITTING gate, and `_build_pr_description`'s "Related MRs" section.

        Returns `{gitlab_repo: [edit_dicts]}` for deps that produced edits ({} when
        none did), or None when a compliance violation blocked a dep diff — in which
        case the run is ALREADY SUSPENDED and the caller must return immediately,
        exactly as it does for `_collect_workspace_edits() is None`.

        Never touches `compile-only` deps: they are chmod'd read-only at the FS level
        on purpose (`stage_deps_for_cli`), so a write there is a bug to surface, not
        to collect. Only `kind == "editable"` rows are considered.

        Single-repo runs (no editable dep rows) return {} before any git call,
        filesystem write, or context write — but do perform one `sdlc_run_repos`
        lookup (`_get_run_repos_info`) first.

        `stage` is the pipeline stage this collection belongs to — it labels both
        the compliance scan and any resulting suspend, so a resume restarts at the
        RIGHT phase. Defaults to "IMPLEMENT" (the first call site, unchanged);
        the REVIEW fix round passes stage="REVIEW" so a dep compliance block there
        no longer re-runs the whole IMPLEMENT phase on resume.
        """
        import os as _os

        rows = self._get_run_repos_info()
        editable = [r for r in (rows or []) if r.get("kind") == "editable"]
        if not editable:
            return {}

        out: dict = {}
        for row in editable:
            repo = (row.get("repo") or "").strip()
            if not repo:
                continue

            # Resolve the staged checkout: in-memory MultiRepoWorkspace first (set by
            # _setup_multi_repo_workspace earlier in THIS phase), then the persisted
            # sdlc_run_repos.workspace_path, then the deterministic layout.
            dep_path = ""
            _ws = getattr(self, "_mr_workspace", None)
            if _ws is not None:
                dep_path = (getattr(_ws, "dep_paths", None) or {}).get(repo) or ""
            if not dep_path:
                dep_path = (row.get("workspace_path") or "").strip()
            if not dep_path and self._run_workspace_path:
                # Must match agents/multi_repo_workspace.py's `_slug_for` exactly.
                _slug = repo.replace("/", "__").replace("..", "_").strip()
                dep_path = _os.path.join(
                    self._run_workspace_path, ".sdlc_deps", _slug,
                )
            if not dep_path or not _os.path.isdir(dep_path):
                logger.warning(
                    "[SM] editable dep has no staged checkout — dep diff skipped",
                    run_id=self.run_id, repo=repo,
                )
                continue

            # Diff each dep against ITS OWN pinned ref_sha, NEVER the primary's
            # base_sha — the two repos share no commit history.
            # NOTE: if preflight left ref_sha empty we fall back to HEAD. A
            # HEAD-relative diff still surfaces the CLI's uncommitted edits, which is
            # the most we can honestly report without a pin; it is deliberately not
            # treated as a hard failure.
            ref_sha = (row.get("ref_sha") or "").strip() or "HEAD"

            # Stage first (intent-to-add) so NEWLY-CREATED dep files are visible to
            # `git diff` — mirrors the primary path in _collect_workspace_edits.
            # Both structural calls use check=True (Fix D): a failed `add -A` or
            # `diff --cached` here would otherwise return "" indistinguishable from
            # "no changes", silently dropping this dep's edits with no sibling MR
            # ever appearing. A suspend surfaces the failure instead.
            try:
                self._git_in(dep_path, ["add", "-A"], check=True)
                name_status = self._git_in(
                    dep_path, ["diff", "--cached", "--name-status", ref_sha], check=True
                )
            except Exception as _dge:
                logger.error(
                    "[SM] dep git command failed — suspending run (a silently-skipped "
                    "dep is worse than a suspend, since the sibling MR would never appear)",
                    run_id=self.run_id, repo=repo, error=str(_dge), stage=stage,
                )
                self._suspend(stage, f"dep git command failed for {repo}: {_dge}")
                return None

            dep_edits: list = []
            for line in (name_status or "").splitlines():
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                parts = line.split("\t")
                status = parts[0].strip()
                # Rename (Rxx) reports old\tnew — take the new path; treat as add of new.
                path = parts[-1].strip()
                if not path:
                    continue
                is_new = status.startswith("A") or status.startswith("R") or status.startswith("C")
                deleted = status.startswith("D")
                # NOTE: restore_missing_imports is deliberately NOT applied to dep
                # bodies — it is language-specific and `self.language` describes the
                # PRIMARY repo, which may differ from the dep's language.
                dep_edits.append({
                    "path": path,
                    "kind": "slt" if "slt" in path.lower() else "code",
                    "is_new": is_new,
                    "is_test": _is_test_path(path),
                    "new_body": "" if deleted else self._read_dep_file(dep_path, path),
                    "base_body": "" if is_new else self._git_in(
                        dep_path, ["show", f"{ref_sha}:{path}"]
                    ),
                    "deleted": deleted,
                })

            if not dep_edits:
                # INFO, not WARNING (Fix D): an editable dep the coder legitimately
                # didn't need to touch is the common case, not an anomaly.
                logger.info(
                    "[SM] editable dep produced an empty diff",
                    run_id=self.run_id, repo=repo,
                )
                continue

            # Identical compliance gate as the primary path. A block SUSPENDS the run
            # (inside the helper) — bail out so no dep code can slip past it.
            if not self._compliance_scan_edits(dep_edits, stage=stage, repo_label=repo):
                return None

            logger.info(
                "[SM] dep diff captured", run_id=self.run_id, repo=repo,
                file_count=len(dep_edits), ref_sha=ref_sha,
            )
            out[repo] = dep_edits

        # ── Publish the result. Reaching this line means the editable-dep scan
        # GENUINELY RAN: single-repo runs already returned {} at the top of this
        # method, before any git call, filesystem read or context write. That
        # scoping is what makes an empty `out` here meaningful — it means "the deps
        # have no changes RIGHT NOW", not "this caller never looked".
        #
        # The in-memory review/approval-visibility copy is assigned
        # UNCONDITIONALLY (it used to be guarded by `if out:`). It is the source
        # for the diff Opus reviews (_build_review_diff) and the diff the human
        # approves (_build_dep_approval_sections); if a REVIEW fix round reverts a
        # dep edit, a stale copy would show both of them a hunk that no longer
        # exists on disk. Rich edits (base_body + new_body) — needed to render a
        # diff; NOT an appliable primary edit list.
        self._dep_edits = out

        # The PERSISTED copy is cleared on an empty scan for the same reason,
        # scoped the same way. `_create_sibling_mrs` pushes straight from this
        # key, so leaving a reverted dep behind here would open a sibling MR for
        # changes that no longer exist — strictly worse than the "risk" the old
        # `if out:` guard was protecting against (an unrelated caller that never
        # ran the scan wiping a legitimate key, which cannot happen from inside
        # this method). update_run_state does a top-level dict .update(), so
        # writing {} replaces the whole key rather than merging.
        try:
            from store.sdlc_store import patch_run_context
            patch_run_context(self.run_id, {"code_output_by_repo": {
                _repo: {"files": [
                    {
                        "path": e["path"],
                        "content": e["new_body"],
                        "is_test": e["is_test"],
                        "is_new": e["is_new"],
                        "deleted": e["deleted"],
                    }
                    for e in _edits
                ]}
                for _repo, _edits in out.items()
            }})
            if not out:
                logger.info(
                    "[SM] editable deps produced no changes — code_output_by_repo "
                    "cleared so no stale sibling MR is opened",
                    run_id=self.run_id, stage=stage, dep_count=len(editable),
                )
        except Exception as _pe:
            logger.warning(
                "[SM] code_output_by_repo persist failed — sibling MRs will be skipped",
                run_id=self.run_id, error=str(_pe),
            )
        return out

    def _build_unified_diff(self, edits: list) -> str:
        """The diff-only payload the REVIEW phase consumes: per-file
        base_body→new_body unified diff text. No full bodies beyond the hunks."""
        import difflib
        chunks: list = []
        for e in (edits or []):
            path = e.get("path") or ""
            base = (e.get("base_body") or "").splitlines()
            new = (e.get("new_body") or "").splitlines()
            ud = difflib.unified_diff(
                base, new, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
            )
            text = "\n".join(ud)
            if text.strip():
                chunks.append(text)
        return "\n".join(chunks)

    @staticmethod
    def _dep_slug(repo: str) -> str:
        """`group/project` -> `group__project`. Must match
        agents/multi_repo_workspace.py::_slug_for exactly (the authority) — it is
        the on-disk directory name under `.sdlc_deps/`."""
        return (repo or "").replace("/", "__").replace("..", "_").strip()

    def _build_review_diff(self, edits: list) -> str:
        """The diff text the REVIEW phase consumes.

        Single-repo runs (no editable-dep edits captured) return
        `_build_unified_diff(edits)` VERBATIM — the REVIEW input stays
        byte-identical to the pre-multi-repo behaviour.

        Multi-repo runs append one clearly-delimited section per EDITABLE dep that
        the coder touched, so Opus actually reviews the changes that
        `_create_sibling_mrs()` will push to a SECOND customer repository. Without
        this the dep diff reached a customer MR having passed only the compliance
        scan — never the review gate.

        Dep hunks are labelled with the dep's GitLab project AND are rendered with
        their `.sdlc_deps/{slug}/` WORKSPACE-relative path, not their bare
        repo-relative path. Two reasons: (a) the model cannot mistake a dep file for
        a primary-tree file, and (b) when REVIEW blocks, `blocking_issues[].file`
        is echoed verbatim into the fix-round prompt — a workspace-relative path is
        one the CLI can actually open (its cwd is the primary workspace root, and
        the dep tree is staged inside it), whereas a bare dep-relative path would
        point at a non-existent primary file."""
        primary = self._build_unified_diff(edits)
        dep_map = getattr(self, "_dep_edits", None) or {}
        if not dep_map:
            return primary

        sections: list = []
        _file_n = 0
        for repo, dep_edits in dep_map.items():
            prefix = f".sdlc_deps/{self._dep_slug(repo)}/"
            text = self._build_unified_diff([
                {**e, "path": f"{prefix}{e.get('path') or ''}"}
                for e in (dep_edits or [])
            ])
            if not text.strip():
                continue
            _file_n += len(dep_edits or [])
            sections.append(
                "\n"
                "# ═══════════════════════════════════════════════════════════════\n"
                f"# SEPARATE SIBLING REPOSITORY: {repo}\n"
                "# These files are NOT part of the primary repository tree. They\n"
                f"# live in the dependent repository {repo}, checked out inside\n"
                f"# the workspace at `{prefix}`, and they will be pushed as a\n"
                "# SEPARATE merge request against that repository.\n"
                "# Paths below are workspace-relative — refer to them exactly as\n"
                "# shown if you need to report an issue in one of these files.\n"
                "# ═══════════════════════════════════════════════════════════════\n"
                f"{text}"
            )
        if not sections:
            return primary

        logger.info(
            "[SM] dep diffs folded into REVIEW input", run_id=self.run_id,
            repo_count=len(sections), file_count=_file_n,
        )
        header = (
            f"# PRIMARY REPOSITORY: {self.gitlab_repo}\n"
            "# Paths below are relative to the primary repository root.\n"
            "# ═══════════════════════════════════════════════════════════════\n"
        )
        return header + primary + "\n" + "\n".join(sections)

    def _build_dep_approval_sections(self) -> dict:
        """The `dep_edits_by_repo` section of the VERIFIED_DIFF artifact — the
        editable-dep changes rendered for the HUMAN approver at
        AWAITING_CODE_APPROVAL / AWAITING_SOLUTION_APPROVAL.

        Returns {} for single-repo runs (the caller then omits the key entirely, so
        the artifact stays byte-identical to today).

        DELIBERATELY a SEPARATE, distinctly-keyed section rather than extra entries
        in the artifact's `edits` list: `edits` is the PRIMARY repo's apply set —
        `_apply_verified_edits()` writes each of those paths into the primary
        workspace and `gitlab_batch_commit` pushes them to the primary repo. A dep
        path in there would commit dependency source into the primary repository.
        Every entry here carries `appliable: False` to make that contract explicit.

        Per-file entries mirror the primary `edits` shape (path/kind/is_new/is_test/
        new_body/base_body, plus `deleted`) so a renderer can reuse the same
        base→new diff view. `path` is REPO-relative — it is the path the sibling MR
        will carry — with the workspace location given once, per repo, in
        `workspace_path`."""
        dep_map = getattr(self, "_dep_edits", None) or {}
        out: dict = {}
        for repo, dep_edits in dep_map.items():
            files = [
                {
                    "path": e.get("path") or "",
                    "kind": e.get("kind") or "code",
                    "is_new": bool(e.get("is_new")),
                    "is_test": bool(e.get("is_test")),
                    "deleted": bool(e.get("deleted")),
                    "new_body": e.get("new_body") or "",
                    "base_body": e.get("base_body") or "",
                }
                for e in (dep_edits or [])
                if (e.get("path") or "")
            ]
            if not files:
                continue
            out[repo] = {
                "repo": repo,
                "workspace_path": f".sdlc_deps/{self._dep_slug(repo)}/",
                # Review/approval VISIBILITY only — never fed to _apply_verified_edits
                # or the primary commit. Pushed by _create_sibling_mrs() as its own MR.
                "appliable": False,
                "mr": "sibling",
                "edits": files,
            }
        return out

    @staticmethod
    def _implement_drives_tests_green() -> bool:
        """Whether IMPLEMENT drives the FULL test suite green pre-gate (Option 1)
        or only compiles + authors tests and defers execution to the post-gate
        TEST_VERIFY phase (Option 2, default). Env-overridable via
        SDLC_IMPLEMENT_DRIVE_TESTS_GREEN; read at call time (no worker restart).

        Default False = Option 2: the suite is run once, post-gate, by the phase
        built for it — removing the slowest, flakiest loop from IMPLEMENT. Set to
        true to restore the old author-and-drive-suite-green behavior."""
        import os as _os
        return _os.getenv("SDLC_IMPLEMENT_DRIVE_TESTS_GREEN", "false").strip().lower() in ("1", "true", "yes")

    def _dep_block_for_prompt(self) -> str:
        """Best-effort ``dependent_repos_clause()`` text for IMPLEMENT / continue /
        fix-round prompts (Step 3, multi-repo CLI visibility). Sources rows from
        ``self._mr_workspace`` (already staged by ``_setup_multi_repo_workspace``
        earlier in THIS phase — reconstructed from ``dep_paths`` + the parallel
        ``self._compile_only_repos`` list, no extra store round-trip) when set,
        otherwise falls back to ``store.sdlc_store.list_run_repos`` (e.g. a fresh
        state-machine instance resuming REVIEW/fix-round without having re-run
        ``_setup_multi_repo_workspace``). "" for a single-repo run — a
        prompt-decoration failure must never break IMPLEMENT."""
        try:
            from agents.sdlc_implement_prompt import dependent_repos_clause
            ws = getattr(self, "_mr_workspace", None)
            dep_paths = getattr(ws, "dep_paths", None) if ws is not None else None
            if dep_paths:
                _compile_only = set(getattr(self, "_compile_only_repos", None) or [])
                rows = [
                    {"repo": r, "kind": "compile-only" if r in _compile_only else "editable"}
                    for r in dep_paths.keys()
                ]
            else:
                from store.sdlc_store import list_run_repos
                rows = list_run_repos(self.run_id) or []
            return dependent_repos_clause(rows)
        except Exception as _dep_e:
            logger.debug(f"[SM {self.run_id}] dep_block_for_prompt failed (non-fatal): {_dep_e}")
            return ""

    def _build_implement_prompt(self, plan: dict) -> str:
        """Holistic IMPLEMENT prompt from the approved PLAN. Delegates the pure
        assembly to ``agents.sdlc_implement_prompt.build_implement_prompt`` (shared
        verbatim with the offline probe) and supplies the two run-scoped pieces:
        the corrective engineer feedback on a go-back, and the B(ii) warm-start
        file inlining. Verification depth depends on mode: compile-green only by
        default (Option 2 — TEST_VERIFY runs the suite post-gate), full drive-to-green
        when SDLC_IMPLEMENT_DRIVE_TESTS_GREEN is set (Option 1); skip_tests drops the
        test half entirely (and strips test files from the injected plan). Every
        variant carries the explicit STOP/termination contract."""
        from agents.sdlc_implement_prompt import build_implement_prompt as _bip
        plan = plan or {}
        logger.debug(
            "[SM] IMPLEMENT prompt builder", run_id=self.run_id,
            plan_has_keys=list(plan.keys()) if isinstance(plan, dict) else [],
            skip_tests=self.skip_tests,
        )
        # On a resume / go-back (Request Changes), lead with the engineer's
        # corrective feedback so the re-implement is steered decisively.
        _fb = (getattr(self, "_resume_feedback", "") or "").strip()
        # B(ii) head-start: inline the CURRENT bodies of the files PLAN already
        # grounded, so the coder doesn't spend turns re-Reading them.
        _files_block = self._inline_files_block(plan)
        return _bip(
            plan,
            language=self.language or "unknown",
            skip_tests=self.skip_tests,
            drives_tests_green=self._implement_drives_tests_green(),
            feedback=_fb,
            files_block=_files_block,
            workspace_root=self._run_workspace_path or "",
            governance_block=self._governance_awareness(),
            dep_block=self._dep_block_for_prompt(),
        )

    def _build_continue_prompt(self) -> str:
        """The bounded auto-continue / manual-resume prompt (delegated to the shared
        builder). Honours skip_tests — the previous inline prompt hard-coded
        'author the tests the plan calls for' even on skip_tests runs — and carries
        the same STOP contract so a resume also ends as soon as the work is done."""
        from agents.sdlc_implement_prompt import build_continue_prompt as _bcp
        return _bcp(
            skip_tests=self.skip_tests,
            drives_tests_green=self._implement_drives_tests_green(),
            workspace_root=self._run_workspace_path or "",
            governance_block=self._governance_awareness(),
            dep_block=self._dep_block_for_prompt(),
        )

    def _inline_files_block(self, plan: dict) -> str:
        """B(ii): inline the CURRENT contents of the plan's EXISTING files_to_change
        into the IMPLEMENT prompt so the coder starts warm instead of re-Reading what
        CLASSIFY/PLAN already grounded. Bounded by per-file + total char caps; files
        that are missing, binary, or too large are listed as 'read on demand' instead.
        New files (new_files_needed) are never inlined — they don't exist yet.
        Default OFF — the full CLI toolset (A) is the primary speedup; warm-loading
        is an opt-in experiment. Enable with SDLC_IMPLEMENT_INLINE_FILES=true.
        Best-effort — never raises."""
        import os as _os
        if _os.getenv("SDLC_IMPLEMENT_INLINE_FILES", "false").strip().lower() not in ("1", "true", "yes"):
            return ""
        ws = getattr(self, "_run_workspace_path", "") or ""
        paths = [p for p in (plan.get("files_to_change") or [])
                 if isinstance(p, str) and p.strip()]
        if not ws or not paths:
            return ""
        _PER_FILE_MAX = 24_000    # chars — annotate & skip files larger than this
        _TOTAL_MAX    = 120_000   # chars — stop inlining once this budget is spent
        blocks, deferred, total = [], [], 0
        for rel in paths:
            full = _os.path.join(ws, rel.replace("\\", "/").lstrip("/"))
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
            except Exception:
                deferred.append(rel)       # missing/binary → coder Reads it itself
                continue
            if len(body) > _PER_FILE_MAX or total + len(body) > _TOTAL_MAX:
                deferred.append(rel)
                continue
            total += len(body)
            blocks.append(f"----- {rel} -----\n{body}")
        if not blocks and not deferred:
            return ""
        out = (
            "CURRENT CONTENTS OF THE FILES YOU WILL EDIT (already grounded by PLAN — use "
            "these instead of re-reading; Read is still available for anything not shown):\n\n"
            + "\n\n".join(blocks)
        )
        if deferred:
            out += ("\n\nNOT inlined (too large or new — Read these yourself as needed): "
                    + ", ".join(deferred))
        return out + "\n\n"

    def _build_fix_prompt(self, issues: list, plan: dict, notes: str = "") -> str:
        """The one bounded REVIEW fix-round prompt — address ONLY the reviewer's
        blocking issues; no scope expansion, no test-weakening.

        Carries the EXACT reviewer feedback: structured `blocking_issues` when the
        reviewer returned them, otherwise the free-text `notes` verbatim (e.g. when
        the verdict was prose or failed-closed to blocking with no structured list).
        The caller guarantees at least one of the two is non-empty."""
        import json as _json
        issues = issues or []
        notes = (notes or "").strip()
        if issues:
            _feedback = f"BLOCKING ISSUES:\n{_json.dumps(issues, indent=2, default=str)}"
            if notes:
                _feedback += f"\n\nReviewer notes:\n{notes}"
        else:
            # No structured issues — carry the reviewer's notes so the fix round
            # still gets actionable feedback rather than an empty list.
            _feedback = f"REVIEWER FEEDBACK:\n{notes}"
        # Delegate assembly to the shared pure builder so the fix round carries the
        # SAME verification-depth guard + STOP/termination contract as IMPLEMENT
        # (the old inline prompt hard-coded "Re-run the build and tests until GREEN"
        # with no terminal condition, so the coder ran to --max-turns).
        from agents.sdlc_implement_prompt import build_fix_round_prompt
        return build_fix_round_prompt(
            _feedback,
            solution_approach=(plan or {}).get("solution_approach", "") or "",
            skip_tests=self.skip_tests,
            drives_tests_green=self._implement_drives_tests_green(),
            workspace_root=self._run_workspace_path or "",
            governance_block=self._governance_awareness(),
            dep_block=self._dep_block_for_prompt(),
        )

    def _persist_implement_session(self, session_id: str) -> None:
        """Persist (or clear, with "") the IMPLEMENT CLI session id in run context so a
        manual resume-from-stage can `--resume` the SAME coding session and continue on
        the already-written files. Best-effort — budget/session bookkeeping must never
        crash a run."""
        try:
            from store.sdlc_store import patch_run_context
            patch_run_context(self.run_id, {"implement_session_id": session_id or ""})
        except Exception as _pe:
            logger.debug(f"[SM {self.run_id}] persist implement_session_id failed: {_pe}")

    def _phase_implement(self):
        """Merged IMPLEMENT + REVIEW pre-gate phase (Steps 5 + 6).

        One FRESH Sonnet CLI session (no --resume; the approved PLAN JSON is the
        handoff) writes code + tests and drives to green. The platform then captures
        the diff (compliance-on-diff inside), runs ONE platform Opus review over the
        diff (with one bounded CLI fix round), and finalizes the VERIFIED_DIFF. CLI
        runs PRE-GATE only — never _phase_applying."""
        from agents.sdlc_cli_engine import run_cli, CliEngineConfig
        from agents.sdlc_cli_budget import (
            record_cli_usage, remaining_budget, is_exhausted,
            resolve_implement_turns, _FIX_ROUND_TURNS_MAX,
        )
        from core.model_registry import cli_implement_model

        self._set_state("IMPLEMENT")

        # 0. MANUAL-RESUME signal: a prior IMPLEMENT that hit its turn cap persisted its
        #    CLI session id to context. If present, CONTINUE that session (--resume) and
        #    reuse the workspace IN PLACE (no wipe/reset) so the already-written files
        #    survive. Absent → a normal fresh first attempt.
        _resume_sid = ""
        try:
            _resume_sid = ((get_run(self.run_id) or {}).get("context") or {}).get("implement_session_id") or ""
        except Exception:
            _resume_sid = ""
        if _resume_sid:
            logger.info(
                "[SM] IMPLEMENT manual resume", run_id=self.run_id, stage="IMPLEMENT",
                resume_session_id=_resume_sid, resume_in_place=True,
            )

        # 1. Reuse the pinned checkout from PLAN (in place on a manual resume).
        try:
            self._ensure_run_workspace(self.repo, resume_in_place=bool(_resume_sid))
        except Exception as _ws:
            self._suspend("IMPLEMENT", f"workspace prep failed: {_ws}")
            return
        if not self._run_workspace_path:
            self._suspend("IMPLEMENT", "no run workspace for IMPLEMENT")
            return

        # 1b. Multi-repo: stage dep-repo checkouts inside the primary workspace
        #     (no-op for single-repo runs). Fires on both a fresh start and a
        #     manual resume (resume_in_place=True) since a resumed run on a
        #     fresh gateway instance has no staging yet.
        self._setup_multi_repo_workspace("IMPLEMENT")
        if ((get_run(self.run_id) or {}).get("state") or "") == "SUSPENDED":
            return

        plan = self.design or {}
        # DEBUG: log the plan to understand what IMPLEMENT received
        logger.info(
            "[SM] IMPLEMENT phase started", run_id=self.run_id,
            stage="IMPLEMENT",
            plan_keys=list(plan.keys()) if isinstance(plan, dict) else "not-a-dict",
            plan_type=type(plan).__name__,
            has_files_to_change=bool(plan.get("files_to_change")) if isinstance(plan, dict) else False,
        )

        # 2/3. Budget → holistic prompt → CLI session (code profile, Sonnet). The first
        #      call is fresh unless this is a manual resume (then --resume continues the
        #      stored session). Turn budget = PLAN's implement_max_turns × the tunable
        #      multiplier (sdlc_cli_budget), clamped + lowered by remaining HOD budget.
        if is_exhausted(self.run_id, "IMPLEMENT"):
            self._suspend("IMPLEMENT", "per-run budget exhausted")
            return
        _plan = plan or {}
        _file_count = len(_plan.get("files_to_change") or []) + len(_plan.get("new_files_needed") or [])
        _max_turns = resolve_implement_turns(
            _plan.get("implement_max_turns"),
            remaining_budget(self.run_id, "IMPLEMENT"),
            file_count=_file_count,
        )
        logger.info(
            "[SM] IMPLEMENT CLI selected", run_id=self.run_id,
            stage="IMPLEMENT", session=("resume" if _resume_sid else "fresh"),
            max_turns=_max_turns,
            plan_turn_estimate=(plan or {}).get("implement_max_turns"),
        )
        # PART 1 governance awareness — inlined into the IMPLEMENT prompt via
        # _build_implement_prompt (no CLI plugin-loading mechanism exists;
        # confirmed 2026-07-20 — see agents.sdlc_governance.engine.resolve_awareness).
        result = run_cli(
            config=CliEngineConfig.from_env(),
            workspace_root=self._run_workspace_path,
            prompt=self._build_implement_prompt(plan),
            profile="code",
            model=cli_implement_model(),
            max_turns=_max_turns,
            run_id=self.run_id,
            resume_session_id=_resume_sid,
        )
        try:
            record_cli_usage(self.run_id, result.usage or {}, result.total_cost_usd or 0.0)
        except Exception as _bue:
            logger.warning(f"[SM {self.run_id}] IMPLEMENT budget accounting failed: {_bue}")
        # Persist the session id so a later manual resume can continue this session.
        # Guard against empty: a FAILED resume (e.g. server binary lacks --resume →
        # non-zero exit → session_id="") must NOT clobber a previously-stored id, or a
        # second manual retry would lose the resume anchor and reset the workspace.
        if result.session_id:
            self._persist_implement_session(result.session_id)

        # 3b. ONE BOUNDED CONTINUE, THEN SALVAGE (2026-07-07 user decision; timeout
        #     added 2026-07-09).
        #     If the coder ran out of turns OR hit the wall-clock timeout mid-run,
        #     resume the SAME session ONE more time on the UNTOUCHED workspace — but with
        #     a SMALL (fix-round-sized) turn budget and a STOP-focused continue prompt,
        #     NOT another full IMPLEMENT budget (the old code re-spent the whole budget,
        #     doubling wall-clock on work that was often already done). A single automatic
        #     continuation only, so it can never loop. Timeout only reaches here with a
        #     session id when run_cli recovered one from partial stdout; without it the
        #     guard falls through to salvage (below). If --resume is unsupported on the
        #     server binary the continue call exits non-zero → suspended (safe under
        #     suspend-not-fail; the persisted session id lets a human retry).
        # A transient upstream 502/api_error mid-IMPLEMENT is handled EXACTLY like
        # error_max_turns: resume the SAME session on the UNTOUCHED workspace and continue
        # from where it left off (never clean/re-spawn fresh). Requires a session_id; if
        # the blip carried none, this falls through to the _collect_workspace_edits salvage
        # below so whatever was written is captured, never wiped.
        if (result.subtype in ("error_max_turns", "timeout", "stalled") or result.transient) \
                and result.session_id and not is_exhausted(self.run_id, "IMPLEMENT"):
            _cont_turns = resolve_implement_turns(
                (plan or {}).get("implement_max_turns"),
                remaining_budget(self.run_id, "IMPLEMENT"),
                ceiling=_FIX_ROUND_TURNS_MAX,
            )
            logger.info(
                "[SM] IMPLEMENT bounded auto-continue", run_id=self.run_id,
                stage="IMPLEMENT", trigger=result.subtype, transient=result.transient,
                prev_session_id=result.session_id, continue_turns=_cont_turns,
            )
            _cont = run_cli(
                config=CliEngineConfig.from_env(),
                workspace_root=self._run_workspace_path,
                prompt=self._build_continue_prompt(),
                profile="code",
                model=cli_implement_model(),
                max_turns=_cont_turns,
                run_id=self.run_id,
                resume_session_id=result.session_id,
            )
            try:
                record_cli_usage(self.run_id, _cont.usage or {}, _cont.total_cost_usd or 0.0)
            except Exception as _bue:
                logger.warning(f"[SM {self.run_id}] IMPLEMENT continue budget accounting failed: {_bue}")
            # Prefer the continuation's session id for any further manual resume; never
            # persist "" (would drop the resume anchor — see the guard above).
            _next_sid = _cont.session_id or result.session_id
            if _next_sid:
                self._persist_implement_session(_next_sid)
            result = _cont

        # 4. Resolve the CLI outcome into a captured diff.
        #    • suspended for a hard reason (auth/spawn/tool) → suspend.
        #    • otherwise (completed cleanly, OR still error_max_turns / timeout after the
        #      bounded continue) → CAPTURE the diff. A capped OR timed-out run frequently
        #      already wrote a complete, self-consistent diff; discarding it (the old
        #      behaviour — which also reset the workspace on the next attempt, throwing
        #      away the finished work) dead-ended the run. Capture it and, when non-empty,
        #      proceed to REVIEW — Opus over the diff remains the quality gate. Timeout is
        #      now treated like max-turns: keep the work, continue from where we left off.
        if result.status == "suspended" and result.subtype not in ("error_max_turns", "timeout", "stalled") \
                and not result.transient:
            self._suspend("IMPLEMENT", result.reason or "cli suspended")
            return

        _capped = result.status == "suspended"   # hit the cap / timed out / stalled / transient, even after the continue
        _cap_kind = {"timeout": "timeout", "stalled": "stall (124)"}.get(
            result.subtype, "transient upstream error" if result.transient else "max turns")
        edits = self._collect_workspace_edits()
        if edits is None:          # compliance-blocked → already suspended
            return
        # The coder declares any file it touched OUTSIDE the plan (path/kind/reason)
        # in a delimited JSON block at the end of its final message. Parse the CURRENT
        # `result` — after the bounded auto-continue above `result` is the continuation,
        # whose message carries the authoritative final declaration.
        added_files = _parse_unplanned_changes(result.result_text or "")
        # Multi-repo write-back: capture + compliance-scan each EDITABLE dep's diff and
        # publish `code_output_by_repo` for _create_sibling_mrs / the COMMITTING gate.
        # Runs AFTER the primary check so a primary compliance block short-circuits
        # first. {} (no-op) for single-repo runs.
        if self._collect_dep_edits() is None:   # dep compliance-blocked → suspended
            return
        if not edits:
            if _capped:
                logger.warning(
                    f"[SM] IMPLEMENT hit {_cap_kind} and produced NO workspace changes — "
                    "suspending (manual resume will continue the session)", run_id=self.run_id,
                    stage="IMPLEMENT", subtype=result.subtype, session_id=result.session_id,
                )
                self._suspend("IMPLEMENT", f"cli hit {_cap_kind} and produced no workspace changes")
            else:
                self._suspend("IMPLEMENT", "CLI produced no workspace changes")
            return
        # ── Deterministic scope classifier (RCA 9a2acc49 / N20-351) + declaration flow.
        #    A timed-out / max-turns IMPLEMENT can leave UNPLANNED files that `git add -A`
        #    sweeps into `edits` (the pom.xml reactor-build break). The classifier splits
        #    out-of-scope edits into:
        #      • excused    — the coder DECLARED them with a justification → allowed
        #        through to REVIEW, which judges each on merit.
        #      • undeclared — no justification → the RCA failure mode. Give the coder ONE
        #        bounded reasoning go-back (declare-or-revert); if it STILL leaves an
        #        undeclared out-of-scope file, hard-suspend for human review as before.
        _sv = self._classify_scope_violations(edits, plan, added_files)
        if _sv.get("enabled") and _sv.get("undeclared"):
            edits, added_files = self._reason_or_block_unplanned(
                edits, plan, added_files, _sv["undeclared"], result,
            )
            if edits is None:      # suspended inside (go-back failed / compliance / budget)
                return
            # Re-classify against the post-go-back edits + declarations so the excused
            # set handed to REVIEW reflects the corrected state.
            _sv = self._classify_scope_violations(edits, plan, added_files)
        # Justified out-of-scope additions to thread into REVIEW (empty when the
        # classifier is disabled or nothing was excused).
        _excused = set(_sv.get("excused") or [])
        _added_files = [a for a in (added_files or []) if (a.get("path") or "") in _excused]
        if _capped:
            logger.info(
                f"[SM] IMPLEMENT salvaged a complete diff after {_cap_kind} — proceeding to "
                "REVIEW instead of discarding it", run_id=self.run_id, stage="IMPLEMENT",
                changed_files=len(edits), subtype=result.subtype, session_id=result.session_id,
            )

        # 5. REVIEW (one Opus diff-review + one bounded CLI fix round). Edits to
        #    existing test files are NOT specially gated — the coder may legitimately
        #    update existing tests for a feature/signature change, and REVIEW judges
        #    the diff as a whole.
        if not self._run_review_and_maybe_fix(edits, plan, added_files=_added_files):
            return  # suspended inside

        # 6b. GOVERNANCE — RELOCATED (end-gate overhaul 2026-07-23; author-triggered
        #     2026-07-24). Governance NO LONGER runs here (mid-pipeline, over the
        #     pre-apply diff). It is now an AUTHOR-TRIGGERED END-GATE that fires AFTER
        #     COMMITTING + a normal (non-draft) MR — see _run_governance_endgate(),
        #     invoked by run_endgate_governance_job / the GOVERNANCE_SCAN resume path
        #     (NOT auto-called from _phase_commit anymore). REVIEW now flows
        #     straight to the design/solution-approval gate and the unchanged
        #     APPLYING → TEST_VERIFY → SLT_RUNNING → COMMITTING post-gate path.
        #     (_run_governance_review remains for legacy resume compatibility but is no
        #     longer called from the mid-pipeline path.)

        # IMPLEMENT succeeded → clear the stored resume session id so a later go-back to
        # IMPLEMENT starts a FRESH session rather than resuming a now-stale session
        # against a workspace that will be re-materialized/reset.
        self._persist_implement_session("")

        # 7. The CLI drove to green — record compile/tests state (honoring waivers)
        #    and finalize the VERIFIED_DIFF via the unchanged pre-gate terminus.
        if not self.compile_skipped:
            self._conf_build = 1.0
        # Only claim tests-green pre-gate when the coder was actually told to drive
        # the suite green (Option 1). Under the default Option 2, tests are authored
        # but NOT run here — leave _conf_tests at 0 so the VERIFIED_DIFF honestly
        # marks test execution as deferred to the post-gate TEST_VERIFY phase.
        if not self.skip_tests and self._implement_drives_tests_green():
            self._conf_tests = 1.0
        self._proceed_post_tests(tests_skipped=self.skip_tests)

    def _classify_scope_violations(self, edits: list, plan: dict, added_files: list) -> dict:
        """Deterministic out-of-scope CLASSIFIER over the CAPTURED edits (RCA 9a2acc49).

        Pure decision helper — it NEVER suspends or goes back; the caller
        (``_phase_implement``) owns the action. Splits every edit whose path is
        covered by NEITHER ``files_to_change`` NOR ``new_files_needed`` into:

        * ``excused``   — the coder DECLARED it (path present in ``added_files``, which
          the parser already filtered to entries carrying a real justification). These
          are allowed through to REVIEW, which judges each on merit.
        * ``undeclared`` — touched outside the plan with NO justification. These are
          the RCA-9a2acc49 failure mode (a salvaged/timed-out IMPLEMENT sweeping stray
          files in); the caller bounces them back for ONE correction round, then blocks.

        Returns ``{"enabled": bool, "excused": [paths], "undeclared": [paths]}``.
        ``enabled`` is False when the flag is off OR the plan declares no manifest —
        both are historical NO-OPs and must let the run proceed untouched. Never
        raises: any internal error returns ``enabled=False`` so a classifier bug
        cannot dead-end an otherwise-good run (the Opus reviewer remains the backstop).

        Path matching delegates to ``_path_covered`` (Windows-safe; tolerant of
        separator, leading-slash, ``./``, basename-suffix and repo-slug-prefix drift),
        the same helper the plan-coverage gate and the excuse-match use, so a declared
        path and its captured-edit form reconcile despite cosmetic drift.
        """
        _none = {"enabled": False, "excused": [], "undeclared": []}
        try:
            if os.getenv("SDLC_SCOPE_GUARD_ENABLED", "true").strip().lower() \
                    not in ("1", "true", "yes"):
                return _none
            plan = plan or {}

            def _paths(vals) -> list:
                out = []
                for p in (vals or []):
                    if isinstance(p, str) and p.strip():
                        out.append(p.strip())
                    elif isinstance(p, dict):
                        _pp = p.get("path") or p.get("file") or p.get("name") or ""
                        if isinstance(_pp, str) and _pp.strip():
                            out.append(_pp.strip())
                return out

            manifest = set(
                _paths(plan.get("files_to_change"))
                + _paths(plan.get("new_files_needed"))
            )
            if not manifest:
                logger.warning(
                    "[SM] scope guard SKIPPED — plan declares no manifest "
                    "(files_to_change + new_files_needed both empty)",
                    run_id=self.run_id, stage="IMPLEMENT",
                )
                return _none

            from agents.sdlc_pipeline._phases import _path_covered
            # A declared path excuses a violation only if it reconciles (same tolerant
            # matcher) with the captured edit path — a declaration for a file that
            # isn't actually in the diff excuses nothing.
            declared = {(a.get("path") or "").strip()
                        for a in (added_files or []) if (a.get("path") or "").strip()}

            excused, undeclared = [], []
            for e in (edits or []):
                p = (e.get("path") or "")
                if not p or _path_covered(p, manifest):
                    continue
                if declared and _path_covered(p, declared):
                    excused.append(p)
                else:
                    undeclared.append(p)
            return {"enabled": True, "excused": excused, "undeclared": undeclared}
        except Exception as _sge:
            logger.warning(
                f"[SM {self.run_id}] scope classifier errored — failing OPEN "
                f"(review remains backstop): {_sge}"
            )
            return _none

    def _reason_or_block_unplanned(self, edits: list, plan: dict, added_files: list,
                                   undeclared: list, prev_result):
        """ONE bounded reasoning go-back for UNDECLARED out-of-scope edits.

        Rather than hard-suspending the moment the classifier finds an unplanned file
        with no justification, resume the SAME coder session ONCE with corrective
        feedback: for each flagged path, either DECLARE it (emit the justification
        block) or REVERT it. Then re-collect the workspace edits + the fresh
        declaration block and let the caller re-classify.

        Returns ``(edits, added_files)`` to continue with (possibly changed by the
        go-back). Returns ``(None, None)`` when it SUSPENDED — because the go-back is
        impossible (no session / budget exhausted / --resume unsupported), a
        compliance block hit during re-collection, or the coder STILL left an
        undeclared out-of-scope file after its one correction round (the original
        hard-block behaviour, now reached only after the reasoning attempt). Never
        loops: exactly one go-back per IMPLEMENT phase."""
        from agents.sdlc_cli_engine import run_cli, CliEngineConfig
        from agents.sdlc_cli_budget import (
            record_cli_usage, remaining_budget, is_exhausted,
            resolve_implement_turns, _FIX_ROUND_TURNS_MAX,
        )
        from core.model_registry import cli_implement_model

        def _hard_block(reason_paths: list) -> tuple:
            logger.error(
                "[SM] scope guard BLOCK — IMPLEMENT left undeclared out-of-scope files",
                run_id=self.run_id, stage="IMPLEMENT", out_of_scope=reason_paths,
            )
            self._add_event(
                "IMPLEMENT", "scope-guard",
                f"out-of-scope edits blocked: {', '.join(reason_paths)}",
                {"out_of_scope": reason_paths},
            )
            self._suspend(
                "IMPLEMENT",
                "out-of-scope edits (not in plan and not justified after one correction "
                "round): " + ", ".join(reason_paths),
            )
            return (None, None)

        _sid = getattr(prev_result, "session_id", "") or ""
        if not _sid or is_exhausted(self.run_id, "IMPLEMENT"):
            # No session to resume or no budget left — cannot reason; fall back to the
            # deterministic hard block (the pre-existing RCA-9a2acc49 behaviour).
            return _hard_block(undeclared)

        logger.warning(
            "[SM] IMPLEMENT scope reasoning go-back — undeclared out-of-scope files",
            run_id=self.run_id, stage="IMPLEMENT", undeclared=undeclared,
            prev_session_id=_sid,
        )
        _turns = resolve_implement_turns(
            (plan or {}).get("implement_max_turns"),
            remaining_budget(self.run_id, "IMPLEMENT"),
            ceiling=_FIX_ROUND_TURNS_MAX,
        )
        result = run_cli(
            config=CliEngineConfig.from_env(),
            workspace_root=self._run_workspace_path,
            prompt=self._build_scope_reason_prompt(undeclared),
            profile="code",
            model=cli_implement_model(),
            max_turns=_turns,
            run_id=self.run_id,
            resume_session_id=_sid,
        )
        try:
            record_cli_usage(self.run_id, result.usage or {}, result.total_cost_usd or 0.0)
        except Exception:
            pass
        if result.session_id:
            self._persist_implement_session(result.session_id)
        if result.status == "suspended" and result.subtype not in ("error_max_turns", "timeout", "stalled") \
                and not result.transient:
            self._suspend("IMPLEMENT", result.reason or "cli suspended")
            return (None, None)

        edits2 = self._collect_workspace_edits()
        if edits2 is None:      # compliance-blocked → already suspended
            return (None, None)
        if self._collect_dep_edits() is None:   # dep compliance-blocked → suspended
            return (None, None)
        added_files2 = _parse_unplanned_changes(result.result_text or "")
        # Re-classify: anything STILL undeclared after the correction round is a real
        # scope violation → the deterministic hard block (human review).
        _sv2 = self._classify_scope_violations(edits2, plan, added_files2)
        if _sv2.get("enabled") and _sv2.get("undeclared"):
            return _hard_block(_sv2["undeclared"])
        return (edits2, added_files2)

    def _build_scope_reason_prompt(self, undeclared: list) -> str:
        """Corrective prompt for the one scope reasoning go-back: name each undeclared
        out-of-scope file and demand the coder either JUSTIFY it (emit the declaration
        block) or REVERT it. Reuses the shared continue builder's discipline via the
        unplanned-change clause; kept inline (not in the pure prompt module) because it
        is a run-scoped corrective message, mirroring _build_continue_prompt's scope."""
        from agents.sdlc_implement_prompt import (
            workspace_boundary_clause, unplanned_changes_clause, implement_stop_clause,
        )
        _paths = "\n".join(f"  - {p}" for p in (undeclared or []))
        body = (
            "SCOPE CORRECTION — one round only.\n"
            "You changed the following file(s) that are NOT in the plan's files_to_change / "
            "new_files_needed, and you did NOT declare them:\n"
            f"{_paths}\n\n"
            "For EACH file above you MUST do exactly one of:\n"
            "  (a) KEEP it and DECLARE it — emit the unplanned-change declaration block below "
            "with a concrete reason why implementing the plan REQUIRES this file, or\n"
            "  (b) REVERT it fully to its original state so it no longer appears in the diff.\n"
            "Do not leave any listed file both changed AND undeclared — that will block the run. "
            "Do not touch any other file."
        )
        return (
            workspace_boundary_clause(self._run_workspace_path or "").lstrip("\n")
            + body
            + unplanned_changes_clause()
            + implement_stop_clause(done_condition="every listed file is declared or reverted")
        )

    def _run_review_and_maybe_fix(self, edits: list, plan: dict, *, added_files: list | None = None) -> bool:
        """Step 6 (state-machine half): ONE platform Opus review over the diff, then
        at most ONE CLI fix round, then re-review once. Returns True if approved,
        False if it suspended (compliance/budget/unresolved)."""
        from agents.sdlc_pipeline import _run_review_phase
        from agents.sdlc_cli_engine import run_cli, CliEngineConfig
        from agents.sdlc_cli_budget import (
            record_cli_usage, remaining_budget, is_exhausted,
            resolve_implement_turns, fix_round_ceiling,
        )
        from core.model_registry import cli_implement_model

        self._set_state("REVIEW")

        # WS-3 (gate-reorder, 2026-07-02) — SDLC_SIMPLE_SKIP_REVIEW: for
        # complexity=="simple" runs, operators may skip the Opus diff-review gate
        # entirely (auto-approve) once the loop is trusted. Default false — keeps
        # REVIEW+HITL for every run (Decision 3A). Reads complexity off the stored
        # CLASSIFYING artifact rather than threading it through the constructor.
        _cls = self._get_artifact("CLASSIFYING") or {}
        _complexity = str(_cls.get("complexity") or "").strip().lower()
        if _complexity == "simple" and os.getenv("SDLC_SIMPLE_SKIP_REVIEW", "false").lower() in ("1", "true", "yes"):
            logger.info(
                "[SM] REVIEW skipped — SDLC_SIMPLE_SKIP_REVIEW on simple complexity",
                run_id=self.run_id, stage="REVIEW", complexity=_complexity,
            )
            return True

        # INCREMENTAL REVIEW ANCHOR (2026-08-16): the FIRST review of a run defines the
        # canonical open-issue set. It is persisted (REVIEW_ANCHOR) and reused as
        # prior_issues on EVERY subsequent REVIEW entry — including after a suspend +
        # human resume, which re-runs the whole IMPLEMENT+REVIEW phase. Without this,
        # each re-entry ran a FRESH review that could raise a DIFFERENT set of findings
        # ("different issues at different times"). Anchoring makes the review monotonic:
        # every later pass may only VERIFY the anchored issues (see _scope_clause in
        # _run_review_phase), so the open set can only shrink and the loop converges.
        _anchor = (self._get_artifact("REVIEW_ANCHOR") or {}).get("blocking_issues") or []

        # _build_review_diff == _build_unified_diff for a single-repo run; on a
        # multi-repo run it appends the editable deps' hunks so the gate actually
        # covers the code the sibling MRs will push to the OTHER customer repo.
        verdict = _run_review_phase(
            self.run_id, self._build_review_diff(edits), plan,
            prior_issues=(_anchor or None), added_files=added_files,
        )
        if verdict.get("approved"):
            self._clear_review_anchor()
            return True

        issues = verdict.get("blocking_issues") or []
        # Persist the anchor exactly ONCE — the first time a review blocks with real
        # findings. Later re-entries load this set above; they never overwrite it, so
        # the canonical open-issue set stays fixed for the life of the run.
        if not _anchor and issues:
            self._put_artifact(
                "REVIEW_ANCHOR", {"blocking_issues": issues},
                reason="first REVIEW blocking-issue anchor (incremental re-review)",
            )
            _anchor = issues
        _notes = (verdict.get("notes") or "").strip()
        # If REVIEW blocked but produced NO actionable feedback at all (no structured
        # issues AND no notes — e.g. an unparseable verdict that failed closed), a
        # fix-round CLI would run blind and change nothing. Suspend for human review
        # instead of burning a no-op CLI round.
        if not issues and not _notes:
            self._suspend("REVIEW", "review blocked with no actionable feedback (unparseable verdict)")
            return False
        logger.warning(
            "[SM] REVIEW unresolved — one CLI fix round", run_id=self.run_id,
            stage="REVIEW", top_issue=(issues[0] if issues else None),
            has_notes=bool(_notes),
        )

        # ── ONE bounded CLI fix round (steered by the EXACT reviewer feedback).
        if is_exhausted(self.run_id, "REVIEW"):
            self._suspend("REVIEW", "per-run budget exhausted")
            return False
        # A fix-round is small — bound it below the full-IMPLEMENT estimate, but give
        # it the SAME ~1.5x turn headroom IMPLEMENT gets (fix_round_ceiling) so a
        # multi-issue fix isn't starved. The STOP contract in the fix prompt makes it
        # terminate as soon as the flagged issues are green, so this is a safety cap.
        _max_turns = resolve_implement_turns(
            (plan or {}).get("implement_max_turns"),
            remaining_budget(self.run_id, "REVIEW"),
            ceiling=fix_round_ceiling(),
        )
        result = run_cli(
            config=CliEngineConfig.from_env(),
            workspace_root=self._run_workspace_path,
            prompt=self._build_fix_prompt(issues, plan, notes=_notes),
            profile="code",
            model=cli_implement_model(),
            max_turns=_max_turns,
            run_id=self.run_id,
        )
        try:
            record_cli_usage(self.run_id, result.usage or {}, result.total_cost_usd or 0.0)
        except Exception:
            pass
        if result.status == "suspended":
            self._suspend("REVIEW", result.reason or "cli suspended")
            return False

        edits2 = self._collect_workspace_edits(stage="REVIEW")
        if edits2 is None:      # compliance-blocked → suspended
            return False
        # Re-capture the dep diffs after the fix round so code_output_by_repo reflects
        # the post-fix dep state (the fix round may have edited an editable dep too,
        # or REVERTED one — an empty result now clears both the in-memory copy and
        # the persisted key). stage="REVIEW": a dep compliance block / git failure
        # here belongs to the REVIEW fix round, so the resume restarts REVIEW rather
        # than re-running the entire IMPLEMENT phase.
        if self._collect_dep_edits(stage="REVIEW") is None:   # dep compliance-blocked → suspended
            return False
        if not edits2:
            self._suspend("REVIEW", "fix round produced no changes")
            return False

        # Follow-up review is SCOPED to the anchored open-issue set (monotonic close):
        # it may only confirm/deny those, never introduce new findings. Prefer the
        # persisted anchor over this call's `issues` so the scope is identical to every
        # cross-resume re-entry.
        verdict2 = _run_review_phase(
            self.run_id, self._build_review_diff(edits2), plan,
            prior_issues=(_anchor or issues), added_files=added_files,
        )
        _remaining = len(verdict2.get("blocking_issues") or [])
        logger.info("[SM] REVIEW follow-up verdict", run_id=self.run_id,
                    prior=len(issues), remaining=_remaining,
                    approved=bool(verdict2.get("approved")))
        if verdict2.get("approved"):
            self._clear_review_anchor()
            return True
        _top = (verdict2.get("blocking_issues") or [])
        _top_issue = (_top[0].get("issue") if (_top and isinstance(_top[0], dict)) else "see notes")
        # Persist the (compiled, real) diff as a VERIFIED_DIFF before suspending so a
        # human with waive authority can accept it: waive-of-REVIEW routes to the
        # post-gate APPLYING phase, which reads this artifact. Without it, a waived
        # REVIEW would immediately re-suspend ("no verified diff to apply"). This
        # stores the artifact ONLY — it does NOT transition to the approval gate;
        # the run stays SUSPENDED at REVIEW until a human waives. Best-effort.
        try:
            self._finalize_pregate()
        except Exception as _fe:
            logger.warning(
                f"[SM {self.run_id}] REVIEW-suspend VERIFIED_DIFF persist failed "
                f"(waive-of-REVIEW will require re-implement): {_fe}"
            )
        self._suspend("REVIEW", f"opus review unresolved: {_top_issue}")
        return False

    # ══════════════════════════════════════════════════════════════════════
    # GOVERNANCE_REVIEW (2026-07-17) — separate EA/IS/DPDP gate + own fixer loop
    # ══════════════════════════════════════════════════════════════════════

    def _governance_awareness(self) -> str:
        """PART 1 awareness pointer_block (governance skills' SKILL.md content
        inlined), cached per run. Fail-safe → "" so IMPLEMENT prompts are unchanged
        whenever no bundle resolves or awareness is disabled. There is no CLI
        plugin-loading mechanism (confirmed 2026-07-20 — see
        agents.sdlc_governance.engine.resolve_awareness), so this is prompt text
        only; it is no longer threaded into run_cli() as plugins/plugin_marketplace."""
        if getattr(self, "_gov_awareness_cache", None) is not None:
            return self._gov_awareness_cache
        res = ""
        try:
            from agents.sdlc_governance import engine as gov_engine
            # IMPLEMENT-phase awareness (PLAN resolves its own "plan"-phase set in the
            # pipeline; the governance review resolves its "review"-phase set).
            # Pass the run workspace so each skill's full folder is staged READ-ONLY
            # inside it and the CLI reads it itself (no truncated inline). Falls back
            # to inlining when the workspace isn't materialized yet.
            _ws = self._run_workspace_path if (self._run_workspace_path
                                               and os.path.isdir(self._run_workspace_path)) else ""
            res = gov_engine.resolve_awareness(getattr(self, "governance_subset", None),
                                               phase="implement", workspace_root=_ws)
        except Exception as e:
            logger.warning(f"[SM {self.run_id}] governance awareness resolve failed (non-fatal): {e}")
            res = ""
        self._gov_awareness_cache = res
        return res

    def _persist_governance_report(self, report) -> None:
        """Persist the GOVERNANCE_REPORT artifact (UI + read endpoint). Non-fatal."""
        if not report:
            return
        try:
            self._put_artifact("GOVERNANCE_REPORT", report, reason="governance review report")
            self._add_event(
                "GOVERNANCE_REVIEW", "governance",
                f"governance report: {report.get('overall_verdict', '?')}",
                {"overall_verdict": report.get("overall_verdict"),
                 "skills": [s.get("skill") for s in (report.get("skills") or [])]},
            )
        except Exception as e:
            logger.warning(f"[SM {self.run_id}] persist GOVERNANCE_REPORT failed: {e}")

    def _write_governance_report_file(self, report) -> str:
        """Write the report_md to a STANDALONE file OUTSIDE the tracked workspace
        (so it never lands in the VERIFIED_DIFF) and return its path. Best-effort."""
        if not report or not report.get("report_md"):
            return ""
        try:
            import os as _os
            from core.config import BUILDER_WORKSPACE_ROOT as _ROOT
            _dir = _os.path.join(_ROOT, "gov_reports", self.run_id or "norun")
            _os.makedirs(_dir, exist_ok=True)
            _path = _os.path.join(_dir, "governance_report.md")
            with open(_path, "w", encoding="utf-8") as _fh:
                _fh.write(report["report_md"])
            logger.info("[SDLC-GOV] standalone report file written", run_id=self.run_id, path=_path)
            return _path
        except Exception as e:
            logger.debug(f"[SM {self.run_id}] governance report file write failed: {e}")
            return ""

    def _run_governance_review(self, edits: list, plan: dict) -> bool:
        """GOVERNANCE_REVIEW gate: scan diff → persist findings → seed per-domain
        approvals → suspend to AWAITING_GOVERNANCE_APPROVAL. Returns True (proceed)
        when no open/blocking findings. Returns False when suspended for approvals.

        The resume path (all domains approved) re-enters via
        ``_resume_after_governance_approval``, which runs the fixer for exactly the
        open findings, re-collects edits, and continues to VERIFIED_DIFF/APPLYING.

        Whether this gate runs at all is decided by the caller
        (run_governance_review / ctx / SDLC_GOVERNANCE_ALWAYS) so governance-disabled
        runs flow exactly as before. Never raises — an unexpected error fails CLOSED
        (suspend)."""
        from agents.sdlc_pipeline import _run_governance_review_phase
        from agents.sdlc_governance import engine as gov_engine

        db = None
        report = None
        try:
            self._set_state("GOVERNANCE_REVIEW")
            subset = getattr(self, "governance_subset", None)
            repo = self.gitlab_repo or self.repo

            try:
                from db.database import SessionLocal
                db = SessionLocal()
            except Exception:
                db = None

            product_id = gov_engine.resolve_product_id(db, repo)
            diff_text = self._build_unified_diff(edits)

            res = _run_governance_review_phase(
                self.run_id, self._run_workspace_path, diff_text,
                [e["path"] for e in (edits or [])], product_id, repo, subset, db,
            )
            report = res.get("report")
            open_findings = res.get("open_findings") or []

            if res.get("skipped"):
                # No bundle/skills resolved — nothing to review; proceed.
                return True

            if res.get("scan_error"):
                # The CLI session could not complete (crash/timeout/max_turns) OR the
                # diff was too large for an automated pass (diff_too_large). Either way
                # this is not a real finding — SUSPEND so a human retries / reviews,
                # rather than seeding a phantom approval gate.
                _detail = res.get("scan_error_detail") or ""
                logger.warning(
                    "[SDLC-GOV] governance scan unavailable — suspending for retry",
                    run_id=self.run_id, detail=_detail,
                    diff_too_large=bool(res.get("diff_too_large")),
                )
                self._set_state("SUSPENDED")
                _msg = (
                    f"Governance review not run — {_detail}"
                    if res.get("diff_too_large")
                    else "Governance scan could not complete (CLI error/timeout). "
                         "Increase SDLC_GOVERNANCE_SCAN_TURNS and retry."
                )
                self._suspend("GOVERNANCE_REVIEW", _msg)
                return False

            # Persist findings (open + suppressed), tagged by governance domain.
            from store.sdlc_governance_findings import persist_findings, domain_open_counts
            domain_by_skill = {}
            try:
                _, skills = gov_engine.select_skills(subset, phase="review")
                domain_by_skill = {s.slug: s.domain for s in (skills or []) if s.domain}
            except Exception:
                pass
            persist_findings(
                self.run_id, open_findings + (res.get("suppressed") or []), domain_by_skill,
            )

            # Persist governance report artifact + standalone file.
            self._persist_governance_report(report)
            self._write_governance_report_file(report)

            if not res.get("blocking") or not open_findings:
                return True

            # Seed one pending approval row per domain with ≥1 open finding.
            from store.sdlc_governance_approvers import seed_domain_approvals
            counts = domain_open_counts(self.run_id)
            seed_domain_approvals(self.run_id, counts)

            # Persist a waivable VERIFIED_DIFF so the existing waive→APPLYING escape
            # hatch still works from the new AWAITING_GOVERNANCE_APPROVAL gate.
            try:
                self._finalize_pregate()
            except Exception as _fe:
                logger.warning(
                    f"[SM {self.run_id}] GOVERNANCE_REVIEW-suspend VERIFIED_DIFF persist "
                    f"failed (waive will require re-run): {_fe}"
                )

            logger.info(
                "[SDLC-GOV] in-pipeline gate suspend for approvals", run_id=self.run_id,
                domains=list(counts.keys()), open=len(open_findings),
            )

            # Suspend to the per-domain HITL approval gate.
            self._set_state("AWAITING_GOVERNANCE_APPROVAL")
            # Persist a governance-window HITL deadline so the watchdog can expire
            # a governance gate that is never actioned (7d default). Without this
            # the gate had no deadline and could sit forever.
            try:
                from store.sdlc_store import patch_run_context
                from core.config import sdlc_gate_deadline
                _gov_deadline = sdlc_gate_deadline("governance")
                patch_run_context(self.run_id, {"hitl_deadline": _gov_deadline, "gate_kind": "governance"})
                logger.info("[SDLC-GOV] gate entered", run_id=self.run_id,
                            gate_kind="governance", hitl_deadline=_gov_deadline)
            except Exception as _gd:
                logger.warning(f"[SM {self.run_id}] governance hitl_deadline persist failed: {_gd}")
            self._suspend(
                "GOVERNANCE_REVIEW",
                f"Awaiting per-domain approval: {', '.join(counts.keys())}",
            )
            return False

        except Exception as e:
            logger.error("[SDLC-GOV] _run_governance_review unexpected error — suspending",
                         run_id=self.run_id, error=str(e))
            try:
                self._suspend("GOVERNANCE_REVIEW", f"Governance review error: {e}")
            except Exception:
                pass
            return False
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    # ══════════════════════════════════════════════════════════════════════
    # GOVERNANCE END-GATE (2026-07-23) — post-COMMITTING gate before merge
    # ══════════════════════════════════════════════════════════════════════

    def _governance_endgate_clear(self, branch, pr_number, pr_url) -> None:
        """No blocking governance findings → flip the draft MR to mergeable and
        advance to the existing AWAITING_PR_APPROVAL gate (governance precedes PR
        approval). Un-draft is best-effort; never raises."""
        if pr_number:
            try:
                from core.config import SCM_PROVIDER as _SCM
                if _SCM == "github":
                    from tools.github_tools import github_set_pr_draft as _set_draft
                else:
                    from tools.gitlab_tools import gitlab_set_mr_draft as _set_draft
                _set_draft(self.gitlab_repo, pr_number, draft=False)
                logger.info("[SDLC-GOV] MR unblocked — no blocking governance findings",
                            run_id=self.run_id, mr_iid=pr_number)
            except Exception as _ue:
                logger.warning("[SDLC-GOV] MR undraft failed (non-fatal)",
                               run_id=self.run_id, mr_iid=pr_number, error=str(_ue))
        update_run_state(self.run_id, "AWAITING_PR_APPROVAL",
                         branch=branch, pr_number=pr_number, pr_url=pr_url)
        logger.info("[SDLC-GOV] end-gate cleared → AWAITING_PR_APPROVAL", run_id=self.run_id)

    def _run_governance_endgate(self, *, branch: str, pr_number, pr_url: str,
                                commit_sha: str) -> None:
        """End-gate overhaul (2026-07-23): governance runs AFTER COMMITTING + a DRAFT
        MR, as the final gate before merge.

        Runs the first governance scan over the committed diff, dual-writes findings
        (legacy table) + an immutable scan snapshot (+ observations), then either:
          (a) nothing blocking → un-draft the MR and advance to AWAITING_PR_APPROVAL, or
          (b) blocking findings → seed per-domain approvals and SUSPEND to
              AWAITING_GOVERNANCE_APPROVAL (the author triage / team review gate).

        The author remediation loop (auto re-scan on request-fix) + snapshot-scoped
        carry-forward are layered on by B2.2/B2.5; the resume → un-draft → PR-approval
        wiring is B2.6. Never raises — fails CLOSED (suspend). Uses the B2.1 shared
        controller primitive run_governance_scan_snapshot (scan + dual-write snapshot),
        also used by the author remediation re-scan loop."""
        from agents.sdlc_pipeline import run_governance_scan_snapshot
        from agents.sdlc_governance import engine as gov_engine

        db = None
        try:
            self._set_state("GOVERNANCE_SCAN")
            subset = getattr(self, "governance_subset", None)
            repo = self.gitlab_repo or self.repo

            # Resolve the end-gate diff BASE (scan-unify 2026-07-28). COMMITTING already
            # committed+pushed the change, so on this freshly-cloned working branch it sits
            # at HEAD — diffing vs the pinned base_sha (empty when SDLC_REUSE_RUN_WORKSPACE
            # is off → "HEAD") would yield an EMPTY diff and a false-green gate. Diff vs the
            # merge-base against the MR base branch instead so the full committed MR diff is
            # seen. `git clone --branch <working>` (no --single-branch) fetches all remote
            # refs, so origin/<base_branch> is present — same assumption _gov_git_diff relies on.
            base_branch = self.base_branch or "main"
            base_sha = (self._git(["merge-base", f"origin/{base_branch}", "HEAD"]).strip()
                        or self._git(["rev-parse", f"origin/{base_branch}"]).strip()
                        or base_branch)

            edits = self._collect_workspace_edits(stage="GOVERNANCE_SCAN", base_override=base_sha)
            if edits is None:      # compliance-blocked → already suspended
                return
            edits = edits or []

            logger.info(
                "[SDLC-GOV] end-gate diff base resolved", run_id=self.run_id,
                base_branch=base_branch, base_sha=base_sha, n_edits=len(edits),
            )

            # EMPTY-DIFF GUARD (2026-07-30): the end-gate scans a FRESH CLONE of
            # origin/<working_branch>. If that branch has no changes over its base
            # (merge-base == HEAD) the collected diff is empty — almost always
            # because the run's commits never reached origin (unpushed local changes)
            # or a base/branch misresolution. Scanning an empty diff writes empty
            # per-skill .patch files and would FALSE-GREEN the gate. Fail CLOSED:
            # SUSPEND with an actionable message rather than pretend it passed.
            if not edits:
                logger.error(
                    "[SDLC-GOV] end-gate diff is EMPTY — suspending "
                    "(unpushed local changes or base/branch misresolution)",
                    run_id=self.run_id, base_branch=base_branch, base_sha=base_sha,
                    working_branch=branch,
                )
                self._set_state("SUSPENDED")
                self._suspend(
                    "GOVERNANCE_SCAN",
                    f"Governance scan found no changes on '{branch}' over '{base_branch}'. "
                    "The branch has no diff versus its base — this usually means the "
                    "run's commits were not pushed to origin. Ensure the changes are "
                    f"committed and pushed to origin/{branch}, then retry governance.",
                )
                return

            try:
                from db.database import SessionLocal
                db = SessionLocal()
            except Exception:
                db = None

            product_id = gov_engine.resolve_product_id(db, repo)
            diff_text = self._build_unified_diff(edits)

            # Unified scan core: per-skill parallel scan + dual-write snapshot in one call
            # (SAME primitive the standalone pipeline + worker use). base_sha labels the
            # per-skill prompt's `base_sha...HEAD` reference to the merge-base computed above.
            res = run_governance_scan_snapshot(
                self.run_id, workspace=self._run_workspace_path, diff_text=diff_text,
                changed_files=[e["path"] for e in edits], product_id=product_id,
                repo=repo, base_sha=base_sha, subset=subset, db=db, trigger="initial",
                created_by=self.user_email,
            )
            report = res.get("report")
            open_findings = res.get("open_findings") or []

            if res.get("skipped"):
                # No bundle/skills resolved — nothing to gate → un-draft + PR approval.
                self._governance_endgate_clear(branch, pr_number, pr_url)
                return

            if res.get("scan_error"):
                # CLI session could not complete OR the diff was too large for an
                # automated pass — availability error, not a real finding. SUSPEND so
                # a human retries / reviews (the draft MR stays un-mergeable — safe posture).
                _detail = res.get("scan_error_detail") or ""
                logger.warning("[SDLC-GOV] end-gate scan unavailable — suspending for retry",
                               run_id=self.run_id, detail=_detail,
                               diff_too_large=bool(res.get("diff_too_large")))
                self._set_state("SUSPENDED")
                _msg = (
                    f"Governance review not run — {_detail}"
                    if res.get("diff_too_large")
                    else "Governance scan could not complete (CLI error/timeout). "
                         "Increase SDLC_GOVERNANCE_SCAN_TURNS and retry."
                )
                self._suspend("GOVERNANCE_SCAN", _msg)
                return

            from store.sdlc_governance_findings import domain_open_counts

            # Persist governance report artifact + standalone file (best-effort).
            try:
                self._persist_governance_report(report)
                self._write_governance_report_file(report)
            except Exception:
                pass

            # FAIL-CLOSED guard (review finding #1): _run_governance_review_phase's
            # outer except returns blocking=True with open_findings=[] on an
            # UNEXPECTED internal error (DB blip, render/parse failure) — NOT the
            # scan_error path. That is an availability error, not "nothing to
            # review". Do NOT clear the gate (which would un-draft the MR and let a
            # change merge with no governance sign-off) — SUSPEND for retry and keep
            # the MR drafted (the safe posture).
            if res.get("blocking") and not open_findings:
                logger.error(
                    "[SDLC-GOV] end-gate scan errored (fail-closed) — suspending for retry",
                    run_id=self.run_id, stage="GOVERNANCE_SCAN",
                )
                self._set_state("SUSPENDED")
                self._suspend(
                    "GOVERNANCE_SCAN",
                    "Governance scan errored unexpectedly — the draft MR stays blocked. "
                    "Retry the governance stage.",
                )
                return

            # Per-domain team sign-off gate (2026-07-30): EVERY scanned domain now
            # requires explicit team acknowledgement — including a CLEAN PASS (zero
            # findings). Seed a 'pending' row for each scanned domain (open_count 0 for
            # clean ones) and suspend to AWAITING_GOVERNANCE_APPROVAL. Only when NO
            # domain was classified at all (nothing to acknowledge) do we fall back to
            # the old auto-clear so the run can never stall with an empty gate.
            from store.sdlc_governance_approvers import seed_domain_approvals
            counts = domain_open_counts(self.run_id)
            scanned_domains = {
                (d or "").strip().upper()
                for d in (res.get("domain_by_skill") or {}).values()
                if (d or "").strip()
            }
            all_domains = scanned_domains | set(counts.keys())

            if not res.get("blocking") and not all_domains:
                # Nothing blocking AND no domain to acknowledge → un-draft + PR approval.
                self._governance_endgate_clear(branch, pr_number, pr_url)
                return

            seed_domain_approvals(self.run_id, counts, all_domains=all_domains)

            logger.info(
                "[SDLC-GOV] Governance end-gate entered after commit — awaiting approval",
                run_id=self.run_id, stage="GOVERNANCE_SCAN", snapshot_seq=1,
                domains=sorted(all_domains), open=len(open_findings),
                clean_pass=not res.get("blocking"), mr_iid=pr_number,
            )

            self._set_state("AWAITING_GOVERNANCE_APPROVAL")
            # Governance-window HITL deadline (7d default) so the watchdog can expire
            # an un-actioned end-gate.
            try:
                from store.sdlc_store import patch_run_context
                from core.config import sdlc_gate_deadline
                _gov_deadline = sdlc_gate_deadline("governance")
                patch_run_context(self.run_id, {"hitl_deadline": _gov_deadline, "gate_kind": "governance"})
                logger.info("[SDLC-GOV] end-gate entered", run_id=self.run_id,
                            gate_kind="governance", hitl_deadline=_gov_deadline)
            except Exception as _gd:
                logger.warning(f"[SM {self.run_id}] governance end-gate hitl_deadline persist failed: {_gd}")
            self._suspend(
                "GOVERNANCE_SCAN",
                f"Awaiting per-domain governance approval: {', '.join(sorted(all_domains))}",
            )
            return

        except Exception as e:
            logger.error("[SDLC-GOV] _run_governance_endgate unexpected error — suspending",
                         run_id=self.run_id, error=str(e), stage="GOVERNANCE_SCAN")
            try:
                self._suspend("GOVERNANCE_SCAN", f"Governance end-gate error: {e}")
            except Exception:
                pass
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def _resume_after_governance_approval(self, plan: dict) -> bool:
        """LEGACY (pre-2026-07-23 mid-pipeline gate). No longer called: under the
        governance END-GATE, approval-resume simply un-drafts the already-committed
        MR and advances to AWAITING_PR_APPROVAL (see
        agents.sdlc_pipeline.resume_in_pipeline_governance_approval, B2.6) — there is
        nothing to fix/apply post-approval because end-gate remediation happens in the
        author loop (run_governance_author_fix) BEFORE approval. Retained for
        reference / legacy standalone compatibility; do not wire into the end-gate.

        Called when all governance domains are approved. Loads the open findings
        (non-false-positive), runs the fixer for exactly those, re-collects edits,
        marks them fixed, then returns True to continue to VERIFIED_DIFF/APPLYING.

        Fail-closed: re-verifies ``all_finding_domains_approved`` before fixing.
        Returns False if not all approved, if the fixer suspended, or if the fix was
        compliance-blocked (already suspended in that case)."""
        from store.sdlc_governance_approvers import all_finding_domains_approved
        from store.sdlc_governance_findings import open_findings as load_open_findings

        # Re-verify (fail-closed guard) — never fix on an incompletely-approved run.
        if not all_finding_domains_approved(self.run_id):
            logger.warning("[SDLC-GOV] _resume_after_governance_approval: not all approved — no-op",
                           run_id=self.run_id)
            return False

        findings_to_fix = load_open_findings(self.run_id)
        if not findings_to_fix:
            return True  # nothing to fix → proceed

        from agents.sdlc_governance import engine as gov_engine
        from agents.sdlc_cli_engine import run_cli, CliEngineConfig
        from agents.sdlc_cli_budget import record_cli_usage
        from core.model_registry import cli_implement_model

        self._set_state("GOVERNANCE_REVIEW")
        fix_res = run_cli(
            config=CliEngineConfig.from_env(),
            workspace_root=self._run_workspace_path,
            prompt=gov_engine.build_fix_prompt(findings_to_fix, self._run_workspace_path),
            profile="code",
            model=cli_implement_model(),
            max_turns=60,
            run_id=self.run_id,
        )
        try:
            record_cli_usage(self.run_id, fix_res.usage or {}, fix_res.total_cost_usd or 0.0)
        except Exception:
            pass

        if fix_res.status == "suspended":
            self._suspend("GOVERNANCE_REVIEW", f"Governance fixer suspended: {fix_res.reason}")
            return False

        new_edits = self._collect_workspace_edits(stage="GOVERNANCE_REVIEW")
        if new_edits is None:
            return False  # compliance blocked → already suspended

        # Mark exactly the findings we handed the fixer as fixed.
        from store.sdlc_governance_findings import mark_fixed
        from agents.sdlc_governance.schema import fingerprint as fp_fn
        fix_fps = [fp_fn(f) for f in findings_to_fix]
        mark_fixed(self.run_id, fix_fps)

        return True

    def _build_verified_edits(self) -> list:
        """Assemble the per-file edit list for the VERIFIED_DIFF.

        Each entry carries the full NEW body plus the BASE body (read from GitLab
        at base_branch) so the post-gate applier can either write directly
        (HEAD==base) or derive content-anchored SEARCH/REPLACE blocks via difflib
        and apply them two-tier to a moved HEAD. Both impl/test files (kind=code)
        and SLT files (kind=slt) are captured so the diff is complete."""
        from tools.gitlab_tools import gitlab_read_file
        _branch = self.base_branch or "main"
        edits: list = []

        def _add(f: dict, kind: str):
            if not isinstance(f, dict):
                return
            path = f.get("path") or f.get("file")
            if not path:
                return
            new_body = f.get("content", "")
            base_body = ""
            is_new = bool(f.get("is_new"))
            try:
                _raw = gitlab_read_file(self.gitlab_repo, path, _branch)
                if _raw and not _raw.startswith("[Error"):
                    base_body = _raw
                    is_new = False
                elif _raw and ("404" in _raw):
                    is_new = True
            except Exception as _e:
                logger.debug(f"[SM {self.run_id}] verified-diff base read failed for {path}: {_e}")
            edits.append({
                "path":      path,
                "kind":      kind,
                "is_new":    is_new,
                "is_test":   bool(f.get("is_test")),
                "new_body":  new_body,
                "base_body": base_body,
                "deleted":   bool(f.get("deleted")),
            })

        for f in (self.code_output.get("files") or []):
            _add(f, "code")
        for f in (self.slt_output.get("slt_files") or []):
            _add(f, "slt")
        return edits

    def _finalize_pregate(self, *, tests_skipped: bool = False):
        """PRE-GATE terminus. Assemble + store the VERIFIED_DIFF artifact and
        RETURN without committing or opening an MR. The pipeline caller then
        transitions to the HITL approval gate."""
        logger.info(f"[SM {self.run_id}] PRE-GATE finalize — assembling VERIFIED_DIFF")
        base_sha = self._get_run_base_sha()
        edits = self._build_verified_edits()
        compile_summary = self._pregate_compile_summary()
        _tests_skipped = bool(tests_skipped or self.skip_tests)
        # Deferred = tests authored pre-gate but execution left to post-gate
        # TEST_VERIFY (Option 2). Distinguished from "failed" so the approval-gate
        # UI shows "verified post-gate", not a red X.
        _tests_deferred = bool((not _tests_skipped) and self._conf_tests <= 0)
        tests_summary = {
            "passed": bool((not _tests_skipped) and self._conf_tests > 0),
            "skipped": _tests_skipped,
            "deferred": _tests_deferred,
            "summary": (
                "tests authored pre-gate; execution deferred to post-gate TEST_VERIFY"
                if _tests_deferred else f"conf_tests={self._conf_tests:.2f}"
            ),
        }
        artifact = {
            "edits": edits,
            "base_sha": base_sha,
            "compile": compile_summary,
            "tests": tests_summary,
            "files": [e["path"] for e in edits if e.get("kind") == "code"],
            "slt_files": [e["path"] for e in edits if e.get("kind") == "slt"],
            "summary": self.code_output.get("summary", ""),
            "language": self.language,
            "skip_tests": bool(self.skip_tests),
            "skip_slt": bool(self.skip_slt),
            "compile_skipped": bool(self.compile_skipped),
        }
        # ── Multi-repo: surface the EDITABLE deps' changes to the human approver.
        #    ADDITIVE, distinctly-keyed section — the key is ABSENT on single-repo
        #    runs, so the artifact stays byte-identical there. `edits` / `files` /
        #    `slt_files` above remain PRIMARY-ONLY: they are the apply+commit set
        #    (_apply_verified_edits → _workspace_write → gitlab_batch_commit), and a
        #    dep path in them would commit dep source into the primary repo.
        #    Without this, COMMITTING's _create_sibling_mrs() would push dep code to
        #    a second customer repo that the approver never saw.
        # NOTE: sourced from the in-memory self._dep_edits (rich base/new bodies)
        #    populated by _collect_dep_edits() earlier in THIS phase. The persisted
        #    run.context["code_output_by_repo"] is not a usable fallback — it stores
        #    only the NEW content, with no base body to diff against.
        _dep_sections = self._build_dep_approval_sections()
        if _dep_sections:
            artifact["dep_edits_by_repo"] = _dep_sections
            logger.info(
                "[SM] dep edits surfaced to approval gate", run_id=self.run_id,
                repo_count=len(_dep_sections),
            )
        self._put_artifact("VERIFIED_DIFF", artifact, reason="pre-gate verified diff")
        self._add_event(
            "VERIFIED_DIFF", "ai-coder",
            f"verified diff ready: {len(edits)} file(s), "
            f"compile={compile_summary.get('passed')}, tests={tests_summary}",
            {"n_edits": len(edits), "base_sha": base_sha,
             "compile_passed": compile_summary.get("passed"),
             "tests_passed": tests_summary.get("passed")},
        )
        logger.info(
            f"[SM {self.run_id}] VERIFIED_DIFF stored",
            run_id=self.run_id, n_edits=len(edits), base_sha=base_sha,
            compile_passed=compile_summary.get("passed"),
            tests_passed=tests_summary.get("passed"),
        )

    # ── Post-gate deterministic apply ─────────────────────────────────────

    def _approval_state(self) -> str:
        """The HITL gate this run re-suspends to on a stale conflict. Bug runs
        re-gate at AWAITING_SOLUTION_APPROVAL; everything else at
        AWAITING_CODE_APPROVAL (renamed 2026-07-29 from AWAITING_DESIGN_APPROVAL)."""
        try:
            run = get_run(self.run_id) or {}
            if str(run.get("type") or "").lower() == "bug":
                return "AWAITING_SOLUTION_APPROVAL"
        except Exception:
            pass
        return "AWAITING_CODE_APPROVAL"

    def _regate(self, reason: str) -> None:
        """Re-suspend to the approval gate with an updated reason (merge-queue
        escalation: a stale base whose clean rebase failed needs a human)."""
        state = self._approval_state()
        logger.warning(
            f"[SM {self.run_id}] APPLYING re-gate → {state}: {reason}",
            run_id=self.run_id, reason=reason,
        )
        try:
            update_run_state(
                self.run_id, state, current_stage=state,
                context_patch={"applying_regate_reason": reason},
            )
        except Exception as _e:
            logger.warning(f"[SM {self.run_id}] re-gate state update failed: {_e}")
        self._add_event(state, "applying", f"RE-GATE: {reason}", {"reason": reason})

    def _suspend_build_failed_for_skip(self, reason: str) -> None:
        """Re-gate a post-apply BUILD FAILURE to the HITL approval gate so the UI
        shows 'compilation failed' AND offers 'Skip compilation & continue'.

        A post-apply build RED previously called ``_regate()``, which re-gated to the
        approval gate with NO build-failure surface — the run appeared to bounce back
        to APPLYING silently, with no way to push the already-created code. This
        re-gates to the SAME approval gate (``_approval_state()`` — the run's HITL
        state, e.g. AWAITING_CODE_APPROVAL) but adds an explicit ``build_failed``
        marker the UI reads to render the failure and expose the skip action.

        'Skip compilation & continue' resolves to the approval-gate resume
        (``POST /runs/{id}/approve`` with ``skip_compile_override=True``), which sets
        ``compile_skipped=True`` in the run context. The resumed POST-GATE machine
        reads ``compile_skipped`` (sdlc_pipeline._core resume path), RE-APPLIES the
        EXISTING VERIFIED_DIFF, SKIPS the build oracle (green-SKIPPED), and flows
        APPLYING → TEST_VERIFY → SLT → COMMITTING — PUSHING the already-created code
        to remote. No codegen re-runs (the VERIFIED_DIFF is reused as-is).

        Deliberately NOT a BASELINE_BUILD suspend: retrigger_pipeline re-runs the
        WHOLE pipeline from preflight (re-CLASSIFY/PLAN/IMPLEMENT), which would
        REGENERATE code instead of pushing what already exists — wrong for a
        post-gate failure."""
        state = self._approval_state()
        logger.warning(
            f"[SM {self.run_id}] APPLYING build failed — re-gating to {state} "
            f"with build-failure surface (skip-compilation to push): {reason}",
            run_id=self.run_id, stage=state, reason=reason,
        )
        # Fresh 48h HITL window — the approve-resume path enforces hitl_deadline, so a
        # build failure must (re)open the window or the panel would immediately EXPIRE.
        import time as _time
        _deadline = int(_time.time()) + 48 * 3600
        try:
            # Set the run DIRECTLY into the approval GATE state (like _regate), NOT
            # "SUSPENDED": the UI approval panel renders on run.state ==
            # AWAITING_CODE_APPROVAL / AWAITING_SOLUTION_APPROVAL (isHitl), and the
            # SUSPENDED dispatch would otherwise route to the generic resume panel,
            # which 400s on this non-resumable gate stage.
            update_run_state(
                self.run_id, state,
                current_stage=state,
                context_patch={
                    # UI marker: render 'compilation failed' + 'Skip compilation &
                    # continue'. Distinct from a plain approval re-gate so the panel
                    # is unambiguous about WHY the run is back at the gate.
                    "build_failed": {"status": "broken", "phase": "APPLYING", "reason": reason},
                    "applying_regate_reason": reason,
                    "hitl_deadline": _deadline,
                },
            )
        except Exception as _e:
            logger.warning(f"[SM {self.run_id}] build-failed re-gate state update failed: {_e}")
        self._add_event(
            state, "applying",
            f"BUILD FAILED: {reason}", {"reason": reason, "phase": "APPLYING", "build_failed": True},
        )

    def _current_branch_head(self, ref: str) -> str:
        """Current GitLab HEAD SHA of `ref` (staleness check independent of any
        locally pinned checkout). Falls back to the repo's real default branch when
        `ref` is empty or 404s, so base_sha capture + staleness stay consistent on
        repos whose default is not 'main' (develop/master). '' on any failure."""
        try:
            from tools.gitlab_tools import _get, _proj, _url_quote, _detect_default_branch

            def _head(b: str) -> str:
                if not b:
                    return ""
                info = _get(f"/projects/{_proj(self.gitlab_repo)}/repository/branches/"
                            f"{_url_quote(b, safe='')}")
                if isinstance(info, dict):
                    return (info.get("commit") or {}).get("id") or ""
                return ""

            sha = _head(ref)
            if not sha:
                _default = _detect_default_branch(self.gitlab_repo)
                if _default and _default != ref:
                    sha = _head(_default)
            return sha
        except Exception as _e:
            logger.debug(f"[SM {self.run_id}] branch head read failed for {ref}: {_e}")
        return ""

    def _derive_search_replace_blocks(self, base_body: str, new_body: str, ctx: int = 3) -> list:
        """Derive content-anchored (search, replace) blocks from base→new via
        difflib. Adjacent changed regions separated by short equal gaps are merged
        so their context windows cannot overlap (which would defeat the applier).
        Blocks match by CONTENT (two-tier), never by line number — tolerating
        unrelated drift elsewhere in a moved HEAD."""
        import difflib
        a = base_body.splitlines()
        b = new_body.splitlines()
        ops = [op for op in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes()
               if op[0] != "equal"]
        if not ops:
            return []
        merged: list = []
        for _tag, i1, i2, j1, j2 in ops:
            if merged and i1 - merged[-1][2] <= 2 * ctx:
                pi1, pj1 = merged[-1][1], merged[-1][3]
                merged[-1] = ("replace", pi1, i2, pj1, j2)
            else:
                merged.append(("replace", i1, i2, j1, j2))
        blocks: list = []
        for _tag, i1, i2, j1, j2 in merged:
            c1 = max(0, i1 - ctx)
            c2 = min(len(a), i2 + ctx)
            search = "\n".join(a[c1:c2])
            replace = "\n".join(a[c1:i1] + b[j1:j2] + a[i2:c2])
            if search.strip():
                blocks.append((search, replace))
        return blocks

    def _apply_verified_edits(self, edits: list, stale: bool) -> tuple:
        """Deterministically apply the approved edits to the workspace.

        Clean (HEAD==base) or new file → write the exact new body. Stale
        (HEAD moved) → read the current branch content from GitLab, derive
        content-anchored blocks and apply them two-tier (exact → whitespace-
        normalized). A clean MISS (no/ambiguous match) is recoverable — recorded
        and surfaced for re-gate — never a wrong silent edit (CLAUDE.md: two-tier
        only). Returns (ok: bool, misses: list[str])."""
        from agents.sdlc_patch_engine import patch_engine as _pe, restore_missing_imports
        _branch = self.base_branch or "main"
        import os as _os
        misses: list = []
        for e in edits:
            path = e.get("path")
            if not path:
                continue
            if e.get("deleted"):
                # Deletion: remove the file from the workspace so TEST_VERIFY
                # runs against the real post-change tree (not a 0-byte stub).
                _ws = self._run_workspace_path or ""
                if _ws:
                    _full = _os.path.join(_ws, path)
                    try:
                        if _os.path.exists(_full):
                            _os.remove(_full)
                    except Exception as _del_e:
                        logger.warning(
                            f"[SM {self.run_id}] could not remove deleted file {path}: {_del_e}"
                        )
                continue
            new_body = e.get("new_body", "")
            base_body = e.get("base_body", "")
            is_new = e.get("is_new")
            if is_new or not base_body:
                # New file (or no base to anchor against) — write the body verbatim.
                self._workspace_write(path, new_body)
                continue
            if not stale:
                # HEAD == base: the checkout content equals base → the exact new
                # body IS the verified result. Deterministic, no derivation needed.
                self._workspace_write(path, new_body)
                continue
            # Stale: re-apply onto current branch content (true HEAD via GitLab).
            head_content = None
            try:
                from tools.gitlab_tools import gitlab_read_file as _glrf
                _raw = _glrf(self.gitlab_repo, path, _branch)
                if _raw and not _raw.startswith("[Error"):
                    head_content = _raw
            except Exception:
                head_content = None
            if head_content is None:
                misses.append(path)
                continue
            blocks = self._derive_search_replace_blocks(base_body, new_body)
            if not blocks:
                continue
            patched, warnings = _pe._apply_search_replace(head_content, blocks, self.language)
            if patched == head_content or warnings:
                # Clean miss or ambiguity — recoverable; do NOT write a partial edit.
                misses.append(path)
                continue
            try:
                patched = restore_missing_imports(patched, head_content, self.language)
            except Exception as _ie:
                logger.warning(f"[SM {self.run_id}] import-restore (apply) failed for {path}: {_ie}")
            self._workspace_write(path, patched)
        return (len(misses) == 0, misses)

    def _refresh_outputs_from_workspace(self, edits: list) -> None:
        """After a successful apply, re-read the applied files from the workspace
        so COMMITTING pushes exactly what was built+tested (important on the
        rebase path where content was re-derived)."""
        code_files: list = []
        slt_files: list = []
        for e in edits:
            path = e.get("path")
            if not path:
                continue
            applied = self._workspace_read(path)
            if applied is None:
                applied = e.get("new_body", "")
            if e.get("kind") == "slt":
                entry = {"path": path, "content": applied}
                if e.get("deleted"):
                    # Deletion: the file was physically removed by
                    # _apply_verified_edits, so the workspace read returned None
                    # and `applied` fell back to "". Preserve the deletion intent
                    # so COMMITTING takes the delete branch instead of tripping
                    # the anti-truncation guard.
                    entry["deleted"] = True
                    entry["content"] = ""
                slt_files.append(entry)
            else:
                entry = {"path": path, "content": applied,
                         "is_test": bool(e.get("is_test")),
                         "is_new": bool(e.get("is_new"))}
                if e.get("deleted"):
                    # See slt branch above — carry the deletion flag through the
                    # refresh so the file reaches COMMITTING's delete branch.
                    entry["deleted"] = True
                    entry["content"] = ""
                code_files.append(entry)
        self.code_output = {"files": code_files,
                            "summary": (self._get_artifact("VERIFIED_DIFF") or {}).get("summary", "")}
        self.slt_output = {"slt_files": slt_files}

    def _phase_applying(self):
        """POST-GATE deterministic apply + staleness re-verify + auto-rebase.

        Reads the approved VERIFIED_DIFF, hydrates code/SLT outputs, applies the
        edits deterministically (two-tier; import guards), rebuilds, and:
          • HEAD==base, green → TEST_VERIFY.
          • HEAD moved, clean rebase + green → TEST_VERIFY and RE-PIN base_sha.
          • apply-miss / red → re-review required (regate back to the approval gate).
        Never commits LLM-authored code post-gate — codegen happened pre-gate.
        """
        vd = self._get_artifact("VERIFIED_DIFF") or {}
        edits = vd.get("edits") or []
        if not edits:
            logger.error(f"[SM {self.run_id}] APPLYING: no VERIFIED_DIFF edits — cannot apply")
            self._suspend("APPLYING", "No verified diff to apply — re-run pre-gate codegen")
            return

        base_sha = vd.get("base_sha") or self._get_run_base_sha()
        try:
            self._ensure_run_workspace(self.repo)
        except Exception as _ws_e:
            logger.warning(f"[SM {self.run_id}] APPLYING: workspace prep failed ({_ws_e})")

        head_sha = self._current_branch_head(self.base_branch or "main")
        stale = bool(base_sha and head_sha and head_sha != base_sha)
        logger.info(
            f"[SM {self.run_id}] APPLYING path chosen",
            run_id=self.run_id, head_sha=head_sha, base_sha=base_sha,
            stale=stale, path=("rebase" if stale else "clean"),
        )

        applied_ok, misses = self._apply_verified_edits(edits, stale)
        # CRITICAL: the post-gate SM is a FRESH instance — self.code_output starts
        # empty. _build_check / _execute_tests are code_output-driven, so we MUST
        # bridge the applied workspace content into code_output BEFORE any build or
        # test, or the build oracle returns a vacuous "no impl files" green and the
        # post-gate recompile guarantee (the whole point of APPLYING) never runs.
        self._refresh_outputs_from_workspace(edits)
        if not applied_ok:
            logger.warning(
                f"[SM {self.run_id}] APPLYING: {len(misses)} edit(s) did not apply cleanly",
                run_id=self.run_id, reason="apply_miss", apply_misses=misses, compile_passed=None,
            )
            self._regate(
                "base moved; clean rebase failed — re-review required"
                if stale else "approved diff did not apply cleanly — re-review required"
            )
            return

        # Build oracle — compile_skipped degrades to green-SKIPPED (no gate, no recompile).
        if self.compile_skipped:
            logger.warning(f"[SM {self.run_id}] APPLYING: compile_skipped=True — build SKIPPED")
            build = {"success": True, "_build_status": "SKIPPED"}
        else:
            build = self._build_oracle()
        if not build.get("success"):
            if build.get("transient"):
                # Infra/transient (docker down, connectivity) — bounded retry, then SUSPEND
                # with an infra-specific reason. Do NOT _regate: re-approval just re-hits the
                # same wall and the "design failed" message misleads the engineer.
                _RETRIES = 2
                for _i in range(_RETRIES):
                    build = self._build_oracle()
                    if build.get("success"):
                        break
                    if not build.get("transient"):
                        break
                if not build.get("success") and build.get("transient"):
                    logger.warning(f"[SM {self.run_id}] APPLYING: build infra unavailable after retries — suspending")
                    # Same UI surface + skip option as a genuine RED: 'Skip
                    # compilation & continue' lets the operator push the created
                    # code without a verified build when infra can't run it.
                    self._suspend_build_failed_for_skip(
                        "post-gate build could not run — Docker/build infrastructure "
                        "unavailable; skip compilation to push, or retry when infra is restored"
                    )
                    return
            # A transient failure that RECOVERED to green on retry must NOT fall into the
            # regate path below (that would falsely _regate a build that actually passed).
            # Only a genuine code red — or a transient that flipped to a real error on
            # retry — reaches regate.
            if not build.get("success"):
                logger.warning(
                    f"[SM {self.run_id}] APPLYING: post-apply build RED",
                    run_id=self.run_id, reason="build_red", apply_misses=misses,
                    compile_passed=False,
                )
                self._suspend_build_failed_for_skip(
                    "post-gate build failed after apply — compilation failed"
                )
                return

        # Recovery may have rewritten files; re-bridge so COMMITTING pushes exactly
        # what was built+tested. Re-pin base on a clean rebase.
        self._refresh_outputs_from_workspace(edits)
        if stale and head_sha:
            try:
                from db.database import engine as _eng
                from sqlalchemy import text as _txt
                with _eng.connect() as _c:
                    _c.execute(_txt("UPDATE sdlc_runs SET base_sha=:s WHERE id=:id"),
                               {"s": head_sha, "id": self.run_id})
                    _c.commit()
                logger.info(f"[SM {self.run_id}] APPLYING: re-pinned base_sha={head_sha[:8]} after rebase")
            except Exception as _pin_e:
                logger.warning(f"[SM {self.run_id}] APPLYING: base_sha re-pin failed: {_pin_e}")

        self._put_artifact("APPLYING", {"applied": True, "stale": stale,
                                        "head_sha": head_sha, "base_sha": base_sha,
                                        "misses": misses})
        self._set_state("TEST_VERIFY")
        self._phase_test_verify()

    def _phase_test_verify(self):
        """POST-GATE deterministic verification. Re-run the SAME unit tests on the
        freshly-applied tree. Green → SLT_RUNNING (→ COMMITTING). Red → SUSPEND for
        human re-review."""
        if self.skip_tests:
            logger.info(f"[SM {self.run_id}] TEST_VERIFY: skip_tests=True — bypassing → SLT_RUNNING")
            self._set_state("SLT_RUNNING")
            self._phase_slt_running()
            return

        test_files = [f for f in self.code_output.get("files", []) if f.get("is_test")]
        impl_files = [f for f in self.code_output.get("files", []) if not f.get("is_test")]
        if not test_files:
            logger.warning(f"[SM {self.run_id}] TEST_VERIFY: no test files in diff → SLT_RUNNING")
            self._set_state("SLT_RUNNING")
            self._phase_slt_running()
            return

        test_result = self._execute_tests(test_files, impl_files)
        self._add_event("TEST_VERIFY", "test-runner",
                        f"passed={test_result.get('passed')} failed={test_result.get('failed')}",
                        test_result)
        ok = bool(test_result.get("success") and test_result.get("passed", 0) > 0)
        logger.info(
            f"[SM {self.run_id}] TEST_VERIFY result tests_passed={ok}",
            run_id=self.run_id, tests_passed=ok, suspended=(not ok),
        )
        if ok:
            self._put_artifact("TEST_VERIFY", {"passed": True})
            self._set_state("SLT_RUNNING")
            self._phase_slt_running()
            return
        self._suspend("TEST_VERIFY",
                      "post-gate tests failed on the applied tree — re-review required")

    # ── Phase: COMPLETION_REVIEW ──────────────────────────────

    def _build_solution_doc(self) -> str:
        """Build a Markdown documentation file summarising the AI solution design."""
        run = get_run(self.run_id) or {}
        confluence_url = (run.get("context") or {}).get("confluence_url", "")
        gitlab_issue_url = (run.get("context") or {}).get("gitlab_issue_url", "")

        lines = [
            f"# {self.jira_key} — AI Solution Design",
            "",
            "> Auto-generated by AiNxt AI. Do not edit manually.",
            "",
            "## Overview",
            "",
        ]

        approach = (
            self.design.get("solution_approach")
            or "\n".join(self.design.get("implementation_plan") or [])
        )
        if approach:
            lines += [approach, ""]

        def _sp(x):
            if isinstance(x, str): return x
            if isinstance(x, dict): return x.get("path") or x.get("file") or x.get("name") or ""
            return str(x)
        files_changed = [
            _sp(f)
            for f in (list(self.analysis.get("files_to_change") or [])
                      + list(self.analysis.get("new_files_needed") or []))
            if _sp(f)
        ]
        if files_changed:
            lines += ["## Files Affected", ""]
            for f in files_changed:
                lines.append(f"- `{f}`")
            lines.append("")

        db_chg = str(self.design.get("data_model_changes") or "").strip()
        if db_chg and db_chg.lower() not in ("none", "n/a", "—", "-", "", "null", "{}"):
            lines += ["## Database Changes", "", db_chg, ""]

        api_chg = str(self.design.get("api_changes") or "").strip()
        if api_chg and api_chg.lower() not in ("none", "n/a", "—", "-", "", "null", "{}"):
            lines += ["## API Changes", "", api_chg, ""]

        testing = str(self.design.get("testing_strategy") or "").strip()
        if testing and testing.lower() not in ("none", "n/a", "—", "-", "", "null", "{}"):
            lines += ["## Testing Strategy", "", testing, ""]

        if confluence_url:
            lines += ["## Full Design Document", "", f"[View on Confluence]({confluence_url})", ""]

        if gitlab_issue_url:
            lines += ["## GitLab Tracking Issue", "", f"[View Issue]({gitlab_issue_url})", ""]

        lines += ["---", f"*Generated by AiNxt AI SDLC Pipeline — {self.jira_key}*"]
        return _sanitize_for_api("\n".join(lines))

    # ── Phase: COMMITTING ─────────────────────────────────────

    def _phase_commit(self):
        """
        Create branch → commit all files → open MR.
        This is the final CRED stage: autonomous commit to GitLab.
        """
        from store.sdlc_store import SDLCCancelled
        # Cancellation gate: if the run was cancelled out-of-band (operator hit
        # cancel mid-pipeline), abort BEFORE creating a branch / committing /
        # opening an MR. Without this, a cancelled run's zombie worker would race
        # a re-triggered run and open a duplicate MR on a different branch (which
        # the 409 idempotency rule cannot catch). Leave the state as CANCELLED.
        try:
            _live = get_run(self.run_id) or {}
            if _live.get("state") == "CANCELLED":
                logger.info(
                    f"[SM {self.run_id}] COMMITTING aborted — run was cancelled; "
                    f"no branch/commit/MR will be created"
                )
                self._add_event("COMMITTING", "ai-committer",
                                "Commit aborted: run cancelled before MR creation", {})
                return
        except Exception as _cancel_chk_err:
            logger.warning(f"[SM {self.run_id}] commit cancel-check failed: {_cancel_chk_err}")

        # Guard: never create an MR if ALL generated files were blocked.
        # An empty MR with no code changes is worse than no MR at all.
        #
        # Multi-repo amendment: if the primary has no files but at least one
        # editable dep has files in code_output_by_repo, we proceed — the
        # primary MR will still open (some Jira tickets are dep-only refactors
        # consumed via version bump elsewhere) and siblings will commit their
        # changes. The guard only blocks when EVERY repo produced nothing.
        _primary_has_files = bool(self.code_output.get("files"))
        _dep_has_files = False
        if not _primary_has_files:
            try:
                _run = get_run(self.run_id) or {}
                _cobr = (_run.get("context") or {}).get("code_output_by_repo") or {}
                _dep_has_files = any(
                    bool((v or {}).get("files"))
                    for k, v in _cobr.items()
                    if k != self.gitlab_repo
                )
            except Exception:
                _dep_has_files = False
        if not _primary_has_files and not _dep_has_files:
            logger.error(
                f"[SM {self.run_id}] MR BLOCKED — zero files generated successfully. "
                f"All files failed self-healing. Jira ticket will be commented with failure details."
            )
            self._set_state("FAILED")
            self._add_event(
                "COMMIT", "mr-blocked",
                "MR creation blocked: all code generation attempts failed self-healing validation. "
                "Manual implementation required.",
                {},
            )
            # Notify via Jira comment
            try:
                from tools.jira_tools import jira_add_comment
                jira_add_comment(
                    self.jira_key,
                    f"[AiNxt AI] Code generation failed for all planned files after self-healing. "
                    f"No MR was created. Manual implementation required for ticket {self.jira_key}.",
                    user_id=self.user_id, user_email=self.user_email,
                )
            except Exception:
                pass
            return

        logger.info(f"[SM {self.run_id}] COMMITTING — creating branch + committing files + opening PR")

        # Ensure the triggering user's SCM token is active on this worker thread
        # so commits and MR creation are attributed to the correct user.
        self._set_user_scm_token()

        # Phase 5 backend: open sibling MRs for editable deps BEFORE the primary
        # MR. The primary's PR body embeds links to the siblings, so they must
        # exist first. No-op for single-repo runs (returns {}). Failures on
        # individual sibling repos are logged but never abort the primary.
        try:
            self._sibling_mr_urls = self._create_sibling_mrs() or {}
        except Exception as _sib_exc:
            logger.error(f"[SM {self.run_id}] _create_sibling_mrs raised: {_sib_exc}")
            self._sibling_mr_urls = {}

        # Use pre-created working branch when available (set at trigger time from product_repos).
        # Fall back to old naming scheme for backwards compat / Jira webhook triggers.
        branch = self.working_branch or f"{_BRANCH_PREFIX}/{self.jira_key.lower()}-ai-impl"
        pr_url = ""
        pr_number = None

        if not self.gitlab_repo:
            logger.error(f"[SM {self.run_id}] COMMITTING aborted: no SCM repo configured")
            update_run_state(self.run_id, "FAILED", error="No SCM repo configured")
            return

        try:
            from core.config import SCM_PROVIDER as _SCM
            if _SCM == "github":
                from tools.github_tools import (
                    github_create_branch as gitlab_create_branch,
                    github_batch_commit as gitlab_batch_commit,
                    github_create_pr as gitlab_create_mr,
                )
            else:
                from tools.gitlab_tools import (
                    gitlab_create_branch, gitlab_batch_commit, gitlab_create_mr
                )

            # 1. Create branch — use base_branch as source so the MR diff is correct.
            # Passing from_branch explicitly avoids _detect_default_branch timing out
            # and silently falling back to "main" instead of the repo's real default.
            _from_branch = self.base_branch or "main"
            branch_result = gitlab_create_branch(self.gitlab_repo, branch, from_branch=_from_branch)
            if branch_result.startswith("[Error"):
                # 409 = branch already exists (e.g. prior partial run) — reuse it safely
                if "already exists" in branch_result or "Branch already exists" in branch_result or "409" in branch_result:
                    logger.warning(f"[SM {self.run_id}] branch {branch!r} already exists — reusing existing branch")
                else:
                    raise Exception(f"Branch creation failed: {branch_result}")
            else:
                logger.info(f"[SM {self.run_id}] branch created: {branch}")

            # 2. Build the atomic actions array for ALL files (impl + tests +
            #    slt + solution doc). One commit either lands whole or not at
            #    all — a transient Gitaly "4:Deadline Exceeded" mid-loop no
            #    longer leaves the run half-committed and FAILED.
            _raw_impl  = [f for f in self.code_output.get("files", []) if not f.get("is_test")]
            _raw_tests = [f for f in self.code_output.get("files", []) if f.get("is_test")]
            _raw_slt   = self.slt_output.get("slt_files", [])

            # W-E (fixes C1): the commit set includes ALL impl files — no cap.
            # The previous MAX_FILES_PER_RUN slice silently dropped planned new
            # files from the MR; full scope is approved at the HITL design gate.
            all_files = _raw_impl + _raw_tests + _raw_slt

            actions = []
            for file_def in all_files:
                path    = file_def.get("path", "")
                content = file_def.get("content", "")
                if not path:
                    continue
                if file_def.get("deleted"):
                    actions.append({"action": "delete", "file_path": path})
                    continue
                if not content:
                    if file_def.get("is_new"):
                        # Legitimately empty new file (e.g. __init__.py, .gitkeep).
                        actions.append({"action": "create", "file_path": path, "content": ""})
                    else:
                        # Empty content on a non-new, non-deleted file is a coder
                        # bug — committing it would silently truncate the file.
                        # Suspend so the operator can investigate and re-run IMPLEMENT.
                        logger.error(
                            f"[SM {self.run_id}] COMMITTING: {path!r} has empty content "
                            f"but is neither new nor marked deleted — suspending to prevent "
                            f"silent truncation."
                        )
                        self._suspend(
                            "COMMITTING",
                            f"{path!r} has empty content without a deletion marker — "
                            f"likely a coder output bug. Re-run IMPLEMENT to regenerate.",
                        )
                        return
                    continue
                actions.append({
                    # is_new==True → create, else update (resume re-runs are
                    # idempotent: gitlab_batch_commit flips create↔update if the
                    # file already exists from a prior partial commit).
                    "action":    "create" if file_def.get("is_new") else "update",
                    "file_path": path,
                    "content":   content,
                })

            # Append the solution documentation file to the SAME atomic commit.
            try:
                doc_content = self._build_solution_doc()
                if doc_content:
                    doc_path = f"docs/{_BRANCH_PREFIX}/{self.jira_key.lower()}-solution.md"
                    actions.append({
                        "action":    "update",  # flips to create if absent
                        "file_path": doc_path,
                        "content":   doc_content,
                    })
            except Exception as _doc_ex:
                logger.warning(f"[SM {self.run_id}] solution doc build failed (non-fatal): {_doc_ex}")

            # Hard gate: if the coder produced no parseable files, suspend (not
            # FAIL) — resumable. No fallback commits (IMPLEMENTATION_NOTES.md,
            # empty stubs, markdown extracts). A real commit MUST contain code
            # that passed build + tests.
            #
            # Multi-repo amendment: dep-only refactors (no primary code changes,
            # only editable deps modified) are valid. When _dep_has_files is True
            # we let the primary commit go through with just the solution doc,
            # since sibling MRs already carry the real changes.
            _has_code_actions = any(
                a["file_path"] != f"docs/{_BRANCH_PREFIX}/{self.jira_key.lower()}-solution.md"
                for a in actions
            )
            if not _has_code_actions and not _dep_has_files:
                logger.error(f"[SM {self.run_id}] COMMITTING: no valid files to commit — FAILED")
                update_run_state(
                    self.run_id, "FAILED",
                    error="Code generation produced no parseable files to commit. "
                          "The coder LLM output could not be converted to committable source code. "
                          "Check coder prompt and re-run the coding phase."
                )
                return

            # 2b. ONE atomic, retried commit for all files.
            commit_msg = f"[{self.jira_key}] AI implementation ({len(actions)} file(s))"
            batch_result = gitlab_batch_commit(
                repo=self.gitlab_repo,
                branch=branch,
                actions=actions,
                message=commit_msg,
            )
            if batch_result.startswith("[Error"):
                # Commit failed after retries. The generated code is already
                # durably persisted in the CODING artifact — do NOT mark FAILED
                # (which forces a full re-run). Suspend at the resumable
                # COMMIT_FAILED state; POST /sdlc/runs/{id}/retry-commit replays
                # branch + batch-commit + MR with no earlier stages.
                logger.error(f"[SM {self.run_id}] COMMITTING: batch commit failed → COMMIT_FAILED: {batch_result}")
                update_run_state(
                    self.run_id, "COMMIT_FAILED",
                    branch=branch,
                    error=f"Atomic commit failed after retries: {batch_result}. "
                          f"Generated code is preserved — retry the commit via "
                          f"POST /sdlc/runs/{self.run_id}/retry-commit (no earlier stages re-run).",
                )
                self._add_event("COMMITTING", "ai-committer",
                                f"Atomic commit failed — run resumable at COMMIT_FAILED (branch={branch})",
                                {"branch": branch, "commit_error": batch_result})
                try:
                    from tools.jira_tools import jira_add_comment as _jac
                    _jac(
                        self.jira_key,
                        f"[AiNxt AI] ⚠️ Commit to branch `{branch}` failed transiently and was preserved. "
                        f"The run is paused at COMMIT_FAILED — retry the commit from the SDLC UI "
                        f"(no earlier stages will re-run).",
                        user_id=self.user_id, user_email=self.user_email,
                    )
                except Exception:
                    pass
                return
            logger.info(f"[SM {self.run_id}] {len(actions)} file(s) committed atomically to {branch}")

            # 3. Open MR — target is base_branch (the team's real working branch)
            # _s() coerces solution_approach to a plain string: the designer may
            # return it as a dict/list, and slicing a dict raises
            # KeyError: slice(None, 80, None) — see SDLC_FORMATTING_RULES.md.
            _sa_title = _s(self.design.get('solution_approach')).strip() or 'AI Implementation'
            pr_title = f"[{self.jira_key}] {_sa_title[:80]}"
            pr_body  = self._build_pr_description()
            if self.base_branch:
                default_branch = self.base_branch
            else:
                from tools.gitlab_tools import _detect_default_branch
                default_branch = _detect_default_branch(self.gitlab_repo)

            # Empty-diff guard (2026-07-31): never open an MR with no changes. If the
            # committed branch has no diff over the target branch there is nothing to
            # merge — mark the run COMPLETE (a valid no-op) instead of creating an
            # empty MR. FAIL-OPEN on an undetermined compare (None) so a transient
            # GitLab error never silently drops a real change. Skipped for multi-repo
            # dep-only runs, where the sibling MRs carry the real changes.
            if not _dep_has_files:
                from core.config import SCM_PROVIDER as _SCM
                if _SCM == "github":
                    from tools.github_tools import github_branch_has_changes as _branch_has_changes
                else:
                    from tools.gitlab_tools import gitlab_branch_has_changes as _branch_has_changes
                _has_changes = _branch_has_changes(self.gitlab_repo, default_branch, branch)
                if _has_changes is False:
                    logger.info(
                        f"[SM {self.run_id}] COMMITTING: branch {branch!r} has no diff over "
                        f"{default_branch!r} — no MR created, marking run COMPLETE."
                    )
                    update_run_state(
                        self.run_id, "COMPLETE",
                        branch=branch,
                        current_stage="COMPLETE",
                    )
                    self._add_event(
                        "COMMITTING", "ai-committer",
                        f"No changes to merge — branch '{branch}' matches '{default_branch}'; "
                        f"no MR created. Run marked complete.",
                        {"branch": branch, "base_branch": default_branch},
                    )
                    try:
                        from services.sdlc_budget_tracker import finalize_run_budget as _fin
                        _fin(self.run_id)
                    except Exception:
                        pass
                    try:
                        from tools.jira_tools import jira_add_comment as _jac
                        _jac(
                            self.jira_key,
                            f"[AiNxt AI] The implementation produced no net changes over "
                            f"`{default_branch}` — no merge request was created. Run marked complete.",
                            user_id=self.user_id, user_email=self.user_email,
                        )
                    except Exception:
                        pass
                    return

            # 2026-07-24 (author-triggered governance): the MR is always opened as a
            # NORMAL (non-draft) MR. Governance is no longer an automatic draft-gate
            # at commit time — the author optionally triggers it after the MR exists
            # (POST /sdlc/runs/{id}/governance/start), and the author-triggered
            # end-gate re-drafts the MR for the gate duration (un-drafts on approval).
            pr_result_str = gitlab_create_mr(
                repo=self.gitlab_repo,
                title=pr_title,
                body=pr_body,
                head=branch,
                base=default_branch,
                draft=False,
            )
            # gitlab_create_mr returns: "MR created: <url> (!<number>)"
            # or "[Error creating MR: ...]"
            pr_number = None
            pr_url    = ""
            if pr_result_str.startswith("[Error"):
                logger.error(f"[SM {self.run_id}] MR creation failed: {pr_result_str}")
                # The atomic commit already landed on `branch`. Only MR creation
                # failed — suspend at AWAITING_PR_APPROVAL (commit-then-suspend,
                # same resumable model as the commit-failure path). The engineer
                # can create the MR manually, OR retry the commit step
                # (POST /sdlc/runs/{id}/retry-commit) which re-runs MR creation
                # idempotently via gitlab_create_mr's _find_existing_mr.
                update_run_state(
                    self.run_id, "AWAITING_PR_APPROVAL",
                    branch=branch,
                    pr_number=None,
                    pr_url="",
                    error=f"Code committed to branch '{branch}' but MR creation failed: {pr_result_str}. "
                          f"Create the MR manually in GitLab, or retry the commit step from the SDLC UI.",
                )
                self._add_event("COMMITTING", "ai-committer",
                                f"MR creation failed — branch={branch} committed OK, create MR manually",
                                {"branch": branch, "mr_error": pr_result_str})
                try:
                    from tools.jira_tools import jira_add_comment as _jac
                    _jac(
                        self.jira_key,
                        f"[AiNxt AI] ⚠️ Code committed to branch `{branch}` but MR creation failed.\n"
                        f"Error: `{pr_result_str}`\nCreate the MR manually in GitLab, or retry the commit "
                        f"step from the SDLC UI (it re-runs MR creation idempotently).",
                        user_id=self.user_id, user_email=self.user_email,
                    )
                except Exception:
                    pass
                return
            else:
                num_match = re.search(r"\(!(\d+)\)", pr_result_str)
                url_match = re.search(r"MR created:\s*(https://\S+)", pr_result_str)
                pr_number = int(num_match.group(1)) if num_match else None
                pr_url    = url_match.group(1).rstrip(")") if url_match else ""
            logger.info(f"[SM {self.run_id}] MR created: !{pr_number} {pr_url}")

            # Phase 6: after the primary MR is open, ask manifest_writer to
            # diff sdlc_run_repos vs the primary's .sdlc.yml and (if changed)
            # open a follow-up MR updating the manifest. Best-effort — any
            # failure is logged but never raised; the primary MR stands alone.
            if self._sibling_mr_urls or self._get_run_repos_info():
                try:
                    from agents.manifest_writer import propose_manifest_update
                    self._manifest_update_url = propose_manifest_update(self.run_id) or ""
                    if self._manifest_update_url:
                        logger.info(
                            f"[SM {self.run_id}] manifest follow-up MR opened: {self._manifest_update_url}"
                        )
                except Exception as _mu_exc:
                    logger.warning(f"[SM {self.run_id}] manifest_writer failed: {_mu_exc}")

        except SDLCCancelled:
            # Out-of-band cancel during commit — preserve CANCELLED, do not
            # overwrite with COMMIT_FAILED.
            raise
        except Exception as e:
            # Suspend-not-fail: the generated code is durable in the CODING
            # artifact, so an unexpected error in the COMMITTING phase
            # (branch/MR/manifest) leaves the run resumable at COMMIT_FAILED
            # rather than forcing a full re-run. Branch reuse + idempotent
            # batch-commit + MR _find_existing_mr make re-entry safe.
            logger.error(f"[SM {self.run_id}] COMMITTING failed: {e}")
            update_run_state(
                self.run_id, "COMMIT_FAILED",
                branch=branch,
                error=f"Commit phase error: {e}. Generated code is preserved — retry the commit via "
                      f"POST /sdlc/runs/{self.run_id}/retry-commit.",
            )
            self._add_event("COMMITTING", "ai-committer",
                            f"Commit phase error — run resumable at COMMIT_FAILED: {e}",
                            {"branch": branch, "error": str(e)})
            return

        # ── Merge conflict detection ───────────────────────────
        # Check if the newly-opened MR has a merge conflict with the base branch.
        try:
            from agents.sdlc_pipeline import _detect_merge_conflict, _generate_conflict_resolution
            _gl_base  = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
            _repo_url = f"{_gl_base}/{self.gitlab_repo}"
            _has_conflict = _detect_merge_conflict(_repo_url, branch, default_branch)
            if _has_conflict:
                logger.warning(
                    f"[SM {self.run_id}] Merge conflict detected on branch {branch} → MERGE_CONFLICT"
                )
                _conflict_ctx = (
                    f"Repository: {self.gitlab_repo}\n"
                    f"Feature branch: {branch}\n"
                    f"Base branch: {default_branch}\n"
                    f"Jira ticket: {self.jira_key}\n"
                    f"PR URL: {pr_url}\n"
                    f"Files changed: {[f.get('path','') for f in self.code_output.get('files', [])]}"
                )
                _resolution = _generate_conflict_resolution(_conflict_ctx)

                # Transition to MERGE_CONFLICT state and store proposal
                update_run_state(
                    self.run_id, "MERGE_CONFLICT",
                    branch=branch,
                    pr_number=pr_number,
                    pr_url=pr_url,
                    resolution_proposal=_resolution,
                )
                self._add_event("MERGE_CONFLICT", "ai-conflict-resolver",
                                f"Merge conflict on branch={branch} vs {default_branch}",
                                {
                                    "branch": branch,
                                    "base_branch": default_branch,
                                    "pr_number": pr_number,
                                    "pr_url": pr_url,
                                    "resolution_proposal": _resolution,
                                })

                # Post inbox notification for HITL resolution
                try:
                    from store.inbox_store import publish_inbox_item
                    publish_inbox_item(
                        user_id="platform",
                        type="merge_conflict_resolution",
                        title=f"Merge conflict detected: {self.jira_key} — {branch}",
                        body=(
                            f"Merge conflict detected on PR #{pr_number} "
                            f"(`{branch}` → `{default_branch}`).\n\n"
                            f"**AI Resolution Proposal:**\n\n{_resolution}"
                        ),
                        source_id=self.run_id,
                        metadata={
                            "run_id":              self.run_id,
                            "jira_key":            self.jira_key,
                            "pr_number":           pr_number,
                            "pr_url":              pr_url,
                            "branch":              branch,
                            "base_branch":         default_branch,
                            "hitl_status":         "pending",
                            "resolution_proposal": _resolution,
                        },
                    )
                except Exception as _inbox_ex:
                    logger.warning(f"[SM {self.run_id}] conflict inbox_notify failed: {_inbox_ex}")

                # Teams notification
                try:
                    from agents.sdlc_pipeline import _teams_notify
                    _teams_notify(
                        self.run_id,
                        f"⚠️ **Merge Conflict Detected** — `{self.jira_key}`\n"
                        f"Branch: `{branch}` cannot be merged into `{default_branch}`.\n"
                        f"Review the resolution proposal in Threads/Inbox and resolve manually.\n"
                        + (f"[Open PR]({pr_url})" if pr_url else ""),
                    )
                except Exception:
                    pass

                logger.info(f"[SM {self.run_id}] COMMITTING complete → MERGE_CONFLICT (hitl_required)")
                return
        except Exception as _mc_ex:
            logger.warning(f"[SM {self.run_id}] merge conflict check failed (non-fatal): {_mc_ex}")

        # 2026-07-24 (author-triggered governance): governance is decoupled from
        # commit. EVERY run — governance-enabled or not — advances to
        # AWAITING_PR_APPROVAL with a normal (non-draft) MR. The author then
        # optionally POSTs /governance/start to run the end-gate (which re-drafts the
        # MR for the gate duration). No auto draft-gate, no COMMITTING-hold branch.
        update_run_state(
            self.run_id, "AWAITING_PR_APPROVAL",
            branch=branch,
            pr_number=pr_number,
            pr_url=pr_url,
        )
        logger.info("[SDLC-GOV] MR created (non-draft) — advancing to PR approval "
                    "(governance is author-triggered)",
                    run_id=self.run_id, mr_url=pr_url, pr_number=pr_number)
        self._add_event("COMMITTING", "ai-committer",
                        f"branch={branch} pr={pr_number}",
                        {"branch": branch, "pr_number": pr_number, "pr_url": pr_url})
        # Extract the commit SHA from the batch_result string.
        # Format: "Batch commit OK: N file(s) on branch — <sha> (<web_url>)"
        import re as _re_sha
        _sha_match = _re_sha.search(r'— ([0-9a-f]{7,40})\b', batch_result)
        commit_sha = _sha_match.group(1) if _sha_match else ''
        if not commit_sha:
            logger.warning(
                f"[SM {self.run_id}] COMMITTING: could not extract commit SHA from "
                f"batch result — branch may have no committed files. "
                f"batch_result={batch_result!r}"
            )
        self._put_artifact('COMMITTING', {
            'branch':     branch,
            'commit_sha': commit_sha,
            'mr_url':     pr_url or '',
            'pr_number':  pr_number,
        })

        # 2026-07-24: the automatic post-commit governance end-gate call was removed.
        # Governance now runs only when the author triggers it (POST
        # /sdlc/runs/{id}/governance/start → run_endgate_governance_job → this SM's
        # _run_governance_endgate). _run_governance_endgate / _governance_endgate_clear
        # / the resume machinery are all retained and reused by that trigger.

        # Comment on Jira with structured coding output
        try:
            from tools.jira_tools import jira_add_comment
            from agents.sdlc_pipeline import _fmt_coding
            all_files = self.code_output.get("files", [])
            _fmt_code_str = _fmt_coding(all_files, pr_url)
            jira_add_comment(
                self.jira_key,
                f"[AI Coding Agent] Implementation complete.\n\n{_fmt_code_str}\n\nBranch: `{branch}`",
                user_id=self.user_id, user_email=self.user_email,
            )
        except Exception:
            pass

        # Publish inbox notification for engineer PR review
        try:
            from store.inbox_store import publish_inbox_item
            from agents.sdlc_pipeline import _fmt_coding
            all_files = self.code_output.get("files", [])
            _fmt_code_str = _fmt_coding(all_files, pr_url)
            publish_inbox_item(
                user_id="platform",
                type="pr_ready_for_review",
                title=f"PR ready: {self.jira_key}",
                body=_fmt_code_str,
                source_id=self.run_id,
                metadata={
                    "run_id":    self.run_id,
                    "jira_key":  self.jira_key,
                    "pr_number": pr_number,
                    "pr_url":    pr_url,
                    "branch":    branch,
                },
            )
        except Exception:
            pass

        logger.info(f"[SM {self.run_id}] COMMITTING complete → AWAITING_PR_APPROVAL")

        # Notify Teams that PR is ready for review (only for Teams-triggered runs)
        try:
            from agents.sdlc_pipeline import _teams_notify
            _pr_teams_msg = (
                f"🔀 **PR Created** — `{self.jira_key}`\n"
                f"Branch: `{branch}`\n"
            )
            if pr_url:
                _pr_teams_msg += f"[Open PR]({pr_url})\n"
            _pr_teams_msg += "_Awaiting engineer review & approval._"
            _teams_notify(self.run_id, _pr_teams_msg)
        except Exception:
            pass

    # ── Resume: COMMIT_FAILED → re-run commit only ─────────────

    def resume_commit(self) -> None:
        """
        Resume a run suspended at COMMIT_FAILED (or AWAITING_PR_APPROVAL where
        only MR creation failed) by replaying ONLY the COMMITTING phase.

        The generated code is durable in the CODING artifact and the SLT files
        in the SLT artifact, so no earlier stage is re-run. We rehydrate
        self.code_output / self.slt_output from those artifacts, then delegate
        to _phase_commit which is naturally idempotent:
          • branch creation reuses an existing branch (409 → reuse)
          • gitlab_batch_commit flips create↔update if files already exist
          • gitlab_create_mr returns the existing MR via _find_existing_mr

        Idempotent and safe to call repeatedly. Artifact I/O is non-fatal.
        """
        logger.info(f"[SM {self.run_id}] resume_commit — replaying COMMITTING from CODING/SLT artifacts")

        # Rehydrate in-memory state from durable artifacts. Only overwrite when
        # the artifact has data so a live in-memory state machine (rare) isn't
        # clobbered with an empty dict.
        _coding = self._get_artifact("CODING") or {}
        if _coding.get("files"):
            self.code_output = _coding
        _slt = self._get_artifact("SLT") or {}
        if _slt.get("slt_files"):
            self.slt_output = _slt

        # Guard: COMMITTING consumes self.code_output. If the CODING artifact is
        # missing (run predates the artifact store), we cannot resume — fail with
        # an accurate message rather than producing an empty/misleading commit.
        if not (self.code_output.get("files") or self.slt_output.get("slt_files")):
            _msg = (
                "Cannot resume commit: no CODING/SLT artifact found for this run. "
                "Runs whose coding phase executed before the stage-artifact store "
                "was deployed cannot be commit-resumed — re-run the coding phase."
            )
            logger.error(f"[SM {self.run_id}] resume_commit: {_msg}")
            update_run_state(self.run_id, "FAILED", error=_msg)
            return

        self._add_event("COMMITTING", "ai-committer",
                        "Resuming commit from COMMIT_FAILED — re-running branch + atomic commit + MR",
                        {"resume": "commit"})

        # _phase_commit re-resolves the GitLab token, branch, and MR — fully
        # idempotent. It transitions the run to AWAITING_PR_APPROVAL on success
        # or back to COMMIT_FAILED on another transient failure.
        self._phase_commit()

    # ── Phase: AI_ADDRESSING_COMMENTS ─────────────────────────

    def _phase_address_comments(self):
        """
        Fetch all review comments on the open PR, generate fixes with the
        AI Coding Agent, commit them to the same branch, and transition to
        AWAITING_RE_REVIEW.

        Called by address_pr_review_comments() in sdlc_pipeline.py (via job queue).
        """
        logger.info(f"[SM {self.run_id}] AI_ADDRESSING_COMMENTS — fetching PR review comments")
        self._set_state("AI_ADDRESSING_COMMENTS")
        self._set_user_scm_token()

        run = get_run(self.run_id)
        repo      = run.get("repo", self.repo or "")
        pr_number = run.get("pr_number")
        branch    = run.get("branch", f"{_BRANCH_PREFIX}/{self.jira_key.lower()}-ai-impl")

        if not repo or not pr_number:
            logger.error(f"[SM {self.run_id}] address_comments: missing repo/pr_number")
            update_run_state(self.run_id, "FAILED", error="Missing repo or pr_number for comment addressing")
            return

        try:
            from core.config import SCM_PROVIDER as _SCM
            if _SCM == "github":
                from tools.github_tools import (
                    github_get_pr_diff_notes as gitlab_get_mr_diff_notes,
                    github_create_or_update_file as gitlab_create_or_update_file,
                    github_comment_on_pr as gitlab_comment_on_mr,
                )
            else:
                from tools.gitlab_tools import (
                    gitlab_get_mr_diff_notes,
                    gitlab_create_or_update_file,
                    gitlab_comment_on_mr,
                )
            from collections import OrderedDict

            ctx = run.get("context") or {}
            # Structured per-file comments submitted at the PR-approval gate via
            # /request-changes (Step 7/8): [{file, line?, comment}].
            structured = ctx.get("pr_review_file_comments") or []
            whole_run_fb = (ctx.get("pr_review_feedback") or "").strip()

            # Position-aware notes from the ACTUAL MR (preserves new_path/new_line).
            try:
                mr_notes = gitlab_get_mr_diff_notes(repo, int(pr_number))
            except Exception as _ne:
                mr_notes = []
                logger.warning(f"[SM {self.run_id}] gitlab_get_mr_diff_notes failed (non-fatal): {_ne}")

            # 1. Merge both sources into a per-file map (+ a general/whole-MR bucket).
            by_file: "OrderedDict[str, list]" = OrderedDict()
            general: list = []

            def _add(path: str, line, text: str, author: str):
                text = (text or "").strip()
                if not text:
                    return
                if path:
                    by_file.setdefault(path, []).append({"line": line, "text": text, "author": author})
                else:
                    general.append({"text": text, "author": author})

            for n in mr_notes:
                _add(n.get("new_path") or n.get("old_path") or "",
                     n.get("new_line") if n.get("new_line") is not None else n.get("old_line"),
                     n.get("body", ""), n.get("author", "reviewer"))
            for fc in structured:
                _add((fc.get("file") or "").strip(), fc.get("line"), fc.get("comment", ""), "reviewer")
            if whole_run_fb:
                general.append({"text": whole_run_fb, "author": "reviewer"})

            total_comments = sum(len(v) for v in by_file.values()) + len(general)

            # Preserve the short-circuit: nothing to address → straight to re-review.
            if total_comments == 0:
                logger.info(f"[SM {self.run_id}] no review comments — skipping to AWAITING_RE_REVIEW")
                update_run_state(self.run_id, "AWAITING_RE_REVIEW")
                return

            logger.info(
                "[SM] per-file addressing scope", run_id=self.run_id,
                files_with_comments=len(by_file), total_comments=total_comments,
            )

            # 2. Build a PER-FILE-SCOPED instruction block (grouped by file, lines cited)
            #    instead of a flat blob so the coder edits the right file.
            _blocks = []
            for path, items in by_file.items():
                _lines = [f"#### File: `{path}`"]
                for it in items:
                    _loc = f" (line {it['line']})" if it.get("line") is not None else ""
                    _lines.append(f"  - {it['text']}{_loc}")
                _blocks.append("\n".join(_lines))
            if general:
                _gl = ["#### General / whole-MR comments"]
                for it in general:
                    _gl.append(f"  - {it['text']}")
                _blocks.append("\n".join(_gl))
            comments_scoped = "\n\n".join(_blocks)

            # 3. Ask AI to generate code fixes, file-scoped.
            fix_prompt = (
                f"You are an AI Coding Agent. A human reviewer has left comments on a GitLab MR.\n"
                f"The comments are grouped BY FILE below — apply each comment to the file it is\n"
                f"listed under, at the cited line where given. Do not touch unrelated files.\n\n"
                f"Jira: {self.jira_key}\n"
                f"Language: {self.language}\n\n"
                f"Original solution design:\n{json.dumps(self.design, indent=2)}\n\n"
                f"Reviewer comments (grouped by file):\n{comments_scoped}\n\n"
                f"For each file that needs changing, output:\n"
                f"### `path/to/file.ext`\n```lang\n<full updated file content>\n```\n\n"
                f"Address ALL reviewer concerns. Write complete file contents, not diffs."
            )
            fix_raw = self._llm_traced("FIXING", fix_prompt)  # Claude → fallback, never Ollama

            # 3. Extract files from LLM response and commit them
            files_to_fix = _extract_code_files_from_markdown(fix_raw)
            committed = 0
            for f in files_to_fix:
                path    = f.get("path", "")
                content = f.get("content", "")
                if not path or not content:
                    continue
                result = gitlab_create_or_update_file(
                    repo=repo, path=path, content=content,
                    message=f"[{self.jira_key}] Address review comments: {path}",
                    branch=branch,
                )
                if not result.startswith("[Error"):
                    committed += 1
                    logger.info(f"[SM {self.run_id}] fix committed: {path}")
                else:
                    logger.warning(f"[SM {self.run_id}] fix commit failed: {path} → {result}")

            # 4. Post summary comment on PR
            summary = (
                f"**[AiNxt AI]** Addressed {committed} file change(s) based on review comments.\n\n"
                f"Changes committed to branch `{branch}`. Please re-review.\n\n"
                f"_Automated by AiNxt AI Coding Agent_"
            )
            gitlab_comment_on_mr(repo, int(pr_number), summary)
            logger.info(f"[SM {self.run_id}] {committed} fixes committed → AWAITING_RE_REVIEW")

        except Exception as e:
            logger.error("[SM] _phase_address_comments failed", run_id=self.run_id, error=str(e))
            update_run_state(self.run_id, "FAILED", error=str(e))
            return

        update_run_state(self.run_id, "AWAITING_RE_REVIEW", branch=branch, pr_number=pr_number)
        self._add_event(
            "AI_ADDRESSING_COMMENTS", "ai-coding-agent",
            f"Addressed review comments, {committed} file(s) committed",
            {"pr_number": pr_number, "files_committed": committed},
        )
        logger.info(f"[SM {self.run_id}] AI_ADDRESSING_COMMENTS complete → AWAITING_RE_REVIEW")

    # ── Helpers ───────────────────────────────────────────────

    def _set_state(self, to_state: str):
        run = get_run(self.run_id)
        from_state = run["state"] if run else "UNKNOWN"
        # In-flight cancellation: an out-of-band cancel persisted state=CANCELLED.
        # Stop the coding state machine here rather than overwriting CANCELLED and
        # proceeding to commit/MR. Raised before the state update so CANCELLED is
        # preserved; the callers of machine.run() catch SDLCCancelled.
        if from_state == "CANCELLED":
            from store.sdlc_store import SDLCCancelled
            raise SDLCCancelled(self.run_id)
        _now = time.time()
        self._stage_start = _now
        update_run_state(self.run_id, to_state, current_stage=to_state)
        add_run_event(
            self.run_id, from_state, to_state,
            stage=to_state, actor="sdlc-state-machine",
        )
        logger.info(f"[SM {self.run_id}] {from_state} → {to_state}")

    def _add_event(self, stage: str, actor: str, output: str, data: dict = None):
        run = get_run(self.run_id)
        add_run_event(
            self.run_id,
            from_state=run["state"] if run else stage,
            to_state=stage,
            stage=stage,
            actor=actor,
            output=output,
            data=data or {},
        )

    def _put_artifact(self, stage: str, payload: dict, reason: str = None) -> None:
        """Store stage output to in-memory cache and DB. Non-fatal."""
        self._artifact_cache[stage] = payload
        logger.info(f'[SM {self.run_id}] _put_artifact called for stage={stage}')
        try:
            from store.sdlc_artifacts import _store_artifact, compute_input_hash
            from core.model_registry import CLAUDE_PRIMARY_MODEL
            _store_artifact(
                run_id=self.run_id,
                stage=stage,
                payload=payload,
                producer=f'ai:{CLAUDE_PRIMARY_MODEL}',
                input_hash=compute_input_hash(self.run_id, stage),
                created_by=self.user_id or 'system',
                reason=reason,
            )
        except Exception as e:
            logger.warning(f'[SM {self.run_id}] _put_artifact {stage} failed: {e}')

    def _get_artifact(self, stage: str) -> dict:
        """Return cached stage payload, loading from DB if not in memory."""
        if stage in self._artifact_cache:
            return self._artifact_cache[stage]
        try:
            from store.sdlc_artifacts import _load_latest_artifact
            art = _load_latest_artifact(self.run_id, stage)
            if art:
                return art.get('payload') or {}
        except Exception as e:
            logger.warning(f'[SM {self.run_id}] _get_artifact {stage} failed: {e}')
        return {}

    def _clear_review_anchor(self) -> None:
        """Clear the incremental-review anchor once a run is approved. The artifact
        store is append-only (no delete), so this overwrites the anchor with an empty
        blocking-issue set and drops the hot-path cache entry. _get_artifact then reads
        it as "no anchor" (``.get("blocking_issues") or []`` → []), so a later
        re-implement of the SAME run re-anchors fresh instead of reusing a stale set.
        Non-fatal — a failure here only means a benign stale anchor, never a wrong gate."""
        try:
            if (self._get_artifact("REVIEW_ANCHOR") or {}).get("blocking_issues"):
                self._put_artifact(
                    "REVIEW_ANCHOR", {"blocking_issues": []},
                    reason="review approved — clear anchor",
                )
            self._artifact_cache.pop("REVIEW_ANCHOR", None)
        except Exception as _cae:
            logger.warning(f"[SM {self.run_id}] _clear_review_anchor failed (non-fatal): {_cae}")

    def _suspend(self, stage: str, reason: str) -> None:
        """Suspend the pipeline at the given stage with the given reason.

        Normalises non-resumable internal phase names to their nearest valid
        resume target (via _SUSPEND_STAGE_MAP) so the DB record is always
        recoverable through the resume API.  The original stage name is kept
        in the log and suspend reason for diagnostics.

        Logs ERROR for any stage that is neither in the map nor in the known
        valid resume sequences, so drift can't reappear silently after refactors.
        """
        resume_stage = self._SUSPEND_STAGE_MAP.get(stage, stage)
        if resume_stage != stage:
            logger.error(
                f"[SM {self.run_id}] _suspend: non-resumable stage={stage!r} normalised to "
                f"{resume_stage!r}. Fix the _suspend() call site. reason={reason!r}"
            )
        else:
            from store.sdlc_artifacts import stage_sequence_for
            _all_valid = (
                set(stage_sequence_for("feature"))
                | set(stage_sequence_for("governance"))
            )
            if stage not in _all_valid:
                logger.error(
                    f"[SM {self.run_id}] _suspend: stage={stage!r} is not in any known "
                    f"resume sequence and has no normalisation mapping — this run will be "
                    f"unresumable. Add an entry to _SUSPEND_STAGE_MAP."
                )
        logger.warning(f'[SM {self.run_id}] SUSPENDING at stage={resume_stage}: {reason}')
        try:
            update_run_state(
                self.run_id, 'SUSPENDED',
                current_stage=resume_stage,
                context_patch={'suspended_at_stage': resume_stage, 'suspend_reason': reason},
                suspended_at_stage=resume_stage,
            )
        except TypeError:
            update_run_state(
                self.run_id, 'SUSPENDED',
                current_stage=resume_stage,
                context_patch={'suspended_at_stage': resume_stage, 'suspend_reason': reason},
            )
        self._add_event(resume_stage, 'pipeline', f'SUSPENDED: {reason}', {'reason': reason})

    # Step 6(c): coarse issue-category buckets keyed off salient tokens, so an
    # equivalent-but-reworded critical maps to the same category and trips the
    # convergence short-circuit instead of running all 3 rounds.
    _ISSUE_CATEGORIES = (
        ("security",      ("inject", "sql", "xss", "ssrf", "xxe", "traversal", "auth",
                           "credential", "secret", "crypto", "deserial", "pci", "pan",
                           "aadhaar", "cvv", "upi")),
        ("null_safety",   ("null", "npe", "nullpointer", "none type", "optional")),
        ("resource_leak", ("leak", "unclosed", "not closed", "resource", "connection")),
        ("error_handling",("exception", "error handling", "catch", "try", "swallow", "rethrow")),
        ("completeness",  ("todo", "stub", "placeholder", "not implemented", "missing impl",
                           "incomplete")),
        ("wiring",        ("call site", "caller", "callsite", "invocation", "signature",
                           "import", "wire", "reference", "undefined")),
        ("correctness",   ("incorrect", "wrong", "logic", "off-by", "race", "concurren")),
    )

    def _fallback_commit(self, commit_fn, branch: str, committed: int) -> int:
        """
        DEPRECATED — no longer called. Replaced by a hard FAIL when committed==0.
        Keeping definition to avoid AttributeError if called from external code during migration.
        """
        logger.error(
            f"[SM {self.run_id}] _fallback_commit called — this path should be unreachable. "
            "Pipeline should have FAILED at the committed==0 check. Returning 0."
        )
        return 0

    def _fallback_commit_DISABLED(self, commit_fn, branch: str, committed: int) -> int:
        """
        DISABLED. Original four-stage fallback — removed because it committed
        IMPLEMENTATION_NOTES.md (a fake MR with no code). See checklist section 9.

        Stage 1 — Extract files from coder's raw markdown output.
        Stage 2 — Per-file generation from design.code_changes  (bug runs).
        Stage 3 — Per-file generation from analysis files list  (feature runs).
        Stage 4 — Commit IMPLEMENTATION_NOTES.md so the branch has a diff.  ← REMOVED
        """
        # ── Stage 1: markdown extraction from coder's raw output ──────────────
        raw_output = self.code_output.get("raw", "")
        if raw_output:
            md_files = _extract_code_files_from_markdown(raw_output)
            if md_files:
                logger.info(f"[SM {self.run_id}] fallback S1: committing {len(md_files)} files from coder markdown")
                for f in md_files[:8]:
                    result = commit_fn(
                        repo=self.gitlab_repo, path=f["path"],
                        content=f["content"],
                        message=f"[{self.jira_key}] {f['path']}",
                        branch=branch,
                    )
                    if not result.startswith("[Error"):
                        committed += 1
                if committed:
                    return committed

        # ── Stage 2: per-file generation from code_changes (bug runs) ─────────
        code_changes = self.design.get("code_changes", [])
        if code_changes:
            logger.info(f"[SM {self.run_id}] fallback S2: per-file gen from {len(code_changes)} code_changes")
            committed = self._generate_and_commit_files(
                commit_fn, branch,
                [(c.get("file", ""), c.get("change", "")) for c in code_changes[:6]],
            )
            if committed:
                return committed

        # ── Stage 3: per-file generation from analysis.files lists (feature) ──
        def _sp3(x):
            if isinstance(x, str): return x
            if isinstance(x, dict): return x.get("path") or x.get("file") or x.get("name") or ""
            return str(x)
        files_list = [
            _sp3(f)
            for f in (self.analysis.get("files_to_change", []) or []) +
                     (self.analysis.get("new_files_needed", []) or [])
            if _sp3(f)
        ]
        impl_plan = self.design.get("implementation_plan", [])
        plan_text = "\n".join(f"- {s}" for s in impl_plan[:10])
        solution  = self.design.get("solution_approach", "") or plan_text

        if files_list and solution:
            logger.info(f"[SM {self.run_id}] fallback S3: per-file gen from {len(files_list)} analysis files")
            committed = self._generate_and_commit_files(
                commit_fn, branch,
                [(fp, f"Implement changes required by: {solution[:200]}") for fp in files_list[:6]],
            )
            if committed:
                return committed

        # ── Stage 4: last resort — IMPLEMENTATION_NOTES.md ───────────────────
        logger.warning(f"[SM {self.run_id}] fallback S4: committing IMPLEMENTATION_NOTES.md")
        notes  = self._build_implementation_notes()
        result = commit_fn(
            repo=self.gitlab_repo,
            path=f"{_BRANCH_PREFIX}/{self.jira_key}/IMPLEMENTATION_NOTES.md",
            content=notes,
            message=f"[{self.jira_key}] AiNxt implementation notes",
            branch=branch,
        )
        if not result.startswith("[Error"):
            committed = 1
            logger.info(f"[SM {self.run_id}] IMPLEMENTATION_NOTES.md committed")
        else:
            logger.warning(f"[SM {self.run_id}] last-resort commit failed: {result}")
        return committed

    def _generate_and_commit_files(self, commit_fn, branch: str,
                                    file_tasks: list) -> int:
        """
        For each (file_path, change_description) pair: call the LLM to write
        the complete file content, strip any accidental fences, then commit.
        Returns the number of files successfully committed.
        """
        import re as _re
        committed = 0
        for file_path, change_desc in file_tasks:
            file_path   = (file_path or "").strip()
            change_desc = (change_desc or "").strip()
            if not file_path or not change_desc:
                continue

            code_prompt = (
                f"You are an expert {self.language} developer addressing PR review comments.\n\n"
                f"Jira: {self.jira_key} | Repository: {self.repo}\n"
                f"File to update: {file_path}\n"
                f"Change required: {change_desc}\n\n"
                f"Call gitlab_read_file to read the current content of {file_path!r} before making changes.\n\n"
                f"Write the complete, production-ready implementation for this file.\n"
                f"Return ONLY the raw source code — no markdown fences, no prose, no explanation."
            )
            generated = _extract_code_from_markdown(_run_sdlc_agent("sdlc-coding-agent", code_prompt))

            if not generated or len(generated) < 10:
                continue

            result = commit_fn(
                repo=self.gitlab_repo, path=file_path,
                content=generated,
                message=f"[{self.jira_key}] {change_desc[:60]}",
                branch=branch,
            )
            if not result.startswith("[Error"):
                committed += 1
                logger.info(f"[SM {self.run_id}] committed via per-file gen: {file_path}")
            else:
                logger.warning(f"[SM {self.run_id}] per-file commit failed: {file_path} → {result}")
        return committed

    def _build_implementation_notes(self) -> str:
        """Fallback markdown file committed when the AI coder produces no parseable files."""
        design_summary = self.design.get("solution_approach", "") or self.design.get("fix_description", "")
        code_changes   = self.design.get("code_changes", [])
        tests          = self.design.get("tests_to_add", [])
        lines = [
            f"# Implementation Notes — {self.jira_key}",
            "",
            f"> Generated by AiNxt AI Coding Agent",
            "",
            "## Solution Approach",
            design_summary or "_No solution description available_",
            "",
        ]
        if code_changes:
            lines += ["## Code Changes", ""]
            for c in code_changes[:10]:
                lines.append(f"- **{c.get('file', '?')}**: {c.get('change', '')}")
            lines.append("")
        if tests:
            lines += ["## Tests to Add", ""]
            for t in tests[:10]:
                lines.append(f"- {t}")
            lines.append("")
        lines += [
            "## Coding Agent Output",
            "```",
            self.code_output.get("summary", self.code_output.get("raw", "")) or "_No output_",
            "```",
        ]
        return "\n".join(lines)

    def _build_pr_description(self) -> str:
        # Extract solution details. _s() coerces dict/list LLM outputs to plain
        # strings — the designer may return solution_approach/fix_description as
        # a dict, which would otherwise reach "\n".join(lines) raw and raise
        # "sequence item N: expected str instance, dict found".
        design_summary = _s(
            self.design.get("solution_approach", "")
            or self.design.get("fix_description", "")
        )
        impl_summary  = _s(self.code_output.get("summary", ""))
        slt_scenarios = self.slt_output.get("test_scenarios", [])
        impl_files = [f for f in self.code_output.get("files", []) if not f.get("is_test")]
        test_files = [f for f in self.code_output.get("files", []) if f.get("is_test")]
        slt_files  = self.slt_output.get("slt_files", [])

        # Multi-repo: lead with sibling MR links so reviewers see the full
        # scope of the change before reading the design. Manifest follow-up
        # MR (if any) is listed alongside so engineers know to merge it in
        # the same window.
        multi_repo_section: list = []
        if self._sibling_mr_urls or self._manifest_update_url:
            multi_repo_section = [
                "### Multi-repo Run — Related MRs",
                "",
                f"This change spans {len(self._sibling_mr_urls) + 1} repo(s). "
                "Merge the dep MRs **before** this primary so consumers compile against "
                "the new interfaces.",
                "",
            ]
            for repo, url in sorted(self._sibling_mr_urls.items()):
                if url:
                    multi_repo_section.append(f"- `{repo}` → {url}")
            if self._manifest_update_url:
                multi_repo_section.append(
                    f"- Manifest update (.sdlc.yml) → {self._manifest_update_url}"
                )
            multi_repo_section.append("")

        code_changes = self.design.get("code_changes", [])
        regression_risk = _s(self.design.get("regression_risk", ""))
        verification    = self.design.get("verification_steps", [])

        lines = [
            f"## 🤖 AiNxt AI Implementation — `{self.jira_key}`",
            "",
        ]
        # Insert multi-repo cross-link section near the top of the body (before
        # the "What Changed" summary) so reviewers see the full scope first.
        lines += multi_repo_section
        lines += [
            "### What Changed",
            design_summary or impl_summary or "_See implementation plan below_",
            "",
        ]

        if code_changes:
            lines += ["### Approved Solution Design", ""]
            for c in code_changes[:10]:
                if isinstance(c, dict):
                    lines.append(f"- **`{_s(c.get('file', '?'))}`** — {_s(c.get('change', ''))}")
                else:
                    lines.append(f"- {_s(c)}")
            lines.append("")

        if impl_files:
            lines += ["### Files Modified / Created / Deleted", ""]
            for f in impl_files[:10]:
                _fp = _s(f.get('path', f) if isinstance(f, dict) else f)
                _tag = " _(deleted)_" if isinstance(f, dict) and f.get("deleted") else ""
                lines.append(f"- `{_fp}`{_tag}")
            lines.append("")

        if test_files or slt_files:
            lines += ["### Tests Added", ""]
            for f in test_files[:4]:
                lines.append(f"- `{_s(f.get('path', f) if isinstance(f, dict) else f)}` _(unit test)_")
            for f in slt_files[:4]:
                lines.append(f"- `{_s(f.get('path', f) if isinstance(f, dict) else f)}` _(SLT — API-level)_")
            lines.append("")

        if slt_scenarios:
            lines += ["### Test Scenarios Covered", ""]
            for s in slt_scenarios[:8]:
                lines.append(f"- {_s(s)}")
            lines.append("")

        if verification:
            lines += ["### Verification Steps", ""]
            for v in verification[:5]:
                lines.append(f"1. {_s(v)}")
            lines.append("")

        if regression_risk:
            lines += [f"**Regression Risk:** {regression_risk}", ""]

        # Add links to GitLab issue and Confluence doc when available
        run_ctx = (get_run(self.run_id) or {}).get("context") or {}
        gl_issue_url   = run_ctx.get("gitlab_issue_url", "")
        confluence_url = run_ctx.get("confluence_url", "")
        if gl_issue_url or confluence_url:
            lines += ["### References", ""]
            if gl_issue_url:
                lines.append(f"- [GitLab Issue]({gl_issue_url})")
            if confluence_url:
                lines.append(f"- [Confluence Design Doc]({confluence_url})")
            lines.append("")

        lines += [
            "---",
            f"_Generated by **AiNxt AI Coding Agent** · {_CLAUDE_PRIMARY_MODEL}_",
            "",
            f"<!-- {_RUN_ID_PREFIX}: {self.run_id} -->",
        ]

        # Prepend any waiver banners (PCI/DSS compliance — must appear before all other content)
        try:
            _waiver_banners = run_ctx.get("waiver_banners") or []
            if _waiver_banners:
                banner_lines = ["## ⚠ Gate Waivers (PCI/DSS Audit Required)", ""]
                for b in _waiver_banners:
                    banner_lines.append(f"> {b}")
                banner_lines += ["", "---", ""]
                lines = banner_lines + lines
        except Exception:
            pass

        return _sanitize_for_api("\n".join(lines))
