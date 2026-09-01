# SPDX-License-Identifier: Apache-2.0
# ============================================================
# SDLC PATCH ENGINE — Developer Loop Engine
# Surgical patch generation for existing file modifications.
#
# For existing-file changes (not new files):
#   1. Generate structured patch JSON (only changed parts, not full file)
#   2. Apply patch to existing content (function-boundary replacement)
#   3. Validate via sandbox compile — feed errors back into next attempt
#   4. Up to MAX_PATCH_ATTEMPTS before falling back to full-gen in state machine
#
# Patch JSON schema produced by LLM:
#   {
#     "reasoning":          "one-line explanation",
#     "added_imports":      ["import X;"],
#     "changed_functions":  [{"name": "fn", "complete_code": "...full method..."}],
#     "new_functions":      [{"insert_after": "fn|end_of_class|end_of_file", "complete_code": "..."}],
#     "changed_fields":     [{"old_line": "...", "new_line": "..."}],
#     "changed_lines":      [{"old_text": "...", "new_text": "..."}]
#   }
# ============================================================

import re
from typing import Optional

from agents.compliance_engine import is_compliance_block
from core.logger import logger, bind_context
from core.model_registry import sdlc_stage_hint

MAX_PATCH_ATTEMPTS = 3


# Output-format instructions for the patch LLM. Kept as a plain (non-f) string so the
# example code can contain literal braces without f-string escaping. LANG / FILEPATH
# tokens are substituted per call. The marker syntax matches `gitlab_apply_patch`
# (tools/gitlab_tools.py) and the agentic coding prompt, so the format is consistent
# across both coding paths.
_SEARCH_REPLACE_INSTRUCTIONS = """=== OUTPUT FORMAT — SEARCH/REPLACE BLOCKS ===
Return ONE OR MORE search/replace blocks. Output NOTHING else — no JSON, no prose,
no markdown fences. Each block has this EXACT shape:

<<<<<<< SEARCH
<lines copied VERBATIM from the existing file above>
=======
<the replacement lines>
>>>>>>> REPLACE

RULES:
- The SEARCH section MUST be copied EXACTLY from the existing file — every character,
  including leading indentation. It is matched against the file; if it does not match,
  the edit is rejected.
- Keep each SEARCH block SMALL and UNIQUE: just enough lines around the change to be
  unambiguous. Do NOT paste the whole file or whole methods unless they all change.
- The REPLACE section is the FULL replacement for those lines — no "...", no TODO,
  no placeholders. It must be valid LANG on its own.
- Emit a SEPARATE block for each distinct change. Blocks are applied in order.
- ONLY use identifiers that already exist in THIS file (FILEPATH). Do NOT copy code or
  imports from the related-code section.
- Do NOT rename or change the type of a field/variable that is used elsewhere in this
  file — its other usages would break. Add a NEW field/variable instead.

HOW TO ADD CODE (there is no separate insert syntax — anchor on existing text):
- ADD AN IMPORT: SEARCH the last existing import line; REPLACE it with itself plus the
  new import on the next line.
- ADD A NEW METHOD/FUNCTION: SEARCH the closing line of an existing nearby method;
  REPLACE it with that same closing line plus the new method after it.

WORKED EXAMPLE:
<<<<<<< SEARCH
    total = price * qty
=======
    total = price * qty
    total = apply_discount(total, qty)
>>>>>>> REPLACE"""


def _count_outcomes(outcomes: list[dict]) -> dict:
    """Roll up per-attempt outcome records into {outcome: count} for telemetry."""
    counts: dict[str, int] = {}
    for o in outcomes:
        k = o.get("outcome", "unknown")
        counts[k] = counts.get(k, 0) + 1
    return counts


def _fuzzy_ws_replace(content: str, old_text: str, new_text: str) -> tuple[str, bool]:
    """Fallback when exact changed_lines match fails.
    Compares each line stripped of leading/trailing whitespace. Works for all languages —
    handles column-alignment drift in Python, Java field declarations, etc.
    Returns (new_content, matched)."""
    old_lines = old_text.splitlines()
    if not old_lines:
        return content, False
    content_lines = content.splitlines()
    window = len(old_lines)
    norm_old = [ln.strip() for ln in old_lines]
    for i in range(len(content_lines) - window + 1):
        if [ln.strip() for ln in content_lines[i : i + window]] == norm_old:
            content_lines[i : i + window] = new_text.splitlines()
            return "\n".join(content_lines), True
    return content, False


# ── Localization helpers for oversized files (E) ──────────────────────────────

def _extract_anchor_identifiers(text: str) -> set:
    """Likely anchor symbol names from a change description / solution text:
    backtick-quoted names, `name(` call/def forms, and CamelCase types. Used to
    locate the impacted region(s) in a large file with NO extra LLM call."""
    ids: set = set()
    if not text:
        return ids
    for m in re.finditer(r"`([^`]+)`", text):
        for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", m.group(1)):
            ids.add(tok)
    for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text):
        ids.add(m.group(1))
    for m in re.finditer(r"\b([A-Z][A-Za-z0-9]{2,})\b", text):
        ids.add(m.group(1))
    _STOP = {"this", "that", "with", "from", "into", "code", "file", "test", "tests",
             "function", "method", "class", "True", "False", "None", "json", "JSON",
             "Column", "import", "return", "should", "added", "after", "before"}
    return {i for i in ids if len(i) >= 4 and i not in _STOP}


_DECL_RE = re.compile(
    r"^\s*(?:export\s+|default\s+|public\s+|private\s+|protected\s+|static\s+|final\s+|"
    r"abstract\s+|async\s+)*"
    r"(?:def|class|function|func|fn|interface|enum|struct|trait|impl|type|module)\b"
)


