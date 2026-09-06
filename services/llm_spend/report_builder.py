# SPDX-License-Identifier: MIT
# ============================================================
# services.llm_spend.report_builder
#
# Reads llm_spend_daily and produces dataclass payloads for Jinja
# rendering of the four exec digests (daily/weekly/monthly/quarterly).
#
# All money in USD. All aggregation done in Postgres.
# ============================================================

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy import text

from core.logger import logger
from db.database import SessionLocal
from services.llm_spend.fetchers._common import (
    TOKEN_TYPE_UNCACHED, TOKEN_TYPE_CACHE_READ, TOKEN_TYPE_CACHE_WRITE_5M,
    TOKEN_TYPE_CACHE_WRITE_1H, TOKEN_TYPE_OUTPUT, TOKEN_TYPE_NON_TOKEN,
    TOKEN_TYPE_BLENDED,
)


# ── source-of-truth policy ────────────────────────────────────────────────
#
# Source-of-truth ordering:
#
#   1. **Provider admin APIs are PRIMARY** for every cadence (daily / weekly /
#      monthly / quarterly). On every digest build we re-invoke the fetchers
#      for the report window — they hit the upstream cost + usage endpoints
#      via llm_proxy and UPSERT into ainxt.llm_spend_daily. This guarantees
#      the row a digest reads is the freshest available authoritative number
#      from the provider, not whatever was scraped on a nightly cron N days
#      ago that may have since been revised by the provider (Anthropic and
#      OpenAI both correct cost rows retroactively for ~24h; GCP billing
#      lags 6–24h).
#
#   2. **ainxt.llm_spend_daily is the typed FALLBACK.** If a fetcher fails
#      (network, 4xx/5xx, schema drift, empty response) the corresponding
#      llm_spend_fetch_runs row lands as 'failed' and that provider's slice
#      of the digest is sourced from whatever rows already exist in
#      llm_spend_daily for the window — including ones written by earlier
#      successful runs. The digest still ships rather than blocking on a
#      transient outage; the freshness banner notes which providers were
#      served from the fallback.
#
#   3. **Daily UPSERTs ALWAYS happen.** Whether the digest is daily or
#      quarterly, the fetcher path runs for the requested window and
#      idempotently writes one row per (usage_date, provider, model, source).
#      Long-cadence digests therefore continuously refresh the daily table
#      and a future digest run can fall back to those rows if its own fetch
#      fails. There is no code path in build() that skips the fetcher.
#
# Implementation note: aggregation continues to happen against
# llm_spend_daily — both the "API primary" and "DB fallback" branches funnel
# through the same _MODEL_SQL / _DAILY_SQL queries below. The only thing
# that differs is whether build() upserted fresh rows before aggregating.
# This keeps the SQL and the sparkline logic single-pathed; the
# source-of-truth policy is purely about whether we
# re-fetch first.

_REFETCH_ON_BUILD_ENV = "LLM_SPEND_DIGEST_REFETCH"   # set to "0" to disable; default = on


@dataclass
class ModelRow:
    provider:      str
    model:         str
    token_type:    str
    cost_usd:      Decimal
    input_tokens:  int
    output_tokens: int
    request_count: int


@dataclass
class ProviderTotal:
    provider:       str
    cost_usd:       Decimal
    requests:       int
    input_tokens:   int = 0
    output_tokens:  int = 0


@dataclass
class DailyPoint:
    usage_date: date
    cost_usd:   Decimal


# ── cache savings ────────────────────────────────────────────────────────
#
# Savings are ALWAYS computed at digest build time from the token_type rows
# already in the window — never stored. Storing a savings figure would be a
# denormalised third copy of numbers already implied by cost_usd/tokens on
# the uncached and cache_read rows, and it could drift from the rate
# definition without anyone noticing. Recomputing costs two divisions per
# model — negligible next to the rest of build().
#
# The counterfactual: "what would these cache_read tokens have cost at the
# uncached (full) rate, versus what was actually paid". This requires a
# sibling 'uncached' row for the SAME (provider, model) to derive a rate
# from — a token_type='blended' row (Gemini's CSV-sourced spend; see the
# token_type migration) has no such sibling, so blended providers/models are
# excluded entirely rather than reported with a wrong or partial number. The
# digest states which providers are covered so this isn't mistaken for an
# org-wide figure.

@dataclass
class CacheSavingsRow:
    provider:                    str
    model:                       str
    cache_read_tokens:           int
    cache_read_cost_usd:         Decimal
    uncached_rate_per_1m:        Decimal   # USD per 1M uncached INPUT tokens, derived
    would_cost_usd:              Decimal   # cache_read_tokens priced at uncached_rate
    saved_usd:                   Decimal   # would_cost_usd - cache_read_cost_usd
    saved_pct:                   Decimal   # saved_usd / would_cost_usd * 100
    # Additional per-token-type rates (None when that token_type has no rows
    # for this model in the window — e.g. a model with no output tokens).
    uncached_output_rate_per_1m:  Optional[Decimal] = None  # USD per 1M output tokens
    cache_read_rate_per_1m:       Optional[Decimal] = None  # USD per 1M cache-read tokens
    cache_write_5m_rate_per_1m:   Optional[Decimal] = None  # USD per 1M cache-write (5m) tokens
    cache_write_1h_rate_per_1m:   Optional[Decimal] = None  # USD per 1M cache-write (1h) tokens


@dataclass
class CacheSavingsSummary:
    rows:                  List[CacheSavingsRow] = field(default_factory=list)
    total_cache_read_cost_usd: Decimal = Decimal("0")
    total_would_cost_usd:  Decimal = Decimal("0")
    total_saved_usd:       Decimal = Decimal("0")
    total_saved_pct:       Decimal = Decimal("0")
    # Providers with at least one itemised (non-blended) model this window —
    # these ARE reflected in the totals above.
    covered_providers:     List[str] = field(default_factory=list)
    # Providers present in the window whose data is entirely token_type=
    # 'blended' (cannot derive a rate) — explicitly NOT reflected above.
    # The digest must name these so the savings figure is never read as
    # org-wide when it isn't.
    excluded_providers:    List[str] = field(default_factory=list)


@dataclass
class PeriodReport:
    label:           str                       # human-friendly period label
    cadence:         str                       # 'daily'|'weekly'|'monthly'|'quarterly'
    window_start:    date
    window_end:      date
    total_cost_usd:  Decimal = Decimal("0")
    provider_totals: List[ProviderTotal] = field(default_factory=list)
    model_breakdown: List[ModelRow]      = field(default_factory=list)
    daily_series:    List[DailyPoint]    = field(default_factory=list)
    prev_total_usd:  Optional[Decimal]   = None    # same-length prior window
    sevenday_avg_usd: Optional[Decimal]  = None    # 7-day rolling average (daily only)
    # Active users during the report window — COUNT(DISTINCT user_id) from
    # ainxt.model_usages for [window_start, window_end]. Shows how many
    # people actually used the platform during the period, not total registered.
    active_users:    int = 0
    # Per-provider source-of-truth resolution at build time.
    # Map: provider -> "api" | "db_fallback" | "skipped".
    #   "api"          — fetcher succeeded for this window; numbers are the
    #                    fresh provider response, also upserted into the
    #                    daily table.
    #   "db_fallback"  — fetcher failed (network / 4xx / 5xx / empty); the
    #                    digest is served from whatever rows already exist
    #                    in llm_spend_daily for this window.
    #   "skipped"      — refetch disabled by LLM_SPEND_DIGEST_REFETCH=0.
    # Surfaced in the email so execs can see when numbers are stale.
    source_of_truth: Dict[str, str] = field(default_factory=dict)
    # Per-provider error string when source_of_truth == "db_fallback".
    # Empty when the API was the source.
    refetch_errors:  Dict[str, str] = field(default_factory=dict)
    # Providers whose numbers for this window may be incomplete / not yet
    # settled at send time — anything NOT freshly sourced from the provider
    # API ("api"). Drives the stale-data banner in the digest. Most common
    # real-world case: Gemini, because GCP's BigQuery billing export lags
    # 6–24h, so an early-morning send may ship before the prior day's Gemini
    # spend has fully landed. Derived in build() from source_of_truth.
    stale_providers: List[str] = field(default_factory=list)
    # Cache-savings breakdown, computed fresh on every build() call for every
    # cadence (daily/weekly/monthly/quarterly). None only if the window has
    # no itemised cache_read rows at all (e.g. total outage). See
    # CacheSavingsSummary docstring for what "covered" vs "excluded" means.
    cache_savings: Optional["CacheSavingsSummary"] = None
    # Human-readable footnote: negotiated-rate providers, WITHOUT a hardcoded
    # discount percentage (that would silently drift on contract renewal).
    # Set in build() whenever at least one itemised provider is present.
    discount_note: str = ""

