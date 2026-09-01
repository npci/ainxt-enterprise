# SPDX-License-Identifier: Apache-2.0
# ============================================================
# HIERARCHY SERVICE — read-only helpers for the reporting-manager
# team-budget view.
#
# Uses ``hierarchy_table`` for fast indexed lookups at runtime:
#   SELECT ... FROM hierarchy_table WHERE lower(root_manager_email) = :email
#
# The hierarchy_table is fully rebuilt (never incrementally patched) from
# ``users.manager_dn`` (primary source) with ``org_tree`` as fallback, via
# workers/hierarchy_rebuild_worker.py::rebuild_hierarchy_table_if_dirty().
# That worker polls a Redis dirty flag ("hierarchy_table:dirty") on a
# fixed interval (registered in workers/start_workers.py's interval_jobs)
# and performs at most one rebuild per tick, debouncing any number of
# logins that dirtied the flag in between. The flag itself is set by
# services/user_directory_sync.py whenever a login-time live-AD check
# detects a real department/manager change for that user. It can also be
# triggered manually: `python workers/hierarchy_rebuild_worker.py --force`.
#
# The trg_users_hierarchy_sync trigger checked for below is NOT created by
# any code in this repo — if present at all, it was added directly against
# the DB out of band. Treat the health check purely as a defence-in-depth
# signal, not as something this codebase provisions or relies on.
# ============================================================

from __future__ import annotations

from typing import Optional, List, Dict

from core.logger import logger
from db.database import SessionLocal


# ── Public API ───────────────────────────────────────────────────────────────

def get_caller_and_subtree(email: str, *, max_rows: int = 1000) -> Dict:
    """Single-session lookup: caller info + has_reports flag + full subtree.

    Consolidates get_caller_info, has_direct_reports, and get_subtree into
    one DB session with two queries (users lookup + hierarchy_table SELECT).

    Returns
    -------
    dict with keys:
        caller : dict or None
        has_reports : bool
        subtree : list[dict]
        trigger_healthy : bool   — True if the trigger exists on users table
    """
    if not email:
        return {"caller": None, "has_reports": False, "subtree": [], "trigger_healthy": True}

    from sqlalchemy import text
    from db.models import User

    clean_email = email.strip().lower()
    db = SessionLocal()
    try:
        # 1. Caller info from users table
        u = db.query(User).filter(User.email.ilike(clean_email)).first()
        if not u:
            return {"caller": None, "has_reports": False, "subtree": [], "trigger_healthy": True}

        caller = {
            "user_id":    str(u.id),
            "name":       u.name or "",
            "email":      (u.email or "").lower(),
            "department": u.department or "",
            "title":      u.ad_title or "",
            "manager_dn": (u.manager_dn or "").strip(),
        }

        # 2. Subtree from hierarchy_table (case-insensitive match)
        rows = db.execute(
            text("""
                SELECT id, name, email, department, ad_title,
                       manager_dn, ad_level, level
                FROM   hierarchy_table
                WHERE  lower(root_manager_email) = :email
                ORDER  BY level ASC, name ASC
                LIMIT  :lim
            """),
            {"email": clean_email, "lim": max_rows},
        ).fetchall()

        subtree = []
        for r in rows:
            subtree.append({
                "user_id":         str(r.id) if r.id else "",
                "display_name":    r.name or "",
                "mail":            (r.email or "").lower().strip(),
                "title":           r.ad_title or "",
                "department":      r.department or "",
                "manager_node_id": r.manager_dn or "",
                "level":           r.ad_level if r.ad_level is not None else 0,
                "relative_depth":  r.level if r.level is not None else 1,
            })

        if len(rows) >= max_rows:
            logger.warning(
                "hierarchy_service get_subtree hit max_rows cap (%d) for caller=%s",
                max_rows, clean_email,
            )

        # 3. Trigger health check — confirm trigger exists on users table
        trigger_healthy = True
        try:
            trig = db.execute(text("""
                SELECT 1 FROM pg_trigger
                WHERE tgrelid = (current_schema() || '.users')::regclass
                  AND tgname = 'trg_users_hierarchy_sync'
                  AND tgenabled != 'D'
            """)).fetchone()
            trigger_healthy = trig is not None
            if not trigger_healthy:
                logger.error(
                    "hierarchy_service CRITICAL: trg_users_hierarchy_sync trigger "
                    "is missing or disabled on ainxt.users — hierarchy_table data may be stale"
                )
        except Exception:
            pass  # pg_trigger query failed — don't block the response

        return {
            "caller": caller,
            "has_reports": len(subtree) > 0,
            "subtree": subtree,
        }
    except Exception as exc:
        logger.error("hierarchy_service get_caller_and_subtree error: %s", exc)
        return {"caller": None, "has_reports": False, "subtree": [], "trigger_healthy": True}
    finally:
        db.close()


def has_direct_reports(caller_email: str) -> bool:
    """Return True if `caller_email` has anyone (at any depth) reporting to
    them, walking org_tree's live manager graph recursively.

    Deliberately does NOT read hierarchy_table: that table is a periodically
    rebuilt snapshot (see workers/hierarchy_rebuild_worker.py) which is only
    refreshed on a live-AD login detecting a change, or a scheduled job that
    is disabled by default (HIERARCHY_REBUILD_ENABLED=false). A direct
    org_tree edit — or any other change that doesn't go through that
    pipeline — would silently not show up here for a long time, or ever, if
    the caller never logs in again to re-trigger a sync. Querying org_tree
    directly (same approach as routers/budget_router.py::_get_org_tree_subtree)
    makes this always reflect the current org_tree contents.
    """
    if not caller_email:
        return False
    db = SessionLocal()
    try:
        from sqlalchemy import text
        row = db.execute(
            text("""
                WITH RECURSIVE subtree AS (
                    SELECT node_id
                    FROM   org_tree
                    WHERE  manager = (
                             SELECT node_id FROM org_tree
                             WHERE  lower(mail) = lower(:email)
                             LIMIT  1
                           )
                    UNION ALL
                    SELECT o.node_id
                    FROM   org_tree o
                    JOIN   subtree s ON o.manager = s.node_id
                )
                SELECT 1 FROM subtree LIMIT 1
            """),
            {"email": caller_email.strip().lower()},
        ).fetchone()
        return row is not None
    except Exception as exc:
        logger.warning("hierarchy_service has_direct_reports error: %s", exc)
        return False
    finally:
        db.close()
