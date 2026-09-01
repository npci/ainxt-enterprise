# SPDX-License-Identifier: Apache-2.0
# ============================================================
# DLQ WORKER — records permanently failed jobs for inspection
# ============================================================

from core.logger import logger


def record_dlq_job(payload: dict) -> dict:
    """
    No-op worker that simply logs the DLQ entry.
    The job payload (original fn_name, payload, error) is visible
    via GET /jobs/failed (admin endpoint) for manual intervention.
    """
    job_id  = payload.get("original_job_id", "unknown")
    fn_name = payload.get("fn_name", "unknown")
    error   = payload.get("error", "")[:500]
    logger.error(f"DLQ entry: job={job_id} fn={fn_name} error={error}")
    return {"recorded": True, "job_id": job_id}
