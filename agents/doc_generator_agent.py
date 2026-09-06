# SPDX-License-Identifier: MIT
# ============================================================
# MARKDOWN DOCUMENT AGENT
#
# Core agent logic for generating and editing Markdown (.md)
# documents from user prompts.  Designed to be called from
# workers/md_doc_worker.py (RQ job) or directly via the
# Python API.
#
# Design mirrors doc_worker.py / doc_generator.py standards:
#   - Same JSON section schema (heading, subheading, level,
#     content, bullets, callout, table)
#   - Same McKinsey/BCG-quality LLM prompts
#   - Same domain-aware palette system
#   - Same section numbering (1., 2., 1.1, 1.2)
#   - Callout boxes → GFM blockquotes
#   - Data tables → GFM pipe tables
#
# Public API:
#   generate_md_doc(prompt, output_path, chat_id, model_hint)
#     → {file_id, output_path, filename, title, domain,
#        sections, word_count, meta}
#
#   edit_md_doc(edit_request, chat_id, model_hint)
#     → {file_id, output_path, edit_summary, sections_before,
#        sections_after, edit_type, sections_affected, meta}
#
# ── SYNC NOTE ────────────────────────────────────────────────
# The following helpers are DUPLICATED from workers/doc_worker.py
# to avoid circular imports (doc_worker imports Redis/Postgres
# at module level which crashes without those services):
#   _derive_title_from_question  (last synced: 2026-05-27)
#   _sanitize_llm_title          (last synced: 2026-05-27)
#   _extract_section_count       (last synced: 2026-05-27)
#   _parse_llm_json              (last synced: 2026-05-27)
# When updating these in doc_worker.py, update here too.
# ============================================================

import json
import os
import re
import uuid as _uuid_mod
from datetime import date as _date

from core.config import DOC_STORAGE_DIR, user_doc_dir
from core.logger import logger

# ── Direct imports (safe — no Redis/DB at module level) ──────
from tools.doc_generator import (
    generate_md as _generate_md_bytes,
    slugify,
    smart_filename,
    get_palette,
)

# Persistent storage (see core.config.DOC_STORAGE_DIR). NOT /tmp.
DOC_DIR = DOC_STORAGE_DIR
os.makedirs(DOC_DIR, exist_ok=True)

# ── Session TTL (24 h — matches doc:result:* TTL) ────────────
SESSION_TTL = 86400


# ══════════════════════════════════════════════════════════════
# DUPLICATED HELPERS FROM doc_worker.py
# (see SYNC NOTE above)
# ══════════════════════════════════════════════════════════════

def _derive_title_from_question(question: str) -> str:
    """
    Derive a clean, professional document title from the raw user question.
    Strips common request verbs and format keywords.

    Examples:
      "generate a markdown report on UPI payments in India"
        → "UPI Payments in India"
      "write a document about AI trends 2025"
        → "AI Trends 2025"
    """
    text = (question or "").strip()

    _STRIP_PREFIX = re.compile(
        r"^(please\s+)?"
        r"(generate|create|make|write|export|produce|draft|build|prepare|give|get|"
        r"want|need|show|provide|send|share|download|fetch|output|"
        r"summari[sz]e|summari[sz]ation|tl;?dr|rewrite|reword|shorten|expand|"
        r"convert|transform|turn|extract|pull|merge|combine|consolidate|update|revise|edit)\s+"
        r"(me\s+)?(this\s+|that\s+|these\s+|those\s+|it\s+)?(a\s+|an\s+|the\s+)?"
        r"(pdf|docx?|word|excel|xlsx?|pptx?|powerpoint|presentation|slides?|"
        r"spreadsheet|markdown|text|txt|report|document|doc|file|summary|analysis|md)?\s*"
        r"(report|document|doc|file|on|about|for|regarding|covering|of|into|to|from)?\s*",
        re.IGNORECASE,
    )
    cleaned = _STRIP_PREFIX.sub("", text).strip()

    if cleaned:
        title = " ".join(w.capitalize() for w in cleaned.split())
        return title[:80]

    return " ".join(w.capitalize() for w in text.split())[:80] or "Document"


def _sanitize_llm_title(llm_title: str, question: str) -> str:
    """
    Guard against LLM returning the raw question or a request-verb phrase as the title.
    Falls back to _derive_title_from_question if the title looks like a raw prompt.
    """
    t = (llm_title or "").strip()
    if not t:
        return _derive_title_from_question(question)

    if len(t) > 100:
        return _derive_title_from_question(question)

    _BAD_START = re.compile(
        r"^(please\s+)?(generate|create|make|write|export|produce|draft|build|prepare|"
        r"give|get|want|need|show|provide|send|share|download|fetch|output|"
        r"summari[sz]e|summari[sz]ation|tl;?dr|rewrite|reword|shorten|expand|"
        r"convert|transform|turn|extract|pull|merge|combine|consolidate|update|revise|edit)\b",
        re.IGNORECASE,
    )
    if _BAD_START.match(t):
        return _derive_title_from_question(question)

    return t


def _extract_section_count(question: str) -> int | None:
    """
    Parse the user's question for an explicit section / page count.
    Returns None when no count is found.
    """
    patterns = [
        r"\b(\d+)\s*[-–]?\s*(?:slides?|pages?|sections?)\b",
        r"\b(?:slides?|pages?|sections?)\s*[-–]?\s*(\d+)\b",
    ]
    for pat in patterns:
        m = re.search(pat, question, re.IGNORECASE)
        if m:
            n = int(m.group(1))
            return max(1, min(n, 50))
    return None


def _parse_llm_json(raw: str, context: str = "") -> dict:
    """
    Strip markdown fences and parse JSON robustly.
    Attempts partial recovery when the LLM response is truncated mid-JSON.
    """
    original = raw
    raw = raw.strip()
    raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
    raw = re.sub(r"\s*```\s*$", "", raw.strip())
    raw = raw.strip()
    if not raw.startswith("{"):
        m = re.search(r"\{", raw)
        if m:
            raw = raw[m.start():]
    if raw and not raw.endswith("}"):
        last_brace = raw.rfind("}")
        if last_brace != -1:
            raw = raw[:last_brace + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(f"[md_agent] JSON parse failed — attempting partial recovery | "
                       f"context={context!r} error={exc} raw_preview={original[:300]!r}")
        recovered_sections = []
        for m in re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}', raw):
            try:
                obj = json.loads(m.group())
                if "heading" in obj:
                    recovered_sections.append(obj)
            except (json.JSONDecodeError, ValueError):
                continue

        if recovered_sections:
            logger.info(f"[md_agent] partial recovery: {len(recovered_sections)} sections")
            title_m  = re.search(r'"title"\s*:\s*"([^"]*)"', raw)
            domain_m = re.search(r'"domain"\s*:\s*"([^"]*)"', raw)
            return {
                "title":    title_m.group(1)  if title_m  else "",
                "domain":   domain_m.group(1) if domain_m else "default",
                "sections": recovered_sections,
            }

        logger.error(f"[md_agent] JSON parse failed, no recovery | "
                     f"context={context!r} error={exc}")
        raise


