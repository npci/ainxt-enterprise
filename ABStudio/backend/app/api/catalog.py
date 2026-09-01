# SPDX-License-Identifier: Apache-2.0
"""Tool catalog, skill catalog, and agent registry endpoints."""
import io
import os
import re
import zipfile
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel
from core.logger import logger
from app.models import AuthenticatedUser
from app.core import workflow_repo
from app.api.deps import require_access, require_admin
from agent_factory.pipeline import (
    AgentRegistry, MonitoringLogger,
    AGENTS_FILE, LOGS_FILE,
    DynamicToolGenerator, DynamicSkillGenerator,
)
from skill_factory.pipeline import (
    catalog_cache, _validate_skill_md, _safe_rel_path, parse_frontmatter,
)

router = APIRouter()

_registry = AgentRegistry(str(AGENTS_FILE))
_monitor = MonitoringLogger(str(LOGS_FILE))


def _slim_tool_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": row.get("name"),
        "description": row.get("description", ""),
        "input_schema": row.get("input_schema") or {},
        "generated": bool(row.get("generated")),
        "service": row.get("service", ""),
        "code": row.get("code", ""),
    }


USABLE_STATUSES = {"APPROVED", "PRODUCTION", "ACTIVE"}


def _slim_skill_row(row: Dict[str, Any], meta: Optional[Dict[str, Any]] = None,
                    *, viewer_id: str = "") -> Dict[str, Any]:
    """Merge an ABStudio ``skills_catalog`` row with its governance mirror meta.

    ``meta`` is the dict returned by ``bulk_governance_meta`` for this row's
    name; ``None`` for built-in / seeded skills that don't need governance.
    ``viewer_id`` decides ``is_usable`` from the viewer's perspective (owners
    can always attach their own approved-once-more-later skill; runtime still
    enforces).
    """
    m = meta or {}
    status = m.get("status")
    is_generated = bool(row.get("generated"))
    # Non-generated (seeded / built-in) skills are always usable — they ship
    # with the platform and don't take a governance mirror row.
    if not is_generated:
        usable = True
    else:
        usable = status in USABLE_STATUSES
    # Origin of the row for the Skills-tab source filter/badge. The DB column
    # ``source`` supersedes the older ``generated`` flag (which only
    # distinguished ai/builtin) — we still fall back to inferring from
    # ``generated`` when a legacy row hasn't been backfilled yet, so no
    # frontend regression on old data.
    src = row.get("source")
    if not src:
        src = "ai" if is_generated else "builtin"
    return {
        "name":              row.get("name"),
        "description":       row.get("description", ""),
        "category":          row.get("category", "general"),
        "generated":         is_generated,
        "source":            src,
        "created_at":        row.get("created_at"),
        "updated_at":        row.get("updated_at"),
        # Governance fields (None on built-ins or when no mirror row exists)
        "status":            status,
        "visibility":        m.get("visibility"),
        "department":        m.get("department"),
        "created_by":        m.get("created_by"),
        "created_by_email":  m.get("created_by_email"),
        "created_by_name":   m.get("created_by_name"),
        "approved_by":       m.get("approved_by"),
        "approved_by_email": m.get("approved_by_email"),
        "approved_by_name":  m.get("approved_by_name"),
        "approved_at":       m.get("approved_at"),
        "is_usable":         usable,
        "is_owner":          bool(viewer_id) and str(m.get("created_by") or "") == str(viewer_id),
    }


