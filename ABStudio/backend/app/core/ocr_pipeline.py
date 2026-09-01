# SPDX-License-Identifier: Apache-2.0
"""Hybrid OCR / document-extraction orchestrator for Build Studio.

This module is the **single entry point** both ``app/api/documents.py``
(transient chat attachments) and ``app/api/kb.py`` (persistent KB ingest)
call to extract text from uploaded files. It wraps the parent platform's
``core.document_parser`` and ``core.pdf_ocr`` without forking them, and
adds these enrichments missing from the previous direct-call paths:

1. **Image files as first-class inputs.** PNG/JPG/JPEG/TIFF/BMP/WEBP are
   accepted standalone and OCR'd via the cached RapidOCR singleton from
   ``core.pdf_ocr._get_ocr_engine``. Previously these were rejected at
   the validator.

2. **Multi-engine table extraction for unstructured PDFs.** A cascade of
   pdfplumber (default), Camelot lattice (ruled tables), and Camelot
   stream (whitespace-aligned tables in bank statements / invoices /
   unstructured reports) recovers tables that ``parse_file_structured``
   misses. Each engine is optional — missing imports degrade gracefully
   with a warning, never a hard failure.

3. **Embedded image OCR inside PDFs.** Each ``page.get_images()`` is
   walked and individually OCR'd, so a small chart on a text-heavy page
   no longer disappears entirely.

4. **Unstructured-data salvage pass.** When prior passes return less than
   ``_SALVAGE_THRESHOLD_CHARS_PER_PAGE`` chars/page on average (typical
   of multi-column scans, newspapers, or partially-scanned reports), a
   whole-page rasterise + low-threshold OCR pass runs to recover any
   text the structured parsers missed.

5. **Sentinel error detection.** Both empty *and* sentinel-string parser
   results (``[PDF parse error …]`` etc., listed in
   ``app.core.parser_errors``) trigger the OCR fallback. The chat path
   previously leaked sentinel strings verbatim to the model.

6. **Rich metadata in the return envelope.** The route layers can now
   surface per-file ``engine``, ``page_count``, ``images_extracted``,
   ``tables_extracted``, ``warnings``, and ``cache_hit`` to the UI.

7. **Content-hash result cache** via ``app.core.ocr_cache`` — identical
   re-uploads skip OCR entirely.

The module is import-safe: every optional dependency (pdfplumber,
camelot, pillow, fitz, the parent-platform ``core.*`` packages) is
imported lazily inside the functions that use them and the failure is
turned into a warning string on the result, never a 500.
"""
from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from app.core.parser_errors import is_parser_error

from core.logger import logger

# ────────────────────────────────────────────────────────────────────────
# Public dataclasses
# ────────────────────────────────────────────────────────────────────────

@dataclass
class ImageInfo:
    """Metadata for one image extracted from a document."""
    page: int
    index: int
    width: int
    height: int
    ocr_text: str = ""
    vision_description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TableInfo:
    """Metadata for one table extracted from a document."""
    page: int
    rows: int
    cols: int
    engine: str
    markdown: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Markdown can be long; route layer trims if needed.
        return d


@dataclass
class PageInfo:
    """Per-page extraction record."""
    page: int
    source: str  # "text-layer" | "rapidocr" | "vision" | "salvage" | "structured"
    char_count: int
    table_count: int
    image_count: int
    warnings: List[str] = field(default_factory=list)


@dataclass
class ExtractionOptions:
    """User-overridable extraction options.

    These map 1:1 to the form fields exposed by the route layer. Anything
    that influences the produced text/structure goes here so the cache
    key fingerprint is correct.
    """
    force_ocr: bool = False
    describe_visuals: bool = False
    ocr_lang: str = "en"
    extract_images: bool = True
    extract_tables: bool = True
    no_cache: bool = False

    def to_cache_key(self) -> Dict[str, Any]:
        # ``no_cache`` deliberately omitted — it controls cache USE,
        # not cache CONTENT.
        return {
            "force_ocr": self.force_ocr,
            "describe_visuals": self.describe_visuals,
            "ocr_lang": self.ocr_lang,
            "extract_images": self.extract_images,
            "extract_tables": self.extract_tables,
        }


