# SPDX-License-Identifier: Apache-2.0
"""
Google Drive custom adapter — Google Drive API v3.

Handles Drive-specific quirks:
- `q` search query syntax (files.list)
- `pageToken`/`nextPageToken` cursor-based pagination
- `fields` partial-response selection
- File text extraction via files.get?alt=media (export for Google-native docs)

Modeled on connectors/adapters/microsoft365.py — same class shape, same token
injection (inherited build_headers), same 401 → ConnectorReauthRequired handling,
same re-raise of other HTTP errors so the engine's retry/backoff can act.
"""
from __future__ import annotations

from typing import Optional

import httpx

from connectors.adapters.base import AdapterBase, AdapterPage
from connectors.base import ConnectorContext, ConnectorTool
from core.logger import logger

DRIVE_BASE = "https://www.googleapis.com/drive/v3"

# Fields we request per item on files.list to keep payloads small.
_FILE_LIST_FIELDS = (
    "nextPageToken,files(id,name,mimeType,modifiedTime,createdTime,size,"
    "owners(displayName,emailAddress),webViewLink,parents,trashed,iconLink)"
)
_FILE_META_FIELDS = (
    "id,name,mimeType,modifiedTime,createdTime,size,"
    "owners(displayName,emailAddress),webViewLink,parents,trashed,description,iconLink"
)

# Google-native docs are not directly downloadable; they must be exported.
# Map the source mimeType to a plain-text-ish export format.
_EXPORT_MIME = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

# Upper bound on extracted text we hand back, to avoid context explosion.
_MAX_TEXT_CHARS = 20_000


