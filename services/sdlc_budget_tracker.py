# SPDX-License-Identifier: Apache-2.0
# ============================================================
# SDLC HOD Budget Tracker
#
# Bridges SDLC pipeline cost accumulation → HOD allocation cap governance.
#
# Responsibilities:
#   * Preflight: look up the triggering user's department → HOD email,
#     check HOD cap remaining before any LLM work starts.
#   * Per-call: atomically increment total_cost_usd / total_input_tokens /
#     total_output_tokens on sdlc_runs after each _llm() / _llm_cached() call.
#   * Run-end: write one consolidated ledger entry (action='sdlc_run') against
#     the HOD cap and stamp hod_ledger_id back onto sdlc_runs.
#
# Design notes:
#   * Per-call DB increments (COALESCE(col,0)+delta) survive across multiple
#     RQ job segments (design/coding/pr-review) on the same run_id.
#   * finalize_run_budget() is idempotent via hod_ledger_id already-set guard.
#   * All failures are logged and swallowed — never terminates an LLM call.
#   * No circular imports: callers pass run_id explicitly; this module never
#     imports from agents.sdlc_pipeline at module level.
# ============================================================

from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

from core.logger import logger
from db.database import SessionLocal
from sqlalchemy import text


# ── Internal helpers ─────────────────────────────────────────────────────────

_NIL_UUID = "00000000-0000-0000-0000-000000000000"


def _is_uuid(val) -> bool:
    """True when val is a syntactically valid UUID string."""
    if not val:
        return False
    import uuid as _uuid
    try:
        _uuid.UUID(str(val))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _get_user_department(user_id: str) -> Optional[str]:
    """Read department from users table for the given user UUID."""
    if not user_id:
        return None
    db = SessionLocal()
    try:
        row = db.execute(
            text('SELECT "department" FROM users WHERE "id" = :uid LIMIT 1'),
            {"uid": user_id},
        ).first()
        return (row[0] or "").strip() or None
    except Exception as exc:
        logger.warning("sdlc_budget: _get_user_department failed for %r: %s", user_id, exc)
        return None
    finally:
        db.close()


def _resolve_hod_email(department: str) -> Optional[str]:
    """Look up HOD email from ainxt.department_hod_mapping (case-insensitive dept match)."""
    if not department:
        return None
    db = SessionLocal()
    try:
        row = db.execute(
            text(
                'SELECT "hod_email" '
                'FROM ainxt.department_hod_mapping '
                'WHERE lower("department_name") = lower(:dept) '
                'LIMIT 1'
            ),
            {"dept": department},
        ).first()
        return (row[0] or "").strip() or None
    except Exception as exc:
        logger.warning("sdlc_budget: _resolve_hod_email failed for dept=%r: %s", department, exc)
        return None
    finally:
        db.close()


def _default_hod_email() -> Optional[str]:
    """Fallback HOD email for runs whose triggering user has no department or whose
    department has no HOD mapping.

    Set ``SDLC_DEFAULT_HOD_EMAIL`` to attribute such runs (e.g. admin-triggered or
    service-triggered runs) to a catch-all HOD so budget still finalizes instead of
    logging "finalize skip — no hod_email". Unset (the default) preserves the strict
    behavior: no department/HOD → no attribution.
    """
    return (os.getenv("SDLC_DEFAULT_HOD_EMAIL", "") or "").strip().lower() or None


