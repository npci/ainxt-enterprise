# SPDX-License-Identifier: MIT
# ============================================================
# TEAMS ADAPTER — processes incoming Bot Framework activities
#
# Responsibilities:
#   • Validate Bearer JWT from Microsoft Bot Framework
#   • Parse Bot Framework Activity objects
#   • Strip @AiNxt mention, route to correct agent/pipeline
#   • Map Teams conversation ↔ AiNxt thread (Redis)
#   • Run PCI/PII compliance checks on all input
#   • Check budget before agent execution
#   • Update Teams metrics
# ============================================================

import os
import re
import json
import time
import threading
from typing import Optional

from core.config import RDB_QUEUE, RDB_WORKFLOW
from core.kv import get_kv, KVClient, KVError

from core.logger import logger, bind_context

# ── Config ────────────────────────────────────────────────────────────────────

TEAMS_APP_ID     = os.getenv("TEAMS_BOT_APP_ID", "")
TEAMS_APP_SECRET = os.getenv("TEAMS_BOT_SECRET", "")
TEAMS_SKIP_AUTH  = os.getenv("TEAMS_SKIP_AUTH", "false").lower() == "true"

# DB=5 — Teams conversation mapping (queue KV).
# Backend selected via REDIS_CLIENT_CONFIG_DB5.
_redis_client: Optional[KVClient] = None
_redis_lock = threading.Lock()

CONV_TTL = 86400 * 30   # 30 days


def _get_redis() -> Optional[KVClient]:
    global _redis_client
    with _redis_lock:
        if _redis_client is None:
            try:
                c = get_kv(RDB_QUEUE, decode_responses=True)
                c.ping()
                _redis_client = c
            except KVError as e:
                logger.warning(f"TeamsAdapter: KV backend unavailable → {e}")
        return _redis_client


def _get_workflow_kv() -> Optional[KVClient]:
    """KV client for HITL pending state (DB=2, workflow)."""
    try:
        return get_kv(RDB_WORKFLOW, decode_responses=True)
    except KVError as e:
        logger.warning(f"TeamsAdapter: workflow KV unavailable → {e}")
        return None


# ── In-memory Teams metrics ───────────────────────────────────────────────────

class _TeamsMetrics:
    def __init__(self):
        self._lock   = threading.Lock()
        self.requests_total    = 0
        self.agent_runs_total  = 0
        self.success_total     = 0
        self.failure_total     = 0
        self.latency_sum_ms    = 0.0
        self.latency_count     = 0

    def inc_request(self):
        with self._lock: self.requests_total += 1

    def inc_agent_run(self):
        with self._lock: self.agent_runs_total += 1

    def inc_success(self):
        with self._lock: self.success_total += 1

    def inc_failure(self):
        with self._lock: self.failure_total += 1

    def record_latency(self, ms: float):
        with self._lock:
            self.latency_sum_ms += ms
            self.latency_count  += 1

    def snapshot(self) -> dict:
        with self._lock:
            avg = (self.latency_sum_ms / self.latency_count
                   if self.latency_count else 0.0)
            rate = (self.success_total / max(1, self.success_total + self.failure_total))
            return {
                "requests_total":   self.requests_total,
                "agent_runs_total": self.agent_runs_total,
                "success_total":    self.success_total,
                "failure_total":    self.failure_total,
                "success_rate":     round(rate, 4),
                "avg_latency_ms":   round(avg, 1),
            }

teams_metrics = _TeamsMetrics()


# ── JWT validation ────────────────────────────────────────────────────────────
# Microsoft Bot Framework tokens are signed RS256 JWTs.
# We verify using the public JWKS published by Microsoft.

_JWKS_CACHE: dict   = {}
_JWKS_FETCHED_AT    = 0.0
_JWKS_TTL           = 3600   # refresh JWKS every hour
_OPENID_META_URL    = os.getenv(
    "TEAMS_BOT_OPENID_META_URL",
    "https://login.microsoftonline.com/botframework.com/v2.0/.well-known/openid-configuration",
)


def _fetch_jwks() -> dict:
    global _JWKS_CACHE, _JWKS_FETCHED_AT
    now = time.time()
    if _JWKS_CACHE and now - _JWKS_FETCHED_AT < _JWKS_TTL:
        return _JWKS_CACHE

    try:
        # Route through web02 relay — app01 has no direct internet egress.
        # relay_request() falls back to direct when LLM_PROXY_URL is unset (dev).
        from connectors.net_relay import relay_request as _relay
        meta_resp = _relay("GET", _OPENID_META_URL, timeout=10)
        meta_resp.raise_for_status()
        meta = meta_resp.json()
        jwks_uri = meta.get("jwks_uri", "")
        if not jwks_uri:
            raise ValueError("No jwks_uri in OpenID metadata")
        keys_resp = _relay("GET", jwks_uri, timeout=10)
        keys_resp.raise_for_status()
        _JWKS_CACHE    = {k["kid"]: k for k in keys_resp.json().get("keys", [])}
        _JWKS_FETCHED_AT = now
        logger.info(f"TeamsAdapter: JWKS refreshed ({len(_JWKS_CACHE)} keys)")
    except Exception as e:
        logger.warning(f"TeamsAdapter: JWKS fetch failed → {e}")

    return _JWKS_CACHE


