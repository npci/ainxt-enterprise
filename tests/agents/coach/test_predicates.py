# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt COACH — rule predicate + scoring-math unit tests
# ============================================================
#
# Every predicate in agents.coach_evaluator is a pure function
# (event, ctx) -> Optional[evidence_dict]. These tests exercise each rule with
# crafted positive + negative cases, then verify the exponential-decay scoring
# math (_decay_score), the MIN_EVENTS gate, and severity weighting.
#
# No DB or network is required — predicates and _decay_score are self-contained.
# Tests that touch the model registry / classifier (tool.premium_for_trivial)
# are tolerant of those modules being unavailable.
# ============================================================

import math

import pytest

from agents import coach_evaluator as ce
from services.coach_ingestor.ingestor import _redact as _coach_redact


# ── helpers ──────────────────────────────────────────────────────────────────

def _event(**kw):
    """A minimal redacted coach_event dict."""
    base = {
        "user_id": "u1",
        "channel": "web",
        "department": "ENG",
        "model": None,
        "prompt_redacted": "",
        "prompt_hash": None,
        "context_window_pct": 0.0,
        "tool_calls": [],
        "accepted": None,
        "pii_flags": [],
        "secret_flags": [],
        "compliance_flags": [],
        "governance_flags": [],
    }
    base.update(kw)
    return base


def _fire(rule_id, event, ctx=None):
    """Return the evidence dict (truthy) or None for a single rule."""
    return ce.RULES_BY_ID[rule_id].predicate(event, ctx or {})


# ── registry sanity ──────────────────────────────────────────────────────────

def test_registry_is_complete_and_unique():
    ids = [r.rule_id for r in ce.BASELINE_RULES]
    assert len(ids) == len(set(ids)), "duplicate rule ids"
    assert len(ids) >= 23
    # every rule maps to a known category and a known severity weight
    for r in ce.BASELINE_RULES:
        assert r.category in ce.ALL_CATEGORIES
        assert r.severity in ce._SEVERITY_WEIGHT
        assert r.title and r.advice
    assert set(ce.RULES_BY_ID) == set(ids)


def test_rule_catalog_shape():
    cat = ce.rule_catalog()
    assert len(cat) == len(ce.BASELINE_RULES)
    for m in cat:
        assert set(m) >= {"rule_id", "code", "category", "severity", "title", "advice"}
        assert m["code"].startswith("AINXT-")


# ── prompt-quality ───────────────────────────────────────────────────────────

def test_vague_fires_on_trivial_prompt():
    assert _fire("prompt.vague", _event(prompt_redacted="fix it"))
    assert _fire("prompt.vague", _event(prompt_redacted="help"))


def test_vague_silent_on_specific_prompt():
    p = "Refactor parse_config() in core/config.py to validate the PORT env var is an int"
    assert _fire("prompt.vague", _event(prompt_redacted=p)) is None
    assert _fire("prompt.vague", _event(prompt_redacted="what is python anaconda?")) is None
    assert _fire("prompt.vague", _event(prompt_redacted="explain java 8 features")) is None
    # empty prompt does not fire
    assert _fire("prompt.vague", _event(prompt_redacted="")) is None


def test_missing_acceptance():
    long_no_criteria = "write a function that downloads the file and stores the bytes somewhere useful"
    assert _fire("prompt.missing_acceptance", _event(prompt_redacted=long_no_criteria))
    with_criteria = "write a parser; expected output is a dict and it should return None on bad input"
    assert _fire("prompt.missing_acceptance", _event(prompt_redacted=with_criteria)) is None
    # short prompt is exempt (covered by vague rule)
    assert _fire("prompt.missing_acceptance", _event(prompt_redacted="do thing")) is None


def test_ambiguous_pronoun():
    assert _fire("prompt.ambiguous_pronoun", _event(prompt_redacted="fix it and then make that work"))
    assert _fire("prompt.ambiguous_pronoun",
                 _event(prompt_redacted="add input validation to the registerUser handler in auth.py")) is None


def test_multi_intent():
    p = "add tests and also update the docs; also bump the version and also fix the lint?"
    assert _fire("prompt.multi_intent", _event(prompt_redacted=p))
    assert _fire("prompt.multi_intent", _event(prompt_redacted="just add one unit test for foo()")) is None
    # three questions also trips it
    assert _fire("prompt.multi_intent", _event(prompt_redacted="why? how? when?"))


