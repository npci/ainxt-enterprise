# SPDX-License-Identifier: MIT
# ============================================================
# DIGEST SERVICE — shared core for HOD and Manager monthly digests
#
# This module is the single home for the end-to-end digest pipeline shared
# by BOTH the HOD (department) and Manager (team) digests:
#
#   * Digest-type constants (DIGEST_TYPE_HOD, DIGEST_TYPE_MANAGER)
#   * Per-user usage aggregation (_build_user_blocks)
#   * LLM inference helpers (_call_llm_for_inferences, _fallback_inferences)
#   * Shared rendering (render_digest_* → unified Jinja templates with a
#     ``digest_type`` variable that switches HOD vs Manager branding)
#   * Shared send pipeline (generate_and_send_digest)
#   * Shared bulk loop (generate_and_send_digest_bulk)
#
# Do NOT put HOD-only or Manager-only logic here. Domain-specific roster
# resolution and payload building live in:
#
#   services/hod_statement_service.py     — HOD (per-department) specifics
#   services/manager_statement_service.py — Manager (per-team) specifics
#
# Trigger surfaces:
#   HOD     → routers/digest_hod_router.py
#   Manager → routers/digest_manager_router.py
# ============================================================

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from core.logger import logger
from db.database import SessionLocal
from db.models import User
from services.monthly_statement_service import (
    _jinja,
    build_statement_payload,
    render_html as render_user_html,
    upsert_archive,
)
from services.smtp_service import send_html_email


# ── Digest type constants ─────────────────────────────────────────────────
DIGEST_TYPE_HOD     = "hod"
DIGEST_TYPE_MANAGER = "manager"

# ── Configuration ─────────────────────────────────────────────────────────
# May be a raw model ID (e.g. "claude-sonnet-4-6", "gpt-5.4") OR a tier alias
# (e.g. "sonnet", "claude", "medium", "simple"). Both forms are honoured by
# models.model_router via its _HINT_MAP.
HOD_STATEMENT_LLM_MODEL = os.getenv("HOD_STATEMENT_LLM_MODEL", "").strip()
_IST_TZ = timezone(timedelta(hours=5, minutes=30))


# ── Small helpers ─────────────────────────────────────────────────────────
def _slug(value: str) -> str:
    if not value:
        return "department"
    s = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    s = re.sub(r"_+", "_", s)
    return s or "department"


def _now_ist_label() -> str:
    return datetime.now(timezone.utc).astimezone(_IST_TZ).strftime("%d %b %Y, %H:%M IST")


