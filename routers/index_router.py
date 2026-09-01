# SPDX-License-Identifier: Apache-2.0
# ============================================================
# INDEX ROUTER — /index/*
#
# Governance-gated repo indexing:
#   1. Any operator submits a GitLab URL  → POST /index/submit
#   2. C1+ / admin approves or rejects    → POST /index/requests/{id}/approve|reject
#   3. On approval, worker clones using the submitter's (or approver's) GitLab PAT, indexes into pgvector
#
# Direct admin endpoints:
#   GET /index/repos           — list all indexed repos
#   DELETE /index/repos/{name} — delete all vectors for a repo
#   GET /index/repos/{name}/status — live status + vector count
#   POST /index/repos/{name}/reindex — admin re-index in background
# ============================================================

import os
import uuid
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from core.config import RDB_REGISTRY
from core.kv import get_kv, KVError
from core.logger import logger
from auth.dependencies import get_current_user
from auth.rbac import require_role, is_c1_plus, can_approve
from core.security_validation import (
    validate_security,
    validate_url,
    validate_description,
)

router = APIRouter(tags=["codebase"])

_REDIS_DB = RDB_REGISTRY
_r = None

PROTECTED_BRANCHES = {"main", "master", "release", "prod", "production"}


def _get_redis():
    """Return a cached KV client for the index governance registry (DB=3).

    Name retained for backwards compatibility; returns a KVClient.
    Backend selected via REDIS_CLIENT_CONFIG_DB3.
    """
    global _r
    if _r is None:
        try:
            c = get_kv(_REDIS_DB, decode_responses=True)
            c.ping()
            _r = c
        except KVError as e:
            logger.warning(f"IndexRouter: KV backend unavailable → {e}")
    return _r


def _get_pg():
    """Connect to PGS01 (POSTGRES_DB / ainxt_memory) — index_requests, products, etc."""
    import psycopg2
    from core.config import postgres_dsn
    return psycopg2.connect(postgres_dsn())


def _get_vec_pg():
    """Connect to PGS02 (PGVECTOR_DB / ainxt_vectors) — document_embeddings."""
    import psycopg2
    from core.config import pgvector_dsn
    return psycopg2.connect(**pgvector_dsn())


# ============================================================
# PYDANTIC SCHEMAS
# ============================================================

class IndexSubmitRequest(BaseModel):
    # repo_url is the provider-agnostic field (works for both GitHub and GitLab
    # HTTPS URLs; the target host is inferred from SCM_PROVIDER / the URL itself
    # and does not need to match either brand). gitlab_url is kept as a
    # backward-compatible alias so existing callers/UI payloads keep
    # working unchanged — exactly one of the two must be supplied.
    repo_url:    Optional[str] = None   # e.g. https://github.com/org/repo or https://gitlab.company.com/team/repo
    gitlab_url:  Optional[str] = None   # deprecated alias for repo_url — kept for backward compatibility
    branch:      str = "main"
    product_id:  Optional[str] = None
    note:        Optional[str] = None

    def resolved_repo_url(self) -> str:
        """Return whichever of repo_url / gitlab_url was supplied."""
        return (self.repo_url or self.gitlab_url or "").strip()


class ReviewAction(BaseModel):
    note: Optional[str] = None


# ============================================================
# HELPERS
# ============================================================

def _validate_repo_url(url: str):
    """Ensure URL looks like a valid HTTPS repo URL (any git host — GitHub, GitLab, etc.)."""
    if not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Repo URL must start with https://")
    if ".." in url or " " in url:
        raise HTTPException(status_code=400, detail="Invalid repo URL")


# Backward-compatible alias — old call sites used this name.
_validate_gitlab_url = _validate_repo_url


def _check_protected_branch(repo_url: str, branch: str) -> bool:
    """
    Verify whether the branch is protected on the repo's host.
    Returns True if protected. Fails silently (returns False) if API unreachable
    or the host is not a provider this check knows how to query (GitHub PRs
    already enforce branch protection server-side on merge, so a False here
    for GitHub just means "no extra pre-check warning" — not unsafe).
    """
    import httpx
    try:
        parts = repo_url.rstrip("/").split("/")
        # parts: ['https:', '', 'host.example.com', 'team', 'repo'] (GitLab
        # namespace/project can have more than 5 segments for nested groups)
        if len(parts) < 5:
            return False
        host = parts[2]
        path = "/".join(parts[3:])

        if host == "github.com":
            # GitHub: GET /repos/{owner}/{repo}/branches/{branch}/protection
            api_url = f"https://api.github.com/repos/{path}/branches/{branch}/protection"
            resp = httpx.get(api_url, timeout=5.0)
            return resp.status_code == 200

        # GitLab (self-hosted or gitlab.com): GET /api/v4/projects/{id}/protected_branches/{branch}
        encoded_path = path.replace("/", "%2F")
        api_url = f"https://{host}/api/v4/projects/{encoded_path}/protected_branches/{branch}"
        headers = {}  # branch-protection check is unauthenticated; token injected at clone time
        resp = httpx.get(api_url, headers=headers, timeout=5.0)
        return resp.status_code == 200
    except Exception:
        return False


def _extract_repo_name(repo_url: str) -> str:
    """Extract repo name slug from a repo URL. Strips .git suffix first."""
    name = repo_url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name.lower().replace("-", "_").replace(".", "_")