@dataclass
class ExtractionResult:
    """Final envelope returned to the route layer."""
    text: str
    engine: str  # "text-layer" | "rapidocr" | "vision" | "mixed" | "structured"
    pages: List[PageInfo] = field(default_factory=list)
    images: List[ImageInfo] = field(default_factory=list)
    tables: List[TableInfo] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    page_count: int = 0
    char_count: int = 0
    original_char_count: int = 0
    truncated: bool = False
    cache_hit: bool = False

    def to_response_dict(self) -> Dict[str, Any]:
        """Compact dict for the JSON response — heavy fields omitted."""
        return {
            "text": self.text,
            "engine": self.engine,
            "warnings": self.warnings,
            "page_count": self.page_count,
            "char_count": self.char_count,
            "original_char_count": self.original_char_count,
            "truncated": self.truncated,
            "images_extracted": len(self.images),
            "tables_extracted": len(self.tables),
            "cache_hit": self.cache_hit,
        }

    def to_cache_payload(self) -> Dict[str, Any]:
        """Serializable form for the on-disk cache."""
        return {
            "text": self.text,
            "engine": self.engine,
            "pages": [asdict(p) for p in self.pages],
            "images": [i.to_dict() for i in self.images],
            "tables": [t.to_dict() for t in self.tables],
            "warnings": list(self.warnings),
            "page_count": self.page_count,
            "char_count": self.char_count,
            "original_char_count": self.original_char_count,
            "truncated": self.truncated,
        }

    @classmethod
    def from_cache_payload(cls, payload: Dict[str, Any]) -> "ExtractionResult":
        return cls(
            text=payload.get("text", "") or "",
            engine=payload.get("engine", "cached"),
            pages=[PageInfo(**p) for p in payload.get("pages", [])],
            images=[ImageInfo(**i) for i in payload.get("images", [])],
            tables=[TableInfo(**t) for t in payload.get("tables", [])],
            warnings=list(payload.get("warnings", [])),
            page_count=int(payload.get("page_count", 0)),
            char_count=int(payload.get("char_count", 0)),
            original_char_count=int(payload.get("original_char_count", 0)),
            truncated=bool(payload.get("truncated", False)),
            cache_hit=True,
        )


# ────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────

IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "tiff", "tif", "bmp", "webp"})

# Average chars/page below which the unstructured-data salvage pass runs.
# Tuned by inspection: a born-digital prose page yields well over 500
# chars; multi-column scans and partially-extracted PDFs often fall under
# 100. The salvage pass is cheap on small docs and necessary on bad ones.
_SALVAGE_THRESHOLD_CHARS_PER_PAGE = 100

# Hard cap on text returned to a single caller. Mirrors the existing
# documents.py cap so we never silently exceed it inside the pipeline.
DEFAULT_MAX_CHARS = 60_000

# Skipped page warning cap so we do not flood ``warnings`` on a 1000-pg
# corrupt PDF.
_MAX_WARNING_LINES = 20


def supported_extensions(text_formats: frozenset) -> frozenset:
    """Combine the caller's structured-text allow-list with image formats.

    Routes pass their pre-existing extension allow-list; this helper adds
    the image extensions so the route does not have to know about them.
    """
    return text_formats | IMAGE_EXTENSIONS


def is_image_ext(ext: str) -> bool:
    return (ext or "").lower() in IMAGE_EXTENSIONS


def vision_available() -> bool:
    """True when Gemini Vision can be invoked.

    Used by the UI capabilities endpoint to enable/disable the "Describe
    figures with Vision" toggle, and internally to skip vision passes when
    the key is unset.
    """
    return bool(os.environ.get("GOOGLE_API_KEY"))


# ────────────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────────────

