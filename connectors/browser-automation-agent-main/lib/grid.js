// SPDX-License-Identifier: Apache-2.0
// Shared addressed-grid renderer (REQUIREMENTS-STRUCTURED_FIDELITY): renders a
// rows array as the same "  | A | B" header + "<n>| a | b" body the spreadsheet
// path produces (renderSheetGrid in documents.js), so reconstructed PDF and
// Word tables read consistently with real spreadsheets. Kept dependency-free
// and self-contained so both lib/pdf.js and lib/documents.js can import it
// without a circular dependency (documents.js already imports pdf.js).

// 0 -> "A", 27 -> "AB"
export function colLetters(index) {
  let s = "";
  for (let n = index + 1; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
  return s;
}

// rows: array of arrays of cell strings (ragged allowed). Returns the grid as
// text with a column-letter header row and 1-based row numbers. Stops adding
// rows once the char budget is reached (inline tables are already bounded, so
// this is a safety cap, not the primary paging mechanism).
export function renderAddressedGrid(rows, { maxChars = 6000 } = {}) {
  const totalCols = rows.reduce((n, r) => Math.max(n, r.length), 0);
  if (!rows.length || !totalCols) return "";

  const header = ["  "];
  for (let c = 0; c < totalCols; c++) header.push(colLetters(c));
  const lines = [header.join(" | ")];
  let chars = lines[0].length;

  for (let r = 0; r < rows.length; r++) {
    const cells = [];
    for (let c = 0; c < totalCols; c++) cells.push(String(rows[r]?.[c] ?? "").replace(/\s+/g, " "));
    const line = `${r + 1}| ${cells.join(" | ")}`;
    if (chars + line.length > maxChars) break;
    lines.push(line);
    chars += line.length + 1;
  }
  return lines.join("\n");
}
