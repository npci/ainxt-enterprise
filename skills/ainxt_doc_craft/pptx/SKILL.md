---
name: pptx-craft
description: Build brand-compliant .pptx decks with the preinstalled ainxt-deck module. Read with SKELETON.js.
license: Apache-2.0
source: Authored from the pptxgenjs public documentation and this project's brand contract (see ../brand/BRAND.md).
---

# Deck Craft — PPTX

You are producing a deck an executive will present.

**Do not write raw pptxgenjs.** The sandbox has `ainxt-deck` preinstalled. It already
encodes every rule in `INTERNAL_BRAND.md` — the 16:9 canvas, margins, palette, type
scale, contrast limits, table styling, chart defaults, the classification on every
slide. Your job is the CONTENT. A deck written against the module is ~40 lines; the
same deck in raw pptxgenjs is ~300 and gets the brand wrong.

```js
const deck = require('ainxt-deck');
const d = deck.create({ classification: 'Confidential' });
d.cover('Title', 'Subtitle', '31 Dec 2025');
d.metric('Quarter at a glance', [{ figure: '98.7%', label: 'Cycles auto-settled', status: 'good' }]);
d.save();
```

## Step 1 — Plan before you write code

Write the slide plan as a comment at the top. For each slide: pattern, heading, and
the single idea it carries.

- One idea per slide. If a slide needs two verbs, split it.
- 6–14 slides. Never pad.
- **Never two identical patterns back to back.** The module warns in the build log
  when you do.
- Dark slides are `cover`, `statement`, `close`. Everything else is light.
  Alternate so the deck breathes.

## Step 2 — The API, in full

Every call returns `d`, so they chain. All are optional except `cover` and `save`.

| Call | Use it for |
|---|---|
| `d.cover(title, subtitle, date)` | Opening slide. Dark, logo, motif, classification. |
| `d.contents(heading, [labels])` | Numbered agenda. Max 7. |
| `d.metric(heading, [tiles], note?)` | 2–4 KPI tiles. `{figure, label, status}`, status is `'good'\|'warn'\|'bad'`. |
| `d.evidence(heading, [bullets], visual?)` | The workhorse. Max 6 bullets. `visual` is `{image:'/work/x.png'}` or `{chart:{…}}`, or omit for full width. |
| `d.split(heading, [panelA, panelB])` | Two columns. Each `{title, bullets:[…]}`. Risk/mitigation, before/after. |
| `d.table(heading, header, rows, opts)` | Table on its own slide. `opts = {colW:[…], rightCols:[…]}`. Max 7 rows. |
| `d.statement(sentence, attribution?)` | One dark slide, one sentence. Section breaks and the line to remember. |
| `d.close(line, [nextSteps])` | Final slide. Max 3 next steps. |
| `d.notes(text)` | Speaker notes on the slide you just added. |
| `d.save()` | Writes `/work/output.pptx`. Always last. |

Chart spec: `{ type:'bar'|'line'|'pie', data:[{name, labels:[…], values:[…]}], minVal?, labelFormat? }`.

## Step 3 — The judgements the module cannot make for you

The module enforces the brand. It cannot decide what is worth saying, and it cannot
tell whether your chart is honest. These are yours:

**Right-align numerals.** Pass `rightCols` on every table with numeric columns. The
module aligns what you tell it to; it cannot tell a count from a label.

**Choose the chart honestly.** Bars are read by length, so the module starts the
value axis at zero. That has a consequence: if your values cluster in a narrow band
far from zero (98.1%, 98.6%, 99.1%), a zero-based bar chart renders three identical
bars and says nothing — while a truncated axis would exaggerate a 0.5-point move
into a landslide. Neither is acceptable. Do one of:

- **Chart the complement** — plot the exception rate (1.9, 1.4, 0.9) rather than the
  success rate. It starts near zero, so the movement is real and the axis honest.
- **Use a line chart**, where a non-zero axis reads as a trend, and set `minVal`.
- **Don't chart it.** Three numbers in a `metric` row beat a flat bar chart.

**Keep bullets short.** Max 2 lines each at 16pt — roughly 60 characters a line in
the standard column. Cut words; do not rely on shrink-to-fit.

**Say something on each slide.** A heading plus three thin bullets on an empty
canvas is the most common failure. Give the slide substance or merge it.

## Step 4 — Self-check before you call build_document

1. Any pattern repeated on consecutive slides? → change one.
2. Any two dark slides adjacent? → rebalance.
3. Every table with numbers: is `rightCols` set?
4. Is any chart's story invisible because the values cluster? → chart the complement.
5. Every bullet ≤ 2 lines?
6. Placeholder text — `REPLACE`, `TODO`, `Lorem`, `Sample` — all gone?
7. Does the script end in `d.save()`?

## Step 5 — Output contract

Write **one** self-contained script beginning `const deck = require('ainxt-deck');`
and ending `d.save();`. Express content only. No network calls. Images, if any, are
already on disk in `/work/` under the names you were given — pass the path.

Raw `pptxgenjs` is available as an escape hatch for a layout no pattern covers, but
then every brand rule is yours to satisfy by hand, and you must still finish by
writing `/work/output.pptx`. Prefer the module.

If a build fails, read the error, fix the script, and call `build_document` again
with the same `artifact_id`.
