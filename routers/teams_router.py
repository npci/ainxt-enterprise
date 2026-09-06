# SPDX-License-Identifier: MIT
# ============================================================
# TEAMS ROUTER — /teams
#
# Endpoints:
#   POST /teams/messages        Main Bot Framework webhook
#   POST /teams/notify          Internal proactive notification trigger
#   GET  /teams/metrics         Prometheus-style metrics for Teams channel
#   GET  /teams/health          Health check for Azure Bot Service verification
# ============================================================

import json
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.logger import logger

router = APIRouter(prefix="/teams", tags=["teams"])


# ── Internal notification request model ──────────────────────────────────────

class NotifyRequest(BaseModel):
    thread_id:  str
    event:      str          # pr_created | bug_fixed | workflow_done | approval_required | failed
    run_id:     Optional[str] = ""
    jira_key:   Optional[str] = ""
    pr_url:     Optional[str] = ""
    branch:     Optional[str] = ""
    state:      Optional[str] = ""   # SDLC state for approval cards
    summary:    Optional[str] = ""
    message:    Optional[str] = ""   # freeform override
    workflow:   Optional[str] = ""
    error:      Optional[str] = ""

class ProcessMeetingRequest(BaseModel):
    meeting_id:   str                       # Graph onlineMeeting id
    organizer_id: str                       # Entra user id (oid) of the meeting organizer
    detected_via: Optional[str] = "manual"

# ── POST /teams/messages ──────────────────────────────────────────────────────

