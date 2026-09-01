# SPDX-License-Identifier: Apache-2.0
# ============================================================
# CIL — model-only analyze() (Wave 3: regex-free)
# ============================================================
#
# The chat conversation-intelligence path is now MODEL-ONLY (cil/intent.py).
# analyze() no longer uses any regex/keyword lexical signals. Its contract:
#   - NEVER raises.
#   - Returns a ConversationState.
#   - In a bare env (no model reachable) or when the model is skipped/off, it
#     returns the SAFE STATIC DEFAULT (medium/general/chat) with
#     intent_source="default" — there is NO regex fallback.
# ============================================================

from cil.analyze import analyze
from cil.state import ConversationState


def test_analyze_returns_conversation_state():
    st = analyze("what is 2+2?")
    assert isinstance(st, ConversationState)
    assert st.task_complexity in ("simple", "medium", "complex", "deep", "solution")
    assert st.domain  # non-empty
    assert st.analyze_ms >= 0.0


def test_analyze_never_raises_on_garbage():
    for bad in [None, "", "   ", "🙂" * 1000, "\x00\x01"]:
        st = analyze(bad)  # type: ignore[arg-type]
        assert isinstance(st, ConversationState)


def test_bare_env_uses_static_default_not_regex():
    # No model reachable in the test env → safe static default, NOT regex.
    st = analyze("write a function to sort a list")
    assert st.intent_source in ("default", "model")
    if st.intent_source == "default":
        assert st.task_complexity == "medium"
        assert st.domain == "general"
        assert st.intent == "chat"
        assert "default" in st.signal_sources
        # the old regex provenance tag must NEVER appear
        assert "lexical" not in st.signal_sources


def test_skip_model_forces_static_default():
    st = analyze("refactor this entire service", skip_model=True)
    assert st.intent_source == "default"
    assert st.task_complexity == "medium"
    assert "lexical" not in st.signal_sources


def test_defaults_equal_todays_posture():
    st = ConversationState()
    assert st.task_complexity == "medium"
    assert st.domain == "general"
    assert st.clarification_needed is False
    assert st.tool_need.score == 0.0
    assert st.output_format == "prose"
    assert st.intent == "chat"
    assert st.intent_source == "default"


def test_snapshot_is_scalar():
    snap = analyze("hi").snapshot()
    for k, v in snap.items():
        assert v is None or isinstance(v, (str, int, float, bool)), (k, type(v))


def test_snapshot_includes_intent_fields():
    snap = analyze("hi", skip_model=True).snapshot()
    for key in ("intent", "skill_hint", "agent_hint", "intent_conf", "intent_source"):
        assert key in snap


def test_snapshot_includes_style_fields():
    snap = analyze("hi", skip_model=True).snapshot()
    for key in ("tone", "formality", "language", "sentiment", "wants_brief"):
        assert key in snap


def test_style_defaults_are_neutral():
    st = ConversationState()
    assert st.tone == "neutral"
    assert st.language == "en"
    assert st.sentiment == "neutral"
    assert st.wants_brief is False
