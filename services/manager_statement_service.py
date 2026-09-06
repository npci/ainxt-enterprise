# SPDX-License-Identifier: MIT
# ============================================================
# MANAGER MONTHLY USAGE DIGEST — thin wrapper over shared core
#
# Domain-specific pieces that live here:
#   * Manager eligibility configuration (AD-level cutoff, HOD exclusion)
#   * Roster resolution (_resolve_manager, list_manager_emails) — uses
#     hierarchy_table / ad_level / department_hod_mapping
#   * Payload builder (_build_manager_context) — shapes the manager
#     payload with a department.* alias so shared templates Just Work,
#     plus a native manager.* block for identity-specific branding
#
# Everything else (LLM inference, rendering, SMTP dispatch, archival,
# bulk loop) delegates to the shared functions in
# services.digest_service.
#
# Trigger surface: routers/digest_manager_router.py
# ============================================================

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.logger import logger
from db.database import SessionLocal
from db.models import User
from services.hierarchy_service import get_caller_and_subtree
from services.monthly_statement_service import (
    build_period,
)
from services.digest_service import (
    DIGEST_TYPE_MANAGER,
    _build_user_blocks,
    generate_and_send_digest,
    generate_and_send_digest_bulk,
)


# ── Configuration ─────────────────────────────────────────────────────────
# AD-level eligibility cutoff for who gets the manager digest. Levels are
# inclusive of MIN: a manager with ad_level == MIN qualifies.
#
# AiNxt scheme:
#   ad_level 0–1 = Admin   (exec tier — excluded from this digest)
#   ad_level 2   = HOD     (gets the HOD digest already — excluded)
#   ad_level 3+  = Manager (this digest)
#
# Configurable via env var to allow ops to widen (lower the number) or narrow
# (raise the number) the cohort without code changes.
MANAGER_DIGEST_MIN_AD_LEVEL: int = int(os.getenv("MANAGER_DIGEST_MIN_AD_LEVEL", "3"))

# Optional independent model hint for manager digests; falls back to the HOD
# model when not set so a single env var still controls both pipelines.
# This value is threaded through to _call_llm_for_inferences via its
# model_hint parameter.
MANAGER_STATEMENT_LLM_MODEL: str = (
    os.getenv("MANAGER_STATEMENT_LLM_MODEL")
    or os.getenv("HOD_STATEMENT_LLM_MODEL", "")
).strip()

logger.info(
    "manager_statement: configured min_ad_level=%d llm_model=%r",
    MANAGER_DIGEST_MIN_AD_LEVEL, MANAGER_STATEMENT_LLM_MODEL,
)


# ── Internal helpers ─────────────────────────────────────────────────────

def _is_hod_email(db: Session, email: str) -> bool:
    """Belt-and-braces HOD check sourced directly from department_hod_mapping.

    The AD-level cutoff (default >=3) already excludes HODs at AD-level 2, so
    this is a safety net for orgs where the AD level may lag behind the HOD
    mapping table.
    """
    if not email:
        return False
    row = db.execute(
        text(
            "SELECT 1 FROM ainxt.department_hod_mapping "
            "WHERE lower(hod_email) = lower(:e) LIMIT 1"
        ),
        {"e": email},
    ).first()
    return row is not None


# ============================================================
# PUBLIC: roster resolution
# ============================================================

def list_manager_emails(db: Session) -> List[str]:
    """Distinct lowercase root-manager emails eligible for the digest.

    Joins ``hierarchy_table`` (≥1 report) with ``ainxt.users`` to apply the
    AD-level cutoff. Inactive users are excluded.
    """
    rows = db.execute(
        text(
            """
            SELECT DISTINCT lower(h.root_manager_email) AS email
            FROM   hierarchy_table h
            JOIN   ainxt.users u
                   ON lower(u.email) = lower(h.root_manager_email)
            WHERE  h.root_manager_email IS NOT NULL
              AND  u.ad_level IS NOT NULL
              AND  u.ad_level >= :min_level
              AND  COALESCE(u.is_active, TRUE) = TRUE
            ORDER  BY email
            """
        ),
        {"min_level": MANAGER_DIGEST_MIN_AD_LEVEL},
    ).all()
    return [r[0] for r in rows if r[0]]