class GoogleDriveAdapter(AdapterBase):
    """Custom adapter for Google Drive API v3."""

    TIMEOUT = 25  # Drive media reads can be slow

    def execute(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str] = None,
    ) -> AdapterPage:
        name = tool.name

        try:
            if name == "drive_search_files":
                return self._search_files(tool, params, context, cursor)
            if name == "drive_get_file_metadata":
                return self._get_file_metadata(tool, params, context)
            if name == "drive_get_file_text":
                return self._get_file_text(tool, params, context)

            # Fallback: generic GET against the resolved path (keeps parity with
            # the reference adapter's "else: pass through" behaviour).
            return self._generic_get(tool, params, context, cursor)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                # Engine refreshes + retries once before treating this as a reconnect.
                from connectors.base import ConnectorTokenRejected
                raise ConnectorTokenRejected("Google Drive access token was rejected (401).")
            # Re-raise 400/403/404/429/5xx untouched — the engine decides
            # retryable vs non-retryable and applies backoff.
            raise

    # ── Tool: drive_search_files ────────────────────────────────────────────
    def _search_files(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str],
    ) -> AdapterPage:
        url = DRIVE_BASE + "/files"
        headers = self.build_headers(context)
        query_params = self._build_search_params(tool, dict(params), cursor)

        resp = httpx.get(url, headers=headers, params=query_params, timeout=self.TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        raw_files = data.get("files", [])
        if not isinstance(raw_files, list):
            raw_files = [raw_files] if raw_files else []
        items = [self._normalize_file(f) for f in raw_files]

        return AdapterPage(
            items=items,
            next_cursor=data.get("nextPageToken"),
            meta={"incomplete_search": data.get("incompleteSearch", False)},
        )

    def _build_search_params(self, tool: ConnectorTool, remaining: dict, cursor: Optional[str]) -> dict:
        q: dict = {
            "fields": _FILE_LIST_FIELDS,
            "spaces": "drive",
            "corpora": "user",
        }

        # Build the Drive `q` query from simple, structured params.
        clauses: list[str] = []

        raw_q = remaining.pop("query", None)
        if raw_q:
            # Free-text name/full-text match. Escape single quotes for Drive syntax.
            safe = str(raw_q).replace("'", "\\'")
            clauses.append(f"(name contains '{safe}' or fullText contains '{safe}')")

        name_contains = remaining.pop("name_contains", None)
        if name_contains:
            safe = str(name_contains).replace("'", "\\'")
            clauses.append(f"name contains '{safe}'")

        mime_type = remaining.pop("mime_type", None)
        if mime_type:
            safe = str(mime_type).replace("'", "\\'")
            clauses.append(f"mimeType = '{safe}'")

        modified_after = remaining.pop("modified_after", None)
        if modified_after:
            # Drive expects RFC 3339; accept a bare YYYY-MM-DD and widen it.
            val = str(modified_after)
            if len(val) == 10:
                val = f"{val}T00:00:00"
            clauses.append(f"modifiedTime > '{val}'")

        include_trashed = remaining.pop("include_trashed", False)
        if not include_trashed:
            clauses.append("trashed = false")

        if clauses:
            q["q"] = " and ".join(clauses)

        # pageSize (limit) — clamp to the tool's hard cap.
        limit = remaining.pop("limit", tool.max_items)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = tool.max_items
        q["pageSize"] = max(1, min(limit, tool.max_items, 100))

        q["orderBy"] = "modifiedTime desc"

        if cursor:
            q["pageToken"] = cursor

        return q

    # ── Tool: drive_get_file_metadata ───────────────────────────────────────
    def _get_file_metadata(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
    ) -> AdapterPage:
        resolved_path, remaining = self._resolve_path(tool.path, params)
        url = DRIVE_BASE + resolved_path
        headers = self.build_headers(context)

        resp = httpx.get(
            url,
            headers=headers,
            params={"fields": _FILE_META_FIELDS},
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        return AdapterPage(items=[self._normalize_file(data)], next_cursor=None, meta={})

    # ── Tool: drive_get_file_text ───────────────────────────────────────────
    def _get_file_text(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
    ) -> AdapterPage:
        file_id = str(params.get("file_id", "")).strip()
        headers = self.build_headers(context)

        # 1) Fetch metadata to learn the mimeType (export vs direct download).
        meta_url = f"{DRIVE_BASE}/files/{file_id}"
        meta_resp = httpx.get(
            meta_url,
            headers=headers,
            params={"fields": "id,name,mimeType,size"},
            timeout=self.TIMEOUT,
        )
        meta_resp.raise_for_status()
        meta = meta_resp.json()
        mime_type = meta.get("mimeType", "")

        # 2) Google-native docs → export; everything else → alt=media download.
        if mime_type in _EXPORT_MIME:
            content_url = f"{DRIVE_BASE}/files/{file_id}/export"
            content_params = {"mimeType": _EXPORT_MIME[mime_type]}
        else:
            content_url = f"{DRIVE_BASE}/files/{file_id}"
            content_params = {"alt": "media"}

        content_resp = httpx.get(
            content_url,
            headers=headers,
            params=content_params,
            timeout=self.TIMEOUT,
        )
        content_resp.raise_for_status()

        text = self._extract_text(content_resp, mime_type)

        item = {
            "id": meta.get("id", file_id),
            "name": meta.get("name", ""),
            "mime_type": mime_type,
            "text": text,
            "truncated": len(text) >= _MAX_TEXT_CHARS,
        }
        return AdapterPage(items=[item], next_cursor=None, meta={})

    def _extract_text(self, resp: httpx.Response, mime_type: str) -> str:
        """Best-effort plain-text extraction from a Drive content response."""
        content_type = resp.headers.get("content-type", "")
        # Treat text/* and Google-native exports as decodable text.
        if mime_type in _EXPORT_MIME or content_type.startswith("text/") or "json" in content_type:
            try:
                text = resp.text
            except Exception:
                text = resp.content.decode("utf-8", errors="replace")
        else:
            # Binary (PDF, images, office blobs): we cannot text-extract here.
            # Return a marker instead of dumping bytes into the LLM context.
            return f"[binary content: {mime_type or content_type or 'unknown'} — not text-extractable]"
        return text[:_MAX_TEXT_CHARS]

    # ── Generic fallback ─────────────────────────────────────────────────────
    def _generic_get(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str],
    ) -> AdapterPage:
        resolved_path, remaining = self._resolve_path(tool.path, params)
        url = DRIVE_BASE + resolved_path
        headers = self.build_headers(context)
        if cursor:
            remaining["pageToken"] = cursor
        resp = httpx.get(url, headers=headers, params=remaining, timeout=self.TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("files", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            items = [items] if items else []
        return AdapterPage(items=items, next_cursor=data.get("nextPageToken"), meta={})

    # ── Normalization ────────────────────────────────────────────────────────
    def _normalize_file(self, item: dict) -> dict:
        owners = item.get("owners", []) or []
        primary_owner = owners[0] if owners else {}
        return {
            "id": item.get("id", ""),
            "name": item.get("name", "(untitled)"),
            "mime_type": item.get("mimeType", ""),
            "modified_at": item.get("modifiedTime", ""),
            "created_at": item.get("createdTime", ""),
            "size": item.get("size", ""),
            "owner": primary_owner.get("displayName", ""),
            "owner_email": primary_owner.get("emailAddress", ""),
            "web_url": item.get("webViewLink", ""),
            "parents": item.get("parents", []),
            "trashed": item.get("trashed", False),
            "description": item.get("description", ""),
        }


google_drive_adapter = GoogleDriveAdapter()
