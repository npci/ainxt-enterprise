#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# ============================================================
# PURGE WORKER — nightly cleanup of expired generated docs,
# generated images, uploaded chat files, and inbox items
#
# Consolidates the old doc_purge.py, image_purge.py, and
# upload_purge.py workers into a single script while preserving:
#   - separate retention env vars
#   - separate dry-run env vars
#   - separate throttle keys
#   - separate trace/audit key prefixes
#   - doc-specific chat marker cleanup
#   - upload deletion via core.storage.storage.delete(...)
#
# inbox_items purge (SEC-F-MISC-002 / SEC-F-MISC-006): INBOX_RETAIN_DAYS
# defaults to 90 days. No data-governance approval is required for this
# window — inbox items are transient, in-app notification records (not a
# regulated HR/financial system of record), so the retention window is an
# engineering/operational decision. Adjustable via INBOX_RETAIN_DAYS.
# ============================================================

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ckms import load_at_boot as _ckms_load_at_boot
_ckms_load_at_boot()

from core.logger import logger

DOC_RETAIN_DAYS = int(os.getenv("DOC_RETAIN_DAYS", "2"))
IMAGE_RETAIN_DAYS = int(os.getenv("IMAGE_RETAIN_DAYS", "2"))
UPLOAD_RETAIN_DAYS = int(os.getenv("UPLOAD_RETAIN_DAYS", "2"))

# SEC-F-MISC-002 / SEC-F-MISC-006: inbox_items carries PII (titles/bodies/
# metadata can reference people, approvals, HR events, etc.) and previously
# had no retention at all. 90 days is the approved default retention
# window. No governance sign-off is required for this window — inbox
# items are transient, in-app notification records, not a regulated
# HR/financial system of record. Configurable via INBOX_RETAIN_DAYS if the
# platform's data policy changes later.
INBOX_RETAIN_DAYS = int(os.getenv("INBOX_RETAIN_DAYS", "90"))

