# SPDX-License-Identifier: Apache-2.0
# ============================================================
# KB INGEST WORKER — rq job that runs activate_doc() post-approval.
#
# Flow:
#   1. Router sets doc status → INDEXING and enqueues this job to kb_queue
#   2. This worker calls activate_doc(doc_id, approved_by)
#      — Docling ML parse, chunk, embed, pgvector INSERT (no HTTP timeout)
#   3. On success  → set status = ACTIVE, send Inbox notification to uploader
#   4. On failure  → set status = PENDING_APPROVAL (rollback), log error
#
# Picked up by:
#   python workers/start_workers.py --kb --n 5
#
# Queue   : kb_queue  (Q_KB in core/job_queue.py)
# Timeout : no RQ hard cap — stage-level HTTP timeouts still apply
# Retries : 1 retry after 120 s (configured at enqueue time in docs_router.py)
# ============================================================

import time as _time

from core.logger import logger


# ── Public RQ entry point ─────────────────────────────────────────────────────

def run_activate_doc(payload: dict) -> dict:
    """
    RQ entry point consumed by kb_queue workers.

    Expected payload keys:
        doc_id      (str)  — UUID of the KnowledgeDocument row
        approved_by (str)  — email of the approver
        doc_name    (str)  — human-readable document name (for Inbox notification)

    Returns:
        {"success": True,  "chunk_count": N}   on success
        {"success": False, "error": "..."}     on failure
    """
    doc_id      = payload.get("doc_id")
    approved_by = payload.get("approved_by", "approver")
    doc_name    = payload.get("doc_name") or doc_id

    if not doc_id:
        logger.error("[KB_WORKER][step=start][ERROR] payload missing doc_id — aborting")
        return {"success": False, "error": "payload missing doc_id"}

    _start = _time.perf_counter()
    logger.info(
        f"[KB_WORKER][step=start] doc_id={doc_id} "
        f"approved_by={approved_by} doc_name='{doc_name}'"
    )

    # ── Step 1: run activate_doc (Docling parse → chunk → embed → pgvector) ──
    try:
        from store.docs_store import activate_doc as _activate
        result = _activate(doc_id=doc_id, approved_by=approved_by)
    except Exception as exc:
        _elapsed_ms = (_time.perf_counter() - _start) * 1000
        logger.error(
            f"[KB_WORKER][step=activate_doc][ERROR] doc_id={doc_id} "
            f"elapsed={_elapsed_ms:.0f}ms error='{exc}'"
        )
        try:
            from db.database import SessionLocal as _SL_cancel_chk
            from db.models import KnowledgeDocument as _KD_cancel_chk
            _db_cancel_chk = _SL_cancel_chk()
            try:
                _doc_cancel_chk = _db_cancel_chk.get(_KD_cancel_chk, doc_id)
                if not _doc_cancel_chk or _doc_cancel_chk.status in ("DELETING", "DELETED"):
                    logger.info(
                        f"[KB_WORKER][step=activate_doc][CANCELLED] doc_id={doc_id} "
                        f"activation error occurred after document delete"
                    )
                    return {"success": False, "cancelled": True, "error": "Document deleted during activation"}
            finally:
                _db_cancel_chk.close()
        except Exception as _cancel_chk_exc:
            logger.warning(
                f"[KB_WORKER][step=activate_doc][WARN] doc_id={doc_id} "
                f"cancel check failed: {_cancel_chk_exc}"
            )
        _rollback_status(doc_id, "PENDING_APPROVAL", error=str(exc))
        return {"success": False, "error": str(exc)}

    _elapsed_ms = (_time.perf_counter() - _start) * 1000

    if result.get("cancelled"):
        logger.info(
            f"[KB_WORKER][step=activate_doc][CANCELLED] doc_id={doc_id} "
            f"elapsed={_elapsed_ms:.0f}ms — document was deleted during activation"
        )
        return result

    # ── Step 2: handle activate_doc failure ───────────────────────────────────
    if not result.get("success"):
        try:
            from db.database import SessionLocal as _SL_fail_chk
            from db.models import KnowledgeDocument as _KD_fail_chk
            _db_fail_chk = _SL_fail_chk()
            try:
                _doc_fail_chk = _db_fail_chk.get(_KD_fail_chk, doc_id)
                if not _doc_fail_chk or _doc_fail_chk.status in ("DELETING", "DELETED"):
                    logger.info(
                        f"[KB_WORKER][step=activate_doc][CANCELLED] doc_id={doc_id} "
                        f"elapsed={_elapsed_ms:.0f}ms — activation stopped after document delete"
                    )
                    return {"success": False, "cancelled": True, "error": "Document deleted during activation"}
            finally:
                _db_fail_chk.close()
        except Exception as _fail_chk_exc:
            logger.warning(
                f"[KB_WORKER][step=activate_doc][WARN] doc_id={doc_id} "
                f"failure cancel check failed: {_fail_chk_exc}"
            )
        logger.error(
            f"[KB_WORKER][step=activate_doc][FAILED] doc_id={doc_id} "
            f"elapsed={_elapsed_ms:.0f}ms error='{result.get('error')}'"
        )
        # Guard: do NOT rollback if activate_doc already set a terminal status
        # (e.g. REJECTED after compliance block on scanned PDF OCR text).
        # Overwriting REJECTED with PENDING_APPROVAL would allow re-approval
        # of a compliance-blocked document.
        _should_rollback = True
        try:
            from db.database import SessionLocal as _SL_chk
            from db.models import KnowledgeDocument as _KD_chk
            _db_chk = _SL_chk()
            try:
                _doc_chk = _db_chk.get(_KD_chk, doc_id)
                if _doc_chk and _doc_chk.status in ("REJECTED", "DEPRECATED"):
                    _should_rollback = False
                    logger.info(
                        f"[KB_WORKER][step=activate_doc][SKIP_ROLLBACK] doc_id={doc_id} "
                        f"current_status={_doc_chk.status} — not rolling back to PENDING_APPROVAL"
                    )
            finally:
                _db_chk.close()
        except Exception as _chk_exc:
            logger.warning(
                f"[KB_WORKER][step=activate_doc][WARN] doc_id={doc_id} "
                f"could not check current status before rollback: {_chk_exc} — rolling back anyway"
            )

        if _should_rollback:
            _rollback_status(doc_id, "PENDING_APPROVAL", error=result.get("error", ""))
        return result

    # ── Step 3: confirm ACTIVE (set early inside activate_doc post-pgvector) ──
    # activate_doc() now flips status → ACTIVE immediately after pgvector_write
    # so the UI reflects the correct state without waiting for the MD file write.
    # We call _set_status() here as a safety net in case the early flip inside
    # activate_doc failed (e.g. transient DB error) — it is idempotent.
    _set_status(doc_id, "ACTIVE")
    logger.info(
        f"[KB_WORKER][step=complete] doc_id={doc_id} "
        f"chunks={result.get('chunk_count', 0)} "
        f"elapsed={_elapsed_ms:.0f}ms status=ACTIVE"
    )

    # ── Step 4: delete original binary — only .md is needed after activation ──
    # The original PDF/DOCX/PPTX/HTML was needed only for Docling parsing.
    # Now that parsing is done and the .md is written to KB_DOC_STORAGE_PATH,
    # the binary is dead weight. Delete it to keep storage clean.
    _delete_original_file(doc_id, approved_by)

    # ── Step 5: notify uploader via Inbox ────────────────────────────────────
    _notify_uploader(doc_id, doc_name, approved_by)

    return result


