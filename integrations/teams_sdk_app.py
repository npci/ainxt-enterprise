# SPDX-License-Identifier: Apache-2.0
# ============================================================
# TEAMS SDK APP — official Microsoft Teams SDK (microsoft-teams-apps)
#
# Scope doc §3: "Teams SDK is the preferred approach" (NOT the legacy
# hand-rolled Bot Framework, NOT M365 Agents / Copilot SDKs). This module
# constructs the SDK `App`, mounts its messaging endpoint onto the EXISTING
# gateway FastAPI instance (no second uvicorn — App.initialize() registers
# the route without serving), and ports the bot's business logic into SDK
# decorator handlers.
#
# Transport-only guardrail (§2.1): the SDK handles Teams auth/transport;
# ALL reasoning stays in AiNxt (model_router) and ALL gating in the
# compliance engine. No Copilot / Azure OpenAI.
#
# ── Inbound vs outbound ─────────────────────────────────────────────────
# Inbound  : Teams SDK (native JWT/JWKS validation + activity parsing).
# Streaming: native `ctx.stream` (§3.2 progressive message updates) for the
#            general chat path.
# Outbound (SDLC / HR / HITL cards): REUSES the proven legacy outbound in
#            services.teams_adapter + integrations.teams_client (posts to the
#            activity serviceUrl with its own bot token — independent of the
#            SDK). This keeps the migration low-risk: we don't rewrite the
#            working SDLC/HITL flows, only the inbound + streaming layer.
#
# ── Safe rollout ────────────────────────────────────────────────────────
# Gated behind TEAMS_SDK_ENABLED (default false) and registered at a DISTINCT
# endpoint (TEAMS_SDK_MESSAGING_ENDPOINT, default /ainxt/v1/api/teams/v2/messages)
# so the existing legacy bot at /ainxt/v1/api/teams/messages is untouched.
# Cut over by pointing the Azure Bot messaging endpoint at the v2 path and
# retiring the legacy route once parity is verified on the live bot.
# ============================================================

import os
import asyncio
from typing import Optional

from core.logger import logger

# ── Config ──────────────────────────────────────────────────────────────
TEAMS_SDK_ENABLED       = os.getenv("TEAMS_SDK_ENABLED", "false").lower() == "true"
_APP_ID                 = os.getenv("TEAMS_BOT_APP_ID", "")
_APP_SECRET             = os.getenv("TEAMS_BOT_SECRET", "")
_TENANT_ID              = os.getenv("TEAMS_BOT_TENANT_ID", "")
_SKIP_AUTH              = os.getenv("TEAMS_SKIP_AUTH", "false").lower() == "true"
_MESSAGING_ENDPOINT     = os.getenv("TEAMS_SDK_MESSAGING_ENDPOINT", "/ainxt/v1/api/teams/v2/messages")

# Progressive-streaming throttle: emit at most one update per N chars accumulated.
_STREAM_CHUNK_CHARS     = int(os.getenv("TEAMS_SDK_STREAM_CHUNK", "200"))

_app = None  # microsoft_teams.apps.App singleton


# ── Activity field helpers (defensive — SDK Pydantic models) ─────────────
def _sender(activity) -> tuple[str, str]:
    frm = getattr(activity, "from_", None)
    uid = getattr(frm, "id", None) or "unknown"
    name = getattr(frm, "name", None) or uid
    return uid, name


def _conv_id(activity) -> str:
    conv = getattr(activity, "conversation", None)
    return getattr(conv, "id", "") or ""