def extract(
    *,
    raw_bytes: bytes,
    ext: str,
    filename: str,
    options: Optional[ExtractionOptions] = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> ExtractionResult:
    """Extract structured text from a single uploaded file.

    Parameters
    ----------
    raw_bytes: bytes
        The file contents.
    ext: str
        Lower-cased extension WITHOUT the leading dot (e.g. ``"pdf"``).
    filename: str
        Original filename — used for logging / labelling only.
    options: ExtractionOptions
        User-overridable extraction knobs.
    max_chars: int
        Hard truncation cap. Caller already applies its own; the pipeline
        does not normally hit this but enforces it as a final safeguard.

    Returns
    -------
    ExtractionResult
        Always returns a result. Missing optional libs become entries in
        ``result.warnings``; only completely unrecoverable I/O turns into
        a ``ValueError`` for the route to translate to a 400/500.
    """
    options = options or ExtractionOptions()
    ext = (ext or "").lower()

    # ── Dispatch by type ────────────────────────────────────────────────
    suffix = f".{ext}" if ext else ""
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        os.write(fd, raw_bytes)
    finally:
        os.close(fd)

    try:
        if is_image_ext(ext):
            result = _extract_image(tmp_path, filename, options)
        elif ext == "pdf":
            result = _extract_pdf(tmp_path, filename, options)
        else:
            result = _extract_structured(tmp_path, ext, filename, options)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # ── Post-process: truncate, return ───────────────────────────────────
    _apply_truncation(result, max_chars)
    return result


def _apply_truncation(result: ExtractionResult, max_chars: int) -> None:
    original_len = len(result.text)
    result.original_char_count = result.original_char_count or original_len
    if original_len > max_chars:
        result.text = result.text[:max_chars]
        result.truncated = True
    result.char_count = len(result.text)


# Map cache-warning prefixes to the import name we should probe to decide
# whether the warning is now stale.  The pipeline emits these exact
# strings at lines 616 / 649 / etc.; keep this table in sync if those
# strings change.
_MISSING_LIB_WARNING_PROBES = {
    "pdfplumber not installed":                "pdfplumber",
    "camelot not installed":                   "camelot",
    "core.pdf_backend not importable":         "core.pdf_backend",
    "Pillow not installed":                    "PIL",
}


def _stale_missing_lib_warnings(warnings: List[str]) -> List[str]:
    """Return the subset of *warnings* that say "<lib> not installed"
    for libs that ARE now importable in this process.  Used to detect
    when a cached extraction predates an OCR-deps install and should be
    re-run from scratch.
    """
    stale: List[str] = []
    for w in warnings:
        for prefix, mod_name in _MISSING_LIB_WARNING_PROBES.items():
            if not w.startswith(prefix):
                continue
            try:
                __import__(mod_name)
            except Exception:
                # Still missing — warning is accurate, not stale.
                break
            stale.append(w)
            break
    return stale


# ────────────────────────────────────────────────────────────────────────
# Structured (DOCX/PPTX/XLSX/CSV/HTML/RTF/TXT/JSON/MD) extraction
# ────────────────────────────────────────────────────────────────────────

def _extract_structured(
    path: str, ext: str, filename: str, options: ExtractionOptions,
) -> ExtractionResult:
    """Delegate to ``core.document_parser.parse_file_structured``.

    Mirrors the existing direct-call sites but adds:
      * sentinel-error detection (returns warnings instead of leaking).
      * empty-result detection (returns an explicit warning).
    """
    warnings: List[str] = []
    try:
        from core.document_parser import parse_file_structured  # type: ignore
    except ImportError as exc:
        return ExtractionResult(
            text="",
            engine="structured",
            warnings=[f"Document parser unavailable: {exc}"],
        )

    try:
        parsed = parse_file_structured(path, ext, filename)
    except Exception as exc:
        return ExtractionResult(
            text="",
            engine="structured",
            warnings=[f"Parser raised: {exc}"],
        )

    text = (parsed.get("content") or "").strip()
    if is_parser_error(text):
        warnings.append(f"Parser sentinel: {text[:120]}")
        text = ""

    # Page count is only set for PDFs by parse_file_structured, but the
    # other formats are not paged, so 1 is a reasonable default.
    pages = int(((parsed.get("metadata") or {}).get("pages") or 1) or 1)

    return ExtractionResult(
        text=text,
        engine="structured" if text else "structured-empty",
        page_count=pages,
        warnings=warnings,
    )


# ────────────────────────────────────────────────────────────────────────
# Image extraction (standalone PNG/JPG/TIFF/...)
# ────────────────────────────────────────────────────────────────────────

def _extract_image(
    path: str, filename: str, options: ExtractionOptions,
) -> ExtractionResult:
    """OCR a standalone image. Handles screenshots, photos, scanned pages.

    Reuses the RapidOCR singleton from ``core.pdf_ocr._get_ocr_engine``.
    Falls back to Gemini Vision for low-text images (charts/diagrams) if
    a key is available; otherwise just attaches a warning.
    """
    warnings: List[str] = []

    # Normalize via Pillow (EXIF orientation, RGB conversion).
    try:
        from PIL import Image, ImageOps  # type: ignore
    except ImportError as exc:
        return ExtractionResult(
            text="",
            engine="image",
            warnings=[f"Pillow unavailable: {exc}"],
        )

    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            width, height = im.size
            # Persist a normalized copy for downstream consumers.
            norm_path = path + ".norm.png"
            im.save(norm_path, format="PNG")
    except Exception as exc:
        return ExtractionResult(
            text="",
            engine="image",
            warnings=[f"Could not open image: {exc}"],
        )

    # ── Step 2: contrast/sharpen pre-process so RapidOCR sees clean edges.
    # We OCR the *preprocessed* image but keep `norm_path` around for the
    # Gemini-Vision fallback below (vision models prefer the original).
    ocr_input_path = norm_path
    try:
        with open(norm_path, "rb") as fh_read:
            pre_bytes = _preprocess_image_for_ocr(fh_read.read())
        if pre_bytes:
            ocr_input_path = norm_path + ".pre.png"
            import contextlib
            with contextlib.closing(open(ocr_input_path, "wb")) as _fh:
                _fh.write(pre_bytes)
                _fh.flush()
    except Exception:
        ocr_input_path = norm_path  # fall back to the un-preprocessed copy

    try:
        ocr_text, box_count = _run_rapidocr_on_path(ocr_input_path)
    except Exception as exc:
        ocr_text, box_count = "", 0
        warnings.append(f"RapidOCR failed: {exc}")
    finally:
        try:
            os.unlink(norm_path)
        except OSError:
            pass
        if ocr_input_path != norm_path:
            try:
                os.unlink(ocr_input_path)
            except OSError:
                pass

    engine = "rapidocr" if ocr_text else "image-empty"
    vision_text = ""

    # Low-text image (chart/diagram) → optional Gemini vision pass.
    if (box_count < 10 or len(ocr_text) < 20) and options.describe_visuals:
        if vision_available():
            vision_text = _try_parse_image_vision(path, filename, warnings)
            if vision_text:
                engine = "mixed" if ocr_text else "vision"
        else:
            warnings.append(
                "Low-text image detected; Gemini Vision unavailable (GOOGLE_API_KEY unset)."
            )

    text_parts: List[str] = []
    if ocr_text:
        text_parts.append(ocr_text)
    if vision_text:
        text_parts.append(f"\n> Figure description: {vision_text}")

    text = "\n".join(text_parts).strip()
    image_info = ImageInfo(
        page=1, index=0, width=width, height=height,
        ocr_text=ocr_text, vision_description=vision_text,
    )

    return ExtractionResult(
        text=text,
        engine=engine,
        page_count=1,
        images=[image_info],
        pages=[PageInfo(
            page=1, source=engine, char_count=len(text),
            table_count=0, image_count=1, warnings=list(warnings),
        )],
        warnings=warnings,
    )


# ────────────────────────────────────────────────────────────────────────
# PDF multi-pass extraction
# ────────────────────────────────────────────────────────────────────────

def _extract_pdf(
    path: str, filename: str, options: ExtractionOptions,
) -> ExtractionResult:
    """Multi-pass PDF extraction.

    Pass 1: text-layer via ``parse_file_structured`` (markitdown markdown).
    Pass 2: tables via pdfplumber + Camelot (lattice + stream).
    Pass 3: embedded image OCR.
    Pass 4: scanned/error → ``core.pdf_ocr.extract_pdf``.
    Pass 5: unstructured-data salvage (whole-page OCR if char/page is low).
    Pass 6: optional Gemini vision for chart-heavy pages.
    """
    warnings: List[str] = []
    images: List[ImageInfo] = []
    tables: List[TableInfo] = []
    pages_info: List[PageInfo] = []

    # ── Pass 1: structured text layer ───────────────────────────────────
    pass1 = _extract_structured(path, "pdf", filename, options)
    text_layer = pass1.text
    structured_warnings = pass1.warnings
    warnings.extend(structured_warnings)
    page_count = pass1.page_count or _count_pdf_pages(path) or 1

    # ── Fix A: detect fully-scanned PDFs early to skip redundant passes ─
    # A "fully scanned" PDF has no usable text layer AND its pages
    # register as scanned by the partial-scan probe. On such docs Camelot
    # (lattice + stream), the embedded-image OCR pass, and the salvage
    # pass are all either useless or duplicate work relative to the
    # single ``_run_pdf_ocr`` call below — they collectively add 40-120s
    # per document with no incremental text recovered. Short-circuit
    # them so the scanned-PDF path is: parse → ocr → return.
    #
    # ``looks_scanned`` is memoised into a local because the same probe
    # feeds the ``needs_ocr`` decision below — fitz would otherwise open
    # and iterate every page twice.
    text_layer_missing = (not text_layer.strip()) or is_parser_error(text_layer)
    looks_scanned = _looks_partially_scanned(path, warnings) if text_layer_missing else False
    is_fully_scanned = text_layer_missing and looks_scanned

    # ── Pass 2: tables via pdfplumber + Camelot ─────────────────────────
    # Skip entirely for fully-scanned PDFs: Camelot needs a text layer to
    # detect rulings/columns; without one it burns 20-60s finding nothing.
    if options.extract_tables and not is_fully_scanned:
        pdfplumber_tables = _extract_tables_pdfplumber(path, warnings)
        camelot_lattice_tables = _extract_tables_camelot(path, "lattice", warnings)
        camelot_stream_tables = _extract_tables_camelot(path, "stream", warnings)
        # Trivial dedup: keep all distinct (page, rows, cols) signatures.
        tables = _merge_tables(
            pdfplumber_tables, camelot_lattice_tables, camelot_stream_tables,
        )

    # ── Pass 3: embedded image OCR ──────────────────────────────────────
    # Fix D: skip on fully-scanned PDFs. Such docs typically embed one
    # page-sized raster per page; ``_extract_embedded_images`` would OCR
    # each of them, then ``_run_pdf_ocr`` would OCR the same pages again
    # as full-page rasters — pure duplicate work.
    if options.extract_images and not is_fully_scanned:
        images = _extract_embedded_images(path, filename, options, warnings)

    # ── Determine if OCR fallback is needed ─────────────────────────────
    # Reuses ``looks_scanned`` (computed above) so we don't rescan the
    # PDF with fitz a second time.
    needs_ocr = (
        options.force_ocr
        or text_layer_missing
        or looks_scanned
    )

    ocr_text = ""
    ocr_engine_label = ""
    if needs_ocr:
        ocr_text, ocr_engine_label, ocr_warnings = _run_pdf_ocr(
            path, filename, options,
        )
        warnings.extend(ocr_warnings)

    # ── Pass 5: unstructured-data salvage ───────────────────────────────
    # Fix C: tighten the guard. The salvage pass is a whole-page
    # rasterise + RapidOCR pass — but ``_run_pdf_ocr`` already IS a
    # whole-page RapidOCR pass on scanned PDFs, so running salvage after
    # it is pure duplicate work. Also skip when we already know the PDF
    # is fully scanned (``_run_pdf_ocr`` handled it). Salvage now only
    # runs for PDFs with a partial/degraded text layer that neither Pass
    # 1 nor Pass 4 fully recovered.
    combined_text_pre_salvage = ocr_text if ocr_text else text_layer
    avg_chars = (
        len(combined_text_pre_salvage) / page_count
        if page_count > 0 else len(combined_text_pre_salvage)
    )
    salvage_text = ""
    if (
        not options.force_ocr
        and not is_fully_scanned
        and not ocr_text
        and avg_chars < _SALVAGE_THRESHOLD_CHARS_PER_PAGE
    ):
        salvage_text, salvage_warnings = _salvage_unstructured_pdf(
            path, filename, warnings_limit=_MAX_WARNING_LINES,
        )
        warnings.extend(salvage_warnings)

    # ── Compose final text ──────────────────────────────────────────────
    parts: List[str] = []
    primary = text_layer or ocr_text or salvage_text
    if primary:
        parts.append(primary)
    if tables and options.extract_tables:
        tables_md = "\n\n".join(t.markdown for t in tables if t.markdown)
        if tables_md.strip():
            parts.append("\n\n## Extracted tables\n\n" + tables_md)
    if images and options.extract_images:
        image_blocks: List[str] = []
        for img in images:
            if img.ocr_text or img.vision_description:
                header = f"![image: page {img.page}, idx {img.index}]"
                body_parts: List[str] = []
                if img.ocr_text:
                    body_parts.append(f"OCR: {img.ocr_text}")
                if img.vision_description:
                    body_parts.append(f"Vision: {img.vision_description}")
                image_blocks.append(header + "\n" + "\n".join(body_parts))
        if image_blocks:
            parts.append("\n\n## Embedded images\n\n" + "\n\n".join(image_blocks))

    final_text = "\n\n".join(parts).strip()

    # ── Pick engine label ───────────────────────────────────────────────
    engine = _pick_engine_label(
        had_text_layer=bool(text_layer),
        had_ocr=bool(ocr_text),
        had_salvage=bool(salvage_text),
        had_images=bool(images),
        had_tables=bool(tables),
        ocr_engine_label=ocr_engine_label,
    )

    # ── Per-page info (best-effort) ─────────────────────────────────────
    if not pages_info:
        for p in range(1, page_count + 1):
            page_imgs = [i for i in images if i.page == p]
            page_tbls = [t for t in tables if t.page == p]
            pages_info.append(PageInfo(
                page=p,
                source=engine,
                char_count=0,  # per-page char count not tracked here
                table_count=len(page_tbls),
                image_count=len(page_imgs),
                warnings=[],
            ))

    # Trim runaway warnings.
    if len(warnings) > _MAX_WARNING_LINES:
        warnings = warnings[:_MAX_WARNING_LINES] + [
            f"... ({len(warnings) - _MAX_WARNING_LINES} more warnings truncated)"
        ]

    return ExtractionResult(
        text=final_text,
        engine=engine,
        pages=pages_info,
        images=images,
        tables=tables,
        warnings=warnings,
        page_count=page_count,
    )


# ────────────────────────────────────────────────────────────────────────
# Helpers — pdfplumber tables
# ────────────────────────────────────────────────────────────────────────

def _extract_tables_pdfplumber(
    path: str, warnings: List[str],
) -> List[TableInfo]:
    """Best-effort table extraction with pdfplumber."""
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        warnings.append("pdfplumber not installed; skipping that table pass.")
        return []
    found: List[TableInfo] = []
    try:
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                try:
                    raw_tables = page.extract_tables() or []
                except Exception as exc:
                    warnings.append(f"pdfplumber page {i}: {exc}")
                    continue
                for t in raw_tables:
                    md = _rows_to_markdown(t)
                    if md:
                        rows = len(t)
                        cols = max((len(r) for r in t), default=0)
                        found.append(TableInfo(
                            page=i, rows=rows, cols=cols,
                            engine="pdfplumber", markdown=md,
                        ))
    except Exception as exc:
        warnings.append(f"pdfplumber open failed: {exc}")
    return found


def _extract_tables_camelot(
    path: str, flavor: str, warnings: List[str],
) -> List[TableInfo]:
    """Best-effort table extraction with Camelot (lattice or stream)."""
    try:
        import camelot  # type: ignore
    except ImportError:
        # Only warn once per flavor.
        msg = f"camelot not installed; skipping {flavor} table pass."
        if msg not in warnings:
            warnings.append(msg)
        return []
    found: List[TableInfo] = []
    try:
        tables = camelot.read_pdf(path, pages="all", flavor=flavor, suppress_stdout=True)
    except Exception as exc:
        warnings.append(f"Camelot {flavor} failed: {exc}")
        return []
    for t in tables:
        try:
            df = t.df
            rows_list = df.values.tolist()
            md = _rows_to_markdown(rows_list)
            if not md:
                continue
            found.append(TableInfo(
                page=int(getattr(t, "page", 1) or 1),
                rows=len(rows_list),
                cols=(len(rows_list[0]) if rows_list else 0),
                engine=f"camelot-{flavor}",
                markdown=md,
            ))
        except Exception as exc:
            warnings.append(f"Camelot {flavor} parse failure: {exc}")
            continue
    return found


def _merge_tables(*lists: List[TableInfo]) -> List[TableInfo]:
    """Cheap dedup: drop later tables that share (page, rows, cols) with
    an earlier one (preserves engine priority).
    """
    seen: set = set()
    out: List[TableInfo] = []
    for lst in lists:
        for t in lst:
            sig = (t.page, t.rows, t.cols, t.markdown[:80] if t.markdown else "")
            if sig in seen:
                continue
            seen.add(sig)
            out.append(t)
    return out


def _rows_to_markdown(rows: List[List[Any]]) -> str:
    """Render a 2-D list as a GitHub-flavoured pipe table."""
    if not rows:
        return ""
    # Clean & coerce to strings.
    clean_rows: List[List[str]] = []
    for r in rows:
        clean_rows.append([
            ("" if c is None else str(c)).strip().replace("\n", " ").replace("|", "\\|")
            for c in r
        ])
    # Drop fully-empty rows.
    clean_rows = [r for r in clean_rows if any(c.strip() for c in r)]
    if not clean_rows:
        return ""
    width = max(len(r) for r in clean_rows)
    # Pad short rows.
    clean_rows = [r + [""] * (width - len(r)) for r in clean_rows]
    header = clean_rows[0]
    sep = ["---"] * width
    body = clean_rows[1:] if len(clean_rows) > 1 else []
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join(sep) + " |"]
    for r in body:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────
