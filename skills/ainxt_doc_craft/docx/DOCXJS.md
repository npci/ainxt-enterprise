---
name: docxjs-facts
description: API facts and failure modes for docx (docx-js) v9 as installed in the AiNxt doc sandbox.
license: Apache-2.0
source: Compiled from the docx (docx-js) project's public documentation.
---

# docx-js v9 — API Facts

Installed globally in the sandbox as `docx@9`, on `NODE_PATH`.

## Units — get these wrong and nothing looks right

| Property | Unit | Example |
|---|---|---|
| Font `size` | **half-points** | 11pt → `size: 22`; 18pt → `size: 36` |
| `spacing.before/after` | twentieths of a point (twips) | 12pt → `240`; 4pt → `80` |
| `spacing.line` | twips; 240 = single | 1.15 line → `line: 276` |
| Page size / margin / indent | twips (DXA) | 1in → `1440` |
| Border `size` | **eighths of a point** | 0.75pt → `6`; 1pt → `8` |
| `ImageRun.transformation` | pixels | `{ width: 115, height: 48 }` |
| Colour | 6 hex digits, no `#` | `color: '1F3864'` |

Helpers: `convertInchesToTwip(1)` → 1440, `convertMillimetersToTwip(210)` → **11905**
(the exact value is 11905.5; the helper truncates). A4 therefore emits
`<w:pgSz w:w="11905" w:h="16837"/>`, which is correct — do not "fix" it to 11906.

## Skeleton of a document

```js
const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
        WidthType, BorderStyle, ShadingType, AlignmentType, Header, Footer,
        PageNumber, ImageRun, LevelFormat, convertMillimetersToTwip } = require('docx');
const fs = require('fs');

const doc = new Document({
  styles: { default: { document: { run: { font: 'Arial', size: 22, color: '222222' } } } },
  numbering: { config: [{ reference: 'steps', levels: [
    { level: 0, format: LevelFormat.DECIMAL, text: '%1.', alignment: AlignmentType.START },
  ] }] },
  sections: [{
    properties: {
      page: {
        size:   { width: convertMillimetersToTwip(210), height: convertMillimetersToTwip(297) },
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
      },
    },
    footers: { default: new Footer({ children: [ /* see below */ ] }) },
    children: [ /* body */ ],
  }],
});

Packer.toBuffer(doc)
  .then(b => { fs.writeFileSync('/work/output.docx', b); console.log('ok'); })
  .catch(e => { console.error(e); process.exit(1); });
```

`Packer.toBuffer` is a Promise. Never let the process exit before it settles, and
make the catch path exit non-zero so a failure is reported rather than silent.

## Headings with the brand rule beneath

Build headings explicitly rather than relying on `HeadingLevel` — the built-in
heading styles carry Word's own colours and sizes, not ours.

```js
function h1(text) {
  return new Paragraph({
    children: [ new TextRun({ text, bold: true, size: 36, color: '1F3864' }) ],
    spacing: { before: 240, after: 80 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: '1F3864', space: 4 } },
  });
}
function h2(text) {
  return new Paragraph({
    children: [ new TextRun({ text, bold: true, size: 28, color: '1F3864' }) ],
    spacing: { before: 200, after: 80 },
  });
}
function body(text) {
  return new Paragraph({
    children: [ new TextRun({ text, size: 22, color: '222222' }) ],
    spacing: { after: 160, line: 276 },
    alignment: AlignmentType.LEFT,
  });
}
```

## Lists

```js
new Paragraph({ text: 'A bulleted point', bullet: { level: 0 } });
new Paragraph({ text: 'First step', numbering: { reference: 'steps', level: 0 } });
```

Bullets need no config. Numbered lists require the matching `numbering.config`
entry on the `Document` (see skeleton) — omit it and numbering silently disappears.

## Tables

