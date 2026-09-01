# SPDX-License-Identifier: Apache-2.0
"""cli_runtime.runner — spawn the headless ``ainxt`` CLI and stream its events.

One public coroutine, ``run_cli_turn``, which is an async generator yielding
``CliEvent`` objects as the CLI produces them and finishing with a terminal
``end`` or ``error`` event. It owns the whole lifecycle: acquire a concurrency
slot, register the MCP session, prepare the workspace, spawn, stream, then
guarantee teardown.

Argv is built against the REAL contract of ``ainxt 0.2.101``
--------------------------------------------------------------
The flags used by ``agents/sdlc_cli_engine.py`` — ``--yes``, ``--no-review``,
``--output-schema``, ``--allowed-tools``, ``--add-dir``, ``--mcp-config`` — do not
exist on this build; each is rejected with ``error: unexpected argument``. That
module targets a different CLI generation, and reusing it (as the previous
attempt did) makes every spawn fail on usage before a single token is produced.
Only flags verified present are emitted here.

Two child-env settings are load-bearing:

``AINXT_FOLDER_TRUST=0``
    Without it the CLI *silently* declines to start our repo-local MCP server and
    the agent runs with no ABStudio tools at all — no error, no warning.
``AINXT_API_KEY``
    How the CLI authenticates to the gateway for LLM traffic. Set explicitly
    rather than relying on ``~/.ainxt/config.json``, whose discovery depends on
    ``HOME`` being right — frequently untrue under systemd and K8s.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from core.logger import logger

from . import workspace as ws
from .config import CliRuntimeConfig, cli_runtime_config
from .process import ProcHandle, spawn
from .session import RunSession, get_registry

# ── CLI event types (from `--output-format streaming-json`) ──────────────────
# Verified on 0.2.101. NOTE the absence of any tool event: tool activity is
# published by our MCP layer, not parsed from stdout. The docs describe the list
# as non-exhaustive, so unknown types are ignored rather than treated as errors.
EV_TEXT = "text"
EV_THOUGHT = "thought"
EV_END = "end"
EV_ERROR = "error"

# Exit codes the CLI uses, mapped to operator-actionable reasons.
_EXIT_REASONS: Dict[int, str] = {
    1: "the CLI exited with a general error",
    2: "the CLI rejected its arguments (flag contract mismatch — check the CLI version)",
    3: "authentication failed — check ABSTUDIO_CLI_API_KEY",
    124: "the CLI stopped making progress and self-terminated",
    130: "the CLI was interrupted",
}


@dataclass
class CliEvent:
    """One normalised event from a CLI run."""

    type: str
    text: str = ""
    session_id: str = ""
    stop_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    num_turns: int = 0
    message: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CliTurnRequest:
    """Everything needed to execute one agent turn through the CLI."""

    prompt: str
    model: str
    run_id: str = ""
    user_id: str = ""
    email: str = ""
    agent_name: str = ""
    node_id: str = ""

    # Scope handed to the MCP tool plane.
    tool_names: List[str] = field(default_factory=list)
    skill_names: List[str] = field(default_factory=list)

    # Read-only agents get ``plan``; anything that may write gets ``acceptEdits``.
    permission_mode: str = "acceptEdits"
    max_turns: int = 0                      # 0 → config default

    # Optional git checkout. When set, the CLI runs inside the clone.
    repo: str = ""
    repo_ref: str = ""

    # Where ``code_executor`` writes artefacts. Defaults to the workspace.
    workflow_artifact_dir: str = ""

    # Per-agent Sample Document (look-and-feel reference). Propagated from
    # ``AgentTurnSpec`` into the ``RunSession`` so the MCP-side dispatcher
    # can expose SAMPLE_DOC_* to the ``code_executor`` sandbox.
    sample_doc_path: str = ""
    sample_doc_kind: str = ""

    # User-uploaded documents (``{file_name, parsed_text, ...}``) to stage into the
    # CLI working directory so the agent can read the files directly.
    documents: List[dict] = field(default_factory=list)

    # Continue a prior CLI session (used by HITL resume).
    resume_session_id: str = ""


# ════════════════════════════════════════════════════════════════════════════
# Concurrency
# ════════════════════════════════════════════════════════════════════════════

_SEMAPHORE: Optional[asyncio.Semaphore] = None
_SEMAPHORE_CAPACITY: int = 0


def get_semaphore() -> asyncio.Semaphore:
    """Process-wide cap on concurrent CLI subprocesses.

    Rebuilt if the configured capacity changes, so an operator can retune the cap
    without a restart. Under N uvicorn workers the host-level cap is N × capacity,
    because this is per-process.
    """
    global _SEMAPHORE, _SEMAPHORE_CAPACITY
    capacity = cli_runtime_config().max_concurrency
    if _SEMAPHORE is None or capacity != _SEMAPHORE_CAPACITY:
        _SEMAPHORE = asyncio.Semaphore(capacity)
        _SEMAPHORE_CAPACITY = capacity
    return _SEMAPHORE


# ════════════════════════════════════════════════════════════════════════════
# Argv / env
# ════════════════════════════════════════════════════════════════════════════

# ---------------------------------------------------------------------------
# Local-model id normalisation for the ainxt CLI
# ---------------------------------------------------------------------------
# The headless ``ainxt`` CLI advertises its local (in-house) models with a
# ``local:`` prefix (see ``ainxt.exe models`` — ``local:glm-5.2-fp8``,
# ``local:gpt-oss-120b``, …). Cloud models are advertised bare
# (``claude-sonnet-4-6``, ``gpt-5.4``, …). When a caller passes ``--model x``
# and ``x`` does not appear in either advertised form, the CLI refuses to
# start the run with:
#
#     Couldn't set model 'x': Invalid params: "unknown model id".
#
# ABStudio persists model ids as the user picked them in the frontend. In
# most rows the id is exactly the CLI form (``claude-sonnet-4-6``,
# ``gpt-5.4``, …). But a chunk of agents were saved with the *bare* local
# name (``glm-5.2-fp8``, ``gpt-oss-120b``, …). The interactive engine path
# (a non-CLI HTTP proxy call) tolerates the bare form because our own local
# gateway also accepts it, so those rows continued to work in "Test run".
# The CLI path — used by scheduled triggers — does not tolerate the bare
# form, so every trigger firing against one of those agents ends with
# ``status='error'`` and the message above in ``trigger_executions.error``.
#
# We fix this at the single point where the CLI is spawned. If a caller
# passes a bare id that the platform's local-model catalogue recognises,
# prepend ``local:`` before it reaches the CLI. Cloud ids and already-
# prefixed ids are left exactly as passed in. Failing the catalogue check
# also passes the id through unchanged so an unknown cloud id keeps its
# original error message.
#
# We import ``_is_local_model`` lazily so a broken governance import can
# never keep the CLI from spawning — the normalise step is best-effort by
# construction. A guarded env kill-switch (``ABSTUDIO_CLI_MODEL_NORMALISE=0``)
# lets ops disable this once every persisted row has been migrated.


# Static list of local-model bare ids we know the ainxt CLI expects with a
# ``local:`` prefix. Sourced from ``ainxt.exe models`` on the local machine and
# from ``core.governance._is_local_model``'s prefix heuristic. Keeping this in
# code (rather than a network probe at dispatch time) means the normalisation
# stays hot-path safe — the CLI spawn must not depend on a fresh HTTP call to
# the local LLM gateway.
# LOCAL_MODEL_IDS extends (or replaces) this list without a code change.
#
# The shipped set was, by its own comment above, "sourced from `ainxt.exe models`
# on the local machine" -- one machine's inventory, frozen into source. Any other
# deployment serves different models, so `_normalize_cli_model` would not add the
# `local:` prefix for them and CLI dispatch would send a bare id the local
# gateway does not recognise. The prefix heuristic below catches the common
# families, but anything outside them needed a source edit.
#
#   LOCAL_MODEL_IDS=my-llm-7b,acme/coder-32b     # added to the shipped set
#   LOCAL_MODEL_IDS_REPLACE=true                 # use ONLY the configured set
# The shipped default and the configured value use the SAME format and the SAME
# parser. It was a frozenset literal beside a comma-separated env var, which meant
# two formats, two code paths, and eight bare model-id literals to keep in step.
_SHIPPED_LOCAL_MODEL_IDS_DEFAULT = os.getenv("LOCAL_MODEL_IDS_DEFAULT", "")  # set to your locally-served model IDs, comma-separated


def _parse_model_ids(raw: str) -> frozenset:
    return frozenset(part.strip().lower() for part in (raw or "").split(",") if part.strip())


_SHIPPED_LOCAL_MODEL_IDS = _parse_model_ids(_SHIPPED_LOCAL_MODEL_IDS_DEFAULT)
_CONFIGURED_LOCAL_MODEL_IDS = _parse_model_ids(os.getenv("LOCAL_MODEL_IDS", ""))

_KNOWN_LOCAL_MODEL_IDS = (
    _CONFIGURED_LOCAL_MODEL_IDS
    if _CONFIGURED_LOCAL_MODEL_IDS
    and os.getenv("LOCAL_MODEL_IDS_REPLACE", "false").strip().lower() in ("true", "1", "yes")
    else _SHIPPED_LOCAL_MODEL_IDS | _CONFIGURED_LOCAL_MODEL_IDS
)

# Bare-id prefixes that always resolve to local families on this platform
# (matches ``core.governance._is_local_model``'s heuristic). Kept in sync
# by construction — the governance function is the source of truth for what
# counts as "local", and this static mirror stays a short lookup table so
# the CLI dispatch never blocks on a gateway probe.
_LOCAL_MODEL_PREFIXES = ("kimi-", "glm-", "qwen", "mistral", "mixtral", "gemma", "deepseek", "gpt-oss", "llama")


def _normalize_cli_model(model: str) -> str:
    """Return ``model`` in the id form the ainxt CLI expects.

    * Empty / falsy → returned unchanged.
    * Already prefixed with ``local:`` → returned unchanged.
    * Contains a namespace ``/`` (e.g. ``openai/gpt-5.4``) → returned unchanged;
      the CLI already knows those forms.
    * Bare id that matches the known local catalogue or a local family prefix
      → prefixed with ``local:`` (e.g. ``glm-5.2-fp8`` → ``local:glm-5.2-fp8``).
    * Anything else → returned unchanged, so an unknown cloud id keeps its
      "unknown model id" error unchanged and we do not accidentally rewrite it.

    Fully synchronous — no network calls. This runs on every CLI dispatch and
    must not add latency or block on external services. Any exception is
    swallowed and the original id is passed through untouched.
    """
    if not model:
        return model
    if os.getenv("ABSTUDIO_CLI_MODEL_NORMALISE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return model
    try:
        stripped = model.strip()
        if not stripped:
            return model
        lower = stripped.lower()
        if lower.startswith("local:"):
            return stripped
        if "/" in stripped:
            return stripped
        # Strip YYYYMMDD date suffix — CLI accepts base form only (e.g. claude-haiku-4-5).
        import re as _re
        normalised = _re.sub(r"-\d{8}$", "", stripped)
        if normalised != stripped:
            logger.info(f"[AGENT] cli_runtime: stripped date suffix {stripped!r} → {normalised!r}")
            stripped = normalised
            lower = stripped.lower()
        # Exact-match against the known local ids first (fast, unambiguous).
        if lower in _KNOWN_LOCAL_MODEL_IDS:
            logger.info(
                f"[AGENT] cli_runtime: normalising local model id {stripped!r} → 'local:{stripped}' "
                "(matched _KNOWN_LOCAL_MODEL_IDS)"
            )
            return f"local:{stripped}"
        # Prefix heuristic — covers hyphenated local ids we haven't
        # explicitly enumerated (kimi-*, qwen-*, glm-*, …).
        for pfx in _LOCAL_MODEL_PREFIXES:
            if lower.startswith(pfx):
                logger.info(
                    f"[AGENT] cli_runtime: normalising local model id {stripped!r} → 'local:{stripped}' "
                    f"(matched prefix {pfx!r})"
                )
                return f"local:{stripped}"
    except Exception as exc:
        logger.warning(
            f"[AGENT] cli_runtime: _normalize_cli_model({model!r}) raised {exc!r}; "
            "leaving model id unchanged"
        )
    return model


def build_argv(
    *,
    config: CliRuntimeConfig,
    request: CliTurnRequest,
    workspace: str,
    prompt_file: str = "",
) -> List[str]:
    """Build the exact argument vector for a headless run.

    Every flag below was confirmed against ``ainxt 0.2.101``:

    ``-p/--single``         single-turn headless prompt
    ``--prompt-file``       same, read from a file (avoids the argv size limit)
    ``--output-format``     ``streaming-json`` for live NDJSON
    ``--model``             model id
    ``--permission-mode``   default|acceptEdits|auto|dontAsk|bypassPermissions|plan
    ``--max-turns``         turn cap
    ``--cwd``               working directory
    ``--allow``             permission rule; MCP rules need ``MCPTool(server__tool)``
                            form — ``mcp__server__tool`` never matches
    ``--verbatim``          send the prompt exactly as given (no CLI rewriting)
    ``--no-plan``           skip plan mode; we drive the workflow ourselves
    ``--resume``            continue a prior session
    """
    binary = config.resolved_binary or config.cli_path
    argv: List[str] = [binary]

    if prompt_file:
        argv += ["--prompt-file", prompt_file]
    else:
        argv += ["--single", request.prompt]

    argv += [
        "--output-format", "streaming-json",
        "--permission-mode", request.permission_mode or "acceptEdits",
        "--max-turns", str(request.max_turns or config.max_turns),
        "--cwd", workspace,
        # Pre-authorise our own MCP server so no interactive approval is needed.
        "--allow", f"MCPTool({config.mcp_server_name}__*)",
        # The prompt is already fully composed by the engine.
        "--verbatim",
        # ABStudio owns orchestration; the CLI should just execute this turn.
        "--no-plan",
        "--no-subagents",
    ]
    if request.model:
        # Normalise local model ids so the CLI accepts them. See the
        # ``_normalize_cli_model`` docstring for the exact rewrite rules.
        argv += ["--model", _normalize_cli_model(request.model)]
    if request.resume_session_id:
        argv += ["--resume", request.resume_session_id]
    return argv


def build_env(*, config: CliRuntimeConfig, workspace: str) -> Dict[str, str]:
    """Child environment for the spawned CLI."""
    env = dict(os.environ)
    env["AINXT_API_KEY"] = config.api_key
    # Mandatory: repo-local MCP servers are silently skipped in an untrusted
    # folder, which would leave the agent with zero ABStudio tools and no error.
    env["AINXT_FOLDER_TRUST"] = "0"
    # Keep ANSI escapes out of the NDJSON stream.
    env["NO_COLOR"] = "1"
    env["FORCE_COLOR"] = "0"
    env["TERM"] = "dumb"
    return env


# ════════════════════════════════════════════════════════════════════════════
# Event parsing
# ════════════════════════════════════════════════════════════════════════════

def parse_event(line: str) -> Optional[CliEvent]:
    """Parse one NDJSON line into a ``CliEvent``.

    Returns ``None`` for blank lines, non-JSON noise (a banner or warning the CLI
    writes to stdout) and event types we do not consume. Being permissive here is
    deliberate: the documented event list is explicitly non-exhaustive, so an
    unrecognised type must never abort a run.
    """
    raw = (line or "").strip()
    if not raw or not raw.startswith("{"):
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    etype = str(obj.get("type") or "")
    if etype == EV_TEXT:
        return CliEvent(type=EV_TEXT, text=str(obj.get("data") or ""), raw=obj)
    if etype == EV_THOUGHT:
        return CliEvent(type=EV_THOUGHT, text=str(obj.get("data") or ""), raw=obj)
    if etype == EV_END:
        return CliEvent(
            type=EV_END,
            session_id=str(obj.get("sessionId") or ""),
            stop_reason=str(obj.get("stopReason") or ""),
            usage=obj.get("usage") if isinstance(obj.get("usage"), dict) else {},
            num_turns=int(obj.get("num_turns") or 0),
            raw=obj,
        )
    if etype == EV_ERROR:
        return CliEvent(type=EV_ERROR, message=str(obj.get("message") or "CLI error"), raw=obj)
    return None


def normalise_usage(usage: Optional[dict]) -> Dict[str, Any]:
    """Map the CLI's usage keys onto ABStudio's.

    The CLI reports Anthropic-style ``input_tokens``/``output_tokens``; ABStudio's
    accounting, Grafana traces and budget ledger all expect
    ``prompt_tokens``/``completion_tokens``/``total_tokens``.
    """
    usage = usage or {}
    prompt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "estimated": False,
    }


# ════════════════════════════════════════════════════════════════════════════
# Process teardown
# ════════════════════════════════════════════════════════════════════════════

async def _terminate(proc: ProcHandle, grace: float, run_id: str) -> None:
    """Stop a child process (and its group) gracefully, then forcibly. Never raises.

    ``ProcHandle`` signals the whole process group on POSIX, so helpers the CLI
    spawned are stopped too rather than orphaned.
    """
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
        return
    except asyncio.TimeoutError:
        pass
    except Exception:
        return

    logger.warning("[CLI-RUN] child ignored terminate — killing", run_id=run_id, pid=proc.pid)
    proc.kill()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════════════════════

async def run_cli_turn(
    request: CliTurnRequest,
    *,
    config: Optional[CliRuntimeConfig] = None,
    on_session: Optional[Callable[[RunSession], None]] = None,
) -> AsyncIterator[CliEvent]:
    """Execute one agent turn in a spawned CLI, yielding events as they arrive.

    Always terminates with exactly one ``end`` or ``error`` event, so a consumer
    can rely on a single terminal frame. Never raises for an expected failure
    (missing binary, timeout, non-zero exit) — those become ``error`` events, so a
    failing tool call cannot take down the request. ``asyncio.CancelledError``
    *does* propagate, after the child has been killed, because a cancelled
    request must not be reported as a completed one.

    The session token is registered before the spawn and revoked in ``finally``,
    so the MCP endpoint is only reachable while this exact process is alive.

    ``on_session`` is invoked with the ``RunSession`` as soon as it is registered
    (before the spawn), so a caller can drain tool events for the whole run
    without having to guess when the session appears.
    """
    cfg = config or cli_runtime_config()
    run_id = request.run_id or f"abs-{uuid.uuid4().hex[:16]}"
    registry = get_registry()

    if not cfg.binary_available:
        yield CliEvent(
            type=EV_ERROR,
            message=(f"ainxt binary not found (ABSTUDIO_CLI_PATH={cfg.cli_path!r}). "
                     f"Set it to the binary's absolute path on this host."),
        )
        return
    if not cfg.api_key:
        yield CliEvent(
            type=EV_ERROR,
            message=("No ABSTUDIO_CLI_API_KEY configured — the CLI has no gateway "
                     "credentials. Set it before enabling ABSTUDIO_CLI_MODE."),
        )
        return

    session: Optional[RunSession] = None
    proc: Optional[ProcHandle] = None
    started = time.monotonic()

    semaphore = get_semaphore()
    waited_since = time.monotonic()
    await semaphore.acquire()
    queue_wait_ms = int((time.monotonic() - waited_since) * 1000)

    try:
        workspace = ws.prepare_workspace(run_id)
        artifact_dir = request.workflow_artifact_dir or workspace

        session = registry.register(
            run_id=run_id,
            user_id=request.user_id,
            email=request.email,
            allowed_tools=request.tool_names,
            allowed_skills=request.skill_names,
            workflow_artifact_dir=artifact_dir,
            sample_doc_path=getattr(request, "sample_doc_path", "") or "",
            sample_doc_kind=getattr(request, "sample_doc_kind", "") or "",
            agent_name=request.agent_name,
            node_id=request.node_id,
            ttl_seconds=cfg.run_timeout_s + 60,
        )
        if on_session is not None:
            # Hand the session over immediately so the caller's event drain
            # covers the entire run, including the first tool call.
            try:
                on_session(session)
            except Exception as exc:
                logger.warning("[CLI-RUN] on_session callback raised", run_id=run_id, error=str(exc))
        ws.write_mcp_config(
            workspace=workspace, config=cfg, run_id=run_id, token=session.token,
        )

        # Optional git checkout — the CLI then runs inside the clone so its
        # file tools see the repo at the root of their sandbox.
        cwd = workspace
        if request.repo:
            clone = ws.ensure_repo(
                workspace=workspace, repo=request.repo, ref=request.repo_ref,
                user_id=request.user_id, email=request.email, run_id=run_id,
            )
            if not clone.ok:
                yield CliEvent(type=EV_ERROR, message=clone.error or "git checkout failed")
                return
            cwd = clone.path
            ws.write_mcp_config(
                workspace=cwd, config=cfg, run_id=run_id, token=session.token,
            )

        # Stage any user-uploaded documents into the CLI's working directory so
        # the agent can open and re-read them directly (every node gets its own
        # copy, regardless of file size). Staged AFTER the optional git clone so
        # they land in the actual cwd the CLI runs in.
        if request.documents:
            ws.stage_documents(cwd, request.documents)

        # Prompts go via a file by default (threshold 0) — see
        # ``config.prompt_file_threshold`` for why inline argv is a trap.
        prompt_file = ""
        if len(request.prompt or "") >= max(cfg.prompt_file_threshold, 1):
            prompt_file = ws.write_prompt_file(cwd, request.prompt)

        argv = build_argv(config=cfg, request=request, workspace=cwd, prompt_file=prompt_file)
        env = build_env(config=cfg, workspace=cwd)

        logger.info(
            "[CLI-RUN] spawning",
            run_id=run_id, agent=request.agent_name, node_id=request.node_id,
            model=request.model, permission_mode=request.permission_mode,
            cwd=cwd, tools=len(request.tool_names), skills=len(request.skill_names),
            prompt_chars=len(request.prompt or ""), prompt_via_file=bool(prompt_file),
            queue_wait_ms=queue_wait_ms, repo=request.repo or "",
            resume=bool(request.resume_session_id),
            # argv without the prompt, which can be huge and may hold user data.
            argv=[a for a in argv if a != request.prompt],
        )

        proc = await spawn(argv, cwd=cwd, env=env)

        deadline = time.monotonic() + cfg.run_timeout_s
        saw_terminal = False

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                line_bytes = await asyncio.wait_for(proc.readline(), timeout=remaining)
                if not line_bytes:
                    break  # EOF
                event = parse_event(line_bytes.decode("utf-8", errors="replace"))
                if event is None:
                    continue
                if event.type in (EV_END, EV_ERROR):
                    saw_terminal = True
                yield event
                if event.type == EV_ERROR:
                    # An error frame is terminal; stop reading.
                    break

            await asyncio.wait_for(proc.wait(), timeout=30)

        except asyncio.TimeoutError:
            await _terminate(proc, cfg.kill_grace_s, run_id)
            logger.error(
                "[CLI-RUN] timeout",
                run_id=run_id, timeout_s=cfg.run_timeout_s,
                elapsed_s=round(time.monotonic() - started, 1),
                tool_calls=session.tool_calls if session else 0,
            )
            yield CliEvent(
                type=EV_ERROR,
                message=f"The agent did not finish within {cfg.run_timeout_s}s and was stopped.",
            )
            return

        exit_code = proc.returncode if proc.returncode is not None else -1
        stderr_text = proc.stderr_tail()

        logger.info(
            "[CLI-RUN] finished",
            run_id=run_id, exit_code=exit_code,
            duration_s=round(time.monotonic() - started, 1),
            tool_calls=session.tool_calls if session else 0,
            tools_listed=session.listed_tool_count if session else -1,
            saw_terminal=saw_terminal,
        )

        # Backstop: register any file the model wrote DIRECTLY into its workspace
        # (bypassing code_executor) so it becomes a real /generated-files/<x>
        # download instead of a bare filename the UI linkifies into a broken
        # portal route. Recorded on the session so it flows through the same
        # generated-files path as code_executor output.
        if session is not None:
            try:
                _rescued = ws.rescue_workspace_files(cwd, run_id, request.user_id)
                if _rescued:
                    session.record_files(_rescued)
            except Exception as _rescue_exc:
                logger.warning(
                    "[CLI-RUN] workspace file rescue failed",
                    run_id=run_id, error=str(_rescue_exc),
                )

        # A tool-less run is almost always a misconfiguration rather than an
        # agent that chose not to act, and it is the exact silent failure that
        # sank the previous attempt — so it is surfaced loudly.
        if session and session.listed_tool_count == 0 and request.tool_names:
            logger.error(
                "[CLI-RUN] the CLI never received any ABStudio tools — check that "
                "AINXT_FOLDER_TRUST=0 reached the child and that the MCP URL is "
                "reachable from it",
                run_id=run_id, requested_tools=request.tool_names,
            )

        if not saw_terminal:
            reason = _EXIT_REASONS.get(exit_code, f"the CLI exited with code {exit_code}")
            detail = f" {stderr_text.strip()}" if stderr_text.strip() else ""
            yield CliEvent(
                type=EV_ERROR,
                message=(f"{reason}." + (f" Details:{detail}" if detail else "")),
            )

    except asyncio.CancelledError:
        # The user pressed Stop, or the HTTP client disconnected. Kill the child
        # before propagating, or the subprocess outlives the request.
        logger.info("[CLI-RUN] cancelled — terminating child", run_id=run_id)
        if proc is not None:
            await _terminate(proc, cfg.kill_grace_s, run_id)
        raise

    except Exception as exc:
        logger.exception("[CLI-RUN] unexpected failure", run_id=run_id, error=str(exc))
        if proc is not None:
            await _terminate(proc, cfg.kill_grace_s, run_id)
        yield CliEvent(type=EV_ERROR, message=f"CLI execution failed: {exc}")

    finally:
        # Order matters. Revoke the token first so the MCP endpoint is closed
        # even if the steps below throw; then make sure no child outlives the
        # request; then always give the concurrency slot back — a leaked slot
        # permanently reduces capacity and eventually deadlocks every run.
        try:
            registry.revoke(run_id)
            if proc is not None and proc.returncode is None:
                await _terminate(proc, cfg.kill_grace_s, run_id)
        finally:
            semaphore.release()


def get_session(run_id: str) -> Optional[RunSession]:
    """Look up a live session (used by the SSE bridge to drain tool events)."""
    return get_registry().get(run_id)


__all__ = [
    "EV_TEXT",
    "EV_THOUGHT",
    "EV_END",
    "EV_ERROR",
    "CliEvent",
    "CliTurnRequest",
    "run_cli_turn",
    "build_argv",
    "build_env",
    "parse_event",
    "normalise_usage",
    "get_semaphore",
    "get_session",
]
