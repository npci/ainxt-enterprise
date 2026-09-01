#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Backfill rag_mode on historical rows.

Run once after deploying the S26 migration. Idempotent and resumable —
safe to re-run on any environment.

Usage:
    python db/backfill_rag_mode.py [--batch-size 10000] [--dry-run]

Tables affected:
    - chat_messages   (via chats.rag_mode snapshot)
    - conversations   (via session_id → chat_id → chats.rag_mode heuristic)
    - ainxt.semantic_answer_cache  (via repo_filter presence)
    - ainxt.semantic_memory        (via scope_type + content.repo presence)

Rows with rag_mode already set are skipped (WHERE rag_mode IS NULL).
Ambiguous rows are marked 'unknown' and excluded from Generic reads
(fail-closed posture).

Works on both Linux and Windows (no shell dependencies; pure Python + SQL).
"""
import argparse
import os
import sys

def _cfg(key: str, default: str = "") -> str:
    """Read a configuration value from the environment.
    Generic accessor used for all connection parameters including credentials.
    """
    return os.environ.get(key, default)

# Allow running from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(_env_path, override=False)
except ImportError:
    pass

from db.database import DB_SCHEMA

# ── Migration engine (reuse migrate.py pattern) ──────────────────────────────
_MIG_USER = os.getenv("POSTGRES_MIGRATE_USER") or os.getenv("POSTGRES_USER", "postgres")
# No hardcoded localhost default — reuse core.config.POSTGRES_HOST (itself
# no-default) so this script and the app agree on what "unset" means.
from core.config import POSTGRES_DB as _CONFIG_POSTGRES_DB, POSTGRES_HOST as _CONFIG_POSTGRES_HOST
_MIG_HOST = os.getenv("POSTGRES_HOST", _CONFIG_POSTGRES_HOST)
_MIG_PORT = os.getenv("POSTGRES_PORT", "5432")
_MIG_DB   = os.getenv("POSTGRES_DB", _CONFIG_POSTGRES_DB)

from sqlalchemy import create_engine, text as _text
from sqlalchemy.engine import URL as _URL

engine = create_engine(
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
    echo=False,
)


def _column_exists(conn, table: str, column: str, schema: str = None) -> bool:
    """Check if a column exists on a table. Handles schema-qualified names.
    Defaults to DB_SCHEMA (typically 'ainxt') for unqualified table names."""
    if "." in table:
        schema, table = table.split(".", 1)
    elif schema is None:
        schema = DB_SCHEMA
    row = conn.execute(_text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = :schema AND table_name = :table AND column_name = :col"
    ), {"schema": schema, "table": table, "col": column}).fetchone()
    return row is not None


def _table_exists(conn, table: str, schema: str = None) -> bool:
    """Check if a table exists. Handles schema-qualified names.
    Defaults to DB_SCHEMA (typically 'ainxt') for unqualified table names."""
    if "." in table:
        schema, table = table.split(".", 1)
    elif schema is None:
        schema = DB_SCHEMA
    row = conn.execute(_text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = :schema AND table_name = :table"
    ), {"schema": schema, "table": table}).fetchone()
    return row is not None


def _backfill_chat_messages(batch_size: int, dry_run: bool) -> int:
    """
    chat_messages.rag_mode ← chats.rag_mode for the parent chat.

    Because Chat.rag_mode is mutable (user can toggle the KB switch), this is
    a best-effort snapshot: we use the chat's CURRENT rag_mode. Rows where the
    parent chat no longer exists get 'unknown'.
    """
    label = "chat_messages"
    total = 0
    with engine.connect() as conn:
        if not _column_exists(conn, "chat_messages", "rag_mode"):
            print(f"  [{label}] SKIPPED — rag_mode column not found (run migration first)")
            return 0
        # Step 1: tag rows that have a parent chat with a known rag_mode
        while True:
            sql = _text("""
                UPDATE chat_messages cm
                SET rag_mode = c.rag_mode
                FROM chats c
                WHERE cm.chat_id = c.id
                  AND cm.rag_mode IS NULL
                  AND c.rag_mode IS NOT NULL
                  AND cm.id IN (
                      SELECT id FROM chat_messages
                      WHERE rag_mode IS NULL
                      LIMIT :batch
                  )
            """)
            if dry_run:
                row = conn.execute(_text(
                    "SELECT count(*) FROM chat_messages WHERE rag_mode IS NULL"
                )).scalar()
                print(f"  [{label}] dry-run: {row} rows with NULL rag_mode")
                return 0
            result = conn.execute(sql, {"batch": batch_size})
            affected = result.rowcount
            conn.commit()
            total += affected
            print(f"  [{label}] tagged {affected} rows (running total: {total})")
            if affected < batch_size:
                break

        # Step 2: orphans (no parent chat) → 'unknown'
        result = conn.execute(_text("""
            UPDATE chat_messages
            SET rag_mode = 'unknown'
            WHERE rag_mode IS NULL
        """))
        orphans = result.rowcount
        conn.commit()
        if orphans:
            print(f"  [{label}] marked {orphans} orphan rows as 'unknown'")
        total += orphans
    return total


def _backfill_conversations(batch_size: int, dry_run: bool) -> int:
    """
    conversations.rag_mode ← heuristic via session_id.

    Cross-chat memory rows have session_id = 'user:{user_id}' and role = 'summary'.
    We cannot reliably infer their origin mode, so they get 'unknown'.
    Regular conversation rows have session_id = chat_id, so we join to chats.
    """
    label = "conversations"
    total = 0
    with engine.connect() as conn:
        if not _column_exists(conn, "conversations", "rag_mode"):
            print(f"  [{label}] SKIPPED — rag_mode column not found (run migration first)")
            return 0
        if dry_run:
            row = conn.execute(_text(
                "SELECT count(*) FROM conversations WHERE rag_mode IS NULL"
            )).scalar()
            print(f"  [{label}] dry-run: {row} rows with NULL rag_mode")
            return 0

        # Step 1: conversation rows whose session_id matches a chat_id
        while True:
            result = conn.execute(_text("""
                UPDATE conversations cv
                SET rag_mode = c.rag_mode
                FROM chats c
                WHERE cv.session_id = c.id::text
                  AND cv.rag_mode IS NULL
                  AND c.rag_mode IS NOT NULL
                  AND cv.id IN (
                      SELECT id FROM conversations
                      WHERE rag_mode IS NULL
                        AND session_id NOT LIKE 'user:%%'
                      LIMIT :batch
                  )
            """), {"batch": batch_size})
            affected = result.rowcount
            conn.commit()
            total += affected
            print(f"  [{label}] tagged {affected} chat-linked rows (total: {total})")
            if affected < batch_size:
                break

        # Step 2: remaining rows (cross-chat summaries, orphans) → 'unknown'
        result = conn.execute(_text("""
            UPDATE conversations
            SET rag_mode = 'unknown'
            WHERE rag_mode IS NULL
        """))
        remainder = result.rowcount
        conn.commit()
        if remainder:
            print(f"  [{label}] marked {remainder} remaining rows as 'unknown'")
        total += remainder
    return total


def _backfill_semantic_answer_cache(batch_size: int, dry_run: bool) -> int:
    """
    semantic_answer_cache.rag_mode ← 'on' if repo_filter IS NOT NULL, else 'unknown'.

    A row with a repo_filter cannot have come from Generic. Rows without one
    are ambiguous (could be Generic or KB-without-repo).
    """
    label = "semantic_answer_cache"
    with engine.connect() as conn:
        if not _table_exists(conn, f"{DB_SCHEMA}.semantic_answer_cache"):
            print(f"  [{label}] SKIPPED — table does not exist (pgvector not installed?)")
            return 0
        if not _column_exists(conn, f"{DB_SCHEMA}.semantic_answer_cache", "rag_mode"):
            print(f"  [{label}] SKIPPED — rag_mode column not found (run migration first)")
            return 0
        if dry_run:
            row = conn.execute(_text(
                "SELECT count(*) FROM ainxt.semantic_answer_cache WHERE rag_mode IS NULL"
            )).scalar()
            print(f"  [{label}] dry-run: {row} rows with NULL rag_mode")
            return 0

        # Rows with repo_filter → definitely not Generic
        r1 = conn.execute(_text("""
            UPDATE ainxt.semantic_answer_cache
            SET rag_mode = 'on'
            WHERE rag_mode IS NULL AND repo_filter IS NOT NULL
        """))
        tagged_on = r1.rowcount

        # Remaining → ambiguous
        r2 = conn.execute(_text("""
            UPDATE ainxt.semantic_answer_cache
            SET rag_mode = 'unknown'
            WHERE rag_mode IS NULL
        """))
        tagged_unknown = r2.rowcount

        conn.commit()
        total = tagged_on + tagged_unknown
        print(f"  [{label}] tagged {tagged_on} as 'on', {tagged_unknown} as 'unknown' (total: {total})")
    return total


def _backfill_semantic_memory(batch_size: int, dry_run: bool) -> int:
    """
    semantic_memory.rag_mode:
      - scope_type='org' (orchestrator-written) → 'on'
      - content::text LIKE '%%"repo"%%' (has repo in JSON) → 'on', source_repo ← content->>'repo'
      - everything else → 'unknown'
    """
    label = "semantic_memory"
    with engine.connect() as conn:
        if not _table_exists(conn, f"{DB_SCHEMA}.semantic_memory"):
            print(f"  [{label}] SKIPPED — table does not exist (pgvector not installed?)")
            return 0
        if not _column_exists(conn, f"{DB_SCHEMA}.semantic_memory", "rag_mode"):
            print(f"  [{label}] SKIPPED — rag_mode column not found (run migration first)")
            return 0
        if dry_run:
            row = conn.execute(_text(
                "SELECT count(*) FROM ainxt.semantic_memory WHERE rag_mode IS NULL"
            )).scalar()
            print(f"  [{label}] dry-run: {row} rows with NULL rag_mode")
            return 0

        # Org-scope rows (from orchestrator) → 'on'
        r1 = conn.execute(_text("""
            UPDATE ainxt.semantic_memory
            SET rag_mode = 'on'
            WHERE rag_mode IS NULL AND scope_type = 'org'
        """))
        tagged_org = r1.rowcount

        # Rows with a repo in content JSON → 'on' + extract source_repo
        r2 = conn.execute(_text("""
            UPDATE ainxt.semantic_memory
            SET rag_mode = 'on',
                source_repo = content->>'repo'
            WHERE rag_mode IS NULL
              AND content->>'repo' IS NOT NULL
              AND content->>'repo' != ''
        """))
        tagged_repo = r2.rowcount

        # Remaining → ambiguous
        r3 = conn.execute(_text("""
            UPDATE ainxt.semantic_memory
            SET rag_mode = 'unknown'
            WHERE rag_mode IS NULL
        """))
        tagged_unknown = r3.rowcount

        conn.commit()
        total = tagged_org + tagged_repo + tagged_unknown
        print(
            f"  [{label}] tagged {tagged_org} org-scope as 'on', "
            f"{tagged_repo} repo-bearing as 'on', "
            f"{tagged_unknown} as 'unknown' (total: {total})"
        )
    return total


def main():
    parser = argparse.ArgumentParser(description="Backfill rag_mode on historical rows")
    parser.add_argument("--batch-size", type=int, default=10000, help="Rows per UPDATE batch")
    parser.add_argument("--dry-run", action="store_true", help="Print counts without modifying data")
    args = parser.parse_args()

    print(f"Backfilling rag_mode (batch_size={args.batch_size}, dry_run={args.dry_run})...")
    grand_total = 0

    grand_total += _backfill_chat_messages(args.batch_size, args.dry_run)
    grand_total += _backfill_conversations(args.batch_size, args.dry_run)
    grand_total += _backfill_semantic_answer_cache(args.batch_size, args.dry_run)
    grand_total += _backfill_semantic_memory(args.batch_size, args.dry_run)

    if args.dry_run:
        print("Dry run complete — no rows modified.")
    else:
        print(f"Backfill complete — {grand_total} total rows updated.")


if __name__ == "__main__":
    main()
