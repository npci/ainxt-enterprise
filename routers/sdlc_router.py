# SPDX-License-Identifier: MIT
# ============================================================
# SDLC ROUTER — /sdlc
# Manual triggers + run management + HITL approval gates
# ============================================================

import re as _re
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from auth.dependencies import get_current_user

from auth.dependencies import get_current_user
from core.logger import logger, bind_context
from core.config import (
    CODE_APPROVAL_STATES,
    SDLC_HITL_TTL_HOURS,
    SDLC_GOVERNANCE_HITL_TTL_HOURS,
)
from core.security_validation import (
    validate_sdlc_trigger_request,
    validate_sdlc_approval_request,
    validate_sdlc_reject_request,
    validate_sdlc_cancel_request,
    validate_sdlc_revision_request,
    validate_governance_trigger_request,
    validate_governance_suppression_request,
    validate_governance_decision_request,
    validate_free_text,
    validate_identifier,
    _flatten_errors,
)

def _require_rq() -> None:
    """
    Raise 503 if the RQ worker queue is unavailable.
    The SDLC pipeline must NEVER run inside the gateway process —
    with two gateway instances, in-process BackgroundTask execution
    splits pipeline state across hosts, causing race conditions on
    sdlc_runs (context_patch overwrites) and sdlc_run_events (direct
    inserts from two processes). All pipeline work MUST go through RQ
    workers so the single Kafka consumer and Postgres writer serialise
    all DB writes.
    """
    from core.job_queue import _rq_available  # type: ignore[attr-defined]
    if not _rq_available:
        raise HTTPException(
            status_code=503,
            detail=(
                "RQ worker queue is unavailable. "
                "The SDLC pipeline cannot run — please check Redis and the sdlc_queue workers. "
                "Do not retry until the queue is healthy."
            ),
        )

router = APIRouter(prefix="/sdlc", tags=["sdlc"])


# ── Run visibility scoping (department + owner; admins see all) ────────
#
# Runs are visible to their creator, to everyone in a department the run's repo
# is mapped to (product_repos ⋈ dept_product_mappings), and to admins. This is
# the SAME ACL that scopes /sdlc/products, so run and product visibility can
# never drift. Applied to the list endpoint AND to every /runs/{id} read/action
# below — scoping only the list would leave a direct-object (IDOR) hole.

def _user_scope(current_user: dict) -> dict:
    """Extract the caller's SDLC visibility scope from their JWT claims."""
    return {
        "is_admin":   current_user.get("role") == "admin",
        "owner_ids":  [v for v in (current_user.get("sub"),
                                   current_user.get("id"),
                                   current_user.get("email")) if v],
        "department": current_user.get("department") or "",
    }


def _authorize_run(run: dict, current_user: dict) -> None:
    """Raise 404 unless the caller may see/act on this run.

    Returns 404 (not 403) on a visibility miss so we never leak the existence of
    another department's run — indistinguishable from a genuinely absent run.
    """
    from store.sdlc_store import run_visible_to_user
    if not run_visible_to_user(run, **_user_scope(current_user)):
        raise HTTPException(status_code=404, detail="Run not found")


def _is_run_owner(run: dict, current_user: dict) -> bool:
    """True if the caller triggered this run (its author) or is an admin.

    Governance *author-triage* actions — marking a finding a false positive and
    requesting a fix — are limited to the run owner + admins. This is distinct
    from the domain-approval sign-off gate (auth.rbac.can_approve_domain), which
    is the segregation-of-duties control for approving another team's findings.
    """
    if current_user.get("role") == "admin":
        return True
    ctx = run.get("context") or {}
    cur_email  = (current_user.get("email") or "").strip().lower()
    trig_email = (ctx.get("triggered_by_email") or "").strip().lower()
    if cur_email and trig_email and cur_email == trig_email:
        return True
    cur_ids = {str(v).strip() for v in (current_user.get("sub"),
                                        current_user.get("id"),
                                        current_user.get("email")) if v}
    created_by = str(run.get("created_by") or "").strip()
    trig_uid   = str(ctx.get("triggered_by_user_id") or "").strip()
    if created_by and created_by in cur_ids:
        return True
    if trig_uid and trig_uid in cur_ids:
        return True
    return False


# ── Request Models ────────────────────────────────────────────

class FeatureRequest(BaseModel):
    # Optional so non-Jira teams (or GitHub Issues-only teams) can trigger the
    # pipeline without a ticket reference. ticket_key is the provider-agnostic
    # alternative (e.g. a GitHub issue number/URL); jira_key is kept for
    # backward compatibility with existing Jira-driven triggers. At least a
    # summary is always required — see trigger_feature()'s validation.
    jira_key:          Optional[str] = ""
    ticket_key:        Optional[str] = ""   # provider-agnostic alternative to jira_key
    summary:           str
    description:       Optional[str] = ""
    repo:              Optional[str] = ""
    priority:          Optional[str] = "Medium"
    assignee:          Optional[str] = ""
    language_override: Optional[str] = ""   # e.g. "java", "go", "python" — overrides auto-detect
    product_id:        Optional[str] = None  # product UUID — used to resolve authoritative branch
    branch:            Optional[str] = None  # explicit base branch override
    # Multi-repo SDLC (Phase 5 backend) — list of dependent repos to include
    # in the run. Each entry: {repo: "group/project", ref: "main", kind: "editable"|"compile-only"}.
    # Ignored unless ENABLE_MULTI_REPO_SDLC is on. Optional; falls back to
    # manifest + build-file parsing in preflight when absent.
    dependencies:      Optional[list] = None
    skip_tests:        Optional[bool] = False  # bypass TESTING + SLT_RUNNING (default false = SLT ON; PCI/DSS)
    skip_slt:          Optional[bool] = False  # skip SLT *creation* in CODING (independent of skip_tests)
    # Governance (2026-07-17) — PART 2 opt-in EA/IS/DPDP gate after REVIEW. Default
    # off so existing triggers are unaffected. governance_skills=None → all loaded
    # skills; a list subsets to those slugs/plugin names (see gov config.parse_subset).
    run_governance_review: Optional[bool] = False
    governance_skills:     Optional[list[str]] = None


class BugRequest(BaseModel):
    # See FeatureRequest.jira_key/ticket_key for the rationale — optional so
    # non-Jira teams can trigger the pipeline without a ticket reference.
    jira_key:          Optional[str] = ""
    ticket_key:        Optional[str] = ""   # provider-agnostic alternative to jira_key
    summary:           str
    description:       Optional[str] = ""
    repo:              Optional[str] = ""
    priority:          Optional[str] = "High"
    assignee:          Optional[str] = ""
    language_override: Optional[str] = ""   # e.g. "java", "go", "python" — overrides auto-detect
    product_id:        Optional[str] = None  # product UUID — used to resolve authoritative branch
    branch:            Optional[str] = None  # explicit base branch override
    # Multi-repo SDLC (Phase 5 backend) — see FeatureRequest.dependencies for shape.
    dependencies:      Optional[list] = None
    skip_tests:        Optional[bool] = False  # bypass TESTING + SLT_RUNNING (default false = SLT ON; PCI/DSS)
    skip_slt:          Optional[bool] = False  # skip SLT *creation* in CODING (independent of skip_tests)
    # Governance (2026-07-17) — see FeatureRequest.run_governance_review for the contract.
    run_governance_review: Optional[bool] = False
    governance_skills:     Optional[list[str]] = None


# ── Branch resolution helpers ─────────────────────────────────

def _resolve_base_branch(repo: str, product_id: Optional[str] = None,
                          explicit_branch: Optional[str] = None) -> Optional[str]:
    """
    Resolve the authoritative base branch for an SDLC pipeline run.
    Priority: explicit_branch > product_repos.branch (by product) > product_repos (any) >
              index_requests (latest done) > None (pipeline falls back to _detect_default_branch)
    """
    if explicit_branch and explicit_branch.strip():
        return explicit_branch.strip()
    if not repo:
        return None
    try:
        from db.database import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            if product_id:
                row = db.execute(text(
                    "SELECT branch FROM product_repos WHERE product_id = :pid AND repo_name = :repo LIMIT 1"
                ), {"pid": product_id, "repo": repo}).fetchone()
                if row and row[0]:
                    return row[0]
            # Any product_repos entry for this repo
            row = db.execute(text(
                "SELECT branch FROM product_repos WHERE repo_name = :repo ORDER BY created_at DESC LIMIT 1"
            ), {"repo": repo}).fetchone()
            if row and row[0]:
                return row[0]
            # Last successful index request
            row = db.execute(text(
                "SELECT branch FROM index_requests WHERE repo_name = :repo AND status = 'done' "
                "ORDER BY updated_at DESC LIMIT 1"
            ), {"repo": repo}).fetchone()
            if row and row[0]:
                return row[0]
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"_resolve_base_branch: DB lookup failed for repo={repo!r}: {e}")
    return None


def _make_working_branch(jira_key: str, summary: str, pipeline_type: str, run_id: str = "") -> str:
    """
    Generate a working branch name for SDLC work.
    Format: feature/{jira_key}-{run_id_short}-{slug}  or  fix/{jira_key}-{run_id_short}-{slug}
    Slug: summary lowercased, spaces→dashes, non-alnum stripped, max 30 chars.
    run_id_short: first 8 chars of the run UUID — guarantees every run gets a fresh
    branch on GitLab so stale files from prior runs of the same JIRA cannot leak
    into this run's build workspace via the clone step in _ensure_run_workspace.
    """
    prefix = "fix" if pipeline_type == "bug" else "feature"
    slug = summary.lower()
    slug = _re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = _re.sub(r"[\s-]+", "-", slug).strip("-")[:30].rstrip("-")
    key  = jira_key.lower().replace("_", "-")
    run_part = f"-{run_id[:8]}" if run_id else ""
    return f"{prefix}/{key}{run_part}-{slug}"


class PRReviewRequest(BaseModel):
    pr_number:   int
    title:       str
    body:        Optional[str] = ""
    repo:        str
    branch:      Optional[str] = ""
    base:        Optional[str] = "main"
    author:      Optional[str] = ""
    url:         Optional[str] = ""
    diff_url:    Optional[str] = ""


class ApprovalRequest(BaseModel):
    feedback:           Optional[str]  = ""     # optional reviewer notes
    approved_by:        Optional[str]  = "user"
    skip_tests_override: Optional[bool] = None  # None = keep context value; True/False = override at resume time
    skip_compile_override: Optional[bool] = None  # True = "Skip compilation & continue" at a post-apply build failure —
                                                  # sets compile_skipped so the post-gate machine re-applies the
                                                  # existing VERIFIED_DIFF, skips the build, and pushes. None = unchanged.


class RejectRequest(BaseModel):
    reason:      str
    rejected_by: Optional[str] = "user"


class CancelRequest(BaseModel):
    reason:       str = "Cancelled by user"
    cancelled_by: str = "user"


class FileComment(BaseModel):
    """One structured per-file reviewer comment. `line` is optional (a whole-file
    comment when omitted). `comment` must be non-blank (mirrors the governance
    send-back rule)."""
    file:    str
    line:    Optional[int] = None
    comment: str


class RevisionRequest(BaseModel):
    feedback:      str = ""                              # whole-run free-text (as today)
    revised_by:    str = "user"
    # Additive per-file structured request-changes. Optional — whole-run feedback
    # keeps working unchanged when this is absent.
    file_comments: Optional[list[FileComment]] = None


class AnswerQuestionsRequest(BaseModel):
    """Submitted at AWAITING_USER_INPUT to resolve analyst-raised open_questions.

    `answers` is a list aligned 1-1 with `run.context.pending_questions`. Each
    entry carries either the index of the chosen option OR a free-text answer
    (or both — selected_option lets the UI track WHICH option was picked even
    if the user then edited the text).
    """
    answers:      list   # [{"selected_option": int|None, "answer": str}]
    answered_by:  Optional[str] = "user"
    # WS-5 (2026-07-02 gate reorder): optional human edits to the WorkItem,
    # submitted alongside GATE 1 (normalization) approval — e.g. from the
    # WorkItemPanel "approve with edits" flow. Ignored for GATE 2 (questions).
    work_item:    Optional[dict] = None


# ── Background task helpers ───────────────────────────────────

def _bg_feature(issue_dict: dict, run_id: str):
    try:
        from agents.sdlc_pipeline import run_feature_pipeline
        run_feature_pipeline(issue_dict, run_id)
    except Exception as e:
        logger.error(f"sdlc_router: feature pipeline error → {e}")
        from store.sdlc_store import update_run_state
        update_run_state(run_id, "FAILED", error=str(e))


def _bg_bug(issue_dict: dict, run_id: str):
    try:
        from agents.sdlc_pipeline import run_bug_pipeline
        run_bug_pipeline(issue_dict, run_id)
    except Exception as e:
        logger.error(f"sdlc_router: bug pipeline error → {e}")
        from store.sdlc_store import update_run_state
        update_run_state(run_id, "FAILED", error=str(e))


def _bg_pr_review(pr_dict: dict, run_id: str):
    try:
        from agents.sdlc_pipeline import run_pr_review_pipeline
        run_pr_review_pipeline(pr_dict, run_id)
    except Exception as e:
        logger.error(f"sdlc_router: PR review pipeline error → {e}")
        from store.sdlc_store import update_run_state
        update_run_state(run_id, "FAILED", error=str(e))


def _bg_resume_feature(run_id: str, feedback: str):
    try:
        from agents.sdlc_pipeline import resume_feature_after_design_approval
        resume_feature_after_design_approval(run_id, feedback)
    except Exception as e:
        logger.error(f"sdlc_router: feature resume error → {e}")
        from store.sdlc_store import update_run_state
        update_run_state(run_id, "FAILED", error=str(e))


def _bg_resume_bug(run_id: str, feedback: str):
    try:
        from agents.sdlc_pipeline import resume_bug_after_solution_approval
        resume_bug_after_solution_approval(run_id, feedback)
    except Exception as e:
        logger.error(f"sdlc_router: bug resume error → {e}")
        from store.sdlc_store import update_run_state
        update_run_state(run_id, "FAILED", error=str(e))


def _bg_resume_feature_revision(run_id: str, feedback: str):
    try:
        from agents.sdlc_pipeline import run_feature_revision
        run_feature_revision(run_id, feedback)
    except Exception as e:
        logger.error(f"sdlc_router: feature revision error → {e}")
        from store.sdlc_store import update_run_state
        update_run_state(run_id, "FAILED", error=str(e))


def _bg_resume_bug_revision(run_id: str, feedback: str):
    try:
        from agents.sdlc_pipeline import run_bug_revision
        run_bug_revision(run_id, feedback)
    except Exception as e:
        logger.error(f"sdlc_router: bug revision error → {e}")
        from store.sdlc_store import update_run_state
        update_run_state(run_id, "FAILED", error=str(e))


def _bg_resume_pr(run_id: str):
    try:
        from agents.sdlc_pipeline import resume_after_pr_approval
        resume_after_pr_approval(run_id)
    except Exception as e:
        logger.error(f"sdlc_router: PR approval resume error → {e}")
        from store.sdlc_store import update_run_state
        update_run_state(run_id, "FAILED", error=str(e))


# ─────────────────────────────────────────────────────────────
# POST /sdlc/feature
# Manual trigger: start a feature SDLC pipeline
# ─────────────────────────────────────────────────────────────

@router.post("/feature")
def trigger_feature(req: FeatureRequest, background_tasks: BackgroundTasks,
                    current_user: dict = Depends(get_current_user)):
    """
    Manually trigger a feature SDLC pipeline for a Jira issue.
    Returns the run_id immediately; the pipeline runs in the background.
    Poll GET /sdlc/runs/{run_id} for status.
    """
    if not (req.repo or "").strip() and not (req.language_override or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "Either 'repo' (namespace/project format, e.g. ainxt/payment-service) or "
                "'language_override' (e.g. java, go, python) is required. "
                "Index the codebase first via CodebaseManager, or provide a language override."
            ),
        )

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    is_valid, field_errors, sanitized = validate_sdlc_trigger_request(req)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    req.summary = sanitized["summary"]
    req.description = sanitized["description"]

    from store.sdlc_store import create_run
    _user_id    = current_user.get("sub", "")   # UUID — direct key into user_tokens.user_id
    _user_email = current_user.get("email", "")  # kept for display/logging only
    # jira_key is Jira-specific; ticket_key is the provider-agnostic alternative
    # (e.g. a GitHub issue reference). Fall back to a generated placeholder so
    # branch naming / run creation never breaks when neither is supplied.
    _ticket_key = (req.jira_key or req.ticket_key or "").strip()
    issue_dict = req.model_dump()
    issue_dict["key"]                  = _ticket_key
    issue_dict["issue_type"]           = "Story"
    issue_dict["triggered_by_user_id"]    = current_user.get("id") or current_user.get("sub", "")
    issue_dict["triggered_by_email"] = current_user.get("email", "")
    issue_dict["product_id"]           = req.product_id or ""
    issue_dict["dependencies"]         = req.dependencies or []

    # Admission pre-check (read-only) BEFORE persisting a run row, so a rate-limited
    # or duplicate trigger never leaves an orphan run with no job. The in-enqueue
    # guard remains authoritative (defense-in-depth + webhook path).
    from core.job_queue import check_sdlc_admission
    _reporter = (current_user.get("email") or current_user.get("sub") or "unknown").lower().strip()
    _adm = check_sdlc_admission(req.jira_key, _reporter)
    if _adm.get("existing_job_id"):
        return {
            "run_id":   None,
            "job_id":   _adm["existing_job_id"],
            "message":  "Pipeline already running for this Jira",
            "jira_key": req.jira_key,
        }
    if not _adm.get("allowed", True):
        raise HTTPException(status_code=429, detail=_adm.get("reason") or "SDLC running limit reached")

    # Resolve base branch (DB lookup only — synchronous but fast)
    _base_branch = _resolve_base_branch(req.repo or "", req.product_id, req.branch)

    # Create the run first so its UUID can be embedded in the working branch name.
    # A unique per-run branch prevents stale files from prior runs of the same JIRA
    # from being cloned into the build workspace.
    run = create_run(
        run_type="feature",
        jira_key=_ticket_key,
        jira_summary=req.summary,
        repo=req.repo or "",
        triggered_by=_user_email or _user_id or "manual",
        created_by=_user_id or _user_email or "",
    )
    issue_dict["_run_id"] = run["id"]

    _working_branch = _make_working_branch(_ticket_key or run["id"][:8], req.summary, "feature", run_id=run["id"])
    # Always set working_branch in the issue dict so the HITL resume always has it.
    issue_dict["working_branch"] = _working_branch
    if _base_branch:
        issue_dict["base_branch"] = _base_branch
        logger.info(
            f"sdlc/feature: branch resolved for {_ticket_key}: "
            f"base={_base_branch!r} working={_working_branch!r}"
        )
    else:
        logger.info(
            f"sdlc/feature: no base_branch resolved for {_ticket_key} "
            f"— working={_working_branch!r}, pipeline will detect default branch"
        )

    # Persist branch info + skip flags in run context so pipeline reads them from ctx
    from store.sdlc_store import update_run_state
    update_run_state(run["id"], run["state"],
                     branch=_working_branch,
                     context_patch={
                         "base_branch":    _base_branch or "",
                         "working_branch": _working_branch,
                         "skip_tests":     bool(req.skip_tests),
                         "skip_slt":       bool(req.skip_slt),
                         # Persist the triggering user's identity into the JSONB run
                         # context AT CREATION — the router is the one place current_user
                         # is reliably in hand. Downstream phases (PLAN, IMPLEMENT via the
                         # state machine) read ctx["user_id"]/["user_email"] to resolve the
                         # per-user GitLab PAT for clones (primary AND dependent repos).
                         # Previously identity was only written later by _phase_preflight
                         # from the issue dict, gated by `if user_id or user_email` and
                         # skipped on resume via preflight_ok — so any run whose context
                         # lost it dropped to the env GITLAB_TOKEN for the dep clone and
                         # 403'd on a private dependent repo the user's PAT could reach.
                         # Resume paths rebuild the issue from ctx["user_id"]
                         # (_rebuild_issue_from_context), so persisting here fixes them too.
                         "user_id":        _user_id,
                         "user_email":     _user_email,
                         "product_id":     req.product_id or "",
                         # Governance (STEP 8) — mirrors skip_tests: persisted into the
                         # JSONB run context, read by the worker when constructing the SM.
                         "run_governance_review": bool(req.run_governance_review),
                         "governance_skills":     req.governance_skills,
                         "dependencies":          req.dependencies or [],
                     })

    # Notify inbox immediately at trigger time
    try:
        from store.inbox_store import publish_inbox_item
        _branch_info = f" | Branch: {_base_branch}" if _base_branch else ""
        publish_inbox_item(
            user_id="platform",
            type="sdlc_started",
            title=f"[SDLC/FEATURE] Pipeline started — {_ticket_key}",
            body=f"Feature pipeline triggered for **{_ticket_key}**: {req.summary}\nRepo: {req.repo or 'auto-detect'} | Priority: {req.priority}{_branch_info}",
            source_id=run["id"],
            metadata={"run_id": run["id"], "jira_key": _ticket_key, "stage": "triggered", "pipeline": "feature"},
        )
    except Exception:
        pass

    # Pipeline MUST run in RQ workers — never in-process.
    # Two gateway instances sharing the same DB/Kafka cannot safely run
    # pipeline code in FastAPI BackgroundTasks without state split.
    _require_rq()
    from core.job_queue import enqueue_sdlc_job
    try:
        job_id = enqueue_sdlc_job("workers.sdlc_worker.run_feature_pipeline_job", issue_dict)
    except RuntimeError as _rq_err:
        # Rate-limit or dedup rejection from enqueue_sdlc_job — surface as 429
        raise HTTPException(status_code=429, detail=str(_rq_err))

    logger.info(f"sdlc/feature: triggered run {run['id']} for {_ticket_key} job_id={job_id}")
    return {
        "run_id":          run["id"],
        "job_id":          job_id,
        "state":           run["state"],
        "jira_key":        _ticket_key,
        "base_branch":     _base_branch,
        "working_branch":  _working_branch if _base_branch else None,
        "message":         "Feature pipeline started",
    }


