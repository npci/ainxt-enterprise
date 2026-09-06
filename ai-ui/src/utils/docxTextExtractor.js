// SPDX-License-Identifier: MIT
/**
 * docxTextExtractor — Pure-JS DOCX to HTML extractor using browser-native APIs.
 *
 * Extracts readable HTML from .docx files (which are ZIP archives containing
 * OOXML WordprocessingML XML) without any npm dependencies.
 * Uses zipReader.js for extraction and DOMParser for XML parsing.
 *
 * Produces structured HTML with:
 *   - Headings (h1–h6)
 *   - Paragraphs
 *   - Bold, italic, underline, strikethrough
 *   - Tables
 *   - Ordered and unordered lists
 *   - Line breaks and tabs
 *
 * Limitations (acceptable for simplified preview):
 *   - No page layout / pagination
 *   - No images or embedded objects
 *   - No headers/footers
 *   - No precise font/color styling
 *   - Nested lists shown flat
 *
 * Export:
 *   extractDocxHtml(arrayBuffer) → string (HTML)
 */

import { zipExtract } from "./zipReader.js";

const W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";

const xmlParser = new DOMParser();

// ── Helpers ─────────────────────────────────────────────────────────────────

/**
 * Decode a Uint8Array to a UTF-8 string.
 */
function decode(bytes) {
  return new TextDecoder("utf-8").decode(bytes);
}

/**
 * Get direct child elements with a given localName (namespace-agnostic).
 */
function children(parent, localName) {
  const result = [];
  if (!parent) return result;
  for (const child of parent.children) {
    if (child.localName === localName) result.push(child);
  }
  return result;
}

/**
 * Get the first direct child element with a given localName.
 */
function child(parent, localName) {
  if (!parent) return null;
  for (const ch of parent.children) {
    if (ch.localName === localName) return ch;
  }
  return null;
}

/**
 * Get an attribute value, trying both the w: namespace and plain.
 */
function attr(el, name) {
  if (!el) return null;
  return el.getAttributeNS(W_NS, name) || el.getAttribute(`w:${name}`) || el.getAttribute(name) || null;
}

/**
 * Escape HTML special characters.
 */
