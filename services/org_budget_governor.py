# SPDX-License-Identifier: MIT
# ============================================================
# ORG BUDGET GOVERNOR — single org-wide monthly cap (flat/admin-only mode)
#
# Used only when HOD_APPROVAL_ENABLED=false (the default). SDLC run budget
# gating and managed-endpoint cloud spend structurally require *some* budget
# ceiling, but flat-mode deployments have no department/HOD hierarchy to hang
# a per-identity cap off. Rather than thread a fake "org-wide sentinel" HOD
# email through hod_allocation_caps/hod_allocation_ledger, this module governs
# ONE global row with NO identity column at all.
#
# Tables (created by db/migrate.py::_part_oss11_org_wide_budget_cap_2026_09_02):
#   ainxt.org_wide_budget_cap    — singleton config row (id=1), admin-managed
#                                   via GET/PUT /budget/admin/org-cap.
#   ainxt.org_wide_budget_ledger — append-only consumption ledger, one row per
#                                   reservation. No identity column: source is
#                                   'sdlc_run' or 'endpoint_spend', with an
#                                   optional run_id/endpoint_id for traceability.
#
# Feature flag:
#   HOD_CAP_ENFORCEMENT_ENABLED=true|false (default: false -> shadow mode).
#   Deliberately the SAME flag hod_budget_governor.py/endpoint_budget_governor.py
#   use — enforcement is a platform-wide posture, not something that should
#   differ between HOD-mode and flat-mode deployments.
#
# All money is Decimal. NUMERIC(12,2) for the cap, NUMERIC(12,6) for ledger
# rows (mirrors hod_budget_governor's precision split). All date arithmetic is
# calendar-month UTC.
# ============================================================

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional, Tuple

from sqlalchemy import text

from core.config import POSTGRES_SCHEMA as _DB_SCHEMA
from core.logger import logger
from db.database import SessionLocal
from services.hod_budget_governor import (
    _current_period,
    _enforcement_enabled,
    _first_of_next_month,
    _money,
)

_VALID_SOURCES = {"sdlc_run", "endpoint_spend"}
_ZERO = Decimal("0.00")
_INFLIGHT_TTL = 300   # seconds — orphaned reservation self-heals in 5 min


@dataclass
class OrgCapStatus:
    cap_usd:       Decimal
    consumed_usd:  Decimal
    remaining_usd: Decimal
    period_yyyymm: str
    resets_on:     date

    def to_dict(self) -> dict:
        return {
            "cap_usd":       float(self.cap_usd),
            "consumed_usd":  float(self.consumed_usd),
            "remaining_usd": float(self.remaining_usd),
            "period_yyyymm": self.period_yyyymm,
            "resets_on":     self.resets_on.isoformat(),
            "enforcement":   _enforcement_enabled(),
        }


def _fetch_cap(db) -> Decimal:
    """Read the singleton cap row. Returns $0.00 if the row is somehow missing
    (migration not yet run) rather than raising — callers treat $0 as "blocked
    until an admin configures a cap", matching HOD-mode's no-cap-row default.
    """
    row = db.execute(
        text(f'SELECT "monthly_cap_usd" FROM {_DB_SCHEMA}.org_wide_budget_cap WHERE id = 1'),
    ).first()
    return _money(row[0]) if row else Decimal("0.00")


def _fetch_consumption(db, period: str) -> Decimal:
    """O(N) sum over the current period's ledger rows. The ledger is small
    (one row per SDLC run / per endpoint-spend event, reset conceptually by
    period, never bulk-deleted here) so a SUM is cheap — no running-total
    column is needed, unlike the per-HOD ledger's MAX(consumed_after_usd)
    trick, which existed there to avoid a full-table scan across many HODs.
    """
    row = db.execute(
        text(
            'SELECT COALESCE(SUM("amount_usd"), 0) '
            f'FROM {_DB_SCHEMA}.org_wide_budget_ledger '
            'WHERE "period_yyyymm" = :p'
        ),
        {"p": period},
    ).scalar()
    return _money(row)


