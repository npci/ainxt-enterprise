# SPDX-License-Identifier: Apache-2.0
# ============================================================
# DOCLING PARSER — alternative document extraction backend
#
# Wraps IBM Docling (https://github.com/docling-project/docling) to produce
# clean, structure-rich Markdown for PDF / DOCX / HTML / PPTX uploads.
#
# Why this exists alongside core/document_parser.py:
#   The existing per-format parsers (markitdown, python-docx, BeautifulSoup)
#   work well when source documents use explicit heading styles. They DO NOT
#   recover structure when:
#     - Word docs use plain bold text instead of "Heading 1/2/3" styles
#     - PDFs have no TOC and inconsistent font sizes
#     - HTML uses <p><strong> instead of <h1>-<h6>
#
#   Docling runs ML layout + table models (DocLayNet + TableFormer) and emits
#   a unified DoclingDocument with explicit section hierarchy, restoring the
#   `##` headings that downstream chunkers (docs_store._chunk_document_structured)
#   need to attach section_path metadata to each chunk.
#
# Activation:
#   Controlled by env var USE_DOCLING_PARSER:
#     - "0" / unset (default): use existing per-format parsers — NO behavior change
#     - "1":                   use Docling for PDF/DOCX/HTML/PPTX
#     - "shadow":              run Docling alongside legacy parser, log diff,
#                              but return legacy output (for safe rollout)
#
# OCR backend:
#   PaddleOCR (PP-OCRv4) is used for scanned PDFs via a custom PaddleOcrModel
#   registered into Docling's OCR factory (see core/paddle_ocr_model.py).
#   Set PADDLEOCR_MODELS_PATH to a local directory containing det/, rec/, cls/
#   subdirs for air-gapped deployments. Leave unset to auto-download on first use.
#
# Mixed-PDF support:
#   PDFs where only some pages are scanned (e.g. pages 17, 22, 27, 45 in a
#   60-page document) are now detected and handled correctly. The full document
#   is scanned page-by-page; born-digital pages use the fast text-only converter
#   and scanned pages use the PaddleOCR converter. Results are merged in order.
#
# Failure mode:
#   Any Docling failure (import error, conversion error, timeout) falls back
#   silently to the legacy parser. Uploads never break because Docling is off,
#   missing, or buggy.
# ============================================================

from __future__ import annotations

import os
import threading as _threading
from typing import Optional

from core.logger import logger


# ---- Custom exceptions ----------------------------------------------------

class PageConversionError(RuntimeError):
    """
    Raised by _convert_per_page_smart() when one or more page batches fail
    even after retry.  Carries a human-readable message listing the exact
    failed page ranges so activate_doc() can store it in parse_error and
    surface it to the user in the request/status tab.

    Inherits from RuntimeError so existing callers that catch RuntimeError
    (e.g. _try_docling, activate_doc) handle it automatically without any
    additional except clauses.
    """


# ---- Mode resolution ------------------------------------------------------

_MODE_ENV = "USE_DOCLING_PARSER"


def get_mode() -> str:
    """
    Return the active Docling mode: "off" | "on" | "shadow".

    Resolves env var once per call so an admin can flip the flag without
    restarting workers (useful during incident response).
    """
    raw = (os.environ.get(_MODE_ENV) or "").strip().lower()
    if raw in ("1", "true", "on", "yes"):
        return "on"
    if raw == "shadow":
        return "shadow"
    return "off"


def is_active() -> bool:
    """True when Docling output should be used in place of legacy parsers."""
    return get_mode() == "on"


def is_shadow() -> bool:
    """True when Docling should run in parallel but legacy output is returned."""
    return get_mode() == "shadow"


# ---- Supported formats ----------------------------------------------------

# Docling currently handles these via DocumentConverter. Anything outside this
# set should keep using the legacy parser in core/document_parser.py.
_SUPPORTED = frozenset({"pdf", "docx", "html", "htm", "pptx"})


def supports(file_type: str) -> bool:
    return (file_type or "").lower().strip(".") in _SUPPORTED


# ---- Lazy converter singleton --------------------------------------------
#
# DocumentConverter pre-loads ML models the first time it is constructed
# (layout + table models, ~few hundred MB on disk, ~1–3 s warm-up). We
# cache one instance per worker so subsequent uploads pay zero warm-up cost.
#
# Model location:
#   DocLayNet + TableFormer weights live outside the package. By default
#   Docling fetches them from huggingface.co on first use. In air-gapped /
#   restricted-egress deployments (the typical case here), set the env var:
#
#       DOCLING_ARTIFACTS_PATH=/abs/path/to/docling-models
#
#   pointing at a checkout of ds4sd/docling-models. We pass that path into
#   PdfPipelineOptions(artifacts_path=...) so Docling loads from disk and
#   never reaches out to the network.
#
#   When the env var is unset we keep the default behavior (HF download)
#   so dev machines with internet access continue to work without change.

_ARTIFACTS_ENV = "DOCLING_ARTIFACTS_PATH"

# OCR model location (PaddleOCR PP-OCRv4):
#   PaddleOCR downloads ~50 MB of PP-OCRv4 weights from its CDN into
#   ~/.paddleocr/ the first time OCR runs. In air-gapped deployments set:
#
#       PADDLEOCR_MODELS_PATH=/abs/path/to/paddleocr_models
#
#   pointing at a directory with subdirs:
#       det/  — detection model (PP-OCRv4 det)
#       rec/  — recognition model (PP-OCRv4 rec)
#       cls/  — classification model (PP-OCRv4 cls)
#
#   These are passed as det_model_dir / rec_model_dir / cls_model_dir to
#   PaddleOCR so it loads from disk and never touches the network.
#   Leave unset to allow PaddleOCR's default auto-download behavior.
_PADDLEOCR_ENV = "PADDLEOCR_MODELS_PATH"

# Cache for the second, OCR-enabled converter. Built lazily on the first
# scanned PDF so dev/test workflows that never see a scanned doc don't pay
# the PaddleOCR warm-up cost.
_converter = None
_converter_ocr = None
_converter_hybrid = None
_converter_init_failed = False
_converter_ocr_init_failed = False
_converter_hybrid_init_failed = False


def _resolve_artifacts_path() -> Optional[str]:
    """
    Return an existing local model directory or None.

    The path is validated up-front so a typo in the env var surfaces as a
    clear log line rather than a confusing HuggingFace network error later.
    """
    raw = (os.environ.get(_ARTIFACTS_ENV) or "").strip().strip('"').strip("'")
    if not raw:
        return None
    if not os.path.isdir(raw):
        logger.warning(
            f"docling_parser: {_ARTIFACTS_ENV}={raw!r} but the directory "
            "does not exist. Falling back to HuggingFace download."
        )
        return None
    return raw


def _resolve_paddleocr_path() -> Optional[str]:
    """
    Return an existing PaddleOCR models directory or None.

    Expected to contain subdirs: det/, rec/, cls/ with PP-OCRv4 model files.
    Returns None silently when the env var is unset — that's the explicit
    opt-out signal for environments that allow PaddleOCR's default auto-download.
    Returns None with a warning when the env var points at a missing directory.
    """
    raw = (os.environ.get(_PADDLEOCR_ENV) or "").strip().strip('"').strip("'")
    if not raw:
        return None
    if not os.path.isdir(raw):
        logger.warning(
            f"docling_parser: {_PADDLEOCR_ENV}={raw!r} but the directory "
            "does not exist. PaddleOCR will use default auto-download behavior."
        )
        return None
    return raw


# ---- Scanned-page detection & per-page strategy ---------------------------

# Minimum fraction of page area an image block must cover to trigger OCR on
# that page. Filters out tiny inline icons / logos / watermarks that don't
# carry meaningful text content.
#
# Raised from 0.05 → 0.60 (2026-07-30). Rationale: a genuinely scanned page is
# ONE image covering essentially the whole page (>90%). Born-digital documents
# routinely embed wide banner-shaped diagrams, flowcharts and table screenshots
# that clear a 5% bar without being scans at all. On a payment-scheme dispute
# Management PDF (Microsoft Word 2010, 168 born-digital pages) the 5% threshold
# misclassified 168/168 pages as needing OCR because each carried 471×140-style
# diagram strips (~13% of page area). Override via PDF_IMAGE_AREA_THRESHOLD.
def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


_PDF_IMAGE_AREA_THRESHOLD: float = _env_float("PDF_IMAGE_AREA_THRESHOLD", 0.60, 0.0, 1.0)

# Minimum characters of native (selectable) text on a page for it to be
# treated as born-digital and routed to the fast text-only converter,
# regardless of how much image area the page contains.
#
# Why this gate exists:
#   OCR exists to recover text from pages that have NO extractable text.
#   If a page already yields hundreds of characters via core.pdf_backend, any images
#   on it are diagrams/charts/logos sitting alongside that text — Docling's
#   text pipeline already handles figure captions and table structure.
#   Sending such a page to OCR adds latency and, historically, crashes
#   (PaddleOCR subprocess contention) while adding no new information.
#
# Measured impact on that manual: 168 pages × 900–4,200 native chars each.
#   With this gate  : 0 OCR pages,   ~647 ms, 243,081 chars extracted.
#   Without it      : 168 OCR pages, 230,499 ms, 0 chars (140 batches crashed).
# Override via PDF_MIN_NATIVE_CHARS.
_PDF_MIN_NATIVE_CHARS: int = int(os.getenv("PDF_MIN_NATIVE_CHARS", "100"))

# ── Hybrid (region-OCR) tuning ───────────────────────────────────────────────
# A page classified "hybrid" has BOTH native text and images large enough to
# plausibly contain text (flowcharts, infographic cards, chart labels). It is
# converted with the region-OCR converter: only the image rectangles are sent
# to PaddleOCR and the results are merged with — never allowed to overwrite —
# the native text.
#
# Set PDF_HYBRID_ENABLED=0 to disable hybrid entirely; such pages then fall
# back to "text" (fast, but text baked into images is lost).
_PDF_HYBRID_ENABLED = (
    os.getenv("PDF_HYBRID_ENABLED", "1").strip().lower() in ("1", "true", "on", "yes")
)

# Minimum single-image area (fraction of page) for a page WITH text to be
# considered hybrid. Deliberately small: a 471x140 flowchart strip is ~6% of
# an A4 page and definitely worth OCR-ing. Tiny logos/icons stay below it.
_PDF_HYBRID_MIN_IMAGE_AREA: float = _env_float(
    "PDF_HYBRID_MIN_IMAGE_AREA", 0.03, 0.0, 1.0
)

