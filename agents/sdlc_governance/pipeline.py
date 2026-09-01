# SPDX-License-Identifier: Apache-2.0
"""Standalone governance pipeline (run_type="governance").

Extracted from ``agents/sdlc_pipeline.py`` on 2026-08-04 as stage 1 of the
Part B decomposition. This is the self-contained governance flow that is
INDEPENDENT of the feature/bug CodingStateMachine (scan an already-pushed branch
against the EA/IS/DPDP skills -> per-domain HITL approval gate -> auto-fixer ->
commit -> MR). Every public name here is re-exported from ``agents.sdlc_pipeline``
so existing ``from agents.sdlc_pipeline import run_governance_pipeline`` imports
(workers, routers, state machine) keep resolving unchanged.
"""

import os

from core.logger import logger, bind_context, clear_bound_context, set_request_id
from store.sdlc_store import (
    create_run,
    get_run,
    update_run_state,
    add_run_event,
    patch_run_context,
    SDLCCancelled,
)

# Non-governance pipeline helpers this block depends on. They live in the leaf
# module ``agents.sdlc_pipeline._core`` (which imports nothing from this package),
# so importing them from the SUBMODULE (not the package facade) keeps module load
# acyclic regardless of import order. The sdlc_pipeline package __init__ resolves
# governance names lazily (module __getattr__), so it never imports this module at
# package-init time.
from agents.sdlc_pipeline._core import (
    _authenticated_clone_url,
    _resolve_gitlab_repo,
    _transition,
    _gov_resolve_gitlab_token,
)
# run_governance_scan_snapshot moved to ._phases in stage 2 of the decomposition.
from agents.sdlc_pipeline._phases import run_governance_scan_snapshot


# ============================================================
# STANDALONE GOVERNANCE PIPELINE  (run_type="governance")
#
# A self-contained governance flow that is INDEPENDENT of the
# feature/bug CodingStateMachine. It scans an already-pushed
# branch (head_branch) against the EA/IS/DPDP governance skills,
# suspends to a per-domain HITL approval gate, and — once every
# finding-domain is approved — runs an auto-fixer, commits the
# fix onto head_branch, and opens an MR head_branch → base_branch.
#
# Stage flow (see store/sdlc_artifacts.stage_sequence_for):
#   GOVERNANCE_SCAN → [AWAITING_GOVERNANCE_APPROVAL]
#   → GOVERNANCE_FIX → GOVERNANCE_REVERIFY → COMMITTING → MR_CREATION
#
# The feature/bug pipeline and CodingStateMachine are NOT touched.
# ============================================================


def _gov_workspace_dir(run_id: str) -> str:
    """Deterministic per-run governance workspace path (shared by scan + resume).

    Lives under ``BUILDER_WORKSPACE_ROOT/runs/{run_id}_gov`` — a sibling of the SDLC
    pipeline's own ``runs/{run_id}_{slug}`` workspaces — NOT the OS temp dir, so all
    SDLC scratch sits under one governed base path."""
    from core.config import BUILDER_WORKSPACE_ROOT
    return os.path.join(BUILDER_WORKSPACE_ROOT, "runs", f"{run_id}_gov")

def _gov_resolve_clone_url(repo: str, gl_url: str, gl_token: str,
                           user_id: str = "", user_email: str = "") -> str:
    """Resolve the clone URL for a governance workspace.

    Prefers ``repo_index_status.git_url`` — the SAME source the feature/bug
    pipeline clones from (``sdlc_state_machine._ensure_run_workspace``). This is
    what makes governance honor the local GitLab mock (a ``file://`` git_url is
    seeded there) AND, in production, clone the exact registered origin with the
    triggering user's own PAT re-injected. Falls back to the ``GITLAB_URL``-derived
    ``oauth2:<token>`` URL only when the repo is not registered in the index.
    """
    try:
        from db.database import engine as _eng
        from sqlalchemy import text as _txt
        from agents.sdlc_context import normalize_repo_index_key_without_prefix as _nrik
        _canon = _nrik(repo)
        for _slug in (_canon, repo):
            if not _slug:
                continue
            with _eng.connect() as _c:
                _row = _c.execute(
                    _txt("SELECT git_url FROM repo_index_status WHERE repo_name=:s"),
                    {"s": _slug},
                ).fetchone()
            if _row and _row.git_url:
                from core.platform_credentials import build_run_clone_url as _burl
                url = _burl(_row.git_url, user_id=user_id or "", email=user_email or "")
                logger.info("[SDLC-GOV] clone url from repo_index_status",
                            repo=repo, slug=_slug)
                return url
    except Exception as e:
        logger.warning("[SDLC-GOV] repo_index_status lookup failed (falling back to GITLAB_URL)",
                       repo=repo, error=str(e))
    return _authenticated_clone_url(repo, gl_url, gl_token)


def _gov_clone_workspace(run_id: str, repo: str, head_branch: str,
                         gl_url: str, gl_token: str,
                         user_id: str = "", user_email: str = "") -> str:
    """Fresh-clone ``head_branch`` of ``repo`` into the governance workspace.
    Returns the workspace path on success, "" on failure. Never raises."""
    import shutil
    import subprocess
    ws = _gov_workspace_dir(run_id)
    try:
        if os.path.isdir(ws):
            shutil.rmtree(ws, ignore_errors=True)
        os.makedirs(ws, exist_ok=True)
        clone_url = _gov_resolve_clone_url(repo, gl_url, gl_token, user_id, user_email)
        cmd = ["git", "clone"]
        if head_branch:
            cmd += ["--branch", head_branch]
        cmd += [clone_url, ws]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            logger.error("[SDLC-GOV] clone failed", run_id=run_id, repo=repo,
                         head_branch=head_branch, stderr=(r.stderr or "")[-500:])
            return ""
        return ws
    except Exception as e:
        logger.error("[SDLC-GOV] clone errored", run_id=run_id, repo=repo, error=str(e))
        return ""


def _gov_git_diff(workspace: str, base_ref: str) -> tuple:
    """Return (changed_files, diff_text) for ``{base_ref}...HEAD`` in ``workspace``.
    Falls back to ``origin/{base_ref}`` when the bare ref is unknown locally.
    Never raises — returns ([], "") on any failure."""
    import subprocess

    def _try(ref: str):
        try:
            names = subprocess.run(
                ["git", "diff", f"{ref}...HEAD", "--name-only"],
                cwd=workspace, capture_output=True, text=True, timeout=60,
            )
            if names.returncode != 0:
                return None
            body = subprocess.run(
                ["git", "diff", f"{ref}...HEAD"],
                cwd=workspace, capture_output=True, text=True, timeout=120,
            )
            files = [ln.strip() for ln in (names.stdout or "").splitlines() if ln.strip()]
            return files, (body.stdout or "")
        except Exception:
            return None

    for ref in [r for r in (base_ref, f"origin/{base_ref}") if r]:
        out = _try(ref)
        if out is not None:
            return out
    return [], ""


def _gov_commit_and_push(workspace: str, push_branch: str, *, run_id: str,
                         message: str, author_name: str = "",
                         author_email: str = "") -> tuple:
    """Stage + commit the governance fixer's working-tree changes and push to
    ``origin HEAD:{push_branch}`` so the fix reaches origin and the subsequent
    re-scan diff (``git diff base...HEAD``) actually SEES it. ``push_branch`` is the
    governance FIX branch (not the developer's scanned branch); the post-approval MR
    is opened fix_branch → scanned_branch.

    The commit ALWAYS passes ``-c user.name`` / ``-c user.email``: the fresh
    governance clone (``_gov_clone_workspace``) never configures a git identity, so a
    bare ``git commit`` fails with "Author identity unknown" on any host without a
    global identity — and that failure was silently swallowed as ``(False, False)``,
    which is exactly the "changes staged but never committed/pushed" symptom. Prefer
    the triggering user's identity; fall back to a platform bot.

    Returns ``(committed: bool, pushed: bool)``. Idempotent: a clean tree
    ("nothing to commit") returns ``(False, False)`` so the caller STOPS the loop
    rather than looping forever or pushing an empty change. The origin URL was
    cloned with the triggering user's embedded PAT (``_gov_ensure_workspace``), so
    the push authenticates without mutating the process-wide GITLAB_TOKEN. Never
    raises. NOTE: ``.governance_skills/`` / ``.governance_diff/`` are git-excluded,
    so ``git add -A`` never stages the read-only staged skills."""
    import subprocess

    _name  = (author_name or "").strip()  or "AiNxt AI"
    _email = (author_email or "").strip() or os.getenv("SDLC_BOT_EMAIL", "ainxt-bot@example.com")

    def _run(args, timeout=120):
        try:
            return subprocess.run(["git"] + args, cwd=workspace,
                                  capture_output=True, text=True, timeout=timeout)
        except Exception as _e:
            logger.warning("[SDLC-GOV] git op failed", run_id=run_id, args=args, error=str(_e))
            return None

    _run(["add", "-A"])
    # `git diff --cached --quiet` exits 0 when the index is clean, 1 when staged
    # changes exist — the reliable "is there anything to commit?" check.
    _st = _run(["diff", "--cached", "--quiet"])
    if _st is not None and _st.returncode == 0:
        return (False, False)   # nothing staged → nothing to commit

    # `-c user.name/-c user.email` MUST precede the `commit` subcommand. Without an
    # identity the commit exits non-zero ("Please tell me who you are") and never lands.
    _c = _run(["-c", f"user.name={_name}", "-c", f"user.email={_email}",
               "commit", "-m", message])
    if _c is None or _c.returncode != 0:
        _out = ((_c.stdout if _c else "") + (_c.stderr if _c else "")).lower()
        if "nothing to commit" in _out:
            return (False, False)
        logger.warning("[SDLC-GOV] git commit failed (treating as no-change)", run_id=run_id,
                       out=(_c.stdout if _c else ""), err=(_c.stderr if _c else ""))
        return (False, False)

    pushed = False
    if push_branch:
        _p = _run(["push", "origin", f"HEAD:{push_branch}"], timeout=180)
        pushed = bool(_p is not None and _p.returncode == 0)
        if not pushed:
            logger.warning("[SDLC-GOV] git push failed (fix committed locally, MR may lag)",
                           run_id=run_id, branch=push_branch, err=(_p.stderr if _p else ""))
    else:
        logger.warning("[SDLC-GOV] no push branch resolved for governance fix",
                       run_id=run_id)
    return (True, pushed)