def get_org_cap_status() -> OrgCapStatus:
    """Cheap, side-effect-free summary for the UI banner and gate checks."""
    period = _current_period()
    try:
        db = SessionLocal()
        try:
            cap      = _fetch_cap(db)
            consumed = _fetch_consumption(db, period)
        finally:
            db.close()
    except Exception as exc:
        # Fail-soft for visibility: never break the UI banner because the
        # ledger query failed. Enforcement, when on, fails closed at the
        # call sites (SDLC preflight / endpoint gate), not here.
        logger.warning("get_org_cap_status: query failed: %s", exc)
        cap, consumed = Decimal("0.00"), Decimal("0.00")

    remaining = cap - consumed
    if remaining < 0:
        remaining = Decimal("0.00")

    return OrgCapStatus(
        cap_usd       = cap,
        consumed_usd  = consumed,
        remaining_usd = remaining,
        period_yyyymm = period,
        resets_on     = _first_of_next_month(),
    )


def get_org_cap_config() -> dict:
    """Admin-facing read of the raw cap row (for GET /budget/admin/org-cap)."""
    try:
        db = SessionLocal()
        try:
            row = db.execute(
                text(
                    'SELECT "monthly_cap_usd", "updated_at", "updated_by" '
                    f'FROM {_DB_SCHEMA}.org_wide_budget_cap WHERE id = 1'
                ),
            ).first()
        finally:
            db.close()
    except Exception as exc:
        logger.warning("get_org_cap_config: query failed: %s", exc)
        row = None

    if not row:
        return {"monthly_cap_usd": 0.0, "updated_at": None, "updated_by": None}
    return {
        "monthly_cap_usd": float(row[0] or 0),
        "updated_at": row[1].isoformat() if row[1] else None,
        "updated_by": row[2],
    }


def set_org_cap(monthly_cap_usd, updated_by: Optional[str] = None) -> dict:
    """Admin write (PUT /budget/admin/org-cap). Upserts the singleton row."""
    amount = _money(monthly_cap_usd)
    if amount < 0:
        amount = Decimal("0.00")

    db = SessionLocal()
    try:
        with db.begin():
            db.execute(
                text(
                    f'INSERT INTO {_DB_SCHEMA}.org_wide_budget_cap (id, monthly_cap_usd, updated_at, updated_by) '
                    "VALUES (1, :cap, NOW(), :by) "
                    'ON CONFLICT (id) DO UPDATE SET '
                    '"monthly_cap_usd" = EXCLUDED."monthly_cap_usd", '
                    '"updated_at" = EXCLUDED."updated_at", '
                    '"updated_by" = EXCLUDED."updated_by"'
                ),
                {"cap": amount, "by": (updated_by or None)},
            )
    finally:
        db.close()

    logger.info("org_budget: cap set to %.2f by %s", float(amount), updated_by or "unknown")
    return get_org_cap_config()


