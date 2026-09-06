# SPDX-License-Identifier: MIT
# ============================================================
# ZOHO ROUTER — /hr
#
# POST /hr/leave         → apply leave via Zoho People API
# GET  /hr/leave/health  → verify Zoho token is valid
# ============================================================

import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.logger import logger

router = APIRouter(prefix="/hr", tags=["zoho", "hr"])


# ── Date helpers ──────────────────────────────────────────────

_MONTH_ABBR = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr",
    5: "May", 6: "Jun", 7: "Jul", 8: "Aug",
    9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}


def _to_zoho_date(dt: datetime) -> str:
    """Convert datetime → DD-Mon-YYYY (Zoho's expected format)."""
    return f"{dt.day:02d}-{_MONTH_ABBR[dt.month]}-{dt.year}"


def _parse_date(date_str: str) -> datetime:
    """
    Parse a date string into datetime.
    Accepts ISO (2026-03-10), DD-Mon-YYYY (10-Mar-2026), or
    natural language: 'today', 'tomorrow'.
    """
    today = datetime.today()

    s = date_str.strip().lower()
    if s == "today":
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)

    # ISO: 2026-03-10
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        pass

    # DD-Mon-YYYY: 10-Mar-2026
    try:
        return datetime.strptime(date_str, "%d-%b-%Y")
    except ValueError:
        pass

    raise ValueError(
        f"Cannot parse date '{date_str}'. "
        "Use YYYY-MM-DD, DD-Mon-YYYY, 'today', or 'tomorrow'."
    )


# ── Request model ─────────────────────────────────────────────

class LeaveRequest(BaseModel):
    employee_id: str = "1"
    from_date: str           # YYYY-MM-DD, DD-Mon-YYYY, 'today', 'tomorrow'
    to_date: Optional[str] = None   # defaults to from_date if omitted
    reason: str = "Personal"
    leave_type: str = "Casual Leave"


# ── POST /api/leave ───────────────────────────────────────────

@router.post("/leave")
async def apply_leave(req: LeaveRequest):
    """Apply leave in Zoho People via P_ApplyLeave form."""
    try:
        from integrations.zoho_people import apply_leave as _apply

        from_dt = _parse_date(req.from_date)
        to_dt   = _parse_date(req.to_date) if req.to_date else from_dt

        zoho_from = _to_zoho_date(from_dt)
        zoho_to   = _to_zoho_date(to_dt)

        result = _apply(
            employee_id=req.employee_id,
            from_date=zoho_from,
            to_date=zoho_to,
            reason=req.reason,
            leave_type=req.leave_type,
        )

        return {
            "status":  "success",
            "message": "Leave applied in Zoho",
            "from":    zoho_from,
            "to":      zoho_to,
            "zoho":    result,
        }

    except RuntimeError as e:
        logger.error(f"zoho_router: leave application failed → {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"zoho_router: unexpected error → {e}")
        raise HTTPException(status_code=500, detail=f"Leave application failed: {e}")


# ── GET /api/leave/health ─────────────────────────────────────

@router.get("/leave/balance")
async def leave_balance(employee_id: str = ""):
    """Get leave balance for an employee from Zoho People."""
    try:
        from integrations.zoho_people import get_leave_types, DEFAULT_EMPLOYEE_ID
        emp = employee_id or DEFAULT_EMPLOYEE_ID
        types = get_leave_types(emp)
        return {
            "status":   "success",
            "employee": emp,
            "balances": [
                {
                    "name":    lt.get("Name"),
                    "balance": lt.get("BalanceCount"),
                    "availed": lt.get("AvailedCount"),
                    "unit":    lt.get("Unit"),
                }
                for lt in types
            ],
        }
    except Exception as e:
        logger.error(f"zoho_router: leave balance failed → {e}")
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/leave/pending")
async def pending_leaves(employee_id: str = ""):
    """Get all leave records for an employee from Zoho People."""
    try:
        from integrations.zoho_people import get_pending_leaves, DEFAULT_EMPLOYEE_ID
        emp    = employee_id or DEFAULT_EMPLOYEE_ID
        leaves = get_pending_leaves(emp)
        return {"status": "success", "employee": emp, "leaves": leaves}
    except Exception as e:
        logger.error(f"zoho_router: pending leaves failed → {e}")
        raise HTTPException(status_code=503, detail=str(e))


class CancelRequest(BaseModel):
    reason: str = "Cancelled via AiNxt"


@router.post("/leave/cancel/{record_id}")
async def cancel_leave(record_id: str, req: CancelRequest = CancelRequest()):
    """Cancel a leave record by its Zoho record ID."""
    try:
        from integrations.zoho_people import cancel_leave as _cancel
        result = _cancel(record_id, reason=req.reason)
        return {"status": "success", "record_id": record_id, "zoho": result}
    except Exception as e:
        logger.error(f"zoho_router: cancel leave failed → {e}")
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/leave/health")
async def zoho_health():
    """Verify Zoho connectivity — refreshes access token first, then fetches employee records."""
    try:
        from integrations.zoho_people import refresh_access_token, get_employee_records
        # Always refresh so an expired cached token doesn't block the check
        new_token = refresh_access_token()
        data = get_employee_records()
        return {
            "status":  "ok",
            "message": "Zoho token refreshed and valid",
            "token_prefix": new_token[:8] + "...",
            "sample": str(data)[:200],
        }
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"zoho_router: health check failed → {e}")
        raise HTTPException(status_code=503, detail=f"Zoho health check failed: {e}")
