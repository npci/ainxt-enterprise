# SPDX-License-Identifier: Apache-2.0
"""Permissive PDF backend — pypdfium2 + pypdf + pdfplumber.

Licences, which is the entire point of this module:

    pypdfium2   BSD-3-Clause / Apache-2.0  (wraps Google's PDFium)
    pypdf       BSD-3-Clause
    pdfplumber  MIT
    Pillow      HPND (MIT-style)

No copyleft. This is the default backend precisely because of that: the
AGPL-3.0 alternative (PyMuPDF) is not installed by default and must be opted
into explicitly.

DIVISION OF LABOUR
------------------
No single permissive library covers what PyMuPDF does alone, so each capability
goes to the library that does it best:

    rasterisation (get_pixmap)   pypdfium2  -- the only permissive engine that
                                              renders PDF pages to bitmaps
    plain text (get_text)        pdfplumber -- better layout fidelity than pypdf
    layout blocks ("blocks")     pdfplumber -- word boxes clustered into lines
    embedded images              pypdf      -- exposes decoded image objects
    outline / TOC                pypdf      -- .outline
    page geometry, metadata      pypdf      -- .mediabox, .metadata

KNOWN BEHAVIOURAL DIFFERENCES FROM PyMuPDF
------------------------------------------
Stated plainly because these are the things the differential harness exists to
measure, and because pretending they do not exist would be the failure mode
that matters:

* **Text ordering.** PyMuPDF and pdfplumber both emit reading order
  heuristically. On multi-column or heavily-styled pages they will differ.
  Downstream RAG chunking may therefore chunk differently.
* **"blocks" granularity.** PyMuPDF returns its own block segmentation. Here
  blocks are reconstructed by clustering pdfplumber words into lines by vertical
  proximity, then into paragraphs by gap. Similar, not identical.
* **Image xrefs.** PyMuPDF's `xref` is a genuine PDF cross-reference number.
  pypdf does not expose xrefs, so `get_images()` yields a synthetic stable index
  and `extract_image()` accepts that same index. Any code that persists an xref
  across runs, or compares one to a PyMuPDF-derived value, will not match.
  Verified: no call site in this repository does either -- xrefs are obtained
  and consumed within the same function.
* **Rasterisation output.** PDFium and MuPDF are different renderers. Bytes will
  differ even at identical DPI; anti-aliasing and font hinting are not
  bit-identical. OCR accuracy should be compared, not image checksums.
* **Encrypted PDFs.** pypdf needs an explicit `decrypt("")` for empty-password
  files; handled below.

Anything outside the documented surface raises `PdfBackendUnsupported` rather
than returning a plausible-looking substitute.
"""

from __future__ import annotations

import io
import os
from typing import Any, Dict, List, Optional, Tuple

from core.pdf_backend import Matrix, PdfBackendError, PdfBackendUnsupported

_DEFAULT_DPI = 72.0