# ── Internal helpers ──────────────────────────────────────────────────────────

def _set_status(doc_id: str, status: str) -> None:
    """
    Update knowledge_docs.status on PGS01.
    Called both on success (→ ACTIVE) and on failure (→ PENDING_APPROVAL).
    """
    try:
        from db.database import SessionLocal
        from db.models import KnowledgeDocument
        db = SessionLocal()
        try:
            doc = db.get(KnowledgeDocument, doc_id)
            if doc:
                doc.status = status
                db.commit()
                logger.info(
                    f"[KB_WORKER][step=set_status] doc_id={doc_id} status={status}"
                )
            else:
                logger.warning(
                    f"[KB_WORKER][step=set_status][WARN] doc_id={doc_id} "
                    f"doc not found — cannot set status={status}"
                )
        finally:
            db.close()
    except Exception as exc:
        logger.error(
            f"[KB_WORKER][step=set_status][ERROR] doc_id={doc_id} "
            f"status={status} error='{exc}'"
        )


def _rollback_status(doc_id: str, status: str, error: str = "") -> None:
    """
    Roll back document status on failure so the approver can retry.
    Persists the error message on knowledge_docs.parse_error so the UI can
    surface the exact failure reason instead of silently showing PENDING_APPROVAL.
    Logs a WARNING so ops can distinguish rollbacks from normal status updates.

    The persisted message is passed through sanitize_user_error() first. This is
    the single write point for parse_error on the activation-failure path, so
    scrubbing here guarantees no internal implementation detail (model names such
    as Docling / PaddleOCR / Ollama, service URLs, absolute file paths, env-var
    names, Python exception classes, stack traces) can ever reach the Request
    Status tab in the UI. Necessary as a safety net because `error` is often a
    bare str(exc) from an arbitrary third-party exception.

    The WARNING log below keeps the RAW, unsanitized message — ops need the full
    internal detail for diagnosis. Only the user-visible column is sanitized.
    """
    logger.warning(
        f"[KB_WORKER][step=rollback] doc_id={doc_id} "
        f"rolling_back_to={status}"
        + (f" error='{error}'" if error else "")
    )
    try:
        from db.database import SessionLocal
        from db.models import KnowledgeDocument

        # Scrub internal detail before it becomes user-visible.
        _user_error = ""
        if error:
            try:
                from core.user_error_messages import sanitize_user_error
                _user_error = sanitize_user_error(error)
            except Exception as _san_exc:
                # Sanitizer unavailable/broken — never block the rollback. Store
                # a safe generic message rather than risk leaking the raw text.
                logger.warning(
                    f"[KB_WORKER][step=rollback][WARN] doc_id={doc_id} "
                    f"error sanitization failed: {_san_exc} — storing generic message"
                )
                _user_error = (
                    "Document processing failed. Please re-upload the document "
                    "or contact support if the issue persists."
                )

        db = SessionLocal()
        try:
            doc = db.get(KnowledgeDocument, doc_id)
            if doc:
                doc.status = status
                doc.parse_error = _user_error or None
                db.commit()
                logger.info(
                    f"[KB_WORKER][step=rollback] doc_id={doc_id} "
                    f"status={status} parse_error stored "
                    f"user_message='{_user_error}'"
                )
            else:
                logger.warning(
                    f"[KB_WORKER][step=rollback][WARN] doc_id={doc_id} "
                    f"doc not found — cannot rollback status"
                )
        finally:
            db.close()
    except Exception as exc:
        logger.error(
            f"[KB_WORKER][step=rollback][ERROR] doc_id={doc_id} "
            f"error='{exc}' — falling back to _set_status"
        )
        _set_status(doc_id, status)


