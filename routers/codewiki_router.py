# SPDX-License-Identifier: MIT
"""
CodeWiki documentation router.

Provides:
- POST /generate       — submit a new codebase for documentation. Governance-
       gated like Codebase indexing (routers/index_router.py): admin auto-
       approves and triggers immediately; everyone else lands in
       'pending_approval' until a HOD/delegate/senior-approver/admin
       approves it via POST /requests/{id}/approve.
- POST /requests/{id}/approve | reject — approve/reject a pending generate
       request. Same authorisation rule as index_router's approve/reject:
       admin, OR the submitter's HOD/delegate, OR (no resolvable HOD) any
       senior approver — department-scoped only when a product_id was
       attached at submit time, exactly like index_requests.
- POST /regenerate     — regenerate docs for an existing codebase_name.
- POST /retry          — re-run a FAILED job from scratch (same repo/branch).
- GET  /codebases      — list the latest completed docs per codebase.
- GET  /codebases/{codebase_name}            — status / page list.
- GET  /codebases/{codebase_name}/pages/{path} — raw Markdown content.
- GET  /codebases/{codebase_name}/page-tree    — hierarchical module tree
       (folders/files) derived from the CLI's own module_tree.json, used
       to order the sidebar's page list (folders before files).
- GET  /codebases/{codebase_name}/search       — full-text search over every
       generated page's raw Markdown content (not just page titles).
"""

import os
import json
import re
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException

from auth.dependencies import get_current_user
from auth.rbac import is_admin, is_request_approver, can_approve
from pydantic import BaseModel, field_validator

from db.database import pg_raw_connection, DB_SCHEMA
from core.job_queue import enqueue_codewiki_job, cancel_job
from core.url_masking import mask_repo_url, mask_text
from core.security_validation import (
    validate_codewiki_generate_request,
    validate_codewiki_regenerate_request,
    _flatten_errors,
)

router = APIRouter(prefix="/codewiki", tags=["CodeWiki"])


@router.get("/status")
def codewiki_status(current_user: dict = Depends(get_current_user)):
    """Surface CodeWiki config/worker readiness to the UI up front, so a user
    opening the panel sees WHY generation will fail/hang instead of finding
    out only after submitting a request and watching it sit at "pending"
    (no worker) or fail deep in a job's error_message (missing config).

    CODEWIKI_BASE_URL/CODEWIKI_API_KEY are checked directly via os.getenv --
    same two vars workers/codewiki_worker.py's _sync_codewiki_config_from_env()
    requires. Worker liveness mirrors index_router.py's
    _index_worker_liveness_warning() pattern, just pointed at codewiki_queue.
    """
    # CODEWIKI_MAIN_MODEL has no real fallback either (same as BASE_URL/
    # API_KEY) -- CODEWIKI_CLUSTER_MODEL/CODEWIKI_FALLBACK_MODEL are excluded
    # here since workers/codewiki_worker.py now defaults both to whatever
    # CODEWIKI_MAIN_MODEL resolves to when unset, so they're never actually
    # required on their own (2026-09-05 fix).
    missing_env = [
        name for name in ("CODEWIKI_BASE_URL", "CODEWIKI_API_KEY", "CODEWIKI_MAIN_MODEL")
        if not os.getenv(name)
    ]

    worker_running = False
    try:
        import rq
        from core.job_queue import get_queue, Q_CODEWIKI
        queue = get_queue(Q_CODEWIKI)
        worker_running = queue is not None and bool(rq.Worker.all(queue=queue))
    except Exception as e:
        from core.logger import logger
        logger.warning(f"CodeWikiRouter: worker liveness check failed (non-fatal): {e}")

    return {"missing_env": missing_env, "worker_running": worker_running}


def _resolve_repo_reachability(repo_url: str, branch: str, current_user: dict) -> str:
    """Check that ``repo_url``/``branch`` is reachable, retrying with the
    CALLING user's own stored git token if a plain (unauthenticated) check
    fails. Returns the branch's current HEAD sha on success.

    Tries public access first -- so a public repo never touches
    ``user_tokens`` at all -- then falls back to the caller's own SCM token
    (GitHub or GitLab, per core.config.SCM_PROVIDER) via
    core.platform_credentials, exactly like routers/index_router.py's
    existing pattern for Codebase indexing. Never a service-account/admin
    token. Raises RuntimeError with an actionable message on failure --
    this never touches HTTP directly, so callers decide how to surface it
    (HTTPException 400 at submit time, a dry-run "note" at regenerate time,
    etc).
    """
    from git.cmd import Git as GitCmd

    def _ls_remote_sha(url: str) -> str:
        out = GitCmd().ls_remote(url, branch)
        sha = out.split()[0] if out.strip() else None
        if not sha:
            raise RuntimeError(f"Branch '{branch}' not found on the remote.")
        return sha

    try:
        return _ls_remote_sha(repo_url)
    except Exception as first_exc:
        first_msg = mask_text(str(first_exc))

    from core.platform_credentials import get_scm_token, inject_scm_token
    try:
        token = get_scm_token(
            user_id=current_user.get("sub", ""),
            email=current_user.get("email", ""),
        )
    except PermissionError:
        raise RuntimeError(
            f"Repository {mask_repo_url(repo_url)} is not reachable ({first_msg}). "
            "If this is a private repository, add your GitHub/GitLab token under "
            "Profile → Git Token and try again."
        )

    authed_url = inject_scm_token(repo_url, token)
    try:
        return _ls_remote_sha(authed_url)
    except Exception as second_exc:
        second_msg = mask_text(str(second_exc))
        raise RuntimeError(
            f"Repository {mask_repo_url(repo_url)} is still not reachable, even with your "
            f"configured git token ({second_msg}). Check the repo URL/branch, or that your "
            "token has access to this repo."
        )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# No in-repo fallback default -- CODEWIKI_DOCS_DIR must point at a directory
# OUTSIDE the repo checkout (generated docs are runtime data, not source;
# see docs/codewiki-server-deployment.md section 4.1). Read lazily via
# _require_codewiki_docs_dir() below rather than raising here at import
# time: this module is imported unconditionally by gateway.py at startup
# (not behind a feature flag like ENABLE_COACH/ENABLE_DISCUSSIONS), so a
# hard failure here would take down the whole gateway over one optional
# feature's missing config, not just codewiki's own endpoints.
_CODEWIKI_DOCS_DIR = os.getenv("CODEWIKI_DOCS_DIR")