# bitmap_area_threshold handed to Docling in region mode. Controls which
# bitmap rects get_ocr_rects() returns. Keep at/below the hybrid trigger so
# every image that made a page hybrid is actually OCR'd.
_PDF_HYBRID_BITMAP_THRESHOLD: float = _env_float(
    "PDF_HYBRID_BITMAP_THRESHOLD", 0.03, 0.0, 1.0
)

# Safety valve for very large documents. Hybrid OCR costs seconds per page, so
# a 1000-page manual where every page has a diagram would take hours. Above
# this page count, hybrid pages are downgraded to "text" and a WARNING is
# logged naming the affected page count. 0 disables the cap.
_PDF_HYBRID_MAX_PAGES: int = int(os.getenv("PDF_HYBRID_MAX_PAGES", "0"))

# Boilerplate-image suppression.
#
# Headers, footers, logos and watermarks are embedded on nearly every page of
# a corporate document. They are pure branding — OCR-ing them 168 times yields
# nothing but the company name, repeated.
#
# Any image whose byte content appears on more than this fraction of pages is
# treated as boilerplate and ignored when deciding whether a page is "hybrid".
#
# Measured on a payment-scheme manual: two images (an organisation logo and a header rule)
# appear on 168/168 and 167/168 pages respectively and account for the entire
# 11% image coverage on every page. Suppressing them takes the document from
# 168 hybrid pages to only the handful with genuine diagrams.
_PDF_BOILERPLATE_IMAGE_RATIO: float = _env_float(
    "PDF_BOILERPLATE_IMAGE_RATIO", 0.50, 0.0, 1.0
)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


_PDF_SMART_PARALLEL_ENABLED = (
    os.getenv("PDF_SMART_PARALLEL_ENABLED", "1").strip().lower()
    in ("1", "true", "on", "yes")
)
_PDF_SMART_PARALLEL_THRESHOLD = _env_int("PDF_SMART_PARALLEL_THRESHOLD", 50, 1, 5000)
_PDF_SMART_BATCH_SIZE = _env_int("PDF_SMART_BATCH_SIZE", 25, 1, 200)
_PDF_SMART_OCR_BATCH_SIZE = _env_int("PDF_SMART_OCR_BATCH_SIZE", 1, 1, 25)
_PDF_SMART_HYBRID_BATCH_SIZE = _env_int("PDF_SMART_HYBRID_BATCH_SIZE", 2, 1, 25)
_PDF_SMART_MAX_WORKERS = _env_int("PDF_SMART_MAX_WORKERS", 10, 1, 16)

# ── Phase 1: OCR admission control ───────────────────────────────────────────
# Bounds how many threads may be inside an OCR-bearing conversion at once.
#
# Why a semaphore and not just the pool's internal queue: a thread that has
# already entered converter.convert() has rasterized page images held in
# memory (up to ~13 MB per full page). Blocking there means N threads sit idle
# holding N buffers. Acquiring this semaphore BEFORE the conversion call moves
# the wait earlier — threads queue while holding nothing heavy.
#
# Defaults to the PaddleOCR child-pool size so admissions match real OCR
# capacity: with 3 children, 3 conversions may proceed and the rest wait.
def _default_ocr_slots() -> int:
    try:
        from core.paddle_ocr_subprocess import POOL_SIZE as _PS
        return max(1, int(_PS))
    except Exception:
        return 1


_PDF_OCR_MAX_CONCURRENCY = _env_int(
    "PDF_OCR_MAX_CONCURRENCY", _default_ocr_slots(), 1, 16
)
_OCR_SLOTS = _threading.Semaphore(_PDF_OCR_MAX_CONCURRENCY)


def pdf_scanned_page_indices(path: str, min_chars_per_page: int = 60) -> list[int]:
    """
    Return 0-indexed page numbers that appear to be scanned / image-only.

    Scans ALL pages in the PDF (not just the first few) so mixed PDFs are
    detected correctly. A page is considered scanned when it has fewer than
    min_chars_per_page extractable characters via core.pdf_backend.

    Born-digital pages almost always exceed this threshold; scanned pages
    produce 0 or a handful of stray characters from embedded fonts.

    Returns [] on any error (safe default = no OCR triggered).
    """
    try:
        from core import pdf_backend as fitz
    except ImportError:
        return []
    try:
        with fitz.open(path) as doc:
            scanned = []
            for i in range(len(doc)):
                txt = doc[i].get_text() or ""
                if len(txt.strip()) < min_chars_per_page:
                    scanned.append(i)
            total = len(doc)
            if scanned:
                logger.info(
                    f"docling_parser: pdf_scanned_page_indices '{path}' — "
                    f"{len(scanned)}/{total} scanned pages (0-indexed): "
                    f"{scanned[:20]}{'...' if len(scanned) > 20 else ''}"
                )
            else:
                logger.debug(
                    f"docling_parser: pdf_scanned_page_indices '{path}' — "
                    f"all {total} pages have selectable text (no OCR needed)"
                )
            return scanned
    except Exception as e:
        logger.warning(f"docling_parser: pdf_scanned_page_indices failed for {path}: {e}")
        return []


def pdf_has_any_scanned_pages(path: str, min_chars_per_page: int = 60) -> bool:
    """
    Return True if ANY page in the PDF appears to be scanned / image-only.

    Used at upload time to flag mixed PDFs for deferred OCR at activation.
    Unlike pdf_likely_scanned() which requires the majority of pages to be
    scanned, this returns True even if only a single page is image-only.
    """
    return len(pdf_scanned_page_indices(path, min_chars_per_page)) > 0


def pdf_likely_scanned(path: str, sample_pages: int = 3,
                       min_chars_per_page: int = 60) -> bool:
    """
    Return True when the PDF appears to be FULLY scanned (majority of pages
    are image-only and need OCR); return False when it has selectable text
    we can use directly (the common, fast path).

    Now scans ALL pages (not just the first `sample_pages`) and returns True
    only when more than 80% of pages are scanned. The sample_pages parameter
    is kept for API compatibility but no longer limits the scan range.

    For mixed-PDF detection (some pages scanned, some digital), use
    pdf_has_any_scanned_pages() instead.

    On any error (pdf_backend import failure, corrupt PDF) we return False so
    OCR is NOT triggered — the caller's existing flow handles the failure
    via the legacy parser fallback.
    """
    try:
        from core import pdf_backend as fitz
        with fitz.open(path) as doc:
            total = len(doc)
    except Exception as e:
        logger.warning(f"docling_parser: pdf_likely_scanned check failed for {path}: {e}")
        return False
    if total == 0:
        return False
    scanned = pdf_scanned_page_indices(path, min_chars_per_page)
    # "Fully scanned" = more than 80% of pages are image-only
    return len(scanned) / total > 0.8


def pdf_page_needs_ocr(page, image_area_threshold: float = _PDF_IMAGE_AREA_THRESHOLD) -> bool:
    """
    Determine whether a single PDF page needs the OCR converter by detecting
    embedded image blocks directly — NOT by counting extractable characters.

    Runs two complementary detectors, both keyed on the same >= threshold rule:

      1. Primary — page.get_text("blocks")
         The pdf_backend layout analyzer. Returns every layout element the page
         declares, with a block_type field:
             0 = text block  (native selectable text)
             1 = image block (embedded raster/vector image)
         Catches PDFs from tools that register images as page-level layout
         elements (Docling, LaTeX \\includegraphics, ReportLab, Acrobat
         "Insert Image", most modern authoring pipelines).

      2. Secondary — page.get_images(full=True) + page.get_image_rects(xref)
         Reads the page's /Resources /XObject dictionary directly and walks
         the content stream to find rendered rectangles. Catches PDFs where
         the image is drawn from a content-stream XObject rather than a
         layout block — invisible to the primary detector. Common producers:
         Word "Print to PDF", Windows Photos, Copilot's image-to-PDF, most
         scanner drivers, page.insert_image()-style embedding, img2pdf,
         ImageMagick convert.

    A page needs OCR when either detector finds an image covering
    >= image_area_threshold of the total page area. Correctly handles:

        Case A — 0 native chars + full-page scanned image
                 → detected → True  (OCR)
        Case B — 200 native chars + large scanned figure/table
                 → detected → True  (OCR)
                 Docling's OCR converter will OCR only the image region and
                 merge the result with the existing native text cells via
                 post_process_cells() — native text is never overwritten.
        Case C — 500 native chars + tiny 10px logo
                 → area < threshold → False (text-only, fast path)
        Case D — 0 native chars + full-page image, XObject-embedded (Word,
                 Copilot, scanner drivers, insert_image, img2pdf)
                 → primary misses it, secondary catches it → True (OCR)

    Falls back to the old char-count heuristic (< 60 chars) only when
    get_text("blocks") itself raises an exception (e.g. corrupt page).

    Args:
        page: an open core.pdf_backend Page object
        image_area_threshold: min image_area/page_area ratio to trigger OCR

    Returns:
        True  → route this page to the OCR converter
        False → route this page to the text-only converter
    """
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return False  # degenerate / zero-size page

    # ── GATE 0: native-text check ────────────────────────────────────────────
    # Full-page OCR DISCARDS native text cells (Docling's post_process_cells
    # keeps only from_ocr cells when force_full_page_ocr=True). Doing that to
    # a page that already has good text destroys information, so a page with
    # substantial native text must never be routed to full-page OCR.
    #
    # This is what prevents that class of failure: 168 born-digital Word
    # pages, each carrying banner-shaped diagrams >5% of page area, were all
    # misrouted to full OCR and crashed PaddleOCR — despite every page having
    # 900-4,200 characters of perfectly extractable native text.
    #
    # NOTE: such pages are NOT simply "text". If they carry sizeable images
    # they are classified "hybrid" by page_strategy() below, which OCRs only
    # the image regions and merges the result with the native text. This
    # function answers the narrower question "does this page need FULL-page
    # OCR?" and is kept for backward compatibility.
    try:
        _native = (page.get_text() or "").strip()
        if len(_native) >= _PDF_MIN_NATIVE_CHARS:
            return False
    except Exception:
        # get_text() failed — fall through to image detection below, which
        # has its own exception handling. Never fail classification here.
        pass

    try:
        blocks = page.get_text("blocks")
        # Each block tuple: (x0, y0, x1, y1, content, block_no, block_type)
        # block_type == 1 → image block
        for block in blocks:
            if block[6] == 1:  # image block
                x0, y0, x1, y1 = block[0], block[1], block[2], block[3]
                img_area = (x1 - x0) * (y1 - y0)
                if img_area > 0 and (img_area / page_area) >= image_area_threshold:
                    return True
    except Exception:
        # get_text("blocks") failed (corrupt page, encrypted PDF, etc.)
        # Fall back to the original char-count heuristic so we never silently
        # skip OCR on a page that genuinely needs it.
        try:
            txt = page.get_text() or ""
            return len(txt.strip()) < 60
        except Exception:
            return False

    # Secondary check — XObject-embedded scans.
    #
    # Some PDFs embed a page-sized raster as an XObject drawn from the page
    # content stream rather than as a top-level image block. Common producers:
    # Microsoft Word "Print to PDF", Windows Photos, Copilot's image-to-PDF,
    # many scanner drivers, page.insert_image()-style embedding, img2pdf, and
    # ImageMagick's convert. These never appear in get_text("blocks") with
    # block_type == 1, so the loop above finds nothing and we'd wrongly route
    # the page to the text-only converter (classifying it as "text" or
    # "blank"). Consult page.get_images() as the ground truth for embedded
    # images — if any single image covers >= image_area_threshold of the
    # page, treat the page as needing OCR. The same 5% threshold is applied
    # here, so small logos / headers / page-number graphics do NOT trigger.
    try:
        for im in page.get_images(full=True):
            xref = im[0]
            for r in page.get_image_rects(xref):
                w = r.x1 - r.x0
                h = r.y1 - r.y0
                img_area = w * h
                if img_area > 0 and (img_area / page_area) >= image_area_threshold:
                    return True
    except Exception:
        # get_images / get_image_rects unavailable on exotic PDFs —
        # don't fail classification, just skip.
        pass

    return False


