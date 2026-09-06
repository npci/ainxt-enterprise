---
name: pptxgenjs-facts
description: API facts and failure modes for pptxgenjs v3 as installed in the AiNxt doc sandbox.
license: MIT
source: Compiled from the pptxgenjs project's public documentation.
---

# pptxgenjs v3 — API Facts

Installed globally in the sandbox as `pptxgenjs@3`. Require it directly; it is on
`NODE_PATH`. All positions and sizes are **inches** unless given as a `'NN%'` string.

## Setup and write

```js
const pptxgen = require('pptxgenjs');
const pres = new pptxgen();
pres.layout = 'LAYOUT_WIDE';          // 13.333 x 7.5in — set BEFORE addSlide()
pres.author  = process.env.DOC_BRAND_NAME || '';
pres.title   = 'Deck title';
const slide = pres.addSlide();
// ... build ...
pres.writeFile({ fileName: '/work/output.pptx' })
    .then(() => console.log('ok'))
    .catch(e => { console.error(e); process.exit(1); });
```

`writeFile` returns a Promise. The process must not exit before it resolves — always
chain `.then/.catch` and let the error path set a non-zero exit code so the build
reports a real failure instead of a silent empty file.

**Built-in layout names are not all the same canvas — this catches people out:**

| Name | Inches | Note |
|---|---|---|
| `LAYOUT_WIDE` | **13.333 × 7.5** | 16:9. **Use this one.** Brand geometry assumes it. |
| `LAYOUT_16x9` | 10 × 5.625 | Also 16:9 by ratio, but a smaller canvas |
| `LAYOUT_16x10` | 10 × 6.25 | |
| `LAYOUT_4x3` | 10 × 7.5 | Prohibited by the brand contract |

Picking `LAYOUT_16x9` while using brand coordinates silently pushes everything below
y=5.625 off the slide — the footer and classification simply vanish, and the file
still saves without an error. If page furniture is missing from a render, check this
first.

For a non-standard size use `pres.defineLayout({ name:'CUSTOM', width:13.333, height:7.5 })`
then `pres.layout = 'CUSTOM'`.

## Colour — the rule that breaks builds most often

Hex strings carry **no leading `#`**: `color: '1F3864'` is correct, `'#1F3864'` is
not. Six hex digits only — pptxgenjs does not accept an 8-digit alpha hex. For
transparency use the separate `transparency` property (0–100) on a fill.

## Text

```js
slide.addText('Heading', {
  x:0.6, y:0.18, w:9.5, h:0.54,
  fontFace:'Arial', fontSize:28, bold:true, color:'FFFFFF',
  align:'left', valign:'middle'
});
```

Rich runs — pass an array; each item's own `options` override the block options:

```js
slide.addText([
  { text:'98.7% ', options:{ bold:true, fontSize:32, color:'1F3864' } },
  { text:'settled automatically', options:{ fontSize:16, color:'222222' } }
], { x:0.6, y:1.4, w:7.4, h:0.8, fontFace:'Arial' });
```

Bullets: `{ bullet:true }`, or `{ bullet:{ code:'2022' } }` for an explicit glyph, or
`{ bullet:{ type:'number' } }` for numbering. Indent with `indentLevel:0|1|2`.

Overflow control: `fit:'shrink'` shrinks text to the box; `fit:'resize'` grows the
box. Prefer cutting words over shrinking — shrunk text below 12pt is unreadable on a
projector.

`breakLine:true` inside a run array forces a line break. `\n` also works in a plain
string.

## Shapes

```js
slide.addShape(pres.ShapeType.rect, {
  x:0.6, y:1.6, w:2.8, h:2.1,
  fill:{ color:'E8EDF6' },
  line:{ type:'none' }            // v3 spelling — NOT line:{ width:0 }
});
```

Useful `ShapeType` values: `rect`, `roundRect`, `ellipse`, `line`, `triangle`,
`rightArrow`, `chevron`. For `roundRect`, `rectRadius` takes inches, not a ratio.

