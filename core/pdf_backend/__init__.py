# SPDX-License-Identifier: MIT
"""PDF backend facade — a fitz-compatible surface over the pdfium engine.

WHY THIS EXISTS
---------------
PDF handling was bound directly to PyMuPDF (`import fitz`) at 20 sites across 10
files, with roughly 99 call sites and no abstraction. . This package removed the direct binding so call sites use
a stable API while the underlying engine is swappable:

    # before
    import fitz

    # after
    from core import pdf_backend as fitz

Nothing else at the call site changes. PyMuPDF has since been removed
entirely (see NOTICE) — the only engine implementing this facade is
`pdfium` (pypdfium2 + pypdf + pdfplumber + markitdown, all permissively
licensed: BSD-3-Clause / Apache-2.0 / MIT).

WHAT WAS TRADED BY REMOVING PYMUPDF
------------------------------------
This is a real trade, stated plainly rather than buried, `core.document_parser`
descends a documented ladder — `markitdown` (MIT), then plain text via this
facade — and the resulting Markdown has coarser structure. Text content is
extracted either way; it is the *structure* that degrades.

THE SURFACE THIS IMPLEMENTS
---------------------------
Only what the codebase actually uses, verified by inspection rather than
guessed:

  module   open(path), Matrix(zx, zy), Page
  Document context manager, close(), len(), page_count, metadata,
           iteration over pages, load_page(n), extract_image(xref), get_toc()
  Page     get_text(), get_text("text"), get_text("blocks"),
           get_pixmap(matrix=, alpha=), get_images(full=), rect.width,
           rect.height, bound(), number
  Pixmap   tobytes("png"), save(path)

Anything outside that surface raises `PdfBackendUnsupported` rather than
silently returning something plausible-but-wrong.
"""

from __future__ import annotations

import os
from typing import Any, Optional

__all__ = [
    "open",
    "to_markdown",
    "Matrix",
    "Page",
    "Document",
    "Pixmap",
    "PdfBackendError",
    "PdfBackendUnsupported",
    "active_backend",
    "BACKEND_ENV",
    "DEFAULT_BACKEND",
]

BACKEND_ENV = "PDF_BACKEND"

#: The only backend: permissively-licensed pdfium.
DEFAULT_BACKEND = "pdfium"

_BACKENDS = ("pdfium",)


class PdfBackendError(RuntimeError):
    """A backend could not satisfy a request."""


class PdfBackendUnsupported(PdfBackendError):
    """The active backend does not implement this part of the surface.

    Raised rather than approximated. A PDF pipeline that silently substitutes a
    different behaviour produces degraded text and images without failing, which
    is the hardest kind of regression to notice.
    """


class Matrix:
    """Scaling matrix. Only the uniform/diagonal form is used in this codebase.

    PyMuPDF's Matrix is a full 2-D affine transform; every call site here is
    ``Matrix(zoom, zoom)`` or ``Matrix(sx, sy)``, i.e. pure scaling. Supporting
    only that keeps the shim honest -- a rotation or skew would raise rather
    than be quietly ignored.
    """

    __slots__ = ("a", "b", "c", "d", "e", "f")

    def __init__(self, *args: float):
        if len(args) == 2:
            self.a, self.d = float(args[0]), float(args[1])
            self.b = self.c = self.e = self.f = 0.0
        elif len(args) == 6:
            self.a, self.b, self.c, self.d, self.e, self.f = (float(x) for x in args)
            if self.b or self.c or self.e or self.f:
                raise PdfBackendUnsupported(
                    "pdf_backend.Matrix supports scaling only; this matrix has "
                    "rotation/skew/translation components, which no current call "
                    "site uses and which the shim will not silently drop"
                )
        else:
            raise TypeError("Matrix expects 2 or 6 numbers, got %d" % len(args))

    @property
    def zoom_x(self) -> float:
        return self.a

    @property
    def zoom_y(self) -> float:
        return self.d

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "Matrix(%.4f, %.4f)" % (self.a, self.d)


def _selected() -> str:
    name = (os.getenv(BACKEND_ENV) or DEFAULT_BACKEND).strip().lower()

    if name not in _BACKENDS:
        raise PdfBackendError(
            "%s=%r is not a known PDF backend (%s). Refusing to fall back to a "
            "different engine than requested: a silent substitution would change "
            "extraction output without anyone noticing."
            % (BACKEND_ENV, name, ", ".join(_BACKENDS))
        )
    return name


_impl = None
_impl_name = ""


def _backend():
    """Import and cache the selected backend module."""
    global _impl, _impl_name
    name = _selected()
    if _impl is not None and _impl_name == name:
        return _impl
    from core.pdf_backend import pdfium_backend as mod
    _impl, _impl_name = mod, name
    return mod


def active_backend() -> str:
    """Which backend is in force. Useful in logs and in the harness report."""
    return _selected()


def open(path: Any = None, *args: Any, **kwargs: Any):  # noqa: A001 - fitz parity
    """Open a PDF. Mirrors ``fitz.open(path)``, the only form used here."""
    if args or kwargs:
        raise PdfBackendUnsupported(
            "pdf_backend.open() accepts a single path; fitz's stream/filetype "
            "variants are not used by this codebase and are not shimmed"
        )
    return _backend().open_document(path)


def to_markdown(path: Any, **kwargs: Any) -> str:
    """PDF to markdown via the active backend (markitdown, MIT-licensed).

    Because a caller asking for a page range
    and silently receiving the whole document would corrupt batched ingestion
    without raising.
    """
    return _backend().to_markdown(path, **kwargs)


# Re-exported for isinstance checks and type hints at call sites.
class Document:  # pragma: no cover - marker base
    """Marker base so ``isinstance(x, pdf_backend.Document)`` works."""


class Page:  # pragma: no cover - marker base
    """Marker base so ``isinstance(x, pdf_backend.Page)`` works."""


class Pixmap:  # pragma: no cover - marker base
    """Marker base so ``isinstance(x, pdf_backend.Pixmap)`` works."""