def pdf_boilerplate_xrefs(doc) -> set:
    """
    Identify images that are page furniture rather than content.

    Returns the set of xrefs whose byte content appears on more than
    _PDF_BOILERPLATE_IMAGE_RATIO of the document's pages — logos, header rules,
    footer marks, watermarks. These carry no per-page information, so OCR-ing
    them once per page is pure cost.

    Images are grouped by a hash of their decoded bytes, not by xref, because
    the same logo is often stored under a different xref on each page.

    Returns an empty set on any failure or for very short documents (< 3
    pages), where "appears on most pages" is not a meaningful signal.
    """
    import hashlib
    from collections import defaultdict

    try:
        n_pages = len(doc)
    except Exception:
        return set()
    if n_pages < 3:
        return set()

    hash_pages: dict = defaultdict(set)   # content hash -> set of page indices
    hash_xrefs: dict = defaultdict(set)   # content hash -> set of xrefs
    try:
        for i in range(n_pages):
            for im in doc[i].get_images(full=True):
                xref = im[0]
                try:
                    blob = doc.extract_image(xref)["image"]
                except Exception:
                    continue
                # Non-security use: dedup key for identical embedded images, not a
                # cryptographic protection of sensitive data — sha256 avoids the
                # static-analysis false positive while keeping the same purpose.
                h = hashlib.sha256(blob).hexdigest()
                hash_pages[h].add(i)
                hash_xrefs[h].add(xref)
    except Exception as e:
        logger.debug(f"docling_parser: boilerplate detection failed: {e}")
        return set()

    boiler: set = set()
    for h, pages in hash_pages.items():
        if len(pages) / n_pages > _PDF_BOILERPLATE_IMAGE_RATIO:
            boiler |= hash_xrefs[h]

    if boiler:
        logger.info(
            f"docling_parser: suppressing {len(boiler)} boilerplate image xref(s) "
            f"(appear on >{_PDF_BOILERPLATE_IMAGE_RATIO:.0%} of {n_pages} pages) "
            f"— headers/logos/watermarks will not trigger hybrid OCR"
        )
    return boiler


def pdf_page_largest_image_ratio(page, skip_xrefs: Optional[set] = None) -> float:
    """
    Return the largest single embedded-image area on `page` as a fraction of
    total page area (0.0 when there are no images or detection fails).

    skip_xrefs: xrefs identified as boilerplate by pdf_boilerplate_xrefs().
    Those images are excluded so a logo repeated on every page cannot make the
    whole document hybrid.

    Uses get_images(full=True) + get_image_rects() so each rectangle can be
    attributed to an xref and filtered. The get_text("blocks") detector is
    also consulted, but only when no skip list is in play — block tuples carry
    no xref, so they cannot be filtered and would reintroduce the boilerplate.

    We deliberately use the LARGEST SINGLE image rather than combined
    coverage: overlapping placements can sum well past 100% of the page
    (measured 271% on a newsletter page with 12 stacked graphics), which
    makes a combined figure useless as a threshold.
    """
    try:
        page_area = page.rect.width * page.rect.height
        if page_area <= 0:
            return 0.0
    except Exception:
        return 0.0

    skip_xrefs = skip_xrefs or set()
    largest = 0.0

    if not skip_xrefs:
        try:
            for block in page.get_text("blocks"):
                if block[6] == 1:  # image block
                    a = (block[2] - block[0]) * (block[3] - block[1])
                    if a > 0:
                        largest = max(largest, a / page_area)
        except Exception:
            pass

    try:
        for im in page.get_images(full=True):
            xref = im[0]
            if xref in skip_xrefs:
                continue
            for r in page.get_image_rects(xref):
                a = (r.x1 - r.x0) * (r.y1 - r.y0)
                if a > 0:
                    largest = max(largest, a / page_area)
    except Exception:
        pass

    return largest


def page_strategy(page, skip_xrefs: Optional[set] = None) -> str:
    """
    Classify a single page as "ocr" | "hybrid" | "text" | "blank".

    skip_xrefs: boilerplate image xrefs (logos/headers) to ignore when judging
    whether the page carries a content-bearing image.

    Decision order:

      1. "ocr"    — pdf_page_needs_ocr() is True.
                    Little/no native text plus a page-dominant image, i.e. a
                    genuine scan. Full-page OCR converter; native text (there
                    is effectively none) is discarded, which is correct here.

      2. "blank"  — no native text and no significant image.
                    Nothing to convert; a page marker is emitted and no
                    converter call is made.

      3. "hybrid" — has native text AND its largest image covers at least
                    _PDF_HYBRID_MIN_IMAGE_AREA of the page.
                    Region-OCR converter: only the image rectangles are sent
                    to PaddleOCR and results merge with (never overwrite) the
                    native text. This is what recovers flowchart labels,
                    infographic-card headings and chart annotations that the
                    text-only converter cannot see at all.

      4. "text"   — has native text and no significant image.
                    Fast text-only converter, no OCR involved.

    Set PDF_HYBRID_ENABLED=0 to collapse case 3 into case 4 (faster, but text
    baked into images is lost).
    """
    if pdf_page_needs_ocr(page):
        return "ocr"

    try:
        has_text = bool((page.get_text() or "").strip())
    except Exception:
        has_text = True   # safest default: treat as text, never drop the page

    largest_img = pdf_page_largest_image_ratio(page, skip_xrefs=skip_xrefs)

    if not has_text:
        # No text at all. If there is any meaningful image, OCR is the only
        # way to get content off this page; otherwise it is genuinely blank.
        return "ocr" if largest_img >= _PDF_HYBRID_MIN_IMAGE_AREA else "blank"

    if _PDF_HYBRID_ENABLED and largest_img >= _PDF_HYBRID_MIN_IMAGE_AREA:
        return "hybrid"

    return "text"


def pdf_page_strategies(path: str) -> list:
    """
    Classify every page in the PDF into a processing strategy.

    Returns a list of (page_index_0based, strategy) tuples, one per page:

        "ocr"    — little/no native text plus a page-dominant image: a genuine
                   scan. Full-page OCR converter.
        "hybrid" — native text AND a significant image (flowchart, infographic
                   card, annotated chart). Region-OCR converter: only the image
                   rectangles are OCR'd and merged with the native text, which
                   is never overwritten.
        "text"   — native text, no significant image. Fast text-only converter.
        "blank"  — no text and no significant image. No converter call; a page
                   marker is emitted so page numbering stays correct.

    Large-document guard: when PDF_HYBRID_MAX_PAGES is set (> 0) and the
    document exceeds it, hybrid pages are downgraded to "text" and a WARNING
    names how many pages were affected. Hybrid OCR costs seconds per page, so
    a very long diagram-heavy manual could otherwise run for hours.

    Returns [] on any failure — caller falls back to the legacy _pick_converter()
    path so the document is never silently dropped.
    """
    try:
        from core import pdf_backend as fitz
    except ImportError:
        return []

    strategies = []
    try:
        doc = fitz.open(path)
    except Exception as e:
        logger.error(f"docling_parser: pdf_page_strategies cannot open '{path}': {e}")
        return []

    try:
        # Identify logos / headers / watermarks once for the whole document so
        # repeated page furniture cannot make every page hybrid.
        _boiler = pdf_boilerplate_xrefs(doc) if _PDF_HYBRID_ENABLED else set()
        for i in range(len(doc)):
            try:
                strategy = page_strategy(doc[i], skip_xrefs=_boiler)
            except Exception as _pe:
                # Never let one bad page abort classification — "text" is the
                # safe default because the text converter cannot crash on it.
                logger.warning(
                    f"docling_parser: page_strategy failed for page {i + 1} "
                    f"of '{path}': {_pe} — defaulting to text"
                )
                strategy = "text"
            strategies.append((i, strategy))
    finally:
        doc.close()

    total = len(strategies)

    # ── Large-document guard ────────────────────────────────────────────────
    if _PDF_HYBRID_MAX_PAGES > 0 and total > _PDF_HYBRID_MAX_PAGES:
        _downgraded = sum(1 for _, s in strategies if s == "hybrid")
        if _downgraded:
            strategies = [
                (i, "text" if s == "hybrid" else s) for i, s in strategies
            ]
            logger.warning(
                f"docling_parser: '{path}' has {total} pages (> "
                f"PDF_HYBRID_MAX_PAGES={_PDF_HYBRID_MAX_PAGES}) — downgraded "
                f"{_downgraded} hybrid page(s) to text. Text embedded in images "
                f"on those pages will NOT be extracted. Raise PDF_HYBRID_MAX_PAGES "
                f"to process them."
            )

    ocr_c    = sum(1 for _, s in strategies if s == "ocr")
    hybrid_c = sum(1 for _, s in strategies if s == "hybrid")
    text_c   = sum(1 for _, s in strategies if s == "text")
    blank_c  = sum(1 for _, s in strategies if s == "blank")
    logger.info(
        f"docling_parser: pdf_page_strategies '{path}' → "
        f"{text_c} text / {hybrid_c} hybrid / {ocr_c} ocr / {blank_c} blank  "
        f"(total {total} pages)"
    )
    return strategies