def _require_codewiki_docs_dir() -> str:
    """Return CODEWIKI_DOCS_DIR, or raise a clear 500 if it's not set.

    Only called from the two spots that actually need it as a FALLBACK
    (when a job's own stored `output_dir` is missing) -- the common,
    normal-path reads never touch this at all.
    """
    if not _CODEWIKI_DOCS_DIR:
        raise HTTPException(
            status_code=500,
            detail=(
                "CODEWIKI_DOCS_DIR is not configured on this server. Set it "
                "to a directory outside the repo checkout (see "
                "docs/codewiki-server-deployment.md section 4.1)."
            ),
        )
    return _CODEWIKI_DOCS_DIR


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class GenerateDocsRequest(BaseModel):
    codebase_name: str
    repo_url: str
    branch: str = "main"
    product_id: Optional[str] = None   # optional — scopes dept-approval like index_requests

    @field_validator("codebase_name", "repo_url", "branch")
    @classmethod
    def strip_strings(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class ReviewAction(BaseModel):
    note: Optional[str] = None


class RegenerateDocsRequest(BaseModel):
    codebase_name: str
    confirm: bool = False


class JobResponse(BaseModel):
    id: str
    codebase_name: str
    repo_url: str
    branch: str
    status: str
    error_message: str | None
    pages: List[dict]
    created_at: str | None
    updated_at: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: tuple, mask: bool = True) -> dict:
    """Convert a `codewiki_doc_jobs` row into a dict.

    `repo_url` is masked by DEFAULT (`mask=True`) -- this is the single
    choke point nearly every caller routes through, so the real credential
    embedded in a repo URL (e.g. `https://user:token@host/org/repo`, per
    the CodeWiki panel's supported URL form) never leaves the server in an
    HTTP response body, and never gets echoed back into an error message.

    `mask=False` is for the small number of INTERNAL call sites that need
    the REAL url for an actual git operation (the worker's own clone --
    which reads repo_url via its own separate query, not through this
    function at all -- and this router's own regenerate-diff clone /
    re-enqueue calls, which explicitly opt out of masking; see
    _get_job_by_codebase(..., mask=False) call sites below). Every one of
    those call sites is deliberately explicit about requesting the raw
    value specifically because it's about to hand it to `git clone` or to
    `enqueue_codewiki_job()`, not because it forgot to mask.
    """
    d = {
        "id": str(row[0]),
        "codebase_name": row[1],
        "repo_url": mask_repo_url(row[2]) if mask else row[2],
        "branch": row[3],
        "status": row[4],
        "error_message": row[5],
        "output_dir": row[6],
        "pages": row[7] if row[7] is not None else [],
        "created_at": row[8].isoformat() if row[8] else None,
        "updated_at": row[9].isoformat() if row[9] else None,
        "last_commit_sha": row[10] if len(row) > 10 else None,
    }
    # Approval-workflow columns (Part AA15) — optional in the tuple since
    # not every SELECT in this file needs them.
    if len(row) > 15:
        d["requested_by"] = row[11]
        d["product_id"]   = str(row[12]) if row[12] else None
        d["reviewed_by"]  = row[13]
        d["reviewed_at"]  = row[14].isoformat() if row[14] else None
        d["review_note"]  = row[15]
    return d


def _get_job_by_codebase(codebase_name: str, mask: bool = True) -> dict | None:
    """Fetch a job row by codebase_name.

    `mask` defaults to True (safe for the common case: status checks,
    display, building an API response). Pass `mask=False` ONLY when the
    caller is about to use the returned `repo_url` for a real git
    operation (see _row_to_dict's docstring).
    """
    with pg_raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, codebase_name, repo_url, branch, status,
                       error_message, output_dir, pages, created_at, updated_at,
                       last_commit_sha, requested_by, product_id, reviewed_by,
                       reviewed_at, review_note
                  FROM {DB_SCHEMA}.codewiki_doc_jobs
                 WHERE codebase_name = %s
                """,
                (codebase_name,),
            )
            row = cur.fetchone()
            return _row_to_dict(row, mask=mask) if row else None


def _get_job_by_id(job_id: str, mask: bool = True) -> dict | None:
    """Fetch a job row by id — used by the approve/reject endpoints, which
    only have the job id (not codebase_name) from the URL path."""
    with pg_raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, codebase_name, repo_url, branch, status,
                       error_message, output_dir, pages, created_at, updated_at,
                       last_commit_sha, requested_by, product_id, reviewed_by,
                       reviewed_at, review_note
                  FROM {DB_SCHEMA}.codewiki_doc_jobs
                 WHERE id = %s::uuid
                """,
                (job_id,),
            )
            row = cur.fetchone()
            return _row_to_dict(row, mask=mask) if row else None


def _get_job_by_repo_branch(repo_url: str, branch: str) -> dict | None:
    """Look up a job by its (repo_url, branch) uniqueness key.

    Always returns a MASKED dict -- every current call site only uses this
    for the "does a job already exist for this repo?" duplicate-detection
    check (see generate_docs()), which only needs codebase_name/status,
    never the url itself. If a future caller needs the raw url from this
    lookup, mirror the `mask` parameter pattern from _get_job_by_codebase.
    """
    with pg_raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, codebase_name, repo_url, branch, status,
                       error_message, output_dir, pages, created_at, updated_at
                  FROM {DB_SCHEMA}.codewiki_doc_jobs
                 WHERE repo_url = %s AND branch = %s
                """,
                (repo_url, branch),
            )
            row = cur.fetchone()
            return _row_to_dict(row) if row else None


def _create_job(
    codebase_name: str, repo_url: str, branch: str,
    status: str, requested_by: str, product_id: Optional[str] = None,
) -> dict:
    """Insert a new job row and return the full record.

    status is 'pending' (job-execution-ready — admin auto-approve path,
    matching Products/Codebase's "admin auto-approves own submissions") or
    'pending_approval' (governance-gated — see generate_docs()).
    """
    with pg_raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {DB_SCHEMA}.codewiki_doc_jobs
                    (id, codebase_name, repo_url, branch, status, pages,
                     requested_by, product_id)
                VALUES (%s, %s, %s, %s, %s, '[]'::jsonb, %s, %s::uuid)
                RETURNING id, codebase_name, repo_url, branch, status,
                          error_message, output_dir, pages, created_at, updated_at,
                          last_commit_sha, requested_by, product_id, reviewed_by,
                          reviewed_at, review_note
                """,
                (str(uuid.uuid4()), codebase_name, repo_url, branch, status,
                 requested_by, product_id),
            )
            row = cur.fetchone()
            conn.commit()
            return _row_to_dict(row)


