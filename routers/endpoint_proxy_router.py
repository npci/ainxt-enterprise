# SPDX-License-Identifier: MIT
# ============================================================
# Endpoint Proxy Router
# OpenAI-compatible proxy routes for managed endpoints.
#
# Routes (registered under /ainxt/v1/api in gateway.py):
#   GET  /ainxt/v1/api/{slug}/v1/models
#   POST /ainxt/v1/api/{slug}/v1/chat/completions
#
# Caller auth:
#   Callers pass the platform-generated API key as Authorization: Bearer <key>.
#   The key is validated by SHA-256 hash lookup in user_api_keys (same as CLI keys).
#   key_hash is cached in the slug→endpoint Redis entry for fast validation.
#
#   NOTE: this key is SHARED by everyone using the endpoint — it identifies the
#   ENDPOINT, not the person. model_usages attribution is therefore per-endpoint
#   (via system_user_id), not per-caller.
#
# LiteLLM backend key (platform → LiteLLM):
#   Controlled by use_env_key on the endpoint:
#     True  → os.getenv(env_key_name)  — team-specific LiteLLM virtual key
#     False → global LOCAL_LLM_API_KEY
#
# TWO SERVING PATHS
#   local  — model is in the LiteLLM catalog → forwarded to LiteLLM. Free, ungated.
#   cloud  — model is in the platform cloud catalog (GPT / Claude / Gemini) →
#            served by the platform gateway through models.model_router. PAID, so
#            it is gated against the funding HOD's monthly cap and billed.
#
#   Cloud models can ONLY be enabled by an admin adding them to the endpoint's
#   model_ids allowlist (routers/endpoint_mgmt_router.py). An endpoint with no
#   allowlist is local-only and can never incur cloud spend.
#
# BUDGET
#   ainxt.endpoint_hod_mapping names the funding HOD; their cap lives in
#   ainxt.hod_allocation_caps and running endpoint spend in
#   hod_allocation_ledger.endpoint_spend_usd. See
#   services/endpoint_budget_governor.py for the accounting.
#
# COMPLIANCE
#   INPUT  is scanned and blocked/redacted BEFORE any provider call, on both the
#          local and cloud paths — non-bypassable.
#   OUTPUT is redacted in place on the NON-streaming paths. On the STREAMING
#          paths tokens have already been forwarded to the client by the time the
#          full text can be assembled, so the post-stream scan is an AUDIT (it
#          logs when a redaction would have applied) and not a redaction. This is
#          pre-existing behaviour for the local path and matches it for cloud;
#          callers needing guaranteed output redaction must use stream=false.
# ============================================================

import hashlib
import json
import os
import threading
import time
import uuid
from typing import Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.config import HOD_APPROVAL_ENABLED
from core.logger import logger
from db.database import SessionLocal
from db.models import ManagedEndpoint, UserAPIKey, User

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_LITELLM_BASE_URL = (
    os.getenv("LOCAL_LLM_BASE_URL") or os.getenv("LITELLM_BASE_URL", "")
).rstrip("/")
_LOCAL_LLM_API_KEY = (
    os.getenv("LOCAL_LLM_API_KEY") or os.getenv("LITELLM_API_KEY", "sk-local")
)

_MODELS_CACHE_TTL = 300   # seconds — per-endpoint model list cache
_SLUG_CACHE_TTL   = 60    # seconds — slug → endpoint config cache

# model_usages.endpoint prefix for managed-endpoint traffic. Fixed leading
# segment so existing dashboards that GROUP BY endpoint don't fragment into one
# bucket per slug. Mirrors services.endpoint_budget_governor.ENDPOINT_USAGE_PREFIX.
ENDPOINT_USAGE_PREFIX = "/endpoint/"

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

proxy_router = APIRouter(tags=["endpoint-proxy"])

# ---------------------------------------------------------------------------
# DB dependency
# ---------------------------------------------------------------------------


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Minimal Pydantic schema — only the fields we need to inspect.
# The full raw body is read separately and forwarded to LiteLLM untouched
# so that tools, tool_choice, response_format, top_p, seed, etc. all pass
# through without requiring code changes for each new OpenAI field.
# ---------------------------------------------------------------------------


class _ChatRequest(BaseModel):
    model:    str
    messages: list                # raw list — no structural validation
    stream:   Optional[bool] = True  # needed only to branch stream vs non-stream


# ---------------------------------------------------------------------------
# Slug resolution with Redis cache
# ---------------------------------------------------------------------------


def _ep_to_dict(ep: ManagedEndpoint, key_hash: Optional[str]) -> dict:
    """
    Minimal dict stored in Redis cache — only what the proxy needs at request time.
    key_hash is fetched from user_api_keys and included for fast auth validation.
    """
    return {
        "id":           ep.id,
        "slug":         ep.slug,
        "env_key_name": ep.env_key_name,
        "use_env_key":  ep.use_env_key,
        "key_hash":     key_hash,    # SHA-256 from user_api_keys — for Bearer validation
        "enabled":      ep.enabled,
        "tool_calls_enabled": ep.tool_calls_enabled,
        "system_user_id":     str(ep.system_user_id) if ep.system_user_id else None,
        "model_ids":    ep.model_ids or [],   # allowed models (local AND cloud); [] = no restriction
        # NOTE: fallback for an unrecognised model is COMPUTED from model_ids at
        # request time (see _resolve_model — local-first, else cheapest cloud),
        # never read from ep.fallback_model. That DB column is no longer written
        # by the admin API and is not cached here.
    }


def _resolve_endpoint(slug: str, db: Session) -> dict:
    """
    Resolve slug → endpoint dict.
    Checks Redis DB 0 cache first (TTL 60s), falls back to DB.
    On DB hit, also fetches key_hash from user_api_keys and caches it together.
    Returns a plain dict (not ORM object) for safe use across threads.
    """
    cache_key = f"ep:slug:{slug}"
    try:
        from core.kv import get_kv
        kv = get_kv(0)
        cached = kv.get(cache_key)
        if cached:
            ep_dict = json.loads(cached)
            if not ep_dict.get("enabled", True):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Endpoint '{slug}' not found or is disabled.",
                )
            return ep_dict
    except HTTPException:
        raise
    except Exception:
        pass  # cache miss or Redis unavailable — fall through to DB

    ep = db.query(ManagedEndpoint).filter_by(slug=slug, enabled=True).first()
    if not ep:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Endpoint '{slug}' not found or is disabled.",
        )

    # Fetch key_hash from user_api_keys for auth validation
    key_hash = None
    if ep.api_key_id:
        key_row = db.query(UserAPIKey).filter_by(id=ep.api_key_id, is_active=True).first()
        if key_row:
            key_hash = key_row.key_hash

    ep_dict = _ep_to_dict(ep, key_hash)

    try:
        from core.kv import get_kv
        kv = get_kv(0)
        kv.set(cache_key, json.dumps(ep_dict), ex=_SLUG_CACHE_TTL)
    except Exception:
        pass

    return ep_dict


# ---------------------------------------------------------------------------
# Caller auth — Bearer token validation against user_api_keys hash
# ---------------------------------------------------------------------------


