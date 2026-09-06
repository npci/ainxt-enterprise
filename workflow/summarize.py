# SPDX-License-Identifier: MIT
# ============================================================
# Summarization workflow — segment→map→reduce→format planner (pure)
# ============================================================
#
# docs/architecture/13-generation-workflows.md §13.6 (W3). Elevates summarization
# from one generic LLM call to a STAGED, verifiable pipeline that mirrors the
# coverage_retriever map-reduce and the doc pipeline (§15):
#
#   segment → map (per-segment summary) → reduce (merge) → format (per output_format)
#
# This module owns the pure orchestration: how source text is SEGMENTED, the
# stage sequence, the reduce plan (single vs. hierarchical for many segments),
# and the final format shaping. The actual per-segment LLM calls live in the
# worker; this planner decides WHAT each stage does. Pure → testable offline;
# caller falls back to the single-call summary on any failure.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# stages (docs/architecture/13 §13.6)
SEGMENT = "segment"
MAP = "map"
REDUCE = "reduce"
FORMAT = "format"

STAGES = [SEGMENT, MAP, REDUCE, FORMAT]

# reduce beyond this many map-summaries in one pass → go hierarchical (tree reduce)
_HIERARCHICAL_THRESHOLD = 8


@dataclass
class Segment:
    id: int
    text: str
    tokens: int = 0


@dataclass
class SummarizePlan:
    segments: List[Segment] = field(default_factory=list)
    map_calls: int = 0            # one per segment
    reduce_passes: int = 1        # 1 = single reduce; >1 = hierarchical
    hierarchical: bool = False
    output_format: str = "prose"  # prose|bullets|table
    stages: List[str] = field(default_factory=lambda: list(STAGES))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "segments": len(self.segments),
            "map_calls": self.map_calls,
            "reduce_passes": self.reduce_passes,
            "hierarchical": self.hierarchical,
            "output_format": self.output_format,
        }


def _est_tokens(text: str) -> int:
    return (len(text) + 3) // 4 if text else 0


def segment_text(text: str, *, max_seg_tokens: int = 500) -> List[Segment]:
    """Split source into topic-ish segments bounded by token budget. Splits on
    blank-line paragraph boundaries first (natural topic units), then packs
    paragraphs into segments up to max_seg_tokens. Never raises."""
    try:
        paras = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
        segments: List[Segment] = []
        buf: List[str] = []
        buf_tokens = 0
        for p in paras:
            pt = _est_tokens(p)
            if buf and buf_tokens + pt > max_seg_tokens:
                joined = "\n\n".join(buf)
                segments.append(Segment(id=len(segments), text=joined, tokens=buf_tokens))
                buf, buf_tokens = [], 0
            buf.append(p)
            buf_tokens += pt
        if buf:
            joined = "\n\n".join(buf)
            segments.append(Segment(id=len(segments), text=joined, tokens=buf_tokens))
        return segments
    except Exception:  # noqa: BLE001
        return [Segment(id=0, text=text or "", tokens=_est_tokens(text or ""))]


def plan_summarize(
    text: str,
    *,
    output_format: str = "prose",
    max_seg_tokens: int = 500,
) -> SummarizePlan:
    """Build the staged plan. One map call per segment; a single reduce pass,
    unless there are many segments → hierarchical (tree) reduce so the reduce
    prompt never itself overflows. Never raises → caller degrades to one call."""
    try:
        segments = segment_text(text, max_seg_tokens=max_seg_tokens)
        n = len(segments)
        hierarchical = n > _HIERARCHICAL_THRESHOLD
        # tree reduce: ceil(log_threshold(n)) passes
        reduce_passes = 1
        if hierarchical:
            remaining = n
            reduce_passes = 0
            while remaining > 1:
                remaining = (remaining + _HIERARCHICAL_THRESHOLD - 1) // _HIERARCHICAL_THRESHOLD
                reduce_passes += 1
        return SummarizePlan(
            segments=segments,
            map_calls=n,
            reduce_passes=reduce_passes,
            hierarchical=hierarchical,
            output_format=output_format if output_format in ("prose", "bullets", "table") else "prose",
        )
    except Exception:  # noqa: BLE001
        return SummarizePlan(segments=[Segment(0, text or "", _est_tokens(text or ""))],
                             map_calls=1, reduce_passes=1)


def next_stage(current: Optional[str]) -> Optional[str]:
    """Stage after `current` (None → first; last → None)."""
    if current is None:
        return STAGES[0]
    try:
        i = STAGES.index(current)
        return STAGES[i + 1] if i + 1 < len(STAGES) else None
    except ValueError:
        return STAGES[0]
