# SPDX-License-Identifier: Apache-2.0
"""SDLC pipeline phase functions.

Normalize / validate / explore gates, the PLAN driver (read-only ainxt CLI
planner), pre-gate codegen, the platform REVIEW phase, and the governance
review + scan-snapshot entrypoints.

Extracted from ``agents/sdlc_pipeline/_core.py`` on 2026-08-04 as stage 2 of the
Part B decomposition. Every name here is re-exported through the
``agents.sdlc_pipeline`` package facade, so existing lazy imports such as
``from agents.sdlc_pipeline import _run_review_phase`` (state machine) resolve
unchanged. Shared plumbing is imported from the ``._core`` leaf at module scope;
``_core`` in turn imports the handful of phase entrypoints it drives *lazily*
(inside functions), which keeps the module load order acyclic.
"""

import json
import os

from core.logger import logger
from store.sdlc_store import (
    create_run,
    get_run,
    update_run_state,
    add_run_event,
    patch_run_context,
    SDLCCancelled,
)

# Shared helpers/plumbing that stay in the _core leaf. Imported from the SUBMODULE
# (not the package facade) so the load stays acyclic: _core is fully initialised
# before this module is ever loaded (the facade loads _core first and resolves
# phase names lazily via __getattr__; _core references phase funcs lazily too).
from agents.sdlc_pipeline._core import (
    _event,
    _filter_noneditable_files,
    _jira_comment,
    _llm,
    _materialize_early_workspace,
    _parse_json,
    _read_jira_full,
    _s,
    _sdlc_model,
    _self_review,
    _setup_multi_repo_workspace_for_plan,
    _transition,
)


def _resolve_required_against_workspace(expected, workspace_root: str = "",
                                        new_files=None) -> tuple:
    """Step 2: filter the required-read set to REALITY against the materialized
    workspace checkout (NOT GitLab).

    Drops paths absent from disk (foreign/hallucinated paths like ABStudio/… can
    never be read, so requiring them yields FALSE incompleteness) and excludes any
    path in `new_files` (a file to be CREATED can't be read). Returns
    (kept: list[str], dropped_nonexistent: list[str], excluded_new: list[str]).

    Conservative: when `workspace_root` is empty/missing we cannot verify existence
    on disk, so we keep every non-new path (prior behavior) rather than over-filter
    and re-introduce an ungrounded analysis. Pure / Windows-safe."""
    import os as _os
    _new = {(_s(p) or "").replace("\\", "/").lstrip("/")
            for p in (new_files or []) if isinstance(p, str) and p.strip()}
    kept: list = []
    dropped_nonexistent: list = []
    excluded_new: list = []
    for p in (expected or []):
        if not isinstance(p, str) or not p.strip():
            continue
        rel = p.replace("\\", "/").lstrip("/")
        if rel in _new:
            excluded_new.append(p)
            continue
        if workspace_root:
            full = _os.path.join(workspace_root, rel)
            if not _os.path.isfile(full):
                dropped_nonexistent.append(p)
                continue
        kept.append(p)
    return kept, dropped_nonexistent, excluded_new


# ============================================================
# New pipeline stage functions (Part 3)
# ============================================================

def _phase_normalize_ticket(run_id: str, jira_key: str, issue: dict,
                             repo_ctx: dict, workspace_root: str = "") -> tuple:
    """TICKET_NORMALIZATION stage — convert raw Jira ticket to structured WorkItem.

    Returns (work_item, open_questions). If open_questions is non-empty,
    the pipeline transitions to AWAITING_USER_INPUT; the caller persists
    the work_item + open_questions and pauses.

    If jira_get_issue_full fails, falls back to jira_dict built from issue.
    """
    from agents.sdlc_normalizer import NormalizationAgent
    _transition(run_id, "TICKET_NORMALIZATION", "normalizer-agent")
    user_id = (
        issue.get("triggered_by_user_id", "")
        or issue.get("user_id", "")
    )
    user_email = (
        issue.get("triggered_by_email", "")
        or issue.get("user_email", "")
    )
    try:
        from tools.jira_tools import jira_get_issue_full
        jira_dict = jira_get_issue_full(jira_key, user_id=user_id, user_email=user_email)
    except Exception as _je:
        logger.warning(f"[NORM {run_id}] jira_get_issue_full failed ({_je}) — using issue dict")
        jira_dict = {}
    if not jira_dict:
        # jira_get_issue_full returned empty (Jira not reachable or creds missing);
        # try _read_jira_full which has _get_run_user() fallback built in.
        try:
            jira_dict = _read_jira_full(jira_key, user_id=user_id, user_email=user_email)
        except Exception as _rjf_err:
            logger.warning(f"[NORM {run_id}] _read_jira_full also failed ({_rjf_err}) — using raw issue dict")
            jira_dict = {}
    if not jira_dict:
        jira_dict = {
            "key": jira_key,
            "summary": issue.get("summary", ""),
            "description": issue.get("description", ""),
            "comments": [],
            "acceptance_criteria": "",
            "labels": [],
            "epic_summary": "",
            "raw_fields": {},
        }
    agent = NormalizationAgent(run_id=run_id)
    work_item, open_questions = agent.normalize(jira_dict, repo_ctx, workspace_root)
    return work_item, open_questions