def _write_hod_email(run_id: str, hod_email: str) -> None:
    """Persist the resolved HOD email onto sdlc_runs.hod_email (once at preflight)."""
    if not run_id or not hod_email:
        return
    db = SessionLocal()
    try:
        db.execute(
            text('UPDATE sdlc_runs SET hod_email = :e WHERE id = :id'),
            {"e": hod_email.lower(), "id": run_id},
        )
        db.commit()
    except Exception as exc:
        logger.warning("sdlc_budget: _write_hod_email failed run=%s: %s", run_id, exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


# ── Public API ───────────────────────────────────────────────────────────────

def check_hod_budget(user_id: str, run_id: str, user_email: str = "") -> tuple[bool, str]:
    """
    Preflight HOD budget check. Called from _preflight_check() before any LLM work.

    Steps:
      1. Look up user.department for user_id.
      2. Map department → HOD email via ainxt.department_hod_mapping.
      3. Check HOD monthly cap remaining via get_cap_status().
      4. On pass: write hod_email to sdlc_runs.hod_email for run-end deduction.

    Returns (True, "") when the run may proceed.
    Returns (False, error_msg) when the run must be blocked.

    When HOD_CAP_ENFORCEMENT_ENABLED=false the check is advisory only:
    missing dept/HOD mapping and exhausted cap all log warnings but return True.
    """
    from services.hod_budget_governor import get_cap_status, _enforcement_enabled

    enforce = _enforcement_enabled()
    who = user_email or user_id or "unknown"

    _default_hod = _default_hod_email()

    # ── 1. Department ─────────────────────────────────────────────────────────
    department = _get_user_department(user_id)
    if not department:
        if _default_hod:
            logger.info(
                "sdlc_budget: no department for user %r — attributing to default HOD %s",
                who, _default_hod,
            )
            department = None  # skip mapping lookup; use the default HOD directly
        else:
            msg = (
                f"SDLC budget pre-check FAILED: no department configured for user {who!r}. "
                "Every AiNxt employee must belong to a department. "
                "Contact your administrator to set your department in the org tree "
                "(POST /admin/sync/org-tree), or set SDLC_DEFAULT_HOD_EMAIL."
            )
            if enforce:
                return False, msg
            logger.warning("sdlc_budget: %s (enforcement off — allowing run)", msg)
            return True, ""

    # ── 2. HOD email ──────────────────────────────────────────────────────────
    hod_email = _resolve_hod_email(department) if department else None
    if not hod_email:
        hod_email = _default_hod
    if not hod_email:
        msg = (
            f"SDLC budget pre-check FAILED: department {department!r} (user: {who!r}) "
            "has no HOD mapping in ainxt.department_hod_mapping. "
            "Contact your administrator to configure the department-HOD mapping, "
            "or set SDLC_DEFAULT_HOD_EMAIL."
        )
        if enforce:
            return False, msg
        logger.warning("sdlc_budget: %s (enforcement off — allowing run)", msg)
        return True, ""

    # ── 3. Cap check ──────────────────────────────────────────────────────────
    try:
        status = get_cap_status(hod_email)
    except Exception as exc:
        logger.warning(
            "sdlc_budget: get_cap_status failed for hod=%s (allowing run): %s", hod_email, exc
        )
        return True, ""

    if enforce and status.remaining_usd <= Decimal("0.00"):
        return False, (
            f"SDLC run blocked: HOD monthly budget exhausted for department {department!r}. "
            f"HOD: {hod_email} | Used: ${float(status.consumed_usd):.2f} of "
            f"${float(status.cap_usd):.2f} for period {status.period_yyyymm}. "
            f"Budget resets on {status.resets_on.isoformat()}. "
            "Contact your HOD to request an additional allocation via the Budget portal."
        )

    # ── 4. Persist hod_email on the run row (used at run-end for deduction) ───
    _write_hod_email(run_id, hod_email)

    logger.info(
        "sdlc_budget: preflight OK dept=%r hod=%s remaining=$%.2f enforce=%s run=%s",
        department, hod_email, float(status.remaining_usd), enforce, run_id,
    )
    return True, ""


def record_llm_cost(tokens_in: int, tokens_out: int, cost_usd: float, run_id: str) -> None:
    """
    Atomically increment the running cost/token counters on sdlc_runs.

    Called after every _llm() call in the SDLC pipeline.
    run_id must be supplied by the caller (pass _cv_run_id.get() from sdlc_pipeline).
    No-op when run_id is empty or all values are zero.
    """
    if not run_id:
        return
    if not tokens_in and not tokens_out and not cost_usd:
        return
    db = SessionLocal()
    try:
        db.execute(
            text(
                "UPDATE sdlc_runs "
                "SET total_input_tokens  = COALESCE(total_input_tokens,  0) + :ti, "
                "    total_output_tokens = COALESCE(total_output_tokens, 0) + :to, "
                "    total_cost_usd      = COALESCE(total_cost_usd,      0) + :c "
                "WHERE id = :id"
            ),
            {"ti": int(tokens_in), "to": int(tokens_out), "c": float(cost_usd), "id": run_id},
        )
        db.commit()
    except Exception as exc:
        logger.warning(
            "sdlc_budget: record_llm_cost failed run=%s tokens_in=%s tokens_out=%s cost=%.6f: %s",
            run_id, tokens_in, tokens_out, cost_usd, exc,
        )
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def finalize_run_budget(run_id: str) -> None:
    """
    Write the consolidated HOD ledger entry for a completed or failed SDLC run.

    Called from update_run_state() when to_state ∈ {COMPLETE, FAILED}.

    Flow:
      1. Read total_cost_usd + hod_email from sdlc_runs.
      2. Guard: skip if hod_ledger_id already set (idempotency).
      3. Guard: skip if hod_email is not set (preflight failed / enforcement off).
      4. Call reserve_and_record(action='sdlc_run') — atomic HOD cap deduction.
      5. Stamp the returned ledger_id onto sdlc_runs.hod_ledger_id.

    All exceptions are caught and logged. A billing failure NEVER rolls back a
    state transition — the run's final state is authoritative.
    """
    if not run_id:
        return

    db = SessionLocal()
    try:
        row = db.execute(
            text(
                "SELECT hod_email, total_cost_usd, total_input_tokens, "
                "       total_output_tokens, hod_ledger_id, created_by "
                "FROM sdlc_runs WHERE id = :id"
            ),
            {"id": run_id},
        ).first()
    except Exception as exc:
        logger.warning("sdlc_budget: finalize_run_budget read failed run=%s: %s", run_id, exc)
        db.close()
        return
    finally:
        db.close()

    if not row:
        return

    hod_email, total_cost, tokens_in, tokens_out, existing_ledger_id, created_by = row

    # Idempotency: don't write twice (e.g. double-FAILED from two concurrent workers)
    if existing_ledger_id:
        logger.info(
            "sdlc_budget: finalize skip — hod_ledger_id already set run=%s", run_id
        )
        return

    if not hod_email:
        logger.info(
            "sdlc_budget: finalize skip — no hod_email on run=%s "
            "(dept/HOD not mapped, or enforcement was off at preflight)", run_id
        )
        return

    cost_usd = float(total_cost or 0)

    # The ledger's target_user_id column is UUID NOT NULL. created_by is not always
    # a UUID — some runs store the triggering user's email, and service-triggered
    # runs may have it empty. Coerce to a valid UUID (or the nil UUID sentinel) so
    # the ledger insert never fails on a bad cast.
    target_uid = created_by if _is_uuid(created_by) else _NIL_UUID

    try:
        from services.hod_budget_governor import reserve_and_record
        result = reserve_and_record(
            hod_email=hod_email,
            target_user_id=target_uid,
            target_user_email=(created_by if (created_by and "@" in created_by) else None),
            action="sdlc_run",
            amount_usd=cost_usd,
            request_id=run_id,
        )
        ledger_id = result.get("ledger_id")
    except Exception as exc:
        logger.error(
            "sdlc_budget: reserve_and_record failed run=%s hod=%s cost=%.4f: %s",
            run_id, hod_email, cost_usd, exc,
        )
        return

    # Stamp ledger_id back onto sdlc_runs for traceability
    if ledger_id:
        db2 = SessionLocal()
        try:
            db2.execute(
                text("UPDATE sdlc_runs SET hod_ledger_id = :lid WHERE id = :id"),
                {"lid": ledger_id, "id": run_id},
            )
            db2.commit()
        except Exception as exc:
            logger.warning(
                "sdlc_budget: hod_ledger_id stamp failed run=%s: %s", run_id, exc
            )
            try:
                db2.rollback()
            except Exception:
                pass
        finally:
            db2.close()

    logger.info(
        "sdlc_budget: finalize OK run=%s hod=%s tokens_in=%s tokens_out=%s "
        "cost=$%.4f ledger_id=%s",
        run_id, hod_email, tokens_in, tokens_out, cost_usd, ledger_id,
    )
