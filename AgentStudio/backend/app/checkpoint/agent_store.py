# SPDX-License-Identifier: MIT
"""
Agent chat history store — analogous to checkpoint.store/postgres_store, but
scoped to (agent_id, owner_user_id) rather than workflow_id.

Used by the Agent Builder's runner chat (POST /agent-runner/chat) to give
each user a private list of past conversations per deployed agent.

Two backends, picked by app/main.py based on POSTGRES_HOST:
  - PostgresAgentChatStore — table ``agent_chat_threads`` (created on startup)
  - FileAgentChatStore      — single JSON file at backend/data/agent_chat_history.json

Reuses ChatMessage / ThreadSummary from .store so the API layer can share types.
"""

from __future__ import annotations

import asyncio
import json

import os
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .store import ChatMessage, ThreadSummary, summarise_thread

from core.logger import logger
def _serialise_message(m: ChatMessage) -> dict:
    """Mirror checkpoint.store's serializer so agent-runner download chips
    survive a thread reload. ``generated_files`` is only emitted when present
    so existing rows stay bit-identical and the JSON doesn't grow null fields.
    """
    payload: dict = {"role": m.role, "content": m.content}
    if getattr(m, "generated_files", None):
        payload["generated_files"] = m.generated_files
    return payload


def _deserialise_message(m: dict) -> ChatMessage:
    return ChatMessage(
        role=m["role"],
        content=m["content"],
        generated_files=m.get("generated_files") or None,
    )


def _serialise_message(m: ChatMessage) -> dict:
    """Mirror checkpoint.store's serializer so agent-runner download chips
    survive a thread reload. ``generated_files`` is only emitted when present
    so existing rows stay bit-identical and the JSON doesn't grow null fields.
    """
    payload: dict = {"role": m.role, "content": m.content}
    if getattr(m, "generated_files", None):
        payload["generated_files"] = m.generated_files
    if getattr(m, "usage", None):
        payload["usage"] = m.usage
    return payload


def _deserialise_message(m: dict) -> ChatMessage:
    return ChatMessage(
        role=m["role"],
        content=m["content"],
        generated_files=m.get("generated_files") or None,
        usage=m.get("usage") or None,
    )


class AgentChatStore(ABC):

    @abstractmethod
    async def startup(self) -> None: ...

    @abstractmethod
    async def shutdown(self) -> None: ...

    @abstractmethod
    async def save_messages(
        self, thread_id: str, agent_id: str, owner_user_id: str,
        messages: List[ChatMessage],
    ) -> None: ...

    @abstractmethod
    async def load_messages(
        self, thread_id: str, owner_user_id: str,
    ) -> Optional[List[ChatMessage]]:
        """Return messages, or None if the thread doesn't exist for this user."""

    @abstractmethod
    async def list_threads(
        self, agent_id: str, owner_user_id: str,
    ) -> List[ThreadSummary]: ...

    @abstractmethod
    async def delete_thread(self, thread_id: str, owner_user_id: str) -> bool:
        """Return True if a row was deleted, False if not found / not owned."""

    @abstractmethod
    async def delete_threads_for_agent(self, agent_id: str, owner_user_id: str) -> int:
        """Return the number of owner-scoped threads deleted for an agent."""


_DEFAULT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "agent_chat_history.json")
)


class FileAgentChatStore(AgentChatStore):
    """
    JSON file store.
    Schema: { thread_id: { agent_id, owner_user_id, messages: [...], last_updated } }
    """

    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: Dict[str, dict] = {}

    async def startup(self) -> None:
        await asyncio.to_thread(self._load)

    async def shutdown(self) -> None:
        pass

    def _load(self) -> None:
        if not os.path.exists(self._path):
            self._data = {}
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            logger.info(f'[AGENT] FileAgentChatStore loaded {len(self._data)} threads from {self._path}')
        except Exception as e:
            logger.warning(f'[AGENT] FileAgentChatStore could not load {self._path}: {e}')
            self._data = {}

    def _flush(self) -> None:
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self._path), exist_ok=True)
                tmp = self._path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2, default=str)
                os.replace(tmp, self._path)
            except Exception as e:
                logger.warning(f'[AGENT] FileAgentChatStore flush failed: {e}')

    async def save_messages(
        self, thread_id: str, agent_id: str, owner_user_id: str,
        messages: List[ChatMessage],
    ) -> None:
        self._data[thread_id] = {
            "agent_id":      agent_id,
            "owner_user_id": owner_user_id,
            "messages":      [_serialise_message(m) for m in messages],
            "last_updated":  datetime.now(timezone.utc).isoformat(),
        }
        await asyncio.to_thread(self._flush)

    async def load_messages(
        self, thread_id: str, owner_user_id: str,
    ) -> Optional[List[ChatMessage]]:
        record = self._data.get(thread_id)
        if not record or record.get("owner_user_id") != owner_user_id:
            return None
        return [_deserialise_message(m) for m in record.get("messages", [])]

    async def list_threads(
        self, agent_id: str, owner_user_id: str,
    ) -> List[ThreadSummary]:
        summaries = []
        for tid, record in self._data.items():
            if record.get("agent_id") != agent_id:
                continue
            if record.get("owner_user_id") != owner_user_id:
                continue
            summaries.append(summarise_thread(
                tid, record.get("messages", []), record.get("last_updated"),
            ))
        summaries.sort(key=lambda t: t.last_updated or "", reverse=True)
        return summaries

    async def delete_thread(self, thread_id: str, owner_user_id: str) -> bool:
        record = self._data.get(thread_id)
        if not record or record.get("owner_user_id") != owner_user_id:
            return False
        self._data.pop(thread_id, None)
        await asyncio.to_thread(self._flush)
        return True

    async def delete_threads_for_agent(self, agent_id: str, owner_user_id: str) -> int:
        thread_ids = [
            tid for tid, record in self._data.items()
            if record.get("agent_id") == agent_id and record.get("owner_user_id") == owner_user_id
        ]
        for tid in thread_ids:
            self._data.pop(tid, None)
        if thread_ids:
            await asyncio.to_thread(self._flush)
        return len(thread_ids)


