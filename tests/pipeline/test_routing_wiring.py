# SPDX-License-Identifier: MIT
# ============================================================
# Phase 3 — router tier wiring equivalence (pure mirror)
# ============================================================
#
# Mirrors the gateway.py _fp_hint logic (line ~4774) to prove:
#   - flag OFF                     → "medium" (today's auto default)
#   - forced model (hint set)      → the forced hint (never overridden)
#   - voice                        → "complex" (unchanged)
#   - flag ON + auto + CIL tier    → the CIL task_complexity tier (real change)
# The vocabulary is shared with model_router._HINT_MAP.
# ============================================================

from cil.state import ConversationState

_TIER_HINTS = {"simple", "medium", "complex", "deep", "solution"}


def _fp_hint(*, is_voice, model_hint, flag_on, conv_state):
    """Mirror of gateway.py _fp_hint computation incl. the PIPELINE_V2_ROUTING hook."""
    hint = "complex" if is_voice else (model_hint or "medium")
    if flag_on and not is_voice and not model_hint and conv_state is not None:
        tier = getattr(conv_state, "task_complexity", None)
        if tier in _TIER_HINTS:
            hint = tier
    return hint


def _cs(complexity):
    st = ConversationState()
    st.task_complexity = complexity
    return st


# ── flag OFF → today's behavior ─────────────────────────────────────────────

def test_off_auto_is_medium():
    assert _fp_hint(is_voice=False, model_hint=None, flag_on=False, conv_state=_cs("complex")) == "medium"


def test_off_forced_model_kept():
    assert _fp_hint(is_voice=False, model_hint="haiku", flag_on=False, conv_state=None) == "haiku"


def test_off_voice_is_complex():
    assert _fp_hint(is_voice=True, model_hint=None, flag_on=False, conv_state=None) == "complex"


# ── flag ON → real change, but only for auto turns ──────────────────────────

def test_on_auto_follows_cil_complexity():
    assert _fp_hint(is_voice=False, model_hint=None, flag_on=True, conv_state=_cs("simple")) == "simple"
    assert _fp_hint(is_voice=False, model_hint=None, flag_on=True, conv_state=_cs("complex")) == "complex"
    assert _fp_hint(is_voice=False, model_hint=None, flag_on=True, conv_state=_cs("deep")) == "deep"


def test_on_forced_model_never_overridden():
    # user explicitly picked a model → CIL never overrides it
    assert _fp_hint(is_voice=False, model_hint="opus-4-6", flag_on=True, conv_state=_cs("simple")) == "opus-4-6"


def test_on_voice_unchanged():
    assert _fp_hint(is_voice=True, model_hint=None, flag_on=True, conv_state=_cs("simple")) == "complex"


def test_on_no_conv_state_is_medium():
    assert _fp_hint(is_voice=False, model_hint=None, flag_on=True, conv_state=None) == "medium"


def test_on_invalid_tier_falls_back_to_medium():
    assert _fp_hint(is_voice=False, model_hint=None, flag_on=True, conv_state=_cs("bogus")) == "medium"
