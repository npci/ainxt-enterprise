# SPDX-License-Identifier: MIT
# ============================================================
# UNIFIED CONVERSATION INTENT  (MODEL-ONLY — ZERO REGEX)
#
# The Conversation Intelligence Layer's understanding step. Turns a raw user
# turn into a structured ConversationState using ONE fast model call — NOT
# keyword/regex heuristics. This is what makes the layer an "intelligence"
# layer instead of a prompt->llm->UI chatbot: the model actually UNDERSTANDS
# whether the turn is trivial vs. deep, a follow-up vs. new, needs tools /
# retrieval / freshness, and whether it should route to a skill/agent.
#
# Modeled on models/doc_intent.py (the proven model-only classifier in this
# codebase): strict JSON schema, robust JSON recovery, enum validation, and a
# fail-safe that NEVER raises.
#
# HARD RULES:
#   * NO regex / keyword intent detection anywhere in this module.
#   * classify() returns None on ANY failure (model outage, bad JSON) so the
#     caller (cil/analyze.py) degrades to SAFE STATIC DEFAULTS (medium/general/
#     chat) — never to regex, and never a crash.
#   * doc-generation intent is NOT decided here — models/doc_intent.py remains
#     the sole doc authority. `route` here is a conversational route only.
# ============================================================

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

from core.logger import logger

try:
    from core.config import CIL_INTENT_MODEL as _INTENT_MODEL  # optional central config
except Exception:  # noqa: BLE001
    import os as _os
    _INTENT_MODEL = _os.getenv("CIL_INTENT_MODEL", "local_mini")  # local_mini tier — model configured via OPENAI_OSS_MODEL in core/model_registry.py

# ── Classification cache (PERF) ────────────────────────────────────────────
# classify() previously called the local LLM unconditionally on every turn,
# even for an identical repeated question (double-submits, retried requests,
# "same question, different chat" testing). Cache the full UnifiedIntent
# result keyed on a hash of every input that affects the classification —
# text, rag_mode, has_attachments, doc_memory_summary, has_chat_context,
# recent_turns, include_doc_intent — so a cache HIT is provably the same
# answer the model would have given, not a stale approximation. Set
# CIL_INTENT_CACHE_ENABLED=false to disable without a deploy.
_CACHE_ENABLED = os.getenv("CIL_INTENT_CACHE_ENABLED", "true").lower() == "true"
_CACHE_TTL = int(os.getenv("CIL_INTENT_CACHE_TTL", "3600") or "3600")  # 1h default


def _cache_key(text: str, *, rag_mode: str, history_summary: str,
              has_attachments: bool, doc_memory_summary: str,
              has_chat_context: bool, recent_turns: Optional[list],
              include_doc_intent: bool,
              include_img_intent: bool = True,
              include_vid_intent: bool = True,
              attachment_kinds: Optional[list] = None) -> str:
    # recent_turns is the same [-5:] slice classify() itself uses for the
    # prompt, so the key exactly matches what would be sent to the model.
    _turns_sig = ""
    if recent_turns:
        _turns_sig = "|".join(
            f"{t.get('role', '?')}:{str(t.get('content') or '')[:200]}"
            for t in recent_turns[-5:]
        )
    # attachment_kinds (e.g. ["image"], ["document"]) — NOT the attachment
    # content — changes what the classifier is told ("Attachments present:
    # yes (kind: image)" vs "yes (kind: document)"), so it must be part of
    # the cache key or two different attachment kinds could collide on a
    # stale cached result.
    _kinds_sig = ",".join(sorted(set(str(k) for k in (attachment_kinds or []) if k)))
    # v8: doc/img/vid guidance rewritten — doc-intent now carries explicit intent
    # DEFINITIONS and the "questions ABOUT a document are chat answers" rule, and
    # img/vid guidance asks the generation question directly. v7 entries were
    # classified without any of that: the turn "what does it say about wind and
    # precipitation" cached as doc_intent='extract' conf=0.60 (it should be
    # 'none'), and a cache HIT returns before the model is ever consulted, so the
    # corrected prompt would have had no effect on the very question that
    # motivated it. Bump invalidates them immediately rather than waiting out TTL.
    # v7: vid_duration_secs range narrowed from 2–16 to 4–8 (see _VID_MIN_DURATION).
    # v6 entries were cached under the old guidance and can hold a duration of 2,
    # 12 or 16, which is now out of policy — a cache HIT would replay an illegal
    # duration straight past classify()'s clamp. Bumping the version invalidates
    # them instead of relying on the 1h TTL to age them out.
    # v6: cache key now also varies on attachment_kinds (see above).
    # v5: corrected img_intent guidance — "improve this image" / "redesign this" /
    # "make this look better" WITH an image attached now correctly routes to
    # img_intent="generate" + img_source_scope="uploaded" so the gateway fetches
    # parsed_text (Vision description) and builds an enriched prompt for the image
    # model. Only pure text-feedback requests ("give me feedback", "what's wrong",
    # "analyze this") remain img_intent="none". Invalidates v4 entries that
    # incorrectly cached img_intent="none" for visual-improvement requests.
    raw = (
        f"v8:{text.strip().lower()}\n"
        f"rag={rag_mode}|hist={history_summary}|att={has_attachments}|"
        f"attkinds={_kinds_sig}|"
        f"docmem={doc_memory_summary}|chatctx={has_chat_context}|"
        f"turns={_turns_sig}|doc_intent={include_doc_intent}|img_intent={include_img_intent}|"
        f"vid_intent={include_vid_intent}"
    )
    return "cil:intent:" + hashlib.sha256(raw.encode()).hexdigest()

_VALID_ROUTES = {"chat", "skill", "agent", "analyse"}
_VALID_COMPLEXITY = {"simple", "medium", "complex", "deep", "solution"}
_VALID_FRESHNESS = {"none", "low", "high"}
_VALID_FORMATS = {"prose", "code", "table", "document", "data"}
_VALID_TONE = {"formal", "neutral", "casual", "frustrated", "excited"}
_VALID_SENTIMENT = {"neg", "neutral", "pos"}
_VALID_DOC_INTENTS = {"generate", "summarize", "convert", "extract", "compare", "revise", "none"}
_VALID_DOC_FORMATS = {"pdf", "docx", "pptx", "xlsx", "csv", "md", "txt"}
_VALID_DOC_SCOPES = {"uploaded", "chat", "artifact", "none"}
_VALID_IMG_INTENTS = {"generate", "none"}
_VALID_IMG_SCOPES = {"uploaded", "chat", "uploaded_and_chat", "none"}
_VALID_VID_INTENTS = {"generate", "none"}
_VALID_VID_SCOPES  = {"uploaded", "chat", "none"}
_VALID_VID_ASPECTS = {"16:9", "9:16", "1:1", "4:3", "3:4"}

