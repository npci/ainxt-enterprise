# SPDX-License-Identifier: MIT
# ============================================================
# STRUCTURED FACT STORE — verbatim exact-value memory per chat
#
# Problem this solves:
#   The rolling summary (chat_summarizer) and the history cleaner
#   (_clean_for_history) both strip JSON / YAML / CSV / tables and
#   collapse exact numbers into prose. After summarisation kicks in
#   on long chats, the model can no longer recall exact values it was
#   given ("what is the 'version' in the JSON?", "what is the
#   conversion ratio?"). It answers "I don't have that content".
#
# This module extracts structured payloads and exact key:value facts
# from the USER's message and stores them VERBATIM, so they can be
# re-injected into context regardless of summarisation. It also keeps
# an ORDERED history of values per field (for contradiction tracking:
# "list every budget value in the order I gave them").
#
# Storage reuses the existing AgentMemory table via
# store.episodic_memory — no new table, no migration.
#   agent_name = "chat:{chat_id}"
#   key        = "fact:{slug}"            → verbatim payload/value
#   key        = "value_history:{field}"  → JSON array, chronological
# ============================================================

from __future__ import annotations

import json
import re
from typing import List

from core.logger import logger

# Reuse the existing keyed/tagged upsert store.
from store import episodic_memory as _em

_MAX_FACTS_IN_BLOCK = 40
_MAX_VALUE_LEN      = 2000   # per stored fact
_MAX_BLOCK_CHARS    = 2400   # injected block budget
_MAX_HISTORY_LEN    = 30     # values kept per field-history

# ── Regexes for structured payloads in the user's message ───────────────────

# Fenced code / data blocks:  ```json … ```  ```yaml … ```  ```csv … ```
_FENCED_RE = re.compile(r"```(\w*)\n?([\s\S]*?)```", re.MULTILINE)

# key: value  /  key = value  numeric-or-short-scalar facts on their own line.
# Captures things like:  replicas: 7 | version: v4.18.2 | port = 5439 |
# conversion ratio: 0.7183 | budget: $2.76345M | deadline: 2026-03-15
_KV_RE = re.compile(
    r"^[ \t>*\-]*"
    r"([A-Za-z][A-Za-z0-9 _.\-]{0,48}?)"          # field name
    r"\s*[:=]\s*"
    r"([^\n]{1,80}?)\s*$",
    re.MULTILINE,
)

# A "field" worth tracking chronologically (contradiction scenario).
# We track any scalar field that appears repeatedly with different values.
# The canonical keyword is used so "budget", "actually budget" and
# "final budget" all accumulate into ONE ordered history.
_TRACK_FIELDS = ("conversion ratio", "budget", "forecast", "deadline", "region",
                 "ratio", "price", "cost", "target", "value")

# Lines that are really block preambles ("here is the config:", "db config:")
# — the value is an empty/fence token, not a real fact. Reject them.
_FENCE_TOKEN_RE = re.compile(r"^`{1,3}\w*$|^$")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:60] or "fact"


def _canonical_field(field: str) -> str:
    """Return the tracked keyword contained in `field`, or '' if none.

    Ensures 'budget', 'actually budget', 'final budget' → 'budget' so their
    values accumulate into a single chronological history.
    """
    fl = field.lower()
    for t in _TRACK_FIELDS:
        if t in fl:
            return t
    return ""


def _first_number_token(value: str) -> str:
    """A short normalised token for de-dup / display."""
    return value.strip()


# ── EXTRACT + STORE ─────────────────────────────────────────────────────────

