# SPDX-License-Identifier: Apache-2.0
# ============================================================
# MANAGER MONTHLY USAGE DIGEST ROUTER
#
#   POST /admin/send-manager-statement/{manager_email}
#   POST /admin/send-manager-statements
#
# Both endpoints are admin-only (require_admin_flag) and rate-limited
# under the SENSITIVE_ADMIN behaviour. The full pipeline lives in
# services/manager_statement_service.py (manager-specific roster + payload)
# which in turn delegates rendering/dispatch/archival to
# services/digest_service.py. This router is just a thin HTTP adapter that
# mirrors routers/digest_hod_router.py.
# ============================================================

from __future__ import annotations

from typing import Optional

import re

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from auth.rbac import require_admin_flag
from core.logger import logger
from core.rate_limiter import enforce_rate_limit_with_behaviour, SENSITIVE_ADMIN
from services.manager_statement_service import (
    generate_and_send_manager,
    generate_and_send_manager_bulk,
)


digest_manager_router = APIRouter(tags=["digest-manager"])


# ── Helpers ─────────────────────────────────────────────────────────────
# Mirror routers/digest_hod_router.py — same light-touch email validation
# used by routers/monthly_statement_router.py.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _parse_to_override(to: Optional[str]) -> Optional[str]:
    """Validate the optional ``?to=`` query param. Returns the cleaned address
    or ``None`` when the param is absent / blank. Raises HTTPException(400)
    on a malformed address so we fail fast at the edge."""
    if to is None:
        return None
    cleaned = to.strip()
    if not cleaned:
        return None
    if not _EMAIL_RE.match(cleaned):
        raise HTTPException(400, "?to= must be a valid email address")
    return cleaned


def _validate_period(month: Optional[int], year: Optional[int]) -> None:
    if month is None or year is None:
        raise HTTPException(400, "month and year are both required")
    if not isinstance(month, int) or not isinstance(year, int):
        raise HTTPException(400, "month and year must be integers")
    if not 1 <= month <= 12:
        raise HTTPException(400, "month must be between 1 and 12")
    if not 2024 <= year <= 2100:
        raise HTTPException(400, "year must be between 2024 and 2100")


# ── Request body ────────────────────────────────────────────────────────
class PeriodBody(BaseModel):
    # Mirror the HOD router contract: typing the fields as Optional lets
    # us reject missing values with HTTPException(400) rather than
    # Pydantic's 422.
    month: Optional[int] = Field(None, description="1-12")
    year:  Optional[int] = Field(None, description="YYYY")


# ============================================================
# Single-manager trigger
# ============================================================

@digest_manager_router.post("/admin/send-manager-statement/{manager_email}")
def admin_send_manager_one(
    manager_email: str,
    request: Request,
    body: PeriodBody = Body(...),
    to: Optional[str] = Query(
        None,
        description=(
            "Optional test recipient override. When set, the digest is "
            "rendered and dispatched exactly as in production but the email "
            "goes to this address instead of the real manager's inbox. "
            "Audit-logged."
        ),
    ),
    admin: dict = Depends(require_admin_flag),
):
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)
    _validate_period(body.month, body.year)
    to_override = _parse_to_override(to)

    # Audit marker — distinguishes ad-hoc admin triggers from the cron-driven
    # monthly run (services/digest_service.py:_job_team_digest, which logs
    # `trigger=cron`). When ?to= is set, a TEST marker is also logged inside
    # services.manager_statement_service.generate_and_send_manager.
    logger.info(
        "manager_statement: trigger=manual admin=%s manager=%s month=%s year=%s to=%s",
        (admin or {}).get("email"),
        manager_email, body.month, body.year, to_override,
    )
    try:
        result = generate_and_send_manager(
            manager_email=manager_email,
            month=body.month,
            year=body.year,
            to_override=to_override,
        )
    except ValueError as exc:
        # _resolve_manager raises ValueError for unknown email, below the
        # AD-level cutoff, HODs, and managers without reports. All map to
        # 404 — the same surface the HOD router uses for invalid scopes.
        raise HTTPException(404, str(exc))
    return result


# ============================================================
# Bulk trigger (every eligible reporting manager)
# ============================================================

@digest_manager_router.post("/admin/send-manager-statements")
def admin_send_manager_bulk(
    request: Request,
    body: PeriodBody = Body(...),
    admin: dict = Depends(require_admin_flag),
):
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)
    _validate_period(body.month, body.year)

    # Defence: ?to= is meaningless for a bulk fan-out (a single inbox can't
    # represent every manager). Reject explicitly rather than silently ignore.
    if "to" in request.query_params:
        raise HTTPException(
            400,
            "?to= override is only supported on the single-manager endpoint "
            "(/admin/send-manager-statement/{manager_email}).",
        )

    # Audit marker — see admin_send_manager_one for the rationale.
    logger.info(
        "manager_statement: trigger=manual admin=%s bulk=true month=%s year=%s",
        (admin or {}).get("email"), body.month, body.year,
    )
    return generate_and_send_manager_bulk(month=body.month, year=body.year)
