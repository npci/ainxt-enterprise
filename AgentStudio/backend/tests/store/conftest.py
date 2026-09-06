# SPDX-License-Identifier: MIT
"""Test bootstrap for the workflow-engine perf suite (catalog cache,
per-run resolution cache, KB overlap).

Mirrors the sys.path bootstrap used by ``ABStudio/backend/tests/governance/``
so ``app.*`` (and peer top-level modules such as ``core.*``) resolve when the
suite runs on a bare interpreter, without requiring the package to be
installed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2]
_PLATFORM_ROOT = _BACKEND.parents[1]
for _p in (str(_BACKEND), str(_PLATFORM_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    """Clear workflow_repo's module-level catalog cache (and its generation
    counters) before AND after every test in this suite so tests don't leak
    cached rows — or a bumped generation — into each other (the cache is a
    plain process-wide dict — see REQ-P3-1)."""
    try:
        from app.core import workflow_repo as wr
    except Exception:
        yield
        return

    def _clear():
        wr._tool_cache.clear()
        wr._skill_cache.clear()
        wr._skill_files_cache.clear()
        wr._tool_cache_generation.value = 0
        wr._skill_cache_generation.value = 0

    _clear()
    yield
    _clear()
