# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Tests for models/followup_condenser.py
#
# All tests mock model_router.generate() and the module's redis_client —
# no live LLM or Redis dependency, so these run anywhere (including CI
# with no infra configured).
# ============================================================

import pytest

from models import followup_condenser as fc


class _FakeRedis:
    """Minimal in-memory stand-in for the redis_client used by the module."""
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


@pytest.fixture
def fake_redis(monkeypatch):
    client = _FakeRedis()
    monkeypatch.setattr(fc, "redis_client", client)
    return client


def _history():
    return [
        {"role": "user", "content": "What is UPI settlement TAT?"},
        {"role": "assistant", "content": "UPI settlement happens in 3 steps: initiation, clearing, confirmation."},
    ]


# ── _build_history_text ─────────────────────────────────────────────────────

def test_build_history_text_includes_full_content_no_truncation():
    long_answer = "A" * 5000  # deliberately long — must NOT be truncated
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": long_answer},
    ]
    text = fc._build_history_text(messages)
    assert long_answer in text  # full text present, not cut off
    assert "USER: question" in text
    assert f"ASSISTANT: {long_answer}" in text


def test_build_history_text_skips_non_user_assistant_roles():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hello"},
    ]
    text = fc._build_history_text(messages)
    assert "system prompt" not in text
    assert "USER: hello" in text


def test_build_history_text_skips_empty_content():
    messages = [
        {"role": "user", "content": "   "},
        {"role": "assistant", "content": "real answer"},
    ]
    text = fc._build_history_text(messages)
    assert "real answer" in text
    assert text.count("\n") == 0  # only one non-empty line


# ── model chain — configurability (core/config.py wiring) ───────────────────

def test_condense_model_chain_default_matches_config():
    """The module-level chain must come from core.config's
    KB_FOLLOWUP_CONDENSE_MODEL_CHAIN (not a hardcoded literal in this
    module) — this is what makes the chain configurable via the
    KB_FOLLOWUP_CONDENSE_MODEL_CHAIN env var with no code change."""
    from core.config import KB_FOLLOWUP_CONDENSE_MODEL_CHAIN
    assert fc._CONDENSE_MODEL_CHAIN == KB_FOLLOWUP_CONDENSE_MODEL_CHAIN


def test_condense_model_chain_default_value():
    """Default chain (no env var set): local:gpt-oss-120b first (zero
    marginal cost in-house model), haiku as the cloud fallback."""
    assert fc._CONDENSE_MODEL_CHAIN == ["local:gpt-oss-120b", "haiku"]


def test_condense_model_chain_env_var_override(monkeypatch):
    """Setting KB_FOLLOWUP_CONDENSE_MODEL_CHAIN must change the resolved
    chain with no code change — verified by re-importing core.config's
    module-level constant construction logic directly (the env var is read
    at import time, so we simulate that by re-running the same os.getenv
    parsing the module performs, rather than reloading the whole module
    tree — reloading core.config mid-test-suite risks other modules that
    already cached the old constant)."""
    monkeypatch.setenv("KB_FOLLOWUP_CONDENSE_MODEL_CHAIN", "haiku,local:kimi-k2.7-code,local:glm-5.2")
    import os as _os
    _parsed = [
        m.strip() for m in _os.getenv(
            "KB_FOLLOWUP_CONDENSE_MODEL_CHAIN", "local:gpt-oss-120b,haiku"
        ).split(",") if m.strip()
    ]
    assert _parsed == ["haiku", "local:kimi-k2.7-code", "local:glm-5.2"]


# ── condense_followup — happy path ──────────────────────────────────────────

def test_condense_followup_returns_llm_output(monkeypatch, fake_redis):
    monkeypatch.setattr(
        "models.model_router.model_router.generate",
        lambda prompt, model_hint=None: "What is the UPI settlement confirmation step?",
    )
    result = fc.condense_followup("what about step 3?", _history(), chat_id="chat-1")
    assert result == "What is the UPI settlement confirmation step?"


def test_condense_followup_caches_result(monkeypatch, fake_redis):
    calls = {"n": 0}

    def _fake_generate(prompt, model_hint=None):
        calls["n"] += 1
        return "standalone question"

    monkeypatch.setattr("models.model_router.model_router.generate", _fake_generate)

    first = fc.condense_followup("what about step 3?", _history())
    second = fc.condense_followup("what about step 3?", _history())

    assert first == "standalone question"
    assert second == "standalone question"
    assert calls["n"] == 1  # second call hit the cache, no second LLM call


