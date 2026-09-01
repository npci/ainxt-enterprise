# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Persona & Style layer — composer + preference derivation
# ============================================================
#
# compose_persona is a PURE function (no model, no I/O). Tests cover:
#   - casual-buddy baseline + tone mirroring
#   - the HARD domain dial-down guardrail (sensitive → professional)
#   - memory / feedback-hint injection
#   - fail-safe on garbage input
# Plus the deterministic feedback→preference derivation (loop C).
# ============================================================

from cil.persona import compose_persona, compose_stable_persona
from cil.state import ConversationState


def _cs(**kw) -> ConversationState:
    s = ConversationState()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ── baseline + mirroring ─────────────────────────────────────────────────────
def test_casual_baseline_and_name():
    p = compose_persona(conv_state=_cs(domain="general", tone="casual"),
                        question="hey help me", user_name="Kannan T")
    assert "helpful friend" in p.lower()
    assert "Kannan" in p          # first name used
    assert "Kannan T" not in p    # only first name


def test_mirror_language_and_brevity():
    p = compose_persona(
        conv_state=_cs(domain="general", tone="casual", language="hinglish",
                       wants_brief=True, formality=0.2),
        question="bhai isko fix karde quickly",
    )
    assert "hinglish" in p.lower()
    assert "short" in p.lower()


def test_frustrated_user_gets_calm_directive():
    p = compose_persona(conv_state=_cs(domain="general", tone="frustrated",
                                       sentiment="neg"),
                        question="this still doesn't work")
    assert "frustrated" in p.lower()


# ── GUARDRAIL: sensitive domains must NOT be casual ──────────────────────────
def test_sensitive_domain_forces_professional():
    for dom in ("finance", "legal", "security", "compliance"):
        p = compose_persona(conv_state=_cs(domain=dom, tone="casual"),
                            question="quick q", user_name="Kannan")
        assert "professional and precise" in p.lower()
        assert "helpful friend" not in p.lower()


def test_sensitive_marker_forces_professional_even_if_general_domain():
    # domain says general, but the content is clearly sensitive → professional.
    for q in ("show me the aadhaar number", "is this PCI DSS compliant?",
              "report the fraud incident"):
        p = compose_persona(conv_state=_cs(domain="general", tone="casual"),
                            question=q)
        assert "professional" in p.lower(), q
        assert "helpful friend" not in p.lower(), q


def test_no_tone_mirror_on_sensitive():
    # even if the user is casual, a sensitive turn must not carry mirror lines.
    p = compose_persona(conv_state=_cs(domain="compliance", tone="casual",
                                       language="hinglish", wants_brief=True),
                        question="kyc rules?")
    assert "hinglish" not in p.lower()


# ── memory + feedback injection ──────────────────────────────────────────────
def test_memory_and_feedback_hint_injected():
    p = compose_persona(
        conv_state=_cs(domain="general"),
        question="hi",
        custom_about="Senior engineer in payments.",
        memory_facts=["Prefers dark mode", "Works on UPI"],
        feedback_hint="This user consistently prefers concise answers.",
    )
    assert "About the user" in p
    assert "UPI" in p
    assert "Learned preference" in p
    assert "concise" in p.lower()


# ── fail-safe ────────────────────────────────────────────────────────────────
def test_failsafe_on_garbage_conv_state():
    p = compose_persona(conv_state=object(), question="hi", user_name="X")
    assert isinstance(p, str) and len(p) > 0


def test_empty_inputs_still_returns_a_block():
    p = compose_persona()
    assert isinstance(p, str)  # never crashes


# ── loop C: deterministic preference derivation ──────────────────────────────
def test_preference_derivation_concise():
    from workers.preference_learner import _derive_from_rows
    rows = [{"rating": -1, "issue": "too long", "sub_issue": "", "comment": ""}
            for _ in range(3)] + [{"rating": 1}]
    assert "concise" in (_derive_from_rows(rows) or "").lower()


def test_preference_derivation_needs_enough_signals():
    from workers.preference_learner import _derive_from_rows
    assert _derive_from_rows([{"rating": -1, "issue": "too long"}]) is None


def test_preference_derivation_no_majority_returns_none():
    from workers.preference_learner import _derive_from_rows
    mixed = [{"rating": -1, "issue": "too long"},
             {"rating": -1, "issue": "too short"},
             {"rating": -1, "issue": "off topic"}]
    assert _derive_from_rows(mixed) is None


def test_preference_derivation_too_formal_to_casual():
    from workers.preference_learner import _derive_from_rows
    rows = [{"rating": -1, "issue": "too formal", "sub_issue": "robotic"}
            for _ in range(4)]
    assert "casual" in (_derive_from_rows(rows) or "").lower()


# ── compose_stable_persona — KV-cache variant ────────────────────────────────

def test_stable_persona_includes_name_and_custom_about():
    p = compose_stable_persona(
        user_name="Kannan T",
        custom_about="Senior engineer in payments.",
        custom_style="concise",
    )
    assert "Kannan" in p
    assert "Senior engineer" in p
    assert "concise" in p.lower()


def test_stable_persona_has_no_mirror_lines():
    """Tone-mirror lines (language/brevity/sentiment) must NOT appear."""
    p = compose_stable_persona(
        user_name="Kannan",
        custom_about="Senior engineer",
        custom_style="concise",
    )
    # These strings only appear when _mirror_lines() fires
    assert "hinglish" not in p.lower()
    assert "match the user" not in p.lower()
    assert "wants it short" not in p.lower()


def test_stable_persona_no_memory_instruction():
    """compose_stable_persona must NOT include the memory instruction footer —
    that is added separately by _build_local_system_message."""
    p = compose_stable_persona(user_name="X")
    assert "MEMORY INSTRUCTION" not in p


def test_stable_persona_sensitive_forces_professional():
    p = compose_stable_persona(user_name="X", sensitive=True)
    assert "professional" in p.lower()
    assert "helpful friend" not in p.lower()


def test_stable_persona_empty_inputs_never_crashes():
    p = compose_stable_persona()
    assert isinstance(p, str)


def test_stable_persona_memory_facts_injected():
    p = compose_stable_persona(
        memory_facts=["Prefers dark mode", "Works on UPI"],
        feedback_hint="This user consistently prefers concise answers.",
    )
    assert "UPI" in p
    assert "Learned preference" in p
    assert "concise" in p.lower()
