# SPDX-License-Identifier: MIT
# ============================================================
# services.llm_spend.orchestrator
#
# Public entrypoints used by:
#   - APScheduler cron jobs in gateway.py
#   - the admin router (routers/llm_spend_report_router.py)
#
# Fetch entrypoints:
#   run_daily_fetch()                      — 2-day rolling window
#   run_fetch_window(ws, we)               — explicit window (admin / backfill)
#   backfill_if_empty(days=90)             — first-deploy 90-day backfill
#
# Digest entrypoints:
#   send_daily_digest(for_date=None)       — yesterday
#   send_weekly_digest(week_start=None)    — prior Mon..Sun
#   send_monthly_digest(month=None)        — prior calendar month
#   send_quarterly_digest(quarter=None)    — prior calendar quarter
#
# Every digest:
#   1. resolves window
#   2. checks llm_spend_fetch_runs for coverage
#   3. if missing → calls alerts.alert_missing_fetch() and returns False
#   4. otherwise builds the report, renders Jinja, sends one BCC email
# ============================================================

from __future__ import annotations

import calendar
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.logger import logger
from services.llm_spend.alerts import (
    alert_failed_fetch, alert_missing_fetch, alert_missing_recipients,
    alert_partial_fetch, claim_digest_send, claim_fetch_run, record_digest_send,
)
from services.llm_spend.fetchers import openai_costs, anthropic_admin, gcp_billing_bq
from services.llm_spend.recipients import (
    cadence_to_env_var,
    resolve_digest_to,
    resolve_digest_bcc,
)
from services.llm_spend.report_builder import (
    PeriodReport, build as build_report,
    REQUIRED_PROVIDERS,
    down_providers,
    failed_fetch_runs,
    missing_fetch_dates, missing_fetch_gaps,
    sparkline_ascii, sparkline_svg, svg_to_png_base64,
    model_breakdown_charts_png,
)
from services.smtp_service import send_html_email


_TZ = ZoneInfo(os.getenv("LLM_SPEND_TZ", "Asia/Kolkata"))

_TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "assets", "email_templates",
)

_jinja = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=False,
    lstrip_blocks=False,
)


# ── window helpers ─────────────────────────────────────────────────────────

def _today_local() -> date:
    return datetime.now(tz=_TZ).date()


def _yesterday() -> date:
    return _today_local() - timedelta(days=1)


def _previous_week_window(today: Optional[date] = None) -> Tuple[date, date]:
    today = today or _today_local()
    # Monday of *this* week:
    this_monday = today - timedelta(days=today.weekday())
    prev_monday = this_monday - timedelta(days=7)
    prev_sunday = this_monday - timedelta(days=1)
    return prev_monday, prev_sunday


def _previous_month_window(today: Optional[date] = None) -> Tuple[date, date]:
    today = today or _today_local()
    first_of_this_month = today.replace(day=1)
    last_of_prev_month  = first_of_this_month - timedelta(days=1)
    first_of_prev_month = last_of_prev_month.replace(day=1)
    return first_of_prev_month, last_of_prev_month


def _previous_quarter_window(today: Optional[date] = None) -> Tuple[date, date]:
    today = today or _today_local()
    q = (today.month - 1) // 3 + 1               # current quarter 1..4
    if q == 1:
        ps, pe = (10, 12)
        year = today.year - 1
    else:
        ps = 3 * (q - 2) + 1
        pe = ps + 2
        year = today.year
    start = date(year, ps, 1)
    end   = date(year, pe, calendar.monthrange(year, pe)[1])
    return start, end


# ── fetch entrypoints ──────────────────────────────────────────────────────

