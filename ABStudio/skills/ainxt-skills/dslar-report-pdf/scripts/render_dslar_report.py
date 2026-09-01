#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Render the single DSLAR validation-report PDF from enriched.json.

This is a DETERMINISTIC renderer (no LLM). It reads the final workflow state
that the decision-maker nodes wrote into ``enriched.json`` and produces ONE
PDF that reproduces the canonical "Validation Report" layout:

    Validation Report
      Verdict / Validation Type / Report Detail / Job ID / Created At
    Metadata Checks            (dlsar mode)
    Report Metadata Checks     (report mode)
    Clause Validation (13 clauses)
      #N Name -- present/not concluded, Satisfactory, Evidence
      Clause 1 -- Data elements (AiNxt checklist)   (68 Sr. rows)
    Points Not Concluded

The script writes the PDF into ``OUTPUT_DIR`` (the directory the platform's
code_executor auto-collects into GENERATED_FILES_DIR and serves back as a
``/generated-files/<name>`` download link). It does NOT construct URLs itself;
the platform does that from the collected file.

After a successful render it deletes ``chunk_*.json`` scratch files from the
artifact dir but leaves ``enriched.json`` intact (audit trail / re-render).

CLI:
    python render_dslar_report.py \
        --enriched-json <path/to/enriched.json> \
        --output-dir <OUTPUT_DIR> \
        [--artifact-dir <WORKFLOW_ARTIFACT_DIR>] \
        [--source-name <original_pdf_name>] \
        [--job-id <id>] [--created-at <str>] \
        [--keep-chunks]

Prints a compact JSON summary to stdout:
    {"pdf_filename": ..., "pdf_path": ..., "chunks_deleted": N, "verdict": ...}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Table,
    TableStyle,
)

# ---------------------------------------------------------------------------
# Palette + status colour mapping (kept in one place so the whole report uses
# a consistent, readable colour scheme).
# ---------------------------------------------------------------------------

_BRAND = colors.HexColor("#1F3B73")        # headings / table header fills
_BRAND_LIGHT = colors.HexColor("#E8EDF6")  # zebra / label-cell background
_RULE = colors.HexColor("#C9D2E3")         # grid lines
_TEXT = colors.HexColor("#1A1A1A")

_STATUS_COLORS = {
    "passed": colors.HexColor("#1B7F3B"),
    "present": colors.HexColor("#1B7F3B"),
    "yes": colors.HexColor("#1B7F3B"),
    "failed": colors.HexColor("#B00020"),
    "not present": colors.HexColor("#B00020"),
    "no": colors.HexColor("#B00020"),
    "inconclusive": colors.HexColor("#B8860B"),
    "not concluded": colors.HexColor("#B8860B"),
    "n/a": colors.HexColor("#6B6B6B"),
}


def _status_color(label: Any) -> colors.Color:
    return _STATUS_COLORS.get(str(label).strip().lower(), _TEXT)

# ---------------------------------------------------------------------------
# Value normalisation helpers
# ---------------------------------------------------------------------------

# Canonical 13 clause names so a missing clause still renders a labelled row.
_CLAUSE_NAMES = {
    "1": "Payments Data Elements",
    "2": "Transaction/Data Flow",
    "3": "Application Architecture",
    "4": "Network Diagram/Architecture",
    "5": "Data Storage",
    "6": "Transaction Processing",
    "7": "Activities Related to Payment Processing",
    "8": "Cross Border Transactions",
    "9": "Database Storage and Maintenance",
    "10": "Data Backup & Restoration",
    "11": "Data Security",
    "12": "Access Management",
    "13": "Data Sharing",
}


