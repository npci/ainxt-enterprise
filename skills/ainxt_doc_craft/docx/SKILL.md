---
name: docx-craft
description: Build brand-compliant .docx (and PDF, via export) with the preinstalled ainxt-doc module. Read with SKELETON.js.
license: MIT
source: Authored from the docx (docx-js) public documentation and this project's brand contract (see ../brand/BRAND.md).
---

# Document Craft — DOCX and PDF

You are producing a document that will be circulated and relied on. Obey the
brand contract supplied above this file for every colour, size and prohibition.

**Do not write raw docx-js.** The sandbox has `ainxt-doc` preinstalled; it already
encodes the A4 geometry, the type scale, heading rules, table styling and the page
footer, so you express CONTENT only. This file tells you **how to compose**; the
API table below and `SKELETON.js` (a working file you may fill in) give you the
exact calls to write.

**PDF is authored here too.** A PDF request is written as a Word document and
exported; the export is faithful, so everything below applies unchanged. Do not
reach for a PDF-drawing library — you will lose styles, tables and pagination.

## The API — every call you have

```js
const doc = require('ainxt-doc');
const d = doc.create({ title: 'Title', subtitle: 'Subtitle', date: '31 Dec 2025',
                       classification: 'Confidential' });
```

Every call below returns `d`, so they chain. Pass plain strings — the module wraps
them in the correct docx objects.

| Call | Use it for |
|---|---|
| `d.h1(text)` · `d.h2(text)` · `d.h3(text)` | Headings. Never skip a level. |
| `d.p(text)` | Body paragraph. |
| `d.bullet(text)` | Bulleted point. |
| `d.step(text)` | Numbered step. |
| `d.caption(text)` | 9pt caption beneath a table. |
| `d.table(header, rows, { pct:[…], rightCols:[…] })` | Table. `pct` = column widths in percent (summing to 100), `rightCols` = 0-based numeric columns. Rows are arrays of plain strings. |
| `d.pageBreak()` | Start a new page. |
| `d.save()` | Writes `/work/output.docx`. Always last. |

The title block, page geometry, footer and logo come from `doc.create` — do not
build them yourself, and do not `require('docx')` directly.

## Step 1 — Decide the document's spine

Before writing code, write the section outline as a comment. A report that is one
undifferentiated run of paragraphs is the most common failure.

Standard spine for a briefing or report:

1. Title block — title, subtitle, date, classification
2. Purpose — 2–4 sentences. Why this document exists.
3. Summary — the answer up front, as 3–5 bullets or a short table. Never make the
   reader reach page 3 for the conclusion.
4. Body sections — one H1 per topic, H2 beneath where a topic splits
5. Data — tables, each with a caption stating what it shows
6. Risks / caveats — where relevant
7. Next steps — who does what, by when

Adjust for the ask. A memo collapses to Purpose / Position / Next steps. A
specification adds Scope and Assumptions. Never emit an empty section heading.

## Step 2 — Composition rules

The module already applies everything in this section. It is written out so you can
tell when a draft reads wrong — not so you can set any of it by hand.

**Headings.** H1 18pt bold `navy` with a 0.75pt `navy` rule beneath, 12pt above,
4pt below. H2 14pt bold `navy`, no rule. H3 12pt bold `ink`. Never skip a level
(no H3 directly under H1). Sentence case, not Title Case.

**Body.** 11pt `ink`, line spacing 1.15, 8pt after each paragraph, left-aligned.
Never justify — justified text produces rivers of whitespace in Word's engine.
Keep paragraphs to 4 sentences or fewer; split rather than run on.

**Lists.** Bulleted for unordered points, numbered for sequences and anything the
reader must cite. Two levels maximum. A bullet is a phrase or one sentence — if it
needs two sentences it is a paragraph.
Use `d.bullet('text')` for a single item. Use `d.bullets(['a','b','c'])` to add
multiple items at once. There is NO `d.bullets` that takes individual string
arguments — it takes ONE array. Never call `d.bullets('text')` with a plain string.

**Tables.** Header row filled `navy` with 10pt bold `paper`. Body rows alternate
`paper` / `row-fill`. Horizontal hairlines in `rule` only: no vertical borders and
no outer box. Numbers right-aligned, text left-aligned, headers matching their
column. Give every table a 9pt `ink-muted` caption beneath saying what it shows and
the period it covers. Set explicit column widths — Word's autofit is unreliable
across viewers.

**Emphasis.** Bold only. No italic, no underline (underline reads as a broken
hyperlink), no coloured body text. `green`/`amber`/`red` may appear only in a status
cell at 10pt or above, and must be accompanied by the word — never colour alone.

**Page furniture.** A4 portrait, 1in margins. Footer on every page: `<brand> —
Confidential` left, `Page N of M` right, 9pt `ink-muted`. Title block on page 1
with a `navy` rule beneath it. Logo top-right of page 1 if the file exists.

## Step 3 — Numbers and currency

Indian digit grouping where the audience is domestic: `₹1,23,45,678`. Where the
document is international, use `₹12,345,678` and say which convention you used.
State the unit in the column header (`Value (₹ Cr)`), not in every cell. Negatives
in parentheses, not with a minus sign. Percentages to one decimal. Dates as
`DD Mon YYYY` — never a bare numeric date, which is ambiguous across regions.

## Step 4 — Never fabricate data

If the user did not give you a number, there is no number. Do not invent figures,
percentages, growth rates, benchmarks or currency amounts to fill a table or make a
summary sound authoritative. Where the document genuinely needs a value the user did
not supply, either leave the row out or name the source the reader must fill in.
Fabricated data presented as fact is worse than no data at all.

## Step 5 — Self-check before you call build_document

Read your own script against this list and fix what fails. Do not skip it.

1. Does the summary appear before the detail?
2. Every heading level used in order, no skips, no empty sections?
3. Any justified paragraph? → left-align.
4. Any italic or underline used for emphasis? → make it bold.
5. Any coloured body text? → set it `ink`.
6. Every table: `navy` header, alternating rows, no vertical borders, explicit
   column widths, a caption beneath?
7. Any hand-built docx object (`new Paragraph`, `new Table`, a raw `size:` or
   `color:` value)? → replace it with the matching `d.*` call.
8. Any number, percentage or currency figure the user never gave you? → remove it.
9. Footer present with page number and classification?
10. Every table row exactly as long as its header row?
11. Placeholder text — `Lorem`, `TODO`, `REPLACE`, `Sample` — all gone?
12. Does the script end with `d.save()`?

## Step 6 — Output contract

Write **one** self-contained script that starts `const doc = require('ainxt-doc');`.
That module is preinstalled in the sandbox and already enforces A4 geometry, the
type scale, heading rules with `keepNext`, table styling and the page footer.
Express CONTENT only. Finish with `d.save()`, which writes `/work/output.docx` — for a PDF request write
`/work/output.docx` too; the export happens outside your script. No network calls.
Images, if any, are already on disk in `/work/` under the names you were given.

If a build fails, read the error, fix the script, and call `build_document` again
with the same `artifact_id`.
