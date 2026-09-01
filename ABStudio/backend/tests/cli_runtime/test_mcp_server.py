# SPDX-License-Identifier: Apache-2.0
"""The MCP tool plane: JSON-RPC contract, name sanitisation, and scope guards."""

from __future__ import annotations

import json
import sys
import types

from app.cli_runtime import mcp_server as ms
from app.cli_runtime.config import cli_runtime_config
from app.cli_runtime.session import SessionRegistry


# ── stub the dispatcher so no DB or sandbox is required ─────────────────────
class _FakeDispatcher:
    """Records calls and returns a predictable result."""

    calls: list = []

    async def dispatch(self, *, tool_name, inputs, user_id, email, workflow_artifact_dir):
        _FakeDispatcher.calls.append({
            "tool": tool_name, "inputs": inputs, "user_id": user_id,
            "email": email, "artifact_dir": workflow_artifact_dir,
        })
        if tool_name == "boom":
            raise RuntimeError("tool blew up")
        if tool_name == "needs_token":
            raise PermissionError("No GitLab personal access token found for this user.")
        return {"result": f"{tool_name} ok", "echo": inputs}


def _install_dispatcher():
    module = types.ModuleType("agent_factory.pipeline")
    module.ToolDispatcher = _FakeDispatcher
    package = types.ModuleType("agent_factory")
    package.__path__ = []
    sys.modules["agent_factory"] = package
    sys.modules["agent_factory.pipeline"] = module
    _FakeDispatcher.calls = []


def _server(tools=("gitlab_read_file",), skills=(), user_id="u-1", artifact_dir=""):
    _install_dispatcher()

    async def _specs(*, allowed_tools, expose_draft_tools=False):
        out = []
        for name in allowed_tools:
            if name in ms.ENGINE_NATIVE_TOOLS:
                out.append({"name": name, "description": "native",
                            "input_schema": {"type": "object"}})
            else:
                out.append({"name": name, "description": f"desc {name}",
                            "input_schema": {"type": "object", "properties": {}}})
        return out

    ms.load_tool_specs = _specs
    registry = SessionRegistry()
    session = registry.register(
        run_id="r1", user_id=user_id, email="a@b.c",
        allowed_tools=list(tools), allowed_skills=list(skills),
        workflow_artifact_dir=artifact_dir, agent_name="A",
    )
    return ms.AbstudioMcpServer(session=session, config=cli_runtime_config()), session


def _rpc(method, params=None, msg_id=1):
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}


def _text(response):
    return response["result"]["content"][0]["text"]


class TestNameSanitisation:
    def test_double_underscore_is_collapsed(self):
        """The CLI splits ``server__tool`` on ``__`` and silently DROPS any tool
        whose own name contains a second one."""
        assert ms.sanitize_tool_name("microsoft_365__outlook_send_mail") == \
            "microsoft_365_outlook_send_mail"

    def test_ordinary_names_are_untouched(self):
        for name in ("gitlab_read_file", "jira_create_issue", "code_executor"):
            assert ms.sanitize_tool_name(name) == name

    def test_names_round_trip(self):
        real = "microsoft_365__outlook_send_mail"
        exposed_by_real, real_by_exposed = ms.build_name_maps([real])
        assert real_by_exposed[exposed_by_real[real]] == real

    def test_a_collision_drops_the_loser_rather_than_shadowing(self):
        exposed, real = ms.build_name_maps(["a__b", "a_b"])
        assert len(real) == 1  # one name kept, the ambiguous one dropped


