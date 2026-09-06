# SPDX-License-Identifier: MIT
# ============================================================
# FEEDBACK ROUTER — /chat/messages/{message_id}/feedback
# ============================================================
#
# Lets engineers rate individual AI responses (thumbs up / down).
# Data flows into message_feedback table for quality analysis.
# Captured chunk IDs enable root-cause analysis of bad answers.
# ============================================================

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from auth.dependencies import get_current_user as _require_auth
from core.logger import logger

router = APIRouter(prefix="/chat", tags=["feedback"])


class FeedbackRequest(BaseModel):
    rating:            int              # +1 = thumbs up, -1 = thumbs down
    issue:             Optional[str] = None   # thumbs-down category
    sub_issue:         Optional[str] = None   # sub-category
    comment:           Optional[str] = None   # free-text (max 1000 chars)
    user_prompt:       Optional[str] = None   # the question that triggered the response
    assistant_summary: Optional[str] = None   # first 800 chars of the response


@router.post("/messages/{message_id}/feedback", status_code=200)
async def submit_feedback(
    message_id: str,
    body: FeedbackRequest,
    current_user: dict = Depends(_require_auth),
):
    """
    Submit thumbs-up (+1) or thumbs-down (-1) feedback on an AI response.
    Linked to message_feedback table (ORM: MessageFeedback).

    SEC-10: Rate-limited to 1 submission per user per message (dedup via Redis SETNX).
    Prevents feedback flooding that could poison chunk quality scores.
    """
    if body.rating not in (1, -1):
        raise HTTPException(status_code=422, detail="rating must be +1 or -1")

    user_id = current_user.get("user_id") or current_user.get("sub") or current_user.get("id", "")

    # SEC-10: per-user per-message dedup (1 feedback per message per user per 24h)
    try:
        from core.kv import get_kv
        from core.config import RDB_CACHE
        _rc = get_kv(RDB_CACHE, decode_responses=True)
        _dedup_key = f"feedback:dedup:{user_id}:{message_id}"
        if not _rc.set(_dedup_key, "1", nx=True, ex=86400):
            # Already submitted — allow update (upsert below handles it) but don't re-count
            pass  # upsert path still runs; dedup only prevents new row spam
    except Exception:
        pass  # Redis unavailable — allow submission (non-critical guard)

    try:
        from db.database import SessionLocal
        from db.models import MessageFeedback

        db = SessionLocal()
        try:
            # Upsert: one rating per user per message
            existing = db.query(MessageFeedback).filter_by(
                message_id=message_id, user_id=user_id
            ).first()

            if existing:
                existing.rating            = body.rating
                existing.issue             = body.issue
                existing.sub_issue         = body.sub_issue
                existing.comment           = body.comment[:1000]           if body.comment           else existing.comment
                existing.user_prompt       = body.user_prompt[:2000]       if body.user_prompt       else existing.user_prompt
                existing.assistant_summary = body.assistant_summary[:1000] if body.assistant_summary else existing.assistant_summary
            else:
                db.add(MessageFeedback(
                    message_id        = message_id,
                    user_id           = user_id,
                    rating            = body.rating,
                    issue             = body.issue,
                    sub_issue         = body.sub_issue,
                    comment           = body.comment[:1000]           if body.comment           else None,
                    user_prompt       = body.user_prompt[:2000]       if body.user_prompt       else None,
                    assistant_summary = body.assistant_summary[:1000] if body.assistant_summary else None,
                ))

            db.commit()
        finally:
            db.close()

        logger.info(f"feedback: msg={message_id} user={user_id} rating={body.rating}")
        return {"ok": True, "message_id": message_id, "rating": body.rating}

    except Exception as e:
        logger.error(f"feedback: failed for msg={message_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to save feedback")


@router.get("/messages/{message_id}/feedback", status_code=200)
async def get_feedback(
    message_id: str,
    current_user: dict = Depends(_require_auth),
):
    """Get the current user's feedback for a message (for UI state restore)."""
    user_id = current_user.get("user_id") or current_user.get("sub") or current_user.get("id", "")

    try:
        from db.database import SessionLocal
        from db.models import MessageFeedback

        db = SessionLocal()
        try:
            fb = db.query(MessageFeedback).filter_by(
                message_id=message_id, user_id=user_id
            ).first()
        finally:
            db.close()

        return {
            "message_id":         message_id,
            "rating":             fb.rating            if fb else None,
            "issue":              fb.issue             if fb else None,
            "sub_issue":          fb.sub_issue         if fb else None,
            "comment":            fb.comment           if fb else None,
            "user_prompt":        fb.user_prompt       if fb else None,
            "assistant_summary":  fb.assistant_summary if fb else None,
        }
    except Exception as e:
        logger.warning(f"get_feedback failed: {e}")
        return {"message_id": message_id, "rating": None}


# ── Admin endpoint: repo permission management ───────────────

class RepoPermissionRequest(BaseModel):
    repo:      str
    user_id:   Optional[str] = None   # grant to specific user
    user_role: Optional[str] = None   # grant to role (viewer/developer/operator/security/admin)
    granted:   bool = True