def _enqueue_index(payload: dict) -> str:
    """Enqueue an index job via RQ index_queue. Returns the RQ job_id.
    Raises HTTPException(503) if the job was rejected because the per-repo
    distributed lock is already held (another worker is actively indexing).
    Status updates are handled by the worker."""
    from core.job_queue import enqueue_index_job
    job_id = enqueue_index_job(
        repo_name    = payload["repo_name"],
        repo_path    = payload["repo_path"],
        triggered_by = payload.get("triggered_by", "system"),
        drop_index   = payload.get("drop_index", False),
        product_id   = payload.get("product_id", ""),
        department   = payload.get("department", ""),
        request_id   = payload.get("request_id", ""),
        branch       = payload.get("branch", ""),
    )
    if not job_id:
        repo_name = payload["repo_name"]
        logger.warning(f"IndexRouter: index job for '{repo_name}' rejected — lock already held")
        raise HTTPException(
            status_code=503,
            detail=f"Indexing for '{repo_name}' is already in progress. Please wait or check status.",
        )
    return job_id


def _notify_approvers_codebase(req_id: str, repo_name: str, branch: str, submitter: str) -> None:
    """Push a single codebase_approval inbox item to each recipient who should
    approve this index request.

    Routing (mirrors budget-approval routing — auth.rbac.resolve_request_approvers):
      - the submitter's own HOD (users.hod_email), plus any delegatees that
        HOD has nominated (department_hod_mapping.delegated_to).
      - falls back to every active admin/ad_level<=3 user when the submitter
        has no resolvable HOD, so a submission is never left unrouted.
    """
    try:
        from store.inbox_store import publish_inbox_item
        from db.database import SessionLocal
        from db.models import User
        from sqlalchemy import or_, func
        from auth.rbac import resolve_request_approvers

        approvers = resolve_request_approvers(submitter or "")
        hod_email = approvers.get("hod_email")
        delegatee_emails = approvers.get("delegatee_emails") or []
        recipient_emails = ([hod_email] if hod_email else []) + delegatee_emails

        db = SessionLocal()
        try:
            if recipient_emails:
                recipients = db.query(User).filter(
                    func.lower(User.email).in_([e.lower() for e in recipient_emails]),
                    User.is_active == True,
                ).all()
            else:
                # Fallback: no resolvable HOD — notify configurable approval level
                _approval_level = int(os.getenv("APPROVAL_AD_LEVEL", "3"))
                recipients = db.query(User).filter(
                    or_(User.ad_level <= _approval_level, User.role == "admin"),
                    User.is_active == True,
                ).all()
            for u in recipients:
                publish_inbox_item(
                    user_id   = str(u.id),
                    type      = "codebase_approval",
                    title     = f"[Codebase] Index request: {repo_name}",
                    body      = f"**{submitter}** has requested indexing of `{repo_name}` (branch: `{branch}`). Review and approve or reject.",
                    source_id = req_id,
                    metadata  = {
                        "entity_type": "codebase",
                        "request_id":  req_id,
                        "repo_name":   repo_name,
                        "branch":      branch,
                        "submitted_by": submitter,
                        "action":      "submit",
                        "hod_email":   hod_email,
                        "delegatee_emails": delegatee_emails,
                    },
                )
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"IndexRouter: failed to notify approvers for {repo_name}: {e}")


def _notify_submitter(req_id: str, repo_name: str, submitter_email: str,
                      approved: bool, note: str = "") -> None:
    """Notify the submitter when their request is approved or rejected."""
    try:
        from store.inbox_store import publish_inbox_item
        from db.database import SessionLocal
        from db.models import User
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == submitter_email).first()
            if not user:
                return
            action = "approved" if approved else "rejected"
            title  = f"[Codebase] Request {action}: {repo_name}"
            body   = (
                f"Your indexing request for `{repo_name}` was **{action}**."
                + (f"\n\nNote: {note}" if note else "")
            )
            publish_inbox_item(
                user_id   = str(user.id),
                type      = "codebase_result",
                title     = title,
                body      = body,
                source_id = req_id,
                metadata  = {
                    "entity_type": "codebase",
                    "request_id":  req_id,
                    "repo_name":   repo_name,
                    "action":      action,
                },
            )
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"IndexRouter: failed to notify submitter for {repo_name}: {e}")


def _trigger_index_from_request(req_id: str, repo_name: str, gitlab_url: str, branch: str,
                                approved_by: str, product_id: str = None, requested_by_email: str = ""):
    """Mark request as running and kick off indexing.

    Token resolution order (provider selected by core.config.SCM_PROVIDER):
      1. Submitter's personal token (profile → user_tokens table)
      2. Approver's personal token
    Admin/service-account credentials are never used. Raises PermissionError if neither
    the submitter nor approver has a stored token for the active SCM provider.

    NOTE: the `gitlab_url` parameter name is kept for backward compatibility with
    existing call sites — it holds the repo URL regardless of SCM_PROVIDER.
    """
    from core.platform_credentials import get_scm_token as get_gitlab_token, inject_scm_token as inject_gitlab_token

    conn = _get_pg()
    department = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE index_requests SET status='running', updated_at=NOW() WHERE id=%s::uuid",
                (req_id,)
            )
            # Lookup department for the product so vectors can be scoped
            if product_id:
                cur.execute(
                    "SELECT department FROM dept_product_mappings WHERE product_id::text = %s LIMIT 1",
                    (str(product_id),)
                )
                row = cur.fetchone()
                if row:
                    department = row[0]
        conn.commit()
    finally:
        conn.close()

    rc = _get_redis()
    if rc:
        rc.set(f"index:repo:{repo_name}:status", "running")
        rc.set(f"index:repo:{repo_name}:url", gitlab_url)
        rc.sadd("index:repo:index", repo_name)

    # Resolve GitLab token: submitter's profile first, then approver's profile.
    # Admin credentials are never used — raise if neither has a stored token.
    token = None
    try:
        token = get_gitlab_token(email=requested_by_email)
    except PermissionError:
        pass
    if not token:
        token = get_gitlab_token(email=approved_by)  # raises PermissionError if not found

    authed_url = inject_gitlab_token(gitlab_url, token)
    logger.info(f"IndexRouter: indexing {repo_name} with user token (submitter or approver)")

    payload = {
        "repo_name":    repo_name,
        "repo_path":    authed_url,
        "branch":       branch,
        "triggered_by": approved_by,
        "drop_index":   True,   # vectors wiped at submission time; skip dedup for fresh index
        "request_id":   req_id,
        "product_id":   product_id or "",
        "department":   department or "",
    }

    _enqueue_index(payload)


