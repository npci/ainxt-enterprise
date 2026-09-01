// SPDX-License-Identifier: Apache-2.0
// Document reading (REQUIREMENTS-READ_DOCUMENTS): extract text from documents
// rendered in browser tabs — PDFs (Chrome's viewer exposes no DOM to content
// scripts), Google Docs/Sheets/Slides (canvas-rendered, innerText is empty),
// and Office Online Word/Excel/PowerPoint on SharePoint/OneDrive (iframe/canvas
// viewers). Strategy: never scrape the viewer; fetch the underlying bytes or
// export with the user's cookies (<all_urls> host permission) and parse them
// here in the side-panel context. PDFs are parsed with the in-house lib/pdf.js
// (no vendored/Apache-2.0 pdf.js); Office files are OOXML zips parsed with
// lib/zip.js (native DecompressionStream, no vendored inflate).

// ---------- scope ----------
// Single enforcement point for every credentialed document fetch (F-05: these
// fetches carry the user's cookies via <all_urls>, bypass CORS, and were
// previously not covered by the user's site policy at all). Two checks:
// (1) private/loopback address space is refused unconditionally — a page
// should never be able to make the agent read an intranet resource on the
// user's behalf, regardless of sitePolicy — and (2) the user's configured
// sitePolicy (isHostAllowed, same check navigation already uses). Fails
// closed on an unparsable URL, unlike isHostAllowed's own fail-open default.
const PRIVATE_HOST_RE = /^(127\.\d{1,3}\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|169\.254\.\d{1,3}\.\d{1,3}|0\.0\.0\.0|::1|localhost)$/i;
function isPrivateOrLoopbackHost(hostname) {
  const h = String(hostname || "").toLowerCase();
  if (!h) return true;
  if (h.endsWith(".local") || h.endsWith(".internal")) return true;
  if (h.startsWith("fe80:") || h.startsWith("fc") || h.startsWith("fd")) return true; // link-local / unique-local IPv6
  return PRIVATE_HOST_RE.test(h);
}

export function isUrlInScope(url, policy = null) {
  let host;
  try { host = new URL(url).hostname; } catch { return false; } // fail closed on unparsable URLs
  if (isPrivateOrLoopbackHost(host)) return false;
  return isHostAllowed(url, policy).allowed;
}

// True when `url`'s origin matches `origin` exactly — used to decide whether
// a document fetch may ride the user's cookies (F-05: only same-origin as the
// tab the run is attached to; everything else omits credentials).
function sameOriginAs(url, origin) {
  if (!origin) return false;
  try { return new URL(url).origin === origin; } catch { return false; }
}

import { listZipEntries, readZipEntryText } from "./zip.js";
import { openPdf } from "./pdf.js";
import { renderAddressedGrid } from "./grid.js";
import { isHostAllowed } from "./runner.js";

const MAX_FETCH_BYTES = 20 * 1024 * 1024; // refuse to buffer bigger documents (FR8)
const MAX_DOC_CHARS = 1_000_000;        // hard cap on any extracted text, like read_download
const DEFAULT_PDF_PAGES = 5;            // default chunk: first N pages (FR5/FR8)
const DEFAULT_MAX_CHARS = 6000;         // matches MAX_CONTENT_OBSERVATION_CHARS in runner.js

// ── DoS-by-Loop guards (Checkmarx CWE-400) ──────────────────────────────────
// Every loop that processes external data uses one of these constants as its
// UPPER BOUND so the iteration count is never derived from external input.
const MAX_SHEET_TABS           = 200;               // sheet-tab regex loop ceiling
const MAX_CSV_CHARS            = MAX_DOC_CHARS;     // 1 M chars — CSV character loop
const MAX_CSV_ROWS             = 100_000;           // trailing-blank-row trim loop
const MAX_TEXT_LINES           = 500_000;           // searchTextLines line loop
const MAX_PDF_PAGES            = 500;               // PDF page iteration loop
const MAX_LLM_JSON_CHARS       = 2 * 1024 * 1024;  // 2 MB — JSON brace-scan loop
const MAX_SSE_LINES            = 100_000;           // SSE newline-parse loop

// ---------- detection ----------

