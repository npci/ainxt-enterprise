# SPDX-License-Identifier: Apache-2.0
"""Utility helpers for the eval-viewer review server."""


def clean_benchmark(raw: dict) -> dict:
    """Return a sanitized benchmark dict containing only safe scalar values."""
    return {
        k: v for k, v in raw.items()
        if isinstance(v, (str, int, float, bool, list)) or v is None
    }