class TestProtocol:
    async def test_initialize_advertises_our_protocol_and_name(self):
        server, _ = _server()
        response = await server.handle(_rpc("initialize", {"clientInfo": {"name": "ainxt"}}))
        result = response["result"]
        assert result["protocolVersion"] == ms.MCP_PROTOCOL_VERSION
        assert result["serverInfo"]["name"] == "abstudio"
        assert result["capabilities"]["tools"] == {"listChanged": False}

    async def test_notifications_get_no_reply(self):
        server, session = _server()
        assert await server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
        assert session.handshake_done is True

    async def test_ping_is_answered(self):
        server, _ = _server()
        response = await server.handle(_rpc("ping"))
        assert response["result"] == {}

    async def test_unknown_method_returns_method_not_found(self):
        server, _ = _server()
        response = await server.handle(_rpc("does/not/exist"))
        assert response["error"]["code"] == ms.ERR_METHOD_NOT_FOUND

    async def test_bad_jsonrpc_version_is_rejected(self):
        server, _ = _server()
        response = await server.handle({"jsonrpc": "1.0", "id": 1, "method": "ping"})
        assert response["error"]["code"] == ms.ERR_INVALID_REQUEST

    async def test_non_object_body_is_rejected(self):
        server, _ = _server()
        response = await server.handle(["not", "an", "object"])
        assert response["error"]["code"] == ms.ERR_INVALID_REQUEST


class TestToolsList:
    async def test_only_attached_tools_are_exposed(self):
        server, _ = _server(tools=("gitlab_read_file", "jira_create_issue"))
        response = await server.handle(_rpc("tools/list"))
        names = [t["name"] for t in response["result"]["tools"]]
        assert names == ["gitlab_read_file", "jira_create_issue"]

    async def test_exposed_names_are_sanitised(self):
        server, _ = _server(tools=("microsoft_365__outlook_send_mail",))
        response = await server.handle(_rpc("tools/list"))
        assert response["result"]["tools"][0]["name"] == "microsoft_365_outlook_send_mail"

    async def test_schema_uses_the_mcp_key_name(self):
        server, _ = _server()
        tool = (await server.handle(_rpc("tools/list")))["result"]["tools"][0]
        assert "inputSchema" in tool  # MCP spells it camelCase

    async def test_tool_count_is_recorded_for_the_zero_tools_alarm(self):
        server, session = _server(tools=("gitlab_read_file",))
        await server.handle(_rpc("tools/list"))
        assert session.listed_tool_count == 1


class TestToolsCall:
    async def test_a_call_reaches_the_dispatcher_with_the_users_identity(self):
        server, _ = _server(user_id="u-42")
        await server.handle(_rpc("tools/call", {
            "name": "gitlab_read_file", "arguments": {"path": "README.md"},
        }))
        call = _FakeDispatcher.calls[-1]
        assert call["tool"] == "gitlab_read_file"
        assert call["user_id"] == "u-42"
        assert call["inputs"] == {"path": "README.md"}

    async def test_a_sanitised_name_routes_to_the_real_tool(self):
        server, _ = _server(tools=("microsoft_365__outlook_send_mail",))
        await server.handle(_rpc("tools/call", {
            "name": "microsoft_365_outlook_send_mail", "arguments": {},
        }))
        assert _FakeDispatcher.calls[-1]["tool"] == "microsoft_365__outlook_send_mail"

    async def test_an_unattached_tool_is_denied(self):
        server, _ = _server(tools=("gitlab_read_file",))
        response = await server.handle(_rpc("tools/call", {"name": "jira_create_issue"}))
        assert response["result"]["isError"] is True
        assert "not available" in _text(response)
        assert _FakeDispatcher.calls == []  # never dispatched

    async def test_a_raising_tool_becomes_an_error_result_not_a_crash(self):
        server, _ = _server(tools=("boom",))
        response = await server.handle(_rpc("tools/call", {"name": "boom"}))
        assert "error" in json.loads(_text(response))

    async def test_a_missing_credential_message_is_surfaced_verbatim(self):
        """The resolver's message tells the user where to add their token."""
        server, _ = _server(tools=("needs_token",))
        response = await server.handle(_rpc("tools/call", {"name": "needs_token"}))
        assert "personal access token" in json.loads(_text(response))["error"]

    async def test_tool_events_are_published_for_the_ui(self):
        """The CLI's own output has no tool events, so these are the only source."""
        server, session = _server()
        await server.handle(_rpc("tools/call", {
            "name": "gitlab_read_file", "arguments": {"path": "a"},
        }))
        kinds = [e.kind for e in session.drain_events()]
        assert kinds == ["tool_call_start", "tool_call_result"]

    async def test_secrets_are_redacted_from_the_published_arguments(self):
        server, session = _server()
        await server.handle(_rpc("tools/call", {
            "name": "gitlab_read_file",
            "arguments": {"path": "a", "api_key": "sk-super-secret"},
        }))
        start = session.drain_events()[0]
        assert start.arguments["api_key"] == "***"
        assert start.arguments["path"] == "a"

    async def test_code_executor_receives_the_artifact_directory(self):
        server, _ = _server(tools=("code_executor",), artifact_dir="/run/art")
        await server.handle(_rpc("tools/call", {"name": "code_executor",
                                                "arguments": {"code": "x=1"}}))
        assert _FakeDispatcher.calls[-1]["artifact_dir"] == "/run/art"

    async def test_other_tools_do_not_receive_an_artifact_directory(self):
        server, _ = _server(tools=("gitlab_read_file",), artifact_dir="/run/art")
        await server.handle(_rpc("tools/call", {"name": "gitlab_read_file"}))
        assert _FakeDispatcher.calls[-1]["artifact_dir"] == ""