def extract_and_store(chat_id: str, question: str, answer: str) -> int:
    """
    Extract structured payloads + exact key:value facts from the USER message
    (`question`) and persist them verbatim. Returns the number of facts stored.

    Safe to call from a background thread; never raises.
    """
    if not chat_id or not question:
        return 0

    agent = f"chat:{chat_id}"
    stored = 0

    try:
        # 1. Fenced structured blocks (json/yaml/csv/toml/table) — store verbatim.
        for m in _FENCED_RE.finditer(question):
            lang = (m.group(1) or "data").strip().lower() or "data"
            body = (m.group(2) or "").strip()
            if not body:
                continue
            if lang not in ("json", "yaml", "yml", "csv", "toml", "table", "data", "sql"):
                # only structured/data blocks — skip prose code we don't need verbatim
                if not re.search(r"[:=,{}\[\]|]", body):
                    continue
            value = body[:_MAX_VALUE_LEN]
            key = f"fact:block_{lang}_{_slug(body[:24])}"
            _em.remember(agent, key, value, tags=["structured", "block", lang])
            stored += 1

        # 2. key: value scalar facts. Run over the message with fenced blocks
        #    removed so we don't re-capture block preambles / interior lines.
        scalar_src = _FENCED_RE.sub(" ", question)
        for m in _KV_RE.finditer(scalar_src):
            field = m.group(1).strip()
            val   = m.group(2).strip()
            # Skip empty / fence-token / over-long values and pure prose.
            if not val or _FENCE_TOKEN_RE.match(val) or len(val) > 80:
                continue
            # Require the value to contain a digit or be short & token-like
            if not re.search(r"\d", val) and len(val.split()) > 3:
                continue
            canonical = _canonical_field(field)
            # Store under the canonical field name when it is a tracked field,
            # else under the raw field slug.
            slug = _slug(canonical or field)
            _em.remember(agent, f"fact:{slug}", val[:_MAX_VALUE_LEN],
                         tags=["structured", "kv"])
            stored += 1

            # 3. Ordered value-history for trackable fields (contradiction).
            if canonical:
                _append_history(agent, _slug(canonical), val)

    except Exception as e:
        logger.warning(f"structured_facts: extract_and_store failed for {chat_id}: {e}")

    if stored:
        logger.debug(f"structured_facts: stored {stored} fact(s) for chat={chat_id}")
    return stored


def _append_history(agent: str, field_slug: str, value: str) -> None:
    """Append value to the chronological history JSON array for a field."""
    key = f"value_history:{field_slug}"
    try:
        raw = _em.recall(agent, key)
        arr: List[str] = json.loads(raw) if raw else []
        if not isinstance(arr, list):
            arr = []
    except Exception:
        arr = []
    arr.append(_first_number_token(value))
    if len(arr) > _MAX_HISTORY_LEN:
        arr = arr[-_MAX_HISTORY_LEN:]
    try:
        _em.remember(agent, key, json.dumps(arr), tags=["structured", "history"])
    except Exception as e:
        logger.debug(f"structured_facts: history append failed ({e})")


# ── READ / INJECT ───────────────────────────────────────────────────────────

def get_facts_block(chat_id: str) -> str:
    """
    Build a compact plain-text block of the stored exact values for this chat,
    ready to inject as a synthetic context message. Returns '' if none.
    """
    if not chat_id:
        return ""
    agent = f"chat:{chat_id}"
    try:
        allrecs = _em.recall_all(agent)
    except Exception as e:
        logger.debug(f"structured_facts: recall_all failed ({e})")
        return ""

    if not allrecs:
        return ""

    lines: List[str] = []

    # Chronological value histories first (most relevant for contradiction Qs).
    for key, val in allrecs.items():
        if not key.startswith("value_history:"):
            continue
        field = key.split(":", 1)[1].replace("_", " ")
        try:
            arr = json.loads(val)
            if isinstance(arr, list) and arr:
                lines.append(f"- {field} (in chronological order): "
                             + " -> ".join(str(v) for v in arr))
        except Exception:
            continue

    # Then verbatim facts / blocks.
    count = 0
    for key, val in allrecs.items():
        if not key.startswith("fact:"):
            continue
        if count >= _MAX_FACTS_IN_BLOCK:
            break
        label = key.split(":", 1)[1]
        snippet = (val or "").strip()
        if not snippet:
            continue
        if label.startswith("block_"):
            lines.append(f"- (stored data block)\n{snippet}")
        else:
            lines.append(f"- {label.replace('_', ' ')}: {snippet}")
        count += 1

    if not lines:
        return ""

    block = ("[Stored exact values from earlier in this conversation - use these "
             "verbatim when asked; do not say you don't have them]\n"
             + "\n".join(lines))
    if len(block) > _MAX_BLOCK_CHARS:
        block = block[:_MAX_BLOCK_CHARS]
    return block
