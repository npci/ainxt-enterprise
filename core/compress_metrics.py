# SPDX-License-Identifier: Apache-2.0
"""
Phase 2 — Compression Telemetry.

Redis-backed counters for real-time compression stats.
Key: compress:metrics:{YYYY-MM-DD}  →  HASH of {source.before, source.after, source.calls}

Sources: ide_session | ide_tool | sdlc_build | sdlc_test | rag_dedup | rag_trim | orchestrator

No hot-path DB writes — all counters live in Redis with 8-day TTL.
The /metrics/compression endpoint reads the last 7 days and returns aggregates.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta

logger = logging.getLogger(__name__)

_METRICS_TTL = 8 * 24 * 3600   # 8 days in Redis
_METRICS_DB  = 9                # dedicated Redis db for compress metrics


def _redis():
    """Lazy KV connection — fail silently if backend unavailable.

    Routes through core.kv so the backend is
    selected per-DB via REDIS_CLIENT_CONFIG_DB{_METRICS_DB}.
    """
    try:
        from core.kv import get_kv
        return get_kv(_METRICS_DB, decode_responses=True)
    except Exception:
        return None


def record(source: str, before_chars: int, after_chars: int) -> None:
    """
    Record a single compression event.
    source: e.g. "ide_session", "ide_tool", "sdlc_build", "rag_dedup"
    """
    if before_chars <= 0:
        return
    try:
        rc = _redis()
        if not rc:
            return
        key = f"compress:metrics:{date.today().isoformat()}"
        pipe = rc.pipeline()
        pipe.hincrby(key, f"{source}.before", before_chars)
        pipe.hincrby(key, f"{source}.after",  after_chars)
        pipe.hincrby(key, f"{source}.calls",  1)
        pipe.expire(key, _METRICS_TTL)
        pipe.execute()
    except Exception:  # noqa: BLE001
        # SECURITY: exception variable intentionally not referenced in log (CWE-209).
        logger.debug("[compress_metrics] record failed (non-blocking)")


def get_stats(days: int = 7) -> dict:
    """
    Return per-source aggregated stats for the last N days.
    Response shape:
    {
      "days": [...],
      "totals": {"ide_session": {"before": N, "after": M, "calls": K, "reduction_pct": X}, ...},
      "daily": [{"date": "...", "sources": {...}}, ...]
    }
    """
    rc = _redis()
    if not rc:
        return {"error": "Redis unavailable", "days": [], "totals": {}, "daily": []}

    today  = date.today()
    daily  = []
    totals: dict = {}

    for i in range(days - 1, -1, -1):
        d   = today - timedelta(days=i)
        key = f"compress:metrics:{d.isoformat()}"
        raw = rc.hgetall(key) or {}

        day_sources: dict = {}
        # Parse fields like "ide_session.before" → 12345
        for field, val in raw.items():
            parts = field.rsplit(".", 1)
            if len(parts) == 2:
                src, metric = parts
                day_sources.setdefault(src, {"before": 0, "after": 0, "calls": 0})
                day_sources[src][metric] = int(val)

        # Compute reduction % per source per day
        for src, v in day_sources.items():
            if v["before"] > 0:
                v["reduction_pct"] = round(100 * (1 - v["after"] / v["before"]), 1)
            else:
                v["reduction_pct"] = 0.0
            totals.setdefault(src, {"before": 0, "after": 0, "calls": 0})
            totals[src]["before"] += v["before"]
            totals[src]["after"]  += v["after"]
            totals[src]["calls"]  += v["calls"]

        daily.append({"date": d.isoformat(), "sources": day_sources})

    for src, v in totals.items():
        if v["before"] > 0:
            v["reduction_pct"] = round(100 * (1 - v["after"] / v["before"]), 1)
        else:
            v["reduction_pct"] = 0.0

    return {"days": days, "totals": totals, "daily": daily}
