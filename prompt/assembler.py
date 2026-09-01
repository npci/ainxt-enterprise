# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Prompt assembler — typed slots → CompiledPrompt (pure)
# ============================================================
#
# docs/architecture/09-prompt-construction.md §9.2-9.4. Treats the prompt as a
# COMPILED artifact from typed slots rather than ad-hoc string concatenation.
# Slots are ordered stable→volatile so the stable prefix is prompt-cacheable
# (§9.3): a `cache_boundary` marks where the reusable prefix ends.
#
# Pure stdlib. NOT yet wired into gateway (adoption flag-gated later). Provides
# the tested target so the eventual swap from inline concatenation is mechanical.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# stability classes — higher = more stable = earlier in prompt = more cacheable
STABLE = "stable"       # system, tools, safety, durable memory, custom instructions
VOLATILE = "volatile"   # retrieved evidence, conversation, current task


@dataclass
class Slot:
    name: str
    content: str
    priority: int             # higher = kept first when trimming to budget
    stability: str = VOLATILE
    droppable: bool = True
    tokens: int = 0           # estimated; filled by assemble()


# canonical slot order + stability (docs/architecture/09 §9.2)
_SLOT_SPEC = [
    ("system",       100, STABLE,   False),
    ("tools",         95, STABLE,   True),
    ("safety",        90, STABLE,   False),
    ("durable_memory", 85, STABLE,  False),
    ("instructions",  80, STABLE,   True),
    ("evidence",      70, VOLATILE, True),
    ("conversation",  60, VOLATILE, True),
    ("task",          50, VOLATILE, False),
]

_CHARS_PER_TOKEN = 4  # matches gateway's estimate; providers override when known


def _est_tokens(text: str) -> int:
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN if text else 0


@dataclass
class CompiledPrompt:
    slots: List[Slot] = field(default_factory=list)
    cache_boundary_index: int = 0     # slots[:idx] are the stable, cacheable prefix
    token_count: int = 0
    slots_dropped: List[str] = field(default_factory=list)
    evidence_chunk_ids: List[str] = field(default_factory=list)

    def stable_prefix(self) -> List[Slot]:
        return self.slots[: self.cache_boundary_index]

    def text(self) -> str:
        return "\n\n".join(s.content for s in self.slots if s.content)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "slots_included": [s.name for s in self.slots],
            "slots_dropped": list(self.slots_dropped),
            "cache_boundary_index": self.cache_boundary_index,
            "token_count": self.token_count,
            "evidence_chunk_ids": list(self.evidence_chunk_ids),
        }


def assemble(
    parts: Dict[str, str],
    *,
    budget_tokens: int = 0,
    evidence_chunk_ids: Optional[List[str]] = None,
) -> CompiledPrompt:
    """Assemble typed slots into a CompiledPrompt. Never raises.

    `parts` maps slot name → content (missing/empty slots are skipped). Slots are
    emitted in canonical stable→volatile order. If `budget_tokens` > 0 and the
    total exceeds it, droppable slots are trimmed LOWEST-priority first (never a
    non-droppable slot). `cache_boundary_index` marks the end of the stable prefix.
    """
    cp = CompiledPrompt(evidence_chunk_ids=list(evidence_chunk_ids or []))
    try:
        built: List[Slot] = []
        for name, priority, stability, droppable in _SLOT_SPEC:
            content = (parts or {}).get(name) or ""
            if not content:
                continue
            s = Slot(name=name, content=content, priority=priority,
                     stability=stability, droppable=droppable)
            s.tokens = _est_tokens(content)
            built.append(s)

        # budget trim: drop lowest-priority droppable slots until under budget
        if budget_tokens and budget_tokens > 0:
            total = sum(s.tokens for s in built)
            if total > budget_tokens:
                for s in sorted(built, key=lambda x: x.priority):  # low priority first
                    if total <= budget_tokens:
                        break
                    if s.droppable:
                        cp.slots_dropped.append(s.name)
                        total -= s.tokens
                built = [s for s in built if s.name not in cp.slots_dropped]

        # canonical order is already stable→volatile via _SLOT_SPEC iteration
        cp.slots = built
        cp.cache_boundary_index = sum(1 for s in built if s.stability == STABLE
                                      and _is_prefix_stable(built, s))
        cp.token_count = sum(s.tokens for s in built)
    except Exception:  # noqa: BLE001 — assembler must never break a turn
        cp.slots = [Slot(name="task", content=(parts or {}).get("task", ""),
                        priority=50, stability=VOLATILE, droppable=False)]
        cp.token_count = _est_tokens(cp.slots[0].content)
    return cp


def _is_prefix_stable(slots: List[Slot], slot: Slot) -> bool:
    """A stable slot counts toward the cacheable prefix only if every slot before
    it is also stable (a volatile slot breaks the cacheable prefix)."""
    for s in slots:
        if s is slot:
            return True
        if s.stability != STABLE:
            return False
    return True
