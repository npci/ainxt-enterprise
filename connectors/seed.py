# SPDX-License-Identifier: MIT
"""
Seed connector definitions — inserted at platform startup.

These are the built-in connectors shipped with AiNxt.
Custom connectors can be added by admins via POST /connectors/definitions.
"""
from __future__ import annotations

import json
from core.logger import logger

_ENV_AZURE_OAUTH_CREDENTIAL    = "AZURE_AD_CLIENT_SECRET"
_ENV_GOOGLE_OAUTH_CREDENTIAL   = "GOOGLE_CLIENT_SECRET"
_ENV_SLACK_OAUTH_CREDENTIAL    = "SLACK_CLIENT_SECRET"
_ENV_GITHUB_OAUTH_CREDENTIAL   = "GITHUB_OAUTH_CLIENT_SECRET"
_ENV_JIRA_API_CREDENTIAL       = "JIRA_API_TOKEN"


# ── Connector definitions ──────────────────────────────────────────────────────

SEED_CONNECTORS = [
    {
        "name": "microsoft_365",
        "display_name": "Microsoft 365",
        "description": "Connect to Outlook email, Teams channels, and Microsoft calendar via Microsoft Graph API.",
        "icon_url": "/icons/microsoft365.svg",
        "category": "productivity",
        "auth_type": "oauth2",
        "has_custom_adapter": True,
        "rate_limit_per_min": 60,
        "is_builtin": True,
        "base_url": "https://graph.microsoft.com",
        "auth_config": {
            "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            "client_id_env": "AZURE_AD_CLIENT_ID",
            "client_secret_env": _ENV_AZURE_OAUTH_CREDENTIAL,
            "pkce": True,
            "scopes": [
                "openid", "profile", "email", "offline_access",
                "Mail.Read", "Mail.ReadWrite", "Mail.Send",
                "User.Read", "User.Read.All",
                "Calendars.Read", "Calendars.ReadWrite", "Calendars.Read.Shared",
                "ChannelMessage.Read.All", "ChannelMessage.Send",
                "Team.ReadBasic.All", "Channel.ReadBasic.All",
                "Channel.Create", "ChannelMember.Read.All", "TeamMember.Read.All",
                "Chat.Read", "Chat.ReadWrite", "Chat.Create",
                "Files.ReadWrite",
                "OnlineMeetings.Read", "OnlineMeetings.ReadWrite",
                "Presence.Read", "Presence.Read.All",
                "People.Read",
                # Post-meeting intelligence (scope §4/§5). NOTE: transcript CONTENT
                # is read by the meeting worker using APPLICATION permissions
                # (centralized admin consent + Application Access Policy), not these
                # delegated scopes — see integrations/graph_app_client.py.
                "OnlineMeetingTranscript.Read.All",
            ],
            "extra_params": {"response_mode": "query"},
        },
        "tools": [
            {
                "name": "outlook_search_emails",
                "description": "Search the user's Outlook inbox. Returns subject, sender, date, read-status, and a SHORT ~255-char body preview per email — NOT the full body (call outlook_read_email with an id for the full body). For free-text 'emails about X', use search_query; for 'emails from <person>', use from_address.",
                "method": "GET",
                "path": "/v1.0/me/messages",
                "requires_scopes": ["Mail.Read"],
                "cache_ttl_s": 300,
                "paginated": True,
                "max_items": 50,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "search_query": {"type": "string", "description": "Free-text keyword(s) searched across subject + body + sender (use for 'emails about X'). When set, do NOT also set from_address/subject_contains/date_from/date_to — Graph cannot combine search with filters."},
                        "from_address": {"type": "string", "description": "Sender email address (exact match)"},
                        "from_name": {"type": "string", "description": "Sender display name (exact match)"},
                        "subject_contains": {"type": "string", "description": "Substring that must appear in the subject line"},
                        "date_from": {"type": "string", "description": "Start date YYYY-MM-DD. ONLY set if the user explicitly gives a date/range; OMIT for open-ended queries like 'recent emails' (otherwise results are wrongly limited to that single day)."},
                        "date_to": {"type": "string", "description": "End date YYYY-MM-DD. Set only together with date_from for an explicit range; otherwise OMIT."},
                        "is_read": {"type": "boolean", "description": "Filter by read/unread"},
                        "limit": {"type": "integer", "description": "Max results (default 50, max 50)"},
                    },
                },
            },
            {
                "name": "outlook_count_emails",
                "description": "Count emails from a specific sender or matching criteria.",
                "method": "GET",
                "path": "/v1.0/me/messages",
                "requires_scopes": ["Mail.Read"],
                "cache_ttl_s": 300,
                "paginated": True,
                "max_items": 50,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "from_address": {"type": "string", "description": "Sender email address"},
                        "from_name": {"type": "string", "description": "Sender name (e.g., 'CEO')"},
                        "date_from": {"type": "string", "description": "Start date YYYY-MM-DD. ONLY set if the user explicitly gives a date/range; OMIT otherwise (else it counts only that single day)."},
                        "date_to": {"type": "string", "description": "End date YYYY-MM-DD. Set only together with date_from; otherwise OMIT."},
                    },
                },
            },
            {
                "name": "teams_get_channel_messages",
                "description": "Get recent messages from a Microsoft Teams channel.",
                "method": "GET",
                "path": "/v1.0/teams/{team_id}/channels/{channel_id}/messages",
                "requires_scopes": ["ChannelMessage.Read.All"],
                "cache_ttl_s": 60,
                "paginated": True,
                "max_items": 100,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "team_id": {"type": "string", "description": "Teams team ID"},
                        "channel_id": {"type": "string", "description": "Channel ID"},
                        "limit": {"type": "integer", "description": "Max messages"},
                    },
                    "required": ["team_id", "channel_id"],
                },
            },
            # NOTE: teams_list_meetings (GET /me/onlineMeetings) was REMOVED — Graph
            # returns 400 on that endpoint without a JoinWebUrl/videoTeleconferenceId
            # filter; it cannot list meetings. Use calendar_list_events instead.
            {
                "name": "teams_list_transcripts",
                "description": "List available transcripts (metadata only) for one of the user's online meetings. Use after a meeting to find its transcript before summarizing.",
                "method": "GET",
                "path": "/v1.0/me/onlineMeetings/{meeting_id}/transcripts",
                "requires_scopes": ["OnlineMeetingTranscript.Read.All"],
                "cache_ttl_s": 120,
                "paginated": False,
                "max_items": 20,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "meeting_id": {"type": "string", "description": "Graph onlineMeeting id"},
                    },
                    "required": ["meeting_id"],
                },
            },
            {
                "name": "outlook_send_mail",
                "description": "Send an email from the user's Outlook account, optionally with generated, uploaded, or local documents attached. WRITE action — requires explicit user confirmation before sending.",
                "method": "POST",
                "path": "/v1.0/me/sendMail",
                "requires_scopes": ["Mail.Send"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient email address(es). Separate multiple with comma, semicolon, or spaces."},
                        "cc": {"type": "string", "description": "Optional CC recipient(s). Separate multiple with comma, semicolon, or spaces."},
                        "bcc": {"type": "string", "description": "Optional BCC recipient(s)."},
                        "subject": {"type": "string", "description": "Email subject"},
                        "body": {"type": "string", "description": "Email body (plain text, or HTML if html=true)"},
                        "html": {"type": "boolean", "description": "true = body is HTML."},
                        "importance": {"type": "string", "description": "low | normal | high."},
                        "attachment_id": {"type": "string", "description": "PREFERRED for user-uploaded files: when the user's message contains [attachment_id=<uuid>] or [File: name [attachment_id=<uuid>]], pass that UUID here. Do NOT use attachment_file_path or attachment_job_id for these files."},
                        "attachment_ids": {"type": "array", "items": {"type": "string"}, "description": "PREFERRED for multiple user-uploaded files: when the message contains multiple [attachment_id=<uuid>] values, pass them all here as an array. Do NOT use attachment_file_paths for these."},
                        "attachment_file_path": {"type": "string", "description": "Attach ONE file from the working folder by bare filename only (e.g. 'Summary.pptx') — do NOT pass a full Windows path like C:\\Users\\... The server locates the file automatically. Use only when no attachment_id is available."},
                        "attachment_file_paths": {"type": "array", "items": {"type": "string"}, "description": "Attach MULTIPLE files from the working folder by bare filename only. Use only when no attachment_ids are available."},
                        "attachment_job_id": {"type": "string", "description": "Use ONLY for documents YOU built with build_document: pass the job id from the [DOCJOB:<job_id>:...] marker. Do NOT use this for user-uploaded files — use attachment_id instead."},
                        "attachment_job_ids": {"type": "array", "items": {"type": "string"}, "description": "Use ONLY for MULTIPLE documents YOU built with build_document. Do NOT use for user-uploaded files."},
                        "attachment_artifact_id": {"type": "string", "description": "Attach a document built EARLIER (not in this turn) by its artifact_id — e.g. 'email the deck I made'."},
                    },
                    "required": ["to", "subject", "body"],
                },
            },
            {
                "name": "teams_send_message",
                "description": "Post a message to a Microsoft Teams channel. WRITE action — requires explicit user confirmation before sending.",
                "method": "POST",
                "path": "/v1.0/teams/{team_id}/channels/{channel_id}/messages",
                "requires_scopes": ["ChannelMessage.Send"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "team_id": {"type": "string", "description": "Teams team ID"},
                        "channel_id": {"type": "string", "description": "Channel ID"},
                        "message": {"type": "string", "description": "Message text to post"},
                        "attachment_id": {"type": "string", "description": "PREFERRED for user-uploaded files: when the user's message contains [attachment_id=<uuid>], pass that UUID here. Uploaded to OneDrive and linked in the Teams message. Do NOT use attachment_file_path for these."},
                        "attachment_ids": {"type": "array", "items": {"type": "string"}, "description": "PREFERRED for multiple user-uploaded files: pass all [attachment_id=<uuid>] values as an array. Uploaded to OneDrive and linked in the Teams message."},
                        "attachment_file_path": {"type": "string", "description": "Attach ONE file from the working folder by bare filename only (e.g. 'Report.xlsx'). Do NOT pass a full Windows path. Use only when no attachment_id is available."},
                        "attachment_file_paths": {"type": "array", "items": {"type": "string"}, "description": "Attach MULTIPLE files from the working folder by bare filename only. Use only when no attachment_ids are available."},
                        "attachment_job_id": {"type": "string", "description": "Use ONLY for documents YOU built with build_document (DOCJOB job id). Do NOT use for user-uploaded files."},
                        "attachment_job_ids": {"type": "array", "items": {"type": "string"}, "description": "Use ONLY for MULTIPLE documents YOU built with build_document. Do NOT use for user-uploaded files."},
                    },
                    "required": ["team_id", "channel_id", "message"],
                },
            },
            # ── Added 2026-06-23: full Outlook + Calendar + Teams + People surface ──
            {
                "name": "outlook_read_email",
                "description": "Open and read the full body of a specific email by its id.",
                "method": "GET",
                "path": "/v1.0/me/messages/{message_id}",
                "requires_scopes": ["Mail.Read"],
                "cache_ttl_s": 300,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "Email id (from outlook_search_emails)"},
                    },
                    "required": ["message_id"],
                },
            },
            {
                "name": "outlook_reply_email",
                "description": "Reply to an email. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/me/messages/{message_id}/reply",
                "requires_scopes": ["Mail.Send"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "Email id to reply to"},
                        "comment": {"type": "string", "description": "Reply text"},
                    },
                    "required": ["message_id", "comment"],
                },
            },
            {
                "name": "outlook_reply_all_email",
                "description": "Reply to ALL recipients (To + Cc) of an email. Graph resolves the full recipient list from the original message automatically. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/me/messages/{message_id}/replyAll",
                "requires_scopes": ["Mail.Send"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "Email id to reply-all to"},
                        "comment": {"type": "string", "description": "Reply text"},
                    },
                    "required": ["message_id", "comment"],
                },
            },
            {
                "name": "outlook_forward_email",
                "description": "Forward an email to recipients. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/me/messages/{message_id}/forward",
                "requires_scopes": ["Mail.Send"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "Email id to forward"},
                        "to": {"type": "string", "description": "Recipient email(s), comma-separated"},
                        "comment": {"type": "string", "description": "Optional note to prepend"},
                    },
                    "required": ["message_id", "to"],
                },
            },
            {
                "name": "outlook_list_folders",
                "description": "List the user's Outlook mail folders (Inbox, Sent, Archive, custom folders) with their ids — use to get a destination folder id for moving mail.",
                "method": "GET",
                "path": "/v1.0/me/mailFolders",
                "requires_scopes": ["Mail.Read"],
                "cache_ttl_s": 600,
                "paginated": True,
                "max_items": 200,
                "is_write": False,
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "outlook_create_folder",
                "description": "Create a new Outlook mail folder. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/me/mailFolders",
                "requires_scopes": ["Mail.ReadWrite"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "New folder display name."}},
                    "required": ["name"],
                },
            },
            {
                "name": "outlook_move_email",
                "description": "Move an email to another folder (e.g. Archive, Deleted Items, or a custom folder id from outlook_list_folders). WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/me/messages/{message_id}/move",
                "requires_scopes": ["Mail.ReadWrite"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "Email id to move."},
                        "destination_folder_id": {"type": "string", "description": "Target folder id, or a well-known name like 'archive', 'deleteditems', 'inbox', 'junkemail'."},
                    },
                    "required": ["message_id", "destination_folder_id"],
                },
            },
            {
                "name": "outlook_delete_email",
                "description": "Delete an email (moves it to Deleted Items). WRITE action — requires explicit user confirmation.",
                "method": "DELETE",
                "path": "/v1.0/me/messages/{message_id}",
                "requires_scopes": ["Mail.ReadWrite"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {"message_id": {"type": "string", "description": "Email id to delete."}},
                    "required": ["message_id"],
                },
            },
            {
                "name": "outlook_mark_email",
                "description": "Update an email's state: mark read/unread, flag it, set importance, or set categories. WRITE action — requires explicit user confirmation.",
                "method": "PATCH",
                "path": "/v1.0/me/messages/{message_id}",
                "requires_scopes": ["Mail.ReadWrite"],
                "is_write": True,
                "input_schema": {
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
            },
            {
                "name": "outlook_create_draft",
                "description": "Create a DRAFT email (not sent) — optionally with attachments. Returns the draft id; send later with outlook_send_draft. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/me/messages",
                "requires_scopes": ["Mail.ReadWrite"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient(s), comma/semicolon separated."},
                        "cc": {"type": "string", "description": "CC recipient(s)."},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                        "html": {"type": "boolean", "description": "true = body is HTML."},
                        "attachment_id": {"type": "string", "description": "PREFERRED for user-uploaded files: when the user's message contains [attachment_id=<uuid>], pass that UUID here. Do NOT use attachment_file_path for these."},
                        "attachment_ids": {"type": "array", "items": {"type": "string"}, "description": "PREFERRED for multiple user-uploaded files: pass all [attachment_id=<uuid>] values as an array."},
                        "attachment_file_path": {"type": "string", "description": "Attach ONE file from the working folder by bare filename only. Do NOT pass a full Windows path."},
                        "attachment_file_paths": {"type": "array", "items": {"type": "string"}, "description": "Attach MULTIPLE files from the working folder by bare filename only."},
                        "attachment_job_id": {"type": "string", "description": "Use ONLY for documents YOU built with build_document (DOCJOB job id). Do NOT use for user-uploaded files."},
                        "attachment_job_ids": {"type": "array", "items": {"type": "string"}, "description": "Multiple build_document job ids to attach."},
                    },
                    "required": ["subject"],
                },
            },
            {
                "name": "outlook_send_draft",
                "description": "Send a previously created draft email by its id. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/me/messages/{message_id}/send",
                "requires_scopes": ["Mail.Send"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {"message_id": {"type": "string", "description": "Draft email id (from outlook_create_draft)."}},
                    "required": ["message_id"],
                },
            },
            {
                "name": "outlook_list_attachments",
                "description": "List the attachments on an email (names, sizes, ids).",
                "method": "GET",
                "path": "/v1.0/me/messages/{message_id}/attachments",
                "requires_scopes": ["Mail.Read"],
                "cache_ttl_s": 300,
                "paginated": True,
                "max_items": 100,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {"message_id": {"type": "string", "description": "Email id."}},
                    "required": ["message_id"],
                },
            },
            {
                "name": "calendar_list_events",
                "description": "List/search the user's calendar events / meetings (expands recurring meetings). For cancel/reschedule, pass the narrowest date window plus subject_contains/search_query or attendee/organizer email to find the exact event id. Defaults to the next 30 days when no dates are given.",
                "method": "GET",
                "path": "/v1.0/me/calendarView",
                "requires_scopes": ["Calendars.Read"],
                "cache_ttl_s": 120,
                "paginated": True,
                "max_items": 200,
                "is_write": False,
                "input_schema": {
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
            },
            {
                "name": "calendar_create_event",
                "description": "Create/schedule a calendar event or Teams meeting. Supports required attendees and optional_attendees. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/me/events",
                "requires_scopes": ["Calendars.ReadWrite"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string", "description": "Event title"},
                        "start": {"type": "string", "description": "ISO datetime, e.g. 2026-07-01T10:00:00"},
                        "end": {"type": "string", "description": "ISO datetime"},
                        "attendees": {"type": "string", "description": "Required attendee email(s), comma/semicolon separated"},
                        "optional_attendees": {"type": "string", "description": "Optional attendee email(s), comma/semicolon separated. Use this when the user says someone is optional."},
                        "body": {"type": "string", "description": "Event description"},
                        "is_online_meeting": {"type": "boolean", "description": "true = add a Teams meeting link"},
                        "timezone": {"type": "string", "description": "e.g. India Standard Time"},
                    },
                    "required": ["subject", "start", "end"],
                },
            },
            {
                "name": "calendar_update_event",
                "description": "Update / RESCHEDULE an existing calendar event by its id (changes time, subject, attendees, etc. on the SAME event — does NOT create a new invite). Use this to reschedule. WRITE action — requires explicit user confirmation.",
                "method": "PATCH",
                "path": "/v1.0/me/events/{event_id}",
                "requires_scopes": ["Calendars.ReadWrite"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "The id of the event to update (from calendar_list_events)."},
                        "subject": {"type": "string", "description": "New title (omit to keep current)."},
                        "start": {"type": "string", "description": "New start ISO datetime (omit to keep)."},
                        "end": {"type": "string", "description": "New end ISO datetime (omit to keep)."},
                        "attendees": {"type": "string", "description": "Replace required attendee list — email(s), comma/semicolon separated."},
                        "optional_attendees": {"type": "string", "description": "Replace optional attendee list — email(s), comma/semicolon separated. Use this when the user says someone is optional."},
                        "location": {"type": "string", "description": "New location."},
                        "body": {"type": "string", "description": "New description."},
                        "timezone": {"type": "string", "description": "e.g. India Standard Time"},
                    },
                    "required": ["event_id"],
                },
            },
            {
                "name": "calendar_cancel_event",
                "description": "Cancel a meeting you ORGANIZED and notify attendees (keeps a cancelled record). Use this when you are the organizer. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/me/events/{event_id}/cancel",
                "requires_scopes": ["Calendars.ReadWrite"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "The id of the event to cancel (from calendar_list_events)."},
                        "comment": {"type": "string", "description": "Optional cancellation message sent to attendees."},
                    },
                    "required": ["event_id"],
                },
            },
            {
                "name": "calendar_delete_event",
                "description": "DELETE an event from the calendar by id. If you are the organizer this cancels the meeting; if you are an attendee it removes it from your calendar. Prefer calendar_cancel_event when you organized it and want attendees notified. WRITE action — requires explicit user confirmation.",
                "method": "DELETE",
                "path": "/v1.0/me/events/{event_id}",
                "requires_scopes": ["Calendars.ReadWrite"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "The id of the event to delete (from calendar_list_events)."},
                    },
                    "required": ["event_id"],
                },
            },
            {
                "name": "calendar_accept_event",
                "description": "ACCEPT a meeting invitation you received. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/me/events/{event_id}/accept",
                "requires_scopes": ["Calendars.ReadWrite"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "The id of the invite to accept (from calendar_list_events)."},
                        "comment": {"type": "string", "description": "Optional note sent to the organizer."},
                        "send_response": {"type": "boolean", "description": "Notify the organizer of your response (default true)."},
                    },
                    "required": ["event_id"],
                },
            },
            {
                "name": "calendar_decline_event",
                "description": "DECLINE a meeting invitation you received. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/me/events/{event_id}/decline",
                "requires_scopes": ["Calendars.ReadWrite"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "The id of the invite to decline (from calendar_list_events)."},
                        "comment": {"type": "string", "description": "Optional note sent to the organizer."},
                        "send_response": {"type": "boolean", "description": "Notify the organizer of your response (default true)."},
                    },
                    "required": ["event_id"],
                },
            },
            {
                "name": "calendar_tentative_event",
                "description": "Respond TENTATIVELY (maybe) to a meeting invitation you received. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/me/events/{event_id}/tentativelyAccept",
                "requires_scopes": ["Calendars.ReadWrite"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "The id of the invite (from calendar_list_events)."},
                        "comment": {"type": "string", "description": "Optional note sent to the organizer."},
                        "send_response": {"type": "boolean", "description": "Notify the organizer of your response (default true)."},
                    },
                    "required": ["event_id"],
                },
            },
            {
                "name": "calendar_forward_event",
                "description": "Forward a meeting/event to additional people. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/me/events/{event_id}/forward",
                "requires_scopes": ["Calendars.ReadWrite"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "The id of the event to forward (from calendar_list_events)."},
                        "to": {"type": "string", "description": "Recipient email(s), comma/semicolon separated."},
                        "comment": {"type": "string", "description": "Optional note to include."},
                    },
                    "required": ["event_id", "to"],
                },
            },
            {
                "name": "calendar_find_meeting_times",
                "description": "Suggest available meeting time slots for a set of attendees (uses free/busy). Read-only — safe to call without confirmation.",
                "method": "POST",
                "path": "/v1.0/me/findMeetingTimes",
                "requires_scopes": ["Calendars.Read.Shared"],
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "attendees": {"type": "string", "description": "Attendee email(s), comma/semicolon separated."},
                        "meeting_duration": {"type": "string", "description": "ISO-8601 duration, e.g. PT30M for 30 minutes."},
                        "minimum_attendee_percentage": {"type": "number", "description": "Min % of attendees that must be free (e.g. 100)."},
                    },
                    "required": ["attendees"],
                },
            },
            {
                "name": "calendar_get_schedule",
                "description": "Get free/busy availability for one or more people over a time window. Read-only — safe to call without confirmation.",
                "method": "POST",
                "path": "/v1.0/me/calendar/getSchedule",
                "requires_scopes": ["Calendars.Read.Shared"],
                "is_write": False,
                "input_schema": {
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
            },
            {
                "name": "onedrive_upload",
                "description": "Internal: upload a file to the user's OneDrive and return its shareable webUrl. Used to host files for Teams message attachments.",
                "method": "PUT",
                "path": "/v1.0/me/drive/root",
                "requires_scopes": ["Files.ReadWrite"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "content_bytes": {"type": "string", "description": "base64-encoded file bytes"},
                        "content_type": {"type": "string"},
                    },
                    "required": ["filename", "content_bytes"],
                },
            },
            {
                "name": "teams_list_my_teams",
                "description": "List the Teams the user is a member of (to get team_id).",
                "method": "GET",
                "path": "/v1.0/me/joinedTeams",
                "requires_scopes": ["Team.ReadBasic.All"],
                "cache_ttl_s": 600,
                "paginated": False,
                "max_items": 50,
                "is_write": False,
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "teams_list_channels",
                "description": "List channels in a team (to get channel_id).",
                "method": "GET",
                "path": "/v1.0/teams/{team_id}/channels",
                "requires_scopes": ["Channel.ReadBasic.All"],
                "cache_ttl_s": 600,
                "paginated": False,
                "max_items": 50,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "team_id": {"type": "string", "description": "Teams team ID (from teams_list_my_teams)"},
                    },
                    "required": ["team_id"],
                },
            },
            {
                "name": "teams_get_transcript_content",
                "description": "Read the full text of a meeting transcript.",
                "method": "GET",
                "path": "/v1.0/me/onlineMeetings/{meeting_id}/transcripts/{transcript_id}/content",
                "requires_scopes": ["OnlineMeetingTranscript.Read.All"],
                "cache_ttl_s": 300,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "meeting_id": {"type": "string", "description": "Graph onlineMeeting id"},
                        "transcript_id": {"type": "string", "description": "Transcript id (from teams_list_transcripts)"},
                    },
                    "required": ["meeting_id", "transcript_id"],
                },
            },
            {
                "name": "people_search",
                "description": "Find any colleague's email/profile by exact email/UPN first, then name/keyword across the org directory. If multiple same-name people are returned, ask the user which exact person to use before any outbound Teams, Outlook, or Calendar action.",
                "method": "GET",
                "path": "/v1.0/me/people",
                "requires_scopes": ["People.Read"],
                "cache_ttl_s": 600,
                "paginated": True,
                "max_items": 25,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "search_query": {"type": "string", "description": "Name or keyword to search across the org directory"},
                        "limit": {"type": "integer", "description": "Max results"},
                    },
                },
            },
            {
                "name": "org_direct_reports",
                "description": "List a person's DIRECT reports (the people who report to them). Omit user_email for the signed-in user's own reports. To get INDIRECT reports, call this again for each returned person.",
                "method": "GET",
                "path": "/v1.0/me/directReports",
                "requires_scopes": ["User.Read.All"],
                "cache_ttl_s": 600,
                "paginated": True,
                "max_items": 100,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_email": {"type": "string", "description": "Optional: the manager's email/id whose reports you want. Omit for yourself."},
                    },
                },
            },
            {
                "name": "org_get_manager",
                "description": "Get a person's manager. Omit user_email for the signed-in user.",
                "method": "GET",
                "path": "/v1.0/me/manager",
                "requires_scopes": ["User.Read.All"],
                "cache_ttl_s": 600,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_email": {"type": "string", "description": "Optional: the person's email/id. Omit for yourself."},
                    },
                },
            },
            # ── Teams CHATS (1:1 + group DMs) — distinct from channels ──
            {
                "name": "teams_list_chats",
                "description": "List the user's Teams chats (1:1 and GROUP). Returns each chat's topic + member names so you can find a group chat by name. Pass name_contains to filter by group name or member; the adapter scans deep across chat pages so older group chats are findable.",
                "method": "GET",
                "path": "/v1.0/me/chats",
                "requires_scopes": ["Chat.Read"],
                "cache_ttl_s": 120,
                "paginated": True,
                "max_items": 1000,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name_contains": {"type": "string", "description": "Optional: filter chats whose topic/group name or a member name contains this text (case-insensitive)."},
                        "limit": {"type": "integer", "description": "Max chats to scan"},
                    },
                },
            },
            {
                "name": "teams_get_chat_messages",
                "description": "Get recent messages from a Teams 1:1 or group chat.",
                "method": "GET",
                "path": "/v1.0/me/chats/{chat_id}/messages",
                "requires_scopes": ["Chat.Read"],
                "cache_ttl_s": 60,
                "paginated": True,
                "max_items": 50,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "chat_id": {"type": "string", "description": "Chat id (from teams_list_chats)"},
                        "limit": {"type": "integer", "description": "Max messages"},
                    },
                    "required": ["chat_id"],
                },
            },
            {
                "name": "teams_send_chat_message",
                "description": "Send a message to a Teams 1:1 or group chat. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/me/chats/{chat_id}/messages",
                "requires_scopes": ["Chat.ReadWrite"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "chat_id": {"type": "string", "description": "Chat id (from teams_list_chats)"},
                        "message": {"type": "string", "description": "Message text to send"},
                        "attachment_id": {"type": "string", "description": "PREFERRED for user-uploaded files: when the user's message contains [attachment_id=<uuid>], pass that UUID here. Uploaded to OneDrive and linked in the Teams chat. Do NOT use attachment_file_path for these."},
                        "attachment_ids": {"type": "array", "items": {"type": "string"}, "description": "PREFERRED for multiple user-uploaded files: pass all [attachment_id=<uuid>] values as an array. Uploaded to OneDrive and linked in the Teams chat."},
                        "attachment_file_path": {"type": "string", "description": "Attach ONE file from the working folder by bare filename only (e.g. 'Report.xlsx'). Do NOT pass a full Windows path. Use only when no attachment_id is available."},
                        "attachment_file_paths": {"type": "array", "items": {"type": "string"}, "description": "Attach MULTIPLE files from the working folder by bare filename only. Use only when no attachment_ids are available."},
                        "attachment_job_id": {"type": "string", "description": "Use ONLY for documents YOU built with build_document (DOCJOB job id). Do NOT use for user-uploaded files."},
                        "attachment_job_ids": {"type": "array", "items": {"type": "string"}, "description": "Use ONLY for MULTIPLE documents YOU built with build_document. Do NOT use for user-uploaded files."},
                    },
                    "required": ["chat_id", "message"],
                },
            },
            {
                "name": "teams_start_chat",
                "description": "Create or retrieve a 1:1 Teams chat with a colleague by exact confirmed work email/UPN or selected email from people_search — not a bare name. Graph is idempotent and returns the existing chat if one already exists. If people_search returns multiple matches, ask the user which exact person to use before calling this. Returns a chat_id to use with teams_send_chat_message. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/chats",
                "requires_scopes": ["Chat.Create"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_email": {"type": "string", "description": "The colleague's work email or user id (from people_search)."},
                    },
                    "required": ["user_email"],
                },
            },
            {
                "name": "teams_reply_channel_message",
                "description": "Reply in-thread to a specific Teams channel message. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies",
                "requires_scopes": ["ChannelMessage.Send"],
                "is_write": True,
                "input_schema": {
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
            },
            {
                "name": "teams_list_channel_members",
                "description": "List members of a Teams channel.",
                "method": "GET",
                "path": "/v1.0/teams/{team_id}/channels/{channel_id}/members",
                "requires_scopes": ["ChannelMember.Read.All"],
                "cache_ttl_s": 600,
                "paginated": True,
                "max_items": 500,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {"team_id": {"type": "string"}, "channel_id": {"type": "string"}},
                    "required": ["team_id", "channel_id"],
                },
            },
            {
                "name": "teams_list_members",
                "description": "List members of a Team.",
                "method": "GET",
                "path": "/v1.0/teams/{team_id}/members",
                "requires_scopes": ["TeamMember.Read.All"],
                "cache_ttl_s": 600,
                "paginated": True,
                "max_items": 1000,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {"team_id": {"type": "string"}},
                    "required": ["team_id"],
                },
            },
            {
                "name": "teams_create_channel",
                "description": "Create a new channel in a Team. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/teams/{team_id}/channels",
                "requires_scopes": ["Channel.Create"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "team_id": {"type": "string"},
                        "name": {"type": "string", "description": "Channel display name."},
                        "description": {"type": "string"},
                        "membership_type": {"type": "string", "description": "standard | private | shared."},
                    },
                    "required": ["team_id", "name"],
                },
            },
            {
                "name": "teams_get_chat_members",
                "description": "List the members of a Teams chat (1:1 or group).",
                "method": "GET",
                "path": "/v1.0/me/chats/{chat_id}/members",
                "requires_scopes": ["Chat.Read"],
                "cache_ttl_s": 300,
                "paginated": True,
                "max_items": 500,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {"chat_id": {"type": "string"}},
                    "required": ["chat_id"],
                },
            },
            {
                "name": "teams_list_meetings",
                "description": "List the user's Teams online meetings.",
                "method": "GET",
                "path": "/v1.0/me/onlineMeetings",
                "requires_scopes": ["OnlineMeetings.Read"],
                "cache_ttl_s": 120,
                "paginated": True,
                "max_items": 200,
                "is_write": False,
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "teams_create_online_meeting",
                "description": "Create a Teams online meeting and get its join link. WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/v1.0/me/onlineMeetings",
                "requires_scopes": ["OnlineMeetings.ReadWrite"],
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string"},
                        "start": {"type": "string", "description": "ISO datetime."},
                        "end": {"type": "string", "description": "ISO datetime."},
                    },
                    "required": ["subject"],
                },
            },
            {
                "name": "teams_get_presence",
                "description": "Get the signed-in user's Teams presence (Available/Busy/Away/etc.).",
                "method": "GET",
                "path": "/v1.0/me/presence",
                "requires_scopes": ["Presence.Read"],
                "cache_ttl_s": 30,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "teams_get_user_presence",
                "description": "Get another user's Teams presence by their user id.",
                "method": "GET",
                "path": "/v1.0/users/{user_id}/presence",
                "requires_scopes": ["Presence.Read.All"],
                "cache_ttl_s": 30,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {"user_id": {"type": "string", "description": "The user's id (from people_search)."}},
                    "required": ["user_id"],
                },
            },
        ],
    },
    {
        "name": "gmail",
        "display_name": "Gmail",
        "description": "Connect to Google Gmail — search, count, and read emails.",
        "icon_url": "/icons/gmail.svg",
        "category": "productivity",
        "auth_type": "oauth2",
        "has_custom_adapter": True,
        "rate_limit_per_min": 60,
        "is_builtin": True,
        "base_url": "https://gmail.googleapis.com/gmail/v1",
        "auth_config": {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "client_id_env": "GOOGLE_CLIENT_ID",
            "client_secret_env": _ENV_GOOGLE_OAUTH_CREDENTIAL,
            "pkce": True,
            "scopes": [
                "openid", "email", "profile",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send",
                # Required for the mark-read/unread, label, and trash tools —
                # covers read+most-write but deliberately NOT permanent delete
                # or account settings. OAuth scopes aren't retroactive, so an
                # already-connected user needs to reconnect once for this to
                # take effect.
                "https://www.googleapis.com/auth/gmail.modify",
            ],
            "extra_params": {"access_type": "offline", "prompt": "consent"},
            "revoke_url": "https://oauth2.googleapis.com/revoke",
        },
        "tools": [
            {
                "name": "gmail_search_emails",
                "description": "Search Gmail emails. Filter by sender, subject, date, label. Use for 'emails from', 'inbox', 'gmail' queries.",
                "method": "GET",
                "path": "/users/me/messages",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                "cache_ttl_s": 300,
                "paginated": True,
                "max_items": 50,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "from_address": {"type": "string", "description": "Sender email or name"},
                        "subject_contains": {"type": "string"},
                        "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                        "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                        "label": {"type": "string", "description": "Gmail label (e.g., INBOX, SENT)"},
                        "search_query": {"type": "string", "description": "Raw Gmail search query"},
                        "limit": {"type": "integer"},
                    },
                },
            },
            {
                "name": "gmail_count_emails",
                "description": "Count Gmail emails matching criteria.",
                "method": "GET",
                "path": "/users/me/messages",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                "cache_ttl_s": 300,
                "paginated": True,
                "max_items": 50,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "from_address": {"type": "string"},
                        "date_from": {"type": "string"},
                        "date_to": {"type": "string"},
                    },
                },
            },
            {
                "name": "gmail_read_email",
                "description": "Read the full content of a Gmail message by ID.",
                "method": "GET",
                "path": "/users/me/messages/{message_id}",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                "cache_ttl_s": 3600,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "Gmail message ID"},
                    },
                    "required": ["message_id"],
                },
            },
            {
                "name": "gmail_send_email",
                "description": "Send an email from the user's Gmail. WRITE action — requires explicit user "
                               "confirmation (routes through the compliance-gated action endpoint).",
                "method": "POST",
                "path": "/users/me/messages/send",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.send"],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient email address(es), comma-separated."},
                        "subject": {"type": "string", "description": "Email subject."},
                        "body": {"type": "string", "description": "Email body (plain text)."},
                        "cc": {"type": "string", "description": "Optional CC address(es), comma-separated."},
                    },
                    "required": ["to", "subject", "body"],
                },
            },
            # gmail_list_labels / gmail_list_threads: the adapter's _extract_items()
            # already handles these tool names.
            {
                "name": "gmail_list_labels",
                "description": "List all Gmail labels (system labels like INBOX/SENT/UNREAD, and the "
                               "user's custom labels). Use to resolve a label name to its ID before "
                               "gmail_apply_label/gmail_remove_label, or to answer 'what labels do I have'.",
                "method": "GET",
                "path": "/users/me/labels",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                "cache_ttl_s": 3600,
                "paginated": False,
                "max_items": 200,
                "is_write": False,
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "gmail_list_threads",
                "description": "List Gmail conversation threads (grouped by subject/participants), not "
                               "individual messages. Use for 'show me my conversations with X' rather than "
                               "a flat message list; follow up with gmail_get_thread for the full contents.",
                "method": "GET",
                "path": "/users/me/threads",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                "cache_ttl_s": 300,
                "paginated": True,
                "max_items": 50,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "from_address": {"type": "string"},
                        "subject_contains": {"type": "string"},
                        "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                        "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                        "label": {"type": "string"},
                        "search_query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                },
            },
            {
                "name": "gmail_get_thread",
                "description": "Get every message in one Gmail conversation thread, in order — use after "
                               "gmail_list_threads/gmail_search_emails to read a full back-and-forth, not "
                               "just the single message that matched a search.",
                "method": "GET",
                "path": "/users/me/threads/{thread_id}",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                "cache_ttl_s": 300,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {"thread_id": {"type": "string", "description": "Gmail thread ID"}},
                    "required": ["thread_id"],
                },
            },
            {
                "name": "gmail_mark_read",
                "description": "Mark a Gmail message as read (removes the UNREAD label). WRITE action.",
                "method": "POST",
                "path": "/users/me/messages/{message_id}/modify",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.modify"],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {"message_id": {"type": "string", "description": "Gmail message ID"}},
                    "required": ["message_id"],
                },
            },
            {
                "name": "gmail_mark_unread",
                "description": "Mark a Gmail message as unread (adds the UNREAD label). WRITE action.",
                "method": "POST",
                "path": "/users/me/messages/{message_id}/modify",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.modify"],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {"message_id": {"type": "string", "description": "Gmail message ID"}},
                    "required": ["message_id"],
                },
            },
            {
                "name": "gmail_apply_label",
                "description": "Apply a label to a Gmail message (e.g. file it, star it, mark important). "
                               "label_id must be a real Gmail label ID — call gmail_list_labels first to "
                               "resolve a label name (e.g. 'Important') to its ID. WRITE action.",
                "method": "POST",
                "path": "/users/me/messages/{message_id}/modify",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.modify"],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "Gmail message ID"},
                        "label_id": {"type": "string", "description": "Label ID from gmail_list_labels (e.g. IMPORTANT, STARRED, or a custom label's id)"},
                    },
                    "required": ["message_id", "label_id"],
                },
            },
            {
                "name": "gmail_remove_label",
                "description": "Remove a label from a Gmail message. label_id must be a real Gmail label "
                               "ID — call gmail_list_labels first if unsure. WRITE action.",
                "method": "POST",
                "path": "/users/me/messages/{message_id}/modify",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.modify"],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "Gmail message ID"},
                        "label_id": {"type": "string", "description": "Label ID from gmail_list_labels"},
                    },
                    "required": ["message_id", "label_id"],
                },
            },
            {
                "name": "gmail_trash_email",
                "description": "Move a Gmail message to Trash (reversible — Gmail keeps trashed mail for "
                               "30 days before permanent deletion). WRITE action — requires explicit user "
                               "confirmation.",
                "method": "POST",
                "path": "/users/me/messages/{message_id}/trash",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.modify"],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {"message_id": {"type": "string", "description": "Gmail message ID"}},
                    "required": ["message_id"],
                },
            },
            {
                "name": "gmail_create_draft",
                "description": "Save an email as a Gmail draft without sending it, for the user to review "
                               "and send themselves later. Prefer this over gmail_send_email when the user "
                               "wants to review before sending. WRITE action.",
                "method": "POST",
                "path": "/users/me/drafts",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.modify"],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient email address(es), comma-separated."},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                        "cc": {"type": "string"},
                    },
                    "required": ["to", "subject", "body"],
                },
            },
            {
                "name": "gmail_list_drafts",
                "description": "List the user's saved Gmail drafts (not yet sent).",
                "method": "GET",
                "path": "/users/me/drafts",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.modify"],
                "cache_ttl_s": 60,
                "paginated": True,
                "max_items": 50,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer"}},
                },
            },
            {
                "name": "gmail_send_draft",
                "description": "Send a previously-saved Gmail draft by its draft ID (from gmail_create_draft "
                               "or gmail_list_drafts). WRITE action — requires explicit user confirmation.",
                "method": "POST",
                "path": "/users/me/drafts/send",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.modify"],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {"draft_id": {"type": "string", "description": "Draft ID"}},
                    "required": ["draft_id"],
                },
            },
            {
                "name": "gmail_reply_to_email",
                "description": "Reply within an existing Gmail conversation thread (sets In-Reply-To/"
                               "References/threadId so it lands as a reply, not a new email). Use instead "
                               "of gmail_send_email whenever the user is responding to a specific message "
                               "found via gmail_search_emails/gmail_read_email. WRITE action — requires "
                               "explicit user confirmation.",
                "method": "POST",
                "path": "/users/me/messages/send",
                "requires_scopes": ["https://www.googleapis.com/auth/gmail.send"],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "ID of the message being replied to"},
                        "body": {"type": "string", "description": "Reply body (plain text)"},
                        "to": {"type": "string", "description": "Recipient — usually the original sender; ask the user if ambiguous"},
                        "subject": {"type": "string", "description": "Optional — defaults to 'Re: <original subject>'"},
                        "cc": {"type": "string"},
                    },
                    "required": ["message_id", "body", "to"],
                },
            },
        ],
    },
    {
        "name": "slack",
        "display_name": "Slack",
        "description": "Connect to Slack — search messages, list channels, read conversations.",
        "icon_url": "/icons/slack.svg",
        "category": "communication",
        "auth_type": "oauth2",
        "has_custom_adapter": True,
        "rate_limit_per_min": 100,
        "is_builtin": True,
        "base_url": "https://slack.com/api",
        "auth_config": {
            "authorize_url": "https://slack.com/oauth/v2/authorize",
            "token_url": "https://slack.com/api/oauth.v2.access",
            "client_id_env": "SLACK_CLIENT_ID",
            "client_secret_env": _ENV_SLACK_OAUTH_CREDENTIAL,
            "pkce": False,
            "scopes": [
                "channels:read", "channels:history",
                "search:read", "users:read",
                "im:read", "im:history",
                "groups:read", "groups:history",
                "chat:write",
            ],
            "extra_params": {},
        },
        "tools": [
            {
                "name": "slack_search_messages",
                "description": "Search Slack messages across all channels. Use for 'Slack messages', 'what was said about', 'find in Slack' queries.",
                "method": "GET",
                "path": "/search.messages",
                "requires_scopes": ["search:read"],
                "cache_ttl_s": 60,
                "paginated": True,
                "max_items": 100,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "search_query": {"type": "string", "description": "Search query"},
                        "channel": {"type": "string", "description": "Filter by channel name"},
                        "from_user": {"type": "string", "description": "Filter by user"},
                        "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["search_query"],
                },
            },
            {
                "name": "slack_list_channels",
                "description": "List Slack channels the bot is a member of.",
                "method": "GET",
                "path": "/conversations.list",
                "requires_scopes": ["channels:read"],
                "cache_ttl_s": 600,
                "paginated": True,
                "max_items": 100,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer"},
                    },
                },
            },
            {
                "name": "slack_get_channel_messages",
                "description": "Get recent messages from a specific Slack channel.",
                "method": "GET",
                "path": "/conversations.history",
                "requires_scopes": ["channels:history"],
                "cache_ttl_s": 60,
                "paginated": True,
                "max_items": 100,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Channel ID or name"},
                        "limit": {"type": "integer"},
                        "oldest": {"type": "string", "description": "Oldest timestamp to fetch from"},
                    },
                    "required": ["channel"],
                },
            },
            {
                "name": "slack_post_message",
                "description": "Post a message to a Slack channel. WRITE action — requires explicit user "
                               "confirmation (routes through the compliance-gated action endpoint).",
                "method": "POST",
                "path": "/chat.postMessage",
                "requires_scopes": ["chat:write"],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Channel ID or name to post to."},
                        "text": {"type": "string", "description": "The message text."},
                    },
                    "required": ["channel", "text"],
                },
            },
        ],
    },
    {
        "name": "github",
        "display_name": "GitHub",
        "description": "Connect to GitHub — read issues, PRs, and repositories. (Extends existing GitLab-focused platform with GitHub support.)",
        "icon_url": "/icons/github.svg",
        "category": "devtools",
        "auth_type": "oauth2",
        "has_custom_adapter": False,
        "rate_limit_per_min": 60,
        "is_builtin": True,
        "base_url": "https://api.github.com",
        "auth_config": {
            "authorize_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "client_id_env": "GITHUB_OAUTH_CLIENT_ID",
            "client_secret_env": _ENV_GITHUB_OAUTH_CREDENTIAL,
            "pkce": False,
            "scopes": ["repo", "read:user", "read:org"],
            "extra_params": {},
        },
        "tools": [
            {
                "name": "github_list_issues",
                "description": "List GitHub issues for a repository.",
                "method": "GET",
                "path": "/repos/{owner}/{repo}/issues",
                "requires_scopes": ["repo"],
                "cache_ttl_s": 300,
                "paginated": True,
                "max_items": 50,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "state": {"type": "string", "enum": ["open", "closed", "all"]},
                        "limit": {"type": "integer"},
                    },
                    "required": ["owner", "repo"],
                },
            },
        ],
    },
    # ── Jira connector (PAT-based) ───────────────────────────────────────────
    # Uses the user's Atlassian API Token from Profile → API Token Vault, stored
    # as "email:api_token" and sent as Authorization: Basic base64(email:token).
    # Connect via POST /connectors/pat-connect/jira_connector — no OAuth popup.
    # base_url is resolved from JIRA_URL env at connect time and stored in metadata.
    #
    # has_custom_adapter=True → ConnectorEngine routes through JiraAdapter
    # (connectors/adapters/jira.py) which delegates to tools/jira_tools.py — the
    # same canonical HTTP client used by the SDLC pipeline. That client relays
    # through the web02 LLM proxy (Atlassian is unreachable from app02) and adds
    # a circuit breaker, retries, and request-id correlation.
    #
    # KEEP THE TOOL LIST BELOW IN SYNC with JiraAdapter._TOOL_MAP — a tool listed
    # here but unmapped raises "unknown tool", and a mapped tool missing from here
    # is unreachable. Also check the DB row actually matches:
    #   SELECT jsonb_array_length(tools), has_custom_adapter
    #   FROM ainxt.connector_definitions WHERE name='jira_connector';  -- expect 13 / TRUE
    #
    # ORDERING NOTE: jira_get_current_user is FIRST and takes no params, so the
    # connection test (connectors/probe.py) picks it as a parameterless probe.
    {
        "name": "jira_connector",
        "display_name": "Jira",
        "description": "Connect to Jira using your Atlassian API Token from Profile → API Token Vault. Search and read issues, browse projects, create and update issues, change status, assign work, and comment.",
        "icon_url": "/icons/jira.svg",
        "category": "devtools",
        "auth_type": "pat",
        "has_custom_adapter": True,
        "rate_limit_per_min": 60,
        "is_builtin": True,
        "base_url": "",  # resolved from JIRA_URL env at connect time; stored in metadata
        "auth_config": {},
        "tools": [
            # ── Identity (parameterless — used as the connection-test probe) ──
            {
                "name": "jira_get_current_user",
                "description": "Get the current user's own Jira profile — display name, email, and Atlassian accountId. Use this to answer 'who am I in Jira', and to obtain the accountId needed by jira_assign_issue when the user says 'assign it to me'.",
                "method": "GET",
                "path": "/myself",
                "requires_scopes": [],
                "cache_ttl_s": 600,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "jira_search_issues",
                "description": "Search Jira issues/tickets using a JQL query. Use for 'my open issues', 'my tickets', 'anything assigned to me', 'bugs in project X', 'tickets updated this week', 'what's in my sprint', 'my backlog'. For the user's OWN work use assignee = currentUser() — no project or username needed. Jira is a remote server: never answer these questions from a shell, from the local filesystem, or from your own knowledge.",
                # POST /search/jql — the old GET /search was REMOVED from Jira Cloud.
                # Cursor pagination via nextPageToken; returns no total count
                # (use jira_count_issues for that).
                "method": "POST",
                "path": "/search/jql",
                "requires_scopes": [],
                "cache_ttl_s": 300,
                "paginated": True,
                "max_items": 50,
                "is_write": False,
                "response_items_path": "issues",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "jql": {"type": "string", "description": (
                            "JQL query string. Common patterns:\n"
                            "  • the user's open work (no username needed): "
                            "'assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC'\n"
                            "  • tickets the user reported: 'reporter = currentUser() ORDER BY created DESC'\n"
                            "  • one project: 'project = PAY AND status = \"In Progress\" ORDER BY updated DESC'\n"
                            "  • recently touched: 'assignee = currentUser() AND updated >= -7d ORDER BY updated DESC'"
                        )},
                        "fields": {"type": "string", "description": "Comma-separated Jira fields to return (default: summary,status,assignee,reporter,priority,issuetype,created,updated)"},
                        "limit": {"type": "integer", "description": "Max issues to return (default 25, hard max 50)"},
                    },
                    "required": ["jql"],
                },
            },
            {
                "name": "jira_get_issue",
                "description": "Get full details of ONE Jira issue by its key — summary, description, status, assignee, priority, labels. Use this whenever the user mentions an issue key like PAY-123 or ABC-4521, even if they just paste the bare key or ask only 'what's the status of ABC-123'. An issue key is always a Jira lookup, never a shell command or a code search.",
                "method": "GET",
                "path": "/issue/{issue_key}",
                "requires_scopes": [],
                "cache_ttl_s": 300,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string", "description": "Jira issue key, e.g. 'PAY-123'"},
                        "fields": {"type": "string", "description": "Comma-separated fields to return"},
                    },
                    "required": ["issue_key"],
                },
            },
            {
                "name": "jira_create_issue",
                "description": "Create a new Jira issue in a project. WRITE action — requires user confirmation before executing.",
                "method": "POST",
                "path": "/issue",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_key": {"type": "string", "description": "Jira project key, e.g. 'PAY'"},
                        "summary": {"type": "string", "description": "Issue summary / title"},
                        "description": {"type": "string", "description": "Issue description (plain text)"},
                        "issue_type": {"type": "string", "description": "Issue type, e.g. 'Bug', 'Task', 'Story'"},
                        "priority": {"type": "string", "description": "Priority: Highest, High, Medium, Low, Lowest"},
                        "labels": {"type": "string", "description": "Comma-separated labels"},
                    },
                    "required": ["project_key", "summary"],
                },
            },
            {
                "name": "jira_add_comment",
                "description": "Add a comment to an existing Jira issue. WRITE action — requires user confirmation before executing.",
                "method": "POST",
                "path": "/issue/{issue_key}/comment",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string", "description": "Jira issue key, e.g. 'PAY-123'"},
                        "comment": {"type": "string", "description": "Comment text to add"},
                    },
                    "required": ["issue_key", "comment"],
                },
            },
            # ── Project / metadata lookups ────────────────────────────────────
            {
                "name": "jira_list_projects",
                "description": "List the Jira projects the user can see. Use for 'what Jira projects do I have', and ALWAYS use this FIRST to resolve a project key when another Jira tool needs project_key but the user only gave an informal name ('the payments project'). Never guess a project key.",
                "method": "GET",
                "path": "/project/search",
                "requires_scopes": [],
                "cache_ttl_s": 600,
                "paginated": False,
                "max_items": 150,
                "is_write": False,
                "response_items_path": "values",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "search": {"type": "string", "description": "Filter projects by name or key"},
                        "limit": {"type": "integer", "description": "Max projects to return (default 150)"},
                    },
                    "required": [],
                },
            },
            {
                "name": "jira_get_project",
                "description": "Get details of ONE Jira project by its key — name, description, and project lead. Use when the user asks about a project itself rather than its issues.",
                "method": "GET",
                "path": "/project/{project_key}",
                "requires_scopes": [],
                "cache_ttl_s": 600,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_key": {"type": "string", "description": "Jira project key, e.g. 'PAY'"},
                    },
                    "required": ["project_key"],
                },
            },
            {
                "name": "jira_count_issues",
                "description": "Count issues matching a JQL query WITHOUT fetching them. Use for 'how many open bugs are there', 'how many tickets are assigned to me' — far cheaper than jira_search_issues when the user only wants a number.",
                "method": "POST",
                "path": "/search/approximate-count",
                "requires_scopes": [],
                "cache_ttl_s": 300,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "jql": {"type": "string", "description": "JQL query to count, e.g. 'project = PAY AND statusCategory != Done'"},
                    },
                    "required": ["jql"],
                },
            },
            {
                "name": "jira_list_comments",
                "description": "List the comments on a Jira issue, with full text. Use for 'what did people say on PAY-123', 'summarise the discussion on this ticket'. jira_get_issue does NOT return comments — use this tool for them.",
                "method": "GET",
                "path": "/issue/{issue_key}/comment",
                "requires_scopes": [],
                "cache_ttl_s": 120,
                "paginated": False,
                "max_items": 150,
                "is_write": False,
                "response_items_path": "comments",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string", "description": "Jira issue key, e.g. 'PAY-123'"},
                        "limit": {"type": "integer", "description": "Max comments to return (default 150)"},
                    },
                    "required": ["issue_key"],
                },
            },
            {
                "name": "jira_get_transitions",
                "description": "List the status transitions currently allowed on a Jira issue. Jira only permits transitions its workflow allows from the issue's CURRENT status, so ALWAYS call this before jira_transition_issue rather than guessing a status name.",
                "method": "GET",
                "path": "/issue/{issue_key}/transitions",
                "requires_scopes": [],
                "cache_ttl_s": 60,
                "paginated": False,
                "max_items": 50,
                "is_write": False,
                "response_items_path": "transitions",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string", "description": "Jira issue key, e.g. 'PAY-123'"},
                    },
                    "required": ["issue_key"],
                },
            },
            # ── Write tools (all require user confirmation) ───────────────────
            {
                "name": "jira_update_issue",
                "description": "Update an existing Jira issue — change priority, reassign, transition status, and/or add a comment in one call. Use for 'change PAY-123 to high priority', 'reassign this ticket'. WRITE action — requires user confirmation before executing.",
                "method": "PUT",
                "path": "/issue/{issue_key}",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string", "description": "Jira issue key, e.g. 'PAY-123'"},
                        "status": {"type": "string", "description": "New status to transition to. Check jira_get_transitions first — only workflow-permitted values succeed."},
                        "priority": {"type": "string", "description": "Priority: Highest, High, Medium, Low, Lowest"},
                        "assignee_account_id": {"type": "string", "description": "Atlassian accountId of the new assignee. NOT a name or email — get it from jira_get_current_user."},
                        "comment": {"type": "string", "description": "Comment to add alongside the update"},
                    },
                    "required": ["issue_key"],
                },
            },
            {
                "name": "jira_transition_issue",
                "description": "Move a Jira issue to a new status — 'start work on PAY-123', 'mark this done', 'move to In Progress'. Call jira_get_transitions first to see which statuses the workflow currently allows. WRITE action — requires user confirmation before executing.",
                "method": "POST",
                "path": "/issue/{issue_key}/transitions",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string", "description": "Jira issue key, e.g. 'PAY-123'"},
                        "status": {"type": "string", "description": "Target status name, e.g. 'In Progress', 'Done'"},
                    },
                    "required": ["issue_key", "status"],
                },
            },
            {
                "name": "jira_assign_issue",
                "description": "Assign a Jira issue to a user. Requires the Atlassian accountId — a display name or email will NOT work. For 'assign it to me', call jira_get_current_user first to get the accountId. WRITE action — requires user confirmation before executing.",
                "method": "PUT",
                "path": "/issue/{issue_key}/assignee",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string", "description": "Jira issue key, e.g. 'PAY-123'"},
                        "account_id": {"type": "string", "description": "Atlassian accountId of the assignee (from jira_get_current_user)"},
                    },
                    "required": ["issue_key", "account_id"],
                },
            },
        ],
    },
    # ── GitLab connector (PAT-based) ─────────────────────────────────────────
    # Uses the user's GitLab Personal Access Token stored in Profile → API Token Vault.
    # Connect via POST /connectors/pat-connect/gitlab — no OAuth popup needed.
    # base_url is resolved from GITLAB_URL env at connect time and stored in metadata.
    #
    # has_custom_adapter=True → ConnectorEngine routes through GitLabAdapter
    # (connectors/adapters/gitlab.py) which delegates to tools/gitlab_tools.py —
    # the same canonical HTTP client used by the SDLC pipeline.  This eliminates
    # the previous duplication where Buddy had 5 read-only tools via GenericHTTPAdapter
    # while SDLC had 10+ tools via a separate code path.
    #
    # KEEP THE TOOL LIST BELOW IN SYNC with GitLabAdapter._TOOL_MAP — a tool listed
    # here but unmapped raises "unknown tool", and a mapped tool missing from here is
    # unreachable. Also check the DB row actually matches: a catch-up migration once
    # overwrote it with only 5 tools and has_custom_adapter=FALSE, which is why the UI
    # showed 5 tools and the connection test errored on 'project_id'.
    #   SELECT jsonb_array_length(tools), has_custom_adapter
    #   FROM ainxt.connector_definitions WHERE name='gitlab';   -- expect 17 / TRUE
    #
    # ORDERING NOTE: keep at least one tool with no required params early in the
    # array — see connectors/probe.py.
    {
        "name": "gitlab",
        "display_name": "GitLab",
        "description": "Connect to GitLab using your Personal Access Token from Profile → API Token Vault. Read and manage issues, merge requests, branches, files, and projects.",
        "icon_url": "/icons/gitlab.svg",
        "category": "devtools",
        "auth_type": "pat",
        "has_custom_adapter": True,   # routes through connectors/adapters/gitlab.py
        "rate_limit_per_min": 60,
        "is_builtin": True,
        "base_url": "",  # resolved from GITLAB_URL env at connect time; stored in metadata
        "auth_config": {},
        "tools": [
            # ── Cross-project "my work" tools (NO project needed) ─────────────
            # These MUST come first in the list: they answer the most common phrasing
            # ("my open MRs", "tickets assigned to me"), which names no project. Every
            # other tool here requires project_id, so without these the model finds no
            # valid call for a repo-less question and falls back to a shell/git guess.
            {
                "name": "gitlab_list_my_mrs",
                "description": "The user's OWN GitLab merge requests across ALL projects — no project needed. Use this FIRST for 'my merge requests', 'my open MRs', 'my PRs', 'what's waiting on my review', 'anything to review?', 'MRs I opened', 'ready to merge'. Use this whenever the user does NOT name a specific project. Never answer these questions with a shell command or git — GitLab is a remote server.",
                "method": "GET",
                "path": "/merge_requests",
                "query_params": {"scope": "assigned_to_me", "order_by": "updated_at", "sort": "desc"},
                "requires_scopes": [],
                "cache_ttl_s": 120,
                "paginated": True,
                "max_items": 50,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "scope": {"type": "string", "enum": ["assigned_to_me", "created_by_me", "all"],
                                  "description": "'assigned_to_me' (default) = needs the user's review/action; 'created_by_me' = MRs the user opened; 'all' = both"},
                        "state": {"type": "string", "enum": ["open", "closed", "merged", "all"],
                                  "description": "Filter by MR state (default: open)"},
                        "limit": {"type": "integer", "description": "Max MRs to return (default 20, max 50)"},
                    },
                    "required": [],
                },
            },
            {
                "name": "gitlab_list_my_issues",
                "description": "The user's OWN GitLab issues across ALL projects — no project needed. Use for 'my issues', 'GitLab issues assigned to me', 'what am I working on in GitLab', when the user does NOT name a project. (For Jira tickets use jira_search_issues instead.)",
                "method": "GET",
                "path": "/issues",
                "query_params": {"scope": "assigned_to_me", "order_by": "updated_at", "sort": "desc"},
                "requires_scopes": [],
                "cache_ttl_s": 120,
                "paginated": True,
                "max_items": 50,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "scope": {"type": "string", "enum": ["assigned_to_me", "created_by_me", "all"],
                                  "description": "'assigned_to_me' (default), 'created_by_me', or 'all'"},
                        "state": {"type": "string", "enum": ["open", "closed", "all"],
                                  "description": "Filter by issue state (default: open)"},
                        "limit": {"type": "integer", "description": "Max issues to return (default 20, max 50)"},
                    },
                    "required": [],
                },
            },
            # ── Read tools ────────────────────────────────────────────────────
            {
                "name": "gitlab_list_projects",
                "description": "List the GitLab repositories/projects the user has access to. Use for 'my repos', 'which projects do I have', 'how many repos do I have access to', or to search for a project by name. ALSO use this FIRST to resolve a project path when another GitLab tool needs project_id and the user only gave a partial or informal name — never guess a project path, and never fall back to a shell.",
                "method": "GET",
                "path": "/projects",
                "query_params": {"membership": "true", "order_by": "last_activity_at", "sort": "desc"},
                "requires_scopes": [],
                "cache_ttl_s": 300,
                "paginated": True,
                "max_items": 100,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "search":     {"type": "string",  "description": "Filter projects by name"},
                        "limit":      {"type": "integer", "description": "Max projects to return (default 50, max 100)"},
                        "membership": {"type": "boolean", "description": "Only return projects the user is a member of (default true)"},
                    },
                    "required": [],
                },
                "response_fields": ["id", "name", "path_with_namespace", "description", "visibility", "last_activity_at", "web_url"],
            },
            {
                "name": "gitlab_get_project",
                "description": "Get details of a GitLab project including description, visibility, default branch, and statistics.",
                "method": "GET",
                "path": "/projects/{project_id}",
                "requires_scopes": [],
                "cache_ttl_s": 600,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string", "description": "GitLab project path in 'namespace/project' format (e.g. 'myorg/myrepo')"},
                    },
                    "required": ["project_id"],
                },
            },
            {
                "name": "gitlab_list_issues",
                "description": "List issues in ONE named GitLab project. Use for 'open issues in <project>', 'bugs in <repo>'. If the user did NOT name a project, use gitlab_list_my_issues instead; if they named it informally, call gitlab_list_projects first to resolve the exact path.",
                "method": "GET",
                "path": "/projects/{project_id}/issues",
                "requires_scopes": [],
                "cache_ttl_s": 300,
                "paginated": True,
                "max_items": 50,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id":        {"type": "string", "description": "Project path in 'namespace/project' format"},
                        "state":             {"type": "string", "enum": ["open", "closed", "all"], "description": "Filter by issue state (default: open)"},
                        "labels":            {"type": "string", "description": "Comma-separated label names to filter by"},
                        "assignee_username": {"type": "string", "description": "Filter by assignee username"},
                        "limit":             {"type": "integer", "description": "Max issues to return (default 20, max 50)"},
                    },
                    "required": ["project_id"],
                },
            },
            {
                "name": "gitlab_list_mrs",
                "description": "List merge requests in ONE named GitLab project. Use for 'open MRs in <project>', 'merge requests for <repo>'. If the user did NOT name a project, use gitlab_list_my_mrs instead; if they named it informally, call gitlab_list_projects first to resolve the exact path. Never use git or a shell for this.",
                "method": "GET",
                "path": "/projects/{project_id}/merge_requests",
                "requires_scopes": [],
                "cache_ttl_s": 300,
                "paginated": True,
                "max_items": 50,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id":        {"type": "string", "description": "Project path in 'namespace/project' format"},
                        "state":             {"type": "string", "enum": ["open", "closed", "merged", "all"], "description": "Filter by MR state (default: open)"},
                        "assignee_username": {"type": "string", "description": "Filter by assignee username"},
                        "limit":             {"type": "integer", "description": "Max MRs to return (default 20, max 50)"},
                    },
                    "required": ["project_id"],
                },
            },
            {
                "name": "gitlab_list_commits",
                "description": "List recent commits on a GitLab project branch. Use for 'recent changes to <repo>', 'what landed this week', 'who changed this project lately'. This reads the REMOTE GitLab server — never run `git log` or a shell command to answer it.",
                "method": "GET",
                "path": "/projects/{project_id}/repository/commits",
                "requires_scopes": [],
                "cache_ttl_s": 300,
                "paginated": True,
                "max_items": 50,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string", "description": "Project path in 'namespace/project' format"},
                        "ref_name":   {"type": "string", "description": "Branch, tag, or commit SHA (default: default branch)"},
                        "limit":      {"type": "integer", "description": "Max commits to return (default 25, max 50)"},
                    },
                    "required": ["project_id"],
                },
            },
            {
                "name": "gitlab_list_branches",
                "description": "List all branches in a GitLab project. Use for 'what branches exist in <repo>', 'is there a branch called X', 'list branches'. This reads the REMOTE GitLab server — never run `git branch` or a shell command to answer it.",
                "method": "GET",
                "path": "/projects/{project_id}/repository/branches",
                "requires_scopes": [],
                "cache_ttl_s": 120,
                "paginated": True,
                "max_items": 100,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string", "description": "Project path in 'namespace/project' format"},
                        "search":     {"type": "string", "description": "Filter branches whose name contains this string"},
                        "limit":      {"type": "integer", "description": "Max branches to return (default 50, max 100)"},
                    },
                    "required": ["project_id"],
                },
            },
            {
                "name": "gitlab_read_file",
                "description": "Read a file's contents from a GitLab repository at a given branch — source code, config, Dockerfiles, CI/CD pipelines. Use this for ANY file that lives in a repo: those files are on the remote GitLab server, so the local Read tool and a shell CANNOT see them.",
                "method": "GET",
                "path": "/projects/{project_id}/repository/files/{path}",
                "requires_scopes": [],
                "cache_ttl_s": 120,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string", "description": "Project path in 'namespace/project' format"},
                        "path":       {"type": "string", "description": "File path within the repo (e.g. src/main/App.java)"},
                        "branch":     {"type": "string", "description": "Branch name (default: main)"},
                    },
                    "required": ["project_id", "path"],
                },
            },
            {
                "name": "gitlab_get_mr_files",
                "description": "Get the files changed in a merge request, for review context. Use for 'what changed in MR !123', 'review this MR', 'show me the diff'. Get the mr_iid from gitlab_list_my_mrs or gitlab_list_mrs first.",
                "method": "GET",
                "path": "/projects/{project_id}/merge_requests/{mr_iid}/changes",
                "requires_scopes": [],
                "cache_ttl_s": 120,
                "paginated": False,
                "max_items": 50,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string", "description": "Project path in 'namespace/project' format"},
                        "mr_iid":     {"type": "integer", "description": "MR internal ID"},
                        "max_files":  {"type": "integer", "description": "Max files to return (default 20)"},
                    },
                    "required": ["project_id", "mr_iid"],
                },
            },
            {
                # Present in GitLabAdapter._TOOL_MAP (and tools/gitlab_tools.py) since the
                # adapter landed, but never listed here — so the engine's _get_tool() could
                # not resolve it and the planner never saw it. Adding it closes that gap:
                # the adapter already maps project_id → repo and limit → max_results for
                # this tool in _normalise_params().
                "name": "gitlab_search_code",
                "description": "Search for a code pattern, symbol, or string across a GitLab repository's source files. Returns matching file paths and line snippets. Use for 'where is <function> defined', 'find usages of X in <repo>', 'which file contains Y'. The files live on the REMOTE GitLab server, so the local Read tool and a shell CANNOT see them.",
                "method": "GET",
                "path": "/projects/{project_id}/search",
                "requires_scopes": [],
                "cache_ttl_s": 120,
                "paginated": True,
                "max_items": 20,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string", "description": "Project path in 'namespace/project' format"},
                        "query":      {"type": "string", "description": "Code pattern, symbol name, or string to search for"},
                        "limit":      {"type": "integer", "description": "Max matches to return (default 10, max 20)"},
                    },
                    "required": ["project_id", "query"],
                },
            },
            # ── Write tools ───────────────────────────────────────────────────
            {
                "name": "gitlab_create_issue",
                "description": "Create a new issue in a GitLab repository.",
                "method": "POST",
                "path": "/projects/{project_id}/issues",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string", "description": "Project path in 'namespace/project' format"},
                        "title":      {"type": "string", "description": "Issue title"},
                        "body":       {"type": "string", "description": "Issue description (markdown)"},
                        "labels":     {"type": "array", "items": {"type": "string"}, "description": "Labels to apply"},
                    },
                    "required": ["project_id", "title"],
                },
            },
            {
                "name": "gitlab_create_mr",
                "description": "Create a merge request in GitLab. Always check for an existing open MR for the same branch before creating — handles 409 idempotently.",
                "method": "POST",
                "path": "/projects/{project_id}/merge_requests",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string", "description": "Project path in 'namespace/project' format"},
                        "title":      {"type": "string", "description": "MR title"},
                        "body":       {"type": "string", "description": "MR description"},
                        "head":       {"type": "string", "description": "Source branch name"},
                        "base":       {"type": "string", "description": "Target branch (merge into)", "default": "main"},
                        "draft":      {"type": "boolean", "description": "Create as draft MR"},
                    },
                    "required": ["project_id", "title", "body", "head"],
                },
            },
            {
                "name": "gitlab_create_branch",
                "description": "Create a new branch in a GitLab repository from a base branch.",
                "method": "POST",
                "path": "/projects/{project_id}/repository/branches",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id":  {"type": "string", "description": "Project path in 'namespace/project' format"},
                        "branch":      {"type": "string", "description": "New branch name"},
                        "from_branch": {"type": "string", "description": "Base branch (default: main)"},
                    },
                    "required": ["project_id", "branch"],
                },
            },
            {
                "name": "gitlab_comment_on_mr",
                "description": "Add a comment to a GitLab merge request (use for code review feedback).",
                "method": "POST",
                "path": "/projects/{project_id}/merge_requests/{mr_iid}/notes",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string", "description": "Project path in 'namespace/project' format"},
                        "mr_iid":     {"type": "integer", "description": "MR internal ID"},
                        "body":       {"type": "string", "description": "Comment body (markdown)"},
                    },
                    "required": ["project_id", "mr_iid", "body"],
                },
            },
            {
                "name": "gitlab_merge_mr",
                "description": "Merge an approved merge request. Only call after all checks pass.",
                "method": "PUT",
                "path": "/projects/{project_id}/merge_requests/{mr_iid}/merge",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id":   {"type": "string", "description": "Project path in 'namespace/project' format"},
                        "mr_iid":       {"type": "integer", "description": "MR internal ID"},
                        "merge_method": {"type": "string", "enum": ["squash", "merge", "rebase"], "description": "Merge strategy (default: squash)"},
                    },
                    "required": ["project_id", "mr_iid"],
                },
            },
            {
                "name": "gitlab_create_or_update_file",
                "description": "Create or update a file in a GitLab repository. Use for committing AI-generated code, config changes, or documentation.",
                "method": "POST",
                "path": "/projects/{project_id}/repository/files/{path}",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string", "description": "Project path in 'namespace/project' format"},
                        "path":       {"type": "string", "description": "File path within the repo"},
                        "content":    {"type": "string", "description": "File content"},
                        "message":    {"type": "string", "description": "Commit message"},
                        "branch":     {"type": "string", "description": "Branch to commit to (default: main)"},
                    },
                    "required": ["project_id", "path", "content", "message"],
                },
            },
        ],
    },
    {
        # OSS default SCM connector (SCM_PROVIDER=github). Mirrors the GitLab
        # connector's tool surface 1:1 via connectors/adapters/github.py →
        # tools/github_tools.py, so the two are interchangeable for chat/Cowork.
        # Seeded unconditionally (like both jira and confluence are) — a
        # deployment simply doesn't connect the one it doesn't use.
        "name": "github",
        "display_name": "GitHub",
        "description": "Connect to GitHub using your Personal Access Token from Profile → API Token Vault. Read and manage issues, pull requests, branches, files, and repositories.",
        "icon_url": "/icons/github.svg",
        "category": "devtools",
        "auth_type": "pat",
        "has_custom_adapter": True,   # routes through connectors/adapters/github.py
        "rate_limit_per_min": 60,
        "is_builtin": True,
        "base_url": "https://api.github.com",
        "auth_config": {},
        "tools": [
            {
                "name": "github_read_file",
                "description": "Read a file's contents from a GitHub repository at a given branch — source code, config, Dockerfiles, CI/CD pipelines. Use this for ANY file that lives in a repo: those files are on the remote GitHub server, so the local Read tool and a shell CANNOT see them.",
                "method": "GET",
                "path": "/repos/{repo_id}/contents/{path}",
                "requires_scopes": [],
                "cache_ttl_s": 120,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_id": {"type": "string", "description": "Repository in 'owner/repo' format (e.g. 'myorg/myrepo')"},
                        "path":    {"type": "string", "description": "File path within the repo (e.g. src/main/App.java)"},
                        "branch":  {"type": "string", "description": "Branch name (default: main)"},
                    },
                    "required": ["repo_id", "path"],
                },
            },
            {
                "name": "github_list_issues",
                "description": "List issues in ONE named GitHub repository. Use for 'open issues in <repo>', 'bugs in <repo>'.",
                "method": "GET",
                "path": "/repos/{repo_id}/issues",
                "requires_scopes": [],
                "cache_ttl_s": 300,
                "paginated": True,
                "max_items": 50,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_id": {"type": "string", "description": "Repository in 'owner/repo' format"},
                        "state":   {"type": "string", "enum": ["open", "closed", "all"], "description": "Filter by issue state (default: open)"},
                        "limit":   {"type": "integer", "description": "Max issues to return (default 20, max 50)"},
                    },
                    "required": ["repo_id"],
                },
            },
            {
                "name": "github_list_prs",
                "description": "List pull requests in ONE named GitHub repository. Use for 'open PRs in <repo>', 'pull requests for <repo>'. Never use git or a shell for this.",
                "method": "GET",
                "path": "/repos/{repo_id}/pulls",
                "requires_scopes": [],
                "cache_ttl_s": 300,
                "paginated": True,
                "max_items": 50,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_id": {"type": "string", "description": "Repository in 'owner/repo' format"},
                        "state":   {"type": "string", "enum": ["open", "closed", "all"], "description": "Filter by PR state (default: open)"},
                        "limit":   {"type": "integer", "description": "Max PRs to return (default 20, max 50)"},
                    },
                    "required": ["repo_id"],
                },
            },
            {
                "name": "github_get_pr",
                "description": "Get details of a specific pull request — title, state, branches, author, description.",
                "method": "GET",
                "path": "/repos/{repo_id}/pulls/{pr_number}",
                "requires_scopes": [],
                "cache_ttl_s": 120,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_id":   {"type": "string", "description": "Repository in 'owner/repo' format"},
                        "pr_number": {"type": "integer", "description": "PR number"},
                    },
                    "required": ["repo_id", "pr_number"],
                },
            },
            {
                "name": "github_get_pr_files",
                "description": "Get the files changed in a pull request, for review context. Use for 'what changed in PR #123', 'review this PR', 'show me the diff'. Get the pr_number from github_list_prs first.",
                "method": "GET",
                "path": "/repos/{repo_id}/pulls/{pr_number}/files",
                "requires_scopes": [],
                "cache_ttl_s": 120,
                "paginated": False,
                "max_items": 50,
                "is_write": False,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_id":   {"type": "string", "description": "Repository in 'owner/repo' format"},
                        "pr_number": {"type": "integer", "description": "PR number"},
                        "max_files": {"type": "integer", "description": "Max files to return (default 20)"},
                    },
                    "required": ["repo_id", "pr_number"],
                },
            },
            # ── Write tools ───────────────────────────────────────────────────
            {
                "name": "github_create_issue",
                "description": "Create a new issue in a GitHub repository.",
                "method": "POST",
                "path": "/repos/{repo_id}/issues",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_id": {"type": "string", "description": "Repository in 'owner/repo' format"},
                        "title":   {"type": "string", "description": "Issue title"},
                        "body":    {"type": "string", "description": "Issue description (markdown)"},
                        "labels":  {"type": "array", "items": {"type": "string"}, "description": "Labels to apply"},
                    },
                    "required": ["repo_id", "title"],
                },
            },
            {
                "name": "github_create_pr",
                "description": "Create a pull request in GitHub. Always check for an existing open PR for the same branch before creating — handles 422 idempotently.",
                "method": "POST",
                "path": "/repos/{repo_id}/pulls",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_id": {"type": "string", "description": "Repository in 'owner/repo' format"},
                        "title":   {"type": "string", "description": "PR title"},
                        "body":    {"type": "string", "description": "PR description"},
                        "head":    {"type": "string", "description": "Source branch name"},
                        "base":    {"type": "string", "description": "Target branch (merge into)", "default": "main"},
                    },
                    "required": ["repo_id", "title", "body", "head"],
                },
            },
            {
                "name": "github_create_branch",
                "description": "Create a new branch in a GitHub repository from a base branch.",
                "method": "POST",
                "path": "/repos/{repo_id}/git/refs",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_id":     {"type": "string", "description": "Repository in 'owner/repo' format"},
                        "branch":      {"type": "string", "description": "New branch name"},
                        "from_branch": {"type": "string", "description": "Base branch (default: main)"},
                    },
                    "required": ["repo_id", "branch"],
                },
            },
            {
                "name": "github_comment_on_pr",
                "description": "Add a comment to a GitHub pull request (use for code review feedback).",
                "method": "POST",
                "path": "/repos/{repo_id}/issues/{pr_number}/comments",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_id":   {"type": "string", "description": "Repository in 'owner/repo' format"},
                        "pr_number": {"type": "integer", "description": "PR number"},
                        "body":      {"type": "string", "description": "Comment body (markdown)"},
                    },
                    "required": ["repo_id", "pr_number", "body"],
                },
            },
            {
                "name": "github_merge_pr",
                "description": "Merge an approved pull request. Only call after all checks pass.",
                "method": "PUT",
                "path": "/repos/{repo_id}/pulls/{pr_number}/merge",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_id":      {"type": "string", "description": "Repository in 'owner/repo' format"},
                        "pr_number":    {"type": "integer", "description": "PR number"},
                        "merge_method": {"type": "string", "enum": ["squash", "merge", "rebase"], "description": "Merge strategy (default: squash)"},
                    },
                    "required": ["repo_id", "pr_number"],
                },
            },
            {
                "name": "github_create_or_update_file",
                "description": "Create or update a file in a GitHub repository. Use for committing AI-generated code, config changes, or documentation.",
                "method": "PUT",
                "path": "/repos/{repo_id}/contents/{path}",
                "requires_scopes": [],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "response_items_path": "",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_id": {"type": "string", "description": "Repository in 'owner/repo' format"},
                        "path":    {"type": "string", "description": "File path within the repo"},
                        "content": {"type": "string", "description": "File content"},
                        "message": {"type": "string", "description": "Commit message"},
                        "branch":  {"type": "string", "description": "Branch to commit to (default: main)"},
                    },
                    "required": ["repo_id", "path", "content", "message"],
                },
            },
        ],
    },
]