def _is_legacy_ds4sd_snapshot(root: str) -> bool:
    """
    Return True when `root` matches the layout of the legacy
    ds4sd/docling-models HuggingFace repo, i.e. it contains
    `model_artifacts/layout/model.safetensors` and
    `model_artifacts/tableformer/{accurate,fast}/...`.

    This snapshot ships well via internal mirrors but Docling 2.x expects
    each model in its own `<owner>--<repo>` subfolder. We use this signal
    to decide whether a one-shot staging-directory rewrite is needed.
    """
    layout_st = os.path.join(root, "model_artifacts", "layout", "model.safetensors")
    tf_fast   = os.path.join(root, "model_artifacts", "tableformer", "fast",
                             "tableformer_fast.safetensors")
    return os.path.isfile(layout_st) and os.path.isfile(tf_fast)


def _prepare_legacy_staging(root: str) -> str:
    """
    Build (once) a Docling 2.x-compatible artifacts tree alongside the
    legacy snapshot. We materialise it as a sibling directory:

        <root>/                                          (legacy snapshot)
        <root>/_docling2_layout/                         (staging — created here)
            docling-project--docling-layout-old/         -> mirrors layout/
            docling-project--docling-models/             -> mirrors snapshot root

    Each entry is a directory junction (Windows) or symlink (POSIX) so we
    never duplicate the multi-hundred-megabyte safetensors files. If linking
    isn't permitted, we fall back to a directory copy of just the config
    files plus an in-place link of the safetensors.

    Returns the staging path that should be passed as `artifacts_path` to
    PdfPipelineOptions.
    """
    staging = os.path.join(root, "_docling2_layout")
    layout_target  = os.path.join(staging, "docling-project--docling-layout-old")
    tf_target_root = os.path.join(staging, "docling-project--docling-models")

    # Re-use an existing staging dir when both expected subpaths are intact.
    if (os.path.isfile(os.path.join(layout_target, "model.safetensors"))
            and os.path.isdir(os.path.join(tf_target_root, "model_artifacts", "tableformer"))):
        return staging

    os.makedirs(staging, exist_ok=True)

    def _link(src: str, dst: str) -> None:
        """Create a directory junction/symlink, fall back to copy on failure."""
        if os.path.exists(dst):
            return
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            # On Windows os.symlink for a directory requires admin or developer
            # mode — fall back to a junction via CMD's mklink /J which any user
            # can create.
            if os.name == "nt":
                import subprocess
                subprocess.check_call(
                    ["cmd", "/c", "mklink", "/J", dst, src],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                os.symlink(src, dst, target_is_directory=True)
        except Exception:
            # Last-resort copy. Slower but guaranteed to work.
            import shutil
            shutil.copytree(src, dst)

    layout_src = os.path.join(root, "model_artifacts", "layout")
    _link(layout_src, layout_target)
    _link(root, tf_target_root)
    return staging


def _build_converter(enable_ocr: bool = False, ocr_mode: str = "full"):
    """
    Construct DocumentConverter with PDF pipeline options pointed at the
    local artifacts directory when DOCLING_ARTIFACTS_PATH is set.

    enable_ocr controls whether the OCR stage is wired up:
      - False (default): fast path for born-digital PDFs. ~2-5x faster.
      - True: build with PaddleOCR (PP-OCRv4) pointed at PADDLEOCR_MODELS_PATH
        so scanned PDFs are run through detection+recognition before the layout
        model. The OCR converter is only built when a scanned PDF is encountered;
        see _get_ocr_converter() for the lazy init.

    ocr_mode selects HOW OCR is applied when enable_ocr=True:

      "full"   — force_full_page_ocr=True. The whole page is rasterized and
                 OCR'd, and Docling DISCARDS native PDF text cells in favour
                 of OCR cells (see BaseOcrModel.post_process_cells). Correct
                 for genuinely scanned pages where native text is absent or
                 unreliable. This is the historical behaviour.

      "region" — force_full_page_ocr=False with a low bitmap_area_threshold.
                 Docling's get_ocr_rects() returns only the individual image
                 rectangles on the page, OCR runs on those crops alone, and
                 _filter_ocr_cells() drops any OCR cell overlapping existing
                 native text. Native text therefore always wins and OCR only
                 fills the gaps where pictures are.

                 This is what recovers text baked into flowcharts, infographic
                 cards and chart labels on pages that ALSO have real text —
                 content that both the text-only converter (no OCR at all) and
                 the "full" converter (native text discarded) handle poorly.

                 It is also far cheaper: only the image crops are shipped to
                 PaddleOCR rather than a full-page raster (measured ~70% less
                 data on a 4-page newsletter).

    When the local snapshot is the legacy ds4sd/docling-models layout, we
    transparently switch to DOCLING_LAYOUT_V2 (which maps to the matching
    `docling-project--docling-layout-old` model) and stage a sibling tree
    so Docling 2.x's directory-naming expectations are satisfied without
    asking operators to restructure their model mirror.
    """
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions, LayoutOptions,
        TableStructureOptions, TableFormerMode,
    )
    from docling.datamodel.layout_model_specs import DOCLING_LAYOUT_V2

    artifacts = _resolve_artifacts_path()
    if not artifacts:
        # No local path configured — let Docling resolve models its default way
        # (cached on disk after the first HuggingFace download).
        logger.info("docling_parser: DocumentConverter initialized (default HF cache)")
        return DocumentConverter()

    # Detect snapshot shape and stage if needed.
    if _is_legacy_ds4sd_snapshot(artifacts):
        artifacts_for_docling = _prepare_legacy_staging(artifacts)
        layout_options = LayoutOptions(model_spec=DOCLING_LAYOUT_V2)
        logger.info(
            f"docling_parser: legacy ds4sd snapshot detected; using "
            f"DOCLING_LAYOUT_V2 with staging at {artifacts_for_docling!r}"
        )
    else:
        artifacts_for_docling = artifacts
        layout_options = LayoutOptions()  # default (Heron)

    # Assemble OCR options using PaddleOCR (PP-OCRv4).
    # We pin language to English and configure local model dirs when
    # PADDLEOCR_MODELS_PATH is set (air-gapped deployment).
    # When PADDLEOCR_MODELS_PATH is unset, PaddleOCR auto-downloads models
    # from its CDN on first use (suitable for internet-connected environments).
    ocr_options = None
    if enable_ocr:
        # Force PaddleOCR to run inside a throwaway subprocess (see
        # core/paddle_ocr_subprocess.py). This is required because
        # paddlepaddle 2.6.x accumulates corrupt native state inside a
        # long-running process and eventually raises
        # "could not execute a primitive". The subprocess isolation is
        # now proven on production PDFs (Technology.pdf, 135 pages, 84
        # OCR pages, zero crashes). Setting it here means deployments do
        # not need to remember the env var.
        os.environ["PADDLE_OCR_ISOLATE"] = "1"

        from core.paddle_ocr_model import PaddleOcrOptions, register_paddle_ocr
        # Register PaddleOcrModel into Docling's OCR factory (idempotent).
        register_paddle_ocr()
        paddle_dir = _resolve_paddleocr_path()
        # use_angle_cls=False: AiNxt documents are always in portrait/landscape
        # orientation — never rotated 90/180/270°. Disabling angle classification
        # skips PaddleOCR's `predict_cls` sub-model, which was throwing
        # RuntimeError: could not execute a primitive on certain box shapes
        # produced by the detector on pages 58, 59, 63, etc. Detection +
        # recognition alone are stable.
        # force_full_page_ocr — set per ocr_mode:
        #
        # mode="full"  (scanned pages, strategy="ocr")
        #   True: bypass Docling's `get_ocr_rects()` region detection and pass
        #   the entire page image to PaddleOCR.
        #   Why: scripts/diagnose_paddleocr_crash.py proved that PaddleOCR
        #   handles full-page images (1786×2526 at scale=3) reliably — the
        #   "could not execute a primitive" crashes occurred on small cropped
        #   regions (~939×1317) that Docling's region-detector produced.
        #   Note this also makes Docling DISCARD native text cells
        #   (post_process_cells keeps only from_ocr cells), which is correct
        #   for a scan but destructive on a page that has real text.
        #
        # mode="region" (mixed pages, strategy="hybrid")
        #   False: Docling calls get_ocr_rects() and returns just the bitmap
        #   rectangles whose coverage exceeds bitmap_area_threshold. Only those
        #   crops go to PaddleOCR, and _filter_ocr_cells() drops any OCR cell
        #   overlapping an existing native text cell — so native text always
        #   wins and OCR only fills picture regions. This is what recovers
        #   flowchart labels and infographic-card headings on pages that also
        #   contain ordinary prose.
        _force_full = (ocr_mode != "region")
        _bitmap_thresh = (
            _PDF_HYBRID_BITMAP_THRESHOLD if ocr_mode == "region" else 0.05
        )
        #
        # use_angle_cls=False: AiNxt documents are always in portrait/landscape
        # orientation — never rotated 90/180/270°. Disabling angle classification
        # skips PaddleOCR's `predict_cls` sub-model (a secondary crash surface).
        _ocr_kwargs = dict(
            lang=["en"],
            use_gpu=False,
            use_angle_cls=False,
            force_full_page_ocr=_force_full,
            confidence_threshold=0.5,
            show_log=False,
            det_model_dir=os.path.join(paddle_dir, "det") if paddle_dir else None,
            rec_model_dir=os.path.join(paddle_dir, "rec") if paddle_dir else None,
            cls_model_dir=os.path.join(paddle_dir, "cls") if paddle_dir else None,
        )
        # bitmap_area_threshold lives on Docling's OcrOptions base class. Guard
        # with a try/except so a Docling version without the field cannot break
        # converter construction — region mode then simply uses the default.
        try:
            ocr_options = PaddleOcrOptions(
                bitmap_area_threshold=_bitmap_thresh, **_ocr_kwargs
            )
        except TypeError:
            logger.warning(
                "docling_parser: PaddleOcrOptions does not accept "
                "bitmap_area_threshold — region mode will use Docling's default"
            )
            ocr_options = PaddleOcrOptions(**_ocr_kwargs)
        logger.info(
            f"docling_parser: PaddleOcrOptions configured mode={ocr_mode} "
            f"force_full_page_ocr={_force_full} bitmap_thresh={_bitmap_thresh} "
            f"({'offline: ' + paddle_dir if paddle_dir else 'online: auto-download'})"
        )

    # TableFormer mode: "fast" uses ~half the memory of "accurate" and is
    # well-suited to the kind of grid/list tables we see in AiNxt release
    # notes + policy documents. We saw 32-bit address-space exhaustion
    # (std::bad_alloc on every page from ~page 31 of a 56-page handbook
    # with embedded images) when running "accurate" on large PDFs.
    # Quality-wise "fast" still recovers row/column structure for
    # straightforward grids; complex merged-cell tables degrade slightly
    # but never silently drop pages.
    table_opts = TableStructureOptions(mode=TableFormerMode.FAST)

    pdf_opts = PdfPipelineOptions(
        artifacts_path=artifacts_for_docling,
        layout_options=layout_options,
        table_structure_options=table_opts,
        do_ocr=bool(ocr_options),
        ocr_options=ocr_options if ocr_options else PdfPipelineOptions().ocr_options,
        # allow_external_plugins=True is REQUIRED so Docling's OCR factory
        # accepts PaddleOcrModel (which lives in core.paddle_ocr_model, not
        # in a docling.* module). Without this flag the factory silently
        # ignores our registered model and falls back to the default OCR engine.
        allow_external_plugins=True,
        # Explicitly do not retain rendered page images / picture images.
        # Defaults are already False but pin them so a future Docling upgrade
        # that flips defaults doesn't silently 10x our memory footprint.
        generate_page_images=False,
        generate_picture_images=False,
        # images_scale controls the rasterization scale applied to each PDF
        # page before the layout model looks at it. Default 1.0 (~144 DPI)
        # produces large numpy arrays for image-heavy pages — that's the
        # immediate trigger for `std::bad_alloc` in preprocess. 0.5 (~72 DPI)
        # keeps the layout model accuracy on text-dominant policy documents
        # and approximately quarters the per-page memory cost.
        images_scale=0.5,
    )
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_opts),
        }
    )
    logger.info(
        f"docling_parser: DocumentConverter initialized (offline mode, "
        f"artifacts_path={artifacts_for_docling!r}, "
        f"ocr={'PaddleOCR enabled' if ocr_options else 'disabled'})"
    )
    return converter


