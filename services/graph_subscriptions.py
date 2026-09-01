# SPDX-License-Identifier: Apache-2.0
# ============================================================
# GRAPH SUBSCRIPTIONS — change-notification lifecycle (scope §5.2)
#
# "Meeting ended" has no native Graph trigger. The polling fallback lives in
# workers/meeting_worker.poll_recent_meetings; this module is the webhook path:
# it creates/renews Microsoft Graph /subscriptions for meeting transcripts so
# Graph POSTs us a notification when a transcript becomes available.
#
# App-only (centralized consent, §7.2) via integrations.graph_app_client.
# Subscriptions WITHOUT includeResourceData → no encryption certs needed; the
# notification's `resource` path carries the organizer + meeting + transcript
# ids, which is enough to enqueue a fetch.
#
# Needs a Graph-reachable public HTTPS notificationUrl. If AiNxt blocks inbound,
# polling (meeting_worker) remains the primary detector.
# ============================================================

import os
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text as _text

from core.logger import logger
from db.database import SessionLocal, DB_SCHEMA
from integrations import graph_app_client as graph

# getAllTranscripts: tenant-wide transcript-available notifications.
_DEFAULT_RESOURCE = os.getenv(
    "GRAPH_SUB_RESOURCE",
    "communications/onlineMeetings/getAllTranscripts",
)
# Max expiration for this resource is short; renew well before it.
_EXPIRY_MINUTES = int(os.getenv("GRAPH_SUB_EXPIRY_MIN", "55"))


def _notification_url() -> str:
    base = os.getenv("PLATFORM_BASE_URL", "").rstrip("/")
    return os.getenv("GRAPH_SUB_NOTIFICATION_URL", f"{base}/ainxt/v1/api/webhooks/graph")


def create_subscription(resource: Optional[str] = None) -> dict:
    """Create a Graph change-notification subscription and persist it."""
    resource = resource or _DEFAULT_RESOURCE
    client_state = secrets.token_urlsafe(32)
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=_EXPIRY_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    notif_url = _notification_url()

    body = {
        "changeType":         "created",
        "notificationUrl":    notif_url,
        "resource":           resource,
        "expirationDateTime": expiry,
        "clientState":        client_state,
    }
    resp = graph.post_json("/v1.0/subscriptions", body)
    sub_id = resp.get("id", "")
    if not sub_id:
        raise RuntimeError(f"Graph subscription create returned no id: {resp}")

    db = SessionLocal()
    try:
        db.execute(
            _text(
                f"INSERT INTO {DB_SCHEMA}.graph_subscriptions "
                f"(subscription_id, resource, change_type, client_state, notification_url, expires_at, status) "
                f"VALUES (:sid, :res, 'created', :cs, :url, CAST(:exp AS TIMESTAMPTZ), 'active') "
                f"ON CONFLICT (subscription_id) DO UPDATE SET "
                f"  client_state=:cs, expires_at=CAST(:exp AS TIMESTAMPTZ), status='active', updated_at=NOW()"
            ),
            {"sid": sub_id, "res": resource, "cs": client_state, "url": notif_url, "exp": expiry},
        )
        db.commit()
    finally:
        db.close()

    # Audit the subscription creation (hash of clientState, never the secret).
    try:
        from core import graph_audit
        graph_audit.record(f"subscription:{sub_id}", graph_audit.EVENT_SUBSCRIPTION,
                           data=client_state, resource=resource, meta={"expires_at": expiry})
    except Exception:
        pass

    logger.info(f"graph_subscriptions: created {sub_id} for '{resource}' (expires {expiry})")
    return {"subscription_id": sub_id, "expires_at": expiry, "resource": resource}


def renew_expiring(within_minutes: int = 15) -> dict:
    """PATCH active subscriptions expiring within `within_minutes` to extend them.

    Schedule via cron alongside poll_recent_meetings.
    """
    cutoff = (datetime.now(timezone.utc) + timedelta(minutes=within_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_expiry = (datetime.now(timezone.utc) + timedelta(minutes=_EXPIRY_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

    db = SessionLocal()
    try:
        rows = db.execute(
            _text(
                f"SELECT subscription_id FROM {DB_SCHEMA}.graph_subscriptions "
                f"WHERE status='active' AND expires_at <= CAST(:cut AS TIMESTAMPTZ)"
            ),
            {"cut": cutoff},
        ).mappings().all()
    finally:
        db.close()

    renewed, failed = 0, 0
    for r in rows:
        sid = r["subscription_id"]
        try:
            # PATCH via Graph (post_json only does POST). Relay through the LLM proxy server
            # has no internet egress.
            from connectors.net_relay import relay_request
            from integrations.graph_app_client import GRAPH_BASE, _headers, _TIMEOUT
            resp = relay_request("PATCH", f"{GRAPH_BASE}/v1.0/subscriptions/{sid}",
                                 headers={**_headers("application/json"), "Content-Type": "application/json"},
                                 json={"expirationDateTime": new_expiry}, timeout=_TIMEOUT)
            resp.raise_for_status()
            db = SessionLocal()
            try:
                db.execute(
                    _text(f"UPDATE {DB_SCHEMA}.graph_subscriptions SET expires_at=CAST(:exp AS TIMESTAMPTZ), updated_at=NOW() WHERE subscription_id=:sid"),
                    {"exp": new_expiry, "sid": sid},
                )
                db.commit()
            finally:
                db.close()
            renewed += 1
        except Exception as e:
            logger.warning(f"graph_subscriptions: renew failed for {sid} → {e}")
            failed += 1

    logger.info(f"graph_subscriptions: renewed={renewed} failed={failed}")
    return {"renewed": renewed, "failed": failed}


def find_by_subscription_id(sub_id: str) -> Optional[dict]:
    """Return the stored subscription row (for clientState verification)."""
    db = SessionLocal()
    try:
        row = db.execute(
            _text(
                f"SELECT subscription_id, resource, client_state, status "
                f"FROM {DB_SCHEMA}.graph_subscriptions WHERE subscription_id=:sid"
            ),
            {"sid": sub_id},
        ).mappings().first()
        return dict(row) if row else None
    finally:
        db.close()
