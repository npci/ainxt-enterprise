# SPDX-License-Identifier: MIT
"""
DigiLocker (DPI) adapter.

SANDBOX → synthetic verified documents (offline). Production → DigiLocker pull
API via the consent artifact; requires MeitY/DigiLocker partner credentials, so
it fails closed until wired (Phase 2).
"""
from __future__ import annotations

from typing import Optional

from connectors.adapters.base import AdapterBase, AdapterPage
from connectors.base import ConnectorContext, ConnectorTool
from connectors.dpi.sandbox import load_fixture
from core.logger import logger


class DpiDigilockerAdapter(AdapterBase):

    def execute(self, tool: ConnectorTool, params: dict, context: ConnectorContext,
                cursor: Optional[str] = None) -> AdapterPage:
        if context.is_sandbox:
            if tool.name == "digilocker_list_documents":
                items = load_fixture("digilocker_documents").get("documents", [])
                return AdapterPage(items=items, next_cursor=None, meta={"sandbox": True})
            if tool.name == "digilocker_fetch_document":
                doc = load_fixture("digilocker_document").get("document", {})
                return AdapterPage(items=[doc] if doc else [], next_cursor=None,
                                   meta={"sandbox": True, "doc_id": params.get("doc_id", "")})
            return AdapterPage(items=[], next_cursor=None, meta={"sandbox": True, "unknown_tool": tool.name})

        logger.warning("dpi_digilocker: production DigiLocker access not configured (DPI_SANDBOX off)")
        raise RuntimeError(
            "Real DigiLocker access requires partner credentials and a configured endpoint. "
            "Set DPI_SANDBOX=true to use the open synthetic sandbox."
        )


dpi_digilocker_adapter = DpiDigilockerAdapter()
