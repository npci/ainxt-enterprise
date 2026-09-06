// SPDX-License-Identifier: MIT
// kbFormat.js — shared formatting helpers for the Knowledge Base UI.
//
// Centralises the "scope + IST timestamp" rendering used by:
//   - KbChatPanel.jsx        (new chat title)
//   - KbChatList.jsx         (delete confirm dialog)
//   - KbChat.jsx             (scope-summary welcome)
//   - KbDrillGraph.jsx       (document search highlight)
//
// Single source of truth so the title parsed by KbChatList's fallback
// always matches the format produced by KbChatPanel.

const IST_TZ = "Asia/Kolkata";

const MONTHS_SHORT = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/**
 * Format a Date (or millisecond / ISO string) as "DD-MMM-YYYY HH:MM IST".
 * Always renders in Asia/Kolkata regardless of the user's local timezone.
 */
export function formatIstStamp(input) {
  const d = input instanceof Date ? input : new Date(input || Date.now());
  if (Number.isNaN(d.getTime())) return "";

  // Use Intl.DateTimeFormat to project into IST, then re-assemble in our
  // own DD-MMM-YYYY HH:MM shape (en-GB gives us 24h + DD/MM but we need
  // the month abbreviated, so we destructure the parts).
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: IST_TZ,
    day:    "2-digit",
    month:  "2-digit",
    year:   "numeric",
    hour:   "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(d);

  const get = (type) => parts.find((p) => p.type === type)?.value || "";
  const day    = get("day");
  const mm     = parseInt(get("month"), 10);
  const year   = get("year");
  const hour   = get("hour");
  const minute = get("minute");
  const mon    = MONTHS_SHORT[(mm - 1) || 0] || "";

  return `${day}-${mon}-${year} ${hour}:${minute} IST`;
}

/**
 * Format the human-readable scope label used in chat titles and welcome
 * messages: "{Product} · {Domain} · {Version}". ONLY includes the slots
 * the user actually selected — unselected slots are omitted entirely so
 * a domain-only pick renders as just "Tech" rather than "Tech · — · —".
 *
 * Order is preserved: product first (when set), then domain, then
 * version. Order matches the original 3-slot layout for the case where
 * all three are set, keeping titles visually consistent with older
 * fully-scoped chats.
 */
export function formatKbScope(scope) {
  const s = scope || {};
  const product = s.product_name || s.product || "";
  const domain  = s.domain                       || "";
  const version = s.spec_version                 || "";
  const parts = [product, domain, version].filter(Boolean);
  return parts.length ? parts.join(" \u00B7 ") : "—";
}

/**
 * Build a new KB chat title: "{Product} · {Domain} · {Version} — DD-MMM-YYYY HH:MM IST".
 * Pass the resolved scope (with at least product_name / domain / spec_version)
 * and a Date. If date is omitted, "now" is used.
 */
export function formatKbChatTitle(scope, date) {
  return `${formatKbScope(scope)} \u2014 ${formatIstStamp(date)}`;
}

/**
 * Slash-joined path useful for the embedded chat welcome line.
 * ONLY includes the slots the user actually selected — a domain-only
 * scope renders as "Tech", a product+version scope as "Tech / Billing / v2",
 * etc. Order: Domain → Product → Version → Document.
 */
export function formatKbScopePath(scope) {
  const s = scope || {};
  const domain  = s.domain                              || "";
  const product = s.product_name || s.product           || "";
  const version = s.spec_version                        || "";
  const doc     = s.kb_doc_name  || s.document  || s.doc_name || "";
  const parts = [domain, product, version, doc].filter(Boolean);
  return parts.length ? parts.join(" / ") : "—";
}

/**
 * Highlight occurrences of `query` within `text` and return an array of
 * { text, match } segments suitable for rendering in React without
 * dangerouslySetInnerHTML.
 *
 * Case-insensitive; safe for any user input (no regex injection).
 */
export function highlightMatch(text, query) {
  const t = String(text == null ? "" : text);
  const q = String(query || "").trim();
  if (!q) return [{ text: t, match: false }];

  const lower  = t.toLowerCase();
  const needle = q.toLowerCase();
  const out = [];
  let i = 0;
  while (i < t.length) {
    const idx = lower.indexOf(needle, i);
    if (idx === -1) {
      out.push({ text: t.slice(i), match: false });
      break;
    }
    if (idx > i) out.push({ text: t.slice(i, idx), match: false });
    out.push({ text: t.slice(idx, idx + needle.length), match: true });
    i = idx + needle.length;
  }
  return out;
}
