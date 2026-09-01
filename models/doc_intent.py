# SPDX-License-Identifier: Apache-2.0
# ============================================================
# DOCUMENT-GENERATION INTENT CLASSIFIER  (LLM-ONLY — NO REGEX)
#
# Turns a raw user message (plus the conversation's document memory + any
# uploaded attachments) into a structured intent so doc-gen routes correctly.
#
# Intents:
#   generate  — author a brand-new document
#   summarize — condense an existing doc / uploaded file(s) / the chat
#   convert   — same content, different format ("convert that to PDF")
#   extract   — pull/merge info FROM one or more uploaded docs into a new one
#   compare   — diff TWO documents into a structured comparison report
#   revise    — edit a previously generated doc into a new version
#   none      — not a document request at all
#
# RULE: intent classification is done ENTIRELY by the fast in-house/quantized
# local model (config.DOC_INTENT_MODEL, e.g. gemma/kimi/glm-class). There is NO
# regex-based intent detection anywhere. Authoring is done later by the cloud
# model ("complex" → Claude Sonnet). The model is instructed to ALWAYS return a
# concrete format so `is_doc` alone is enough to route to document generation.
# ============================================================

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from core.logger import logger

try:
    from core.config import DOC_INTENT_MODEL as _INTENT_MODEL
except Exception:  # noqa: BLE001
    _INTENT_MODEL = "local"

# Single source of truth for doc-gen intents. "none" = not a document request.
ACTION_INTENTS = ("generate", "summarize", "convert", "extract", "compare", "revise")
_VALID_INTENTS = set(ACTION_INTENTS) | {"none"}


@dataclass
class DocIntent:
    intent: str = "generate"
    format: Optional[str] = None
    source_scope: str = "none"          # "uploaded" | "chat" | "artifact" | "none"
    target_artifact_id: Optional[str] = None
    preserve: bool = False              # uploaded file: reproduce verbatim vs. generate new
    is_doc: bool = True                 # convenience flag for callers
    # True when the user clearly wants a document but gave NO usable subject/topic
    # (and there is no attachment or prior artifact to work from). The caller
    # should ASK what the document is about rather than generate filler.
    needs_topic: bool = False
    topic: str = ""                     # the subject the model extracted, if any
    docs: list = field(default_factory=list)
    confidence: float = 0.5
    reason: str = ""
    raw: dict = field(default_factory=dict)
    # Set to the format/extension the user explicitly asked for (e.g. "py",
    # "sql", "html") when the MODEL judged that format is NOT one we can build
    # as a downloadable document (only pdf/docx/pptx/xlsx/csv/md/txt are). In
    # that case intent is forced to "none" (answer in chat) and the caller
    # should tell the user we can't produce that format as a downloadable file,
    # then put the content in a fenced code block. Empty otherwise. The decision
    # is made by the LLM — no hardcoded extension list.
    unsupported_format: str = ""