# ============================================================
# SUBMIT & GOVERNANCE ENDPOINTS
# ============================================================

@router.post("/index/submit")
def submit_index_request(body: IndexSubmitRequest, current_user=Depends(get_current_user)):
    """
    Submit a repo URL (GitHub, GitLab, or any HTTPS git host) for indexing.
    Creates a pending IndexRequest awaiting C1+ approval.
    """
    # Validate repo URL - basic validation
    gitlab_url = body.resolved_repo_url()
    if not gitlab_url:
        raise HTTPException(status_code=400, detail="repo_url is required")
    if not gitlab_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Repo URL must start with https://")
    if ".." in gitlab_url or " " in gitlab_url:
        raise HTTPException(status_code=400, detail="Invalid repo URL")
    
    # Validate branch (only alphanumeric, hyphens, underscores, periods, forward slashes)
    branch = body.branch.strip() if body.branch else "main"
    if branch and not re.match(r'^[a-zA-Z0-9/_\-.]+$', branch):
        raise HTTPException(status_code=400, detail="Branch can only contain alphanumeric characters, hyphens, underscores, periods, and forward slashes")
    
    # Validate note using shared security validation (XSS, SQL injection, special chars)
    note = body.note.strip() if body.note else ""
    if note:
        from core.security_validation import validate_security
        note_valid, note_errors = validate_security(note)
        if not note_valid:
            raise HTTPException(status_code=400, detail=f"Invalid note: {note_errors[0]}")
    
    # Validate product_id (if provided) - just basic validation
    product_id = body.product_id.strip() if body.product_id else ""
    if product_id and not re.match(r'^[a-zA-Z0-9_-]+$', product_id):
        raise HTTPException(status_code=400, detail="Product ID contains invalid characters")
    
    _validate_gitlab_url(gitlab_url)

    repo_name = _extract_repo_name(gitlab_url)

    # Block duplicate submissions only for the exact same (repo_name, product_id, branch)
    # combination. Different product or different branch = independent indexing job = allowed.
    conn = _get_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT status FROM index_requests
                WHERE repo_name = %s
                  AND COALESCE(product_id::text, '') = %s
                  AND branch = %s
                  AND status NOT IN ('rejected', 'failed')
                ORDER BY updated_at DESC LIMIT 1
            """, (repo_name, product_id or "", branch))
            existing = cur.fetchone()
    finally:
        conn.close()

    if existing:
        existing_status = existing[0]
        if existing_status not in ("done",):
            raise HTTPException(
                status_code=409,
                detail=f"A request for '{repo_name}' (branch='{branch}') is already {existing_status}. Wait for it to complete or reject it first."
            )
        # "done" → allow re-submission: wipe old request row for this exact (repo, product, branch)
        conn2 = _get_pg()
        try:
            with conn2.cursor() as cur:
                cur.execute(
                    """DELETE FROM index_requests
                       WHERE repo_name = %s
                         AND COALESCE(product_id::text, '') = %s
                         AND branch = %s
                         AND status = 'done'""",
                    (repo_name, product_id or "", branch)
                )
            conn2.commit()
        finally:
            conn2.close()

    # Wipe stale vectors scoped to this exact (repo, product_id, branch) combination.
    # Other products' or branches' vectors for the same repo are preserved.
    try:
        vec_conn = _get_vec_pg()
        try:
            with vec_conn.cursor() as cur:
                if product_id and branch:
                    cur.execute(
                        "DELETE FROM document_embeddings WHERE repo = %s AND product_id::text = %s AND branch = %s",
                        (f"repo_{repo_name}", product_id, branch)
                    )
                elif product_id:
                    cur.execute(
                        "DELETE FROM document_embeddings WHERE repo = %s AND product_id::text = %s",
                        (f"repo_{repo_name}", product_id)
                    )
                else:
                    cur.execute(
                        "DELETE FROM document_embeddings WHERE repo = %s AND product_id IS NULL",
                        (f"repo_{repo_name}",)
                    )
            vec_conn.commit()
        finally:
            vec_conn.close()
        logger.info(f"IndexRouter: cleared stale vectors for {repo_name} (product={product_id or 'unscoped'}, branch={branch or 'unscoped'})")
    except Exception as _ve:
        logger.warning(f"IndexRouter: could not clear stale vectors for {repo_name}: {_ve}")

    from auth.rbac import is_admin as _is_admin
    submitter_is_admin = _is_admin(current_user)

    is_protected = _check_protected_branch(gitlab_url, body.branch)
    req_id       = str(uuid.uuid4())
    now          = datetime.utcnow()
    submitter    = current_user.get("email", "unknown")

    # Admins auto-approve their own submissions (consistent with Products flow)
    initial_status = "approved" if submitter_is_admin else "pending"

    conn = _get_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO index_requests
                    (id, repo_name, branch, product_id, requested_by, status, reviewed_by, reviewed_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                req_id,
                repo_name,
                body.branch,
                body.product_id,
                submitter,
                initial_status,
                submitter if submitter_is_admin else None,
                now if submitter_is_admin else None,
                now, now,
            ))
        conn.commit()
    finally:
        conn.close()

    # Store URL keyed by request ID for retrieval on approval
    rc = _get_redis()
    if rc:
        rc.set(f"index:request:{req_id}:url", gitlab_url, ex=86400 * 30)

    logger.info(f"IndexRouter: new request {req_id} for {gitlab_url} (protected={is_protected}, admin_auto_approve={submitter_is_admin})")

    if submitter_is_admin:
        # Trigger indexing immediately — use the stored product_id (not body.product_id which may be None)
        try:
            _trigger_index_from_request(
                req_id=req_id,
                repo_name=repo_name,
                gitlab_url=gitlab_url,
                branch=body.branch,
                approved_by=submitter,
                product_id=str(body.product_id) if body.product_id else None,
                requested_by_email=submitter,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        return {
            "request_id":          req_id,
            "repo_name":           repo_name,
            "branch":              body.branch,
            "status":              "running",
            "is_protected_branch": is_protected,
            "message":             "Admin submitted — indexing started immediately.",
        }

    _notify_approvers_codebase(req_id, repo_name, body.branch, submitter)

    return {
        "request_id":       req_id,
        "repo_name":        repo_name,
        "branch":           body.branch,
        "status":           "pending",
        "is_protected_branch": is_protected,
        "message":          "Request submitted. Awaiting C1+ approval.",
    }


@router.get("/index/requests")
def list_index_requests(
    status: Optional[str] = None,
    current_user=Depends(get_current_user),
):
    """
    List IndexRequests for the governance queue.
    'done' requests are excluded — they appear in the Repos tab instead.
    C1+ / admin sees all; others see only their own.
    Deduped: only the latest request per repo_name is shown.
    """
    from auth.rbac import is_admin as _is_admin
    user_email = current_user.get("email", "")
    user_dept  = current_user.get("department", "")
    admin      = _is_admin(current_user)

    conn = _get_pg()
    try:
        with conn.cursor() as cur:
            # Exclude 'done' — those have graduated to the Repos tab
            if admin:
                if status:
                    cur.execute(
                        "SELECT DISTINCT ON (repo_name) * FROM index_requests WHERE status = %s ORDER BY repo_name, updated_at DESC",
                        (status,)
                    )
                else:
                    cur.execute(
                        "SELECT DISTINCT ON (repo_name) * FROM index_requests WHERE status != 'done' ORDER BY repo_name, updated_at DESC"
                    )
            else:
                # Dept-scoped: own submissions + repos in user's dept (via product mapping)
                status_clause = "AND ir.status = %s" if status else "AND ir.status != 'done'"
                params = [user_email, user_dept]
                if status:
                    params.append(status)
                cur.execute(f"""
                    SELECT DISTINCT ON (ir.repo_name) ir.*
                    FROM index_requests ir
                    WHERE (
                        ir.requested_by = %s
                        OR EXISTS (
                            SELECT 1 FROM dept_product_mappings dpm
                            WHERE dpm.product_id = ir.product_id
                              AND dpm.department = %s
                        )
                    )
                    {status_clause}
                    ORDER BY ir.repo_name, ir.updated_at DESC
                """, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()

    return {"requests": rows}


@router.post("/index/requests/{req_id}/approve")
def approve_index_request(req_id: str, body: ReviewAction = ReviewAction(), current_user=Depends(get_current_user)):
    """Approve an IndexRequest.

    Authorised: admin, OR the submitter's own HOD / one of the HOD's
    nominated delegatees, OR (fallback, when the submitter has no resolvable
    HOD) any senior approver (APPROVAL_AD_LEVEL) — mirrors the KB doc-approval gate.
    """
    from auth.rbac import is_admin as _is_admin, is_request_approver as _is_request_approver

    # Validate note if provided
    if body.note:
        from core.security_validation import validate_security
        note_valid, note_errors = validate_security(body.note.strip())
        if not note_valid:
            raise HTTPException(status_code=400, detail=f"Invalid note: {note_errors[0]}")

    approver_email = current_user.get("email", "")
    user_dept      = current_user.get("department", "")

    conn = _get_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM index_requests WHERE id=%s::uuid", (req_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Request not found")
            cols = [d[0] for d in cur.description]
            req  = {
                k: (str(v).strip() if isinstance(v, str) else v)
                for k, v in zip(cols, row)
            }

        if not (_is_admin(current_user)
                or _is_request_approver(current_user, req.get("requested_by") or "")
                or can_approve(current_user)):
            raise HTTPException(status_code=403, detail="Only the submitter's HOD (or their delegate) or an admin can approve index requests")

        if req["status"] != "pending":
            raise HTTPException(status_code=400, detail=f"Request is already {req['status']}")

        # 4-eyes: cannot approve your own submission
        if req.get("requested_by") == approver_email:
            raise HTTPException(status_code=403, detail="Cannot approve your own index request (4-eyes principle)")

        # Dept gate for non-admins: approver must be in the product's department —
        # skipped for the submitter's own HOD/delegate, whose authority to act
        # comes from being the submitter's manager, not from department membership.
        if not (_is_admin(current_user) or _is_request_approver(current_user, req.get("requested_by") or "")) and req.get("product_id"):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT department FROM dept_product_mappings WHERE product_id::text = %s",
                    (str(req["product_id"]),)
                )
                product_depts = [r[0] for r in cur.fetchall()]
            if product_depts and user_dept not in product_depts:
                raise HTTPException(status_code=403, detail="Your department is not mapped to this product's repo")

        approver = approver_email or "admin"
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE index_requests
                   SET status='approved', reviewed_by=%s, review_note=%s, reviewed_at=NOW(), updated_at=NOW()
                 WHERE id=%s::uuid
            """, (approver, body.note, req_id))
        conn.commit()
    finally:
        conn.close()

    # Fetch GitLab URL stored at submit time
    rc         = _get_redis()
    gitlab_url = (rc.get(f"index:request:{req_id}:url") or "") if rc else ""

    if not gitlab_url:
        raise HTTPException(status_code=400, detail="GitLab URL not found for this request. Please re-submit.")

    try:
        _trigger_index_from_request(
            req_id=req_id,
            repo_name=req["repo_name"],
            gitlab_url=gitlab_url,
            branch=req["branch"],
            approved_by=approver,
            product_id=req.get("product_id"),
            requested_by_email=req.get("requested_by", ""),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    _notify_submitter(req_id, req["repo_name"], req.get("requested_by", ""),
                      approved=True, note=body.note or "")

    return {"success": True, "request_id": req_id, "status": "running"}


@router.post("/index/requests/{req_id}/reject")
def reject_index_request(req_id: str, body: ReviewAction = ReviewAction(), current_user=Depends(get_current_user)):
    """Reject an IndexRequest.

    Authorised: admin, OR the submitter's own HOD / one of the HOD's
    nominated delegatees, OR (fallback, when the submitter has no resolvable
    HOD) any senior approver (APPROVAL_AD_LEVEL) — mirrors the KB doc-approval gate.
    """
    from auth.rbac import is_admin as _is_admin, is_request_approver as _is_request_approver

    # Validate note if provided
    if body.note:
        from core.security_validation import validate_security
        note_valid, note_errors = validate_security(body.note.strip())
        if not note_valid:
            raise HTTPException(status_code=400, detail=f"Invalid note: {note_errors[0]}")

    approver_email = current_user.get("email", "")
    user_dept      = current_user.get("department", "")

    conn = _get_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM index_requests WHERE id=%s::uuid", (req_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Request not found")
            cols = [d[0] for d in cur.description]
            req  = {
                k: (str(v).strip() if isinstance(v, str) else v)
                for k, v in zip(cols, row)
            }

        if not (_is_admin(current_user)
                or _is_request_approver(current_user, req.get("requested_by") or "")
                or can_approve(current_user)):
            raise HTTPException(status_code=403, detail="Only the submitter's HOD (or their delegate) or an admin can reject index requests")

        if req["status"] != "pending":
            raise HTTPException(status_code=400, detail=f"Request is already {req['status']}")

        # 4-eyes: cannot reject your own submission
        if req.get("requested_by") == approver_email:
            raise HTTPException(status_code=403, detail="Cannot reject your own index request")

        # Dept gate for non-admins — skipped for the submitter's own
        # HOD/delegate, whose authority comes from being the submitter's
        # manager, not from department membership.
        if not (_is_admin(current_user) or _is_request_approver(current_user, req.get("requested_by") or "")) and req.get("product_id"):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT department FROM dept_product_mappings WHERE product_id::text = %s",
                    (str(req["product_id"]),)
                )
                product_depts = [r[0] for r in cur.fetchall()]
            if product_depts and user_dept not in product_depts:
                raise HTTPException(status_code=403, detail="Your department is not mapped to this product's repo")

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE index_requests
                   SET status='rejected', reviewed_by=%s, review_note=%s, reviewed_at=NOW(), updated_at=NOW()
                 WHERE id=%s::uuid
            """, (approver_email or "admin", body.note, req_id))
        conn.commit()
    finally:
        conn.close()

    _notify_submitter(req_id, req["repo_name"], req.get("requested_by", ""),
                      approved=False, note=body.note or "")

    return {"success": True, "request_id": req_id, "status": "rejected"}


# ============================================================
# ADMIN ENDPOINTS (list repos, delete, status, reindex)
# ============================================================

@router.get("/index/repos")
def list_repos(current_user=Depends(get_current_user)):
    """
    List repos from index_requests submitted by users.
    Admin / approvers see all; regular users see only their own.
    Status derived from index_requests.status + live vector counts.
    """
    from auth.rbac import is_admin as _is_admin
    user_email       = current_user.get("email", "")
    admin            = _is_admin(current_user)
    user_can_approve = can_approve(current_user)
    user_dept        = current_user.get("department", "")

    try:
        conn = _get_pg()

        # 1. Fetch index requests scoped by department via product mapping.
        # Admin sees all. Everyone else (including approvers) sees only:
        #   - Repos whose product is mapped to their department
        #   - Their own submissions (so they can track pending/failed status)
        # Repos with no product_id are visible only to admin + submitter.
        with conn.cursor() as cur:
            if admin:
                cur.execute("""
                    SELECT ir.repo_name, ir.branch, ir.status AS req_status,
                           ir.requested_by, ir.product_id,
                           p.name AS product_name, ir.updated_at, ir.error_msg
                    FROM index_requests ir
                    LEFT JOIN products p ON p.id::text = ir.product_id::text
                    ORDER BY ir.updated_at DESC
                """)
            else:
                cur.execute("""
                    SELECT ir.repo_name, ir.branch, ir.status AS req_status,
                           ir.requested_by, ir.product_id,
                           p.name AS product_name, ir.updated_at, ir.error_msg
                    FROM index_requests ir
                    LEFT JOIN products p ON p.id::text = ir.product_id::text
                    WHERE ir.requested_by = %s
                       OR EXISTS (
                           SELECT 1 FROM dept_product_mappings dpm
                           WHERE dpm.product_id = ir.product_id
                             AND dpm.department = %s
                       )
                    ORDER BY ir.updated_at DESC
                """, (user_email, user_dept))
            request_rows = cur.fetchall()

        conn.close()

        # 2. Vector counts from document_embeddings on PGS02 — keyed by (slug, branch)
        #    so that repos indexed on multiple branches each get their own count.
        vector_map = {}
        try:
            vec_conn = _get_vec_pg()
            try:
                with vec_conn.cursor() as cur:
                    cur.execute("""
                        SELECT REPLACE(repo, 'repo_', ''), COALESCE(branch, ''), COUNT(*)
                        FROM document_embeddings
                        WHERE repo LIKE 'repo_%'
                        GROUP BY repo, branch
                    """)
                    vector_map = {(r[0], r[1]): r[2] for r in cur.fetchall()}
            finally:
                vec_conn.close()
        except Exception as ve:
            logger.warning(f"IndexRouter: vector_map query failed (non-fatal): {ve}")

        # Deduplicate by (repo_name, branch) — a repo indexed on multiple branches
        # produces one entry per branch, not one entry per repo.
        seen = {}
        for repo_name, branch, req_status, requested_by, product_id, product_name, updated_at, error_msg in request_rows:
            dedup_key = (repo_name, branch or "")
            if dedup_key in seen:
                continue

            slug      = repo_name.split("/")[-1].lower().replace("-", "_").replace(".", "_")
            vec_count = vector_map.get((slug, branch or ""), 0)

            # Status derived solely from index_requests.status + vector presence
            if req_status == "pending":
                display_status = "pending"
            elif req_status == "rejected":
                display_status = "rejected"
            elif req_status == "failed":
                display_status = "failed"
            elif req_status in ("running", "approved"):
                display_status = "running"
            elif req_status == "done":
                display_status = "ready" if vec_count > 0 else "failed"
            else:
                display_status = "not_indexed"

            indexed_at = updated_at.timestamp() if updated_at and req_status == "done" else None
            seen[dedup_key] = {
                "name":         repo_name,
                "slug":         slug,
                "branch":       branch,
                "product_name": product_name or "",
                "product_id":   product_id or "",
                "vector_count": vec_count,
                "status":       display_status,
                "indexed_at":   indexed_at,
                "error":        error_msg or "",
            }

        return {"repos": list(seen.values())}

    except Exception as e:
        logger.error(f"IndexRouter list_repos: {e}")
        import traceback; traceback.print_exc()
        return {"repos": [], "error": str(e)}


@router.delete("/index/repos/{name}")
def delete_repo(
    name: str,
    product_id: str,
    branch: str,
    current_user=Depends(get_current_user),
):
    """Delete vectors for a specific (repo, product, branch) combination from pgvector.
    C1+ or admin required. Non-admins must be in the product's mapped department.
    Only the vectors for the given product_id + branch are deleted — other products'
    and branches' vectors for the same repo are preserved."""
    from auth.rbac import is_admin as _is_admin
    if not (can_approve(current_user) or current_user.get("role") == "admin"):
        raise HTTPException(status_code=403, detail="Senior level (ad_level ≤ 3) or admin required")

    if not product_id or not branch:
        raise HTTPException(status_code=400, detail="product_id and branch are required query parameters")

    # Dept gate: verify the caller's department is mapped to this product
    if not _is_admin(current_user):
        user_dept = current_user.get("department", "")
        try:
            conn_chk = _get_pg()
            try:
                with conn_chk.cursor() as cur:
                    cur.execute(
                        "SELECT department FROM dept_product_mappings WHERE product_id::text = %s",
                        (product_id,)
                    )
                    product_depts = [r[0] for r in cur.fetchall()]
                if product_depts and user_dept not in product_depts:
                    raise HTTPException(status_code=403, detail="Your department is not mapped to this repo's product")
            finally:
                conn_chk.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"IndexRouter delete_repo dept check failed (non-fatal): {e}")

    repo_key = f"repo_{name}"
    deleted = 0
    try:
        # Delete vectors scoped to (repo, product_id, branch) from PGS02
        vec_conn = _get_vec_pg()
        try:
            with vec_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM document_embeddings WHERE repo = %s AND product_id::text = %s AND branch = %s",
                    (repo_key, product_id, branch)
                )
                deleted = cur.rowcount
                # Check if any vectors remain for this repo (other products/branches)
                cur.execute("SELECT COUNT(*) FROM document_embeddings WHERE repo = %s", (repo_key,))
                remaining = cur.fetchone()[0]
            vec_conn.commit()
        finally:
            vec_conn.close()
        # Remove the specific index_request row for this (repo, product, branch) from PGS01
        conn = _get_pg()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """DELETE FROM index_requests
                       WHERE repo_name = %s
                         AND COALESCE(product_id::text, '') = %s
                         AND branch = %s""",
                    (name, product_id, branch)
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")

    # Only clean up Redis repo-level keys if no vectors remain for the repo at all
    rc = _get_redis()
    if rc and remaining == 0:
        rc.delete(f"index:repo:{name}:status", f"index:repo:{name}:indexed_at",
                  f"index:repo:{name}:vector_count", f"index:repo:{name}:error",
                  f"index:repo:{name}:url")
        rc.srem("index:repo:index", name)

    logger.info(f"IndexRouter: deleted {deleted} vectors for repo '{name}' (product={product_id}, branch={branch}), {remaining} vectors remain for other products/branches")
    return {"success": True, "name": name, "product_id": product_id, "branch": branch, "deleted_vectors": deleted, "remaining_vectors": remaining}


