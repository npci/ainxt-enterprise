# SPDX-License-Identifier: MIT
"""Context-assembly strategies under test.

Each strategy is a pure function: (case, model_hint) -> AssembledContext.
No DB, no Redis, no network — the synthetic transcript IS the history.

`current_strategy` faithfully reproduces the production selection logic in
gateway.py (as of the flat-150K era) so the harness measures a REAL baseline:

  - load up to _RAW_TURNS turns (newest first, capped)
  - estimate tokens at len//4
  - if under _TRIGGER_TOKENS  -> send all loaded turns verbatim
  - else                      -> rolling summary + last _SUMMARY_TURNS raw turns

The summary is modelled as a lossy compression: we keep a short distillation of
each old turn (first ~120 chars) concatenated. This mirrors the fact that the
production summarizer preserves gist but drops specific values — which is
exactly the omission the tiers aim to reduce.

`c1_strategy` adds Tier 2 (model-aware usable budget) + Tier 5 (output
reservation): the trigger becomes window(model)*USABLE_FRACTION - reserved.
Still NO retrieval (that is C3, flagged off/deferred).
"""
from __future__ import annotations

from typing import Optional

from .model import AssembledContext, Case, Turn

# ── Mirror of production constants (gateway.py) ─────────────────────────────
_CHARS_PER_TOKEN = 4
_TRIGGER_TOKENS = 150_000
_RAW_TURNS = 200
_SUMMARY_TURNS = 20

# ── C1 constants (Tier 2 + Tier 5) ──────────────────────────────────────────
USABLE_FRACTION = 0.75
# Per-model output reservation (Tier 5). Conservative defaults.
_RESERVED_OUTPUT = {
    "claude": 8_000, "sonnet": 8_000, "opus": 8_000, "haiku": 4_000,
    "gpt-5": 16_000, "gpt-4": 4_000, "gpt": 4_000,
    "gemini": 8_000, "kimi": 4_000, "local": 2_000,
}
_MODEL_CONTEXT_WINDOW = {
    "claude": 200_000, "sonnet": 200_000, "opus": 200_000, "haiku": 200_000,
    "gpt-5": 256_000, "gpt-4": 128_000, "gpt": 128_000,
    "gemini": 1_000_000, "kimi": 128_000, "local": 128_000,
}


def _count_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _context_window_for(model_hint: Optional[str]) -> int:
    m = (model_hint or "").lower().strip()
    if m:
        for key, win in _MODEL_CONTEXT_WINDOW.items():
            if key in m:
                return win
    return 128_000


def _reserved_output_for(model_hint: Optional[str]) -> int:
    m = (model_hint or "").lower().strip()
    if m:
        for key, res in _RESERVED_OUTPUT.items():
            if key in m:
                return res
    return 2_000


def _distill(content: str) -> str:
    """Lossy per-turn distillation used to model the rolling summary.

    Keeps the opening gist and drops the tail — the realistic failure mode is
    that a specific value stated late in a turn is lost from the summary.
    """
    text = " ".join(content.split())
    return text[:120]


def _relevant_turns(case: Case) -> list[Turn]:
    """Apply the current rag_mode isolation filter, matching production."""
    if case.rag_mode == "off":
        return [t for t in case.turns if t.rag_mode == "off"]
    return list(case.turns)


def current_strategy(case: Case, model_hint: Optional[str] = None) -> AssembledContext:
    """Baseline: flat 150K trigger, raw-below / summary+recent-above."""
    turns = _relevant_turns(case)[-_RAW_TURNS:]
    raw_tokens = sum(_count_tokens(t.content) for t in turns)

    if raw_tokens < _TRIGGER_TOKENS:
        msgs = [{"role": t.role, "content": t.content} for t in turns]
        return AssembledContext(messages=msgs, summary_used=False,
                                approx_tokens=raw_tokens)

    # Over threshold: rolling summary of the OLD turns + last N raw turns.
    recent = turns[-_SUMMARY_TURNS:]
    older = turns[:-_SUMMARY_TURNS]
    summary_text = " | ".join(_distill(t.content) for t in older)
    msgs = [
        {"role": "user", "content": "[Previous conversation summary]"},
        {"role": "assistant", "content": summary_text},
    ]
    msgs += [{"role": t.role, "content": t.content} for t in recent]
    approx = sum(_count_tokens(m["content"]) for m in msgs)
    return AssembledContext(messages=msgs, summary_used=True, approx_tokens=approx)


def c1_strategy(case: Case, model_hint: Optional[str] = None) -> AssembledContext:
    """C1: model-aware usable budget (Tier 2) + output reservation (Tier 5).

    Same raw-vs-summary mechanism as baseline, but the trigger is derived from
    the actual model window instead of a flat 150K, and the input budget is
    reduced by the reserved output tokens. NO retrieval.
    """
    window = _context_window_for(model_hint)
    reserved = _reserved_output_for(model_hint)
    model_usable = int(window * USABLE_FRACTION) - reserved
    # Safety floor (ship-gate lesson from C0): the model-aware budget must NEVER
    # trigger compaction *earlier* than today's flat behavior, or small-window
    # models regress (they compact sooner into a lossy summary). Tier 2 may only
    # RAISE the ceiling for large-window models, never lower it below baseline.
    trigger = max(_TRIGGER_TOKENS, model_usable)

    turns = _relevant_turns(case)[-_RAW_TURNS:]
    raw_tokens = sum(_count_tokens(t.content) for t in turns)

    if raw_tokens < trigger:
        msgs = [{"role": t.role, "content": t.content} for t in turns]
        return AssembledContext(messages=msgs, summary_used=False,
                                approx_tokens=raw_tokens)

    recent = turns[-_SUMMARY_TURNS:]
    older = turns[:-_SUMMARY_TURNS]
    summary_text = " | ".join(_distill(t.content) for t in older)
    msgs = [
        {"role": "user", "content": "[Previous conversation summary]"},
        {"role": "assistant", "content": summary_text},
    ]
    msgs += [{"role": t.role, "content": t.content} for t in recent]
    approx = sum(_count_tokens(m["content"]) for m in msgs)
    return AssembledContext(messages=msgs, summary_used=True, approx_tokens=approx)


# Registry so the runner can iterate strategies by name.
STRATEGIES = {
    "current": current_strategy,
    "c1": c1_strategy,
}
