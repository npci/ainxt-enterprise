# SPDX-License-Identifier: Apache-2.0
# ============================================================
# BUDGET AUDIT SERVICE  —  monthly snapshot + reset
#
# Single entry point: snapshot_and_reset_all_budgeted_users(period_yyyymm).
#
# For every user with a per-user BudgetConfig row:
#   1. SELECT FOR UPDATE on their BudgetConfig (prevents races with
#      concurrent HOD approvals / admin top-ups).
#   2. Read User.ad_level + UserUsageTotal counters + the user's
#      HodAllocationLedger entries for `period_yyyymm`.
#   3. Look up the previous period's BudgetPeriodAudit for opening_limit_usd.
#   4. INSERT a BudgetPeriodAudit row with ON CONFLICT DO NOTHING
#      (idempotent — re-running for the same period is a no-op).
#   5. UPDATE BudgetConfig.monthly_limit_usd → band default.
#   6. Call store.budget_store.reset_usage(user_id) to zero Postgres +
#      Redis counters.
#
# Per-user errors are caught + logged with user_id; the batch continues.
#
# NOTE: This service has NO internal feature flag check. Callers are
# responsible for gating on BUDGET_MONTHLY_RESET_ENABLED.
# ============================================================

from __future__ import annotations

import html as _html
import logging
import os
import calendar
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.database import SessionLocal
from db.models import (
    BudgetConfig,
    BudgetPeriodAudit,
    HodAllocationLedger,
    User,
    UserUsageTotal,
)
from db.monthly_statement_models import UserNotificationPreference
from routers.budget_router import _get_band_allocation
from services.smtp_service import send_html_email
from store.budget_store import reset_usage, update_cached_cost_limit

logger = logging.getLogger(__name__)

# ── TEST OVERRIDE ───────────────────────────────────────────────────────────
# Set BUDGET_RESET_EMAIL_TEST_OVERRIDE=<email> to route ALL reset-related emails
# (pre-warning + post-confirmation) to that single address regardless of the
# recipient's stored email or opt-out preference. Leave UNSET in production.
_EMAIL_TEST_OVERRIDE = os.getenv("BUDGET_RESET_EMAIL_TEST_OVERRIDE", "").strip()

# Treat anything below this as a "genuine" increase (filter out approve_request
# rows that did not actually bump the limit).
_ZERO = Decimal("0")


def _to_decimal(value: Any, default: Decimal = _ZERO) -> Decimal:
    """Coerce arbitrary numeric/None into Decimal without losing precision."""
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return default


#: Base allocation every user resets to, winners included.
_RESET_BASE_USD = Decimal("50")


def compute_winner_carryover(
    utilised_usd: Decimal,
    extra_usd: Decimal,
    winner_extra_usd: Decimal,
    base_usd: Decimal = _RESET_BASE_USD,
) -> Decimal:
    """Return the 10x-winner extra balance that survives the monthly reset.

    `extra_usd` is the POOLED extra allocation; `winner_extra_usd` is the
    winner-origin slice of it. The remainder is HOD-granted money, which
    never carries over — so it is drained FIRST, and only spend beyond both
    the base and the HOD portion eats into the winner's balance:

        over_base   = max(0, utilised - base)
        hod_portion = extra - winner_extra
        drain       = max(0, over_base - hod_portion)
        new_winner  = max(0, winner_extra - drain)

    Consequences worth noting:
      - Utilisation at or below the base => the full winner balance carries.
      - A winner with HOD top-ups spends those down first, at no cost to
        their award.
      - Once the winner balance reaches 0 it stays there until a new award.

    Pure arithmetic — no DB access — so the boundary cases are unit-testable
    on their own. Decimal throughout to match the NUMERIC(12,6) columns and
    avoid float drift on values like 120.79.
    """
    over_base = utilised_usd - base_usd
    if over_base < _ZERO:
        over_base = _ZERO

    hod_portion = extra_usd - winner_extra_usd
    if hod_portion < _ZERO:
        # Defensive: winner_extra should never exceed the pooled extra, but a
        # hand-edited row must not yield a negative HOD portion (which would
        # over-drain the winner).
        hod_portion = _ZERO

    drain = over_base - hod_portion
    if drain < _ZERO:
        drain = _ZERO

    new_winner = winner_extra_usd - drain
    if new_winner < _ZERO:
        new_winner = _ZERO
    return new_winner