def extract_sections_partial(raw: str) -> tuple[str, str, list[dict]]:
    """Best-effort recovery of (title, domain, sections) from a possibly
    incomplete JSON stream. Safe to call mid-stream during LLM token streaming
    to drive the live document preview UI.

    Returns ('', '', []) when nothing parseable has arrived yet.
    """
    if not raw:
        return "", "", []
    s = raw.strip()
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s.strip())
    if not s.startswith("{"):
        m = re.search(r"\{", s)
        if m:
            s = s[m.start():]

    title_m  = re.search(r'"title"\s*:\s*"([^"]*)"', s)
    domain_m = re.search(r'"domain"\s*:\s*"([^"]*)"', s)
    title    = title_m.group(1) if title_m else ""
    domain   = domain_m.group(1) if domain_m else ""

    # Recover only sections whose braces are fully balanced — same approach
    # as the existing _parse_llm_json fallback (handles one level of nesting
    # for nested objects like callout/table).
    sections: list[dict] = []
    for m in re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', s):
        try:
            obj = json.loads(m.group())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and "heading" in obj:
            sections.append(obj)

    return title, domain, sections


# ══════════════════════════════════════════════════════════════
# LLM CALL WRAPPER
# ══════════════════════════════════════════════════════════════

def _llm_call(prompt: str, context: str = "", model_hint: str = "complex") -> tuple:
    """
    Call model_router.generate and return (raw_text, llm_meta).
    Raises on failure.
    """
    from models.model_router import model_router
    logger.info(
        f"[md_agent] calling model_router.generate | context={context!r} "
        f"prompt_len={len(prompt)} model_hint={model_hint!r}"
    )
    result   = model_router.generate(prompt, model_hint=model_hint, return_meta=True)
    raw      = (result.get("text") or "").strip()
    llm_meta = result.get("meta") or {}
    logger.info(f"[md_agent] LLM response | context={context!r} raw_len={len(raw)} "
                f"model={llm_meta.get('model')}")
    return raw, llm_meta


def _llm_call_stream(
        prompt: str,
        context: str = "",
        on_section=None,
        on_title=None,
        rate_limit_sec: float = 0.4,
        model_hint: str = "complex",
) -> tuple:
    """Stream tokens from model_router.stream() and fire on_section(section)
    each time a new fully-formed section appears in the accumulated JSON.

    on_section: callable(section_dict) — invoked once per newly-completed
                section, in document order.
    on_title:   callable(title, domain) — invoked when title/domain are
                first recovered (and again if they change).
    model_hint: model_router tier ("complex" | "fast" | "openai-deep" | ...).
                Defaults to "complex" (Claude Sonnet) to preserve the old
                behaviour when callers don't pass anything explicit.

    Returns (final_raw_text, llm_meta) after the __stream_meta__ sentinel.
    """
    import time
    from models.model_router import model_router

    logger.info(
        f"[md_agent] calling model_router.stream | context={context!r} "
        f"prompt_len={len(prompt)} model_hint={model_hint!r}"
    )

    parts: list[str] = []
    llm_meta: dict = {}
    last_section_count = 0
    last_title = ""
    last_domain = ""
    last_emit = 0.0

    def _maybe_emit(force: bool = False) -> None:
        nonlocal last_section_count, last_title, last_domain, last_emit
        now = time.monotonic()
        if not force and (now - last_emit) < rate_limit_sec:
            return
        last_emit = now
        raw_so_far = "".join(parts)
        title, domain, sections = extract_sections_partial(raw_so_far)
        if on_title and (title != last_title or domain != last_domain):
            try:
                on_title(title, domain)
            except Exception as exc:
                logger.warning(f"[md_agent] on_title callback failed: {exc}")
            last_title, last_domain = title, domain
        if on_section and len(sections) > last_section_count:
            for sec in sections[last_section_count:]:
                try:
                    on_section(sec)
                except Exception as exc:
                    logger.warning(f"[md_agent] on_section callback failed: {exc}")
            last_section_count = len(sections)

    for chunk in model_router.stream(prompt, model_hint=model_hint):
        if isinstance(chunk, dict) and "__stream_meta__" in chunk:
            llm_meta = chunk["__stream_meta__"] or {}
            break
        if isinstance(chunk, str) and chunk:
            parts.append(chunk)
            _maybe_emit(force=False)

    # Final emit in case the last section completed in the trailing burst.
    _maybe_emit(force=True)

    raw = "".join(parts).strip()
    logger.info(f"[md_agent] LLM stream done | context={context!r} raw_len={len(raw)} "
                f"sections_emitted={last_section_count} model={llm_meta.get('model_label')}")
    # Normalize the stream meta shape to match _llm_call's return for callers.
    if "model_label" in llm_meta and "model" not in llm_meta:
        llm_meta["model"] = llm_meta.get("model_label")
    return raw, llm_meta


# ══════════════════════════════════════════════════════════════
# PROMPT BUILDERS
# ══════════════════════════════════════════════════════════════