def _delete_original_file(doc_id: str, approved_by: str) -> None:
    """
    Delete the original binary file (PDF/DOCX/PPTX/HTML) from KB_DOC_STORAGE_PATH
    after successful activation.

    The binary was only needed for Docling parsing during activate_doc().
    After activation the canonical .md file at KB_DOC_STORAGE_PATH/<doc_id>.md
    is the only file needed for RAG — the original binary is dead weight.

    Non-fatal: any failure is logged as WARNING and swallowed so it never
    blocks the worker from completing successfully.
    """
    try:
        import os as _os
        from db.database import SessionLocal
        from db.models import KnowledgeDocument
        from core.config import KB_DOC_STORAGE_PATH

        db = SessionLocal()
        try:
            doc = db.get(KnowledgeDocument, doc_id)
            if not doc or not doc.original_ext:
                logger.info(
                    f"[KB_WORKER][step=delete_original] doc_id={doc_id} "
                    f"no original_ext — nothing to delete"
                )
                return

            _orig_path = _os.path.join(KB_DOC_STORAGE_PATH, f"{doc_id}.{doc.original_ext}")
            if _os.path.isfile(_orig_path):
                _os.remove(_orig_path)
                logger.info(
                    f"[KB_WORKER][step=delete_original] doc_id={doc_id} "
                    f"deleted '{_orig_path}' ext={doc.original_ext} — .md retained"
                )
            else:
                logger.info(
                    f"[KB_WORKER][step=delete_original] doc_id={doc_id} "
                    f"original file not found at '{_orig_path}' — already deleted or never saved"
                )
            try:
                from store.kb_replication import delete_file as _delete_replica_file
                _delete_replica_file(doc_id, doc.original_ext, kind="original")
            except Exception as _rep_del_exc:
                logger.warning(
                    f"[KB_WORKER][step=delete_original][WARN] doc_id={doc_id} "
                    f"replica delete failed: {_rep_del_exc}"
                )
        finally:
            db.close()

    except Exception as exc:
        logger.warning(
            f"[KB_WORKER][step=delete_original][WARN] doc_id={doc_id} "
            f"error='{exc}' (non-fatal — .md and embeddings are intact)"
        )


