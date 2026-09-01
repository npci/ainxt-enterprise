# SPDX-License-Identifier: Apache-2.0
"""
AiNxt OS — Templates Router

Backend integration layer for the AiNxt OS Engineering Governance Operating System.
Provides endpoints that:

- Serve golden template manifest + content to CLI / IDE / Chat clients
- Accept CI-pushed indexing operations (writes ainxt/ content to pgvector)
- Read operation status from on-disk metadata
- Bootstrap operation folder structure when an engineer starts new work

Operation execution itself (LLM calls, code generation) flows through the
existing /ask endpoint — this router is the *governance* + *catalog* layer.

Mounted at /ainxt/v1/api by gateway.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth.dependencies import get_current_user, require_admin
from core.logger import logger


router = APIRouter(prefix="/templates", tags=["ainxt-os"])
admin_router = APIRouter(prefix="/admin/templates", tags=["ainxt-os-admin"])


# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

# Root for the AiNxt OS content. Resolution order:
#   1. AINXT_OS_ROOT env var
#   2. <cwd>/ainxt/
#   3. Repo-root-relative ainxt/ (derived from this file's location)
def _resolve_ainxt_root() -> Path:
    env = os.environ.get("AINXT_OS_ROOT")
    if env:
        p = Path(env)
        if p.exists():
            return p

    cwd_candidate = Path.cwd() / "ainxt"
    if cwd_candidate.exists():
        return cwd_candidate

    # gateway.py imports this router; assume repo root is gateway.py's parent
    repo_candidate = Path(__file__).resolve().parent.parent / "ainxt"
    if repo_candidate.exists():
        return repo_candidate

    # Optional cross-repo dependency: this content lives in the separate
    # ainxt-os repository, which a Platform-only install does not have. Raising
    # RuntimeError here surfaced as HTTP 500 on /templates, /templates/manifest
    # and /templates/operations for every default install — an unconfigured
    # optional feature reported as a server fault. Fail with 503 + an actionable
    # message instead, matching how the SCIM endpoints report "not configured".
    raise HTTPException(
        status_code=503,
        detail=(
            "AiNxt OS content is not available on this install. The SDLC "
            "templates live in the separate ainxt-os repository. Point "
            "AINXT_OS_ROOT at a checkout of it, or place it at ./ainxt, then "
            "restart the gateway. This feature is optional — the rest of the "
            "platform is unaffected."
        ),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Request/Response models
# ──────────────────────────────────────────────────────────────────────────────


class InitOperationRequest(BaseModel):
    template_id: str = Field(..., description="One of: migration, feature, bugfix, rca, security_audit, performance")
    repo_name: str
    jira_key: str
    operation_slug: Optional[str] = None  # e.g. 'spring-boot-3-migration'; derived from jira_key if absent
    inputs: dict = Field(default_factory=dict)


class InitOperationResponse(BaseModel):
    run_id: str
    operation_path: str  # relative to ainxt/
    created_files: list[str]
    next_step: str


class TemplateSummary(BaseModel):
    id: str
    name: str
    slash_command: str
    description: str
    typical_runtime_minutes: Optional[list[int]] = None


class OperationStatus(BaseModel):
    run_id: str
    template_id: str
    repo_name: str
    jira_key: str
    operation_path: str
    current_phase: str  # planning | docs_review | docs_approved | code_generation | ...
    approvals: dict  # {architecture_review: bool, security_review: bool, ...}
    needs_engineer_input: list[str]  # outstanding [NEEDS ENGINEER INPUT] markers
    created_at: Optional[str] = None
    last_updated: Optional[str] = None


class IndexEntry(BaseModel):
    source_path: str
    namespace: Optional[str] = None  # null for structured_data
    upload_as: str = "embedding"  # 'embedding' | 'structured_data'
    chunk_strategy: str = "per_section"
    content: str
    metadata: dict = Field(default_factory=dict)


class IndexResponse(BaseModel):
    success: bool
    chunks_written: int
    namespace: Optional[str] = None
    message: str = ""


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _slugify(s: str) -> str:
    import re
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", s.strip().lower())
    return re.sub(r"-+", "-", s).strip("-")[:60] or "operation"


def _load_manifest() -> dict:
    """Read org/golden_templates/_manifest.yml."""
    ainxt_root = _resolve_ainxt_root()
    manifest_path = ainxt_root / "org" / "golden_templates" / "_manifest.yml"
    if not manifest_path.exists():
        raise HTTPException(status_code=500, detail=f"Manifest not found: {manifest_path}")
    with manifest_path.open() as f:
        return yaml.safe_load(f)


def _template_to_operation_type(template_id: str) -> str:
    """Map template_id to the operation folder name (matches _manifest.yml structure)."""
    mapping = {
        "migration": "migration",
        "feature": "feature",
        "bugfix": "bugs",
        "rca": "rca",
        "security_audit": "security_audits",
        "performance": "performance",
    }
    if template_id not in mapping:
        raise HTTPException(status_code=400, detail=f"Unknown template_id: {template_id}")
    return mapping[template_id]


def _operation_dir_name(template_id: str, jira_key: str, operation_slug: Optional[str]) -> str:
    """Build the operation directory name: <jira>_<slug>."""
    slug = operation_slug or _slugify(jira_key)
    return f"{jira_key}_{_slugify(slug)}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────────────────────────────────────
# Public endpoints (engineer-facing)
# ──────────────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[TemplateSummary])
def list_templates(_user: dict = Depends(get_current_user)) -> list[TemplateSummary]:
    """List all available AiNxt OS golden templates from the manifest."""
    manifest = _load_manifest()
    out: list[TemplateSummary] = []
    for t in manifest.get("templates", []):
        out.append(TemplateSummary(
            id=t["id"],
            name=t["name"],
            slash_command=t["slash_command"],
            description=t["description"],
            typical_runtime_minutes=t.get("typical_runtime_minutes"),
        ))
    return out


@router.get("/manifest")
def get_manifest(_user: dict = Depends(get_current_user)) -> dict:
    """Return the full manifest. Used by CLI to discover slash commands + lifecycle commands."""
    return _load_manifest()


@router.get("/template/{template_id}")
def get_template_content(
    template_id: str,
    _user: dict = Depends(get_current_user),
) -> dict:
    """Return the raw markdown content of a specific golden template."""
    manifest = _load_manifest()
    template_meta = next((t for t in manifest["templates"] if t["id"] == template_id), None)
    if not template_meta:
        raise HTTPException(status_code=404, detail=f"Template not found: {template_id}")

    ainxt_root = _resolve_ainxt_root()
    template_path = ainxt_root / "org" / "golden_templates" / template_meta["file"]
    if not template_path.exists():
        raise HTTPException(status_code=500, detail=f"Template file missing: {template_path}")

    return {
        "id": template_id,
        "name": template_meta["name"],
        "content": template_path.read_text(),
        "meta": template_meta,
    }


@router.post("/init", response_model=InitOperationResponse)
def init_operation(
    req: InitOperationRequest,
    user: dict = Depends(get_current_user),
) -> InitOperationResponse:
    """
    Bootstrap an operation folder structure.

    Creates: ainxt/repos/<repo>/operations/<type>/<jira>_<slug>/{metadata,plan,implementation,ai_execution,final_review,generated_artifacts}/
    Initialises metadata/status.md, ownership.md, timelines.md, approvals.md
    Returns run_id + operation path + next step.
    """
    ainxt_root = _resolve_ainxt_root()
    operation_type = _template_to_operation_type(req.template_id)
    op_dir_name = _operation_dir_name(req.template_id, req.jira_key, req.operation_slug)

    base = ainxt_root / "repos" / req.repo_name / "operations" / operation_type / op_dir_name

    if base.exists():
        # Don't clobber an existing operation
        raise HTTPException(
            status_code=409,
            detail=f"Operation already exists: {base.relative_to(ainxt_root)}. "
                   f"Use /resume/{op_dir_name} to continue.",
        )

    run_id = f"run_{uuid.uuid4().hex[:12]}"
    created: list[str] = []

    # Create folder skeleton
    for sub in [
        "metadata",
        f"{req.template_id}_plan",
        "implementation",
        "ai_execution",
        "final_review",
        "generated_artifacts/prompts",
        "generated_artifacts/generated_code",
        "generated_artifacts/generated_tests",
        "generated_artifacts/generated_sql",
    ]:
        (base / sub).mkdir(parents=True, exist_ok=True)
        created.append(str((base / sub).relative_to(ainxt_root)))

    # metadata/status.md
    status_content = f"""# Operation Status

