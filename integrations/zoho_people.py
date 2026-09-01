# SPDX-License-Identifier: Apache-2.0
# ============================================================
# ZOHO PEOPLE INTEGRATION
# Handles OAuth2 token management and leave application via
# the Zoho People API.
#
# Required env vars:
#   ZOHO_CLIENT_ID
#   ZOHO_CLIENT_SECRET
#   ZOHO_REFRESH_TOKEN
#   ZOHO_ACCESS_TOKEN          (initial / cached token)
#   ZOHO_ACCOUNTS_URL          (default: https://accounts.zoho.in)
#   ZOHO_EMPLOYEE_ID           (Zoho record ID of the employee)
#   ZOHO_LEAVE_TYPE_CASUAL     (record ID for Casual Leave)
#   ZOHO_LEAVE_TYPE_EARNED     (record ID for Earned Leave)
#   ZOHO_LEAVE_TYPE_SICK       (record ID for Sick Leave)
# ============================================================

import json
import os
import threading
from typing import Optional

import requests

from core.logger import logger

# ── Config ────────────────────────────────────────────────────
_PEOPLE_DOMAIN = "https://people.zoho.in"
_ACCOUNTS_URL  = os.getenv("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in")
_CLIENT_ID     = os.getenv("ZOHO_CLIENT_ID", "")
_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET", "")
_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN", "")

# Default employee and leave type IDs (override per-request)
DEFAULT_EMPLOYEE_ID = os.getenv("ZOHO_EMPLOYEE_ID", "")

# Leave type name → record ID map (populated from env)
_LEAVE_TYPE_MAP = {
    "casual leave":        os.getenv("ZOHO_LEAVE_TYPE_CASUAL", ""),
    "earned leave":        os.getenv("ZOHO_LEAVE_TYPE_EARNED", ""),
    "sick leave":          os.getenv("ZOHO_LEAVE_TYPE_SICK", ""),
}

# In-memory token cache
_token_lock          = threading.Lock()
_cached_access_token: Optional[str] = os.getenv("ZOHO_ACCESS_TOKEN", "") or None


# ── Token management ──────────────────────────────────────────

def get_access_token() -> str:
    global _cached_access_token
    with _token_lock:
        if _cached_access_token:
            return _cached_access_token
    return refresh_access_token()