_SYS = (
    "You decide whether the user wants a DOWNLOADABLE FILE (a document artifact "
    "they can save/download) versus a normal conversational answer in chat.\n"
    "\n"
    "FIRST, READ THE ENTIRE MESSAGE AND UNDERSTAND THE USER'S ACTUAL GOAL — do "
    "NOT decide from isolated keywords. A word like 'compare', 'summarize', "
    "'report', 'document', 'write' or 'file' appearing SOMEWHERE in the message "
    "does NOT by itself mean the user wants a file. Ask yourself: does the user "
    "want the ANSWER delivered as a downloadable file, or do they just want me "
    "to reply in the conversation? When in doubt, it is a normal chat answer "
    "(intent='none').\n"
    "\n"
    "Respond with ONLY a JSON object, no prose. Put your reasoning FIRST so you "
    "think before you decide.\n"
    "Schema:\n"
    '{"reasoning":"<one sentence: what does the user actually want, and is the '
    'deliverable a downloadable file or a chat reply?>",'
    '"intent":"generate|summarize|convert|extract|compare|revise|none",'
    '"format":"pdf|docx|pptx|xlsx|csv|md|txt",'
    '"source_scope":"uploaded|chat|artifact|none",'
    '"target_artifact_id":"<id from the list or null>",'
    '"preserve":true|false,'
    '"topic":"<the subject of the document in a few words, or empty>",'
    '"needs_topic":true|false,'
    '"docs":[{"intent":"generate|summarize|convert|extract|compare|revise",'
    '"format":"pdf|docx|pptx|xlsx|csv|md|txt","topic":"<subject or empty>"}],'
    '"requested_format":"<the exact file type the user literally asked for if '
    'any, e.g. py, sql, sh, yaml, html, dockerfile — else empty>",'
    '"confidence":0.0-1.0}\n'
    "\n"
    "MULTIPLE DELIVERABLES — the `docs` array lists EACH downloadable file the "
    "user wants (1 to 3 entries; NEVER more than 3). Emit MORE THAN ONE entry "
    "ONLY when the user explicitly asks for several files in one message, e.g.:\n"
    "  • 'give me this as a PDF AND a Word doc' → two entries, SAME topic, "
    "differing only in format (pdf, docx).\n"
    "  • 'make a summary PDF and a separate data spreadsheet' → two entries with "
    "DIFFERENT topics/intents (summarize→pdf, extract/generate→xlsx).\n"
    "For a normal single-file request emit exactly ONE entry that mirrors the "
    "top-level intent/format/topic. When intent='none' (chat), emit an EMPTY "
    "docs array []. Keep the top-level intent/format/topic equal to docs[0].\n"
    "\n"
    "SUPPORTED DOWNLOADABLE FORMATS — we can ONLY build these as a downloadable "
    "file: pdf, docx, pptx, xlsx, csv, md, txt. If the user explicitly asks to "
    "create/generate a file of ANY OTHER type — source code or config or script "
    "files such as .py, .js, .ts, .sql, .sh, .yaml/.yml, .toml, .ini, .env, "
    "Dockerfile, Makefile, .html, .css, .xml, etc. — we CANNOT produce it as a "
    "downloadable document, so set intent='none' (empty docs array; we will "
    "answer in chat with the content in a code block) and put the exact type "
    "they asked for in 'requested_format' (e.g. 'py'). Do NOT coerce such a "
    "request into pdf/txt. Example: 'generate my config.py' / 'give me a "
    "deploy.sh' / 'make an alter.sql' → intent='none', requested_format set "
    "accordingly.\n"
    "\n"
    "DECISION GATE — default to intent='none' (normal chat) UNLESS the user "
    "clearly wants a FILE ARTIFACT as the deliverable. It IS a document request "
    "ONLY when at least one of these is true:\n"
    "  (a) they explicitly ask for the output AS a named file/format: 'make a "
    "pdf', 'as a word doc', 'export to excel', 'give me a pptx', 'download "
    "spreadsheet'; OR\n"
    "  (b) they use a produce-a-file verb on such a noun: create/make/generate/"
    "build/draft/export/download/prepare a <deck|report|pdf|doc|spreadsheet|file>; OR\n"
    "  (c) an attachment is present AND they explicitly ask for the RESULT as a "
    "NEW downloadable file/format (e.g. 'convert this to pdf', 'turn the "
    "attachment into a word doc', 'export this as an xlsx', 'make a slide deck "
    "from this file'); OR\n"
    "  (d) they act on a previously generated document (revise/convert/compare it).\n"
    "\n"
    "CRITICAL — ATTACHMENTS DEFAULT TO A CHAT ANSWER. When a file is attached and "
    "the user says 'summarize this', 'give me a summary', 'summarise the "
    "document', 'what does this say', 'explain this file', 'analyze/review this', "
    "'extract the key points', 'compare these files', or asks ANY question ABOUT "
    "the attached file, they want the answer IN THE CHAT REPLY — intent='none'. An "
    "attachment by itself is NOT a reason to produce a file. Only set intent to a "
    "document intent for an attachment when the user EXPLICITLY names a target "
    "file/format to CREATE (see (c) above). 'Summarize this attachment' → 'none'. "
    "'Summarize this attachment INTO A PDF' → summarize→pdf.\n"
    "\n"
    "It is NOT a document request (intent='none') when the user wants an ANSWER "
    "in the conversation — EVEN IF the message is long, contains pasted content "
    "(schemas, code, logs, tables, data dumps), or uses words like 'summarize', "
    "'compare', 'write', 'list', 'report', 'document'. Pasting content and asking "
    "me to 'provide / give / write / show X' means: put X in your CHAT REPLY, "
    "not in a file. Examples that are 'none':\n"
    "  • 'Tell me a short story about a lighthouse keeper.'   (prose reply)\n"
    "  • 'Summarize the plot of Hamlet.'                      (chat answer)\n"
    "  • 'Explain how UPI works.' / 'What is X?' / 'Write a poem.'\n"
    "  • 'Compare Python and Java.'                           (opinion answer)\n"
    "  • '<pasted schema A> <pasted schema B> compare these and give me the "
    "ALTER script for what's missing.'  (wants SQL in the reply, NOT a file)\n"
    "  • '<pasted code> review this and suggest fixes.'       (chat answer)\n"
    "  • '[attachment: report.pdf] summarize this.'           (chat answer)\n"
    "  • '[attachment: spec.docx] give me a summary / key points.' (chat answer)\n"
    "  • '[attachment: data.xlsx] what does this file contain?' (chat answer)\n"
    "Only choose summarize/compare/extract when there is a REAL source FILE "
    "(attachment) or a prior generated DOC to act on AND the user explicitly asks "
    "for the OUTPUT as a downloadable file/format — never for content pasted "
    "inline, general-knowledge questions, or a bare 'summarize this attachment' "
    "(that is a chat answer, intent='none').\n"
    "\n"
    "ANSWERING THE ASSISTANT'S QUESTIONS IS CHAT. If the RECENT CONVERSATION "
    "shows the assistant just asked the user probing / brainstorming / "
    "clarifying questions and THIS message is the user ANSWERING them, that is "
    "intent='none' — EVEN IF the answers describe deliverables using words like "
    "'report', 'checklist', 'roadmap', 'framework', 'downloadable' or "
    "'document'. Describing a DESIRED OUTCOME while answering is NOT commanding "
    "me to produce a file. Treat it as a document request ONLY if THIS message "
    "adds a fresh produce-a-file command per the DECISION GATE (a)/(b) above "
    "(e.g. the user now says 'ok, put that in a PDF').\n"
    "\n"
    "Intent meanings (only when the gate above says it IS a document request):\n"
    "- generate: author a NEW file the user asked to create ('make a ppt on X').\n"
    "- summarize: condense an UPLOADED FILE, the chat, or a PRIOR DOC into a file.\n"
    "- convert: SAME content into a different file format.\n"
    "- extract: pull/MERGE info FROM uploaded file(s) into a new file.\n"
    "- compare: DIFF two real documents (uploads and/or prior docs) into a report.\n"
    "- revise: change a document you already made ('make the intro shorter').\n"
    "- none: NOT a file request — answer in chat.\n"
    "\n"
    "When (and only when) intent is NOT 'none', ALWAYS choose a concrete format: "
    "deck/slides/presentation/'ppt' -> pptx; report/letter/'doc'/'word' -> docx; "
    "sheet/table/data/numbers -> xlsx; plain data dump -> csv; notes -> md; "
    "otherwise -> pdf.\n"
    "- topic: the SUBJECT of the requested document in a few words (e.g. 'Q3 "
    "payments report', 'UPI architecture'). Empty string if the user named no "
    "subject at all.\n"
    "- needs_topic: set TRUE when the user clearly wants a document (generate) "
    "but gave NO usable subject — e.g. 'make a pdf', 'create a document', "
    "'generate a report' with nothing about WHAT — AND there is no attachment, "
    "prior document, or live conversation to work from. In that case we must ASK "
    "what it should be about instead of generating filler. Set FALSE whenever a "
    "subject is present, an attachment/prior doc exists, source_scope=chat (the "
    "conversation IS the subject), or intent is summarize/convert/extract/"
    "compare/revise on real source material.\n"
    "- preserve=true ONLY when reproducing an uploaded file faithfully in another "
    "format (convert/copy). preserve=false when generating NEW content.\n"
    "- If the user refers to a previously generated document, set target_artifact_id "
    "to its id from the PRIOR DOCUMENTS list and source_scope=artifact.\n"
    "- If files are attached and the ask is about them, source_scope=uploaded.\n"
    "- If a live conversation is present and the document should be built from "
    "THIS conversation, set source_scope=chat. That covers the obvious cases "
    "('summarize this chat into a doc', 'make a report of what we discussed') "
    "AND the case where the user names no subject of their own and instead "
    "points back at the conversation with a deictic word — 'this', 'that', 'it', "
    "'the above' (also in other languages, e.g. Hinglish/Malayalam 'itha pathi', "
    "'ithine pathi', 'iske baare mein'). This applies to intent='generate' too: "
    "'generate a pdf on this' after a conversation about Java means "
    "source_scope=chat and topic='Java'. Use source_scope=none only when the "
    "user supplies their OWN subject (e.g. 'make a pdf about the history of "
    "Rome'), even if a conversation is present.\n"
)