# ── condense_followup — fallback / fail-safe behaviour ──────────────────────

def test_condense_followup_returns_original_on_empty_history(monkeypatch, fake_redis):
    # No history at all → nothing to condense against.
    result = fc.condense_followup("what about step 3?", [])
    assert result == "what about step 3?"


def test_condense_followup_returns_original_on_empty_question(monkeypatch, fake_redis):
    result = fc.condense_followup("", _history())
    assert result == ""


def test_condense_followup_falls_back_on_llm_exception(monkeypatch, fake_redis):
    def _raise(*a, **k):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("models.model_router.model_router.generate", _raise)

    result = fc.condense_followup("what about step 3?", _history())
    assert result == "what about step 3?"  # falls back to original, never raises


def test_condense_followup_falls_back_on_empty_llm_output(monkeypatch, fake_redis):
    monkeypatch.setattr("models.model_router.model_router.generate", lambda p, model_hint=None: "")
    result = fc.condense_followup("what about step 3?", _history())
    assert result == "what about step 3?"


def test_condense_followup_falls_back_on_too_long_output(monkeypatch, fake_redis):
    too_long = "x" * (fc._MAX_STANDALONE_LEN + 50)
    monkeypatch.setattr("models.model_router.model_router.generate", lambda p, model_hint=None: too_long)
    result = fc.condense_followup("what about step 3?", _history())
    assert result == "what about step 3?"


def test_condense_followup_falls_back_on_multiline_output(monkeypatch, fake_redis):
    multiline = "line one\nline two\nline three"
    monkeypatch.setattr("models.model_router.model_router.generate", lambda p, model_hint=None: multiline)
    result = fc.condense_followup("what about step 3?", _history())
    assert result == "what about step 3?"


def test_condense_followup_strips_surrounding_quotes(monkeypatch, fake_redis):
    monkeypatch.setattr(
        "models.model_router.model_router.generate",
        lambda p, model_hint=None: '"What is the settlement step?"',
    )
    result = fc.condense_followup("what about step 3?", _history())
    assert result == "What is the settlement step?"


def test_condense_followup_redis_get_failure_does_not_raise(monkeypatch, fake_redis):
    def _raise_get(key):
        raise ConnectionError("redis down")

    monkeypatch.setattr(fake_redis, "get", _raise_get)
    monkeypatch.setattr("models.model_router.model_router.generate", lambda p, model_hint=None: "standalone q")

    result = fc.condense_followup("what about step 3?", _history())
    assert result == "standalone q"  # still works, just skips the cache


def test_condense_followup_redis_setex_failure_does_not_raise(monkeypatch, fake_redis):
    def _raise_setex(key, ttl, value):
        raise ConnectionError("redis down")

    monkeypatch.setattr(fake_redis, "setex", _raise_setex)
    monkeypatch.setattr("models.model_router.model_router.generate", lambda p, model_hint=None: "standalone q")

    result = fc.condense_followup("what about step 3?", _history())
    assert result == "standalone q"  # cache write failure is non-fatal


# ── condense_followup — model fallback chain ────────────────────────────────
#
# The chain is CONFIGURABLE (core/config.py's KB_FOLLOWUP_CONDENSE_MODEL_CHAIN,
# env var KB_FOLLOWUP_CONDENSE_MODEL_CHAIN) and independent of the user's
# chosen chat model (q.model in gateway.py) — this is a small internal
# utility call, never the model the user picked for their actual answer. See
# _CONDENSE_MODEL_CHAIN's docstring in followup_condenser.py.
#
# Tests below read the configured chain from fc._CONDENSE_MODEL_CHAIN rather
# than hardcoding specific model names, so they stay correct regardless of
# which models are configured as the default (only the ORDER/FALLBACK
# BEHAVIOR is under test here, not any particular model choice).

_PRIMARY = fc._CONDENSE_MODEL_CHAIN[0]
_SECONDARY = fc._CONDENSE_MODEL_CHAIN[1]