def _phase_validate_manifest(run_id: str, jira_key: str, work_item_dict: dict,
                              design: dict, analysis: dict,
                              workspace_root: str = "") -> tuple:
    """MANIFEST_VALIDATION stage — structural + OpenAI cross-check of the change manifest.

    Returns (passed: bool, issues: list[str]).
    Structural check is deterministic (no LLM) and is the authoritative gate.
    OpenAI cross-check is gated by SDLC_MANIFEST_VALIDATION_ENABLED (default FALSE —
    opt-in only; it re-decided scope subjectively and caused frequent false rejects).
    """
    _transition(run_id, "MANIFEST_VALIDATION", "manifest-validator")
    issues = []
    file_changes = (design.get("file_changes") or design.get("files_to_change") or
                    analysis.get("files_to_change") or [])
    new_files = design.get("new_files_needed") or analysis.get("new_files_needed") or []
    affected = analysis.get("affected_components") or []
    # The PLAN dict (passed as both `design` and `analysis`) carries the reasoning the
    # cross-check needs to avoid false positives — it was previously discarded:
    #   • ruled_out: files the plan DELIBERATELY excluded, with a reason. A scope-listed
    #     file that appears here is NOT "missing" — it was a reasoned decision.
    #   • solution_approach / code_structure: WHY each manifest file is touched, so
    #     mandatory companion files (e.g. db/migrate.py for a schema change) are not
    #     read as out-of-scope just because the scope bullets didn't enumerate them.
    ruled_out = design.get("ruled_out") or analysis.get("ruled_out") or []
    solution_approach = _s(design.get("solution_approach") or analysis.get("solution_approach") or "")
    code_structure = _s(design.get("code_structure") or analysis.get("code_structure") or "")

    # WS-3/WS-4 (gate-reorder, 2026-07-02): promote MANIFEST_VALIDATION to a
    # first-class stage that STORES an artifact (P5 — the verdict was previously
    # never persisted/shown), matching the ManifestValidationPanel.jsx contract:
    # struct_pass, openai_pass, struct_failures, hallucinated_paths,
    # missing_components, oos_violations, openai_issues. `_finish` wraps every
    # return point so the return-shape contract (passed, issues) stays unchanged
    # for the one existing caller (_run_plan_phase).
    def _finish(passed: bool, out_issues: list, **details) -> tuple:
        try:
            from store.sdlc_artifacts import _store_artifact as _mv_sa, compute_input_hash as _mv_cih
            _mv_sa(
                run_id=run_id, stage="MANIFEST_VALIDATION",
                payload={"passed": passed, "issues": out_issues, **details},
                producer="ai:manifest-validator",
                input_hash=_mv_cih(run_id, "MANIFEST_VALIDATION"),
                created_by="system",
            )
        except Exception as _mvae:
            logger.warning(f"[SDLC {run_id}] MANIFEST_VALIDATION artifact store failed (non-fatal): {_mvae}")
        return passed, out_issues

    # WS-3: for complexity=="simple" runs, skip the OpenAI cross-check (Step 2) —
    # the structural check alone is enough signal for a small, low-blast-radius
    # change. Read complexity off the run's stored classification.
    _complexity = str(
        (get_run(run_id) or {}).get("context", {}).get("classification", {}).get("complexity") or ""
    ).strip().lower()

    # ── FULL INPUT DUMP (no stripping) — so a structural failure (which returns
    #    before the LLM prompt is built) is just as diagnosable as an LLM reject.
    try:
        _input_dump = json.dumps({
            "work_item": work_item_dict,
            "file_changes": file_changes,
            "new_files_needed": new_files,
            "affected_components": affected,
            "workspace_root": workspace_root,
        }, indent=2, default=str)
    except Exception as _de:
        _input_dump = f"<unserializable inputs: {_de}>"
    logger.info(
        f"[MANIFEST-INPUT {run_id}] ===== FULL VALIDATION INPUT BEGIN =====\n"
        f"{_input_dump}\n"
        f"[MANIFEST-INPUT {run_id}] ===== FULL VALIDATION INPUT END ====="
    )

    # ── Step 1: Structural validation (deterministic, no LLM) ──
    # Path existence is decided HERE, against real disk — this is the authoritative,
    # deterministic hallucination check. The Step-2 LLM cross-check no longer judges
    # path existence (it saw a pruned/head-capped tree and false-flagged real
    # root-level files like gateway.py); struct_missing_paths is what feeds the
    # artifact's hallucinated_paths field.
    covered_affected = set()
    struct_missing_paths: list = []
    for fc in file_changes:
        path = _s(fc.get("path") or fc) if isinstance(fc, dict) else _s(fc)
        if not path:
            continue
        # Check file exists in workspace
        if workspace_root:
            import os as _os
            full = _os.path.join(workspace_root, path.lstrip("/"))
            exists = _os.path.isfile(full)
        else:
            exists = True  # no workspace: cannot verify, assume OK
        logger.info(f"[MANIFEST-STRUCT {run_id}] path_check: {path!r} → exists={exists}")
        if not exists and path not in (new_files or []):
            issues.append(f"path not found in workspace: {path}")
            struct_missing_paths.append(path)
        # Check affected component coverage
        for af in affected:
            af_s = _s(af)
            if af_s and (af_s in path or path.endswith(af_s)):
                covered_affected.add(af_s)
        # Check non-empty change_description
        if isinstance(fc, dict):
            if not (fc.get("change_description") or fc.get("description") or fc.get("reason")):
                issues.append(f"missing change_description for: {path}")

    n_covered = len(covered_affected)
    n_total = len(affected)
    logger.info(f"[MANIFEST-STRUCT {run_id}] coverage: {n_covered}/{n_total} affected_components covered")
    struct_pass = len(issues) == 0
    logger.info(f"[MANIFEST-STRUCT {run_id}] result: pass={struct_pass} failures={len(issues)}")

    if not struct_pass:
        return _finish(False, issues, struct_pass=False, openai_pass=None,
                       struct_failures=issues, hallucinated_paths=struct_missing_paths)

    # ── Step 2: OpenAI cross-validation (gated; skipped for simple complexity) ──
    # DEFAULT OFF (2026-08-16): the Step-1 structural check (disk path existence,
    # change_description, affected-component coverage) is the authoritative,
    # deterministic gate. The Step-2 LLM scope cross-check re-decided scope
    # subjectively across runs and drove frequent FALSE rejects (documented history
    # of false-flagging real files). It is retained behind the flag for opt-in only —
    # set SDLC_MANIFEST_VALIDATION_ENABLED=true to re-enable.
    enabled = os.getenv("SDLC_MANIFEST_VALIDATION_ENABLED", "false").lower() not in ("false", "0", "no")
    if not enabled:
        logger.info(f"[MANIFEST-OPENAI {run_id}] SDLC_MANIFEST_VALIDATION_ENABLED=false — skipping")
        return _finish(True, [], struct_pass=True, openai_pass=None, struct_failures=[])
    if _complexity == "simple":
        logger.info(f"[MANIFEST-OPENAI {run_id}] complexity=simple — skipping OpenAI cross-check (WS-3)")
        return _finish(True, [], struct_pass=True, openai_pass=None, struct_failures=[], skipped_reason="simple_complexity")

    try:
        manifest_summary = json.dumps({
            "file_changes": [_s(fc) if not isinstance(fc, dict) else fc for fc in file_changes[:20]],
            "new_files_needed": [_s(f) for f in new_files[:10]],
        }, indent=2)
        wi_problem = work_item_dict.get("problem_statement", "")
        wi_scope = work_item_dict.get("scope", [])
        wi_oos = work_item_dict.get("out_of_scope", [])
        # Path existence is NOT judged here. Step 1 already verified every manifest
        # path against real disk (the authoritative, deterministic check). This LLM
        # cross-check used to receive a repo file tree and flag "hallucinated" paths
        # against it — but the tree was pruned/head-capped (≤40K) and routinely dropped
        # real root-level files (e.g. gateway.py), false-rejecting a valid manifest.
        # The tree and the hallucinated_paths judgement are gone; the cross-check now
        # judges ONLY scope adherence and affected-component coverage.
        # ── Build the two reasoning blocks that stop the historical false positives ──
        # (1) DELIBERATELY EXCLUDED: a scope-listed file that the plan ruled out (with a
        #     reason) is NOT missing — surfacing this stops the "auth/rbac.py missing"
        #     class of false reject, where the plan verified an already-correct file.
        if ruled_out:
            _ro_lines = []
            for _r in ruled_out:
                if isinstance(_r, dict):
                    _rp = _s(_r.get("path") or _r.get("file") or "")
                    _rr = _s(_r.get("reason") or _r.get("rationale") or "")
                    if _rp:
                        _ro_lines.append(f"  - {_rp}: {_rr}" if _rr else f"  - {_rp}")
                elif _s(_r):
                    _ro_lines.append(f"  - {_s(_r)}")
            ruled_out_block = (
                "Files the plan DELIBERATELY EXCLUDED (do NOT report these as missing — "
                "each was a reasoned decision, e.g. the file was inspected and needs no change):\n"
                + "\n".join(_ro_lines) + "\n\n"
            ) if _ro_lines else ""
        else:
            ruled_out_block = ""
        # (2) RATIONALE: why each manifest file is touched. Lets the reviewer see that a
        #     file not literally enumerated in the scope bullets (e.g. db/migrate.py, a
        #     SQL catch-up script, a test file) is a mandatory companion of an in-scope
        #     change — not an out-of-scope excursion.
        rationale_block = ""
        if solution_approach:
            rationale_block += f"Solution approach:\n{solution_approach[:1500]}\n\n"
        if code_structure:
            rationale_block += f"Per-file change rationale:\n{code_structure[:2500]}\n\n"
        cross_prompt = (
            f"You are a senior engineer reviewing a change manifest. Every file path has "
            f"ALREADY been verified to exist — do NOT judge or flag path existence.\n\n"
            f"Work Item Problem: {wi_problem}\n"
            f"In Scope: {wi_scope}\n"
            f"Out of Scope: {wi_oos}\n\n"
            f"{ruled_out_block}"
            f"{rationale_block}"
            f"Manifest (files to change):\n{manifest_summary}\n\n"
            f"Judge ONLY: (1) does any change fall MATERIALLY OUTSIDE the stated scope, and "
            f"(2) is any in-scope component MISSING from the manifest.\n"
            f"Judging rules — read carefully, these prevent false positives:\n"
            f"- The 'In Scope' list is HIGH-LEVEL GUIDANCE, not an exhaustive whitelist. "
            f"Mandatory companion files that directly serve an in-scope change are IN scope "
            f"even if not individually named — e.g. the DB migration runner (db/migrate.py) "
            f"and SQL catch-up scripts always accompany a schema change; test files always "
            f"accompany code changes. Do NOT flag these as out-of-scope.\n"
            f"- A file listed under 'DELIBERATELY EXCLUDED' above is NOT missing. Never "
            f"report a ruled-out file as a missing component.\n"
            f"- Only report an out-of-scope violation for a change that is UNRELATED to the "
            f"work item or explicitly named under 'Out of Scope'. When in doubt, do not flag.\n"
            f"Answer ONLY in JSON: "
            f'{{ "valid": true/false, "missing_components": [], '
            f'"out_of_scope_violations": [], "issues": [] }}'
        )
        # Step 1: resolve the judge model through config. _sdlc_model returns the
        # TIER (default "deep" → gpt-5.5, env-overridable via SDLC_MODEL_MANIFEST_VALIDATE);
        # openai_model_for_tier maps it to a CONCRETE OpenAI model id. This validator
        # calls the OpenAI gateway directly, so a Claude tier must fall back to the
        # latest OpenAI model — sending a Claude id (or None) 400s.
        from core.model_registry import openai_model_for_tier as _omft
        _mv_hint = _sdlc_model("manifest_validate")
        _mv_model, _mv_fellback = _omft(_mv_hint)
        _mv_source = (
            "fallback" if _mv_fellback
            else ("env" if os.getenv("SDLC_MODEL_MANIFEST_VALIDATE") else "default")
        )
        if _mv_fellback:
            logger.warning(
                "[MANIFEST-OPENAI] judge tier has no OpenAI model — using latest",
                run_id=run_id, requested_hint=_mv_hint, model=_mv_model,
            )
        logger.info(
            "[MANIFEST-OPENAI] judge model resolved",
            run_id=run_id, model=_mv_model, source=_mv_source,
        )
        prompt_chars = len(cross_prompt)
        logger.info(
            f"[MANIFEST-OPENAI {run_id}] sending manifest model={_mv_model} prompt_chars={prompt_chars}"
        )
        # ── FULL PROMPT DUMP (no stripping / no truncation) — for diagnosing why the
        #    manifest cross-check rejects. Delimited so it is easy to extract from logs.
        logger.info(
            f"[MANIFEST-OPENAI {run_id}] ===== FULL CROSS-VALIDATION PROMPT BEGIN "
            f"(chars={prompt_chars}) =====\n{cross_prompt}\n"
            f"[MANIFEST-OPENAI {run_id}] ===== FULL CROSS-VALIDATION PROMPT END ====="
        )
        # 2026-07-07 (scoped Fix 2) / 2026-07-13 (Step 1): call the resolved judge model
        # (_mv_model — default gpt-5.5) with its model id EXPLICITLY set for THIS
        # cross-check only. The shared medium router path (_try_openai_coding) sends
        # model=None to the proxy → 400 on every medium call; rather than change that
        # universal path, this best-effort validation calls the OpenAI gateway directly
        # with the model. On any call failure `raw` stays empty and the cross-check
        # SKIPs gracefully below (non-blocking gate).
        from models.model_router import model_router as _mr
        _gw = _mr._get_openai()

        def _judge_call(_p: str) -> str:
            """Call the OpenAI judge once with the resolved model; account cost;
            return raw text ('' on any failure — non-blocking)."""
            if _gw is None:
                logger.warning(f"[MANIFEST-OPENAI {run_id}] no OpenAI gateway available — cross-check skipped")
                return ""
            try:
                _r = _mr._collect(_gw.generate(_p, model=_mv_model)) or ""
            except Exception as _ce:
                logger.warning(f"[MANIFEST-OPENAI {run_id}] cross-check call failed (non-fatal): {_ce}")
                return ""
            # Best-effort cost/budget accounting (mirrors _llm's estimate) since this
            # bypasses _llm. Cost tier tracks the ACTUAL model used — deep rate when the
            # deep/fallback model runs (Step 1). Never fatal.
            try:
                from core.model_registry import tier_cost_per_1m as _tc
                _ti, _to = len(_p) // 4, (len(_r) // 4 if _r else 0)
                _ri, _ro = _tc("deep" if _mv_fellback else _mv_hint)
                _mv_cost = (_ti / 1_000_000 * _ri) + (_to / 1_000_000 * _ro)
                from services.sdlc_budget_tracker import record_llm_cost as _rec_cost
                _rec_cost(_ti, _to, round(_mv_cost, 6), run_id=run_id)
            except Exception:
                pass
            return _r

        # NOTE: the OpenAI gateway generate() exposes NO response_format / JSON-mode
        # kwarg (confirmed against _ProxyGateway.generate — the production path via
        # web02), so Step 2 relies on the strict-shape RE-ASK below rather than a
        # structured-output request.
        raw = _judge_call(cross_prompt)
        # ── FULL RESPONSE DUMP (no stripping / no truncation) ──
        logger.info(
            f"[MANIFEST-OPENAI {run_id}] ===== FULL CROSS-VALIDATION RESPONSE BEGIN "
            f"(chars={len(raw or '')}) =====\n{raw}\n"
            f"[MANIFEST-OPENAI {run_id}] ===== FULL CROSS-VALIDATION RESPONSE END ====="
        )
        # Step 7: do NOT fail open. A compliance-blocked or unparseable cross-check
        # yields NO verdict — skip it (non-blocking, best-effort), never silently
        # treat the absence of a verdict as a PASS.
        from agents.compliance_engine import is_compliance_block
        if is_compliance_block(raw):
            logger.warning(
                "[SDLC] manifest cross-check SKIPPED", run_id=run_id, reason="compliance_block"
            )
            logger.warning(f"[MANIFEST-OPENAI {run_id}] cross-check compliance-blocked — SKIPPED (no verdict)")
            return _finish(True, [], struct_pass=True, openai_pass=None, struct_failures=[], skipped_reason="compliance_block")
        result = _parse_json(raw)
        if not isinstance(result, dict) or "valid" not in result:
            # Step 2: ONE re-ask before giving up — the judge may have wrapped the JSON
            # in prose. Re-call appending a strict shape instruction (no response_format
            # kwarg exists on the gateway, so a re-ask is the only lever).
            logger.warning(f"[MANIFEST-OPENAI {run_id}] first verdict unparseable — re-asking once")
            _reask_prompt = (
                cross_prompt
                + "\n\nReturn ONLY a JSON object of the exact shape "
                  '{"valid": true/false, "missing_components": [], '
                  '"out_of_scope_violations": [], "issues": []} — no prose, no code fences.'
            )
            raw2 = _judge_call(_reask_prompt)
            logger.info(
                f"[MANIFEST-OPENAI {run_id}] ===== RE-ASK RESPONSE BEGIN "
                f"(chars={len(raw2 or '')}) =====\n{raw2}\n"
                f"[MANIFEST-OPENAI {run_id}] ===== RE-ASK RESPONSE END ====="
            )
            # A compliance-blocked re-ask is still NO verdict — never parse it as one.
            if is_compliance_block(raw2):
                logger.warning(
                    "[SDLC] manifest cross-check SKIPPED", run_id=run_id,
                    reason="compliance_block_on_retry",
                )
                return _finish(True, [], struct_pass=True, openai_pass=None,
                               struct_failures=[], skipped_reason="compliance_block")
            result = _parse_json(raw2)
            if not isinstance(result, dict) or "valid" not in result:
                # Still unparseable → keep the non-blocking SKIP (a flaky judge must not
                # dead-end the pipeline) BUT make it LOUD + ATTRIBUTED so the operator
                # sees the gate was effectively bypassed. This is "no silent skip".
                logger.warning(
                    "[MANIFEST-OPENAI] judge_unparseable_after_retry",
                    run_id=run_id, raw_len=len(raw or ""),
                    skipped_reason="unparseable_after_retry",
                )
                return _finish(True, [], struct_pass=True, openai_pass=None,
                               struct_failures=[], skipped_reason="unparseable_after_retry")
        # Only a real dict carrying an explicit `valid` key produces a verdict —
        # no silent `, True` default.
        valid = result.get("valid")
        missing = result.get("missing_components") or []
        violations = result.get("out_of_scope_violations") or []
        logger.info(
            f"[MANIFEST-OPENAI {run_id}] result: valid={valid} "
            f"missing={len(missing)} violations={len(violations)}"
        )
        verdict = "PASS" if valid else "REJECT"
        logger.info(f"[MANIFEST-OPENAI {run_id}] verdict: {verdict}")
        if not valid:
            oi = [_s(x) for x in (missing + violations + (result.get("issues") or []))]
            return _finish(
                False, oi, struct_pass=True, openai_pass=False, struct_failures=[],
                hallucinated_paths=[],  # existence is Step 1's job — never from the LLM
                missing_components=[_s(x) for x in missing],
                oos_violations=[_s(x) for x in violations],
                openai_issues=[_s(x) for x in (result.get("issues") or [])],
            )
    except Exception as _ve:
        logger.warning(f"[MANIFEST-OPENAI {run_id}] cross-validation failed (non-fatal): {_ve}")

    return _finish(True, [], struct_pass=True, openai_pass=True, struct_failures=[],
                    hallucinated_paths=[], missing_components=[], oos_violations=[], openai_issues=[])


# ── Pre-gate completeness verifier (shift-left: "decide before the gate") ─────
# Required JSON keys per explore stage. A pull-loop result missing any of these
# (or that never actually READ the files it claims to change) is "thin" and must
# SUSPEND to HITL rather than be force-synthesized into the codegen stage.
# CLI three-phase-engine: the PLAN phase (CLI-driven, read-only) emits a single
# implementation plan covering BOTH the analyst and designer concerns plus the file
# list. This is the SOLE required-keys contract.
_PLAN_REQUIRED_KEYS = (
    "files_to_change", "sub_tasks", "implementation_spec", "solution_approach",
    "implementation_plan", "code_structure", "testing_strategy", "rollback_strategy",
)


def _canon_path(p: str) -> str:
    """Pure, Windows-safe path canonicalizer for coverage matching. Unifies
    separators (``\\`` → ``/``), strips a leading ``/``, and resolves ``./`` +
    redundant segments at the STRING level (``posixpath.normpath`` — no filesystem
    access). Returns ``""`` for falsy / non-str input.

    Note: it does NOT strip a repo-slug head prefix — this helper has no repo-slug
    context (see the canonical repo-slug form in memory
    ``project_sdlc_threefix_2026_06_10``). Path-form drift where one side carries an
    extra leading repo-slug segment is absorbed by ``_path_covered``'s basename-suffix
    rule instead."""
    if not p or not isinstance(p, str):
        return ""
    s = p.replace("\\", "/").strip().lstrip("/")
    if not s:
        return ""
    import posixpath
    norm = posixpath.normpath(s)
    if norm == ".":
        return ""
    return norm.lstrip("/")


def _path_covered(expected: str, read_paths: set) -> bool:
    """True if `expected` is covered by any path in `read_paths`. Tolerant of
    absolute-vs-relative, leading-slash, separator, and ``./`` drift (all normalized
    via ``_canon_path``). After canonicalization, coverage holds when:
      * a read path equals `expected`, or
      * one is a basename-suffix of the other (``a/b/c.py`` covered by ``c.py`` or
        ``b/c.py`` and vice-versa — absorbs repo-slug-prefix drift), or
      * `expected` names a DIRECTORY (trailing ``/`` or no ``.`` in its final segment)
        and some read path lies beneath it (``startswith expected.rstrip('/') + '/'``).
    The directory rule only ADDS coverage — it never removes an equal/suffix match, so
    no currently-passing plan starts failing. Pure / Windows-safe; ``not expected``
    short-circuits True (nothing to cover)."""
    if not expected:
        return True
    e = _canon_path(expected)
    if not e:
        return True
    # Directory detection off the ORIGINAL expected (trailing slash) or the canonical
    # final segment carrying no dot (e.g. a package/dir-level classifier guess).
    _last = e.rsplit("/", 1)[-1]
    is_dir = expected.replace("\\", "/").rstrip().endswith("/") or ("." not in _last)
    e_prefix = e.rstrip("/") + "/"
    for rp in read_paths:
        if not isinstance(rp, str):
            continue
        r = _canon_path(rp)
        if not r:
            continue
        if r == e or r.endswith("/" + e) or e.endswith("/" + r):
            return True
        if is_dir and r.startswith(e_prefix):
            return True
    return False


