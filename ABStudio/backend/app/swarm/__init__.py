# SPDX-License-Identifier: Apache-2.0
"""Adaptive sub-agent swarm.

A central LLM orchestrator dynamically synthesises N short-lived universal
sub-agents per request, runs them under bounded parallelism over a shared
blackboard, and reduces their outputs through an aggregator LLM — all behind
a single ``spawn_swarm`` synthetic tool exposed to the parent.

Unlike the static specs in ``app.subagents`` (5 frozen domain specialists,
each a separate file), swarm workers are **born for one run and discarded**.
The orchestrator picks tools/skills/KBs only from the live capability
manifest the deployment exposes, so a new worker capability is added by
adding a tool / skill — never by editing this package.

Public surface:
  * ``SwarmRuntime``           — conductor: plan → workers → aggregate
  * ``SwarmOrchestrator``      — one LLM call → strict-JSON ``SwarmPlan``
  * ``SwarmAggregator``        — one LLM call → parent-facing envelope
  * ``SharedBlackboard``       — asyncio-safe per-run shared workspace
  * ``CapabilityManifest``     — grounded catalog (workflow_repo, 60s TTL)
  * ``WorkerSpec``             — runtime-built sibling of ``SubAgentSpec``
  * ``SwarmPlan``              — dataclass envelope of the orchestrator's JSON

Feature flag ``SWARM_MODE`` (env, default ``legacy``):
  * ``legacy``  — module is dormant; ``spawn_swarm`` not surfaced
  * ``hybrid``  — ``spawn_swarm`` PLUS existing ``delegate_to_*`` tools
  * ``adaptive``— ``spawn_swarm`` only (legacy delegate tools suppressed
                  by callers that honour the flag)

Importers should rely on this module's lazy-import shape — sub-modules read
``app.core.workflow_repo`` / ``app.core.kb_retriever`` only on the hot path
so the import cost stays bounded for processes that never spawn a swarm.
"""
from __future__ import annotations

from .types import SwarmPlan, SwarmAggregatorSpec
from .worker_spec import WorkerSpec
from .blackboard import SharedBlackboard
from .capability_manifest import CapabilityManifest
from .orchestrator import SwarmOrchestrator, PlanValidationError
from .aggregator import SwarmAggregator
from .runtime import SwarmRuntime

__all__ = [
    "SwarmPlan",
    "SwarmAggregatorSpec",
    "WorkerSpec",
    "SharedBlackboard",
    "CapabilityManifest",
    "SwarmOrchestrator",
    "PlanValidationError",
    "SwarmAggregator",
    "SwarmRuntime",
]