def run_fetch_window(window_start: date, window_end: date) -> dict:
    """Run fetchers against the explicit window. Returns per-provider summary.

    GCP BigQuery (Gemini) fetch is gated by LLM_SPEND_GCP_ENABLED (default false).
    When disabled, the gemini entry is skipped entirely — no LLM_PROXY_URL required.
    OpenAI and Anthropic fetchers run independently of this flag.
    """
    if window_end < window_start:
        raise ValueError("window_end < window_start")

    from core.config import LLM_SPEND_GCP_ENABLED

    fetchers = [
        ("openai",    openai_costs),
        ("anthropic", anthropic_admin),
    ]
    if LLM_SPEND_GCP_ENABLED:
        fetchers.append(("gemini", gcp_billing_bq))
    else:
        logger.debug(
            "[orchestrator] GCP BigQuery fetch skipped "
            "(LLM_SPEND_GCP_ENABLED=false — set true to enable Gemini spend tracking)"
        )

    summary = {}
    for name, mod in fetchers:
        try:
            res = mod.fetch_window(window_start, window_end)
            summary[name] = {
                "status": res.status,
                "rows":   len(res.rows),
                "error":  res.error_text,
            }
        except Exception as e:
            logger.error(f"[orchestrator] {name} fetcher crashed: {e}")
            summary[name] = {"status": "failed", "rows": 0, "error": str(e)[:300]}
    return summary


def run_daily_fetch() -> dict:
    """Nightly job — refresh [today-2, yesterday] across all providers.

    Multi-worker safe: gateway_bootstrap registers this cron in EVERY uvicorn
    worker, so all N workers (e.g. N=8 in a multi-worker deployment) fire it at
    01:30 simultaneously. We claim the run on ainxt.llm_spend_alerts_sent
    (kind='fetch_run') so exactly ONE worker actually hits the provider APIs
    per window; the rest no-op. The UPSERTs are idempotent so this is purely
    an efficiency / API-quota guard, but it also keeps fetch_runs free of N
    duplicate rows every night. The claim fails OPEN on a DB error, so a
    dedup-table blip degrades to the old "every worker fetches" behaviour
    rather than skipping the night entirely.
    """
    end   = _yesterday()
    start = end - timedelta(days=1)             # 2-day window absorbs late corrections

    claim_id = claim_fetch_run(
        cadence="nightly",
        window_start=start, window_end=end,
        dedup_key=f"{start.isoformat()}..{end.isoformat()}",
    )
    if claim_id is None:
        logger.info(
            f"[orchestrator] run_daily_fetch window={start}..{end} already "
            f"claimed by another worker; skipping duplicate fetch"
        )
        return {"skipped": "claimed_by_other_worker"}

    logger.info(f"[orchestrator] run_daily_fetch window={start}..{end}")
    return run_fetch_window(start, end)


# ── evening fetch + overnight Gemini settle loop ───────────────────────────
#
# New nightly model (2026-06-30):
#   * 21:00 IST — run_evening_fetch() pulls TODAY's spend across all three
#     providers, then spawns a background thread that keeps re-fetching
#     Gemini until its BigQuery billing export settles (or a deadline, default
#     06:00 the next morning). GCP's export lags 6–24h, so a single 21:00
#     fetch sees little/no Gemini data for the day; the loop catches the late
#     arrival and upserts the real numbers as they land.
#   * 06:00 IST — send_daily_digest() reports YESTERDAY (= the day fetched the
#     evening before). If Gemini still hasn't settled by 06:00 the digest
#     ships anyway with a stale-data banner (report.stale_providers).
#
# OpenAI + Anthropic settle far faster than GCP, so only Gemini gets the
# overnight retry loop. The morning digest's own refetch picks up any late
# OpenAI/Anthropic corrections.

# Env knobs (all optional):
_GEMINI_SETTLE_INTERVAL_MIN = int(os.getenv("LLM_SPEND_GEMINI_RETRY_INTERVAL_MIN", "30"))
_GEMINI_SETTLE_DEADLINE_HR  = int(os.getenv("LLM_SPEND_GEMINI_SETTLE_DEADLINE_HOUR", "6"))