To place text inside a shape, either pass `shape:` to `addText`, or draw the shape
and then `addText` at the same coordinates. Drawing order is z-order: earlier calls
sit behind later ones. Background art must therefore be added first.

## Backgrounds

```js
slide.background = { color:'1F3864' };                 // solid
slide.background = { path:'/work/cover.png' };          // local file only
```

There is no gradient background property. Simulate a `navy`→`navy-deep` gradient by
laying a full-bleed `navy` rect and then a partially transparent `navy-deep` rect
over part of it.

## Images

```js
slide.addImage({ path:'/work/chart.png', x:8.3, y:1.35, w:4.4, h:4.6 });
```

Local paths only — the sandbox has no network, so a URL will fail. Use `sizing:{
type:'cover'|'contain', w, h }` to crop rather than distort. Guard optional files:

```js
const fs = require('fs');
const mark = process.env.DOC_BRAND_MARK || '/opt/ainxt-brand/brand-mark.png';
if (fs.existsSync(mark)) slide.addImage({ path:mark, x:11.5, y:0.2, w:1.2, h:0.5 });
```

## Tables

```js
const header = ['Bank','Cycles','Value (₹ Cr)','Status'];
const rows = [[ 'Bank A', '1,204', '8,431', 'Settled' ]];
slide.addTable(
  [ header.map(t => ({ text:t, options:{ bold:true, color:'FFFFFF', fill:{ color:'1F3864' } } })),
    ...rows.map((r,i) => r.map(c => ({ text:c, options:{ fill:{ color: i%2 ? 'F2F5FA' : 'FFFFFF' } } })))
  ],
  { x:0.6, y:1.35, w:12.1, colW:[3.4,2.6,3.1,3.0],
    fontFace:'Arial', fontSize:10, color:'222222', valign:'middle',
    border:[ { type:'none' }, { type:'none' },
             { type:'solid', pt:0.75, color:'C9D2E3' }, { type:'none' } ] }
);
```

`border` as a 4-element array is `[top, right, bottom, left]`. Per-cell alignment
goes in that cell's `options.align`. `autoPage:false` keeps a table on one slide;
leave it off and a long table silently spills onto generated slides that bypass your
layout.

## Charts

```js
slide.addChart(pres.ChartType.bar,
  [{ name:'Settled', labels:['Q1','Q2','Q3','Q4'], values:[92,94,96,98] }],
  { x:0.6, y:1.35, w:7.4, h:4.6,
    chartColors:['1F3864','00A551','5A6472','C77700'],
    showLegend:true, legendPos:'b', legendFontFace:'Arial', legendFontSize:10,
    catAxisLabelFontSize:10, valAxisLabelFontSize:10,
    catAxisLabelColor:'5A6472', valAxisLabelColor:'5A6472',
    valGridLine:{ color:'C9D2E3', style:'solid', size:0.75 },
    catGridLine:{ style:'none' },
    showValue:false, dataLabelFontFace:'Arial', dataLabelFontSize:10 });
```

`ChartType`: `bar`, `line`, `pie`, `doughnut`, `area`, `scatter`, `radar`. For
horizontal bars set `barDir:'bar'` (`'col'` is vertical, and is the default). Multiple
series = multiple objects in the data array, each with the same `labels`.

## Speaker notes

```js
slide.addNotes('One sentence the presenter says here.');
```

## Failure modes to avoid

- **Shared options object.** `const o = {...}; slide.addText('a', o); slide.addText('b', o);`
  — pptxgenjs writes into `o`, so the second call inherits mutations. Build a fresh
  object each time, or spread: `{ ...base, x:6.9 }`.
- **`#` in a colour** — throws or renders black.
- **8-digit alpha hex** — unsupported; use `transparency`.
- **Setting `pres.layout` after `addSlide()`** — slides keep the old dimensions.
- **Exiting before `writeFile` resolves** — produces a truncated or missing file.
- **`line:{ width:0 }`** to hide a border — use `line:{ type:'none' }`.
- **Percent strings mixed with inches** in one shape's coordinates — pick one.
- **Remote image or font URL** — no network in the sandbox.
- **Table without `colW`** — columns distribute unevenly and text wraps badly.
