# SPDX-License-Identifier: Apache-2.0
"""Trigger/Routine scheduling and execution history endpoints."""
import asyncio
import hashlib
import hmac
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from app.models import TriggerCreate, TriggerUpdate, TriggerOut, TriggerExecutionOut, AuthenticatedUser
from app.core import workflow_repo
from app.services import trigger_scheduler
from app.api.deps import require_access
from core.logger import logger

router = APIRouter()


# ---------------------------------------------------------------------------
# FR-T0-3 (REQ-T2/T4) — signed webhook / event ingestion
# ---------------------------------------------------------------------------
# In-process token-bucket-ish rate limiter, keyed per trigger. Simple and
# dependency-free — bounds burst so a misbehaving/hostile source can't drive
# unbounded runs. On overload we return 503 (back-pressure) rather than queue.
_WEBHOOK_RATE_WINDOW_S    = 60.0
_WEBHOOK_RATE_MAX         = 30           # max fires per trigger per window
# C7: cap the in-process hit dict so deleted/rotated trigger IDs do not
# accumulate forever. When the cap is hit, evict the oldest entry (FIFO).
_WEBHOOK_RATE_MAX_TRIGGERS = 1000
_webhook_hits: dict[str, list[float]] = {}


def _rate_limited(trigger_id: str) -> bool:
    now = time.monotonic()
    hits = [t for t in _webhook_hits.get(trigger_id, []) if now - t < _WEBHOOK_RATE_WINDOW_S]
    if len(hits) >= _WEBHOOK_RATE_MAX:
        _webhook_hits[trigger_id] = hits
        return True
    hits.append(now)
    _webhook_hits[trigger_id] = hits
    # Evict oldest entry when the dict grows beyond the cap so deleted/rotated
    # trigger IDs do not accumulate unboundedly in long-running processes.
    if len(_webhook_hits) > _WEBHOOK_RATE_MAX_TRIGGERS:
        try:
            _webhook_hits.pop(next(iter(_webhook_hits)))
        except StopIteration:
            pass
    return False


def _verify_signature(secret: str, raw_body: bytes, provided: str) -> bool:
    """Constant-time HMAC-SHA256 verification. ``provided`` may be the bare
    hex digest or the GitHub/GitLab-style ``sha256=<hex>`` form.
    """
    if not secret or not provided:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    candidate = provided.split("=", 1)[1] if "=" in provided else provided
    return hmac.compare_digest(expected, candidate.strip())


# S2 fix: the webhook HMAC secret must not be persisted plaintext in the
# schedule JSONB. We encrypt it at rest with the platform-standard Fernet vault
# (store.credential_vault, keyed by FERNET_KEY) and decrypt only in-memory at
# verification time. The ciphertext is stored under the ``secret_enc`` key so we
# can unambiguously distinguish encrypted values from legacy plaintext ``secret``
# values written before this change.

def _encrypt_schedule_secret(schedule: dict) -> dict:
    """Return a copy of *schedule* with any plaintext webhook ``secret``
    replaced by an encrypted ``secret_enc`` Fernet token. Idempotent: an
    already-encrypted schedule (only ``secret_enc`` present) is returned as-is.
    A falsy/empty secret is left untouched.
    """
    if not isinstance(schedule, dict):
        return schedule
    secret = schedule.get("secret")
    if not secret:
        return schedule
    from store.credential_vault import encrypt_value
    out = dict(schedule)
    out.pop("secret", None)
    out["secret_enc"] = encrypt_value(secret)
    return out


def _resolve_schedule_secret(schedule: dict) -> str:
    """Return the plaintext webhook secret for verification.

    Prefers the encrypted ``secret_enc`` token (decrypted in-memory only);
    falls back to a legacy plaintext ``secret`` for rows written before the
    S2 encryption change. Returns "" if neither is present or decryption fails
    (the caller treats an empty secret as a failed verification).
    """
    if not isinstance(schedule, dict):
        return ""
    enc = schedule.get("secret_enc")
    if enc:
        try:
            from store.credential_vault import decrypt_value
            return decrypt_value(enc)
        except Exception:
            # Never log the token/secret. A decryption failure (tampered token,
            # wrong/rotated key) yields an empty secret → 401 downstream.
            logger.warning("[AGENT] webhook secret decryption failed")
            return ""
    # Legacy plaintext fallback (pre-S2 rows). Kept for backward compatibility.
    return schedule.get("secret") or ""