# Format normalisation — a plain lookup table (NOT intent detection). Maps the
# model's format word (or a caller-supplied hint) to the canonical extension.
_FMT_ALIASES = {
    "word": "docx", "doc": "docx", "powerpoint": "pptx", "presentation": "pptx",
    "slides": "pptx", "slide": "pptx", "deck": "pptx", "ppt": "pptx",
    "excel": "xlsx", "xls": "xlsx", "spreadsheet": "xlsx", "sheet": "xlsx",
    "markdown": "md", "text": "txt",
}
_CANON_FORMATS = {"pdf", "docx", "pptx", "xlsx", "csv", "md", "txt"}


def _norm_format(fmt) -> Optional[str]:
    if not fmt:
        return None
    f = str(fmt).lower().strip()
    f = _FMT_ALIASES.get(f, f)
    return f if f in _CANON_FORMATS else None


# Hard cap on documents produced from a single prompt (guards against a runaway
# prompt spinning up many expensive jobs). Enforced in _normalize_docs().
_MAX_DOCS = 3


def _normalize_docs(raw_docs, *, fallback_intent: str, fallback_format: str,
                    fallback_topic: str) -> list:
    """Normalise the model's `docs` array into a clean, capped list of
    {intent, format, topic} dicts.

    Each entry's intent is validated against ACTION_INTENTS (invalid → the
    fallback), format is run through _norm_format() (missing → pdf), and the
    list is de-duplicated on (intent, format, topic) and capped to _MAX_DOCS.
    When the model omits/empties the array we synthesise a single entry from the
    scalar fallbacks — so callers ALWAYS get at least one entry for a doc
    request (back-compat with the pre-multi-doc single-format behaviour)."""
    out: list = []
    seen: set = set()
    for d in (raw_docs or []):
        if not isinstance(d, dict):
            continue
        _intent = str(d.get("intent", "") or "").lower().strip()
        if _intent not in ACTION_INTENTS:
            _intent = fallback_intent
        _fmt = _norm_format(d.get("format")) or "pdf"
        _topic = str(d.get("topic") or "").strip()
        key = (_intent, _fmt, _topic)
        if key in seen:
            continue
        seen.add(key)
        out.append({"intent": _intent, "format": _fmt, "topic": _topic})
        if len(out) >= _MAX_DOCS:
            break
    if not out:
        # No usable array from the model → synthesise from the scalar decision.
        out = [{
            "intent": fallback_intent,
            "format": _norm_format(fallback_format) or "pdf",
            "topic":  (fallback_topic or "").strip(),
        }]
    return out


