# SPDX-License-Identifier: Apache-2.0
# ============================================================
# FR-T0-3 — durable replay plumbing
#
#   * CheckpointStore base defines run_steps / run_events methods as
#     safe no-ops (backends without durability just drop the writes).
#   * FileCheckpointStore inherits those no-ops without error.
#   * _ExecState.step_index survives snapshot round-trip so the durable
#     step counter stays monotonic across HITL pause/resume.
#
# Skipped cleanly if ABStudio backend deps are unavailable.
# ============================================================

import asyncio
import os
import sys

import pytest

_ABS_BACKEND = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "ABStudio", "backend")
)
if _ABS_BACKEND not in sys.path:
    sys.path.insert(0, _ABS_BACKEND)

store_mod = pytest.importorskip(
    "app.checkpoint.store",
    reason="ABStudio backend deps not installed",
)
engine_mod = pytest.importorskip(
    "app.engine.native_engine",
    reason="ABStudio backend deps not installed",
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── base no-op contract (REQ-D1/D2) ───────────────────────────────────────

def test_base_store_defines_durable_methods():
    base = store_mod.CheckpointStore
    for name in ("save_run_step", "load_run_state", "append_run_event", "replay_events"):
        assert hasattr(base, name), f"CheckpointStore missing {name}"


def test_file_store_durable_noops_do_not_raise(tmp_path):
    fs = store_mod.FileCheckpointStore(path=str(tmp_path / "chat.json"))
    # No-op save should not raise and load should return an empty list.
    _run(fs.save_run_step(
        "thread-1", "wf-1", 0, "node-a", "agent", "running",
        input_snapshot={"current_input": "hi"},
    ))
    _run(fs.append_run_event("thread-1", "wf-1", "node_running", {"node_id": "node-a"}, 0))
    assert _run(fs.load_run_state("thread-1")) == []
    assert _run(fs.replay_events("thread-1")) == []


# ── step_index snapshot round-trip (REQ-D3/D5) ────────────────────────────

def test_step_index_survives_snapshot_round_trip():
    st = engine_mod._ExecState(current_input="the input")
    st.step_index = 7
    d = engine_mod._state_to_dict(st)
    assert d["step_index"] == 7
    st2 = engine_mod._state_from_dict(d)
    assert st2.step_index == 7
    assert st2.current_input == "the input"


def test_step_index_defaults_to_zero_when_absent():
    # Older snapshots (pre-T0-3) have no step_index key — must default to 0.
    st = engine_mod._state_from_dict({"current_input": "x"})
    assert st.step_index == 0