def test_missing_constraints():
    p = "build a rate limiter for the api that handles a lot of traffic gracefully please"
    assert _fire("prompt.missing_constraints", _event(prompt_redacted=p))
    p2 = "build a rate limiter that must cap at 100 req/s and should use redis only"
    assert _fire("prompt.missing_constraints", _event(prompt_redacted=p2)) is None


def test_no_success_def():
    p = "please implement the new caching layer for the retrieval pipeline across all of the namespaces"
    assert _fire("prompt.no_success_def", _event(prompt_redacted=p))
    p2 = "implement caching for retrieval; this is done when cache hit-rate exceeds eighty percent in the bench"
    assert _fire("prompt.no_success_def", _event(prompt_redacted=p2)) is None


# ── session-hygiene ──────────────────────────────────────────────────────────

def test_thread_too_long():
    assert _fire("session.thread_too_long", _event(), {"thread_msg_count": 45})
    assert _fire("session.thread_too_long", _event(), {"thread_msg_count": 10}) is None


def test_excess_continue():
    assert _fire("session.excess_continue", _event(prompt_redacted="continue"), {"continue_count": 5})
    # not a continue prompt → silent even with high count
    assert _fire("session.excess_continue", _event(prompt_redacted="write a test"), {"continue_count": 9}) is None
    # continue but low count → silent
    assert _fire("session.excess_continue", _event(prompt_redacted="continue"), {"continue_count": 1}) is None


def test_stale_resume():
    ctx = {"seconds_since_thread_start": 8 * 3600, "thread_msg_count": 6}
    assert _fire("session.stale_resume", _event(), ctx)
    assert _fire("session.stale_resume", _event(), {"seconds_since_thread_start": 60, "thread_msg_count": 6}) is None


# ── review-discipline ────────────────────────────────────────────────────────

def test_low_acceptance():
    assert _fire("review.low_acceptance", _event(), {"recent_acceptance_rate": 0.1, "recent_acceptance_samples": 10})
    # too few samples → silent
    assert _fire("review.low_acceptance", _event(), {"recent_acceptance_rate": 0.1, "recent_acceptance_samples": 2}) is None
    # healthy rate → silent
    assert _fire("review.low_acceptance", _event(), {"recent_acceptance_rate": 0.8, "recent_acceptance_samples": 10}) is None


def test_unreviewed_apply():
    assert _fire("review.unreviewed_apply", _event(accepted=True), {"review_dwell_ms": 200})
    assert _fire("review.unreviewed_apply", _event(accepted=True), {"review_dwell_ms": 9000}) is None
    # not accepted → silent
    assert _fire("review.unreviewed_apply", _event(accepted=False), {"review_dwell_ms": 200}) is None


# ── tool-mastery ─────────────────────────────────────────────────────────────

def test_premium_for_trivial_silent_without_model():
    assert _fire("tool.premium_for_trivial", _event(model=None)) is None


def test_premium_for_trivial_monkeypatched(monkeypatch):
    """Force a premium model + a 'simple/high-confidence' classification."""
    import core.model_registry as mr
    monkeypatch.setattr(mr, "MODEL_COST_PER_1M", {"premium-x": (5.0, 25.0)}, raising=False)
    import models.classifier as clf
    monkeypatch.setattr(clf, "classify_with_confidence", lambda *_a, **_k: ("simple", 0.95), raising=False)

    ev = _event(model="premium-x", prompt_redacted="hi there")
    out = _fire("tool.premium_for_trivial", ev)
    assert out and out["model"] == "premium-x"

    # complex classification → silent
    monkeypatch.setattr(clf, "classify_with_confidence", lambda *_a, **_k: ("complex", 0.95), raising=False)
    assert _fire("tool.premium_for_trivial", ev) is None


def test_retry_storm():
    assert _fire("tool.retry_storm", _event(), {"tool_retry_count": 6})
    assert _fire("tool.retry_storm", _event(), {"tool_retry_count": 1}) is None


def test_unused_tools():
    assert _fire("tool.unused_tools", _event(tool_calls=[]), {"suggested_tool": "gitlab_create_mr"})
    # already invoked a tool → silent
    assert _fire("tool.unused_tools", _event(tool_calls=[{"name": "x"}]), {"suggested_tool": "gitlab_create_mr"}) is None


# ── context-management ───────────────────────────────────────────────────────

def test_context_saturated():
    assert _fire("context.saturated", _event(context_window_pct=95.0))
    assert _fire("context.saturated", _event(context_window_pct=40.0)) is None