def _get_converter():
    """
    Return the cached text-only Docling DocumentConverter (do_ocr=False).
    Used for born-digital PDFs and all DOCX / HTML / PPTX uploads — the fast
    path. None when Docling is not installed or model init failed.
    """
    global _converter, _converter_init_failed
    if _converter is not None:
        return _converter
    if _converter_init_failed:
        return None
    try:
        _converter = _build_converter(enable_ocr=False)
        return _converter
    except ImportError:
        _converter_init_failed = True
        logger.warning(
            "docling_parser: 'docling' package not installed — "
            "USE_DOCLING_PARSER flag has no effect. "
            "Install with: pip install docling"
        )
        return None
    except Exception as e:
        _converter_init_failed = True
        logger.error(f"docling_parser: failed to init DocumentConverter: {e}")
        return None


def _get_ocr_converter():
    """
    Return the cached OCR-enabled Docling DocumentConverter (do_ocr=True
    with PaddleOCR PP-OCRv4).

    Built lazily on the first scanned PDF to avoid paying the PaddleOCR
    warm-up cost in deployments that never see a scan. Subsequent scanned
    uploads reuse the same instance.

    Returns None when Docling is not installed or PaddleOCR init fails.
    When PADDLEOCR_MODELS_PATH is unset, PaddleOCR will auto-download models
    from its CDN on first use (acceptable for internet-connected environments).
    """
    global _converter_ocr, _converter_ocr_init_failed
    if _converter_ocr is not None:
        return _converter_ocr
    if _converter_ocr_init_failed:
        return None
    try:
        _converter_ocr = _build_converter(enable_ocr=True, ocr_mode="full")
        return _converter_ocr
    except Exception as e:
        _converter_ocr_init_failed = True
        logger.error(f"docling_parser: failed to init OCR converter: {e}")
        return None


def _get_hybrid_converter():
    """
    Return the cached region-OCR Docling DocumentConverter.

    Used for "hybrid" pages — pages that have BOTH native text and images
    large enough to plausibly contain text (flowcharts, infographic cards,
    chart labels, diagram annotations).

    Differs from the full-OCR converter in one crucial way:
    force_full_page_ocr=False. That makes Docling
      * call get_ocr_rects() and OCR only the bitmap rectangles, and
      * run _filter_ocr_cells(), which drops any OCR cell overlapping an
        existing native text cell.
    Native text therefore always wins; OCR only fills the picture regions.

    Built lazily on the first hybrid page so deployments that never see one
    pay no warm-up cost. Returns None when Docling or PaddleOCR init fails —
    callers must degrade to the text-only converter in that case.
    """
    global _converter_hybrid, _converter_hybrid_init_failed
    if _converter_hybrid is not None:
        return _converter_hybrid
    if _converter_hybrid_init_failed:
        return None
    try:
        _converter_hybrid = _build_converter(enable_ocr=True, ocr_mode="region")
        return _converter_hybrid
    except Exception as e:
        _converter_hybrid_init_failed = True
        logger.error(f"docling_parser: failed to init hybrid region-OCR converter: {e}")
        return None


# ---- Conversion entry point ----------------------------------------------

def _pick_converter(path: str, file_type: str):
    """
    Choose between the text-only and the OCR-enabled converter for this file.

    Routing rule:
      - Non-PDF formats (docx/html/pptx): always text-only converter.
      - PDF with all selectable text: text-only converter (the fast path).
      - PDF that is fully scanned (>80% pages image-only): OCR converter.
      - PDF that is mixed (some pages scanned, some digital): both converters
        are returned so _convert_mixed_pdf() can route each page range correctly.

    Returns (converter_or_tuple, mode, scanned_pages) where:
      mode = "text"  → converter is a single text-only DocumentConverter
      mode = "ocr"   → converter is a single OCR DocumentConverter (fully scanned)
      mode = "mixed" → converter is (ocr_converter, text_converter) tuple
      scanned_pages  → list of 0-indexed page numbers that are image-only
    """
    ft = (file_type or "").lower().strip(".")
    if ft != "pdf":
        logger.debug(f"docling_parser: _pick_converter '{path}' ext={ft} → text-only (non-PDF)")
        return _get_converter(), "text", []

    scanned_pages = pdf_scanned_page_indices(path)

    if not scanned_pages:
        logger.info(
            f"docling_parser: _pick_converter '{path}' → text-only "
            f"(born-digital, no scanned pages detected)"
        )
        return _get_converter(), "text", []

    # Determine if fully scanned or mixed
    try:
        from core import pdf_backend as fitz
        with fitz.open(path) as doc:
            total = len(doc)
    except Exception:
        total = len(scanned_pages)

    is_fully_scanned = len(scanned_pages) / max(total, 1) > 0.8
    ocr_conv = _get_ocr_converter()

    if ocr_conv is None:
        logger.warning(
            f"docling_parser: '{path}' has {len(scanned_pages)} scanned page(s) "
            "but OCR converter unavailable (PaddleOCR init failed). "
            "Falling back to text-only conversion."
        )
        return _get_converter(), "text", scanned_pages

    if is_fully_scanned:
        logger.info(
            f"docling_parser: '{path}' is fully scanned "
            f"({len(scanned_pages)}/{total} pages) → PaddleOCR mode"
        )
        return ocr_conv, "ocr", scanned_pages
    else:
        logger.info(
            f"docling_parser: '{path}' is mixed PDF — "
            f"{len(scanned_pages)}/{total} scanned pages: "
            f"{scanned_pages[:10]}{'...' if len(scanned_pages) > 10 else ''}"
        )
        # Return tuple: (ocr_converter, text_converter)
        return (ocr_conv, _get_converter()), "mixed", scanned_pages


# Large-PDF batching:
#   On a 56-page PDF with embedded high-resolution images we observed Docling's
#   preprocess stage failing with `std::bad_alloc` from page ~31 onwards. The
#   conversion silently returned only the first 30 pages of markdown (about
#   half the document) without raising.
#
#   Splitting the conversion into smaller page batches keeps each call's peak
#   memory bounded. Batch size is intentionally small because the failure
#   mode is rasterizing each page to a numpy array for the layout model —
#   one image-heavy page can dominate, so fewer pages per batch wins more
#   than processing them serially within a single Docling call.
_PDF_BATCH_THRESHOLD = 25     # pages — single-call path stays in use below this
_PDF_BATCH_SIZE      = 10     # pages per batch — keeps peak page-render
                              # working set small for image-heavy PDFs



def _pdf_page_count(path: str) -> int:
    """Return PDF page count, 0 on error. Used to decide batching."""
    try:
        from core import pdf_backend as fitz
        with fitz.open(path) as d:
            return len(d)
    except Exception:
        return 0


def _promote_bold_headings(md: str, file_type: str) -> str:
    """
    Post-process Docling's markdown output to promote bold-only lines to
    proper Markdown headings.

    WHY THIS EXISTS:
      Docling's Word backend (WordDocumentBackend) reads Word XML and maps
      standard heading styles (Heading 1/2/3) to # headings. However, many
      real-world documents — especially policy manuals, dispute processing
      guides, and internal SOPs — use bold Normal-style paragraphs as section
      titles instead of proper Word heading styles. Docling does not classify
      these as section_header items, so export_to_markdown() emits them as
      plain text or **bold** markdown, producing 0 headings in the output.

      Without headings:
        - _chunk_document_structured() cannot split on sections
        - _make_section_map() returns [] → section_map is empty
        - Coverage tier falls back to returning only the first 8,000 chars
        - Deep sections (e.g. "Arbitration Time Limits") never reach the LLM

    WHAT IT DOES:
      Scans the markdown line by line. A line is promoted to a ## or ###
      heading when ALL of the following hold:
        1. The entire line content is wrapped in **...** (bold markdown)
        2. The text is ≤ 120 chars (headings are never long sentences)
        3. The text does not end with sentence-ending punctuation (. ! ? , : ;)
           — body sentences end with punctuation; headings typically don't
        4. The document currently has fewer than 3 # headings
           — if Docling already produced headings, we trust its output and
           skip promotion to avoid double-counting

    HEADING LEVEL HEURISTIC:
      Docling sometimes emits font-size hints in its output. When not
      available, we use text length as a proxy:
        - Short bold lines (≤ 60 chars) → ## (major section)
        - Longer bold lines (61–120 chars) → ### (sub-section)

    APPLIES TO:
      All file types, but most impactful for .docx and .pptx where bold
      Normal-style headings are common. PDFs processed via OCR already
      go through DocLayNet layout detection which classifies headings
      correctly — but the guard (< 3 existing headings) prevents any
      interference with well-structured PDFs.
    """
    import re

    if not md:
        return md

    # Count existing # headings — if Docling already produced a reasonable
    # heading structure, trust it and skip promotion entirely.
    existing_headings = len(re.findall(r"^#{1,6}\s", md, re.MULTILINE))
    if existing_headings >= 3:
        return md

    # Pattern: entire line is **text** (bold), text ≤ 120 chars, no trailing punct
    bold_line_re = re.compile(
        r"^\*{2}(?P<text>[^*\n]{1,120}?)\*{2}\s*$"
    )

    out_lines = []
    for line in md.splitlines():
        m = bold_line_re.match(line)
        if m:
            text = m.group("text").strip()
            # Skip if it looks like a sentence (ends with punctuation)
            if text and text[-1] not in ".!?,:;":
                # Short bold lines → major section (##), longer → sub-section (###)
                prefix = "##" if len(text) <= 60 else "###"
                out_lines.append(f"{prefix} {text}")
                continue
        out_lines.append(line)

    promoted = "\n".join(out_lines)

    # Log how many headings were added so ops can verify the promotion worked
    new_headings = len(re.findall(r"^#{1,6}\s", promoted, re.MULTILINE))
    if new_headings > existing_headings:
        logger.info(
            f"docling_parser._promote_bold_headings: "
            f"promoted {new_headings - existing_headings} bold lines → headings "
            f"(file_type={file_type}, was={existing_headings}, now={new_headings})"
        )

    return promoted


