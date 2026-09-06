# SPDX-License-Identifier: MIT
"""
Gmail custom adapter — Google Gmail API v1.

Handles Gmail quirks:
- Thread model (messages vs. threads)
- Gmail q-syntax search (from:, subject:, after:, before:)
- pageToken cursor pagination
- Label filtering

The write tools that modify message state (mark read/unread, label,
trash) require the gmail.modify OAuth scope (see connectors/seed.py's
auth_config for this connector). OAuth scopes aren't retroactive, so a
user connected before that scope was present needs to reconnect once
before those specific tools will work.
"""
from __future__ import annotations

from typing import Optional
import base64
import email

import httpx

from connectors.adapters.base import AdapterBase, AdapterPage
from connectors.base import (
    ConnectorContext,
    ConnectorReauthRequired,
    ConnectorTokenRejected,
    ConnectorTool,
)
from core.logger import logger

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"


class GmailAdapter(AdapterBase):

    TIMEOUT = 20

    # Tool name -> bound write-handler name. Dispatch table instead of the
    # original blanket "any POST/is_write tool goes to _send_email" — that
    # stopped being correct once more write tools were added.
    _WRITE_HANDLERS = {
        "gmail_send_email":    "_send_email",
        "gmail_create_draft":  "_create_draft",
        "gmail_send_draft":    "_send_draft",
        "gmail_reply_to_email": "_reply_to_email",
        "gmail_mark_read":     "_mark_read",
        "gmail_mark_unread":   "_mark_unread",
        "gmail_apply_label":   "_apply_label",
        "gmail_remove_label":  "_remove_label",
        "gmail_trash_email":   "_trash_email",
    }

    def execute(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str] = None,
    ) -> AdapterPage:
        handler_name = self._WRITE_HANDLERS.get(tool.name)
        if handler_name:
            return getattr(self, handler_name)(tool, params, context)
        # Fallback for any write tool that forgets to register above, rather
        # than silently treating an unknown write as a read.
        if (getattr(tool, "method", "GET") or "GET").upper() == "POST" or getattr(tool, "is_write", False):
            return self._send_email(tool, params, context)

        resolved_path, remaining = self._resolve_path(tool.path, params)
        url = GMAIL_BASE + resolved_path

        headers = self.build_headers(context)

        # Build Gmail-specific query params
        qp = self._build_gmail_params(tool, remaining, cursor)

        try:
            resp = httpx.get(url, headers=headers, params=qp, timeout=self.TIMEOUT)

            if resp.status_code == 401:
                # Token aged out — let the engine refresh + retry once before this
                # ever becomes a "please reconnect" that deactivates the token.
                from connectors.base import ConnectorTokenRejected
                raise ConnectorTokenRejected("Gmail access token was rejected (401).")

            resp.raise_for_status()
            data = resp.json()

            items, next_cursor = self._extract_items(data, tool, headers)

            return AdapterPage(items=items, next_cursor=next_cursor, meta=data)

        except (ConnectorReauthRequired, ConnectorTokenRejected):
            raise
        except httpx.HTTPStatusError as e:
            logger.error(f"GmailAdapter: HTTP {e.response.status_code} for {tool.name}")
            raise

    # ============================================================
    # WRITE HANDLERS
    # ============================================================

    def _build_raw_message(self, params: dict, thread_id: str = "", in_reply_to: str = "",
                            references: str = "") -> str:
        """Build a base64url-encoded RFC-2822 message, shared by send/draft/reply."""
        from email.message import EmailMessage
        to = (params.get("to") or "").strip()
        if not to:
            raise ValueError("requires a 'to' recipient")
        msg = EmailMessage()
        msg["To"] = to
        if params.get("cc"):
            msg["Cc"] = params["cc"]
        msg["Subject"] = params.get("subject", "")
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references
        msg.set_content(params.get("body", ""))
        return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    def _send_email(self, tool: ConnectorTool, params: dict, context: ConnectorContext) -> AdapterPage:
        """Send an email via Gmail API (POST /users/me/messages/send). Builds a
        base64url-encoded RFC-2822 message. Called only after the confirm +
        compliance gate at POST /connectors/action."""
        raw = self._build_raw_message(params)
        headers = self.build_headers(context)
        headers["Content-Type"] = "application/json"
        try:
            resp = httpx.post(f"{GMAIL_BASE}/users/me/messages/send",
                              headers=headers, json={"raw": raw}, timeout=self.TIMEOUT)
            if resp.status_code == 401:
                from connectors.base import ConnectorTokenRejected
                raise ConnectorTokenRejected("Gmail access token was rejected (401).")
            resp.raise_for_status()
            data = resp.json()
            return AdapterPage(
                items=[{"id": data.get("id", ""), "thread_id": data.get("threadId", ""),
                        "status": "sent", "to": params.get("to", ""), "subject": params.get("subject", "")}],
                next_cursor=None, meta=data)
        except httpx.HTTPStatusError as e:
            logger.error(f"GmailAdapter: send failed HTTP {e.response.status_code}")
            raise

    def _reply_to_email(self, tool: ConnectorTool, params: dict, context: ConnectorContext) -> AdapterPage:
        """Reply within an existing thread (POST /users/me/messages/send with
        threadId + In-Reply-To/References so Gmail — and the recipient's client —
        treats it as a reply, not a new conversation)."""
        message_id = (params.get("message_id") or "").strip()
        if not message_id:
            raise ValueError("gmail_reply_to_email requires a 'message_id'")
        headers = self.build_headers(context)

        # Fetch the original message's threading headers — Message-ID becomes
        # In-Reply-To, and its own References (if any) get carried forward.
        meta_resp = httpx.get(
            f"{GMAIL_BASE}/users/me/messages/{message_id}",
            headers=headers,
            params={"format": "metadata", "metadataHeaders": ["Message-ID", "Subject", "References"]},
            timeout=self.TIMEOUT,
        )
        if meta_resp.status_code == 401:
            raise ConnectorTokenRejected("Gmail access token was rejected (401).")
        meta_resp.raise_for_status()
        original = meta_resp.json()
        hdrs = {h["name"]: h["value"] for h in original.get("payload", {}).get("headers", [])}
        thread_id = original.get("threadId", "")
        orig_msg_id = hdrs.get("Message-ID", "")
        references = (hdrs.get("References", "") + " " + orig_msg_id).strip()

        reply_params = dict(params)
        if not reply_params.get("subject") and hdrs.get("Subject"):
            subj = hdrs["Subject"]
            reply_params["subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"

        raw = self._build_raw_message(reply_params, in_reply_to=orig_msg_id, references=references)
        payload = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id

        send_headers = dict(headers)
        send_headers["Content-Type"] = "application/json"
        resp = httpx.post(f"{GMAIL_BASE}/users/me/messages/send",
                           headers=send_headers, json=payload, timeout=self.TIMEOUT)
        if resp.status_code == 401:
            raise ConnectorTokenRejected("Gmail access token was rejected (401).")
        resp.raise_for_status()
        data = resp.json()
        return AdapterPage(
            items=[{"id": data.get("id", ""), "thread_id": data.get("threadId", thread_id),
                    "status": "sent", "in_reply_to": message_id}],
            next_cursor=None, meta=data)

    def _create_draft(self, tool: ConnectorTool, params: dict, context: ConnectorContext) -> AdapterPage:
        """POST /users/me/drafts — saves without sending, for user review."""
        raw = self._build_raw_message(params)
        headers = self.build_headers(context)
        headers["Content-Type"] = "application/json"
        resp = httpx.post(f"{GMAIL_BASE}/users/me/drafts",
                           headers=headers, json={"message": {"raw": raw}}, timeout=self.TIMEOUT)
        if resp.status_code == 401:
            raise ConnectorTokenRejected("Gmail access token was rejected (401).")
        resp.raise_for_status()
        data = resp.json()
        msg = data.get("message", {})
        return AdapterPage(
            items=[{"draft_id": data.get("id", ""), "message_id": msg.get("id", ""),
                    "thread_id": msg.get("threadId", ""), "status": "draft_saved",
                    "to": params.get("to", ""), "subject": params.get("subject", "")}],
            next_cursor=None, meta=data)

    def _send_draft(self, tool: ConnectorTool, params: dict, context: ConnectorContext) -> AdapterPage:
        """POST /users/me/drafts/send — sends a previously-created draft by id."""
        draft_id = (params.get("draft_id") or "").strip()
        if not draft_id:
            raise ValueError("gmail_send_draft requires a 'draft_id'")
        headers = self.build_headers(context)
        headers["Content-Type"] = "application/json"
        resp = httpx.post(f"{GMAIL_BASE}/users/me/drafts/send",
                           headers=headers, json={"id": draft_id}, timeout=self.TIMEOUT)
        if resp.status_code == 401:
            raise ConnectorTokenRejected("Gmail access token was rejected (401).")
        resp.raise_for_status()
        data = resp.json()
        return AdapterPage(
            items=[{"id": data.get("id", ""), "thread_id": data.get("threadId", ""),
                    "status": "sent", "draft_id": draft_id}],
            next_cursor=None, meta=data)

    def _modify_labels(self, message_id: str, context: ConnectorContext,
                        add: Optional[list[str]] = None, remove: Optional[list[str]] = None) -> dict:
        """Shared POST /users/me/messages/{id}/modify — used by mark read/unread
        and apply/remove label, which are all the same Gmail endpoint underneath."""
        headers = self.build_headers(context)
        headers["Content-Type"] = "application/json"
        body: dict = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        resp = httpx.post(f"{GMAIL_BASE}/users/me/messages/{message_id}/modify",
                           headers=headers, json=body, timeout=self.TIMEOUT)
        if resp.status_code == 401:
            raise ConnectorTokenRejected("Gmail access token was rejected (401).")
        resp.raise_for_status()
        return resp.json()

    def _mark_read(self, tool: ConnectorTool, params: dict, context: ConnectorContext) -> AdapterPage:
        message_id = (params.get("message_id") or "").strip()
        if not message_id:
            raise ValueError("gmail_mark_read requires a 'message_id'")
        data = self._modify_labels(message_id, context, remove=["UNREAD"])
        return AdapterPage(items=[{"id": message_id, "status": "marked_read",
                                    "label_ids": data.get("labelIds", [])}], next_cursor=None, meta=data)

    def _mark_unread(self, tool: ConnectorTool, params: dict, context: ConnectorContext) -> AdapterPage:
        message_id = (params.get("message_id") or "").strip()
        if not message_id:
            raise ValueError("gmail_mark_unread requires a 'message_id'")
        data = self._modify_labels(message_id, context, add=["UNREAD"])
        return AdapterPage(items=[{"id": message_id, "status": "marked_unread",
                                    "label_ids": data.get("labelIds", [])}], next_cursor=None, meta=data)

    def _apply_label(self, tool: ConnectorTool, params: dict, context: ConnectorContext) -> AdapterPage:
        message_id = (params.get("message_id") or "").strip()
        label_id = (params.get("label_id") or "").strip()
        if not message_id or not label_id:
            raise ValueError("gmail_apply_label requires 'message_id' and 'label_id'")
        data = self._modify_labels(message_id, context, add=[label_id])
        return AdapterPage(items=[{"id": message_id, "status": "label_applied", "label_id": label_id,
                                    "label_ids": data.get("labelIds", [])}], next_cursor=None, meta=data)

    def _remove_label(self, tool: ConnectorTool, params: dict, context: ConnectorContext) -> AdapterPage:
        message_id = (params.get("message_id") or "").strip()
        label_id = (params.get("label_id") or "").strip()
        if not message_id or not label_id:
            raise ValueError("gmail_remove_label requires 'message_id' and 'label_id'")
        data = self._modify_labels(message_id, context, remove=[label_id])
        return AdapterPage(items=[{"id": message_id, "status": "label_removed", "label_id": label_id,
                                    "label_ids": data.get("labelIds", [])}], next_cursor=None, meta=data)

    def _trash_email(self, tool: ConnectorTool, params: dict, context: ConnectorContext) -> AdapterPage:
        """POST /users/me/messages/{id}/trash — reversible (30-day Gmail trash),
        deliberately NOT a permanent-delete tool."""
        message_id = (params.get("message_id") or "").strip()
        if not message_id:
            raise ValueError("gmail_trash_email requires a 'message_id'")
        headers = self.build_headers(context)
        resp = httpx.post(f"{GMAIL_BASE}/users/me/messages/{message_id}/trash",
                           headers=headers, timeout=self.TIMEOUT)
        if resp.status_code == 401:
            raise ConnectorTokenRejected("Gmail access token was rejected (401).")
        resp.raise_for_status()
        data = resp.json()
        return AdapterPage(items=[{"id": message_id, "status": "trashed"}], next_cursor=None, meta=data)

    # ============================================================
    # READ PATH HELPERS
    # ============================================================

    def _build_gmail_params(self, tool: ConnectorTool, remaining: dict, cursor: Optional[str]) -> dict:
        """Build Gmail API query parameters."""
        qp: dict = {}

        # Gmail search query syntax
        q_parts = []
        if "from_address" in remaining:
            q_parts.append(f"from:{remaining.pop('from_address')}")
        if "to_address" in remaining:
            q_parts.append(f"to:{remaining.pop('to_address')}")
        if "subject_contains" in remaining:
            q_parts.append(f"subject:{remaining.pop('subject_contains')}")
        if "date_from" in remaining:
            # Gmail uses after:YYYY/MM/DD
            val = remaining.pop("date_from").replace("-", "/")
            q_parts.append(f"after:{val}")
        if "date_to" in remaining:
            val = remaining.pop("date_to").replace("-", "/")
            q_parts.append(f"before:{val}")
        if "label" in remaining:
            q_parts.append(f"label:{remaining.pop('label')}")
        if "search_query" in remaining:
            q_parts.append(remaining.pop("search_query"))
        if "has_attachment" in remaining and remaining.pop("has_attachment"):
            q_parts.append("has:attachment")

        if q_parts:
            qp["q"] = " ".join(q_parts)

        limit = remaining.pop("limit", tool.max_items)
        qp["maxResults"] = min(int(limit), tool.max_items)

        if cursor:
            qp["pageToken"] = cursor

        # Format: minimal | metadata | full | raw
        if tool.name in ("gmail_search_emails", "gmail_count_emails", "gmail_list_threads"):
            qp["format"] = "metadata"
            qp["metadataHeaders"] = ["From", "Subject", "Date", "To"]
        elif tool.name == "gmail_get_thread":
            qp["format"] = "full"

        return qp

    def _extract_items(self, data: dict, tool: ConnectorTool, headers: dict) -> tuple[list[dict], Optional[str]]:
        """Extract items and next page token from Gmail response."""
        next_cursor = data.get("nextPageToken")

        if tool.name in ("gmail_search_emails", "gmail_count_emails"):
            messages = data.get("messages", [])
            # messages list only has {id, threadId} — fetch metadata for each
            items = []
            for msg_ref in messages[:tool.max_items]:
                try:
                    meta = self._fetch_message_metadata(msg_ref["id"], headers)
                    items.append(meta)
                except Exception as e:
                    logger.debug(f"GmailAdapter: failed to fetch message {msg_ref['id']}: {e}")
            return items, next_cursor

        elif tool.name == "gmail_read_email":
            # Single message response
            item = self._normalize_full_message(data)
            return [item], None

        elif tool.name == "gmail_list_labels":
            return data.get("labels", []), None

        elif tool.name == "gmail_list_threads":
            threads = data.get("threads", [])
            return [{"id": t["id"], "snippet": t.get("snippet", "")} for t in threads], next_cursor

        elif tool.name == "gmail_get_thread":
            # Full thread — every message in the conversation, oldest first
            # (Gmail's own ordering), each normalized the same way a single
            # gmail_read_email result is.
            msgs = [self._normalize_full_message(m) for m in data.get("messages", [])]
            return [{"thread_id": data.get("id", ""), "message_count": len(msgs), "messages": msgs}], None

        elif tool.name == "gmail_list_drafts":
            drafts = data.get("drafts", [])
            items = []
            for d in drafts[:tool.max_items]:
                msg_id = (d.get("message") or {}).get("id", "")
                entry = {"draft_id": d.get("id", ""), "message_id": msg_id}
                if msg_id:
                    try:
                        entry.update(self._fetch_message_metadata(msg_id, headers))
                    except Exception as e:
                        logger.debug(f"GmailAdapter: failed to fetch draft message {msg_id}: {e}")
                items.append(entry)
            return items, next_cursor

        return [], next_cursor

    def _fetch_message_metadata(self, msg_id: str, headers: dict) -> dict:
        """Fetch a single message's metadata (headers only, not full body)."""
        url = f"{GMAIL_BASE}/users/me/messages/{msg_id}"
        resp = httpx.get(
            url,
            headers=headers,
            params={"format": "metadata", "metadataHeaders": "From,Subject,Date,To"},
            timeout=10,
        )
        resp.raise_for_status()
        return self._normalize_message_metadata(resp.json())

    def _normalize_message_metadata(self, item: dict) -> dict:
        hdrs = {h["name"].lower(): h["value"] for h in item.get("payload", {}).get("headers", [])}
        return {
            "id": item.get("id", ""),
            "thread_id": item.get("threadId", ""),
            "subject": hdrs.get("subject", "(no subject)"),
            "from": hdrs.get("from", ""),
            "to": hdrs.get("to", ""),
            "date": hdrs.get("date", ""),
            "snippet": item.get("snippet", ""),
            "label_ids": item.get("labelIds", []),
        }

    def _normalize_full_message(self, item: dict) -> dict:
        base = self._normalize_message_metadata(item)
        # Extract body text
        payload = item.get("payload", {})
        body = self._extract_body(payload)
        base["body"] = body
        return base

    def _extract_body(self, payload: dict) -> str:
        """Recursively extract plain text body from Gmail message payload."""
        mime = payload.get("mimeType", "")
        if mime == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        elif mime.startswith("multipart/"):
            for part in payload.get("parts", []):
                text = self._extract_body(part)
                if text:
                    return text
        return ""


gmail_adapter = GmailAdapter()
