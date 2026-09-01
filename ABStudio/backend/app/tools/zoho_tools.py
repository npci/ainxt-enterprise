# SPDX-License-Identifier: Apache-2.0
"""
Zoho tools — Zoho People (HR leave management) and Zoho CRM (customer records).

Env vars:
  ZOHO_PEOPLE_URL    — Zoho People API base URL
  ZOHO_CRM_URL       — Zoho CRM API base URL
  ZOHO_ACCESS_TOKEN  — OAuth2 access token for Zoho APIs
Each tool's `code` string is self-contained and runs in the sandbox subprocess.

NOTE: These tools are marked `"draft": True` — they are present in the catalog
but will NOT be seeded into the database until the Zoho integration is
configured and the draft flag is removed.
"""

_HELPERS = '''
import os, json, urllib.request, urllib.error, urllib.parse

def _zoho_people_base():
    return os.environ.get("ZOHO_PEOPLE_URL", "https://people.zoho.com/people/api").rstrip("/")

def _zoho_crm_base():
    return os.environ.get("ZOHO_CRM_URL", "https://www.zohoapis.com/crm/v2").rstrip("/")

def _zoho_token():
    token = os.environ.get("ZOHO_ACCESS_TOKEN", "")
    if not token:
        raise PermissionError("ZOHO_ACCESS_TOKEN env var required.")
    return token

def _request(method, url, payload=None, params=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Authorization": f"Zoho-oauthtoken {_zoho_token()}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        # Enterprise-grade timeout: 60s (was 20s) to survive Zoho People /
        # CRM slow queries and OAuth token-refresh latency when called from
        # attached or generate-with-AI flows. Expired tokens still surface
        # as HTTP 401 from the catch block below.
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise Exception(f"HTTP {e.code}: {body[:400]}")
    except urllib.error.URLError as e:
        raise Exception(f"Zoho unreachable: {e.reason}")
'''

ZOHO_TOOLS = [
    # ------------------------------------------------------------------ #
    # zoho_apply_leave                                                     #
    # ------------------------------------------------------------------ #
    {
        "name": "zoho_apply_leave",
        "draft": True,
        "description": "Apply for leave in Zoho People HR system on behalf of an employee.",
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string", "description": "Zoho People employee ID"},
                "from_date":   {"type": "string", "description": "Leave start date in DD-Mon-YYYY format e.g. 01-Jun-2025"},
                "to_date":     {"type": "string", "description": "Leave end date in DD-Mon-YYYY format e.g. 05-Jun-2025"},
                "reason":      {"type": "string", "description": "Reason for leave"},
                "leave_type":  {"type": "string", "description": "Leave type e.g. Casual Leave, Sick Leave", "default": "Casual Leave"},
            },
            "required": ["employee_id", "from_date", "to_date", "reason"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        employee_id = inputs.get("employee_id", "")
        from_date   = inputs.get("from_date", "")
        to_date     = inputs.get("to_date", "")
        reason      = inputs.get("reason", "")
        leave_type  = inputs.get("leave_type", "Casual Leave")
        url         = f"{_zoho_people_base()}/forms/leave/insertRecord"
        payload     = {
            "inputData": json.dumps({
                "Employee_ID": employee_id,
                "Leavetype":   leave_type,
                "From":        from_date,
                "To":          to_date,
                "Reason":      reason,
            })
        }
        result = _request("POST", url, payload)
        status = result.get("response", {}).get("status", "unknown")
        msg    = result.get("response", {}).get("message", str(result))
        return {"result": f"Leave applied for {employee_id} ({from_date} to {to_date}): {status} — {msg}", "status": status}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # zoho_lookup                                                          #
    # ------------------------------------------------------------------ #
    {
        "name": "zoho_lookup",
        "draft": True,
        "description": "Retrieve customer account details and interaction history from Zoho CRM.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "Zoho CRM account/contact ID"},
            },
            "required": ["customer_id"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        customer_id = inputs.get("customer_id", "")
        url         = f"{_zoho_crm_base()}/Accounts/{customer_id}"
        result      = _request("GET", url)
        data_list   = result.get("data", [])
        if not data_list:
            return {"error": f"No CRM record found for customer_id: {customer_id}"}
        record = data_list[0]
        data   = {
            "id":          record.get("id"),
            "name":        record.get("Account_Name", record.get("Full_Name", "")),
            "email":       record.get("Email", ""),
            "phone":       record.get("Phone", ""),
            "status":      record.get("Account_Status", record.get("Lead_Status", "")),
            "description": (record.get("Description") or "")[:300],
        }
        return {
            "result": f"Customer {data[\'name\']} (id={data[\'id\']}) — status: {data[\'status\']}",
            **data,
        }
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # zoho_update                                                          #
    # ------------------------------------------------------------------ #
    {
        "name": "zoho_update",
        "draft": True,
        "description": "Update a customer record in Zoho CRM with a new status and optional case notes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "Zoho CRM account/contact ID"},
                "status":      {"type": "string", "description": "New account/lead status"},
                "notes":       {"type": "string", "description": "Case notes to append to the description"},
                "sla_tag":     {"type": "string", "description": "SLA tag to set (optional)"},
            },
            "required": ["customer_id", "status"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        customer_id = inputs.get("customer_id", "")
        status      = inputs.get("status", "")
        notes       = inputs.get("notes", "")
        sla_tag     = inputs.get("sla_tag", "")
        url         = f"{_zoho_crm_base()}/Accounts/{customer_id}"
        record      = {"Account_Status": status}
        if notes:
            record["Description"] = notes
        if sla_tag:
            record["SLA_Tag"] = sla_tag
        payload = {"data": [record]}
        result  = _request("PUT", url, payload)
        resp    = (result.get("data") or [{}])[0]
        code    = resp.get("code", "unknown")
        msg     = resp.get("message", str(result))
        return {"result": f"Customer {customer_id} updated (status={status}): {code} — {msg}", "code": code}
    except Exception as e:
        return {"error": str(e)}
''',
    },
]
