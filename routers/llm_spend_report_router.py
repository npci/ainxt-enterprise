# SPDX-License-Identifier: MIT
# ============================================================
# LLM SPEND REPORT ROUTER
#
# Admin-only surface for the enterprise-spend feature.
#
#   POST /admin/llm-spend/fetch
#     Body / query: for_date=YYYY-MM-DD  |  from=YYYY-MM-DD&to=YYYY-MM-DD
#     (default: today-2 .. yesterday — the same window the cron uses)
#
#   GET  /admin/llm-spend/reconcile?from=YYYY-MM-DD&to=YYYY-MM-DD
#     Returns DB totals per (provider, day) alongside the URLs admins
#     should compare against in each provider's console. Used for UI
#     parity verification.
#
#   POST /admin/llm-spend/email/daily?for_date=YYYY-MM-DD&dry_run=0|1
#   POST /admin/llm-spend/email/weekly?week_start=YYYY-MM-DD(Mon)&dry_run=0|1
#   POST /admin/llm-spend/email/monthly?month=YYYY-MM&dry_run=0|1
#   POST /admin/llm-spend/email/quarterly?quarter=YYYY-Q[1-4]&dry_run=0|1
#
# `dry_run=1` renders + returns the HTML and the resolved recipients
# without sending. Useful for QA and template diffing.
# ============================================================

from __future__ import annotations

import calendar
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from auth.rbac import require_admin_flag
from core.logger import logger
from db.database import SessionLocal
from services.llm_spend import orchestrator
from services.llm_spend.fetchers import (
    anthropic_admin as _anthropic_fetcher,
    gcp_billing_bq as _gemini_fetcher,
    openai_costs as _openai_fetcher,
)
from services.llm_spend.report_builder import (
    REQUIRED_PROVIDERS, ModelRow, compute_cache_savings, build_discount_note,
)


llm_spend_report_router = APIRouter(tags=["llm-spend"])

# Match the orchestrator's timezone so manual admin sends resolve the same
# "today"/"yesterday" the cron jobs do. Using UTC here would push the default
# window a day off for IST-evening / early-morning requests.
_TZ = ZoneInfo(os.getenv("LLM_SPEND_TZ", "Asia/Kolkata"))


def _today_local() -> date:
    return datetime.now(tz=_TZ).date()


# ── helpers ────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except Exception:
        raise HTTPException(400, f"invalid date: {s!r} (expect YYYY-MM-DD)")


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _parse_month(s: str) -> tuple[date, date]:
    try:
        y_str, m_str = s.split("-")
        y = int(y_str); m = int(m_str)
        if not 1 <= m <= 12:
            raise ValueError
    except Exception:
        raise HTTPException(400, f"invalid month: {s!r} (expect YYYY-MM)")
    ws = date(y, m, 1)
    we = date(y, m, calendar.monthrange(y, m)[1])
    return ws, we


def _parse_quarter(s: str) -> tuple[date, date]:
    try:
        y_str, q_str = s.split("-Q")
        y = int(y_str); q = int(q_str)
        if q not in (1, 2, 3, 4):
            raise ValueError
    except Exception:
        raise HTTPException(400, f"invalid quarter: {s!r} (expect YYYY-Q[1-4])")
    first_month = 3 * (q - 1) + 1
    last_month  = first_month + 2
    ws = date(y, first_month, 1)
    we = date(y, last_month, calendar.monthrange(y, last_month)[1])
    return ws, we


# ── window resolution ──────────────────────────────────────────────────────

_REQUIRED_PROVIDERS = tuple(REQUIRED_PROVIDERS)

_PROVIDER_ESTIMATES = {
    "openai": 5,
    "anthropic": 5,
    "gemini": 180,
}