def _xml_escape(text: Any) -> str:
    s = "" if text is None else str(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _passed_label(check: Any) -> str:
    """Map a metadata check object to Passed / Failed / Inconclusive."""
    if not isinstance(check, dict):
        # Some pipelines store a bare bool.
        if check is True:
            return "Passed"
        if check is False:
            return "Failed"
        return "Inconclusive"
    if check.get("inconclusive"):
        return "Inconclusive"
    passed = check.get("passed")
    if passed is True:
        return "Passed"
    if passed is False:
        return "Failed"
    return "Inconclusive"


def _present_label(clause: dict) -> str:
    """present=True -> 'present'; inconclusive -> 'not concluded'; else 'not present'."""
    if clause.get("inconclusive"):
        return "not concluded"
    present = clause.get("present")
    if present is True:
        return "present"
    if present is False:
        return "not present"
    return "not concluded"


def _yes_no(value: Any) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "N/A"


# Typographic / non-Latin glyphs that the built-in Helvetica font cannot draw
# (they would show as black boxes). Map them to safe ASCII equivalents so the
# evidence text stays readable.
_GLYPH_FIXES = {
    "\u2022": "-",   # bullet •
    "\u25aa": "-",   # black small square ▪
    "\u25cf": "-",   # black circle ●
    "\u2013": "-",   # en dash –
    "\u2014": "-",   # em dash —
    "\u2018": "'",   # left single quote ‘
    "\u2019": "'",   # right single quote ’
    "\u201c": '"',   # left double quote “
    "\u201d": '"',   # right double quote ”
    "\u00a0": " ",   # non-breaking space
    "\ufffd": "'",   # replacement char
}

_GLYPH_RE = re.compile("|".join(re.escape(k) for k in _GLYPH_FIXES))


def _clean_text(text: Any) -> str:
    """Strip PDF-extraction artifacts and normalise whitespace so evidence and
    other free text reads cleanly. Removes ``(cid:NNN)`` glyph codes, maps
    typographic glyphs the base font cannot render to ASCII, and collapses runs
    of whitespace / dotted-leader runs."""
    s = "" if text is None else str(text)
    s = re.sub(r"\(cid:\d+\)", " ", s)                       # unmapped glyphs
    s = _GLYPH_RE.sub(lambda m: _GLYPH_FIXES[m.group(0)], s)  # typographic glyphs
    s = re.sub(r"\.{4,}", " ... ", s)                        # dotted leaders
    s = re.sub(r"\s+", " ", s)                                # normalise spaces
    return s.strip()


def _truncate(text: str, limit: int = 320) -> str:
    """Trim overly long blobs (raw evidence) to a readable length with an
    ellipsis so a single field never floods the page."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip(" ,;.") + " ..."


def _evidence_items(refs: Any) -> "list[str]":
    """Return a list of cleaned individual evidence snippets."""
    if not refs:
        return []
    if isinstance(refs, str):
        parts = [refs]
    elif isinstance(refs, (list, tuple)):
        parts = [str(r) for r in refs if r is not None]
    else:
        parts = [str(refs)]
    cleaned = []
    for p in parts:
        # Some pipelines pack many "; "-joined snippets into one string.
        for sub in str(p).split("; "):
            c = _clean_text(sub)
            if c:
                cleaned.append(c)
    return cleaned


def _evidence_join(refs: Any) -> str:
    return "; ".join(_evidence_items(refs))


def _slug(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "report").strip("_")
    return base or "report"


def _resolve_source_name(cli_source: str, payload: dict) -> str:
    """Resolve the original input PDF filename from every shape the pipeline
    might store. Tries, in order:
      --source-name CLI arg, top-level source_name, extracted.source_name,
      extracted.ingested.source_name, ingested_doc.source_name, and finally
      the basename of any source_path. Falls back to "report"."""
    extracted = payload.get("extracted") or {}
    ingested = (extracted.get("ingested") or {}) if isinstance(extracted, dict) else {}
    ingested_doc = payload.get("ingested_doc") or {}

    candidates = [
        cli_source,
        payload.get("source_name"),
        extracted.get("source_name") if isinstance(extracted, dict) else None,
        ingested.get("source_name") if isinstance(ingested, dict) else None,
        ingested_doc.get("source_name") if isinstance(ingested_doc, dict) else None,
    ]
    for cand in candidates:
        if cand and str(cand).strip():
            return str(cand).strip()

    # Last resort: derive from a stored source_path (full path).
    for sp in (
        ingested.get("source_path") if isinstance(ingested, dict) else None,
        ingested_doc.get("source_path") if isinstance(ingested_doc, dict) else None,
        payload.get("source_path"),
    ):
        if sp and str(sp).strip():
            name = Path(str(sp)).name
            # Ignore the materialized "input.pdf" placeholder.
            if name and name.lower() != "input.pdf":
                return name

    return "report"


# ---------------------------------------------------------------------------
# Schema normalisation
#
# The running pipeline stores results in more than one shape:
#   * the canonical final state may live at the TOP level OR inside a
#     ``final_report`` sub-dict (decision-maker output);
#   * ``metadata_checks`` may be a DICT keyed by check name
#     ({"auditor_name": {"passed": bool, "inconclusive": bool}, ...}) OR a
#     LIST of {"check", "value", "passed", "inconclusive"} objects;
#   * ``clause_results`` may be a LIST of clause objects OR a DICT keyed by
#     clause id.
# These helpers fold every variant into the dict/list shapes build_pdf wants.
# ---------------------------------------------------------------------------

# Canonical metadata-check ordering + display labels. ``issue_date_validity``
# and ``issue_date_valid`` are treated as the same check.
_META_LABELS = [
    ("auditor_name", "Auditor Name"),
    ("company_name", "Company Name"),
    ("product_name", "Product Name"),
    ("issue_date_valid", "Issue Date Valid"),
    ("issue_date_validity", "Issue Date Valid"),
]


def _has_data(value: Any) -> bool:
    """True if a clause_results / metadata_checks value carries real content."""
    if isinstance(value, list):
        return len(value) > 0
    if isinstance(value, dict):
        return len(value) > 0
    return False


def _resolve_state(payload: dict) -> dict:
    """Merge top-level and ``final_report`` layouts, preferring whichever copy
    actually has content for each key. The decision-maker writes the rich copy
    into ``final_report``; older/partial runs keep it at the top level."""
    fr = payload.get("final_report")
    fr = fr if isinstance(fr, dict) else {}
    merged = dict(payload)
    for key in (
        "verdict", "validation_type", "report_detail", "executive_summary",
        "summary", "metadata_checks", "report_metadata_checks",
        "clause_results", "points_not_concluded", "job_id", "created_at",
        "source_name", "fail_reasons",
    ):
        top_val = payload.get(key)
        fr_val = fr.get(key)
        # Prefer the copy that carries data; fall back to whichever is set.
        if _has_data(fr_val) and not _has_data(top_val):
            merged[key] = fr_val
        elif _has_data(top_val):
            merged[key] = top_val
        elif fr_val not in (None, "", [], {}):
            merged[key] = fr_val
        elif top_val not in (None, "", [], {}):
            merged[key] = top_val
    return merged


def _normalize_metadata_checks(meta: Any) -> "list[tuple[str, Any]]":
    """Return an ordered list of (display_label, check_value) regardless of
    whether ``meta`` is a dict keyed by check name or a list of check objects."""
    if not meta:
        return []
    items: dict[str, Any] = {}
    if isinstance(meta, list):
        for entry in meta:
            if isinstance(entry, dict):
                key = (entry.get("check") or entry.get("name") or "").strip()
                if key:
                    items[key] = entry
    elif isinstance(meta, dict):
        items = dict(meta)
    else:
        return []

    out: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for key, label in _META_LABELS:
        if key in items and key not in seen:
            # Collapse the two issue-date aliases to a single labelled row.
            if label == "Issue Date Valid" and any(
                l == "Issue Date Valid" for _, l in
                [(k, lb) for k, lb in _META_LABELS if k in seen]
            ):
                seen.add(key)
                continue
            out.append((label, items[key]))
            seen.add(key)
    for key, val in items.items():
        if key in seen:
            continue
        out.append((key.replace("_", " ").title(), val))
        seen.add(key)
    return out


def _normalize_clause_list(clauses: Any) -> list:
    """Return a list of clause dicts whether the source is a list or a dict
    keyed by clause id. Dict form is sorted numerically by clause id."""
    if isinstance(clauses, list):
        return [c for c in clauses if isinstance(c, dict)]
    if isinstance(clauses, dict):
        def _key(item):
            cid = item[0]
            try:
                return (0, int(str(cid)))
            except (TypeError, ValueError):
                return (1, str(cid))
        result = []
        for cid, c in sorted(clauses.items(), key=_key):
            if isinstance(c, dict):
                c = dict(c)
                c.setdefault("clause_id", cid)
                result.append(c)
        return result
    return []


# ---------------------------------------------------------------------------
# PDF build
# ---------------------------------------------------------------------------

def _styles():
    ss = getSampleStyleSheet()
    title = ParagraphStyle(
        "DSLARTitle", parent=ss["Title"], fontSize=22, spaceAfter=2,
        alignment=TA_LEFT, textColor=_BRAND,
    )
    subtitle = ParagraphStyle(
        "DSLARSubtitle", parent=ss["BodyText"], fontSize=9, leading=12,
        textColor=colors.HexColor("#5A6377"), spaceAfter=2,
    )
    h2 = ParagraphStyle(
        "DSLARH2", parent=ss["Heading2"], fontSize=14, spaceBefore=14, spaceAfter=6,
        textColor=_BRAND,
    )
    h3 = ParagraphStyle(
        "DSLARH3", parent=ss["Heading4"], fontSize=10.5, spaceBefore=8, spaceAfter=2,
        textColor=_TEXT,
    )
    body = ParagraphStyle(
        "DSLARBody", parent=ss["BodyText"], fontSize=9, leading=12, spaceAfter=1,
        textColor=_TEXT,
    )
    cell = ParagraphStyle(
        "DSLARCell", parent=ss["BodyText"], fontSize=8, leading=10, textColor=_TEXT,
    )
    cell_head = ParagraphStyle(
        "DSLARCellHead", parent=cell, fontSize=8, leading=10,
        textColor=colors.white, fontName="Helvetica-Bold",
    )
    label = ParagraphStyle(
        "DSLARLabel", parent=cell, fontName="Helvetica-Bold",
    )
    evidence = ParagraphStyle(
        "DSLAREvidence", parent=ss["BodyText"], fontSize=8, leading=11,
        leftIndent=8, textColor=colors.HexColor("#444444"), spaceAfter=1,
    )
    return {
        "title": title, "subtitle": subtitle, "h2": h2, "h3": h3,
        "body": body, "cell": cell, "cell_head": cell_head,
        "label": label, "evidence": evidence,
    }


# Usable content width inside the A4 page margins set on the doc template.
_CONTENT_WIDTH = A4[0] - (18 * mm) * 2


def _kv_table(st, rows: "list[tuple[str, str]]", *, label_w: float = 42 * mm):
    """Two-column key/value table with a tinted label column."""
    data = [
        [Paragraph(_xml_escape(k), st["label"]), Paragraph(_xml_escape(v), st["cell"])]
        for k, v in rows
    ]
    tbl = Table(data, colWidths=[label_w, _CONTENT_WIDTH - label_w], hAlign="LEFT")
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), _BRAND_LIGHT),
        ("GRID", (0, 0), (-1, -1), 0.5, _RULE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return tbl


def _status_table(st, rows: "list[tuple[str, str]]", *, label_w: float = 60 * mm):
    """Two-column table whose value cell is colour-coded by status word."""
    data = []
    styles = []
    for i, (k, v) in enumerate(rows):
        vstyle = ParagraphStyle(
            f"st{i}", parent=st["cell"], fontName="Helvetica-Bold",
            textColor=_status_color(v),
        )
        data.append([Paragraph(_xml_escape(k), st["label"]),
                     Paragraph(_xml_escape(v), vstyle)])
    tbl = Table(data, colWidths=[label_w, _CONTENT_WIDTH - label_w], hAlign="LEFT")
    base = [
        ("BACKGROUND", (0, 0), (0, -1), _BRAND_LIGHT),
        ("GRID", (0, 0), (-1, -1), 0.5, _RULE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    tbl.setStyle(TableStyle(base + styles))
    return tbl


def _data_elements_table(st, rows: list) -> Table:
    """Render the AiNxt data-element checklist as a wrapped, zebra-striped table.

    Columns: Sr. | Scope | Category | Data Element | Status | Rest/Proc |
    Jurisdiction | Brought Back. Each cell is a Paragraph so long values wrap
    instead of overflowing the page, and the status column is colour-coded."""
    header = ["Sr.", "Scope", "Category", "Data Element",
              "Status", "Rest/Proc", "Jurisdiction", "Brought Back"]
    data = [[Paragraph(_xml_escape(h), st["cell_head"]) for h in header]]

    r_idx = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        r_idx += 1
        status = "present" if r.get("present") else (
            "not concluded" if r.get("inconclusive") else "not present")
        sstyle = ParagraphStyle(
            f"des{r_idx}", parent=st["cell"], fontName="Helvetica-Bold",
            textColor=_status_color(status),
        )
        values = [
            str(r.get("serial", "")),
            str(r.get("scope", "") or "-"),
            str(r.get("category", "") or "-"),
            str(r.get("label") or "-"),
            None,  # status handled with its own style below
            str(r.get("rest_or_processing") or "-"),
            str(r.get("jurisdiction") or "-"),
            str(r.get("brought_back_status") or "-"),
        ]
        cells = []
        for ci, v in enumerate(values):
            if ci == 4:
                cells.append(Paragraph(_xml_escape(status), sstyle))
            else:
                cells.append(Paragraph(_xml_escape(v), st["cell"]))
        data.append(cells)

    # Column widths (mm) sized to the content area; Data Element gets the most.
    col_mm = [9, 20, 26, 34, 20, 18, 20, 19]
    scale = _CONTENT_WIDTH / (sum(col_mm) * mm)
    col_widths = [w * mm * scale for w in col_mm]

    tbl = Table(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), _BRAND),
        ("GRID", (0, 0), (-1, -1), 0.4, _RULE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    # Zebra striping on body rows for readability.
    for ri in range(1, len(data)):
        if ri % 2 == 0:
            style.append(("BACKGROUND", (0, ri), (-1, ri), _BRAND_LIGHT))
    tbl.setStyle(TableStyle(style))
    return tbl


def build_pdf(payload: dict, out_path: Path, *, job_id: str | None,
              created_at: str | None) -> None:
    st = _styles()
    story: list = []

    # Fold top-level / final_report layouts into one state dict.
    payload = _resolve_state(payload)

    verdict = payload.get("verdict") or payload.get("final_verdict") or "INCONCLUSIVE"
    vtype = payload.get("validation_type") or "dlsar"
    detail = payload.get("report_detail") or "Complete"
    summary = payload.get("executive_summary") or payload.get("summary") or ""

    # ---- Header ----
    story.append(Paragraph("Validation Report", st["title"]))
    vstyle = ParagraphStyle(
        "Verdict", parent=st["subtitle"], fontSize=11,
        fontName="Helvetica-Bold", textColor=_status_color(verdict),
    )
    story.append(Paragraph(f"Verdict: {_xml_escape(verdict)}", vstyle))
    story.append(HRFlowable(width="100%", thickness=1.2, color=_BRAND,
                            spaceBefore=4, spaceAfter=8))

    header_rows = [
        ("Validation Type", str(vtype)),
        ("Report Detail", str(detail)),
    ]
    if job_id:
        header_rows.append(("Job ID", str(job_id)))
    if created_at:
        header_rows.append(("Created At", str(created_at)))
    story.append(_kv_table(st, header_rows))

    # ---- Metadata checks (accepts dict-keyed or list-of-objects form) ----
    meta_rows = _normalize_metadata_checks(payload.get("metadata_checks"))
    if meta_rows:
        story.append(Paragraph("Metadata Checks", st["h2"]))
        story.append(_status_table(
            st, [(label, _passed_label(val)) for label, val in meta_rows]))

    # ---- Report-mode checks ----
    rmeta_rows = _normalize_metadata_checks(payload.get("report_metadata_checks"))
    if rmeta_rows:
        story.append(Paragraph("Report Metadata Checks", st["h2"]))
        story.append(_status_table(
            st, [(label, _passed_label(val)) for label, val in rmeta_rows]))

    # ---- Clause validation (accepts list or dict-keyed form) ----
    clauses = _normalize_clause_list(payload.get("clause_results"))
    if clauses:
        story.append(Paragraph(f"Clause Validation ({len(clauses)} clauses)", st["h2"]))
        for clause in clauses:
            if not isinstance(clause, dict):
                continue
            cid = str(clause.get("clause_id", "")).strip()
            cname = clause.get("clause_name") or _CLAUSE_NAMES.get(cid, "")
            present = _present_label(clause)

            block: list = []
            # Clause heading with colour-coded status badge.
            head = (
                f'<font color="#{_BRAND.hexval()[2:]}"><b>#{_xml_escape(cid)} '
                f'{_xml_escape(cname)}</b></font> - '
                f'<font color="#{_status_color(present).hexval()[2:]}">'
                f'<b>{_xml_escape(present)}</b></font>'
            )
            block.append(Paragraph(head, st["h3"]))
            block.append(Paragraph(
                f"Satisfactory: <b>{_yes_no(clause.get('satisfactory'))}</b>",
                st["body"]))
            for ev in _evidence_items(clause.get("evidence_refs")):
                block.append(Paragraph(
                    f"<b>Evidence:</b> {_xml_escape(_truncate(ev))}", st["evidence"]))
            # Keep a clause heading with its first lines together where possible.
            story.append(KeepTogether(block))

            # Clause 1 data-element detail (AiNxt checklist) rendered as a table.
            if cid == "1":
                rows = clause.get("data_element_results") or []
                if rows:
                    story.append(Paragraph(
                        "Clause 1 - Data Elements (AiNxt checklist)", st["h3"]))
                    story.append(_data_elements_table(st, rows))

    # ---- Executive summary (optional) ----
    if summary:
        story.append(Paragraph("Executive Summary", st["h2"]))
        story.append(Paragraph(_clean_text(summary), st["body"]))

    # ---- Points not concluded ----
    points = payload.get("points_not_concluded") or []
    if points:
        story.append(Paragraph("Points Not Concluded", st["h2"]))
        for i, p in enumerate(points, start=1):
            story.append(Paragraph(
                f"{i}.&nbsp;&nbsp;{_xml_escape(_clean_text(p))}", st["body"]))

    def _decorate(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#8A93A6"))
        canvas.drawString(18 * mm, 10 * mm, "AiNxt DL-SAR Validation Report")
        canvas.drawRightString(
            A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title="Validation Report",
    )
    doc.build(story, onFirstPage=_decorate, onLaterPages=_decorate)


# ---------------------------------------------------------------------------
# Chunk cleanup
# ---------------------------------------------------------------------------

def delete_chunks(artifact_dir: Path) -> int:
    deleted = 0
    if not artifact_dir or not artifact_dir.is_dir():
        return 0
    for f in artifact_dir.glob("chunk_*.json"):
        try:
            f.unlink()
            deleted += 1
        except OSError:
            pass
    return deleted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render DSLAR validation report PDF.")
    ap.add_argument("--enriched-json", required=True)
    ap.add_argument("--output-dir", default=os.environ.get("OUTPUT_DIR", ""))
    ap.add_argument("--artifact-dir", default=os.environ.get("WORKFLOW_ARTIFACT_DIR", ""))
    ap.add_argument("--source-name", default="")
    ap.add_argument("--job-id", default="")
    ap.add_argument("--created-at", default="")
    ap.add_argument("--keep-chunks", action="store_true")
    args = ap.parse_args(argv)

    enriched_path = Path(args.enriched_json)
    if not enriched_path.is_file():
        print(json.dumps({"error": f"enriched.json not found: {enriched_path}"}))
        return 2

    payload = json.loads(enriched_path.read_text(encoding="utf-8"))

    out_dir = Path(args.output_dir) if args.output_dir else enriched_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    source = _resolve_source_name(args.source_name, payload)
    # Preserve the original filename verbatim (spaces/case kept). Drop a
    # trailing ".pdf"/".PDF" so the single ".pdf" suffix is not doubled, and
    # strip path separators / control chars that would break the filename.
    stem = re.sub(r"\.pdf$", "", source, flags=re.IGNORECASE)
    stem = re.sub(r"[\\/\x00-\x1f]+", "_", stem).strip() or "report"
    pdf_name = f"validation-report-complete-{stem}.pdf"
    pdf_path = out_dir / pdf_name

    job_id = args.job_id or payload.get("job_id") or None
    created_at = args.created_at or payload.get("created_at") or None

    build_pdf(payload, pdf_path, job_id=job_id, created_at=created_at)

    artifact_dir = Path(args.artifact_dir) if args.artifact_dir else enriched_path.parent
    chunks_deleted = 0 if args.keep_chunks else delete_chunks(artifact_dir)

    print(json.dumps({
        "pdf_filename": pdf_name,
        "pdf_path": str(pdf_path),
        "chunks_deleted": chunks_deleted,
        "verdict": payload.get("verdict") or payload.get("final_verdict"),
        "validation_type": payload.get("validation_type"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
