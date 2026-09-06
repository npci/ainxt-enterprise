# SPDX-License-Identifier: MIT
# ============================================================
# ENDPOINT BUDGET GOVERNOR — HOD-funded cloud spend for managed endpoints
#
# Managed endpoints (/ainxt/v1/api/{slug}/v1/chat/completions) may serve CLOUD
# models (GPT / Claude / Gemini). Cloud inference costs money, so every such
# call is funded by a HOD's monthly cap and refused once that cap is exhausted.
#
# WHY THIS IS A SEPARATE MODULE FROM services/hod_budget_governor.py
#   That module governs ALLOCATIONS ('allocate' / 'approve_request' /
#   'sdlc_run'): it INSERTs append-only rows and maintains the
#   MAX(consumed_after_usd) running-total invariant. Endpoint spend is
#   CONSUMPTION — one row per (HOD, period) that is UPDATEd in place. Routing it
#   through reserve_and_record() would insert a row per HTTP request and corrupt
#   that invariant.
#
# WHERE THE NUMBERS LIVE
#   ainxt.hod_allocation_caps.monthly_cap_usd            — the cap (DBA-owned)
#   ainxt.hod_allocation_ledger.consumed_after_usd       — approved allocations
#   ainxt.hod_allocation_ledger.endpoint_spend_usd       — endpoint cloud spend
#       (action='endpoint_spend', ONE carrier row per hod+period, NUMERIC(12,6))
#   ainxt.model_usages                                    — per-request audit detail
#   KV epinflight:{email}:{period}                         — in-flight reservations only
#
#   remaining = cap - (approved allocations + endpoint spend + in-flight)
#
#   No KV cache sits in front of endpoint_spend_usd: the gate already reads the
#   cap from Postgres in the same call, so caching only the spend side would save
#   no round-trip while adding a staleness window on a financial figure.
#
# WHY A LEDGER COLUMN AND NOT SUM(model_usages.cost_usd)
#   model_usages is hash-partitioned into 128 partitions with NO index on
#   `endpoint` or `created_at`, so a per-request SUM would seq-scan 500k+ rows
#   across every partition. The ledger column is an O(1) read and, being an
#   atomic INSERT ... ON CONFLICT DO UPDATE, is race-free. model_usages stays
#   the per-request audit trail and the reconciliation source.
#
# IN-FLIGHT RESERVATIONS
#   The cap gate allows while remaining > 0 (matching the SDLC precedent). On its
#   own that means K concurrent requests all read the same `remaining` and all
#   pass, so overrun is K x per-request. A conservative estimate is therefore
#   reserved before the call and released after, which bounds the overrun to
#   roughly a single request.
#
# FEATURE FLAG
#   HOD_CAP_ENFORCEMENT_ENABLED=true|false (default false -> shadow mode).
#   Shadow mode NEVER blocks; it logs what would have been blocked and still
#   records spend, so the numbers can be validated before enforcing.
#
# FAIL BEHAVIOUR
#   Cloud requests FAIL CLOSED when enforcement is on and the cap/spend lookup
#   errors — serving unbounded paid inference is worse than a 503. This is the
#   opposite of hod_budget_governor.get_cap_status(), which fails soft because
#   it only feeds a UI banner.
# ============================================================

from __future__ import annotations

import os
import time
import uuid
from decimal import Decimal
from typing import Optional, Tuple

from sqlalchemy import text

from core.config import RDB_BUDGET
from core.kv import get_kv
from core.logger import logger, mask_email
from db.database import SessionLocal
from services.hod_budget_governor import (
    _current_period,
    _default_cap_usd,
    _enforcement_enabled,
    _first_of_next_month,
    _money,
)

# ── Constants ────────────────────────────────────────────────────────────────

# Ledger action for the endpoint-spend carrier row. `action` is VARCHAR(32) with
# no CHECK constraint, so this value needed no DDL. Registered in
# hod_budget_governor._VALID_ACTIONS so audit tooling recognises it.
ACTION_ENDPOINT_SPEND = "endpoint_spend"