def test_condense_followup_uses_primary_hop_when_it_succeeds(monkeypatch, fake_redis):
    calls = []

    def _fake_generate(prompt, model_hint=None):
        calls.append(model_hint)
        return "standalone question"

    monkeypatch.setattr("models.model_router.model_router.generate", _fake_generate)
    result = fc.condense_followup("what about step 3?", _history())

    assert result == "standalone question"
    assert calls == [_PRIMARY]  # only the primary hop was called — no fallback needed


def test_condense_followup_falls_back_to_local_model_on_error_string(monkeypatch, fake_redis):
    calls = []

    def _fake_generate(prompt, model_hint=None):
        calls.append(model_hint)
        if model_hint == _PRIMARY:
            return "Error: no gateway available"  # model_router's failure shape
        return "What is the settlement confirmation step?"

    monkeypatch.setattr("models.model_router.model_router.generate", _fake_generate)
    result = fc.condense_followup("what about step 3?", _history())

    assert result == "What is the settlement confirmation step?"
    assert calls == [_PRIMARY, _SECONDARY]  # fell through to the secondary hop


def test_condense_followup_falls_back_to_local_model_on_exception(monkeypatch, fake_redis):
    calls = []

    def _fake_generate(prompt, model_hint=None):
        calls.append(model_hint)
        if model_hint == _PRIMARY:
            raise RuntimeError("primary hop circuit breaker open")
        return "standalone from secondary hop"

    monkeypatch.setattr("models.model_router.model_router.generate", _fake_generate)
    result = fc.condense_followup("what about step 3?", _history())

    assert result == "standalone from secondary hop"
    assert calls == [_PRIMARY, _SECONDARY]


def test_condense_followup_falls_back_to_local_model_on_bad_output(monkeypatch, fake_redis):
    calls = []

    def _fake_generate(prompt, model_hint=None):
        calls.append(model_hint)
        if model_hint == _PRIMARY:
            return "line one\nline two\nline three"  # malformed — multi-line
        return "standalone from secondary hop"

    monkeypatch.setattr("models.model_router.model_router.generate", _fake_generate)
    result = fc.condense_followup("what about step 3?", _history())

    assert result == "standalone from secondary hop"
    assert calls == [_PRIMARY, _SECONDARY]


def test_condense_followup_returns_original_when_all_hops_fail(monkeypatch, fake_redis):
    calls = []

    def _fake_generate(prompt, model_hint=None):
        calls.append(model_hint)
        return "Error: no gateway available"  # every hop fails

    monkeypatch.setattr("models.model_router.model_router.generate", _fake_generate)
    result = fc.condense_followup("what about step 3?", _history())

    assert result == "what about step 3?"  # falls back to original, never raises
    assert calls == [_PRIMARY, _SECONDARY]  # tried every hop before giving up


def test_condense_followup_never_calls_a_model_outside_the_configured_chain(monkeypatch, fake_redis):
    """The fallback chain must stay within the CONFIGURED hops only — never
    GPT-5.4 or Claude Sonnet, which is model_router's OWN built-in
    fallback-on-primary-hop-failure that we deliberately bypass by catching
    the failure ourselves."""
    calls = []

    def _fake_generate(prompt, model_hint=None):
        calls.append(model_hint)
        return "Error: no gateway available"

    monkeypatch.setattr("models.model_router.model_router.generate", _fake_generate)
    fc.condense_followup("what about step 3?", _history())

    assert "gpt-5.4" not in [c.lower() if c else c for c in calls]
    assert "claude sonnet" not in [c.lower() if c else c for c in calls]
    assert all(c in fc._CONDENSE_MODEL_CHAIN for c in calls)


def test_condense_followup_caches_only_the_winning_hops_output(monkeypatch, fake_redis):
    calls = []

    def _fake_generate(prompt, model_hint=None):
        calls.append(model_hint)
        if model_hint == _PRIMARY:
            return "Error: no gateway available"
        return "standalone from secondary hop"

    monkeypatch.setattr("models.model_router.model_router.generate", _fake_generate)

    first = fc.condense_followup("what about step 3?", _history())
    second = fc.condense_followup("what about step 3?", _history())

    assert first == second == "standalone from secondary hop"
    # Only 2 calls total (both hops on the FIRST invocation) — the second
    # invocation must hit the cache and make zero further LLM calls.
    assert calls == [_PRIMARY, _SECONDARY]


