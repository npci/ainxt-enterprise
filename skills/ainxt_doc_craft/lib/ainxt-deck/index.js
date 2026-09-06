// SPDX-License-Identifier: MIT
/**
 * ainxt-deck — brand-aware PPTX composition, preinstalled in the doc sandbox.
 *
 * Exists so a model only has to express CONTENT. Every geometry, colour and
 * accessibility rule from INTERNAL_BRAND.md is enforced here, which means a small
 * local model gets the same output as a frontier one — it writes ~15 lines
 * instead of reproducing ~300 lines of helper definitions it cannot copy
 * reliably.
 *
 *   const deck = require('ainxt-deck');
 *   const d = deck.create({ classification: 'Confidential' });
 *   d.cover('Title', 'Subtitle', '31 Dec 2025');
 *   d.metric('Quarter at a glance', [{ figure: '98.7%', label: '…', status: 'good' }]);
 *   d.save();
 */
'use strict';

const pptxgen = require('pptxgenjs');
const fs = require('fs');

// ── Brand tokens (INTERNAL_BRAND.md) ──────────────────────────────────────────
const C = {
  navy: '1F3864', navyDeep: '16294A', navyTint: 'E8EDF6',
  green: '00A551', amber: 'C77700', red: 'B3261E',
  rowFill: 'F2F5FA', rule: 'C9D2E3',
  ink: '222222', inkMuted: '5A6472', paper: 'FFFFFF',
};
const FONT = 'Arial';
const M = 0.6;                      // margin (in)
const SW = 13.333, SH = 7.5;        // LAYOUT_WIDE
const CONTENT_W = SW - 2 * M;
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
const STATUS = { good: C.green, warn: C.amber, bad: C.red };

class Deck {
  constructor(opts = {}) {
    this.classification = opts.classification || 'Confidential';
    this.out = opts.out || '/work/output.pptx';
    this.pres = new pptxgen();
    // LAYOUT_WIDE is 13.333x7.5. LAYOUT_16x9 is 10x5.625 — using it would push
    // every footer and classification off the slide.
    this.pres.layout = 'LAYOUT_WIDE';
    if (BRAND) this.pres.author = BRAND;
    if (opts.title) this.pres.title = opts.title;
    this._last = null;              // previous pattern, for variety enforcement
    this._n = 0;
  }

  // ── internals ──────────────────────────────────────────────────────────────
  _track(pattern) {
    if (this._last === pattern && pattern !== 'evidence') {
      // Two identical layouts back to back is the top "AI deck" tell. Rather
      // than fail the build, note it so the caller can see it in the log.
      console.warn(`ainxt-deck: consecutive '${pattern}' slides — vary the pattern`);
    }
    this._last = pattern; this._n += 1;
  }

  _logo(s) {
    if (fs.existsSync(MARK)) {
      s.addImage({ path: MARK, x: SW - M - 1.2, y: 0.2, w: 1.2, h: 0.5 });
    }
  }

  _classify(s, onDark) {
    s.addText(brandLabel(this.classification), {
      x: M, y: SH - 0.8, w: 5, h: 0.3, fontFace: FONT, fontSize: 9,
      color: onDark ? C.paper : C.inkMuted,
    });
  }

  // Banded wash: pptxgenjs has no gradient background, and one translucent rect
  // leaves a hard seam that reads as a rendering fault.
  _dark() {
    const s = this.pres.addSlide();
    s.background = { color: C.navy };
    const bands = 14, top = SH * 0.35, h = (SH - top) / bands;
    for (let i = 0; i < bands; i++) {
      s.addShape(this.pres.ShapeType.rect, {
        x: 0, y: top + i * h, w: SW, h: h + 0.02,
        fill: { color: C.navyDeep, transparency: Math.round(100 - (i + 1) * (55 / bands)) },
        line: { type: 'none' },
      });
    }
    return s;
  }

  _band(heading) {
    const s = this.pres.addSlide();
    s.addShape(this.pres.ShapeType.rect, {
      x: 0, y: 0, w: SW, h: 0.9, fill: { color: C.navy }, line: { type: 'none' },
    });
    s.addText(String(heading || ''), {
      x: M, y: 0.18, w: CONTENT_W - 1.4, h: 0.54,
      fontFace: FONT, fontSize: 28, bold: true, color: C.paper, valign: 'middle',
      fit: 'shrink',
    });
    // Classification on every slide, not just cover and close.
    this._classify(s, false);
    return s;
  }

  _motif(s, y, h) {
    s.addShape(this.pres.ShapeType.rect, {
      x: M, y, w: 0.09, h, fill: { color: C.green }, line: { type: 'none' },
    });
  }