def _file_outline(lines: list) -> list:
    """[(line_idx, header_text)] for declaration-like lines — a compact map of a
    large file, shown to the Tier-2 locator. Language-agnostic best effort."""
    out = []
    for i, ln in enumerate(lines):
        if _DECL_RE.search(ln):
            out.append((i, ln.strip()[:120]))
    return out


def _target_lines(lines: list, identifiers: set) -> set:
    """Line indices containing any anchor identifier."""
    if not identifiers:
        return set()
    tgt = set()
    for i, ln in enumerate(lines):
        for ident in identifiers:
            if ident in ln:
                tgt.add(i)
                break
    return tgt


def _render_windows(lines: list, target_idxs, pad: int, header_lines: int,
                    tail_lines: int, cap: int) -> tuple:
    """
    Render a localized VIEW of a large file: the import/header block, a tail window
    (so end-of-file appends can be anchored), and ±pad-line windows around each
    target line. Non-contiguous windows are separated by '... N lines omitted ...'.
    NO line-number prefixes — shown lines are verbatim so SEARCH blocks match.
    Greedy-capped to `cap` chars. Returns (view, note).
    """
    n = len(lines)
    if n == 0:
        return "", ""
    windows = []
    if header_lines > 0:
        windows.append((0, min(header_lines - 1, n - 1)))
    if tail_lines > 0:
        windows.append((max(0, n - tail_lines), n - 1))
    for t in target_idxs:
        windows.append((max(0, t - pad), min(n - 1, t + pad)))
    windows.sort()
    merged = []
    for s, e in windows:
        if merged and s <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    parts, prev_end, used = [], -1, 0
    for s, e in merged:
        seg = "\n".join(lines[s:e + 1])
        if used + len(seg) > cap and parts:
            break
        if s > prev_end + 1:
            parts.append(f"... ({s - (prev_end + 1)} lines omitted) ...")
        parts.append(seg)
        used += len(seg)
        prev_end = e
    if 0 <= prev_end < n - 1:
        parts.append(f"... ({n - 1 - prev_end} lines omitted) ...")
    view = "\n".join(parts)
    note = (
        f"\n[LARGE FILE — {n} lines total. You are shown ONLY the regions most likely "
        f"relevant to this change; gaps are marked '... N lines omitted ...'. Anchor every "
        f"SEARCH block on lines VISIBLE above — never on omitted lines. The file continues "
        f"beyond the visible text wherever an omission marker is shown.]\n"
    )
    return view, note


# Compile commands for single-file sandbox validation.
# Mirrors _SANDBOX_COMPILE_COMMANDS in sdlc_state_machine.py — keep in sync if that changes.
# Key differences from naive compile:
#   Python: ast.parse instead of py_compile — py_compile fails on project-relative imports
#           because only the single patched file is mounted at /sandbox, not the full project.
#   TypeScript/JSX/TSX: check_jsx.js (baked into the image) handles generics and decorators
#           that node --check rejects.
#   Go: gofmt -e for syntax; `go build ./...` requires the full module tree.
_COMPILE_COMMANDS: dict[str, str] = {
    "python":     "python3 -c \"import ast, sys; ast.parse(open('/sandbox/main.py').read()); print('OK')\"",
    "py":         "python3 -c \"import ast, sys; ast.parse(open('/sandbox/main.py').read()); print('OK')\"",
    "javascript": "node --check /sandbox/main.js",
    "js":         "node --check /sandbox/main.js",
    "typescript": "node /usr/local/bin/check_jsx.js /sandbox/main.ts 2>&1",
    "ts":         "node /usr/local/bin/check_jsx.js /sandbox/main.ts 2>&1",
    "jsx":        "node /usr/local/bin/check_jsx.js /sandbox/main.jsx 2>&1",
    "tsx":        "node /usr/local/bin/check_jsx.js /sandbox/main.tsx 2>&1",
    "react":      "node /usr/local/bin/check_jsx.js /sandbox/main.jsx 2>&1",
    "angular":    "node /usr/local/bin/check_jsx.js /sandbox/main.ts 2>&1",
    "vue":        "node /usr/local/bin/check_vue.js /sandbox/main.vue 2>&1",
    "java": (
        "sh -c 'DEPS=$(find /workspace/target/dependency -name \"*.jar\" 2>/dev/null"
        " | tr \"\\n\" \":\" | sed \"s/:$//\");"
        " CLS=/workspace/target/classes;"
        " CP=\"${DEPS}:${CLS}\";"
        " javac -cp \"${CP}\" /sandbox/Main.java 2>&1; exit $?'"
    ),
    "kotlin": "sh -c 'kotlinc /sandbox/Main.kt -include-runtime -d /sandbox/out.jar 2>&1; exit $?'",
    "kt":     "sh -c 'kotlinc /sandbox/Main.kt -include-runtime -d /sandbox/out.jar 2>&1; exit $?'",
    "scala":  "scala-cli compile /sandbox/Main.scala 2>&1",
    "go":     "gofmt -e /sandbox/main.go",
    "golang": "gofmt -e /sandbox/main.go",
    "rust":   "sh -c 'rustc --edition 2021 --crate-type lib /sandbox/main.rs 2>&1; exit $?'",
    "rs":     "sh -c 'rustc --edition 2021 --crate-type lib /sandbox/main.rs 2>&1; exit $?'",
    "csharp": "sh -c 'dotnet-script /sandbox/Main.cs 2>&1; exit $?'",
    "cs":     "sh -c 'dotnet-script /sandbox/Main.cs 2>&1; exit $?'",
    "dotnet": "sh -c 'dotnet-script /sandbox/Main.cs 2>&1; exit $?'",
    "ruby":   "ruby -c /sandbox/main.rb 2>&1",
    "rb":     "ruby -c /sandbox/main.rb 2>&1",
    "php":    "php -l /sandbox/main.php 2>&1",
    "cpp":    "sh -c 'g++ -fsyntax-only /sandbox/main.cpp 2>&1; exit $?'",
    "c":      "sh -c 'gcc -fsyntax-only /sandbox/main.c 2>&1; exit $?'",
    "swift":  "sh -c 'swiftc -parse /sandbox/main.swift 2>&1; exit $?'",
    "bash":   "bash -n /sandbox/main.sh",
    "shell":  "sh -n /sandbox/main.sh",
    "sh":     "sh -n /sandbox/main.sh",
}