@router.post("/index/repos/{name}/reindex")
def reindex_repo(name: str, current_user=Depends(require_role("admin"))):
    """Re-index a repo from scratch. Admin only."""
    from core.platform_credentials import get_scm_token as get_gitlab_token, inject_scm_token as inject_gitlab_token

    rc        = _get_redis()
    gitlab_url = (rc.get(f"index:repo:{name}:url") or "") if rc else ""

    if not gitlab_url:
        raise HTTPException(status_code=400, detail="No URL found for this repo. Submit a new index request.")

    # Look up branch, product_id, and request_id from the most recent index request.
    # These are needed to scope the drop_index delete and to update the DB row on failure.
    branch     = ""
    product_id = ""
    request_id = ""
    try:
        conn_b = _get_pg()
        try:
            with conn_b.cursor() as cur:
                cur.execute(
                    """SELECT branch, product_id::text, id::text
                       FROM index_requests
                       WHERE repo_name = %s
                       ORDER BY updated_at DESC LIMIT 1""",
                    (name,)
                )
                row = cur.fetchone()
                if row:
                    branch     = row[0] or ""
                    product_id = row[1] or ""
                    request_id = row[2] or ""
        finally:
            conn_b.close()
    except Exception as e:
        logger.warning(f"IndexRouter reindex: could not fetch request metadata for {name}: {e}")

    if rc:
        rc.set(f"index:repo:{name}:status", "running")

    # Reindex uses the admin's own stored token for the active SCM_PROVIDER — no
    # service-account fallback.
    try:
        token = get_gitlab_token(user_id=current_user.get("sub", ""), email=current_user.get("email", ""))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    authed_url = inject_gitlab_token(gitlab_url, token)

    payload = {
        "repo_name":    name,
        "repo_path":    authed_url,
        "branch":       branch,
        "product_id":   product_id,
        "triggered_by": current_user.get("email", "admin"),
        "drop_index":   True,
        "request_id":   request_id,
    }
    _enqueue_index(payload)
    logger.info(f"IndexRouter: enqueued reindex for repo '{name}' branch='{branch}' product='{product_id}'")
    return {"success": True, "name": name, "status": "running"}



