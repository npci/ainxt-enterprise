# SPDX-License-Identifier: MIT
"""
Confluence Cloud custom adapter — Atlassian Confluence Cloud REST API.

Handles Confluence-specific quirks:
- CQL (Confluence Query Language) search via /content/search
- `_links.next` relative-URL cursor pagination
- `body.storage` HTML payload for page bodies
- `space.key` + `title` + `body.storage` create-page request shape

Mirrors the Microsoft 365 adapter contract exactly:
- subclasses AdapterBase, implements only execute(...)
- injects the already-decrypted/refreshed token via build_headers(context)
- returns AdapterPage (engine wraps into ConnectorResponse)
- raises ConnectorReauthRequired on 401, re-raises everything else so the
  engine's retry/backoff handles 429/5xx and treats 400/403/404 as fatal

Token & secrets are never logged.
"""
from __future__ import annotations

from typing import Optional

import httpx

from connectors.adapters.base import AdapterBase, AdapterPage
from connectors.base import ConnectorContext, ConnectorTool
from core.logger import logger

# Confluence Cloud REST API base (per-site Wiki REST API v1).
# The site host is supplied per-connector via base_url / context.metadata["base_url"];
# this constant is the default fallback if metadata does not carry a base_url.
CONFLUENCE_BASE = "https://your-domain.atlassian.net/wiki/rest/api"