def _explore_convergence_verdict(stage: str, parsed, ctx,
                                 expected_files=None, required_keys=(),
                                 affected_components=None, final_text: str = ""):
    """Deterministic convergence verdict — the CORE predicate shared by the in-loop
    `propose_plan` stop signal (artifact-planning-loop Step 4) and the post-loop
    `_verify_explore_output` completeness gate.

    `parsed` is the plan as a dict: mid-loop pass `artifact.to_combined_json()`
    (the live PlanningArtifact); post-loop pass `_parse_json(final_text)`. The verdict
    splits failures into:
      * coverage_gaps  — a required key empty, or an affected_component neither in
        files_to_change/new_files_needed nor read (the thing to keep exploring).
      * grounding_gaps — a cited EXISTING file not in ctx._reads (read-set): the
        anti-hallucination defense. New files are exempt (they don't exist yet).

    Returns {ok, coverage_gaps, grounding_gaps, recoverable}. Pure (no LLM/network).
    Stop = (no coverage_gaps) ∧ (no grounding_gaps). NEVER a confidence score
    (Research Q1/Q2): `assumptions[].confidence` is audit-only and is not read here.
    """
    from agents.sdlc_cli_utils import _looks_truncated_json as _trunc_json

    coverage_gaps: list = []
    grounding_gaps: list = []

    def _is_recoverable() -> bool:
        raw_text = final_text or ""
        if raw_text and _trunc_json(raw_text):
            return True
        if isinstance(parsed, dict):
            _raw = parsed.get("raw")
            if isinstance(_raw, str) and len(_raw) > 200 and _trunc_json(_raw):
                return True
        return False

    if not isinstance(parsed, dict) or not parsed:
        return {"ok": False,
                "coverage_gaps": [f"{stage}: plan is not a valid non-empty object"],
                "grounding_gaps": [], "recoverable": _is_recoverable()}

    # (b) required keys present and non-empty → COVERAGE
    for k in required_keys:
        v = parsed.get(k)
        if v is None or (isinstance(v, (str, list, dict, tuple)) and len(v) == 0):
            coverage_gaps.append(f"required field empty: {k}")

    # read-set: tool-read paths + files whose REAL content reached the model via the
    # seed_full channel (ctx._reads["contents"]). Mirrors the original verifier (c).
    _reads = getattr(ctx, "_reads", {}) or {}
    read_paths = set(_reads.get("paths") or [])
    read_paths |= set((_reads.get("contents") or {}).keys())

    # files the plan declares NEW are exempt from the read requirement (don't exist yet)
    _new_declared = set()
    for f in (parsed.get("new_files_needed") or []):
        if isinstance(f, str) and f.strip():
            _new_declared.add(f.strip())
        elif isinstance(f, dict):
            _p = f.get("path") or f.get("file") or f.get("name")
            if _p:
                _new_declared.add(_p)

    # (c) every cited EXISTING file must be grounded (read) → GROUNDING
    expected = [p for p in (expected_files or [])
                if isinstance(p, str) and p.strip() and p not in _new_declared]
    for p in expected:
        if not _path_covered(p, read_paths):
            grounding_gaps.append(f"cited but unread: {p}")

    # (d) affected-component coverage — read OR named in the plan OR explicitly
    #     ruled_out (with a concrete reason) → COVERAGE. The ruled_out escape hatch
    #     (2026-07-07) removes the false "plan must be a SUPERSET of the classifier's
    #     guess" requirement: coverage is now measured off the PLAN's own decisions
    #     (files_to_change / new_files_needed / ruled_out), not the raw classifier
    #     list. Grounding (clause c above) is UNCHANGED and stays HARD.
    out_files = set(_new_declared)
    for f in (parsed.get("files_to_change") or []):
        if isinstance(f, str):
            out_files.add(f)
        elif isinstance(f, dict):
            _p = f.get("path") or f.get("file") or f.get("name")
            if _p:
                out_files.add(_p)
    # ruled_out discharge set: a classifier candidate the planner explicitly decided
    # is irrelevant WITH a non-empty reason. An entry whose reason is blank/missing
    # does NOT discharge — it must carry a justification.
    ruled_out_paths = set()
    for entry in (parsed.get("ruled_out") or []):
        if isinstance(entry, dict):
            _rp = entry.get("path")
            _reason = entry.get("reason")
            if isinstance(_rp, str) and _rp.strip() \
                    and isinstance(_reason, str) and _reason.strip():
                ruled_out_paths.add(_rp.strip())
    for c in (affected_components or []):
        if isinstance(c, str) and c.strip() \
                and not _path_covered(c, read_paths) \
                and not _path_covered(c, out_files) \
                and not _path_covered(c, ruled_out_paths):
            coverage_gaps.append(f"affected component not covered: {c}")

    ok = not coverage_gaps and not grounding_gaps
    recoverable = (not ok) and _is_recoverable()
    return {"ok": ok, "coverage_gaps": coverage_gaps,
            "grounding_gaps": grounding_gaps, "recoverable": recoverable}


def _verify_explore_output(stage: str, final_text: str, ctx,
                           expected_files=None, required_keys=(),
                           affected_components=None):
    """Deterministic completeness gate for a pre-gate explore stage.

    Asserts that (a) the output parses as a non-empty JSON object, (b) every key
    in `required_keys` is present AND non-empty, (c) every path in `expected_files`
    actually appears in `ctx._reads.paths` (the loop READ it, did not guess it),
    and (d) every affected component is covered (read OR declared in the output's
    files_to_change). Pure and unit-testable — no network, no LLM.

    Returns (ok: bool, reasons: list[str], recoverable: bool).

    `recoverable` is True when a FAILED verdict looks like JSON that was truncated
    at the output ceiling (starts like a JSON object/array but is unbalanced, or
    _parse_json returned a {"raw": …} wrapper over large truncated-looking text) —
    as opposed to genuinely thin/empty/prose output. The caller repairs a
    recoverable result (re-ask / _self_review) before suspending (Step 2). A
    thin/empty/prose failure is NOT recoverable → suspend as before.
    """
    parsed = _parse_json(final_text or "")
    verdict = _explore_convergence_verdict(
        stage, parsed, ctx,
        expected_files=expected_files, required_keys=required_keys,
        affected_components=affected_components, final_text=final_text or "",
    )
    coverage_gaps = verdict.get("coverage_gaps") or []
    grounding_gaps = verdict.get("grounding_gaps") or []
    # Flatten the split verdict back into the legacy flat reasons list so existing
    # callers (analyst/designer/diagnose post-loop) are unchanged.
    reasons: list = list(coverage_gaps) + list(grounding_gaps)
    ok = verdict.get("ok", not reasons)
    recoverable = verdict.get("recoverable", False)
    logger.info(
        f"[VERIFY-EXPLORE] {stage} verdict ok={ok}",
        run_id=getattr(ctx, "run_id", ""),
        stage=stage,
        ok=ok,
        recoverable=recoverable,
        coverage_gaps=coverage_gaps,
        grounding_gaps=grounding_gaps,
    )
    return ok, reasons, recoverable


def _repair_explore_json(run_id: str, stage: str, final_text: str,
                         required_keys: tuple) -> str:
    """Repair a truncated/malformed explore-stage JSON answer (Step 2) using the
    direct path's _self_review repair loop. Returns the repaired text (or the
    original if repair fails). Best-effort — never raises."""
    try:
        criteria = (
            "Valid JSON object with ALL of these non-empty top-level keys: "
            + ", ".join(required_keys)
            + ". Output ONLY the JSON — no prose, no markdown fences."
        )
        repaired = _self_review(final_text or "", criteria, max_iter=1)
        logger.info(
            f"[SDLC {run_id}] {stage}: JSON repair attempted",
            run_id=run_id, stage=stage,
            before_len=len(final_text or ""), after_len=len(repaired or ""),
        )
        return repaired or final_text
    except Exception as _e:
        logger.warning(f"[SDLC {run_id}] {stage}: JSON repair failed ({_e}) — using original",
                       run_id=run_id, stage=stage)
        return final_text


def _phase_pre_coding_build(run_id: str, machine) -> bool:
    """PRE_CODING_BUILD stage — build workspace before CODING starts.

    Returns True if build passes (or gate disabled), False on failure (run is SUSPENDED).
    `machine` is the CodingStateMachine instance (already created, workspace not yet set up).
    """
    _transition(run_id, "PRE_CODING_BUILD", "pre-coding-build")
    gate_enabled = os.getenv("SDLC_ENABLE_BASELINE_GATE", "false").lower() in ("1", "true", "yes")
    if not gate_enabled:
        logger.info(
            f"[PRE-CODE-BUILD {run_id}] gate disabled (SDLC_ENABLE_BASELINE_GATE=false) "
            f"— skipping build, proceeding anyway"
        )
        return True
    try:
        import time as _time
        machine._ensure_run_workspace(machine.repo)
        branch = machine.base_branch or "main"
        logger.info(f"[PRE-CODE-BUILD {run_id}] workspace_sync: re-synced to {branch!r} sha=HEAD")
        t0 = _time.monotonic()
        result = machine._build_oracle()
        duration = round(_time.monotonic() - t0, 1)
        success = bool(result.get("success"))
        logger.info(f"[PRE-CODE-BUILD {run_id}] build: success={success} duration={duration}s")
        if success:
            logger.info(f"[PRE-CODE-BUILD {run_id}] PASS — baseline clean, proceeding to CODING")
            return True
        errors = result.get("errors") or []
        err_preview = "; ".join(_s(e) for e in errors[:3])
        logger.error(
            f"[PRE-CODE-BUILD {run_id}] FAIL — workspace does not build: {err_preview}",
            run_id=run_id,
            errors=errors[:3],
        )
        update_run_state(
            run_id, "SUSPENDED",
            context_patch={"suspended_at_stage": "PRE_CODING_BUILD"},
            suspended_at_stage="PRE_CODING_BUILD",
            error="Workspace does not build cleanly before coding. Fix manually and re-trigger."
        )
        return False
    except (RuntimeError, OSError, ImportError) as _infra:
        # Infrastructure failure (clone missing, network, missing dependency) — workspace
        # is unusable but this is not a code-quality build failure.  Proceed with a warning
        # so CODING can still attempt the run; a genuine build failure is caught above.
        logger.error(
            f"[PRE-CODE-BUILD {run_id}] infra error setting up build gate ({_infra!r}) "
            f"— proceeding anyway",
            run_id=run_id,
        )
        return True
    except Exception as _be:
        # Unexpected exception (e.g. assertion in _build_oracle internals) — proceed with
        # warning; do not silently treat this as a passing build.
        logger.error(
            f"[PRE-CODE-BUILD {run_id}] unexpected error in build gate ({_be!r}) "
            f"— proceeding anyway",
            run_id=run_id,
        )
        return True


# ── Shift-left pre-gate codegen helpers ("decide before the gate") ───────────

def _bug_analysis_from_fix(run_id: str, jira_key: str, fix: dict, triage: dict,
                           jira_summary: str = "") -> dict:
    """Build the state-machine `analysis` dict from an approved bug `fix` + triage.
    Single source of truth shared by the bug pre-gate codegen and the bug post-gate
    resume so both build the identical edit list / requirements."""
    fix = fix or {}
    triage = triage or {}
    _fix_files = [c.get("file", "") for c in fix.get("code_changes", [])
                  if isinstance(c, dict) and c.get("file")]
    _bug_ftc = _fix_files or triage.get("affected_components", [])
    _bug_ftc_kept, _bug_ftc_dropped = _filter_noneditable_files(_bug_ftc)
    if _bug_ftc_dropped:
        logger.warning(
            f"[SDLC {run_id}] Deny-list (bug): dropped {len(_bug_ftc_dropped)} "
            f"non-editable path(s) from files_to_change: {_bug_ftc_dropped}"
        )
    return {
        "files_to_change":   _bug_ftc_kept,
        "new_files_needed":  [],
        "requirements":      fix.get("fix_description", "") or jira_summary,
        "problem_statement": (
            f"Bug {jira_key}: {jira_summary}\n"
            f"Fix: {fix.get('fix_description', '')}\n"
            f"Root cause: {fix.get('root_cause_analysis', '')}"
        ).strip(),
        "root_cause":        fix.get("root_cause_analysis", ""),
    }


def _capture_base_sha_unconditional(run_id: str, machine) -> None:
    """Pin base_sha even when SDLC_REUSE_RUN_WORKSPACE is off, so every approved
    diff has a base to rebase from at APPLYING (recommended adjustment 2).
    First-writer-wins; best-effort (never fatal)."""
    try:
        if machine._get_run_base_sha():
            return
        # Prefer the GitLab API branch head (cheap) so we do NOT force a clone on
        # every pre-gate run just to read HEAD. Fall back to a workspace clone only
        # if the API read returns nothing (e.g. transient API failure).
        sha = machine._current_branch_head(machine.base_branch or "main")
        if not sha:
            try:
                ws = machine._ensure_run_workspace(machine.repo)
                if ws:
                    from workers.workspace_sync_worker import _git_head as _gh
                    sha = _gh(ws) or ""
            except Exception as _ws_e:
                logger.debug(f"[SDLC {run_id}] base_sha workspace head read failed: {_ws_e}")
        if sha:
            machine._set_run_base_sha(sha)
            logger.info(f"[SDLC {run_id}] base_sha pinned unconditionally: {sha[:8]}")
    except Exception as _e:
        logger.warning(f"[SDLC {run_id}] unconditional base_sha capture failed (non-fatal): {_e}")


