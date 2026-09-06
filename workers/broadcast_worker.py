# SPDX-License-Identifier: MIT
# ============================================================
# BROADCAST WORKER — sends ONE recipient of an admin email
# broadcast through the AiNxt SMTP relay.
#
# Execution model:
#   Broadcasts are sent only once or twice a month, so a dedicated
#   RQ worker pool was overkill. send_broadcast_recipient now runs
#   on an 8-thread ThreadPoolExecutor *inside the gateway process*
#   (see submit_broadcast_recipient below). The function body is
#   unchanged from the original RQ-job form and remains fully
#   thread-safe:
#     - Each invocation opens its own SessionLocal() and closes it
#       in finally — Sessions are NOT shared across threads.
#     - Counter updates use atomic UPDATE … SET col = col + 1 SQL,
#       not Python read-modify-write.
#     - Finalisation uses a conditional UPDATE so two threads
#       racing on the last recipient cannot double-write the
#       'completed' audit row.
#     - smtp_service.send_html_email opens a fresh smtplib.SMTP
#       connection per call — no shared SMTP socket.
#
# Flow per job:
#   1. Open SessionLocal()
#   2. Load EmailBroadcast + EmailBroadcastRecipient
#   3. If broadcast is cancelled → mark recipient 'skipped' and return
#   4. If enrich_name → substitute {{name}} in html_body / text_body
#   5. Load attachments → read bytes from storage_path
#   6. Call services.smtp_service.send_html_email(...)
#   7. Atomic UPDATE … SET success_count = success_count + 1
#      (or failure_count) — no Python read-modify-write
#   8. Append EmailBroadcastAuditLog row (sent_one)
#   9. When success_count + failure_count == total_count:
#      set broadcast status=completed and write 'completed' audit row.
# ============================================================

from __future__ import annotations

import html as _html
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import update as _sa_update

from core.logger import logger


_DEFAULT_NAME_FALLBACK = "there"

# ── Thread-pool dispatcher ───────────────────────────────────────────────────
# Bounded pool for recipient sends. Tune BROADCAST_THREADS to match your SMTP
# relay's connection limit — 8 is a safe default that leaves headroom for
# other transactional emails the gateway may send concurrently.
_BROADCAST_POOL_SIZE = int(os.getenv("BROADCAST_THREADS", "8"))
_BROADCAST_EXECUTOR = ThreadPoolExecutor(
    max_workers=_BROADCAST_POOL_SIZE,
    thread_name_prefix="broadcast",
)


def _run_one_safe(payload: dict) -> None:
    """Threadpool target — swallows exceptions so one bad recipient
    cannot kill a pool worker thread or poison subsequent jobs."""
    try:
        send_broadcast_recipient(payload)
    except Exception as exc:
        logger.error(
            f"broadcast_worker: thread crashed processing "
            f"broadcast_id={payload.get('broadcast_id')!r} "
            f"recipient_id={payload.get('recipient_id')!r}: {exc}",
            exc_info=True,
        )


def submit_broadcast_recipient(payload: dict) -> None:
    """
    Fire-and-forget submit of one recipient send to the broadcast threadpool.

    payload keys (same shape as the previous RQ job payload):
      broadcast_id  str — EmailBroadcast.id (UUID string)
      recipient_id  str — EmailBroadcastRecipient.id (UUID string)

    Returns immediately. The router does NOT wait for the future — it returns
    the broadcast_id to the UI which polls /broadcast/{id} for progress, exactly
    like the previous RQ-backed flow.
    """
    _BROADCAST_EXECUTOR.submit(_run_one_safe, payload)


def _first_name(full_name: str | None) -> str:
    """Return the recipient's first-name token, HTML-escaped, falling back to 'there'."""
    if not full_name:
        return _DEFAULT_NAME_FALLBACK
    first = (full_name.strip().split() or [_DEFAULT_NAME_FALLBACK])[0]
    return _html.escape(first, quote=True)


def _substitute_name(body: str | None, name_token: str) -> str | None:
    if body is None:
        return None
    return body.replace("{{name}}", name_token)


def _load_attachments(db, broadcast_id: str) -> List[Dict[str, Any]]:
    """Read all attachment bytes for a broadcast. Skips any file that cannot be opened."""
    from db.models import EmailBroadcastAttachment

    rows = (
        db.query(EmailBroadcastAttachment)
        .filter(EmailBroadcastAttachment.broadcast_id == broadcast_id)
        .all()
    )
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            with open(row.storage_path, "rb") as fh:
                data = fh.read()
            out.append({
                "filename": row.filename,
                "content":  data,
                "mimetype": row.mimetype or "application/octet-stream",
            })
        except Exception as exc:
            logger.warning(
                f"broadcast_worker: failed to read attachment {row.id} "
                f"path={row.storage_path!r}: {exc}"
            )
    return out


