# SPDX-License-Identifier: MIT
# ============================================================
# MONTHLY USAGE STATEMENT — generator + dispatcher
#
# Responsibilities:
#   * Aggregate one user's usage for a (billing_month, billing_year)
#     across the four required dimensions: account summary, day-wise,
#     model-wise, channel-wise.
#   * Render an HTML + plain-text email via Jinja2.
#   * Upsert one row into `monthly_statements` (idempotent re-runs).
#   * Send the HTML mail via services.smtp_service (AiNxt relay).
#
# Trigger surfaces: routers/monthly_statement_router.py
#
# All timestamp aggregation is done in `Asia/Kolkata` to match the
# IST-based billing-period spec.
# ============================================================

from __future__ import annotations

import calendar
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.config import PLATFORM_NAME
from core.logger import logger
from db.database import SessionLocal
from db.models import User, ModelUsage, ModelRateTable, BudgetConfig
from db.monthly_statement_models import (
    MonthlyStatement,
    UserNotificationPreference,
)
from services.smtp_service import send_html_email


# ── Template environment ───────────────────────────────────────────────────
_TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "email_templates",
)
_jinja = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=False,
    lstrip_blocks=False,
)

# The deploying organisation's name, exposed to every template as `platform_name`
# so no caller has to add it to its own payload. The templates previously hardcoded
# one specific organisation's name in text the recipient reads, which meant every
# outbound email carried that brand no matter who deployed the platform.
# Defaults to "AiNxt"; set PLATFORM_NAME to rebrand.
_jinja.globals["platform_name"] = PLATFORM_NAME


# ── Tier mapping ───────────────────────────────────────────────────────────
# ad_level 0–1 = Admin   ($500)
# ad_level 2–3 = Director ($100)
# ad_level 4–6 = Engineer ($50)
BUDGET_TIER_MAP: List[Tuple[range, str, float]] = [
    (range(0, 2), "Admin",    500.0),
    (range(2, 4), "Director", 100.0),
    (range(4, 7), "Engineer",  50.0),
]


def _resolve_tier(ad_level: Optional[int]) -> Tuple[str, float]:
    lvl = ad_level if ad_level is not None else 6
    for r, name, budget in BUDGET_TIER_MAP:
        if lvl in r:
            return name, budget
    return "Engineer", 50.0


# ── Channel attribution (derived from model_usages.endpoint) ──────────────
#
# Channel buckets, mapped by the path prefix of the request that produced
# the model_usages row.  This is the single source of truth: we do NOT add
# a new column to model_usages — we read what is already there.
#
# Mapping (prefix is matched case-sensitively in SQL with `LIKE`):
#
#   /v1/messages                  → cli          AiNxt CLI / Claude-style API
#   /ask                          → chat         Chat (Web UI)
#   /v1/chat/completions          → ide          IDE (Kilo Code / VS Code / JetBrains)
#   /agents/.../run               → agent        Agent Runs (AiNxt Policy, etc.)
#   /sdlc/                        → sdlc         SDLC Pipeline
#   anything else (non-NULL)      → api          API / other backend calls
#   NULL                          → unknown      Legacy rows without endpoint
#
# Keep the codes in lock-step with `CHANNEL_CASE_SQL` below — both the
# CASE branches and the display map must list the same bucket codes.
CHANNEL_DISPLAY = {
    "cli":     "CLI (AiNxt CLI)",
    "chat":    "Chat (Web UI)",
    "ide":     "IDE (Kilo Code / VS Code / JetBrains)",
    "agent":   "Agent Runs",
    "sdlc":    "SDLC Pipeline",
    "api":     "API / Other",
    "unknown": "Unknown (endpoint not recorded)",
}

# Stable ordering used for display when costs tie.
CHANNEL_ORDER = ["cli", "chat", "ide", "agent", "sdlc", "api", "unknown"]

# The CASE branches MUST be kept in sync with CHANNEL_DISPLAY above.
# Note the use of LIKE with explicit prefixes so a path such as
# '/v1/chat/completions/stream' is still attributed to the IDE bucket.
CHANNEL_CASE_SQL = """
    CASE
        WHEN endpoint IS NULL                        THEN 'unknown'
        WHEN endpoint LIKE '/v1/messages%'           THEN 'cli'
        WHEN endpoint LIKE '/ask%'                   THEN 'chat'
        WHEN endpoint LIKE '/v1/chat/completions%'   THEN 'ide'
        WHEN endpoint LIKE '/agents/%'               THEN 'agent'
        WHEN endpoint LIKE '/sdlc/%'                 THEN 'sdlc'
        ELSE                                              'api'
    END
"""