# ─────────────────────────────────────────────────────────────
# POST /sdlc/bug
# Manual trigger: start a bug SDLC pipeline
# ─────────────────────────────────────────────────────────────

@router.post("/bug")
def trigger_bug(req: BugRequest, background_tasks: BackgroundTasks,
                current_user: dict = Depends(get_current_user)):
    """
    Manually trigger a bug SDLC pipeline for a Jira issue.
    Returns the run_id immediately; the pipeline runs in the background.
    """
    if not (req.repo or "").strip() and not (req.language_override or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "Either 'repo' (namespace/project format, e.g. ainxt/payment-service) or "
                "'language_override' (e.g. java, go, python) is required. "
                "Index the codebase first via CodebaseManager, or provide a language override."
            ),
        )

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    is_valid, field_errors, sanitized = validate_sdlc_trigger_request(req)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    req.summary = sanitized["summary"]
    req.description = sanitized["description"]

    from store.sdlc_store import create_run
    _user_id    = current_user.get("sub", "")
    _user_email = current_user.get("email", "")
    # jira_key is Jira-specific; ticket_key is the provider-agnostic alternative.
    _ticket_key = (req.jira_key or req.ticket_key or "").strip()
    issue_dict = req.model_dump()
    issue_dict["key"]                  = _ticket_key
    issue_dict["issue_type"]           = "Bug"
    issue_dict["triggered_by_user_id"]    = current_user.get("id") or current_user.get("sub", "")
    issue_dict["triggered_by_email"] = current_user.get("email", "")
    issue_dict["product_id"]           = req.product_id or ""
    issue_dict["dependencies"]         = req.dependencies or []

    # Admission pre-check (read-only) BEFORE persisting a run row, so a rate-limited
    # or duplicate trigger never leaves an orphan run with no job. The in-enqueue
    # guard remains authoritative (defense-in-depth + webhook path).
    from core.job_queue import check_sdlc_admission
    _reporter = (current_user.get("email") or current_user.get("sub") or "unknown").lower().strip()
    _adm = check_sdlc_admission(req.jira_key, _reporter)
    if _adm.get("existing_job_id"):
        return {
            "run_id":   None,
            "job_id":   _adm["existing_job_id"],
            "message":  "Pipeline already running for this Jira",
            "jira_key": req.jira_key,
        }
    if not _adm.get("allowed", True):
        raise HTTPException(status_code=429, detail=_adm.get("reason") or "SDLC running limit reached")

    # Resolve base branch
    _base_branch = _resolve_base_branch(req.repo or "", req.product_id, req.branch)

    # Create the run first so its UUID can be embedded in the working branch name.
    # A unique per-run branch prevents stale files from prior runs of the same JIRA
    # from being cloned into the build workspace.
    run = create_run(
        run_type="bug",
        jira_key=_ticket_key,
        jira_summary=req.summary,
        repo=req.repo or "",
        triggered_by=_user_email or _user_id or "manual",
    )
    issue_dict["_run_id"] = run["id"]

    _working_branch = _make_working_branch(_ticket_key or run["id"][:8], req.summary, "bug", run_id=run["id"])
    # Always set working_branch in the issue dict so the HITL resume always has it.
    # base_branch is only set when resolved — pipeline falls back to _detect_default_branch.
    issue_dict["working_branch"] = _working_branch
    if _base_branch:
        issue_dict["base_branch"] = _base_branch
        logger.info(
            f"sdlc/bug: branch resolved for {_ticket_key}: "
            f"base={_base_branch!r} working={_working_branch!r}"
        )
    else:
        logger.info(
            f"sdlc/bug: no base_branch resolved for {_ticket_key} "
            f"— working={_working_branch!r}, pipeline will detect default branch"
        )

    from store.sdlc_store import update_run_state
    update_run_state(run["id"], run["state"],
                     branch=_working_branch,
                     context_patch={
                         "base_branch":    _base_branch or "",
                         "working_branch": _working_branch,
                         "skip_tests":     bool(req.skip_tests),
                         "skip_slt":       bool(req.skip_slt),
                         # Persist the triggering user's identity into the JSONB run
                         # context AT CREATION — the router is the one place current_user
                         # is reliably in hand. Downstream phases (PLAN, IMPLEMENT via the
                         # state machine) read ctx["user_id"]/["user_email"] to resolve the
                         # per-user GitLab PAT for clones (primary AND dependent repos).
                         # Previously identity was only written later by _phase_preflight
                         # from the issue dict, gated by `if user_id or user_email` and
                         # skipped on resume via preflight_ok — so any run whose context
                         # lost it dropped to the env GITLAB_TOKEN for the dep clone and
                         # 403'd on a private dependent repo the user's PAT could reach.
                         # Resume paths rebuild the issue from ctx["user_id"]
                         # (_rebuild_issue_from_context), so persisting here fixes them too.
                         "user_id":        _user_id,
                         "user_email":     _user_email,
                         "product_id":     req.product_id or "",
                         # Governance (STEP 8) — mirrors skip_tests: persisted into the
                         # JSONB run context, read by the worker when constructing the SM.
                         "run_governance_review": bool(req.run_governance_review),
                         "governance_skills":     req.governance_skills,
                         "dependencies":          req.dependencies or [],
                     })

    # Notify inbox immediately at trigger time
    try:
        from store.inbox_store import publish_inbox_item
        _branch_info = f" | Branch: {_base_branch}" if _base_branch else ""
        publish_inbox_item(
            user_id="platform",
            type="sdlc_started",
            title=f"[SDLC/BUG] Pipeline started — {_ticket_key}",
            body=f"Bug pipeline triggered for **{_ticket_key}**: {req.summary}\nRepo: {req.repo or 'auto-detect'} | Priority: {req.priority}{_branch_info}",
            source_id=run["id"],
            metadata={"run_id": run["id"], "jira_key": _ticket_key, "stage": "triggered", "pipeline": "bug"},
        )
    except Exception:
        pass

    _require_rq()
    from core.job_queue import enqueue_sdlc_job
    try:
        job_id = enqueue_sdlc_job("workers.sdlc_worker.run_bug_pipeline_job", issue_dict)
    except RuntimeError as _rq_err:
        raise HTTPException(status_code=429, detail=str(_rq_err))

    logger.info(f"sdlc/bug: triggered run {run['id']} for {_ticket_key} job_id={job_id}")
    return {
        "run_id":          run["id"],
        "job_id":          job_id,
        "state":           run["state"],
        "jira_key":        _ticket_key,
        "base_branch":     _base_branch,
        "working_branch":  _working_branch if _base_branch else None,
        "message":         "Bug pipeline started",
    }


# ─────────────────────────────────────────────────────────────
# POST /sdlc/review-pr
# Manual trigger: start a PR review pipeline
# ─────────────────────────────────────────────────────────────

@router.post("/review-pr")
def trigger_pr_review(req: PRReviewRequest, background_tasks: BackgroundTasks):
    """
    Manually trigger a PR review pipeline.
    Useful for reviewing PRs not created by the SDLC coding agent.
    """
    from store.sdlc_store import create_run
    pr_dict = req.model_dump()

    run = create_run(
        run_type="pr_review",
        repo=req.repo,
        triggered_by="manual",
    )
    pr_dict["_run_id"] = run["id"]

    # Notify inbox immediately at trigger time
    try:
        from store.inbox_store import publish_inbox_item
        publish_inbox_item(
            user_id="platform",
            type="sdlc_started",
            title=f"[SDLC/PR-REVIEW] Review started — PR #{req.pr_number}",
            body=f"PR review pipeline triggered for **PR #{req.pr_number}** in {req.repo}\nTitle: {req.title}",
            source_id=run["id"],
            metadata={"run_id": run["id"], "pr_number": req.pr_number, "repo": req.repo, "stage": "triggered", "pipeline": "pr_review"},
        )
    except Exception:
        pass

    _require_rq()
    from core.job_queue import enqueue_sdlc_job
    try:
        job_id = enqueue_sdlc_job("workers.sdlc_worker.run_pr_review_pipeline_job", pr_dict)
    except RuntimeError as _rq_err:
        raise HTTPException(status_code=429, detail=str(_rq_err))

    logger.info(f"sdlc/review-pr: triggered run {run['id']} for PR #{req.pr_number} job_id={job_id}")
    return {
        "run_id":    run["id"],
        "job_id":    job_id,
        "state":     run["state"],
        "pr_number": req.pr_number,
        "message":   "PR review pipeline started",
    }


# ─────────────────────────────────────────────────────────────
# GET /sdlc/products
# List products with at least one repo configured — for UI dropdowns
# ─────────────────────────────────────────────────────────────

@router.get("/products")
def list_sdlc_products(current_user: dict = Depends(get_current_user)):
    """
    Return products that have at least one repo configured in product_repos.
    Filtered by the current user's department via dept_product_mappings.
    Admins see all products.
    """
    try:
        from db.database import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            user_dept = current_user.get("department", "")
            user_role = current_user.get("role", "user")

            if user_role == "admin" or not user_dept:
                rows = db.execute(text("""
                    SELECT DISTINCT p.id, p.name, p.description
                    FROM products p
                    JOIN product_repos pr ON pr.product_id = p.id
                    WHERE p.is_active = true
                    ORDER BY p.name
                """)).fetchall()
            else:
                rows = db.execute(text("""
                    SELECT DISTINCT p.id, p.name, p.description
                    FROM products p
                    JOIN product_repos pr ON pr.product_id = p.id
                    LEFT JOIN dept_product_mappings dpm ON dpm.product_id = p.id
                    WHERE p.is_active = true AND (dpm.department = :dept OR dpm.department IS NULL)
                    ORDER BY p.name
                """), {"dept": user_dept}).fetchall()

            products = [
                {"id": str(r[0]), "name": r[1], "description": r[2] or ""}
                for r in rows
            ]
            return {"products": products}
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"sdlc/products: DB lookup failed: {e}")
        return {"products": []}


# ─────────────────────────────────────────────────────────────
# GET /sdlc/products/{product_id}/repos
# List repos + branches for a product — for UI branch auto-fill
# ─────────────────────────────────────────────────────────────

@router.get("/products/{product_id}/repos")
def list_product_repos(product_id: str, current_user: dict = Depends(get_current_user)):
    """
    Return repos and their configured branches for a product.
    Also checks index_requests for repos not yet in product_repos.
    """
    try:
        from db.database import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            rows = db.execute(text("""
                SELECT repo_name, branch
                FROM product_repos
                WHERE product_id = :pid
                ORDER BY repo_name
            """), {"pid": product_id}).fetchall()

            repos = [{"repo": r[0], "branch": r[1]} for r in rows]

            # Augment with any indexed repos for this product not yet in product_repos
            if not repos:
                idx_rows = db.execute(text("""
                    SELECT DISTINCT repo_name, branch
                    FROM index_requests
                    WHERE product_id = :pid AND status = 'done'
                    ORDER BY repo_name
                """), {"pid": product_id}).fetchall()
                repos = [{"repo": r[0], "branch": r[1]} for r in idx_rows]

            return {"product_id": product_id, "repos": repos}
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"sdlc/products/{product_id}/repos: DB lookup failed: {e}")
        return {"product_id": product_id, "repos": []}


# ─────────────────────────────────────────────────────────────
# GET /sdlc/repo/{repo}/dependencies
# Read .sdlc.yml dependencies: from the named repo at the given ref.
# Used by the trigger-form dep table to pre-fill from manifest.
# ─────────────────────────────────────────────────────────────

@router.get("/repo/{repo:path}/dependencies")
def get_repo_dependencies(
    repo: str,
    ref: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """
    Return the `dependencies:` block from `.sdlc.yml` in the named repo, parsed
    into the same shape the trigger payload uses. 404 when the repo has no
    `.sdlc.yml` or the block is absent — the UI treats 404 as "show empty dep
    table".

    Token resolution: per-user GitLab PAT from user_tokens, falls back to the
    GITLAB_TOKEN env var. The same path preflight uses.
    """
    if not repo or "/" not in repo:
        raise HTTPException(status_code=400, detail="repo must be in 'group/project' format")

    # Install caller's GitLab token in thread-local before any gitlab_tools call.
    try:
        from core.platform_credentials import get_gitlab_token as _get_gl
        from tools.gitlab_tools import set_token as _set_gl
        import os as _os
        try:
            tok = _get_gl(user_id=current_user.get("id") or current_user.get("sub", ""))
        except PermissionError:
            tok = _os.getenv("GITLAB_TOKEN", "")
        if tok and ":" in tok:
            tok = tok.split(":", 1)[-1]
        if tok:
            _set_gl(tok)
    except Exception as exc:
        logger.warning(f"sdlc/repo/{repo}/dependencies: token setup failed: {exc}")

    try:
        from agents.dep_resolver import resolve_dependencies
    except Exception as exc:
        logger.error(f"sdlc/repo/{repo}/dependencies: dep_resolver unavailable: {exc}")
        raise HTTPException(status_code=500, detail="dep_resolver unavailable")

    primary_branch = (ref or "").strip() or "main"
    try:
        specs = resolve_dependencies(
            primary_repo=repo,
            primary_branch=primary_branch,
            user_overrides=None,
            fetch_manifest=True,
            fetch_build_files=False,  # UI pre-fill is manifest-only; build-file inferences
                                       # appear at trigger time only when preflight runs.
        )
    except Exception as exc:
        logger.warning(f"sdlc/repo/{repo}/dependencies: resolve failed: {exc}")
        raise HTTPException(status_code=404, detail="No dependencies manifest")

    if not specs:
        raise HTTPException(status_code=404, detail="No dependencies declared in .sdlc.yml")

    return {
        "repo": repo,
        "ref":  primary_branch,
        "dependencies": [
            {"repo": s.repo, "ref": s.ref, "kind": s.kind, "source": s.source}
            for s in specs
        ],
    }


# ─────────────────────────────────────────────────────────────
# GET /sdlc/runs
# List recent pipeline runs
# ─────────────────────────────────────────────────────────────

@router.get("/runs")
def list_runs(
    limit:    int           = Query(default=50, ge=1, le=200),
    run_type: Optional[str] = Query(default=None),
    current_user: dict      = Depends(get_current_user),
):
    """Return recent SDLC pipeline runs, newest first, scoped to the caller's own
    runs + their department's runs (admins see all). Auto-cancels stale runs lazily."""
    import threading
    from store.sdlc_store import list_runs, cancel_stale_runs
    # Run stale cleanup in background so it doesn't add latency to the request
    threading.Thread(target=cancel_stale_runs, args=(4,), daemon=True).start()
    runs = list_runs(limit=limit, run_type=run_type, **_user_scope(current_user))
    return {"runs": runs, "total": len(runs)}


# ─────────────────────────────────────────────────────────────
# GET /sdlc/runs/{run_id}
# Get a specific run + its events
# ─────────────────────────────────────────────────────────────

@router.get("/runs/{run_id}")
def get_run(run_id: str, current_user: dict = Depends(get_current_user)):
    """Return a specific SDLC run and its full event history."""
    from store.sdlc_store import get_run, get_run_events
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)
    events = get_run_events(run_id)
    return {"run": run, "events": events}


# ─────────────────────────────────────────────────────────────
# GET /sdlc/jira-ticket/{key}
# Read-only JIRA ticket fetch for TriggerModal auto-fill
# ─────────────────────────────────────────────────────────────

@router.get("/jira-ticket/{key}")
def get_jira_ticket(key: str, current_user: dict = Depends(get_current_user)):
    """Fetch JIRA ticket metadata for UI auto-fill. Read-only, no pipeline side-effects."""
    from tools.jira_tools import jira_get_issue_dict
    _user_id    = current_user.get("sub", "")
    _user_email = current_user.get("email", "")
    try:
        data = jira_get_issue_dict(key.upper(), user_id=_user_id, user_email=_user_email)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"JIRA fetch failed: {e}")
    if not data:
        raise HTTPException(status_code=404, detail=f"JIRA ticket {key.upper()} not found")
    return data


# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/approve
# HITL approval gate — resume pipeline
# ─────────────────────────────────────────────────────────────