@router.get("/index/repos/{name}/status")
def repo_status(name: str, current_user=Depends(get_current_user)):
    """Get current index status and live vector count for a repo.
    User must be admin, the submitter, or in the product's mapped department."""
    from auth.rbac import is_admin as _is_admin
    if not _is_admin(current_user):
        user_email = current_user.get("email", "")
        user_dept  = current_user.get("department", "")
        try:
            conn_chk = _get_pg()
            try:
                with conn_chk.cursor() as cur:
                    cur.execute(
                        "SELECT requested_by, product_id FROM index_requests WHERE repo_name = %s ORDER BY updated_at DESC LIMIT 1",
                        (name,)
                    )
                    row = cur.fetchone()
                if row:
                    req_by, product_id = row
                    if req_by != user_email:
                        if product_id:
                            with conn_chk.cursor() as cur:
                                cur.execute(
                                    "SELECT department FROM dept_product_mappings WHERE product_id::text = %s",
                                    (str(product_id),)
                                )
                                product_depts = [r[0] for r in cur.fetchall()]
                            if product_depts and user_dept not in product_depts:
                                raise HTTPException(status_code=403, detail="Access denied: repo belongs to a different department")
                        else:
                            # No product mapping — only submitter or admin can see status
                            raise HTTPException(status_code=403, detail="Access denied: repo not linked to your department")
            finally:
                conn_chk.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"IndexRouter repo_status dept check failed: {e}")

    rc         = _get_redis()
    status     = (rc.get(f"index:repo:{name}:status") or "unknown") if rc else "unknown"
    error      = (rc.get(f"index:repo:{name}:error") or "") if rc else ""
    indexed_at = rc.get(f"index:repo:{name}:indexed_at") if rc else None

    vector_count = 0
    try:
        vec_conn = _get_vec_pg()
        try:
            with vec_conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM document_embeddings WHERE repo = %s",
                    (f"repo_{name}",)
                )
                vector_count = cur.fetchone()[0]
        finally:
            vec_conn.close()
    except Exception as e:
        logger.warning(f"IndexRouter status: pgvector query failed → {e}")

    return {
        "name":         name,
        "status":       status,
        "indexed_at":   float(indexed_at) if indexed_at else None,
        "vector_count": vector_count,
        "error":        error,
    }


