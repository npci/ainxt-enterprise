---
name: xlsx-craft
description: Build brand-compliant .xlsx with openpyxl, with real formulas and a LibreOffice recalculation check. Read with SKELETON.py.
license: Apache-2.0
source: Authored from the openpyxl public documentation, standard financial-modelling convention, and this project's brand contract (see ../brand/BRAND.md).
---

# Workbook Craft — XLSX

You are producing a workbook an analyst will audit, extend and trust. Obey
`INTERNAL_BRAND.md` (supplied above this file). `SKELETON.py` is a working file you may
fill in rather than starting from nothing.

## Read the prompt first — pick the right shape

Before writing a single line of code, classify the user's request into one of two
workbook types. The wrong type produces a document that is technically correct but
completely wrong for the user.

### Type A — Reference / List workbook

The user wants a **table of information** to read, share, or print.

Signals: questions, steps, items, topics, definitions, comparisons, checklists,
schedules, plans, guides, FAQs, syllabi, roadmaps.

Examples: "interview questions", "onboarding checklist", "feature comparison",
"project milestones", "API reference", "training topics".

Shape:
- One sheet (or one sheet per logical group — e.g. one sheet per topic area).
- Columns are the **attributes of each item**. Only include a column if the user's
  prompt implies it is wanted — see the column-decision rule below.
- **No Summary sheet, no formulas, no chart** — unless the user explicitly asks.
- Use `b.sheet()` with text columns; `right_cols=[]`, no numeric `formats`, no
  `total_row()`, no `b.summary()`, no `b.chart()`.
- Column widths: wide for text columns (40–60), narrow for index/status (6–16).
- Row heights: let the library wrap text naturally — do not force single-line rows
  for long content.

### Type B — Data / Analytics workbook

The user wants a **live, auditable dataset** with totals, ratios, or a chart.

Signals: numbers, amounts, counts, percentages, dates, metrics, settlement,
performance, revenue, transactions, report, dashboard, tracker.

Examples: "UPI settlement data", "branch performance", "monthly transaction summary",
"expense tracker", "sales report".

Shape: use the **financial skeleton** in `SKELETON.py` as-is — Summary sheet + data
sheet + formulas + chart.

### Deciding columns — do not over-engineer

Only add a column if the user's prompt **explicitly or clearly implies** it.

| Prompt says… | Include |
|---|---|
| "interview questions" | `#`, `Question` |
| "interview questions with answers" / "Q&A" / "solutions" | `#`, `Question`, `Answer` |
| "interview questions with difficulty" | `#`, `Question`, `Difficulty` |
| "onboarding checklist" | `#`, `Task`, `Owner`, `Due Date` |
| "feature comparison" | `Feature`, then one column per option |

**Never add** `Difficulty`, `Time Limit`, `Avg Score`, `Candidate Benchmarks`,
`Scoring Rubric`, `Pass Rate`, `FAANG Frequency`, `Std Deviation`, `Prep Hours`,
or any analytics/scoring column unless the user explicitly asked for it.

When in doubt, **fewer columns is better**. The user can add columns themselves.
A one-column-too-many workbook is harder to fix than a one-column-too-few one.

### Never fabricate data

**If the user did not provide numbers, there are no numbers.** Do not invent:
- Scores, pass rates, difficulty ratings, or benchmarks
- Frequency percentages, industry averages, or YoY changes
- Candidate performance data, prep hours, or practice problem counts
- Any metric that sounds plausible but was not given by the user

A Type A workbook contains only content the user asked for — questions, steps,
topics, descriptions. If you find yourself writing a number the user did not
give you, stop and remove that column entirely. Fabricated data presented as
fact is worse than no data at all.

### If the user provided no data, it is always Type A

The clearest signal for Type B is that the user **supplies actual numbers** in
their request ("here is our settlement data", "transactions: Bank A 1204 cycles").
If the user gives you only a topic or a count ("5 Java interview questions"),
there is no data — use Type A unconditionally.

---

A spreadsheet is not a report. Its job is to let someone **check your arithmetic**.
Two rules dominate everything else below (they apply to Type B; for Type A, Rule 1
still applies to any numeric cells, but most cells will be text).

## Rule 1 — Numbers must be numbers