def validate_teams_token(authorization: str) -> bool:
    """
    Validate a Bot Framework Bearer JWT.

    Returns True if valid (or if TEAMS_SKIP_AUTH=true), False if invalid.
    Skips validation if credentials are not configured (dev mode).
    """
    if TEAMS_SKIP_AUTH:
        return True

    if not TEAMS_APP_ID:
        # No App ID configured → skip validation (local dev)
        return True

    if not authorization or not authorization.lower().startswith("bearer "):
        logger.warning("TeamsAdapter: missing or non-Bearer Authorization header")
        return False

    token = authorization[7:].strip()

    try:
        import base64
        import jwt as _jwt
        from jwt.algorithms import RSAAlgorithm

        # ── Peek at header (kid) and payload (aud) without verifying ─────────
        def _b64decode(s):
            s += "=" * (-len(s) % 4)
            return base64.urlsafe_b64decode(s)

        parts      = token.split(".")
        header     = json.loads(_b64decode(parts[0]))
        payload    = json.loads(_b64decode(parts[1]))
        kid        = header.get("kid", "")
        token_aud  = payload.get("aud", "")
        token_iss  = payload.get("iss", "")

        logger.info(
            f"TeamsAdapter: JWT kid={kid} iss={token_iss} aud={token_aud}"
        )

        # ── Fetch signing key ─────────────────────────────────────────────────
        jwks = _fetch_jwks()
        if kid not in jwks:
            logger.warning(f"TeamsAdapter: JWT kid='{kid}' not in JWKS")
            return False

        public_key = RSAAlgorithm.from_jwk(json.dumps(jwks[kid]))

        # ── Bot Framework valid audience values ───────────────────────────────
        # Azure Bot Service issues tokens with audience = the bot's App ID.
        # Newer AAD v2 app registrations may use the "api://botid-{appid}" format.
        # SECURITY: Do NOT include token_aud (attacker-controlled) in the
        # accepted set — only server-configured values are trusted.
        valid_audiences = [
            TEAMS_APP_ID,
            f"api://botid-{TEAMS_APP_ID}",
        ]

        # ── Validate issuer — hard block on mismatch ──────────────────────────
        _VALID_ISSUERS = {
            "https://api.botframework.com",
            f"https://sts.windows.net/{payload.get('tid', '')}/" if payload.get("tid") else "",
            f"https://login.microsoftonline.com/{payload.get('tid', '')}/v2.0" if payload.get("tid") else "",
        }
        _VALID_ISSUERS.discard("")

        if token_iss not in _VALID_ISSUERS:
            logger.warning(
                f"TeamsAdapter: JWT issuer '{token_iss}' not in valid set {_VALID_ISSUERS} — rejecting"
            )
            return False

        # Try each valid audience until one succeeds
        last_err = None
        for aud in valid_audiences:
            try:
                _jwt.decode(
                    token,
                    public_key,
                    algorithms=["RS256"],
                    audience=aud,
                    options={"verify_exp": True},
                )
                logger.info(f"TeamsAdapter: JWT valid (aud={aud})")
                return True
            except _jwt.InvalidAudienceError as e:
                last_err = e
                continue
            except Exception as e:
                last_err = e
                break

        logger.warning(f"TeamsAdapter: JWT validation failed → {last_err}")
        return False

    except Exception as e:
        logger.warning(f"TeamsAdapter: JWT validation failed → {e}")
        return False


# ── Conversation mapping ──────────────────────────────────────────────────────

def get_thread_for_conversation(conv_id: str) -> Optional[str]:
    rc = _get_redis()
    if rc:
        return rc.get(f"teams:conversation:{conv_id}")
    return None


def map_conversation_to_thread(conv_id: str, thread_id: str):
    rc = _get_redis()
    if rc:
        rc.set(f"teams:conversation:{conv_id}", thread_id, ex=CONV_TTL)
        rc.set(f"teams:thread_to_conv:{thread_id}", conv_id, ex=CONV_TTL)
    logger.info(f"TeamsAdapter: mapped conv={conv_id[:12]} → thread={thread_id[:12]}")


def get_conversation_for_thread(thread_id: str) -> Optional[str]:
    rc = _get_redis()
    if rc:
        return rc.get(f"teams:thread_to_conv:{thread_id}")
    return None


def store_service_url(conv_id: str, service_url: str):
    """Cache the serviceUrl so we can send proactive messages later."""
    rc = _get_redis()
    if rc:
        rc.set(f"teams:serviceurl:{conv_id}", service_url, ex=CONV_TTL)


def get_service_url(conv_id: str) -> Optional[str]:
    rc = _get_redis()
    if rc:
        return rc.get(f"teams:serviceurl:{conv_id}")
    return None


# ── Message parsing ───────────────────────────────────────────────────────────

_MENTION_RE = re.compile(r"<at>[^<]*</at>", re.IGNORECASE)