def _validate_endpoint_key(request: Request, ep: dict) -> None:
    """
    Validate the Authorization: Bearer <key> header.
    The provided key is SHA-256 hashed and compared against the stored hash
    from user_api_keys — identical pattern to CLI key auth in api_key_auth.py.
    Raises 401 on mismatch, 503 if no key is configured on the endpoint.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. Expected: Authorization: Bearer <key>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw = auth[7:].strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token is empty.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    stored_hash = ep.get("key_hash")
    if not stored_hash:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="This endpoint has no API key configured. Contact your admin.",
        )

    if hashlib.sha256(raw.encode()).hexdigest() != stored_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key for this endpoint.",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# LiteLLM backend key selection
# ---------------------------------------------------------------------------


def _get_litellm_key(ep: dict) -> str:
    """
    Select the LiteLLM API key to use based on the endpoint's use_env_key flag.
      use_env_key=True  → os.getenv(env_key_name)  — team-specific virtual key
      use_env_key=False → global LOCAL_LLM_API_KEY
    Raises 503 if use_env_key=True but the env var is not set.
    """
    if ep.get("use_env_key"):
        env_key_name = ep.get("env_key_name") or ""
        key = os.getenv(env_key_name)
        if not key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"use_env_key is ON but environment variable '{env_key_name}' is not set. "
                    "Contact your admin."
                ),
            )
        return key
    return _LOCAL_LLM_API_KEY


# ---------------------------------------------------------------------------
# Per-endpoint model list (cached per slug)
# ---------------------------------------------------------------------------


def _get_allowed_models(slug: str, litellm_key: str) -> List[str]:
    """
    Fetch models accessible to the given LiteLLM key.
    Cached in Redis DB 0 per slug (key: ep:models:{slug}, TTL 300s).
    Returns empty list if LiteLLM is unreachable (fail-open — LiteLLM enforces its own controls).
    """
    cache_key = f"ep:models:{slug}"
    try:
        from core.kv import get_kv
        kv = get_kv(0)
        cached = kv.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    if not _LITELLM_BASE_URL:
        logger.warning("[endpoint-proxy] LITELLM_BASE_URL not configured — skipping model check")
        return []

    try:
        resp = httpx.get(
            f"{_LITELLM_BASE_URL}/v1/models",
            headers={"Authorization": f"Bearer {litellm_key}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        models = [m["id"] for m in data if isinstance(m, dict) and m.get("id")]
    except Exception as exc:
        logger.warning(f"[endpoint-proxy] Model list fetch failed for slug='{slug}': {exc}")
        return []  # fail-open

    try:
        from core.kv import get_kv
        kv = get_kv(0)
        kv.set(cache_key, json.dumps(models), ex=_MODELS_CACHE_TTL)
    except Exception:
        pass

    return models


# ---------------------------------------------------------------------------
# Model resolution — which model actually serves this request?
#
# The platform's default behaviour for an unrecognised model name is to fall
# through to ModelRouter auto-routing, which lands on TIER_MEDIUM = gpt-5.4 —
# a PAID cloud model (see gateway._oai_model_hint returning None, and
# models/model_router.py:1135). For a managed endpoint that would mean a caller
# could trigger cloud spend on an endpoint where no admin ever enabled cloud.
#
# So resolution here is explicit and never escalates by accident:
#   requested ∈ allowlist        → serve it (local → LiteLLM, cloud → gated)
#   requested ∉ allowlist        → first LOCAL model in the allowlist ($0)
#                                → else cheapest CLOUD model in the allowlist,
#                                  computed (gated + billed) — never admin-set
#                                → else 403
# ---------------------------------------------------------------------------


class _ModelDecision:
    """Outcome of resolving a caller's requested model against an endpoint."""

    __slots__ = ("model", "kind", "substituted", "requested")

    def __init__(self, model: str, kind: str, requested: str, substituted: bool = False):
        self.model       = model        # model that will actually serve the request
        self.kind        = kind         # "local" | "cloud"
        self.requested   = requested    # what the caller asked for
        self.substituted = substituted  # True when model != requested


def _resolve_model(ep: dict, requested: str, litellm_key: str, slug: str) -> _ModelDecision:
    """
    Decide which model serves this request, or raise 403 when nothing may.

    Enforces the endpoint's explicit allowlist (model_ids). When no allowlist is
    set the endpoint is local-only by definition: cloud models are reachable
    ONLY via an explicit admin-curated allowlist, so an unrestricted endpoint can
    never incur cloud spend.
    """
    from services.endpoint_model_catalog import (
        cheapest_cloud_model, first_local_model, is_cloud_model, is_local_model,
    )

    allowlist = ep.get("model_ids") or []

    # ── Explicit allowlist path ──────────────────────────────────────────────
    if allowlist:
        if requested in allowlist:
            if is_cloud_model(requested):
                return _ModelDecision(requested, "cloud", requested)
            if is_local_model(requested):
                return _ModelDecision(requested, "local", requested)
            # In the allowlist but in NEITHER catalog. This happens when an admin
            # saved a cloud model whose feature flag was later turned off (the
            # stored allowlist keeps the id, but get_cloud_models() no longer
            # returns it). Treating it as "local" would forward a cloud model to
            # LiteLLM and fail with a confusing 502, so refuse explicitly.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": (
                        f"Model '{requested}' is currently unavailable — it is no "
                        f"longer offered by the platform (it may have been retired "
                        f"or disabled). Ask an admin to update this endpoint."
                    ),
                    "allowed_models": allowlist,
                },
            )

        # Unrecognised / disallowed model → prefer a FREE local substitute.
        local_alt = first_local_model(allowlist)
        if local_alt:
            logger.info(
                "[endpoint-proxy] slug=%s model=%r not allowed — falling back to "
                "local %r (no cloud spend)", slug, requested, local_alt,
            )
            return _ModelDecision(local_alt, "local", requested, substituted=True)

        # Cloud-only endpoint: fall back to the cheapest CLOUD model in the
        # allowlist — computed from current pricing, never admin-set, so it
        # can never drift out of sync with the allowlist or platform pricing.
        # Still gated + billed exactly like an explicitly requested cloud model.
        fb = cheapest_cloud_model(allowlist)
        if fb:
            logger.info(
                "[endpoint-proxy] slug=%s model=%r not allowed — falling back to "
                "cheapest cloud %r (gated + billed)", slug, requested, fb,
            )
            return _ModelDecision(fb, "cloud", requested, substituted=True)

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": f"Model '{requested}' is not allowed for this endpoint.",
                "allowed_models": allowlist,
            },
        )

    # ── No allowlist — local-only, validated against LiteLLM ────────────────
    if is_cloud_model(requested):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": (
                    f"Model '{requested}' is a cloud model and is not enabled for "
                    f"this endpoint. An admin must add it to the endpoint's allowed "
                    f"models and assign a budget owner."
                ),
            },
        )

    allowed = _get_allowed_models(slug, litellm_key)
    if allowed and requested not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": f"Model '{requested}' is not accessible for this endpoint.",
                "allowed_models": allowed,
            },
        )
    # Local by elimination: cloud was rejected above, and LiteLLM confirmed this
    # model is served by the local fleet.
    return _ModelDecision(requested, "local", requested)


# ---------------------------------------------------------------------------
# Cloud budget gate
# ---------------------------------------------------------------------------


