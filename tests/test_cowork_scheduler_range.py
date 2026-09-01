# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the recurrence gates in workers/cowork_scheduler.py.

The gates decide whether a due row should fire, be skipped, or transition to
the terminal `completed` state. They are pure functions (no DB, no queue,
no clock) so we can exercise them directly without spinning up Postgres.

Coverage:
  - _in_start_window: schedule not yet started
  - _range_exhausted: end-by date and max-runs occurrence cap
  - _interval_matches_this_cycle: every-N-weeks and every-N-months skipping
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import workers.cowork_scheduler as sch


UTC = timezone.utc


# ── _in_start_window ─────────────────────────────────────────────────────────

def test_start_window_none_starts_at_always_active():
    """A task with no starts_at fires immediately (matches legacy behaviour)."""
    now = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    assert sch._in_start_window(None, now) is True


def test_start_window_before_starts_at_is_dormant():
    now = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    starts_at = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    assert sch._in_start_window(starts_at, now) is False


def test_start_window_after_starts_at_is_active():
    now = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    starts_at = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    assert sch._in_start_window(starts_at, now) is True


# ── _range_exhausted ─────────────────────────────────────────────────────────

def test_range_not_exhausted_when_no_limits():
    """No ends_at, no max_runs → never exhausted (unbounded schedule)."""
    now = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    assert sch._range_exhausted(None, None, 0, now) is False
    assert sch._range_exhausted(None, None, 999, now) is False


def test_range_exhausted_when_ends_at_passed():
    ends_at = datetime(2026, 1, 28, 23, 59, tzinfo=UTC)
    now = datetime(2026, 1, 29, 0, 0, tzinfo=UTC)
    assert sch._range_exhausted(ends_at, None, 5, now) is True


def test_range_not_exhausted_when_ends_at_in_future():
    ends_at = datetime(2027, 1, 28, 23, 59, tzinfo=UTC)
    now = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    assert sch._range_exhausted(ends_at, None, 5, now) is False


def test_range_exhausted_when_max_runs_reached():
    """`runs_count` is the count BEFORE this potential fire — reaching max_runs
    means the previous fire was the last one."""
    now = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    assert sch._range_exhausted(None, 25, 25, now) is True
    assert sch._range_exhausted(None, 25, 26, now) is True  # over cap = also done


def test_range_not_exhausted_below_max_runs():
    now = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    assert sch._range_exhausted(None, 25, 24, now) is False


# ── _interval_matches_this_cycle: every N weeks ──────────────────────────────

def test_weekly_interval_none_fires_every_week():
    """No custom interval → every occurrence fires."""
    starts = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)  # Mon
    now = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)    # +1 week
    assert sch._interval_matches_this_cycle(starts, None, None, now) is True


def test_weekly_interval_2_fires_on_even_weeks_only():
    starts = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)  # week 0
    # +0 weeks (same week) → fires
    assert sch._interval_matches_this_cycle(starts, 2, None, datetime(2026, 8, 3, 9, 0, tzinfo=UTC)) is True
    # +1 week → skipped
    assert sch._interval_matches_this_cycle(starts, 2, None, datetime(2026, 8, 10, 9, 0, tzinfo=UTC)) is False
    # +2 weeks → fires
    assert sch._interval_matches_this_cycle(starts, 2, None, datetime(2026, 8, 17, 9, 0, tzinfo=UTC)) is True
    # +3 weeks → skipped
    assert sch._interval_matches_this_cycle(starts, 2, None, datetime(2026, 8, 24, 9, 0, tzinfo=UTC)) is False


def test_weekly_interval_3_pattern():
    starts = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
    weeks_from_start = [0, 1, 2, 3, 4, 5]
    fires = [
        sch._interval_matches_this_cycle(starts, 3, None, starts + timedelta(weeks=w))
        for w in weeks_from_start
    ]
    # Every third week: True, False, False, True, False, False
    assert fires == [True, False, False, True, False, False]


