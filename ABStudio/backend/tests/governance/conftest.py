# SPDX-License-Identifier: Apache-2.0
"""Test bootstrap for the governance/budget suite.

Mirrors the sys.path bootstrap used elsewhere so ``app.*`` and ``core.*``
resolve when the suite runs on a bare interpreter.
"""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2]
_PLATFORM_ROOT = _BACKEND.parents[1]
for _p in (str(_BACKEND), str(_PLATFORM_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
