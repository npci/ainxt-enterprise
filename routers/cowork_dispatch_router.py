# SPDX-License-Identifier: MIT
"""
Cowork Dispatch — mobile/web → desktop task hand-off.

Buddy dispatch lets a user kick off a task from their phone and have it run on
their desktop session, where computer-use, the browser and local files live. This
is the server side of that: a small per-user queue.

Flow:
  1. Any client (mobile web, desktop, API) creates a dispatch:
        POST /buddy/dispatch { prompt, role?, project?, origin? }
  2. The user's RUNNING DESKTOP long-polls and atomically CLAIMS the oldest
     queued item (single-claim via UPDATE … WHERE status='queued'):
        GET  /buddy/dispatch/pending?instance_id=<desktop-id>
  3. The desktop runs it through the Buddy agent locally, then posts the result:
        POST /buddy/dispatch/{id}/result { status, result?, error? }
  4. The originating client polls its own list to see the outcome:
        GET  /buddy/dispatch            (my dispatches, newest first)

All rows are scoped to the caller's JWT `sub` — a user can only see/claim their
own dispatches. No connector/computer-use action runs here; execution happens on
the desktop under the SAME confirm + compliance gates as an interactive task.
"""
import asyncio
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.dependencies import get_current_user
from core.config import REDIS_HOST, REDIS_PORT
from core.logger import logger

try:
    from core.config import REDIS_PASSWORD as _REDIS_PASSWORD
except Exception:
    _REDIS_PASSWORD = None

router = APIRouter(prefix="/buddy", tags=["buddy"])

# Long-poll wake-up: creating a dispatch RPUSHes a token to this per-user key;
# a waiting /dispatch/pending BLPOPs it instead of busy-polling the DB. This kills
# the 2k-desktops × every-15s empty-poll storm — pending calls sleep in Redis and
# wake the instant a task arrives. (feedback_scale_2k_users)
_NOTIFY_KEY = "cowork:dispatch:notify:{uid}"
_LONGPOLL_HOLD = 25   # seconds the server holds an empty /pending before returning

_async_redis = None
_redis_unavailable = False


def _get_async_redis():
    global _async_redis, _redis_unavailable
    if _async_redis is not None or _redis_unavailable:
        return _async_redis
    try:
        import redis.asyncio as aioredis
        _async_redis = aioredis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=5, password=_REDIS_PASSWORD or None,
            decode_responses=True, socket_connect_timeout=2, health_check_interval=30,
        )
    except Exception as e:
        logger.warning(f"cowork_dispatch: redis.asyncio unavailable ({e}) — pending falls back to short return")
        _redis_unavailable = True
        _async_redis = None
    return _async_redis


def _db():
    from db.database import engine
    from sqlalchemy import text
    return engine, text


def _claim_one(user_id: str, instance_id: str):
    """Atomically claim the oldest queued dispatch for a user (one-shot). Returns
    a row dict or None. FOR UPDATE SKIP LOCKED → safe under many concurrent
    desktops/workers."""
    engine, text = _db()
    with engine.connect() as conn:
        row = conn.execute(text(f"""
            UPDATE cowork_dispatch
               SET status = 'claimed', claimed_by = :inst, claimed_at = NOW()
             WHERE id = (
                   SELECT id FROM cowork_dispatch
                    WHERE user_id = :uid AND status = 'queued'
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1)
         RETURNING {_COLS}
        """), {"uid": user_id, "inst": (instance_id or "desktop")[:64]}).fetchone()
        conn.commit()
    return _row_to_dict(row) if row else None


class DispatchIn(BaseModel):
    prompt: str
    role: Optional[str] = None
    project: Optional[Dict[str, Any]] = None
    origin: str = "mobile"   # mobile | web | api


class DispatchResult(BaseModel):
    status: str              # done | failed
    result: Optional[str] = None
    error: Optional[str] = None


def _row_to_dict(r) -> dict:
    return {
        "id": r[0], "prompt": r[1], "role": r[2], "project": r[3], "origin": r[4],
        "status": r[5], "claimed_by": r[6], "result": r[7], "error": r[8],
        "created_at": str(r[9]) if r[9] else None,
        "claimed_at": str(r[10]) if r[10] else None,
        "finished_at": str(r[11]) if r[11] else None,
    }


_COLS = "id, prompt, role, project, origin, status, claimed_by, result, error, created_at, claimed_at, finished_at"


