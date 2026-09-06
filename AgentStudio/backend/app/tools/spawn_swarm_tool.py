# SPDX-License-Identifier: MIT
"""``spawn_swarm`` — the single synthetic tool that surfaces the swarm runtime.

Two adapters live in this module:

* :class:`SpawnSwarmTool` — for the chat path
  (``agent_factory.pipeline.AgentRunner``). Same shape the runner's
  internal tool list uses: ``name``, ``description``, ``call(args)``,
  ``to_function_spec()``. The runner intercepts ``tc_name == "spawn_swarm"``
  before its catalog dispatcher, identically to how
  ``_is_subagent_tool`` is intercepted today.

* :class:`WorkflowSwarmTool` — for the workflow engine path
  (``app.engine.native_engine.NativeEngine._run_agent``). Same shape the
  engine's existing tool-dispatch loop expects (``name``,
  ``description``, async ``call``, ``to_function_spec``).

Both adapters emit SSE events through a caller-provided sink so the
``swarm_*`` events flow onto whichever stream the caller owns. The
caller is responsible for forwarding those events out to the client
(chat: through the chat's SSE stream; workflow: through NativeEngine's
event yield loop).

Feature-flag gating (``SWARM_MODE``) lives in the CALLERS — this module
is unconditionally importable. Callers check the env and only construct
the tool when ``SWARM_MODE in ("adaptive","hybrid")``.
"""
from __future__ import annotations

import json

from typing import Any, Callable, Optional

from app.swarm.runtime import SwarmContext, SwarmRuntime

from core.logger import logger
# Stable tool name. Used everywhere so a name change here automatically
# updates the runner's intercept check (which imports this constant).
SPAWN_SWARM_TOOL_NAME = "spawn_swarm"


# The description shown to the parent LLM in its function-calling
# manifest. Kept short on purpose — the smart-delegation addendum (see
# ``app.swarm.prompts.SWARM_POLICY_ADDENDUM``) carries the longer "when
# to use this" policy. The manifest description is the routing signal.
_SPAWN_SWARM_DESCRIPTION = (
    "Decompose a multi-step or fan-out task into a planned swarm of "
    "short-lived specialist workers. Use when the task benefits from "
    "parallel work, structured aggregation, or worker-level isolation. "
    "Pass a fully self-contained 'goal' — the orchestrator cannot see "
    "this conversation. "
    # Anti-role-drift guardrail. The orchestrator LLM has been observed
    # treating worker-persona goals ('You are a GitLab agent...') as
    # instructions to roleplay, emitting markdown reports with
    # fabricated tool calls instead of a JSON plan. Phrase the goal as
    # the RESULT you want, not the role you want assumed.
    "Phrase 'goal' as a SPECIFICATION of the desired result, not a "
    "DIRECTIVE addressed to a worker. Do NOT begin with 'You are…' "
    "or 'Perform the following…'. Describe what data / output is "
    "wanted as nouns and outcomes (e.g. 'Commits, MR !3 details, "
    "and file tree for GitLab project ea/mcp_codebase'). The "
    "orchestrator decides who does what."
)


def _parameters_schema() -> dict:
    """JSON-Schema for the function-calling spec.

    Two parameters: ``goal`` (required) and ``hints`` (optional, free-form).
    We intentionally do NOT type-constrain ``hints`` — parents may pass
    arbitrary structured payloads (CSV blobs, JSON arrays, etc.).
    """
    return {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "Self-contained description of the work. Include every "
                    "fact, identifier, dataset reference, and constraint — "
                    "the orchestrator cannot see the parent conversation. "
                    # Provider tool-calling stacks (OpenAI, Anthropic) tend
                    # to weight the parameter description more heavily than
                    # the function description when generating the actual
                    # argument value, so the anti-drift guidance is
                    # restated here verbatim.
                    "Phrase as a SPECIFICATION of the desired result, NOT "
                    "as a directive to a worker. Do NOT begin with "
                    "'You are…' or 'Perform the following…'. Describe what "
                    "is wanted as nouns and outcomes; the orchestrator "
                    "will choose the workers."
                ),
            },
            "hints": {
                "type": "object",
                "description": (
                    "Optional structured inputs the workers may consume "
                    "(e.g. {\"data\": <csv>, \"jd\": <text>})."
                ),
                "additionalProperties": True,
            },
        },
        "required": ["goal"],
    }


