# SPDX-License-Identifier: Apache-2.0
"""
Cowork Projects — server-persisted (Postgres), per-user.

Projects were renderer localStorage (not durable, not multi-device, and schedules
couldn't reference them). They now live in `cowork_projects`, scoped to the JWT
`sub`. A project bundles standing instructions + persistent memory + an optional
document folder; scheduled tasks reference a project so a user can see all
schedules for it.

  GET    /cowork/projects            — my projects
  POST   /cowork/projects            — create
  PUT    /cowork/projects/{id}       — update
  DELETE /cowork/projects/{id}       — delete (its schedules are unlinked, not deleted)
"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.dependencies import get_current_user
from core.logger import logger

router = APIRouter(prefix="/buddy", tags=["buddy"])


def _db():
    from db.database import engine
    from sqlalchemy import text
    return engine, text


class ProjectBody(BaseModel):
    name: str
    instructions: str = ""
    memory: str = ""
    folder: Optional[str] = None


def _row(r) -> dict:
    return {
        "id": r[0], "name": r[1], "instructions": r[2] or "", "memory": r[3] or "",
        "folder": r[4], "created_at": str(r[5]) if r[5] else None,
        "updated_at": str(r[6]) if r[6] else None,
    }


_COLS = "id, name, instructions, memory, folder, created_at, updated_at"


@router.get("/projects")
async def list_projects(current_user: dict = Depends(get_current_user)):
    engine, text = _db()
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT {_COLS} FROM cowork_projects WHERE user_id = :uid ORDER BY updated_at DESC"
        ), {"uid": current_user["sub"]}).fetchall()
    return {"projects": [_row(r) for r in rows]}


@router.post("/projects", status_code=201)
async def create_project(body: ProjectBody, current_user: dict = Depends(get_current_user)):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, detail="name is required")
    engine, text = _db()
    pid = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO cowork_projects (id, user_id, name, instructions, memory, folder, department)
            VALUES (:id, :uid, :name, :instr, :mem, :folder, :dept)
        """), {
            "id": pid, "uid": current_user["sub"], "name": name,
            "instr": body.instructions or "", "mem": body.memory or "",
            "folder": body.folder or None, "dept": current_user.get("department") or None,
        })
    logger.info(f"cowork_projects: created {pid} for {current_user['sub']}")
    return {"id": pid, "name": name}


@router.put("/projects/{project_id}")
async def update_project(project_id: str, body: ProjectBody, current_user: dict = Depends(get_current_user)):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, detail="name is required")
    engine, text = _db()
    with engine.begin() as conn:
        res = conn.execute(text("""
            UPDATE cowork_projects
               SET name = :name, instructions = :instr, memory = :mem, folder = :folder, updated_at = NOW()
             WHERE id = :id AND user_id = :uid
        """), {
            "name": name, "instr": body.instructions or "", "mem": body.memory or "",
            "folder": body.folder or None, "id": project_id, "uid": current_user["sub"],
        })
    if (res.rowcount or 0) == 0:
        raise HTTPException(404, detail="Project not found")
    return {"id": project_id, "updated": True}


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, current_user: dict = Depends(get_current_user)):
    engine, text = _db()
    with engine.begin() as conn:
        # Unlink (don't delete) this project's schedules so they aren't orphaned/lost.
        conn.execute(text("UPDATE cowork_scheduled_tasks SET project_id = NULL WHERE project_id = :id AND user_id = :uid"),
                     {"id": project_id, "uid": current_user["sub"]})
        res = conn.execute(text("DELETE FROM cowork_projects WHERE id = :id AND user_id = :uid"),
                           {"id": project_id, "uid": current_user["sub"]})
    if (res.rowcount or 0) == 0:
        raise HTTPException(404, detail="Project not found")
    return {"deleted": True}
