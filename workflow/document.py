# SPDX-License-Identifier: MIT
# ============================================================
# Staged document pipeline — state + stage planner (pure)
# ============================================================
#
# docs/architecture/15-document-generation.md §15.4-15.5. Turns one-shot doc
# generation into an outline→draft→consistency→style pipeline with a shared
# DocumentState so figures/terms stated once are REUSED (anti-drift, §15.4).
#
# This module owns the pure orchestration: the DocumentState object, the stage
# sequence, and the dependency/consistency logic. The actual drafting/rendering
# stays in workers/doc_worker.py; this planner decides WHAT each stage does and
# tracks shared state. Pure → testable offline; caller falls back to today's
# single-pass generation on any failure.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# stage identifiers (docs/architecture/15 §15.5)
OUTLINE = "outline"
DRAFT = "draft"
CONSISTENCY = "consistency"
STYLE = "style"
RENDER = "render"

STAGES = [OUTLINE, DRAFT, CONSISTENCY, STYLE, RENDER]


@dataclass
class Section:
    id: str
    title: str
    budget_tokens: int = 500
    status: str = "pending"    # pending | drafted
    depends_on: List[str] = field(default_factory=list)


@dataclass
class DocumentState:
    """Shared, evolving state across sections (§15.4). `figures` is the anti-drift
    mechanism: a value stated once is reused verbatim, never re-generated."""

    doc_id: str = ""
    fmt: str = "docx"
    intent: str = ""
    audience: str = "general"
    length_target_tokens: int = 0
    outline: List[Section] = field(default_factory=list)
    defined_terms: Dict[str, str] = field(default_factory=dict)
    figures: Dict[str, str] = field(default_factory=dict)   # reused verbatim
    decisions: List[str] = field(default_factory=list)
    style_voice: str = "neutral"

    def set_figure(self, key: str, value: str) -> str:
        """Record a figure once; subsequent reads return the SAME value (prevents
        '99.95%' becoming '99.9%' three sections later)."""
        if key not in self.figures:
            self.figures[key] = value
        return self.figures[key]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id, "fmt": self.fmt, "intent": self.intent,
            "audience": self.audience, "sections": len(self.outline),
            "figures": dict(self.figures), "defined_terms": dict(self.defined_terms),
            "style_voice": self.style_voice,
        }


def draft_order(state: DocumentState) -> List[str]:
    """Topologically order sections by depends_on so a section is drafted only
    after its dependencies. Never raises; cycles fall back to declared order."""
    try:
        by_id = {s.id: s for s in state.outline}
        ordered: List[str] = []
        seen: set = set()

        def visit(sid: str, stack: set):
            if sid in seen or sid not in by_id:
                return
            if sid in stack:      # cycle → stop (fall back to declared order)
                return
            stack.add(sid)
            for dep in by_id[sid].depends_on:
                visit(dep, stack)
            stack.discard(sid)
            if sid not in seen:
                seen.add(sid)
                ordered.append(sid)

        for s in state.outline:
            visit(s.id, set())
        # append any missed (cyclic) sections in declared order
        for s in state.outline:
            if s.id not in seen:
                ordered.append(s.id)
        return ordered
    except Exception:  # noqa: BLE001
        return [s.id for s in state.outline]


def consistency_issues(state: DocumentState, section_texts: Dict[str, str]) -> List[str]:
    """Detect cross-section drift: a figure value that appears CONTRADICTED in a
    section's text (same key, different value). Returns human-readable issues.
    Pure heuristic; the real fix is drafting from state.figures in the first place."""
    issues: List[str] = []
    try:
        for key, canonical in (state.figures or {}).items():
            for sid, text in (section_texts or {}).items():
                # if the key is mentioned but the canonical value is absent →
                # possible drift (a different value may have been generated).
                if key.lower() in (text or "").lower() and canonical and canonical not in (text or ""):
                    issues.append(f"section {sid}: '{key}' may drift from canonical '{canonical}'")
    except Exception:  # noqa: BLE001
        pass
    return issues


def next_stage(current: Optional[str]) -> Optional[str]:
    """Return the stage after `current` (None → first stage; last → None)."""
    if current is None:
        return STAGES[0]
    try:
        i = STAGES.index(current)
        return STAGES[i + 1] if i + 1 < len(STAGES) else None
    except ValueError:
        return STAGES[0]