def _ledger_row_to_json(row: HodAllocationLedger) -> dict:
    """Serialise a HodAllocationLedger row into a JSON-safe dict."""
    created = row.created_at
    if isinstance(created, datetime):
        created_iso = created.isoformat()
    else:
        created_iso = str(created) if created is not None else None
    return {
        "ledger_id":          str(row.id),
        "action":             row.action,
        "amount_usd":         float(row.amount_usd) if row.amount_usd is not None else None,
        "previous_limit_usd": float(row.previous_limit_usd) if row.previous_limit_usd is not None else None,
        "new_limit_usd":      float(row.new_limit_usd) if row.new_limit_usd is not None else None,
        "hod_email":          row.hod_email,
        "request_id":         row.request_id,
        "created_at":         created_iso,
    }


def _snapshot_one_user(user_id: str, period_yyyymm: str) -> str:
    """
    Snapshot + reset for a single user. Each call uses its own session
    so a failure in one user does not poison the batch transaction.

    Returns one of: "snapshotted", "skipped" (idempotent no-op).
    Raises on error — caller logs + counts.
    """
    db = SessionLocal()
    try:
        # ── 1. Lock the user's BudgetConfig row ──────────────────────────
        cfg = (
            db.query(BudgetConfig)
              .filter(BudgetConfig.user_id == user_id)
              .with_for_update()
              .first()
        )
        if cfg is None:
            # Row vanished between the outer SELECT and now — treat as skip.
            return "skipped"

        closing_limit_usd = _to_decimal(cfg.monthly_limit_usd)

        # ── 2. ad_level + band default ───────────────────────────────────
        user_row = db.query(User).filter(User.id == user_id).first()
        ad_level = user_row.ad_level if (user_row and user_row.ad_level is not None) else None

        # _get_band_allocation opens its own session — safe; it only reads.
        default_limit_usd = _to_decimal(_get_band_allocation(ad_level if ad_level is not None else 6))

        # ── 3. Usage counters ────────────────────────────────────────────
        usage = (
            db.query(UserUsageTotal)
              .filter(UserUsageTotal.user_id == user_id)
              .first()
        )
        cost_used_usd = _to_decimal(usage.cost_usd_spent) if usage else _ZERO
        tokens_used   = int(usage.tokens_used)   if usage else 0
        requests_made = int(usage.requests_made) if usage else 0

        unutilized_usd = closing_limit_usd - cost_used_usd
        if unutilized_usd < _ZERO:
            unutilized_usd = _ZERO

        # ── 4. HOD ledger history for this period ───────────────────────
        # HodAllocationLedger.target_user_id is UUID, BudgetConfig.user_id is
        # VARCHAR(255). Cast in the WHERE clause for portability across drivers.
        ledger_rows = (
            db.query(HodAllocationLedger)
              .filter(
                  text("CAST(ainxt.hod_allocation_ledger.target_user_id AS TEXT) = :uid"),
                  HodAllocationLedger.period_yyyymm == period_yyyymm,
              )
              .params(uid=user_id)
              .order_by(HodAllocationLedger.created_at.asc())
              .all()
        )

        increase_history_json: list = []
        increase_count = 0
        for r in ledger_rows:
            increase_history_json.append(_ledger_row_to_json(r))
            prev = _to_decimal(r.previous_limit_usd, default=_ZERO)
            new  = _to_decimal(r.new_limit_usd,      default=_ZERO)
            if new > prev:
                increase_count += 1

        # ── 5. Opening limit from previous period's audit (best-effort) ──
        prev_audit = (
            db.query(BudgetPeriodAudit)
              .filter(BudgetPeriodAudit.user_id == user_id)
              .filter(BudgetPeriodAudit.period_yyyymm < period_yyyymm)
              .order_by(BudgetPeriodAudit.period_yyyymm.desc())
              .first()
        )
        opening_limit_usd = (
            _to_decimal(prev_audit.closing_limit_usd)
            if prev_audit is not None
            else default_limit_usd
        )

        # ── 6. Work out the post-reset allocation ───────────────────────
        # Pure arithmetic, computed BEFORE the INSERT so `reset_to_usd` (and
        # therefore the confirmation email's "New monthly limit") reflects the
        # carried winner balance rather than a flat $50. Computing it here is
        # safe on a re-run: nothing is written until the `if inserted:` branch
        # below, so the drain is still applied at most once per period.
        old_extra        = _to_decimal(cfg.extra_cost_usd)
        old_winner_extra = _to_decimal(cfg.winner_extra_usd)
        new_winner_extra = compute_winner_carryover(
            utilised_usd     = cost_used_usd,   # cost_usd_spent for the closing period
            extra_usd        = old_extra,
            winner_extra_usd = old_winner_extra,
            base_usd         = _RESET_BASE_USD,
        )
        new_total = _RESET_BASE_USD + new_winner_extra

        # ── 6b. INSERT ... ON CONFLICT DO NOTHING ───────────────────────
        stmt = (
            pg_insert(BudgetPeriodAudit.__table__)
            .values(
                user_id               = user_id,
                period_yyyymm         = period_yyyymm,
                ad_level              = ad_level,
                default_limit_usd     = default_limit_usd,
                opening_limit_usd     = opening_limit_usd,
                closing_limit_usd     = closing_limit_usd,
                cost_used_usd         = cost_used_usd,
                tokens_used           = tokens_used,
                requests_made         = requests_made,
                unutilized_usd        = unutilized_usd,
                increase_count        = increase_count,
                increase_history_json = increase_history_json,
                reset_to_usd          = new_total,
            )
            .on_conflict_do_nothing(
                constraint="uq_budget_period_audit_user_period"
            )
        )
        result = db.execute(stmt)
        inserted = (result.rowcount or 0) > 0

        if not inserted:
            # Already snapshotted for this period — DO NOT reset again.
            # This preserves idempotency: re-running for the same period is
            # a true no-op (the limit will already have been reset on the
            # original run; we must not blow away any subsequent admin/HOD
            # top-ups by resetting a second time).
            db.commit()
            return "skipped"

        # ── 7. Reset the per-user cost allocation ───────────────────────
        #
        # Base returns to $50 for EVERY user, winners included — no band
        # differentiation applies to the cost dimension. _get_band_allocation
        # is kept only for the audit `default_limit_usd` snapshot column,
        # preserving its historical shape.
        #
        # Extra is NOT flattened to $0 any more. HOD-granted extra still
        # expires, but the unspent 10x-winner portion carries over: the drain
        # formula (see compute_winner_carryover) consumes the HOD money first
        # and only then eats into the winner's balance.
        #
        # These writes MUST stay inside the `if inserted:` branch — a re-run
        # for the same period returns "skipped" above, so the drain lands
        # exactly once per period. Applying it outside would double-drain.
        # (new_winner_extra / new_total were computed at step 6.)
        cfg.base_cost_usd    = _RESET_BASE_USD
        cfg.extra_cost_usd   = new_winner_extra   # pooled == winner-only after reset
        cfg.winner_extra_usd = new_winner_extra
        # monthly_limit_usd is the audit's closing_limit_usd next period AND
        # the sole input to monthly-statement utilisation, so it has to track
        # the carried balance rather than staying flat at $50.
        cfg.monthly_limit_usd  = float(new_total)
        # Fully depleted ⇒ drop the award provenance, so the user is eligible
        # for a fresh award and the UI stops badging them as a winner.
        if new_winner_extra <= _ZERO:
            cfg.winner_origin_period = None
        db.add(cfg)

        # max_cost_usd_total is NOT a mapped column on the BudgetConfig ORM
        # class (it was added to the table via raw SQL in db/migrate.py and
        # was left unmapped) — setting it as a plain attribute would be
        # silently dropped by SQLAlchemy on flush. Write it directly so the
        # Postgres fallback path (_pg_get_budget) doesn't keep serving the
        # pre-reset total if the Redis cache is ever flushed. Same
        # transaction as the ORM commit above, so this can't drift from it.
        db.execute(
            text("UPDATE ainxt.budget_configs SET max_cost_usd_total = :v WHERE user_id = :uid"),
            {"v": float(new_total), "uid": user_id},
        )
        db.commit()

        # ── 8. Zero usage counters (Postgres + Redis) ───────────────────
        # reset_usage manages its own connection / commit.
        reset_usage(user_id)

        # Keep the Redis fast-path budget cache in sync with the new Postgres
        # limits. get_budget() is Redis-first, so without this the UI would
        # keep serving the pre-reset base/extra/total until the budget:{uid}
        # hash expired. Reset every cost field together.
        update_cached_cost_limit(
            user_id,
            float(new_total),
            new_base_cost_usd=float(_RESET_BASE_USD),
            new_extra_cost_usd=float(new_winner_extra),
            new_winner_extra_usd=float(new_winner_extra),
            clear_winner_origin_period=(new_winner_extra <= _ZERO),
        )

        # ── 9. Best-effort confirmation email — never breaks the reset ──
        # Sent AFTER commit, so SMTP failures cannot roll back the snapshot.
        try:
            _send_post_reset_confirmation(user_id, period_yyyymm)
        except Exception:
            logger.error(
                "budget_audit_service: confirmation email crashed user=%s period=%s",
                user_id, period_yyyymm,
            )

        return "snapshotted"
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()