def _pregate_codegen(run_id: str, jira_key: str, repo_resolved: str, language: str,
                     design: dict, analysis: dict, base_branch: str = "",
                     working_branch: str = "", ctx: dict = None,
                     run_type: str = "feature") -> bool:
    """Run the FULL CODING → REVIEW_GATE → TESTING machinery in PRE-GATE mode so
    the human approves a real, compiled, test-green diff (the VERIFIED_DIFF) — not
    a JSON plan. Captures base_sha unconditionally and runs PRE_CODING_BUILD first.

    Returns True iff a VERIFIED_DIFF was produced and the run did not FAIL/SUSPEND;
    on False the caller must NOT advance to the approval gate (the run is already
    SUSPENDED/FAILED with the partial state the engineer needs)."""
    from agents.sdlc_state_machine import CodingStateMachine
    from agents.sdlc_context import normalize_repo_index_key_without_prefix as _nrik
    import os as _os
    ctx = ctx or {}
    repo_key = _nrik(repo_resolved) if repo_resolved else ""
    _skip_tests_env = _os.getenv("SDLC_SKIP_TESTS", "").lower() in ("1", "true", "yes")
    _st_raw = ctx.get("skip_tests", _skip_tests_env)
    _ss_raw = ctx.get("skip_slt", False)
    _resolved_skip_tests = _st_raw if isinstance(_st_raw, bool) else str(_st_raw).lower() in ("1", "true", "yes")
    _resolved_skip_slt = _ss_raw if isinstance(_ss_raw, bool) else str(_ss_raw).lower() in ("1", "true", "yes")
    try:
        machine = CodingStateMachine(
            run_id=run_id, jira_key=jira_key, repo=repo_key, language=language,
            design=design, analysis=analysis,
            base_branch=base_branch or ctx.get("base_branch", ""),
            working_branch=working_branch or ctx.get("working_branch", ""),
            gitlab_repo=repo_resolved,
            skip_tests=_resolved_skip_tests, skip_slt=_resolved_skip_slt,
            compile_skipped=bool(ctx.get("compile_skipped", False)),
            user_id=ctx.get("user_id", ""), user_email=ctx.get("user_email", ""),
            mode="pregate",
        )
    except Exception as _ce:
        logger.error(f"[SDLC {run_id}] PREGATE_CODEGEN construction failed: {_ce}")
        # Terminalize: without this the run is left at its prior (non-terminal) state
        # with no SUSPEND/FAIL — the caller just returns and the run is orphaned/stuck.
        _suspend_plan(
            run_id, jira_key, "IMPLEMENT",
            f"pre-gate codegen could not start (state-machine construction failed): {_ce}",
        )
        return False

    logger.info(
        f"[SDLC {run_id}] PREGATE_CODEGEN start",
        run_id=run_id, stage="PREGATE_CODEGEN",
        files=(analysis.get("files_to_change") if isinstance(analysis, dict) else None),
    )
    _capture_base_sha_unconditional(run_id, machine)
    # PRE_CODING_BUILD runs here now (moved off the resume path) so the workspace
    # is proven to build BEFORE pre-gate patching begins.
    if not _phase_pre_coding_build(run_id, machine):
        return False
    try:
        machine.run()
    except SDLCCancelled:
        logger.info(f"SDLC {run_id}: pregate codegen cancelled mid-run")
        return False
    except Exception as _re:
        logger.error(f"[SDLC {run_id}] PREGATE_CODEGEN failed: {_re}")
        update_run_state(run_id, "FAILED", error=str(_re))
        return False

    _state = (get_run(run_id) or {}).get("state", "")
    if _state in ("FAILED", "SUSPENDED", "CANCELLED", "MERGE_CONFLICT"):
        logger.warning(
            f"[SDLC {run_id}] PREGATE_CODEGEN did not reach the gate (state={_state}) — not advancing"
        )
        return False
    from store.sdlc_artifacts import _load_latest_artifact
    _vd = _load_latest_artifact(run_id, "VERIFIED_DIFF")
    _payload = (_vd or {}).get("payload") or {}
    if not _payload.get("edits"):
        logger.warning(f"[SDLC {run_id}] PREGATE_CODEGEN produced no VERIFIED_DIFF edits — not advancing")
        # Terminalize: the machine left the run at REVIEW (a non-terminal state) but
        # produced no applicable diff. Returning False here without a SUSPEND leaves the
        # run silently stranded at REVIEW (Case 3 orphan). Suspend at IMPLEMENT so it is
        # visible/actionable and a resume re-runs implementation to produce a real diff.
        _suspend_plan(
            run_id, jira_key, "IMPLEMENT",
            "pre-gate codegen completed but produced no VERIFIED_DIFF edits to review/apply "
            "— re-run implementation (resume) to generate a diff",
        )
        return False
    logger.info(
        f"[SDLC {run_id}] PREGATE_CODEGEN finish",
        run_id=run_id, stage="PREGATE_CODEGEN",
        base_sha=_payload.get("base_sha"),
        files=_payload.get("files"),
        compile_passed=(_payload.get("compile") or {}).get("passed"),
        tests_passed=(_payload.get("tests") or {}).get("passed"),
    )
    return True


# ── CLI three-phase engine: PLAN + REVIEW phases (additive; Step 3 + Step 6) ──
# These are NET-NEW functions for the CLI three-phase engine. They do NOT rewire
# run_feature_pipeline/run_bug_pipeline yet (a later step does the cutover). PLAN
# drives the read-only CLI to produce the implementation plan; REVIEW is the
# platform-controlled Opus diff-only gate. Both REUSE existing helpers verbatim.

# JSON Schema for the PLAN phase structured output. Kept module-level (next to the
# required-keys constant) so it is defined once and shared. `required` == the
# _PLAN_REQUIRED_KEYS tuple so the CLI's structured-output contract matches the
# post-loop completeness gate exactly.
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "files_to_change":    {"type": "array", "items": {"type": "string"}},
        "new_files_needed":   {"type": "array"},
        "sub_tasks":          {"type": "array"},
        "implementation_spec": {"type": "string"},
        "solution_approach":  {"type": "string"},
        # implementation_plan is intentionally permissive: models emit either an
        # ordered list of steps or a single prose block. Both are acceptable.
        # Expressed via anyOf rather than a union `type: [array, string]` — the CLI
        # binary compiles this schema with Ajv in strict mode, which REJECTS the union
        # `type` array form ("strictTypes: use allowUnionTypes …") at schema-compile
        # time. That broke the StructuredOutput tool for the whole PLAN session (see
        # logs/root_cause_analysis_plan_stall.md). anyOf is strict-clean and preserves
        # both acceptable shapes.
        "implementation_plan": {"anyOf": [{"type": "array"}, {"type": "string"}]},
        "code_structure":     {"type": "string"},
        "testing_strategy":   {"type": "string"},
        "rollback_strategy":  {"type": "string"},
        "affected_components": {"type": "array"},
        # Escape hatch (2026-07-07): classifier-flagged paths the planner decided are
        # NOT relevant to this change. Each entry {path, reason} with a NON-EMPTY
        # reason discharges that path from the affected-component coverage check
        # (see _explore_convergence_verdict). Optional (NOT in _PLAN_REQUIRED_KEYS) —
        # missing ⇒ treated as empty.
        "ruled_out": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path":   {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        # Planner's estimate of how many CLI tool-call turns IMPLEMENT will need.
        # Optional (not in _PLAN_REQUIRED_KEYS) — a missing/invalid value falls back
        # to the coder's default budget (see sdlc_cli_budget.resolve_implement_turns).
        "implement_max_turns": {"type": "integer"},
        # Per-file UNIQUE VERBATIM anchor strings the coder matches on instead of line
        # numbers. Line hints drift against the live tree and force expensive re-reads
        # (the dominant IMPLEMENT-timeout cause); an exact anchor lets the coder Grep to
        # the edit site in one pass. Optional (NOT in _PLAN_REQUIRED_KEYS) — a missing
        # value just means the coder falls back to reading. Projected into the IMPLEMENT
        # prompt via sdlc_implement_prompt._SPEC_KEYS. Object items with plain string
        # properties only — Ajv strict mode (see implementation_plan note above) rejects
        # union `type` arrays, so this shape mirrors ruled_out/open_questions exactly.
        "edit_anchors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file":    {"type": "string"},
                    "anchors": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        # Per-anchor ~5-15 line source WINDOW as it appears in the CURRENT tree, so
        # the coder edits relative to real context instead of re-reading the file to
        # rediscover it. Optional (NOT in _PLAN_REQUIRED_KEYS) — a missing value just
        # means the coder falls back to reading. Same strict-Ajv-clean shape as
        # edit_anchors (plain string properties, no union `type`). Projected into the
        # IMPLEMENT prompt via sdlc_implement_prompt._SPEC_KEYS.
        "snippets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file":    {"type": "string"},
                    "anchor":  {"type": "string"},
                    "window":  {"type": "string"},
                },
            },
        },
        # Exact method/function signatures, enum members, and constant names the edit
        # DEPENDS on (what the coder would otherwise grep for). The planner already read
        # these; serializing them here converts code-phase reads into near-zero. Optional
        # (NOT in _PLAN_REQUIRED_KEYS). Strict-Ajv-clean object items.
        "signatures_needed": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file":       {"type": "string"},
                    "signatures": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "open_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question":    {"type": "string"},
                    "options":     {"type": "array"},
                    "recommended": {"type": "integer"},
                    "rationale":   {"type": "string"},
                },
            },
        },
    },
    "required": list(_PLAN_REQUIRED_KEYS),
}


def _suspend_plan(run_id: str, jira_key: str, stage: str, reason: str) -> None:
    """Suspend a run from the PLAN (or any CLI) phase. Mirrors the SUSPEND shape used
    elsewhere in this module (event + transition + state patch + best-effort Jira
    comment). Never raises — a suspend must never itself crash the pipeline."""
    try:
        logger.warning(
            f"[SDLC {run_id}] {stage} suspended: {reason}",
            run_id=run_id, stage=stage, reason=reason,
        )
    except Exception:
        pass
    try:
        _event(run_id, stage, f"{stage.lower()}-suspend", reason, {"stage": stage})
    except Exception:
        pass
    try:
        _transition(run_id, "SUSPENDED", f"{stage.lower()}-suspend")
    except Exception:
        pass
    try:
        update_run_state(
            run_id, "SUSPENDED",
            context_patch={"suspended_at_stage": stage, "suspend_reason": reason},
            suspended_at_stage=stage, error=reason,
        )
    except Exception:
        pass
    try:
        _jira_comment(jira_key, f"[AiNxt AI] {stage} suspended: {reason}")
    except Exception:
        pass


def _ground_plan_reads(run_id: str, plan: dict, workspace_root: str):
    """Build a lightweight ctx stub whose ``_reads`` reflects the plan's cited EXISTING
    files, read off the pinned checkout (the CLI's own reads happen in a subprocess the
    platform cannot observe). Returns ``(ctx_v, kept)`` where ``kept`` is the set of
    existing ``files_to_change`` that resolve on disk (new files excluded; cited-but-
    absent paths are dropped from ``kept`` by ``_resolve_required_against_workspace``, so
    they are NOT passed as ``expected_files`` and therefore do NOT themselves raise a
    grounding gap — a *resolved* file that then fails to READ is what leaves an expected
    path unseeded → grounding gap → suspend). Filesystem READ only — never mutates. Shared
    by the initial grounding gate and the fix-round re-grounding so both judge identically."""
    expected_raw = [p for p in (plan.get("files_to_change") or [])
                    if isinstance(p, str) and p.strip()]
    _new_files = [p for p in (plan.get("new_files_needed") or [])
                  if isinstance(p, str) and p.strip()]
    kept, _dropped_nonexistent, _excluded_new = _resolve_required_against_workspace(
        expected_raw, workspace_root, new_files=_new_files,
    )

    class _PlanCtx:
        pass
    ctx_v = _PlanCtx()
    ctx_v.run_id = run_id
    ctx_v._reads = {"paths": set(), "contents": {}}
    for _p in kept:
        try:
            _rel = _p.replace("\\", "/").lstrip("/")
            _full = os.path.join(workspace_root, _rel)
            with open(_full, "r", encoding="utf-8", errors="replace") as _fh:
                _content = _fh.read()
            ctx_v._reads["paths"].add(_p)
            ctx_v._reads["contents"][_p] = _content
        except Exception as _re:
            logger.debug(f"[SDLC {run_id}] PLAN ground-read miss {_p!r}: {_re}")
    return ctx_v, kept


def _plan_gate_action(verdict: dict) -> tuple:
    """Pure decision over a split convergence verdict (Research Q2 split). Returns
    ``(action, hard_gaps, candidate_gaps)`` where action ∈ {"suspend","fix_round",
    "proceed"}:
      * grounding gaps (cited-but-unread EXISTING file = anti-hallucination) OR a
        ``required field empty:`` coverage gap (structurally-thin plan) ⇒ ``suspend``
        (HARD — never softened);
      * any OTHER coverage gap (a classifier candidate neither changed nor ruled_out)
        ⇒ ``fix_round``;
      * neither ⇒ ``proceed``.
    Pure — no LLM/network/IO. This is the accept/suspend boundary guarding IMPLEMENT."""
    grounding = [g for g in (verdict.get("grounding_gaps") or [])]
    coverage = [g for g in (verdict.get("coverage_gaps") or [])]

    def _is_hard_coverage(g) -> bool:
        # A structurally-thin plan (required key empty) or a non-dict/empty plan
        # sentinel is HARD (not a mere classifier-guess mismatch to fix-round).
        s = str(g)
        return s.startswith("required field empty:") or "not a valid non-empty object" in s

    empty_field = [g for g in coverage if _is_hard_coverage(g)]
    candidates = [g for g in coverage if not _is_hard_coverage(g)]
    hard = grounding + empty_field
    if hard:
        return ("suspend", hard, candidates)
    if candidates:
        return ("fix_round", [], candidates)
    return ("proceed", [], [])


