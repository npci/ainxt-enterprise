#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AD SYNC WORKER — nightly LDAP → Postgres user mirror
#
# Runs nightly at 2 AM IST (configured via LDAP_SYNC_HOUR).
# Can also be triggered manually: python workers/ad_sync.py
#
# What it does:
#   1. Fetches all users from AD via auth/ldap_handler.sync_all_users()
#   2. Resolves band/band_level from AD 'title' via title_band_map ILIKE
#   3. Upserts users table (email as key — never deletes, only deactivates)
#   4. Rebuilds user_hierarchy (manager chain, 4 levels deep)
#   5. Auto-assigns product_members from manager chain where product owners
#      are in the manager chain (C1+ auto-membership rule)
# ============================================================

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# CKMS — decrypt LDAP_BIND_PASSWORD and friends before core.config is imported.
from core.ckms import load_at_boot as _ckms_load_at_boot
_ckms_load_at_boot()

from core.logger import logger, mask_email
from core.config import LDAP_ENABLED, SECURITY_AD_GROUP, APPROVER_AD_GROUP


def run_ad_sync() -> dict:
    """
    Run a full AD sync cycle.
    Returns a summary dict: {synced, created, updated, band_misses, errors}
    """
    if not LDAP_ENABLED:
        logger.info("AD sync: LDAP_ENABLED=false — skipping")
        return {"skipped": True, "reason": "LDAP_ENABLED=false"}

    from auth.ldap_handler import sync_all_users, resolve_band, build_manager_chain
    from db.database import engine
    from sqlalchemy import text

    summary = {"synced": 0, "created": 0, "updated": 0, "band_misses": 0, "errors": 0}

    logger.info("AD sync: starting full user sync from LDAP")
    users = sync_all_users()
    logger.info(f"AD sync: fetched {len(users)} users from AD")

    for ad_user in users:
        email = (ad_user.get("email") or "").strip().lower()
        if not email:
            continue

        ad_title = ad_user.get("ad_title") or ""
        band, band_level = resolve_band(ad_title)
        if not ad_title or band == "A1":
            summary["band_misses"] += 1

        # Derive IS team and approver flags from AD memberOf groups
        member_of = ad_user.get("member_of", [])
        is_security_team = any(
            SECURITY_AD_GROUP.lower() in g.lower() for g in member_of
        )
        is_approver = any(
            APPROVER_AD_GROUP.lower() in g.lower() for g in member_of
        )

        try:
            with engine.connect() as conn:
                # Check if user exists
                existing = conn.execute(
                    text("SELECT id, band, band_level FROM users WHERE email = :email"),
                    {"email": email},
                ).fetchone()

                now = datetime.now(timezone.utc)

                if existing:
                    conn.execute(
                        text("""
                            UPDATE users SET
                                name             = :name,
                                ad_username      = :ad_username,
                                employee_id      = COALESCE(:employee_id, employee_id),
                                ad_dn            = :ad_dn,
                                ad_title         = :ad_title,
                                department       = :department,
                                manager_dn       = :manager_dn,
                                band             = :band,
                                band_level       = :band_level,
                                is_security_team = :is_security_team,
                                is_approver      = :is_approver,
                                last_ad_sync     = :now,
                                account_status   = 'active'
                            WHERE email = :email
                        """),
                        {
                            "email":            email,
                            "name":             ad_user.get("name") or email.split("@")[0],
                            "ad_username":      ad_user.get("ad_username"),
                            "employee_id":      ad_user.get("employee_id"),
                            "ad_dn":            ad_user.get("ad_dn"),
                            "ad_title":         ad_title,
                            "department":       ad_user.get("department"),
                            "manager_dn":       ad_user.get("manager_dn"),
                            "band":             band,
                            "band_level":       band_level,
                            "is_security_team": is_security_team,
                            "is_approver":      is_approver,
                            "now":              now,
                        },
                    )
                    summary["updated"] += 1
                else:
                    conn.execute(
                        text("""
                            INSERT INTO users
                                (email, name, role, ad_username, employee_id, ad_dn, ad_title,
                                 department, manager_dn, band, band_level,
                                 is_security_team, is_approver,
                                 is_active, account_status, last_ad_sync, created_at)
                            VALUES
                                (:email, :name, 'developer', :ad_username, :employee_id, :ad_dn,
                                 :ad_title, :department, :manager_dn, :band, :band_level,
                                 :is_security_team, :is_approver,
                                 TRUE, 'active', :now, :now)
                            ON CONFLICT (email) DO NOTHING
                        """),
                        {
                            "email":            email,
                            "name":             ad_user.get("name") or email.split("@")[0],
                            "ad_username":      ad_user.get("ad_username"),
                            "employee_id":      ad_user.get("employee_id"),
                            "ad_dn":            ad_user.get("ad_dn"),
                            "ad_title":         ad_title,
                            "department":       ad_user.get("department"),
                            "manager_dn":       ad_user.get("manager_dn"),
                            "band":             band,
                            "band_level":       band_level,
                            "is_security_team": is_security_team,
                            "is_approver":      is_approver,
                            "now":              now,
                        },
                    )
                    summary["created"] += 1

                conn.commit()

            # Rebuild hierarchy cache
            user_dn = ad_user.get("ad_dn")
            if user_dn:
                manager_ids = build_manager_chain(user_dn, depth=4)
                with engine.connect() as conn:
                    conn.execute(
                        text("""
                            INSERT INTO user_hierarchy (user_id, manager_ids, last_synced_at)
                            VALUES (:uid, :managers::jsonb, :now)
                            ON CONFLICT (user_id) DO UPDATE
                                SET manager_ids   = :managers::jsonb,
                                    last_synced_at = :now
                        """),
                        {
                            "uid":      email,
                            "managers": str(manager_ids).replace("'", '"'),
                            "now":      datetime.now(timezone.utc),
                        },
                    )
                    conn.commit()

            summary["synced"] += 1

        except Exception as exc:
            logger.error(f"AD sync: failed for {mask_email(email)}: {exc}")
            summary["errors"] += 1

    # Auto-membership: assign product_members for users whose manager chain
    # contains a product owner (4-level deep, source='ad_sync')
    _auto_assign_product_members()

    logger.info(
        f"AD sync complete: synced={summary['synced']} created={summary['created']} "
        f"updated={summary['updated']} band_misses={summary['band_misses']} "
        f"errors={summary['errors']}"
    )
    return summary