@router.post("/runs/{run_id}/approve")
def approve_run(run_id: str, body: ApprovalRequest, background_tasks: BackgroundTasks,
                current_user: dict = Depends(get_current_user)):
    """
    Human-in-the-loop approval gate.

    Allowed states for approval:
      AWAITING_CODE_APPROVAL (or legacy AWAITING_DESIGN_APPROVAL) → resume feature coding phase
      AWAITING_SOLUTION_APPROVAL → resume bug coding phase
      AWAITING_PR_APPROVAL → mark run COMPLETE

    Pass optional `feedback` string to provide notes to the coding agent.
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_approve")
    import time
    from store.sdlc_store import get_run, update_run_state, add_run_event
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    # Check HITL approval TTL — runs waiting >48h are expired
    ctx      = run.get("context") or {}
    deadline = ctx.get("hitl_deadline")
    if deadline and time.time() > deadline:
        update_run_state(run_id, "EXPIRED",
                         error=f"Approval window expired (>{SDLC_HITL_TTL_HOURS}h). Re-trigger the pipeline.")
        raise HTTPException(
            status_code=410,
            detail=f"Approval window for run {run_id} expired. Re-trigger the pipeline."
        )

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
    # feedback is optional reviewer free text, rendered into run events / the
    # coding agent's context, so XSS-only.
    is_valid, field_errors, sanitized = validate_sdlc_approval_request(body)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    body.feedback = sanitized["feedback"]

    state = run["state"]
    approved_by = body.approved_by or "user"
    feedback    = body.feedback or ""
    skip_tests_override = body.skip_tests_override  # None = keep stored value; True/False = override
    skip_compile_override = body.skip_compile_override  # True = skip build & push at a post-apply build failure

    from core.job_queue import enqueue_hitl_resume_job

    if state in CODE_APPROVAL_STATES:
        add_run_event(run_id, state, "APPROVED", actor=approved_by, output=f"Design approved. Feedback: {feedback}")
        # Persist skip_tests override into context before re-enqueue so the worker
        # reads the updated value without needing a payload change.
        _ctx = {"design_feedback": feedback, "approved_by": approved_by}
        if skip_tests_override is not None:
            _ctx["skip_tests"] = bool(skip_tests_override)
            logger.info(
                f"sdlc/approve: run {run_id} design-approval skip_tests_override={skip_tests_override} "
                f"persisted to context by {approved_by}"
            )
        if skip_compile_override:
            # 'Skip compilation & continue' at a post-apply build failure: the
            # post-gate machine reads compile_skipped and re-applies the EXISTING
            # VERIFIED_DIFF, skips the build, and pushes. Adds a waiver banner.
            _ctx["compile_skipped"] = True
            _banners = list(ctx.get("waiver_banners") or [])
            _banners.append(
                f"⚠ Compilation SKIPPED at post-apply build failure by {approved_by} — "
                f"code was committed WITHOUT a successful build."
            )
            _ctx["waiver_banners"] = _banners
            logger.info(
                f"sdlc/approve: run {run_id} design-approval skip_compile_override=True "
                f"persisted (compile_skipped) to context by {approved_by}"
            )
        update_run_state(run_id, "APPROVED", context_patch=_ctx)
        enqueue_hitl_resume_job("workers.sdlc_worker.resume_feature_job", run_id, feedback)
        return {"run_id": run_id, "action": "approved", "next_state": "CODING", "message": "Feature coding phase started"}

    elif state == "AWAITING_SOLUTION_APPROVAL":
        add_run_event(run_id, state, "APPROVED", actor=approved_by, output=f"Solution approved. Feedback: {feedback}")
        _ctx = {"solution_feedback": feedback, "approved_by": approved_by}
        if skip_tests_override is not None:
            _ctx["skip_tests"] = bool(skip_tests_override)
            logger.info(
                f"sdlc/approve: run {run_id} solution-approval skip_tests_override={skip_tests_override} "
                f"persisted to context by {approved_by}"
            )
        if skip_compile_override:
            # 'Skip compilation & continue' at a post-apply build failure (bug run).
            _ctx["compile_skipped"] = True
            _banners = list(ctx.get("waiver_banners") or [])
            _banners.append(
                f"⚠ Compilation SKIPPED at post-apply build failure by {approved_by} — "
                f"code was committed WITHOUT a successful build."
            )
            _ctx["waiver_banners"] = _banners
            logger.info(
                f"sdlc/approve: run {run_id} solution-approval skip_compile_override=True "
                f"persisted (compile_skipped) to context by {approved_by}"
            )
        update_run_state(run_id, "APPROVED", context_patch=_ctx)
        enqueue_hitl_resume_job("workers.sdlc_worker.resume_bug_job", run_id, feedback)
        return {"run_id": run_id, "action": "approved", "next_state": "CODING", "message": "Bug coding phase started"}

    elif state == "AWAITING_PR_APPROVAL":
        add_run_event(run_id, state, "COMPLETE", actor=approved_by, output="PR approved and merged")
        update_run_state(run_id, "COMPLETE", context_patch={"pr_approved_by": approved_by})
        enqueue_hitl_resume_job("workers.sdlc_worker.resume_pr_approval_job", run_id)
        return {"run_id": run_id, "action": "approved", "next_state": "COMPLETE", "message": "Run marked COMPLETE"}

    elif state == "AWAITING_RE_REVIEW":
        # Engineer manually approves after AI addressed review comments → enqueue merge
        add_run_event(run_id, state, "MERGE_READY", actor=approved_by,
                      output="Re-review approved — merging PR")
        update_run_state(run_id, "MERGE_READY", context_patch={"re_review_approved_by": approved_by})
        from core.job_queue import enqueue_merge_pr_job
        job_id = enqueue_merge_pr_job(run_id)
        return {"run_id": run_id, "action": "approved", "next_state": "MERGED",
                "job_id": job_id, "message": "Merge job enqueued"}

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Run is in state '{state}' — approval not applicable. "
                   f"Expected AWAITING_CODE_APPROVAL | AWAITING_SOLUTION_APPROVAL | "
                   f"AWAITING_PR_APPROVAL | AWAITING_RE_REVIEW"
        )


# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/answer-questions
# Submit answers to the analyst's open_questions (AWAITING_USER_INPUT gate)
# ─────────────────────────────────────────────────────────────

@router.post("/runs/{run_id}/answer-questions")
def answer_questions(
    run_id: str,
    body: AnswerQuestionsRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Resolve a pipeline paused at AWAITING_USER_INPUT.

    Validates the state, pairs each answer with its pending question, stores
    the result in run.context.user_answers, and re-enqueues the worker. The
    second pipeline pass sees the answers in the analyst prompt and produces
    an empty open_questions list, so the gate fires at most once per run.
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_answer_questions")
    from store.sdlc_store import get_run, add_run_event
    import time as _time

    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    state = run.get("state", "")
    if state != "AWAITING_USER_INPUT":
        raise HTTPException(
            status_code=400,
            detail=f"Run is in state {state!r} — answer-questions only valid at AWAITING_USER_INPUT",
        )

    ctx = run.get("context") or {}
    deadline = ctx.get("hitl_deadline")
    if deadline and _time.time() > deadline:
        from store.sdlc_store import update_run_state as _urs
        _urs(run_id, "EXPIRED",
             error=f"Question-answer window expired (>{SDLC_HITL_TTL_HOURS}h). Re-trigger the pipeline.")
        raise HTTPException(
            status_code=410,
            detail=f"Question window for run {run_id} expired. Re-trigger the pipeline.",
        )

    answers = body.answers or []
    if not isinstance(answers, list):
        raise HTTPException(status_code=400, detail="`answers` must be a list aligned with pending_questions")

    # Distinguish the normalization HITL gate from the analyst-stage question gate.
    # gate_kind="normalization" is set in context when TICKET_NORMALIZATION suspends;
    # absent (or any other value) means the analyst-stage gate — route accordingly
    # so each resume path continues to the correct stage.
    _gate_kind = ctx.get("gate_kind", "")

    add_run_event(
        run_id, state, "RESUMING_FROM_QUESTIONS",
        actor=body.answered_by or current_user.get("email", "user"),
        output=f"User submitted {len(answers)} answer(s) — re-enqueuing pipeline (gate_kind={_gate_kind!r})",
        data={"answer_count": len(answers), "gate_kind": _gate_kind},
    )
    logger.info(
        f"sdlc/answer-questions: answers merged + locked — "
        f"run_id={run_id} gate_kind={_gate_kind!r} n_answers={len(answers)}",
    )

    try:
        if _gate_kind == "normalization":
            from agents.sdlc_pipeline import resume_after_normalization_confirmed
            resume_after_normalization_confirmed(run_id, answers, work_item=body.work_item)
        else:
            from agents.sdlc_pipeline import resume_after_user_answers
            resume_after_user_answers(run_id, answers)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"sdlc/runs/{run_id}/answer-questions: resume failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Could not resume pipeline: {exc}")

    return {
        "run_id":     run_id,
        "action":     "resumed",
        "gate_kind":  _gate_kind or "questions",
        "next_state": "CLASSIFYING" if _gate_kind == "normalization" else "PLAN",
        "message":    f"Pipeline re-enqueued with {len(answers)} user answer(s).",
    }


# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/reject
# HITL rejection — mark run FAILED with reason
# ─────────────────────────────────────────────────────────────

@router.post("/runs/{run_id}/reject")
def reject_run(run_id: str, body: RejectRequest,
               current_user: dict = Depends(get_current_user)):
    """
    Reject a run at a HITL gate. Marks it FAILED with the provided reason.
    The Jira issue and inbox item will reflect the rejection.
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_reject")
    from store.sdlc_store import get_run, update_run_state, add_run_event
    from store.inbox_store import publish_inbox_item

    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    is_valid, field_errors, sanitized = validate_sdlc_reject_request(body)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    body.reason = sanitized["reason"]

    state       = run["state"]
    rejected_by = body.rejected_by or "user"
    reason      = body.reason

    _allowed = CODE_APPROVAL_STATES | {"AWAITING_SOLUTION_APPROVAL",
                "AWAITING_PR_APPROVAL", "AWAITING_RE_REVIEW"}
    if state not in _allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Run is in state '{state}' — rejection not applicable."
        )

    add_run_event(run_id, state, "FAILED",
                  actor=rejected_by,
                  output=f"Rejected by {rejected_by}: {reason}")

    update_run_state(run_id, "FAILED",
                     error=f"Rejected by {rejected_by}: {reason}",
                     context_patch={"rejected_by": rejected_by, "rejection_reason": reason})

    # Notify via inbox
    try:
        publish_inbox_item(
            user_id="team",
            type="sdlc_rejected",
            title=f"SDLC Run Rejected — {run.get('jira_key', run_id)}",
            body=f"Rejected at {state} by {rejected_by}: {reason}",
            source_id=run_id,
            metadata={"run_id": run_id, "jira_key": run.get("jira_key", ""), "state": state},
        )
    except Exception:
        pass

    logger.info(f"sdlc/reject: run {run_id} rejected at {state} by {rejected_by}")
    return {
        "run_id":     run_id,
        "action":     "rejected",
        "final_state": "FAILED",
        "reason":     reason,
    }

# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/build-metadata/resolve
# HITL build-metadata gate (Issue 1) — confirm which language version to use
# ─────────────────────────────────────────────────────────────

class BuildMetadataResolveRequest(BaseModel):
    choice: str                              # "detected" | "stored" | "custom"
    chosen_version: Optional[str] = None     # required when choice == "custom"


@router.post("/runs/{run_id}/build-metadata/resolve")
def resolve_build_metadata(run_id: str, body: BuildMetadataResolveRequest,
                           current_user: dict = Depends(get_current_user)):
    """
    Resolve a run paused at AWAITING_BUILD_METADATA_APPROVAL: the operator confirms
    which language version to use for this (product, repo). Enqueues a worker that
    persists the confirmed version to repo_build_metadata, invalidates the resolved
    manifest cache, and re-enters the pipeline at BASELINE.
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_build_metadata_resolve")
    from store.sdlc_store import get_run, add_run_event
    import time as _time

    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    state = run.get("state", "")
    if state != "AWAITING_BUILD_METADATA_APPROVAL":
        raise HTTPException(
            status_code=400,
            detail=f"Run is in state {state!r} — build-metadata resolve only valid at "
                   "AWAITING_BUILD_METADATA_APPROVAL",
        )

    ctx = run.get("context") or {}
    deadline = ctx.get("hitl_deadline")
    if deadline and _time.time() > deadline:
        from store.sdlc_store import update_run_state as _urs
        _urs(run_id, "EXPIRED",
             error="Build-metadata confirmation window expired. Re-trigger the pipeline.")
        raise HTTPException(
            status_code=410,
            detail=f"Confirmation window for run {run_id} expired. Re-trigger the pipeline.",
        )

    _choice = (body.choice or "").strip().lower()
    if _choice not in ("detected", "stored", "custom"):
        raise HTTPException(status_code=400, detail="choice must be one of: detected, stored, custom")
    if _choice == "custom" and not (body.chosen_version or "").strip():
        raise HTTPException(status_code=400, detail="chosen_version is required when choice='custom'")

    _require_rq()
    try:
        from core.job_queue import enqueue_hitl_resume_job
        job_id = enqueue_hitl_resume_job(
            "workers.sdlc_worker.run_build_metadata_resume_job", run_id,
            extra={"choice": _choice, "chosen_version": (body.chosen_version or "").strip()},
        )
    except Exception as exc:
        logger.error(f"sdlc/runs/{run_id}/build-metadata/resolve: enqueue failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Could not resume pipeline: {exc}")

    gate = ctx.get("build_metadata_gate") or {}
    add_run_event(
        run_id, state, "RESUMING_FROM_BUILD_METADATA",
        actor=current_user.get("email", "user"),
        output=f"Build version confirmed (choice={_choice})",
        data={"choice": _choice, "chosen_version": (body.chosen_version or "").strip()},
    )
    logger.info(f"sdlc/build-metadata/resolve: run_id={run_id} choice={_choice} job_id={job_id}")
    return {
        "run_id":           run_id,
        "action":           "resumed",
        "gate_kind":        "build_metadata",
        "choice":           _choice,
        "job_id":           job_id,
        "detected_version": gate.get("detected_version"),
        "stored_version":   gate.get("stored_version"),
        "message":          "Build-metadata confirmation accepted; pipeline resuming at BASELINE.",
    }


# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/cancel
# Cancel any non-terminal run immediately
# ─────────────────────────────────────────────────────────────

@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, body: CancelRequest,
               current_user: dict = Depends(get_current_user)):
    """
    Cancel a running pipeline at any non-terminal state.
    Returns 409 if the run is already in a terminal state.
    """
    from store.sdlc_store import get_run, update_run_state, add_run_event
    from store.inbox_store import publish_inbox_item

    _TERMINAL = {"COMPLETE", "MERGED", "FAILED", "CANCELLED"}

    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    state = run["state"]
    if state in _TERMINAL:
        raise HTTPException(
            status_code=409,
            detail=f"Run is already in terminal state '{state}' — cannot cancel."
        )

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    is_valid, field_errors, sanitized = validate_sdlc_cancel_request(body)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    body.reason = sanitized["reason"]

    cancelled_by = body.cancelled_by
    reason       = body.reason

    add_run_event(run_id, state, "CANCELLED",
                  actor=cancelled_by,
                  output=f"Cancelled by {cancelled_by}: {reason}")
    update_run_state(run_id, "CANCELLED",
                     error=f"Cancelled by {cancelled_by}: {reason}",
                     context_patch={"cancelled_by": cancelled_by, "cancel_reason": reason,
                                    # Bug 5 policy: cleanup_abandoned_governance_mr (called below)
                                    # closes any abandoned MR but keeps the feature branch.
                                    "mr_closed_on_cancel": True})

    # Release the Redis dedup slot so the same Jira can be re-triggered
    # immediately. Compare-and-delete by run_id: we only clear the slot if it
    # still belongs to THIS run, never one a re-triggered run has since claimed.
    # Also decrement the per-reporter counter here — if the job was cancelled
    # before being picked up by a worker the finally block never runs, leaving
    # the counter stuck until TTL. If the worker's finally also runs (in-flight
    # cancel), the decr+delete-if-zero guard makes double-decrement safe.
    try:
        from core.job_queue import release_sdlc_slot
        release_sdlc_slot(
            run.get("jira_key", ""),
            reporter=run.get("triggered_by") or None,
            owner=run_id,
        )
    except Exception as _rel_err:
        logger.warning(f"sdlc/cancel: slot release failed for {run_id}: {_rel_err}")

    # End-gate overhaul (2026-07-23): if this run was parked at the governance
    # end-gate with a DRAFT MR + committed branch, close the abandoned draft MR so
    # it doesn't linger un-mergeable. No-op (fail-safe) when the run has no MR yet.
    #
    # Bug 5 policy: cancelling ANY non-terminal run — including one already past
    # a feature-branch push (e.g. parked at AWAITING_PR_APPROVAL) — closes the
    # abandoned MR but retains the feature branch (recoverable). This helper
    # already defaults to delete_branch=False, so the branch is never deleted
    # here; do not pass delete_branch=True.
    try:
        from agents.sdlc_pipeline import cleanup_abandoned_governance_mr
        cleanup_abandoned_governance_mr(run_id, actor=cancelled_by or "system")
    except Exception as _cleanup_err:
        logger.warning(f"sdlc/cancel: abandoned MR cleanup failed for {run_id}: {_cleanup_err}")

    try:
        publish_inbox_item(
            user_id="team",
            type="sdlc_cancelled",
            title=f"SDLC Run Cancelled — {run.get('jira_key', run_id)}",
            body=f"Cancelled at {state} by {cancelled_by}: {reason}",
            source_id=run_id,
            metadata={"run_id": run_id, "jira_key": run.get("jira_key", ""), "state": state},
        )
    except Exception:
        pass

    logger.info(f"sdlc/cancel: run {run_id} cancelled at {state} by {cancelled_by}")
    return {
        "run_id":      run_id,
        "action":      "cancelled",
        "final_state": "CANCELLED",
        "reason":      reason,
    }


# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/retry-commit
# Retry the COMMITTING phase for a run suspended at COMMIT_FAILED
# ─────────────────────────────────────────────────────────────

@router.post("/runs/{run_id}/retry-commit")
def retry_commit(run_id: str, current_user: dict = Depends(get_current_user)):
    """
    Retry the atomic commit for a run suspended at COMMIT_FAILED.

    The generated code is already durable in the CODING artifact, so this
    re-runs ONLY the COMMITTING phase (branch + atomic batch commit + MR) —
    no earlier stages. The underlying job is idempotent (branch reuse,
    create↔update flips, MR _find_existing_mr), so this is safe to call
    repeatedly; a double-click does not duplicate work.

    Pre-req run state: COMMIT_FAILED.
    Returns 200 {"job_id": ...}.
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_retry_commit")
    from store.sdlc_store import get_run, add_run_event

    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    state = run.get("state", "")
    if state != "COMMIT_FAILED":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Run is in state '{state}' — retry-commit only valid at COMMIT_FAILED. "
                "The commit can only be retried for runs whose commit step failed transiently."
            ),
        )

    actor = (
        current_user.get("sub") or current_user.get("id")
        or current_user.get("email") or "user"
    )
    add_run_event(
        run_id, state, "RETRYING_COMMIT",
        actor=actor,
        output="User requested commit retry — re-running COMMITTING (no earlier stages).",
    )

    # Pipeline continuation MUST run in RQ workers — never in the gateway
    # process (multi-instance state-split safety). Reuse the HITL-resume
    # enqueue path: Q_SDLC, retry_count=0 (double-execution would risk a
    # duplicate MR; the job is idempotent but we still avoid rq auto-retries).
    _require_rq()
    from core.job_queue import enqueue_hitl_resume_job
    try:
        job_id = enqueue_hitl_resume_job("workers.sdlc_worker.retry_commit_job", run_id)
    except RuntimeError as _rq_err:
        raise HTTPException(status_code=429, detail=str(_rq_err))

    logger.info(f"sdlc/retry-commit: run {run_id} commit retry enqueued job_id={job_id} by {actor}")
    return {
        "run_id":  run_id,
        "job_id":  job_id,
        "action":  "retry_commit",
        "message": "Commit retry enqueued — re-running COMMITTING phase only.",
    }


# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/request-changes
# Non-terminating revision request at HITL design/solution gates
# ─────────────────────────────────────────────────────────────

