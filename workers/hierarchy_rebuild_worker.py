#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# ============================================================
# HIERARCHY REBUILD WORKER — debounced hierarchy_table rebuild
#
# hierarchy_table has no incremental-update path: it is always fully
# recomputed from users.manager_dn (primary) + org_tree (fallback for
# manager identities not found among active users) via a recursive CTE.
# A single user's department/manager change can also change the
# root_manager/level rows for everyone below them in the chain, so a
# per-user patch would not be correct — only a full rebuild is.
#
# Rebuilding on every login would be wasteful (it scans every active user
# and walks their manager chain), so services/user_directory_sync.py just
# flags a Redis key ("hierarchy_table:dirty") whenever it detects a real
# department/manager change. This worker polls that flag (registered as an
# interval job in workers/start_workers.py, see `interval_jobs`) and
# performs at most one rebuild per poll tick, coalescing any number of
# logins that dirtied the flag in between — hence "debounced".
#
# The rebuild itself uses a build-then-swap pattern (hierarchy_table_new ->
# RENAME) so readers (services/hierarchy_service.py, routers/budget_router.py)
# never see an empty/half-built table mid-rebuild.
#
# On repeated failure (e.g. a structural issue like a permissions error),
# retries back off exponentially instead of re-running the full CTE scan
# every single 2-minute poll tick forever — see _BACKOFF_BASE_SECONDS /
# _BACKOFF_MAX_SECONDS / _consume_dirty_flag() below. A critical log fires
# once the failure streak crosses _ALERT_THRESHOLD so this isn't silently
# stuck in backoff indefinitely.
#
# SCHEDULED EXECUTION IS DISABLED BY DEFAULT. The interval job is only
# registered in workers/start_workers.py when HIERARCHY_REBUILD_ENABLED=true,
# and rebuild_hierarchy_table_if_dirty() itself short-circuits on that same
# flag as defense-in-depth. `--force` (manual/ad-hoc runs) always bypasses the
# flag so an operator can still refresh the table on demand.
#
# Can also be run manually / ad hoc:
#   python workers/hierarchy_rebuild_worker.py            # only if dirty (and enabled)
#   python workers/hierarchy_rebuild_worker.py --force    # unconditional (also resets backoff)
# ============================================================

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.logger import logger

HIERARCHY_DIRTY_KEY = "hierarchy_table:dirty"

# ── Failure backoff state (Redis) ─────────────────────────────────────────
# Consecutive-failure count and the epoch timestamp before which retries are
# suppressed. Both auto-expire so a resolved incident doesn't need manual
# cleanup — worst case the next real dirtying event re-triggers a rebuild
# attempt at full speed after the TTL lapses.
_FAIL_COUNT_KEY     = "hierarchy_table:rebuild_fail_count"
_BACKOFF_UNTIL_KEY  = "hierarchy_table:rebuild_backoff_until"
_STATE_TTL_SECONDS  = 24 * 3600   # auto-reset failure streak after 1 day

_BACKOFF_BASE_SECONDS = 120        # = the poll interval; first retry is un-delayed
_BACKOFF_MAX_SECONDS  = 3600        # cap backoff at 1 hour between retries
_ALERT_THRESHOLD      = 5           # log critical once streak reaches this many failures

