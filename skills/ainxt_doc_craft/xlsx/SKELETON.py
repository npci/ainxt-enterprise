#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Workbook skeleton — two skeleton shapes. Read SKILL.md to pick the right one.

TYPE A (this active block) — Reference / List workbook
  Use when the user wants a table of items to READ: questions, steps, topics,
  checklists, comparisons, guides. No fabricated data — only content the user
  provided or that is factually known. Writes /work/output.xlsx.

TYPE B (commented block below) — Data / Analytics workbook
  Use when the user provides REAL numbers to track: settlement, transactions,
  revenue, performance metrics. Uncomment that block and delete this one.
"""

# ── TYPE A — Reference / List workbook (DEFAULT) ──────────────────────────────
# Use for: interview questions, checklists, comparisons, guides, FAQs, plans.
# Rules:
#   • Only include columns the user's prompt implies — do NOT add Answer/Notes
#     unless the user asked for them. Do NOT add Difficulty/Score/Benchmark
#     unless the user asked for them.
#   • Never fabricate data. Every cell value must come from the user's request
#     or be factually known. No invented scores, frequencies, or benchmarks.
#   • No Summary sheet, no formulas, no chart unless explicitly requested.

from ainxt_sheet import Book

b = Book(title="REPLACE — e.g. Java Interview Questions",
         classification="Confidential")

sheet = b.sheet(
    "REPLACE — sheet name, e.g. Questions",
    ["#", "Question"],              # REPLACE — add columns only if the prompt implies them
                                    # e.g. add "Answer" only if user asked for answers
    widths=[6, 90],                 # narrow index, wide text; adjust per content
    right_cols=[],                  # no numeric columns in a reference sheet
)
sheet.rows([
    [1, "REPLACE — first question or item"],
    [2, "REPLACE — second question or item"],
    [3, "REPLACE — third question or item"],
    # … one row per item; add as many rows as the content requires
])

# No total_row(), no b.summary(), no b.chart() for a reference workbook.
# Add a second sheet only if the content naturally splits into distinct groups.

b.save()


# ── TYPE B — Data / Analytics workbook (use only when user provides real data) ─
# Use for: settlement data, transaction reports, performance dashboards, trackers.
# ONLY use this shape when the user has provided actual numbers to work with.
# Never invent metrics, scores, or benchmarks to fill this shape.
#
# Instructions:
#   1. Delete the Type A block above entirely.
#   2. Uncomment this block.
#   3. Replace REPLACE placeholders with real content and real data.
#
# from ainxt_sheet import Book
#
# b = Book(title="REPLACE — Workbook title", classification="Confidential")
#
# data = b.sheet(
#     "Data",
#     ["REPLACE — Bank", "Cycles", "Value (₹ Cr)", "Status"],
#     widths=[30, 14, 18, 16],
#     right_cols=[1, 2],                       # 0-based numeric columns
#     formats={1: Book.COUNT, 2: Book.CRORE},
# )
# data.rows([                                  # REPLACE with real rows — never fabricate
#     ["Bank A", 1204, 8431, "Settled"],
#     ["Bank B", 987,  6220, "Settled"],
#     ["Bank C", 431,  2109, "Pending"],
# ])
# data.total_row(["Total", "SUM", "SUM", ""])
#
# b.summary([
#     ("Total cycles",       data.sum_formula(1),               Book.COUNT),
#     ("Total value (₹ Cr)", data.sum_formula(2),               Book.CRORE),
#     ("Settled share",      data.share_formula(1, 3, "Settled"), Book.PCT),
# ])
#
# b.chart(data, value_col=2, title="REPLACE — chart title")
#
# b.save()
