# SPDX-License-Identifier: Apache-2.0
"""
Escalation gate — Fast tier → Coverage tier.

Implements §8y of docs/SPEC_KNOWLEDGE_ARCHITECTURE.md. All signals are cheap
(graph lookups + scores + tiny local heuristic); no cloud tokens are spent
deciding whether to escalate.

Fail-safe principle: when in doubt, escalate. The cost of an unnecessary
Coverage run is a few hundred ms; the cost of a missed exception on page 270
is a wrong answer.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict
from typing import Iterable


# ── Tunables (env-overridable) ───────────────────────────────────────────────

_RERANK_MIN_TOP            = float(os.getenv("KB_GATE_RERANK_MIN_TOP",        "0.55"))
_RERANK_FLAT_GAP_THRESHOLD = float(os.getenv("KB_GATE_RERANK_FLAT_GAP",       "0.05"))
_MIN_SECTION_COVERAGE      = float(os.getenv("KB_GATE_MIN_SECTION_COVERAGE",  "0.30"))
_SUFFICIENCY_THRESHOLD     = float(os.getenv("KB_GATE_SUFFICIENCY_THRESHOLD", "0.55"))

# Intent keywords that strongly imply a completeness/aggregation question.
# These rarely fit in top-k retrieval — escalate by default.
_COVERAGE_INTENT_RE = re.compile(
    r"\b("
    r"all|every|complete|exhaustive|exhaustively|comprehensive|"
    r"each|enumerate|enumerated|list\s+all|"
    r"any\s+exception|exceptions?|edge\s*cases?|"
    r"throughout|across\s+the\s+spec|across\s+the\s+document|"
    r"what\s+changed|version\s+diff|compare\s+versions?"
    r")\b",
    re.IGNORECASE,
)


@dataclass
class GateDecision:
    escalate: bool
    sufficiency: float         # 0..1 — combined score across signals 1..5
    reason: str                # short human-readable explanation
    signals: dict              # per-signal scores for trace/audit

    def to_dict(self) -> dict:
        return asdict(self)


def _score(values: list[float]) -> float:
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def evaluate(
    question: str,
    reranked: Iterable[dict],
    section_map: list[dict] | None = None,
    parent_section_paths: set[str] | None = None,
    has_dependency_leak: bool = False,
) -> GateDecision:
    """
    Return a GateDecision based on the fast-tier output.

    Parameters
    ----------
    question
        The raw user query.
    reranked
        Items the Fast tier produced (each dict carries `score`, `text`,
        `section_path`, etc.). Order is rerank-score descending.
    section_map
        Optional list of `{section_path, level, start, end}` entries from the
        cached doc payload. Drives the section-coverage signal.
    parent_section_paths
        Optional set of section paths the fast tier reached (typically every
        retrieved chunk's `section_path` + any expanded parents). When empty,
        coverage falls back to a chunk-count heuristic.
    has_dependency_leak
        True when any retrieved chunk has graph edges pointing at sections
        that did NOT appear in the fast-tier result set.
    """
    reranked_list = list(reranked or [])
    signals: dict = {}

    # ── Signal 1: section-coverage ratio ───────────────────────────────────
    section_total = len(section_map) if section_map else 0
    section_hit   = len(parent_section_paths) if parent_section_paths else 0
    if section_total > 0:
        coverage_ratio = min(1.0, section_hit / section_total)
    else:
        # No section map (code or unstructured doc) — fall back to chunk count.
        coverage_ratio = min(1.0, len(reranked_list) / 6.0)
    signals["section_coverage_ratio"] = round(coverage_ratio, 3)

    # ── Signal 2: graph dependency leak ────────────────────────────────────
    signals["dependency_leak"] = bool(has_dependency_leak)

    # ── Signal 3: rerank score profile ─────────────────────────────────────
    scores = [float(r.get("score", 0)) for r in reranked_list]
    top_score = max(scores) if scores else 0.0
    signals["top_score"] = round(top_score, 3)

    # ── Signal 4: score gap (top-1 vs top-k) ───────────────────────────────
    if len(scores) >= 2:
        score_gap = scores[0] - scores[min(len(scores) - 1, 4)]
    else:
        score_gap = 0.0
    signals["score_gap"] = round(score_gap, 3)

    # ── Signal 5: query intent ─────────────────────────────────────────────
    is_coverage_intent = bool(_COVERAGE_INTENT_RE.search(question or ""))
    signals["coverage_intent"] = is_coverage_intent

    # ── Combine signals into a sufficiency score ───────────────────────────
    # Each signal contributes a [0,1] component that means "fast tier is
    # sufficient" (higher = more sufficient). Final = mean of components.
    components: list[float] = []
    components.append(coverage_ratio)
    components.append(0.0 if has_dependency_leak else 1.0)
    components.append(min(1.0, top_score / max(_RERANK_MIN_TOP, 1e-6)))
    components.append(min(1.0, score_gap / max(_RERANK_FLAT_GAP_THRESHOLD, 1e-6)))
    components.append(0.0 if is_coverage_intent else 1.0)
    sufficiency = _score(components)
    signals["sufficiency"] = round(sufficiency, 3)

    # ── Decision ───────────────────────────────────────────────────────────
    reasons: list[str] = []
    if coverage_ratio < _MIN_SECTION_COVERAGE and section_total > 0:
        reasons.append(
            f"low section coverage ({signals['section_coverage_ratio']:.2f} < {_MIN_SECTION_COVERAGE})"
        )
    if has_dependency_leak:
        reasons.append("graph dependency leak")
    if top_score < _RERANK_MIN_TOP:
        reasons.append(f"top rerank score {top_score:.2f} < {_RERANK_MIN_TOP}")
    if score_gap < _RERANK_FLAT_GAP_THRESHOLD and len(scores) >= 2:
        reasons.append(f"flat rerank scores (gap {score_gap:.2f})")
    if is_coverage_intent:
        reasons.append("query has completeness/aggregation intent")
    if not reranked_list:
        reasons.append("fast tier returned zero candidates")

    escalate = (sufficiency < _SUFFICIENCY_THRESHOLD) or bool(reasons)
    reason = "; ".join(reasons) if reasons else "fast tier sufficient"

    return GateDecision(
        escalate=escalate,
        sufficiency=round(sufficiency, 3),
        reason=reason,
        signals=signals,
    )
