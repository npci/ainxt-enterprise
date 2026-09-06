# SPDX-License-Identifier: MIT
# ============================================================
# P10 — model router policy: constraint filter + weighted score (pure)
# ============================================================

from router.policy import (
    ModelSpec,
    RouteRequest,
    RouteWeights,
    route,
)


LOCAL = ModelSpec(name="local", tier="medium", context_window=8000, privacy_tier="restricted",
                  cost_per_1k=0.0, latency_ms=300, quality=0.5, supports_tools=True, is_local=True)
SONNET = ModelSpec(name="sonnet", tier="complex", context_window=200000, privacy_tier="confidential",
                   cost_per_1k=0.003, latency_ms=800, quality=0.9, supports_tools=True)
GEMINI = ModelSpec(name="gemini", tier="vision", context_window=100000, privacy_tier="internal",
                   cost_per_1k=0.001, latency_ms=700, quality=0.8, supports_tools=True,
                   supports_vision=True)
ALL = [LOCAL, SONNET, GEMINI]


def test_restricted_forces_local_only():
    r = route(RouteRequest(sensitivity="restricted", complexity="complex"), ALL,
              weights=RouteWeights(quality=1.0), default_model="x")
    assert r.model == "local"
    assert "sonnet" in r.rejected and "gemini" in r.rejected


def test_quality_first_picks_best_capable():
    r = route(RouteRequest(sensitivity="internal", complexity="complex"), ALL,
              weights=RouteWeights(quality=1.0, cost=0.0))
    assert r.model == "sonnet"   # highest quality, handles complex


def test_context_window_filter():
    r = route(RouteRequest(sensitivity="internal", tokens_needed=50000, complexity="medium"),
              ALL, default_model="x")
    # local (8k window) rejected for 50k tokens
    assert "local" in r.rejected
    assert r.model in ("sonnet", "gemini")


def test_vision_requirement_filters():
    r = route(RouteRequest(sensitivity="internal", needs_vision=True, complexity="medium"), ALL)
    assert r.model == "gemini"   # only vision-capable
    assert "local" in r.rejected and "sonnet" in r.rejected


def test_budget_pressure_biases_to_local():
    # near cap → downshift to free local even though sonnet has higher quality
    r = route(RouteRequest(sensitivity="internal", complexity="medium"), ALL,
              weights=RouteWeights(quality=1.0, cost=0.1),
              budget_remaining=0.5, budget_cap=100.0)  # 99.5% consumed
    assert r.model == "local"
    assert r.reason == "budget-downshift→local"


def test_cost_weighted_prefers_cheaper_when_quality_close():
    cheap = ModelSpec(name="cheap", tier="medium", context_window=8000, privacy_tier="internal",
                      cost_per_1k=0.0001, latency_ms=400, quality=0.75)
    pricey = ModelSpec(name="pricey", tier="medium", context_window=8000, privacy_tier="internal",
                       cost_per_1k=0.01, latency_ms=400, quality=0.78)
    r = route(RouteRequest(sensitivity="internal", tokens_needed=2000, complexity="medium"),
              [cheap, pricey], weights=RouteWeights(quality=1.0, cost=50.0))
    assert r.model == "cheap"


def test_fail_safe_returns_default_when_none_viable():
    r = route(RouteRequest(sensitivity="restricted"), [SONNET, GEMINI], default_model="fallback")
    assert r.model == "fallback"
    assert r.reason == "no viable candidate"


def test_empty_candidates_returns_default():
    r = route(RouteRequest(), [], default_model="d")
    assert r.model == "d"