def _resolve_window(
    granularity: str,
    reference_date: Optional[str] = None,
    from_: Optional[str] = None,
    to: Optional[str] = None,
) -> tuple[date, date]:
    """Resolve a (window_start, window_end) pair from granularity or explicit dates."""
    if (from_ and not to) or (to and not from_):
        raise HTTPException(400, "supply both `from` and `to`")
    if from_ and to:
        ws = _parse_date(from_)
        we = _parse_date(to)
        if we < ws:
            raise HTTPException(400, "to < from")
        return ws, we

    ref = _parse_date(reference_date) if reference_date else _today_local()

    if granularity == "day":
        return ref, ref
    if granularity == "week":
        monday = ref - timedelta(days=ref.weekday())
        return monday, monday + timedelta(days=6)
    if granularity == "month":
        ws = ref.replace(day=1)
        we = ref.replace(day=calendar.monthrange(ref.year, ref.month)[1])
        return ws, we
    if granularity == "quarter":
        q = (ref.month - 1) // 3 + 1
        first_month = 3 * (q - 1) + 1
        last_month = first_month + 2
        ws = date(ref.year, first_month, 1)
        we = date(ref.year, last_month, calendar.monthrange(ref.year, last_month)[1])
        return ws, we

    raise HTTPException(400, f"invalid granularity: {granularity!r}")


def _previous_window(window_start: date, window_end: date) -> tuple[date, date]:
    """Return the immediately preceding window of the same length."""
    span_days = (window_end - window_start).days + 1
    prev_end = window_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span_days - 1)
    return prev_start, prev_end


# ── fetch endpoint (manual / backfill) ─────────────────────────────────────

@llm_spend_report_router.post("/admin/llm-spend/fetch")
def admin_fetch(
    for_date: Optional[str] = Query(None, description="single day YYYY-MM-DD"),
    from_:    Optional[str] = Query(None, alias="from", description="window start YYYY-MM-DD"),
    to:       Optional[str] = Query(None, description="window end YYYY-MM-DD (inclusive)"),
    _admin = Depends(require_admin_flag),
):
    """Trigger an immediate fetch across all three providers."""
    if for_date and (from_ or to):
        raise HTTPException(400, "supply either for_date OR from+to, not both")
    if for_date:
        d = _parse_date(for_date)
        ws = we = d
    elif from_ and to:
        ws = _parse_date(from_); we = _parse_date(to)
        if we < ws:
            raise HTTPException(400, "to < from")
    else:
        # Default window mirrors the nightly cron: [today-2, yesterday] in the
        # configured TZ (IST) so a manual fetch lines up with the cron's days.
        today_local = _today_local()
        we = today_local - timedelta(days=1)
        ws = we - timedelta(days=1)

    logger.info(f"[llm-spend] admin fetch requested for {ws}..{we}")
    summary = orchestrator.run_fetch_window(ws, we)
    return {"window_start": ws.isoformat(), "window_end": we.isoformat(), "providers": summary}


# ── reconcile endpoint (UI parity verification) ────────────────────────────

# Note: intentionally NOT grouped by token_type. This endpoint compares our
# TOTALS against each provider's own billing console, which reports one
# figure per day — the token_type breakdown would have no counterpart to
# reconcile against here. SUM(cost_usd)/SUM(input_tokens) across all
# token_type rows for a (date, provider) reconstructs the same totals this
# query produced before the token_type migration.
_RECONCILE_SQL = text(
    """
    SELECT
        usage_date,
        provider,
        SUM(cost_usd)      AS cost_usd,
        SUM(input_tokens)  AS input_tokens,
        SUM(output_tokens) AS output_tokens,
        SUM(request_count) AS request_count,
        COUNT(DISTINCT model) AS model_count
    FROM ainxt.llm_spend_daily
    WHERE usage_date BETWEEN :ws AND :we
    GROUP BY usage_date, provider
    ORDER BY usage_date, provider
    """
)

# URLs admins should compare our DB numbers against.
_UI_COMPARE_URLS = {
    "openai":    "https://platform.openai.com/usage",
    "anthropic": "https://console.anthropic.com/settings/usage",
    "gemini":    "https://console.cloud.google.com/billing — service: 'Vertex AI' and 'Generative Language API'",
}

# One-line caveat per provider so the admin knows where numbers can drift.
_UI_CAVEATS = {
    "openai":    "OpenAI dashboard renders in account currency + dashboard TZ; our totals are USD in UTC.",
    "anthropic": "Anthropic Console renders the same Admin API; numbers should match to the cent. UI defaults to one workspace at a time; our totals are org-wide.",
    "gemini":    "GCP Billing UI lags ~6–24h; our totals catch up via the next nightly fetch. Includes both Vertex AI and Generative Language API SKUs.",
}