# ── Billing-period helper ──────────────────────────────────────────────────
@dataclass
class BillingPeriod:
    month: int
    year:  int
    start_ist: datetime    # local IST tz-naive
    end_ist:   datetime    # exclusive upper bound (first day of next month, IST)

    @property
    def label(self) -> str:
        last_day = calendar.monthrange(self.year, self.month)[1]
        return (
            f"{calendar.month_name[self.month]} 1, {self.year} – "
            f"{calendar.month_name[self.month]} {last_day}, {self.year}"
        )


def build_period(month: int, year: int) -> BillingPeriod:
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    return BillingPeriod(month=month, year=year, start_ist=start, end_ist=end)


# ============================================================
# AGGREGATION QUERIES
# ============================================================
#
# All queries use:
#   WHERE created_at >= (:start AT TIME ZONE 'Asia/Kolkata')
#     AND created_at <  (:end   AT TIME ZONE 'Asia/Kolkata')
# which converts the IST-local boundaries to UTC for the comparison
# against TIMESTAMPTZ columns.
# ============================================================

def _q_summary(db: Session, user_id: str, period: BillingPeriod) -> Dict[str, Any]:
    row = db.execute(text("""
        SELECT
            COALESCE(SUM(input_tokens),  0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(total_tokens),  0) AS total_tokens,
            COALESCE(SUM(cost_usd),      0) AS total_cost,
            COUNT(*)                        AS total_requests
        FROM ainxt.model_usages
        WHERE user_id = :uid
          AND created_at >= (:start AT TIME ZONE 'Asia/Kolkata')
          AND created_at <  (:end   AT TIME ZONE 'Asia/Kolkata')
    """), {"uid": user_id, "start": period.start_ist, "end": period.end_ist}).mappings().first()
    return dict(row or {})


def _q_daily(db: Session, user_id: str, period: BillingPeriod) -> List[Dict[str, Any]]:
    # Generate a row per calendar day in IST, then LEFT JOIN aggregated usage.
    rows = db.execute(text("""
        WITH days AS (
            SELECT generate_series(
                CAST(:start AS date), (CAST(:end AS date) - INTERVAL '1 day')::date, INTERVAL '1 day'
            )::date AS d
        ),
        agg AS (
            SELECT
                (created_at AT TIME ZONE 'Asia/Kolkata')::date AS d,
                COUNT(*)                        AS requests,
                COALESCE(SUM(input_tokens),  0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(total_tokens),  0) AS total_tokens,
                COALESCE(SUM(cost_usd),      0) AS cost
            FROM ainxt.model_usages
            WHERE user_id = :uid
              AND created_at >= (:start AT TIME ZONE 'Asia/Kolkata')
              AND created_at <  (:end   AT TIME ZONE 'Asia/Kolkata')
            GROUP BY 1
        )
        SELECT
            days.d                              AS date,
            COALESCE(agg.requests,      0)      AS requests,
            COALESCE(agg.input_tokens,  0)      AS input_tokens,
            COALESCE(agg.output_tokens, 0)      AS output_tokens,
            COALESCE(agg.total_tokens,  0)      AS total_tokens,
            COALESCE(agg.cost,          0)      AS cost
        FROM days
        LEFT JOIN agg ON agg.d = days.d
        ORDER BY days.d ASC
    """), {"uid": user_id, "start": period.start_ist, "end": period.end_ist}).mappings().all()
    return [dict(r) for r in rows]