def function_spec() -> dict:
    """Function-calling spec shared by both adapters.

    Exported so callers (and tests) can verify the shape without
    instantiating a runtime.
    """
    return {
        "name":        SPAWN_SWARM_TOOL_NAME,
        "description": _SPAWN_SWARM_DESCRIPTION,
        "parameters":  _parameters_schema(),
    }


# ---------------------------------------------------------------------------
# Internal-failure envelope translation (Fix 3)
# ---------------------------------------------------------------------------
# When the swarm's planning or runtime path fails internally (validation
# drift, gateway block, capability lookup failure) the parent agent's
# LLM sees the raw error envelope as ``role: tool`` content. Without
# guidance, it confabulates a user-facing story to defend whatever
# string it just read — a recent dump showed the parent inventing
# "the required tools (get_jira_issue and get_gitlab_merge_request)
# are not currently available in the connected tools catalog" because
# it had just seen ``"errors": ["worker 'jira_worker'.knowledge must
# be an object"]`` — pure hallucination, the tools were perfectly
# present.
#
# The translation strips the raw validator strings, replaces them with
# a prescriptive directive ("don't apologise, don't blame tools,
# complete the request using your own tools"), and tags the envelope
# so the engine can still emit a clean SSE event for the UI/debug
# telemetry. The directive is what the parent LLM sees as the tool's
# content; the original envelope is preserved for downstream SSE
# emitters (engine, SSE bus) via the ``_swarm_error`` field.

_SWARM_FAILURE_DIRECTIVE = (
    "The swarm planner could not produce a valid plan for this goal. "
    "Do NOT tell the user any tools are missing, unavailable, or that "
    "integrations need configuration, and do NOT name specific tools. "
    "Complete the user's request directly using your own attached "
    "tools. If you genuinely cannot, say exactly: 'I hit an internal "
    "planning error — please rephrase your request.' Never invent a "
    "tool-availability explanation."
)

# Internal failure codes that should be translated. Successful results
# (and ``bad_input`` / ``envelope_serialization_failed`` — those are
# the caller's fault, not the swarm's) pass through unchanged so the
# parent can give the user actionable feedback ("the goal was empty").
_INTERNAL_SWARM_FAILURES = frozenset({
    "plan_validation_failed",
    "swarm_runtime_failure",
    "gateway_blocked",
    "manifest_failure",
})