# The canonical hierarchy_table rebuild query. Derives every (employee,
# ancestor-manager) pair by expanding org_tree.path — the authoritative
# top-down chain string from the AD sync — instead of recursively walking
# users.manager_dn. The old recursive-CTE version matched managers by
# display name against users.name, which silently dropped anyone whose
# manager was not an active platform user and mis-levelled the rest.
#
# path looks like "MD > CFO > VP Finance > Analyst" (HTML-escaped '&gt;'
# is normalised first). Each element except the last is an ancestor of the
# employee; `level` is the distance from that ancestor down to the employee,
# so the immediate manager is level 1. root_manager_email is resolved by
# matching the ancestor's display name back to org_tree.
#
# Targets hierarchy_table_new so the swap below is atomic from readers'
# perspective.
_REBUILD_SQL = """
CREATE TABLE hierarchy_table_new AS
WITH employee_paths AS (
    SELECT
        u.id::text AS id,
        COALESCE(NULLIF(TRIM(o.display_name), ''), TRIM(u.name)) AS name,
        LOWER(TRIM(u.email))                  AS email,
        COALESCE(u.department, '')            AS department,
        COALESCE(u.ad_title, '')              AS ad_title,
        COALESCE(u.manager_dn, '')            AS manager_dn,
        COALESCE(u.ad_username, '')           AS ad_username,
        COALESCE(u.account_status, 'active')  AS account_status,
        COALESCE(u.ad_level, 6)               AS ad_level,
        STRING_TO_ARRAY(
            REPLACE(REPLACE(o.path, '&gt;', '>'), ' > ', '>'),
            '>'
        ) AS path_parts
    FROM users u
    INNER JOIN org_tree o
        ON LOWER(TRIM(o.mail)) = LOWER(TRIM(u.email))
    WHERE u.is_active = TRUE
      AND NULLIF(TRIM(u.email), '') IS NOT NULL
      AND NULLIF(TRIM(o.path), '')  IS NOT NULL
),
expanded_hierarchy AS (
    SELECT
        ep.id, ep.name, ep.email, ep.department, ep.ad_title,
        ep.manager_dn, ep.ad_username, ep.account_status, ep.ad_level,
        TRIM(p.manager_name)                        AS root_manager,
        CARDINALITY(ep.path_parts) - p.position     AS level
    FROM employee_paths ep
    CROSS JOIN LATERAL
        UNNEST(ep.path_parts) WITH ORDINALITY AS p(manager_name, position)
    WHERE p.position < CARDINALITY(ep.path_parts)
      AND NULLIF(TRIM(p.manager_name), '') IS NOT NULL
),
manager_directory AS (
    SELECT DISTINCT ON (LOWER(TRIM(COALESCE(NULLIF(display_name, ''), node_id))))
        LOWER(TRIM(COALESCE(NULLIF(display_name, ''), node_id))) AS manager_key,
        LOWER(NULLIF(TRIM(mail), ''))                            AS manager_email
    FROM org_tree
    WHERE COALESCE(NULLIF(TRIM(display_name), ''), NULLIF(TRIM(node_id), '')) IS NOT NULL
    ORDER BY
        LOWER(TRIM(COALESCE(NULLIF(display_name, ''), node_id))),
        synced_at DESC NULLS LAST,
        id
)
SELECT
    eh.id,
    eh.name,
    eh.email,
    eh.department,
    eh.ad_title,
    eh.manager_dn,
    eh.ad_username,
    eh.account_status,
    eh.ad_level,
    eh.root_manager,
    eh.level,
    md.manager_email AS root_manager_email
FROM expanded_hierarchy eh
LEFT JOIN manager_directory md
    ON md.manager_key = LOWER(TRIM(eh.root_manager));
"""

# Index names carry a _new suffix so they never collide with the live table's
# indexes; _run_rebuild() renames them after the table swap.
_INDEX_SQL = [
    "CREATE INDEX hierarchy_table_new_email_idx "
    "ON hierarchy_table_new (LOWER(email));",

    "CREATE INDEX hierarchy_table_new_root_manager_idx "
    "ON hierarchy_table_new (LOWER(root_manager));",

    "CREATE INDEX hierarchy_table_new_root_manager_email_idx "
    "ON hierarchy_table_new (LOWER(root_manager_email));",

    "CREATE INDEX hierarchy_table_new_email_root_manager_idx "
    "ON hierarchy_table_new (LOWER(email), LOWER(root_manager));",

    "CREATE INDEX hierarchy_table_new_level_idx "
    "ON hierarchy_table_new (level);",
]