@llm_spend_report_router.get("/admin/llm-spend/reconcile")
def admin_reconcile(
    from_: Optional[str] = Query(None, alias="from", description="window start YYYY-MM-DD"),
    to:    Optional[str] = Query(None, description="window end YYYY-MM-DD (inclusive)"),
    _admin = Depends(require_admin_flag),
):
    """Side-by-side DB totals for admin UI-parity verification.

    Returns one row per (date, provider) so the admin can paste these next
    to each provider console's filtered-by-date view and confirm match.
    """
    if (from_ and not to) or (to and not from_):
        raise HTTPException(400, "supply both `from` and `to`")
    if from_ and to:
        try:
            ws = date.fromisoformat(from_); we = date.fromisoformat(to)
        except Exception:
            raise HTTPException(400, "invalid date format; expect YYYY-MM-DD")
        if we < ws:
            raise HTTPException(400, "to < from")
    else:
        we = _today_local() - timedelta(days=1)
        ws = we - timedelta(days=6)              # default to last 7 days ending yesterday

    rows = []
    daily_totals: dict[date, Decimal] = {}
    provider_totals: dict[str, Decimal] = {}

    with SessionLocal() as session:
        for r in session.execute(_RECONCILE_SQL, {"ws": ws, "we": we}).fetchall():
            cost = Decimal(str(r.cost_usd or 0))
            rows.append({
                "usage_date":    r.usage_date.isoformat() if hasattr(r.usage_date, "isoformat") else str(r.usage_date),
                "provider":      r.provider,
                "cost_usd":      str(cost),
                "input_tokens":  int(r.input_tokens or 0),
                "output_tokens": int(r.output_tokens or 0),
                "request_count": int(r.request_count or 0),
                "model_count":   int(r.model_count or 0),
            })
            daily_totals[r.usage_date] = daily_totals.get(r.usage_date, Decimal("0")) + cost
            provider_totals[r.provider] = provider_totals.get(r.provider, Decimal("0")) + cost

    # Fetch-run coverage check so the admin knows which days are trustworthy.
    coverage = []
    with SessionLocal() as session:
        for r in session.execute(
            text(
                "SELECT provider, MAX(window_end) AS last_ok_end "
                "FROM ainxt.llm_spend_fetch_runs "
                "WHERE status = 'ok' "
                "GROUP BY provider"
            )
        ).fetchall():
            coverage.append({
                "provider":    r.provider,
                "last_ok_end": r.last_ok_end.isoformat() if hasattr(r.last_ok_end, "isoformat") else str(r.last_ok_end),
            })

    return {
        "window_start":        ws.isoformat(),
        "window_end":          we.isoformat(),
        "rows":                rows,
        "provider_totals_usd": {p: str(v) for p, v in provider_totals.items()},
        "daily_totals_usd":    {d.isoformat(): str(v) for d, v in sorted(daily_totals.items())},
        "fetch_coverage":      coverage,
        "ui_compare_urls":     _UI_COMPARE_URLS,
        "ui_caveats":          _UI_CAVEATS,
    }


# ── cloud usage dashboard endpoints ────────────────────────────────────────

