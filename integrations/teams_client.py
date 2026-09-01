# SPDX-License-Identifier: Apache-2.0
# ============================================================
# TEAMS CLIENT — low-level Bot Framework HTTP client
#
# Responsibilities:
#   • Obtain and cache OAuth2 access tokens from Microsoft
#   • Send plain text messages to Teams conversations
#   • Send Adaptive Cards (HITL approve/reject panels)
# ============================================================

import os
import time
import threading
import requests

from connectors.net_relay import relay_request
from core.logger import logger

TEAMS_APP_ID     = os.getenv("TEAMS_BOT_APP_ID", "")
TEAMS_APP_SECRET = os.getenv("TEAMS_BOT_SECRET", "")

# Optional: set to your Azure AD tenant ID or domain to skip auto-discovery
# e.g. TEAMS_BOT_TENANT_ID=bharathieduacademygmail.onmicrosoft.com
_TENANT = os.getenv("TEAMS_BOT_TENANT_ID", "")

# OAuth2 token cache — refresh 60 s before expiry
_token_lock  = threading.Lock()
_token_cache: dict = {"access_token": None, "expires_at": 0.0}

_BOT_SCOPE = "https://api.botframework.com/.default"

# Ordered list of tenant endpoints to try. Apps registered directly in Azure AD
# (not via dev.botframework.com) live in the user's own tenant — try "common"
# first so they work without any extra config.
def _token_urls() -> list:
    urls = []
    if _TENANT:
        urls.append(f"https://login.microsoftonline.com/{_TENANT}/oauth2/v2.0/token")
    else:
        urls.append("https://login.microsoftonline.com/common/oauth2/v2.0/token")
        urls.append("https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token")
    return urls


# ── Token management ──────────────────────────────────────────────────────────

def _get_access_token() -> str:
    """Return a valid Bot Framework OAuth2 access token (cached, thread-safe)."""
    with _token_lock:
        now = time.time()
        if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
            return _token_cache["access_token"]

        if not TEAMS_APP_ID or not TEAMS_APP_SECRET:
            raise RuntimeError(
                "TEAMS_BOT_APP_ID and TEAMS_BOT_SECRET environment variables are required."
            )

        last_err = None
        for url in _token_urls():
            try:
                # this host has no internet — relay through the LLM proxy server's LLM proxy.
                resp = relay_request(
                    "POST",
                    url,
                    data={
                        "grant_type":    "client_credentials",
                        "client_id":     TEAMS_APP_ID,
                        "client_secret": TEAMS_APP_SECRET,
                        "scope":         _BOT_SCOPE,
                    },
                    timeout=10,
                )
                if resp.status_code == 400:
                    err_body = resp.json() if resp.content else {}
                    err_desc = err_body.get("error_description", "")
                    # AADSTS700016 = app not in this tenant — try next endpoint
                    if "AADSTS700016" in err_desc:
                        logger.warning(f"TeamsClient: token endpoint {url} → AADSTS700016, trying next")
                        last_err = RuntimeError(err_desc[:200])
                        continue
                resp.raise_for_status()
                data = resp.json()
                _token_cache["access_token"] = data["access_token"]
                _token_cache["expires_at"]   = now + data.get("expires_in", 3600)
                logger.info(f"TeamsClient: OAuth2 token refreshed via {url}")
                # Decode and log token claims for diagnostics
                try:
                    import base64 as _b64, json as _json
                    _parts = data["access_token"].split(".")
                    _pad   = lambda s: s + "=" * (-len(s) % 4)
                    _claims = _json.loads(_b64.urlsafe_b64decode(_pad(_parts[1])))
                    logger.info(
                        f"TeamsClient: token claims — aud={_claims.get('aud')} "
                        f"iss={_claims.get('iss')} appid={_claims.get('appid')} "
                        f"tid={_claims.get('tid')}"
                    )
                except Exception:
                    pass
                return _token_cache["access_token"]
            except RuntimeError:
                raise
            except Exception as e:
                last_err = e
                logger.warning(f"TeamsClient: token fetch failed on {url} → {e}")

        logger.error(f"TeamsClient: all token endpoints failed. Last error: {last_err}")
        raise last_err or RuntimeError("All token endpoints failed")


# ── Activity helpers ──────────────────────────────────────────────────────────

def _activity_url(service_url: str, conversation_id: str) -> str:
    return f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"


