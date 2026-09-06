# SPDX-License-Identifier: MIT
# ============================================================
# USER DIRECTORY SYNC — live-AD staleness check on login
#
# Called from routers/auth_router.py::login() as the single fire-and-forget
# background sync (never on the request/response path) — see
# routers/auth_router.py::_run_post_login_sync(), which runs this BEFORE
# the org_tree-snapshot sync in the same thread so the two can never race
# and clobber each other's writes to the same `users` columns.
#
# Reuses the AD attributes already fetched by auth.ldap_handler.
# authenticate_user() during this same login (passed in as `ldap_attrs`)
# instead of issuing a second LDAP search — halves AD query volume per
# login. Falls back to a fresh auth.ldap_handler.get_user_attributes()
# lookup only when called without attrs on hand.
#
# If department or manager actually changed since the last known value:
#   1. Updates ainxt.users (department, manager_dn, ad_title, ad_username,
#      ad_dn, last_ad_sync).
#   2. Upserts the matching ainxt.org_tree row (by mail) so org_tree does
#      not drift from the freshly-observed AD state.
#   3. Flags ainxt.hierarchy_table as dirty (Redis) so the debounced
#      rebuild worker (workers/hierarchy_rebuild_worker.py) picks it up on
#      its next poll tick, instead of rebuilding the full table on every
#      single login.
#
# This is a narrower, faster complement to the nightly workers/ad_sync.py
# full sync — it does NOT replace it. It is a no-op whenever LDAP_ENABLED
# is false, and never raises (every step best-effort / non-blocking, so a
# failure here can never fail or slow down a login).
# ============================================================

from __future__ import annotations

from typing import Optional

from core.logger import logger

HIERARCHY_DIRTY_KEY = "hierarchy_table:dirty"


def _mark_hierarchy_dirty() -> None:
    """Flag hierarchy_table as needing a rebuild. Best-effort — if Redis is
    unreachable, the debounced worker will just pick up the next dirtying
    event instead; this must never raise into the caller."""
    try:
        from core.config import RDB_CACHE
        from core.kv import get_kv

        rc = get_kv(RDB_CACHE, decode_responses=True)
        rc.set(HIERARCHY_DIRTY_KEY, "1")
    except Exception as exc:
        logger.warning("user_directory_sync: failed to set hierarchy dirty flag: %s", exc)


def _parse_direct_reports(raw: str | None) -> list[str]:
    """Parse a comma-separated direct_reports string into a list of stripped entries.
    Also handles semicolon-separated legacy entries gracefully."""
    if not raw:
        return []
    # Normalise: treat both ',' and ';' as separators to handle any legacy format
    normalised = raw.replace(";", ",")
    return [p.strip() for p in normalised.split(",") if p.strip()]


def _format_direct_reports(entries: list[str]) -> str | None:
    """Serialise a list of direct-report entries back to the ', '-separated format
    consistent with the existing org_tree data.
    Returns None if the list is empty (stored as NULL in org_tree)."""
    filtered = [e for e in entries if e]
    return ", ".join(filtered) if filtered else None


def _resolve_manager_email(db, manager_dn: str) -> str | None:
    """Resolve a manager identifier to their email address.

    Handles both full LDAP DNs (CN=...) and display names (the format
    stored on PROD in users.manager_dn and org_tree.manager).

    Lookup chain:
      For full DNs (CN= prefix):
        1. org_tree.dn  → org_tree.mail
        2. users.ad_dn  → users.email
      For display names:
        3. users.name   → users.email
        4. org_tree.display_name → org_tree.mail

    Returns the lowercase email, or None if unresolvable.
    """
    from sqlalchemy import text

    if not manager_dn:
        return None

    if manager_dn.strip().upper().startswith("CN="):
        # Full LDAP DN path
        row = db.execute(
            text("SELECT mail FROM org_tree WHERE lower(dn) = lower(:dn) LIMIT 1"),
            {"dn": manager_dn},
        ).fetchone()
        if row and row.mail:
            return row.mail.lower()

        row = db.execute(
            text("SELECT email FROM users WHERE lower(ad_dn) = lower(:dn) LIMIT 1"),
            {"dn": manager_dn},
        ).fetchone()
        if row and row.email:
            return row.email.lower()
    else:
        # Display name path (PROD format)
        row = db.execute(
            text("SELECT email FROM users WHERE lower(name) = lower(:name) LIMIT 1"),
            {"name": manager_dn},
        ).fetchone()
        if row and row.email:
            return row.email.lower()

        row = db.execute(
            text("SELECT mail FROM org_tree WHERE lower(display_name) = lower(:name) LIMIT 1"),
            {"name": manager_dn},
        ).fetchone()
        if row and row.mail:
            return row.mail.lower()

    return None