def extract_command(text: str) -> str:
    """
    Strip @AiNxt mention tags and leading @AiNxt text from the message.
    Returns the clean command string.
    """
    # Remove <at>AiNxt</at> HTML tags (Teams mention format)
    clean = _MENTION_RE.sub("", text or "").strip()
    # Also strip plain @AiNxt prefix
    clean = re.sub(r"^@AiNxt\s*", "", clean, flags=re.IGNORECASE).strip()
    return clean


def is_ainxt_mention(activity: dict) -> bool:
    """Return True if the activity mentions @AiNxt or is a direct message."""
    text      = activity.get("text", "")
    mentions  = activity.get("entities", [])
    conv_type = (activity.get("conversation") or {}).get("conversationType", "")

    # Direct message (1:1 with the bot)
    if conv_type in ("personal", ""):
        return True

    # Explicit @AiNxt text mention
    if re.search(r"@AiNxt", text, re.IGNORECASE):
        return True

    # Bot mention entity in metadata
    for ent in mentions:
        if ent.get("type") == "mention":
            mentioned = (ent.get("mentioned") or {}).get("name", "")
            if "AiNxt" in mentioned or "ainxt" in mentioned.lower():
                return True

    return False


# ── Command router ────────────────────────────────────────────────────────────
# Maps message text → pipeline/agent function

_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_PR_RE       = re.compile(r"\b(?:PR|pull.request)\s+#?(\d+)\b", re.IGNORECASE)
_REPO_RE     = re.compile(
    r'(?i)repo\s*:\s*'
    r'(?:https?://[^/]+/)?'
    r'([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)'
)


def _extract_repo_from_text(text: str) -> str:
    """Extract owner/repo from description text (same logic as webhooks_router._extract_repo)."""
    m = _REPO_RE.search(text or "")
    return m.group(1).rstrip("/") if m else ""


def _route_command(command: str) -> dict:
    """
    Parse a Teams command and return a routing descriptor.

    Returns:
        {
          "route":    "sdlc_bug" | "pr_review" | "orchestrator",
          "jira_key": str | None,
          "pr_num":   int | None,
          "message":  str,
        }
    """
    cmd_lower = command.lower().strip()

    # fix/bug JIRA-KEY → SDLC bug pipeline
    if re.match(r"^(fix|bug|triage|resolve)\b", cmd_lower):
        m = _JIRA_KEY_RE.search(command)
        return {
            "route":    "sdlc_bug",
            "jira_key": m.group(1) if m else None,
            "pr_num":   None,
            "message":  command,
        }

    # review PR <num> → PR review
    if re.match(r"^review\s+(pr|pull)", cmd_lower):
        m = _PR_RE.search(command)
        return {
            "route":   "pr_review",
            "jira_key": None,
            "pr_num":  int(m.group(1)) if m else None,
            "message": command,
        }

    # feature/implement → SDLC feature pipeline
    if re.match(r"^(feature|implement|new feature)\b", cmd_lower):
        m = _JIRA_KEY_RE.search(command)
        return {
            "route":    "sdlc_feature",
            "jira_key": m.group(1) if m else None,
            "pr_num":   None,
            "message":  command,
        }

    # my leaves / pending leaves
    if re.search(r"(my|pending|all|list|show).*(leave|leaves)", cmd_lower) or \
       re.match(r"^(leave|leaves)\s*(list|history|status)?$", cmd_lower):
        return {
            "route":    "hr_my_leaves",
            "jira_key": None,
            "pr_num":   None,
            "message":  command,
        }

    # cancel leave
    if re.match(r"^(cancel|withdraw|revoke)\s+(leave|my leave)", cmd_lower):
        return {
            "route":    "hr_cancel_leave",
            "jira_key": None,
            "pr_num":   None,
            "message":  command,
        }

    # check/show leave balance
    if re.match(r"^(check|show|my|get|view)\s+(leave|vacation).*(balance|available|remaining|left)?", cmd_lower) or \
       re.match(r"^(leave|vacation)\s+(balance|available|remaining|left)", cmd_lower) or \
       "leave balance" in cmd_lower or "how many.*leave" in cmd_lower:
        return {
            "route":    "hr_leave_balance",
            "jira_key": None,
            "pr_num":   None,
            "message":  command,
        }

    # apply/request leave → HR leave agent
    if re.match(r"^(apply|request|take|book)\s+(leave|vacation|time off|day off)\b", cmd_lower):
        return {
            "route":    "hr_leave",
            "jira_key": None,
            "pr_num":   None,
            "message":  command,
        }

    # Everything else → general orchestrator
    return {
        "route":    "orchestrator",
        "jira_key": None,
        "pr_num":   None,
        "message":  command,
    }


# ── Compliance check ──────────────────────────────────────────────────────────

def _compliance_check(text: str) -> dict:
    """
    Screen an inbound Teams command.

    Fails CLOSED. This previously returned `{"blocked": False, "findings": []}` on any exception,
    so an import error or a privacy-svc timeout silently disabled the primary PCI control on the
    whole Teams surface. `unavailable` lets the caller say why it refused, instead of reporting an
    empty list of blocked types.
    """
    try:
        from agents.compliance_engine import ComplianceEngine
        return ComplianceEngine().validate_input(text)
    except Exception as e:
        logger.critical(f"TeamsAdapter: compliance unavailable — refusing turn → {e}")
        return {"blocked": True, "unavailable": True, "findings": [], "redacted_text": ""}


