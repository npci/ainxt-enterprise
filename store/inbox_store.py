# SPDX-License-Identifier: MIT
# ============================================================
# INBOX STORE — Postgres (inbox_items)
# ============================================================

import queue
import threading
import uuid
from typing import Optional, List

from core.logger import logger
from db.database import SessionLocal
from db.models import InboxItem


# ============================================================
# SSE SUBSCRIBER REGISTRY
# Keyed by user_id → list of queue.SimpleQueue objects.
# Each active GET /inbox/stream connection holds one queue.
# publish_inbox_item() pushes to all queues for the user.
# ============================================================

_sse_lock = threading.Lock()
_sse_subscribers: dict = {}   # user_id → [SimpleQueue, ...]


def _sse_subscribe(user_id: str) -> queue.SimpleQueue:
    q: queue.SimpleQueue = queue.SimpleQueue()
    with _sse_lock:
        _sse_subscribers.setdefault(user_id, []).append(q)
    return q


def _sse_unsubscribe(user_id: str, q: queue.SimpleQueue) -> None:
    with _sse_lock:
        bucket = _sse_subscribers.get(user_id, [])
        try:
            bucket.remove(q)
        except ValueError:
            pass


def _sse_push(user_id: str, payload: dict) -> None:
    """Push a notification payload to all active SSE subscribers for user_id."""
    with _sse_lock:
        buckets = list(_sse_subscribers.get(user_id, []))
    for q in buckets:
        try:
            q.put_nowait(payload)
        except Exception:
            pass


def _utc_posix(dt) -> float:
    """Convert a stored backend ``datetime`` to POSIX seconds correctly.

    Two shapes of stored datetimes coexist in this schema:

      1. **Naive UTC** (``TIMESTAMP`` columns): the code writes
         ``datetime.utcnow()`` — the value is a UTC wall-clock with no
         tzinfo. Python's naive ``.timestamp()`` interprets a naive datetime
         as **server-local time**, so on an IST host it emits POSIX for a
         moment 5:30 h behind reality. We stamp ``utc`` before converting.

      2. **Aware UTC** (``TIMESTAMP WITH TIME ZONE`` columns, e.g.
         ``inbox_items.created_at``): new writes use ``_now_utc`` which
         supplies a tz-aware UTC value, so Postgres stores the correct
         absolute moment and read-back yields e.g. ``14:26+05:30``. That
         value already carries the truth — honouring the tzinfo and calling
         ``.timestamp()`` yields the right POSIX.

    Both branches converge on the same POSIX seconds for the same real
    moment. We must NOT strip tzinfo from aware values — doing so shifts the
    absolute moment by the local offset (a "future by 5:30h" render).
    """
    if not dt:
        return 0
    try:
        from datetime import timezone as _tz
        if getattr(dt, "tzinfo", None) is None:
            # Naive column: value is UTC wall-clock, stamp UTC.
            dt = dt.replace(tzinfo=_tz.utc)
        # Aware column: value is already at the correct absolute moment.
        return dt.timestamp()
    except Exception:
        try:
            return dt.timestamp()
        except Exception:
            return 0


def _row_to_dict(item: InboxItem) -> dict:
    return {
        "id":        item.id,
        "user_id":   item.user_id,
        "type":      item.type,
        "title":     item.title,
        "body":      item.body or "",
        "source_id": item.source_id or "",
        "metadata":  item.metadata_ or {},
        "read":      item.read,
        "created_at": _utc_posix(item.created_at),
    }


def publish_inbox_item(
    user_id: str,
    type: str,
    title: str,
    body: str,
    source_id: str = "",
    metadata: Optional[dict] = None,
) -> str:
    db = SessionLocal()
    try:
        item = InboxItem(
            id=str(uuid.uuid4()),
            user_id=user_id,
            type=type,
            title=title,
            body=body,
            source_id=source_id,
            metadata_=metadata or {},
            read=False,
        )
        db.add(item)
        db.commit()
        item_id = item.id
        # Push to active SSE subscribers (fire-and-forget)
        _sse_push(user_id, _row_to_dict(item))
        return item_id
    except Exception as e:
        db.rollback()
        logger.error(f"InboxStore.publish: {e}")
        return ""
    finally:
        db.close()


def get_inbox(user_id: str, type_filter: Optional[str] = None, limit: int = 50) -> List[dict]:
    db = SessionLocal()
    try:
        q = (db.query(InboxItem)
             .filter(InboxItem.user_id == user_id)
             .order_by(InboxItem.created_at.desc()))
        if type_filter:
            q = q.filter(InboxItem.type == type_filter)
        return [_row_to_dict(r) for r in q.limit(limit).all()]
    finally:
        db.close()


