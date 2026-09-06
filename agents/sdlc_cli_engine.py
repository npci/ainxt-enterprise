# SPDX-License-Identifier: MIT
"""
agents/sdlc_cli_engine.py — Step 2: CLI adapter + replaceable engine seam.

Part of the 2026-06-27 CLI-loop rework (see docs/planning under the shift-left
RFD). This module wraps invocation of the `ainxt` CLI binary as a subprocess,
behind a small `AgentEngine` protocol so the concrete implementation
(`AinxtCliEngine` / `run_cli`) can be swapped out later without touching call
sites.

HARD CONSTRAINTS
----------------
- Import side-effect-free: only stdlib + `core.logger` at module import time.
  `core.model_registry` is imported LAZILY inside `_is_cli_forbidden_model` —
  no Postgres/Redis/Docker/network/heavy-model-registry import at import time.
- All env is read at CALL TIME (via `agents.sdlc_cli_utils._env_str` /
  `_env_int`), so a deploy-time env flip needs no restart.
- Suspend-not-fail: `run_cli` never raises on a normal failure (bad exit code,
  timeout, missing binary/key, blocked model) — it always returns a
  `CliResult` with `status="suspended"` and a `reason`.
- `spawn` is injectable (defaults to `subprocess.run`) so this is unit-testable
  on Windows without a real `ainxt` binary or real credentials.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from core.logger import logger

# Reused helpers — pure env readers + the truncated-JSON heuristic. These live in
# agents.sdlc_cli_utils, a stdlib+logger-only leaf module, so a top-level import
# does not pull in Postgres/Redis/Docker/network at import time.
from agents.sdlc_cli_utils import _env_str, _env_int, _service_api_key, _looks_truncated_json


# ════════════════════════════════════════════════════════════════════════════
# Constants
# ════════════════════════════════════════════════════════════════════════════

# The flag NAME used to advertise the coder/planner toolset to the CLI binary.
# `ainxt --help` (2026-07-13) confirms the binary accepts `--allowedTools, --allowed-tools
# <tools...>` — both spellings are aliases, so the NAME below was never the problem. The
# real bug was the VALUE FORMAT: `<tools...>` is variadic (space-separated, one token per
# tool). The engine used to pass the preset as a SINGLE comma-joined token, so the binary
# saw one unknown tool name, ignored it, and fell back to its default [Bash,Edit,Read] —
# dropping MultiEdit/Grep/Glob/Write. The split into separate tokens lives in _build_argv.
# Single source of truth (intentionally NOT an env var); `--allowedTools` is equivalent.
_ALLOWED_TOOLS_FLAG = "--allowed-tools"


# ════════════════════════════════════════════════════════════════════════════
# Config
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class CliEngineConfig:
    """Config for one `run_cli` invocation. Read at CALL TIME via
    `.from_env()` — never cached at import time, so env changes apply without
    a process restart."""
    binary_path: str = ""
    gateway_url: str = ""
    service_key: str = ""
    timeout_secs: int = 1800
    # CLI flavor: "v3" (default — the deployed ainxt 0.2.101+ headless contract),
    # "v2" (the older ainxt-v2 contract) or "v1" (the legacy ainxt --full path).
    # Differences, all handled in _build_argv:
    #   • v3: prompt via `-p`, toolset via `--tools`, auto-approve via `--always-approve`,
    #     schema INLINE via `--json-schema`, single-envelope `--output-format json`; NO
    #     --verbose/--no-review/--full. Envelope fields are camelCase (text/structuredOutput/
    #     sessionId) with a `stopReason` in place of is_error/subtype — see _parse_cli_envelope.
    #   • v1: requires a leading --full, schema INLINE via --json-schema, and
    #     --dangerously-skip-permissions for unattended runs (not --yes).
    #   • v2: --print, --output-schema FILE, --yes.
    flavor: str = "v3"
    # --resume capability seam — ON by default (2026-07-07 user decision: wire
    # IMPLEMENT auto-continue + manual resume, enabled by default). SUPERSEDES the
    # earlier default-OFF (memory project_sdlc_cli_perf_review_2026_07_04 B(i)) which
    # kept it off until server-verified. Risk if the server `ainxt` lacks --resume: a
    # resume call exits non-zero → suspended CliResult (safe under suspend-not-fail) —
    # set SDLC_CLI_RESUME_ENABLED=false to fall back to fresh sessions. Note: the flag
    # only takes effect when a caller supplies a resume_session_id (v2 only — see
    # _build_argv), so the FIRST PLAN/IMPLEMENT call is always fresh regardless.
    resume_enabled: bool = True
    resume_flag: str = "--resume"
    # --no-review seam — pass the CLI's --no-review flag so the binary SKIPS its
    # automatic post-change code-review (the /simplify step + /batch worker
    # checklist the agent self-invokes after edits). That per-phase review is what
    # made server runs 3–5× slower (PLAN + IMPLEMENT both, IMPLEMENT worst). ON by
    # default — that's the whole point; set SDLC_CLI_NO_REVIEW=false to restore the
    # CLI's built-in review. The flag lives in the deployed binary; if a CLI build
    # lacks it the spawn exits non-zero → suspended CliResult (safe, suspend-not-fail).
    no_review: bool = True
    # ── Real-time activity stream (2026-07-09) ──────────────────────────────────
    # Run the CLI in stream-json + verbose mode and TEE its stdout to a per-run
    # NDJSON file AS IT IS WRITTEN (not buffered to the end), so a stuck run can be
    # diagnosed from the last lines before it went silent — instead of 30 minutes of
    # nothing. Requires the updated binary (stream-json needs --verbose or it errors;
    # v1 --full also emits --include-hook-events / --include-partial-messages frames).
    # The updated binary additionally self-exits 124 on an internal stall. Set
    # SDLC_CLI_STREAM_JSON=false to fall back to single-envelope --output-format json
    # (no live stream). The final type=="result" envelope is still emitted in stream
    # mode, so _extract_result_envelope parses it exactly as before.
    stream_json: bool = True
    # Base dir for the NDJSON activity-stream files. Empty → <tempdir>/ainxt_sdlc_cli_logs.
    # Files land at <log_dir>/<run_id>/<profile>-<ts>-<rand>.ndjson (+ .err sidecar).
    log_dir: str = ""
    # Idle/stall watchdog control (exported as AINXT_STALL_TIMEOUT_MS). The updated
    # binary self-exits 124 when it makes no progress within its stall window (built-in
    # default 120s). That 120s was pre-empting normal PLAN/IMPLEMENT sessions — a single
    # long model turn on a read-only PLAN looks "idle" to the watchdog — so every run was
    # suspending with exit 124. Semantics (SDLC_CLI_STALL_TIMEOUT_MS):
    #   •  0 (DEFAULT) → DISABLE the idle watchdog: pin AINXT_STALL_TIMEOUT_MS just past
    #      our own wall-clock cap (timeout_secs) so the binary can never self-exit 124
    #      before our subprocess timeout fires. Real hang protection is still timeout_secs.
    #   • <0           → leave the binary's own default (120s) untouched — don't export
    #      (opt back into the old behavior).
    #   • >0           → export exactly this millisecond threshold.
    stall_timeout_ms: int = 0
    # ── Headless plugin-loading seam (governance-skill enforcement) ─────────────
    # These three flags let the EXACT mechanism the deployed `ainxt` binary uses to
    # load a plugin in HEADLESS mode be env-selected ONCE it is confirmed on the host,
    # WITHOUT another code change (plan prereq #1). The spellings below are GUESSES —
    # they MUST be overwritten from the confirmed working manual `/plugin` invocation.
    # Three known variants the binary might expose (only one is wired today):
    #   • repeatable per-plugin flag: `--plugin <name>` emitted once per requested
    #     plugin (the default assumed here — see `_plugin_argv`);
    #   • a marketplace / plugins-dir flag that points at where plugins live, emitted
    #     once BEFORE the per-plugin flags (`plugin_marketplace_flag`, e.g.
    #     `--plugin-dir` / `--marketplace`); empty ("") → not emitted;
    #   • a generated `--settings <file>` variant that enables plugins via a settings
    #     JSON (`plugins_settings_flag`) — documented seam only, NOT yet wired (no file
    #     generation here) until its spelling is confirmed.
    # When NO plugins are requested, none of these are emitted and argv is byte-identical
    # to today — see `_plugin_argv` (returns [] for falsy plugins) and `_build_argv`.
    plugins_flag: str = "--plugin"
    plugin_marketplace_flag: str = ""
    plugins_settings_flag: str = ""

    @classmethod
    def from_env(cls) -> "CliEngineConfig":
        return cls(
            binary_path=_env_str("SDLC_CLI_BINARY_PATH", ""),
            gateway_url=_env_str("SDLC_CLI_GATEWAY_URL", ""),
            # Fail-closed if empty, never a user JWT — mirrors
            # sdlc_cli_utils._service_api_key's fail-closed semantics.
            service_key=_service_api_key() or _env_str("SDLC_SERVICE_API_KEY", ""),
            timeout_secs=_env_int("SDLC_CLI_TIMEOUT_SECS", 1800),
            flavor=(_env_str("SDLC_CLI_FLAVOR", "v3").strip().lower() or "v3"),
            resume_enabled=(_env_str("SDLC_CLI_RESUME_ENABLED", "true").strip().lower() in ("1", "true", "yes")),
            resume_flag=_env_str("SDLC_CLI_RESUME_FLAG", "--resume"),
            no_review=(_env_str("SDLC_CLI_NO_REVIEW", "true").strip().lower() in ("1", "true", "yes")),
            stream_json=(_env_str("SDLC_CLI_STREAM_JSON", "true").strip().lower() in ("1", "true", "yes")),
            log_dir=_env_str("SDLC_CLI_LOG_DIR", ""),
            stall_timeout_ms=_env_int("SDLC_CLI_STALL_TIMEOUT_MS", 0),
            # Plugin-loading seam — spellings are GUESSES, set from the confirmed
            # working manual `/plugin` command on the host (plan prereq #1).
            plugins_flag=_env_str("SDLC_CLI_PLUGIN_FLAG", "--plugin"),
            plugin_marketplace_flag=_env_str("SDLC_CLI_PLUGIN_MARKETPLACE_FLAG", ""),
            plugins_settings_flag=_env_str("SDLC_CLI_PLUGIN_SETTINGS_FLAG", ""),
        )


# ════════════════════════════════════════════════════════════════════════════
# Result
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class CliResult:
    status: str                                  # "completed" | "suspended"
    reason: str = ""
    result_text: str = ""
    structured_output: Optional[dict] = None
    is_error: bool = False
    subtype: str = ""
    exit_code: int = 0
    usage: dict = field(default_factory=dict)
    total_cost_usd: float = 0.0
    session_id: str = ""
    transient: bool = False                      # retryable upstream/proxy blip (502/api_error)
    num_turns: int = -1                           # v3 envelope's num_turns; -1 = unknown/unavailable

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    @property
    def suspended(self) -> bool:
        return self.status == "suspended"


# ════════════════════════════════════════════════════════════════════════════
# Profile presets
# ════════════════════════════════════════════════════════════════════════════

# profile -> (permission_mode, allowed_tools)
#
# Unattended auto-approval is NOT per-profile: every headless phase (plan, code,
# govreview, govscan) runs without an approver on the other end, so _build_argv
# always emits the auto-approve flag (v1: --dangerously-skip-permissions,
# v2: --yes). A per-profile opt-in only produced "commands require approval"
# stalls in the governance review/scan sessions, which need Bash to run the
# skill analyzer scripts.
#
# The "code" profile deliberately grants the SAME toolset a human gets when
# running the CLI standalone in auto/acceptEdits mode. Restricting it to
# Read,Edit,Bash bought NO safety (in acceptEdits, Bash is already a superset —
# it can grep, create, and edit any file) while FORCING slow workarounds: the
# coder had to shell out to `bash grep`/`find` instead of native Grep/Glob, and
# create new files (plan.new_files_needed) via `bash` heredocs instead of Write.
# That inflated both turn count and wall-clock. Native Grep/Glob/Write/MultiEdit
# let IMPLEMENT converge in far fewer turns.
# The "govreview" profile is the governance-skill REVIEWER pass: it reads the diff
# and runs skill helper scripts, but must NOT itself mutate the tree — so it grants
# Read/Grep/Glob plus Bash (to run the skill's helper scripts) but withholds
# Write/Edit/MultiEdit — the withheld toolset, not permission_mode, is what keeps the
# reviewer from editing. Bash is required: the skill helper scripts run through it, and
# blocking its approval prompt is exactly what stalled this profile before.
# NOTE: the governance FIXER (which DOES mutate the tree) reuses the existing "code"
# profile plus the loaded plugins — NOT "govreview".
# The "govscan" profile is the governance-skill SCANNER pass: it grants
# Read/Grep/Bash (no Write/Edit/MultiEdit/Glob) so scan sessions cannot mutate
# tracked files via a file tool. It keeps plan permission-mode (not acceptEdits) to
# signal intent, but with auto-approval unconditional the real read-only guarantee is
# the withheld toolset plus the post-scan git diff guard in run_scan_session, which
# discards any residual tracked-file mutation.
_PROFILE_PRESETS: dict[str, tuple[str, str]] = {
    # The "plan" profile is the read-only exploration toolset shared by the CLASSIFY
    # and PLAN phases. Bash is granted IN ADDITION to Read/Grep/Glob so the model has a
    # fallback way to locate files/symbols (ls/find/case-insensitive grep) when a
    # structured Grep/Glob misses — the classify phase used to have no such fallback and
    # could loop on a single failed name search.
    #
    # permission_mode is "auto", NOT "plan" — despite the profile's name. Confirmed
    # live that literal `--permission-mode plan` silently cancels the whole headless
    # session (not just a denied tool call) both when the model calls its native
    # exit_plan_mode tool AND when it calls plain run_terminal_command (Bash) —
    # see the "Universal headless normalization" comment in _build_argv for the
    # full writeup. Read-only-ness instead rests on the WITHHELD toolset, enforced
    # via `--disallowed-tools` (v3's real tool ids, derived in _build_argv from the
    # absence of Write/Edit/MultiEdit here) — not on permission_mode. Same posture
    # as the "govscan" profile below.
    "plan": ("auto", "Read,Grep,Glob,Bash"),
    "code": ("acceptEdits", "Read,Write,Edit,MultiEdit,Grep,Glob,Bash"),
    "govreview": ("acceptEdits", "Read,Grep,Glob,Bash"),
    "govscan": ("auto", "Read,Grep,Bash"),
}


def _profile_preset(profile: str) -> tuple[str, str]:
    """Resolve (permission_mode, allowed_tools) for a profile.
    Unknown profiles conservatively fall back to the 'plan' preset (read-only
    permission-mode, exploration toolset) rather than something permissive."""
    return _PROFILE_PRESETS.get(profile, _PROFILE_PRESETS["plan"])


# ════════════════════════════════════════════════════════════════════════════
# Model guard (Step 8 hook — the resolver itself lives in core/model_registry.py)
# ════════════════════════════════════════════════════════════════════════════

def _is_cli_forbidden_model(model_id: str) -> bool:
    """True if `model_id` is in BLOCKED_MODELS and so must never reach a CLI phase.

    BLOCKED_MODELS is the single source of truth: it already encodes the retired
    ids plus the import-time kill-switches (ENABLE_OPUS / ENABLE_CLI_OPUS_48 /
    ENABLE_CLI_OPUS_5 / ENABLE_SONNET_5), so any model an operator has enabled —
    Opus included — may run any CLI phase.

    ENABLE_OPUS is additionally re-read at CALL time (mirroring
    core.model_registry.cli_model_for_tier) so flipping the kill-switch off takes
    effect without a restart even though BLOCKED_MODELS was built at import.

    core.model_registry is imported LAZILY here (not at module top) to keep
    this module import side-effect-free."""
    if not isinstance(model_id, str) or not model_id.strip():
        return True  # empty/invalid model id — fail closed, never spawn
    mid = model_id.strip().lower()
    try:
        from core.model_registry import (
            BLOCKED_MODELS, CLAUDE_OPUS_MODEL, CLAUDE_OPUS_48_MODEL, CLAUDE_OPUS_5_MODEL,
        )
        blocked = {m.lower() for m in BLOCKED_MODELS}
        if os.getenv("ENABLE_OPUS", "true").strip().lower() not in ("true", "1", "yes"):
            blocked |= {
                m.lower() for m in
                (CLAUDE_OPUS_MODEL, CLAUDE_OPUS_48_MODEL, CLAUDE_OPUS_5_MODEL)
            }
        if mid in blocked:
            return True
    except Exception as e:  # pragma: no cover - defensive; never let this crash the guard
        logger.warning(f"[SDLC-CLI] model guard: could not import BLOCKED_MODELS: {e}")
    return False


# ════════════════════════════════════════════════════════════════════════════
# Argv construction
# ════════════════════════════════════════════════════════════════════════════

def _plugin_argv(config: CliEngineConfig, plugins, marketplace_dir: str = "") -> list:
    """Build the argv fragment that loads governance plugins into a headless CLI run.

    PURE + testable: takes only the config + the requested plugin names (+ an optional
    marketplace/plugins dir) and returns a flat list of argv tokens. No env reads, no
    I/O, no side effects.

    CRITICAL — ADDITIVE CONTRACT: returns `[]` when `plugins` is falsy (None or empty),
    so a caller that passes no plugins gets NOTHING spliced into argv → the argv is
    byte-identical to the pre-plugin engine. This is the #1 acceptance criterion; every
    existing PLAN/IMPLEMENT/REVIEW call passes no plugins and MUST be unaffected.

    Emission order when plugins ARE requested:
      1. If both `config.plugin_marketplace_flag` and `marketplace_dir` are set, emit
         `[flag, marketplace_dir]` FIRST — the binary needs to know WHERE plugins live
         before it can load them by name.
      2. Then, for each non-blank plugin name, emit `[config.plugins_flag, name]`
         (repeatable — one flag+value pair per plugin).

    The `config.plugins_settings_flag` (generated `--settings <file>`) variant is a
    documented NOT-YET-WIRED seam: once its exact spelling is confirmed on the host
    (plan prereq #1), the settings-file variant would be emitted here instead of / in
    addition to the repeatable `--plugin` flags. No file generation is implemented now.

    NOTE: all flag spellings come from `config` and are GUESSES until confirmed against
    the working manual `/plugin` command — that's why they are env-overridable."""
    if not plugins:
        return []  # additive guarantee: no plugins → no argv change
    out: list = []
    # (1) marketplace / plugins-dir flag first, only when both flag + dir are present.
    if config.plugin_marketplace_flag and marketplace_dir:
        out += [config.plugin_marketplace_flag, marketplace_dir]
    # (2) one repeatable per-plugin flag per requested (non-blank) name.
    for name in plugins:
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name:
            continue
        out += [config.plugins_flag, name]
    return out


def _build_argv(
        *,
        config: CliEngineConfig,
        prompt: str,
        permission_mode: str,
        model: str,
        output_schema_path: Optional[str],
        output_schema_inline: Optional[str],
        max_turns: Optional[int],
        resume_session_id: str = "",
        plugins: Optional[list] = None,
        plugin_marketplace: str = "",
        allowed_tools: str = "",
) -> list:
    is_v1 = config.flavor == "v1"
    is_v3 = config.flavor == "v3"
    # ── Universal headless normalization (applies to v1, v2 and v3) ──────────────
    # Three defects were seen against the deployed v3 binary but are safe to
    # normalize for every flavor:
    #   1. `--permission-mode acceptEdits` (and `default`/`dontAsk`) silently CANCEL
    #      every tool call in headless mode even with `--always-approve` present —
    #      only `auto` and `bypassPermissions` actually let the model call Write/Edit.
    #      Symptom: envelope returns `stopReason: "Cancelled"`, num_turns≈2–3, empty
    #      workspace diff (exactly the IMPLEMENT-phase failure in run
    #      791f86cd-01ca-4370-bb59-2b3517d7aa20). We map the three broken modes to
    #      `auto`; `bypassPermissions` is left alone.
    #   2. `--permission-mode plan` is ALSO broken headlessly, in TWO distinct ways
    #      confirmed live (run d2b05274, 7+ reproductions + `--debug` replays against
    #      the real binary): (a) it advertises the CLI's native enter_plan_mode/
    #      exit_plan_mode tools (its interactive "Plan Mode" UX), and exit_plan_mode
    #      is an `ext_method` requiring a live ACP CLIENT round-trip approval that
    #      does not exist in headless single-turn (-p) mode ("channel closed"); AND
    #      (b) independently of that, it ALSO silently cancels a plain
    #      `run_terminal_command` (Bash) call the same way `acceptEdits`/`default`/
    #      `dontAsk` cancel Write/Edit above — despite `--always-approve` and despite
    #      the "plan" preset explicitly granting Bash (see _PROFILE_PRESETS). Both
    #      manifest identically: envelope `stopReason: "Cancelled"`,
    #      `cancellationCategory: "PermissionCancelled"`, is_error, subtype mapped to
    #      "error_max_turns" regardless of how many turns were actually used (1, or
    #      after several turns of real work — see verified replay of run d2b05274).
    #      There is no combination of flags that makes `plan` mode reliable headless
    #      — only `auto`/`bypassPermissions` are. So `plan` is retired as a
    #      permission_mode value entirely (see _PROFILE_PRESETS: the "plan"/
    #      "govscan" profiles now resolve to `auto`); read-only-ness for those
    #      profiles is enforced via `--disallowed-tools` instead (see below).
    #   3. `--tools <legacy-names>` (Read,Write,Edit,MultiEdit,Grep,Glob,Bash) is a
    #      hard allow-list in v3 and NONE of those names match the real v3 tool ids
    #      (read_file/write/search_replace/list_dir/grep/run_terminal_command), so
    #      every tool the model tries is filtered out. With `--always-approve` on,
    #      the allow-list is unnecessary in the first place — drop it universally.
    _HEADLESS_BLOCKED_MODES = {"default", "acceptEdits", "dontAsk", "plan"}
    if permission_mode in _HEADLESS_BLOCKED_MODES:
        permission_mode = "auto"
    # allowed_tools is a v1/v2-spelled allow-list string (e.g. "Read,Grep,Glob,Bash")
    # that is NOT forwarded as an allow-list to any flavor (see point 3 above — v3's
    # real tool ids don't match these names, so an allow-list would filter out every
    # tool). For v3 we instead derive a DISALLOW-list from it: a profile that
    # withholds Write/Edit/MultiEdit (the "plan"/"govscan" read-only presets) maps to
    # v3's real mutating-tool ids so read-only-ness is enforced by an actual denied
    # tool call rather than by permission_mode (which point 2 above proved
    # insufficient — and actively harmful — for this). `--disallowed-tools` is
    # honored by exact v3 tool id; verified live that a call requesting a disallowed
    # tool is denied gracefully (the model adapts and continues) rather than
    # cancelling the whole session, unlike the permission_mode="plan" defect above.
    _v3_disallowed_tools = (
        "write,search_replace"
        if not any(t in allowed_tools for t in ("Write", "Edit", "MultiEdit"))
        else ""
    )
    # ── v3 (ainxt 0.2.101+) headless contract ────────────────────────────────────
    # The deployed CLI is now v3-only. Its headless flag/envelope contract differs from
    # v1/v2 (verified against `ainxt --help` on the host): the prompt goes via
    # `-p/--single` (no --print/--full), auto-approval via `--always-approve` (not
    # --yes/--dangerously-skip-permissions), the schema INLINE via `--json-schema`,
    # and there is NO --verbose/--no-review/stream-frame flags. Output is forced to
    # single-envelope `json` (streaming-json uses a different per-token frame format
    # we intentionally do not parse). resume/--max-turns/--permission-mode unchanged.
    # Plugins reuse the existing seam.
    if is_v3:
        argv = [config.binary_path, "-p", prompt, "--output-format", "json",
                "--model", model, "--permission-mode", permission_mode,
                "--always-approve"]
        if _v3_disallowed_tools:
            argv += ["--disallowed-tools", _v3_disallowed_tools]
        argv += _plugin_argv(config, plugins, plugin_marketplace)
        if output_schema_inline:
            argv += ["--json-schema", output_schema_inline]
        if max_turns is not None:
            argv += ["--max-turns", str(max_turns)]
        if config.resume_enabled and resume_session_id:
            argv += [config.resume_flag, resume_session_id]
        return argv
    argv = [config.binary_path]
    # v1's --full path (the older ainxt) requires --full before the headless flags.
    if is_v1:
        argv.append("--full")
    argv += [
        "--print", prompt,
        "--output-format", ("stream-json" if config.stream_json else "json"),
        "--model", model,
        "--permission-mode", permission_mode,
    ]
    if config.stream_json:
        # --verbose is REQUIRED for stream-json (the binary errors out without it).
        argv.append("--verbose")
        if is_v1:
            # v1 (--full) also emits richer per-event frames that make a stall
            # diagnosable — which tool/hook/message was in flight when it went silent.
            argv += ["--include-hook-events", "--include-partial-messages"]
    if config.no_review:
        # Skip the CLI's automatic post-change code-review (/simplify + the /batch
        # worker checklist). This is the per-phase review that dominated server-side
        # latency; orthogonal to flavor, so emit for both v1 (--full) and v2.
        argv.append("--no-review")
    # Every headless run is unattended — there is no approver to answer a prompt, so
    # auto-approval is unconditional for all profiles (a per-profile opt-in left the
    # governance review/scan sessions stalling on "commands require approval" for the
    # Bash calls their analyzer scripts need).
    # v1: --dangerously-skip-permissions. v2: --yes.
    argv.append("--dangerously-skip-permissions" if is_v1 else "--yes")
    # ── Plugin-loading seam ─────────────────────────────────────────────────────
    # Splice in the governance plugin flags (if any) right after the toolset block and
    # before the schema flags — a stable spot. `_plugin_argv` returns [] when `plugins`
    # is None/empty, so this line is a NO-OP for every existing PLAN/IMPLEMENT/REVIEW
    # caller and argv stays byte-identical to today. Only governance callers pass plugins.
    argv += _plugin_argv(config, plugins, plugin_marketplace)
    if is_v1:
        # v1 takes the schema INLINE (--json-schema works on both v1 builds;
        # --output-schema is a same-session alias). Skip when no schema.
        if output_schema_inline:
            argv += ["--json-schema", output_schema_inline]
    elif output_schema_path:
        # v2: a JSON-schema FILE path.
        argv += ["--output-schema", output_schema_path]
    if max_turns is not None:
        argv += ["--max-turns", str(max_turns)]
    # --resume seam: only emit for v2 (the flavor known to accept it), and only
    # when both the config gate and a caller-supplied session id are truthy.
    # Fresh session (no --resume) otherwise.
    if config.resume_enabled and resume_session_id and not is_v1:
        argv += [config.resume_flag, resume_session_id]
    return argv


def _child_env(config: CliEngineConfig) -> dict:
    """Child env = a copy of the current process env (preserves HTTP(S)_PROXY /
    NO_PROXY / anything else already set) plus the gateway URL and service
    credentials the CLI needs. Both ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN
    are set to the service key — the real CLI reads either; harmless for a
    fake/test double.

    AINXT_GATEWAY_URL: used by the CLI binary for its own settings/policy endpoints
    (appends /api/claude_code/... to this base). Keep as the full endpoint path.

    ANTHROPIC_BASE_URL: used by the Anthropic SDK inside the CLI to construct the
    messages endpoint (appends /v1/messages). Must be the base WITHOUT /v1/messages
    suffix, i.e. http://host/ainxt/v1/api — not the full endpoint path.
    Without this, the SDK hits Anthropic directly with the platform JWT → auth failure.

    AINXT_API_KEY: gateway Bearer auth token for the CLI binary's own requests."""
    env = os.environ.copy()
    # Treat an empty/blank AINXT_HOME (e.g. a stray `AINXT_HOME=` line in .env) as
    # unset: drop it so the CLI falls back to ~/.ainxt instead of resolving its home
    # to "" and writing into the wrong workspace root.
    if not (env.get("AINXT_HOME") or "").strip():
        env.pop("AINXT_HOME", None)
    env["AINXT_GATEWAY_URL"] = config.gateway_url
    env["AINXT_API_KEY"] = config.service_key
    env["ANTHROPIC_API_KEY"] = config.service_key
    env["ANTHROPIC_AUTH_TOKEN"] = config.service_key
    # ANTHROPIC_BASE_URL must NOT include /v1/messages — the Anthropic SDK appends
    # that path itself. AINXT_GATEWAY_URL ends in /v1/messages (the full endpoint);
    # strip it to get the base URL the SDK needs.
    _base = config.gateway_url
    if _base.endswith("/v1/messages"):
        _base = _base[: -len("/v1/messages")]
    env["ANTHROPIC_BASE_URL"] = _base
    # Disable ANSI color so no escape codes leak into the JSON envelope on stdout
    # (belt-and-suspenders; a piped, non-TTY subprocess usually disables color).
    env["NO_COLOR"] = "1"
    # v1 CLI running outside sandbox: auto-enable AINXT_BYPASS_ACK to allow
    # --dangerously-skip-permissions. This is safe for non-internet-connected
    # deployments; for internet-facing hosts, upgrade to v2 (uses --yes instead).
    if config.flavor == "v1":
        if not env.get("AINXT_BYPASS_ACK"):
            env["AINXT_BYPASS_ACK"] = "1"
    # Idle/stall watchdog (see CliEngineConfig.stall_timeout_ms for the full contract).
    #   >0 → use exactly this threshold.
    #    0 → DISABLE: pin the watchdog just PAST our wall-clock cap so the binary never
    #        self-exits 124 before our own subprocess timeout does. This is the default,
    #        so a fresh deploy no longer suspends every PLAN run on the binary's 120s.
    #   <0 → leave the binary's own default (120s) untouched — don't export.
    st = config.stall_timeout_ms
    if st > 0:
        env["AINXT_STALL_TIMEOUT_MS"] = str(st)
    elif st == 0:
        # Exceed the wall-clock cap by a margin (in ms) so the watchdog cannot fire first;
        # a stalled run then ends via our TimeoutExpired path (exit -1, subtype "timeout")
        # with session-id recovery, instead of the binary's 124 self-exit.
        env["AINXT_STALL_TIMEOUT_MS"] = str((max(config.timeout_secs, 60) + 120) * 1000)
    # st < 0 → do not export; the binary keeps its own default.
    return env


# ════════════════════════════════════════════════════════════════════════════
# Failure mapping (suspend-not-fail)
# ════════════════════════════════════════════════════════════════════════════

_EXIT_CODE_REASONS: dict[int, str] = {
    2: "bad CLI usage",
    3: "auth: provision ~/.ainxt/credentials.json",
    4: "gateway/network 5xx",
    5: "tool failure",
    124: "stalled — the CLI made no progress within the stall timeout (binary self-exit 124)",
    130: "interrupted",
}


def _reason_for_exit_code(exit_code: int) -> str:
    return _EXIT_CODE_REASONS.get(exit_code, f"CLI exited with code {exit_code}")


# ── Transient upstream-failure detection ────────────────────────────────────
# The external CLI's exit code / envelope subtype for a mid-stream gateway 502 is NOT
# reliably a clean 5xx: the gateway commits a 200 + SSE headers before the proxy call,
# so a proxy 502 arrives in-band as an SSE api_error on a 200 response. Detection is
# therefore by-EXCLUSION + buffer pattern-match, NOT a single hardcoded exit code:
#   • NEVER transient: bad usage (2), auth (3), tool failure (5), missing binary
#     (spawn_error) — deterministic, must not be retried.
#   • transient if: exit 4 (platform maps this to "gateway/network 5xx"), OR the subtype
#     is in the operator-configured SDLC_CLI_TRANSIENT_SUBTYPES set, OR the captured
#     stdout/stderr/stream buffer matches a known upstream-error signature below.
_TRANSIENT_OUTPUT_PATTERNS = [
    r"\bapi_error\b",
    r"upstream (?:llm )?error",
    r"\bbad gateway\b",
    r"\bgateway time-?out\b",
    r"\bservice unavailable\b",
    r"\boverloaded_error\b",
    r"\b50[234]\b",                 # 502 / 503 / 504
    r"connection reset",
    r"connection aborted",
    r"temporarily unavailable",
    r"\beof occurred\b",           # TLS EOF mid-stream
]
_NON_TRANSIENT_EXIT_CODES = (2, 3, 5)          # usage / auth / tool — never retry
_NON_TRANSIENT_SUBTYPES   = ("spawn_error",)   # missing binary — never retry
_TRANSIENT_BACKOFF_BASE_SECS = 2.0
_TRANSIENT_BACKOFF_CAP_SECS  = 30.0


def _transient_subtypes() -> set:
    """Operator-configured extra CLI envelope subtypes to treat as transient. Env
    SDLC_CLI_TRANSIENT_SUBTYPES = comma-separated list (case-insensitive). Empty by
    default — detection then relies on exit 4 + the buffer patterns. Populate this
    once the real subtype for an in-band 502 is observed in the [SDLC-CLI] exit log."""
    import os
    raw = os.getenv("SDLC_CLI_TRANSIENT_SUBTYPES", "") or ""
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def _is_transient_failure(
    exit_code: int, subtype: str, buffers: str,
    num_turns: int = -1, max_turns: Optional[int] = None,
) -> bool:
    """True iff a suspended CLI outcome looks like a RETRYABLE upstream/proxy blip
    (502/503/api_error/connection reset), by exclusion. Deterministic failures
    (bad usage / auth / tool / missing binary) are never transient."""
    sub = (subtype or "").lower()
    # by-exclusion: deterministic, non-retryable failures
    if exit_code in _NON_TRANSIENT_EXIT_CODES:
        return False
    if sub in _NON_TRANSIENT_SUBTYPES:
        return False
    # positive signals
    if sub and sub in _transient_subtypes():
        return True
    if exit_code == 4:             # platform maps exit 4 → gateway/network 5xx
        return True
    # Spurious low-turn cancellation: v3 maps ANY non-EndTurn stopReason (not just a
    # genuine --max-turns exhaustion) to subtype "error_max_turns" (see
    # _parse_cli_envelope). Observed live (run d2b05274, PLAN, 4/4 reproductions):
    # the binary reports stopReason "Cancelled" after exactly 1 turn — with
    # structuredOutputError "model did not produce structured output" — while
    # max-turns was 60. A run cannot legitimately exhaust a 60-turn budget in 1-2
    # turns, so this is the binary/harness cancelling early, not real exhaustion.
    # Retrying is safe: PLAN/CLASSIFY callers that opt into transient_retries are
    # read-only (wrote nothing), and IMPLEMENT reads .transient to continue the
    # same session rather than treat it as a real budget exhaustion.
    if (
        sub == "error_max_turns" and 0 <= num_turns <= 2
        and isinstance(max_turns, int) and max_turns >= 10
    ):
        return True
    # Match both the raw buffers AND the subtype string against the upstream-error
    # signatures — the real in-band 502 may surface as an api_error subtype, an
    # api_error SSE line in stdout, or a 502/reset in stderr.
    hay = ((buffers or "") + "\n" + (subtype or "")).lower()
    if not hay.strip():
        return False
    import re as _re
    return any(_re.search(p, hay) for p in _TRANSIENT_OUTPUT_PATTERNS)


def _trunc(s: str, n: int) -> str:
    """Head-truncate a string for logging (keeps logs bounded; marks elision)."""
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + f"…[+{len(s) - n} chars truncated]"


# ════════════════════════════════════════════════════════════════════════════
# run_cli
# ════════════════════════════════════════════════════════════════════════════

def run_cli(
        *,
        config: CliEngineConfig,
        workspace_root: str,
        prompt: str,
        profile: str,
        model: str,
        output_schema: Optional[dict] = None,
        max_turns: Optional[int] = None,
        run_id: str = "",
        resume_session_id: str = "",
        plugins: Optional[list] = None,
        plugin_marketplace: str = "",
        transient_retries: int = 0,
        _transient_attempt: int = 0,
        spawn=subprocess.run,
) -> CliResult:
    """Spawn the `ainxt` CLI as a subprocess and parse its single-envelope JSON
    output. Never raises on a normal failure — non-zero exit, `is_error`, or a
    timeout all map to a suspended CliResult with a reason keyed off the exit
    code. `spawn` is injectable for tests (defaults to `subprocess.run`).

    `transient_retries` (default 0 → unchanged behaviour) enables a bounded FRESH
    re-spawn on a detected transient upstream/proxy failure (502/api_error). Only
    read-only callers (PLAN/CLASSIFY/PLAN-fix, profile="plan") that wrote nothing
    should opt in — a fresh re-run is unsafe for a phase that mutated the workspace.
    IMPLEMENT (profile="code") keeps the default 0 and instead reads `result.transient`
    at its call site to CONTINUE the same session on the untouched workspace.
    `_transient_attempt` is internal (recursion depth for backoff) — do not pass it."""
    permission_mode, allowed_tools = _profile_preset(profile)

    # ── pre-spawn guards (fail-closed; never spawn) ─────────────────────────
    if not config.service_key:
        reason = "no SDLC_SERVICE_API_KEY configured — refusing to spawn CLI"
        logger.warning("[SDLC-CLI] fail-closed guard", run_id=run_id, profile=profile, reason=reason)
        return CliResult(status="suspended", reason=reason)

    if not config.binary_path:
        reason = "no SDLC_CLI_BINARY_PATH configured — refusing to spawn CLI"
        logger.warning("[SDLC-CLI] fail-closed guard", run_id=run_id, profile=profile, reason=reason)
        return CliResult(status="suspended", reason=reason)

    if _is_cli_forbidden_model(model):
        reason = f"model guard: {model} not allowed for CLI phase"
        logger.warning("[SDLC-CLI] fail-closed guard", run_id=run_id, profile=profile, reason=reason)
        return CliResult(status="suspended", reason=reason)

    # An empty/blank workspace_root means the checkout was never materialized —
    # spawning would call subprocess with cwd='' and surface the opaque
    # "failed to spawn CLI: [Errno 2] No such file or directory: ''". The usual
    # root cause is a missing per-user GitLab token (early-checkout could not
    # clone). Fail closed here with an actionable reason instead of an errno.
    if not (workspace_root or "").strip():
        reason = (
            "workspace was not materialized (likely missing GitLab token) — "
            "add your PAT under Profile → GitLab Token, then re-run"
        )
        logger.warning(
            "[SDLC-CLI] fail-closed guard", run_id=run_id, profile=profile,
            reason=reason, workspace_root=workspace_root,
        )
        return CliResult(status="suspended", reason=reason)

    logger.info(
        "[SDLC-CLI] spawn start", run_id=run_id, profile=profile, model=model,
        workspace_root=workspace_root, binary=config.binary_path,
        # Log the REQUESTED toolset + the flag used, so a mismatch against the tools
        # the CLI's `init` event actually registers is diagnosable straight from the
        # worker log (see Defect 2 #1 — MultiEdit/Grep/Glob/Write being dropped).
        allowed_tools_requested=allowed_tools, allowed_tools_flag=_ALLOWED_TOOLS_FLAG,
        permission_mode=permission_mode,
    )

    schema_path: Optional[str] = None
    schema_inline: Optional[str] = None
    tmp_fd = None
    try:
        if output_schema is not None:
            if config.flavor in ("v1", "v3"):
                # v1 and v3 take the schema INLINE (--json-schema) — no temp file.
                schema_inline = json.dumps(output_schema)
            else:
                tmp_fd, schema_path = tempfile.mkstemp(
                    prefix=".sdlc_cli_schema_", suffix=".json", dir=workspace_root or None,
                )
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(output_schema, f)
                tmp_fd = None  # fdopen closed it; don't double-close in finally

        argv = _build_argv(
            config=config, prompt=prompt, permission_mode=permission_mode,
            model=model,
            output_schema_path=schema_path, output_schema_inline=schema_inline,
            max_turns=max_turns, resume_session_id=resume_session_id,
            plugins=plugins, plugin_marketplace=plugin_marketplace,
            allowed_tools=allowed_tools,
        )
        resume_used = bool(config.resume_enabled and resume_session_id and config.flavor != "v1")
        logger.info(
            "[SDLC-CLI] resume decision", run_id=run_id, profile=profile,
            resume_enabled=config.resume_enabled, resume_used=resume_used,
            session_id=resume_session_id or "",
        )
        # ── Log the exact command for offline reproduction ──
        # This allows extracting the exact invocation and prompt to iterate offline
        logger.info(
            "[SDLC-CLI] command-for-reproduction", run_id=run_id, profile=profile,
            exact_command=" ".join(argv),
            # Fold the requested plugins into the reproduction record so the exact
            # governance invocation is replayable offline. Empty [] for every non-
            # governance (PLAN/IMPLEMENT/REVIEW) caller — see plan prereq #1.
            plugins=plugins or [],
        )
        logger.info(
            "[SDLC-CLI] prompt-for-reproduction", run_id=run_id, profile=profile,
            prompt=prompt,
            workspace_root=workspace_root,
            model=model,
            permission_mode=permission_mode,
            max_turns=max_turns,
        )
        env = _child_env(config)

        # ── Prepare the per-run activity-stream capture file (stream mode only). The
        #    CLI's stdout is redirected straight to this file so NDJSON lines land as
        #    they are produced (LIVE, not buffered to the end) — this file is the
        #    artifact to open when a run is stuck: its last lines are the last thing
        #    the CLI did before going silent. Non-stream mode keeps the original
        #    in-process capture_output path.
        stream_path = ""
        if config.stream_json:
            try:
                if config.log_dir:
                    _base = config.log_dir
                else:
                    from core.config import BUILDER_WORKSPACE_ROOT as _BR
                    _base = os.path.join(_BR, "cli_logs")
                _dir = os.path.join(_base, run_id or "norun")
                os.makedirs(_dir, exist_ok=True)
                stream_path = os.path.join(
                    _dir, f"{profile}-{int(time.time())}-{uuid.uuid4().hex[:8]}.ndjson"
                )
                logger.info(
                    "[SDLC-CLI] activity stream (live NDJSON — one JSON object per line)",
                    run_id=run_id, profile=profile, stream_file=stream_path,
                    err_file=stream_path + ".err",
                )
            except Exception as _le:
                logger.warning(
                    f"[SDLC-CLI] could not prepare activity-stream file — falling back "
                    f"to buffered capture: {_le}", run_id=run_id, profile=profile,
                )
                stream_path = ""

        def _read_file(path: str) -> str:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as _fh:
                    return _fh.read()
            except Exception:
                return ""

        start = time.monotonic()
        try:
            if stream_path:
                with open(stream_path, "w", encoding="utf-8") as _out_fh, \
                        open(stream_path + ".err", "w", encoding="utf-8") as _err_fh:
                    proc = spawn(
                        argv, cwd=workspace_root, env=env,
                        stdout=_out_fh, stderr=_err_fh, text=True,
                        timeout=config.timeout_secs,
                    )
                # Prefer the live file (real streaming redirects the child's stdout fd
                # straight into it). Fall back to proc.stdout when the file is empty —
                # e.g. a test double that returns a CompletedProcess with .stdout set
                # rather than writing to the redirected fd. In production proc.stdout is
                # None (no capture_output), so the file is always the source of truth.
                stdout = _read_file(stream_path) or (getattr(proc, "stdout", None) or "")
                stderr = _read_file(stream_path + ".err") or (getattr(proc, "stderr", None) or "")
            else:
                proc = spawn(
                    argv, cwd=workspace_root, env=env,
                    capture_output=True, text=True, timeout=config.timeout_secs,
                )
                stdout = proc.stdout or ""
                stderr = proc.stderr or ""
        except subprocess.TimeoutExpired as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            # Surface cwd + the wall-clock cap in the reason: subprocess.TimeoutExpired
            # only carries argv, so the working directory (and which timeout fired)
            # is otherwise invisible in the raw exception string.
            reason = f"CLI timeout after {config.timeout_secs}s (cwd={workspace_root or '<cwd>'})"
            # Best-effort: recover the session id from the partial output — the LIVE
            # stream file (stream mode) or e.stdout (buffered mode). Many CLIs emit an
            # early init/system line carrying session_id, which _extract_result_envelope
            # picks up line-by-line. A recovered id lets IMPLEMENT resume the SAME session
            # IN PLACE after a timeout — continuing from where it left off — instead of
            # resetting the workspace. Files written so far are preserved regardless (the
            # caller salvages them via git diff); the stream file shows what stalled.
            if stream_path:
                _partial_stdout = _read_file(stream_path)
            else:
                _partial_stdout = (e.stdout if isinstance(e.stdout, str) else "") or ""
            _recovered_sid = ""
            try:
                if _partial_stdout.strip():
                    _recovered_sid = _parse_cli_envelope(_partial_stdout, exit_code=-1).session_id or ""
            except Exception:
                _recovered_sid = ""
            logger.error(
                f"[SDLC-CLI] spawn failed — timeout", run_id=run_id, profile=profile,
                cwd=workspace_root, timeout_secs=config.timeout_secs,
                duration_ms=duration_ms, error=str(e), recovered_session_id=_recovered_sid,
                stream_file=stream_path,
            )
            logger.info(
                "[SDLC-CLI] exit", run_id=run_id, profile=profile, exit_code=-1,
                is_error=True, subtype="timeout", duration_ms=duration_ms,
                usage={}, cost=0.0,
            )
            return CliResult(
                status="suspended", reason=reason, exit_code=-1, is_error=True,
                subtype="timeout", session_id=_recovered_sid,
            )
        except Exception as e:  # OSError (missing binary), permission error, etc.
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.error(f"[SDLC-CLI] spawn failed", run_id=run_id, profile=profile, error=str(e))
            logger.info(
                "[SDLC-CLI] exit", run_id=run_id, profile=profile, exit_code=-1,
                is_error=True, subtype="spawn_error", duration_ms=duration_ms,
                usage={}, cost=0.0,
            )
            return CliResult(status="suspended", reason=f"failed to spawn CLI: {e}", exit_code=-1, is_error=True, subtype="spawn_error")

        duration_ms = int((time.monotonic() - start) * 1000)
        exit_code = proc.returncode if proc.returncode is not None else -1

        # ── Tool-subset signal (retained but neutralized) ───────────────────────
        # Previously we forwarded a legacy `--allowed-tools`/`--tools` list and warned
        # when the CLI's init event registered fewer tools than requested. That path
        # is now gone (see `_build_argv`: the legacy names — Read/Write/Edit/etc. —
        # do not match v3 tool ids and were causing every tool call to be filtered
        # out). With `--always-approve` on and no allow-list forwarded, the CLI uses
        # its full built-in toolset, so this diagnostic no longer applies. Left as a
        # dead-code no-op instead of ripped out, to keep the diff surgical.
        try:
            _registered = _extract_registered_tools(stdout)
            if False and _registered is not None and allowed_tools:
                _requested = {t.split("(", 1)[0].strip() for t in allowed_tools.split(",") if t.strip()}
                _reg_names = {r.split("(", 1)[0].strip() for r in _registered}
                _missing = sorted(_requested - _reg_names)
                if _missing:
                    logger.warning(
                        "[SDLC-CLI] requested tools NOT registered by the CLI — the coder "
                        "cannot use them (edits won't batch → slower IMPLEMENT). Verify the "
                        "deployed binary supports these tools and honors --allowed-tools.",
                        run_id=run_id, profile=profile,
                        requested=sorted(_requested), registered=sorted(_reg_names),
                        missing=_missing, allowed_tools_flag=_ALLOWED_TOOLS_FLAG,
                    )
        except Exception as _te:
            logger.debug(f"[SDLC-CLI] tool-subset check skipped: {_te}", run_id=run_id, profile=profile)

        # Exit 124 = the binary's built-in stall watchdog self-exited (no progress within
        # AINXT_STALL_TIMEOUT_MS). Treat it like a timeout: recover the session id and let
        # the caller SALVAGE on-disk edits + continue instead of discarding the run. The
        # LAST lines of the stream file show which tool/hook/message was in flight.
        if exit_code == 124:
            _recovered_sid = ""
            try:
                _recovered_sid = _parse_cli_envelope(stdout, exit_code=exit_code).session_id or ""
            except Exception:
                _recovered_sid = ""
            reason = _reason_for_exit_code(124)
            logger.error(
                "[SDLC-CLI] stall self-exit (124)", run_id=run_id, profile=profile,
                duration_ms=duration_ms, recovered_session_id=_recovered_sid,
                stream_file=stream_path, stderr=_trunc(stderr, 3000),
            )
            logger.info(
                "[SDLC-CLI] exit", run_id=run_id, profile=profile, exit_code=124,
                is_error=True, subtype="stalled", duration_ms=duration_ms,
                usage={}, cost=0.0, stream_file=stream_path,
            )
            return CliResult(
                status="suspended", reason=reason, exit_code=124, is_error=True,
                subtype="stalled", session_id=_recovered_sid,
            )

        result = _parse_cli_envelope(stdout, exit_code=exit_code)

        logger.info(
            "[SDLC-CLI] exit", run_id=run_id, profile=profile, exit_code=exit_code,
            is_error=result.is_error, subtype=result.subtype, duration_ms=duration_ms,
            usage=result.usage, cost=result.total_cost_usd,
            stdout_len=len(stdout), stdout_head=_trunc(stdout, 2000),
            stream_file=stream_path,
        )

        if exit_code != 0 or result.is_error:
            reason = _reason_for_exit_code(exit_code)
            # Dump the RAW CLI output so a suspend (esp. unparseable_json / a real
            # in-CLI error) can be diagnosed straight from the worker log without
            # re-running on the host.
            logger.warning(
                "[SDLC-CLI] suspend — raw CLI output", run_id=run_id, profile=profile,
                exit_code=exit_code, subtype=result.subtype, reason=reason,
                stdout=_trunc(stdout, 6000), stderr=_trunc(stderr, 3000),
            )
            # Transient upstream failure (proxy/gateway 502/503, api_error, "Upstream LLM
            # error", connection reset) — detected by-exclusion (a deterministic
            # usage/auth/tool/missing-binary failure is never transient).
            _transient = _is_transient_failure(
                exit_code, result.subtype, (stdout or "") + "\n" + (stderr or ""),
                num_turns=result.num_turns, max_turns=max_turns,
            )
            # Read-only callers (profile="plan") opt into a bounded FRESH re-spawn: they
            # wrote nothing so a clean re-run is safe. IMPLEMENT (profile="code") passes
            # transient_retries=0 and NEVER re-spawns here — it reads result.transient at
            # its call site to CONTINUE the same session on the untouched workspace.
            if _transient and transient_retries > 0:
                import random as _random
                _backoff = min(
                    _TRANSIENT_BACKOFF_CAP_SECS,
                    _TRANSIENT_BACKOFF_BASE_SECS * (2 ** _transient_attempt),
                    ) + _random.uniform(0.0, 1.0)
                logger.warning(
                    "[SDLC-CLI] transient upstream failure — bounded fresh retry",
                    run_id=run_id, profile=profile, exit_code=exit_code,
                    subtype=result.subtype, attempt=_transient_attempt + 1,
                    retries_left=transient_retries, backoff_secs=round(_backoff, 2),
                )
                time.sleep(_backoff)
                return run_cli(
                    config=config, workspace_root=workspace_root, prompt=prompt,
                    profile=profile, model=model, output_schema=output_schema,
                    max_turns=max_turns, run_id=run_id,
                    resume_session_id=resume_session_id, plugins=plugins,
                    plugin_marketplace=plugin_marketplace,
                    transient_retries=transient_retries - 1,
                    _transient_attempt=_transient_attempt + 1, spawn=spawn,
                )
            return CliResult(
                status="suspended", reason=reason, result_text=result.result_text,
                structured_output=result.structured_output, is_error=True,
                subtype=result.subtype, exit_code=exit_code, usage=result.usage,
                total_cost_usd=result.total_cost_usd, session_id=result.session_id,
                transient=_transient, num_turns=result.num_turns,
            )

        # ── Eval: CLI answer quality (fire-and-forget) ───────────────────────
        # CLI had zero eval coverage. This adds Check 2 (hallucination) +
        # Check 3 (usefulness) on every completed CLI result — covers all
        # SDLC phases that use run_cli() (classify, plan, implement, govern).
        # Runs in a daemon thread so it never delays the caller.
        if result.result_text:
            try:
                import threading as _cli_eval_thread
                _cli_q   = prompt[:400]
                _cli_ans = result.result_text
                _cli_rid = run_id
                def _run_cli_eval():
                    try:
                        from core.evals import eval_engine as _ee
                        _ee.eval_answer_quality(_cli_q, _cli_ans, [], run_id=_cli_rid)
                    except Exception:
                        pass
                _cli_eval_thread.Thread(
                    target=_run_cli_eval, daemon=True, name="eval-cli-answer"
                ).start()
            except Exception:
                pass

        return CliResult(
            status="completed", reason="", result_text=result.result_text,
            structured_output=result.structured_output, is_error=result.is_error,
            subtype=result.subtype, exit_code=exit_code, usage=result.usage,
            total_cost_usd=result.total_cost_usd, session_id=result.session_id,
        )
    finally:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if schema_path:
            try:
                os.remove(schema_path)
            except OSError:
                pass


def _extract_registered_tools(stdout: str):
    """Best-effort: the set of tool names the CLI actually REGISTERED, pulled from its
    stream-json `system`/`init` event (`{"type":"system","subtype":"init","tools":[...]}`).

    `tools` items may be plain names ("Edit") or objects ({"name":"Edit",...}); both are
    handled. Returns a set of names, or None when no such event is present (single-envelope
    json mode, or an older binary that doesn't emit init) — None means 'unknown', so the
    caller must NOT treat it as 'no tools registered'. Never raises."""
    s = (stdout or "").strip()
    if not s:
        return None
    for line in s.splitlines():
        line = line.strip()
        if '"tools"' not in line or not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if not (isinstance(o, dict) and o.get("type") == "system" and isinstance(o.get("tools"), list)):
            continue
        names = set()
        for t in o["tools"]:
            if isinstance(t, str) and t.strip():
                names.add(t.strip())
            elif isinstance(t, dict):
                _n = t.get("name")
                if isinstance(_n, str) and _n.strip():
                    names.add(_n.strip())
        return names
    return None


def _extract_result_envelope(stdout: str):
    """Return the CLI result envelope dict from stdout, tolerating log lines /
    banners / NDJSON / trailing text around the JSON (real headless CLIs rarely
    emit ONLY the envelope on stdout). Returns None if no JSON object is found.

    Strategy, in order: (1) whole-string parse; (2) per-line parse, so an
    envelope amid log lines or a stream-json feed is recovered; (3) a
    string-aware brace-matched scan for balanced top-level {...} blocks spanning
    multiple lines. In every case, prefer the object with type=="result" (the
    documented envelope), else fall back to the LAST JSON object seen."""
    s = (stdout or "").lstrip("").strip()
    if not s:
        return None
    # (1) fast path — clean single object.
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    candidates: list = []
    # (2) line-by-line (envelope-amid-logs / NDJSON / stream-json).
    for line in s.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            o = json.loads(line)
            if isinstance(o, dict):
                candidates.append(o)
        except Exception:
            continue
    # (3) string-aware brace scan for multi-line objects.
    if not candidates:
        depth, start, in_str, esc = 0, -1, False, False
        for i, ch in enumerate(s):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}" and depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        o = json.loads(s[start:i + 1])
                        if isinstance(o, dict):
                            candidates.append(o)
                    except Exception:
                        pass
                    start = -1
    if not candidates:
        return None
    for o in reversed(candidates):
        if o.get("type") == "result":
            return o
    return candidates[-1]


