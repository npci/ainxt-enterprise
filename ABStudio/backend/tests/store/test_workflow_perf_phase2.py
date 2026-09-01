# SPDX-License-Identifier: Apache-2.0
"""Phase 2 workflow-engine perf requirements (ABSTUDIO_WORKFLOW_CATALOG_CACHE
_PERF_REQUIREMENTS.md):

  REQ-P3-2  Per-run resolution cache on ``_GraphCtx`` — a node re-entered
            inside a loop reuses the tools/skills it resolved the first time
            instead of re-hitting ``workflow_repo``.
  REQ-P4-1  ``_resolve_catalog_skills`` issues one concurrent wave of
            ``get_skill``/``list_skill_files`` calls per skill instead of
            two serial waves.
  REQ-P6-1  A tool's JSON payload is parsed at most once in the dispatch
            path (parse-once → shorten-for-LLM reuses the parsed object).
  REQ-P7-1  ``_CatalogTool.to_function_spec()`` memoizes the JSON-schema
            derivation instead of recomputing it on every call.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.engine import native_engine as engine_mod
from app.engine import interface as iface


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# REQ-P3-2 — per-run resolution cache on _GraphCtx
# ---------------------------------------------------------------------------

class TestPerRunResolutionCache:
    def _build_single_agent_ctx(self):
        nodes = {
            "agentA": {"type": "agent", "data": {"name": "A", "tools": [{"name": "t1"}]}},
            "end":    {"type": "end", "data": {}},
        }
        return engine_mod._GraphCtx(
            start_id="agentA",
            end_id="end",
            nodes_by_id=nodes,
            outgoing={"agentA": ["end"]},
            incoming={"end": ["agentA"]},
            condition_edges={},
            fan_out_nodes=set(),
            fan_in_nodes=set(),
            parallel_agents=set(),
            tools_map={},
            final_agent_ids={"agentA"},
        )

    def test_gctx_has_resolution_cache_fields(self):
        gctx = self._build_single_agent_ctx()
        assert gctx.resolved_tools_cache == {}
        assert gctx.resolved_skills_cache == {}

    def test_second_resolution_for_same_node_reuses_cache(self, monkeypatch):
        """Simulates the exact guard added to _run_agent: the second lookup
        for the same node_id must not call _resolve_catalog_tools again and
        must return the identical (cached) list object."""
        eng = engine_mod.NativeEngine()
        gctx = self._build_single_agent_ctx()

        calls = []

        async def fake_resolve(requested, **kwargs):
            calls.append(requested)
            return [f"tool-for-{requested}"]

        monkeypatch.setattr(eng, "_resolve_catalog_tools", fake_resolve)

        async def _lookup(node_id, requested):
            if node_id in gctx.resolved_tools_cache:
                return gctx.resolved_tools_cache[node_id]
            result = await eng._resolve_catalog_tools(requested)
            gctx.resolved_tools_cache[node_id] = result
            return result

        first = _run(_lookup("agentA", [{"name": "t1"}]))
        second = _run(_lookup("agentA", [{"name": "t1"}]))
        third = _run(_lookup("agentA", [{"name": "t1"}]))

        assert len(calls) == 1, "resolution must only run once for a repeated node_id"
        assert second is first
        assert third is first

    def test_different_node_ids_each_resolve_once(self, monkeypatch):
        eng = engine_mod.NativeEngine()
        gctx = self._build_single_agent_ctx()
        calls = []

        async def fake_resolve(requested, **kwargs):
            calls.append(requested)
            return [f"tool-{requested}"]

        monkeypatch.setattr(eng, "_resolve_catalog_tools", fake_resolve)

        async def _lookup(node_id, requested):
            if node_id in gctx.resolved_tools_cache:
                return gctx.resolved_tools_cache[node_id]
            result = await eng._resolve_catalog_tools(requested)
            gctx.resolved_tools_cache[node_id] = result
            return result

        _run(_lookup("agentA", [{"name": "t1"}]))
        _run(_lookup("agentB", [{"name": "t2"}]))
        _run(_lookup("agentA", [{"name": "t1"}]))
        _run(_lookup("agentB", [{"name": "t2"}]))

        assert len(calls) == 2, "each distinct node_id resolves exactly once"

    def test_loop_reentry_via_run_agent_resolves_tools_once(self, monkeypatch):
        """End-to-end: drive the real _run_agent 4 times for the SAME node_id
        (simulating 4 loop iterations) against a stubbed LLM that returns
        immediately with no tool calls, and assert workflow_repo.get_tool is
        hit at most once across all 4 iterations for the node's tool."""
        from app import workflow_repo as wr

        get_tool_calls = []

        async def fake_get_tool(name):
            get_tool_calls.append(name)
            return {
                "name": name, "description": "d", "input_schema": {},
                "code": "c", "generated": True, "service": "", "created_at": None, "updated_at": None,
            }

        monkeypatch.setattr(wr, "get_tool", fake_get_tool)
        monkeypatch.setattr(engine_mod, "get_hitl_mode", lambda data: "off")
        # Avoid the real cost estimator, which lazily imports the local-model
        # gateway (and, transitively, an HSM/CKMS bootstrap chain) — slow and
        # irrelevant to what this test verifies (catalog cache reuse).
        monkeypatch.setattr(engine_mod, "_usage_cost", lambda *a, **k: 0.0)

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

        monkeypatch.setattr(engine_mod, "get_llm_client", lambda cfg: _FakeLLMClient())

        # Avoid the _CatalogTool constructor's real ToolDispatcher import/
        # dependency chain — stub _resolve_catalog_tools' underlying model
        # by monkeypatching _CatalogTool itself to a lightweight stand-in
        # that just records name/schema and exposes to_function_spec/call.
        class _StubCatalogTool:
            def __init__(self, name, description, input_schema, **kwargs):
                self.name = name
                self.description = description
                self._input_schema = input_schema

            def to_function_spec(self):
                return {"name": self.name, "description": self.description, "parameters": {}}

            async def call(self, arguments):
                return "{}"

        monkeypatch.setattr(engine_mod, "_CatalogTool", _StubCatalogTool)

        eng = engine_mod.NativeEngine()
        nodes = {
            "agentA": {"type": "agent", "data": {"name": "A", "tools": [{"name": "t1"}]}},
            "end":    {"type": "end", "data": {}},
        }
        gctx = engine_mod._GraphCtx(
            start_id="agentA", end_id="end", nodes_by_id=nodes,
            outgoing={"agentA": ["end"]}, incoming={"end": ["agentA"]},
            condition_edges={}, fan_out_nodes=set(), fan_in_nodes=set(),
            parallel_agents=set(), tools_map={}, final_agent_ids={"agentA"},
        )
        ctx = iface.ExecutionContext(thread_id="t1", workflow_id="wf1")

        async def _drive_once(i):
            state = engine_mod._ExecState(current_input=f"input {i}")
            events = []
            async for ev in eng._run_agent("agentA", nodes["agentA"], state, gctx, "t1", ctx):
                events.append(ev)
            return events

        for i in range(4):  # 4 "loop iterations" re-entering the same node
            _run(_drive_once(i))

        assert get_tool_calls == ["t1"], (
            f"expected workflow_repo.get_tool('t1') to run exactly once across "
            f"4 re-entries of the same node, got {get_tool_calls}"
        )