def run_gemini_until_settled(
    target_date: date,
    deadline_hour: Optional[int] = None,
    interval_min: Optional[int] = None,
) -> None:
    """Re-fetch Gemini for `target_date` until its billing export settles.

    No-op when LLM_SPEND_GCP_ENABLED=false.
    

    Intended to run in a background thread launched by run_evening_fetch().
    Each pass calls the Gemini fetcher (which upserts ainxt.llm_spend_daily),
    then probes gcp_billing_bq.window_is_settled(). Stops when:
      * the day's Gemini data is settled (cost + input + output tokens present), or
      * the local clock passes `deadline_hour` on the day AFTER target_date
        (so the 06:00 digest send isn't kept waiting), or
      * a hard safety cap of attempts is hit.

    All exceptions are swallowed — this is a best-effort background refiner;
    the morning digest ships regardless, with a stale banner if Gemini never
    settled.
    """
    from core.config import LLM_SPEND_GCP_ENABLED
    if not LLM_SPEND_GCP_ENABLED:
        logger.debug(
            "[orchestrator] run_gemini_until_settled skipped "
            "(LLM_SPEND_GCP_ENABLED=false)"
        )
        return

    deadline_hour = deadline_hour if deadline_hour is not None else _GEMINI_SETTLE_DEADLINE_HR
    interval_min  = interval_min  if interval_min  is not None else _GEMINI_SETTLE_INTERVAL_MIN
    interval_secs = max(60, interval_min * 60)

    # Deadline: deadline_hour on the morning AFTER the usage day. e.g. usage
    # day 30 Jun, deadline_hour 6 -> stop at 1 Jul 06:00 IST.
    deadline_dt = datetime.combine(
        target_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=_TZ,
    ).replace(hour=deadline_hour)

    # Safety cap so a clock/timezone surprise can't spin forever: at most one
    # attempt per interval across the whole 21:00->06:00 window, +2 slack.
    max_attempts = int(((9 * 60) / interval_min)) + 2 if interval_min else 20

    logger.info(
        f"[orchestrator] gemini settle loop start for {target_date} "
        f"(interval={interval_min}m, deadline={deadline_dt.isoformat()})"
    )

    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        now = datetime.now(tz=_TZ)
        if now >= deadline_dt:
            logger.warning(
                f"[orchestrator] gemini settle loop hit deadline for {target_date} "
                f"after {attempt - 1} attempts; morning digest will flag it stale"
            )
            return

        try:
            res = gcp_billing_bq.fetch_window(target_date, target_date)
            logger.info(
                f"[orchestrator] gemini settle attempt {attempt} for {target_date}: "
                f"status={res.status} rows={len(res.rows)}"
            )
        except Exception as e:
            logger.error(f"[orchestrator] gemini settle attempt {attempt} crashed: {e}")

        try:
            if gcp_billing_bq.window_is_settled(target_date):
                logger.info(
                    f"[orchestrator] gemini data settled for {target_date} "
                    f"after {attempt} attempt(s); stopping settle loop"
                )
                return
        except Exception as e:
            logger.warning(f"[orchestrator] gemini settle probe crashed: {e}")

        # Not settled yet — wait, unless the next sleep would overrun the
        # deadline (then just stop now).
        if datetime.now(tz=_TZ) + timedelta(seconds=interval_secs) >= deadline_dt:
            logger.warning(
                f"[orchestrator] gemini not settled for {target_date} and next "
                f"retry would overrun deadline; stopping (will flag stale)"
            )
            return
        time.sleep(interval_secs)

    logger.warning(
        f"[orchestrator] gemini settle loop exhausted {max_attempts} attempts "
        f"for {target_date} without settling"
    )