def _gate_cloud_request(ep: dict, decision: _ModelDecision, messages: list,
                        max_tokens: Optional[int]) -> tuple:
    """
    Authorise a cloud call against the funding budget.

    HOD_APPROVAL_ENABLED=True (default-preserving path): gates against the
    endpoint's funding HOD's monthly cap, unchanged from before this flag
    existed.

    HOD_APPROVAL_ENABLED=False (flat mode): skips resolve_endpoint_hod() /
    check_endpoint_budget() / reserve_inflight() (the hod_budget_governor
    path) entirely and gates against the org-wide cap
    (services/org_budget_governor.py) instead — no per-endpoint HOD mapping
    is consulted.

    Returns (hod_email, inflight_token). hod_email is always None in flat
    mode — the caller already treats "no hod_email" as "not chargeable to a
    HOD", which is exactly flat mode's semantics. Raises:
      503 — no HOD mapped (HOD mode only) or lookup unavailable
      429 — cap exhausted

    Also reserves a conservative estimate so concurrent requests see this one's
    pending spend; the caller MUST release it in a finally block.
    """
    if not HOD_APPROVAL_ENABLED:
        from services.endpoint_budget_governor import estimate_request_cost
        from services.org_budget_governor import check_org_endpoint_budget, reserve_org_inflight

        allowed, reason, st = check_org_endpoint_budget()
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error":         reason,
                    "code":          "BUDGET_EXCEEDED",
                    "cap_usd":       st.get("cap_usd"),
                    "consumed_usd":  st.get("consumed_usd"),
                    "remaining_usd": st.get("remaining_usd"),
                    "resets_on":     st.get("resets_on"),
                },
            )
        token = None
        try:
            est   = estimate_request_cost(decision.model, messages, max_tokens)
            token = reserve_org_inflight(est)
        except Exception as exc:
            logger.warning("[endpoint-proxy] org inflight reserve skipped: %s", exc)
        return None, token

    from services.endpoint_budget_governor import (
        check_endpoint_budget, estimate_request_cost, reserve_inflight,
        resolve_endpoint_hod,
    )

    endpoint_id = ep.get("id")
    hod_email   = resolve_endpoint_hod(endpoint_id)

    allowed, reason, st = check_endpoint_budget(hod_email)
    if not allowed:
        # Distinguish "misconfigured" (503, admin must act) from "out of money"
        # (429, retryable next period) so clients can react correctly.
        if not hod_email:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": reason, "code": "NO_BUDGET_OWNER"},
            )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error":         reason,
                "code":          "BUDGET_EXCEEDED",
                "cap_usd":       st.get("cap_usd"),
                "consumed_usd":  st.get("consumed_usd"),
                "remaining_usd": st.get("remaining_usd"),
                "resets_on":     st.get("resets_on"),
            },
        )

    token = None
    if hod_email:
        try:
            est   = estimate_request_cost(decision.model, messages, max_tokens)
            token = reserve_inflight(hod_email, est)
        except Exception as exc:
            # Reservation is a concurrency optimisation, not a correctness gate.
            logger.warning("[endpoint-proxy] inflight reserve skipped: %s", exc)
    return hod_email, token


# ---------------------------------------------------------------------------
# Background: stamp updated_at (fire-and-forget)
# ---------------------------------------------------------------------------


def _stamp_last_used(endpoint_id: str):
    def _do():
        try:
            db = SessionLocal()
            try:
                from datetime import datetime
                ep = db.query(ManagedEndpoint).filter_by(id=endpoint_id).first()
                if ep:
                    ep.updated_at = datetime.utcnow()
                    db.commit()
            finally:
                db.close()
        except Exception:
            pass

    threading.Thread(target=_do, daemon=True).start()

def _record_endpoint_usage(
    *,
    ep: dict,
    model: str,
    messages: list,
    response_text: str,
    latency_ms: int,
) -> None:
    """Fire-and-forget: persist endpoint call as Chat + ChatMessage rows."""
    def _do():
        try:
            db = SessionLocal()
            try:
                system_user_id = ep.get("system_user_id")
                if not system_user_id:
                    return

                user = db.query(User).filter_by(id=system_user_id).first()
                if not user:
                    return

                # Build the user question text from messages
                input_text = "\n".join(
                    m.get("content", "") for m in messages
                    if isinstance(m.get("content"), str)
                )

                from db.models import Chat, ChatMessage
                import uuid as _uuid_mod
                from datetime import datetime as _dt

                chat_id = str(_uuid_mod.uuid4())
                title = (input_text or "Endpoint call")[:80]

                chat = Chat(
                    id=chat_id,
                    user_id=system_user_id,
                    title=title,
                    client_source="endpoint",
                    endpoint_slug=ep.get("slug"),
                    rag_mode="off",
                )
                db.add(chat)

                db.add(ChatMessage(
                    id=str(_uuid_mod.uuid4()),
                    chat_id=chat_id,
                    role="user",
                    content=input_text or "",
                ))
                db.add(ChatMessage(
                    id=str(_uuid_mod.uuid4()),
                    chat_id=chat_id,
                    role="assistant",
                    content=response_text or "",
                    model_used=model,
                    latency=float(latency_ms) / 1000.0 if latency_ms else None,
                ))
                db.commit()
            finally:
                db.close()
        except Exception:
            pass

    threading.Thread(target=_do, daemon=True).start()