def _post_activity(service_url: str, conversation_id: str, payload: dict,
                   reply_to_id: str = None) -> bool:
    """POST a Bot Framework activity to Teams. Returns True on success."""
    try:
        token = _get_access_token()
        # Always POST to .../activities — never append activity ID to URL path.
        # For replies, set replyToId inside the payload body instead.
        url = _activity_url(service_url, conversation_id)

        # Bot Framework requires 'from' (bot identity) in every outbound activity
        payload.setdefault("from", {"id": TEAMS_APP_ID, "name": "AiNxt"})
        payload.setdefault("conversation", {"id": conversation_id})
        if reply_to_id:
            payload["replyToId"] = reply_to_id

        # Relay through the LLM proxy server (the bot serviceUrl is a Microsoft cloud host).
        resp = relay_request(
            "POST",
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            timeout=15,
        )
        if not resp.is_success:
            logger.error(
                f"TeamsClient: POST {url} → {resp.status_code}: {resp.text[:200]}"
            )
            return False
        return True
    except Exception as e:
        logger.error(f"TeamsClient: _post_activity failed → {e}")
        return False


# ── Public API ────────────────────────────────────────────────────────────────

def send_message(service_url: str, conversation_id: str,
                 text: str, reply_to_id: str = None) -> bool:
    """
    Send a plain-text (markdown) message to a Teams conversation.

    Args:
        service_url:     Bot Framework service URL from the incoming activity.
        conversation_id: Teams conversation ID.
        text:            Message body (markdown supported).
        reply_to_id:     Activity ID to reply to (keeps messages threaded).
    """
    payload = {
        "type":       "message",
        "textFormat": "markdown",
        "text":       text,
    }
    return _post_activity(service_url, conversation_id, payload, reply_to_id)


def send_adaptive_card(service_url: str, conversation_id: str,
                       card: dict, reply_to_id: str = None) -> bool:
    """
    Send an Adaptive Card attachment (e.g. HITL approval panel).

    Args:
        card: Full Adaptive Card JSON (type, version, body, actions).
    """
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content":     card,
        }],
    }
    return _post_activity(service_url, conversation_id, payload, reply_to_id)


def build_hitl_card(run_id: str, state: str, summary: str) -> dict:
    """
    Build an Adaptive Card for SDLC HITL approval/rejection.

    The card data is submitted back to /teams/messages as an invoke or
    messageBack activity and handled by the router.
    """
    stage_label = {
        "PENDING_APPROVAL":           "Solution Proposal — Approve to Start Pipeline",
        "AWAITING_DESIGN_APPROVAL":   "Design Review",
        "AWAITING_SOLUTION_APPROVAL": "Solution Review",
        "AWAITING_PR_APPROVAL":       "PR Review",
    }.get(state, state)

    # Pull linked URLs from run context (best-effort — never block card creation)
    gitlab_issue_url = ""
    confluence_url   = ""
    jira_url         = ""
    pr_url           = ""
    try:
        from store.sdlc_store import get_run as _get_run
        _ctx = (_get_run(run_id) or {}).get("context") or {}
        gitlab_issue_url = _ctx.get("gitlab_issue_url", "")
        confluence_url   = _ctx.get("confluence_url", "")
        jira_url         = _ctx.get("jira_url", "")
        pr_url           = (_get_run(run_id) or {}).get("pr_url", "") or _ctx.get("pr_url", "")
    except Exception:
        pass

    # Build facts list — add URL rows when available
    facts = [
        {"title": "Run ID",  "value": run_id[:12] + "…"},
        {"title": "Stage",   "value": stage_label},
        {"title": "Summary", "value": summary[:300]},
    ]
    if jira_url:
        facts.append({"title": "Jira", "value": jira_url})
    if gitlab_issue_url:
        facts.append({"title": "GitLab Issue", "value": gitlab_issue_url})
    if confluence_url:
        facts.append({"title": "Confluence", "value": confluence_url})
    if pr_url:
        facts.append({"title": "Pull Request", "value": pr_url})

    # Primary approve/reject actions
    actions = [
        {
            "type":  "Action.Submit",
            "title": "✅ Approve",
            "style": "positive",
            "data":  {os.getenv("TEAMS_ACTION_KEY", "ainxt_action"): "hitl_approve", "run_id": run_id},
        },
        {
            "type":  "Action.Submit",
            "title": "❌ Reject",
            "style": "destructive",
            "data":  {os.getenv("TEAMS_ACTION_KEY", "ainxt_action"): "hitl_reject", "run_id": run_id},
        },
    ]
    # Link buttons — only shown when URLs are available
    if gitlab_issue_url:
        actions.append({
            "type":  "Action.OpenUrl",
            "title": "🐛 Open GitLab Issue",
            "url":   gitlab_issue_url,
        })
    if confluence_url:
        actions.append({
            "type":  "Action.OpenUrl",
            "title": "📄 View Design Doc",
            "url":   confluence_url,
        })
    if pr_url:
        actions.append({
            "type":  "Action.OpenUrl",
            "title": "🔀 Open PR",
            "url":   pr_url,
        })

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type":    "AdaptiveCard",
        "version": "1.3",
        "body": [
            {
                "type":   "TextBlock",
                "text":   f"🔔 AiNxt — {stage_label} Required",
                "weight": "Bolder",
                "size":   "Medium",
            },
            {
                "type":  "FactSet",
                "facts": facts,
            },
        ],
        "actions": actions,
    }