def _q_models(db: Session, user_id: str, period: BillingPeriod) -> List[Dict[str, Any]]:
    rows = db.execute(text("""
        SELECT
            mu.model                            AS model,
            COUNT(*)                            AS requests,
            COALESCE(SUM(mu.input_tokens),  0)  AS input_tokens,
            COALESCE(SUM(mu.output_tokens), 0)  AS output_tokens,
            COALESCE(SUM(mu.total_tokens),  0)  AS total_tokens,
            COALESCE(SUM(mu.cost_usd),      0)  AS cost
        FROM ainxt.model_usages mu
        WHERE mu.user_id = :uid
          AND mu.created_at >= (:start AT TIME ZONE 'Asia/Kolkata')
          AND mu.created_at <  (:end   AT TIME ZONE 'Asia/Kolkata')
        GROUP BY mu.model
        ORDER BY cost DESC
    """), {"uid": user_id, "start": period.start_ist, "end": period.end_ist}).mappings().all()

    # Cross-reference latest model_rate_table row for a friendlier display name.
    display = {}
    for mid, in db.execute(
        text("SELECT DISTINCT model_id FROM ainxt.model_rate_table")
    ).all():
        display[mid] = mid   # display_name column doesn't exist; use model_id as-is

    return [
        {
            **dict(r),
            "display_name": display.get(r["model"], r["model"]),
        }
        for r in rows
    ]


def _q_channels(db: Session, user_id: str, period: BillingPeriod) -> List[Dict[str, Any]]:
    """
    Channel attribution is derived in-query from the existing
    `model_usages.endpoint` column (no new schema).  See `CHANNEL_CASE_SQL`
    above for the prefix → bucket mapping.

    Rows where `endpoint IS NULL` fall into the `'unknown'` bucket — this is
    expected for legacy traffic and is the bucket we expect to drain over
    time as upstream writers populate `endpoint` consistently.
    """
    sql = text(f"""
        SELECT
            {CHANNEL_CASE_SQL}                  AS channel,
            COUNT(*)                            AS requests,
            COALESCE(SUM(input_tokens),  0)     AS input_tokens,
            COALESCE(SUM(output_tokens), 0)     AS output_tokens,
            COALESCE(SUM(total_tokens),  0)     AS total_tokens,
            COALESCE(SUM(cost_usd),      0)     AS cost
        FROM ainxt.model_usages
        WHERE user_id = :uid
          AND created_at >= (:start AT TIME ZONE 'Asia/Kolkata')
          AND created_at <  (:end   AT TIME ZONE 'Asia/Kolkata')
        GROUP BY 1
    """)
    rows = db.execute(
        sql, {"uid": user_id, "start": period.start_ist, "end": period.end_ist},
    ).mappings().all()

    out = []
    for r in rows:
        d = dict(r)
        d["display_name"] = CHANNEL_DISPLAY.get(d["channel"], d["channel"])
        out.append(d)
    # Primary sort: cost descending.  Secondary: stable bucket order so two
    # zero-cost buckets always render in the same visual sequence.
    order_index = {code: i for i, code in enumerate(CHANNEL_ORDER)}
    out.sort(key=lambda x: (-float(x["cost"]), order_index.get(x["channel"], 99)))
    return out


def _resolve_monthly_budget(db: Session, user: User, tier_default: float) -> float:
    """
    Prefer the user's explicit budget_configs row, else fall back to the
    tier default we derived from ad_level. There is no band-level fallback
    row to consult any more — budget_configs is only ever seeded per-user
    (band-level template rows with user_id IS NULL were dead data and are no
    longer created).
    """
    row = (
        db.query(BudgetConfig)
        .filter(BudgetConfig.user_id == str(user.id))
        .first()
    )
    if row is not None:
        return float(row.monthly_limit_usd)
    return tier_default


# ============================================================
# PUBLIC API
# ============================================================