def _gov_prepare_fix_branch(workspace: str, scanned_branch: str, fix_branch: str,
                            *, run_id: str) -> bool:
    """Check out the governance FIX branch in ``workspace`` so every fixer commit
    lands on it, NEVER on the developer's scanned branch. If origin already has the
    fix branch (a prior 'Run fixes' round on this run), fetch + check it out so
    earlier fix commits are preserved; otherwise create it off the current HEAD (the
    scanned branch tip). Idempotent — a no-op when already on the fix branch. Returns
    True on success. Never raises."""
    import subprocess

    def _run(args, timeout=120):
        try:
            return subprocess.run(["git"] + args, cwd=workspace,
                                  capture_output=True, text=True, timeout=timeout)
        except Exception as _e:
            logger.warning("[SDLC-GOV] git op failed", run_id=run_id, args=args, error=str(_e))
            return None

    # Already on the fix branch (same batch job, later iteration) → nothing to do.
    _cur = _run(["rev-parse", "--abbrev-ref", "HEAD"])
    if _cur is not None and _cur.returncode == 0 and (_cur.stdout or "").strip() == fix_branch:
        return True

    # Reuse an existing remote fix branch (a prior round) to keep earlier fix commits.
    _fetch = _run(["fetch", "origin", fix_branch], timeout=120)
    if _fetch is not None and _fetch.returncode == 0:
        _co = _run(["checkout", "-B", fix_branch, "FETCH_HEAD"])
        if _co is not None and _co.returncode == 0:
            return True

    # First round → create the fix branch off the current (scanned) HEAD.
    _co = _run(["checkout", "-B", fix_branch])
    ok = bool(_co is not None and _co.returncode == 0)
    if not ok:
        logger.error("[SDLC-GOV] could not create governance fix branch",
                     run_id=run_id, fix_branch=fix_branch, scanned=scanned_branch,
                     err=(_co.stderr if _co else ""))
    return ok