# ARCH-F-014 (2026-08-26): department isolation for web_search.
#
# Analysis: mcp/tool_registry.py's ToolRegistry (the platform-wide tool
# store used by Chat/Buddy/CLI) has NO department/tenant scoping concept at
# all — ToolDefinition carries no owning-department field, and register()/
# discover()/execute() are global. That "no isolation" finding is accurate
# for the platform registry, but implementing per-department isolation
# there would be a much larger change (every registered tool would need an
# owning-department, and every execute() caller would need to pass the
# caller's department) that's out of scope for a single web_search fix and
# would affect tools with no restriction requirement at all.
#
# web_search itself is a NARROWER, already-isolated case that doesn't need
# that broader change:
#   - The Chat/Buddy/CLI web_search path never touches mcp/tool_registry.py.
#     It runs through services/llm_proxy/main.py's native web_search
#     endpoint, gated by routers/model_governance_router.is_web_search_allowed()
#     which is ALREADY department-aware via dept_model_permissions /
#     user_model_permissions.web_search_allowed (opt-in per department/user,
#     defaults to deny). Nothing to change there.
#   - The ABStudio Agent Studio web_search tool (app/tools/platform_tools.py)
#     was NEVER seeded — it carries "draft": True, so seed_canonical_tools()
#     skips it and it has been unreachable by any agent to date.
# The isolation gap is specifically: IF web_search is un-drafted so ABStudio
# agents can use it, it must not become visible/usable to every department by
# default. This endpoint is where every other per-tool visibility rule for
# this catalog already lives (see the M365-connection check below), so the
# department gate for web_search is added here, in the same place, rather
# than inventing a new department-aware layer inside ToolRegistry.
_WEB_SEARCH_TOOL_NAME = "web_search"
_WEB_SEARCH_ALLOWED_DEPARTMENTS = frozenset(
    d.strip().casefold() for d in
    os.getenv("ABSTUDIO_WEB_SEARCH_DEPARTMENTS", "marketing").split(",")
    if d.strip()
)


def _web_search_visible_to(current_user: AuthenticatedUser) -> bool:
    """web_search is isolated to the Marketing department (configurable via
    ABSTUDIO_WEB_SEARCH_DEPARTMENTS) plus admins and the security team, who
    already bypass tool restrictions elsewhere in this module."""
    role = (current_user.role or "").lower()
    if role == "admin" or bool(getattr(current_user, "is_security_team", False)):
        return True
    viewer_dept = (current_user.department or "").strip().casefold()
    return viewer_dept in _WEB_SEARCH_ALLOWED_DEPARTMENTS


@router.get("/tools-catalog")
async def list_tools_catalog(
    current_user: AuthenticatedUser = Depends(require_access),
    include_generated: bool = True,
    include_platform: bool = False,
):
    rows = await workflow_repo.list_tools()

    # Microsoft 365 tools are only usable once the user has an active M365 OAuth
    # connection (each tool runs against the user's own token via the connector
    # bridge). Hide them from the catalog/picker unless connected — checked once
    # per request via the platform bridge. Fail-safe: any error → hidden.
    from app.core.m365_connection import is_m365_connected
    m365_ok = await is_m365_connected(current_user.id)

    web_search_ok = _web_search_visible_to(current_user)

    def _keep(r: Dict[str, Any]) -> bool:
        if r.get("generated") and not include_generated:
            return False
        service = r.get("service")
        if service == "platform" and not include_platform:
            return False
        if service == "microsoft_365" and not m365_ok:
            return False
        if r.get("name") == _WEB_SEARCH_TOOL_NAME and not web_search_ok:
            return False
        return True
    return {"tools": [_slim_tool_row(r) for r in rows if _keep(r)]}