# ── Budget check ──────────────────────────────────────────────────────────────

def _budget_check(user_id: str) -> dict:
    try:
        from store.budget_store import check_budget
        return check_budget(user_id)
    except Exception as e:
        logger.warning(f"TeamsAdapter: budget check failed → {e}")
        return {"allowed": True}


# ── Background execution ──────────────────────────────────────────────────────

def _fetch_jira_context(jira_key: str, command: str, default_issue_type: str = "Bug"):
    """Resolve Jira issue details; fall back to command text if Jira not configured."""
    summary     = command
    description = command
    issue_type  = default_issue_type
    priority    = "Medium"
    repo        = ""
    if jira_key:
        try:
            from tools.jira_tools import jira_get_issue_dict
            issue = jira_get_issue_dict(jira_key)
            if issue:
                summary     = issue.get("summary")     or command
                description = issue.get("description") or command
                issue_type  = issue.get("issue_type")  or default_issue_type
                priority    = issue.get("priority")    or "Medium"
                repo        = _extract_repo_from_text(description)
        except Exception as e:
            logger.warning(f"TeamsAdapter: jira_get_issue_dict({jira_key}) failed → {e}")
    return summary, description, issue_type, priority, repo


def _propose_sdlc_bug(jira_key: str, command: str, conv_id: str, service_url: str,
                      user_id: str, reply_to_id: str):
    """
    Analyse the bug and send an Adaptive Card proposal to Teams.
    The SDLC pipeline does NOT start until the user clicks Approve.
    """
    from integrations.teams_client import send_message, send_adaptive_card, build_hitl_card
    try:
        bind_context(correlation_id="", pipeline_stage="sdlc_teams_bug_propose")
        summary, description, issue_type, priority, repo = _fetch_jira_context(
            jira_key, command, "Bug"
        )

        # Quick triage analysis via LLM
        triage_prompt = (
            f"You are AiNxt, an engineering AI. Analyse this bug report and propose a fix approach.\n\n"
            f"Summary: {summary}\n\nDescription:\n{description[:2000]}\n\n"
            f"Respond with:\n## Root Cause\n## Proposed Fix\n## Impact\n## Priority\n\n"
            f"Be concise and reference likely file paths or components."
        )
        try:
            from models.model_router import model_router
            analysis = model_router.generate(triage_prompt, model_hint="complex")
        except Exception as _ae:
            logger.warning(f"TeamsAdapter: triage LLM failed → {_ae}")
            analysis = f"Auto-triage unavailable. Summary: {summary}"

        from store.sdlc_store import create_run, update_run_state as _urs
        run = create_run(
            run_type="bug",
            jira_key=jira_key or "TEAMS-BUG",
            jira_summary=summary,
            repo=repo,
            triggered_by=f"teams:{user_id}",
        )
        run_id = run["id"]

        issue_dict = {
            "key":         jira_key or f"TEAMS-{run_id[:6].upper()}",
            "summary":     summary,
            "description": description + "\n\n" + analysis,
            "issue_type":  issue_type,
            "priority":    priority,
            "repo":        repo,
            "assignee":    user_id,
            "_run_id":     run_id,
        }

        # Store pending data in the workflow KV — keyed by run_id, type=bug
        try:
            _rc = _get_workflow_kv()
            if _rc is None:
                raise KVError("workflow KV unavailable")
            _rc.set(
                f"teams:hitl_pending:{run_id}",
                json.dumps({
                    "issue_dict":  issue_dict,
                    "run_type":    "bug",
                    "conv_id":     conv_id,
                    "service_url": service_url,
                }),
                ex=604800,  # 7 days
            )
        except Exception as _re:
            logger.warning(f"TeamsAdapter: Redis pending store failed → {_re}")

        # Mark run as PENDING_APPROVAL (not CREATED — pipeline hasn't started)
        try:
            _urs(run_id, "PENDING_APPROVAL", context_patch={
                "teams_conv_id":     conv_id,
                "teams_service_url": service_url,
            })
        except Exception:
            pass

        # Send triage text first, then approval card
        send_message(
            service_url, conv_id,
            f"**Bug Analysis — `{jira_key or summary[:60]}`**\n\n{analysis[:1500]}",
            reply_to_id,
        )
        card = build_hitl_card(run_id, "PENDING_APPROVAL", summary)
        send_adaptive_card(service_url, conv_id, card)
        teams_metrics.inc_agent_run()

    except Exception as e:
        teams_metrics.inc_failure()
        logger.error(f"TeamsAdapter: propose bug failed → {e}")
        send_message(service_url, conv_id, f"❌ Analysis error: {str(e)[:300]}", reply_to_id)