def build_statement_payload(
    db: Session,
    user: User,
    month: int,
    year: int,
) -> Dict[str, Any]:
    """
    Pure aggregation + shaping; no DB writes, no email.  The returned dict is
    what we both render with Jinja and persist in `statement_json`.
    """
    period = build_period(month, year)
    summary = _q_summary(db, str(user.id), period)
    daily   = _q_daily  (db, str(user.id), period)
    models  = _q_models (db, str(user.id), period)
    channels = _q_channels(db, str(user.id), period)

    tier_name, tier_default_budget = _resolve_tier(user.ad_level)
    monthly_budget = _resolve_monthly_budget(db, user, tier_default_budget)
    total_cost = float(summary.get("total_cost") or 0)
    remaining  = max(monthly_budget - total_cost, 0.0)
    utilization = (total_cost / monthly_budget * 100.0) if monthly_budget > 0 else 0.0
    utilization_clamped = min(max(utilization, 0.0), 100.0)
    if utilization < 60:
        util_color = "#10b981"   # green
    elif utilization < 90:
        util_color = "#f59e0b"   # amber
    else:
        util_color = "#ef4444"   # red

    total_pos_cost = sum(float(m["cost"]) for m in models) or 1.0
    for m in models:
        m["pct"] = float(m["cost"]) / total_pos_cost * 100.0
    for c in channels:
        c["pct"] = float(c["cost"]) / total_pos_cost * 100.0

    payload = {
        "billing_month": month,
        "billing_year":  year,
        "billing_period_label": period.label,
        "user": {
            "id":          str(user.id),
            "name":        user.name,
            "email":       user.email,
            "employee_id": user.ad_username or "—",
            "department":  user.department,
            "ad_level":    user.ad_level,
            "role":        user.role,
        },
        "summary": {
            "budget_tier":     tier_name,
            "monthly_budget":  monthly_budget,
            "total_cost":      total_cost,
            "remaining":       remaining,
            "utilization_pct": utilization,
            "utilization_clamped": utilization_clamped,
            "utilization_color":   util_color,
            "total_requests":  int(summary.get("total_requests") or 0),
            "input_tokens":    int(summary.get("input_tokens")  or 0),
            "output_tokens":   int(summary.get("output_tokens") or 0),
            "total_tokens":    int(summary.get("total_tokens")  or 0),
        },
        "daily":    [
            {
                "date":          r["date"].isoformat() if isinstance(r["date"], date) else str(r["date"]),
                "requests":      int(r["requests"]),
                "input_tokens":  int(r["input_tokens"]),
                "output_tokens": int(r["output_tokens"]),
                "total_tokens":  int(r["total_tokens"]),
                "cost":          float(r["cost"]),
            } for r in daily
        ],
        "models":   [
            {
                "model":         m["model"],
                "display_name":  m["display_name"],
                "requests":      int(m["requests"]),
                "input_tokens":  int(m["input_tokens"]),
                "output_tokens": int(m["output_tokens"]),
                "total_tokens":  int(m["total_tokens"]),
                "cost":          float(m["cost"]),
                "pct":           float(m["pct"]),
            } for m in models
        ],
        "channels": [
            {
                "channel":       c["channel"],
                "display_name":  c["display_name"],
                "requests":      int(c["requests"]),
                "input_tokens":  int(c["input_tokens"]),
                "output_tokens": int(c["output_tokens"]),
                "total_tokens":  int(c["total_tokens"]),
                "cost":          float(c["cost"]),
                "pct":           float(c["pct"]),
            } for c in channels
        ],
    }
    return payload


def render_html(payload: Dict[str, Any]) -> str:
    tpl = _jinja.get_template("monthly_statement.html")
    return tpl.render(**payload)


def render_text(payload: Dict[str, Any]) -> str:
    tpl = _jinja.get_template("monthly_statement.txt")
    return tpl.render(**payload)


def upsert_archive(
    db: Session,
    user_id: str,
    payload: Dict[str, Any],
    html: str,
    sent_at: Optional[datetime],
) -> MonthlyStatement:
    """Public alias of :func:`_upsert_archive` for cross-service reuse
    (e.g. the HOD digest pipeline). Behaviour is identical."""
    return _upsert_archive(db, user_id, payload, html, sent_at)


def _upsert_archive(
    db: Session,
    user_id: str,
    payload: Dict[str, Any],
    html: str,
    sent_at: Optional[datetime],
) -> MonthlyStatement:
    existing = (
        db.query(MonthlyStatement)
        .filter(
            MonthlyStatement.user_id == user_id,
            MonthlyStatement.billing_month == payload["billing_month"],
            MonthlyStatement.billing_year  == payload["billing_year"],
        )
        .first()
    )
    if existing is None:
        existing = MonthlyStatement(
            user_id        = user_id,
            billing_month  = payload["billing_month"],
            billing_year   = payload["billing_year"],
        )
        db.add(existing)

    existing.statement_html  = html
    existing.statement_json  = payload
    existing.total_cost      = Decimal(str(payload["summary"]["total_cost"]))
    existing.total_tokens    = payload["summary"]["total_tokens"]
    existing.total_requests  = payload["summary"]["total_requests"]
    if sent_at is not None:
        existing.sent_at = sent_at
    db.flush()
    return existing


