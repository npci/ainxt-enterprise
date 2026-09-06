# SPDX-License-Identifier: MIT
# ============================================================
# core.ckms.repository — read keys_table and key_type_mapping
#
# Only reads. Inserts/updates are handled by ops tooling
# (requirement §"Out of Scope").
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from sqlalchemy import text

from db.database import SessionLocal


@dataclass(frozen=True)
class KeyRow:
    """In-memory view of one ``keys_table`` row."""

    key_name: str   # logical name, e.g. "KEY_CREDS"
    dek: str        # DEK_KEK hex, or "BASE:<base64>"
    kek: str        # KEK_LMK hex (ignored for BASE rows)
    status: str     # 'A' or 'I'


def load_active_keys() -> List[KeyRow]:
    """Return every row in ``ainxt.keys_table`` with status='A'."""
    sql = text(
        "SELECT key_name, dek, kek, status "
        "FROM ainxt.keys_table "
        "WHERE status = 'A'"
    )
    with SessionLocal() as session:
        rows = session.execute(sql).fetchall()

    return [
        KeyRow(
            key_name=r[0],
            dek=r[1],
            kek=r[2] or "",
            status=r[3],
        )
        for r in rows
    ]


def load_env_var_mapping() -> Dict[str, str]:
    """Return ``{env_var: key_type}`` from ``ainxt.key_type_mapping``.

    Env vars absent from the table fall back to ``KEY_CREDS`` at call sites
    (requirement §"Default rule"); that fallback lives in
    :class:`core.ckms.key_service.KeyService`, not here.
    """
    sql = text("SELECT env_var, key_type FROM ainxt.key_type_mapping")
    with SessionLocal() as session:
        rows = session.execute(sql).fetchall()
    return {r[0]: r[1] for r in rows}