def _propose_sdlc_feature(jira_key: str, command: str, conv_id: str, service_url: str,
                          user_id: str, reply_to_id: str):
    """
    Analyse the feature request and send an Adaptive Card proposal to Teams.
    The SDLC pipeline does NOT start until the user clicks Approve.
    """
    from integrations.teams_client import send_message, send_adaptive_card, build_hitl_card
    try:
        bind_context(correlation_id="", pipeline_stage="sdlc_teams_feature_propose")
        summary, description, issue_type, priority, repo = _fetch_jira_context(
            jira_key, command, "Story"
        )

        triage_prompt = (
            f"You are AiNxt, an engineering AI. Analyse this feature request and propose an implementation plan.\n\n"
            f"Summary: {summary}\n\nDescription:\n{description[:2000]}\n\n"
            f"Respond with:\n## Implementation Plan\n## Files to Modify\n## Risks\n## Effort Estimate\n\n"
            f"Be concise."
        )
        try:
            from models.model_router import model_router
            analysis = model_router.generate(triage_prompt, model_hint="complex")
        except Exception as _ae:
            logger.warning(f"TeamsAdapter: triage LLM failed → {_ae}")
            analysis = f"Auto-analysis unavailable. Summary: {summary}"

        from store.sdlc_store import create_run, update_run_state as _urs
        run = create_run(
            run_type="feature",
            jira_key=jira_key or "TEAMS-FEAT",
            jira_summary=summary,
            repo=repo,
            triggered_by=f"teams:{user_id}",
        )
        run_id = run["id"]

        issue_dict = {
            "key":         jira_key or f"TEAMS-{run_id[:6].upper()}",
            "summary":     summary,
            "description": description + "\n\n" + analysis,
            "issue_type":  issue_type,
            "priority":    priority,
            "repo":        repo,
            "assignee":    user_id,
            "_run_id":     run_id,
        }

        try:
            _rc = _get_workflow_kv()
            if _rc is None:
                raise KVError("workflow KV unavailable")
            _rc.set(
                f"teams:hitl_pending:{run_id}",
                json.dumps({
                    "issue_dict":  issue_dict,
                    "run_type":    "feature",
                    "conv_id":     conv_id,
                    "service_url": service_url,
                }),
                ex=604800,
            )
        except Exception as _re:
            logger.warning(f"TeamsAdapter: workflow KV pending store failed → {_re}")

        try:
            _urs(run_id, "PENDING_APPROVAL", context_patch={
                "teams_conv_id":     conv_id,
                "teams_service_url": service_url,
            })
        except Exception:
            pass

        send_message(
            service_url, conv_id,
            f"**Feature Analysis — `{jira_key or summary[:60]}`**\n\n{analysis[:1500]}",
            reply_to_id,
        )
        card = build_hitl_card(run_id, "PENDING_APPROVAL", summary)
        send_adaptive_card(service_url, conv_id, card)
        teams_metrics.inc_agent_run()

    except Exception as e:
        teams_metrics.inc_failure()
        logger.error(f"TeamsAdapter: propose feature failed → {e}")
        send_message(service_url, conv_id, f"❌ Analysis error: {str(e)[:300]}", reply_to_id)


def _run_hr_my_leaves(command: str, conv_id: str, service_url: str,
                      user_id: str, reply_to_id: str):
    """Fetch and display all leave records for the employee."""
    from integrations.teams_client import send_message
    try:
        from integrations.zoho_people import get_pending_leaves, DEFAULT_EMPLOYEE_ID
        leaves = get_pending_leaves(DEFAULT_EMPLOYEE_ID)

        if not leaves:
            send_message(service_url, conv_id, "📋 No leave records found.", reply_to_id)
            return

        lines = [f"📋 **Your Leaves ({len(leaves)} records):**\n"]
        for i, lv in enumerate(leaves, 1):
            lines.append(
                f"**{i}. {lv['type']}** — {lv['from']} to {lv['to']} "
                f"({lv['days']} day(s)) | Status: {lv['status']} | ID: `{lv['record_id']}`"
            )

        lines.append("\n_To cancel: `@AiNxt cancel leave <record_id>`_")
        teams_metrics.inc_success()
        send_message(service_url, conv_id, "\n".join(lines), reply_to_id)

    except Exception as e:
        teams_metrics.inc_failure()
        logger.error(f"TeamsAdapter: my leaves failed → {e}")
        send_message(service_url, conv_id, f"❌ Could not fetch leaves: {str(e)[:200]}", reply_to_id)


def _run_hr_cancel_leave(command: str, conv_id: str, service_url: str,
                         user_id: str, reply_to_id: str):
    """Cancel a leave by record ID extracted from the command."""
    from integrations.teams_client import send_message
    import re as _re
    try:
        # Extract record ID — long numeric ID from command
        m = _re.search(r"\b(3300\d{12,}|\d{15,})\b", command)
        if not m:
            send_message(
                service_url, conv_id,
                "Please provide the leave record ID.\n"
                "Run `@AiNxt my leaves` to see your leave IDs, then:\n"
                "`@AiNxt cancel leave 330071000000XXXXXX`",
                reply_to_id,
            )
            return

        record_id = m.group(1)
        from integrations.zoho_people import cancel_leave
        result = cancel_leave(record_id, reason=f"Cancelled via AiNxt Teams bot by {user_id}")

        if result.get("status") == "success":
            teams_metrics.inc_success()
            send_message(
                service_url, conv_id,
                f"✅ Leave `{record_id}` has been **cancelled** successfully.",
                reply_to_id,
            )
        else:
            send_message(service_url, conv_id, f"⚠️ Zoho response: {result}", reply_to_id)

    except Exception as e:
        teams_metrics.inc_failure()
        logger.error(f"TeamsAdapter: cancel leave failed → {e}")
        send_message(service_url, conv_id, f"❌ Could not cancel leave: {str(e)[:200]}", reply_to_id)