def reserve_org_spend(
        source: str,
        amount_usd,
        *,
        run_id: Optional[str] = None,
        endpoint_id: Optional[str] = None,
) -> dict:
    """
    Atomically check + record org-wide spend.

    Behaviour mirrors hod_budget_governor.reserve_and_record(), simplified for
    a single global row (no per-identity cap lock — the ledger INSERT itself,
    combined with reading consumption inside the same transaction, is
    sufficient serialisation for this deployment shape's traffic volume):
      * In **enforcement mode**, raises on cap overrun (see below) — callers
        (SDLC preflight, endpoint gate) are expected to catch and turn this
        into their own user-facing error, matching how they already handle
        hod_budget_governor's HTTPException(409) today.
      * In **shadow mode**, always allows and still records the ledger row, so
        the numbers can be validated before enforcement is switched on.

    Returns: {"cap_usd", "consumed_usd", "remaining_usd", "shadow_mode",
              "would_exceed", "ledger_id"}
    Raises:  ValueError on overrun in enforcement mode (caller maps to their
             own HTTP status — this module has no FastAPI dependency).
    """
    if source not in _VALID_SOURCES:
        raise ValueError(f"Unknown org-budget ledger source: {source!r}")

    amount = _money(amount_usd)
    if amount < 0:
        amount = Decimal("0.00")

    period = _current_period()
    shadow = not _enforcement_enabled()

    db = SessionLocal()
    try:
        with db.begin():
            # Lock the singleton cap row for the duration of the txn so
            # concurrent reservations are serialised org-wide.
            cap_row = db.execute(
                text(f'SELECT "monthly_cap_usd" FROM {_DB_SCHEMA}.org_wide_budget_cap WHERE id = 1 FOR UPDATE'),
            ).first()
            cap = _money(cap_row[0]) if cap_row else Decimal("0.00")

            consumed = _fetch_consumption(db, period)
            projected = consumed + amount
            would_exceed = projected > cap

            if would_exceed and not shadow:
                raise ValueError(
                    f"Org-wide monthly budget cap would be exceeded: "
                    f"${float(consumed):.2f} used + ${float(amount):.2f} requested "
                    f"> ${float(cap):.2f}. Cap resets on "
                    f"{_first_of_next_month().isoformat()}."
                )

            insert_row = db.execute(
                text(
                    f'INSERT INTO {_DB_SCHEMA}.org_wide_budget_ledger '
                    '("id", "period_yyyymm", "source", "amount_usd", "run_id", "endpoint_id", "created_at") '
                    'VALUES (gen_random_uuid(), :p, :src, :amt, :rid, :eid, NOW()) '
                    'RETURNING "id"'
                ),
                {
                    "p": period,
                    "src": source,
                    "amt": amount,
                    "rid": str(run_id) if run_id else None,
                    "eid": str(endpoint_id) if endpoint_id else None,
                },
            )
            ledger_id = insert_row.scalar()
    finally:
        db.close()

    remaining = cap - projected
    if remaining < 0:
        remaining = Decimal("0.00")

    logger.info(
        "org_budget_charge source=%s period=%s amount=%.2f consumed_after=%.2f "
        "cap=%.2f shadow=%s would_exceed=%s run_id=%s endpoint_id=%s ledger_id=%s",
        source, period, float(amount), float(projected), float(cap),
        shadow, would_exceed, run_id, endpoint_id, ledger_id,
    )

    return {
        "cap_usd":       float(cap),
        "consumed_usd":  float(projected),
        "remaining_usd": float(remaining),
        "shadow_mode":   shadow,
        "would_exceed":  would_exceed,
        "ledger_id":     str(ledger_id) if ledger_id else None,
    }


# ── Managed-endpoint cloud spend gate (flat mode) ────────────────────────────
#
# Mirrors services/endpoint_budget_governor.py's check_endpoint_budget() /
# reserve_inflight() / release_inflight(), but against the single org-wide
# cap instead of a per-HOD one. Used by routers/endpoint_proxy_router.py's
# _gate_cloud_request() when HOD_APPROVAL_ENABLED is False — no
# resolve_endpoint_hod() call, no per-endpoint HOD mapping involved.

_org_kv = None


def _get_kv():
    """Cached KV client for in-flight reservations. Returns None when unavailable."""
    global _org_kv
    if _org_kv is None:
        try:
            from core.config import RDB_BUDGET
            from core.kv import get_kv
            c = get_kv(RDB_BUDGET, decode_responses=True)
            c.ping()
            _org_kv = c
        except Exception as exc:
            logger.warning("org_budget: KV unavailable -> %s", exc)
            return None
    return _org_kv


def _inflight_key(period: str) -> str:
    return f"orginflight:{period}"


