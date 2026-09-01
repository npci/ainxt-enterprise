# SPDX-License-Identifier: Apache-2.0
"""Shared parser-failure sentinels.

``core.document_parser`` signals a parse failure by returning a bracketed
string in the ``content`` field (it does not raise). Multiple ABStudio
routes need to detect those sentinels and trigger fallback paths
(OCR, vision, etc.) instead of storing the sentinel as document content.

This module lifts the previously-duplicated ``_PARSER_ERROR_PREFIXES``
tuple out of ``app/api/kb.py`` so that ``app/api/documents.py`` and the
new ``app/core/ocr_pipeline.py`` can share the exact same list.

Keep in sync with the parent-platform list in
``routers/docs_router.py::_PARSER_ERROR_PREFIXES``.
"""
from __future__ import annotations

PARSER_ERROR_PREFIXES: tuple[str, ...] = (
    "[PDF parse error",
    "[PDF parsing unavailable",
    "[DOCX parse error",
    "[DOCX parsing unavailable",
    "[PPTX parse error",
    "[PPTX parsing unavailable",
    "[Presentation has no text content]",
    "[Legacy .ppt format",
    "[HTML parse error",
    "[HTML parsing unavailable",
    "[TXT read error",
    "[Excel parse error",
    "[Excel parsing unavailable",
    "[Excel file is empty]",
    "[CSV parse error",
    "[CSV parsing unavailable",
    "[RTF parse error",
    "[RTF parsing unavailable",
    "[JSON parse error",
)


def is_parser_error(text: str) -> bool:
    """Return True if ``text`` is a parser sentinel error string."""
    if not text:
        return False
    return text.startswith(PARSER_ERROR_PREFIXES)