# Video duration is a PRODUCT policy, not a provider limit: Veo accepts a wider
# range, but web chat only ever asks for 4–8 s so the per-request Veo cost (per
# SECOND, ~$0.40/s) and the proxy LRO timeout stay predictable. Enforced in four
# places, all derived from these two constants here or mirrored from them:
#   1. the CIL prompt schema + guidance (below)   — the model is told 4..8
#   2. classify()'s clamp (below)                 — model garbage can't escape
#   3. gateway.py VIDEO-INTENT ROUTING            — re-clamps before handoff
#   4. routers/chat_router.py /chat/video-generate — authoritative server clamp
# The frontend (Chat.jsx / KbChat.jsx extractDurationFromPrompt) mirrors the same
# range so an out-of-range ask is corrected client-side too.
_VID_MIN_DURATION = 4
_VID_MAX_DURATION = 8
_VID_DEFAULT_DURATION = 8


@dataclass
class UnifiedIntent:
    task_complexity: str = "medium"
    domain: str = "general"
    is_continuation: bool = False
    output_format: str = "prose"
    tool_need: float = 0.0
    retrieval_need: float = 0.0
    freshness_need: str = "none"
    route: str = "chat"                 # chat|skill|agent|analyse
    skill_hint: Optional[str] = None
    agent_hint: Optional[str] = None
    clarification_needed: bool = False
    # ── style/persona signals (Persona & Style layer A) ──────────────
    tone: str = "neutral"               # formal|neutral|casual|frustrated|excited
    formality: float = 0.5              # 0 = very casual, 1 = very formal
    language: str = "en"                # e.g. en | hinglish | hi | ta ...
    sentiment: str = "neutral"          # neg|neutral|pos
    wants_brief: bool = False           # user seems to want a short answer
    confidence: float = 0.5
    reason: str = ""
    raw: dict = field(default_factory=dict)

    # ── document-generation intent (merged from models/doc_intent.py) ─
    doc_intent: str = "none"             # generate|summarize|convert|extract|compare|revise|none
    doc_format: Optional[str] = None     # pdf|docx|pptx|xlsx|csv|md|txt
    doc_source_scope: str = "none"       # uploaded|chat|artifact|none
    doc_target_artifact_id: Optional[str] = None
    doc_needs_topic: bool = False
    doc_topic: str = ""
    doc_confidence: float = 0.0
    doc_reason: str = ""

    # ── image-generation intent ───────────────────────────────────────
    img_intent: str = "none"             # generate|none
    img_source_scope: str = "none"       # uploaded|chat|uploaded_and_chat|none
    img_prompt: str = ""                 # visual description for the image model
    img_confidence: float = 0.0
    img_reason: str = ""

    # ── video-generation intent ───────────────────────────────────────
    vid_intent: str = "none"             # generate|none
    vid_source_scope: str = "none"       # uploaded|chat|none
    vid_prompt: str = ""                 # visual description for the video model
    vid_confidence: float = 0.0
    vid_reason: str = ""
    vid_aspect_ratio: str = "16:9"       # 16:9 | 9:16 | 1:1 | 4:3 | 3:4
    vid_duration_secs: int = 8           # 4..8 (product policy — see _VID_MIN/_MAX)


_BASE_SYS = (
    "You are the understanding stage of an enterprise assistant. Read the user's "
    "LATEST turn and classify it. Respond with ONLY a JSON object, no prose.\n"
    "Schema:\n"
    '{"task_complexity":"simple|medium|complex|deep|solution",'
    '"domain":"general|code|finance|hr|legal|data|devops|security",'
    '"is_continuation":true|false,'
    '"output_format":"prose|code|table|document|data",'
    '"tool_need":0.0-1.0,'
    '"retrieval_need":0.0-1.0,'
    '"freshness_need":"none|low|high",'
    '"route":"chat|skill|agent|analyse",'
    '"skill_hint":"<skill name or null>",'
    '"agent_hint":"<agent name or null>",'
    '"clarification_needed":true|false,'
    '"tone":"formal|neutral|casual|frustrated|excited",'
    '"formality":0.0-1.0,'
    '"language":"<lang tag: en|hi|hinglish|ta|...>",'
    '"sentiment":"neg|neutral|pos",'
    '"wants_brief":true|false,'
    '"confidence":0.0-1.0}\n'
)

_DOC_INTENT_SYS = (
    '"doc_intent":"generate|summarize|convert|extract|compare|revise|none",'
    '"doc_format":"pdf|docx|pptx|xlsx|csv|md|txt",'
    '"doc_source_scope":"uploaded|chat|artifact|none",'
    '"doc_target_artifact_id":"<id or null>",'
    '"doc_needs_topic":true|false,'
    '"doc_topic":"<subject or empty>",'
    '"doc_confidence":0.0-1.0,'
)