def _build_md_prompt(question: str, chat_context: str = "") -> str:
    """
    Build the LLM prompt for Markdown document generation.

    Mirrors _build_docx_prompt() in doc_worker.py — same JSON schema,
    same quality rules, same section count injection.

    The JSON schema is identical to DOCX/PDF so the same _sections_to_md()
    renderer can produce rich GFM output (callouts, tables, subheadings).

    `chat_context` (optional): the current conversation's history — passed ONLY
    when the caller determined the document's source is the chat itself (e.g.
    "summarize this chat into a document"). It is STRICTLY this chat's history,
    never the knowledge base or codebase. When present, the LLM is instructed to
    build the document FROM the conversation rather than authoring a fresh essay.
    """
    requested_sections = _extract_section_count(question)
    if requested_sections:
        section_rule = (
            f"CRITICAL: The user explicitly requested exactly {requested_sections} section(s). "
            f"You MUST produce exactly {requested_sections} section(s) in the 'sections' array — "
            f"no more, no less.\n\n"
        )
        section_count_inline = (
            f"Produce EXACTLY {requested_sections} section(s) — this is a hard requirement."
        )
    else:
        section_rule = ""
        section_count_inline = "Minimum 5-7 sections — no thin or placeholder sections."

    ctx = (chat_context or "").strip()
    if ctx:
        source_block = (
            "The document's source is the CONVERSATION below between a user and "
            "an AI assistant. Build the document FROM this conversation — capture "
            "its key information, decisions, insights and content faithfully. Do "
            "NOT invent topics that are not present in the conversation.\n\n"
            "CONVERSATION:\n"
            "-----\n"
            f"{ctx[:8000]}\n"
            "-----\n\n"
            "User request:\n"
            f"{question}\n\n"
        )
    else:
        source_block = (
            "Create a rich, multi-section Markdown document on:\n"
            f"{question}\n\n"
        )

    return (
        "You are a professional document author and senior analyst. "
        "You produce McKinsey/BCG-quality Markdown documents for executive audiences.\n\n"

        + section_rule +

        source_block +

        "Respond with ONLY valid JSON — no markdown fences, no explanation.\n\n"

        "JSON SCHEMA (follow exactly — use standard single-brace JSON syntax):\n"
        '{\n'
        '  "title": "<concise 3-7 word professional title — NO request verbs like generate/create/write/make>",\n'
        '  "domain": "<single industry keyword: payments|ai|healthcare|government|banking|'
        'fintech|cybersecurity|legal|education|heritage|sports|esg|retail|luxury|startup|'
        'media|travel|food|hr|executive|infrastructure|default>",\n'
        '  "sections": [\n'
        '    {\n'
        '      "heading": "Executive Summary",\n'
        '      "subheading": "",\n'
        '      "level": 1,\n'
        '      "content": "<2-3 substantive paragraphs: context, key findings, significance>",\n'
        '      "bullets": [],\n'
        '      "callout": {"label": "Key Highlight", "text": "<single most important insight from this section>"},\n'
        '      "table": null\n'
        '    },\n'
        '    {\n'
        '      "heading": "<Core Section Heading>",\n'
        '      "subheading": "<optional H3 sub-label>",\n'
        '      "level": 1,\n'
        '      "content": "<3-4 analytical paragraphs with real data, percentages, named entities>",\n'
        '      "bullets": ["<complete insight sentence>","<complete insight sentence>","<complete insight sentence>"],\n'
        '      "callout": {"label": "Market Inflection", "text": "<key statistic or data point>"},\n'
        '      "table": {"headers": ["<Col 1>","<Col 2>","<Col 3>"], "rows": [["<v>","<v>","<v>"],["<v>","<v>","<v>"],["<v>","<v>","<v>"]]}\n'
        '    },\n'
        '    {\n'
        '      "heading": "<Analysis Section>",\n'
        '      "subheading": "",\n'
        '      "level": 2,\n'
        '      "content": "<2-3 paragraphs>",\n'
        '      "bullets": ["<insight>","<insight>","<insight>"],\n'
        '      "callout": {"label": "Critical Finding", "text": "<analytical insight>"},\n'
        '      "table": null\n'
        '    },\n'
        '    {\n'
        '      "heading": "Challenges & Risk Factors",\n'
        '      "subheading": "",\n'
        '      "level": 1,\n'
        '      "content": "<2-3 paragraphs identifying key risks>",\n'
        '      "bullets": ["<risk 1>","<risk 2>","<risk 3>"],\n'
        '      "callout": {"label": "Risk Alert", "text": "<primary risk or challenge>"},\n'
        '      "table": null\n'
        '    },\n'
        '    {\n'
        '      "heading": "Strategic Recommendations",\n'
        '      "subheading": "",\n'
        '      "level": 1,\n'
        '      "content": "<2-3 paragraphs with actionable guidance>",\n'
        '      "bullets": ["<recommendation 1>","<recommendation 2>","<recommendation 3>","<recommendation 4>"],\n'
        '      "callout": {"label": "Strategic Vision", "text": "<primary strategic goal>"},\n'
        '      "table": null\n'
        '    },\n'
        '    {\n'
        '      "heading": "Conclusion",\n'
        '      "subheading": "",\n'
        '      "level": 1,\n'
        '      "content": "<strong closing paragraph with forward-looking statement>",\n'
        '      "bullets": [],\n'
        '      "callout": {"label": "Closing Insight", "text": "<closing insight or call to action>"},\n'
        '      "table": null\n'
        '    }\n'
        '  ]\n'
        '}\n\n'

        f"DOCUMENT QUALITY RULES:\n"
        f"1. title: 3-7 words, professional noun phrase, NO verbs like generate/create/write/make/report/document.\n"
        f"   GOOD: 'UPI Payments in India 2025'  BAD: 'Generate a Report on UPI'\n"
        f"2. {section_count_inline}\n"
        "3. Each section's 'content' must be 2-4 full analytical paragraphs with real data.\n"
        "4. All data must be realistic and specific: real percentages, years, named entities.\n"
        "5. Bullets must be complete, insightful sentences — not fragments.\n"
        "6. Every section must have a callout box with a DESCRIPTIVE label (e.g. 'Key Highlight',\n"
        "   'Market Inflection', 'Critical Finding', 'Risk Alert', 'Strategic Vision',\n"
        "   'Growth Driver', 'Data Insight', 'Action Required', 'Closing Insight').\n"
        "   Do NOT use generic labels like KEY, STAT, INSIGHT, GOAL, NOTE.\n"
        "7. At least 2 sections must include a data table with 3+ columns and 3+ rows.\n"
        "8. domain must be a single lowercase keyword from the allowed list.\n"
        "9. Output tone: Executive briefing — authoritative, data-driven, factual.\n"
        "10. Output RAW JSON ONLY. Any non-JSON text is a failure.\n\n"
        "MARKDOWN RENDERING NOTES (the renderer handles this automatically):\n"
        "  - callout boxes → rendered as GFM blockquotes: > **Label:** text\n"
        "  - tables → rendered as GFM pipe tables\n"
        "  - level 1 sections → ## headings with section numbers (1., 2., ...)\n"
        "  - level 2 sections → ### headings with sub-numbers (1.1, 1.2, ...)\n"
        "  - subheadings → #### headings\n"
        "  - bullets → - bullet items\n"
    )


