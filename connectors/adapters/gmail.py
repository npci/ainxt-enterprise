# SPDX-License-Identifier: Apache-2.0
"""
Gmail custom adapter — Google Gmail API v1.

Handles Gmail quirks:
- Thread model (messages vs. threads)
- Gmail q-syntax search (from:, subject:, after:, before:)
- pageToken cursor pagination
- Label filtering
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

    def execute(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str] = None,
    ) -> AdapterPage:
        # WRITE: send an email (routes here only after the confirm + compliance gate).
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

    def _send_email(self, tool: ConnectorTool, params: dict, context: ConnectorContext) -> AdapterPage:
        """Send an email via Gmail API (POST /users/me/messages/send). Builds a
        base64url-encoded RFC-2822 message. Called only after the confirm +
        compliance gate at POST /connectors/action."""
        from email.message import EmailMessage
        to = (params.get("to") or "").strip()
        if not to:
            raise ValueError("gmail_send_email requires a 'to' recipient")
        msg = EmailMessage()
        msg["To"] = to
        if params.get("cc"):
            msg["Cc"] = params["cc"]
        msg["Subject"] = params.get("subject", "")
        msg.set_content(params.get("body", ""))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

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
                        "status": "sent", "to": to, "subject": params.get("subject", "")}],
                next_cursor=None, meta=data)
        except httpx.HTTPStatusError as e:
            logger.error(f"GmailAdapter: send failed HTTP {e.response.status_code}")
            raise

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
        if tool.name in ("gmail_search_emails", "gmail_count_emails"):
            qp["format"] = "metadata"
            qp["metadataHeaders"] = ["From", "Subject", "Date", "To"]

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
