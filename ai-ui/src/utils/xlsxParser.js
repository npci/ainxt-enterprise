// SPDX-License-Identifier: Apache-2.0
/**
 * xlsxParser — Pure-JS XLSX parser using browser-native APIs.
 *
 * Parses .xlsx files (which are ZIP archives containing XML) into structured
 * sheet data without any npm dependencies. Uses zipReader.js for extraction
 * and DOMParser for XML parsing.
 *
 * Supports:
 *   - Multiple sheets with names
 *   - Shared strings and inline strings
 *   - Numbers, booleans, errors
 *   - Sparse cell references (e.g. A1, Z100, AA1)
 *
 * Limitations (acceptable for preview):
 *   - Formulas: shows cached value only (no evaluation)
 *   - Merged cells: not visually merged
 *   - Cell formatting/styles: not applied
 *   - Charts/images: ignored
 *   - Date serial numbers: shown as-is (no date formatting)
 *
 * Export:
 *   parseXlsx(arrayBuffer) → { sheets: [{ name, rows: string[][] }] }
 */

import { zipExtract } from "./zipReader.js";

const xmlParser = new DOMParser();

// ── Helpers ─────────────────────────────────────────────────────────────────

/**
 * Parse an XML string into a Document, returning null on failure.
 */
function parseXml(xmlString) {
  const doc = xmlParser.parseFromString(xmlString, "text/xml");
  if (doc.querySelector("parsererror")) return null;
  return doc;
}

/**
 * Decode a Uint8Array to a UTF-8 string.
 */
function decode(bytes) {
  return new TextDecoder("utf-8").decode(bytes);
}

/**
 * Convert an Excel-style cell reference (e.g. "B3", "AA1") to [row, col] (0-indexed).
 */
function cellRefToRC(ref) {
  const match = ref.match(/^([A-Z]+)(\d+)$/i);
  if (!match) return null;
  const letters = match[1].toUpperCase();
  const col = letters.split("").reduce((acc, ch) => acc * 26 + ch.charCodeAt(0) - 64, 0) - 1;
  const row = parseInt(match[2], 10) - 1;
  return [row, col];
}

/**
 * Get all child elements with a given local name (ignores namespace prefixes).
 */
function getByLocal(parent, localName) {
  if (!parent) return [];
  const result = [];
  for (const child of parent.children) {
    if (child.localName === localName) result.push(child);
  }
  return result;
}

/**
 * Recursively collect all text content from <t> elements inside an <si> or <is>.
 * Handles both <si><t>text</t></si> and <si><r><t>text</t></r>...</si> (rich text).
 */
function collectText(el) {
  if (!el) return "";
  const parts = [];
  const tElements = el.getElementsByTagName("*");
  for (const node of tElements) {
    if (node.localName === "t") {
      parts.push(node.textContent || "");
    }
  }
  return parts.join("");
}

// ── Shared strings parser ───────────────────────────────────────────────────

function parseSharedStrings(xmlString) {
  if (!xmlString) return [];
  const doc = parseXml(xmlString);
  if (!doc) return [];

  const strings = [];
  // <sst> → <si> elements, each containing <t> or <r><t> children
  const siElements = doc.getElementsByTagName("si");
  for (const si of siElements) {
    strings.push(collectText(si));
  }
  return strings;
}

// ── Workbook parser (sheet names + rIds) ────────────────────────────────────

function parseWorkbook(xmlString) {
  if (!xmlString) return [];
  const doc = parseXml(xmlString);
  if (!doc) return [];

  const sheets = [];
  const sheetEls = doc.getElementsByTagName("sheet");
  for (const el of sheetEls) {
    const name = el.getAttribute("name") || `Sheet${sheets.length + 1}`;
    // r:id attribute — uses the relationship namespace
    const rId = el.getAttributeNS("http://schemas.openxmlformats.org/officeDocument/2006/relationships", "id")
             || el.getAttribute("r:id")
             || el.getAttribute("rId")
             || "";
    sheets.push({ name, rId });
  }
  return sheets;
}

// ── Relationships parser (rId → target filename) ───────────────────────────

function parseRels(xmlString) {
  if (!xmlString) return {};
  const doc = parseXml(xmlString);
  if (!doc) return {};

  const map = {};
  const rels = doc.getElementsByTagName("Relationship");
  for (const rel of rels) {
    const id     = rel.getAttribute("Id") || "";
    const target = rel.getAttribute("Target") || "";
    if (id) map[id] = target;
  }
  return map;
}

