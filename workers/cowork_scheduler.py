#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# ============================================================
# COWORK SCHEDULER — fires recurring Cowork tasks
#
# Recurring "Cowork" tasks let a user schedule an agent run on a cron
# cadence (e.g. "every Mon 9:00 — email me a calendar digest").
# Definitions live in Postgres table `cowork_scheduled_tasks`; this
# process polls for due rows and enqueues each one onto the RQ
# `connector_queue` so it is executed by
# `workers.cowork_task_worker.run_scheduled_task`.
#
# Scheduling backend selection (best-available, no behaviour change):
#   1. rq-scheduler  (from rq_scheduler import Scheduler)  — if installed
#   2. APScheduler   (BlockingScheduler)                    — fallback
#   3. built-in croniter poll loop                          — last resort
#
# Regardless of backend, the SOURCE OF TRUTH for *what* runs and *when*
# is the Postgres table (cron + next_run + active). The backend is only a
# heartbeat that wakes us up to re-evaluate due rows; we never store task
# state inside the scheduler. This keeps multi-host deployments safe and
# means a scheduler restart never loses or double-fires a task (the row's
# next_run is advanced atomically before enqueue).
#
# AiNxt guardrails honoured here:
#   - This scheduler ONLY enqueues a job. It never executes connector/doc
#     WRITES or sends. The downstream worker
#     (workers.cowork_task_worker.run_scheduled_task) is responsible for
#     running the agent; any outbound write/send it produces must still go
#     through the existing confirm + compliance-gated path
#     (POST /connectors/action, workers/doc_worker.py). Nothing is
#     auto-executed from here.
#   - Never log prompts in full, secrets, tokens, or connector payloads.
#
# Run (PM2-managed in prod; never systemd):
#   python workers/cowork_scheduler.py
#
# Required env:
#   POSTGRES_HOST / POSTGRES_PORT / POSTGRES_DB / POSTGRES_USER /
#   POSTGRES_PASSWORD / POSTGRES_SCHEMA          (platform DB — task table)
#   REDIS_HOST / REDIS_PORT                       (RQ broker, db=5)
#   BUDDY_SCHED_POLL_SECONDS   (optional, default 30) — poll cadence
#   BUDDY_SCHED_BATCH          (optional, default 50) — max rows per tick
# ============================================================

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

# Add project root to path so imports work when launched directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env before any project imports so os.getenv() sees correct values.
try:
    from dotenv import load_dotenv
    load_dotenv(
        dotenv_path=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
        ),
        override=True,
    )
except ImportError:
    pass

from core.logger import logger
from core.job_queue import enqueue_job, Q_CONNECTOR

# Worker entrypoint the enqueued jobs resolve to (dotted path).
_TARGET_FN = "workers.cowork_task_worker.run_scheduled_task"

_POLL_SECONDS = int(os.getenv("BUDDY_SCHED_POLL_SECONDS", "30"))
_BATCH        = int(os.getenv("BUDDY_SCHED_BATCH", "50"))


# ── Postgres helpers ──────────────────────────────────────────

def _pg():
    """Return a raw psycopg2 connection to the platform database.

    Uses the same DSN helper as the rest of the platform so the
    search_path (ainxt schema) is applied consistently.
    """
    import psycopg2
    from core.config import postgres_dsn
    return psycopg2.connect(postgres_dsn(), connect_timeout=5)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _next_run_utc(cron_expr: str, base_utc: datetime, tz: str = "UTC") -> datetime | None:
    """Next fire time strictly after `base_utc`, with the cron interpreted in `tz`.

    Returns a tz-aware UTC datetime, or None for an invalid cron / tz.

    We deliberately run croniter on a NAIVE local wall-clock datetime and do the
    timezone attach/convert ourselves with `zoneinfo`. croniter (6.2.x) does not
    reliably anchor a `ZoneInfo`-aware base to the intended offset, so handing it
    a tz-aware base made '20 14 * * *' come back as 14:20 *UTC* instead of 14:20
    in the task's zone. Feeding croniter naive wall-clock time (which it handles
    correctly) and re-attaching the real zone afterwards fixes that.
    """
    try:
        from croniter import croniter
        if not croniter.is_valid(cron_expr):
            return None
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(tz or "UTC")
        except Exception:
            zone = timezone.utc
        # 1) express the base as a NAIVE local wall clock (croniter-safe)
        base_local_naive = base_utc.astimezone(zone).replace(tzinfo=None)
        # 2) croniter does pure wall-clock math on the naive value
        nxt_naive = croniter(cron_expr, base_local_naive).get_next(datetime)
        # 3) attach the REAL zone, then convert to UTC ourselves
        nxt_local = nxt_naive.replace(tzinfo=zone)
        return nxt_local.astimezone(timezone.utc)
    except Exception as e:
        logger.warning(f"cowork next_run: bad cron/tz (id-redacted): {e}")
        return None


