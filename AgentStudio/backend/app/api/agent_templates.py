# SPDX-License-Identifier: MIT
"""Agent template endpoints — pre-built agent presets served from DB."""
from fastapi import APIRouter, Depends, HTTPException
from app.models import AuthenticatedUser
from app.core import workflow_repo
from app.core.workflow_repo import NameValidationError
from app.api.deps import require_access

router = APIRouter()


@router.get("/agent-templates")
async def list_agent_templates(current_user: AuthenticatedUser = Depends(require_access)):
    # Department-scoped: admins see all; others see public + their own dept's
    # private presets. See workflow_repo.get_all_agent_templates.
    return await workflow_repo.get_all_agent_templates(
        department=current_user.department or "",
        is_admin=(current_user.role == "admin"),
    )


@router.get("/agent-templates/{template_id}")
async def get_agent_template_route(template_id: str, current_user: AuthenticatedUser = Depends(require_access)):
    t = await workflow_repo.get_agent_template(
        template_id,
        department=current_user.department or "",
        is_admin=(current_user.role == "admin"),
    )
    if not t:
        raise HTTPException(status_code=404, detail="Agent template not found")
    return t


@router.post("/agent-templates/{template_id}/use", status_code=201)
async def use_agent_template_route(template_id: str, current_user: AuthenticatedUser = Depends(require_access)):
    try:
        agent = await workflow_repo.use_agent_template(
            template_id,
            current_user.id,
            department=current_user.department or "",
            is_admin=(current_user.role == "admin"),
        )
    except NameValidationError as exc:
        # Should not normally happen now that use_agent_template derives a
        # unique name, but guard so a collision never surfaces as a 500.
        raise HTTPException(
            status_code=409,
            detail={"error": "name_conflict", "message": str(exc)},
        )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent template not found")
    return agent