# Nil UUID — hod_allocation_ledger.target_user_id is NOT NULL but an endpoint's
# system_user_id is nullable. Same coercion as sdlc_budget_tracker.py.
_NIL_UUID = "00000000-0000-0000-0000-000000000000"

_INFLIGHT_TTL      = 300         # seconds — orphaned reservation self-heals in 5 min
_HOD_CACHE_TTL     = 60          # seconds — endpoint -> HOD mapping cache

_ZERO = Decimal("0.00")


# ── KV (DB 4 — same financial fast-path DB as store/budget_store) ────────────

_kv = None


def _get_kv():
    """Cached KV client for the budget DB. Returns None when unavailable."""
    global _kv
    if _kv is None:
        try:
            c = get_kv(RDB_BUDGET, decode_responses=True)
            c.ping()
            _kv = c
        except Exception as exc:
            logger.warning("endpoint_budget: KV unavailable → %s", exc)
            return None
    return _kv


def _inflight_key(hod_email_lc: str, period: str) -> str:
    return f"epinflight:{hod_email_lc}:{period}"


# ── Endpoint → HOD resolution ────────────────────────────────────────────────

def resolve_endpoint_hod(endpoint_id: str) -> Optional[str]:
    """
    Return the lowercased funding HOD email for an endpoint, or None.

    Cached in KV for 60s — this runs on every cloud request. Invalidated
    explicitly by endpoint_mgmt_router on any write to the mapping.
    """
    if not endpoint_id:
        return None

    cache_key = f"ep:hod:{endpoint_id}"
    kv = _get_kv()
    if kv:
        try:
            cached = kv.get(cache_key)
            if cached is not None:
                # "" is a cached negative result — avoids re-querying for
                # local-only endpoints on every single request.
                return cached or None
        except Exception:
            pass

    hod = None
    try:
        db = SessionLocal()
        try:
            row = db.execute(
                text(
                    'SELECT "hod_email" FROM ainxt.endpoint_hod_mapping '
                    'WHERE "endpoint_id" = :eid AND "is_active" = TRUE'
                ),
                {"eid": str(endpoint_id)},
            ).first()
            if row and row[0]:
                hod = str(row[0]).strip().lower() or None
        finally:
            db.close()
    except Exception as exc:
        logger.warning("endpoint_budget: HOD lookup failed for endpoint=%s: %s",
                       endpoint_id, exc)
        return None   # caller decides; cloud path treats None as "no owner"

    if kv:
        try:
            kv.set(cache_key, hod or "", ex=_HOD_CACHE_TTL)
        except Exception:
            pass
    return hod


def invalidate_endpoint_hod_cache(endpoint_id: str) -> None:
    """Drop the cached endpoint→HOD entry. Called on admin writes."""
    kv = _get_kv()
    if not kv or not endpoint_id:
        return
    try:
        kv.delete(f"ep:hod:{endpoint_id}")
    except Exception:
        pass


# ── Spend reads ──────────────────────────────────────────────────────────────

def _fetch_endpoint_spend_db(db, hod_email_lc: str, period: str) -> Decimal:
    """Read the carrier row's running total. O(1) via uq_hal_endpoint_spend_period."""
    row = db.execute(
        text(
            'SELECT COALESCE("endpoint_spend_usd", 0) '
            'FROM ainxt.hod_allocation_ledger '
            'WHERE lower("hod_email") = :e '
            '  AND "period_yyyymm" = :p '
            "  AND \"action\" = 'endpoint_spend'"
        ),
        {"e": hod_email_lc, "p": period},
    ).scalar()
    return Decimal(str(row or 0))