def _parse_cli_envelope(stdout: str, *, exit_code: int) -> CliResult:
    """Parse the CLI's result envelope, tolerating log/banner noise around the
    JSON (see _extract_result_envelope). Never raises — a missing/unparseable
    envelope is surfaced as a completed CliResult with structured_output=None
    and is_error, so the caller (Step 3) can apply its own thin/truncation logic
    on result_text (which reuses `_looks_truncated_json`)."""
    if not isinstance(stdout, str) or not stdout.strip():
        return CliResult(status="completed", result_text="", structured_output=None,
                         is_error=True, subtype="empty_output", exit_code=exit_code)
    envelope = _extract_result_envelope(stdout)
    if not isinstance(envelope, dict):
        # No JSON object anywhere in stdout — surface raw text for the caller.
        return CliResult(
            status="completed", result_text=stdout, structured_output=None,
            is_error=True, subtype="unparseable_json", exit_code=exit_code,
        )

    # v3 (ainxt 0.2.101+) renamed the envelope fields: result→text,
    # structured_output→structuredOutput, session_id→sessionId, and replaced the
    # is_error/subtype pair with a single `stopReason`. There is no total_cost_usd
    # (only cost_is_partial). Read the v1/v2 keys FIRST (byte-identical behaviour for
    # those flavors) and fall back to the v3 camelCase keys, so one parser serves all.
    result_text = envelope.get("result")
    if result_text is None:
        result_text = envelope.get("text")
    if not isinstance(result_text, str):
        result_text = "" if result_text is None else str(result_text)
    structured = envelope.get("structured_output")
    if not isinstance(structured, dict):
        structured = envelope.get("structuredOutput")
    if not isinstance(structured, dict):
        structured = None
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    try:
        total_cost_usd = float(envelope.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        total_cost_usd = 0.0
    session_id = envelope.get("session_id") or envelope.get("sessionId") or ""
    if not isinstance(session_id, str):
        session_id = str(session_id)
    try:
        num_turns = int(envelope.get("num_turns"))
    except (TypeError, ValueError):
        num_turns = -1
    # Error/subtype: v1/v2 carry is_error + subtype directly. v3 has neither — it
    # reports a `stopReason` we map into the existing subtype vocabulary so downstream
    # logic (IMPLEMENT auto-continue keys on subtype=="error_max_turns") keeps working.
    is_error = bool(envelope.get("is_error"))
    subtype = envelope.get("subtype") or ""
    if not isinstance(subtype, str):
        subtype = str(subtype)
    if not subtype and "is_error" not in envelope:
        stop = str(envelope.get("stopReason") or "").strip()
        _stop_l = stop.lower()
        if _stop_l in ("cancelled", "canceled", "maxturns", "max_turns"):
            # v3 emits stopReason "Cancelled" when it hits --max-turns (exit 0). Map to the
            # subtype the state machine watches so a turn-exhausted IMPLEMENT resumes.
            subtype = "error_max_turns"
        elif _stop_l and _stop_l != "endturn":
            subtype = stop
        # A non-terminal stopReason (anything but EndTurn/max-turns) also flags an error so
        # the caller does not treat a truncated/aborted turn as a clean completion.
        if subtype == "error_max_turns" or (_stop_l and _stop_l != "endturn"):
            is_error = True

    return CliResult(
        status="completed", result_text=result_text, structured_output=structured,
        is_error=is_error, subtype=subtype, exit_code=exit_code, usage=usage,
        total_cost_usd=total_cost_usd, session_id=session_id, num_turns=num_turns,
    )


# ════════════════════════════════════════════════════════════════════════════
# AgentEngine protocol + first implementation
# ════════════════════════════════════════════════════════════════════════════

class AgentEngine(Protocol):
    """The replaceable boundary — swap `AinxtCliEngine` for a different engine
    implementation without touching call sites."""

    def run(
            self,
            *,
            workspace_root: str,
            prompt: str,
            profile: str,
            model: str,
            output_schema: Optional[dict] = None,
            max_turns: Optional[int] = None,
            run_id: str = "",
            plugins: Optional[list] = None,
            plugin_marketplace: str = "",
    ) -> CliResult:
        ...


class AinxtCliEngine:
    """The first `AgentEngine` implementation — wraps `run_cli`. Config is
    resolved fresh (via `CliEngineConfig.from_env()`) on every `run()` call
    unless a config was supplied at construction, so env flips apply without
    a restart."""

    def __init__(self, config: Optional[CliEngineConfig] = None, spawn=subprocess.run):
        self._config = config
        self._spawn = spawn

    def run(
            self,
            *,
            workspace_root: str,
            prompt: str,
            profile: str,
            model: str,
            output_schema: Optional[dict] = None,
            max_turns: Optional[int] = None,
            run_id: str = "",
            resume_session_id: str = "",
            plugins: Optional[list] = None,
            plugin_marketplace: str = "",
    ) -> CliResult:
        config = self._config or CliEngineConfig.from_env()
        return run_cli(
            config=config, workspace_root=workspace_root, prompt=prompt,
            profile=profile, model=model, output_schema=output_schema,
            max_turns=max_turns, run_id=run_id, resume_session_id=resume_session_id,
            plugins=plugins, plugin_marketplace=plugin_marketplace,
            spawn=self._spawn,
        )
