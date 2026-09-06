# SPDX-License-Identifier: MIT
# ============================================================
# PREFERENCE LEARNER — feedback → per-user style preference (loop C)
# ============================================================
#
# Closes the "friendly chat" feedback loop WITHOUT reinforcement learning /
# weight training. It reads the thumbs feedback users already give
# (message_feedback table) and, when a STABLE pattern emerges, writes a compact
# durable style preference into that user's cross-chat memory. The persona
# composer (cil/persona.py) then reads it back, so future answers adapt.
#
#   feedback (thumbs + issue) → derived preference → user memory → persona → warmer/shorter replies
#
# DESIGN:
#   * DETERMINISTIC first: preferences are derived from counts/thresholds over
#     recent feedback (auditable, cheap, no model). An optional LLM nuance pass
#     over free-text comments is gated OFF by default.
#   * BACKGROUND ONLY: runs in a daemon poll thread (mirrors
#     workers.cowork_scheduler). Never touches the request/answer path.
#   * IDEMPOTENT: writes via PostgresMemory.save_user_memory with a stable
#     context_hint ("response_style_pref"); the memory layer dedups by key.
#   * FAIL-SAFE: every step is wrapped; a failure logs and is skipped.
#
# Flag: PREFERENCE_LEARNING (default true). Poll: PREFERENCE_LEARNER_POLL_SECONDS
# (default 3600). Window: PREFERENCE_FEEDBACK_WINDOW rows per user (default 30).
# ============================================================

from __future__ import annotations

import os
import threading
import time
from collections import Counter
from typing import Dict, List, Optional

from core.logger import logger

_ENABLED = os.getenv("PREFERENCE_LEARNING", "true").lower() == "true"
_POLL_SECONDS = max(300, int(os.getenv("PREFERENCE_LEARNER_POLL_SECONDS", "3600")))
_WINDOW = max(5, int(os.getenv("PREFERENCE_FEEDBACK_WINDOW", "30")))
# A pattern must appear at least this many times AND be a majority of the user's
# negative signals before we write a preference (avoids over-fitting one gripe).
_MIN_SIGNALS = max(2, int(os.getenv("PREFERENCE_MIN_SIGNALS", "3")))
_REDIS_LAST_RUN_KEY = "preference_learner:last_run_ts"

# Map thumbs-down issue/sub_issue tokens → a human preference sentence. These
# are matched as case-insensitive substrings against issue + sub_issue + comment.
_ISSUE_TO_PREF = [
    (("too long", "too_long", "verbose", "lengthy", "wordy"),
     "Prefers concise, to-the-point answers."),
    (("too short", "too_short", "not enough", "more detail", "too brief"),
     "Prefers thorough, detailed answers."),
    (("too formal", "too_formal", "robotic", "stiff"),
     "Prefers a warm, casual, conversational tone."),
    (("too casual", "too_casual", "unprofessional"),
     "Prefers a professional, formal tone."),
    (("off topic", "off_topic", "irrelevant", "not what i asked"),
     "Wants answers that stay tightly on the exact question asked."),
]


def _derive_from_rows(rows: List[dict]) -> Optional[str]:
    """Given a user's recent feedback rows, return ONE preference sentence or None.
    Deterministic: counts negative-signal categories, requires a clear majority."""
    try:
        negs = [r for r in rows if int(r.get("rating", 0) or 0) < 0]
        if len(negs) < _MIN_SIGNALS:
            return None
        counts: Counter = Counter()
        pref_for_bucket: Dict[int, str] = {}
        for r in negs:
            blob = " ".join(str(r.get(k) or "") for k in ("issue", "sub_issue", "comment")).lower()
            for i, (tokens, pref) in enumerate(_ISSUE_TO_PREF):
                if any(t in blob for t in tokens):
                    counts[i] += 1
                    pref_for_bucket[i] = pref
                    break
        if not counts:
            return None
        top_bucket, top_count = counts.most_common(1)[0]
        # Require the dominant complaint to be frequent AND a majority of negatives.
        if top_count >= _MIN_SIGNALS and top_count >= (len(negs) * 0.6):
            return pref_for_bucket[top_bucket]
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug(f"preference_learner: derive failed → {e}")
        return None


def _recent_feedback_by_user(db, since_ts) -> Dict[str, List[dict]]:
    """Fetch recent feedback rows grouped by user_id, newest first, capped to
    the window per user. `since_ts` limits to rows created after the last run."""
    from sqlalchemy import text as _sql
    q = _sql(
        "SELECT user_id, rating, issue, sub_issue, comment, created_at "
        "FROM message_feedback "
        "WHERE created_at > :since "
        "ORDER BY created_at DESC"
    )
    out: Dict[str, List[dict]] = {}
    for row in db.execute(q, {"since": since_ts}).fetchall():
        uid = row[0]
        if not uid:
            continue
        bucket = out.setdefault(uid, [])
        if len(bucket) < _WINDOW:
            bucket.append({
                "rating": row[1], "issue": row[2], "sub_issue": row[3],
                "comment": row[4], "created_at": row[5],
            })
    return out


def run_once() -> int:
    """Derive + persist preferences for every user with new feedback since the
    last run. Returns the number of users updated. Never raises."""
    if not _ENABLED:
        return 0
    updated = 0
    try:
        from datetime import datetime, timedelta
        from db.database import SessionLocal
        from core.kv import get_kv
        from core.config import RDB_CACHE

        # Determine the "since" watermark from Redis (fallback: last 7 days).
        _since = datetime.utcnow() - timedelta(days=7)
        try:
            _r = get_kv(RDB_CACHE, decode_responses=True)
            _last = _r.get(_REDIS_LAST_RUN_KEY)
            if _last:
                _since = datetime.utcfromtimestamp(float(_last))
        except Exception:
            _r = None

        db = SessionLocal()
        try:
            by_user = _recent_feedback_by_user(db, _since)
        finally:
            db.close()

        if by_user:
            from memory.postgres_memory import PostgresMemory
            _mem = PostgresMemory()
            for uid, rows in by_user.items():
                pref = _derive_from_rows(rows)
                if not pref:
                    continue
                try:
                    _mem.save_user_memory(
                        uid, pref, context_hint="response_style_pref",
                    )
                    updated += 1
                    logger.info(f"preference_learner: user={uid} → {pref!r}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"preference_learner: save failed for {uid} → {e}")

        # Advance the watermark so next run only sees newer feedback.
        try:
            if _r is not None:
                _r.set(_REDIS_LAST_RUN_KEY, str(time.time()))
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001 — background job must never crash the process
        logger.error(f"preference_learner run_once error: {e}")
    return updated


def preference_learner_thread(stop_event: threading.Event):
    """Daemon poll loop. Mirrors workers.start_workers cowork scheduler thread.
    Runs as ONE thread in the parent worker process."""
    if not _ENABLED:
        logger.info("preference_learner: disabled (PREFERENCE_LEARNING=false)")
        return
    logger.info(f"preference_learner thread started (tick every {_POLL_SECONDS}s)")
    while not stop_event.is_set():
        try:
            n = run_once()
            if n:
                logger.info(f"preference_learner: updated {n} user preference(s)")
        except Exception as e:  # noqa: BLE001
            logger.error(f"preference_learner tick error: {e}")
        stop_event.wait(_POLL_SECONDS)
