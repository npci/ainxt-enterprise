# SPDX-License-Identifier: MIT
"""
Cowork ENTERPRISE CONTROLS — admin CRUD for connector policy + role→connector grants.

Parity with Claude Cowork's per-tool connector controls. The READ side (enforcement)
already lives in services/cowork_policy.py (filter_office_catalog + org_denies_tool,
applied in the orchestrator office path AND the desktop MCP bridge). This router is
the admin-facing WRITE/list surface for the rules.

Tables (created in db/migrate.py _part_u1):
  - ainxt.cowork_connector_policy(id, department, connector, tool, allow, created_by, …)
      A row scopes org-wide (department NULL/'') or to a department; tool='*' = whole
      connector. allow=false (deny) wins with precedence (see cowork_policy._org_decision).
  - ainxt.role_connector_grants(id, role, connector_name, created_by, …)
      A role's connector allowlist; absent ⇒ role is unrestricted.

All endpoints are ADMIN-only. Spend limits + usage analytics live in
routers/cowork_usage_router.py.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from auth.dependencies import get_current_user
from auth.rbac import require_admin
from core.logger import logger
from db.database import engine

router = APIRouter(prefix="/buddy", tags=["buddy"])


# ── Connector policy (org / department allow-deny) ────────────────────────────
class ConnectorPolicyBody(BaseModel):
    department: str = ""          # "" / null = org-wide
    connector: str
    tool: str = "*"               # "*" = whole connector
    allow: bool = True


@router.get("/connector-policy")
async def list_connector_policy(department: Optional[str] = None,
                                current_user: dict = Depends(require_admin)):
    """List connector allow/deny rules (optionally filtered to one department)."""
    sql = ("SELECT id, COALESCE(department,'') AS department, connector, tool, allow, "
           "COALESCE(created_by,'') AS created_by, created_at "
           "FROM ainxt.cowork_connector_policy")
    params = {}
    if department is not None:
        sql += " WHERE COALESCE(department,'') = :dept"
        params["dept"] = department
    sql += " ORDER BY connector, tool, department"
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return {"rules": [dict(r) for r in rows]}


@router.post("/connector-policy", status_code=201)
async def upsert_connector_policy(body: ConnectorPolicyBody,
                                  current_user: dict = Depends(require_admin)):
    """Create or update a rule (upsert on department+connector+tool)."""
    if not body.connector.strip():
        raise HTTPException(400, detail="connector is required")
    params = {
        "dept": (body.department or "").strip(),
        "connector": body.connector.strip(),
        "tool": (body.tool or "*").strip() or "*",
        "allow": bool(body.allow),
        "by": current_user.get("email") or current_user["sub"],
    }
    with engine.begin() as conn:
        # Store '' (not NULL) for org-wide so ON CONFLICT can dedupe — Postgres
        # treats NULLs as distinct in unique constraints. The read side
        # (_load_org_policy) already matches `department IS NULL OR department=''`.
        conn.execute(text("""
            INSERT INTO ainxt.cowork_connector_policy (department, connector, tool, allow, created_by)
            VALUES (:dept, :connector, :tool, :allow, :by)
            ON CONFLICT (department, connector, tool) DO UPDATE
              SET allow = EXCLUDED.allow, updated_at = NOW()
        """), params)
    logger.info(f"cowork_policy: rule {params['connector']}/{params['tool']} "
                f"allow={params['allow']} dept='{params['dept']}' by {current_user.get('email')}")
    return {"ok": True}


@router.delete("/connector-policy/{rule_id}")
async def delete_connector_policy(rule_id: str, current_user: dict = Depends(require_admin)):
    with engine.begin() as conn:
        res = conn.execute(text("DELETE FROM ainxt.cowork_connector_policy WHERE id = :id"),
                           {"id": rule_id})
    if not res.rowcount:
        raise HTTPException(404, detail="Rule not found")
    return {"deleted": True}


# ── Role → connector grants (a role's connector allowlist) ────────────────────
class RoleGrantBody(BaseModel):
    role: str
    connector_name: str


@router.get("/role-grants")
async def list_role_grants(current_user: dict = Depends(require_admin)):
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, role, connector_name, COALESCE(created_by,'') AS created_by, created_at "
            "FROM ainxt.role_connector_grants ORDER BY role, connector_name")).mappings().all()
    return {"grants": [dict(r) for r in rows]}


@router.post("/role-grants", status_code=201)
async def add_role_grant(body: RoleGrantBody, current_user: dict = Depends(require_admin)):
    if not body.role.strip() or not body.connector_name.strip():
        raise HTTPException(400, detail="role and connector_name are required")
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO ainxt.role_connector_grants (role, connector_name, created_by)
            VALUES (:role, :connector, :by)
            ON CONFLICT (role, connector_name) DO NOTHING
        """), {"role": body.role.strip(), "connector": body.connector_name.strip(),
               "by": current_user.get("email") or current_user["sub"]})
    return {"ok": True}


@router.delete("/role-grants/{grant_id}")
async def delete_role_grant(grant_id: str, current_user: dict = Depends(require_admin)):
    with engine.begin() as conn:
        res = conn.execute(text("DELETE FROM ainxt.role_connector_grants WHERE id = :id"),
                           {"id": grant_id})
    if not res.rowcount:
        raise HTTPException(404, detail="Grant not found")
    return {"deleted": True}
