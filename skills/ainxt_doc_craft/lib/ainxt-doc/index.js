// SPDX-License-Identifier: Apache-2.0
/**
 * ainxt-doc — brand-aware DOCX (and PDF, via export) composition, preinstalled
 * in the doc sandbox.
 *
 *   const doc = require('ainxt-doc');
 *   const d = doc.create({ title: 'UPI Settlement', subtitle: '…', date: '31 Dec 2025' });
 *   d.h1('Purpose'); d.p('…'); d.bullet('…');
 *   d.table(['Bank','Cycles'], [['A','1,204']], { pct:[60,40], rightCols:[1] });
 *   d.caption('What this shows.');
 *   d.save();
 */
'use strict';

const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  WidthType, BorderStyle, ShadingType, AlignmentType, Footer,
  PageNumber, ImageRun, LevelFormat, convertMillimetersToTwip,
} = require('docx');
const fs = require('fs');

const C = {
  navy: '1F3864', navyTint: 'E8EDF6', green: '00A551', amber: 'C77700',
  red: 'B3261E', rowFill: 'F2F5FA', rule: 'C9D2E3',
  ink: '222222', inkMuted: '5A6472', paper: 'FFFFFF',
};
const FONT = 'Arial';
// Font sizes are HALF-POINTS; spacing is TWIPS (20 per point).
const SZ = { title: 56, h1: 36, h2: 28, h3: 24, body: 22, cell: 20, small: 18 };
const MARGIN = 1440;                                        // 1in
const TEXT_W = convertMillimetersToTwip(210) - 2 * MARGIN;  // A4 text column
// Optional brand mark. Filename is configurable so an adopter can drop in
// their own logo without editing this library; every use is guarded by an
// existence check, so an absent file simply omits the mark.
// Document identity. Empty by default: an adopter's documents carry their own
// brand or none, never this project's. Set DOC_BRAND_NAME when building the doc
// sandbox image (--build-arg) -- the container is run with no host environment
// passed in, so a runtime-only variable would never reach this code.
const BRAND = process.env.DOC_BRAND_NAME || '';
const brandLabel = (cls) => (BRAND ? `${BRAND} \u2014 ${cls}` : String(cls));

const MARK = process.env.DOC_BRAND_MARK || '/opt/ainxt-brand/brand-mark.png';

const NO_BORDER = { style: BorderStyle.NONE, size: 0, color: C.paper };
const HAIRLINE = { style: BorderStyle.SINGLE, size: 6, color: C.rule };  // 0.75pt

class Doc {
  constructor(opts = {}) {
    this.classification = opts.classification || 'Confidential';
    this.out = opts.out || '/work/output.docx';
    this.children = [];
    if (opts.title) this.titleBlock(opts.title, opts.subtitle, opts.date);
  }

