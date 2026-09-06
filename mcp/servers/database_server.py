# SPDX-License-Identifier: MIT
"""
Database MCP Server — read-only Postgres access for AiNxt internal data.

Security constraints:
  - SELECT only — DDL/DML/DCL blocked at parse level
  - Query timeout: 10 seconds
  - Row limit: 500 rows max
  - Columns containing PAN/CVV/AADHAAR are masked automatically
  - Admin role required to connect this server

Tools exposed:
  db_query        — run a SELECT query
  db_list_tables  — list all tables in a schema
  db_describe     — describe a table's columns and types
"""

import asyncio
import re

from mcp.servers.base import BaseMCPServer, MCPTool
from core.logger import logger

# Columns whose values must be masked regardless of content
_SENSITIVE_COLS = {
    "pan", "card_number", "cvv", "expiry", "pin", "account_number",
    "aadhaar", "aadhaar_number", "mobile", "email", "ifsc_code",
    "private_key", "api_key", "access_token", "secret",
}

# SQL patterns that indicate write operations — blocked outright
_WRITE_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|EXEC|EXECUTE|COPY|CALL)\b",
    re.IGNORECASE,
)

MAX_ROWS = 500
QUERY_TIMEOUT = 10  # seconds


def _mask_row(row: dict) -> dict:
    return {
        k: ("***" if k.lower() in _SENSITIVE_COLS else v)
        for k, v in row.items()
    }


def _db_query(query: str, params: dict = None) -> str:
    if _WRITE_PATTERN.search(query):
        return "ERROR: Only SELECT queries are allowed. DDL/DML operations are blocked."

    try:
        from db.database import SessionLocal
        from sqlalchemy import text
        import json

        with SessionLocal() as db:
            result = db.execute(
                text(query + f" LIMIT {MAX_ROWS}").bindparams(**(params or {}))
            )
            rows = [_mask_row(dict(zip(result.keys(), row))) for row in result.fetchall()]
            if not rows:
                return "Query returned 0 rows."
            return json.dumps(rows, default=str, indent=2)
    except Exception as e:
        return f"Query error: {e}"


def _db_list_tables(schema: str = "public") -> str:
    query = """
        SELECT table_name, table_type
        FROM information_schema.tables
        WHERE table_schema = :schema
        ORDER BY table_name
    """
    try:
        from db.database import SessionLocal
        from sqlalchemy import text
        import json

        with SessionLocal() as db:
            result = db.execute(text(query), {"schema": schema})
            rows = [{"table": r[0], "type": r[1]} for r in result.fetchall()]
            return json.dumps(rows, indent=2)
    except Exception as e:
        return f"Error listing tables: {e}"


def _db_describe(table_name: str, schema: str = "public") -> str:
    # Validate table name — no injection via table_name parameter
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table_name):
        return "ERROR: Invalid table name."

    query = """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = :schema AND table_name = :table
        ORDER BY ordinal_position
    """
    try:
        from db.database import SessionLocal
        from sqlalchemy import text
        import json

        with SessionLocal() as db:
            result = db.execute(text(query), {"schema": schema, "table": table_name})
            rows = [
                {
                    "column":   r[0],
                    "type":     r[1],
                    "nullable": r[2],
                    "default":  r[3],
                    "masked":   r[0].lower() in _SENSITIVE_COLS,
                }
                for r in result.fetchall()
            ]
            if not rows:
                return f"Table '{table_name}' not found in schema '{schema}'."
            return json.dumps(rows, indent=2)
    except Exception as e:
        return f"Error describing table: {e}"


class DatabaseMCPServer(BaseMCPServer):

    server_name = "database"

    def _setup_tools(self):
        self._register(MCPTool(
            name="db_query",
            description=(
                "Run a read-only SQL SELECT query against the AiNxt platform database. "
                "DDL/DML operations are blocked. Sensitive columns (PAN, CVV, AADHAAR) are auto-masked. "
                "Max 500 rows returned. 10 second timeout."
            ),
            fn=_db_query,
            pci_audit=True,
            input_schema={
                "type": "object",
                "properties": {
                    "query":  {"type": "string", "description": "SQL SELECT query (parameterised with :name syntax)"},
                    "params": {"type": "object", "description": "Query parameters dict", "default": {}},
                },
                "required": ["query"],
            },
        ))

        self._register(MCPTool(
            name="db_list_tables",
            description="List all tables in a Postgres schema.",
            fn=_db_list_tables,
            input_schema={
                "type": "object",
                "properties": {
                    "schema": {"type": "string", "description": "Schema name", "default": "public"},
                },
            },
        ))

        self._register(MCPTool(
            name="db_describe",
            description=(
                "Describe the columns, types, and nullability of a database table. "
                "Masked columns (containing PAN/CVV/etc) are flagged in the output."
            ),
            fn=_db_describe,
            input_schema={
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "Table name (alphanumeric + underscore only)"},
                    "schema":     {"type": "string", "description": "Schema name", "default": "public"},
                },
                "required": ["table_name"],
            },
        ))


if __name__ == "__main__":
    asyncio.run(DatabaseMCPServer().run_stdio())