def _resolve_manager(
    db: Session, manager_email: str,
) -> Tuple[User, List[User]]:
    """Return ``(manager_user_row, [report_user_rows])``.

    Raises ``ValueError`` for the bulk loop to catch & isolate:

    * ``"manager_not_found"``      — no users row for the email.
    * ``"manager_above_cutoff"``   — ad_level below the configured cutoff
                                     (admin/HOD tier).
    * ``"manager_is_hod"``         — present in department_hod_mapping.
    * ``"manager_has_no_reports"`` — empty subtree.
    """
    email = (manager_email or "").strip().lower()
    if not email:
        raise ValueError("manager_not_found")

    manager = db.query(User).filter(User.email.ilike(email)).first()
    if manager is None:
        raise ValueError("manager_not_found")

    # AD-level cutoff (primary gate; covers HOD at lvl 2 and admins at 0–1).
    if manager.ad_level is None or int(manager.ad_level) < MANAGER_DIGEST_MIN_AD_LEVEL:
        raise ValueError("manager_above_cutoff")

    # Defence-in-depth HOD check (if the AD level lags the mapping table).
    if _is_hod_email(db, email):
        raise ValueError("manager_is_hod")

    # Subtree via the same path /budget/team uses.
    result = get_caller_and_subtree(email, max_rows=1000)
    subtree = result.get("subtree") or []
    if not result.get("has_reports") or not subtree:
        raise ValueError("manager_has_no_reports")

    user_ids = [e["user_id"] for e in subtree if e.get("user_id")]
    if not user_ids:
        raise ValueError("manager_has_no_reports")

    reports: List[User] = (
        db.query(User)
        .filter(User.id.in_(user_ids))
        .filter((User.is_active.is_(None)) | (User.is_active.is_(True)))
        .all()
    )
    if not reports:
        raise ValueError("manager_has_no_reports")

    return manager, reports


# ============================================================
# PUBLIC: payload builder
# ============================================================

def build_manager_payload(
    db: Session, manager_email: str, month: int, year: int,
) -> Dict[str, Any]:
    """Public builder; see :func:`_build_manager_context` if you also need
    the per-user sub-payload map for archival."""
    payload, _sub_by_uid = _build_manager_context(db, manager_email, month, year)
    return payload