def _reset_job_for_regen(job_id: str) -> None:
    """Mark an existing job as pending so the worker can overwrite it.

    Also clears `logs` immediately, at reset time, rather than leaving that
    to the worker's own first _update_job(..., logs="") call once it
    actually picks up the job (see workers/codewiki_worker.py's
    run_codewiki_doc_job()). Without this, there's a real window --
    however brief -- between this reset (status becomes 'pending') and the
    worker starting the new run where GET /codebases/{name}/logs still
    returns the PREVIOUS attempt's logs (including its full failure
    traceback, if this reset followed a failure). The frontend polls logs
    for any 'pending'/'running' job (see CodeWikiDocs.jsx), so a user
    clicking Retry/Regenerate right after a failure could see that old
    failure's traceback flash on screen again, looking like the brand-new
    attempt had already failed before it even started.
    """
    with pg_raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {DB_SCHEMA}.codewiki_doc_jobs
                   SET status = 'pending',
                       error_message = NULL,
                       logs = '',
                       updated_at = NOW()
                 WHERE id = %s
                """,
                (job_id,),
            )
            conn.commit()


def _is_running_or_pending(status: str) -> bool:
    return status in ("pending", "running", "pending_approval")


def _require_owner_approver_or_admin(current_user: dict, job: dict) -> None:
    """Gate for /retry and /delete — both re-trigger or destroy a real clone
    +processing job, same risk class as /generate, but previously had NO
    RBAC at all beyond plain login (any authenticated user could retry or
    delete anyone else's CodeWiki job for any codebase). Allows the
    original submitter (routine cleanup/retry of your own work), any
    admin/senior approver, or admin outright.
    """
    if is_admin(current_user):
        return
    if current_user.get("email") == job.get("requested_by"):
        return
    if can_approve(current_user):
        return
    raise HTTPException(status_code=403, detail="Only the original submitter, a senior approver, or an admin can do this")


def _resolve_output_dir(job: dict, codebase_name: str) -> Path:
    """Resolve a completed job's on-disk output directory.

    Prefers the job row's own stored `output_dir` (set by the worker at
    completion); falls back to the CODEWIKI_DOCS_DIR-derived legacy path
    only if that column is missing (old rows) -- same resolution rule
    already used by get_page()/regenerate_docs(), pulled out here so the
    new page-tree/search endpoints share it instead of re-deriving it.
    """
    output_dir = job.get("output_dir") or str(
        Path(_require_codewiki_docs_dir()) / codebase_name / job["branch"] / "latest"
    )
    return Path(output_dir)


def _title_from_key(key: str) -> str:
    """Mirror workers/codewiki_worker.py's _list_markdown_pages() title
    casing exactly, so a tree-node's title matches the flat page list's
    title for the same underlying file."""
    return key.replace("_", " ").replace("-", " ").title()


def _build_tree_children(children_map: dict, output_dir: Path, seen_files: set) -> list:
    """Recursively convert module_tree.json's `{key: {components, children}}`
    shape into the frontend-facing `[{id, title, type, path, children?}]`
    shape expected by GET .../page-tree.

    A node is a "folder" iff its module_tree.json entry has a non-empty
    `children` dict (i.e. the CLI decomposed it into sub-modules); otherwise
    it's a "file" leaf. Folder nodes still carry their own `path` when a
    `.md` file exists for them (a folder module usually has its own
    overview-style doc, e.g. `ai_ui_frontend.md`, in addition to its
    children), so clicking the folder's own row/node can open that doc
    directly rather than only being able to expand it.

    `seen_files` is mutated in place (shared across the whole recursive
    walk) so the caller can compute which flat `pages` entries are
    "orphans" -- generated .md files with no corresponding tree key at all.
    See _attach_orphans_by_prefix() below for how those are actually
    reattached to the right place in the tree (NOT surfaced as extra
    top-level nodes -- see that function's docstring for why).
    """
    nodes = []
    for key, info in sorted((children_map or {}).items()):
        info = info or {}
        sub_children = info.get("children") or {}
        is_folder = bool(sub_children)
        md_name = f"{key}.md"
        has_file = (output_dir / md_name).exists()
        if has_file:
            seen_files.add(md_name)
        node = {
            "id": key,
            "title": _title_from_key(key),
            "type": "folder" if is_folder else "file",
            "path": md_name if has_file else None,
        }
        if is_folder:
            node["children"] = _build_tree_children(sub_children, output_dir, seen_files)
        nodes.append(node)
    return nodes


def _attach_orphans_by_prefix(top_nodes: list, orphan_files: list) -> None:
    """Reattach "orphan" pages -- generated `.md` files with no
    module_tree.json entry of their own -- to their real parent module,
    inferred from the CLI's own underlying naming convention, instead of
    dumping them all as extra TOP-LEVEL nodes.

    Why this matters: confirmed via direct inspection of a real generation
    (ainxt/uat) that orphans are NOT unrelated stray files -- every single
    one of them is named as `<some_existing_module_key>_<suffix>.md` (e.g.
    `agents_feature_card.md` for the `agents_feature` module, which module_
    tree.json already contains at `abstudio_frontend -> agents_feature`,
    just without `agents_feature_card` listed as one of its children).
    This happens because the CLI's forced-split/context-overflow fallback
    path (see workers/codewiki_worker.py's docstring on
    MAX_FILE_TOKENS_BEFORE_SNIPPET / FORCED_SPLIT_SYSTEM_PROMPT) writes
    extra sub-module files directly to disk without updating
    module_tree.json to record the new nesting.

    Treating every orphan as a top-level node (the previous behavior) made
    the sidebar's top level balloon from ~25 real top-level modules to
    ~184 entries for a mid-sized repo, since none of these orphan files
    would nest correctly under their real parent module.

    Algorithm: for each orphan, find the LONGEST existing node id such that
    orphan_stem == "{node_id}_{suffix}" (i.e. the orphan's name starts with
    an existing node's id followed by an underscore), and attach it as that
    node's child -- promoting that node from "file" to "folder" if it
    wasn't already one. Runs in passes so an orphan can attach under
    ANOTHER orphan that was itself just attached in an earlier pass (e.g.
    `mcp_system_registry_master.md` attaches under the orphan
    `mcp_system_registry.md`, not directly under `mcp_system_registry.md`'s
    own inferred top-level ancestor) -- confirmed this exact chain occurs
    in the real `ainxt/uat` sample (3 such cases). Any orphan matching
    nothing after all passes (never observed in testing, but structurally
    possible) is left as a genuine top-level node, same as before, so no
    file silently disappears from the tree.
    """
    if not orphan_files:
        return

    node_by_id: dict = {}

    def index_all(nodes):
        for n in nodes:
            node_by_id[n["id"]] = n
            if n.get("children"):
                index_all(n["children"])

    index_all(top_nodes)

    remaining = list(orphan_files)
    for _pass in range(10):  # generous bound on orphan-under-orphan chain depth
        if not remaining:
            break
        still_remaining = []
        progress = False
        for fname in remaining:
            stem = fname[:-3]  # strip ".md"
            candidates = [k for k in node_by_id if stem.startswith(k + "_")]
            if not candidates:
                still_remaining.append(fname)
                continue
            parent_key = max(candidates, key=len)  # longest/most-specific match
            parent_node = node_by_id[parent_key]
            parent_node["type"] = "folder"
            parent_node.setdefault("children", [])
            new_node = {
                "id": stem,
                "title": _title_from_key(stem),
                "type": "file",
                "path": fname,
            }
            parent_node["children"].append(new_node)
            node_by_id[stem] = new_node
            progress = True
        remaining = still_remaining
        if not progress:
            break

    # Any orphan that never matched anything (shouldn't happen in practice,
    # per the real-data testing above) still needs to be reachable --
    # fall back to a top-level node exactly like the old behavior, rather
    # than silently dropping it.
    for fname in remaining:
        key = fname[:-3]
        top_nodes.append({
            "id": key,
            "title": _title_from_key(key),
            "type": "file",
            "path": fname,
        })


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/generate")
def generate_docs(payload: GenerateDocsRequest, current_user: dict = Depends(get_current_user)) -> JobResponse:
    """Create a new CodeWiki documentation job.

    Governance-gated like Codebase indexing: admin submissions auto-approve
    and enqueue immediately; everyone else lands in 'pending_approval' until
    POST /requests/{id}/approve. Previously this had no RBAC at all beyond
    plain login — any authenticated user could have any git URL (including
    one carrying embedded credentials) cloned and processed server-side.
    """
    if not payload.codebase_name:
        raise HTTPException(status_code=400, detail="codebase_name is required.")
    if not payload.repo_url:
        raise HTTPException(status_code=400, detail="repo_url is required.")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
    # codebase_name/branch flow into filesystem Path(...) construction, so
    # they're checked against the identifier allow-list.
    is_valid, field_errors, sanitized = validate_codewiki_generate_request(payload)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    payload.codebase_name = sanitized["codebase_name"]
    payload.repo_url = sanitized["repo_url"]
    payload.branch = sanitized["branch"]

    # Enforce uniqueness rules
    existing_by_name = _get_job_by_codebase(payload.codebase_name)
    if existing_by_name is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Codebase name '{payload.codebase_name}' already exists. "
                "Use the regenerate option to rebuild its documentation."
            ),
        )

    existing_by_repo = _get_job_by_repo_branch(payload.repo_url, payload.branch)
    if existing_by_repo is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Documentation already exists for {mask_repo_url(payload.repo_url)} "
                f"({payload.branch}) as '{existing_by_repo['codebase_name']}'. "
                "Regenerate that codebase instead."
            ),
        )

    # Reachability check up front -- public repos pass with no auth touched
    # at all; private repos are retried with the submitter's own stored git
    # token; if it's still unreachable this fails clearly right here, rather
    # than the submitter only finding out after approval + a background job
    # run.
    try:
        _resolve_repo_reachability(payload.repo_url, payload.branch, current_user)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    submitter = current_user.get("email") or current_user.get("sub", "unknown")
    submitter_is_admin = is_admin(current_user)
    status = "pending" if submitter_is_admin else "pending_approval"

    job = _create_job(
        payload.codebase_name, payload.repo_url, payload.branch,
        status=status, requested_by=submitter, product_id=payload.product_id,
    )

    if submitter_is_admin:
        enqueue_codewiki_job(
            job_id=job["id"],
            codebase_name=payload.codebase_name,
            repo_url=payload.repo_url,
            branch=payload.branch,
            requested_by=submitter,
        )
    else:
        _notify_approvers_codewiki(
            job["id"], payload.codebase_name, submitter, payload.product_id,
            repo_url=payload.repo_url, branch=payload.branch,
        )

    return JobResponse(**job)


def _notify_approvers_codewiki(
    job_id: str, codebase_name: str, submitter_email: str, product_id: Optional[str],
    repo_url: str = "", branch: str = "",
) -> None:
    """Notify whoever should approve this CodeWiki generation request.

    Same routing as Products/Codebase indexing (auth.rbac.resolve_request_approvers):
    the submitter's own HOD + nominated delegates, falling back to every
    admin/senior-approver (optionally scoped to product_id's mapped
    departments) when the submitter has no resolvable HOD.

    metadata uses the SAME shape as index_router._notify_approvers_codebase
    (entity_type / request_id / submitted_by / action) so
    ai-ui/src/components/Inbox.jsx's UniversalInboxActions can render
    Approve/Reject for this item exactly like it does for codebase_approval
    -- previously this used a different, ad-hoc metadata shape (`job_id`
    instead of `request_id`, no repo_url/branch) that Inbox.jsx had no
    matching render branch for at all, so the notification showed up with
    no action buttons.
    """
    try:
        from store.inbox_store import publish_inbox_item
        from db.database import SessionLocal
        from db.models import User
        from sqlalchemy import or_
        from auth.rbac import resolve_request_approvers

        approvers = resolve_request_approvers(submitter_email or "")
        hod_email = approvers.get("hod_email")
        delegatee_emails = approvers.get("delegatee_emails") or []
        recipient_emails = ([hod_email] if hod_email else []) + delegatee_emails

        db = SessionLocal()
        try:
            if recipient_emails:
                from sqlalchemy import func
                recipients = db.query(User).filter(
                    func.lower(User.email).in_([e.lower() for e in recipient_emails]),
                    User.is_active == True).all()
            else:
                from core.config import APPROVAL_AD_LEVEL as _APPROVAL_LEVEL
                q = db.query(User).filter(
                    or_(User.ad_level <= _APPROVAL_LEVEL, User.role == "admin"),
                    User.is_active == True,
                )
                if product_id:
                    from db.models import DeptProductMapping
                    depts = [r.department for r in db.query(DeptProductMapping).filter(DeptProductMapping.product_id == product_id).all()]
                    if depts:
                        q = q.filter(User.department.in_(depts))
                recipients = q.all()
            for u in recipients:
                publish_inbox_item(
                    user_id=str(u.id),
                    type="codewiki_approval",
                    title=f"[CodeWiki] New documentation request: {codebase_name}",
                    body=f"**{submitter_email}** requested CodeWiki documentation for **{codebase_name}** (branch: `{branch}`). Review and approve or reject.",
                    source_id=job_id,
                    metadata={
                        "entity_type": "codewiki",
                        "request_id": job_id,
                        "codebase_name": codebase_name,
                        "repo_url": mask_repo_url(repo_url) if repo_url else "",
                        "branch": branch,
                        "submitted_by": submitter_email,
                        "action": "submit",
                    },
                )
        finally:
            db.close()
    except Exception as e:
        from core.logger import logger
        logger.warning(f"CodeWikiRouter: failed to notify approvers for {codebase_name}: {e}")


@router.post("/requests/{job_id}/approve")
def approve_codewiki_request(job_id: str, body: ReviewAction = ReviewAction(), current_user: dict = Depends(get_current_user)) -> dict:
    """Approve a pending_approval CodeWiki request and enqueue generation.

    Authorisation mirrors index_router.approve_index_request exactly:
    admin, OR the submitter's HOD/delegate, OR (no resolvable HOD) any
    senior approver — department-scoped only when a product_id is attached.
    """
    job = _get_job_by_id(job_id, mask=False)
    if job is None:
        raise HTTPException(status_code=404, detail="Request not found")
    if job["status"] != "pending_approval":
        raise HTTPException(status_code=400, detail=f"Request is already '{job['status']}'")

    approver_email = current_user.get("email", "")
    if job.get("requested_by") == approver_email:
        raise HTTPException(status_code=403, detail="Cannot approve your own CodeWiki request (4-eyes principle)")

    if not (is_admin(current_user) or is_request_approver(current_user, job.get("requested_by") or "") or can_approve(current_user)):
        raise HTTPException(status_code=403, detail="Only the submitter's HOD (or their delegate), a senior approver, or an admin can approve CodeWiki requests")

    if not (is_admin(current_user) or is_request_approver(current_user, job.get("requested_by") or "")) and job.get("product_id"):
        with pg_raw_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT department FROM {DB_SCHEMA}.dept_product_mappings WHERE product_id::text = %s", (job["product_id"],))
                product_depts = [r[0] for r in cur.fetchall()]
        if product_depts and current_user.get("department", "") not in product_depts:
            raise HTTPException(status_code=403, detail="Your department is not mapped to this request's product")

    with pg_raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {DB_SCHEMA}.codewiki_doc_jobs
                       SET status = 'pending', reviewed_by = %s, reviewed_at = NOW(), review_note = %s
                     WHERE id = %s::uuid""",
                (approver_email, body.note, job_id),
            )
        conn.commit()

    enqueue_codewiki_job(
        job_id=job_id, codebase_name=job["codebase_name"], repo_url=job["repo_url"], branch=job["branch"],
        requested_by=job.get("requested_by"),
    )

    # Remove the original "pending approval" notification for every OTHER
    # recipient (other HODs/delegates/approvers notified at submit time) --
    # otherwise they keep seeing this as still-actionable indefinitely (see
    # store.inbox_store.delete_all_by_source's docstring).
    from store.inbox_store import delete_all_by_source
    delete_all_by_source("codewiki_approval", job_id)

    return {"id": job_id, "status": "pending"}


@router.post("/requests/{job_id}/reject")
def reject_codewiki_request(job_id: str, body: ReviewAction = ReviewAction(), current_user: dict = Depends(get_current_user)) -> dict:
    """Reject a pending_approval CodeWiki request — same authorisation as approve."""
    job = _get_job_by_id(job_id, mask=False)
    if job is None:
        raise HTTPException(status_code=404, detail="Request not found")
    if job["status"] != "pending_approval":
        raise HTTPException(status_code=400, detail=f"Request is already '{job['status']}'")

    approver_email = current_user.get("email", "")
    if job.get("requested_by") == approver_email:
        raise HTTPException(status_code=403, detail="Cannot reject your own CodeWiki request")

    if not (is_admin(current_user) or is_request_approver(current_user, job.get("requested_by") or "") or can_approve(current_user)):
        raise HTTPException(status_code=403, detail="Only the submitter's HOD (or their delegate), a senior approver, or an admin can reject CodeWiki requests")

    with pg_raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {DB_SCHEMA}.codewiki_doc_jobs
                       SET status = 'rejected', reviewed_by = %s, reviewed_at = NOW(), review_note = %s
                     WHERE id = %s::uuid""",
                (approver_email, body.note, job_id),
            )
        conn.commit()

    from store.inbox_store import delete_all_by_source
    delete_all_by_source("codewiki_approval", job_id)

    return {"id": job_id, "status": "rejected"}


@router.get("/requests/pending")
def list_pending_codewiki_requests(current_user: dict = Depends(get_current_user)) -> dict:
    """Pending-approval queue — mirrors products_router.list_pending_products."""
    if not (is_admin(current_user) or can_approve(current_user)):
        raise HTTPException(status_code=403, detail="Approver access required")
    caller_email = current_user.get("email", "")
    with pg_raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, codebase_name, repo_url, branch, requested_by, created_at
                      FROM {DB_SCHEMA}.codewiki_doc_jobs
                     WHERE status = 'pending_approval' AND requested_by != %s
                     ORDER BY created_at DESC""",
                (caller_email,),
            )
            rows = cur.fetchall()
    return {
        "requests": [
            {
                "id": str(r[0]), "codebase_name": r[1], "repo_url": mask_repo_url(r[2]),
                "branch": r[3], "requested_by": r[4],
                "created_at": r[5].isoformat() if r[5] else None,
            }
            for r in rows
        ]
    }


