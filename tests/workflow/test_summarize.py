# SPDX-License-Identifier: MIT
# ============================================================
# P13 (W3) — staged summarization pipeline planner (pure)
# ============================================================

from workflow.summarize import (
    FORMAT,
    MAP,
    REDUCE,
    SEGMENT,
    STAGES,
    next_stage,
    plan_summarize,
    segment_text,
)


def test_segment_packs_paragraphs_to_budget():
    text = "\n\n".join([f"paragraph number {i} with some words here" for i in range(6)])
    segs = segment_text(text, max_seg_tokens=20)
    assert len(segs) >= 2
    assert all(s.text for s in segs)
    # ids are sequential
    assert [s.id for s in segs] == list(range(len(segs)))


def test_plan_single_reduce_for_few_segments():
    text = "\n\n".join([f"para {i}" for i in range(3)])
    plan = plan_summarize(text, max_seg_tokens=5)
    assert plan.map_calls == len(plan.segments)
    assert plan.hierarchical is False
    assert plan.reduce_passes == 1


def test_plan_hierarchical_for_many_segments():
    # force many tiny segments (each paragraph its own segment)
    text = "\n\n".join([f"paragraph {i} content" for i in range(20)])
    plan = plan_summarize(text, max_seg_tokens=1)
    assert plan.map_calls == len(plan.segments)
    assert plan.hierarchical is True
    assert plan.reduce_passes >= 2   # tree reduce


def test_output_format_validated():
    assert plan_summarize("a\n\nb", output_format="bullets").output_format == "bullets"
    assert plan_summarize("a\n\nb", output_format="weird").output_format == "prose"


def test_stage_sequence():
    assert next_stage(None) == SEGMENT
    assert next_stage(SEGMENT) == MAP
    assert next_stage(MAP) == REDUCE
    assert next_stage(REDUCE) == FORMAT
    assert next_stage(FORMAT) is None
    assert next_stage("bogus") == STAGES[0]


def test_empty_text_safe():
    plan = plan_summarize("")
    assert plan.map_calls == len(plan.segments)
    assert plan.reduce_passes >= 1