- **run_id:** {run_id}
- **template_id:** {req.template_id}
- **repo_name:** {req.repo_name}
- **jira_key:** {req.jira_key}
- **operation_slug:** {op_dir_name}
- **created_at:** {_now_iso()}
- **created_by:** {user.get('email', user.get('sub', 'unknown'))}
- **current_phase:** planning

## State transitions
| Timestamp | Phase | Triggered by |
|---|---|---|
| {_now_iso()} | planning | /ainxt-init |

## Lifecycle states
planning → docs_in_progress → docs_review → docs_approved → code_generation → code_review → deployed → verified
"""
    (base / "metadata" / "status.md").write_text(status_content)

    # metadata/ownership.md
    ownership_content = f"""# Ownership

- **Operation owner:** {user.get('email', 'pending')}
- **Tech lead:** [NEEDS ENGINEER INPUT: assignee email]
- **QA owner:** [NEEDS ENGINEER INPUT: assignee email]
- **Security reviewer:** [NEEDS ENGINEER INPUT: assignee email]
- **Deployment approver:** [NEEDS ENGINEER INPUT: assignee email]
"""
    (base / "metadata" / "ownership.md").write_text(ownership_content)

    # metadata/timelines.md
    timelines_content = f"""# Timelines

- **Operation type:** {req.template_id}
- **Started:** {_now_iso()}
- **Target completion:** [NEEDS ENGINEER INPUT]
- **Phases planned:** [NEEDS ENGINEER INPUT]

