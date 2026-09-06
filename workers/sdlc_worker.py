# SPDX-License-Identifier: MIT
# ============================================================
# SDLC WORKER — rq job wrappers for SDLC pipelines
# Each function is importable by rq workers.
# ============================================================

import os

from core.logger import logger, bind_context, clear_bound_context
from store.sdlc_store import SDLCCancelled


def _release_slot(issue_dict: dict) -> None:
    """Release the SDLC dedup + user-counter slot after any pipeline outcome."""
    try:
        from core.job_queue import release_sdlc_slot
        jira_key = issue_dict.get("key", "")
        reporter  = (
            issue_dict.get("assignee") or issue_dict.get("reporter")
            or issue_dict.get("triggered_by_email") or issue_dict.get("triggered_by_user_id")
            or "unknown"
        )
        # Owner token must mirror what enqueue_sdlc_job stored as the slot value:
        # the run_id when the caller pre-created the run, else this rq job's id.
        owner = issue_dict.get("_run_id")
        if not owner:
            try:
                from rq import get_current_job
                _job = get_current_job()
                owner = _job.id if _job else None
            except Exception:
                owner = None
        release_sdlc_slot(jira_key, reporter, owner=owner)
    except Exception as _e:
        logger.warning(f"sdlc_worker: release_slot failed: {_e}")