def _build_md_edit_prompt(
    question: str,
    edit_request: str,
    current_content: str,
    sections: list,
    conversation_summary: str,
) -> str:
    """
    Build the LLM prompt for editing an existing Markdown document.

    Returns a structured JSON patch (not a full regeneration) so only
    the affected sections need to be re-generated.
    """
    sections_json = json.dumps(sections, indent=2, ensure_ascii=False)
    section_count = len(sections)

    return (
        "You are a professional document editor. You are editing an existing Markdown document.\n\n"

        f"ORIGINAL DOCUMENT REQUEST:\n{question}\n\n"

        f"CONVERSATION HISTORY (last turns):\n{conversation_summary}\n\n"

        f"CURRENT DOCUMENT SECTION COUNT: {section_count}\n\n"

        "CURRENT DOCUMENT CONTENT:\n"
        "---\n"
        f"{current_content[:6000]}\n"
        "---\n\n"

        "CURRENT SECTIONS STRUCTURE (JSON):\n"
        f"{sections_json[:4000]}\n\n"

        f"USER EDIT REQUEST:\n{edit_request}\n\n"

        "Respond with ONLY valid JSON — no markdown fences, no explanation.\n\n"

        "JSON SCHEMA — choose the right variant based on the edit type:\n\n"

        "For INSERT (add new section(s)):\n"
        '{"edit_type":"insert_section","edit_summary":"<1-2 sentence description>","target_heading":null,'
        '"insert_after":"<exact heading to insert AFTER, or null to append>","new_sections":[{...full section schema...}],'
        '"updated_section":null,"full_sections":null}\n\n'

        "For REWRITE (rewrite a single section):\n"
        '{"edit_type":"rewrite_section","edit_summary":"<description>","target_heading":"<exact heading>",'
        '"insert_after":null,"new_sections":null,"updated_section":{...full section schema...},"full_sections":null}\n\n'

        "For ADD TABLE (add a table to an existing section):\n"
        '{"edit_type":"add_table","edit_summary":"<description>","target_heading":"<exact heading>",'
        '"insert_after":null,"new_sections":null,"updated_section":null,'
        '"table_data":{"headers":["<Col 1>","<Col 2>"],"rows":[["<v>","<v>"]]},"full_sections":null}\n\n'

        "For ADD BULLETS (add bullet points to an existing section):\n"
        '{"edit_type":"add_bullets","edit_summary":"<description>","target_heading":"<exact heading>",'
        '"insert_after":null,"new_sections":null,"updated_section":null,'
        '"new_bullets":["<bullet 1>","<bullet 2>"],"full_sections":null}\n\n'

        "For DELETE (remove a section):\n"
        '{"edit_type":"delete_section","edit_summary":"<description>","target_heading":"<exact heading>",'
        '"insert_after":null,"new_sections":null,"updated_section":null,"full_sections":null}\n\n'

        "For RENAME (rename a section heading):\n"
        '{"edit_type":"rename_section","edit_summary":"<description>","target_heading":"<exact current heading>",'
        '"new_heading":"<new heading text>","insert_after":null,"new_sections":null,'
        '"updated_section":null,"full_sections":null}\n\n'

        "For GLOBAL EDIT (tone change, full restructure, major rewrite):\n"
        '{"edit_type":"global_edit","edit_summary":"<description of global changes>",'
        '"target_heading":null,"insert_after":null,"new_sections":null,"updated_section":null,'
        '"full_sections":[...complete updated sections array...]}\n\n'

        "EDIT QUALITY RULES:\n"
        "1. Preserve all existing sections NOT being modified.\n"
        "2. New/rewritten content must match the quality and tone of the existing document.\n"
        "3. All data: realistic and specific — no placeholder text.\n"
        "4. Callout labels: descriptive (not KEY, STAT, INSIGHT, GOAL, NOTE).\n"
        "5. For global edits: reproduce ALL sections (modified + unmodified) in full_sections.\n"
        "6. edit_summary: clear, human-readable description of what changed.\n"
        "7. target_heading must match EXACTLY one of the existing section headings.\n"
        "8. Output RAW JSON ONLY. Any non-JSON text is a failure.\n"
    )


# ══════════════════════════════════════════════════════════════
# SUMMARY + PREVIEW BUILDER
# ══════════════════════════════════════════════════════════════

_PREVIEW_MAX_SECTIONS = 3
_PREVIEW_SNIPPET_CHARS = 280
_SUMMARY_MAX_BULLETS = 5


def _first_sentence(text: str, max_chars: int = 180) -> str:
    """Pull the first reasonable sentence out of a content blob."""
    t = (text or "").strip()
    if not t:
        return ""
    # Strip markdown emphasis / list markers from the leading edge
    t = re.sub(r"^[\-\*\>\#\s]+", "", t)
    m = re.search(r"(.+?[\.\!\?])(\s|$)", t)
    snippet = m.group(1).strip() if m else t
    if len(snippet) > max_chars:
        snippet = snippet[: max_chars - 1].rstrip() + "…"
    return snippet


def _section_text(sec: dict) -> str:
    """Best-effort body text for a section/slide (PPTX slides use key_message)."""
    return (
        (sec.get("content") or "").strip()
        or (sec.get("key_message") or "").strip()
        or (sec.get("subheading") or "").strip()
    )


def _deterministic_summary(sections: list) -> list:
    """Fallback summary: first sentence of each top-level section, up to 5."""
    bullets: list = []
    for sec in sections or []:
        h = (sec.get("heading") or "").strip()
        content = _section_text(sec)
        first = _first_sentence(content) or h
        if first:
            # Combine "Heading: first sentence" when both exist for context
            if h and first and h.lower() not in first.lower():
                bullets.append(f"{h}: {first}")
            else:
                bullets.append(first)
        if len(bullets) >= _SUMMARY_MAX_BULLETS:
            break
    return bullets