def _event_matches(schedule: dict, payload: dict, headers) -> bool:
    """For 'event' triggers, drop non-matching events silently (REQ-T2).

    Matches the configured event_source/event_type against provider-specific
    signals. Jira: body ``webhookEvent`` (e.g. 'jira:issue_created'); GitLab:
    ``X-Gitlab-Event`` header / body ``object_kind``.
    """
    def _norm(s: str) -> str:
        # Normalize separators so "merge_request", "Merge Request Hook" and
        # "merge-request" all compare equal on their tokens.
        return " ".join(str(s).lower().replace("_", " ").replace("-", " ").split())

    want_type = _norm(schedule.get("event_type") or "")
    if not want_type:
        return True  # no filter configured — accept any event
    source = (schedule.get("event_source") or "").lower()
    if source == "jira":
        got = _norm(payload.get("webhookEvent") or payload.get("issue_event_type_name") or "")
        return want_type in got
    if source == "gitlab":
        # GitLab sends "Merge Request Hook" in the header and "merge_request"
        # in object_kind — normalize and drop a trailing "hook" token.
        got_hdr = _norm(headers.get("X-Gitlab-Event") or "")
        if got_hdr.endswith(" hook"):
            got_hdr = got_hdr[: -len(" hook")]
        got_body = _norm(payload.get("object_kind") or "")
        return want_type in got_hdr or want_type in got_body
    # Generic: accept an explicit event_type field in the body.
    got = _norm(payload.get("event_type") or payload.get("type") or "")
    return (not got) or (want_type in got)


def _trigger_to_out(trigger: dict) -> dict:
    out = dict(trigger)
    out.pop("owner_user_id", None)
    # FR-T0-3: never leak the webhook HMAC secret back to clients (write-only).
    # Scrub both the legacy plaintext ``secret`` and the encrypted ``secret_enc``
    # token so neither the secret nor its ciphertext is ever emitted.
    sched = out.get("schedule")
    if isinstance(sched, dict) and ("secret" in sched or "secret_enc" in sched):
        sched = dict(sched)
        if "secret" in sched:
            sched["secret"] = None
        if "secret_enc" in sched:
            sched.pop("secret_enc", None)
        out["schedule"] = sched
    if not out.get("next_run_at"):
        nr = trigger_scheduler.get_next_run(trigger["id"])
        if nr:
            out["next_run_at"] = nr.isoformat()
    return out


