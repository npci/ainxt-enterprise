# SPDX-License-Identifier: Apache-2.0
# ============================================================
# HOD MONTHLY USAGE DIGEST — HOD-specific layer
#
# This module holds ONLY the HOD-specific pieces:
#
#   * Department roster resolution (_resolve_dept, list_hod_users)
#   * HOD payload builder (_build_hod_context, build_hod_payload)
#   * HOD pipeline wrappers (generate_and_send_hod, generate_and_send_hod_bulk)
#   * Backward-compat render_hod_* wrappers
#
# Everything shared with the Manager digest (LLM inference, rendering, SMTP
# dispatch, archival, bulk loop) lives in services/digest_service.py.
#
# Trigger surface: routers/digest_hod_router.py
# ============================================================

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from core.logger import logger
from db.database import SessionLocal
from db.models import DepartmentHodMapping, User
from services.monthly_statement_service import build_period
from services.digest_service import (
    DIGEST_TYPE_HOD,
    _build_user_blocks,
    generate_and_send_digest,
    generate_and_send_digest_bulk,
    render_digest_html_attachment,
    render_digest_email_body,
    render_digest_email_text,
)


# ============================================================
# PUBLIC: roster resolution (HOD-specific)
# ============================================================

def _resolve_dept(
    db: Session,
    corrected_department_name: str,
) -> Tuple[DepartmentHodMapping, List[User]]:
    """Single round-trip resolver: return (mapping_row, active_users).

    Raises ValueError if the department is not in `department_hod_mapping`.
    """
    mapping = (
        db.query(DepartmentHodMapping)
        .filter(DepartmentHodMapping.corrected_department_name == corrected_department_name)
        .first()
    )
    if mapping is None:
        raise ValueError(
            f"corrected_department_name {corrected_department_name!r} not found "
            "in department_hod_mapping"
        )
    users: List[User] = (
        db.query(User)
        .filter(User.department == mapping.department_name)
        .filter((User.is_active.is_(None)) | (User.is_active.is_(True)))
        .all()
    )
    return mapping, users


def list_hod_users(
    db: Session,
    corrected_department_name: str,
) -> Tuple[str, str, List[User]]:
    """Public surface kept stable per spec §5: returns
    ``(hod_email, department_name, active_users)``."""
    mapping, users = _resolve_dept(db, corrected_department_name)
    return mapping.hod_email, mapping.department_name, users


# ============================================================
# PUBLIC: HOD payload builder
# ============================================================

def build_hod_payload(
    db: Session,
    corrected_department_name: str,
    month: int,
    year:  int,
) -> Dict[str, Any]:
    """Build the public HOD payload (spec §6). See :func:`_build_hod_context`
    when the per-user sub-payloads are also needed (e.g. archive upsert)."""
    payload, _sub_by_uid = _build_hod_context(db, corrected_department_name, month, year)
    return payload


