# SPDX-License-Identifier: Apache-2.0
# ============================================================
# P17 (PR5) — adaptive response shaping merge (pure)
# ============================================================

from profiles.shaping import ResponseShape, shape_response


def test_per_turn_style_overrides_user_and_role():
    s = shape_response(profile_style="detailed", role_style="detailed",
                       user_style="concise", turn_style="step_by_step")
    assert s.style == "step_by_step"


def test_user_overrides_role_and_profile():
    s = shape_response(profile_style="detailed", role_style="detailed", user_style="concise")
    assert s.style == "concise"


def test_role_overrides_profile():
    s = shape_response(profile_style="detailed", role_style="concise")
    assert s.style == "concise"


def test_expertise_fills_style_when_no_explicit_signal():
    # only profile default present → novice pushes detailed, expert pushes concise
    assert shape_response(profile_style="detailed", expertise="expert").style == "concise"
    assert shape_response(profile_style="concise", expertise="novice").style == "detailed"


def test_explicit_style_beats_expertise():
    # a user preference must not be overridden by expertise heuristic
    s = shape_response(profile_style="detailed", user_style="concise", expertise="novice")
    assert s.style == "concise"


def test_depth_from_expertise():
    assert shape_response(expertise="novice").depth == "scaffolded"
    assert shape_response(expertise="expert").depth == "terse"
    assert shape_response(expertise="intermediate").depth == "normal"


def test_empathy_from_emotional_tone():
    assert shape_response(emotional_tone="frustrated").empathetic is True
    assert shape_response(emotional_tone="urgent").empathetic is True
    assert shape_response(emotional_tone="neutral").empathetic is False


def test_tone_validated():
    assert shape_response(profile_tone="formal-precise").tone == "formal-precise"
    assert shape_response(profile_tone="bogus").tone == "helpful"


def test_defaults_are_neutral():
    s = shape_response()
    assert s.style == "detailed" and s.tone == "helpful" and s.depth == "normal"
    assert s.empathetic is False


def test_as_dict_has_provenance():
    s = shape_response(user_style="concise", expertise="expert")
    d = s.as_dict()
    assert "sources" in d and any("style:" in x for x in d["sources"])