def _convert_pdf_batched(converter, path: str, page_count: int) -> Optional[str]:
    """
    Run Docling on `path` in successive page-range slices and concatenate
    the markdown.

    We export each slice independently, then join the strings with a blank
    line separator. Sections that span a slice boundary will appear as two
    sibling headings rather than a single merged section, but downstream
    chunking handles it correctly because each piece is already
    heading-anchored.
    """
    parts: list[str] = []
    failures: list[int] = []
    for start in range(1, page_count + 1, _PDF_BATCH_SIZE):
        end = min(start + _PDF_BATCH_SIZE - 1, page_count)
        try:
            result = converter.convert(path, page_range=(start, end))
            if result is None or getattr(result, "document", None) is None:
                failures.append(start)
                continue
            piece = _promote_bold_headings(result.document.export_to_markdown() or "", ft)
            if piece.strip():
                parts.append(piece.strip())
            logger.debug(
                f"docling_parser: batched convert pages {start}-{end}/"
                f"{page_count} OK ({len(piece)} chars)"
            )
        except Exception as e:
            failures.append(start)
            logger.warning(
                f"docling_parser: batch {start}-{end} failed for {path}: {e}"
            )
    if failures:
        logger.warning(
            f"docling_parser: {len(failures)} batch(es) failed for {path}; "
            f"first-page-of-failed = {failures}"
        )
    if not parts:
        return None
    return "\n\n".join(parts)


def _group_page_strategies(strategies: list) -> list:
    """
    Group consecutive pages that share the same strategy into batches.

    Input:  [(0,"text"),(1,"text"),(2,"ocr"),(3,"ocr"),(4,"text")]
    Output: [([0,1],"text"), ([2,3],"ocr"), ([4],"text")]

    This keeps converter.convert() call count low while preserving per-page
    accuracy at strategy boundaries. A mixed PDF with N strategy-change
    boundaries produces N+1 groups.

    This is the first-stage grouping only. Large groups are split later so
    large PDFs do not force one huge Docling conversion call.

    "blank" pages are intentionally NOT merged with adjacent groups — they
    emit a page marker but trigger no conversion call, so merging them into
    a neighbouring group would cause the converter to process blank pages
    unnecessarily.
    """
    if not strategies:
        return []

    groups = []
    current_pages = [strategies[0][0]]
    current_strategy = strategies[0][1]

    for page_idx, strategy in strategies[1:]:
        if strategy == current_strategy:
            current_pages.append(page_idx)
        else:
            groups.append((current_pages, current_strategy))
            current_pages = [page_idx]
            current_strategy = strategy

    groups.append((current_pages, current_strategy))
    return groups


def _split_large_smart_groups(groups: list) -> list:
    """Split large smart-parser groups into bounded page batches.

    Batch size per strategy:
      text   — _PDF_SMART_BATCH_SIZE (25): cheap, batch generously.
      ocr    — _PDF_SMART_OCR_BATCH_SIZE (1): full-page OCR is expensive and
               a failure loses the whole batch, so keep batches minimal.
      hybrid — _PDF_SMART_HYBRID_BATCH_SIZE (2): only image crops are OCR'd,
               so it is cheaper than full-page OCR but still far dearer than
               text. Small batches also bound the blast radius of a failure.
    """
    split_groups = []
    for page_indices, strategy in groups:
        if strategy == "text":
            batch_size = _PDF_SMART_BATCH_SIZE
        elif strategy == "ocr":
            batch_size = _PDF_SMART_OCR_BATCH_SIZE
        elif strategy == "hybrid":
            batch_size = _PDF_SMART_HYBRID_BATCH_SIZE
        else:
            split_groups.append((page_indices, strategy))
            continue

        for start in range(0, len(page_indices), batch_size):
            split_groups.append((page_indices[start:start + batch_size], strategy))
    return split_groups


def _convert_smart_group(
    text_converter,
    ocr_converter,
    path: str,
    page_indices: list,
    strategy: str,
    hybrid_converter=None,
) -> tuple:
    """Convert one per_page_smart page group and return ordered markdown data.

    Converter is selected by strategy:
      "ocr"    → ocr_converter     (full-page OCR; native text discarded)
      "hybrid" → hybrid_converter  (region OCR; native text preserved)
      "text"   → text_converter    (no OCR at all)
      "blank"  → no conversion; page marker only

    OCR-bearing batches ("ocr" and "hybrid") acquire _OCR_SLOTS before calling
    the converter. Acquiring here — rather than blocking inside the PaddleOCR
    pool — means a waiting thread is not yet holding a rasterized page image,
    which keeps peak memory bounded under concurrency.

    Retries once on failure (after a short pause) before writing an error
    placeholder.  A single retry handles the most common transient causes:
    momentary memory spikes, one-time PaddleOCR native-state corruption, and
    brief I/O hiccups.  If the retry also fails the error placeholder is
    written as before so _convert_per_page_smart() can count failures and
    decide whether to fall back to the legacy parser.
    """
    import time as _time

    start_1 = page_indices[0] + 1
    end_1   = page_indices[-1] + 1
    marker  = f"<!-- page:{start_1} -->"

    if strategy == "blank":
        logger.info(
            f"docling_parser: _convert_per_page_smart — batch SKIP "
            f"pages {start_1}-{end_1} strategy=blank"
        )
        return start_1, marker, False

    if strategy == "ocr":
        converter = ocr_converter
    elif strategy == "hybrid":
        # Degrade to text-only if the hybrid converter failed to initialise —
        # better to lose image text than to fail the page entirely.
        converter = hybrid_converter if hybrid_converter is not None else text_converter
        if hybrid_converter is None:
            logger.warning(
                f"docling_parser: hybrid converter unavailable for pages "
                f"{start_1}-{end_1} — using text-only (image text will be missed)"
            )
    else:
        converter = text_converter

    _needs_ocr_slot = strategy in ("ocr", "hybrid")

    logger.info(
        f"docling_parser: _convert_per_page_smart — batch START "
        f"pages {start_1}-{end_1} strategy={strategy}"
    )

    def _attempt(attempt_num: int) -> tuple:
        """Single conversion attempt. Returns (md, success_bool) or raises."""
        if _needs_ocr_slot:
            # Admission control: bound concurrent OCR conversions. Acquired
            # per attempt (not around the whole retry loop) so the 2 s backoff
            # does not hold a slot another thread could use.
            with _OCR_SLOTS:
                result = converter.convert(path, page_range=(start_1, end_1))
        else:
            result = converter.convert(path, page_range=(start_1, end_1))
        if result is None or getattr(result, "document", None) is None:
            raise ValueError("converter returned None document")
        md = result.document.export_to_markdown(traverse_pictures=True) or ""
        return md, bool(md.strip())

    last_exc: Exception = ValueError("no attempt made")
    for attempt in range(2):   # attempt 0 = first try, attempt 1 = retry
        try:
            md, has_content = _attempt(attempt)
            if has_content:
                logger.info(
                    f"docling_parser: _convert_per_page_smart — "
                    f"pages {start_1}-{end_1} strategy={strategy} "
                    f"→ {len(md):,} chars"
                    + (f" (retry {attempt})" if attempt > 0 else "")
                )
                return start_1, f"{marker}\n\n{md.strip()}", True
            # Converter returned empty output (not an exception) — no point retrying
            logger.info(
                f"docling_parser: _convert_per_page_smart — "
                f"pages {start_1}-{end_1} strategy={strategy} → empty output"
            )
            return start_1, marker, False
        except Exception as e:
            last_exc = e
            if attempt == 0:
                logger.warning(
                    f"docling_parser: _convert_per_page_smart — "
                    f"pages {start_1}-{end_1} strategy={strategy} "
                    f"attempt {attempt} failed for '{path}': {e} — retrying in 2s"
                )
                _time.sleep(2)
            else:
                logger.error(
                    f"docling_parser: _convert_per_page_smart — "
                    f"pages {start_1}-{end_1} strategy={strategy} "
                    f"attempt {attempt} failed for '{path}': {e}",
                    exc_info=True,
                )

    # Both attempts failed — write error placeholder so the caller can count failures
    return (
        start_1,
        f"{marker}\n\n<!-- conversion-error: pages {start_1}-{end_1} ({type(last_exc).__name__}) -->",
        False,
    )