def snapshot_and_reset_all_budgeted_users(period_yyyymm: str) -> dict:
    """
    For every user with a per-user BudgetConfig row:
      1. Snapshot their closing state into budget_period_audits.
      2. Reset BudgetConfig.monthly_limit_usd to the band default.
      3. Zero their usage counters (Postgres + Redis) via reset_usage().

    Idempotent: re-running for the same period_yyyymm is a no-op (the
    INSERT uses ON CONFLICT DO NOTHING; if the row already exists, the
    reset step is skipped to avoid clobbering subsequent admin/HOD changes).

    Then, once for the whole table:
      4. Snapshot managed-endpoint cloud spend (action='endpoint_spend' rows).
      5. Wipe ainxt.hod_allocation_ledger — this resets both approved-allocation
         history AND the endpoint cloud-spend counters for the new period.
      6. Clear the endpoint-spend KV counters, so no stale cached total leaks
         into the new period and wrongly gates it.

    Caller is responsible for checking BUDGET_MONTHLY_RESET_ENABLED before
    invoking. This service performs full mutations unconditionally.

    Returns: {"period", "processed", "snapshotted", "reset", "skipped", "errors",
              "ledger_wiped", "endpoint_spend_closing", "endpoint_cache_cleared"}.
    """
    if not period_yyyymm or len(period_yyyymm) != 7 or period_yyyymm[4] != "-":
        raise ValueError(f"period_yyyymm must be 'YYYY-MM', got {period_yyyymm!r}")

    logger.info(
        "budget_audit_service: starting snapshot_and_reset for period=%s",
        period_yyyymm,
    )

    # Enumerate user_ids up-front using a short-lived session, then iterate
    # one-by-one with per-user sessions for transactional isolation.
    enum_db = SessionLocal()
    try:
        user_ids = [
            row.user_id
            for row in enum_db.query(BudgetConfig.user_id)
                              .filter(BudgetConfig.user_id.isnot(None))
                              .all()
        ]
    finally:
        enum_db.close()

    processed = 0
    snapshotted = 0
    skipped = 0
    errors = 0

    for uid in user_ids:
        processed += 1
        try:
            outcome = _snapshot_one_user(uid, period_yyyymm)
            if outcome == "snapshotted":
                snapshotted += 1
            else:
                skipped += 1
        except Exception:
            errors += 1
            logger.warning(
                "budget_audit_service: per-user failure user_id=%s period=%s",
                uid, period_yyyymm,
            )

    # ── Final step: wipe the HOD allocation ledger for the closed period ──
    #
    # Base/extra increases do not carry over across
    # a monthly reset — the ledger is the source of the "increase history"
    # for the current period only, and each user's closing state has already
    # been snapshotted into budget_period_audits.increase_history_json above
    # for durable audit. So the ledger table is fully truncated here (every
    # row, not just rows for the closed period) to start the new period
    # clean, including any still-`pending` request rows that were never
    # acted on — those requesters would need to resubmit.
    #
    # Runs ONCE for the whole table AFTER the per-user loop completes, not
    # per-user, to avoid wiping rows for users not yet processed in the same
    # batch.
    #
    # Managed-endpoint cloud spend lives in this same table as
    # action='endpoint_spend' carrier rows (one per HOD per period), so the wipe
    # resets those counters too — which is the intended behaviour. Their totals
    # are captured just below FIRST, because the DELETE is irreversible.
    endpoint_spend_snapshot = {}
    try:
        from services.endpoint_budget_governor import snapshot_endpoint_spend
        endpoint_spend_snapshot = snapshot_endpoint_spend(period_yyyymm)
        if endpoint_spend_snapshot:
            # Logged as a single grep-friendly line so the closed period's
            # endpoint spend remains recoverable from logs even though the
            # ledger rows are about to be deleted.
            logger.info(
                "budget_audit_service: endpoint_spend closing snapshot period=%s data=%s",
                period_yyyymm, endpoint_spend_snapshot,
            )
    except Exception:
        logger.warning(
            "budget_audit_service: endpoint spend snapshot failed period=%s",
            period_yyyymm,
        )

    ledger_deleted = 0
    try:
        wipe_db = SessionLocal()
        try:
            wipe_db.execute(text("DELETE FROM ainxt.hod_allocation_ledger"))
            wipe_db.commit()
        finally:
            wipe_db.close()
        try:
            count_db = SessionLocal()
            try:
                row = count_db.execute(
                    text("SELECT COUNT(*) FROM ainxt.hod_allocation_ledger")
                ).fetchone()
                ledger_deleted = -1  # sentinel: we don't know the pre-count
                remaining = int(row[0] or 0) if row else 0
                if remaining != 0:
                    logger.warning(
                        "budget_audit_service: hod_allocation_ledger still has %d rows after wipe",
                        remaining,
                    )
            finally:
                count_db.close()
        except Exception:
            pass
        logger.info(
            "budget_audit_service: hod_allocation_ledger truncated at end of period=%s",
            period_yyyymm,
        )
    except Exception:
        logger.error(
            "budget_audit_service: failed to wipe hod_allocation_ledger period=%s",
            period_yyyymm,
        )

    # ── Clear the endpoint-spend KV counters ──────────────────────────────────
    #
    # services/endpoint_budget_governor caches each HOD's running endpoint spend
    # in KV (endpointspend:*) as a hot-read fast path, plus in-flight
    # reservations (epinflight:*). The ledger DELETE above does NOT touch those,
    # so a stale cached total would survive into the new period and immediately
    # gate it — exactly the bug store/budget_store.reset_usage avoids by deleting
    # usage:{uid}:total alongside the Postgres reset.
    endpoint_cache_cleared = 0
    try:
        from services.endpoint_budget_governor import clear_endpoint_spend_cache
        endpoint_cache_cleared = clear_endpoint_spend_cache()
    except Exception:
        logger.error(
            "budget_audit_service: failed to clear endpoint spend cache period=%s "
            "— endpoint cloud budgets may read stale totals until the TTL expires",
            period_yyyymm,
        )

    summary = {
        "period":         period_yyyymm,
        "processed":      processed,
        "snapshotted":    snapshotted,
        "reset":          snapshotted,   # reset count == snapshotted (gated together)
        "skipped":        skipped,
        "errors":         errors,
        "ledger_wiped":   True,
        "endpoint_spend_closing":  endpoint_spend_snapshot,
        "endpoint_cache_cleared":  endpoint_cache_cleared,
    }
    logger.info("budget_audit_service: completed %s", type(summary).__name__)
    return summary


