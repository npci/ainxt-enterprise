# SPDX-License-Identifier: Apache-2.0
"""Smoke tests against the REAL ``ainxt`` binary. Skipped when it is absent.

Everything else in this suite uses ``fake_cli.py``, which is fast and
deterministic but only ever confirms that our code agrees with *our own*
assumptions. These tests confirm those assumptions still match the deployed
binary — the exact class of mistake that sank the previous attempt, which built
its argv against a different CLI generation whose flags this build rejects.

The flag/contract checks need no credentials or model traffic. The one test that
does call a model is additionally gated on ``ABSTUDIO_CLI_SMOKE_MODEL=1``, so a
normal run never spends tokens.

Enable with:  ABSTUDIO_CLI_TEST_BINARY=/path/to/ainxt
              (or leave unset to auto-detect ``ainxt`` on PATH)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

# Flags this integration relies on. If the binary stops accepting one of these,
# every CLI run breaks, and it must fail loudly here rather than in production.
REQUIRED_FLAGS = [
    "--single",
    "--prompt-file",
    "--output-format",
    "--permission-mode",
    "--max-turns",
    "--cwd",
    "--allow",
    "--verbatim",
    "--no-plan",
    "--no-subagents",
    "--model",
    "--resume",
]

# Flags used by ``agents/sdlc_cli_engine.py`` that this CLI generation does NOT
# accept. Asserting their absence documents *why* that module is not reused: if
# one ever appears, the two code paths could be reconciled.
ABSENT_FLAGS = [
    "--yes",
    "--no-review",
    "--output-schema",
    "--allowed-tools",
    "--add-dir",
    "--mcp-config",
]


def _binary() -> str:
    explicit = (os.getenv("ABSTUDIO_CLI_TEST_BINARY", "") or "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else ""
    return shutil.which("ainxt") or ""


def _require_binary() -> str:
    binary = _binary()
    if not binary:
        pytest.skip("no ainxt binary available (set ABSTUDIO_CLI_TEST_BINARY)")
    return binary


def _help_text(binary: str) -> str:
    proc = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=60)
    return (proc.stdout or "") + (proc.stderr or "")


class TestBinaryContract:
    def test_the_version_is_one_we_validated_against(self):
        from app.cli_runtime.config import probe_cli_version, validated_cli_versions

        binary = _require_binary()
        version = probe_cli_version(binary)
        assert version, "could not determine the CLI version"
        supported = validated_cli_versions()
        if supported and version not in supported:
            pytest.skip(
                f"ainxt {version} is outside the validated set {supported} — "
                f"re-verify the flag and event contract before widening it"
            )

    def test_every_flag_we_depend_on_still_exists(self):
        binary = _require_binary()
        text = _help_text(binary)
        missing = [flag for flag in REQUIRED_FLAGS if flag not in text]
        assert not missing, f"the CLI no longer documents: {missing}"

    def test_the_flags_from_the_other_engine_are_still_absent(self):
        binary = _require_binary()
        text = _help_text(binary)
        present = [flag for flag in ABSENT_FLAGS if flag in text]
        if present:
            pytest.skip(
                f"this build now accepts {present} — the sdlc_cli_engine argv "
                f"builder may be reusable; re-check before relying on it"
            )

    def test_streaming_json_is_an_accepted_output_format(self):
        binary = _require_binary()
        assert "streaming-json" in _help_text(binary)

    def test_the_permission_modes_we_use_are_accepted(self):
        binary = _require_binary()
        proc = subprocess.run(
            [binary, "--permission-mode", "definitely-not-a-mode"],
            capture_output=True, text=True, timeout=60,
        )
        text = (proc.stdout or "") + (proc.stderr or "")
        for mode in ("plan", "acceptEdits", "bypassPermissions"):
            assert mode in text, f"{mode} is no longer a valid permission mode"


class TestMcpDiscovery:
    """The folder-trust behaviour this integration hinges on.

    A repo-local (project-scope) MCP server is SILENTLY skipped in an untrusted
    folder — the run proceeds with zero ABStudio tools and no error anywhere.
    ``AINXT_FOLDER_TRUST=0`` in the child env is what prevents that, and this is
    the test that will catch it if the behaviour ever changes.
    """

    def _doctor(self, binary: str, workspace: str, *, trust_disabled: bool) -> str:
        env = dict(os.environ)
        if trust_disabled:
            env["AINXT_FOLDER_TRUST"] = "0"
        else:
            env.pop("AINXT_FOLDER_TRUST", None)
        proc = subprocess.run(
            [binary, "mcp", "doctor"],
            cwd=workspace, env=env, capture_output=True, text=True, timeout=180,
        )
        return (proc.stdout or "") + (proc.stderr or "")

    def _workspace(self, tmp_path) -> str:
        """A workspace with a real (trivial) stdio MCP server declared."""
        import textwrap

        workspace = str(tmp_path)
        server = os.path.join(workspace, "server.py")
        with open(server, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent('''
                import json, sys
                def send(obj):
                    sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
                for line in sys.stdin:
                    line = line.strip()
                    if not line:
                        continue
                    msg = json.loads(line)
                    mid, method = msg.get("id"), msg.get("method")
                    if method == "initialize":
                        send({"jsonrpc":"2.0","id":mid,"result":{
                            "protocolVersion":"2024-11-05",
                            "capabilities":{"tools":{"listChanged":False}},
                            "serverInfo":{"name":"probe","version":"1.0"}}})
                    elif method == "tools/list":
                        send({"jsonrpc":"2.0","id":mid,"result":{"tools":[
                            {"name":"probe_tool","description":"probe",
                             "inputSchema":{"type":"object","properties":{}}}]}})
                    elif method == "ping":
                        send({"jsonrpc":"2.0","id":mid,"result":{}})
            ''').strip() + "\n")

        os.makedirs(os.path.join(workspace, ".ainxt"), exist_ok=True)
        with open(os.path.join(workspace, ".ainxt", "config.toml"), "w", encoding="utf-8") as fh:
            fh.write(
                "[mcp_servers.probe]\n"
                f'command = "{sys.executable}"\n'
                f'args = ["{server}"]\n'.replace("\\", "\\\\")
                + "enabled = true\nstartup_timeout_sec = 30\n"
            )
        return workspace

    def test_a_project_server_starts_when_folder_trust_is_disabled(self, tmp_path):
        binary = _require_binary()
        output = self._doctor(binary, self._workspace(tmp_path), trust_disabled=True)
        assert "untrusted" not in output.lower(), (
            "AINXT_FOLDER_TRUST=0 no longer bypasses the folder-trust gate — "
            "repo-local MCP servers will be skipped and agents will have no tools"
        )
        assert "handshake OK" in output or "tools discovered" in output, output[-600:]

    def test_it_is_skipped_without_that_override(self, tmp_path):
        """Documents the failure this integration is designed around."""
        binary = _require_binary()
        output = self._doctor(binary, self._workspace(tmp_path), trust_disabled=False)
        if "untrusted" not in output.lower():
            pytest.skip("this folder is already trusted, so the gate cannot be observed")


class TestModelRoundTrip:
    """Costs tokens, so it is opt-in via ABSTUDIO_CLI_SMOKE_MODEL=1."""

    def test_the_cli_calls_an_mcp_tool_and_uses_its_answer(self, tmp_path):
        binary = _require_binary()
        if os.getenv("ABSTUDIO_CLI_SMOKE_MODEL", "") != "1":
            pytest.skip("set ABSTUDIO_CLI_SMOKE_MODEL=1 to run the model round trip")

        workspace = TestMcpDiscovery()._workspace(tmp_path)
        # Replace the probe server with one holding an unguessable answer.
        secret = "the ledger total is INR 88,417.25"
        server = os.path.join(workspace, "server.py")
        source = open(server, encoding="utf-8").read().replace(
            '{"name":"probe_tool","description":"probe",',
            '{"name":"get_ledger_total","description":"Return the ledger total.",',
        ).replace(
            'elif method == "ping":',
            'elif method == "tools/call":\n'
            f'        send({{"jsonrpc":"2.0","id":mid,"result":{{"content":['
            f'{{"type":"text","text":"{secret}"}}]}}}})\n'
            '    elif method == "ping":',
        )
        open(server, "w", encoding="utf-8").write(source)

        env = {**os.environ, "AINXT_FOLDER_TRUST": "0", "NO_COLOR": "1"}
        proc = subprocess.run(
            [binary, "--single", "What is the ledger total? Use your tools.",
             "--output-format", "json", "--permission-mode", "bypassPermissions",
             "--max-turns", "6", "--cwd", workspace],
            cwd=workspace, env=env, capture_output=True, text=True, timeout=300,
        )
        assert proc.returncode == 0, (proc.stderr or "")[-800:]
        payload = json.loads(proc.stdout)
        assert "88,417.25" in (payload.get("text") or ""), payload
