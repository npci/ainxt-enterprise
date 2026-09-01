// SPDX-License-Identifier: Apache-2.0
// lib/pii.js — lightweight PII detection for values typed into pages.
//
// Deliberately conservative: only patterns with strong structure (SSN shape,
// Luhn-valid card numbers, IBAN shape) so ordinary numbers, order IDs, and
// phone numbers don't trigger false warnings. Secrets referenced as
// ${secrets.*} are handled by the existing redactor and never reach this scan
// unresolved — callers suppress the warning gate for vaulted values.

// Luhn checksum — filters out random digit runs that merely look like cards.
function luhnValid(digits) {
  let sum = 0;
  let dbl = false;
  for (let i = digits.length - 1; i >= 0; i--) {
    let d = digits.charCodeAt(i) - 48;
    if (dbl) { d *= 2; if (d > 9) d -= 9; }
    sum += d;
    dbl = !dbl;
  }
  return sum % 10 === 0;
}

const SSN_RE = /\b\d{3}-\d{2}-\d{4}\b/g;
// 13-19 digits, optionally space/dash separated in groups (e.g. 4111 1111 1111 1111).
const CARD_RE = /\b(?:\d[ -]?){12,18}\d\b/g;
const IBAN_RE = /\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b/g;

// Scan a string for PII. Returns [{ kind, match }] — empty when clean.
export function scanPII(text) {
  const s = String(text ?? "");
  if (!s) return [];
  const hits = [];
  for (const m of s.match(SSN_RE) || []) {
    hits.push({ kind: "SSN", match: m });
  }
  for (const m of s.match(CARD_RE) || []) {
    const digits = m.replace(/[ -]/g, "");
    if (digits.length >= 13 && digits.length <= 19 && luhnValid(digits)) {
      hits.push({ kind: "credit card number", match: m });
    }
  }
  for (const m of s.match(IBAN_RE) || []) {
    hits.push({ kind: "IBAN", match: m });
  }
  return hits;
}

// Replace detected PII with a masked placeholder, keeping the last 4 characters
// so runs remain distinguishable in logs ("…1111").
export function maskPII(value) {
  if (Array.isArray(value)) return value.map(maskPII);
  if (typeof value !== "string" || !value) return value;
  let out = value;
  for (const { match } of scanPII(value)) {
    out = out.split(match).join(`***…${match.slice(-4)}`);
  }
  return out;
}
