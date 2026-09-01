# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Gateway browser-agent passthrough — decision-logic unit tests
# ============================================================
#
# gateway.py cannot be imported in a bare test env (it pulls in HSM / redis /
# live infra at import time), so these tests validate the *pure* passthrough
# decision logic via reference implementations that mirror gateway.py exactly:
#
#   • _messages_have_image()  — multimodal-turn detection (gateway.py:6498)
#   • _CLAUDE_TOOL_HINTS       — drift-proof Claude hint set (gateway.py:6482)
#   • the tools dispatch rule   — passthrough vs IDE branch + image steering
#                                 (gateway.py:7935-7945)
#
# A DRIFT GUARD at the bottom reads gateway.py as source text and asserts the
# real definitions still match what these tests assume — so if someone changes
# the hint set or the steering rule in gateway.py, this test fails loudly.
# ============================================================

import os
import re

import pytest


GATEWAY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "gateway.py",
)


# ── reference implementations (mirror gateway.py) ───────────────────────────

# gateway.py:6482
_CLAUDE_TOOL_HINTS = frozenset({"claude", "solution", "haiku", "opus-4-6", "opus-4-8"})

# gateway.py:7942 — the IDE dispatch tuple (intentionally omits "opus-4-8")
_IDE_CLAUDE_HINTS = ("claude", "solution", "opus-4-6", "haiku")


class _Msg:
    """Stand-in for _OAIMessage — only `.content` is read by _messages_have_image."""
    def __init__(self, content):
        self.content = content


def _messages_have_image(msgs) -> bool:
    """Mirror of gateway.py:6498."""
    for m in msgs:
        if isinstance(m.content, list):
            if any(isinstance(p, dict) and p.get("type") == "image_url" for p in m.content):
                return True
    return False


def _resolve_use_claude(model_hint, passthrough, has_image):
    """Mirror of the oai_stream tools-dispatch branch (gateway.py:7935-7945)."""
    if passthrough:
        use_claude = model_hint in _CLAUDE_TOOL_HINTS
        force_proxy_for_image = has_image
        if force_proxy_for_image:
            use_claude = False
    else:
        use_claude = model_hint in _IDE_CLAUDE_HINTS
    return use_claude


# ── _messages_have_image ────────────────────────────────────────────────────

def test_no_image_plain_string():
    msgs = [_Msg("hello"), _Msg("world")]
    assert _messages_have_image(msgs) is False


def test_no_image_text_only_list():
    msgs = [_Msg([{"type": "text", "text": "describe this"}])]
    assert _messages_have_image(msgs) is False


def test_detects_image_url_part():
    msgs = [_Msg([
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ])]
    assert _messages_have_image(msgs) is True


def test_detects_image_in_any_message():
    msgs = [
        _Msg("system-ish"),
        _Msg([{"type": "text", "text": "t"}]),
        _Msg([{"type": "image_url", "image_url": {"url": "x"}}]),
    ]
    assert _messages_have_image(msgs) is True


def test_ignores_non_dict_list_parts():
    msgs = [_Msg(["just", "strings"])]
    assert _messages_have_image(msgs) is False


def test_none_content_is_safe():
    msgs = [_Msg(None)]
    assert _messages_have_image(msgs) is False


# ── Claude hint set ─────────────────────────────────────────────────────────

def test_opus_48_is_in_passthrough_set():
    """The whole point of B2: opus-4-8 must route to Claude on the passthrough lane."""
    assert "opus-4-8" in _CLAUDE_TOOL_HINTS


def test_opus_48_is_absent_from_ide_tuple():
    """IDE tuple is intentionally left unchanged (scope decision)."""
    assert "opus-4-8" not in _IDE_CLAUDE_HINTS


def test_passthrough_set_superset_of_ide_tuple():
    assert set(_IDE_CLAUDE_HINTS).issubset(_CLAUDE_TOOL_HINTS)


# ── dispatch decision ───────────────────────────────────────────────────────

@pytest.mark.parametrize("hint", ["claude", "solution", "haiku", "opus-4-6", "opus-4-8"])
def test_passthrough_text_turn_routes_claude_hints_to_claude(hint):
    assert _resolve_use_claude(hint, passthrough=True, has_image=False) is True


def test_passthrough_non_claude_hint_uses_proxy():
    assert _resolve_use_claude("gpt-4o", passthrough=True, has_image=False) is False


@pytest.mark.parametrize("hint", ["claude", "opus-4-8", "haiku"])
def test_passthrough_image_turn_forces_proxy_even_for_claude_hint(hint):
    """Image turns must NOT go to the Claude stream (it drops image_url parts)."""
    assert _resolve_use_claude(hint, passthrough=True, has_image=True) is False


def test_ide_opus_48_falls_through_to_proxy():
    """Regression: IDE behavior is byte-identical — opus-4-8 still hits the proxy."""
    assert _resolve_use_claude("opus-4-8", passthrough=False, has_image=False) is False


def test_ide_image_flag_never_forces_proxy_off_claude():
    """has_image is a no-op on the IDE branch (force_proxy_for_image only true under passthrough)."""
    assert _resolve_use_claude("claude", passthrough=False, has_image=True) is True


# ── DRIFT GUARD — keep reference impls in sync with gateway.py ───────────────

@pytest.fixture(scope="module")
def gateway_src():
    with open(GATEWAY_PATH, "r", encoding="utf-8") as f:
        return f.read()


def test_gateway_claude_hint_set_matches_reference(gateway_src):
    m = re.search(r"_CLAUDE_TOOL_HINTS\s*=\s*frozenset\(\{([^}]*)\}\)", gateway_src)
    assert m, "could not find _CLAUDE_TOOL_HINTS in gateway.py"
    literal = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert literal == set(_CLAUDE_TOOL_HINTS), (
        "gateway._CLAUDE_TOOL_HINTS drifted from the test reference — "
        "update tests/agents/test_gateway_passthrough_logic.py"
    )


def test_gateway_ide_dispatch_tuple_unchanged(gateway_src):
    m = re.search(
        r'_use_claude\s*=\s*_model_hint\s+in\s*\(([^)]*)\)', gateway_src
    )
    assert m, "could not find IDE dispatch tuple in gateway.py"
    literal = tuple(re.findall(r'"([^"]+)"', m.group(1)))
    assert literal == _IDE_CLAUDE_HINTS, (
        "IDE dispatch tuple changed — it is meant to stay byte-identical "
        "(opus-4-8 fix is passthrough-only)"
    )


def test_gateway_force_proxy_for_image_definition_present(gateway_src):
    assert "_force_proxy_for_image = _passthrough and _messages_have_image(req.messages)" in gateway_src


def test_gateway_skips_session_compression_on_passthrough(gateway_src):
    # compress_ide_messages must be guarded by `if not _passthrough:`
    assert re.search(
        r"if not _passthrough:\s*\n(?:.*\n)*?\s*oai_messages = compress_ide_messages\(",
        gateway_src,
    ), "session compression must be gated behind `if not _passthrough:`"