_GUIDANCE = (
    "\n"
    "GUIDANCE:\n"
    "- task_complexity: default 'medium'. Use 'simple' ONLY for greetings, "
    "small-talk, or one-line trivia. Use 'complex'/'deep'/'solution' for "
    "multi-step reasoning, architecture, or code spanning several files.\n"
    "- is_continuation: true if the turn depends on the prior conversation "
    "(pronouns like 'it/that', 'also', 'what about', an unfinished thread).\n"
    "- tool_need / retrieval_need: how much the turn needs external tools "
    "(APIs, connectors, browsing) or document/code retrieval to answer well.\n"
    "- freshness_need: 'high' if it needs current/live info (today, latest, "
    "prices, news), else 'none'.\n"
    "- route: 'chat' for a normal answer (the default). 'skill' only if the "
    "user clearly wants a named platform skill to run; 'agent' only for a named "
    "autonomous agent; 'analyse' for deep multi-step analysis of provided data. "
    "When unsure, use 'chat'.\n"
    "- skill_hint / agent_hint: the specific skill/agent name ONLY when route is "
    "skill/agent and you can name it; else null. Do NOT invent names.\n"
    "- clarification_needed: true ONLY when the request is too ambiguous to "
    "answer safely and a follow-up question is genuinely required.\n"
    "- tone/formality: read how the USER writes. Slang, contractions, 'u/plz/"
    "hey', emojis → casual + low formality; full sentences, 'please/kindly' → "
    "formal + high formality. 'frustrated' if annoyed, 'excited' if enthusiastic.\n"
    "- language: the language/register the user wrote in (en, hi, hinglish, ta, "
    "…). Detect code-switching (e.g. English+Hindi = 'hinglish').\n"
    "- sentiment: overall emotional valence of the user's message.\n"
    "- wants_brief: true if they signal they want it short ('quickly', 'in short', "
    "'tl;dr', 'just tell me') or the ask is trivially small.\n"
    "\n"
    "THE THREE GENERATION QUESTIONS — answer each one explicitly.\n"
    "Before filling in doc_intent / img_intent / vid_intent, ask yourself these "
    "three questions separately and literally. The default answer to all three "
    "is NO:\n"
    "  Q1. Is the user asking me to PRODUCE A FILE (document/deck/sheet) as the "
    "deliverable?                       -> if NO, doc_intent='none'\n"
    "  Q2. Is the user asking me to PRODUCE AN IMAGE (a new picture) as the "
    "deliverable?                        -> if NO, img_intent='none'\n"
    "  Q3. Is the user asking me to PRODUCE A VIDEO (a moving clip) as the "
    "deliverable?                        -> if NO, vid_intent='none'\n"
    "\n"
    "  A question, a request for an explanation, a request for feedback, or any "
    "ask whose answer is TEXT means NO to all three — even when a file, image or "
    "video is attached, and even when one was produced earlier in this "
    "conversation. Existing attachments and previously generated artifacts are "
    "CONTEXT to answer FROM; they are never a reason to produce a new artifact. "
    "Their content (document text, image description) is injected into the "
    "prompt automatically, so answering in chat is always possible.\n"
    "  Say YES only when the user's own words ask for the artifact: 'make/create/"
    "generate/draw/animate/export/convert it to ...'. If you are unsure, the "
    "answer is NO — an unwanted answer in chat is a minor annoyance, an unwanted "
    "generated file/image/video wastes the user's time and money.\n"
    "\n"
    "INTENT RELATIONSHIPS (doc / img / vid):\n"
    "- doc_intent is mutually exclusive with both img_intent and vid_intent. "
    "A document-generation turn never also generates an image or video.\n"
    "- img_intent and vid_intent are independent — set each based solely on "
    "what the user is asking for:\n"
    "  * Still image output (draw, generate image, improve/redesign/enhance this image) "
    "→ img_intent='generate', vid_intent='none'.\n"
    "  * Video output (animate, make a video, create a clip, turn this into a video) "
    "→ vid_intent='generate', img_intent='none'.\n"
    "  * Both explicitly requested → set both.\n"
    "  * When an image is attached and the user says 'animate this' / 'make a video of "
    "this' → vid_intent='generate' + vid_source_scope='uploaded', img_intent='none'.\n"
)

_IMG_INTENT_SYS = (
    '"img_intent":"generate|none",'
    '"img_source_scope":"uploaded|chat|uploaded_and_chat|none",'
    '"img_prompt":"<visual description or empty>",'
    '"img_confidence":0.0-1.0,'
)

_IMG_INTENT_GUIDANCE = (
    "- img_intent: default 'none'. Set to 'generate' when the user wants a NEW IMAGE "
    "as output. This includes:\n"
    "  GENERATE (use img_intent='generate'):\n"
    "    * Explicit creation: draw, generate, create, render, make an image, paint\n"
    "    * Variation/redraw: redraw this, generate a new version, create a variation\n"
    "    * Visual improvement WITH an image attached: improve this image, make this "
    "look better, enhance this design, redesign this, modernise this UI, update the "
    "styling, make it more professional, improve the layout, restyle this — when an "
    "image is attached the user wants a NEW improved image as output.\n"
    "  NOT GENERATE (use img_intent='none') — text advice/feedback only:\n"
    "    * 'give me feedback on this' / 'what should I change' / 'review this'\n"
    "    * 'analyze this screenshot' / 'what's wrong with this' / 'critique this'\n"
    "    * 'how can I make this better' / 'suggest improvements' / 'what do you think'\n"
    "    * 'explain this image' / 'describe what you see' / 'what is in this image'\n"
    "    * Any request for TEXT ADVICE, CODE CHANGES, or WRITTEN FEEDBACK about an image.\n"
    "  KEY DISTINCTION: if the user attached an image and uses action verbs like "
    "'improve', 'enhance', 'redesign', 'modernise', 'update', 'make better', "
    "'restyle' → they want a NEW IMAGE (img_intent='generate', img_source_scope='uploaded'). "
    "If they ask for opinions, feedback, or analysis → img_intent='none'.\n"
    "  These are analysis/chat requests — the image's Vision description is injected "
    "into the prompt automatically and the model answers with text.\n"
    "- img_source_scope: how to source the content for the new image.\n"
    "  'uploaded' — an image is attached AND the user wants a new image based on it "
    "(redraw, improve, generate a variation, create a new version). The gateway will "
    "prepend the original's Vision description automatically — do NOT repeat it in "
    "img_prompt.\n"
    "  'chat' — the user wants an image based on the conversation topic "
    "(e.g. 'make an image of what we discussed').\n"
    "  'uploaded_and_chat' — both apply: image attached AND the conversation provides "
    "additional context for the new image.\n"
    "  'none' — fresh generation from the user's text alone (no attachment, no chat context).\n"
    "- img_prompt: the visual description to send to the image model. "
    "When img_source_scope includes 'uploaded', describe the desired output "
    "style/improvements only — NOT the original image (the gateway prepends the "
    "original's Vision description automatically). When img_source_scope='none', "
    "this is the full visual prompt.\n"
    "- img_confidence: how confident you are that this is an image-generation request. "
    "Set >= 0.65 for any prompt that contains an image-output noun (image, photo, "
    "picture, illustration, poster, thumbnail, banner, logo) alongside a generation "
    "verb or a bare noun+image-word pattern. The routing gate is > 0.5, so anything "
    "clearly image-related should be >= 0.65.\n"
    "  SHORT-FORM EXAMPLES — always img_intent='generate', img_source_scope='none':\n"
    "    * 'generate elephant image' → img_intent='generate'\n"
    "    * 'create lion photo' → img_intent='generate'\n"
    "    * 'make a cat image' → img_intent='generate'\n"
    "    * 'draw a tiger' → img_intent='generate'\n"
    "    * 'elephant image' → img_intent='generate'\n"
    "    * 'dog picture' → img_intent='generate'\n"
    "    * 'generate a red sports car image' → img_intent='generate'\n"
    "    * 'show me a sunset photo' → img_intent='generate'\n"
    "    * 'design a product launch poster' → img_intent='generate'\n"
    "    * 'create a thumbnail for my YouTube video' → img_intent='generate'\n"
    "  Pattern: [verb] + [subject noun(s)] + [image word] = image generation.\n"
    "  Even a bare noun + image word ('elephant image', 'cat photo') with no verb\n"
    "  is an image generation request — set img_intent='generate'.\n"
    "  NOT image generation: 'draw a conclusion', 'paint a picture of the situation'\n"
    "  (figurative language) — set img_intent='none' for these.\n"
    "  DISAMBIGUATION — image vs video: if the prompt contains BOTH image and video\n"
    "  keywords, use the OUTPUT TYPE the user is asking for:\n"
    "    * 'generate an image of a video game character' → img_intent='generate'\n"
    "      (output is a still image; 'video game' is context, not output type)\n"
    "    * 'create a video of a character from this image' → vid_intent='generate'\n"
    "      (output is a video; 'image' is the source, not the output type)\n"
)

