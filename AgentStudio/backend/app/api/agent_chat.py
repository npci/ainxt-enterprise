# SPDX-License-Identifier: MIT
"""Agent chat history endpoints.

Mirrors app/api/chat.py but scoped per (agent_id, owner_user_id) so each user
sees only their own conversations with a given deployed agent.

The store singleton is created here and started/stopped from app/main.py's
lifespan. /agent-runner/chat (in app/api/factories.py) imports `get_store()`
to persist messages after each successful run.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.models import AuthenticatedUser
from app.api.deps import require_access
from app.core.config import postgres_enabled
from app.checkpoint import (
    AgentChatStore, FileAgentChatStore, PostgresAgentChatStore,
)

router = APIRouter()
from core.logger import logger
# Lifecycle managed by app.main._lifespan
_store: Optional[AgentChatStore] = None


def get_store() -> AgentChatStore:
    if _store is None:
        raise RuntimeError("agent chat store not initialised — startup() not called")
    return _store


async def startup() -> None:
    global _store
    _store = PostgresAgentChatStore() if postgres_enabled() else FileAgentChatStore()
    await _store.startup()
    logger.info(f'[AGENT] Agent chat store ready (backend={_store.__class__.__name__})')


async def shutdown() -> None:
    global _store
    if _store:
        await _store.shutdown()
        _store = None


@router.get("/agent-chat-threads/{agent_id}")
async def list_agent_chat_threads(
    agent_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    try:
        summaries = await get_store().list_threads(agent_id, current_user.id)
        return {
            "agent_id": agent_id,
            "threads": [
                {
                    "thread_id":            s.thread_id,
                    "title":                s.title,
                    "last_message_preview": s.last_message_preview,
                    "last_updated":         s.last_updated,
                    "message_count":        s.message_count,
                }
                for s in summaries
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agent-chat-history/{thread_id}")
async def get_agent_chat_history(
    thread_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    try:
        msgs = await get_store().load_messages(thread_id, current_user.id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if msgs is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    def _msg_out(m):
        out = {"role": m.role, "content": m.content}
        # Surface persisted download chips so the agent-runner UI can
        # re-render them on thread reload (mapHistoryToUiMessages reads
        # ``generated_files``). Only emit when present to keep the payload
        # lean for plain text turns.
        if getattr(m, "generated_files", None):
            out["generated_files"] = m.generated_files
        # Surface persisted usage so the usage footer re-renders on reload.
        if getattr(m, "usage", None):
            out["usage"] = m.usage
        return out

    return {
        "thread_id": thread_id,
        "messages":  [_msg_out(m) for m in msgs],
    }


@router.delete("/agent-chat-threads/{thread_id}", status_code=204)
async def delete_agent_chat_thread(
    thread_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    try:
        deleted = await get_store().delete_thread(thread_id, current_user.id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Thread not found")