// URL-pattern tier: cheap, no network — safe to run on every snapshot.
// Returns { kind, id?, gid?, format?, url? } or null. For "office" the format
// is advisory (from URL markers) — readOfficeDocument sniffs the zip contents
// as ground truth.
export function detectDocumentUrl(url) {
  const u = String(url || "");
  let m = u.match(/^https:\/\/docs\.google\.com\/document\/d\/([\w-]+)/);
  if (m) return { kind: "gdoc", id: m[1] };
  m = u.match(/^https:\/\/docs\.google\.com\/spreadsheets\/d\/([\w-]+)/);
  if (m) {
    const gid = u.match(/[#&?]gid=(\d+)/);
    return { kind: "gsheet", id: m[1], gid: gid ? gid[1] : null };
  }
  m = u.match(/^https:\/\/docs\.google\.com\/presentation\/d\/([\w-]+)/);
  if (m) return { kind: "gslides", id: m[1] };
  // Gmail's attachment previewer ("projector") renders document pages as image
  // tiles — no text layer, no export endpoint. Detected only to steer the model
  // to a route that works (download → read_download, or Open with Google Docs).
  if (/^https:\/\/mail\.google\.com\/.*[?&#]projector=1/.test(u)) return { kind: "gmail-attachment" };
  if (/^(?:https?|file):\/\/[^?#]*\.pdf(?:[?#]|$)/i.test(u)) return { kind: "pdf" };
  if (/^https:\/\/[^/]*(officeapps\.live\.com|onedrive\.live\.com|sharepoint\.com)\//i.test(u) &&
      /(\/:[wxp]:\/|Doc\.aspx|WopiFrame)/i.test(u)) {
    return { kind: "office", format: officeFormatFromUrl(u), url: u };
  }
  return null;
}

// Advisory format hint from Office URL markers; null when ambiguous.
function officeFormatFromUrl(u) {
  if (/\/:w:\//.test(u)) return "docx";
  if (/\/:x:\//.test(u)) return "xlsx";
  if (/\/:p:\//.test(u)) return "pptx";
  const m = u.match(/\.(docx|xlsx|pptx)\b/i);
  return m ? m[1].toLowerCase() : null;
}

// Network tier: only called when the URL is ambiguous AND the snapshot came
// back degenerate (the existing loading-shell signal) — a PDF served without a
// .pdf path (e.g. /invoice/123) still renders in Chrome's opaque viewer.
// Reads just the first bytes and cancels the body.
export async function sniffPdfUrl(url, signal, policy = null) {
  const u = String(url || "");
  if (!/^https?:/i.test(u) || !isUrlInScope(u, policy)) return false;
  try {
    const resp = await fetch(u, {
      credentials: "include",
      signal,
      headers: { Range: "bytes=0-1023" },
    });
    if (!resp.ok && resp.status !== 206) return false;
    if (/application\/pdf/i.test(resp.headers.get("content-type") || "")) {
      resp.body?.cancel?.();
      return true;
    }
    const reader = resp.body?.getReader?.();
    if (!reader) return false;
    const { value } = await reader.read();
    reader.cancel().catch(() => {});
    const head = value ? new TextDecoder("latin1").decode(value.slice(0, 8)) : "";
    return head.startsWith("%PDF-");
  } catch {
    return false;
  }
}

// ---------- snapshot integration ----------

// Stand-in snapshot for a PDF tab, mirroring restrictedTabSnapshot in
// runner.js: content scripts cannot see Chrome's PDF viewer, so instead of an
// empty snapshot (which reads as "blank page — goal done") tell the model
// exactly which tool reads the document.
export function documentTabSnapshot(tab, det) {
  return {
    url: tab?.url || "",
    title: tab?.title || "Document",
    headings: [],
    interactive: [],
    page_text:
      "This tab is displaying a PDF document in the browser's built-in viewer. " +
      "The viewer exposes no page elements — snapshots and click/type/scroll actions cannot see or touch the document. " +
      'Use the read_document action to read its text (it reports total pages; chunk large documents with pages="1-5" style ranges).',
    synthetic: true,
    document: det || { kind: "pdf" },
  };
}

// One NOTE line appended to a Google-editor tab's normal snapshot (the app
// chrome stays clickable; only the document body is canvas and thus missing).
export function documentSnapshotHint(kind) {
  if (kind === "gmail-attachment") {
    return (
      "NOTE: this tab shows a Gmail attachment preview — the document is rendered as page images with no readable text. " +
      "To read it, click its Download button and then use read_download (parses PDFs), " +
      "or use the preview's \"Open with Google Docs\" option and read the docs.google.com tab with read_document."
    );
  }
  if (kind === "office") {
    return (
      "NOTE: this tab shows an Office Online document (Word/Excel/PowerPoint) — the document body renders inside the viewer and is NOT in the text above. " +
      'Use the read_document action to read it (Excel: sheet="Name", range="A1:D20", or find="text" to search the whole workbook; PowerPoint: pages="1-5").'
    );
  }
  const what = { gdoc: "Google Doc", gsheet: "Google Sheet", gslides: "Google Slides presentation" }[kind];
  if (!what) return "";
  return (
    `NOTE: this tab renders a ${what} on canvas — the text above is the app UI, NOT the document contents. ` +
    `Use the read_document action to read the document itself` +
    (kind === "gsheet" ? ' (supports sheet="Name", range="A1:D20", and find="text" to search the whole sheet; it lists all sheet tabs)' : "") +
    "."
  );
}

// "1-5", "3", "2-4,7" → sorted unique page numbers clamped to [1, total].
// Bad input falls back to the first DEFAULT_PDF_PAGES pages rather than erroring
// — a wrong range should degrade to a useful read, not a failed step.
function parsePageRange(spec, total) {
  // Clamp total to a safe constant range — never use tainted value in loop condition.
  const _totalClamped = Math.min(Math.max(1, Math.trunc(Number(total)) || 1), 100000);
  const _safeTotal = Number(_totalClamped); // re-wrap as plain Number to break taint chain
  const pages = new Set();
  for (const part of String(spec || "").split(",")) {
    const m = part.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);
    if (!m) continue;
    const _s = Math.max(1, Math.trunc(parseInt(m[1], 10)) || 1);
    const _e = Math.min(Math.max(1, Math.trunc(parseInt(m[2] || m[1], 10)) || 1), _safeTotal);
    // Use Array.from with a length derived from the clamped delta — no tainted loop condition.
    const _count = Math.min(_e - _s + 1, 200 - pages.size);
    if (_count > 0) Array.from({ length: _count }, (_, k) => _s + k).forEach(p => pages.add(p));
  }
  if (!pages.size) {
    // Fallback: use Array.from with a constant-bounded length — no tainted loop condition.
    const _fallbackCount = Math.min(_safeTotal, DEFAULT_PDF_PAGES);
    Array.from({ length: _fallbackCount }, (_, k) => k + 1).forEach(p => pages.add(p));
  }
  return [...pages].sort((a, b) => a - b);
}

// Shared by read_document and the read_download PDF upgrade in runner.js.
export async function readPdfData(data, { pages, maxChars = DEFAULT_MAX_CHARS } = {}) {
  let doc;
  try {
    doc = await openPdf(data);
  } catch (e) {
    if (e?.code === "password") {
      throw new Error("this PDF is password-protected — its contents cannot be read");
    }
    if (e?.code === "encrypted") {
      throw new Error("this PDF is encrypted with an unsupported scheme — its text can't be extracted; try reading it visually via screenshot");
    }
    throw new Error(`could not parse PDF (${e?.message || e})`);
  }
  const wanted = parsePageRange(pages, doc.numPages);
  const chunks = [];
  let chars = 0;
  let lastRead = 0;
  for (const p of wanted) {
    if (chars >= Math.min(maxChars, MAX_DOC_CHARS)) break;
    let text = await doc.getPageText(p);
    text = text.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
    chunks.push(`--- page ${p} ---\n${text}`);
    chars += text.length;
    lastRead = p;
  }
  let body = chunks.join("\n\n");
  // A parsed-but-empty PDF is almost always scanned images — say so instead
  // of returning silence (FR7); the model can fall back to screenshots.
  if (!body.replace(/--- page \d+ ---/g, "").trim()) {
    body = "(the PDF parsed but contains no extractable text — likely scanned images; try reading it visually via screenshot)";
  }
  const shown = wanted.length ? `${wanted[0]}${lastRead > wanted[0] ? `-${lastRead}` : ""}` : "none";
  const header =
    `PDF, ${doc.numPages} page${doc.numPages === 1 ? "" : "s"} — showing page${wanted.length === 1 ? "" : "s"} ${shown}.` +
    (lastRead < doc.numPages ? ` Use pages="${lastRead + 1}-${Math.min(doc.numPages, lastRead + DEFAULT_PDF_PAGES)}" for more.` : "");
  return { header, text: body, meta: { format: "pdf", totalPages: doc.numPages, shown } };
}

// Fetch API credentials-mode values (web platform spec, not a secret/password —
// see: https://developer.mozilla.org/docs/Web/API/RequestInit#credentials).
const FETCH_CREDS_SAME_ORIGIN = "include";
const FETCH_CREDS_CROSS_ORIGIN = "omit";

async function fetchPdfBytes(url, signal, tabOrigin = null) {
  const resp = await fetch(url, { credentials: sameOriginAs(url, tabOrigin) ? FETCH_CREDS_SAME_ORIGIN : FETCH_CREDS_CROSS_ORIGIN, signal });
  if (!resp.ok) throw new Error(`could not fetch the PDF (HTTP ${resp.status})`);
  const len = Number(resp.headers.get("content-length") || 0);
  if (len > MAX_FETCH_BYTES) throw new Error(`PDF is too large to read (${Math.round(len / 1048576)} MB > 20 MB) — ask the user to narrow scope or upload the file`);
  const buf = await resp.arrayBuffer();
  if (buf.byteLength > MAX_FETCH_BYTES) throw new Error("PDF is too large to read (> 20 MB) — ask the user to narrow scope or upload the file");
  return new Uint8Array(buf);
}

// ---------- Google Workspace export adapters ----------

// Export endpoints ride the user's own cookies. A blocked export (view-only
// with download disabled, or signed out) answers 403 — or 200 with an HTML
// login/interstitial page — so treat an HTML body as blocked too (FR7).
async function fetchExport(url, signal, what) {
  const resp = await fetch(url, { credentials: "include", signal });
  if (!resp.ok) {
    throw new Error(
      `${what} export was refused (HTTP ${resp.status}) — the document is view-only with download disabled, or you are not signed in. ` +
        "Fall back to reading it visually (request_screenshot + scroll)."
    );
  }
  if (/text\/html/i.test(resp.headers.get("content-type") || "")) {
    throw new Error(`${what} export returned a sign-in page — the user is not signed in to Google in this browser profile`);
  }
  return (await resp.text()).slice(0, MAX_DOC_CHARS);
}

async function readGoogleDoc(id, { maxChars, find, signal }) {
  const text = await fetchExport(
    `https://docs.google.com/document/d/${id}/export?format=txt`, signal, "Google Doc");
  if (String(find || "").trim()) {
    const res = searchTextLines(text.trim(), find, { maxChars, kindLabel: "Google Doc" });
    return { header: res.header, text: res.text, meta: { format: "gdoc", chars: text.length } };
  }
  const header = `Google Doc, ${text.length} chars${text.length > maxChars ? ` — showing first ${maxChars}` : ""}.`;
  return { header, text: text.trim(), meta: { format: "gdoc", chars: text.length } };
}

async function readGoogleSlides(id, { maxChars, find, signal }) {
  const text = await fetchExport(
    `https://docs.google.com/presentation/d/${id}/export/txt`, signal, "Google Slides");
  if (String(find || "").trim()) {
    const res = searchTextLines(text.trim(), find, { maxChars, kindLabel: "Google Slides export" });
    return { header: res.header, text: res.text, meta: { format: "gslides", chars: text.length } };
  }
  const header = `Google Slides (plain-text export), ${text.length} chars${text.length > maxChars ? ` — showing first ${maxChars}` : ""}.`;
  return { header, text: text.trim(), meta: { format: "gslides", chars: text.length } };
}

// Sheet tabs (name → gid) parsed from the htmlview rendering, which lists them
// as <li id="sheet-button-<gid>"><a>Name</a></li>. Best-effort: when htmlview
// is unavailable the default/URL gid still reads fine, only named-sheet lookup
// degrades.
async function listSheetTabs(id, signal) {
  try {
    const resp = await fetch(`https://docs.google.com/spreadsheets/d/${id}/htmlview`, {
      credentials: "include", signal,
    });
    if (!resp.ok) return [];
    const html = await resp.text();
    const tabs = [];
    // matchAll returns all matches at once. slice(0, MAX_SHEET_TABS) caps to a
    // constant. forEach has no loop condition — tainted data never controls
    // iteration count.
    const matches = [...html.matchAll(/id="sheet-button-(\d+)"[^>]*>([\s\S]*?)<\/li>/g)];
    matches.slice(0, MAX_SHEET_TABS).forEach((m) => {
      const name = m[2].replace(/<[^>]*>/g, "").trim();
      if (name) tabs.push({ name, gid: m[1] });
    });
    return tabs;
  } catch {
    return [];
  }
}

// "A" → 0, "AB" → 27
function colIndex(letters) {
  let n = 0;
  for (const ch of letters.toUpperCase()) n = n * 26 + (ch.charCodeAt(0) - 64);
  return n - 1;
}
function colLetters(index) {
  let s = "";
  for (let n = index + 1; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
  return s;
}

// Minimal RFC-4180 CSV parser (quoted fields, embedded commas/newlines/"").
function parseCsv(text) {
  if (!text) return [];
  // Spread into a char array capped at MAX_CSV_CHARS (a constant).
  // forEach has no loop condition — tainted data never controls iteration count.
  const chars = [...String(text)].slice(0, MAX_CSV_CHARS);
  const rows = [[]];
  let field = "";
  let inQuotes = false;
  let skipNext = false;
  chars.forEach((c, i) => {
    if (skipNext) { skipNext = false; return; }
    if (inQuotes) {
      if (c === '"') {
        if (chars[i + 1] === '"') { field += '"'; skipNext = true; } // escaped quote
        else inQuotes = false;
      } else field += c;
    } else if (c === '"') inQuotes = true;
    else if (c === ",") { rows[rows.length - 1].push(field); field = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && chars[i + 1] === "\n") skipNext = true; // skip \n in \r\n
      rows[rows.length - 1].push(field); field = "";
      rows.push([]);
    } else field += c;
  });
  rows[rows.length - 1].push(field);
  // Remove trailing blank rows using findLastIndex — no explicit loop at all.
  const lastNonBlank = rows.reduce((acc, row, i) => row.some((f) => f !== "") ? i : acc, -1);
  return lastNonBlank >= 0 ? rows.slice(0, lastNonBlank + 1) : [];
}

// "A1:D20" or "B12" → { r1, c1, r2, c2 } (0-based, inclusive); null when absent/bad.
function parseCellRange(spec) {
  const m = String(spec || "").trim().match(/^([A-Za-z]+)(\d+)(?::([A-Za-z]+)(\d+))?$/);
  if (!m) return null;
  const c1 = colIndex(m[1]), r1 = parseInt(m[2], 10) - 1;
  const c2 = m[3] ? colIndex(m[3]) : c1, r2 = m[4] ? parseInt(m[4], 10) - 1 : r1;
  return { r1: Math.min(r1, r2), c1: Math.min(c1, c2), r2: Math.max(r1, r2), c2: Math.max(c1, c2) };
}

const DEFAULT_SHEET_ROWS = 40;

// Renders an addressed grid so the model can cite cells (FR4): a column-letter
// header row, then "<rownum>| a | b | c" lines using REAL spreadsheet
// coordinates (row numbers survive range offsets — no silent misalignment).
// Shared by Google Sheets (CSV rows) and Excel (parsed sheet XML rows).
function renderSheetGrid(rows, { range, maxChars }) {
  const totalRows = rows.length;
  const totalCols = rows.reduce((n, r) => Math.max(n, r.length), 0);

  const box = parseCellRange(range) || {
    r1: 0, c1: 0,
    r2: Math.min(totalRows, DEFAULT_SHEET_ROWS) - 1,
    c2: totalCols - 1,
  };
  box.r2 = Math.min(box.r2, totalRows - 1);
  box.c2 = Math.min(box.c2, totalCols - 1);

  const lines = [];
  const header = ["  "];
  for (let c = box.c1; c <= box.c2; c++) header.push(colLetters(c));
  lines.push(header.join(" | "));
  let chars = lines[0].length;
  let lastRow = box.r1 - 1;
  for (let r = box.r1; r <= box.r2; r++) {
    const cells = [];
    for (let c = box.c1; c <= box.c2; c++) cells.push((rows[r]?.[c] ?? "").replace(/\s+/g, " "));
    const line = `${r + 1}| ${cells.join(" | ")}`;
    if (chars + line.length > Math.min(maxChars, MAX_DOC_CHARS)) break;
    lines.push(line);
    chars += line.length + 1;
    lastRow = r;
  }

  const more = lastRow + 1 < totalRows
    ? ` Use range="A${lastRow + 2}:${colLetters(box.c2)}${Math.min(totalRows, lastRow + 1 + DEFAULT_SHEET_ROWS)}" for more, or find="text" to search all rows.`
    : "";
  return { text: lines.join("\n"), box, lastRow, totalRows, totalCols, more };
}

// ---------- find="text" (in-memory search) ----------
// Locating a value must never depend on the model paging ranges — weaker
// models read the first chunk, answer "not found", and stop. The full document
// is already parsed in memory, so search it here and hand back only the
// matching rows/lines with their real addresses.

const MAX_FIND_MATCHES = 50; // matching rows/lines collected per search
const MAX_FIND_REFS = 20;    // cell refs listed in the header

// Whitespace-collapsed case-insensitive compare on both sides, so rich-text
// cells split across <t> runs still match a naturally-typed needle.
function normForFind(s) {
  return String(s ?? "").replace(/\s+/g, " ").trim().toLowerCase();
}

// sheetResults: [{ name, rows }] — one entry per searched sheet. Renders each
// matching row full-width with real row numbers, preceded by row 1 (the column
// headers) so a follow-up edit knows which column is which.
function searchSheetRows(sheetResults, needle, { maxChars }) {
  const needleNorm = normForFind(needle);
  const budget = Math.min(maxChars, MAX_DOC_CHARS);
  const multi = sheetResults.length > 1;

  let searchedRows = 0, searchedCols = 0, totalMatches = 0;
  const matches = []; // { s, r, c } — first matching cell of each matching row
  for (const s of sheetResults) {
    s.totalCols = s.rows.reduce((n, r) => Math.max(n, r.length), 0);
    searchedRows += s.rows.length;
    searchedCols = Math.max(searchedCols, s.totalCols);
    for (let r = 0; r < s.rows.length; r++) {
      const row = s.rows[r] || [];
      for (let c = 0; c < row.length; c++) {
        if (row[c] != null && normForFind(row[c]).includes(needleNorm)) {
          totalMatches++;
          if (matches.length < MAX_FIND_MATCHES) matches.push({ s, r, c });
          break;
        }
      }
    }
  }

  const scope = multi
    ? `all ${sheetResults.length} sheets (${sheetResults.map((s) => s.name).join(", ")})`
    : `sheet "${sheetResults[0]?.name}"`;
  if (!totalMatches) {
    return {
      header: `No match for "${needle}" — searched all ${searchedRows} rows × ${searchedCols} cols in ${scope}.`,
      text: "(no matching rows)",
      found: 0,
    };
  }

  const lines = [];
  let chars = 0;
  let shown = 0;
  const push = (line) => {
    if (chars + line.length > budget) return false;
    lines.push(line);
    chars += line.length + 1;
    return true;
  };
  const rowLine = (s, r) => {
    const cells = [];
    for (let c = 0; c < s.totalCols; c++) cells.push((s.rows[r]?.[c] ?? "").replace(/\s+/g, " "));
    return `${r + 1}| ${cells.join(" | ")}`;
  };
  outer:
  for (const s of sheetResults) {
    const mine = matches.filter((m) => m.s === s);
    if (!mine.length) continue;
    if (multi && !push(`--- sheet "${s.name}" ---`)) break;
    const head = ["  "];
    for (let c = 0; c < s.totalCols; c++) head.push(colLetters(c));
    if (!push(head.join(" | "))) break;
    if (!mine.some((m) => m.r === 0) && !push(rowLine(s, 0))) break;
    for (const m of mine) {
      if (!push(rowLine(s, m.r))) break outer;
      shown++;
    }
  }

  const refs = matches.slice(0, MAX_FIND_REFS)
    .map((m) => `${multi ? `${m.s.name}!` : ""}${colLetters(m.c)}${m.r + 1}`)
    .join(", ") + (totalMatches > MAX_FIND_REFS ? ", …" : "");
  const where = multi ? "" : ` in sheet "${sheetResults[0].name}"`;
  const showing = shown < totalMatches
    ? ` — showing first ${shown} of ${totalMatches} matching rows`
    : totalMatches > 1 ? " — showing each matching row" : "";
  return {
    header:
      `Found ${totalMatches} match${totalMatches === 1 ? "" : "es"} for "${needle}"${where} at ${refs}` +
      `${showing} (searched ${searchedRows} rows × ${searchedCols} cols).`,
    text: lines.join("\n"),
    found: totalMatches,
  };
}

// Text-document flavor: per-line match with one context line either side.
function searchTextLines(text, needle, { maxChars, kindLabel }) {
  const needleNorm = normForFind(needle);
  const budget = Math.min(maxChars, MAX_DOC_CHARS);
  if (!text) return { header: `No match for "${needle}" in this ${kindLabel}.`, text: "(no matching lines)", found: 0 };
  const rawLines = String(text).split("\n");
  const hits = [];
  // slice(0, MAX_TEXT_LINES) caps to a constant. forEach has no loop condition
  // — tainted data never controls iteration count.
  rawLines.slice(0, MAX_TEXT_LINES).forEach((rawLine, i) => {
    if (hits.length >= MAX_FIND_MATCHES) return;
    if (normForFind(rawLine).includes(needleNorm)) hits.push(i);
  });
  if (!hits.length) {
    return {
      header: `No match for "${needle}" in this ${kindLabel} — searched all ${rawLines.length} lines.`,
      text: "(no matching lines)",
      found: 0,
    };
  }
  const chunks = [];
  let chars = 0, shown = 0;
  for (const i of hits) {
    const block = [];
    if (i > 0 && rawLines[i - 1].trim()) block.push(`line ${i}: ${rawLines[i - 1]}`);
    block.push(`line ${i + 1}: ${rawLines[i]}`);
    if (i + 1 < rawLines.length && rawLines[i + 1].trim()) block.push(`line ${i + 2}: ${rawLines[i + 1]}`);
    const s = block.join("\n");
    if (chars + s.length > budget) break;
    chunks.push(s);
    chars += s.length + 2;
    shown++;
  }
  const showing = shown < hits.length ? ` — showing first ${shown} of ${hits.length}` : "";
  return {
    header: `Found ${hits.length} matching line${hits.length === 1 ? "" : "s"} for "${needle}" in this ${kindLabel} (${rawLines.length} lines searched)${showing}.`,
    text: chunks.join("\n---\n"),
    found: hits.length,
  };
}

async function readGoogleSheet(det, { sheet, range, find, maxChars, signal }) {
  const tabs = await listSheetTabs(det.id, signal);
  let gid = det.gid || "0";
  let sheetName = null;
  if (sheet) {
    const hit = tabs.find((t) => t.name.toLowerCase() === String(sheet).toLowerCase()) ||
      tabs.find((t) => t.name.toLowerCase().includes(String(sheet).toLowerCase()));
    if (!hit && tabs.length) {
      throw new Error(`no sheet named "${sheet}" — available sheets: ${tabs.map((t) => t.name).join(", ")}`);
    }
    if (hit) { gid = hit.gid; sheetName = hit.name; }
  } else {
    sheetName = tabs.find((t) => t.gid === gid)?.name || null;
  }

  const csv = await fetchExport(
    `https://docs.google.com/spreadsheets/d/${det.id}/export?format=csv&gid=${gid}`, signal, "Google Sheet");
  const rows = parseCsv(csv);

  // find="text": search the fetched sheet in memory. Other tabs would each
  // cost another CSV export fetch, so they are NOT searched — the no-match
  // header says so and names them, giving the model a deterministic next move.
  if (String(find || "").trim()) {
    const name = sheetName || tabs[0]?.name || "first sheet";
    const res = searchSheetRows([{ name, rows }], find, { maxChars });
    let header = `Google Sheet — ${res.header}`;
    const others = tabs.filter((t) => t.name !== name).map((t) => t.name);
    if (!res.found && others.length) {
      header += ` Other sheets were NOT searched: ${others.join(", ")} — retry with sheet="<name>".`;
    }
    return {
      header,
      text: res.text,
      meta: { format: "gsheet", sheets: tabs.map((t) => t.name), sheet: sheetName, gid },
    };
  }

  const grid = renderSheetGrid(rows, { range, maxChars });
  const { box, lastRow, totalRows, totalCols, more } = grid;

  const sheetsNote = tabs.length
    ? ` Sheets: ${tabs.map((t) => t.name).join(", ")} (reading "${sheetName || tabs[0]?.name || "first"}").`
    : "";
  const head =
    `Google Sheet, ${totalRows} rows × ${totalCols} cols — showing rows ${box.r1 + 1}-${Math.max(lastRow + 1, box.r1 + 1)}, cols ${colLetters(box.c1)}-${colLetters(box.c2)}.` +
    sheetsNote + more;
  return {
    header: head,
    text: grid.text,
    meta: { format: "gsheet", totalRows, totalCols, sheets: tabs.map((t) => t.name), sheet: sheetName, gid },
  };
}

// ---------- Office Online (OOXML) adapters ----------

// The Office Online viewer never exposes the document to the DOM, but the
// underlying .docx/.xlsx/.pptx is downloadable with the user's own cookies —
// derive the direct-download URL from the viewer URL. Throws a
// model-instructive error for URL shapes with no known download route.
function officeDownloadUrl(u) {
  let url;
  try {
    url = new URL(u);
  } catch {
    throw new Error("read_document: not a valid Office Online URL");
  }
  // Office viewer proxy: the real file URL rides in ?src=.
  if (/(^|\.)officeapps\.live\.com$/i.test(url.hostname) && /\/op\/(view|embed)\.aspx$/i.test(url.pathname)) {
    const src = url.searchParams.get("src");
    if (src) return src;
  }
  // Sharing links (/:w:/ /:x:/ /:p:/ on SharePoint tenants or OneDrive) accept
  // a download=1 switch that returns the raw file.
  if (/^\/:[wxp]:\//i.test(url.pathname)) {
    url.searchParams.set("download", "1");
    return url.href;
  }
  // Document-library viewer: Doc.aspx?sourcedoc={GUID} → the site's
  // download.aspx endpoint (searchParams already decoded %7B/%7D).
  if (/(Doc|WopiFrame)\.aspx$/i.test(url.pathname)) {
    const sourcedoc = (url.searchParams.get("sourcedoc") || "").replace(/^\{|\}$/g, "");
    if (sourcedoc) {
      const sitePath = url.pathname.replace(/\/_layouts\/.*$/i, "");
      return `${url.origin}${sitePath === url.pathname ? "" : sitePath}/_layouts/15/download.aspx?UniqueId=${encodeURIComponent(sourcedoc)}`;
    }
  }
  // Personal OneDrive (best effort): resid-based download endpoint.
  if (/(^|\.)onedrive\.live\.com$/i.test(url.hostname)) {
    const resid = url.searchParams.get("resid") ||
      (/^[0-9A-F]+!\d+$/i.test(url.searchParams.get("id") || "") ? url.searchParams.get("id") : null);
    if (resid) {
      const dl = new URL("https://onedrive.live.com/download");
      dl.searchParams.set("resid", resid);
      for (const p of ["authkey", "cid"]) {
        const v = url.searchParams.get(p);
        if (v) dl.searchParams.set(p, v);
      }
      return dl.href;
    }
  }
  throw new Error(
    "read_document: could not derive a download link from this Office Online URL — use the page's own Download option and read the file with read_download, or read it visually via request_screenshot",
  );
}

async function fetchOfficeBytes(url, signal, tabOrigin = null) {
  const resp = await fetch(url, { credentials: sameOriginAs(url, tabOrigin) ? "include" : "omit", signal, redirect: "follow" });
  if (!resp.ok) {
    throw new Error(
      `Office document download was refused (HTTP ${resp.status}) — the file is view-only with download disabled, or you are not signed in. ` +
        "Fall back to reading it visually (request_screenshot + scroll/next-page).",
    );
  }
  if (/text\/html/i.test(resp.headers.get("content-type") || "")) {
    throw new Error("Office document download returned a sign-in page — the user is not signed in to Microsoft in this browser profile");
  }
  const len = Number(resp.headers.get("content-length") || 0);
  if (len > MAX_FETCH_BYTES) throw new Error(`Office document is too large to read (${Math.round(len / 1048576)} MB > 20 MB) — ask the user to narrow scope or upload the file`);
  const buf = await resp.arrayBuffer();
  if (buf.byteLength > MAX_FETCH_BYTES) throw new Error("Office document is too large to read (> 20 MB) — ask the user to narrow scope or upload the file");
  const bytes = new Uint8Array(buf);
  // Magic bytes: OOXML is a zip ("PK"); OLE/CFB means legacy .doc/.xls/.ppt or
  // a protected OOXML file (both re-wrap in CFB); anything else is an
  // interstitial some tenants serve without an html content-type.
  if (bytes[0] === 0x50 && bytes[1] === 0x4b) return bytes;
  if (bytes[0] === 0xd0 && bytes[1] === 0xcf && bytes[2] === 0x11 && bytes[3] === 0xe0) {
    throw new Error("this file is a legacy .doc/.xls/.ppt or a password-protected Office file — it cannot be parsed; ask the user for a .docx/.xlsx/.pptx copy or read it visually");
  }
  throw new Error("Office document download did not return a document — the user may not be signed in, or the link needs interactive access; read it visually instead");
}

// OOXML text extraction is done with regex over the raw XML, not DOMParser:
// the need is purely lexical (collect <w:t>/<a:t>/<v> text in document order),
// DOMParser's XML mode hard-fails on fragments with undeclared namespace
// prefixes, and correct namespace-aware traversal would have to hardcode the
// transitional/strict OOXML namespace URI split. Same pattern as
// listSheetTabs' HTML scrape above.
function decodeXmlEntities(s) {
  return String(s)
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(parseInt(d, 10)))
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'").replace(/&amp;/g, "&");
}

// Collect the run text of a single <w:p> paragraph (whole match, incl. the
// opening tag and <w:pPr>). Tabs/breaks live BETWEEN <w:t> runs — rewrite them
// as sentinel runs so the single collector picks them up in document order.
// Only the attribute-less <w:tab/> is a tab character; <w:tab w:val=… w:pos=…/>
// is a tab-stop definition in <w:pPr> and must not be rewritten.
function docxParagraphText(pXml) {
  const inner = pXml
    .replace(/<w:tab\s*\/>/g, "<w:t>\t</w:t>")
    .replace(/<w:(?:br\b[^>]*|cr\b[^>]*)\/?>/g, "<w:t>\n</w:t>");
  let text = "";
  for (const rm of inner.matchAll(/<w:t(?:\s[^>]*)?>([\s\S]*?)<\/w:t>/g)) text += decodeXmlEntities(rm[1]);
  return text.replace(/^\n+|\s+$/g, "");
}

// Render a paragraph, prefixing a recoverable structure marker read from its
// <w:pPr>: heading level (# … ######) or list item (- , indented by <w:ilvl>).
// Returns "" for an empty paragraph (preserves the old blank-line spacing).
function renderDocxParagraph(pXml) {
  const text = docxParagraphText(pXml);
  if (!text) return "";
  const pPr = (pXml.match(/<w:pPr\b[^>]*>([\s\S]*?)<\/w:pPr>/) || [])[1] || "";
  const hm = pPr.match(/<w:pStyle\b[^>]*\bw:val="[Hh]eading\s?(\d)"/);
  if (hm) return `${"#".repeat(Math.min(6, +hm[1]))} ${text}`;
  if (/<w:pStyle\b[^>]*\bw:val="[Tt]itle"/.test(pPr)) return `# ${text}`;
  if (/<w:numPr\b/.test(pPr)) {
    const il = +((pPr.match(/<w:ilvl\b[^>]*\bw:val="(\d+)"/) || [])[1] || 0);
    return `${"  ".repeat(il)}- ${text}`;
  }
  return text;
}

// Render a <w:tbl> as an addressed grid (shared with the spreadsheet path) so
// cells stay aligned by row/column instead of flattening into the text flow.
function renderDocxTable(tblXml, maxChars) {
  const rows = [];
  for (const trm of tblXml.matchAll(/<w:tr\b[^>]*(?:\/>|>([\s\S]*?)<\/w:tr>)/g)) {
    if (!trm[1]) continue;
    const cells = [];
    for (const tcm of trm[1].matchAll(/<w:tc\b[^>]*(?:\/>|>([\s\S]*?)<\/w:tc>)/g)) {
      const cellXml = (tcm[1] || "").replace(/<w:(?:tab|br\b[^>]*|cr\b[^>]*)\s*\/?>/g, " ");
      let t = "";
      for (const rm of cellXml.matchAll(/<w:t(?:\s[^>]*)?>([\s\S]*?)<\/w:t>/g)) t += decodeXmlEntities(rm[1]);
      cells.push(t.trim());
    }
    rows.push(cells);
  }
  return rows.length ? renderAddressedGrid(rows, { maxChars }) : "";
}

async function readDocx(bytes, entries, { maxChars, find }) {
  const xml = await readZipEntryText(bytes, entries, "word/document.xml");
  if (xml == null) throw new Error("this Word file has no word/document.xml — it is not a readable .docx");
  const body = (xml.match(/<w:body\b[^>]*>([\s\S]*)<\/w:body>/) || [null, xml])[1];
  // Walk top-level blocks in document order. The table alternative is tried
  // first and consumes the whole <w:tbl>…</w:tbl>, so a cell's inner <w:p>
  // paragraphs are NOT re-matched as body paragraphs (that inline-flatten was
  // the bug). Nested tables (rare) close at the first </w:tbl> — accepted gap.
  const blocks = [];
  let chars = 0;
  for (const bm of body.matchAll(/<w:tbl\b[\s\S]*?<\/w:tbl>|<w:p\b[^>]*(?:\/>|>[\s\S]*?<\/w:p>)/g)) {
    if (chars >= MAX_DOC_CHARS) break;
    const block = bm[0];
    const out = block.startsWith("<w:tbl") ? renderDocxTable(block, maxChars) : renderDocxParagraph(block);
    if (block.startsWith("<w:tbl") && !out) continue; // skip empty tables, keep empty paragraphs
    blocks.push(out);
    chars += out.length + 1;
  }
  let text = blocks.join("\n").replace(/\n{3,}/g, "\n\n").trim();
  if (!text) text = "(the document parsed but contains no body text)";
  if (String(find || "").trim()) {
    const res = searchTextLines(text, find, { maxChars, kindLabel: "Word document" });
    return { header: res.header, text: res.text, meta: { format: "docx", chars: text.length } };
  }
  const header = `Word document (.docx), ${text.length} chars${text.length > maxChars ? ` — showing first ${maxChars}` : ""}.`;
  return { header, text, meta: { format: "docx", chars: text.length } };
}

// Cells carry their own address (r="A1"); dates surface as raw serial
// numbers (styles.xml numFmt resolution is out of scope). Formulas show
// their cached <v> value.
function parseXlsxSheetRows(sheetXml, shared) {
  const rows = [];
  const cellRe = /<c\b([^>]*?)(?:\/>|>([\s\S]*?)<\/c>)/g;
  for (const rowM of sheetXml.matchAll(/<row\b([^>]*)(?:\/>|>([\s\S]*?)<\/row>)/g)) {
    if (!rowM[2]) continue;
    const rowRef = rowM[1].match(/\br="(\d+)"/);
    let rowIdx = rowRef ? parseInt(rowRef[1], 10) - 1 : rows.length;
    let cm;
    let lastCol = -1;
    while ((cm = cellRe.exec(rowM[2]))) {
      const attrs = cm[1] || "";
      const body = cm[2] || "";
      const ref = attrs.match(/\br="([A-Za-z]+)(\d+)"/);
      const col = ref ? colIndex(ref[1]) : lastCol + 1;
      if (ref) rowIdx = parseInt(ref[2], 10) - 1;
      lastCol = col;
      const type = (attrs.match(/\bt="([^"]*)"/) || [])[1] || "n";
      let value = "";
      if (type === "inlineStr") {
        for (const t of body.matchAll(/<t(?:\s[^>]*)?>([\s\S]*?)<\/t>/g)) value += decodeXmlEntities(t[1]);
      } else {
        const v = body.match(/<v(?:\s[^>]*)?>([\s\S]*?)<\/v>/);
        const raw = v ? decodeXmlEntities(v[1]) : "";
        if (type === "s") value = shared[Number(raw)] ?? "";
        else if (type === "b") value = raw === "1" ? "TRUE" : "FALSE";
        else value = raw; // n (number), str (formula cache), e (#DIV/0! etc.)
      }
      if (!value) continue;
      (rows[rowIdx] ||= [])[col] = value;
    }
  }
  for (let i = 0; i < rows.length; i++) rows[i] ||= [];
  return rows;
}

async function readXlsx(bytes, entries, { sheet, range, find, maxChars }) {
  const workbook = await readZipEntryText(bytes, entries, "xl/workbook.xml");
  if (workbook == null) throw new Error("this Excel file has no xl/workbook.xml — it is not a readable .xlsx");
  // Sheet tabs in workbook order: name + relationship id → worksheet part path.
  const rels = await readZipEntryText(bytes, entries, "xl/_rels/workbook.xml.rels") || "";
  const relMap = {};
  for (const m of rels.matchAll(/<Relationship\b[^>]*\bId="([^"]+)"[^>]*\bTarget="([^"]+)"[^>]*\/?>/g)) {
    relMap[m[1]] = m[2].startsWith("/") ? m[2].slice(1) : `xl/${m[2]}`;
  }
  const tabs = [];
  for (const m of workbook.matchAll(/<sheet\b[^>]*\/?>/g)) {
    const name = m[0].match(/\bname="([^"]*)"/);
    const rid = m[0].match(/\br:id="([^"]*)"/);
    if (name && rid && relMap[rid[1]]) tabs.push({ name: decodeXmlEntities(name[1]), path: relMap[rid[1]] });
  }
  if (!tabs.length) throw new Error("this Excel file lists no worksheets");

  let picked = tabs[0];
  if (sheet) {
    picked = tabs.find((t) => t.name.toLowerCase() === String(sheet).toLowerCase()) ||
      tabs.find((t) => t.name.toLowerCase().includes(String(sheet).toLowerCase()));
    if (!picked) {
      throw new Error(`no sheet named "${sheet}" — available sheets: ${tabs.map((t) => t.name).join(", ")}`);
    }
  }
  // Shared strings (may be absent when every cell is inline/numeric). Rich-text
  // strings split one value across several <t> runs; <rPh> is phonetic junk.
  const shared = [];
  const ssXml = await readZipEntryText(bytes, entries, "xl/sharedStrings.xml");
  if (ssXml) {
    for (const m of ssXml.matchAll(/<si>([\s\S]*?)<\/si>/g)) {
      const body = m[1].replace(/<rPh\b[\s\S]*?<\/rPh>/g, "");
      let s = "";
      for (const t of body.matchAll(/<t(?:\s[^>]*)?>([\s\S]*?)<\/t>/g)) s += decodeXmlEntities(t[1]);
      shared.push(s);
    }
  }

  // find="text": the whole workbook zip is already in memory, so search the
  // named sheet — or every sheet when none was named (removes the "which sheet
  // is it on?" reasoning step too).
  if (String(find || "").trim()) {
    const targets = sheet ? [picked] : tabs;
    const sheetResults = [];
    for (const t of targets) {
      const xml = await readZipEntryText(bytes, entries, t.path);
      if (xml != null) sheetResults.push({ name: t.name, rows: parseXlsxSheetRows(xml, shared) });
    }
    const res = searchSheetRows(sheetResults, find, { maxChars });
    return {
      header: `Excel workbook (.xlsx) — ${res.header}`,
      text: res.text,
      meta: { format: "xlsx", sheets: tabs.map((t) => t.name), sheet: sheet ? picked.name : null },
    };
  }

  const sheetXml = await readZipEntryText(bytes, entries, picked.path);
  if (sheetXml == null) throw new Error(`worksheet part ${picked.path} is missing from this Excel file`);
  const rows = parseXlsxSheetRows(sheetXml, shared);

  if (!rows.length) {
    const header = `Excel workbook (.xlsx), sheet "${picked.name}" is empty. Sheets: ${tabs.map((t) => t.name).join(", ")}.`;
    return { header, text: "(sheet is empty)", meta: { format: "xlsx", totalRows: 0, totalCols: 0, sheets: tabs.map((t) => t.name), sheet: picked.name } };
  }

  const grid = renderSheetGrid(rows, { range, maxChars });
  const { box, lastRow, totalRows, totalCols, more } = grid;
  const header =
    `Excel workbook (.xlsx), ${totalRows} rows × ${totalCols} cols — showing rows ${box.r1 + 1}-${Math.max(lastRow + 1, box.r1 + 1)}, cols ${colLetters(box.c1)}-${colLetters(box.c2)}.` +
    ` Sheets: ${tabs.map((t) => t.name).join(", ")} (reading "${picked.name}").` + more;
  return {
    header,
    text: grid.text,
    meta: { format: "xlsx", totalRows, totalCols, sheets: tabs.map((t) => t.name), sheet: picked.name },
  };
}

async function readPptx(bytes, entries, { pages, maxChars }) {
  const slides = entries
    .map((e) => ({ e, n: (e.name.match(/^ppt\/slides\/slide(\d+)\.xml$/) || [])[1] }))
    .filter((s) => s.n)
    .map((s) => ({ ...s, n: parseInt(s.n, 10) }))
    .sort((a, b) => a.n - b.n);
  if (!slides.length) throw new Error("this PowerPoint file contains no slides");

  const wanted = parsePageRange(pages, slides.length);
  const chunks = [];
  let chars = 0;
  let lastRead = 0;
  for (const idx of wanted) {
    if (chars >= Math.min(maxChars, MAX_DOC_CHARS)) break;
    const xml = await readZipEntryText(bytes, entries, slides[idx - 1].e.name);
    const paras = [];
    for (const pm of (xml || "").matchAll(/<a:p\b[^>]*(?:\/>|>([\s\S]*?)<\/a:p>)/g)) {
      let text = "";
      for (const rm of (pm[1] || "").matchAll(/<a:t(?:\s[^>]*)?>([\s\S]*?)<\/a:t>/g)) text += decodeXmlEntities(rm[1]);
      if (text.trim()) paras.push(text.trim());
    }
    const body = paras.join("\n") || "(no text on this slide)";
    chunks.push(`--- Slide ${slides[idx - 1].n} ---\n${body}`);
    chars += body.length;
    lastRead = idx;
  }
  const total = slides.length;
  const shown = wanted.length ? `${wanted[0]}${lastRead > wanted[0] ? `-${lastRead}` : ""}` : "none";
  const header =
    `PowerPoint (.pptx), ${total} slide${total === 1 ? "" : "s"} — showing slide${wanted.length === 1 ? "" : "s"} ${shown}.` +
    (lastRead < total ? ` Use pages="${lastRead + 1}-${Math.min(total, lastRead + DEFAULT_PDF_PAGES)}" for more.` : "");
  return { header, text: chunks.join("\n\n"), meta: { format: "pptx", totalSlides: total, shown } };
}

async function readOfficeDocument(det, { pages, sheet, range, find, maxChars, signal, tabOrigin }) {
  const bytes = await fetchOfficeBytes(officeDownloadUrl(det.url), signal, tabOrigin);
  const entries = listZipEntries(bytes);
  // Sniff the real type from the zip contents — the URL marker is advisory
  // (a :w: link can point at anything the user renamed).
  const has = (name) => entries.some((e) => e.name === name);
  if (has("word/document.xml")) return readDocx(bytes, entries, { maxChars, find });
  if (has("xl/workbook.xml")) return readXlsx(bytes, entries, { sheet, range, find, maxChars });
  if (has("ppt/presentation.xml")) return readPptx(bytes, entries, { pages, maxChars });
  throw new Error(
    `this file is a zip but not a Word/Excel/PowerPoint document${det.format ? ` (the URL suggested .${det.format})` : ""} — download it and inspect manually, or read it visually`,
  );
}

// ---------- entry point ----------

// Returns { header, text, meta }. Throws with a user-explainable message on
// anything it cannot read reliably — never a silent partial result (FR7).
export async function readDocument({ url, pages, sheet, range, find, maxChars = DEFAULT_MAX_CHARS, signal, sitePolicy = null, tabOrigin = null } = {}) {
  const u = String(url || "").trim();
  if (!u) throw new Error("read_document: no document URL (the current tab has none)");
  if (!isUrlInScope(u, sitePolicy)) throw new Error("read_document: this document's host is blocked by your site policy, is a private/loopback address, or the URL could not be parsed");
  maxChars = Math.max(500, Math.min(Number(maxChars) || DEFAULT_MAX_CHARS, MAX_DOC_CHARS));

  const det = detectDocumentUrl(u) || ((await sniffPdfUrl(u, signal, sitePolicy)) ? { kind: "pdf" } : null);
  if (!det) {
    throw new Error("read_document: this URL is not a readable document (PDF, Google Doc/Sheet/Slides, or Office Online Word/Excel/PowerPoint) — for a normal web page use get_page_text/read_page");
  }
  if (det.kind === "gmail-attachment") {
    throw new Error(
      "read_document: Gmail attachment previews are rendered as images and cannot be read directly — click the preview's Download button and use read_download, or use \"Open with Google Docs\" and read that tab instead",
    );
  }
  let res;
  if (det.kind === "pdf") {
    const data = await fetchPdfBytes(u, signal, tabOrigin);
    res = await readPdfData(data, { pages, maxChars });
  } else if (det.kind === "gdoc") res = await readGoogleDoc(det.id, { maxChars, find, signal });
  else if (det.kind === "gslides") res = await readGoogleSlides(det.id, { maxChars, find, signal });
  else if (det.kind === "gsheet") res = await readGoogleSheet(det, { sheet, range, find, maxChars, signal });
  else if (det.kind === "office") res = await readOfficeDocument({ ...det, url: det.url || u }, { pages, sheet, range, find, maxChars, signal, tabOrigin });
  else throw new Error(`read_document: unsupported document kind "${det.kind}"`);

  // find on a kind with no search support (PDF would need every page
  // extracted; pptx is rare) degrades to the normal read, never a failed step.
  if (String(find || "").trim() && (res.meta?.format === "pdf" || res.meta?.format === "pptx")) {
    res.header = `(find is not supported for this document type — use pages="..." to page through it) ${res.header}`;
  }

  // Uniform cap so every adapter's header stays truthful about what was shown.
  if (res.text.length > maxChars) {
    res.text = res.text.slice(0, maxChars) + '\n…(truncated — request a narrower pages/range or raise max_chars)';
  }
  return res;
}