_VID_INTENT_SYS = (
    '"vid_intent":"generate|none",'
    '"vid_source_scope":"uploaded|chat|none",'
    '"vid_prompt":"<visual description or empty>",'
    '"vid_confidence":0.0-1.0,'
    '"vid_aspect_ratio":"16:9|9:16|1:1|4:3|3:4",'
    f'"vid_duration_secs":{_VID_MIN_DURATION}-{_VID_MAX_DURATION},'
)

_VID_INTENT_GUIDANCE = (
    "- vid_intent: default 'none'. Set to 'generate' when the user wants a NEW VIDEO "
    "as output. This includes BOTH direct commands AND structured production briefs:\n"
    "  GENERATE (use vid_intent='generate', vid_confidence >= 0.65):\n"
    "    * Direct commands: animate, generate a video, create a video, make a video,\n"
    "      record, film, produce a clip, show as video, render a clip.\n"
    "    * Structured video briefs: prompts that contain scene headings, shot\n"
    "      descriptions, timecodes (e.g. '0–3 seconds', '3–6 seconds'), camera\n"
    "      directions (slow pan, close-up, wide shot), lighting notes (warm lighting,\n"
    "      natural light), or cinematic style keywords (photorealistic, cinematic,\n"
    "      premium commercial quality) — these are video production briefs even if\n"
    "      the user does not say 'make a video' in those exact words.\n"
    "    * 'generate below video', 'create this video', 'produce this scene',\n"
    "      'animate this', 'make a reel of', 'create a clip of'.\n"
    "    * Any prompt whose structure is a scene description: 'Scene 1: [location]\n"
    "      ([timecode]) Prompt: [cinematic description]' is ALWAYS a video request.\n"
    "  NOT GENERATE (use vid_intent='none'):\n"
    "    * Asking for a video script, storyboard, or written description only.\n"
    "    * Asking how to make a video (tutorial/advice), not to produce one.\n"
    "    * Figurative use: 'paint a picture', 'draw a conclusion'.\n"
    "  EXAMPLES — always vid_intent='generate':\n"
    "    * 'Please generate below video Scene 1: The Pharmacy (0–3 seconds) Prompt:\n"
    "      Cinematic, photorealistic shot. A 65-year-old Indian woman stands at a\n"
    "      pharmacy counter…' → vid_intent='generate' (video production brief)\n"
    "    * 'generate a 5 second video of a sunset over the ocean' → vid_intent='generate'\n"
    "    * 'make a video of a dog running in a park' → vid_intent='generate'\n"
    "    * 'animate this image' → vid_intent='generate', vid_source_scope='uploaded'\n"
    "    * 'create a reel showing our product launch' → vid_intent='generate'\n"
    "    * 'Scene 1 (0–3s): Wide shot of a busy street. Scene 2 (3–6s): Close-up\n"
    "      of a shopfront sign.' → vid_intent='generate' (timecoded scene brief)\n"
    "- vid_source_scope: 'uploaded' if an image/doc is attached and the user wants "
    "the video based on it; 'chat' if the video should be based on the conversation "
    "topic; 'none' for fresh generation from the user's text alone.\n"
    "- vid_prompt: the visual description to send to the video model. Full scene "
    "description including motion, style, lighting, camera angle. When the user "
    "provides a structured brief (scene headings, timecodes, shot descriptions), "
    "consolidate it into a single cohesive visual description.\n"
    "- vid_confidence: how confident you are that this is a video-generation request. "
    "Set >= 0.65 for any prompt that contains scene headings, timecodes, camera "
    "directions, or cinematic style keywords alongside a generation verb or the "
    "phrase 'generate/create/produce [the] video'. The routing gate is > 0.5, so "
    "anything clearly video-related should be >= 0.65.\n"
    "- vid_aspect_ratio: infer from context — landscape/widescreen → '16:9' (default); "
    "portrait/mobile/reel/story → '9:16'; square/social → '1:1'. Default '16:9'.\n"
    f"- vid_duration_secs: MUST be an integer between {_VID_MIN_DURATION} and "
    f"{_VID_MAX_DURATION} inclusive — never outside that range, even if the user "
    f"asks for a longer or shorter video. Infer within the range: 'short clip' / "
    f"'quick' / 'brief' / 'a few seconds' → {_VID_MIN_DURATION}; "
    f"'long' / 'longer' / 'detailed' / 'full' → {_VID_MAX_DURATION}; "
    f"no duration cue → {_VID_DEFAULT_DURATION}. If the user names an explicit "
    f"duration, clamp it into the range (e.g. '2 seconds' → {_VID_MIN_DURATION}, "
    f"'30 seconds' → {_VID_MAX_DURATION}).\n"
)