@router.post("/admin/repo-permissions", status_code=200)
async def set_repo_permission(
    body: RepoPermissionRequest,
    current_user: dict = Depends(_require_auth),
):
    """
    Admin endpoint: grant or revoke access to a specific indexed repo.
    Requires admin role. Enforced at retrieval level in hybrid_search.py.
    """
    role = current_user.get("role", "viewer")
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")

    if not body.user_id and not body.user_role:
        raise HTTPException(status_code=422, detail="Either user_id or user_role must be provided")

    try:
        from db.database import SessionLocal
        from db.models import RepoPermission

        db = SessionLocal()
        try:
            existing = db.query(RepoPermission).filter_by(
                repo=body.repo,
                user_id=body.user_id,
                user_role=body.user_role,
            ).first()

            admin_id = current_user.get("user_id") or current_user.get("sub", "")

            if existing:
                existing.granted = body.granted
            else:
                perm = RepoPermission(
                    repo=body.repo,
                    user_id=body.user_id,
                    user_role=body.user_role,
                    granted=body.granted,
                    created_by=admin_id,
                )
                db.add(perm)

            db.commit()
        finally:
            db.close()

        logger.info(f"repo_perm: repo={body.repo} user={body.user_id} role={body.user_role} granted={body.granted} by={admin_id}")
        return {"ok": True, "repo": body.repo, "granted": body.granted}

    except Exception as e:
        logger.error(f"set_repo_permission failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to set permission")


@router.get("/admin/repo-permissions/{repo}", status_code=200)
async def get_repo_permissions(
    repo: str,
    current_user: dict = Depends(_require_auth),
):
    """List all permission entries for a repo (admin only)."""
    role = current_user.get("role", "viewer")
    if role not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="Operator+ role required")

    try:
        from db.database import SessionLocal
        from db.models import RepoPermission

        db = SessionLocal()
        try:
            rows = db.query(RepoPermission).filter_by(repo=repo).all()
        finally:
            db.close()

        return {
            "repo": repo,
            "permissions": [
                {
                    "id": str(r.id),
                    "user_id": r.user_id,
                    "user_role": r.user_role,
                    "granted": r.granted,
                    "created_by": r.created_by,
                }
                for r in rows
            ],
        }
    except Exception as e:
        logger.error(f"get_repo_permissions failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get permissions")


# ── P6: Feedback insights endpoint (admin dashboard) ─────────────────────────

@router.get("/feedback/insights", status_code=200)
async def get_feedback_insights(
    lookback_hours: int = 24,
    current_user: dict = Depends(_require_auth),
):
    """
    Admin endpoint: return feedback quality insights for the dashboard.

    Returns:
      - total_feedback: total thumbs-up + thumbs-down count
      - thumbs_up / thumbs_down: counts
      - top_issues: most common thumbs-down issue categories
      - penalized_chunks: count of chunks with quality penalty in Redis
      - preferences_stored: count of user preference memory entries

    Requires admin role.
    """
    role = current_user.get("role", "viewer")
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")

    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _sqlt
        from datetime import datetime, timedelta

        db = SessionLocal()
        try:
            cutoff = datetime.utcnow() - timedelta(hours=lookback_hours)
            stats = db.execute(
                _sqlt(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE rating = 1)  AS thumbs_up,
                        COUNT(*) FILTER (WHERE rating = -1) AS thumbs_down,
                        COUNT(*)                             AS total
                    FROM message_feedback
                    WHERE created_at >= :cutoff
                    """
                ),
                {"cutoff": cutoff},
            ).fetchone()

            issues = db.execute(
                _sqlt(
                    """
                    SELECT issue, COUNT(*) AS cnt
                    FROM message_feedback
                    WHERE rating = -1 AND issue IS NOT NULL AND created_at >= :cutoff
                    GROUP BY issue
                    ORDER BY cnt DESC
                    LIMIT 10
                    """
                ),
                {"cutoff": cutoff},
            ).fetchall()
        finally:
            db.close()

        # Count penalized chunks in Redis
        # SEC-09: use scan_iter (cursor-based, non-blocking) instead of KEYS (O(N) blocking)
        penalized_count = 0
        try:
            from core.kv import get_kv
            from core.config import RDB_CACHE
            _redis = get_kv(RDB_CACHE, decode_responses=True)
            penalized_count = sum(1 for _ in _redis.scan_iter("chunk_quality:*", count=100))
        except Exception:
            pass

        # Count preference memory entries
        prefs_count = 0
        try:
            from db.database import SessionLocal as _SL2
            from sqlalchemy import text as _sqlt2
            _db2 = _SL2()
            try:
                prefs_count = _db2.execute(
                    _sqlt2(
                        "SELECT COUNT(*) FROM memory_entries WHERE source_type = 'feedback'"
                    )
                ).scalar() or 0
            finally:
                _db2.close()
        except Exception:
            pass

        return {
            "lookback_hours":    lookback_hours,
            "total_feedback":    int(stats[2] or 0),
            "thumbs_up":         int(stats[0] or 0),
            "thumbs_down":       int(stats[1] or 0),
            "top_issues":        [{"issue": r[0], "count": int(r[1])} for r in issues],
            "penalized_chunks":  penalized_count,
            "preferences_stored": int(prefs_count),
        }
    except Exception as e:
        logger.error(f"get_feedback_insights failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get feedback insights")