# ---------------------------------------------------------------------------
# REQ-P4-1 — single-wave _resolve_catalog_skills
# ---------------------------------------------------------------------------

class TestSingleWaveSkillResolution:
    def test_get_skill_and_list_skill_files_run_concurrently(self, monkeypatch):
        """Old (two-wave) behaviour: list_skill_files for ANY skill would not
        start until get_skill for EVERY skill had finished. New (one-wave)
        behaviour: for a given skill, get_skill and list_skill_files are
        in flight at the same time. We prove this with a barrier: each
        get_skill call waits on an event that only list_skill_files sets,
        and vice versa — if they were sequenced, this would deadlock/timeout."""
        from app import workflow_repo as wr

        get_started = asyncio.Event()
        files_started = asyncio.Event()

        async def fake_get_skill(name):
            get_started.set()
            await asyncio.wait_for(files_started.wait(), timeout=2)
            return {"name": name, "content": "body", "description": "", "category": "general"}

        async def fake_list_skill_files(name):
            files_started.set()
            await asyncio.wait_for(get_started.wait(), timeout=2)
            return [{"rel_path": "a.md"}]

        monkeypatch.setattr(wr, "get_skill", fake_get_skill)
        monkeypatch.setattr(wr, "list_skill_files", fake_list_skill_files)

        eng = engine_mod.NativeEngine()
        result = _run(eng._resolve_catalog_skills([{"name": "s1"}]))

        assert len(result) == 1
        assert result[0]["name"] == "s1"
        assert result[0]["body"] == "body"
        assert result[0]["files"] == [{"rel_path": "a.md"}]

    def test_missing_skill_discards_its_file_list(self, monkeypatch):
        from app import workflow_repo as wr

        async def fake_get_skill(name):
            return None  # skill doesn't exist

        file_calls = []

        async def fake_list_skill_files(name):
            file_calls.append(name)
            return [{"rel_path": "orphan.md"}]

        monkeypatch.setattr(wr, "get_skill", fake_get_skill)
        monkeypatch.setattr(wr, "list_skill_files", fake_list_skill_files)

        eng = engine_mod.NativeEngine()
        result = _run(eng._resolve_catalog_skills([{"name": "ghost"}]))

        assert result == []
        # The file-list call for the missing skill IS issued (both queries
        # are launched together per REQ-P4-1) but its result is discarded.
        assert file_calls == ["ghost"]

    def test_mixed_valid_and_invalid_skills(self, monkeypatch):
        from app import workflow_repo as wr

        async def fake_get_skill(name):
            if name == "real":
                return {"name": "real", "content": "hello", "description": "", "category": "general"}
            return None

        async def fake_list_skill_files(name):
            return [{"rel_path": f"{name}.md"}] if name == "real" else []

        monkeypatch.setattr(wr, "get_skill", fake_get_skill)
        monkeypatch.setattr(wr, "list_skill_files", fake_list_skill_files)

        eng = engine_mod.NativeEngine()
        result = _run(eng._resolve_catalog_skills([{"name": "real"}, {"name": "fake"}]))

        assert len(result) == 1
        assert result[0]["name"] == "real"

    def test_per_skill_exception_is_skipped_not_fatal(self, monkeypatch):
        from app import workflow_repo as wr

        async def fake_get_skill(name):
            if name == "broken":
                raise RuntimeError("boom")
            return {"name": name, "content": "ok", "description": "", "category": "general"}

        async def fake_list_skill_files(name):
            return []

        monkeypatch.setattr(wr, "get_skill", fake_get_skill)
        monkeypatch.setattr(wr, "list_skill_files", fake_list_skill_files)

        eng = engine_mod.NativeEngine()
        result = _run(eng._resolve_catalog_skills([{"name": "broken"}, {"name": "good"}]))

        assert len(result) == 1
        assert result[0]["name"] == "good"


