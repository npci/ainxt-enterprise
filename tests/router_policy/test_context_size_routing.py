# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Gap #5 — context-size as a first-class routing dimension
# ============================================================
#
# Frontier pattern #5 (docs/architecture/02 §2.5/§2.8): when a turn's token
# footprint won't fit the complexity-derived tier's window, the router promotes
# to a larger-window model rather than risking truncation. These tests cover the
# pure promotion helper (deterministic, no live model needed).
# ============================================================

from models.model_router import (
    _promote_for_context,
    _tier_window,
    ModelRouter,
    TIER_SIMPLE,
)


def test_small_context_does_not_promote():
    assert _promote_for_context("medium", 5_000) == "medium"


def test_medium_overflow_promotes_to_deep():
    # 150K tokens / 0.8 headroom = 187.5K needed; medium=128K can't fit → deep=256K
    assert _promote_for_context("medium", 150_000) == "deep"


def test_deep_overflow_promotes_to_gemini():
    # 250K / 0.8 = 312.5K needed; deep=256K can't fit → gemini=1M
    assert _promote_for_context("medium", 250_000) == "gemini"
    assert _promote_for_context("complex", 250_000) == "gemini"


def test_already_large_window_unchanged():
    assert _promote_for_context("gemini", 500_000) == "gemini"


def test_beyond_all_windows_picks_largest():
    assert _promote_for_context("simple", 5_000_000) == "gemini"


def test_zero_or_negative_tokens_unchanged():
    assert _promote_for_context("medium", 0) == "medium"
    assert _promote_for_context("medium", -1) == "medium"


def test_tier_window_defaults_safely():
    assert _tier_window("unknown_tier") == 128_000


def test_privacy_floor_still_wins_over_context_size():
    # A restricted turn must stay local even if the context is huge — privacy
    # is enforced before context-size routing.
    r = ModelRouter()
    d = r.route("x" * 2_000_000, data_classification="RESTRICTED")
    assert d.tier == TIER_SIMPLE