def _run_hr_leave_balance(command: str, conv_id: str, service_url: str,
                         user_id: str, reply_to_id: str):
    """Fetch leave balance from Zoho People and reply in Teams."""
    from integrations.teams_client import send_message
    try:
        from integrations.zoho_people import get_leave_types, DEFAULT_EMPLOYEE_ID
        types = get_leave_types(DEFAULT_EMPLOYEE_ID)

        if not types:
            send_message(service_url, conv_id, "No leave types found in Zoho People.", reply_to_id)
            return

        lines = ["📊 **Your Leave Balance:**\n"]
        for lt in types:
            name    = lt.get("Name", "")
            balance = lt.get("BalanceCount", "0")
            availed = lt.get("AvailedCount", 0)
            unit    = lt.get("Unit", "Days")
            lines.append(f"• **{name}**: {balance} {unit} available _(used: {availed})_")

        teams_metrics.inc_agent_run()
        teams_metrics.inc_success()
        send_message(service_url, conv_id, "\n".join(lines), reply_to_id)

    except Exception as e:
        teams_metrics.inc_failure()
        logger.error(f"TeamsAdapter: leave balance failed → {e}")
        send_message(service_url, conv_id, f"❌ Could not fetch leave balance: {str(e)[:200]}", reply_to_id)


