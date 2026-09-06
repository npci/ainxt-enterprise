# SPDX-License-Identifier: MIT
# ============================================================
# LEARNING STORE
# Captures tool failures, retries, and low-confidence answers
# and converts them into pattern signals for the agent loop.
#
# Storage: Redis db=1 (trace store) — failure lists per tool
# Keys:
#   learn:tool_fail:{tool}   — list of JSON failure records (last 100)
#   learn:low_conf:{user_id} — list of low-confidence question patterns
# ============================================================

import json
import time
from typing import Optional

from core.config import REDIS_HOST, REDIS_PORT, RDB_TRACE
from core.kv import get_kv, KVClient
from core.logger import logger

_FAIL_TTL    = 86400 * 7   # 7 days
_MAX_ENTRIES = 100


def _redis() -> KVClient:
    """KV client for the learning store (DB=1).

    Backend selected via REDIS_CLIENT_CONFIG_DB1.
    """
    return get_kv(RDB_TRACE, decode_responses=True)


def record_tool_failure(
    tool: str,
    error: str,
    request_id: str = "",
    user_id: str = "",
    plan: Optional[list] = None,
) -> None:
    """Record a tool failure for pattern detection."""
    try:
        r = _redis()
        key = f"learn:tool_fail:{tool}"
        entry = json.dumps({
            "ts":         time.time(),
            "error":      error[:300],
            "request_id": request_id,
            "user_id":    user_id,
            "plan":       json.dumps(plan or [])[:500],
        })
        r.lpush(key, entry)
        r.ltrim(key, 0, _MAX_ENTRIES - 1)
        r.expire(key, _FAIL_TTL)
        logger.debug(f"[LearningStore] tool failure recorded → tool={tool}")
    except Exception as e:
        logger.debug(f"[LearningStore] record_tool_failure skipped: {e}")


def get_tool_failure_count(tool: str, window_seconds: int = 86400) -> int:
    """
    Return number of failures for a tool within the time window.
    Used by orchestrator to skip consistently-failing tools.
    """
    try:
        r = _redis()
        key = f"learn:tool_fail:{tool}"
        raw_entries = r.lrange(key, 0, _MAX_ENTRIES - 1)
        cutoff = time.time() - window_seconds
        count = 0
        for raw in raw_entries:
            try:
                entry = json.loads(raw)
                if float(entry.get("ts", 0)) >= cutoff:
                    count += 1
            except Exception:
                pass
        return count
    except Exception:
        return 0


def record_low_confidence(
    question: str,
    confidence: float,
    user_id: str = "",
    request_id: str = "",
) -> None:
    """Record a low-confidence answer for routing improvement analysis."""
    try:
        r = _redis()
        key = f"learn:low_conf:{user_id or 'global'}"
        entry = json.dumps({
            "ts":         time.time(),
            "question":   question[:200],
            "confidence": round(confidence, 3),
            "request_id": request_id,
        })
        r.lpush(key, entry)
        r.ltrim(key, 0, _MAX_ENTRIES - 1)
        r.expire(key, _FAIL_TTL)
    except Exception as e:
        logger.debug(f"[LearningStore] record_low_confidence skipped: {e}")


def get_failure_summary() -> dict:
    """
    Return a summary of all tool failures in the last 24h.
    Used by the admin metrics dashboard.
    """
    try:
        r = _redis()
        keys = r.keys("learn:tool_fail:*")
        summary = {}
        cutoff = time.time() - 86400
        for key in keys:
            tool = key.replace("learn:tool_fail:", "")
            entries = r.lrange(key, 0, _MAX_ENTRIES - 1)
            recent = sum(
                1 for e in entries
                if json.loads(e).get("ts", 0) >= cutoff
            )
            if recent > 0:
                summary[tool] = recent
        return summary
    except Exception:
        return {}