# ── _interval_matches_this_cycle: every N months ─────────────────────────────

def test_monthly_interval_none_fires_every_month():
    starts = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
    for m in range(1, 13):
        now = starts.replace(month=m)
        assert sch._interval_matches_this_cycle(starts, None, None, now) is True


def test_monthly_interval_2_fires_every_other_month():
    starts = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
    # Jan (0), Feb (skip), Mar (fire), Apr (skip), May (fire) …
    expected = {1: True, 2: False, 3: True, 4: False, 5: True, 6: False}
    for m, exp in expected.items():
        now = datetime(2026, m, 15, 9, 0, tzinfo=UTC)
        assert sch._interval_matches_this_cycle(starts, None, 2, now) is exp, (
            f"month={m} expected fires={exp}"
        )


def test_monthly_interval_3_crosses_year_boundary():
    """Every 3 months from Nov 2026 → Nov, Feb, May, Aug, Nov."""
    starts = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    checks = [
        (datetime(2026, 11, 1, 9, 0, tzinfo=UTC), True),   # +0
        (datetime(2026, 12, 1, 9, 0, tzinfo=UTC), False),  # +1
        (datetime(2027, 1, 1, 9, 0, tzinfo=UTC),  False),  # +2
        (datetime(2027, 2, 1, 9, 0, tzinfo=UTC),  True),   # +3
        (datetime(2027, 5, 1, 9, 0, tzinfo=UTC),  True),   # +6
        (datetime(2027, 8, 1, 9, 0, tzinfo=UTC),  True),   # +9
    ]
    for now, expected in checks:
        assert sch._interval_matches_this_cycle(starts, None, 3, now) is expected, (
            f"now={now.isoformat()} expected fires={expected}"
        )


# ── Basic helpers exposed for the gates ──────────────────────────────────────

def test_weeks_between_ignores_intra_week_time():
    """The cadence is a cycle count anchored to Monday of the ISO week."""
    mon = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)     # Mon 00:00
    sun = datetime(2026, 8, 9, 23, 59, tzinfo=UTC)   # Sun 23:59 same week
    assert sch._weeks_between(mon, sun) == 0
    next_mon = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
    assert sch._weeks_between(mon, next_mon) == 1


def test_months_between_calendar_delta():
    jan = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    dec_next = datetime(2027, 12, 5, 8, 0, tzinfo=UTC)
    # 2027-12 minus 2026-01 = 23 calendar months
    assert sch._months_between(jan, dec_next) == 23


# ── _parse_monthly_dom ────────────────────────────────────────────────────────

def test_parse_monthly_dom_returns_none_for_non_monthly():
    """Daily and weekly crons are not intercepted."""
    assert sch._parse_monthly_dom("0 9 * * *") is None       # daily
    assert sch._parse_monthly_dom("0 9 * * 1") is None       # weekly
    assert sch._parse_monthly_dom("0 9 * * 1#2") is None     # weekday-of-month


def test_parse_monthly_dom_returns_none_for_days_lte_28():
    """Days 1–28 are handled correctly by croniter; no interception needed."""
    assert sch._parse_monthly_dom("0 9 1 * *") is None
    assert sch._parse_monthly_dom("0 9 28 * *") is None


def test_parse_monthly_dom_returns_day_for_29_to_31():
    assert sch._parse_monthly_dom("0 9 29 * *") == 29
    assert sch._parse_monthly_dom("0 9 30 * *") == 30
    assert sch._parse_monthly_dom("0 9 31 * *") == 31
    # Step-based month field (every N months) is also intercepted.
    assert sch._parse_monthly_dom("0 9 31 */3 *") == 31


# ── _next_run_month_end_aware ─────────────────────────────────────────────────

