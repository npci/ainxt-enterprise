# SPDX-License-Identifier: MIT
"""
agents/sdlc_governance/engine.py — Step 4: governance review orchestration.

The heart of PART 2 (the separate GOVERNANCE_REVIEW phase) plus the PART 1
awareness resolver. Responsibilities:

- `resolve_awareness()` — PART 1: resolve the bundle + selected skills and return a
  pointer_block with each skill's SKILL.md INLINED, for the EXISTING PLAN / IMPLEMENT
  CLI prompts. There is no CLI plugin-loading mechanism (confirmed 2026-07-20 —
  neither a `--plugin`/`--skill` flag nor a `/plugin`/`/skill` slash command loads
  anything headlessly on the deployed binary), so prompt text is the only channel.
  Fully fail-safe: any problem → "" so a run behaves exactly as if governance were
  absent.
- `select_skills(subset)` — resolve the bundle and the (optionally subset-filtered)
  skill list.
- `build_review_prompt()` / `run_review()` — the diff-only governance review CLI
  session, forced to emit the platform-owned GOVERNANCE_SCHEMA. Fail-CLOSED: a
  missing/errored/unparseable result becomes a synthetic blocking FAIL (mirrors
  the code-REVIEW gate — never a silent pass).
- `apply_suppressions()` — drop findings matching active per-(product, repo)
  suppressions (content-fingerprint match); suppressed findings are retained for
  the report with status="suppressed".
- `build_fix_prompt()` — the governance fixer prompt (minimal-diff, findings only).
- `render_report()` — per-skill report (verdict + findings + statuses) + report_md
  for the MR note / UI / standalone file.
- `resolve_product_id()` — map a repo to its product_id via product_repos.

HARD CONSTRAINTS
----------------
- LLM calls ONLY via the CLI engine (which routes through the gateway) — never a
  direct SDK/HTTP call here.
- Diff-only scope; suppressions are never auto-created.
- Import side-effect-free (stdlib + core.logger + sibling governance modules).
"""

from __future__ import annotations

import os
import shutil
import stat as _stat
from typing import List, Optional, Tuple

from core.logger import logger

from agents.sdlc_governance import config as gov_config
from agents.sdlc_governance import bundle as gov_bundle
from agents.sdlc_governance.schema import (
    GOVERNANCE_SCHEMA,
    Finding,
    fingerprint,
    content_fingerprint,
    parse_findings,
    is_blocking,
    overall_verdict_of,
    severity_rank,
)


# ════════════════════════════════════════════════════════════════════════════
# Skill resolution
# ════════════════════════════════════════════════════════════════════════════

def _matches_subset(skill, want: set) -> bool:
    return (skill.slug or "").lower() in want or (skill.plugin_name or "").lower() in want


def select_skills(subset=None, phase=None) -> Tuple[Optional[gov_bundle.Bundle], List[gov_bundle.GovSkill]]:
    """Resolve the bundle and its skills, filtered by (1) the run-level `subset`
    (slugs/plugin names; None → all), then (2) the `phase` ("plan"|"implement"|
    "review"), if given. Phase filtering precedence: a per-phase env override
    (SDLC_GOVERNANCE_<PHASE>_SKILLS) wins; else the skill's manifest `phases` tag
    (empty tag = applies to all phases). Returns (None, []) when governance is
    disabled or no bundle/skills resolve — NEVER raises."""
    try:
        if not gov_config.enabled():
            return None, []
        b = gov_bundle.resolve_bundle()
        if b is None:
            return None, []
        skills = gov_bundle.discover_skills(b) or []
        # (1) run-level subset (applies across all phases).
        sub = gov_config.parse_subset(subset)
        if sub:
            want = {s.strip().lower() for s in sub if isinstance(s, str) and s.strip()}
            skills = [s for s in skills if _matches_subset(s, want)]
        # (2) per-phase routing.
        if phase:
            ph = str(phase).strip().lower()
            env_sel = gov_config.skills_for_phase(ph)
            if env_sel is not None:
                want = {s.strip().lower() for s in env_sel if isinstance(s, str) and s.strip()}
                skills = [s for s in skills if _matches_subset(s, want)]
            else:
                # Manifest `phases` tag: keep skills that either apply to all phases
                # (empty tag) or explicitly include this phase.
                skills = [s for s in skills if (not s.phases) or ph in s.phases]
        bundle_dir = getattr(b, "dir", "") or ""
        logger.info(
            "[SDLC-GOV] select_skills resolved",
            bundle_dir=bundle_dir,
            bundle_source=getattr(b, "source", ""),
            bundle_ref=getattr(b, "ref", ""),
            phase=phase or "all",
            subset=list(subset or []),
            skills_count=len(skills),
            skills=[getattr(s, "slug", "") for s in skills],
            domains=[getattr(s, "domain", "") for s in skills],
        )
        return b, skills
    except Exception as e:  # pragma: no cover - defensive; governance is fail-safe
        logger.warning("[SDLC-GOV] select_skills failed — treating as no skills", error=str(e))
        return None, []


# Per-skill cap on inlined SKILL.md content so N loaded skills can't blow the
# PLAN/IMPLEMENT prompt budget. Confirmed 2026-07-20 on the actual deployed CLI
# binary: neither a `--plugin`/`--skill` flag nor a `/plugin`/`/skill` slash
# command loads anything headlessly (the flag text is silently swallowed and
# any leftover text — or a bare slash-command string — is sent to the model as
# plain chat text, never intercepted). Prompt text is therefore the ONLY
# channel that reaches a headless CLI session, so PART 1 awareness now INLINES
# each skill's SKILL.md directly instead of naming a plugin the CLI can't load.
_SKILL_MD_MAX_CHARS = 6000


def _read_skill_md(skill: gov_bundle.GovSkill) -> str:
    """Read one skill's SKILL.md content, bounded to _SKILL_MD_MAX_CHARS. Never
    raises — a missing/unreadable file is logged and skipped (returns "")."""
    path = getattr(skill, "skill_md_path", "") or ""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception as e:
        logger.warning(
            "[SDLC-GOV] could not read SKILL.md — skipping its inline content",
            skill=getattr(skill, "slug", ""), path=path, error=str(e),
        )
        return ""
    text = text.strip()
    if len(text) > _SKILL_MD_MAX_CHARS:
        text = text[:_SKILL_MD_MAX_CHARS] + f"\n...[+{len(text) - _SKILL_MD_MAX_CHARS} chars truncated]"
    return text