def test_cross_channel():
    ev = _event(prompt_hash="abc", channel="web")
    ctx = {"recent_channels_by_prompt_hash": {"abc": ["cli", "web"]}}
    assert _fire("context.cross_channel", ev, ctx)
    assert _fire("context.cross_channel", ev, {"recent_channels": ["web", "cli", "slack"]}) is None
    assert _fire("context.cross_channel", _event(prompt_hash="zzz", channel="web"), ctx) is None


def test_kb_miss():
    assert _fire("context.kb_miss", _event(), {"kb_hit": False, "kb_attempted": True})
    assert _fire("context.kb_miss", _event(), {"kb_hit": True, "kb_attempted": True}) is None
    # not attempted → silent
    assert _fire("context.kb_miss", _event(), {"kb_hit": False, "kb_attempted": False}) is None


def test_duplicate_prompt():
    assert _fire("context.duplicate_prompt", _event(prompt_hash="abc"), {"recent_prompt_hashes": ["abc", "def"]})
    assert _fire("context.duplicate_prompt", _event(prompt_hash="zzz"), {"recent_prompt_hashes": ["abc"]}) is None


# ── security ─────────────────────────────────────────────────────────────────

def test_pii():
    assert _fire("security.pii_in_prompt", _event(pii_flags=["EMAIL"]))
    assert _fire("security.pii_in_prompt", _event(pii_flags=[])) is None


def test_secret():
    assert _fire("security.secret_in_prompt", _event(secret_flags=["API_KEY"]))
    assert _fire("security.secret_in_prompt", _event(secret_flags=[])) is None


def test_compliance():
    assert _fire("security.compliance_block", _event(compliance_flags=["PAN"]))
    assert _fire("security.compliance_block", _event(compliance_flags=[])) is None


def test_governance():
    assert _fire("security.governance_flag", _event(governance_flags=["policy_x"]))
    assert _fire("security.governance_flag", _event(governance_flags=[])) is None


def test_sensitive_keyword():
    assert _fire("security.sensitive_keyword", _event(prompt_redacted="what is the admin password again"))
    # suppressed when a secret/compliance flag already covered it
    assert _fire("security.sensitive_keyword",
                 _event(prompt_redacted="here is the api key", secret_flags=["API_KEY"])) is None
    assert _fire("security.sensitive_keyword", _event(prompt_redacted="write a haiku about spring")) is None


@pytest.mark.parametrize("rule_id,event,ctx", [
    ("prompt.vague", _event(prompt_redacted="fix it"), {}),
    ("prompt.missing_acceptance", _event(prompt_redacted="write a function that downloads the file and stores the bytes somewhere useful"), {}),
    ("prompt.ambiguous_pronoun", _event(prompt_redacted="fix it and then make that work"), {}),
    ("prompt.multi_intent", _event(prompt_redacted="add tests and also update docs and also bump version and also fix lint"), {}),
    ("prompt.missing_constraints", _event(prompt_redacted="build a rate limiter for the api that handles lots of traffic gracefully please"), {}),
    ("prompt.no_success_def", _event(prompt_redacted="please implement the new caching layer for the retrieval pipeline across all namespaces today"), {}),
    ("session.thread_too_long", _event(), {"thread_msg_count": 45}),
    ("session.excess_continue", _event(prompt_redacted="continue"), {"continue_count": 5}),
    ("session.stale_resume", _event(), {"seconds_since_thread_start": 8 * 3600, "thread_msg_count": 6}),
    ("review.low_acceptance", _event(), {"recent_acceptance_rate": 0.1, "recent_acceptance_samples": 10}),
    ("review.unreviewed_apply", _event(accepted=True), {"review_dwell_ms": 200}),
    ("tool.retry_storm", _event(), {"tool_retry_count": 6}),
    ("tool.unused_tools", _event(tool_calls=[]), {"suggested_tool": "gitlab_create_mr"}),
    ("context.saturated", _event(context_window_pct=95.0), {}),
    ("context.cross_channel", _event(prompt_hash="abc", channel="web"), {"recent_channels_by_prompt_hash": {"abc": ["cli"]}}),
    ("context.kb_miss", _event(), {"kb_hit": False, "kb_attempted": True}),
    ("context.duplicate_prompt", _event(prompt_hash="abc"), {"recent_prompt_hashes": ["abc"]}),
    ("security.pii_in_prompt", _event(pii_flags=["MOBILE"]), {}),
    ("security.secret_in_prompt", _event(secret_flags=["API_KEY"]), {}),
    ("security.compliance_block", _event(compliance_flags=["PAN"]), {}),
    ("security.governance_flag", _event(governance_flags=["policy_x"]), {}),
    ("security.sensitive_keyword", _event(prompt_redacted="what is the admin password again"), {}),
])
def test_representative_fixture_fires_expected_rule(rule_id, event, ctx):
    ids = {h["rule_id"] for h in ce._run_rules(event, ctx)}
    assert rule_id in ids


