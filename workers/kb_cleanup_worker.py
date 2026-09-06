# SPDX-License-Identifier: MIT
# ============================================================
# KB CLEANUP WORKER — recover documents stuck in INDEXING status.
#
# Problem:
#   When the RQ worker process is hard-killed (SIGKILL, OOM, server restart,
#   Docker container stop) while activate_doc() is running, the in-flight job
#   is abandoned. The rollback code in kb_worker.run_activate_doc() never runs,
#   so the document stays stuck in INDEXING status forever. The UI polls every
#   5 seconds showing "⏳ Parsing & indexing..." indefinitely with no error.
#
# Solution:
#   This job runs every 10 minutes via the cron scheduler in start_workers.py.
#   It finds any KnowledgeDocument that has been in INDEXING status for more
#   than STALE_THRESHOLD_MINUTES (35 min) and resets it to PENDING_APPROVAL
#   with a human-readable parse_error message so the approver can retry.
#
# Why 35 minutes?
#   The RQ job timeout for kb_queue is 1800s (30 min). Any document still in
#   INDEXING after 35 min is guaranteed to be stuck — the job either timed out
#   or the worker was killed. The 5-minute buffer accounts for slow startups.
#
# Wired into:
#   workers/start_workers.py — interval_jobs list (every 10 minutes)
# ============================================================

from core.logger import logger

# Threshold: documents stuck in INDEXING longer than this are considered stale.
# Must be > RQ job timeout (1800s = 30 min) + startup buffer.
_STALE_THRESHOLD_MINUTES = 35


def recover_stale_indexing_docs() -> dict:
    """
    Find KnowledgeDocuments stuck in INDEXING for more than STALE_THRESHOLD_MINUTES
    and reset them to PENDING_APPROVAL so the approver can retry.

    Called every 10 minutes by the cron scheduler in start_workers.py.

    Returns:
        {"recovered": N}  — number of documents reset (0 if none were stale)
    """
    import datetime

    try:
        from db.database import SessionLocal
        from db.models import KnowledgeDocument

        stale_cutoff = datetime.datetime.utcnow() - datetime.timedelta(
            minutes=_STALE_THRESHOLD_MINUTES
        )

        db = SessionLocal()
        try:
            stale_docs = (
                db.query(KnowledgeDocument)
                .filter(
                    KnowledgeDocument.status == "INDEXING",
                    KnowledgeDocument.updated_at < stale_cutoff,
                )
                .all()
            )

            if not stale_docs:
                logger.debug(
                    f"[KB_CLEANUP] No stale INDEXING documents found "
                    f"(threshold={_STALE_THRESHOLD_MINUTES}min)"
                )
                return {"recovered": 0}

            _error_msg = (
                f"Processing was interrupted (server restart, worker crash, or "
                f"job timeout after {_STALE_THRESHOLD_MINUTES} min). "
                f"Please re-approve to retry."
            )

            for doc in stale_docs:
                logger.warning(
                    f"[KB_CLEANUP] Recovering stale INDEXING doc: "
                    f"doc_id={doc.id} name='{doc.name}' "
                    f"stuck_since={doc.updated_at.isoformat()}"
                )
                doc.status = "PENDING_APPROVAL"
                doc.parse_error = _error_msg

            db.commit()

            logger.info(
                f"[KB_CLEANUP] Recovered {len(stale_docs)} stale INDEXING "
                f"document(s) → PENDING_APPROVAL"
            )
            return {"recovered": len(stale_docs)}

        finally:
            db.close()

    except Exception as exc:
        logger.error(f"[KB_CLEANUP][ERROR] recover_stale_indexing_docs failed: {exc}")
        return {"recovered": 0}
