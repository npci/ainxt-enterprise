#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# ============================================================
# THREAD PURGE WORKER — nightly cleanup of stale threads
#
# Purge policy (configurable via env vars):
#   THREAD_RETAIN_DAYS  — keep threads with activity in last N days (default 90)
#   THREAD_MAX_MESSAGES — hard cap: delete oldest messages beyond this per thread (default 1000)
#
# Schedule: run as a cron job at 03:00 IST daily (or via start_workers.py)
#
# Safety:
#   - Only deletes threads where ALL messages are older than THREAD_RETAIN_DAYS
#   - Never deletes threads with pending @AiNxt agent tasks (status=running/pending)
#   - Logs summary to stdout + writes a purge_run record to Redis (db=1, trace store)
# ============================================================

import os
import json
import sys
from datetime import datetime, timedelta, timezone

# Allow running from the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# CKMS — decrypt protected env vars before core.logger / db imports run.
from core.ckms import load_at_boot as _ckms_load_at_boot
_ckms_load_at_boot()

from core.logger import logger

THREAD_RETAIN_DAYS  = int(os.getenv("THREAD_RETAIN_DAYS",  "90"))
THREAD_MAX_MESSAGES = int(os.getenv("THREAD_MAX_MESSAGES", "1000"))

_DRY_RUN = os.getenv("THREAD_PURGE_DRY_RUN", "false").lower() == "true"


def run_purge() -> dict:
    """
    Execute the thread purge cycle.

    Returns a summary dict:
      {threads_scanned, threads_deleted, messages_trimmed, errors, duration_s}
    """
    from db.database import SessionLocal
    from db.models import Thread, ThreadMessage
    from sqlalchemy import func

    start     = datetime.now(timezone.utc)
    cutoff    = start - timedelta(days=THREAD_RETAIN_DAYS)
    stats     = {"threads_scanned": 0, "threads_deleted": 0, "messages_trimmed": 0, "errors": 0}

    db = SessionLocal()
    try:
        threads = db.query(Thread).all()
        stats["threads_scanned"] = len(threads)

        for thread in threads:
            try:
                # --- Hard message cap: trim oldest messages beyond THREAD_MAX_MESSAGES ---
                msg_count = (
                    db.query(func.count(ThreadMessage.id))
                    .filter(ThreadMessage.thread_id == str(thread.id))
                    .scalar()
                ) or 0

                if msg_count > THREAD_MAX_MESSAGES:
                    excess = msg_count - THREAD_MAX_MESSAGES
                    oldest_ids = (
                        db.query(ThreadMessage.id)
                        .filter(ThreadMessage.thread_id == str(thread.id))
                        .order_by(ThreadMessage.created_at.asc())
                        .limit(excess)
                        .all()
                    )
                    ids_to_trim = [r.id for r in oldest_ids]
                    if not _DRY_RUN:
                        db.query(ThreadMessage).filter(ThreadMessage.id.in_(ids_to_trim)).delete(
                            synchronize_session=False
                        )
                    stats["messages_trimmed"] += len(ids_to_trim)
                    logger.info(
                        f"thread_purge: trimmed {len(ids_to_trim)} old messages "
                        f"from thread {thread.id} (cap={THREAD_MAX_MESSAGES})"
                    )

                # --- Stale thread deletion ---
                # Find most recent message timestamp
                last_msg = (
                    db.query(func.max(ThreadMessage.created_at))
                    .filter(ThreadMessage.thread_id == str(thread.id))
                    .scalar()
                )

                thread_updated = getattr(thread, "updated_at", None) or getattr(thread, "created_at", None)
                last_activity  = max(
                    filter(None, [last_msg, thread_updated]),
                    default=None,
                )

                if last_activity is None:
                    continue  # can't determine age — skip

                # Make timezone-aware if naive
                if last_activity.tzinfo is None:
                    last_activity = last_activity.replace(tzinfo=timezone.utc)

                if last_activity >= cutoff:
                    continue  # thread has recent activity — keep it

                # Don't delete threads with active agent runs
                agent_status = getattr(thread, "agent_status", None)
                if agent_status in ("running", "pending"):
                    logger.info(
                        f"thread_purge: skipping active agent thread {thread.id} "
                        f"(status={agent_status})"
                    )
                    continue

                if _DRY_RUN:
                    logger.info(
                        f"thread_purge: [DRY RUN] would delete thread {thread.id} "
                        f"(last_activity={last_activity.isoformat()})"
                    )
                else:
                    # Messages cascade-deleted via FK on thread delete
                    db.delete(thread)
                    logger.info(
                        f"thread_purge: deleted stale thread {thread.id} "
                        f"(last_activity={last_activity.isoformat()})"
                    )

                stats["threads_deleted"] += 1

            except Exception as e:
                stats["errors"] += 1
                logger.error(f"thread_purge: error processing thread {thread.id}: {e}")

        if not _DRY_RUN:
            db.commit()

    except Exception as e:
        stats["errors"] += 1
        logger.error(f"thread_purge: fatal error: {e}")
    finally:
        db.close()

    duration = (datetime.now(timezone.utc) - start).total_seconds()
    stats["duration_s"] = round(duration, 2)

    _write_run_record(stats)

    logger.info(
        f"thread_purge: complete — "
        f"scanned={stats['threads_scanned']} "
        f"deleted={stats['threads_deleted']} "
        f"trimmed={stats['messages_trimmed']} "
        f"errors={stats['errors']} "
        f"({duration:.1f}s)"
        + (" [DRY RUN]" if _DRY_RUN else "")
    )
    return stats


def _write_run_record(stats: dict) -> None:
    """Write a purge-run summary to the KV trace store (DB=1) with 30-day TTL."""
    try:
        from core.config import RDB_TRACE
        from core.kv import get_kv
        r = get_kv(RDB_TRACE, decode_responses=True)
        key = f"thread_purge:{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        r.setex(key, 86400 * 30, json.dumps(stats))
    except Exception as e:
        logger.warning(f"thread_purge: could not write run record to KV: {e}")


if __name__ == "__main__":
    import sys
    if "--dry-run" in sys.argv:
        os.environ["THREAD_PURGE_DRY_RUN"] = "true"
        logger.info("thread_purge: DRY RUN mode")
    run_purge()