def _run_plan_fix_round(run_id: str, workspace_root: str, repo_resolved: str,
                        language: str, plan: dict, unaddressed_paths: list,
                        session_id: str, issue: dict = None,
                        manifest_feedback: dict = None):
    """One bounded PLAN fix round (mirrors REVIEW's single fix round). Re-invokes the
    read-only CLI with targeted feedback naming the EXACT unaddressed classifier paths,
    warm-started PATH-ONLY (prior plan file lists + prior narrative fields + never file
    contents — item 9). When ``SDLC_CLI_RESUME_ENABLED`` is on and a ``session_id`` is
    available the CLI resumes that session; otherwise it is a fresh session — which is
    the shipped default, so the prompt carries enough ticket + repo + approach context
    to regenerate a COMPLETE plan (not just the delta). Returns the fix-round plan dict,
    or None when the CLI produced nothing usable. Never raises.

    ``manifest_feedback`` (2026-07-13): when the caller is the manifest auto-correction
    round rather than the classifier coverage round, this dict carries the cross-model
    judge's structured reject reasons (``missing_components`` / ``out_of_scope_violations``).
    When present an extra directive block is appended telling the planner to add each
    missing component (or ruled_out it) and drop/justify each out-of-scope file."""
    from agents.sdlc_cli_engine import run_cli, CliEngineConfig
    from agents.sdlc_cli_budget import remaining_budget, resolve_plan_turns, record_cli_usage
    from core.model_registry import cli_plan_model
    try:
        cfg = CliEngineConfig.from_env()
        issue = issue or {}
        _prior_change = [p for p in (plan.get("files_to_change") or [])
                         if isinstance(p, str) and p.strip()]
        _prior_new = [p for p in (plan.get("new_files_needed") or [])
                      if isinstance(p, str) and p.strip()]
        _cls = (get_run(run_id) or {}).get("context", {}).get("classification", {}) or {}
        max_turns = resolve_plan_turns(_cls.get("complexity"), remaining_budget(run_id, "PLAN"))
        # Ticket + prior narrative context so a FRESH (resume-off, the default) session
        # can regenerate a complete plan rather than run blind. Text/paths only — never
        # source file contents (item 9 warm-start contract).
        _summary = issue.get("summary", "") or ""
        _desc = issue.get("description", "") or issue.get("jira_description", "") or ""
        _approach = plan.get("solution_approach", "") or ""
        from agents.sdlc_implement_prompt import workspace_boundary_clause as _wbc
        # Classifier-coverage block — only when there are unaddressed classifier paths
        # (the original coverage fix round). Empty for a pure manifest auto-correction.
        _unaddressed_block = ""
        if unaddressed_paths:
            _unaddressed_block = (
                "Your previous implementation PLAN left these classifier-flagged paths "
                "UNADDRESSED (they were neither in files_to_change / new_files_needed nor "
                "in ruled_out):\n"
                f"  {unaddressed_paths}\n\n"
                "For EACH such path you MUST either (a) add it to files_to_change (or "
                "new_files_needed if it does not exist), OR (b) add it to ruled_out as "
                "{\"path\": ..., \"reason\": ...} with a concrete reason it is irrelevant "
                "to this change. Keep the rest of the plan intact.\n\n"
            )
        # Manifest cross-validation reject block (2026-07-13 auto-correction round).
        _manifest_block = ""
        if manifest_feedback:
            _mf_missing = manifest_feedback.get("missing_components") or []
            _mf_oos = manifest_feedback.get("out_of_scope_violations") or []
            _manifest_block = (
                "Your previous plan was REJECTED by manifest cross-validation.\n"
                f"  Missing in-scope components: {_mf_missing}\n"
                f"  Out-of-scope violations: {_mf_oos}\n"
                "Revise the manifest to (a) ADD each missing component to "
                "files_to_change / new_files_needed, OR explicitly add it to ruled_out "
                "as {\"path\": ..., \"reason\": ...} with a concrete reason; and (b) DROP "
                "each out-of-scope file OR justify it as a mandatory companion of an "
                "in-scope change (e.g. db/migrate.py + SQL for a schema change). Keep the "
                "rest of the plan intact.\n\n"
            )
        fix_prompt = (
            "You are refining an implementation PLAN for a code change in repository "
            f"{repo_resolved} (language: {language or 'unknown'})."
            f"{_wbc(workspace_root)}\n\n"
            f"TICKET:\n  Summary: {_summary}\n  Description: {_desc}\n\n"
            f"Intended solution approach (from your prior plan):\n{_approach}\n\n"
            f"{_unaddressed_block}"
            f"{_manifest_block}"
            "Your prior plan's file lists (paths only — re-read the ACTUAL source before "
            "deciding, do NOT guess):\n"
            f"  files_to_change: {_prior_change}\n"
            f"  new_files_needed: {_prior_new}\n\n"
            "Output ONLY the full required JSON with all top-level keys: "
            f"{', '.join(_PLAN_REQUIRED_KEYS)}, plus new_files_needed, "
            "affected_components, ruled_out, and implement_max_turns."
        )
        # PART 1 governance awareness into the PLAN fix round (fail-safe → no-op).
        # Inlined SKILL.md content in the prompt text — no CLI plugin-loading
        # mechanism exists (confirmed 2026-07-20; see resolve_awareness docstring).
        from agents.sdlc_governance import engine as _gov_engine
        from agents.sdlc_implement_prompt import governance_pointer_clause as _gov_pointer
        _run_ctx = (get_run(run_id) or {}).get("context", {}) or {}
        _gov_block = _gov_engine.resolve_awareness(
            _run_ctx.get("governance_skills"), phase="plan", workspace_root=workspace_root
        )
        if _gov_block:
            fix_prompt = fix_prompt + _gov_pointer(_gov_block)
        result = run_cli(
            config=cfg, workspace_root=workspace_root, prompt=fix_prompt,
            profile="plan", model=cli_plan_model(), output_schema=PLAN_SCHEMA,
            max_turns=max_turns, run_id=run_id, resume_session_id=session_id or "",
            transient_retries=2,   # read-only PLAN-fix: safe to re-spawn on a proxy 502
        )
        try:
            record_cli_usage(run_id, result.usage or {}, result.total_cost_usd or 0.0)
        except Exception as _bue:
            logger.warning(f"[SDLC {run_id}] PLAN fix-round budget accounting failed: {_bue}")
        if result.status == "suspended":
            logger.warning(
                f"[SDLC {run_id}] PLAN fix round CLI suspended: {result.reason}",
                run_id=run_id, stage="PLAN",
            )
            return None
        fixp = result.structured_output
        if not isinstance(fixp, dict) or not fixp:
            fixp = _parse_json(result.result_text or "")
        if not isinstance(fixp, dict) or not fixp:
            return None
        return fixp
    except Exception as _fre:
        logger.warning(f"[SDLC {run_id}] PLAN fix round errored (non-fatal): {_fre}",
                       run_id=run_id, stage="PLAN")
        return None