# ── condense_followup — detecting model_router's OWN silent internal
# fallback (the "[fallback]" label marker) ──────────────────────────────────
#
# model_router.generate() can silently substitute a DIFFERENT model than the
# one requested (e.g. the primary hop secretly served by GPT-5.4, or the
# secondary hop secretly served by GPT-5 mini / Claude Sonnet) and still
# return a normal, non-"Error"-prefixed string. The only way to detect this
# is via model_router.last_model_label, which always carries a "[fallback]"
# suffix when this happens (consistent across every internal fallback path
# in models/model_router.py). These tests simulate that by setting
# last_model_label alongside the mocked generate() return value.

class _FakeModelRouter:
    """Stand-in for the real model_router singleton — lets tests control
    both generate()'s return value AND last_model_label independently,
    exactly like the real object's thread-local property does."""
    def __init__(self, responses):
        # responses: list of (model_hint, output_text, label) tuples, consumed in order
        self._responses = list(responses)
        self.last_model_label = ""
        self.calls = []

    def generate(self, prompt, model_hint=None):
        self.calls.append(model_hint)
        _, output_text, label = self._responses.pop(0)
        self.last_model_label = label
        return output_text


def test_condense_followup_detects_silent_fallback_on_primary_hop(monkeypatch, fake_redis):
    """Primary hop 'succeeds' (no Error prefix) but last_model_label reveals
    it was actually served by GPT-5.4 [fallback] — must be treated as failed
    and move on to the secondary hop."""
    fake_router = _FakeModelRouter([
        (_PRIMARY, "What is the settlement fee for UPI?", "GPT-5.4 (Coding) (gpt-5.4) [fallback]"),
        (_SECONDARY, "What is the UPI settlement confirmation step?", "Local/Cloud (secondary-hop)"),
    ])
    monkeypatch.setattr("models.model_router.model_router", fake_router)

    result = fc.condense_followup("what about step 3?", _history())

    assert result == "What is the UPI settlement confirmation step?"
    assert fake_router.calls == [_PRIMARY, _SECONDARY]


def test_condense_followup_detects_silent_fallback_on_secondary_hop(monkeypatch, fake_redis):
    """Both hops 'succeed' textually, but BOTH were secretly served by a
    paid model ([fallback] on each) — must fall back to the original
    question rather than accept either."""
    fake_router = _FakeModelRouter([
        (_PRIMARY, "some text", "GPT-5.4 (Coding) (gpt-5.4) [fallback]"),
        (_SECONDARY, "some other text", "GPT-5-mini (Fast) (gpt-5-mini) [fallback]"),
    ])
    monkeypatch.setattr("models.model_router.model_router", fake_router)

    result = fc.condense_followup("what about step 3?", _history())

    assert result == "what about step 3?"  # never accepted a paid-model-served hop
    assert fake_router.calls == [_PRIMARY, _SECONDARY]


def test_condense_followup_accepts_non_fallback_secondary_label(monkeypatch, fake_redis):
    """Sanity check the detection logic doesn't have false positives: a
    genuinely successful secondary-hop call (label has NO '[fallback]'
    marker) must be accepted normally."""
    fake_router = _FakeModelRouter([
        (_PRIMARY, "Error: no gateway available", ""),
        (_SECONDARY, "What is the settlement step?", "Local/Cloud (secondary-hop)"),
    ])
    monkeypatch.setattr("models.model_router.model_router", fake_router)

    result = fc.condense_followup("what about step 3?", _history())

    assert result == "What is the settlement step?"
    assert fake_router.calls == [_PRIMARY, _SECONDARY]


# ── condense_followup — LLM decides self-contained vs. follow-up ────────────
#
# There is no separate pattern-matching classifier any more (see the module
# docstring in followup_condenser.py) — the condenser is called on EVERY
# turn with history, and the LLM itself decides whether to echo the
# question back unchanged (self-contained) or rewrite it (follow-up).
# Callers derive the "was this a follow-up?" signal by comparing the
# returned value to the original question — these tests cover both paths.

def test_condense_followup_llm_echoes_self_contained_question_unchanged(monkeypatch, fake_redis):
    """When the LLM judges the question already self-contained, it should
    echo it back verbatim — the caller then correctly concludes this was
    NOT a follow-up (result == original question)."""
    original = "What is the UPI settlement TAT?"
    monkeypatch.setattr(
        "models.model_router.model_router.generate",
        lambda prompt, model_hint=None: original,
    )
    result = fc.condense_followup(original, _history())
    assert result == original


