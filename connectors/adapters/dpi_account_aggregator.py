# SPDX-License-Identifier: Apache-2.0
"""
Account Aggregator (DPI) adapter.

In SANDBOX mode (context.is_sandbox) returns synthetic fixtures — fully offline,
no real upstream/credentials. In production it would call the user's FIP via the
consent artifact (context.access_token); that path is the Phase-2 plug point and
requires RBI AA licensing, so it fails closed with a clear message.
"""
from __future__ import annotations

from typing import Optional

from connectors.adapters.base import AdapterBase, AdapterPage
from connectors.base import ConnectorContext, ConnectorTool
from connectors.dpi.sandbox import load_fixture
from core.logger import logger


class DpiAccountAggregatorAdapter(AdapterBase):

    def execute(self, tool: ConnectorTool, params: dict, context: ConnectorContext,
                cursor: Optional[str] = None) -> AdapterPage:
        if context.is_sandbox:
            if tool.name == "aa_list_accounts":
                items = load_fixture("aa_accounts").get("accounts", [])
                return AdapterPage(items=items, next_cursor=None, meta={"sandbox": True})
            if tool.name == "aa_fetch_statement":
                stmt = load_fixture("aa_statement")
                items = stmt.get("transactions", [])
                meta = {"sandbox": True, "account_id": params.get("account_id", ""),
                        "opening_balance": stmt.get("opening_balance"),
                        "closing_balance": stmt.get("closing_balance")}
                return AdapterPage(items=items, next_cursor=None, meta=meta)
            return AdapterPage(items=[], next_cursor=None, meta={"sandbox": True, "unknown_tool": tool.name})

        # PRODUCTION: call the FIP via the consent artifact. Requires RBI AA
        # licensing + a configured AA endpoint — fail closed until wired (Phase 2).
        logger.warning("dpi_account_aggregator: production AA access not configured (DPI_SANDBOX off)")
        raise RuntimeError(
            "Real Account Aggregator access requires RBI AA licensing and a configured FIP/AA endpoint. "
            "Set DPI_SANDBOX=true to use the open synthetic sandbox."
        )


dpi_account_aggregator_adapter = DpiAccountAggregatorAdapter()