def refresh_access_token() -> str:
    global _cached_access_token

    if not _CLIENT_ID or not _CLIENT_SECRET or not _REFRESH_TOKEN:
        raise RuntimeError(
            "Zoho credentials not configured. "
            "Set ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN."
        )

    resp = requests.post(f"{_ACCOUNTS_URL}/oauth/v2/token", data={
        "grant_type":    "refresh_token",
        "client_id":     _CLIENT_ID,
        "client_secret": _CLIENT_SECRET,
        "refresh_token": _REFRESH_TOKEN,
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if "access_token" not in data:
        raise RuntimeError(f"Zoho token refresh failed: {data}")

    with _token_lock:
        _cached_access_token = data["access_token"]

    logger.info("ZohoPeople: access token refreshed")
    return _cached_access_token


def _auth_header() -> dict:
    return {"Authorization": f"Zoho-oauthtoken {get_access_token()}"}


def _resolve_leave_type(leave_type: str) -> str:
    """
    Resolve leave type name to Zoho record ID.
    Accepts record ID directly (if it looks numeric) or a name like 'Casual Leave'.
    """
    if leave_type.isdigit() or (len(leave_type) > 10 and leave_type.isdigit()):
        return leave_type  # already an ID
    mapped = _LEAVE_TYPE_MAP.get(leave_type.lower().strip())
    if mapped:
        return mapped
    # Fallback: default to casual leave
    return _LEAVE_TYPE_MAP.get("casual leave", leave_type)


# ── Leave application ─────────────────────────────────────────

def apply_leave(
    employee_id: str,
    from_date: str,
    to_date: str,
    reason: str,
    leave_type: str = "Casual Leave",
) -> dict:
    """
    Apply leave in Zoho People.

    Args:
        employee_id: Zoho record ID (e.g. "330071000000294005") or use DEFAULT_EMPLOYEE_ID
        from_date:   "DD-Mon-YYYY" (e.g. "10-Mar-2026")
        to_date:     "DD-Mon-YYYY"
        reason:      Reason string
        leave_type:  Name ("Casual Leave") or record ID

    Returns:
        Zoho API response dict.
    """
    emp_id       = employee_id if employee_id != "1" else DEFAULT_EMPLOYEE_ID or employee_id
    leave_type_id = _resolve_leave_type(leave_type)

    url = f"{_PEOPLE_DOMAIN}/people/api/forms/json/leave/insertRecord"

    input_data = json.dumps({
        "Employee_ID": emp_id,
        "Leavetype":   leave_type_id,
        "From":        from_date,
        "To":          to_date,
        "days": {
            from_date: {"LeaveCount": 1, "Session": 1}
        },
    })

    logger.info(
        f"ZohoPeople: apply_leave emp={emp_id} "
        f"from={from_date} to={to_date} type={leave_type_id}"
    )

    def _do_request(token_refreshed=False):
        resp = requests.get(
            url,
            headers=_auth_header(),
            params={"inputData": input_data},
            timeout=15,
        )
        if resp.status_code == 401 and not token_refreshed:
            logger.info("ZohoPeople: 401 — refreshing token and retrying")
            global _cached_access_token
            with _token_lock:
                _cached_access_token = None
            refresh_access_token()
            return _do_request(token_refreshed=True)
        resp.raise_for_status()
        return resp.json()

    result = _do_request()
    logger.info(f"ZohoPeople: response → {result}")
    return result


# ── Employee records ──────────────────────────────────────────

def get_employee_records() -> dict:
    """Fetch employee records — used to verify token is valid."""
    resp = requests.get(
        f"{_PEOPLE_DOMAIN}/people/api/forms/employee/getRecords",
        headers=_auth_header(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# ── Leave type list ───────────────────────────────────────────

def get_pending_leaves(employee_id: str = "") -> list:
    """
    Fetch all leave records for an employee.
    Returns list of leave dicts with keys: record_id, from, to, type, status, days.
    """
    emp = employee_id or DEFAULT_EMPLOYEE_ID

    def _do_request(token_refreshed=False):
        resp = requests.get(
            f"{_PEOPLE_DOMAIN}/people/api/forms/leave/getRecords",
            headers=_auth_header(),
            params={"empId": emp, "sIndex": 1, "limit": 20},
            timeout=10,
        )
        if resp.status_code == 401 and not token_refreshed:
            global _cached_access_token
            with _token_lock:
                _cached_access_token = None
            refresh_access_token()
            return _do_request(token_refreshed=True)
        resp.raise_for_status()
        return resp.json()

    data = _do_request()
    raw = data.get("response", {}).get("result", [])

    leaves = []
    for entry in raw:
        for record_id, fields_list in entry.items():
            if not fields_list:
                continue
            f = fields_list[0]
            leaves.append({
                "record_id": record_id,
                "from":      f.get("From", ""),
                "to":        f.get("To", ""),
                "type":      f.get("Leavetype", ""),
                "status":    f.get("ApprovalStatus", ""),
                "days":      f.get("Daystaken", ""),
                "reason":    f.get("Reasonforleave", ""),
                "requested": f.get("DateOfRequest", ""),
            })
    return leaves


def cancel_leave(record_id: str, reason: str = "Cancelled via AiNxt") -> dict:
    """
    Cancel a leave record by its Zoho record ID.
    Uses PATCH /api/v2/leavetracker/leaves/records/cancel/<record_id>
    """
    def _do_request(token_refreshed=False):
        resp = requests.patch(
            f"{_PEOPLE_DOMAIN}/api/v2/leavetracker/leaves/records/cancel/{record_id}",
            headers=_auth_header(),
            data={"reason": reason},
            timeout=10,
        )
        if resp.status_code == 401 and not token_refreshed:
            global _cached_access_token
            with _token_lock:
                _cached_access_token = None
            refresh_access_token()
            return _do_request(token_refreshed=True)
        resp.raise_for_status()
        return resp.json()

    result = _do_request()
    logger.info(f"ZohoPeople: cancel_leave record={record_id} → {result}")
    return result


def get_leave_types(employee_id: str = "") -> list:
    """Fetch all leave types with their record IDs."""
    emp = employee_id or DEFAULT_EMPLOYEE_ID

    def _do_request(token_refreshed=False):
        resp = requests.get(
            f"{_PEOPLE_DOMAIN}/people/api/leave/getLeaveTypeDetails",
            headers=_auth_header(),
            params={"userId": emp},
            timeout=10,
        )
        if resp.status_code == 401 and not token_refreshed:
            global _cached_access_token
            with _token_lock:
                _cached_access_token = None
            refresh_access_token()
            return _do_request(token_refreshed=True)
        resp.raise_for_status()
        return resp.json().get("response", {}).get("result", [])

    return _do_request()