def _get_preference(db: Session, user_id: str) -> UserNotificationPreference:
    pref = (
        db.query(UserNotificationPreference)
        .filter(UserNotificationPreference.user_id == user_id)
        .first()
    )
    if pref is None:
        pref = UserNotificationPreference(user_id=user_id)
        db.add(pref)
        db.flush()
    return pref


def generate_statement(
    user_id: str,
    month: int,
    year: int,
    db: Optional[Session] = None,
) -> Dict[str, Any]:
    """
    Build the JSON payload + HTML and upsert into `monthly_statements`
    *without* sending an email.  Useful for the in-app /user/statement
    endpoint and admin previews.

    Returns the payload (with `statement_id` added).
    """
    owns_db = db is None
    db = db or SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            raise ValueError(f"user {user_id} not found")

        payload = build_statement_payload(db, user, month, year)
        html    = render_html(payload)
        row     = _upsert_archive(db, user_id, payload, html, sent_at=None)
        if owns_db:
            db.commit()
        payload["statement_id"] = str(row.id)
        return payload
    except Exception:
        if owns_db:
            db.rollback()
        raise
    finally:
        if owns_db:
            db.close()


def generate_and_send(
    user_id: str,
    month: int,
    year: int,
    force: bool = False,
    db: Optional[Session] = None,
) -> Dict[str, Any]:
    """
    Generate the statement and dispatch the HTML email through the AiNxt relay.

    `force=True` skips the opt-out check (admin override).

    Returns dict: {statement_id, sent, skipped_reason}.
    """
    owns_db = db is None
    db = db or SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            raise ValueError(f"user {user_id} not found")

        pref = _get_preference(db, user_id)
        if not force and not pref.monthly_statement_enabled:
            logger.info("monthly_statement: user %s opted out — skipping send", user_id)
            payload = build_statement_payload(db, user, month, year)
            html    = render_html(payload)
            row     = _upsert_archive(db, user_id, payload, html, sent_at=None)
            if owns_db:
                db.commit()
            return {
                "statement_id":   str(row.id),
                "sent":           False,
                "skipped_reason": "user_opted_out",
            }

        payload = build_statement_payload(db, user, month, year)
        html    = render_html(payload)
        text_body = render_text(payload)

        recipient = pref.email_override or user.email
        subject   = (
            f"AiNxt — Your Monthly Usage Statement "
            f"({payload['billing_period_label']})"
        )

        ok = send_html_email(
            to        = [recipient],
            subject   = subject,
            html_body = html,
            text_body = text_body,
        )
        sent_at = datetime.now(timezone.utc) if ok else None
        row = _upsert_archive(db, user_id, payload, html, sent_at=sent_at)
        if owns_db:
            db.commit()

        return {
            "statement_id":   str(row.id),
            "sent":           bool(ok),
            "skipped_reason": None if ok else "smtp_failure",
        }
    except Exception:
        if owns_db:
            db.rollback()
        raise
    finally:
        if owns_db:
            db.close()


# ============================================================
# BULK
# ============================================================

def list_active_user_ids(db: Session) -> List[str]:
    """
    All non-deactivated users whose monthly_statement_enabled is TRUE (or
    who have no preference row yet — default opt-in).
    """
    rows = db.execute(text("""
        SELECT u.id::text AS id
        FROM ainxt.users u
        LEFT JOIN ainxt.user_notification_preferences p
               ON p.user_id = u.id
        WHERE COALESCE(u.is_active, TRUE) = TRUE
          AND COALESCE(p.monthly_statement_enabled, TRUE) = TRUE
    """)).all()
    return [r[0] for r in rows]


def generate_and_send_bulk(
    month: int,
    year: int,
) -> Dict[str, Any]:
    """
    Run generate_and_send for every eligible user.  Errors per user are
    isolated — one failure must not abort the rest.
    """
    db = SessionLocal()
    try:
        user_ids = list_active_user_ids(db)
    finally:
        db.close()

    sent = 0
    skipped = 0
    failed: List[Dict[str, str]] = []
    for uid in user_ids:
        try:
            result = generate_and_send(uid, month, year)
            if result["sent"]:
                sent += 1
            else:
                skipped += 1
        except Exception as exc:
            logger.error("monthly_statement bulk: user=%s err=%s", uid, exc)
            failed.append({"user_id": uid, "error": str(exc)})
    return {
        "total":   len(user_ids),
        "sent":    sent,
        "skipped": skipped,
        "failed":  failed,
    }
