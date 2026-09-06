# SPDX-License-Identifier: MIT
# ============================================================
# PDF OCR + TABLE HYBRID EXTRACTOR
#
# Single entry point: extract_pdf(path, filename) -> dict
#
# Behaviour per page (hybrid, lossless):
#   1. Extract born-digital text via core.pdf_backend page.get_text("text").
#   2. Detect born-digital tables via page.find_tables() and render
#      each as a GitHub-flavoured pipe table. Tables are emitted AT THE
#      POSITION they occur in the page (top-of-page above body text).
#   3. If the page has no born-digital text AND has at least one image
#      or covers more than _SCANNED_COVERAGE of the page area with image
#      pixels, the page is treated as SCANNED:
#        - rasterise at _OCR_DPI dpi
#        - run rapidocr-onnxruntime → list of (box, text, confidence)
#        - reconstruct rows by y-clustering of box centres
#        - reconstruct table columns inside a row when ≥ _TABLE_MIN_COLS
#          x-aligned bands appear across ≥ _TABLE_MIN_ROWS consecutive
#          rows. Otherwise emit as plain prose.
#   4. Per-page Markdown is concatenated under `## Page N` headings so
#      every citation downstream remains traceable to a page.
#
# This module is import-safe: rapidocr is loaded lazily on first OCR
# call and any import / runtime failure degrades the page to text-only
# with a warning. Born-digital PDFs incur ZERO OCR cost.
# ============================================================

from __future__ import annotations

import io
import logging
import os
import statistics
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

try:
    from core.logger import logger  # type: ignore
except Exception:  # pragma: no cover — module standalone import
    logger = logging.getLogger(__name__)


# ── Tuning knobs (kept module-local; no env coupling) ────────────────
_OCR_DPI = 220                  # 200-250 is the sweet spot for printed text
_SCANNED_COVERAGE = 0.6         # image-pixel fraction that flags a page as scanned
_TABLE_MIN_COLS = 2             # below this we treat columns as prose
_TABLE_MIN_ROWS = 2             # tables must span at least 2 consecutive rows
_ROW_GAP_MULT = 0.6             # row break when vertical gap > median_h * 1.6
_OCR_CONF_FLOOR = 0.30          # drop boxes below this confidence
_MAX_OCR_PAGES = 500            # safety cap; tweak if you ever exceed


# Singleton OCR engine — RapidOCR holds ONNX sessions and is expensive
# to instantiate. One per process is plenty; the worker pool can share it
# (rapidocr is internally thread-safe enough for our serial KB parses).
_OCR_SINGLETON: Optional[Any] = None


def _get_ocr_engine() -> Optional[Any]:
    global _OCR_SINGLETON
    if _OCR_SINGLETON is not None:
        return _OCR_SINGLETON
    try:
        from rapidocr_onnxruntime import RapidOCR
        _OCR_SINGLETON = RapidOCR()
        return _OCR_SINGLETON
    except Exception:  # pragma: no cover — env-dependent
        logger.warning(
            "pdf_ocr: rapidocr-onnxruntime is unavailable. Install "
            "`rapidocr-onnxruntime` and `onnxruntime` to enable OCR."
        )
        return None


# ── Box dataclass ────────────────────────────────────────────────────
@dataclass
class _OcrBox:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def h(self) -> float:
        return self.y1 - self.y0