def _build_user_blocks(
    db: Session,
    users: List[User],
    month: int,
    year: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Aggregate per-user usage into digest-ready blocks.

    Returns ``(user_blocks, sub_by_uid, roster_totals)`` where
    ``user_blocks`` is sorted by total cost descending.
    """
    user_blocks: List[Dict[str, Any]] = []
    sub_by_uid: Dict[str, Dict[str, Any]] = {}
    total_cost     = 0.0
    total_tokens   = 0
    total_requests = 0

    for u in users:
        sub_payload = build_statement_payload(db, u, month, year)
        s = sub_payload["summary"]
        uid = sub_payload["user"]["id"]
        sub_by_uid[uid] = sub_payload
        user_blocks.append({
            "user_id":    uid,
            "name":       sub_payload["user"]["name"],
            "email":      sub_payload["user"]["email"],
            "department": sub_payload["user"]["department"],
            "ad_level":   sub_payload["user"]["ad_level"],
            "summary": {
                "total_cost":      float(s["total_cost"]),
                "total_tokens":    int(s["total_tokens"]),
                "total_requests":  int(s["total_requests"]),
                "monthly_budget":  float(s["monthly_budget"]),
                "utilization_pct": float(s["utilization_pct"]),
            },
            "daily":    sub_payload["daily"],
            "models":   sub_payload["models"],
            "channels": sub_payload["channels"],
        })
        total_cost     += float(s["total_cost"])
        total_tokens   += int(s["total_tokens"])
        total_requests += int(s["total_requests"])

    user_blocks.sort(
        key=lambda b: float(b["summary"]["total_cost"]),
        reverse=True,
    )

    roster_totals = {
        "total_cost":     total_cost,
        "total_tokens":   total_tokens,
        "total_requests": total_requests,
        "user_count":     len(user_blocks),
    }
    return user_blocks, sub_by_uid, roster_totals


# ============================================================
# LLM call — exactly one per digest per request
# ============================================================

_LLM_SYSTEM_PROMPT = (
    "You are a usage-analytics assistant. Given JSON of users' monthly LLM "
    "usage, identify top performers and underperformers with brief reasons, "
    "and write a 2-3 sentence narrative for the HOD. Respond ONLY with "
    "strict JSON matching this schema: "
    '{"top_performers": [{"user_id": str, "name": str, "reason": str}], '
    '"underperformers": [{"user_id": str, "name": str, "reason": str}], '
    '"narrative": str}. '
    "Pick between 1 and 5 entries per list. Do not include any prose outside the JSON."
)


def _compact_user_projection(users_block: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip the daily/heavy fields before shipping to the LLM."""
    out: List[Dict[str, Any]] = []
    for u in users_block:
        models   = u.get("models")   or []
        channels = u.get("channels") or []
        out.append({
            "user_id":         u.get("user_id"),
            "name":            u.get("name"),
            "email":           u.get("email"),
            "total_cost":      float(u["summary"]["total_cost"]),
            "total_tokens":    int(u["summary"]["total_tokens"]),
            "total_requests": int(u["summary"]["total_requests"]),
            "monthly_budget":  float(u["summary"]["monthly_budget"]),
            "utilization_pct": float(u["summary"]["utilization_pct"]),
            "top_3_models":    [
                {"model": m.get("display_name") or m.get("model"),
                 "cost":  float(m.get("cost", 0))}
                for m in models[:3]
            ],
            "top_3_channels":  [
                {"channel": c.get("display_name") or c.get("channel"),
                 "cost":    float(c.get("cost", 0))}
                for c in channels[:3]
            ],
        })
    return out


def _validate_inferences_shape(data: Any) -> Optional[Dict[str, Any]]:
    """Light validation — returns dict if shape matches, None otherwise."""
    if not isinstance(data, dict):
        return None
    tops = data.get("top_performers")
    unds = data.get("underperformers")
    narr = data.get("narrative", "")
    if not isinstance(tops, list) or not isinstance(unds, list):
        return None
    if not isinstance(narr, str):
        return None

    def _clean(items: List[Any]) -> List[Dict[str, str]]:
        cleaned: List[Dict[str, str]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            cleaned.append({
                "user_id": str(it.get("user_id") or ""),
                "name":    str(it.get("name")    or ""),
                "reason":  str(it.get("reason")  or ""),
            })
        return cleaned

    return {
        "top_performers":  _clean(tops),
        "underperformers": _clean(unds),
        "narrative":       narr.strip(),
    }


_LLM_TIMEOUT_SECS = 120


def _strip_code_fences(raw: str) -> str:
    """Remove leading ```json / trailing ``` fences a model may have emitted."""
    if not raw:
        return ""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    return s


def _call_llm_for_inferences(
    department_label: str,
    period_label:     str,
    compact_users:    List[Dict[str, Any]],
    model_hint:       Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], bool, int, str]:
    """One-shot LLM call routed through models.model_router.

    ``model_hint`` is passed as the model identifier — the router's
    ``_HINT_MAP`` accepts both raw model IDs (e.g. ``claude-sonnet-4-6``) and
    tier aliases (``sonnet``, ``claude``, ``medium``, …) and dispatches to
    the matching gateway (Claude / OpenAI / Gemini / Local LLM).

    When ``model_hint`` is ``None`` or empty, falls back to the module-level
    ``HOD_STATEMENT_LLM_MODEL`` env var.

    Returns ``(parsed_inferences_or_None, ok, elapsed_ms, model_used)``.
    ``ok=False`` means the caller must apply the deterministic fallback.
    """
    effective_model = (model_hint or "").strip() or HOD_STATEMENT_LLM_MODEL
    if not effective_model:
        logger.warning("digest_service: llm skipped — no model configured (HOD_STATEMENT_LLM_MODEL unset)")
        return None, False, 0, ""

    user_msg = json.dumps({
        "department":     department_label,
        "billing_period": period_label,
        "users":          compact_users,
    }, default=str)
    messages = [
        {"role": "system", "content": _LLM_SYSTEM_PROMPT},
        {"role": "user",   "content": user_msg},
    ]

    start = time.monotonic()
    raw = ""
    try:
        # model_router.generate never raises (it returns "Error: ..." text on
        # gateway failure), so we wrap it in a thread+timeout to honour the
        # spec's 30-s hard cap regardless of the gateway's own timeout.
        from models.model_router import model_router

        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FTE
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(model_router.generate, messages, effective_model)
            try:
                raw = future.result(timeout=_LLM_TIMEOUT_SECS) or ""
            except _FTE:
                logger.warning(
                    "digest_service: llm call timed out after %ds model=%s",
                    _LLM_TIMEOUT_SECS, effective_model,
                )
                # The router thread keeps running; we ignore its eventual result.
                return None, False, _LLM_TIMEOUT_SECS * 1000, effective_model
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "digest_service: llm call failed model=%s err=%s", effective_model, exc,
        )
        return None, False, elapsed_ms, effective_model

    elapsed_ms = int((time.monotonic() - start) * 1000)

    if not isinstance(raw, str) or not raw.strip():
        logger.warning("digest_service: llm returned empty content")
        return None, False, elapsed_ms, effective_model
    # model_router.generate returns "Error: ..." on gateway failure.
    if raw.startswith("Error:") or raw.startswith("Error collecting response"):
        logger.warning("digest_service: llm gateway error: %s", raw[:200])
        return None, False, elapsed_ms, effective_model

    cleaned = _strip_code_fences(raw)
    try:
        parsed = json.loads(cleaned) if cleaned else None
    except json.JSONDecodeError:
        logger.warning("digest_service: llm returned non-JSON content")
        return None, False, elapsed_ms, effective_model

    validated = _validate_inferences_shape(parsed)
    if validated is None:
        logger.warning("digest_service: llm JSON did not match schema")
        return None, False, elapsed_ms, effective_model
    return validated, True, elapsed_ms, effective_model


