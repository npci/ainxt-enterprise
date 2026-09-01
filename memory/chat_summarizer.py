# SPDX-License-Identifier: Apache-2.0
# ============================================================
# CHAT SUMMARIZER — rolling per-chat summary (ChatGPT-style)
#
# Stores summary in existing agent_memory table:
#   agent_name = "chat:{chat_id}"
#   key        = "rolling_summary"
#
# Uses gpt-5-mini (simple tier via Local LLM proxy) — cheap, fast.
# Called in background thread after _save_chat_messages.
# Only triggers when raw history exceeds 800-token threshold.
# ============================================================

import json
import re
from core.logger import logger

_CHARS_PER_TOKEN    = 4
_TRIGGER_TOKENS     = 800   # only summarise when history exceeds this
_MAX_SUMMARY_CHARS  = 1200  # ~300 tokens — stays flat forever


def _count_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _to_plain_english(text: str) -> str:
    """
    Reduce a message to plain English prose WHILE PRESERVING exact values.

    Large code fences and huge blobs are dropped, but scalar key:value pairs
    (e.g. version: v4.18.2, budget: 2.7M, ratio: 0.7183) are flattened to
    prose rather than deleted — exact numbers/IDs/dates must survive so the
    summariser can retain them.
    """
    # Fenced code blocks → drop the code but keep any scalar key:value lines,
    # flattened, so exact values inside a small config block survive.
    def _keep_scalars(m):
        body = m.group(0)
        kept = []
        # Split on newlines AND commas so inline JSON ({"a": 1, "b": 2}) and
        # multi-line YAML both yield individual key:value fragments.
        for frag in re.split(r'[\n,]', body):
            frag = frag.strip().strip('{}[]')
            kv = re.match(r'\s*["\']?([A-Za-z][\w .\-]{0,40}?)["\']?\s*[:=]\s*'
                          r'["\']?([^\n,"\']{1,60}?)["\']?\s*$', frag)
            if kv and re.search(r'\d', kv.group(2)):
                kept.append(f"{kv.group(1).strip()} is {kv.group(2).strip()}.")
        return " " + " ".join(kept) + " " if kept else " "
    text = re.sub(r'```[\s\S]*?```', _keep_scalars, text)
    # Indented code lines
    text = re.sub(r'(?m)^(    |\t).+', ' ', text)
    # JSON / dict / array blobs → keep scalar key:values, drop the braces.
    text = re.sub(r'\{[^{}]{0,800}\}', _keep_scalars, text)
    text = re.sub(r'\[[^\]]{40,}\]',  ' ', text)
    # URLs
    text = re.sub(r'https?://\S+', ' ', text)
    # Markdown noise
    text = re.sub(r'^#{1,6}\s+',           '', text, flags=re.MULTILINE)
    text = re.sub(r'\*{1,2}([^*\n]+)\*{1,2}', r'\1', text)
    text = re.sub(r'`([^`\n]+)`',           r'\1', text)
    text = re.sub(r'^\s*[-*+]\s+',          '',    text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\|.+',             '',    text, flags=re.MULTILINE)
    # Non-ASCII: vectors, math symbols, emoji, Unicode arrows
    text = text.encode('ascii', errors='ignore').decode('ascii')
    # Keep plain English characters, punctuation, and value symbols ($ % / =)
    text = re.sub(r'[^a-zA-Z0-9 .,!?;:()\'\-\n$%/=]', ' ', text)
    # Collapse whitespace
    text = re.sub(r'\n{2,}', ' ', text)
    text = re.sub(r'\s{2,}',  ' ', text).strip()
    # Keep phrases with >=4 words OR any phrase containing a digit (exact facts
    # like "budget 2.7M" must not be filtered out as heading debris).
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text)
                 if len(s.split()) >= 4 or re.search(r'\d', s)]
    return ' '.join(sentences)


def _call_model(prompt: str) -> str:
    """Call gpt-5-mini (simple tier) via model_router → Local LLM proxy."""
    try:
        from models.model_router import model_router
        return model_router.generate(prompt, model_hint="simple").strip()
    except Exception as e:
        logger.warning(f"chat_summarizer: model call failed: {e}")
        return ""