# Post-swap index renames: (from, to). Applied after hierarchy_table_new has
# been renamed to hierarchy_table so the index names match the live table.
_INDEX_RENAMES = [
    ("hierarchy_table_new_email_idx",             "hierarchy_table_email_idx"),
    ("hierarchy_table_new_root_manager_idx",      "hierarchy_table_root_manager_idx"),
    ("hierarchy_table_new_root_manager_email_idx", "hierarchy_table_root_manager_email_idx"),
    ("hierarchy_table_new_email_root_manager_idx", "hierarchy_table_email_root_manager_idx"),
    ("hierarchy_table_new_level_idx",             "hierarchy_table_level_idx"),
]


def _consume_dirty_flag() -> bool:
    """Return True and clear the flag if hierarchy_table is marked dirty.

    Not perfectly atomic (GET then DEL — the KVClient interface used across
    both supported backends does not expose GETDEL), but the worst case of
    the small race window is a redundant extra rebuild on the next tick,
    never a missed one — acceptable for a debounce, not a correctness issue.
    """
    try:
        from core.config import RDB_CACHE
        from core.kv import get_kv

        rc = get_kv(RDB_CACHE, decode_responses=True)
        if rc.get(HIERARCHY_DIRTY_KEY) == "1":
            rc.delete(HIERARCHY_DIRTY_KEY)
            return True
        return False
    except Exception as exc:
        logger.warning("hierarchy_rebuild_worker: dirty-flag check failed: %s", exc)
        return False


def _in_backoff_window() -> bool:
    """True if a prior failure streak means retries should be suppressed
    until `_BACKOFF_UNTIL_KEY`'s epoch timestamp. Fails open (returns False,
    i.e. allows the rebuild attempt) if Redis is unreachable, since a
    backoff check that can't be answered should never itself block a real
    rebuild."""
    try:
        from core.config import RDB_CACHE
        from core.kv import get_kv

        rc = get_kv(RDB_CACHE, decode_responses=True)
        until = rc.get(_BACKOFF_UNTIL_KEY)
        return bool(until) and time.time() < float(until)
    except Exception:
        return False


def _record_failure_and_backoff() -> int:
    """Increment the consecutive-failure counter and set a backoff window
    before the next retry is allowed, growing exponentially (capped) with
    each additional failure. Returns the new failure count (0 if the
    counter couldn't be read/written, e.g. Redis unreachable).
    """
    try:
        from core.config import RDB_CACHE
        from core.kv import get_kv

        rc = get_kv(RDB_CACHE, decode_responses=True)
        count = rc.incr(_FAIL_COUNT_KEY)
        rc.expire(_FAIL_COUNT_KEY, _STATE_TTL_SECONDS)

        delay = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (count - 1)))
        rc.setex(_BACKOFF_UNTIL_KEY, _STATE_TTL_SECONDS, str(time.time() + delay))

        # Re-flag dirty so the next eligible poll tick (once backoff lapses)
        # retries instead of silently leaving hierarchy_table stale forever.
        rc.set(HIERARCHY_DIRTY_KEY, "1")

        if count >= _ALERT_THRESHOLD:
            logger.critical(
                "hierarchy_rebuild_worker: %d consecutive rebuild failures — "
                "backing off %ds before next retry; hierarchy_table is stale "
                "and NOT being refreshed. Investigate before this compounds.",
                count, delay,
            )
        else:
            logger.warning(
                "hierarchy_rebuild_worker: rebuild failed (streak=%d) — "
                "backing off %ds before next retry",
                count, delay,
            )
        return count
    except Exception as exc:
        logger.warning("hierarchy_rebuild_worker: failed to record failure/backoff: %s", exc)
        return 0


def _clear_failure_streak() -> None:
    """Reset the failure/backoff state after a successful rebuild."""
    try:
        from core.config import RDB_CACHE
        from core.kv import get_kv

        rc = get_kv(RDB_CACHE, decode_responses=True)
        rc.delete(_FAIL_COUNT_KEY)
        rc.delete(_BACKOFF_UNTIL_KEY)
    except Exception as exc:
        logger.warning("hierarchy_rebuild_worker: failed to clear failure streak: %s", exc)


