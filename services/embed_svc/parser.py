# SPDX-License-Identifier: MIT
# ============================================================
# EMBED SERVICE — Document Parser wrapper
#
# Wraps core/docling_parser.py so Docling + PaddleOCR run inside
# the embed service process (on the embed server) rather than
# inside the gateway process.
#
# Public API:
#   warm_up()                              → pre-loads Docling models at startup
#   parse(file_bytes, filename, file_type) → str  (markdown, "" on failure)
#   is_ready()                             → bool (True once warm_up succeeded)
#
# Called from:
#   services/embed_svc/main.py  lifespan  → warm_up()
#   services/embed_svc/main.py  POST /parse → parse()
#   services/embed_svc/main.py  GET /health → is_ready()
#
# Design notes:
#   - Delegates entirely to core.docling_parser (no duplication of ML logic).
#   - Uses the same NamedTemporaryFile pattern as docs_router.py so Docling
#     receives a real file path (it cannot parse from bytes directly).
#   - Returns "" (empty string) on any failure — the gateway interprets this
#     as "parse failed, fall back to legacy parser" and never raises.
#   - warm_up() forces the Docling DocumentConverter singleton to initialise
#     (loads DocLayNet + TableFormer weights, ~1-3 s) so the first real
#     upload does not pay the cold-start cost.
# ============================================================

from __future__ import annotations

import os
import tempfile
from typing import Optional

from core.logger import logger

# ── Module-level state ────────────────────────────────────────────────────────
_ready: bool = False   # True after warm_up() succeeds


# ── Public helpers ────────────────────────────────────────────────────────────

def is_ready() -> bool:
    """Return True when Docling models have been successfully loaded."""
    return _ready


def warm_up() -> None:
    """
    Pre-load the Docling DocumentConverter (and optionally the OCR converter)
    so the first real /parse call does not pay the cold-start cost.

    Sets the module-level _ready flag to True on success.
    Logs a warning (never raises) on failure — the /parse endpoint will
    return HTTP 503 until the service is restarted with working model paths.
    """
    global _ready

    # Inject DOCLING_ARTIFACTS_PATH / PADDLEOCR_MODELS_PATH / USE_DOCLING_PARSER
    # from embed-service config into os.environ so core.docling_parser picks
    # them up.  We do this here (not in config.py) to keep the side-effect
    # isolated to the parse subsystem.
    from services.embed_svc.config import (
        USE_DOCLING_PARSER,
        DOCLING_ARTIFACTS_PATH,
        PADDLEOCR_MODELS_PATH,
    )

    if USE_DOCLING_PARSER:
        os.environ.setdefault("USE_DOCLING_PARSER", USE_DOCLING_PARSER)
    if DOCLING_ARTIFACTS_PATH:
        os.environ.setdefault("DOCLING_ARTIFACTS_PATH", DOCLING_ARTIFACTS_PATH)
    if PADDLEOCR_MODELS_PATH:
        os.environ.setdefault("PADDLEOCR_MODELS_PATH", PADDLEOCR_MODELS_PATH)

    try:
        from core import docling_parser as _dp

        if not _dp.is_active():
            logger.warning(
                "parser.warm_up: USE_DOCLING_PARSER is not '1' — "
                "Docling is disabled. Set USE_DOCLING_PARSER=1 in the "
                "embed service .env to enable the /parse endpoint."
            )
            # Still mark ready=False so health check surfaces the misconfiguration.
            return

        # Force the text-only converter to initialise (loads DocLayNet + TableFormer).
        conv = _dp._get_converter()
        if conv is None:
            logger.error(
                "parser.warm_up: Docling DocumentConverter failed to initialise. "
                "Check DOCLING_ARTIFACTS_PATH and that the 'docling' package is installed."
            )
            return

        logger.info("parser.warm_up: Docling text-only converter ready")

        # Pre-load the OCR converter (PaddleOCR PP-OCRv4).
        # This is best-effort — scanned PDFs will still work even if this fails
        # (they fall back to the text-only converter).
        # When PADDLEOCR_MODELS_PATH is set, models load from disk (air-gapped).
        # When unset, PaddleOCR will auto-download models on first use.
        ocr_conv = _dp._get_ocr_converter()
        if ocr_conv is not None:
            logger.info("parser.warm_up: Docling OCR converter ready (PaddleOCR loaded)")
        else:
            logger.warning(
                "parser.warm_up: OCR converter not available — "
                "scanned PDFs will use text-only converter as fallback."
            )

        _ready = True
        logger.info("parser.warm_up: parse service ready ✓")

    except ImportError:
        logger.error(
            "parser.warm_up: 'docling' package not installed. "
            "Install with: pip install docling"
        )
    except Exception as e:
        logger.error(f"parser.warm_up: unexpected error during warm-up: {e}")


def parse(file_bytes: bytes, filename: str, file_type: str) -> str:
    """
    Parse document bytes → markdown string using Docling.

    Writes `file_bytes` to a NamedTemporaryFile, calls
    core.docling_parser.parse(), deletes the temp file, and returns
    the resulting markdown.

    Returns "" (empty string) on any failure — the gateway interprets
    this as "parse failed" and falls back to its legacy parser chain.

    Args:
        file_bytes: Raw file content (PDF, DOCX, HTML, PPTX bytes).
        filename:   Original filename — used only for logging.
        file_type:  Extension without dot: "pdf" | "docx" | "html" | "pptx".

    Returns:
        Markdown string on success, "" on failure.
    """
    ft = (file_type or "").lower().strip(".")

    try:
        from core import docling_parser as _dp
        from core.docling_parser import PageConversionError

        if not _dp.supports(ft):
            logger.debug(f"parser.parse: unsupported file_type '{ft}' — returning empty")
            return ""

        if not _dp.is_active():
            logger.warning(
                f"parser.parse: Docling is not active (USE_DOCLING_PARSER not '1') "
                f"for '{filename}' — returning empty"
            )
            return ""

        # Write bytes to a temp file — Docling requires a real file path.
        # Pattern mirrors docs_router.py lines 149-159 exactly.
        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ft}") as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name

            md = _dp.parse(tmp_path, ft)

        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception as _ue:
                    logger.debug(f"parser.parse: temp file cleanup failed (non-fatal): {_ue}")

        if md and md.strip():
            logger.debug(
                f"parser.parse: '{filename}' ({ft}) → {len(md):,} chars"
            )
            return md

        # Docling returned None or empty — signal fallback to caller.
        logger.debug(
            f"parser.parse: Docling returned empty markdown for '{filename}' ({ft})"
        )
        return ""

    except PageConversionError:
        # One or more page batches failed even after retry.  This message
        # carries the exact failed page ranges and MUST reach the user — it
        # is surfaced in the KB request/status tab via knowledge_docs.parse_error.
        # Returning "" here would collapse it into the generic "parse service
        # returned empty content" message and the page list would be lost.
        # The /parse endpoint converts this into HTTP 422 with the detail intact.
        logger.error(
            f"parser.parse: page conversion failure for '{filename}' ({ft}) "
            f"— propagating to caller as HTTP 422"
        )
        raise

    except Exception as e:
        logger.warning(
            f"parser.parse: failed for '{filename}' ({ft}): {e} — returning empty"
        )
        return ""
