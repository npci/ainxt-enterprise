---
name: internal-brand
description: Binding visual identity contract for internal document artifacts (docx, pptx, xlsx, pdf). Set DOC_BRAND_FILE=brand/INTERNAL_BRAND.md in your .env to activate.
license: Apache-2.0
---

# Internal Document Identity — Binding Contract

Every artifact produced under this brand contract follows the organisation's
visual identity. This contract governs docx, pptx, xlsx and pdf equally.
Where a format recipe conflicts with this file, **this file wins**.

## 1. Palette

| Token        | Hex      | Use                                                       |
|--------------|----------|-----------------------------------------------------------|
| `navy`       | `1F3864` | Title bands, heading text, table header fill, dark slides |
| `navy-deep`  | `16294A` | Gradient partner for `navy` only. Never for text.         |
| `navy-tint`  | `E8EDF6` | Callout panels, KPI tiles                                 |
| `green`      | `00A551` | Positive status, KPI figures. Accent only.                |
| `amber`      | `C77700` | Caution / pending status                                  |
| `red`        | `B3261E` | Failure / breach status                                   |
| `row-fill`   | `F2F5FA` | Alternating table rows                                    |
| `rule`       | `C9D2E3` | Hairlines, cell borders, dividers                         |
| `ink`        | `222222` | Body text on light backgrounds                            |
| `ink-muted`  | `5A6472` | Captions, footers, axis labels                            |
| `paper`      | `FFFFFF` | Light background; also text on `navy`                     |

**Contrast — verified ratios, treat as hard limits:**

- `ink` on `paper` = 15.9:1 · `navy` on `paper` = 11.6:1 · `paper` on `navy` = 11.6:1.
  All pass WCAG AAA. These are the only combinations permitted for body text.
- `green` on `paper` = **3.2:1**. Legal only at ≥18pt, or ≥14pt bold, or for shapes
  and rules. **Never set body text, table text or captions in `green`.** For a
  positive figure inside body copy, set the figure in `ink` and carry the meaning
  with a `green` shape, arrow or fill beside it.
- Never place `green`, `amber` or `red` text on `navy`. On dark backgrounds status
  must be a shape, not coloured type.
- Colour is never the sole carrier of meaning. Always pair it with a label or glyph.

## 2. Type

**Arial only.** No web fonts, no font downloads, no bundled font files — Arial or a
metric equivalent is present on every target machine and inside the render
container. If Arial is unavailable fall back to Helvetica, then the system
sans-serif. Never substitute a serif.

| Role            | Size  | Weight  | Colour      |
|-----------------|-------|---------|-------------|
| Deck title      | 40pt  | Bold    | `paper`     |
| Document title  | 28pt  | Bold    | `navy`      |
| Slide heading   | 28pt  | Bold    | `navy`      |
| H1              | 18pt  | Bold    | `navy`      |
| H2              | 14pt  | Bold    | `navy`      |
| H3              | 12pt  | Bold    | `ink`       |
| Body            | 11pt  | Regular | `ink`       |
| Slide body      | 16pt  | Regular | `ink`       |
| Table header    | 10pt  | Bold    | `paper`     |
| Table cell      | 10pt  | Regular | `ink`       |
| Caption/footer  | 9pt   | Regular | `ink-muted` |
| KPI figure      | 32pt  | Bold    | `navy`      |

Line spacing 1.15 for body, 1.0 for headings. Never letter-space. Never set body
text in all-caps; small labels may use caps at 9pt with light tracking.

## 3. Geometry

Page default **A4** (India), not Letter. Slides **16:9**, never 4:3.

| Quantity      | inch   | pt   | twips (docx) | EMU (pptx)  |
|---------------|--------|------|--------------|-------------|
| A4 width      | 8.268  | 595  | 11906        | 7560310     |
| A4 height     | 11.693 | 842  | 16838        | 10692130    |
| Slide width   | 13.333 | 960  | —            | 12192000    |
| Slide height  | 7.5    | 540  | —            | 6858000     |
| Page margin   | 1.0    | 72   | 1440         | 914400      |
| Slide margin  | 0.6    | 43   | —            | 548640      |
| Baseline unit | 0.0556 | 4    | 80           | 50800       |

Conversions: `1in = 914400 EMU = 1440 twips = 72pt`; `1pt = 12700 EMU = 20 twips`.
All vertical spacing must be a whole multiple of the 4pt baseline unit.

## 4. Composition

- **Title page / opening slide** — full-bleed `navy`, or a `navy`→`navy-deep`
  gradient. Title in `paper` bold, left-aligned at the left margin, set at optical
  centre (~38% down). Date beneath at 12pt `paper`. Classification bottom-left.
- **Headings (docx/pdf)** — `navy` bold with a 0.75pt `navy` rule beneath spanning
  the text column. 12pt space above, 4pt below.
- **Tables** — `navy` header row, `paper` bold header text. Body rows alternate
  `paper` / `row-fill`. Horizontal `rule` hairlines only: no vertical borders, no
  outer box. Numerals right-aligned; text left-aligned; each header matches its
  column's alignment.
- **Slides** — one idea per slide. Either a `navy` band across the top (0.9in)
  carrying the heading in `paper`, or a full-bleed `navy` slide for section breaks
  and statements. Alternate light and dark slides so no two consecutive slides look
  identical. Body text left-aligned, never centred. Maximum 6 bullets, maximum 2
  lines each.
- **Footer (docx/pdf)** — `<DOC_BRAND_NAME> — Confidential` left (the brand name is
  omitted entirely when `DOC_BRAND_NAME` is unset), `Page N of M` right, 9pt
  `ink-muted`, on every page including the first.
- Generous whitespace. Left-align by default; centre only titles on full-bleed
  slides.

## 5. Prohibited

Clip-art · emoji · stock photography · any gradient other than `navy`→`navy-deep` ·
drop shadows · bevels · 3-D chart effects · pie charts with more than 5 slices ·
rainbow or default-Office palettes · centred body text · justified text · italic
for emphasis (use bold) · more than two type sizes on one slide · a decorative rule
under every heading on a slide · any remote asset (image, font, stylesheet) — the
renderer has no network.

## 6. Logo

If the brand mark (`DOC_BRAND_MARK`, default `/opt/ainxt-brand/brand-mark.png`)
exists, place it top-right on the title page or
opening slide, 1.2in wide, aligned to the right margin and vertically centred on
the title band. If the file is absent, omit it silently — never error, never draw a
placeholder box, never substitute text.

## 7. Classification

Every artifact carries a classification. Default `Confidential` unless instructed
otherwise. Title page: bottom-left, 9pt. Every subsequent page or slide: in the
footer. Permitted values: `Public`, `Internal`, `Confidential`, `Restricted`.
