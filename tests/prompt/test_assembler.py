# SPDX-License-Identifier: MIT
# ============================================================
# P9 — prompt assembler (pure)
# ============================================================

from prompt.assembler import STABLE, VOLATILE, assemble


def test_canonical_stable_before_volatile():
    cp = assemble({
        "task": "answer this",
        "system": "you are helpful",
        "evidence": "some context",
        "durable_memory": "user prefers concise",
    })
    names = [s.name for s in cp.slots]
    # system + durable_memory (stable) must precede evidence + task (volatile)
    assert names.index("system") < names.index("evidence")
    assert names.index("durable_memory") < names.index("task")


def test_cache_boundary_marks_stable_prefix():
    cp = assemble({
        "system": "sys",
        "safety": "safe",
        "evidence": "ev",
        "task": "q",
    })
    prefix = [s.name for s in cp.stable_prefix()]
    # system + safety are the cacheable prefix; evidence/task are not
    assert prefix == ["system", "safety"]
    assert all(cp.slots[i].stability == STABLE for i in range(cp.cache_boundary_index))


def test_empty_slots_skipped():
    cp = assemble({"system": "", "task": "just this"})
    assert [s.name for s in cp.slots] == ["task"]


def test_budget_trims_lowest_priority_droppable_first():
    parts = {
        "system": "s" * 400,        # ~100 tok, non-droppable
        "evidence": "e" * 4000,     # ~1000 tok, droppable, priority 70
        "conversation": "c" * 4000, # ~1000 tok, droppable, priority 60
        "task": "t" * 400,          # ~100 tok, non-droppable
    }
    cp = assemble(parts, budget_tokens=1200)
    # conversation (lowest priority droppable) dropped before evidence
    assert "conversation" in cp.slots_dropped
    assert "system" not in cp.slots_dropped  # never drop non-droppable
    assert "task" not in cp.slots_dropped


def test_non_droppable_never_dropped_even_over_budget():
    parts = {"system": "s" * 8000, "task": "t" * 8000}  # both non-droppable
    cp = assemble(parts, budget_tokens=100)
    assert cp.slots_dropped == []  # can't drop either
    assert {s.name for s in cp.slots} == {"system", "task"}


def test_evidence_chunk_ids_carried():
    cp = assemble({"task": "q"}, evidence_chunk_ids=["c1", "c2"])
    assert cp.evidence_chunk_ids == ["c1", "c2"]
    assert cp.as_dict()["evidence_chunk_ids"] == ["c1", "c2"]


def test_never_raises_on_garbage():
    cp = assemble(None)  # type: ignore[arg-type]
    assert cp is not None
    cp2 = assemble({"task": None})  # type: ignore[dict-item]
    assert cp2 is not None


def test_volatile_breaks_cache_prefix():
    # if a volatile slot sneaks before a stable one (shouldn't via spec), the
    # boundary logic must not count the later stable slot. Here normal order:
    cp = assemble({"system": "s", "evidence": "e", "instructions": "i"})
    # instructions is stable but comes AFTER evidence (volatile) in output order?
    # No — canonical order puts instructions(80,stable) before evidence(70,volatile).
    names = [s.name for s in cp.slots]
    assert names.index("instructions") < names.index("evidence")
