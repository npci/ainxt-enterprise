# SPDX-License-Identifier: Apache-2.0
"""
Slack custom adapter.

Handles Slack API quirks:
- cursor-based pagination (response_metadata.next_cursor)
- conversations.history vs search.messages endpoints
- message normalization (user IDs, timestamps)
"""
from __future__ import annotations

from typing import Optional

import httpx

from connectors.adapters.base import AdapterBase, AdapterPage
from connectors.base import ConnectorContext, ConnectorTool
from core.logger import logger

SLACK_BASE = "https://slack.com/api"


class SlackAdapter(AdapterBase):

    TIMEOUT = 15

    def execute(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str] = None,
    ) -> AdapterPage:
        method = tool.method.upper()
        resolved_path, remaining = self._resolve_path(tool.path, params)
        url = SLACK_BASE + resolved_path

        headers = {
            "Authorization": f"Bearer {context.access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        # Inject pagination cursor + limit only for reads — a write (chat.postMessage)
        # takes {channel, text} and must not carry list-style params.
        if not getattr(tool, "is_write", False):
            if cursor:
                remaining["cursor"] = cursor
            limit = remaining.pop("limit", tool.max_items)
            remaining["limit"] = min(int(limit), tool.max_items)


        try:
            if method == "GET":
                resp = httpx.get(url, headers=headers, params=remaining, timeout=self.TIMEOUT)
            else:
                resp = httpx.post(url, headers=headers, json=remaining, timeout=self.TIMEOUT)

            resp.raise_for_status()
            data = resp.json()

            if not data.get("ok"):
                err = data.get("error", "unknown_error")
                if err == "token_revoked":
                    from connectors.base import ConnectorReauthRequired
                    raise ConnectorReauthRequired("Slack token revoked. Please reconnect.")
                raise ValueError(f"Slack API error: {err}")

            items = self._extract_items(data, tool)
            next_cursor = (
                data.get("response_metadata", {}).get("next_cursor") or
                data.get("next_cursor")
            )
            # Empty string means no more pages
            if not next_cursor:
                next_cursor = None

            return AdapterPage(items=items, next_cursor=next_cursor, meta=data)

        except (ConnectorReauthRequired, ValueError):
            raise
        except httpx.HTTPStatusError as e:
            logger.error(f"SlackAdapter: HTTP {e.response.status_code} for {tool.name}")
            raise

    def _extract_items(self, data: dict, tool: ConnectorTool) -> list[dict]:
        """Extract and normalize items based on tool."""
        if tool.name == "slack_list_channels":
            channels = data.get("channels", [])
            return [self._normalize_channel(c) for c in channels]
        elif tool.name == "slack_post_message":
            return [{"id": data.get("ts", ""), "ts": data.get("ts", ""),
                     "channel": data.get("channel", ""), "status": "posted"}]
        elif tool.name in ("slack_get_channel_messages", "slack_search_messages"):
            messages = data.get("messages", {})
            if isinstance(messages, dict):
                # search.messages wraps in {matches: [...]}
                msgs = messages.get("matches", messages.get("messages", []))
            else:
                msgs = messages
            return [self._normalize_message(m) for m in msgs]
        else:
            # Generic extraction
            for key in ("items", "members", "users", "data"):
                if key in data:
                    return data[key]
            return []

    def _normalize_message(self, item: dict) -> dict:
        ts = item.get("ts", "")
        return {
            "id": ts,
            "text": item.get("text", ""),
            "user": item.get("user", item.get("username", "")),
            "channel": item.get("channel", {}).get("id", "") if isinstance(item.get("channel"), dict) else item.get("channel", ""),
            "channel_name": item.get("channel", {}).get("name", "") if isinstance(item.get("channel"), dict) else "",
            "timestamp": ts,
            "has_files": bool(item.get("files")),
        }

    def _normalize_channel(self, item: dict) -> dict:
        return {
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "is_private": item.get("is_private", False),
            "member_count": item.get("num_members", 0),
            "topic": item.get("topic", {}).get("value", ""),
            "purpose": item.get("purpose", {}).get("value", ""),
        }


slack_adapter = SlackAdapter()