Write numeric cells as Python `int`/`float`, never as strings. A number stored as
text will not sum, sorts wrongly, and raises "Number stored as text" in Excel.
Apply presentation through `number_format`, never by pre-formatting into a string.

Wrong: `ws["B2"] = "8,431"` · Right: `ws["B2"] = 8431` with
`ws["B2"].number_format = '#,##0'`

Number formats to use:

| Purpose | `number_format` |
|---|---|
| Whole count | `'#,##0'` |
| Rupee crore, negatives in parens | `'#,##0;(#,##0)'` |
| Rupee with unit | `'₹#,##0'` |
| Two decimals | `'#,##0.00'` |
| Percentage, one decimal | `'0.0%'` — store `0.987`, not `98.7` |
| Date | `'dd mmm yyyy'` |
| Indian grouping (lakh/crore) | `'[>=10000000]#\\,##\\,##\\,##0;[>=100000]#\\,##\\,##0;#\\,##0'` |

Put the unit in the column header (`Value (₹ Cr)`), not in each cell.

## Rule 2 — Compute with formulas, not in Python

If a cell is derived, it must contain a formula so the reader can trace it and the
workbook stays live when inputs change. Computing the answer in Python and writing
a constant destroys the audit trail — which is the entire point of sending a
spreadsheet rather than a PDF.

Wrong: `ws["D2"] = sum(values)` · Right: `ws["D2"] = "=SUM(B2:C2)"`

Guard every division: `=IFERROR(B2/C2, "")` — never leave a bare `/` that can hit a
zero. Prefer `SUMIFS`/`COUNTIFS` over hardcoded ranges that break on insert.

**Bound every range to the real data rows.** An open-ended range like
`=SUM(Data!B2:B100)` over a sheet that ends in a totals row silently counts that
row too, doubling the answer. Compute the last data row and emit
`=SUM(Data!B2:B6)`, or put the total outside the summed block. This is the most
common defect in generated workbooks, and the recalculation check below **will not
catch it** — a wrong number is not an error value. Only the self-check catches it,
so verify one total by hand against the source rows before you finish.

## Structure

- **One purpose per sheet.** `Summary` first, then data sheets, then any working
  sheet. The reader must land on the answer.
- **Row 1 is the header row.** Freeze it (`ws.freeze_panes = "A2"`) and turn on the
  autofilter over the used range. Never leave blank rows above the header —
  it breaks filtering, sorting and pivots.
- **One table per sheet**, starting at `A1`. Side-by-side tables break filters.
- **No merged cells in a data region.** Merging is permitted only in a title bar
  above a frozen header, and even then prefer not to.
- Set explicit column widths. Turn off gridlines on presentation sheets
  (`ws.sheet_view.showGridLines = False`), leave them on for working sheets.

## Appearance

Header row filled `navy` with 10pt bold `paper`, vertically centred, wrapped. Body
rows 10pt `ink`, alternating `paper` / `row-fill`. Horizontal hairlines in `rule`
only — no vertical borders, no outer box. Numerals right-aligned, text
left-aligned, headers matching their column.

**Status colour**: carry status with a fill plus the word, never colour alone. Cell
*text* at 10pt cannot use `00A551` — it measures 3.2:1 and fails legibility. Where
green text is genuinely required, use the darkened `00703C` (6.2:1). Reserve
`00A551` for fills and chart series.

## Cell-role convention (model workbooks only)

For a financial *model* — something with drivers a reader will change — mark cell
roles by font colour, which is long-standing industry practice:

| Role | Font colour |
|---|---|
| Hardcoded input a reader may change | `1F3864` navy |
| Formula computed on this sheet | `222222` ink |
| Link to another sheet in this workbook | `00703C` |
| Flag / assumption needing review | fill `E8EDF6` with a note |

State the convention in a legend on the `Summary` sheet. For a plain data or report
workbook, skip this and use the brand palette straight.

## Charts

Native charts only, never a pasted image. Build series from `Reference` objects so
the chart follows the data if it changes. No 3-D, no shadow.

Three defaults you must override or the chart ships wrong:

- **Colour.** openpyxl paints every series stock Office blue (`4472C4`). Set
  `series.graphicalProperties.solidFill` (and `.line.solidFill`) to `navy`, then
  `green`, `ink-muted`, `amber`.
