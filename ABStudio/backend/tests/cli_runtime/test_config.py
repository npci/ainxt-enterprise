# SPDX-License-Identifier: Apache-2.0
"""Config, flag semantics, and startup preflight."""

from __future__ import annotations

import pytest

from app.cli_runtime import config as cfgmod


class TestFlag:
    """``ABSTUDIO_CLI_MODE`` is the only switch that changes behaviour."""

    def test_defaults_to_off(self):
        assert cfgmod.cli_mode_enabled() is False

    def test_truthy_spellings_enable_it(self, monkeypatch):
        for value in ("true", "TRUE", "1", "yes", "on", " true "):
            monkeypatch.setenv("ABSTUDIO_CLI_MODE", value)
            assert cfgmod.cli_mode_enabled() is True, value

    def test_everything_else_is_off(self, monkeypatch):
        for value in ("false", "0", "no", "off", "", "maybe"):
            monkeypatch.setenv("ABSTUDIO_CLI_MODE", value)
            assert cfgmod.cli_mode_enabled() is False, value

    def test_read_at_call_time_not_import_time(self, monkeypatch):
        """An operator must be able to flip the flag without a restart."""
        assert cfgmod.cli_mode_enabled() is False
        monkeypatch.setenv("ABSTUDIO_CLI_MODE", "true")
        assert cfgmod.cli_mode_enabled() is True
        monkeypatch.delenv("ABSTUDIO_CLI_MODE")
        assert cfgmod.cli_mode_enabled() is False

    def test_emergency_fallback_is_off_by_default(self):
        """A silent fallback is what made the previous attempt undebuggable."""
        assert cfgmod.emergency_native_fallback() is False


class TestNumericKnobs:
    def test_defaults(self):
        assert cfgmod.max_concurrency() == 5
        assert cfgmod.run_timeout_s() == 900
        assert cfgmod.max_turns() == 20
        assert cfgmod.workspace_ttl_seconds() == 86400

    def test_garbage_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("ABSTUDIO_CLI_MAX_CONCURRENCY", "not-a-number")
        assert cfgmod.max_concurrency() == 5

    def test_concurrency_is_never_zero(self, monkeypatch):
        """Zero would deadlock every run on an unacquirable semaphore."""
        monkeypatch.setenv("ABSTUDIO_CLI_MAX_CONCURRENCY", "0")
        assert cfgmod.max_concurrency() == 1

    def test_prompts_go_via_file_by_default(self):
        """Inline argv prompts break on size, quoting and newlines."""
        assert cfgmod.prompt_file_threshold() == 0


class TestSnapshot:
    def test_mcp_url_is_per_run(self, monkeypatch):
        monkeypatch.setenv("ABSTUDIO_MCP_BASE_URL", "http://127.0.0.1:9000/")
        snapshot = cfgmod.cli_runtime_config()
        assert snapshot.mcp_url_for("run-1") == "http://127.0.0.1:9000/abstudio-mcp/run-1"
        assert snapshot.mcp_url_for("run-2").endswith("/run-2")

    def test_snapshot_is_immutable(self):
        snapshot = cfgmod.cli_runtime_config()
        with pytest.raises(Exception):
            snapshot.max_concurrency = 99  # type: ignore[misc]

    def test_missing_binary_reports_unavailable(self, monkeypatch):
        monkeypatch.setenv("ABSTUDIO_CLI_PATH", "definitely-not-a-real-binary-xyz")
        assert cfgmod.cli_runtime_config().binary_available is False


class TestPreflight:
    def test_checks_nothing_when_the_flag_is_off(self):
        result = cfgmod.preflight()
        assert result.enabled is False
        assert result.ready is False
        assert result.problems == []

    def test_reports_missing_binary_and_key(self, monkeypatch):
        monkeypatch.setenv("ABSTUDIO_CLI_MODE", "true")
        monkeypatch.setenv("ABSTUDIO_CLI_PATH", "definitely-not-a-real-binary-xyz")
        monkeypatch.delenv("AINXT_API_KEY", raising=False)
        result = cfgmod.preflight(probe_version=False)
        assert result.enabled is True
        assert result.ready is False
        assert any("binary not found" in p for p in result.problems)
        assert any("API_KEY" in p for p in result.problems)

    def test_rejects_a_server_name_containing_the_cli_delimiter(self, monkeypatch):
        """``__`` in the server name would make the CLI drop every tool."""
        monkeypatch.setenv("ABSTUDIO_CLI_MODE", "true")
        monkeypatch.setenv("ABSTUDIO_MCP_SERVER_NAME", "ab__studio")
        result = cfgmod.preflight(probe_version=False)
        assert any("__" in p for p in result.problems)

    def test_warns_when_the_mcp_url_is_not_loopback(self, monkeypatch):
        """rustls rejects IP-based MCP URLs, so a hostname is required."""
        monkeypatch.setenv("ABSTUDIO_CLI_MODE", "true")
        monkeypatch.setenv("ABSTUDIO_CLI_API_KEY", "k")
        monkeypatch.setenv("ABSTUDIO_MCP_BASE_URL", "https://10.0.0.5:8000")
        result = cfgmod.preflight(probe_version=False)
        assert any("hostname" in w for w in result.warnings)