# ============================================================
# EMAIL NOTIFICATIONS  —  pre-reset warning + post-reset confirmation
#
# Reuses services.smtp_service.send_html_email (the AiNxt internal relay)
# and honors UserNotificationPreference.monthly_statement_enabled as the
# opt-out signal (this codebase has no budget-specific preference column).
#
# All senders are best-effort: they catch every exception and return a
# string outcome — they NEVER raise. A failed email must never affect the
# reset transaction.
#
# Testing: set BUDGET_RESET_EMAIL_TEST_OVERRIDE=<addr> to route every
# email to a single inbox regardless of recipient.
# ============================================================


def _resolve_recipient(db, user_id: str) -> tuple[Optional[str], bool]:
    """
    Return (recipient_email, opted_out).

    Honors:
      * UserNotificationPreference.email_override if set
      * UserNotificationPreference.monthly_statement_enabled as opt-out flag
        (no budget-specific column exists in this codebase yet)

    If BUDGET_RESET_EMAIL_TEST_OVERRIDE is set, returns that address with
    opted_out=False unconditionally (for testing).
    """
    if _EMAIL_TEST_OVERRIDE:
        return _EMAIL_TEST_OVERRIDE, False

    pref = (
        db.query(UserNotificationPreference)
          .filter(UserNotificationPreference.user_id == user_id)
          .first()
    )
    if pref is not None and pref.monthly_statement_enabled is False:
        return None, True
    if pref is not None and pref.email_override:
        return pref.email_override, False

    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.email:
        return None, False
    return user.email, False