# ---------------------------------------------------------------------------
# Token accounting helpers
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """
    chars // 4 — the platform-wide heuristic (gateway_ollama.count_tokens,
    memory.chat_summarizer._count_tokens, ABStudio governance._estimate_tokens).
    There is no tiktoken in the request path anywhere in this codebase.
    """
    return max(1, len(text or "") // 4)


def _resolve_token_counts(usage_in: int, usage_out: int,
                          messages: list, response_text: str) -> tuple:
    """
    Prefer real provider token counts; estimate only what is missing.

    Providers are the source of truth (OpenAI usage chunk, Anthropic
    message_start/message_delta, Gemini usage_metadata). When a count is absent
    we fall back to chars//4 rather than recording 0, so a missing usage chunk
    can never make a paid call look free.
    """
    if not usage_in:
        joined = "\n".join(
            m.get("content", "") for m in (messages or [])
            if isinstance(m, dict) and isinstance(m.get("content"), str)
        )
        usage_in = _estimate_tokens(joined)
    if not usage_out:
        usage_out = _estimate_tokens(response_text) if response_text else 0
    return int(usage_in), int(usage_out)


# ---------------------------------------------------------------------------
# Cost + spend accounting  (model_usages row + HOD ledger increment)
# ---------------------------------------------------------------------------


def _finalize_billing(
    *,
    ep: dict,
    decision: "_ModelDecision",
    hod_email: Optional[str],
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    request_id: Optional[str],
    failed: bool = False,
) -> None:
    """
    Record one request's usage and charge its cost. Fire-and-forget.

    ALWAYS writes a model_usages row (the per-request audit detail) and, for a
    billable cloud call, increments the funding HOD's running endpoint spend.

    Called from a `finally` block so usage is captured even when the client
    disconnects mid-stream — otherwise a caller could abort every request and
    consume inference for free.

    `failed` is an AUDIT/LOGGING label ONLY — it does not zero the cost.
    Billing is always computed from `input_tokens`/`output_tokens`, whatever
    they resolved to. This is a deliberate fix: a prior version of this
    function unconditionally forced cost to $0 whenever `failed=True`, which
    silently discarded genuine, already-incurred provider cost whenever a
    request streamed real content and THEN failed mid-stream (e.g. a network
    drop after the provider had already generated and likely already billed
    partial output) — the platform ate that cost instead of passing it
    through to the HOD cap, and repeated partial-then-fail requests could be
    used to extract free inference. The correct place to represent "no real
    generation happened" is the CALLER resolving output_tokens=0 for that
    case (see _cloud_stream_response / _cloud_non_stream_response, which
    distinguish an in-band gateway error with zero prior output from a
    mid-stream failure after real partial generation) — not a blanket
    override here that cannot tell the two apart.

    KNOWN RISK: this runs on a daemon thread (see the pre-existing
    `_stamp_last_used` / `_record_endpoint_usage` pattern this follows), so a
    process shutdown/deploy that lands between the provider response and this
    thread completing can lose the billing write — spend would go unrecorded for
    that one request. This is the same fire-and-forget pattern already used
    elsewhere in this file; fixing it platform-wide (e.g. a bounded, non-daemon
    executor drained on ASGI shutdown) is a bigger change than this feature's
    scope and is flagged for the module owner rather than attempted here.

    The `model_usages` audit row itself is now produced onto the `ainxt.metrics`
    Kafka topic rather than written to Postgres directly from this thread —
    see the `_do()` body below. That narrows (but does not eliminate) the
    KNOWN RISK above: once `produce()` returns, the event is durable in either
    Kafka or the Redis fallback queue and workers/kafka_consumer.py owns the
    actual Postgres write, so a process shutdown after `produce()` returns no
    longer loses the audit row. The residual risk is now the same for
    `produce()` itself — the daemon thread dying before `produce()` is even
    called (e.g. the process is killed mid-`_do()`).
    """
    def _do():
        try:
            from services.endpoint_model_catalog import estimate_cost_usd

            model = decision.model
            # Always bill from the real resolved token counts — see the
            # docstring above for why `failed` must not zero this.
            cost  = estimate_cost_usd(model, input_tokens, output_tokens)
            slug = ep.get("slug") or "unknown"

            # ── 1. Per-request audit row in model_usages ────────────────────
            #
            # Published onto ainxt.metrics — the same Kafka topic + event
            # shape gateway.py already produces to for /ask and
            # /v1/chat/completions. workers/kafka_consumer._handle_metrics
            # now stores a NULL user_id (an endpoint's system_user_id is
            # nullable) rather than dropping the event, so this row is no
            # longer at risk of silently vanishing the way it used to be
            # before that handler was fixed. Falls back to a Redis-backed
            # queue automatically when Kafka is unreachable (see
            # core.kafka_producer), so the event survives broker downtime.
            try:
                from core.kafka_producer import produce, TOPIC_METRICS
                from core.time_utils import now_ist_iso as _now_ist_iso_ep
                _sent_to_kafka = produce(TOPIC_METRICS, {
                    "event":          "llm_cost",
                    "request_id":     request_id,
                    "user_id":        ep.get("system_user_id"),
                    "model":          model,
                    "input_tokens":   int(input_tokens or 0),
                    "output_tokens":  int(output_tokens or 0),
                    "total_tokens":   int(input_tokens or 0) + int(output_tokens or 0),
                    "latency_ms":     float(latency_ms or 0),
                    "cost_usd":       float(cost),
                    # Fixed prefix keeps existing GROUP BY endpoint dashboards
                    # intact while staying per-endpoint attributable.
                    "endpoint":       f"{ENDPOINT_USAGE_PREFIX}{slug}/v1/chat/completions",
                    "agent_id":       f"endpoint:{slug}",
                    # First-class channel discriminator (model_usages.source_channel),
                    # so channel-wise utilization is a GROUP BY rather than an
                    # inference over endpoint/agent_id.
                    "source_channel": "ENDPOINT",
                    "product_id":     None,
                    "timestamp":      _now_ist_iso_ep(),
                }, key=ep.get("system_user_id"))
                logger.info(
                    "[endpoint-proxy] model_usages produced slug=%s model=%s cost=$%.6f via=%s",
                    slug, model, float(cost), "kafka" if _sent_to_kafka else "redis-fallback",
                )
            except Exception as exc:
                logger.warning(
                    "[endpoint-proxy] model_usages kafka produce FAILED slug=%s: %s", slug, exc
                )

            # ── 2. Charge the funding budget (cloud only) ────────────────────
            # HOD mode: charge the funding HOD's running endpoint spend.
            # Flat mode: charge the org-wide cap instead — no HOD identity.
            if decision.kind == "cloud" and cost > 0:
                if not HOD_APPROVAL_ENABLED:
                    try:
                        from services.org_budget_governor import reserve_org_spend
                        reserve_org_spend(
                            "endpoint_spend", cost,
                            endpoint_id=ep.get("id"),
                        )
                    except Exception as exc:
                        logger.error(
                            "[endpoint-proxy] org spend recording FAILED slug=%s cost=%.6f "
                            "— UNBILLED: %s", slug, float(cost), exc,
                        )
                elif hod_email:
                    try:
                        from services.endpoint_budget_governor import record_endpoint_spend
                        record_endpoint_spend(
                            hod_email,
                            cost,
                            endpoint_slug  = slug,
                            system_user_id = ep.get("system_user_id"),
                        )
                    except Exception as exc:
                        logger.error(
                            "[endpoint-proxy] spend recording FAILED slug=%s cost=%.6f "
                            "— UNBILLED: %s", slug, float(cost), exc,
                        )

            logger.info(
                "[endpoint-proxy] billed slug=%s model=%s kind=%s in=%d out=%d "
                "cost=$%.6f latency=%dms failed=%s",
                slug, model, decision.kind, int(input_tokens or 0),
                int(output_tokens or 0), float(cost), int(latency_ms or 0), failed,
            )
        except Exception as exc:
            logger.error("[endpoint-proxy] _finalize_billing crashed: %s", exc)

    threading.Thread(target=_do, daemon=True).start()


# ---------------------------------------------------------------------------
# Compliance helpers
# ---------------------------------------------------------------------------


def _compliance_check_input(messages: list) -> list:
    """
    Run PCI/PII compliance on the messages list.

    Scanning strategy (mirrors gateway_openai.py):
      - Join all string content fields across turns into one text blob for scanning.
        This catches cross-turn PCI patterns (e.g. a PAN split across messages).
      - Non-string content (image parts, tool result arrays) is excluded from
        scanning — the compliance engine only understands plain text.

    Redaction strategy:
      - Only str content fields are redacted, using literal value replacement
        from the findings (same as gateway_openai.redact()).
      - Non-string content passes through completely untouched.
      - The dict spread {**m, "content": redacted} preserves all other message
        keys (name, tool_call_id, tool_calls, etc.) so tool-use conversations
        are not broken.

    Block behaviour: raises HTTP 400 if any finding type is configured as
    action='block'. Output is never blocked — only redacted.
    """
    try:
        from agents.compliance_engine import compliance_engine
    except Exception:
        logger.warning("[endpoint-proxy] compliance_engine unavailable — skipping input check")
        return messages

    # Step 1: Extract text to scan. When COMPLIANCE_SCAN_HISTORY is OFF (default)
    # scan only the LAST string turn (the current user message); when ON, scan all
    # turns joined. Redaction (Step 3) always runs across every turn regardless.
    from core.config import COMPLIANCE_SCAN_HISTORY
    _str_contents = [m.get("content") for m in messages if isinstance(m.get("content"), str)]
    if COMPLIANCE_SCAN_HISTORY:
        text_to_scan = "\n".join(_str_contents)
    else:
        text_to_scan = _str_contents[-1] if _str_contents else ""

    result = compliance_engine.validate_input(text_to_scan)

    # Step 2: Block check — raises 400 immediately if any block-type finding triggered.
    if result.get("blocked"):
        types = list({f.get("type", "UNKNOWN") for f in result.get("findings", [])})
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Request blocked by compliance policy.", "blocked_types": types},
        )

    # Step 3: In-place redaction — only str content fields, everything else untouched.
    findings = result.get("findings", [])
    clean = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            redacted = content
            for finding in findings:
                v = finding.get("value")
                if v:
                    redacted = redacted.replace(v, "[REDACTED]")
            clean.append({**m, "content": redacted})
        else:
            # List content (image parts, tool results, tool_calls) passes through untouched.
            clean.append(m)
    return clean





# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@proxy_router.get("/{slug}/v1/models", summary="List models accessible to this endpoint")
def endpoint_models(
    slug: str,
    request: Request,
    db: Session = Depends(_get_db),
):
    from core.config import APP_OWNER as _app_owner
    """
    Models this endpoint can actually serve, in OpenAI-compatible format.
    Requires Authorization: Bearer <platform-key>.

    The returned set is exactly what /v1/chat/completions will accept — the list
    is generated from the same rules the request path enforces, so a caller can
    never be advertised a model that would then be rejected with 403:

      * explicit allowlist (model_ids) → those models, each tagged
        owned_by="cloud"|"local" so callers can see which ones cost money.
      * no allowlist → LOCAL models only. Cloud models are reachable solely via
        an admin-curated allowlist (they need a funding HOD), so advertising them
        here would be a lie.
    """
    ep = _resolve_endpoint(slug, db)
    _validate_endpoint_key(request, ep)

    from services.endpoint_model_catalog import is_cloud_model

    allowed_models = ep.get("model_ids") or []
    if allowed_models:
        return {
            "object": "list",
            "data": [
                {
                    "id":       m,
                    "object":   "model",
                    "created":  1700000000,
                    "owned_by": "cloud" if is_cloud_model(m) else "local",
                }
                for m in allowed_models
            ],
        }

    # No allowlist — local-only endpoint. Report just what LiteLLM serves.
    litellm_key = _get_litellm_key(ep)
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": 1700000000, "owned_by": _app_owner}
            for m in _get_allowed_models(slug, litellm_key)
        ],
    }


