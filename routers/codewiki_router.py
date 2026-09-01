# SPDX-License-Identifier: Apache-2.0
"""
CodeWiki documentation router.

Provides:
- POST /generate       — submit a new codebase for documentation.
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
from typing import List

from fastapi import APIRouter, Depends, HTTPException

from auth.dependencies import get_current_user
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

    @field_validator("codebase_name", "repo_url", "branch")
    @classmethod
    def strip_strings(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


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
    return {
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
    }


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
                       error_message, output_dir, pages, created_at, updated_at
                  FROM {DB_SCHEMA}.codewiki_doc_jobs
                 WHERE codebase_name = %s
                """,
                (codebase_name,),
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


def _create_job(codebase_name: str, repo_url: str, branch: str) -> dict:
    """Insert a new job row and return the full record."""
    with pg_raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {DB_SCHEMA}.codewiki_doc_jobs
                    (id, codebase_name, repo_url, branch, status, pages)
                VALUES (%s, %s, %s, %s, 'pending', '[]'::jsonb)
                RETURNING id, codebase_name, repo_url, branch, status,
                          error_message, output_dir, pages, created_at, updated_at
                """,
                (str(uuid.uuid4()), codebase_name, repo_url, branch),
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
    return status in ("pending", "running")


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
def generate_docs(payload: GenerateDocsRequest, _user: dict = Depends(get_current_user)) -> JobResponse:
    """Create a new CodeWiki documentation job and enqueue it."""
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

    job = _create_job(payload.codebase_name, payload.repo_url, payload.branch)
    enqueue_codewiki_job(
        job_id=job["id"],
        codebase_name=payload.codebase_name,
        repo_url=payload.repo_url,
        branch=payload.branch,
    )
    return JobResponse(**job)


@router.post("/regenerate")
def regenerate_docs(payload: RegenerateDocsRequest, _user: dict = Depends(get_current_user)) -> dict:
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

    if _is_running_or_pending(existing["status"]):
        raise HTTPException(
            status_code=409,
            detail=(
                f"A job for '{payload.codebase_name}' is already "
                f"{existing['status']}. Please wait for it to finish."
            ),
        )

    # Compute dry-run: find changed files between last_commit_sha and HEAD of branch
    # If last_commit_sha is missing, we treat as full incremental run (regenerate all existing modules)
    job_record = existing
    last_sha = job_record.get("last_commit_sha")

    # Helper: read the latest docs in disk for the job if available
    output_dir = job_record.get("output_dir") or str(
        Path(_require_codewiki_docs_dir()) / job_record["codebase_name"] / job_record["branch"] / "latest"
    )
    docs_dir = Path(output_dir) / "docs"

    # Enumerate existing module md files under docs_dir
    existing_modules = []
    if docs_dir.exists():
        for p in sorted(docs_dir.iterdir()):
            if p.is_file() and p.suffix == ".md" and p.name.lower() != "overview.md":
                existing_modules.append(p.name)

    # If last_commit_sha is missing, dry-run is full: regenerate all existing modules
    if not last_sha:
        dry_run = {
            "existing_modules_to_regenerate": existing_modules,
            "new_modules_to_create": [],
            "modules_to_remove": [],
            "overview_should_regen": bool(existing_modules),
            "note": "last_commit_sha missing — full incremental run will regenerate all existing module .md files",
        }

        if not payload.confirm:
            return dry_run

        # Confirmed: enqueue normal regenerate job (full incremental)
        _reset_job_for_regen(existing["id"])
        enqueue_codewiki_job(
            job_id=existing["id"],
            codebase_name=existing["codebase_name"],
            repo_url=existing["repo_url"],
            branch=existing["branch"],
            extra_payload={
                "modules": dry_run["existing_modules_to_regenerate"],
                "overview": dry_run["overview_should_regen"],
            },
        )

        refreshed = _get_job_by_codebase(payload.codebase_name)
        return JobResponse(**refreshed)

    # When last_commit_sha exists: clone the repo shallowly and compute changed files
    import tempfile
    import shutil
    from git import Repo as GitRepo

    dry_run = {
        "existing_modules_to_regenerate": [],
        "new_modules_to_create": [],
        "modules_to_remove": [],
        "overview_should_regen": False,
        "note": "",
    }

    temp_dir = None
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="codewiki_diff_"))
        # Try a shallow clone first; if the previous commit is not reachable we will fall back
        try:
            GitRepo.clone_from(job_record["repo_url"], str(temp_dir), branch=job_record["branch"], depth=50, single_branch=True)
            repo = GitRepo(str(temp_dir))
        except Exception:
            # Fallback to a full clone if shallow clone failed to include the old commit
            shutil.rmtree(temp_dir, ignore_errors=True)
            temp_dir = Path(tempfile.mkdtemp(prefix="codewiki_diff_full_"))
            GitRepo.clone_from(job_record["repo_url"], str(temp_dir), branch=job_record["branch"], single_branch=True)
            repo = GitRepo(str(temp_dir))

        latest_sha = repo.head.commit.hexsha
        if latest_sha == last_sha:
            dry_run["note"] = "Already up to date"
            # nothing to do
            if not payload.confirm:
                return dry_run
            refreshed = _get_job_by_codebase(payload.codebase_name)
            return dict(refreshed)

        # Compute changed files between last_sha and latest_sha
        try:
            diff_out = repo.git.diff('--name-only', f"{last_sha}..{latest_sha}")
            changed_files = [l.strip() for l in diff_out.splitlines() if l.strip()]
        except Exception:
            # If git could not compute diff (missing history), fallback to full
            changed_files = []

        # Map changed files to modules using existing module_tree.json if present
        mapped_modules = set()
        unmapped_files = []
        module_tree_path = docs_dir / "module_tree.json"
        module_map = {}
        if module_tree_path.exists():
            try:
                mt = json.loads(module_tree_path.read_text(encoding='utf-8'))
                # mt is dict of module_key -> { components: [...] }
                for mod_key, info in mt.items():
                    module_map[mod_key] = info.get('components', [])
            except Exception:
                module_map = {}

        def _normalize(p: str) -> str:
            return p.replace('\\', '/').lstrip('./')

        for f in changed_files:
            nf = _normalize(f)
            found = False
            for mod_key, components in module_map.items():
                for comp in components:
                    # component entries have form 'path::Symbol' or just 'path'
                    comp_path = comp.split('::', 1)[0]
                    if not comp_path:
                        continue
                    comp_norm = _normalize(comp_path)
                    if nf.endswith(comp_norm) or comp_norm.endswith(nf) or comp_norm == nf:
                        mapped_modules.add(f"{mod_key}.md")
                        found = True
                        break
                if found:
                    break
            if not found:
                unmapped_files.append(f)

        existing_set = set(existing_modules)
        existing_to_regen = sorted(list(mapped_modules & existing_set))
        new_to_create = sorted(list(mapped_modules - existing_set))

        dry_run["existing_modules_to_regenerate"] = existing_to_regen
        dry_run["new_modules_to_create"] = new_to_create
        dry_run["modules_to_remove"] = []
        dry_run["overview_should_regen"] = bool(existing_to_regen or new_to_create)
        dry_run["note"] = f"Latest commit {latest_sha}; changed_files={len(changed_files)}; unmapped_files={len(unmapped_files)}"
        if unmapped_files:
            dry_run['unmapped_files'] = unmapped_files

        if not payload.confirm:
            return dry_run

        # If confirmed, enqueue job with list of modules to regenerate/create and overview flag
        _reset_job_for_regen(existing["id"])
        enqueue_codewiki_job(
            job_id=existing["id"],
            codebase_name=existing["codebase_name"],
            repo_url=existing["repo_url"],
            branch=existing["branch"],
            extra_payload={
                "modules": dry_run["existing_modules_to_regenerate"] + dry_run["new_modules_to_create"],
                "overview": dry_run["overview_should_regen"],
                "target_sha": latest_sha,
            },
        )

        refreshed = _get_job_by_codebase(payload.codebase_name)
        return dict(refreshed)

    except Exception as exc:
        # Defense-in-depth credential redaction on this diff-clone's own
        # exception text -- see workers/codewiki_worker.py's identical
        # comment on its own except-block for the full rationale (GitPython
        # already redacts credentials in GitCommandError as of 3.1.40, this
        # is a second, library-independent layer).
        safe_exc_text = mask_text(str(exc))
        # Return the error in a JSON-digestible form for the dry-run
        dry_run["note"] = f"Error computing diff: {safe_exc_text}"
        if not payload.confirm:
            return dry_run
        # If the user confirmed but an error occurred, raise HTTPException so the UI sees a 500
        raise HTTPException(status_code=500, detail=safe_exc_text)

    finally:
        if temp_dir and temp_dir.exists():
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass


@router.post("/retry")
def retry_docs(payload: RegenerateDocsRequest, _user: dict = Depends(get_current_user)) -> JobResponse:
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
    )

    refreshed = _get_job_by_codebase(payload.codebase_name)
    return JobResponse(**refreshed)


@router.get("/codebases")
def list_codebases(_user: dict = Depends(get_current_user)) -> List[JobResponse]:
    """List every codebase (only one row per codebase_name by schema design)."""
    with pg_raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, codebase_name, repo_url, branch, status,
                       error_message, output_dir, pages, created_at, updated_at
                  FROM {DB_SCHEMA}.codewiki_doc_jobs
                 ORDER BY codebase_name ASC
                """
            )
            rows = cur.fetchall()
    return [JobResponse(**_row_to_dict(row)) for row in rows]


@router.get("/codebases/{codebase_name}")
def get_codebase(codebase_name: str, _user: dict = Depends(get_current_user)) -> JobResponse:
    """Return status and page list for a single codebase."""
    job = _get_job_by_codebase(codebase_name)
    if job is None:
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
def delete_codebase(payload: RegenerateDocsRequest, _user: dict = Depends(get_current_user)) -> dict:
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
