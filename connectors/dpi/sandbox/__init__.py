# SPDX-License-Identifier: MIT
"""
DPI synthetic sandbox — loads offline fixtures so DPI connectors run with NO
real upstream, credentials, or licensing. Everything here is SYNTHETIC test data
(values are shaped like Aadhaar/account numbers so the compliance redactor is
visibly exercised, but they are not real and not Luhn-valid card numbers).
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

_DIR = os.path.dirname(os.path.abspath(__file__))


@lru_cache(maxsize=16)
def load_fixture(name: str) -> dict:
    """Load a synthetic fixture by name (without .json). Returns {} if missing."""
    path = os.path.join(_DIR, f"{name}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}