# Helpers — embedded image extraction
# ────────────────────────────────────────────────────────────────────────

def _extract_embedded_images(
    path: str, filename: str, options: ExtractionOptions, warnings: List[str],
) -> List[ImageInfo]:
    """Pull every embedded raster image from a PDF and OCR each one."""
    try:
        from core import pdf_backend as fitz  # type: ignore
    except ImportError:
        warnings.append("core.pdf_backend not importable; skipping embedded-image pass.")
        return []
    doc = None
    found: List[ImageInfo] = []
    # page.get_images(full=True) returns the same xref every time
    # an image is *referenced* on a page. Most real PDFs share a single
    # logo / chart across multiple pages, so iterating naively over every
    # page yields N copies of the same picture. We dedup by xref so the
    # chip-counter "N images" matches what the human actually sees in the
    # PDF (e.g. a 4-page doc with one logo shows "1 image", not "4").
    seen_xrefs: set = set()
    try:
        try:
            doc = fitz.open(path)
        except Exception as exc:
            warnings.append(f"fitz.open failed in embedded-image pass: {exc}")
            return []
        for page_index in range(len(doc)):
            try:
                page = doc[page_index]
                img_list = page.get_images(full=True)
            except Exception as exc:
                warnings.append(f"Embedded images page {page_index + 1}: {exc}")
                continue
            for idx, img in enumerate(img_list):
                xref = img[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    img_dict = doc.extract_image(xref)
                except Exception as exc:
                    warnings.append(
                        f"extract_image failed page {page_index + 1} xref {xref}: {exc}"
                    )
                    continue
                img_bytes = img_dict.get("image")
                ext_name = img_dict.get("ext", "png")
                if not img_bytes:
                    continue
                # Cap per-doc image-OCR fanout to avoid pathological inputs.
                if len(found) >= 50:
                    warnings.append(
                        f"Image-OCR cap (50) reached on '{filename}'; remaining images skipped."
                    )
                    return found

                # ── Step 1: re-rasterise low-res embedded images at 300 DPI ──
                # Many PDFs embed stamps / logos at 96-150 DPI which is below
                # RapidOCR's comfort zone for embossed or halftoned text.
                # When the raster is small, re-render the *page region* the
                # image occupies at 300 DPI — this restores the precise
                # strokes the original PDF rendering pipeline saw.
                width_px = int(img_dict.get("width") or 0)
                height_px = int(img_dict.get("height") or 0)
                if max(width_px, height_px) and max(width_px, height_px) < 1000:
                    try:
                        bbox = page.get_image_bbox(img)
                        if bbox and not bbox.is_empty:
                            zoom = 300 / 72  # 300 DPI ≈ 4.17× the default 72 DPI
                            hires = page.get_pixmap(
                                matrix=fitz.Matrix(zoom, zoom),
                                clip=bbox,
                                alpha=False,
                            )
                            img_bytes = hires.tobytes("png")
                            ext_name = "png"
                            width_px = hires.width
                            height_px = hires.height
                    except Exception as exc:  # pragma: no cover — best-effort
                        # If re-rasterisation fails, fall back to the original
                        # raw bytes — never worse than the old behaviour.
                        warnings.append(
                            f"hi-res re-render skipped p{page_index + 1} #{idx}: {exc}"
                        )

                # ── Step 2: contrast + sharpen preprocessing before OCR ──
                img_bytes_for_ocr = _preprocess_image_for_ocr(img_bytes) or img_bytes
                ocr_text, _box_count = _run_rapidocr_on_bytes(img_bytes_for_ocr, ext_name)
                vision_desc = ""
                if (not ocr_text or len(ocr_text) < 20) and options.describe_visuals \
                        and vision_available():
                    vision_desc = _try_describe_image_bytes(
                        img_bytes, ext_name, f"{filename} p{page_index + 1} #{idx}",
                        warnings,
                    )
                found.append(ImageInfo(
                    page=page_index + 1,
                    index=idx,
                    width=width_px,
                    height=height_px,
                    ocr_text=ocr_text,
                    vision_description=vision_desc,
                ))
    finally:
        if doc is not None:
            doc.close()
            doc = None  # explicit release — satisfies static resource-shutdown analysis
    return found


# ────────────────────────────────────────────────────────────────────────
# Helpers — RapidOCR
# ────────────────────────────────────────────────────────────────────────

def _get_rapidocr_engine():
    """Reuse the platform-cached RapidOCR singleton if available, else
    construct one locally on first call.

    Package fallback order (works across Python 3.10 → 3.14):
      1. parent platform's cached singleton (``core.pdf_ocr._get_ocr_engine``)
      2. ``rapidocr_onnxruntime``  — legacy package, wheels capped at Python<3.13
      3. ``rapidocr``              — successor package, required on Python>=3.13

    The two packages have slightly different return shapes; we normalize
    that in ``_run_rapidocr_on_path`` below.
    """
    # Step 3: request angle classification so rotated / circular stamp
    # text (embossed seals, curved captions) is upright before recognition.
    # RapidOCR silently ignores unknown kwargs across versions, but some
    # older wheels reject them at __init__ — fall back to a bare RapidOCR()
    # per package if the kwarg constructor blows up.
    try:
        from core.pdf_ocr import _get_ocr_engine  # type: ignore
        return _get_ocr_engine()
    except Exception:
        pass
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        try:
            return RapidOCR(use_angle_cls=True)
        except TypeError:
            return RapidOCR()
    except Exception:
        pass
    try:
        from rapidocr import RapidOCR  # type: ignore
        try:
            return RapidOCR(params={"Global.use_cls": True})
        except TypeError:
            return RapidOCR()
    except Exception:
        return None


def _preprocess_image_for_ocr(img_bytes: bytes) -> Optional[bytes]:
    """Boost contrast + sharpen to improve RapidOCR accuracy on noisy art.

    Targets stamp paper, embossed seals, watermarked backgrounds, low-DPI
    screenshots — anywhere a soft histogram makes RapidOCR mis-read
    characters (``Rs.500`` → ``百.500``, ``5th`` → ``Sth``).

    Pipeline:
      1. Convert to grayscale (drops the watermark colour cast).
      2. ``ImageOps.autocontrast`` with a 2% histogram cutoff — stretches
         dynamic range without crushing midtones.
      3. ``ImageFilter.SHARPEN`` — restores stroke edges blurred by JPEG
         compression or low-DPI raster sampling.
      4. Soft binarisation: pixels > 180 → white, < 80 → black, midtones
         preserved. Keeps embossed strokes legible while killing the
         watermark.

    Returns the preprocessed PNG bytes, or ``None`` if Pillow is missing /
    the input is not a decodable image (caller falls back to raw bytes).
    """
    try:
        from PIL import Image, ImageOps, ImageFilter  # type: ignore
    except ImportError:
        return None
    try:
        import io
        with Image.open(io.BytesIO(img_bytes)) as im:
            im = ImageOps.exif_transpose(im).convert("L")
            im = ImageOps.autocontrast(im, cutoff=2)
            im = im.filter(ImageFilter.SHARPEN)
            im = im.point(lambda p: 255 if p > 180 else (0 if p < 80 else p))
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=False)
            return buf.getvalue()
    except Exception:
        return None