def _run_plan_phase(run_id: str, jira_key: str, repo_resolved: str, language: str,
                    issue: dict, ctx: dict, run_type: str = "feature"):
    """CLI three-phase engine — PLAN phase (Step 3).

    Drives the read-only CLI (profile="plan", Sonnet workhorse) to produce an
    implementation PLAN dict, verifies it is grounded+complete (REUSING
    _verify_explore_output), optionally clarifies-in-plan (suspends to
    AWAITING_USER_INPUT), runs the manifest-validation sub-check, stores the plan
    as the PLAN artifact, and mirrors it into run context as analysis/design so the
    existing _pregate_codegen(design=plan, analysis=plan) contract holds unchanged.

    Returns the plan dict on success, or None when the run has ALREADY been
    suspended/paused (the caller must then `return run_id`)."""
    from agents.sdlc_cli_engine import run_cli, CliEngineConfig
    from agents.sdlc_cli_budget import (
        record_cli_usage, remaining_budget, derive_max_turns, is_exhausted,
        resolve_plan_turns,
    )
    from agents.sdlc_cli_utils import _looks_truncated_json
    from core.model_registry import cli_plan_model
    from store.sdlc_artifacts import _store_artifact, compute_input_hash

    issue = issue or {}
    ctx = ctx or {}
    _cls = (get_run(run_id) or {}).get("context", {}).get("classification", {}) or {}

    # Live state → PLAN so the UI/manifest highlights the PLAN node during planning.
    try:
        _transition(run_id, "PLAN", "cli-planner")
    except Exception:
        pass

    # ── 3. Materialize + pin the workspace early (PLAN needs the pinned tree so the
    #        grounding predicate can confirm cited files exist on disk).
    workspace_root = _materialize_early_workspace(
        run_id, repo_resolved,
        issue.get("working_branch", "") or "",
        issue.get("base_branch", "") or "",
        user_id=issue.get("triggered_by_user_id", "") or "",
        user_email=issue.get("triggered_by_email", "") or "",
    )
    if not workspace_root:
        logger.warning(
            f"[SDLC {run_id}] PLAN workspace materialization returned empty path",
            run_id=run_id, stage="PLAN",
        )
        _suspend_plan(run_id, jira_key, "PLAN", "workspace materialization failed")
        return None
    patch_run_context(run_id, {"workspace_root": workspace_root})

    # ── 3b. Multi-repo: stage dep-repo checkouts inside the primary workspace
    #        (no-op for single-repo runs). See _setup_multi_repo_workspace_for_plan
    #        docstring for why PLAN never suspends on a staging failure.
    _setup_multi_repo_workspace_for_plan(run_id, workspace_root)

    # ── 4. Budget check + derive the CLI turn cap. PLAN turns scale to classifier
    #        complexity (simple/medium/complex → 8/20/40) so a small ticket cannot burn
    #        the flat default budget; the HOD budget can only REDUCE that cap, never
    #        inflate it (resolve_plan_turns → derive_max_turns).
    if is_exhausted(run_id, "PLAN"):
        _suspend_plan(run_id, jira_key, "PLAN", "per-run budget exhausted")
        return None
    _plan_remaining = remaining_budget(run_id, "PLAN")
    max_turns = resolve_plan_turns(_cls.get("complexity"), _plan_remaining)
    logger.info(
        "[SDLC PLAN] turn budget resolved", run_id=run_id,
        complexity=_cls.get("complexity"), plan_turns=max_turns,
        budget_ceiling=derive_max_turns(_plan_remaining), resolved=max_turns,
    )

    # ── 5. Build the PLAN prompt (ticket + conventions + seeded components + prior
    #        clarify answers + approved scope). Do NOT instruct it to write code —
    #        PLAN is read-only.
    _summary = issue.get("summary", "") or ""
    _desc = issue.get("description", "") or issue.get("jira_description", "") or ""
    _seed_components = _cls.get("affected_components") or []
    _prior_answers = ctx.get("user_answers") or []
    _answers_block = ""
    if isinstance(_prior_answers, list) and _prior_answers:
        _lines = ["AUTHORITATIVE USER ANSWERS (already provided — honor these, do NOT re-ask):"]
        for _qa in _prior_answers:
            if isinstance(_qa, dict):
                _lines.append(f"  Q: {_qa.get('question', '')}\n  A: {_qa.get('answer', '')}")
        _answers_block = "\n".join(_lines) + "\n\n"
    # WS-5: inject the GATE-1-approved WorkItem scope so the planner stays inside
    # the human-confirmed boundary (prevents the scope divergence that used to
    # trip an unconfirmed out_of_scope_violations suspend at MANIFEST_VALIDATION).
    _wi = ctx.get("work_item") or {}
    _scope_block = ""
    if isinstance(_wi, dict) and (_wi.get("scope") or _wi.get("out_of_scope")):
        _scope_block = (
            "APPROVED SCOPE (human-confirmed at the WorkItem gate — stay inside it):\n"
            f"  In scope: {_wi.get('scope') or []}\n"
            f"  Out of scope (do NOT touch): {_wi.get('out_of_scope') or []}\n\n"
        )
    # Step 4.2 (2026-07-13): a COLD manual retry (resume_pre_sm_pipeline("PLAN") →
    # _drive_pre_sm → here) re-enters with the run context that carries a prior
    # manifest_feedback (persisted at the Step-13 suspend tail). Inject it so the cold
    # retry re-plans WITH the reject reasons instead of blind — closing the
    # "feedback lost on retry" gap. The warm auto-round reads the SAME contract via
    # _run_plan_fix_round(manifest_feedback=...); this is its cold-path twin.
    _manifest_fb = ctx.get("manifest_feedback") or {}
    _manifest_fb_block = ""
    if isinstance(_manifest_fb, dict) and (
        _manifest_fb.get("missing_components") or _manifest_fb.get("out_of_scope_violations")
    ):
        _manifest_fb_block = (
            "PRIOR MANIFEST REJECTION (address before re-planning):\n"
            f"  Missing components: {_manifest_fb.get('missing_components') or []}\n"
            f"  Out-of-scope: {_manifest_fb.get('out_of_scope_violations') or []}\n"
            "Add each missing component to files_to_change / new_files_needed or to "
            "ruled_out (with a concrete reason); drop or justify each out-of-scope file "
            "as a mandatory companion of an in-scope change.\n\n"
        )
        logger.info(
            "[PLAN] injecting prior manifest feedback into cold re-plan",
            run_id=run_id, has_manifest_feedback=True,
        )
    _keys_list = ", ".join(_PLAN_REQUIRED_KEYS)
    from agents.sdlc_implement_prompt import workspace_boundary_clause as _wbc

    # ── 5a. Multi-repo: dependent-repo awareness (Step 3, multi-repo CLI visibility).
    #        Hoisted ahead of the boundary clause below (Fix A) so the workspace-boundary
    #        clause's deps_dirname= carve-out can be conditioned on whether this run
    #        actually has dep rows — IMPLEMENT/continue/fix-round already pass
    #        deps_dirname=".sdlc_deps"; PLAN was missed, which silently negated the whole
    #        point of staging deps inside the workspace (workspace_boundary_clause says
    #        workspace_root is "the ONLY tree you may read", so without the carve-out the
    #        model treats .sdlc_deps/ as out of scope). Sourced from list_run_repos (same
    #        rows _setup_multi_repo_workspace_for_plan used to stage the checkouts at 3b)
    #        rather than the MultiRepoWorkspace return value, since the rows already carry
    #        the kind/ref info the clause needs and this avoids depending on
    #        prepare_and_install_deps' return shape.
    #        "" for single-repo runs — prompt stays byte-identical to today.
    try:
        from agents.sdlc_implement_prompt import dependent_repos_clause as _dep_clause
        from store.sdlc_store import list_run_repos as _list_run_repos_for_dep
        _dep_rows = _list_run_repos_for_dep(run_id) or []
        _dep_block = _dep_clause(_dep_rows)
    except Exception as _dep_e:
        logger.debug(f"[SDLC {run_id}] PLAN dep_block build failed (non-fatal): {_dep_e}")
        _dep_rows, _dep_block = [], ""

    prompt = (
        "You are a senior engineer producing an implementation PLAN for a code change.\n"
        f"Repository: {repo_resolved} (language: {language or 'unknown'})."
        f"{_wbc(workspace_root, deps_dirname='.sdlc_deps' if _dep_block else '')}\n\n"
        f"TICKET {jira_key}:\n  Summary: {_summary}\n  Description: {_desc}\n\n"
        + (f"Classifier-flagged affected components: {_seed_components}\n"
           "For EACH classifier-flagged path you MUST either (a) include it in "
           "files_to_change (or new_files_needed if it does not exist yet), OR "
           "(b) list it in ruled_out as {\"path\": ..., \"reason\": ...} with a "
           "concrete reason it is NOT relevant to this change. A classifier-flagged "
           "path that is neither changed nor ruled_out will FAIL validation. Verify "
           "each against the real code before deciding.\n\n" if _seed_components else "")
        + _scope_block
        + _answers_block
        + _manifest_fb_block
        + "Read the ACTUAL source via your read-only tools before planning — do NOT guess "
        "file paths or APIs. Every file you list in files_to_change MUST be a real, existing "
        "file you have read (new files go in new_files_needed instead).\n\n"
        f"Output ONLY the required JSON with these top-level keys: {_keys_list}, plus "
        "new_files_needed, affected_components, ruled_out, implement_max_turns, "
        "edit_anchors, snippets, and signatures_needed. All clarifying questions were "
        "already resolved before planning started — do NOT ask questions here; make the "
        "best grounded decision and proceed.\n\n"
        "Also emit edit_anchors: for EACH file in files_to_change, a list of SHORT, UNIQUE, "
        "VERBATIM strings copied from the CURRENT source at (or immediately adjacent to) every "
        "edit site — a function/method signature, a decorator, a distinctive comment, or the "
        "exact line your change attaches to. Copy each string exactly as it appears (including "
        "whitespace) and make it unique enough to match exactly ONE place in its file. These let "
        "the coder locate each edit by string match in a single pass instead of trusting line "
        "numbers, which drift against the live tree and force the expensive re-reads that are the "
        "main cause of IMPLEMENT timeouts. Do NOT use line numbers as anchors. Shape: "
        "[{\"file\": \"<path>\", \"anchors\": [\"<verbatim snippet>\", ...]}]. Inside "
        "implementation_spec, reference these anchor strings rather than \"~line NNN\".\n\n"
        "Also emit snippets: for each edit_anchor, the ~5-15 line WINDOW of source around that "
        "anchor, copied VERBATIM from the CURRENT tree (whitespace preserved) — never a whole-file "
        "dump, just the lines the coder needs to see to make the change. You already read these "
        "files to plan; serialize what you saw so the coder edits against real context instead of "
        "re-reading. Shape: [{\"file\": \"<path>\", \"anchor\": \"<matching anchor>\", "
        "\"window\": \"<verbatim source window>\"}].\n\n"
        "Also emit signatures_needed: the EXACT method/function signatures, enum members, "
        "constant names, and type names each edit DEPENDS on — the things the coder would "
        "otherwise have to grep for. Copy them verbatim from the source you read; emit ONLY the "
        "signatures the code phase actually needs, not an inventory. Shape: [{\"file\": "
        "\"<path or defining file>\", \"signatures\": [\"<verbatim signature>\", ...]}].\n\n"
        "Also emit implement_max_turns: your realistic estimate of how many CLI tool-call "
        "turns a coding agent will need to IMPLEMENT this plan end-to-end (read each target "
        "file, write every file in files_to_change + new_files_needed, author the tests the "
        "plan calls for, and get the build compiling). Size it from the ACTUAL file count in "
        "your plan: budget ~10–12 turns per file that is edited or created (read → edit → "
        "re-read/verify), PLUS 15–25 turns of overhead for build/compile/test iteration and "
        "fixing the errors those surface. Concrete anchors: a focused 1–2 file change ~25, a "
        "typical 3–5 file multi-file change ~60, a large change spanning a DB migration + UI + "
        "backend (8+ files) ~100–140. Count your files and scale accordingly. When uncertain, "
        "round UP — under-estimating forces the coder to abort mid-implementation when it hits "
        "the turn cap (which fails the whole run), whereas the per-run budget ceiling and the "
        "coder's own STOP contract already stop it from over-spending an over-estimate. Do not "
        "low-ball this number."
    )

    # ── 5a2. Read-only exploration + symbol-selection disciplines (P1 + P2).
    #         search_discipline_clause: stop the same failed exact-name search from looping
    #         (SecureNxt vs SecureNext). identifier_fidelity_clause: bind the ticket's domain
    #         qualifier (issuer vs acquirer) to the RIGHT symbol when look-alikes coexist in
    #         the target scope, and record the choice in implementation_spec/solution_approach
    #         so REVIEW can verify it. Appended (established pattern — gov/dep blocks below
    #         also append after the "Output ONLY the required JSON" instruction).
    from agents.sdlc_implement_prompt import (
        search_discipline_clause as _search_discipline,
        identifier_fidelity_clause as _identifier_fidelity,
    )
    prompt = prompt + _search_discipline() + _identifier_fidelity()

    # ── 5b. PART 1 governance awareness (always-on, fail-safe): inline the governance
    #        skills' SKILL.md content directly into the PLAN prompt + append the short
    #        pointer clause so the plan is conditioned on the standards. There is no CLI
    #        plugin-loading mechanism (confirmed 2026-07-20 — neither a --plugin/--skill
    #        flag nor a /plugin//skill slash command loads anything headlessly on the
    #        deployed binary), so prompt text is the only channel that reaches the CLI.
    #        "" when no bundle resolves / awareness disabled → prompt unchanged.
    from agents.sdlc_governance import engine as _gov_engine
    from agents.sdlc_implement_prompt import governance_pointer_clause as _gov_pointer
    _gov_block = _gov_engine.resolve_awareness(
        ctx.get("governance_skills"), phase="plan", workspace_root=workspace_root
    )
    if _gov_block:
        prompt = prompt + _gov_pointer(_gov_block)
        logger.info(
            "[SDLC-GOV] Governance awareness added to PLAN prompt (staged read-only in workspace)",
            run_id=run_id,
        )

    # ── 5c. Multi-repo: dependent-repo awareness — append the block already computed
    #        at 5a above (do NOT re-query list_run_repos here).
    if _dep_block:
        prompt = prompt + _dep_block
        logger.info(
            "[SDLC-CLI] Dep block added to PLAN prompt", run_id=run_id,
            dep_count=sum(1 for r in _dep_rows if r.get("kind") != "primary"),
        )

    # ── 6. Drive the read-only CLI (profile=plan pins Sonnet + no Edit tool).
    result = run_cli(
        config=CliEngineConfig.from_env(),
        workspace_root=workspace_root,
        prompt=prompt,
        profile="plan",
        model=cli_plan_model(),
        output_schema=PLAN_SCHEMA,
        max_turns=max_turns,
        run_id=run_id,
        transient_retries=2,   # read-only PLAN: safe to re-spawn on a proxy 502
    )

    # ── 7. Record CLI usage (per-run budget accounting).
    try:
        record_cli_usage(run_id, result.usage or {}, result.total_cost_usd or 0.0)
    except Exception as _bue:
        logger.warning(f"[SDLC {run_id}] PLAN budget accounting failed (non-fatal): {_bue}")

    # ── 8. Engine-level suspend (max turns / error subtype / harness abort).
    if result.status == "suspended":
        _suspend_plan(run_id, jira_key, "PLAN", result.reason or "cli suspended")
        return None

    # ── 9. Extract the plan; treat truncated / retry-exhausted / empty as thin.
    plan = result.structured_output
    if plan is None:
        plan = _parse_json(result.result_text or "")
    if (result.subtype == "error_max_structured_output_retries"
            or _looks_truncated_json(result.result_text or "")
            or not isinstance(plan, dict) or not plan):
        _suspend_plan(run_id, jira_key, "PLAN", "plan incomplete")
        return None

    # ── 10. Gate reorder (2026-07-02): PLAN no longer raises a question gate — the
    #         SINGLE question gate (GATE 2) now lives in CLASSIFY, which always runs
    #         before PLAN starts. A PLAN that still can't proceed falls through to
    #         the grounding/manifest suspends below (go-back, not a question gate).

    # ── 11. RETAINED grounding gate. The CLI read files read-only in its subprocess,
    #         which the platform cannot observe, so we GROUND by confirming each cited
    #         EXISTING file exists on the pinned checkout and seeding its content into
    #         a lightweight ctx._reads so the RETAINED predicate can verify it. A cited
    #         path that is NOT on disk is dropped from `kept` → remains a grounding gap
    #         → suspend, which correctly catches hallucinated paths.
    ctx_v, kept = _ground_plan_reads(run_id, plan, workspace_root)
    _affected = (_cls.get("affected_components") or kept)

    ok, reasons, recoverable = _verify_explore_output(
        "PLAN", json.dumps(plan), ctx_v,
        expected_files=kept, required_keys=_PLAN_REQUIRED_KEYS,
        affected_components=_affected,
    )

    # One repair attempt on a recoverable (truncated-looking) verdict — ORDERED FIRST,
    # before the coverage fix round: a truncated plan must be de-truncated before its
    # coverage/grounding is judged.
    if (not ok) and recoverable:
        _repaired = _repair_explore_json(run_id, "PLAN", json.dumps(plan), _PLAN_REQUIRED_KEYS)
        _rplan = _parse_json(_repaired or "")
        if isinstance(_rplan, dict) and _rplan:
            _rok, _rreasons, _rrecoverable = _verify_explore_output(
                "PLAN", json.dumps(_rplan), ctx_v,
                expected_files=kept, required_keys=_PLAN_REQUIRED_KEYS,
                affected_components=_affected,
            )
            if _rok:
                plan, ok, _reasons, recoverable = _rplan, _rok, _rreasons, _rrecoverable
                ctx_v, kept = _ground_plan_reads(run_id, plan, workspace_root)
                _affected = (_cls.get("affected_components") or kept)

    # ── Verdict SPLIT (2026-07-07 coverage-gate fix). Grounding gaps (cited-but-unread
    #     EXISTING file = anti-hallucination) and a structurally-thin plan (required
    #     field empty) are HARD suspends. A residual classifier-candidate coverage gap
    #     (the planner neither changed nor ruled_out a flagged path) gets exactly ONE
    #     bounded fix round, then WARN+PROCEED — never a silent discard of an otherwise
    #     grounded plan (Research Q1/Q2). See _plan_gate_action / _run_plan_fix_round.
    def _split_verdict(_pd):
        return _explore_convergence_verdict(
            "PLAN", _pd, ctx_v, expected_files=kept,
            required_keys=_PLAN_REQUIRED_KEYS, affected_components=_affected,
            final_text=json.dumps(_pd),
        )

    verdict = _split_verdict(plan)
    action, hard_gaps, candidate_gaps = _plan_gate_action(verdict)
    logger.info(
        "[PLAN] gate decision", run_id=run_id,
        grounding_gaps=verdict.get("grounding_gaps") or [],
        coverage_gaps=verdict.get("coverage_gaps") or [],
        action=action,
    )

    # Warm-start persist (4b): stash the best plan + gap list BEFORE any fix round OR
    # suspend so a later resume can warm-start from plan_partial/plan_gaps instead of
    # exploring cold. (patch_run_context merges into context JSON, so the subsequent
    # suspend's own context_patch does not clobber these.)
    try:
        patch_run_context(run_id, {
            "plan_partial": plan,
            "plan_gaps": list(verdict.get("grounding_gaps") or [])
                         + list(verdict.get("coverage_gaps") or []),
        })
    except Exception as _ppe:
        logger.warning(f"[SDLC {run_id}] PLAN warm-start persist failed (non-fatal): {_ppe}")

    if action == "suspend":
        _suspend_plan(run_id, jira_key, "PLAN", f"plan not grounded/complete: {hard_gaps}")
        return None

    if action == "fix_round":
        _resume_enabled = False
        try:
            from agents.sdlc_cli_engine import CliEngineConfig as _CEC
            _resume_enabled = bool(_CEC.from_env().resume_enabled)
        except Exception:
            pass
        _warm_paths = len([p for p in (plan.get("files_to_change") or [])
                           if isinstance(p, str) and p.strip()]) \
            + len([p for p in (plan.get("new_files_needed") or [])
                   if isinstance(p, str) and p.strip()])
        logger.info(
            "[PLAN] fix round start", run_id=run_id, unaddressed_paths=candidate_gaps,
            resume_used=(_resume_enabled and bool(result.session_id)),
            warm_start_paths=_warm_paths,
        )
        _fixed = _run_plan_fix_round(
            run_id, workspace_root, repo_resolved, language,
            plan, candidate_gaps, result.session_id, issue=issue,
        )
        # Reaching the fix_round branch GUARANTEES the ORIGINAL `plan` is already
        # grounding-clean AND has every required field (else _plan_gate_action would
        # have returned "suspend"). So the original is fully shippable modulo one soft
        # coverage candidate — we must NEVER discard it just because the bounded fix
        # round returned something worse. Only a GENUINE hallucination in the fix-round
        # output (a cited EXISTING file that isn't on disk = grounding gap) is allowed
        # to hard-suspend (plan Step 6). Any other regression (a structurally-thin
        # _fixed) falls back to warn+proceed on the grounded original.
        if isinstance(_fixed, dict) and _fixed:
            # Re-ground the fix-round plan (its files_to_change may have changed) and
            # re-judge with a fresh verdict against the re-grounded read-set.
            ctx_v, kept = _ground_plan_reads(run_id, _fixed, workspace_root)
            _affected = (_cls.get("affected_components") or kept)
            _fverdict = _split_verdict(_fixed)
            _fground = _fverdict.get("grounding_gaps") or []
            if _fground:
                # Genuine hallucination in the fix-round output → hard suspend; never
                # ship an ungrounded plan to IMPLEMENT.
                plan = _fixed
                _suspend_plan(run_id, jira_key, "PLAN",
                              f"plan not grounded/complete: {_fground}")
                return None
            _faction, _fhard, _fcand = _plan_gate_action(_fverdict)
            if _faction == "proceed":
                # Fix round fully resolved the coverage gap → adopt the better plan.
                plan = _fixed
            elif _faction == "fix_round":
                # Residual coverage gap AFTER the one allowed round → adopt + warn.
                plan = _fixed
                plan["coverage_warnings"] = _fcand
                logger.warning(
                    "[PLAN] residual coverage gap after fix round — proceeding",
                    run_id=run_id, coverage_warnings=_fcand,
                )
            else:
                # _faction == "suspend" WITHOUT a grounding gap ⇒ _fixed regressed to a
                # structurally-thin plan (required field empty). Keep the grounded
                # ORIGINAL rather than discard a shippable plan over a malformed fix
                # output — warn + proceed on the original's residual candidates.
                plan["coverage_warnings"] = candidate_gaps
                logger.warning(
                    "[PLAN] fix round produced a thin plan — keeping original + proceeding",
                    run_id=run_id, coverage_warnings=candidate_gaps, fix_round_gaps=_fhard,
                )
        else:
            # Fix round produced nothing usable → keep the grounded original plan,
            # attach the residual coverage gaps as a warning, and PROCEED (never
            # discard an otherwise-grounded plan for a mere classifier-guess mismatch).
            plan["coverage_warnings"] = candidate_gaps
            logger.warning(
                "[PLAN] fix round yielded no usable plan — proceeding with residual",
                run_id=run_id, coverage_warnings=candidate_gaps,
            )
    else:
        logger.info(
            "[PLAN] verify", run_id=run_id, ok=True, recoverable=recoverable,
            open_questions=len(plan.get("open_questions") or []), keys_missing=[],
        )

    # ── 12. Persist the PLAN artifact + mirror into run context as analysis/design
    #         FIRST — BEFORE the (non-blocking, best-effort) manifest gate. The plan
    #         must be durable regardless of the gate verdict: if the gate suspends and
    #         a human WAIVES it, resume re-enters IMPLEMENT and reads
    #         run["context"]["analysis"]/["design"]. Persisting AFTER the gate (the old
    #         order) left those empty on a suspend, so a waive ran IMPLEMENT with no
    #         plan and failed. The existing _pregate_codegen(design=plan, analysis=plan)
    #         + resume paths read these keys unchanged.
    try:
        _store_artifact(
            run_id, "PLAN", plan, producer="cli-planner",
            input_hash=compute_input_hash(run_id, "PLAN"),
            created_by="sdlc", reason="cli plan phase",
        )
    except Exception as _sae:
        logger.warning(f"[SDLC {run_id}] PLAN artifact store failed (non-fatal): {_sae}")
    patch_run_context(run_id, {"plan": plan, "analysis": plan, "design": plan})

    # ── 13. MANIFEST_VALIDATION gate with ONE bounded auto-correction round
    #         (2026-07-13). The plan is already durable above, so any suspend here is
    #         safely waivable. Flow: validate → PASS proceeds; on REJECT, if under the
    #         correction budget, re-plan ONCE with the judge's structured reject
    #         reasons, re-persist the revised plan, and re-validate ONCE. Only a
    #         still-failing round suspends to HITL (with the feedback persisted so a
    #         later COLD manual retry re-plans WITH it — Step 4). Research: Reflexion /
    #         self-refine — exactly ONE critique→revise→recommit round then escalate;
    #         the external verifier is Step-1's deterministic disk check + the CLI
    #         re-reading real source, never the judge grading its own prose.
    def _run_manifest_gate(_plan_arg):
        _wi = (get_run(run_id) or {}).get("context", {}).get("work_item") or {}
        try:
            return _phase_validate_manifest(
                run_id, jira_key, _wi, _plan_arg, _plan_arg, workspace_root,
            )
        except Exception as _mve:
            # Best-effort: a crashing cross-check must not silently pass — fail toward
            # the gate rather than certify an unvalidated plan.
            logger.warning(f"[SDLC {run_id}] PLAN manifest validation errored (non-fatal): {_mve}")
            return False, [f"manifest validation error: {_mve}"]

    def _read_manifest_reasons() -> dict:
        """Structured reject reasons from the just-written MANIFEST_VALIDATION artifact.
        `_finish` stores missing_components / oos_violations / openai_issues on every
        REJECT return — this reads them back as the single feedback contract."""
        try:
            from store.sdlc_artifacts import _load_latest_artifact as _lla
            _art = _lla(run_id, "MANIFEST_VALIDATION") or {}
            _pl = _art.get("payload") or {}
            return {
                "missing_components": list(_pl.get("missing_components") or []),
                "out_of_scope_violations": list(_pl.get("oos_violations") or []),
                "issues": list(_pl.get("openai_issues") or []),
            }
        except Exception as _rae:
            logger.warning(f"[SDLC {run_id}] manifest reason read failed (non-fatal): {_rae}")
            return {"missing_components": [], "out_of_scope_violations": [], "issues": []}

    mv_pass, mv_issues = _run_manifest_gate(plan)

    if not mv_pass:
        _ctx_now = (get_run(run_id) or {}).get("context", {}) or {}
        _attempts = int(_ctx_now.get("manifest_correction_attempts") or 0)
        try:
            _max_corr = int(os.getenv("SDLC_MANIFEST_MAX_CORRECTIONS", "1"))
        except (TypeError, ValueError):
            logger.warning(
                "[PLAN] invalid SDLC_MANIFEST_MAX_CORRECTIONS — using default 1",
                run_id=run_id, raw=os.getenv("SDLC_MANIFEST_MAX_CORRECTIONS"),
            )
            _max_corr = 1
        if _attempts < _max_corr:
            _reasons = _read_manifest_reasons()
            logger.info(
                "[PLAN] manifest auto-correction round start", run_id=run_id,
                attempt=_attempts + 1,
                missing_components=_reasons.get("missing_components"),
                oos_violations=_reasons.get("out_of_scope_violations"),
            )
            _fixed = _run_plan_fix_round(
                run_id, workspace_root, repo_resolved, language,
                plan, _reasons.get("missing_components") or [],
                result.session_id, issue=issue, manifest_feedback=_reasons,
            )
            if isinstance(_fixed, dict) and _fixed:
                plan = _fixed
                # Re-persist the REVISED plan BEFORE re-validation so a later waive/
                # resume reads the corrected plan, not the stale one (the bug fixed in
                # project_sdlc_manifest_plan_persist_2026_07_09).
                try:
                    _store_artifact(
                        run_id, "PLAN", plan, producer="cli-planner",
                        input_hash=compute_input_hash(run_id, "PLAN"),
                        created_by="sdlc", reason="cli plan phase (manifest auto-correction)",
                    )
                except Exception as _sae2:
                    logger.warning(f"[SDLC {run_id}] revised PLAN artifact store failed (non-fatal): {_sae2}")
                patch_run_context(run_id, {"plan": plan, "analysis": plan, "design": plan})
            else:
                logger.warning(
                    "[PLAN] manifest auto-correction produced no usable plan — re-validating original",
                    run_id=run_id, attempt=_attempts + 1,
                )
            # Increment the ctx counter REGARDLESS of fix-round outcome so the bound
            # holds even if the fix round returned nothing usable.
            patch_run_context(run_id, {"manifest_correction_attempts": _attempts + 1})
            mv_pass, mv_issues = _run_manifest_gate(plan)
            logger.info(
                "[PLAN] manifest auto-correction result", run_id=run_id,
                attempt=_attempts + 1, revalidate_pass=bool(mv_pass),
            )

    if not mv_pass:
        # Round exhausted (or disabled via SDLC_MANIFEST_MAX_CORRECTIONS=0) → persist
        # the structured feedback so a later COLD manual retry re-plans WITH it (Step 4),
        # then suspend to HITL. patch_run_context merges into the context JSON, so the
        # subsequent _suspend_plan context_patch will NOT clobber this key.
        _reasons = _read_manifest_reasons()
        _fb = {
            "missing_components": _reasons.get("missing_components") or [],
            "out_of_scope_violations": _reasons.get("out_of_scope_violations") or [],
            "issues": _reasons.get("issues") or [],
            "attempts": int((get_run(run_id) or {}).get("context", {}).get("manifest_correction_attempts") or 0),
        }
        try:
            patch_run_context(run_id, {"manifest_feedback": _fb})
        except Exception as _fbe:
            logger.warning(f"[SDLC {run_id}] manifest_feedback persist failed (non-fatal): {_fbe}")
        logger.warning(
            "[PLAN] manifest gate exhausted — suspending with feedback", run_id=run_id,
            attempts=_fb["attempts"],
            feedback_keys=[k for k, v in _fb.items() if v and k != "attempts"],
        )
        _suspend_plan(run_id, jira_key, "PLAN", f"manifest validation failed: {mv_issues[:3]}")
        return None

    # Manifest PASSED → clear any prior feedback from ctx so stale reject reasons never
    # bias an unrelated future re-plan (Step 4.3).
    try:
        _ctx_after = (get_run(run_id) or {}).get("context", {}) or {}
        if _ctx_after.get("manifest_feedback"):
            patch_run_context(run_id, {"manifest_feedback": None})
            logger.info("[PLAN] cleared manifest_feedback after PASS", run_id=run_id)
    except Exception:
        pass

    # ── 14. Success.
    return plan


