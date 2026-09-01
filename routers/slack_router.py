# SPDX-License-Identifier: Apache-2.0
# ============================================================
# SLACK ROUTER — /slack
# Event dispatch (app_mention) + interactive button handler (HITL)
# ============================================================

import json
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from core.logger import logger
from core.slack_bot import verify_slack_signature, send_agent_response

router = APIRouter(prefix="/slack", tags=["slack"])

SLACK_DEFAULT_CHANNEL = os.getenv("SLACK_DEFAULT_CHANNEL", "#general")


# ── POST /slack/events ────────────────────────────────────────

@router.post("/events")
async def slack_events(
    request: Request,
    x_slack_request_timestamp: Optional[str] = Header(None),
    x_slack_signature:         Optional[str] = Header(None),
):
    """
    Receive Slack Events API payloads.

    Handles:
    - url_verification challenge (Slack setup handshake)
    - app_mention → invoke agent and reply in channel
    """
    body = await request.body()

    # Verify signature
    if not verify_slack_signature(body, x_slack_request_timestamp or "", x_slack_signature or ""):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # ── URL verification challenge ────────────────────────────
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge", "")})

    event = payload.get("event", {})
    event_type = event.get("type", "")

    # ── app_mention → run default agent ──────────────────────
    if event_type == "app_mention":
        text    = event.get("text", "")
        channel = event.get("channel", SLACK_DEFAULT_CHANNEL)
        user    = event.get("user", "")

        # Strip the bot mention (@AiNxt <message>)
        import re
        message = re.sub(r"<@[A-Z0-9]+>", "", text).strip()

        logger.info(f"slack/events: app_mention from {user} in {channel}")

        # Run agent in background
        import threading
        def _reply():
            try:
                from core.job_queue import enqueue_agent_job
                enqueue_agent_job("sdlc-coding-agent", message)
                # Quick acknowledgement
                send_agent_response(
                    channel,
                    f"<@{user}> Processing your request: _{message[:200]}_"
                )
            except Exception as e:
                logger.error(f"slack/events: agent dispatch failed → {e}")
                send_agent_response(channel, f"<@{user}> Sorry, I encountered an error: {e}")

        threading.Thread(target=_reply, daemon=True).start()
        return {"ok": True}

    # All other events — acknowledge
    return {"ok": True}


# ── POST /slack/interactions ──────────────────────────────────

@router.post("/interactions")
async def slack_interactions(
    request: Request,
    x_slack_request_timestamp: Optional[str] = Header(None),
    x_slack_signature:         Optional[str] = Header(None),
):
    """
    Receive Slack interactive component payloads (button clicks).

    Handles HITL approval / rejection buttons:
      hitl_approve_{run_id} → approve the SDLC run
      hitl_reject_{run_id}  → reject the SDLC run
    """
    body = await request.body()

    if not verify_slack_signature(body, x_slack_request_timestamp or "", x_slack_signature or ""):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # Interactions arrive as form-encoded payload= field
    from urllib.parse import parse_qs, unquote_plus
    try:
        decoded = unquote_plus(body.decode())
        qs = parse_qs(decoded)
        payload_str = qs.get("payload", ["{}"])[0]
        payload = json.loads(payload_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid interaction payload")

    actions = payload.get("actions", [])
    user    = (payload.get("user") or {}).get("name", "slack-user")
    channel = (payload.get("channel") or {}).get("id", SLACK_DEFAULT_CHANNEL)

    for action in actions:
        action_id = action.get("action_id", "")
        run_id    = action.get("value", "")

        if action_id.startswith("hitl_approve_"):
            _handle_hitl_approve(run_id, user, channel)
        elif action_id.startswith("hitl_reject_"):
            _handle_hitl_reject(run_id, user, channel)

    return {"ok": True}


# ── HITL helpers ──────────────────────────────────────────────

def _handle_hitl_approve(run_id: str, user: str, channel: str):
    logger.info(f"slack/interactions: HITL approve run={run_id} by={user}")
    try:
        from store.sdlc_store import get_run, update_run_state, add_run_event
        run = get_run(run_id)
        if not run:
            send_agent_response(channel, f"Run `{run_id}` not found.")
            return

        state = run["state"]
        _allowed = {"AWAITING_DESIGN_APPROVAL", "AWAITING_SOLUTION_APPROVAL", "AWAITING_PR_APPROVAL"}
        if state not in _allowed:
            send_agent_response(channel, f"Run `{run_id}` is in state `{state}` — approval not applicable.")
            return

        add_run_event(run_id, state, "APPROVED", actor=user, output=f"Approved via Slack by {user}")
        update_run_state(run_id, "APPROVED", context_patch={"approved_by": user, "approved_via": "slack"})

        import threading
        def _resume():
            try:
                if state == "AWAITING_DESIGN_APPROVAL":
                    from agents.sdlc_pipeline import resume_feature_after_design_approval
                    resume_feature_after_design_approval(run_id, "")
                elif state == "AWAITING_SOLUTION_APPROVAL":
                    from agents.sdlc_pipeline import resume_bug_after_solution_approval
                    resume_bug_after_solution_approval(run_id, "")
                elif state == "AWAITING_PR_APPROVAL":
                    from agents.sdlc_pipeline import resume_after_pr_approval
                    resume_after_pr_approval(run_id)
            except Exception as e:
                logger.error(f"slack: resume failed → {e}")

        threading.Thread(target=_resume, daemon=True).start()
        send_agent_response(channel, f"✅ Run `{run_id}` approved by <@{user}>. Pipeline resuming...")
    except Exception as e:
        logger.error(f"slack: HITL approve error → {e}")
        send_agent_response(channel, f"Error approving run `{run_id}`: {e}")


def _handle_hitl_reject(run_id: str, user: str, channel: str):
    logger.info(f"slack/interactions: HITL reject run={run_id} by={user}")
    try:
        from store.sdlc_store import get_run, update_run_state, add_run_event
        run = get_run(run_id)
        if not run:
            send_agent_response(channel, f"Run `{run_id}` not found.")
            return

        state = run["state"]
        reason = f"Rejected via Slack by {user}"
        add_run_event(run_id, state, "FAILED", actor=user, output=reason)
        update_run_state(run_id, "FAILED", error=reason, context_patch={"rejected_by": user})
        send_agent_response(channel, f"❌ Run `{run_id}` rejected by <@{user}>.")
    except Exception as e:
        logger.error(f"slack: HITL reject error → {e}")
        send_agent_response(channel, f"Error rejecting run `{run_id}`: {e}")
