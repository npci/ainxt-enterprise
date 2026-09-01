# SPDX-License-Identifier: Apache-2.0
"""Runtime worker spec — the as-executed sibling of ``WorkerPlan``.

While ``WorkerPlan`` (in ``types.py``) is the verbatim shape returned by
the orchestrator LLM, ``WorkerSpec`` is what the runtime actually executes.
The conversion adds two pieces:

1. ``run_id`` — the per-swarm UUID, used to build the synthetic
   ``swarm::<run_id>::<role_id>`` agent_id that ``AgentRunner._load_agent``
   resolves via the in-memory swarm registry.

2. The mutability boundary. ``WorkerPlan`` is frozen because it represents
   the LLM's declared intent. ``WorkerSpec`` is the runtime view that
   ``AgentRunner`` constructs an agent dict from — kept frozen for safety
   but explicitly mutable through ``WorkerSpec.with_overrides`` if a
   future hot-fix needs to clamp a value at scheduling time.

The runtime never accepts a ``WorkerSpec`` constructed from outside
``from_plan_entry`` — that's the only place ``run_id`` gets attached and
the role_id gets a second-pass validation against ``ALIAS_RE`` (defence
in depth; ``WorkerPlan`` already validated it).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List

from ._shared import ALIAS_RE
from .types import WorkerPlan


@dataclass(frozen=True)
class WorkerSpec:
    """Per-worker runtime config.

    Fields are intentionally a superset of ``WorkerPlan`` so the runtime
    has everything it needs to construct an agent dict for
    ``AgentRunner._load_agent`` without re-reading the plan.
    """
    run_id:            str
    role_id:           str
    role_synth_prompt: str
    task:              str
    tools:             List[str]      = field(default_factory=list)
    skills:            List[str]      = field(default_factory=list)
    knowledge:         Dict[str, Any] = field(default_factory=lambda: {"mode": "none"})
    max_tool_rounds:   int            = 4
    max_tokens:        int            = 2048
    temperature:       float          = 0.2
    timeout_s:         int            = 90
    # Inherited from the parent agent node's modelName when the runtime
    # promotes a WorkerPlan via ``from_plan_entry``. Empty string falls
    # through to FACTORY_MODEL in the AgentRunner (which itself routes
    # via the LLM_PROXY-aware helpers, so SIT remains reachable). Adding
    # this here — instead of inferring at runner load — keeps the per-
    # worker model visible to the SwarmRuntime for structured logging
    # and the per-run JSON dump without re-reading config.
    worker_model:      str            = ""

    @property
    def synthetic_agent_id(self) -> str:
        """The ``agent_id`` shape ``AgentRunner._load_agent`` resolves.

        Format is ``swarm::<run_id>::<role_id>`` — keeps the namespace
        flat so a single ``startswith("swarm::")`` check in the loader
        suffices, mirroring the existing ``subagent::<alias>`` convention
        in ``app.subagents._base.SubAgentSpec.synthetic_id``.
        """
        return f"swarm::{self.run_id}::{self.role_id}"

    @classmethod
    def from_plan_entry(
        cls,
        run_id: str,
        entry: WorkerPlan,
        *,
        worker_model: str = "",
    ) -> "WorkerSpec":
        """Promote a ``WorkerPlan`` into a runtime ``WorkerSpec``.

        Re-validates ``role_id`` against the alias regex (defence in
        depth — ``WorkerPlan.from_dict`` already validated it, but the
        runtime cannot trust callers not to construct one directly).

        ``worker_model`` is forwarded by the SwarmRuntime so every spec
        carries the parent agent's selected model. Empty string is
        valid — ``AgentRunner`` then falls through to ``FACTORY_MODEL``
        which itself routes via the LLM_PROXY helpers, so SIT stays
        reachable. Keeping the field on the spec (vs. resolving lazily)
        lets the runtime emit it in structured logs / JSON dumps.
        """
        if not run_id or "::" in run_id:
            raise ValueError(
                f"run_id must be non-empty and free of '::', got {run_id!r}"
            )
        if not ALIAS_RE.match(entry.role_id):
            raise ValueError(
                f"role_id {entry.role_id!r} must match {ALIAS_RE.pattern}"
            )
        return cls(
            run_id=run_id,
            role_id=entry.role_id,
            role_synth_prompt=entry.role_synth_prompt,
            task=entry.task,
            tools=list(entry.tools),
            skills=list(entry.skills),
            knowledge=dict(entry.knowledge),
            max_tool_rounds=entry.max_tool_rounds,
            max_tokens=entry.max_tokens,
            temperature=entry.temperature,
            timeout_s=entry.timeout_s,
            worker_model=worker_model or "",
        )

    def with_overrides(self, **overrides: Any) -> "WorkerSpec":
        """Return a copy with selected fields replaced.

        Used at scheduling time when the runtime needs to clamp values —
        e.g. force ``max_tool_rounds`` below a deployment-wide ceiling
        without rejecting the whole plan.
        """
        return replace(self, **overrides)


__all__ = ["WorkerSpec"]