def _resolve_needs_topic(needs_topic: bool, *, has_attachments: bool, intent: str,
                         source_scope: str, target_artifact_id, topic: str,
                         has_chat_context: bool = False) -> bool:
    """Should we ASK the user for a topic instead of generating? (pure/testable)

    Only True for a topicless NEW-content generate with NO source to work from.
    Any material — an attachment, a prior artifact, a live conversation to
    summarise, a non-generate intent, or a topic the model extracted — clears
    the flag so a legitimate request is never blocked. This is the deterministic
    guard over the model's own needs_topic."""
    if not needs_topic:
        return False
    if (has_attachments
            or has_chat_context
            or (intent or "generate") != "generate"
            or (source_scope or "none") in ("uploaded", "artifact", "chat")
            or target_artifact_id
            or (topic or "").strip()):
        return False
    return True


# Explicit "the user wants a FILE ARTIFACT" tokens. This is a deterministic VETO
# of model false-positives (e.g. "tell me a story" mis-classified as generate),
# NOT a router: it can only turn is_doc=true into chat when NO file signal is
# present. It NEVER invents a doc intent. Word-boundary token match, lowercased.
#
# STRONG vs WEAK. A STRONG signal is an explicit file format/extension — the user
# clearly named the deliverable AS a file ("as a pdf", "a word doc", "export to
# excel"). A WEAK signal is a bare descriptive noun ("report", "document",
# "file") that people also use when merely DESCRIBING a desired outcome inside a
# sentence ("the outcome should be like a roadmap report") — NOT a command to
# produce a file. A weak noun only counts as a real file signal when paired with
# a produce-a-file verb ("make a report", "generate a one-pager"). This split
# stops the veto from rubber-stamping a descriptive noun as a doc request.
_STRONG_ARTIFACT_TOKENS = frozenset({
    # format words / extensions — an explicit named file format
    "pdf", "docx", "doc", "word", "pptx", "ppt", "powerpoint", "presentation",
    "slide", "slides", "deck", "xlsx", "xls", "excel", "spreadsheet", "csv",
    "md", "markdown", "readme", "txt",
    # "document" promoted from weak: phrases like "give this to me in document",
    # "put this in a document", "as a document" unambiguously request a file
    # artifact. Keeping it weak caused the veto to fire when has_chat_context=True
    # suppressed the weak-signal fallback, routing to chat instead of doc-gen.
    "document",
})
_WEAK_ARTIFACT_TOKENS = frozenset({
    # bare artifact nouns — only a file signal alongside a produce verb
    "report", "file", "sheet", "workbook", "handout", "brochure",
    "whitepaper", "datasheet", "one-pager", "onepager",
})
# Back-compat union: "is there ANY artifact token at all?" (strong OR weak).
_ARTIFACT_TOKENS = _STRONG_ARTIFACT_TOKENS | _WEAK_ARTIFACT_TOKENS
# Verbs that turn a bare artifact noun into a real "produce a file" command.
_PRODUCE_VERBS = frozenset({
    "create", "make", "generate", "build", "draft", "export", "download",
    "prepare", "produce", "write", "compose",
})
_STRONG_EXTENSIONS = (".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".doc", ".ppt",
                      ".xls", ".md", ".txt")