// ── Sheet parser (cells → 2D string array) ──────────────────────────────────

function parseSheet(xmlString, sharedStrings) {
  if (!xmlString) return [];
  const doc = parseXml(xmlString);
  if (!doc) return [];

  const rows = [];
  let maxCol = 0;

  const rowEls = doc.getElementsByTagName("row");
  for (const rowEl of rowEls) {
    const cellEls = rowEl.getElementsByTagName("c");

    for (const cell of cellEls) {
      const ref  = cell.getAttribute("r");
      if (!ref) continue;

      const rc = cellRefToRC(ref);
      if (!rc) continue;
      const [_r, _c] = rc;
      // Cap row/col to safe maximums to prevent unbounded loop growth
      // from untrusted XML content (Checkmarx: Unchecked Input For Loop Condition)
      const r = Math.min(Math.max(0, Math.trunc(_r) || 0), 100000);
      const c = Math.min(Math.max(0, Math.trunc(_c) || 0), 16383);

      // Get cell value
      const type   = cell.getAttribute("t") || "";
      const vEl    = cell.getElementsByTagName("v")[0];
      const rawVal = vEl ? (vEl.textContent || "") : "";

      let value = "";
      switch (type) {
        case "s":
          // Shared string — rawVal is the index
          value = sharedStrings[parseInt(rawVal, 10)] ?? rawVal;
          break;
        case "inlineStr": {
          // Inline string — text inside <is><t>
          const isEl = cell.getElementsByTagName("is")[0];
          value = isEl ? collectText(isEl) : rawVal;
          break;
        }
        case "b":
          value = rawVal === "1" ? "TRUE" : "FALSE";
          break;
        case "e":
          // Error value
          value = rawVal || "#ERROR!";
          break;
        default:
          // Number or general — use as-is
          value = rawVal;
          break;
      }

      // Expand the rows array as needed
      while (rows.length <= r) rows.push([]);
      while (rows[r].length <= c) rows[r].push("");
      rows[r][c] = value;
      if (c > maxCol) maxCol = c;
    }
  }

  // Normalize: ensure all rows have the same number of columns
  for (let i = 0; i < rows.length; i++) {
    while (rows[i].length <= maxCol) rows[i].push("");
  }

  return rows;
}

// ── Public API ──────────────────────────────────────────────────────────────

/**
 * Parse an .xlsx file from an ArrayBuffer into structured sheet data.
 *
 * @param {ArrayBuffer} arrayBuffer — the .xlsx file bytes
 * @returns {Promise<{ sheets: Array<{ name: string, rows: string[][] }> }>}
 */
export async function parseXlsx(arrayBuffer) {
  // 1. Extract and parse shared strings
  const ssBytes = await zipExtract(arrayBuffer, "xl/sharedStrings.xml");
  const sharedStrings = ssBytes ? parseSharedStrings(decode(ssBytes)) : [];

  // 2. Extract and parse workbook (sheet names + relationship IDs)
  const wbBytes = await zipExtract(arrayBuffer, "xl/workbook.xml");
  if (!wbBytes) throw new Error("Invalid XLSX: missing xl/workbook.xml");
  const sheetDefs = parseWorkbook(decode(wbBytes));
  if (!sheetDefs.length) throw new Error("Invalid XLSX: no sheets found in workbook");

  // 3. Extract and parse relationships (rId → sheet file path)
  const relsBytes = await zipExtract(arrayBuffer, "xl/_rels/workbook.xml.rels");
  const relsMap   = relsBytes ? parseRels(decode(relsBytes)) : {};

  // 4. Parse each sheet
  const sheets = [];
  for (let i = 0; i < sheetDefs.length; i++) {
    const def = sheetDefs[i];

    // Resolve the sheet filename from relationships
    let sheetPath = "";
    if (def.rId && relsMap[def.rId]) {
      // Target is relative to xl/ (e.g., "worksheets/sheet1.xml")
      const target = relsMap[def.rId];
      sheetPath = target.startsWith("/") ? target.slice(1) : `xl/${target}`;
    } else {
      // Fallback: convention-based path
      sheetPath = `xl/worksheets/sheet${i + 1}.xml`;
    }

    const sheetBytes = await zipExtract(arrayBuffer, sheetPath);
    const rows = sheetBytes ? parseSheet(decode(sheetBytes), sharedStrings) : [];
    sheets.push({ name: def.name, rows });
  }

  return { sheets };
}
