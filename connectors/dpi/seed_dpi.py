# SPDX-License-Identifier: Apache-2.0
"""
DPI connector definitions (same shape as connectors/seed.py entries).

Two reference connectors:
  • dpi_account_aggregator — consent-based financial data (the hero; proves the
    Account-Aggregator consent model).
  • dpi_digilocker        — verified government documents (proves generality).

auth_type="dpi_consent" → engine routes auth through the ConsentHandler, not
OAuth. has_custom_adapter=True → engine loads the dpi_* adapter (sandbox-aware).
Appended to SEED_CONNECTORS by connectors/seed.py so they auto-bootstrap and
auto-surface to the Cowork agent once a (synthetic) consent is granted.
"""

DPI_CONNECTORS = [
    {
        "name": "dpi_account_aggregator",
        "display_name": "Account Aggregator (DPI)",
        "description": "Consent-based access to the user's financial data (RBI Account Aggregator / DEPA). "
                       "Read accounts and statements ONLY within the user's granted consent.",
        "icon_url": "/icons/dpi_aa.svg",
        "category": "dpi",
        "auth_type": "dpi_consent",
        "has_custom_adapter": True,
        "base_url": "",                       # sandbox uses fixtures; prod endpoint is env-injected
        "rate_limit_per_min": 30,
        "is_builtin": True,
        "auth_config": {
            "consent_purpose": "Personal finance review",
            "default_scopes": ["aa:accounts:read", "aa:statement:read"],
            "data_range_days": 180,
        },
        "tools": [
            {
                "name": "aa_list_accounts",
                "description": "List the user's linked bank accounts available under the granted Account Aggregator consent.",
                "method": "GET",
                "path": "/accounts",
                "requires_scopes": ["aa:accounts:read"],
                "cache_ttl_s": 60,
                "paginated": False,
                "max_items": 25,
                "is_write": False,
                "response_items_path": "accounts",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "aa_fetch_statement",
                "description": "Fetch transactions for a consented account over the consent's data range. "
                               "Use for spend analysis, cash-flow, income/expense summaries.",
                "method": "GET",
                "path": "/accounts/{account_id}/statement",
                "requires_scopes": ["aa:statement:read"],
                "cache_ttl_s": 60,
                "paginated": False,
                "max_items": 500,
                "is_write": False,
                "response_items_path": "transactions",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "Account id from aa_list_accounts."},
                    },
                    "required": ["account_id"],
                },
            },
        ],
    },
    {
        "name": "dpi_digilocker",
        "display_name": "DigiLocker (DPI)",
        "description": "Access the user's verified government-issued documents (PAN, driving licence, Aadhaar, "
                       "certificates) from DigiLocker, under the user's granted consent.",
        "icon_url": "/icons/dpi_digilocker.svg",
        "category": "dpi",
        "auth_type": "dpi_consent",
        "has_custom_adapter": True,
        "base_url": "",
        "rate_limit_per_min": 30,
        "is_builtin": True,
        "auth_config": {
            "consent_purpose": "Document verification / form-filling",
            "default_scopes": ["digilocker:docs:read"],
            "data_range_days": 0,
        },
        "tools": [
            {
                "name": "digilocker_list_documents",
                "description": "List the user's verified documents available in DigiLocker.",
                "method": "GET",
                "path": "/documents",
                "requires_scopes": ["digilocker:docs:read"],
                "cache_ttl_s": 300,
                "paginated": False,
                "max_items": 50,
                "is_write": False,
                "response_items_path": "documents",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "digilocker_fetch_document",
                "description": "Fetch a single verified document's details by doc_id (e.g. to read or fill a KYC form).",
                "method": "GET",
                "path": "/documents/{doc_id}",
                "requires_scopes": ["digilocker:docs:read"],
                "cache_ttl_s": 300,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "response_items_path": "document",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "doc_id": {"type": "string", "description": "Document id from digilocker_list_documents."},
                    },
                    "required": ["doc_id"],
                },
            },
        ],
    },
]
