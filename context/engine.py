# SPDX-License-Identifier: MIT
# ============================================================
# Context engine — age-tiered history assembly (pure planner)
# ============================================================
#
# docs/architecture/06-context-engineering.md §6.2-6.4. Produces a ContextPlan:
# recent turns kept verbatim (sacred), mid turns distilled, old turns summarized
# — the mechanism that "feels" like memory without full-transcript cost. Also
# topic segmentation to bias verbatim budget toward the current topic (§6.5).
#
# This is a PURE planner: it decides WHICH turns go verbatim/distilled/summarized
# and returns the plan. It does NOT call the summarizer/LLM — the caller applies
# the plan using memory.chat_summarizer.distill_turn / get_chat_summary. This
# keeps it testable offline (like tests/context_benchmark) and fail-safe: on any
# error the caller falls back to today's flat recent+summary path.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

VERBATIM = "verbatim"
DISTILL = "distill"
SUMMARIZE = "summarize"


@dataclass
class TurnPlan:
    index: int
    band: str            # verbatim | distill | summarize
    topic: int = 0       # topic segment id (0 = current/default)


@dataclass
class ContextPlan:
    turns: List[TurnPlan] = field(default_factory=list)
    recent_verbatim: int = 0
    mid_distilled: int = 0
    old_summarized: int = 0
    topics: int = 1
    fits_verbatim: bool = True   # whole transcript fit → no compaction (fit-first)

    def band_of(self, index: int) -> Optional[str]:
        for t in self.turns:
            if t.index == index:
                return t.band
        return None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "recent_verbatim": self.recent_verbatim,
            "mid_distilled": self.mid_distilled,
            "old_summarized": self.old_summarized,
            "topics": self.topics,
            "fits_verbatim": self.fits_verbatim,
        }


def _segment_topics(turn_texts: List[str], boundary_markers) -> List[int]:
    """Assign each turn a topic id. A new topic starts when a turn opens with an
    explicit boundary marker ('new question', 'different topic', 'switching').
    Simple + deterministic; embedding-based segmentation is a later upgrade."""
    topic = 0
    ids: List[int] = []
    for txt in turn_texts:
        low = (txt or "").strip().lower()
        if any(low.startswith(m) or m in low[:40] for m in boundary_markers):
            topic += 1
        ids.append(topic)
    return ids


_DEFAULT_BOUNDARIES = ("new question", "different topic", "switching topics",
                       "unrelated", "change of subject", "new topic")


def plan_context(
    turn_texts: List[str],
    *,
    total_tokens: int,
    usable_budget: int,
    recent_keep: int = 20,
    mid_keep: int = 40,
    segment: bool = True,
) -> ContextPlan:
    """Decide the age-tier band for each turn. Never raises.

    Fit-first: if total_tokens <= usable_budget, ALL turns are verbatim (matches
    the confirmed strategy — no compaction until overflow). On overflow: newest
    `recent_keep` verbatim (sacred), next `mid_keep` distilled, older summarized.
    Topic segmentation (when enabled) is recorded so a caller can bias verbatim
    budget toward the current topic; it does not itself drop turns.
    """
    plan = ContextPlan()
    try:
        n = len(turn_texts or [])
        if n == 0:
            return plan

        topics = _segment_topics(turn_texts, _DEFAULT_BOUNDARIES) if segment else [0] * n
        plan.topics = (max(topics) + 1) if topics else 1

        # fit-first: everything verbatim when it fits
        if total_tokens <= usable_budget:
            plan.fits_verbatim = True
            plan.turns = [TurnPlan(i, VERBATIM, topics[i]) for i in range(n)]
            plan.recent_verbatim = n
            return plan

        # overflow: age-tier by recency (newest = highest index)
        plan.fits_verbatim = False
        for i in range(n):
            age_from_newest = (n - 1) - i  # 0 = newest
            if age_from_newest < recent_keep:
                band = VERBATIM
            elif age_from_newest < recent_keep + mid_keep:
                band = DISTILL
            else:
                band = SUMMARIZE
            plan.turns.append(TurnPlan(i, band, topics[i]))

        plan.recent_verbatim = sum(1 for t in plan.turns if t.band == VERBATIM)
        plan.mid_distilled = sum(1 for t in plan.turns if t.band == DISTILL)
        plan.old_summarized = sum(1 for t in plan.turns if t.band == SUMMARIZE)
        return plan
    except Exception:  # noqa: BLE001 — caller falls back to flat path
        # safe fallback: mark everything verbatim (fit-first), never lose a turn
        plan.turns = [TurnPlan(i, VERBATIM, 0) for i in range(len(turn_texts or []))]
        plan.recent_verbatim = len(plan.turns)
        plan.fits_verbatim = True
        return plan