def _auto_assign_product_members():
    """
    For each product, find users whose manager chain (L1-L4) contains a
    product owner.  Upsert them as product_members with source='ad_sync'.
    This implements the 'AD reporting chain auto-membership' rule.
    """
    from db.database import engine
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            # Get all product owners
            owners = conn.execute(
                text("SELECT product_id, user_id FROM product_owners")
            ).fetchall()

            for product_id, owner_id in owners:
                # Find users who have this owner in their manager chain
                members = conn.execute(
                    text("""
                        SELECT user_id FROM user_hierarchy
                        WHERE manager_ids @> :owner_json::jsonb
                    """),
                    {"owner_json": f'["{owner_id}"]'},
                ).fetchall()

                for (member_id,) in members:
                    conn.execute(
                        text("""
                            INSERT INTO product_members (product_id, user_id, source, added_at)
                            VALUES (:pid, :uid, 'ad_sync', NOW())
                            ON CONFLICT (product_id, user_id) DO NOTHING
                        """),
                        {"pid": str(product_id), "uid": member_id},
                    )

            conn.commit()
            logger.info("AD sync: product auto-membership updated")

    except Exception as exc:
        logger.warning(f"AD sync: auto-membership update failed: {exc}")


if __name__ == "__main__":
    result = run_ad_sync()
    print(result)