# Language → sandbox filename. Single-file validation writes one file at a time,
# so we always use the canonical sandbox name (e.g. main.py) and the command above
# reads from that same path. Mirrors _SANDBOX_FILENAMES in sdlc_state_machine.py.
_SANDBOX_FILENAMES: dict[str, str] = {
    "python":     "main.py",
    "py":         "main.py",
    "javascript": "main.js",
    "js":         "main.js",
    "typescript": "main.ts",
    "ts":         "main.ts",
    "jsx":        "main.jsx",
    "tsx":        "main.tsx",
    "react":      "main.jsx",
    "angular":    "main.ts",
    "vue":        "main.vue",
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


class PatchEngine:
    """
    Developer-loop patch engine for existing file modifications.
    Used by _run_coder() before falling back to full-file generation.
    """

    # ── Public entry point ────────────────────────────────────────────────────

    def run_patch_loop(
        self,
        *,
        path: str,
        existing_content: str,
        desc: str,
        solution_text: str,
        dep_block: str = "",
        rag_context: str = "",
        cs_block: str = "",
        prior_block: str = "",
        language: str,
        jira_key: str,
        repo: str,
        sandbox_image: Optional[str],
        run_id: str,
        max_attempts: Optional[int] = None,
        model_hint: Optional[str] = None,
    ) -> dict:
        """
        Developer loop: generate patch → apply → validate → feed errors back.

        max_attempts caps the retry budget (default MAX_PATCH_ATTEMPTS=3). Callers
        pass a lower value for low-value work — e.g. FIXING passes 1 for "broadcast"
        issues that could not be localized to a specific file, to avoid spending 3
        attempts on collateral files.

        model_hint selects the patch-generation model tier; defaults to the
        configurable "coder" stage (sdlc_stage_hint("coder") → Sonnet by default,
        overridable via SDLC_MODEL_CODER). FIXING passes the "fixer" stage hint.

        Returns:
            {
                "success":  bool,
                "content":  str,      # patched content, or original on failure
                "attempts": int,
                "error":    str | None,
                "method":   "patch" | "failed",
            }
        """
        bind_context(correlation_id=run_id, pipeline_stage="sdlc_patch_engine")

        # Guard: existing file content must not be the compliance-block sentinel.
        # If the file was read through a compliance-gated gateway that blocked the
        # response, treating the sentinel string as real source would corrupt the
        # file. Signal failure using the same shape _patch_attempts returns on a
        # total failure so callers fall back to their existing recovery path.
        if is_compliance_block(existing_content):
            logger.error(
                "[PATCH] compliance-block sentinel detected — dropping file",
                file=path,
                action="dropped",
            )
            return {
                "success":        False,
                "content":        existing_content,
                "attempts":       0,
                "error":          "compliance-block sentinel in existing_content — file dropped",
                "method":         "failed",
                "outcomes":       [],
                "outcome_counts": {},
            }

        _budget = max(1, min(int(max_attempts or MAX_PATCH_ATTEMPTS), MAX_PATCH_ATTEMPTS))
        _hint   = model_hint or sdlc_stage_hint("coder")

        import os as _os_pe
        _cap = int(_os_pe.getenv("SDLC_PATCH_FILE_CHARS", "120000"))

        _common = dict(
            path=path, existing_content=existing_content, desc=desc,
            solution_text=solution_text, dep_block=dep_block, rag_context=rag_context,
            cs_block=cs_block, prior_block=prior_block, language=language,
            jira_key=jira_key, run_id=run_id, sandbox_image=sandbox_image,
            model_hint=_hint,
        )

        # Small/medium files: show the whole file, single attempt-loop (unchanged).
        if len(existing_content) <= _cap:
            return self._patch_attempts(**_common, file_view=None, file_view_note="",
                                        max_attempts=_budget)

        # Oversized files (E): the model can't see the whole file, so localize the
        # region(s) to change and patch those — never head/tail-blind, never
        # full-regen. Escalates ONLY on apply_miss (region not shown). A capped
        # budget (FIXING broadcast files, max=1) runs Tier 1 only (no escalation),
        # preserving the D cost-narrowing.
        return self._run_patch_large(**_common, max_attempts=_budget, cap=_cap)

    # ── Inner attempt loop (operates on a fixed view of the file) ──────────────

    def _patch_attempts(
        self, *, path, existing_content, desc, solution_text, dep_block, rag_context,
        cs_block, prior_block, language, jira_key, run_id, sandbox_image,
        file_view, file_view_note, max_attempts, model_hint="complex",
    ) -> dict:
        """
        One generate→parse→apply→validate retry loop against a FIXED view of the file.
        `file_view` (when not None) is the slice shown in the prompt; apply and compile
        validation always run against the FULL `existing_content`.
        """
        _max_attempts = max(1, int(max_attempts))
        error_feedback = ""
        last_error     = ""
        outcomes: list[dict] = []   # W0: per-attempt outcome tags

        def _record(outcome: str, detail: str = "") -> None:
            outcomes.append({"attempt": attempt, "outcome": outcome,
                             "language": language, "detail": detail[:200]})

        for attempt in range(1, _max_attempts + 1):
            logger.info(f"[PE {run_id}] {path} — patch attempt {attempt}/{_max_attempts}")

            patch_prompt = self._build_patch_prompt(
                path=path, existing_content=existing_content, desc=desc,
                solution_text=solution_text, dep_block=dep_block,
                rag_context=rag_context, cs_block=cs_block, prior_block=prior_block,
                language=language, jira_key=jira_key, error_feedback=error_feedback,
                file_view=file_view, file_view_note=file_view_note,
            )
            try:
                from models.model_router import model_router
                raw_patch = model_router.generate(patch_prompt, model_hint=model_hint)
            except Exception as _e:
                logger.error(f"[PE {run_id}] Patch LLM call failed: {_e}")
                _record("llm_error", str(_e))
                break

            # Guard: if the gateway returned a compliance-block sentinel instead of
            # real generated content, do NOT parse or apply it — treat it the same
            # as an LLM call failure (break out of the retry loop so callers fall
            # back to full-file generation or signal a patch failure).
            if is_compliance_block(raw_patch):
                logger.error(
                    "[PATCH] compliance-block sentinel detected — dropping file",
                    file=path,
                    action="dropped",
                )
                _record("llm_error", "compliance-block sentinel in generated patch")
                break

            edits = _parse_search_replace_blocks(raw_patch)
            if not edits:
                logger.warning(f"[PE {run_id}] No search/replace blocks parsed on attempt {attempt}")
                _record("parse_fail")
                error_feedback = (
                    "Previous attempt produced no valid SEARCH/REPLACE blocks. "
                    "Return one or more blocks EXACTLY in this form and nothing else "
                    "(no JSON, no prose, no markdown fences):\n"
                    "<<<<<<< SEARCH\n<exact existing lines>\n=======\n<replacement>\n>>>>>>> REPLACE"
                )
                continue

            logger.info(f"[PE {run_id}] Parsed {len(edits)} search/replace block(s)")

            patched_content, apply_warnings = self._apply_search_replace(existing_content, edits, language)
            if apply_warnings:
                logger.warning(f"[PE {run_id}] Patch apply warnings: {apply_warnings}")

            if not patched_content or patched_content == existing_content:
                _record("apply_miss", "; ".join(apply_warnings))
                error_feedback = (
                    f"No SEARCH block matched the file (attempt {attempt}). "
                    f"Details: {'; '.join(apply_warnings) or 'none'}. "
                    f"Copy each SEARCH block VERBATIM from the existing file shown above — "
                    f"every character including indentation must match."
                )
                continue

            compile_ok, compile_error = self._validate(
                content=patched_content, path=path, language=language,
                sandbox_image=sandbox_image, run_id=run_id,
                solution_text=solution_text, desc=desc,
            )

            if compile_ok:
                logger.info(f"[PE {run_id}] Patch validated for {path} on attempt {attempt}")
                _record("success")
                # ── Gap-fix: eval_code_quality (fire-and-forget) ──────────────
                # Now that the patch compiled successfully, grade the generated
                # code for security issues (hardcoded secrets, SQL injection,
                # OWASP Top 10) in a background thread so the patch loop is
                # never slowed down.
                try:
                    import threading as _pe_threading
                    _pe_code    = patched_content
                    _pe_desc    = desc
                    _pe_run_id  = run_id
                    _pe_lang    = language
                    def _run_code_eval():
                        try:
                            from core.evals import eval_engine as _ee
                            _ee.eval_code_quality(
                                question=_pe_desc,
                                code=_pe_code[:1500],
                                run_id=_pe_run_id,
                                repo_ctx={"language": _pe_lang},
                            )
                        except Exception as _ce:
                            logger.debug(f"[PE] eval_code_quality failed (non-critical): {_ce}")
                    _pe_threading.Thread(
                        target=_run_code_eval, daemon=True, name="eval-code-quality"
                    ).start()
                except Exception:
                    pass
                return {
                    "success":       True,
                    "content":       patched_content,
                    "attempts":      attempt,
                    "error":         None,
                    "method":        "patch",
                    "outcomes":      outcomes,
                    "outcome_counts": _count_outcomes(outcomes),
                }

            last_error = compile_error
            _err_tail  = _extract_error_tail(compile_error)
            _record("compile_fail", _err_tail)
            error_feedback = (
                f"Attempt {attempt} applied but failed to compile:\n"
                f"```\n{_err_tail}\n```\n"
                f"Your REPLACE content introduced a {language} syntax error. "
                f"Return corrected SEARCH/REPLACE block(s) that fix it — keep all other edits intact."
            )
            logger.warning(f"[PE {run_id}] Patch compile failed (attempt {attempt}): {_err_tail[:1000]}")

        return {
            "success":        False,
            "content":        existing_content,
            "attempts":       _max_attempts,
            "error":          last_error,
            "method":         "failed",
            "outcomes":       outcomes,
            "outcome_counts": _count_outcomes(outcomes),
        }

    # ── Oversized-file orchestration (E): localize → patch → escalate ──────────

    def _run_patch_large(
        self, *, path, existing_content, desc, solution_text, dep_block, rag_context,
        cs_block, prior_block, language, jira_key, run_id, sandbox_image,
        max_attempts, cap, model_hint="complex",
    ) -> dict:
        """
        For files larger than the prompt cap. Shows the model only the localized
        region(s) (apply/validate still run on the full file). Tiers escalate ONLY
        on apply_miss; a capped budget (max_attempts==1) runs Tier 1 only.
        """
        lines = existing_content.split("\n")
        _PAD, _PAD_WIDE, _HEADER, _TAIL = 40, 90, 40, 30
        agg_outcomes: list[dict] = []
        agg_attempts = 0

        def _attempt(view, note, budget):
            nonlocal agg_attempts
            r = self._patch_attempts(
                path=path, existing_content=existing_content, desc=desc,
                solution_text=solution_text, dep_block=dep_block, rag_context=rag_context,
                cs_block=cs_block, prior_block=prior_block, language=language,
                jira_key=jira_key, run_id=run_id, sandbox_image=sandbox_image,
                file_view=view, file_view_note=note, max_attempts=budget,
                model_hint=model_hint,
            )
            agg_attempts += r.get("attempts", 0)
            agg_outcomes.extend(r.get("outcomes", []))
            return r

        def _finalize(r, method=None):
            out = {**r, "attempts": agg_attempts, "outcomes": agg_outcomes,
                   "outcome_counts": _count_outcomes(agg_outcomes)}
            if method:
                out["method"] = method
            return out

        def _missed(r):
            return (not r.get("success")) and any(
                o.get("outcome") == "apply_miss" for o in r.get("outcomes", []))

        # ── Tier 1: mechanical localization (no extra LLM call) ──
        ids = _extract_anchor_identifiers(f"{desc}\n{solution_text}")
        tgt = _target_lines(lines, ids)
        view, note = _render_windows(lines, tgt, pad=_PAD, header_lines=_HEADER,
                                     tail_lines=_TAIL, cap=cap)
        logger.info(
            f"[PE {run_id}] {path} LARGE ({len(existing_content)} chars) — Tier 1 mechanical: "
            f"{len(ids)} anchor id(s), {len(tgt)} target line(s)"
        )
        res = _attempt(view, note, min(MAX_PATCH_ATTEMPTS, max_attempts))
        # Stop here if: success, capped budget (no escalation), or failure was NOT
        # a localization miss (compile_fail means the region WAS shown).
        if res.get("success") or max_attempts <= 1 or not _missed(res):
            return _finalize(res)

        # ── Tier 2: Haiku locate (+1 cheap call) ──
        regions = self._llm_locate_regions(lines, desc, solution_text, language, run_id)
        if not regions:
            logger.error(
                f"[PE {run_id}] {path} — Haiku locate returned no regions; BLOCKING (large file)"
            )
            return _finalize(res, method="failed_large_localize")

        logger.info(f"[PE {run_id}] {path} — Tier 2 Haiku locate → lines {regions[:10]}")
        view, note = _render_windows(lines, set(regions), pad=_PAD, header_lines=_HEADER,
                                     tail_lines=_TAIL, cap=cap)
        res = _attempt(view, note, 2)
        if res.get("success") or not _missed(res):
            return _finalize(res)

        # ── Tier 3: expand windows + neighbors, final single attempt ──
        logger.info(f"[PE {run_id}] {path} — Tier 3 expand (wider windows)")
        view, note = _render_windows(lines, set(regions) | tgt, pad=_PAD_WIDE,
                                     header_lines=_HEADER, tail_lines=_TAIL, cap=cap)
        res = _attempt(view, note, 1)
        if res.get("success"):
            return _finalize(res)
        logger.error(f"[PE {run_id}] {path} — localization failed across tiers; BLOCKING (large file)")
        return _finalize(res, method="failed_large_localize")

    # ── Region locator (Tier 2, Haiku) ─────────────────────────────────────────

    def _llm_locate_regions(self, lines: list, desc: str, solution_text: str,
                            language: str, run_id: str) -> list:
        """
        Tier 2 localization: show Haiku the file's declaration outline (cheap — just
        signatures) and ask which line numbers must change. Returns a list of 0-based
        line indices, or [] on any failure (caller then blocks).
        """
        outline = _file_outline(lines)
        if not outline:
            return []
        outline_txt = "\n".join(f"L{idx}: {hdr}" for idx, hdr in outline)[:20_000]
        prompt = (
            f"You are locating which regions of a large {language} file must change.\n\n"
            f"=== TASK ===\n{desc[:2000]}\n\n"
            f"=== SOLUTION CONTEXT ===\n{(solution_text or '')[:1500]}\n\n"
            f"=== FILE DECLARATION OUTLINE (line_number: header) ===\n{outline_txt}\n\n"
            f"Return ONLY a JSON array of the L<line_number> values whose definitions must be "
            f"read or changed for this task (include the nearest declaration above any insertion "
            f"point). Example: [12, 88]. No prose, no other text."
        )
        try:
            import json as _json
            from models.model_router import model_router
            raw = model_router.generate(prompt, model_hint=sdlc_stage_hint("locate")) or ""
            m = re.search(r"\[[\d,\s]*\]", raw)
            if not m:
                return []
            nums = _json.loads(m.group(0))
            return [int(x) for x in nums
                    if isinstance(x, (int, float)) and 0 <= int(x) < len(lines)]
        except Exception as _e:
            logger.warning(f"[PE {run_id}] Haiku locate failed: {_e}")
            return []

    # ── Patch prompt ──────────────────────────────────────────────────────────

    def _build_patch_prompt(
        self, *, path, existing_content, desc, solution_text, dep_block,
        rag_context, cs_block, prior_block, language, jira_key, error_feedback="",
        file_view=None, file_view_note=None,
    ) -> str:
        _sol = solution_text[:3_000] + ("\n[...truncated...]" if len(solution_text) > 3_000 else "")
        _dep = f"\n{dep_block}\n" if dep_block else ""
        _rag = f"\n=== RELATED CODE FROM CODEBASE ===\n{rag_context}\n" if rag_context else ""
        _cs  = f"\n{cs_block}\n" if cs_block else ""
        _pri = f"\n{prior_block}\n" if prior_block else ""
        _err = (
            f"\n=== PREVIOUS ATTEMPT ERRORS — FIX THESE ===\n{error_feedback}\n"
        ) if error_feedback else ""

        # E: when the orchestrator supplies a localized view (oversized file), show
        # exactly that — apply/validate still run on the full file. Otherwise fall
        # back to W3 behaviour: whole file under the cap, head+tail only if a caller
        # invokes this directly on an oversized file without a view.
        import os as _os_w3
        _FILE_LIMIT = int(_os_w3.getenv("SDLC_PATCH_FILE_CHARS", "120000"))
        if file_view is not None:
            _file_body  = file_view
            _trunc_note = file_view_note or ""
        elif len(existing_content) <= _FILE_LIMIT:
            _file_body  = existing_content
            _trunc_note = ""
        else:
            _head_chars = int(_FILE_LIMIT * 0.7)
            _tail_chars = _FILE_LIMIT - _head_chars
            _head       = existing_content[:_head_chars]
            _tail       = existing_content[-_tail_chars:]
            _elided     = len(existing_content) - _head_chars - _tail_chars
            _file_body  = (
                f"{_head}\n"
                f"\n[... {_elided} chars elided from the MIDDLE of the file ...]\n\n"
                f"{_tail}"
            )
            _trunc_note = (
                f"\n[FILE IS LARGE ({len(existing_content)} chars). You are shown the "
                f"first {_head_chars} and last {_tail_chars} chars; the middle is elided. "
                f"Anchor your edits ONLY on text visible above — do not reference elided "
                f"lines. The file does NOT end where the visible portion ends.]\n"
            )

        _instructions = (
            _SEARCH_REPLACE_INSTRUCTIONS
            .replace("LANG", language)
            .replace("FILEPATH", path)
        )
        return f"""You are a senior {language} engineer making a surgical targeted change to an existing file.

=== ASSIGNMENT ===
Jira:   {jira_key}
File:   {path}
Change: {desc}

=== APPROVED SOLUTION DESIGN ===
{_sol}
{_cs}{_dep}{_pri}{_rag}
=== EXISTING FILE (read-only — patch only what must change) ===
```{language}
{_file_body}
```
{_trunc_note}{_err}
""" + _instructions

    # ── Patch application (SEARCH/REPLACE) ─────────────────────────────────────

    def _apply_search_replace(
        self, content: str, edits: list, language: str
    ) -> tuple[str, list[str]]:
        """
        Apply ordered (search, replace) blocks to existing content.

        Two-tier matching — BOTH tiers match the FULL search block, so the applier
        can never silently edit the wrong region:
          1. exact, indentation-preserving substring match
          2. whitespace-normalized full-block match (_fuzzy_ws_replace) — tolerates
             leading-indent / column-alignment drift

        A block that matches neither is reported as a warning and left unapplied; the
        caller feeds that back for the next attempt. (A clean miss is recoverable via
        retry + the full-file fallback; a wrong silent edit would not be — so we never
        guess with looser anchor matching.)

        Returns (patched_content, warnings).
        """
        warnings: list[str] = []
        for idx, (search, replace) in enumerate(edits, start=1):
            # Trim only wrapping blank lines — preserve internal indentation, which is
            # often the only thing disambiguating one line from a similar neighbour.
            s = search.strip("\n")
            r = replace.strip("\n")
            if not s.strip():
                warnings.append(f"block #{idx}: empty SEARCH — skipped")
                continue

            # Tier 1: exact (indentation preserved)
            cnt = content.count(s)
            if cnt == 1:
                content = content.replace(s, r, 1)
                continue
            if cnt > 1:
                # Ambiguous: refuse rather than silently patch the first (possibly
                # wrong) occurrence. Surfaces as an apply warning → the retry loop
                # asks for a more specific, unique SEARCH block.
                warnings.append(
                    f"block #{idx}: SEARCH matched {cnt}x — ambiguous, not applied. "
                    f"Add surrounding lines so the SEARCH block is unique."
                )
                continue

            # Tier 2: whitespace-normalized full-block match
            content, matched = _fuzzy_ws_replace(content, s, r)
            if matched:
                continue

            # Miss — recoverable: report and let the retry loop correct it.
            _first = next((ln for ln in s.splitlines() if ln.strip()), s)
            warnings.append(f"block #{idx}: SEARCH not found near '{_first.strip()[:80]}'")

        return content, warnings

    # ── Import injection ──────────────────────────────────────────────────────

    def _inject_imports(self, content: str, imports: list, language: str) -> str:
        """
        Insert net-new import statements at the correct position for each language.
        Deduplicates — skips imports already present in the file.
        """
        if not imports:
            return content

        net_new = [imp.strip() for imp in imports if imp.strip() and imp.strip() not in content]
        if not net_new:
            return content

        lines      = content.split("\n")
        lang_lower = language.lower()

        if lang_lower == "java":
            last_import = -1
            package_line = -1
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("import "):
                    last_import = i
                elif stripped.startswith("package "):
                    package_line = i
            insert_after = last_import if last_import >= 0 else package_line
            if insert_after >= 0:
                for j, imp in enumerate(net_new):
                    lines.insert(insert_after + 1 + j, imp)
            else:
                lines = net_new + [""] + lines

        elif lang_lower in ("python", "py"):
            last_import = -1
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("import ") or stripped.startswith("from "):
                    last_import = i
                elif last_import >= 0 and stripped and not stripped.startswith("#"):
                    break
            if last_import >= 0:
                for j, imp in enumerate(net_new):
                    lines.insert(last_import + 1 + j, imp)
            else:
                lines = net_new + [""] + lines

        elif lang_lower in ("go", "golang"):
            import_block_start = -1
            import_block_end   = -1
            for i, line in enumerate(lines):
                if re.match(r"^import\s*\(", line.strip()):
                    import_block_start = i
                elif import_block_start >= 0 and line.strip() == ")":
                    import_block_end = i
                    break
            if import_block_start >= 0 and import_block_end >= 0:
                for j, imp in enumerate(net_new):
                    lines.insert(import_block_end + j, f'\t{imp}')
            else:
                last_import = -1
                for i, line in enumerate(lines):
                    if line.startswith("import "):
                        last_import = i
                if last_import >= 0:
                    for j, imp in enumerate(net_new):
                        lines.insert(last_import + 1 + j, f"import {imp}")
                else:
                    lines = [f"import {imp}" for imp in net_new] + [""] + lines

        else:
            # TypeScript / JavaScript: insert after last `import` at file top
            last_import = -1
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("import ") or ("require(" in stripped):
                    last_import = i
                elif last_import >= 0 and stripped and not stripped.startswith("//"):
                    break
            if last_import >= 0:
                for j, imp in enumerate(net_new):
                    lines.insert(last_import + 1 + j, imp)
            else:
                lines = net_new + [""] + lines

        return "\n".join(lines)

    # ── Compile validation ────────────────────────────────────────────────────

    def _validate(
        self,
        content: str,
        path: str,
        language: str,
        sandbox_image: Optional[str],
        run_id: str,
        solution_text: str,
        desc: str,
    ) -> tuple[bool, str]:
        """
        Compile-check content in the sandbox. Returns (success, error_output).
        If no sandbox image or no compile command for language, returns (True, "")
        to allow patching to proceed without blocking on infra gaps.
        """
        if not sandbox_image:
            logger.debug(f"[PE {run_id}] No sandbox image — skipping compile for {path}")
            return True, ""

        lang_lower = language.lower()

        # File extension takes priority for the compile-command lookup.
        # A .jsx file arrives with language="javascript" (the broad category
        # from _EXT_LANG), but "javascript" → `node --check` which rejects JSX
        # syntax.  The more specific extension keys (jsx, tsx, ts) map to the
        # correct Babel/TS checker.  Only override when the extension is
        # recognised so unknown extensions still fall back to language.
        _EXT_COMPILE_OVERRIDE: dict[str, str] = {
            ".jsx": "jsx", ".tsx": "tsx",
            ".ts":  "typescript",
            ".vue": "vue",
        }
        if path and "." in path:
            _fext = "." + path.rsplit(".", 1)[-1].lower()
            lang_lower = _EXT_COMPILE_OVERRIDE.get(_fext, lang_lower)

        # Belt-and-suspenders: non-code file types have no compile step.
        # Phase 0a routes these before the patch engine, but guard here too.
        _NONCODE_SENTINELS = {
            "sql", "xml", "markdown", "md", "yaml", "json",
            "properties", "txt", "csv", "toml", "ini", "nocompile",
        }
        if lang_lower in _NONCODE_SENTINELS or lang_lower not in _COMPILE_COMMANDS:
            return True, ""

        cmd        = _COMPILE_COMMANDS.get(lang_lower)
        filename   = _SANDBOX_FILENAMES.get(lang_lower, "main.py")

        if not cmd:
            logger.debug(f"[PE {run_id}] No compile command for '{language}' — skipping")
            return True, ""

        try:
            import re as _re
            from sandbox.self_healing_engine import SelfHealingEngine as _SHE

            code_input = content
            if lang_lower == "java":
                # Strip `public` from top-level declaration to avoid
                # "class X is public, should be in file X.java" error in sandbox
                code_input = _re.sub(
                    r"\bpublic\s+(class|interface|enum|record)\b",
                    r"\1", content, count=1,
                )

            healer = _SHE()
            # Use _execute (compile-only, no healing) — the outer loop handles retries
            result = healer._execute(
                code_input, lang_lower,
                image_tag=sandbox_image,
                command=cmd,
                filename=filename,
            )

            if result.get("success"):
                return True, ""
            if result.get("image_missing"):
                logger.warning(
                    f"[PE {run_id}] Sandbox image not available locally — "
                    f"skipping compile check for {path} (infra skip)"
                )
                return True, ""
            err = result.get("error") or result.get("output") or "Unknown compile error"
            return False, str(err)

        except Exception as _e:
            logger.warning(f"[PE {run_id}] Compile check error: {_e} — treating as pass")
            return True, ""  # Don't block on infra failures


# ── Error tail extractor ──────────────────────────────────────────────────────

def _extract_error_tail(compile_error: str) -> str:
    """
    Pull out only the useful part of an ast.parse / compiler traceback.

    For SyntaxError the reported line is often WHERE the parser gave up, not
    WHERE the actual problem is (e.g. an unclosed paren on the previous line).
    We include 4 lines before the error marker so the LLM sees the real cause.
    Falls back to the last 500 chars if no error marker is found.
    """
    if not compile_error:
        return ""
    lines = compile_error.strip().splitlines()
    error_start = len(lines)
    for i, ln in enumerate(lines):
        if re.match(r"^\w+Error:", ln.strip()) or re.match(r"^\s+\^+\s*$", ln):
            error_start = max(0, i - 4)  # 4 lines back catches unclosed-delimiter context
            break
    tail = "\n".join(lines[error_start:])
    return tail if tail else compile_error[-500:]


# ── SEARCH/REPLACE block parser ─────────────────────────────────────────────

# Tolerant of variable-length markers (>=5 fence chars), trailing spaces on marker
# lines, and surrounding prose. Markers must each be on their own line; the newlines
# adjoining the markers are consumed so the captured groups hold the block content
# WITHOUT wrapping newlines (internal indentation is preserved).
_SR_BLOCK_RE = re.compile(
    r"<{5,}[ \t]*SEARCH[ \t]*\n(.*?)\n={5,}[ \t]*\n(.*?)\n>{5,}[ \t]*REPLACE",
    re.DOTALL,
)


def _parse_search_replace_blocks(raw: str) -> list:
    """
    Parse LLM output into an ordered list of (search, replace) string pairs.

    Tolerant of a wrapping markdown fence and prose around the blocks; malformed or
    empty-SEARCH blocks are dropped (they surface later as apply warnings). Returns
    [] when no well-formed block is present.
    """
    if not raw:
        return []
    text = raw.strip()
    # Strip a single wrapping code fence if the whole reply is fenced.
    text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)

    blocks: list = []
    for m in _SR_BLOCK_RE.finditer(text):
        search  = m.group(1)
        replace = m.group(2)
        if search.strip() == "":
            continue  # empty SEARCH is not anchorable
        blocks.append((search, replace))
    return blocks


