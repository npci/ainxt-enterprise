# SPDX-License-Identifier: Apache-2.0
"""cli_runtime.config — the single feature flag, operational knobs, and preflight.

Follows the established ``app/core/config.py`` pattern: one module-level function
per env var, every value read at CALL TIME (never cached at import) so an operator
can flip a knob without a process restart, and ``try/except ValueError`` around
every numeric parse with the literal default repeated.

The ONE behavioural flag is ``ABSTUDIO_CLI_MODE``. Everything else in this module
is operational (where the binary lives, how long to wait, how many at once) and
never changes *whether* the CLI is used.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import List

# Deliberately stdlib-only: this module must stay import-side-effect-free.
# Importing ``app.core.config`` would pull in ``app/core/__init__.py``, which
# imports the ~10k-line ``workflow_repo`` (and with it Postgres), so a cheap
# flag read would drag in the whole data layer. ``_TRUTHY`` mirrors
# ``app.core.config.env_flag`` exactly so behaviour stays consistent.
_TRUTHY = {"1", "true", "yes", "on"}


def env_flag(name: str, default: bool = False) -> bool:
    """Parse a boolean feature-flag env var (1/true/yes/on, case-insensitive).

    Kept byte-compatible with ``app.core.config.env_flag``.
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in _TRUTHY


# ════════════════════════════════════════════════════════════════════════════
# The single flag
# ════════════════════════════════════════════════════════════════════════════

def cli_mode_enabled() -> bool:
    """True when agent execution must go through the ``ainxt`` CLI.

    This is the ONLY switch that changes behaviour. When it is false (the
    default) every code path is byte-identical to the pre-CLI engine: the
    branches added to ``native_engine._run_agent`` and ``AgentRunner.run`` are
    skipped entirely and no subprocess, workspace or MCP session is created.
    """
    return env_flag("ABSTUDIO_CLI_MODE", False)


def emergency_native_fallback() -> bool:
    """Break-glass: fall back to the in-process engine when a CLI run fails.

    DEFAULT OFF, and deliberately so. The previous attempt at this feature
    shipped a silent fallback, which meant every CLI failure quietly degraded to
    native execution — the feature looked healthy while never once actually
    using the CLI, and two rounds of diagnostic logging were needed to discover
    it. When this is enabled every fallback is logged at WARNING with the reason.
    """
    return env_flag("ABSTUDIO_CLI_EMERGENCY_FALLBACK", False)


def expose_draft_tools() -> bool:
    """Include ``draft: True`` catalog tools (confluence_*, zoho_*, n8n_*, memory_*).

    Draft tools are not seeded into ``tools_catalog``, so they are invisible to
    the native engine. They can still be exposed over MCP by reading
    ``CANONICAL_TOOLS`` in memory. Off by default to keep parity with native.
    """
    return env_flag("ABSTUDIO_CLI_EXPOSE_DRAFT_TOOLS", False)


# ════════════════════════════════════════════════════════════════════════════
# Operational knobs
# ════════════════════════════════════════════════════════════════════════════

def cli_path() -> str:
    """Path to (or bare name of) the ``ainxt`` binary.

    Also the injection point for the fake-CLI test harness: point this at a
    Python script that speaks the same argv + ``streaming-json`` contract and the
    whole runtime is testable with no model traffic and no real binary.
    """
    return (os.getenv("ABSTUDIO_CLI_PATH", "") or "ainxt").strip()


def cli_api_key() -> str:
    """Platform API key exported to the child as ``AINXT_API_KEY``.

    This is how the spawned CLI authenticates to the gateway for LLM traffic.
    Preferring an explicit env var over ``~/.ainxt/config.json`` avoids the
    ``HOME``-dependency trap: under systemd/K8s ``HOME`` frequently is not the
    directory holding that config, and the CLI then exits with an auth error.
    Falls back to the platform-wide key so a single-key deployment works.
    """
    return (
        os.getenv("ABSTUDIO_CLI_API_KEY", "")
        or os.getenv("AINXT_API_KEY", "")
        or ""
    ).strip()