def resolve_awareness(subset=None, phase=None, workspace_root: str = "") -> str:
    """PART 1 (always-on) awareness resolver. Returns a pointer_block: prompt text
    appended to PLAN/IMPLEMENT prompts via ``governance_pointer_clause``. `phase`
    routes phase-scoped skills (see select_skills): PLAN passes "plan", IMPLEMENT
    passes "implement".

    When `workspace_root` is given, each selected skill's FULL folder (SKILL.md +
    all subfolders/rule files) is staged READ-ONLY inside the workspace (see
    stage_skill_readonly) and the block POINTS the CLI at those folders — the CLI
    reads them itself, untruncated. This mirrors the governance SCAN path and was
    verified on the real binary (its file tools are jailed to the workspace cwd, so
    the skill folder must live inside it; `--add-dir` is a no-op there). When no
    workspace is available (or staging fails for any skill) it FALLS BACK to inlining
    a bounded SKILL.md excerpt per skill (_SKILL_MD_MAX_CHARS) so behaviour is never
    worse than before.

    Fail-safe: gated by ``awareness_enabled()`` and bundle availability; any problem
    → "" so PLAN/IMPLEMENT run exactly as they do today (kill-switch:
    SDLC_GOVERNANCE_AWARENESS=false).

    Per-phase kill-switch: SDLC_GOVERNANCE_<PHASE>_DISABLED (phase = plan|implement)
    drops the awareness block for just that CLI phase in the bug/feature pipeline
    (the only caller of this function). The governance REVIEW is deliberately NOT
    covered — review skills cannot be disabled, since review without them is
    meaningless (it runs via select_skills(), not this resolver)."""
    try:
        if not gov_config.enabled() or not gov_config.awareness_enabled():
            return ""
        if gov_config.phase_disabled(phase):
            logger.info("[SDLC-GOV] resolve_awareness: phase disabled — no awareness block",
                        phase=str(phase or "").strip().lower())
            return ""
        b, skills = select_skills(subset, phase=phase)
        if not b or not skills:
            return ""
        names = ", ".join((s.name or s.slug) for s in skills if (s.name or s.slug))

        # Preferred path: stage every skill's full folder read-only INTO the workspace
        # and point the CLI at it. Only used when a workspace is available AND every
        # skill staged successfully — a partial stage would silently hide a skill, so
        # we fall back to full inlining in that case.
        if workspace_root:
            staged = []
            for s in skills:
                rel = stage_skill_readonly(workspace_root, s)
                if rel:
                    staged.append((s, rel))
            if len(staged) == len(skills):
                lines = [
                    f"- The following governance skills are BINDING for this change: {names}.",
                    "- Each skill's FULL materials (SKILL.md + any subfolders / rule files) "
                    "have been placed READ-ONLY in this workspace. Consult and honour them "
                    "for every file you plan or edit — read the actual files, do not guess:",
                ]
                for s, rel in staged:
                    lines.append(f"    - {s.name or s.slug}: `{rel}/`  (start with `{rel}/SKILL.md`)")
                return "\n".join(lines)

        # Fallback: inline bounded SKILL.md excerpts (previous behaviour).
        sections = []
        for s in skills:
            content = _read_skill_md(s)
            if content:
                sections.append(f"### {s.name or s.slug}\n{content}")
        if not sections:
            return ""
        return (
            f"- The following governance skills are BINDING for this change: {names}.\n"
            "- Their standards are inlined below — consult and honour them for every "
            "file you plan or edit.\n\n" + "\n\n".join(sections)
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[SDLC-GOV] resolve_awareness failed — skipping awareness", error=str(e))
        return ""


# ════════════════════════════════════════════════════════════════════════════
# Review prompt + session
# ════════════════════════════════════════════════════════════════════════════

def _diff_block(diff_text: str, diff_path: str) -> str:
    """The 'UNIFIED DIFF' section of a governance prompt. When `diff_path` is set
    (the diff was staged to a file in the workspace — see stage_diff_file), point
    the CLI at the file instead of inlining the diff, so a large diff never bloats
    the `--print` argv token. Falls back to inlining when no file was staged."""
    if diff_path:
        return (
            "UNIFIED DIFF: the full unified diff for this change has been written to "
            f"`{diff_path}` in this workspace (it is too large to inline). READ THAT "
            "FILE with your file tool to see every changed hunk before reviewing.\n\n"
        )
    return "UNIFIED DIFF:\n```diff\n" + (diff_text or "") + "\n```\n\n"


def _scan_diff_block(diff_text: str, diff_path: str, base_sha: str) -> str:
    """Same file-vs-inline choice as _diff_block, for the per-skill SCAN prompt
    (carries the base→HEAD label)."""
    if diff_path:
        return (
            f"UNIFIED DIFF (base {base_sha} → HEAD): written to `{diff_path}` in this "
            "workspace (too large to inline). READ THAT FILE to see the change; the "
            "analyzer entrypoint above already points at it.\n\n"
        )
    return (
        f"UNIFIED DIFF (base {base_sha} → HEAD):\n```diff\n" + (diff_text or "") + "\n```\n"
    )


def build_review_prompt(diff_text: str, changed_files: list, skills: list, ref: str,
                        staged=None, diff_path: str = "") -> str:
    """Diff-only governance review prompt over ALL selected skills in ONE session.

    When `staged` (a list of (skill, workspace-relative-path) tuples) is given, each
    skill's FULL folder has been copied READ-ONLY into the workspace (see
    stage_skill_readonly) and the prompt POINTS the CLI at those folders — it reads
    each SKILL.md + subfolders/rule scripts ITSELF. There is NO CLI plugin-loading
    channel on the deployed binary (verified), so this staged-folder pointer is the
    real load mechanism; naming a plugin does nothing. Falls back to inlining bounded
    SKILL.md excerpts when nothing staged. Carries the full output-contract so skill
    teams stay schema-agnostic."""
    skill_names = ", ".join((getattr(s, "name", "") or getattr(s, "slug", "")) for s in (skills or [])) \
        or "the loaded governance skills"

    if staged:
        lines = [
            "The FULL materials for each governance skill have been placed READ-ONLY in "
            "this workspace. Read each skill's SKILL.md (and any subfolders / rule scripts "
            "it references) and apply ONLY those standards:",
        ]
        for s, rel in staged:
            dom = getattr(s, "domain", "") or "n/a"
            lines.append(f"  - {s.name or s.slug} (domain {dom}): `{rel}/`  (start with `{rel}/SKILL.md`)")
        materials_block = "\n".join(lines) + "\n\n"
    else:
        sections = []
        for s in (skills or []):
            content = _read_skill_md(s)
            if content:
                sections.append(f"### {getattr(s, 'name', '') or getattr(s, 'slug', '')}\n{content}")
        if sections:
            materials_block = (
                "The governance skills' standards are inlined below (bounded excerpts; the "
                "full folders could not be staged this run). Apply ONLY these standards:\n\n"
                + "\n\n".join(sections) + "\n\n"
            )
        else:
            materials_block = f"The governance skills for this session are: {skill_names}.\n\n"

    return (
        "You are a governance reviewer.\n"
        f"{materials_block}"
        "Using ONLY those skills' standards (e.g. EA enterprise-architecture, IS "
        "information-security, DPDP data-protection — whichever are provided), review the "
        "UNIFIED DIFF below for NEW violations INTRODUCED by these changes. Do NOT flag "
        "pre-existing issues the diff does not touch. If a skill ships analyzer/helper "
        "scripts in its folder, run them via Bash as its SKILL.md prescribes (write any "
        "scratch output to a directory OUTSIDE this workspace — the skill folders are "
        "read-only).\n\n"
        f"GOVERNANCE BUNDLE REF: {ref}\n"
        f"CHANGED FILES: {list(changed_files or [])}\n\n"
        f"{_diff_block(diff_text, diff_path)}"
        "Emit your result in the REQUIRED structured-output schema:\n"
        "- overall_verdict: \"PASS\" if there are NO new violations, else \"FAIL\".\n"
        "- skills: one entry per skill you evaluated, each with: skill (its name), "
        "verdict (PASS|FAIL), summary (one line), and findings (a list).\n"
        "- each finding: severity (critical|high|medium|low), file, line (integer or "
        "null), rule (the skill rule id/name), title, detail, fix_hint, and snippet (a "
        "SHORT verbatim excerpt of the offending diff line(s)).\n"
        "Report a skill with an empty findings list and verdict PASS when it finds nothing."
    )


# ════════════════════════════════════════════════════════════════════════════
# Read-only skill staging (Step 2 support)
# ════════════════════════════════════════════════════════════════════════════
#
# The deployed headless CLI (ainxt v1.0.5-beta) jails its file tools to the
# session's workspace cwd — VERIFIED on the real binary: `--add-dir` is a no-op
# for the read_file tool (an absolute path outside cwd is invisible even under
# `--yes` full-bypass). It also does NOT honour `--permission-mode plan` as a
# write-blocker (a benign write_file succeeded in plan mode). So to give the CLI
# access to a skill's FULL materials (SKILL.md + every subfolder/rule script,
# untruncated) as READ-ONLY, we must:
#   1. copy the skill folder INTO the workspace (so it's inside the tool jail), and
#   2. chmod the staged subtree read-only at the FILESYSTEM level (the only
#      enforcement the binary actually respects — a staged write then fails EACCES).
# The staging dir is added to .git/info/exclude so it never enters the review diff.
# All of the above was verified end-to-end against the live binary before shipping.
_STAGE_DIRNAME = ".governance_skills"

# Serializes the shared-file mutation during parallel governance SCAN staging (the
# `.git/info/exclude` append). Per-skill copytree/chmod need no lock — each skill
# stages into its OWN distinct <workspace>/.governance_skills/<slug>/ subdir, so
# concurrent copies never touch the same path.
# NOTE (Fix G): this lock is shared with `agents/multi_repo_workspace.py`, which
# does its own read-modify-append on the SAME <workspace>/.git/info/exclude file
# via its own `_git_exclude`. Both modules must share this ONE lock object — a
# module-local `threading.Lock()` per module would make the mutual exclusion
# illusory. The lock itself lives in the dependency-free `agents/_stage_lock.py`
# (not here) so this module doesn't have to be imported just to get the Lock —
# `multi_repo_workspace.py` is deliberately import-light and must not pull in
# this module's heavier dependency chain (core.logger et al.) at import time.
from agents._stage_lock import STAGE_LOCK as _STAGE_LOCK


def _chmod_tree(path: str, *, writable: bool) -> None:
    """Add/remove the write bit across a tree (read+execute bits untouched, so the
    CLI can still read files and run analyzer scripts). Removing write from the
    directories too is what actually blocks new-file creation inside them."""
    wbits = _stat.S_IWUSR | _stat.S_IWGRP | _stat.S_IWOTH
    for root, dirs, files in os.walk(path):
        for name in [root] + [os.path.join(root, f) for f in files] \
                + [os.path.join(root, d) for d in dirs]:
            try:
                mode = os.stat(name).st_mode
                os.chmod(name, (mode | wbits) if writable else (mode & ~wbits))
            except OSError:
                pass


def _git_exclude(workspace_root: str, pattern: str) -> None:
    """Best-effort: add `pattern` to <workspace>/.git/info/exclude so the staged
    skills never show up in the review diff / VERIFIED_DIFF. Never raises."""
    try:
        info_dir = os.path.join(workspace_root, ".git", "info")
        if not os.path.isdir(info_dir):
            return
        exclude = os.path.join(info_dir, "exclude")
        # Lock the read-modify-append so parallel scan sessions can't interleave-write
        # or double-append the pattern into the shared exclude file.
        with _STAGE_LOCK:
            existing = ""
            if os.path.isfile(exclude):
                with open(exclude, "r", encoding="utf-8", errors="replace") as fh:
                    existing = fh.read()
            if pattern not in existing.split():
                with open(exclude, "a", encoding="utf-8") as fh:
                    fh.write(("" if existing.endswith("\n") or not existing else "\n") + pattern + "\n")
    except Exception:
        pass


# The unified diff is written to a file INSIDE the workspace (git-excluded) and the
# prompt references its PATH — it is NEVER interpolated whole into the prompt text.
# The deployed CLI passes the entire prompt as a single `--print <prompt>` argv token,
# so a large embedded diff (e.g. a 400-file change) overflows the OS argument limit
# (ARG_MAX) and the spawn fails with a usage error → fail-closed blocking FAIL. A file
# the CLI reads via its workspace-jailed read_file tool sidesteps the limit entirely.
_DIFF_DIRNAME = ".governance_diff"


def stage_diff_file(workspace_root: str, diff_text: str, tag: str = "") -> str:
    """Write `diff_text` to ``<workspace>/.governance_diff/diff[-tag].patch`` and return
    the workspace-relative path (forward-slashed), or "" on failure so the caller can
    fall back to inlining the diff into the prompt. git-excluded so it never enters the
    review diff / VERIFIED_DIFF and never trips the scan's post-session mutation guard.
    A distinct `tag` (e.g. a skill slug) gives each parallel scan session its own file,
    so concurrent writes never race. Never raises."""
    try:
        if not workspace_root:
            return ""
        abs_dir = os.path.join(workspace_root, _DIFF_DIRNAME)
        os.makedirs(abs_dir, exist_ok=True)
        safe_tag = (tag or "").replace("/", "__").replace("\\", "__").strip()
        fname = f"diff{('-' + safe_tag) if safe_tag else ''}.patch"
        with open(os.path.join(abs_dir, fname), "w", encoding="utf-8") as fh:
            fh.write(diff_text or "")
        _git_exclude(workspace_root, _DIFF_DIRNAME + "/")
        rel = os.path.join(_DIFF_DIRNAME, fname).replace(os.sep, "/")
        logger.info("[SDLC-GOV] staged diff file", rel=rel, diff_chars=len(diff_text or ""))
        return rel
    except Exception as e:
        logger.warning("[SDLC-GOV] stage_diff_file failed — falling back to inline diff",
                       error=str(e))
        return ""


def stage_skill_readonly(workspace_root: str, skill) -> str:
    """Copy a skill's ENTIRE folder (SKILL.md + all subfolders/rule scripts) into a
    read-only staging dir inside `workspace_root`, so the headless CLI (whose file
    tools are jailed to the workspace cwd) can read the full, UNTRUNCATED skill
    materials itself. The staged subtree is chmod'd read-only at the filesystem
    level (the binary's own permission-mode does not block writes). Returns the
    workspace-relative staged path (e.g. ".governance_skills/dpdp"), or "" on any
    failure so the caller can fall back to inlining. Never raises."""
    try:
        src = getattr(skill, "dir", "") or ""
        if not workspace_root or not src or not os.path.isdir(src):
            return ""
        slug = (getattr(skill, "slug", "") or "skill").replace("/", "__")
        stage_root = os.path.join(workspace_root, _STAGE_DIRNAME)
        dest = os.path.join(stage_root, slug)
        if os.path.isdir(dest):
            _chmod_tree(dest, writable=True)   # restore write bits so rmtree can delete
            shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(stage_root, exist_ok=True)
        shutil.copytree(src, dest)
        _chmod_tree(dest, writable=False)      # FS-level read-only (verified enforced)
        _git_exclude(workspace_root, _STAGE_DIRNAME + "/")
        rel = os.path.relpath(dest, workspace_root).replace(os.sep, "/")
        logger.info("[SDLC-GOV] staged skill read-only", skill=slug, rel=rel, src=src)
        return rel
    except Exception as e:
        logger.warning("[SDLC-GOV] stage_skill_readonly failed — falling back to inlined SKILL.md",
                       skill=getattr(skill, "slug", ""), error=str(e))
        return ""


def build_scan_prompt(skill, diff_text: str, changed_files: list,
                      base_sha: str, workspace_root: str, bundle_dir: str,
                      staged_rel: str = "", diff_path: str = "",
                      scratch_dir: str = "") -> str:
    """Agentic per-skill SCAN prompt (Step 2). Unlike build_review_prompt (a
    single diff-read session over ALL loaded skills), this drives ONE isolated
    session for ONE skill that RUNS that skill's analyzer scripts via Bash and
    reports only NEW violations introduced by base_sha...HEAD.

    When `staged_rel` is given, the skill's FULL folder has been copied read-only
    INTO the workspace at that relative path (see stage_skill_readonly) — the
    prompt then points the CLI there and it reads SKILL.md + every subfolder
    ITSELF (no truncation). When staging failed (staged_rel==""), we fall back to
    inlining a bounded SKILL.md excerpt so the scan still has context. Carries the
    full output-contract instruction so skill teams stay schema-agnostic, plus a
    fail-CLOSED directive: a missing/failing analyzer binary must be reported as a
    blocking finding, never treated as a pass."""
    skill_name = (getattr(skill, "name", "") or getattr(skill, "slug", "")) or "the governance skill"
    slug = getattr(skill, "slug", "") or skill_name
    domain = getattr(skill, "domain", "") or ""
    entrypoint = getattr(skill, "entrypoint", "") or ""
    unavail_rule = f"{domain}-SCAN-UNAVAIL" if domain else "SCAN-UNAVAIL"

    # Skill-materials block: prefer the read-only staged folder (full access);
    # fall back to a bounded inline excerpt only if staging failed.
    if staged_rel:
        skill_root = staged_rel
        materials_block = (
            f"The COMPLETE '{skill_name}' skill has been placed READ-ONLY inside this "
            f"workspace at `{staged_rel}/`. It contains SKILL.md and any subfolders / "
            "rule scripts / reference files the skill ships. This folder is BINDING — "
            f"START by reading `{staged_rel}/SKILL.md` in full, then read whatever other "
            "files under that folder it references. Do NOT rely on any summary; consult "
            "the actual files. The folder is read-only — do not attempt to modify it.\n\n"
        )
    else:
        skill_root = bundle_dir
        skill_md = _read_skill_md(skill) or "(SKILL.md was empty or unreadable)"
        materials_block = (
            "The SKILL.md below is BINDING — it defines exactly what to check, which "
            "analyzer scripts to run, and how to interpret their output. Follow it "
            "literally. (NOTE: this is a bounded excerpt; the full skill folder could "
            "not be staged into the workspace this run.)\n\n"
            "=== BINDING SKILL.md ===\n"
            f"{skill_md}\n"
            "=== END SKILL.md ===\n\n"
        )

    # Analyzer scratch dir — OUTSIDE the tracked checkout (so it never pollutes the
    # git diff) and under BUILDER_WORKSPACE_ROOT (not the OS temp dir). Always supplied
    # by run_scan_session; the fallback only guards direct/legacy callers.
    _scratch = scratch_dir or os.path.join(
        os.path.dirname(os.path.abspath(workspace_root or ".")), "gov_scratch")

    entry_clause = ""
    if entrypoint and diff_path:
        entry_clause = (
            "- This skill ships an analyzer entrypoint. The unified diff for this change "
            f"is already available in this workspace at `{diff_path}`. Run:\n"
            f"    python {skill_root}/{entrypoint} --diff {diff_path} --base {base_sha} "
            f"--output {_scratch}/gov_{slug}_findings.json\n"
            "  then fold its JSON output into your findings.\n"
        )
    elif entrypoint:
        entry_clause = (
            "- This skill ships an analyzer entrypoint. Write the unified diff below to a "
            f"file (e.g. {_scratch}/gov_{slug}.diff) and run:\n"
            f"    python {skill_root}/{entrypoint} --diff <diff_file_path> --base {base_sha} "
            f"--output {_scratch}/gov_{slug}_findings.json\n"
            "  then fold its JSON output into your findings.\n"
        )
    else:
        entry_clause = (
            "- This skill has no dedicated entrypoint script; run whatever analyzer "
            "commands its SKILL.md prescribes (via Bash) and interpret their output.\n"
        )

    return (
        f"You are a governance SCANNER running the '{skill_name}' skill"
        f"{f' (domain {domain})' if domain else ''} against a code change.\n"
        f"{materials_block}"
        f"GOVERNANCE SKILL FOLDER (analyzer scripts live here): {skill_root}\n"
        f"WORKSPACE ROOT (the repo under review): {workspace_root}\n"
        f"BASE SHA: {base_sha}\n"
        f"CHANGED FILES: {list(changed_files or [])}\n\n"
        "HOW TO RUN THE SCAN:\n"
        f"{entry_clause}"
        "- Run the skill's dependent analyzer scripts exactly as its SKILL.md references "
        "them, using Bash. Do NOT edit any repository files — this is a read-only scan.\n"
        f"- Report ONLY violations INTRODUCED by the range {base_sha}...HEAD. Do NOT flag "
        "pre-existing issues that the diff does not touch.\n\n"
        f"{_scan_diff_block(diff_text, diff_path, base_sha)}"
        f"CHANGED FILES: {list(changed_files or [])}\n\n"
        "Emit your result in the REQUIRED structured-output schema:\n"
        "- overall_verdict: \"PASS\" if there are NO new violations, else \"FAIL\".\n"
        "- skills: one entry for this skill, with: skill (its name), verdict "
        "(PASS|FAIL), summary (one line), and findings (a list).\n"
        "- each finding: severity (critical|high|medium|low), file, line (integer or "
        "null), rule (the skill rule id/name), title, detail, fix_hint, and snippet (a "
        "SHORT verbatim excerpt of the offending diff line(s)).\n"
        "Report an empty findings list with verdict PASS only when the analyzers ran "
        "successfully AND found nothing.\n\n"
        "FAIL-CLOSED (mandatory): if a required analyzer binary/script is missing, errors, "
        "or cannot run to completion, DO NOT report PASS. Instead emit a single blocking "
        f"finding with severity \"high\", rule \"{unavail_rule}\", title describing what was "
        "unavailable, and verdict FAIL — so the gate blocks rather than silently passing."
    )


def _fail_closed_structured(skills: list, msg: str) -> dict:
    """Synthetic blocking FAIL when the review could not be completed — mirrors the
    code-REVIEW gate's fail-toward-blocking. One high-severity availability finding
    per selected skill so the gate blocks rather than silently passing."""
    results = []
    for s in (skills or []):
        slug = getattr(s, "slug", None) or getattr(s, "name", None) or "governance"
        results.append({
            "skill": slug,
            "verdict": "FAIL",
            "summary": msg,
            "findings": [{
                "severity": "high",
                "file": "",
                "line": None,
                "rule": "governance-review-unavailable",
                "title": "Governance review could not be completed",
                "detail": msg,
                "fix_hint": "Retry once the governance CLI/plugins are available on the host.",
                "snippet": "",
            }],
        })
    if not results:
        results.append({
            "skill": "governance", "verdict": "FAIL", "summary": msg,
            "findings": [{
                "severity": "high", "file": "", "line": None,
                "rule": "governance-review-unavailable",
                "title": "Governance review could not be completed",
                "detail": msg, "fix_hint": "Retry once available.", "snippet": "",
            }],
        })
    # _scan_error=True distinguishes "engine could not complete" from "completed scan
    # found real violations". Callers that see this flag should SUSPEND the run rather
    # than treating the synthetic FAIL finding as a real governance violation.
    return {"overall_verdict": "FAIL", "_scan_error": True, "skills": results}


def run_review(*, engine, workspace_root: str, diff_text: str, changed_files: list,
               skills: list, marketplace: str, model: str, run_id: str = "",
               ref: str = "", max_turns: Optional[int] = None) -> dict:
    """Run the governance review CLI session and return a GOVERNANCE_SCHEMA-shaped
    dict. Fail-CLOSED: a suspended/errored/unparseable result → a synthetic blocking
    FAIL (never a silent pass). Never raises.

    Skill loading: there is NO CLI plugin channel on the deployed binary (verified),
    so each selected skill's FULL folder is staged READ-ONLY into the workspace (see
    stage_skill_readonly) and the prompt points the CLI at those folders — the model
    reads the SKILL.md's itself. The dead `plugins=`/`plugin_marketplace=` args are no
    longer passed to the engine. `marketplace` is accepted for call-site back-compat
    but unused. Write bits on the staged tree are restored after the session so a later
    workspace teardown can't trip over the read-only dirs."""
    if max_turns is None:
        max_turns = gov_config.review_turns()

    # Stage every selected skill read-only INTO the workspace (all skills → one review
    # session). staged = [(skill, rel)]; _staged_abs collected for post-session restore.
    staged = []
    _staged_abs = []
    for s in (skills or []):
        rel = stage_skill_readonly(workspace_root, s)
        if rel:
            staged.append((s, rel))
            _staged_abs.append(os.path.join(workspace_root, rel))

    # Stage the diff to a workspace file and reference its PATH in the prompt (never
    # inline the possibly-multi-MB diff — that overflows the CLI --print argv token).
    # "" on failure → build_review_prompt falls back to inlining.
    diff_rel = stage_diff_file(workspace_root, diff_text, tag="review")
    _prompt = build_review_prompt(diff_text, changed_files, skills, ref,
                                  staged=staged, diff_path=diff_rel)
    logger.info(
        "[SDLC-GOV] run_review dispatch",
        run_id=run_id, ref=ref, model=model, max_turns=max_turns,
        skills=[getattr(s, "slug", "") for s in (skills or [])],
        staged=[rel for _, rel in staged],
        changed_files=list(changed_files or []),
        diff_chars=len(diff_text or ""),
        prompt_preview=(_prompt[:500] + "…") if len(_prompt) > 500 else _prompt,
    )
    try:
        result = engine.run(
            workspace_root=workspace_root,
            prompt=_prompt,
            profile="govreview",
            model=model,
            output_schema=GOVERNANCE_SCHEMA,
            max_turns=max_turns,
            run_id=run_id,
        )
    except Exception as e:
        logger.error("[SDLC-GOV] run_review fail-closed (exception)", run_id=run_id,
                     subtype="exception", error=str(e))
        return _fail_closed_structured(skills, f"governance review call errored: {e}")
    finally:
        # Restore write bits on the staged skill tree (read-only only needs to hold
        # DURING the session) so a later workspace teardown / rmtree isn't blocked.
        # `finally` covers the success path AND the except's early return.
        for _abs in _staged_abs:
            if os.path.isdir(_abs):
                _chmod_tree(_abs, writable=True)

    # Accrue this review session's cost onto the run's HOD budget (see run_scan_session).
    try:
        from agents.sdlc_cli_budget import record_cli_usage
        record_cli_usage(run_id, getattr(result, "usage", None) or {},
                         getattr(result, "total_cost_usd", 0.0) or 0.0)
    except Exception as _ce:
        logger.warning("[SDLC-GOV] review cost accrual failed (non-fatal)",
                       run_id=run_id, error=str(_ce))

    if getattr(result, "status", "") == "suspended" or getattr(result, "is_error", False) \
            or not isinstance(getattr(result, "structured_output", None), dict):
        _sub = getattr(result, "subtype", "") or ("suspended" if getattr(result, "suspended", False)
                                                   else "no_structured_output")
        logger.error("[SDLC-GOV] run_review fail-closed (no/invalid structured_output)",
                     run_id=run_id, subtype=_sub, reason=getattr(result, "reason", ""))
        return _fail_closed_structured(skills, f"governance review unavailable: {getattr(result, 'reason', '') or _sub}")

    structured = result.structured_output
    logger.info("[SDLC-GOV] run_review verdict", run_id=run_id,
                skills=[getattr(s, "slug", "") for s in (skills or [])],
                overall_verdict=overall_verdict_of(structured),
                findings=len(parse_findings(structured)))
    return structured


# ════════════════════════════════════════════════════════════════════════════
# Agentic per-skill scan sessions (Step 2)
# ════════════════════════════════════════════════════════════════════════════

def run_scan_session(*, engine, skill, workspace_root: str, bundle_dir: str,
                     diff_text: str, changed_files: list, base_sha: str,
                     model: str, run_id: str = "", max_turns: int = 60) -> dict:
    """Run one agentic CLI scan session for a single skill. Returns a
    GOVERNANCE_SCHEMA-shaped dict. Fail-CLOSED: any error/suspend/missing-output
    → _fail_closed_structured([skill], msg). Never raises.

    Unlike run_review, this path passes NO plugins/plugin_marketplace — the
    'govscan' profile uses Bash to run the skill's analyzer scripts directly (the
    CLI has no headless plugin-loading channel; see resolve_awareness). Also
    guards against the session mutating tracked files: a scan is read-only, so any
    dirty tracked file is discarded (`git checkout -- .`) after the session."""
    profile = gov_config.scan_profile() or "govscan"
    # Stage the skill's full folder read-only INSIDE the workspace so the headless
    # CLI (file tools jailed to cwd) can read SKILL.md + all subfolders itself,
    # untruncated. "" on failure → build_scan_prompt falls back to an inline excerpt.
    staged_rel = stage_skill_readonly(workspace_root, skill)
    _staged_abs = os.path.join(workspace_root, staged_rel) if staged_rel else ""
    # Stage the diff to a per-skill workspace file (tag=slug → no race across the
    # parallel scan sessions) and reference its PATH in the prompt, never inline it.
    diff_rel = stage_diff_file(workspace_root, diff_text, tag=(getattr(skill, "slug", "") or "scan"))
    # Per-run analyzer scratch dir, OUTSIDE the checkout and under BUILDER_WORKSPACE_ROOT
    # (never /tmp, never the tracked workspace). Created here so the analyzer's
    # `--output <scratch>/...` write succeeds.
    try:
        from core.config import BUILDER_WORKSPACE_ROOT as _BR
        scratch_dir = os.path.join(_BR, "gov_scratch", run_id or "norun")
        os.makedirs(scratch_dir, exist_ok=True)
    except Exception:
        scratch_dir = ""
    _prompt = build_scan_prompt(skill, diff_text, changed_files, base_sha,
                                workspace_root, bundle_dir, staged_rel=staged_rel,
                                diff_path=diff_rel, scratch_dir=scratch_dir)
    logger.info(
        "[SDLC-GOV] run_scan_session start",
        run_id=run_id, skill=skill.slug, domain=skill.domain,
        bundle_dir=bundle_dir, staged_rel=staged_rel,
        workspace_root=workspace_root,
        changed_files=len(changed_files or []),
        diff_chars=len(diff_text or ""),
        model=model, max_turns=max_turns, profile=profile,
        prompt_preview=(_prompt[:500] + "…") if len(_prompt) > 500 else _prompt,
    )
    try:
        result = engine.run(
            workspace_root=workspace_root,
            prompt=_prompt,
            profile=profile,
            model=model,
            output_schema=GOVERNANCE_SCHEMA,
            max_turns=max_turns,
            run_id=run_id,
        )
    except Exception as e:
        logger.error("[SDLC-GOV] run_scan_session fail-closed", run_id=run_id,
                     skill=skill.slug, subtype="exception", reason=str(e))
        if _staged_abs:
            _chmod_tree(_staged_abs, writable=True)  # restore so workspace teardown can delete
        return _fail_closed_structured([skill], f"scan session unavailable: {e}")

    # Accrue this scan session's token/cost onto the run's HOD budget counters —
    # the SAME accounting every other SDLC CLI call site uses (PLAN/IMPLEMENT/
    # REVIEW). Governance scans consume real LLM tokens, so they must be billed
    # too; without this the run's total_cost_usd stays 0 and the HOD ledger entry
    # at run-end records a $0 deduction. Best-effort — never fail a scan on it.
    try:
        from agents.sdlc_cli_budget import record_cli_usage
        record_cli_usage(run_id, getattr(result, "usage", None) or {},
                         getattr(result, "total_cost_usd", 0.0) or 0.0)
    except Exception as _ce:
        logger.warning("[SDLC-GOV] scan cost accrual failed (non-fatal)",
                       run_id=run_id, skill=skill.slug, error=str(_ce))

    # Post-scan guard: a scan must not mutate the workspace. Discard any tracked-file
    # changes the session made so the diff under review stays exactly as it was.
    try:
        import subprocess
        _chk = subprocess.run(["git", "diff", "--quiet"], cwd=workspace_root,
                              capture_output=True, timeout=30)
        if _chk.returncode != 0:
            _dirty = subprocess.run(["git", "diff", "--name-only"], cwd=workspace_root,
                                    capture_output=True, text=True, timeout=30).stdout.strip()
            logger.warning("[SDLC-GOV] scan mutated tracked files — discarding",
                           run_id=run_id, skill=skill.slug, files=_dirty)
            subprocess.run(["git", "checkout", "--", "."], cwd=workspace_root,
                           capture_output=True, timeout=30)
    except Exception as _ge:
        logger.warning("[SDLC-GOV] post-scan git check failed", run_id=run_id,
                       skill=skill.slug, error=str(_ge))
    finally:
        # The read-only guarantee only needs to hold DURING the scan session. Restore
        # write bits now so a later shutil.rmtree of the workspace can't trip over the
        # 0o555/0o444 staged tree (the dir is git-excluded, so this touches no diff).
        if _staged_abs and os.path.isdir(_staged_abs):
            _chmod_tree(_staged_abs, writable=True)

    if getattr(result, "status", "") == "suspended" or getattr(result, "is_error", False) \
            or not isinstance(getattr(result, "structured_output", None), dict):
        _sub = getattr(result, "subtype", "") or ("suspended" if getattr(result, "suspended", False)
                                                   else "no_structured_output")
        _reason = getattr(result, "reason", "") or _sub
        logger.error("[SDLC-GOV] run_scan_session fail-closed", run_id=run_id,
                     skill=skill.slug, subtype=_sub, reason=_reason)
        return _fail_closed_structured([skill], f"scan session unavailable: {_reason}")

    structured = result.structured_output
    # Tag every finding with the owning skill's domain (IS/EA/DPDP) for grouping in
    # the report. The GOVERNANCE_SCHEMA has no `domain` property (additionalProperties
    # is False), so this is a platform-side annotation on the already-validated dict.
    domain = getattr(skill, "domain", "") or ""
    try:
        for sk in (structured.get("skills") or []):
            if not isinstance(sk, dict):
                continue
            for f in (sk.get("findings") or []):
                if isinstance(f, dict):
                    f["domain"] = domain
    except Exception as _te:  # pragma: no cover - defensive; never fail the scan on tagging
        logger.warning("[SDLC-GOV] domain tagging failed", run_id=run_id,
                       skill=skill.slug, error=str(_te))

    logger.info("[SDLC-GOV] run_scan_session verdict", run_id=run_id, skill=skill.slug,
                domain=domain, overall_verdict=overall_verdict_of(structured),
                findings=len(parse_findings(structured)))
    return structured


def scan_all_skills(*, engine, bundle, skills: list, workspace_root: str,
                    diff_text: str, changed_files: list, base_sha: str,
                    model: str, run_id: str = "") -> tuple:
    """Run one scan session per skill in PARALLEL (ThreadPoolExecutor), merge the
    per-skill results, and return (merged_structured_output, domain_by_skill_map).

    Parallelism over a SHARED workspace_root is safe because: (1) each skill stages
    into its OWN distinct <ws>/.governance_skills/<slug>/ subdir (slug '/'→'__'), so
    concurrent copytree/chmod never touch the same path; (2) that staging tree is
    git-excluded and the scan is read-only by contract, so a session does not add
    tracked changes — `git diff --quiet` stays clean and the post-scan `git checkout`
    guard does not fire (no concurrent index.lock contention in practice); (3) the one
    shared-file mutation (the .git/info/exclude append) is serialized by _STAGE_LOCK.
    Any temp output from analyzer scripts goes to a scratch dir OUTSIDE the workspace
    (BUILDER_WORKSPACE_ROOT/gov_scratch/{run_id}), never into it. NOTE: the
    'govscan' profile's permission_mode="plan" is NOT relied on for read-only — that
    flag is not enforced by the deployed binary (verified); read-only of the skill
    materials is enforced at the filesystem level by stage_skill_readonly.

    Concurrency cap: SDLC_GOVERNANCE_SCAN_WORKERS (default 4) via config.scan_workers().
    Set to 1 to force sequential execution (e.g. for debugging).

    Never raises. Fail-closed is inherited from run_scan_session: a skill whose
    session could not complete contributes a blocking FAIL entry, so the merged
    verdict is FAIL if ANY skill FAILs."""
    import concurrent.futures as _cf
    max_turns = gov_config.scan_turns()
    n_workers_cfg = gov_config.scan_workers()
    n_skills = len(skills or [])
    n_workers = min(n_workers_cfg, n_skills) if n_workers_cfg > 0 else n_skills
    n_workers = max(n_workers, 1)
    bundle_dir = getattr(bundle, "dir", "") or ""
    logger.info(
        "[SDLC-GOV] scan_all_skills start",
        run_id=run_id, bundle_dir=bundle_dir,
        skills_count=n_skills,
        skills=[getattr(s, "slug", "") for s in (skills or [])],
        workspace_root=workspace_root,
        parallel_workers=n_workers,
    )

    def _run_one(skill):
        return skill, run_scan_session(
            engine=engine, skill=skill, workspace_root=workspace_root,
            bundle_dir=bundle_dir, diff_text=diff_text, changed_files=changed_files,
            base_sha=base_sha, model=model, run_id=run_id, max_turns=max_turns,
        )

    raw_results: list = []
    with _cf.ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(_run_one, skill): skill for skill in (skills or [])}
        for future in _cf.as_completed(futures):
            try:
                raw_results.append(future.result())
            except Exception as exc:
                # run_scan_session never raises, but be defensive
                skill = futures[future]
                logger.error("[SDLC-GOV] scan_all_skills unexpected future error",
                             run_id=run_id, skill=getattr(skill, "slug", ""), error=str(exc))
                raw_results.append((skill, _fail_closed_structured([skill], str(exc))))

    domain_by_skill: dict = {}
    merged_skills: list = []
    all_findings: list = []
    has_scan_error = False
    for skill, structured in raw_results:
        # If any skill session could not complete (max_turns / CLI error), propagate
        # the _scan_error flag so run_governance_pipeline can SUSPEND instead of
        # treating the synthetic fail-closed finding as a real violation.
        if isinstance(structured, dict) and structured.get("_scan_error"):
            has_scan_error = True
        domain_by_skill[skill.slug] = skill.domain
        if isinstance(structured, dict):
            for sk in (structured.get("skills") or []):
                if isinstance(sk, dict):
                    merged_skills.append(sk)
        all_findings.extend(parse_findings(structured))

    overall = "FAIL" if any(
        str((sk or {}).get("verdict") or "").strip().upper() == "FAIL" for sk in merged_skills
    ) else "PASS"
    merged_structured = {"overall_verdict": overall, "skills": merged_skills}
    if has_scan_error:
        merged_structured["_scan_error"] = True
    logger.info("[SDLC-GOV] scan_all_skills merge", run_id=run_id,
                skills=n_skills, workers=n_workers, findings=len(all_findings))
    return merged_structured, domain_by_skill