def _parse_monthly_dom(cron_expr: str) -> int | None:
    """If `cron_expr` is a plain monthly cron (``M H D * *`` with a numeric DOM
    and wildcard DOW), return the configured day-of-month (1–31).  Otherwise
    return None so the caller falls back to the standard croniter path.

    We only intercept the simple "Day N of every month" pattern that the
    Buddy scheduler UI emits (``M H D */N *``).  Weekday-based monthly crons
    (``M H * * DOW#n``) are left to croniter unchanged.
    """
    parts = (cron_expr or "").strip().split()
    if len(parts) != 5:
        return None
    _m, _h, dom, _mon, dow = parts
    # Must be a numeric DOM, wildcard or step-based month field, and wildcard DOW.
    if not dom.isdigit():
        return None
    if dow != "*":
        return None
    day = int(dom)
    return day if 29 <= day <= 31 else None


def _next_run_month_end_aware(
    cron_expr: str, base_utc: datetime, tz: str = "UTC"
) -> datetime | None:
    """Compute next_run for monthly crons whose DOM is 29, 30, or 31.

    Standard croniter skips months that don't have that day (e.g. ``0 9 31 * *``
    never fires in April).  The Buddy scheduler requirement is different: if the
    user configured day 31 and the next month only has 30 days, fire on the 30th
    (i.e. the last valid day of that month).

    Algorithm:
      1. Walk forward month by month from `base_utc`.
      2. For each candidate month compute ``min(configured_dom, days_in_month)``.
      3. Build a candidate fire datetime at the configured H:M in the task's tz.
      4. Return the first candidate that is strictly after `base_utc`.

    Falls back to the standard croniter path for any parse/tz error.
    """
    import calendar

    configured_dom = _parse_monthly_dom(cron_expr)
    if configured_dom is None:
        # Not a simple monthly-DOM cron — use the standard path.
        return _next_run_utc(cron_expr, base_utc, tz)

    parts = cron_expr.strip().split()
    try:
        fire_minute = int(parts[0])
        fire_hour   = int(parts[1])
    except (ValueError, IndexError):
        return _next_run_utc(cron_expr, base_utc, tz)

    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz or "UTC")
    except Exception:
        zone = timezone.utc

    # Express base in the task's local timezone so month arithmetic is correct.
    base_local = base_utc.astimezone(zone)

    # Search up to 36 months ahead (safety cap — should always find one sooner).
    year  = base_local.year
    month = base_local.month
    for _ in range(36):
        days_in_month = calendar.monthrange(year, month)[1]
        actual_dom    = min(configured_dom, days_in_month)
        try:
            candidate_local = datetime(
                year, month, actual_dom,
                fire_hour, fire_minute, 0,
                tzinfo=zone,
            )
        except ValueError:
            # Shouldn't happen after the min() clamp, but be safe.
            candidate_local = None

        if candidate_local is not None and candidate_local.astimezone(timezone.utc) > base_utc:
            return candidate_local.astimezone(timezone.utc)

        # Advance to next month.
        month += 1
        if month > 12:
            month = 1
            year += 1

    logger.warning("cowork next_run: could not find next monthly fire within 36 months")
    return None


