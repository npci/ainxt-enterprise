# SPDX-License-Identifier: MIT
"""
AdapterBase ABC and GenericHTTPAdapter.

GenericHTTPAdapter covers standard REST APIs defined entirely in the DB
(connector_definitions.tools JSONB).  Complex APIs with pagination quirks,
special filters, or batching (Graph API, Slack, Gmail) use custom adapters.
"""
from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Optional
from dataclasses import dataclass, field

import httpx

from connectors.base import ConnectorContext, ConnectorResponse, ConnectorTool
from core.logger import logger


@dataclass
class AdapterPage:
    """Single page of results from a paginated API call."""
    items: list[dict]
    next_cursor: Optional[str] = None   # None means no more pages
    meta: dict = field(default_factory=dict)


class AdapterBase(ABC):
    """Base class for all connector adapters."""

    TIMEOUT = 20  # seconds
    RETRY_CODES = {429, 500, 502, 503, 504}
    NO_RETRY_CODES = {400, 401, 403, 404}

    @abstractmethod
    def execute(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str] = None,
    ) -> AdapterPage:
        """Execute one tool call (one page of results)."""
        ...

    def build_headers(self, context: ConnectorContext) -> dict:
        """Build auth headers. Supports OAuth2 Bearer (default) and PAT variants.

        PAT connectors (GitLab, Jira) store auth metadata in context.metadata:
          - auth_type: "pat"
          - pat_header: header name (e.g. "PRIVATE-TOKEN" for GitLab, "Authorization" for Jira)
          - pat_scheme: "token" (raw value), "Basic" (base64-encode), or "Bearer"
        """
        meta = context.metadata or {}
        auth_type = meta.get("auth_type", "oauth2")

        if auth_type == "pat":
            pat_header = meta.get("pat_header", "Authorization")
            pat_scheme = meta.get("pat_scheme", "Bearer")

            if pat_scheme == "Basic":
                # Jira: Authorization: Basic base64(email:api_token)
                import base64
                encoded = base64.b64encode(context.access_token.encode()).decode()
                return {
                    "Authorization": f"Basic {encoded}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
            elif pat_header == "PRIVATE-TOKEN":
                # GitLab: PRIVATE-TOKEN: <raw_pat>
                return {
                    "PRIVATE-TOKEN": context.access_token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
            else:
                # Generic PAT: <pat_header>: <pat_scheme> <token>
                return {
                    pat_header: f"{pat_scheme} {context.access_token}".strip(),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }

        # Default: OAuth2 Bearer
        return {
            "Authorization": f"Bearer {context.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _resolve_path(self, path: str, params: dict) -> tuple[str, dict]:
        """
        Replace {param} placeholders in path with values from params.
        Returns (resolved_path, remaining_params).
        """
        resolved = path
        remaining = dict(params)
        for match in re.findall(r"\{(\w+)\}", path):
            if match in remaining:
                resolved = resolved.replace(f"{{{match}}}", str(remaining.pop(match)))
        return resolved, remaining


class GenericHTTPAdapter(AdapterBase):
    """
    Generic REST API adapter driven entirely by ConnectorTool definition.
    Works for any standard REST API without custom pagination logic.
    Supports GET (query params) and POST (JSON body).
    """

    def execute(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str] = None,
    ) -> AdapterPage:
        base_url = context.metadata.get("base_url", "")
        resolved_path, remaining_params = self._resolve_path(tool.path, params)
        url = base_url.rstrip("/") + resolved_path

        headers = self.build_headers(context)
        static_qp = dict(tool.query_params)

        # Inject cursor as a query param if provided
        if cursor:
            remaining_params["cursor"] = cursor

        try:
            if tool.method.upper() == "GET":
                all_qp = {**static_qp, **remaining_params}
                resp = httpx.get(url, headers=headers, params=all_qp, timeout=self.TIMEOUT)
            else:
                resp = httpx.post(
                    url,
                    headers=headers,
                    params=static_qp,
                    json=remaining_params,
                    timeout=self.TIMEOUT,
                )

            resp.raise_for_status()
            data = resp.json()

            # Extract items using response_items_path
            items_path = tool.response_items_path or "value"
            items = data
            for part in items_path.split("."):
                if isinstance(items, dict):
                    items = items.get(part, [])
            if not isinstance(items, list):
                items = [items] if items else []

            # Guard: some APIs (e.g. GitLab /projects) return a top-level JSON
            # array rather than a dict.  Calling .get() on a list raises
            # AttributeError — check type before extracting pagination cursors.
            next_cursor = (
                (data.get("@odata.nextLink") or data.get("next_cursor"))
                if isinstance(data, dict) else None
            )

            return AdapterPage(items=items, next_cursor=next_cursor, meta=data)

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in self.NO_RETRY_CODES:
                raise
            raise  # let engine handle retry

        except Exception as e:
            logger.error(f"GenericHTTPAdapter: {tool.name} failed — {e}")
            raise
