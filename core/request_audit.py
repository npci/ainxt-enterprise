# SPDX-License-Identifier: MIT
"""
Shared request-audit writer.

`request_audit_log` is meant to hold "one row per /ask AND /ide/chat request"
(see db.models.RequestAuditLog), but historically only /ask wrote rows. This
helper lets the IDE router (and any other client path) record usage too, so
client_source telemetry (cli / ide-vscode / ide-jetbrains) is complete.

Fire-and-forget: never raises into the request path.
"""
from __future__ import annotations

import hashlib
import threading


def record_audit(
    *,
    user_id: str,
    client_source: str,
    endpoint: str,
    request_id: str = "",
    email: str = "",
    department: str = "",
    question: str = "",
    model_used: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    latency_ms: int = 0,
    cache_hit: str = "none",
    compliance_blocked: bool = False,
    error: str = "",
) -> None:
    """Write one request_audit_log row on a background thread."""
    def _write():
        try:
            from db.database import SessionLocal
            from db.models import RequestAuditLog
            db = SessionLocal()
            try:
                q_hash = hashlib.sha256(question.encode()).hexdigest() if question else None
                db.add(RequestAuditLog(
                    request_id=request_id or "",
                    user_id=user_id or "anonymous",
                    email=email or None,
                    department=department or None,
                    client_source=client_source or "platform",
                    endpoint=endpoint,
                    question_hash=q_hash,
                    model_used=model_used or None,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost_usd,
                    latency_ms=latency_ms,
                    cache_hit=cache_hit or "none",
                    compliance_blocked=compliance_blocked,
                    error=error or None,
                ))
                db.commit()
            finally:
                db.close()
        except Exception:
            pass  # audit must never affect the user response

    threading.Thread(target=_write, daemon=True).start()
