# SPDX-License-Identifier: MIT
"""Model-alias helpers shared by the agent and workflow factory patchers.

Extracted from ``app/api/factories.py`` so the workflow patcher can reuse the
same intent-detection, token-extraction, catalogue-alias-resolution, and
rejection-message logic without duplicating rules that already ship in the
agent factory. Both patchers must reject invalid model ids identically —
splitting the helpers apart would drift.
"""

from __future__ import annotations

import re


_MODEL_INTENT_RE = re.compile(
    r"\b(?:model|llm|switch\s+to|use\s+model|change\s+to)\b",
    re.IGNORECASE,
)

_MODEL_TOKEN_RE = re.compile(
    r"(?:model|llm|switch\s+to|use)\s+(?:to\s+)?[`\"']?"
    r"([A-Za-z0-9][A-Za-z0-9._\-\/]{1,60})[`\"']?",
    re.IGNORECASE,
)


def message_asks_for_model_change(message: str) -> bool:
    """Heuristic — did the user's chat message mention changing the model?

    Used only to trigger validation when the LLM patcher returns nothing at
    all despite the user asking about the model. Kept intentionally loose:
    false positives merely trigger a catalogue check that returns "no valid
    model" and prompts the user to try a real id.
    """
    return bool(_MODEL_INTENT_RE.search(message or ""))


def extract_model_token(message: str) -> str:
    """Extract the model name the user tried to set from the raw message.

    Returns the first plausible token after a trigger phrase like "model to",
    "switch to", "use model". Falls back to an empty string when nothing
    matches so the caller can decide how to phrase the rejection.
    """
    m = _MODEL_TOKEN_RE.search(message or "")
    if not m:
        return ""
    token = m.group(1).strip()
    return token.rstrip(".,;:!?")


def _strip_provider(mid: str) -> str:
    """Return the bare model id (drops any ``provider/`` prefix)."""
    return mid.split("/", 1)[-1] if "/" in mid else mid


def resolve_model_alias(requested: str, allowed_ids: list) -> str:
    """Resolve a user / LLM-supplied model id to a real catalogue entry.

    Returns the canonical id if the request matches an allowed model
    (exact, case-insensitive, or substring after stripping any
    ``provider/`` prefix), otherwise an empty string.

    Multi-hit substring matches are disambiguated by picking the shortest
    id — this maps "sonnet" onto the newest / canonical entry (e.g.
    "claude-sonnet-4-6", not the older "-5" variant) and matches the
    ranking the CLI's ``/model`` picker uses.
    """
    req = (requested or "").strip()
    if not req or not allowed_ids:
        return ""

    req_norm = _strip_provider(req).lower()
    norm_to_id = {_strip_provider(mid).lower(): mid for mid in allowed_ids}

    # 1. Exact (case-insensitive) match against the bare id.
    if req_norm in norm_to_id:
        return norm_to_id[req_norm]

    # 2. Exact match against the full id including provider prefix.
    lower_full = {mid.lower(): mid for mid in allowed_ids}
    if req.lower() in lower_full:
        return lower_full[req.lower()]

    # 3. Substring match (e.g. "sonnet" → "claude-sonnet-4-6"), but only
    # when the request is at least 3 chars — shorter tokens like "gpt"
    # would match too many ids to be useful. When multiple ids match, prefer
    # the shortest one.
    if len(req_norm) >= 3:
        hits = [mid for norm, mid in norm_to_id.items() if req_norm in norm]
        if hits:
            hits.sort(key=lambda mid: (len(_strip_provider(mid)), mid))
            return hits[0]

    return ""


def build_model_rejection_message(requested: str, allowed_ids: list) -> str:
    """Compose a chat-friendly rejection message for an unknown model id.

    Lists a handful of the user's actual available models so they can pick
    a valid one on the next turn without having to open the manual editor.
    """
    req_display = (requested or "").strip() or "(empty)"
    if not allowed_ids:
        return (
            f"I couldn't find a model named `{req_display}` in your catalogue, "
            "and I couldn't reach the model service to suggest alternatives. "
            "Please pick a model directly from the dropdown on the right."
        )
    _AUTO = "ainxt-auto-route"
    picks = [mid for mid in allowed_ids if mid != _AUTO][:8]
    picks_str = ", ".join(f"`{p}`" for p in picks)
    return (
        f"There's no model named `{req_display}` in your catalogue. "
        f"Try one of: {picks_str}. "
        "You can also pick from the model dropdown on the right."
    )