def _fmt_usd(value: Any) -> str:
    """Render a Decimal/float as $N.NN — safe for HTML."""
    try:
        return f"${float(value):,.2f}"
    except Exception:
        return "$0.00"


def _render_pre_reset_html(
    name: str,
    period_closing: str,
    current_limit: Decimal,
    cost_used: Decimal,
    unutilized: Decimal,
    default_limit: Decimal,
) -> tuple[str, str]:
    """Return (html_body, text_body) for the warning email."""
    safe_name   = _html.escape(name or "User")
    safe_period = _html.escape(period_closing)
    html_body = f"""
    <html><body style="font-family:Segoe UI,Arial,sans-serif;color:#222;">
      <h2 style="color:#b35900;">Your AiNxt monthly budget resets tomorrow</h2>
      <p>Hi {safe_name},</p>
      <p>This is a reminder that your monthly budget for <b>{type(safe_period).__name__}</b>
         will be reset within the next ~24 hours.</p>
      <table cellpadding="6" style="border-collapse:collapse;">
        <tr><td><b>Current monthly limit</b></td><td>{_fmt_usd(current_limit)}</td></tr>
        <tr><td><b>Used so far this month</b></td><td>{_fmt_usd(cost_used)}</td></tr>
        <tr><td><b>Unutilized (will not carry over)</b></td><td>{_fmt_usd(unutilized)}</td></tr>
        <tr><td><b>Next month's limit (band default)</b></td><td>{_fmt_usd(default_limit)}</td></tr>
      </table>
      <p style="margin-top:16px;">After the reset:
        <ul>
          <li>Your base budget allocation returns to the platform default of $50.</li>
          <li>Your token / request / cost counters will be zeroed.</li>
          <li>Any HOD-approved top-ups (extra budget) from this month will NOT carry forward.</li>
          <li>If you hold a <b>10x Winner</b> extra budget, whatever you haven't spent
              <b>does carry over</b> to next month. It is drawn on only after your $50 base
              is used, and any HOD top-ups are consumed before it — so nothing needs to be
              re-applied.</li>
        </ul>
      </p>
      <p style="color:#888;font-size:12px;">— AiNxt Platform</p>
    </body></html>
    """
    text_body = (
        f"Hi {name or 'User'},\n\n"
        f"Your AiNxt monthly budget for {type(period_closing).__name__} resets in ~24 hours.\n\n"
        f"  Current limit       : {_fmt_usd(current_limit)}\n"
        f"  Used so far         : {_fmt_usd(cost_used)}\n"
        f"  Unutilized          : {_fmt_usd(unutilized)}\n"
        f"  Next-month default  : {_fmt_usd(default_limit)}\n\n"
        f"After the reset your counters will be zeroed, your base returns to $50,\n"
        f"and any HOD-approved top-ups (extra budget) will NOT carry forward.\n"
        f"If you hold a 10x Winner extra budget, whatever you haven't spent DOES\n"
        f"carry over to next month — it is drawn on only after your $50 base is\n"
        f"used, and HOD top-ups are consumed before it, so nothing needs to be\n"
        f"re-applied.\n\n"
        f"— AiNxt Platform\n"
    )
    return html_body, text_body