  titleBlock(title, subtitle, dateStr) {
    if (fs.existsSync(MARK)) {
      this.children.push(new Paragraph({
        alignment: AlignmentType.RIGHT,
        children: [new ImageRun({ type: 'png', data: fs.readFileSync(MARK),
                                  transformation: { width: 115, height: 48 } })],
      }));
    }
    this.children.push(new Paragraph({
      children: [new TextRun({ text: String(title), bold: true, size: SZ.title,
                               color: C.navy, font: FONT })],
      spacing: { before: 240, after: 80 },
    }));
    if (subtitle) this.children.push(new Paragraph({
      children: [new TextRun({ text: String(subtitle), size: SZ.h3, color: C.ink, font: FONT })],
      spacing: { after: 80 },
    }));
    this.children.push(new Paragraph({
      children: [new TextRun({
        text: `${dateStr || ''}  ·  ${brandLabel(this.classification)}`,
        size: SZ.small, color: C.inkMuted, font: FONT })],
      spacing: { after: 120 },
      border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: C.navy, space: 6 } },
    }));
    this.children.push(new Paragraph({ text: '', spacing: { after: 240 } }));
    return this;
  }

  // keepNext binds a heading to what follows so it is never stranded at a page foot.
  h1(text) {
    this.children.push(new Paragraph({
      children: [new TextRun({ text: String(text), bold: true, size: SZ.h1, color: C.navy, font: FONT })],
      spacing: { before: 240, after: 80 }, keepNext: true,
      border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: C.navy, space: 4 } },
    }));
    return this;
  }
  h2(text) {
    this.children.push(new Paragraph({
      children: [new TextRun({ text: String(text), bold: true, size: SZ.h2, color: C.navy, font: FONT })],
      spacing: { before: 200, after: 80 }, keepNext: true }));
    return this;
  }
  h3(text) {
    this.children.push(new Paragraph({
      children: [new TextRun({ text: String(text), bold: true, size: SZ.h3, color: C.ink, font: FONT })],
      spacing: { before: 160, after: 80 }, keepNext: true }));
    return this;
  }
  p(text) {
    this.children.push(new Paragraph({
      children: [new TextRun({ text: String(text), size: SZ.body, color: C.ink, font: FONT })],
      spacing: { after: 160, line: 276 }, alignment: AlignmentType.LEFT }));
    return this;
  }
  bullet(text) {
    this.children.push(new Paragraph({
      children: [new TextRun({ text: String(text), size: SZ.body, color: C.ink, font: FONT })],
      bullet: { level: 0 }, spacing: { after: 80, line: 276 } }));
    return this;
  }
  /** Convenience alias: d.bullets(['a','b','c']) — accepts an array and calls bullet() for each. */
  bullets(items) {
    (Array.isArray(items) ? items : [items]).forEach(t => this.bullet(t));
    return this;
  }
  step(text) {
    this.children.push(new Paragraph({
      children: [new TextRun({ text: String(text), size: SZ.body, color: C.ink, font: FONT })],
      numbering: { reference: 'ainxt-steps', level: 0 }, spacing: { after: 80, line: 276 } }));
    return this;
  }
  caption(text) {
    this.children.push(new Paragraph({
      children: [new TextRun({ text: String(text), size: SZ.small, color: C.inkMuted, font: FONT })],
      spacing: { before: 80, after: 240 } }));
    return this;
  }
  pageBreak() {
    this.children.push(new Paragraph({ children: [], pageBreakBefore: true }));
    return this;
  }

  /** table(header, rows, { pct:[...], rightCols:[...] }) */
  table(header, rows, opts = {}) {
    const cols = (header || []).length || 1;
    const pct = opts.pct || Array(cols).fill(Math.floor(100 / cols));
    const rightCols = opts.rightCols || [];
    const cell = (text, i, o) => new TableCell({
      width: { size: pct[i], type: WidthType.PERCENTAGE },
      shading: { type: ShadingType.CLEAR, color: 'auto', fill: o.fill },
      margins: { top: 80, bottom: 80, left: 120, right: 120 },
      children: [new Paragraph({
        alignment: rightCols.indexOf(i) !== -1 ? AlignmentType.RIGHT : AlignmentType.LEFT,
        children: [new TextRun({ text: String(text), bold: !!o.bold, size: SZ.cell,
                                 color: o.head ? C.paper : C.ink, font: FONT })],
      })],
    });
    this.children.push(new Table({
      width: { size: 100, type: WidthType.PERCENTAGE },
      borders: { top: NO_BORDER, bottom: HAIRLINE, left: NO_BORDER, right: NO_BORDER,
                 insideHorizontal: HAIRLINE, insideVertical: NO_BORDER },
      rows: [
        new TableRow({ tableHeader: true,
          children: (header || []).map((t, i) => cell(t, i, { bold: true, head: true, fill: C.navy })) }),
        ...(rows || []).map((r, ri) => new TableRow({
          children: r.map((t, i) => cell(t, i, { fill: ri % 2 ? C.rowFill : C.paper })) })),
      ],
    }));
    return this;
  }

  _footer() {
    return new Footer({ children: [new Paragraph({
      tabStops: [{ type: 'right', position: TEXT_W }],
      children: [
        new TextRun({ text: brandLabel(this.classification), size: SZ.small, color: C.inkMuted, font: FONT }),
        new TextRun({ text: '\t', size: SZ.small }),
        new TextRun({ text: 'Page ', size: SZ.small, color: C.inkMuted, font: FONT }),
        new TextRun({ children: [PageNumber.CURRENT], size: SZ.small, color: C.inkMuted, font: FONT }),
        new TextRun({ text: ' of ', size: SZ.small, color: C.inkMuted, font: FONT }),
        new TextRun({ children: [PageNumber.TOTAL_PAGES], size: SZ.small, color: C.inkMuted, font: FONT }),
      ],
    })] });
  }

  save() {
    const doc = new Document({
      styles: { default: { document: { run: { font: FONT, size: SZ.body, color: C.ink } } } },
      numbering: { config: [{ reference: 'ainxt-steps', levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: '%1.', alignment: AlignmentType.START },
      ] }] },
      sections: [{
        properties: { page: {
          size: { width: convertMillimetersToTwip(210), height: convertMillimetersToTwip(297) },
          margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN } } },
        footers: { default: this._footer() },
        children: this.children,
      }],
    });
    return Packer.toBuffer(doc)
      .then(b => { fs.writeFileSync(this.out, b); console.log(`ainxt-doc: wrote ${this.out}`); })
      .catch(err => { console.error(err); process.exit(1); });
  }
}

// Re-export the docx primitives that generated build scripts may need directly.
// This lets code that does const { BorderStyle } = require("ainxt-doc") work
// without a separate require("docx") line, preventing the common failure mode
// where BorderStyle (or similar) is destructured from ainxt-doc and comes back
// undefined, causing "Cannot read properties of undefined (reading SINGLE)".
// NOTE: these are already declared at the top of this file; we just re-export them.
const { Header, convertInchesToTwip } = require("docx");  // the two not imported above

module.exports = {
  create: (opts) => new Doc(opts), Doc, COLORS: C, FONT,
  // docx primitives -- available as const { BorderStyle } = require("ainxt-doc")
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  WidthType, BorderStyle, ShadingType, AlignmentType, Header, Footer,
  PageNumber, ImageRun, LevelFormat, convertMillimetersToTwip, convertInchesToTwip,
};