def test_day31_fires_on_30_in_april():
    """Day 31 → fires on 30 Apr (April has 30 days)."""
    # Base: 31 Mar 2026 09:00 UTC (just fired)
    base = datetime(2026, 3, 31, 9, 0, tzinfo=UTC)
    nxt = sch._next_run_month_end_aware("0 9 31 * *", base, "UTC")
    assert nxt is not None
    assert nxt.year == 2026 and nxt.month == 4 and nxt.day == 30
    assert nxt.hour == 9 and nxt.minute == 0


def test_day31_fires_on_31_in_may():
    """Day 31 → fires on 31 May (May has 31 days)."""
    base = datetime(2026, 4, 30, 9, 0, tzinfo=UTC)
    nxt = sch._next_run_month_end_aware("0 9 31 * *", base, "UTC")
    assert nxt is not None
    assert nxt.year == 2026 and nxt.month == 5 and nxt.day == 31


def test_day31_fires_on_28_in_february_non_leap():
    """Day 31 → fires on 28 Feb in a non-leap year."""
    base = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
    nxt = sch._next_run_month_end_aware("0 9 31 * *", base, "UTC")
    assert nxt is not None
    assert nxt.year == 2026 and nxt.month == 2 and nxt.day == 28


def test_day29_fires_on_29_in_leap_february():
    """Day 29 → fires on 29 Feb in a leap year."""
    base = datetime(2028, 1, 29, 9, 0, tzinfo=UTC)
    nxt = sch._next_run_month_end_aware("0 9 29 * *", base, "UTC")
    assert nxt is not None
    assert nxt.year == 2028 and nxt.month == 2 and nxt.day == 29


def test_day29_fires_on_28_in_non_leap_february():
    """Day 29 → fires on 28 Feb in a non-leap year."""
    base = datetime(2026, 1, 29, 9, 0, tzinfo=UTC)
    nxt = sch._next_run_month_end_aware("0 9 29 * *", base, "UTC")
    assert nxt is not None
    assert nxt.year == 2026 and nxt.month == 2 and nxt.day == 28


def test_day30_fires_on_28_in_february():
    """Day 30 → fires on 28 Feb (non-leap)."""
    base = datetime(2026, 1, 30, 9, 0, tzinfo=UTC)
    nxt = sch._next_run_month_end_aware("0 9 30 * *", base, "UTC")
    assert nxt is not None
    assert nxt.year == 2026 and nxt.month == 2 and nxt.day == 28


def test_day31_sequence_across_year():
    """Walk through a full year of day-31 fires and verify each lands on the
    last day of its month."""
    import calendar
    base = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
    current = base
    for month in range(2, 14):   # Feb 2026 … Jan 2027
        year = 2026 if month <= 12 else 2027
        m = month if month <= 12 else month - 12
        nxt = sch._next_run_month_end_aware("0 9 31 * *", current, "UTC")
        assert nxt is not None, f"No next_run found after {current}"
        expected_day = min(31, calendar.monthrange(year, m)[1])
        assert nxt.year == year and nxt.month == m and nxt.day == expected_day, (
            f"month={m}: expected day {expected_day}, got {nxt}"
        )
        current = nxt


def test_compute_next_run_delegates_to_month_end_aware_for_dom_31():
    """_compute_next_run routes day-31 monthly crons through the month-end helper."""
    base = datetime(2026, 3, 31, 9, 0, tzinfo=UTC)
    nxt = sch._compute_next_run("0 9 31 * *", base, "UTC")
    assert nxt is not None
    assert nxt.month == 4 and nxt.day == 30   # April has 30 days


def test_compute_next_run_uses_croniter_for_dom_28():
    """_compute_next_run leaves day ≤ 28 to croniter (no interception)."""
    base = datetime(2026, 1, 28, 9, 0, tzinfo=UTC)
    nxt = sch._compute_next_run("0 9 28 * *", base, "UTC")
    assert nxt is not None
    # croniter gives 28 Feb for a day-28 cron
    assert nxt.month == 2 and nxt.day == 28