def get_endpoint_spend(hod_email: str, period: Optional[str] = None) -> Decimal:
    """
    Running endpoint cloud spend for this HOD/period. Public accessor for
    reporting and verification.

    Reads the ledger directly — O(1) via uq_hal_endpoint_spend_period. There is
    deliberately NO KV cache here: the gate must read the cap and allocation
    consumption from Postgres in the same session anyway, so caching only this
    one value would save no round-trip while adding a staleness window on a
    financial figure.
    """
    if not hod_email:
        return _ZERO
    try:
        db = SessionLocal()
        try:
            return _fetch_endpoint_spend_db(db, hod_email.lower(),
                                            period or _current_period())
        finally:
            db.close()
    except Exception as exc:
        logger.warning("endpoint_budget: spend read failed for %s: %s",
                       hod_email, exc)
        raise


def _fetch_allocation_consumption(db, hod_email_lc: str, period: str) -> Decimal:
    """
    Approved-allocation consumption, EXCLUDING endpoint_spend carrier rows.

    Mirrors hod_budget_governor._fetch_consumption but filters the carrier row
    out, since its consumed_after_usd is NULL and must not pollute the MAX().
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


def get_inflight(hod_email: str, period: Optional[str] = None) -> Decimal:
    """Currently reserved (in-flight) spend. Best-effort — 0 if KV is down."""
    kv = _get_kv()
    if not kv or not hod_email:
        return _ZERO
    try:
        raw = kv.get(_inflight_key(hod_email.lower(), period or _current_period()))
        val = Decimal(str(raw)) if raw is not None else _ZERO
        # Guard against a negative drift from unbalanced release calls.
        return val if val > 0 else _ZERO
    except Exception:
        return _ZERO


# ── The gate ─────────────────────────────────────────────────────────────────

def check_endpoint_budget(
        hod_email: Optional[str],
        period: Optional[str] = None,
) -> Tuple[bool, str, dict]:
    """
    Decide whether a cloud request may proceed.

    Returns (allowed, reason, status) where status carries cap/consumed/
    remaining/period/resets_on for logging and response headers.

    Allows while remaining > 0 (SDLC-consistent). In-flight reservations are
    included in `consumed`, so concurrent callers see each other's pending spend.

    Shadow mode never blocks. Enforcement mode fails CLOSED on lookup error.
    """
    period = period or _current_period()
    shadow = not _enforcement_enabled()

    status = {
        "cap_usd":       0.0,
        "consumed_usd":  0.0,
        "remaining_usd": 0.0,
        "period_yyyymm": period,
        "resets_on":     _first_of_next_month().isoformat(),
        "shadow_mode":   shadow,
        "hod_email":     hod_email or "",
    }

    if not hod_email:
        # No funding owner. Cloud is not permitted — but in shadow mode we only
        # warn, so enabling the feature can never break live traffic.
        if shadow:
            logger.warning(
                "endpoint_budget: no HOD mapped (shadow mode — allowing). period=%s", period
            )
            return True, "", status
        return False, (
            "This endpoint has no budget owner configured. An admin must assign a "
            "HOD before it can serve cloud models."
        ), status

    email_lc = hod_email.lower()

    try:
        db = SessionLocal()
        try:
            cap_row = db.execute(
                text(
                    'SELECT "monthly_cap_usd", "is_active" '
                    'FROM ainxt.hod_allocation_caps '
                    'WHERE lower("hod_email") = :e'
                ),
                {"e": email_lc},
            ).first()
            if cap_row is None or not bool(cap_row[1]):
                cap = _default_cap_usd()      # no/inactive row → global default
            else:
                cap = _money(cap_row[0])

            allocated = _fetch_allocation_consumption(db, email_lc, period)
            spent     = _fetch_endpoint_spend_db(db, email_lc, period)
        finally:
            db.close()
    except Exception as exc:
        logger.error("endpoint_budget: cap lookup failed for %s: %s", mask_email(email_lc), exc)
        if shadow:
            return True, "", status
        # Fail CLOSED — never serve unbounded paid inference on an unknown cap.
        return False, (
            "Budget verification is temporarily unavailable. Cloud models are "
            "unavailable until it recovers."
        ), status

    inflight  = get_inflight(email_lc, period)
    consumed  = allocated + spent + inflight
    remaining = cap - consumed

    status.update({
        "cap_usd":       float(cap),
        "consumed_usd":  float(consumed),
        "remaining_usd": float(remaining if remaining > 0 else _ZERO),
    })

    if remaining > 0:
        return True, "", status

    msg = (
        f"HOD monthly budget exhausted for cloud models: "
        f"${float(consumed):.2f} used of ${float(cap):.2f}. "
        f"Resets on {status['resets_on']}."
    )
    if shadow:
        logger.warning("endpoint_budget: WOULD BLOCK (shadow mode) hod=%s %s", mask_email(email_lc), msg)
        return True, "", status

    logger.info("endpoint_budget: BLOCKED hod=%s period=%s consumed=%.6f cap=%.2f",
                email_lc, period, float(consumed), float(cap))
    return False, msg, status


# ── In-flight reservations ───────────────────────────────────────────────────

def reserve_inflight(hod_email: str, amount, period: Optional[str] = None) -> Optional[str]:
    """
    Reserve an estimated amount so concurrent requests see it as consumed.

    Returns an opaque token to pass to release_inflight(), or None if nothing was
    reserved (KV down / no HOD / non-positive amount). Best-effort by design: a
    KV outage degrades concurrency protection but must not fail the request.
    The key carries a TTL so a crashed worker's reservation self-heals.

    The token embeds the PERIOD the reservation was made against. A request that
    straddles UTC midnight on the 1st of the month would otherwise be released
    against the new period — leaking the old counter and wrongly decrementing the
    new one.
    """
    amt = _money(amount)
    if not hod_email or amt <= 0:
        return None
    kv = _get_kv()
    if not kv:
        return None

    per = period or _current_period()
    key = _inflight_key(hod_email.lower(), per)
    try:
        kv.incrbyfloat(key, float(amt))
        kv.expire(key, _INFLIGHT_TTL)
        return f"{amt}|{per}|{uuid.uuid4().hex[:8]}"
    except Exception as exc:
        logger.warning("endpoint_budget: reserve failed for %s: %s", mask_email(hod_email), exc)
        return None


def release_inflight(hod_email: str, token: Optional[str],
                     period: Optional[str] = None) -> None:
    """
    Release a reservation made by reserve_inflight(). Never raises.

    The amount and period are read back from the token so the release always
    targets the same counter the reservation incremented.
    """
    if not token or not hod_email:
        return
    kv = _get_kv()
    if not kv:
        return
    try:
        parts = token.split("|")
        amt = Decimal(parts[0])
        # Tokens are "amount|period|nonce"; tolerate the legacy "amount|nonce"
        # shape in case one is still in flight across a deploy.
        tok_period = parts[1] if len(parts) >= 3 else None
    except Exception:
        return

    key = _inflight_key(hod_email.lower(), tok_period or period or _current_period())
    try:
        newval = kv.incrbyfloat(key, -float(amt))
        # Do NOT clamp a negative back to 0 with kv.set(): this counter is SHARED
        # across every concurrent request for the same (hod, period), so writing
        # 0 would silently discard other in-flight reservations and collapse the
        # concurrency protection back to K x per-request overrun. A negative value
        # only arises from an already-expired/evicted key, and get_inflight()
        # already floors reads at 0, so the TTL heals it safely.
        if newval is not None and float(newval) < 0:
            logger.warning(
                "endpoint_budget: in-flight counter negative (%.6f) for %s — "
                "key likely expired mid-request; leaving TTL to heal it",
                float(newval), key,
            )
    except Exception as exc:
        logger.warning("endpoint_budget: release failed for %s: %s", mask_email(hod_email), exc)


# ── Spend recording ──────────────────────────────────────────────────────────

def record_endpoint_spend(
        hod_email: str,
        cost_usd,
        *,
        endpoint_slug: str = "",
        system_user_id: Optional[str] = None,
        period: Optional[str] = None,
) -> None:
    """
    Add `cost_usd` to this HOD's running endpoint spend for the period.

    Atomic: a single INSERT ... ON CONFLICT DO UPDATE against the partial unique
    index uq_hal_endpoint_spend_period, so concurrent requests cannot lose an
    increment to a read-modify-write race. Postgres is written FIRST (durable
    truth), then the KV cache is bumped.

    Never raises — billing must not turn a successful completion into a 500. A
    failure here is logged loudly because it means unbilled spend.
    """
    amt = Decimal(str(cost_usd or 0))
    if not hod_email or amt <= 0:
        return

    email_lc = hod_email.lower()
    period   = period or _current_period()
    target   = str(system_user_id) if system_user_id else _NIL_UUID
    shadow   = not _enforcement_enabled()

    try:
        db = SessionLocal()
        try:
            with db.begin():
                # created_at is NOT NULL with NO database default on this
                # DBA-owned table, so it must be supplied explicitly.
                db.execute(
                    text(
                        'INSERT INTO ainxt.hod_allocation_ledger ('
                        ' "id", "hod_email", "period_yyyymm", "target_user_id",'
                        ' "action", "amount_usd", "endpoint_spend_usd",'
                        ' "shadow_mode", "status", "justification", "created_at"'
                        ') VALUES ('
                        ' gen_random_uuid(), :e, :p, :tu,'
                        " 'endpoint_spend', 0, :amt,"
                        " :sh, 'approved', :just, NOW()"
                        ') ON CONFLICT (lower("hod_email"), "period_yyyymm") '
                        "  WHERE \"action\" = 'endpoint_spend' "
                        ' DO UPDATE SET '
                        '   "endpoint_spend_usd" = '
                        '       ainxt.hod_allocation_ledger."endpoint_spend_usd" '
                        '       + EXCLUDED."endpoint_spend_usd", '
                        # Refresh shadow_mode too. Without this the flag is frozen
                        # at whatever it was on the period's FIRST cloud request:
                        # if that happened in shadow mode, the row would stay
                        # shadow_mode=TRUE all month even after enforcement was
                        # switched on, and any report filtering shadow_mode=FALSE
                        # (the convention elsewhere in this table) would report the
                        # real spend as zero.
                        '   "shadow_mode" = EXCLUDED."shadow_mode"'
                    ),
                    {
                        "e":    email_lc,
                        "p":    period,
                        "tu":   target,
                        "amt":  amt,
                        "sh":   shadow,
                        "just": "Managed-endpoint cloud model spend (running total)",
                    },
                )
        finally:
            db.close()
    except Exception as exc:
        logger.error(
            "endpoint_budget: FAILED to record spend hod=%s period=%s cost=%.6f "
            "slug=%s — spend is UNBILLED: %s",
            email_lc, period, float(amt), endpoint_slug, exc,
        )
        return

    logger.info(
        "endpoint_spend hod=%s period=%s slug=%s cost=%.6f shadow=%s",
        email_lc, period, endpoint_slug, float(amt), shadow,
    )


# ── Cost estimation (for the in-flight reservation) ──────────────────────────

def estimate_request_cost(model: str, messages: list, max_tokens: Optional[int] = None):
    """
    Conservative pre-call cost estimate, used only to size the in-flight
    reservation. Deliberately over-estimates: under-reserving would let
    concurrent requests overshoot the cap, which is the failure mode we are
    guarding against.

    Input tokens use the platform-wide chars//4 heuristic (see gateway_ollama
    count_tokens / memory.chat_summarizer._count_tokens). Output is assumed to
    hit max_tokens, since that is the true worst case.
    """
    from services.endpoint_model_catalog import estimate_cost_usd  # local import

    text_len = 0
    for m in (messages or []):
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str):
            text_len += len(c)
        elif isinstance(c, list):
            # Multimodal parts — count only text segments.
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text_len += len(part["text"])

    in_tok  = max(1, text_len // 4)
    out_tok = int(max_tokens) if max_tokens else 4096   # assume a full response

    return estimate_cost_usd(model, in_tok, out_tok)


# ── Status for the admin UI ──────────────────────────────────────────────────

def get_endpoint_budget_status(hod_email: Optional[str]) -> dict:
    """
    Cap / consumed / remaining for an endpoint's funding HOD.

    Fails SOFT (zeros + has_cap_row=False) — this only feeds the admin UI banner
    and must never break the Endpoints screen.
    """
    period = _current_period()
    out = {
        "hod_email":       hod_email or "",
        "has_cap_row":     False,
        "cap_usd":         0.0,
        "allocated_usd":   0.0,
        "endpoint_spend_usd": 0.0,
        "consumed_usd":    0.0,
        "remaining_usd":   0.0,
        "period_yyyymm":   period,
        "resets_on":       _first_of_next_month().isoformat(),
        "enforcement":     _enforcement_enabled(),
    }
    if not hod_email:
        return out

    email_lc = hod_email.lower()
    try:
        db = SessionLocal()
        try:
            cap_row = db.execute(
                text(
                    'SELECT "monthly_cap_usd", "is_active" '
                    'FROM ainxt.hod_allocation_caps WHERE lower("hod_email") = :e'
                ),
                {"e": email_lc},
            ).first()
            has_row = cap_row is not None and bool(cap_row[1])
            cap     = _money(cap_row[0]) if has_row else _default_cap_usd()

            allocated = _fetch_allocation_consumption(db, email_lc, period)
            spent     = _fetch_endpoint_spend_db(db, email_lc, period)
        finally:
            db.close()
    except Exception as exc:
        logger.warning("endpoint_budget: status query failed for %s: %s", mask_email(email_lc), exc)
        return out

    consumed  = allocated + spent
    remaining = cap - consumed
    out.update({
        "has_cap_row":        has_row,
        "cap_usd":            float(cap),
        "allocated_usd":      float(allocated),
        "endpoint_spend_usd": float(spent),
        "consumed_usd":       float(consumed),
        "remaining_usd":      float(remaining if remaining > 0 else _ZERO),
    })
    return out


# ── Monthly reset support ────────────────────────────────────────────────────

def clear_endpoint_spend_cache() -> int:
    """
    Delete every leftover in-flight-reservation KV key (epinflight:*).

    Called by the monthly reset AFTER the ledger is wiped. Reservations already
    self-heal via their 5-minute TTL, so this exists only to guarantee the new
    period starts with zero pending reservations rather than waiting out that
    window. Returns the number of keys deleted (0 if KV is unavailable).

    Uses KEYS rather than a cursor scan — the KVClient abstraction (core/kv/base.py)
    does not expose a plain-key SCAN, only `keys()` and hash-scoped `hscan()`. This
    is a once-a-month admin operation over a small, TTL-bounded key set (never
    more than one key per in-flight request), so the KEYS scan cost is negligible
    despite running on the shared budget KV (DB 4).
    """
    kv = _get_kv()
    if not kv:
        return 0
    deleted = 0
    try:
        keys = kv.keys("epinflight:*")
        if keys:
            kv.delete(*keys)
            deleted = len(keys)
    except Exception as exc:
        logger.warning("endpoint_budget: cache clear failed: %s", exc)
    logger.info("endpoint_budget: cleared %d in-flight reservation KV keys", deleted)
    return deleted


def snapshot_endpoint_spend(period: str) -> dict:
    """
    {hod_email: spend} for a period — captured BEFORE the ledger wipe so the
    closed period's endpoint spend stays auditable after the rows are deleted.
    """
    out: dict = {}
    try:
        db = SessionLocal()
        try:
            rows = db.execute(
                text(
                    'SELECT lower("hod_email"), "endpoint_spend_usd" '
                    'FROM ainxt.hod_allocation_ledger '
                    "WHERE \"action\" = 'endpoint_spend' AND \"period_yyyymm\" = :p"
                ),
                {"p": period},
            ).fetchall()
            for r in rows:
                out[str(r[0])] = float(r[1] or 0)
        finally:
            db.close()
    except Exception as exc:
        logger.warning("endpoint_budget: snapshot failed for period=%s: %s", period, exc)
    return out