def _run_review_phase(run_id: str, diff_text: str, plan: dict,
                      prior_issues: list | None = None,
                      added_files: list | None = None) -> dict:
    """CLI three-phase engine — REVIEW phase (Step 6 pipeline half).

    Platform-controlled Opus review over the DIFF ONLY. Returns
    {"approved": bool, "blocking_issues": [{"file","line"?,"issue","fix_hint"}],
    "notes": str}. Never raises."""
    try:
        plan = plan or {}
        _approach = plan.get("solution_approach", "") or ""

        # A plan lists MODIFIED existing files in files_to_change and NEW files (a DB
        # migration/SQL, a new class, etc.) in the SEPARATE new_files_needed key. Both
        # are in-scope; both show up in the unified diff (git add -A captures untracked
        # new files). Give the reviewer BOTH lists — a plan-declared new file (e.g.
        # upgrade_v12.sql) is expected, not scope creep. Feeding only files_to_change is
        # what caused REVIEW to falsely flag legitimately-planned new files as
        # "out of scope" and the fix round to delete them (P3). Items may be plain path
        # strings or {path/file: ...} dicts — normalize either shape.
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

        _changed_files = _paths(plan.get("files_to_change"))
        _new_files = _paths(plan.get("new_files_needed"))

        # JUSTIFIED OUT-OF-SCOPE ADDITIONS (scope-guard → REVIEW handoff): files the
        # coder created/modified OUTSIDE the plan and DECLARED with a reason. The
        # deterministic scope guard let these through specifically so this reviewer can
        # judge them on merit — without this list the reviewer would (correctly, per the
        # old rules) flag them as scope creep and the fix round could delete them (the
        # documented P3 regression). Each item is {"path", "kind", "reason"}.
        _added_files_justified = "\n".join(
            f"- {a.get('path')} ({a.get('kind', 'modify')}): "
            f"{a.get('reason') or '(no justification provided)'}"
            for a in (added_files or [])
        ) or "(none — IMPLEMENT added no unplanned files)"

        # FOLLOW-UP REVIEW SCOPING (monotonic close): when this is a re-review after a
        # fix round, the reviewer VERIFIES the previously-flagged issues ONLY. It may
        # NOT introduce new findings — that is what caused the endless "new question
        # each round" loop. The open-issue set can then only shrink → the loop closes.
        import json as _json_pr
        _prior = prior_issues or []
        _scope_clause = ""
        if _prior:
            _scope_clause = (
                "THIS IS A FOLLOW-UP REVIEW. A previous review flagged the issues below and "
                "the engineer has since attempted to fix them. Your ONLY job is to verify, for "
                "EACH prior issue, whether it is now resolved in the diff.\n"
                "- Do NOT raise any new issue that is not one of the PRIOR ISSUES below.\n"
                "- Return in blocking_issues ONLY those PRIOR ISSUES that are STILL unresolved, "
                "each referencing the same issue text.\n"
                "- If every prior issue is resolved, return approved=true with an empty "
                "blocking_issues list.\n\n"
                f"PRIOR ISSUES (verify these only):\n{_json_pr.dumps(_prior, indent=2, default=str)}\n\n"
            )

        # 1. Diff-ONLY prompt: the reviewer sees only the unified diff hunks, the
        #    intended approach, and the changed-file paths — NOT full file bodies or
        #    unrelated files.
        review_prompt = (
            "You are a senior code reviewer. Review ONLY the unified diff below against "
            "the intended solution approach. Judge correctness, scope adherence, and "
            "security. You are NOT given full file bodies — review only the hunks shown.\n\n"
            f"{_scope_clause}"
            f"INTENDED APPROACH:\n{_approach}\n\n"
            f"MODIFIED FILES (existing files the plan changes): {_changed_files}\n\n"
            f"NEW FILES (the plan declares these must be CREATED — creating them is IN "
            f"SCOPE, not scope creep): {_new_files}\n\n"
            f"JUSTIFIED OUT-OF-SCOPE CHANGES (the engineer created/modified these files "
            f"OUTSIDE the plan and gave a reason why implementing the plan required them — "
            f"judge each on merit, do NOT auto-reject as scope creep):\n"
            f"{_added_files_justified}\n\n"
            "SCOPE RULES:\n"
            "- Adding a brand-new file that appears in the NEW FILES list above (e.g. a DB "
            "migration/SQL script, a new module) is EXPECTED and in scope — do NOT flag it "
            "as out-of-scope.\n"
            "- A file listed under JUSTIFIED OUT-OF-SCOPE CHANGES is a deliberate, DECLARED "
            "deviation from the plan. Do NOT flag it merely for being outside the plan. "
            "Instead, JUDGE the justification: approve when the reason is valid and the change "
            "is correct and consistent with the intended approach; raise a blocking issue ONLY "
            "when the justification is weak/absent, the change is wrong, or it does work the "
            "approach does not call for. Never instruct deleting such a file solely because it "
            "is not in the plan.\n"
            "- Only flag a file as out-of-scope if it is in the diff but appears in NONE of the "
            "MODIFIED FILES, NEW FILES, or JUSTIFIED OUT-OF-SCOPE CHANGES lists, or if a hunk "
            "clearly does work the intended approach does not call for.\n"
            "- Also check the diff HONORS the intended approach's domain terminology: if the "
            "approach specifies a domain qualifier, flag a hunk that uses a contradicting "
            "look-alike symbol.\n\n"
            "UNIFIED DIFF:\n"
            "```diff\n"
            f"{diff_text or ''}\n"
            "```\n\n"
            "Return STRICT JSON only, no prose or fences:\n"
            '{"approved": true|false, '
            '"blocking_issues": [{"file": "...", "line": 0, "issue": "...", "fix_hint": "..."}], '
            '"notes": "..."}'
        )

        # 2. ONE in-process platform call — Opus (honors ENABLE_OPUS→Sonnet via the
        #    code_review stage tier). This is the platform reviewer, NOT the CLI.
        _review_model = _sdlc_model("code_review")
        # ── FULL PROMPT DUMP (no stripping / no truncation) — for diagnosing why the
        #    diff review approves/blocks. Delimited so it is easy to extract from logs.
        logger.info(
            f"[REVIEW {run_id}] ===== FULL DIFF-REVIEW PROMPT BEGIN "
            f"(model={_review_model} chars={len(review_prompt)} diff_bytes={len(diff_text or '')}) =====\n"
            f"{review_prompt}\n"
            f"[REVIEW {run_id}] ===== FULL DIFF-REVIEW PROMPT END ====="
        )
        raw = _llm(review_prompt, hint=_review_model, agent_id="sdlc-diff-reviewer")
        # ── FULL RESPONSE DUMP (no stripping / no truncation) ──
        logger.info(
            f"[REVIEW {run_id}] ===== FULL DIFF-REVIEW RESPONSE BEGIN "
            f"(chars={len(raw or '')}) =====\n{raw}\n"
            f"[REVIEW {run_id}] ===== FULL DIFF-REVIEW RESPONSE END ====="
        )

        # 3. Coerce to schema. Fail toward BLOCKING on any parse failure — never
        #    silently approve an unparseable review.
        verdict = _parse_json(raw or "")
        if not isinstance(verdict, dict):
            verdict = {}
        _approved = verdict.get("approved")
        if not isinstance(_approved, bool):
            verdict["approved"] = False
            verdict.setdefault("notes", "review verdict unparseable — blocking")
        _bi = verdict.get("blocking_issues")
        if not isinstance(_bi, list):
            verdict["blocking_issues"] = []
        verdict.setdefault("notes", "")

        # 4. Do NOT double-record cost: the _llm() path already books HOD cost
        #    internally for this in-process call.

        # 5. Log the verdict. The CALLER logs the warning + drives any fix round on
        #    an unresolved (not approved) verdict — this function only returns it.
        logger.info(
            "[REVIEW] diff-review verdict", run_id=run_id,
            approved=verdict.get("approved"),
            blocking=len(verdict.get("blocking_issues") or []),
            diff_bytes=len(diff_text or ""),
        )

        # 6. Emit a REVIEW run event (actor is a plain string).
        try:
            add_run_event(
                run_id, from_state="REVIEW", to_state="REVIEW", stage="REVIEW",
                actor="diff-reviewer", output=(verdict.get("notes") or ""),
                data={
                    "approved": verdict.get("approved"),
                    "blocking": len(verdict.get("blocking_issues") or []),
                },
            )
        except Exception as _ee:
            logger.debug(f"[SDLC {run_id}] REVIEW run event emit failed: {_ee}")

        return verdict
    except Exception as _e:
        # 7. Never raise — fail toward blocking.
        return {
            "approved": False,
            "blocking_issues": [{"issue": f"review call failed: {_e}"}],
            "notes": "review error",
        }


