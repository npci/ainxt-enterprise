# SPDX-License-Identifier: Apache-2.0
# ============================================================
# MONTHLY STATEMENT ROUTER
#
#   POST /admin/generate-statements                 (admin)
#   POST /admin/send-statement/{user_id}            (admin)
#   POST /admin/send-statement/{user_id}/{month}/{year}  (admin, explicit period)
#   GET  /user/statement/{month}/{year}             (logged-in user)
#   PUT  /user/preferences/monthly-statement        (logged-in user)
# ============================================================

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
import re

from pydantic import BaseModel, Field, field_validator

from auth.dependencies import get_current_user
from auth.rbac import require_admin_flag
from core.logger import logger
from core.rate_limiter import enforce_rate_limit_with_behaviour, SENSITIVE_ADMIN
from db.database import SessionLocal
from db.models import User
from db.monthly_statement_models import (
    MonthlyStatement,
    UserNotificationPreference,
)
from services.monthly_statement_service import (
    generate_and_send,
    generate_and_send_bulk,
    generate_statement,
)


monthly_statement_router = APIRouter(tags=["monthly-statements"])


# ── Helpers ─────────────────────────────────────────────────────────────
def _validate_period(month: int, year: int) -> None:
    if not 1 <= month <= 12:
        raise HTTPException(400, "month must be between 1 and 12")
    if not 2024 <= year <= 2100:
        raise HTTPException(400, "year must be between 2024 and 2100")


def _previous_billing_period() -> tuple[int, int]:
    now = datetime.utcnow()
    if now.month == 1:
        return 12, now.year - 1
    return now.month - 1, now.year


# ============================================================
# ADMIN — bulk generate + send
# ============================================================

class BulkRequest(BaseModel):
    month: Optional[int] = Field(None, description="1-12; defaults to previous month")
    year:  Optional[int] = Field(None, description="YYYY; defaults to previous month's year")


@monthly_statement_router.post("/admin/generate-statements")
def admin_bulk_generate(
    request: Request,
    body: BulkRequest = Body(default_factory=BulkRequest),
    _admin=Depends(require_admin_flag),
):
    """Run statement generation + email send for every active opted-in user."""
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)

    month = body.month
    year  = body.year
    if month is None or year is None:
        month, year = _previous_billing_period()
    _validate_period(month, year)

    logger.info("admin_bulk_generate: month=%s year=%s", month, year)
    result = generate_and_send_bulk(month=month, year=year)
    return {"ok": True, "period": {"month": month, "year": year}, **result}


# ============================================================
# ADMIN — single-user send
# ============================================================

@monthly_statement_router.post("/admin/send-statement/{user_id}")
def admin_send_one(
    user_id: str,
    request: Request,
    body: BulkRequest = Body(default_factory=BulkRequest),
    _admin=Depends(require_admin_flag),
):
    """
    Generate and send a single user's statement.  Honours opt-out by default.
    Pass ?force=true to bypass the user's preference (e.g. legal/audit copy).
    """
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)

    force = request.query_params.get("force", "").lower() in {"1", "true", "yes"}

    month = body.month
    year  = body.year
    if month is None or year is None:
        month, year = _previous_billing_period()
    _validate_period(month, year)

    logger.info(
        "admin_send_one: user=%s month=%s year=%s force=%s",
        user_id, month, year, force,
    )
    try:
        result = generate_and_send(user_id=user_id, month=month, year=year, force=force)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True, "period": {"month": month, "year": year}, **result}


# ============================================================
# USER — view archived statement
# ============================================================

@monthly_statement_router.get("/user/statement/{month}/{year}")
def user_view_statement(
    month: int,
    year: int,
    current_user: dict = Depends(get_current_user),
):
    """
    Return the most recent archived statement HTML + JSON for the
    logged-in user.  If none exists yet (e.g. mid-month preview), it is
    generated on-the-fly and archived.
    """
    _validate_period(month, year)
    user_id = current_user.get("sub") or current_user.get("user_id")
    if not user_id:
        raise HTTPException(401, "no user identity in token")

    db = SessionLocal()
    try:
        existing = (
            db.query(MonthlyStatement)
            .filter(
                MonthlyStatement.user_id == user_id,
                MonthlyStatement.billing_month == month,
                MonthlyStatement.billing_year  == year,
            )
            .first()
        )
        if existing is not None:
            return {
                "statement_id": str(existing.id),
                "user_id":      user_id,
                "billing_month": month,
                "billing_year":  year,
                "sent_at":      existing.sent_at.isoformat() if existing.sent_at else None,
                "summary":      existing.statement_json,
                "html":         existing.statement_html,
            }

        # Generate-on-demand (does NOT send an email).
        payload = generate_statement(user_id=user_id, month=month, year=year, db=db)
        db.commit()
        # Re-fetch HTML from the row we just upserted
        row = (
            db.query(MonthlyStatement)
            .filter(MonthlyStatement.id == payload["statement_id"])
            .first()
        )
        return {
            "statement_id": payload["statement_id"],
            "user_id":      user_id,
            "billing_month": month,
            "billing_year":  year,
            "sent_at":      None,
            "summary":      row.statement_json,
            "html":         row.statement_html,
        }
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    finally:
        db.close()


# ============================================================
# USER — preferences
# ============================================================

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class PreferenceUpdate(BaseModel):
    enabled:        Optional[bool] = Field(None, description="opt-in / opt-out")
    email_override: Optional[str]  = Field(None, description="alternate inbox; empty string to clear")

    @field_validator("email_override")
    @classmethod
    def _check_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        if not _EMAIL_RE.match(v):
            raise ValueError("email_override must be a valid email address")
        return v


@monthly_statement_router.put("/user/preferences/monthly-statement")
def user_update_preference(
    body: PreferenceUpdate,
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user.get("sub") or current_user.get("user_id")
    if not user_id:
        raise HTTPException(401, "no user identity in token")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            raise HTTPException(404, "user not found")

        pref = (
            db.query(UserNotificationPreference)
            .filter(UserNotificationPreference.user_id == user_id)
            .first()
        )
        if pref is None:
            pref = UserNotificationPreference(user_id=user_id)
            db.add(pref)

        if body.enabled is not None:
            pref.monthly_statement_enabled = bool(body.enabled)
        if body.email_override is not None:
            # empty string  → clear override; valid email → set; null → leave
            pref.email_override = body.email_override or None

        db.commit()
        return {
            "ok":                       True,
            "user_id":                  user_id,
            "monthly_statement_enabled": pref.monthly_statement_enabled,
            "email_override":            pref.email_override,
        }
    finally:
        db.close()


@monthly_statement_router.get("/user/preferences/monthly-statement")
def user_get_preference(
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user.get("sub") or current_user.get("user_id")
    if not user_id:
        raise HTTPException(401, "no user identity in token")

    db = SessionLocal()
    try:
        pref = (
            db.query(UserNotificationPreference)
            .filter(UserNotificationPreference.user_id == user_id)
            .first()
        )
        return {
            "user_id":                   user_id,
            "monthly_statement_enabled": pref.monthly_statement_enabled if pref else True,
            "email_override":            pref.email_override if pref else None,
        }
    finally:
        db.close()