# ── Public entry point ───────────────────────────────────────────────
def extract_pdf(path: str, filename: str = "") -> Dict[str, Any]:
    """
    Hybrid PDF extraction. Always returns a dict; never raises for
    "page is empty" cases — those become warnings.

    Return shape (stable):
      {
        "text": "<full markdown>",
        "pages": [
            {"page": 1, "text": "...", "tables": [[row, ...], ...],
             "source": "text-layer" | "ocr" | "hybrid" | "empty",
             "warnings": [...]},
            ...
        ],
        "source": "text-layer" | "ocr" | "hybrid" | "empty",
        "ocr_engine": "rapidocr" | "none",
        "warnings": [...],
      }
    """
    try:
        from core import pdf_backend as fitz
    except ImportError:
        return {
            "text": "",
            "pages": [],
            "source": "empty",
            "ocr_engine": "none",
            "warnings": ["core.pdf_backend (pdfium engine) is not installed"],
        }

    pages_out: List[Dict[str, Any]] = []
    warnings: List[str] = []
    sources_seen: set[str] = set()
    used_ocr = False

    doc = fitz.open(path)
    try:
        page_count = len(doc)
        if page_count > _MAX_OCR_PAGES:
            warnings.append(
                f"PDF has {page_count} pages; only first {type(_MAX_OCR_PAGES).__name__} will be OCR'd."
            )
        for idx in range(page_count):
            page = doc[idx]
            page_no = idx + 1
            try:
                page_md, page_tables, page_src, page_warns = _extract_page(
                    page, page_no, allow_ocr=(idx < _MAX_OCR_PAGES),
                )
            except Exception:  # pragma: no cover — defensive
                logger.error("pdf_ocr: page %d failed", page_no)
                page_md, page_tables, page_src, page_warns = (
                    "",
                    [],
                    "empty",
                    [f"page {page_no} extraction error: {type(exc).__name__}"],
                )
            if page_src == "ocr" or page_src == "hybrid":
                used_ocr = True
            sources_seen.add(page_src)
            warnings.extend(page_warns)
            pages_out.append({
                "page": page_no,
                "text": page_md,
                "tables": page_tables,
                "source": page_src,
                "warnings": page_warns,
            })
    finally:
        doc.close()

    # Compose full markdown with `## Page N` anchors
    full_parts: List[str] = []
    for p in pages_out:
        if not p["text"].strip():
            continue
        full_parts.append(f"## Page {p['page']}\n\n{p['text'].strip()}")
    full_md = "\n\n".join(full_parts)

    # Roll up the overall source label
    if not full_md.strip():
        overall = "empty"
    elif sources_seen == {"text-layer"}:
        overall = "text-layer"
    elif sources_seen == {"ocr"}:
        overall = "ocr"
    else:
        overall = "hybrid"

    return {
        "text": full_md,
        "pages": pages_out,
        "source": overall,
        "ocr_engine": "rapidocr" if used_ocr else "none",
        "warnings": warnings,
    }


# ── Single-page extraction ───────────────────────────────────────────
def _extract_page(
    page: Any, page_no: int, allow_ocr: bool = True,
) -> Tuple[str, List[List[List[str]]], str, List[str]]:
    """
    Returns: (page_markdown, tables_as_rows, source_label, warnings)
    """
    warnings: List[str] = []

    # 1) Born-digital text + tables via fitz
    text_layer = page.get_text("text") or ""
    has_text_layer = bool(text_layer.strip())

    fitz_tables_md: List[str] = []
    fitz_tables_rows: List[List[List[str]]] = []
    try:
        tf = page.find_tables()
        for t in (tf.tables or []):
            try:
                rows = t.extract()  # list[list[str|None]]
                if not rows:
                    continue
                # Clean None → "" and strip
                clean = [
                    [(c or "").strip().replace("\n", " ") for c in row]
                    for row in rows if row
                ]
                # Drop fully-empty rows
                clean = [r for r in clean if any(cell for cell in r)]
                if not clean:
                    continue
                fitz_tables_rows.append(clean)
                fitz_tables_md.append(_rows_to_md_table(clean))
            except Exception:  # pragma: no cover — pdf_backend table edge cases
                warnings.append(f"page {page_no}: table extract skipped ()")
    except Exception:  # pragma: no cover — pdf_backend without find_tables support
        warnings.append(f"page {page_no}: table finder unavailable ()")

    # 2) Decide whether to OCR this page
    needs_ocr = (not has_text_layer) and allow_ocr and _page_looks_scanned(page)

    ocr_md = ""
    ocr_tables_rows: List[List[List[str]]] = []
    used_ocr = False
    if needs_ocr:
        engine = _get_ocr_engine()
        if engine is None:
            warnings.append(
                f"page {type(page_no).__name__}: scanned page detected but OCR engine unavailable"
            )
        else:
            try:
                ocr_md, ocr_tables_rows, ocr_warns = _ocr_page_markdown(page, engine)
                warnings.extend([f"page {page_no}: {type(w).__name__}" for w in ocr_warns])
                used_ocr = bool(ocr_md.strip()) or bool(ocr_tables_rows)
            except Exception:  # pragma: no cover — defensive
                warnings.append(f"page {page_no}: ocr error ()")

    # 3) Compose the page markdown — tables FIRST so they're easy to find,
    #    then prose body. Both fitz tables and OCR tables are included
    #    (deduped by exact-row equality to avoid double-emission if both
    #    paths happen to fire on a hybrid page).
    parts: List[str] = []
    emitted_table_keys: set[Tuple] = set()
    all_tables_rows: List[List[List[str]]] = []
    for rows in fitz_tables_rows + ocr_tables_rows:
        key = tuple(tuple(r) for r in rows)
        if key in emitted_table_keys:
            continue
        emitted_table_keys.add(key)
        all_tables_rows.append(rows)
        parts.append(_rows_to_md_table(rows))

    # Body text: prefer born-digital, fall back to OCR
    if has_text_layer:
        parts.append(text_layer.strip())
    if ocr_md.strip():
        # Only add OCR body if it adds anything (born-digital text wins).
        if not has_text_layer:
            parts.append(ocr_md.strip())

    page_md = "\n\n".join(p for p in parts if p.strip())

    # 4) Label the page's source for the caller
    if has_text_layer and used_ocr:
        src = "hybrid"
    elif has_text_layer:
        src = "text-layer"
    elif used_ocr:
        src = "ocr"
    else:
        src = "empty"

    return page_md, all_tables_rows, src, warnings


