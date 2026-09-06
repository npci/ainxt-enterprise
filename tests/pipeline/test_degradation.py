# SPDX-License-Identifier: MIT
# ============================================================
# P20 (§20.6) — degradation ladder (pure)
# ============================================================

from pipeline.degradation import (
    CLOUD_DEGRADED,
    CONTEXT_DEGRADED,
    FULL,
    LOCAL_ONLY,
    TOOLS_DEGRADED,
    Degradation,
    Health,
    degrade,
)


def test_all_healthy_is_full():
    d = degrade(Health())
    assert d.level == FULL
    assert d.available and d.use_cloud and d.use_retrieval and d.use_tools and d.rich_context
    assert d.hedge is False


def test_cloud_down_falls_back_local_generation():
    d = degrade(Health(cloud=False))
    assert d.level == CLOUD_DEGRADED
    assert d.use_cloud is False
    assert d.available is True


def test_retrieval_down_hedges():
    d = degrade(Health(retrieval=False))
    assert d.level == TOOLS_DEGRADED
    assert d.use_retrieval is False
    assert d.hedge is True


def test_tools_down_hedges():
    d = degrade(Health(tools=False))
    assert d.level == TOOLS_DEGRADED
    assert d.use_tools is False
    assert d.hedge is True


def test_context_engine_down_flat_summary():
    d = degrade(Health(context_engine=False))
    assert d.level == CONTEXT_DEGRADED
    assert d.rich_context is False


def test_worst_level_wins_when_multiple_down():
    # cloud + retrieval + context all down → most-degraded reported
    d = degrade(Health(cloud=False, retrieval=False, context_engine=False))
    assert d.level == CONTEXT_DEGRADED   # highest rank among the failures
    assert d.use_cloud is False and d.use_retrieval is False and d.rich_context is False
    assert d.hedge is True


def test_perimeter_down_is_the_only_true_outage():
    d = degrade(Health(perimeter=False))
    assert d.level == LOCAL_ONLY
    assert d.available is False
    assert d.use_cloud is False and d.use_tools is False


def test_fail_safe_on_garbage_input():
    class Bad:
        pass
    d = degrade(Bad())          # missing all attributes → policy still returns
    assert isinstance(d, Degradation)
    assert d.available is True   # alive at the floor, never crashes