- **Legend.** A legend naming a single series just repeats the title — set
  `chart.legend = None` when there is only one.
- **Anchor.** A chart placed to the right of a summary block straddles the
  print-page boundary and is sliced in half on export to PDF. Anchor it *below* the
  block, and check the rendered page rather than trusting the sheet view.

## Verification — mandatory before you finish

openpyxl **does not evaluate formulas**; it stores them. A workbook can therefore
contain a broken formula and still save without error. You must recalculate and
check. LibreOffice is installed in the sandbox and does this without network:

```python
import subprocess, glob
from openpyxl import load_workbook

ERRORS = ("#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A", "#NULL!", "#NUM!")
recalculated = []
try:
    subprocess.run(["soffice", "--headless",
                    "-env:UserInstallation=file:///tmp/lo_recalc",
                    "--convert-to", "xlsx", "--outdir", "/work/recalc",
                    "/work/output.xlsx"],
                   check=False, timeout=180,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    recalculated = glob.glob("/work/recalc/*.xlsx")
except FileNotFoundError:
    print("FORMULA CHECK: DID NOT RUN — soffice not found; formulas UNVERIFIED")
except (OSError, subprocess.TimeoutExpired) as exc:
    print(f"FORMULA CHECK: DID NOT RUN — {exc}; formulas UNVERIFIED")

if recalculated:
    found = []
    for path in recalculated:
        chk = load_workbook(path, data_only=True)
        for sheet in chk.worksheets:
            for row in sheet.iter_rows():
                for c in row:
                    if isinstance(c.value, str) and c.value in ERRORS:
                        found.append(f"{sheet.title}!{c.coordinate} = {c.value}")
    print("FORMULA ERRORS:", found if found else "none")
else:
    print("FORMULA CHECK: DID NOT RUN — no recalculated workbook; formulas UNVERIFIED")
```

Two traps this guards against, both of which produce a **false pass**:

- `check=False` does **not** cover a missing binary — `Popen` raises
  `FileNotFoundError` before `check` is ever consulted, so an unguarded call crashes
  the script after the workbook has already been saved.
- If the conversion silently produces nothing, an unguarded glob loop finds no files,
  reports `none`, and looks like a clean run. Never print `none` unless a
  recalculated workbook actually existed.

If errors are reported, fix the formula and rebuild — do not ship a workbook with a
live error. Leave this check in your script; its output is captured in the build log.

## Self-check before you call build_document

**For Type A (reference/list) workbooks:**

1. Did I use the Type A skeleton — one sheet, text columns, no Summary, no chart?
2. Does every column exist because the user's prompt implies it — or did I add
   columns the user never asked for?
3. Did I fabricate any number, score, rating, or metric the user did not provide?
   If yes, remove that column entirely.
4. Is every cell value either factual content (a real question, step, or topic)
   or left blank — never a made-up placeholder value?
5. Header row frozen, autofilter set, no blank rows above it?
6. Explicit column widths — wide (40–90) for text, narrow (6–16) for index?
7. Header `navy` + `paper` bold; alternating rows; no vertical borders?
8. Placeholder text — `REPLACE`, `TODO`, `Sample` — all gone?

**For Type B (data/analytics) workbooks:**

1. Every numeric cell a real number, not a string?
2. Every derived cell a formula, not a Python-computed constant?
3. Every division wrapped in `IFERROR`?
4. Percentages stored as fractions with a `0.0%` format?
5. Header row frozen, autofilter set, no blank rows above it?
6. No merged cells inside a data region?
7. Explicit column widths, so nothing shows as `####`?
8. Header `navy` + `paper` bold; alternating rows; no vertical borders?
9. Any 10pt green text? → use `00703C`, or move the colour to a fill.
10. Recalculation check present and reporting `none`?
11. Placeholder text — `REPLACE`, `TODO`, `Sample` — all gone?

## Output contract

Write **one** self-contained Python script that starts `from ainxt_sheet import Book`.
That module is preinstalled in the sandbox and already enforces the palette, number
formats, frozen headers, right-aligned numerics, brand chart colours and
data-bounded summary formulas. Express CONTENT only. Finish with `b.save()`, which
writes `/work/output.xlsx` and runs the recalculation check for you. No
network calls.
