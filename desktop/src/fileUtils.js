// SPDX-License-Identifier: MIT
"use strict";

function sanitizeFileContent(rawText) {
  // HTML-escape so file content can never be interpreted as markup if it
  // ends up rendered in the app UI. The previous version replaced each
  // character with the IDENTICAL character via JS unicode escapes (e.g.
  // "&" -> "\u0026", which IS "&") -- a complete no-op that provided zero
  // protection. `&` must be escaped FIRST so the entities below aren't
  // themselves re-escaped.
  return String(rawText)
    .replace(/&/g,  "&amp;")
    .replace(/</g,  "&lt;")
    .replace(/>/g,  "&gt;")
    .replace(/'/g,  "&#x27;")
    .replace(/"/g,  "&quot;")
    .replace(/\//g, "&#x2f;");
}

function buildFileResult(rawBytes) {
  const text = sanitizeFileContent(rawBytes.toString("utf-8"));
  return Object.freeze({ content: text });
}

module.exports = { buildFileResult };