def _resolve_manager_display_name(db, manager_dn: str) -> str | None:
    """Resolve a manager's AD DN to their display name — the format used
    consistently across users.manager_dn, org_tree.manager, and org_tree.parent_id
    on PROD (display names, never raw LDAP DNs).

    Lookup chain (email-first, then display name):
      1. Resolve DN → email via _resolve_manager_email()
      2. email → users.name          (preferred — always present for active users)
      3. email → org_tree.display_name (fallback if not in users)

    Returns the display name string, or None if it cannot be resolved.
    Falls back to returning the raw DN only as a last resort so callers
    always have something to write rather than NULL.
    """
    from sqlalchemy import text

    if not manager_dn:
        return None

    # If it's already a display name (no CN= prefix), return as-is
    if not manager_dn.strip().upper().startswith("CN="):
        return manager_dn

    mgr_email = _resolve_manager_email(db, manager_dn)
    if not mgr_email:
        return None

    row = db.execute(
        text("SELECT name FROM users WHERE lower(email) = lower(:email) LIMIT 1"),
        {"email": mgr_email},
    ).fetchone()
    if row and row.name:
        return row.name

    row = db.execute(
        text("SELECT display_name FROM org_tree WHERE lower(mail) = lower(:email) LIMIT 1"),
        {"email": mgr_email},
    ).fetchone()
    if row and row.display_name:
        return row.display_name

    return None


def _is_same_person(entry: str, email: str, display_name: str) -> bool:
    """Return True if a direct_reports entry refers to the same person.

    Matches against (all case-insensitive):
      1. Email exact match          — entries written by login-time sync going forward
      2. display_name exact match   — entries written by login-time sync (display name form)
      3. Substring match            — legacy CSV entries where AD exported a longer name

    Rule 3 is intentionally conservative: we only remove an entry if our known
    display_name is a substring of it (not the other way around), so a short
    common name like "Raj" never accidentally removes an unrelated "Raj Kumar".
    """
    e = entry.strip().lower()
    if not e:
        return False
    if email and e == email.lower():
        return True
    if display_name:
        dn_lower = display_name.lower()
        if e == dn_lower:
            return True
        # Legacy CSV form: entry is a longer name that contains our display_name
        if dn_lower and dn_lower in e:
            return True
    return False


def _update_manager_direct_reports(
    db, email: str, display_name: str, old_manager_dn: str, new_manager_dn: str
) -> None:
    """Patch direct_reports on the old and new manager's org_tree rows when a
    user's manager changes (promotion / lateral move).

    Remove step: cleans up all legacy forms of the user's name (email, display
    name, or longer AD display name from old CSV loads) via _is_same_person().

    Add step: writes display_name — consistent with the AD/CSV format already
    in direct_reports. Also strips any legacy forms from the new manager's list
    first, so a user who was already listed there under a different name variant
    doesn't end up duplicated.

    Both steps are best-effort: if the manager's org_tree row cannot be found
    the step is skipped silently (DEBUG log only) so a login is never blocked.
    Runs inside the caller's db session; the caller is responsible for commit.
    """
    from sqlalchemy import text

    # ── Remove from old manager ──────────────────────────────────────────────
    if old_manager_dn:
        old_mgr_email = _resolve_manager_email(db, old_manager_dn)
        if old_mgr_email:
            row = db.execute(
                text(
                    "SELECT id, direct_reports FROM org_tree "
                    "WHERE lower(mail) = lower(:mail) LIMIT 1"
                ),
                {"mail": old_mgr_email},
            ).fetchone()
            if row:
                entries = _parse_direct_reports(row.direct_reports)
                cleaned = [e for e in entries if not _is_same_person(e, email, display_name)]
                db.execute(
                    text("UPDATE org_tree SET direct_reports = :dr WHERE id = :id"),
                    {"dr": _format_direct_reports(cleaned), "id": row.id},
                )
                logger.debug(
                    "user_directory_sync: removed %r/%s from old manager (%s) direct_reports",
                    display_name, email, old_mgr_email,
                )
            else:
                logger.debug(
                    "user_directory_sync: old manager %s has no org_tree row — skipping direct_reports remove",
                    old_mgr_email,
                )
        else:
            logger.debug(
                "user_directory_sync: could not resolve old manager DN %r to email — skipping direct_reports remove",
                old_manager_dn,
            )

    # ── Add to new manager ───────────────────────────────────────────────────
    if new_manager_dn:
        new_mgr_email = _resolve_manager_email(db, new_manager_dn)
        if new_mgr_email:
            row = db.execute(
                text(
                    "SELECT id, direct_reports FROM org_tree "
                    "WHERE lower(mail) = lower(:mail) LIMIT 1"
                ),
                {"mail": new_mgr_email},
            ).fetchone()
            if row:
                entries = _parse_direct_reports(row.direct_reports)
                # Remove all legacy forms of this person first (same logic as
                # the remove step) so we never end up with both
                entries = [e for e in entries if not _is_same_person(e, email, display_name)]
                # Add using display_name — consistent with AD/CSV format, no
                # long email strings in the list.
                entries.append(display_name)
                db.execute(
                    text("UPDATE org_tree SET direct_reports = :dr WHERE id = :id"),
                    {"dr": _format_direct_reports(entries), "id": row.id},
                )
                logger.debug(
                    "user_directory_sync: added %r to new manager (%s) direct_reports",
                    display_name, new_mgr_email,
                )
            else:
                logger.debug(
                    "user_directory_sync: new manager %s has no org_tree row — skipping direct_reports add",
                    new_mgr_email,
                )
        else:
            logger.debug(
                "user_directory_sync: could not resolve new manager DN %r to email — skipping direct_reports add",
                new_manager_dn,
            )