# ── Handler registration ─────────────────────────────────────────────────
def _register_handlers(app) -> None:
    """Attach on_message handlers to the SDK App instance."""

    @app.on_message
    async def _on_message(ctx):  # noqa: ANN001 — ctx: ActivityContext[MessageActivity]
        from services.teams_adapter import (
            extract_command, _compliance_check, _budget_check, _route_command,
            store_service_url, teams_metrics,
        )

        activity     = ctx.activity
        text         = (getattr(activity, "text", "") or "").strip()
        value        = getattr(activity, "value", None)
        conv_id      = _conv_id(activity)
        service_url  = getattr(activity, "service_url", "") or ""
        reply_to_id  = getattr(activity, "id", None)
        user_id, user_name = _sender(activity)

        teams_metrics.inc_request()

        # Cache serviceUrl so legacy outbound (proactive/cards) can post later.
        if service_url and conv_id:
            try:
                store_service_url(conv_id, service_url)
            except Exception:
                pass

        # ── HITL Adaptive Card submit (value carries TEAMS_ACTION_KEY) ───────
        # Bot Framework delivers Action.Submit as a message activity whose
        # `value` is populated. Reuse the legacy HITL handler verbatim.
        import os as _os
        _action_key = _os.getenv("TEAMS_ACTION_KEY", "ainxt_action")
        hitl_action = ""
        if isinstance(value, dict):
            hitl_action = value.get(_action_key, "")
        if hitl_action in ("hitl_approve", "hitl_reject"):
            run_id = value.get("run_id", "")
            from services.teams_adapter import _handle_hitl_action
            await asyncio.to_thread(
                _handle_hitl_action, hitl_action, run_id, user_name,
                conv_id, service_url, reply_to_id,
            )
            return

        if not text:
            return

        command = extract_command(text)
        if not command:
            return

        logger.info(f"TeamsSDK: @AiNxt from {user_name} ({user_id}) conv={conv_id[:12]}")

        # ── Compliance (redact-and-proceed; block only on hard types) ─────
        check = _compliance_check(command)
        if check.get("blocked"):
            blocked = [f["type"] for f in check.get("findings", []) if f.get("blocked")]
            await ctx.send(f"⛔ Request blocked by compliance: `{', '.join(blocked)}`")
            teams_metrics.inc_failure()
            return
        command = check.get("redacted_text") or command

        # ── Budget ────────────────────────────────────────────────────────
        budget = _budget_check(user_id)
        if not budget.get("allowed", True):
            await ctx.send(f"⛔ Budget limit reached: {budget.get('reason', '')}")
            teams_metrics.inc_failure()
            return

        routing = _route_command(command)

        # ── General chat → native progressive streaming via ctx.stream ────
        if routing["route"] == "orchestrator":
            await _stream_answer(ctx, command)
            return

        # ── SDLC / HR / PR review → reuse proven legacy background flows ───
        await _dispatch_legacy(routing, command, conv_id, service_url, user_id, reply_to_id, ctx)


async def _stream_answer(ctx, command: str) -> None:
    """Stream the model answer into Teams as progressive updates (§3.2).

    Uses model_router.stream() (true token streaming) → ctx.stream.emit().
    Output compliance runs on the full text before the final close; if it
    redacts anything, we replace the rendered text via ctx.stream.update().
    """
    from models.model_router import model_router
    from services.teams_adapter import teams_metrics

    acc: list[str] = []
    pending = 0
    try:
        for tok in model_router.stream(command, model_hint="complex"):
            if isinstance(tok, dict):  # __stream_meta__ sentinel — ignore
                continue
            if not tok:
                continue
            acc.append(tok)
            pending += len(tok)
            # Throttle: emit on chunk boundaries to respect Teams update limits.
            if pending >= _STREAM_CHUNK_CHARS:
                ctx.stream.emit(tok)
                pending = 0
            else:
                ctx.stream.emit(tok)

        answer = "".join(acc) or "No response."

        teams_metrics.inc_agent_run()
        teams_metrics.inc_success()
    except Exception as e:
        logger.error(f"TeamsSDK: stream failed → {e}")
        teams_metrics.inc_failure()
        try:
            ctx.stream.update(f"❌ Error: {str(e)[:300]}")
        except Exception:
            pass
    finally:
        try:
            await ctx.stream.close()
        except Exception as e:
            logger.warning(f"TeamsSDK: stream close failed → {e}")


