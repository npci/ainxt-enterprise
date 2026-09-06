# SPDX-License-Identifier: MIT
# ============================================================
# services.llm_spend.backfill
#
# One-off historical backfill: re-fetches OpenAI and Anthropic spend with
# the token_type itemisation and replaces
# the old blended rows for those two providers.
#
# WHY THIS EXISTS — the double-count trap
#   The token_type migration rekeys the unique constraint to
#   (usage_date, provider, model, source, token_type). A pre-migration
#   blended row has token_type='blended', which is a DIFFERENT key from the
#   itemised rows a fresh fetch writes (token_type='uncached' etc). UPSERT
#   therefore CANNOT replace the old row — it inserts alongside it — so a
#   naive re-fetch after the migration would double the reported cost for
#   every backfilled day:
#
#     2026-07-28 anthropic opus-4-7 <src> blended     cost_usd=185.25  (survives)
#     2026-07-28 anthropic opus-4-7 <src> uncached     cost_usd=  1.99
#     2026-07-28 anthropic opus-4-7 <src> cache_read   cost_usd= 89.94
#     2026-07-28 anthropic opus-4-7 <src> cache_write  cost_usd= 65.19
#     2026-07-28 anthropic opus-4-7 <src> output       cost_usd= 28.13
#                                                  SUM = 370.50  <- DOUBLE
#
#   So the old blended rows for a (provider, source, window) MUST be deleted
#   in the SAME transaction as the new itemised rows are inserted. Never as
#   two separate statements/commits — a crash between them would leave the
#   window either fully blended (safe, just not yet itemised) or fully
#   doubled (wrong), and only the atomic version guarantees the former.
#
# SCOPE — why this is per-provider, and why Gemini is NOT here
#   Each provider gets its OWN transaction. A failure backfilling Anthropic
#   must not touch OpenAI's already-committed itemised rows, and vice versa.
#
#   Gemini is deliberately excluded.
#
# USAGE
#   From a Python shell / one-off script on the host with LLM_PROXY_URL set:
#
#       from services.llm_spend import backfill
#       backfill.run_openai(date(2026, 4, 30), date(2026, 7, 29))
#       backfill.run_anthropic(date(2026, 5, 4), date(2026, 7, 29))
#
#   Each call is idempotent — safe to re-run if it fails partway (the
#   DELETE+INSERT is atomic per call, so a re-run just redoes the same
#   window cleanly). Runs entirely against whatever LLM_PROXY_URL points at
#   — there is no environment gate here, by
#   design: the caller is responsible for pointing this at the right place.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List

from sqlalchemy import text

from core.logger import logger
from db.database import SessionLocal
from services.llm_spend.fetchers._common import (
    SpendRow, upsert_rows_in_session, TOKEN_TYPE_BLENDED,
)
from services.llm_spend.fetchers import anthropic_admin, openai_costs


@dataclass
class BackfillResult:
    provider:      str
    source:        str
    window_start:  date
    window_end:    date
    legacy_deleted: int
    rows_inserted:  int
    status:         str          # 'ok' | 'failed'
    error_text:     str = ""


# Deletes ONLY the legacy blended rows for this exact (provider, source,
# window) — never a blanket delete. This is what keeps Gemini's CSV-sourced
# rows (different source string) and any out-of-window history untouched
# even if this module is ever called with a wider date range by mistake.
_DELETE_LEGACY_SQL = text(
    """
    DELETE FROM ainxt.llm_spend_daily
    WHERE provider   = :provider
      AND source     = :source
      AND token_type = :token_type
      AND usage_date >= :window_start
      AND usage_date <= :window_end
    """
)


