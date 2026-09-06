# SPDX-License-Identifier: MIT
# ============================================================
# IST TIME HELPERS
# ============================================================
#
# IST (Asia/Kolkata) is a fixed UTC+5:30 offset with no DST, so a plain
# `timezone(timedelta(hours=5, minutes=30))` is exact and — unlike
# `zoneinfo.ZoneInfo("Asia/Kolkata")` — never depends on the optional
# `tzdata` package being installed (zoneinfo has no bundled tz database on
# Windows / minimal Linux images; ZoneInfo("Asia/Kolkata") raises
# ZoneInfoNotFoundError there unless `tzdata` is explicitly pip-installed).
# This mirrors the existing fixed-offset precedent in
# services/digest_service.py (_IST_TZ) and workers/kb_worker.py.
#
# Use these helpers anywhere a timestamp is logged/stored for human
# consumption (model_usages.created_at, Kafka event "timestamp" fields,
# audit logs) instead of datetime.utcnow(). Anything used purely for
# interval math against other UTC-based columns (e.g. Redis TTLs, "not older
# than N days" filters) should stay on UTC — see call sites for reasoning.
# ============================================================

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """
    Current time as a NAIVE IST datetime (tzinfo stripped) — for storing into
    `DateTime` (not `DateTime(timezone=True)`) columns such as
    `model_usages.created_at`, where the column has no timezone awareness and
    a tz-aware value would raise/be silently mishandled by some drivers.
    """
    return datetime.now(timezone.utc).astimezone(IST).replace(tzinfo=None)


def now_ist_iso() -> str:
    """
    Current time as an IST ISO-8601 string with an explicit `+05:30` offset —
    for the "timestamp" field of Kafka events (ainxt.metrics etc.), replacing
    the previous `datetime.utcnow().isoformat() + "Z"` pattern.
    """
    return datetime.now(timezone.utc).astimezone(IST).isoformat()