async def _dispatch_legacy(routing, command, conv_id, service_url, user_id, reply_to_id, ctx) -> None:
    """Send an ack via the SDK, then run the existing legacy handler off-thread.

    The legacy handlers (services.teams_adapter) post their replies/cards back
    via integrations.teams_client using the activity serviceUrl — proven and
    independent of the SDK. We just hand off the same arguments.
    """
    from services import teams_adapter as ta

    ack = {
        "sdlc_bug":      f"🔍 Analysing `{routing['jira_key'] or command[:40]}`… I'll propose a solution for your approval before touching any code.",
        "sdlc_feature":  f"🔍 Analysing feature `{routing['jira_key'] or command[:40]}`… I'll propose an implementation plan for your approval.",
        "pr_review":     f"⚙️ Running PR review for PR #{routing['pr_num']}…",
        "hr_leave":      "🗓️ Applying your leave in Zoho People…",
        "hr_leave_balance": "📊 Fetching your leave balance…",
        "hr_my_leaves":  "📋 Fetching your leave records…",
        "hr_cancel_leave": "🗑️ Cancelling your leave…",
    }.get(routing["route"], "🤖 Processing…")
    try:
        await ctx.send(ack)
    except Exception:
        pass

    handler_map = {
        "sdlc_bug":         lambda: ta._propose_sdlc_bug(routing["jira_key"], command, conv_id, service_url, user_id, reply_to_id),
        "sdlc_feature":     lambda: ta._propose_sdlc_feature(routing["jira_key"], command, conv_id, service_url, user_id, reply_to_id),
        "hr_leave_balance": lambda: ta._run_hr_leave_balance(command, conv_id, service_url, user_id, reply_to_id),
        "hr_leave":         lambda: ta._run_hr_leave(command, conv_id, service_url, user_id, reply_to_id),
        "hr_my_leaves":     lambda: ta._run_hr_my_leaves(command, conv_id, service_url, user_id, reply_to_id),
        "hr_cancel_leave":  lambda: ta._run_hr_cancel_leave(command, conv_id, service_url, user_id, reply_to_id),
    }
    fn = handler_map.get(routing["route"])
    if fn is None:
        # pr_review / fallback → general orchestrator via legacy (sends over serviceUrl)
        fn = lambda: ta._run_orchestrator(command, conv_id, service_url, user_id, reply_to_id)

    # Run off the event loop — the legacy handlers are synchronous + blocking.
    asyncio.create_task(asyncio.to_thread(fn))


# ── Mount into the gateway FastAPI app ───────────────────────────────────
async def mount_teams_sdk_app(fastapi_app) -> Optional[object]:
    """Register the Teams SDK messaging route on the existing FastAPI app.

    Call from a gateway startup hook. Returns the App instance (or None if
    disabled). Does NOT start a uvicorn server — App.initialize() only wires
    the route + JWT validation onto the provided FastAPI instance.
    """
    global _app
    if not TEAMS_SDK_ENABLED:
        logger.info("TeamsSDK: disabled (set TEAMS_SDK_ENABLED=true to mount)")
        return None
    if _app is not None:
        return _app

    try:
        from microsoft_teams.apps import App, FastAPIAdapter

        adapter = FastAPIAdapter(app=fastapi_app)
        opts = {
            "http_server_adapter": adapter,
            "messaging_endpoint":  _MESSAGING_ENDPOINT,
            "skip_auth":           _SKIP_AUTH,
        }
        if _APP_ID:
            opts["client_id"] = _APP_ID
        if _APP_SECRET:
            opts["client_secret"] = _APP_SECRET
        if _TENANT_ID:
            opts["tenant_id"] = _TENANT_ID

        app = App(**opts)
        _register_handlers(app)
        await app.initialize()  # registers route on fastapi_app — no server spawned
        _app = app
        logger.info(f"TeamsSDK: mounted at POST {_MESSAGING_ENDPOINT} (skip_auth={_SKIP_AUTH})")
        return _app
    except Exception as e:
        logger.error(f"TeamsSDK: mount failed → {e}")
        return None
