# SPDX-License-Identifier: MIT
# ============================================================
# THREADS STORE — Postgres (threads_pg + thread_messages)
# ============================================================

import uuid
from datetime import datetime
from typing import Optional, List

from core.logger import logger
from db.database import SessionLocal
from db.models import Thread, ThreadMessage


def _row_to_dict(t: Thread) -> dict:
    return {
        "id":          t.id,
        "title":       t.title,
        "description": t.description or "",
        "project_id":  t.project_id or "",
        "product_id":  str(t.product_id) if t.product_id else "",
        "repo":        t.repo or "",
        "created_by":  t.created_by or "user",
        "labels":      t.labels or [],
        "priority":    t.priority or "Medium",
        "status":      t.status or "open",
        "department":  t.department or "",
        "created_at":  t.created_at.timestamp() if t.created_at else 0,
        "updated_at":  t.updated_at.timestamp() if getattr(t, "updated_at", None) else 0,
    }


def _msg_to_dict(m: ThreadMessage) -> dict:
    return {
        "id":               m.id,
        "thread_id":        m.thread_id,
        "parent_message_id": str(m.parent_message_id) if getattr(m, "parent_message_id", None) else None,
        "content":          m.content,
        "author":           m.author,
        "author_name":      getattr(m, "author_name", None) or m.author,
        "author_band":      getattr(m, "author_band", None) or "",
        "message_type":     getattr(m, "message_type", None) or "text",
        "hitl_status":      getattr(m, "hitl_status", None),
        "ainxt_run_id":     getattr(m, "ainxt_run_id", None),
        "reactions":        getattr(m, "reactions", None) or {},
        "mentions":         m.mentions or [],
        "created_at":       m.created_at.timestamp() if m.created_at else 0,
        "model_used":       m.model_used,
        "tokens_in":        m.tokens_in,
        "tokens_out":       m.tokens_out,
        "cost_usd":         m.cost_usd,
        "latency_ms":       m.latency_ms,
    }


def create_thread(data: dict) -> dict:
    db = SessionLocal()
    try:
        product_id = data.get("product_id") or None
        # Coerce empty string to None for FK
        if product_id == "":
            product_id = None
        t = Thread(
            id=str(uuid.uuid4()),
            title=data.get("title", ""),
            description=data.get("description", ""),
            project_id=data.get("project_id", ""),
            repo=data.get("repo", ""),
            product_id=product_id,
            created_by=data.get("created_by", "user"),
            labels=data.get("labels", []),
            priority=data.get("priority", "Medium"),
            status="open",
            department=data.get("department") or None,
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        return _row_to_dict(t)
    except Exception as e:
        db.rollback()
        logger.error(f"ThreadsStore.create_thread: {e}")
        raise
    finally:
        db.close()


def get_thread(thread_id: str) -> Optional[dict]:
    db = SessionLocal()
    try:
        t = db.query(Thread).filter(Thread.id == thread_id).first()
        return _row_to_dict(t) if t else None
    finally:
        db.close()


def list_threads(
    project_id: Optional[str] = None,
    product_id: Optional[str] = None,
    status: Optional[str] = None,
    label: Optional[str] = None,
    limit: int = 50,
    department: Optional[str] = None,
    accessible_product_ids: Optional[List[str]] = None,
) -> List[dict]:
    """
    Visibility rules (Slack-style collaboration):
    - admin: sees all threads
    - non-admin with product filter: sees threads for that product (if they have access)
    - non-admin without filter: sees all threads whose product_id is in their accessible products
      OR threads with no product_id but in their department
      → this makes Threads a true collaboration space: all teammates see the same threads
    """
    db = SessionLocal()
    try:
        from sqlalchemy import or_, and_
        q = db.query(Thread).order_by(Thread.created_at.desc())
        if status:
            q = q.filter(Thread.status == status)
        if project_id:
            q = q.filter(Thread.project_id == project_id)
        if product_id:
            q = q.filter(Thread.product_id == product_id)
        elif accessible_product_ids is not None:
            # Show threads belonging to the user's accessible products,
            # plus unscoped threads (no product_id) from the user's own department
            dept_clause = (
                and_(Thread.product_id.is_(None),
                     or_(Thread.department == department,
                         Thread.department.is_(None),
                         Thread.department == ""))
                if department else Thread.product_id.is_(None)
            )
            if accessible_product_ids:
                q = q.filter(or_(
                    Thread.product_id.in_(accessible_product_ids),
                    dept_clause,
                ))
            else:
                q = q.filter(dept_clause)
        elif department:
            # Fallback: no product info available — scope by department
            q = q.filter(
                (Thread.department == department) | (Thread.department.is_(None)) | (Thread.department == "")
            )
        rows = q.limit(limit).all()
        results = []
        for t in rows:
            d = _row_to_dict(t)
            if label and label not in d.get("labels", []):
                continue
            results.append(d)
        return results
    finally:
        db.close()


def delete_thread(thread_id: str) -> bool:
    db = SessionLocal()
    try:
        t = db.query(Thread).filter(Thread.id == thread_id).first()
        if not t:
            return False
        db.delete(t)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"ThreadsStore.delete_thread: {e}")
        return False
    finally:
        db.close()


