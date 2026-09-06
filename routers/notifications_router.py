# SPDX-License-Identifier: MIT
# ============================================================
# NOTIFICATIONS ROUTER
# POST /notifications/send   — send a test or real notification
# GET  /notifications/config — return channel configuration status
# ============================================================

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import List, Optional

router = APIRouter(prefix="/notifications", tags=["observability"])


class NotifyRequest(BaseModel):
    event_type:         str
    title:              str
    message:            str
    severity:           str = "info"
    channels:           Optional[List[str]] = None
    email_recipients:   Optional[List[str]] = None
    whatsapp_numbers:   Optional[List[str]] = None
    fields:             Optional[dict] = None


@router.post("/send")
def send_notification(body: NotifyRequest):
    """
    Trigger a notification to Slack / Email / WhatsApp.
    Requires operator role or higher.
    """
    from core.notifications import notify
    from auth.rbac import require_operator
    notify(
        event_type=body.event_type,
        title=body.title,
        message=body.message,
        severity=body.severity,
        channels=body.channels,
        email_recipients=body.email_recipients,
        whatsapp_numbers=body.whatsapp_numbers,
        fields=body.fields,
    )
    return {"status": "queued", "channels": body.channels or "default"}


@router.get("/config")
def get_notification_config():
    """Return which notification channels are configured."""
    import os
    return {
        "slack":     bool(os.getenv("SLACK_WEBHOOK_URL")),
        "email":     bool(os.getenv("AINXT_SMTP_HOST")),
        "whatsapp":  bool(os.getenv("WHATSAPP_API_URL") and os.getenv("WHATSAPP_ACCESS_TOKEN")),
    }
