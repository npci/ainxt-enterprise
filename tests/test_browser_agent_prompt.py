# SPDX-License-Identifier: Apache-2.0
# ============================================================
# gateway.looks_like_browser_agent_prompt — header-independent
# detection of a browser-automation-agent turn.
#
# WHY THIS TEST EXISTS
# --------------------
# The browser extension is tagged via `X-AiNxt-Client: browser-agent`, but that
# header can be stripped by proxy hops, leaving the request classified as
# `platform`. When that happens the prompt would flow through the IDE plain-chat
# path and get RAG-injected (detect_repo matches a bare repo token like
# 'ainxt-platform' anywhere in the ~28K agent prompt), corrupting/oversizing the
# prompt and triggering an upstream 400 — see
# docs/BROWSER_AGENT_AUTO-MODE_FIX_PLAN.md.
#
# The server-side guard relies on a request-shape heuristic: every browser-agent
# turn embeds a live DOM snapshot with FIXED markers from the extension's prompt
# template ("PAGE SNAPSHOT:" + "INTERACTIVE ELEMENTS:"). This test pins that
# contract so a future edit to the markers (or to the all()-vs-any() logic) can't
# silently re-open the RAG-injection bug.
#
# WHY WE exec() THE SOURCE INSTEAD OF `import gateway`
# ---------------------------------------------------
# gateway.py calls core.ckms.load_at_boot() at import time (gateway.py:30), which
# requires a live HSM connection. That is unavailable in unit-test / CI
# environments, so `import gateway` fails before reaching this pure function.
# We therefore load ONLY the marker constant + function definition from the real
# gateway.py source into an isolated namespace. This still tests the shipped code
# (guarding against template drift) without triggering the boot side effects.
# ============================================================

from __future__ import annotations

import ast
import pathlib

import pytest

_GATEWAY_PATH = pathlib.Path(__file__).resolve().parent.parent / "gateway.py"


def _load_from_gateway_source():
    """Extract `_BROWSER_AGENT_PROMPT_MARKERS` and `looks_like_browser_agent_prompt`
    from the real gateway.py source without importing the module (which would run
    the HSM boot code). Returns the compiled function and marker tuple.
    """
    source = _GATEWAY_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    wanted = {"_BROWSER_AGENT_PROMPT_MARKERS", "looks_like_browser_agent_prompt"}
    selected: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            selected.append(node)
        elif isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if targets & wanted:
                selected.append(node)

    assert len(selected) == 2, (
        "Expected to find both _BROWSER_AGENT_PROMPT_MARKERS and "
        f"looks_like_browser_agent_prompt in gateway.py; found {len(selected)}. "
        "Did they get renamed/moved?"
    )

    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict = {}
    exec(compile(module, str(_GATEWAY_PATH), "exec"), namespace)  # noqa: S102
    return namespace["looks_like_browser_agent_prompt"], namespace["_BROWSER_AGENT_PROMPT_MARKERS"]


looks_like_browser_agent_prompt, MARKERS = _load_from_gateway_source()


# A realistic (trimmed) browser-agent Auto-mode turn: both markers present.
_BROWSER_AGENT_TURN = """You are an autonomous browser agent.

CURRENT URL: https://<YOUR_GITLAB_URL>/ainxt/ainxt-platform

PAGE SNAPSHOT:
<title>ainxt-platform · GitLab</title>
<h1>ainxt-platform</h1>

INTERACTIVE ELEMENTS:
[1] <button>Clone</button>
[2] <a href="/ainxt/ainxt-platform/-/tree/main">main</a>

Decide the next action as JSON.
"""

# A genuine IDE / platform codebase question that mentions the repo name but has
# no live DOM snapshot — must NOT be treated as browser-agent traffic.
_IDE_CODEBASE_QUESTION = (
    "In the ainxt-platform repo, where does gateway.py build the "
    "AGENT_SYSTEM_PROMPT and how is detect_repo used? Explain the passthrough lane."
)


def test_markers_are_the_documented_pair():
    """Guard against silent template drift: the markers must stay exactly the pair
    the extension emits. If this changes intentionally, update the extension side
    and this assertion together."""
    assert MARKERS == ("PAGE SNAPSHOT:", "INTERACTIVE ELEMENTS:")


def test_both_markers_present_is_browser_agent():
    assert looks_like_browser_agent_prompt(_BROWSER_AGENT_TURN) is True


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Here is the PAGE SNAPSHOT: of the page.", id="only-first-marker"),
        pytest.param("INTERACTIVE ELEMENTS: [1] button", id="only-second-marker"),
        pytest.param(_IDE_CODEBASE_QUESTION, id="ide-codebase-question"),
        pytest.param("Summarise this article about UPI settlement.", id="plain-chat"),
    ],
)
def test_missing_a_marker_is_not_browser_agent(text):
    """A single marker (or none) must NOT trip the heuristic — otherwise a
    platform-web user could suppress RAG by quoting one marker verbatim, and IDE
    codebase questions would be misclassified."""
    assert looks_like_browser_agent_prompt(text) is False


@pytest.mark.parametrize("text", ["", None])
def test_empty_or_none_is_not_browser_agent(text):
    assert looks_like_browser_agent_prompt(text) is False


def test_case_sensitive_markers_do_not_match_lowercased():
    """Markers are matched case-sensitively (they come from a fixed template), so a
    lowercased/reworded prompt must not accidentally match."""
    lowered = _BROWSER_AGENT_TURN.lower()
    assert looks_like_browser_agent_prompt(lowered) is False