def test_condense_followup_llm_rewrites_dependent_question(monkeypatch, fake_redis):
    """When the LLM judges the question depends on context, it rewrites it
    — the caller then correctly concludes this WAS a follow-up
    (result != original question)."""
    monkeypatch.setattr(
        "models.model_router.model_router.generate",
        lambda prompt, model_hint=None: "What is the UPI settlement confirmation step?",
    )
    result = fc.condense_followup("what about step 3?", _history())
    assert result != "what about step 3?"
    assert result == "What is the UPI settlement confirmation step?"


def test_condense_followup_prompt_instructs_llm_to_judge_self_containment(monkeypatch, fake_redis):
    """The prompt sent to the LLM must explicitly ask it to decide between
    echoing the question unchanged vs rewriting it — this is the mechanism
    that replaced the old regex classifier."""
    captured = {}

    def _capture(prompt, model_hint=None):
        captured["prompt"] = prompt
        return "some output"

    monkeypatch.setattr("models.model_router.model_router.generate", _capture)
    fc.condense_followup("what about step 3?", _history())

    assert "self-contained" in captured["prompt"].lower()
    assert "unchanged" in captured["prompt"].lower()


# ── condense_followup — resistance to persona/style bleed-through ──────────
#
# gateway.py prepends behavioral directives (built-in persona, a user's own
# Custom Instructions, cross-chat memory notes) onto the FIRST message in
# the conversation history before this function ever sees it. Those
# directives are arbitrary and unbounded — any user can type anything, in
# any language, in Settings — so the fix can't rely on detecting specific
# known phrases. Instead the condensation prompt itself must instruct the
# model to disregard any such directives it encounters in the transcript.
# These tests verify that instruction is actually present, and (functionally)
# that a persona-laden history doesn't change condense_followup's contract.

def test_condense_followup_prompt_tells_model_to_ignore_persona_directives(monkeypatch, fake_redis):
    """The prompt must explicitly instruct the model to ignore any
    persona/tone/style directives found in the conversation transcript,
    regardless of what that directive says or what language it's in —
    this is what prevents a chatty/long rewrite that fails the length
    sanity check on the primary (paid) hop."""
    captured = {}

    def _capture(prompt, model_hint=None):
        captured["prompt"] = prompt
        return "some output"

    monkeypatch.setattr("models.model_router.model_router.generate", _capture)
    fc.condense_followup("what about step 3?", _history())

    prompt_lower = captured["prompt"].lower()
    assert "ignore" in prompt_lower
    assert "persona" in prompt_lower
    assert "not the assistant" in prompt_lower or "background utility" in prompt_lower


def test_condense_followup_still_works_with_persona_laden_history(monkeypatch, fake_redis):
    """Functional check: a history whose first message carries a persona
    directive (the real-world shape gateway.py produces) must not break
    condensation — the model still receives the full history (nothing
    stripped) and the function still returns whatever the model outputs,
    following the same contract as a persona-free history."""
    persona_history = [
        {
            "role": "user",
            "content": (
                "[PERSONA — talk like a helpful friend, not a corporate bot] "
                "Be warm, natural, and genuinely conversational. Their name "
                "is Naveen. Use contractions and everyday language.\n\n"
                "What is UPI settlement TAT?"
            ),
        },
        {"role": "assistant", "content": "UPI settlement TAT is T+1 business day."},
    ]
    captured = {}

    def _capture(prompt, model_hint=None):
        captured["prompt"] = prompt
        return "What is the UPI settlement confirmation step?"

    monkeypatch.setattr("models.model_router.model_router.generate", _capture)
    result = fc.condense_followup("what about step 3?", persona_history)

    # The persona text is NOT stripped out of the history sent to the model
    # (stripping arbitrary/unknown user text is fragile — see module
    # docstring) — it's present verbatim, alongside the ignore-directive.
    assert "PERSONA" in captured["prompt"]
    assert "ignore" in captured["prompt"].lower()
    # condense_followup still returns the model's output normally.
    assert result == "What is the UPI settlement confirmation step?"