def add_message(thread_id: str, message: dict) -> dict:
    db = SessionLocal()
    try:
        parent_id = message.get("parent_message_id") or None
        if parent_id == "":
            parent_id = None
        m = ThreadMessage(
            id=str(uuid.uuid4()),
            thread_id=thread_id,
            content=message.get("content", ""),
            author=message.get("author", "user"),
            author_name=message.get("author_name") or None,
            author_band=message.get("author_band") or None,
            message_type=message.get("message_type") or "text",
            hitl_status=message.get("hitl_status") or None,
            ainxt_run_id=message.get("ainxt_run_id") or None,
            parent_message_id=parent_id,
            mentions=message.get("mentions", []),
            reactions={},
            model_used=message.get("model_used"),
            tokens_in=message.get("tokens_in"),
            tokens_out=message.get("tokens_out"),
            cost_usd=message.get("cost_usd"),
            latency_ms=message.get("latency_ms"),
        )
        db.add(m)
        # bump updated_at on parent thread
        t = db.query(Thread).filter(Thread.id == thread_id).first()
        if t:
            t.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(m)
        return _msg_to_dict(m)
    except Exception as e:
        db.rollback()
        logger.error(f"ThreadsStore.add_message: {e}")
        raise
    finally:
        db.close()


def get_messages(thread_id: str, limit: int = 200) -> List[dict]:
    db = SessionLocal()
    try:
        rows = (db.query(ThreadMessage)
                .filter(ThreadMessage.thread_id == thread_id)
                .order_by(ThreadMessage.created_at.asc())
                .limit(limit)
                .all())
        return [_msg_to_dict(m) for m in rows]
    finally:
        db.close()


def get_reply_counts(thread_id: str) -> dict:
    """Return {parent_message_id: reply_count} for a thread."""
    db = SessionLocal()
    try:
        from sqlalchemy import func
        rows = (db.query(ThreadMessage.parent_message_id, func.count(ThreadMessage.id))
                .filter(
                    ThreadMessage.thread_id == thread_id,
                    ThreadMessage.parent_message_id.isnot(None),
                )
                .group_by(ThreadMessage.parent_message_id)
                .all())
        return {str(r[0]): r[1] for r in rows}
    finally:
        db.close()


# Agent status is still transient — keep in a simple in-process dict
# (resets on restart, which is fine for @AiNxt background task tracking)
_agent_status: dict = {}


def set_agent_status(thread_id: str, status: str) -> None:
    _agent_status[thread_id] = status


def get_agent_status(thread_id: str) -> str:
    return _agent_status.get(thread_id, "idle")
