# SPDX-License-Identifier: Apache-2.0
"""
Zoom custom adapter — Zoom API v2 (https://api.zoom.us/v2).

Handles Zoom-specific quirks:
- List endpoints wrap results in a named array (e.g. "meetings") with a
  `next_page_token` cursor and `page_size` query param.
- Get endpoints return a single bare object (no envelope).
- Create (write) endpoints accept a verbose JSON body and echo back the
  created object; we keep the connector's public schema simple (topic / start /
  duration) and shape it into Zoom's structure here.
- OAuth bearer token, same injection path as the Microsoft 365 adapter.
"""
from __future__ import annotations

from typing import Optional

import httpx

from connectors.adapters.base import AdapterBase, AdapterPage
from connectors.base import ConnectorContext, ConnectorTool
from core.logger import logger

ZOOM_BASE = "https://api.zoom.us/v2"

# Provider envelope key holding the item list, per list tool.
_LIST_KEYS = {
    "zoom_list_meetings": "meetings",
}


class ZoomAdapter(AdapterBase):
    """Custom adapter for Zoom API v2 (meetings)."""

    TIMEOUT = 25

    def execute(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str] = None,
    ) -> AdapterPage:
        method = tool.method.upper()
        resolved_path, remaining = self._resolve_path(tool.path, params)
        url = ZOOM_BASE + resolved_path

        headers = self.build_headers(context)

        try:
            if method == "GET":
                query_params = self._build_query_params(tool, remaining, cursor)
                resp = httpx.get(url, headers=headers, params=query_params, timeout=self.TIMEOUT)
                resp.raise_for_status()
                data = resp.json() if resp.content else {}
                return self._build_read_page(tool, data)

            # Write path (POST). Forward the engine-provided idempotency key so a
            # retried "create" is not duplicated provider-side.
            body = self._build_write_body(tool, remaining)
            idem = context.metadata.get("Idempotency-Key")
            if idem:
                headers["Idempotency-Key"] = idem
            resp = httpx.post(url, headers=headers, json=body, timeout=self.TIMEOUT)
            resp.raise_for_status()
            # Zoom create-meeting returns 201 with the created object; some write
            # endpoints return 204 with no body.
            if not resp.content:
                return AdapterPage(items=[{"status": "created", "tool": tool.name}], next_cursor=None, meta={})
            created = resp.json()
            return AdapterPage(
                items=[self._normalize_meeting(created)],
                next_cursor=None,
                meta={},
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                # Engine refreshes + retries once before treating this as a reconnect.
                from connectors.base import ConnectorTokenRejected
                raise ConnectorTokenRejected("Zoom access token was rejected (401).")
            # Re-raise others (429/5xx → engine retries; 400/403/404 → non-retryable).
            raise

    # ── request building ────────────────────────────────────────────────────

    def _build_query_params(self, tool: ConnectorTool, remaining: dict, cursor: Optional[str]) -> dict:
        """Build Zoom query params from tool definition and user params."""
        q: dict = {}

        # page_size (Zoom caps list page size at 300; respect the tool's max_items).
        limit = remaining.pop("limit", tool.max_items)
        try:
            limit_i = int(limit)
        except (TypeError, ValueError):
            limit_i = tool.max_items
        q["page_size"] = max(1, min(limit_i, tool.max_items, 300))

        # type filter for list_meetings (scheduled | live | upcoming).
        mtype = remaining.pop("type", None)
        if mtype:
            q["type"] = mtype

        # Resume pagination from the provider's opaque cursor token.
        if cursor:
            q["next_page_token"] = cursor

        return q

    def _build_write_body(self, tool: ConnectorTool, remaining: dict) -> dict:
        """
        Shape the simple tool params into the Zoom request body for WRITE tools.
        Keeps the public schema simple (topic/start_time/duration/agenda) while
        emitting the structure Zoom's create-meeting endpoint expects.
        """
        if tool.name == "zoom_create_meeting":
            body: dict = {
                "topic": remaining.get("topic", ""),
                # type 2 = scheduled meeting (the common create case).
                "type": 2,
            }
            if remaining.get("start_time"):
                body["start_time"] = remaining["start_time"]
            if remaining.get("duration") is not None:
                try:
                    body["duration"] = int(remaining["duration"])
                except (TypeError, ValueError):
                    pass
            if remaining.get("timezone"):
                body["timezone"] = remaining["timezone"]
            if remaining.get("agenda"):
                body["agenda"] = remaining["agenda"]
            return body
        return remaining

    # ── response normalization ──────────────────────────────────────────────

    def _build_read_page(self, tool: ConnectorTool, data: dict) -> AdapterPage:
        """Normalize a GET response into an AdapterPage."""
        list_key = _LIST_KEYS.get(tool.name)
        if list_key is not None:
            raw_items = data.get(list_key, []) if isinstance(data, dict) else []
            if not isinstance(raw_items, list):
                raw_items = [raw_items] if raw_items else []
            items = [self._normalize_meeting(m) for m in raw_items]
            # Zoom returns "" (empty string) for next_page_token when exhausted.
            next_token = data.get("next_page_token") or None
            return AdapterPage(
                items=items,
                next_cursor=next_token,
                meta={
                    "total_records": data.get("total_records"),
                    "page_count": data.get("page_count"),
                },
            )

        # Single-object GET (e.g. get_meeting): no envelope, no pagination.
        item = self._normalize_meeting(data) if isinstance(data, dict) else {}
        return AdapterPage(items=[item] if item else [], next_cursor=None, meta={})

    def _normalize_meeting(self, item: dict) -> dict:
        if not isinstance(item, dict):
            return {}
        return {
            "id": str(item.get("id", "")),
            "uuid": item.get("uuid", ""),
            "topic": item.get("topic", "(no topic)"),
            "type": item.get("type"),
            "status": item.get("status", ""),
            "start_time": item.get("start_time", ""),
            "duration": item.get("duration"),
            "timezone": item.get("timezone", ""),
            "agenda": item.get("agenda", ""),
            "host_id": item.get("host_id", ""),
            "host_email": item.get("host_email", ""),
            "join_url": item.get("join_url", ""),
            "created_at": item.get("created_at", ""),
        }


zoom_adapter = ZoomAdapter()
