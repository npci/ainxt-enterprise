# SPDX-License-Identifier: Apache-2.0
"""
workers/memory_maintenance_worker.py — P5: Memory quality maintenance.

Runs every 6 hours via the cron scheduler in start_workers.py.

Tasks:
  1. expire_stale_memories()  — DELETE memory_entries WHERE expires_at <= NOW()
  2. decay_importance_scores() — multiply importance_score × 0.95 for entries
                                  older than 30 days (prevents stale memories
                                  from permanently dominating retrieval)

Both operations are idempotent and safe to run concurrently with live traffic.
"""

from core.logger import logger


def run_memory_maintenance() -> dict:
    """
    Perform memory quality maintenance:
      - Expire stale memory_entries (expires_at in the past)
      - Decay importance scores for old entries

    Returns a summary dict for logging/monitoring.
    Called every 6h by the cron scheduler in start_workers.py.
    """
    result = {
        "expired_count":  0,
        "decayed_count":  0,
        "error":          None,
    }
    try:
        from memory.postgres_memory import PostgresMemory
        mem = PostgresMemory()
        if not mem.available:
            logger.warning("memory_maintenance_worker: PostgresMemory unavailable — skipping")
            result["error"] = "postgres_unavailable"
            return result

        expired = mem.expire_stale_memories()
        result["expired_count"] = expired

        decayed = mem.decay_importance_scores(decay_factor=0.95)
        result["decayed_count"] = decayed

        logger.info(
            f"memory_maintenance_worker: expired={expired} decayed={decayed}"
        )
    except Exception as e:
        logger.error(f"memory_maintenance_worker failed: {e}")
        result["error"] = str(e)

    return result