def _build_preview(title: str, sections: list) -> dict:
    """
    Build a deterministic preview from the sections list.

    Returns:
        {
          "title": str,
          "intro": str,          # first non-empty paragraph of section 0
          "sections": [
              {"heading": str, "snippet": str},
              ...
          ],
          "truncated": bool,     # True when more sections exist than shown
        }
    """
    secs = sections or []
    intro = ""
    if secs:
        first = secs[0]
        content = _section_text(first)
        if content:
            para = next((p.strip() for p in content.split("\n\n") if p.strip()), "")
            intro = para[: _PREVIEW_SNIPPET_CHARS].rstrip()
            if len(para) > _PREVIEW_SNIPPET_CHARS:
                intro += "…"

    preview_sections: list = []
    for sec in secs[: _PREVIEW_MAX_SECTIONS]:
        heading = (sec.get("heading") or "").strip()
        content = _section_text(sec)
        bullets = sec.get("bullets") or []
        if content:
            snippet = content[: _PREVIEW_SNIPPET_CHARS].rstrip()
            if len(content) > _PREVIEW_SNIPPET_CHARS:
                snippet += "…"
        elif bullets:
            snippet = " · ".join(str(b).strip() for b in bullets[:2] if str(b).strip())
            if len(snippet) > _PREVIEW_SNIPPET_CHARS:
                snippet = snippet[: _PREVIEW_SNIPPET_CHARS - 1].rstrip() + "…"
        else:
            snippet = ""
        if heading or snippet:
            preview_sections.append({"heading": heading, "snippet": snippet})

    return {
        "title": title or "",
        "intro": intro,
        "sections": preview_sections,
        "truncated": len(secs) > _PREVIEW_MAX_SECTIONS,
    }


def _build_summary_prompt(title: str, sections: list, original_prompt: str) -> str:
    """LLM prompt for the 5-bullet plain-language summary."""
    # Reuse the rendered markdown as compact, well-structured context.
    rendered = _sections_to_md(title or "Document", sections or [])
    # Cap the context — the fast model doesn't need the whole doc.
    if len(rendered) > 6000:
        rendered = rendered[:6000] + "\n\n[...truncated...]"
    return (
        "You are summarising a document for a busy reader.\n\n"
        f"Original request: {original_prompt!r}\n\n"
        "Document:\n"
        "------------\n"
        f"{rendered}\n"
        "------------\n\n"
        "Write a TL;DR as a JSON object: {\"bullets\": [\"...\", \"...\"]}.\n"
        "Rules:\n"
        f"- At most {_SUMMARY_MAX_BULLETS} bullets.\n"
        "- Each bullet ≤ 18 words.\n"
        "- Plain language, ~6th-grade reading level. No jargon.\n"
        "- Cover the most important points, in document order.\n"
        "- Output RAW JSON ONLY. No prose, no markdown fences.\n"
    )


def build_summary_and_preview(
    title: str,
    sections: list,
    prompt: str,
    chat_id: str | None = None,
) -> tuple[list, dict, dict]:
    """
    Produce a short summary + structured preview for a generated document.

    Args:
        title:   Document title.
        sections: The same `sections` list returned by generate_md_doc /
                  doc_worker's LLM structuring step.
        prompt:  Original user request (used for summary grounding).
        chat_id: Optional chat session id (used only for log context).

    Returns:
        (summary, preview, summary_meta)
          summary:      list[str] (≤ 5 bullets)
          preview:      dict (see _build_preview)
          summary_meta: dict with keys {tokens, in_tok, out_tok, cost_usd,
                         model, latency, source} — `source` is "llm" or
                         "fallback".

    Uses a lightweight model (model_hint="haiku") — this is a cosmetic ≤5
    bullet summary of content the structuring step already authored, not new
    authoring, so it does not need the full Sonnet 4.6 model used for
    structuring. (Previously used model_hint="complex"/Sonnet, adding several
    seconds of blocking latency between the file being ready and the result
    being published — see doc_worker.py's `_attach_summary_preview` call
    site.) On any LLM/parse error the function falls back to a deterministic
    per-section summary and never raises — callers can publish results
    safely.
    """
    ctx = f"summary:{chat_id or 'no-chat'}"
    preview = _build_preview(title, sections)
    summary_meta: dict = {
        "tokens":   0,
        "in_tok":   0,
        "out_tok":  0,
        "cost_usd": 0.0,
        "model":    None,
        "latency":  0.0,
        "source":   "fallback",
    }

    if not sections:
        return [], preview, summary_meta

    # ── Try LLM summarisation ──────────────────────────────────
    # Uses model_hint="haiku" → Claude Haiku (lightweight/fast). See
    # models/model_router.py routing table for the haiku tier's fallback chain.
    try:
        from models.model_router import model_router
        prompt_text = _build_summary_prompt(title, sections, prompt or "")
        logger.info(f"[md_agent] summary LLM call | context={ctx!r} "
                    f"prompt_len={len(prompt_text)}")
        result = model_router.generate(
            prompt_text, model_hint="haiku", return_meta=True
        )
        raw = (result.get("text") or "").strip()
        meta = result.get("meta") or {}
        parsed = _parse_llm_json(raw, context=ctx) if raw else {}
        bullets_raw = parsed.get("bullets") or []
        bullets = [
            str(b).strip()
            for b in bullets_raw
            if b and str(b).strip()
        ][:_SUMMARY_MAX_BULLETS]
        if bullets:
            in_tok = int(meta.get("in_tok") or 0)
            out_tok = int(meta.get("out_tok") or 0)
            total = int(meta.get("tokens") or (in_tok + out_tok))
            summary_meta.update({
                "tokens":   total,
                "in_tok":   in_tok,
                "out_tok":  out_tok,
                "cost_usd": float(meta.get("cost_usd") or 0.0),
                "model":    meta.get("model"),
                "latency":  float(meta.get("latency") or 0.0),
                "source":   "llm",
            })
            logger.info(f"[md_agent] summary OK | context={ctx!r} "
                        f"bullets={len(bullets)} model={summary_meta['model']} "
                        f"tokens={total} cost={summary_meta['cost_usd']:.6f}")
            return bullets, preview, summary_meta
        logger.warning(f"[md_agent] summary LLM returned no bullets | context={ctx!r}")
    except Exception as exc:
        logger.warning(f"[md_agent] summary LLM failed, using fallback | "
                       f"context={ctx!r} error={exc}")

    # ── Deterministic fallback ─────────────────────────────────
    bullets = _deterministic_summary(sections)
    logger.info(f"[md_agent] summary fallback | context={ctx!r} bullets={len(bullets)}")
    return bullets, preview, summary_meta


# ══════════════════════════════════════════════════════════════
# MARKDOWN RENDERER
# ══════════════════════════════════════════════════════════════