def _tokenize(text: str) -> set:
    tl = (text or "").lower()
    return set(
        "".join(ch if (ch.isalnum() or ch == "-") else " " for ch in tl).split()
    )


def _artifact_signal(text: str) -> tuple[bool, bool]:
    """Classify the prompt's file-artifact signal in ONE pass. Returns
    (any_signal, strong_signal):

    - strong  = an explicit format/extension is present, OR a produce-a-file verb
                is paired with an artifact noun ("make a report", "give me a pdf").
    - any     = strong, OR a bare descriptive noun ("a roadmap report") appears
                on its own — the kind of phrase used when merely DESCRIBING a
                desired outcome, which is NOT by itself a command to produce a file.

    Deterministic token check (no routing regex). Used only to gate the veto —
    never invents intent. strong implies any."""
    tl = (text or "").lower()
    ext = any(_ext in tl for _ext in _STRONG_EXTENSIONS)
    tokens = _tokenize(text)
    strong = (ext
              or bool(tokens & _STRONG_ARTIFACT_TOKENS)
              or bool((tokens & _WEAK_ARTIFACT_TOKENS) and (tokens & _PRODUCE_VERBS)))
    any_signal = strong or ext or bool(tokens & _ARTIFACT_TOKENS)
    return any_signal, strong