class ConfluenceAdapter(AdapterBase):
    """Custom adapter for Atlassian Confluence Cloud REST API."""

    TIMEOUT = 25  # Confluence search can be slow on large spaces

    def _base_url(self, context: ConnectorContext) -> str:
        """Resolve the site base URL from context metadata, fall back to constant."""
        base = (context.metadata or {}).get("base_url") or CONFLUENCE_BASE
        return base.rstrip("/")

    def execute(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str] = None,
    ) -> AdapterPage:
        method = tool.method.upper()
        base = self._base_url(context)
        headers = self.build_headers(context)

        # Cursor pagination: Confluence returns a relative _links.next path.
        if cursor and method == "GET":
            return self._fetch_cursor(base, cursor, tool, context)

        resolved_path, remaining = self._resolve_path(tool.path, params)
        url = base + resolved_path

        try:
            if method == "GET":
                query_params = self._build_query_params(tool, remaining)
                resp = httpx.get(url, headers=headers, params=query_params, timeout=self.TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                items = self._extract_items(data, tool)
                return AdapterPage(
                    items=items,
                    next_cursor=self._next_cursor(data),
                    meta={
                        "size": data.get("size"),
                        "limit": data.get("limit"),
                        "start": data.get("start"),
                    },
                )

            # WRITE path (POST) — e.g. confluence_create_page.
            body = self._build_write_body(tool, remaining)
            write_headers = dict(headers)
            idem = (context.metadata or {}).get("Idempotency-Key")
            if idem:
                write_headers["Idempotency-Key"] = idem
            resp = httpx.post(url, headers=write_headers, json=body, timeout=self.TIMEOUT)
            resp.raise_for_status()
            # Confluence returns the created page (201/200 with body); some
            # deployments return empty — emit a synthetic confirmation.
            if not resp.content:
                return AdapterPage(
                    items=[{"status": "created", "tool": tool.name}],
                    next_cursor=None,
                    meta={},
                )
            data = resp.json()
            return AdapterPage(
                items=[self._normalize_page(data, include_body=False, created=True)],
                next_cursor=None,
                meta={},
            )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                # Engine refreshes + retries once before treating this as a reconnect.
                from connectors.base import ConnectorTokenRejected
                raise ConnectorTokenRejected("Confluence access token was rejected (401).")
            # Re-raise 429/5xx (engine retries) and 400/403/404 (engine treats as fatal).
            # Do not log response bodies — they can echo the bearer token in some proxies.
            logger.warning(f"Confluence adapter HTTP {e.response.status_code} on {tool.name}")
            raise

    # ── pagination ──────────────────────────────────────────────────────────────

    def _fetch_cursor(
        self,
        base: str,
        cursor: str,
        tool: ConnectorTool,
        context: ConnectorContext,
    ) -> AdapterPage:
        """Follow a Confluence _links.next relative path (or absolute URL)."""
        headers = self.build_headers(context)
        url = cursor if cursor.startswith("https://") else base + cursor
        resp = httpx.get(url, headers=headers, timeout=self.TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return AdapterPage(
            items=self._extract_items(data, tool),
            next_cursor=self._next_cursor(data),
            meta={
                "size": data.get("size"),
                "limit": data.get("limit"),
                "start": data.get("start"),
            },
        )

    def _next_cursor(self, data: dict) -> Optional[str]:
        """Confluence pagination token: _links.next is a relative path or None."""
        links = data.get("_links", {}) if isinstance(data, dict) else {}
        return links.get("next") or None

    # ── request building ────────────────────────────────────────────────────────

    def _build_query_params(self, tool: ConnectorTool, remaining: dict) -> dict:
        """Build Confluence query params from tool definition and user params."""
        q: dict = {}

        limit = remaining.pop("limit", tool.max_items)
        try:
            q["limit"] = min(int(limit), tool.max_items)
        except (TypeError, ValueError):
            q["limit"] = tool.max_items

        if tool.name == "confluence_search_pages":
            cql = remaining.pop("cql", None)
            if not cql:
                # Build a sensible CQL from simple params if raw cql not supplied.
                clauses = ["type=page"]
                text = remaining.pop("query", None)
                if text:
                    safe = str(text).replace('"', '\\"')
                    clauses.append(f'text ~ "{safe}"')
                space = remaining.pop("space_key", None)
                if space:
                    clauses.append(f'space = "{space}"')
                cql = " AND ".join(clauses)
            q["cql"] = cql
            # Always expand a short body preview + space for normalization.
            q["expand"] = "space,version"

        elif tool.name == "confluence_get_page":
            # Body expansion for full page content.
            q["expand"] = "body.storage,space,version"

        # Forward any leftover simple params (engine already validated/coerced them).
        for k, v in remaining.items():
            if v is not None:
                q.setdefault(k, v)
        return q

    def _build_write_body(self, tool: ConnectorTool, remaining: dict) -> dict:
        """Shape simple tool params into the Confluence create-page request body."""
        if tool.name == "confluence_create_page":
            body: dict = {
                "type": "page",
                "title": remaining.get("title", ""),
                "space": {"key": remaining.get("space_key", "")},
                "body": {
                    "storage": {
                        "value": remaining.get("body", ""),
                        "representation": "storage",
                    }
                },
            }
            parent_id = remaining.get("parent_id")
            if parent_id:
                body["ancestors"] = [{"id": str(parent_id)}]
            return body
        return remaining

    # ── normalization ───────────────────────────────────────────────────────────

    def _extract_items(self, data: dict, tool: ConnectorTool) -> list[dict]:
        """Extract and normalize items from a Confluence response."""
        if tool.name == "confluence_get_page":
            # /content/{id} returns a single page object (no "results" envelope).
            if isinstance(data, dict) and data.get("id"):
                return [self._normalize_page(data, include_body=True)]
            return []

        raw_items = data.get("results", []) if isinstance(data, dict) else []
        if not isinstance(raw_items, list):
            raw_items = [raw_items] if raw_items else []
        return [self._normalize_page(item, include_body=False) for item in raw_items]

    def _normalize_page(
        self,
        item: dict,
        include_body: bool = False,
        created: bool = False,
    ) -> dict:
        """Flatten a Confluence content object into a flat dict."""
        if not isinstance(item, dict):
            return {"raw": str(item)}

        space = item.get("space", {}) or {}
        version = item.get("version", {}) or {}
        links = item.get("_links", {}) or {}

        out = {
            "id": item.get("id", ""),
            "type": item.get("type", "page"),
            "title": item.get("title", "(untitled)"),
            "status": item.get("status", ""),
            "space_key": space.get("key", ""),
            "space_name": space.get("name", ""),
            "version": version.get("number", ""),
            "updated_at": version.get("when", ""),
            "url": links.get("webui", "") or links.get("self", ""),
        }
        if created:
            out["status"] = item.get("status", "current") or "created"
        if include_body:
            storage = (item.get("body", {}) or {}).get("storage", {}) or {}
            out["body"] = storage.get("value", "")
            out["body_format"] = storage.get("representation", "storage")
        return out


# MANDATORY module-level singleton — engine looks up exactly this name:
# connector_name "confluence" → "confluence".replace("-", "_") + "_adapter"
confluence_adapter = ConfluenceAdapter()
