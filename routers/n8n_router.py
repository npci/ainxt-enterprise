# SPDX-License-Identifier: MIT
# ============================================================
# N8N ROUTER  — /n8n
# Full CRUD for n8n workflows + execution tracking.
# All endpoints require operator+ role.
# ============================================================

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.rbac import require_role
from core.logger import logger

router = APIRouter(prefix="/n8n", tags=["n8n"])

_OP = Depends(require_role("operator"))


# ============================================================
# REQUEST MODELS
# ============================================================

class WorkflowCreateRequest(BaseModel):
    definition: dict              # raw n8n workflow JSON
    activate: bool = True         # activate immediately after create


class WorkflowUpdateRequest(BaseModel):
    definition: dict
    activate: Optional[bool] = None


class TriggerRequest(BaseModel):
    webhook_path: str
    payload: dict = {}


class AutoBuildRequest(BaseModel):
    task_description: str
    activate: bool = True


# ============================================================
# WORKFLOW CRUD
# ============================================================

@router.get("/workflows")
def list_workflows(active_only: bool = False, _=_OP):
    """List all workflows in n8n."""
    from tools.n8n_client import list_workflows as _list
    return _list(active_only=active_only)


@router.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str, _=_OP):
    """Get a single workflow by ID."""
    from tools.n8n_client import get_workflow as _get
    result = _get(workflow_id)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.post("/workflows", status_code=201)
def create_workflow(body: WorkflowCreateRequest, _=_OP):
    """Create a new n8n workflow from a definition dict."""
    from tools.n8n_client import create_workflow as _create, activate_workflow
    result = _create(body.definition)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    if body.activate:
        wf_id = result.get("id")
        if wf_id:
            activate_workflow(wf_id)
    return result


@router.put("/workflows/{workflow_id}")
def update_workflow(workflow_id: str, body: WorkflowUpdateRequest, _=_OP):
    """Update an existing workflow."""
    from tools.n8n_client import update_workflow as _update, activate_workflow, deactivate_workflow
    result = _update(workflow_id, body.definition)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    if body.activate is True:
        activate_workflow(workflow_id)
    elif body.activate is False:
        deactivate_workflow(workflow_id)
    return result


@router.delete("/workflows/{workflow_id}")
def delete_workflow(workflow_id: str, _=_OP):
    """Delete a workflow."""
    from tools.n8n_client import delete_workflow as _delete
    result = _delete(workflow_id)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.post("/workflows/{workflow_id}/activate")
def activate_workflow(workflow_id: str, _=_OP):
    """Activate a workflow."""
    from tools.n8n_client import activate_workflow as _activate
    result = _activate(workflow_id)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.post("/workflows/{workflow_id}/deactivate")
def deactivate_workflow(workflow_id: str, _=_OP):
    """Deactivate a workflow."""
    from tools.n8n_client import deactivate_workflow as _deactivate
    result = _deactivate(workflow_id)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


# ============================================================
# EXECUTION TRIGGER + STATUS
# ============================================================

@router.post("/trigger")
def trigger_workflow(body: TriggerRequest, _=_OP):
    """Trigger a workflow via its webhook path."""
    from tools.n8n_client import trigger_workflow as _trigger
    result = _trigger(body.webhook_path, body.payload)
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.get("/executions")
def list_executions(workflow_id: Optional[str] = None, limit: int = 20, _=_OP):
    """List recent executions, optionally scoped to a workflow."""
    from tools.n8n_client import list_executions as _list_exec
    return _list_exec(workflow_id=workflow_id, limit=limit)


@router.get("/executions/{execution_id}")
def get_execution(execution_id: str, _=_OP):
    """Get execution status and output."""
    from tools.n8n_client import get_execution as _get_exec
    result = _get_exec(execution_id)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.get("/executions/{execution_id}/wait")
def wait_for_execution(execution_id: str, timeout: int = 120, _=_OP):
    """Poll until execution completes (max timeout seconds)."""
    from tools.n8n_client import wait_for_execution as _wait
    return _wait(execution_id, timeout_sec=timeout)


# ============================================================
# AUTONOMOUS BUILDER
# ============================================================

@router.post("/build", status_code=201)
def autonomous_build(body: AutoBuildRequest, _=_OP):
    """
    Generate, validate, create, and activate an n8n workflow from a
    plain-English task description using Claude.
    """
    from tools.n8n_autonomous_builder import autonomous_build as _build
    try:
        result = _build(body.task_description)
        return result
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.error(f"n8n_router: autonomous_build failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