def _finalize_if_done(db, broadcast_id: str) -> None:
    """
    If success_count + failure_count == total_count → set status=completed
    and write ONE completed audit row.

    Concurrency: two workers can race to finalize the last recipient. We
    serialise with a conditional UPDATE — only the worker whose UPDATE
    actually flips status from sending/queued to completed writes the
    audit row. Other concurrent finalisers see status='completed' and exit.
    """
    from db.models import EmailBroadcast, EmailBroadcastAuditLog

    bc = db.query(EmailBroadcast).filter(EmailBroadcast.id == broadcast_id).first()
    if bc is None:
        return
    if bc.status in ("completed", "cancelled", "failed"):
        return
    if bc.success_count + bc.failure_count < bc.total_count:
        return

    # Conditional UPDATE — only the row whose status is still queued/sending
    # gets flipped. rowcount > 0 means "we won the race".
    result = db.execute(
        _sa_update(EmailBroadcast)
        .where(
            EmailBroadcast.id == broadcast_id,
            EmailBroadcast.status.in_(("queued", "sending")),
        )
        .values(status="completed", updated_at=datetime.utcnow())
    )
    db.commit()
    if (result.rowcount or 0) == 0:
        # Another worker already finalised it — no audit row needed.
        return

    db.add(EmailBroadcastAuditLog(
        broadcast_id=broadcast_id,
        actor_user_id=None,
        actor_email=None,
        action="completed",
        detail_json={
            "total_count":   bc.total_count,
            "success_count": bc.success_count,
            "failure_count": bc.failure_count,
        },
    ))
    db.commit()


def send_broadcast_recipient(payload: dict) -> None:
    """
    Sends ONE recipient. Called by the broadcast ThreadPoolExecutor
    (see submit_broadcast_recipient) — runs inside the gateway process.

    Thread-safety: opens its own SessionLocal() and closes it in finally,
    uses atomic SQL UPDATEs for counter increments, and relies on a
    conditional UPDATE for race-safe broadcast finalisation. No mutable
    module-level state is shared across threads.

    payload keys:
      broadcast_id  str — EmailBroadcast.id (UUID string)
      recipient_id  str — EmailBroadcastRecipient.id (UUID string)
    """
    broadcast_id = payload.get("broadcast_id") or ""
    recipient_id = payload.get("recipient_id") or ""
    if not broadcast_id or not recipient_id:
        logger.error(
            f"broadcast_worker: invalid payload (missing ids) "
            f"broadcast_id={broadcast_id!r} recipient_id={recipient_id!r}"
        )
        return

    from db.database import SessionLocal
    from db.models import (
        EmailBroadcast,
        EmailBroadcastRecipient,
        EmailBroadcastAuditLog,
    )
    from services.smtp_service import send_html_email, SMTPSendError

    db = SessionLocal()
    try:
        bc = db.query(EmailBroadcast).filter(EmailBroadcast.id == broadcast_id).first()
        rcpt = (
            db.query(EmailBroadcastRecipient)
            .filter(EmailBroadcastRecipient.id == recipient_id)
            .first()
        )
        if bc is None or rcpt is None:
            logger.error(
                f"broadcast_worker: broadcast or recipient missing "
                f"broadcast_id={broadcast_id} recipient_id={recipient_id}"
            )
            return

        # Skip if broadcast has been cancelled — mark recipient 'skipped'.
        if bc.status == "cancelled":
            if rcpt.status == "pending":
                rcpt.status     = "skipped"
                rcpt.error_text = "Broadcast cancelled before send"
                db.commit()
            return

        # If recipient is no longer pending (e.g. concurrent worker raced us),
        # don't double-send.
        if rcpt.status != "pending":
            return

        # Flip broadcast to 'sending' once the first recipient starts.
        if bc.status == "queued":
            bc.status     = "sending"
            bc.updated_at = datetime.utcnow()
            db.commit()

        # ── Body assembly (with optional name enrichment) ────────────────
        if bc.enrich_name:
            name_token = _first_name(rcpt.name)
            html_body = _substitute_name(bc.html_body, name_token) or bc.html_body
            text_body = _substitute_name(bc.text_body, name_token) if bc.text_body else None
        else:
            html_body = bc.html_body
            text_body = bc.text_body

        attachments = _load_attachments(db, broadcast_id)

        # ── Send via AiNxt internal SMTP relay ────────────────────────────
        send_ok    = False
        send_error: str | None = None
        try:
            send_ok = send_html_email(
                to=[rcpt.email],
                subject=bc.subject,
                html_body=html_body,
                text_body=text_body,
                attachments=attachments or None,
            )
            if not send_ok:
                send_error = "SMTP relay returned False"
        except SMTPSendError as exc:
            send_error = f"SMTP relay refused: {exc}"
        except Exception as exc:
            send_error = f"SMTP send raised: {exc}"

        # ── Persist recipient outcome ────────────────────────────────────
        now = datetime.utcnow()
        if send_ok:
            rcpt.status  = "sent"
            rcpt.sent_at = now
            rcpt.error_text = None
            counter_col = EmailBroadcast.success_count
        else:
            rcpt.status     = "failed"
            rcpt.error_text = (send_error or "Unknown error")[:2000]
            counter_col = EmailBroadcast.failure_count

        # ── Atomic counter increment (UPDATE … SET col = col + 1) ────────
        db.execute(
            _sa_update(EmailBroadcast)
            .where(EmailBroadcast.id == broadcast_id)
            .values({counter_col: counter_col + 1, EmailBroadcast.updated_at: now})
        )

        # ── Audit row ────────────────────────────────────────────────────
        db.add(EmailBroadcastAuditLog(
            broadcast_id=broadcast_id,
            actor_user_id=None,
            actor_email=rcpt.email,
            action="sent_one",
            detail_json={
                "recipient_id": recipient_id,
                "status":       rcpt.status,
                "error":        rcpt.error_text,
            },
        ))
        db.commit()

        # ── Finalize broadcast if all rows accounted for ─────────────────
        _finalize_if_done(db, broadcast_id)

    except Exception as exc:
        logger.error(
            f"broadcast_worker: unhandled error broadcast={broadcast_id} "
            f"recipient={recipient_id} err={exc}",
            exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        try:
            db.close()
        except Exception:
            pass
