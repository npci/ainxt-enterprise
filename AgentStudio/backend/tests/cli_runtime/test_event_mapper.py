# SPDX-License-Identifier: MIT
"""SSE mapping.

These are compatibility tests. The frontend must not be able to tell a CLI-backed
turn from a native one, and three consumers break *silently* if a payload shape
drifts: ``execution._TRACED_EVENTS`` + ``RunUsageTracker`` (cost tracing goes
dark), ``LoopRunner._capture_terminal_output`` (loops capture nothing), and the
Debug Log (flooded without the 50 KB cap).
"""

from __future__ import annotations

from app.cli_runtime.event_mapper import (
    SSE_RESULT_MAX,
    CliTurnResult,
    merge,
    tool_event_to_sse,
    usage_events,
)
from app.cli_runtime.mcp_server import ENGINE_NATIVE_SENTINEL
from app.cli_runtime.runner import EV_END, EV_ERROR, EV_TEXT, EV_THOUGHT, CliEvent
from app.cli_runtime.session import (
    TOOL_EVENT_RESULT,
    TOOL_EVENT_START,
    SessionRegistry,
    ToolEvent,
)


async def _events(*items):
    for item in items:
        yield item


class TestToolPayloads:
    def test_start_matches_the_native_key_shape(self):
        name, payload = tool_event_to_sse(
            ToolEvent(kind=TOOL_EVENT_START, tool_name="gitlab_read_file",
                      arguments={"path": "a"}),
            agent_name="A",
        )
        assert name == "tool_call_start"
        assert payload == {"agent": "A", "tool_name": "gitlab_read_file",
                           "arguments": {"path": "a"}}

    def test_result_matches_the_native_key_shape(self):
        name, payload = tool_event_to_sse(
            ToolEvent(kind=TOOL_EVENT_RESULT, tool_name="t", result={"ok": True}),
            agent_name="A",
        )
        assert name == "tool_call_result"
        assert set(payload) == {"agent", "tool_name", "result"}

    def test_large_results_are_capped_and_flagged(self):
        _name, payload = tool_event_to_sse(
            ToolEvent(kind=TOOL_EVENT_RESULT, tool_name="t", result="x" * (SSE_RESULT_MAX + 500)),
            agent_name="A",
        )
        assert len(payload["result"]) == SSE_RESULT_MAX
        assert payload["truncated"] is True
        assert payload["full_length"] == SSE_RESULT_MAX + 500

    def test_small_results_carry_no_truncation_metadata(self):
        _name, payload = tool_event_to_sse(
            ToolEvent(kind=TOOL_EVENT_RESULT, tool_name="t", result="small"),
            agent_name="A",
        )
        assert "truncated" not in payload

    def test_an_error_is_surfaced_in_the_result_field(self):
        _name, payload = tool_event_to_sse(
            ToolEvent(kind=TOOL_EVENT_RESULT, tool_name="t", error="it failed"),
            agent_name="A",
        )
        assert payload["result"] == "it failed"


class TestUsageFrame:
    def test_the_payload_carries_what_cost_tracing_reads(self):
        frames = usage_events(
            agent_name="A", node_id="n1", model="claude-sonnet-4-6",
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        )
        name, payload = frames[0]
        assert name == "agent_usage"
        assert set(payload) == {"agent", "node_id", "model", "usage"}
        assert payload["usage"]["total_tokens"] == 3