# Grouped by (provider, model, token_type). A model with
# itemised rows (OpenAI, Anthropic) yields up to 5 rows here (uncached /
# cache_read / cache_write_5m / cache_write_1h / output); a model whose
# source cannot be itemised (Gemini, blended) yields exactly one. Per-model
# totals (cost, tokens, requests) are unaffected — they're just the sum
# across a model's token_type rows now instead of one pre-summed row.
_SUMMARY_MODEL_SQL = text(
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

_SUMMARY_DAILY_SQL = text(
    """
    SELECT usage_date,
           SUM(cost_usd) AS cost_usd
    FROM ainxt.llm_spend_daily
    WHERE usage_date BETWEEN :ws AND :we
    GROUP BY usage_date
    ORDER BY usage_date
    """
)

_SUMMARY_TOTAL_SQL = text(
    """
    SELECT COALESCE(SUM(cost_usd), 0)      AS cost_usd,
           COALESCE(SUM(input_tokens), 0)  AS input_tokens,
           COALESCE(SUM(output_tokens), 0) AS output_tokens,
           COALESCE(SUM(request_count), 0) AS request_count
    FROM ainxt.llm_spend_daily
    WHERE usage_date BETWEEN :ws AND :we
    """
)

_COVERAGE_SQL = text(
    """
    SELECT provider, MAX(window_end) AS last_ok_end
    FROM ainxt.llm_spend_fetch_runs
    WHERE status = 'ok'
    GROUP BY provider
    """
)

_DAYS_PRESENT_SQL = text(
    """
    SELECT provider, COUNT(DISTINCT usage_date) AS days_present
    FROM ainxt.llm_spend_daily
    WHERE usage_date BETWEEN :ws AND :we
    GROUP BY provider
    """
)


# All token_type rows pass their input_tokens / output_tokens straight through
# from the DB. cache_write_* rows now carry real token counts; non_token rows have cost only and contribute zero tokens by
# construction — no special-casing needed for either.
def _aggregate_window(session, window_start: date, window_end: date) -> dict:
    """Aggregate cost/tokens/requests and daily series for a single window."""
    total = session.execute(_SUMMARY_TOTAL_SQL, {"ws": window_start, "we": window_end}).fetchone()
    model_rows = session.execute(_SUMMARY_MODEL_SQL, {"ws": window_start, "we": window_end}).fetchall()
    daily_rows = session.execute(_SUMMARY_DAILY_SQL, {"ws": window_start, "we": window_end}).fetchall()

    provider_acc: dict[str, dict] = {}
    # Per-provider cache token/cost rollup, surfaced separately from the
    # plain input/output totals above so the UI can show "cache read: X
    # tokens / $Y" without the caller having to filter model_breakdown
    # itself. Providers whose data is entirely token_type='blended' (Gemini
    # today) simply never populate this — zeros, not a wrong estimate.
    cache_acc: dict[str, dict] = {}
    model_breakdown = []
    # Parallel ModelRow list, fed to the SAME compute_cache_savings() used by
    # the exec digest (services/llm_spend/report_builder.py) — the
    # uncached/cache_read join and blended-exclusion rule live in exactly one
    # place, so the dashboard and the email can never silently disagree on a
    # savings figure.
    model_rows_typed: list[ModelRow] = []
    for r in model_rows:
        prov = r.provider
        tt = r.token_type
        cost = Decimal(str(r.cost_usd or 0))
        in_tok = int(r.input_tokens or 0)
        out_tok = int(r.output_tokens or 0)
        req = int(r.request_count or 0)

        model_rows_typed.append(ModelRow(
            provider=prov, model=r.model, token_type=tt,
            cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok,
            request_count=req,
        ))

        if prov not in provider_acc:
            provider_acc[prov] = {
                "provider": prov,
                "cost_usd": Decimal("0"),
                "input_tokens": 0,
                "output_tokens": 0,
                "requests": 0,
            }
        acc = provider_acc[prov]
        acc["cost_usd"] += cost
        acc["input_tokens"] += in_tok
        acc["output_tokens"] += out_tok
        acc["requests"] += req

        if prov not in cache_acc:
            cache_acc[prov] = {
                "provider": prov,
                "cache_read_tokens": 0, "cache_read_cost_usd": Decimal("0"),
                "cache_write_tokens": 0, "cache_write_cost_usd": Decimal("0"),
            }
        cc = cache_acc[prov]
        if tt == "cache_read":
            cc["cache_read_tokens"] += in_tok
            cc["cache_read_cost_usd"] += cost
        elif tt in ("cache_write_5m", "cache_write_1h"):
            cc["cache_write_tokens"] += in_tok   # real count now that fetcher fix is in
            cc["cache_write_cost_usd"] += cost

        model_breakdown.append({
            "provider": prov,
            "model": r.model,
            "token_type": tt,
            "cost_usd": str(cost),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "request_count": req,
        })

    provider_totals = [provider_acc[p] for p in sorted(provider_acc)]
    for pt in provider_totals:
        pt["cost_usd"] = str(pt["cost_usd"])

    cache_totals = [cache_acc[p] for p in sorted(cache_acc)]
    for ct in cache_totals:
        ct["cache_read_cost_usd"] = str(ct["cache_read_cost_usd"])
        ct["cache_write_cost_usd"] = str(ct["cache_write_cost_usd"])

    daily_series = [
        {"usage_date": r.usage_date.isoformat(), "cost_usd": str(Decimal(str(r.cost_usd or 0)))}
        for r in daily_rows
    ]

    # Cache savings — same computation and same blended-exclusion rule as the
    # exec digest (see report_builder.compute_cache_savings docstring).
    # Serialised to strings for JSON transport, matching every other Decimal
    # field in this payload.
    savings = compute_cache_savings(model_rows_typed)
    cache_savings = {
        "rows": [
            {
                "provider": r.provider,
                "model": r.model,
                "cache_read_tokens": r.cache_read_tokens,
                "cache_read_cost_usd": str(r.cache_read_cost_usd),
                "uncached_rate_per_1m": str(r.uncached_rate_per_1m),
                "would_cost_usd": str(r.would_cost_usd),
                "saved_usd": str(r.saved_usd),
                "saved_pct": str(r.saved_pct),
            }
            for r in savings.rows
        ],
        "total_cache_read_cost_usd": str(savings.total_cache_read_cost_usd),
        "total_would_cost_usd": str(savings.total_would_cost_usd),
        "total_saved_usd": str(savings.total_saved_usd),
        "total_saved_pct": str(savings.total_saved_pct),
        "covered_providers": savings.covered_providers,
        "excluded_providers": savings.excluded_providers,
    }
    discount_note = build_discount_note(savings)

    return {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "has_data": bool(model_rows or daily_rows),
        "total_cost_usd": str(Decimal(str(total.cost_usd or 0))),
        "total_input_tokens": int(total.input_tokens or 0),
        "total_output_tokens": int(total.output_tokens or 0),
        "total_requests": int(total.request_count or 0),
        "provider_totals": provider_totals,
        "cache_totals": cache_totals,
        "cache_savings": cache_savings,
        "discount_note": discount_note,
        "model_breakdown": model_breakdown,
        "daily_series": daily_series,
    }


def _compute_comparison(current: dict, previous: dict) -> dict:
    """Compute percentage change between current and previous windows."""
    def _pct(cur, prev):
        cur_v = Decimal(str(cur or 0))
        prev_v = Decimal(str(prev or 0))
        if prev_v == 0:
            return None
        return round(float(((cur_v - prev_v) / prev_v) * Decimal("100")), 2)

    return {
        "cost_pct_change": _pct(current["total_cost_usd"], previous["total_cost_usd"]),
        "input_tokens_pct_change": _pct(current["total_input_tokens"], previous["total_input_tokens"]),
        "output_tokens_pct_change": _pct(current["total_output_tokens"], previous["total_output_tokens"]),
        "requests_pct_change": _pct(current["total_requests"], previous["total_requests"]),
    }


def _fetch_coverage(session, window_end: date) -> tuple[list[dict], list[str]]:
    """Return per-provider coverage rows and the list of stale providers."""
    coverage = []
    stale = []
    for r in session.execute(_COVERAGE_SQL).fetchall():
        last_ok = _as_date(r.last_ok_end)
        is_stale = not last_ok or last_ok < window_end
        coverage.append({
            "provider": r.provider,
            "last_ok_end": last_ok.isoformat() if hasattr(last_ok, "isoformat") else str(last_ok),
            "stale": is_stale,
        })
        if is_stale:
            stale.append(r.provider)
    return coverage, sorted(stale)


@llm_spend_report_router.get("/admin/llm-spend/usage-summary")
def admin_usage_summary(
    granularity: str = Query("month", description="day | week | month | quarter"),
    reference_date: Optional[str] = Query(None, description="anchor date YYYY-MM-DD"),
    from_: Optional[str] = Query(None, alias="from", description="override window start YYYY-MM-DD"),
    to: Optional[str] = Query(None, description="override window end YYYY-MM-DD"),
    _admin = Depends(require_admin_flag),
):
    """Aggregated provider/model/daily usage for the Cloud Usage dashboard."""
    ws, we = _resolve_window(granularity, reference_date, from_, to)
    prev_ws, prev_we = _previous_window(ws, we)

    with SessionLocal() as session:
        current = _aggregate_window(session, ws, we)
        previous = _aggregate_window(session, prev_ws, prev_we)
        coverage, _ = _fetch_coverage(session, we)
        providers = _provider_gap_analysis(session, ws, we)

    return {
        "window_start": ws.isoformat(),
        "window_end": we.isoformat(),
        "granularity": granularity,
        "reference_date": reference_date or ws.isoformat(),
        "current": current,
        "previous": previous,
        "comparison": _compute_comparison(current, previous),
        "fetch_coverage": coverage,
        "stale_providers": sorted(p for p, info in providers.items() if info["action"] == "fetch"),
        "providers": providers,
    }


def _provider_gap_analysis(session, window_start: date, window_end: date) -> dict:
    """Classify each required provider as present/stale/missing for the window."""
    span_days = (window_end - window_start).days + 1

    # Latest ok fetch_run end per provider
    coverage_rows = {
        r.provider: _as_date(r.last_ok_end)
        for r in session.execute(_COVERAGE_SQL).fetchall()
    }

    # Distinct days present per provider
    days_present_rows = {
        r.provider: int(r.days_present or 0)
        for r in session.execute(
            _DAYS_PRESENT_SQL,
            {"ws": window_start, "we": window_end},
        ).fetchall()
    }

    providers = {}
    for prov in _REQUIRED_PROVIDERS:
        last_ok = coverage_rows.get(prov)
        present_days = days_present_rows.get(prov, 0)

        if present_days == 0:
            action = "fetch"
            reason = "missing"
        elif present_days < span_days or not last_ok or last_ok < window_end:
            action = "fetch"
            reason = "stale"
        else:
            action = "skip"
            reason = "present"

        info = {
            "action": action,
            "reason": reason,
            "estimated_seconds": _PROVIDER_ESTIMATES.get(prov, 30),
            "days_present": present_days,
            "days_missing": span_days - present_days,
            "days_expected": span_days,
        }
        if last_ok:
            info["last_ok_end"] = last_ok.isoformat() if hasattr(last_ok, "isoformat") else str(last_ok)
        providers[prov] = info

    return providers


@llm_spend_report_router.get("/admin/llm-spend/fetch-preview")
def admin_fetch_preview(
    granularity: str = Query("month", description="day | week | month | quarter"),
    reference_date: Optional[str] = Query(None, description="anchor date YYYY-MM-DD"),
    from_: Optional[str] = Query(None, alias="from", description="override window start YYYY-MM-DD"),
    to: Optional[str] = Query(None, description="override window end YYYY-MM-DD"),
    _admin = Depends(require_admin_flag),
):
    """Return a gap analysis for the selected window without calling provider APIs."""
    ws, we = _resolve_window(granularity, reference_date, from_, to)

    with SessionLocal() as session:
        providers = _provider_gap_analysis(session, ws, we)

    return {
        "window_start": ws.isoformat(),
        "window_end": we.isoformat(),
        "granularity": granularity,
        "providers": providers,
    }


def _run_targeted_fetch(window_start: date, window_end: date) -> dict:
    """Run fetchers only for providers that are stale or missing for the window."""
    with SessionLocal() as session:
        providers = _provider_gap_analysis(session, window_start, window_end)

    summary = {}
    fetchers = {
        "openai": _openai_fetcher,
        "anthropic": _anthropic_fetcher,
        "gemini": _gemini_fetcher,
    }

    for prov, info in providers.items():
        if info["action"] != "fetch":
            summary[prov] = {"status": "skipped", "reason": info["reason"], "rows": 0}
            continue

        mod = fetchers[prov]
        try:
            res = mod.fetch_window(window_start, window_end)
            summary[prov] = {
                "status": res.status,
                "rows": len(res.rows),
                "error": res.error_text,
            }
        except Exception as e:
            logger.error(f"[llm-spend] targeted fetch crashed for {prov}: {e}")
            summary[prov] = {"status": "failed", "rows": 0, "error": str(e)[:300]}

    return summary


_LATEST_RUN_SQL = text(
    """
    SELECT DISTINCT ON (provider)
           provider, status, rows_upserted, error_text, run_finished
    FROM ainxt.llm_spend_fetch_runs
    WHERE window_start = :ws
      AND window_end   = :we
      AND run_started  >= :since
    ORDER BY provider, run_started DESC
    """
)


@llm_spend_report_router.get("/admin/llm-spend/fetch-status")
def admin_fetch_status(
    since: str = Query(..., description="ISO timestamp the fetch was dispatched"),
    granularity: str = Query("month", description="day | week | month | quarter"),
    reference_date: Optional[str] = Query(None, description="anchor date YYYY-MM-DD"),
    from_: Optional[str] = Query(None, alias="from", description="override window start YYYY-MM-DD"),
    to: Optional[str] = Query(None, description="override window end YYYY-MM-DD"),
    _admin = Depends(require_admin_flag),
):
    """Report the latest fetch_run result per provider for a dispatched fetch.

    The UI polls this after POST /fetch-async so the progress banner can move
    each provider from 'running' → 'ok'/'failed' and surface the error text.
    """
    ws, we = _resolve_window(granularity, reference_date, from_, to)
    try:
        # UI sends an ISO string, often with a trailing 'Z'. Normalise to a
        # naive UTC datetime so it compares cleanly against run_started, which
        # is stored as a naive-UTC TIMESTAMP (see db.models.LLMSpendFetchRun).
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        if since_dt.tzinfo is not None:
            since_dt = since_dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        raise HTTPException(400, "invalid `since` (expect ISO timestamp)")

    runs: dict[str, dict] = {}
    with SessionLocal() as session:
        for r in session.execute(_LATEST_RUN_SQL, {"ws": ws, "we": we, "since": since_dt}).fetchall():
            runs[r.provider] = {
                "status": r.status,
                "rows": int(r.rows_upserted or 0),
                "error": r.error_text,
                "finished_at": r.run_finished.isoformat() if hasattr(r.run_finished, "isoformat") else str(r.run_finished),
            }
        # Only providers that were actually dispatched (stale/missing) write a
        # run. Skipped providers are already terminal, so 'done' must ignore
        # them — otherwise the banner would poll forever.
        gap = _provider_gap_analysis(session, ws, we)

    awaited = [p for p, info in gap.items() if info["action"] == "fetch"]
    done = all(p in runs for p in awaited)
    return {
        "window_start": ws.isoformat(),
        "window_end": we.isoformat(),
        "runs": runs,
        "done": done,
    }


@llm_spend_report_router.post("/admin/llm-spend/fetch-async")
def admin_fetch_async(
    background_tasks: BackgroundTasks,
    granularity: str = Query("month", description="day | week | month | quarter"),
    reference_date: Optional[str] = Query(None, description="anchor date YYYY-MM-DD"),
    from_: Optional[str] = Query(None, alias="from", description="override window start YYYY-MM-DD"),
    to: Optional[str] = Query(None, description="override window end YYYY-MM-DD"),
    _admin = Depends(require_admin_flag),
):
    """Trigger provider fetches only for stale/missing providers in the background."""
    ws, we = _resolve_window(granularity, reference_date, from_, to)

    with SessionLocal() as session:
        providers = _provider_gap_analysis(session, ws, we)

    should_fetch = any(info["action"] == "fetch" for info in providers.values())
    if should_fetch:
        background_tasks.add_task(_run_targeted_fetch, ws, we)

    return {
        "window_start": ws.isoformat(),
        "window_end": we.isoformat(),
        "accepted": should_fetch,
        "providers": providers,
    }


# ── digest endpoints ───────────────────────────────────────────────────────

def _dry_or_send(cadence: str, ws: date, we: date, label: str, dry_run: bool):
    if dry_run:
        payload = orchestrator.render_for_dry_run(cadence, ws, we, label)
        return HTMLResponse(content=payload["html"], headers={
            "X-LLM-Spend-To":            ",".join(payload["to"])[:1500],
            "X-LLM-Spend-Cc":            ",".join(payload["cc"])[:1500],
            "X-LLM-Spend-Bcc":           ",".join(payload.get("bcc", []))[:1500],
            "X-LLM-Spend-Missing-Dates": ",".join(payload["missing_dates"])[:1500],
            "X-LLM-Spend-Total-USD":     payload["total_cost_usd"],
        })
    # send
    sent = False
    if cadence == "daily":
        sent = orchestrator.send_daily_digest(for_date=ws)
    elif cadence == "weekly":
        sent = orchestrator.send_weekly_digest(week_start=ws)
    elif cadence == "monthly":
        sent = orchestrator.send_monthly_digest(month=ws.strftime("%Y-%m"))
    elif cadence == "quarterly":
        q = (we.month - 1) // 3 + 1
        sent = orchestrator.send_quarterly_digest(quarter=f"{we.year}-Q{q}")
    return JSONResponse({
        "cadence":      cadence,
        "label":        label,
        "window_start": ws.isoformat(),
        "window_end":   we.isoformat(),
        "sent":         bool(sent),
    })


@llm_spend_report_router.post("/admin/llm-spend/email/daily")
def admin_email_daily(
    for_date: Optional[str] = Query(None),
    dry_run:  int            = Query(0),
    _admin = Depends(require_admin_flag),
):
    if for_date:
        d = _parse_date(for_date)
    else:
        d = _today_local() - timedelta(days=1)
    label = d.strftime("%A, %d %b %Y")
    return _dry_or_send("daily", d, d, label, bool(dry_run))


@llm_spend_report_router.post("/admin/llm-spend/email/weekly")
def admin_email_weekly(
    week_start: Optional[str] = Query(None, description="Monday YYYY-MM-DD"),
    dry_run:    int           = Query(0),
    _admin = Depends(require_admin_flag),
):
    if week_start:
        ws = _parse_date(week_start)
        if ws.weekday() != 0:
            raise HTTPException(400, "week_start must be a Monday")
    else:
        today = _today_local()
        this_monday = today - timedelta(days=today.weekday())
        ws = this_monday - timedelta(days=7)
    we = ws + timedelta(days=6)
    label = f"{ws.strftime('%d %b')} – {we.strftime('%d %b %Y')}"
    return _dry_or_send("weekly", ws, we, label, bool(dry_run))


@llm_spend_report_router.post("/admin/llm-spend/email/monthly")
def admin_email_monthly(
    month:   Optional[str] = Query(None, description="YYYY-MM"),
    dry_run: int           = Query(0),
    _admin = Depends(require_admin_flag),
):
    if month:
        ws, we = _parse_month(month)
    else:
        today = _today_local()
        first_this = today.replace(day=1)
        last_prev  = first_this - timedelta(days=1)
        ws = last_prev.replace(day=1)
        we = last_prev
    label = ws.strftime("%B %Y")
    return _dry_or_send("monthly", ws, we, label, bool(dry_run))


@llm_spend_report_router.post("/admin/llm-spend/email/quarterly")
def admin_email_quarterly(
    quarter: Optional[str] = Query(None, description="YYYY-Q[1-4]"),
    dry_run: int           = Query(0),
    _admin = Depends(require_admin_flag),
):
    if quarter:
        ws, we = _parse_quarter(quarter)
    else:
        today = _today_local()
        q = (today.month - 1) // 3 + 1
        if q == 1:
            ws = date(today.year - 1, 10, 1)
            we = date(today.year - 1, 12, 31)
        else:
            first_month = 3 * (q - 2) + 1
            last_month  = first_month + 2
            ws = date(today.year, first_month, 1)
            we = date(today.year, last_month, calendar.monthrange(today.year, last_month)[1])
    label = f"Q{((we.month - 1)//3)+1} {we.year}"
    return _dry_or_send("quarterly", ws, we, label, bool(dry_run))
