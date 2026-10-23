# SPDX-License-Identifier: MIT
"""Containment-checked path construction (CWE-22 / CodeQL py/path-injection).

Every filesystem path built from request-derived components should go through
``safe_join_path`` so the result is *proven* to live under a trusted root.

The check is deliberately written in the ``normpath(...)`` + ``startswith(root)``
form: that is the pattern CodeQL's path-injection query recognises as a barrier
(custom validator helpers that merely raise are not traced through).
"""
from __future__ import annotations

import os


def safe_join_path(root: str, *parts: str) -> str:
    """Join ``parts`` under ``root`` and guarantee the result stays inside it.

    Raises ``ValueError`` if the normalised result escapes ``root`` (``..``
    segments, absolute components, etc.) or if any component contains a NUL.
    Returns the normalised absolute path as ``str``.
    """
    base = os.path.abspath(str(root))
    for p in parts:
        if "\x00" in str(p):
            raise ValueError("invalid path component")
    full = os.path.normpath(os.path.join(base, *[str(p) for p in parts]))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("path escapes its root directory")
    return full
