# SPDX-License-Identifier: Apache-2.0
# ============================================================
# ENTERPRISE NOTIFICATION SERVICE
#
# Channels:
#   Slack    — via Incoming Webhook URL (no dependency)
#   Email    — via SMTP (smtplib stdlib)
#   WhatsApp — via Meta Business Cloud API (HTTP)
#
# Env vars:
#   SLACK_WEBHOOK_URL       — https://hooks.slack.com/services/...
#   AINXT_SMTP_HOST         — leave empty to disable email (OSS default);
#                             shared with services/smtp_service.py
#   AINXT_SMTP_PORT         — default 587
#   AINXT_SMTP_USER         — default "" (empty = no auth)
#   AINXT_SMTP_PASSWORD     — default "" (empty = no auth)
#   AINXT_SMTP_FROM         — default noreply@ainxt.local
#   WHATSAPP_API_URL        — https://graph.facebook.com/v18.0/.../messages
#   WHATSAPP_ACCESS_TOKEN   — EAA...
#   WHATSAPP_PHONE_NUMBER_ID — 1234567890
#
# Usage:
#   from core.notifications import notify
#   notify("incident_detected", "Payment service down", details="...", channels=["slack","email"])
# ============================================================

import os
import json
import smtplib
import threading
import urllib.request
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional

from core.logger import logger

# ── Env ───────────────────────────────────────────────────────

SLACK_WEBHOOK_URL      = os.getenv("SLACK_WEBHOOK_URL", "")
# Shared with services/smtp_service.py — one SMTP config for the whole
# platform. Empty AINXT_SMTP_HOST (OSS default) disables email delivery.
SMTP_HOST              = os.getenv("AINXT_SMTP_HOST", "")
SMTP_PORT              = int(os.getenv("AINXT_SMTP_PORT", "587"))
SMTP_USER              = os.getenv("AINXT_SMTP_USER", "")
SMTP_PASSWORD          = os.getenv("AINXT_SMTP_PASSWORD", "")
SMTP_FROM              = os.getenv("AINXT_SMTP_FROM", "noreply@ainxt.local")
WHATSAPP_API_URL       = os.getenv("WHATSAPP_API_URL", "")
WHATSAPP_ACCESS_TOKEN  = os.getenv("WHATSAPP_ACCESS_TOKEN", "")

# ── Severity → Slack color ────────────────────────────────────
_SEVERITY_COLOR = {
    "critical": "#FF0000",
    "high":     "#FF8C00",
    "medium":   "#FFC107",
    "low":      "#36A64F",
    "info":     "#2196F3",
}

# ── Event type → default channels ────────────────────────────
_DEFAULT_CHANNELS = {
    "incident_detected":    ["slack", "email", "whatsapp"],
    "incident_resolved":    ["slack", "email"],
    "security_alert":       ["slack", "email", "whatsapp"],
    "compliance_violation": ["slack", "email"],
    "agent_failure":        ["slack"],
    "model_cost_exceeded":  ["slack", "email"],
    "workflow_failed":      ["slack"],
    "default":              ["slack"],
}


# ============================================================
# SLACK
# ============================================================

def send_slack(
    title: str,
    message: str,
    severity: str = "info",
    fields: Optional[dict] = None,
) -> bool:
    """Post a message to the configured Slack webhook."""
    if not SLACK_WEBHOOK_URL:
        logger.warning("Slack notification skipped: SLACK_WEBHOOK_URL not set")
        return False
    try:
        color = _SEVERITY_COLOR.get(severity.lower(), "#2196F3")
        attachment = {
            "color":    color,
            "title":    title,
            "text":     message,
            "fallback": f"{title}: {message}",
            "footer":   "AiNxt Enterprise | AiNxt",
        }
        if fields:
            attachment["fields"] = [
                {"title": k, "value": str(v), "short": True}
                for k, v in fields.items()
            ]
        payload = json.dumps({"attachments": [attachment]}).encode()
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        r = urllib.request.urlopen(req, timeout=5)
        try:
            ok = r.status == 200
        finally:
            r.close()
        logger.info(f"Slack notification sent: {title}")
        return ok
    except Exception as e:
        logger.error(f"Slack notification failed: {e}")
        return False


# ============================================================
# EMAIL
# ============================================================