_DOC_INTENT_GUIDANCE = (
    "- doc_intent: default 'none'. Set to a document intent ONLY when the user "
    "clearly wants a DOWNLOADABLE FILE artifact. A word like 'compare', "
    "'summarize', 'report', 'document', 'write' or 'file' appearing somewhere "
    "does NOT by itself mean the user wants a file. The deliverable must be a "
    "downloadable file. When in doubt, use 'none'.\n"
    "\n"
    "  QUESTIONS *ABOUT* A DOCUMENT ARE CHAT ANSWERS — doc_intent='none'.\n"
    "  This is the single most important rule here. When an attachment is "
    "present, or a document was generated earlier in this conversation, and the "
    "user asks ANYTHING ABOUT it, they want the answer IN THE CHAT REPLY, not a "
    "new file. Its full text is injected into the prompt automatically, so you "
    "can simply answer. All of these are doc_intent='none':\n"
    "    * 'what does it say about X' / 'what does the report say' / 'does it "
    "mention Y' / 'summarise it' / 'give me the key points' / 'explain this "
    "file' / 'what were the figures for Q3'\n"
    "    * 'analyze this' / 'review this' / 'what do you think of it'\n"
    "    * ANY follow-up question about a document that already exists.\n"
    "  An existing document (attached OR previously generated) is NEVER by "
    "itself a reason to produce another document. Only choose a document intent "
    "when THIS turn explicitly asks for a NEW FILE as the deliverable — e.g. "
    "'summarise it INTO A PDF', 'put that in a word doc', 'export as xlsx', "
    "'now make a deck from it'.\n"
    "  'What does the weather report say about wind?' -> 'none' (answer in chat).\n"
    "  'Turn the weather report into a PDF.'          -> convert -> pdf.\n"
    "\n"
    "  INTENT MEANINGS — apply ONLY once you have decided the user genuinely "
    "wants a new downloadable FILE. Each of these produces a FILE:\n"
    "    * generate  — author a NEW file on a subject ('make a ppt on X').\n"
    "    * summarize — condense an upload / the chat / a prior doc INTO A FILE.\n"
    "    * convert   — the SAME content in a different file format.\n"
    "    * extract   — pull/MERGE info FROM uploaded file(s) INTO A NEW FILE. "
    "NOTE: 'extract' does NOT mean answering a question or pulling a fact out "
    "for the chat reply — if the user just wants to KNOW something, that is "
    "'none'.\n"
    "    * compare   — DIFF two real documents INTO A REPORT FILE.\n"
    "    * revise    — change a document you already produced ('make the intro "
    "shorter').\n"
    "    * none      — NOT a file request; answer in the chat.\n"
    "- doc_format: when doc_intent is not 'none', choose one of pdf/docx/pptx/"
    "xlsx/csv/md/txt. deck/slides/presentation/'ppt' → pptx; report/letter/'doc'/"
    "'word' → docx; sheet/table/data/numbers → xlsx; plain data dump → csv; "
    "notes → md; otherwise → pdf.\n"
    "- doc_source_scope: 'uploaded' if acting on attached files; 'chat' if the "
    "document should be built from THIS conversation; 'artifact' if revising/"
    "converting a previously generated document; 'none' otherwise.\n"
    "  Use 'chat' whenever the user names no subject of their own and instead "
    "points back at the conversation with a deictic word — 'this', 'that', 'it', "
    "'the above', 'what we discussed', 'our chat' (also in other languages, e.g. "
    "Hinglish/Malayalam 'itha pathi', 'ithine pathi', 'iske baare mein'). This "
    "applies to doc_intent='generate' too: 'generate a pdf on this' after a "
    "conversation about Java means scope='chat' — the PDF is about Java. Only "
    "use 'none' when the user supplies their OWN subject (e.g. 'make a pdf about "
    "the history of Rome'), even if a conversation is present.\n"
    "- doc_target_artifact_id: when the user refers to a previously generated "
    "document, set this to its id from the PRIOR DOCUMENTS list.\n"
    "- doc_needs_topic: true when the user clearly wants a document (generate) "
    "but gave NO usable subject and there is no attachment, prior document, or "
    "live conversation to work from. In that case we must ASK what it should be "
    "about. Set FALSE when doc_source_scope='chat' — the conversation IS the "
    "subject, so never ask.\n"
    "  Restated: when doc_source_scope='chat' the conversation IS the subject; "
    "doc_needs_topic must be false.\n"
    "- doc_topic: the subject of the requested document in a few words.\n"
    "- doc_confidence: how confident you are that this is a document request.\n"
)


def _build_sys_prompt(*, include_doc_intent: bool = True,
                      include_img_intent: bool = True,
                      include_vid_intent: bool = True) -> str:
    """Build the CIL system prompt.

    Doc-intent, img-intent, and vid-intent schema blocks are included independently:
    - include_doc_intent: True when the gateway detected a document signal
      (format noun, slash command, attachment, prior doc session).
    - include_img_intent: True when the gateway detected an image signal
      (image keyword in text, or an image attachment present).
    - include_vid_intent: True when the gateway detected a video signal
      (video keyword in text).

    Keeping the three gates separate means a pure image request ("draw me a
    logo") gets the img-intent schema without the doc/vid-intent schemas, and a
    pure video request ("make a video") gets the vid-intent schema without the
    doc/img-intent schemas. All are included when multiple signals are present.
    """
    schema = _BASE_SYS
    extra_sys = ""
    extra_guidance = ""
    if include_doc_intent:
        extra_sys += _DOC_INTENT_SYS
        extra_guidance += _DOC_INTENT_GUIDANCE
    if include_img_intent:
        extra_sys += _IMG_INTENT_SYS
        extra_guidance += _IMG_INTENT_GUIDANCE
    if include_vid_intent:
        extra_sys += _VID_INTENT_SYS
        extra_guidance += _VID_INTENT_GUIDANCE
    if extra_sys:
        # _BASE_SYS ends with '"confidence":0.0-1.0}\n'
        schema = schema.replace(
            '"confidence":0.0-1.0}\n',
            extra_sys + '"confidence":0.0-1.0}\n',
        )
    return f"{schema}{_GUIDANCE}{extra_guidance}"