@proxy_router.post("/{slug}/v1/chat/completions", summary="OpenAI-compatible chat completions proxy")
async def endpoint_chat_completions(
    slug: str,
    request: Request,
    db: Session = Depends(_get_db),
):
    """
    OpenAI-compatible chat completions endpoint for a managed endpoint slug.

    Serves BOTH local (LiteLLM) and cloud (GPT / Claude / Gemini) models. Local
    calls are free and forwarded to LiteLLM with the body intact, so any
    OpenAI-compatible field (tools, tool_choice, response_format, top_p, seed, n,
    stop, ...) passes through without code changes. Cloud calls cost money and are
    therefore gated against the funding HOD's monthly cap and billed per request.

    Only `model`, `messages` and `stream` are inspected internally:
      - model    → resolved against the endpoint's allowlist (see _resolve_model)
      - messages → PCI/PII compliance scan + in-place string redaction
      - stream   → SSE vs JSON response

    Flow:
     1. Read raw JSON body — nothing dropped
     2. Extract model / messages / stream via the minimal _ChatRequest validator
     3. Resolve slug → endpoint config (Redis cache → DB)
     4. Validate Authorization: Bearer <platform-key> (SHA-256 vs user_api_keys)
     4b. Tool-call gating
     5. Select the LiteLLM backend key (use_env_key toggle)
     6. Resolve the serving model — enforces the allowlist and falls back to a
        LOCAL model for unknown names so nothing silently escalates to paid cloud
     7. Compliance check on input (non-bypassable, both paths). Deliberately
        BEFORE the gate: it can raise 400, and a reservation taken first would
        never be released
     8. CLOUD ONLY: HOD budget gate (503 no owner / 429 cap exhausted) plus an
        in-flight reservation so concurrent requests see pending spend
     9. Dispatch — cloud → platform gateway via ModelRouter; local → LiteLLM
    10. Compliance on output (redacted when stream=false; audited when streaming)
    11. Record usage to model_usages and charge the HOD (in a finally block, so
        spend is captured even if the client disconnects mid-stream)

    Returns 403 when the model is not permitted, 429 when the HOD cap is spent,
    503 when a cloud model is requested but no budget owner is configured.
    """
    # ── Step 1: Read raw body — nothing is dropped ────────────────────────────
    try:
        raw_body: dict = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request body must be valid JSON.",
        )

    # ── Step 2: Extract only the fields we need to inspect ────────────────────
    try:
        parsed = _ChatRequest(**raw_body)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid request body: 'model' (string) and 'messages' (array) are required.",
        )

    # ── Step 3: Resolve endpoint ──────────────────────────────────────────────
    ep = _resolve_endpoint(slug, db)

    # ── Step 4: Validate caller's platform API key ────────────────────────────
    _validate_endpoint_key(request, ep)

    # ── Step 4b: Tool call gating ─────────────────────────────────────────────
    if not ep.get("tool_calls_enabled", True):
        if raw_body.get("tools") or raw_body.get("tool_choice"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "Tool calls are disabled for this endpoint."},
            )

    # ── Step 5: Select LiteLLM backend key ───────────────────────────────────
    litellm_key = _get_litellm_key(ep)

    # ── Step 6: Resolve which model actually serves this request ──────────────
    # Enforces the allowlist and never silently escalates an unknown model to
    # paid cloud inference. Raises 403 when nothing may serve it.
    decision = _resolve_model(ep, parsed.model, litellm_key, slug)

    # ── Step 6b: Cloud tool calls require a supported provider ────────────────
    # Cloud tool calls are served via services.cloud_tool_stream.stream_cloud_tools,
    # which talks to one of three internal LLM-proxy endpoints
    # (/llm/{openai,claude,gemini}-tools-stream). All three providers in the
    # platform's cloud catalog are covered today, so this is a defensive guard
    # against a future provider gap — not a live rejection. This check is
    # independent of the endpoint's tool_calls_enabled flag, which only gates
    # the LOCAL/LiteLLM path (Step 4b above).
    wants_tools = bool(raw_body.get("tools") or raw_body.get("tool_choice"))
    if decision.kind == "cloud" and wants_tools:
        from services.endpoint_model_catalog import provider_of
        tool_provider = provider_of(decision.model)
        if tool_provider == "unknown":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": (
                        f"Tool calls are not supported for model '{decision.model}' "
                        f"on this proxy. Use a local model, use a different cloud "
                        f"model, or omit 'tools'/'tool_choice'."
                    ),
                },
            )

    # ── Step 7: Compliance check on input (non-bypassable, both paths) ────────
    #
    # MUST run BEFORE the budget gate. _compliance_check_input raises 400 on a
    # blocked payload; if the gate ran first it would already have taken an
    # in-flight reservation that no handler would ever release, leaving it to
    # expire on its 5-minute TTL. A caller could then loop PCI-triggering
    # payloads to pin a HOD's cap at "exhausted" without spending anything —
    # a denial-of-service on the budget. Compliance is also cheaper and needs
    # no funding owner, so it belongs first regardless.
    clean_messages = _compliance_check_input(raw_body.get("messages", []))

    # ── Step 8: Cloud budget gate (cloud models only) ────────────────────────
    # Local/in-house models are free and ungated. Cloud calls must be funded by
    # the endpoint's HOD: 503 if no owner is configured, 429 if the cap is spent.
    # Everything from here on is guaranteed to reach a handler with a finally
    # block that releases the reservation.
    hod_email      = None
    inflight_token = None
    if decision.kind == "cloud":
        hod_email, inflight_token = _gate_cloud_request(
            ep, decision, clean_messages, raw_body.get("max_tokens"),
        )

    # ── Step 9: Dispatch ─────────────────────────────────────────────────────
    # extra_kwargs = everything from the raw body except the fields we handle
    # explicitly, so tools, tool_choice, response_format, temperature, top_p,
    # seed, n, stop, etc. all pass through without code changes here.
    _HANDLED_KEYS = {"model", "messages", "stream"}
    extra_kwargs  = {k: v for k, v in raw_body.items() if k not in _HANDLED_KEYS}

    endpoint_id = ep.get("id")
    request_id  = str(uuid.uuid4())

    # Tell the caller when we served something other than what they asked for,
    # so they are never misled about which model they were billed for.
    resp_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                    "X-Request-ID": request_id}
    if decision.substituted:
        resp_headers["X-AiNxt-Model-Substituted"] = "true"
        resp_headers["X-AiNxt-Model-Served"]      = decision.model

    if decision.kind == "cloud":
        # Cloud models are served by the platform gateway (via ModelRouter for
        # plain text, or services.cloud_tool_stream when tools are present —
        # ModelRouter has no tools parameter) — never LiteLLM.
        tools_payload = raw_body.get("tools") if wants_tools else None
        tool_choice_payload = raw_body.get("tool_choice") if wants_tools else None

        if parsed.stream is not False:
            return StreamingResponse(
                _cloud_stream_response(
                    clean_messages, decision, ep, hod_email, inflight_token,
                    extra_kwargs, request_id,
                    tools=tools_payload, tool_choice=tool_choice_payload,
                ),
                media_type="text/event-stream",
                headers=resp_headers,
            )
        # _cloud_non_stream_response makes a BLOCKING HTTP call to a real cloud
        # provider (tens of seconds for a large Opus/GPT response). Unlike the
        # local LiteLLM path (LAN-local, effectively instant), a synchronous call
        # here would freeze the entire uvicorn worker's event loop for the whole
        # duration, stalling every other in-flight request on that worker.
        from fastapi.concurrency import run_in_threadpool
        return await run_in_threadpool(
            _cloud_non_stream_response,
            clean_messages, decision, ep, hod_email, inflight_token,
            extra_kwargs, request_id,
            tools=tools_payload, tool_choice=tool_choice_payload,
        )

    # ── Local path — unchanged LiteLLM passthrough ────────────────────────────
    if parsed.stream is not False:
        return StreamingResponse(
            _stream_response(
                clean_messages, decision.model, litellm_key, endpoint_id,
                extra_kwargs, ep, decision, request_id,
            ),
            media_type="text/event-stream",
            headers=resp_headers,
        )
    return _non_stream_response(
        clean_messages, decision.model, litellm_key, endpoint_id,
        extra_kwargs, ep, decision, request_id,
    )