def _require(module: str, pip_name: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise PdfBackendError(
            "PDF_BACKEND=pdfium requires %s (pip install %s). It is declared in "
            "requirements.txt; this environment does not have it." % (module, pip_name)
        ) from exc


# ---------------------------------------------------------------- Pixmap


class _Pixmap:
    """Rendered page bitmap. Mirrors the fitz Pixmap surface actually used."""

    __slots__ = ("_pil", "width", "height")

    def __init__(self, pil_image):
        self._pil = pil_image
        self.width, self.height = pil_image.size

    def tobytes(self, output: str = "png") -> bytes:
        fmt = (output or "png").lower()
        if fmt in ("png", "ppm", "pnm"):
            pil_fmt = "PNG" if fmt == "png" else "PPM"
        elif fmt in ("jpg", "jpeg"):
            pil_fmt = "JPEG"
        else:
            raise PdfBackendUnsupported(
                "Pixmap.tobytes(%r): only png/jpeg/ppm are shimmed" % output
            )
        buf = io.BytesIO()
        image = self._pil
        if pil_fmt == "JPEG" and image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.save(buf, format=pil_fmt)
        return buf.getvalue()

    def save(self, path: str, output: Optional[str] = None) -> None:
        image = self._pil
        fmt = (output or os.path.splitext(path)[1].lstrip(".") or "png").upper()
        if fmt in ("JPG", "JPEG"):
            fmt = "JPEG"
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
        image.save(path, format=fmt)


# ------------------------------------------------------------------ Page


class _Rect:
    __slots__ = ("x0", "y0", "x1", "y1")

    def __init__(self, x0: float, y0: float, x1: float, y1: float):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def __iter__(self):
        return iter((self.x0, self.y0, self.x1, self.y1))

    def __repr__(self) -> str:  # pragma: no cover
        return "Rect(%.2f, %.2f, %.2f, %.2f)" % (self.x0, self.y0, self.x1, self.y1)


class _Page:
    """One page, backed by pypdf (geometry/images) and pdfplumber (text)."""

    def __init__(self, doc: "_Document", index: int):
        self._doc = doc
        self.number = index

    # -- geometry ----------------------------------------------------
    @property
    def rect(self) -> _Rect:
        box = self._doc._pypdf_page(self.number).mediabox
        return _Rect(float(box.left), float(box.bottom), float(box.right), float(box.top))

    def bound(self) -> _Rect:
        return self.rect

    # -- text --------------------------------------------------------
    def get_text(self, option: str = "text", **kwargs: Any) -> Any:
        if kwargs:
            raise PdfBackendUnsupported(
                "Page.get_text() keyword arguments %s are not shimmed"
                % ", ".join(sorted(kwargs))
            )
        mode = (option or "text").lower()
        if mode in ("text", ""):
            page = self._doc._plumber_page(self.number)
            return page.extract_text() or ""
        if mode == "blocks":
            return self._blocks()
        raise PdfBackendUnsupported(
            "Page.get_text(%r): only 'text' and 'blocks' are used by this "
            "codebase and shimmed here" % option
        )

    def _blocks(self) -> List[Tuple[float, float, float, float, str, int, int]]:
        """Reconstruct fitz-style block tuples from pdfplumber words.

        fitz returns ``(x0, y0, x1, y1, text, block_no, block_type)``. Words are
        grouped into lines by vertical overlap, then lines into blocks on a
        vertical gap larger than 1.5x the median line height -- a paragraph
        break. This approximates, and does not reproduce, MuPDF's own
        segmentation; see the module docstring.
        """
        page = self._doc._plumber_page(self.number)
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False) or []
        if not words:
            return []
        words.sort(key=lambda w: (round(float(w["top"]), 1), float(w["x0"])))

        lines: List[Dict[str, Any]] = []
        for w in words:
            top, bottom = float(w["top"]), float(w["bottom"])
            placed = False
            for line in lines:
                # same line if vertical centres overlap materially
                if abs(line["top"] - top) <= max(1.0, 0.4 * (bottom - top)):
                    line["words"].append(w)
                    line["x0"] = min(line["x0"], float(w["x0"]))
                    line["x1"] = max(line["x1"], float(w["x1"]))
                    line["bottom"] = max(line["bottom"], bottom)
                    placed = True
                    break
            if not placed:
                lines.append({
                    "top": top, "bottom": bottom,
                    "x0": float(w["x0"]), "x1": float(w["x1"]), "words": [w],
                })

        heights = sorted(l["bottom"] - l["top"] for l in lines)
        median_h = heights[len(heights) // 2] if heights else 10.0
        gap_threshold = max(2.0, 1.5 * median_h)

        blocks: List[Tuple[float, float, float, float, str, int, int]] = []
        current: List[Dict[str, Any]] = []

        def flush() -> None:
            if not current:
                return
            x0 = min(l["x0"] for l in current)
            x1 = max(l["x1"] for l in current)
            y0 = min(l["top"] for l in current)
            y1 = max(l["bottom"] for l in current)
            text = "\n".join(
                " ".join(str(w["text"]) for w in sorted(l["words"], key=lambda w: float(w["x0"])))
                for l in current
            )
            blocks.append((x0, y0, x1, y1, text + "\n", len(blocks), 0))

        for line in lines:
            if current and (line["top"] - current[-1]["bottom"]) > gap_threshold:
                flush()
                current = []
            current.append(line)
        flush()
        return blocks

    # -- raster ------------------------------------------------------
    def get_pixmap(self, matrix: Optional[Matrix] = None, alpha: bool = False,
                   dpi: Optional[int] = None, **kwargs: Any) -> _Pixmap:
        if kwargs:
            raise PdfBackendUnsupported(
                "Page.get_pixmap() keyword arguments %s are not shimmed"
                % ", ".join(sorted(kwargs))
            )
        if dpi is not None:
            scale = float(dpi) / _DEFAULT_DPI
        elif matrix is not None:
            # PDFium renders at a single uniform scale; a non-uniform matrix
            # would need a post-resize, which no call site requires.
            if abs(matrix.zoom_x - matrix.zoom_y) > 1e-6:
                raise PdfBackendUnsupported(
                    "Page.get_pixmap(): non-uniform scaling (%.3f x %.3f) is not "
                    "shimmed; every call site in this codebase uses a uniform zoom"
                    % (matrix.zoom_x, matrix.zoom_y)
                )
            scale = matrix.zoom_x
        else:
            scale = 1.0

        pdfium_page = self._doc._pdfium_page(self.number)
        bitmap = pdfium_page.render(scale=scale, draw_annots=True)
        pil = bitmap.to_pil()
        if not alpha and pil.mode in ("RGBA", "LA"):
            pil = pil.convert("RGB")
        return _Pixmap(pil)

    # -- embedded images --------------------------------------------
    def get_images(self, full: bool = False) -> List[Tuple]:
        """Return image references for this page.

        fitz yields tuples whose first element is a PDF xref. pypdf does not
        expose xrefs, so element 0 is a **synthetic stable index** of the form
        ``page_index * 10000 + image_ordinal``, which `extract_image()` on this
        backend accepts. See the module docstring: nothing in this repository
        persists or cross-compares an xref.
        """
        page = self._doc._pypdf_page(self.number)
        out: List[Tuple] = []
        try:
            images = list(page.images)
        except Exception:
            return out
        for ordinal, img in enumerate(images):
            key = self.number * 10000 + ordinal
            self._doc._image_cache[key] = img
            width = getattr(getattr(img, "image", None), "width", 0) or 0
            height = getattr(getattr(img, "image", None), "height", 0) or 0
            if full:
                # (xref, smask, width, height, bpc, colorspace, alt, name, filter)
                out.append((key, 0, width, height, 8, "", "", getattr(img, "name", ""), ""))
            else:
                out.append((key, 0, width, height, 8, "", "", getattr(img, "name", "")))
        return out


# -------------------------------------------------------------- Document


class _Document:
    """A PDF held open by three libraries at once, each for what it does best."""

    def __init__(self, path: Any):
        pypdf = _require("pypdf", "pypdf")
        pdfplumber = _require("pdfplumber", "pdfplumber")
        pypdfium2 = _require("pypdfium2", "pypdfium2")
        _require("PIL", "pillow")

        self._path = str(path)
        self._closed = False
        self._image_cache: Dict[int, Any] = {}

        self._reader = pypdf.PdfReader(self._path)
        if getattr(self._reader, "is_encrypted", False):
            try:
                self._reader.decrypt("")
            except Exception as exc:
                raise PdfBackendError(
                    "PDF is encrypted and could not be opened with an empty "
                    "password: %s" % exc
                ) from exc

        self._plumber = pdfplumber.open(self._path)
        self._pdfium = pypdfium2.PdfDocument(self._path)
        self._pages = [_Page(self, i) for i in range(len(self._reader.pages))]

    # -- internals ---------------------------------------------------
    def _check(self) -> None:
        if self._closed:
            raise PdfBackendError("operation on a closed Document")

    def _pypdf_page(self, index: int):
        self._check()
        return self._reader.pages[index]

    def _plumber_page(self, index: int):
        self._check()
        return self._plumber.pages[index]

    def _pdfium_page(self, index: int):
        self._check()
        return self._pdfium[index]

    # -- surface -----------------------------------------------------
    def __len__(self) -> int:
        self._check()
        return len(self._pages)

    @property
    def page_count(self) -> int:
        return len(self)

    def __iter__(self):
        self._check()
        return iter(self._pages)

    def __getitem__(self, index: int) -> _Page:
        self._check()
        return self._pages[index]

    def load_page(self, index: int) -> _Page:
        self._check()
        return self._pages[index]

    @property
    def metadata(self) -> Dict[str, Any]:
        self._check()
        raw = self._reader.metadata or {}
        # fitz exposes lowercase keys without the PDF '/' prefix.
        return {
            str(k).lstrip("/").lower(): (str(v) if v is not None else "")
            for k, v in dict(raw).items()
        }

    @property
    def is_encrypted(self) -> bool:
        return bool(getattr(self._reader, "is_encrypted", False))

    def extract_image(self, xref: int) -> Dict[str, Any]:
        """Return ``{"image": bytes, "ext": str, "width": int, "height": int}``.

        ``xref`` is the synthetic index produced by `Page.get_images()` on this
        backend, not a real PDF xref.
        """
        self._check()
        key = int(xref)
        img = self._image_cache.get(key)
        if img is None:
            # Populate lazily: the caller may have obtained the index on a
            # previous Page object that has since been discarded.
            page_index = key // 10000
            if 0 <= page_index < len(self._pages):
                self._pages[page_index].get_images(full=True)
                img = self._image_cache.get(key)
        if img is None:
            raise PdfBackendError(
                "extract_image(%r): no such image index on this backend. Note "
                "that indices are synthetic here and are not interchangeable "
                "with PyMuPDF xrefs." % xref
            )
        data = getattr(img, "data", None)
        if data is None:
            raise PdfBackendError("extract_image(%r): image carries no data" % xref)
        name = str(getattr(img, "name", "") or "")
        ext = (os.path.splitext(name)[1].lstrip(".") or "png").lower()
        pil = getattr(img, "image", None)
        return {
            "image": data,
            "ext": ext,
            "width": getattr(pil, "width", 0) or 0,
            "height": getattr(pil, "height", 0) or 0,
        }

    def get_toc(self, simple: bool = True) -> List[List[Any]]:
        """Return ``[[level, title, page], ...]`` as fitz does."""
        self._check()
        out: List[List[Any]] = []

        def walk(items: Any, level: int) -> None:
            for item in items or ():
                if isinstance(item, list):
                    walk(item, level + 1)
                    continue
                title = str(getattr(item, "title", "") or "")
                try:
                    page = self._reader.get_destination_page_number(item) + 1
                except Exception:
                    page = 0
                out.append([level, title, page])

        try:
            walk(self._reader.outline, 1)
        except Exception:
            return []
        return out

    # -- lifecycle ---------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for closer in (
            lambda: self._plumber.close(),
            lambda: self._pdfium.close(),
        ):
            try:
                closer()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
        self._image_cache.clear()

    def __enter__(self) -> "_Document":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# --------------------------------------------------------------- entry


