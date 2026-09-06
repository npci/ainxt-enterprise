# SPDX-License-Identifier: MIT
# ============================================================
# services.llm_spend.gateway_bootstrap
#
# Owns the AsyncIOScheduler instance for the LLM-spend feature.
# Called by gateway.py's startup/shutdown hooks.
#
# Jobs registered:
#   * llm_spend_daily_fetch         — 21:00 IST (default): fetch TODAY's spend
#                                     for all providers, then run an overnight
#                                     loop that keeps re-fetching Gemini until
#                                     GCP's billing export settles (or 06:00).
#   * llm_spend_digest_daily        — 06:00 IST (default): report YESTERDAY,
#                                     shipping with a stale-data banner if a
#                                     provider (typically Gemini) hasn't settled.
#   * llm_spend_digest_weekly       — 10:00 IST Mon (default)
#   * llm_spend_digest_monthly      — 10:00 IST on the 1st (default)
#   * llm_spend_digest_quarterly    — 10:00 IST on 1st Jan/Apr/Jul/Oct
#
# Why 21:00 fetch / 06:00 send: GCP's BigQuery billing export lags 6–24h, so a
# same-evening Gemini fetch is incomplete. Fetching at 21:00 and letting the
# Gemini settle loop run overnight maximises the chance the prior day's Gemini
# spend has landed by the 06:00 send; the banner covers the residual case.
#
# Times are all env-overridable. TZ defaults to Asia/Kolkata.
#
# Also runs a one-shot 90-day backfill in a background task if
# llm_spend_daily is empty (first-deploy behaviour).
# ============================================================

from __future__ import annotations

import asyncio
import os
import threading
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from core.logger import logger
from services.llm_spend import orchestrator


_scheduler: Optional[AsyncIOScheduler] = None
_lock = threading.Lock()


def _tz() -> str:
    return os.getenv("LLM_SPEND_TZ", "Asia/Kolkata")


def _i(env_name: str, default: int) -> int:
    try:
        return int(os.getenv(env_name, str(default)))
    except Exception:
        return default


# ── job wrappers ───────────────────────────────────────────────────────────
# APScheduler can call sync callables on the asyncio loop. We use thin
# wrappers so we can log fire-events with consistent prefixes.

def _job_fetch() -> None:
    logger.info("[llm_spend] cron fire: evening_fetch")
    try:
        # 21:00 — fetch today's spend and launch the overnight Gemini settle
        # loop. run_evening_fetch is multi-worker safe (claims the run).
        orchestrator.run_evening_fetch()
    except Exception as e:
        logger.error(f"[llm_spend] evening_fetch crashed: {e}")


def _job_daily_digest() -> None:
    logger.info("[llm_spend] cron fire: daily_digest")
    try:
        orchestrator.send_daily_digest()
    except Exception as e:
        logger.error(f"[llm_spend] daily_digest crashed: {e}")


def _job_weekly_digest() -> None:
    logger.info("[llm_spend] cron fire: weekly_digest")
    try:
        orchestrator.send_weekly_digest()
    except Exception as e:
        logger.error(f"[llm_spend] weekly_digest crashed: {e}")


def _job_monthly_digest() -> None:
    logger.info("[llm_spend] cron fire: monthly_digest")
    try:
        orchestrator.send_monthly_digest()
    except Exception as e:
        logger.error(f"[llm_spend] monthly_digest crashed: {e}")


def _job_quarterly_digest() -> None:
    logger.info("[llm_spend] cron fire: quarterly_digest")
    try:
        orchestrator.send_quarterly_digest()
    except Exception as e:
        logger.error(f"[llm_spend] quarterly_digest crashed: {e}")


# ── public ─────────────────────────────────────────────────────────────────

def start() -> None:
    """Idempotent — safe to call twice."""
    global _scheduler
    with _lock:
        if _scheduler is not None:
            logger.info("[llm_spend] scheduler already running")
            return

        tz = _tz()
        scheduler = AsyncIOScheduler(timezone=tz)

        scheduler.add_job(
            _job_fetch,
            CronTrigger(
                hour=_i("LLM_SPEND_FETCH_HOUR",   21),
                minute=_i("LLM_SPEND_FETCH_MINUTE", 0),
                timezone=tz,
            ),
            id="llm_spend_daily_fetch",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )

        scheduler.add_job(
            _job_daily_digest,
            CronTrigger(
                hour=_i("LLM_SPEND_DIGEST_DAILY_HOUR",   6),
                minute=_i("LLM_SPEND_DIGEST_DAILY_MINUTE", 0),
                timezone=tz,
            ),
            id="llm_spend_digest_daily",
            replace_existing=True,
            misfire_grace_time=7200,
            coalesce=True,
            max_instances=1,
        )

        scheduler.add_job(
            _job_weekly_digest,
            CronTrigger(
                day_of_week="mon",
                hour=_i("LLM_SPEND_DIGEST_WEEKLY_HOUR",   10),
                minute=_i("LLM_SPEND_DIGEST_WEEKLY_MINUTE", 0),
                timezone=tz,
            ),
            id="llm_spend_digest_weekly",
            replace_existing=True,
            misfire_grace_time=21600,
            coalesce=True,
            max_instances=1,
        )

        scheduler.add_job(
            _job_monthly_digest,
            CronTrigger(
                day=1,
                hour=_i("LLM_SPEND_DIGEST_MONTHLY_HOUR",   10),
                minute=_i("LLM_SPEND_DIGEST_MONTHLY_MINUTE", 0),
                timezone=tz,
            ),
            id="llm_spend_digest_monthly",
            replace_existing=True,
            misfire_grace_time=21600,
            coalesce=True,
            max_instances=1,
        )

        scheduler.add_job(
            _job_quarterly_digest,
            CronTrigger(
                month="1,4,7,10",
                day=1,
                hour=_i("LLM_SPEND_DIGEST_QUARTERLY_HOUR",   10),
                minute=_i("LLM_SPEND_DIGEST_QUARTERLY_MINUTE", 0),
                timezone=tz,
            ),
            id="llm_spend_digest_quarterly",
            replace_existing=True,
            misfire_grace_time=43200,
            coalesce=True,
            max_instances=1,
        )

        scheduler.start()
        _scheduler = scheduler
        logger.info(
            f"[llm_spend] scheduler started in {tz} with 5 jobs: "
            f"daily_fetch, digest_{{daily,weekly,monthly,quarterly}}"
        )

    # ── 90-day backfill (first deploy only; idempotent thereafter) ─────────
    # Runs in a background thread to avoid blocking uvicorn startup. The
    # backfill itself does up to 3 long HTTP calls + a BigQuery query, so
    # we don't want it in the synchronous boot path.
    days = _i("LLM_SPEND_BACKFILL_DAYS", 90)

    def _bg_backfill():
        try:
            res = orchestrator.backfill_if_empty(days=days)
            if res is None:
                logger.info("[llm_spend] backfill skipped (table not empty or probe failed)")
            else:
                logger.info(f"[llm_spend] backfill completed: {res}")
        except Exception as e:
            logger.error(f"[llm_spend] backfill crashed: {e}")

    threading.Thread(target=_bg_backfill, name="llm_spend_backfill", daemon=True).start()


def stop() -> None:
    """Idempotent shutdown."""
    global _scheduler
    with _lock:
        if _scheduler is None:
            return
        try:
            _scheduler.shutdown(wait=False)
        except Exception as e:
            logger.warning(f"[llm_spend] scheduler shutdown error: {e}")
        _scheduler = None