def run_evening_fetch(target_date: Optional[date] = None) -> dict:
    """21:00 job — fetch TODAY's spend, then spawn the Gemini settle loop.

    Fetches all three providers once for `target_date` (default: today in the
    configured TZ). OpenAI/Anthropic are usually near-complete same-evening;
    Gemini is typically still empty because GCP's export lags 6–24h, so we
    launch run_gemini_until_settled() in a daemon thread to keep refining the
    Gemini rows through the night up to the 06:00 deadline.

    Multi-worker safe: claims the fetch run on ainxt.llm_spend_alerts_sent so
    only one uvicorn worker fetches + owns the settle loop per date.
    """
    target = target_date or _today_local()

    claim_id = claim_fetch_run(
        cadence="evening",
        window_start=target, window_end=target,
        dedup_key=f"evening-{target.isoformat()}",
    )
    if claim_id is None:
        logger.info(
            f"[orchestrator] run_evening_fetch {target} already claimed by "
            f"another worker; skipping duplicate fetch + settle loop"
        )
        return {"skipped": "claimed_by_other_worker"}

    logger.info(f"[orchestrator] run_evening_fetch target={target}")
    summary = run_fetch_window(target, target)

    # Spawn the overnight Gemini settle loop only if Gemini isn't already
    # settled from the initial pass (rare this early, but cheap to check).
    try:
        already = gcp_billing_bq.window_is_settled(target)
    except Exception:
        already = False

    if already:
        logger.info(
            f"[orchestrator] gemini already settled for {target} at evening fetch; "
            f"no settle loop needed"
        )
    else:
        threading.Thread(
            target=run_gemini_until_settled,
            args=(target,),
            name=f"gemini_settle_{target.isoformat()}",
            daemon=True,
        ).start()
        logger.info(f"[orchestrator] launched gemini settle loop for {target}")

    return summary


def backfill_if_empty(days: int = 90) -> Optional[dict]:
    """First-deploy helper: if llm_spend_daily is empty, fetch trailing `days` days.

    Idempotent: a non-empty table short-circuits with no work.
    """
    from sqlalchemy import text as _sql
    from db.database import SessionLocal as _S
    try:
        with _S() as session:
            n = session.execute(_sql("SELECT COUNT(*) FROM ainxt.llm_spend_daily")).scalar() or 0
    except Exception as e:
        logger.warning(f"[orchestrator] backfill probe failed: {e}")
        return None
    if n > 0:
        logger.info(f"[orchestrator] backfill skipped — llm_spend_daily has {n} rows")
        return None

    end   = _yesterday()
    start = end - timedelta(days=days - 1)
    logger.info(f"[orchestrator] backfill_if_empty {days}d → {start}..{end}")
    return run_fetch_window(start, end)


def prune_fetch_runs(before: date) -> int:
    """Delete ainxt.llm_spend_fetch_runs rows whose window_end < `before`.

    The fetch_runs table is purely an audit / freshness-gate log; the actual
    spend lives in llm_spend_daily. The quarterly digest is the last consumer
    of these rows (it's the widest window), so once it has shipped we can prune
    everything older than the current quarter to keep the table bounded. We
    DELETE (not TRUNCATE) so rows for the CURRENT period — which the daily /
    weekly missing/failed gates still rely on — are preserved; wiping them
    would make the next daily digest read a spurious total-outage.

    Best-effort: a failed prune must never break the digest send that triggered
    it, so all exceptions are swallowed and logged. Returns the rowcount
    deleted (0 on error).
    """
    from sqlalchemy import text as _sql
    from db.database import SessionLocal as _S
    try:
        with _S() as session:
            res = session.execute(
                _sql("DELETE FROM ainxt.llm_spend_fetch_runs WHERE window_end < :before"),
                {"before": before},
            )
            session.commit()
            n = res.rowcount or 0
        logger.info(f"[orchestrator] pruned {n} fetch_runs rows with window_end < {before}")
        return n
    except Exception as e:
        logger.error(f"[orchestrator] prune_fetch_runs(before={before}) failed: {e}")
        return 0