# ──────────────────────────────────────────────────────────────
# GET /index/health — all repos with status (G12)
# Returns per-repo index health: status, vector count, last indexed, staleness
# ──────────────────────────────────────────────────────────────

@router.get("/index/health")
def index_health(current_user=Depends(require_role("admin"))):
    """Return health summary for all indexed repos. Admin only."""
    import time as _time

    try:
        rc = _get_redis()
        repos_in_index = list(rc.smembers("index:repo:index")) if rc else []

        # Also pull from document_embeddings for any repo that may have been indexed directly
        vec_conn = None
        repos_from_db: list = []
        try:
            vec_conn = _get_vec_pg()
            with vec_conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT repo, COUNT(*) as cnt FROM document_embeddings GROUP BY repo ORDER BY repo"
                )
                repos_from_db = [(r[0].replace("repo_", ""), r[1]) for r in cur.fetchall()]
        except Exception as e:
            logger.warning(f"index_health: pgvector query failed → {e}")
        finally:
            if vec_conn:
                vec_conn.close()

        # Merge repo names from both sources
        # smembers with decode_responses=True returns str; without it returns bytes — handle both
        all_repo_names = set(r.decode() if isinstance(r, bytes) else r for r in repos_in_index)
        db_counts = {name: cnt for name, cnt in repos_from_db}
        all_repo_names.update(db_counts.keys())

        now = _time.time()
        stale_days = 7

        result = []
        for name in sorted(all_repo_names):
            # Redis client uses decode_responses=True — values are already strings, not bytes
            status     = (rc.get(f"index:repo:{name}:status") or "unknown") if rc else "unknown"
            indexed_at = rc.get(f"index:repo:{name}:indexed_at") if rc else None
            indexed_ts = float(indexed_at) if indexed_at else None
            error      = (rc.get(f"index:repo:{name}:error") or "") if rc else ""
            url        = (rc.get(f"index:repo:{name}:url") or "") if rc else ""
            vec_count  = db_counts.get(name, 0)

            days_since = round((now - indexed_ts) / 86400, 1) if indexed_ts else None
            # stale: true if never indexed (indexed_ts is None) OR older than 7 days
            is_stale   = (indexed_ts is None) or (days_since is not None and days_since > stale_days)

            result.append({
                "name":        name,
                "status":      status,
                "indexed_at":  indexed_ts,
                "days_since":  days_since,
                "is_stale":    is_stale,
                "stale":       is_stale,
                "stale_days":  days_since,
                "vector_count": vec_count,
                "url":         url,
                "error":       error,
            })

        return {"total": len(result), "stale_count": sum(1 for r in result if r["is_stale"]), "repos": result}

    except Exception as e:
        logger.error(f"index_health: unexpected error → {e}")
        return {"total": 0, "stale_count": 0, "repos": [], "error": str(e)}