class TestMerge:
    async def test_text_becomes_agent_token_frames(self):
        result = CliTurnResult()
        frames = [
            frame async for frame in merge(
                _events(CliEvent(type=EV_TEXT, text="hi")),
                agent_name="A", result=result,
            )
        ]
        assert frames == [("agent_token", {"agent": "A", "token": "hi"})]
        assert result.output == "hi"

    async def test_tokens_are_suppressed_for_non_final_nodes(self):
        """The native engine only streams tokens for the terminal agent."""
        result = CliTurnResult()
        frames = [
            frame async for frame in merge(
                _events(CliEvent(type=EV_TEXT, text="hi")),
                agent_name="A", result=result, emit_tokens=False,
            )
        ]
        assert frames == []
        assert result.output == "hi"  # still captured for the next node

    async def test_thoughts_are_captured_but_not_emitted(self):
        result = CliTurnResult()
        frames = [
            frame async for frame in merge(
                _events(CliEvent(type=EV_THOUGHT, text="thinking")),
                agent_name="A", result=result,
            )
        ]
        assert frames == []
        assert result.thoughts == ["thinking"]

    async def test_end_populates_usage_and_the_resume_session(self):
        result = CliTurnResult()
        async for _frame in merge(
            _events(CliEvent(type=EV_END, session_id="s9", num_turns=4,
                             stop_reason="EndTurn",
                             usage={"input_tokens": 8, "output_tokens": 2})),
            agent_name="A", result=result,
        ):
            pass
        assert result.usage["total_tokens"] == 10
        assert result.session_id == "s9" and result.num_turns == 4
        assert result.ok is True

    async def test_an_error_marks_the_turn_as_failed(self):
        result = CliTurnResult()
        async for _frame in merge(
            _events(CliEvent(type=EV_ERROR, message="boom")),
            agent_name="A", result=result,
        ):
            pass
        assert result.ok is False and result.error == "boom"

    async def test_tool_frames_precede_the_final_response_text(self):
        """Ordering must stay causal: tool cards appear before the answer that
        talks about them. Response text is buffered, scrubbed and emitted ONCE at
        the end (per-token scrubbing cannot catch a path split across chunks), so
        the tool frames come first and a single agent_token comes last."""
        registry = SessionRegistry()
        session = registry.register(run_id="r1")

        async def _stream():
            yield CliEvent(type=EV_TEXT, text="calling... ")
            session.publish(ToolEvent(kind=TOOL_EVENT_START, tool_name="t"))
            session.publish(ToolEvent(kind=TOOL_EVENT_RESULT, tool_name="t", result={"n": 1}))
            yield CliEvent(type=EV_TEXT, text="done")

        result = CliTurnResult()
        names = [
            name async for name, _payload in merge(
                _stream(), agent_name="A", result=result, session=session,
            )
        ]
        assert names == ["tool_call_start", "tool_call_result", "agent_token"]
        # The two text chunks are concatenated into the single final token.
        assert result.output == "calling... done"

    async def test_events_published_after_the_last_text_are_still_flushed(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1")

        async def _stream():
            yield CliEvent(type=EV_TEXT, text="x")
            session.publish(ToolEvent(kind=TOOL_EVENT_START, tool_name="late"))

        result = CliTurnResult()
        names = [
            name async for name, _p in merge(
                _stream(), agent_name="A", result=result, session=session,
            )
        ]
        assert "tool_call_start" in names

    async def test_generated_files_are_collected(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1")

        async def _stream():
            session.publish(ToolEvent(
                kind=TOOL_EVENT_RESULT, tool_name="code_executor",
                generated_files=[{"filename": "d.pptx", "disk_name": "d_1.pptx"}],
            ))
            yield CliEvent(type=EV_TEXT, text="made it")

        result = CliTurnResult()
        async for _frame in merge(_stream(), agent_name="A", result=result, session=session):
            pass
        assert [f["filename"] for f in result.generated_files] == ["d.pptx"]

    async def test_tool_calls_are_counted(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1")

        async def _stream():
            session.publish(ToolEvent(kind=TOOL_EVENT_RESULT, tool_name="a"))
            session.publish(ToolEvent(kind=TOOL_EVENT_RESULT, tool_name="b"))
            yield CliEvent(type=EV_TEXT, text="x")

        result = CliTurnResult()
        async for _frame in merge(_stream(), agent_name="A", result=result, session=session):
            pass
        assert result.tool_calls == 2

    async def test_an_engine_native_request_is_detected(self):
        """``ask_human`` / ``spawn_swarm`` come back as sentinels for the caller
        to run natively, since neither can execute inside the CLI."""
        registry = SessionRegistry()
        session = registry.register(run_id="r1")

        async def _stream():
            session.publish(ToolEvent(
                kind=TOOL_EVENT_RESULT, tool_name="ask_human",
                result={ENGINE_NATIVE_SENTINEL: True, "tool": "ask_human",
                        "arguments": {"question": "go?"}},
            ))
            yield CliEvent(type=EV_TEXT, text="x")

        result = CliTurnResult()
        async for _frame in merge(_stream(), agent_name="A", result=result, session=session):
            pass
        assert result.engine_native_requests == [
            {"tool": "ask_human", "arguments": {"question": "go?"}},
        ]

    async def test_a_session_provider_is_resolved_lazily(self):
        """The bridge builds the stream before the session exists."""
        registry = SessionRegistry()
        holder: dict = {}

        async def _stream():
            session = registry.register(run_id="r1")
            holder["session"] = session
            session.publish(ToolEvent(kind=TOOL_EVENT_START, tool_name="t"))
            yield CliEvent(type=EV_TEXT, text="x")

        result = CliTurnResult()
        names = [
            name async for name, _p in merge(
                _stream(), agent_name="A", result=result,
                session_provider=lambda: holder.get("session"),
            )
        ]
        assert "tool_call_start" in names