def _resolve_manager_node_id(db, manager_dn: str) -> str | None:
    """Resolve a manager DN to their org_tree node_id (used as parent_id on
    the report's row).

    Lookup chain:
      1. DN → email via _resolve_manager_email()
      2. email → org_tree.node_id

    Returns None if the manager has no org_tree row or node_id is NULL.
    """
    from sqlalchemy import text

    mgr_email = _resolve_manager_email(db, manager_dn)
    if not mgr_email:
        return None

    row = db.execute(
        text("SELECT node_id FROM org_tree WHERE lower(mail) = lower(:mail) LIMIT 1"),
        {"mail": mgr_email},
    ).fetchone()

    return row.node_id if row and row.node_id else None


def _upsert_org_tree_row(db, email: str, attrs: dict, fallback_level: int, fallback_name: str) -> None:
    """Best-effort single-row upsert into org_tree for `email`.

    org_tree has no unique constraint on `mail` (only a plain index — the
    live data even has NULL/duplicate mail values from CSV imports), so this
    does an explicit SELECT-then-UPDATE-or-INSERT rather than
    `INSERT ... ON CONFLICT`. `node_id` is left unset on insert since a
    login-time LDAP lookup has no AD objectGUID/node identifier available
    (only the bulk CSV export provides one) — existing consumers
    (hierarchy_table rebuild's org_map CTE) already tolerate NULL node_id
    rows by excluding them from the join.
    """
    from sqlalchemy import text
    from datetime import datetime

    raw_manager_dn  = attrs.get("manager_dn")
    # Resolve raw LDAP DN → display name to stay consistent with how
    # org_tree.manager and users.manager_dn are stored on PROD (display names).
    manager_display = _resolve_manager_display_name(db, raw_manager_dn) if raw_manager_dn else None
    parent_id       = _resolve_manager_node_id(db, raw_manager_dn) if raw_manager_dn else None

    row = db.execute(
        text("SELECT id FROM org_tree WHERE lower(mail) = lower(:email) LIMIT 1"),
        {"email": email},
    ).fetchone()

    now = datetime.utcnow()
    if row:
        db.execute(
            text("""
                UPDATE org_tree
                SET department = :department,
                    manager    = :manager,
                    parent_id  = :parent_id,
                    title      = :title,
                    synced_at  = :now
                WHERE id = :id
            """),
            {
                "department": attrs.get("department"),
                "manager":    manager_display,
                "parent_id":  parent_id,
                "title":      attrs.get("ad_title"),
                "now":        now,
                "id":         row.id,
            },
        )
    else:
        db.execute(
            text("""
                INSERT INTO org_tree
                    (level, display_name, mail, department, manager, parent_id, title, synced_at)
                VALUES
                    (:level, :display_name, :mail, :department, :manager, :parent_id, :title, :now)
            """),
            {
                "level":        fallback_level,
                "display_name": attrs.get("name") or fallback_name or email,
                "mail":         email.lower(),
                "department":   attrs.get("department"),
                "manager":      manager_display,
                "parent_id":    parent_id,
                "title":        attrs.get("ad_title"),
                "now":          now,
            },
        )