def _fallback_inferences(users_block: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministic top-3 / bottom-3 by total cost (excluding zero usage from
    the underperformer list)."""
    ranked = sorted(
        users_block,
        key=lambda u: float(u["summary"]["total_cost"]),
        reverse=True,
    )
    top = ranked[:3]
    with_usage = [u for u in ranked if float(u["summary"]["total_cost"]) > 0]
    bottom_pool = sorted(with_usage, key=lambda u: float(u["summary"]["total_cost"]))
    bottom = bottom_pool[:3]

    def _row(u: Dict[str, Any], reason_prefix: str) -> Dict[str, str]:
        cost = float(u["summary"]["total_cost"])
        return {
            "user_id": str(u["user_id"]),
            "name":    str(u["name"] or u["email"] or u["user_id"]),
            "reason":  f"{reason_prefix}: ${cost:.2f}",
        }

    return {
        "source":          "fallback",
        "top_performers":  [_row(u, "Highest total spend")   for u in top],
        "underperformers": [_row(u, "Lowest total spend")    for u in bottom],
        "narrative":       "",
    }


# ============================================================
# SHARED: rendering
# ============================================================

def _render(template_name: str, payload: Dict[str, Any], **extra: Any) -> str:
    return _jinja.get_template(template_name).render(**payload, **extra)


def _attachment_filename(payload: Dict[str, Any], prefix: str = DIGEST_TYPE_HOD) -> str:
    """Build the attachment filename. ``prefix`` is DIGEST_TYPE_HOD or DIGEST_TYPE_MANAGER."""
    if prefix == DIGEST_TYPE_MANAGER:
        label = (
            payload.get("manager", {}).get("name")
            or payload["department"]["department_name"]
            or "team"
        )
    else:
        label = payload["department"]["department_name"] or "department"
    return (
        f"{prefix}_statement_{_slug(label)}_"
        f"{payload['billing_year']:04d}-{payload['billing_month']:02d}.html"
    )


def render_digest_html_attachment(payload: Dict[str, Any], digest_type: str = DIGEST_TYPE_HOD) -> str:
    """Render the self-contained interactive HTML attachment.

    ``digest_type`` (``"hod"`` or ``"manager"``) is forwarded to the Jinja
    template so it can switch branding (title, header, HOD/Manager label).
    """
    return _render(
        "digest_attachment.html",
        payload,
        digest_type=digest_type,
        hod_data_json=json.dumps(payload, default=str),
        generated_at_ist=_now_ist_label(),
    )


def render_digest_email_body(
    payload: Dict[str, Any],
    digest_type: str = DIGEST_TYPE_HOD,
    attachment_filename: Optional[str] = None,
) -> str:
    """Render the Outlook-safe HTML email body."""
    return _render(
        "digest_email_body.html",
        payload,
        digest_type=digest_type,
        attachment_filename=attachment_filename or _attachment_filename(payload, prefix=digest_type),
    )


def render_digest_email_text(
    payload: Dict[str, Any],
    digest_type: str = DIGEST_TYPE_HOD,
    attachment_filename: Optional[str] = None,
) -> str:
    """Render the plain-text email fallback."""
    return _render(
        "digest_email_body.txt",
        payload,
        digest_type=digest_type,
        attachment_filename=attachment_filename or _attachment_filename(payload, prefix=digest_type),
    )


# ============================================================
# SHARED: end-to-end pipeline
# ============================================================

def generate_and_send_digest(
    payload:       Dict[str, Any],
    sub_by_uid:    Dict[str, Dict[str, Any]],
    recipient:     str,
    subject:       str,
    digest_type:   str,
    log_prefix:    str,
    log_context:   str,
    month:         int,
    year:          int,
    model_hint:    Optional[str] = None,
    db:            Optional[Session] = None,
) -> Dict[str, Any]:
    """Shared send pipeline for both HOD and Manager digests.

    1. Run LLM inference (one-shot, with deterministic fallback).
    2. Render attachment + email body + text body.
    3. Dispatch via SMTP.
    4. Archive per-user audit rows.

    Parameters
    ----------
    payload : dict
        The fully-built digest payload (HOD or Manager shaped).
    sub_by_uid : dict
        ``{user_id: full_per_user_payload}`` for the archive step.
    recipient : str
        Email address to send to.
    subject : str
        Email subject line.
    digest_type : str
        ``"hod"`` or ``"manager"`` — controls template branding.
    log_prefix : str
        Logger tag, e.g. ``"hod_statement"`` or ``"manager_statement"``.
    log_context : str
        Extra context for log lines, e.g. ``"dept=Finance"`` or ``"manager=foo@bar"``.
    month, year : int
        Billing period.
    model_hint : str or None
        LLM model hint; ``None`` → ``HOD_STATEMENT_LLM_MODEL`` env var.
    db : Session or None
        SQLAlchemy session; creates its own if ``None``.

    Returns
    -------
    dict
        ``{ok, sent, skipped_reason, llm_used, statement_ids, users_count, ...}``
    """
    owns_db = db is None
    db = db or SessionLocal()
    try:
        users_block = payload["users"]
        users_count = len(users_block)
        if users_count == 0:
            raise ValueError(f"{log_prefix}: no active users for {log_context}")

        logger.info(
            "%s: trigger %s month=%s year=%s users=%d",
            log_prefix, log_context, month, year, users_count,
        )

        # ── LLM inference ─────────────────────────────────────────────
        label = (
            payload.get("manager", {}).get("name")
            or payload["department"].get("corrected_department_name")
            or payload["department"].get("department_name")
        )
        inf_data, llm_ok, elapsed_ms, llm_model = _call_llm_for_inferences(
            department_label=label,
            period_label=payload["billing_period_label"],
            compact_users=_compact_user_projection(users_block),
            model_hint=model_hint,
        )
        logger.info(
            "%s: llm model=%s ok=%s elapsed_ms=%d",
            log_prefix, llm_model, llm_ok, elapsed_ms,
        )
        if llm_ok and inf_data is not None:
            payload["inferences"] = {
                "source":          "llm",
                "top_performers":  inf_data["top_performers"],
                "underperformers": inf_data["underperformers"],
                "narrative":       inf_data["narrative"],
            }
        else:
            payload["inferences"] = _fallback_inferences(users_block)

        # ── Render ────────────────────────────────────────────────────
        att_filename    = _attachment_filename(payload, prefix=digest_type)
        attachment_html = render_digest_html_attachment(payload, digest_type=digest_type)
        email_html      = render_digest_email_body(payload, digest_type=digest_type, attachment_filename=att_filename)
        email_text      = render_digest_email_text(payload, digest_type=digest_type, attachment_filename=att_filename)

        # ── SMTP dispatch ─────────────────────────────────────────────
        try:
            ok = send_html_email(
                to          = [recipient],
                subject     = subject,
                html_body   = email_html,
                text_body   = email_text,
                attachments = [{
                    "filename": att_filename,
                    "content":  attachment_html.encode("utf-8"),
                    "mimetype": "text/html",
                }],
            )
        except Exception as exc:
            logger.error(
                "%s: smtp dispatch failed %s err=%s",
                log_prefix, log_context, exc,
            )
            ok = False

        sent_at = datetime.now(timezone.utc) if ok else None

        # ── Archive per-user rows ─────────────────────────────────────
        statement_ids: List[str] = []
        for u in users_block:
            uid = u["user_id"]
            full_payload = sub_by_uid.get(uid)
            if full_payload is None:
                continue
            try:
                user_html = render_user_html(full_payload)
                row = upsert_archive(
                    db=db,
                    user_id=uid,
                    payload=full_payload,
                    html=user_html,
                    sent_at=sent_at,
                )
                statement_ids.append(str(row.id))
            except Exception as exc:
                logger.error(
                    "%s: archive upsert failed user=%s err=%s",
                    log_prefix, uid, exc,
                )

        if owns_db:
            db.commit()

        logger.info(
            "%s: sent %s users=%d ok=%s",
            log_prefix, log_context, users_count, ok,
        )

        return {
            "ok":             True,
            "sent":           bool(ok),
            "skipped_reason": None if ok else "smtp_failure",
            "llm_used":       bool(llm_ok),
            "statement_ids":  statement_ids,
            "users_count":    users_count,
        }
    except Exception:
        if owns_db:
            try:
                db.rollback()
            except Exception:
                pass
        raise
    finally:
        if owns_db:
            db.close()


# ============================================================
# SHARED: bulk loop
# ============================================================

def generate_and_send_digest_bulk(
    roster:            List[str],
    send_fn:           Callable[[str, int, int], Dict[str, Any]],
    roster_key_name:   str,
    log_prefix:        str,
    skippable_reasons: Set[str],
    month:             int,
    year:              int,
) -> Dict[str, Any]:
    """Shared bulk loop for both HOD and Manager digests.

    Iterates ``roster`` (a list of department names or manager emails),
    calls ``send_fn(key, month, year)`` for each, and classifies errors
    into skipped (expected) vs failed (unexpected).

    Parameters
    ----------
    roster : list of str
        Keys to iterate (corrected_department_name or manager_email).
    send_fn : callable
        ``(key, month, year) -> dict`` — the single-item send function.
        Must accept the key as first positional arg plus month, year as kwargs.
    roster_key_name : str
        The key name for the error dicts, e.g. ``"corrected_department_name"``
        or ``"manager_email"``.
    log_prefix : str
        Logger tag.
    skippable_reasons : set of str
        ValueError messages that count as "skipped" rather than "failed".
    month, year : int
        Billing period.

    Returns
    -------
    dict
        ``{ok, period, total, sent, skipped, skipped_reasons, failed}``
    """
    sent = 0
    skipped = 0
    failed: List[Dict[str, str]] = []
    skipped_reasons: Dict[str, int] = {}

    for key in roster:
        try:
            res = send_fn(key, month=month, year=year)
            if res.get("sent"):
                sent += 1
            else:
                skipped += 1
                reason = res.get("skipped_reason") or "unknown"
                skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
        except ValueError as exc:
            reason = str(exc)
            if reason in skippable_reasons:
                skipped += 1
                skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                logger.info(
                    "%s bulk: skip %s=%s reason=%s",
                    log_prefix, roster_key_name, key, reason,
                )
            else:
                logger.error(
                    "%s bulk: %s=%s err=%s", log_prefix, roster_key_name, key, reason,
                )
                failed.append({roster_key_name: key, "error": reason})
        except Exception as exc:
            logger.error(
                "%s bulk: %s=%s err=%s", log_prefix, roster_key_name, key, exc,
            )
            failed.append({roster_key_name: key, "error": str(exc)})

    return {
        "ok":              True,
        "period":          {"month": month, "year": year},
        "total":           len(roster),
        "sent":            sent,
        "skipped":         skipped,
        "skipped_reasons": skipped_reasons,
        "failed":          failed,
    }


# ============================================================
# CRON SCHEDULER — monthly HOD + Manager digest run
#
# Fires once per month (default 18:00 IST on the last day of the month) and
# dispatches BOTH the HOD bulk and the Manager bulk for the CURRENT billing
# month (the month the fire date falls in). HOD runs first; a HOD crash does
# not prevent the Manager step.
#
# Configuration (all optional; defaults yield 18:00 IST on the last day):
#
#   TEAM_USAGE_DIGEST_CRON_TIME=18:00         HH:MM, 24h.
#   TEAM_USAGE_DIGEST_CRON_TZ=Asia/Kolkata    IANA tz name.
#   TEAM_USAGE_DIGEST_CRON_DAY=last           'last' OR int 1..31.
#   TEAM_USAGE_DIGEST_CRON_ENABLED=true       Kill switch.
#
# Wiring: gateway.py calls start_scheduler() in its FastAPI startup hook and
# stop_scheduler() in its shutdown hook. Both are idempotent. The existing
# admin POST endpoints stay live for manual triggers; they log
# `trigger=manual` so cron-vs-manual is greppable in production logs.
# ============================================================

import threading

_scheduler: Any = None  # apscheduler.schedulers.asyncio.AsyncIOScheduler when running
_scheduler_lock = threading.Lock()


def _cron_tz() -> str:
    return os.getenv("TEAM_USAGE_DIGEST_CRON_TZ", "Asia/Kolkata").strip() or "Asia/Kolkata"


def _cron_time() -> Tuple[int, int]:
    """Parse TEAM_USAGE_DIGEST_CRON_TIME='HH:MM' → (hour, minute).

    Falls back to (18, 0) on malformed input with a warn log so a typo in the
    env doesn't silently disable the schedule.
    """
    raw = os.getenv("TEAM_USAGE_DIGEST_CRON_TIME", "").strip()
    if not raw:
        return 18, 0
    try:
        hh_str, mm_str = raw.split(":", 1)
        hh, mm = int(hh_str), int(mm_str)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("out of range")
        return hh, mm
    except Exception:
        logger.warning(
            "digest_cron: invalid TEAM_USAGE_DIGEST_CRON_TIME=%r — falling back to 18:00",
            raw,
        )
        return 18, 0


def _cron_day() -> Any:
    """Return ``"last"`` (literal) or an ``int`` in 1..31. Defaults to ``"last"``."""
    raw = os.getenv("TEAM_USAGE_DIGEST_CRON_DAY", "").strip().lower()
    if not raw or raw == "last":
        return "last"
    try:
        d = int(raw)
        if 1 <= d <= 31:
            return d
        raise ValueError("out of range")
    except Exception:
        logger.warning(
            "digest_cron: invalid TEAM_USAGE_DIGEST_CRON_DAY=%r — falling back to 'last'",
            raw,
        )
        return "last"


def _cron_enabled() -> bool:
    raw = os.getenv("TEAM_USAGE_DIGEST_CRON_ENABLED", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _current_period_ist() -> Tuple[int, int]:
    """Return ``(month, year)`` for the current billing period in IST.

    The cron fires on the last day of the month at 18:00 IST, so the firing
    day's IST month/year IS the billing period we want — exactly the "month
    that is ending today" semantics requested.
    """
    now_ist = datetime.now(timezone.utc).astimezone(_IST_TZ)
    return now_ist.month, now_ist.year


def _job_team_digest() -> None:
    """APScheduler callback — runs HOD bulk then Manager bulk for the
    current IST billing month. Each step is wrapped so one crash does not
    block the other.
    """
    month, year = _current_period_ist()
    logger.info(
        "digest_cron: fire trigger=cron month=%d year=%d", month, year,
    )

    # ── HOD bulk ──────────────────────────────────────────────────────
    try:
        from services.hod_statement_service import generate_and_send_hod_bulk
        hod_res = generate_and_send_hod_bulk(month=month, year=year)
        logger.info(
            "digest_cron: hod_bulk done sent=%s skipped=%s failed=%s",
            hod_res.get("sent"),
            hod_res.get("skipped"),
            len(hod_res.get("failed") or []),
        )
    except Exception as exc:
        logger.error("digest_cron: hod_bulk crashed err=%s", exc)

    # ── Manager bulk ──────────────────────────────────────────────────
    try:
        from services.manager_statement_service import generate_and_send_manager_bulk
        mgr_res = generate_and_send_manager_bulk(month=month, year=year)
        logger.info(
            "digest_cron: manager_bulk done sent=%s skipped=%s failed=%s",
            mgr_res.get("sent"),
            mgr_res.get("skipped"),
            len(mgr_res.get("failed") or []),
        )
    except Exception as exc:
        logger.error("digest_cron: manager_bulk crashed err=%s", exc)

    logger.info("digest_cron: cycle complete month=%d year=%d", month, year)


def start_scheduler() -> None:
    """Idempotent — starts the AsyncIOScheduler that owns the monthly job.

    Safe to call from FastAPI's startup hook; a no-op if already running or
    if disabled via ``TEAM_USAGE_DIGEST_CRON_ENABLED=false``.
    """
    global _scheduler

    if not _cron_enabled():
        logger.info(
            "digest_cron: disabled by TEAM_USAGE_DIGEST_CRON_ENABLED=false",
        )
        return

    with _scheduler_lock:
        if _scheduler is not None:
            logger.info("digest_cron: scheduler already running")
            return

        # Imported lazily so the rest of digest_service stays importable in
        # environments where APScheduler is not installed (e.g. unit tests).
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        tz = _cron_tz()
        hh, mm = _cron_time()
        day = _cron_day()

        scheduler = AsyncIOScheduler(timezone=tz)
        trigger = CronTrigger(
            day=day,           # 'last' or int 1..31 — both accepted by APScheduler ≥3.6
            hour=hh,
            minute=mm,
            timezone=tz,
        )
        scheduler.add_job(
            _job_team_digest,
            trigger,
            id="team_usage_digest",
            replace_existing=True,
            max_instances=1,   # if a prior fire is still running, skip the new one
            coalesce=True,     # collapse missed fires into a single catch-up run
            misfire_grace_time=3600,
        )
        scheduler.start()
        _scheduler = scheduler
        logger.info(
            "digest_cron: scheduler started tz=%s day=%s time=%02d:%02d",
            tz, day, hh, mm,
        )


def stop_scheduler() -> None:
    """Idempotent shutdown — safe to call from FastAPI's shutdown hook."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            return
        try:
            _scheduler.shutdown(wait=False)
        except Exception as exc:
            logger.warning("digest_cron: scheduler shutdown error: %s", exc)
        _scheduler = None
