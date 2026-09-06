# SPDX-License-Identifier: MIT
# ============================================================
# P05 — CIL Class-4 policy derivation (pure)
# ============================================================

from cil.policy import (
    DomainProfile,
    derive_clarification,
    derive_policy,
    derive_risk_level,
    derive_sensitivity,
    tools_allowed,
)
from cil.state import ConversationState, Score


def _state(**kw):
    st = ConversationState()
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_write_tool_is_high_risk():
    st = _state(tool_need=Score(score=0.9, tags=["email"]))
    assert derive_risk_level(st, DomainProfile()) == "high"


def test_read_tool_not_high_risk():
    st = _state(tool_need=Score(score=0.9, tags=["search"]))
    assert derive_risk_level(st, DomainProfile()) == "low"


def test_high_risk_domain():
    st = _state(domain="finance")
    p = DomainProfile(high_risk_domains=["finance", "legal"])
    assert derive_risk_level(st, p) == "high"


def test_sensitive_content_is_medium():
    st = _state()
    setattr(st, "sensitivity", "confidential")
    assert derive_risk_level(st, DomainProfile()) == "medium"


def test_clarify_only_when_ambiguous_high_stakes_and_not_continuation():
    p = DomainProfile(high_risk_domains=["finance"], ambiguity_threshold=0.6,
                      min_risk_to_clarify="high")
    ambiguous_risky = _state(domain="finance", ambiguity=Score(score=0.8),
                             is_continuation=False)
    assert derive_clarification(ambiguous_risky, p) is True

    # same but a continuation → do not interrupt
    cont = _state(domain="finance", ambiguity=Score(score=0.8), is_continuation=True)
    assert derive_clarification(cont, p) is False

    # ambiguous but low-risk → do not clarify
    low = _state(domain="general", ambiguity=Score(score=0.8))
    assert derive_clarification(low, p) is False


def test_sensitivity_most_restrictive_wins():
    st = _state()
    setattr(st, "sensitivity", "restricted")
    # profile default internal, state says restricted → restricted
    assert derive_sensitivity(st, DomainProfile(default_sensitivity="internal")) == "restricted"
    # no hint → profile default
    assert derive_sensitivity(_state(), DomainProfile(default_sensitivity="confidential")) == "confidential"


def test_tools_allowed_threshold():
    p = DomainProfile(tool_need_threshold=0.5)
    assert tools_allowed(_state(tool_need=Score(score=0.6)), p) is True
    assert tools_allowed(_state(tool_need=Score(score=0.4)), p) is False


def test_derive_policy_one_shot():
    st = _state(tool_need=Score(score=0.9, tags=["deploy"]), domain="general")
    out = derive_policy(st, DomainProfile(name="devops"))
    assert out["risk_level"] == "high"
    assert out["tools_allowed"] is True
    assert out["profile"] == "devops"


def test_fail_safe_on_garbage_state():
    class Bad:
        pass
    assert derive_risk_level(Bad(), DomainProfile()) == "low"
    assert derive_clarification(Bad(), DomainProfile()) is False