# ---------------------------------------------------------------------------
# REQ-P6-1 — parse each tool payload once
# ---------------------------------------------------------------------------

class TestParseToolPayloadOnce:
    def test_shorten_accepts_raw_string_and_parses_once(self, monkeypatch):
        calls = []
        real_loads = json.loads

        def counting_loads(s, *a, **k):
            calls.append(s)
            return real_loads(s, *a, **k)

        monkeypatch.setattr(engine_mod.json, "loads", counting_loads)

        payload = json.dumps({"message": "hi", "generated_files": []})
        result = engine_mod._shorten_tool_payload_for_llm(payload)

        assert len(calls) == 1
        assert isinstance(result, str)

    def test_shorten_accepts_preparsed_object_without_reparsing(self, monkeypatch):
        calls = []
        real_loads = json.loads

        def counting_loads(s, *a, **k):
            calls.append(s)
            return real_loads(s, *a, **k)

        monkeypatch.setattr(engine_mod.json, "loads", counting_loads)

        parsed = {"message": "hi", "generated_files": []}
        result = engine_mod._shorten_tool_payload_for_llm(parsed)

        assert len(calls) == 0, "a pre-parsed object must not trigger json.loads at all"
        assert isinstance(result, str)

    def test_dispatch_style_sequence_parses_exactly_once(self, monkeypatch):
        """Mirrors the actual dispatch-loop sequence: parse result_str once
        into result_obj (file-collection), then hand THAT object to
        _shorten_tool_payload_for_llm instead of the raw string."""
        calls = []
        real_loads = json.loads

        def counting_loads(s, *a, **k):
            calls.append(s)
            return real_loads(s, *a, **k)

        monkeypatch.setattr(engine_mod.json, "loads", counting_loads)

        result_str = json.dumps({
            "message": "did the thing",
            "generated_files": [{"filename": "x.pdf", "download_url": "/dl/x.pdf"}],
        })

        # Step 1 (file-collection block): parse once.
        result_obj = json.loads(result_str) if isinstance(result_str, str) else result_str
        # Step 2 (shorten-for-LLM): reuse result_obj, no second parse.
        shortened = engine_mod._shorten_tool_payload_for_llm(
            result_obj if result_obj is not None else result_str
        )

        assert len(calls) == 1, f"expected exactly one json.loads call, got {len(calls)}"
        assert isinstance(shortened, str)
        parsed_back = json.loads(shortened)
        assert parsed_back["generated_files"][0]["download_url"] == "/dl/x.pdf"

    def test_shorten_falls_back_to_raw_slice_on_bad_json(self):
        garbage = "not json at all {{{"
        result = engine_mod._shorten_tool_payload_for_llm(garbage, max_chars=10)
        assert result == garbage[:10]

    def test_shorten_handles_none_and_empty_string(self):
        assert engine_mod._shorten_tool_payload_for_llm("") == ""
        assert engine_mod._shorten_tool_payload_for_llm(None) == ""