def _convert_per_page_smart(
    text_converter,
    ocr_converter,
    path: str,
    page_strategies: list,
    hybrid_converter=None,
) -> Optional[str]:
    """
    Primary PDF conversion path — replaces _convert_mixed_pdf() for all PDFs.

    Converts consecutive same-strategy page groups with the appropriate
    converter, splits large groups into bounded batches, runs text and
    OCR-bearing batches on separate lanes, prepends <!-- page:N --> markers,
    and joins output in original page order.

    hybrid_converter is the region-OCR converter used for "hybrid" pages
    (native text + significant images). When None, hybrid pages degrade to
    the text-only converter and text embedded in images is not recovered.
    """
    grouped = _group_page_strategies(page_strategies)
    if not grouped:
        return None

    groups = _split_large_smart_groups(grouped)
    page_count = len(page_strategies)

    # ── Split by strategy ────────────────────────────────────────────────────
    # OCR-bearing batches ("ocr" and "hybrid") are scheduled on their own
    # bounded lane; text/blank batches keep the full thread-pool parallelism.
    #
    # History: PaddleOCR originally ran in ONE single-threaded child behind ONE
    # unguarded pipe. Concurrent OCR batches caused a fatal race — a failing
    # thread's _recycle() closed the shared pipe while others were mid-write,
    # cascading into a recycle storm that failed 140 of 168 batches in
    # production.
    #
    # That is now addressed on three levels:
    #   1. _PaddleOcrChild holds a lock across its whole request/response cycle
    #   2. PaddleOcrSubprocessPool runs N independent children (own process,
    #      pipe and state each), so OCR parallelism is real and race-free
    #   3. _OCR_SLOTS (acquired inside _convert_smart_group) caps how many
    #      conversions may be inside OCR at once, matching child-pool capacity
    #
    # OCR batches are therefore dispatched through the pool when there are
    # several of them; the semaphore — not the thread count — governs real
    # concurrency. Text/blank batches are pure Docling CPU work with no shared
    # mutable state and are always safe to parallelise.
    ocr_groups  = [g for g in groups if g[1] in ("ocr", "hybrid")]
    para_groups = [g for g in groups if g[1] not in ("ocr", "hybrid")]

    use_parallel = (
        _PDF_SMART_PARALLEL_ENABLED
        and page_count >= _PDF_SMART_PARALLEL_THRESHOLD
        and _PDF_SMART_MAX_WORKERS > 1
        and len([1 for _, strategy in para_groups if strategy != "blank"]) > 1
    )
    # OCR lane runs concurrently only when the semaphore actually allows more
    # than one at a time; otherwise sequential avoids pointless thread churn.
    ocr_parallel = (
        _PDF_SMART_PARALLEL_ENABLED
        and _PDF_OCR_MAX_CONCURRENCY > 1
        and len(ocr_groups) > 1
    )

    _n_hybrid = len([1 for _, s in groups if s == "hybrid"])
    _n_ocr    = len([1 for _, s in groups if s == "ocr"])
    logger.info(
        f"docling_parser: _convert_per_page_smart — "
        f"pages={page_count} original_groups={len(grouped)} batches={len(groups)} "
        f"text_batches={len(para_groups)} hybrid_batches={_n_hybrid} ocr_batches={_n_ocr} "
        f"text_mode={'parallel(' + str(_PDF_SMART_MAX_WORKERS) + ')' if use_parallel else 'sequential'} "
        f"ocr_mode={'parallel(' + str(_PDF_OCR_MAX_CONCURRENCY) + ')' if ocr_parallel else 'sequential'}"
    )

    def _run_group(page_indices: list, strategy: str) -> tuple:
        """Invoke _convert_smart_group, converting any escaped exception into
        an error-placeholder result so one bad batch never aborts the run."""
        try:
            return _convert_smart_group(
                text_converter, ocr_converter, path, page_indices, strategy,
                hybrid_converter=hybrid_converter,
            )
        except Exception as e:
            start_1 = page_indices[0] + 1
            end_1   = page_indices[-1] + 1
            marker  = f"<!-- page:{start_1} -->"
            logger.error(
                f"docling_parser: _convert_per_page_smart — batch raised "
                f"pages {start_1}-{end_1} strategy={strategy} error='{e}'",
                exc_info=True,
            )
            return (
                start_1,
                f"{marker}\n\n<!-- conversion-error: pages {start_1}-{end_1} ({type(e).__name__}) -->",
                False,
            )

    results: list = []

    # 1) Text / blank batches — parallel when the document is large enough.
    if para_groups:
        if use_parallel:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(
                max_workers=_PDF_SMART_MAX_WORKERS,
                thread_name_prefix="docling-smart",
            ) as pool:
                futures = [
                    pool.submit(_run_group, page_indices, strategy)
                    for page_indices, strategy in para_groups
                ]
                for future in as_completed(futures):
                    results.append(future.result())
        else:
            for page_indices, strategy in para_groups:
                results.append(_run_group(page_indices, strategy))

    # 2) OCR-bearing batches ("ocr" + "hybrid").
    #    Real concurrency is capped by _OCR_SLOTS inside _convert_smart_group,
    #    which matches the PaddleOCR child-pool size. The thread pool here only
    #    provides enough workers to keep those slots saturated.
    if ocr_groups:
        if ocr_parallel:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(
                max_workers=min(_PDF_OCR_MAX_CONCURRENCY, len(ocr_groups)),
                thread_name_prefix="docling-ocr",
            ) as pool:
                futures = [
                    pool.submit(_run_group, page_indices, strategy)
                    for page_indices, strategy in ocr_groups
                ]
                for future in as_completed(futures):
                    results.append(future.result())
        else:
            for page_indices, strategy in ocr_groups:
                results.append(_run_group(page_indices, strategy))

    any_success = any(success for _, _, success in results)
    if use_parallel and para_groups and not any_success:
        logger.warning(
            f"docling_parser: _convert_per_page_smart — parallel produced no "
            f"successful batches for '{path}', retrying everything sequentially"
        )
        results = [
            _run_group(page_indices, strategy)
            for page_indices, strategy in groups
        ]
        any_success = any(success for _, _, success in results)

    results.sort(key=lambda item: item[0])
    parts = [content for _, content, _ in results if content]

    if not any_success:
        logger.error(
            f"docling_parser: _convert_per_page_smart — "
            f"all batches failed for '{path}'"
        )
        return None

    # ── Conversion-error check ────────────────────────────────────────────────
    # If ANY page batch failed (even after retry), raise PageConversionError.
    # This propagates through parse() → _try_docling() → activate_doc() which
    # catches it, returns {"success": False, "error": <message>}, and the
    # kb_worker rolls back the document to PENDING_APPROVAL storing the exact
    # failed page ranges in parse_error — visible to the user in the
    # request/status tab.
    #
    # We do NOT proceed with a partial result — a document with silently
    # missing pages is worse than a document that clearly needs re-processing.
    import re as _re
    _joined = "\n\n".join(parts)
    _error_matches = _re.findall(
        r'<!--\s*conversion-error:\s*pages\s+(\d+)-(\d+)[^>]*-->',
        _joined, _re.IGNORECASE
    )
    if _error_matches:
        # Count the exact number of pages that failed across all error batches
        _failed_page_count = sum(
            int(end) - int(start) + 1 for start, end in _error_matches
        )
        # Show up to 3-4 sample ranges so the message is readable but informative
        _sample_ranges = [
            f"pages {s}-{e}" for s, e in _error_matches[:4]
        ]
        _sample_str = ", ".join(_sample_ranges)
        if len(_error_matches) > 4:
            _sample_str += f" (and {len(_error_matches) - 4} more batch(es))"

        _msg = (
            f"PDF conversion failed: {_failed_page_count} page(s) across "
            f"{len(_error_matches)} batch(es) could not be extracted even after retry "
            f"— {_sample_str}. "
            f"Total failed pages: {_failed_page_count}. "
            f"Please re-upload the document or contact support if the issue persists."
        )
        logger.warning(
            f"docling_parser: _convert_per_page_smart — "
            f"{_failed_page_count} page(s) in {len(_error_matches)} batch(es) failed "
            f"for '{path}' even after retry: {_sample_str} — raising PageConversionError"
        )
        raise PageConversionError(_msg)

    return _joined


def _convert_mixed_pdf(text_converter, ocr_converter, path: str,
                       scanned_pages: list[int]) -> Optional[str]:
    """
    Handle mixed PDFs: text-only converter on born-digital pages,
    PaddleOCR converter on scanned pages, results merged in page order.

    Strategy:
      1. Build contiguous ranges of born-digital pages and scanned pages.
      2. Run text-only converter on each born-digital range.
      3. Run OCR converter on each scanned range.
      4. Sort all results by start page and join with blank-line separators.

    Docling's page_range parameter is 1-indexed; scanned_pages is 0-indexed.
    """
    try:
        from core import pdf_backend as fitz
        with fitz.open(path) as doc:
            total_pages = len(doc)
    except Exception as e:
        logger.warning(f"docling_parser: mixed PDF page count failed: {e}")
        return None

    scanned_set = set(scanned_pages)

    def _build_ranges(page_indices: list[int]) -> list[tuple[int, int]]:
        """Convert a list of 0-indexed page numbers into contiguous 1-indexed ranges."""
        if not page_indices:
            return []
        sorted_p = sorted(page_indices)
        ranges: list[tuple[int, int]] = []
        start = end = sorted_p[0]
        for p in sorted_p[1:]:
            if p == end + 1:
                end = p
            else:
                ranges.append((start + 1, end + 1))  # convert to 1-indexed
                start = end = p
        ranges.append((start + 1, end + 1))
        return ranges

    digital_pages = [i for i in range(total_pages) if i not in scanned_set]
    scanned_ranges = _build_ranges(list(scanned_set))
    digital_ranges = _build_ranges(digital_pages)

    logger.info(
        f"docling_parser: _convert_mixed_pdf '{path}' — "
        f"total={total_pages} pages | "
        f"digital ranges (text-only): {digital_ranges} | "
        f"scanned ranges (PaddleOCR): {scanned_ranges}"
    )

    parts: list[tuple[int, str]] = []  # (0-indexed start page, markdown text)

    # Born-digital ranges → text-only converter (fast path)
    for (start_1, end_1) in digital_ranges:
        try:
            result = text_converter.convert(path, page_range=(start_1, end_1))
            if result and getattr(result, "document", None):
                md = result.document.export_to_markdown() or ""
                if md.strip():
                    parts.append((start_1 - 1, md.strip()))
                    logger.debug(
                        f"docling_parser: text pages {start_1}-{end_1} → {len(md)} chars"
                    )
        except Exception as e:
            logger.warning(
                f"docling_parser: text convert pages {start_1}-{end_1} failed for '{path}': {e}"
            )

    # Scanned ranges → PaddleOCR converter
    for (start_1, end_1) in scanned_ranges:
        try:
            result = ocr_converter.convert(path, page_range=(start_1, end_1))
            if result and getattr(result, "document", None):
                md = result.document.export_to_markdown() or ""
                if md.strip():
                    parts.append((start_1 - 1, md.strip()))
                    logger.info(
                        f"docling_parser: PaddleOCR pages {start_1}-{end_1} → {len(md)} chars"
                    )
        except Exception as e:
            logger.warning(
                f"docling_parser: OCR convert pages {start_1}-{end_1} failed for '{path}': {e}"
            )

    if not parts:
        logger.warning(
            f"docling_parser: _convert_mixed_pdf '{path}' — "
            f"all ranges produced empty output"
        )
        return None

    # Sort by page order and join
    parts.sort(key=lambda x: x[0])
    merged = "\n\n".join(text for _, text in parts)
    logger.info(
        f"docling_parser: _convert_mixed_pdf '{path}' — "
        f"merged {len(parts)} range(s) → {len(merged):,} total chars"
    )
    return merged