def open_document(path: Any) -> _Document:
    return _Document(path)


def to_markdown(path: Any, **kwargs: Any) -> str:
    """PDF to markdown, replacing `pymupdf4llm.to_markdown`.

    Uses **markitdown** (MIT, already declared in requirements.txt with the
    `[pdf]` extra). Output will not be textually identical to pymupdf4llm's --
    heading inference and table rendering differ -- which is precisely what the
    differential harness is for.

    `pymupdf4llm`-specific keywords (`pages`, `hdr_info`, `page_chunks`, ...)
    have no markitdown equivalent. They are rejected rather than ignored: a
    caller asking for a page range and silently receiving the whole document
    would corrupt batched ingestion without raising.
    """
    if kwargs:
        raise PdfBackendUnsupported(
            "to_markdown() on the pdfium backend does not support %s. The "
            "pymupdf4llm batching/header options have no markitdown equivalent; "
            "call sites using them need restructuring rather than a silent "
            "behaviour change." % ", ".join(sorted(kwargs))
        )
    try:
        from markitdown import MarkItDown  # type: ignore
    except ImportError as exc:
        raise PdfBackendError(
            "PDF_BACKEND=pdfium needs markitdown for to_markdown() "
            "(declared in requirements.txt as markitdown[pdf,...])."
        ) from exc
    result = MarkItDown().convert(str(path))
    return getattr(result, "text_content", "") or ""