# ════════════════════════════════════════════════════════════════════════════
# Suppression filter
# ════════════════════════════════════════════════════════════════════════════

def apply_suppressions(findings: List[Finding], db, product_id: Optional[str], repo: str):
    """Split findings into (open, suppressed). A finding is suppressed when an ACTIVE
    row in sdlc_governance_suppressions matches EITHER the legacy tuple
    (product_id [NULL-safe], repo, skill, fingerprint) OR the new content tuple
    (product_id [NULL-safe], repo, content_key). The content key (gvc1:…) is
    skill-independent, so a suppression written after cross-domain approval hides
    the same code issue in ANY domain in future runs. Legacy gv1: rows keep
    matching on the (skill, fingerprint) tuple. Suppressed findings are retained
    (status="suppressed") for the report. Never raises — a DB error means treat
    everything as open (fail toward surfacing findings, not hiding them)."""
    open_f: List[Finding] = []
    suppressed_f: List[Finding] = []
    suppressed_keys = set()
    suppressed_content = set()
    if db is not None and repo:
        try:
            from sqlalchemy import text
            rows = db.execute(
                text(
                    "SELECT skill, fingerprint, content_key FROM sdlc_governance_suppressions "
                    "WHERE active = TRUE AND pending_signoff = FALSE AND repo_name = :repo "
                    "AND (product_id IS NOT DISTINCT FROM :pid)"
                ),
                {"repo": repo, "pid": product_id},
            ).fetchall()
            suppressed_keys = {(r[0], r[1]) for r in rows}
            suppressed_content = {r[2] for r in rows if r[2]}
        except Exception as e:
            logger.warning("[SDLC-GOV] suppression load failed — treating all findings as open",
                           repo=repo, error=str(e))
            suppressed_keys = set()
            suppressed_content = set()
    for f in (findings or []):
        fp = fingerprint(f)
        ck = content_fingerprint(f)
        if (f.skill, fp) in suppressed_keys or ck in suppressed_content:
            f.status = "suppressed"
            suppressed_f.append(f)
        else:
            f.status = "open"
            open_f.append(f)
    logger.info("[SDLC-GOV] suppression filter", repo=repo,
                open=len(open_f), suppressed=len(suppressed_f))
    return open_f, suppressed_f