def _run_backfill(
    provider: str,
    source: str,
    window_start: date,
    window_end: date,
    compute_rows_fn,
) -> BackfillResult:
    """Shared atomic DELETE-legacy-then-INSERT-itemised for one provider.

    `compute_rows_fn` is the provider fetcher's compute_rows(ws, we) — it
    does the actual provider API calls BEFORE the transaction opens, so a
    slow upstream response never holds a DB transaction open. Only the
    DELETE and the INSERT run inside the transaction.
    """
    logger.info(
        f"[llm_spend.backfill] {provider}: computing itemised rows for "
        f"{window_start}..{window_end} (source={source})"
    )
    try:
        rows: List[SpendRow] = compute_rows_fn(window_start, window_end)
    except Exception as e:
        logger.error(f"[llm_spend.backfill] {provider}: fetch failed: {e}")
        return BackfillResult(
            provider=provider, source=source,
            window_start=window_start, window_end=window_end,
            legacy_deleted=0, rows_inserted=0,
            status="failed", error_text=str(e)[:500],
        )

    if not rows:
        logger.warning(
            f"[llm_spend.backfill] {provider}: fetch returned ZERO rows for "
            f"{window_start}..{window_end} — aborting without touching "
            f"legacy data (an empty provider response should never wipe "
            f"existing history)"
        )
        return BackfillResult(
            provider=provider, source=source,
            window_start=window_start, window_end=window_end,
            legacy_deleted=0, rows_inserted=0,
            status="failed", error_text="fetch returned zero rows; refusing to delete legacy data",
        )

    try:
        with SessionLocal() as session:
            deleted = session.execute(
                _DELETE_LEGACY_SQL,
                {
                    "provider": provider,
                    "source": source,
                    "token_type": TOKEN_TYPE_BLENDED,
                    "window_start": window_start,
                    "window_end": window_end,
                },
            ).rowcount or 0

            inserted = upsert_rows_in_session(session, rows, source=source)

            session.commit()
    except Exception as e:
        logger.error(f"[llm_spend.backfill] {provider}: transaction failed, rolled back: {e}")
        return BackfillResult(
            provider=provider, source=source,
            window_start=window_start, window_end=window_end,
            legacy_deleted=0, rows_inserted=0,
            status="failed", error_text=str(e)[:500],
        )

    logger.info(
        f"[llm_spend.backfill] {provider}: deleted {deleted} legacy blended "
        f"rows, inserted/updated {inserted} itemised rows for "
        f"{window_start}..{window_end}"
    )
    return BackfillResult(
        provider=provider, source=source,
        window_start=window_start, window_end=window_end,
        legacy_deleted=deleted, rows_inserted=inserted,
        status="ok",
    )


def run_openai(window_start: date, window_end: date) -> BackfillResult:
    """Backfill OpenAI spend with token_type itemisation.

    Default full-history window: source has rows from 2026-04-30 (per prod
    `SELECT min(usage_date) FROM llm_spend_daily WHERE source='openai_costs_api'`)
    through whatever the caller passes as window_end.
    """
    return _run_backfill(
        provider="openai",
        source=openai_costs.SOURCE,
        window_start=window_start,
        window_end=window_end,
        compute_rows_fn=openai_costs.compute_rows,
    )


def run_anthropic(window_start: date, window_end: date) -> BackfillResult:
    """Backfill Anthropic spend with token_type itemisation.

    Default full-history window: source has rows from 2026-05-04 (per prod
    `SELECT min(usage_date) FROM llm_spend_daily WHERE source='anthropic_admin'`)
    through whatever the caller passes as window_end.
    """
    return _run_backfill(
        provider="anthropic",
        source=anthropic_admin.SOURCE,
        window_start=window_start,
        window_end=window_end,
        compute_rows_fn=anthropic_admin.compute_rows,
    )


def run_gemini(window_start: date, window_end: date) -> BackfillResult:
    """Documented no-op — Gemini is NOT backfilled by this module.

    Those rows remain token_type='blended' until a per-token-type
    Gemini export lands.
    Calling this raises rather than silently doing nothing, so an automation
    script that loops over all three providers fails loudly instead of
    quietly skipping Gemini.
    """
    raise NotImplementedError(
        "Gemini is intentionally excluded from the token_type backfill — "
        "its spend is CSV-sourced (source='gcp_csv_backfill') with no API "
        "to re-fetch an itemised breakdown from. See module docstring."
    )


def run_all(openai_start: date, anthropic_start: date, window_end: date) -> List[BackfillResult]:
    """Convenience wrapper: backfill OpenAI then Anthropic, each in its own
    transaction, continuing to the next provider even if one fails.

    Does NOT touch Gemini — see run_gemini().
    """
    results: List[BackfillResult] = []

    r = run_openai(openai_start, window_end)
    results.append(r)
    if r.status != "ok":
        logger.error(
            f"[llm_spend.backfill] openai backfill FAILED: {r.error_text} "
            f"— continuing to anthropic anyway (independent transactions)"
        )

    r = run_anthropic(anthropic_start, window_end)
    results.append(r)
    if r.status != "ok":
        logger.error(f"[llm_spend.backfill] anthropic backfill FAILED: {r.error_text}")

    return results