# ── digest entrypoints ─────────────────────────────────────────────────────

def _send_digest(
    cadence:      str,
    window_start: date,
    window_end:   date,
    period_label: str,
    template_html: str,
    template_txt:  str,
    subject:       str,
    refetch:      bool = True,
    claim_id:     int | None = None,
) -> bool:
    """Common path used by all four digests.

    `claim_id` lets a caller that has ALREADY won the digest-send claim
    (currently send_daily_digest, which claims up front so its pre-refresh
    runs on one worker only) hand the ownership in. When provided, we skip
    the internal claim and re-use it for the audit back-fill. Weekly /
    monthly / quarterly callers omit it and we claim here.

    Source-of-truth ordering (see report_builder module docstring):
      1. build_report() re-invokes the fetchers (provider APIs PRIMARY)
         and upserts ainxt.llm_spend_daily for the window.
      2. Aggregation reads from llm_spend_daily — fresh API rows where
         the fetch succeeded, existing rows where it failed (DB fallback).
      3. missing_fetch_gaps() is evaluated AFTER build() so it sees the
         freshly-written rows. A gap now means a provider failed re-fetch
         AND has no historical row for that (provider, day) — i.e. true
         no-data, not a transient outage already corrected by the refresh.
    """

    # Per-cadence recipient routing. Each cadence's To: comes solely from its
    # own env var (LLM_SPEND_{DAILY,WEEKLY,MONTHLY,QUARTERLY}_TO). No Cc/Bcc.
    to = resolve_digest_to(cadence)
    cc: List[str] = []
    if not to:
        env_var = cadence_to_env_var(cadence)
        logger.error(
            f"[orchestrator] {cadence} digest aborted — no recipients "
            f"(env {env_var} empty/unset); firing misconfiguration alert"
        )
        alert_missing_recipients(cadence, period_label, window_start, window_end, env_var)
        return False

    # Optional blind-copy list from LLM_SPEND_{CADENCE}_BCC. Empty is fine — the
    # digest ships without a BCC. Resolved AFTER the empty-To: abort so a BCC can
    # never keep a no-recipient digest alive.
    bcc = resolve_digest_bcc(cadence)

    # Step 0 — multi-worker send dedup. gateway_bootstrap registers the cron
    # job in EVERY uvicorn worker, so all N workers reach this point at the
    # same minute. Claim the right to send exactly once per (cadence, window)
    # via ainxt.llm_spend_alerts_sent (kind='digest_sent'). The single winner
    # proceeds; every loser no-ops and reports success (the digest IS being
    # sent — just by another worker). We claim BEFORE build_report() so the
    # losers skip the expensive fetch + render + SMTP entirely.
    # If the caller already won the claim (daily path), reuse it instead of
    # re-claiming (which would self-conflict and wrongly return None here).
    if claim_id is None:
        claim_id = claim_digest_send(cadence, window_start, window_end, period_label)
        if claim_id is None:
            logger.info(
                f"[orchestrator] {cadence} digest for {period_label} already claimed "
                f"by another worker; skipping duplicate send"
            )
            return True

    # Step 1 — build the report. This is the API-primary step: the report
    # builder calls each fetcher for the window, upserts the daily table,
    # then aggregates. Per-provider source ("api"|"db_fallback"|"skipped")
    # lives on report.source_of_truth.
    # The `refetch` flag is False only when the caller already triggered
    # the fetcher path (e.g. send_daily_digest pre-refreshes so its
    # failed_fetch_runs check sees post-refresh state).
    report = build_report(
        cadence, window_start, window_end, period_label, refetch=refetch
    )

    # Step 2 — partial-vs-total outage gate, evaluated AFTER the re-fetch.
    #
    # Policy (2026-06-27): the digest must SHIP as long as at least one
    # provider has usable data for the window. We cancel ONLY when EVERY
    # required provider is down (no OK coverage / latest run failed) — i.e.
    # there is nothing meaningful left to report. When some-but-not-all
    # providers are down, we still send the digest carrying the surviving
    # providers (the down provider's slice is simply absent from the
    # aggregation) and fire a separate on-call alert so the gap is noticed
    # and backfilled.
    down = down_providers(window_start, window_end)
    if down and len(down) >= len(REQUIRED_PROVIDERS):
        # TOTAL outage — every provider is down. Suppress the exec mail and
        # fire the missing-fetch alert (the original hard-skip behaviour).
        gaps = missing_fetch_gaps(window_start, window_end)
        gap_repr = [f"{d.isoformat()}/{p}" for (d, p) in gaps]
        logger.warning(
            f"[orchestrator] {cadence} digest skipped — ALL providers down "
            f"after API refresh: down={down} gaps={gap_repr}"
        )
        alert_missing_fetch(cadence, period_label, window_start, window_end, gaps)
        # Back-fill the claim row: we won the send race but legitimately sent
        # nothing (total outage). Recording smtp_ok=False keeps the audit row
        # accurate and the alert path (separate dedup key) still fires above.
        record_digest_send(claim_id, recipients=0, smtp_ok=False)
        return False

    if down:
        # PARTIAL outage — ship the digest with the surviving providers but
        # warn the on-call list which provider(s) were dropped.
        logger.warning(
            f"[orchestrator] {cadence} digest shipping WITHOUT down providers "
            f"{down} (partial outage) for {window_start}..{window_end}"
        )
        alert_partial_fetch(cadence, period_label, window_start, window_end, down)

    svg = sparkline_svg(report.daily_series)
    png_b64 = svg_to_png_base64(svg)
    ascii_spark = sparkline_ascii(report.daily_series)

    ctx = {
        "report":                  report,
        "sparkline_png":           png_b64,
        "sparkline_ascii":         ascii_spark,
        "model_breakdown_charts":  model_breakdown_charts_png(report.model_breakdown),
        "generated_at":            datetime.now(tz=_TZ).strftime("%Y-%m-%d %H:%M %Z"),
        # Stale-data banner inputs. stale_providers is the providers whose
        # numbers may still be settling (e.g. Gemini before GCP's export lands).
        "stale_providers":         report.stale_providers,
        "source_of_truth":         report.source_of_truth,
    }

    html_body = _jinja.get_template(template_html).render(**ctx)
    text_body = _jinja.get_template(template_txt).render(**ctx)

    try:
        ok = send_html_email(
            to        = to,
            cc        = cc or None,
            bcc       = bcc or None,
            subject   = subject,
            html_body = html_body,
            text_body = text_body,
        )
    except Exception as e:
        logger.error(f"[orchestrator] {cadence} send failed: {e}")
        record_digest_send(claim_id, recipients=len(to), smtp_ok=False)
        return False

    if ok:
        logger.info(
            f"[orchestrator] {cadence} digest sent — To: {len(to)} recipients, "
            f"Cc: {len(cc)} admin(s), Bcc: {len(bcc)} addr(s) ({period_label})"
        )
    else:
        logger.error(f"[orchestrator] {cadence} digest SMTP returned False")
    record_digest_send(claim_id, recipients=len(to), smtp_ok=bool(ok))
    return bool(ok)


