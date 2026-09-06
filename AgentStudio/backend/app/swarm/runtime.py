# SPDX-License-Identifier: MIT
"""SwarmRuntime — the conductor.

Wraps the three-phase swarm into one async method:

    1. Plan   (SwarmOrchestrator.plan)        — 1 LLM call → SwarmPlan
    2. Workers (bounded parallel runner.run)  — N calls to AgentRunner.run
    3. Reduce  (SwarmAggregator.reduce)       — 1 LLM call → envelope

Bounded parallelism is provided by ``asyncio.Semaphore(SWARM_MAX_PARALLEL)``.
The semaphore is around each worker's ``runner.run`` call, NOT around the
LLM HTTP layer — ``httpx`` already caps TCP concurrency at 400/200 via
``LLM_HTTP_MAX_CONNECTIONS`` / ``LLM_HTTP_MAX_KEEPALIVE``. Adding a second
LLM-layer semaphore would be redundant and harder to reason about.

Per-worker failure policy is **isolated continuation**: a worker that
raises (or times out) writes a structured ``{"error": "worker_failure",
...}`` entry to the blackboard and the swarm continues. The aggregator
decides whether the salvageable subset is enough. This mirrors the
existing per-branch isolation in ``native_engine._run_parallel_branches``
at lines 2710-2722.

SSE event surface (frontend gracefully ignores unknown events — verified
at ChatPanel.jsx:2401-2635 in the exploration phase):

    swarm_plan              {run_id, node_id, strategy, worker_count, role_ids[]}
    swarm_worker_start      {run_id, node_id, role_id, task_preview}
    swarm_worker_complete   {run_id, node_id, role_id, ok, preview?, error?, duration_s}
    subagent_start          {call_id, node_id, alias, agent_id, parent_agent_id,
                             task_preview, tools[], skills[]}
    subagent_complete       {call_id, node_id, alias, agent_id, parent_agent_id,
                             duration_s, ok, error?, preview?}
    swarm_aggregate_start   {run_id, node_id, kind}
    swarm_aggregate_complete{run_id, node_id, ok, error?, duration_s}
    swarm_complete          {run_id, node_id, ok, error?}
    swarm_error             {run_id, node_id, code, detail}

    ``node_id`` is the workflow-graph agent node that owns this swarm run
    (empty string in the chat path). The frontend groups subagent pills
    under the parent node using this field; frames without it fall back
    to attaching by ``parent_agent_id``.
"""
from __future__ import annotations

import asyncio
import json

import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .aggregator import SwarmAggregator
from .blackboard import SharedBlackboard
from .capability_manifest import CapabilityManifest
from .orchestrator import GatewayBlockedError, PlanValidationError, SwarmOrchestrator
from .registry import register as _swarm_register, unregister as _swarm_unregister
from .types import SwarmPlan
from .worker_spec import WorkerSpec

from core.logger import logger
# ---------------------------------------------------------------------------
# Config knobs (read once at import time, like LLM_HTTP_MAX_CONNECTIONS)
# ---------------------------------------------------------------------------

SWARM_MAX_PARALLEL    = int(os.getenv("SWARM_MAX_PARALLEL", "8"))
SWARM_WORKER_TIMEOUT_S = int(os.getenv("SWARM_WORKER_TIMEOUT_S", "90"))


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

# The runtime emits events through a caller-provided sink so it works in
# both the chat path (where the AgentRunner converts them into the chat
# SSE stream) and the workflow path (where NativeEngine forwards them
# onto its own stream). A sync sink is sufficient — events are tiny and
# the writer is non-blocking.
SseSink = Callable[[str], None]

# Reuse the engine's SSE wire-format helper so the format stays in lock-
# step across all event sources. ``engine/interface.py`` only imports
# stdlib, so this is cheap to pull in.
from app.engine.interface import make_sse as _make_sse


# ---------------------------------------------------------------------------
# Structured-log prefix + per-run JSON dump
# ---------------------------------------------------------------------------
#
# Prefix every swarm log line with a fixed token so operators can grep one
# run's lifecycle out of a mixed-process terminal. The token is short and
# upper-case for visual contrast in the default ``logging`` format.
_SWARM_LOG_PREFIX = "[SWARM]"

# Where per-run JSON dumps land. Defaults to ``logs/swarm/`` under the
# backend cwd; override with ``SWARM_DUMP_DIR``. Set ``SWARM_DUMP=0`` to
# disable dumps entirely (CI, ephemeral containers, …). The directory is
# created lazily on the first write so we don't pay the syscall at import.
_SWARM_DUMP_ENABLED = os.getenv("SWARM_DUMP", "1").strip().lower() not in (
    "0", "false", "no", "off", "",
)
_SWARM_DUMP_DIR = Path(os.getenv("SWARM_DUMP_DIR", "logs/swarm")).resolve()


def _utc_now_iso() -> str:
    """ISO-8601 UTC stamp with explicit 'Z' suffix — stable for log greps."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# Process-wide map of run_id → live dump dict + dump file path. Used
# by nested swarms to append a back-link to their parent's dump so the
# operator can follow "parent run X spawned nested run Y" without
# grepping multiple files. The entry is removed when the run completes.
_LIVE_DUMPS: Dict[str, Dict[str, Any]] = {}


def _register_live_dump(run_id: str, dump: Dict[str, Any]) -> None:
    """Mark a run's dump as in-flight so children can attach back-links."""
    _LIVE_DUMPS[run_id] = dump


def _unregister_live_dump(run_id: str) -> None:
    """Forget the run's dump (called at run_complete)."""
    _LIVE_DUMPS.pop(run_id, None)


