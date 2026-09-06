# SPDX-License-Identifier: MIT
"""
Code Conversations — server-persisted Code-tab task/session history (Postgres).

The Code tab's task sessions previously lived in renderer localStorage, which is
lost on app restart. They now live in `code_conversations`, scoped to the JWT
`sub` and the project FOLDER — durable + multi-device. Kept in a SEPARATE table
from `cowork_conversations` so Code task sessions never mix with Buddy chats.

  GET    /code/conversations                 — list (metadata only, all folders)
  GET    /code/conversations/{id}            — one session WITH messages
  PUT    /code/conversations/{id}            — upsert (save title + messages + folder)
  DELETE /code/conversations/{id}            — delete

Messages are the renderer's message-block array, stored as JSONB verbatim.
"""
import json
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.dependencies import get_current_user
from core.logger import logger

router = APIRouter(prefix="/code", tags=["code"])

_MAX_MESSAGES_BYTES = 4_000_000   # ~4 MB cap per session (guardrail)
_table_ready = False


def _db():
    from db.database import engine
    from sqlalchemy import text
    return engine, text


def _ensure_table():
    """Idempotently create the table. Runs once per process — keeps the Code tab
    working even on machines where db/migrate.py hasn't been run."""
    global _table_ready
    if _table_ready:
        return
    engine, text = _db()
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS code_conversations (
                    id          TEXT NOT NULL,
                    user_id     TEXT NOT NULL,
                    folder      TEXT,
                    title       TEXT,
                    messages    JSONB DEFAULT '[]'::jsonb,
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    updated_at  TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (id, user_id)
                )
            """))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_code_conv_user ON code_conversations (user_id, updated_at DESC)"
            ))
        _table_ready = True
    except Exception as exc:
        logger.error(f"code_conversations ensure-table failed: {exc}")


class ConvUpsert(BaseModel):
    title:    str           = "Conversation"
    messages: List[Any]     = []
    folder:   Optional[str] = None


@router.get("/conversations")
async def list_conversations(current_user: dict = Depends(get_current_user)):
    """List the caller's Code task sessions (newest first), metadata only."""
    _ensure_table()
    engine, text = _db()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, title, folder, created_at, updated_at
            FROM code_conversations
            WHERE user_id = :uid
            ORDER BY created_at DESC
        """), {"uid": current_user["sub"]}).fetchall()
    return {"conversations": [{
        "id": r[0], "title": r[1], "folder": r[2],
        "created_at": str(r[3]) if r[3] else None, "updated_at": str(r[4]) if r[4] else None,
    } for r in rows]}


@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str, current_user: dict = Depends(get_current_user)):
    """Fetch a single Code session WITH its messages."""
    _ensure_table()
    engine, text = _db()
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT id, title, folder, messages
            FROM code_conversations WHERE id = :id AND user_id = :uid
        """), {"id": conv_id, "uid": current_user["sub"]}).fetchone()
    if not r:
        raise HTTPException(404, detail="Conversation not found")
    msgs = r[3]
    if isinstance(msgs, str):
        try: msgs = json.loads(msgs)
        except Exception: msgs = []
    return {"id": r[0], "title": r[1], "folder": r[2], "messages": msgs or []}


@router.put("/conversations/{conv_id}")
async def upsert_conversation(conv_id: str, body: ConvUpsert, current_user: dict = Depends(get_current_user)):
    """Create or update a Code session (save messages). Idempotent by id."""
    _ensure_table()
    msgs_json = json.dumps(body.messages or [], default=str)
    if len(msgs_json) > _MAX_MESSAGES_BYTES:
        raise HTTPException(413, detail="Conversation too large to save")
    engine, text = _db()
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO code_conversations (id, user_id, folder, title, messages, created_at, updated_at)
                VALUES (:id, :uid, :folder, :title, CAST(:messages AS jsonb), NOW(), NOW())
                ON CONFLICT (id, user_id) DO UPDATE SET
                    title      = EXCLUDED.title,
                    messages   = EXCLUDED.messages,
                    folder     = EXCLUDED.folder,
                    updated_at = NOW()
            """), {
                "id": conv_id, "uid": current_user["sub"],
                "folder": body.folder or None, "title": (body.title or "Conversation")[:200],
                "messages": msgs_json,
            })
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))
    return {"id": conv_id, "saved": True}


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str, current_user: dict = Depends(get_current_user)):
    _ensure_table()
    engine, text = _db()
    with engine.begin() as conn:
        res = conn.execute(text("DELETE FROM code_conversations WHERE id = :id AND user_id = :uid"),
                           {"id": conv_id, "uid": current_user["sub"]})
    return {"deleted": (res.rowcount or 0) > 0}
