# SPDX-License-Identifier: MIT
"""Data model for the context benchmark: transcripts, probes, and eval cases.

Everything here is plain dataclasses so eval cases can be written by hand in
Python or loaded from JSON — no framework, no I/O, fully deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional

Role = Literal["user", "assistant"]

# Probe kinds mirror docs §6:
#   recall     — a fact stated once; must survive into context.
#   override   — a fact stated, then updated later; the NEW value must be the
#                one available, and the OLD value must NOT silently override it.
#   distractor — many similar-but-irrelevant turns; the ONE relevant turn must
#                survive despite the noise.
#   longctx    — transcript large enough to force overflow/compaction.
ProbeKind = Literal["recall", "override", "distractor", "longctx"]


@dataclass
class Turn:
    """A single conversation turn."""
    role: Role
    content: str
    # Optional stable tag so a probe can point at the exact turn(s) that carry
    # the answer without relying on fragile string matching.
    tag: str = ""
    # rag_mode of the turn as stored today ("off" == Generic). The current
    # strategy filters Generic reads to rag_mode=="off" turns only.
    rag_mode: str = "off"


@dataclass
class Probe:
    """A question asked at the END of a transcript, with a known answer."""
    kind: ProbeKind
    question: str
    # The substring that MUST appear in assembled context for the probe to be
    # answerable (the fresh/correct fact).
    answer_fact: str
    # For override probes: the stale value that must NOT be the only thing
    # present, or must not appear without its fresher counterpart. Optional.
    stale_fact: str = ""


@dataclass
class Case:
    """One eval case: a transcript plus one or more probes against it."""
    name: str
    turns: List[Turn]
    probes: List[Probe] = field(default_factory=list)
    # rag_mode of the *current* request (Generic == "off").
    rag_mode: str = "off"


@dataclass
class AssembledContext:
    """What a strategy decided to send to the model for one probe.

    `messages` is the ordered list of {role, content} the strategy would inject
    (excluding system prefaces / durable memory, which are always-injected and
    out of scope for the omission metric). `summary_used` flags compaction.
    """
    messages: List[dict]
    summary_used: bool
    approx_tokens: int

    def flat_text(self) -> str:
        return "\n".join((m.get("content") or "") for m in self.messages)