def _attach_nested_run(
    parent_run_id: str,
    parent_role_id: Optional[str],
    nested_run_id: str,
    nested_dump_path: Optional[Path],
) -> None:
    """Append a ``{role_id, run_id, dump_path}`` entry to the parent
    dump's ``nested_runs`` list and re-flush to disk.

    Best-effort — if the parent is no longer live (already completed
    before the child started, which is impossible in normal flow but
    possible under cancellation), the back-link is silently skipped.
    The child's own ``parent_run_id`` field still records the linkage
    so the tree is browsable from the child side.
    """
    parent = _LIVE_DUMPS.get(parent_run_id)
    if parent is None:
        return
    try:
        parent.setdefault("nested_runs", []).append({
            "role_id":   parent_role_id,
            "run_id":    nested_run_id,
            "dump_path": str(nested_dump_path) if nested_dump_path else None,
            "linked_at": _utc_now_iso(),
        })
        _write_dump(parent_run_id, parent)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f'[AGENT] {_SWARM_LOG_PREFIX} nested back-link failed (parent={parent_run_id} child={nested_run_id}): {exc}')


def _write_dump(run_id: str, payload: Dict[str, Any]) -> Optional[Path]:
    """Persist ``payload`` as a pretty-printed JSON dump.

    Never raises — disk failures degrade silently to a warning log so a
    full filesystem can't take down the swarm. The returned path (or
    ``None`` on failure / when disabled) is logged so operators know
    where to look.

    The dump is a single file per run id so iterative writes overwrite
    rather than append — the structure carries the full picture each
    time, making the file safe to tail mid-run and complete post-run.
    """
    if not _SWARM_DUMP_ENABLED:
        return None
    try:
        _SWARM_DUMP_DIR.mkdir(parents=True, exist_ok=True)
        path = _SWARM_DUMP_DIR / f"run_{run_id}.json"
        # ``default=str`` lets us drop dataclasses / Path / datetime in
        # without writing a custom encoder. ``ensure_ascii=False`` keeps
        # non-ASCII task previews readable in the file.
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
        return path
    except Exception as exc:  # noqa: BLE001
        logger.warning(f'[AGENT] {_SWARM_LOG_PREFIX} dump write failed for run {run_id}: {exc}')
        return None


# ---------------------------------------------------------------------------
# Runner protocol (avoids a hard import on agent_factory at module load)
# ---------------------------------------------------------------------------

# A runner_factory is any zero-arg callable returning an object with an
# async ``run(agent_id, user_message, history=..., user_id=..., email=...,
# *, department=..., is_admin=...) -> dict`` method. The chat path passes
# a closure that reuses ``self``'s registry/monitor; the workflow path
# passes a closure that constructs a fresh AgentRunner.
RunnerFactory = Callable[[], Any]


# ---------------------------------------------------------------------------
# Execution context for the swarm
# ---------------------------------------------------------------------------