# ──────────────────────────────────────────────────────────────
# POST /index/bulk — trigger re-index for all stale repos (G12)
# ──────────────────────────────────────────────────────────────

class BulkIndexRequest(BaseModel):
    stale_only:  bool = True    # True = only re-index repos stale > 7 days; False = all
    stale_days:  int  = 7       # staleness threshold

@router.post("/index/bulk")
def bulk_index(body: BulkIndexRequest = BulkIndexRequest(), current_user=Depends(require_role("admin"))):
    """Re-index all stale repos (or all repos) in one call. Admin only."""
    import time as _time
    from core.platform_credentials import get_scm_token as get_gitlab_token, inject_scm_token as inject_gitlab_token

    rc  = _get_redis()
    now = _time.time()

    repos_in_index = list(rc.smembers("index:repo:index")) if rc else []
    all_repo_names = [r.decode() if isinstance(r, bytes) else r for r in repos_in_index]

    triggered = []
    skipped   = []
    try:
        token = get_gitlab_token(user_id=current_user.get("sub", ""), email=current_user.get("email", ""))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    # Fetch all branches and request IDs in a single query to avoid N+1.
    # request_id_map is used so the worker can update index_requests on failure.
    branch_map:     dict[str, str] = {}
    request_id_map: dict[str, str] = {}
    try:
        conn_b = _get_pg()
        try:
            with conn_b.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT ON (repo_name) repo_name, branch, id::text FROM index_requests ORDER BY repo_name, updated_at DESC"
                )
                rows = cur.fetchall()
                branch_map     = {r[0]: (r[1] or "") for r in rows}
                request_id_map = {r[0]: (r[2] or "") for r in rows}
        finally:
            conn_b.close()
    except Exception as e:
        logger.warning(f"bulk_index: could not fetch branch map: {e}")

    for name in all_repo_names:
        indexed_at = rc.get(f"index:repo:{name}:indexed_at") if rc else None
        indexed_ts = float(indexed_at) if indexed_at else None
        days_since = (now - indexed_ts) / 86400 if indexed_ts else 999.0

        if body.stale_only and days_since <= body.stale_days:
            skipped.append(name)
            continue

        gitlab_url = (rc.get(f"index:repo:{name}:url") or b"").decode() if rc else ""
        if not gitlab_url:
            skipped.append(f"{name} (no URL)")
            continue

        if rc:
            rc.set(f"index:repo:{name}:status", "running")

        authed_url = inject_gitlab_token(gitlab_url, token)
        branch     = branch_map.get(name, "")
        payload = {
            "repo_name":    name,
            "repo_path":    authed_url,
            "branch":       branch,
            "triggered_by": current_user.get("email", "admin"),
            "drop_index":   True,
            "request_id":   request_id_map.get(name, ""),
        }
        # A single locked repo must not abort the entire bulk operation — catch
        # the 503 raised by _enqueue_index and add to skipped instead.
        try:
            _enqueue_index(payload)
            triggered.append(name)
            logger.info(f"bulk_index: triggered re-index for '{name}' branch='{branch}'")
        except HTTPException:
            skipped.append(f"{name} (already indexing)")
            logger.warning(f"bulk_index: skipped '{name}' — lock already held")

    return {
        "triggered": len(triggered),
        "skipped":   len(skipped),
        "repos":     triggered,
    }