@router.delete("/tools-catalog/{name}", status_code=204)
async def delete_tool_route(name: str, current_user: AuthenticatedUser = Depends(require_admin)):
    # Admin-gated (security review F-05): this row is shared across every
    # tenant user, not owned by the caller — a non-admin deleting it would
    # be an unscoped, tenant-wide destructive action.
    deleted = await workflow_repo.delete_tool(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Tool '{name}' not found")
    logger.info(f'[AGENT] Catalog tool deleted by admin={current_user.id}: {name}')
    return Response(status_code=204)


@router.delete("/tools-catalog", status_code=200)
async def clear_all_tools_route(current_user: AuthenticatedUser = Depends(require_admin)):
    # Admin-gated (security review F-05): wipes the ENTIRE shared tools
    # catalog for every user — never appropriate for a non-admin caller.
    count = await workflow_repo.clear_all_tools()
    logger.info(f'[AGENT] Catalog tools cleared by admin={current_user.id}: {count} rows')
    return {"deleted": count}


def _skill_visible_to(meta: Dict[str, Any], current_user: AuthenticatedUser) -> bool:
    """Access rule for governed (AI-generated / uploaded) skills.

    - Owner: always sees their own skill.
    - Otherwise, the skill must be in a usable status; and:
        * public  -> visible to everyone
        * private -> visible only to same-department users, plus the HOD who
                     approves that department, plus admins.
    - Pending/rejected/deprecated skills are hidden from non-owners, except:
        * pending PUBLIC   -> also visible to admins (they are the approver)
        * pending PRIVATE  -> also visible to the HOD of the creator's dept
    """
    status     = meta.get("status")
    visibility = (meta.get("visibility") or "private").lower()
    cr_dept    = (meta.get("department") or "").strip()
    role       = (current_user.role or "").lower()
    is_admin   = role == "admin"
    is_hod     = bool(getattr(current_user, "is_hod", False))
    # HOD of the creator's department? Reuse the same signal the platform
    # governance router uses so the two stay consistent.
    caller_hod_of_dept = False
    if is_hod and cr_dept:
        try:
            from auth.rbac import get_hod_departments
            caller_hod_of_dept = cr_dept.casefold() in {
                d.casefold() for d in get_hod_departments({
                    "is_hod": True,
                    "hod_departments": getattr(current_user, "hod_departments", None) or [],
                    "department": current_user.department or "",
                })
            }
        except Exception:
            logger.warning("catalog: HOD RBAC lookup failed; defaulting caller_hod_of_dept=False", exc_info=True)
            caller_hod_of_dept = False

    # Usable statuses — cataloging rules by visibility.
    if status in USABLE_STATUSES:
        if visibility == "public":
            return True
        # private -> same dept OR admin OR the mapped HOD
        viewer_dept = (current_user.department or "").strip().casefold()
        if is_admin or caller_hod_of_dept:
            return True
        return bool(cr_dept) and viewer_dept == cr_dept.casefold()

    # Pending / draft / rejected — the approver may see it so they can act.
    if status in ("PENDING_APPROVAL", "PENDING_L2"):
        if visibility == "public" and is_admin:
            return True
        if visibility == "private" and (caller_hod_of_dept or is_admin):
            return True
    # Everything else (draft/rejected/deprecated + no-mirror): owner-only,
    # handled by the caller.
    return False


@router.get("/skills-catalog")
async def list_skills_catalog(current_user: AuthenticatedUser = Depends(require_access)):
    rows = await workflow_repo.list_skills()
    from app.core import governance_client as _gc
    # Bulk-fetch governance meta for every generated skill in one query.
    names = [r["name"] for r in rows if r.get("generated")]
    meta_by_name = _gc.bulk_governance_meta("skills", names)

    viewer_id = str(current_user.id or "")
    out = []
    for r in rows:
        is_generated = bool(r.get("generated"))
        m = meta_by_name.get(r["name"]) if is_generated else None
        if is_generated:
            # No mirror row (legacy or never-submitted) -> owner-only view is
            # impossible to determine here (no created_by), so we hide from
            # everyone except when the skill has been marked usable already.
            # In practice every submit path writes a mirror row, so this is
            # the fail-closed branch.
            if not m:
                continue
            is_owner = str(m.get("created_by") or "") == viewer_id and viewer_id != ""
            if not is_owner and not _skill_visible_to(m, current_user):
                continue
        out.append(_slim_skill_row(r, m, viewer_id=viewer_id))
    return {"skills": out}


@router.get("/skills-catalog/{name}")
async def get_skill_detail(name: str, current_user: AuthenticatedUser = Depends(require_access)):
    row = await workflow_repo.get_skill(name)
    if not row:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    from app.core import governance_client as _gc
    meta = None
    viewer_id = str(current_user.id or "")
    if row.get("generated"):
        meta = _gc.bulk_governance_meta("skills", [name]).get(name)
        # Fail-closed detail view: mirror the list-endpoint gate so a
        # non-owner can't fetch a pending/rejected skill by guessing the name.
        if not meta:
            raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
        is_owner = str(meta.get("created_by") or "") == viewer_id and viewer_id != ""
        if not is_owner and not _skill_visible_to(meta, current_user):
            raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    slim = _slim_skill_row(row, meta, viewer_id=viewer_id)
    slim["content"] = row.get("content", "")
    return slim


class _SkillUpsertReq(BaseModel):
    name: str
    content: str
    description: str = ""
    category: str = "general"


@router.post("/skills-catalog", status_code=201)
async def upsert_catalog_skill(request: _SkillUpsertReq, current_user: AuthenticatedUser = Depends(require_access)):
    name = (request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Skill name is required.")

    from app.core import governance_client as _gc
    existing = await workflow_repo.get_skill(name)

    # Owner-gate edits while the skill is under review: while a governed skill
    # is DRAFT / PENDING_APPROVAL / REJECTED, only its creator may modify it.
    # Once APPROVED/PRODUCTION editing must re-submit (that flow handles the
    # demote via reconcile_after_update below), but we still block strangers.
    if existing and existing.get("generated"):
        meta = _gc.bulk_governance_meta("skills", [name]).get(name) or {}
        owner_id = str(meta.get("created_by") or "")
        viewer_id = str(current_user.id or "")
        if owner_id and viewer_id and owner_id != viewer_id and (current_user.role or "").lower() != "admin":
            raise HTTPException(status_code=403,
                                detail="Only the creator can edit this skill.")

    # Preserve the origin flag on update — an AI/uploaded skill must not be
    # silently reclassified as built-in when its creator saves an edit. New
    # rows default to False (this endpoint is not used by the factory/upload
    # paths, which set generated=True explicitly).
    generated = bool(existing.get("generated")) if existing else False

    row = await workflow_repo.upsert_skill(
        name=name,
        content=request.content,
        description=request.description.strip(),
        category=request.category.strip() or "general",
        generated=generated,
    )

    # Reconcile the governance mirror hash. If the skill was APPROVED, changing
    # its content demotes it back to DRAFT (creator must re-submit). If it's
    # still pending, this only refreshes the hash so the approver's view
    # reflects the latest content. Fire-and-forget so a mirror hiccup can't
    # fail the save.
    try:
        _gc.reconcile_after_update(
            "skills", name, request.content,
            owner_id=str(current_user.id or ""),
        )
    except Exception:
        logger.debug(f"[AGENT] reconcile_after_update failed for skills/{name}", exc_info=True)

    catalog_cache.invalidate()
    # Return the same enriched shape the list endpoint uses so the UI can
    # render Scope / Created by / status immediately after an edit save
    # (built-in edits get meta=None, matching list behaviour).
    meta = _gc.bulk_governance_meta("skills", [name]).get(name) if row.get("generated") else None
    return _slim_skill_row(row, meta, viewer_id=str(current_user.id or ""))


@router.delete("/skills-catalog/{name}", status_code=204)
async def delete_catalog_skill(name: str, current_user: AuthenticatedUser = Depends(require_access)):
    """Delete a skill from the catalog.

    Authorization: creator (owner of the mirror row) OR admin. Any other
    user attempting to delete gets 403 — stops a stranger from removing
    somebody else's work. Built-in / non-governed rows (no mirror) can only
    be deleted by an admin.
    """
    existing = await workflow_repo.get_skill(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found in catalog.")

    role = (current_user.role or "").lower()
    is_admin = role == "admin"
    if not is_admin:
        # Non-admin: must be the creator of the governance mirror row.
        from app.core import governance_client as _gc
        meta = _gc.bulk_governance_meta("skills", [name]).get(name) or {}
        owner_id = str(meta.get("created_by") or "")
        viewer_id = str(current_user.id or "")
        if not owner_id or owner_id != viewer_id:
            raise HTTPException(status_code=403,
                                detail="Only the creator or an admin can delete this skill.")
    deleted = await workflow_repo.delete_skill(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found in catalog.")
    logger.info(f'[AGENT] Catalog skill deleted by admin={current_user.id}: {name}')


class _CatalogGenerateReq(BaseModel):
    name: str
    description: str = ""


@router.post("/tools-catalog/generate", status_code=201)
async def generate_catalog_tool(request: _CatalogGenerateReq, current_user: AuthenticatedUser = Depends(require_access)):
    name = (request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Tool name is required.")
    description = (request.description or "").strip() or f"Tool that handles {name} operations"
    generator = DynamicToolGenerator()
    result = await generator.generate(name, description)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    row = await workflow_repo.get_tool(name)
    if not row:
        raise HTTPException(status_code=500, detail="Tool generated but missing from catalog after upsert.")
    return _slim_tool_row(row)


@router.post("/skills-catalog/generate", status_code=201)
async def generate_catalog_skill(request: _CatalogGenerateReq, current_user: AuthenticatedUser = Depends(require_access)):
    name = (request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Skill name is required.")
    description = (request.description or "").strip() or f"Skill that provides {name} capability"
    generator = DynamicSkillGenerator()
    result = await generator.generate(name, description)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    row = await workflow_repo.get_skill(result["name"])
    if not row:
        raise HTTPException(status_code=500, detail="Skill generated but missing from catalog after upsert.")
    # Include governance meta so the Skills card shows Scope / Created by /
    # status immediately, without waiting for the user to open the detail modal.
    from app.core import governance_client as _gc
    meta = _gc.bulk_governance_meta("skills", [row["name"]]).get(row["name"]) if row.get("generated") else None
    return _slim_skill_row(row, meta, viewer_id=str(current_user.id or ""))


# ---------------------------------------------------------------------------
# Skill upload — import a packaged .zip / .skill bundle into the catalog
# ---------------------------------------------------------------------------

# A skill bundle is a zip: SKILL.md at the root (or inside a single top-level
# folder) plus optional scripts/ and references/ files. We reuse the same
# constraints the AI-generation path enforces so an uploaded skill can't ship
# larger/less-safe files than a generated one.
_UPLOAD_MAX_SIZE_BYTES = 5 * 1024 * 1024        # 5 MB compressed zip cap
_UPLOAD_MAX_BUNDLE_FILES = 8
_UPLOAD_MAX_BUNDLE_FILE_BYTES = 64 * 1024       # 64 KB per bundled file
# Zip-bomb guard: a small compressed archive can inflate to gigabytes. Cap the
# SKILL.md and the sum of all decompressed entries we read into memory.
_UPLOAD_MAX_SKILL_MD_BYTES = 256 * 1024         # 256 KB SKILL.md
_UPLOAD_MAX_TOTAL_UNCOMPRESSED_BYTES = 8 * 1024 * 1024   # 8 MB total inflated


@router.post("/skills-catalog/upload", status_code=201)
async def upload_catalog_skill(
    file: UploadFile = File(...),
    visibility: str = Form("private"),
    category: str = Form(""),
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Import a packaged skill (.zip / .skill) into the common catalog.

    Validates the SKILL.md against the skill spec, safely extracts any
    bundled scripts/references, persists everything to skills_catalog +
    skill_files, and submits the skill for governance with the chosen
    visibility (public / private).
    """
    from core.file_validator import validate_upload
    from app.core import governance_client as _gc

    data = await file.read()
    original_name = file.filename or "skill.zip"

    # Both .zip and .skill are ZIP archives (PK magic). The validator doesn't
    # know the .skill extension, so validate under .zip semantics — the magic
    # byte check still runs and rejects anything that isn't a real archive.
    validate_name = original_name
    if validate_name.lower().endswith(".skill"):
        validate_name = validate_name[: -len(".skill")] + ".zip"
    vr = validate_upload(
        filename=validate_name,
        content=data,
        allowed_extensions=frozenset({"zip"}),
        max_size_bytes=_UPLOAD_MAX_SIZE_BYTES,
        caller="skill_upload_router",
    )
    if not vr.valid:
        raise HTTPException(status_code=400, detail=f"Upload rejected: {vr.error}")

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="File is not a valid zip archive.")

    # Reject before decompressing when the declared inflated total is implausible
    # — cheap protection against a zip bomb whose entries claim huge file_size.
    if sum(zi.file_size for zi in zf.infolist()) > _UPLOAD_MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise HTTPException(status_code=400, detail="Archive contents are too large.")

    def _read_entry(entry: str, limit: int) -> bytes:
        """Read one zip entry, refusing anything whose declared or actual size
        exceeds ``limit`` (defence in depth against a lying central directory)."""
        if zf.getinfo(entry).file_size > limit:
            raise HTTPException(status_code=400, detail=f"'{entry}' exceeds the {limit // 1024}KB limit.")
        raw = zf.read(entry)
        if len(raw) > limit:
            raise HTTPException(status_code=400, detail=f"'{entry}' exceeds the {limit // 1024}KB limit.")
        return raw

    # Normalise entry names (forward slashes) and locate SKILL.md. Allow either
    # a root-level SKILL.md or one nested under a single top-level folder.
    names = [n.replace("\\", "/") for n in zf.namelist() if not n.endswith("/")]
    skill_md_entries = [n for n in names if n.split("/")[-1] == "SKILL.md"]
    if not skill_md_entries:
        raise HTTPException(status_code=400, detail="No SKILL.md found in the archive.")
    # Prefer the shallowest SKILL.md (fewest path segments).
    skill_md_path = min(skill_md_entries, key=lambda n: n.count("/"))
    root_prefix = skill_md_path[: -len("SKILL.md")]  # "" or "myskill/"

    try:
        skill_content = _read_entry(skill_md_path, _UPLOAD_MAX_SKILL_MD_BYTES).decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="SKILL.md is not valid UTF-8 text.")

    # Frontmatter/shape checks (opening ``---``, closing ``---``, kebab-case
    # name, description length, unknown keys) are the ONLY validations the
    # operator can relax via ``SKILL_UPLOAD_STRICT_FRONTMATTER=0`` — safety
    # checks above (zip validity, magic bytes, size caps, UTF-8, path
    # traversal, extension whitelist) always run. See
    # ``abstudio/backend/app/api/SKILL_UPLOAD_VALIDATION.md``.
    from app.core.config import env_flag
    strict_frontmatter = env_flag("SKILL_UPLOAD_STRICT_FRONTMATTER", default=True)

    valid, msg = _validate_skill_md(skill_content)
    if not valid:
        if strict_frontmatter:
            raise HTTPException(status_code=400, detail=f"SKILL.md is invalid: {msg}")
        # Lax mode: keep going. Log the shape issue so operators still see it.
        logger.info(
            f"[AGENT] skill_upload: frontmatter shape check skipped "
            f"(SKILL_UPLOAD_STRICT_FRONTMATTER=0) — {msg}"
        )

    fm = parse_frontmatter(skill_content)
    name = (fm.get("name") or "").strip()
    if not name:
        if strict_frontmatter:
            raise HTTPException(status_code=400, detail="SKILL.md frontmatter is missing 'name'.")
        # Lax mode: derive a name from the top-level folder or filename so the
        # row still has a primary key. Kebab-case is enforced downstream by
        # the workflow_repo layer; slugify to that shape defensively.
        candidate = root_prefix.rstrip("/") or original_name.rsplit(".", 1)[0]
        candidate = candidate.strip().lower()
        candidate = re.sub(r"[^a-z0-9-]+", "-", candidate).strip("-")
        candidate = re.sub(r"-{2,}", "-", candidate)
        name = candidate[:64] or "uploaded-skill"
        logger.info(
            f"[AGENT] skill_upload: frontmatter missing 'name' — "
            f"derived '{name}' from archive (lax mode)"
        )
    description = (fm.get("description") or "").strip()
    resolved_category = (category.strip() or fm.get("metadata") or "general") or "general"

    # Collect bundled scripts/references, enforcing the same safety rules as the
    # generation path: known prefixes, allowed extensions, no path traversal,
    # per-file size cap.
    bundle_files: list[Dict[str, Any]] = []
    for entry in names:
        if entry == skill_md_path:
            continue
        rel = entry[len(root_prefix):] if root_prefix and entry.startswith(root_prefix) else entry
        rel = rel.lstrip("/")
        if not rel:
            continue
        if rel.startswith("scripts/"):
            kind = "script"
        elif rel.startswith("references/"):
            kind = "reference"
        else:
            # Ignore anything outside the two known bundle folders.
            continue
        safe = _safe_rel_path(rel, kind)
        if not safe:
            logger.warning(f"[AGENT] skill_upload: skipping unsafe/invalid bundle path '{entry}'")
            continue
        if len(bundle_files) >= _UPLOAD_MAX_BUNDLE_FILES:
            raise HTTPException(
                status_code=400,
                detail=f"Too many bundled files (max {_UPLOAD_MAX_BUNDLE_FILES}).",
            )
        raw = _read_entry(entry, _UPLOAD_MAX_BUNDLE_FILE_BYTES)
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail=f"Bundled file '{safe}' is not valid UTF-8 text.")
        bundle_files.append({
            "rel_path": safe,
            "content": body,
            "size_bytes": len(raw),
            "description": "",
            "kind": kind,
            "abs_path": "",
        })

    row = await workflow_repo.upsert_skill(
        name=name,
        content=skill_content,
        description=description,
        category=resolved_category,
        generated=True,
        # Mark the row as uploaded so the Skills tab can filter/badge it
        # separately from AI-Factory-generated skills.
        source="upload",
    )

    bundle_written = 0
    bundle_error = ""
    if bundle_files:
        try:
            bundle_written = await workflow_repo.upsert_skill_files(name, bundle_files)
        except Exception as exc:
            logger.exception(f"[AGENT] skill_upload: failed to persist bundled files for {name}")
            bundle_error = str(exc)

    # Governance: uploaded skills require HOD approval before use, same as
    # AI-generated ones.
    await _gc.submit_skill_async(
        name,
        content=skill_content,
        created_by=str(current_user.id or ""),
        actor=(current_user.full_name or current_user.email or str(current_user.id or "")),
        department=current_user.department or "",
        description=description,
        visibility=visibility,
    )

    catalog_cache.invalidate()
    # Fetch the just-written mirror row so the response carries Scope /
    # Created by / status like the list endpoint — otherwise the Skills card
    # renders a blank meta row until the user opens the detail modal.
    meta = _gc.bulk_governance_meta("skills", [name]).get(name)
    result = _slim_skill_row(row, meta, viewer_id=str(current_user.id or ""))
    result["bundle_files_written"] = bundle_written
    result["bundle_files_error"] = bundle_error
    return result


@router.get("/agent-registry/agents")
async def list_factory_agents(current_user: AuthenticatedUser = Depends(require_access)):
    return {"agents": _registry.list_agents()}


@router.delete("/agent-registry/{agent_id}", status_code=204)
async def delete_factory_agent(agent_id: str, current_user: AuthenticatedUser = Depends(require_admin)):
    # Admin-gated (security review F-05): the factory agent registry is a
    # single shared store, not scoped per-user.
    deleted = _registry.delete(agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Agent not found")
    logger.info(f'[AGENT] Factory agent deleted by admin={current_user.id}: {agent_id}')
