# SPDX-License-Identifier: Apache-2.0
"""
agents/recovery_engine.py — P9: Recovery engine.

Provides:
  1. Undo stack for write operations (Redis-backed, max 20 entries per session)
  2. ReAct checkpoint/resume (Redis-backed, TTL=1h)
  3. Graceful partial completion message when timeout/max_rounds hit

REVERSIBLE TOOLS
----------------
Only tools that have a clear inverse are tracked:
  - gitlab_create_or_update_file → delete the file (or restore prior content)
  - jira_create_issue            → delete the issue (transition to "Cancelled")
  - jira_add_comment             → no inverse (comments are immutable in Jira)

WHAT IS NOT BUILT
-----------------
- Redo (forward undo)
- Distributed undo across sessions
- Undo for run_code (sandbox is ephemeral)
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from core.logger import logger


# SEC-05: key is namespaced by user_id to prevent BOLA (cross-user undo)
# Format: undo_stack:{user_id}:{session_id}
_UNDO_STACK_KEY_PFX = "undo_stack:"
_UNDO_STACK_MAX = 20
_UNDO_STACK_TTL = 3600  # 1h

_REACT_CKPT_KEY_PFX = "react_ckpt:"
_REACT_CKPT_TTL = int(os.getenv("PLAN_CHECKPOINT_TTL_SEC", "3600"))

# Tools that have a reversible inverse
_REVERSIBLE_TOOLS = {
    "gitlab_create_or_update_file",
    "jira_create_issue",
}


class RecoveryEngine:
    """
    Manages undo stacks and ReAct checkpoints for session-level recovery.
    """

    def __init__(self):
        self._redis = None

    def _get_redis(self):
        if self._redis is None:
            from core.kv import get_kv
            from core.config import RDB_CACHE
            self._redis = get_kv(RDB_CACHE, decode_responses=True)
        return self._redis

    # ── Undo stack ───────────────────────────────────────────────────────────

    def _undo_key(self, session_id: str, user_id: str = "") -> str:
        """
        SEC-05: Build the Redis undo stack key namespaced by user_id.
        Format: undo_stack:{user_id}:{session_id}
        Falls back to undo_stack::{session_id} when user_id is empty (internal callers).
        """
        return f"{_UNDO_STACK_KEY_PFX}{user_id}:{session_id}"

    def record_action(
        self,
        tool_name: str,
        inputs: Dict[str, Any],
        result: str,
        session_id: str,
        user_id: str = "",
    ) -> Optional[str]:
        """
        Record a write operation on the undo stack for session_id.

        Only reversible tools are tracked (see _REVERSIBLE_TOOLS).
        Stack is capped at _UNDO_STACK_MAX entries (oldest dropped).
        SEC-05: key is namespaced by user_id to prevent cross-user undo.

        Returns the action_id (UUID) or None if not tracked.
        """
        if tool_name not in _REVERSIBLE_TOOLS:
            return None

        import uuid
        action_id = str(uuid.uuid4())
        entry = {
            "action_id":  action_id,
            "tool_name":  tool_name,
            "inputs":     inputs,
            "result":     result[:500],
            "timestamp":  time.time(),
        }

        try:
            redis = self._get_redis()
            key = self._undo_key(session_id, user_id)
            # Push to list (RPUSH = append to right = newest at end)
            redis.rpush(key, json.dumps(entry))
            # Trim to max size (keep newest)
            redis.ltrim(key, -_UNDO_STACK_MAX, -1)
            redis.expire(key, _UNDO_STACK_TTL)
            logger.debug(f"RecoveryEngine: recorded action {action_id} ({tool_name}) for session {session_id}")
            return action_id
        except Exception as e:
            logger.warning(f"RecoveryEngine.record_action failed (non-fatal): {e}")
            return None

    def undo_last(self, session_id: str, user_id: str = "") -> Optional[str]:
        """
        Pop the last action from the undo stack and execute its inverse.
        SEC-05: user_id scopes the key so users can only undo their own actions.

        Returns a human-readable description of what was undone, or None if
        the stack is empty or the inverse failed.
        """
        try:
            redis = self._get_redis()
            key = self._undo_key(session_id, user_id)
            raw = redis.rpop(key)
            if not raw:
                return None

            entry = json.loads(raw)
            tool_name = entry.get("tool_name", "")
            inputs = entry.get("inputs", {})

            result = self._execute_inverse(tool_name, inputs)
            logger.info(f"RecoveryEngine: undid {tool_name} for session {session_id}")
            return result
        except Exception as e:
            logger.error(f"RecoveryEngine.undo_last failed: {e}")
            return None

    def get_undo_stack(self, session_id: str, user_id: str = "") -> List[Dict]:
        """
        Return the current undo stack for a session (newest last).
        SEC-05: user_id scopes the key so users can only see their own stack.
        """
        try:
            redis = self._get_redis()
            key = self._undo_key(session_id, user_id)
            raw_list = redis.lrange(key, 0, -1)
            return [json.loads(r) for r in raw_list]
        except Exception as e:
            logger.warning(f"RecoveryEngine.get_undo_stack failed: {e}")
            return []

    def _execute_inverse(self, tool_name: str, inputs: Dict) -> str:
        """Execute the inverse of a recorded tool call."""
        if tool_name == "gitlab_create_or_update_file":
            # Inverse: delete the file (or restore prior content if available)
            try:
                from tools.gitlab_tools import gitlab_delete_file
                repo = inputs.get("repo", "")
                path = inputs.get("path", "")
                branch = inputs.get("branch", "main")
                gitlab_delete_file(repo, path, branch, commit_message="[undo] Reverting file creation")
                return f"Undone: deleted {path} from {repo} (branch: {branch})"
            except Exception as e:
                return f"Undo failed for gitlab_create_or_update_file: {e}"

        elif tool_name == "jira_create_issue":
            # Inverse: transition issue to "Cancelled" (Jira doesn't support delete via API)
            try:
                from tools.jira_tools import jira_transition_issue
                issue_key = _extract_jira_key(inputs.get("result", ""))
                if issue_key:
                    jira_transition_issue(issue_key, "Cancel")
                    return f"Undone: cancelled Jira issue {issue_key}"
                return "Undo: could not extract Jira issue key from result"
            except Exception as e:
                return f"Undo failed for jira_create_issue: {e}"

        return f"No inverse defined for {tool_name}"

    # ── ReAct checkpointing ──────────────────────────────────────────────────

    def save_react_checkpoint(
        self,
        session_id: str,
        goal: str,
        observations: List[Dict],
        answer_so_far: str,
        loop_count: int,
    ) -> None:
        """
        Save ReAct loop state to Redis for crash recovery.
        Key: react_ckpt:{session_id}:{goal_hash}
        TTL: PLAN_CHECKPOINT_TTL_SEC (default 3600s = 1h)
        """
        try:
            import hashlib
            goal_hash = hashlib.sha256(goal.encode()).hexdigest()[:16]
            key = f"{_REACT_CKPT_KEY_PFX}{session_id}:{goal_hash}"
            redis = self._get_redis()
            redis.setex(key, _REACT_CKPT_TTL, json.dumps({
                "goal":           goal,
                "observations":   observations,
                "answer_so_far":  answer_so_far[:2000],
                "loop_count":     loop_count,
                "timestamp":      time.time(),
            }))
        except Exception as e:
            logger.debug(f"RecoveryEngine.save_react_checkpoint failed (non-fatal): {e}")

    def load_react_checkpoint(
        self,
        session_id: str,
        goal: str,
        max_age_sec: int = 3600,
    ) -> Optional[Dict]:
        """
        Load a ReAct checkpoint if it exists and is not too old.
        Returns None if no checkpoint or checkpoint is stale.
        """
        try:
            import hashlib
            goal_hash = hashlib.sha256(goal.encode()).hexdigest()[:16]
            key = f"{_REACT_CKPT_KEY_PFX}{session_id}:{goal_hash}"
            redis = self._get_redis()
            raw = redis.get(key)
            if not raw:
                return None
            ckpt = json.loads(raw)
            age = time.time() - ckpt.get("timestamp", 0)
            if age > max_age_sec:
                redis.delete(key)
                return None
            logger.info(
                f"RecoveryEngine: loaded ReAct checkpoint for session={session_id} "
                f"age={age:.0f}s loop_count={ckpt.get('loop_count', 0)}"
            )
            return ckpt
        except Exception as e:
            logger.debug(f"RecoveryEngine.load_react_checkpoint failed (non-fatal): {e}")
            return None

    # ── Partial completion ───────────────────────────────────────────────────

    def handle_partial_completion(
        self,
        goal: str,
        observations: List[Dict],
        partial_answer: str,
        reason: str = "timeout",
    ) -> str:
        """
        Return a graceful partial completion message when the ReAct loop
        hits max_rounds or a timeout.

        Summarizes what was completed, what remains, and suggests next steps.
        """
        completed_tools = [o["tool"] for o in observations if o.get("ok")]
        failed_tools = [o["tool"] for o in observations if not o.get("ok")]

        completed_str = ", ".join(completed_tools[:6]) if completed_tools else "none"
        failed_str = ", ".join(failed_tools[:3]) if failed_tools else "none"

        partial_note = (
            f"\n\n---\n"
            f"**⚠️ Partial completion** ({reason})\n\n"
            f"**Completed steps:** {completed_str}\n"
            f"**Failed/skipped:** {failed_str}\n\n"
            f"**What remains:** The task was not fully completed. "
            f"You can retry with a more specific request, or continue from where this left off.\n\n"
            f"**Partial result:**\n{partial_answer[:1000] if partial_answer else '(no result yet)'}"
        )
        return partial_note


def _extract_jira_key(text: str) -> Optional[str]:
    """Extract a Jira issue key (e.g. PROJ-123) from text."""
    import re
    m = re.search(r'\b([A-Z]{2,10}-\d{1,6})\b', text or "")
    return m.group(1) if m else None


# Module-level singleton
recovery_engine = RecoveryEngine()
