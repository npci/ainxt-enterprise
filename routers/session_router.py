# SPDX-License-Identifier: Apache-2.0
# ============================================================
# SESSION ROUTER  — concurrent session management
# Prefix : /auth   (registered via gateway.py)
# ============================================================
#
# DAST finding: "The application permits multiple concurrent login sessions
# for the same user account without restriction or alerting the user.
# There is no control to detect or limit parallel authentication sessions
# from different devices or locations."
#
# This module implements the three self-service session management endpoints
# that address the finding:
#
#   GET    /auth/sessions            — list all active sessions (device, IP, time)
#   DELETE /auth/sessions            — revoke all OTHER sessions ("sign out everywhere else")
#   DELETE /auth/sessions/{sid}      — revoke one specific session by ID
#
# Enforcement is layered:
#   1. session_manager.register_session() evicts the oldest session when
#      MAX_CONCURRENT_SESSIONS is reached (called from auth_router.login).
#   2. jwt_handler.decode_token() validates the `sid` claim on every request
#      so revoked-session tokens are rejected even before JWT expiry.
#   3. auth_router.logout() calls session_manager.revoke_session() to free
#      the slot immediately on sign-out.
#   4. These endpoints let users inspect and terminate their own sessions.
# ============================================================

import os
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from auth.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_ts(ts: int) -> str:
    """Convert a Unix timestamp to a human-readable UTC string."""
    import datetime as _dt
    from datetime import timezone as _tz
    try:
        return _dt.datetime.fromtimestamp(ts, tz=_tz.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return ""


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/sessions")
def list_sessions(payload: dict = Depends(get_current_user)):
    """
    List all active sessions for the authenticated user.

    Each entry includes:
      - session_id   — unique session identifier
      - ip           — IP address at login time
      - device       — browser / OS hint (e.g. "Chrome / Windows")
      - user_agent   — raw User-Agent string (truncated at 512 chars)
      - created_at   — human-readable login timestamp (UTC)
      - is_current   — True if this is the calling session

    DAST fix: "Monitor login activity and notify users when new logins occur
    to help detect unauthorized access attempts."
    """
    from auth.session_manager import get_active_sessions

    user_id  = payload.get("sub")
    sessions = get_active_sessions(user_id)
    result   = []
    for s in sessions:
        result.append({
            "session_id": s["session_id"],
            "ip":         s.get("ip", ""),
            "device":     s.get("device", ""),
            "user_agent": s.get("user_agent", ""),
            "created_at": _fmt_ts(s.get("created_at", 0)),
            "is_current": s["session_id"] == payload.get("sid", ""),
        })
    return {
        "sessions":    result,
        "total":       len(result),
        "max_allowed": int(os.getenv("MAX_CONCURRENT_SESSIONS", "5")),
    }


@router.delete("/sessions")
def revoke_other_sessions(
    request: Request,
    response: Response,
    payload: dict = Depends(get_current_user),
):
    """
    Revoke ALL active sessions EXCEPT the current one.

    "Sign out everywhere else" — lets a user terminate all parallel sessions
    in one click after noticing an unexpected login notification.

    DAST fix: "Restrict the number of concurrent sessions per account or
    invalidate previous sessions upon new login."
    """
    from auth.session_manager import revoke_all_sessions

    user_id       = payload.get("sub")
    current_sid   = payload.get("sid")
    revoked_count = revoke_all_sessions(user_id, except_session_id=current_sid)
    logger.info(
        "sessions: user=%s revoked %d other session(s) via DELETE /auth/sessions",
        user_id, revoked_count,
    )
    return {
        "success":       True,
        "revoked_count": revoked_count,
        "message": (
            f"Revoked {revoked_count} other session(s). "
            "Your current session remains active."
        ),
    }


@router.delete("/sessions/{session_id}")
def revoke_specific_session(
    session_id: str,
    payload: dict = Depends(get_current_user),
):
    """
    Revoke a specific session by its session_id.

    The session must belong to the authenticated user.  Any device still
    presenting the revoked token will receive HTTP 401 on its next request
    (jwt_handler.decode_token() checks the session registry on every call).

    DAST fix: fine-grained session termination — users can remove individual
    suspicious sessions without signing out of all devices.
    """
    from auth.session_manager import revoke_session, get_active_sessions

    user_id = payload.get("sub")
    active  = get_active_sessions(user_id)
    owned   = [s for s in active if s["session_id"] == session_id]
    if not owned:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found or already expired.",
        )
    revoke_session(user_id, session_id, also_blacklist_jwt=True)
    logger.info(
        "sessions: user=%s revoked session=%s via DELETE /auth/sessions/{id}",
        user_id, session_id,
    )
    return {
        "success":    True,
        "session_id": session_id,
        "message": (
            "Session revoked. "
            "The device using that session will be signed out on its next request."
        ),
    }