  _bullets(s, items, x, y, w, h, colour) {
    s.addText((items || []).slice(0, 6).map(t => ({
      text: String(t), options: { bullet: { code: '2022' } },
    })), {
      x, y, w, h, fontFace: FONT, fontSize: 16, color: colour || C.ink,
      lineSpacingMultiple: 1.15, paraSpaceAfter: 8, valign: 'top', fit: 'shrink',
    });
  }

  // ── patterns ───────────────────────────────────────────────────────────────
  cover(title, subtitle, dateStr) {
    const s = this._dark(); this._track('cover');
    this._logo(s); this._motif(s, 2.55, 1.7);
    const x = M + 0.34;
    s.addText(String(title || ''), {
      x, y: 2.55, w: 10.2, h: 1.7, fontFace: FONT, fontSize: 40, bold: true,
      color: C.paper, valign: 'middle', fit: 'shrink',
    });
    if (subtitle) s.addText(String(subtitle), {
      x, y: 4.35, w: 10.2, h: 0.45, fontFace: FONT, fontSize: 16, color: C.paper });
    if (dateStr) s.addText(String(dateStr), {
      x, y: 4.85, w: 10.2, h: 0.4, fontFace: FONT, fontSize: 12, color: C.paper });
    this._classify(s, true);
    return this;
  }

  contents(heading, items) {
    const s = this._band(heading); this._track('contents');
    (items || []).slice(0, 7).forEach((label, i) => {
      const y = 1.5 + i * 0.72;
      s.addShape(this.pres.ShapeType.rect, {
        x: M, y, w: 0.42, h: 0.42, fill: { color: C.navy }, line: { type: 'none' } });
      s.addText(String(i + 1), {
        x: M, y, w: 0.42, h: 0.42, fontFace: FONT, fontSize: 12, bold: true,
        color: C.paper, align: 'center', valign: 'middle' });
      s.addText(String(label), {
        x: M + 0.66, y, w: CONTENT_W - 0.66, h: 0.42,
        fontFace: FONT, fontSize: 16, color: C.ink, valign: 'middle' });
    });
    return this;
  }

  /** visual: null | {image:'/work/x.png'} | {chart:{type,data,minVal?,labelFormat?}} */
  evidence(heading, bullets, visual) {
    const s = this._band(heading); this._track('evidence');
    const hasVisual = !!(visual && (visual.image || visual.chart));
    this._bullets(s, bullets, M, 1.35, hasVisual ? 7.4 : CONTENT_W, 4.7);
    if (visual && visual.image && fs.existsSync(visual.image)) {
      s.addImage({ path: visual.image, x: 8.3, y: 1.35, w: 4.4, h: 4.6,
                   sizing: { type: 'cover', w: 4.4, h: 4.6 } });
    } else if (visual && visual.chart) {
      this._chart(s, visual.chart, 8.3, 1.35, 4.4, 4.6);
    }
    return this;
  }

  _chart(s, spec, x, y, w, h) {
    const series = spec.data || [];
    const multi = series.length > 1;
    const type = spec.type || this.pres.ChartType.bar;
    s.addChart(type, series, {
      x, y, w, h,
      // One colour per SERIES. varyColors on paints each bar of a single series
      // a different colour, implying categories that do not exist.
      varyColors: false,
      chartColors: multi ? [C.navy, C.green, C.inkMuted, C.amber] : [C.navy],
      showLegend: multi, legendPos: 'b', legendFontFace: FONT, legendFontSize: 10,
      // Bars are read by length: the axis starts at zero unless explicitly told
      // otherwise. If your values cluster far from zero, chart the complement.
      valAxisMinVal: (spec.minVal !== undefined ? spec.minVal : 0),
      catAxisLabelFontFace: FONT, catAxisLabelFontSize: 11, catAxisLabelColor: C.inkMuted,
      valAxisLabelFontFace: FONT, valAxisLabelFontSize: 11, valAxisLabelColor: C.inkMuted,
      valGridLine: { color: C.rule, style: 'solid', size: 0.75 },
      catGridLine: { style: 'none' },
      showValue: !multi, dataLabelFontFace: FONT, dataLabelFontSize: 11,
      dataLabelColor: C.ink, dataLabelPosition: 'outEnd',
      dataLabelFormatCode: spec.labelFormat || '#,##0.0',
    });
  }

  /** panels: [{title, bullets[]}, {title, bullets[]}] */
  split(heading, panels) {
    const s = this._band(heading); this._track('split');
    [M, 6.9].forEach((x, i) => {
      const p = (panels || [])[i];
      if (!p) return;
      s.addText(String(p.title || ''), {
        x, y: 1.35, w: 5.9, h: 0.45,
        fontFace: FONT, fontSize: 14, bold: true, color: C.navy });
      this._bullets(s, p.bullets, x, 1.9, 5.9, 4.1);
    });
    return this;
  }