def parse(path: str, file_type: str) -> Optional[str]:
    """
    Convert a document to Markdown using Docling.

    PRIMARY PATH (PDFs only) — image-aware per-page strategy:
      Uses pdf_page_strategies() to classify every page, then
      _convert_per_page_smart() routes each group of consecutive same-strategy
      pages to the right converter:
        - "ocr"    little/no native text + page-dominant image (a real scan)
                   → full-page OCR converter
        - "hybrid" native text AND a significant image (flowchart, infographic
                   card, annotated chart) → region-OCR converter. Only the
                   image rectangles are OCR'd; Docling's _filter_ocr_cells()
                   drops OCR cells overlapping native text, so native text is
                   never overwritten and image-embedded text is recovered.
        - "text"   native text, no significant image → text-only converter (fast)
        - "blank"  marker emitted, no conversion call

    LEGACY FALLBACK (all formats, and PDFs when primary path fails):
      Falls back to _pick_converter() → _convert_mixed_pdf() /
      _convert_pdf_batched() using the original char-count heuristic.
      DOCX / HTML / PPTX always use this path (text-only converter).

    Page markers (<!-- page:N -->):
      _convert_per_page_smart() prepends a <!-- page:N --> marker to each
      group's output. The chunker in docs_store._chunk_document_structured()
      uses these to populate DocumentEmbedding.page_number for every chunk.

    Returns:
        - Markdown string on success
        - None when Docling is unavailable, the format is unsupported, or
          conversion fails. Callers MUST treat None as "fall back to the
          legacy parser" — never as "empty document".
    """
    if not supports(file_type):
        return None

    import time as _time
    _t0 = _time.perf_counter()

    ft = (file_type or "").lower().strip(".")

    # ── PRIMARY PATH: image-aware per-page strategy (PDFs only) ──────────────
    # Replaces the 60-char threshold heuristic with direct image-block detection.
    # Falls back to the legacy path if strategy detection fails or all batches
    # fail — the document is never silently dropped.
    if ft == "pdf":
        page_strategies = pdf_page_strategies(path)
        if page_strategies:
            text_conv = _get_converter()
            _has_ocr    = any(s == "ocr"    for _, s in page_strategies)
            _has_hybrid = any(s == "hybrid" for _, s in page_strategies)

            # Build each OCR converter only when the document actually needs
            # it — a pure-text PDF never pays the PaddleOCR warm-up cost.
            ocr_conv    = _get_ocr_converter()    if _has_ocr    else None
            hybrid_conv = _get_hybrid_converter() if _has_hybrid else None

            if text_conv is not None:
                # Degrade gracefully when an OCR converter failed to init —
                # text-only output is better than failing the document.
                effective_ocr_conv = ocr_conv if ocr_conv is not None else text_conv
                if _has_ocr and ocr_conv is None:
                    logger.warning(
                        f"docling_parser: full-OCR converter unavailable for '{path}' — "
                        f"scanned pages will use text-only converter "
                        f"(image content may be missed)"
                    )
                if _has_hybrid and hybrid_conv is None:
                    logger.warning(
                        f"docling_parser: region-OCR converter unavailable for '{path}' — "
                        f"hybrid pages will use text-only converter "
                        f"(text embedded in images will be missed)"
                    )

                logger.info(
                    f"docling_parser: parse START '{path}' ext={ft} "
                    f"mode=per_page_smart pages={len(page_strategies)} "
                    f"parallel={_PDF_SMART_PARALLEL_ENABLED} "
                    f"threshold={_PDF_SMART_PARALLEL_THRESHOLD} "
                    f"batch={_PDF_SMART_BATCH_SIZE} workers={_PDF_SMART_MAX_WORKERS} "
                    f"ocr_slots={_PDF_OCR_MAX_CONCURRENCY} "
                    f"hybrid={'on' if _PDF_HYBRID_ENABLED else 'off'}"
                )
                try:
                    md = _convert_per_page_smart(
                        text_conv, effective_ocr_conv, path, page_strategies,
                        hybrid_converter=hybrid_conv,
                    )
                except PageConversionError:
                    # Page batch(es) failed even after retry — re-raise so
                    # _try_docling() → activate_doc() can roll back the document
                    # to PENDING_APPROVAL with the exact failed page ranges.
                    # Do NOT fall through to the legacy parser: the user must
                    # be informed that pages are missing, not silently served
                    # a lower-quality result.
                    raise
                if md is not None:
                    _elapsed = (_time.perf_counter() - _t0) * 1000
                    logger.info(
                        f"docling_parser: parse DONE '{path}' "
                        f"mode=per_page_smart "
                        f"chars={len(md):,} latency={_elapsed:.0f}ms"
                    )
                    return md
                logger.warning(
                    f"docling_parser: per_page_smart returned None for '{path}' "
                    f"— falling back to legacy mode"
                )
            else:
                logger.warning(
                    f"docling_parser: text converter unavailable for '{path}' "
                    f"— falling back to legacy mode"
                )

    # ── LEGACY FALLBACK ───────────────────────────────────────────────────────
    # Reached when:
    #   - ft != "pdf" (DOCX / HTML / PPTX — always text-only, no OCR needed)
    #   - pdf_page_strategies() returned [] (fitz unavailable / corrupt PDF)
    #   - _convert_per_page_smart() returned None (all batches failed with no
    #     content at all — distinct from PageConversionError which is raised
    #     when batches fail after retry and is never caught here)
    #   - text converter failed to initialise
    #
    # NOTE: PageConversionError is NEVER caught here — it propagates up through
    # _try_docling() → activate_doc() so the document is rolled back to
    # PENDING_APPROVAL with the exact failed page ranges in parse_error.
    # Legacy parsing must NOT silently replace a partial Docling result.
    converter, mode, scanned_pages = _pick_converter(path, file_type)
    if converter is None:
        return None

    logger.info(
        f"docling_parser: parse START '{path}' ext={ft} mode={mode}"
        + (f" scanned_pages={scanned_pages}" if scanned_pages else "")
    )

    # Mixed PDF: delegate to the dedicated merge function
    if mode == "mixed" and ft == "pdf":
        ocr_conv, text_conv = converter  # tuple unpacking
        md = _convert_mixed_pdf(text_conv, ocr_conv, path, scanned_pages)
        _elapsed = (_time.perf_counter() - _t0) * 1000
        if md:
            logger.info(
                f"docling_parser: parse DONE '{path}' mode=mixed "
                f"chars={len(md):,} latency={_elapsed:.0f}ms"
            )
        else:
            logger.warning(
                f"docling_parser: parse DONE '{path}' mode=mixed "
                f"result=empty latency={_elapsed:.0f}ms"
            )
        return md

    if mode == "ocr":
        logger.info(
            f"docling_parser: routing '{path}' through PaddleOCR converter "
            f"({len(scanned_pages)} scanned pages)"
        )

    try:
        # Large-PDF page-batched path. Only kicks in for PDFs above the
        # threshold; everything else uses the single-call path so the
        # output is bit-for-bit identical to today's behavior.
        if ft == "pdf":
            pc = _pdf_page_count(path)
            if pc > _PDF_BATCH_THRESHOLD:
                logger.info(
                    f"docling_parser: '{path}' has {pc} pages — using "
                    f"batched conversion ({_PDF_BATCH_SIZE} pages/batch)"
                )
                md = _convert_pdf_batched(converter, path, pc)
                if not md or not md.strip():
                    logger.warning(
                        f"docling_parser: batched convert produced empty markdown for '{path}'"
                    )
                    return None
                _elapsed = (_time.perf_counter() - _t0) * 1000
                logger.info(
                    f"docling_parser: parse DONE '{path}' mode={mode} "
                    f"chars={len(md):,} latency={_elapsed:.0f}ms (batched)"
                )
                return md

        result = converter.convert(path)
        if result is None or getattr(result, "document", None) is None:
            logger.warning(f"docling_parser: empty result for '{path}'")
            return None
        md = result.document.export_to_markdown()
        if not md or not md.strip():
            logger.warning(f"docling_parser: empty markdown for '{path}'")
            return None
        # Post-process: promote bold Normal-style paragraphs to # headings.
        # Handles documents where section titles use bold formatting instead
        # of Word Heading 1/2/3 styles — a common pattern in policy manuals.
        md = _promote_bold_headings(md, ft)
        _elapsed = (_time.perf_counter() - _t0) * 1000
        logger.info(
            f"docling_parser: parse DONE '{path}' mode={mode} "
            f"chars={len(md):,} latency={_elapsed:.0f}ms"
        )
        return md
    except Exception as e:
        logger.warning(
            f"docling_parser: conversion failed for '{path}' ({file_type}): {e} — "
            f"caller will fall back to legacy parser"
        )
        return None


# ---- Diagnostic helpers (used by structure scorer + shadow mode) ----------

def parse_with_meta(path: str, file_type: str) -> Optional[dict]:
    """
    Same as parse() but also returns structural metadata extracted from the
    DoclingDocument: heading count by level, table count, page count.

    Useful for the structure quality scorer (Step 3) and for shadow-mode
    diffing against the legacy parser without re-running conversion.

    Returns:
        {
            "markdown":      "...",
            "heading_count": {1: 1, 2: 12, 3: 4},
            "table_count":   8,
            "page_count":    7,
        }
        or None on failure.
    """
    if not supports(file_type):
        return None

    converter = _get_converter()
    if converter is None:
        return None

    try:
        result = converter.convert(path)
        if result is None or getattr(result, "document", None) is None:
            return None

        doc = result.document
        md = doc.export_to_markdown()
        if not md or not md.strip():
            return None
        md = _promote_bold_headings(md, file_type)

        # Walk DoclingDocument to count structural elements. The schema exposes
        # a flat `texts` list with `label` ∈ {"section_header","paragraph",...}
        # and a `level` attribute on headers. Tables live on `doc.tables`.
        heading_count: dict[int, int] = {}
        for item in getattr(doc, "texts", []) or []:
            label = getattr(item, "label", None)
            if label in ("section_header", "title"):
                lvl = int(getattr(item, "level", 1) or 1)
                heading_count[lvl] = heading_count.get(lvl, 0) + 1

        table_count = len(getattr(doc, "tables", []) or [])
        # `pages` may be dict-like or list-like depending on Docling version.
        pages_attr = getattr(doc, "pages", None)
        try:
            page_count = len(pages_attr) if pages_attr is not None else 0
        except TypeError:
            page_count = 0

        return {
            "markdown":      md,
            "heading_count": heading_count,
            "table_count":   table_count,
            "page_count":    page_count,
        }
    except Exception as e:
        logger.warning(f"docling_parser.parse_with_meta failed for '{path}': {e}")
        return None
