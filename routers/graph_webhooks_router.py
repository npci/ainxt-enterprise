# SPDX-License-Identifier: MIT
# ============================================================
# GRAPH WEBHOOKS ROUTER — /webhooks/graph  (scope §5.2)
#
# Receives Microsoft Graph change notifications for meeting transcripts and
# enqueues the post-meeting job. Two cases:
#   1. Validation handshake: Graph GET/POSTs ?validationToken=... on
#      subscription create — must echo the token as text/plain within 10s.
#   2. Notification: body {"value":[{subscriptionId, clientState, resource,...}]}
#      — verify clientState (constant-time) against the stored subscription,
#      parse organizer + meeting ids from the resource path, enqueue the worker.
#
# Respond fast (<3s): the actual transcript fetch/MoM runs on Q_CONNECTOR.
# If AiNxt blocks inbound HTTPS, polling (meeting_worker) is the primary path.
# ============================================================

import hmac
import re
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, JSONResponse

from core.logger import logger

router = APIRouter(prefix="/webhooks", tags=["graph"])

# users/{org}/onlineMeetings/{meeting}/transcripts/{tid} — with ('id') or /id forms
_ORG_RE     = re.compile(r"users[/(]'?([^/')]+)'?[)/]")
_MEETING_RE = re.compile(r"onlineMeetings[/(]'?([^/')]+)'?[)/]?")


def _parse_resource(resource: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (organizer_id, meeting_id) from a Graph transcript resource path."""
    org = _ORG_RE.search(resource or "")
    mtg = _MEETING_RE.search(resource or "")
    return (org.group(1) if org else None, mtg.group(1) if mtg else None)


@router.api_route("/graph", methods=["GET", "POST"])
async def graph_notifications(request: Request):
    # ── 1. Validation handshake ───────────────────────────────────────────────
    validation_token = request.query_params.get("validationToken")
    if validation_token is not None:
        # Graph requires the raw token echoed back as text/plain, 200.
        return PlainTextResponse(content=validation_token, status_code=200)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": True})  # nothing to do

    notifications = (payload or {}).get("value", []) or []
    enqueued, rejected = 0, 0

    from services.graph_subscriptions import find_by_subscription_id

    for note in notifications:
        sub_id       = note.get("subscriptionId", "")
        client_state = note.get("clientState", "")
        resource     = note.get("resource", "")

        # ── 2. Verify clientState (constant-time) against stored subscription ──
        sub = find_by_subscription_id(sub_id) if sub_id else None
        if not sub or not hmac.compare_digest(sub.get("client_state", ""), client_state or ""):
            logger.warning(f"graph webhook: clientState mismatch / unknown subscription {sub_id[:12]} — rejected")
            rejected += 1
            continue

        organizer_id, meeting_id = _parse_resource(resource)
        if not (organizer_id and meeting_id):
            logger.warning(f"graph webhook: could not parse organizer/meeting from resource '{resource}'")
            rejected += 1
            continue

        try:
            from core.job_queue import enqueue_job, Q_CONNECTOR
            enqueue_job(
                "workers.meeting_worker.run_post_meeting_job",
                {"meeting_id": meeting_id, "organizer_id": organizer_id, "detected_via": "webhook"},
                queue_name=Q_CONNECTOR,
            )
            enqueued += 1
        except Exception as e:
            logger.error(f"graph webhook: enqueue failed for meeting {meeting_id} → {e}")
            rejected += 1

    logger.info(f"graph webhook: enqueued={enqueued} rejected={rejected}")
    # Graph expects a fast 2xx; work proceeds async.
    return JSONResponse({"ok": True, "enqueued": enqueued, "rejected": rejected}, status_code=202)