# ---------------------------------------------------------------------------
# Streaming generator
# ---------------------------------------------------------------------------


def _stream_response(
    messages: list,
    model: str,
    litellm_key: str,
    endpoint_id: str,
    extra_kwargs: dict,
    ep: dict,
    decision: Optional["_ModelDecision"] = None,
    request_id: Optional[str] = None,
):
    if not _LITELLM_BASE_URL:
        yield f"data: {json.dumps({'error': 'LITELLM_BASE_URL not configured'})}\n\n"
        yield "data: [DONE]\n\n"
        return

    full_response = []
    start_ts = time.time()
    # Token counts for the model_usages audit row. LiteLLM only emits a usage
    # chunk when the caller asked for one, so these stay 0 unless it does; the
    # finally block falls back to an estimate. Local models cost $0 regardless.
    usage_in  = 0
    usage_out = 0
    failed    = False

    try:
        from openai import OpenAI
        client = OpenAI(base_url=f"{_LITELLM_BASE_URL}/v1", api_key=litellm_key)

        # Spread extra_kwargs last so callers can pass tools, tool_choice,
        # response_format, top_p, seed, etc. without any code changes here.
        kwargs: dict = {"model": model, "messages": messages, "stream": True, **extra_kwargs}

        stream = client.chat.completions.create(**kwargs)

        for chunk in stream:
            # Serialize the full chunk to a plain dict — this transparently
            # forwards delta.content, delta.tool_calls, finish_reason, and any
            # future OpenAI streaming fields without field-by-field handling.
            chunk_dict = (
                chunk.model_dump()
                if hasattr(chunk, "model_dump")
                else chunk.dict()
            )
            yield f"data: {json.dumps(chunk_dict)}\n\n"

            # Capture real token counts if LiteLLM sends a usage chunk (it does
            # when the caller passes stream_options={"include_usage": true}).
            _u = chunk_dict.get("usage") or {}
            if _u:
                usage_in  = _u.get("prompt_tokens")     or usage_in
                usage_out = _u.get("completion_tokens") or usage_out

            # Accumulate text tokens for post-stream compliance scan only.
            # Tool call argument JSON is intentionally not scanned.
            if chunk.choices and chunk.choices[0].delta.content:
                full_response.append(chunk.choices[0].delta.content)

        full_text = "".join(full_response)

        yield "data: [DONE]\n\n"

    except Exception as exc:
        failed = True
        logger.error(f"[endpoint-proxy] Streaming error model={model} endpoint={endpoint_id}: {exc}")
        yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'server_error'}})}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        latency_ms = int((time.time() - start_ts) * 1000)
        _text = "".join(full_response)
        if endpoint_id:
            _stamp_last_used(endpoint_id)
        if ep:
            _record_endpoint_usage(
                ep=ep,
                model=model,
                messages=messages,
                response_text=_text,
                latency_ms=latency_ms,
            )
            # Per-request audit row. Local models are $0, so an estimated token
            # count is sufficient here — it never affects money, only reporting.
            _in, _out = _resolve_token_counts(usage_in, usage_out, messages, _text)
            _finalize_billing(
                ep=ep,
                decision=decision or _ModelDecision(model, "local", model),
                hod_email=None,          # local models are never charged
                input_tokens=_in,
                output_tokens=_out,
                latency_ms=latency_ms,
                request_id=request_id,
                failed=failed,
            )


# ---------------------------------------------------------------------------
# Non-streaming response
# ---------------------------------------------------------------------------