def _build_hod_context(
    db: Session,
    corrected_department_name: str,
    month: int,
    year:  int,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Internal variant of :func:`build_hod_payload` that also returns a
    side-map ``{user_id: full_sub_payload}`` so the archive step can reuse
    the per-user payload without recomputing or leaking it into the public
    HOD payload."""
    mapping, users = _resolve_dept(db, corrected_department_name)
    period = build_period(month, year)
    user_blocks, sub_by_uid, roster_totals = _build_user_blocks(db, users, month, year)

    payload: Dict[str, Any] = {
        "billing_month":        month,
        "billing_year":         year,
        "billing_period_label": period.label,
        "department": {
            "corrected_department_name": mapping.corrected_department_name,
            "department_name":           mapping.department_name,
            "hod_email":                 mapping.hod_email,
            "hod_name":                  mapping.hod_name,
        },
        "roster_totals": roster_totals,
        "users": user_blocks,
        # Caller (generate_and_send_hod) overwrites this with the real
        # LLM or fallback result; we initialise so render templates can
        # safely guard on `inferences.source`.
        "inferences": {
            "source":          "fallback",
            "top_performers":  [],
            "underperformers": [],
            "narrative":       "",
        },
    }
    return payload, sub_by_uid


# ============================================================
# Backward-compatible HOD render wrappers
#
# Existing callers (and tests) may import these names directly. They're
# thin shims over the shared render_digest_* functions.
# ============================================================

def render_hod_html_attachment(payload: Dict[str, Any]) -> str:
    return render_digest_html_attachment(payload, digest_type=DIGEST_TYPE_HOD)


def render_hod_email_body(payload: Dict[str, Any]) -> str:
    return render_digest_email_body(payload, digest_type=DIGEST_TYPE_HOD)


def render_hod_email_text(payload: Dict[str, Any]) -> str:
    return render_digest_email_text(payload, digest_type=DIGEST_TYPE_HOD)


# ============================================================
# PUBLIC: HOD end-to-end pipeline
# ============================================================

def generate_and_send_hod(
    corrected_department_name: str,
    month: int,
    year:  int,
    db:    Optional[Session] = None,
    to_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build HOD payload → delegate to shared pipeline → reshape response.

    ``to_override`` is the real-time test hook: when set (admin-only via the
    router's ``?to=`` query param), the email is dispatched to that address
    instead of ``department.hod_email``. Everything else — roster, LLM
    inference, attachment, archive upsert — runs exactly as in production.
    """
    owns_db = db is None
    db = db or SessionLocal()
    try:
        payload, sub_by_uid = _build_hod_context(
            db, corrected_department_name, month, year,
        )
        users_count = len(payload["users"])
        if users_count == 0:
            raise ValueError(
                f"department {corrected_department_name!r} has no active users"
            )

        dept_name = payload["department"]["department_name"]
        hod_email = payload["department"]["hod_email"]
        subject = (
            f"AiNxt \u2014 HOD Monthly Usage Digest: {dept_name} "
            f"({payload['billing_period_label']})"
        )

        # Resolve the actual recipient. The archive + response still record
        # ``hod_email`` (the real HOD) so the test send is auditable end-to-end.
        recipient = (to_override or "").strip() or hod_email
        if to_override:
            logger.info(
                "hod_statement: trigger=manual TEST recipient_override=%s "
                "real_hod_email=%s dept=%s",
                recipient, hod_email, corrected_department_name,
            )

        result = generate_and_send_digest(
            payload=payload,
            sub_by_uid=sub_by_uid,
            recipient=recipient,
            subject=subject,
            digest_type=DIGEST_TYPE_HOD,
            log_prefix="hod_statement",
            log_context=f"dept={corrected_department_name}",
            month=month,
            year=year,
            model_hint=None,  # uses HOD_STATEMENT_LLM_MODEL
            db=db,
        )

        # Persist archive rows: the shared pipeline doesn't commit when it
        # didn't open the session, so the outer wrapper must commit here.
        if owns_db:
            db.commit()

        # Reshape to preserve the existing HOD API contract.
        return {
            "ok":                        result["ok"],
            "corrected_department_name": corrected_department_name,
            "department_name":           dept_name,
            "hod_email":                 hod_email,
            "period":                    {"month": month, "year": year},
            "users_count":               result["users_count"],
            "sent":                      result["sent"],
            "skipped_reason":            result["skipped_reason"],
            "llm_used":                  result["llm_used"],
            "statement_ids":             result["statement_ids"],
            "recipient_used":            recipient,
            "test_recipient_override":   bool(to_override),
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


def generate_and_send_hod_bulk(
    month: int,
    year:  int,
) -> Dict[str, Any]:
    """Loop distinct ``corrected_department_name`` values; isolate per-dept errors."""
    db = SessionLocal()
    try:
        rows = (
            db.query(DepartmentHodMapping.corrected_department_name)
            .filter(DepartmentHodMapping.corrected_department_name.isnot(None))
            .distinct()
            .all()
        )
        names = [r[0] for r in rows if r[0]]
    finally:
        db.close()

    result = generate_and_send_digest_bulk(
        roster=names,
        send_fn=lambda name, month, year: generate_and_send_hod(
            corrected_department_name=name, month=month, year=year,
        ),
        roster_key_name="corrected_department_name",
        log_prefix="hod_statement",
        skippable_reasons=set(),  # HOD bulk had no skippable reasons originally
        month=month,
        year=year,
    )

    # Reshape to preserve the existing HOD bulk API contract.
    return {
        "ok":                result["ok"],
        "period":            result["period"],
        "total_departments": result["total"],
        "sent":              result["sent"],
        "skipped":           result["skipped"],
        "failed":            result["failed"],
    }
