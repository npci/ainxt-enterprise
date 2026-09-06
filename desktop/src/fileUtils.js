// SPDX-License-Identifier: MIT
"use strict";

function sanitizeFileContent(rawText) {
  return String(rawText)
    .replace(/&/g,  "\u0026")
    .replace(/</g,  "\u003c")
    .replace(/>/g,  "\u003e")
    .replace(/'/g,  "\u0027")
    .replace(/\//g, "\u002f");
}

function buildFileResult(rawBytes) {
  const text = sanitizeFileContent(rawBytes.toString("utf-8"));
  return Object.freeze({ content: text });
}

module.exports = { buildFileResult };
