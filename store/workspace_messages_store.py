# SPDX-License-Identifier: Apache-2.0
# ============================================================
# WORKSPACE MESSAGES STORE — server-side project chat history
# Option B: dedicated workspace_messages partitioned table
# (HASH(project_id), 128 partitions)
# ============================================================

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from core.logger import logger
from db.database import SessionLocal
from db.models import WorkspaceMessage


def _now():
    return datetime.now(timezone.utc)


def _msg_to_dict(m: WorkspaceMessage) -> dict:
    """
    Serialize a WorkspaceMessage row to the shape the frontend expects.
    Matches the localStorage message shape used in Projects.jsx:
      { id, role, content, streaming, modelLabel, costUsd, latency, inTok, outTok, created_at }
    """
    return {
        "id":         m.id,
        "role":       m.role,
        "content":    m.content,
        "streaming":  False,   # persisted messages are never streaming
        "modelLabel": m.model_label,
        "costUsd":    m.cost_usd,
        "latency":    m.latency,
        "inTok":      m.in_tok,
        "outTok":     m.out_tok,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def save_messages(
    project_id: str,
    user_id: str,
    messages: List[dict],
) -> None:
    """
    Persist a list of messages (user + assistant) for a project/user pair.

    Each dict must have at minimum: role, content.
    Optional fields: modelLabel, costUsd, latency, inTok, outTok.

    Called fire-and-forget from a background thread after the SSE stream ends.
    Errors are logged but never raised — must not affect the streaming response.
    """
    if not messages:
        return
    db = SessionLocal()
    try:
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not role or not content:
                continue
            # Only persist user and assistant messages
            if role not in ("user", "assistant"):
                continue
            row = WorkspaceMessage(
                id=str(uuid.uuid4()),
                project_id=project_id,
                user_id=user_id,
                role=role,
                content=content,
                model_label=msg.get("modelLabel") or msg.get("model_label"),
                cost_usd=msg.get("costUsd") or msg.get("cost_usd"),
                latency=msg.get("latency"),
                in_tok=msg.get("inTok") or msg.get("in_tok"),
                out_tok=msg.get("outTok") or msg.get("out_tok"),
                created_at=_now(),
            )
            db.add(row)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(f"WorkspaceMessagesStore.save_messages: {exc}")
    finally:
        db.close()


def get_messages(
    project_id: str,
    user_id: str,
    limit: int = 60,
) -> List[dict]:
    """
    Fetch the most recent `limit` messages for a project/user pair,
    ordered oldest-first (for display in the chat panel).

    User isolation: filters by BOTH project_id AND user_id so users
    never see each other's messages within the same project.
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(WorkspaceMessage)
            .filter(
                WorkspaceMessage.project_id == project_id,
                WorkspaceMessage.user_id == user_id,
            )
            .order_by(WorkspaceMessage.created_at.desc())
            .limit(limit)
            .all()
        )
        # Reverse so oldest message is first (chronological order for display)
        return [_msg_to_dict(m) for m in reversed(rows)]
    except Exception as exc:
        logger.error(f"WorkspaceMessagesStore.get_messages: {exc}")
        return []
    finally:
        db.close()


def get_history_for_injection(
    project_id: str,
    user_id: str,
    limit: int = 6,
) -> List[dict]:
    """
    Fetch the most recent `limit` messages for history injection into the LLM prompt.
    Returns plain dicts with 'role' and 'content' keys — ready for LangChain message
    construction (HumanMessage / AIMessage).

    Ordered oldest-first so the conversation reads naturally in the prompt.
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(WorkspaceMessage)
            .filter(
                WorkspaceMessage.project_id == project_id,
                WorkspaceMessage.user_id == user_id,
            )
            .order_by(WorkspaceMessage.created_at.desc())
            .limit(limit)
            .all()
        )
        return [{"role": m.role, "content": m.content} for m in reversed(rows)]
    except Exception as exc:
        logger.error(f"WorkspaceMessagesStore.get_history_for_injection: {exc}")
        return []
    finally:
        db.close()


def delete_project_messages(project_id: str, user_id: Optional[str] = None) -> int:
    """
    Delete messages for a project.
    If user_id is provided, only that user's messages are deleted.
    If user_id is None (admin/cleanup), all messages for the project are deleted.

    Returns the number of rows deleted.
    """
    db = SessionLocal()
    try:
        q = db.query(WorkspaceMessage).filter(
            WorkspaceMessage.project_id == project_id
        )
        if user_id:
            q = q.filter(WorkspaceMessage.user_id == user_id)
        count = q.delete(synchronize_session=False)
        db.commit()
        return count
    except Exception as exc:
        db.rollback()
        logger.error(f"WorkspaceMessagesStore.delete_project_messages: {exc}")
        return 0
    finally:
        db.close()