def send_daily_digest(for_date: Optional[date] = None) -> bool:
    d = for_date or _yesterday()
    label = d.strftime("%A, %d %b %Y")

    # Multi-worker safe: claim the whole daily-digest operation up front so
    # exactly ONE worker runs the pre-refresh + down-check + send for this
    # date. Without this, all N workers would each run the pre-digest
    # run_fetch_window() (N× provider API calls at 10:00) and race the
    # down_providers() check against half-written rows. We use a 'digest_sent'
    # claim keyed on the daily period_label — the SAME key _send_digest would
    # otherwise claim — so the winner here is also the send winner and
    # _send_digest's internal claim becomes a harmless no-op (it re-claims the
    # already-owned key, gets None, and we skip it via refetch handling below).
    # Losers no-op and report success (the digest IS being produced by the
    # winner). Fails OPEN on a DB error.
    claim_id = claim_digest_send(
        cadence="daily",
        window_start=d, window_end=d,
        dedup_key=label,
    )
    if claim_id is None:
        logger.info(
            f"[orchestrator] daily digest for {label} already claimed by "
            f"another worker; skipping duplicate"
        )
        return True

    # DAILY hard-skip policy (revised 2026-06-27): cancel ONLY when EVERY
    # provider is down for this date. A partial failure (one or two providers
    # failed, at least one OK) must still ship the digest with the surviving
    # providers — the partial-outage alert is fired by _send_digest below.
    # We deliberately do NOT apply the total-cancel-on-any-failure check to
    # weekly/monthly/quarterly cadences either; all cadences now share the
    # same "ship unless everything is down" contract.
    #
    # Note: failed_fetch_runs / down_providers use "latest-attempt" semantics,
    # so a failure that is corrected by the build()-triggered re-fetch will be
    # hidden. We therefore perform the refresh ourselves first so the
    # subsequent down-provider check sees the post-refresh state. We use
    # run_fetch_window() rather than build() to avoid building a doomed report
    # just to discard it.
    refresh_summary = run_fetch_window(d, d)
    logger.info(f"[orchestrator] daily pre-digest refresh: {refresh_summary}")

    down = down_providers(d, d)
    if down and len(down) >= len(REQUIRED_PROVIDERS):
        # TOTAL outage — every provider is down for today. Cancel the exec
        # digest and fire the daily failed-fetch alert (richer per-run detail
        # than the generic missing-fetch alert).
        fails = failed_fetch_runs(d, d)
        logger.warning(
            f"[orchestrator] daily digest cancelled — ALL providers down: "
            f"{down} for {d.isoformat()}"
        )
        alert_failed_fetch(
            period_label=label,
            window_start=d, window_end=d,
            failed_runs=fails,
        )
        # We own the digest_sent claim (claimed up front); back-fill it as
        # "won the race but sent nothing" so the audit row is accurate.
        record_digest_send(claim_id, recipients=0, smtp_ok=False)
        return False

    # Partial outage (if any) is handled inside _send_digest, which ships the
    # digest with surviving providers and fires alert_partial_fetch. We pass
    # refetch=False since we just refreshed above, and hand in the claim_id we
    # already won so _send_digest does not re-claim (which would self-conflict).
    return _send_digest(
        cadence="daily",
        window_start=d, window_end=d,
        period_label=label,
        template_html="llm_spend_period_report.html.j2",
        template_txt ="llm_spend_period_report.txt.j2",
        subject=f"AiNxt LLM Spend — Daily Digest for {label}",
        # Already refreshed above for the failed_fetch_runs gate; avoid
        # a redundant second round-trip inside build().
        refetch=False,
        claim_id=claim_id,
    )