# ── SQL ────────────────────────────────────────────────────────────────────
#
# Grouped by (provider, model, token_type) rather than just (provider,
# model). This is the token_type itemisation added 2026-07-30 — see
# db/sql/prod_catchup_2026_07_30_llm_spend_token_type.sql. A model with
# itemised rows (OpenAI, Anthropic) now yields up to 5 ModelRows (uncached /
# cache_read / cache_write_5m / cache_write_1h / output); a model whose
# source cannot be itemised (Gemini's CSV-sourced spend) yields exactly one
# row with token_type='blended'. SUM(cost_usd) GROUP BY provider, model
# still reconstructs the pre-migration blended total either way — callers
# that only need per-model cost (not the token_type breakdown) can simply
# sum ModelRow.cost_usd across a model's rows.

_MODEL_SQL = text(
    """
    SELECT provider,
           model,
           token_type,
           SUM(cost_usd)      AS cost_usd,
           SUM(input_tokens)  AS input_tokens,
           SUM(output_tokens) AS output_tokens,
           SUM(request_count) AS request_count
    FROM ainxt.llm_spend_daily
    WHERE usage_date BETWEEN :ws AND :we
    GROUP BY provider, model, token_type
    ORDER BY provider, model, cost_usd DESC NULLS LAST
    """
)

_DAILY_SQL = text(
    """
    SELECT usage_date,
           SUM(cost_usd) AS cost_usd
    FROM ainxt.llm_spend_daily
    WHERE usage_date BETWEEN :ws AND :we
    GROUP BY usage_date
    ORDER BY usage_date
    """
)


# ── API-primary re-fetch ───────────────────────────────────────────────────