def _has_artifact_signal(text: str) -> bool:
    """True if the prompt references a downloadable file/artifact in ANY way
    (strong format word OR weak descriptive noun)."""
    return _artifact_signal(text)[0]


def _has_strong_artifact_signal(text: str) -> bool:
    """True when the prompt STRONGLY indicates the user wants a file artifact
    (explicit format/extension, or produce-verb + artifact noun). A bare
    descriptive noun alone ("a roadmap report for the entity") is NOT strong."""
    return _artifact_signal(text)[1]


def _format_recent_turns(recent_turns, *, max_turns: int = 3,
                         per_turn_chars: int = 300, total_chars: int = 1200) -> str:
    """Render the last few conversation turns into a compact transcript block so
    the classifier can see the CONVERSATIONAL ACT (e.g. the assistant just asked
    questions and the user is answering) rather than deciding from isolated
    words. `recent_turns` is a list of {'role','content'} dicts, newest last —
    exactly the shape of RedisMemory.get_conversation(). Length-capped because
    the model router does not truncate the prompt. Returns '' when empty."""
    if not recent_turns:
        return ""
    header = "RECENT CONVERSATION (newest last):\n"
    budget = total_chars - len(header)
    picked = []
    used = 0
    for entry in reversed(recent_turns):          # collect newest first
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        content = str(entry.get("content") or "").strip()
        if not content:
            continue
        line = f"{role}: {content[:per_turn_chars].replace(chr(10), ' ').strip()}"
        # Drop whole turns once the budget is spent rather than slicing mid-line.
        if picked and used + 1 + len(line) > budget:
            break
        used += len(line) + (1 if picked else 0)
        picked.append(line)
        if len(picked) >= max_turns:
            break
    if not picked:
        return ""
    picked.reverse()                              # restore chronological order
    return header + "\n".join(picked)