def _compute_next_run(cron_expr: str, base: datetime, tz: str = "UTC") -> datetime | None:
    """Return the next fire time strictly after `base` for a cron expression,
    interpreting the cron in the task's TIMEZONE (so '15 1 * * *' means 01:15 in
    the user's local time, not UTC). Returns a tz-aware UTC datetime, or None for
    an invalid cron / tz (the row is then de-scheduled to avoid a hot-loop).

    For monthly crons with DOM 29–31 the standard croniter path is replaced by
    a month-end-aware helper: if the configured day exceeds the number of days in
    a given month the task fires on the last day of that month instead of being
    skipped entirely (e.g. day 31 fires on 30 Apr, 28/29 Feb, etc.).
    """
    if _parse_monthly_dom(cron_expr) is not None:
        return _next_run_month_end_aware(cron_expr, base, tz)
    return _next_run_utc(cron_expr, base, tz)


# ── Recurrence gates (2026-08-10) ─────────────────────────────
#
# The Outlook-style Recurrence editor gives users three knobs that cron alone
# can't express: a start date, an end date OR max-occurrence count, and a
# custom multi-week / multi-month interval ("every 2 weeks", "every 3 months").
# The helpers below let the fire loop honour all three without touching the
# per-task cron string. See routers/cowork_tasks_router.py for the columns.

def _in_start_window(starts_at: datetime | None, now: datetime) -> bool:
    """False → the schedule hasn't started yet; skip this fire."""
    return not (starts_at is not None and now < starts_at)


def _range_exhausted(
    ends_at: datetime | None,
    max_runs: int | None,
    runs_count: int,
    now: datetime,
) -> bool:
    """True → the recurrence range is complete; row should be marked `completed`."""
    if ends_at is not None and now >= ends_at:
        return True
    if max_runs is not None and runs_count >= max_runs:
        return True
    return False


def _weeks_between(a: datetime, b: datetime) -> int:
    """Whole ISO weeks between two UTC-aware datetimes (a earlier, b later).

    Anchors both sides to Monday 00:00 of their ISO week so intra-week times
    don't perturb the count — the "every N weeks" cadence is a cycle count,
    not a wall-clock delta.
    """
    from datetime import timedelta

    def _monday(d: datetime) -> datetime:
        d = d.astimezone(timezone.utc)
        midnight = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        return midnight - timedelta(days=midnight.weekday())

    return int((_monday(b) - _monday(a)).days // 7)


def _months_between(a: datetime, b: datetime) -> int:
    """Whole calendar months between two UTC-aware datetimes (a earlier, b later)."""
    a = a.astimezone(timezone.utc)
    b = b.astimezone(timezone.utc)
    return (b.year - a.year) * 12 + (b.month - a.month)


def _interval_matches_this_cycle(
    starts_at: datetime | None,
    interval_weeks: int | None,
    interval_months: int | None,
    now: datetime,
) -> bool:
    """True → this fire is on an N-cycle boundary and should proceed. When the
    task has no custom interval, this returns True unconditionally.

    We intentionally use `starts_at` (falling back to `now` if unset) as the
    anchor so the cadence is deterministic across process restarts and multi-
    host schedulers — the Nth cycle offset is a function of the anchor and the
    current time only, not of past fires.
    """
    if interval_weeks and interval_weeks > 1:
        anchor = starts_at or now
        return (_weeks_between(anchor, now) % interval_weeks) == 0
    if interval_months and interval_months > 1:
        anchor = starts_at or now
        return (_months_between(anchor, now) % interval_months) == 0
    return True


def _in_time_window(recurrence: dict | None, now: datetime, tz: str = "UTC") -> bool:
    """True → the current time is within the user-configured time window.

    The time window (recurrence.window) is a UI concept for minutely/hourly
    tasks: "Only between HH:MM and HH:MM". It is encoded into the cron string
    by the frontend, but we also enforce it here as a server-side safety net
    to handle DST edge cases, manually edited cron strings, or any future
    frontend bugs.

    Returns True unconditionally when:
      - recurrence is None/missing (legacy rows without the recurrence column)
      - window.enabled is False or absent
      - pattern is not 'minutely' or 'hourly' (window only applies to sub-daily)
    """
    if not recurrence or not isinstance(recurrence, dict):
        return True
    window = recurrence.get("window", {})
    if not window or not window.get("enabled"):
        return True
    pattern = recurrence.get("pattern", "")
    if pattern not in ("minutely", "hourly"):
        return True

    win_start = window.get("start", "00:00")
    win_end   = window.get("end",   "23:59")

    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz or "UTC")
    except Exception:
        zone = timezone.utc

    now_local = now.astimezone(zone)
    now_minutes = now_local.hour * 60 + now_local.minute

    def _parse_hm(t: str) -> int:
        parts = (t or "00:00").split(":")
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            return 0

    start_minutes = _parse_hm(win_start)
    end_minutes   = _parse_hm(win_end)

    return start_minutes <= now_minutes <= end_minutes