def update_inbox_item(item_id: str, body: str = None, metadata: dict = None) -> bool:
    """Patch body and/or metadata of an existing inbox item and push SSE update."""
    db = SessionLocal()
    try:
        item = db.query(InboxItem).filter(InboxItem.id == item_id).first()
        if not item:
            return False
        if body is not None:
            item.body = body
        if metadata is not None:
            item.metadata_ = {**(item.metadata_ or {}), **metadata}
        db.commit()
        _sse_push(item.user_id, _row_to_dict(item))
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"InboxStore.update: {e}")
        return False
    finally:
        db.close()


def mark_read(item_id: str) -> bool:
    db = SessionLocal()
    try:
        item = db.query(InboxItem).filter(InboxItem.id == item_id).first()
        if not item:
            return False
        item.read = True
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"InboxStore.mark_read: {e}")
        return False
    finally:
        db.close()


def mark_all_read(user_id: str) -> int:
    db = SessionLocal()
    try:
        count = (db.query(InboxItem)
                 .filter(InboxItem.user_id == user_id, InboxItem.read == False)
                 .update({"read": True}))
        db.commit()
        return count
    except Exception as e:
        db.rollback()
        logger.error(f"InboxStore.mark_all_read: {e}")
        return 0
    finally:
        db.close()


def delete_item(user_id: str, item_id: str) -> bool:
    db = SessionLocal()
    try:
        item = (db.query(InboxItem)
                .filter(InboxItem.id == item_id, InboxItem.user_id == user_id)
                .first())
        if not item:
            return False
        db.delete(item)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"InboxStore.delete_item: {e}")
        return False
    finally:
        db.close()


def unread_count(user_id: str) -> int:
    db = SessionLocal()
    try:
        return (db.query(InboxItem)
                .filter(InboxItem.user_id == user_id, InboxItem.read == False)
                .count())
    finally:
        db.close()


def delete_pending_by_source(user_ids: List[str], type_: str, source_id: str) -> int:
    """Delete inbox items superseded by a fresh submission of the same artifact.

    Called on governance re-submit to make room for a brand-new ``[Needs
    Approval]`` row. Rows are removed for the given recipients when their
    ``metadata.status`` is one of:

      - ``PENDING_APPROVAL`` / ``PENDING_L2`` — a stale request; the fresh
        submit replaces it.
      - ``REJECTED`` — the recipient (usually the approver who rejected)
        previously acted; the maker has since fixed the artifact and
        re-submitted, so the old rejection is now obsolete and would otherwise
        hide the fresh PENDING row from the frontend collapse (which keeps
        one row per ``(type, source_id)`` for governance items).
      - ``DRAFT`` — a cancelled request. Same reasoning: the maker withdrew
        then re-submitted, so the DRAFT audit row should not out-rank the
        new PENDING notification.

    Terminal-and-permanent statuses (``APPROVED`` / ``PRODUCTION`` /
    ``DEPRECATED``) are preserved — those represent the artifact's real
    current state, not an obsolete step in the lifecycle. Best-effort;
    returns the count deleted, 0 on any error.
    """
    if not user_ids or not source_id or not type_:
        return 0
    SUPERSEDED = {"PENDING_APPROVAL", "PENDING_L2", "REJECTED", "DRAFT"}
    db = SessionLocal()
    try:
        rows = (db.query(InboxItem)
                .filter(InboxItem.user_id.in_(list(user_ids)),
                        InboxItem.type == type_,
                        InboxItem.source_id == source_id)
                .all())
        n = 0
        for r in rows:
            m = r.metadata_ or {}
            if m.get("status") in SUPERSEDED:
                db.delete(r)
                n += 1
        if n:
            db.commit()
        return n
    except Exception as e:
        db.rollback()
        logger.error(f"InboxStore.delete_pending_by_source: {e}")
        return 0
    finally:
        db.close()


def delete_all_by_source(type_: str, source_id: str) -> int:
    """Delete EVERY inbox item of the given ``(type_, source_id)``, for every
    recipient, once the underlying request has been approved/rejected.

    For one-shot approval-request notifications (``product_approval``,
    ``codebase_approval``, ``codewiki_approval``) that don't carry a reusable
    lifecycle ``metadata.status`` the way ``governance_approval`` items do
    (see :func:`delete_pending_by_source`'s docstring for that case): once a
    decision is made, NOBODY else who was notified still has anything to do,
    so every recipient's copy is stale -- not just the acting approver's own.
    Without this, every other HOD/delegate/admin who was notified at submit
    time keeps seeing a "pending approval" prompt indefinitely for a request
    someone else already resolved. Best-effort; returns the count deleted,
    0 on any error.
    """
    if not source_id or not type_:
        return 0
    db = SessionLocal()
    try:
        n = (db.query(InboxItem)
             .filter(InboxItem.type == type_, InboxItem.source_id == source_id)
             .delete(synchronize_session=False))
        if n:
            db.commit()
        return n
    except Exception as e:
        db.rollback()
        logger.error(f"InboxStore.delete_all_by_source: {e}")
        return 0
    finally:
        db.close()