def _non_stream_response(
    messages: list,
    model: str,
    litellm_key: str,
    endpoint_id: str,
    extra_kwargs: dict,
    ep: dict,
    decision: Optional["_ModelDecision"] = None,
    request_id: Optional[str] = None,
) -> JSONResponse:
    if not _LITELLM_BASE_URL:
        raise HTTPException(status_code=503, detail="LITELLM_BASE_URL not configured.")

    start_ts = time.time()
    raw_dict = {}
    failed   = False

    try:
        from openai import OpenAI
        client = OpenAI(base_url=f"{_LITELLM_BASE_URL}/v1", api_key=litellm_key)

        # Spread extra_kwargs last so callers can pass tools, tool_choice,
        # response_format, top_p, seed, etc. without any code changes here.
        kwargs: dict = {"model": model, "messages": messages, "stream": False, **extra_kwargs}

        response = client.chat.completions.create(**kwargs)

        # Serialize the full LiteLLM response to a plain dict — this preserves
        # tool_calls, finish_reason, logprobs, refusal, usage, and any future
        # OpenAI fields without requiring code changes here.
        raw_dict = (
            response.model_dump()
            if hasattr(response, "model_dump")
            else response.dict()
        )

    except Exception as exc:
        failed = True
        logger.error(f"[endpoint-proxy] Non-streaming error model={model}: {exc}")
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}")
    finally:
        latency_ms = int((time.time() - start_ts) * 1000)
        if endpoint_id:
            _stamp_last_used(endpoint_id)
        if ep:
            text_content = (raw_dict.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            _record_endpoint_usage(
                ep=ep,
                model=model,
                messages=messages,
                response_text=text_content,
                latency_ms=latency_ms,
            )
            # Non-streaming LiteLLM responses always carry a usage block.
            _u   = raw_dict.get("usage") or {}
            _in, _out = _resolve_token_counts(
                _u.get("prompt_tokens") or 0,
                _u.get("completion_tokens") or 0,
                messages, text_content,
            )
            _finalize_billing(
                ep=ep,
                decision=decision or _ModelDecision(model, "local", model),
                hod_email=None,          # local models are never charged
                input_tokens=_in,
                output_tokens=_out,
                latency_ms=latency_ms,
                request_id=request_id,
                failed=failed,
            )

    return JSONResponse(content=raw_dict)


# ---------------------------------------------------------------------------
# CLOUD PATH — served by the platform gateway (ModelRouter), not LiteLLM
#
# Cloud models are not in LiteLLM's catalog, so these handlers call
# models.model_router.model_router.stream() and translate its output into the
# OpenAI wire format the caller expects.
#
# Why ModelRouter and not the gateway singletons (openai_gateway, claude_gateway,
# gemini_gateway) directly:
#   * ModelRouter transparently swaps in _ProxyGateway when LLM_PROXY_URL is set.
#     Production app servers have NO outbound internet, so calling the singletons
#     directly would fail there.
#   * Circuit breakers (_CB_OPENAI / _CB_CLAUDE / _CB_GEMINI) are honoured.
#
# Token counts come from the {"__stream_meta__": {...}} sentinel yielded at the
# end of stream(). We must NOT read model_router.last_input_tokens after the loop:
# under StreamingResponse the generator is driven by anyio.iterate_in_threadpool,
# which can resume each next() on a different worker thread, so the
# threading.local-backed counters read as 0. See models/model_router.py:2383.
# ---------------------------------------------------------------------------

# In-band error strings the gateways yield instead of raising. Billing these
# would charge callers for our own failures.
_GATEWAY_ERROR_PREFIXES = (
    "Error generating response",
    "Error: no gateway available",
    "Error: unknown routing tier",
    "Request blocked due to PCI violation",
)


def _is_gateway_error(text: str) -> bool:
    t = (text or "").strip()
    return any(t.startswith(p) for p in _GATEWAY_ERROR_PREFIXES)


def _model_hint_for(model: str) -> str:
    """
    Map a concrete cloud model id to the routing hint ModelRouter expects.

    Passing the bare model id works for most models because _HINT_MAP is keyed by
    the registry constants themselves (models/model_router.py:547), so
    "claude-opus-5", "gpt-5.4", "gemini-3.5-flash" etc. all resolve directly.
    """
    return (model or "").strip()


def _cloud_stream_response(
    messages: list,
    decision: "_ModelDecision",
    ep: dict,
    hod_email: Optional[str],
    inflight_token: Optional[str],
    extra_kwargs: dict,
    request_id: str,
    tools: Optional[list] = None,
    tool_choice=None,
):
    """
    SSE generator for a cloud model, in OpenAI chat.completion.chunk format.

    Two dispatch paths, sharing the same billing/gating wrapper below:
      - `tools` present  -> services.cloud_tool_stream.stream_cloud_tools
        (the shared client also used by gateway.py's refactored IDE route)
      - plain text        -> models.model_router.stream (unchanged)

    KNOWN LIMITATION (plain-text path only): `extra_kwargs` (max_tokens,
    temperature, response_format, stop, ...) is accepted for interface
    symmetry with the local path but is NOT forwarded to
    models.model_router.stream() — that shared, heavily-used router has no
    seam for per-call generation parameters, and extending it is out of scope
    here. Practical effect: a caller's `max_tokens` is NOT enforced on the
    plain-text cloud path, so `estimate_request_cost`'s reservation (sized
    from max_tokens) is a lower bound, not a hard ceiling. The tool-call path
    DOES forward `max_tokens` (services.cloud_tool_stream.stream_cloud_tools
    accepts it directly), since that HTTP contract already supports it.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created_ts    = int(time.time())
    model         = decision.model

    chunks: List[str] = []
    meta: dict        = {}
    start_ts          = time.time()
    failed            = False
    # Text that should actually be BILLED. Starts equal to whatever was
    # streamed; set to "" the moment we determine no real generation happened
    # (an in-band gateway error with nothing before it — scenario 7a), so
    # _resolve_token_counts never word-counts an error message as if it were
    # real output. Left as real accumulated text when a failure occurs AFTER
    # genuine partial generation (scenario 7b), so that partial usage is
    # billed and gated correctly instead of being discarded. For the tool-call
    # path, "text" also includes accumulated tool-call argument fragments —
    # a tool-call-only response with zero prose is not a $0 response.
    billable_text     = ""

    def _chunk(delta: dict, finish=None) -> str:
        return "data: " + json.dumps({
            "id":      completion_id,
            "object":  "chat.completion.chunk",
            "created": created_ts,
            "model":   model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }) + "\n\n"

    try:
        # Opening role delta so clients know the speaker before the first token.
        yield _chunk({"role": "assistant", "content": ""})

        if tools:
            from services.cloud_tool_stream import stream_cloud_tools
            from services.endpoint_model_catalog import provider_of

            provider = provider_of(model)
            for tok in stream_cloud_tools(
                messages, tools, tool_choice, model, provider,
                max_tokens=extra_kwargs.get("max_tokens") or 8000,
                request_id=request_id,
            ):
                if isinstance(tok, dict):
                    if "__stream_meta__" in tok:
                        meta = tok["__stream_meta__"] or {}
                        continue
                    if "tool_call_delta" in tok:
                        # Accumulate only the real argument TEXT (never the
                        # dict's id/type/function wrapper) — this is what a
                        # fallback token estimate must be computed from if the
                        # provider's usage sentinel is ever missing. A
                        # tool-call-only response with zero prose must not
                        # look like $0 just because `chunks` stayed empty.
                        args = (tok["tool_call_delta"].get("function") or {}).get("arguments")
                        if args:
                            chunks.append(args)
                        yield _chunk({"tool_calls": [tok["tool_call_delta"]]})
                    continue
                if not isinstance(tok, str) or not tok:
                    continue
                chunks.append(tok)
                yield _chunk({"content": tok})
        else:
            from models.model_router import model_router

            for tok in model_router.stream(
                messages,
                model_hint=_model_hint_for(model),
                # Compliance already ran on the input in the request handler; tell
                # the gateway so it does not double-block, and re-use its findings.
                precleared=True,
            ):
                # The final sentinel carries the real token counts as DATA.
                if isinstance(tok, dict):
                    if "__stream_meta__" in tok:
                        meta = tok["__stream_meta__"] or {}
                    continue
                # Gateways may also yield ReasoningMarker objects — skip non-strings.
                if not isinstance(tok, str) or not tok:
                    continue
                chunks.append(tok)
                yield _chunk({"content": tok})

        full_text     = "".join(chunks)
        billable_text = full_text

        # The gateways signal failure in-band rather than raising. When the
        # WHOLE response is an in-band error (no real generation happened —
        # scenario 7a), billable_text must be cleared so _resolve_token_counts
        # resolves out_tok=0, NOT a word-count of the error message itself
        # (which would otherwise get billed as if it were real output, now
        # that _finalize_billing no longer blindly zeroes on `failed`).
        if _is_gateway_error(full_text):
            failed = True
            billable_text = ""
            logger.error(
                "[endpoint-proxy] cloud gateway error slug=%s model=%s: %s",
                ep.get("slug"), model, full_text[:200],
            )

        # Real provider finish_reason when available (tool calls need
        # "tool_calls", not "stop", so IDE-side agent loops know to execute
        # tools rather than treat the turn as final text).
        yield _chunk({}, finish=meta.get("finish_reason") or "stop")

        # Final usage chunk (OpenAI streaming spec: empty choices + usage), so
        # IDE clients can update their token counters.
        _in, _out = _resolve_token_counts(
            meta.get("in_tok") or 0, meta.get("out_tok") or 0, messages, billable_text,
        )
        yield "data: " + json.dumps({
            "id":      completion_id,
            "object":  "chat.completion.chunk",
            "created": created_ts,
            "model":   model,
            "choices": [],
            "usage": {
                "prompt_tokens":     _in,
                "completion_tokens": _out,
                "total_tokens":      _in + _out,
            },
        }) + "\n\n"
        yield "data: [DONE]\n\n"

    except Exception as exc:
        failed = True
        # billable_text was set to the real accumulated chunks just before this
        # block (or stays "" if the exception happened before any content
        # streamed) — either way it is REAL text, never the synthetic message
        # below, which is shown to the caller but must not be billed as output.
        billable_text = "".join(chunks)
        logger.error(
            "[endpoint-proxy] cloud streaming error slug=%s model=%s: %s",
            ep.get("slug"), model, exc,
        )
        yield _chunk({"content": "\nError generating response"}, finish="stop")
        yield "data: [DONE]\n\n"

    finally:
        latency_ms = int((time.time() - start_ts) * 1000)
        full_text  = "".join(chunks)
        if ep.get("id"):
            _stamp_last_used(ep["id"])

        # Bill from the model the router ACTUALLY used. On a provider failure it
        # silently substitutes a different (billable) model and marks the label
        # " [fallback]" — billing the caller's requested model would be wrong.
        served = meta.get("model_label") or model
        bill   = _ModelDecision(served, decision.kind, decision.requested,
                                decision.substituted)

        # Audit row shows the full text actually produced/shown, including any
        # synthetic error message — this is a display/audit record, not the
        # billing input.
        _record_endpoint_usage(
            ep=ep, model=served, messages=messages,
            response_text=full_text, latency_ms=latency_ms,
        )
        # Billing uses billable_text: real generation only (see comments above
        # on the two failure branches for exactly what it holds in each case).
        _in, _out = _resolve_token_counts(
            meta.get("in_tok") or 0, meta.get("out_tok") or 0, messages, billable_text,
        )
        _finalize_billing(
            ep=ep, decision=bill, hod_email=hod_email,
            input_tokens=_in, output_tokens=_out,
            latency_ms=latency_ms, request_id=request_id, failed=failed,
        )
        # Release the reservation AFTER recording actual spend, so the cap never
        # momentarily reads as free between the two.
        _release_inflight(hod_email, inflight_token)


def _assemble_tool_calls(fragments: Dict[int, dict]) -> List[dict]:
    """
    Reconstruct complete OpenAI `tool_calls` entries from accumulated
    incremental deltas, keyed by index (the first fragment for an index
    carries id/type/function.name; continuations carry only
    function.arguments, which are concatenated in arrival order).
    """
    out = []
    for idx in sorted(fragments.keys()):
        f = fragments[idx]
        out.append({
            "id":       f.get("id") or f"call_{idx}",
            "type":     f.get("type") or "function",
            "function": {
                "name":      f.get("name", ""),
                "arguments": "".join(f.get("arg_parts", [])),
            },
        })
    return out


def _cloud_non_stream_response(
    messages: list,
    decision: "_ModelDecision",
    ep: dict,
    hod_email: Optional[str],
    inflight_token: Optional[str],
    extra_kwargs: dict,
    request_id: str,
    tools: Optional[list] = None,
    tool_choice=None,
) -> JSONResponse:
    """
    Non-streaming cloud completion in OpenAI chat.completion format.

    When `tools` is present, drains services.cloud_tool_stream.stream_cloud_tools
    instead of models.model_router.stream() and assembles the accumulated
    tool_call_delta fragments into a final `message.tool_calls` array — the
    non-streaming twin of what _cloud_stream_response does chunk-by-chunk.
    """
    model      = decision.model
    start_ts   = time.time()
    chunks: List[str] = []
    tool_fragments: Dict[int, dict] = {}
    meta: dict = {}
    failed     = False
    # Real generation only — see the streaming twin (_cloud_stream_response)
    # for the full rationale. Empty when the whole response was an in-band
    # gateway error (no real output), equal to the drained partial content
    # when a Python exception interrupted a stream that had already produced
    # real tokens — that partial usage must still be billed, not discarded.
    # For the tool-call path this also includes accumulated tool-call
    # argument text via `chunks` (see the tool_call_delta branch below).
    billable_text = ""

    try:
        if tools:
            from services.cloud_tool_stream import stream_cloud_tools
            from services.endpoint_model_catalog import provider_of

            provider = provider_of(model)
            for tok in stream_cloud_tools(
                messages, tools, tool_choice, model, provider,
                max_tokens=extra_kwargs.get("max_tokens") or 8000,
                request_id=request_id,
            ):
                if isinstance(tok, dict):
                    if "__stream_meta__" in tok:
                        meta = tok["__stream_meta__"] or {}
                        continue
                    if "tool_call_delta" in tok:
                        d   = tok["tool_call_delta"]
                        idx = d.get("index", 0)
                        frag = tool_fragments.setdefault(idx, {"arg_parts": []})
                        if d.get("id"):
                            frag["id"] = d["id"]
                        if d.get("type"):
                            frag["type"] = d["type"]
                        fn = d.get("function") or {}
                        if fn.get("name"):
                            frag["name"] = fn["name"]
                        if fn.get("arguments"):
                            frag["arg_parts"].append(fn["arguments"])
                            chunks.append(fn["arguments"])
                    continue
                if isinstance(tok, str) and tok:
                    chunks.append(tok)
        else:
            from models.model_router import model_router
            for tok in model_router.stream(
                messages, model_hint=_model_hint_for(model), precleared=True,
            ):
                if isinstance(tok, dict):
                    if "__stream_meta__" in tok:
                        meta = tok["__stream_meta__"] or {}
                    continue
                if isinstance(tok, str) and tok:
                    chunks.append(tok)

        full_text     = "".join(chunks)
        billable_text = full_text
        if _is_gateway_error(full_text):
            failed = True
            # The ENTIRE response is the error message (no real content
            # preceded it — _is_gateway_error matches on startswith) — clear
            # billable_text so no output tokens are charged for a failure.
            billable_text = ""
            raise HTTPException(status_code=502, detail=f"LLM call failed: {full_text[:200]}")

    except HTTPException:
        raise
    except Exception as exc:
        failed = True
        # Whatever was already appended to `chunks` before this exception is
        # REAL generated content — keep it billable, don't discard it.
        billable_text = "".join(chunks)
        logger.error(
            "[endpoint-proxy] cloud non-streaming error slug=%s model=%s: %s",
            ep.get("slug"), model, exc,
        )
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}")

    finally:
        latency_ms = int((time.time() - start_ts) * 1000)
        full_text  = "".join(chunks)
        if ep.get("id"):
            _stamp_last_used(ep["id"])

        served = meta.get("model_label") or model
        bill   = _ModelDecision(served, decision.kind, decision.requested,
                                decision.substituted)

        # Audit row shows the full text actually produced (including any error
        # content that was part of the drained stream) — display/audit only.
        _record_endpoint_usage(
            ep=ep, model=served, messages=messages,
            response_text=full_text, latency_ms=latency_ms,
        )
        # Billing uses billable_text — real generation only.
        _in, _out = _resolve_token_counts(
            meta.get("in_tok") or 0, meta.get("out_tok") or 0, messages, billable_text,
        )
        _finalize_billing(
            ep=ep, decision=bill, hod_email=hod_email,
            input_tokens=_in, output_tokens=_out,
            latency_ms=latency_ms, request_id=request_id, failed=failed,
        )
        _release_inflight(hod_email, inflight_token)

    tool_calls = _assemble_tool_calls(tool_fragments) if tool_fragments else None

    _in, _out  = _resolve_token_counts(
        meta.get("in_tok") or 0, meta.get("out_tok") or 0, messages, full_text,
    )

    message: dict = {"role": "assistant", "content": None if tool_calls else full_text}
    if tool_calls:
        message["tool_calls"] = tool_calls

    return JSONResponse(content={
        "id":      f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   model,
        "choices": [{
            "index":         0,
            "message":       message,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": {
            "prompt_tokens":     _in,
            "completion_tokens": _out,
            "total_tokens":      _in + _out,
        },
    })


def _release_inflight(hod_email: Optional[str], token: Optional[str]) -> None:
    """Release a budget reservation. Never raises — best-effort cleanup.

    Flat mode (HOD_APPROVAL_ENABLED=False): hod_email is always None (see
    _gate_cloud_request) but a token may still exist from
    org_budget_governor.reserve_org_inflight() — release it there instead.
    """
    if not token:
        return
    try:
        if not HOD_APPROVAL_ENABLED:
            from services.org_budget_governor import release_org_inflight
            release_org_inflight(token)
            return
        if not hod_email:
            return
        from services.endpoint_budget_governor import release_inflight
        release_inflight(hod_email, token)
    except Exception as exc:
        logger.warning("[endpoint-proxy] inflight release failed: %s", exc)