def classify(text: str, *, rag_mode: str = "off",
             history_summary: str = "",
             has_attachments: bool = False,
             doc_memory_summary: str = "",
             has_chat_context: bool = False,
             recent_turns: Optional[list] = None,
             include_doc_intent: bool = True,
             include_img_intent: bool = True,
             include_vid_intent: bool = True,
             attachment_kinds: Optional[list] = None) -> Optional[UnifiedIntent]:
    """Classify one turn with the fast model. Returns None on ANY failure so the
    caller degrades to safe static defaults (never regex, never a crash).

    PERF: results are cached (see _cache_key) so an identical turn — same text
    plus every input that can change the classification — doesn't pay for a
    fresh local-LLM round-trip (measured ~2.3s in production). The cache is a
    pure function of its inputs (no chat_id/user_id/time dependence), so a HIT
    is exactly the answer the model would give, not a staleness trade-off.

    attachment_kinds: e.g. ["image"], ["document"], ["image", "document"] —
    the `kind` column of each ChatAttachment referenced by this turn's
    attachment_ids. This tells the classifier WHAT TYPE of attachment is
    present ("Attachments present: yes (kind: image)") so it can reason about
    image-intent vs. doc-intent correctly. It intentionally carries NO
    attachment CONTENT (no parsed_text / image_description / image_caption) —
    those are read directly off the ChatAttachment row by the gateway's
    routing blocks (STEP 0 attachment-context injection and IMAGE-INTENT
    ROUTING in gateway.py), never passed through the intent classifier. This
    keeps the classifier prompt small/fast and avoids ever feeding
    attachment content into the intent-classification model call.
    """
    text = (text or "").strip()
    if not text:
        return None

    _ckey = None
    if _CACHE_ENABLED:
        try:
            from core.config import RDB_CACHE
            from core.kv import get_kv
            _ckey = _cache_key(
                text, rag_mode=rag_mode, history_summary=history_summary,
                has_attachments=has_attachments, doc_memory_summary=doc_memory_summary,
                has_chat_context=has_chat_context, recent_turns=recent_turns,
                include_doc_intent=include_doc_intent,
                include_img_intent=include_img_intent,
                include_vid_intent=include_vid_intent,
                attachment_kinds=attachment_kinds,
            )
            _cached_raw = get_kv(RDB_CACHE, decode_responses=True).get(_ckey)
            if _cached_raw:
                _cached = json.loads(_cached_raw)
                _hit_result = UnifiedIntent(**_cached)
                logger.info(
                    "[cil] cache HIT | key=%s "
                    "img_intent=%r img_conf=%.2f "
                    "vid_intent=%r vid_conf=%.2f "
                    "doc_intent=%r complexity=%r user_turn=%r",
                    _ckey[-16:],
                    _hit_result.img_intent, _hit_result.img_confidence,
                    _hit_result.vid_intent, _hit_result.vid_confidence,
                    _hit_result.doc_intent,
                    _hit_result.task_complexity,
                    text[:120],
                )
                return _hit_result
        except Exception as _cache_err:  # noqa: BLE001 — cache is best-effort only
            logger.debug(f"[cil] cache lookup failed (continuing uncached): {_cache_err}")

    try:
        from models.model_router import model_router
        ctx = ""
        if history_summary:
            ctx += f"\nCONVERSATION SO FAR (summary):\n{history_summary}\n"
        ctx += f"\nrag_mode: {rag_mode}\n"
        # State attachment PRESENCE and KIND only — never content. The actual
        # text (parsed_text / image_description / image_caption) is injected
        # separately into the main LLM prompt / image-gen prompt by the
        # gateway's routing blocks, not sent through this classifier.
        _kinds_clean = sorted(set(str(k) for k in (attachment_kinds or []) if k))
        if has_attachments and _kinds_clean:
            ctx += f"Attachments present: yes (kind: {', '.join(_kinds_clean)})\n"
        else:
            ctx += f"Attachments present: {'yes' if has_attachments else 'no'}\n"
        if doc_memory_summary:
            ctx += f"\nPRIOR DOCUMENTS in this conversation:\n{doc_memory_summary}\n"
        ctx += f"Live conversation present: {'yes' if has_chat_context else 'no'}\n"
        if recent_turns:
            ctx += "\nRECENT TURNS:\n"
            for _turn in recent_turns[-5:]:
                _role = _turn.get("role", "?")
                _content = str(_turn.get("content") or "")[:200]
                ctx += f"{_role}: {_content}\n"
        _sys = _build_sys_prompt(include_doc_intent=include_doc_intent,
                                 include_img_intent=include_img_intent,
                                 include_vid_intent=include_vid_intent)
        # ── Media-estimation preamble (Fix: structured scene-description prompts) ──
        # Ask the model to explicitly estimate whether the user is requesting a
        # video or image BEFORE producing the JSON. This chain-of-thought nudge
        # forces the model to reason about the OUTPUT TYPE first, then encode that
        # reasoning into vid_intent/img_intent with the correct confidence.
        # Without this, prompts that look like scene descriptions or production
        # briefs were classified as vid_intent="none" because the model treated
        # them as document/text tasks rather than video-generation requests.
        _media_estimation = (
            "\nMEDIA OUTPUT ESTIMATION (answer before producing JSON):\n"
            "Step 1 — Is the user asking for a VIDEO as output?\n"
            "  YES if any of these are true:\n"
            "    • The word 'video', 'clip', 'film', 'animate', 'animation', 'reel',\n"
            "      'footage', 'motion', 'cinematic', 'scene' appears AND the user\n"
            "      wants that as the deliverable (not just describing a concept).\n"
            "    • The prompt is a video production brief: scene headings, shot\n"
            "      descriptions, timecodes (e.g. '0–3 seconds'), camera directions,\n"
            "      lighting notes, or 'generate/create/produce [the] video'.\n"
            "    • The user says 'generate below video', 'create this video',\n"
            "      'make a video of', 'produce a clip', 'animate this'.\n"
            "  If YES → set vid_intent='generate', vid_confidence >= 0.65.\n"
            "Step 2 — Is the user asking for a STILL IMAGE as output?\n"
            "  YES if any of these are true:\n"
            "    • The words 'image', 'photo', 'picture', 'illustration', 'drawing',\n"
            "      'render', 'poster', 'thumbnail', 'banner', 'logo' appear AND the\n"
            "      user wants that as the deliverable.\n"
            "    • The user says 'generate/draw/create/make an image', 'show me a\n"
            "      photo of', 'design a poster', 'create a thumbnail'.\n"
            "  If YES → set img_intent='generate', img_confidence >= 0.65.\n"
            "Step 3 — If NEITHER, set both vid_intent='none' and img_intent='none'.\n"
            "IMPORTANT: A video production brief (scene descriptions, timecodes,\n"
            "camera directions) is ALWAYS a video request even if it does not use\n"
            "the exact phrase 'make a video'. Encode your Step 1/2 answer directly\n"
            "into vid_intent/img_intent in the JSON — do NOT output the steps.\n"
        )
        prompt = f"{_sys}{ctx}{_media_estimation}\nUSER TURN:\n{text}\n\nJSON:"
        # Log the user turn sent to the SLM so we can debug misclassifications
        # without needing to reproduce the exact request.
        logger.debug(
            "[cil] SLM input | model=%s user_turn=%r",
            _INTENT_MODEL, text[:300],
        )
        # model_router.generate() already cascades small->cloud on outage; a
        # TOTAL outage yields an 'Error:' sentinel which we treat as failure.
        raw = (model_router.generate(prompt, model_hint=_INTENT_MODEL,
                                     return_meta=False) or "").strip()
        # Log the raw SLM response — the single most useful thing for debugging
        # misclassifications (e.g. vid_intent="none" when it should be "generate").
        logger.debug("[cil] SLM raw response | %r", raw[:500] if raw else "")
        if not raw or raw.startswith("Error:"):
            raise RuntimeError(f"intent model unavailable ({raw[:80]!r})")

        data = _parse_json(raw)
        if not isinstance(data, dict):
            raise ValueError("intent JSON not an object")

        def _enum(v, valid, default):
            v = str(v or "").lower().strip()
            return v if v in valid else default

        def _clamp01(v):
            try:
                return max(0.0, min(1.0, float(v)))
            except Exception:  # noqa: BLE001
                return 0.0

        route = _enum(data.get("route"), _VALID_ROUTES, "chat")
        skill_hint = (data.get("skill_hint") or None)
        agent_hint = (data.get("agent_hint") or None)
        # Clamp obvious garbage: a skill/agent route with no name → chat. The
        # authoritative existence check is done by the gateway against the DB.
        if route == "skill" and not skill_hint:
            route = "chat"
        if route == "agent" and not agent_hint:
            route = "chat"

        # doc-intent normalisation
        _doc_intent = _enum(data.get("doc_intent"), _VALID_DOC_INTENTS, "none")
        _doc_format = _enum(data.get("doc_format"), _VALID_DOC_FORMATS, "")
        _doc_format = _doc_format if _doc_format else None
        _doc_scope = _enum(data.get("doc_source_scope"), _VALID_DOC_SCOPES, "none")
        _doc_target = data.get("doc_target_artifact_id")
        _doc_target = str(_doc_target).strip() if _doc_target else None

        # img-intent normalisation
        _img_intent = _enum(data.get("img_intent"), _VALID_IMG_INTENTS, "none")
        _img_scope = _enum(data.get("img_source_scope"), _VALID_IMG_SCOPES, "none")
        _img_prompt = str(data.get("img_prompt") or "").strip()
        _img_conf = _clamp01(data.get("img_confidence", 0.0))

        # vid-intent normalisation
        _vid_intent = _enum(data.get("vid_intent"), _VALID_VID_INTENTS, "none")
        _vid_scope = _enum(data.get("vid_source_scope"), _VALID_VID_SCOPES, "none")
        _vid_prompt = str(data.get("vid_prompt") or "").strip()
        _vid_conf = _clamp01(data.get("vid_confidence", 0.0))
        _vid_aspect = _enum(data.get("vid_aspect_ratio"), _VALID_VID_ASPECTS, "16:9")
        try:
            _vid_duration = max(_VID_MIN_DURATION, min(
                _VID_MAX_DURATION,
                int(data.get("vid_duration_secs") or _VID_DEFAULT_DURATION),
            ))
        except (TypeError, ValueError):
            _vid_duration = _VID_DEFAULT_DURATION

        # Exclusivity guard: doc beats everything (most expensive misroute).
        # img and vid are NO LONGER mutually exclusive — the LLM decides.
        # "animate this image" legitimately sets vid_intent="generate" +
        # vid_source_scope="uploaded" while img_intent stays "none".
        # "improve this image AND make a video" could set both; the gateway
        # routing blocks handle them in order (image first, then video).
        # Only doc zeroes out both because a doc-generation turn should never
        # also trigger image or video generation as a side-effect.
        if _doc_intent != "none":
            _img_intent = "none"
            _img_scope = "none"
            _img_prompt = ""
            _img_conf = 0.0
            _vid_intent = "none"
            _vid_scope = "none"
            _vid_prompt = ""
            _vid_conf = 0.0

        result = UnifiedIntent(
            task_complexity=_enum(data.get("task_complexity"), _VALID_COMPLEXITY, "medium"),
            domain=str(data.get("domain") or "general").lower().strip() or "general",
            is_continuation=bool(data.get("is_continuation", False)),
            output_format=_enum(data.get("output_format"), _VALID_FORMATS, "prose"),
            tool_need=_clamp01(data.get("tool_need")),
            retrieval_need=_clamp01(data.get("retrieval_need")),
            freshness_need=_enum(data.get("freshness_need"), _VALID_FRESHNESS, "none"),
            route=route,
            skill_hint=(str(skill_hint).strip() if skill_hint else None),
            agent_hint=(str(agent_hint).strip() if agent_hint else None),
            clarification_needed=bool(data.get("clarification_needed", False)),
            # style/persona signals — default to neutral on absence/garbage
            tone=_enum(data.get("tone"), _VALID_TONE, "neutral"),
            formality=_clamp01(data.get("formality", 0.5)),
            language=(str(data.get("language") or "en").lower().strip() or "en")[:16],
            sentiment=_enum(data.get("sentiment"), _VALID_SENTIMENT, "neutral"),
            wants_brief=bool(data.get("wants_brief", False)),
            confidence=_clamp01(data.get("confidence", 0.5)),
            reason="model",
            raw=data,
            # document-generation intent
            doc_intent=_doc_intent if include_doc_intent else "none",
            doc_format=_doc_format if include_doc_intent else None,
            doc_source_scope=_doc_scope if include_doc_intent else "none",
            doc_target_artifact_id=_doc_target if include_doc_intent else None,
            doc_needs_topic=bool(data.get("doc_needs_topic", False)) if include_doc_intent else False,
            doc_topic=str(data.get("doc_topic") or "").strip() if include_doc_intent else "",
            doc_confidence=_clamp01(data.get("doc_confidence", 0.5)) if include_doc_intent else 0.0,
            doc_reason="model" if include_doc_intent else "default",
            # image-generation intent — gated on include_img_intent independently
            # of include_doc_intent so a pure image request ("draw me a logo")
            # gets img_intent populated even when there is no doc signal.
            img_intent=_img_intent if include_img_intent else "none",
            img_source_scope=_img_scope if include_img_intent else "none",
            img_prompt=_img_prompt if include_img_intent else "",
            img_confidence=_img_conf if include_img_intent else 0.0,
            img_reason="model" if include_img_intent else "default",
            # video-generation intent — gated on include_vid_intent independently
            # so a pure video request ("make a video") gets vid_intent populated
            # even when there is no doc or image signal.
            vid_intent=_vid_intent if include_vid_intent else "none",
            vid_source_scope=_vid_scope if include_vid_intent else "none",
            vid_prompt=_vid_prompt if include_vid_intent else "",
            vid_confidence=_vid_conf if include_vid_intent else 0.0,
            vid_reason="model" if include_vid_intent else "default",
            vid_aspect_ratio=_vid_aspect if include_vid_intent else "16:9",
            vid_duration_secs=_vid_duration if include_vid_intent else _VID_DEFAULT_DURATION,
        )

        # Log the parsed media-intent fields at INFO so they appear in agent.log
        # without needing debug mode. This is the key diagnostic line for video/image
        # misrouting — mirrors the [cil] line the gateway logs after classify() returns,
        # but lives here so it's visible even when called outside the gateway (tests, etc.).
        logger.info(
            "[cil] classified | "
            "img_intent=%r img_conf=%.2f "
            "vid_intent=%r vid_conf=%.2f vid_scope=%r "
            "doc_intent=%r "
            "complexity=%r user_turn=%r",
            result.img_intent, result.img_confidence,
            result.vid_intent, result.vid_confidence, result.vid_source_scope,
            result.doc_intent,
            result.task_complexity,
            text[:120],
        )

        if _ckey is not None:
            try:
                from core.config import RDB_CACHE
                from core.kv import get_kv
                get_kv(RDB_CACHE, decode_responses=True).setex(
                    _ckey, _CACHE_TTL, json.dumps(asdict(result))
                )
            except Exception as _cache_werr:  # noqa: BLE001 — cache is best-effort only
                logger.debug(f"[cil] cache write failed (non-fatal): {_cache_werr}")

        return result
    except Exception as exc:  # noqa: BLE001 — never raise; signal fallback
        logger.warning(f"[cil] classify unavailable ({exc}); caller uses safe defaults")
        return None


