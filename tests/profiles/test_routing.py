# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Wave 4 — constraint-filtered weighted routing (pure)
# ============================================================

from profiles.routing import ModelCandidate, choose
from profiles.schema import RoutingPolicy


_LOCAL = ModelCandidate(name="local", context_window=128000, privacy_tier="restricted",
                        quality=0.5, cost_per_1k=0.0, latency_ms=200)
_SONNET = ModelCandidate(name="claude", context_window=200000, privacy_tier="public",
                         quality=0.9, cost_per_1k=0.009, latency_ms=1500)
_GEMINI = ModelCandidate(name="gemini", context_window=1000000, privacy_tier="public",
                         quality=0.8, cost_per_1k=0.002, latency_ms=1200, supports_vision=True)
_ALL = [_LOCAL, _SONNET, _GEMINI]


def test_restricted_content_is_local_only():
    # THE enterprise invariant: restricted sensitivity must never pick a cloud model.
    d = choose(_ALL, sensitivity="restricted", tokens_needed=1000)
    assert d is not None
    assert d.model == "local"


def test_quality_wins_for_public_content():
    d = choose(_ALL, sensitivity="public", tokens_needed=1000,
               policy=RoutingPolicy(w_quality=1.0, w_cost=0.0, w_latency=0.0))
    assert d.model == "claude"  # highest quality


def test_cost_weight_shifts_to_cheaper_model():
    d = choose(_ALL, sensitivity="public", tokens_needed=100000,
               policy=RoutingPolicy(w_quality=0.3, w_cost=1.0, w_latency=0.0))
    # heavy cost weight on a big request → cheaper cloud model (gemini) or local
    assert d.model in ("gemini", "local")


def test_context_window_constraint_filters_small_models():
    small = ModelCandidate(name="small", context_window=8000, privacy_tier="public", quality=0.99)
    d = choose([small, _GEMINI], sensitivity="public", tokens_needed=500000)
    assert d.model == "gemini"  # small window rejected despite higher quality


def test_vision_requirement_filters_non_vision():
    d = choose([_SONNET, _GEMINI], sensitivity="public", tokens_needed=1000, need_vision=True)
    assert d.model == "gemini"  # only vision-capable survivor


def test_no_candidate_returns_none_for_fallback():
    # nothing can serve restricted if no restricted-capable model exists →
    # None → caller keeps today's route (fail-safe).
    d = choose([_SONNET, _GEMINI], sensitivity="restricted", tokens_needed=1000)
    assert d is None


def test_latency_ceiling_is_soft_but_respected_when_alternatives_exist():
    d = choose(_ALL, sensitivity="public", tokens_needed=1000,
               policy=RoutingPolicy(w_quality=1.0, w_cost=0.0, w_latency=0.0, max_latency_ms=500))
    assert d.model == "local"  # only one under 500ms


def test_deterministic():
    a = choose(_ALL, sensitivity="public", tokens_needed=1000)
    b = choose(_ALL, sensitivity="public", tokens_needed=1000)
    assert a == b


def test_profile_privacy_floor_is_enforced():
    # A regulated profile with privacy_floor='confidential' must keep even
    # confidential-sensitivity traffic off public cloud models.
    floor = RoutingPolicy(w_quality=1.0, w_cost=0.0, w_latency=0.0,
                          privacy_floor="confidential")
    d = choose(_ALL, sensitivity="confidential", tokens_needed=1000, policy=floor)
    # claude/gemini are 'public' tier → rejected by the floor; only local remains
    assert d is not None and d.model == "local"


def test_privacy_floor_does_not_over_restrict_public_asks():
    # public-sensitivity request under a public floor still reaches cloud
    d = choose(_ALL, sensitivity="public", tokens_needed=1000,
               policy=RoutingPolicy(w_quality=1.0, w_cost=0.0, w_latency=0.0,
                                    privacy_floor="public"))
    assert d.model == "claude"
