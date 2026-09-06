# SPDX-License-Identifier: MIT
# ============================================================
# ConversationState — the Conversation Intelligence Layer's typed state
# ============================================================
#
# Replaces the "single intent label" model with a rich, typed state computed
# once per turn and read by every downstream stage. See
# docs/architecture/04-conversation-intelligence-layer.md §4.2 for the full
# schema; this is the Wave-2 subset (the dimensions the existing classifiers
# can already produce). Later waves add hidden_goal, emotional_tone, etc.
#
# WAVE 2 CONTRACT: shadow-populated behind PIPELINE_V2, carried on
# RequestContext.conv_state, read by nothing yet → inert when the flag is OFF.
#
# Pure stdlib only (importable in a bare test env). @dataclass house style.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Score:
    """A soft signal: a 0..1 score plus the categories/reason behind it."""

    score: float = 0.0
    tags: List[str] = field(default_factory=list)


@dataclass
class ConversationState:
    """Structured understanding of a single turn.

    Every field has a safe default equal to today's implicit behavior, so a CIL
    failure degrades to: medium complexity, general domain, no tools, no
    clarification — i.e. the current path.
    """

    # ── understanding ────────────────────────────────────────────────
    # task_complexity: today's classifier emits simple|medium|complex;
    # deep|solution are reserved for a richer classifier in a later wave.
    task_complexity: str = "medium"
    domain: str = "general"              # general|code|finance|hr|...
    is_continuation: bool = False
    ambiguity: Score = field(default_factory=Score)
    clarification_needed: bool = False

    # ── needs (drive routing / orchestration in later waves) ─────────
    output_format: str = "prose"         # prose|code|table|document|data
    tool_need: Score = field(default_factory=Score)
    retrieval_need: Score = field(default_factory=Score)
    freshness_need: str = "none"         # none|low|high

    # ── intent & routing (model-derived; Wave-3 model-intent) ────────
    # Populated by the model-based classifier (cil/intent.py). intent drives the
    # gateway's skill/agent fork; skill_hint/agent_hint name the target when the
    # model recognises one. Defaults ("chat"/None) = plain conversational answer.
    intent: str = "chat"                 # chat|skill|agent|analyse
    skill_hint: Optional[str] = None
    agent_hint: Optional[str] = None
    intent_conf: float = 0.0

    # ── document-generation intent (merged from models/doc_intent.py) ─
    # When PIPELINE_V2 + Auto model, the CIL classifier also decides whether the
    # user wants a downloadable document. This lets gateway.py skip the separate
    # doc-intent LLM call. Defaults mirror a plain chat answer.
    doc_intent: str = "none"             # generate|summarize|convert|extract|compare|revise|none
    doc_format: Optional[str] = None     # pdf|docx|pptx|xlsx|csv|md|txt
    doc_source_scope: str = "none"       # uploaded|chat|artifact|none
    doc_target_artifact_id: Optional[str] = None
    doc_needs_topic: bool = False
    doc_topic: str = ""
    doc_confidence: float = 0.0
    doc_reason: str = ""

    # ── image-generation intent ──────────────────────────────────────
    # Parallel to doc_intent. Populated when the user wants a NEW image as
    # output. img_source_scope encodes the input modality:
    #   "none"             — fresh generation from user's text only
    #   "uploaded"         — base the new image on the uploaded image's Vision description
    #   "chat"             — base the new image on the conversation context
    #   "uploaded_and_chat"— use both the uploaded image description AND the conversation
    img_intent: str = "none"             # "generate" | "none"
    img_source_scope: str = "none"       # "uploaded" | "chat" | "uploaded_and_chat" | "none"
    img_prompt: str = ""                 # visual description for the image model
    img_confidence: float = 0.0
    img_reason: str = ""

    # ── video-generation intent ──────────────────────────────────────
    # Parallel to img_intent. Populated when the user wants a NEW video as
    # output via Google Veo 3.1. vid_source_scope encodes the input modality:
    #   "none"     — fresh generation from user's text only
    #   "uploaded" — base the video on an uploaded image/doc (used as visual reference)
    #   "chat"     — base the video on the conversation context
    # vid_aspect_ratio / vid_duration_secs carry the user's preferred Veo parameters
    # (validated and clamped by the routing block before forwarding to /chat/video-generate).
    # Supported aspect ratios: 16:9 | 9:16 | 1:1 | 4:3 | 3:4
    # Supported duration: 4–8 seconds — product policy, see cil/intent.py
    # _VID_MIN_DURATION/_VID_MAX_DURATION (clamped server-side in both the
    # gateway routing block and /chat/video-generate).
    vid_intent: str = "none"             # "generate" | "none"
    vid_source_scope: str = "none"       # "uploaded" | "chat" | "none"
    vid_prompt: str = ""                 # visual description for the video model
    vid_confidence: float = 0.0
    vid_reason: str = ""
    vid_aspect_ratio: str = "16:9"       # 16:9 | 9:16 | 1:1 | 4:3 | 3:4
    vid_duration_secs: int = 8           # 4..8 (clamped by routing block)

    # ── style / persona signals (Persona & Style layer) ──────────────
    # Defaults are neutral so a CIL/model failure yields today's tone.
    tone: str = "neutral"                # formal|neutral|casual|frustrated|excited
    formality: float = 0.5               # 0 = very casual, 1 = very formal
    language: str = "en"                 # en|hi|hinglish|ta|...
    sentiment: str = "neutral"           # neg|neutral|pos
    wants_brief: bool = False

    # ── provenance ───────────────────────────────────────────────────
    classifier_conf: float = 0.0
    # "model" when the model-based classifier produced this state; "default" when
    # it fell back to safe static defaults (model unavailable). NEVER "lexical" —
    # the chat intent path is regex-free.
    intent_source: str = "default"
    signal_sources: List[str] = field(default_factory=list)  # e.g. ["model"]
    analyze_ms: float = 0.0

    def snapshot(self) -> Dict[str, Any]:
        """Flat scalar dict for span attributes / telemetry."""
        return {
            "task_complexity": self.task_complexity,
            "domain": self.domain,
            "is_continuation": self.is_continuation,
            "ambiguity": self.ambiguity.score,
            "clarification_needed": self.clarification_needed,
            "output_format": self.output_format,
            "tool_need": self.tool_need.score,
            "retrieval_need": self.retrieval_need.score,
            "freshness_need": self.freshness_need,
            "intent": self.intent,
            "skill_hint": self.skill_hint or "",
            "agent_hint": self.agent_hint or "",
            "intent_conf": self.intent_conf,
            "tone": self.tone,
            "formality": self.formality,
            "language": self.language,
            "sentiment": self.sentiment,
            "wants_brief": self.wants_brief,
            "intent_source": self.intent_source,
            "classifier_conf": self.classifier_conf,
            "sources": ",".join(self.signal_sources),
            "img_intent": self.img_intent,
            "img_source_scope": self.img_source_scope,
            "img_confidence": self.img_confidence,
            "vid_intent": self.vid_intent,
            "vid_source_scope": self.vid_source_scope,
            "vid_confidence": self.vid_confidence,
        }

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)