def send_weekly_digest(week_start: Optional[date] = None) -> bool:
    if week_start is None:
        ws, we = _previous_week_window()
    else:
        if week_start.weekday() != 0:
            raise ValueError("week_start must be a Monday")
        ws = week_start
        we = week_start + timedelta(days=6)
    label = f"{ws.strftime('%d %b')} – {we.strftime('%d %b %Y')}"
    return _send_digest(
        cadence="weekly",
        window_start=ws, window_end=we,
        period_label=label,
        template_html="llm_spend_period_report.html.j2",
        template_txt ="llm_spend_period_report.txt.j2",
        subject=f"AiNxt LLM Spend — Weekly Digest ({label})",
    )


def send_monthly_digest(month: Optional[str] = None) -> bool:
    """`month` format YYYY-MM (refers to the period itself, not 'as-of')."""
    if month is None:
        ws, we = _previous_month_window()
    else:
        y, m = month.split("-")
        ws = date(int(y), int(m), 1)
        we = date(int(y), int(m), calendar.monthrange(int(y), int(m))[1])
    label = ws.strftime("%B %Y")
    return _send_digest(
        cadence="monthly",
        window_start=ws, window_end=we,
        period_label=label,
        template_html="llm_spend_period_report.html.j2",
        template_txt ="llm_spend_period_report.txt.j2",
        subject=f"AiNxt LLM Spend — Monthly Report ({label})",
    )


