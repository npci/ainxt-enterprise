# SPDX-License-Identifier: MIT
# ============================================================
# SECURE CODE GATE ROUTER  (mounted at /ainxt/v1/api/secure-code-gate)
#
# Generation-time SAST gate for the CLI (full mode). The CLI sends the file it
# just wrote; we scan it (Semgrep/Bandit/secrets), and on HIGH/CRITICAL findings
# run an LLM fix loop and return clean code. Server-side so the CLI stays a thin
# client (no per-machine scanner installs).
#
#   POST /secure-code-gate/scan   { files:[{path,content,language?}], threshold?, fix? }
#       → { blocked, findings, files, fixed_files:[{path,content}], gate, report }
#   GET  /secure-code-gate/status/{job_id}   (for any future async/deep scan)
# ============================================================

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth.dependencies import get_current_user
from core.logger import logger

router = APIRouter(prefix="/secure-code-gate", tags=["secure_code_gate"])


class GateFile(BaseModel):
    path: str
    content: str
    language: Optional[str] = None


class ScanRequest(BaseModel):
    files: list[GateFile]
    threshold: Optional[float] = None
    fix: bool = True


@router.post("/scan")
async def scan(req: ScanRequest, current_user=Depends(get_current_user)):
    """Synchronous fast-tier scan (+ optional LLM auto-fix)."""
    from workers.secure_code_gate_worker import run_secure_code_gate

    user_id = (
        current_user.get("sub") if isinstance(current_user, dict)
        else getattr(current_user, "sub", "")
    )
    payload = {
        "files":     [f.dict() for f in req.files],
        "threshold": req.threshold,
        "do_fix":    req.fix,
        "user_id":   user_id,
    }
    # The fix loop makes LLM calls — run off the event loop so we don't block.
    return await asyncio.to_thread(run_secure_code_gate, payload)


@router.get("/status/{job_id}")
async def scan_status(job_id: str, current_user=Depends(get_current_user)):
    """Poll an enqueued (deep-tier) scan job."""
    from core.job_queue import get_job_status
    return get_job_status(job_id)
