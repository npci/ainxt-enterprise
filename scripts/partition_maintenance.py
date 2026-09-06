#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Partition Maintenance Script
- Creates partitions for current month + next 2 months (idempotent)
- Drops partitions older than 18 months (configurable via PARTITION_RETENTION_MONTHS env)
- Runs ANALYZE on recent partitions (last 2 months)
- Logs all actions, dry-run mode via --dry-run flag
- Safe: CREATE IF NOT EXISTS pattern, DROP only after confirming retention passed
- Usage: python scripts/partition_maintenance.py [--dry-run] [--retention-months N]
"""

import argparse
import os
import sys
from datetime import date, datetime
from dateutil.relativedelta import relativedelta

# Allow running from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from db.database import engine
from core.logger import logger

# ── Configuration ─────────────────────────────────────────────────────────────

PARTITION_RETENTION_MONTHS = int(os.environ.get("PARTITION_RETENTION_MONTHS", "18"))

# Tables partitioned by RANGE on created_at (monthly)
# Naming convention: {table}_{YYYY}_{MM}  e.g. chat_messages_2026_03
PARTITIONED_TABLES = [
    "chat_messages",
    "thread_messages",
    "model_usages",
    "rag_access_log",
]

# ── Helpers ───────────────────────────────────────────────────────────────────


def _partition_name(table: str, year: int, month: int) -> str:
    """Return partition table name for a given table / year / month."""
    return f"{table}_{year:04d}_{month:02d}"


def _month_range(year: int, month: int) -> tuple[str, str]:
    """
    Return (from_date, to_date) strings for a partition covering one calendar month.
    e.g. (2026-03-01, 2026-04-01)
    """
    from_dt = date(year, month, 1)
    to_dt = from_dt + relativedelta(months=1)
    return from_dt.strftime("%Y-%m-%d"), to_dt.strftime("%Y-%m-%d")


def _log(dry_run: bool, msg: str) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    logger.info(f"{prefix}{msg}")
    print(f"[{datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}] {prefix}{msg}")


# ── Core operations ───────────────────────────────────────────────────────────


def list_existing_partitions(conn) -> dict[str, list[str]]:
    """
    Query pg_inherits + pg_class to list all child partition tables grouped by parent.
    Returns: { parent_table_name: [child_partition_name, ...] }
    """
    sql = text("""
        SELECT
            parent_ns.nspname || '.' || parent_cls.relname  AS parent_table,
            child_ns.nspname  || '.' || child_cls.relname   AS child_table,
            child_cls.relname                                AS child_name
        FROM pg_inherits inh
        JOIN pg_class     child_cls  ON child_cls.oid  = inh.inhrelid
        JOIN pg_namespace child_ns   ON child_ns.oid   = child_cls.relnamespace
        JOIN pg_class     parent_cls ON parent_cls.oid = inh.inhparent
        JOIN pg_namespace parent_ns  ON parent_ns.oid  = parent_cls.relnamespace
        WHERE parent_cls.relname = ANY(:tables)
        ORDER BY parent_cls.relname, child_cls.relname
    """)
    rows = conn.execute(sql, {"tables": PARTITIONED_TABLES}).fetchall()

    result: dict[str, list[str]] = {t: [] for t in PARTITIONED_TABLES}
    for row in rows:
        # parent_table may include schema prefix; strip it
        parent = row.parent_table.split(".")[-1]
        if parent in result:
            result[parent].append(row.child_name)

    return result


def create_future_partitions(conn, dry_run: bool = False) -> list[str]:
    """
    Create partitions for the current month + next 2 months.
    Uses CREATE TABLE IF NOT EXISTS ... PARTITION OF ... pattern — safe to run repeatedly.
    Returns list of partition names that were created (or would be in dry-run).
    """
    today = date.today()
    months_to_create = [today + relativedelta(months=i) for i in range(3)]  # 0, +1, +2

    created: list[str] = []

    for offset_date in months_to_create:
        year, month = offset_date.year, offset_date.month
        from_date, to_date = _month_range(year, month)

        for table in PARTITIONED_TABLES:
            partition = _partition_name(table, year, month)
            sql = f"""
                CREATE TABLE IF NOT EXISTS {partition}
                    PARTITION OF {table}
                    FOR VALUES FROM ('{from_date}') TO ('{to_date}')
            """
            _log(dry_run, f"CREATE partition {partition} FOR VALUES FROM '{from_date}' TO '{to_date}'")
            if not dry_run:
                try:
                    conn.execute(text(sql))
                    conn.commit()
                    logger.info(f"Partition created (or already exists): {partition}")
                except Exception as exc:
                    conn.rollback()
                    logger.error(f"Failed to create partition {partition}: {exc}")
                    continue
            created.append(partition)

    return created


def drop_old_partitions(
    conn,
    dry_run: bool = False,
    retention_months: int = PARTITION_RETENTION_MONTHS,
) -> list[str]:
    """
    Drop partitions older than `retention_months` months.
    Safety check: only drops if the partition name follows the expected
    {table}_{YYYY}_{MM} convention and the date is beyond the retention window.
    Does NOT check row count — partitions older than retention window are dropped
    unconditionally (data at that age should have been archived already).
    Returns list of partition names that were dropped (or would be in dry-run).
    """
    today = date.today()
    cutoff = today - relativedelta(months=retention_months)

    existing = list_existing_partitions(conn)
    dropped: list[str] = []

    for table, partitions in existing.items():
        for partition in partitions:
            # Parse the YYYY_MM suffix from partition name
            # Expected pattern: {table}_{YYYY}_{MM}
            prefix = f"{table}_"
            if not partition.startswith(prefix):
                logger.warning(f"Skipping unexpected partition name: {partition}")
                continue

            suffix = partition[len(prefix):]
            parts = suffix.split("_")
            if len(parts) < 2:
                logger.warning(f"Cannot parse date from partition: {partition}")
                continue

            try:
                year  = int(parts[0])
                month = int(parts[1])
            except ValueError:
                logger.warning(f"Cannot parse year/month from partition: {partition}")
                continue

            partition_date = date(year, month, 1)
            if partition_date >= cutoff:
                # Within retention window — keep it
                continue

            _log(
                dry_run,
                f"DROP partition {partition} (date={partition_date}, cutoff={cutoff})",
            )
            if not dry_run:
                try:
                    conn.execute(text(f"DROP TABLE IF EXISTS {partition}"))
                    conn.commit()
                    logger.info(f"Partition dropped: {partition}")
                except Exception as exc:
                    conn.rollback()
                    logger.error(f"Failed to drop partition {partition}: {exc}")
                    continue
            dropped.append(partition)

    return dropped


def analyze_recent_partitions(conn, dry_run: bool = False) -> list[str]:
    """
    Run ANALYZE on partitions for the current month and previous month.
    This keeps planner statistics fresh for the two most-active partitions.
    Returns list of partition names that were analyzed (or would be in dry-run).
    """
    today = date.today()
    months_to_analyze = [today - relativedelta(months=1), today]  # prev + current

    analyzed: list[str] = []

    for offset_date in months_to_analyze:
        year, month = offset_date.year, offset_date.month

        for table in PARTITIONED_TABLES:
            partition = _partition_name(table, year, month)

            # Only ANALYZE if the partition actually exists
            exists_sql = text("""
                SELECT 1 FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = :pname
                  AND c.relkind = 'r'
                LIMIT 1
            """)
            row = conn.execute(exists_sql, {"pname": partition}).fetchone()
            if row is None:
                logger.debug(f"Skipping ANALYZE — partition does not exist: {partition}")
                continue

            _log(dry_run, f"ANALYZE {partition}")
            if not dry_run:
                try:
                    # ANALYZE cannot run inside a transaction block in some PG configs;
                    # use autocommit-compatible execution
                    conn.execute(text(f"ANALYZE {partition}"))
                    conn.commit()
                    logger.info(f"ANALYZE complete: {partition}")
                except Exception as exc:
                    conn.rollback()
                    logger.error(f"ANALYZE failed for {partition}: {exc}")
                    continue
            analyzed.append(partition)

    return analyzed


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monthly PostgreSQL partition maintenance for AiNxt platform"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log actions without executing any DDL statements",
    )
    parser.add_argument(
        "--retention-months",
        type=int,
        default=PARTITION_RETENTION_MONTHS,
        help=f"Drop partitions older than N months (default: {PARTITION_RETENTION_MONTHS})",
    )
    args = parser.parse_args()

    dry_run          = args.dry_run
    retention_months = args.retention_months

    if dry_run:
        print("=" * 60)
        print("DRY-RUN MODE — no DDL will be executed")
        print("=" * 60)

    logger.info(
        f"Partition maintenance starting — dry_run={dry_run}, "
        f"retention_months={retention_months}"
    )

    with engine.connect() as conn:
        # ── 1. List existing partitions (informational) ────────────────
        print("\n--- Existing partitions ---")
        existing = list_existing_partitions(conn)
        total_existing = 0
        for table, parts in existing.items():
            print(f"  {table}: {len(parts)} partition(s)")
            for p in sorted(parts):
                print(f"    {p}")
            total_existing += len(parts)
        print(f"  Total: {total_existing} partitions\n")

        # ── 2. Create future partitions ────────────────────────────────
        print("--- Creating future partitions (current + next 2 months) ---")
        created = create_future_partitions(conn, dry_run=dry_run)
        print(f"  Partitions created: {len(created)}\n")

        # ── 3. Drop old partitions ─────────────────────────────────────
        print(f"--- Dropping partitions older than {retention_months} months ---")
        dropped = drop_old_partitions(conn, dry_run=dry_run, retention_months=retention_months)
        print(f"  Partitions dropped: {len(dropped)}\n")

        # ── 4. ANALYZE recent partitions ───────────────────────────────
        print("--- Running ANALYZE on recent partitions (last 2 months) ---")
        analyzed = analyze_recent_partitions(conn, dry_run=dry_run)
        print(f"  Partitions analyzed: {len(analyzed)}\n")

    # ── Summary ───────────────────────────────────────────────────────
    print("=" * 60)
    print("Partition maintenance summary")
    print(f"  Existing partitions : {total_existing}")
    print(f"  Created             : {len(created)}")
    print(f"  Dropped             : {len(dropped)}")
    print(f"  Analyzed            : {len(analyzed)}")
    if dry_run:
        print("  [DRY-RUN — no changes committed]")
    print("=" * 60)

    logger.info(
        f"Partition maintenance complete — created={len(created)}, "
        f"dropped={len(dropped)}, analyzed={len(analyzed)}"
    )


if __name__ == "__main__":
    main()
