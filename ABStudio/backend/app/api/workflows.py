# SPDX-License-Identifier: Apache-2.0
"""Workflow CRUD endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from app.models import AuthenticatedUser
from app.core import workflow_repo
from app.core.workflow_repo import StaleWorkflowError, NameValidationError
from app.services import trigger_scheduler
from app.engine import get_engine
from app.api.deps import require_access
from app.core.governance import audit_event

router = APIRouter()
from core.logger import logger
@router.get("/workflows")
async def list_workflows(current_user: AuthenticatedUser = Depends(require_access)):
    return await workflow_repo.get_all_workflows(current_user.id)


@router.post("/workflows", status_code=201)
async def create_workflow_route(data: dict, current_user: AuthenticatedUser = Depends(require_access)):
    try:
        wf = await workflow_repo.create_workflow(data, current_user.id, current_user.full_name)
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.workflow.crud",
            action="create",
            workflow_id=wf.get("id", "") if isinstance(wf, dict) else "",
            workflow_name=data.get("name", ""),
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
        )
        return wf
    except NameValidationError as exc:
        # Bad/duplicate name -- surface as 400 with the validator's message so
        # the frontend can show it inline next to the name field.
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.workflow.crud",
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
            endpoint="abstudio.workflow.crud",
            action="create_error",
            workflow_name=data.get("name", ""),
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workflows/{workflow_id}")
async def get_workflow_route(workflow_id: str, current_user: AuthenticatedUser = Depends(require_access)):
    wf = await workflow_repo.get_workflow(workflow_id, current_user.id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return wf


@router.put("/workflows/{workflow_id}")
async def update_workflow_route(workflow_id: str, data: dict, current_user: AuthenticatedUser = Depends(require_access)):
    try:
        wf = await workflow_repo.update_workflow(workflow_id, data, current_user.id)
    except StaleWorkflowError as exc:
        # Concurrent writer beat this request. Return the server-side row so
        # the frontend can merge instead of silently overwriting.
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.workflow.crud",
            action="update_stale",
            workflow_id=workflow_id,
            workflow_name=data.get("name", ""),
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error="stale_workflow",
        )
        raise HTTPException(status_code=409, detail={
            "error": "stale_workflow",
            "current": exc.current,
        })
    except NameValidationError as exc:
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.workflow.crud",
            action="update_invalid_name",
            workflow_id=workflow_id,
            workflow_name=data.get("name", ""),
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail={"error": "invalid_name", "message": str(exc)})
    except Exception as exc:
        logger.exception(f'[AGENT] update_workflow failed for {workflow_id}')
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.workflow.crud",
            action="update_error",
            workflow_id=workflow_id,
            workflow_name=data.get("name", ""),
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=str(exc))
    if not wf:
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.workflow.crud",
            action="update_missing",
            workflow_id=workflow_id,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
        )
        raise HTTPException(status_code=404, detail="Workflow not found")
    audit_event(
        user_id=current_user.id,
        endpoint="abstudio.workflow.crud",
        action="update",
        workflow_id=workflow_id,
        workflow_name=data.get("name", ""),
        email=current_user.email,
        department=getattr(current_user, "department", "") or "",
    )
    return wf


@router.delete("/workflows/{workflow_id}", status_code=204)
async def delete_workflow_route(workflow_id: str, current_user: AuthenticatedUser = Depends(require_access)):
    try:
        existing = await workflow_repo.list_triggers(current_user.id, "workflow", workflow_id)
        for t in existing:
            trigger_scheduler.deregister_trigger(t["id"])
        await workflow_repo.delete_triggers_for_target("workflow", workflow_id)
    except Exception:
        logger.exception('[AGENT] trigger cleanup on workflow delete failed')
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.workflow.crud",
            action="delete_trigger_cleanup_error",
            workflow_id=workflow_id,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error="trigger cleanup failed",
        )
    # Cascade-delete this workflow's chat history (threads, HITL snapshots,
    # loop/condition/HITL audit trails). Best-effort: a checkpoint-store
    # failure must not block the workflow row from being removed.
    try:
        removed = await get_engine().delete_threads_for_workflow(workflow_id)
        if removed:
            logger.info(f'[AGENT] deleted {removed} chat thread(s) for workflow {workflow_id}')
    except Exception:
        logger.exception('[AGENT] chat-history cleanup on workflow delete failed')
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.workflow.crud",
            action="delete_chat_cleanup_error",
            workflow_id=workflow_id,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
            error="chat-history cleanup failed",
        )
    await workflow_repo.delete_workflow(workflow_id, current_user.id)
    # Best-effort: remove any workflow-node sample-doc folder on disk.
    # Mirrors the agent-delete cleanup — the workflow JSON is gone, so
    # leaving the files behind would be a slow-growing leak.
    try:
        import shutil as _shutil
        from app.api.agent_sample import (
            _workflow_samples_root as _wf_samples_root,
        )
        _safe = "".join(
            c for c in (workflow_id or "") if c.isalnum() or c in ("-", "_")
        )
        if _safe:
            _wf_dir = _wf_samples_root() / _safe
            if _wf_dir.exists():
                _shutil.rmtree(_wf_dir, ignore_errors=True)
    except Exception:
        logger.exception("[AGENT] workflow-node sample cleanup on workflow delete failed")
    audit_event(
        user_id=current_user.id,
        endpoint="abstudio.workflow.crud",
        action="delete",
        workflow_id=workflow_id,
        email=current_user.email,
        department=getattr(current_user, "department", "") or "",
    )


@router.post("/workflows/{workflow_id}/duplicate", status_code=201)
async def duplicate_workflow_route(workflow_id: str, current_user: AuthenticatedUser = Depends(require_access)):
    wf = await workflow_repo.duplicate_workflow(workflow_id, current_user.id, current_user.full_name)
    if not wf:
        audit_event(
            user_id=current_user.id,
            endpoint="abstudio.workflow.crud",
            action="duplicate_missing",
            workflow_id=workflow_id,
            email=current_user.email,
            department=getattr(current_user, "department", "") or "",
        )
        raise HTTPException(status_code=404, detail="Workflow not found")
    audit_event(
        user_id=current_user.id,
        endpoint="abstudio.workflow.crud",
        action="duplicate",
        workflow_id=workflow_id,
        workflow_name=wf.get("name", "") if isinstance(wf, dict) else "",
        email=current_user.email,
        department=getattr(current_user, "department", "") or "",
        extra={"new_id": wf.get("id", "") if isinstance(wf, dict) else ""},
    )
    return wf