def mcp_base_url() -> str:
    """Base URL the spawned CLI uses to call BACK into this process.

    Must be reachable from the child. Default is loopback, which is correct
    whenever the CLI runs on the same host as the backend.

    If this is ever pointed at a non-loopback address it MUST use the TLS
    certificate's hostname rather than a bare IP: the CLI uses rustls for MCP
    connections, which rejects IP-based URLs with ``NotValidForName``.

    ``ABSTUDIO_CLI_MCP_LOOPBACK_URL`` takes precedence when set. Under a
    multi-worker gunicorn the session registry is per-process, so a callback
    routed by the kernel to a different worker returns 401 "unknown or
    expired run". Each worker publishes its own private loopback URL via
    ``post_worker_init`` in ``gunicorn.conf.py``, so every child dials back
    into the exact worker that spawned it.
    """
    override = os.getenv("ABSTUDIO_CLI_MCP_LOOPBACK_URL", "").strip().rstrip("/")
    if override:
        return override
    return (
        os.getenv("ABSTUDIO_MCP_BASE_URL", "") or "http://127.0.0.1:8000"
    ).strip().rstrip("/")


def mcp_server_name() -> str:
    """MCP server name as the CLI sees it.

    The CLI namespaces MCP tools as ``<server>__<tool>``, so this becomes a
    prefix on every exposed tool. Keep it short and free of underscores: the CLI
    splits on ``__`` and silently drops any tool name containing a second one.
    """
    return (os.getenv("ABSTUDIO_MCP_SERVER_NAME", "") or "abstudio").strip()


def max_concurrency() -> int:
    """Maximum concurrent ``ainxt`` subprocesses IN THIS PROCESS.

    Note the scoping: under gunicorn/uvicorn with N workers the effective host
    cap is N × this value, because the semaphore is per-process. For a true
    global cap, wire ``core.distributed_semaphore.DistributedSemaphore``.
    """
    try:
        return max(1, int(os.getenv("ABSTUDIO_CLI_MAX_CONCURRENCY", "5")))
    except (TypeError, ValueError):
        return 5


def run_timeout_s() -> int:
    """Wall-clock cap for a single CLI turn, in seconds.

    The floor is low (5s) purely so tests can exercise the timeout-and-kill path
    quickly; no sane deployment sets it anywhere near that.
    """
    try:
        return max(5, int(os.getenv("ABSTUDIO_CLI_RUN_TIMEOUT_S", "900")))
    except (TypeError, ValueError):
        return 900


def startup_timeout_s() -> int:
    """How long the CLI waits for our MCP server to answer ``initialize``.

    Small on purpose: the server is already running inside this process, so a
    slow handshake means something is wrong (wrong URL, wrong port) and failing
    fast beats hanging. A stdio sidecar would need ~60s for interpreter start-up.
    """
    try:
        return max(5, int(os.getenv("ABSTUDIO_CLI_MCP_STARTUP_TIMEOUT_S", "30")))
    except (TypeError, ValueError):
        return 30


def kill_grace_s() -> float:
    """Seconds between ``terminate()`` and ``kill()`` when stopping a child."""
    try:
        return max(0.5, float(os.getenv("ABSTUDIO_CLI_KILL_GRACE_S", "5")))
    except (TypeError, ValueError):
        return 5.0


def max_turns() -> int:
    """Cap on CLI agent turns. Mirrors the engine's ``AGENT_MAX_ITER_DEFAULT``."""
    try:
        return max(1, int(os.getenv("ABSTUDIO_CLI_MAX_TURNS", "20")))
    except (TypeError, ValueError):
        return 20


def workspace_ttl_seconds() -> int:
    """Age after which a per-run CLI workspace may be swept."""
    try:
        return max(300, int(os.getenv("ABSTUDIO_CLI_WORKSPACE_TTL_SECONDS", "86400")))
    except (TypeError, ValueError):
        return 86400


