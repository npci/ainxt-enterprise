# SPDX-License-Identifier: MIT
"""Template endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from app.models import AuthenticatedUser
from app.core import workflow_repo
from app.core.workflow_repo import NameValidationError
from app.api.deps import require_access, require_admin
from core.logger import logger

router = APIRouter()


@router.get("/templates")
async def list_templates(current_user: AuthenticatedUser = Depends(require_access)):
    # Department-scoped: admins see all; others see public + their own dept's
    # private templates. See workflow_repo.get_all_templates.
    return await workflow_repo.get_all_templates(
        department=current_user.department or "",
        is_admin=(current_user.role == "admin"),
    )


@router.post("/templates/reseed")
async def reseed_templates_route(current_user: AuthenticatedUser = Depends(require_admin)):
    """Insert any seed templates that aren't already in the DB. Idempotent --
    rows that already exist are left untouched. Returns insert/skip/fail
    counts so the caller can confirm what changed without restarting.

    Admin-gated (security review F-16): reseeding is a tenant-wide operation
    that can revert operator-curated changes to shared templates, so it must
    not be reachable by an ordinary authenticated user.
    """
    result = await workflow_repo.reseed_templates()
    logger.info(f'[AGENT] Templates reseeded by admin={current_user.id}: {result}')
    return result


@router.get("/templates/{template_id}")
async def get_template_route(template_id: str, current_user: AuthenticatedUser = Depends(require_access)):
    t = await workflow_repo.get_template(template_id)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    return t


@router.post("/templates/{template_id}/use", status_code=201)
async def use_template_route(template_id: str, current_user: AuthenticatedUser = Depends(require_access)):
    try:
        wf = await workflow_repo.use_template(template_id, current_user.id, current_user.full_name)
    except NameValidationError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "name_conflict", "message": str(exc)},
        )
    if not wf:
        raise HTTPException(status_code=404, detail="Template not found")
    return wf