# ── Core: claim + enqueue due tasks ───────────────────────────

def _fire_due_tasks() -> int:
    """Find due rows, advance their next_run atomically, and enqueue each.

    Returns the number of tasks fired. Designed to be safe under multiple
    scheduler instances: each row is locked FOR UPDATE SKIP LOCKED and its
    next_run is advanced *before* the job is enqueued, so two schedulers can
    never fire the same occurrence twice and a crash mid-tick re-fires at
    most one occurrence (idempotency belongs to the worker).
    """
    fired = 0
    conn = None
    try:
        conn = _pg()
        conn.autocommit = False
        with conn.cursor() as cur:
            # BOOTSTRAP: newly-created tasks are inserted with next_run = NULL and
            # nothing else computes the FIRST fire time — without this they would
            # never become 'due' and never run. Compute the initial next_run from
            # the cron for any active task missing it. (Invalid cron → leave NULL.)
            #
            # Two recurrence-aware corrections applied here:
            #   1. Use max(now, starts_at) as the base so the first fire is on
            #      or after the configured start date, not just "after now".
            #   2. If the computed next_run already exceeds ends_at the task can
            #      never fire within its valid range — mark it completed immediately
            #      rather than leaving a misleading future next_run in the DB.
            cur.execute(
                "SELECT id, cron, COALESCE(tz,'UTC'), starts_at, ends_at "
                "FROM cowork_scheduled_tasks "
                "WHERE status = 'active' AND next_run IS NULL "
                "FOR UPDATE SKIP LOCKED"
            )
            for (bid, bcron, btz, b_starts_at, b_ends_at) in cur.fetchall():
                now_utc = _now_utc()
                # Anchor to starts_at when it's in the future so the first
                # next_run respects the configured range start date.
                base = max(now_utc, b_starts_at) if b_starts_at else now_utc
                nr = _compute_next_run(bcron or "", base, btz)
                if nr is None:
                    continue  # invalid cron — leave next_run NULL, operator must fix
                # If the first valid fire time is already past ends_at, the
                # task can never fire within its range — complete it now.
                if b_ends_at is not None and nr > b_ends_at:
                    cur.execute(
                        """
                        UPDATE cowork_scheduled_tasks
                           SET status = 'completed', next_run = NULL, updated_at = NOW()
                         WHERE id = %s
                        """,
                        (bid,),
                    )
                    logger.info(
                        f"cowork_scheduler: task {bid} immediately completed at bootstrap "
                        f"(first next_run={nr.isoformat()} > ends_at={b_ends_at.isoformat()})"
                    )
                else:
                    cur.execute(
                        "UPDATE cowork_scheduled_tasks SET next_run = %s WHERE id = %s",
                        (nr, bid),
                    )
                    logger.info(f"cowork_scheduler: bootstrapped next_run for task {bid} → {nr.isoformat()}")
            conn.commit()

            # Claim a batch of due, active tasks. SKIP LOCKED lets parallel
            # schedulers split the workload without contention.
            #
            # Also fetches the recurrence columns (2026-08-10) so the gates
            # below can decide whether to actually enqueue this fire, mark the
            # row complete, or silently advance next_run and skip.
            cur.execute(
                """
                SELECT id, user_id, role, prompt, cron, connectors, COALESCE(tz,'UTC'),
                       starts_at, ends_at, max_runs, runs_count,
                       interval_weeks, interval_months, recurrence
                  FROM cowork_scheduled_tasks
                 WHERE status = 'active'
                   AND next_run IS NOT NULL
                   AND next_run <= %s
                 ORDER BY next_run ASC
                 LIMIT %s
                 FOR UPDATE SKIP LOCKED
                """,
                (_now_utc(), _BATCH),
            )
            rows = cur.fetchall()

            for (
                task_id, user_id, role, prompt, cron_expr, connectors, task_tz,
                starts_at, ends_at, max_runs, runs_count,
                interval_weeks, interval_months, recurrence,
            ) in rows:
                now = _now_utc()
                next_run = _compute_next_run(cron_expr or "", now, task_tz)

                # Pre-compute whether the NEXT cron tick would already fall
                # past ends_at.  If so, this is the last valid fire — we must
                # write status='completed' / next_run=NULL instead of a future
                # date that the UI would incorrectly display as a pending run.
                # (Mirrors the identical guard in the bootstrap section above.)
                _exhausted_after = (
                    next_run is not None
                    and ends_at is not None
                    and next_run > ends_at
                )

                if next_run is None:
                    # Invalid cron — stop scheduling it (operator must fix).
                    cur.execute(
                        """
                        UPDATE cowork_scheduled_tasks
                           SET next_run = NULL, last_run = %s
                         WHERE id = %s
                        """,
                        (now, task_id),
                    )
                    logger.warning(
                        f"cowork_scheduler: de-scheduled task {task_id} "
                        f"(invalid cron) for user={user_id}"
                    )
                    continue

                # ── Range gate ────────────────────────────────────────────
                # If the recurrence has an end date or a max-occurrence limit
                # that's already exhausted, transition to `completed` and stop.
                # This is the ONE spot in the system that writes `completed` —
                # the router refuses to write it and the UI never sees it as a
                # writable state. See routers/cowork_tasks_router.py.
                if _range_exhausted(ends_at, max_runs, int(runs_count or 0), now):
                    cur.execute(
                        """
                        UPDATE cowork_scheduled_tasks
                           SET status = 'completed', next_run = NULL, updated_at = NOW()
                         WHERE id = %s
                        """,
                        (task_id,),
                    )
                    logger.info(
                        f"cowork_scheduler: task {task_id} completed "
                        f"(ends_at={ends_at.isoformat() if ends_at else None}, "
                        f"runs={runs_count}/{max_runs})"
                    )
                    continue

                # ── Start-window gate ────────────────────────────────────
                # If starts_at hasn't been reached yet, don't fire — just
                # advance next_run so we'll try again on the next tick past
                # starts_at. This shouldn't normally happen because next_run
                # is bootstrapped from cron, but a user can edit starts_at
                # forward and we must respect that without losing the row.
                if not _in_start_window(starts_at, now):
                    # If cron's next occurrence is still before starts_at, we
                    # push next_run to starts_at itself so the row remains
                    # dormant until then instead of hot-looping.
                    resume_at = starts_at if (next_run < starts_at) else next_run
                    cur.execute(
                        "UPDATE cowork_scheduled_tasks SET next_run = %s WHERE id = %s",
                        (resume_at, task_id),
                    )
                    continue

                # ── Interval-cycle gate ──────────────────────────────────
                # "Every N weeks" / "Every N months" — cron fires every cycle
                # by design; here we skip the ones that don't fall on the
                # N-cycle boundary. We still advance next_run to the cron's
                # next tick so we're re-evaluated at each cycle.
                fires_this_cycle = _interval_matches_this_cycle(
                    starts_at, interval_weeks, interval_months, now
                )

                # Advance schedule state inside the same transaction that
                # holds the row lock — commit gates the enqueue below.
                # runs_count only bumps when we actually enqueue (skipped
                # cycles from the interval gate don't count as occurrences).
                if fires_this_cycle:
                    if _exhausted_after:
                        # This is the last valid fire — complete the task now
                        # so the UI never shows a misleading future next_run.
                        cur.execute(
                            """
                            UPDATE cowork_scheduled_tasks
                               SET last_run   = %s,
                                   next_run   = NULL,
                                   runs_count = runs_count + 1,
                                   status     = 'completed',
                                   updated_at = NOW()
                             WHERE id = %s
                            """,
                            (now, task_id),
                        )
                        logger.info(
                            f"cowork_scheduler: task {task_id} completed after last run "
                            f"(next_run {next_run.isoformat()} > ends_at {ends_at.isoformat()})"
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE cowork_scheduled_tasks
                               SET last_run   = %s,
                                   next_run   = %s,
                                   runs_count = runs_count + 1
                             WHERE id = %s
                            """,
                            (now, next_run, task_id),
                        )
                else:
                    if _exhausted_after:
                        # Skipped cycle but no future valid run exists — complete.
                        cur.execute(
                            """
                            UPDATE cowork_scheduled_tasks
                               SET next_run   = NULL,
                                   status     = 'completed',
                                   updated_at = NOW()
                             WHERE id = %s
                            """,
                            (task_id,),
                        )
                        logger.info(
                            f"cowork_scheduler: task {task_id} completed (skipped cycle) — "
                            f"next_run {next_run.isoformat()} > ends_at {ends_at.isoformat()}"
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE cowork_scheduled_tasks
                               SET next_run = %s
                             WHERE id = %s
                            """,
                            (next_run, task_id),
                        )
                        logger.info(
                            f"cowork_scheduler: task {task_id} skipped this cycle "
                            f"(interval_weeks={interval_weeks}, interval_months={interval_months})"
                        )
                    continue

                # ── Time-window gate ─────────────────────────────────────
                # For minutely/hourly tasks with a time window ("Only between
                # HH:MM and HH:MM"), skip this fire if we're outside the
                # window. The cron string already encodes the window via an
                # hour-range field, but this server-side gate is a safety net
                # for DST edge cases and manually edited cron strings.
                if not _in_time_window(recurrence, now, task_tz):
                    cur.execute(
                        "UPDATE cowork_scheduled_tasks SET next_run = %s WHERE id = %s",
                        (next_run, task_id),
                    )
                    logger.info(
                        f"cowork_scheduler: task {task_id} skipped — outside time window "
                        f"(recurrence.window="
                        f"{recurrence.get('window') if recurrence else None})"
                    )
                    continue

                # ── Time-window gate ─────────────────────────────────────
                # For minutely/hourly tasks with a time window ("Only between
                # HH:MM and HH:MM"), skip this fire if we're outside the
                # window. The cron string already encodes the window via an
                # hour-range field, but this server-side gate is a safety net
                # for DST edge cases and manually edited cron strings.
                if not _in_time_window(recurrence, now, task_tz):
                    cur.execute(
                        "UPDATE cowork_scheduled_tasks SET next_run = %s WHERE id = %s",
                        (next_run, task_id),
                    )
                    logger.info(
                        f"cowork_scheduler: task {task_id} skipped — outside time window "
                        f"(recurrence.window="
                        f"{recurrence.get('window') if recurrence else None})"
                    )
                    continue

                # psycopg2 returns jsonb as already-parsed Python objects.
                connectors_val = connectors if connectors is not None else []

                payload = {
                    "task_id":    str(task_id),
                    "user_id":    user_id,
                    "role":       role,
                    "prompt":     prompt,        # passed straight to the worker
                    "connectors": connectors_val,
                    "scheduled":  True,
                    "fired_at":   now.isoformat(),
                }

                try:
                    job_id = enqueue_job(
                        _TARGET_FN,
                        payload,
                        queue_name=Q_CONNECTOR,
                        timeout=600,     # 10 min — a scheduled agent run
                        retry_count=0,   # never auto-retry: a scheduled run that
                                         # partially sent must not silently re-run
                    )
                    fired += 1
                    # Do NOT log prompt / connector contents.
                    logger.info(
                        f"cowork_scheduler: fired task {task_id} user={user_id} "
                        f"role={role} job={job_id} next_run={next_run.isoformat()} "
                        f"runs={(runs_count or 0) + 1}"
                        + (f"/{max_runs}" if max_runs else "")
                    )
                except Exception as enq_err:
                    # Enqueue failed (e.g. queue back-pressure / RQ down).
                    # Roll back so next_run is NOT advanced and we retry the
                    # whole batch next tick. Re-raise to abort this tick.
                    conn.rollback()
                    logger.error(
                        f"cowork_scheduler: enqueue failed for task {task_id} "
                        f"— rolling back tick: {enq_err}"
                    )
                    return fired

            conn.commit()
    except Exception as e:
        logger.error(f"cowork_scheduler: tick failed: {e}")
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return fired