def prompt_file_threshold() -> int:
    """Prompt size (chars) above which the prompt is passed via ``--prompt-file``.

    Defaults to ``0`` — i.e. ALWAYS use a file. The CLI accepts the prompt as a
    single argv token, and passing it inline invites three separate failures:

    * **Size.** A big prompt (an inlined document, a large diff) exceeds the OS
      argument limit and the spawn dies with a usage error. This is the ARG_MAX
      failure the SDLC governance engine documents having hit in production.
    * **Quoting.** Any intermediate shell or launcher re-parses the argument
      string, and agent prompts routinely contain quotes and backticks.
    * **Newlines.** A multi-line prompt can be split into separate arguments,
      silently truncating it at the first line break — the prompt looks fine in
      the log and the model receives one line.

    A file has none of these properties, costs one small write per run against
    the cost of spawning a process, and keeps the prompt out of the process table
    (where it would otherwise be world-readable on a shared host). Raise this if
    you specifically want small prompts inline.
    """
    try:
        return max(0, int(os.getenv("ABSTUDIO_CLI_PROMPT_FILE_THRESHOLD", "0")))
    except (TypeError, ValueError):
        return 0


def max_tool_result_bytes() -> int:
    """Cap on a single tool result handed back to the CLI."""
    try:
        return max(4096, int(os.getenv("ABSTUDIO_CLI_MAX_TOOL_RESULT_BYTES", "900000")))
    except (TypeError, ValueError):
        return 900000


def validated_cli_versions() -> List[str]:
    """CLI versions this integration was verified against.

    Empty disables the check. The flag/event contract was validated on 0.2.101;
    a drift warning at startup is far cheaper to read than a run that fails
    mid-flight because a flag was renamed.
    """
    raw = os.getenv("ABSTUDIO_CLI_SUPPORTED_VERSIONS", "") or "0.2.101"
    return [v.strip() for v in raw.split(",") if v.strip()]


# ════════════════════════════════════════════════════════════════════════════
# Resolved snapshot
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CliRuntimeConfig:
    """Immutable snapshot of the runtime config for ONE spawn.

    Built via ``cli_runtime_config()`` at the start of each run so a mid-run env
    change cannot make a single spawn internally inconsistent (e.g. a timeout
    read before the change and a kill-grace read after it).
    """

    enabled: bool
    cli_path: str
    api_key: str
    mcp_base_url: str
    mcp_server_name: str
    max_concurrency: int
    run_timeout_s: int
    startup_timeout_s: int
    kill_grace_s: float
    max_turns: int
    workspace_ttl_seconds: int
    prompt_file_threshold: int
    max_tool_result_bytes: int
    expose_draft_tools: bool
    emergency_native_fallback: bool
    validated_versions: List[str] = field(default_factory=list)

    # ── derived ────────────────────────────────────────────────────────────
    @property
    def resolved_binary(self) -> str:
        """Absolute path to the binary, or "" when it cannot be found.

        Accepts both a bare name on ``PATH`` and an absolute path, matching how
        ``AINXT_CLI_BIN`` is used elsewhere in the platform.
        """
        p = self.cli_path
        if not p:
            return ""
        if os.path.isfile(p):
            return os.path.abspath(p)
        found = shutil.which(p)
        return found or ""

    @property
    def binary_available(self) -> bool:
        return bool(self.resolved_binary)

    def mcp_url_for(self, run_id: str) -> str:
        """Per-run MCP endpoint URL written into the workspace ``config.toml``."""
        return f"{self.mcp_base_url}/abstudio-mcp/{run_id}"


def cli_runtime_config() -> CliRuntimeConfig:
    """Read every knob once and return an immutable snapshot."""
    return CliRuntimeConfig(
        enabled=cli_mode_enabled(),
        cli_path=cli_path(),
        api_key=cli_api_key(),
        mcp_base_url=mcp_base_url(),
        mcp_server_name=mcp_server_name(),
        max_concurrency=max_concurrency(),
        run_timeout_s=run_timeout_s(),
        startup_timeout_s=startup_timeout_s(),
        kill_grace_s=kill_grace_s(),
        max_turns=max_turns(),
        workspace_ttl_seconds=workspace_ttl_seconds(),
        prompt_file_threshold=prompt_file_threshold(),
        max_tool_result_bytes=max_tool_result_bytes(),
        expose_draft_tools=expose_draft_tools(),
        emergency_native_fallback=emergency_native_fallback(),
        validated_versions=validated_cli_versions(),
    )