@router.get("/triggers/config")
async def triggers_config():
    """Return trigger feature flags driven by the root .env.

    Called once by the frontend on load (triggersStore.loadConfig).
    No auth required — returns only boolean flags, no sensitive data.

    Re-reads the on-disk .env before consulting os.environ so a flag flipped
    at runtime (development-mode env toggles) is picked up without needing a
    full process restart. The gateway's uvicorn --reload watches .py files
    only, not .env, so the initial load_dotenv() would otherwise pin the
    value at process boot.
    """
    import os
    try:
        # Best-effort: reload the same .env the gateway loaded at boot.
        from dotenv import load_dotenv
        _env_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..", ".env",
        )
        load_dotenv(dotenv_path=os.path.abspath(_env_path), override=True)
    except Exception:
        pass
    enabled = os.getenv("ABSTUDIO_AGENT_TRIGGERS_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    return {"agent_triggers_enabled": enabled}


@router.get("/triggers")
async def list_triggers_route(
    target_kind: Optional[str] = None,
    target_id: Optional[str] = None,
    node_id: Optional[str] = None,
    node_scope: str = "any",
    current_user: AuthenticatedUser = Depends(require_access),
):
    rows = await workflow_repo.list_triggers(
        current_user.id, target_kind, target_id,
        node_id=node_id, node_scope=node_scope,
    )
    return [_trigger_to_out(r) for r in rows]


@router.post("/triggers", status_code=201)
async def create_trigger_route(
    data: TriggerCreate,
    current_user: AuthenticatedUser = Depends(require_access),
):
    # Sanity check the target exists and belongs to this user.
    if data.target_kind == "workflow":
        target = await workflow_repo.get_workflow(data.target_id, current_user.id)
        if not target:
            raise HTTPException(status_code=404, detail="Workflow not found")
    elif data.target_kind == "agent":
        target = await workflow_repo.get_agent(data.target_id, current_user.id) \
                or await workflow_repo.get_agent_by_id(data.target_id)
        if not target:
            raise HTTPException(status_code=404, detail="Agent not found")
    else:
        raise HTTPException(status_code=400, detail="Invalid target_kind")

    payload = {
        "target_kind": data.target_kind.value if hasattr(data.target_kind, "value") else data.target_kind,
        "target_id":   data.target_id,
        "node_id":     data.node_id or None,
        "name":        data.name or "",
        # S2: encrypt the webhook HMAC secret at rest before persisting.
        "schedule":    _encrypt_schedule_secret(data.schedule.model_dump(exclude_none=False)),
        "input_text":  data.input_text or "",
        "enabled":     data.enabled,
    }
    trigger = await workflow_repo.create_trigger(payload, current_user.id)
    if trigger.get("enabled"):
        next_run = trigger_scheduler.register_trigger(trigger)
        if next_run is not None:
            await workflow_repo.update_trigger_run_metadata(
                trigger["id"], next_run_at=next_run,
            )
            trigger["next_run_at"] = next_run.isoformat()
    return _trigger_to_out(trigger)


@router.get("/triggers/{trigger_id}")
async def get_trigger_route(
    trigger_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    trigger = await workflow_repo.get_trigger(trigger_id, current_user.id)
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")
    return _trigger_to_out(trigger)


@router.put("/triggers/{trigger_id}")
async def update_trigger_route(
    trigger_id: str,
    data: TriggerUpdate,
    current_user: AuthenticatedUser = Depends(require_access),
):
    payload = data.model_dump(exclude_none=True)
    if "schedule" in payload and payload["schedule"] is not None:
        # S2: encrypt the webhook HMAC secret at rest before persisting.
        payload["schedule"] = _encrypt_schedule_secret(payload["schedule"])
    trigger = await workflow_repo.update_trigger(trigger_id, payload, current_user.id)
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")
    next_run = trigger_scheduler.reschedule_trigger(trigger)
    if next_run is not None:
        await workflow_repo.update_trigger_run_metadata(
            trigger["id"], next_run_at=next_run,
        )
        trigger["next_run_at"] = next_run.isoformat()
    else:
        # Positively clear next_run_at in the DB whenever the trigger was
        # disabled, or its schedule became unfireable (e.g. a past ``once``
        # run_at, malformed cron, event-driven trigger). Without this the DB
        # row keeps the previous future timestamp, the UI shows a stale
        # "Next run", and a subsequent enable-toggle would let the dispatcher
        # fire immediately at that past time.
        await workflow_repo.update_trigger_run_metadata(
            trigger["id"], clear_next_run_at=True,
        )
        trigger["next_run_at"] = None
    return _trigger_to_out(trigger)


@router.delete("/triggers/{trigger_id}", status_code=204)
async def delete_trigger_route(
    trigger_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    trigger_scheduler.deregister_trigger(trigger_id)
    deleted = await workflow_repo.delete_trigger(trigger_id, current_user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Trigger not found")


@router.post("/triggers/{trigger_id}/webhook", status_code=202)
async def trigger_webhook_route(trigger_id: str, request: Request):
    """FR-T0-3 (REQ-T2): signed webhook / event ingestion.

    This is the ONLY unauthenticated trigger route — external systems (Jira,
    GitLab, …) cannot present a JWT. It is secured instead by an HMAC-SHA256
    signature over the raw body using the per-trigger ``secret``. Flow:

        verify signature → match event type → compliance (C4) + injection (PI2)
        gate runs inside _fire_trigger → durable run (D1..D5).

    Non-matching events are dropped with 200 so the provider stops retrying.
    Bad signatures are rejected 401. Overload returns 503 (back-pressure).
    """
    trigger = await workflow_repo.get_trigger_by_id(trigger_id)
    if not trigger:
        # Do not leak existence — generic 404.
        raise HTTPException(status_code=404, detail="Not found")

    schedule = trigger.get("schedule") or {}
    if (schedule.get("type") or "").lower() not in ("webhook", "event"):
        raise HTTPException(status_code=404, detail="Not found")

    # REQ-T4: honour per-trigger enable/disable.
    if not trigger.get("enabled"):
        raise HTTPException(status_code=403, detail="Trigger disabled")

    raw_body = await request.body()

    # Verify HMAC signature BEFORE rate-limiting (S1): an attacker who knows a
    # valid trigger_id must not be able to exhaust the rate-limit bucket without
    # knowing the secret. Signature check is cheap (one HMAC-SHA256 call).
    # S2: the secret is encrypted at rest (Fernet vault, keyed by FERNET_KEY)
    # under schedule["secret_enc"] and decrypted in-memory only here. Legacy rows
    # with a plaintext schedule["secret"] are still honoured for compatibility.
    # The secret is never emitted to logs (the logger.warning calls do not
    # include it).
    secret = _resolve_schedule_secret(schedule)
    provided = (
        request.headers.get("X-Hub-Signature-256")
        or request.headers.get("X-Gitlab-Token")
        or request.headers.get("X-Signature")
        or request.headers.get("X-Webhook-Signature")
        or ""
    )
    # GitLab's X-Gitlab-Token is a shared-secret compare, not an HMAC digest.
    if request.headers.get("X-Gitlab-Token"):
        if not secret or not hmac.compare_digest(secret, provided):
            logger.warning(f"[AGENT] webhook trigger {trigger_id}: bad GitLab token")
            raise HTTPException(status_code=401, detail="Invalid signature")
    else:
        if not _verify_signature(secret, raw_body, provided):
            logger.warning(f"[AGENT] webhook trigger {trigger_id}: bad signature")
            raise HTTPException(status_code=401, detail="Invalid signature")

    # REQ-T4: rate limit / back-pressure — checked AFTER signature so only
    # authenticated callers consume the bucket (prevents unauthenticated DoS).
    if _rate_limited(trigger_id):
        logger.warning(f"[AGENT] webhook trigger {trigger_id}: rate limited")
        raise HTTPException(status_code=503, detail="Rate limit exceeded, retry later")

    # Parse JSON body (best-effort) for event matching.
    try:
        import json as _json
        payload = _json.loads(raw_body.decode("utf-8")) if raw_body else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    # REQ-T2: silently drop non-matching events (return 200 so provider stops).
    if not _event_matches(schedule, payload, request.headers):
        logger.info(f"[AGENT] webhook trigger {trigger_id}: event dropped (no match)")
        return {"status": "ignored"}

    # Fire on the durable path. _fire_trigger reloads the trigger fresh and
    # runs the C4 compliance + PI2 injection gate on input_text before execute.
    # Run in the background so the provider gets a fast 202.
    logger.info(f"[AGENT] webhook trigger {trigger_id}: accepted, firing")
    _task = asyncio.create_task(trigger_scheduler._fire_trigger(trigger_id))
    # S5: attach a done-callback so unhandled exceptions in _fire_trigger are
    # logged rather than silently dropped (Python only logs them to stderr as
    # "Task exception was never retrieved" which is easy to miss in production).
    def _on_fire_done(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            logger.error(
                f"[AGENT] webhook trigger {trigger_id}: _fire_trigger raised: {t.exception()}",
                exc_info=t.exception(),
            )
    _task.add_done_callback(_on_fire_done)
    return {"status": "accepted"}


@router.get("/trigger-executions")
async def list_trigger_executions_route(
    trigger_id: Optional[str] = None,
    limit: int = 50,
    current_user: AuthenticatedUser = Depends(require_access),
):
    rows = await workflow_repo.list_trigger_executions(
        current_user.id, trigger_id=trigger_id, limit=max(1, min(limit, 200)),
    )
    return rows


@router.get("/trigger-executions/unseen")
async def list_unseen_executions_route(
    current_user: AuthenticatedUser = Depends(require_access),
):
    return await workflow_repo.list_unseen_executions(current_user.id)


@router.get("/trigger-executions/{execution_id}")
async def get_execution_route(
    execution_id: int,
    current_user: AuthenticatedUser = Depends(require_access),
):
    row = await workflow_repo.get_execution(execution_id, current_user.id)
    if not row:
        raise HTTPException(status_code=404, detail="Execution not found")
    return row


@router.post("/trigger-executions/{execution_id}/seen", status_code=200)
async def mark_execution_seen_route(
    execution_id: int,
    current_user: AuthenticatedUser = Depends(require_access),
):
    ok = await workflow_repo.mark_execution_seen(execution_id, current_user.id)
    return {"ok": ok}


@router.post("/trigger-executions/mark-all-seen", status_code=200)
async def mark_all_executions_seen_route(
    current_user: AuthenticatedUser = Depends(require_access),
):
    count = await workflow_repo.mark_all_executions_seen(current_user.id)
    return {"updated": count}


@router.delete("/trigger-executions/{execution_id}", status_code=204)
async def delete_execution_route(
    execution_id: int,
    current_user: AuthenticatedUser = Depends(require_access),
):
    deleted = await workflow_repo.delete_execution(execution_id, current_user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Execution not found")


@router.delete("/trigger-executions", status_code=200)
async def delete_all_executions_route(
    current_user: AuthenticatedUser = Depends(require_access),
):
    count = await workflow_repo.delete_all_executions(current_user.id)
    return {"deleted": count}