def _run_rebuild() -> int:
    """Build hierarchy_table_new + its indexes, then atomically swap it in
    for hierarchy_table. Returns the resulting row count."""
    from db.database import engine
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS hierarchy_table_new"))
        conn.execute(text(_REBUILD_SQL))
        for stmt in _INDEX_SQL:
            conn.execute(text(stmt))
        conn.execute(text("ANALYZE hierarchy_table_new"))
        row_count = conn.execute(text("SELECT COUNT(*) FROM hierarchy_table_new")).scalar()

        conn.execute(text("DROP TABLE IF EXISTS hierarchy_table_old"))
        # IF EXISTS: hierarchy_table is not created by any migration, so on a
        # fresh database the very first rebuild has nothing to rename away.
        conn.execute(text("ALTER TABLE IF EXISTS hierarchy_table RENAME TO hierarchy_table_old"))
        conn.execute(text("ALTER TABLE hierarchy_table_new RENAME TO hierarchy_table"))

        # Drop the previous run's indexes (now orphaned on hierarchy_table_old,
        # dropped with it below) before renaming the new ones into their place —
        # avoids a duplicate-name error if a prior partial run left them behind.
        # The legacy single-index name from the old rebuild query is included so
        # upgrading deployments clean it up on their first run.
        conn.execute(text("DROP INDEX IF EXISTS idx_hierarchy_root_manager_email"))
        for _old, new_name in _INDEX_RENAMES:
            conn.execute(text(f"DROP INDEX IF EXISTS {new_name}"))
        for old_name, new_name in _INDEX_RENAMES:
            conn.execute(text(f"ALTER INDEX {old_name} RENAME TO {new_name}"))

        conn.execute(text("DROP TABLE IF EXISTS hierarchy_table_old"))

    return int(row_count or 0)


def rebuild_hierarchy_table_if_dirty(force: bool = False) -> dict:
    """Poll the dirty flag (unless `force`); if set, rebuild hierarchy_table
    via a build-then-swap so readers never see a dropped/empty table.

    On repeated failure, retries back off exponentially (2min, 4min, 8min,
    ... capped at 1h) instead of re-running the full rebuild every single
    poll tick forever — a persistent failure (e.g. a permissions error)
    would otherwise turn into a self-inflicted load generator running a
    full table scan every 2 minutes indefinitely. `force` bypasses the
    HIERARCHY_REBUILD_ENABLED kill switch, the dirty-flag check AND the
    backoff window, and clears any existing failure streak on success —
    for manual/ad-hoc invocation.

    Returns {"rebuilt": bool, "row_count": int|None, "error": str|None}.
    """
    if not force:
        # Kill switch (default OFF). start_workers.py already skips registering
        # this job when disabled; this is defense-in-depth for any other caller.
        try:
            from core.config import HIERARCHY_REBUILD_ENABLED
        except Exception:
            HIERARCHY_REBUILD_ENABLED = False
        if not HIERARCHY_REBUILD_ENABLED:
            return {"rebuilt": False, "row_count": None, "error": None}

        if _in_backoff_window():
            return {"rebuilt": False, "row_count": None, "error": None}
        if not _consume_dirty_flag():
            return {"rebuilt": False, "row_count": None, "error": None}

    try:
        row_count = _run_rebuild()
        logger.info("hierarchy_rebuild_worker: rebuilt hierarchy_table (%d rows)", row_count)
        _clear_failure_streak()
        return {"rebuilt": True, "row_count": row_count, "error": None}
    except Exception as exc:
        logger.error("hierarchy_rebuild_worker: rebuild failed: %s", exc)
        _record_failure_and_backoff()
        return {"rebuilt": False, "row_count": None, "error": str(exc)}


if __name__ == "__main__":
    _force = "--force" in sys.argv[1:]
    print(rebuild_hierarchy_table_if_dirty(force=_force))