# ── Scanned-page detection ───────────────────────────────────────────
def _page_looks_scanned(page: Any) -> bool:
    """Heuristic: page has no text layer AND ≥ _SCANNED_COVERAGE of its
    area is covered by image bboxes. Pure-blank pages return False
    so we don't OCR genuinely empty pages.
    """
    try:
        img_list = page.get_images(full=True)
        if not img_list:
            return False
        page_rect = page.rect
        page_area = max(1.0, page_rect.width * page_rect.height)
        img_area = 0.0
        # info_dict path gives us bboxes; iterate page.get_image_info() if available
        try:
            infos = page.get_image_info(xrefs=True)
            for info in infos:
                bbox = info.get("bbox")
                if not bbox:
                    continue
                x0, y0, x1, y1 = bbox
                img_area += max(0.0, (x1 - x0) * (y1 - y0))
        except Exception:
            # If get_image_info is unavailable, assume any image on a
            # no-text page = scanned (safer than missing OCR).
            return True
        return (img_area / page_area) >= _SCANNED_COVERAGE
    except Exception:
        return False


# ── OCR a single page into markdown + tables ─────────────────────────
def _ocr_page_markdown(page: Any, engine: Any) -> Tuple[str, List[List[List[str]]], List[str]]:
    """Rasterise → OCR → cluster boxes → emit markdown + table rows."""
    from core import pdf_backend as fitz
    import numpy as np

    warnings: List[str] = []
    zoom = _OCR_DPI / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img_bytes = pix.tobytes("png")

    # rapidocr accepts a numpy array; use PIL → np to avoid a temp file on disk.
    try:
        from PIL import Image
        pil = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img_np = np.array(pil)
    except Exception:
        warnings.append(f"image decode failed ()")
        return "", [], warnings

    try:
        result, _elapse = engine(img_np)
    except Exception:
        warnings.append(f"ocr engine error ()")
        return "", [], warnings

    if not result:
        return "", [], warnings

    boxes: List[_OcrBox] = []
    for entry in result:
        # rapidocr returns [box, text, score] where box is 4 (x,y) pts
        if not entry or len(entry) < 3:
            continue
        box_pts, text, score = entry[0], entry[1], entry[2]
        if score is None or float(score) < _OCR_CONF_FLOOR:
            continue
        if not text or not str(text).strip():
            continue
        try:
            xs = [float(p[0]) for p in box_pts]
            ys = [float(p[1]) for p in box_pts]
        except Exception:
            continue
        boxes.append(_OcrBox(
            text=str(text).strip(),
            x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys),
            conf=float(score),
        ))

    if not boxes:
        return "", [], warnings

    # Sort top-to-bottom for row clustering
    boxes.sort(key=lambda b: (b.cy, b.x0))

    # Row cluster: break when vertical gap > median_h * _ROW_GAP_MULT * 1.6
    heights = [b.h for b in boxes if b.h > 0]
    med_h = statistics.median(heights) if heights else 12.0
    row_break = max(4.0, med_h * (1.0 + _ROW_GAP_MULT))

    rows: List[List[_OcrBox]] = []
    cur_row: List[_OcrBox] = []
    last_cy: Optional[float] = None
    for b in boxes:
        if last_cy is None or abs(b.cy - last_cy) <= row_break:
            cur_row.append(b)
        else:
            rows.append(sorted(cur_row, key=lambda x: x.x0))
            cur_row = [b]
        last_cy = b.cy if last_cy is None else (last_cy + b.cy) / 2.0
    if cur_row:
        rows.append(sorted(cur_row, key=lambda x: x.x0))

    # Detect table-shaped row runs: ≥ _TABLE_MIN_ROWS consecutive rows
    # that all have ≥ _TABLE_MIN_COLS boxes and share at least
    # _TABLE_MIN_COLS x-aligned column bands (median start within ±25 % of
    # median row-width / cols).
    tables_rows: List[List[List[str]]] = []
    prose_rows: List[str] = []

    i = 0
    while i < len(rows):
        # Try to grow a table starting at i
        j = i
        table_rows: List[List[_OcrBox]] = []
        while j < len(rows) and len(rows[j]) >= _TABLE_MIN_COLS:
            table_rows.append(rows[j])
            j += 1
        if len(table_rows) >= _TABLE_MIN_ROWS and _rows_are_columnar(table_rows):
            tables_rows.append(_columnise(table_rows))
            i = j
        else:
            # Treat as prose: join boxes with two spaces between columns
            prose_rows.append("  ".join(b.text for b in rows[i]))
            i += 1

    parts: List[str] = []
    for trows in tables_rows:
        parts.append(_rows_to_md_table(trows))
    if prose_rows:
        parts.append("\n".join(prose_rows))
    return "\n\n".join(parts), tables_rows, warnings