def classify(text: str, *, has_attachments: bool = False,
             doc_memory_summary: str = "", chat_id: str = "",
             user_id: str = "", has_chat_context: bool = False,
             recent_turns: list | None = None) -> DocIntent:
    """Classify a document request USING THE SMALL LOCAL MODEL ONLY.

    `doc_memory_summary` is the compact listing from
    services.doc_context.DocMemory.summary_for_llm() so the model can point at an
    existing artifact for revise/convert/summarize.

    `has_chat_context` signals that the current chat has plain-chat history the
    user could be asking to summarise/convert/extract — it nudges the model
    toward source_scope="chat" and clears needs_topic (a live conversation IS
    the topic).

    `recent_turns` is the recent conversation ({'role','content'} dicts, newest
    last — RedisMemory.get_conversation() shape). When present it is rendered
    into a short transcript so the model can tell that the user is, e.g.,
    ANSWERING the assistant's questions (chat) rather than commanding a file —
    instead of guessing from trigger words alone. Optional; callers without
    conversation history simply omit it. Never raises."""
    text = (text or "").strip()
    if not text:
        return DocIntent(intent="none", is_doc=False, confidence=0.9, reason="empty")

    try:
        from models.model_router import model_router
        ctx = ""
        if doc_memory_summary:
            ctx += f"\nPRIOR DOCUMENTS in this conversation:\n{doc_memory_summary}\n"
        ctx += f"\nAttachments present: {'yes' if has_attachments else 'no'}\n"
        ctx += f"Live conversation present: {'yes' if has_chat_context else 'no'}\n"
        _turns_block = _format_recent_turns(recent_turns)
        if _turns_block:
            ctx += f"\n{_turns_block}\n"
        prompt = f"{_SYS}{ctx}\nUSER REQUEST:\n{text}\n\nJSON:"
        # model_router.generate() ALREADY cascades small→cloud internally:
        # model_hint="local" → TIER_SIMPLE → local model, then GPT-5-mini, then
        # Claude Sonnet (see models/model_router._try_local_simple). So the
        # classifier survives a local-model outage automatically. Only a TOTAL
        # outage (every provider down) yields the "Error: no gateway available"
        # sentinel below.
        raw = (model_router.generate(prompt, model_hint=_INTENT_MODEL,
                                     return_meta=False) or "").strip()
        if not raw or raw.startswith("Error:"):
            raise RuntimeError(f"all models unavailable ({raw[:80]!r})")
        data = _parse_json(raw)
        if not isinstance(data, dict):
            raise ValueError("intent JSON not an object")
        intent = str(data.get("intent", "")).lower().strip()
        if intent not in _VALID_INTENTS:
            raise ValueError(f"invalid intent {intent!r}")

        fmt = _norm_format(data.get("format"))
        # The model is told to always pick a format; if it still omitted one for
        # a real doc request, default sensibly (pdf) — a lookup default, no regex.
        if intent != "none" and not fmt:
            fmt = "pdf"

        # The model reasons about the whole message FIRST (schema puts
        # "reasoning" before "intent"); we surface it in reason/logs so a
        # misroute can be traced to WHY the model decided the way it did.
        _model_reason = str(data.get("reasoning", "") or "").strip()
        _topic = str(data.get("topic") or "").strip()
        _needs_topic = bool(data.get("needs_topic", False))
        _docs = ([] if intent == "none"
                 else _normalize_docs(data.get("docs"),
                                      fallback_intent=intent,
                                      fallback_format=fmt,
                                      fallback_topic=_topic))
        # The exact file type the user asked for when it is NOT a supported
        # downloadable format (the model sets this and picks intent='none').
        # Normalised to a bare extension word; only meaningful when is_doc=False.
        _requested_fmt = str(data.get("requested_format") or "").lower().strip().lstrip(".")
        _unsupported_fmt = (
            _requested_fmt
            if (intent == "none" and _requested_fmt
                and _requested_fmt not in _CANON_FORMATS)
            else ""
        )
        di = DocIntent(
            intent=intent,
            format=fmt,
            source_scope=str(data.get("source_scope", "none")).lower().strip() or "none",
            target_artifact_id=(data.get("target_artifact_id") or None),
            preserve=bool(data.get("preserve", False)),
            is_doc=(intent != "none"),
            needs_topic=_needs_topic,
            topic=_topic,
            docs=_docs,
            confidence=float(data.get("confidence", 0.7) or 0.7),
            reason=(f"model: {_model_reason}" if _model_reason else "model"),
            unsupported_format=_unsupported_fmt,
            raw=data,
        )
        logger.info(
            f"[doc_intent] intent={di.intent!r} fmt={di.format} "
            f"docs={len(di.docs)}({','.join(d['format'] for d in di.docs)}) "
            f"conf={di.confidence:.2f} scope={di.source_scope} "
            f"unsupported_fmt={di.unsupported_format or '-'} "
            f"reasoning={_model_reason[:160]!r} | text={text[:80]!r}"
        )
        # Guard: target artifact must actually exist in the listing.
        if di.target_artifact_id and di.target_artifact_id not in (doc_memory_summary or ""):
            di.target_artifact_id = None
        # needs_topic only applies to a NEW-content generate with no source to
        # work from — see _resolve_needs_topic (pure, testable) for the rule.
        di.needs_topic = _resolve_needs_topic(
            di.needs_topic, has_attachments=has_attachments, intent=di.intent,
            source_scope=di.source_scope, target_artifact_id=di.target_artifact_id,
            topic=_topic, has_chat_context=has_chat_context,
        )

        # ── DETERMINISTIC VETO (err toward CHAT) ──────────────────────────────
        # Even if the model said this is a document request, REQUIRE a concrete
        # reason to route to doc-gen. Route to doc-gen ONLY when at least one is
        # true: (a) the prompt names a file/artifact, (b) a file is attached, or
        # (c) it acts on a previously generated doc. Otherwise force chat. This
        # kills false positives like "tell me a story" / "summarize the plot of
        # Hamlet" that a small model can mis-classify — without any routing regex.
        if di.is_doc:
            _acts_on_prior = bool(di.target_artifact_id) or di.source_scope == "artifact"
            _any_sig, _strong = _artifact_signal(text)
            _text_has_artifact = _strong or (_any_sig and not has_chat_context)
            _has_signal = has_attachments or _acts_on_prior or _text_has_artifact
            if not _has_signal:
                logger.info(
                    f"[doc_intent] model said intent={di.intent!r} but NO artifact "
                    f"signal (no format/noun/file/prior) → vetoing to chat | "
                    f"text={text[:80]!r}"
                )
                return DocIntent(intent="none", format=None, source_scope="none",
                                 is_doc=False, confidence=0.6, reason="veto_no_artifact_signal")

            # ── ATTACHMENT VETO (err toward CHAT) ─────────────────────────────
            # An attachment being present is NOT, on its own, a reason to emit a
            # downloadable file. The overwhelmingly common ask is "summarize /
            # explain / analyze THIS attached file" → the user wants the answer
            # IN CHAT. Only honour a doc intent for an attachment when the user
            # EXPLICITLY names a target file/format to CREATE, i.e. the prompt
            # itself carries an artifact/format signal (or it acts on a prior
            # generated doc). Without that explicit signal, force chat. This is
            # the backstop for the small model still routing a bare "summarize
            # this attachment" to doc-gen despite the prompt guidance.
            if (has_attachments and not _acts_on_prior and not _text_has_artifact):
                logger.info(
                    f"[doc_intent] model said intent={di.intent!r} on an ATTACHMENT "
                    f"but the prompt names NO target file/format → vetoing to chat "
                    f"(answer about the file in-conversation) | text={text[:80]!r}"
                )
                return DocIntent(intent="none", format=None, source_scope="none",
                                 is_doc=False, confidence=0.6, reason="veto_attachment_no_artifact_signal")
        return di
    except Exception as exc:  # noqa: BLE001
        # We reach here only if the router's ENTIRE cascade (local → GPT-5-mini →
        # Claude) was unavailable, or the reply couldn't be parsed. We do NOT use
        # regex intent detection (platform rule). Best-effort BEST-EFFORT generate:
        # assume a document request with a safe default format and let the doc
        # worker's own model cascade + self-heal retries attempt authoring, rather
        # than silently answering a doc request as chat during an outage.
        # Err toward CHAT: only best-effort generate when the prompt itself shows
        # an explicit artifact/format signal. A bare "tell me a story" — or a bare
        # "summarize this attachment" (attachment present but NO named target
        # file/format) — during an outage must NOT spin up a doc job; those are
        # chat answers. An attachment alone is deliberately NOT sufficient here,
        # mirroring the ATTACHMENT VETO on the success path above. Require a
        # STRONG signal (explicit format, or produce-verb + noun) — a bare
        # descriptive "report"/"document" during an outage must NOT spin up a job.
        if _has_strong_artifact_signal(text):
            logger.warning(f"[doc_intent] classify unavailable ({exc}); artifact signal present → best-effort generate/pdf")
            return DocIntent(intent="generate", format="pdf", source_scope="none",
                             is_doc=True, confidence=0.25, reason="model_error",
                             docs=[{"intent": "generate", "format": "pdf", "topic": ""}])
        logger.warning(f"[doc_intent] classify unavailable ({exc}); no artifact signal → chat")
        return DocIntent(intent="none", format=None, source_scope="none",
                         is_doc=False, confidence=0.25, reason="model_error")


def _parse_json(raw: str):
    """Extract a JSON object from the model output. Reuses the robust,
    recovery-capable parser when available; otherwise a minimal brace scan.
    (String slicing — not intent classification.)"""
    try:
        from agents.doc_generator_agent import _parse_llm_json as _pjson
        return _pjson(raw)
    except Exception:  # noqa: BLE001
        s = (raw or "").strip()
        if s.startswith("```"):
            s = s.split("\n", 1)[-1] if "\n" in s else s
            if s.rstrip().endswith("```"):
                s = s.rstrip()[:-3]
        s = s.strip()
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(s[start:end + 1])
        return json.loads(s)