def _translate_internal_failure_envelope(
    envelope: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Replace the raw validator-string envelope with a prescriptive
    directive so the parent LLM cannot confabulate a "tool not found"
    explanation. Preserves the original envelope under ``_swarm_error``
    for the engine's SSE emitter and debug logs.

    Pass-through for envelopes whose ``error`` is not an internal
    failure code, and for successful (non-``error``) results.
    """
    if not isinstance(envelope, dict):
        return envelope
    err = envelope.get("error")
    if err not in _INTERNAL_SWARM_FAILURES:
        return envelope
    return {
        "status":       "planner_failed",
        "failure_code": err,
        "instruction":  _SWARM_FAILURE_DIRECTIVE,
        # Preserve the original envelope verbatim so the engine /
        # native_engine fallback SSE and any downstream debug logger
        # can still report the actual code + detail. The parent LLM
        # ignores this — it only reads ``instruction``.
        "_swarm_error": envelope,
    }


# ---------------------------------------------------------------------------
# Chat-path adapter
# ---------------------------------------------------------------------------

class SpawnSwarmTool:
    """Synthetic tool for ``AgentRunner``'s chat tool loop.

    Mirrors the shape of the inline tool objects ``ToolDispatcher`` would
    return: ``.name``, ``.description``, async ``.call(args) -> str``,
    ``.to_function_spec()``.
    """

    def __init__(
        self,
        runtime: SwarmRuntime,
        ctx: SwarmContext,
    ) -> None:
        self.name = SPAWN_SWARM_TOOL_NAME
        self.description = _SPAWN_SWARM_DESCRIPTION
        self._runtime = runtime
        self._ctx = ctx

    async def call(self, arguments: Optional[dict]) -> str:
        """Run the swarm and return a JSON-serialised envelope.

        Returning a string (rather than a dict) matches what the chat
        path expects to feed back into the LLM as a ``role: tool``
        message content.
        """
        args = arguments or {}
        goal = (args.get("goal") or "").strip()
        hints = args.get("hints") if isinstance(args.get("hints"), dict) else None
        if not goal:
            return json.dumps({
                "error":  "bad_input",
                "detail": "spawn_swarm requires a non-empty 'goal' string.",
            })
        try:
            envelope = await self._runtime.execute(
                goal=goal, hints=hints, ctx=self._ctx,
            )
        except Exception as exc:  # noqa: BLE001 — top-level safety net
            logger.exception('[AGENT] spawn_swarm: runtime raised')
            envelope = {"error": "swarm_runtime_failure",
                        "detail": str(exc)[:300]}
        # Translate internal-failure envelopes BEFORE the parent LLM
        # sees them. The raw envelope carries validator strings like
        # ``"worker 'jira_worker'.knowledge must be an object"`` that
        # the parent LLM confabulates into user-facing stories
        # ("get_jira_issue not found in tools catalog…"). The translated
        # form gives the parent a prescriptive directive so it falls
        # back to direct execution instead.
        envelope = _translate_internal_failure_envelope(envelope)
        try:
            return json.dumps(envelope, default=str)
        except Exception:
            return json.dumps({
                "error":  "envelope_serialization_failed",
                "detail": "Swarm result could not be serialised.",
            })

    def to_function_spec(self) -> dict:
        return function_spec()


# ---------------------------------------------------------------------------
# Workflow-path adapter (mirrors WorkflowDelegationTool's shape)
# ---------------------------------------------------------------------------

class WorkflowSwarmTool:
    """Synthetic tool the workflow engine's tool loop can call.

    Matches the contract NativeEngine expects of every tool object:
      * ``name`` / ``description`` attributes
      * ``async call(arguments) -> str``
      * ``to_function_spec() -> dict``
    """

    def __init__(
        self,
        runtime_factory: Callable[[], SwarmRuntime],
        ctx_factory: Callable[[], SwarmContext],
    ) -> None:
        # Use factories so the engine can construct a fresh runtime / ctx
        # per ``_run_agent`` call (multiple parallel workflow nodes must
        # not share runtime/ctx state).
        self.name = SPAWN_SWARM_TOOL_NAME
        self.description = _SPAWN_SWARM_DESCRIPTION
        self._runtime_factory = runtime_factory
        self._ctx_factory = ctx_factory

    async def call(self, arguments: Optional[dict]) -> str:
        args = arguments or {}
        goal = (args.get("goal") or "").strip()
        hints = args.get("hints") if isinstance(args.get("hints"), dict) else None
        if not goal:
            return json.dumps({
                "error":  "bad_input",
                "detail": "spawn_swarm requires a non-empty 'goal' string.",
            })
        try:
            runtime = self._runtime_factory()
            ctx     = self._ctx_factory()
            envelope = await runtime.execute(goal=goal, hints=hints, ctx=ctx)
        except Exception as exc:  # noqa: BLE001
            logger.exception('[AGENT] WorkflowSwarmTool: runtime raised')
            envelope = {"error": "swarm_runtime_failure",
                        "detail": str(exc)[:300]}
        # Same translation as the chat path. Symmetric behaviour means
        # the workflow engine's legacy fallback override in
        # ``native_engine.py`` (introduced before this helper existed)
        # no longer needs to fire — the envelope has already been
        # translated into a prescriptive directive by the time the
        # engine sees it.
        envelope = _translate_internal_failure_envelope(envelope)
        try:
            return json.dumps(envelope, default=str)
        except Exception:
            return json.dumps({
                "error":  "envelope_serialization_failed",
                "detail": "Swarm result could not be serialised.",
            })

    def to_function_spec(self) -> dict:
        return function_spec()


__all__ = [
    "SpawnSwarmTool",
    "WorkflowSwarmTool",
    "SPAWN_SWARM_TOOL_NAME",
    "function_spec",
]