class TestSkillScope:
    async def test_an_unattached_skill_is_refused(self):
        server, _ = _server(tools=("read_skill_file",), skills=("pptx",))
        response = await server.handle(_rpc("tools/call", {
            "name": "read_skill_file", "arguments": {"skill": "pdf", "rel_path": "x"},
        }))
        assert "not attached" in json.loads(_text(response))["error"]
        assert _FakeDispatcher.calls == []

    async def test_an_attached_skill_is_allowed_through(self):
        server, _ = _server(tools=("read_skill_file",), skills=("pptx",))
        await server.handle(_rpc("tools/call", {
            "name": "read_skill_file", "arguments": {"skill": "pptx", "rel_path": "x"},
        }))
        assert _FakeDispatcher.calls[-1]["tool"] == "read_skill_file"

    async def test_scope_is_fail_closed_when_no_skills_are_attached(self):
        server, _ = _server(tools=("read_skill_file",), skills=())
        response = await server.handle(_rpc("tools/call", {
            "name": "read_skill_file", "arguments": {"skill": "pptx", "rel_path": "x"},
        }))
        assert "error" in json.loads(_text(response))
        assert _FakeDispatcher.calls == []


class TestEngineNativeTools:
    async def test_ask_human_returns_a_sentinel_instead_of_dispatching(self):
        """It drives the HITL suspend protocol, which a subprocess cannot do."""
        server, _ = _server(tools=("ask_human",))
        response = await server.handle(_rpc("tools/call", {
            "name": "ask_human", "arguments": {"question": "proceed?"},
        }))
        payload = json.loads(_text(response))
        assert payload[ms.ENGINE_NATIVE_SENTINEL] is True
        assert payload["tool"] == "ask_human"
        assert _FakeDispatcher.calls == []

    async def test_it_is_still_advertised_so_the_model_sees_the_capability(self):
        server, _ = _server(tools=("ask_human",))
        names = [t["name"] for t in (await server.handle(_rpc("tools/list")))["result"]["tools"]]
        assert "ask_human" in names


class TestResultEncoding:
    async def test_oversized_results_are_truncated_not_dropped(self, monkeypatch):
        monkeypatch.setenv("ABSTUDIO_CLI_MAX_TOOL_RESULT_BYTES", "5000")
        server, _ = _server(tools=("big",))

        class _Big(_FakeDispatcher):
            async def dispatch(self, **kwargs):
                return {"result": "y" * 20000}

        sys.modules["agent_factory.pipeline"].ToolDispatcher = _Big
        response = await server.handle(_rpc("tools/call", {"name": "big"}))
        assert "[truncated]" in _text(response)

    def test_non_serialisable_values_do_not_break_the_call(self):
        import datetime
        payload = ms._encode_result({"when": datetime.datetime.now()}, 10_000)
        assert "when" in payload
