# SPDX-License-Identifier: MIT
"""cli_runtime.event_mapper — translate a CLI run into ABStudio's SSE vocabulary.

The frontend must not be able to tell whether a turn ran natively or in a CLI
subprocess, so this module reproduces the existing event payloads exactly. Three
of them are contractual and break other subsystems silently if their shape drifts:

``agent_usage`` / ``agent_complete``
    Consumed by ``execution._TRACED_EVENTS`` and ``RunUsageTracker``. Wrong keys
    mean Grafana cost/token tracing goes dark without any error.
``complete`` / ``agent_complete`` ``.output``
    ``LoopRunner._capture_terminal_output`` reads this to decide whether a loop
    iteration produced anything. Wrong shape means loops silently capture nothing.
``tool_call_result``
    Capped at 50 KB, matching ``native_engine``, so a large tool result cannot
    flood the browser.

Two streams, one timeline
-------------------------
Text comes from the CLI's stdout; tool activity comes from our own MCP server
(the CLI's ``streaming-json`` output contains no tool events at all). ``merge``
interleaves them: each time a text event arrives, any tool events queued since
the last one are flushed first, which keeps the visible ordering causal — the
tool card appears before the text that describes its result.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from .runner import EV_END, EV_ERROR, EV_TEXT, EV_THOUGHT, CliEvent, normalise_usage
from .session import TOOL_EVENT_RESULT, TOOL_EVENT_START, RunSession, ToolEvent

# Matches ``native_engine``'s cap so the Debug Log behaves identically.
SSE_RESULT_MAX = 50_000


def tool_event_to_sse(
    event: ToolEvent, *, agent_name: str,
) -> Tuple[str, Dict[str, Any]]:
    """Convert a tool event into ``(sse_event_name, payload)``.

    Payloads mirror ``native_engine`` exactly: ``tool_call_start`` carries
    ``{agent, tool_name, arguments}`` and ``tool_call_result`` carries
    ``{agent, tool_name, result}`` plus truncation metadata when the result was
    clipped.
    """
    if event.kind == TOOL_EVENT_START:
        return "tool_call_start", {
            "agent": agent_name,
            "tool_name": event.tool_name,
            "arguments": event.arguments or {},
        }

    result_str = _stringify(event.error or event.result)
    payload: Dict[str, Any] = {
        "agent": agent_name,
        "tool_name": event.tool_name,
        "result": result_str[:SSE_RESULT_MAX],
    }
    if len(result_str) > SSE_RESULT_MAX:
        payload["truncated"] = True
        payload["full_length"] = len(result_str)
    return "tool_call_result", payload


def _stringify(value: Any) -> str:
    """Render a tool result for display without ever raising."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)


class CliTurnResult:
    """Accumulated outcome of one CLI turn.

    The caller needs the final text, usage, generated files and the CLI session
    id (for a later ``--resume``) after the stream has finished, so they are
    collected here rather than reconstructed from the SSE frames.
    """

    def __init__(self) -> None:
        self.text_parts: List[str] = []
        self.thoughts: List[str] = []
        self.usage: Dict[str, Any] = {}
        self.session_id: str = ""
        self.stop_reason: str = ""
        self.num_turns: int = 0
        self.error: str = ""
        self.generated_files: List[dict] = []
        self.tool_calls: int = 0
        self.engine_native_requests: List[dict] = []

    @property
    def output(self) -> str:
        return "".join(self.text_parts).strip()

    @property
    def ok(self) -> bool:
        return not self.error

    def as_log_fields(self) -> dict:
        return {
            "ok": self.ok,
            "output_chars": len(self.output),
            "tool_calls": self.tool_calls,
            "num_turns": self.num_turns,
            "stop_reason": self.stop_reason,
            "total_tokens": self.usage.get("total_tokens", 0),
            "generated_files": len(self.generated_files),
            "error": self.error[:200],
        }