def _run_hr_leave(command: str, conv_id: str, service_url: str,
                  user_id: str, reply_to_id: str):
    """Parse natural language leave request and apply via Zoho People."""
    from integrations.teams_client import send_message
    try:
        from datetime import datetime, timedelta
        import re as _re

        today    = datetime.today()
        tomorrow = today + timedelta(days=1)

        _MONTH_ABBR = {
            1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr",
            5: "May", 6: "Jun", 7: "Jul", 8: "Aug",
            9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
        }

        def _zoho_fmt(dt):
            return f"{dt.day:02d}-{_MONTH_ABBR[dt.month]}-{dt.year}"

        # Parse date from command
        cmd_lower = command.lower()
        if "tomorrow" in cmd_lower:
            from_dt = tomorrow
        elif "today" in cmd_lower:
            from_dt = today
        else:
            from_dt = None
            # ISO: 2026-03-16
            m = _re.search(r"\b(\d{4}-\d{2}-\d{2})\b", command)
            if m:
                from_dt = datetime.strptime(m.group(1), "%Y-%m-%d")
            # DD-Mon-YYYY: 16-Mar-2026
            if not from_dt:
                m = _re.search(r"\b(\d{1,2}-[A-Za-z]{3}-\d{4})\b", command)
                if m:
                    from_dt = datetime.strptime(m.group(1), "%d-%b-%Y")
            # DD/MM/YYYY or DD-MM-YYYY
            if not from_dt:
                m = _re.search(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b", command)
                if m:
                    from_dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            if not from_dt:
                from_dt = tomorrow  # default to tomorrow

        to_dt       = from_dt
        zoho_from   = _zoho_fmt(from_dt)
        zoho_to     = _zoho_fmt(to_dt)
        employee_id = "1"  # Default; in production resolve from user_id

        from integrations.zoho_people import apply_leave as _apply
        result = _apply(
            employee_id=employee_id,
            from_date=zoho_from,
            to_date=zoho_to,
            reason="Leave request via AiNxt Teams bot",
        )

        teams_metrics.inc_agent_run()
        teams_metrics.inc_success()
        send_message(
            service_url, conv_id,
            f"✅ Your leave for **{zoho_from}** has been applied in Zoho People.",
            reply_to_id,
        )
    except Exception as e:
        teams_metrics.inc_failure()
        logger.error(f"TeamsAdapter: HR leave failed → {e}")
        send_message(
            service_url, conv_id,
            f"❌ Could not apply leave: {str(e)[:300]}",
            reply_to_id,
        )


def _run_orchestrator(command: str, conv_id: str, service_url: str,
                      user_id: str, reply_to_id: str):
    from integrations.teams_client import send_message
    t0 = time.time()
    try:
        from models.model_router import model_router
        answer = model_router.generate(command, model_hint="complex")

        teams_metrics.inc_agent_run()
        teams_metrics.inc_success()
        teams_metrics.record_latency((time.time() - t0) * 1000)

        send_message(service_url, conv_id, answer or "No response.", reply_to_id)
    except Exception as e:
        teams_metrics.inc_failure()
        logger.error(f"TeamsAdapter: orchestrator run failed → {e}")
        send_message(
            service_url, conv_id,
            f"❌ Error: {str(e)[:300]}",
            reply_to_id,
        )


# ── Main dispatch ─────────────────────────────────────────────────────────────

def dispatch_activity(activity: dict) -> str:
    """
    Process a Bot Framework Activity dict.

    Returns an acknowledgement string (logged, not sent to caller directly).
    Actual replies go back via Teams client in background threads.
    """
    teams_metrics.inc_request()

    activity_type = activity.get("type", "")
    conv          = activity.get("conversation") or {}
    conv_id       = conv.get("id", "")
    service_url   = activity.get("serviceUrl", "")
    from_user     = activity.get("from") or {}
    user_id       = from_user.get("id", "unknown")
    user_name     = from_user.get("name", user_id)
    reply_to_id   = activity.get("id")

    # Cache service URL for proactive messages
    if service_url and conv_id:
        store_service_url(conv_id, service_url)

    # ── Adaptive Card submit (HITL button click) ──────────────────────────────
    if activity_type in ("invoke", "message"):
        value = activity.get("value") or {}
        _action_key = os.getenv("TEAMS_ACTION_KEY", "ainxt_action")
        hitl_action = value.get(_action_key, "")

        if hitl_action in ("hitl_approve", "hitl_reject"):
            run_id = value.get("run_id", "")
            _handle_hitl_action(
                hitl_action, run_id, user_name,
                conv_id, service_url, reply_to_id,
            )
            return "hitl_handled"

    # ── Regular message ───────────────────────────────────────────────────────
    if activity_type != "message":
        return "ignored"

    text = activity.get("text", "").strip()
    if not text:
        return "empty_message"

    if not is_ainxt_mention(activity):
        return "not_mentioned"

    command = extract_command(text)
    if not command:
        return "empty_command"

    logger.info(
        f"TeamsAdapter: @AiNxt from {user_name} ({user_id}) "
        f"conv={conv_id[:12]}: {command[:100]}"
    )

    # Compliance check
    check = _compliance_check(command)
    if check.get("blocked"):
        from integrations.teams_client import send_message as _send
        blocked_types = [f["type"] for f in check.get("findings", []) if f.get("blocked")]
        msg = (
            "⛔ Compliance screening is unavailable — your request was refused rather than "
            "processed unscanned. Please contact your administrator."
            if check.get("unavailable")
            else f"⛔ Request blocked by compliance: `{', '.join(blocked_types)}`"
        )
        _send(service_url, conv_id, msg, reply_to_id)
        teams_metrics.inc_failure()
        return "compliance_blocked"

    # Use the ENGINE's redaction from here on. `command` is routed to the orchestrator and reaches
    # an LLM, so reading only `blocked` and forwarding the original meant every action="redact"
    # type (Aadhaar, UPI, IFSC, account numbers, secrets) went to the provider in the clear —
    # the same defect fixed on the chat and CLI paths.
    command = check.get("redacted_text") or command

    # Budget check
    budget = _budget_check(user_id)
    if not budget.get("allowed", True):
        from integrations.teams_client import send_message as _send
        _send(
            service_url, conv_id,
            f"⛔ Budget limit reached: {budget.get('reason', '')}",
            reply_to_id,
        )
        teams_metrics.inc_failure()
        return "budget_exceeded"

    # ── Acknowledge immediately before running ────────────────────────────────
    from integrations.teams_client import send_message as _send
    routing = _route_command(command)

    ack_msg = {
        "sdlc_bug":          f"🔍 Analysing `{routing['jira_key'] or command[:40]}`… I'll propose a solution for your approval before touching any code.",
        "sdlc_feature":      f"🔍 Analysing feature `{routing['jira_key'] or command[:40]}`… I'll propose an implementation plan for your approval.",
        "pr_review":         f"⚙️ Running **PR Review** for PR #{routing['pr_num']}…",
        "hr_leave":          f"🗓️ Applying your leave in Zoho People…",
        "hr_leave_balance":  f"📊 Fetching your leave balance from Zoho People…",
        "hr_my_leaves":      f"📋 Fetching your leave records from Zoho People…",
        "hr_cancel_leave":   f"🗑️ Cancelling your leave in Zoho People…",
        "orchestrator":      f"🤖 Thinking…",
    }.get(routing["route"], "🤖 Processing…")
    _send(service_url, conv_id, ack_msg, reply_to_id)

    # Route to appropriate handler in background thread
    # SDLC routes: propose solution first, require approval before pipeline starts
    if routing["route"] == "sdlc_bug":
        t = threading.Thread(
            target=_propose_sdlc_bug,
            args=(routing["jira_key"], command, conv_id, service_url, user_id, reply_to_id),
            daemon=True,
        )
    elif routing["route"] == "sdlc_feature":
        t = threading.Thread(
            target=_propose_sdlc_feature,
            args=(routing["jira_key"], command, conv_id, service_url, user_id, reply_to_id),
            daemon=True,
        )
    elif routing["route"] == "hr_leave_balance":
        t = threading.Thread(
            target=_run_hr_leave_balance,
            args=(command, conv_id, service_url, user_id, reply_to_id),
            daemon=True,
        )
    elif routing["route"] == "hr_leave":
        t = threading.Thread(
            target=_run_hr_leave,
            args=(command, conv_id, service_url, user_id, reply_to_id),
            daemon=True,
        )
    elif routing["route"] == "hr_my_leaves":
        t = threading.Thread(
            target=_run_hr_my_leaves,
            args=(command, conv_id, service_url, user_id, reply_to_id),
            daemon=True,
        )
    elif routing["route"] == "hr_cancel_leave":
        t = threading.Thread(
            target=_run_hr_cancel_leave,
            args=(command, conv_id, service_url, user_id, reply_to_id),
            daemon=True,
        )
    else:
        # orchestrator / pr_review → use model_router directly
        t = threading.Thread(
            target=_run_orchestrator,
            args=(command, conv_id, service_url, user_id, reply_to_id),
            daemon=True,
        )

    t.start()
    return "dispatched"


# ── HITL handler ──────────────────────────────────────────────────────────────

def _handle_hitl_action(action: str, run_id: str, user: str,
                        conv_id: str, service_url: str, reply_to_id: str):
    from integrations.teams_client import send_message as _send
    logger.info(f"TeamsAdapter: HITL {action} run={run_id} by={user}")

    try:
        from store.sdlc_store import get_run, update_run_state, add_run_event
        run = get_run(run_id)
        if not run:
            _send(service_url, conv_id, f"Run `{run_id[:8]}` not found.", reply_to_id)
            return

        state    = run["state"]
        _allowed = {
            "PENDING_APPROVAL",
            "AWAITING_DESIGN_APPROVAL",
            "AWAITING_SOLUTION_APPROVAL",
            "AWAITING_PR_APPROVAL",
        }
        if state not in _allowed:
            _send(service_url, conv_id,
                  f"Run `{run_id[:8]}` is in `{state}` — approval not applicable.",
                  reply_to_id)
            return

        if action == "hitl_approve":
            add_run_event(run_id, state, "APPROVED", actor=user,
                          output=f"Approved via Teams by {user}")
            update_run_state(run_id, "APPROVED",
                             context_patch={"approved_by": user, "approved_via": "teams"})

            if state == "PENDING_APPROVAL":
                # Initial approval gate — retrieve pending issue_dict and start the pipeline
                pending = None
                try:
                    _rc = _get_workflow_kv()
                    if _rc is not None:
                        raw = _rc.get(f"teams:hitl_pending:{run_id}")
                        if raw:
                            pending = json.loads(raw)
                            _rc.delete(f"teams:hitl_pending:{run_id}")
                except Exception as _re:
                    logger.warning(f"TeamsAdapter: workflow KV pending fetch failed → {_re}")

                if pending:
                    _issue_dict = pending["issue_dict"]
                    _run_type   = pending.get("run_type", "bug")

                    def _start_pipeline():
                        try:
                            bind_context(correlation_id=run_id, pipeline_stage="sdlc_teams")
                            update_run_state(run_id, "CREATED")
                            if _run_type == "feature":
                                from agents.sdlc_pipeline import run_feature_pipeline
                                run_feature_pipeline(_issue_dict, run_id)
                            else:
                                from agents.sdlc_pipeline import run_bug_pipeline
                                run_bug_pipeline(_issue_dict, run_id)
                        except Exception as _exc:
                            logger.error(f"TeamsAdapter: pipeline start failed run={run_id} → {_exc}")
                            update_run_state(run_id, "FAILED", error=str(_exc))

                    threading.Thread(target=_start_pipeline, daemon=True,
                                     name=f"sdlc-teams-{run_id[:8]}").start()
                    teams_metrics.inc_success()
                    _send(service_url, conv_id,
                          f"✅ Approved by **{user}**. SDLC pipeline started for `{run_id[:8]}`.\n"
                          f"Check the SDLC tab in AiNxt for progress.",
                          reply_to_id)
                else:
                    _send(service_url, conv_id,
                          f"⚠️ Approval recorded but pending run data not found for `{run_id[:8]}`.",
                          reply_to_id)
            else:
                # Mid-pipeline HITL gate — resume the already-running pipeline
                def _resume():
                    try:
                        bind_context(correlation_id=run_id, pipeline_stage="sdlc_teams_resume")
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
                        logger.error(f"TeamsAdapter: HITL resume failed → {e}")

                threading.Thread(target=_resume, daemon=True).start()
                _send(service_url, conv_id,
                      f"✅ Run `{run_id[:8]}` **approved** by {user}. Pipeline resuming…",
                      reply_to_id)

        else:  # hitl_reject
            reason = f"Rejected via Teams by {user}"
            add_run_event(run_id, state, "FAILED", actor=user, output=reason)
            update_run_state(run_id, "FAILED", error=reason,
                             context_patch={"rejected_by": user})
            _send(service_url, conv_id,
                  f"❌ Run `{run_id[:8]}` **rejected** by {user}.",
                  reply_to_id)

    except Exception as e:
        logger.error(f"TeamsAdapter: HITL action failed → {e}")
        _send(service_url, conv_id, f"Error processing HITL action: {e}", reply_to_id)
