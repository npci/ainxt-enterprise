# SPDX-License-Identifier: Apache-2.0
"""
Microsoft 365 tools — direct connector dispatch.

These tools do NOT run in the sandbox subprocess. ``ToolDispatcher.dispatch()``
in ``agent_factory/pipeline.py`` detects ``service == "microsoft_365"`` and
calls ``connector_registry.execute()`` directly in-process (the same path used
by the Buddy/Cowork orchestrator). This avoids the ``python -I`` sandbox
entirely, so there is no proxy interference and no HTTP round-trip to an
internal bridge endpoint.

``ConnectorEngine`` (via the registry) owns OAuth token get/refresh, scope
enforcement, pagination, compliance, and the Microsoft Graph API calls. Every
call runs against the requesting user's own M365 OAuth connection stored in
``ainxt.user_oauth_tokens``.

The ``code`` field in each tool spec is intentionally empty — it is never
executed. The tool name, description, input_schema, and ``service`` tag are
what matter: they seed ``tools_catalog`` and drive the CatalogPicker UI.
"""

# ---------------------------------------------------------------------------
# Shared shim — prepended to every tool's code. A per-tool ``_TOOL = "..."``
# line is injected by _make_tool() so run() knows which connector tool to call.
# ---------------------------------------------------------------------------

_SHIM = '''
import os, json, urllib.request, urllib.parse, urllib.error

# Bridge URL: derive from PLATFORM_BASE_URL + /ainxt/v1/api. Loopback default
# handles same-host deploys where PLATFORM_BASE_URL is unset.
_pbu = os.environ.get("PLATFORM_BASE_URL", "").rstrip("/") or "http://127.0.0.1:8000"
_BRIDGE_URL = _pbu + "/ainxt/v1/api"

# Bridge token: reuses AZURE_AD_CLIENT_SECRET (the platform's Azure AD app
# secret) as the internal-service bridge secret. Must match the value on the
# platform host — see routers/connectors_router._bridge_token_ok. Rotation is
# forced by Microsoft ~every 180 days; both hosts must be redeployed together.
_BRIDGE_TOKEN = os.environ.get("AZURE_AD_CLIENT_SECRET", "")

_USER_ID = os.environ.get("AINXT_USER_ID", "")

_HTTPS_PROXY = (
    os.environ.get("HTTPS_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("FORWARD_PROXY_URL")
    or ""
)

_M365_NOT_CONNECTED = (
    "You have not connected Microsoft 365 (or the session expired). "
    "Ask the user to connect/reconnect it under Settings \u2192 Connectors, then retry."
)

def _make_opener():
    import ssl
    # Security review F-07: TLS certificate verification is always enforced
    # (CWE-599). When REQUESTS_CA_BUNDLE / SSL_CERT_FILE points at a CA bundle
    # (a corporate CA, or the cert of a TLS-terminating proxy) we verify against
    # it; otherwise we verify against the system trust store. There is
    # deliberately no unverified fallback — an environment without a usable
    # trust anchor must be provisioned with one rather than silently accepting
    # forged certificates on a path that carries an access token.
    _ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or ""
    ctx = ssl.create_default_context(cafile=_ca_bundle or None)
    https_handler = urllib.request.HTTPSHandler(context=ctx)
    if _HTTPS_PROXY:
        proxy_handler = urllib.request.ProxyHandler({"https": _HTTPS_PROXY, "http": _HTTPS_PROXY})
        return urllib.request.build_opener(proxy_handler, https_handler)
    return urllib.request.build_opener(https_handler)

def _bridge_call(params: dict) -> dict:
    payload = json.dumps({
        "connector": "microsoft_365",
        "tool":      _TOOL,
        "params":    params or {},
        "user_id":   _USER_ID,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{_BRIDGE_URL}/connectors/execute",
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            "X-Bridge-Token": _BRIDGE_TOKEN,
        },
        method="POST",
    )
    opener = _make_opener()
    # 90s tolerates slow Graph responses (rate-limit retries, multi-region lag).
    with opener.open(req, timeout=90) as r:
        body = r.read()
        return json.loads(body) if body else {}

def run(inputs: dict) -> dict:
    if not _BRIDGE_TOKEN:
        return {"error": "Microsoft 365 bridge is not configured on this host "
                         "(AZURE_AD_CLIENT_SECRET is unset)."}
    if not _USER_ID:
        return {"error": "No user context; cannot call Microsoft 365."}
    try:
        resp = _bridge_call(inputs or {})
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="replace")[:400]
        except Exception:
            pass
        if e.code == 401:
            return {"error": "Microsoft 365 bridge rejected the request (auth). " + detail}
        if e.code == 422:
            return {"error": "Blocked by compliance policy. " + detail}
        return {"error": "Microsoft 365 bridge HTTP %s: %s" % (e.code, detail)}
    except urllib.error.URLError as e:
        return {"error": "Microsoft 365 bridge unreachable: %s" % (e.reason,)}
    except Exception as e:  # noqa: BLE001
        return {"error": "Microsoft 365 bridge call failed: %s" % (e,)}

    if not isinstance(resp, dict):
        return {"error": "Unexpected bridge response."}

    if resp.get("success"):
        # Reads carry items+count; writes carry just success. Pass both through
        # so the LLM sees whatever the connector returned.
        out = {"success": True}
        if "items" in resp:
            out["items"] = resp.get("items") or []
            out["count"] = resp.get("count", len(out["items"]))
        return out

    # success:false — surface the mapped guidance verbatim (reauth/scope/etc).
    err = resp.get("error") or _M365_NOT_CONNECTED
    code = resp.get("code") or ""
    if code in ("REAUTH_REQUIRED", "ACCESS_DENIED"):
        err = err or _M365_NOT_CONNECTED
    return {"error": err}
'''


