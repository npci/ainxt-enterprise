# SPDX-License-Identifier: Apache-2.0
"""
ConnectorEngine — production-hardened universal connector runtime.

Execution pipeline (per tool call):
  1. Pydantic/schema validation
  2. Scope enforcement
  3. Cost guardrail (max_items)
  4. Cache check (Redis, bypass on freshness keywords)
  5. Idempotency key (write ops)
  6. Get + auto-refresh OAuth token
  7. Sync vs. async routing
  8. CustomAdapter | GenericHTTPAdapter
     - Retry (3× exponential backoff + jitter for 429/5xx)
     - Pagination loop (cursor/nextLink, max_pages=5)
     - Partial failure handling
  9. Response normalization → ConnectorResponse
  10. Compliance scan (before LLM context injection)
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
from typing import Any, Optional

import httpx

from connectors.base import (
    ConnectorAccessDeniedError,
    ConnectorContext,
    ConnectorNotConnectedError,
    ConnectorRateLimitError,
    ConnectorReauthRequired,
    ConnectorResponse,
    ConnectorScopeError,
    ConnectorTokenRejected,
    ConnectorTool,
    ConnectorTransientError,
)
from connectors.metrics import connector_metrics
from core.logger import logger

# ── Constants ─────────────────────────────────────────────────────────────────

# Max @odata.nextLink pages to follow. Raised so large mailboxes/chats page through
# fully (bounded together with TOOL_MAX_ITEMS, whichever is hit first).
MAX_PAGES = int(os.getenv("CONNECTOR_MAX_PAGES", "50"))
ASYNC_THRESHOLD_ITEMS = 200  # requests for more items than this → async queue
# Hard wall-clock timeout per connector call. NOTE: for M365 tools that do their
# OWN internal @odata.nextLink pagination inside a single adapter.execute() call
# (outlook_search_emails, outlook_count_emails, teams_get_channel_messages,
# teams_get_chat_messages, calendar_list_events, teams_list_chats, people_search —
# see AUTO_PAGE in connectors/adapters/microsoft365.py), this outer deadline is
# only checked BEFORE the engine starts a new page of its OWN pagination loop
# below — it does not interrupt the adapter mid-call. Those tools' real wall-clock
# ceiling is the adapter's own Microsoft365Adapter.PAGINATE_BUDGET_S (40s), which
# can legitimately exceed this value. Raised 10s -> 45s so this outer deadline is
# not misleadingly shorter than the adapter's own pagination budget for those tools.
MAX_CONNECTOR_EXECUTION_MS = 45_000
FRESHNESS_KEYWORDS = {"latest", "today", "right now", "current", "recent", "now", "just"}

# How long before expiry an access token is proactively refreshed.
# Deliberately wider than the old hard-coded 300s: a SCHEDULED task can sit in
# connector_queue for minutes between enqueue and execution, so a token that looked
# fresh at enqueue time could already be expired when the Graph call finally runs —
# which surfaced to the user as "please connect the connector".
_REFRESH_WINDOW_S = int(os.getenv("CONNECTOR_TOKEN_REFRESH_WINDOW_S", "900"))

# Connectors whose READ tool calls require explicit user permission before execution.
# The orchestrator checks this set before every connector_call step. If the user has
# stored always_allow=TRUE in user_connector_permissions the gate is skipped; if
# always_allow=FALSE the call is blocked; if no row exists the user is prompted.
# M365 write tools already go through the [SENDPROPOSAL]/[ACTIONPROPOSAL] path and
# are excluded from auto-planning, so they don't need to be listed here.
PERMISSION_GATED_CONNECTORS: frozenset = frozenset({
    "gitlab",
    "github",
    "jira_connector",
    "google_drive",
    "slack",
    "zoom",
})

# Per-tool result ceilings. Deliberately HIGH so reads come back WHOLE — the agent
# should see a full mailbox / chat history / calendar, not a truncated sample. These
# are safety ceilings against a runaway (e.g. 100k messages), not a curation limit;
# a caller can still pass a smaller `limit` when it genuinely wants fewer. Override
# any of these per-deployment via the env below.
_MAX_ITEMS_DEFAULT = int(os.getenv("CONNECTOR_MAX_ITEMS_DEFAULT", "1000"))
TOOL_MAX_ITEMS: dict[str, int] = {
    "outlook_search_emails":      int(os.getenv("CONNECTOR_MAX_ITEMS_MAIL", "2000")),
    "outlook_count_emails":       int(os.getenv("CONNECTOR_MAX_ITEMS_MAIL", "2000")),
    "outlook_list_folders":       500,
    "calendar_list_events":       int(os.getenv("CONNECTOR_MAX_ITEMS_CALENDAR", "1000")),
    "gmail_search_emails":        int(os.getenv("CONNECTOR_MAX_ITEMS_MAIL", "2000")),
    "gmail_count_emails":         int(os.getenv("CONNECTOR_MAX_ITEMS_MAIL", "2000")),
    "slack_search_messages":      2000,
    "slack_get_channel_messages": 2000,
    "teams_get_channel_messages": int(os.getenv("CONNECTOR_MAX_ITEMS_TEAMS", "2000")),
    "teams_get_chat_messages":    int(os.getenv("CONNECTOR_MAX_ITEMS_TEAMS", "2000")),
    "teams_list_my_teams":        500,
    "teams_list_channels":        500,
    "teams_list_chats":           1000,
    "people_search":              200,
}

# Tools whose PERSISTED (DB-seeded) max_items is known to be stale/undersized
# relative to the ceiling above — e.g. outlook_search_emails/outlook_count_emails
# were seeded with max_items=50 (matching Graph's page size) back when TOOL_MAX_ITEMS
# only allowed 50 too. TOOL_MAX_ITEMS was later raised to let full mailboxes/chats/
# channels page through, but the seeded per-tool value in connectors/seed.py was
# never bumped to match, so _get_tool() kept honouring the stale smaller value and
# pagination stopped after the very first Graph page even though @odata.nextLink
# was still present:
#   - outlook_search_emails / outlook_count_emails: seeded 50, ceiling 2000
#     (email retrieval/summarization silently capped at 50 messages).
#   - teams_get_channel_messages: seeded 100, ceiling 2000 (channel summarization
#     silently capped at 100 messages).
#   - teams_get_chat_messages: seeded 50, ceiling 2000 (1:1/group chat
#     summarization silently capped at 50 messages).
# teams_list_chats is NOT listed here — its seeded max_items (1000) already
# matches TOOL_MAX_ITEMS, so there's nothing stale to override.
#
# _get_tool() takes max(seeded_value, TOOL_MAX_ITEMS[tool_name]) ONLY for tools
# listed here — every other tool keeps its exact seeded ceiling unchanged, so this
# does not widen limits for GitLab/Jira/Slack/calendar/org-lookup tools or any
# other Teams tool not named below.
_STALE_SEEDED_MAX_ITEMS_TOOLS: frozenset = frozenset({
    "outlook_search_emails",
    "outlook_count_emails",
    "teams_get_channel_messages",
    "teams_get_chat_messages",
})

# Per-connector default rate limits (requests/min per user).
# The engine is synchronous so OS-level asyncio.Semaphore is not used;
# concurrency control is enforced via Redis counters in _check_rate_limit().
CONNECTOR_SEMAPHORES: dict[str, int] = {
    "microsoft_365": 10,
    "gmail":         10,
    "slack":         20,
    "default":       15,
}

RATE_LIMIT_PER_MIN: dict[str, int] = {
    "microsoft_365": 60,
    "gmail": 60,
    "slack": 100,
}


class ConnectorEngine:
    """
    Central execution engine for all connector tool calls.
    Thread-safe and synchronous (compatible with RQ workers + FastAPI sync endpoints).
    """

    def __init__(self):
        self._redis = None
        self._adapters: dict[str, Any] = {}
        self._pydantic_models: dict[str, Any] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def execute(
        self,
        connector_name: str,
        tool_name: str,
        params: dict,
        user_id: str,
        query_text: str = "",
    ) -> ConnectorResponse:
        """
        Execute a connector tool.
        Returns ConnectorResponse — never raises (errors captured in .error field).
        """
        start_ms = int(time.time() * 1000)
        cache_hit = False
        # Fetch user dept once for access control + observability (best-effort)
        _user_level, _user_dept = self._get_user_level_dept(user_id)

        try:
            # Load connector definition from DB
            defn = self._load_definition(connector_name)
            tool = self._get_tool(defn, tool_name)

            # 0. Access control (AD level + department policy)
            self._check_access_policy(user_id, defn)

            # 1. Schema validation
            params = self._validate_params(tool, params)

            # 2. Scope enforcement + per-user rate limit
            token_row = self._get_token_row(user_id, connector_name, defn)
            self._enforce_scopes(tool, token_row)
            self._check_rate_limit(
                user_id, connector_name,
                defn.get("rate_limit_per_min") or RATE_LIMIT_PER_MIN.get(connector_name, 100)
            )

            # 3. Cost guardrail (only meaningful for paginated tools — see
            # _apply_cost_guardrail docstring for why non-paginated tools must
            # never have a synthetic "limit" injected into their params)
            params = self._apply_cost_guardrail(tool_name, tool.paginated, params)
            truncated = params.pop("__truncated__", False)

            # 4. Cache check
            bypass_cache = self._should_bypass_cache(query_text, params)
            if not bypass_cache and tool.cache_ttl_s > 0:
                cache_key = self._cache_key(connector_name, tool_name, params, user_id)
                cached = self._cache_get(cache_key)
                if cached is not None:
                    logger.info(f"ConnectorEngine: cache hit for {connector_name}.{tool_name}")
                    # `cached` came from ConnectorResponse.to_dict(), so it ALREADY
                    # has truncated/source/tool — passing them again caused
                    # "got multiple values for keyword argument 'truncated'".
                    cached["truncated"] = truncated
                    cached["source"] = connector_name
                    cached["tool"] = tool_name
                    connector_metrics.record_call(
                        connector_name, tool_name,
                        int(time.time() * 1000) - start_ms, True, True, user_id, dept=_user_dept
                    )
                    return ConnectorResponse(**cached)

            # 5. Idempotency key for write operations
            idempotency_key = ""
            if tool.is_write:
                idempotency_key = self._idempotency_key(user_id, connector_name, tool_name, params)

            # 6. Get valid OAuth token (auto-refresh)
            context = self._get_context(user_id, connector_name, token_row, defn)
            if idempotency_key:
                context.metadata["Idempotency-Key"] = idempotency_key

            # 7. Execute via adapter (with retry + pagination + timeout)
            adapter = self._get_adapter(connector_name, defn)
            deadline_ms = int(time.time() * 1000) + MAX_CONNECTOR_EXECUTION_MS
            items, partial, timed_out = self._execute_with_pagination(
                adapter, tool, params, context, deadline_ms=deadline_ms
            )

            # Compliance check BEFORE returning (Check 1 — before LLM)
            compliance_blocked = self._compliance_check(items)
            if compliance_blocked:
                return ConnectorResponse(
                    success=False,
                    items=[],
                    count=0,
                    source=connector_name,
                    tool=tool_name,
                    error="Connector response blocked: contains sensitive data (PCI/PII)",
                    latency_ms=int(time.time() * 1000) - start_ms,
                )

            # 8a. Data minimization — strip fields not in response_fields whitelist
            items = self._minimize_response(items, tool)

            response = ConnectorResponse(
                success=True,
                items=items,
                count=len(items),
                source=connector_name,
                tool=tool_name,
                partial=partial,
                truncated=truncated,
                timed_out=timed_out,
                latency_ms=int(time.time() * 1000) - start_ms,
            )

            # Cache the result
            if not bypass_cache and tool.cache_ttl_s > 0 and not partial:
                self._cache_set(cache_key, response.to_dict(), tool.cache_ttl_s)

            connector_metrics.record_call(
                connector_name, tool_name,
                response.latency_ms, True, cache_hit, user_id, dept=_user_dept
            )
            return response

        except ConnectorAccessDeniedError as e:
            return ConnectorResponse(
                success=False, items=[], count=0,
                source=connector_name, tool=tool_name,
                error=f"ACCESS_DENIED: {e}",
                latency_ms=int(time.time() * 1000) - start_ms,
            )
        except ConnectorNotConnectedError as e:
            # For PAT connectors (gitlab, jira_connector): attempt auto-connect from
            # the profile vault (user_tokens table) before surfacing the error.
            # This handles the common case where the user stored their PAT in
            # Profile → API Token Vault but never explicitly clicked "Connect" in
            # Settings → Connectors, so ainxt.user_oauth_tokens has no row yet.
            _defn_for_retry = None
            try:
                _defn_for_retry = self._load_definition(connector_name)
            except Exception:
                pass
            if _defn_for_retry and _defn_for_retry.get("auth_type") == "pat":
                _auto_ok = self._try_auto_connect_pat(user_id, connector_name)
                if _auto_ok:
                    # Retry once — the token row now exists in user_oauth_tokens
                    return self.execute(connector_name, tool_name, params, user_id, query_text)
            return ConnectorResponse(
                success=False, items=[], count=0,
                source=connector_name, tool=tool_name,
                error=str(e), latency_ms=int(time.time() * 1000) - start_ms,
            )
        except ConnectorTransientError as e:
            # Temporary DB/availability blip — NOT a disconnect. Surface a distinct
            # TRANSIENT_ERROR marker so the UI/agent asks the user to RETRY rather
            # than showing a misleading "connect again" prompt. Do NOT deactivate
            # the token.
            logger.warning(f"ConnectorEngine: {connector_name}.{tool_name} transient — {e}")
            return ConnectorResponse(
                success=False, items=[], count=0,
                source=connector_name, tool=tool_name,
                error=f"TRANSIENT_ERROR: {e}",
                latency_ms=int(time.time() * 1000) - start_ms,
            )
        except ConnectorReauthRequired as e:
            # Deactivate the stored token so UI shows "reconnect needed"
            self._deactivate_token(user_id, connector_name)
            return ConnectorResponse(
                success=False, items=[], count=0,
                source=connector_name, tool=tool_name,
                error=f"REAUTH_REQUIRED: {e}",
                latency_ms=int(time.time() * 1000) - start_ms,
            )
        except ConnectorScopeError as e:
            return ConnectorResponse(
                success=False, items=[], count=0,
                source=connector_name, tool=tool_name,
                error=f"SCOPE_ERROR: {e}",
                latency_ms=int(time.time() * 1000) - start_ms,
            )
        except ConnectorRateLimitError as e:
            return ConnectorResponse(
                success=False, items=[], count=0,
                source=connector_name, tool=tool_name,
                error=f"RATE_LIMIT: {e}",
                latency_ms=int(time.time() * 1000) - start_ms,
            )
        except Exception as e:
            logger.error(f"ConnectorEngine: {connector_name}.{tool_name} failed — {e}")
            connector_metrics.record_call(
                connector_name, tool_name,
                int(time.time() * 1000) - start_ms, False, False, user_id,
                dept=_user_dept, error_type=type(e).__name__,
            )
            return ConnectorResponse(
                success=False, items=[], count=0,
                source=connector_name, tool=tool_name,
                error=str(e),
                latency_ms=int(time.time() * 1000) - start_ms,
            )

    # ── Step 1: Schema validation ──────────────────────────────────────────────

    def _validate_params(self, tool: ConnectorTool, params: dict) -> dict:
        """Validate params against tool input_schema. Strips unknown keys.

        Internal keys (prefixed with '_') injected by mcp_bridge — such as
        _attachments, _attachment, _attachment_retry — are always preserved
        so they survive into the adapter's _build_write_body / _collect_attachments.
        """
        schema = tool.input_schema
        if not schema:
            return params

        required = schema.get("required", [])
        properties = schema.get("properties", {})
        validated = {}

        for key, prop in properties.items():
            if key in params:
                val = params[key]
                # Type coercion
                expected_type = prop.get("type")
                try:
                    if expected_type == "integer" and not isinstance(val, int):
                        val = int(val)
                    elif expected_type == "number" and not isinstance(val, (int, float)):
                        val = float(val)
                    elif expected_type == "boolean" and not isinstance(val, bool):
                        val = str(val).lower() in ("true", "1", "yes")
                    elif expected_type == "string":
                        val = str(val)
                except (ValueError, TypeError):
                    pass
                validated[key] = val
            elif key in required:
                raise ValueError(f"Required parameter missing: {key!r}")

        # Preserve internal bridge keys (e.g. _attachments, _attachment,
        # _attachment_retry) that mcp_bridge injects AFTER schema validation
        # would normally strip them. Without this, _attachments is silently
        # dropped and _collect_attachments sees NoneType -> no attachment sent.
        for key, val in params.items():
            if key.startswith('_') and key not in validated:
                validated[key] = val

        return validated

    # ── Step 2: Scope enforcement ──────────────────────────────────────────────

    def _enforce_scopes(self, tool: ConnectorTool, token_row: dict) -> None:
        if not tool.requires_scopes:
            return
        granted = set(token_row.get("scopes") or [])
        required = set(tool.requires_scopes)
        missing = required - granted
        if missing:
            raise ConnectorScopeError(
                f"Token lacks required scopes for {tool.name}: {missing}. "
                "Re-connect the integration to grant additional permissions."
            )

    # ── Step 3: Cost guardrail ────────────────────────────────────────────────

    def _apply_cost_guardrail(self, tool_name: str, paginated: bool, params: dict) -> dict:
        """Cap `limit` at a hard per-tool ceiling — but ONLY for paginated tools.

        A non-paginated tool (e.g. gitlab_create_branch, gitlab_merge_mr) has no
        concept of "limit" at all. Unconditionally injecting one here used to
        crash every such tool whose underlying function has a fixed signature
        with no **kwargs catch-all — e.g.
            gitlab_create_branch() got an unexpected keyword argument 'limit'
        because the injected key survived adapter param normalisation and was
        passed straight through to `fn(**call_params)`. Gating on `paginated`
        fixes it at the source for every connector/adapter, not just GitLab.
        """
        if not paginated:
            return params
        hard_max = TOOL_MAX_ITEMS.get(tool_name, _MAX_ITEMS_DEFAULT)
        requested = params.get("limit", 0)
        if requested == 0 or requested > hard_max:
            params = dict(params)
            params["limit"] = hard_max
            if requested > hard_max:
                params["__truncated__"] = True
        return params

    # ── Step 4: Cache ─────────────────────────────────────────────────────────

    def _should_bypass_cache(self, query_text: str, params: dict) -> bool:
        if not query_text:
            return False
        query_lower = query_text.lower()
        return any(kw in query_lower for kw in FRESHNESS_KEYWORDS)

    def _cache_key(self, connector: str, tool: str, params: dict, user_id: str) -> str:
        content = f"{connector}:{tool}:{user_id}:{json.dumps(params, sort_keys=True)}"
        return "connector:cache:" + hashlib.sha256(content.encode()).hexdigest()[:32]

    def _cache_get(self, key: str) -> Optional[dict]:
        try:
            r = self._get_redis()
            if not r:
                return None
            raw = r.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def _cache_set(self, key: str, data: dict, ttl_s: int) -> None:
        try:
            r = self._get_redis()
            if r:
                r.set(key, json.dumps(data), ex=ttl_s)
        except Exception:
            pass

    # ── Step 5: Idempotency ───────────────────────────────────────────────────

    def _idempotency_key(self, user_id: str, connector: str, tool: str, params: dict) -> str:
        # Bucket by minute so retries within 60s reuse the same key
        minute_bucket = int(time.time()) // 60
        content = f"{user_id}:{connector}:{tool}:{json.dumps(params, sort_keys=True)}:{minute_bucket}"
        return hashlib.sha256(content.encode()).hexdigest()[:32]

    # ── Step 6: Token management ──────────────────────────────────────────────

    def _get_token_row(self, user_id: str, connector_name: str, defn: dict = None) -> dict:
        """Fetch the auth row from DB. For OAuth connectors, auto-refresh if
        expiring. For DPI connectors (auth_type='dpi_consent'), the row holds a
        signed CONSENT ARTIFACT — verify it (expiry/signature) instead of
        refreshing. defn is the connector definition (carries auth_type)."""
        # A TRANSIENT DB error (pool exhaustion, dropped/timed-out connection) must
        # NOT be reported as "not connected" — that produced the intermittent
        # "please connect again" that then succeeds on retry. Retry the read a couple
        # of times with a tiny backoff before giving up, and only then surface the
        # error as a transient DB fault (NOT a disconnect).
        row = None
        last_err = None
        for _attempt in range(3):
            try:
                from db.database import SessionLocal
                db = SessionLocal()
                try:
                    row = db.execute(
                        __import__("sqlalchemy").text(
                            "SELECT access_token, refresh_token, expires_at, scopes, metadata, is_active "
                            "FROM ainxt.user_oauth_tokens WHERE user_id = :uid AND connector_name = :cn"
                        ),
                        {"uid": user_id, "cn": connector_name},
                    ).fetchone()
                finally:
                    db.close()
                last_err = None
                break
            except Exception as e:
                last_err = e
                logger.warning(
                    f"ConnectorEngine._get_token_row: DB read failed for {connector_name} "
                    f"(attempt {_attempt + 1}/3) — {type(e).__name__}: {e}"
                )
                import time as _t
                _t.sleep(0.15 * (_attempt + 1))
        if last_err is not None:
            # All retries exhausted — this is a DB availability problem, not a missing
            # connection. Surface it as such so the user is told to RETRY, not reconnect.
            logger.error(f"ConnectorEngine._get_token_row: DB error after retries — {last_err}")
            raise ConnectorTransientError(
                f"{connector_name}: could not read the connection right now (temporary "
                f"database issue). Please try again in a moment — you do NOT need to reconnect."
            )

        if not row or not row[5]:  # is_active check
            # Distinguish the two very different causes so an intermittent
            # "connector is not connected" can be diagnosed from the logs instead of
            # guessing: NO ROW (never connected / wrong user_id) vs ROW PRESENT BUT
            # is_active=FALSE (token was deactivated by a prior reauth failure).
            if not row:
                logger.warning(
                    f"ConnectorEngine: {connector_name} has NO token row for "
                    f"user_id={user_id!r} — never connected, or the caller passed a "
                    f"different user id than the one used at connect time."
                )
                raise ConnectorNotConnectedError(
                    f"{connector_name} is not connected. "
                    "Go to Settings → Connectors to connect it."
                )
            logger.warning(
                f"ConnectorEngine: {connector_name} token row EXISTS for "
                f"user_id={user_id!r} but is_active=FALSE — it was deactivated by an "
                f"earlier re-auth failure. The user must reconnect once."
            )
            raise ConnectorNotConnectedError(
                f"{connector_name} needs to be reconnected — its saved authorisation "
                "was marked inactive. Go to Settings → Connectors and connect it again."
            )

        # Decrypt the stored token. A failure here is NOT "not connected" — the row
        # exists and is active. It almost always means this PROCESS has a different
        # (or missing) vault key than the process that STORED the token: the worker's
        # FERNET_KEY / VAULT_ENCRYPTION_KEY must EXACTLY match the gateway's. Surface
        # that precisely instead of the misleading generic error, so ops can fix the
        # env rather than chase a phantom "reconnect" loop.
        from store.credential_vault import decrypt_value
        try:
            access_token = decrypt_value(row[0])
        except Exception as _dec_err:
            logger.error(
                f"ConnectorEngine: token decrypt FAILED for {connector_name} "
                f"(user token is present + active, but this process cannot decrypt it). "
                f"This is a VAULT KEY MISMATCH — set the SAME FERNET_KEY on this worker "
                f"as the gateway. err={type(_dec_err).__name__}"
            )
            raise ConnectorNotConnectedError(
                f"{connector_name}: the connection exists but this server can't read its "
                f"stored credentials — the worker's encryption key (FERNET_KEY) does not "
                f"match the one used when you connected. This is a server configuration "
                f"issue, not something you can fix by reconnecting. Ask an administrator to "
                f"align FERNET_KEY across the gateway and worker hosts."
            )

        # ── PAT connectors (GitLab, Jira): no refresh_token — return immediately ──
        if defn and defn.get("auth_type") == "pat":
            return {
                "access_token": access_token,
                "scopes": row[3] or [],
                "metadata": row[4] or {},
            }

        # ── DPI consent connectors: verify the consent artifact, never refresh ──
        if defn and defn.get("auth_type") == "dpi_consent":
            import json as _json
            from connectors.dpi.consent import consent_handler
            try:
                artifact = _json.loads(access_token)
            except Exception:
                raise ConnectorReauthRequired(f"{connector_name}: malformed consent — please re-grant.")
            ok, reason = consent_handler.verify_artifact(artifact)
            if not ok:
                self._deactivate_token(user_id, connector_name)
                raise ConnectorReauthRequired(f"{connector_name} consent invalid: {reason}")
            return {
                "access_token": access_token,            # the artifact JSON, passed to the adapter
                "scopes": row[3] or artifact.get("scope", []),
                "metadata": {**(row[4] or {}), "auth_type": "dpi_consent", "consent": artifact},
            }

        # ── Auto-refresh if the access token is expiring soon ─────────────────
        # Timezone-AWARE comparison. expires_at is TIMESTAMPTZ, so psycopg2 hands
        # it back in the DB session's timezone. The previous code did
        # `expires_at.replace(tzinfo=None)` and compared against utcnow(), which
        # silently added the UTC offset to the deadline (+5h30m for IST) — the token
        # therefore always looked fresh, refresh NEVER ran, and the connector only
        # broke later when Graph returned 401. Comparing two aware datetimes is
        # correct regardless of the session timezone.
        expires_at = row[2]
        if expires_at:
            import datetime
            now = datetime.datetime.now(datetime.timezone.utc)
            if getattr(expires_at, "tzinfo", None) is None:
                # Naive value → it was written as UTC (see _update_token /
                # _store_token, both use utcfromtimestamp), so label it UTC.
                expires_aware = expires_at.replace(tzinfo=datetime.timezone.utc)
            else:
                expires_aware = expires_at
            if (expires_aware - now).total_seconds() < _REFRESH_WINDOW_S:
                access_token = self._refresh_token(user_id, connector_name, row[1])

        return {
            "access_token": access_token,
            "scopes": row[3] or [],
            "metadata": row[4] or {},
        }

    def _refresh_token(self, user_id: str, connector_name: str, enc_refresh_token: Optional[str]) -> str:
        """Refresh access token. Returns new access token."""
        if not enc_refresh_token:
            self._deactivate_token(user_id, connector_name)
            raise ConnectorReauthRequired(
                f"{connector_name} session expired. Please reconnect."
            )

        from store.credential_vault import decrypt_value, encrypt_value
        from connectors.oauth2 import oauth2_handler

        try:
            refresh_token = decrypt_value(enc_refresh_token)
        except Exception as _dec_err:
            # Same vault-key-mismatch case as the access token — do NOT deactivate
            # (the token is fine; THIS process just can't read it). Surface precisely.
            logger.error(
                f"ConnectorEngine: refresh-token decrypt FAILED for {connector_name} "
                f"— VAULT KEY MISMATCH (align FERNET_KEY across gateway + worker). "
                f"err={type(_dec_err).__name__}"
            )
            raise ConnectorNotConnectedError(
                f"{connector_name}: this server can't read the stored credentials "
                f"(FERNET_KEY mismatch between gateway and worker) — a server config issue."
            )
        defn = self._load_definition(connector_name)
        auth_config = self._build_oauth_config(defn)

        try:
            token_set = oauth2_handler.refresh_token(auth_config, refresh_token)
            connector_metrics.record_token_refresh(connector_name, user_id, True)
        except ConnectorReauthRequired:
            # The GRANT itself is gone (invalid_grant / revoked / consent required).
            # This is the ONLY case where deactivating is correct.
            connector_metrics.record_token_refresh(connector_name, user_id, False)
            self._deactivate_token(user_id, connector_name)
            raise
        except ConnectorTransientError as _tr_err:
            # Server-side / network / misconfiguration (e.g. `unauthorized_client`
            # from refreshing a single-tenant app against /common/, an expired client
            # secret, relay/egress down, or a 5xx at the token endpoint). The refresh
            # token is STILL VALID, so we must NOT deactivate it — doing so is what
            # produced the "reconnect the connector every hour" loop.
            connector_metrics.record_token_refresh(connector_name, user_id, False)
            logger.error(
                f"ConnectorEngine: token refresh FAILED for {connector_name} "
                f"(TRANSIENT/CONFIG — token left ACTIVE). Check AZURE_AD_TENANT_ID, "
                f"the client secret, and LLM_PROXY_URL/egress on this host. "
                f"err={_tr_err}"
            )
            raise ConnectorTransientError(
                f"{connector_name}: couldn't refresh the session right now — "
                f"{_tr_err} Please retry; you do NOT need to reconnect."
            )
        except Exception as _rf_err:
            # Unknown failure — still treat as transient. Wrongly deactivating costs
            # the user a manual reconnect; a failed run costs one retry.
            connector_metrics.record_token_refresh(connector_name, user_id, False)
            logger.error(
                f"ConnectorEngine: token refresh call FAILED for {connector_name} "
                f"(unexpected {type(_rf_err).__name__} — token left ACTIVE): {_rf_err}"
            )
            raise ConnectorTransientError(
                f"{connector_name}: couldn't refresh the session from this server "
                f"(unexpected error). Your connection is still valid; please retry."
            )

        # Persist new tokens
        self._update_token(user_id, connector_name, token_set)
        return token_set.access_token

    def _update_token(self, user_id: str, connector_name: str, token_set) -> None:
        try:
            from store.credential_vault import encrypt_value
            import datetime
            from db.database import SessionLocal
            db = SessionLocal()
            try:
                expires_dt = datetime.datetime.utcfromtimestamp(token_set.expires_at)
                enc_access = encrypt_value(token_set.access_token)
                enc_refresh = encrypt_value(token_set.refresh_token) if token_set.refresh_token else None
                db.execute(
                    __import__("sqlalchemy").text(
                        "UPDATE ainxt.user_oauth_tokens SET "
                        "access_token = :at, refresh_token = COALESCE(:rt, refresh_token), "
                        "expires_at = :ea, scopes = :sc, updated_at = NOW() "
                        "WHERE user_id = :uid AND connector_name = :cn"
                    ),
                    {
                        "at": enc_access,
                        "rt": enc_refresh,
                        "ea": expires_dt,
                        "sc": token_set.scopes,
                        "uid": user_id,
                        "cn": connector_name,
                    },
                )
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"ConnectorEngine._update_token: {e}")

    def _deactivate_token(self, user_id: str, connector_name: str) -> None:
        try:
            from db.database import SessionLocal
            db = SessionLocal()
            try:
                db.execute(
                    __import__("sqlalchemy").text(
                        "UPDATE ainxt.user_oauth_tokens SET is_active = FALSE, updated_at = NOW() "
                        "WHERE user_id = :uid AND connector_name = :cn"
                    ),
                    {"uid": user_id, "cn": connector_name},
                )
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"ConnectorEngine._deactivate_token: {e}")

    def _get_context(self, user_id: str, connector_name: str, token_row: dict, defn: dict) -> ConnectorContext:
        import os as _os
        auth_type = defn.get("auth_type", "oauth2")
        # DPI sandbox: synthetic, offline, no real upstream/credentials. On only
        # for dpi_* connectors when DPI_SANDBOX is set.
        is_sandbox = (
            auth_type == "dpi_consent"
            and _os.getenv("DPI_SANDBOX", "").strip().lower() in ("1", "true", "yes", "on")
        )
        return ConnectorContext(
            user_id=user_id,
            connector_name=connector_name,
            access_token=token_row["access_token"],
            scopes=token_row.get("scopes", []),
            tenant_id=token_row.get("metadata", {}).get("tenant_id"),
            metadata={
                "base_url": defn.get("base_url", ""),
                **token_row.get("metadata", {}),
            },
            auth_type=auth_type,
            is_sandbox=is_sandbox,
        )

    # ── Step 7: Adapter selection ─────────────────────────────────────────────

    def _get_adapter(self, connector_name: str, defn: dict):
        if connector_name not in self._adapters:
            if defn.get("has_custom_adapter"):
                self._adapters[connector_name] = self._load_custom_adapter(connector_name)
            else:
                from connectors.adapters.base import GenericHTTPAdapter
                self._adapters[connector_name] = GenericHTTPAdapter()
        return self._adapters[connector_name]

    def _load_custom_adapter(self, connector_name: str):
        """Lazy-load the custom adapter module for this connector."""
        adapter_map = {
            "microsoft_365": "connectors.adapters.microsoft365",
            "slack": "connectors.adapters.slack",
            "gmail": "connectors.adapters.gmail",
            # GitLab — custom adapter delegates to tools/gitlab_tools.py (shared with SDLC).
            # Replaces the previous GenericHTTPAdapter path (5 read-only tools) with the
            # full read + write surface used by the SDLC pipeline. The tool list itself
            # lives in connectors/seed.py and must stay in step with the DB row and
            # GitLabAdapter._TOOL_MAP — they have drifted before, when a catch-up
            # migration overwrote the DB row with fewer tools.
            "gitlab": "connectors.adapters.gitlab",
            # GitHub — custom adapter delegates to tools/github_tools.py. Mirrors
            # the GitLab adapter above; both providers can be enabled side-by-side
            # (a deployment can seed both connectors) or exclusively via SCM_PROVIDER.
            "github": "connectors.adapters.github",
            # Cowork connector pack (2026-05-30) — custom adapters
            "google_drive": "connectors.adapters.google_drive",
            # Google Calendar shares the Google OAuth client with Gmail/Drive.
            "google_calendar": "connectors.adapters.google_calendar",
            "docusign": "connectors.adapters.docusign",
            "zoom": "connectors.adapters.zoom",
            "jira": "connectors.adapters.jira",
            "jira_connector": "connectors.adapters.jira",
            "confluence": "connectors.adapters.confluence",
            # DPI (India Stack) connectors — consent-based, sandbox-aware.
            "dpi_account_aggregator": "connectors.adapters.dpi_account_aggregator",
            "dpi_digilocker": "connectors.adapters.dpi_digilocker",
        }
        module_path = adapter_map.get(connector_name)
        if not module_path:
            from connectors.adapters.base import GenericHTTPAdapter
            return GenericHTTPAdapter()

        import importlib
        mod = importlib.import_module(module_path)
        # Adapters expose a module-level singleton: slack_adapter, gmail_adapter, etc.
        # Convention is connector_name + "_adapter", but some modules drop the
        # underscore (e.g. connector "microsoft_365" → module singleton
        # "microsoft365_adapter"). Try the conventional name first, then fall back
        # to ANY module-level AdapterBase instance. Never return the bare module —
        # that yields `module.execute` AttributeErrors that the retry/pagination
        # loop swallows into a false success=True/count=0.
        from connectors.adapters.base import AdapterBase
        singleton_name = connector_name.replace("-", "_") + "_adapter"
        adapter = getattr(mod, singleton_name, None)
        if not isinstance(adapter, AdapterBase):
            adapter = next(
                (v for v in vars(mod).values() if isinstance(v, AdapterBase)),
                None,
            )
        if adapter is None:
            raise ValueError(
                f"Custom adapter module {module_path!r} for connector "
                f"{connector_name!r} exposes no AdapterBase singleton"
            )
        return adapter

    # ── Step 8: Execute with retry + pagination ────────────────────────────────

    def _execute_with_pagination(
        self,
        adapter,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        deadline_ms: int = 0,
    ) -> tuple[list[dict], bool, bool]:
        """
        Execute tool call with pagination loop.
        Returns (all_items, partial_flag, timed_out_flag).
        partial=True means pagination stopped early due to an error.
        timed_out=True means the wall-clock deadline was hit.
        """
        if not tool.paginated:
            page = self._execute_with_retry(adapter, tool, params, context, cursor=None)
            return page.items, False, False

        all_items: list[dict] = []
        partial = False
        timed_out = False
        cursor = None

        for page_num in range(MAX_PAGES):
            # Gap 4: enforce wall-clock deadline before starting a new page
            if deadline_ms and int(time.time() * 1000) >= deadline_ms:
                logger.warning(
                    f"ConnectorEngine: timeout hit at page {page_num} for "
                    f"{context.connector_name}.{tool.name}"
                )
                timed_out = True
                break

            try:
                page = self._execute_with_retry(adapter, tool, params, context, cursor=cursor)
                all_items.extend(page.items)
                cursor = page.next_cursor
                if not cursor:
                    break
                if len(all_items) >= tool.max_items:
                    break
            except Exception as e:
                logger.warning(
                    f"ConnectorEngine: pagination stopped at page {page_num} for "
                    f"{context.connector_name}.{tool.name}: {e}"
                )
                partial = True
                break

        return all_items[:tool.max_items], partial, timed_out

    def _execute_with_retry(self, adapter, tool: ConnectorTool, params: dict, context: ConnectorContext, cursor) -> Any:
        """Execute one adapter call with exponential backoff retry.

        A 401 gets ONE forced token refresh + retry before it is allowed to become
        ConnectorReauthRequired. Without this, an access token that expired between
        the pre-flight expiry check and the actual API call (very likely for a
        scheduled job that waited in connector_queue) went straight to
        "REAUTH_REQUIRED" and DEACTIVATED a perfectly refreshable connection.

        ARCH-F-007 / ARCH-F-008 (2026-08-26): wrapped in a per-connector circuit
        breaker (core/circuit_breaker.get_breaker, already used by
        agents/sdlc_pipeline and connectors/adapters/jira.py's underlying
        tools/jira_tools.py client). Evaluated as needed, not theoretical: Jira,
        GitLab, and Confluence get breaker protection today because they delegate
        to their own legacy tool modules — but every OTHER adapter (Microsoft
        365, Gmail, Slack, GitHub, Google Drive, Zoom, DocuSign, the DPI
        connectors) calls adapter.execute() through this exact method with NO
        breaker at all. A stalled/degraded upstream (e.g. a Graph API partial
        outage) previously meant every one of those calls independently retried
        3x with backoff before failing — for a burst of concurrent chat/Cowork
        requests hitting the same outage, that is N x 3 slow failures in a row
        with no fast-fail, unlike Jira/GitLab which already short-circuit via
        their own breaker. Wrapping here closes the gap for every adapter in
        one place, keyed by the connector's own name (e.g. "microsoft_365",
        "gmail", "github") so each connector's failures are tracked
        independently — one connector tripping never blocks another.

        Uses record_success()/record_failure() rather than breaker.call() so
        only genuine upstream-health signals (429/5xx, connection errors, a
        rejected token) count toward the trip threshold — a legitimate 4xx
        business error (404 "issue not found", 403 "no access to this file")
        reflects the request, not upstream health, and must not trip the
        breaker or reset its failure count.
        """
        from core.circuit_breaker import get_breaker

        breaker = get_breaker(context.connector_name)
        if breaker.is_open:
            raise ConnectorTransientError(
                f"{context.connector_name} circuit breaker is OPEN — too many recent "
                f"failures; fast-failing {tool.name} instead of retrying against a "
                f"likely-degraded upstream. It will automatically retry after the "
                f"recovery window."
            )

        max_attempts = 3
        last_exc = None
        refreshed_once = False

        for attempt in range(max_attempts):
            try:
                result = adapter.execute(tool, params, context, cursor=cursor)
                breaker.record_success()
                return result
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status == 401 and not refreshed_once:
                    refreshed_once = True
                    if self._refresh_context_token(context):
                        logger.info(
                            f"ConnectorEngine: 401 on {context.connector_name}.{tool.name} — "
                            f"refreshed the access token, retrying once"
                        )
                        continue
                    raise
                if status in (400, 401, 403, 404):
                    raise  # non-retryable, and not a breaker signal (see docstring)
                if status == 429 or status >= 500:
                    breaker.record_failure(e)
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    wait = min(wait, 32)
                    logger.warning(f"ConnectorEngine: HTTP {status}, retry {attempt+1}/{max_attempts} in {wait:.1f}s")
                    time.sleep(wait)
                    last_exc = e
                    continue
                raise
            except ConnectorTokenRejected as e:
                # An adapter told us the provider rejected the token (Graph 401).
                # Same policy as a raw 401: refresh once, retry once, and only then
                # escalate to a real re-auth requirement.
                if not refreshed_once:
                    refreshed_once = True
                    if self._refresh_context_token(context):
                        logger.info(
                            f"ConnectorEngine: token rejected by "
                            f"{context.connector_name}.{tool.name} — refreshed, retrying once"
                        )
                        continue
                raise ConnectorReauthRequired(str(e))
            except (ConnectorReauthRequired, ConnectorScopeError):
                raise
            except Exception as e:
                breaker.record_failure(e)
                if attempt == max_attempts - 1:
                    raise
                wait = (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning(f"ConnectorEngine: transient error, retry {attempt+1}/{max_attempts}: {e}")
                time.sleep(wait)
                last_exc = e

        if last_exc:
            raise last_exc
        raise RuntimeError("execute_with_retry: exhausted attempts")

    def _refresh_context_token(self, context: ConnectorContext) -> bool:
        """Force a token refresh and update `context.access_token` in place.

        Used by the 401 retry path. Returns True when the context now carries a
        NEW access token and the call is worth retrying; False when refreshing is
        impossible or pointless (non-OAuth auth, no refresh token on file).

        Re-raises ConnectorReauthRequired so a genuinely dead grant still reaches
        the caller and deactivates the token exactly once.
        """
        if context.auth_type not in ("oauth2", ""):
            return False  # PAT / API key / DPI consent cannot be refreshed
        try:
            from db.database import SessionLocal
            db = SessionLocal()
            try:
                row = db.execute(
                    __import__("sqlalchemy").text(
                        "SELECT refresh_token FROM ainxt.user_oauth_tokens "
                        "WHERE user_id = :uid AND connector_name = :cn"
                    ),
                    {"uid": context.user_id, "cn": context.connector_name},
                ).fetchone()
            finally:
                db.close()
        except Exception as exc:
            logger.warning(
                f"ConnectorEngine._refresh_context_token: could not read refresh token "
                f"for {context.connector_name}: {type(exc).__name__}"
            )
            return False

        if not row or not row[0]:
            return False

        new_access = self._refresh_token(
            context.user_id, context.connector_name, row[0]
        )
        if not new_access:
            return False
        context.access_token = new_access
        return True

    # ── Step 9: Response normalization — handled by ConnectorResponse dataclass ─

    # ── Step 10: Compliance check ─────────────────────────────────────────────

    def _compliance_check(self, items: list[dict]) -> bool:
        """Returns True if items contain blocked PCI/PII content."""
        try:
            from agents.compliance_engine import compliance_engine as _ce
            sample = json.dumps(items[:10], default=str)[:5000]  # scan first 10 items
            findings = _ce.analyze(sample)
            return any(f.get("blocked", False) for f in findings)
        except Exception as e:
            logger.debug(f"ConnectorEngine._compliance_check: {e}")
            return False

    # ── Helper: auto-connect PAT connectors from profile vault ────────────────

    # Maps connector_name → token_type in the user_tokens (profile vault) table.
    _PAT_TOKEN_TYPE_MAP: dict = {"gitlab": "gitlab", "github": "github", "jira_connector": "atlassian"}

    def _try_auto_connect_pat(self, user_id: str, connector_name: str) -> bool:
        """
        Auto-connect a PAT connector by reading the user's stored token from
        the profile vault (user_tokens table) and writing it to user_oauth_tokens.

        This is called when ConnectorNotConnectedError is raised for a PAT connector
        — it handles the common case where the user stored their PAT in
        Profile → API Token Vault but never explicitly clicked "Connect" in
        Settings → Connectors.

        Returns True if successfully connected, False if no profile token exists.
        """
        import os as _os
        token_type = self._PAT_TOKEN_TYPE_MAP.get(connector_name)
        if not token_type:
            return False

        try:
            from routers.profile_router import get_decrypted_token
            raw = get_decrypted_token(user_id, token_type)
            if not raw:
                logger.info(
                    f"ConnectorEngine: auto-connect {connector_name} skipped — "
                    f"no {token_type} token in profile vault for user {user_id}"
                )
                return False

            # Build PAT and metadata per connector type
            if connector_name == "gitlab":
                from core.platform_credentials import extract_gitlab_pat
                pat = extract_gitlab_pat(raw)
                base_url = _os.getenv("GITLAB_URL", "https://gitlab.example.com").rstrip("/") + "/api/v4"
                metadata = {
                    "auth_type":  "pat",
                    "pat_header": "PRIVATE-TOKEN",
                    "pat_scheme": "",
                    "base_url":   base_url,
                }
            elif connector_name == "github":
                # GitHub PATs are bare tokens (ghp_.../github_pat_...); the Profile
                # UI may still store a "user:token" pair for symmetry with GitLab —
                # strip any such prefix the same way gitlab's does.
                pat = raw.split(":", 1)[-1].strip() if ":" in raw else raw.strip()
                metadata = {
                    "auth_type":  "pat",
                    "pat_header": "Authorization",
                    "pat_scheme": "Bearer",
                    "base_url":   "https://api.github.com",
                }
            else:  # jira_connector
                # Profile.jsx stores only the bare Atlassian API token (no email
                # prefix). Normalise to "email:api_token" before storing in
                # user_oauth_tokens so JiraAdapter.execute() →
                # extract_atlassian_creds() receives a valid email for Basic Auth.
                # extract_atlassian_creds() detects "email:token" by checking for
                # "@" in the head segment — already-correct values are left unchanged.
                from core.platform_credentials import extract_atlassian_creds as _extract_at
                _email_part, _token_part = _extract_at(raw, "")
                if not _email_part:
                    # Bare token — look up the user's email from the users table.
                    _jira_email = ""
                    try:
                        from db.database import engine as _db_engine
                        import sqlalchemy as _sa2
                        with _db_engine.connect() as _conn:
                            _row = _conn.execute(
                                _sa2.text(
                                    "SELECT email FROM users WHERE id = :uid LIMIT 1"
                                ),
                                {"uid": user_id},
                            ).fetchone()
                        if _row:
                            _jira_email = _row[0] or ""
                    except Exception as _email_err:
                        logger.warning(
                            f"ConnectorEngine: could not resolve email for user "
                            f"{user_id} during jira auto-connect — {_email_err}"
                        )
                    pat = f"{_jira_email}:{raw}" if _jira_email else raw
                    email = _jira_email
                else:
                    # Already "email:token" — store as-is.
                    pat = raw
                    email = _email_part
                base_url = _os.getenv("JIRA_URL", "").rstrip("/") + "/rest/api/3"
                metadata = {
                    "auth_type":  "pat",
                    "pat_header": "Authorization",
                    "pat_scheme": "Basic",
                    "email":      email,   # was missing — needed by JiraAdapter fallback
                    "base_url":   base_url,
                }

            from store.credential_vault import encrypt_value
            encrypted = encrypt_value(pat)

            from db.database import SessionLocal
            import sqlalchemy as _sa
            db = SessionLocal()
            try:
                db.execute(
                    _sa.text("""
                        INSERT INTO ainxt.user_oauth_tokens
                            (user_id, connector_name, access_token, metadata, is_active)
                        VALUES (:uid, :cn, :at, :meta::jsonb, TRUE)
                        ON CONFLICT (user_id, connector_name) DO UPDATE SET
                            access_token = :at,
                            metadata     = EXCLUDED.metadata,
                            is_active    = TRUE,
                            updated_at   = NOW()
                    """),
                    {
                        "uid":  user_id,
                        "cn":   connector_name,
                        "at":   encrypted,
                        "meta": json.dumps(metadata),
                    },
                )
                db.commit()
            finally:
                db.close()

            logger.info(
                f"ConnectorEngine: auto-connected {connector_name} for user {user_id} "
                f"from profile vault (token_type={token_type})"
            )
            return True

        except Exception as e:
            logger.warning(f"ConnectorEngine: auto-connect {connector_name} failed — {e}")
            return False

    # ── Helper: check user permission for a connector tool ────────────────────

    def _check_user_permission(self, user_id: str, connector_name: str, tool_name: str) -> str:
        """
        Check the user's stored permission decision for a connector tool.

        Queries ainxt.user_connector_permissions. A specific tool row takes
        precedence over a wildcard '*' row for the same connector.

        Returns one of:
          'always_allow' — user pre-approved; skip the gate
          'denied'       — user explicitly denied; block without prompting
          'needs_prompt' — no decision stored; ask the user
        """
        try:
            from db.database import SessionLocal
            import sqlalchemy as _sa
            db = SessionLocal()
            try:
                rows = db.execute(
                    _sa.text("""
                        SELECT tool_name, always_allow
                        FROM ainxt.user_connector_permissions
                        WHERE user_id = :uid
                          AND connector_name = :cn
                          AND tool_name IN (:tn, '*')
                        ORDER BY
                            CASE WHEN tool_name = :tn THEN 0 ELSE 1 END
                        LIMIT 2
                    """),
                    {"uid": user_id, "cn": connector_name, "tn": tool_name},
                ).fetchall()
            finally:
                db.close()

            if rows:
                # First row is the most specific match (specific tool > wildcard)
                always_allow = rows[0][1]
                return "always_allow" if always_allow else "denied"

        except Exception as e:
            logger.warning(f"ConnectorEngine._check_user_permission: {e}")

        return "needs_prompt"

    # ── Helper: load connector definition ─────────────────────────────────────

    def _load_definition(self, connector_name: str) -> dict:
        """Load connector definition from DB (cached in memory for 5 min)."""
        cache_attr = f"_defn_cache_{connector_name}"
        cached = getattr(self, cache_attr, None)
        if cached and cached.get("_ts", 0) > time.time() - 300:
            return cached

        try:
            from db.database import SessionLocal
            db = SessionLocal()
            try:
                row = db.execute(
                    __import__("sqlalchemy").text(
                        "SELECT name, display_name, category, auth_type, auth_config, tools, "
                        "base_url, has_custom_adapter, rate_limit_per_min, is_active, "
                        "required_ad_level, allowed_departments "
                        "FROM ainxt.connector_definitions WHERE name = :name"
                    ),
                    {"name": connector_name},
                ).fetchone()
            finally:
                db.close()
        except Exception as e:
            raise RuntimeError(f"Failed to load connector definition for {connector_name!r}: {e}")

        if not row:
            raise ConnectorNotConnectedError(
                f"No connector named {connector_name!r} is registered. "
                "Check Settings → Connectors."
            )
        if not row[9]:  # is_active
            raise ConnectorNotConnectedError(f"Connector {connector_name!r} is disabled.")

        defn = {
            "name": row[0],
            "display_name": row[1],
            "category": row[2],
            "auth_type": row[3],
            "auth_config": row[4] or {},
            "tools": row[5] or [],
            "base_url": row[6],
            "has_custom_adapter": row[7],
            "rate_limit_per_min": row[8] or 100,
            # Gap 1: access policy columns (None means no restriction)
            "required_ad_level": row[10],
            "allowed_departments": list(row[11]) if row[11] else [],
            "_ts": time.time(),
        }
        setattr(self, cache_attr, defn)
        return defn

    def _get_tool(self, defn: dict, tool_name: str) -> ConnectorTool:
        """Find a tool in the connector definition."""
        for t in defn.get("tools", []):
            if t.get("name") == tool_name:
                seeded_max_items = t.get("max_items", TOOL_MAX_ITEMS.get(tool_name, _MAX_ITEMS_DEFAULT))
                resolved_max_items = seeded_max_items
                if tool_name in _STALE_SEEDED_MAX_ITEMS_TOOLS:
                    ceiling = TOOL_MAX_ITEMS.get(tool_name, _MAX_ITEMS_DEFAULT)
                    try:
                        resolved_max_items = max(int(seeded_max_items or 0), int(ceiling))
                    except (TypeError, ValueError):
                        resolved_max_items = ceiling
                    if resolved_max_items != seeded_max_items:
                        logger.info(
                            f"ConnectorEngine._get_tool: {tool_name!r} seeded "
                            f"max_items={seeded_max_items!r} is stale — using raised "
                            f"ceiling {resolved_max_items} so pagination doesn't stop "
                            f"after the first Graph page (email/Teams retrieval & "
                            f"summarization fix)."
                        )
                return ConnectorTool(
                    name=t["name"],
                    description=t.get("description", ""),
                    method=t.get("method", "GET"),
                    path=t.get("path", "/"),
                    input_schema=t.get("input_schema", {}),
                    requires_scopes=t.get("requires_scopes", []),
                    cache_ttl_s=t.get("cache_ttl_s", 300),
                    paginated=t.get("paginated", False),
                    max_items=resolved_max_items,
                    is_write=t.get("is_write", False),
                    query_params=t.get("query_params", {}),
                    response_items_path=t.get("response_items_path", "value"),
                    response_fields=t.get("response_fields", []),
                )
        raise ValueError(f"Tool {tool_name!r} not found in connector {defn['name']!r}")

    def _build_oauth_config(self, defn: dict):
        from connectors.base import OAuth2Config
        from connectors.oauth2 import pin_azure_tenant

        ac = defn.get("auth_config", {})
        if isinstance(ac, str):
            # Defensive: some code paths hand back the raw JSONB string.
            try:
                ac = json.loads(ac) if ac else {}
            except Exception:
                ac = {}
        # CRITICAL: pin the Entra authority here too. The connect path
        # (routers/connectors_router) has always done this, but this REFRESH path
        # did not — so a single-tenant app refreshed against the seeded /common/
        # URL, Entra returned `unauthorized_client`, and the token was deactivated
        # ~1h after connecting. Same helper, both paths, no drift.
        ac = pin_azure_tenant(ac)
        return OAuth2Config(
            authorize_url=ac.get("authorize_url", ""),
            token_url=ac.get("token_url", ""),
            client_id_env=ac.get("client_id_env", ""),
            client_secret_env=ac.get("client_secret_env", ""),
            scopes=ac.get("scopes", []),
            pkce=ac.get("pkce", True),
            extra_params=ac.get("extra_params", {}),
            revoke_url=ac.get("revoke_url"),
        )

    # ── Gap 1: Access control ─────────────────────────────────────────────────

    def _get_user_level_dept(self, user_id: str) -> tuple[int, str]:
        """Returns (ad_level, department) for the given user from the DB."""
        try:
            from db.database import SessionLocal
            import sqlalchemy
            db = SessionLocal()
            try:
                row = db.execute(
                    sqlalchemy.text(
                        "SELECT ad_level, department FROM ainxt.users WHERE id = :uid"
                    ),
                    {"uid": user_id},
                ).fetchone()
            finally:
                db.close()
            if row:
                return (int(row[0] or 6), str(row[1] or ""))
        except Exception as e:
            logger.debug(f"ConnectorEngine._get_user_level_dept: {e}")
        return (6, "")  # safe default: most-junior, no department

    def _check_access_policy(self, user_id: str, defn: dict) -> None:
        """Enforce required_ad_level and allowed_departments from connector definition."""
        required_level = defn.get("required_ad_level")
        allowed_depts = defn.get("allowed_departments") or []

        if required_level is None and not allowed_depts:
            return  # no policy set — open to all authenticated users

        user_level, user_dept = self._get_user_level_dept(user_id)

        # AD level: lower number = more senior. required_ad_level=3 means level ≤ 3 only.
        if required_level is not None and user_level > required_level:
            raise ConnectorAccessDeniedError(
                f"Your role level ({user_level}) does not meet the minimum required "
                f"({required_level}) for connector '{defn['name']}'. "
                "Contact your administrator to request access."
            )

        if allowed_depts and user_dept not in allowed_depts:
            raise ConnectorAccessDeniedError(
                f"Connector '{defn['name']}' is restricted to departments: "
                f"{', '.join(allowed_depts)}. Your department: '{user_dept}'."
            )

    # ── Gap 2: Data minimization ──────────────────────────────────────────────

    def _minimize_response(self, items: list[dict], tool: ConnectorTool) -> list[dict]:
        """Keep only whitelisted fields per item when response_fields is set."""
        if not tool.response_fields:
            return items
        fields = set(tool.response_fields)
        return [{k: v for k, v in item.items() if k in fields} for item in items]

    # ── Rate limiting ─────────────────────────────────────────────────────────

    def _check_rate_limit(self, user_id: str, connector_name: str, limit_per_min: int) -> None:
        try:
            r = self._get_redis()
            if not r:
                return
            key = f"connector:ratelimit:{connector_name}:{user_id}"
            count = r.incr(key)
            if count == 1:
                r.expire(key, 60)
            if count > limit_per_min:
                raise ConnectorRateLimitError(
                    f"Rate limit exceeded for {connector_name} ({limit_per_min}/min). Try again later."
                )
        except ConnectorRateLimitError:
            raise
        except Exception:
            pass  # non-critical

    # ── Redis helper ──────────────────────────────────────────────────────────

    def _get_redis(self):
        if self._redis is None:
            try:
                import redis
                from core.config import REDIS_HOST as _cfg_redis_host
                self._redis = redis.Redis(
                    # No localhost default: reuses the canonical (also
                    # no-default) core.config value; construction is
                    # already wrapped in try/except below.
                    host=os.getenv("REDIS_HOST", _cfg_redis_host),
                    port=int(os.getenv("REDIS_PORT", 6379)),
                    password=os.getenv("REDIS_PASSWORD", "") or None,
                    db=0,
                    decode_responses=True,
                    socket_timeout=2,
                )
            except Exception:
                pass
        return self._redis


# Module-level singleton
connector_engine = ConnectorEngine()