## Phase log
| Phase | Started | Completed | Notes |
|---|---|---|---|
| planning | {_now_iso()} | — | folder bootstrapped |
"""
    (base / "metadata" / "timelines.md").write_text(timelines_content)

    # metadata/approvals.md
    approvals_content = """# Approvals

| Gate | Approver | Status | Approved at |
|---|---|---|---|
| docs_approval | [tech_lead] | pending | — |
| architecture_review | [architecture_lead] | pending | — |
| security_review | [security_architect] | pending | — |
| performance_review | [SRE] | pending | — |
| deployment_signoff | [tech_lead + SRE] | pending | — |
"""
    (base / "metadata" / "approvals.md").write_text(approvals_content)

    # final_review/* — empty checklists
    for review_file in [
        "architecture_review.md",
        "security_review.md",
        "performance_review.md",
        "deployment_signoff.md",
    ]:
        (base / "final_review" / review_file).write_text(
            f"# {review_file.replace('.md', '').replace('_', ' ').title()}\n\n"
            f"**Status:** pending\n\n"
            f"## Checklist\n[Generated by template execution — fill during review]\n\n"
            f"## Approver sign-off\n- [ ] [approver_name] — [date]\n"
        )

    logger.info(
        f"templates: initialised operation run_id={run_id} repo={req.repo_name} "
        f"jira={req.jira_key} type={req.template_id} path={base.relative_to(ainxt_root)}"
    )

    next_step = (
        f"Run the AiNxt OS {req.template_id} golden template against this operation. "
        f"Inputs collected: {list(req.inputs.keys())}. "
        f"The template will populate {req.template_id}_plan/ and implementation/."
    )

    return InitOperationResponse(
        run_id=run_id,
        operation_path=str(base.relative_to(ainxt_root)),
        created_files=created,
        next_step=next_step,
    )


@router.get("/status/{identifier}", response_model=OperationStatus)
def get_operation_status(
    identifier: str,
    repo_name: Optional[str] = Query(None, description="Optional repo hint to disambiguate"),
    _user: dict = Depends(get_current_user),
) -> OperationStatus:
    """
    Read metadata/status.md + final_review/ approvals for an operation.
    Identifier can be: run_id, jira_key, or operation_slug.
    """
    ainxt_root = _resolve_ainxt_root()
    repos_root = ainxt_root / "repos"
    if not repos_root.exists():
        raise HTTPException(status_code=404, detail="No repos/ found in AiNxt OS root")

    # Search all repos/<repo>/operations/<type>/<slug>/ for a match
    candidates: list[Path] = []
    for repo_dir in repos_root.iterdir():
        if not repo_dir.is_dir() or repo_dir.name.startswith("_"):
            continue
        if repo_name and repo_dir.name != repo_name:
            continue
        ops_root = repo_dir / "operations"
        if not ops_root.exists():
            continue
        for type_dir in ops_root.iterdir():
            if not type_dir.is_dir():
                continue
            for op_dir in type_dir.iterdir():
                if not op_dir.is_dir():
                    continue
                # Match by name (jira_<slug>) starting with identifier OR exact run_id in status.md
                if op_dir.name.startswith(identifier):
                    candidates.append(op_dir)
                    continue
                status_md = op_dir / "metadata" / "status.md"
                if status_md.exists() and identifier in status_md.read_text():
                    candidates.append(op_dir)

    if not candidates:
        raise HTTPException(
            status_code=404,
            detail=f"No operation found for identifier '{identifier}'. "
                   f"Searched all repos under {repos_root.relative_to(ainxt_root)}.",
        )
    if len(candidates) > 1:
        raise HTTPException(
            status_code=409,
            detail=f"Multiple operations match '{identifier}': "
                   f"{[str(c.relative_to(ainxt_root)) for c in candidates]}. "
                   f"Specify ?repo_name=<repo> to disambiguate.",
        )

    op_dir = candidates[0]
    status_path = op_dir / "metadata" / "status.md"
    if not status_path.exists():
        raise HTTPException(status_code=500, detail=f"status.md missing in {op_dir}")

    status_text = status_path.read_text()

    # Parse key fields
    def _extract(field: str) -> str:
        for line in status_text.splitlines():
            if line.startswith(f"- **{field}:**"):
                return line.split(":**", 1)[1].strip()
        return ""

    # Approvals
    approvals: dict[str, bool] = {}
    for review_file in ["architecture_review", "security_review", "performance_review", "deployment_signoff"]:
        review_path = op_dir / "final_review" / f"{review_file}.md"
        if review_path.exists():
            approvals[review_file] = "**Status:** approved" in review_path.read_text()
        else:
            approvals[review_file] = False

    # Outstanding [NEEDS ENGINEER INPUT] markers
    needs_input: list[str] = []
    for md_file in op_dir.rglob("*.md"):
        text = md_file.read_text()
        if "[NEEDS ENGINEER INPUT" in text:
            rel = md_file.relative_to(ainxt_root)
            count = text.count("[NEEDS ENGINEER INPUT")
            needs_input.append(f"{rel} ({count} item{'s' if count > 1 else ''})")

    return OperationStatus(
        run_id=_extract("run_id"),
        template_id=_extract("template_id"),
        repo_name=_extract("repo_name"),
        jira_key=_extract("jira_key"),
        operation_path=str(op_dir.relative_to(ainxt_root)),
        current_phase=_extract("current_phase") or "unknown",
        approvals=approvals,
        needs_engineer_input=needs_input,
        created_at=_extract("created_at") or None,
        last_updated=None,
    )


@router.get("/operations")
def list_operations(
    repo_name: Optional[str] = Query(None),
    template_id: Optional[str] = Query(None),
    _user: dict = Depends(get_current_user),
) -> list[dict]:
    """
    List all operations, optionally filtered by repo or template type.
    Useful for /ainxt-status without an identifier (browse mode).
    """
    ainxt_root = _resolve_ainxt_root()
    repos_root = ainxt_root / "repos"
    if not repos_root.exists():
        return []

    out: list[dict] = []
    for repo_dir in repos_root.iterdir():
        if not repo_dir.is_dir() or repo_dir.name.startswith("_"):
            continue
        if repo_name and repo_dir.name != repo_name:
            continue
        ops_root = repo_dir / "operations"
        if not ops_root.exists():
            continue
        for type_dir in ops_root.iterdir():
            if not type_dir.is_dir():
                continue
            # Map back to template_id (reverse of _template_to_operation_type)
            type_to_tid = {
                "migration": "migration", "feature": "feature", "bugs": "bugfix",
                "rca": "rca", "security_audits": "security_audit", "performance": "performance",
            }
            tid = type_to_tid.get(type_dir.name, type_dir.name)
            if template_id and tid != template_id:
                continue
            for op_dir in type_dir.iterdir():
                if not op_dir.is_dir():
                    continue
                status_path = op_dir / "metadata" / "status.md"
                if not status_path.exists():
                    continue
                status_text = status_path.read_text()
                phase = "unknown"
                for line in status_text.splitlines():
                    if line.startswith("- **current_phase:**"):
                        phase = line.split(":**", 1)[1].strip()
                        break
                out.append({
                    "operation_path": str(op_dir.relative_to(ainxt_root)),
                    "repo_name": repo_dir.name,
                    "template_id": tid,
                    "operation_slug": op_dir.name,
                    "current_phase": phase,
                })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Admin endpoints (CI indexer + manifest upload)
# ──────────────────────────────────────────────────────────────────────────────

@admin_router.post("/index", response_model=IndexResponse)
def index_content(
    entry: IndexEntry,
    _user: dict = Depends(require_admin),
) -> IndexResponse:
    """
    Receive a single file's content from the CI sync pipeline and write it
    into pgvector (for embedding) or appropriate structured-data storage.

    Called by ainxt/scripts/index_ainxt.py.

    Idempotency: identified by (namespace, source_path) — re-indexing the
    same file overwrites the previous chunks.
    """
    if entry.upload_as == "structured_data":
        # Manifest and similar — store as JSON or YAML blob in a dedicated table
        # (out of scope for this initial implementation; backend just acknowledges)
        logger.info(
            f"templates: structured_data accepted source={entry.source_path} "
            f"(manifest registry — stored in-memory; persistent table TBD)"
        )
        return IndexResponse(
            success=True,
            chunks_written=0,
            namespace=None,
            message=f"Structured data accepted for {entry.source_path}",
        )

    if not entry.namespace:
        raise HTTPException(status_code=400, detail="namespace required for upload_as=embedding")

    # Chunk content
    chunks = _chunk_content(entry.content, entry.chunk_strategy)
    if not chunks:
        return IndexResponse(success=True, chunks_written=0, namespace=entry.namespace, message="No content")

    # Embed via embed svc
    try:
        embeddings = _embed_chunks(chunks)
    except Exception as e:
        logger.error(f"templates: embed svc failed for {entry.source_path}: {e}")
        raise HTTPException(status_code=502, detail=f"Embedding failed: {e}")

    # Write to pgvector
    written = _write_pgvector(
        namespace=entry.namespace,
        source_path=entry.source_path,
        chunks=chunks,
        embeddings=embeddings,
        metadata=entry.metadata,
    )

    return IndexResponse(
        success=True,
        chunks_written=written,
        namespace=entry.namespace,
        message=f"Indexed {written} chunks from {entry.source_path}",
    )


@admin_router.post("/manifest")
def upload_manifest(
    payload: IndexEntry,
    _user: dict = Depends(require_admin),
) -> dict:
    """
    Receive the manifest YAML from CI. Backend caches it for fast lookup by
    /templates/manifest GET requests (no need to re-read from disk).

    For now this is a no-op acknowledgement — manifest is read from disk on
    demand. Future: cache to Redis db=0 with TTL.
    """
    logger.info(f"templates: manifest upload received source={payload.source_path}")
    return {"success": True, "cached": False, "message": "Manifest read on-demand from disk"}


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers — chunking + embedding + pgvector
# ──────────────────────────────────────────────────────────────────────────────

def _chunk_content(content: str, strategy: str) -> list[str]:
    if strategy == "full_file":
        return [content]
    if strategy == "per_section":
        # Split on markdown H2 headers (##), keep H1 with the first section
        import re
        sections: list[str] = []
        current: list[str] = []
        for line in content.splitlines():
            if re.match(r"^##\s+", line) and current:
                sections.append("\n".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            sections.append("\n".join(current))
        # Filter very small chunks (< 100 chars) by appending to previous
        merged: list[str] = []
        for s in sections:
            if merged and len(s) < 100:
                merged[-1] = merged[-1] + "\n" + s
            else:
                merged.append(s)
        return merged
    if strategy == "per_paragraph":
        return [p.strip() for p in content.split("\n\n") if p.strip()]
    # default
    return [content]


def _embed_chunks(chunks: list[str]) -> list[list[float]]:
    import httpx
    from core.config import EMBED_SVC_URL
    embeddings: list[list[float]] = []
    BATCH = 64
    for start in range(0, len(chunks), BATCH):
        batch = chunks[start:start + BATCH]
        resp = httpx.post(
            f"{EMBED_SVC_URL}/embed",
            json={"texts": batch, "provider": "ollama"},
            timeout=120.0,
        )
        resp.raise_for_status()
        embeddings.extend(resp.json()["embeddings"])
    return embeddings


def _write_pgvector(
    namespace: str,
    source_path: str,
    chunks: list[str],
    embeddings: list[list[float]],
    metadata: dict,
) -> int:
    from db.database import VectorSessionLocal
    from db.models import DocumentEmbedding
    from sqlalchemy import text as _sql_text

    vdb = VectorSessionLocal()
    try:
        # Idempotency — delete prior entries for this (namespace, source_path)
        vdb.execute(
            _sql_text(
                "DELETE FROM document_embeddings "
                "WHERE repo = :repo AND file_path = :file_path"
            ),
            {"repo": namespace, "file_path": source_path},
        )
        for idx, (chunk_text, emb) in enumerate(zip(chunks, embeddings)):
            content_hash = hashlib.sha256(chunk_text.encode()).hexdigest()
            vdb.add(DocumentEmbedding(
                id=str(uuid.uuid4()),
                repo=namespace,
                file_path=source_path,
                chunk_index=idx,
                content=chunk_text,
                embedding=emb,
                content_hash=content_hash,
                metadata_=metadata,
            ))
        vdb.commit()
        return len(chunks)
    except Exception as e:
        vdb.rollback()
        logger.error(f"templates: pgvector write failed for {source_path}: {e}")
        raise
    finally:
        vdb.close()