# ── Singleton ─────────────────────────────────────────────────────────────────

patch_engine = PatchEngine()


# ── Import restoration ────────────────────────────────────────────────────────

def restore_missing_imports(new_content: str, original_content: str, language: str) -> str:
    """
    After full-file LLM generation, re-inject any imports that existed in the
    original file but are absent from the generated content. Called on the agentic
    path and the LLM fallback path in _run_coder, and on the CLI-engine diff-capture
    path (sdlc_state_machine._collect_workspace_edits).

    Matching is by MODULE SPECIFIER, not by exact line. An original import is only
    treated as "missing" when the new content imports NOTHING from that same module.
    This is critical for a legitimately MODIFIED import — e.g. adding a symbol to an
    existing named import ``import { A } from "m"`` → ``import { A, B } from "m"``:
    the new line is no longer string-equal to the original, so exact-string matching
    would flag the original as dropped and re-inject it, producing a DUPLICATE import
    from the same module. In JS/TS that duplicate is a build-time
    ``Identifier 'A' has already been declared`` SyntaxError (the exact failure that
    was suspending SDLC REVIEW forever, since the coder could never see or fix the
    injected duplicate). Module-keying still catches the real case this guard exists
    for: a full-file regen that drops a module's import wholesale.
    """
    import re as _re
    lang_lower = language.lower()

    def _import_lines(text: str) -> list[str]:
        result = []
        for ln in text.splitlines():
            s = ln.strip()
            if not s:
                continue
            if lang_lower in ("python", "py"):
                if s.startswith("import ") or s.startswith("from "):
                    result.append(s)
            elif lang_lower in ("go", "golang"):
                if s.startswith("import ") or (s.startswith('"') and "/" in s):
                    result.append(s)
            else:
                # Java, Kotlin, TypeScript, JavaScript, etc.
                if s.startswith("import "):
                    result.append(s)
        return result

    def _module_key(line: str):
        """Best-effort (kind, module-specifier) identity for an import line, so a
        modified import maps to the SAME key as its original form. Falls back to a
        raw-line key when the specifier can't be parsed (never worse than exact
        matching)."""
        s = line.strip()
        if lang_lower in ("python", "py"):
            if s.startswith("from "):
                return ("from", s[5:].split(" import ", 1)[0].strip())
            if s.startswith("import "):
                return ("import", s[7:].split(" as ", 1)[0].split(",", 1)[0].strip())
            return ("raw", s)
        if lang_lower in ("go", "golang"):
            m = _re.search(r'"([^"]+)"', s)
            return ("go", m.group(1)) if m else ("raw", s)
        # JS / TS: `import ... from "mod"` or side-effect `import "mod"`
        m = _re.search(r'from\s+["\']([^"\']+)["\']', s)
        if m:
            return ("mod", m.group(1))
        m = _re.match(r'import\s+["\']([^"\']+)["\']', s)
        if m:
            return ("mod", m.group(1))
        # Java / Kotlin: `import a.b.C;` / `import static a.b.C.d;` — the FQN is the
        # specifier (there is no per-module merge, so FQN-keying == exact behavior).
        body = s[len("import "):].strip() if s.startswith("import ") else s
        if body.startswith("static "):
            body = body[len("static "):].strip()
        return ("fqn", body.rstrip(";").strip())

    original_imports = _import_lines(original_content)
    if not original_imports:
        return new_content

    new_import_lines = _import_lines(new_content)
    new_exact = set(new_import_lines)
    new_modules = {_module_key(imp) for imp in new_import_lines}

    # "Missing" = neither present verbatim NOR covered by any new import from the
    # same module (the latter means the original was edited/merged, not dropped).
    missing = [
        imp for imp in original_imports
        if imp not in new_exact and _module_key(imp) not in new_modules
    ]
    if not missing:
        return new_content

    return patch_engine._inject_imports(new_content, missing, language)