def run_feature_pipeline_job(issue_dict: dict) -> str:
    """rq job: run the full feature SDLC pipeline."""
    run_id = issue_dict.get("_run_id")
    bind_context(correlation_id=run_id or "", pipeline_stage="sdlc_feature")
    try:
        from agents.sdlc_pipeline import run_feature_pipeline
        run_feature_pipeline(issue_dict, run_id)
        return f"feature pipeline completed for {issue_dict.get('key', 'unknown')}"
    except Exception as e:
        logger.error(f"sdlc_worker: feature pipeline failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise
    finally:
        _release_slot(issue_dict)


def run_bug_pipeline_job(issue_dict: dict) -> str:
    """rq job: run the full bug SDLC pipeline."""
    run_id = issue_dict.get("_run_id")
    bind_context(correlation_id=run_id or "", pipeline_stage="sdlc_bug")
    try:
        from agents.sdlc_pipeline import run_bug_pipeline
        run_bug_pipeline(issue_dict, run_id)
        return f"bug pipeline completed for {issue_dict.get('key', 'unknown')}"
    except Exception as e:
        logger.error(f"sdlc_worker: bug pipeline failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise
    finally:
        _release_slot(issue_dict)


def run_pr_review_pipeline_job(pr_dict: dict) -> str:
    """rq job: run PR review pipeline."""
    run_id = pr_dict.get("_run_id")
    bind_context(correlation_id=run_id or "", pipeline_stage="sdlc_pr_review")

    # Acquire the same lock used by the inline webhook thread to prevent double-execution.
    # If the inline thread already claimed this run, skip.
    if run_id:
        try:
            from core.config import REDIS_HOST as _H, REDIS_PORT as _P
            import redis as _r
            _rc = _r.Redis(host=_H, port=_P, db=5, decode_responses=True,
                           socket_connect_timeout=2)
            _lock_key = f"pr_review:running:{run_id}"
            acquired  = _rc.set(_lock_key, "1", nx=True, ex=1800)
            if not acquired:
                logger.info(f"sdlc_worker: inline thread already running PR review {run_id} — job skipping")
                return f"PR review already running inline for run {run_id}"
        except Exception:
            pass  # Redis unavailable — proceed (both may run, idempotent transitions handle it)

    try:
        from agents.sdlc_pipeline import run_pr_review_pipeline
        run_pr_review_pipeline(pr_dict, run_id)
        return f"PR review pipeline completed for PR #{pr_dict.get('number', 'unknown')}"
    except Exception as e:
        logger.error(f"sdlc_worker: PR review pipeline failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise


def address_pr_comments_job(payload: dict) -> str:
    """
    rq job: AI addresses reviewer comments on a PR.

    payload keys:
        run_id     — SDLC run ID (required)
        repo       — owner/repo
        pr_number  — PR number (int)
    """
    run_id = payload.get("run_id")
    bind_context(correlation_id=run_id or "", pipeline_stage="sdlc_pr_comments")
    try:
        from agents.sdlc_pipeline import address_pr_review_comments
        address_pr_review_comments(run_id)
        return f"PR review comments addressed for run {run_id}"
    except Exception as e:
        logger.error(f"sdlc_worker: address_pr_comments failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise


def merge_pr_job(payload: dict) -> str:
    """
    rq job: merge an approved PR.

    payload keys:
        run_id     — SDLC run ID (required)
        repo       — owner/repo (optional; looked up from run if absent)
        pr_number  — PR number (optional; looked up from run if absent)
    """
    run_id = payload.get("run_id")
    bind_context(correlation_id=run_id or "", pipeline_stage="sdlc_merge")
    try:
        from store.sdlc_store import get_run, update_run_state, add_run_event
        from tools.gitlab_tools import gitlab_merge_mr

        run = get_run(run_id)
        if not run:
            raise ValueError(f"SDLC run {run_id} not found")

        repo      = payload.get("repo") or run.get("repo", "")
        pr_number = payload.get("pr_number") or run.get("pr_number")

        if not repo or not pr_number:
            raise ValueError(f"merge_mr_job: missing repo/mr_number for run {run_id}")

        result = gitlab_merge_mr(repo=repo, mr_iid=int(pr_number))
        if result.startswith("[Error"):
            raise RuntimeError(result)

        update_run_state(run_id, "MERGED")
        add_run_event(run_id, "MERGED", "ai-merger", f"MR !{pr_number} merged",
                      {"mr_number": pr_number, "result": result})
        logger.info(f"sdlc_worker: merged MR !{pr_number} for run {run_id}")
        return f"MR !{pr_number} merged for run {run_id}"

    except Exception as e:
        logger.error(f"sdlc_worker: merge_pr_job failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise


def resume_feature_job(payload: dict) -> str:
    """rq job: resume the feature pipeline after HITL design approval."""
    run_id   = payload.get("run_id", "")
    feedback = payload.get("feedback", "")
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_feature_resume")
    try:
        from agents.sdlc_pipeline import resume_feature_after_design_approval
        resume_feature_after_design_approval(run_id, feedback)
        return f"feature resume completed for {run_id}"
    except Exception as e:
        logger.error(f"sdlc_worker: resume_feature_job failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise


def resume_bug_job(payload: dict) -> str:
    """rq job: resume the bug pipeline after HITL solution approval."""
    run_id   = payload.get("run_id", "")
    feedback = payload.get("feedback", "")
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_bug_resume")
    try:
        from agents.sdlc_pipeline import resume_bug_after_solution_approval
        resume_bug_after_solution_approval(run_id, feedback)
        return f"bug resume completed for {run_id}"
    except Exception as e:
        logger.error(f"sdlc_worker: resume_bug_job failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise


def run_pre_sm_resume_job(payload: dict) -> str:
    """rq job: WS-0 re-entrant pre-SM resume (gate-reorder, 2026-07-02).

    Replaces re-enqueuing the whole run_feature_pipeline_job/run_bug_pipeline_job
    after GATE 1 (normalization approval) or GATE 2 (classify questions) —
    resumes at `start_at` via agents.sdlc_pipeline.resume_pre_sm_pipeline, which
    skips every already-durable phase (PREFLIGHT/BASELINE/NORMALIZE/CLASSIFY)
    instead of restarting the pipeline from the top."""
    run_id   = payload.get("run_id", "")
    start_at = payload.get("start_at", "")
    bind_context(correlation_id=run_id, pipeline_stage=f"sdlc_pre_sm_resume_{start_at.lower()}")
    try:
        from agents.sdlc_pipeline import resume_pre_sm_pipeline
        resume_pre_sm_pipeline(run_id, start_at)
        return f"pre-sm resume completed for {run_id} (start_at={start_at})"
    except Exception as e:
        logger.error(f"sdlc_worker: run_pre_sm_resume_job failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise


def run_build_metadata_resume_job(payload: dict) -> str:
    """rq job: resume a run paused at AWAITING_BUILD_METADATA_APPROVAL once the
    operator has confirmed which language version to use (Issue 1). Persisting the
    confirmed version may re-clone the base-branch checkout, so this runs off the
    HTTP request path. Delegates to agents.sdlc_pipeline.resume_build_metadata_gate,
    which re-enqueues the pre-SM pipeline at BASELINE on success."""
    run_id         = payload.get("run_id", "")
    choice         = payload.get("choice", "")
    chosen_version = payload.get("chosen_version", "")
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_build_metadata_resume")
    try:
        from agents.sdlc_pipeline import resume_build_metadata_gate
        resume_build_metadata_gate(run_id, choice, chosen_version)
        return f"build-metadata resume completed for {run_id} (choice={choice})"
    except Exception as e:
        logger.error(f"sdlc_worker: run_build_metadata_resume_job failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise


def resume_from_stage_job(payload: dict) -> str:
    """rq job: generic stage resume for the flexible pipeline."""
    run_id       = payload.get("run_id", "")
    target_stage = payload.get("target_stage", "")
    mode         = payload.get("mode", "retry")
    feedback     = payload.get("feedback", "")
    actor        = payload.get("actor", "system")
    reason       = payload.get("reason", "")
    bind_context(correlation_id=run_id, pipeline_stage=f"sdlc_resume_{target_stage.lower()}")
    try:
        from store.sdlc_store import get_run, update_run_state
        run = get_run(run_id)
        if not run:
            logger.error(f"resume_from_stage_job: run {run_id} not found")
            return f"run {run_id} not found"

        ctx      = run.get("context") or {}
        design   = ctx.get("design") or {}
        analysis = ctx.get("analysis") or {}
        repo     = run.get("repo") or ""
        language = (ctx.get("repo_ctx") or {}).get("language") if isinstance(ctx.get("repo_ctx"), dict) else ctx.get("language") or ""
        if not language:
            # repo_ctx may be a nested map {repo: {language: ...}}; flatten
            _rctx = ctx.get("repo_ctx") or {}
            for _v in _rctx.values():
                if isinstance(_v, dict) and _v.get("language"):
                    language = _v["language"]
                    break

        from agents.sdlc_state_machine import CodingStateMachine
        from agents.sdlc_pipeline import _resolve_gitlab_repo
        gitlab_repo    = _resolve_gitlab_repo(repo)
        base_branch    = ctx.get("base_branch", "")
        working_branch = ctx.get("working_branch", "")
        user_id        = ctx.get("user_id") or run.get("triggered_by") or ""
        user_email     = ctx.get("user_email", "")

        # Inject feedback as a correction note in context so _phase_* picks it up
        if feedback:
            update_run_state(run_id, "RUNNING",
                             context_patch={"resume_feedback": feedback, "resume_stage": target_stage})

        sm = CodingStateMachine(
            run_id=run_id,
            jira_key=run.get("jira_key", ""),
            repo=repo,
            language=language or "python",
            design=design,
            analysis=analysis,
            base_branch=base_branch,
            working_branch=working_branch,
            gitlab_repo=gitlab_repo,
            skip_tests=bool(ctx.get("skip_tests", False)),
            skip_slt=bool(ctx.get("skip_slt", False)),
            compile_skipped=bool(ctx.get("compile_skipped", False)),
            user_id=user_id,
            user_email=user_email,
        )
        # Governance (STEP 8) — thread the run-context flags onto the reconstructed
        # SM instance, same as the primary trigger-time construction; set AFTER
        # construction per the governance package's contract (attributes, not ctor kwargs).
        # Defensive bool coercion mirrors skip_tests/skip_slt above (ctx may carry a
        # JSON-serialized "true"/"false" string instead of a real bool).
        _gov_raw = ctx.get("run_governance_review", False)
        sm.run_governance_review = _gov_raw if isinstance(_gov_raw, bool) else str(_gov_raw).strip().lower() in ("1", "true", "yes")
        sm.governance_subset = ctx.get("governance_skills")

        # Corrective feedback for this resume/go-back — steers the coder decisively.
        # Prefer the payload feedback; fall back to the value persisted in context.
        sm._resume_feedback = feedback or ctx.get("resume_feedback", "") or ""

        # Rehydrate in-memory state from durable artifacts so resumed phases that
        # read self.code_output / self.slt_output have real data. The three-phase
        # engine stores the coder's output as VERIFIED_DIFF (there is NO "CODING"
        # artifact — reading it always yielded {}), so derive code_output from the
        # VERIFIED_DIFF edits. The post-gate APPLYING phase re-bridges code_output
        # from the applied workspace anyway; this hydration just gives any code that
        # reads code_output before APPLYING real file data instead of an empty dict.
        _vd = sm._get_artifact("VERIFIED_DIFF") or {}
        sm.code_output = {
            "files": [
                {
                    "path":    e.get("path"),
                    "content": e.get("new_body", ""),
                    "is_new":  bool(e.get("is_new")),
                    "is_test": bool(e.get("is_test")),
                    "deleted": bool(e.get("deleted")),
                }
                for e in (_vd.get("edits") or [])
                if e.get("kind") == "code" and e.get("path")
            ],
            "summary": _vd.get("summary", ""),
        }
        sm.slt_output  = sm._get_artifact("SLT") or {}
        sm._risk_score = float((sm._get_artifact("CLASSIFYING") or {}).get("risk_score", 0.5))

        # Three-phase CLI engine resume targets:
        #
        #  • IMPLEMENT / REVIEW → re-run the merged PRE-GATE implement (fresh CLI
        #    session: code + tests + green, then platform diff-review), which
        #    re-captures + re-reviews + re-produces the VERIFIED_DIFF and re-gates
        #    to AWAITING_*_APPROVAL. The engineer's _resume_feedback is folded into
        #    the implement prompt. mode MUST be "pregate" so _proceed_post_tests
        #    finalizes the VERIFIED_DIFF instead of committing straight through.
        #  • COMMITTING / TEST_VERIFY (post-gate) → run the deterministic post-gate
        #    machine. It reads the approved VERIFIED_DIFF, APPLIES it to the workspace
        #    (bridging code_output), re-verifies staleness, then chains
        #    APPLYING → TEST_VERIFY → SLT_RUNNING → COMMITTING → MR. TEST_VERIFY
        #    lands here when retry is requested from a suspended-after-tests run.
        #    Resuming COMMITTING in isolation is WRONG for this engine — the diff must
        #    be applied first.  Note: a REVIEW waive no longer routes to APPLYING — it
        #    routes to the AWAITING_APPROVAL sentinel handled just below. The only
        #    remaining APPLYING source is a TEST_VERIFY waive (post-gate), whose bare
        #    "APPLYING" is normalised to "COMMITTING" by _suspend(), so this branch is
        #    the correct handler for it.
        if target_stage == "AWAITING_APPROVAL":
            # Waive-of-REVIEW: do NOT re-implement and do NOT apply/commit. The
            # VERIFIED_DIFF was already finalized when REVIEW suspended; simply present
            # it at the human diff-approval gate, exactly like a passing REVIEW.
            _vd_w = sm._get_artifact("VERIFIED_DIFF") or {}
            if not _vd_w.get("edits"):
                _msg = ("Cannot waive REVIEW: no VERIFIED_DIFF artifact for this run. "
                        "Resume at IMPLEMENT to regenerate the diff.")
                logger.error(f"[SM {run_id}] resume_from_stage_job: {_msg}")
                update_run_state(run_id, "FAILED", error=_msg)
                return _msg
            from agents.sdlc_pipeline import _transition, _inbox_notify, _teams_notify
            from core.config import sdlc_gate_deadline
            _gate = sm._approval_state()  # bug → AWAITING_SOLUTION_APPROVAL else AWAITING_CODE_APPROVAL
            _gate_kind = "solution" if _gate == "AWAITING_SOLUTION_APPROVAL" else "code"
            _deadline = sdlc_gate_deadline(_gate_kind)
            _transition(run_id, _gate, "hitl-gate")
            update_run_state(run_id, _gate, context_patch={"hitl_deadline": _deadline})
            try:
                _jira = run.get("jira_key", "")
                _inbox_notify(
                    run_id,
                    "solution_approval" if _gate == "AWAITING_SOLUTION_APPROVAL" else "design_approval",
                    f"[{_jira}] Diff ready for approval (REVIEW waived).", {"jira_key": _jira},
                )
                _teams_notify(run_id, hitl=True, stage=_gate,
                              summary=f"[{_jira}] Diff ready for approval (REVIEW waived).")
            except Exception as _ne:
                logger.warning(f"[SM {run_id}] waive gate notify failed: {_ne}")
            logger.info(f"[SM {run_id}] REVIEW waived → {_gate}")
            return f"resume_from_stage_job completed for {run_id} at {_gate}"
        if target_stage in ("COMMITTING", "TEST_VERIFY"):
            if not (_vd.get("edits")):
                _msg = (
                    "Cannot resume post-gate: no VERIFIED_DIFF artifact for this run. "
                    "Re-trigger the pipeline or resume at IMPLEMENT."
                )
                logger.error(f"[SM {run_id}] resume_from_stage_job: {_msg}")
                update_run_state(run_id, "FAILED", error=_msg)
                return _msg
            sm.mode = "postgate"
            sm.run()   # APPLYING → TEST_VERIFY → SLT_RUNNING → COMMITTING → MR
        elif target_stage in ("GOVERNANCE_SCAN", "GOVERNANCE_FIX", "GOVERNANCE_REVERIFY"):
            # Governance-aware resume (2026-07-24): a run suspended at the governance
            # end-gate re-runs GOVERNANCE, NOT implement. Falling through to
            # _phase_implement() here was the reported "retry goes back to plan" bug —
            # it discards the committed MR and re-plans. Instead rehydrate the
            # committed-MR coordinates and re-run _run_governance_endgate over the
            # committed diff (re-draft/un-draft handled inside the end-gate).
            sm.run_governance_review = True
            sm.governance_subset = ctx.get("governance_skills")
            _branch     = working_branch or run.get("branch", "") or ""
            _pr_number  = run.get("pr_number")
            _pr_url     = run.get("pr_url") or ""
            _commit_sha = ""
            try:
                _cm = sm._get_artifact("COMMITTING") or {}
                _branch     = _cm.get("branch") or _branch
                _pr_number  = _pr_number or _cm.get("pr_number")
                _pr_url     = _pr_url or _cm.get("mr_url") or ""
                _commit_sha = _cm.get("commit_sha") or ""
            except Exception:
                pass
            try:
                sm._ensure_run_workspace(repo)
            except Exception as _we:
                logger.error(f"[SM {run_id}] governance resume: workspace error → suspending: {_we}")
                try:
                    sm._suspend("GOVERNANCE_SCAN", f"Governance resume workspace error: {_we}")
                except Exception:
                    update_run_state(run_id, "SUSPENDED", current_stage="GOVERNANCE_SCAN",
                                     error=f"Governance resume workspace error: {_we}")
                return f"resume_from_stage_job governance workspace error for {run_id}"
            # Thread-local GitLab token so any un-draft/re-draft uses the triggering
            # user's PAT (never mutate GITLAB_TOKEN).
            try:
                from agents.sdlc_pipeline import _gov_resolve_gitlab_token
                from tools.gitlab_tools import set_token as _set_gl_token
                _tok = _gov_resolve_gitlab_token(user_id)
                if _tok:
                    _set_gl_token(_tok)
            except Exception:
                pass
            logger.info("[SDLC-GOV] governance resume — re-running end-gate (not implement)",
                        run_id=run_id, target_stage=target_stage)
            sm._run_governance_endgate(branch=_branch, pr_number=_pr_number,
                                       pr_url=_pr_url, commit_sha=_commit_sha)
        else:
            # IMPLEMENT / REVIEW (or any other SM stage) → pre-gate re-implement.
            sm.mode = "pregate"
            sm._phase_implement()

            # _finalize_pregate deliberately STOPS at REVIEW with a fresh VERIFIED_DIFF and
            # leaves the HITL gate transition to the caller (see its docstring). The NORMAL
            # path does this in sdlc_pipeline.py after _pregate_codegen (lines ~6050 / ~7062).
            # The resume path must do the SAME, or the run is stranded at REVIEW and never
            # reaches AWAITING_*_APPROVAL (the reported bug).
            _run2 = get_run(run_id) or {}
            if _run2.get("state", "") not in ("FAILED", "SUSPENDED", "CANCELLED", "MERGE_CONFLICT"):
                _vd2 = sm._get_artifact("VERIFIED_DIFF") or {}
                if _vd2.get("edits"):
                    import time as _time
                    from agents.sdlc_pipeline import _transition, _inbox_notify, _teams_notify
                    from core.config import sdlc_gate_deadline
                    _gate = sm._approval_state()  # bug → AWAITING_SOLUTION_APPROVAL else AWAITING_CODE_APPROVAL
                    _gate_kind = "solution" if _gate == "AWAITING_SOLUTION_APPROVAL" else "code"
                    _deadline = sdlc_gate_deadline(_gate_kind)
                    logger.info("[SM] gate entered", run_id=run_id, gate_kind=_gate_kind, hitl_deadline=_deadline)
                    _transition(run_id, _gate, "hitl-gate")
                    update_run_state(run_id, _gate, context_patch={"hitl_deadline": _deadline})
                    try:
                        _jira = run.get("jira_key", "")
                        _inbox_notify(
                            run_id,
                            "solution_approval" if _gate == "AWAITING_SOLUTION_APPROVAL" else "design_approval",
                            f"[{_jira}] Diff ready for approval after resume.", {"jira_key": _jira},
                        )
                        _teams_notify(run_id, hitl=True, stage=_gate,
                                      summary=f"[{_jira}] Diff ready for approval after resume.")
                    except Exception as _ne:
                        logger.warning(f"[SM {run_id}] resume gate notify failed: {_ne}")
                    logger.info(f"[SM {run_id}] resume pre-gate complete → {_gate}")

        return f"resume_from_stage_job completed for {run_id} at {target_stage}"
    except SDLCCancelled:
        logger.info(f"sdlc_worker: resume_from_stage_job {run_id} stopped — run cancelled")
        return f"resume_from_stage_job cancelled for {run_id}"
    except Exception as e:
        logger.error(f"sdlc_worker: resume_from_stage_job failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise


def retry_commit_job(payload: dict) -> str:
    """
    rq job: retry the COMMITTING phase for a run suspended at COMMIT_FAILED.

    The generated code is already durable in the CODING artifact, so this
    re-runs ONLY the commit (branch + atomic batch commit + MR) — no earlier
    stages. Idempotent: branch reuse, create↔update flips, MR _find_existing_mr.

    payload keys:
        run_id — SDLC run ID (required)
    """
    run_id = payload.get("run_id", "")
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_retry_commit")
    try:
        from store.sdlc_store import get_run, update_run_state, add_run_event
        run = get_run(run_id)
        if not run:
            logger.error(f"retry_commit_job: run {run_id} not found")
            return f"run {run_id} not found"

        state = run.get("state", "")
        # Terminal-state bail — never retry a finished run (mirrors sibling jobs).
        _TERMINAL = {"COMPLETE", "MERGED", "FAILED", "CANCELLED", "EXPIRED"}
        if state in _TERMINAL:
            logger.info(f"retry_commit_job: run {run_id} in terminal state {state!r} — skipping")
            return f"retry_commit skipped — run {run_id} is {state}"
        # Idempotency: only COMMIT_FAILED (or AWAITING_PR_APPROVAL where only MR
        # creation failed) are resumable here. Anything else is a no-op so a
        # double-click cannot replay commit on an in-flight run.
        if state not in ("COMMIT_FAILED", "AWAITING_PR_APPROVAL"):
            logger.info(
                f"retry_commit_job: run {run_id} in state {state!r} — not COMMIT_FAILED, skipping"
            )
            return f"retry_commit skipped — run {run_id} is {state}, expected COMMIT_FAILED"

        ctx      = run.get("context") or {}
        design   = ctx.get("design") or {}
        analysis = ctx.get("analysis") or {}
        repo     = run.get("repo") or ""
        language = (ctx.get("repo_ctx") or {}).get("language") if isinstance(ctx.get("repo_ctx"), dict) else ctx.get("language") or ""
        if not language:
            _rctx = ctx.get("repo_ctx") or {}
            for _v in _rctx.values():
                if isinstance(_v, dict) and _v.get("language"):
                    language = _v["language"]
                    break

        from agents.sdlc_state_machine import CodingStateMachine
        from agents.sdlc_pipeline import _resolve_gitlab_repo
        gitlab_repo    = _resolve_gitlab_repo(repo)
        base_branch    = ctx.get("base_branch", "")
        working_branch = ctx.get("working_branch", "") or run.get("branch", "")
        user_id        = ctx.get("user_id") or run.get("triggered_by") or ""
        user_email     = ctx.get("user_email", "")

        sm = CodingStateMachine(
            run_id=run_id,
            jira_key=run.get("jira_key", ""),
            repo=repo,
            language=language or "python",
            design=design,
            analysis=analysis,
            base_branch=base_branch,
            working_branch=working_branch,
            gitlab_repo=gitlab_repo,
            skip_tests=bool(ctx.get("skip_tests", False)),
            skip_slt=bool(ctx.get("skip_slt", False)),
            compile_skipped=bool(ctx.get("compile_skipped", False)),
            user_id=user_id,
            user_email=user_email,
        )
        # Governance (STEP 8) — see resume_from_stage_job above for the same
        # threading + defensive-bool-coercion rationale. COMMITTING is downstream
        # of GOVERNANCE_REVIEW in STAGE_DAG, so this retry path realistically never
        # re-enters the gate, but the attributes are threaded for consistency with
        # every other SM reconstruction site.
        _gov_raw = ctx.get("run_governance_review", False)
        sm.run_governance_review = _gov_raw if isinstance(_gov_raw, bool) else str(_gov_raw).strip().lower() in ("1", "true", "yes")
        sm.governance_subset = ctx.get("governance_skills")

        sm.resume_commit()
        return f"retry_commit_job completed for {run_id}"
    except SDLCCancelled:
        logger.info(f"sdlc_worker: retry_commit_job {run_id} stopped — run cancelled")
        return f"retry_commit_job cancelled for {run_id}"
    except Exception as e:
        logger.error(f"sdlc_worker: retry_commit_job failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                # Suspend-not-fail: keep the run resumable so a fresh retry-commit
                # can be issued once the underlying issue clears.
                update_run_state(run_id, "COMMIT_FAILED", error=f"retry_commit error: {e}")
            except Exception:
                pass
        raise


def resume_pr_approval_job(payload: dict) -> str:
    """rq job: post-PR-approval cleanup (mark COMPLETE, notify)."""
    run_id = payload.get("run_id", "")
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_pr_approval_resume")
    try:
        from agents.sdlc_pipeline import resume_after_pr_approval
        resume_after_pr_approval(run_id)
        return f"PR approval resume completed for {run_id}"
    except Exception as e:
        logger.error(f"sdlc_worker: resume_pr_approval_job failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise


def resume_feature_revision_job(payload: dict) -> str:
    """rq job: run a requested feature revision cycle."""
    run_id   = payload.get("run_id", "")
    feedback = payload.get("feedback", "")
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_feature_revision")
    try:
        from agents.sdlc_pipeline import run_feature_revision
        run_feature_revision(run_id, feedback)
        return f"feature revision completed for {run_id}"
    except Exception as e:
        logger.error(f"sdlc_worker: resume_feature_revision_job failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise


def resume_bug_revision_job(payload: dict) -> str:
    """rq job: run a requested bug revision cycle."""
    run_id   = payload.get("run_id", "")
    feedback = payload.get("feedback", "")
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_bug_revision")
    try:
        from agents.sdlc_pipeline import run_bug_revision
        run_bug_revision(run_id, feedback)
        return f"bug revision completed for {run_id}"
    except Exception as e:
        logger.error(f"sdlc_worker: resume_bug_revision_job failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise


# ─────────────────────────────────────────────────────────────
# Governance (STEP 9, 2026-07-17) — standalone review job
# ─────────────────────────────────────────────────────────────

def _gov_bool(raw, default: bool = False) -> bool:
    """Defensive bool coercion — mirrors ctx.get('skip_tests', ...) idiom used
    throughout agents/sdlc_pipeline.py (a JSONB round-trip can hand back a
    "true"/"false" string instead of a real bool)."""
    if raw is None:
        return default
    return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes")


def _gov_resolve_and_set_gitlab_token(payload: dict) -> None:
    """Resolve + set the per-thread GitLab token for a standalone governance job
    (repo/MR mode — no run_id, so nothing has called set_token() yet). Mirrors
    the run_id-mode pipeline's per-user token resolution (core.platform_credentials);
    falls back to leaving the thread-local unset, in which case
    tools.gitlab_tools._resolve_token() falls back to the GITLAB_TOKEN env var —
    same default every other GitLab caller in this codebase uses."""
    try:
        from core.platform_credentials import get_gitlab_token
        from tools.gitlab_tools import set_token
        user_id = payload.get("triggered_by_user_id") or ""
        email   = payload.get("triggered_by_email") or ""
        if user_id or email:
            set_token(get_gitlab_token(user_id=user_id, email=email))
    except PermissionError:
        logger.info("[SDLC-GOV] no user GitLab token on file — falling back to GITLAB_TOKEN env")
    except Exception as _te:
        logger.warning(f"[SDLC-GOV] gitlab token resolution failed (falling back to env): {_te}")


def _gov_clone_workspace(repo: str, ref: str, dedup_key: str) -> str:
    """Materialize a throwaway workspace for standalone (repo/MR) governance
    review/fix, reusing the same per-run clone machinery the pipeline uses
    (workers.workspace_sync_worker.prepare_run_workspace) so build tooling /
    .git behave identically. No repo_index_status row is required here (unlike
    CodingStateMachine._ensure_run_workspace) — the clone URL is resolved
    directly from the GitLab project API."""
    from tools.gitlab_tools import gitlab_get_project_clone_url, _resolve_token
    from core.platform_credentials import inject_gitlab_token
    from workers.workspace_sync_worker import prepare_run_workspace
    bare_url = gitlab_get_project_clone_url(repo)
    if not bare_url:
        raise RuntimeError(f"could not resolve clone URL for repo {repo!r}")
    tok = _resolve_token()
    clone_url = inject_gitlab_token(bare_url, tok) if tok else bare_url
    repo_slug = (repo or "unknown").replace("/", "_").lower()
    return prepare_run_workspace(dedup_key, repo_slug, clone_url, ref or "main")


def _gov_diff_against_base(workspace: str, base: str) -> tuple:
    """git diff the current checkout against origin/<base> (fetched --depth=50
    on demand — the workspace clone only carries the checked-out branch).
    Returns (diff_text, changed_files); best-effort, never raises."""
    from workers.workspace_sync_worker import _run as _grun
    base = (base or "main").strip()
    try:
        _grun(["git", "-C", workspace, "fetch", "--depth=50", "origin", base], check=False)
        d = _grun(["git", "-C", workspace, "diff", f"origin/{base}...HEAD"], check=False)
        diff_text = (d.stdout or "") if d.returncode == 0 else ""
        f = _grun(["git", "-C", workspace, "diff", "--name-only", f"origin/{base}...HEAD"], check=False)
        changed_files = [l.strip() for l in (f.stdout or "").splitlines() if l.strip()] if f.returncode == 0 else []
        return diff_text, changed_files
    except Exception as _de:
        logger.warning(f"[SDLC-GOV] _gov_diff_against_base failed: {_de}")
        return "", []


def _gov_push_fix(workspace: str, ref: str) -> bool:
    """Commit + push a governance fixer round's changes to the source branch.
    Best-effort — a push failure here must never fail the whole job (the report
    has already been generated/persisted regardless). Returns True on push."""
    from workers.workspace_sync_worker import _run as _grun
    try:
        _grun(["git", "-C", workspace, "add", "-A"], check=False)
        _diff_check = _grun(["git", "-C", workspace, "diff", "--cached", "--quiet"], check=False)
        if _diff_check.returncode == 0:
            return False  # nothing to commit
        _grun(["git", "-C", workspace, "commit", "-m", "AiNxt governance auto-fix"], check=False)
        _p = _grun(["git", "-C", workspace, "push", "origin", f"HEAD:{ref}"], check=False)
        return _p.returncode == 0
    except Exception as _pe:
        logger.warning(f"[SDLC-GOV] _gov_push_fix failed: {_pe}")
        return False


def run_governance_review_job(payload: dict) -> str:
    """
    rq job: standalone governance review (report-first; optional bounded
    auto-fix). Two mutually exclusive modes, validated by the router:

      run_id mode : diff read from the run's latest VERIFIED_DIFF artifact
                    (rebuilt as a unified diff via difflib, mirroring
                    CodingStateMachine._build_unified_diff), reviewed against
                    the run's live workspace when one is still on disk. auto_fix
                    is unavailable without a live workspace (nowhere to push) —
                    conservative choice: silently downgrades to report-only,
                    logged loudly below.
      repo mode    : diff read via a GitLab MR (mr_iid, gitlab_get_mr_diff) or a
                    fresh clone + `git diff` against the repo default branch
                    (ref/branch). A clone is made for auto_fix regardless of
                    which diff source is used (MR-mode review itself doesn't
                    need one).

    Report-first: NEVER forces a pipeline suspend (that gate lives entirely in
    CodingStateMachine._run_governance_review, untouched by this job). When
    auto_fix is requested and the review is blocking, runs the SAME fixer-loop
    shape as the SM's own governance gate (code-profile CLI + loaded plugins),
    re-reviewing up to config.max_iters() times, then best-effort pushes the
    fix + posts/updates the MR note. Always persists a GOVERNANCE_REPORT
    artifact (run_id mode) and a standalone report file under
    tempfile.gettempdir()/sdlc_governance_reports/<key>/. Never raises.
    """
    run_id            = (payload.get("run_id") or "").strip()
    repo              = (payload.get("repo") or "").strip()
    ref               = (payload.get("ref") or payload.get("branch") or "").strip()
    mr_iid            = payload.get("mr_iid")
    auto_fix          = _gov_bool(payload.get("auto_fix"), default=True)
    governance_skills = payload.get("governance_skills")
    product_id_in     = payload.get("product_id")

    mode = "run_id" if run_id else "repo"
    dedup_key = run_id or f"govstandalone-{(repo or 'unknown').replace('/', '_')}-{mr_iid or ref or 'norefs'}"
    bind_context(correlation_id=run_id or dedup_key, pipeline_stage="sdlc_governance_standalone")
    logger.info(
        "[SDLC-GOV] run_governance_review_job start",
        mode=mode, repo=repo, ref=ref, mr_iid=mr_iid, auto_fix=auto_fix,
    )

    from agents.sdlc_governance import config as gov_config, engine as gov_engine
    from agents.sdlc_pipeline import run_governance_scan_snapshot
    from agents.sdlc_cli_engine import run_cli, CliEngineConfig
    from core.model_registry import cli_model_for
    from store.sdlc_artifacts import _store_artifact, compute_input_hash

    workspace: str = ""
    diff_text: str = ""
    changed_files: list = []
    resolved_repo = repo
    base_ref = ""   # target/default branch a repo-mode diff/push is measured against
    triggered_by = payload.get("triggered_by_user_id") or payload.get("triggered_by_email") or "governance-standalone"

    db = None
    try:
        from db.database import SessionLocal
        db = SessionLocal()
    except Exception as _dbe:
        logger.warning(f"[SDLC-GOV] run_governance_review_job: no DB session — {_dbe}")

    try:
        if run_id:
            from store.sdlc_store import get_run
            run = get_run(run_id)
            if not run:
                logger.error(f"[SDLC-GOV] run_governance_review_job: run {run_id} not found")
                return f"run {run_id} not found"
            resolved_repo = run.get("repo") or resolved_repo
            ctx = run.get("context") or {}
            base_ref = ctx.get("base_branch") or ""  # used to re-diff after a fixer round below

            from store.sdlc_artifacts import _load_latest_artifact
            vd = _load_latest_artifact(run_id, "VERIFIED_DIFF") or {}
            edits = ((vd.get("payload") or {}).get("edits")) or []
            if edits:
                import difflib
                chunks = []
                for e in edits:
                    path = e.get("path") or ""
                    base_lines = (e.get("base_body") or "").splitlines()
                    new_lines  = (e.get("new_body") or "").splitlines()
                    ud = difflib.unified_diff(base_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="")
                    t = "\n".join(ud)
                    if t.strip():
                        chunks.append(t)
                    if path:
                        changed_files.append(path)
                diff_text = "\n".join(chunks)

            # Reuse the run's live workspace only if it is still on local disk
            # (same instance, run not yet cleaned up). No cross-instance fetch —
            # this is a best-effort convenience, not a correctness dependency.
            _ws = ctx.get("_run_workspace_path") or ""
            if _ws and os.path.isdir(_ws):
                workspace = _ws
            if auto_fix and not workspace:
                logger.warning(
                    "[SDLC-GOV] run_id mode: no live workspace on disk for this run — "
                    "auto_fix downgraded to report-only (nowhere to apply/push a fix)"
                )
                auto_fix = False
        else:
            if not repo:
                logger.error("[SDLC-GOV] run_governance_review_job: neither run_id nor repo provided")
                return "neither run_id nor repo provided"
            _gov_resolve_and_set_gitlab_token(payload)
            from tools.gitlab_tools import gitlab_get_mr_diff, _detect_default_branch

            if mr_iid:
                diff_text, changed_files, source_branch, target_branch = gitlab_get_mr_diff(repo, int(mr_iid))
                ref = ref or source_branch
                base_ref = target_branch or _detect_default_branch(repo)
            else:
                base_ref = _detect_default_branch(repo)

            if auto_fix:
                # A clone is needed for the fixer loop regardless of diff source
                # (MR-mode review doesn't need one; branch-mode review does).
                try:
                    workspace = _gov_clone_workspace(repo, ref, dedup_key)
                    if not diff_text:
                        diff_text, changed_files = _gov_diff_against_base(workspace, base_ref)
                except Exception as _ce:
                    logger.warning(
                        f"[SDLC-GOV] repo-mode clone failed — auto_fix downgraded to report-only: {_ce}"
                    )
                    auto_fix = False
            elif not diff_text:
                # Branch mode without auto_fix still needs a diff — clone read-only.
                try:
                    workspace = _gov_clone_workspace(repo, ref, dedup_key)
                    diff_text, changed_files = _gov_diff_against_base(workspace, base_ref)
                except Exception as _ce:
                    logger.error(f"[SDLC-GOV] repo-mode clone failed (report-only, no diff): {_ce}")

        product_id = product_id_in or gov_engine.resolve_product_id(db, resolved_repo)
        subset = gov_config.parse_subset(governance_skills)

        result = run_governance_scan_snapshot(
            run_id or dedup_key, workspace=workspace, diff_text=diff_text,
            changed_files=changed_files, product_id=product_id, repo=resolved_repo,
            base_sha=(base_ref or "HEAD"), subset=subset, db=db, trigger="initial",
            created_by=triggered_by,
        )
        iterations = 1
        max_iters = gov_config.max_iters()

        # Bounded fixer loop — same shape as CodingStateMachine._run_governance_review's
        # own-fixer, but capped by gov_config.review_turns() instead of the per-run HOD
        # budget helpers in agents/sdlc_cli_budget (those key off a real sdlc_runs row;
        # a standalone repo/MR-mode job may have none — conservative choice, flagged here).
        if auto_fix and workspace and not result.get("skipped") and result.get("blocking"):
            while result.get("blocking") and iterations < max_iters:
                fix_res = run_cli(
                    config=CliEngineConfig.from_env(),
                    workspace_root=workspace,
                    prompt=gov_engine.build_fix_prompt(result.get("open_findings") or [], workspace),
                    profile="code",
                    model=cli_model_for("coder"),
                    max_turns=gov_config.review_turns(),
                    run_id=run_id or dedup_key,
                )
                logger.info(
                    "[SDLC-GOV] standalone fixer round", run_id=run_id or dedup_key,
                    iteration=iterations, status=getattr(fix_res, "status", ""),
                )
                if getattr(fix_res, "status", "") == "suspended":
                    logger.warning(
                        "[SDLC-GOV] standalone fixer suspended — stopping loop",
                        run_id=run_id or dedup_key, iteration=iterations,
                        reason=getattr(fix_res, "reason", ""),
                    )
                    break
                # Re-derive the diff from the workspace via git (not the stale
                # VERIFIED_DIFF-derived diff_text) so the re-review sees the
                # fixer's actual changes. Valid for both modes here — the loop
                # only runs when `workspace` is a real git checkout.
                diff_text, changed_files = _gov_diff_against_base(workspace, base_ref)
                result = run_governance_scan_snapshot(
                    run_id or dedup_key, workspace=workspace, diff_text=diff_text,
                    changed_files=changed_files, product_id=product_id, repo=resolved_repo,
                    base_sha=(base_ref or "HEAD"), subset=subset, db=db, trigger="rescan",
                    created_by=triggered_by,
                )
                iterations += 1

        report = result.get("report") or gov_engine.render_report(
            structured={}, findings=[], ref="", skills=[], iterations=iterations,
        )
        report["iterations"] = iterations

        if run_id:
            try:
                _store_artifact(
                    run_id=run_id,
                    stage="GOVERNANCE_REPORT",
                    payload=report,
                    producer="governance-standalone",
                    # Mirrors CodingStateMachine._put_artifact's own convention
                    # (same stage passed to both _store_artifact and
                    # compute_input_hash) rather than hard-coding "GOVERNANCE_REVIEW" —
                    # keeps this job's persistence identical to the SM's.
                    input_hash=compute_input_hash(run_id, "GOVERNANCE_REPORT"),
                    created_by=triggered_by,
                    reason="standalone governance review",
                )
            except Exception as _se:
                logger.warning(f"[SDLC-GOV] GOVERNANCE_REPORT persist failed: {_se}")

        # Standalone report file — OUTSIDE any repo tree (never lands in a diff).
        try:
            import tempfile as _tf
            report_dir = os.path.join(_tf.gettempdir(), "sdlc_governance_reports", dedup_key)
            os.makedirs(report_dir, exist_ok=True)
            with open(os.path.join(report_dir, "governance_report.md"), "w", encoding="utf-8") as f:
                f.write(report.get("report_md") or "")
        except Exception as _fe:
            logger.warning(f"[SDLC-GOV] standalone report file write failed: {_fe}")

        # Best-effort push (repo mode only — run_id mode never mutates the run's
        # workspace here; APPLYING/COMMITTING own that) + MR note.
        if not run_id and workspace and auto_fix and iterations > 1:
            try:
                _gov_push_fix(workspace, ref or base_ref)
            except Exception as _pe:
                logger.warning(f"[SDLC-GOV] governance fix push failed (best-effort): {_pe}")

        if mr_iid and repo:
            try:
                from tools.gitlab_tools import gitlab_post_governance_note
                gitlab_post_governance_note(repo, int(mr_iid), report.get("report_md") or "")
            except Exception as _ne:
                logger.warning(f"[SDLC-GOV] MR note post failed (best-effort): {_ne}")

        verdict = report.get("overall_verdict", "PASS")
        logger.info(
            "[SDLC-GOV] run_governance_review_job end",
            mode=mode, repo=resolved_repo, auto_fix=auto_fix, verdict=verdict, iterations=iterations,
        )
        return f"governance review completed mode={mode} verdict={verdict}"
    except Exception as e:
        logger.error(f"[SDLC-GOV] run_governance_review_job failed → {e}")
        return f"governance review failed: {e}"
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def governance_evidence_job(payload: dict) -> str:
    """
    SUPERSEDED (2026-08-06): the governance approval/finalize paths now call the
    evidence functions synchronously in-process (post_domain_decision /
    post_final_attestation) because the doc_queue (Q_DOC) worker this job ran on
    is not deployed on this host. Kept for compatibility; no longer enqueued.

    rq job: post one governance evidence event (a per-domain decision or the
    final attestation) to the run's linked Jira Change ticket. Fire-and-forget
    from the governance approval/finalize paths — the dedup ledger in
    agents/sdlc_governance_change_ticket.py (post_domain_decision /
    post_final_attestation) makes re-delivery safe, so this job never raises;
    any failure is logged and swallowed (RQ retry may still re-run it, which
    is fine because of the dedup claim).
    """
    run_id = payload.get("run_id")
    event = payload.get("event")
    try:
        if not run_id:
            logger.error("[GOV-EVIDENCE] job: missing run_id", payload=payload)
            return "no run_id"

        from store.sdlc_store import get_run
        run = get_run(run_id)
        if not run:
            logger.error("[GOV-EVIDENCE] job: run not found", run_id=run_id)
            return f"run {run_id} not found"

        bind_context(correlation_id=run_id, pipeline_stage="sdlc_governance_evidence")

        ctx = run.get("context") or {}
        user_id = payload.get("user_id") or run.get("triggered_by") or ctx.get("user_id") or ""
        user_email = payload.get("user_email") or ""

        logger.info(
            "[GOV-EVIDENCE] job entry", run_id=run_id, job_event=event, domain=payload.get("domain"),
        )

        from agents.sdlc_governance_change_ticket import post_domain_decision, post_final_attestation

        if event == "domain_decision":
            post_domain_decision(
                run,
                payload.get("domain") or "",
                payload.get("status") or "",
                payload.get("decided_by") or "",
                payload.get("snapshot_id") or "",
                user_id=user_id,
                user_email=user_email,
            )
        elif event == "final":
            post_final_attestation(
                run,
                payload.get("snapshot_id") or "",
                user_id=user_id,
                user_email=user_email,
            )
        else:
            logger.warning("[GOV-EVIDENCE] job: unknown event", run_id=run_id, job_event=event)

        return f"governance_evidence_job done run={run_id} event={event}"
    except Exception as e:
        logger.error(
            "[GOV-EVIDENCE] job body failed", run_id=payload.get("run_id"), job_event=payload.get("event"),
            error=str(e),
        )
        return f"governance_evidence_job failed: {e}"


def expire_stale_hitl_runs() -> int:
    """
    Active HITL watchdog — scans all AWAITING_* runs and marks those past their
    hitl_deadline as EXPIRED. Callable by the RQ scheduler every 15 minutes.
    Returns the number of runs expired.
    """
    bind_context(correlation_id="hitl_watchdog", pipeline_stage="sdlc_hitl_watchdog")
    import time
    from store.sdlc_store import list_runs, update_run_state

    # Every human-gate / suspended state gets an EXPIRED path. Dual-read the
    # renamed code-approval state. AWAITING_RE_REVIEW/SUSPENDED aren't included
    # here because they lack a hitl_deadline (they resume via their own flows).
    AWAITING_STATES = {
        "AWAITING_CODE_APPROVAL",       # renamed successor
        "AWAITING_DESIGN_APPROVAL",     # legacy alias — dual-read
        "AWAITING_SOLUTION_APPROVAL",
        "AWAITING_PR_APPROVAL",
        "AWAITING_USER_INPUT",
        "AWAITING_GOVERNANCE_APPROVAL",
    }
    now = time.time()
    expired = 0
    try:
        from core.job_queue import refresh_sdlc_slot
    except Exception:
        refresh_sdlc_slot = None
    try:
        runs = list_runs(limit=200)
        for run in runs:
            state = run.get("state")
            if state not in AWAITING_STATES:
                continue
            ctx      = run.get("context") or {}
            deadline = ctx.get("hitl_deadline")
            if deadline and now > deadline:
                # Past deadline → expire. Message references the configured window.
                update_run_state(
                    run["id"], "EXPIRED",
                    error=(
                        f"Approval window expired. State was {state}. "
                        f"Re-trigger the pipeline."
                    ),
                )
                expired += 1
                logger.info(
                    "sdlc_worker: expire_stale_hitl_runs expired run",
                    run_id=run["id"], state=state, deadline=deadline,
                )
            else:
                # Still legitimately waiting → renew the Redis dedup/rate-limit
                # lease so a multi-day gate never loses dedup. Pure EXPIRE.
                if refresh_sdlc_slot is not None:
                    jira_key = run.get("jira_key", "")
                    reporter = (
                        ctx.get("assignee") or ctx.get("reporter")
                        or ctx.get("triggered_by_email") or ctx.get("user_email")
                        or ctx.get("triggered_by_user_id") or ctx.get("user_id")
                        or run.get("triggered_by") or "unknown"
                    )
                    try:
                        refresh_sdlc_slot(jira_key, reporter=reporter)
                        logger.info(
                            "sdlc_worker: expire_stale_hitl_runs renewed slot",
                            run_id=run["id"], jira_key=jira_key, state=state,
                            ttl="SDLC_ACTIVE_TTL_SECS",
                        )
                    except Exception as _re:
                        logger.warning(
                            f"sdlc_worker: slot renewal failed for {run['id']}: {_re}"
                        )
    except Exception as _e:
        logger.error(f"sdlc_worker: expire_stale_hitl_runs failed: {_e}")
    return expired


# ─────────────────────────────────────────────────────────────
# Governance pipeline jobs (Step 7, 2026-07-21)
# ─────────────────────────────────────────────────────────────

def run_governance_pipeline_job(issue_dict: dict) -> str:
    """rq job: run standalone governance scan pipeline."""
    run_id = issue_dict.get("_run_id") or issue_dict.get("run_id")
    bind_context(correlation_id=run_id or "", pipeline_stage="sdlc_governance_scan")
    try:
        from agents.sdlc_pipeline import run_governance_pipeline
        run_governance_pipeline(issue_dict, run_id)
        return f"governance pipeline completed for run {run_id}"
    except SDLCCancelled:
        logger.info(f"sdlc_worker: governance pipeline cancelled for run {run_id}")
        return f"governance pipeline cancelled for run {run_id}"
    except Exception as e:
        logger.error(f"sdlc_worker: governance pipeline failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise


def resume_governance_fix_job(payload: dict) -> str:
    """rq job: resume governance fix after all domains approved."""
    run_id = payload.get("run_id", "")
    actor  = payload.get("actor", "user")
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_governance_fix_resume")
    try:
        from agents.sdlc_pipeline import resume_governance_fix
        resume_governance_fix(run_id, actor)
        return f"governance fix resume completed for {run_id}"
    except SDLCCancelled:
        logger.info(f"sdlc_worker: governance fix cancelled for {run_id}")
        return f"governance fix cancelled for {run_id}"
    except Exception as e:
        logger.error(f"sdlc_worker: resume_governance_fix_job failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise


def trigger_domain_fix_job(payload: dict) -> str:
    """rq job: run auto-fixer for one governance domain after author requests it."""
    run_id           = payload.get("run_id", "")
    domain           = payload.get("domain", "")
    actor            = payload.get("actor", "user")
    fix_instructions = payload.get("fix_instructions", "")
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_governance_domain_fix")
    try:
        from agents.sdlc_pipeline import trigger_domain_fix
        trigger_domain_fix(run_id, domain, actor, fix_instructions)
        return f"domain fix completed for {run_id}/{domain}"
    except SDLCCancelled:
        logger.info(f"sdlc_worker: domain fix cancelled for {run_id}/{domain}")
        return f"domain fix cancelled for {run_id}/{domain}"
    except Exception as e:
        logger.error(f"sdlc_worker: trigger_domain_fix_job failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise


def governance_author_fix_job(payload: dict) -> str:
    """rq job: run the bounded author remediation loop (auto-fix + auto re-scan +
    convergence) for one finding the author asked to fix (B2.2).

    Enqueued by routers.sdlc_router.author_request_fix via enqueue_hitl_resume_job
    (a run continuation — bypasses the per-reporter admission counter). The run
    stays at AWAITING_GOVERNANCE_APPROVAL; this job holds a worker slot only while
    actively fixing, then returns (no slot held during subsequent human triage)."""
    run_id      = payload.get("run_id", "")
    fingerprint = payload.get("fingerprint", "")
    actor       = payload.get("actor", "user")
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_governance_author_fix")
    try:
        from agents.sdlc_pipeline import run_governance_author_fix
        run_governance_author_fix(run_id, fingerprint, actor)
        return f"author fix completed for {run_id}/{fingerprint[:12]}"
    except SDLCCancelled:
        logger.info(f"sdlc_worker: author fix cancelled for {run_id}")
        # B2.6: run abandoned mid-governance while a draft MR + committed branch
        # exist → close the orphan draft MR (best-effort; never re-raises).
        try:
            from agents.sdlc_pipeline import cleanup_abandoned_governance_mr
            cleanup_abandoned_governance_mr(run_id, actor=actor)
        except Exception:
            pass
        return f"author fix cancelled for {run_id}"
    except Exception as e:
        logger.error(f"sdlc_worker: governance_author_fix_job failed → {e}")
        # Fail-safe: never leave the run FAILED for an author-loop error — the loop
        # itself re-suspends to AWAITING_GOVERNANCE_APPROVAL. Only log here.
        raise


def governance_batch_fix_job(payload: dict) -> str:
    """rq job: run ONE bounded fixer session over a BATCH of findings the author
    explicitly asked to fix (Fix selected / Fix all requested).

    Enqueued by routers.sdlc_router.author_run_fixes via enqueue_hitl_resume_job (a
    run continuation — bypasses the per-reporter admission counter). The run stays at
    AWAITING_GOVERNANCE_APPROVAL; the job holds a worker slot only while actively
    fixing, then returns. `fingerprints` is the explicit target list (already resolved
    to the fix_requested set by the endpoint)."""
    run_id       = payload.get("run_id", "")
    fingerprints = payload.get("fingerprints", []) or []
    actor        = payload.get("actor", "user")
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_governance_batch_fix")
    try:
        from agents.sdlc_pipeline import run_governance_batch_fix
        run_governance_batch_fix(run_id, fingerprints, actor)
        return f"batch fix completed for {run_id} ({len(fingerprints)} finding(s))"
    except SDLCCancelled:
        logger.info(f"sdlc_worker: batch fix cancelled for {run_id}")
        # Run abandoned mid-governance while a draft MR + committed branch exist →
        # close the orphan draft MR (best-effort; never re-raises).
        try:
            from agents.sdlc_pipeline import cleanup_abandoned_governance_mr
            cleanup_abandoned_governance_mr(run_id, actor=actor)
        except Exception:
            pass
        return f"batch fix cancelled for {run_id}"
    except Exception as e:
        logger.error(f"sdlc_worker: governance_batch_fix_job failed → {e}")
        # Fail-safe: run_governance_batch_fix re-suspends + clears the rescanning flag
        # itself; only log here.
        raise


def resume_in_pipeline_governance_job(payload: dict) -> str:
    """rq job: resume a feature/bug run from AWAITING_GOVERNANCE_APPROVAL.

    Called when all governance domains are approved for a feature/bug run
    (not a standalone governance run). Re-enters the CodingStateMachine at
    the governance-approval resume point and continues to APPLYING.
    """
    run_id = payload.get("run_id", "")
    actor  = payload.get("actor", "user")
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_governance_in_pipeline_resume")
    try:
        from agents.sdlc_pipeline import resume_in_pipeline_governance_approval
        resume_in_pipeline_governance_approval(run_id, actor)
        return f"in-pipeline governance resume completed for {run_id}"
    except SDLCCancelled:
        logger.info(f"sdlc_worker: in-pipeline governance resume cancelled for {run_id}")
        # B2.6: run abandoned mid-governance while a draft MR + committed branch
        # exist → close the orphan draft MR (best-effort; never re-raises).
        try:
            from agents.sdlc_pipeline import cleanup_abandoned_governance_mr
            cleanup_abandoned_governance_mr(run_id, actor=actor)
        except Exception:
            pass
        return f"in-pipeline governance resume cancelled for {run_id}"
    except Exception as e:
        logger.error(f"sdlc_worker: resume_in_pipeline_governance_job failed → {e}")
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "FAILED", error=str(e))
            except Exception:
                pass
        raise


def _push_local_workspace_to_origin(run_id: str, repo: str, branch: str) -> tuple:
    """Best-effort: ensure the run's LOCAL workspace commits/edits are on
    origin/<branch> BEFORE the governance end-gate re-clones fresh (2026-07-30).

    The end-gate scans a FRESH CLONE of origin — so any change that lives only in
    the run's local workspace (unpushed) yields an EMPTY diff → empty .patch files →
    false-green. This stages + commits any pending working-tree edits and pushes
    HEAD:<branch> so the fresh clone (and the MR) actually carry the change. The
    workspace's `origin` remote was cloned with the triggering user's embedded PAT
    (build_run_clone_url), so the push authenticates without mutating GITLAB_TOKEN.

    Non-fatal: if there is no local workspace, nothing to push, or the push fails,
    the downstream empty-diff guard SUSPENDs with an actionable message. Returns
    (pushed: bool, detail: str). Never raises."""
    import subprocess
    from core import config as _cfg
    try:
        from agents.sdlc_context import normalize_repo_index_key_without_prefix as _nrik
        slug = _nrik(repo) or repo
    except Exception:
        slug = repo
    ws = os.path.join(_cfg.BUILDER_WORKSPACE_ROOT, "runs", f"{run_id}_{slug}")
    if not os.path.isdir(os.path.join(ws, ".git")):
        return (False, f"no local workspace at {ws}")
    if not branch:
        return (False, "no working branch to push")

    def _g(args, timeout=180):
        try:
            return subprocess.run(["git", "-C", ws] + args,
                                  capture_output=True, text=True, timeout=timeout)
        except Exception as _e:
            logger.warning("[SDLC-GOV] pre-scan push git op failed",
                           run_id=run_id, args=args, error=str(_e))
            return None

    # Commit any uncommitted working-tree edits so the push carries them.
    _g(["add", "-A"])
    _st = _g(["diff", "--cached", "--quiet"])
    if _st is not None and _st.returncode == 1:   # 1 = staged changes present
        _g(["commit", "-m", "chore(sdlc): sync governance pre-scan changes"])

    r = _g(["push", "origin", f"HEAD:{branch}"])
    if r is not None and r.returncode == 0:
        logger.info("[SDLC-GOV] pre-scan push to origin OK",
                    run_id=run_id, branch=branch, workspace=ws)
        return (True, "pushed")
    _detail = ((r.stderr or r.stdout) if r is not None else "push failed") or "push failed"
    logger.warning("[SDLC-GOV] pre-scan push to origin did not succeed (non-fatal)",
                   run_id=run_id, branch=branch, detail=_detail[-300:])
    return (False, _detail[-300:])


def run_endgate_governance_job(payload: dict) -> str:
    """rq job: AUTHOR-TRIGGERED governance end-gate (2026-07-24).

    Governance is decoupled from commit — a normal (non-draft) MR is opened at
    COMMIT and the run sits at AWAITING_PR_APPROVAL. The author triggers governance
    here (POST /sdlc/runs/{id}/governance/start). This job rehydrates the
    CodingStateMachine for the existing run, materializes the run workspace at the
    committed working branch, re-drafts the MR for the gate duration, then runs
    _run_governance_endgate over the committed diff. On nothing-blocking the
    end-gate un-drafts the MR + advances to AWAITING_PR_APPROVAL; on blocking
    findings it suspends to AWAITING_GOVERNANCE_APPROVAL (author triage / team
    review gate). _run_governance_endgate never raises — it fails CLOSED (suspend).

    payload keys: run_id (required), actor (optional).
    """
    run_id = payload.get("run_id", "")
    actor  = payload.get("actor", "user")
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_governance_endgate_start")
    try:
        from store.sdlc_store import get_run, update_run_state
        run = get_run(run_id)
        if not run:
            logger.error(f"run_endgate_governance_job: run {run_id} not found")
            return f"run {run_id} not found"

        ctx      = run.get("context") or {}
        design   = ctx.get("design") or {}
        analysis = ctx.get("analysis") or {}
        repo     = run.get("repo") or ""
        language = (ctx.get("repo_ctx") or {}).get("language") if isinstance(ctx.get("repo_ctx"), dict) else ctx.get("language") or ""
        if not language:
            _rctx = ctx.get("repo_ctx") or {}
            for _v in _rctx.values():
                if isinstance(_v, dict) and _v.get("language"):
                    language = _v["language"]
                    break

        from agents.sdlc_state_machine import CodingStateMachine
        from agents.sdlc_pipeline import _resolve_gitlab_repo
        gitlab_repo    = _resolve_gitlab_repo(repo)
        base_branch    = ctx.get("base_branch", "")
        working_branch = ctx.get("working_branch", "") or run.get("branch", "")
        user_id        = ctx.get("user_id") or run.get("triggered_by") or ""
        user_email     = ctx.get("user_email", "")

        sm = CodingStateMachine(
            run_id=run_id,
            jira_key=run.get("jira_key", ""),
            repo=repo,
            language=language or "python",
            design=design,
            analysis=analysis,
            base_branch=base_branch,
            working_branch=working_branch,
            gitlab_repo=gitlab_repo,
            skip_tests=bool(ctx.get("skip_tests", False)),
            skip_slt=bool(ctx.get("skip_slt", False)),
            compile_skipped=bool(ctx.get("compile_skipped", False)),
            user_id=user_id,
            user_email=user_email,
        )
        # Governance is being EXPLICITLY triggered → enable + thread the subset
        # (attributes, not ctor kwargs — same contract as resume_from_stage_job).
        sm.run_governance_review = True
        sm.governance_subset = ctx.get("governance_skills")

        # Rehydrate the committed-MR coordinates from the run row + COMMITTING artifact.
        branch     = working_branch or ""
        pr_number  = run.get("pr_number")
        pr_url     = run.get("pr_url") or ""
        commit_sha = ""
        try:
            _cm = sm._get_artifact("COMMITTING") or {}
            branch     = _cm.get("branch") or branch
            pr_number  = pr_number or _cm.get("pr_number")
            pr_url     = pr_url or _cm.get("mr_url") or ""
            commit_sha = _cm.get("commit_sha") or ""
        except Exception:
            pass

        if not pr_number:
            _msg = "run_endgate_governance_job: no MR/pr_number — cannot start governance"
            logger.error(_msg, run_id=run_id)
            update_run_state(run_id, run.get("state") or "AWAITING_PR_APPROVAL",
                             error="Cannot start governance: no MR exists for this run.")
            return _msg

        # Fix 2c (2026-07-30): _ensure_run_workspace clones sm.working_branch — the
        # ctor value read from ctx.working_branch|run.branch. But the AUTHORITATIVE
        # working branch may only be on the COMMITTING artifact / the MR itself. If they
        # disagree (or the ctor value was empty), the clone fetches the WRONG branch —
        # typically the base branch — so HEAD == base → merge-base diff is EMPTY (the
        # reported empty-.patch symptom). Reconcile the resolved `branch` onto the SM and
        # validate it BEFORE cloning; last-resort read the MR's source branch from GitLab.
        if not branch:
            try:
                from tools.gitlab_tools import gitlab_get_mr_diff
                _mr_tuple = gitlab_get_mr_diff(gitlab_repo, pr_number)
                if isinstance(_mr_tuple, tuple) and len(_mr_tuple) >= 3:
                    branch = _mr_tuple[2] or ""
            except Exception as _mbe:
                logger.warning("[SDLC-GOV] endgate: MR source-branch lookup failed (non-fatal)",
                               run_id=run_id, error=str(_mbe))
        if not branch or (base_branch and branch == base_branch):
            _msg = (
                f"Cannot start governance: could not resolve the run's working branch "
                f"(resolved={branch!r}, base={base_branch!r}). The scan would clone the "
                "base branch and see an empty diff. Retry once the MR/commit stage has "
                "recorded the source branch."
            )
            logger.error("[SDLC-GOV] endgate: working-branch resolution failed — suspending",
                         run_id=run_id, resolved_branch=branch, base_branch=base_branch)
            try:
                sm._suspend("GOVERNANCE_SCAN", _msg)
            except Exception:
                update_run_state(run_id, "SUSPENDED", current_stage="GOVERNANCE_SCAN",
                                 error=_msg)
            return _msg
        # Pin the reconciled branch onto the SM so the clone + base-vs-working diff use IT.
        if sm.working_branch != branch:
            logger.info("[SDLC-GOV] endgate: reconciled working_branch before clone",
                        run_id=run_id, was=sm.working_branch or "", now=branch)
            sm.working_branch = branch

        # Push any LOCAL run-workspace commits/edits to origin/<branch> BEFORE the
        # re-clone below (2026-07-30). The end-gate scans a FRESH CLONE of origin, so
        # unpushed local changes would produce an EMPTY diff (empty .patch files /
        # false-green). This publishes them so the fresh clone + MR carry the change.
        # Best-effort — the empty-diff guard in _run_governance_endgate SUSPENDs if the
        # branch is still empty over its base after this.
        try:
            _pushed, _pdetail = _push_local_workspace_to_origin(run_id, repo, branch)
            logger.info("[SDLC-GOV] endgate pre-scan origin sync",
                        run_id=run_id, pushed=_pushed, detail=_pdetail)
        except Exception as _pe:
            logger.warning("[SDLC-GOV] endgate pre-scan origin sync errored (non-fatal)",
                           run_id=run_id, error=str(_pe))

        # Materialize the run workspace at the committed working branch so the
        # end-gate scan diffs the committed change (base_sha pin intact — the
        # diff basis is validated by the Step 1 live diagnostic).
        try:
            sm._ensure_run_workspace(repo)
        except Exception as _we:
            logger.error("[SDLC-GOV] endgate start: workspace materialization failed — suspending",
                         run_id=run_id, error=str(_we))
            try:
                sm._suspend("GOVERNANCE_SCAN", f"Governance could not start (workspace error): {_we}")
            except Exception:
                update_run_state(run_id, "SUSPENDED", current_stage="GOVERNANCE_SCAN",
                                 error=f"Governance workspace error: {_we}")
            return f"run_endgate_governance_job workspace error for {run_id}"

        # Set the triggering user's GitLab token (thread-local) so the re-draft and
        # any end-gate un-draft use the right credentials. NEVER mutate GITLAB_TOKEN.
        try:
            from agents.sdlc_pipeline import _gov_resolve_gitlab_token
            from tools.gitlab_tools import set_token as _set_gl_token
            _tok = _gov_resolve_gitlab_token(user_id)
            if _tok:
                _set_gl_token(_tok)
        except Exception as _te:
            logger.warning("[SDLC-GOV] endgate start: gitlab token resolve failed (non-fatal)",
                           run_id=run_id, error=str(_te))

        # Re-draft the MR for the gate duration (Open Question 1 → re-draft during
        # gate; _governance_endgate_clear un-drafts on nothing-blocking/approval).
        try:
            from tools.gitlab_tools import gitlab_set_mr_draft
            gitlab_set_mr_draft(gitlab_repo, pr_number, draft=True)
            logger.info("[SDLC-GOV] endgate start: MR re-drafted for gate duration",
                        run_id=run_id, pr_number=pr_number)
        except Exception as _de:
            logger.warning("[SDLC-GOV] endgate start: MR re-draft failed (non-fatal)",
                           run_id=run_id, pr_number=pr_number, error=str(_de))

        logger.info("[SDLC-GOV] author-triggered governance end-gate starting",
                    run_id=run_id, actor=actor, pr_number=pr_number, branch=branch)
        sm._run_governance_endgate(branch=branch, pr_number=pr_number,
                                   pr_url=pr_url, commit_sha=commit_sha)
        return f"endgate governance completed for {run_id}"
    except SDLCCancelled:
        logger.info(f"sdlc_worker: run_endgate_governance_job cancelled for {run_id}")
        return f"endgate governance cancelled for {run_id}"
    except Exception as e:
        logger.error(f"sdlc_worker: run_endgate_governance_job failed → {e}")
        # Governance is optional + fail-closed-recoverable: do NOT strand the run at
        # FAILED (the committed MR is intact) — suspend at GOVERNANCE_SCAN so the
        # author can retry via the governance resume path (Step 7).
        if run_id:
            try:
                from store.sdlc_store import update_run_state
                update_run_state(run_id, "SUSPENDED", current_stage="GOVERNANCE_SCAN",
                                 error=f"Governance end-gate error: {e}")
            except Exception:
                pass
        raise