def _render_post_reset_html(
    name: str,
    period_closed: str,
    closing_limit: Decimal,
    cost_used: Decimal,
    unutilized: Decimal,
    increase_count: int,
    reset_to: Decimal,
) -> tuple[str, str]:
    """Return (html_body, text_body) for the confirmation email."""
    safe_name   = _html.escape(name or "User")
    safe_period = _html.escape(period_closed)

    # Compute the "valid till" date — last day of the next calendar month,
    # since the new monthly limit applies to the month AFTER period_closed.
    try:
        y, m = period_closed.split("-")
        year, month = int(y), int(m)
        # Advance one month
        if month == 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1
        last_day = calendar.monthrange(next_year, next_month)[1]
        valid_till = f"{next_year:04d}-{next_month:02d}-{last_day:02d}"
    except Exception:
        valid_till = "end of next month"
    safe_valid_till = _html.escape(valid_till)

    html_body = f"""
      <html><body style="font-family:Segoe UI,Arial,sans-serif;color:#222;">
        <h2 style="color:#1b6e1b;">Your AiNxt monthly budget has been reset</h2>
        <p>Hi {safe_name},</p>
        <p>Your budget for <b>{type(safe_period).__name__}</b> has been closed and your new
           monthly allocation is active.</p>
        <table cellpadding="6" style="border-collapse:collapse;">
          <tr><td><b>Closing limit (end of {safe_period})</b></td><td>{_fmt_usd(closing_limit)}</td></tr>
          <tr><td><b>Total spent</b></td><td>{_fmt_usd(cost_used)}</td></tr>
          <tr><td><b>Unutilized</b></td><td>{_fmt_usd(unutilized)}</td></tr>
          <tr><td><b>HOD top-ups received</b></td><td>{int(increase_count)}</td></tr>
          <tr>
            <td>
              <b>New monthly limit</b><br>
              <span style="color:#666;font-size:12px;">Valid till {safe_valid_till} (this month only)</span>
            </td>
            <td><b>{_fmt_usd(reset_to)}</b></td>
          </tr>
        </table>
        <p style="margin-top:16px;color:#555;font-size:12px;">
          Every user has been reset to a $50 base allocation, and any HOD-approved extra
          budget from last month has been cleared. Unspent <b>10x Winner</b> extra budget
          is the exception — it carries over and is included in your new limit above.
          Nothing needs to be re-applied.
        </p>
        <p style="margin-top:16px;">This is an automated message — do not reply to this email.</p>
        <p style="color:#888;font-size:12px;">— AiNxt Enterprise Platform</p>
      </body></html>
      """
    text_body = (
        f"Hi {name or 'User'},\n\n"
        f"Your AiNxt monthly budget for {type(period_closed).__name__} has been reset.\n\n"
        f"  Closing limit        : {_fmt_usd(closing_limit)}\n"
        f"  Total spent          : {_fmt_usd(cost_used)}\n"
        f"  Unutilized           : {_fmt_usd(unutilized)}\n"
        f"  HOD top-ups received : {int(increase_count)}\n"
        f"  New monthly limit    : {_fmt_usd(reset_to)} (valid till {type(valid_till).__name__}, this month only)\n\n"
        f"Every user has been reset to a $50 base allocation, and any HOD-approved extra\n"
               f"budget from last month has been cleared. Unspent 10x Winner extra budget is the\n"
               f"exception — it carries over and is included in your new limit above. Nothing\n"
               f"needs to be re-applied.\n\n"
               f"— AiNxt Platform\n"
    )
    return html_body, text_body