def _get_raw_history_tokens(chat_id: str) -> int:
    """Count tokens of the last 6 raw messages — cheap check before triggering."""
    try:
        from db.database import SessionLocal
        from db.models import ChatMessage as _CM
        db = SessionLocal()
        try:
            msgs = (
                db.query(_CM.content)
                .filter(_CM.chat_id == chat_id, _CM.role.in_(["user", "assistant"]))
                .order_by(_CM.created_at.desc())
                .limit(6)
                .all()
            )
        finally:
            db.close()
        return sum(_count_tokens(m.content or "") for m in msgs)
    except Exception:
        return 0


def get_chat_summary(chat_id: str) -> str:
    """Read current rolling summary from agent_memory. Returns '' if none."""
    try:
        from db.database import SessionLocal
        from db.models import AgentMemory
        db = SessionLocal()
        try:
            row = db.query(AgentMemory).filter(
                AgentMemory.agent_name == f"chat:{chat_id}",
                AgentMemory.key       == "rolling_summary",
            ).first()
            return row.value if row else ""
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"chat_summarizer: get failed for {chat_id}: {e}")
        return ""


def _save_summary(chat_id: str, summary: str) -> None:
    """Upsert rolling summary into agent_memory."""
    try:
        from db.database import SessionLocal
        from db.models import AgentMemory
        import uuid
        db = SessionLocal()
        try:
            row = db.query(AgentMemory).filter(
                AgentMemory.agent_name == f"chat:{chat_id}",
                AgentMemory.key       == "rolling_summary",
            ).first()
            if row:
                row.value = summary
            else:
                db.add(AgentMemory(
                    id         = str(uuid.uuid4()),
                    agent_name = f"chat:{chat_id}",
                    key        = "rolling_summary",
                    value      = summary,
                    tags       = [],
                ))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"chat_summarizer: save failed for {chat_id}: {e}")


def distill_turn(question: str, answer: str) -> str:
    """
    Produce a compact (≤300 char) plain-English summary of one Q&A turn.
    Used for cross-chat user memory — no code, no JSON, no symbols.
    Returns '' if nothing meaningful survives stripping.
    """
    clean_q = _to_plain_english(question)
    clean_a = _to_plain_english(answer)
    if len(clean_q) > 120: clean_q = clean_q[:120]
    if len(clean_a) > 150: clean_a = clean_a[:150]
    if not clean_q:
        return ""
    summary = f"User asked about {clean_q}."
    if clean_a:
        summary += f" Assistant: {clean_a}"
    return summary[:300].strip()


# ============================================================
# LLM-BASED MEMORY FILTER
# Decides whether a Q&A turn is worth storing in long-term
# cross-chat memory, and produces a clean summary + context hint.
# ============================================================

_MEMORY_FILTER_PROMPT = """\
You are a memory manager for an enterprise AI assistant.

Analyse the following conversation exchange and decide whether it contains \
information worth storing in the user's long-term memory.

STORE if the exchange reveals:
  - A user preference or personal fact (name, lucky number, language, timezone…)
  - A long-term interest or recurring topic
  - A skill, technology stack, or domain expertise
  - An important decision or goal
  - A recurring behaviour or workflow pattern

DO NOT STORE if the exchange is:
  - A greeting, small-talk, or one-liner ("hi", "thanks", "ok")
  - A one-off factual question with no personal relevance
  - Pure code generation with no user-specific context
  - A trivial clarification

Respond with ONLY valid JSON — no markdown, no explanation:
{{
  "should_store_chat_memory": true | false,
  "summary": "<concise plain-English memory, ≤200 chars, empty string if false>",
  "context_hint": "<2-4 word topic label, snake_case, empty string if false>"
}}

Exchange:
User: {question}
Assistant: {answer}
"""