# ════════════════════════════════════════════════════════════════════════════
# Preflight
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class PreflightResult:
    """Outcome of the startup readiness check."""

    ready: bool
    enabled: bool
    binary: str = ""
    version: str = ""
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def as_log_fields(self) -> dict:
        return {
            "cli_mode": self.enabled,
            "ready": self.ready,
            "binary": self.binary or "(not found)",
            "version": self.version or "(unknown)",
            "problems": self.problems,
            "warnings": self.warnings,
        }


def probe_cli_version(binary: str, timeout: float = 10.0) -> str:
    """Return the CLI's version string, or "" if it cannot be determined.

    Read-only and model-free: runs ``<binary> --version`` and parses the output.
    Never raises.
    """
    if not binary:
        return ""
    import subprocess

    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return ""
    raw = ((proc.stdout or "") + " " + (proc.stderr or "")).strip()
    # Output looks like: "ainxt 0.2.101 (66fbf71)" → take the first dotted token.
    for token in raw.replace("(", " ").replace(")", " ").split():
        cleaned = token.strip().lstrip("v")
        if cleaned and cleaned[0].isdigit() and "." in cleaned:
            return cleaned
    return ""


def preflight(*, probe_version: bool = True) -> PreflightResult:
    """Check whether CLI mode can actually run, without spawning a model call.

    Called once from the app lifespan so a misconfiguration is visible in the
    startup log rather than surfacing as a failed user request. When the flag is
    off this reports ``enabled=False`` and checks nothing.
    """
    cfg = cli_runtime_config()
    if not cfg.enabled:
        return PreflightResult(ready=False, enabled=False)

    problems: List[str] = []
    warnings: List[str] = []

    binary = cfg.resolved_binary
    if not binary:
        problems.append(
            f"ainxt binary not found (ABSTUDIO_CLI_PATH={cfg.cli_path!r}) — "
            f"set it to the absolute path of the binary on this host"
        )
    if not cfg.api_key:
        problems.append(
            "no ABSTUDIO_CLI_API_KEY (or AINXT_API_KEY) — the spawned CLI would "
            "have no gateway credentials and exit with an auth error"
        )
    if not cfg.mcp_base_url:
        problems.append("ABSTUDIO_MCP_BASE_URL is empty — the CLI cannot reach the tool plane")

    version = probe_cli_version(binary) if (probe_version and binary) else ""
    if version and cfg.validated_versions and version not in cfg.validated_versions:
        warnings.append(
            f"ainxt {version} is outside the validated set "
            f"{cfg.validated_versions} — flag names and streaming-json event "
            f"types may have changed; re-verify before relying on this"
        )

    if "__" in cfg.mcp_server_name:
        problems.append(
            f"ABSTUDIO_MCP_SERVER_NAME={cfg.mcp_server_name!r} contains '__', which "
            f"is the CLI's own server/tool delimiter — every tool would be dropped"
        )

    if not cfg.mcp_base_url.startswith("http://127.0.0.1") and \
            not cfg.mcp_base_url.startswith("http://localhost"):
        warnings.append(
            "ABSTUDIO_MCP_BASE_URL is not loopback — it must use the TLS "
            "certificate's hostname (not a bare IP), because the CLI's rustls "
            "rejects IP-based URLs with NotValidForName"
        )

    return PreflightResult(
        ready=not problems,
        enabled=True,
        binary=binary,
        version=version,
        problems=problems,
        warnings=warnings,
    )


__all__ = [
    "cli_mode_enabled",
    "emergency_native_fallback",
    "expose_draft_tools",
    "cli_path",
    "cli_api_key",
    "mcp_base_url",
    "mcp_server_name",
    "max_concurrency",
    "run_timeout_s",
    "startup_timeout_s",
    "kill_grace_s",
    "max_turns",
    "workspace_ttl_seconds",
    "prompt_file_threshold",
    "max_tool_result_bytes",
    "validated_cli_versions",
    "CliRuntimeConfig",
    "cli_runtime_config",
    "PreflightResult",
    "probe_cli_version",
    "preflight",
]