def send_email(
    to: List[str],
    subject: str,
    body: str,
    html: bool = False,
) -> bool:
    """Send an email via SMTP."""
    if not SMTP_HOST:
        logger.info(
            "Email notification skipped: AINXT_SMTP_HOST not configured. "
            "Set AINXT_SMTP_HOST in .env to enable email notifications."
        )
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_FROM
        msg["To"]      = ", ".join(to)
        mime_type = "html" if html else "plain"
        msg.attach(MIMEText(body, mime_type))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            if SMTP_USER and SMTP_PASSWORD:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_FROM, to, msg.as_string())

        logger.info(f"Email sent to {to}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Email notification failed: {e}")
        return False


# ============================================================
# WHATSAPP (Meta Business Cloud API)
# ============================================================

def send_whatsapp(
    to_phone: str,
    message: str,
) -> bool:
    """
    Send a WhatsApp message via Meta Business Cloud API.
    to_phone: E.164 format, e.g. +919876543210
    """
    if not WHATSAPP_API_URL or not WHATSAPP_ACCESS_TOKEN:
        logger.warning("WhatsApp notification skipped: credentials not set")
        return False
    try:
        payload = json.dumps({
            "messaging_product": "whatsapp",
            "to":                to_phone.lstrip("+"),
            "type":              "text",
            "text":              {"body": message},
        }).encode()
        req = urllib.request.Request(
            WHATSAPP_API_URL,
            data=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
            },
        )
        r = urllib.request.urlopen(req, timeout=5)
        try:
            ok = r.status == 200
        finally:
            r.close()
        logger.info(f"WhatsApp sent to {to_phone}")
        return ok
    except Exception as e:
        logger.error(f"WhatsApp notification failed: {e}")
        return False


# ============================================================
# UNIFIED NOTIFY (fire-and-forget in background thread)
# ============================================================

def notify(
    event_type: str,
    title: str,
    message: str,
    severity: str = "info",
    channels: Optional[List[str]] = None,
    email_recipients: Optional[List[str]] = None,
    whatsapp_numbers: Optional[List[str]] = None,
    fields: Optional[dict] = None,
) -> None:
    """
    Send a notification to one or more channels, non-blocking.

    event_type: incident_detected | security_alert | agent_failure |
                compliance_violation | model_cost_exceeded | workflow_failed
    channels:   list of "slack" | "email" | "whatsapp" (defaults from event_type)
    """
    ch = channels or _DEFAULT_CHANNELS.get(event_type, _DEFAULT_CHANNELS["default"])

    def _send():
        if "slack" in ch:
            send_slack(title, message, severity, fields)

        if "email" in ch:
            recipients = email_recipients or ([SMTP_USER] if SMTP_USER else [])
            if recipients:
                html_body = f"""
                <h3>{title}</h3>
                <p><b>Severity:</b> {severity.upper()}</p>
                <p>{message}</p>
                {''.join(f'<p><b>{k}:</b> {v}</p>' for k, v in (fields or {}).items())}
                <hr/><small>AiNxt Enterprise — AiNxt</small>
                """
                send_email(recipients, f"[{severity.upper()}] {title}", html_body, html=True)

        if "whatsapp" in ch:
            numbers = whatsapp_numbers or []
            wa_msg  = f"[{severity.upper()}] {title}\n{message}"
            for num in numbers:
                send_whatsapp(num, wa_msg)

    t = threading.Thread(target=_send, daemon=True)
    t.start()


# ============================================================
# CONVENIENCE SHORTCUTS
# ============================================================

def notify_incident(title: str, details: str, fields: dict = None):
    notify("incident_detected", title, details, severity="high", fields=fields)

def notify_security(title: str, details: str):
    notify("security_alert", title, details, severity="critical")

def notify_agent_failure(agent_name: str, error: str):
    notify("agent_failure", f"Agent Failed: {agent_name}", error, severity="high",
           fields={"agent": agent_name})

def notify_cost_exceeded(user: str, usage: float, limit: float):
    notify("model_cost_exceeded",
           f"Budget Exceeded: {user}",
           f"Usage ${usage:.2f} exceeded limit ${limit:.2f}",
           severity="high",
           fields={"user": user, "usage": f"${usage:.2f}", "limit": f"${limit:.2f}"})
