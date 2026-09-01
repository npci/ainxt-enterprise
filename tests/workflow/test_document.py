# SPDX-License-Identifier: Apache-2.0
# ============================================================
# P15 — staged document pipeline: state + planner (pure)
# ============================================================

from workflow.document import (
    CONSISTENCY,
    DRAFT,
    OUTLINE,
    RENDER,
    STAGES,
    STYLE,
    DocumentState,
    Section,
    consistency_issues,
    draft_order,
    next_stage,
)


def test_figure_reused_verbatim_prevents_drift():
    st = DocumentState(doc_id="d1")
    first = st.set_figure("uptime", "99.95%")
    # a later section tries to set a different value — canonical wins
    second = st.set_figure("uptime", "99.9%")
    assert first == "99.95%"
    assert second == "99.95%"
    assert st.figures["uptime"] == "99.95%"


def test_draft_order_respects_dependencies():
    st = DocumentState(outline=[
        Section(id="conclusion", title="Conclusion", depends_on=["body"]),
        Section(id="body", title="Body", depends_on=["intro"]),
        Section(id="intro", title="Intro"),
    ])
    order = draft_order(st)
    assert order.index("intro") < order.index("body") < order.index("conclusion")


def test_draft_order_cycle_falls_back_to_declared_order():
    st = DocumentState(outline=[
        Section(id="a", title="A", depends_on=["b"]),
        Section(id="b", title="B", depends_on=["a"]),
    ])
    order = draft_order(st)
    # never raises; all sections present
    assert set(order) == {"a", "b"}


def test_consistency_flags_possible_drift():
    st = DocumentState()
    st.set_figure("uptime", "99.95%")
    texts = {
        "s1": "We guarantee 99.95% uptime.",     # ok — canonical present
        "s2": "Our uptime target is 99.9 percent.",  # mentions key, canonical absent
    }
    issues = consistency_issues(st, texts)
    assert any("s2" in i for i in issues)
    assert not any("s1" in i for i in issues)


def test_consistency_empty_inputs_safe():
    assert consistency_issues(DocumentState(), {}) == []
    assert consistency_issues(DocumentState(), None) == []


def test_next_stage_sequence():
    assert next_stage(None) == OUTLINE
    assert next_stage(OUTLINE) == DRAFT
    assert next_stage(DRAFT) == CONSISTENCY
    assert next_stage(CONSISTENCY) == STYLE
    assert next_stage(STYLE) == RENDER
    assert next_stage(RENDER) is None


def test_next_stage_unknown_resets_to_first():
    assert next_stage("bogus") == STAGES[0]


def test_as_dict_snapshot():
    st = DocumentState(doc_id="d9", intent="report")
    st.set_figure("x", "1")
    d = st.as_dict()
    assert d["doc_id"] == "d9"
    assert d["intent"] == "report"
    assert d["figures"] == {"x": "1"}