@router.post("/runs/{run_id}/request-changes")
def request_changes(run_id: str, body: RevisionRequest, background_tasks: BackgroundTasks,
                    current_user: dict = Depends(get_current_user)):
    """
    Request changes at a HITL gate without terminating the run.

    Two gate families are supported:
      • Pre-apply code/solution gate (AWAITING_CODE_APPROVAL / legacy
        AWAITING_DESIGN_APPROVAL / AWAITING_SOLUTION_APPROVAL): re-runs the design
        revision loop (max 3 cycles → 409 after that).
      • PR-approval gate (AWAITING_PR_APPROVAL): enqueues the PR-comment
        addressing job (same-run remediation). Uncapped, matching the existing
        PR webhook path.

    Structured per-file feedback (`file_comments=[{file, line?, comment}]`) is
    additive to the whole-run `feedback` string — either or both may be sent.
    Each file comment must carry a non-blank `comment` (mirrors the governance
    send-back rule).
    """
    from store.sdlc_store import get_run, update_run_state, add_run_event
    from store.inbox_store import publish_inbox_item

    # Dual-read: accept the renamed code-approval state AND its legacy alias, plus
    # the bug solution gate and the PR-approval gate.
    _CODE_SOLUTION = CODE_APPROVAL_STATES | {"AWAITING_SOLUTION_APPROVAL"}
    _ALLOWED = _CODE_SOLUTION | {"AWAITING_PR_APPROVAL"}

    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    state      = run["state"]
    revised_by = body.revised_by

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
    # whole-run feedback is XSS-checked free text; each per-file comment is
    # XSS-checked too (the file path itself is left alone here, since it's
    # validated against the run's real file list elsewhere).
    is_valid, field_errors, sanitized = validate_sdlc_revision_request(body)
    if not is_valid and (body.feedback or "").strip():
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    feedback = sanitized.get("feedback", body.feedback or "")

    if state not in _ALLOWED:
        raise HTTPException(
            status_code=400,
            detail=f"Request changes only allowed at AWAITING_CODE_APPROVAL, "
                   f"AWAITING_SOLUTION_APPROVAL or AWAITING_PR_APPROVAL. "
                   f"Current state: '{state}'"
        )

    # ── Validate + normalize structured per-file comments ──────────────
    file_comments = body.file_comments or []
    _normalized_fc = []
    for fc in file_comments:
        if not (fc.comment or "").strip():
            raise HTTPException(
                status_code=422,
                detail=f"Per-file comment for {fc.file!r} must not be blank.",
            )
        if not (fc.file or "").strip():
            raise HTTPException(status_code=422, detail="Per-file comment is missing a file path.")
        _fc_ok, _fc_errs, _fc_val = validate_free_text(fc.comment.strip())
        if not _fc_ok:
            raise HTTPException(
                status_code=400,
                detail=f"Per-file comment for {fc.file!r}: {'; '.join(_fc_errs)}",
            )
        _normalized_fc.append({"file": fc.file, "line": fc.line, "comment": _fc_val})

    if not feedback.strip() and not _normalized_fc:
        raise HTTPException(
            status_code=422,
            detail="Provide whole-run `feedback` and/or at least one non-blank `file_comments` entry.",
        )

    def _render_file_block(entries: list) -> str:
        """Group per-file comments by path into a reviewer-authored block that is
        appended to the free-text feedback so the coder sees file-scoped asks."""
        if not entries:
            return ""
        by_file: dict = {}
        for e in entries:
            by_file.setdefault(e["file"], []).append(e)
        lines = ["", "── Per-file requested changes ──"]
        for path, items in by_file.items():
            lines.append(f"\n### {path}")
            for it in items:
                loc = f" (line {it['line']})" if it.get("line") else ""
                lines.append(f"- {it['comment']}{loc}")
        return "\n".join(lines)

    _file_block = _render_file_block(_normalized_fc)
    # Feedback string that flows to the coder = whole-run feedback + per-file block.
    combined_feedback = (feedback + ("\n" + _file_block if _file_block else "")).strip()

    # ══════════════════════════════════════════════════════════════════
    # PR-APPROVAL gate → same-run PR-comment remediation (uncapped)
    # ══════════════════════════════════════════════════════════════════
    if state == "AWAITING_PR_APPROVAL":
        add_run_event(run_id, state, "PR_CHANGES_REQUESTED", actor=revised_by,
                      output=f"PR changes requested by {revised_by}: {combined_feedback[:500]}")
        update_run_state(run_id, state, context_patch={
            "pr_review_file_comments": _normalized_fc,
            "pr_review_feedback":      feedback,
            "pr_review_requested_by":  revised_by,
        })
        try:
            jira_key = run.get("jira_key", "")
            if jira_key and combined_feedback:
                from tools.jira_tools import jira_add_comment
                _rctx = run.get("context") or {}
                jira_add_comment(jira_key,
                    f"[AiNxt] PR changes requested by {revised_by}:\n{combined_feedback}",
                    user_id=_rctx.get("user_id", ""), user_email=_rctx.get("user_email", ""))
        except Exception:
            pass
        from core.job_queue import enqueue_pr_comments_job
        job_id = enqueue_pr_comments_job(run_id)
        logger.info("sdlc/request-changes: PR-gate per-file request accepted",
                    run_id=run_id, state=state,
                    file_comment_count=len(_normalized_fc), revision_count=0)
        return {
            "run_id":             run_id,
            "action":             "pr_changes_requested",
            "file_comment_count": len(_normalized_fc),
            "job_id":             job_id,
            "message":            "PR review comments enqueued for AI addressing (same run).",
        }

    # ══════════════════════════════════════════════════════════════════
    # Pre-apply code/solution gate → design revision loop (max 3)
    # ══════════════════════════════════════════════════════════════════
    revision_count = run["context"].get("revision_count", 0) + 1
    if revision_count > 3:
        raise HTTPException(
            status_code=409,
            detail="Max revisions (3) reached. Use Reject (Terminate) to end this run."
        )

    add_run_event(run_id, state, "REVISION_REQUESTED",
                  actor=revised_by,
                  output=f"Revision #{revision_count} requested by {revised_by}: {combined_feedback}")
    update_run_state(run_id, "REVISION_REQUESTED",
                     context_patch={
                         "revision_feedback":      combined_feedback,
                         "revision_file_comments": _normalized_fc,
                         "revised_by":             revised_by,
                         "revision_count":         revision_count,
                     })

    # Post Jira comment with feedback
    try:
        jira_key = run.get("jira_key", "")
        if jira_key:
            from tools.jira_tools import jira_add_comment
            _rctx = run.get("context") or {}
            jira_add_comment(jira_key,
                f"[AiNxt] Revision #{revision_count} requested by {revised_by}:\n{combined_feedback}",
                user_id=_rctx.get("user_id", ""), user_email=_rctx.get("user_email", ""))
    except Exception:
        pass

    # Inbox notification
    try:
        publish_inbox_item(
            user_id="team",
            type="sdlc_revision_requested",
            title=f"SDLC Revision #{revision_count} — {run.get('jira_key', run_id)}",
            body=f"Revision requested at {state} by {revised_by}: {combined_feedback}",
            source_id=run_id,
            metadata={"run_id": run_id, "jira_key": run.get("jira_key", ""),
                      "revision_count": revision_count, "feedback": combined_feedback,
                      "file_comment_count": len(_normalized_fc)},
        )
    except Exception:
        pass

    # Enqueue revision task into RQ — never in the gateway process.
    # combined_feedback (whole-run + per-file block) is what the coder receives.
    from core.job_queue import enqueue_hitl_resume_job
    if state in CODE_APPROVAL_STATES:
        enqueue_hitl_resume_job("workers.sdlc_worker.resume_feature_revision_job", run_id, combined_feedback)
    else:
        enqueue_hitl_resume_job("workers.sdlc_worker.resume_bug_revision_job", run_id, combined_feedback)

    logger.info("sdlc/request-changes: per-file request-changes accepted",
                run_id=run_id, state=state,
                file_comment_count=len(_normalized_fc), revision_count=revision_count)
    return {
        "run_id":          run_id,
        "action":          "revision_requested",
        "revision_count":  revision_count,
        "revisions_left":  3 - revision_count,
        "file_comment_count": len(_normalized_fc),
        "message":         f"Revision #{revision_count} started. Pipeline will return to {state}.",
    }


# ─────────────────────────────────────────────────────────────
# GET /sdlc/runs/{run_id}/events
# Event audit trail for a run
# ─────────────────────────────────────────────────────────────

@router.get("/runs/{run_id}/events")
def get_run_events(run_id: str, current_user: dict = Depends(get_current_user)):
    """Return the full ordered event/audit trail for a run."""
    from store.sdlc_store import get_run, get_run_events
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)
    events = get_run_events(run_id)
    return {"run_id": run_id, "events": events, "total": len(events)}


@router.get("/runs/{run_id}/replay")
def get_run_replay(run_id: str, current_user: dict = Depends(get_current_user)):
    """Return the LLM replay log for a run (prompt hashes + previews for each phase call)."""
    from store.sdlc_store import get_run
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)
    import json as _json
    from core.config import REDIS_HOST as _H, REDIS_PORT as _P
    try:
        import redis as _r
        rc      = _r.Redis(host=_H, port=_P, db=2, decode_responses=True, socket_connect_timeout=2)
        entries = rc.lrange(f"sdlc:replay:{run_id}", 0, -1)
        parsed  = []
        for e in entries:
            try:
                parsed.append(_json.loads(e))
            except Exception:
                parsed.append({"raw": e})
        return {"run_id": run_id, "replay": parsed, "total": len(parsed)}
    except Exception as _e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {_e}")


@router.get("/runs/{run_id}/confidence")
def get_run_confidence(run_id: str, current_user: dict = Depends(get_current_user)):
    """Return the confidence aggregation score for a completed run."""
    from store.sdlc_store import get_run
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)
    ctx = run.get("context") or {}
    score   = ctx.get("final_confidence")
    detail  = ctx.get("confidence_detail")
    if score is None:
        return {"run_id": run_id, "final_confidence": None,
                "message": "Confidence not yet computed (run may still be in progress)"}
    return {"run_id": run_id, "final_confidence": score, "detail": detail}


# ─────────────────────────────────────────────────────────────
# POST /sdlc/brd-fsd/{epic_key}/approve
# HITL approval gate for the BRD→FSD pipeline
# ─────────────────────────────────────────────────────────────

class BRDApprovalRequest(BaseModel):
    note:        Optional[str] = ""       # optional reviewer notes / instructions
    approved_by: Optional[str] = "user"


@router.post("/brd-fsd/{epic_key}/approve")
def approve_brd_fsd_endpoint(
        epic_key: str,
        body: BRDApprovalRequest,
        background_tasks: BackgroundTasks,
        current_user: dict = Depends(get_current_user),
):
    """
    Human-in-the-loop approval gate for the BRD→FSD pipeline.

    After the pipeline runs and generates an FSD, it pauses here waiting
    for a human reviewer to approve before publishing to Confluence and
    creating Jira stories.

    Triggers:
      - Confluence page creation ("FSD: {epic_summary}")
      - Jira Story creation for each user story in the FSD
      - Inbox notification on completion

    Poll the response or the inbox for status.
    """
    from agents.brd_fsd_pipeline import approve_brd_fsd

    note        = body.note or ""
    approved_by = body.approved_by or "user"

    logger.info(f"sdlc/brd-fsd/approve: HITL approval for epic={epic_key} by={approved_by}")

    _approver_id    = current_user.get("id") or current_user.get("sub", "")
    _approver_email = current_user.get("email", "")

    def _bg_approve():
        try:
            result = approve_brd_fsd(epic_key=epic_key, note=note,
                                     user_id=_approver_id, user_email=_approver_email)
            logger.info(
                f"sdlc/brd-fsd/approve: approval complete for {epic_key} "
                f"status={result.get('status')}"
            )
        except Exception as e:
            logger.error(f"sdlc/brd-fsd/approve: background approval failed for {epic_key}: {e}")

    # Check if there is a pending state before kicking off background task
    from agents.brd_fsd_pipeline import _hitl_state
    if epic_key not in _hitl_state:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No pending BRD→FSD pipeline found for epic '{epic_key}'. "
                "Ensure the BRD→FSD pipeline has been triggered first "
                "(POST /webhooks/jira with a BRD-labelled Epic, or run manually)."
            ),
        )

    pending = _hitl_state[epic_key]
    if pending.get("status") == "approved":
        return {
            "epic_key":        epic_key,
            "action":          "already_approved",
            "confluence_url":  pending.get("confluence_url", ""),
            "stories_created": pending.get("stories_created", []),
            "hitl_status":     "approved",
            "message":         "BRD→FSD pipeline was already approved.",
        }

    # Kick off Confluence publish + Jira story creation in background
    background_tasks.add_task(_bg_approve)

    return {
        "epic_key":    epic_key,
        "action":      "approved",
        "approved_by": approved_by,
        "hitl_status": "processing",
        "message":     (
            f"BRD→FSD approval accepted for Epic {epic_key}. "
            "Confluence page and Jira stories are being created in the background. "
            "Check the inbox or poll /sdlc/brd-fsd/{epic_key}/status for completion."
        ),
    }


# ─────────────────────────────────────────────────────────────
# GET /sdlc/brd-fsd/{epic_key}/status
# Check the current state of a BRD→FSD pipeline run
# ─────────────────────────────────────────────────────────────

@router.get("/brd-fsd/{epic_key}/status")
def brd_fsd_status(epic_key: str, current_user: dict = Depends(get_current_user)):
    """
    Return the current state of the BRD→FSD pipeline for a given Epic key.

    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    this endpoint previously had no auth dependency at all, exposing
    internal HITL approval state and Confluence links for any epic to any
    anonymous caller.
    Fix: added `current_user: dict = Depends(get_current_user)` as a
    function parameter so FastAPI rejects any request without a valid JWT
    (Bearer token / auth_token cookie / platform API key) with 401 before
    the handler body runs. No change to the response shape or the query
    logic below — any authenticated user can still call this exactly as
    before.
    """
    from agents.brd_fsd_pipeline import _hitl_state

    state = _hitl_state.get(epic_key)
    if not state:
        raise HTTPException(
            status_code=404,
            detail=f"No BRD→FSD pipeline state found for epic '{epic_key}'.",
        )

    return {
        "epic_key":        epic_key,
        "hitl_status":     state.get("status", "unknown"),
        "hitl_id":         state.get("hitl_id", ""),
        "confluence_url":  state.get("confluence_url", ""),
        "stories_created": state.get("stories_created", []),
        "created_at":      state.get("created_at", ""),
        "approved_at":     state.get("approved_at", ""),
    }


# ─────────────────────────────────────────────────────────────
# GET /sdlc/stats
# Quick stats dashboard
# ─────────────────────────────────────────────────────────────

@router.get("/stats")
def get_stats(current_user: dict = Depends(get_current_user)):
    """Return aggregate stats: counts by state and type.

    Uses SQL `GROUP BY` so the JSONB `context` column is never read or
    deserialized — counts only.

    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    this endpoint previously had no auth dependency at all, exposing
    internal SDLC run volume/state metrics to any anonymous caller.
    Fix: added `current_user: dict = Depends(get_current_user)` as a
    function parameter so FastAPI enforces a valid JWT before the handler
    runs; the aggregate query and response are unchanged.
    """
    from store.sdlc_store import _get_session, _runs

    session = _get_session()
    if session:
        try:
            from db.models import SDLCRun
            from sqlalchemy import func
            state_rows = (
                session.query(SDLCRun.state, func.count(SDLCRun.id))
                .group_by(SDLCRun.state)
                .all()
            )
            type_rows = (
                session.query(SDLCRun.type, func.count(SDLCRun.id))
                .group_by(SDLCRun.type)
                .all()
            )
            by_state = {s or "UNKNOWN": int(c) for s, c in state_rows}
            by_type  = {t or "unknown": int(c) for t, c in type_rows}
            return {
                "total":    sum(by_state.values()),
                "by_state": by_state,
                "by_type":  by_type,
            }
        except Exception as e:
            from core.logger import logger
            logger.warning(f"sdlc/stats: SQL aggregation failed → {e}")
        finally:
            session.close()

    # In-memory fallback (DB unavailable)
    by_state: dict = {}
    by_type:  dict = {}
    for r in _runs.values():
        s = r.get("state", "UNKNOWN")
        t = r.get("type", "unknown")
        by_state[s] = by_state.get(s, 0) + 1
        by_type[t]  = by_type.get(t, 0) + 1
    return {
        "total":    len(_runs),
        "by_state": by_state,
        "by_type":  by_type,
    }


# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/resume
# Generic flexible-pipeline resume (retry / go_back / override / waive)
# ─────────────────────────────────────────────────────────────

class ResumeRequest(BaseModel):
    target_stage:     str
    mode:             str                   # retry | go_back | override | waive
    feedback:         Optional[str] = None
    reason:           Optional[str] = None  # required for waive + override
    override_payload: Optional[dict] = None # for mode=override only