# ── Scheduling backends ───────────────────────────────────────

def _run_with_rq_scheduler() -> bool:
    """Drive ticks via rq-scheduler's periodic job, if installed.

    Returns True if rq-scheduler was used (this function blocks while it
    runs); False if rq-scheduler is unavailable so the caller can fall back.
    """
    try:
        from rq_scheduler import Scheduler  # noqa: F401
    except Exception:
        return False

    try:
        from core.job_queue import _redis_conn, _rq_available
        if not _rq_available or _redis_conn is None:
            logger.warning(
                "cowork_scheduler: rq-scheduler present but RQ/Redis "
                "unavailable — falling back"
            )
            return False

        from rq_scheduler import Scheduler

        scheduler = Scheduler(connection=_redis_conn)
        # Register a single repeating job that performs one due-scan tick.
        # interval=None occurrences run forever. We schedule the module-level
        # _fire_due_tasks (importable by the rq-scheduler runner).
        scheduler.schedule(
            scheduled_time=_now_utc(),
            func=_fire_due_tasks,
            interval=_POLL_SECONDS,
            repeat=None,
            id="cowork_scheduler_tick",
        )
        logger.info(
            f"cowork_scheduler: using rq-scheduler "
            f"(tick every {_POLL_SECONDS}s on {Q_CONNECTOR})"
        )
        # rq-scheduler's run() is a blocking loop that releases due jobs.
        scheduler.run()
        return True
    except Exception as e:
        logger.warning(f"cowork_scheduler: rq-scheduler init failed → {e}; falling back")
        return False


