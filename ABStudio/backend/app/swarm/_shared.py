# SPDX-License-Identifier: Apache-2.0
"""Shared utilities for the swarm layer.

These helpers used to live in ``app.tools.delegate_tools`` as underscore-
prefixed privates back when the static sub-agent registry was the only
delegation surface. The static layer has been removed; the swarm layer
inherits the same anti-hallucination contract (sourced claims, low-
confidence warnings, fenced/bare JSON parse) and now owns them outright.

Single source of truth for:

* ``ALIAS_RE``                  — lowercase identifier regex for role_ids
* ``try_parse_json_object``     — fenced/bare/prose-prefixed JSON pluck
* ``enforce_no_unsourced_claim``— guard against unsupported factual answers
* ``surface_low_confidence``    — warning when a worker self-reports doubt
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict


# Lowercase ASCII identifier, 1..40 chars. Used to validate role_ids in
# orchestrator plans and as the canonical regex for any future synthetic
# identifier namespace.
ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


# Threshold below which a worker's self-reported confidence triggers a
# structured warning on the aggregator envelope. Tunable via env so ops
# can experiment without code edits.
LOW_CONFIDENCE_THRESHOLD = float(os.getenv("SWARM_LOW_CONFIDENCE", "0.5"))


def try_parse_json_object(text: Any) -> Any:
    """Parse ``text`` as a JSON object.

    Tolerates:
      * a single ```json fenced block
      * prose preceding or following the object (picks the first balanced
        ``{...}`` substring)

    Returns ``None`` on any failure — callers decide whether that means
    "retry the LLM" or "fall back to a prose envelope".
    """
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\n", "", candidate)
        candidate = re.sub(r"\n```$", "", candidate)
    first_brace = candidate.find("{")
    last_brace = candidate.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidate = candidate[first_brace:last_brace + 1]
    try:
        return json.loads(candidate)
    except Exception:
        return None


def enforce_no_unsourced_claim(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Replace an answer-with-zero-sources envelope with an error envelope.

    Structural counterpart to the prompt-side rule "every factual claim
    must cite a source". Prompts can drift; this guard cannot.
    """
    if "error" in envelope:
        return envelope
    answer = envelope.get("answer")
    if isinstance(answer, str) and answer.strip():
        sources = envelope.get("sources")
        if isinstance(sources, list) and len(sources) == 0:
            return {
                "error":  "unsourced_answer",
                "detail": ("Worker returned an answer with zero sources. "
                           "Refusing to forward an unsourced factual claim."),
                "alias":  envelope.get("alias"),
            }
    return envelope


def surface_low_confidence(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Append a structured warning when ``confidence`` is below threshold.

    Mutates a copy of ``envelope.warnings`` — the parent prompt can
    react accordingly (e.g. ask the user to confirm).
    """
    if "error" in envelope:
        return envelope
    conf = envelope.get("confidence")
    if isinstance(conf, (int, float)) and 0 <= conf < LOW_CONFIDENCE_THRESHOLD:
        envelope.setdefault("warnings", []).append({
            "code":      "low_confidence",
            "threshold": LOW_CONFIDENCE_THRESHOLD,
            "actual":    conf,
            "guidance":  ("Parent: treat this answer as tentative. "
                          "Consider asking the user to confirm or retry "
                          "with more context."),
        })
    return envelope


__all__ = [
    "ALIAS_RE",
    "LOW_CONFIDENCE_THRESHOLD",
    "try_parse_json_object",
    "enforce_no_unsourced_claim",
    "surface_low_confidence",
]
