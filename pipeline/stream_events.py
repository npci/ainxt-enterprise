# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Structured SSE stream events — tool / reasoning (pure builders)
# ============================================================
#
# docs/architecture/16-streaming.md §16.2-16.4. Today tool activity surfaces only
# as coarse `{"status": "..."}` strings; the UI can't show which tool, its args,
# progress, or per-tool result/error. This module builds the STRUCTURED event
# dicts the gateway emits (additive to the SSE envelope — clients ignore unknown
# keys, so it's backward-compatible). Reasoning deltas likewise get a first-class
# event instead of being revealed only at the end.
#
# Pure builders (no I/O). The gateway serializes these to `data: {json}\n\n`
# behind PIPELINE_V2_STREAM. Keeping them pure makes the envelope contract
# unit-testable and stable across every client (web, Buddy, CLI, Teams).
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolMarker:
    """A typed sentinel a generator (e.g. OrchestratorAgent.run) can yield to
    signal tool activity WITHOUT breaking a str-only token contract.

    Consumers that understand it (the Phase 5 gateway loop) translate it into a
    tool_event() SSE frame and never append it to the answer text. Consumers
    that don't will str()-coerce it — so we give it an empty __str__ to make that
    degradation harmless (no dict noise leaks into an answer)."""

    tool_id: str
    name: str
    phase: str
    summary: str = ""
    ok: Optional[bool] = None
    args: Optional[Dict[str, Any]] = field(default=None)

    def __str__(self) -> str:  # harmless if a non-aware consumer coerces it
        return ""

    def to_event(self) -> Dict[str, Any]:
        return tool_event(self.tool_id, self.name, self.phase,
                          args=self.args, summary=self.summary, ok=self.ok)


@dataclass
class ReasoningMarker:
    """A typed sentinel a generator yields to stream a REASONING (extended-
    thinking) delta live, without breaking a str-only token contract.

    Like ToolMarker, it str()-coerces to "" so a non-aware consumer that
    accumulates tokens can NEVER let reasoning text leak into the final answer.
    A Phase-5-aware consumer translates it into a reasoning_event() SSE frame."""

    delta: str = ""

    def __str__(self) -> str:  # reasoning must NEVER pollute the answer text
        return ""

    def to_event(self) -> Dict[str, Any]:
        return reasoning_event(self.delta)


# tool event phases (docs/architecture/16 §16.3)
START = "start"
PROGRESS = "progress"
RESULT = "result"
ERROR = "error"

_VALID_PHASES = {START, PROGRESS, RESULT, ERROR}


def tool_event(
    tool_id: str,
    name: str,
    phase: str,
    *,
    args: Optional[Dict[str, Any]] = None,
    summary: str = "",
    detail: str = "",
    ok: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build a structured tool event: {"tool": {...}}.

    phase ∈ {start, progress, result, error}. Unknown phase coerced to progress
    (never emit an invalid contract). Only non-empty fields are included so the
    payload stays compact.
    """
    if phase not in _VALID_PHASES:
        phase = PROGRESS
    payload: Dict[str, Any] = {"id": str(tool_id), "name": str(name), "phase": phase}
    if args:
        payload["args"] = args
    if summary:
        payload["summary"] = summary
    if detail:
        payload["detail"] = detail
    if ok is not None:
        payload["ok"] = bool(ok)
    return {"tool": payload}


def reasoning_event(delta: str) -> Dict[str, Any]:
    """Build a streamed reasoning delta: {"reasoning": {"delta": "..."}}."""
    return {"reasoning": {"delta": delta or ""}}


def plan_event(
    shape: str,
    *,
    steps: Optional[List[str]] = None,
    reason: str = "",
) -> Dict[str, Any]:
    """Build a user-visible plan-panel event: {"plan": {...}}.

    Surfaces the planner/CIL shape (direct|retrieve|clarify|tool_use|decompose)
    so the client can render a "here's my plan" panel like Claude/ChatGPT. Only
    non-empty fields are included. Additive to the SSE envelope — old clients
    ignore the `plan` key.
    """
    payload: Dict[str, Any] = {"shape": str(shape or "direct")}
    _steps = [str(s).strip() for s in (steps or []) if str(s).strip()]
    if _steps:
        payload["steps"] = _steps
    if reason:
        payload["reason"] = reason
    return {"plan": payload}


def group_read_only(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse a run of consecutive read-only tool RESULT events into a single
    group event for the UI (§16.3, Buddy's ToolGroup). Pure; input unchanged.

    A tool is 'read-only' when its result event carries ok=True and no mutation
    marker; here we simply group consecutive result-phase tool events. Mutating
    tools should be emitted with phase=result individually by the caller.
    """
    out: List[Dict[str, Any]] = []
    run: List[Dict[str, Any]] = []

    def _flush():
        if not run:
            return
        if len(run) == 1:
            out.append(run[0])
        else:
            out.append({"tool_group": {"count": len(run),
                                       "tools": [e["tool"]["name"] for e in run]}})
        run.clear()

    for e in events or []:
        t = e.get("tool")
        if t and t.get("phase") == RESULT and t.get("ok", True):
            run.append(e)
        else:
            _flush()
            out.append(e)
    _flush()
    return out