@router.post("/regenerate")
def regenerate_docs(payload: RegenerateDocsRequest, current_user: dict = Depends(get_current_user)) -> dict:
    """Regenerate documentation for an existing codebase name.

    Behavior:
    - If payload.confirm is False: return a dry-run listing modules that would be
      regenerated/created/removed and whether overview.md would be regenerated.
    - If payload.confirm is True: enqueue a job to perform the incremental
      regeneration for the listed modules (or full regen if dry-run indicates full).
    """
    payload.codebase_name = payload.codebase_name.strip()
    if not payload.codebase_name:
        raise HTTPException(status_code=400, detail="codebase_name is required.")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    is_valid, field_errors, sanitized = validate_codewiki_regenerate_request(payload)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    payload.codebase_name = sanitized["codebase_name"]

    # mask=False: this handler needs the REAL repo_url below, to clone the
    # repo itself (computing the changed-files diff) and to re-enqueue the
    # worker job -- both are genuine git/internal operations, never
    # returned directly to the client (the only things sent back from this
    # endpoint are the dry-run summary and, on the "already up to date" /
    # confirmed-regenerate paths, a freshly re-fetched MASKED row -- see
    # the `refreshed = _get_job_by_codebase(...)` calls further down,
    # which correctly use the default mask=True since those go straight
    # into the HTTP response).
    existing = _get_job_by_codebase(payload.codebase_name, mask=False)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail=f"No documentation found for codebase '{payload.codebase_name}'.",
        )
    _require_owner_approver_or_admin(current_user, existing)

    if _is_running_or_pending(existing["status"]):
        raise HTTPException(
            status_code=409,
            detail=(
                f"A job for '{payload.codebase_name}' is already "
                f"{existing['status']}. Please wait for it to finish."
            ),
        )

    last_sha = existing.get("last_commit_sha")

    # Cheap remote check — `git ls-remote` returns just the branch's current
    # HEAD sha over the network, no clone at all. This only needs to answer
    # "has anything changed since the commit we last documented" — the real
    # file/module-level diff is computed by codewiki's own `--update
    # --compare-to <sha>` once a job is actually enqueued (see
    # workers/codewiki_worker.py), not duplicated here. The previous
    # implementation cloned the repo itself and hand-mapped changed files to
    # modules via module_tree.json — meaningful work, but it was also
    # unreachable in practice: `existing["last_commit_sha"]` was never
    # actually selected from the DB (see _get_job_by_codebase, fixed above),
    # so `last_sha` here was always None and every regenerate always took
    # the "no commit on record" branch regardless of real DB state.
    #
    # _resolve_repo_reachability retries with the CALLER's own git token if
    # the plain check fails (private repo, or a token added since the repo
    # was first submitted) before giving up with a clear message.
    try:
        latest_sha = _resolve_repo_reachability(existing["repo_url"], existing["branch"], current_user)
    except RuntimeError as exc:
        detail = str(exc)
        if not payload.confirm:
            return {"note": detail}
        raise HTTPException(status_code=500, detail=detail)

    if last_sha and latest_sha == last_sha:
        if not payload.confirm:
            return {"note": "Already up to date — no changes since the last generated commit.", "commit_sha": last_sha}
        return dict(_get_job_by_codebase(payload.codebase_name))

    if not payload.confirm:
        if last_sha:
            return {
                "note": f"Branch has moved since the last documented commit ({last_sha[:8]} → {latest_sha[:8]}). Confirm to regenerate — only the modules affected by the diff will be regenerated.",
                "current_commit_sha": last_sha,
                "latest_commit_sha": latest_sha,
            }
        return {
            "note": "No previously-documented commit on record for this codebase — regenerate will run a full generation.",
            "latest_commit_sha": latest_sha,
        }

    # Confirmed. With a known last_sha this is an incremental regenerate:
    # reuse the existing output_dir and pass compare_to_sha through so the
    # worker invokes `codewiki generate --update --compare-to <last_sha>`
    # (codewiki's own native incremental mode) instead of a fresh full
    # generate. Without a last_sha there's nothing to compare against, so
    # this falls back to a full regenerate into a fresh output_dir, same as
    # a first-time /generate.
    _reset_job_for_regen(existing["id"])
    enqueue_codewiki_job(
        job_id=existing["id"],
        codebase_name=existing["codebase_name"],
        repo_url=existing["repo_url"],
        branch=existing["branch"],
        output_dir=existing.get("output_dir") if last_sha else None,
        compare_to_sha=last_sha,
        requested_by=existing.get("requested_by"),
    )

    return dict(_get_job_by_codebase(payload.codebase_name))