def _sections_to_md(title: str, sections: list) -> str:
    """
    Convert a title + sections list to rich GFM Markdown.

    Extends tools.doc_generator.generate_md() to also render:
      - Cover metadata line (Prepared by AiNxt · date · CONFIDENTIAL)
      - Callout boxes → GFM blockquotes: > **Label:** text
      - Data tables → GFM pipe tables
      - Subheadings → #### headings
      - Section numbering: H1 → 1., 2., ...  H2 → 1.1, 1.2, ...
      - Horizontal rules between H1 sections

    This matches the visual standard of generate_docx() / generate_pdf()
    as closely as Markdown allows.
    """
    today = _date.today().strftime("%B %d, %Y")
    lines = [
        f"# {title}",
        "",
        f"> *Prepared by AiNxt Platform · {today} · CONFIDENTIAL*",
        "",
        "---",
        "",
    ]

    _h1_counter = 0
    _h2_counter = 0

    def _strip_leading_number(text: str) -> str:
        return re.sub(r"^\s*\d+[\.\)]\s*", "", text).strip()

    for sec_idx, sec in enumerate(sections):
        h          = (sec.get("heading")    or "").strip()
        subheading = (sec.get("subheading") or "").strip()
        content    = (sec.get("content")    or "").strip()
        bullets    = sec.get("bullets") or []
        callout    = sec.get("callout")
        table      = sec.get("table")
        level      = int(sec.get("level") or 2)

        # ── Heading with section numbering ────────────────────
        if h:
            clean_h = _strip_leading_number(h)
            if level == 1:
                _h1_counter += 1
                _h2_counter  = 0
                lines.append(f"## {_h1_counter}.  {clean_h}")
            else:
                _h2_counter += 1
                lines.append(f"### {_h1_counter}.{_h2_counter}  {clean_h}")
            lines.append("")

        # ── Subheading (H3 → H4 in Markdown) ─────────────────
        if subheading:
            lines.append(f"#### {subheading}")
            lines.append("")

        # ── Body paragraphs ───────────────────────────────────
        if content:
            for para in [p.strip() for p in content.split("\n\n") if p.strip()]:
                lines.append(para)
                lines.append("")

        # ── Callout box → GFM blockquote ─────────────────────
        if callout and isinstance(callout, dict):
            label = (callout.get("label") or "Key Highlight").strip()
            text  = (callout.get("text")  or "").strip()
            if text:
                lines.append(f"> **{label.title()}:** {text}")
                lines.append("")

        # ── Bullet list ───────────────────────────────────────
        if bullets:
            for b in bullets:
                if b and str(b).strip():
                    lines.append(f"- {str(b).strip()}")
            lines.append("")

        # ── GFM pipe table ────────────────────────────────────
        if table and isinstance(table, dict):
            headers = table.get("headers") or []
            rows    = table.get("rows")    or []
            if headers:
                # Header row
                lines.append("| " + " | ".join(str(h).strip() for h in headers) + " |")
                # Separator row
                lines.append("| " + " | ".join("---" for _ in headers) + " |")
                # Data rows
                for row in rows:
                    padded = list(row) + [""] * max(0, len(headers) - len(row))
                    lines.append("| " + " | ".join(str(v).strip() for v in padded[:len(headers)]) + " |")
                lines.append("")

        # ── Section divider after H1 (except last section) ───
        if level == 1 and sec_idx < len(sections) - 1:
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# EDIT CLASSIFICATION & PATCH APPLICATION
# ══════════════════════════════════════════════════════════════

def _classify_edit_type(edit_request: str) -> str:
    """
    Heuristic classifier for the type of edit being requested.
    Used for logging and progress messages — the LLM determines
    the actual edit_type in the patch response.
    """
    text = edit_request.lower()
    if re.search(r"\b(add|insert|include|append|create)\b.{0,40}\b(section|chapter|part)\b", text):
        return "insert_section"
    if re.search(r"\b(rewrite|rephrase|revise|update|change|improve|expand|shorten)\b.{0,40}\b(section|summary|introduction|conclusion)\b", text):
        return "rewrite_section"
    if re.search(r"\b(add|insert|include)\b.{0,40}\b(table|data|statistics|numbers)\b", text):
        return "add_table"
    if re.search(r"\b(add|include|append)\b.{0,40}\b(bullet|point|item|list)\b", text):
        return "add_bullets"
    if re.search(r"\b(delete|remove|drop|eliminate)\b.{0,40}\b(section|chapter|part)\b", text):
        return "delete_section"
    if re.search(r"\b(rename|retitle|change.{0,10}title|change.{0,10}heading)\b", text):
        return "rename_section"
    if re.search(r"\b(tone|style|format|restructure|rewrite.{0,10}entire|global|throughout|all sections)\b", text):
        return "global_edit"
    return "unknown"


def _apply_section_patch(current_sections: list, patch: dict) -> list:
    """
    Apply a structured JSON patch to the current sections list.

    Supported edit_types:
      insert_section  — splice new_sections after insert_after heading (or append)
      rewrite_section — replace target section with updated_section
      add_table       — set section["table"] on target
      add_bullets     — extend section["bullets"] on target
      delete_section  — remove target section
      rename_section  — update heading of target
      global_edit     — replace all sections with full_sections

    Returns the updated sections list.
    """
    edit_type      = patch.get("edit_type", "unknown")
    target_heading = (patch.get("target_heading") or "").strip()
    sections       = [dict(s) for s in current_sections]  # shallow copy

    def _find_idx(heading: str) -> int:
        """Find section index by heading (case-insensitive, strip leading numbers)."""
        h_clean = re.sub(r"^\s*\d+[\.\)]\s*", "", heading).strip().lower()
        for i, s in enumerate(sections):
            s_clean = re.sub(r"^\s*\d+[\.\)]\s*", "", (s.get("heading") or "")).strip().lower()
            if s_clean == h_clean:
                return i
        # Fuzzy fallback: partial match
        for i, s in enumerate(sections):
            s_clean = re.sub(r"^\s*\d+[\.\)]\s*", "", (s.get("heading") or "")).strip().lower()
            if h_clean in s_clean or s_clean in h_clean:
                return i
        return -1

    if edit_type == "insert_section":
        new_sections = patch.get("new_sections") or []
        insert_after = (patch.get("insert_after") or "").strip()
        if insert_after:
            idx = _find_idx(insert_after)
            if idx >= 0:
                for i, ns in enumerate(new_sections):
                    sections.insert(idx + 1 + i, ns)
            else:
                logger.warning(f"[md_agent] insert_after heading not found: {insert_after!r} — appending")
                sections.extend(new_sections)
        else:
            sections.extend(new_sections)

    elif edit_type == "rewrite_section":
        updated = patch.get("updated_section")
        if updated and target_heading:
            idx = _find_idx(target_heading)
            if idx >= 0:
                sections[idx] = updated
            else:
                logger.warning(f"[md_agent] rewrite target not found: {target_heading!r} — appending")
                sections.append(updated)

    elif edit_type == "add_table":
        table_data = patch.get("table_data")
        if table_data and target_heading:
            idx = _find_idx(target_heading)
            if idx >= 0:
                sections[idx] = dict(sections[idx])
                sections[idx]["table"] = table_data
            else:
                logger.warning(f"[md_agent] add_table target not found: {target_heading!r}")

    elif edit_type == "add_bullets":
        new_bullets = patch.get("new_bullets") or []
        if new_bullets and target_heading:
            idx = _find_idx(target_heading)
            if idx >= 0:
                sections[idx] = dict(sections[idx])
                existing = list(sections[idx].get("bullets") or [])
                sections[idx]["bullets"] = existing + new_bullets
            else:
                logger.warning(f"[md_agent] add_bullets target not found: {target_heading!r}")

    elif edit_type == "delete_section":
        if target_heading:
            idx = _find_idx(target_heading)
            if idx >= 0:
                sections.pop(idx)
            else:
                logger.warning(f"[md_agent] delete target not found: {target_heading!r}")

    elif edit_type == "rename_section":
        new_heading = (patch.get("new_heading") or "").strip()
        if new_heading and target_heading:
            idx = _find_idx(target_heading)
            if idx >= 0:
                sections[idx] = dict(sections[idx])
                sections[idx]["heading"] = new_heading
            else:
                logger.warning(f"[md_agent] rename target not found: {target_heading!r}")

    elif edit_type == "global_edit":
        full_sections = patch.get("full_sections")
        if full_sections:
            sections = full_sections
        else:
            logger.warning("[md_agent] global_edit: full_sections is empty — no change")

    else:
        logger.warning(f"[md_agent] unknown edit_type: {edit_type!r} — no change applied")

    return sections


