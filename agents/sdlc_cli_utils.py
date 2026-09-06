# SPDX-License-Identifier: MIT
"""Small, side-effect-free utility helpers shared by the SDLC CLI engine and
governance config.

These were formerly defined in ``agents/sdlc_agent_loop.py``. That module hosted
the (now-removed) default-off agentic recovery loop; only these leaf helpers were
imported by live code, so they were extracted here when the loop subsystem was
retired. Keep this module dependency-light (stdlib + logger only) — it is imported
at module-import time by the live CLI engine.
"""
from __future__ import annotations

import json
import os

from core.logger import logger


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        logger.warning(f"[sdlc-cli-utils] invalid int for {name}={raw!r} — using {default}")
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw.strip() if (raw and raw.strip()) else default


def _service_api_key() -> str:
    """The SDLC service-principal platform API key. No default — if unset the
    caller fails closed (suspends) rather than borrowing a user JWT."""
    return _env_str("SDLC_SERVICE_API_KEY", "")


def _strip_json_fences(text: str) -> str:
    """Strip a leading ```json / ``` fence and trailing ``` so a fenced answer can be
    parse-checked. Tolerant — returns the input unchanged if no fence is present."""
    if not isinstance(text, str):
        return ""
    t = text.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _json_parses(text: str) -> bool:
    """True if `text` (after fence-stripping) parses as JSON. Deterministic, no LLM."""
    if not isinstance(text, str) or not text.strip():
        return False
    try:
        json.loads(_strip_json_fences(text))
        return True
    except Exception:
        return False


def _looks_truncated_json(text: str) -> bool:
    """Heuristic: the answer looks like JSON that was cut off — it starts with a
    brace/bracket but the open/close counts don't balance (and it does not already
    parse). Catches the 'unclosed object at the token ceiling' case even when the
    upstream stop_reason wasn't surfaced."""
    t = _strip_json_fences(text or "")
    if not (t.startswith("{") or t.startswith("[")):
        return False
    if _json_parses(t):
        return False
    return (t.count("{") > t.count("}")) or (t.count("[") > t.count("]"))