def send_quarterly_digest(quarter: Optional[str] = None) -> bool:
    """`quarter` format YYYY-Q[1-4]."""
    if quarter is None:
        ws, we = _previous_quarter_window()
    else:
        y, q = quarter.split("-Q")
        y = int(y); q = int(q)
        if q not in (1, 2, 3, 4):
            raise ValueError("quarter must be 1..4")
        first_month = 3 * (q - 1) + 1
        ws = date(y, first_month, 1)
        last_month = first_month + 2
        we = date(y, last_month, calendar.monthrange(y, last_month)[1])
    label = f"Q{((we.month - 1)//3)+1} {we.year}"
    sent = _send_digest(
        cadence="quarterly",
        window_start=ws, window_end=we,
        period_label=label,
        template_html="llm_spend_period_report.html.j2",
        template_txt ="llm_spend_period_report.txt.j2",
        subject=f"AiNxt LLM Spend — Quarterly Report ({label})",
    )

    if sent:
        current_q_start = we + timedelta(days=1)
        prune_claim = claim_fetch_run(
            cadence="prune",
            window_start=current_q_start, window_end=current_q_start,
            dedup_key=f"prune-{current_q_start.isoformat()}",
        )
        if prune_claim is not None:
            prune_fetch_runs(before=current_q_start)
        else:
            logger.info(
                f"[orchestrator] quarter-end prune for {current_q_start} already "
                f"claimed by another worker; skipping duplicate prune"
            )

    return sent


# ── dry-run helpers for admin endpoints ────────────────────────────────────

def render_for_dry_run(
    cadence: str,
    window_start: date, window_end: date,
    period_label: str,
    refetch: bool = True,
) -> dict:
    """Return rendered HTML + recipient list without sending.

    Defaults to refetch=True so dry-runs reflect the same API-primary
    source-of-truth ordering as a live send. Pass refetch=False from
    admin endpoints that want a pure DB-sourced preview (e.g. when
    debugging template changes without hammering provider APIs).
    """
    report = build_report(
        cadence, window_start, window_end, period_label, refetch=refetch
    )
    template_html = "llm_spend_period_report.html.j2"
    svg = sparkline_svg(report.daily_series)
    ctx = {
        "report":                  report,
        "sparkline_png":           svg_to_png_base64(svg),
        "sparkline_ascii":         sparkline_ascii(report.daily_series),
        "model_breakdown_charts":  model_breakdown_charts_png(report.model_breakdown),
        "generated_at":            datetime.now(tz=_TZ).strftime("%Y-%m-%d %H:%M %Z"),
        "stale_providers":         report.stale_providers,
        "source_of_truth":         report.source_of_truth,
    }
    html = _jinja.get_template(template_html).render(**ctx)
    to = resolve_digest_to(cadence)
    cc: List[str] = []
    bcc = resolve_digest_bcc(cadence)
    gaps = missing_fetch_gaps(window_start, window_end)
    return {
        "to":              to,
        "cc":              cc,
        "bcc":             bcc,
        "recipients":      to,        # back-compat alias
        "missing_dates":   sorted({d.isoformat() for (d, _p) in gaps}),
        "missing_gaps":    [f"{d.isoformat()}/{p}" for (d, p) in gaps],
        "html":            html,
        "total_cost_usd":  str(report.total_cost_usd),
    }