async def merge(
    cli_events: AsyncIterator[CliEvent],
    *,
    agent_name: str,
    result: CliTurnResult,
    session: Optional[RunSession] = None,
    session_provider: Optional[Callable[[], Optional[RunSession]]] = None,
    emit_tokens: bool = True,
) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
    """Yield ``(sse_event, payload)`` pairs for one CLI turn.

    Interleaves tool events from the session bus with text from the CLI, and
    accumulates everything into ``result``. Terminal ``agent_usage`` /
    ``agent_complete`` frames are NOT emitted here — the caller owns those,
    because only it knows whether this node is the workflow's final agent (which
    decides ``agent_complete`` vs ``agent_progress``).

    Pass either ``session`` (when it already exists) or ``session_provider`` (a
    zero-arg callable resolved on each use, for when the session is created by the
    same call that produces ``cli_events``).

    ``emit_tokens=False`` suppresses ``agent_token`` for non-final nodes, matching
    the native engine, which only streams tokens for the final agent.
    """
    def _session() -> Optional[RunSession]:
        if session is not None:
            return session
        return session_provider() if session_provider is not None else None

    def _absorb(tool_event: ToolEvent) -> None:
        """Fold a tool result into the accumulated turn state."""
        if tool_event.kind != TOOL_EVENT_RESULT:
            return
        result.tool_calls += 1
        if tool_event.generated_files:
            result.generated_files.extend(tool_event.generated_files)
        native = _engine_native_request(tool_event)
        if native:
            result.engine_native_requests.append(native)

    # Response text is buffered rather than streamed token-by-token, then scrubbed
    # and emitted once. Path redaction cannot be done per token: an internal path
    # (``D:\...\ABStudio\tmp\x``) is routinely split across several chunks, and a
    # per-chunk regex would miss the boundary and leak. Buffering the whole thing
    # first is the only reliable way to scrub it (see sanitize.scrub_paths).
    from .sanitize import scrub_paths

    raw_text_parts: List[str] = []

    async for event in cli_events:
        # Flush queued tool activity BEFORE the response text, so tool cards
        # always precede the answer that talks about their results.
        live = _session()
        if live is not None:
            for tool_event in live.drain_events():
                _absorb(tool_event)
                yield tool_event_to_sse(tool_event, agent_name=agent_name)

        if event.type == EV_TEXT:
            raw_text_parts.append(event.text)

        elif event.type == EV_THOUGHT:
            # Reasoning tokens are captured for logs but not surfaced: the native
            # path has no equivalent event, and inventing one would change the UI.
            result.thoughts.append(event.text)

        elif event.type == EV_END:
            # v1 delivers the whole response on the terminal object; 0.2.x streams
            # EV_TEXT chunks. Either way the text is buffered and scrubbed below.
            if event.text:
                raw_text_parts.append(event.text)
            result.usage = normalise_usage(event.usage)
            result.session_id = event.session_id
            result.stop_reason = event.stop_reason
            result.num_turns = event.num_turns

        elif event.type == EV_ERROR:
            result.error = event.message or "the CLI reported an error"

    # Scrub the assembled response once, store it, and emit it as a single token.
    scrubbed = scrub_paths("".join(raw_text_parts))
    if scrubbed:
        result.text_parts.append(scrubbed)
        if emit_tokens:
            yield "agent_token", {"agent": agent_name, "token": scrubbed}

    # Drain anything published between the last text event and process exit.
    live = _session()
    if live is not None:
        for tool_event in live.drain_events():
            _absorb(tool_event)
            yield tool_event_to_sse(tool_event, agent_name=agent_name)
        # Files recorded on the session are authoritative (deduplicated there).
        if live.generated_files:
            result.generated_files = list(live.generated_files)


def _engine_native_request(event: ToolEvent) -> Optional[dict]:
    """Detect an ``ask_human`` / ``spawn_swarm`` sentinel in a tool result.

    Those two tools cannot execute inside the CLI (one drives the HITL suspend
    protocol, the other needs a live in-process swarm runtime), so the MCP server
    returns a marked payload and the caller runs the native path instead.
    """
    from .mcp_server import ENGINE_NATIVE_SENTINEL

    result = event.result
    if isinstance(result, dict) and result.get(ENGINE_NATIVE_SENTINEL):
        return {
            "tool": result.get("tool") or event.tool_name,
            "arguments": result.get("arguments") or {},
        }
    return None


def usage_events(
    *,
    agent_name: str,
    node_id: str,
    model: str,
    usage: Dict[str, Any],
) -> List[Tuple[str, Dict[str, Any]]]:
    """Build the ``agent_usage`` frame.

    Kept here so the payload lives beside the rest of the mapping, and because
    both the workflow and chat integrations must emit an identical shape for cost
    tracing to keep working.
    """
    return [("agent_usage", {
        "agent": agent_name,
        "node_id": node_id,
        "model": model,
        "usage": usage or {},
    })]


__all__ = [
    "SSE_RESULT_MAX",
    "CliTurnResult",
    "merge",
    "tool_event_to_sse",
    "usage_events",
]
