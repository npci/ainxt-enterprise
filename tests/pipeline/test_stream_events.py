# SPDX-License-Identifier: Apache-2.0
# ============================================================
# P16/P12 — structured stream event builders (pure)
# ============================================================

from pipeline.stream_events import (
    ERROR,
    PROGRESS,
    RESULT,
    START,
    ReasoningMarker,
    ToolMarker,
    group_read_only,
    plan_event,
    reasoning_event,
    tool_event,
)


def test_tool_start_event_shape():
    e = tool_event("t1", "kb_search", START, args={"q": "retry"})
    assert e == {"tool": {"id": "t1", "name": "kb_search", "phase": "start", "args": {"q": "retry"}}}


def test_tool_result_and_error():
    r = tool_event("t1", "kb_search", RESULT, summary="12 hits", ok=True)
    assert r["tool"]["phase"] == "result" and r["tool"]["ok"] is True and r["tool"]["summary"] == "12 hits"
    er = tool_event("t2", "gitlab", ERROR, detail="timeout, retry 2/3", ok=False)
    assert er["tool"]["phase"] == "error" and er["tool"]["ok"] is False


def test_invalid_phase_coerced():
    e = tool_event("t1", "x", "bogus")
    assert e["tool"]["phase"] == PROGRESS


def test_compact_payload_omits_empty():
    e = tool_event("t1", "x", START)
    assert "args" not in e["tool"] and "summary" not in e["tool"] and "ok" not in e["tool"]


def test_reasoning_event():
    assert reasoning_event("thinking...") == {"reasoning": {"delta": "thinking..."}}
    assert reasoning_event("") == {"reasoning": {"delta": ""}}


def test_group_collapses_consecutive_readonly_results():
    events = [
        tool_event("1", "kb_search", RESULT, ok=True),
        tool_event("2", "confluence", RESULT, ok=True),
        tool_event("3", "gitlab_write", RESULT, ok=False),  # not ok → breaks group
    ]
    grouped = group_read_only(events)
    # first two collapse into a tool_group; the third stays separate
    assert grouped[0].get("tool_group", {}).get("count") == 2
    assert grouped[1]["tool"]["name"] == "gitlab_write"


def test_group_single_not_collapsed():
    events = [tool_event("1", "kb_search", RESULT, ok=True)]
    assert group_read_only(events) == events


def test_group_passthrough_non_results():
    events = [tool_event("1", "kb_search", START)]
    assert group_read_only(events) == events


def test_group_empty():
    assert group_read_only([]) == []


# ── Phase 5: plan panel + tool marker ────────────────────────────────────────
def test_plan_event_shape():
    e = plan_event("decompose", reason="deep/multi-signal")
    assert e == {"plan": {"shape": "decompose", "reason": "deep/multi-signal"}}


def test_plan_event_with_steps():
    e = plan_event("tool_use", steps=["search kb", "  ", "synthesize"])
    # blank steps are filtered
    assert e["plan"]["steps"] == ["search kb", "synthesize"]


def test_plan_event_defaults():
    e = plan_event("")
    assert e["plan"]["shape"] == "direct"  # empty coerced to a safe default
    assert "steps" not in e["plan"] and "reason" not in e["plan"]


def test_tool_marker_str_is_empty():
    # A non-aware consumer must be able to str()-coerce a marker without
    # polluting the answer text.
    m = ToolMarker(tool_id="r-1", name="retrieve", phase="start")
    assert str(m) == ""


def test_tool_marker_to_event():
    m = ToolMarker(tool_id="r-1", name="retrieve", phase="result", ok=True,
                   summary="Found 3 sources")
    e = m.to_event()
    assert e["tool"]["id"] == "r-1"
    assert e["tool"]["name"] == "retrieve"
    assert e["tool"]["phase"] == "result"
    assert e["tool"]["ok"] is True
    assert e["tool"]["summary"] == "Found 3 sources"


def test_tool_marker_does_not_pollute_answer():
    m = ToolMarker(tool_id="r-1", name="retrieve", phase="start")
    full = ""
    for tok in ["Hello ", m, "world"]:
        full += tok if isinstance(tok, str) else str(tok)
    assert full == "Hello world"


# ── Gap #2: live reasoning deltas ────────────────────────────────────────────
def test_reasoning_marker_str_is_empty():
    m = ReasoningMarker(delta="deliberating about the budget")
    assert str(m) == ""


def test_reasoning_marker_to_event():
    m = ReasoningMarker(delta="step 1: check the numbers")
    assert m.to_event() == {"reasoning": {"delta": "step 1: check the numbers"}}


def test_reasoning_marker_does_not_pollute_answer():
    m = ReasoningMarker(delta="hidden thinking")
    full = ""
    for tok in ["Visible ", m, "answer"]:
        full += tok if isinstance(tok, str) else str(tok)
    assert full == "Visible answer"
