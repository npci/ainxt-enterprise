# SPDX-License-Identifier: MIT
"""Process-local registry that lets ``AgentRunner._load_agent`` resolve
``swarm::<run_id>::<role_id>`` synthetic ids back to a ``WorkerSpec``.

Why a module-level dict instead of plumbing the spec through ``runner.run``?
Because the runner's ``_load_agent(agent_id)`` interface is the established
contract for "give me the agent dict for this id" and it's called from
multiple sites (chat path, workflow subflow path, sub-agent dispatch). The
swarm only needs to interpose ONE branch in that lookup — registering the
spec under a synthetic id mirrors exactly how ``app.subagents`` handles
its static specs (immutable tuple, ``get_spec(alias)`` lookup).

The dict lives at module scope, scoped to the process. Concurrency:

* ``register`` / ``unregister`` are called once per swarm run, from a
  single coroutine on a single event loop. No external locking is
  required — Python dict mutation is atomic enough for the
  insert/delete pattern we use, and we never iterate-while-mutating.

* ``resolve`` is read-only and may fire from many concurrent workers
  on the same loop. Plain dict reads under the GIL are safe.

Persistence: NONE. A swarm run that crosses a process restart is not
resumable in v1 — the parent's tool call will simply fail on resume.
This matches the v1 scope (no persistent / resumable swarms).
"""
from __future__ import annotations


from typing import Dict, Optional

from .worker_spec import WorkerSpec

from core.logger import logger
# run_id -> role_id -> WorkerSpec. Nested dict so an unregister can drop
# the whole run in one call without iterating.
_RUNS: Dict[str, Dict[str, WorkerSpec]] = {}


def register(run_id: str, specs: Dict[str, WorkerSpec]) -> None:
    """Register every worker for a swarm run under one ``run_id``.

    Called by ``SwarmRuntime`` right after the orchestrator returns a
    valid plan and before the first worker is spawned. Overwriting an
    existing ``run_id`` is treated as a logic error — the runtime mints
    a fresh UUID per run, so a collision means we leaked one.
    """
    if not run_id:
        raise ValueError("swarm registry: run_id must be non-empty")
    if run_id in _RUNS:
        # Defensive: refuse to overwrite. If we ever hit this, something
        # leaked a prior run.
        logger.error(f'[AGENT] swarm registry: refusing to overwrite existing run_id {run_id!r}')
        raise RuntimeError(f"swarm run_id {run_id!r} already registered")
    if not specs:
        raise ValueError("swarm registry: specs map must be non-empty")
    _RUNS[run_id] = dict(specs)


def resolve(run_id: str, role_id: str) -> Optional[WorkerSpec]:
    """Return the worker spec for (run_id, role_id), or None if unknown.

    Called from ``AgentRunner._load_agent`` on the hot path of every
    swarm worker turn. Must be cheap: two dict lookups, no allocations.
    """
    if not run_id or not role_id:
        return None
    bucket = _RUNS.get(run_id)
    if bucket is None:
        return None
    return bucket.get(role_id)


def unregister(run_id: str) -> None:
    """Drop every spec for a finished (or crashed) swarm run.

    Always called from ``SwarmRuntime.execute``'s ``finally`` block so a
    failing aggregator can't leak the run's worker specs into the
    long-running process's memory.
    """
    if not run_id:
        return
    _RUNS.pop(run_id, None)


def active_run_count() -> int:
    """Number of swarm runs whose specs are currently in memory.

    Exported for observability / tests; not part of the hot-path API.
    """
    return len(_RUNS)


def _reset_for_tests() -> None:
    """Clear the registry. Test-only; never call from production code."""
    _RUNS.clear()


__all__ = ["register", "resolve", "unregister", "active_run_count"]
