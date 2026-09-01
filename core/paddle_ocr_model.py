# SPDX-License-Identifier: Apache-2.0
"""
PaddleOCR backend for Docling.

Implements BaseOcrModel using the `paddleocr` package (PP-OCRv4 models).
Registered into Docling's OCR factory via register_paddle_ocr() before
_build_converter() is called in core/docling_parser.py.

Usage in docling_parser.py:
    from core.paddle_ocr_model import PaddleOcrOptions, register_paddle_ocr
    register_paddle_ocr()
    ocr_options = PaddleOcrOptions(lang=["en"], use_gpu=False)

Air-gapped deployment:
    Set PADDLEOCR_MODELS_PATH to a directory containing:
        det/  — detection model files
        rec/  — recognition model files
        cls/  — classification model files
    These are passed as det_model_dir / rec_model_dir / cls_model_dir to PaddleOCR.
    Leave PADDLEOCR_MODELS_PATH unset to allow PaddleOCR to auto-download on first use.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar, Literal, Optional, Type

import numpy
from docling_core.types.doc import BoundingBox, CoordOrigin
from docling_core.types.doc.page import BoundingRectangle, TextCell

from docling.datamodel.accelerator_options import AcceleratorOptions
from docling.datamodel.base_models import Page
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import OcrOptions
from docling.datamodel.settings import settings
from docling.models.base_ocr_model import BaseOcrModel
from docling.utils.profiling import TimeRecorder

# Use the platform's structlog-wired logger (same one docling_parser.py uses).
# Fall back to stdlib logging only if core.logger isn't importable (e.g. during
# very early bootstrapping or standalone unit tests).
try:
    from core.logger import logger as _log  # type: ignore
except Exception:  # pragma: no cover
    _log = logging.getLogger(__name__)


# ── Options ───────────────────────────────────────────────────────────────────

class PaddleOcrOptions(OcrOptions):
    """Configuration for PaddleOCR engine (PP-OCRv4).

    All fields have sensible defaults for English OCR.
    """
    kind: ClassVar[Literal["paddleocr"]] = "paddleocr"

    lang: list[str] = ["en"]
    """Language codes for OCR. PaddleOCR uses 'en' for English."""

    use_gpu: bool = False
    """Enable GPU inference. Requires paddlepaddle-gpu package."""

    use_angle_cls: bool = True
    """Enable text direction classification (handles rotated text)."""

    det_db_thresh: float = 0.3
    """Minimum pixel score for text detection binarization."""

    det_db_box_thresh: float = 0.6
    """Minimum box score threshold for text detection."""

    rec_batch_num: int = 6
    """Batch size for text recognition."""

    confidence_threshold: float = 0.5
    """Minimum recognition confidence to include a text cell."""

    show_log: bool = False
    """Enable verbose PaddleOCR logging (noisy — keep False in production)."""

    enable_mkldnn: bool = False
    """Enable Intel OneDNN (MKL-DNN) primitive backend. Default False.

    Why disabled by default:
      paddlepaddle 2.6.x on Linux CPU has a known bug in
      paddle/phi/backends/onednn/onednn_reuse.h where the primitive-
      descriptor cache raises `could not execute a primitive` on
      specific input shapes (observed: 939×1317, 789×1107 RGB) instead
      of falling back to a native kernel. The failure is deterministic
      per shape — retries don't help — and cannot be worked around via
      env vars (FLAGS_use_mkldnn, MKL_CBWR, OMP_NUM_THREADS, etc.).
      Standalone `paddleocr` avoids the code path; Docling's call
      pattern hits it every time.

    Perf impact of disabling: ~30% slower per page (native CPU kernels
    instead of AVX2/AVX-512 OneDNN). Acceptable for our workload —
    OCR runs once at ingestion, not on the hot chat path.

    Re-enable only after:
      1. Upgrading paddlepaddle to ≥3.0 (requires PaddleOCR ≥3.x, API break), OR
      2. Confirming empirically that all shapes we see in production
         are handled by the current paddlepaddle version's OneDNN cache."""

    # Optional local model dirs for air-gapped deployment.
    # When None, PaddleOCR downloads models from its CDN on first use.
    det_model_dir: Optional[str] = None
    """Path to local detection model directory (PP-OCRv4 det)."""

    rec_model_dir: Optional[str] = None
    """Path to local recognition model directory (PP-OCRv4 rec)."""

    cls_model_dir: Optional[str] = None
    """Path to local classification model directory (PP-OCRv4 cls)."""