def _build_manager_context(
    db: Session, manager_email: str, month: int, year: int,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Shape the manager payload to be Jinja-compatible with the shared
    templates. We populate ``department.*`` as an alias for the manager
    so the unified hod_statement templates render correctly; ``manager.*``
    is the native identity block used by digest_type=="manager" conditionals."""
    manager, reports = _resolve_manager(db, manager_email)
    period = build_period(month, year)
    user_blocks, sub_by_uid, roster_totals = _build_user_blocks(db, reports, month, year)

    manager_name  = (manager.name or "").strip()
    manager_email_clean = (manager.email or "").strip().lower()
    team_label = (
        f"{manager_name}'s team" if manager_name else "Team"
    )

    payload: Dict[str, Any] = {
        "billing_month":        month,
        "billing_year":         year,
        "billing_period_label": period.label,
        # Department-shaped alias so the shared Jinja templates render correctly.
        "department": {
            "corrected_department_name": team_label,
            "department_name":           team_label,
            "hod_email":                 manager_email_clean,
            "hod_name":                  manager_name,
        },
        # Native manager block for digest_type=="manager" template conditionals.
        "manager": {
            "user_id":    str(manager.id),
            "name":       manager_name,
            "email":      manager_email_clean,
            "title":      manager.ad_title or "",
            "department": manager.department or "",
            "ad_level":   manager.ad_level,
        },
        "roster_totals": roster_totals,
        "users": user_blocks,
        # Overwritten by the shared pipeline's LLM or fallback step.
        "inferences": {
            "source":          "fallback",
            "top_performers":  [],
            "underperformers": [],
            "narrative":       "",
        },
    }
    return payload, sub_by_uid


# ============================================================
# PUBLIC: end-to-end pipeline (thin wrapper)
# ============================================================

def generate_and_send_manager(
    manager_email: str,
    month: int,
    year:  int,
    db:    Optional[Session] = None,
    to_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Build manager payload → delegate to shared pipeline → reshape response.

    ``to_override`` is the real-time test hook (admin-only via the router's
    ``?to=`` query param): when set, the email is dispatched to that address
    instead of the manager's own address. Roster, LLM inference, attachment,
    and archive upsert all run exactly as in production.
    """
    owns_db = db is None
    db = db or SessionLocal()
    try:
        payload, sub_by_uid = _build_manager_context(
            db, manager_email, month, year,
        )
        users_count = len(payload["users"])
        if users_count == 0:
            raise ValueError("manager_has_no_reports")

        team_label = payload["department"]["department_name"]
        subject = (
            f"AiNxt \u2014 Team Monthly Usage Digest: {team_label} "
            f"({payload['billing_period_label']})"
        )

        real_manager_email = payload["manager"]["email"]
        recipient = (to_override or "").strip() or real_manager_email
        if to_override:
            logger.info(
                "manager_statement: trigger=manual TEST recipient_override=%s "
                "real_manager_email=%s",
                recipient, real_manager_email,
            )

        result = generate_and_send_digest(
            payload=payload,
            sub_by_uid=sub_by_uid,
            recipient=recipient,
            subject=subject,
            digest_type=DIGEST_TYPE_MANAGER,
            log_prefix="manager_statement",
            log_context=f"manager={manager_email}",
            month=month,
            year=year,
            model_hint=MANAGER_STATEMENT_LLM_MODEL or None,
            db=db,
        )

        # Persist archive rows: the shared pipeline doesn't commit when it
        # didn't open the session, so the outer wrapper must commit here.
        if owns_db:
            db.commit()

        # Reshape to preserve the existing Manager API contract.
        return {
            "ok":             result["ok"],
            "manager_email":  real_manager_email,
            "manager_name":   payload["manager"]["name"],
            "period":         {"month": month, "year": year},
            "users_count":    result["users_count"],
            "sent":           result["sent"],
            "skipped_reason": result["skipped_reason"],
            "llm_used":       result["llm_used"],
            "statement_ids":  result["statement_ids"],
            "recipient_used":          recipient,
            "test_recipient_override": bool(to_override),
        }
    except Exception:
        if owns_db:
            try:
                db.rollback()
            except Exception:
                pass
        raise
    finally:
        if owns_db:
            db.close()


# ============================================================
# BULK (thin wrapper)
# ============================================================

_SKIPPABLE_REASONS = {
    "manager_not_found",
    "manager_above_cutoff",
    "manager_is_hod",
    "manager_has_no_reports",
}


def generate_and_send_manager_bulk(
    month: int,
    year:  int,
) -> Dict[str, Any]:
    """Loop every eligible reporting-manager email; isolate per-manager errors."""
    db = SessionLocal()
    try:
        emails = list_manager_emails(db)
    finally:
        db.close()

    result = generate_and_send_digest_bulk(
        roster=emails,
        send_fn=lambda email, month, year: generate_and_send_manager(
            manager_email=email, month=month, year=year,
        ),
        roster_key_name="manager_email",
        log_prefix="manager_statement",
        skippable_reasons=_SKIPPABLE_REASONS,
        month=month,
        year=year,
    )

    # Reshape to preserve the existing Manager bulk API contract.
    return {
        "ok":              result["ok"],
        "period":          result["period"],
        "total_managers":  result["total"],
        "sent":            result["sent"],
        "skipped":         result["skipped"],
        "skipped_reasons": result["skipped_reasons"],
        "failed":          result["failed"],
    }
