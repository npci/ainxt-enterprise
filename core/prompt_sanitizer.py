# SPDX-License-Identifier: MIT
# ============================================================
# PROMPT SANITIZER  —  core/prompt_sanitizer.py
#
# See docs/PROMPT_SANITIZATION.md for the full specification.
#
# Every string that will be sent to any LLM API (Anthropic, OpenAI,
# Google, Local LLM proxy) MUST pass through sanitize() first.
# All four gateways call it immediately before the API call so no
# code path can bypass it accidentally.
# ============================================================

import re

# ── Whitelist regex ────────────────────────────────────────────────────────────
#
# KEEP everything that is human-readable or structurally meaningful in code:
#
#   \t   (0x09)  — tab            → Python/Go/YAML indentation
#   \n   (0x0A)  — line feed      → universal line ending; code structure
#   \r   (0x0D)  — carriage ret   → normalised to \n below
#   0x20-0x7E    — printable ASCII (space through ~)
#   0xA0-0xD7FF  — printable non-ASCII Unicode (Latin, CJK, Arabic, etc.)
#                  starts at 0xA0 (non-breaking space) intentionally to skip
#                  C1 control chars 0x80-0x9F
#   0xE000-0xFFFD — Private-Use Area + most Specials (excludes BOM 0xFFFE/0xFFFF)
#   0x10000+      — Supplementary planes (emoji, CJK extensions, etc.)
#
# STRIP everything else — invisible, non-printable, or API-hostile:
#   0x00-0x08    — NUL through BS (null bytes, backspace, bell, etc.)
#   0x0B         — VT  (vertical tab)
#   0x0C         — FF  (form feed)
#   0x0E-0x1F    — SO through US (shift-out, escape, etc.)
#   0x7F         — DEL
#   0x80-0x9F    — C1 controls (not covered by 0xA0 start above)
#   0xD800-0xDFFF — Unicode surrogates (invalid in text)
#   0xFFFE-0xFFFF — BOM and non-characters
#
# This is a WHITELIST: anything not explicitly in the allowed set is dropped.
# All legitimate code content (including special symbols in string literals,
# regex patterns, template strings, etc.) falls within the allowed ranges.
# ─────────────────────────────────────────────────────────────────────────────

_STRIP_RE = re.compile(
    r"[^\t\n\r\x20-\x7e\xa0-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)

# Normalise Windows (\r\n) and old-Mac (\r) line endings to Unix (\n)
_CRLF_RE = re.compile(r"\r\n|\r")


def sanitize(text: str) -> str:
    """
    Return a sanitized copy of *text* safe to send to any LLM API.

    Algorithm (two passes, O(n)):
      1. Normalise line endings  (\r\n and \r  →  \n)
      2. Strip all non-whitelisted characters

    Guarantees:
      - All printable content is preserved exactly — no accuracy loss
      - Code indentation, whitespace, and structure survive unchanged
      - Multiple blank lines are never collapsed
      - Non-ASCII text (CJK, Arabic, Devanagari, emoji) is preserved
      - Null bytes, control chars, and surrogates are removed
    """
    if not text:
        return text
    text = _CRLF_RE.sub("\n", text)
    return _STRIP_RE.sub("", text)


def sanitize_messages(messages: list) -> list:
    """
    Sanitize a list of chat-format message dicts {"role": …, "content": …}.
    Returns a new list; originals are not mutated.
    Handles both string content and Anthropic-style multi-part content blocks.
    """
    out = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            out.append({**m, "content": sanitize(content)})
        elif isinstance(content, list):
            new_blocks = []
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    new_blocks.append({**block, "text": sanitize(block["text"])})
                else:
                    new_blocks.append(block)
            out.append({**m, "content": new_blocks})
        else:
            out.append(m)
    return out
