# SPDX-License-Identifier: Apache-2.0
"""
AiNxt Universal Connector Framework.

Provides authenticated, schema-driven, LLM-callable connectors to any enterprise system.
Adding a new connector requires only a DB insert (connector_definitions table) — no code change
needed for standard REST APIs. Complex APIs (Graph, Slack, Gmail) have custom adapters.

Usage:
    from connectors.registry import connector_registry
    result = connector_registry.execute("microsoft_365", "outlook_search_emails",
                                        {"from_address": "ceo@ainxt.com", "limit": 10},
                                        user_id="user-123")
"""
from connectors.base import (
    ConnectorTool,
    OAuth2Config,
    ConnectorResponse,
    ConnectorContext,
    ConnectorNotConnectedError,
    ConnectorReauthRequired,
    ConnectorScopeError,
    ConnectorRateLimitError,
)

__all__ = [
    "ConnectorTool",
    "OAuth2Config",
    "ConnectorResponse",
    "ConnectorContext",
    "ConnectorNotConnectedError",
    "ConnectorReauthRequired",
    "ConnectorScopeError",
    "ConnectorRateLimitError",
]
