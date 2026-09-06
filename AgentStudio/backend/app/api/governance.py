# SPDX-License-Identifier: MIT
"""Governance/approval endpoints for Build Studio artifacts.

Build Studio stores the operational copy of an artifact (its graph/config) in
ABStudio's own tables, while the platform governance lifecycle lives on the
``*_pg`` mirror records read by ``routers/governance_router``. A brand-new
Build Studio artifact has NO mirror record yet, so calling the platform
``/governance/{type}/{name}/submit`` directly 404s.

These endpoints bridge that gap: they look the artifact up in ABStudio, build
the mirror record on demand (with the creator's real department + a content
hash for template-modification detection), and drive the submit through
``governance_client`` — which reuses the platform router's inbox notify +
signed audit event. Mounted on the ABStudio base (``/ainxt/v1/api/abs``).
"""

from typing import Optional
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from app.models import AuthenticatedUser
from app.api.deps import require_access
from app.core import workflow_repo
from app.core import governance_client as gc

router = APIRouter()
from core.logger import logger
class SubmitBody(BaseModel):
    # Optional submitter note: "why this should be approved".
    reason: Optional[str] = ""
    # Requested catalog visibility once approved: 'public' (all users) or
    # 'private' (only the submitter's department). Applied to the published
    # template on approval. Defaults to public.
    visibility: Optional[str] = "public"

_VALID = {"workflows", "agents", "skills"}
_VALID_VISIBILITY = {"public", "private"}


async def _resolve_artifact(entity_type: str, name: str, user: AuthenticatedUser):
    """Return (canonical_content, description) for the named artifact, or None.

    Content is the semantic payload hashed for template-modification detection.
    """
    if entity_type == "workflows":
        for wf in await workflow_repo.get_all_workflows(user.id):
            if wf.get("name") == name:
                return wf.get("graphData") or wf.get("graph_data") or {}, wf.get("description", "")
        return None
    if entity_type == "agents":
        for ag in await workflow_repo.get_all_agents(user.id):
            if ag.get("name") == name:
                content = {k: ag.get(k) for k in
                           ("instructions", "model_name", "tools", "skills",
                            "guardrails", "memory_config", "attached_flows")}
                return content, ag.get("description", "")
        return None
    if entity_type == "skills":
        sk = await workflow_repo.get_skill(name)
        if sk:
            return sk.get("content", ""), sk.get("description", "")
        return None
    return None


@router.get("/governance-status/{entity_type}/{name}")
async def governance_status(
    entity_type: str,
    name: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Current governance status for an artifact. ``null`` = not yet submitted.

    Status is looked up by ``name`` only (not scoped to the caller as the
    creator). The named artifact is unique in ``*_pg`` (``uq_*_name_org``),
    so name-only is the authoritative lookup — and it must return the same
    status regardless of who is asking, otherwise the Deploy / Cancel /
    Delete affordance on a non-owner's card would misrepresent the state
    (e.g. showing ``Deploy`` for a skill that is already APPROVED).
    """
    if entity_type not in _VALID:
        raise HTTPException(status_code=404, detail=f"Unknown entity type: {entity_type}")
    return {"entity_type": entity_type, "name": name,
            "status": gc.get_governance_status(entity_type, name)}


@router.post("/governance-submit/{entity_type}/{name}")
async def governance_submit(
    entity_type: str,
    name: str,
    body: SubmitBody = Body(default=SubmitBody()),
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Submit a Build Studio artifact to its department manager for approval.

    Creates the governance mirror record on demand from the artifact's real
    data, then submits — so this works even for artifacts created before the
    governance layer existed. ``body.reason`` is an optional submitter note.
    """
    if entity_type not in _VALID:
        raise HTTPException(status_code=404, detail=f"Unknown entity type: {entity_type}")

    resolved = await _resolve_artifact(entity_type, name, current_user)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"{entity_type} '{name}' not found")
    content, description = resolved

    department = current_user.department or gc.resolve_user_department(current_user.id)
    actor = current_user.full_name or current_user.email or str(current_user.id or "")
    visibility = (body.visibility or "public").strip().lower()
    if visibility not in _VALID_VISIBILITY:
        visibility = "public"
    status = gc.submit_for_governance(
        entity_type, name,
        created_by=str(current_user.id or ""),
        actor=actor,
        department=department or "",
        description=description or "",
        reason=(body.reason or "").strip(),
        visibility=visibility,
        source_template_hash=gc.canonical_hash(content) if content is not None else None,
    )
    # Resolve who the submission was routed to so the maker sees a clear
    # "Submitted for approval to <names>" confirmation instead of a generic
    # message. Reuses the platform router's resolver (shared by the approval
    # guard + inbox notification) so the names match the actual recipients.
    from routers.governance_router import _resolve_approver_display_names
    approver_names = _resolve_approver_display_names(department or "", visibility)
    message = "Submitted for approval. Your department manager has been notified."
    if approver_names:
        message = (
            "Submitted for approval to: " + ", ".join(approver_names) + ". "
            "They have been notified and can review it in their inbox."
        )
    return {"entity_type": entity_type, "name": name, "status": status,
            "message": message, "approvers": approver_names}


@router.post("/governance-withdraw/{entity_type}/{name}")
async def governance_withdraw(
    entity_type: str,
    name: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Cancel a pending deploy request and return the artifact to an editable
    DRAFT. Owner-scoped: only the submitter (or an approver) may cancel — the
    ``created_by`` we pass is the caller's own id, so the platform router's
    owner guard authorizes it. Valid only while the artifact is still awaiting
    approval; otherwise a 409 is returned.
    """
    if entity_type not in _VALID:
        raise HTTPException(status_code=404, detail=f"Unknown entity type: {entity_type}")

    # Confirm the artifact exists and belongs to the caller before touching it.
    resolved = await _resolve_artifact(entity_type, name, current_user)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"{entity_type} '{name}' not found")

    actor = current_user.full_name or current_user.email or str(current_user.id or "")
    try:
        status = gc.withdraw_governance(
            entity_type, name,
            created_by=str(current_user.id or ""),
            actor=actor,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"[AGENT] governance_withdraw failed for {entity_type}/{name}")
        raise HTTPException(status_code=500, detail=str(exc))
    return {"entity_type": entity_type, "name": name, "status": status,
            "message": "Deploy request cancelled. The item is editable again."}