def test_representative_fixture_covers_every_rule_except_classifier_dependent():
    covered = {
        "prompt.vague", "prompt.missing_acceptance", "prompt.ambiguous_pronoun", "prompt.multi_intent",
        "prompt.missing_constraints", "prompt.no_success_def", "session.thread_too_long",
        "session.excess_continue", "session.stale_resume", "review.low_acceptance",
        "review.unreviewed_apply", "tool.retry_storm", "tool.unused_tools", "context.saturated",
        "context.cross_channel", "context.kb_miss", "context.duplicate_prompt", "security.pii_in_prompt",
        "security.secret_in_prompt", "security.compliance_block", "security.governance_flag",
        "security.sensitive_keyword",
    }
    assert set(ce.RULES_BY_ID) - {"tool.premium_for_trivial"} == covered


def test_noisy_normal_questions_do_not_get_success_definition_rule():
    assert _fire("prompt.no_success_def", _event(prompt_redacted="what is java 8 features?")) is None
    assert _fire("prompt.no_success_def", _event(prompt_redacted="can you please validate my mobile number 99*****99?")) is None


def test_ingestor_redaction_maps_mobile_prompt_to_pii_flag():
    redacted, pii, secret, compliance = _coach_redact("can you please validate my mobile number 999999999?")
    assert "MOBILE" in pii
    assert "999999999" not in redacted
    assert secret == []
    assert compliance == []


def test_ingestor_redaction_maps_card_number_prompt_to_coach_pii_flag():
    redacted, pii, secret, compliance = _coach_redact("99999999 please check this card number")
    assert "ACCOUNT_NUMBER" in pii
    assert "99999999" not in redacted
    assert secret == []
    assert compliance == []


def test_ingestor_redaction_does_not_flag_unanchored_numeric_text():
    redacted, pii, secret, compliance = _coach_redact("99999999 please check this")
    assert pii == []
    assert redacted == "99999999 please check this"
    assert secret == []
    assert compliance == []


# ── evaluate_dry_run ─────────────────────────────────────────────────────────

def test_dry_run_runs_all_and_subset():
    ev = _event(prompt_redacted="fix it", pii_flags=["EMAIL"])
    all_hits = ce.evaluate_dry_run(ev)
    ids = {h["rule_id"] for h in all_hits}
    assert "prompt.vague" in ids
    assert "security.pii_in_prompt" in ids
    # subset restriction
    only = ce.evaluate_dry_run(ev, rules=["security.pii_in_prompt"])
    assert {h["rule_id"] for h in only} == {"security.pii_in_prompt"}


def test_dry_run_hit_has_metadata_and_evidence():
    ev = _event(pii_flags=["AADHAAR"])
    hits = ce.evaluate_dry_run(ev, rules=["security.pii_in_prompt"])
    assert len(hits) == 1
    h = hits[0]
    assert h["category"] == ce.CATEGORY_SECURITY
    assert h["severity"] == "high"
    assert h["title"] and h["advice"]
    assert h["evidence"]["pii_types"] == ["AADHAAR"]


# ── scoring math ─────────────────────────────────────────────────────────────

def test_decay_score_monotonic_and_bounds():
    assert ce._decay_score(0.0) == 100.0
    assert 0.0 <= ce._decay_score(1000.0) <= 100.0
    # strictly decreasing with penalty
    a, b, c = ce._decay_score(1), ce._decay_score(5), ce._decay_score(20)
    assert a > b > c


def test_decay_score_matches_formula():
    pen = 12.0
    expected = round(100.0 * math.exp(-pen / ce.COACH_SCORE_DECAY_K), 2)
    assert ce._decay_score(pen) == expected


def test_severity_weight_ordering():
    w = ce._SEVERITY_WEIGHT
    assert w["low"] < w["medium"] < w["high"] < w["critical"]
