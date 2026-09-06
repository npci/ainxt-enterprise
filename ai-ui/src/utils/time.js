// SPDX-License-Identifier: MIT
// ============================================================
// TIME UTILITIES — UTC → IST display helpers
//
// Storage: UTC (all backend timestamps)
// Display: IST (Asia/Kolkata, UTC+5:30)
// ============================================================

const IST_ZONE = "Asia/Kolkata";

/**
 * Format a UTC timestamp string/Date as IST with full date + time.
 * Example: "02 Mar 2026, 09:41 AM IST"
 */
export function toIST(ts) {
  if (!ts) return "—";
  try {
    const d = typeof ts === "string" ? new Date(ts.endsWith("Z") || ts.includes("+") ? ts : ts + "Z") : ts;
    return d.toLocaleString("en-IN", {
      timeZone: IST_ZONE,
      day:    "2-digit",
      month:  "short",
      year:   "numeric",
      hour:   "2-digit",
      minute: "2-digit",
      hour12: true,
    }) + " IST";
  } catch {
    return String(ts);
  }
}

/**
 * Format as short date only: "02 Mar 2026"
 */
export function toISTDate(ts) {
  if (!ts) return "—";
  try {
    const d = typeof ts === "string" ? new Date(ts.endsWith("Z") || ts.includes("+") ? ts : ts + "Z") : ts;
    return d.toLocaleDateString("en-IN", {
      timeZone: IST_ZONE,
      day:   "2-digit",
      month: "short",
      year:  "numeric",
    });
  } catch {
    return String(ts);
  }
}

/**
 * Format as short date + time: "18 Jun 09:41 AM" (no year, IST).
 * Used by the Query Explorer session range labels.
 */
export function toISTShort(ts) {
  if (!ts) return "—";
  try {
    const d = typeof ts === "string" ? new Date(ts.endsWith("Z") || ts.includes("+") ? ts : ts + "Z") : ts;
    return d.toLocaleString("en-IN", {
      timeZone: IST_ZONE,
      day:    "2-digit",
      month:  "short",
      hour:   "2-digit",
      minute: "2-digit",
      hour12: true,
    });
  } catch {
    return String(ts);
  }
}

/**
 * Format as time only: "09:41 AM" (IST).
 * Used for per-prompt timestamps inside a session.
 */
export function toISTTimeShort(ts) {
  if (!ts) return "—";
  try {
    const d = typeof ts === "string" ? new Date(ts.endsWith("Z") || ts.includes("+") ? ts : ts + "Z") : ts;
    return d.toLocaleString("en-IN", {
      timeZone: IST_ZONE,
      hour:   "2-digit",
      minute: "2-digit",
      hour12: true,
    });
  } catch {
    return String(ts);
  }
}

/**
 * Format as relative time with IST absolute in tooltip-friendly string.
 * e.g. "2 hours ago" — for very recent; "02 Mar, 09:41 AM IST" for older.
 */
export function toISTRelative(ts) {
  if (!ts) return "—";
  try {
    // Backend stores UTC without a timezone suffix. A timezone-less ISO string is
    // parsed as *local* time by JS, which (in IST) skews the result by 5.5h and
    // makes fresh items read "5h ago". Append "Z" so it is parsed as UTC — matching
    // toIST / toISTShort / toISTDate.
    const d      = typeof ts === "string" ? new Date(ts.endsWith("Z") || ts.includes("+") ? ts : ts + "Z") : ts;
    const diffMs = Date.now() - d.getTime();
    const diffM  = Math.floor(diffMs / 60_000);
    if (diffM < 1)  return "just now";
    if (diffM < 60) return `${diffM}m ago`;
    const diffH  = Math.floor(diffM / 60);
    if (diffH < 24) return `${diffH}h ago`;
    return d.toLocaleString("en-IN", {
      timeZone: IST_ZONE,
      day:    "2-digit",
      month:  "short",
      hour:   "2-digit",
      minute: "2-digit",
      hour12: true,
    }) + " IST";
  } catch {
    return String(ts);
  }
}