@router.post("/retry")
def retry_docs(payload: RegenerateDocsRequest, current_user: dict = Depends(get_current_user)) -> JobResponse:
    """Re-run a FAILED job from scratch, with no dry-run/confirmation step.

    Unlike /regenerate (which diffs against the last successfully-documented
    commit to compute an incremental set of modules to touch — meaningless
    for a job that never completed successfully), /retry simply resets the
    job back to 'pending' and re-enqueues the exact same (repo_url, branch)
    from scratch, exactly like a fresh /generate. Only allowed when the
    existing job's status is 'failed', so this can't be used to interrupt a
    job that's currently running/pending, nor to blindly re-run one that
    already completed (use /regenerate for that).
    """
    payload.codebase_name = payload.codebase_name.strip()
    if not payload.codebase_name:
        raise HTTPException(status_code=400, detail="codebase_name is required.")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    is_valid, field_errors, sanitized = validate_codewiki_regenerate_request(payload)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    payload.codebase_name = sanitized["codebase_name"]

    # mask=False: existing["repo_url"] is re-enqueued below (a real git
    # clone operation, not returned in this response).
    existing = _get_job_by_codebase(payload.codebase_name, mask=False)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail=f"No documentation found for codebase '{payload.codebase_name}'.",
        )
    _require_owner_approver_or_admin(current_user, existing)

    if existing["status"] != "failed":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Codebase '{payload.codebase_name}' is not in a failed state "
                f"(current status: {existing['status']}) — retry is only available "
                "for failed generations."
            ),
        )

    _reset_job_for_regen(existing["id"])
    enqueue_codewiki_job(
        job_id=existing["id"],
        codebase_name=existing["codebase_name"],
        repo_url=existing["repo_url"],
        branch=existing["branch"],
        requested_by=existing.get("requested_by"),
    )

    refreshed = _get_job_by_codebase(payload.codebase_name)
    return JobResponse(**refreshed)