# ---------------------------------------------------------------------------
# REQ-P7-1 — memoize _CatalogTool.to_function_spec()
# ---------------------------------------------------------------------------

class TestMemoizedFunctionSpec:
    def _make_tool(self, monkeypatch):
        # ToolDispatcher() itself is a cheap no-op constructor (the catalog
        # lives in postgres now — see its __init__), so no stubbing is
        # needed there; only ``_input_schema_to_json_schema`` gets patched
        # per-test to count invocations.
        tool = engine_mod._CatalogTool(
            name="my_tool", description="does things",
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        )
        return tool

    def test_schema_builder_invoked_exactly_once(self, monkeypatch):
        calls = []

        def fake_schema_builder(schema):
            calls.append(schema)
            return {"type": "object", "properties": {"x": {"type": "string"}}}

        import agent_factory.pipeline as pipeline_mod
        monkeypatch.setattr(
            pipeline_mod.ToolDispatcher, "_input_schema_to_json_schema",
            staticmethod(fake_schema_builder),
        )

        tool = self._make_tool(monkeypatch)

        for _ in range(5):
            spec = tool.to_function_spec()
            assert spec["name"] == "my_tool"

        assert len(calls) == 1, f"schema builder should run once, ran {len(calls)} times"

    def test_returned_dict_is_a_copy_each_time(self, monkeypatch):
        import agent_factory.pipeline as pipeline_mod
        monkeypatch.setattr(
            pipeline_mod.ToolDispatcher, "_input_schema_to_json_schema",
            staticmethod(lambda schema: {"type": "object"}),
        )
        tool = self._make_tool(monkeypatch)

        spec1 = tool.to_function_spec()
        spec2 = tool.to_function_spec()

        assert spec1 == spec2
        assert spec1 is not spec2, "each call must return a fresh copy, not the cached dict itself"

        # The copy is a DEEP copy (code review fix #3) — reassigning a
        # top-level key on the returned dict must not corrupt the cache.
        spec1["name"] = "MUTATED"
        spec3 = tool.to_function_spec()
        assert spec3["name"] == "my_tool"

    def test_nested_parameters_mutation_does_not_corrupt_the_cache(self, monkeypatch):
        """Code review fix #3: a shallow ``dict(self._spec_cache)`` only
        protects the top-level keys — the nested ``parameters`` schema dict
        was still shared across calls. Real callers on the hot path
        (llm_handler's per-provider schema clean-up, e.g. ``_fix_array_items``)
        patch missing fields IN PLACE on whatever they're handed, so a
        shared nested dict lets one LLM call permanently rewrite the cached
        schema for every later call on this instance."""
        import agent_factory.pipeline as pipeline_mod
        monkeypatch.setattr(
            pipeline_mod.ToolDispatcher, "_input_schema_to_json_schema",
            staticmethod(lambda schema: {
                "type": "object",
                "properties": {"tags": {"type": "array"}},
            }),
        )
        tool = self._make_tool(monkeypatch)

        spec1 = tool.to_function_spec()
        # Simulate exactly what llm_handler._fix_array_items does: patch a
        # missing nested field in place on the returned schema.
        spec1["parameters"]["properties"]["tags"]["items"] = {"type": "string"}
        spec1["parameters"]["properties"]["injected"] = {"type": "string"}

        spec2 = tool.to_function_spec()
        assert "items" not in spec2["parameters"]["properties"]["tags"], (
            "a nested mutation on a previously-returned spec must not leak "
            "into a later to_function_spec() call"
        )
        assert "injected" not in spec2["parameters"]["properties"], (
            "a nested mutation must not add keys visible to later calls"
        )
        # And spec1's own nested objects must not be the same objects the
        # cache holds (proves the copy went all the way down, not just to
        # depth 1).
        assert spec1["parameters"] is not spec2["parameters"]
        assert spec1["parameters"]["properties"]["tags"] is not spec2["parameters"]["properties"]["tags"]