function esc(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ── Heading style detection ─────────────────────────────────────────────────

const HEADING_RE = /^heading\s*(\d)/i;
const TITLE_RE   = /^title$/i;
const SUBTITLE_RE = /^subtitle$/i;

/**
 * Detect heading level from a paragraph's style.
 * Returns 1–6 for headings, 0 for non-heading paragraphs.
 */
function headingLevel(pPr) {
  if (!pPr) return 0;
  const pStyle = child(pPr, "pStyle");
  if (!pStyle) return 0;
  const val = attr(pStyle, "val") || "";
  const m = val.match(HEADING_RE);
  if (m) return Math.min(parseInt(m[1], 10), 6);
  if (TITLE_RE.test(val)) return 1;
  if (SUBTITLE_RE.test(val)) return 2;
  return 0;
}

/**
 * Check if a paragraph has a numbering property (list item).
 */
function isListItem(pPr) {
  if (!pPr) return false;
  return !!child(pPr, "numPr");
}

// ── Run (w:r) processing ────────────────────────────────────────────────────

function processRun(rEl) {
  const parts = [];

  for (const node of rEl.children) {
    const ln = node.localName;

    if (ln === "t") {
      parts.push(esc(node.textContent || ""));
    } else if (ln === "br") {
      parts.push("<br>");
    } else if (ln === "tab") {
      parts.push("&emsp;");
    } else if (ln === "cr") {
      parts.push("<br>");
    }
    // Skip rPr, drawing, pict, etc.
  }

  if (!parts.length) return "";

  let text = parts.join("");

  // Apply inline formatting from run properties
  const rPr = child(rEl, "rPr");
  if (rPr) {
    const isBold      = !!child(rPr, "b");
    const isItalic    = !!child(rPr, "i");
    const isUnderline = !!child(rPr, "u");
    const isStrike    = !!child(rPr, "strike");
    const isSuperscript = attr(child(rPr, "vertAlign"), "val") === "superscript";
    const isSubscript   = attr(child(rPr, "vertAlign"), "val") === "subscript";

    if (isStrike)    text = `<s>${text}</s>`;
    if (isUnderline) text = `<u>${text}</u>`;
    if (isItalic)    text = `<em>${text}</em>`;
    if (isBold)      text = `<strong>${text}</strong>`;
    if (isSuperscript) text = `<sup>${text}</sup>`;
    if (isSubscript)   text = `<sub>${text}</sub>`;
  }

  return text;
}

// ── Paragraph (w:p) processing ──────────────────────────────────────────────

function processParagraph(pEl) {
  const pPr    = child(pEl, "pPr");
  const hLevel = headingLevel(pPr);
  const isList = isListItem(pPr);

  // Collect text from all runs and hyperlinks within the paragraph
  const parts = [];
  for (const ch of pEl.children) {
    const ln = ch.localName;
    if (ln === "r") {
      parts.push(processRun(ch));
    } else if (ln === "hyperlink") {
      // Hyperlink contains runs — extract text (URL not available without rels)
      const linkParts = [];
      for (const rc of ch.children) {
        if (rc.localName === "r") linkParts.push(processRun(rc));
      }
      const linkText = linkParts.join("");
      if (linkText) parts.push(`<span style="color:#4338ca;text-decoration:underline">${linkText}</span>`);
    }
    // Skip bookmarkStart, bookmarkEnd, etc.
  }

  const content = parts.join("");

  // Empty paragraph → spacer
  if (!content.trim()) return "<p>&nbsp;</p>";

  if (hLevel > 0) return `<h${hLevel}>${content}</h${hLevel}>`;
  if (isList)     return `<li>${content}</li>`;
  return `<p>${content}</p>`;
}

// ── Table (w:tbl) processing ────────────────────────────────────────────────

function processTable(tblEl) {
  const rows = [];

  for (const trEl of children(tblEl, "tr")) {
    const cells = [];
    for (const tcEl of children(trEl, "tc")) {
      // Table cell can contain multiple paragraphs
      const cellParts = [];
      for (const ch of tcEl.children) {
        if (ch.localName === "p") {
          const inner = processParagraph(ch);
          // Strip the outer <p> tags for table cells to keep them compact
          cellParts.push(inner.replace(/^<p>(.*)<\/p>$/s, "$1"));
        }
      }
      cells.push(`<td>${cellParts.join("<br>")}</td>`);
    }
    rows.push(`<tr>${cells.join("")}</tr>`);
  }

  return `<table>${rows.join("")}</table>`;
}

// ── Body walker ─────────────────────────────────────────────────────────────

function processBody(bodyEl) {
  if (!bodyEl) return "<p>No content found in document.</p>";

  const output = [];
  let inList = false;

  for (const el of bodyEl.children) {
    const ln = el.localName;

    if (ln === "p") {
      const html = processParagraph(el);
      const isLi = html.startsWith("<li>");

      if (isLi && !inList) {
        output.push("<ul>");
        inList = true;
      } else if (!isLi && inList) {
        output.push("</ul>");
        inList = false;
      }

      output.push(html);
    } else if (ln === "tbl") {
      if (inList) { output.push("</ul>"); inList = false; }
      output.push(processTable(el));
    } else if (ln === "sectPr") {
      // Section properties — skip (page setup, margins, etc.)
    }
    // Skip unknown elements
  }

  if (inList) output.push("</ul>");

  return output.join("\n");
}

// ── Public API ──────────────────────────────────────────────────────────────

/**
 * Extract readable HTML from a .docx file.
 *
 * Produces simplified but structured HTML suitable for preview display.
 * Not a faithful visual renderer — use a dedicated application for
 * full-fidelity viewing.
 *
 * @param {ArrayBuffer} arrayBuffer — the .docx file bytes
 * @returns {Promise<string>} — HTML string
 */
export async function extractDocxHtml(arrayBuffer) {
  const docBytes = await zipExtract(arrayBuffer, "word/document.xml");
  if (!docBytes) {
    throw new Error("Invalid DOCX: missing word/document.xml");
  }

  const xmlString = decode(docBytes);
  const doc = xmlParser.parseFromString(xmlString, "text/xml");

  if (doc.querySelector("parsererror")) {
    throw new Error("Invalid DOCX: failed to parse document XML");
  }

  // Find <w:body> — the main document content container.
  // DOMParser may or may not resolve the w: namespace prefix, so try both approaches.
  let body = doc.getElementsByTagNameNS(W_NS, "body")[0];
  if (!body) {
    // Fallback: querySelector with escaped colon
    body = doc.querySelector("body");
  }
  if (!body) {
    // Last resort: iterate root children
    for (const ch of doc.documentElement.children) {
      if (ch.localName === "body") { body = ch; break; }
    }
  }

  return processBody(body);
}
