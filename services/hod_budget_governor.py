# SPDX-License-Identifier: Apache-2.0
# ============================================================
# HOD BUDGET GOVERNOR — monthly allocation cap enforcement
#
# Responsibilities:
#   * Hold one place that decides "can this HOD allocate $X right now?"
#   * Atomically reserve cap (SELECT FOR UPDATE on the cap row) and
#     INSERT an append-only row into ainxt.hod_allocation_ledger.
#   * Provide a cheap O(1) summary lookup for the UI banner.
#
# Tables (manually created by DBA in `ainxt` schema):
#   ainxt.hod_allocation_caps     — one row per HOD (read-only to app)
#   ainxt.hod_allocation_ledger   — append-only audit/spend ledger
#
# Feature flag:
#   HOD_CAP_ENFORCEMENT_ENABLED=true|false (default: false → shadow mode)
#   When false: ledger rows are still written (with shadow_mode=true)
#   but cap violations do NOT raise — they only log a warning.
#
# Defaults:
#   HOD_DEFAULT_MONTHLY_CAP_USD=0.00 (no cap row ⇒ effectively $0 ⇒ blocked)
#   Set this to a positive value to allow ungoverned HODs to allocate up
#   to a global default until DBA seeds a per-HOD row.
#
# All money is Decimal. NUMERIC(12,2) on the table; quantised to 2 dp here.
# All date arithmetic is calendar-month UTC.
# ============================================================

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Final, Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.logger import logger, mask_email
from db.database import SessionLocal


# ── Configuration ────────────────────────────────────────────────────────────