@router.post("/runs/{run_id}/resume")
def resume_run(
    run_id: str,
    body: ResumeRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Resume a suspended (or completed) pipeline run from any stage.
    Modes: retry | go_back | override | waive.
    Returns {job_id, cascade_preview}.
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_resume")
    from store.sdlc_store import get_run as _get_run
    _run = _get_run(run_id)
    if not _run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(_run, current_user)
    try:
        from agents.sdlc_pipeline import resume_from_stage
        result = resume_from_stage(
            run_id=run_id,
            target_stage=body.target_stage,
            mode=body.mode,
            feedback=body.feedback,
            override_payload=body.override_payload,
            actor=current_user.get("sub") or current_user.get("id") or current_user.get("email") or "unknown",
            reason=body.reason,
            jwt_claims=current_user,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error(f"sdlc/resume: unexpected error for {run_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/baseline/resume
# Resume a run SUSPENDED at BASELINE_BUILD by re-entering the FULL pipeline.
# NOT a stage-resume (BASELINE_BUILD runs in _preflight_check, before any
# artifact-backed stage) — see agents.sdlc_pipeline.retrigger_pipeline.
# ─────────────────────────────────────────────────────────────

class BaselineResumeRequest(BaseModel):
    skip_compile: bool           = False   # True = "Skip compilation & continue" — bypass build everywhere
    skip_tests:   Optional[bool] = None    # None = keep stored value; True = explicit opt-out of tests+SLT


@router.post("/runs/{run_id}/baseline/resume")
def resume_baseline_build(
    run_id: str,
    body: BaselineResumeRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Resume a run suspended at BASELINE_BUILD.

    - default: the operator pushed a repo fix — re-run preflight so the baseline
      gate rebuilds HEAD and (if green) the pipeline proceeds.
    - skip_compile=True: proceed WITHOUT compiling — the baseline gate is bypassed
      and every downstream compile point (build-check, dep install, test loop) is
      skipped for this run. A waiver banner records that the build was not verified.
    - skip_tests=True: explicit user opt-out of TESTING+SLT for this run resume. Must
      be a deliberate human action at the suspended panel — never set automatically by
      the backend. skip_tests=None (default) leaves the stored run context value unchanged.

    Auth is the same as the trigger routes (run owner re-running their own build);
    no can_approve needed. Returns {job_id}.
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_baseline_resume")
    from store.sdlc_store import get_run as _get_run
    _run = _get_run(run_id)
    if not _run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(_run, current_user)
    try:
        from agents.sdlc_pipeline import retrigger_pipeline
        result = retrigger_pipeline(
            run_id,
            skip_compile=bool(body.skip_compile),
            skip_tests=body.skip_tests,  # None = keep stored; True/False = explicit override
            actor=current_user.get("sub") or current_user.get("id") or current_user.get("email") or "unknown",
            jwt_claims=current_user,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        # enqueue admission rejection (rate-limit / dedup) — mirror the trigger routes
        raise HTTPException(status_code=429, detail=str(e))
    except Exception as e:
        logger.error(f"sdlc/baseline/resume: unexpected error for {run_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────
# GET /sdlc/runs/{run_id}/stages
# List all stage artifacts for a run (for the UI stage timeline)
# ─────────────────────────────────────────────────────────────

@router.get("/runs/{run_id}/stages")
def list_run_stages(
    run_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Return all stage artifacts for a run, ordered by stage DAG position.
    Each entry: {stage, version, status, score, producer, created_at}.
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_stages")
    from store.sdlc_store import get_run
    from store.sdlc_artifacts import STAGE_DAG
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    try:
        from db.database import SessionLocal
        from sqlalchemy import text
        session = SessionLocal()
        try:
            rows = session.execute(
                text(
                    "SELECT stage, version, status, score, producer, created_at, reason "
                    "FROM sdlc_stage_artifacts "
                    "WHERE run_id = :r "
                    "ORDER BY created_at ASC"
                ),
                {"r": run_id},
            ).fetchall()
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"sdlc/stages: DB query failed for {run_id}: {e}")
        rows = []

    # Index by stage — keep latest version per stage
    by_stage: dict = {}
    for row in rows:
        s = row.stage
        if s not in by_stage or row.version > by_stage[s]["version"]:
            by_stage[s] = {
                "stage":      s,
                "version":    row.version,
                "status":     row.status,
                "score":      row.score,
                "producer":   row.producer,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "reason":     row.reason,
            }

    # Return in DAG order
    ordered = [by_stage[s] for s in STAGE_DAG if s in by_stage]
    return ordered


# ─────────────────────────────────────────────────────────────
# GET /sdlc/runs/{run_id}/stages/{stage}/artifact
# Fetch the latest artifact payload for a specific stage
# ─────────────────────────────────────────────────────────────

@router.get("/runs/{run_id}/stages/{stage}/artifact")
def get_stage_artifact(
    run_id: str,
    stage: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Return the latest artifact for (run_id, stage).
    Includes the full payload, input_hash, producer, reason.
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_artifact")
    from store.sdlc_store import get_run
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    from store.sdlc_artifacts import _load_latest_artifact
    artifact = _load_latest_artifact(run_id, stage)
    if artifact is None:
        raise HTTPException(
            status_code=404,
            detail=f"No artifact found for run={run_id} stage={stage!r}",
        )
    return artifact


@router.get("/runs/{run_id}/verified-diff")
def get_verified_diff(
    run_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Return the VERIFIED_DIFF artifact the human approves at the relocated HITL
    gate ("decide before the gate"): the real per-file edits + compile/test status
    + base_sha, plus any compile-waiver banners so the reviewer sees that the
    compile gate was waived BEFORE approving."""
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_verified_diff")
    from store.sdlc_store import get_run
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    from store.sdlc_artifacts import _load_latest_artifact
    artifact = _load_latest_artifact(run_id, "VERIFIED_DIFF")
    if artifact is None:
        raise HTTPException(
            status_code=404,
            detail=f"No verified diff for run={run_id} — pre-gate codegen has not completed",
        )
    ctx = (run.get("context") or {}) if isinstance(run.get("context"), dict) else {}
    payload = artifact.get("payload") or {}
    return {
        "run_id": run_id,
        "state": run.get("state"),
        "verified_diff": payload,
        "waiver_banners": list(ctx.get("waiver_banners") or []),
    }


# ─────────────────────────────────────────────────────────────
# GET /sdlc/pipeline-manifest?type=feature|bug
# Canonical, backend-owned stage manifest the UI renders its
# timeline from (single source of truth — kills the 3-way drift).
# ─────────────────────────────────────────────────────────────

@router.get("/pipeline-manifest")
def get_pipeline_manifest(
    type: str = Query(default="feature", description="feature | bug"),
    current_user: dict = Depends(get_current_user),
):
    """
    Return the ordered stage manifest for a run type plus a legacy/transient
    `aliases` map. The UI maps `run.state`/`current_stage` onto a manifest node
    (direct match, then alias) and renders the timeline from this — no hardcoded
    stage arrays in the frontend.

    Honest about the live shape: when `SDLC_PLANNER_MODE=merged` the feature
    manifest has a single `PLAN` node; when split it has separate `ANALYZE` +
    `DESIGN` nodes (read from the same flag the backend pipeline reads).
    """
    run_type = (type or "feature").strip().lower()
    if run_type not in ("feature", "bug", "pr_review", "governance"):
        logger.warning(f"sdlc/pipeline-manifest: unknown run_type requested — {run_type!r}")
    from store.sdlc_stage_manifest import pipeline_manifest
    manifest = pipeline_manifest(run_type)
    logger.info(
        "[SDLC-MANIFEST] manifest served",
        run_type=manifest.get("run_type"),
        node_count=len(manifest.get("nodes") or []),
        planner_mode=manifest.get("planner_mode"),
    )
    return manifest


# NOTE: The per-run SDLC metrics endpoint (GET /sdlc/runs/{run_id}/metrics) and
# its backing module agents/sdlc_metrics.py were removed (2026-07-08). The
# three-phase engine's metrics surface is being reimagined; the old analytics
# (RunMetricsPanel / RunMetricsDashboard / ConvergencePanel) were torn out.


# ─────────────────────────────────────────────────────────────
# POST /sdlc/governance-review
# STEP 9 (2026-07-17) — standalone EA/IS/DPDP governance review, decoupled
# from the SDLC pipeline. Two mutually exclusive modes:
#   run_id mode : diff read from the run's VERIFIED_DIFF artifact/workspace.
#   repo mode   : diff read from a GitLab MR (mr_iid) or branch (ref/branch),
#                 no sdlc_runs row required.
# Report-first: never suspends a pipeline. Enqueued via the SAME sdlc_queue
# path (enqueue_sdlc_job) other SDLC jobs use, so the existing dedup +
# per-reporter rate-limit guards apply here too (deliberate reuse — see
# core/job_queue.py::enqueue_sdlc_job).
# ─────────────────────────────────────────────────────────────

class GovernanceReviewRequest(BaseModel):
    run_id:             Optional[str] = None
    repo:               Optional[str] = None
    ref:                Optional[str] = None   # branch name or commit sha
    branch:             Optional[str] = None   # alias for ref
    mr_iid:             Optional[int] = None
    auto_fix:           bool = True
    governance_skills:  Optional[list[str]] = None
    product_id:         Optional[str] = None


@router.post("/governance-review")
def trigger_governance_review(
    req: GovernanceReviewRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Manually trigger a standalone governance review. Returns the job_id
    immediately; the review runs in an sdlc_queue RQ worker.
    """
    if bool(req.run_id) == bool((req.repo or "").strip()):
        # Exactly one of {run_id} OR {repo(+ref/mr_iid)} — both-or-neither is a 422.
        raise HTTPException(
            status_code=422,
            detail="Provide exactly one of 'run_id' OR 'repo' (with 'ref'/'branch' or 'mr_iid').",
        )
    _ref = (req.ref or req.branch or "").strip()
    if req.repo and not (_ref or req.mr_iid):
        raise HTTPException(
            status_code=422,
            detail="'repo' mode requires 'ref' (or 'branch') or 'mr_iid'.",
        )

    # run_id mode: same IDOR guard as every other /sdlc/runs/{id}/* action —
    # 404 (not 403) on a visibility miss.
    if req.run_id:
        from store.sdlc_store import get_run
        _run = get_run(req.run_id)
        if not _run:
            raise HTTPException(status_code=404, detail=f"Run {req.run_id} not found")
        _authorize_run(_run, current_user)

    _require_rq()
    from core.job_queue import enqueue_sdlc_job
    payload = {
        "run_id":               req.run_id,
        "repo":                 req.repo,
        "ref":                  _ref,
        "mr_iid":               req.mr_iid,
        "auto_fix":             bool(req.auto_fix),
        "governance_skills":    req.governance_skills,
        "product_id":           req.product_id,
        "triggered_by_user_id": current_user.get("id") or current_user.get("sub", ""),
        "triggered_by_email":   current_user.get("email", ""),
        # "key" only feeds enqueue_sdlc_job's Jira-ticket dedup/rate-limit guard —
        # not a real Jira issue. Scoped so identical concurrent requests collapse
        # without colliding across different runs/repos/MRs.
        "key": req.run_id or f"gov:{req.repo}:{req.mr_iid or _ref or 'norefs'}",
    }
    try:
        job_id = enqueue_sdlc_job("workers.sdlc_worker.run_governance_review_job", payload)
    except RuntimeError as _rq_err:
        raise HTTPException(status_code=429, detail=str(_rq_err))

    logger.info(
        "[SDLC-GOV] governance-review triggered",
        run_id=req.run_id, repo=req.repo, mr_iid=req.mr_iid, auto_fix=req.auto_fix,
        job_id=job_id,
    )
    return {"job_id": job_id, "run_id": req.run_id, "repo": req.repo, "message": "Governance review enqueued"}


# ─────────────────────────────────────────────────────────────
# Governance suppressions CRUD + report read (STEP 10, 2026-07-17)
# ─────────────────────────────────────────────────────────────

class GovernanceSuppressionRequest(BaseModel):
    product_id:  Optional[str] = None
    repo:        str
    skill:       str
    fingerprint: str
    rule:        Optional[str] = None
    reason:      Optional[str] = None


class GovernanceBulkSuppressionItem(BaseModel):
    # The real skill SLUG — required, it is the matcher key alongside the fingerprint.
    skill:       str
    # Either a precomputed gv1: fingerprint …
    fingerprint: Optional[str] = None
    # … OR a raw tuple to fingerprint here (file + rule + snippet|title) via
    # agents.sdlc_governance.schema.fingerprint (line-independent gv1 scheme).
    file:        Optional[str] = None
    rule:        Optional[str] = None
    snippet:     Optional[str] = None
    title:       Optional[str] = None
    reason:      Optional[str] = None


class GovernanceBulkSuppressionRequest(BaseModel):
    repo:        str
    product_id:  Optional[str] = None
    source:      Optional[str] = "uploaded"
    items:       list = []


def _gov_suppression_visible(row, current_user: dict) -> bool:
    """Scope a suppression row the same way a run is scoped — reuses
    store.sdlc_store.run_visible_to_user with a synthetic run dict, so a
    suppression is visible to its creator, to admins, and to anyone in a
    department mapped (product_repos ⋈ dept_product_mappings) to the
    suppression's repo. Fails closed (not visible) on any error."""
    from store.sdlc_store import run_visible_to_user
    try:
        synthetic_run = {"repo": row.repo_name, "created_by": row.created_by}
        return run_visible_to_user(synthetic_run, **_user_scope(current_user))
    except Exception as e:
        logger.warning(f"[SDLC-GOV] suppression visibility check failed: {e}")
        return False


def _gov_user_owns_run_for_repo(repo: str, current_user: dict) -> bool:
    """True iff the caller CREATED at least one SDLC run for `repo` (normalized
    match). run_visible_to_user only credits ownership of a specific stored
    row's created_by — it can't answer "did I ever run this repo" — so this is
    the explicit run-ownership arm of the author suppression scope gate.
    Fail-closed (deny) on any error."""
    repo = (repo or "").strip()
    owner_ids = _user_scope(current_user).get("owner_ids") or []
    if not (repo and owner_ids):
        return False
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        from db.models import SDLCRun
        from store.sdlc_store import _norm_repo, _norm_repo_sql
        row = (
            db.query(SDLCRun.id)
            .filter(SDLCRun.created_by.in_(owner_ids),
                    _norm_repo_sql(SDLCRun.repo) == _norm_repo(repo))
            .first()
        )
        return row is not None
    except Exception as e:
        logger.warning("[SDLC-GOV] run-ownership scope check failed — fail-closed", error=str(e))
        return False
    finally:
        db.close()


def _gov_author_in_scope(repo: str, product_id, current_user: dict) -> bool:
    """Scope gate for a NON-privileged author creating/uploading a suppression.

    ── SCOPE-CHECK DECISION (needs review) ──────────────────────────────────
    An ordinary author (not admin, not governance lead) may suppress findings
    ONLY for a repo/product they legitimately own. "Own" is established two
    ways, whichever passes first; fail CLOSED (deny) on any error — mirroring
    auth.rbac.can_approve_domain — because this gate is what stops an author
    pre-suppressing ANOTHER team's real PCI/DSS findings:

      1. The repo is visible to the caller via the SAME product/department ACL
         that scopes every /sdlc run and product
         (store.sdlc_store.run_visible_to_user ⋈ dept_product_mappings). This is
         the existing canonical visibility helper — reused verbatim (through a
         synthetic run with created_by=None so ONLY the dept-mapping arm can
         pass here) so suppression scope can never drift from run/product scope.
      2. The caller CREATED an SDLC run for this repo (created_by ∈ their ids) —
         the explicit run-ownership arm above.

    If neither holds → DENY. product_id is accepted for signature symmetry /
    future product-level scoping and logging; the authoritative unit today is
    the (single) repo, per the single-repo SDLC constraints.
    """
    repo = (repo or "").strip()
    if not repo:
        return False
    try:
        from store.sdlc_store import run_visible_to_user
        # (1) product/department visibility — created_by=None ⇒ only the
        #     dept-mapping arm can pass (owner-of-run handled in (2)).
        if run_visible_to_user({"repo": repo, "created_by": None},
                               **_user_scope(current_user)):
            return True
        # (2) author created a run for this repo
        return _gov_user_owns_run_for_repo(repo, current_user)
    except Exception as e:
        logger.warning("[SDLC-GOV] author scope check failed — fail-closed", error=str(e))
        return False


@router.post("/governance-suppressions")
def create_governance_suppression(
    body: GovernanceSuppressionRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Upsert an ACTIVE suppression for a (product, repo, skill, fingerprint) tuple.

    NOTE: product_id NULL rows do not de-dup via ON CONFLICT — Postgres treats
    NULLs as DISTINCT in a UNIQUE constraint, so an unmapped repo can accumulate
    duplicate NULL-product suppression rows for the same (repo, skill,
    fingerprint). Accepted as-is per the plan: the suppression FILTER
    (agents.sdlc_governance.engine.apply_suppressions) matches by tuple
    regardless of duplicate rows, so this is cosmetic table bloat, not a
    correctness bug — not fixed here.
    """
    import uuid as _uuid_mod
    created_by = (
        current_user.get("email") or current_user.get("sub")
        or current_user.get("id") or "unknown"
    )

    # B3.1 privilege gate (closes a real hole: previously ANY authenticated user
    # could suppress ANY repo's findings). Admins + governance leads are
    # unrestricted; an ordinary author may suppress ONLY within their own
    # repo/product scope. Fail-closed.
    from auth.rbac import can_manage_suppression
    if not can_manage_suppression(current_user):
        if not _gov_author_in_scope(body.repo, body.product_id, current_user):
            logger.warning(
                "[SDLC-GOV] create-suppression denied (out of scope)",
                actor=created_by, repo=body.repo, product_id=body.product_id,
            )
            raise HTTPException(status_code=403,
                                detail="Not authorized to suppress findings for this repo/product")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    is_valid, field_errors, sanitized = validate_governance_suppression_request(body)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    body.skill = sanitized["skill"]
    body.rule = sanitized["rule"] or None
    body.reason = sanitized["reason"]

    from db.database import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        db.execute(
            text(
                "INSERT INTO sdlc_governance_suppressions "
                "(id, product_id, repo_name, skill, fingerprint, rule, reason, created_by, active, created_at) "
                "VALUES (:id, :pid, :repo, :skill, :fp, :rule, :reason, :created_by, TRUE, NOW()) "
                "ON CONFLICT (product_id, repo_name, skill, fingerprint) DO UPDATE SET "
                "active = TRUE, rule = EXCLUDED.rule, reason = EXCLUDED.reason, created_by = EXCLUDED.created_by"
            ),
            {
                "id": str(_uuid_mod.uuid4()), "pid": body.product_id, "repo": body.repo,
                "skill": body.skill, "fp": body.fingerprint, "rule": body.rule,
                "reason": body.reason, "created_by": created_by,
            },
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[SDLC-GOV] governance-suppression upsert failed: {e}")
        raise HTTPException(status_code=500, detail=f"Suppression upsert failed: {e}")
    finally:
        db.close()

    logger.info(
        "[SDLC-GOV] suppression created",
        product_id=body.product_id, repo=body.repo, skill=body.skill,
        fingerprint=body.fingerprint, created_by=created_by,
    )
    return {
        "product_id": body.product_id, "repo": body.repo, "skill": body.skill,
        "fingerprint": body.fingerprint, "active": True, "created_by": created_by,
    }


@router.get("/governance-suppressions")
def list_governance_suppressions(
    repo:       Optional[str] = Query(default=None),
    product_id: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """List active suppressions, optionally filtered by repo/product_id, scoped
    to the caller the same way /sdlc/runs is (own + department + admin-sees-all)."""
    from db.database import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        clauses = ["active = TRUE"]
        params: dict = {}
        if repo:
            clauses.append("repo_name = :repo")
            params["repo"] = repo
        if product_id:
            clauses.append("product_id = :pid")
            params["pid"] = product_id
        rows = db.execute(
            text(
                "SELECT id, product_id, repo_name, skill, fingerprint, rule, reason, "
                "created_by, active, created_at, source, pending_signoff, "
                "signed_off_by, signed_off_at FROM sdlc_governance_suppressions "
                f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC"
            ),
            params,
        ).fetchall()
    except Exception as e:
        logger.warning(f"[SDLC-GOV] list_governance_suppressions query failed: {e}")
        rows = []
    finally:
        db.close()

    is_admin = current_user.get("role") == "admin"
    visible = [r for r in rows if is_admin or _gov_suppression_visible(r, current_user)]
    return {
        "suppressions": [
            {
                "id":          str(r.id),
                "product_id":  str(r.product_id) if r.product_id else None,
                "repo":        r.repo_name,
                "skill":       r.skill,
                "fingerprint": r.fingerprint,
                "rule":        r.rule,
                "reason":      r.reason,
                "created_by":  r.created_by,
                "active":      r.active,
                "created_at":  r.created_at.isoformat() if r.created_at else None,
                # V6 end-gate overhaul fields (bulk FP provenance + signoff gate)
                "source":          getattr(r, "source", None),
                "pending_signoff": getattr(r, "pending_signoff", False),
                "signed_off_by":   getattr(r, "signed_off_by", None),
                "signed_off_at":   r.signed_off_at.isoformat() if getattr(r, "signed_off_at", None) else None,
            }
            for r in visible
        ],
        "total": len(visible),
    }


@router.delete("/governance-suppressions/{suppression_id}")
def delete_governance_suppression(
    suppression_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Soft-delete (active=FALSE) a suppression. IDOR-guarded the same way as
    /sdlc/runs/{id} — 404 (not 403) on a visibility miss. Not explicitly called
    out in the plan for DELETE, but applied here for consistency with every
    other governance-suppression/run endpoint (conservative choice)."""
    from db.database import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        row = db.execute(
            text(
                "SELECT id, product_id, repo_name, skill, fingerprint, created_by "
                "FROM sdlc_governance_suppressions WHERE id = :id"
            ),
            {"id": suppression_id},
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Suppression not found")
        is_admin = current_user.get("role") == "admin"
        if not is_admin and not _gov_suppression_visible(row, current_user):
            raise HTTPException(status_code=404, detail="Suppression not found")

        db.execute(
            text("UPDATE sdlc_governance_suppressions SET active = FALSE WHERE id = :id"),
            {"id": suppression_id},
        )
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"[SDLC-GOV] delete_governance_suppression failed: {e}")
        raise HTTPException(status_code=500, detail=f"Suppression delete failed: {e}")
    finally:
        db.close()

    logger.info("[SDLC-GOV] suppression soft-deleted", suppression_id=suppression_id,
                actor=current_user.get("email") or current_user.get("sub") or "unknown")
    return {"id": suppression_id, "active": False}


@router.post("/governance-suppressions/bulk")
def bulk_upload_governance_suppressions(
    body: GovernanceBulkSuppressionRequest,
    current_user: dict = Depends(get_current_user),
):
    """B3.1 — bulk false-positive upload. Each item is EITHER a precomputed gv1:
    fingerprint OR a raw tuple (file/rule/snippet|title) fingerprinted here via
    the line-independent gv1 scheme. Uploaded rows land source='uploaded',
    pending_signoff=TRUE — INERT until a governance lead signs them off (the
    matcher ignores pending rows), so an upload can never make a real finding
    disappear without an explicit sign-off.

    AuthZ: admins + governance leads unrestricted; an ordinary author may upload
    ONLY within their own repo/product scope (same gate as create). Fail-closed.
    """
    actor = (
        current_user.get("email") or current_user.get("sub")
        or current_user.get("id") or "unknown"
    )

    from auth.rbac import can_manage_suppression
    if not can_manage_suppression(current_user):
        if not _gov_author_in_scope(body.repo, body.product_id, current_user):
            logger.warning(
                "[SDLC-GOV] create-suppression denied (out of scope)",
                actor=actor, repo=body.repo, product_id=body.product_id,
            )
            raise HTTPException(status_code=403,
                                detail="Not authorized to suppress findings for this repo/product")

    from agents.sdlc_governance.schema import Finding, fingerprint as _compute_fp

    source = (body.source or "uploaded").strip() or "uploaded"
    rows: list = []
    for item in (body.items or []):
        # Pydantic v1/v2-safe: items may arrive as dicts (list field, not typed).
        if isinstance(item, dict):
            skill = (item.get("skill") or "").strip()
            fp = (item.get("fingerprint") or "").strip()
            _file = item.get("file"); _rule = item.get("rule")
            _snippet = item.get("snippet"); _title = item.get("title")
            _reason = item.get("reason")
        else:
            skill = (getattr(item, "skill", "") or "").strip()
            fp = (getattr(item, "fingerprint", "") or "").strip()
            _file = getattr(item, "file", None); _rule = getattr(item, "rule", None)
            _snippet = getattr(item, "snippet", None); _title = getattr(item, "title", None)
            _reason = getattr(item, "reason", None)

        if not skill:
            logger.warning("[SDLC-GOV] bulk upload: skipping item without skill slug",
                           actor=actor, repo=body.repo)
            continue

        # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
        # each bulk item's skill/rule/reason gets the same check as the
        # single-item /governance-suppressions endpoint.
        _item_ok, _item_errs, _item_san = validate_governance_suppression_request(
            {"skill": skill, "rule": _rule, "reason": _reason}
        )
        if not _item_ok:
            raise HTTPException(status_code=400, detail=_flatten_errors(_item_errs))
        skill = _item_san["skill"]
        _rule = _item_san["rule"] or None
        _reason = _item_san["reason"]

        if not fp:
            # Build a Finding-like object and fingerprint it with the gv1 scheme.
            try:
                fp = _compute_fp(Finding(
                    skill=skill, severity="low",
                    file=(_file or ""), rule=(_rule or ""),
                    title=(_title or ""), snippet=(_snippet or ""),
                ))
            except Exception as e:
                logger.warning("[SDLC-GOV] bulk upload: fingerprint compute failed — skipping item",
                               actor=actor, repo=body.repo, error=str(e))
                continue

        rows.append({
            "product_id":      body.product_id,
            "repo_name":       body.repo,
            "skill":           skill,
            "fingerprint":     fp,
            "rule":            _rule,
            "reason":          _reason,
            "source":          source,
            "pending_signoff": True,   # INERT until signed off
        })

    from store.sdlc_governance_findings import bulk_insert_suppressions
    inserted = bulk_insert_suppressions(rows, created_by=actor)

    logger.info("[SDLC-GOV] bulk suppression uploaded", actor=actor, repo=body.repo,
                product_id=body.product_id, count=inserted, source=source)
    return {"inserted": inserted, "pending_signoff": True}


@router.post("/governance-suppressions/{suppression_id}/signoff")
def signoff_governance_suppression(
    suppression_id: str,
    current_user: dict = Depends(get_current_user),
):
    """B3.1 — governance-lead/admin ONLY sign-off that clears pending_signoff on a
    bulk-uploaded suppression, making it LIVE for the matcher. This is the
    segregation-of-duties control: the person who uploads FPs cannot be the one
    who activates them unless they are also a governance lead. Fail-closed."""
    from auth.rbac import can_manage_suppression
    if not can_manage_suppression(current_user):
        raise HTTPException(status_code=403,
                            detail="Only a governance lead or admin may sign off suppressions")

    actor = (
        current_user.get("email") or current_user.get("sub")
        or current_user.get("id") or "unknown"
    )
    from db.database import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        result = db.execute(
            text(
                "UPDATE sdlc_governance_suppressions "
                "SET pending_signoff = FALSE, signed_off_by = :actor, signed_off_at = NOW() "
                "WHERE id = :id"
            ),
            {"actor": actor, "id": suppression_id},
        )
        if result.rowcount == 0:
            db.rollback()
            raise HTTPException(status_code=404, detail="Suppression not found")
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"[SDLC-GOV] signoff_governance_suppression failed: {e}")
        raise HTTPException(status_code=500, detail=f"Suppression sign-off failed: {e}")
    finally:
        db.close()

    logger.info("[SDLC-GOV] suppression signed off", actor=actor, suppression_id=suppression_id)
    return {"id": suppression_id, "signed_off": True}


# ─────────────────────────────────────────────────────────────
# GET /sdlc/runs/{run_id}/governance
# Read the persisted GOVERNANCE_REPORT artifact for a run.
# ─────────────────────────────────────────────────────────────

@router.get("/runs/{run_id}/governance")
def get_run_governance_report(run_id: str, current_user: dict = Depends(get_current_user)):
    """Return the latest GOVERNANCE_REPORT artifact for a run, scoped/404'd the
    same way as the sibling /runs/{run_id}/stages and /runs/{run_id}/verified-diff
    endpoints."""
    from store.sdlc_store import get_run
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    from store.sdlc_artifacts import _load_latest_artifact
    artifact = _load_latest_artifact(run_id, "GOVERNANCE_REPORT")
    if artifact is None:
        raise HTTPException(
            status_code=404,
            detail=f"No governance report for run={run_id} — governance review has not run for this run",
        )
    return {
        "run_id": run_id,
        "report": artifact.get("payload") or {},
        "created_at": artifact.get("created_at"),
    }


# ═════════════════════════════════════════════════════════════════════
# STEP 9 (governance pipeline expansion) — standalone governance scan
# trigger, per-domain approval gate, author-triggered domain fix, findings
# read, and admin domain-approver management. All deferred imports (router
# pattern); every /runs/{id}/* action is IDOR-guarded via _authorize_run.
# ═════════════════════════════════════════════════════════════════════

class GovernanceTriggerRequest(BaseModel):
    product_id: Optional[str] = None
    repo: str
    base_branch: str = "main"
    base_commit: Optional[str] = None
    head_branch: str
    governance_skills: Optional[list] = None


class GovernanceDomainApprovalRequest(BaseModel):
    decision: str   # "approved" | "changes_requested" | "approve" | "request_changes"
    note: Optional[str] = ""
    decided_by: Optional[str] = None   # email; defaults to current_user email
    false_positive_fingerprints: Optional[list] = None   # fingerprints the approver marked as FP
    suppress_forward: Optional[bool] = False              # also suppress in future scans


class GovernanceDomainFixRequest(BaseModel):
    fix_instructions: Optional[str] = ""


class GovernanceFindingFPRequest(BaseModel):
    domain:           str
    fingerprints:     list                      # per-run finding fingerprints to mark FP
    suppress_forward: Optional[bool] = False     # also carry forward as a cross-run suppression
    reason:           Optional[str] = None


class GovernanceApproverAddRequest(BaseModel):
    domain: str
    email: str
    user_id: Optional[str] = ""


class GovernanceFindingMarkFpRequest(BaseModel):
    # Author remediation loop (B2.2) — mark a single finding a false positive.
    reason:           Optional[str] = None
    fp_justification: Optional[str] = None
    suppress_forward: Optional[bool] = False


class GovernanceFindingDecisionRequest(BaseModel):
    # Team sign-off (B2.4) — an approver's per-finding decision on the current snapshot.
    decision: str                     # "accept" | "send_back"
    comment:  Optional[str] = ""       # MANDATORY (non-blank) when decision == "send_back"


class GovernanceRunFixesRequest(BaseModel):
    # Explicit batch fixer trigger — the author selected which marked findings to fix.
    # fingerprints omitted / empty → fix ALL currently fix_requested findings.
    fingerprints: Optional[list[str]] = None


class GovernanceDomainSendBackRequest(BaseModel):
    # Domain-level explicit "send back to author" — a domain approver returns the
    # WHOLE domain to the author for remediation with a mandatory reason. Distinct
    # from the per-finding send_back decision (that is granular; this bounces the
    # entire domain so the author sees "IS sent this back to you: <reason>").
    comment: str   # MANDATORY (non-blank) — the reason shown to the author


# ─────────────────────────────────────────────────────────────
# POST /sdlc/governance
# Trigger a standalone governance scan pipeline.
# ─────────────────────────────────────────────────────────────

@router.post("/governance")
def trigger_governance_scan(req: GovernanceTriggerRequest,
                            current_user: dict = Depends(get_current_user)):
    if not (req.repo or "").strip():
        raise HTTPException(status_code=400, detail="repo is required")
    if not (req.head_branch or "").strip():
        raise HTTPException(status_code=400, detail="head_branch is required")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
    # these feed into git clone/checkout, so identifier allow-list applies.
    is_valid, field_errors, sanitized = validate_governance_trigger_request(req)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    req.repo = sanitized["repo"]
    req.head_branch = sanitized["head_branch"]
    req.base_branch = sanitized["base_branch"]

    from store.sdlc_store import create_run, update_run_state
    _user_id    = current_user.get("id") or current_user.get("sub", "")
    _user_email = current_user.get("email", "")

    run = create_run(
        run_type="governance",
        jira_key=req.head_branch,
        jira_summary=f"Governance scan: {req.repo} ({req.head_branch})",
        repo=req.repo,
        triggered_by=_user_email or _user_id or "api",
        created_by=_user_id or _user_email or "",
    )

    issue_dict = {
        "_run_id":              run["id"],
        "product_id":           req.product_id or "",
        "repo":                 req.repo,
        "base_branch":          req.base_branch,
        "base_commit":          req.base_commit or "",
        "head_branch":          req.head_branch,
        "governance_skills":    req.governance_skills or [],
        "triggered_by_user_id": _user_id,
        "triggered_by_email":   _user_email,
    }
    # Persist context immediately
    update_run_state(run["id"], run["state"],
                     context_patch={
                         "product_id":   req.product_id or "",
                         "base_branch":  req.base_branch,
                         "base_commit":  req.base_commit or "",
                         "head_branch":  req.head_branch,
                         "repo":         req.repo,
                         "governance_skills": req.governance_skills or [],
                     })

    _require_rq()
    from core.job_queue import enqueue_sdlc_job
    try:
        job_id = enqueue_sdlc_job("workers.sdlc_worker.run_governance_pipeline_job", issue_dict)
    except RuntimeError as _e:
        raise HTTPException(status_code=429, detail=str(_e))

    logger.info("[SDLC-GOV] governance scan triggered", run_id=run["id"],
                repo=req.repo, head_branch=req.head_branch)
    return {"run_id": run["id"], "job_id": job_id, "state": run["state"]}


# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/governance/resume
# Resume fix phase after all domains are approved.
# ─────────────────────────────────────────────────────────────

@router.post("/runs/{run_id}/governance/resume")
def resume_governance_fix(run_id: str, current_user: dict = Depends(get_current_user)):
    from store.sdlc_store import get_run, add_run_event
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    # Segregation of duties: only the run owner (author) or an admin may ratify /
    # cut the MR — a domain approver signs off their own domain, not the whole run.
    # Mirrors /governance/start.
    if not _is_run_owner(run, current_user):
        raise HTTPException(status_code=403,
            detail="Only the run owner or an admin may ratify and cut the MR")

    if run["state"] != "AWAITING_GOVERNANCE_APPROVAL":
        raise HTTPException(status_code=400,
            detail=f"Run is in state '{run['state']}' — governance resume not applicable")

    from store.sdlc_governance_approvers import all_finding_domains_approved, list_domain_approvals
    if not all_finding_domains_approved(run_id):
        pending = [d["domain"] for d in (list_domain_approvals(run_id) or [])
                   if d.get("status") != "approved"]
        raise HTTPException(status_code=409,
            detail=f"Not all domains approved. Pending: {', '.join(pending)}")

    _actor = current_user.get("email") or current_user.get("sub", "user")
    add_run_event(run_id, "AWAITING_GOVERNANCE_APPROVAL", "GOVERNANCE_FIX",
                  actor=_actor, output="All domains approved — fix phase starting")

    _require_rq()
    from core.job_queue import enqueue_hitl_resume_job

    # Route to the correct worker job based on run type:
    #   "governance" runs → standalone governance pipeline (uses head_branch context)
    #   feature/bug runs  → re-enter CodingStateMachine at governance-approval resume point
    run_type = (run.get("type") or "governance").strip().lower()
    if run_type == "governance":
        job_id = enqueue_hitl_resume_job("workers.sdlc_worker.resume_governance_fix_job",
                                         run_id, extra={"actor": _actor})
    else:
        # feature/bug run suspended at AWAITING_GOVERNANCE_APPROVAL → resume SM
        job_id = enqueue_hitl_resume_job("workers.sdlc_worker.resume_in_pipeline_governance_job",
                                         run_id, extra={"actor": _actor})

    logger.info("[SDLC-GOV] governance fix resumed", run_id=run_id, actor=_actor,
                run_type=run_type)
    return {"run_id": run_id, "job_id": job_id, "action": "fix_started"}


# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/governance/start
# Author-triggered governance end-gate (2026-07-24). Governance is DECOUPLED from
# commit: a normal (non-draft) MR is opened at COMMIT, the run sits at
# AWAITING_PR_APPROVAL, and the author triggers governance here after any local
# integration testing. Enqueues run_endgate_governance_job, which re-drafts the MR
# for the gate duration and runs _run_governance_endgate over the committed diff.
# ─────────────────────────────────────────────────────────────

@router.post("/runs/{run_id}/governance/start")
def start_governance_endgate(run_id: str, current_user: dict = Depends(get_current_user)):
    from store.sdlc_store import get_run, add_run_event
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    # Segregation of duties: only the run owner (author) or an admin may trigger
    # governance — not a domain approver. Mirrors trigger_governance_domain_fix.
    if not _is_run_owner(run, current_user):
        raise HTTPException(status_code=403,
            detail="Only the run owner or an admin may start governance")

    # Guard: an MR must exist and the run must be at the post-commit gate.
    if run.get("state") not in ("AWAITING_PR_APPROVAL", "MR_CREATION"):
        raise HTTPException(status_code=400,
            detail=f"Run is in state '{run.get('state')}' — governance start not applicable "
                   f"(must be AWAITING_PR_APPROVAL / MR_CREATION with an open MR)")
    pr_number = run.get("pr_number")
    if not pr_number:
        raise HTTPException(status_code=409,
            detail="No merge request exists for this run — cannot start governance")

    _actor = current_user.get("email") or current_user.get("sub", "user")
    add_run_event(run_id, run.get("state") or "AWAITING_PR_APPROVAL", "GOVERNANCE_SCAN",
                  actor=_actor, output="Author triggered governance end-gate")

    _require_rq()
    # A run continuation (the run already exists, holds no worker slot at
    # AWAITING_PR_APPROVAL) → hitl-resume enqueue, bypassing per-reporter admission.
    from core.job_queue import enqueue_hitl_resume_job
    job_id = enqueue_hitl_resume_job(
        "workers.sdlc_worker.run_endgate_governance_job", run_id,
        extra={"actor": _actor})

    logger.info("[SDLC-GOV] author-triggered governance enqueued",
                run_id=run_id, actor=_actor, pr_number=pr_number)
    return {"run_id": run_id, "job_id": job_id, "action": "governance_started",
            "pr_number": pr_number}


# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/governance/domains/{domain}/approve
# Per-domain approval (approve or request changes).
# ─────────────────────────────────────────────────────────────

@router.post("/runs/{run_id}/governance/domains/{domain}/approve")
def decide_governance_domain(run_id: str, domain: str,
                             body: Optional[GovernanceDomainApprovalRequest] = None,
                             current_user: dict = Depends(get_current_user)):
    """B2.4 — approve a whole governance domain. The team must first have recorded
    a per-finding decision (accept/send_back) for EVERY visible finding via the
    per-finding decision endpoint. Approval is now a pure gate:
      • every visible finding (disposition ∈ {open, author_fp}) must have a decision
        for the current snapshot → else HTTP 409 (undecided);
      • no visible finding may carry a send_back decision (those route back to the
        author) → else HTTP 409 (send_back_pending);
      • otherwise every visible finding is accepted → mark the domain approved and
        stamp approved_snapshot_id = the current snapshot.

    This replaces the old approve-with-decision-body contract: the endpoint no
    longer takes a decision (send_back is a per-finding action), and the old
    approve-path FP-marking + forward-suppress block has been removed — per-finding
    false positives are now the author's mark-fp path (B2.2).
    """
    from store.sdlc_store import get_run, add_run_event
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    if run["state"] not in ("AWAITING_GOVERNANCE_APPROVAL", "GOVERNANCE_SCAN", "GOVERNANCE_APPROVAL"):
        raise HTTPException(status_code=400,
            detail=f"Run is in state '{run['state']}' — domain approval not applicable")

    from auth.rbac import can_approve_domain
    if not can_approve_domain(current_user, domain):
        raise HTTPException(status_code=403,
            detail=f"Not authorized to approve domain '{domain.upper()}'")

    _decided_by = (getattr(body, "decided_by", None)
                   or current_user.get("email") or current_user.get("sub", "user"))
    _note = getattr(body, "note", None) or ""

    from store.sdlc_governance_findings import current_findings, current_findings_checked, latest_snapshot
    from store.sdlc_governance_approvers import (
        decide_domain, all_finding_domains_approved, get_finding_decisions,
    )

    snap = latest_snapshot(run_id)
    if not snap:
        raise HTTPException(status_code=409,
            detail="No governance snapshot exists for this run — cannot approve")
    snapshot_id = snap["id"]

    # Visible set for this domain = findings whose disposition ∈ {open, author_fp}
    # (fix_confirmed / suppressed / fixed are hidden).
    _dom = domain.upper()
    rows, _ok = current_findings_checked(run_id)
    if not _ok:
        logger.warning(
            "[SDLC-GOV] domain approve blocked",
            run_id=run_id,
            domain=_dom,
            reason="unverifiable_findings_read",
            actor=_decided_by,
        )
        raise HTTPException(status_code=409,
            detail="Could not verify governance findings (transient read error) — retry approval.")
    visible = [
        f for f in rows
        if (f.get("domain") or "").upper() == _dom
        and (f.get("disposition") or "open") in ("open", "author_fp")
    ]

    # A domain with any un-justified FP (author_fp + blank justification — e.g. a
    # legacy row) cannot be approved until the justification is supplied. This
    # mirrors the per-finding justification_required flag surfaced to the UI.
    unjustified = [
        f for f in visible
        if (f.get("disposition") or "open") == "author_fp"
        and not (f.get("fp_justification") or "").strip()
    ]
    if unjustified:
        logger.warning("[SDLC-GOV] domain approve blocked", run_id=run_id, domain=_dom,
                        reason="missing_fp_justification", actor=_decided_by,
                        count=len(unjustified))
        raise HTTPException(status_code=409,
            detail=f"{len(unjustified)} false-positive findings need a justification "
                   f"before this domain can be approved")

    decisions = get_finding_decisions(run_id, snapshot_id, domain=_dom)

    undecided = [f for f in visible if f.get("fingerprint") not in decisions]
    if undecided:
        logger.warning("[SDLC-GOV] domain approve blocked", run_id=run_id, domain=_dom,
                        reason="undecided", actor=_decided_by)
        raise HTTPException(status_code=409,
            detail=f"{len(undecided)} findings still need a decision")

    # Block on send-backs derived from the DECISIONS table for this
    # domain+snapshot, NOT the disposition-filtered `visible` set: a send-back
    # re-opens the finding (disposition 'open') but the block must hold on the
    # recorded decision itself so an un-actioned send-back can never be approved
    # away by a subsequent disposition change.
    sent_back = [fp for fp, d in decisions.items()
                 if (d.get("decision") or "") == "send_back"]
    if sent_back:
        logger.warning("[SDLC-GOV] domain approve blocked", run_id=run_id, domain=_dom,
                        reason="send_back_pending", actor=_decided_by, count=len(sent_back))
        raise HTTPException(status_code=409,
            detail="domain has un-actioned send-backs; returns to author")

    # Every visible finding is accepted → approve the domain, binding the approval
    # to the exact snapshot signed off.
    decide_domain(run_id, _dom, "approved", _decided_by, _note,
                  approved_snapshot_id=snapshot_id)

    add_run_event(run_id, "GOVERNANCE_APPROVAL", "APPROVED",
                  actor=_decided_by,
                  output=f"Domain {_dom} approved. Note: {_note}")

    # Best-effort: export this domain decision to the linked Jira Change ticket
    # (governance evidence, V7). Guarded so an enqueue failure can NEVER 500 the
    # approval — the dedup ledger makes RQ retries / repeated calls safe.
    try:
        # Sync, in-process (2026-08-06): the async doc_queue (Q_DOC) worker is not
        # deployed on this host, so the enqueued evidence job never ran. Call the
        # evidence function directly instead. The try/except preserves best-effort
        # (never-500) semantics; idempotency is handled by the dedup ledger inside
        # post_domain_decision.
        from agents.sdlc_governance_change_ticket import post_domain_decision
        from store.sdlc_store import get_run
        _run = get_run(run_id) or {}
        post_domain_decision(
            _run,
            _dom,
            "approved",
            _decided_by,
            snapshot_id,
            user_id=(current_user.get("sub") or current_user.get("id") or ""),
            user_email=(current_user.get("email") or ""),
        )
    except Exception as _ee:
        logger.warning("[SDLC-GOV] evidence enqueue failed (non-fatal)",
                       run_id=run_id, domain=_dom, error=str(_ee))

    logger.info("[SDLC-GOV] domain decision", run_id=run_id, domain=_dom,
                decision="approved", decided_by=_decided_by, snapshot_id=snapshot_id)

    # Preserve the existing "all domains approved" signal for the resume wiring
    # (the separate resume-governance-fix endpoint gates on this; keep it working).
    all_approved = all_finding_domains_approved(run_id)

    # Best-effort: when THIS approval was the last one outstanding, email the run
    # author that governance has approved the whole run. Guarded so an SMTP
    # failure can never 500 the approval or affect the recorded gate state.
    if all_approved:
        try:
            from services.governance_email_service import notify_author_of_decision
            notify_author_of_decision(run_id, decision="approved", decided_by=_decided_by)
        except Exception as _me:
            logger.warning("[SDLC-GOV] approve: author notification failed — non-fatal",
                           run_id=run_id, domain=_dom, error=str(_me))

    return {
        "run_id":       run_id,
        "domain":       _dom,
        "decision":     "approved",
        "all_approved": all_approved,
    }


# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/governance/domains/{domain}/findings/{fingerprint}/decision
# Team sign-off (B2.4) — an approver records a per-finding accept / send_back
# decision on the CURRENT snapshot. send_back requires a non-blank comment and
# re-opens the finding to the author (disposition → open) for re-fix or
# re-justification, keeping it visible + blocking domain approval until resolved.
# ─────────────────────────────────────────────────────────────

@router.post("/runs/{run_id}/governance/domains/{domain}/findings/{fingerprint}/decision")
def decide_governance_finding(run_id: str, domain: str, fingerprint: str,
                              body: GovernanceFindingDecisionRequest,
                              current_user: dict = Depends(get_current_user)):
    from store.sdlc_store import get_run
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    # Segregation-of-duties: same server gate as the domain-approve endpoint.
    from auth.rbac import can_approve_domain
    if not can_approve_domain(current_user, domain):
        raise HTTPException(status_code=403,
            detail=f"Not authorized to decide findings for domain '{domain.upper()}'")

    decision = (body.decision or "").strip().lower()
    if decision not in ("accept", "send_back"):
        raise HTTPException(status_code=422,
            detail="decision must be 'accept' or 'send_back'")

    comment = (body.comment or "")
    # Mandatory-comment rule (server-enforced): a send_back MUST carry a reason.
    if decision == "send_back" and not comment.strip():
        raise HTTPException(status_code=422,
            detail="a non-blank comment is required when sending a finding back")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
    # comment is rendered back into the governance panel for the author to read.
    if comment.strip():
        _ok, _errs, _val = validate_free_text(comment)
        if not _ok:
            raise HTTPException(status_code=400, detail=_flatten_errors({"comment": _errs}))
        comment = _val

    _dom = domain.upper()
    _actor = current_user.get("email") or current_user.get("sub", "user")

    from store.sdlc_governance_findings import latest_snapshot, set_disposition
    from store.sdlc_governance_approvers import (
        record_finding_decision, bump_domain_send_back, reset_domain_to_pending,
    )

    snap = latest_snapshot(run_id)
    if not snap:
        raise HTTPException(status_code=409,
            detail="No governance snapshot exists for this run — cannot decide")
    snapshot_id = snap["id"]

    ok = record_finding_decision(run_id, snapshot_id, _dom, fingerprint,
                                 decision, comment, _actor)
    if not ok:
        raise HTTPException(status_code=500,
            detail="Failed to record the finding decision")

    # Audit thread row (role=approver; decision_context = the decision itself).
    _gov_add_finding_comment(
        run_id, snapshot_id, fingerprint, _dom,
        author_user_id=(current_user.get("sub") or current_user.get("id")),
        author_email=(current_user.get("email") or ""),
        role="approver",
        body=comment,
        decision_context=decision,
    )

    # On send_back: re-open the finding to the author as ACTIONABLE (disposition
    # 'open' — initial-stage behavior, so the author board offers Request-Fix /
    # Mark-FP again). Do NOT flip it to fix_requested: that would drop it from the
    # team-visible {open, author_fp} approve set and defeat the 409 approve-guard.
    # The recorded send_back decision (above) is what marks the finding "sent back"
    # on the team board and blocks domain approval. Also reset the domain to
    # pending so an already-approved domain re-enters review, and bump its
    # send-back counters. On accept: leave the author disposition untouched.
    if decision == "send_back":
        set_disposition(run_id, [fingerprint], "open", _actor)
        bump_domain_send_back(run_id, _dom)
        reset_domain_to_pending(run_id, _dom)
        logger.info("[SDLC-GOV] send-back recorded — finding re-opened, domain reset to pending",
                    run_id=run_id, domain=_dom, fingerprint=fingerprint, actor=_actor)
    elif decision == "accept":
        # Cross-run suppression is written ONLY after approval, and only once every
        # domain covering this finding's content_key has approved (Task 6). The
        # decision row was just recorded above, so it is visible to the coverage
        # check. Best-effort — never breaks the accept.
        _gov_maybe_write_content_suppression(run_id, run, snapshot_id, fingerprint,
                                             _dom, _actor)

    logger.info("[SDLC-GOV] per-finding decision recorded", run_id=run_id, domain=_dom,
                fingerprint=fingerprint, decision=decision, actor=_actor)

    return {
        "run_id":      run_id,
        "domain":      _dom,
        "fingerprint": fingerprint,
        "decision":    decision,
        "comment":     comment,
        "snapshot_id": snapshot_id,
    }


# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/governance/domains/{domain}/send-back
# Domain-level explicit send-back. A domain approver returns the WHOLE domain to
# the author with a mandatory reason. Sets the domain gate to changes_requested
# (so it blocks final approval and surfaces on the author's board with the reason)
# and bumps the send-back counters. Same-state — the run stays at
# AWAITING_GOVERNANCE_APPROVAL; only per-user content changes.
# ─────────────────────────────────────────────────────────────

@router.post("/runs/{run_id}/governance/domains/{domain}/send-back")
def send_back_governance_domain(run_id: str, domain: str,
                                body: GovernanceDomainSendBackRequest,
                                current_user: dict = Depends(get_current_user)):
    from store.sdlc_store import get_run, add_run_event
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    if run["state"] not in ("AWAITING_GOVERNANCE_APPROVAL", "GOVERNANCE_SCAN", "GOVERNANCE_APPROVAL"):
        raise HTTPException(status_code=400,
            detail=f"Run is in state '{run['state']}' — domain send-back not applicable")

    # Same approver gate as the domain-approve / per-finding decision endpoints.
    from auth.rbac import can_approve_domain
    if not can_approve_domain(current_user, domain):
        raise HTTPException(status_code=403,
            detail=f"Not authorized to send back domain '{domain.upper()}'")

    comment = (body.comment or "")
    if not comment.strip():
        raise HTTPException(status_code=422,
            detail="a non-blank comment is required to send a domain back to the author")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    _ok, _errs, _val = validate_free_text(comment)
    if not _ok:
        raise HTTPException(status_code=400, detail=_flatten_errors({"comment": _errs}))
    comment = _val

    _dom = domain.upper()
    _actor = current_user.get("email") or current_user.get("sub", "user")

    from store.sdlc_governance_approvers import (
        decide_domain, bump_domain_send_back, get_domain_approval,
    )

    appr = get_domain_approval(run_id, _dom)
    if not appr:
        raise HTTPException(status_code=409,
            detail=f"Domain '{_dom}' has no approval row for this run — cannot send back")

    # Flip the domain gate to changes_requested with the reason stored as the note.
    # This blocks all_finding_domains_approved (so the run cannot advance) and the
    # author board reads status + note to render "sent back to you: <reason>".
    if not decide_domain(run_id, _dom, "changes_requested", _actor, comment):
        raise HTTPException(status_code=500, detail="Failed to record the domain send-back")
    bump_domain_send_back(run_id, _dom)

    # Audit thread row (domain-scoped: fingerprint left empty, role=approver).
    # Only written when a snapshot exists (snapshot_id may be NOT NULL); the
    # helper is best-effort and non-fatal regardless.
    snap = None
    try:
        from store.sdlc_governance_findings import latest_snapshot
        snap = latest_snapshot(run_id)
    except Exception:
        snap = None
    if snap and snap.get("id"):
        _gov_add_finding_comment(
            run_id, snap["id"], "", _dom,
            author_user_id=(current_user.get("sub") or current_user.get("id")),
            author_email=(current_user.get("email") or ""),
            role="approver",
            body=comment,
            decision_context="domain_send_back",
        )

    add_run_event(run_id, "GOVERNANCE_APPROVAL", "CHANGES_REQUESTED",
                  actor=_actor,
                  output=f"Domain {_dom} sent back to author. Reason: {comment}")

    # Best-effort: export this send-back decision to the linked Jira Change ticket
    # (governance evidence, V7). Guarded so an enqueue failure can NEVER 500 the
    # send-back — dedup ledger keeps RQ retries / repeated calls safe.
    try:
        # Sync, in-process (2026-08-06): the async doc_queue (Q_DOC) worker is not
        # deployed on this host, so the enqueued evidence job never ran. Call the
        # evidence function directly instead. The try/except preserves best-effort
        # (never-500) semantics; idempotency is handled by the dedup ledger inside
        # post_domain_decision.
        from agents.sdlc_governance_change_ticket import post_domain_decision
        from store.sdlc_store import get_run
        _run = get_run(run_id) or {}
        post_domain_decision(
            _run,
            _dom,
            "changes_requested",
            _actor,
            (snap.get("id") if snap else None),
            user_id=(current_user.get("sub") or current_user.get("id") or ""),
            user_email=(current_user.get("email") or ""),
        )
    except Exception as _ee:
        logger.warning("[SDLC-GOV] evidence enqueue failed (non-fatal)",
                       run_id=run_id, domain=_dom, error=str(_ee))

    # Best-effort: email the run author that this domain was sent back to them,
    # including the reviewer's comment. Guarded so an SMTP failure can never 500
    # the send-back or affect the recorded gate state.
    try:
        from services.governance_email_service import notify_author_of_decision
        notify_author_of_decision(
            run_id, decision="changes_requested",
            domain=_dom, comment=comment, decided_by=_actor,
        )
    except Exception as _me:
        logger.warning("[SDLC-GOV] send-back: author notification failed — non-fatal",
                       run_id=run_id, domain=_dom, error=str(_me))

    logger.info("[SDLC-GOV] domain send-back", run_id=run_id, domain=_dom, actor=_actor)
    return {
        "run_id":   run_id,
        "domain":   _dom,
        "status":   "changes_requested",
        "comment":  comment,
    }


# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/governance/domains/{domain}/trigger-fix
# Author-triggered domain fix after "changes_requested".
# ─────────────────────────────────────────────────────────────

@router.post("/runs/{run_id}/governance/domains/{domain}/trigger-fix")
def trigger_governance_domain_fix(run_id: str, domain: str,
                                  body: GovernanceDomainFixRequest,
                                  current_user: dict = Depends(get_current_user)):
    from store.sdlc_store import get_run
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    # Only the run's triggering user (author) or an admin may request a fix — not
    # domain approvers (segregation of duties).
    if not _is_run_owner(run, current_user):
        raise HTTPException(status_code=403,
            detail="Only the run owner or an admin may request a domain fix")

    from store.sdlc_governance_approvers import get_domain_approval
    appr = get_domain_approval(run_id, domain.upper())
    # Author-driven remediation: a fix may be requested either after an approver
    # sends the domain back ("changes_requested") OR directly by the author while
    # the domain still has open findings ("pending"). An already-approved domain
    # is not re-openable via this path.
    if not appr or appr.get("status") not in ("changes_requested", "pending"):
        raise HTTPException(status_code=409,
            detail=f"Domain '{domain.upper()}' is not open for a fix "
                   f"(state: {appr.get('status') if appr else 'none'})")

    _actor = current_user.get("email") or current_user.get("sub", "user")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
    # fix_instructions is free text handed to the coding agent.
    _fix_instr = body.fix_instructions or ""
    if _fix_instr.strip():
        _ok, _errs, _fix_instr = validate_free_text(_fix_instr)
        if not _ok:
            raise HTTPException(status_code=400, detail=_flatten_errors({"fix_instructions": _errs}))

    _require_rq()
    from core.job_queue import enqueue_hitl_resume_job
    # enqueue_hitl_resume_job(fn_name, run_id, feedback="", extra=None): the domain
    # + fix_instructions + actor ride along in the `extra` dict (merged into the
    # worker payload). It takes neither an `actor=` kwarg nor a free-form payload.
    job_id = enqueue_hitl_resume_job(
        "workers.sdlc_worker.trigger_domain_fix_job", run_id,
        extra={"domain": domain.upper(), "actor": _actor,
               "fix_instructions": _fix_instr})

    logger.info("[SDLC-GOV] domain fix triggered", run_id=run_id, domain=domain.upper(),
                actor=_actor)
    return {"run_id": run_id, "domain": domain.upper(), "job_id": job_id, "action": "fix_triggered"}


# ─────────────────────────────────────────────────────────────
# POST /sdlc/runs/{run_id}/governance/findings/false-positive
# Author (or approver/admin) marks per-run findings as false positive,
# optionally carrying them forward as cross-run suppressions.
# ─────────────────────────────────────────────────────────────

@router.post("/runs/{run_id}/governance/findings/false-positive")
def mark_governance_findings_false_positive(
    run_id: str,
    body: GovernanceFindingFPRequest,
    current_user: dict = Depends(get_current_user),
):
    from store.sdlc_store import get_run
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    domain = (body.domain or "").strip().upper()
    if not domain:
        raise HTTPException(status_code=422, detail="domain is required")
    fps = [f for f in (body.fingerprints or []) if f]
    if not fps:
        raise HTTPException(status_code=422, detail="fingerprints must be a non-empty list")

    # Mandatory FP justification (server-enforced), mirroring the send_back
    # non-blank-comment rule and the per-finding mark-fp endpoint.
    _reason = (body.reason or "").strip() or None
    if not _reason:
        raise HTTPException(status_code=422,
            detail="a non-blank reason is required to mark findings false positive")

    # Author-triage gate: the run owner (author) OR a configured approver for this
    # domain (admins satisfy both). Distinct from the domain sign-off gate.
    from auth.rbac import can_approve_domain
    if not (_is_run_owner(run, current_user) or can_approve_domain(current_user, domain)):
        raise HTTPException(status_code=403,
            detail="Only the run owner, a domain approver, or an admin may mark findings false positive")

    _actor = current_user.get("email") or current_user.get("sub", "user")

    # 1. Mark the per-run findings false_positive (removes them from open_count).
    from store.sdlc_governance_findings import set_status as set_finding_status
    try:
        set_finding_status(run_id, fps, "false_positive", _actor, domain=domain)
    except Exception as _fp_e:
        logger.warning("[SDLC-GOV] author FP marking failed", run_id=run_id,
                       domain=domain, error=str(_fp_e))
        raise HTTPException(status_code=500, detail="Failed to mark findings false positive")

    # NOTE: marking findings false positive is honored ONLY in the current run —
    # it removes them from the open set (step 1 above) but writes NO cross-run
    # suppression. A cross-run suppression is written only after every covering
    # domain approves (see decide_governance_finding's accept path).

    logger.info("[SDLC-GOV] author marked findings false positive", run_id=run_id,
                domain=domain, count=len(fps), actor=_actor)
    return {"run_id": run_id, "domain": domain, "marked": len(fps)}


# ─────────────────────────────────────────────────────────────
# GET /sdlc/runs/{run_id}/governance/findings
# Findings for a run, optionally filtered by domain or status.
# ─────────────────────────────────────────────────────────────

@router.get("/runs/{run_id}/governance/findings")
def get_governance_findings(run_id: str, domain: Optional[str] = None,
                            status: Optional[str] = None,
                            current_user: dict = Depends(get_current_user)):
    from store.sdlc_store import get_run
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)

    from store.sdlc_governance_findings import list_findings, domain_open_counts, current_findings
    from store.sdlc_governance_approvers import list_domain_approvals
    from auth.rbac import can_approve_domain
    from agents.sdlc_governance.schema import severity_rank

    # B2.3: prefer the snapshot projection (latest scan only, disposition +
    # decision stamped in). Falls back to the legacy sdlc_governance_findings
    # read for older runs that never wrote a snapshot (B1.4 dual-write started
    # 2026-07-23) so those still render.
    findings = current_findings(run_id)
    if findings:
        source = "projection"
        if domain:
            findings = [f for f in findings if (f.get("domain") or "").upper() == domain.upper()]
        if status:
            findings = [f for f in findings if (f.get("disposition") or "open") == status]
    else:
        source = "legacy_fallback"
        findings = list_findings(run_id, status=status, domain=domain.upper() if domain else None)
    logger.info("[SDLC-GOV] get_governance_findings", run_id=run_id, source=source,
               count=len(findings or []))

    # Deterministic order (independent of created_at / scan timing):
    #   (severity DESC, domain ASC, file ASC, line ASC NULLS LAST, fingerprint ASC).
    # Applied here so refresh and re-scan render byte-stable; the UI renders in
    # this exact order. `line is None` sorts True-last, giving NULLS LAST.
    def _gov_order_key(f):
        line = f.get("line")
        return (
            -severity_rank(f.get("severity")),
            (f.get("domain") or "").upper(),
            f.get("file") or "",
            (line is None, line if line is not None else 0),
            f.get("fingerprint") or "",
        )
    findings = sorted(findings or [], key=_gov_order_key)

    # Justification-required flag: an author_fp with no justification (legacy
    # un-justified FP) blocks domain approval / forward progress until justified.
    for f in findings:
        disp = f.get("disposition") or f.get("status") or "open"
        f["justification_required"] = bool(
            disp == "author_fp" and not (f.get("fp_justification") or "").strip()
        )

    counts     = domain_open_counts(run_id)
    approvals  = list_domain_approvals(run_id)

    # Segregation of duties (server-authoritative): a domain approver sees ONLY the
    # governance domains they own. Admins and the run OWNER keep the full view.
    #
    # DEVIATION from plan Step 5 (flagged for review): the plan said "restrict all
    # non-admins to owned". Applied literally that would blank the AUTHOR's triage
    # board — the author (run owner) is typically a non-admin, non-approver, so
    # approver_domains_for() is empty for them. The author is remediating findings
    # on their OWN change across every domain, which is not a cross-domain
    # disclosure, so the run owner is exempted here alongside admins. Only true
    # third-party approvers are scoped to their owned domains. Fail-closed: on any
    # error approver_domains_for() returns an empty set → approver sees nothing.
    is_admin = current_user.get("role") == "admin"
    is_owner = _is_run_owner(run, current_user)
    if not is_admin and not is_owner:
        from store.sdlc_governance_approvers import approver_domains_for
        owned = approver_domains_for(current_user)
        findings  = [f for f in (findings or [])
                     if (f.get("domain") or "").upper() in owned]
        counts    = {k: v for k, v in (counts or {}).items()
                     if (k or "").upper() in owned}
        approvals = [a for a in (approvals or [])
                     if (a.get("domain") or "").upper() in owned]
        logger.info("[SDLC-GOV] get_governance_findings per-domain filter applied",
                    run_id=run_id,
                    actor=(current_user.get("email") or current_user.get("sub") or ""),
                    owned_domains=sorted(owned), is_admin=is_admin)

    # Build the per-domain grouped structure the approval UI renders
    # (GovernanceApprovalPanel reads `data.domains`). Each entry merges the
    # domain's findings with its approval-gate row (status / decided_by / note)
    # and a per-caller can_approve flag (segregation-of-duties gate). Without
    # this the panel received no `domains` key and rendered an empty gate.
    appr_by_domain = { (a.get("domain") or ""): a for a in (approvals or []) }
    domain_keys = set(appr_by_domain.keys())
    for f in (findings or []):
        domain_keys.add((f.get("domain") or ""))
    domains = []
    for dk in sorted(domain_keys):
        d_findings = [f for f in (findings or []) if (f.get("domain") or "") == dk]
        appr = appr_by_domain.get(dk, {})
        open_count = appr.get("open_count")
        if open_count is None:
            # Projection findings carry `disposition`; legacy findings carry
            # `status` — check either so the fallback count is correct either way.
            open_count = sum(
                1 for f in d_findings
                if (f.get("disposition") or f.get("status") or "open") == "open"
            )
        blocked_missing_just = any(
            f.get("justification_required") for f in d_findings
        )
        domains.append({
            "domain":            dk,
            "status":            appr.get("status", "pending"),
            "open_count":        open_count,
            "decided_by":        appr.get("decided_by"),
            "decided_at":        appr.get("decided_at"),
            "note":              appr.get("note"),
            # Send-back history — lets the author board render "sent back to you".
            "iteration":         appr.get("iteration"),
            "last_send_back_at": appr.get("last_send_back_at"),
            "can_approve":       can_approve_domain(current_user, dk),
            # True when any visible finding is an un-justified FP → blocks approve.
            "blocked_by_missing_justification": blocked_missing_just,
            "findings":          d_findings,
        })

    # Surface the governance sub-phase flags directly on the findings payload so
    # the UI does not depend on run.context surviving the (light) run serializer —
    # the frontend reads these via getRunFlag(run, findingsMeta, key).
    _ctx = run.get("context") or {}
    return {
        "run_id":                        run_id,
        "is_owner":                      _is_run_owner(run, current_user),
        "domains":                       domains,
        "findings":                      findings,
        "domain_counts":                 counts,
        "domain_approvals":              approvals,
        "governance_submitted_to_teams": bool(_ctx.get("governance_submitted_to_teams")),
        "governance_rescanning":         bool(_ctx.get("governance_rescanning")),
        "governance_not_converging":     bool(_ctx.get("governance_not_converging")),
    }


# ─────────────────────────────────────────────────────────────
# Author remediation loop (B2.2) — AUTHOR-OWNED endpoints.
#
# The author (run owner or admin) triages governance findings while the run is
# SUSPENDED at AWAITING_GOVERNANCE_APPROVAL: mark a finding false-positive,
# request an automated fix (enqueued — NEVER run synchronously), and finally
# hand off to the domain teams. Segregation of duties: the author is not
# necessarily a domain approver, so these gate on _is_run_owner (owner/admin),
# NOT can_approve_domain (which is the team sign-off gate, B2.4).
# ─────────────────────────────────────────────────────────────

def _gov_lookup_finding_meta(run_id: str, fingerprint: str):
    """Best-effort (skill, domain, latest_snapshot_id) for a fingerprint from the
    immutable observations table. Returns (None, None, None) on miss/error."""
    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _t
        _db = SessionLocal()
        try:
            row = _db.execute(_t(
                "SELECT skill, domain, snapshot_id "
                "FROM sdlc_governance_finding_observations "
                "WHERE run_id = :rid AND fingerprint = :fp "
                "ORDER BY created_at DESC LIMIT 1"
            ), {"rid": run_id, "fp": fingerprint}).fetchone()
            if row is None:
                return None, None, None
            return row[0], row[1], (str(row[2]) if row[2] else None)
        finally:
            _db.close()
    except Exception as exc:
        logger.warning("[SDLC-GOV] _gov_lookup_finding_meta failed", run_id=run_id,
                       fingerprint=fingerprint, error=str(exc))
        return None, None, None


def _gov_add_finding_comment(run_id, snapshot_id, fingerprint, domain, *,
                             author_user_id, author_email, role, body,
                             decision_context):
    """Append one finding-comment audit row (sdlc_governance_finding_comments).
    Never raises — a comment failure must not break the triage action."""
    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _t
        _db = SessionLocal()
        try:
            _db.execute(_t(
                "INSERT INTO sdlc_governance_finding_comments "
                "(run_id, snapshot_id, fingerprint, domain, author_user_id, "
                " author_email, role, body, decision_context) "
                "VALUES (:run_id, :snapshot_id, :fingerprint, :domain, :author_user_id, "
                "        :author_email, :role, :body, :decision_context)"
            ), {
                "run_id": run_id, "snapshot_id": snapshot_id, "fingerprint": fingerprint,
                "domain": domain,
                "author_user_id": (str(author_user_id) if author_user_id else None),
                "author_email": author_email or "", "role": role,
                "body": body or "", "decision_context": decision_context,
            })
            _db.commit()
        finally:
            _db.close()
    except Exception as exc:
        logger.warning("[SDLC-GOV] _gov_add_finding_comment failed — non-fatal",
                       run_id=run_id, fingerprint=fingerprint, error=str(exc))


def _gov_list_finding_comments(run_id: str, fingerprint: str) -> list:
    """Read the finding-comment audit thread (send-backs, notes, FP re-justification)
    for one (run_id, fingerprint), oldest first. Never raises → [] on error."""
    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _t
        _db = SessionLocal()
        try:
            rows = _db.execute(_t(
                "SELECT snapshot_id, domain, author_user_id, author_email, role, "
                "       body, decision_context, created_at "
                "FROM sdlc_governance_finding_comments "
                "WHERE run_id = :rid AND fingerprint = :fp "
                "ORDER BY created_at ASC"
            ), {"rid": run_id, "fp": fingerprint}).fetchall()
        finally:
            _db.close()
        return [
            {
                "snapshot_id":      str(r.snapshot_id) if r.snapshot_id else None,
                "domain":           r.domain,
                "author_user_id":   r.author_user_id,
                "author_email":     r.author_email,
                "role":             r.role,
                "body":             r.body,
                "decision_context": r.decision_context,
                "created_at":       r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("[SDLC-GOV] _gov_list_finding_comments failed — returning []",
                       run_id=run_id, fingerprint=fingerprint, error=str(exc))
        return []


@router.get("/runs/{run_id}/governance/findings/{fingerprint}/comments")
def get_governance_finding_comments(run_id: str, fingerprint: str,
                                    current_user: dict = Depends(get_current_user)):
    """Per-finding comment thread (send-back reasons + FP re-justification), for the
    author-triage / team-review boards. Visibility-gated to run viewers."""
    from store.sdlc_store import get_run
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)
    return {"run_id": run_id, "fingerprint": fingerprint,
            "comments": _gov_list_finding_comments(run_id, fingerprint)}


def _gov_forward_suppress_one(run_id, run, fingerprint, skill_hint, actor,
                              reason=None, content_key=None):
    """Write ONE cross-run governance suppression for the run's OWN repo, mirroring
    the approve / false-positive forward-suppress block (recover the real skill
    slug so it matches apply_suppressions' key). When `content_key` is given it is
    persisted too (dual-write) so the suppression matches in ANY domain in future
    runs via the skill-independent content tuple, alongside the legacy
    (skill, fingerprint) row. Never raises."""
    try:
        _repo    = run.get("repo") or ""
        _prod_id = (run.get("context") or {}).get("product_id") or ""
        from db.database import SessionLocal
        from sqlalchemy import text as _t
        import uuid as _uuid
        _db = SessionLocal()
        try:
            _slug = skill_hint or ""
            if not _slug:
                _r = _db.execute(_t(
                    "SELECT skill FROM sdlc_governance_finding_observations "
                    "WHERE run_id = :rid AND fingerprint = :fp AND skill <> '' "
                    "ORDER BY created_at DESC LIMIT 1"
                ), {"rid": run_id, "fp": fingerprint}).fetchone()
                _slug = (_r[0] if _r else "") or ""
            if not _slug:
                logger.warning("[SDLC-GOV] author forward-suppress: no slug recovered "
                               "— skipping", run_id=run_id, fingerprint=fingerprint)
                return
            _db.execute(_t(
                "INSERT INTO sdlc_governance_suppressions "
                "(id, product_id, repo_name, skill, fingerprint, content_key, reason, created_by, active, created_at) "
                "VALUES (:id, :pid, :repo, :skill, :fp, :ck, :reason, :by, TRUE, NOW()) "
                "ON CONFLICT (product_id, repo_name, skill, fingerprint) DO UPDATE "
                "SET active=TRUE, content_key=EXCLUDED.content_key, reason=EXCLUDED.reason, created_by=:by"
            ), {"id": str(_uuid.uuid4()), "pid": _prod_id or None, "repo": _repo,
                "skill": _slug, "fp": fingerprint, "ck": content_key,
                "reason": reason, "by": actor})
            _db.commit()
            logger.info("[SDLC-GOV] author forward-suppress written", run_id=run_id,
                        fingerprint=fingerprint, skill=_slug, content_key=content_key)
        finally:
            _db.close()
    except Exception as exc:
        logger.warning("[SDLC-GOV] author forward-suppress failed — non-fatal",
                       run_id=run_id, fingerprint=fingerprint, error=str(exc))


def _gov_maybe_write_content_suppression(run_id, run, snapshot_id, fingerprint,
                                         accepting_domain, actor):
    """Cross-run suppression on approval, GATED by domain coverage (Task 6).

    Called when a domain ACCEPTS a finding. It writes ONE cross-run suppression
    (keyed by content_key, dual-written with the legacy (skill, fingerprint) row)
    only when:
      • the accepted finding's AUTHOR disposition is `author_fp`, and
      • EVERY domain where the SAME content_key appears in this snapshot has
        approved that content_key — a domain approves the content_key when all its
        findings carrying it have an `accept` decision.
    Single-domain content therefore suppresses immediately on that domain's
    accept. Any covering domain with a pending / send-back finding blocks the
    write. Never raises — a failure here must not break the accept decision.
    """
    try:
        from store.sdlc_governance_findings import current_findings
        from store.sdlc_governance_approvers import get_finding_decisions

        rows = current_findings(run_id) or []
        me = next((f for f in rows if f.get("fingerprint") == fingerprint), None)
        if not me:
            return
        # Only author-declared false positives ever become cross-run suppressions.
        if (me.get("disposition") or "open") != "author_fp":
            return
        content_key = me.get("content_key")
        if not content_key:
            return

        # Findings sharing this content_key, grouped by the domain that observed them.
        peers = [f for f in rows if f.get("content_key") == content_key]
        covering_domains = sorted({
            (f.get("domain") or "").upper() for f in peers if (f.get("domain") or "")
        })
        if not covering_domains:
            return

        # A domain approves the content_key when EVERY one of its findings with that
        # content_key carries an `accept` decision on this snapshot.
        for dom in covering_domains:
            decisions = get_finding_decisions(run_id, snapshot_id, domain=dom)
            dom_fps = [f.get("fingerprint") for f in peers
                       if (f.get("domain") or "").upper() == dom]
            for fp in dom_fps:
                d = decisions.get(fp) or {}
                if (d.get("decision") or "") != "accept":
                    logger.info("[SDLC-GOV] content suppression gated — domain not yet approved",
                                run_id=run_id, content_key=content_key, domain=dom)
                    return

        # Every covering domain approved → write ONE suppression. Recover the
        # owning skill slug from the accepted finding (fall back to observations).
        _skill = me.get("skill") or ""
        _reason = me.get("fp_justification") or ""
        _gov_forward_suppress_one(run_id, run, fingerprint, _skill, actor,
                                  reason=_reason, content_key=content_key)
        logger.info("[SDLC-GOV] cross-run content suppression written on approval",
                    run_id=run_id, content_key=content_key,
                    covering_domains=covering_domains)
    except Exception as exc:
        logger.warning("[SDLC-GOV] content suppression check failed — non-fatal",
                       run_id=run_id, fingerprint=fingerprint, error=str(exc))


@router.post("/runs/{run_id}/governance/findings/{fingerprint}/mark-fp")
def author_mark_finding_fp(run_id: str, fingerprint: str,
                           body: GovernanceFindingMarkFpRequest,
                           current_user: dict = Depends(get_current_user)):
    from store.sdlc_store import get_run
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)
    if not _is_run_owner(run, current_user):
        raise HTTPException(status_code=403,
            detail="Only the run owner or an admin may triage governance findings")

    _actor = current_user.get("email") or current_user.get("sub", "user")
    # Mandatory FP justification (server-enforced), mirroring the send_back
    # non-blank-comment rule. A blank justification is rejected with 422 so a
    # false positive is never recorded without an auditable reason.
    fp_just = (body.fp_justification or body.reason or "").strip() or None
    if not fp_just:
        raise HTTPException(status_code=422,
            detail="a non-blank fp_justification is required to mark a finding false positive")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    _ok, _errs, _val = validate_free_text(fp_just)
    if not _ok:
        raise HTTPException(status_code=400, detail=_flatten_errors({"fp_justification": _errs}))
    fp_just = _val

    # AUTHOR axis → disposition author_fp.
    from store.sdlc_governance_findings import set_disposition, get_dispositions
    set_disposition(run_id, [fingerprint], "author_fp", _actor, fp_justification=fp_just)

    # Marking a false positive resolves this finding — drop any stale not-converging
    # banner so the author isn't shown a dead-end warning after acting.
    try:
        from store.sdlc_store import update_run_state
        update_run_state(run_id, run.get("state") or "AWAITING_GOVERNANCE_APPROVAL",
                         context_patch={"governance_not_converging": False,
                                        "governance_not_converging_reason": ""})
    except Exception:
        pass

    _skill, _domain, _snapshot_id = _gov_lookup_finding_meta(run_id, fingerprint)

    # Clear any stale team `send_back` decision on the CURRENT snapshot for this
    # finding. A per-finding send-back writes a snapshot-keyed `send_back` decision
    # that is otherwise only cleared by a re-scan (new snapshot). Marking the finding
    # a false positive resolves it on the author axis but WITHOUT a re-scan, so the
    # stale decision would keep the team board showing "awaiting author fix" (no
    # accept/send-back buttons) and block domain approval forever. Clearing it makes
    # the finding decidable/approvable again immediately.
    if _snapshot_id:
        try:
            from store.sdlc_governance_approvers import clear_send_back_decisions
            clear_send_back_decisions(run_id, _snapshot_id, _domain, [fingerprint])
        except Exception as _cs:
            logger.warning("[SDLC-GOV] mark-fp: could not clear stale send_back decision — non-fatal",
                           run_id=run_id, fingerprint=fingerprint, error=str(_cs))

    # NOTE: an author FP mark is honored ONLY in the current run — it removes the
    # finding from the actionable/open set but writes NO cross-run suppression.
    # A cross-run suppression is written only after every covering domain approves
    # (see the approval path, which reuses _gov_forward_suppress_one).

    # Audit comment (role=author; fp_justification context when a justification given).
    _gov_add_finding_comment(
        run_id, _snapshot_id, fingerprint, _domain,
        author_user_id=(current_user.get("sub") or current_user.get("id")),
        author_email=(current_user.get("email") or ""),
        role="author",
        body=(fp_just or body.reason or ""),
        decision_context=("fp_justification" if fp_just else "note"),
    )

    disp = get_dispositions(run_id).get(fingerprint, {"disposition": "author_fp"})
    logger.info("[SDLC-GOV] author mark-fp", run_id=run_id, fingerprint=fingerprint,
                actor=_actor)
    return {"run_id": run_id, "fingerprint": fingerprint, "disposition": disp}


@router.post("/runs/{run_id}/governance/findings/{fingerprint}/request-fix")
def author_request_fix(run_id: str, fingerprint: str,
                       current_user: dict = Depends(get_current_user)):
    """MARK a finding for fixing (disposition → fix_requested). This does NOT launch
    the fixer — the author explicitly triggers it later via POST .../governance/run-fixes
    (one batch job for a selected few or all marked findings). No auto-trigger."""
    from store.sdlc_store import get_run
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)
    if not _is_run_owner(run, current_user):
        raise HTTPException(status_code=403,
            detail="Only the run owner or an admin may request a governance fix")

    _actor = current_user.get("email") or current_user.get("sub", "user")

    # AUTHOR axis → disposition fix_requested (marked, NOT yet running).
    from store.sdlc_governance_findings import set_disposition
    set_disposition(run_id, [fingerprint], "fix_requested", _actor)

    # Drop any stale not-converging banner from a prior stalled attempt.
    try:
        from store.sdlc_store import update_run_state
        update_run_state(run_id, run.get("state") or "AWAITING_GOVERNANCE_APPROVAL",
                         context_patch={"governance_not_converging": False,
                                        "governance_not_converging_reason": ""})
    except Exception:
        pass

    logger.info("[SDLC-GOV] author mark-for-fix", run_id=run_id, fingerprint=fingerprint,
                actor=_actor)
    return {"status": "fix_requested", "enqueued": False,
            "run_id": run_id, "fingerprint": fingerprint}


@router.post("/runs/{run_id}/governance/findings/{fingerprint}/unmark")
def author_unmark_finding(run_id: str, fingerprint: str,
                          current_user: dict = Depends(get_current_user)):
    """Return a marked (fix_requested) finding to plain `open` — undoes a "Mark for
    fix" the author no longer wants to run."""
    from store.sdlc_store import get_run
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)
    if not _is_run_owner(run, current_user):
        raise HTTPException(status_code=403,
            detail="Only the run owner or an admin may triage governance findings")

    _actor = current_user.get("email") or current_user.get("sub", "user")
    from store.sdlc_governance_findings import set_disposition
    set_disposition(run_id, [fingerprint], "open", _actor)
    logger.info("[SDLC-GOV] author unmark", run_id=run_id, fingerprint=fingerprint,
                actor=_actor)
    return {"status": "open", "run_id": run_id, "fingerprint": fingerprint}


@router.post("/runs/{run_id}/governance/run-fixes")
def author_run_fixes(run_id: str,
                     body: Optional[GovernanceRunFixesRequest] = None,
                     current_user: dict = Depends(get_current_user)):
    """Explicitly TRIGGER the fixer over a batch of marked findings. `fingerprints`
    selects a subset (Fix selected); omitted/empty → ALL currently fix_requested (Fix
    all). Enqueues ONE governance_batch_fix_job (one CLI fixer session + one re-scan
    handles the whole batch — never one job per finding)."""
    from store.sdlc_store import get_run, update_run_state
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)
    if not _is_run_owner(run, current_user):
        raise HTTPException(status_code=403,
            detail="Only the run owner or an admin may run governance fixes")

    _actor = current_user.get("email") or current_user.get("sub", "user")

    from store.sdlc_governance_findings import get_dispositions, set_disposition
    dispositions = get_dispositions(run_id) or {}
    marked = [fp for fp, d in dispositions.items()
              if (d or {}).get("disposition") == "fix_requested"]

    requested = list((body.fingerprints if body else None) or [])
    if requested:
        # Explicit selection — restrict to the requested set; mark any that aren't
        # already fix_requested (defensive; the UI only offers marked ones).
        targets = [fp for fp in requested if fp]
        not_marked = [fp for fp in targets if fp not in marked]
        if not_marked:
            set_disposition(run_id, not_marked, "fix_requested", _actor)
    else:
        # No body → fix everything currently marked.
        targets = marked

    # De-dup preserve order.
    seen = set(); targets = [fp for fp in targets if not (fp in seen or seen.add(fp))]
    if not targets:
        raise HTTPException(status_code=422,
            detail="No findings marked for fixing — mark one or more with Request Fix first.")

    # Flip the rescanning flag now so the UI reflects the running batch immediately,
    # and clear any stale not-converging banner. The batch job clears rescanning on exit.
    try:
        update_run_state(run_id, run.get("state") or "AWAITING_GOVERNANCE_APPROVAL",
                         context_patch={"governance_rescanning": True,
                                        "governance_not_converging": False,
                                        "governance_not_converging_reason": ""})
    except Exception:
        pass

    # Enqueue ONE batch job — the CLI fixer is long, so it MUST NOT run in-request.
    _require_rq()
    from core.job_queue import enqueue_hitl_resume_job
    job_id = enqueue_hitl_resume_job(
        "workers.sdlc_worker.governance_batch_fix_job", run_id,
        extra={"fingerprints": targets, "actor": _actor})

    logger.info("[SDLC-GOV] author run-fixes (batch)", run_id=run_id,
                count=len(targets), actor=_actor, job_id=job_id)
    return {"status": "fixing", "enqueued": True, "run_id": run_id,
            "count": len(targets), "fingerprints": targets, "job_id": job_id}


@router.post("/runs/{run_id}/governance/submit-to-teams")
def author_submit_to_teams(run_id: str,
                           current_user: dict = Depends(get_current_user)):
    from store.sdlc_store import get_run, update_run_state
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    _authorize_run(run, current_user)
    if not _is_run_owner(run, current_user):
        raise HTTPException(status_code=403,
            detail="Only the run owner or an admin may submit to domain teams")

    _actor = current_user.get("email") or current_user.get("sub", "user")

    # Ensure per-domain approvals are seeded (the end-gate already does this; this
    # is belt-and-braces for a run where seeding was skipped/lost).
    try:
        from store.sdlc_governance_approvers import (
            list_domain_approvals, seed_domain_approvals, reset_domain_to_pending,
        )
        from store.sdlc_governance_findings import domain_open_counts
        approvals = list_domain_approvals(run_id)
        if not approvals:
            seed_domain_approvals(run_id, domain_open_counts(run_id))
        else:
            # RE-SEND after a send-back: any domain the teams bounced back
            # (changes_requested) is reset to pending so those teams re-review the
            # author's fixes. Already-approved domains are left untouched (their
            # carry-forward is handled on the next scan snapshot). This is what
            # scopes the re-send to only the teams still pending approval.
            for a in approvals:
                if (a.get("status") or "") == "changes_requested":
                    reset_domain_to_pending(run_id, (a.get("domain") or ""))

        # RE-SEND also clears stale team `send_back` decisions on the CURRENT snapshot
        # so the bounced findings are team-decidable again WITHOUT requiring a re-scan.
        # A send-back decision is snapshot-keyed and otherwise only cleared by minting a
        # new snapshot; when the author addresses it by mark-FP / re-triage (no re-scan)
        # the stale decision would keep the team board locked ("awaiting author fix") and
        # the domain un-approvable (decide_governance_domain 409s on un-actioned send-
        # backs). Clearing run-wide is safe: an APPROVED domain cannot hold a send_back
        # (the approve gate blocks that), so already-approved teams are never disturbed.
        try:
            from store.sdlc_governance_findings import latest_snapshot
            from store.sdlc_governance_approvers import clear_send_back_decisions
            _snap = latest_snapshot(run_id)
            if _snap and _snap.get("id"):
                clear_send_back_decisions(run_id, _snap["id"])
        except Exception as _cs:
            logger.warning("[SDLC-GOV] submit-to-teams: could not clear stale send_back decisions — non-fatal",
                           run_id=run_id, error=str(_cs))
    except Exception as _se:
        logger.warning("[SDLC-GOV] submit-to-teams: approval seed check failed — non-fatal",
                       run_id=run_id, error=str(_se))

    # Keep AWAITING_GOVERNANCE_APPROVAL; flip a context flag so the UI + team
    # endpoints know the author has finished triage (same-state context patch).
    update_run_state(run_id, "AWAITING_GOVERNANCE_APPROVAL",
                     context_patch={"governance_submitted_to_teams": True})

    # Best-effort: email the governance-team approvers for every domain still
    # awaiting review that a run has been submitted for their sign-off. Guarded
    # so an SMTP/relay problem can NEVER 500 the submit or break the transition.
    try:
        from services.governance_email_service import notify_governance_teams_submitted
        notify_governance_teams_submitted(run_id)
    except Exception as _me:
        logger.warning("[SDLC-GOV] submit-to-teams: team notification failed — non-fatal",
                       run_id=run_id, error=str(_me))

    logger.info("[SDLC-GOV] author submit-to-teams", run_id=run_id, actor=_actor)
    return {"status": "submitted_to_teams", "run_id": run_id}


# ─────────────────────────────────────────────────────────────
# GET/POST/DELETE /sdlc/governance/domain-approvers
# Admin-only domain-approver management.
# ─────────────────────────────────────────────────────────────

@router.get("/governance/domain-approvers")
def list_governance_approvers(domain: Optional[str] = None,
                              current_user: dict = Depends(get_current_user)):
    from auth.rbac import is_admin
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin only")
    from store.sdlc_governance_approvers import list_approvers
    return {"approvers": list_approvers(domain=domain.upper() if domain else None)}


@router.post("/governance/domain-approvers")
def add_governance_approver(req: GovernanceApproverAddRequest,
                            current_user: dict = Depends(get_current_user)):
    from auth.rbac import is_admin
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin only")
    # B4.2: server-side input validation (domain + email format) — mirrors the
    # client check but is the authoritative gate.
    _domain = (req.domain or "").strip().upper()
    _email = (req.email or "").strip()
    if not _domain or not _email:
        raise HTTPException(status_code=400, detail="domain and email are required")
    if len(_domain) > 32:
        raise HTTPException(status_code=400, detail="domain too long (max 32 chars)")
    import re as _re_email
    if not _re_email.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", _email):
        raise HTTPException(status_code=400, detail="invalid email format")
    _created_by = current_user.get("email") or current_user.get("sub", "admin")
    from store.sdlc_governance_approvers import add_approver
    add_approver(_domain, _email, req.user_id or "", _created_by)
    # Audit trail: who added whom, when (NOW() persisted as created_by/created_at
    # on the approver row; this log line is the governance audit event).
    logger.info("[SDLC-GOV] AUDIT approver added", domain=_domain,
                email=_email, added_by=_created_by)
    return {"ok": True, "domain": _domain, "email": _email}


@router.delete("/governance/domain-approvers")
def remove_governance_approver(domain: str, email: str,
                               current_user: dict = Depends(get_current_user)):
    """Soft-delete an approver. The store keys approvers by (domain, email) — it
    has no remove-by-UUID API — so this endpoint takes `domain` + `email` query
    params (not a path id) and calls remove_approver(domain, email) directly."""
    from auth.rbac import is_admin
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin only")
    if not (domain or "").strip() or not (email or "").strip():
        raise HTTPException(status_code=400, detail="domain and email are required")
    from store.sdlc_governance_approvers import remove_approver
    remove_approver(domain.upper(), email)
    # Audit trail: soft-delete (the store sets active=FALSE, never hard-deletes),
    # recording who removed whom — the governance audit event on remove.
    logger.info("[SDLC-GOV] AUDIT approver removed", domain=domain.upper(),
                email=email, removed_by=current_user.get("email") or current_user.get("sub", "admin"))
    return {"ok": True, "domain": domain.upper(), "email": email}