def _notify_uploader(doc_id: str, doc_name: str, approved_by: str) -> None:
    """
    Send an Inbox notification to the document uploader once the doc is ACTIVE.

    This fires AFTER pgvector INSERT is complete — the uploader is only notified
    when the document is truly searchable, not at approval time.
    Non-fatal: any exception is logged as a WARNING and swallowed so it never
    blocks the worker from returning success.
    """
    try:
        from db.database import SessionLocal
        from db.models import KnowledgeDocument, User
        from store.inbox_store import publish_inbox_item
        from datetime import datetime, timezone, timedelta

        ist_now = (
            datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        ).strftime("%d %b %Y, %I:%M %p IST")

        db = SessionLocal()
        try:
            doc = db.get(KnowledgeDocument, doc_id)
            if not doc:
                logger.warning(
                    f"[KB_WORKER][step=notify_uploader][WARN] doc_id={doc_id} "
                    f"doc not found — skipping Inbox notification"
                )
                return
            if not doc.uploaded_by:
                logger.info(
                    f"[KB_WORKER][step=notify_uploader] doc_id={doc_id} "
                    f"no uploaded_by — skipping Inbox notification"
                )
                return

            uploader = (
                db.query(User)
                .filter(User.email == doc.uploaded_by)
                .first()
            )
            if not uploader:
                logger.warning(
                    f"[KB_WORKER][step=notify_uploader][WARN] doc_id={doc_id} "
                    f"uploader email={doc.uploaded_by} not found in users table"
                )
                return

            publish_inbox_item(
                user_id=str(uploader.id),
                type="kb_approval",
                title=f"[KB Active] {doc_name}",
                body=(
                    f"Your document **{doc_name}** was **approved** by "
                    f"`{approved_by}` on {ist_now} and is now **indexed and "
                    f"searchable** in the knowledge base."
                ),
                source_id=doc_id,
                metadata={
                    "entity_id":   doc_id,
                    "status":      "ACTIVE",
                    "approved_by": approved_by,
                },
            )
            logger.info(
                f"[KB_WORKER][step=notify_uploader] doc_id={doc_id} "
                f"uploader={doc.uploaded_by} notification_sent=true"
            )
        finally:
            db.close()

    except Exception as exc:
        logger.warning(
            f"[KB_WORKER][step=notify_uploader][WARN] doc_id={doc_id} "
            f"error='{exc}' (non-fatal — doc is ACTIVE, notification skipped)"
        )