def resolve_product_id(db, repo: str) -> Optional[str]:
    """Map a repo name to its product_id via product_repos (NULL if unmapped).
    Never raises."""
    if db is None or not repo:
        return None
    try:
        from sqlalchemy import text
        row = db.execute(
            text("SELECT product_id FROM product_repos WHERE repo_name = :r LIMIT 1"),
            {"r": repo},
        ).fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.debug(f"[SDLC-GOV] resolve_product_id failed for {repo}: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# Fixer prompt
# ════════════════════════════════════════════════════════════════════════════

def build_fix_prompt(open_findings: List[Finding], workspace_root: str = "") -> str:
    """The governance fixer prompt: fix ONLY the flagged findings, minimal diff, no
    scope creep. Reuses the shared workspace-boundary + STOP contracts so the fixer
    session terminates cleanly."""
    import json as _json
    from agents.sdlc_implement_prompt import workspace_boundary_clause, implement_stop_clause
    by_skill: dict = {}
    for f in (open_findings or []):
        by_skill.setdefault(f.skill, []).append({
            "severity": f.severity,
            "file": f.file,
            "line": f.line,
            "rule": f.rule,
            "title": f.title,
            "detail": f.detail,
            "fix_hint": f.fix_hint,
            "snippet": f.snippet,
        })
    body = _json.dumps(by_skill, indent=2, default=str)
    return (
        "The loaded EA / IS / DPDP governance skills flagged NEW violations in your diff. "
        "Fix ONLY these governance findings in the CODE: make the SMALLEST change that "
        "resolves each one, do NOT expand scope, and do NOT weaken, delete, or edit "
        "existing tests."
        f"{workspace_boundary_clause(workspace_root)}\n\n"
        f"GOVERNANCE FINDINGS TO FIX (grouped by skill):\n{body}\n"
        f"{implement_stop_clause(done_condition='the flagged governance findings are resolved and the code compiles')}"
    )


# ════════════════════════════════════════════════════════════════════════════
# Report rendering
# ════════════════════════════════════════════════════════════════════════════

def render_report(*, structured: dict, findings: List[Finding], ref: str,
                  skills: list, iterations: int = 1, domain_by_skill: dict = None) -> dict:
    """Build the per-skill governance report (for artifact / UI / MR note / file).

    `findings` is the union of open + suppressed (+ fixed) Finding objects with their
    `status` stamped. Per-skill verdict is FAIL iff it has any OPEN finding; suppressed
    findings are shown but do not fail the skill. Returns a dict with `report_md`.

    `domain_by_skill` (optional): maps skill slug/name → domain (IS/EA/DPDP), used to
    tag each skill entry and group the markdown report under domain headers."""
    struct = structured if isinstance(structured, dict) else {}
    struct_skills = {
        str(sk.get("skill")): sk
        for sk in (struct.get("skills") or []) if isinstance(sk, dict) and sk.get("skill")
    }
    per_skill: dict = {}
    for f in (findings or []):
        per_skill.setdefault(f.skill, []).append(f.to_dict())

    all_names = set(per_skill) | set(struct_skills) | {
        (getattr(s, "slug", None) or getattr(s, "name", None)) for s in (skills or [])
    }
    all_names = {n for n in all_names if n}

    # Deterministic display order: severity DESC, then file / line (NULLS LAST) /
    # fingerprint ASC as tie-breakers, so the report is byte-stable across
    # re-renders and mirrors the findings API order.
    def _md_order_key(x):
        line = x.get("line")
        return (
            -severity_rank(x.get("severity")),
            x.get("file") or "",
            (line is None, line if line is not None else 0),
            x.get("fingerprint") or "",
        )

    skills_list = []
    for name in sorted(all_names):
        fs = per_skill.get(name, [])
        fs = sorted(fs, key=_md_order_key)
        open_n = sum(1 for x in fs if x.get("status") == "open")
        verdict = "FAIL" if open_n else "PASS"
        summary = str((struct_skills.get(name, {}) or {}).get("summary") or "")
        domain = (domain_by_skill or {}).get(name, "")
        skills_list.append({
            "skill": name,
            "domain": domain,
            "verdict": verdict,
            "summary": summary,
            "open": open_n,
            "suppressed": sum(1 for x in fs if x.get("status") == "suppressed"),
            "findings": fs,
        })

    overall = "FAIL" if any(s["verdict"] == "FAIL" for s in skills_list) else "PASS"
    report_md = _render_md(overall, ref, skills_list, iterations)
    return {
        "overall_verdict": overall,
        "ref": ref,
        "iterations": iterations,
        "skills": skills_list,
        "report_md": report_md,
    }


def _render_md(overall: str, ref: str, skills_list: list, iterations: int) -> str:
    """Render the governance report as Markdown (MR note / UI / standalone file)."""
    lines = [
        "## Governance Review",
        "",
        f"**Overall verdict:** {overall}  ",
        f"**Bundle ref:** `{ref or 'n/a'}`  ",
        f"**Fix iterations:** {iterations}",
        "",
    ]
    if not skills_list:
        lines.append("_No governance skills were evaluated._")
        return "\n".join(lines)

    # Group skills by domain (IS / EA / DPDP / …). Skills with no domain fall under
    # a generic "Other" header. Domains render in a stable order (known domains first,
    # then any others alphabetically, then the unclassified bucket last).
    by_domain: dict = {}
    for s in skills_list:
        dom = (s.get("domain") or "").strip().upper()
        by_domain.setdefault(dom, []).append(s)

    _KNOWN_ORDER = ["IS", "EA", "DPDP"]
    domains = [d for d in _KNOWN_ORDER if d in by_domain]
    domains += sorted(d for d in by_domain if d and d not in _KNOWN_ORDER)
    if "" in by_domain:
        domains.append("")

    for dom in domains:
        lines.append(f"### Domain: {dom or 'Other'}")
        lines.append("")
        for s in by_domain[dom]:
            lines.append(f"#### {s['skill']} — {s['verdict']}")
            if s.get("summary"):
                lines.append(f"{s['summary']}")
            lines.append(f"_open: {s.get('open', 0)} · suppressed: {s.get('suppressed', 0)}_")
            lines.append("")
            for f in s.get("findings", []):
                status = f.get("status", "open")
                loc = f.get("file") or ""
                if f.get("line"):
                    loc = f"{loc}:{f['line']}"
                lines.append(
                    f"- **[{(f.get('severity') or '').upper()}]** {f.get('title') or f.get('rule') or 'finding'} "
                    f"({status}) — `{loc}`"
                )
                if f.get("detail"):
                    lines.append(f"  - {f['detail']}")
                if f.get("fix_hint"):
                    lines.append(f"  - _fix:_ {f['fix_hint']}")
            lines.append("")
    return "\n".join(lines)
