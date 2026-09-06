# SPDX-License-Identifier: MIT
# ============================================================
# COWORK run_code EXEC WORKER
#
# Runs Cowork's `run_code` sandbox jobs OFF the gateway process, on a dedicated
# RQ pool (Q_EXEC). This is the scaling fix: at 2k users we must NOT spin Docker
# containers inline in a uvicorn worker — that pins gateway threads and lets RAM
# blow up unbounded. Instead:
#   - the MCP bridge enqueues a job on exec_queue (back-pressured: 503-equivalent
#     when the queue is at capacity),
#   - a bounded pool of exec workers (the pool SIZE is the real concurrency cap)
#     each run ONE container at a time via sandbox/docker_executor,
#   - the result is pushed to a short-lived Redis list the bridge BLPOPs.
#
# Isolation/compliance are unchanged (network-disabled, ephemeral, capped,
# input+output compliance-gated inside docker_executor).
# ============================================================

import json

from core.config import REDIS_HOST, REDIS_PORT
from core.logger import logger

# Result hand-off: Redis db=5 (same db as the queues), short TTL so nothing
# accumulates. The bridge BLPOPs cowork:exec:result:{job_id}.
_RESULT_PREFIX = "cowork:exec:result:"
_RESULT_TTL = 120  # seconds — must exceed the bridge's BLPOP timeout


def _redis():
    import redis
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=5,
                       decode_responses=True, socket_connect_timeout=2)


def run_code_job(payload: dict) -> dict:
    """RQ job: execute a Cowork data-analysis script in the isolated sandbox and
    publish the result for the waiting MCP bridge to pick up.

    payload = {job_id, code, language}
    """
    job_id = payload.get("job_id") or ""
    code = payload.get("code") or ""
    language = (payload.get("language") or "python").lower()
    files = payload.get("files") or None  # data-analysis (ADA): bound data files

    # SECURITY: Cowork runs UNTRUSTED, LLM-generated office-user code. It MUST run
    # inside the isolated, network-disabled Docker sandbox — NEVER the subprocess
    # fallback, which executes on the host with full filesystem read access. If
    # Docker isn't available we refuse to run rather than leak the host FS.
    try:
        from sandbox.docker_executor import docker_executor
        if not docker_executor.is_available():
            logger.warning(f"exec_worker: run_code_job {job_id} REFUSED — Docker sandbox unavailable")
            result = {
                "success": False,
                "output": "The secure code sandbox (Docker) isn't running, so I can't run code right "
                          "now — code is never run outside the isolated sandbox. Ask an admin to start "
                          "Docker, or I can help another way.",
                "exit_code": -1, "language": language, "sandbox_unavailable": True,
            }
        else:
            result = docker_executor.execute(code=code, language=language, files=files) or {}
    except Exception as e:
        logger.error(f"exec_worker: run_code_job {job_id} failed → {e}")
        result = {"success": False, "output": f"Sandbox error: {e}", "exit_code": -1, "language": language}

    # Hand the result back to the bridge (RPUSH + expire). Best-effort: if Redis
    # is gone the bridge's BLPOP simply times out and reports a transient error.
    if job_id:
        try:
            r = _redis()
            key = f"{_RESULT_PREFIX}{job_id}"
            r.rpush(key, json.dumps(result))
            r.expire(key, _RESULT_TTL)
        except Exception as e:
            logger.error(f"exec_worker: could not publish result for {job_id} → {e}")
    return result
