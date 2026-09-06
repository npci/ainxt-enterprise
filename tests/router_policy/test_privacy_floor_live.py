# SPDX-License-Identifier: MIT
# ============================================================
# Phase 4 — LIVE model-router privacy-floor enforcement
# ============================================================
#
# The pure decision function (profiles/routing.py) is covered by
# tests/profiles/test_routing.py. THIS suite covers the *live* enforcement point
# wired into models.model_router.ModelRouter.route()/generate() — the hard
# enterprise invariant that CONFIDENTIAL+ data never egresses to a cloud
# provider, even when the caller passed an explicit cloud model_hint.
# ============================================================

import pytest

from models.model_router import (
    ModelRouter,
    TIER_SIMPLE,
    _privacy_requires_local,
    classification_from_policy,
)


@pytest.mark.parametrize("cls,expected", [
    ("RESTRICTED", True),
    ("restricted", True),
    ("PCI_SENSITIVE", True),
    ("CONFIDENTIAL", True),
    ("  Restricted  ", True),
    ("INTERNAL", False),
    ("PUBLIC", False),
    (None, False),
    ("", False),
    ("nonsense", False),
])
def test_privacy_ladder(cls, expected):
    assert _privacy_requires_local(cls) is expected


def test_restricted_forces_local_even_with_cloud_hint():
    """THE invariant: restricted data pins to local, overriding an explicit hint."""
    r = ModelRouter()
    d = r.route("some restricted content", model_hint="opus",
                data_classification="RESTRICTED")
    assert d.tier == TIER_SIMPLE, "restricted data must be pinned to the local tier"


def test_confidential_forces_local():
    r = ModelRouter()
    d = r.route("confidential text", model_hint="claude",
                data_classification="CONFIDENTIAL")
    assert d.tier == TIER_SIMPLE


def test_public_is_not_forced_local():
    """Public/internal traffic keeps normal routing — floor must not over-restrict."""
    r = ModelRouter()
    d = r.route("just a general question", model_hint="opus",
                data_classification="PUBLIC")
    assert d.tier != TIER_SIMPLE


def test_no_classification_is_unchanged():
    r = ModelRouter()
    d_plain = r.route("hello", model_hint="opus")
    d_none = r.route("hello", model_hint="opus", data_classification=None)
    assert d_plain.tier == d_none.tier


class _FakeRouting:
    def __init__(self, floor):
        self.privacy_floor = floor


class _FakePolicy:
    def __init__(self, floor):
        self.routing = _FakeRouting(floor)


@pytest.mark.parametrize("floor,expected", [
    ("restricted", "RESTRICTED"),
    ("confidential", "CONFIDENTIAL"),
    ("internal", None),
    ("public", None),
    (None, None),
])
def test_classification_from_policy(floor, expected):
    assert classification_from_policy(_FakePolicy(floor)) == expected


def test_classification_from_policy_handles_none():
    assert classification_from_policy(None) is None
