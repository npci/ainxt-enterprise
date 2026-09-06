#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# ============================================================
# IMAGE MIGRATION — generated_images table setup
#
# Standalone migration for the image persistence feature.
# Idempotent (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS).
#
# Usage:
#   python db/image_migrate.py
#   python -m db.image_migrate
# ============================================================

import sys
import os

def _cfg(key: str, default: str = "") -> str:
    """Read a configuration value from the environment.
    Generic accessor used for all connection parameters including credentials.
    """
    return os.environ.get(key, default)

# Allow running from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env before database.py reads os.getenv()
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(_env_path, override=False)
except ImportError:
    pass  # dotenv not installed — fall back to shell env vars

# CKMS — decrypt POSTGRES_PASSWORD / POSTGRES_MIGRATE_PASSWORD before db.database
# is imported. Fail-fast on any HSM / DB / crypto error.
from core.ckms import load_at_boot as _ckms_load_at_boot
_ckms_load_at_boot()

from db.database import DB_SCHEMA

# ── Migration engine (same connection logic as db/migrate.py) ────────────────
_MIG_USER = os.getenv("POSTGRES_MIGRATE_USER") or os.getenv("POSTGRES_USER", "postgres")
# No hardcoded localhost default — reuse core.config.POSTGRES_HOST (itself
# no-default) so this script and the app agree on what "unset" means.
from core.config import POSTGRES_DB as _CONFIG_POSTGRES_DB, POSTGRES_HOST as _CONFIG_POSTGRES_HOST
_MIG_HOST = os.getenv("POSTGRES_HOST", _CONFIG_POSTGRES_HOST)
_MIG_PORT = os.getenv("POSTGRES_PORT", "5432")
_MIG_DB   = os.getenv("POSTGRES_DB", _CONFIG_POSTGRES_DB)

_MIG_CONNECT_ARGS = {
    "connect_timeout": 10,
    "options": f"-c statement_timeout=120000 -c search_path={DB_SCHEMA},public",
}

from sqlalchemy import create_engine as _create_engine
from sqlalchemy.engine import URL as _URL
engine = _create_engine(
    _URL.create(
        drivername="postgresql+psycopg2",
        username=_MIG_USER,
        password=_cfg("POSTGRES_MIGRATE_PASSWORD") or _cfg("POSTGRES_PASSWORD"),
        host=_MIG_HOST,
        port=int(_MIG_PORT),
        database=_MIG_DB,
        query={"options": f"-csearch_path={DB_SCHEMA},public"},
    ),
    pool_pre_ping=True,
    pool_size=2,
    max_overflow=0,
    pool_timeout=30,
    connect_args=_MIG_CONNECT_ARGS,
    echo=False,
)


def _run_ddl(sql: str, label: str = "") -> None:
    """Execute raw DDL on the main engine. Idempotent via IF NOT EXISTS/IF EXISTS."""
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                conn.execute(_text(stmt))
            conn.commit()
        if label:
            print(f"  ✓ {label}")
    except Exception as exc:
        print(f"  ! DDL error ({label or 'unknown'}): {exc}")


def run_image_migration():
    """Create the generated_images table and indexes (idempotent)."""
    print("image_migrate: running generated_images table migration ...")

    _run_ddl("""
        CREATE TABLE IF NOT EXISTS generated_images (
            id          VARCHAR(36)  PRIMARY KEY,
            user_id     VARCHAR(255) NOT NULL,
            chat_id     VARCHAR(36),
            provider    VARCHAR(20)  NOT NULL,
            prompt      TEXT,
            filename    VARCHAR(512) NOT NULL,
            file_path   TEXT         NOT NULL,
            mime_type   VARCHAR(50)  NOT NULL DEFAULT 'image/png',
            size_bytes  INTEGER,
            created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_genimg_user    ON generated_images(user_id);
        CREATE INDEX IF NOT EXISTS idx_genimg_chat    ON generated_images(chat_id) WHERE chat_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_genimg_created ON generated_images(created_at DESC)
    """, label="generated_images table + indexes")

    print("image_migrate: done.")


if __name__ == "__main__":
    run_image_migration()
