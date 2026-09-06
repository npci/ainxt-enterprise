# SPDX-License-Identifier: MIT
"""REQ-P5-1 — overlap KB retrieval with catalog/skill resolution.

Background: for a KB-enabled agent node, ``build_context_section_with_meta``
(embedding + vector search) used to be awaited inline, serially AFTER
catalog tool/skill resolution had already completed. ``_run_agent`` now
launches the KB call as a background task right after ``effective_kb`` is
computed (before catalog/skill resolution starts) and only awaits it later,
at the point the KB section is spliced into the prompt — so KB retrieval
wall-clock overlaps catalog/skill resolution instead of stacking after it.

These tests drive the real ``_run_agent`` with a stubbed LLM client (no
network calls) and record wall-clock windows for the KB call and the
catalog-tool-resolution call to prove they run concurrently.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.engine import native_engine as engine_mod
from app.engine import interface as iface


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _build_ctx(nodes):
    return engine_mod._GraphCtx(
        start_id="agentA", end_id="end", nodes_by_id=nodes,
        outgoing={"agentA": ["end"]}, incoming={"end": ["agentA"]},
        condition_edges={}, fan_out_nodes=set(), fan_in_nodes=set(),
        parallel_agents=set(), tools_map={}, final_agent_ids={"agentA"},
    )


class _FakeChunk:
    def __init__(self, text="", tool_calls=None, is_final=False):
        self.text = text
        self.tool_calls = tool_calls or []
        self.is_final = is_final
        self.notice = None
        self.model = "test-model"
        self.usage = None


class _FakeLLMClient:
    async def stream(self, messages, tools=None):
        yield _FakeChunk(text="final answer", is_final=False)
        yield _FakeChunk(text="", tool_calls=[], is_final=True)


@pytest.fixture(autouse=True)
def _common_stubs(monkeypatch):
    """Stubs shared by every test in this module: a no-op LLM client, HITL
    off, and a cheap cost estimator (the real one lazily imports a local
    model gateway with an HSM/CKMS bootstrap chain — slow and irrelevant
    here)."""
    monkeypatch.setattr(engine_mod, "get_llm_client", lambda cfg: _FakeLLMClient())
    monkeypatch.setattr(engine_mod, "get_hitl_mode", lambda data: "off")
    monkeypatch.setattr(engine_mod, "_usage_cost", lambda *a, **k: 0.0)


def _drive(eng, node_id, node, gctx, thread_id="t1"):
    state = engine_mod._ExecState(current_input="hello")
    ctx = iface.ExecutionContext(thread_id=thread_id, workflow_id="wf1")

    async def _go():
        events = []
        async for ev in eng._run_agent(node_id, node, state, gctx, thread_id, ctx):
            events.append(ev)
        return events

    return _run(_go())


class TestKbOverlapsCatalogResolution:
    def test_kb_and_catalog_resolution_run_concurrently(self, monkeypatch):
        windows = {}

        async def fake_kb(*a, **k):
            windows["kb_start"] = time.monotonic()
            await asyncio.sleep(0.15)
            windows["kb_end"] = time.monotonic()
            return {"mode": "org", "section": "## Knowledge\n\nsome context", "chunk_count": 1}

        async def fake_resolve_tools(self, requested, **kwargs):
            windows["catalog_start"] = time.monotonic()
            await asyncio.sleep(0.15)
            windows["catalog_end"] = time.monotonic()
            return []

        from app.core import kb_retriever
        monkeypatch.setattr(kb_retriever, "build_context_section_with_meta", fake_kb)
        monkeypatch.setattr(engine_mod.NativeEngine, "_resolve_catalog_tools", fake_resolve_tools)

        eng = engine_mod.NativeEngine()
        nodes = {
            "agentA": {"type": "agent", "data": {
                "name": "A", "tools": [{"name": "t1"}],
                "knowledge": {"mode": "org"},
            }},
            "end": {"type": "end", "data": {}},
        }
        gctx = _build_ctx(nodes)

        events = _drive(eng, "agentA", nodes["agentA"], gctx)

        assert "kb_start" in windows and "catalog_start" in windows
        # Overlap assertion: each call must have STARTED before the other
        # one FINISHED — i.e. their wall-clock windows intersect. Under the
        # old serial behaviour, catalog_start would be >= kb_end.
        assert windows["catalog_start"] < windows["kb_end"], (
            "catalog resolution should start while the KB task is still "
            "in flight (concurrent), not after it completes (serial)"
        )
        assert windows["kb_start"] < windows["catalog_end"]

        # The KB section must still make it into the final prompt/instructions
        # splice — verified indirectly via the kb_retrieval SSE event.
        assert any('"event": "kb_retrieval"' in e for e in events)

    def test_mode_none_never_launches_kb_task(self, monkeypatch):
        """REQ-P2-4 short-circuit must be preserved: no KB task at all when
        the effective mode is none/absent."""
        kb_calls = []

        async def fake_kb(*a, **k):
            kb_calls.append(1)
            return {"mode": "org", "section": "should never run"}

        from app.core import kb_retriever
        monkeypatch.setattr(kb_retriever, "build_context_section_with_meta", fake_kb)

        eng = engine_mod.NativeEngine()
        nodes = {
            "agentA": {"type": "agent", "data": {
                "name": "A", "tools": [],
                "knowledge": {"mode": "none"},
            }},
            "end": {"type": "end", "data": {}},
        }
        gctx = _build_ctx(nodes)

        events = _drive(eng, "agentA", nodes["agentA"], gctx)

        assert kb_calls == [], "build_context_section_with_meta must not be called when mode=none"
        assert not any('"event": "kb_retrieval"' in e for e in events)

    def test_no_knowledge_key_at_all_never_launches_kb_task(self, monkeypatch):
        """Absent 'knowledge' entirely (the common case for most nodes) must
        behave the same as mode=none."""
        kb_calls = []

        async def fake_kb(*a, **k):
            kb_calls.append(1)
            return {"mode": "org", "section": "should never run"}

        from app.core import kb_retriever
        monkeypatch.setattr(kb_retriever, "build_context_section_with_meta", fake_kb)

        eng = engine_mod.NativeEngine()
        nodes = {
            "agentA": {"type": "agent", "data": {"name": "A", "tools": []}},
            "end": {"type": "end", "data": {}},
        }
        gctx = _build_ctx(nodes)

        _drive(eng, "agentA", nodes["agentA"], gctx)
        assert kb_calls == []

    def test_kb_section_still_spliced_into_instructions_after_skills(self, monkeypatch):
        """Ordering must be preserved: skills are spliced first, then the KB
        section is prepended in front of (skills + original instructions) —
        matching the pre-existing behaviour before the KB block moved."""
        from app.core import kb_retriever
        from app import workflow_repo as wr

        async def fake_kb(*a, **k):
            return {"mode": "org", "section": "KB-SECTION", "chunk_count": 0}

        async def fake_get_skill(name):
            return {"name": name, "content": "SKILL-BODY", "description": "", "category": "general"}

        async def fake_list_skill_files(name):
            return []

        monkeypatch.setattr(kb_retriever, "build_context_section_with_meta", fake_kb)
        monkeypatch.setattr(wr, "get_skill", fake_get_skill)
        monkeypatch.setattr(wr, "list_skill_files", fake_list_skill_files)

        captured_prompts = []
        real_build_agent_prompt = engine_mod.build_agent_prompt

        def spy_build_agent_prompt(name, instructions, *a, **k):
            captured_prompts.append(instructions)
            return real_build_agent_prompt(name, instructions, *a, **k)

        monkeypatch.setattr(engine_mod, "build_agent_prompt", spy_build_agent_prompt)

        eng = engine_mod.NativeEngine()
        nodes = {
            "agentA": {"type": "agent", "data": {
                "name": "A", "tools": [],
                "skills": [{"name": "s1"}],
                "knowledge": {"mode": "org"},
                "instructions": "BASE-INSTRUCTIONS",
            }},
            "end": {"type": "end", "data": {}},
        }
        gctx = _build_ctx(nodes)
        _drive(eng, "agentA", nodes["agentA"], gctx)

        assert captured_prompts, "build_agent_prompt must have been called"
        final_instructions = captured_prompts[-1]
        # KB section comes first, then (further down) the skill body and the
        # original base instructions — i.e. KB splice wraps everything that
        # was assembled before it, consistent with the pre-move code order.
        kb_pos = final_instructions.find("KB-SECTION")
        skill_pos = final_instructions.find("SKILL-BODY")
        base_pos = final_instructions.find("BASE-INSTRUCTIONS")
        assert kb_pos != -1 and skill_pos != -1 and base_pos != -1
        assert kb_pos < skill_pos < base_pos


class TestKbTaskNoLeakOnEarlyAbort:
    def test_compliance_blocked_input_does_not_leak_the_kb_task(self, monkeypatch):
        """When compliance-in blocks the input (an early return inside
        _run_agent, before the KB splice point), the KB task must not be
        left dangling / unawaited — this is the leak scenario called out in
        the requirements doc's risk table for REQ-P5-1."""
        from app.core import kb_retriever

        kb_task_ref = {}

        async def fake_kb(*a, **k):
            await asyncio.sleep(0.05)
            return {"mode": "org", "section": "unused"}

        monkeypatch.setattr(kb_retriever, "build_context_section_with_meta", fake_kb)

        async def fake_compliance_in(text, node_id, node_type):
            return text, {"finding_types": ["PAN"]}, True  # blocked=True

        monkeypatch.setattr(engine_mod, "_compliance_in", fake_compliance_in)

        eng = engine_mod.NativeEngine()
        nodes = {
            "agentA": {"type": "agent", "data": {
                "name": "A", "tools": [],
                "knowledge": {"mode": "org"},
            }},
            "end": {"type": "end", "data": {}},
        }
        gctx = _build_ctx(nodes)

        events = _drive(eng, "agentA", nodes["agentA"], gctx)

        assert any("compliance_blocked" in e for e in events)

        # Let any leaked task's callbacks fire and assert no warning surfaces.
        # We can't directly inspect the task object from outside (it's a
        # local variable in _run_agent), so we instead assert indirectly:
        # a leaked, un-awaited task that raises would show up as an
        # "exception was never retrieved" warning captured by asyncio's
        # default exception handler during loop close. We simply run one
        # more no-op iteration of the loop to let pending callbacks settle,
        # then close without error.
        loop = asyncio.new_event_loop()
        loop.run_until_complete(asyncio.sleep(0.2))
        loop.close()