def _run_governance_review_phase(run_id: str, workspace: str, diff_text: str,
                                 changed_files: list, product_id, repo: str,
                                 subset=None, db=None) -> dict:
    """DEPRECATED shim (scan-unify 2026-07-28). Historically ran ONE diff-only
    governance CLI session; now delegates to the unified per-skill parallel scan core
    ``run_governance_scan_snapshot`` so every governance trigger uses the SAME engine
    (per-skill ``scan_all_skills``, not the retired single-session ``run_review``).

    Retained only for the dead SM ``_run_governance_review`` caller — the live callers
    (the in-pipeline end-gate and the standalone worker job) now call the core directly.
    Returns the core's rich dict (a superset of the old shape). Never raises."""
    return run_governance_scan_snapshot(
        run_id, workspace=workspace, diff_text=diff_text, changed_files=changed_files,
        product_id=product_id, repo=repo, subset=subset, db=db,
    )


def run_governance_scan_snapshot(run_id: str, *, workspace: str, diff_text: str,
                                 changed_files: list, product_id, repo: str,
                                 base_sha: str = "HEAD", subset=None, db=None,
                                 trigger: str = "initial",
                                 created_by: str = None) -> dict:
    """THE single governance scan+persist core (scan-unify 2026-07-28).

    The ONE primitive shared by every governance trigger:
      - the standalone ``run_governance_pipeline`` (Send-to-Governance / API),
      - the in-pipeline END-GATE (``sdlc_state_machine._run_governance_endgate``), and
      - the standalone worker job (``workers.sdlc_worker.run_governance_review_job``).

    Runs one agentic CLI scan session PER SKILL in PARALLEL via ``scan_all_skills``
    (this is the change that made every trigger spawn N sessions instead of one), applies
    per-(product,repo) suppressions, DUAL-WRITES findings (legacy
    ``sdlc_governance_findings`` upsert) + an immutable scan snapshot (+ observations),
    renders the report, and returns the rich dict every caller reads. Never raises —
    fails CLOSED (blocking) on an unexpected internal error.

    ``base_sha`` labels the per-skill scan prompt's ``base_sha...HEAD`` reference; the
    diff itself is precomputed by the caller. Callers that clone-and-diff against the MR
    base branch should pass the merge-base SHA so the prompt label is accurate.

    Returns: ``{report, blocking, open_findings, suppressed, skills, skipped,
    scan_error(+scan_error_detail), diff_too_large, snapshot_id, domain_by_skill}``.
    - ``blocking``      = any non-suppressed finding at/above ``block_severity()``.
    - ``skipped``       = True when no bundle/skills resolve (governance simply not run).
    - ``scan_error``    = availability error (diff too large, or CLI could not complete)
                          — the caller SUSPENDS for a human retry, it is NOT a violation.
    """
    from agents.sdlc_governance import config as gov_config, engine as gov_engine
    from agents.sdlc_governance.engine import scan_all_skills
    from agents.sdlc_governance.schema import parse_findings, is_blocking
    from agents.sdlc_cli_engine import AinxtCliEngine

    logger.info(
        "[SDLC-GOV] unified scan start", run_id=run_id, trigger=trigger,
        changed_files=len(changed_files or []), diff_chars=len(diff_text or ""),
        base_sha=base_sha,
    )
    try:
        # 1. Diff-size cap (fail-closed toward a human): a diff above the file/byte cap
        #    is too large for one meaningful automated pass and would overflow the CLI
        #    --print argv token. Return the scan_error sentinel (with diff_too_large) so
        #    every caller SUSPENDS for manual review rather than crashing or passing.
        _too_large = gov_config.diff_cap_exceeded(changed_files, diff_text)
        if _too_large:
            logger.warning(
                "[SDLC-GOV] diff exceeds governance cap — routing to manual review",
                run_id=run_id, repo=repo, detail=_too_large,
                changed_files=len(changed_files or []), diff_chars=len(diff_text or ""),
            )
            return {
                "report": {"overall_verdict": "FAIL", "ref": "", "skills": [],
                           "report_md": f"Governance review not run — {_too_large}"},
                "blocking": False, "open_findings": [], "suppressed": [],
                "skills": [], "skipped": False,
                "scan_error": True, "scan_error_detail": _too_large, "diff_too_large": True,
                "snapshot_id": None, "domain_by_skill": {},
            }

        # 2. Resolve the governance bundle + selected skills.
        bundle, skills = gov_engine.select_skills(subset, phase="review")
        if not bundle or not skills:
            logger.warning(
                "[SDLC-GOV] no governance skills resolved — skipping governance scan",
                run_id=run_id, repo=repo,
            )
            return {"report": None, "blocking": False, "open_findings": [],
                    "suppressed": [], "skills": [], "skipped": True,
                    "scan_error": False, "diff_too_large": False,
                    "snapshot_id": None, "domain_by_skill": {}}

        # 3. Scan — ONE agentic session PER SKILL, in parallel (ThreadPoolExecutor).
        structured, domain_by_skill = scan_all_skills(
            engine=AinxtCliEngine(), bundle=bundle, skills=skills,
            workspace_root=workspace, diff_text=diff_text, changed_files=changed_files,
            base_sha=base_sha or "HEAD", model=gov_config.review_model(), run_id=run_id,
        )

        # 4. Scan-engine failure (a skill session hit max_turns / crashed / timed out)
        #    → scan_error sentinel. That is an availability error, NOT a real finding;
        #    surfacing it as a blocking violation would route it into the approval gate.
        if structured.get("_scan_error"):
            _err_detail = "; ".join(
                (f.get("detail") or "")
                for sk in (structured.get("skills") or [])
                for f in (sk.get("findings") or [])
                if isinstance(f, dict) and (f.get("detail") or "")
            )
            if len(_err_detail) > 300:
                _err_detail = _err_detail[:300] + "…"
            logger.warning(
                "[SDLC-GOV] unified scan could not complete — returning scan_error",
                run_id=run_id, detail=_err_detail,
            )
            return {
                "report": None, "blocking": False, "open_findings": [],
                "suppressed": [], "skills": [s.slug for s in skills],
                "skipped": False, "scan_error": True,
                "scan_error_detail": _err_detail, "diff_too_large": False,
                "snapshot_id": None, "domain_by_skill": domain_by_skill,
            }

        # 5. Suppress (per-(product,repo) active suppressions; fail toward surfacing).
        findings = parse_findings(structured)
        open_f, suppressed_f = gov_engine.apply_suppressions(findings, db, product_id, repo)
        _all = open_f + suppressed_f

        # 6. DUAL-WRITE: legacy findings upsert + immutable scan snapshot (both fail-safe;
        #    in repo/MR-mode there is no sdlc_runs row and both simply no-op).
        from store.sdlc_governance_findings import persist_findings, persist_snapshot
        persist_findings(run_id, _all, domain_by_skill)
        snapshot_id = None
        try:
            import hashlib as _hl
            from agents.sdlc_governance.bundle import (
                governance_bundle_version as _gbv, skill_versions as _skv,
            )
            snapshot_id = persist_snapshot(
                run_id, _all,
                diff_hash=_hl.sha256((diff_text or "").encode("utf-8", "ignore")).hexdigest(),
                bundle_version=_gbv(bundle), skill_versions=_skv(bundle, skills),
                trigger=trigger, created_by=created_by, domain_by_skill=domain_by_skill,
            )
        except Exception as _se:
            logger.warning("[SDLC-GOV] scan snapshot write skipped — non-fatal",
                           run_id=run_id, error=str(_se))

        # 7. Report + 8. blocking verdict.
        report = gov_engine.render_report(
            structured=structured, findings=_all, ref=bundle.ref, skills=skills,
            domain_by_skill=domain_by_skill,
        )
        threshold = gov_config.block_severity()
        blocking = any(is_blocking(f, threshold) for f in open_f)

        logger.info(
            "[SDLC-GOV] unified scan verdict", run_id=run_id,
            overall=(report or {}).get("overall_verdict"),
            open=len(open_f), suppressed=len(suppressed_f), snapshot_id=snapshot_id,
        )
        return {
            "report": report, "blocking": blocking, "open_findings": open_f,
            "suppressed": suppressed_f, "skills": [s.slug for s in skills],
            "skipped": False, "scan_error": False, "diff_too_large": False,
            "snapshot_id": snapshot_id, "domain_by_skill": domain_by_skill,
        }
    except Exception as _ge:
        logger.error("[SDLC-GOV] unified scan errored — fail-closed blocking",
                     run_id=run_id, error=str(_ge))
        return {
            "report": {"overall_verdict": "FAIL", "ref": "", "skills": [],
                       "report_md": f"Governance scan errored: {_ge}"},
            "blocking": True, "open_findings": [], "suppressed": [],
            "skills": [], "skipped": False, "scan_error": False,
            "diff_too_large": False, "snapshot_id": None, "domain_by_skill": {},
        }


def _post_governance_mr_note_if_present(run_id: str, repo: str) -> None:
    """
    STEP 11 (2026-07-17) — best-effort governance MR note delivery.

    Called AFTER the post-gate CodingStateMachine.run() returns (the point at
    which COMMITTING/MR_CREATION has already happened inside the SM — the MR
    does not exist yet at GOVERNANCE_REVIEW time, hence posting here rather
    than from the governance phase itself). Re-reads the run row for pr_number
    (set by the SM's MR creation) and, when both an MR and a GOVERNANCE_REPORT
    artifact exist, posts/updates the governance note on the MR. Never raises —
    a note-post failure must never fail the pipeline or mask the MR.
    """
    try:
        run = get_run(run_id)
        pr_number = (run or {}).get("pr_number")
        if not pr_number:
            return  # no MR was created on this pass (suspended / commit failed)
        from store.sdlc_artifacts import _load_latest_artifact
        artifact = _load_latest_artifact(run_id, "GOVERNANCE_REPORT")
        if not artifact:
            return  # governance review didn't run for this run — nothing to post
        report_md = (artifact.get("payload") or {}).get("report_md") or ""
        if not report_md.strip():
            return
        from tools.gitlab_tools import gitlab_post_governance_note
        result = gitlab_post_governance_note(repo, int(pr_number), report_md)
        logger.info(f"[SDLC-GOV] MR note posted for run={run_id} MR=!{pr_number}: {result}")
    except Exception as e:
        logger.warning(f"[SDLC-GOV] _post_governance_mr_note_if_present failed for run={run_id} (best-effort): {e}")