```js
const cell = (text, opts = {}) => new TableCell({
  width: { size: opts.pct, type: WidthType.PERCENTAGE },
  shading: { type: ShadingType.CLEAR, color: 'auto', fill: opts.fill || 'FFFFFF' },
  margins: { top: 80, bottom: 80, left: 120, right: 120 },
  children: [ new Paragraph({
    alignment: opts.right ? AlignmentType.RIGHT : AlignmentType.LEFT,
    children: [ new TextRun({ text, bold: !!opts.bold, size: 20,
                              color: opts.head ? 'FFFFFF' : '222222' }) ],
  }) ],
});

const NO = { style: BorderStyle.NONE, size: 0, color: 'FFFFFF' };
const HAIR = { style: BorderStyle.SINGLE, size: 6, color: 'C9D2E3' };

new Table({
  width: { size: 100, type: WidthType.PERCENTAGE },
  borders: { top: NO, bottom: HAIR, left: NO, right: NO,
             insideHorizontal: HAIR, insideVertical: NO },
  rows: [
    new TableRow({ tableHeader: true, children: [
      cell('Bank', { pct: 34, bold: true, head: true, fill: '1F3864' }),
      cell('Value (₹ Cr)', { pct: 22, bold: true, head: true, fill: '1F3864', right: true }),
    ] }),
    new TableRow({ children: [
      cell('Bank A', { pct: 34 }),
      cell('8,431', { pct: 22, right: true, fill: 'F2F5FA' }),
    ] }),
  ],
});
```

`tableHeader: true` on the first row repeats it across page breaks. Column widths
must be set on the **cells**; a width on the table alone is not enough for stable
layout across Word and LibreOffice.

## Footer with page numbers

```js
new Footer({ children: [ new Paragraph({
  tabStops: [{ type: 'right', position: 9026 }],   // A4 width − margins, in twips
  children: [
    new TextRun({ text: process.env.DOC_FOOTER_TEXT || 'Confidential', size: 18, color: '5A6472' }),
    new TextRun({ text: '\t', size: 18 }),
    new TextRun({ text: 'Page ', size: 18, color: '5A6472' }),
    new TextRun({ children: [PageNumber.CURRENT], size: 18, color: '5A6472' }),
    new TextRun({ text: ' of ', size: 18, color: '5A6472' }),
    new TextRun({ children: [PageNumber.TOTAL_PAGES], size: 18, color: '5A6472' }),
  ],
}) ] });
```

`PageNumber.CURRENT` and `PageNumber.TOTAL_PAGES` go in a run's `children` array,
not its `text`.

## Images

```js
const fs = require('fs');
const mark = process.env.DOC_BRAND_MARK || '/opt/ainxt-brand/brand-mark.png';
if (fs.existsSync(mark)) {
  new Paragraph({ alignment: AlignmentType.RIGHT, children: [
    new ImageRun({ type: 'png', data: fs.readFileSync(mark),
                   transformation: { width: 115, height: 48 } }),
  ] });
}
```

**`type` is mandatory in v9** (`'png' | 'jpg' | 'gif' | 'bmp'`). Omitting it throws.
Local files only — the sandbox has no network.

## Page breaks and spacers

```js
new Paragraph({ children: [], pageBreakBefore: true });   // start a new page
new Paragraph({ text: '', spacing: { after: 240 } });      // vertical spacer
```

## Failure modes to avoid

- **Point values where half-points are expected** — `size: 11` renders 5.5pt.
- **Missing `numbering.config`** for a numbered list — numbers vanish.
- **`ImageRun` without `type`** — throws in v9.
- **Relying on `HeadingLevel`** for appearance — you inherit Word's blue, not ours.
- **Column widths on the table only** — set them per cell.
- **Exiting before `Packer.toBuffer` resolves** — truncated or missing file.
- **`AlignmentType.JUSTIFIED`** — prohibited by the brand contract.
- **A border `size` in points** — it is eighths of a point; `size: 6` is 0.75pt.
- **Remote image or font URL** — no network in the sandbox.
