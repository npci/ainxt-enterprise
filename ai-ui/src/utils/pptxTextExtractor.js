// SPDX-License-Identifier: Apache-2.0
/**
 * pptxTextExtractor — Pure-JS PPTX to HTML extractor using browser-native APIs.
 *
 * Extracts readable HTML from .pptx files (which are ZIP archives containing
 * OOXML PresentationML XML) without any npm dependencies.
 * Uses zipReader.js for extraction and DOMParser for XML parsing — the same
 * approach as docxTextExtractor.js / xlsxParser.js.
 *
 * Produces a per-slide outline:
 *   - One card per slide, in slide order
 *   - Text runs (<a:t>) grouped by paragraph (<a:p>)
 *   - Bullet lines preserved as list items
 *
 * Limitations (acceptable for simplified preview):
 *   - No slide layout / positioning / theme colors
 *   - No images, charts, or embedded objects
 *   - No animations or speaker notes
 *
 * Export:
 *   extractPptxHtml(arrayBuffer) → string (HTML)
 */

import { zipExtract, zipListEntries } from "./zipReader.js";

const xmlParser = new DOMParser();

// ── Helpers ─────────────────────────────────────────────────────────────────

function decode(bytes) {
  return new TextDecoder("utf-8").decode(bytes);
}

function esc(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * Return the numeric slide index from a slide path like "ppt/slides/slide12.xml".
 * Used to sort slides in presentation order (slide2 before slide10).
 */
function slideNumber(path) {
  const m = path.match(/slide(\d+)\.xml$/i);
  return m ? parseInt(m[1], 10) : 0;
}

// ── Slide XML → HTML ─────────────────────────────────────────────────────────

/**
 * Extract paragraphs of text from a single slide's XML document.
 * Each <a:p> becomes a line; its text is the concatenation of child <a:t>.
 * Namespace-agnostic (uses localName) so it works regardless of prefixes.
 */
function extractSlideParagraphs(doc) {
  const paragraphs = [];

  // <a:p> elements hold paragraph-level text across all shapes/text boxes.
  const pNodes = doc.getElementsByTagName("*");
  for (const node of pNodes) {
    if (node.localName !== "p") continue;

    const runs = [];
    for (const t of node.getElementsByTagName("*")) {
      if (t.localName === "t") runs.push(t.textContent || "");
    }

    const line = runs.join("").trim();
    if (line) paragraphs.push(line);
  }

  return paragraphs;
}

function slideToHtml(paragraphs, index) {
  const header = `<div class="pptx-slide-num">Slide ${index}</div>`;

  if (!paragraphs.length) {
    return `<section class="pptx-slide">${header}<p class="pptx-empty">(No text on this slide)</p></section>`;
  }

  const items = paragraphs.map((line) => `<li>${esc(line)}</li>`).join("");
  return `<section class="pptx-slide">${header}<ul>${items}</ul></section>`;
}

// ── Public API ──────────────────────────────────────────────────────────────

/**
 * Extract readable HTML from a .pptx file.
 *
 * Produces a simplified per-slide text outline suitable for preview display.
 * Not a faithful visual renderer — use a dedicated application for
 * full-fidelity viewing.
 *
 * @param {ArrayBuffer} arrayBuffer — the .pptx file bytes
 * @returns {Promise<string>} — HTML string
 */
export async function extractPptxHtml(arrayBuffer) {
  const entries = zipListEntries(arrayBuffer);
  if (!entries || !entries.length) {
    throw new Error("Invalid PPTX: could not read archive");
  }

  // Collect slide XML paths and sort them in presentation order.
  const slidePaths = entries
    .filter((name) => /^ppt\/slides\/slide\d+\.xml$/i.test(name))
    .sort((a, b) => slideNumber(a) - slideNumber(b));

  if (!slidePaths.length) {
    throw new Error("Invalid PPTX: no slides found");
  }

  const parts = [];
  for (let i = 0; i < slidePaths.length; i++) {
    const bytes = await zipExtract(arrayBuffer, slidePaths[i]);
    if (!bytes) continue;

    const doc = xmlParser.parseFromString(decode(bytes), "text/xml");
    if (doc.querySelector("parsererror")) continue;

    const paragraphs = extractSlideParagraphs(doc);
    parts.push(slideToHtml(paragraphs, i + 1));
  }

  if (!parts.length) {
    throw new Error("Invalid PPTX: failed to parse slide content");
  }

  return parts.join("\n");
}