def _send_pre_reset_warning(user_id: str, period_closing_yyyymm: str) -> str:
    """
    Build snapshot-of-current-state and send the warning email.

    Returns: 'sent' | 'skipped_optout' | 'skipped_no_email' | 'error'.
    Never raises.
    """
    db = SessionLocal()
    try:
        recipient, opted_out = _resolve_recipient(db, user_id)
        if opted_out:
            return "skipped_optout"
        if not recipient:
            return "skipped_no_email"

        cfg = db.query(BudgetConfig).filter(BudgetConfig.user_id == user_id).first()
        if cfg is None:
            return "skipped_no_email"
        user = db.query(User).filter(User.id == user_id).first()
        usage = (
            db.query(UserUsageTotal)
              .filter(UserUsageTotal.user_id == user_id)
              .first()
        )

        current_limit = _to_decimal(cfg.monthly_limit_usd)
        cost_used     = _to_decimal(usage.cost_usd_spent) if usage else _ZERO
        unutilized    = current_limit - cost_used
        if unutilized < _ZERO:
            unutilized = _ZERO

        ad_level = user.ad_level if (user and user.ad_level is not None) else 6
        default_limit = _to_decimal(_get_band_allocation(ad_level))

        html_body, text_body = _render_pre_reset_html(
            (user.name if user else None) or (user.email if user else None) or "User",
            period_closing_yyyymm,
            current_limit, cost_used, unutilized, default_limit,
        )

        ok = send_html_email(
            to=[recipient],
            subject=f"AiNxt - Your monthly budget resets in 24 hours ({type(period_closing_yyyymm).__name__})",
            html_body=html_body,
            text_body=text_body,
        )
        if ok:
            logger.info(
                "budget_audit_service: warning email sent user=%s period=%s to=%s",
                user_id, period_closing_yyyymm, recipient,
            )
            return "sent"
        return "error"
    except Exception:
        logger.warning(
            "budget_audit_service: warning email failed user=%s period=%s",
            user_id, period_closing_yyyymm,
        )
        return "error"
    finally:
        db.close()