  /** tiles: [{figure, label, status:'good'|'warn'|'bad'}] — 2 to 4 */
  metric(heading, tiles, note) {
    const s = this._band(heading); this._track('metric');
    const list = (tiles || []).slice(0, 4);
    const n = Math.min(Math.max(list.length, 2), 4);
    const gutter = 0.35, w = (CONTENT_W - gutter * (n - 1)) / n;
    const y = 1.7, h = 3.1;
    list.forEach((t, i) => {
      const x = M + i * (w + gutter);
      s.addShape(this.pres.ShapeType.rect, {
        x, y, w, h, fill: { color: C.navyTint }, line: { type: 'none' } });
      // Status is carried by a shape, never by coloured small text (green on
      // white is 3.2:1 and fails legibility below 18pt).
      s.addShape(this.pres.ShapeType.rect, {
        x, y, w: 0.09, h, fill: { color: STATUS[t.status] || C.green }, line: { type: 'none' } });
      s.addText(String(t.figure), {
        x: x + 0.42, y: y + 0.55, w: w - 0.84, h: 1.15,
        fontFace: FONT, fontSize: 40, bold: true, color: C.navy,
        valign: 'middle', fit: 'shrink' });
      s.addText(String(t.label || ''), {
        x: x + 0.42, y: y + 1.8, w: w - 0.84, h: 1.0,
        fontFace: FONT, fontSize: 13, color: C.inkMuted, valign: 'top' });
    });
    if (note) s.addText(String(note), {
      x: M, y: y + h + 0.45, w: CONTENT_W, h: 0.7,
      fontFace: FONT, fontSize: 13, color: C.ink, valign: 'top' });
    return this;
  }

  statement(sentence, attribution) {
    const s = this._dark(); this._track('statement');
    this._motif(s, 2.55, 2.0);
    s.addText(String(sentence || ''), {
      x: M + 0.34, y: 2.55, w: 11.3, h: 2.0, fontFace: FONT, fontSize: 28,
      bold: true, color: C.paper, valign: 'middle', fit: 'shrink' });
    if (attribution) s.addText(String(attribution), {
      x: M + 0.34, y: 4.65, w: 11.3, h: 0.4, fontFace: FONT, fontSize: 12, color: C.paper });
    this._classify(s, true);
    return this;
  }

  /** A table on its own slide. rightCols: 0-based numeric columns. */
  table(heading, header, rows, opts = {}) {
    const s = this._band(heading); this._track('table');
    const cols = (header || []).length || 1;
    const colW = opts.colW || Array(cols).fill(CONTENT_W / cols);
    const rightCols = opts.rightCols || [];
    const align = i => (rightCols.indexOf(i) !== -1 ? 'right' : 'left');
    const head = (header || []).map((t, i) => ({
      text: String(t),
      options: { bold: true, color: C.paper, fill: { color: C.navy }, align: align(i) } }));
    const body = (rows || []).slice(0, 7).map((r, ri) => r.map((c, i) => ({
      text: String(c),
      options: { fill: { color: ri % 2 ? C.rowFill : C.paper }, align: align(i) } })));
    const top = 1.5;
    const rowH = Math.min(0.62, Math.max(0.42, (SH - top - 1.0) / (body.length + 1)));
    s.addTable([head, ...body], {
      x: M, y: top, w: CONTENT_W, colW, rowH,
      fontFace: FONT, fontSize: 12, color: C.ink, valign: 'middle',
      autoPage: false, margin: [4, 8, 4, 8],
      border: [{ type: 'none' }, { type: 'none' },
               { type: 'solid', pt: 0.75, color: C.rule }, { type: 'none' }],
    });
    return this;
  }

  close(line, nextSteps) {
    const s = this._dark(); this._track('close');
    this._motif(s, 1.9, 1.1);
    s.addText(String(line || ''), {
      x: M + 0.34, y: 1.9, w: 11.3, h: 1.1, fontFace: FONT, fontSize: 32,
      bold: true, color: C.paper, valign: 'middle' });
    if (nextSteps && nextSteps.length) {
      this._bullets(s, nextSteps.slice(0, 3), M + 0.34, 3.3, 10.6, 2.6, C.paper);
    }
    this._classify(s, true);
    return this;
  }

  notes(text) {                      // speaker notes on the most recent slide
    const slides = this.pres.slides || [];
    if (slides.length) slides[slides.length - 1].addNotes(String(text));
    return this;
  }

  /** Writes the deck. Returns the Promise so failures exit non-zero. */
  save() {
    return this.pres.writeFile({ fileName: this.out })
      .then(() => { console.log(`ainxt-deck: wrote ${this.out} (${this._n} slides)`); })
      .catch(err => { console.error(err); process.exit(1); });
  }
}

module.exports = {
  create: (opts) => new Deck(opts),
  Deck, COLORS: C, FONT, SLIDE: { w: SW, h: SH, margin: M },
};
