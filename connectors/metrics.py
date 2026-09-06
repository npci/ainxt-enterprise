# SPDX-License-Identifier: MIT
"""
KV-backed metrics for connector executions.
Uses DB=1 (trace store) to record per-connector call stats.
Backend selected via REDIS_CLIENT_CONFIG_DB1.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from core.config import RDB_TRACE
from core.kv import get_kv, KVError
from core.logger import logger


class ConnectorMetrics:
    """
    Records and retrieves per-connector metrics in the KV trace store.
    Key pattern: connector:metrics:{connector_name}:{metric}
    """

    def __init__(self):
        self._redis = None

    def _get_redis(self):
        if self._redis is None:
            try:
                self._redis = get_kv(RDB_TRACE, decode_responses=True)
            except KVError as e:
                logger.warning(f"ConnectorMetrics: KV backend unavailable — {e}")
        return self._redis

    def record_call(
        self,
        connector: str,
        tool: str,
        latency_ms: int,
        success: bool,
        cache_hit: bool = False,
        user_id: str = "",
        dept: str = "",
        error_type: str = "",
    ) -> None:
        """Record a connector tool call and update all observability indexes."""
        try:
            r = self._get_redis()
            if not r:
                return
            prefix = f"connector:metrics:{connector}"
            pipe = r.pipeline()
            pipe.incr(f"{prefix}:calls_total")
            if not success:
                pipe.incr(f"{prefix}:errors_total")
            if cache_hit:
                pipe.incr(f"{prefix}:cache_hits")
            pipe.incrbyfloat(f"{prefix}:latency_sum_ms", latency_ms)
            if not success:
                pipe.set(f"{prefix}:last_error_at", int(time.time()))

            # Top queries: sorted set {connector}:{tool} → score=call_count
            pipe.zincrby("connector:top_queries", 1, f"{connector}:{tool}")

            # Usage by department
            if dept:
                pipe.zincrby(f"connector:usage_by_dept:{connector}", 1, dept)

            # Failure distribution (error_type → count)
            if not success and error_type:
                pipe.zincrby(f"connector:failure_dist:{connector}", 1, error_type)

            pipe.execute()

            # Audit log (keep last 1000)
            audit_key = f"connector:audit:{connector}"
            entry = json.dumps({
                "user_id": user_id,
                "connector": connector,
                "tool": tool,
                "latency_ms": latency_ms,
                "success": success,
                "cache_hit": cache_hit,
                "dept": dept,
                "error_type": error_type,
                "ts": int(time.time()),
            })
            r.lpush(audit_key, entry)
            r.ltrim(audit_key, 0, 999)
        except Exception as e:
            logger.debug(f"ConnectorMetrics.record_call failed (non-critical): {e}")

    def record_token_refresh(self, connector: str, user_id: str, success: bool) -> None:
        """Record a token refresh attempt."""
        try:
            r = self._get_redis()
            if not r:
                return
            pipe = r.pipeline()
            pipe.incr(f"connector:metrics:{connector}:token_refreshes")
            if not success:
                pipe.incr(f"connector:metrics:{connector}:token_refresh_failures")
            pipe.execute()
        except Exception as e:
            logger.debug(f"ConnectorMetrics.record_token_refresh failed: {e}")

    def get_stats(self, connector: str) -> dict:
        """Returns {calls_total, error_rate, avg_latency_ms, cache_hit_rate}."""
        try:
            r = self._get_redis()
            if not r:
                return {"error": "Redis unavailable"}
            prefix = f"connector:metrics:{connector}"
            calls = int(r.get(f"{prefix}:calls_total") or 0)
            errors = int(r.get(f"{prefix}:errors_total") or 0)
            cache_hits = int(r.get(f"{prefix}:cache_hits") or 0)
            latency_sum = float(r.get(f"{prefix}:latency_sum_ms") or 0)
            last_error_at = r.get(f"{prefix}:last_error_at")

            return {
                "calls_total": calls,
                "error_rate": round(errors / calls, 3) if calls else 0.0,
                "avg_latency_ms": round(latency_sum / calls) if calls else 0,
                "cache_hit_rate": round(cache_hits / calls, 3) if calls else 0.0,
                "last_error_at": int(last_error_at) if last_error_at else None,
            }
        except Exception as e:
            logger.debug(f"ConnectorMetrics.get_stats failed: {e}")
            return {}

    def get_audit_log(self, connector: str, limit: int = 50) -> list[dict]:
        """Return recent audit log entries for a connector."""
        try:
            r = self._get_redis()
            if not r:
                return []
            raw = r.lrange(f"connector:audit:{connector}", 0, limit - 1)
            return [json.loads(e) for e in raw]
        except Exception:
            return []

    def get_top_queries(self, limit: int = 10) -> list[dict]:
        """Return top N connector:tool pairs by call volume (across all connectors)."""
        try:
            r = self._get_redis()
            if not r:
                return []
            entries = r.zrevrange("connector:top_queries", 0, limit - 1, withscores=True)
            return [{"query": k, "calls": int(v)} for k, v in entries]
        except Exception:
            return []

    def get_usage_by_dept(self, connector: str) -> list[dict]:
        """Return per-department usage counts for a connector."""
        try:
            r = self._get_redis()
            if not r:
                return []
            entries = r.zrevrange(f"connector:usage_by_dept:{connector}", 0, -1, withscores=True)
            return [{"dept": k, "calls": int(v)} for k, v in entries]
        except Exception:
            return []

    def get_failure_distribution(self, connector: str) -> list[dict]:
        """Return failure counts grouped by error_type for a connector."""
        try:
            r = self._get_redis()
            if not r:
                return []
            entries = r.zrevrange(f"connector:failure_dist:{connector}", 0, -1, withscores=True)
            return [{"error_type": k, "count": int(v)} for k, v in entries]
        except Exception:
            return []


connector_metrics = ConnectorMetrics()