def _send_post_reset_confirmation(user_id: str, period_closed_yyyymm: str) -> str:
    """
    Read the just-written BudgetPeriodAudit row and send confirmation.

    Returns: 'sent' | 'skipped_optout' | 'skipped_no_email' | 'error'.
    Never raises.
    """
    db = SessionLocal()
    try:
        recipient, opted_out = _resolve_recipient(db, user_id)
        if opted_out:
            return "skipped_optout"
        if not recipient:
            return "skipped_no_email"

        audit = (
            db.query(BudgetPeriodAudit)
              .filter(BudgetPeriodAudit.user_id == user_id)
              .filter(BudgetPeriodAudit.period_yyyymm == period_closed_yyyymm)
              .first()
        )
        if audit is None:
            # Shouldn't happen — we just wrote it. Treat as error so it shows
            # up in the summary count, but don't raise.
            return "error"

        user = db.query(User).filter(User.id == user_id).first()
        html_body, text_body = _render_post_reset_html(
            (user.name if user else None) or (user.email if user else None) or "User",
            period_closed_yyyymm,
            _to_decimal(audit.closing_limit_usd),
            _to_decimal(audit.cost_used_usd),
            _to_decimal(audit.unutilized_usd),
            int(audit.increase_count or 0),
            _to_decimal(audit.reset_to_usd),
        )

        ok = send_html_email(
            to=[recipient],
            subject=f"AiNxt - Your monthly budget has been reset ({type(period_closed_yyyymm).__name__})",
            html_body=html_body,
            text_body=text_body,
        )
        if ok:
            logger.info(
                "budget_audit_service: confirmation email sent user=%s period=%s to=%s",
                user_id, period_closed_yyyymm, recipient,
            )
            return "sent"
        return "error"
    except Exception:
        logger.warning(
            "budget_audit_service: confirmation email failed user=%s period=%s",
            user_id, period_closed_yyyymm,
        )
        return "error"
    finally:
        db.close()


def send_pre_reset_warnings_for_all(
    period_closing_yyyymm: str,
    only_user_id: Optional[str] = None,
) -> dict:
    """
    Send the pre-reset warning email to every user with a BudgetConfig row
    (or to just `only_user_id` if supplied — used by the admin endpoint
    for dry-run testing).

    Read-only with respect to the DB; pure side effect is outbound email.

    Returns: {"period", "processed", "sent", "skipped_optout",
              "skipped_no_email", "errors"}.

    Caller is responsible for the BUDGET_MONTHLY_RESET_ENABLED check.
    """
    if not period_closing_yyyymm or len(period_closing_yyyymm) != 7 or period_closing_yyyymm[4] != "-":
        raise ValueError(
            f"period_closing_yyyymm must be 'YYYY-MM', got {period_closing_yyyymm!r}"
        )

    logger.info(
        "budget_audit_service: starting send_pre_reset_warnings_for_all period=%s only_user=%s",
        period_closing_yyyymm, only_user_id,
    )

    enum_db = SessionLocal()
    try:
        q = enum_db.query(BudgetConfig.user_id).filter(BudgetConfig.user_id.isnot(None))
        if only_user_id:
            q = q.filter(BudgetConfig.user_id == only_user_id)
        user_ids = [row.user_id for row in q.all()]
    finally:
        enum_db.close()

    processed = sent = skipped_optout = skipped_no_email = errors = 0
    for uid in user_ids:
        processed += 1
        outcome = _send_pre_reset_warning(uid, period_closing_yyyymm)
        if outcome == "sent":
            sent += 1
        elif outcome == "skipped_optout":
            skipped_optout += 1
        elif outcome == "skipped_no_email":
            skipped_no_email += 1
        else:
            errors += 1

    summary = {
        "period":           period_closing_yyyymm,
        "processed":        processed,
        "sent":             sent,
        "skipped_optout":   skipped_optout,
        "skipped_no_email": skipped_no_email,
        "errors":           errors,
    }
    logger.info("budget_audit_service: warning batch completed %s", type(summary).__name__)
    return summary