def _make_tool(name: str, description: str, input_schema: dict, draft: bool = False) -> dict:
    """Build a canonical tool spec for a Microsoft 365 connector tool.

    ``code`` is empty because ``ToolDispatcher.dispatch()`` intercepts M365
    tools before the sandbox and calls ``connector_registry.execute()`` directly.
    """
    spec = {
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "code": "",  # dispatch() handles M365 in-process; sandbox never runs
        "service": "microsoft_365",
    }
    if draft:
        spec["draft"] = True
    return spec


# ---------------------------------------------------------------------------
# All 49 Microsoft 365 tools — mirrors connectors/seed.py exactly so the LLM
# sees identical guidance whether it uses Cowork or an AB Studio agent.
# ---------------------------------------------------------------------------

M365_TOOLS = [

    # ── Outlook ────────────────────────────────────────────────────────────

    _make_tool(
        "outlook_search_emails",
        "Search the user's Outlook inbox. Returns subject, sender, date, "
        "read-status, and a SHORT ~255-char body preview per email — NOT the "
        "full body (call outlook_read_email with an id for the full body). For "
        "free-text 'emails about X', use search_query; for 'emails from "
        "<person>', use from_address.",
        {
            "type": "object",
            "properties": {
                "search_query": {"type": "string", "description": "Free-text keyword(s) searched across subject + body + sender (use for 'emails about X'). When set, do NOT also set from_address/subject_contains/date_from/date_to — Graph cannot combine search with filters."},
                "from_address": {"type": "string", "description": "Sender email address (exact match)"},
                "from_name": {"type": "string", "description": "Sender display name (exact match)"},
                "subject_contains": {"type": "string", "description": "Substring that must appear in the subject line"},
                "date_from": {"type": "string", "description": "Start date YYYY-MM-DD. ONLY set if the user explicitly gives a date/range; OMIT for open-ended queries like 'recent emails'."},
                "date_to": {"type": "string", "description": "End date YYYY-MM-DD. Set only together with date_from for an explicit range; otherwise OMIT."},
                "is_read": {"type": "boolean", "description": "Filter by read/unread"},
                "limit": {"type": "integer", "description": "Max results (default 50, max 50)"},
            },
        },
    ),

    _make_tool(
        "outlook_count_emails",
        "Count emails from a specific sender or matching criteria.",
        {
            "type": "object",
            "properties": {
                "from_address": {"type": "string", "description": "Sender email address"},
                "from_name": {"type": "string", "description": "Sender name (e.g., 'CEO')"},
                "date_from": {"type": "string", "description": "Start date YYYY-MM-DD. ONLY set if the user explicitly gives a date/range; OMIT otherwise."},
                "date_to": {"type": "string", "description": "End date YYYY-MM-DD. Set only together with date_from; otherwise OMIT."},
            },
        },
    ),

    _make_tool(
        "outlook_read_email",
        "Open and read the full body of a specific email by its id.",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Email id (from outlook_search_emails)"},
            },
            "required": ["message_id"],
        },
    ),

    _make_tool(
        "outlook_reply_email",
        "Reply to an email. WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Email id to reply to"},
                "comment": {"type": "string", "description": "Reply text"},
            },
            "required": ["message_id", "comment"],
        },
    ),

    _make_tool(
        "outlook_reply_all_email",
        "Reply to ALL recipients (To + Cc) of an email. Graph resolves the full "
        "recipient list from the original message automatically. WRITE action — "
        "requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Email id to reply-all to"},
                "comment": {"type": "string", "description": "Reply text"},
            },
            "required": ["message_id", "comment"],
        },
    ),

    _make_tool(
        "outlook_forward_email",
        "Forward an email to recipients. WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Email id to forward"},
                "to": {"type": "string", "description": "Recipient email(s), comma-separated"},
                "comment": {"type": "string", "description": "Optional note to prepend"},
            },
            "required": ["message_id", "to"],
        },
    ),

    _make_tool(
        "outlook_send_mail",
        "Send an email from the user's Outlook account, optionally with generated, "
        "uploaded, or local documents attached. WRITE action — requires explicit "
        "user confirmation before sending.",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address(es). Separate multiple with comma, semicolon, or spaces."},
                "cc": {"type": "string", "description": "Optional CC recipient(s). Separate multiple with comma, semicolon, or spaces."},
                "bcc": {"type": "string", "description": "Optional BCC recipient(s)."},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body (plain text, or HTML if html=true)"},
                "html": {"type": "boolean", "description": "true = body is HTML."},
                "importance": {"type": "string", "description": "low | normal | high."},
                "attachment_id": {"type": "string", "description": "PREFERRED for user-uploaded files: when the user's message contains [attachment_id=<uuid>], pass that UUID here."},
                "attachment_ids": {"type": "array", "items": {"type": "string"}, "description": "PREFERRED for multiple user-uploaded files: pass all [attachment_id=<uuid>] values as an array."},
                "attachment_file_path": {"type": "string", "description": "Attach ONE file from the working folder by bare filename only (e.g. 'Summary.pptx'). Use only when no attachment_id is available."},
                "attachment_file_paths": {"type": "array", "items": {"type": "string"}, "description": "Attach MULTIPLE files from the working folder by bare filename only."},
                "attachment_job_id": {"type": "string", "description": "Use ONLY for documents YOU built with build_document (DOCJOB job id). Do NOT use for user-uploaded files."},
                "attachment_job_ids": {"type": "array", "items": {"type": "string"}, "description": "Use ONLY for MULTIPLE documents YOU built with build_document."},
                "attachment_artifact_id": {"type": "string", "description": "Attach a document built EARLIER (not in this turn) by its artifact_id."},
            },
            "required": ["to", "subject", "body"],
        },
    ),

    _make_tool(
        "outlook_list_folders",
        "List the user's Outlook mail folders (Inbox, Sent, Archive, custom folders) "
        "with their ids — use to get a destination folder id for moving mail.",
        {
            "type": "object",
            "properties": {},
        },
    ),

    _make_tool(
        "outlook_create_folder",
        "Create a new Outlook mail folder. WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "New folder display name."},
            },
            "required": ["name"],
        },
    ),

    _make_tool(
        "outlook_move_email",
        "Move an email to another folder (e.g. Archive, Deleted Items, or a custom "
        "folder id from outlook_list_folders). WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Email id to move."},
                "destination_folder_id": {"type": "string", "description": "Target folder id, or a well-known name like 'archive', 'deleteditems', 'inbox', 'junkemail'."},
            },
            "required": ["message_id", "destination_folder_id"],
        },
    ),

    _make_tool(
        "outlook_delete_email",
        "Delete an email (moves it to Deleted Items). WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Email id to delete."},
            },
            "required": ["message_id"],
        },
    ),

    _make_tool(
        "outlook_mark_email",
        "Update an email's state: mark read/unread, flag it, set importance, or set categories. "
        "WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Email id."},
                "is_read": {"type": "boolean", "description": "true = mark read, false = mark unread."},
                "flag": {"type": "string", "description": "notFlagged | flagged | complete."},
                "importance": {"type": "string", "description": "low | normal | high."},
                "categories": {"type": "string", "description": "Category name(s), comma/semicolon separated."},
            },
            "required": ["message_id"],
        },
    ),

    _make_tool(
        "outlook_create_draft",
        "Create a DRAFT email (not sent) — optionally with attachments. Returns the draft id; "
        "send later with outlook_send_draft. WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient(s), comma/semicolon separated."},
                "cc": {"type": "string", "description": "CC recipient(s)."},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "html": {"type": "boolean", "description": "true = body is HTML."},
                "attachment_id": {"type": "string", "description": "PREFERRED for user-uploaded files: pass the [attachment_id=<uuid>] UUID here."},
                "attachment_ids": {"type": "array", "items": {"type": "string"}, "description": "PREFERRED for multiple user-uploaded files."},
                "attachment_file_path": {"type": "string", "description": "Attach ONE file from the working folder by bare filename only."},
                "attachment_file_paths": {"type": "array", "items": {"type": "string"}, "description": "Attach MULTIPLE files from the working folder by bare filename only."},
                "attachment_job_id": {"type": "string", "description": "Use ONLY for documents YOU built with build_document (DOCJOB job id)."},
                "attachment_job_ids": {"type": "array", "items": {"type": "string"}, "description": "Multiple build_document job ids to attach."},
            },
            "required": ["subject"],
        },
    ),

    _make_tool(
        "outlook_send_draft",
        "Send a previously created draft email by its id. WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Draft email id (from outlook_create_draft)."},
            },
            "required": ["message_id"],
        },
    ),

    _make_tool(
        "outlook_list_attachments",
        "List the attachments on an email (names, sizes, ids).",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Email id."},
            },
            "required": ["message_id"],
        },
    ),

    # ── Calendar ───────────────────────────────────────────────────────────

    _make_tool(
        "calendar_list_events",
        "List/search the user's calendar events / meetings (expands recurring meetings). "
        "For cancel/reschedule, pass the narrowest date window plus subject_contains/search_query "
        "or attendee/organizer email to find the exact event id. Defaults to the next 30 days "
        "when no dates are given.",
        {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Window start YYYY-MM-DD. OMIT to default to today."},
                "date_to": {"type": "string", "description": "Window end YYYY-MM-DD. OMIT to default to ~30 days out."},
                "search_query": {"type": "string", "description": "Free-text meeting title/location keyword for client-side narrowing."},
                "subject_contains": {"type": "string", "description": "Meeting title keyword for client-side narrowing."},
                "organizer_email": {"type": "string", "description": "Organizer email to narrow results."},
                "attendee_email": {"type": "string", "description": "Attendee email to narrow results."},
                "limit": {"type": "integer", "description": "Max events"},
            },
        },
    ),

    _make_tool(
        "calendar_create_event",
        "Create/schedule a calendar event or Teams meeting. Supports required attendees "
        "and optional_attendees. WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Event title"},
                "start": {"type": "string", "description": "ISO datetime, e.g. 2026-07-01T10:00:00"},
                "end": {"type": "string", "description": "ISO datetime"},
                "attendees": {"type": "string", "description": "Required attendee email(s), comma/semicolon separated"},
                "optional_attendees": {"type": "string", "description": "Optional attendee email(s), comma/semicolon separated."},
                "body": {"type": "string", "description": "Event description"},
                "is_online_meeting": {"type": "boolean", "description": "true = add a Teams meeting link"},
                "timezone": {"type": "string", "description": "e.g. India Standard Time"},
            },
            "required": ["subject", "start", "end"],
        },
    ),

    _make_tool(
        "calendar_update_event",
        "Update / RESCHEDULE an existing calendar event by its id (changes time, subject, "
        "attendees, etc. on the SAME event — does NOT create a new invite). Use this to "
        "reschedule. WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The id of the event to update (from calendar_list_events)."},
                "subject": {"type": "string", "description": "New title (omit to keep current)."},
                "start": {"type": "string", "description": "New start ISO datetime (omit to keep)."},
                "end": {"type": "string", "description": "New end ISO datetime (omit to keep)."},
                "attendees": {"type": "string", "description": "Replace required attendee list — email(s), comma/semicolon separated."},
                "optional_attendees": {"type": "string", "description": "Replace optional attendee list — email(s), comma/semicolon separated."},
                "location": {"type": "string", "description": "New location."},
                "body": {"type": "string", "description": "New description."},
                "timezone": {"type": "string", "description": "e.g. India Standard Time"},
            },
            "required": ["event_id"],
        },
    ),

    _make_tool(
        "calendar_cancel_event",
        "Cancel a meeting you ORGANIZED and notify attendees (keeps a cancelled record). "
        "Use this when you are the organizer. WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The id of the event to cancel (from calendar_list_events)."},
                "comment": {"type": "string", "description": "Optional cancellation message sent to attendees."},
            },
            "required": ["event_id"],
        },
    ),

    _make_tool(
        "calendar_delete_event",
        "DELETE an event from the calendar by id. If you are the organizer this cancels "
        "the meeting; if you are an attendee it removes it from your calendar. Prefer "
        "calendar_cancel_event when you organized it and want attendees notified. "
        "WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The id of the event to delete (from calendar_list_events)."},
            },
            "required": ["event_id"],
        },
    ),

    _make_tool(
        "calendar_accept_event",
        "ACCEPT a meeting invitation you received. WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The id of the invite to accept (from calendar_list_events)."},
                "comment": {"type": "string", "description": "Optional note sent to the organizer."},
                "send_response": {"type": "boolean", "description": "Notify the organizer of your response (default true)."},
            },
            "required": ["event_id"],
        },
    ),

    _make_tool(
        "calendar_decline_event",
        "DECLINE a meeting invitation you received. WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The id of the invite to decline (from calendar_list_events)."},
                "comment": {"type": "string", "description": "Optional note sent to the organizer."},
                "send_response": {"type": "boolean", "description": "Notify the organizer of your response (default true)."},
            },
            "required": ["event_id"],
        },
    ),

    _make_tool(
        "calendar_tentative_event",
        "Respond TENTATIVELY (maybe) to a meeting invitation you received. "
        "WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The id of the invite (from calendar_list_events)."},
                "comment": {"type": "string", "description": "Optional note sent to the organizer."},
                "send_response": {"type": "boolean", "description": "Notify the organizer of your response (default true)."},
            },
            "required": ["event_id"],
        },
    ),

    _make_tool(
        "calendar_forward_event",
        "Forward a meeting/event to additional people. WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The id of the event to forward (from calendar_list_events)."},
                "to": {"type": "string", "description": "Recipient email(s), comma/semicolon separated."},
                "comment": {"type": "string", "description": "Optional note to include."},
            },
            "required": ["event_id", "to"],
        },
    ),

    _make_tool(
        "calendar_find_meeting_times",
        "Suggest available meeting time slots for a set of attendees (uses free/busy). "
        "Read-only — safe to call without confirmation.",
        {
            "type": "object",
            "properties": {
                "attendees": {"type": "string", "description": "Attendee email(s), comma/semicolon separated."},
                "meeting_duration": {"type": "string", "description": "ISO-8601 duration, e.g. PT30M for 30 minutes."},
                "minimum_attendee_percentage": {"type": "number", "description": "Min % of attendees that must be free (e.g. 100)."},
            },
            "required": ["attendees"],
        },
    ),

    _make_tool(
        "calendar_get_schedule",
        "Get free/busy availability for one or more people over a time window. "
        "Read-only — safe to call without confirmation.",
        {
            "type": "object",
            "properties": {
                "schedules": {"type": "string", "description": "Email address(es) to check, comma/semicolon separated."},
                "start": {"type": "string", "description": "Window start ISO datetime."},
                "end": {"type": "string", "description": "Window end ISO datetime."},
                "interval_minutes": {"type": "integer", "description": "Availability slot size in minutes (default 30)."},
                "timezone": {"type": "string", "description": "e.g. India Standard Time"},
            },
            "required": ["schedules", "start", "end"],
        },
    ),

    # ── Teams — Channels ───────────────────────────────────────────────────

    _make_tool(
        "teams_list_my_teams",
        "List the Teams the user is a member of (to get team_id).",
        {
            "type": "object",
            "properties": {},
        },
    ),

    _make_tool(
        "teams_list_channels",
        "List channels in a team (to get channel_id).",
        {
            "type": "object",
            "properties": {
                "team_id": {"type": "string", "description": "Teams team ID (from teams_list_my_teams)"},
            },
            "required": ["team_id"],
        },
    ),

    _make_tool(
        "teams_get_channel_messages",
        "Get recent messages from a Microsoft Teams channel.",
        {
            "type": "object",
            "properties": {
                "team_id": {"type": "string", "description": "Teams team ID"},
                "channel_id": {"type": "string", "description": "Channel ID"},
                "limit": {"type": "integer", "description": "Max messages"},
            },
            "required": ["team_id", "channel_id"],
        },
    ),

    _make_tool(
        "teams_send_message",
        "Post a message to a Microsoft Teams channel. WRITE action — requires "
        "explicit user confirmation before sending.",
        {
            "type": "object",
            "properties": {
                "team_id": {"type": "string", "description": "Teams team ID"},
                "channel_id": {"type": "string", "description": "Channel ID"},
                "message": {"type": "string", "description": "Message text to post"},
                "attachment_id": {"type": "string", "description": "PREFERRED for user-uploaded files: pass the [attachment_id=<uuid>] UUID here."},
                "attachment_ids": {"type": "array", "items": {"type": "string"}, "description": "PREFERRED for multiple user-uploaded files."},
                "attachment_file_path": {"type": "string", "description": "Attach ONE file from the working folder by bare filename only."},
                "attachment_file_paths": {"type": "array", "items": {"type": "string"}, "description": "Attach MULTIPLE files from the working folder by bare filename only."},
                "attachment_job_id": {"type": "string", "description": "Use ONLY for documents YOU built with build_document (DOCJOB job id)."},
                "attachment_job_ids": {"type": "array", "items": {"type": "string"}, "description": "Use ONLY for MULTIPLE documents YOU built with build_document."},
            },
            "required": ["team_id", "channel_id", "message"],
        },
    ),

    _make_tool(
        "teams_reply_channel_message",
        "Reply in-thread to a specific Teams channel message. WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "team_id": {"type": "string"},
                "channel_id": {"type": "string"},
                "message_id": {"type": "string", "description": "The parent message id to reply to."},
                "message": {"type": "string", "description": "Reply text."},
                "html": {"type": "boolean", "description": "true = message is HTML."},
            },
            "required": ["team_id", "channel_id", "message_id", "message"],
        },
    ),

    _make_tool(
        "teams_list_channel_members",
        "List members of a Teams channel.",
        {
            "type": "object",
            "properties": {
                "team_id": {"type": "string"},
                "channel_id": {"type": "string"},
            },
            "required": ["team_id", "channel_id"],
        },
    ),

    _make_tool(
        "teams_list_members",
        "List members of a Team.",
        {
            "type": "object",
            "properties": {
                "team_id": {"type": "string"},
            },
            "required": ["team_id"],
        },
    ),

    _make_tool(
        "teams_create_channel",
        "Create a new channel in a Team. WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "team_id": {"type": "string"},
                "name": {"type": "string", "description": "Channel display name."},
                "description": {"type": "string"},
                "membership_type": {"type": "string", "description": "standard | private | shared."},
            },
            "required": ["team_id", "name"],
        },
    ),

    # ── Teams — Chats (1:1 + group DMs) ───────────────────────────────────

    _make_tool(
        "teams_list_chats",
        "List the user's Teams chats (1:1 and GROUP). Returns each chat's topic + member "
        "names so you can find a group chat by name. Pass name_contains to filter by group "
        "name or member; the adapter scans deep across chat pages so older group chats are findable.",
        {
            "type": "object",
            "properties": {
                "name_contains": {"type": "string", "description": "Optional: filter chats whose topic/group name or a member name contains this text (case-insensitive)."},
                "limit": {"type": "integer", "description": "Max chats to scan"},
            },
        },
    ),

    _make_tool(
        "teams_get_chat_messages",
        "Get recent messages from a Teams 1:1 or group chat.",
        {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Chat id (from teams_list_chats)"},
                "limit": {"type": "integer", "description": "Max messages"},
            },
            "required": ["chat_id"],
        },
    ),

    _make_tool(
        "teams_send_chat_message",
        "Send a message to a Teams 1:1 or group chat. WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Chat id (from teams_list_chats)"},
                "message": {"type": "string", "description": "Message text to send"},
                "attachment_id": {"type": "string", "description": "PREFERRED for user-uploaded files: pass the [attachment_id=<uuid>] UUID here."},
                "attachment_ids": {"type": "array", "items": {"type": "string"}, "description": "PREFERRED for multiple user-uploaded files."},
                "attachment_file_path": {"type": "string", "description": "Attach ONE file from the working folder by bare filename only."},
                "attachment_file_paths": {"type": "array", "items": {"type": "string"}, "description": "Attach MULTIPLE files from the working folder by bare filename only."},
                "attachment_job_id": {"type": "string", "description": "Use ONLY for documents YOU built with build_document (DOCJOB job id)."},
                "attachment_job_ids": {"type": "array", "items": {"type": "string"}, "description": "Use ONLY for MULTIPLE documents YOU built with build_document."},
            },
            "required": ["chat_id", "message"],
        },
    ),

    _make_tool(
        "teams_start_chat",
        "Create or retrieve a 1:1 Teams chat with a colleague by exact confirmed work "
        "email/UPN or selected email from people_search — not a bare name. Graph is "
        "idempotent and returns the existing chat if one already exists. If people_search "
        "returns multiple matches, ask the user which exact person to use before calling this. "
        "Returns a chat_id to use with teams_send_chat_message. WRITE action — requires "
        "explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "user_email": {"type": "string", "description": "The colleague's work email or user id (from people_search)."},
            },
            "required": ["user_email"],
        },
    ),

    _make_tool(
        "teams_get_chat_members",
        "List the members of a Teams chat (1:1 or group).",
        {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
            },
            "required": ["chat_id"],
        },
    ),

    # ── Teams — Meetings & Presence ────────────────────────────────────────

    _make_tool(
        "teams_list_meetings",
        "List the user's Teams online meetings.",
        {
            "type": "object",
            "properties": {},
        },
    ),

    _make_tool(
        "teams_create_online_meeting",
        "Create a Teams online meeting and get its join link. WRITE action — requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "start": {"type": "string", "description": "ISO datetime."},
                "end": {"type": "string", "description": "ISO datetime."},
            },
            "required": ["subject"],
        },
    ),

    _make_tool(
        "teams_list_transcripts",
        "List available transcripts (metadata only) for one of the user's online meetings. "
        "Use after a meeting to find its transcript before summarizing.",
        {
            "type": "object",
            "properties": {
                "meeting_id": {"type": "string", "description": "Graph onlineMeeting id"},
            },
            "required": ["meeting_id"],
        },
    ),

    _make_tool(
        "teams_get_transcript_content",
        "Read the full text of a meeting transcript.",
        {
            "type": "object",
            "properties": {
                "meeting_id": {"type": "string", "description": "Graph onlineMeeting id"},
                "transcript_id": {"type": "string", "description": "Transcript id (from teams_list_transcripts)"},
            },
            "required": ["meeting_id", "transcript_id"],
        },
    ),

    _make_tool(
        "teams_get_presence",
        "Get the signed-in user's Teams presence (Available/Busy/Away/etc.).",
        {
            "type": "object",
            "properties": {},
        },
    ),

    _make_tool(
        "teams_get_user_presence",
        "Get another user's Teams presence by their user id.",
        {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "The user's id (from people_search)."},
            },
            "required": ["user_id"],
        },
    ),

    # ── People & Org ───────────────────────────────────────────────────────

    _make_tool(
        "people_search",
        "Find any colleague's email/profile by exact email/UPN first, then "
        "name/keyword across the org directory. If multiple same-name people are "
        "returned, ask the user which exact person to use before any outbound "
        "Teams, Outlook, or Calendar action.",
        {
            "type": "object",
            "properties": {
                "search_query": {"type": "string", "description": "Name or keyword to search across the org directory"},
                "limit": {"type": "integer", "description": "Max results"},
            },
        },
    ),

    _make_tool(
        "org_direct_reports",
        "List a person's DIRECT reports (the people who report to them). Omit user_email "
        "for the signed-in user's own reports. To get INDIRECT reports, call this again "
        "for each returned person.",
        {
            "type": "object",
            "properties": {
                "user_email": {"type": "string", "description": "Optional: the manager's email/id whose reports you want. Omit for yourself."},
            },
        },
    ),

    _make_tool(
        "org_get_manager",
        "Get a person's manager. Omit user_email for the signed-in user.",
        {
            "type": "object",
            "properties": {
                "user_email": {"type": "string", "description": "Optional: the person's email/id. Omit for yourself."},
            },
        },
    ),

    # ── OneDrive (internal) ────────────────────────────────────────────────

    _make_tool(
        "onedrive_upload",
        "Internal: upload a file to the user's OneDrive and return its shareable webUrl. "
        "Used to host files for Teams message attachments.",
        {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "content_bytes": {"type": "string", "description": "base64-encoded file bytes"},
                "content_type": {"type": "string"},
            },
            "required": ["filename", "content_bytes"],
        },
    ),
]
