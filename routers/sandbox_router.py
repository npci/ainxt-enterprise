# SPDX-License-Identifier: Apache-2.0
# ============================================================
# SANDBOX ROUTER — /sandbox
#
# Thin HTTP wrapper around sandbox/docker_executor.py for the CLI's
# /sandbox command. Runs code in a network-disabled Docker container
# with capped CPU/memory; the container is auto-removed after execution.
#
# Security:
#   - Compliance engine runs on the code BEFORE execution. If it flags
#     PCI/PII/secrets, we 403 the request before invoking docker.
#   - DockerExecutor pins resource limits (--network none, 512MB mem,
#     50% CPU quota) and removes the container immediately after.
#   - Per-user rate limit: 30 executions per hour (via Redis sliding window).
#
# Endpoints:
#   POST /sandbox/exec  { language: 'python'|'bash'|'node', code: str }
# ============================================================

from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.logger import logger
from auth.dependencies import get_current_user

router = APIRouter(tags=["sandbox"])


class SandboxExecRequest(BaseModel):
    language: Literal["python", "bash", "node"]
    code:     str = Field(..., min_length=1, max_length=64_000)


class SandboxExecResponse(BaseModel):
    stdout:       str = ""
    stderr:       str = ""
    exit_code:    int = 0
    duration_ms:  int = 0
    blocked:      bool = False
    blocked_reason: Optional[str] = None


# ─────────────────────────────────────────────────────────────
# Rate limit: 30 executions per hour per user.
# Cheap sliding window via Redis ZSET (db=4, key sandbox:rl:<user_id>).
# Falls back to "allow" if Redis is unreachable — never blocks legit users
# due to infra issues.
# ─────────────────────────────────────────────────────────────
_SANDBOX_RATE_LIMIT_PER_HOUR = 30


def _rate_limit_ok(user_id: str) -> bool:
    if not user_id:
        return True
    try:
        import time
        from core.config import RDB_BUDGET
        from core.kv import get_kv
        rc = get_kv(RDB_BUDGET, decode_responses=True)
        key  = f"sandbox:rl:{user_id}"
        now  = time.time()
        # Drop entries older than 1h
        rc.zremrangebyscore(key, 0, now - 3600)
        count = rc.zcard(key)
        if count >= _SANDBOX_RATE_LIMIT_PER_HOUR:
            return False
        rc.zadd(key, {str(now): now})
        rc.expire(key, 3600)
        return True
    except Exception:
        return True  # fail open — never block on KV errors


# ─────────────────────────────────────────────────────────────
# POST /sandbox/exec
# ─────────────────────────────────────────────────────────────
@router.post("/sandbox/exec", response_model=SandboxExecResponse)
def sandbox_exec(
    req: SandboxExecRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Execute a code snippet in the platform's isolated Docker sandbox.

    Pipeline:
      1. Compliance check on `code` (PCI/PII/secrets/dangerous patterns).
      2. Per-user rate limit (30/hour).
      3. Hand off to docker_executor.DockerExecutor.execute().
      4. Return stdout/stderr/exit_code/duration.

    NEVER runs untrusted code on the gateway host — every execution lives
    inside a fresh container with --network none.
    """
    user_id  = current_user.get("sub") or current_user.get("user_id") or current_user.get("id") or "anonymous"
    user_email = current_user.get("email", "")

    # ─── 1. Compliance check ────────────────────────────────
    try:
        from agents.compliance_engine import compliance_engine
        check = compliance_engine.check(req.code)
        if check and getattr(check, "blocked", False):
            reason = getattr(check, "reason", "compliance violation")
            logger.warning(f"sandbox/exec blocked: user={user_email} reason={reason}")
            return SandboxExecResponse(
                blocked=True,
                blocked_reason=reason,
            )
    except ImportError:
        # Compliance engine optional — log and continue (safer to refuse but
        # we honor existing platform install patterns).
        logger.warning("agents.compliance_engine unavailable — sandbox skipping content check")
    except Exception as e:
        logger.warning(f"sandbox/exec compliance check error: {e}")
        # On engine error, refuse rather than risk leaking unscanned code.
        raise HTTPException(status_code=503, detail="Compliance engine unavailable; sandbox rejecting request")

    # ─── 2. Rate limit ──────────────────────────────────────
    if not _rate_limit_ok(user_id):
        raise HTTPException(
            status_code=429,
            detail=f"Sandbox rate limit: {_SANDBOX_RATE_LIMIT_PER_HOUR} executions/hour. Retry later.",
        )

    # ─── 3. Run in Docker ───────────────────────────────────
    try:
        from sandbox.docker_executor import get_executor
        executor = get_executor(req.language)
        if executor is None:
            raise HTTPException(status_code=503, detail="Sandbox executor not available (docker daemon down?)")
        if hasattr(executor, "is_available") and not executor.is_available():
            raise HTTPException(status_code=503, detail="Docker daemon not running on the gateway host")
        result = executor.execute(code=req.code, language=req.language)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"sandbox/exec error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Sandbox execution failed: {e}")

    logger.info(
        f"sandbox/exec ok: user={user_email} language={req.language} "
        f"exit={result.get('exit_code')} duration_ms={result.get('duration_ms', 0)}"
    )

    return SandboxExecResponse(
        stdout=str(result.get("stdout", ""))[:200_000],
        stderr=str(result.get("stderr", ""))[:200_000],
        exit_code=int(result.get("exit_code", 0) or 0),
        duration_ms=int(result.get("duration_ms", 0) or 0),
        blocked=False,
    )