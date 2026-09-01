# SPDX-License-Identifier: Apache-2.0
"""cli_runtime.session — the per-run registry that binds a CLI process to a user.

A spawned ``ainxt`` process is anonymous: it holds no JWT and knows nothing about
who started it. This module is what gives it an identity, and nothing more than
that identity is entitled to.

At spawn time the runner registers a ``RunSession`` carrying the caller's
``user_id`` / ``email``, the exact tools and skills this agent may touch, and a
freshly minted random token. The token is written into the run's private
``.ainxt/config.toml`` as an ``Authorization: Bearer`` header. When the CLI calls
back, the MCP router looks up ``run_id``, compares the token in constant time, and
dispatches with that user's identity — so every GitLab / Jira / Confluence call
runs under the user's own personal access token, exactly as it does today.

Three properties make this safe:

* **Per-run.** A token authorises one run, not a user or a session. It is revoked
  in the runner's ``finally``, so it is dead the moment the process exits.
* **Scoped.** ``allowed_tools`` and ``allowed_skills`` are fixed at spawn time
  from the agent definition. A prompt-injected CLI cannot widen its own surface.
* **Local.** The endpoint is loopback and the token never leaves this host.

The session also owns the **event bus**. This is not incidental: the CLI's
``streaming-json`` output contains no tool-call events at all (only ``text``,
``thought``, ``end``, ``error``), so tool activity for the UI can only come from
the side that actually executes the tools — us. Each ``tools/call`` publishes a
start and a result frame here, and the SSE bridge merges them into the token
stream.

ARCH-F-ABS1-005 (2026-08-26): the event bus is a BOUNDED queue (see
``_EVENTS_MAXSIZE``), not unbounded. The original justification for
unbounded — "the volume is bounded anyway by the CLI's own --max-turns" — is
true for the STEADY-STATE case (a well-behaved run publishing a handful of
tool events per turn), but it does not bound the RATE at which
``drain_events()`` is called relative to publish(): the SSE consumer
(``event_mapper.stream_cli_events``) only drains between CLI text/thought
events, and a fast tool-call producer (e.g. an agent making several rapid
tool calls with no intervening model text) can enqueue many events before the
generator's consumer loop gets back around to draining them. A slow SSE
consumer — a laggy client connection, a browser tab that throttles background
timers, or simply GC/scheduler pressure on the server — can fall further
behind still. An unbounded queue in that situation grows without limit for
the life of the run, and every event in it silently reached the UI late or,
if the run ends first, not at all (the queue is simply dropped with the
session). A bounded queue with an explicit drop-oldest policy fails the same
way MORE VISIBLY: it logs a warning and increments a Prometheus counter the
moment it starts shedding load, instead of growing memory quietly forever.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.logger import logger

# ── ARCH-F-ABS1-005: bounded event bus + observability ──────────────────────
#
# Bound: generous enough that a normal run (a handful of tool events per turn,
# up to ABSTUDIO_CLI_MAX_TURNS turns) never comes close to it, but finite so a
# pathological producer/consumer mismatch sheds load instead of growing memory
# forever. Overridable per-deployment via env var without a code change.
_EVENTS_MAXSIZE = int(os.getenv("ABSTUDIO_CLI_EVENTS_QUEUE_MAXSIZE", "500"))

# Prometheus metrics (prometheus_client is an existing platform dependency —
# see core/telemetry.py for the same Counter/Gauge pattern used elsewhere).
# Import is best-effort: metrics are observability, not correctness, and must
# never be a reason a CLI run fails to start.
try:
    from prometheus_client import Counter as _PromCounter, Gauge as _PromGauge

    _EVENTS_DROPPED_TOTAL = _PromCounter(
        "abstudio_cli_events_dropped_total",
        "Tool events dropped from a RunSession's bounded event queue "
        "(drop-oldest policy) because the queue was full.",
    )
    _EVENTS_QUEUE_DEPTH = _PromGauge(
        "abstudio_cli_events_queue_depth",
        "Current depth of the most recently updated RunSession event queue "
        "(sampled on publish/drain, not a per-session series — see the "
        "per-event warning log for which run_id was affected on drop).",
    )
except Exception:  # pragma: no cover — metrics are optional, never fatal
    _EVENTS_DROPPED_TOTAL = None
    _EVENTS_QUEUE_DEPTH = None


def _record_drop() -> None:
    if _EVENTS_DROPPED_TOTAL is not None:
        try:
            _EVENTS_DROPPED_TOTAL.inc()
        except Exception:
            pass


def _record_depth(depth: int) -> None:
    if _EVENTS_QUEUE_DEPTH is not None:
        try:
            _EVENTS_QUEUE_DEPTH.set(depth)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
# Tool events (published by the MCP layer, consumed by the SSE bridge)
# ════════════════════════════════════════════════════════════════════════════

TOOL_EVENT_START = "tool_call_start"
TOOL_EVENT_RESULT = "tool_call_result"


@dataclass
class ToolEvent:
    """One tool-activity frame, mapped 1:1 onto an ABStudio SSE event.

    ``kind`` is already the SSE event name so the mapper stays trivial.
    """

    kind: str                       # TOOL_EVENT_START | TOOL_EVENT_RESULT
    tool_name: str
    arguments: Optional[dict] = None
    result: Any = None
    error: str = ""
    duration_ms: int = 0
    generated_files: List[dict] = field(default_factory=list)


# ════════════════════════════════════════════════════════════════════════════
# Run session
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class RunSession:
    """Everything the MCP layer needs to serve one CLI run.

    Created by the runner, consumed by the router. Never persisted: a session is
    only meaningful while its subprocess is alive.
    """

    run_id: str
    token: str
    user_id: str = ""
    email: str = ""

    # Scope, fixed at spawn time from the agent definition.
    allowed_tools: List[str] = field(default_factory=list)
    allowed_skills: List[str] = field(default_factory=list)

    # Where ``code_executor`` writes artefacts.
    workflow_artifact_dir: str = ""

    # Optional per-agent Sample Document reference (see
    # ``app/api/agent_sample.py``). Empty strings when no sample is
    # attached. Consumed by ``mcp_server._dispatch`` to forward into
    # ``ToolDispatcher.dispatch(sample_doc_path=..., sample_doc_kind=...)``
    # so the sandbox env carries SAMPLE_DOC_* at code_executor time.
    sample_doc_path: str = ""
    sample_doc_kind: str = ""

    # Labels for SSE payloads and logs.
    agent_name: str = ""
    node_id: str = ""

    expires_at: float = 0.0

    # Live state.
    # ARCH-F-ABS1-005: bounded (see _EVENTS_MAXSIZE) — was an unbounded
    # asyncio.Queue(). publish()/close_events() below handle the resulting
    # QueueFull case explicitly with a drop-oldest policy instead of letting
    # the queue grow forever.
    events: "asyncio.Queue[Optional[ToolEvent]]" = field(
        default_factory=lambda: asyncio.Queue(maxsize=_EVENTS_MAXSIZE), repr=False,
    )
    events_dropped: int = 0          # per-session drop count, surfaced in logs
    tool_calls: int = 0
    generated_files: List[dict] = field(default_factory=list, repr=False)
    listed_tool_count: int = -1     # -1 = tools/list not yet served
    handshake_done: bool = False

    # ── scope checks ───────────────────────────────────────────────────────
    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) > self.expires_at

    def token_matches(self, presented: str) -> bool:
        """Constant-time token comparison (avoids a timing side-channel)."""
        if not presented or not self.token:
            return False
        return hmac.compare_digest(str(presented), str(self.token))

    def allows_tool(self, name: str) -> bool:
        return bool(name) and name in set(self.allowed_tools)

    # ── event publishing ───────────────────────────────────────────────────
    def publish(self, event: ToolEvent) -> None:
        """Publish a tool event. Never blocks and never raises.

        ARCH-F-ABS1-005: the bus is bounded (_EVENTS_MAXSIZE). On overflow we
        drop the OLDEST queued event (not the newest) to make room, because a
        stale start/result frame from several tool calls ago is less useful to
        the UI than the current one — and because dropping the newest would
        require snapshotting/re-queueing under the same "never blocks" budget
        this method promises. Every drop is logged with the run_id and
        recorded as a Prometheus counter so a slow-consumer session is
        diagnosable in production, not silently invisible.
        """
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self.events.get_nowait()   # drop oldest
            except asyncio.QueueEmpty:
                pass
            else:
                self.events_dropped += 1
                _record_drop()
                logger.warning(
                    "cli_runtime.session: event queue full — dropped oldest event to admit a new one",
                    run_id=self.run_id, tool=event.tool_name,
                    maxsize=_EVENTS_MAXSIZE, total_dropped=self.events_dropped,
                )
            try:
                self.events.put_nowait(event)
            except Exception:
                # Queue still full after evicting one item (a concurrent
                # drain() emptied and refilled it, or maxsize=0) — give up on
                # this frame rather than looping; never raise from publish().
                pass
        except Exception:
            pass
        _record_depth(self.events.qsize())

    def close_events(self) -> None:
        """Push the sentinel that tells a draining consumer to stop.

        On overflow, drop the oldest event to make room — the sentinel MUST
        be enqueued so drain loops terminate instead of hanging on an empty
        queue forever; better to lose one stale tool-activity frame than to
        never signal end-of-stream.
        """
        try:
            self.events.put_nowait(None)
        except asyncio.QueueFull:
            try:
                self.events.get_nowait()
            except asyncio.QueueEmpty:
                pass
            else:
                self.events_dropped += 1
                _record_drop()
                logger.warning(
                    "cli_runtime.session: event queue full while closing — dropped "
                    "oldest event to enqueue the end sentinel",
                    run_id=self.run_id, maxsize=_EVENTS_MAXSIZE, total_dropped=self.events_dropped,
                )
            try:
                self.events.put_nowait(None)
            except Exception:
                pass
        except Exception:
            pass

    def drain_events(self) -> List[ToolEvent]:
        """Non-blocking: pop every event queued so far.

        Used by the bridge to interleave tool frames with CLI text frames
        without awaiting.
        """
        out: List[ToolEvent] = []
        while True:
            try:
                item = self.events.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None:
                break
            out.append(item)
        return out

    def record_files(self, files: List[dict]) -> None:
        """Accumulate generated files, de-duplicated by disk name.

        A tool may be called several times in one run; the UI wants each artefact
        once.
        """
        if not files:
            return
        seen = {f.get("disk_name") for f in self.generated_files if isinstance(f, dict)}
        for f in files:
            if not isinstance(f, dict):
                continue
            key = f.get("disk_name")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            self.generated_files.append(f)


# ════════════════════════════════════════════════════════════════════════════
# Registry
# ════════════════════════════════════════════════════════════════════════════

class SessionRegistry:
    """Process-local map of ``run_id`` → ``RunSession``.

    Process-local is correct here rather than a limitation: the CLI child talks
    to loopback, so it always reaches the worker that spawned it. (Cowork's MCP
    router needs Redis because its client is a remote desktop app that may hit
    any worker; ours cannot.)
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, RunSession] = {}
        self._lock = asyncio.Lock()

    # ── lifecycle ──────────────────────────────────────────────────────────
    def register(
        self,
        *,
        run_id: str,
        user_id: str = "",
        email: str = "",
        allowed_tools: Optional[List[str]] = None,
        allowed_skills: Optional[List[str]] = None,
        workflow_artifact_dir: str = "",
        sample_doc_path: str = "",
        sample_doc_kind: str = "",
        agent_name: str = "",
        node_id: str = "",
        ttl_seconds: int = 960,
    ) -> RunSession:
        """Create and store a session, returning it with its minted token.

        ``ttl_seconds`` should exceed the run timeout so a slow-but-healthy run
        is never cut off by its own credentials expiring; the runner revokes
        explicitly on exit, so the TTL is only a backstop against a leak.
        """
        session = RunSession(
            run_id=run_id,
            token=secrets.token_urlsafe(32),
            user_id=user_id or "",
            email=email or "",
            allowed_tools=list(allowed_tools or []),
            allowed_skills=list(allowed_skills or []),
            workflow_artifact_dir=workflow_artifact_dir or "",
            sample_doc_path=sample_doc_path or "",
            sample_doc_kind=sample_doc_kind or "",
            agent_name=agent_name or "",
            node_id=node_id or "",
            expires_at=time.time() + max(60, int(ttl_seconds)),
        )
        self._sessions[run_id] = session
        return session

    def revoke(self, run_id: str) -> Optional[RunSession]:
        """Remove a session and close its bus. Idempotent."""
        session = self._sessions.pop(run_id, None)
        if session is not None:
            session.close_events()
        return session

    def get(self, run_id: str) -> Optional[RunSession]:
        return self._sessions.get(run_id)

    # ── authenticated lookup ───────────────────────────────────────────────
    def authenticate(self, run_id: str, presented_token: str) -> Tuple[Optional[RunSession], str]:
        """Return ``(session, "")`` on success or ``(None, reason)`` on failure.

        The reason is deliberately coarse ("unknown or expired run") so a caller
        probing the endpoint cannot distinguish "no such run" from "wrong token"
        and enumerate live runs.
        """
        session = self._sessions.get(run_id)
        if session is None:
            return None, "unknown or expired run"
        if session.is_expired():
            self.revoke(run_id)
            return None, "unknown or expired run"
        if not session.token_matches(presented_token):
            return None, "unknown or expired run"
        return session, ""

    # ── housekeeping ───────────────────────────────────────────────────────
    def sweep_expired(self) -> int:
        """Drop sessions past their TTL. Returns how many were removed."""
        now = time.time()
        stale = [rid for rid, s in self._sessions.items() if s.is_expired(now)]
        for rid in stale:
            self.revoke(rid)
        return len(stale)

    def active_count(self) -> int:
        return len(self._sessions)

    def clear(self) -> None:
        """Revoke every session (shutdown / test teardown)."""
        for run_id in list(self._sessions):
            self.revoke(run_id)


# Module-level singleton. The router and the runner must share one registry, and
# both live in this process.
_REGISTRY: Optional[SessionRegistry] = None


def get_registry() -> SessionRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = SessionRegistry()
    return _REGISTRY


__all__ = [
    "TOOL_EVENT_START",
    "TOOL_EVENT_RESULT",
    "ToolEvent",
    "RunSession",
    "SessionRegistry",
    "get_registry",
]