@router.get("/codebases")
def list_codebases(current_user: dict = Depends(get_current_user)) -> List[JobResponse]:
    """List codebases (only one row per codebase_name by schema design).

    Completed documentation is visible to everyone — that's the point of
    the feature (browsable docs for new joiners/reviewers). A job that
    hasn't completed yet (pending_approval/pending/running/failed/rejected)
    is only shown to its submitter, an admin, or a senior approver —
    previously this returned every row regardless of status to any
    authenticated user, which bypassed /generate's approval gate entirely:
    anyone could see who submitted what, including rejected/pending
    requests they had no part in.
    """
    caller_email = current_user.get("email", "")
    caller_can_see_all = is_admin(current_user) or can_approve(current_user)
    with pg_raw_connection() as conn:
        with conn.cursor() as cur:
            if caller_can_see_all:
                cur.execute(
                    f"""
                    SELECT id, codebase_name, repo_url, branch, status,
                           error_message, output_dir, pages, created_at, updated_at
                      FROM {DB_SCHEMA}.codewiki_doc_jobs
                     ORDER BY codebase_name ASC
                    """
                )
            else:
                cur.execute(
                    f"""
                    SELECT id, codebase_name, repo_url, branch, status,
                           error_message, output_dir, pages, created_at, updated_at
                      FROM {DB_SCHEMA}.codewiki_doc_jobs
                     WHERE status = 'completed' OR requested_by = %s
                     ORDER BY codebase_name ASC
                    """,
                    (caller_email,),
                )
            rows = cur.fetchall()
    return [JobResponse(**_row_to_dict(row)) for row in rows]