class SwarmContext:
    """Per-run identity + SSE sink bundle.

    Lightweight on purpose — it's recreated on every ``execute``. Anything
    you'd want to share across runs lives at the ``SwarmRuntime`` level
    (orchestrator / aggregator instances).

    ``parent_attached_tools`` is the parent agent's purpose-built tool
    names (platform utilities like ``code_executor`` / ``spawn_swarm``
    excluded). The orchestrator uses this for two things:

    * **Ranker family expansion** — when the goal's text alone doesn't
      name a service (e.g. "track weekly worklog entries"), the parent's
      tool prefixes tell the ranker which service family to include in
      the scoped manifest. Without this, domain-only goals against
      service-specific catalogs (jira_*, gitlab_*) drop required tools.
    * **Plan-time skip rule** — the orchestrator avoids spawning a
      redundant worker for a sub-task whose tool the parent will call
      directly via its own tool loop.
    """
    __slots__ = ("user_id", "email", "department", "is_admin",
                 "parent_agent_id", "thread_id", "sse_sink",
                 "parent_attached_tools", "node_id", "strict_scope",
                 "allowed_extra_domains")

    def __init__(self, *, user_id: str = "", email: str = "",
                 department: str = "", is_admin: bool = False,
                 parent_agent_id: str = "", thread_id: str = "",
                 sse_sink: Optional[SseSink] = None,
                 parent_attached_tools: tuple = (),
                 node_id: str = "",
                 strict_scope: bool = False,
                 allowed_extra_domains: tuple = ()):
        self.user_id = user_id
        self.email = email
        self.department = department
        self.is_admin = is_admin
        self.parent_agent_id = parent_agent_id
        self.thread_id = thread_id
        self.sse_sink = sse_sink or (lambda _ev: None)
        # Normalise to a tuple so callers can't mutate it across runs
        # and the ranker's set-membership tests stay cheap.
        self.parent_attached_tools = tuple(parent_attached_tools or ())
        # Workflow-graph node that owns this swarm run. Every SSE frame
        # this runtime emits echoes ``node_id`` so the frontend timeline
        # can group subagent pills UNDER the correct agent node instead
        # of pushing them into the flat step list (which caused the
        # "orphaned jira_fetcher below Title" symptom). Empty string in
        # the chat path (no graph node); harmless — the ChatPanel keys
        # off ``node_id`` only when non-empty.
        self.node_id = node_id or ""
        # Strict per-node scope. When True, the orchestrator's manifest
        # contains ONLY ``parent_attached_tools`` — no other domain tools
        # from the catalog. Set by the workflow engine when the operator
        # has attached ≥1 tool to the node (an explicit "delegate ACROSS
        # THESE tools" contract). Empty ``parent_attached_tools`` collapses
        # back to unscoped behaviour even when this flag is True — cannot
        # scope to nothing.
        self.strict_scope = bool(strict_scope)
        # Additional domain prefixes (``jira``, ``gitlab``, …) that the
        # scoped manifest may include even when NO parent-attached tool
        # covers them. Derived by the workflow engine from the node's
        # instructions text — e.g. "Perform Jira operations" adds
        # ``jira`` here even if no jira tool is attached. Lets the
        # planner cover the un-tooled half of a mixed-domain task
        # without giving up strict-scope's anti-drift guarantees for
        # domains the operator did NOT mention.
        self.allowed_extra_domains = tuple(allowed_extra_domains or ())


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class SwarmRuntime:
    """Conductor: plan → workers → aggregate.

    One instance can serve many concurrent ``execute`` calls; each call
    mints a fresh ``run_id`` and its own blackboard, so cross-run state
    is impossible.
    """

    def __init__(
        self,
        runner_factory: RunnerFactory,
        *,
        orchestrator: Optional[SwarmOrchestrator] = None,
        aggregator:   Optional[SwarmAggregator]   = None,
        max_parallel: int = SWARM_MAX_PARALLEL,
        max_workers:  Optional[int] = None,
        orchestrator_model: Optional[str] = None,
        aggregator_model:   Optional[str] = None,
    ):
        if max_parallel < 1:
            raise ValueError("SWARM_MAX_PARALLEL must be >= 1")
        if orchestrator is None:
            # ``orchestrator_model`` is the per-node modelName the user
            # picked in Agent Configuration (forwarded by the workflow
            # engine — see ``native_engine.py`` swarm runtime factory).
            # When supplied it overrides ``SWARM_ORCHESTRATOR_MODEL`` /
            # ``FACTORY_MODEL`` so the same model that runs the parent
            # agent also drives swarm planning. This is the single
            # source-of-truth fix for the SIT divergence between the
            # UI model dropdown (sourced from llm_proxy /v1/models) and
            # the orchestrator's hardcoded env-driven default.
            kwargs: Dict[str, Any] = {}
            if max_workers is not None:
                kwargs["max_workers"] = max_workers
            if orchestrator_model:
                kwargs["model"] = orchestrator_model
            orchestrator = SwarmOrchestrator(**kwargs)
        self._runner_factory = runner_factory
        self._orchestrator   = orchestrator
        # Aggregator inherits the parent-agent model for the same reason
        # the orchestrator does — keeps planner + reducer on the user-
        # picked model end-to-end. Explicit kwarg wins, then the runtime-
        # supplied ``aggregator_model``, then SWARM_AGGREGATOR_MODEL env,
        # then factory default. Falls through harmlessly when the caller
        # constructed an aggregator explicitly.
        if aggregator is None:
            self._aggregator = SwarmAggregator(model=aggregator_model)
        else:
            self._aggregator = aggregator
        # Persist the parent-agent model so ``_gather_workers`` can
        # stamp it on every WorkerSpec — keeping planner + reducer +
        # workers on the same model unless the operator explicitly
        # carved out per-tier env overrides.
        self._worker_model = orchestrator_model or ""
        self._max_parallel   = max_parallel
        # The orchestrator owns the cap; the runtime uses it as a
        # belt-and-braces guard inside ``execute`` (a misconfigured
        # injected orchestrator cannot DoS the runtime).
        self._max_workers    = orchestrator.max_workers

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def execute(
        self,
        *,
        goal: str,
        hints: Optional[Dict[str, Any]] = None,
        ctx: SwarmContext,
    ) -> Dict[str, Any]:
        """Run one swarm end-to-end and return the parent envelope.

        Never raises — every failure mode (plan validation, manifest
        load, all workers timing out, aggregator crash) lands as a
        structured ``{error, detail}`` envelope so the parent's tool
        loop reacts deterministically.
        """
        run_id = uuid.uuid4().hex[:12]
        sse = ctx.sse_sink
        wall_start = time.monotonic()

        # ──────────────────────────────────────────────────────────────
        # Structured run dump — accumulates state and is re-flushed to
        # disk at every milestone (setup → plan → workers → aggregate →
        # complete). Keeping it as one file per run id makes the JSON
        # safe to tail mid-run; a crash leaves the latest snapshot on
        # disk. Schema (top-level keys):
        #   schema_version, run_id, started_at, completed_at,
        #   setup{}, plan{}, workers[], aggregate{}, outcome{}, errors[]
        # The ``setup`` block is the SIT debug surface — it lists every
        # model the swarm will use (orchestrator / aggregator / workers)
        # plus the LLM_PROXY-aware base url, so an operator can verify
        # the run hits the right gateway without grepping env vars.
        # ──────────────────────────────────────────────────────────────
        # Detect nested-swarm context. A worker that spawned THIS run
        # signs the context with parent_agent_id="swarm::<run>::<role>".
        # Recording the parent linkage on both sides (child says "I am
        # nested under X", parent appends to nested_runs[]) lets an
        # operator follow the full delegation tree across multiple
        # dump files when diagnosing "where did model Y come from?".
        _parent_run_id: Optional[str] = None
        _parent_role_id: Optional[str] = None
        if isinstance(ctx.parent_agent_id, str) and ctx.parent_agent_id.startswith("swarm::"):
            try:
                _, _parent_run_id, _parent_role_id = ctx.parent_agent_id.split("::", 2)
            except ValueError:
                _parent_run_id = None
                _parent_role_id = None

        dump: Dict[str, Any] = {
            "schema_version": 1,
            "run_id":       run_id,
            "started_at":   _utc_now_iso(),
            "completed_at": None,
            "goal":         goal,
            "hints":        hints or {},
            # When non-null, this run was spawned by another swarm's
            # worker — useful for chasing the "wrong model" trail.
            "parent_run_id":  _parent_run_id,
            "parent_role_id": _parent_role_id,
            "setup":        {},
            "plan":         {},
            "workers":      [],
            # Populated by ``_record_worker_nested_run`` whenever a
            # subagent of THIS run spawns its own swarm. Each entry
            # is ``{role_id, run_id, dump_path}`` so the JSON tree
            # is browsable in one direction (parent → child).
            "nested_runs":  [],
            "aggregate":    {},
            "outcome":      {},
            "errors":       [],
        }

        # ── 0. Pre-flight setup snapshot ──────────────────────────────
        # Probe the LLM_PROXY-aware helpers once so the dump (and the
        # startup log line) reflect EXACTLY what the workers/orchestrator
        # will resolve to at call time. This is the same chain a single
        # non-swarm agent uses — confirms model-routing parity end-to-end.
        try:
            from app.core.config import (
                openai_compatible_base_url as _cfg_base_url,
                factory_model as _cfg_factory_model,
            )
            _resolved_base_url = _cfg_base_url()
            _resolved_factory_model = _cfg_factory_model()
        except Exception:  # noqa: BLE001 — never block a run on diagnostics
            _resolved_base_url = ""
            _resolved_factory_model = ""

        # The worker tier's "effective" model is what AgentRunner will
        # actually use: the parent-agent model when set, else FACTORY_MODEL.
        _effective_worker_model = self._worker_model or _resolved_factory_model

        dump["setup"] = {
            "parent_agent_id":   ctx.parent_agent_id,
            "node_id":           ctx.node_id,
            "thread_id":         ctx.thread_id,
            "user_id":           ctx.user_id,
            "department":        ctx.department,
            "is_admin":          ctx.is_admin,
            "parent_attached_tools": list(ctx.parent_attached_tools),
            "strict_scope":      bool(getattr(ctx, "strict_scope", False)),
            "max_workers":       self._max_workers,
            "max_parallel":      self._max_parallel,
            "models": {
                "orchestrator":  self._orchestrator.model,
                "aggregator":    self._aggregator.model or _resolved_factory_model,
                "workers":       _effective_worker_model,
                "workers_source": (
                    "parent_agent_modelName"
                    if self._worker_model else "factory_default"
                ),
                # Resolution-source breakdown so an operator can answer
                # "why is the orchestrator running on X?" without re-
                # running. ``parent_agent_modelName`` = the value the
                # user picked in Agent Configuration was used; any other
                # value means an env override or factory default took
                # over (typically because the workflow JSON had a blank
                # modelName when run-stream snapshotted it).
                "orchestrator_source": (
                    "parent_agent_modelName" if self._worker_model
                    else ("env:SWARM_ORCHESTRATOR_MODEL"
                          if os.getenv("SWARM_ORCHESTRATOR_MODEL")
                          else "env:FACTORY_MODEL_or_default")
                ),
                "aggregator_source": (
                    "parent_agent_modelName" if self._worker_model
                    else ("env:SWARM_AGGREGATOR_MODEL"
                          if os.getenv("SWARM_AGGREGATOR_MODEL")
                          else "env:FACTORY_MODEL_or_default")
                ),
                # Echo the env overrides currently in effect so the dump
                # is the single source of truth for a misrouted run.
                "env_overrides": {
                    "SWARM_ORCHESTRATOR_MODEL": os.getenv("SWARM_ORCHESTRATOR_MODEL") or None,
                    "SWARM_AGGREGATOR_MODEL":   os.getenv("SWARM_AGGREGATOR_MODEL") or None,
                    "FACTORY_MODEL":            os.getenv("FACTORY_MODEL") or None,
                    "LOCAL_LLM_MODEL":          os.getenv("LOCAL_LLM_MODEL") or None,
                },
            },
            "llm_routing": {
                # Same helper chain non-swarm agents use — confirms
                # parity with the single-agent run path.
                "openai_compatible_base_url": _resolved_base_url,
                "factory_model_default":      _resolved_factory_model,
                "llm_proxy_url_set":          bool(os.getenv("LLM_PROXY_URL")),
                "x_internal_token_will_be_sent":
                    bool(os.getenv("LLM_PROXY_TOKEN")),
            },
        }

        # One bold line at run start — copy/paste-friendly for tickets.
        logger.info(f"[AGENT] {_SWARM_LOG_PREFIX} run_start run_id={run_id} parent_agent={ctx.parent_agent_id or '<chat>'} orchestrator_model={dump['setup']['models']['orchestrator']} aggregator_model={dump['setup']['models']['aggregator']} worker_model={_effective_worker_model or '<factory_default>'} base_url={_resolved_base_url or '<unset>'} max_workers={self._max_workers} max_parallel={self._max_parallel} parent_run={_parent_run_id or '<top-level>'}")
        _start_dump_path = _write_dump(run_id, dump)
        # Make this run's dump discoverable by any child swarms a worker
        # of THIS run spawns. The registry entry is removed at the very
        # end of ``execute`` (finally clause below).
        _register_live_dump(run_id, dump)
        # If we are ourselves a nested run, attach the back-link onto
        # the parent's dump RIGHT NOW so the operator sees the tree
        # the moment workers start (rather than only on completion).
        if _parent_run_id:
            _attach_nested_run(_parent_run_id, _parent_role_id,
                               run_id, _start_dump_path)

        # ── 1. Build manifest ─────────────────────────────────────────
        try:
            manifest = await CapabilityManifest.build(ctx.user_id, ctx.email)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f'[AGENT] {_SWARM_LOG_PREFIX} manifest_failure run_id={run_id}')
            dump["errors"].append({
                "at": _utc_now_iso(), "stage": "manifest",
                "code": "manifest_failure", "detail": str(exc)[:300],
            })
            dump["outcome"] = {"ok": False, "error": "manifest_failure"}
            dump["completed_at"] = _utc_now_iso()
            _write_dump(run_id, dump)
            _unregister_live_dump(run_id)
            sse(_make_sse("swarm_error", {"run_id": run_id,
                                          "node_id": ctx.node_id,
                                          "code": "manifest_failure",
                                          "detail": str(exc)[:240]}))
            return {"error": "manifest_failure", "detail": str(exc)[:300]}

        # ── 2. Plan ───────────────────────────────────────────────────
        logger.info(f"[AGENT] {_SWARM_LOG_PREFIX} plan_start run_id={run_id} orchestrator_model={self._orchestrator.model} manifest_tools={len(getattr(manifest, 'tools', []) or [])}")
        try:
            plan = await self._orchestrator.plan(
                goal, hints, manifest,
                parent_attached_tools=ctx.parent_attached_tools,
                strict_scope=bool(getattr(ctx, "strict_scope", False)),
                allowed_extra_domains=getattr(ctx, "allowed_extra_domains", ()),
            )
        except GatewayBlockedError as exc:
            # B1 — content-filter rejection. Distinguish from generic
            # plan-validation failures so the parent agent can take a
            # different action (rephrase the goal, fall back, surface
            # a clear message) instead of treating it as a retryable
            # planning bug.
            logger.warning(f'[AGENT] {_SWARM_LOG_PREFIX} gateway_blocked run_id={run_id} detail={exc.detail[:240]}')
            dump["errors"].append({
                "at": _utc_now_iso(), "stage": "plan",
                "code": "gateway_blocked", "detail": exc.detail[:300],
            })
            dump["outcome"] = {"ok": False, "error": "gateway_blocked"}
            dump["completed_at"] = _utc_now_iso()
            _write_dump(run_id, dump)
            _unregister_live_dump(run_id)
            sse(_make_sse("swarm_error", {"run_id": run_id,
                                          "node_id": ctx.node_id,
                                          "code": "gateway_blocked",
                                          "detail": exc.detail[:240]}))
            return {
                "error":  "gateway_blocked",
                "detail": exc.detail,
            }
        except PlanValidationError as exc:
            logger.warning(f"[AGENT] {_SWARM_LOG_PREFIX} plan_validation_failed run_id={run_id} errors={'; '.join(exc.errors)[:240]}")
            dump["errors"].append({
                "at": _utc_now_iso(), "stage": "plan",
                "code": "plan_validation_failed",
                "detail": str(exc)[:300], "errors": list(exc.errors),
            })
            dump["outcome"] = {"ok": False, "error": "plan_validation_failed"}
            dump["completed_at"] = _utc_now_iso()
            _write_dump(run_id, dump)
            _unregister_live_dump(run_id)
            # Emit the first ~800 chars of the joined validator errors so
            # the ChatPanel pill actually shows WHICH schema rule failed
            # (previously truncated at 240, cutting off actionable
            # detail). 800 keeps the SSE frame under a few KB.
            sse(_make_sse("swarm_error", {"run_id": run_id,
                                          "node_id": ctx.node_id,
                                          "code": "plan_validation_failed",
                                          "detail": "; ".join(exc.errors)[:800]}))
            return {
                "error":  "plan_validation_failed",
                "detail": str(exc),
                "errors": exc.errors,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception(f'[AGENT] {_SWARM_LOG_PREFIX} orchestrator_failure run_id={run_id}')
            dump["errors"].append({
                "at": _utc_now_iso(), "stage": "plan",
                "code": "orchestrator_failure", "detail": str(exc)[:300],
            })
            dump["outcome"] = {"ok": False, "error": "orchestrator_failure"}
            dump["completed_at"] = _utc_now_iso()
            _write_dump(run_id, dump)
            _unregister_live_dump(run_id)
            sse(_make_sse("swarm_error", {"run_id": run_id,
                                          "node_id": ctx.node_id,
                                          "code": "orchestrator_failure",
                                          "detail": str(exc)[:800]}))
            return {"error": "orchestrator_failure", "detail": str(exc)[:300]}

        # Plan accepted — record it for the dump and log a single human-
        # readable summary so operators can see WHAT the planner picked.
        # The plan block holds the *as-planned* view (verbatim from the
        # orchestrator LLM). The matching *as-executed* worker outcomes
        # land under ``workers`` later, keyed by the same ``role_id``.
        dump["plan"] = {
            "strategy":             plan.strategy,
            "shared_memory_policy": plan.shared_memory_policy,
            "worker_count":         len(plan.workers),
            "aggregator": {
                "kind":   plan.aggregator.kind,
                # Prompt the aggregator LLM will reduce with — useful
                # for debugging "aggregator picked the wrong fields"
                # without re-running. Truncated to keep dump bounded.
                "prompt": (plan.aggregator.prompt or "")[:400],
            },
            "workers": [
                {
                    "role_id":           w.role_id,
                    # Full system prompt the synthetic worker runs with
                    # (wrapped in WORKER_SKELETON_PROMPT before dispatch).
                    # Truncated — full version is reconstructable from
                    # the orchestrator output if needed.
                    "role_synth_prompt": (w.role_synth_prompt or "")[:400],
                    "task":              w.task[:400],
                    "tools":             list(w.tools),
                    "skills":            list(w.skills),
                    "knowledge":         dict(w.knowledge or {}),
                    "max_tool_rounds":   w.max_tool_rounds,
                    "max_tokens":        w.max_tokens,
                    "temperature":       w.temperature,
                    "timeout_s":         w.timeout_s,
                }
                for w in plan.workers
            ],
        }
        logger.info(f'[AGENT] {_SWARM_LOG_PREFIX} plan_ready run_id={run_id} strategy={plan.strategy} workers={len(plan.workers)} roles={[w.role_id for w in plan.workers]} aggregator={plan.aggregator.kind}')
        _write_dump(run_id, dump)

        # Runtime-side worker cap (belt-and-braces; orchestrator's own
        # cap should have caught this, but defending here means a
        # misconfigured orchestrator instance can never DoS the runtime).
        if len(plan.workers) > self._max_workers:
            dump["errors"].append({
                "at": _utc_now_iso(), "stage": "plan_too_large",
                "code": "plan_too_large",
                "detail": f"{len(plan.workers)} workers > cap {self._max_workers}",
            })
            dump["outcome"] = {"ok": False, "error": "plan_too_large"}
            dump["completed_at"] = _utc_now_iso()
            _write_dump(run_id, dump)
            _unregister_live_dump(run_id)
            sse(_make_sse("swarm_error", {
                "run_id": run_id, "node_id": ctx.node_id,
                "code": "plan_too_large",
                "detail": f"{len(plan.workers)} workers > cap {self._max_workers}",
            }))
            return {
                "error":  "plan_too_large",
                "detail": (f"plan has {len(plan.workers)} workers; "
                           f"runtime cap is {self._max_workers}"),
            }

        sse(_make_sse("swarm_plan", {
            "run_id":       run_id,
            "node_id":      ctx.node_id,
            "strategy":     plan.strategy,
            "shared_memory_policy": plan.shared_memory_policy,
            "worker_count": len(plan.workers),
            "role_ids":     [w.role_id for w in plan.workers],
            "aggregator":   plan.aggregator.kind,
        }))

        # ── 3. Promote WorkerPlan → WorkerSpec, register, fan out ─────
        specs = {w.role_id: WorkerSpec.from_plan_entry(
                    run_id, w, worker_model=self._worker_model,
                 )
                 for w in plan.workers}
        _swarm_register(run_id, specs)

        bb = SharedBlackboard(run_id)
        try:
            await self._gather_workers(plan, specs, bb, ctx, run_id, dump=dump)
            _write_dump(run_id, dump)

            # ── 4. Aggregate ──────────────────────────────────────────
            agg_model = self._aggregator.model or _resolved_factory_model
            logger.info(f'[AGENT] {_SWARM_LOG_PREFIX} aggregate_start run_id={run_id} aggregator_model={agg_model} kind={plan.aggregator.kind}')
            agg_start = time.monotonic()
            sse(_make_sse("swarm_aggregate_start", {
                "run_id": run_id, "node_id": ctx.node_id,
                "kind": plan.aggregator.kind,
            }))
            envelope = await self._aggregator.reduce(plan.aggregator, bb)
            agg_duration = round(time.monotonic() - agg_start, 3)
            sse(_make_sse("swarm_aggregate_complete", {
                "run_id":     run_id,
                "node_id":    ctx.node_id,
                "ok":         "error" not in envelope,
                "error":      envelope.get("error"),
                "duration_s": agg_duration,
            }))
            dump["aggregate"] = {
                "model":     agg_model,
                "kind":      plan.aggregator.kind,
                "ok":        "error" not in envelope,
                "error":     envelope.get("error"),
                "duration_s": agg_duration,
            }
            logger.info(f"[AGENT] {_SWARM_LOG_PREFIX} aggregate_complete run_id={run_id} ok={'error' not in envelope} error={envelope.get('error')} duration_s={agg_duration}")
        finally:
            _swarm_unregister(run_id)

        total_duration = round(time.monotonic() - wall_start, 3)
        envelope.setdefault("run_id", run_id)
        envelope.setdefault("duration_s", total_duration)

        sse(_make_sse("swarm_complete", {
            "run_id":     run_id,
            "node_id":    ctx.node_id,
            "ok":         "error" not in envelope,
            "error":      envelope.get("error"),
            "duration_s": total_duration,
            "worker_count": len(plan.workers),
        }))

        # Final dump flush — include the envelope so the file holds the
        # complete story (setup → plan → workers → aggregate → outcome).
        dump["outcome"] = {
            "ok":           "error" not in envelope,
            "error":        envelope.get("error"),
            "duration_s":   total_duration,
            "worker_count": len(plan.workers),
            "envelope":     envelope,
        }
        dump["completed_at"] = _utc_now_iso()
        dump_path = _write_dump(run_id, dump)
        # Drop the live-dump registry entry now that the run is complete.
        # Any further child swarm spawned on/after this point would not
        # find us in the registry and so would skip the back-link write —
        # but by then the dump is on disk anyway, so the tree stays
        # browsable via the child's ``parent_run_id`` field.
        _unregister_live_dump(run_id)
        logger.info(f"[AGENT] {_SWARM_LOG_PREFIX} run_complete run_id={run_id} ok={'error' not in envelope} duration_s={total_duration} workers={len(plan.workers)} dump={(str(dump_path) if dump_path else '<disabled>')} nested_runs={len(dump.get('nested_runs') or [])}")
        return envelope

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _gather_workers(
        self,
        plan: SwarmPlan,
        specs: Dict[str, WorkerSpec],
        bb: SharedBlackboard,
        ctx: SwarmContext,
        run_id: str,
        *,
        dump: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Schedule every worker according to ``plan.strategy``.

        Sequential strategies run with concurrency=1 even though the
        semaphore would allow more — sequential explicitly means
        "previous worker's output is in the digest before next worker
        starts", which is only true if we await each one.

        Parallel + map_reduce both use the semaphore.
        """
        sem = asyncio.Semaphore(self._max_parallel)
        sse = ctx.sse_sink
        broadcast_history = plan.shared_memory_policy != "off"

        # Resolve the effective worker model once — used in logs and
        # in every dump["workers"] record so a tail of the JSON file
        # shows exactly which model each subagent ran on.
        _effective_worker_model_log = (
            self._worker_model or "<factory_default>"
        )

        def _record_worker(
            spec: WorkerSpec,
            *,
            ok: bool,
            error: Optional[str],
            duration_s: float,
            preview: str = "",
        ) -> None:
            """Append one worker outcome to the per-run JSON dump.

            Safe to call from any of the three terminal branches
            (success, timeout, exception). The record holds the model
            the worker actually used so an SIT operator can correlate
            a failed subagent to a specific gateway without re-running.
            """
            if dump is None:
                return
            dump["workers"].append({
                "role_id":     spec.role_id,
                "agent_id":    spec.synthetic_agent_id,
                "model":       spec.worker_model or _effective_worker_model_log,
                "tools":       list(spec.tools),
                "skills":      list(spec.skills),
                # Echo the per-worker tunables here so the outcome row
                # is self-contained (operator doesn't have to cross-ref
                # ``plan.workers`` to see what limits each subagent ran
                # under — useful when diagnosing "why did this worker
                # time out" or "why was the output truncated").
                "max_tool_rounds": spec.max_tool_rounds,
                "max_tokens":      spec.max_tokens,
                "temperature":     spec.temperature,
                "timeout_s":       spec.timeout_s,
                "knowledge":       dict(spec.knowledge or {}),
                "task_preview": spec.task[:400],
                "ok":          ok,
                "error":       error,
                "duration_s":  duration_s,
                "preview":     preview[:400],
                "completed_at": _utc_now_iso(),
            })

        async def _one(spec: WorkerSpec) -> None:
            async with sem:
                w_start = time.monotonic()
                # Per-spawn correlation id so the frontend can pair
                # subagent_start ↔ subagent_complete events without
                # relying on role_id (which can repeat across runs).
                call_id  = uuid.uuid4().hex
                agent_id = spec.synthetic_agent_id  # "swarm::<run_id>::<role_id>"

                logger.info(f'[AGENT] {_SWARM_LOG_PREFIX} worker_start run_id={run_id} role_id={spec.role_id} model={spec.worker_model or _effective_worker_model_log} tools={list(spec.tools)} skills={list(spec.skills)}')

                sse(_make_sse("swarm_worker_start", {
                    "run_id":       run_id,
                    "node_id":      ctx.node_id,
                    "role_id":      spec.role_id,
                    "task_preview": spec.task[:160],
                    "tools":        list(spec.tools),
                    "skills":       list(spec.skills),
                }))
                # New (frontend-facing) event vocabulary. Emitted in
                # parallel with the legacy event above — consumers that
                # listen for `subagent_*` (ChatPanel.jsx) light up live;
                # consumers that listen for `swarm_worker_*` keep working
                # until a follow-up PR removes the legacy emission.
                sse(_make_sse("subagent_start", {
                    "call_id":         call_id,
                    "node_id":         ctx.node_id,
                    "alias":           spec.role_id,
                    "agent_id":        agent_id,
                    "parent_agent_id": ctx.parent_agent_id,
                    "task_preview":    spec.task[:160],
                    # Full, untruncated task so the Debug Log shows the
                    # ENTIRE input handed to this subagent (task_preview is
                    # retained for the compact chat pill).
                    "task":            spec.task,
                    # Render the worker's scoped capabilities in the
                    # chat panel so the user sees WHAT each subagent is
                    # using to do its job. Mirrors the role/task/tools
                    # contract the build studio surfaces in the UI.
                    "tools":           list(spec.tools),
                    "skills":          list(spec.skills),
                }))
                try:
                    result = await asyncio.wait_for(
                        self._run_one_worker(spec, bb, ctx, broadcast_history),
                        timeout=spec.timeout_s,
                    )
                except asyncio.TimeoutError:
                    err = {"error": "worker_timeout",
                           "detail": f"exceeded timeout_s={spec.timeout_s}"}
                    await bb.append(spec.role_id, "results", err)
                    dt = round(time.monotonic() - w_start, 3)
                    logger.warning(f'[AGENT] {_SWARM_LOG_PREFIX} worker_timeout run_id={run_id} role_id={spec.role_id} model={spec.worker_model or _effective_worker_model_log} duration_s={dt}')
                    _record_worker(spec, ok=False, error="worker_timeout", duration_s=dt)
                    sse(_make_sse("swarm_worker_complete", {
                        "run_id":     run_id, "node_id": ctx.node_id,
                        "role_id":    spec.role_id,
                        "ok":         False, "error": "worker_timeout",
                        "duration_s": dt,
                    }))
                    sse(_make_sse("subagent_complete", {
                        "call_id":         call_id,
                        "node_id":         ctx.node_id,
                        "alias":           spec.role_id,
                        "agent_id":        agent_id,
                        "parent_agent_id": ctx.parent_agent_id,
                        "ok":              False,
                        "error":           "worker_timeout",
                        "duration_s":      dt,
                    }))
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception(f'[AGENT] {_SWARM_LOG_PREFIX} worker_failure run_id={run_id} role_id={spec.role_id} model={spec.worker_model or _effective_worker_model_log}')
                    err = {"error": "worker_failure", "detail": str(exc)[:300]}
                    await bb.append(spec.role_id, "results", err)
                    dt = round(time.monotonic() - w_start, 3)
                    _record_worker(spec, ok=False, error="worker_failure", duration_s=dt)
                    sse(_make_sse("swarm_worker_complete", {
                        "run_id":     run_id, "node_id": ctx.node_id,
                        "role_id":    spec.role_id,
                        "ok":         False, "error": "worker_failure",
                        "duration_s": dt,
                    }))
                    sse(_make_sse("subagent_complete", {
                        "call_id":         call_id,
                        "node_id":         ctx.node_id,
                        "alias":           spec.role_id,
                        "agent_id":        agent_id,
                        "parent_agent_id": ctx.parent_agent_id,
                        "ok":              False,
                        "error":           "worker_failure",
                        "duration_s":      dt,
                    }))
                    return

                # Success — write the worker's structured output to the
                # ``results`` channel and any generated files to the
                # artifact bag.
                response_text = result.get("response") if isinstance(result, dict) else str(result)
                files = result.get("generated_files") if isinstance(result, dict) else None
                payload = _coerce_worker_payload(response_text)
                await bb.append(spec.role_id, "results", payload)
                for f in (files or []):
                    await bb.put_artifact(spec.role_id, f)

                dt   = round(time.monotonic() - w_start, 3)
                ok   = ("error" not in payload) if isinstance(payload, dict) else True
                err  = payload.get("error") if isinstance(payload, dict) else None
                prev = (response_text or "")[:240]

                logger.info(f'[AGENT] {_SWARM_LOG_PREFIX} worker_complete run_id={run_id} role_id={spec.role_id} model={spec.worker_model or _effective_worker_model_log} ok={ok} error={err} duration_s={dt}')
                _record_worker(spec, ok=ok, error=err, duration_s=dt, preview=prev)

                sse(_make_sse("swarm_worker_complete", {
                    "run_id":     run_id,
                    "node_id":    ctx.node_id,
                    "role_id":    spec.role_id,
                    "ok":         ok,
                    "error":      err,
                    "preview":    prev,
                    "duration_s": dt,
                }))
                sse(_make_sse("subagent_complete", {
                    "call_id":         call_id,
                    "node_id":         ctx.node_id,
                    "alias":           spec.role_id,
                    "agent_id":        agent_id,
                    "parent_agent_id": ctx.parent_agent_id,
                    "ok":              ok,
                    "error":           err,
                    "preview":         prev,
                    # Full, untruncated subagent output so the Debug Log can
                    # show the ENTIRE result. `output` is the raw response
                    # text; `output_payload` is the JSON-parsed structured
                    # form when the worker returned valid JSON. `preview`
                    # (240 chars) is retained for the compact chat pill.
                    "output":          response_text,
                    "output_payload":  payload,
                    "generated_files": files or [],
                    "duration_s":      dt,
                }))

        if plan.strategy == "sequential":
            for spec in (specs[w.role_id] for w in plan.workers):
                await _one(spec)
        else:
            await asyncio.gather(
                *(_one(specs[w.role_id]) for w in plan.workers),
                return_exceptions=False,
            )

    async def _run_one_worker(
        self,
        spec: WorkerSpec,
        bb: SharedBlackboard,
        ctx: SwarmContext,
        broadcast_history: bool,
    ) -> Dict[str, Any]:
        """Drive a single worker through ``AgentRunner.run``.

        Worker isolation is provided by the synthetic id
        ``swarm::<run_id>::<role_id>`` — ``AgentRunner._load_agent``
        resolves it via ``app.swarm.registry`` (see the
        ``_load_agent`` patch in agent_factory/pipeline.py) and the
        runner builds an agent dict from the ``WorkerSpec``.
        """
        history = bb.snapshot() if broadcast_history else []
        runner = self._runner_factory()
        result = await runner.run(
            spec.synthetic_agent_id,
            spec.task,
            history=history,
            user_id=ctx.user_id, email=ctx.email,
            department=ctx.department, is_admin=ctx.is_admin,
        )
        if not isinstance(result, dict):
            # Older callers returned a bare string; normalise.
            return {"response": str(result), "generated_files": []}
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_worker_payload(response_text: Any) -> Any:
    """Try to JSON-decode the worker's reply; fall back to raw text.

    The worker scaffold prompt instructs every worker to return ONE
    JSON object. Most will; some won't, and that's fine — the aggregator
    handles raw strings too.
    """
    if not isinstance(response_text, str):
        return response_text
    from ._shared import try_parse_json_object
    parsed = try_parse_json_object(response_text)
    if isinstance(parsed, (dict, list)):
        return parsed
    return response_text


__all__ = [
    "SwarmRuntime",
    "SwarmContext",
    "SWARM_MAX_PARALLEL",
    "SWARM_WORKER_TIMEOUT_S",
]
