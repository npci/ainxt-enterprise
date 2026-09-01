# SPDX-License-Identifier: Apache-2.0
# ============================================================
# FR-T0-1 regression — a BLOCKING compliance/injection gate must HALT the run.
#
# Guards the bug where _run_agent returned on a block but did not set an
# abort flag, so _traverse silently advanced to downstream nodes (running
# them on blocked input). The fix: the block sets state.aborted, and
# _traverse / execute() / the parallel-branch merge all bail on it.
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

engine_mod = pytest.importorskip(
    "app.engine.native_engine",
    reason="ABStudio backend deps not installed",
)
iface = pytest.importorskip("app.engine.interface")


def _collect(agen):
    async def _run():
        return [ev async for ev in agen]
    return asyncio.new_event_loop().run_until_complete(_run())


def _build_two_agent_ctx():
    """agentA → agentB → end (linear)."""
    nodes = {
        "agentA": {"type": "agent", "data": {"name": "A"}},
        "agentB": {"type": "agent", "data": {"name": "B"}},
        "end":    {"type": "end", "data": {}},
    }
    gctx = engine_mod._GraphCtx(
        start_id="agentA",
        end_id="end",
        nodes_by_id=nodes,
        outgoing={"agentA": ["agentB"], "agentB": ["end"]},
        incoming={"agentB": ["agentA"], "end": ["agentB"]},
        condition_edges={},
        fan_out_nodes=set(),
        fan_in_nodes=set(),
        parallel_agents=set(),
        tools_map={},
        final_agent_ids={"agentB"},
    )
    return gctx


def test_compliance_block_halts_traversal(monkeypatch):
    eng = engine_mod.NativeEngine()   # _store is None → _durable_step no-ops
    gctx = _build_two_agent_ctx()
    state = engine_mod._ExecState(current_input="4111 1111 1111 1111")  # PAN-like
    ctx = iface.ExecutionContext(thread_id="t1", workflow_id="wf1")

    ran: list[str] = []

    async def fake_run_agent(node_id, node, st, gc, thread_id, context, **kw):
        ran.append(node_id)
        if node_id == "agentA":
            # Simulate a BLOCKING compliance gate: emit error + abort the run.
            st.aborted = True
            yield engine_mod.make_sse("error", {"compliance_blocked": True, "node_id": node_id})
            return
        # agentB would produce normal output if it ever ran.
        st.current_input = "downstream output"
        yield engine_mod.make_sse("agent_complete", {"node_id": node_id, "output": "x"})

    monkeypatch.setattr(eng, "_run_agent", fake_run_agent)

    events = _collect(eng._traverse("agentA", state, gctx, "t1", ctx))

    # agentA ran; agentB (downstream) must NOT have run.
    assert ran == ["agentA"], f"downstream node ran after block: {ran}"
    assert state.aborted is True
    # The error frame was surfaced; no agent_complete for agentB.
    assert any("compliance_blocked" in e for e in events)
    assert not any("downstream output" in e for e in events)


def test_clean_input_runs_all_nodes(monkeypatch):
    """Control: without an abort, traversal proceeds through both nodes."""
    eng = engine_mod.NativeEngine()
    gctx = _build_two_agent_ctx()
    state = engine_mod._ExecState(current_input="hello")
    ctx = iface.ExecutionContext(thread_id="t2", workflow_id="wf2")

    ran: list[str] = []

    async def fake_run_agent(node_id, node, st, gc, thread_id, context, **kw):
        ran.append(node_id)
        st.current_input = f"{node_id} output"
        yield engine_mod.make_sse("agent_complete", {"node_id": node_id})

    monkeypatch.setattr(eng, "_run_agent", fake_run_agent)
    _collect(eng._traverse("agentA", state, gctx, "t2", ctx))

    assert ran == ["agentA", "agentB"]
    assert state.aborted is False