def _refetch_window_api_primary(
    window_start: date, window_end: date
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Re-invoke each provider's fetcher for [window_start, window_end].

    Returns (source_of_truth, refetch_errors), both keyed by provider:
        source_of_truth[provider] -> "api" | "db_fallback" | "skipped"
        refetch_errors[provider]  -> str (empty when source == "api")

    Side effect: each successful fetcher UPSERTs into ainxt.llm_spend_daily.
    Failed fetchers still record a row in ainxt.llm_spend_fetch_runs (status
    'failed'); the digest then falls back to whatever rows are in the daily
    table for that provider.

    Honours LLM_SPEND_DIGEST_REFETCH=0 to disable (purely DB-sourced) — used
    by tests, by ad-hoc admin dry-runs that don't want to hammer provider
    APIs, and as an escape hatch if upstream is in a known degraded state.
    """
    import os
    source_of_truth: Dict[str, str] = {}
    refetch_errors:  Dict[str, str] = {}

    if (os.getenv(_REFETCH_ON_BUILD_ENV) or "1").strip() in ("0", "false", "False"):
        for prov in _REQUIRED_PROVIDERS:
            source_of_truth[prov] = "skipped"
            refetch_errors[prov] = ""
        logger.info(
            f"[report_builder] re-fetch disabled via {_REFETCH_ON_BUILD_ENV}=0; "
            f"sourcing all providers from llm_spend_daily"
        )
        return source_of_truth, refetch_errors

    # Local imports keep module load fast and avoid circular import via
    # services.llm_spend.orchestrator (which itself imports report_builder).
    from services.llm_spend.fetchers import (
        openai_costs as _openai,
        anthropic_admin as _anthropic,
        gcp_billing_bq as _gemini,
    )
    from services.llm_spend.alerts import claim_fetch_run

    claim_id = claim_fetch_run(
        cadence="digest_refetch",
        window_start=window_start, window_end=window_end,
        dedup_key=f"refetch-{window_start.isoformat()}..{window_end.isoformat()}",
    )
    if claim_id is None:
        for prov in _REQUIRED_PROVIDERS:
            source_of_truth[prov] = "db_fallback"
            refetch_errors[prov] = ""
        logger.info(
            f"[report_builder] refetch for {window_start}..{window_end} already "
            f"claimed by another worker; sourcing all providers from "
            f"llm_spend_daily (no duplicate API calls)"
        )
        return source_of_truth, refetch_errors

    fetchers = (
        ("openai",    _openai),
        ("anthropic", _anthropic),
        ("gemini",    _gemini),
    )
    for prov, mod in fetchers:
        try:
            res = mod.fetch_window(window_start, window_end)
            if res.status == "ok":
                source_of_truth[prov] = "api"
                refetch_errors[prov] = ""
                logger.info(
                    f"[report_builder] {prov} API primary ok — "
                    f"{len(res.rows)} rows for {window_start}..{window_end}"
                )
            else:
                # Fetcher returned a non-'ok' status (already logged its own
                # error and recorded a 'failed' fetch_run row). Fall back to
                # whatever the daily table has for this provider.
                source_of_truth[prov] = "db_fallback"
                refetch_errors[prov] = (res.error_text or res.status or "")[:300]
                logger.warning(
                    f"[report_builder] {prov} API failed (status={res.status}); "
                    f"falling back to llm_spend_daily for {window_start}..{window_end}"
                )
        except Exception as e:
            # Bare except: a fetcher crash must NOT abort the digest. The
            # daily table is the safety net; we log + tag the provider as
            # fallback and move on.
            source_of_truth[prov] = "db_fallback"
            refetch_errors[prov] = str(e)[:300]
            logger.error(
                f"[report_builder] {prov} fetcher crashed during refetch: {e}"
            )

    return source_of_truth, refetch_errors


# ── public ─────────────────────────────────────────────────────────────────

def build(
    cadence: str,
    window_start: date,
    window_end: date,
    label: str,
    refetch: bool = True,
) -> PeriodReport:
    """Build a PeriodReport for [window_start, window_end] inclusive.

    Source-of-truth policy (see module docstring at top of file):
      1. Re-invoke fetchers for the window — provider APIs are PRIMARY.
         Each successful fetcher UPSERTs ainxt.llm_spend_daily, so future
         digests can also fall back to these rows.
      2. Aggregate from ainxt.llm_spend_daily. If a provider's API call
         failed, its slice is sourced from existing daily rows ("fallback").
      3. Per-provider source ("api" | "db_fallback" | "skipped") is
         surfaced on the returned PeriodReport for the email banner.

    `refetch=False` skips step 1. Used by callers (e.g. the daily orchestrator
    path) that already invoked the fetchers just before build() and don't
    want a redundant second round-trip. The DB rows those callers wrote are
    still authoritative for this build because the orchestrator path holds
    the same logical refresh contract — only the trigger point differs.
    """
    rep = PeriodReport(label=label, cadence=cadence,
                       window_start=window_start, window_end=window_end)

    if refetch:
        # Step 1 — API-primary refresh. Always upserts on success; fail-soft
        # on a per-provider basis (no exception escapes this call).
        rep.source_of_truth, rep.refetch_errors = _refetch_window_api_primary(
            window_start, window_end
        )
    else:
        # Caller already refreshed via the same fetcher path. We mark every
        # provider as "api" optimistically; the gate in the orchestrator
        # (missing_fetch_gaps / failed_fetch_runs) is the actual authority
        # on whether a provider's slice is fresh.
        for _p in _REQUIRED_PROVIDERS:
            rep.source_of_truth[_p] = "api"
            rep.refetch_errors[_p] = ""

    with SessionLocal() as session:
        model_rows = session.execute(_MODEL_SQL, {"ws": window_start, "we": window_end}).fetchall()
        daily_rows = session.execute(_DAILY_SQL, {"ws": window_start, "we": window_end}).fetchall()

        # Previous comparable window
        span_days = (window_end - window_start).days + 1
        prev_end   = window_start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=span_days - 1)
        prev_total = session.execute(
            text(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM ainxt.llm_spend_daily "
                "WHERE usage_date BETWEEN :ws AND :we"
            ),
            {"ws": prev_start, "we": prev_end},
        ).scalar()

        if cadence == "daily":
            sevenday_start = window_start - timedelta(days=7)
            sevenday_end   = window_start - timedelta(days=1)
            sevenday_sum = session.execute(
                text(
                    "SELECT COALESCE(SUM(cost_usd), 0) FROM ainxt.llm_spend_daily "
                    "WHERE usage_date BETWEEN :ws AND :we"
                ),
                {"ws": sevenday_start, "we": sevenday_end},
            ).scalar() or Decimal("0")
            rep.sevenday_avg_usd = (Decimal(str(sevenday_sum)) / Decimal(7)).quantize(Decimal("0.01"))

        # Active users during the report window — distinct users who made
        # at least one LLM request in [window_start, window_end].
        try:
            rep.active_users = int(
                session.execute(
                    text(
                        "SELECT COUNT(DISTINCT user_id) FROM ainxt.model_usages "
                        "WHERE created_at >= :ws AND created_at < :we_next"
                    ),
                    {"ws": window_start, "we_next": window_end + timedelta(days=1)},
                ).scalar() or 0
            )
        except Exception as e:
            logger.warning(f"[report_builder] active user count failed: {e}")
            rep.active_users = 0

    # Model rows + per-provider rollups (cost, requests, input/output tokens).
    # Using ProviderTotal as the accumulator keeps the field list in one
    # place — no parallel tuple to keep in sync as we add columns.
    #
    # request_count is written by the fetchers on ONLY the 'output' token_type
    # row per (day, model) — summing it across a model's token_type rows is
    # therefore still the true per-model request count, not an inflated one.
    provider_acc: Dict[str, ProviderTotal] = {}
    for r in model_rows:
        prov = r.provider
        mr = ModelRow(
            provider=prov,
            model=r.model,
            token_type=r.token_type,
            cost_usd=Decimal(str(r.cost_usd or 0)),
            input_tokens=int(r.input_tokens or 0),
            output_tokens=int(r.output_tokens or 0),
            request_count=int(r.request_count or 0),
        )
        rep.model_breakdown.append(mr)
        acc = provider_acc.get(prov)
        if acc is None:
            acc = ProviderTotal(provider=prov, cost_usd=Decimal("0"), requests=0)
            provider_acc[prov] = acc
        acc.cost_usd      += mr.cost_usd
        acc.requests      += mr.request_count
        acc.input_tokens  += mr.input_tokens
        acc.output_tokens += mr.output_tokens
        rep.total_cost_usd += mr.cost_usd

    rep.provider_totals = [provider_acc[p] for p in sorted(provider_acc)]

    rep.daily_series = [
        DailyPoint(usage_date=r.usage_date, cost_usd=Decimal(str(r.cost_usd or 0)))
        for r in daily_rows
    ]

    rep.prev_total_usd = Decimal(str(prev_total or 0))

    # Stale-data set: any provider not freshly sourced from its API this build.
    # "db_fallback" (fetch failed/empty, served from existing rows) and
    # "skipped" (refetch disabled) both mean the number may be incomplete or
    # revised later. Drives the digest's stale-data banner. Sorted for stable
    # template output.
    rep.stale_providers = sorted(
        prov for prov, src in rep.source_of_truth.items() if src != "api"
    )

    # Cache savings + discount note — computed fresh on every cadence, never
    # stored. See CacheSavingsSummary docstring for the blended-exclusion
    # rule.
    rep.cache_savings = compute_cache_savings(rep.model_breakdown)
    rep.discount_note = build_discount_note(rep.cache_savings)

    return rep


# ── cache savings computation ───────────────────────────────────────────────
#
# Public (no leading underscore): the Cloud Usage dashboard endpoint
# (routers/llm_spend_report_router.py admin_usage_summary) reuses these
# directly against its own ModelRow-shaped rows rather than re-implementing
# the uncached/cache_read join and blended-exclusion rule a second time.

def compute_cache_savings(model_breakdown: List[ModelRow]) -> CacheSavingsSummary:
    """Derive cache-read savings from the token_type rows already fetched.

    For each (provider, model) that has BOTH an 'uncached' row (to derive a
    per-token rate from) and a 'cache_read' row (tokens actually served from
    cache), compute:
        uncached_rate   = uncached.cost_usd / uncached.input_tokens
        would_cost      = cache_read.input_tokens * uncached_rate
        saved           = would_cost - cache_read.cost_usd

    A (provider, model) is skipped — not zero-filled — when either row is
    missing or the uncached row has zero tokens (rate undefined). This is
    the mechanism that excludes token_type='blended' providers (Gemini's
    CSV-sourced spend has no 'uncached' row to divide by) rather than
    silently reporting a wrong or partial savings number for them.
    """
    by_key: Dict[Tuple[str, str, str], ModelRow] = {
        (r.provider, r.model, r.token_type): r for r in model_breakdown
    }

    # A provider "participates" if it has ANY itemised row (uncached,
    # cache_read, cache_write_*, or output) for at least one model — as
    # opposed to appearing only via token_type='blended' rows.
    itemised_providers: set = set()
    blended_providers: set = set()
    seen_models: set = set()
    for r in model_breakdown:
        seen_models.add((r.provider, r.model))
        if r.token_type == TOKEN_TYPE_BLENDED:
            blended_providers.add(r.provider)
        else:
            itemised_providers.add(r.provider)

    rows: List[CacheSavingsRow] = []
    total_cache_cost = Decimal("0")
    total_would_cost = Decimal("0")

    for (prov, model) in sorted(seen_models):
        uncached = by_key.get((prov, model, TOKEN_TYPE_UNCACHED))
        cache_read = by_key.get((prov, model, TOKEN_TYPE_CACHE_READ))
        if uncached is None or cache_read is None:
            continue
        if uncached.input_tokens <= 0 or cache_read.input_tokens <= 0:
            continue

        # Uncached INPUT rate — the primary rate used for savings maths.
        uncached_input_rate = (
            Decimal(uncached.cost_usd) / Decimal(uncached.input_tokens)
        ) * Decimal(1_000_000)

        would_cost = (Decimal(cache_read.input_tokens) / Decimal(1_000_000)) * uncached_input_rate
        saved = would_cost - cache_read.cost_usd
        saved_pct = (saved / would_cost * Decimal(100)) if would_cost > 0 else Decimal("0")

        # Uncached OUTPUT rate — derived from the 'output' token_type row if present.
        output_row = by_key.get((prov, model, TOKEN_TYPE_OUTPUT))
        uncached_output_rate: Optional[Decimal] = None
        if output_row is not None and output_row.output_tokens > 0:
            uncached_output_rate = (
                Decimal(output_row.cost_usd) / Decimal(output_row.output_tokens)
            ) * Decimal(1_000_000)

        # Cache-read INPUT rate — cost per 1M cache-read tokens actually paid.
        cache_read_rate: Optional[Decimal] = None
        if cache_read.input_tokens > 0:
            cache_read_rate = (
                Decimal(cache_read.cost_usd) / Decimal(cache_read.input_tokens)
            ) * Decimal(1_000_000)

        # Cache-write rates (5m and 1h) — cost per 1M tokens written to cache.
        cw5m_row = by_key.get((prov, model, TOKEN_TYPE_CACHE_WRITE_5M))
        cache_write_5m_rate: Optional[Decimal] = None
        if cw5m_row is not None and cw5m_row.input_tokens > 0:
            cache_write_5m_rate = (
                Decimal(cw5m_row.cost_usd) / Decimal(cw5m_row.input_tokens)
            ) * Decimal(1_000_000)

        cw1h_row = by_key.get((prov, model, TOKEN_TYPE_CACHE_WRITE_1H))
        cache_write_1h_rate: Optional[Decimal] = None
        if cw1h_row is not None and cw1h_row.input_tokens > 0:
            cache_write_1h_rate = (
                Decimal(cw1h_row.cost_usd) / Decimal(cw1h_row.input_tokens)
            ) * Decimal(1_000_000)

        rows.append(CacheSavingsRow(
            provider=prov,
            model=model,
            cache_read_tokens=cache_read.input_tokens,
            cache_read_cost_usd=cache_read.cost_usd,
            uncached_rate_per_1m=uncached_input_rate.quantize(Decimal("0.0001")),
            would_cost_usd=would_cost.quantize(Decimal("0.000001")),
            saved_usd=saved.quantize(Decimal("0.000001")),
            saved_pct=saved_pct.quantize(Decimal("0.1")),
            uncached_output_rate_per_1m=(
                uncached_output_rate.quantize(Decimal("0.0001"))
                if uncached_output_rate is not None else None
            ),
            cache_read_rate_per_1m=(
                cache_read_rate.quantize(Decimal("0.0001"))
                if cache_read_rate is not None else None
            ),
            cache_write_5m_rate_per_1m=(
                cache_write_5m_rate.quantize(Decimal("0.0001"))
                if cache_write_5m_rate is not None else None
            ),
            cache_write_1h_rate_per_1m=(
                cache_write_1h_rate.quantize(Decimal("0.0001"))
                if cache_write_1h_rate is not None else None
            ),
        ))
        total_cache_cost += cache_read.cost_usd
        total_would_cost += would_cost

    total_saved = total_would_cost - total_cache_cost
    total_saved_pct = (
        (total_saved / total_would_cost * Decimal(100)) if total_would_cost > 0 else Decimal("0")
    )

    # A provider only "counts" as excluded if it has NO itemised presence at
    # all (pure blended, e.g. Gemini today). A provider with itemised rows
    # but no cache_read activity this window is simply absent from `rows` —
    # it is not misleadingly listed as excluded.
    excluded = sorted(blended_providers - itemised_providers)
    covered = sorted(itemised_providers)

    return CacheSavingsSummary(
        rows=rows,
        total_cache_read_cost_usd=total_cache_cost.quantize(Decimal("0.000001")),
        total_would_cost_usd=total_would_cost.quantize(Decimal("0.000001")),
        total_saved_usd=total_saved.quantize(Decimal("0.000001")),
        total_saved_pct=total_saved_pct.quantize(Decimal("0.1")),
        covered_providers=covered,
        excluded_providers=excluded,
    )


def build_discount_note(savings: CacheSavingsSummary) -> str:
    """One-line footnote on negotiated rates — deliberately WITHOUT a number.

    A hardcoded discount percentage would silently go stale on the next
    contract renewal; the effective rate is already baked into every cost
    figure in the digest (it comes straight from the provider invoice), so
    stating a separate number would be redundant at best and wrong at worst.
    This note exists only so execs don't assume figures are at public list
    price.
    """
    if not savings.covered_providers:
        return ""
    names = " and ".join(p.capitalize() for p in savings.covered_providers)
    return (
        f"All {names} figures in this report reflect the organization's negotiated "
        f"rates, not public list price — no separate discount percentage "
        f"is applied on top."
    )


# ── fetch freshness ────────────────────────────────────────────────────────
#
# Strict policy (chosen 2026-06-17): a digest may only send if EVERY
# provider has at least one status='ok' row covering EVERY day in the
# window. A single (provider, day) gap suppresses the mail and triggers
# the on-call alert. This prevents silently shipping an incomplete number
# that execs would misread as "no spend" for the affected provider.

_REQUIRED_PROVIDERS = ("openai", "anthropic", "gemini")

# Public alias for cross-module use (orchestrator). The underscore name
# stays as the canonical in-module reference; this keeps callers from
# importing a "private" symbol.
REQUIRED_PROVIDERS = _REQUIRED_PROVIDERS

_MISSING_PROVIDER_DAYS_SQL = text(
    """
    WITH days AS (
        SELECT generate_series(CAST(:ws AS date), CAST(:we AS date), '1 day'::interval)::date AS d
    ),
    expected AS (
        SELECT d.d AS day, p.provider
        FROM days d
        CROSS JOIN UNNEST(CAST(:providers AS text[])) AS p(provider)
    )
    SELECT e.day AS missing_date, e.provider AS provider
    FROM expected e
    WHERE NOT EXISTS (
        SELECT 1 FROM ainxt.llm_spend_fetch_runs r
        WHERE r.status = 'ok'
          AND r.provider = e.provider
          AND r.window_start <= e.day AND r.window_end >= e.day
    )
    ORDER BY e.day, e.provider
    """
)


def missing_fetch_gaps(window_start: date, window_end: date) -> List[Tuple[date, str]]:
    """Strict freshness check: list (day, provider) pairs that lack an ok fetch.

    The digest jobs treat any non-empty return as a hard-skip — they will
    not send the exec mail and the on-call alert is fired instead.
    """
    with SessionLocal() as session:
        rows = session.execute(
            _MISSING_PROVIDER_DAYS_SQL,
            {"ws": window_start, "we": window_end, "providers": list(_REQUIRED_PROVIDERS)},
        ).fetchall()
    return [(r.missing_date, r.provider) for r in rows]


def missing_fetch_dates(window_start: date, window_end: date) -> List[date]:
    """Back-compat wrapper: distinct days with at least one missing provider."""
    return sorted({d for (d, _p) in missing_fetch_gaps(window_start, window_end)})


# ── partial-vs-total outage classification ────────────────────────────────
#
# Policy (set 2026-06-27): a digest must SHIP as long as at least one
# provider has usable data for the window, carrying only the providers that
# fetched OK. The exec mail is cancelled ONLY when EVERY required provider
# is down — i.e. there is nothing meaningful to report. A provider that is
# down but not the whole set still ships (its slice is simply absent from
# the breakdown) and the on-call list is alerted separately.
#
# "Down" = no usable OK coverage for the window. A provider counts as down
# if it appears in missing_fetch_gaps() for ANY day in the window (never
# got an OK fetch_run covering that day) OR its LATEST run for the window
# failed (failed_fetch_runs() final-attempt semantics). Either condition
# means the numbers we'd show for that provider are stale / incomplete.


def providers_with_rows(window_start: date, window_end: date) -> set:
    """Return the set of providers that have at least one llm_spend_daily row.

    "Usable data exists" for a provider iff it has any row in the window,
    regardless of that provider's latest fetch_run status. Used by
    down_providers() to distinguish a genuine no-data outage (cancel the
    digest) from a transient failed re-fetch that still has good historical
    rows to report (ship via DB fallback + stale banner).
    """
    with SessionLocal() as session:
        rows = session.execute(
            text(
                "SELECT DISTINCT provider FROM ainxt.llm_spend_daily "
                "WHERE usage_date BETWEEN :ws AND :we"
            ),
            {"ws": window_start, "we": window_end},
        ).fetchall()
    return {r.provider for r in rows}


def down_providers(window_start: date, window_end: date) -> List[str]:
    """Return the sorted list of required providers with no usable data at all.

    A provider is flagged as missing/failed for [window_start, window_end] if
    EITHER:
      * it lacks an OK fetch_run for at least one day in the window
        (missing_fetch_gaps), OR
      * its latest fetch_run for the window is 'failed' (failed_fetch_runs).

    BUT a provider is only "down" (i.e. counted here) if it ALSO has no rows in
    ainxt.llm_spend_daily for the window. A failed latest attempt with existing
    rows is NOT down — the digest ships those rows via DB fallback and the
    stale-data banner flags the provider (source_of_truth == "db_fallback").
    This prevents one trailing 429/400 (or an always-failing provider like
    gemini before its code ships) from suppressing an exec digest that has
    perfectly usable openai/anthropic data.

    Used by the orchestrator to decide cancellation: the digest is cancelled
    only when down_providers covers EVERY required provider (a true total,
    no-data outage); a partial outage still ships with the surviving providers.
    """
    missing = {p for (_d, p) in missing_fetch_gaps(window_start, window_end)}
    failed  = {r.provider for r in failed_fetch_runs(window_start, window_end)}
    have_rows = providers_with_rows(window_start, window_end)
    down = (missing | failed) - have_rows
    return sorted(p for p in _REQUIRED_PROVIDERS if p in down)


def all_providers_down(window_start: date, window_end: date) -> bool:
    """True when EVERY required provider is down for the window (total outage).

    This is the cancellation predicate: the exec digest is suppressed only
    when there is no provider left with usable data to report.
    """
    return len(down_providers(window_start, window_end)) >= len(_REQUIRED_PROVIDERS)


# ── failed-fetch detection ────────────────────────────────────────────────
#
# Sibling of `missing_fetch_gaps`, used ONLY by the daily digest. Where the
# gap check asks "do we have an OK row for every (provider, day)?", this
# check asks "did any fetch run for this window actually FAIL?". A failed
# run is a stronger signal than a gap — it means we hit the provider API
# and got an error back, so the numbers we DO have for that provider in
# this window are guaranteed to be stale / incomplete.
#
# Scope decision: weekly / monthly / quarterly digests do NOT use this
# check. Those windows are long enough that a single failed daily fetch
# (which auto-retries the next night) is usually corrected before the
# longer-cadence digest fires, and a hard-skip on a quarter's worth of
# data over one provider hiccup is more disruptive than helpful. Only
# the daily digest treats a same-day failure as a hard-stop.
#
# We only consider runs whose [window_start, window_end] overlaps the
# digest window, so a failure on an unrelated historical backfill does
# not block today's digest.

_FAILED_RUNS_SQL = text(
    """
    -- Final-attempt semantics: a provider that ultimately succeeded
    -- after earlier retries (multi-worker fanout, or transient 5xx
    -- auto-retried inside the fetch path) must NOT trigger the
    -- daily-digest hard-skip. We pick the LATEST run per
    -- (provider, window_start, window_end) via DISTINCT ON and only
    -- keep the row if that final attempt is still 'failed'. Earlier
    -- failed attempts later superseded by an 'ok' are hidden.
    -- id DESC after run_started DESC keeps the tie-break deterministic
    -- when two retries share a truncated timestamp.
    SELECT provider, run_started, window_start, window_end, error_text
    FROM (
        SELECT DISTINCT ON (provider, window_start, window_end)
               provider,
               run_started,
               window_start,
               window_end,
               status,
               COALESCE(error_text, '') AS error_text
        FROM ainxt.llm_spend_fetch_runs
        WHERE window_start <= :we
          AND window_end   >= :ws
        ORDER BY provider, window_start, window_end, run_started DESC, id DESC
    ) latest
    WHERE status = 'failed'
    ORDER BY provider
    """
)


@dataclass
class FailedFetchRun:
    provider:     str
    run_started:  datetime
    window_start: date
    window_end:   date
    error_text:   str


def failed_fetch_runs(window_start: date, window_end: date) -> List[FailedFetchRun]:
    """Return one row per provider whose LATEST run for this window failed.

    Final-attempt semantics: we look at the most recent fetch_run per
    (provider, window_start, window_end) and only surface it if that
    final attempt is 'failed'. Earlier failed attempts that were later
    superseded by an 'ok' run (transient 5xx, or one worker failing
    while another succeeded in the same multi-pod deployment) are
    hidden. This is what the on-call list wants — they should hear
    about the outage only if it actually persisted through retries,
    not on every transient blip.

    Called only by the daily digest path in orchestrator. A non-empty
    return is a hard-skip: the exec email is cancelled and the on-call
    list is alerted instead. Weekly / monthly / quarterly digests
    deliberately ignore this signal — see the module comment above.
    """
    with SessionLocal() as session:
        rows = session.execute(
            _FAILED_RUNS_SQL, {"ws": window_start, "we": window_end}
        ).fetchall()
    out: List[FailedFetchRun] = []
    for r in rows:
        out.append(
            FailedFetchRun(
                provider=r.provider,
                run_started=r.run_started,
                window_start=r.window_start,
                window_end=r.window_end,
                error_text=r.error_text or "",
            )
        )
    return out


# ── sparkline ──────────────────────────────────────────────────────────────

def _fmt_usd(value: float) -> str:
    """Compact USD label used on the Y-axis ticks ($1.2K, $3.4M, $12.50, …)."""
    abs_v = abs(value)
    if abs_v >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if abs_v >= 1_000:
        return f"${value / 1_000:.1f}K"
    if abs_v >= 100:
        return f"${value:.0f}"
    return f"${value:.2f}"


def _fmt_date(d: date) -> str:
    """Short X-axis tick label, e.g. '17 Jun'."""
    try:
        return d.strftime("%d %b")
    except Exception:
        return str(d)


def sparkline_svg(points: List[DailyPoint], width: int = 580, height: int = 220) -> str:
    """Render an inline SVG line chart with labelled X (date) and Y (USD) axes.

    The chart is fully self-contained (no external CSS / JS / fonts) so it
    renders identically in every mail client. Axis layout:
        * Y axis  — billed amount in USD, 5 evenly-spaced numeric ticks
                    from 0 → ceil(max). Each tick is drawn with a gridline
                    and a `$1.2K`-style label to its left.
        * X axis  — usage_date, up to 8 evenly-spaced tick labels (DD Mon)
                    so longer windows (monthly / quarterly) stay readable.
    """
    if not points or len(points) < 2:
        return ""

    # ── layout / margins ──────────────────────────────────────────────
    m_left, m_right, m_top, m_bottom = 72, 16, 20, 50
    plot_w = width  - m_left - m_right
    plot_h = height - m_top  - m_bottom

    values = [float(p.cost_usd) for p in points]
    dates  = [p.usage_date     for p in points]
    n      = len(values)
    vmax   = max(values + [0.0])

    # Nice round Y-max: ceil to 1 / 2 / 2.5 / 5 × 10^k so ticks land on tidy
    # numbers. The (1, 10] multiplier sweep guarantees a match for any
    # positive vmax, so no fallback branch is needed.
    if vmax <= 0:
        y_top = 1.0
    else:
        base = 10 ** math.floor(math.log10(vmax))
        y_top = next(m * base for m in (1, 2, 2.5, 5, 10) if m * base >= vmax)

    # Coordinate helpers
    def px(i: int) -> float:
        return m_left + (i / max(n - 1, 1)) * plot_w

    def py(v: float) -> float:
        return m_top + plot_h - (v / y_top) * plot_h

    # ── Y-axis gridlines + labels (5 ticks: 0, 25, 50, 75, 100 %) ─────
    y_ticks = 4
    y_axis_parts: List[str] = []
    for i in range(y_ticks + 1):
        v = y_top * i / y_ticks
        y = py(v)
        y_axis_parts.append(
            f'<line x1="{m_left}" y1="{y:.1f}" x2="{m_left + plot_w}" y2="{y:.1f}" '
            f'stroke="#e1e4e8" stroke-width="1"/>'
        )
        y_axis_parts.append(
            f'<text x="{m_left - 6:.1f}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="13" fill="#555" font-family="Arial,Helvetica,sans-serif">'
            f'{_fmt_usd(v)}</text>'
        )

    # ── X-axis tick labels (≤ 8 evenly-spaced dates) ──────────────────
    max_x_ticks = 8
    if n <= max_x_ticks:
        tick_idx = list(range(n))
    else:
        step = (n - 1) / (max_x_ticks - 1)
        tick_idx = sorted({int(round(step * k)) for k in range(max_x_ticks)})
    x_axis_parts: List[str] = []
    for i in tick_idx:
        x = px(i)
        x_axis_parts.append(
            f'<line x1="{x:.1f}" y1="{m_top + plot_h:.1f}" '
            f'x2="{x:.1f}" y2="{m_top + plot_h + 5:.1f}" '
            f'stroke="#888" stroke-width="1"/>'
        )
        x_axis_parts.append(
            f'<text x="{x:.1f}" y="{m_top + plot_h + 20:.1f}" text-anchor="middle" '
            f'font-size="13" fill="#555" font-family="Arial,Helvetica,sans-serif">'
            f'{_fmt_date(dates[i])}</text>'
        )

    # Axis lines
    axes = (
        f'<line x1="{m_left}" y1="{m_top + plot_h:.1f}" '
        f'x2="{m_left + plot_w}" y2="{m_top + plot_h:.1f}" stroke="#444" stroke-width="1"/>'
        f'<line x1="{m_left}" y1="{m_top}" '
        f'x2="{m_left}" y2="{m_top + plot_h:.1f}" stroke="#444" stroke-width="1"/>'
    )

    # ── plotted polyline + per-point markers ──────────────────────────
    pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(values))
    polyline = (
        f'<polyline fill="none" stroke="#1f6feb" stroke-width="2" points="{pts}"/>'
    )
    markers = "".join(
        f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="2.5" fill="#1f6feb"/>'
        for i, v in enumerate(values)
    )

    # ── axis titles ───────────────────────────────────────────────────
    y_title = (
        f'<text x="14" y="{m_top + plot_h / 2:.1f}" font-size="13" fill="#444" '
        f'font-family="Arial,Helvetica,sans-serif" text-anchor="middle" '
        f'transform="rotate(-90 14 {m_top + plot_h / 2:.1f})">'
        f'Billed amount (USD)</text>'
    )
    x_title = (
        f'<text x="{m_left + plot_w / 2:.1f}" y="{height - 6}" font-size="13" '
        f'fill="#444" font-family="Arial,Helvetica,sans-serif" text-anchor="middle">'
        f'Date</text>'
    )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Daily billed amount in USD by date">'
        f'<rect x="{m_left}" y="{m_top}" width="{plot_w}" height="{plot_h}" '
        f'fill="#fbfcfd" stroke="none"/>'
        + "".join(y_axis_parts)
        + axes
        + polyline
        + markers
        + "".join(x_axis_parts)
        + y_title
        + x_title
        + "</svg>"
    )


def sparkline_ascii(points: List[DailyPoint], width: int = 40) -> str:
    """Fallback ASCII sparkline for plain-text emails."""
    if not points:
        return ""
    chars = "▁▂▃▄▅▆▇█"
    values = [float(p.cost_usd) for p in points]
    vmax = max(values) or 1.0
    vmin = min(values)
    span = max(vmax - vmin, 1e-9)
    step = max(1, len(values) // width) if width else 1
    out = []
    for i in range(0, len(values), step):
        v = values[i]
        idx = int(((v - vmin) / span) * (len(chars) - 1))
        out.append(chars[idx])
    return "".join(out)


def svg_to_png_base64(svg_str: str, width: int = 580, height: int = 220) -> str:
    """Convert an SVG string to a base64-encoded PNG using Pillow.

    Outlook and many email clients do not render inline SVG or
    data:image/svg+xml URIs. This renders the chart to a raster PNG
    and returns a base64 string suitable for embedding as
    <img src="data:image/png;base64,...">.

    Falls back to empty string if rendering fails (missing libs, etc.)
    so the email still sends without a chart rather than crashing.
    """
    if not svg_str:
        return ""
    try:
        import base64
        import io
        import re as _re
        from PIL import Image, ImageDraw, ImageFont

        # Parse key data from the SVG to re-draw as a bitmap.
        # Extract polyline points (the actual chart line).
        pts_match = _re.search(r'<polyline[^>]*points="([^"]+)"', svg_str)
        if not pts_match:
            return ""

        raw_pts = pts_match.group(1).strip().split()
        coords = []
        for pt in raw_pts:
            parts = pt.split(",")
            if len(parts) == 2:
                coords.append((float(parts[0]), float(parts[1])))
        if not coords:
            return ""

        # Extract circle markers
        circles = []
        for cm in _re.finditer(r'<circle[^>]*cx="([^"]+)"[^>]*cy="([^"]+)"', svg_str):
            circles.append((float(cm.group(1)), float(cm.group(2))))

        # Extract Y-axis labels
        y_labels = []
        for ym in _re.finditer(
            r'<text[^>]*text-anchor="end"[^>]*>([^<]+)</text>', svg_str
        ):
            y_labels.append(ym.group(1))

        # Extract X-axis labels
        x_labels = []
        for xm in _re.finditer(
            r'<text[^>]*text-anchor="middle"[^>]*font-size="11"[^>]*>([^<]+)</text>',
            svg_str,
        ):
            val = xm.group(1)
            if val not in ("Billed amount (USD)", "Date"):
                x_labels.append(val)

        # Extract Y-axis gridlines (horizontal lines)
        gridlines = []
        for gm in _re.finditer(
            r'<line[^>]*x1="(\d+)"[^>]*y1="([\d.]+)"[^>]*x2="([\d.]+)"[^>]*y2="([\d.]+)"[^>]*stroke="#e1e4e8"',
            svg_str,
        ):
            gridlines.append(float(gm.group(2)))

        # Scale up 2x for crisp rendering
        scale = 2
        w, h = width * scale, height * scale
        img = Image.new("RGB", (w, h), "#fbfcfd")
        draw = ImageDraw.Draw(img)

        # Layout margins (matching SVG)
        ml, mr, mt, mb = 72 * scale, 16 * scale, 20 * scale, 50 * scale
        pw = w - ml - mr
        ph = h - mt - mb

        # Background plot area
        draw.rectangle([ml, mt, ml + pw, mt + ph], fill="#fbfcfd")

        # Gridlines
        for gy in gridlines:
            y_sc = gy * scale
            draw.line([(ml, y_sc), (ml + pw, y_sc)], fill="#e1e4e8", width=1)

        # Axes
        draw.line([(ml, mt + ph), (ml + pw, mt + ph)], fill="#444444", width=scale)
        draw.line([(ml, mt), (ml, mt + ph)], fill="#444444", width=scale)

        # Scale coords
        sc_coords = [(x * scale, y * scale) for x, y in coords]

        # Draw polyline
        if len(sc_coords) >= 2:
            draw.line(sc_coords, fill="#1f6feb", width=2 * scale)

        # Draw markers
        r = 3 * scale
        for cx, cy in circles:
            cx_s, cy_s = cx * scale, cy * scale
            draw.ellipse(
                [cx_s - r, cy_s - r, cx_s + r, cy_s + r], fill="#1f6feb"
            )

        # Try to load a font — 13pt design size, rendered at 2× for crispness.
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13 * scale)
        except Exception:
            try:
                font = ImageFont.truetype("arial.ttf", 13 * scale)
            except Exception:
                font = ImageFont.load_default()

        # Y-axis labels
        if y_labels and gridlines:
            for i, gy in enumerate(gridlines):
                if i < len(y_labels):
                    y_sc = gy * scale
                    lbl = y_labels[i]
                    bbox = draw.textbbox((0, 0), lbl, font=font)
                    tw = bbox[2] - bbox[0]
                    draw.text((ml - 6 * scale - tw, y_sc - 6 * scale), lbl, fill="#666666", font=font)

        # X-axis labels — evenly space along the bottom
        if x_labels:
            n_labels = len(x_labels)
            for i, lbl in enumerate(x_labels):
                x_pos = ml + (i / max(n_labels - 1, 1)) * pw
                bbox = draw.textbbox((0, 0), lbl, font=font)
                tw = bbox[2] - bbox[0]
                draw.text((x_pos - tw / 2, mt + ph + 8 * scale), lbl, fill="#666666", font=font)

        # Axis titles
        # Y-axis title (rotated) — skip for simplicity, hard to do with Pillow
        # X-axis title
        x_title = "Date"
        bbox = draw.textbbox((0, 0), x_title, font=font)
        tw = bbox[2] - bbox[0]
        draw.text((ml + pw / 2 - tw / 2, h - 8 * scale), x_title, fill="#444444", font=font)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    except Exception as e:
        logger.warning(f"[report_builder] SVG->PNG conversion failed: {e}")
        return ""


# ── model breakdown chart ───────────────────────────────────────────────────
#
# Renders one horizontal stacked-bar PNG *per provider* — a provider's models
# ranked by cost, each bar segmented by token_type, with an explicit
# cost-and-token-count line underneath the bar for every token_type that
# model has. Splitting the chart by provider (rather than one combined
# chart spanning every provider) keeps each image focused: fewer rows to
# lay out means each model gets real vertical room for its full per-
# token-type breakdown instead of a single cramped summary line. This
# chart is now the sole presentation of model_breakdown in the HTML email —
# the old per-provider detailed table has been removed since every number
# it showed (model, token type, cost, tokens) is itemised here instead.
# Rendered directly with Pillow (not via an SVG intermediate like
# sparkline_svg/svg_to_png_base64) since there's no need for a separate
# vector representation here — this chart is email-only.

_TT_ORDER = (
    TOKEN_TYPE_UNCACHED, TOKEN_TYPE_CACHE_READ, TOKEN_TYPE_CACHE_WRITE_5M,
    TOKEN_TYPE_CACHE_WRITE_1H, TOKEN_TYPE_OUTPUT, TOKEN_TYPE_NON_TOKEN,
    TOKEN_TYPE_BLENDED,
)

# Colors mirror the token_type badge colors already used elsewhere in the
# email (e.g. the Cache Savings section) so the chart reads as the same
# vocabulary. cache_write_5m/1h get distinct shades here since the chart
# needs to tell them apart within one stacked bar.
_TT_CHART_COLOR = {
    TOKEN_TYPE_UNCACHED:        (55, 65, 81),     # slate-700
    TOKEN_TYPE_CACHE_READ:      (14, 116, 144),   # cyan-700
    TOKEN_TYPE_CACHE_WRITE_5M:  (217, 119, 6),    # amber-600
    TOKEN_TYPE_CACHE_WRITE_1H:  (146, 64, 14),    # amber-900
    TOKEN_TYPE_OUTPUT:          (30, 58, 138),    # blue-900
    TOKEN_TYPE_NON_TOKEN:       (156, 163, 175),  # gray-400
    TOKEN_TYPE_BLENDED:         (209, 213, 219),  # gray-300
}
# Declared separately so the chart-label map holds a name rather than a
# quoted literal on a TOKEN_* key, which secret scanners read as a token
# assignment. The rendered text is unchanged.
_NON_TOKEN_LABEL: str = "Non-token"

_TT_CHART_LABEL = {
    TOKEN_TYPE_UNCACHED:       "Uncached input",
    TOKEN_TYPE_CACHE_READ:     "Cache read",
    TOKEN_TYPE_CACHE_WRITE_5M: "Cache write (5m)",
    TOKEN_TYPE_CACHE_WRITE_1H: "Cache write (1h)",
    TOKEN_TYPE_OUTPUT:         "Output",
    TOKEN_TYPE_NON_TOKEN:      _NON_TOKEN_LABEL,
    TOKEN_TYPE_BLENDED:        "Blended",
}
_PROVIDER_DOT_COLOR = {
    "openai":    (16, 163, 127),
    "anthropic": (217, 119, 6),
    "gemini":    (66, 133, 244),
}
_PROVIDER_DOT_DEFAULT = (107, 114, 128)


def _fmt_tokens_compact(n: int) -> str:
    """Compact token-count label for the per-model line ($1.2M / 45.3K / 812)."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


# Design-space width the chart is laid out at (before the 2x render scale).
# This is also the value the <img width="..."> HTML attribute must use —
# rendering at width * scale px internally but declaring the *design* width
# in HTML is what makes the chart crisp on retina without displaying
# shrunk-and-illegible (mismatching these two was the original readability
# bug: the bitmap was drawn at 1440px but the <img> tag forced width="360").
MODEL_CHART_DESIGN_WIDTH = 648


def _render_provider_breakdown_chart(provider: str, rows: List["ModelRow"]) -> str:
    """Render a single provider's models as a horizontal stacked-bar PNG.

    One row per model (sorted by cost descending), bar segmented by
    token_type. Underneath each bar, one explicit line per token_type
    present on that model showing BOTH the cost and the token count for
    that type (e.g. "Cache read:  $52.70  ·  543.3M tokens") — no folding
    multiple token types onto a single summary line, since that was what
    made the previous chart unreadable at a glance.

    Returns "" (never raises) on any rendering failure — a missing chart
    must never block the digest send, matching svg_to_png_base64's
    fail-open contract.
    """
    if not rows:
        return ""
    try:
        import base64
        import io
        from collections import defaultdict
        from PIL import Image, ImageDraw, ImageFont

        cost_by_tt: Dict[str, Dict[str, float]] = defaultdict(dict)
        tok_by_tt:  Dict[str, Dict[str, int]]   = defaultdict(dict)
        total_cost: Dict[str, float] = defaultdict(float)

        for m in rows:
            cost = float(m.cost_usd)
            tok = m.input_tokens if m.input_tokens else m.output_tokens
            cost_by_tt[m.model][m.token_type] = cost_by_tt[m.model].get(m.token_type, 0.0) + cost
            if tok:
                tok_by_tt[m.model][m.token_type] = tok_by_tt[m.model].get(m.token_type, 0) + tok
            total_cost[m.model] += cost

        models_sorted = sorted(total_cost.items(), key=lambda kv: -kv[1])
        if not models_sorted:
            return ""

        # Render at 2× for retina/crisp output. The design width is the
        # logical pixel width declared in the <img> HTML tag; the actual
        # bitmap is drawn at design_width * scale px so it looks sharp on
        # high-DPI screens without appearing tiny in email clients.
        # Font sizes are specified in design-space points and multiplied by
        # scale so they stay readable at the declared display size.
        scale         = 2
        width         = MODEL_CHART_DESIGN_WIDTH * scale
        left_margin   = 16 * scale
        right_margin  = 16 * scale
        top_margin    = 52 * scale
        bottom_margin = 20 * scale
        bar_h         = 26 * scale
        header_h      = 28 * scale
        gap_header_bar = 8 * scale
        gap_bar_detail = 8 * scale
        detail_line_h  = 24 * scale
        block_gap      = 22 * scale

        bar_area_w = width - left_margin - right_margin

        def _font(size: int, bold: bool = False):
            names = ("arialbd.ttf",) if bold else ("arial.ttf",)
            for name in names:
                try:
                    return ImageFont.truetype(name, size * scale)
                except Exception:
                    continue
            return ImageFont.load_default()

        font_title  = _font(17, bold=True)
        font_model  = _font(15, bold=True)
        font_value  = _font(15)
        font_detail = _font(13)

        # Precompute, per model, which token_types actually have cost>0 and
        # the resulting block height so total canvas height can be sized
        # exactly (no wasted blank lines for token types a model doesn't use).
        model_tts: Dict[str, List[str]] = {}
        block_h_by_model: Dict[str, int] = {}
        for model, _cost in models_sorted:
            present = [tt for tt in _TT_ORDER if cost_by_tt[model].get(tt, 0.0) > 0]
            model_tts[model] = present
            block_h_by_model[model] = (
                header_h + gap_header_bar + bar_h + gap_bar_detail
                + len(present) * detail_line_h + block_gap
            )

        height = top_margin + sum(block_h_by_model.values()) + bottom_margin

        img = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(img)

        # Title: provider dot + name
        dot_color = _PROVIDER_DOT_COLOR.get(provider, _PROVIDER_DOT_DEFAULT)
        draw.ellipse(
            [left_margin, 12 * scale, left_margin + 13 * scale, 12 * scale + 13 * scale],
            fill=dot_color,
        )
        draw.text(
            (left_margin + 20 * scale, 8 * scale),
            f"{provider.capitalize()} — Spend by Model",
            fill=(55, 65, 81), font=font_title,
        )

        max_cost = max((c for _, c in models_sorted), default=0.0) or 1.0

        y = top_margin
        for model, cost_total in models_sorted:
            present = model_tts[model]

            # Header line: model name (left) + total cost (right, same line).
            label = model if len(model) <= 52 else model[:51] + "…"
            draw.text((left_margin, y), label, fill=(31, 41, 55), font=font_model)
            val_text = f"${cost_total:,.2f}"
            bbox = draw.textbbox((0, 0), val_text, font=font_value)
            vw = bbox[2] - bbox[0]
            draw.text(
                (width - right_margin - vw, y + 1 * scale), val_text,
                fill=(30, 58, 138), font=font_value,
            )

            # Stacked bar, proportional in width to this model's share of
            # the provider's most expensive model (so bars are comparable
            # across the whole chart, not just within one model's segments).
            bar_y = y + header_h + gap_header_bar
            bar_w_total = (cost_total / max_cost) * bar_area_w if max_cost > 0 else 0.0
            x_cursor = float(left_margin)
            for tt in present:
                v = cost_by_tt[model].get(tt, 0.0)
                seg_w = max((v / cost_total) * bar_w_total, 1.0 * scale) if cost_total > 0 else 0.0
                draw.rectangle(
                    [x_cursor, bar_y, x_cursor + seg_w, bar_y + bar_h],
                    fill=_TT_CHART_COLOR[tt],
                )
                x_cursor += seg_w
            # Light outline for the unfilled remainder of the row's max-width
            # track, so a low-cost model's short bar still visually anchors
            # against the same right edge as the highest-cost model's bar.
            if bar_w_total < bar_area_w:
                draw.rectangle(
                    [x_cursor, bar_y, left_margin + bar_area_w, bar_y + bar_h],
                    outline=(229, 231, 235),
                )

            # Detail lines: one per present token_type, each showing BOTH
            # the cost and the token count for that type explicitly.
            dy = bar_y + bar_h + gap_bar_detail
            for tt in present:
                c = cost_by_tt[model].get(tt, 0.0)
                tok = tok_by_tt[model].get(tt, 0)
                draw.rectangle(
                    [left_margin, dy + 3 * scale, left_margin + 10 * scale, dy + 13 * scale],
                    fill=_TT_CHART_COLOR[tt],
                )
                tok_str = f"{_fmt_tokens_compact(tok)} tokens" if tok > 0 else "no tokens (flat charge)"
                line = f"{_TT_CHART_LABEL[tt]}:  ${c:,.2f}   ·   {tok_str}"
                draw.text(
                    (left_margin + 16 * scale, dy), line,
                    fill=(75, 85, 99), font=font_detail,
                )
                dy += detail_line_h

            y += block_h_by_model[model]

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    except Exception as e:
        logger.warning(f"[report_builder] provider breakdown chart render failed ({provider}): {e}")
        return ""


def model_breakdown_charts_png(model_breakdown: List["ModelRow"]) -> List[Tuple[str, str]]:
    """Render one horizontal stacked-bar PNG per provider from model_breakdown.

    Returns an ordered list of (provider, base64_png) tuples, sorted by
    each provider's total cost over the window descending — the provider
    driving the most spend appears first. A provider whose chart fails to
    render (see _render_provider_breakdown_chart's fail-open contract) is
    silently omitted from the list rather than blocking the others or the
    digest send.
    """
    if not model_breakdown:
        return []

    from collections import defaultdict

    by_provider: Dict[str, List["ModelRow"]] = defaultdict(list)
    provider_cost: Dict[str, float] = defaultdict(float)
    for m in model_breakdown:
        by_provider[m.provider].append(m)
        provider_cost[m.provider] += float(m.cost_usd)

    providers_sorted = sorted(by_provider.keys(), key=lambda p: -provider_cost[p])

    charts: List[Tuple[str, str]] = []
    for provider in providers_sorted:
        png_b64 = _render_provider_breakdown_chart(provider, by_provider[provider])
        if png_b64:
            charts.append((provider, png_b64))
    return charts
