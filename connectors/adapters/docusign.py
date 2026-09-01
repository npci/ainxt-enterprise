# SPDX-License-Identifier: Apache-2.0
"""
DocuSign eSignature custom adapter — DocuSign eSignature REST API v2.1.

Handles DocuSign-specific quirks:
- Account-scoped paths: /v2.1/accounts/{account_id}/...
- listStatusChanges envelope listing requires a from_date (or from_to_status) filter
- nextUri cursor-based pagination (a path fragment, NOT a full URL)
- Envelope creation/send returns a small JSON body (envelopeId/status), not 202-empty

Modeled exactly on connectors/adapters/microsoft365.py — same class shape, same
token injection via build_headers(), same 401 → ConnectorReauthRequired handling,
same re-raise of other HTTP errors so the engine can retry 429/5xx.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from connectors.adapters.base import AdapterBase, AdapterPage
from connectors.base import ConnectorContext, ConnectorTool
from core.logger import logger

# Demo/sandbox host. The DB seed base_url (context.metadata["base_url"]) overrides
# this at runtime; the constant is only a fallback for the demo environment.
DOCUSIGN_BASE = "https://demo.docusign.net/restapi"


class DocuSignAdapter(AdapterBase):
    """Custom adapter for the DocuSign eSignature REST API v2.1."""

    TIMEOUT = 25  # DocuSign envelope ops can be slow

    def _base_url(self, context: ConnectorContext) -> str:
        return str(context.metadata.get("base_url") or DOCUSIGN_BASE).rstrip("/")

    def execute(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str] = None,
    ) -> AdapterPage:
        method = tool.method.upper()
        resolved_path, remaining = self._resolve_path(tool.path, params)
        base = self._base_url(context)
        url = base + resolved_path

        headers = self.build_headers(context)

        try:
            if method == "GET":
                query_params = self._build_query_params(tool, remaining, cursor)
                resp = httpx.get(url, headers=headers, params=query_params, timeout=self.TIMEOUT)
                resp.raise_for_status()
                data = resp.json()

                items = self._extract_items(data, tool)
                next_cursor = self._next_cursor(data)

                return AdapterPage(
                    items=items,
                    next_cursor=next_cursor,
                    meta={
                        "result_set_size": data.get("resultSetSize"),
                        "total_set_size": data.get("totalSetSize"),
                    },
                )

            # WRITE path (POST) — create/send an envelope.
            body = self._build_write_body(tool, remaining)
            # Forward the engine-generated idempotency key for true idempotency.
            idem = context.metadata.get("Idempotency-Key")
            if idem:
                headers["X-DocuSign-Idempotency-Key"] = idem

            resp = httpx.post(url, headers=headers, json=body, timeout=self.TIMEOUT)
            resp.raise_for_status()

            if not resp.content:
                return AdapterPage(
                    items=[{"status": "sent", "tool": tool.name}],
                    next_cursor=None,
                    meta={},
                )

            data = resp.json()
            return AdapterPage(
                items=[self._normalize_create_result(data)],
                next_cursor=None,
                meta={},
            )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                # Engine refreshes + retries once before treating this as a reconnect.
                from connectors.base import ConnectorTokenRejected
                raise ConnectorTokenRejected("DocuSign access token was rejected (401).")
            # Re-raise everything else so the engine's retry/backoff logic runs.
            # Keep secrets out of logs: never log headers or the request body.
            logger.warning(
                "DocuSign %s failed: HTTP %s", tool.name, e.response.status_code
            )
            raise

    # ── Query param construction ────────────────────────────────────────────────

    def _build_query_params(
        self, tool: ConnectorTool, remaining: dict, cursor: Optional[str]
    ) -> dict:
        """Build DocuSign query params from the tool definition and user params."""
        q: dict = {}

        if tool.name == "docusign_list_envelopes":
            # listStatusChanges REQUIRES a time/status filter. Default to a 30-day
            # window if the caller did not supply one.
            from_date = remaining.pop("from_date", None)
            if from_date:
                q["from_date"] = str(from_date)
            else:
                q["from_date"] = (
                    datetime.now(timezone.utc) - timedelta(days=30)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")

            status = remaining.pop("status", None)
            if status:
                q["status"] = str(status)

            limit = remaining.pop("limit", tool.max_items)
            q["count"] = min(int(limit), tool.max_items)

            # Cursor resume — DocuSign returns a startPosition in nextUri.
            if cursor:
                q["start_position"] = cursor

        return q

    def _next_cursor(self, data: dict) -> Optional[str]:
        """
        Derive the next-page cursor from a DocuSign list response.

        DocuSign returns `nextUri` (a path fragment with a start_position query) and
        an `endPosition`. We resume by passing the next start_position back in as the
        cursor. Returns None when there are no more pages.
        """
        next_uri = data.get("nextUri")
        if not next_uri:
            return None
        try:
            end = int(data.get("endPosition"))
            return str(end + 1)
        except (TypeError, ValueError):
            return None

    # ── Response normalization ──────────────────────────────────────────────────

    def _extract_items(self, data: dict, tool: ConnectorTool) -> list[dict]:
        """Extract and normalize envelope items from a DocuSign response."""
        if tool.name == "docusign_get_envelope":
            # Single-envelope status response — the envelope is the root object.
            return [self._normalize_envelope(data)]

        raw_items = data.get("envelopes", [])
        if not isinstance(raw_items, list):
            raw_items = [raw_items] if raw_items else []
        return [self._normalize_envelope(item) for item in raw_items]

    def _normalize_envelope(self, item: dict) -> dict:
        return {
            "envelope_id": item.get("envelopeId", ""),
            "status": item.get("status", ""),
            "subject": item.get("emailSubject", ""),
            "sender_name": (item.get("sender") or {}).get("userName", ""),
            "sender_email": (item.get("sender") or {}).get("email", ""),
            "created_at": item.get("createdDateTime", ""),
            "sent_at": item.get("sentDateTime", ""),
            "completed_at": item.get("completedDateTime", ""),
            "last_modified": item.get("lastModifiedDateTime", ""),
        }

    def _normalize_create_result(self, data: dict) -> dict:
        return {
            "envelope_id": data.get("envelopeId", ""),
            "status": data.get("status", ""),
            "status_changed_at": data.get("statusDateTime", ""),
            "uri": data.get("uri", ""),
        }

    # ── Write body construction ─────────────────────────────────────────────────

    def _build_write_body(self, tool: ConnectorTool, remaining: dict) -> dict:
        """
        Shape the simple tool params into the verbose DocuSign envelope-create body.
        Keeps the connector's public schema small (subject/document/recipient) while
        emitting the structure the eSignature API requires.
        """
        if tool.name == "docusign_create_envelope":
            signer_email = str(remaining.get("signer_email", "")).strip()
            signer_name = str(remaining.get("signer_name", "")).strip()
            document_base64 = remaining.get("document_base64", "")
            document_name = remaining.get("document_name", "document.pdf")
            subject = remaining.get("email_subject", "Please sign this document")
            # "sent" triggers immediate send for signature; "created" = draft.
            status = remaining.get("status", "sent")

            return {
                "emailSubject": subject,
                "status": status,
                "documents": [
                    {
                        "documentBase64": document_base64,
                        "name": document_name,
                        "fileExtension": "pdf",
                        "documentId": "1",
                    }
                ],
                "recipients": {
                    "signers": [
                        {
                            "email": signer_email,
                            "name": signer_name,
                            "recipientId": "1",
                            "routingOrder": "1",
                        }
                    ]
                },
            }
        return remaining


# MANDATORY module-level singleton — engine looks up exactly this name.
# connector_name "docusign" → "docusign".replace("-", "_") + "_adapter"
docusign_adapter = DocuSignAdapter()