# ══════════════════════════════════════════════════════════════
# COMPLIANCE CHECK
# ══════════════════════════════════════════════════════════════

def _compliance_check(text: str) -> bool:
    """
    Run compliance gate on text.
    Returns True if content is BLOCKED, False if allowed.
    Fail-open: returns False (allow) on any error.
    """
    try:
        from agents.compliance_engine import compliance_engine as _ce
        chk = _ce.validate_input(text[:4000])
        return bool(chk.get("blocked"))
    except Exception as _ce_err:
        logger.warning(f"[md_agent] compliance check failed (fail-open): {_ce_err}")
        return False


# ══════════════════════════════════════════════════════════════
# PUBLIC PYTHON API
# ══════════════════════════════════════════════════════════════

def generate_md_doc(
    prompt: str,
    output_path: str | None = None,
    chat_id: str | None = None,
    model_hint: str = "complex",
    user_id: str | None = None,
    on_section=None,
    on_title=None,
    chat_context: str = "",
) -> dict:
    """
    Generate a new Markdown document from a user prompt.

    Args:
        prompt:      User's document request (e.g. "Write a report on UPI payments")
        output_path: Optional absolute path for the output .md file.
                     If None, auto-generated in DOC_DIR using smart_filename.
        chat_id:     Chat session ID for context persistence (optional).
        model_hint:  LLM routing hint (default: "complex" → Claude Sonnet).
        chat_context: This chat's conversation history, passed ONLY when the
                     caller determined the document's source is the chat itself
                     (e.g. "summarize this chat into a document"). Strictly this
                     chat's history — never KB/codebase. Empty for topic-only
                     generation ("write a report on UPI"), which stays a clean
                     fresh authoring with no prior-turn bleed.

    Returns:
        {
          "file_id":     str,   # UUID of the generated file
          "output_path": str,   # absolute path to the .md file
          "filename":    str,   # e.g. "upi_payments_in_india_2025.md"
          "title":       str,
          "domain":      str,
          "sections":    list,  # structured sections (for session persistence)
          "word_count":  int,
          "meta":        dict,  # LLM metadata (model, tokens, cost, latency)
        }

    Raises:
        ValueError:   Empty prompt or compliance block.
        RuntimeError: LLM failure or JSON parse failure.
        IOError:      File write failure.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt must not be empty")

    logger.info(f"[md_agent] generate_md_doc START | chat_id={chat_id!r} chat_context_len={len((chat_context or '').strip())}")

    # ── Compliance gate ────────────────────────────────────────
    if _compliance_check(prompt):
        raise ValueError("Content blocked by compliance policy")

    # ── LLM structuring ───────────────────────────────────────
    struct_prompt = _build_md_prompt(prompt, chat_context=chat_context)
    # When on_section is provided we stream so the chat UI can paint
    # sections live; otherwise we use the non-streaming path for callers
    # that don't care about progressive output (CLI, tests, batch jobs).
    # `model_hint` is honoured in both branches so callers can route the
    # MD generation through any model_router tier (defaults to "complex").
    if on_section is not None or on_title is not None:
        raw, llm_meta = _llm_call_stream(
            struct_prompt,
            context=f"generate:{chat_id or 'no-chat'}",
            on_section=on_section,
            on_title=on_title,
            model_hint=model_hint,
        )
    else:
        raw, llm_meta = _llm_call(
            struct_prompt,
            context=f"generate:{chat_id or 'no-chat'}",
            model_hint=model_hint,
        )

    if not raw:
        raise RuntimeError("LLM returned empty response")

    struct = _parse_llm_json(raw, context=f"generate:{chat_id or 'no-chat'}")

    # ── Extract title, domain, sections ───────────────────────
    raw_llm_title = (struct.get("title") or "").strip()
    title         = _sanitize_llm_title(raw_llm_title, prompt)
    domain        = (struct.get("domain") or "").strip().lower() or None
    sections      = struct.get("sections") or []
    llm_meta["domain"] = domain

    if not sections:
        raise RuntimeError("LLM returned no sections")

    logger.info(f"[md_agent] LLM structuring DONE | title={title!r} domain={domain!r} "
                f"sections={len(sections)} model={llm_meta.get('model')}")

    # ── Render Markdown ────────────────────────────────────────
    content = _sections_to_md(title, sections)
    word_count = len(content.split())

    # ── Resolve output path ────────────────────────────────────
    file_id = str(_uuid_mod.uuid4())

    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        path     = output_path
        filename = os.path.basename(output_path)
    else:
        _base    = smart_filename(title=title, question=prompt, fmt_ext="md")
        filename = f"{_base}.md"
        # Per-user subdirectory when user_id provided; falls back to base
        # DOC_DIR for ad-hoc CLI/test calls without a user context.
        _out_dir = user_doc_dir(user_id, chat_id) if user_id else DOC_DIR
        path     = os.path.join(_out_dir, f"{file_id}.md")

    # ── Write file ─────────────────────────────────────────────
    # Atomic write (write-to-.partial + fsync + rename) so the download
    # endpoint on a different instance never sees a half-written file.
    try:
        from workers.doc_worker import _atomic_write_text
        _atomic_write_text(path, content)
        logger.info(f"[md_agent] file written | path={path} words={word_count}")
    except Exception as exc:
        raise IOError(f"File write error: {exc}") from exc

    result = {
        "file_id":     file_id,
        "output_path": path,
        "filename":    filename,
        "title":       title,
        "domain":      domain,
        "sections":    sections,
        "word_count":  word_count,
        "content":     content,
        "meta":        llm_meta,
    }
    logger.info(f"[md_agent] generate_md_doc DONE | file_id={file_id} filename={filename} "
                f"words={word_count} model={llm_meta.get('model')}")
    return result


def edit_md_doc(
    edit_request: str,
    chat_id: str,
    current_sections: list,
    current_content: str,
    original_question: str,
    conversation_history: list,
    title: str,
    domain: str | None = None,
    output_path: str | None = None,
    model_hint: str = "complex",
) -> dict:
    """
    Apply an edit to an existing Markdown document.

    Args:
        edit_request:         User's edit request (e.g. "Add a section on risks")
        chat_id:              Chat session ID (for logging)
        current_sections:     Current sections list (from session)
        current_content:      Current Markdown content (from session content_snapshot)
        original_question:    The original generation prompt (for context)
        conversation_history: List of prior conversation turns (for context)
        title:                Current document title
        domain:               Current document domain
        output_path:          Path to write the updated .md file
        model_hint:           LLM routing hint

    Returns:
        {
          "output_path":       str,
          "edit_summary":      str,
          "edit_type":         str,
          "sections_before":   int,
          "sections_after":    int,
          "sections_affected": list[str],
          "sections":          list,   # updated sections
          "content":           str,    # updated Markdown content
          "word_count":        int,
          "meta":              dict,
        }

    Raises:
        ValueError:   Empty edit_request or compliance block.
        RuntimeError: LLM failure or JSON parse failure.
        IOError:      File write failure.
    """
    edit_request = (edit_request or "").strip()
    if not edit_request:
        raise ValueError("edit_request must not be empty")

    logger.info(f"[md_agent] edit_md_doc START | chat_id={chat_id!r} "
                f"edit_preview={edit_request[:80]!r}")

    # ── Compliance gate ────────────────────────────────────────
    if _compliance_check(edit_request):
        raise ValueError("Content blocked by compliance policy")

    # ── Build conversation summary (last 5 turns) ─────────────
    recent = conversation_history[-10:] if len(conversation_history) > 10 else conversation_history
    conv_summary_lines = []
    for turn in recent:
        role      = turn.get("role", "?")
        turn_type = turn.get("type", "")
        content   = (turn.get("content") or "")[:200]
        conv_summary_lines.append(f"[{turn_type}] {role.capitalize()}: {content}")
    conversation_summary = "\n".join(conv_summary_lines) or "(no prior history)"

    # ── Classify edit type (for logging) ──────────────────────
    heuristic_type = _classify_edit_type(edit_request)
    logger.info(f"[md_agent] heuristic edit_type={heuristic_type!r} | chat_id={chat_id!r}")

    # ── LLM edit call ──────────────────────────────────────────
    edit_prompt = _build_md_edit_prompt(
        question=original_question,
        edit_request=edit_request,
        current_content=current_content,
        sections=current_sections,
        conversation_summary=conversation_summary,
    )
    raw, llm_meta = _llm_call(edit_prompt, context=f"edit:{chat_id}")

    if not raw:
        raise RuntimeError("LLM returned empty response for edit")

    patch = _parse_llm_json(raw, context=f"edit:{chat_id}")

    # ── Apply patch ────────────────────────────────────────────
    sections_before = len(current_sections)
    updated_sections = _apply_section_patch(current_sections, patch)
    sections_after   = len(updated_sections)

    edit_type    = patch.get("edit_type", heuristic_type)
    edit_summary = patch.get("edit_summary", f"Applied {edit_type} edit")

    # Determine which sections were affected
    target_heading = (patch.get("target_heading") or "").strip()
    new_sections   = patch.get("new_sections") or []
    sections_affected = []
    if target_heading:
        sections_affected.append(target_heading)
    for ns in new_sections:
        h = (ns.get("heading") or "").strip()
        if h:
            sections_affected.append(h)

    logger.info(f"[md_agent] patch applied | edit_type={edit_type!r} "
                f"sections: {sections_before} → {sections_after} | chat_id={chat_id!r}")

    # ── Render updated Markdown ────────────────────────────────
    content    = _sections_to_md(title, updated_sections)
    word_count = len(content.split())

    # ── Write file ─────────────────────────────────────────────
    if output_path:
        # Defensive guard: edit_md_doc renders raw Markdown. Refuse to write it
        # into a non-.md path to prevent silently clobbering binary artifacts
        # (docx/pdf/xlsx) — see workers/doc_worker_agent.py for the sidecar
        # routing that callers should perform when original_format is binary.
        _ext = os.path.splitext(output_path)[1].lower()
        if _ext and _ext != ".md":
            raise ValueError(
                f"edit_md_doc refuses to write Markdown to non-.md path "
                f"{output_path!r} (extension={_ext!r}). Callers must route MD "
                f"writes to a .md sidecar when the original format is binary."
            )
        try:
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            from workers.doc_worker import _atomic_write_text
            _atomic_write_text(output_path, content)
            logger.info(f"[md_agent] file updated | path={output_path} words={word_count}")
        except Exception as exc:
            raise IOError(f"File write error: {exc}") from exc

    result = {
        "output_path":       output_path,
        "edit_summary":      edit_summary,
        "edit_type":         edit_type,
        "sections_before":   sections_before,
        "sections_after":    sections_after,
        "sections_affected": sections_affected,
        "sections":          updated_sections,
        "content":           content,
        "word_count":        word_count,
        "meta":              llm_meta,
    }
    logger.info(f"[md_agent] edit_md_doc DONE | edit_type={edit_type!r} "
                f"summary={edit_summary!r} model={llm_meta.get('model')}")
    return result