# Cowork connector pack (Google Drive, DocuSign, Zoom, Jira, Confluence) — scaffolded
# 2026-05-30 alongside their custom adapters in connectors/adapters/. Kept in a
# separate module so the pack is reversible. Most are egress-blocked (demo-only).
try:
    from connectors.seed_cowork_pack import COWORK_PACK_CONNECTORS
    SEED_CONNECTORS += COWORK_PACK_CONNECTORS

    from connectors.seed_cowork_pack import GOOGLE_CALENDAR_CONNECTORS
    SEED_CONNECTORS += GOOGLE_CALENDAR_CONNECTORS
except Exception as _e:  # pragma: no cover — never break seeding if the pack is absent
    from core.logger import logger as _logger
    _logger.warning(f"seed: cowork connector pack not loaded → {_e}")

# DPI (India Stack) connectors — Account Aggregator + DigiLocker. Self-contained
# under connectors/dpi/ for clean open-source extraction (ainxt-dpi-agent).
try:
    from connectors.dpi.seed_dpi import DPI_CONNECTORS
    SEED_CONNECTORS += DPI_CONNECTORS
except Exception as _e:  # pragma: no cover
    from core.logger import logger as _logger
    _logger.warning(f"seed: DPI connectors not loaded → {_e}")


def seed_connectors() -> None:
    """Insert built-in connector definitions into DB if they don't already exist."""
    try:
        from db.database import SessionLocal
        import sqlalchemy as sa
        db = SessionLocal()
        try:
            for conn_def in SEED_CONNECTORS:
                existing = db.execute(
                    sa.text("SELECT name FROM ainxt.connector_definitions WHERE name = :name"),
                    {"name": conn_def["name"]},
                ).fetchone()
                if existing:
                    # Refresh the built-in definition's mutable fields so new tools
                    # (e.g. outlook_send_mail/teams_send_message) and descriptions
                    # propagate on restart. Preserve is_active (admin may have
                    # disabled it) and never touch user tokens.
                    db.execute(
                        sa.text("""
                            UPDATE ainxt.connector_definitions
                            SET display_name       = :display_name,
                                description        = :description,
                                category           = :category,
                                auth_type          = :auth_type,
                                auth_config        = :auth_config,
                                tools              = :tools,
                                base_url           = :base_url,
                                has_custom_adapter = :has_custom_adapter,
                                rate_limit_per_min = :rate_limit_per_min
                            WHERE name = :name
                        """),
                        {
                            "name": conn_def["name"],
                            "display_name": conn_def["display_name"],
                            "description": conn_def.get("description", ""),
                            "category": conn_def.get("category", "custom"),
                            "auth_type": conn_def["auth_type"],
                            "auth_config": json.dumps(conn_def.get("auth_config", {})),
                            "tools": json.dumps(conn_def.get("tools", [])),
                            "base_url": conn_def.get("base_url", ""),
                            "has_custom_adapter": conn_def.get("has_custom_adapter", False),
                            "rate_limit_per_min": conn_def.get("rate_limit_per_min", 100),
                        },
                    )
                    logger.info(f"seed_connectors: refreshed {conn_def['name']} ({len(conn_def.get('tools', []))} tools)")
                    continue

                db.execute(
                    sa.text("""
                        INSERT INTO ainxt.connector_definitions
                        (name, display_name, description, icon_url, category, auth_type,
                         auth_config, tools, base_url, has_custom_adapter, rate_limit_per_min,
                         is_builtin, is_active)
                        VALUES
                        (:name, :display_name, :description, :icon_url, :category, :auth_type,
                         :auth_config, :tools, :base_url, :has_custom_adapter, :rate_limit_per_min,
                         :is_builtin, TRUE)
                    """),
                    {
                        "name": conn_def["name"],
                        "display_name": conn_def["display_name"],
                        "description": conn_def.get("description", ""),
                        "icon_url": conn_def.get("icon_url", ""),
                        "category": conn_def.get("category", "custom"),
                        "auth_type": conn_def["auth_type"],
                        "auth_config": json.dumps(conn_def.get("auth_config", {})),
                        "tools": json.dumps(conn_def.get("tools", [])),
                        "base_url": conn_def.get("base_url", ""),
                        "has_custom_adapter": conn_def.get("has_custom_adapter", False),
                        "rate_limit_per_min": conn_def.get("rate_limit_per_min", 100),
                        "is_builtin": conn_def.get("is_builtin", False),
                    },
                )
                logger.info(f"seed_connectors: inserted {conn_def['name']}")

            db.commit()
        finally:
            db.close()

        # Invalidate all in-memory caches so the next request picks up the
        # freshly-seeded definitions (including has_custom_adapter) without
        # requiring a full process restart.
        #
        # Three caches must be cleared:
        #   1. connector_registry._definitions  — tool lists / tool_count
        #   2. connector_engine._adapters       — adapter singleton per connector
        #      (critical: if GenericHTTPAdapter was cached for gitlab it stays
        #       until cleared, even after has_custom_adapter is fixed in the DB)
        #   3. connector_engine._defn_cache_*   — per-connector 5-min definition
        #      cache (holds has_custom_adapter=False until TTL expires)
        try:
            from connectors.registry import connector_registry
            connector_registry._bootstrapped = False
            connector_registry._definitions = []

            from connectors.engine import connector_engine
            connector_engine._adapters.clear()
            for _attr in list(vars(connector_engine).keys()):
                if _attr.startswith("_defn_cache_"):
                    delattr(connector_engine, _attr)

            logger.info(
                "seed_connectors: registry + engine caches invalidated "
                "— correct adapters will be loaded on next request"
            )
        except Exception as _re:
            logger.warning(f"seed_connectors: cache invalidation skipped — {_re}")

    except Exception as e:
        logger.warning(f"seed_connectors: failed — {e}")
