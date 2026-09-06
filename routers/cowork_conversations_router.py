# SPDX-License-Identifier: MIT
"""
Cowork Conversations — server-persisted chat history (Postgres), per-user.

This was the last Cowork state living in renderer localStorage. It now lives in
`cowork_conversations`, scoped to the JWT `sub`, optionally linked to a project —
so history is durable, multi-device, and project-scoped. (No work in localStorage;
everything in the DB.)

  GET    /cowork/conversations[?project_id=]   — list (metadata only, no messages)
  GET    /cowork/conversations/{id}            — one conversation WITH messages
  PUT    /cowork/conversations/{id}            — upsert (save title + messages + links)
  DELETE /cowork/conversations/{id}            — delete

Messages are the renderer's message-block array, stored as JSONB verbatim.
"""
import json
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.dependencies import get_current_user
from core.logger import logger

router = APIRouter(prefix="/buddy", tags=["buddy"])

_MAX_MESSAGES_BYTES = 4_000_000   # ~4 MB cap per conversation (guardrail)


def _db():
    from db.database import engine
    from sqlalchemy import text
    return engine, text


class ConvUpsert(BaseModel):
    title:      str                 = "Conversation"
    messages:   List[Any]           = []
    project_id: Optional[str]       = None
    folder:     Optional[str]       = None
    # Agent session_id used to --resume the Buddy session so an in-progress task
    # continues across navigation / app restart (never wiped by a save that omits it).
    resume_id:  Optional[str]       = None


@router.get("/conversations")
async def list_conversations(project_id: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    """List the caller's conversations (newest first), metadata only — NOT the
    message bodies (keeps the sidebar list fast). Optional project filter."""
    engine, text = _db()
    clause, params = "", {"uid": current_user["sub"]}
    if project_id == "none":
        clause = " AND project_id IS NULL"
    elif project_id:
        clause = " AND project_id = :pid"
        params["pid"] = project_id
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT id, title, project_id, folder, created_at, updated_at
            FROM cowork_conversations
            WHERE user_id = :uid{clause}
            ORDER BY updated_at DESC
        """), params).fetchall()
    return {"conversations": [{
        "id": r[0], "title": r[1], "project_id": r[2], "folder": r[3],
        "created_at": str(r[4]) if r[4] else None, "updated_at": str(r[5]) if r[5] else None,
    } for r in rows]}


@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str, current_user: dict = Depends(get_current_user)):
    """Fetch a single conversation WITH its messages."""
    engine, text = _db()
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT id, title, project_id, folder, messages, resume_id
            FROM cowork_conversations WHERE id = :id AND user_id = :uid
        """), {"id": conv_id, "uid": current_user["sub"]}).fetchone()
    if not r:
        raise HTTPException(404, detail="Conversation not found")
    msgs = r[4]
    if isinstance(msgs, str):
        try: msgs = json.loads(msgs)
        except Exception: msgs = []
    return {"id": r[0], "title": r[1], "project_id": r[2], "folder": r[3],
            "messages": msgs or [], "resume_id": r[5]}


@router.put("/conversations/{conv_id}")
async def upsert_conversation(conv_id: str, body: ConvUpsert, current_user: dict = Depends(get_current_user)):
    """Create or update a conversation (save messages). Idempotent by id."""
    msgs_json = json.dumps(body.messages or [], default=str)
    if len(msgs_json) > _MAX_MESSAGES_BYTES:
        raise HTTPException(413, detail="Conversation too large to save")
    engine, text = _db()
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO cowork_conversations (id, user_id, project_id, folder, title, messages, resume_id, created_at, updated_at)
                VALUES (:id, :uid, :pid, :folder, :title, CAST(:messages AS jsonb), :resume_id, NOW(), NOW())
                ON CONFLICT (id, user_id) DO UPDATE SET
                    title      = EXCLUDED.title,
                    messages   = EXCLUDED.messages,
                    project_id = EXCLUDED.project_id,
                    folder     = EXCLUDED.folder,
                    -- Never wipe a good resume_id with a save that omits it.
                    resume_id  = COALESCE(EXCLUDED.resume_id, cowork_conversations.resume_id),
                    updated_at = NOW()
            """), {
                "id": conv_id, "uid": current_user["sub"], "pid": body.project_id or None,
                "folder": body.folder or None, "title": (body.title or "Conversation")[:200],
                "messages": msgs_json, "resume_id": body.resume_id or None,
            })
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))
    return {"id": conv_id, "saved": True}


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str, current_user: dict = Depends(get_current_user)):
    engine, text = _db()
    with engine.begin() as conn:
        res = conn.execute(text("DELETE FROM cowork_conversations WHERE id = :id AND user_id = :uid"),
                           {"id": conv_id, "uid": current_user["sub"]})
    return {"deleted": (res.rowcount or 0) > 0}