class PostgresAgentChatStore(AgentChatStore):
    """
    Stores agent chat history in ``agent_chat_threads``:

        CREATE TABLE agent_chat_threads (
            thread_id     TEXT PRIMARY KEY,
            agent_id      TEXT NOT NULL,
            owner_user_id TEXT NOT NULL,
            messages      JSONB NOT NULL DEFAULT '[]',
            last_updated  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """

    def __init__(self, uri: str = "") -> None:
        # ``uri`` accepted for backwards compatibility but ignored: the store
        # borrows from the shared platform pool, not a per-store connection.
        self._pool = None

    async def startup(self) -> None:
        from app.core.config import postgres_enabled
        if not postgres_enabled():
            logger.warning('[AGENT] PostgresAgentChatStore: POSTGRES_HOST not set — store disabled')
            return
        await asyncio.to_thread(self._init_pool)

    def _init_pool(self) -> None:
        # Reuse the platform's single shared connection pool instead of opening
        # a separate psycopg pool. Pool sizing lives in db/database.py; the
        # legacy AGENT_CHAT_PG_POOL_* env vars are no longer used.
        from app.core.db_pool import SHARED_POOL
        self._pool = SHARED_POOL
        with self._pool.connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_chat_threads (
                    thread_id     TEXT PRIMARY KEY,
                    agent_id      TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    messages      JSONB NOT NULL DEFAULT '[]',
                    last_updated  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_agent_chat_threads_agent_owner
                ON agent_chat_threads (agent_id, owner_user_id, last_updated DESC)
            """)
            conn.commit()
        logger.info('[AGENT] PostgresAgentChatStore: table ready')

    async def shutdown(self) -> None:
        # ``_pool`` is the shared platform pool (owned by db.database.engine);
        # it must outlive ABStudio, so only drop the reference — never close it.
        self._pool = None

    def _require_pool(self):
        if not self._pool:
            raise RuntimeError(
                "PostgresAgentChatStore not initialised — check POSTGRES_HOST"
            )
        return self._pool

    async def save_messages(
        self, thread_id: str, agent_id: str, owner_user_id: str,
        messages: List[ChatMessage],
    ) -> None:
        now = datetime.now(timezone.utc)
        msgs_json = json.dumps([_serialise_message(m) for m in messages])

        def _run():
            with self._require_pool().connection() as conn:
                conn.execute("""
                    INSERT INTO agent_chat_threads
                        (thread_id, agent_id, owner_user_id, messages, last_updated)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (thread_id) DO UPDATE
                        SET messages     = EXCLUDED.messages,
                            last_updated = EXCLUDED.last_updated
                """, (thread_id, agent_id, owner_user_id, msgs_json, now))
                conn.commit()
        await asyncio.to_thread(_run)

    async def load_messages(
        self, thread_id: str, owner_user_id: str,
    ) -> Optional[List[ChatMessage]]:
        def _run():
            with self._require_pool().connection() as conn:
                row = conn.execute(
                    "SELECT messages FROM agent_chat_threads "
                    "WHERE thread_id = %s AND owner_user_id = %s",
                    (thread_id, owner_user_id),
                ).fetchone()
            return row
        row = await asyncio.to_thread(_run)
        if not row:
            return None
        return [_deserialise_message(r) for r in (row[0] or [])]

    async def list_threads(
        self, agent_id: str, owner_user_id: str,
    ) -> List[ThreadSummary]:
        def _run():
            with self._require_pool().connection() as conn:
                return conn.execute(
                    "SELECT thread_id, messages, last_updated FROM agent_chat_threads "
                    "WHERE agent_id = %s AND owner_user_id = %s "
                    "ORDER BY last_updated DESC",
                    (agent_id, owner_user_id),
                ).fetchall()
        rows = await asyncio.to_thread(_run)
        return [
            summarise_thread(tid, msgs or [], last_updated.isoformat() if last_updated else None)
            for tid, msgs, last_updated in (rows or [])
        ]

    async def delete_thread(self, thread_id: str, owner_user_id: str) -> bool:
        def _run():
            with self._require_pool().connection() as conn:
                cur = conn.execute(
                    "DELETE FROM agent_chat_threads "
                    "WHERE thread_id = %s AND owner_user_id = %s",
                    (thread_id, owner_user_id),
                )
                conn.commit()
                return cur.rowcount
        rowcount = await asyncio.to_thread(_run)
        return bool(rowcount)

    async def delete_threads_for_agent(self, agent_id: str, owner_user_id: str) -> int:
        def _run():
            with self._require_pool().connection() as conn:
                cur = conn.execute(
                    "DELETE FROM agent_chat_threads "
                    "WHERE agent_id = %s AND owner_user_id = %s",
                    (agent_id, owner_user_id),
                )
                conn.commit()
                return cur.rowcount
        rowcount = await asyncio.to_thread(_run)
        return int(rowcount or 0)