def to_conversation_state(ui: UnifiedIntent, *, rag_mode: str = "off"):
    """Map a UnifiedIntent into a ConversationState (same shape analyze() returns)
    so every downstream reader is unchanged."""
    from cil.state import ConversationState, Score
    st = ConversationState()
    st.task_complexity = ui.task_complexity
    st.domain = ui.domain
    st.is_continuation = ui.is_continuation
    st.output_format = ui.output_format
    st.freshness_need = ui.freshness_need
    st.clarification_needed = ui.clarification_needed
    st.tool_need = Score(score=ui.tool_need,
                         tags=[ui.domain] if ui.domain != "general" else [])
    # retrieval_need only meaningful when the caller hasn't forced Generic mode
    if (rag_mode or "off").strip().lower() != "off":
        st.retrieval_need = Score(score=ui.retrieval_need)
    st.intent = ui.route
    st.skill_hint = ui.skill_hint
    st.agent_hint = ui.agent_hint
    st.intent_conf = ui.confidence
    # style/persona signals — only surfaced when tone detection is enabled;
    # otherwise leave the neutral ConversationState defaults (today's tone).
    import os as _os_t
    if _os_t.getenv("CIL_TONE_DETECT", "true").lower() == "true":
        st.tone = ui.tone
        st.formality = ui.formality
        st.language = ui.language
        st.sentiment = ui.sentiment
        st.wants_brief = ui.wants_brief
    st.classifier_conf = ui.confidence
    st.intent_source = "model"
    st.signal_sources = ["model"]
    # document-generation intent (merged from models/doc_intent.py)
    st.doc_intent = ui.doc_intent
    st.doc_format = ui.doc_format
    st.doc_source_scope = ui.doc_source_scope
    st.doc_target_artifact_id = ui.doc_target_artifact_id
    st.doc_needs_topic = ui.doc_needs_topic
    st.doc_topic = ui.doc_topic
    st.doc_confidence = ui.doc_confidence
    st.doc_reason = ui.doc_reason
    # image-generation intent
    st.img_intent = ui.img_intent
    st.img_source_scope = ui.img_source_scope
    st.img_prompt = ui.img_prompt
    st.img_confidence = ui.img_confidence
    st.img_reason = ui.img_reason
    # video-generation intent
    st.vid_intent = ui.vid_intent
    st.vid_source_scope = ui.vid_source_scope
    st.vid_prompt = ui.vid_prompt
    st.vid_confidence = ui.vid_confidence
    st.vid_reason = ui.vid_reason
    st.vid_aspect_ratio = ui.vid_aspect_ratio
    st.vid_duration_secs = ui.vid_duration_secs
    return st


def _parse_json(raw: str):
    """Extract a JSON object from model output. Reuses doc_intent's robust,
    recovery-capable parser (string slicing — not intent detection)."""
    try:
        from models.doc_intent import _parse_json as _pjson
        return _pjson(raw)
    except Exception:  # noqa: BLE001
        import json
        s = (raw or "").strip()
        if s.startswith("```"):
            s = s.split("\n", 1)[-1] if "\n" in s else s
            if s.rstrip().endswith("```"):
                s = s.rstrip()[:-3]
        s = s.strip()
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(s[start:end + 1])
        return json.loads(s)