# ── Model ─────────────────────────────────────────────────────────────────────

class PaddleOcrModel(BaseOcrModel):
    """Docling OCR backend that delegates to the `paddleocr` package (PP-OCRv4).

    Implements Docling's BaseOcrModel interface:
      1. get_ocr_rects(page)  — find bitmap regions needing OCR
      2. get_page_image(...)  — rasterize each region at 3× scale (216 dpi)
      3. reader.ocr(im)       — run PaddleOCR on the numpy array
      4. Build TextCell list  — map polygon boxes back to page coordinates
      5. post_process_cells() — merge with existing cells
    """

    def __init__(
        self,
        *,
        enabled: bool,
        artifacts_path: Optional[Path],
        options: PaddleOcrOptions,
        accelerator_options: AcceleratorOptions,
    ):
        super().__init__(
            enabled=enabled,
            artifacts_path=artifacts_path,
            options=options,
            accelerator_options=accelerator_options,
        )
        self.options: PaddleOcrOptions
        self.scale = 3  # 72 dpi × 3 = 216 dpi — standard Docling OCR rasterization scale

        if self.enabled:
            try:
                from paddleocr import PaddleOCR
            except ImportError:
                raise ImportError(
                    "PaddleOCR is not installed. "
                    "Install with: pip install paddleocr paddlepaddle"
                )

            lang = self.options.lang[0] if self.options.lang else "en"

            # Resolve model dirs:
            #   1. Explicit paths from PaddleOcrOptions (highest priority)
            #   2. Subdirs under artifacts_path (Docling's standard model dir)
            #   3. None → PaddleOCR auto-downloads from CDN on first use
            det_dir = self.options.det_model_dir
            rec_dir = self.options.rec_model_dir
            cls_dir = self.options.cls_model_dir
            if artifacts_path is not None:
                det_dir = det_dir or str(artifacts_path / "det")
                rec_dir = rec_dir or str(artifacts_path / "rec")
                cls_dir = cls_dir or str(artifacts_path / "cls")

            _log.info(
                f"PaddleOcrModel: initializing PaddleOCR(lang={lang!r}, "
                f"use_gpu={self.options.use_gpu}, "
                f"det_model_dir={det_dir!r})"
            )

            # Kwargs captured so the subprocess pool (if enabled) can
            # reconstruct an identical PaddleOCR reader inside the child.
            self._paddle_kwargs = dict(
                use_angle_cls=self.options.use_angle_cls,
                lang=lang,
                use_gpu=self.options.use_gpu,
                det_db_thresh=self.options.det_db_thresh,
                det_db_box_thresh=self.options.det_db_box_thresh,
                rec_batch_num=self.options.rec_batch_num,
                show_log=self.options.show_log,
                enable_mkldnn=self.options.enable_mkldnn,
                det_model_dir=det_dir,
                rec_model_dir=rec_dir,
                cls_model_dir=cls_dir,
            )
            self.reader = PaddleOCR(**self._paddle_kwargs)

            # Subprocess isolation (opt-in via PADDLE_OCR_ISOLATE=1).
            # When enabled, self.reader.ocr(...) calls are routed through
            # a spawned child process (see core/paddle_ocr_subprocess.py)
            # so paddlepaddle's stateful "could not execute a primitive"
            # corruption cannot accumulate across pages within a single
            # gunicorn worker. The in-process self.reader is kept only
            # as a fallback for the case where the pool import fails.
            self._subproc_pool = None
            try:
                from core.paddle_ocr_subprocess import is_enabled as _iso_enabled, get_pool as _iso_pool
                if _iso_enabled():
                    self._subproc_pool = _iso_pool(self._paddle_kwargs)
                    _log.info(
                        "[PaddleOCR][SUBPROC] subprocess isolation ENABLED "
                        f"(PADDLE_OCR_ISOLATE=1) — reader calls run in a pool of "
                        f"{getattr(self._subproc_pool, 'size', 1)} child process(es)"
                    )
                else:
                    _log.info(
                        "[PaddleOCR][SUBPROC] subprocess isolation DISABLED "
                        "(PADDLE_OCR_ISOLATE unset or 0) — using in-process reader"
                    )
            except Exception:
                _log.error(
                    "[PaddleOCR][SUBPROC] failed to init subprocess pool "
                    "— falling back to in-process reader"
                )
                self._subproc_pool = None
            _log.info(
                "PaddleOcrModel: PaddleOCR reader initialized "
                f"(subproc={self._subproc_pool is not None}, "
                f"det_dir={det_dir!r}, rec_dir={rec_dir!r}, cls_dir={cls_dir!r})"
            )

    def __call__(
        self, conv_res: ConversionResult, page_batch: Iterable[Page]
    ) -> Iterable[Page]:
        if not self.enabled:
            yield from page_batch
            return

        import time as _time

        _log.debug("[PaddleOCR] __call__ entered — processing page_batch")

        for page in page_batch:
            assert page._backend is not None
            if not page._backend.is_valid():
                _log.debug(
                    f"[PaddleOCR] page {getattr(page, 'page_no', '?')} backend invalid — skipping"
                )
                yield page
                continue

            page_no = getattr(page, "page_no", "?")

            with TimeRecorder(conv_res, "ocr"):
                _t0 = _time.perf_counter()
                ocr_rects = self.get_ocr_rects(page)
                all_ocr_cells: list[TextCell] = []

                _log.info(
                    f"[PaddleOCR] page {page_no} — "
                    f"{len(ocr_rects)} OCR region(s) detected"
                )

                for rect_idx, ocr_rect in enumerate(ocr_rects):
                    if ocr_rect.area() == 0:
                        _log.debug(
                            f"[PaddleOCR] page {page_no} rect[{rect_idx}] area=0 — skipping"
                        )
                        continue

                    high_res_image = page._backend.get_page_image(
                        scale=self.scale, cropbox=ocr_rect
                    )
                    im = numpy.array(high_res_image)
                    h, w = im.shape[:2]

                    # Shape normalization — pad height & width up to the next
                    # multiple of 32. PaddleOCR's DB detector uses a ResNet
                    # backbone with 5 downsampling stages, so it internally
                    # resizes inputs to /32-aligned tensors. On paddlepaddle
                    # 2.6.x CPU that internal resize trips a kernel-selection
                    # bug on certain non-aligned shapes (observed: 939×1317,
                    # 789×1107 — neither dim divisible by 32), raising
                    # "could not execute a primitive" from predict_det.
                    #
                    # Padding at our layer bypasses the broken code path.
                    # We add white pixels to bottom + right ONLY, so:
                    #   1. Every original pixel keeps its exact (x, y).
                    #   2. Existing coord-remapping math stays correct.
                    #   3. Detector sees no new text-shaped features in
                    #      the added margin.
                    #   4. Recognizer only sees crops from the original
                    #      pixel region — never touches the padding.
                    _ALIGN = 32
                    pad_h = (_ALIGN - h % _ALIGN) % _ALIGN
                    pad_w = (_ALIGN - w % _ALIGN) % _ALIGN
                    if pad_h or pad_w:
                        im = numpy.pad(
                            im,
                            ((0, pad_h), (0, pad_w), (0, 0)),
                            mode="constant",
                            constant_values=255,  # white pad — matches page background
                        )
                        h_padded, w_padded = im.shape[:2]
                    else:
                        h_padded, w_padded = h, w

                    _log.debug(
                        f"[PaddleOCR] page {page_no} rect[{rect_idx}] "
                        f"image={w}×{h}px→{w_padded}×{h_padded}px (pad+{pad_w},+{pad_h}) "
                        f"scale={self.scale}× "
                        f"rect=(l={ocr_rect.l:.1f},t={ocr_rect.t:.1f},"
                        f"r={ocr_rect.r:.1f},b={ocr_rect.b:.1f}) area={ocr_rect.area():.0f}"
                    )

                    # PaddleOCR v2.x API:
                    #   ocr(img, cls=True) → [page_results]
                    #   page_results = [[box_4pts, (text, score)], ...]
                    #   box_4pts = [[x0,y0],[x1,y1],[x2,y2],[x3,y3]]  (polygon, clockwise)
                    #
                    # When subprocess isolation is enabled, route through the
                    # child process to avoid stateful OneDNN corruption.
                    # When disabled (or pool unavailable), fall back to the
                    # in-process reader.
                    try:
                        if self._subproc_pool is not None:
                            raw = self._subproc_pool.ocr(im, self.options.use_angle_cls)
                        else:
                            raw = self.reader.ocr(im, cls=self.options.use_angle_cls)
                    except Exception:
                        _log.error(
                            f"[PaddleOCR] page {page_no} rect[{rect_idx}] "
                            f"self.reader.ocr() FAILED"
                        )
                        raise
                    del high_res_image, im

                    if not raw or raw[0] is None:
                        _log.debug(
                            f"[PaddleOCR] page {page_no} rect[{rect_idx}] — no text detected"
                        )
                        continue

                    page_results = raw[0]  # first (and only) page in the batch
                    cells: list[TextCell] = []
                    skipped_low_conf = 0

                    for ix, line in enumerate(page_results):
                        box_pts, (text, score) = line
                        if score < self.options.confidence_threshold:
                            skipped_low_conf += 1
                            continue

                        # Use top-left corner [0] and bottom-right corner [2]
                        # of the 4-point polygon for an axis-aligned bounding box.
                        # Scale back from 216 dpi → 72 dpi and offset by ocr_rect origin.
                        x0, y0 = box_pts[0]
                        x2, y2 = box_pts[2]

                        cells.append(TextCell(
                            index=ix,
                            text=text,
                            orig=text,
                            from_ocr=True,
                            confidence=float(score),
                            rect=BoundingRectangle.from_bounding_box(
                                BoundingBox.from_tuple(
                                    coord=(
                                        x0 / self.scale + ocr_rect.l,
                                        y0 / self.scale + ocr_rect.t,
                                        x2 / self.scale + ocr_rect.l,
                                        y2 / self.scale + ocr_rect.t,
                                    ),
                                    origin=CoordOrigin.TOPLEFT,
                                )
                            ),
                        ))

                    _log.debug(
                        f"[PaddleOCR] page {page_no} rect[{rect_idx}] — "
                        f"{len(cells)} cell(s) accepted, "
                        f"{skipped_low_conf} skipped (conf<{self.options.confidence_threshold})"
                    )
                    all_ocr_cells.extend(cells)

                # Merge OCR cells with any existing selectable-text cells on the page.
                self.post_process_cells(all_ocr_cells, page)

                _elapsed_ms = (_time.perf_counter() - _t0) * 1000
                _log.info(
                    f"[PaddleOCR] page {page_no} — "
                    f"{len(all_ocr_cells)} total cell(s) extracted, "
                    f"latency={_elapsed_ms:.0f}ms"
                )

            # Optional debug visualisation (controlled by DOCLING_DEBUG_VISUALIZE_OCR).
            if settings.debug.visualize_ocr:
                self.draw_ocr_rects_and_cells(conv_res, page, ocr_rects)

            yield page

    @classmethod
    def get_options_type(cls) -> Type[OcrOptions]:
        return PaddleOcrOptions


