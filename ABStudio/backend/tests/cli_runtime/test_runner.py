# SPDX-License-Identifier: Apache-2.0
"""The spawn path: argv contract, child env, event parsing, and failure modes.

These run against ``fake_cli.py`` — a stand-in that speaks the same argv and
``streaming-json`` contract as the real binary. That makes every failure mode
(timeout, non-zero exit, malformed output, cancellation) a fast deterministic
test instead of something only reproducible against a live model.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from launcher import make_launcher

from app.cli_runtime.config import cli_runtime_config
from app.cli_runtime.runner import (
    EV_END,
    EV_ERROR,
    EV_TEXT,
    CliTurnRequest,
    build_argv,
    build_env,
    normalise_usage,
    parse_event,
    run_cli_turn,
)


def _configure(monkeypatch, scenario: str = "ok", **env) -> str:
    """Point the runtime at the fake CLI and return its workspace root."""
    tmp = tempfile.mkdtemp()
    launcher = make_launcher(tmp)
    monkeypatch.setenv("ABSTUDIO_CLI_MODE", "true")
    monkeypatch.setenv("ABSTUDIO_CLI_PATH", launcher)
    monkeypatch.setenv("ABSTUDIO_CLI_API_KEY", "fake-key")
    monkeypatch.setenv("ABSTUDIO_CLI_WORKSPACE_ROOT", os.path.join(tmp, "ws"))
    monkeypatch.setenv("FAKE_CLI_SCENARIO", scenario)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    return tmp


async def _collect(request: CliTurnRequest):
    return [event async for event in run_cli_turn(request)]


def _request(**kw) -> CliTurnRequest:
    kw.setdefault("prompt", "do the thing")
    kw.setdefault("model", "claude-sonnet-4-6")
    kw.setdefault("agent_name", "Test Agent")
    return CliTurnRequest(**kw)


class TestArgv:
    """Only flags that exist on ainxt 0.2.101 may be emitted.

    The flags used by ``agents/sdlc_cli_engine.py`` (``--yes``, ``--no-review``,
    ``--output-schema``, ``--allowed-tools``, ``--add-dir``, ``--mcp-config``) are
    all rejected by this build, which is why that module is not reused.
    """

    _FORBIDDEN = (
        "--yes", "--no-review", "--output-schema", "--allowed-tools",
        "--add-dir", "--mcp-config", "--dangerously-skip-permissions",
    )

    def test_no_flag_outside_the_verified_set_is_emitted(self):
        argv = build_argv(
            config=cli_runtime_config(), request=_request(), workspace="/w",
        )
        for flag in self._FORBIDDEN:
            assert flag not in argv, flag

    def test_streaming_json_is_requested(self):
        argv = build_argv(config=cli_runtime_config(), request=_request(), workspace="/w")
        assert argv[argv.index("--output-format") + 1] == "streaming-json"

    def test_the_mcp_permission_rule_uses_the_matching_form(self):
        """``mcp__server__tool`` never matches; ``MCPTool(server__tool)`` does."""
        argv = build_argv(config=cli_runtime_config(), request=_request(), workspace="/w")
        rule = argv[argv.index("--allow") + 1]
        assert rule == "MCPTool(abstudio__*)"
        assert not rule.startswith("mcp__")

    def test_the_prompt_goes_via_a_file_when_one_is_supplied(self):
        argv = build_argv(
            config=cli_runtime_config(), request=_request(prompt="hello"),
            workspace="/w", prompt_file="/w/prompt.txt",
        )
        assert argv[argv.index("--prompt-file") + 1] == "/w/prompt.txt"
        assert "hello" not in argv

    def test_resume_is_only_emitted_when_a_session_is_given(self):
        plain = build_argv(config=cli_runtime_config(), request=_request(), workspace="/w")
        assert "--resume" not in plain
        resumed = build_argv(
            config=cli_runtime_config(),
            request=_request(resume_session_id="sess-1"), workspace="/w",
        )
        assert resumed[resumed.index("--resume") + 1] == "sess-1"

    def test_the_working_directory_is_passed_explicitly(self):
        argv = build_argv(config=cli_runtime_config(), request=_request(), workspace="/my/ws")
        assert argv[argv.index("--cwd") + 1] == "/my/ws"


class TestChildEnv:
    def test_folder_trust_is_disabled(self):
        """Without this the CLI SILENTLY refuses to start our repo-local MCP
        server, and the agent runs with zero ABStudio tools and no error."""
        env = build_env(config=cli_runtime_config(), workspace="/w")
        assert env["AINXT_FOLDER_TRUST"] == "0"

    def test_the_api_key_is_exported_for_gateway_auth(self, monkeypatch):
        monkeypatch.setenv("ABSTUDIO_CLI_API_KEY", "key-123")
        env = build_env(config=cli_runtime_config(), workspace="/w")
        assert env["AINXT_API_KEY"] == "key-123"

    def test_colour_is_disabled_so_it_cannot_corrupt_the_ndjson(self):
        env = build_env(config=cli_runtime_config(), workspace="/w")
        assert env["NO_COLOR"] == "1" and env["FORCE_COLOR"] == "0"

    def test_the_parent_environment_is_inherited(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
        env = build_env(config=cli_runtime_config(), workspace="/w")
        assert env["HTTPS_PROXY"] == "http://proxy:3128"


class TestEventParsing:
    def test_text_events_carry_their_chunk(self):
        event = parse_event('{"type":"text","data":"hello"}')
        assert event.type == EV_TEXT and event.text == "hello"

    def test_end_events_carry_session_and_usage(self):
        event = parse_event(
            '{"type":"end","sessionId":"s1","stopReason":"EndTurn",'
            '"usage":{"input_tokens":10,"output_tokens":4},"num_turns":3}'
        )
        assert event.type == EV_END
        assert event.session_id == "s1" and event.num_turns == 3

    def test_error_events_carry_a_message(self):
        assert parse_event('{"type":"error","message":"boom"}').message == "boom"

    def test_noise_is_ignored_rather_than_fatal(self):
        for line in ("", "   ", "not json", "Warning: something", "[]"):
            assert parse_event(line) is None

    def test_unknown_event_types_are_ignored(self):
        """The documented event list is explicitly non-exhaustive, so a future
        event type must never abort a run."""
        assert parse_event('{"type":"auto_compact_start"}') is None


class TestUsageNormalisation:
    def test_anthropic_keys_map_to_the_abstudio_shape(self):
        usage = normalise_usage({"input_tokens": 10, "output_tokens": 5})
        assert usage == {"prompt_tokens": 10, "completion_tokens": 5,
                         "total_tokens": 15, "estimated": False}

    def test_already_normalised_keys_are_accepted(self):
        usage = normalise_usage({"prompt_tokens": 3, "completion_tokens": 2})
        assert usage["total_tokens"] == 5

    def test_missing_usage_becomes_zeros(self):
        assert normalise_usage(None)["total_tokens"] == 0


class TestPreSpawnGuards:
    async def test_a_missing_binary_reports_an_error_event(self, monkeypatch):
        monkeypatch.setenv("ABSTUDIO_CLI_PATH", "definitely-not-real-xyz")
        monkeypatch.setenv("ABSTUDIO_CLI_API_KEY", "k")
        events = await _collect(_request())
        assert [e.type for e in events] == [EV_ERROR]
        assert "not found" in events[0].message

    async def test_a_missing_api_key_reports_an_error_event(self, monkeypatch):
        _configure(monkeypatch)
        monkeypatch.delenv("ABSTUDIO_CLI_API_KEY")
        monkeypatch.delenv("AINXT_API_KEY", raising=False)
        events = await _collect(_request())
        assert events[0].type == EV_ERROR and "API_KEY" in events[0].message


class TestScenarios:
    async def test_a_normal_run_streams_text_then_ends(self, monkeypatch):
        _configure(monkeypatch, "ok")
        events = await _collect(_request())
        assert [e.type for e in events][-1] == EV_END
        assert "".join(e.text for e in events if e.type == EV_TEXT)

    async def test_an_error_event_terminates_the_run(self, monkeypatch):
        _configure(monkeypatch, "error")
        events = await _collect(_request())
        assert events[-1].type == EV_ERROR

    async def test_a_crash_without_a_terminal_event_still_reports_one(self, monkeypatch):
        """A non-zero exit with no ``end`` frame must not look like success."""
        _configure(monkeypatch, "crash")
        events = await _collect(_request())
        assert events[-1].type == EV_ERROR

    async def test_malformed_output_does_not_prevent_recovery(self, monkeypatch):
        _configure(monkeypatch, "badjson")
        events = await _collect(_request())
        assert events[-1].type == EV_END
        assert "recovered" in "".join(e.text for e in events if e.type == EV_TEXT)

    async def test_a_hang_is_killed_and_reported(self, monkeypatch):
        _configure(monkeypatch, "hang",
                   ABSTUDIO_CLI_RUN_TIMEOUT_S=5, FAKE_CLI_HANG_SECONDS=60)
        events = await _collect(_request())
        assert events[-1].type == EV_ERROR
        assert "did not finish" in events[-1].message

    async def test_an_unknown_flag_surfaces_as_a_contract_mismatch(self, monkeypatch):
        """The fake CLI rejects unknown flags exactly as the real binary does, so
        an argv regression is caught here rather than in production."""
        _configure(monkeypatch, "noflags")
        events = await _collect(_request())
        assert events[-1].type == EV_ERROR
        assert "argument" in events[-1].message.lower() or "flag" in events[-1].message.lower()


class TestSessionLifecycle:
    async def test_the_session_is_revoked_when_the_run_ends(self, monkeypatch, registry):
        _configure(monkeypatch, "ok")
        await _collect(_request(run_id="lifecycle-1"))
        assert registry.get("lifecycle-1") is None

    async def test_the_session_exists_while_the_run_is_in_flight(self, monkeypatch, registry):
        _configure(monkeypatch, "ok")
        seen = {}
        request = _request(run_id="live-1", tool_names=["gitlab_read_file"])
        async for _event in run_cli_turn(
            request, on_session=lambda s: seen.setdefault("session", s),
        ):
            pass
        assert seen["session"].run_id == "live-1"
        assert seen["session"].allowed_tools == ["gitlab_read_file"]

    async def test_cancellation_kills_the_child_and_frees_the_slot(self, monkeypatch, registry):
        _configure(monkeypatch, "hang", FAKE_CLI_HANG_SECONDS=60)

        async def _run():
            async for _event in run_cli_turn(_request(run_id="cancel-1")):
                pass

        task = asyncio.create_task(_run())
        await asyncio.sleep(2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert registry.get("cancel-1") is None


class TestConcurrency:
    async def test_the_semaphore_bounds_simultaneous_spawns(self, monkeypatch):
        _configure(monkeypatch, "ok", ABSTUDIO_CLI_MAX_CONCURRENCY=2)
        from app.cli_runtime import runner as runner_mod

        # Force a rebuild so the new capacity is picked up.
        runner_mod._SEMAPHORE = None
        semaphore = runner_mod.get_semaphore()
        assert semaphore._value == 2

    async def test_slots_are_returned_so_capacity_does_not_leak(self, monkeypatch):
        """A leaked slot permanently shrinks capacity and eventually deadlocks
        every run, so this is the single most important concurrency property."""
        _configure(monkeypatch, "ok", ABSTUDIO_CLI_MAX_CONCURRENCY=1)
        from app.cli_runtime import runner as runner_mod
        runner_mod._SEMAPHORE = None

        for index in range(3):
            await _collect(_request(run_id=f"seq-{index}"))
        assert runner_mod.get_semaphore()._value == 1
