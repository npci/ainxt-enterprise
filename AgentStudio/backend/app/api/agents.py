# SPDX-License-Identifier: MIT
"""Agent CRUD endpoints."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.models import AuthenticatedUser
from app.core import workflow_repo
from app.core.workflow_repo import NameValidationError
from app.services import trigger_scheduler
from app.api.deps import require_access
from app.api import agent_chat as _agent_chat
from app.core.governance import audit_event

router = APIRouter()
from core.logger import logger


# ARCH-F-ABS1-006: accepting a bare dict means required fields are validated
# deep inside workflow_repo, producing opaque errors. A Pydantic model
# validates at the HTTP boundary and returns a clear 422 with field-level
# errors. extra="allow" forwards unknown fields unchanged for backward
# compatibility with any callers already sending additional fields.
class CreateAgentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = Field(default="", max_length=1000)
    model: Optional[str] = None
    tools: Optional[List[str]] = Field(default_factory=list)
    system_prompt: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)

    # KNOWN SCOPE LIMIT (EA Finding 6 / ARCH-F-ABS1-006):
    # extra="allow" is intentional for backward compatibility — callers that
    # send fields beyond the six named above will not be rejected. However,
    # those extra fields flow through model_dump() into workflow_repo.create_agent()
    # completely un-validated, which is the same gap that existed before this
    # model was introduced. The six named fields above now have a clean 422
    # validation boundary; the remainder do not.
    # Follow-up: audit all callers of POST /agents, confirm none rely on
    # extra fields, then tighten to extra="forbid" to close the boundary fully.
    model_config = {"extra": "allow"}
@router.get("/agents")
async def list_agents(current_user: AuthenticatedUser = Depends(require_access)):
    return await workflow_repo.get_all_agents(current_user.id)


@router.post("/agents", status_code=201)
async def create_agent_route(body: CreateAgentRequest, current_user: AuthenticatedUser = Depends(require_access)):
    data = body.model_dump()
    try:
        agent = await workflow_repo.create_agent(data, current_user.id)
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.agent.crud",
            action="create",
            workflow_id=agent.get("id", "") if isinstance(agent, dict) else "",
            workflow_name=data.get("name", ""),
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
        )
        return agent
    except NameValidationError as exc:
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.agent.crud",
            action="create_invalid_name",
            workflow_name=data.get("name", ""),
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail={"error": "invalid_name", "message": str(exc)})
    except Exception as e:
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.agent.crud",
            action="create_error",
            workflow_name=data.get("name", ""),
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents/{agent_id}")
async def get_agent_route(agent_id: str, current_user: AuthenticatedUser = Depends(require_access)):
    agent = await workflow_repo.get_agent(agent_id, current_user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.put("/agents/{agent_id}")
async def update_agent_route(agent_id: str, data: dict, current_user: AuthenticatedUser = Depends(require_access)):
    try:
        agent = await workflow_repo.update_agent(agent_id, data, current_user.id)
    except NameValidationError as exc:
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.agent.crud",
            action="update_invalid_name",
            workflow_id=agent_id,
            workflow_name=data.get("name", ""),
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail={"error": "invalid_name", "message": str(exc)})
    except Exception as exc:
        logger.exception(f'[AGENT] update_agent failed for {agent_id}')
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.agent.crud",
            action="update_error",
            workflow_id=agent_id,
            workflow_name=data.get("name", ""),
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=str(exc))
    if not agent:
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.agent.crud",
            action="update_missing",
            workflow_id=agent_id,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
        )
        raise HTTPException(status_code=404, detail="Agent not found")
    audit_event(
        user_id=current_user.id,
        endpoint="abstudio.agent.crud",
        action="update",
        workflow_id=agent_id,
        workflow_name=data.get("name", ""),
        email=current_user.email,
        department=getattr(current_user, "department", "") or "",
    )
    return agent


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent_route(agent_id: str, current_user: AuthenticatedUser = Depends(require_access)):
    try:
        existing = await workflow_repo.list_triggers(current_user.id, "agent", agent_id)
        for t in existing:
            trigger_scheduler.deregister_trigger(t["id"])
        await workflow_repo.delete_triggers_for_target("agent", agent_id)
    except Exception:
        logger.exception('[AGENT] trigger cleanup on agent delete failed')
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.agent.crud",
            action="delete_trigger_cleanup_error",
            workflow_id=agent_id,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error="trigger cleanup failed",
        )
    deleted = await workflow_repo.delete_agent(agent_id, current_user.id)
    if not deleted:
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.agent.crud",
            action="delete_missing",
            workflow_id=agent_id,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
        )
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        await _agent_chat.get_store().delete_threads_for_agent(agent_id, current_user.id)
    except Exception:
        logger.exception("agent chat cleanup on agent delete failed")
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.agent.crud",
            action="delete_chat_cleanup_error",
            workflow_id=agent_id,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error="agent chat cleanup failed",
        )
    # Best-effort: also remove any per-agent sample document folder on
    # disk. The DB row is gone by the time we reach this point, so the
    # sample metadata is unrecoverable — leaving the file behind would
    # be a slow-growing leak. Failure here is logged and swallowed
    # (delete is idempotent from the caller's perspective).
    try:
        import shutil as _shutil
        from app.api.agent_sample import _agent_sample_dir as _sample_dir
        _dir = _sample_dir(agent_id)
        if _dir.exists():
            _shutil.rmtree(_dir, ignore_errors=True)
    except Exception:
        logger.exception("[AGENT] sample-doc cleanup on agent delete failed")
    audit_event(
        user_id=current_user.id,
        endpoint="abstudio.agent.crud",
        action="delete",
        workflow_id=agent_id,
        email=current_user.email,
        department=getattr(current_user, "department", "") or "",
    )


@router.post("/agents/{agent_id}/duplicate", status_code=201)
async def duplicate_agent_route(agent_id: str, current_user: AuthenticatedUser = Depends(require_access)):
    agent = await workflow_repo.duplicate_agent(agent_id, current_user.id)
    if not agent:
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.agent.crud",
            action="duplicate_missing",
            workflow_id=agent_id,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
        )
        raise HTTPException(status_code=404, detail="Agent not found")
    audit_event(
        user_id=current_user.id,
        endpoint="abstudio.agent.crud",
        action="duplicate",
        workflow_id=agent_id,
        workflow_name=agent.get("name", "") if isinstance(agent, dict) else "",
        email=current_user.email,
        department=getattr(current_user, "department", "") or "",
        extra={"new_id": agent.get("id", "") if isinstance(agent, dict) else ""},
    )
    return agent