@router.get("/codebases/{codebase_name}")
def get_codebase(codebase_name: str, current_user: dict = Depends(get_current_user)) -> JobResponse:
    """Return status and page list for a single codebase.

    Same visibility rule as list_codebases(): a non-completed job is only
    visible to its submitter, an admin, or a senior approver.
    """
    job = _get_job_by_codebase(codebase_name)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"Codebase '{codebase_name}' not found.",
        )
    if job["status"] != "completed" and job.get("requested_by") != current_user.get("email", "") and not (is_admin(current_user) or can_approve(current_user)):
        raise HTTPException(
            status_code=404,
            detail=f"Codebase '{codebase_name}' not found.",
        )
    return JobResponse(**job)


@router.get("/codebases/{codebase_name}/pages/{page_path:path}")
def get_page(codebase_name: str, page_path: str, _user: dict = Depends(get_current_user)) -> dict:
    """Return the raw Markdown content for a specific documentation page."""
    job = _get_job_by_codebase(codebase_name)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"Codebase '{codebase_name}' not found.",
        )

    if job["status"] != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Documentation for '{codebase_name}' is not ready (status: {job['status']}).",
        )

    output_dir = job.get("output_dir") or str(
        Path(_require_codewiki_docs_dir()) / codebase_name / job["branch"] / "latest"
    )
    safe_path = page_path.replace("..", "").lstrip("/")

    # Two on-disk layouts exist:
    #  - Legacy jobs (generated by the old in-process worker): pages live
    #    under <output_dir>/docs/<file>.md.
    #  - Current jobs (generated by the real `codewiki generate
    #    --github-pages --output <output_dir>` CLI, same as a manual
    #    terminal run): pages are written flat, directly under
    #    <output_dir>/<file>.md.
    # Try the flat layout first (current), then fall back to the legacy
    # docs/ subfolder so old completed wikis keep working unmodified.
    candidates = [
        Path(output_dir) / safe_path,
        Path(output_dir) / "docs" / safe_path,
    ]
    file_path = next((c for c in candidates if c.exists()), candidates[0])

    try:
        file_path.resolve().relative_to(Path(output_dir).resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid page path.") from exc

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Page not found.")

    content = file_path.read_text(encoding="utf-8")
    return {"content": content}


@router.get("/codebases/{codebase_name}/page-tree")
def get_page_tree(codebase_name: str, _user: dict = Depends(get_current_user)) -> dict:
    """Return the hierarchical module tree (folders/files) for a completed
    codebase, derived from the `codewiki` CLI's own `module_tree.json`
    (written alongside the generated pages in `output_dir`, one entry per
    top-level module, each optionally decomposed into `children`).

    Shape:
        {
          "root_label": "<codebase_name>",
          "children": [
            {"id": "...", "title": "...", "type": "folder"|"file",
             "path": "<file>.md" | null, "children": [...] },
            ...
          ]
        }

    Returns an empty `children` list (never an error) if `module_tree.json`
    is missing or unreadable -- this happens for jobs generated before this
    endpoint existed, or if a job's on-disk output was hand-edited/removed.
    The frontend treats an empty tree as "no hierarchy available" and falls
    back to a flat file listing, so this is purely additive: nothing that
    worked before this endpoint existed stops working if the tree can't be
    built for some reason.
    """
    job = _get_job_by_codebase(codebase_name)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Codebase '{codebase_name}' not found.")
    if job["status"] != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Documentation for '{codebase_name}' is not ready (status: {job['status']}).",
        )

    output_dir = _resolve_output_dir(job, codebase_name)
    tree_path = output_dir / "module_tree.json"

    result = {"root_label": codebase_name, "children": []}
    if not tree_path.exists():
        return result

    try:
        raw = json.loads(tree_path.read_text(encoding="utf-8"))
    except Exception:
        # Malformed/unreadable module_tree.json -- degrade to "no tree"
        # rather than a 500, since the flat page list is still fully usable.
        return result

    if not isinstance(raw, dict):
        return result

    seen_files: set = set()
    result["children"] = _build_tree_children(raw, output_dir, seen_files)

    # Orphan pages: flat `.md` files that exist on disk but never showed up
    # anywhere in the tree walk above (no module_tree.json key referenced
    # them). Reattach each one under its real parent module, inferred from
    # the CLI's own naming convention -- see _attach_orphans_by_prefix()'s
    # docstring for why this is necessary (treating every orphan as an
    # extra TOP-LEVEL node made the sidebar's top level show ~184 entries
    # for a repo with only ~25 real top-level modules). `overview.md` is
    # deliberately excluded -- it's handled as its own always-first special
    # case by the frontend, not as a tree node.
    try:
        existing_md = {
            p.name for p in output_dir.iterdir()
            if p.is_file() and p.suffix == ".md" and p.name.lower() != "overview.md"
        }
    except Exception:
        existing_md = set()

    orphan_files = sorted(existing_md - seen_files)
    _attach_orphans_by_prefix(result["children"], orphan_files)

    return result



# Matches a fenced code block (``` ... ```), across multiple lines.
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
# Matches an inline code span (`...`) on a single line.
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
# Matches a Markdown link/image target, i.e. the "(url)" part of
# "[label](url)" or "![alt](url)" -- keeps the "]" so the visible label text
# in front of it is left untouched.
_LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")