def should_store_memory(question: str, answer: str) -> dict:
    """
    Call the LLM to decide whether this Q&A turn deserves a memory entry.

    Returns a dict with keys:
      should_store_chat_memory : bool
      summary                  : str   (clean distilled memory, ≤200 chars)
      context_hint             : str   (short snake_case topic label)

    Falls back to a heuristic distill_turn() result on any LLM/parse failure
    so the caller always gets a usable dict.
    """
    clean_q = _to_plain_english(question)
    clean_a = _to_plain_english(answer)

    # Fast-reject: nothing meaningful after stripping (pure code / symbol turn)
    if not clean_q:
        return {"should_store_chat_memory": False, "summary": "", "context_hint": ""}

    # Truncate for the LLM prompt — keep it cheap
    if len(clean_q) > 300: clean_q = clean_q[:300]
    if len(clean_a) > 400: clean_a = clean_a[:400]

    prompt = _MEMORY_FILTER_PROMPT.format(question=clean_q, answer=clean_a)

    try:
        raw = _call_model(prompt)
        # Strip any accidental markdown fences
        raw = re.sub(r"^```[a-z]*\s*", "", raw.strip(), flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw.strip())
        data = json.loads(raw)
        should_store = bool(data.get("should_store_chat_memory", False))
        summary      = str(data.get("summary", "")).strip()[:200]
        context_hint = str(data.get("context_hint", "")).strip()[:60]
        # Sanitise context_hint to snake_case
        context_hint = re.sub(r"[^a-z0-9_]", "_", context_hint.lower())
        context_hint = re.sub(r"_+", "_", context_hint).strip("_")
        return {
            "should_store_chat_memory": should_store,
            "summary":                  summary,
            "context_hint":             context_hint,
        }
    except Exception as e:
        logger.debug(f"should_store_memory: LLM/parse failed ({e}), using heuristic fallback")
        # Heuristic fallback — use distill_turn and always store
        fallback_summary = distill_turn(question, answer)
        return {
            "should_store_chat_memory": bool(fallback_summary),
            "summary":                  fallback_summary,
            "context_hint":             "",
        }


def update_chat_summary(chat_id: str, question: str, answer: str) -> None:
    """
    Update rolling summary for a chat after a new turn.
    Only triggers when raw history exceeds _TRIGGER_TOKENS.
    Safe to call from a background thread.
    """
    try:
        # Fast check — skip if history is still small enough to use raw
        if _get_raw_history_tokens(chat_id) < _TRIGGER_TOKENS:
            return

        existing = get_chat_summary(chat_id)
        clean_q  = _to_plain_english(question)
        clean_a  = _to_plain_english(answer)

        # Truncate new exchange for the summariser input
        if len(clean_q) > 400: clean_q = clean_q[:400]
        if len(clean_a) > 600: clean_a = clean_a[:600]

        if not clean_q and not clean_a:
            return  # nothing useful after stripping (pure code turn)

        if existing:
            prompt = (
                "You are a conversation memory assistant for an enterprise AI platform.\n"
                "Update the existing summary by incorporating the new exchange.\n"
                "Rules: concise plain English. No code fences. No bullet points.\n"
                "IMPORTANT: preserve EVERY exact numeric value, identifier, ratio, "
                "version, and date VERBATIM (e.g. 0.7183, v4.18.2, 2026-03-15, 2.76345M). "
                "If the user changed a value over time, list ALL of its values in the "
                "exact order they were given.\n"
                "Focus on: topic discussed, decisions made, systems or features mentioned, "
                "and all exact values.\n\n"
                f"Existing summary:\n{existing}\n\n"
                f"New exchange:\n"
                f"User: {clean_q}\n"
                f"Assistant: {clean_a}\n\n"
                "Updated summary:"
            )
        else:
            prompt = (
                "Summarise this conversation exchange for a memory store.\n"
                "Rules: concise plain English. No code fences. No bullet points.\n"
                "IMPORTANT: preserve EVERY exact numeric value, identifier, ratio, "
                "version, and date VERBATIM (e.g. 0.7183, v4.18.2, 2026-03-15, 2.76345M). "
                "If the user gave a value more than once, list ALL values in the exact "
                "order they were given.\n"
                "Focus on: topic discussed, decisions made, systems or features mentioned, "
                "and all exact values.\n\n"
                f"User: {clean_q}\n"
                f"Assistant: {clean_a}\n\n"
                "Summary:"
            )

        new_summary = _call_model(prompt)

        if not new_summary:
            # Model unavailable — build minimal fallback from clean question
            new_summary = (
                (existing + " " if existing else "")
                + f"User asked about {clean_q[:120]}."
            ).strip()

        if len(new_summary) > _MAX_SUMMARY_CHARS:
            new_summary = new_summary[:_MAX_SUMMARY_CHARS]

        _save_summary(chat_id, new_summary)
        logger.debug(f"chat_summarizer: updated summary for chat={chat_id} ({len(new_summary)} chars)")

    except Exception as e:
        logger.warning(f"chat_summarizer: update failed for {chat_id}: {e}")
