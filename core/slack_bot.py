# SPDX-License-Identifier: Apache-2.0
# ============================================================
# SLACK BOT — bidirectional agent interface
# Handles event dispatch, HITL approval messages, and replies.
# ============================================================

import hashlib
import hmac
import json
import os
import time
from typing import Optional

from core.logger import logger

SLACK_BOT_TOKEN       = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET  = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_DEFAULT_CHANNEL = os.getenv("SLACK_DEFAULT_CHANNEL", "#general")


# ── Signature verification ────────────────────────────────────

def verify_slack_signature(body: bytes, timestamp: str, sig: str) -> bool:
    """
    Verify a Slack request signature using HMAC-SHA256.
    Returns True if valid or if SLACK_SIGNING_SECRET is not configured.
    """
    if not SLACK_SIGNING_SECRET:
        return True

    if not sig or not timestamp:
        return False

    # Reject requests older than 5 minutes
    try:
        if abs(time.time() - float(timestamp)) > 300:
            logger.warning("slack_bot: stale request (timestamp > 5min)")
            return False
    except ValueError:
        return False

    base = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


# ── HTTP helper ───────────────────────────────────────────────

def _post_slack(endpoint: str, payload: dict) -> dict:
    """POST to Slack API. Returns parsed JSON response."""
    import urllib.request

    url = f"https://slack.com/api/{endpoint}"
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type":  "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            result = json.loads(body)
            if not result.get("ok"):
                logger.warning(f"slack_bot: API error → {result.get('error')}")
            return result
    except Exception as e:
        logger.error(f"slack_bot: HTTP error → {e}")
        return {"ok": False, "error": str(e)}


# ── HITL approval message ─────────────────────────────────────

def send_hitl_approval_message(
    channel: str,
    run_id: str,
    run_type: str,
    summary: str,
) -> bool:
    """
    Post a Block Kit message with Approve / Reject buttons to a Slack channel.
    Returns True on success.
    """
    if not SLACK_BOT_TOKEN:
        logger.warning("slack_bot: SLACK_BOT_TOKEN not set — skipping HITL message")
        return False

    channel = channel or SLACK_DEFAULT_CHANNEL

    payload = {
        "channel": channel,
        "text":    f"HITL Approval needed for {run_type} run `{run_id}`",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f":hourglass: HITL Approval — {run_type.upper()}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Run ID:* `{run_id}`\n*Summary:* {summary[:500]}",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type":      "button",
                        "text":      {"type": "plain_text", "text": "Approve ✅"},
                        "style":     "primary",
                        "action_id": f"hitl_approve_{run_id}",
                        "value":     run_id,
                    },
                    {
                        "type":      "button",
                        "text":      {"type": "plain_text", "text": "Reject ❌"},
                        "style":     "danger",
                        "action_id": f"hitl_reject_{run_id}",
                        "value":     run_id,
                    },
                ],
            },
        ],
    }

    result = _post_slack("chat.postMessage", payload)
    if result.get("ok"):
        logger.info(f"slack_bot: HITL message sent for run {run_id} → channel {channel}")
    return result.get("ok", False)


# ── Agent reply ───────────────────────────────────────────────

def send_agent_response(channel: str, text: str) -> bool:
    """Post an agent response as a plain message to a Slack channel."""
    if not SLACK_BOT_TOKEN:
        return False
    result = _post_slack("chat.postMessage", {"channel": channel, "text": text})
    return result.get("ok", False)