def _run_with_apscheduler() -> bool:
    """Drive ticks via APScheduler's BlockingScheduler, if installed.

    Returns True if APScheduler ran (blocks while running); False otherwise.
    """
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
    except Exception:
        return False

    try:
        sched = BlockingScheduler(timezone="UTC")
        sched.add_job(
            _fire_due_tasks,
            trigger="interval",
            seconds=_POLL_SECONDS,
            id="cowork_scheduler_tick",
            max_instances=1,           # never overlap two ticks
            coalesce=True,             # collapse missed ticks into one
            next_run_time=_now_utc(),  # fire immediately on startup
        )
        logger.info(
            f"cowork_scheduler: using APScheduler "
            f"(tick every {_POLL_SECONDS}s on {Q_CONNECTOR})"
        )
        sched.start()  # blocking
        return True
    except (KeyboardInterrupt, SystemExit):
        return True
    except Exception as e:
        logger.warning(f"cowork_scheduler: APScheduler failed → {e}; falling back")
        return False


def _run_builtin_loop() -> None:
    """Last-resort scheduler: a simple croniter-driven poll loop.

    Works with no extra dependency beyond croniter (already required).
    """
    logger.info(
        f"cowork_scheduler: using built-in poll loop "
        f"(tick every {_POLL_SECONDS}s on {Q_CONNECTOR})"
    )
    while True:
        try:
            _fire_due_tasks()
        except Exception as e:
            logger.error(f"cowork_scheduler: builtin loop tick error: {e}")
        time.sleep(_POLL_SECONDS)


# ── Entrypoint ────────────────────────────────────────────────

def main() -> None:
    """Start the Cowork scheduler using the best available backend."""
    logger.info("cowork_scheduler: starting")

    if _run_with_rq_scheduler():
        return
    if _run_with_apscheduler():
        return

    try:
        _run_builtin_loop()
    except (KeyboardInterrupt, SystemExit):
        logger.info("cowork_scheduler: stopped")


if __name__ == "__main__":
    main()
