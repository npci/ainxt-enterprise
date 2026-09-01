# SPDX-License-Identifier: Apache-2.0
"""
Orchestration engine — interface, shared types, SSE vocabulary, and factory.

INTERFACE
  OrchestrationEngine   abstract base class all execution engines must implement.
  Routes, services, and tests only import from here; never from native_engine
  or any engine-specific module directly.

TYPES
  ChainDefinition / ChainEdge — engine-agnostic workflow schema (what the
    frontend sends and every engine receives).
  ExecutionContext            — per-run metadata (thread ID, workflow ID/name).

SSE CONTRACT
  SSE_EVENTS  — fixed set of event type strings the frontend hardcodes.
  make_sse()  — formats a server-sent event string.

FACTORY
  get_engine() — returns the process-wide singleton NativeEngine instance.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional


# ---------------------------------------------------------------------------
# Engine-agnostic workflow schema
# ---------------------------------------------------------------------------

@dataclass
class ChainEdge:
    source:        str
    target:        str
    source_handle: Optional[str] = None   # identifies which condition branch


@dataclass
class ChainDefinition:
    """
    Engine-agnostic description of a workflow.
    Nodes are kept as raw dicts because the frontend owns the schema;
    each engine adapter unpacks what it needs from node["data"].
    """
    nodes: List[dict]
    edges: List[ChainEdge]
    # Workflow-level KB blob; engines fall back to this for nodes whose own
    # ``data.knowledge.mode`` is ``"none"``.
    knowledge: Optional[dict] = None


@dataclass
class ExecutionContext:
    thread_id:     Optional[str] = None
    workflow_id:   Optional[str] = None
    workflow_name: Optional[str] = None
    user_id:       str = ""
    email:         str = ""
    # Invoker identity used by kb_retriever for the same PUBLIC + user-dept
    # PRIVATE ACL the chat "Knowledge" toggle applies. Empty department →
    # PUBLIC-only; is_admin=True bypasses the dept filter.
    department:    str = ""
    is_admin:         bool = False
    # Hierarchy fields — carried from the enriched JWT via AuthenticatedUser.
    # ad_level: 0 = most senior executive, 6 = junior (default 6 = most restricted).
    # is_hod: True when this user heads one or more departments.
    # is_security_team: True for IS/security team members (bypasses tool restrictions).
    ad_level:         int  = 6
    is_hod:           bool = False
    is_security_team: bool = False
    # Optional reviewer-edited tool-call list, threaded through from the
    # /resume-stream payload. Carries dicts shaped like _toolcall_to_dict
    # output ({id, name, args}). Consumed only in the engine's `before_tool`
    # resume branch; ignored elsewhere. None = use the snapshot's
    # pending_tool_calls unchanged (default / older-client behaviour).
    pending_tool_calls_override: Optional[list] = None
    # Chat-panel "Run settings" → workflow-wide subagent (swarm) opt-in.
    # Tri-state on purpose:
    #   None  → client didn't send (older builds) → engine default applies
    #   True  → user opted IN at chat panel       → swarm advertised on all
    #                                               otherwise-unpinned nodes
    #   False → user opted OUT at chat panel      → swarm suppressed on all
    #                                               otherwise-unpinned nodes
    # Per-node `data.disable_subagents=True` always wins regardless of this.
    subagents_enabled: Optional[bool] = None
    # ──────────────────────── Loop Engineering (P2+) ────────────────────────
    # All optional — older clients / non-loop runs leave them None and the
    # engine's existing branches are unaffected. LoopRunner reads these
    # (and the related fields below) to wire iterations, budget, and
    # gate-audit writes back to the loop_runs / loop_run_events tables.
    goal_id:            Optional[str]  = None
    loop_id:            Optional[str]  = None
    loop_run_id:        Optional[str]  = None
    # Free-form budget override sent from RunRequest.budget. Shape:
    #   {"tokens": int, "wall_clock_s": int, "max_iterations": int}
    # The BudgetMeter resolves precedence ctx.budget → loop.stopping_condition
    # → goal.stop_condition → env defaults.
    budget:             Optional[Dict[str, Any]] = None
    # 'manual' | 'cron' | 'api' — used by run-audit writers; defaulted to
    # 'manual' by the route handlers.
    trigger_src:        Optional[str]  = None
    # Per-run working directory used by ProofEvaluator's sandbox runner.
    # Left unset in v1 — the sandbox falls through to GENERATED_FILES_DIR
    # (or os.getcwd()) when this is None.
    run_workspace_dir:  Optional[str]  = None
    # Allow-list of connection ids whose env vars may enter the sandbox
    # environment. Honoured fully by ToolDispatcher; the proof-sandbox
    # mirrors that contract.
    allowed_connections: Optional[List[str]] = None
    runtime_artifacts_dir: str = ""
    workflow_run_id: str = ""
    # Uploaded documents for this run. Each entry is a dict shaped like the
    # /agent-runner/attachment extraction envelope, normalised to:
    #   {file_name, file_type, parsed_text, char_count, page_count}
    # The engine copies these onto _ExecState.documents once at execute()-seed
    # time (parse-once), then injects them into agent prompts size-aware:
    # small docs → first agent only; big docs → every agent. None/empty for
    # older clients or text-only runs — the engine's existing paths are
    # unaffected. No RAG/KB: the text is the already-extracted parsed_text.
    attachments: Optional[List[Dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# SSE event vocabulary
#
# The frontend (ChatPanel.jsx) hardcodes these event type names.
# Every engine adapter must emit exactly these strings — nothing else.
# ---------------------------------------------------------------------------

SSE_EVENTS = {
    "start",
    # Lifecycle for the terminal agent only — its tokens flow into the user-
    # visible response bubble. Carries node_id so the client can map events to
    # canvas nodes without relying on display names.
    "agent_start",
    "agent_token",
    "agent_complete",
    # Emitted once per retry attempt when the selected model's stream fails to
    # open on a transient error, BEFORE the fallback engages. Carries {agent,
    # node_id, model, attempt, next_attempt, max_attempts, delay_s, error} so
    # the ChatPanel can append a live "retrying attempt N/M…" status line.
    "agent_retry",
    # Emitted when the selected model failed and the fallback (Sonnet 4.6)
    # transparently took over for this turn. Carries {agent, node_id,
    # primary_model, fallback_model, reason} so the ChatPanel can show a
    # non-blocking "switched to fallback model" notice inline.
    "agent_fallback",
    # Lifecycle for intermediate agents — same shape minus the tokens. Carries
    # {agent, node_id, status: 'running'|'done'} so the client can show every
    # step of a sequential workflow in the live timeline.
    "agent_progress",
    # Tool events fire for every agent (intermediate + terminal) so the timeline
    # can attribute each tool call to the right step.
    "tool_call_start",
    "tool_call_result",
    "condition_flash",
    # Emitted right after `condition_flash` with the routing decision —
    # carries {node_id, matched_case, matched_case_label, expression,
    # next_node, evaluated[], warning?} so the UI can show "why did it
    # branch here?" in the timeline.
    "condition_routed",
    # Loop lifecycle — emitted by _run_loop. Carries
    # {node_id, mode, index, total?} for iteration_start, {node_id, index}
    # for iteration_end, {node_id, total_iterations} for loop_complete.
    # `loop_condition_eval` fires once per round in while-mode loops with
    # {node_id, index, case_results, will_continue, eval_state?} so the UI
    # can show why the loop chose to continue or stop.
    "loop_iteration_start",
    "loop_iteration_end",
    "loop_complete",
    "loop_condition_eval",
    # Per-iteration {node_id, index, score?, changes?, output_preview} report
    # for while-mode loops driven by the continuation contract; the UI uses it
    # to render the Confidence Score pill on each iteration's timeline row.
    "loop_iteration_summary",
    # End-of-loop {node_id, iterations[], initial_score, final_score, delta,
    # final_output, final_structured?, max_iterations_hit} aggregate; the UI
    # turns this into a single assistant chat bubble summarising the run.
    "loop_final_summary",
    "hitl_interrupt",
    # Emitted at the top of /resume-stream when the client asked to resume
    # a run that had previously stopped with reason="node_failed". Carries
    # {thread_id, node_id, agent, reason:"node_failed", previous_error} so
    # the frontend can render a "Retrying node X…" line in the timeline
    # instead of a silent restart.
    "workflow_retrying",
    # Dynamic sub-agent (swarm) lifecycle. The frontend ChatPanel uses these
    # to render the live "N sub-agents working" counter chip + per-sub-agent
    # delegation pills. Emitted in parallel with the legacy swarm_worker_*
    # events (during transition); the new payload contract is:
    #   subagent_start    {call_id, alias, agent_id, parent_agent_id, task_preview}
    #   subagent_complete {call_id, alias, agent_id, parent_agent_id,
    #                      duration_s, ok, error?, preview?}
    "subagent_start",
    "subagent_complete",
    # ──────────────────────── Loop Engineering (P2+) ────────────────────────
    # NOTE: the legacy `outer_loop_iteration` event was removed together
    # with the retired outer_loop canvas node. Iteration heartbeats are
    # no longer emitted; per-iteration state is written to loops_repo
    # events instead. Do not re-introduce this event name — a future
    # Loop-node heartbeat should use a fresh identifier so it can't be
    # confused with the retired outer-loop surface.
    # Budget consumption snapshot — fires after each iteration. Payload:
    # {run_id, tokens, wall_clock_s, cap: {tokens_cap, wall_clock_cap_s,
    # max_iterations}}.
    "budget_consumed",
    # Goal predicate verdict — fires when a Goal is attached to the run.
    # {score: 0..1, met: bool, critique: str}.
    "goal_evaluated",
    # Inside-the-workflow gate (LLM-judge node). On the canvas the node
    # has two source handles, "pass" and "fail"; the runner emits one
    # event per evaluation with {gate_id, score, critique, next_node}.
    "evaluation_gate_passed",
    "evaluation_gate_failed",
    # P3 — worktree lifecycle (consumed in PHASE_3_WORKTREE.md).
    "worktree_acquired",
    "worktree_released",
    # P4 — independent VerifierAgent + comprehension digest.
    "verifier_started",
    "verifier_pass",
    "verifier_fail",
    "comprehension_digest",
    # P5 — reflection + triage + degradation router.
    "memory_recalled",
    "memory_read",
    "memory_write",
    "reflection_written",
    "triage_finding",
    "triage_started",
    "triage_completed",
    "triage_overflow",
    "triage_failed",
    "goal_proposed",
    "loop_degraded",
    "complete",
    "error",
}

# Loop node source-handle ids. Mirrors the condition node's per-case ids
# (e.g. 'case-xyz' / 'else') — loop nodes route into 'body' (the iterating
# subgraph) or 'exit' (the post-loop continuation).
LOOP_HANDLES = {"body", "exit"}

# Condition node handle ids. `else` is the catch-all branch; the synthetic
# `__unrouted__` slot stores edges that arrived without a source_handle so
# the misconfig is visible rather than silently masquerading as ELSE.
CONDITION_ELSE_HANDLE = "else"
CONDITION_UNROUTED_HANDLE = "__unrouted__"

# Loop node iteration modes. Kept in lock-step with workflowStore.js's
# getDefaultNodeData('loop') so frontend / backend cannot drift.
LOOP_MODE_FOR_EACH = "for_each"
LOOP_MODE_WHILE    = "while"
LOOP_MODE_COUNT    = "count"
LOOP_MODES = {LOOP_MODE_FOR_EACH, LOOP_MODE_WHILE, LOOP_MODE_COUNT}


def make_sse(event_type: str, data: dict) -> str:
    """Format a server-sent event string."""
    return f"data: {json.dumps({'event': event_type, 'data': data})}\n\n"


# ---------------------------------------------------------------------------
# Canonical "which tools does this graph bind" accessor
# ---------------------------------------------------------------------------
# A node's runtime toolset comes from TWO independent sources that the
# engine resolves separately (see NativeEngine._run_agent /
# NativeEngine._resolve_tools):
#   1. ``data.tools`` — catalog tools attached via the picker. Entries are
#      usually ``{"name": ..., "description": ..., "input_schema": ...}``
#      dicts, but some call sites (and older saved graphs) pass bare
#      strings, so both shapes must be accepted.
#   2. MCP server nodes wired to the agent via an edge (either direction —
#      MCP → Agent or Agent → MCP). These never appear in ``data.tools`` at
#      all; the engine discovers them by walking the edge list
#      (``resolve_agent_mcp_configs``) and only knows their *tool names*
#      once the server subprocess is actually spawned and asked for its
#      tool list — which requires a live connection, not just static graph
#      analysis. For governance/integrity checks that must run BEFORE any
#      subprocess is spawned (e.g. deciding whether to allow a run at all),
#      we instead track MCP *server attachment* as a coarse proxy: any
#      agent node with an MCP node wired to it is treated as bound to
#      that server's synthetic identifier "mcp:<server_type>". This can't
#      list the server's individual tool names ahead of time, but it does
#      let a policy check detect "this graph attaches a NEW MCP server
#      that wasn't in the saved graph" — which is the same class of
#      tool-binding-smuggling concern as a new catalog tool.
#
# This function is the SINGLE place both concerns are read from, so a
# governance check built on top of it can't drift out of sync with what the
# engine actually dispatches — the drift risk that motivated pulling this
# out of app/api/execution.py's own bespoke graph walk.
def extract_bound_tool_names(nodes: List[dict], edges: Optional[list] = None) -> set:
    """Return the set of tool/server identifiers a graph's agent nodes bind.

    ``edges`` may be a list of dicts, ``ChainEdge``/pydantic ``Edge``
    objects, or ``None`` (MCP attachment is simply skipped in that case).
    Never raises — malformed nodes/edges are skipped rather than failing
    the caller.
    """
    names: set = set()
    if not nodes:
        return names

    nodes_by_id = {}
    for node in nodes:
        if isinstance(node, dict) and node.get("id"):
            nodes_by_id[node["id"]] = node

    # ── 1. data.tools (catalog tools) ────────────────────────────────
    for node in nodes:
        if not isinstance(node, dict):
            continue
        data = node.get("data") or {}
        if not isinstance(data, dict):
            continue
        for entry in (data.get("tools") or []):
            name = entry.get("name") if isinstance(entry, dict) else entry
            if name:
                names.add(str(name))

    # ── 2. MCP server nodes wired to an agent node via an edge ───────
    if edges:
        try:
            from app.core.mcp_manager import resolve_agent_mcp_configs
        except Exception:
            resolve_agent_mcp_configs = None
        if resolve_agent_mcp_configs is not None:
            norm_edges = []
            for e in edges:
                if isinstance(e, dict):
                    norm_edges.append(e)
                else:
                    src = getattr(e, "source", None)
                    tgt = getattr(e, "target", None)
                    if src and tgt:
                        norm_edges.append({"source": src, "target": tgt})
            for node_id, node in nodes_by_id.items():
                if node.get("type") != "agent":
                    continue
                try:
                    mcp_configs = resolve_agent_mcp_configs(node_id, nodes_by_id, norm_edges)
                except Exception:
                    continue
                for cfg in mcp_configs or []:
                    server_type = cfg.get("server_type")
                    if server_type:
                        names.add(f"mcp:{server_type}")

    return names


# ---------------------------------------------------------------------------
# Abstract engine interface
# ---------------------------------------------------------------------------

class OrchestrationEngine(ABC):

    @abstractmethod
    async def startup(self) -> None:
        """Called once on FastAPI startup — initialise connections, pools, etc."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Called once on FastAPI shutdown — release all resources cleanly."""

    @abstractmethod
    async def execute(
        self,
        chain: ChainDefinition,
        user_input: str,
        context: ExecutionContext,
    ) -> AsyncIterator[str]:
        """
        Execute a chain and yield SSE-formatted strings.

        Every yielded string must come from make_sse() using one of the
        event types in SSE_EVENTS.  The stream must end with either a
        'complete' event or an 'error' event.

        Minimal example:
            yield make_sse("start",         {"thread_id": context.thread_id})
            yield make_sse("agent_start",   {"agent": "MyAgent"})
            yield make_sse("agent_token",   {"agent": "MyAgent", "token": "Hi"})
            yield make_sse("agent_complete",{"agent": "MyAgent", "output": "Hi"})
            yield make_sse("complete",      {"output": "Hi", "execution_trace": []})
        """

    @abstractmethod
    async def resume(
        self,
        chain: ChainDefinition,
        human_input: str,
        context: ExecutionContext,
    ) -> AsyncIterator[str]:
        """
        Resume a HITL-interrupted execution with the human's response.
        Same SSE contract as execute().
        context.thread_id identifies the paused execution.
        """

    @abstractmethod
    async def get_history(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Return persisted chat history for a thread.
        Shape: {"thread_id": str, "messages": [{"role": "user"|"assistant", "content": str}]}

        ``owner_user_id`` (security review F-06): when given, the thread's
        recorded owner must match or an empty history is returned — see
        NativeEngine.get_history / CheckpointStore.load_messages for the
        exact "no recorded owner = accessible" migration-safety rule.
        """

    async def get_thread_owner(self, thread_id: str) -> Optional[str]:
        """Return the thread's recorded owner, "" if it exists with no
        recorded owner, or None if the thread doesn't exist.

        Security review F-06/F-10 follow-up: used by the run entrypoints
        (/run, /run-stream, /resume-stream) to reject a client-supplied
        thread_id that belongs to a different user BEFORE any read/write
        happens — closing the gap where the read/delete routes in chat.py
        were owner-scoped but the run/write path was not. Default no-op
        returns None so engines without a chat-history store keep working.
        """
        return None

    @abstractmethod
    async def list_threads(
        self, workflow_id: str, owner_user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        List all chat threads for a workflow, newest first.
        Shape: [{"thread_id", "title", "last_message_preview", "last_updated", "message_count"}]

        ``owner_user_id``: when given, only threads owned by that user (or
        with no recorded owner) are returned.
        """

    async def delete_thread(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> bool:
        """Delete a thread and its dependent rows. Returns True if removed.

        ``owner_user_id``: when given and it doesn't match the thread's
        recorded owner, the delete is refused (returns False).

        No default implementation: unlike ``get_pending_interrupt`` /
        ``get_node_last_output`` (where "this engine has no HITL/loop
        support" is a legitimate reason to return an empty result), a
        chat-history-capable engine either supports thread deletion or it
        doesn't — silently returning False here would be indistinguishable
        from "thread not found / not yours" at the API layer (chat.py maps
        False to a 404), masking an engine bug as a routine access-control
        outcome. Engines without a chat-history store must override this
        explicitly (returning False is fine there) rather than relying on
        an implicit base-class default.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement delete_thread()"
        )

    async def delete_threads_for_workflow(self, workflow_id: str) -> int:
        """Delete all chat history for a workflow. Returns threads removed.

        Default no-op; engines with a backing store override this.
        """
        return 0

    async def get_pending_interrupt(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return a frontend-safe view of the pending HITL snapshot or None.

        Default implementation returns None — engines that support HITL
        override this. Used by GET /chat-pending/{thread_id} so a
        reconnecting client can re-render the HITL card without waiting
        for an SSE event. ``owner_user_id``: see get_history.
        """
        return None

    async def clear_pending_interrupt(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> bool:
        """Discard the pending interrupt snapshot for a thread.

        Returns True if a snapshot was actually removed. Default no-op
        returns False; engines with a backing store override this.
        """
        return False

    async def get_node_last_output(
        self, thread_id: str, node_id: str, owner_user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return {agent, output, updated_at} for the last run of a node, or None.

        Powers the Loop node's connection-aware list picker — the frontend
        looks up the upstream node's most recent output so it can surface
        lists as click-to-pick options instead of asking for a typed path.
        Default returns None; engines override to read from their store.
        """
        return None

    @abstractmethod
    async def health(self) -> Dict[str, str]:
        """Return engine status info for the /health endpoint."""


# ---------------------------------------------------------------------------
# Engine factory — singleton
# ---------------------------------------------------------------------------

_instance: Optional[OrchestrationEngine] = None


def get_engine() -> OrchestrationEngine:
    """Return the process-wide singleton NativeEngine instance.

    startup() is called separately in the FastAPI lifespan handler.
    """
    global _instance
    if _instance is not None:
        return _instance

    from .native_engine import NativeEngine
    _instance = NativeEngine()
    return _instance