def get_org_inflight(period: Optional[str] = None) -> Decimal:
    """Currently reserved (in-flight) org-wide spend. Best-effort — 0 if KV is down."""
    kv = _get_kv()
    if not kv:
        return _ZERO
    try:
        raw = kv.get(_inflight_key(period or _current_period()))
        val = Decimal(str(raw)) if raw is not None else _ZERO
        return val if val > 0 else _ZERO
    except Exception:
        return _ZERO


def reserve_org_inflight(amount, period: Optional[str] = None) -> Optional[str]:
    """
    Reserve an estimated amount against the org-wide cap so concurrent cloud
    requests see it as consumed. Returns an opaque token for
    release_org_inflight(), or None if nothing was reserved (KV down /
    non-positive amount).
    """
    amt = _money(amount)
    if amt <= 0:
        return None
    kv = _get_kv()
    if not kv:
        return None

    per = period or _current_period()
    key = _inflight_key(per)
    try:
        kv.incrbyfloat(key, float(amt))
        kv.expire(key, _INFLIGHT_TTL)
        return f"{amt}|{per}|{uuid.uuid4().hex[:8]}"
    except Exception as exc:
        logger.warning("org_budget: reserve_inflight failed: %s", exc)
        return None


def release_org_inflight(token: Optional[str], period: Optional[str] = None) -> None:
    """Release a reservation made by reserve_org_inflight(). Never raises."""
    if not token:
        return
    kv = _get_kv()
    if not kv:
        return
    try:
        parts = token.split("|")
        amt = Decimal(parts[0])
        tok_period = parts[1] if len(parts) >= 3 else None
    except Exception:
        return

    key = _inflight_key(tok_period or period or _current_period())
    try:
        kv.incrbyfloat(key, -float(amt))
    except Exception as exc:
        logger.warning("org_budget: release_inflight failed: %s", exc)


def check_org_endpoint_budget(period: Optional[str] = None) -> Tuple[bool, str, dict]:
    """
    Decide whether a cloud request may proceed against the org-wide cap.

    Returns (allowed, reason, status) — same shape as
    endpoint_budget_governor.check_endpoint_budget(), minus any HOD identity.
    Allows while remaining > 0 (consistent with HOD-mode/SDLC). In-flight
    reservations are included in `consumed`.

    Shadow mode never blocks. Enforcement mode fails CLOSED on lookup error.
    """
    period = period or _current_period()
    shadow = not _enforcement_enabled()

    status_out = {
        "cap_usd":       0.0,
        "consumed_usd":  0.0,
        "remaining_usd": 0.0,
        "period_yyyymm": period,
        "resets_on":     _first_of_next_month().isoformat(),
        "shadow_mode":   shadow,
    }

    try:
        db = SessionLocal()
        try:
            cap      = _fetch_cap(db)
            consumed = _fetch_consumption(db, period)
        finally:
            db.close()
    except Exception as exc:
        logger.error("org_budget: cap lookup failed: %s", exc)
        if shadow:
            return True, "", status_out
        return False, (
            "Budget verification is temporarily unavailable. Cloud models are "
            "unavailable until it recovers."
        ), status_out

    inflight = get_org_inflight(period)
    total_consumed = consumed + inflight
    remaining = cap - total_consumed

    status_out.update({
        "cap_usd":       float(cap),
        "consumed_usd":  float(total_consumed),
        "remaining_usd": float(remaining if remaining > 0 else _ZERO),
    })

    if remaining > 0:
        return True, "", status_out

    msg = (
        f"Org-wide monthly budget exhausted for cloud models: "
        f"${float(total_consumed):.2f} used of ${float(cap):.2f}. "
        f"Resets on {status_out['resets_on']}."
    )
    if shadow:
        logger.warning("org_budget: WOULD BLOCK (shadow mode) %s", msg)
        return True, "", status_out

    logger.info("org_budget: BLOCKED period=%s consumed=%.6f cap=%.2f", period, float(total_consumed), float(cap))
    return False, msg, status_out