def _governance_preflight(run_id: str, product_id, repo: str, base_branch: str,
                          base_commit: str, head_branch: str) -> bool:
    """Validate GitLab credentials + connectivity for a standalone governance run.

    Relies on the caller having already set the resolved GitLab token in
    ``gitlab_tools`` thread-local via ``set_token()`` (run_governance_pipeline
    does this). Checks, in order:
      1. GitLab token present (thread-local → GITLAB_TOKEN env)      — HARD
      2. GitLab repo reachable (GET /projects/{repo})                — HARD on 404
      3. base_commit resolves (GET /repository/commits/{sha})        — SOFT
      4. head_branch exists (GET /repository/branches/{branch})      — HARD on 404

    Hard failure → marks the run FAILED and returns False. Soft issues warn and
    continue. Non-404 transport errors are treated as soft (transient network),
    mirroring _preflight_check.
    """
    import json as _json
    import urllib.request
    import urllib.error
    from urllib.parse import quote as _q

    gl_url = os.getenv("GITLAB_URL", "https://gitlab.example.com").rstrip("/")

    # ── 1. token (already set in thread-local by the caller) ──────────────────
    gl_token = ""
    try:
        from tools.gitlab_tools import _resolve_token as _gl_resolve
        gl_token = _gl_resolve() or ""
    except Exception:
        gl_token = ""
    if not gl_token:
        gl_token = os.getenv("GITLAB_TOKEN", "")
    if not gl_token:
        reason = "no_gitlab_token"
        logger.error("[SDLC-GOV] preflight hard failure", run_id=run_id, reason=reason,
                     repo=repo, head_branch=head_branch)
        update_run_state(run_id, "FAILED", current_stage="GOVERNANCE_SCAN",
                         error="Governance pre-flight FAILED: no GitLab token available "
                               "(add a GitLab PAT under Profile → GitLab Token, or set "
                               "GITLAB_TOKEN for service-triggered runs).")
        return False

    _proj = _q(repo, safe="") if repo else ""

    def _gl_get(path: str, timeout: int = 8):
        req = urllib.request.Request(
            f"{gl_url}/api/v4/projects/{_proj}{path}",
            headers={"PRIVATE-TOKEN": gl_token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode())

    # ── 2. repo connectivity (HARD on 404) ───────────────────────────────────
    if not (repo and "/" in repo):
        reason = "repo_not_namespaced"
        logger.error("[SDLC-GOV] preflight hard failure", run_id=run_id, reason=reason, repo=repo)
        update_run_state(run_id, "FAILED", current_stage="GOVERNANCE_SCAN",
                         error=f"Governance pre-flight FAILED: repo {repo!r} is not in "
                               "namespace/project form.")
        return False
    try:
        _data = _gl_get("")
        logger.info("[SDLC-GOV] preflight repo access OK", run_id=run_id, repo=repo,
                    name=_data.get("name_with_namespace", repo),
                    default_branch=_data.get("default_branch", ""))
    except urllib.error.HTTPError as he:
        if he.code == 404:
            reason = "repo_404"
            logger.error("[SDLC-GOV] preflight hard failure", run_id=run_id, reason=reason,
                         repo=repo, gitlab_url=gl_url)
            update_run_state(run_id, "FAILED", current_stage="GOVERNANCE_SCAN",
                             error=f"Governance pre-flight FAILED: GitLab 404 on repo {repo!r}. "
                                   "Check the repo path and token scope.")
            return False
        logger.warning("[SDLC-GOV] preflight repo check non-fatal", run_id=run_id, repo=repo,
                       http=he.code)
    except Exception as e:
        logger.warning("[SDLC-GOV] preflight repo check non-fatal", run_id=run_id, repo=repo,
                       error=str(e))

    # ── 3. base_commit resolvable (SOFT) ──────────────────────────────────────
    if base_commit:
        try:
            _gl_get(f"/repository/commits/{_q(base_commit, safe='')}")
            logger.info("[SDLC-GOV] preflight base_commit OK", run_id=run_id,
                        base_commit=base_commit)
        except Exception as e:
            logger.warning("[SDLC-GOV] preflight base_commit not verifiable (soft)",
                           run_id=run_id, base_commit=base_commit, error=str(e))

    # ── 4. head_branch exists (HARD on 404) ───────────────────────────────────
    if head_branch:
        try:
            _gl_get(f"/repository/branches/{_q(head_branch, safe='')}")
            logger.info("[SDLC-GOV] preflight head_branch OK", run_id=run_id,
                        head_branch=head_branch)
        except urllib.error.HTTPError as he:
            if he.code == 404:
                reason = "head_branch_not_found"
                logger.error("[SDLC-GOV] preflight hard failure", run_id=run_id, reason=reason,
                             repo=repo, head_branch=head_branch)
                update_run_state(run_id, "FAILED", current_stage="GOVERNANCE_SCAN",
                                 error=f"Governance pre-flight FAILED: branch {head_branch!r} "
                                       f"not found in {repo!r}.")
                return False
            logger.warning("[SDLC-GOV] preflight head_branch check non-fatal", run_id=run_id,
                           head_branch=head_branch, http=he.code)
        except Exception as e:
            logger.warning("[SDLC-GOV] preflight head_branch check non-fatal", run_id=run_id,
                           head_branch=head_branch, error=str(e))

    return True


def run_governance_pipeline(issue: dict, run_id=None) -> str:
    """Standalone governance pipeline: scan → per-domain HITL gate → fix → MR.

    issue keys: product_id, repo, base_branch, base_commit, head_branch,
                governance_skills (optional subset), triggered_by_user_id,
                triggered_by_email
    """
    repo         = (issue.get("repo") or "").strip()
    base_branch  = issue.get("base_branch") or "main"
    base_commit  = issue.get("base_commit") or ""
    head_branch  = issue.get("head_branch") or ""
    subset       = issue.get("governance_skills")
    user_id      = issue.get("triggered_by_user_id", "")
    user_email   = issue.get("triggered_by_email", "")

    # ── 1. create / resolve run ───────────────────────────────────────────────
    if run_id:
        run = get_run(run_id) or create_run(
            run_type="governance", repo=repo, jira_key=head_branch,
            jira_summary=f"Governance scan: {repo}", triggered_by="api",
            created_by=user_id,
        )
        run_id = run["id"]
    else:
        run = create_run(
            run_type="governance", repo=repo, jira_key=head_branch,
            jira_summary=f"Governance scan: {repo}", triggered_by="api",
            created_by=user_id,
        )
        run_id = run["id"]

    # ── 2. context ────────────────────────────────────────────────────────────
    bind_context(correlation_id=run_id, pipeline_stage="governance_pipeline")

    try:
        # Persist the trigger fields into the run context so the HITL-resume path
        # (a separate process) can rebuild the workspace + branch state.
        update_run_state(run_id, run.get("state", "CREATED"), context_patch={
            "repo":         repo,
            "base_branch":  base_branch,
            "base_commit":  base_commit,
            "head_branch":  head_branch,
            "product_id":   issue.get("product_id") or "",
            "user_id":      user_id,
            "user_email":   user_email,
            "governance_skills": subset or [],
        })

        # ── 3. resolve + set GitLab token (thread-local for concurrent workers) ──
        gl_url   = os.getenv("GITLAB_URL", "https://gitlab.example.com").rstrip("/")
        gl_token = _gov_resolve_gitlab_token(user_id)
        if gl_token:
            from tools.gitlab_tools import set_token as _gl_set_token
            _gl_set_token(gl_token)

        # ── 4. preflight ────────────────────────────────────────────────────────
        if not _governance_preflight(run_id, issue.get("product_id"), repo,
                                      base_branch, base_commit, head_branch):
            return run_id

        # ── 4b. HOD budget preflight ─────────────────────────────────────────────
        # Governance runs consume LLM tokens (one agentic scan session per skill),
        # so they participate in HOD budget governance exactly like feature/bug runs.
        # This writes sdlc_runs.hod_email so finalize_run_budget() can attribute the
        # cost at run-end (instead of "finalize skip — no hod_email"). Hard failure
        # only when enforcement is on and the HOD cap is exhausted.
        try:
            from services.sdlc_budget_tracker import check_hod_budget as _chk_hod
            _hod_ok, _hod_err = _chk_hod(user_id=user_id, run_id=run_id, user_email=user_email)
            if not _hod_ok:
                logger.error("[SDLC-GOV] HOD budget preflight blocked run", run_id=run_id,
                             reason=_hod_err)
                update_run_state(run_id, "FAILED", current_stage="GOVERNANCE_SCAN",
                                 error=_hod_err)
                return run_id
        except Exception as _hod_exc:
            logger.warning("[SDLC-GOV] HOD budget preflight error (non-blocking)",
                           run_id=run_id, error=str(_hod_exc))

        # ── 5. GOVERNANCE_SCAN transition ────────────────────────────────────────
        _transition(run_id, "GOVERNANCE_SCAN", "governance-scanner")

        # ── 6. clone workspace (head_branch) ─────────────────────────────────────
        workspace = _gov_clone_workspace(run_id, repo, head_branch, gl_url, gl_token,
                                         user_id=user_id, user_email=user_email)
        if not workspace:
            update_run_state(run_id, "FAILED", current_stage="GOVERNANCE_SCAN",
                             error=f"Governance scan: failed to clone {repo!r}@{head_branch!r}.")
            return run_id
        update_run_state(run_id, "GOVERNANCE_SCAN", context_patch={"workspace": workspace})

        # ── 7. compute diff ──────────────────────────────────────────────────────
        base_ref = base_commit or base_branch
        changed_files, diff_text = _gov_git_diff(workspace, base_ref)
        logger.info("[SDLC-GOV] diff captured", run_id=run_id, repo=repo,
                    base_commit=base_commit, head_branch=head_branch,
                    changed_files=len(changed_files))

        # EMPTY-DIFF GUARD (2026-07-30): the scan runs on a FRESH CLONE of
        # origin/<head_branch>. An empty diff (no changes over the base) means the
        # branch's commits never reached origin (unpushed local changes) or a
        # base/branch misresolution. Scanning it writes empty .patch files and would
        # FALSE-GREEN the gate — SUSPEND with an actionable message instead.
        if not changed_files or not (diff_text or "").strip():
            _empty_msg = (
                f"Governance scan found no changes on '{head_branch}' over "
                f"'{base_ref}'. The branch has no diff versus its base — usually the "
                "commits were not pushed to origin. Ensure the changes are committed "
                f"and pushed to origin/{head_branch}, then retry governance."
            )
            logger.error("[SDLC-GOV] standalone diff is EMPTY — suspending",
                         run_id=run_id, repo=repo, head_branch=head_branch,
                         base_ref=base_ref)
            update_run_state(run_id, "SUSPENDED", current_stage="GOVERNANCE_SCAN",
                             error=_empty_msg)
            add_run_event(run_id, "GOVERNANCE_SCAN", "SUSPENDED",
                          stage="GOVERNANCE_SCAN", actor="governance-scanner")
            # No issues found (empty diff → nothing to scan) is still a
            # governance outcome that needs an audit trail — export the
            # (near-empty) evidence bundle so a linked Jira Change ticket is
            # always created + attached, with the "no issues" state and run
            # metadata recorded. Best-effort / idempotent.
            _enqueue_governance_evidence_final(run_id, actor="governance-scanner")
            return run_id

        # ── 8-10. unified scan core (cap → select → per-skill scan → suppress →
        #    persist findings + snapshot → report). SAME primitive the end-gate and
        #    the standalone worker use — this is what makes every trigger spawn one
        #    parallel session per skill (scan-unify 2026-07-28). ──────────────────
        from agents.sdlc_governance import engine as gov_engine, config as gov_config
        from store.sdlc_governance_findings import domain_open_counts
        from store.sdlc_governance_approvers import seed_domain_approvals
        from store.sdlc_artifacts import _store_artifact

        try:
            from db.database import SessionLocal
            db = SessionLocal()
        except Exception:
            db = None
        product_id = issue.get("product_id") or gov_engine.resolve_product_id(db, repo)

        res = run_governance_scan_snapshot(
            run_id, workspace=workspace, diff_text=diff_text, changed_files=changed_files,
            product_id=product_id, repo=repo, base_sha=base_commit or "HEAD",
            subset=subset, db=db, trigger="initial", created_by=user_email,
        )
        if db:
            db.close()

        # Diff too large OR scan CLI could not complete → SUSPEND for manual retry.
        if res.get("scan_error"):
            _detail = res.get("scan_error_detail") or ""
            _suspend_msg = (
                f"Governance scan not run — {_detail}"
                if res.get("diff_too_large")
                else (f"Governance scan could not complete ({_detail or 'CLI error'}). "
                      "Increase SDLC_GOVERNANCE_SCAN_TURNS and retry.")
            )
            logger.warning("[SDLC-GOV] scan engine failure → SUSPEND",
                           run_id=run_id, reason=_detail,
                           diff_too_large=bool(res.get("diff_too_large")))
            update_run_state(run_id, "SUSPENDED", current_stage="GOVERNANCE_SCAN",
                             error=_suspend_msg)
            add_run_event(run_id, "GOVERNANCE_SCAN", "SUSPENDED",
                          stage="GOVERNANCE_SCAN", actor="governance-scanner")
            # Scan could not complete (diff too large / CLI error) → no issues
            # were recorded, but this is still a governance outcome that needs an
            # audit trail. Export the (near-empty) evidence bundle so a linked
            # Jira Change ticket is always created + attached, capturing the
            # suspend reason and run metadata. Best-effort / idempotent.
            _enqueue_governance_evidence_final(run_id, actor="governance-scanner")
            return run_id

        # No bundle/skills resolved → nothing to scan.
        if res.get("skipped"):
            update_run_state(run_id, "COMPLETE", current_stage="COMPLETE",
                             error="No governance skills resolved — nothing to scan")
            add_run_event(run_id, "GOVERNANCE_SCAN", "COMPLETE", stage="COMPLETE",
                          actor="governance-scanner")
            # Nothing scanned → still export a (near-empty) evidence bundle so
            # every terminal governance run gets a linked Jira Change ticket.
            _enqueue_governance_evidence_final(run_id, actor="governance-scanner")
            return run_id

        open_f = res.get("open_findings") or []
        suppressed_f = res.get("suppressed") or []
        report = res.get("report")
        _store_artifact(run_id, "GOVERNANCE_REPORT", report, "governance-scanner", "", "system")

        # ── 11. per-domain team sign-off gate (clean-PASS acknowledge, 2026-07-30) ─
        # EVERY scanned domain now requires explicit team acknowledgement — including a
        # clean PASS (zero findings). Seed a 'pending' row per scanned domain (count 0
        # for clean ones). Only when NO domain was classified at all is there nothing to
        # acknowledge → COMPLETE (so the run never stalls with an empty gate).
        counts = domain_open_counts(run_id)
        scanned_domains = {
            (d or "").strip().upper()
            for d in (res.get("domain_by_skill") or {}).values()
            if (d or "").strip()
        }
        all_domains = scanned_domains | set(counts.keys())

        if not open_f and not all_domains:
            update_run_state(run_id, "COMPLETE", current_stage="COMPLETE")
            add_run_event(run_id, "GOVERNANCE_SCAN", "COMPLETE", stage="COMPLETE",
                          actor="governance-scanner")
            # Terminal COMPLETE — always export the final evidence bundle (a
            # clean PASS with no findings still gets a Jira Change ticket).
            _enqueue_governance_evidence_final(run_id, actor="governance-scanner")
            return run_id

        # ── 12. seed per-domain approvals (all scanned domains) ──────────────────
        seed_domain_approvals(run_id, counts, all_domains=all_domains)
        logger.info("[SDLC-GOV] suspend to approval gate", run_id=run_id,
                    domains=sorted(all_domains), open=len(open_f),
                    clean_pass=not open_f, suppressed=len(suppressed_f))

        # ── 13. suspend to AWAITING_GOVERNANCE_APPROVAL ──────────────────────────
        update_run_state(run_id, "AWAITING_GOVERNANCE_APPROVAL",
                         current_stage="GOVERNANCE_APPROVAL",
                         context_patch={"awaiting_domain_approvals": sorted(all_domains)})
        add_run_event(run_id, "GOVERNANCE_SCAN", "AWAITING_GOVERNANCE_APPROVAL",
                      stage="GOVERNANCE_APPROVAL", actor="governance-scanner")
        return run_id

    except SDLCCancelled:
        logger.info("[SDLC-GOV] governance pipeline stopped — run cancelled", run_id=run_id)
        return run_id
    except Exception as e:
        logger.error("[SDLC-GOV] governance pipeline failed", run_id=run_id, error=str(e))
        update_run_state(run_id, "FAILED", error=f"Governance pipeline error: {e}")
        return run_id


def _gov_ensure_workspace(run_id: str, ctx: dict) -> str:
    """Return a usable governance workspace for a resume/trigger step.

    Workspace-identity fix (2026-07-30): the author-fix / re-scan MUST operate on the
    SAME tree that produced the findings, not a fresh clone of the wrong branch. The
    standalone governance pipeline persists `ctx["workspace"]`; the IN-PIPELINE
    feature/bug end-gate persists `ctx["workspace_root"]` (= runs/{run_id}_{slug}, the
    tree the end-gate actually scanned) and `working_branch` — it never sets
    `workspace`/`head_branch`. Previously this read only `ctx["workspace"]`/`head_branch`,
    so a feature/bug run fell through to a fresh clone of `runs/{run_id}_gov` at an EMPTY
    branch (→ default/base branch → HEAD==base → empty diff → the SAME findings on
    re-scan). Now: reuse the first existing checkout (workspace → workspace_root → _gov),
    and if none is on disk, re-clone using the WORKING branch (head_branch →
    working_branch → run.branch), NEVER an empty value. Returns "" on failure."""
    for _cand in (ctx.get("workspace"), ctx.get("workspace_root"),
                  _gov_workspace_dir(run_id)):
        if _cand and os.path.isdir(os.path.join(_cand, ".git")):
            return _cand
    repo        = ctx.get("repo", "")
    head_branch = (ctx.get("head_branch") or ctx.get("working_branch")
                   or (get_run(run_id) or {}).get("branch") or "")
    if not head_branch:
        logger.error("[SDLC-GOV] _gov_ensure_workspace: no working branch resolved — "
                     "refusing to clone (would fetch the base branch → empty diff)",
                     run_id=run_id)
        return ""
    gl_url      = os.getenv("GITLAB_URL", "https://gitlab.example.com").rstrip("/")
    gl_token    = _gov_resolve_gitlab_token(ctx.get("user_id", ""))
    if gl_token:
        try:
            from tools.gitlab_tools import set_token as _gl_set_token
            _gl_set_token(gl_token)
        except Exception:
            pass
    ws = _gov_clone_workspace(run_id, repo, head_branch, gl_url, gl_token,
                              user_id=ctx.get("user_id", ""),
                              user_email=ctx.get("user_email", ""))
    if ws:
        update_run_state(run_id, get_run(run_id).get("state", ""),
                         context_patch={"workspace": ws})
    return ws


def resume_governance_fix(run_id: str, actor: str = "user") -> str:
    """Resume after all domains are approved. Called only after
    all_finding_domains_approved. Runs the auto-fixer over the remaining OPEN
    findings, commits onto head_branch, and opens an MR head_branch → base_branch."""
    import re
    import subprocess

    bind_context(correlation_id=run_id, pipeline_stage="governance_fix")

    from store.sdlc_governance_approvers import (
        all_finding_domains_approved, list_domain_approvals,
    )
    # ── 1. fail-closed guard: every seeded domain must be approved ────────────
    if not all_finding_domains_approved(run_id):
        pending = [d["domain"] for d in list_domain_approvals(run_id)
                   if d.get("status") != "approved"]
        logger.warning("[SDLC-GOV] resume_governance_fix: not all domains approved — no-op",
                       run_id=run_id, pending_domains=pending)
        return run_id

    try:
        # ── 2. load run context ───────────────────────────────────────────────
        run = get_run(run_id) or {}
        ctx = run.get("context") or {}
        repo        = run.get("repo") or ctx.get("repo", "")
        head_branch = ctx.get("head_branch", "")     # the scanned branch = MR TARGET
        base_branch = ctx.get("base_branch", "main")
        base_commit = ctx.get("base_commit", "")
        # The governance FIX branch (set by run_governance_batch_fix when it committed
        # any fix). Empty → nothing was ever fixed → no MR is needed.
        fix_branch  = ctx.get("governance_fix_branch", "")

        # ── author GitLab token (thread-local) ────────────────────────────────
        # This is a FRESH rq resume job: the gitlab_tools thread-local is empty, so
        # gitlab_create_mr below would otherwise fall back to the GITLAB_TOKEN env
        # default and raise the MR under the platform's credentials instead of the
        # author's. Re-resolve the per-user PAT (user_tokens → env fallback) and set
        # it before any GitLab call — mirrors run_governance_pipeline / the end-gate
        # resume job. Never mutate the process-wide GITLAB_TOKEN env var.
        try:
            from tools.gitlab_tools import set_token as _gl_set_token
            _gov_tok = _gov_resolve_gitlab_token(ctx.get("user_id", ""))
            if _gov_tok:
                _gl_set_token(_gov_tok)
        except Exception as _te:
            logger.warning("[SDLC-GOV] resume_governance_fix: could not set author GitLab token — "
                           "MR may use env default", run_id=run_id, error=str(_te))

        # ── 3. NO post-approval fixer (2026-07-31) ────────────────────────────
        # Once every domain is approved the findings have already been resolved,
        # accepted, or marked false-positive DURING triage — there is nothing left
        # to fix. Re-running the CLI fixer here is exactly the "governance fix kicked
        # off again" defect. Instead just publish the outcome.
        #
        # TOPOLOGY (2026-08-03): governance fixes were committed onto a SEPARATE
        # fix_branch (run_governance_batch_fix), NEVER the developer's scanned branch.
        # So the MR is opened fix_branch → head_branch (scanned branch). If no
        # fix_branch was ever created — clean pass, or every finding was accepted /
        # marked false-positive without a code change — there is nothing to merge →
        # COMPLETE with no MR.
        from tools.gitlab_tools import gitlab_branch_has_changes, gitlab_create_mr

        if not fix_branch:
            logger.info("[SDLC-GOV] resume_governance_fix: no fix branch (no code fix) — "
                        "COMPLETE, no MR", run_id=run_id, scanned=head_branch)
            update_run_state(run_id, "COMPLETE", current_stage="COMPLETE")
            add_run_event(
                run_id, "GOVERNANCE_SCAN", "COMPLETE", actor=actor,
                output="All domains approved; no governance code fix was needed — "
                       "no MR created, run complete.",
            )
            # Clean-pass / accept-only sign-off is still change evidence — export it.
            _enqueue_governance_evidence_final(run_id, actor=actor)
            return run_id

        # Does the fix branch actually differ from the scanned branch? (defensive —
        # the batch fixer only sets fix_branch after a real commit, but a compare is
        # cheap insurance against an empty MR.)
        has_changes = gitlab_branch_has_changes(repo, head_branch, fix_branch)
        if has_changes is False:
            logger.info("[SDLC-GOV] resume_governance_fix: fix branch has no diff over "
                        "scanned branch — COMPLETE, no MR", run_id=run_id,
                        fix_branch=fix_branch, scanned=head_branch)
            update_run_state(run_id, "COMPLETE", current_stage="COMPLETE")
            add_run_event(
                run_id, "GOVERNANCE_SCAN", "COMPLETE", actor=actor,
                output=f"All domains approved; '{fix_branch}' has no changes over "
                       f"'{head_branch}' — no MR created, run complete.",
            )
            # Clean-pass / accept-only sign-off is still change evidence — export it.
            _enqueue_governance_evidence_final(run_id, actor=actor)
            return run_id

        # A real change exists (or the compare was indeterminate → fail-open and
        # still open the MR so a real fix is never dropped). Open the MR
        # fix_branch → head_branch (409-idempotent — returns any existing MR).
        _transition(run_id, "MR_CREATION", "governance-approved")
        report_md = ""
        try:
            from store.sdlc_artifacts import _load_latest_artifact
            _art = _load_latest_artifact(run_id, "GOVERNANCE_REPORT")
            report_md = ((_art or {}).get("payload") or {}).get("report_md") or ""
        except Exception:
            report_md = ""
        mr_url = ""
        try:
            mr_result = gitlab_create_mr(
                repo=repo,
                title=f"Governance fixes (run {run_id[:8]})",
                body=report_md or "Governance remediation — all domains approved.",
                head=fix_branch,
                base=head_branch,
            )
            _m = re.search(r"https?://\S+", mr_result or "")
            mr_url = _m.group(0) if _m else (mr_result or "")
            logger.info("[SDLC-GOV] MR created (governance approved)", run_id=run_id,
                        mr_url=mr_url, head=fix_branch, base=head_branch)
        except Exception as _me:
            logger.error("[SDLC-GOV] MR creation errored", run_id=run_id, error=str(_me))

        # ── 4. COMPLETE ───────────────────────────────────────────────────────
        update_run_state(run_id, "COMPLETE", current_stage="COMPLETE", pr_url=mr_url)
        add_run_event(
            run_id, "MR_CREATION", "COMPLETE", actor=actor,
            output=f"All domains approved — MR: {mr_url or '(creation failed)'}",
        )
        # Standalone governance run fully approved → export final evidence bundle.
        _enqueue_governance_evidence_final(run_id, actor=actor)
        return run_id

    except SDLCCancelled:
        logger.info("[SDLC-GOV] resume_governance_fix stopped — run cancelled", run_id=run_id)
        return run_id
    except Exception as e:
        logger.error("[SDLC-GOV] resume_governance_fix failed", run_id=run_id, error=str(e))
        update_run_state(run_id, "FAILED", error=f"Governance fix error: {e}")
        return run_id


def trigger_domain_fix(run_id: str, domain: str, actor: str,
                       fix_instructions: str = "") -> str:
    """Run the auto-fixer for a SINGLE domain's open findings after its approver
    requested changes, then reset that domain to pending for re-approval. Does NOT
    commit / open an MR — the run stays at the approval gate until every domain is
    approved (then resume_governance_fix commits + opens the MR).

    LEGACY (2026-07-23, B2.6): the per-DOMAIN fixer predates the end-gate model. The
    author remediation loop ``run_governance_author_fix`` (per-FINDING fix + auto
    re-scan + snapshot-scoped carry-forward) is the current end-gate path. This
    function is retained because it is still wired via
    routers.sdlc_router → workers.sdlc_worker.trigger_domain_fix_job; it operates on
    the still-dual-written legacy findings table, marks fixes there, and never
    resumes into APPLYING, so it is safe under the new tail. Prefer the author loop
    for end-gate remediation."""
    bind_context(correlation_id=run_id, pipeline_stage="governance_domain_fix")

    try:
        dom = (domain or "").upper()

        # ── 1. load open findings for this domain only ─────────────────────────
        from store.sdlc_governance_findings import (
            list_findings, mark_fixed, domain_open_counts,
        )
        rows = list_findings(run_id, status="open", domain=dom)
        # ── 2. nothing to fix → no-op ──────────────────────────────────────────
        if not rows:
            logger.info("[SDLC-GOV] trigger_domain_fix: no open findings", run_id=run_id,
                        domain=dom)
            return run_id

        # rebuild Finding objects for the fixer prompt + fingerprints
        from agents.sdlc_governance.schema import Finding, fingerprint as fp_fn
        findings_to_fix = []
        for r in rows:
            try:
                findings_to_fix.append(Finding(
                    skill=r.get("skill") or "", severity=r.get("severity") or "low",
                    file=r.get("file") or "", rule=r.get("rule") or "",
                    title=r.get("title") or "", detail=r.get("detail") or "",
                    fix_hint=r.get("fix_hint") or "", snippet=r.get("snippet") or "",
                    line=r.get("line"), status="open",
                ))
            except Exception:
                continue
        if not findings_to_fix:
            return run_id

        # ── 3. workspace ───────────────────────────────────────────────────────
        run = get_run(run_id) or {}
        ctx = run.get("context") or {}
        workspace = _gov_ensure_workspace(run_id, ctx)
        if not workspace:
            logger.error("[SDLC-GOV] trigger_domain_fix: workspace unavailable",
                         run_id=run_id, domain=dom)
            return run_id

        # ── 4. build fixer prompt (prepend approver instructions as context) ────
        from agents.sdlc_governance.engine import build_fix_prompt
        fix_prompt = build_fix_prompt(findings_to_fix, workspace)
        if fix_instructions.strip():
            fix_prompt = (
                f"APPROVER REQUESTED CHANGES ({dom}): {fix_instructions.strip()}\n\n"
                + fix_prompt
            )

        # ── 5. run CLI fixer (profile="code") ──────────────────────────────────
        from agents.sdlc_cli_engine import run_cli, CliEngineConfig
        from core.model_registry import cli_model_for
        fix_result = run_cli(
            config=CliEngineConfig.from_env(), workspace_root=workspace,
            prompt=fix_prompt, profile="code", model=cli_model_for("coder"),
            max_turns=60, run_id=run_id,
        )
        if fix_result.status == "suspended":
            logger.warning("[SDLC-GOV] trigger_domain_fix: fixer suspended", run_id=run_id,
                           domain=dom, reason=fix_result.reason)
            return run_id

        # ── 6. mark this domain's findings fixed ───────────────────────────────
        mark_fixed(run_id, [fp_fn(f) for f in findings_to_fix])

        # ── 7. reset the domain back to pending for re-approval ────────────────
        from store.sdlc_governance_approvers import (
            reset_domain_to_pending, seed_domain_approvals,
        )
        reset_domain_to_pending(run_id, dom)

        # ── 8. re-seed open counts (idempotent; refreshes remaining domains) ───
        counts = domain_open_counts(run_id)
        seed_domain_approvals(run_id, counts)
        logger.info("[SDLC-GOV] trigger_domain_fix complete", run_id=run_id, domain=dom,
                    fixed=len(findings_to_fix), remaining_domains=list(counts.keys()))
        return run_id

    except SDLCCancelled:
        logger.info("[SDLC-GOV] trigger_domain_fix stopped — run cancelled", run_id=run_id)
        return run_id
    except Exception as e:
        logger.error("[SDLC-GOV] trigger_domain_fix failed", run_id=run_id,
                     domain=domain, error=str(e))


# ============================================================
# AUTHOR REMEDIATION LOOP (2026-07-23, B2.2)
#
# Runs the bounded auto-fix + auto re-scan loop for ONE finding the author
# asked to fix, while the run is SUSPENDED at AWAITING_GOVERNANCE_APPROVAL.
# Enqueued by routers.sdlc_router.author_request_fix via the sdlc_worker
# governance_author_fix_job (never run synchronously in the request handler).
# The run keeps its AWAITING_GOVERNANCE_APPROVAL state throughout — the rq job
# holds a worker slot only while it is actually fixing; when it returns no slot
# is held during subsequent human think-time.
# ============================================================

def run_governance_batch_fix(run_id: str, fingerprints: list, actor: str = "user") -> str:
    """Bounded CLI fixer + re-scan + convergence loop for a BATCH of requested findings.

    ONE fixer CLI session per iteration handles ALL target findings together (the fix
    prompt lists them all), then ONE re-scan verifies — for N findings that is 1 session
    + 1 re-scan per iteration, not N. Each iteration (capped by ``config.max_iters()``):
    fix → commit/push → re-diff → NEW scan snapshot (trigger="rescan") → mark findings
    that disappeared vs the prior snapshot ``fix_confirmed`` → check convergence.

    Stops on ANY of: all target findings resolved; ``max_iters()`` reached; the open-set
    hash repeats; the open count fails to strictly decrease for
    ``convergence_stall_limit()`` iterations; the HOD per-run budget is exhausted; the
    fixer suspends; or a re-scan that could not complete (fail-closed). On stop-without-
    full-resolution, every UNCONFIRMED target is reset ``fix_requested → open`` (so it
    stays actionable + team-visible, never stranded) and ``governance_not_converging`` is
    set. ALWAYS clears ``governance_rescanning`` and re-suspends to
    AWAITING_GOVERNANCE_APPROVAL. NEVER loops unbounded. Never raises (except
    SDLCCancelled)."""
    import hashlib

    bind_context(correlation_id=run_id, pipeline_stage="governance_batch_fix")

    from agents.sdlc_governance import config as gov_config
    from store.sdlc_governance_findings import (
        list_findings, set_disposition, open_fingerprint_set, domain_open_counts,
    )
    from agents.sdlc_governance.schema import Finding, fingerprint as fp_fn

    # De-dup + drop blanks; the batch acts on this ordered set of target fingerprints.
    target_fps = []
    for fp in (fingerprints or []):
        if fp and fp not in target_fps:
            target_fps.append(fp)

    def _observed_from_res(res: dict) -> set:
        """All fingerprints a scan result recorded (open + suppressed)."""
        out = set()
        for f in (res.get("open_findings") or []) + (res.get("suppressed") or []):
            try:
                out.add(fp_fn(f))
            except Exception:
                pass
        return out

    def _open_hash(snapshot_id):
        fps = sorted(open_fingerprint_set(run_id, snapshot_id))
        return (hashlib.sha256("\n".join(fps).encode("utf-8", "ignore")).hexdigest(),
                len(fps))

    def _resuspend(not_converging: bool, reason: str, open_count) -> None:
        """Re-affirm the AWAITING_GOVERNANCE_APPROVAL gate and ALWAYS clear the
        ``governance_rescanning`` flag — the batch job is no longer running, so the UI
        spinner/Send-gate must release (same-state context patch, no rq slot held)."""
        try:
            update_run_state(
                run_id, "AWAITING_GOVERNANCE_APPROVAL", current_stage="GOVERNANCE_SCAN",
                context_patch={
                    "governance_rescanning": False,
                    "governance_not_converging": bool(not_converging),
                    "governance_not_converging_reason": (reason if not_converging else ""),
                },
            )
        except Exception as _re:
            logger.warning("[SDLC-GOV] batch fix re-suspend failed — non-fatal",
                           run_id=run_id, error=str(_re))

    if not target_fps:
        logger.warning("[SDLC-GOV] batch fix: no target fingerprints — no-op", run_id=run_id)
        _resuspend(False, "", 0)
        return run_id

    try:
        run = get_run(run_id) or {}
        ctx = run.get("context") or {}

        # ── locate the requested findings (for the fixer prompt) ───────────────
        by_fp = {}
        for r in (list_findings(run_id) or []):
            fp = r.get("fingerprint")
            if fp in target_fps and fp not in by_fp:
                by_fp[fp] = r
        target_findings = []
        for fp in target_fps:
            r = by_fp.get(fp)
            if not r:
                continue
            try:
                target_findings.append(Finding(
                    skill=r.get("skill") or "", severity=r.get("severity") or "low",
                    file=r.get("file") or "", rule=r.get("rule") or "",
                    title=r.get("title") or "", detail=r.get("detail") or "",
                    fix_hint=r.get("fix_hint") or "", snippet=r.get("snippet") or "",
                    line=r.get("line"), status="open",
                ))
            except Exception:
                pass
        if not target_findings:
            logger.warning("[SDLC-GOV] batch fix: none of the requested findings exist — no-op",
                           run_id=run_id, requested=len(target_fps))
            _resuspend(False, "", 0)
            return run_id

        # Domains this fix batch actually targets — used to scope the post-rescan
        # carry-forward so an already-approved domain the author did NOT touch (e.g.
        # EA/DPDP) keeps its sign-off instead of being spuriously reverted to pending
        # by the whole-diff re-scan. Derived from the targeted findings' stamped
        # domain. If none resolve, pass None (legacy: re-evaluate every domain).
        targeted_domains = {
            (by_fp[fp].get("domain") or "").upper()
            for fp in target_fps
            if fp in by_fp and (by_fp[fp].get("domain") or "").strip()
        }
        if not targeted_domains:
            targeted_domains = None
        logger.info("[SDLC-GOV] batch fix targeted domains", run_id=run_id,
                    targeted_domains=(sorted(targeted_domains) if targeted_domains else None))

        # ── workspace + base ref (re-clone if the persisted path is gone) ──────
        workspace = _gov_ensure_workspace(run_id, ctx)
        if not workspace:
            logger.error("[SDLC-GOV] batch fix: workspace unavailable — cannot fix",
                         run_id=run_id, targets=len(target_fps))
            _resuspend(True, "workspace unavailable", 0)
            return run_id

        repo = run.get("repo") or ctx.get("repo", "")
        base_ref = ctx.get("base_commit") or ctx.get("base_branch") or "main"

        # ── scanned branch = the developer's branch governance ran against ─────
        # Governance NEVER commits onto this branch; it is the MR *target*. Resolve
        # from context (standalone → ctx["head_branch"]; end-gate → working_branch).
        # If both are empty (run triggered without an explicit head branch), fall
        # back to whatever branch the clone actually checked out — an empty value
        # was the root cause of `branch: ""` → push skipped → fix never reached
        # origin → the same findings re-reported round after round.
        scanned_branch = (ctx.get("head_branch") or ctx.get("working_branch")
                          or run.get("branch") or "")
        if not scanned_branch:
            try:
                import subprocess as _sp
                _hb = _sp.run(["git", "-C", workspace, "rev-parse", "--abbrev-ref", "HEAD"],
                              capture_output=True, text=True, timeout=30)
                _name = (_hb.stdout or "").strip() if _hb.returncode == 0 else ""
                if _name and _name != "HEAD":
                    scanned_branch = _name
            except Exception:
                pass
        if not scanned_branch:
            logger.error("[SDLC-GOV] batch fix: could not resolve the scanned branch — "
                         "cannot open a fix branch/MR", run_id=run_id)
            _resuspend(True, "scanned branch could not be resolved", 0)
            return run_id

        # Commit author identity — a fresh clone has none, so a bare commit fails and
        # gets swallowed as "no changes". Prefer the triggering user; bot fallback.
        author_email = ctx.get("user_email") or ""
        author_name  = (author_email.split("@", 1)[0] if author_email else "") or "AiNxt AI"

        # ── fix branch — topology depends on the run type ─────────────────────
        # STANDALONE governance (run.type == "governance"): the fixer's commits go
        # onto a NEW branch off the scanned branch, and the post-approval MR is opened
        # fix_branch → scanned_branch (resume_governance_fix). Keeps the developer's
        # branch untouched and the remediation independently reviewable.
        #
        # FEATURE/BUG END-GATE: the governance fix MUST stay on the working branch so
        # the downstream APPLYING/COMMITTING tail (resume_in_pipeline_governance_job)
        # carries it into the existing working_branch → base MR. Commit directly onto
        # the working (scanned) branch, as before — do NOT branch off.
        is_standalone_gov = (run.get("type") == "governance")
        if is_standalone_gov:
            fix_branch = (ctx.get("governance_fix_branch")
                          or f"governance-fix/{scanned_branch}-{run_id[:8]}")
            # Check the fix branch out BEFORE any fixer edits so every commit lands on it.
            if not _gov_prepare_fix_branch(workspace, scanned_branch, fix_branch, run_id=run_id):
                _resuspend(True, "could not create governance fix branch", 0)
                return run_id
            # Persist branch resolution so the post-approval MR step (a separate
            # process) opens fix_branch → scanned_branch without re-deriving.
            try:
                update_run_state(run_id, run.get("state") or "AWAITING_GOVERNANCE_APPROVAL",
                                 context_patch={"governance_fix_branch": fix_branch,
                                                "head_branch": scanned_branch})
            except Exception:
                pass
        else:
            fix_branch = scanned_branch

        subset = ctx.get("governance_skills")

        # Re-scan base_sha (2026-07-30): resolve the merge-base against the MR base
        # branch so the re-scan labels its range the SAME way the initial end-gate scan
        # did (base_sha...HEAD). Passing nothing defaults to "HEAD" → HEAD...HEAD +
        # `--base HEAD`, diverging from how the finding was originally produced. A
        # concrete SHA always resolves (unlike a bare/origin branch name in a shallow
        # clone). `_gov_git_diff` already uses three-dot base_ref...HEAD for the diff
        # body, so only the scan's base_sha LABEL needed fixing.
        _base_branch_name = ctx.get("base_branch") or "main"

        def _gov_merge_base(ref: str) -> str:
            import subprocess as _sp
            try:
                _r = _sp.run(["git", "-C", workspace, "merge-base", ref, "HEAD"],
                             capture_output=True, text=True, timeout=30)
                return (_r.stdout or "").strip() if _r.returncode == 0 else ""
            except Exception:
                return ""

        rescan_base_sha = (_gov_merge_base(f"origin/{_base_branch_name}")
                           or _gov_merge_base(_base_branch_name)
                           or base_ref or "HEAD")
        logger.info("[SDLC-GOV] batch fix workspace + base resolved", run_id=run_id,
                    workspace=workspace, scanned_branch=scanned_branch,
                    fix_branch=fix_branch, base_ref=base_ref, rescan_base_sha=rescan_base_sha)

        # product_id (best-effort; scan primitive tolerates None).
        product_id = None
        try:
            from db.database import SessionLocal as _SL
            from agents.sdlc_governance import engine as _gov_engine
            _db = _SL()
            try:
                product_id = _gov_engine.resolve_product_id(_db, repo)
            finally:
                _db.close()
        except Exception:
            product_id = None

        add_run_event(run_id, "GOVERNANCE_SCAN", "AUTHOR_FIX_STARTED", actor=actor,
                      output=f"Batch fix requested for {len(target_findings)} finding(s)")
        # Mark the run as actively re-scanning so the UI shows "fixing…" and gates
        # "Send to governance teams". Cleared by _resuspend / the except handlers.
        try:
            update_run_state(run_id, "AWAITING_GOVERNANCE_APPROVAL",
                             current_stage="GOVERNANCE_SCAN",
                             context_patch={"governance_rescanning": True})
        except Exception:
            pass

        from agents.sdlc_cli_engine import run_cli, CliEngineConfig
        from agents.sdlc_cli_budget import record_cli_usage, is_exhausted
        from agents.sdlc_governance.config import fix_model

        max_iters = gov_config.max_iters()
        stall_limit = gov_config.convergence_stall_limit()

        # Baseline observed set = the current (initial) scan's findings. The legacy
        # findings table has not been mutated by a re-scan yet, so it reflects the
        # end-gate snapshot's detections.
        prev_observed = {r.get("fingerprint") for r in (list_findings(run_id) or [])
                         if r.get("fingerprint")}
        prev_hash = None
        prev_open_count = None
        stall = 0
        resolved = False
        stop_reason = ""
        iteration = 0
        last_open_count = len(prev_observed)
        confirmed_targets = set()   # target fps proven gone (marked fix_confirmed)

        for iteration in range(1, max_iters + 1):
            if is_exhausted(run_id):
                stop_reason = "HOD per-run budget exhausted"
                break

            fix_res = run_cli(
                config=CliEngineConfig.from_env(), workspace_root=workspace,
                prompt=_gov_engine_build_fix_prompt(target_findings, workspace),
                profile="code", model=fix_model(),
                max_turns=gov_config.review_turns(), run_id=run_id,
            )
            try:
                record_cli_usage(run_id, fix_res.usage or {}, fix_res.total_cost_usd or 0.0)
            except Exception:
                pass
            if getattr(fix_res, "status", "") == "suspended":
                stop_reason = f"fixer suspended: {getattr(fix_res, 'reason', '')}"
                break

            # Commit + push the fixer's working-tree changes BEFORE re-diffing so the
            # committed-only re-scan diff sees them and the fix reaches origin/fix_branch.
            # "nothing to commit" → the fixer changed nothing → STOP (no empty MR,
            # no infinite loop). Commits land on the governance fix_branch (never the
            # developer's scanned branch) with an explicit author identity.
            committed, pushed = _gov_commit_and_push(
                workspace, fix_branch, run_id=run_id,
                message=f"[AiNxt AI] governance auto-fix (iter {iteration}) — {len(target_findings)} finding(s)",
                author_name=author_name, author_email=author_email,
            )
            logger.info("[SDLC-GOV] batch fix commit/push", run_id=run_id,
                        iteration=iteration, committed=bool(committed),
                        pushed=bool(pushed), branch=fix_branch)
            if not committed:
                stop_reason = "fixer produced no changes"
                logger.warning("[SDLC-GOV] batch fix stop — fixer produced no changes",
                               run_id=run_id, iteration=iteration, stop_reason=stop_reason)
                break

            # FAIL-CLOSED: the fixer committed but the push to origin did NOT land
            # (an auth/transport failure). The in-session re-scan below runs on the
            # LOCAL commit and could look green, but any later re-clone
            # (_gov_ensure_workspace) pulls origin WITHOUT the fix → the finding
            # silently returns "for the second time" and the MR carries nothing. Stop
            # now with an actionable reason instead of trusting an unverifiable re-scan.
            if not pushed:
                stop_reason = (
                    f"fix committed but not pushed to origin/'{fix_branch}' "
                    "(re-scan would not see the fix after re-clone)"
                )
                logger.error("[SDLC-GOV] batch fix stop — commit not pushed to origin",
                              run_id=run_id, iteration=iteration, branch=fix_branch,
                              stop_reason=stop_reason)
                break

            # Re-diff the workspace (fixer's real changes, now committed) + NEW scan snapshot.
            changed_files, diff_text = _gov_git_diff(workspace, base_ref)
            res = run_governance_scan_snapshot(
                run_id, workspace=workspace, diff_text=diff_text,
                changed_files=changed_files, product_id=product_id, repo=repo,
                base_sha=rescan_base_sha, subset=subset, trigger="rescan",
                created_by=actor,
            )
            snapshot_id = res.get("snapshot_id")

            # FAIL-CLOSED guard (review finding #2): a re-scan that could NOT complete
            # (CLI error/timeout) or resolved no bundle returns scan_error/skipped with
            # open_findings=[] and snapshot_id=None. Treating that as the state below
            # would compute disappeared = prev_observed − ∅ = ALL findings and mark
            # every open finding fix_confirmed — hiding real, unresolved findings from
            # governance. Instead STOP the loop and re-suspend for a human retry;
            # findings stay visible (nothing is marked fix_confirmed on an errored scan).
            if res.get("scan_error") or res.get("skipped") or not snapshot_id:
                stop_reason = (
                    "re-scan could not complete (CLI error/timeout)"
                    if res.get("scan_error") else
                    "re-scan resolved no governance bundle/skills"
                    if res.get("skipped") else
                    "re-scan produced no snapshot"
                )
                break

            # B2.5 — per-domain, fingerprint-granular approval carry-forward on the
            # NEW snapshot: approved domains with no new/changed findings stay
            # approved (their accepts copied forward); a domain that gained a
            # new/changed finding reverts to 'pending' so only that finding blocks
            # the B2.4 gate. Strictly per-domain — never invalidate-all. Fail-safe.
            if snapshot_id:
                try:
                    from store.sdlc_governance_approvers import evaluate_carry_forward
                    evaluate_carry_forward(run_id, snapshot_id,
                                           targeted_domains=targeted_domains)
                except Exception as _cf:
                    logger.warning("[SDLC-GOV] carry-forward eval failed — non-fatal",
                                   run_id=run_id, error=str(_cf))

            # Findings that disappeared since the prior snapshot → fix_confirmed.
            cur_observed = _observed_from_res(res)
            disappeared = prev_observed - cur_observed
            if disappeared:
                set_disposition(run_id, list(disappeared), "fix_confirmed", actor)
            prev_observed = cur_observed

            # Track which TARGETS are now proven gone (marked fix_confirmed above).
            for fp in target_fps:
                if fp not in cur_observed:
                    confirmed_targets.add(fp)

            open_hash, open_count = _open_hash(snapshot_id)
            last_open_count = open_count
            logger.info("[SDLC-GOV] batch fix iteration", run_id=run_id,
                        iteration=iteration, open_count=open_count, open_set_hash=open_hash,
                        targets_confirmed=len(confirmed_targets), targets_total=len(target_fps))

            # All requested findings resolved (none still detected) → done.
            if all(fp not in cur_observed for fp in target_fps):
                resolved = True
                stop_reason = "all requested findings resolved"
                break

            # Convergence guards.
            if prev_hash is not None and open_hash == prev_hash:
                stop_reason = "open-set hash repeated (no progress)"
                break
            if prev_open_count is not None and open_count >= prev_open_count:
                stall += 1
                if stall >= stall_limit:
                    stop_reason = "open count not strictly decreasing"
                    break
            else:
                stall = 0
            prev_hash = open_hash
            prev_open_count = open_count
        else:
            stop_reason = "max iterations reached"

        # Re-seed per-domain approvals (idempotent) so any newly-cleared / newly-
        # surfaced domains are reflected before the author keeps triaging.
        try:
            from store.sdlc_governance_approvers import seed_domain_approvals
            seed_domain_approvals(run_id, domain_open_counts(run_id))
        except Exception:
            pass

        if resolved:
            logger.info("[SDLC-GOV] batch fix resolved all targets", run_id=run_id,
                        targets=len(target_fps), open_count=last_open_count)
            _resuspend(False, "", last_open_count)
            add_run_event(run_id, "GOVERNANCE_SCAN", "AUTHOR_FIX_DONE", actor=actor,
                          output=f"Fix confirmed for {len(target_fps)} finding(s) — awaiting approval")
        else:
            # Auto-fix could not confirm every requested finding. Any target still
            # parked at `fix_requested` would strand the gate (no author actions,
            # excluded from the team-visible set, perpetual "re-scanning…" inference).
            # Reset every UNCONFIRMED target to `open` so the author can retry, mark it
            # a false positive, or send it on for manual review. Confirmed targets keep
            # fix_confirmed. The attempt is preserved via the AUTHOR_FIX_STOPPED event.
            still_open = [fp for fp in target_fps if fp not in confirmed_targets]
            if still_open:
                try:
                    set_disposition(run_id, still_open, "open", actor)
                except Exception as _rd:
                    logger.warning("[SDLC-GOV] batch fix: could not reset unconfirmed targets — non-fatal",
                                   run_id=run_id, error=str(_rd))
            logger.warning("[SDLC-GOV] batch fix stopped — not fully converged", run_id=run_id,
                           iteration=iteration, reason=stop_reason, open_count=last_open_count,
                           confirmed=len(confirmed_targets), reset_to_open=len(still_open))
            _resuspend(True, stop_reason, last_open_count)
            add_run_event(run_id, "GOVERNANCE_SCAN", "AUTHOR_FIX_STOPPED", actor=actor,
                          output=f"Auto-fix stopped ({stop_reason}) — {len(confirmed_targets)}/{len(target_fps)} resolved")
        return run_id

    except SDLCCancelled:
        logger.info("[SDLC-GOV] run_governance_batch_fix stopped — run cancelled",
                    run_id=run_id)
        # Clear the rescanning flag so a cancelled batch doesn't leave the UI spinning.
        try:
            update_run_state(run_id, "AWAITING_GOVERNANCE_APPROVAL",
                             current_stage="GOVERNANCE_SCAN",
                             context_patch={"governance_rescanning": False})
        except Exception:
            pass
        return run_id
    except Exception as e:
        logger.error("[SDLC-GOV] run_governance_batch_fix failed", run_id=run_id,
                     targets=len(target_fps), error=str(e))
        # Anti-strand reset: a crashed fixer must not leave targets parked at
        # `fix_requested` (perpetual spinner + no author actions). Reopen them and
        # clear the rescanning flag so the author retains a forward path.
        try:
            from store.sdlc_governance_findings import set_disposition as _reset_disp
            _reset_disp(run_id, target_fps, "open", actor)
        except Exception:
            pass
        try:
            update_run_state(
                run_id, "AWAITING_GOVERNANCE_APPROVAL", current_stage="GOVERNANCE_SCAN",
                context_patch={"governance_rescanning": False,
                               "governance_not_converging": True,
                               "governance_not_converging_reason": f"batch fix error: {e}"},
            )
        except Exception:
            pass
        return run_id


def run_governance_author_fix(run_id: str, fingerprint: str, actor: str = "user") -> str:
    """Back-compat single-finding wrapper → delegates to run_governance_batch_fix."""
    return run_governance_batch_fix(run_id, [fingerprint], actor)


def _gov_engine_build_fix_prompt(findings, workspace: str) -> str:
    """Build the governance fixer prompt for one OR MORE findings. Reuses
    agents.sdlc_governance.engine.build_fix_prompt (which expects a list); accepts a
    single Finding or a list/tuple for back-compat."""
    from agents.sdlc_governance.engine import build_fix_prompt
    fl = list(findings) if isinstance(findings, (list, tuple)) else [findings]
    return build_fix_prompt(fl, workspace)


# ============================================================
# IN-PIPELINE GOVERNANCE RESUME (2026-07-21; reworked 2026-07-23 B2.6)
#
# Resume a feature/bug run suspended at AWAITING_GOVERNANCE_APPROVAL.
# Distinct from resume_governance_fix (standalone governance pipeline
# path that reads head_branch from context).
#
# END-GATE OVERHAUL (2026-07-23): governance now runs AFTER COMMITTING +
# a DRAFT MR. On all-domains-approved there is NOTHING left to build or
# apply — the change is already committed. This resume therefore just
# UN-DRAFTS the MR (makes it mergeable) and composes into the EXISTING
# AWAITING_PR_APPROVAL gate. It NO LONGER re-enters the state machine to
# re-run the pre-apply fixer / APPLYING (that was the OLD mid-pipeline
# gate); end-gate fixes happen in the author remediation loop
# (run_governance_author_fix) BEFORE approval.
# ============================================================

def _enqueue_governance_evidence_final(run_id: str, snapshot_id=None, actor: str = "user") -> None:
    """Best-effort: enqueue the FINAL governance-evidence export to the linked
    Jira Change ticket (V7). NEVER raises — an evidence-enqueue failure must not
    break the terminal governance transition (un-draft / COMPLETE). The dedup
    ledger (event_key) makes repeated resume calls idempotent.

    Policy (2026-08-05): an approved / terminal governance run ALWAYS exports a
    final evidence bundle to a linked Jira Change ticket — regardless of whether
    the scan produced any observations or fixes. A clean PASS (no findings) and a
    "nothing scanned" run are both legitimate governance outcomes that need an
    audit trail, so the ticket + attachment are created for every run reaching
    here. When a scan snapshot exists it is bound to the dedup key; when none
    exists we fall back to a stable per-run key so repeated resume calls stay
    idempotent, and the worker's ``build_governance_evidence`` degrades to a
    safe empty bundle (still attached).
    """
    try:
        from core.config import SDLC_GOVERNANCE_EVIDENCE_ENABLED
        if not SDLC_GOVERNANCE_EVIDENCE_ENABLED:
            return
        if not snapshot_id:
            try:
                from store.sdlc_governance_findings import latest_snapshot
                snapshot_id = (latest_snapshot(run_id) or {}).get("id")
            except Exception:
                snapshot_id = None
        # No snapshot (nothing scanned / clean PASS with no code change) is NOT a
        # skip condition anymore — we still create the change ticket + attachment.
        # Use a stable sentinel so final_event_key() dedups repeated resume calls.
        effective_snapshot_id = snapshot_id or "none"
        # Sync, in-process (2026-08-06): the async doc_queue (Q_DOC) worker is not
        # deployed on this host, so the enqueued job never ran and the JIRA was
        # silently never created. Call the evidence function directly instead.
        # Best-effort / never-raise semantics are preserved by the surrounding
        # try/except; idempotency is handled inside ensure_change_ticket (key
        # reuse) and the dedup ledger in post_final_attestation.
        from store.sdlc_store import get_run
        run = get_run(run_id) or {}
        from agents.sdlc_governance_change_ticket import post_final_attestation
        post_final_attestation(
            run,
            effective_snapshot_id,
            user_id="",
            user_email=(actor if (actor and "@" in actor) else ""),
        )
        logger.info("[SDLC-GOV] final-evidence posted (sync)",
                    run_id=run_id, snapshot_id=effective_snapshot_id,
                    had_snapshot=bool(snapshot_id))
    except Exception as e:
        logger.warning("[SDLC-GOV] final-evidence enqueue failed (non-fatal)",
                       run_id=run_id, error=str(e))


def resume_in_pipeline_governance_approval(run_id: str, actor: str = "user") -> str:
    """Resume a feature/bug run suspended at AWAITING_GOVERNANCE_APPROVAL.

    End-gate model (B2.6): when every seeded domain is approved, flip the committed
    DRAFT MR to mergeable and transition the run to the EXISTING
    ``AWAITING_PR_APPROVAL`` gate — governance approval PRECEDES PR approval, two
    terminal gates composed in that order. Does NOT re-run IMPLEMENT / APPLYING /
    TEST_VERIFY (the code is already committed on the branch).

    Fail-closed: re-verifies ``all_finding_domains_approved`` before advancing (stays
    suspended otherwise). MR un-draft failures are non-fatal (logged) — a GitLab
    hiccup must not strand the run. Returns run_id in all paths.
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_governance_in_pipeline_resume")

    from store.sdlc_governance_approvers import all_finding_domains_approved, list_domain_approvals

    run = get_run(run_id)
    if not run:
        logger.error("[SDLC-GOV] resume_in_pipeline_governance_approval: run not found",
                     run_id=run_id)
        return run_id

    # Fail-closed guard — every seeded domain must be 'approved' before we unblock.
    if not all_finding_domains_approved(run_id):
        pending = [d["domain"] for d in (list_domain_approvals(run_id) or [])
                   if d.get("status") != "approved"]
        logger.warning("[SDLC-GOV] resume_in_pipeline_governance_approval: not all domains approved",
                       run_id=run_id, pending=pending)
        return run_id

    try:
        ctx  = run.get("context") or {}
        repo = run.get("repo") or ctx.get("repo", "")

        # Resolve the MR iid (pr_number IS the iid) + branch + pr_url from the run
        # row, falling back to the COMMITTING artifact {branch, mr_url, pr_number}.
        pr_number = run.get("pr_number")
        branch    = run.get("branch") or ctx.get("working_branch") or ""
        pr_url    = run.get("pr_url") or ""
        if not pr_number or not branch or not pr_url:
            try:
                from store.sdlc_artifacts import _load_latest_artifact
                _art = (_load_latest_artifact(run_id, "COMMITTING") or {}).get("payload") or {}
                pr_number = pr_number or _art.get("pr_number")
                branch    = branch or _art.get("branch") or ""
                pr_url    = pr_url or _art.get("mr_url") or ""
            except Exception:
                pass

        # Latest scan snapshot id — for the audit log line only (best-effort).
        snapshot_id = None
        try:
            from store.sdlc_governance_findings import latest_snapshot
            snapshot_id = (latest_snapshot(run_id) or {}).get("id")
        except Exception:
            snapshot_id = None

        # Flip the DRAFT MR to mergeable — best-effort, non-fatal on any GitLab hiccup.
        gitlab_repo = _resolve_gitlab_repo(repo) if repo else repo
        if pr_number:
            try:
                from tools.gitlab_tools import gitlab_set_mr_draft
                gitlab_set_mr_draft(gitlab_repo, pr_number, draft=False)
            except Exception as _ue:
                logger.warning("[SDLC-GOV] resume: MR undraft failed (non-fatal)",
                               run_id=run_id, mr_iid=pr_number, error=str(_ue))
        else:
            logger.warning("[SDLC-GOV] resume: no MR iid on run — cannot un-draft",
                           run_id=run_id)

        # Compose into the EXISTING PR-approval gate (governance precedes PR approval).
        update_run_state(run_id, "AWAITING_PR_APPROVAL",
                         branch=(branch or None), pr_number=pr_number,
                         pr_url=(pr_url or None))
        add_run_event(
            run_id, "GOVERNANCE_SCAN", "AWAITING_PR_APPROVAL", actor=actor,
            output="All governance domains approved — MR unblocked, awaiting PR approval",
        )
        # Full approval reached → export the final evidence bundle to the linked
        # Jira Change ticket (best-effort, guarded, dedup-safe).
        _enqueue_governance_evidence_final(run_id, snapshot_id=snapshot_id, actor=actor)
        logger.info("[SDLC-GOV] MR unblocked after all domains approved",
                    run_id=run_id, mr_iid=pr_number, snapshot_id=snapshot_id)
        return run_id

    except SDLCCancelled:
        raise
    except Exception as e:
        logger.error("[SDLC-GOV] resume_in_pipeline_governance_approval failed",
                     run_id=run_id, error=str(e))
        update_run_state(run_id, "FAILED", error=str(e))
        return run_id


def cleanup_abandoned_governance_mr(run_id: str, actor: str = "system",
                                    delete_branch: bool = False) -> None:
    """Best-effort cleanup for a run CANCELLED/abandoned while a governance end-gate
    DRAFT MR + committed branch exist (B2.6).

    Closes the draft MR and (optionally) deletes the abandoned source branch so a
    cancelled run does not leave an un-mergeable draft MR dangling. There is no
    public GitLab close-MR helper, so the MR is closed with an inline
    ``state_event=close`` PUT reusing ``tools.gitlab_tools``' existing request
    pattern (no new API surface added). Branch deletion is OFF by default
    (conservative — abandoned branches are cheap and the committer may want to
    recover the work); pass ``delete_branch=True`` to reap it via the existing
    ``gitlab_delete_branch`` helper.

    Idempotent + fail-safe: no-op when the run has no MR; a GitLab hiccup is logged,
    never re-raised (must not re-enter / strand the cancel path). The per-Jira dedup
    slot is released by the existing cancel machinery (routers.cancel_run /
    worker _release_slot) — NOT re-done here.
    """
    try:
        run = get_run(run_id) or {}
    except Exception:
        return
    ctx  = run.get("context") or {}
    repo = run.get("repo") or ctx.get("repo", "")

    pr_number = run.get("pr_number")
    branch    = run.get("branch") or ctx.get("working_branch") or ""
    if not pr_number or not branch:
        try:
            from store.sdlc_artifacts import _load_latest_artifact
            _art = (_load_latest_artifact(run_id, "COMMITTING") or {}).get("payload") or {}
            pr_number = pr_number or _art.get("pr_number")
            branch    = branch or _art.get("branch") or ""
        except Exception:
            pass

    if not pr_number:
        # Nothing committed / no draft MR for this run → nothing to clean.
        return

    gitlab_repo = _resolve_gitlab_repo(repo) if repo else repo

    # 1. Close the draft MR (inline PUT state_event=close — reuse gitlab_tools'
    #    request pattern; there is no public close helper and we must not add one).
    try:
        from tools.gitlab_tools import _put as _gl_put, _proj as _gl_proj
        _res = _gl_put(
            f"/projects/{_gl_proj(gitlab_repo)}/merge_requests/{pr_number}",
            {"state_event": "close"},
        )
        if isinstance(_res, dict) and _res.get("error"):
            logger.warning("[SDLC-GOV] abandoned draft MR close returned error (non-fatal)",
                           run_id=run_id, mr_iid=pr_number, error=str(_res.get("error")))
    except Exception as _ce:
        # TODO(gov-endgate): no public GitLab close-MR helper exists. If tooling adds
        # tools.gitlab_tools.gitlab_close_mr, prefer it over this inline PUT.
        logger.warning("[SDLC-GOV] abandoned draft MR close failed (non-fatal) — orphan MR",
                       run_id=run_id, mr_iid=pr_number, branch=branch, error=str(_ce))

    # 2. Optionally reap the abandoned source branch (existing helper, best-effort).
    if delete_branch and branch:
        try:
            from tools.gitlab_tools import gitlab_delete_branch
            gitlab_delete_branch(gitlab_repo, branch)
        except Exception as _be:
            logger.warning("[SDLC-GOV] abandoned branch delete failed (non-fatal)",
                           run_id=run_id, branch=branch, error=str(_be))

    logger.info("[SDLC-GOV] abandoned draft MR cleaned on cancel",
                run_id=run_id, mr_iid=pr_number, branch=branch)