@router.post("/messages")
async def teams_messages(
    request: Request,
    authorization: Optional[str] = Header(default=None),
):
    """
    Primary Bot Framework webhook.

    Microsoft Teams / Azure Bot Service will POST every activity here:
      • type: message  → user sent a message
      • type: invoke   → Adaptive Card button clicked
      • type: conversationUpdate → bot added/removed from channel

    Authentication:
      Validates the Bearer JWT from Microsoft using Bot Framework public keys.
      Set TEAMS_SKIP_AUTH=true for local development.

    Azure Bot Service messaging endpoint:
      https://<your-domain>/teams/messages
    """
    body_bytes = await request.body()

    # ── JWT validation ────────────────────────────────────────────────────────
    from services.teams_adapter import validate_teams_token
    if not validate_teams_token(authorization or ""):
        logger.warning("teams/messages: rejected — invalid Bot Framework token")
        raise HTTPException(status_code=401, detail="Unauthorized: invalid Bot Framework token")

    # ── Parse activity ────────────────────────────────────────────────────────
    try:
        activity = json.loads(body_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    activity_type = activity.get("type", "")
    logger.info(
        f"teams/messages: type={activity_type} "
        f"conv={str((activity.get('conversation') or {}).get('id', ''))[:16]}"
    )

    # ── conversationUpdate — bot added/installed ──────────────────────────────
    if activity_type == "conversationUpdate":
        members_added = activity.get("membersAdded", [])
        bot_id        = (activity.get("recipient") or {}).get("id", "")
        service_url   = activity.get("serviceUrl", "")
        conv_id       = (activity.get("conversation") or {}).get("id", "")

        if service_url and conv_id:
            from services.teams_adapter import store_service_url
            store_service_url(conv_id, service_url)

        # Greet if the bot itself was added
        for m in members_added:
            if m.get("id") == bot_id:
                try:
                    from integrations.teams_client import send_message
                    send_message(
                        service_url, conv_id,
                        "👋 **AiNxt is here!**\n\n"
                        "Mention me with `@AiNxt <command>` to get started.\n\n"
                        "**Examples:**\n"
                        "- `@AiNxt fix AiNxt-123` — run bug pipeline\n"
                        "- `@AiNxt feature AiNxt-456` — run feature pipeline\n"
                        "- `@AiNxt review PR 128` — review a pull request\n"
                        "- `@AiNxt explain this error: …` — ask anything",
                    )
                except Exception as e:
                    logger.warning(f"teams: greeting failed → {e}")
        return JSONResponse({"ok": True})

    # ── message / invoke → dispatch ───────────────────────────────────────────
    if activity_type in ("message", "invoke"):
        # ── Adaptive Card submit (HITL) — handle synchronously ───────────────
        value = activity.get("value") or {}
        if value.get(os.getenv("TEAMS_ACTION_KEY", "ainxt_action")) in ("hitl_approve", "hitl_reject"):
            from services.teams_adapter import dispatch_activity
            dispatch_activity(activity)
            return JSONResponse(content={"status": 200}, status_code=200)

        # ── Credentials present → full async POST-back flow ──────────────────
        if os.getenv("TEAMS_BOT_APP_ID") and os.getenv("TEAMS_BOT_SECRET"):
            from services.teams_adapter import dispatch_activity
            result = dispatch_activity(activity)
            logger.info(f"teams/messages: dispatch result={result}")
            if activity_type == "invoke":
                return JSONResponse(content={"status": 200}, status_code=200)
            return JSONResponse({"ok": True})

        # ── No credentials configured ─────────────────────────────────────────
        logger.error(
            "teams/messages: TEAMS_BOT_APP_ID or TEAMS_BOT_SECRET not set — "
            "bot cannot reply. Set both in .env or .zshrc and restart the server."
        )
        return JSONResponse({"ok": False, "error": "Bot credentials not configured"})

    # All other activity types — acknowledge silently
    return JSONResponse({"ok": True})


# ── POST /teams/notify ────────────────────────────────────────────────────────

@router.post("/notify")
def teams_notify(body: NotifyRequest):
    """
    Internal endpoint for AiNxt pipeline components to push proactive
    notifications to a Teams conversation.

    Called by:
      - sdlc_pipeline.py at HITL gates
      - webhooks_router.py on PR events
      - Any background job that needs to notify the Teams originator

    Not exposed externally — should be called from within the platform only.
    """
    from services.teams_notifier import teams_notifier as _notifier

    event = body.event

    if event == "pr_created":
        _notifier.notify_pr_created(
            body.thread_id, body.pr_url, body.run_id, body.branch
        )
    elif event == "bug_fixed":
        _notifier.notify_bug_fixed(
            body.thread_id, body.jira_key, body.run_id, body.pr_url
        )
    elif event == "workflow_done":
        _notifier.notify_workflow_completed(
            body.thread_id, body.workflow, body.run_id
        )
    elif event == "approval_required":
        _notifier.notify_approval_required(
            body.thread_id, body.run_id, body.state, body.summary
        )
    elif event == "failed":
        _notifier.notify_pipeline_failed(
            body.thread_id, body.run_id, body.error
        )
    elif event == "message" and body.message:
        _notifier.send(body.thread_id, body.message)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown event type: {event}")

    return {"ok": True}

# ── POST /teams/meeting/process ───────────────────────────────────────────────

@router.post("/meeting/process")
def teams_meeting_process(
        body: ProcessMeetingRequest,
        authorization: Optional[str] = Header(default=None),
):
    """Manually trigger post-meeting automation for one online meeting.

    Fetches the transcript via app-only Graph, generates Minutes of Meeting
    inside AiNxt, and distributes them — the same job the poll/webhook paths
    enqueue. Requires a valid platform JWT (admin). Used for testing before the
    polling/webhook detectors are live.
    """
    # ── Auth: require a valid platform JWT (admin) ────────────────────────────
    token = (authorization or "")[7:].strip() if (authorization or "").lower().startswith("bearer ") else ""
    try:
        from auth.jwt_handler import decode_token
        claims = decode_token(token) if token else None
    except Exception:
        claims = None
    if not claims:
        raise HTTPException(status_code=401, detail="Unauthorized: valid bearer token required")
    if claims.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")

    try:
        from core.job_queue import enqueue_job, Q_CONNECTOR
        job_id = enqueue_job(
            "workers.meeting_worker.run_post_meeting_job",
            {"meeting_id": body.meeting_id, "organizer_id": body.organizer_id,
             "detected_via": body.detected_via or "manual"},
            queue_name=Q_CONNECTOR,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    logger.info(f"teams/meeting/process: enqueued meeting={body.meeting_id} job={job_id}")
    return {"ok": True, "job_id": job_id, "meeting_id": body.meeting_id}

# ── GET /teams/metrics ────────────────────────────────────────────────────────

@router.get("/metrics")
def teams_metrics_endpoint():
    """
    Returns Teams integration metrics in Prometheus text format + JSON.

    Metrics:
      ainxt_teams_requests_total      — total incoming Teams messages
      ainxt_teams_agent_runs_total    — agent/pipeline invocations
      ainxt_teams_success_total       — successful completions
      ainxt_teams_failure_total       — failures (compliance, budget, errors)
      ainxt_teams_success_rate        — success / (success + failure)
      ainxt_teams_avg_latency_ms      — average orchestrator latency
    """
    from services.teams_adapter import teams_metrics as _m
    snap = _m.snapshot()

    # Prometheus text format
    prom_lines = "\n".join([
        f"# HELP ainxt_teams_requests_total Total Teams messages received",
        f"# TYPE ainxt_teams_requests_total counter",
        f"ainxt_teams_requests_total {snap['requests_total']}",
        f"# HELP ainxt_teams_agent_runs_total Agent/pipeline invocations from Teams",
        f"# TYPE ainxt_teams_agent_runs_total counter",
        f"ainxt_teams_agent_runs_total {snap['agent_runs_total']}",
        f"# HELP ainxt_teams_success_total Successful completions",
        f"# TYPE ainxt_teams_success_total counter",
        f"ainxt_teams_success_total {snap['success_total']}",
        f"# HELP ainxt_teams_failure_total Failures",
        f"# TYPE ainxt_teams_failure_total counter",
        f"ainxt_teams_failure_total {snap['failure_total']}",
        f"# HELP ainxt_teams_success_rate Success rate (0.0–1.0)",
        f"# TYPE ainxt_teams_success_rate gauge",
        f"ainxt_teams_success_rate {snap['success_rate']}",
        f"# HELP ainxt_teams_avg_latency_ms Average orchestrator latency in ms",
        f"# TYPE ainxt_teams_avg_latency_ms gauge",
        f"ainxt_teams_avg_latency_ms {snap['avg_latency_ms']}",
    ])

    return {
        **snap,
        "prometheus": prom_lines,
        "source": "teams",
    }


# ── OPTIONS /teams/messages — CORS preflight for Azure Portal ─────────────────

@router.options("/messages")
async def teams_messages_preflight():
    """
    Explicit OPTIONS handler so Azure Portal's preflight check succeeds.
    FastAPI CORSMiddleware handles this automatically, but Azure Portal
    makes the preflight FROM THE BROWSER which requires an explicit 200.
    """
    return JSONResponse(
        content={"ok": True},
        headers={
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        },
    )


# ── GET /teams/health ─────────────────────────────────────────────────────────

@router.get("/health")
def teams_health():
    """
    Simple health probe.
    Azure Bot Service can optionally ping this to verify the bot is reachable.
    """
    app_id_set = bool(os.getenv("TEAMS_BOT_APP_ID"))
    secret_set = bool(os.getenv("TEAMS_BOT_SECRET"))
    return {
        "status":        "ok",
        "app_id_set":    app_id_set,
        "secret_set":    secret_set,
        "skip_auth":     os.getenv("TEAMS_SKIP_AUTH", "false"),
        "endpoint":      "POST /teams/messages",
    }
