# SPDX-License-Identifier: MIT
# ============================================================
# REDIS MEMORY LAYER
# Fast in-memory storage for conversations, agent runs,
# and workflow history. Uses db=2 (db=1 is traces).
# ============================================================

import json
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any

from core.config import REDIS_HOST, REDIS_PORT, RDB_WORKFLOW
from core.kv import get_kv, KVError
from core.logger import logger


# ============================================================
# CONSTANTS
# ============================================================

REDIS_DB = RDB_WORKFLOW  # db=2 — db=1 is trace store

CONV_TTL = 7 * 24 * 3600   # 7 days
RUN_TTL  = 3 * 24 * 3600   # 3 days
WF_TTL   = 3 * 24 * 3600   # 3 days

# Log "RedisMemory connected" only once per process — not on every instantiation.
# Multiple code paths create RedisMemory() per request; the repeated log adds
# noise without value. The underlying get_kv() factory already pools connections.
_connected_logged: bool = False


# ============================================================
# REDIS MEMORY
# ============================================================

class RedisMemory:
    """
    KV-backed memory for conversations, agent runs,
    and workflow execution history.

    All data is JSON-serialised and stored with TTL. The underlying
    backend is selected per-DB via the
    REDIS_CLIENT_CONFIG_DB2 env var (defaults to REDIS).

    The host/port parameters are retained for API compatibility but
    are no longer used directly — connection details come from
    core.config and the KV factory.
    """

    def __init__(
        self,
        host: str = REDIS_HOST,    # kept for API compatibility
        port: int = REDIS_PORT,    # kept for API compatibility
        db: int = REDIS_DB,
    ):
        self.client = get_kv(db, decode_responses=True)
        self._check_connection()

    # --------------------------------------------------------
    # CONNECTION
    # --------------------------------------------------------

    def _check_connection(self) -> None:
        global _connected_logged
        try:
            self.client.ping()
            if not _connected_logged:
                logger.info(f"RedisMemory connected backend={self.client.backend}")
                _connected_logged = True
        except KVError as e:
            logger.error(f"RedisMemory connection failed: {e}")
            raise

    # ========================================================
    # CONVERSATIONS
    # ========================================================

    @staticmethod
    def _conv_key(session_id: str, user_id: Optional[str] = None) -> str:
        """Build a user-scoped conversation key.
        Format: conv:{user_id}:{session_id} when user_id is known,
                conv:{session_id}            for legacy/anonymous sessions.
        """
        if user_id:
            return f"conv:{user_id}:{session_id}"
        return f"conv:{session_id}"

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
    ) -> str:
        """Append a message to a conversation session."""
        entry = {
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "role": role,         # "user" | "assistant" | "system"
            "content": content,
            "timestamp": datetime.utcnow().isoformat(),
            "metadata": metadata or {},
        }
        key = self._conv_key(session_id, user_id)
        self.client.rpush(key, json.dumps(entry))
        self.client.expire(key, CONV_TTL)
        logger.debug(f"RedisMemory saved message → session={session_id} role={role}")
        return entry["id"]

    def get_conversation(
        self,
        session_id: str,
        limit: int = 50,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return recent messages for a session (newest last)."""
        key = self._conv_key(session_id, user_id)
        raw = self.client.lrange(key, -limit, -1)
        return [json.loads(r) for r in raw]

    def delete_conversation(self, session_id: str, user_id: Optional[str] = None) -> None:
        self.client.delete(self._conv_key(session_id, user_id))
        logger.debug(f"RedisMemory deleted conversation → session={session_id}")

    # ========================================================
    # AGENT RUNS
    # ========================================================

    def save_agent_run(
        self,
        run_id: str,
        agent_name: str,
        question: str,
        answer: str,
        tool_history: List[str],
        compliance_flags: List[Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist the result of a single agent execution."""
        record = {
            "run_id": run_id,
            "agent_name": agent_name,
            "question": question,
            "answer": answer,
            "tool_history": tool_history,
            "compliance_flags": compliance_flags,
            "timestamp": datetime.utcnow().isoformat(),
            "metadata": metadata or {},
        }
        key = f"run:{run_id}"
        self.client.set(key, json.dumps(record), ex=RUN_TTL)

        # also push to per-agent list for history queries
        list_key = f"runs:agent:{agent_name}"
        self.client.rpush(list_key, run_id)
        self.client.expire(list_key, RUN_TTL)

        logger.debug(f"RedisMemory saved agent run → {run_id}")

    def get_agent_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        raw = self.client.get(f"run:{run_id}")
        if raw is None:
            return None
        return json.loads(raw)

    def list_agent_runs(
        self,
        agent_name: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Return recent run records for a given agent name."""
        list_key = f"runs:agent:{agent_name}"
        run_ids = self.client.lrange(list_key, -limit, -1)
        results = []
        for rid in run_ids:
            record = self.get_agent_run(rid)
            if record:
                results.append(record)
        return results

    # ========================================================
    # WORKFLOW HISTORY
    # ========================================================

    def save_workflow_run(
        self,
        workflow_id: str,
        workflow_name: str,
        steps: List[Dict[str, Any]],
        status: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist a workflow execution record."""
        record = {
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "steps": steps,
            "status": status,       # "success" | "failed" | "partial"
            "timestamp": datetime.utcnow().isoformat(),
            "metadata": metadata or {},
        }
        key = f"workflow:{workflow_id}"
        self.client.set(key, json.dumps(record), ex=WF_TTL)

        list_key = f"workflows:{workflow_name}"
        self.client.rpush(list_key, workflow_id)
        self.client.expire(list_key, WF_TTL)

        logger.debug(f"RedisMemory saved workflow run → {workflow_id}")

    def get_workflow_run(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        raw = self.client.get(f"workflow:{workflow_id}")
        if raw is None:
            return None
        return json.loads(raw)

    def list_workflow_runs(
        self,
        workflow_name: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        list_key = f"workflows:{workflow_name}"
        wf_ids = self.client.lrange(list_key, -limit, -1)
        results = []
        for wid in wf_ids:
            record = self.get_workflow_run(wid)
            if record:
                results.append(record)
        return results

    # ========================================================
    # HEALTH
    # ========================================================

    def ping(self) -> bool:
        try:
            return self.client.ping()
        except Exception:
            return False