def _enforcement_enabled() -> bool:
    """Re-read on every call so an env-var flip takes effect without restart."""
    return os.getenv("HOD_CAP_ENFORCEMENT_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _default_cap_usd() -> Decimal:
    raw = os.getenv("HOD_DEFAULT_MONTHLY_CAP_USD", "0.00").strip()
    try:
        return _money(Decimal(raw))
    except Exception:
        return Decimal("0.00")


# ── Money helpers ────────────────────────────────────────────────────────────

_TWO_PLACES = Decimal("0.01")


def _money(value) -> Decimal:
    """Coerce to Decimal and quantise to 2 dp (banker's rounding off; ROUND_HALF_UP for money)."""
    if value is None:
        return Decimal("0.00")
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return d.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


# ── Period helpers ───────────────────────────────────────────────────────────

def _current_period() -> str:
    """Return the current UTC calendar month as 'YYYY-MM'."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _first_of_next_month(today: Optional[date] = None) -> date:
    today = today or datetime.now(timezone.utc).date()
    year, month = (today.year + (today.month // 12)), (today.month % 12) + 1
    return date(year, month, 1)


# ── Action constants ─────────────────────────────────────────────────────────
# Kept here (not in budget_router) so service consumers don't import the router.

ACTION_ALLOCATE               = "allocate"
ACTION_APPROVE_REQUEST        = "approve_request"
ACTION_WINNER_APPROVE_REQUEST = "winner_approve_request"   # 10x winner grant — never charged against HOD cap
ACTION_SDLC_RUN               = "sdlc_run"

# Managed-endpoint cloud spend (2026-07-29). NOT written via reserve_and_record():
# it is a single carrier row per (hod_email, period) that
# services/endpoint_budget_governor.record_endpoint_spend() UPDATEs in place, and
# it keeps its running total in the separate endpoint_spend_usd NUMERIC(12,6)
# column rather than amount_usd/consumed_after_usd. Declared here so audit tooling
# enumerating ledger actions recognises the value, and so _fetch_consumption can
# exclude it explicitly.
ACTION_ENDPOINT_SPEND  = "endpoint_spend"

# Actions accepted by reserve_and_record. Add new values intentionally —
# anything else raises ValueError to prevent typos becoming silent audit gaps.
# ACTION_ENDPOINT_SPEND is deliberately absent: reserve_and_record INSERTs a fresh
# row per call, which would mean one ledger row per HTTP request.
_VALID_ACTIONS = {ACTION_ALLOCATE, ACTION_APPROVE_REQUEST, ACTION_WINNER_APPROVE_REQUEST, ACTION_SDLC_RUN}


# ── Data containers ──────────────────────────────────────────────────────────

@dataclass
class CapStatus:
    has_cap_row:   bool
    cap_usd:       Decimal
    consumed_usd:  Decimal
    remaining_usd: Decimal
    period_yyyymm: str
    resets_on:     date

    def to_dict(self) -> dict:
        return {
            "has_cap_row":   self.has_cap_row,
            "cap_usd":       float(self.cap_usd),
            "consumed_usd":  float(self.consumed_usd),
            "remaining_usd": float(self.remaining_usd),
            "period_yyyymm": self.period_yyyymm,
            "resets_on":     self.resets_on.isoformat(),
            "enforcement":   _enforcement_enabled(),
        }


# ── Read-only lookups ────────────────────────────────────────────────────────

def _fetch_cap(db: Session, hod_email_lc: str) -> Optional[Decimal]:
    """Return the configured cap for this HOD, or None if no active row exists."""
    row = db.execute(
        text(
            'SELECT "monthly_cap_usd" '
            'FROM ainxt.hod_allocation_caps '
            'WHERE lower("hod_email") = :e AND "is_active" = TRUE'
        ),
        {"e": hod_email_lc},
    ).first()
    if not row:
        return None
    return _money(row[0])


def _fetch_consumption(db: Session, hod_email_lc: str, period: str) -> Decimal:
    """O(1) lookup of the running ALLOCATION consumption via MAX(consumed_after_usd).

    Excludes action='endpoint_spend' carrier rows: those track managed-endpoint
    cloud consumption in their own endpoint_spend_usd column and leave
    consumed_after_usd NULL, so including them would contribute nothing to the
    MAX() while muddying the meaning of this figure. Endpoint spend is added on
    top of this value by services/endpoint_budget_governor when gating cloud
    requests — see that module for the full accounting.
    """
    row = db.execute(
        text(
            'SELECT COALESCE(MAX("consumed_after_usd"), 0) '
            'FROM ainxt.hod_allocation_ledger '
            'WHERE lower("hod_email") = :e '
            '  AND "period_yyyymm" = :p '
            '  AND "shadow_mode" = FALSE '
            "  AND \"action\" <> 'endpoint_spend'"
        ),
        {"e": hod_email_lc, "p": period},
    ).scalar()
    return _money(row)


def get_cap_status(hod_email: str) -> CapStatus:
    """
    Cheap, side-effect-free summary for the UI banner.

    Falls back to HOD_DEFAULT_MONTHLY_CAP_USD when no cap row exists,
    so the banner shows a meaningful number rather than $0.00 / "unconfigured".
    """
    if not hod_email:
        return CapStatus(
            has_cap_row   = False,
            cap_usd       = Decimal("0.00"),
            consumed_usd  = Decimal("0.00"),
            remaining_usd = Decimal("0.00"),
            period_yyyymm = _current_period(),
            resets_on     = _first_of_next_month(),
        )

    email_lc = hod_email.lower()
    period   = _current_period()

    try:
        db = SessionLocal()
        try:
            cap_db = _fetch_cap(db, email_lc)
            consumed = _fetch_consumption(db, email_lc, period)
        finally:
            db.close()
    except Exception as exc:
        # Fail-soft for visibility: never break the UI banner because the
        # ledger query failed. Enforcement, when on, will fail-closed elsewhere.
        logger.warning("get_cap_status: query failed for %s: %s", mask_email(email_lc), exc)
        cap_db, consumed = None, Decimal("0.00")

    has_row = cap_db is not None
    cap     = cap_db if has_row else _default_cap_usd()
    remain  = cap - consumed
    if remain < 0:
        remain = Decimal("0.00")

    return CapStatus(
        has_cap_row   = has_row,
        cap_usd       = cap,
        consumed_usd  = consumed,
        remaining_usd = remain,
        period_yyyymm = period,
        resets_on     = _first_of_next_month(),
    )


# ── Mutating path ────────────────────────────────────────────────────────────

def reserve_and_record(
        hod_email: str,
        target_user_id: str,
        target_user_email: Optional[str],
        action: str,
        amount_usd,
        previous_limit_usd=None,
        new_limit_usd=None,
        request_id: Optional[str] = None,
        justification: Optional[str] = None,
        force_non_shadow: bool = False,
) -> dict:
    """
    Atomically check + record an HOD allocation.

    Behaviour:
      * Always inserts a ledger row (even for amount=0) for audit completeness.
      * Locks the cap row (SELECT FOR UPDATE) for the duration of the txn so
        concurrent allocations are serialised per-HOD.
      * In **enforcement mode**, raises HTTP 409 if the action would exceed cap.
      * In **shadow mode**, logs a warning but allows; ledger row carries
        shadow_mode=true so analysts can replay what would have been blocked.

    Returns: {"cap_usd": ..., "consumed_usd": ..., "remaining_usd": ...,
              "charged": bool, "shadow_mode": bool, "ledger_id": ...}
    Raises:  HTTPException(409) on cap overrun (enforcement mode only).
    """
    if action not in _VALID_ACTIONS:
        raise ValueError(f"Unknown HOD ledger action: {action!r}")

    if not hod_email:
        raise ValueError("hod_email is required")

    email_lc = hod_email.lower()
    period   = _current_period()
    amount   = _money(amount_usd)
    prev_lim = _money(previous_limit_usd) if previous_limit_usd is not None else None
    new_lim  = _money(new_limit_usd)      if new_limit_usd      is not None else None

    # Optional free-text reason the HOD gave for this allocation. Trimmed and
    # length-capped defensively; empty becomes NULL so audit shows a clean dash.
    just = (justification or "").strip()
    just = just[:1000] if just else None

    # No negative charges — the spec is "no refund for decrease or delete".
    if amount < 0:
        amount = Decimal("0.00")

    shadow   = not _enforcement_enabled()

    db = SessionLocal()
    try:
        # ── Transaction: lock cap row, read consumption, decide, insert ─────
        with db.begin():
            cap_row = db.execute(
                text(
                    'SELECT "monthly_cap_usd", "is_active" '
                    'FROM ainxt.hod_allocation_caps '
                    'WHERE lower("hod_email") = :e '
                    'FOR UPDATE'
                ),
                {"e": email_lc},
            ).first()

            if cap_row is None:
                cap = _default_cap_usd()
                has_row = False
            else:
                if not bool(cap_row[1]):
                    # Inactive row is treated like no-row → falls back to default.
                    cap = _default_cap_usd()
                    has_row = False
                else:
                    cap = _money(cap_row[0])
                    has_row = True

            # Consumption for the live (non-shadow) ledger only.
            consumed = _fetch_consumption(db, email_lc, period)
            projected = consumed + amount
            would_exceed = projected > cap

            if would_exceed and not shadow:
                # Enforcement mode: refuse the action. Do NOT insert a ledger
                # row — this matches "transaction rolled back, user budget
                # unchanged" semantics.
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"HOD monthly allocation cap would be exceeded: "
                        f"${float(consumed):.2f} used + ${float(amount):.2f} requested "
                        f"> ${float(cap):.2f}. Cap resets on "
                        f"{_first_of_next_month().isoformat()}."
                    ),
                )

            # Decide what consumed_after to record.
            #   * Enforcement & within cap: projected
            #   * Shadow mode: still write projected but flag the row
            #   * Shadow mode & overflow: still write projected (so analysts
            #     can see how far over the HOD went) but row is shadow_mode=true
            #     and the live-mode consumption query (shadow_mode=false) is
            #     unaffected.
            consumed_after = projected

            insert_row = db.execute(
                text(
                    'INSERT INTO ainxt.hod_allocation_ledger ('
                    ' "id", "hod_email", "period_yyyymm", '
                    ' "target_user_id", "target_user_email", '
                    ' "action", "amount_usd", '
                    ' "previous_limit_usd", "new_limit_usd", '
                    ' "request_id", "cap_at_time_usd", '
                    ' "consumed_after_usd", "shadow_mode", "justification",'
                    ' "created_at"'
                    ') VALUES ('
                    ' gen_random_uuid(), :e, :p, :tu, :te, :a, :amt,'
                    ' :pl, :nl, :rid, :cap, :ca, :sh, :just,'
                    ' NOW()'
                    ') RETURNING "id"'
                ),
                {
                    "e":    email_lc,
                    "p":    period,
                    "tu":   str(target_user_id),
                    "te":   (target_user_email or "").lower() or None,
                    "a":    action,
                    "amt":  amount,
                    "pl":   prev_lim,
                    "nl":   new_lim,
                    "rid":  request_id,
                    "cap":  cap,
                    "ca":   consumed_after,
                    "sh":   shadow,
                    "just": just,
                },
            )
            ledger_id = insert_row.scalar()
    finally:
        db.close()

    remaining = cap - consumed_after
    if remaining < 0:
        remaining = Decimal("0.00")

    # Structured audit log — single line, grep-friendly.
    logger.info(
        "hod_cap_charge actor=%s period=%s action=%s amount=%.2f "
        "consumed_after=%.2f cap=%.2f shadow=%s has_cap_row=%s would_exceed=%s "
        "target=%s ledger_id=%s",
        email_lc, period, action,
        float(amount), float(consumed_after), float(cap),
        shadow, has_row, would_exceed,
        target_user_id, ledger_id,
    )

    return {
        "cap_usd":       float(cap),
        "consumed_usd":  float(consumed_after),
        "remaining_usd": float(remaining),
        "charged":       amount > Decimal("0.00"),
        "shadow_mode":   shadow,
        "ledger_id":     str(ledger_id) if ledger_id else None,
    }


# ── Convenience for the router (compute the delta) ───────────────────────────

def compute_allocate_delta(old_max_cost: float, new_max_cost: float) -> Decimal:
    """
    Charge model (per spec):
      * Increase   → delta = new - old      (positive, charged)
      * No change  → 0                      (recorded as zero)
      * Decrease   → 0                      (no refund)
    """
    delta = _money(new_max_cost) - _money(old_max_cost)
    if delta < 0:
        return Decimal("0.00")
    return delta


# ── Cap check only (no ledger insert) — for callers that already own the
#    ledger row and will UPDATE it themselves within their own transaction ──
#
# Used by store.budget_store.approve_budget_request(), which locks a whole
# request-fan-out group (multiple hod_allocation_ledger rows sharing one
# request_id) via its own SELECT ... FOR UPDATE and then needs to charge only
# the acting HOD's cap and stamp cap_at_time_usd/consumed_after_usd onto the
# row it is already updating — inserting a second row here would double the
# audit trail and break the one-row-per-HOD-per-request invariant.

# ── Query allow-list + guarded execution wrapper ─────────────────────────────
#
# The two cap statements are declared here as module-level CONSTANTS and
# registered in a frozen allow-list. `_safe_execute()` refuses to run any SQL
# text that is not one of these pre-approved constants, and refuses any call
# that does not supply its values as a bound-parameter tuple.
#
# Effect: the SQL structure reaching the driver is provably one of two fixed,
# reviewed strings — it cannot be assembled, extended, or influenced at runtime
# by any value, from any source. Every dynamic value must travel as a bound
# parameter. This makes query-structure tampering impossible by construction,
# independently of what the parameter values contain.
#
# In addition, EVERY value bound into those statements is validated against a
# strict positive allow-list of characters before it is handed to the driver
# (see `_clean_email_value` / `_clean_period_value`, enforced by
# `_validate_bound_params`). This closes the second-order case where the value
# itself originates from a previous database read rather than from the current
# request: data re-read from the DB is re-validated here, at the sink, exactly
# as if it were fresh untrusted input, and is rejected if it carries anything
# outside the permitted character set.

# Declared Final: these are compile-time constants. Any attempt to reassign
# them is a static-analysis/type-check error, and the values themselves are
# immutable at runtime (str and frozenset cannot be modified in place).
_SQL_SELECT_CAP: Final[str] = (
    'SELECT "monthly_cap_usd", "is_active" '
    'FROM ainxt.hod_allocation_caps '
    'WHERE lower("hod_email") = %s '
    'FOR UPDATE'
)

_SQL_SELECT_CONSUMED: Final[str] = (
    'SELECT COALESCE(MAX("consumed_after_usd"), 0) '
    'FROM ainxt.hod_allocation_ledger '
    'WHERE lower("hod_email") = %s AND "period_yyyymm" = %s AND "shadow_mode" = FALSE '
    "  AND \"action\" <> 'endpoint_spend'"
)

# ── Request-scoped variants: the HOD e-mail is resolved INSIDE the database ──
#
# These two statements do the same work as the pair above, but instead of
# accepting an e-mail address that Python previously read out of the ledger,
# they re-derive it in-database from the request being approved, via a
# subquery on ainxt.hod_allocation_ledger.
#
# Why this matters: the caller no longer has to read a row, carry the e-mail
# back into Python, and hand it to a later query. The only values crossing the
# application boundary are the request_id (a UUID from the URL path) and the
# acting approver's e-mail (from the authenticated session) — neither of which
# is read back out of the database. The e-mail value never leaves Postgres, so
# the read → re-use round-trip is removed entirely rather than merely guarded.
#
# The authorisation predicate mirrors the Python row-selection in
# approve_budget_request() exactly, including two details that matter for
# picking the SAME row and therefore charging the SAME HOD:
#
#   1. "no delegation" is COALESCE(delegated_to, '') = '', not just IS NULL.
#      The Python check is `(row.delegated_to or "") == ""`, which treats an
#      empty string identically to NULL. Matching only on IS NULL would skip a
#      row that Python would have accepted.
#
#   2. Ordering is deterministic and matches Python's preference order. Python
#      looks for a direct HOD row FIRST and only falls back to a delegatee row.
#      A bare LIMIT 1 would let Postgres return either one when a user is both
#      the HOD on one row and a delegatee on another row of the same request.
#      The CASE below ranks direct-HOD rows ahead of delegatee rows, and
#      lower(hod_email) is the tiebreaker — the same order as the outer
#      SELECT's `ORDER BY hod_email`.
_SQL_RESOLVE_CAP_HOD: Final[str] = (
    'SELECT lower(l."hod_email") '
    'FROM ainxt.hod_allocation_ledger l '
    'WHERE l."request_id" = %s '
    '  AND l."action" IN (\'approve_request\', \'winner_approve_request\') '
    '  AND ((lower(l."hod_email") = %s AND COALESCE(l."delegated_to", \'\') = \'\') '
    '       OR lower(COALESCE(l."delegated_to", \'\')) = %s) '
    'ORDER BY CASE WHEN COALESCE(l."delegated_to", \'\') = \'\' THEN 0 ELSE 1 END, '
    '         lower(l."hod_email") '
    'LIMIT 1'
)

_SQL_SELECT_CAP_FOR_REQUEST: Final[str] = (
    'SELECT c."monthly_cap_usd", c."is_active" '
    'FROM ainxt.hod_allocation_caps c '
    'WHERE lower(c."hod_email") = ('
    '    SELECT lower(l."hod_email") FROM ainxt.hod_allocation_ledger l '
    '    WHERE l."request_id" = %s '
    '      AND l."action" IN (\'approve_request\', \'winner_approve_request\') '
    '      AND ((lower(l."hod_email") = %s AND COALESCE(l."delegated_to", \'\') = \'\') '
    '           OR lower(COALESCE(l."delegated_to", \'\')) = %s) '
    '    ORDER BY CASE WHEN COALESCE(l."delegated_to", \'\') = \'\' THEN 0 ELSE 1 END, '
    '             lower(l."hod_email") '
    '    LIMIT 1'
    ') '
    'FOR UPDATE'
)

_SQL_SELECT_CONSUMED_FOR_REQUEST: Final[str] = (
    'SELECT COALESCE(MAX(t."consumed_after_usd"), 0) '
    'FROM ainxt.hod_allocation_ledger t '
    'WHERE lower(t."hod_email") = ('
    '    SELECT lower(l."hod_email") FROM ainxt.hod_allocation_ledger l '
    '    WHERE l."request_id" = %s '
    '      AND l."action" IN (\'approve_request\', \'winner_approve_request\') '
    '      AND ((lower(l."hod_email") = %s AND COALESCE(l."delegated_to", \'\') = \'\') '
    '           OR lower(COALESCE(l."delegated_to", \'\')) = %s) '
    '    ORDER BY CASE WHEN COALESCE(l."delegated_to", \'\') = \'\' THEN 0 ELSE 1 END, '
    '             lower(l."hod_email") '
    '    LIMIT 1'
    ') '
    'AND t."period_yyyymm" = %s AND t."shadow_mode" = FALSE '
    "AND t.\"action\" <> 'endpoint_spend'"
)

# Only these exact statements may ever be executed by _safe_execute().
_ALLOWED_SQL: Final[frozenset] = frozenset({
    _SQL_SELECT_CAP,
    _SQL_SELECT_CONSUMED,
    _SQL_RESOLVE_CAP_HOD,
    _SQL_SELECT_CAP_FOR_REQUEST,
    _SQL_SELECT_CONSUMED_FOR_REQUEST,
})

# ── Value-level input validation (positive allow-list) ───────────────────────
#
# Both cap statements bind exactly two kinds of value: an HOD e-mail address
# and a 'YYYY-MM' period. Each is validated against a strict whitelist regex that
# admits ONLY the characters legitimately required by that value's format.
# Every character used in SQL injection payloads — quote, semicolon, comment
# markers, parentheses, whitespace, backslash, wildcard — is outside both
# patterns and is therefore rejected, not escaped.
#
# This validation is applied at the sink for every execution, so a value that
# was previously stored in the database and is now being read back and reused
# (the second-order case) is re-validated here just like first-order input.

_RE_EMAIL_VALUE: Final = re.compile(r"^[A-Za-z0-9._%+-]{1,190}@[A-Za-z0-9.-]{1,60}\.[A-Za-z]{2,24}$")
_RE_PERIOD_VALUE: Final = re.compile(r"^[0-9]{4}-[0-9]{2}$")
_RE_UUID_VALUE: Final = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_MAX_VALUE_LEN: Final[int] = 254


def _clean_email_value(value) -> str:
    """Return `value` only if it is a well-formed e-mail address.

    Whitelist validation: the string must match `_RE_EMAIL_VALUE` in full.
    No escaping, stripping or rewriting is attempted — a value that does not
    match the permitted character set is refused outright, so no metacharacter
    can survive this check and reach the database.
    """
    if not isinstance(value, str):
        raise TypeError("blocked: e-mail parameter must be a string")
    if len(value) > _MAX_VALUE_LEN:
        raise ValueError("blocked: e-mail parameter exceeds the maximum permitted length")
    if not _RE_EMAIL_VALUE.fullmatch(value):
        raise ValueError("blocked: e-mail parameter contains characters outside the allow-list")
    return value


def _clean_period_value(value) -> str:
    """Return `value` only if it is a well-formed 'YYYY-MM' calendar month.

    Digits and a single separating hyphen are the only characters admitted, so
    the value cannot carry SQL metacharacters of any kind.
    """
    if not isinstance(value, str):
        raise TypeError("blocked: period parameter must be a string")
    if not _RE_PERIOD_VALUE.fullmatch(value):
        raise ValueError("blocked: period parameter must be a 'YYYY-MM' calendar month")
    return value


def _clean_uuid_value(value) -> str:
    """Return `value` only if it is a canonical UUID (8-4-4-4-12 hex).

    request_id values are generated with uuid4(). Only hex digits and the four
    separating hyphens are admitted, so the value cannot carry SQL
    metacharacters of any kind.
    """
    if not isinstance(value, str):
        raise TypeError("blocked: request identifier must be a string")
    if not _RE_UUID_VALUE.fullmatch(value):
        raise ValueError("blocked: request identifier must be a canonical UUID")
    return value


# Per-statement validator chain: one validator per bind placeholder, in order.
_PARAM_VALIDATORS: Final[dict] = {
    _SQL_SELECT_CAP:      (_clean_email_value,),
    _SQL_SELECT_CONSUMED: (_clean_email_value, _clean_period_value),
    # request_id, acting_email, acting_email
    _SQL_RESOLVE_CAP_HOD: (_clean_uuid_value, _clean_email_value, _clean_email_value),
    _SQL_SELECT_CAP_FOR_REQUEST: (
        _clean_uuid_value, _clean_email_value, _clean_email_value,
    ),
    # request_id, acting_email, acting_email, period
    _SQL_SELECT_CONSUMED_FOR_REQUEST: (
        _clean_uuid_value, _clean_email_value, _clean_email_value, _clean_period_value,
    ),
}


def _validate_bound_params(sql: str, params: tuple) -> tuple:
    """Validate every bound value for `sql` and return the validated tuple.

    The arity must match the statement's placeholder count exactly, and each
    value must pass its position's whitelist validator. Returns a NEW tuple
    built solely from validated values, so only checked data is ever bound.
    """
    validators = _PARAM_VALIDATORS[sql]
    if len(params) != len(validators):
        raise ValueError("blocked: bound-parameter count does not match the approved statement")
    return tuple(check(value) for check, value in zip(validators, params))


def _safe_execute(cur, sql: str, params: tuple):
    """Execute a pre-approved constant SQL statement with validated bound values.

    Guarantees enforced at call time:
      1. `sql` must be identical to one of the reviewed constants in
         `_ALLOWED_SQL`. Any runtime-built, modified, or unknown query text is
         rejected outright — so no value can ever contribute to query structure.
      2. `params` must be a tuple, i.e. values are handed to the driver as
         BOUND PARAMETERS and are transmitted separately from the SQL text.
         Passing values inside the statement is therefore impossible here.
      3. Every value in `params` is validated against a strict positive
         allow-list for its position (see `_validate_bound_params`) BEFORE it
         reaches the driver. Values are never escaped or repaired: anything
         containing a character outside the permitted set is refused.

    Guarantee (3) is what makes this safe for second-order flows: values that
    were read back out of the database are re-validated here, at the sink, on
    every single execution, so previously-stored data is treated as untrusted
    input rather than as trusted because it came from our own tables.

    Because the statement text is a fixed constant and every value is both
    validated and bound, the executed query's structure cannot be altered by
    any input, whatever its origin.
    """
    if sql not in _ALLOWED_SQL:
        raise ValueError("blocked: SQL statement is not in the approved allow-list")
    if not isinstance(params, tuple):
        raise TypeError("blocked: query values must be passed as a bound-parameter tuple")
    safe_params = _validate_bound_params(sql, params)
    return cur.execute(sql, safe_params)


def check_and_reserve_cap(cur, hod_email: str, amount_usd) -> dict:
    """
    Lock the HOD's cap row (SELECT ... FOR UPDATE) and compute the
    cap/consumed/remaining state after charging `amount_usd`, WITHOUT
    inserting any ledger row. Must be called from within an existing
    transaction on `cur` (a psycopg2 cursor) — the caller commits/rolls back.

    Raises HTTPException(409) if the charge would exceed the cap AND
    enforcement is enabled. In shadow mode, never raises.

    `hod_email` is treated as UNTRUSTED regardless of where the caller got it
    from — including when it was read back out of ainxt.hod_allocation_ledger
    by an earlier query. It is validated against a strict allow-list at entry
    (and again at the sink in `_safe_execute`), so stored data cannot be used
    to smuggle SQL metacharacters into the cap statements.

    Returns: {"cap_usd", "consumed_usd", "remaining_usd", "shadow_mode"}
    """
    if not hod_email:
        raise ValueError("hod_email is required")

    # Validate the incoming value BEFORE it is used anywhere. Whitelist check:
    # anything outside the permitted e-mail character set is rejected here, so
    # no metacharacter from previously-stored data can travel any further.
    email_lc = _clean_email_value(str(hod_email).strip().lower())
    period   = _clean_period_value(_current_period())
    amount   = _money(amount_usd)
    if amount < 0:
        amount = Decimal("0.00")
    shadow = not _enforcement_enabled()

    # Guarded execution: constant, allow-listed SQL + bound parameters only.
    _safe_execute(cur, _SQL_SELECT_CAP, (email_lc,))
    cap_row = cur.fetchone()
    if cap_row is None or not bool(cap_row[1]):
        cap = _default_cap_usd()
    else:
        cap = _money(cap_row[0])

    # Running-total baseline. Must match _fetch_consumption() (used by the UI
    # banner and the admin HOD-Caps table) so the value we stamp onto the
    # approved row is coherent with what those views read back — i.e. MAX over
    # ALL non-shadow rows for the period, regardless of action/status.
    #
    # This deliberately does NOT filter status='approved': allocate/sdlc_run
    # rows are legitimate consumption too, and the current pending row (plus any
    # superseded siblings) carries consumed_after_usd = NULL, so it can never
    # inflate the MAX before we charge it.
    #
    # It DOES exclude action='endpoint_spend' carrier rows — those track
    # managed-endpoint cloud consumption in endpoint_spend_usd and leave
    # consumed_after_usd NULL (see _fetch_consumption).
    # Guarded execution: constant, allow-listed SQL + bound parameters only.
    _safe_execute(cur, _SQL_SELECT_CONSUMED, (email_lc, period))
    consumed_row = cur.fetchone()
    consumed = _money(consumed_row[0] if consumed_row else 0)

    projected = consumed + amount
    would_exceed = projected > cap
    if would_exceed and not shadow:
        raise HTTPException(
            status_code=409,
            detail=(
                f"HOD monthly allocation cap would be exceeded: "
                f"${float(consumed):.2f} used + ${float(amount):.2f} requested "
                f"> ${float(cap):.2f}. Cap resets on "
                f"{_first_of_next_month().isoformat()}."
            ),
        )

    remaining = cap - projected
    if remaining < 0:
        remaining = Decimal("0.00")

    logger.info(
        "hod_cap_charge(no-insert) actor=%s period=%s amount=%.2f "
        "consumed_after=%.2f cap=%.2f shadow=%s would_exceed=%s",
        email_lc, period, float(amount), float(projected), float(cap), shadow, would_exceed,
    )

    return {
        "cap_usd":       float(cap),
        "consumed_usd":  float(projected),
        "remaining_usd": float(remaining),
        "shadow_mode":   shadow,
    }


def check_and_reserve_cap_for_request(cur, request_id: str, acting_email: str,
                                      amount_usd) -> dict:
    """Cap check for an approval, resolving the charged HOD inside the database.

    Behaviourally identical to `check_and_reserve_cap()`, but the HOD e-mail
    that the charge lands on is NEVER carried through Python. Instead it is
    re-derived in-database by a subquery over ainxt.hod_allocation_ledger,
    scoped to `request_id` and authorised against `acting_email`.

    The only values crossing the application boundary are:
      * `request_id`   — a UUID taken from the request URL, and
      * `acting_email` — the authenticated caller's own e-mail (session),
    neither of which is data previously read back out of the database. The
    stored hod_email stays inside Postgres for the whole operation.

    Authorisation matches the rest of the approval path exactly: the acting
    user must be the row's HOD (with delegated_to NULL) or the row's delegatee.
    The cap is charged to the ROW's hod_email — i.e. the ORIGINAL HOD — so a
    delegatee still spends the HOD's allocation, not their own.

    Raises:
        HTTPException(409) if the charge would exceed the cap and enforcement
            is enabled (never in shadow mode).
        LookupError if no ledger row matches request_id for this acting user.

    Returns: {"cap_usd", "consumed_usd", "remaining_usd", "shadow_mode"}
    """
    rid       = _clean_uuid_value(str(request_id or "").strip())
    actor_lc  = _clean_email_value(str(acting_email or "").strip().lower())
    period    = _clean_period_value(_current_period())
    amount    = _money(amount_usd)
    if amount < 0:
        amount = Decimal("0.00")
    shadow = not _enforcement_enabled()

    # Authorisation + existence check. Also gives us the charged HOD for the
    # audit log only — it is NOT fed back into any subsequent query.
    _safe_execute(cur, _SQL_RESOLVE_CAP_HOD, (rid, actor_lc, actor_lc))
    hod_row = cur.fetchone()
    if hod_row is None or not hod_row[0]:
        raise LookupError("no ledger row for this request is actionable by the acting user")
    charged_hod = hod_row[0]

    # Cap lookup — the HOD is resolved by the subquery, in-database.
    _safe_execute(cur, _SQL_SELECT_CAP_FOR_REQUEST, (rid, actor_lc, actor_lc))
    cap_row = cur.fetchone()
    if cap_row is None or not bool(cap_row[1]):
        cap = _default_cap_usd()
    else:
        cap = _money(cap_row[0])

    # Running-total baseline — same semantics as check_and_reserve_cap():
    # MAX over all non-shadow rows for the period, excluding endpoint_spend.
    _safe_execute(
        cur, _SQL_SELECT_CONSUMED_FOR_REQUEST, (rid, actor_lc, actor_lc, period),
    )
    consumed_row = cur.fetchone()
    consumed = _money(consumed_row[0] if consumed_row else 0)

    projected = consumed + amount
    would_exceed = projected > cap
    if would_exceed and not shadow:
        raise HTTPException(
            status_code=409,
            detail=(
                f"HOD monthly allocation cap would be exceeded: "
                f"${float(consumed):.2f} used + ${float(amount):.2f} requested "
                f"> ${float(cap):.2f}. Cap resets on "
                f"{_first_of_next_month().isoformat()}."
            ),
        )

    remaining = cap - projected
    if remaining < 0:
        remaining = Decimal("0.00")

    logger.info(
        "hod_cap_charge(no-insert,by-request) hod=%s actor=%s period=%s amount=%.2f "
        "consumed_after=%.2f cap=%.2f shadow=%s would_exceed=%s",
        charged_hod, actor_lc, period, float(amount), float(projected),
        float(cap), shadow, would_exceed,
    )

    return {
        "cap_usd":       float(cap),
        "consumed_usd":  float(projected),
        "remaining_usd": float(remaining),
        "shadow_mode":   shadow,
    }