# ── Factory registration ──────────────────────────────────────────────────────

_registered = False


def register_paddle_ocr() -> None:
    """Register PaddleOcrModel into Docling's OCR factory.

    Must be called once before _build_converter(enable_ocr=True) in
    core/docling_parser.py. Safe to call multiple times (idempotent).

    The factory is keyed by options type, so after registration:
        PdfPipelineOptions(ocr_options=PaddleOcrOptions(...), allow_external_plugins=True)
    will route scanned pages through PaddleOcrModel.__call__().

    Note: get_ocr_factory() is @lru_cache. We call it with
    allow_external_plugins=True so our non-docling.* module is accepted.
    The same allow_external_plugins=True must be set on PdfPipelineOptions
    when building the DocumentConverter.
    """
    global _registered
    if _registered:
        return

    from docling.models.factories import get_ocr_factory

    factory = get_ocr_factory(allow_external_plugins=True)
    try:
        factory.register(
            PaddleOcrModel,
            plugin_name="ainxt",
            plugin_module_name="core.paddle_ocr_model",
        )
        _log.info("PaddleOcrModel registered in Docling OCR factory (kind='paddleocr')")
    except ValueError:
        # Already registered — idempotent, not an error.
        pass

    # ── Pydantic field-annotation patch ─────────────────────────────────────
    # Runtime introspection on the server proved:
    #   PdfPipelineOptions.model_fields['ocr_options'].annotation is OcrOptions
    #   (the base class, not a discriminated union — discriminator=None).
    # Pydantic v2 revalidates any instance assigned to that field AS OcrOptions,
    # which strips every subclass field (including our `kind`) and returns a
    # bare OcrOptions(). That is why, at converter.convert() time, we observed
    # `pipeline_ocr_options_type=None, kind=None` even though we constructed
    # PdfPipelineOptions(ocr_options=PaddleOcrOptions(...)) correctly — the
    # subclass instance was silently downcast to the base, and Docling's
    # pipeline then fell back to its default OCR engine (whose C++ backend
    # throws "could not execute a primitive").
    #
    # Fix: widen the field annotation to Union[OcrOptions, PaddleOcrOptions]
    # and rebuild the model. Pydantic will then preserve PaddleOcrOptions
    # instances as-is (it picks the most specific matching type in a Union).
    try:
        from typing import Union
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions,
            OcrOptions,
            EasyOcrOptions,
            TesseractCliOcrOptions,
            TesseractOcrOptions,
            RapidOcrOptions,
            OcrMacOptions,
            OcrAutoOptions,
            KserveV2OcrOptions,
        )

        _field = PdfPipelineOptions.model_fields.get("ocr_options")
        if _field is not None:
            _current_ann = _field.annotation
            # Only patch if not already widened (idempotent).
            if _current_ann is OcrOptions or (
                hasattr(_current_ann, "__origin__")
                and PaddleOcrOptions not in getattr(_current_ann, "__args__", ())
            ):
                # Union of every concrete subclass Docling ships + ours.
                # PaddleOcrOptions first so pydantic prefers it when kind matches.
                new_ann = Union[
                    PaddleOcrOptions,
                    EasyOcrOptions,
                    TesseractCliOcrOptions,
                    TesseractOcrOptions,
                    RapidOcrOptions,
                    OcrMacOptions,
                    OcrAutoOptions,
                    KserveV2OcrOptions,
                    OcrOptions,
                ]
                _field.annotation = new_ann
                # Rebuild the model so pydantic recompiles the validator.
                PdfPipelineOptions.model_rebuild(force=True)
                _log.info(
                    "[PaddleOCR] Patched PdfPipelineOptions.ocr_options annotation "
                    "to Union[PaddleOcrOptions, EasyOcrOptions, ...] and rebuilt."
                )

                # Verify round-trip: construct and re-read.
                _probe = PdfPipelineOptions(
                    ocr_options=PaddleOcrOptions(),
                    allow_external_plugins=True,
                )
                _log.info(
                    f"[PaddleOCR] Pydantic patch verified — "
                    f"round-trip ocr_options type={type(_probe.ocr_options).__name__}, "
                    f"kind={getattr(_probe.ocr_options, 'kind', None)!r}"
                )
                if not isinstance(_probe.ocr_options, PaddleOcrOptions):
                    _log.error(
                        "[PaddleOCR] Pydantic patch FAILED verification — "
                        f"round-trip produced {type(_probe.ocr_options).__name__} "
                        "instead of PaddleOcrOptions. OCR will fall back to default."
                    )
            else:
                _log.info(
                    "[PaddleOCR] PdfPipelineOptions.ocr_options annotation already "
                    "includes PaddleOcrOptions — skipping patch."
                )
    except Exception:
        _log.error(
            "[PaddleOCR] Failed to patch PdfPipelineOptions.ocr_options field"
        )

    _registered = True