def sync_user_from_ldap_and_org_tree(
    user_id: str, email: str, *, ldap_attrs: Optional[dict] = None
) -> bool:
    """Background, best-effort: compare live AD attrs to the current `users`
    row and, if department/manager actually changed, propagate the update
    into users + org_tree and flag hierarchy_table dirty for rebuild.

    `ldap_attrs`: pass the attrs dict already returned by
    auth.ldap_handler.authenticate_user() during this same login so this
    call does NOT issue a second LDAP search for the same user (halving
    AD query volume per login). Only falls back to a fresh
    get_user_attributes() lookup when the caller has no attrs on hand
    (e.g. invoked outside the login path).

    Returns True iff department/manager/ad_title were actually written here.
    Callers that ALSO sync department/manager from a different source (e.g.
    the org_tree snapshot sync in routers/auth_router.py) must check this
    return value and skip re-writing those same fields — this is the
    authoritative, freshest source (live AD, not a nightly CSV snapshot),
    and running both unconditionally in parallel threads/sessions creates a
    write race where whichever commits last silently wins.

    Safe to call unconditionally — no-ops quickly if LDAP is disabled/
    unavailable, and swallows all exceptions so a login can never fail or
    slow down because of this check.
    """
    try:
        from core.config import LDAP_ENABLED

        if not LDAP_ENABLED or not email:
            return False

        if ldap_attrs is not None:
            attrs = ldap_attrs
        else:
            from auth.ldap_handler import get_user_attributes
            attrs = get_user_attributes(email)

        if not attrs:
            return False  # user not found in AD, or LDAP unreachable — leave DB untouched

        new_department = (attrs.get("department") or "").strip()
        raw_manager_dn = (attrs.get("manager_dn") or "").strip()  # full LDAP DN from AD

        from db.database import SessionLocal
        from db.models import User

        db = SessionLocal()
        try:
            u: Optional[User] = db.query(User).filter(User.id == user_id).first()
            if not u:
                return False

            # Resolve the raw AD DN to a display name so it is consistent with
            # how manager_dn is stored across the rest of the platform (display
            # names, not full LDAP DNs). This also ensures change detection
            # below compares apples-to-apples (display name vs display name)
            # rather than always seeing a mismatch (DN vs display name).
            new_manager_dn = (
                _resolve_manager_display_name(db, raw_manager_dn) or raw_manager_dn
            ).strip()

            old_department = (u.department or "").strip()
            old_manager_dn = (u.manager_dn or "").strip()

            if new_department == old_department and new_manager_dn == old_manager_dn:
                return False  # nothing changed — avoid needless writes/dirty-flagging

            from datetime import datetime

            u.department  = attrs.get("department")
            u.manager_dn  = new_manager_dn
            u.ad_title    = attrs.get("ad_title")
            u.ad_username = attrs.get("ad_username") or u.ad_username
            u.ad_dn       = attrs.get("ad_dn") or u.ad_dn
            u.last_ad_sync = datetime.utcnow()

            _upsert_org_tree_row(
                db, email, attrs,
                fallback_level=u.ad_level if u.ad_level is not None else 6,
                fallback_name=u.name,
            )

            db.commit()

            # Patch direct_reports on the old and new manager's org_tree rows
            # in a separate daemon thread so it never adds latency to the
            # login-time commit above. Uses its own DB session — fully
            # independent of the session that just committed.
            if new_manager_dn != old_manager_dn:
                import threading

                _display_name = u.name or email  # capture before session closes

                def _bg_update_direct_reports():
                    try:
                        from db.database import SessionLocal as _SL
                        _db = _SL()
                        try:
                            _update_manager_direct_reports(
                                _db, email, _display_name, old_manager_dn, new_manager_dn
                            )
                            _db.commit()
                        finally:
                            _db.close()
                    except Exception as _exc:
                        logger.debug(
                            "user_directory_sync: direct_reports background update failed (non-fatal): %s",
                            _exc,
                        )

                threading.Thread(target=_bg_update_direct_reports, daemon=True).start()

            # INFO: fact of the change only. Old/new department + manager DN
            # are org-structure data (manager DN in particular reveals the
            # full reporting chain) — kept out of INFO-level logs and only
            # available at DEBUG for anyone who has explicitly turned that
            # up locally for troubleshooting.
            logger.info(
                "user_directory_sync: department/manager changed for user_id=%s "
                "— hierarchy_table flagged dirty",
                user_id,
            )
            logger.debug(
                "user_directory_sync: %s department %r -> %r, manager %r -> %r",
                email, old_department, new_department, old_manager_dn, new_manager_dn,
            )
            _mark_hierarchy_dirty()
            return True
        finally:
            db.close()
    except Exception as exc:
        logger.warning("user_directory_sync: sync failed for user_id=%s (non-fatal): %s", user_id, exc)
        return False
