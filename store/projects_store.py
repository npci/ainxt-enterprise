# SPDX-License-Identifier: MIT
# ============================================================
# PROJECTS STORE — Postgres (projects_pg table)
# Source of truth: DB. Redis was removed — it was volatile and
# caused projects to silently disappear on Redis restart.
# ============================================================

import uuid
from datetime import datetime
from typing import Optional, List

from core.logger import logger


def _row_to_dict(p) -> dict:
    return {
        "id":                  str(p.id),
        "name":                p.name,
        "description":         p.description or "",
        "repo_name":           p.repo_name or "",
        "default_branch":      p.default_branch or "",
        "team":                p.team or [],
        "custom_instructions": p.custom_instructions or "",
        "tags":                p.tags or [],
        "department":          p.department or "",
        "created_by":          str(p.owner_id) if p.owner_id else "",
        "created_at":          p.created_at.timestamp() if p.created_at else 0,
        "updated_at":          p.updated_at.timestamp() if p.updated_at else 0,
    }


def create_project(data: dict) -> dict:
    from db.database import SessionLocal
    from db.models import ProjectRecord

    db = SessionLocal()
    try:
        owner_id = data.get("created_by") or None
        project = ProjectRecord(
            id=str(uuid.uuid4()),
            name=data.get("name", ""),
            description=data.get("description") or None,
            repo_name=data.get("repo_name") or None,
            default_branch=data.get("default_branch") or None,
            team=data.get("team") or [],
            custom_instructions=data.get("custom_instructions") or None,
            tags=data.get("tags") or [],
            department=data.get("department") or None,
            owner_id=owner_id or None,
        )
        db.add(project)
        db.commit()
        db.refresh(project)
        return _row_to_dict(project)
    except Exception as e:
        db.rollback()
        logger.error(f"projects_store.create_project failed: {e}")
        raise
    finally:
        db.close()


def get_project(project_id: str) -> Optional[dict]:
    from db.database import SessionLocal
    from db.models import ProjectRecord

    db = SessionLocal()
    try:
        p = db.query(ProjectRecord).filter(ProjectRecord.id == project_id).first()
        return _row_to_dict(p) if p else None
    except Exception as e:
        logger.error(f"projects_store.get_project failed: {e}")
        return None
    finally:
        db.close()


def get_project_by_name(name: str) -> Optional[dict]:
    from db.database import SessionLocal
    from db.models import ProjectRecord

    db = SessionLocal()
    try:
        p = db.query(ProjectRecord).filter(
            ProjectRecord.name == name
        ).first()
        return _row_to_dict(p) if p else None
    except Exception as e:
        logger.error(f"projects_store.get_project_by_name failed: {e}")
        return None
    finally:
        db.close()


def list_projects(limit: int = 50, user_id: str = "", is_admin: bool = False, department: str = "") -> List[dict]:
    from db.database import SessionLocal
    from db.models import ProjectRecord
    from sqlalchemy import or_

    db = SessionLocal()
    try:
        q = db.query(ProjectRecord)

        if not is_admin:
            if user_id:
                # User sees only their own projects
                q = q.filter(ProjectRecord.owner_id == user_id)
            else:
                return []

            # Dept scoping: filter to user's dept or projects with no dept set
            if department:
                q = q.filter(
                    or_(
                        ProjectRecord.department == None,
                        ProjectRecord.department == "",
                        ProjectRecord.department == department,
                    )
                )

        rows = q.order_by(ProjectRecord.updated_at.desc()).limit(limit).all()
        return [_row_to_dict(p) for p in rows]
    except Exception as e:
        logger.error(f"projects_store.list_projects failed: {e}")
        return []
    finally:
        db.close()


def update_project(project_id: str, data: dict) -> Optional[dict]:
    from db.database import SessionLocal
    from db.models import ProjectRecord

    db = SessionLocal()
    try:
        p = db.query(ProjectRecord).filter(ProjectRecord.id == project_id).first()
        if not p:
            return None
        for field in ("name", "description", "repo_name", "default_branch", "team", "custom_instructions", "tags"):
            if field in data and data[field] is not None:
                setattr(p, field, data[field])
        p.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(p)
        return _row_to_dict(p)
    except Exception as e:
        db.rollback()
        logger.error(f"projects_store.update_project failed: {e}")
        return None
    finally:
        db.close()


def delete_project(project_id: str) -> bool:
    from db.database import SessionLocal
    from db.models import ProjectRecord

    db = SessionLocal()
    try:
        p = db.query(ProjectRecord).filter(ProjectRecord.id == project_id).first()
        if not p:
            return False
        db.delete(p)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"projects_store.delete_project failed: {e}")
        return False
    finally:
        db.close()