def _rows_are_columnar(rows: List[List[_OcrBox]]) -> bool:
    """True when ≥ _TABLE_MIN_COLS x-bands repeat across most rows."""
    if len(rows) < _TABLE_MIN_ROWS:
        return False
    # Take the smallest row's column count as the table width candidate
    widths = [len(r) for r in rows]
    cols = min(widths)
    if cols < _TABLE_MIN_COLS:
        return False
    # Sample each row's first `cols` box.x0; check std-dev across rows
    starts_per_col: List[List[float]] = [[] for _ in range(cols)]
    for r in rows:
        for c in range(cols):
            starts_per_col[c].append(r[c].x0)
    # Median width of a row → tolerance band
    row_widths = [max(r[-1].x1 - r[0].x0 for r in rows if r), 1.0]
    page_span = max(row_widths)
    tol = page_span * 0.08  # 8 % of the row span
    for col_starts in starts_per_col:
        if not col_starts:
            return False
        med = statistics.median(col_starts)
        if any(abs(x - med) > tol for x in col_starts):
            return False
    return True


def _columnise(rows: List[List[_OcrBox]]) -> List[List[str]]:
    """Project each box into its nearest column slot, then return strings."""
    cols = min(len(r) for r in rows)
    # Anchor column positions on the median start of each column index
    anchors: List[float] = []
    for c in range(cols):
        anchors.append(statistics.median([r[c].x0 for r in rows]))
    out: List[List[str]] = []
    for r in rows:
        slots: List[List[str]] = [[] for _ in range(cols)]
        for b in r:
            # Pick the closest anchor for this box
            best_idx = min(range(cols), key=lambda c: abs(b.cx - (anchors[c])))
            slots[best_idx].append(b.text)
        out.append([" ".join(s) for s in slots])
    return out


# ── Markdown helpers ─────────────────────────────────────────────────
def _rows_to_md_table(rows: List[List[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    # Right-pad short rows so the pipe table stays rectangular
    padded = [list(r) + [""] * (width - len(r)) for r in rows]
    header = padded[0]
    body = padded[1:] if len(padded) > 1 else []
    md = ["| " + " | ".join(_esc(c) for c in header) + " |",
          "| " + " | ".join(["---"] * width) + " |"]
    for r in body:
        md.append("| " + " | ".join(_esc(c) for c in r) + " |")
    return "\n".join(md)


def _esc(cell: str) -> str:
    # Pipe escape so cell content can't break the table grid
    return (cell or "").replace("|", "\\|").strip()