def _run_rapidocr_on_path(path: str) -> Tuple[str, int]:
    engine = _get_rapidocr_engine()
    if engine is None:
        return "", 0
    # Two return shapes exist in the wild:
    #   legacy `rapidocr_onnxruntime`: engine(path) -> (list[[bbox, text, score]], elapsed_secs)
    #   new    `rapidocr`            : engine(path) -> RapidOCROutput with .txts / .scores / .boxes
    try:
        raw = engine(path)
    except Exception:
        return "", 0
    if raw is None:
        return "", 0

    # New API: result object with .txts attribute.
    txts = getattr(raw, "txts", None)
    if txts is not None:
        lines = [str(t) for t in txts if t]
        return ("\n".join(lines).strip(), len(lines))

    # Legacy API: 2-tuple (boxes, elapsed) — unwrap.
    result = raw
    if isinstance(raw, tuple) and len(raw) >= 1:
        result = raw[0]
    if not result:
        return "", 0
    lines = []
    for det in result:
        # Box format: [bbox, text, score]
        try:
            text = det[1] if len(det) >= 2 else ""
        except Exception:
            text = ""
        if text:
            lines.append(str(text))
    return ("\n".join(lines).strip(), len(lines))


def _run_rapidocr_on_bytes(img_bytes: bytes, ext: str = "png") -> Tuple[str, int]:
    """Persist bytes to a tempfile then OCR. RapidOCR is path-oriented."""
    fd, tmp_path = tempfile.mkstemp(suffix=f".{ext or 'png'}")
    try:
        os.write(fd, img_bytes)
    finally:
        os.close(fd)
    try:
        return _run_rapidocr_on_path(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ────────────────────────────────────────────────────────────────────────
# Helpers — core.pdf_ocr scanned-PDF fallback
# ────────────────────────────────────────────────────────────────────────

def _run_pdf_ocr(
    path: str, filename: str, options: ExtractionOptions,
) -> Tuple[str, str, List[str]]:
    """Call ``core.pdf_ocr.extract_pdf`` if available."""
    warnings: List[str] = []
    try:
        from core.pdf_ocr import extract_pdf  # type: ignore
    except ImportError as exc:
        return "", "", [f"core.pdf_ocr unavailable: {exc}"]
    try:
        out = extract_pdf(path, filename)
    except Exception as exc:
        return "", "", [f"core.pdf_ocr raised: {exc}"]
    text = (out.get("text") or "").strip()
    engine = str(out.get("ocr_engine") or "rapidocr")
    for w in (out.get("warnings") or [])[:_MAX_WARNING_LINES]:
        warnings.append(str(w))
    return text, engine, warnings


def _looks_partially_scanned(path: str, warnings: List[str]) -> bool:
    """Heuristic: ≥30% of pages have no extractable text layer.

    Cheap pre-check that decides whether to invoke the OCR fallback even
    when the structured parser returned *some* text (handles mixed PDFs).
    """
    try:
        from core import pdf_backend as fitz  # type: ignore
    except ImportError:
        return False
    doc = None
    try:
        try:
            doc = fitz.open(path)
        except Exception as exc:
            warnings.append(f"partial-scan probe: {exc}")
            return False
        if len(doc) == 0:
            return False
        empty_pages = 0
        for page in doc:
            try:
                if not (page.get_text("text") or "").strip():
                    empty_pages += 1
            except Exception:
                empty_pages += 1
        return (empty_pages / len(doc)) >= 0.3
    finally:
        if doc is not None:
            doc.close()
            doc = None  # explicit release — satisfies static resource-shutdown analysis


# ────────────────────────────────────────────────────────────────────────
# Helpers — unstructured-data salvage
# ────────────────────────────────────────────────────────────────────────

def _salvage_unstructured_pdf(
    path: str, filename: str, warnings_limit: int = 20,
) -> Tuple[str, List[str]]:
    """Whole-page rasterise + RapidOCR on every page.

    Used when prior passes recovered very little text per page — typical
    of multi-column scans, partially-scanned reports, low-quality faxes.
    Runs only on a bounded number of pages to avoid runaway latency.
    """
    warnings: List[str] = []
    try:
        from core import pdf_backend as fitz  # type: ignore
    except ImportError:
        return "", ["fitz unavailable for salvage pass."]
    doc = None
    parts: List[str] = []
    try:
        try:
            doc = fitz.open(path)
        except Exception as exc:
            return "", [f"salvage open failed: {exc}"]
        max_pages = min(len(doc), 50)  # bounded cost
        for i in range(max_pages):
            try:
                page = doc[i]
                pix = page.get_pixmap(matrix=fitz.Matrix(2.2, 2.2), alpha=False)
                img_bytes = pix.tobytes("png")
                text, _ = _run_rapidocr_on_bytes(img_bytes, "png")
                if text:
                    parts.append(f"## Page {i + 1}\n{text}")
            except Exception as exc:
                if len(warnings) < warnings_limit:
                    warnings.append(f"salvage page {i + 1}: {exc}")
                continue
        if len(doc) > max_pages:
            warnings.append(
                f"Salvage truncated at {max_pages}/{len(doc)} pages to bound latency."
            )
    finally:
        if doc is not None:
            doc.close()
            doc = None  # explicit release — satisfies static resource-shutdown analysis
    return "\n\n".join(parts).strip(), warnings


# ────────────────────────────────────────────────────────────────────────
# Helpers — Gemini Vision fallback
# ────────────────────────────────────────────────────────────────────────

def _try_parse_image_vision(path: str, label: str, warnings: List[str]) -> str:
    try:
        from core.document_parser import parse_image  # type: ignore
    except ImportError:
        warnings.append("parse_image unavailable for vision pass.")
        return ""
    try:
        out = parse_image(path, label)
    except Exception as exc:
        warnings.append(f"parse_image failed: {exc}")
        return ""
    text = (out or "").strip()
    if not text or "vision unavailable" in text.lower():
        return ""
    return text


def _try_describe_image_bytes(
    img_bytes: bytes, ext: str, label: str, warnings: List[str],
) -> str:
    fd, tmp_path = tempfile.mkstemp(suffix=f".{ext or 'png'}")
    try:
        os.write(fd, img_bytes)
    finally:
        os.close(fd)
    try:
        return _try_parse_image_vision(tmp_path, label, warnings)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ────────────────────────────────────────────────────────────────────────
# Helpers — misc
# ────────────────────────────────────────────────────────────────────────

def _count_pdf_pages(path: str) -> int:
    try:
        from core import pdf_backend as fitz  # type: ignore
        doc = fitz.open(path)
        try:
            page_count = len(doc)
        finally:
            doc.close()
            doc = None  # explicit release — satisfies static resource-shutdown analysis
        return page_count
    except Exception:
        return 0


def _pick_engine_label(
    *,
    had_text_layer: bool,
    had_ocr: bool,
    had_salvage: bool,
    had_images: bool,
    had_tables: bool,
    ocr_engine_label: str,
) -> str:
    """Pick a short label that summarises which engines actually ran."""
    if had_text_layer and (had_ocr or had_images or had_tables or had_salvage):
        return "mixed"
    if had_text_layer:
        return "text-layer"
    if had_ocr:
        return ocr_engine_label or "rapidocr"
    if had_salvage:
        return "salvage"
    if had_images or had_tables:
        return "structured"
    return "empty"
