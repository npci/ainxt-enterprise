# SPDX-License-Identifier: MIT
"""Strict-JSON dataclasses for the orchestrator/aggregator contract.

The ``SwarmOrchestrator`` LLM call MUST return JSON matching ``SwarmPlan``.
This module is the single source of truth for that shape. Both the
orchestrator (validation, retry-on-bad-output) and the runtime (scheduling,
worker construction) consume these types — keeping the JSON contract here
prevents drift between the prompt's claimed schema and what the code
actually parses.

Design choices:

* All ``from_dict`` constructors REJECT unknown top-level keys. This is
  the cheapest way to catch an orchestrator that hallucinates a new field
  ("priority", "deadline", etc.) — we want a hard validation error so we
  can retry with corrective feedback, not silent acceptance.

* Default values are minimal. Anything the orchestrator can sensibly omit
  defaults to a safe value (``aggregator.kind="none"`` → return raw
  blackboard digest; ``shared_memory_policy="broadcast"`` → all workers
  see all prior results).

* ``WorkerPlan`` lives here (not in ``worker_spec.py``) because it's the
  AS-DECLARED-BY-THE-LLM shape. ``WorkerSpec`` in ``worker_spec.py`` is
  the AS-EXECUTED runtime object with the ``run_id`` attached and the
  role_id validated against the alias regex. Keeping them separate makes
  the data-flow explicit:
        LLM JSON → WorkerPlan → (validate+attach run_id) → WorkerSpec
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.logger import logger
from ._shared import ALIAS_RE


# Reject role_ids that carry a numeric ``tool<N>`` suffix specifically —
# that's the drift shape (``jira_fetcher_tool14``, ``jira_fetcher_tool21``)
# that produces semantically-identical pills rendered as distinct. We
# deliberately do NOT reject ``_worker<N>`` / ``_agent<N>`` here because
# some planners emit ``role_worker_1`` as a legitimate map-reduce role
# and that rejection was over-triggering plan_validation_failed on
# well-formed plans. If the tool-instance-id drift returns, we can widen
# this pattern back — for now, precision > recall.
_FORBIDDEN_ROLE_ID_SUFFIX_RE = re.compile(r"^.+_tool\d+$")


# Strategies the orchestrator may declare. Anything else is rejected.
_VALID_STRATEGIES = {"sequential", "parallel", "map_reduce"}

# Shared-memory policies. ``broadcast`` = every worker sees the running
# blackboard digest as chat history. ``private_with_summary`` = workers see
# only the orchestrator-prepared summary (smaller context). ``off`` = each
# worker runs with empty history (mirrors today's ``delegate_to_*`` shape).
_VALID_MEMORY_POLICIES = {"broadcast", "private_with_summary", "off"}

# Foreign-vocabulary aliases for shared_memory_policy. The orchestrator
# LLM has been observed to emit values from other multi-agent frameworks
# (OpenAI Swarm / AutoGen / CrewAI) — most commonly ``outputs_only`` and
# ``results_only`` which both semantically map to ``private_with_summary``
# (workers see a curated summary of prior outputs, not the full
# blackboard). We coerce silently because:
#   * the planner's intent is unambiguous in every observed case, and
#   * the alternative is a hard PlanValidationError that propagates all
#     the way back to the parent LLM, which then apologises to the user
#     instead of completing the task.
# Aliases are applied AFTER ``_coerce_enum_field`` (so we always compare
# lowercased strings) and BEFORE the membership check.
_MEMORY_POLICY_ALIASES = {
    "outputs_only":   "private_with_summary",
    "results_only":   "private_with_summary",
    "summary_only":   "private_with_summary",
    "summary":        "private_with_summary",
    "private":        "private_with_summary",
    "none":           "off",
    "disabled":       "off",
    "no_sharing":     "off",
    "shared":         "broadcast",
    "full":           "broadcast",
    "all":            "broadcast",
}

# Aggregator kinds known to v1. ``none`` skips the LLM reduce and returns
# a deterministic blackboard digest envelope — useful when the workers
# already returned the final answer (e.g. a single-worker swarm).
_VALID_AGGREGATOR_KINDS = {"none", "ranker", "merger", "voter", "summariser"}


class SwarmPlanError(ValueError):
    """Raised when an orchestrator plan does not match the declared schema."""


# Keys we accept when the LLM returns an enum field as a nested object
# instead of a plain string (e.g. ``{"type": "broadcast"}``). Order matters:
# we prefer the most policy-specific key first.
_ENUM_OBJECT_KEYS = ("type", "mode", "kind", "name", "value", "policy", "strategy")


def _coerce_enum_field(raw: Any, *, default: str, field_name: str) -> str:
    """Normalise an enum-like plan field into a lowercase string.

    The orchestrator LLM sometimes emits an object (``{"type": "broadcast"}``)
    or an unexpected scalar instead of the documented string. Without this
    helper, ``.strip().lower()`` raises ``AttributeError`` and escapes the
    retry loop, which only catches :class:`SwarmPlanError`. We coerce known
    shapes here and raise :class:`SwarmPlanError` for anything else so the
    retry loop can recover.
    """
    if raw is None or raw == "":
        return default
    if isinstance(raw, str):
        return raw.strip().lower() or default
    if isinstance(raw, dict):
        for key in _ENUM_OBJECT_KEYS:
            v = raw.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip().lower()
        raise SwarmPlanError(
            f"{field_name} must be a string; got object with keys "
            f"{sorted(raw.keys())}"
        )
    raise SwarmPlanError(
        f"{field_name} must be a string; got {type(raw).__name__}"
    )


def _format_unknown_field_hints(
    unknown: set,
    translations: Dict[str, str],
) -> str:
    """Render a ``. Field translation: 'a' → use 'b'; …`` suffix for an
    ``unknown ... field(s): [...]`` SwarmPlanError.

    The pattern (sort, look up, format, join) is shared by the three
    foreign-shape translation sites in this module (plan-level,
    worker-level, aggregator-level). Returns an empty string when none
    of the unknown keys have a known translation, so the caller can
    append it unconditionally.
    """
    hints = [
        f"{k!r} → use {translations[k]!r}"
        for k in sorted(unknown)
        if k in translations
    ]
    return f". Field translation: {'; '.join(hints)}." if hints else ""


@dataclass(frozen=True)
class WorkerPlan:
    """One worker as declared by the orchestrator LLM.

    Mirrors the JSON shape verbatim. Validation of cross-field constraints
    (tools exist in the capability manifest, role_id is unique within the
    plan, etc.) happens in ``SwarmOrchestrator.plan``, not here — this
    type only enforces single-field structural rules.
    """
    role_id:           str
    role_synth_prompt: str
    task:              str
    tools:             List[str]            = field(default_factory=list)
    skills:            List[str]            = field(default_factory=list)
    knowledge:         Dict[str, Any]       = field(default_factory=lambda: {"mode": "none"})
    max_tool_rounds:   int                  = 4
    max_tokens:        int                  = 8192
    temperature:       float                = 0.2
    timeout_s:         int                  = 90

    @classmethod
    def from_dict(cls, data: Any) -> "WorkerPlan":
        if not isinstance(data, dict):
            raise SwarmPlanError(f"worker entry must be a JSON object, got {type(data).__name__}")
        # Strict allowlist — any unknown key is a hard error so a typoed
        # field doesn't silently fall back to its default.
        allowed = {
            "role_id", "role_synth_prompt", "task", "tools", "skills",
            "knowledge", "max_tool_rounds", "max_tokens", "temperature",
            "timeout_s",
        }
        unknown = set(data.keys()) - allowed
        if unknown:
            # Foreign-DAG-shape hint. Mirrors ``SwarmPlan.from_dict``'s
            # top-level translation table (above). Sonnet / Opus
            # routinely emit each worker as an AutoGen / LangGraph DAG
            # node (``{"id", "description", "tool", "params", "depends_on"}``)
            # — the corrective retry only fixes this when the error
            # message TELLS the model what to use instead. Without
            # these hints attempt 2 just repeats the same shape and the
            # plan fails permanently. See dump file
            # ``20260620T070447_..._attempt2.json`` for the regression
            # this guards against.
            _WORKER_FIELD_TRANSLATIONS = {
                "id":           "role_id (must match [a-z][a-z0-9_]{0,39})",
                "name":         "role_id (must match [a-z][a-z0-9_]{0,39})",
                # ``worker_id`` is the most common drift shape we see —
                # planners that copy the OpenAI Swarm / AutoGen DAG-node
                # template default to it. Calling it out explicitly here
                # gives the corrective retry feedback an actionable
                # rewrite instruction.
                "worker_id":    "role_id (must match [a-z][a-z0-9_]{0,39})",
                "description":  "task (self-contained per-worker input)",
                "instructions": "role_synth_prompt (six-block worker contract)",
                "prompt":       "role_synth_prompt (six-block worker contract)",
                "system_prompt": "role_synth_prompt (six-block worker contract)",
                "tool":         "tools (JSON ARRAY of tool names, not a single string)",
                # ``tool_hints`` is the second most common drift key —
                # the planner thinks it's "suggesting" tools rather than
                # binding them. The runtime hydrator only reads
                # ``tools``; ``tool_hints`` arrives at dispatch with
                # ``tools=[]`` and nothing to bind, then fails silently.
                "tool_hints":   "tools (JSON ARRAY of tool names from the manifest)",
                "params":       "(remove — inline parameter values into the worker's 'task' text)",
                "parameters":   "(remove — inline parameter values into the worker's 'task' text)",
                "args":         "(remove — inline argument values into the worker's 'task' text)",
                "input":        "task (self-contained per-worker input)",
                "inputs":       "task (self-contained per-worker input)",
                "output":       "(remove — output shape is described inside role_synth_prompt's [OUTPUT] block)",
                "outputs":      "(remove — output shape is described inside role_synth_prompt's [OUTPUT] block)",
                # ``output_key`` is the third drift key from the same
                # DAG-template lineage — workers don't have named output
                # slots; downstream consumers read the worker's emitted
                # JSON directly off the blackboard.
                "output_key":   "(remove — outputs are described inside role_synth_prompt's [OUTPUT] block; downstream consumers read the JSON the worker emits)",
                "depends_on":   "(remove — model dependencies via plan.strategy='sequential'; downstream worker's 'task' references upstream role_ids)",
                "dependencies": "(remove — model dependencies via plan.strategy='sequential'; downstream worker's 'task' references upstream role_ids)",
                "agent":        "role_id (must match [a-z][a-z0-9_]{0,39})",
                "type":         "(remove — workers do not have a type; use role_id and role_synth_prompt)",
                "kind":         "(remove — workers do not have a kind; use role_id and role_synth_prompt)",
                "model":        "(remove — workers do not pick their model; max_tokens/temperature only)",
                "retries":      "(remove — retries are runtime policy, not plan policy)",
                "timeout":      "timeout_s (integer seconds, 1..600)",
            }
            raise SwarmPlanError(
                f"unknown worker field(s): {sorted(unknown)}. "
                f"Allowed keys are exactly: "
                f"['knowledge', 'max_tokens', 'max_tool_rounds', 'role_id', "
                f"'role_synth_prompt', 'skills', 'task', 'temperature', "
                f"'timeout_s', 'tools']"
                f"{_format_unknown_field_hints(unknown, _WORKER_FIELD_TRANSLATIONS)}"
            )

        role_id = (data.get("role_id") or "").strip().lower()
        if not ALIAS_RE.match(role_id):
            raise SwarmPlanError(
                f"worker.role_id '{role_id}' must match {ALIAS_RE.pattern}"
            )
        # Reject planner LLM giving up on semantic naming — role_ids like
        # ``jira_fetcher_tool14`` produce pills that are semantically
        # identical yet render distinct. See _FORBIDDEN_ROLE_ID_SUFFIX_RE.
        if _FORBIDDEN_ROLE_ID_SUFFIX_RE.match(role_id):
            raise SwarmPlanError(
                f"worker.role_id '{role_id}' uses a forbidden numeric "
                f"tool/worker/agent/task/job suffix. role_id MUST describe "
                f"the worker's PURPOSE, not the tool it uses or an index. "
                f"Use a semantic name like '<domain>_<action>' "
                f"(e.g. 'jira_issue_triager', 'gitlab_commits_fetcher'). "
                f"If two workers do the same job, either collapse them "
                f"into one worker or switch to strategy=\"map_reduce\" "
                f"with a single role_id."
            )
        prompt = data.get("role_synth_prompt") or ""
        if not isinstance(prompt, str) or not prompt.strip():
            raise SwarmPlanError(f"worker '{role_id}'.role_synth_prompt must be a non-empty string")
        task = data.get("task") or ""
        if not isinstance(task, str) or not task.strip():
            raise SwarmPlanError(f"worker '{role_id}'.task must be a non-empty string")
        tools = data.get("tools") or []
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise SwarmPlanError(f"worker '{role_id}'.tools must be List[str]")
        skills = data.get("skills") or []
        if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
            raise SwarmPlanError(f"worker '{role_id}'.skills must be List[str]")
        knowledge = data.get("knowledge") or {"mode": "none"}
        # Sonnet / Opus reliably drift on this field: they treat
        # ``knowledge`` as "subject-matter context" and emit a list of
        # topic strings (``["GitLab REST API", "commit data structures"]``)
        # instead of the required KB-selector object. Reject-and-retry
        # was proven not to fix this in practice — the planner re-emits
        # the same shape. So we COERCE the wrong shape into a safe
        # ``{"mode": "none"}`` and log the drift for observability, then
        # continue. Effect: workers run with no KB (which is what a
        # topic-list would produce anyway once we ignored it) and the
        # plan validates.
        if isinstance(knowledge, list):
            logger.info(f"[AGENT] swarm plan drift: worker '{role_id}'.knowledge was a list {knowledge!r}; coerced to {{'mode': 'none'}} (topic strings belong in task text).")
            knowledge = {"mode": "none"}
        if not isinstance(knowledge, dict):
            raise SwarmPlanError(
                f"worker '{role_id}'.knowledge must be an object "
                f'{{"mode": "none" | "existing_kb", "kb_id"?: "..."}}. '
                f"It is NOT a list of subject-matter areas or topic "
                f"strings — those belong in the worker's 'task' text."
            )
        # numeric bounds — defend against negative rounds / negative tokens
        # which would make the runner explode on the first request.
        max_tool_rounds = int(data.get("max_tool_rounds", 4))
        max_tokens      = int(data.get("max_tokens", 8192))
        timeout_s       = int(data.get("timeout_s", 90))
        temperature     = float(data.get("temperature", 0.2))
        if max_tool_rounds < 0 or max_tool_rounds > 12:
            raise SwarmPlanError(f"worker '{role_id}'.max_tool_rounds must be 0..12")
        if max_tokens < 1 or max_tokens > 16384:
            raise SwarmPlanError(f"worker '{role_id}'.max_tokens must be 1..16384")
        # Enterprise-grade floor: even if the orchestrator picks a tight
        # budget (e.g. 2048) for what it thinks is a "short" task, clamp
        # up to 8192 so worker outputs that include markdown tables /
        # multi-section reports don't get cut off mid-response. See the
        # truncated_payload warning class in SwarmAggregator for the
        # symptom this guards against.
        if max_tokens < 8192:
            max_tokens = 8192
        if timeout_s < 1 or timeout_s > 600:
            raise SwarmPlanError(f"worker '{role_id}'.timeout_s must be 1..600")
        if temperature < 0.0 or temperature > 2.0:
            raise SwarmPlanError(f"worker '{role_id}'.temperature must be 0.0..2.0")

        return cls(
            role_id=role_id,
            role_synth_prompt=prompt,
            task=task,
            tools=list(dict.fromkeys(tools)),     # dedup, preserve order
            skills=list(dict.fromkeys(skills)),
            knowledge=dict(knowledge),
            max_tool_rounds=max_tool_rounds,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_s=timeout_s,
        )


@dataclass(frozen=True)
class SwarmAggregatorSpec:
    """Declarative aggregator config the runtime will execute after workers finish.

    ``kind="none"`` short-circuits the aggregator LLM call and returns a
    deterministic envelope built from the blackboard contents — used when
    a one-worker swarm already returned the final answer.
    """
    kind:   str            = "none"
    prompt: str            = ""

    @classmethod
    def from_dict(cls, data: Any) -> "SwarmAggregatorSpec":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise SwarmPlanError(f"aggregator must be an object, got {type(data).__name__}")
        # Coerce the "aggregator-as-worker" drift shape into our
        # {kind, prompt} contract. Sonnet reliably emits ``{task,
        # role_synth_prompt, max_tokens, temperature, ...}`` here
        # because it copies the worker template. Instead of rejecting
        # and retrying (which the planner keeps failing anyway), map
        # the worker-shape fields onto our fields:
        #   * prompt := prompt | task | description | instructions | role_synth_prompt
        #   * kind   := kind (default merger — the common case for
        #                 multi-worker plans; ``none`` only fits
        #                 single-worker swarms which don't drift here)
        # Then strip the rest so the strict-key check below passes.
        worker_shape_keys = set(data.keys()) - {"kind", "prompt"}
        if worker_shape_keys and not (data.get("kind") and data.get("prompt")):
            logger.info(f'[AGENT] swarm plan drift: aggregator emitted as worker shape (extra keys: {sorted(worker_shape_keys)}); coercing to {{kind, prompt}} contract.')
            coerced_prompt = (
                data.get("prompt")
                or data.get("task")
                or data.get("description")
                or data.get("instructions")
                or data.get("role_synth_prompt")
                or ""
            )
            data = {
                "kind":   data.get("kind") or "merger",
                "prompt": coerced_prompt,
            }
        unknown = set(data.keys()) - {"kind", "prompt"}
        if unknown:
            # Aggregator-shape drift: when the planner copies an
            # OpenAI-Swarm / AutoGen template, it tends to emit the
            # aggregator as YET ANOTHER worker — ``{worker_id, task,
            # depends_on, inputs}`` instead of ``{kind, prompt}``. The
            # error message has to spell out the correct shape or the
            # corrective retry repeats the same mistake. See worker
            # ``_WORKER_FIELD_TRANSLATIONS`` above for the parallel
            # design.
            _AGGREGATOR_FIELD_TRANSLATIONS = {
                "worker_id":    "(remove — aggregator is NOT a worker; use 'kind' + 'prompt' only)",
                "id":           "(remove — aggregator is NOT a worker; use 'kind' + 'prompt' only)",
                "role_id":      "(remove — aggregator is NOT a worker; use 'kind' + 'prompt' only)",
                "name":         "(remove — aggregator is NOT a worker; use 'kind' + 'prompt' only)",
                "task":         "prompt (the reducer instructions, e.g. 'Combine outputs into a single envelope...')",
                "description":  "prompt (the reducer instructions)",
                "instructions": "prompt (the reducer instructions)",
                "depends_on":   "(remove — aggregator implicitly depends on every worker)",
                "dependencies": "(remove — aggregator implicitly depends on every worker)",
                "inputs":       "(remove — aggregator reads the blackboard digest automatically)",
                "input":        "(remove — aggregator reads the blackboard digest automatically)",
                "params":       "(remove — aggregator has no parameters; behaviour is determined by 'kind')",
                "parameters":   "(remove — aggregator has no parameters; behaviour is determined by 'kind')",
                "tools":        "(remove — aggregator has no tools; it only reduces)",
                "skills":       "(remove — aggregator has no skills; it only reduces)",
                "output":       "(remove — output shape is fixed by the AGGREGATOR_SYSTEM_PROMPT contract)",
                "outputs":      "(remove — output shape is fixed by the AGGREGATOR_SYSTEM_PROMPT contract)",
                "output_key":   "(remove — outputs are returned directly to the parent agent)",
                "type":         "kind (one of 'none', 'ranker', 'merger', 'voter', 'summariser')",
                "strategy":     "kind (one of 'none', 'ranker', 'merger', 'voter', 'summariser')",
            }
            raise SwarmPlanError(
                f"unknown aggregator field(s): {sorted(unknown)}. "
                f"Allowed aggregator keys are exactly: ['kind', 'prompt']"
                f"{_format_unknown_field_hints(unknown, _AGGREGATOR_FIELD_TRANSLATIONS)}"
            )
        kind = (data.get("kind") or "none").strip().lower()
        if kind not in _VALID_AGGREGATOR_KINDS:
            raise SwarmPlanError(
                f"aggregator.kind '{kind}' must be one of {sorted(_VALID_AGGREGATOR_KINDS)}"
            )
        prompt = data.get("prompt") or ""
        if not isinstance(prompt, str):
            raise SwarmPlanError("aggregator.prompt must be a string")
        return cls(kind=kind, prompt=prompt)


@dataclass(frozen=True)
class SwarmPlan:
    """Top-level orchestrator output.

    ``workers`` order is preserved for sequential strategies and used as
    SSE-emission order even in parallel strategies so the UI timeline is
    deterministic (mirrors the workflow engine's ``branch_order`` sort at
    native_engine.py:2744).
    """
    strategy:              str
    shared_memory_policy:  str
    workers:               List[WorkerPlan]
    aggregator:            SwarmAggregatorSpec

    @classmethod
    def from_dict(cls, data: Any) -> "SwarmPlan":
        if not isinstance(data, dict):
            raise SwarmPlanError(f"plan must be a JSON object, got {type(data).__name__}")
        # Unwrap a single foreign-key envelope. The planner LLM has been
        # observed wrapping the plan in ``{"swarm_plan": {...}}`` (or the
        # variants below) when it copies the shape of other frameworks
        # /examples. We accept the wrapper silently because the intent is
        # unambiguous — the only key is a synonym for "the plan" — and a
        # hard error here means the user sees a `plan_validation_failed`
        # instead of their answer. We deliberately match a tiny allowlist
        # so we never strip a legitimate wrapper from some future schema.
        if len(data) == 1:
            _only = next(iter(data))
            if _only in {"swarm_plan", "plan", "SwarmPlan", "swarmPlan"} \
                    and isinstance(data[_only], dict):
                data = data[_only]
        unknown = set(data.keys()) - {"strategy", "shared_memory_policy", "workers", "aggregator"}
        if unknown:
            # Wrong-vendor schema hint: the orchestrator LLM is regressing
            # to a different framework's plan shape (AutoGen / CrewAI /
            # Bedrock Swarms etc.). Map the common foreign field names to
            # ours so the retry round gets an actionable fix instruction,
            # not just "unknown field". Without this, the LLM repeats the
            # same mistake on retry because the error doesn't tell it what
            # to use INSTEAD.
            _FIELD_TRANSLATIONS = {
                "agents":            "workers",
                "members":           "workers",
                "tasks":             "workers",
                "execution_mode":    "strategy",
                "mode":              "strategy",
                "swarm_name":        "(remove — name is set by the runtime)",
                "goal":              "(remove — goal is the parent's tool argument, not part of the plan)",
                "output_format":     "(remove — output shape is set by aggregator.kind)",
                "max_retries":       "(remove — retries are runtime policy, not plan policy)",
                "timeout_seconds":   "(remove — set worker.timeout_s per worker instead)",
                "execution_notes":   "(remove — notes are not part of the plan schema)",
                # The wrapper-key variants are also unwrapped silently in
                # the len==1 branch above; this hint exists so a planner
                # that mixes the wrapper with sibling keys still gets an
                # actionable rewrite instruction (instead of just being
                # told "unknown field").
                "swarm_plan":        "(remove wrapper — emit the plan object directly at top level)",
                "plan":              "(remove wrapper — emit the plan object directly at top level)",
                "SwarmPlan":         "(remove wrapper — emit the plan object directly at top level)",
            }
            raise SwarmPlanError(
                f"unknown plan field(s): {sorted(unknown)}. "
                f"Allowed top-level keys are exactly: "
                f"['aggregator', 'shared_memory_policy', 'strategy', 'workers']"
                f"{_format_unknown_field_hints(unknown, _FIELD_TRANSLATIONS)}"
            )

        strategy = _coerce_enum_field(
            data.get("strategy"), default="parallel", field_name="plan.strategy"
        )
        if strategy not in _VALID_STRATEGIES:
            raise SwarmPlanError(
                f"plan.strategy '{strategy}' must be one of {sorted(_VALID_STRATEGIES)}"
            )
        memory_policy = _coerce_enum_field(
            data.get("shared_memory_policy"),
            default="broadcast",
            field_name="plan.shared_memory_policy",
        )
        # Coerce common foreign-framework aliases (outputs_only, none, ...)
        # into our enum vocabulary before the strict membership check. See
        # ``_MEMORY_POLICY_ALIASES`` doc-comment for the rationale.
        memory_policy = _MEMORY_POLICY_ALIASES.get(memory_policy, memory_policy)
        if memory_policy not in _VALID_MEMORY_POLICIES:
            raise SwarmPlanError(
                f"plan.shared_memory_policy '{memory_policy}' must be one of "
                f"{sorted(_VALID_MEMORY_POLICIES)}"
            )

        raw_workers = data.get("workers") or []
        if not isinstance(raw_workers, list):
            raise SwarmPlanError("plan.workers must be a list")
        if not raw_workers:
            raise SwarmPlanError("plan.workers must contain at least one entry")
        workers = [WorkerPlan.from_dict(w) for w in raw_workers]

        # role_id uniqueness — the runtime keys the per-run registry by
        # role_id, so collisions would cause one worker to shadow another.
        role_ids = [w.role_id for w in workers]
        if len(set(role_ids)) != len(role_ids):
            dups = sorted({r for r in role_ids if role_ids.count(r) > 1})
            raise SwarmPlanError(f"duplicate worker.role_id(s): {dups}")

        aggregator = SwarmAggregatorSpec.from_dict(data.get("aggregator"))
        return cls(
            strategy=strategy,
            shared_memory_policy=memory_policy,
            workers=workers,
            aggregator=aggregator,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Re-serialise for logging / replay. Round-trips through ``from_dict``."""
        return {
            "strategy": self.strategy,
            "shared_memory_policy": self.shared_memory_policy,
            "workers": [
                {
                    "role_id": w.role_id,
                    "role_synth_prompt": w.role_synth_prompt,
                    "task": w.task,
                    "tools": list(w.tools),
                    "skills": list(w.skills),
                    "knowledge": dict(w.knowledge),
                    "max_tool_rounds": w.max_tool_rounds,
                    "max_tokens": w.max_tokens,
                    "temperature": w.temperature,
                    "timeout_s": w.timeout_s,
                }
                for w in self.workers
            ],
            "aggregator": {"kind": self.aggregator.kind, "prompt": self.aggregator.prompt},
        }


__all__ = [
    "SwarmPlan",
    "SwarmAggregatorSpec",
    "WorkerPlan",
    "SwarmPlanError",
]
