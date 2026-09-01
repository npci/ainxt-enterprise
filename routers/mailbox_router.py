# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AGENT MAILBOX ROUTER — /agent/message (send), /agent/messages (poll)
# ============================================================
#
# Redis-backed, gateway-managed message queue for inter-agent / teammate
# coordination — the v1 in-process mailbox moved server-side (per the v2
# design note "moved to backend-managed queues via gateway"). The CLI's
# send_message tool already POSTs to /agent/message; this router implements
# that endpoint plus the missing receive side (/agent/messages).
#
# Design (scale-safe for 2,000 concurrent users):
#   - Every box is a capped, TTL'd Redis list on db=2 (transient coordination).
#   - All ops are O(1)/O(n-in-box) and NON-BLOCKING (no inline waits, no
#     long-poll holding a worker) — RPUSH to send, atomic LRANGE+DEL to drain.
#   - Messages are scoped to the caller's JWT `sub`, so one user's agents never
#     see another user's traffic.
#   - No DB schema change (Redis only).
#
# Mailbox key:  mbox:{sub}:{box}
#   box == a teammate/agent name the sender addresses ("to"); the recipient
#   agent drains its own box. Broadcasts (to == "*") land in box "*", which an
#   agent can poll explicitly.

import json
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth.dependencies import get_current_user
from core.config import redis_client

router = APIRouter(tags=["mailbox"])

_DB = 2                 # transient agent coordination (Redis db=2)
_TTL_SECONDS = 3600     # stale mailboxes self-expire after 1h
_MAX_PER_BOX = 500      # cap per box; oldest messages drop past the cap


def _sub(user: dict) -> str:
    """Stable per-user scope key from the JWT payload."""
    return str(user.get("sub") or user.get("email") or user.get("user_id") or "anon")


def _key(sub: str, box: str) -> str:
    return f"mbox:{sub}:{box}"


class SendBody(BaseModel):
    to: str
    message: str
    summary: Optional[str] = None
    sent_at: Optional[str] = None
    frm: Optional[str] = None  # optional sender box/agent name


@router.post("/agent/message")
def send_agent_message(body: SendBody, user: dict = Depends(get_current_user)):
    """Deliver a message to a teammate agent's mailbox (or box '*' to broadcast)."""
    box = (body.to or "").strip()
    if not box:
        raise HTTPException(status_code=400, detail="recipient 'to' is required")
    if not (body.message or "").strip():
        raise HTTPException(status_code=400, detail="'message' is required")

    sub = _sub(user)
    msg = {
        "id": uuid.uuid4().hex,
        "from": (body.frm or "").strip(),
        "to": box,
        "message": body.message,
        "summary": ((body.summary or "").strip() or None),
        "ts": body.sent_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    r = redis_client(_DB, decode_responses=True)
    key = _key(sub, box)
    pipe = r.pipeline()
    pipe.rpush(key, json.dumps(msg))
    pipe.ltrim(key, -_MAX_PER_BOX, -1)   # bound the box
    pipe.expire(key, _TTL_SECONDS)        # self-expiring
    pipe.execute()
    return {"ok": True, "id": msg["id"], "to": box}


@router.get("/agent/messages")
def poll_agent_messages(
    box: str = Query(..., description="The mailbox to drain (the polling agent's own name)"),
    peek: bool = Query(default=False, description="Read without removing"),
    user: dict = Depends(get_current_user),
):
    """Drain (or peek) pending messages for `box`. Non-blocking: returns immediately."""
    b = (box or "").strip()
    if not b:
        raise HTTPException(status_code=400, detail="query param 'box' is required")

    sub = _sub(user)
    r = redis_client(_DB, decode_responses=True)
    key = _key(sub, b)
    if peek:
        raw = r.lrange(key, 0, -1)
    else:
        # Atomic drain so two pollers never double-deliver the same message.
        pipe = r.pipeline()
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        raw, _ = pipe.execute()

    messages = []
    for item in (raw or []):
        try:
            messages.append(json.loads(item))
        except Exception:
            pass
    return {"box": b, "messages": messages, "count": len(messages)}


@router.get("/agent/mailboxes")
def list_mailboxes(user: dict = Depends(get_current_user)):
    """List the caller's non-empty mailbox names + pending counts (diagnostic)."""
    sub = _sub(user)
    r = redis_client(_DB, decode_responses=True)
    prefix = f"mbox:{sub}:"
    out = []
    for key in r.scan_iter(match=f"{prefix}*", count=200):
        out.append({"box": key[len(prefix):], "pending": r.llen(key)})
    return {"mailboxes": out}