@router.post("/dispatch", status_code=201)
async def create_dispatch(body: DispatchIn, current_user: dict = Depends(get_current_user)):
    """Create a task to be run by the user's desktop. Returns the dispatch id."""
    import json
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, detail="prompt is required")
    engine, text = _db()
    did = str(uuid.uuid4())
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO cowork_dispatch (id, user_id, prompt, role, project, origin, status)
                VALUES (:id, :uid, :prompt, :role, CAST(:project AS jsonb), :origin, 'queued')
            """), {
                "id": did, "uid": current_user["sub"], "prompt": prompt,
                "role": body.role, "project": json.dumps(body.project) if body.project else None,
                "origin": (body.origin or "mobile")[:20],
            })
            conn.commit()
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))
    # Wake any desktop currently long-polling /dispatch/pending for this user.
    r = _get_async_redis()
    if r is not None:
        try:
            key = _NOTIFY_KEY.format(uid=current_user["sub"])
            await r.rpush(key, did)
            await r.expire(key, 60)
        except Exception:
            pass
    logger.info(f"cowork_dispatch: queued {did} for {current_user['sub']} from {body.origin}")
    return {"id": did, "status": "queued"}


@router.get("/dispatch/pending")
async def claim_pending(instance_id: str = "desktop", current_user: dict = Depends(get_current_user)):
    """Desktop LONG-POLL: claim the oldest queued dispatch for this user; if none,
    hold the connection up to ~25s waiting on a Redis notification instead of
    busy-polling the DB. Returns {dispatch: {...}} or {dispatch: null}.

    This is the scaling fix: 2k idle desktops sleep in Redis (one held request
    each) rather than hammering the DB with ~133 empty claims/sec."""
    uid = current_user["sub"]
    try:
        # First, an immediate atomic claim (covers the common "task already queued").
        claimed = await asyncio.to_thread(_claim_one, uid, instance_id)
        if claimed:
            return {"dispatch": claimed}

        # Nothing waiting → block on a Redis notification (async, non-blocking).
        r = _get_async_redis()
        if r is not None:
            woke = await r.blpop(_NOTIFY_KEY.format(uid=uid), timeout=_LONGPOLL_HOLD)
            if woke:
                # A task arrived — claim it (another desktop may race; that's fine).
                claimed = await asyncio.to_thread(_claim_one, uid, instance_id)
                return {"dispatch": claimed}
        # No Redis (dev) or timed out empty → return null; client re-polls with jitter.
        return {"dispatch": None}
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))


@router.post("/dispatch/{dispatch_id}/result")
async def post_result(dispatch_id: str, body: DispatchResult,
                      current_user: dict = Depends(get_current_user)):
    """Desktop posts the outcome of a claimed dispatch."""
    status = body.status if body.status in ("done", "failed") else "done"
    engine, text = _db()
    try:
        with engine.connect() as conn:
            res = conn.execute(text("""
                UPDATE cowork_dispatch
                   SET status = :st, result = :result, error = :error, finished_at = NOW()
                 WHERE id = :id AND user_id = :uid
            """), {
                "st": status, "result": (body.result or "")[:100000],
                "error": (body.error or None), "id": dispatch_id, "uid": current_user["sub"],
            })
            conn.commit()
        if (res.rowcount or 0) == 0:
            raise HTTPException(404, detail="Dispatch not found")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))
    return {"id": dispatch_id, "status": status}


@router.get("/dispatch")
async def list_my_dispatches(limit: int = 50, current_user: dict = Depends(get_current_user)):
    """The caller's recent dispatches (newest first) — to watch a result land."""
    engine, text = _db()
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT {_COLS} FROM cowork_dispatch
             WHERE user_id = :uid ORDER BY created_at DESC LIMIT :lim
        """), {"uid": current_user["sub"], "lim": max(1, min(200, limit))}).fetchall()
    return {"dispatches": [_row_to_dict(r) for r in rows]}


@router.post("/dispatch/{dispatch_id}/cancel")
async def cancel_dispatch(dispatch_id: str, current_user: dict = Depends(get_current_user)):
    """Cancel a still-queued dispatch (no effect once claimed/finished)."""
    engine, text = _db()
    with engine.connect() as conn:
        res = conn.execute(text("""
            UPDATE cowork_dispatch SET status = 'cancelled', finished_at = NOW()
             WHERE id = :id AND user_id = :uid AND status = 'queued'
        """), {"id": dispatch_id, "uid": current_user["sub"]})
        conn.commit()
    return {"id": dispatch_id, "cancelled": (res.rowcount or 0) > 0}
