# SPDX-License-Identifier: MIT
# ============================================================
# MCP GOVERNANCE ROUTER
#
# Provides approval workflows and versioning for MCP tool
# registrations. Admins approve or reject pending tools.
# Every approval/rejection is logged with a reason.
#
# Endpoints
# ─────────
# GET  /governance/pending          — list tools awaiting approval
# POST /governance/approve/{name}   — approve a pending tool
# POST /governance/reject/{name}    — reject a pending tool
# GET  /governance/log              — audit log of governance actions
# GET  /governance/versions/{name}  — version history for a tool
# POST /governance/rollback/{name}  — rollback to a prior version
# ============================================================

import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.dependencies import get_current_user, require_admin

router = APIRouter(prefix="/governance", tags=["mcp-governance"])

# ── In-process stores (Redis-backed in production) ────────────

_pending: dict   = {}   # name → tool_definition
_approved: dict  = {}   # name → current approved definition
_rejected: dict  = {}   # name → {definition, reason}
_versions: dict  = {}   # name → [list of versioned snapshots]
_audit_log: list = []   # chronological list of governance events


# ─────────────────────────────────────────────────────────────

class SubmitRequest(BaseModel):
    name:        str
    endpoint:    str
    description: str
    tools:       Optional[list]  = []
    auth_config: Optional[dict]  = {}
    submitted_by: Optional[str] = "system"


class ApproveRequest(BaseModel):
    approved_by: str
    notes:       Optional[str] = ""


class RejectRequest(BaseModel):
    rejected_by: str
    reason:      str


class RollbackRequest(BaseModel):
    version:       int
    rolled_back_by: str


# ── Helpers ───────────────────────────────────────────────────

def _log(action: str, tool_name: str, actor: str, detail: str = ""):
    _audit_log.append({
        "timestamp": time.time(),
        "action":    action,
        "tool":      tool_name,
        "actor":     actor,
        "detail":    detail,
    })


def _snapshot_version(name: str, definition: dict, actor: str, event: str):
    """Save a versioned snapshot of a tool definition."""
    history = _versions.setdefault(name, [])
    history.append({
        "version":    len(history) + 1,
        "definition": definition,
        "event":      event,
        "actor":      actor,
        "timestamp":  time.time(),
    })


# ── Endpoints ─────────────────────────────────────────────────

@router.post("/submit")
def submit_for_approval(body: SubmitRequest, _user: dict = Depends(get_current_user)):
    """
    Submit an MCP server / tool for governance review.
    Called automatically by /tools/register when approval is required.
    """
    if body.name in _approved:
        # Treat as a version update — re-enter approval queue
        _pending[body.name] = {
            "name":         body.name,
            "endpoint":     body.endpoint,
            "description":  body.description,
            "tools":        body.tools,
            "auth_config":  body.auth_config,
            "submitted_by": body.submitted_by,
            "submitted_at": time.time(),
            "is_update":    True,
        }
        _log("submitted_update", body.name, body.submitted_by)
        return {"status": "pending_review", "message": "Version update submitted for approval"}

    _pending[body.name] = {
        "name":         body.name,
        "endpoint":     body.endpoint,
        "description":  body.description,
        "tools":        body.tools,
        "auth_config":  body.auth_config,
        "submitted_by": body.submitted_by,
        "submitted_at": time.time(),
        "is_update":    False,
    }
    _log("submitted", body.name, body.submitted_by)
    return {"status": "pending_review", "message": f"'{body.name}' submitted for admin approval"}


@router.get("/pending")
def list_pending(_user: dict = Depends(get_current_user)):
    """List all MCP tools awaiting governance approval."""
    return {
        "pending": list(_pending.values()),
        "count":   len(_pending),
    }


@router.post("/approve/{name}")
def approve_tool(name: str, body: ApproveRequest, _user: dict = Depends(require_admin)):
    """Approve a pending MCP tool. Moves it to the approved registry."""
    if name not in _pending:
        raise HTTPException(status_code=404, detail=f"No pending submission for '{name}'")

    definition = _pending.pop(name)
    definition["approved_by"] = body.approved_by
    definition["approved_at"] = time.time()
    definition["notes"]       = body.notes

    _approved[name] = definition
    _snapshot_version(name, definition, body.approved_by, "approved")
    _log("approved", name, body.approved_by, body.notes or "")

    # Propagate to live MCP registry
    try:
        from mcp.registry import MCPRegistry
        registry = MCPRegistry()
        registry.register_http_tool(
            name=name,
            endpoint=definition["endpoint"],
            description=definition["description"],
            tools=definition.get("tools", []),
            auth_config=definition.get("auth_config", {}),
        )
    except Exception as e:
        # Governance approval succeeded; registry sync is best-effort
        _log("registry_sync_failed", name, body.approved_by, str(e))

    return {"status": "approved", "tool": name, "version": len(_versions.get(name, []))}


@router.post("/reject/{name}")
def reject_tool(name: str, body: RejectRequest, _user: dict = Depends(require_admin)):
    """Reject a pending MCP tool submission."""
    if name not in _pending:
        raise HTTPException(status_code=404, detail=f"No pending submission for '{name}'")

    definition = _pending.pop(name)
    _rejected[name] = {
        "definition":  definition,
        "reason":      body.reason,
        "rejected_by": body.rejected_by,
        "rejected_at": time.time(),
    }
    _log("rejected", name, body.rejected_by, body.reason)

    return {"status": "rejected", "tool": name, "reason": body.reason}


@router.get("/log")
def get_audit_log(limit: int = 100, _user: dict = Depends(get_current_user)):
    """Return the most recent governance audit log entries."""
    recent = sorted(_audit_log, key=lambda e: e["timestamp"], reverse=True)
    return {
        "log":   recent[:limit],
        "total": len(_audit_log),
    }


@router.get("/versions/{name}")
def get_versions(name: str, _user: dict = Depends(get_current_user)):
    """Return the version history for a tool."""
    history = _versions.get(name)
    if history is None:
        raise HTTPException(status_code=404, detail=f"No version history for '{name}'")
    return {
        "tool":     name,
        "versions": history,
        "current":  len(history),
    }


@router.post("/rollback/{name}")
def rollback_tool(name: str, body: RollbackRequest, _user: dict = Depends(require_admin)):
    """Rollback a tool to a prior approved version."""
    history = _versions.get(name, [])
    if not history:
        raise HTTPException(status_code=404, detail=f"No version history for '{name}'")

    target_idx = body.version - 1
    if target_idx < 0 or target_idx >= len(history):
        raise HTTPException(
            status_code=400,
            detail=f"Version {body.version} not found; available: 1–{len(history)}"
        )

    snapshot  = history[target_idx]["definition"]
    _approved[name] = snapshot
    _snapshot_version(name, snapshot, body.rolled_back_by, f"rollback_to_v{body.version}")
    _log("rollback", name, body.rolled_back_by, f"rolled back to version {body.version}")

    return {
        "status":       "rolled_back",
        "tool":         name,
        "to_version":   body.version,
        "new_version":  len(_versions[name]),
    }


@router.get("/status")
def governance_status(_user: dict = Depends(get_current_user)):
    """Summary of governance state."""
    return {
        "pending_count":  len(_pending),
        "approved_count": len(_approved),
        "rejected_count": len(_rejected),
        "audit_entries":  len(_audit_log),
        "tools_with_history": list(_versions.keys()),
    }