def _visible_text_for_search(markdown_text: str) -> str:
    """Strip out every part of a page's raw Markdown that the frontend's
    content-search highlighter (`remarkHighlightSearch` in
    CodeWikiDocs.jsx) never highlights, so `match_count` here reflects the
    number of highlights the user will actually SEE when they open the
    page -- not a raw substring count over the whole file.

    The frontend walks the parsed Markdown AST and only wraps plain-text
    nodes in <mark>; it explicitly skips `code`/`inlineCode` nodes (fenced
    code blocks -- including Mermaid diagram source -- and inline code
    spans render as diagrams/syntax-highlighted code, not prose, so a
    match there isn't something the reader can see highlighted) and never
    touches a link's URL (only its visible label text is prose).

    This mirrors that AST-based behavior well enough for counting purposes
    using plain regexes (verified against the real remark/mdast pipeline
    across multiple real generated docs -- identical counts): remove fenced
    code blocks first (regex is greedy-safe here since fences never nest),
    then inline code spans, then link/image targets (keeping the "]" so
    the label text before it survives untouched).
    """
    text = _FENCED_CODE_RE.sub("", markdown_text)
    text = _INLINE_CODE_RE.sub("", text)
    text = _LINK_TARGET_RE.sub("]", text)
    # Cosmetic cleanup for snippet readability only -- removing an inline
    # code span often leaves its now-empty surrounding literal parens
    # behind, e.g. "Router (`routers/auth_router.py`) is" -> "Router () is".
    # Collapsing "()" -> "" (and the resulting doubled whitespace) never
    # removes any alphanumeric character a search query could match, so it
    # can't change match_count -- it only tidies up what the snippet shows.
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


@router.get("/codebases/{codebase_name}/search")
def search_pages(codebase_name: str, q: str = "", _user: dict = Depends(get_current_user)) -> dict:
    """Full-text search over every generated page's raw Markdown content
    (unlike the sidebar's client-side title filter, this looks INSIDE each
    page). Case-insensitive substring match.

    `match_count` counts only matches inside the page's VISIBLE prose (see
    `_visible_text_for_search`) so it agrees with the number of highlighted
    matches the reader actually sees after clicking through -- a match
    that only exists inside a code fence or a link URL is invisible to the
    reader and would otherwise make the count look wrong/inflated.

    Returns:
        {"query": q, "results": [{title, file, path, snippet, match_count}, ...]}
    Results are ordered by match_count descending (most-relevant first),
    then alphabetically by file for ties.
    """
    query = (q or "").strip()
    if not query:
        return {"query": "", "results": []}

    job = _get_job_by_codebase(codebase_name)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Codebase '{codebase_name}' not found.")
    if job["status"] != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Documentation for '{codebase_name}' is not ready (status: {job['status']}).",
        )

    output_dir = _resolve_output_dir(job, codebase_name)
    pages = job.get("pages") or []

    query_lower = query.lower()
    results = []
    for page in pages:
        rel_path = page.get("path") or page.get("file")
        if not rel_path:
            continue
        # Same flat-then-legacy-docs-subfolder resolution as get_page().
        candidates = [output_dir / rel_path, output_dir / "docs" / rel_path]
        file_path = next((c for c in candidates if c.exists()), None)
        if file_path is None:
            continue

        try:
            raw_text = file_path.read_text(encoding="utf-8")
        except Exception:
            continue

        # Search/count/snippet all operate on the VISIBLE text only (see
        # _visible_text_for_search) -- a match inside a code fence or a
        # link URL isn't something the reader will ever see highlighted
        # after clicking through, so it shouldn't count or be snippeted.
        text = _visible_text_for_search(raw_text)
        text_lower = text.lower()
        match_count = text_lower.count(query_lower)
        if match_count == 0:
            continue

        first_idx = text_lower.find(query_lower)
        snippet_start = max(0, first_idx - 60)
        snippet_end = min(len(text), first_idx + len(query) + 60)
        snippet = text[snippet_start:snippet_end].replace("\n", " ").strip()
        if snippet_start > 0:
            snippet = "…" + snippet
        if snippet_end < len(text):
            snippet = snippet + "…"

        results.append({
            "title": page.get("title") or _title_from_key(Path(rel_path).stem),
            "file": page.get("file") or Path(rel_path).name,
            "path": rel_path,
            "snippet": snippet,
            "match_count": match_count,
        })

    results.sort(key=lambda r: (-r["match_count"], r["file"].lower()))
    return {"query": query, "results": results}


@router.get("/codebases/{codebase_name}/logs")
def get_logs(codebase_name: str, _user: dict = Depends(get_current_user)) -> dict:
    """Return the captured terminal output (stdout/stderr) of the CLI
    subprocess for a codebase's most recent generation job. The frontend
    polls this while a wiki is pending/running to show live logs, mirroring
    what an operator would see running `codewiki generate --github-pages
    --verbose --output <dir>` by hand in a terminal.
    """
    with pg_raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT status, logs
                  FROM {DB_SCHEMA}.codewiki_doc_jobs
                 WHERE codebase_name = %s
                """,
                (codebase_name,),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Codebase '{codebase_name}' not found.",
        )
    return {"status": row[0], "logs": row[1] or ""}


@router.post("/delete")
def delete_codebase(payload: RegenerateDocsRequest, current_user: dict = Depends(get_current_user)) -> dict:
    """Enhanced delete: allow deleting codebases in any status and attempt to
    cancel running/pending RQ jobs before removing files/DB row. Returns a
    detailed summary so the UI can reflect that the generation was stopped and
    artifacts removed.
    """
    """Delete a codebase's job row and remove its generated files.

    This endpoint cancels any running RQ job for the codebase and then deletes
    the database row. It also removes the output_dir from disk if present.

    The operation is destructive and irreversible.
    """
    name = payload.codebase_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="codebase_name is required.")

    existing = _get_job_by_codebase(name)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"No documentation found for codebase '{name}'.")
    _require_owner_approver_or_admin(current_user, existing)

    # Attempt to cancel an RQ job with the same id (best-effort)
    cancel_ok = False
    cancel_err = None
    try:
        cancel_ok = cancel_job(existing["id"])
    except Exception as _e:
        cancel_err = str(_e)

    # Remove files on disk if present
    output_dir = existing.get("output_dir")
    removed = False
    remove_err = None
    if output_dir:
        try:
            import shutil
            shutil.rmtree(output_dir, ignore_errors=True)
            removed = True
        except Exception as _e:
            remove_err = str(_e)

    # Delete the DB row
    with pg_raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {DB_SCHEMA}.codewiki_doc_jobs WHERE id = %s", (existing["id"],))
            conn.commit()

    return {
        "deleted": True,
        "codebase_name": name,
        "cancel_ok": cancel_ok,
        "cancel_err": cancel_err,
        "removed_output_dir": removed,
        "remove_err": remove_err,
    }