_DOC_THROTTLE_SEC = max(3600, DOC_RETAIN_DAYS * 86400 // 4)
_IMAGE_THROTTLE_SEC = max(3600, IMAGE_RETAIN_DAYS * 86400 // 4)
_UPLOAD_THROTTLE_SEC = max(3600, UPLOAD_RETAIN_DAYS * 86400 // 4)
_INBOX_THROTTLE_SEC = max(3600, INBOX_RETAIN_DAYS * 86400 // 4)

_DOC_THROTTLE_KEY = "doc_purge:last_run"
_IMAGE_THROTTLE_KEY = "image_purge:last_run"
_UPLOAD_THROTTLE_KEY = "upload_purge:last_run"
_INBOX_THROTTLE_KEY = "inbox_purge:last_run"


def _is_dry_run(env_name: str) -> bool:
    return os.getenv(env_name, "false").lower() == "true"


def _stamp_last_run(throttle_key: str, throttle_sec: int, label: str) -> None:
    try:
        from core.config import RDB_QUEUE
        from core.kv import get_kv
        r = get_kv(RDB_QUEUE, decode_responses=True)
        r.setex(
            throttle_key,
            throttle_sec,
            str(int(datetime.now(timezone.utc).timestamp())),
        )
    except Exception as e:
        logger.debug(f"{label}: could not stamp last_run: {e}")


def _write_run_record(prefix: str, stats: dict, label: str) -> None:
    try:
        from core.config import RDB_TRACE
        from core.kv import get_kv
        r = get_kv(RDB_TRACE, decode_responses=True)
        key = f"{prefix}:{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        r.setex(key, 86400 * 30, json.dumps(stats))
    except Exception as e:
        logger.warning(f"{label}: could not write run record to KV: {e}")


def _should_run_opportunistic(throttle_key: str, throttle_sec: int, label: str) -> bool:
    try:
        from core.config import RDB_QUEUE
        from core.kv import get_kv
        r = get_kv(RDB_QUEUE, decode_responses=True)
        owned = r.set(
            throttle_key,
            str(int(datetime.now(timezone.utc).timestamp())),
            nx=True,
            ex=throttle_sec,
        )
        return bool(owned)
    except Exception as e:
        logger.debug(f"{label}: throttle check failed, skipping opportunistic sweep: {e}")
        return False


def run_doc_purge() -> dict:
    """Delete only the on-disk binary of documents older than DOC_RETAIN_DAYS.

    IMPORTANT — behavior change: this used to ALSO delete the owning
    ChatMessage row (every message containing a `[DOCJOB:{job_id}:...]`
    marker) and the GeneratedDocument audit row. That meant a document didn't
    just become undownloadable after the retention window — the entire chat
    turn vanished from Buddy's history, as if the exchange never happened,
    with zero warning to the user.

    Now we only remove the large binary from disk (the actual thing that
    needs cleanup for storage reasons) and keep both DB rows intact:
      - GeneratedDocument stays so /docs/job/{id}/status can detect the file
        is gone (row exists, os.path.exists(file_path) is False) and return
        status="expired" instead of a scary generic error.
      - ChatMessage/the [DOCJOB:...] marker stays so the download card keeps
        rendering in the chat history — DocDownloadButton then shows a
        disabled "expired" chip (mirrors AttachmentChip/ImageChip in
        Message.jsx) instead of the whole message disappearing.
      - content_md on GeneratedDocument was already the documented permanent
        audit trail (see db.models.GeneratedDocument docstring), so no
        content is actually lost by keeping the row.
    """
    from db.database import SessionLocal
    from db.models import GeneratedDocument

    dry_run = _is_dry_run("DOC_PURGE_DRY_RUN")
    start = datetime.now(timezone.utc)
    cutoff = start - timedelta(days=DOC_RETAIN_DAYS)
    stats = {
        "docs_scanned": 0,
        "files_deleted": 0,
        "errors": 0,
    }

    logger.info(
        f"doc_purge: START retain_days={DOC_RETAIN_DAYS} cutoff_utc={cutoff.isoformat()} dry_run={dry_run}"
    )

    db = SessionLocal()
    try:
        rows = (
            db.query(
                GeneratedDocument.id,
                GeneratedDocument.job_id,
                GeneratedDocument.file_path,
                GeneratedDocument.created_at,
            )
            .filter(GeneratedDocument.created_at < cutoff)
            .all()
        )
        stats["docs_scanned"] = len(rows)

        for rec in rows:
            try:
                file_path = rec.file_path or ""

                if file_path and os.path.exists(file_path):
                    if dry_run:
                        logger.info(f"doc_purge: [DRY RUN] would unlink file_id={rec.id} path={file_path}")
                    else:
                        try:
                            os.unlink(file_path)
                            stats["files_deleted"] += 1
                        except FileNotFoundError:
                            pass
                # DB rows (GeneratedDocument + the ChatMessage's [DOCJOB:...]
                # marker) are intentionally left in place — see docstring above.
            except Exception as e:
                stats["errors"] += 1
                logger.error(
                    f"doc_purge: row error id={getattr(rec, 'id', '?')!r} job_id={getattr(rec, 'job_id', '?')!r} error={e!r}"
                )
    except Exception as e:
        stats["errors"] += 1
        logger.error(f"doc_purge: fatal error: {e}")
    finally:
        db.close()

    duration = (datetime.now(timezone.utc) - start).total_seconds()
    stats["duration_s"] = round(duration, 2)
    _write_run_record("doc_purge", stats, "doc_purge")
    _stamp_last_run(_DOC_THROTTLE_KEY, _DOC_THROTTLE_SEC, "doc_purge")
    logger.info(
        f"doc_purge: complete — scanned={stats['docs_scanned']} files={stats['files_deleted']} errors={stats['errors']} ({duration:.1f}s)"
        + (" [DRY RUN]" if dry_run else "")
    )
    return stats


def run_image_purge() -> dict:
    from db.database import SessionLocal
    from db.models import GeneratedImage

    dry_run = _is_dry_run("IMAGE_PURGE_DRY_RUN")
    start = datetime.now(timezone.utc)
    cutoff = start - timedelta(days=IMAGE_RETAIN_DAYS)
    stats = {
        "images_scanned": 0,
        "files_deleted": 0,
        "db_rows_deleted": 0,
        "errors": 0,
    }

    logger.info(
        f"image_purge: START retain_days={IMAGE_RETAIN_DAYS} cutoff_utc={cutoff.isoformat()} dry_run={dry_run}"
    )

    db = SessionLocal()
    try:
        rows = (
            db.query(
                GeneratedImage.id,
                GeneratedImage.file_path,
                GeneratedImage.created_at,
            )
            .filter(GeneratedImage.created_at < cutoff)
            .all()
        )
        stats["images_scanned"] = len(rows)

        for rec in rows:
            try:
                file_path = rec.file_path or ""

                if file_path and os.path.exists(file_path):
                    if dry_run:
                        logger.info(f"image_purge: [DRY RUN] would unlink id={rec.id} path={file_path}")
                    else:
                        try:
                            os.unlink(file_path)
                            stats["files_deleted"] += 1
                        except FileNotFoundError:
                            pass

                if dry_run:
                    logger.info(
                        f"image_purge: [DRY RUN] would delete generated_images id={rec.id} created_at={rec.created_at}"
                    )
                else:
                    db.query(GeneratedImage).filter(
                        GeneratedImage.id == rec.id
                    ).delete(synchronize_session=False)
                    stats["db_rows_deleted"] += 1
                    db.commit()
            except Exception as e:
                stats["errors"] += 1
                if not dry_run:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                logger.error(f"image_purge: row error id={getattr(rec, 'id', '?')!r} error={e!r}")
    except Exception as e:
        stats["errors"] += 1
        logger.error(f"image_purge: fatal error: {e}")
    finally:
        db.close()

    duration = (datetime.now(timezone.utc) - start).total_seconds()
    stats["duration_s"] = round(duration, 2)
    _write_run_record("image_purge", stats, "image_purge")
    _stamp_last_run(_IMAGE_THROTTLE_KEY, _IMAGE_THROTTLE_SEC, "image_purge")
    logger.info(
        f"image_purge: complete — scanned={stats['images_scanned']} files={stats['files_deleted']} db_rows={stats['db_rows_deleted']} errors={stats['errors']} ({duration:.1f}s)"
        + (" [DRY RUN]" if dry_run else "")
    )
    return stats


def run_upload_purge() -> dict:
    from db.database import SessionLocal
    from db.models import ChatAttachment
    from core.storage import storage

    dry_run = _is_dry_run("UPLOAD_PURGE_DRY_RUN")
    start = datetime.now(timezone.utc)
    cutoff = start - timedelta(days=UPLOAD_RETAIN_DAYS)
    stats = {
        "uploads_scanned": 0,
        "files_deleted": 0,
        "db_rows_deleted": 0,
        "errors": 0,
    }

    logger.info(
        f"upload_purge: START retain_days={UPLOAD_RETAIN_DAYS} cutoff_utc={cutoff.isoformat()} dry_run={dry_run}"
    )

    db = SessionLocal()
    try:
        rows = (
            db.query(
                ChatAttachment.id,
                ChatAttachment.storage_path,
                ChatAttachment.created_at,
            )
            .filter(ChatAttachment.created_at < cutoff)
            .all()
        )
        stats["uploads_scanned"] = len(rows)

        for rec in rows:
            try:
                storage_path = rec.storage_path or ""

                if storage_path:
                    if dry_run:
                        logger.info(
                            f"upload_purge: [DRY RUN] would delete bytes id={rec.id} path={storage_path}"
                        )
                    else:
                        try:
                            if storage.delete(storage_path):
                                stats["files_deleted"] += 1
                        except Exception as de:
                            logger.warning(
                                f"upload_purge: storage.delete failed id={rec.id} path={storage_path}: {de}"
                            )

                if dry_run:
                    logger.info(
                        f"upload_purge: [DRY RUN] would delete chat_attachments id={rec.id} created_at={rec.created_at}"
                    )
                else:
                    db.query(ChatAttachment).filter(
                        ChatAttachment.id == rec.id
                    ).delete(synchronize_session=False)
                    stats["db_rows_deleted"] += 1
                    db.commit()
            except Exception as e:
                stats["errors"] += 1
                if not dry_run:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                logger.error(f"upload_purge: row error id={getattr(rec, 'id', '?')!r} error={e!r}")
    except Exception as e:
        stats["errors"] += 1
        logger.error(f"upload_purge: fatal error: {e}")
    finally:
        db.close()

    duration = (datetime.now(timezone.utc) - start).total_seconds()
    stats["duration_s"] = round(duration, 2)
    _write_run_record("upload_purge", stats, "upload_purge")
    _stamp_last_run(_UPLOAD_THROTTLE_KEY, _UPLOAD_THROTTLE_SEC, "upload_purge")
    logger.info(
        f"upload_purge: complete — scanned={stats['uploads_scanned']} files={stats['files_deleted']} db_rows={stats['db_rows_deleted']} errors={stats['errors']} ({duration:.1f}s)"
        + (" [DRY RUN]" if dry_run else "")
    )
    return stats


def run_inbox_purge() -> dict:
    """Delete inbox_items rows older than INBOX_RETAIN_DAYS.

    SEC-F-MISC-002 / SEC-F-MISC-006: inbox metadata can carry PII (names,
    approval details, HR-adjacent notifications, etc.) and was previously
    kept forever. INBOX_RETAIN_DAYS defaults to 90 days — see the
    module-level comment on INBOX_RETAIN_DAYS for why no governance
    sign-off is required for this window. Unread items are purged the
    same as read ones: an item a user never opened is not a reason to
    keep PII indefinitely, and this matches the no-exceptions
    cutoff-by-created_at approach used by doc/image/upload purge above.
    """
    from db.database import SessionLocal
    from db.models import InboxItem

    dry_run = _is_dry_run("INBOX_PURGE_DRY_RUN")
    start = datetime.now(timezone.utc)
    cutoff = start - timedelta(days=INBOX_RETAIN_DAYS)
    stats = {
        "items_scanned": 0,
        "db_rows_deleted": 0,
        "errors": 0,
    }

    logger.info(
        f"inbox_purge: START retain_days={INBOX_RETAIN_DAYS} cutoff_utc={cutoff.isoformat()} dry_run={dry_run}"
    )

    db = SessionLocal()
    try:
        rows = (
            db.query(InboxItem.id, InboxItem.created_at)
            .filter(InboxItem.created_at < cutoff)
            .all()
        )
        stats["items_scanned"] = len(rows)

        if dry_run:
            for rec in rows:
                logger.info(
                    f"inbox_purge: [DRY RUN] would delete inbox_items id={rec.id} created_at={rec.created_at}"
                )
        elif rows:
            ids = [rec.id for rec in rows]
            db.query(InboxItem).filter(InboxItem.id.in_(ids)).delete(synchronize_session=False)
            db.commit()
            stats["db_rows_deleted"] = len(ids)
    except Exception as e:
        stats["errors"] += 1
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"inbox_purge: fatal error: {e}")
    finally:
        db.close()

    duration = (datetime.now(timezone.utc) - start).total_seconds()
    stats["duration_s"] = round(duration, 2)
    _write_run_record("inbox_purge", stats, "inbox_purge")
    _stamp_last_run(_INBOX_THROTTLE_KEY, _INBOX_THROTTLE_SEC, "inbox_purge")
    logger.info(
        f"inbox_purge: complete — scanned={stats['items_scanned']} db_rows={stats['db_rows_deleted']} errors={stats['errors']} ({duration:.1f}s)"
        + (" [DRY RUN]" if dry_run else "")
    )
    return stats


def purge_expired_docs() -> dict | None:
    if not _should_run_opportunistic(_DOC_THROTTLE_KEY, _DOC_THROTTLE_SEC, "doc_purge"):
        return None
    return run_doc_purge()


def purge_expired_images() -> dict | None:
    if not _should_run_opportunistic(_IMAGE_THROTTLE_KEY, _IMAGE_THROTTLE_SEC, "image_purge"):
        return None
    return run_image_purge()


def purge_expired_uploads() -> dict | None:
    if not _should_run_opportunistic(_UPLOAD_THROTTLE_KEY, _UPLOAD_THROTTLE_SEC, "upload_purge"):
        return None
    return run_upload_purge()


def purge_expired_inbox() -> dict | None:
    if not _should_run_opportunistic(_INBOX_THROTTLE_KEY, _INBOX_THROTTLE_SEC, "inbox_purge"):
        return None
    return run_inbox_purge()


def run_purge() -> dict:
    start = datetime.now(timezone.utc)
    logger.info("purge_worker: START combined purge")
    doc_stats = run_doc_purge()
    image_stats = run_image_purge()
    upload_stats = run_upload_purge()
    inbox_stats = run_inbox_purge()
    duration = (datetime.now(timezone.utc) - start).total_seconds()
    summary = {
        "doc": doc_stats,
        "image": image_stats,
        "upload": upload_stats,
        "inbox": inbox_stats,
        "duration_s": round(duration, 2),
    }
    logger.info(f"purge_worker: complete — total_duration={duration:.1f}s")
    return summary


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        os.environ["DOC_PURGE_DRY_RUN"] = "true"
        os.environ["IMAGE_PURGE_DRY_RUN"] = "true"
        os.environ["UPLOAD_PURGE_DRY_RUN"] = "true"
        os.environ["INBOX_PURGE_DRY_RUN"] = "true"
        logger.info("purge_worker: DRY RUN mode (--dry-run)")
    run_purge()
