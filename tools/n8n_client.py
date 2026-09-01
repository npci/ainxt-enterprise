# SPDX-License-Identifier: Apache-2.0
# ============================================================
# N8N CLIENT — Full REST API CRUD + execution tracking
# ============================================================
#
# Env vars:
#   N8N_URL        — n8n base URL (default: http://localhost:5678)
#   N8N_API_KEY    — n8n API key (set in n8n Settings > API)
#   N8N_WEBHOOK_URL — direct webhook trigger URL (legacy)
#
# n8n REST API v1:
#   GET    /api/v1/workflows            list
#   POST   /api/v1/workflows            create
#   GET    /api/v1/workflows/{id}       get
#   PUT    /api/v1/workflows/{id}       update
#   DELETE /api/v1/workflows/{id}       delete
#   POST   /api/v1/workflows/{id}/activate
#   POST   /api/v1/workflows/{id}/deactivate
#   POST   /webhook/{path}             trigger (returns execution data)
#   GET    /api/v1/executions           list executions
#   GET    /api/v1/executions/{id}      execution status
# ============================================================

import os
import time
from typing import Optional

import httpx

from core.logger import logger

# ── Config ─────────────────────────────────────────────────────

N8N_BASE_URL    = os.getenv("N8N_URL",    "").rstrip("/")
N8N_API_KEY     = os.getenv("N8N_API_KEY", "")
N8N_API_BASE    = f"{N8N_BASE_URL}/api/v1"

# Persistent httpx client — reuses TCP connection
_http = httpx.Client(
    timeout=30.0,
    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
)


def _headers() -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if N8N_API_KEY:
        h["X-N8N-API-KEY"] = N8N_API_KEY
    return h


def _api(method: str, path: str, body: dict = None) -> dict:
    url = f"{N8N_API_BASE}{path}"
    try:
        resp = _http.request(method, url, headers=_headers(),
                             json=body if body else None)
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    except httpx.HTTPStatusError as e:
        logger.error(f"n8n {method} {path} → {e.response.status_code}: {e.response.text[:200]}")
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        logger.error(f"n8n request failed: {e}")
        return {"error": str(e)}


# ============================================================
# WORKFLOW CRUD
# ============================================================

def list_workflows(active_only: bool = False) -> dict:
    """Return all workflows (optionally filtered to active ones)."""
    result = _api("GET", "/workflows")
    if "error" in result:
        return result
    wfs = result.get("data", [])
    if active_only:
        wfs = [w for w in wfs if w.get("active")]
    return {"workflows": wfs, "total": len(wfs)}


def get_workflow(workflow_id: str) -> dict:
    """Fetch a single workflow by ID."""
    return _api("GET", f"/workflows/{workflow_id}")


def create_workflow(workflow_def: dict) -> dict:
    """
    Create a new workflow from a definition dict.
    Returns the created workflow including its assigned ID.
    """
    result = _api("POST", "/workflows", workflow_def)
    if "error" not in result:
        logger.info(f"n8n: workflow created → id={result.get('id')} name={result.get('name')}")
    return result


def update_workflow(workflow_id: str, workflow_def: dict) -> dict:
    """Update an existing workflow. The full definition must be supplied."""
    result = _api("PUT", f"/workflows/{workflow_id}", workflow_def)
    if "error" not in result:
        logger.info(f"n8n: workflow updated → id={workflow_id}")
    return result


def delete_workflow(workflow_id: str) -> dict:
    """Delete a workflow. Returns {} on success."""
    result = _api("DELETE", f"/workflows/{workflow_id}")
    if "error" not in result:
        logger.info(f"n8n: workflow deleted → id={workflow_id}")
        return {"success": True, "workflow_id": workflow_id}
    return result


def activate_workflow(workflow_id: str) -> dict:
    """Activate a workflow so it responds to its trigger."""
    return _api("POST", f"/workflows/{workflow_id}/activate")


def deactivate_workflow(workflow_id: str) -> dict:
    """Deactivate a workflow."""
    return _api("POST", f"/workflows/{workflow_id}/deactivate")


# ============================================================
# WORKFLOW EXECUTION (webhook trigger)
# ============================================================

def trigger_workflow(webhook_path: str, payload: dict) -> dict:
    """
    Trigger a workflow via its webhook URL and return the response.
    webhook_path — the path configured on the Webhook node (e.g. "agent-trigger").
    """
    url = f"{N8N_BASE_URL}/webhook/{webhook_path.lstrip('/')}"
    try:
        resp = _http.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        logger.error(f"n8n trigger_workflow failed: {e}")
        return {"error": str(e)}


# ============================================================
# EXECUTION STATUS POLLING
# ============================================================

def list_executions(workflow_id: str = None, limit: int = 20) -> dict:
    """List recent executions, optionally scoped to a workflow."""
    path = f"/executions?limit={limit}"
    if workflow_id:
        path += f"&workflowId={workflow_id}"
    return _api("GET", path)


def get_execution(execution_id: str) -> dict:
    """Fetch a single execution by ID — includes status and output data."""
    return _api("GET", f"/executions/{execution_id}")


def wait_for_execution(
    execution_id: str,
    timeout_sec: int = 120,
    poll_interval: float = 2.0,
) -> dict:
    """
    Poll until execution reaches a terminal state (success/error/crashed/waiting).
    Returns the final execution record.
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        result = get_execution(execution_id)
        if "error" in result:
            return result
        status = result.get("status", "running")
        if status in ("success", "error", "crashed", "waiting"):
            return result
        time.sleep(poll_interval)
    return {"error": f"Execution {execution_id} timed out after {timeout_sec}s", "status": "timeout"}
