# SPDX-License-Identifier: Apache-2.0
"""The flag-off contract: with ``ABSTUDIO_CLI_MODE`` unset, nothing changes.

This is the safety property that makes the feature deployable. The integration
adds a branch to two hot paths (``AgentRunner.run`` and
``NativeEngine._run_agent``); if that branch could misfire, or its imports could
fail a run, then merely *shipping* this code would be a risk regardless of the
flag. These tests pin that down.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (_BACKEND / relative).read_text(encoding="utf-8")


class TestGateIsClosedByDefault:
    def test_the_flag_defaults_to_off(self):
        from app.cli_runtime.config import cli_mode_enabled
        assert cli_mode_enabled() is False

    def test_no_other_env_var_can_turn_it_on(self, monkeypatch):
        """Only ABSTUDIO_CLI_MODE decides. Everything else is operational."""
        from app.cli_runtime.config import cli_mode_enabled

        for name in ("ABSTUDIO_CLI_PATH", "ABSTUDIO_CLI_API_KEY",
                     "ABSTUDIO_MCP_BASE_URL", "ABSTUDIO_CLI_MAX_CONCURRENCY",
                     "ABSTUDIO_CLI_EMERGENCY_FALLBACK"):
            monkeypatch.setenv(name, "true")
        assert cli_mode_enabled() is False


class TestIntegrationPointsAreGuarded:
    """Both call sites must check the flag before doing anything else."""

    def test_agent_runner_checks_the_flag_before_delegating(self):
        source = _source("agent_factory/pipeline.py")
        assert "cli_mode_enabled" in source
        # Look at the CALL site (``self._run_turn_via_cli``), not the ``async def``
        # that defines it, and require a flag check immediately before it.
        index = source.index("self._run_turn_via_cli(")
        preceding = source[max(0, index - 800):index]
        assert "_cli_mode_enabled()" in preceding

    def test_native_engine_checks_the_flag_before_delegating(self):
        source = _source("app/engine/native_engine.py")
        index = source.index("self._run_agent_via_cli(")
        preceding = source[max(0, index - 1200):index]
        assert "_cli_enabled" in preceding

    def test_a_missing_cli_runtime_package_cannot_break_a_run(self):
        """The import is wrapped so an incomplete deployment degrades to native
        rather than failing every request."""
        for relative in ("agent_factory/pipeline.py", "app/engine/native_engine.py"):
            source = _source(relative)
            index = source.index("from app.cli_runtime.config import cli_mode_enabled")
            preceding = source[max(0, index - 200):index]
            assert "try:" in preceding, relative

    def test_hitl_and_resume_stay_native(self):
        """Suspending mid-turn needs kill-and-resume, which is staged separately.
        The downgrade must be logged, never silent."""
        source = _source("app/engine/native_engine.py")
        index = source.index("self._run_agent_via_cli(")
        region = source[max(0, index - 1500):index]
        assert "hitl_mode" in region and "resume" in region
        assert "logger.info" in region


class TestNoSilentFallback:
    """The previous attempt shipped a silent fallback, so every CLI failure
    quietly became a native run — the feature looked healthy while never once
    using the CLI, and took two rounds of diagnostic logging to uncover."""

    def test_the_emergency_fallback_is_off_by_default(self):
        from app.cli_runtime.config import emergency_native_fallback
        assert emergency_native_fallback() is False

    def test_a_fallback_is_logged_loudly_at_both_call_sites(self):
        for relative in ("agent_factory/pipeline.py", "app/engine/native_engine.py"):
            source = _source(relative)
            index = source.index("emergency_native_fallback")
            region = source[index:index + 900]
            assert "logger.warning" in region, relative
            assert "EMERGENCY FALLBACK" in region, relative

    def test_a_failure_without_the_fallback_is_surfaced_not_swallowed(self):
        pipeline = _source("agent_factory/pipeline.py")
        index = pipeline.index("emergency_native_fallback")
        assert "raise RuntimeError" in pipeline[index:index + 1200]

        engine = _source("app/engine/native_engine.py")
        index = engine.index("emergency_native_fallback")
        # Window widened: the CLI failure branch now also records partial usage
        # (agent_usage) before yielding the error, so the error yield sits a bit
        # further down — still present and unconditional.
        assert 'yield "error"' in engine[index:index + 2600]


class TestImportHygiene:
    def test_the_config_module_pulls_in_no_heavy_dependencies(self):
        """A flag read must not drag in Postgres or FastAPI: it is called on every
        agent turn, including when the feature is off."""
        tree = ast.parse(_source("app/cli_runtime/config.py"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        allowed = {"os", "shutil", "dataclasses", "typing", "subprocess", "__future__"}
        assert imported <= allowed, f"unexpected imports: {imported - allowed}"

    def test_the_mcp_route_is_mounted_unconditionally(self):
        """Mounting only when the flag is on would mean a mid-flight flag change
        left the route missing and a live run's tools vanishing."""
        source = _source("app/main.py")
        index = source.index("from app.cli_runtime.mcp_router import router")
        region = source[max(0, index - 400):index]
        assert "cli_mode_enabled" not in region
