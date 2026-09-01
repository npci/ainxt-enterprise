# SPDX-License-Identifier: Apache-2.0
"""
Checkpoint store — abstract interface and file-backed implementation.

CheckpointStore (abstract base class)
  Defines the contract every storage backend must fulfil:
    startup / shutdown    — lifecycle hooks called by the engine
    save_messages         — persist a full thread's message list
    load_messages         — retrieve messages for a thread
    list_threads          — all threads for a workflow, newest first
    delete_thread         — remove a thread

FileCheckpointStore (default implementation)
  Persists chat history as a single JSON file. No external dependencies —
  ideal for local development and single-instance deployments.

  Concurrency: thread-safe via threading.Lock with atomic writes
  (write to .tmp then os.replace, so the file is never half-written).

  Default path: <backend_root>/data/chat_history.json
  Override by passing path= to the constructor.

The PostgreSQL backend lives in postgres_store.py and is activated by
setting POSTGRES_HOST in .env (it shares the platform's connection pool).

Used by: native_engine.py
"""

from __future__ import annotations
import asyncio
import json

import os
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.logger import logger
# ===========================================================================
# Shared data types
# ===========================================================================

@dataclass
class ChatMessage:
    role:    str   # "user" | "assistant"
    content: str
    # Optional generated files attached to an assistant message (e.g.
    # PowerPoint / PDF artefacts produced by pptx_creator / code_executor).
    # Each entry is the dict shape emitted by the tool layer:
    #   {"filename": str, "download_url": str, "format": str, "path": str}
    # Persisted alongside the message so the chat panel can re-render the
    # FileDownloadCard chip strip after a page reload.
    generated_files: Optional[List[Dict[str, Any]]] = None
    # Optional accumulated usage for an assistant message (model, tokens_in,
    # tokens_out, cost_usd, latency_ms). Persisted so the usage footer
    # re-renders on thread reload.
    usage: Optional[Dict[str, Any]] = None
    # Wall-clock seconds for the full run — shown as a duration chip in the
    # message action bar. Persisted so it survives page reload.
    duration_s: Optional[int] = None


@dataclass
class ThreadSummary:
    thread_id:              str
    title:                  str
    last_message_preview:   str
    last_updated:           Optional[str]
    message_count:          int
    has_pending_interrupt:  bool = False
    # Reason of the pending interrupt, if any. Values mirror the engine
    # snapshot ``reason`` field: "ask_human", "before_tool", "after_response",
    # "subflow_pending", "node_failed", or "" when no pause exists. The
    # sidebar uses this to render a distinct "failed" badge for
    # ``node_failed`` versus the amber HITL badge for the others.
    pending_reason:         str = ""


def summarise_thread(
    thread_id: str,
    raw_messages: list,
    last_updated_iso: Optional[str],
    has_pending_interrupt: bool = False,
    pending_reason: str = "",
) -> ThreadSummary:
    """Derive a sidebar-ready ThreadSummary from a raw message list.

    Shared by the workflow chat store (chat_threads) and the agent chat
    store (agent_chat_threads). Keep the title/preview heuristics in one
    place so the two sidebars stay visually consistent.
    """
    user_msgs      = [m for m in raw_messages if m["role"] == "user"]
    assistant_msgs = [m for m in raw_messages if m["role"] == "assistant"]
    first_user  = user_msgs[0]["content"]       if user_msgs      else "New chat"
    last_assist = assistant_msgs[-1]["content"] if assistant_msgs else ""
    title   = (first_user[:60]  + "...") if len(first_user)  > 60 else first_user
    preview = (last_assist[:80] + "...") if len(last_assist) > 80 else last_assist
    return ThreadSummary(
        thread_id=thread_id,
        title=title or "New chat",
        last_message_preview=preview,
        last_updated=last_updated_iso,
        message_count=len(raw_messages),
        has_pending_interrupt=has_pending_interrupt,
        pending_reason=pending_reason or "",
    )


# ===========================================================================
# Abstract interface
# ===========================================================================

class CheckpointStore(ABC):

    @abstractmethod
    async def startup(self) -> None:
        """Initialise connections / load state. Called once on app startup."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Release resources. Called once on app shutdown."""

    @abstractmethod
    async def save_messages(
        self, thread_id: str, workflow_id: str, messages: List[ChatMessage],
        owner_user_id: Optional[str] = None,
    ) -> None:
        """Persist a complete message list for a thread (overwrites previous).

        ``owner_user_id`` (security review F-06/F-10) is recorded on first
        write and never overwritten afterwards — see backend implementations.
        None means "caller didn't supply an owner" (legacy call site); the
        row keeps whatever owner it already has, or is created ownerless.
        """

    @abstractmethod
    async def load_messages(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> List[ChatMessage]:
        """Return all messages for the thread, or [] if the thread doesn't
        exist OR (when ``owner_user_id`` is given) belongs to a different
        owner. None skips the ownership check (legacy / internal callers).
        """

    async def get_thread_owner(self, thread_id: str) -> Optional[str]:
        """Return the thread's recorded owner, "" if the thread exists but
        has no recorded owner (pre-migration row), or None if the thread
        doesn't exist at all.

        Security review F-06/F-10 follow-up: the run entrypoints
        (/run, /run-stream, /resume-stream in app/api/execution.py) accept a
        client-supplied ``thread_id`` and, before this method existed, wrote
        into / read from it with no ownership check at all — Bob posting
        Alice's thread_id would append his turn into her history and see
        her prior messages in his prompt. Those routes call this method
        BEFORE building the run's ExecutionContext and reject the request
        (403) when the thread exists and is owned by someone else. A brand
        new thread_id (returns None) or a pre-migration/ownerless one
        (returns "") is allowed to proceed — the write path then stamps the
        caller as owner via the normal save_messages() first-write rule.
        Default no-op returns None so legacy/limited stores keep working.
        """
        return None

    @abstractmethod
    async def list_threads(
        self, workflow_id: str, owner_user_id: Optional[str] = None,
    ) -> List[ThreadSummary]:
        """Return summaries for all threads belonging to a workflow, newest
        first. When ``owner_user_id`` is given, only that owner's threads
        are returned.
        """

    @abstractmethod
    async def delete_thread(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> bool:
        """Delete a thread and all its messages.

        Returns True if a row was deleted. When ``owner_user_id`` is given
        and doesn't match the thread's recorded owner, the delete is refused
        (returns False) rather than silently deleting another user's thread.
        """

    async def delete_threads_for_workflow(self, workflow_id: str) -> int:
        """Delete every thread (and all dependent rows) for a workflow.

        Called when a workflow itself is deleted so its chat history,
        HITL snapshots and audit trails don't linger as orphans keyed by
        a now-dead workflow_id. Returns the number of chat threads removed.

        Not owner-scoped: deleting the parent workflow already implies the
        caller owns it (enforced by the workflow-delete route), so every
        thread under it is fair game regardless of who created it.

        Default no-op returns 0 so legacy/limited stores keep working;
        concrete backends override this to cascade the delete.
        """
        return 0

    # ---------------- HITL pending interrupts ----------------
    #
    # A "pending interrupt" is a snapshot of an in-flight workflow run that
    # has been paused by Human-in-the-Loop. It's keyed by thread_id so a
    # disconnected client can reattach via /threads/{thread_id}/pending and
    # /resume-stream. Default no-op implementations mean legacy stores keep
    # working; backends that support HITL override these.

    async def save_pending_interrupt(
        self, thread_id: str, snapshot: Dict[str, Any],
        owner_user_id: Optional[str] = None,
    ) -> None:
        """Persist a HITL snapshot for a paused run. Overwrites any prior."""
        return None

    async def load_pending_interrupt(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return the pending interrupt snapshot, or None if not paused (or
        owned by someone else, when ``owner_user_id`` is given).
        """
        return None

    async def delete_pending_interrupt(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> None:
        """Clear the pending interrupt (called on resume / cancel)."""
        return None

    # ---------------- Per-node last outputs ----------------
    #
    # Keyed by (thread_id, node_id). Powers the connection-aware Loop node
    # picker: when a user wires Loop ← Agent and opens the Loop config, the
    # frontend fetches the upstream agent's last actual output so it can
    # surface lists as click-to-pick options instead of demanding a typed
    # dotted path. Default no-op so legacy stores keep working.

    async def save_node_output(
        self,
        thread_id: str,
        workflow_id: str,
        node_id: str,
        agent: str,
        output: str,
        owner_user_id: Optional[str] = None,
    ) -> None:
        """Persist the latest output produced by node_id during this thread."""
        return None

    async def load_node_output(
        self, thread_id: str, node_id: str, owner_user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return {agent, output, updated_at} for the last run of node_id, or
        None (including when ``owner_user_id`` is given and doesn't match).
        """
        return None

    # ---------------- Loop / Condition / HITL audit trails ----------------
    #
    # Per-iteration loop diagnostics, per-decision condition routings, and
    # per-resume HITL decisions are emitted by the engine as SSE events but
    # were previously discarded after streaming. These best-effort persistence
    # hooks let backends record them for audit / replay without changing the
    # engine's streaming behaviour. Default no-ops so backends that don't
    # support them simply drop the writes.

    async def save_loop_iteration(
        self,
        thread_id: str,
        workflow_id: str,
        node_id: str,
        index: int,
        mode: str,
        total: Optional[int] = None,
        score: Optional[float] = None,
        changes: Optional[str] = None,
        will_continue: Optional[bool] = None,
        case_results: Optional[list] = None,
        output_preview: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ) -> None:
        """Persist a single loop iteration's diagnostic record."""
        return None

    async def save_condition_routing(
        self,
        thread_id: str,
        workflow_id: str,
        node_id: str,
        matched_case_id: Optional[str],
        matched_label: Optional[str],
        matched_expression: Optional[str],
        upstream_output_preview: Optional[str],
        evaluated_state: Optional[Dict[str, Any]],
        target_node_id: Optional[str],
        owner_user_id: Optional[str] = None,
    ) -> None:
        """Persist a single condition-node routing decision."""
        return None

    async def save_hitl_decision(
        self,
        thread_id: str,
        workflow_id: str,
        node_id: str,
        reason: str,
        hitl_mode: str,
        decision: str,
        human_input: str,
        user_id: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ) -> None:
        """Persist a single HITL resume decision for audit.

        ``user_id`` is the acting user (who decided); ``owner_user_id`` is the
        thread owner used for tenant scoping. Usually identical, but an admin
        resuming another user's run must be audited as the actor without
        re-homing the row into their own scope.
        """
        return None

    # ---------------- Loop cross-run memory (lessons) ----------------
    #
    # When a Loop node has memory.write enabled, the engine persists a
    # compact reflection ("lesson") after each run keyed by
    # (workflow_id, node_id). A later run of the SAME loop with memory.read
    # enabled fetches those lessons and injects them into the body agents'
    # prompt via ``{{loop.prior_lessons}}`` so the loop learns across runs.
    # Default no-ops so backends that don't support it simply skip memory.

    async def save_loop_lesson(
        self, workflow_id: str, node_id: str, digest: str
    ) -> None:
        """Persist one loop reflection digest for future runs to read."""
        return None

    async def load_loop_lessons(
        self, workflow_id: str, node_id: str
    ) -> Optional[str]:
        """Return recent lesson digests for this loop joined into one string,
        or None when memory is unsupported / empty.
        """
        return None

    # ---------------- FR-T0-3: durable replay (run_steps / run_events) ----
    #
    # Authoritative per-step state + append-only event log for durable resume,
    # crash survival, and deterministic replay. Default no-ops so file-backed
    # / in-memory backends simply skip durability (they never survive restart
    # anyway); PostgresCheckpointStore overrides all four.

    async def save_run_step(
        self,
        thread_id: str,
        workflow_id: str,
        step_index: int,
        node_id: str,
        node_type: str,
        status: str,
        *,
        attempt: int = 0,
        input_snapshot: Optional[Dict[str, Any]] = None,
        output_ref: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ) -> None:
        """Upsert authoritative per-step run state (REQ-D1)."""
        return None

    async def load_run_state(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return all steps for a run ordered by step_index (REQ-D1/D3/D5).

        ``owner_user_id`` scopes the read to that user's own run; callers on
        any request-handling path must pass it, since ``input_snapshot``
        holds verbatim node input.
        """
        return []

    async def append_run_event(
        self,
        thread_id: str,
        workflow_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        step_index: Optional[int] = None,
        owner_user_id: Optional[str] = None,
    ) -> None:
        """Append one event to the ordered run event log (REQ-D2)."""
        return None

    async def replay_events(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return the full ordered event log for a run (REQ-D2/D3).

        ``owner_user_id`` scopes the read — see ``load_run_state``. Event
        payloads embed node output, so any HTTP-facing caller must pass the
        authenticated user's id.
        """
        return []


# ===========================================================================
# File-backed implementation
# ===========================================================================

_DEFAULT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "chat_history.json")
)

# Sentinel distinct from any real dict value, used so dict.pop(key, _SENTINEL)
# can tell "key was absent" apart from "key was present with value None".
_SENTINEL = object()


class FileCheckpointStore(CheckpointStore):
    """
    Simple JSON file checkpoint store.
    Schema: { thread_id: { workflow_id, messages: [{role, content}], last_updated } }
    """

    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: Dict[str, dict] = {}

    async def startup(self) -> None:
        await asyncio.to_thread(self._load)

    async def shutdown(self) -> None:
        pass   # file is written on every save; nothing to flush here

    def _load(self) -> None:
        if not os.path.exists(self._path):
            self._data = {}
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            # Security review F-06/F-10 follow-up: stamp legacy_no_owner=True,
            # ONCE, for records that predate the ownership migration —
            # distinguished by the "owner_user_id" key being absent entirely
            # (json.load reads exactly what was on disk; an old file written
            # before this fix never had this key). This runs once per
            # process startup right after load, mirroring
            # PostgresCheckpointStore's one-time _is_first_run backfill, so
            # a record created or saved AFTER this point (which always gets
            # an explicit "owner_user_id" key, even if the value is None)
            # is never retroactively marked legacy — keeping the
            # "NULL owner is accessible" rule bounded to true pre-migration
            # data instead of a permanent fail-open for any future
            # NULL-owner row.
            for _record in self._data.values():
                if isinstance(_record, dict) and "owner_user_id" not in _record:
                    _record["owner_user_id"] = None
                    _record["legacy_no_owner"] = True
            logger.info(f'[AGENT] FileCheckpointStore loaded {len(self._data)} threads from {self._path}')
        except Exception as e:
            logger.warning(f'[AGENT] FileCheckpointStore could not load {self._path}: {e}')
            self._data = {}

    def _flush(self) -> None:
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self._path), exist_ok=True)
                tmp = self._path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2, default=str)
                os.replace(tmp, self._path)
            except Exception as e:
                logger.warning(f'[AGENT] FileCheckpointStore flush failed: {e}')

    def _denied_for_owner(self, record: Optional[dict], owner_user_id: Optional[str]) -> bool:
        """True when ``owner_user_id`` is given and access should be denied.

        Access is allowed ONLY when the recorded owner matches.

        Bug fix (workflow chat cross-user visibility): this used to also
        allow ``legacy_no_owner`` records (pre-migration threads with no
        recorded owner), which let every user of a shared workflow
        read/list/delete threads created by anyone else. Because workflows
        are shared objects (many users open the same workflow_id), that
        fail-open surfaced as "anyone can see anyone's chat". Legacy records
        carry no owner, so denying them here makes them inaccessible to
        everyone — matching the strictly owner-scoped agent chat store,
        which never had this fail-open. The ``legacy_no_owner`` flag is still
        written by ``save_messages`` / ``_load`` for auditing but is no
        longer consulted for access decisions.
        """
        if owner_user_id is None or not record:
            return False
        existing_owner = record.get("owner_user_id")
        if existing_owner == owner_user_id:
            return False
        return True

    async def save_messages(
        self, thread_id: str, workflow_id: str, messages: List[ChatMessage],
        owner_user_id: Optional[str] = None,
    ) -> None:
        from datetime import datetime, timezone
        # Preserve any existing pending_interrupt and per-node outputs so
        # saving messages mid-pause does not clobber the HITL snapshot or
        # erase the cached node outputs used by the Loop picker.
        existing = self._data.get(thread_id) or {}

        def _serialize(m: ChatMessage) -> Dict[str, Any]:
            payload: Dict[str, Any] = {"role": m.role, "content": m.content}
            # Only emit generated_files when present so old messages stay
            # bit-identical and the on-disk JSON doesn't grow `null` fields.
            if m.generated_files:
                payload["generated_files"] = m.generated_files
            if m.usage:
                payload["usage"] = m.usage
            if m.duration_s is not None:
                payload["duration_s"] = m.duration_s
            return payload

        self._data[thread_id] = {
            "workflow_id":       workflow_id,
            # Record the owner on first write; never overwrite it afterwards
            # so a later call without owner_user_id (or from a different
            # user, which _denied_for_owner would have already rejected
            # upstream) can't silently reassign ownership.
            "owner_user_id":     existing.get("owner_user_id") or owner_user_id,
            # Explicitly False on every write (as opposed to absent, which
            # _load() would otherwise mark legacy on the NEXT restart) —
            # any record touched by this code path was written after the
            # ownership migration shipped, so it is never a legacy row
            # regardless of whether owner_user_id ended up populated.
            "legacy_no_owner":   False,
            "messages":          [_serialize(m) for m in messages],
            "last_updated":      datetime.now(timezone.utc).isoformat(),
            "pending_interrupt": existing.get("pending_interrupt"),
            "node_outputs":      existing.get("node_outputs") or {},
        }
        await asyncio.to_thread(self._flush)

    async def load_messages(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> List[ChatMessage]:
        record = self._data.get(thread_id)
        if not record or self._denied_for_owner(record, owner_user_id):
            return []
        return [
            ChatMessage(
                role=m["role"],
                content=m["content"],
                generated_files=m.get("generated_files") or None,
                usage=m.get("usage") or None,
                duration_s=m.get("duration_s"),
            )
            for m in record.get("messages", [])
        ]

    async def get_thread_owner(self, thread_id: str) -> Optional[str]:
        record = self._data.get(thread_id)
        if not record:
            return None
        return record.get("owner_user_id") or ""

    async def list_threads(
        self, workflow_id: str, owner_user_id: Optional[str] = None,
    ) -> List[ThreadSummary]:
        summaries = [
            summarise_thread(
                tid,
                record.get("messages", []),
                record.get("last_updated"),
                has_pending_interrupt=bool(record.get("pending_interrupt")),
                # Extract snapshot.reason so the sidebar can tell an HITL
                # pause apart from a node_failed pause without an extra
                # /chat-pending fetch.
                pending_reason=(
                    (record.get("pending_interrupt") or {}).get("reason")
                    or ""
                ),
            )
            for tid, record in self._data.items()
            if record.get("workflow_id") == workflow_id
            and not self._denied_for_owner(record, owner_user_id)
        ]
        summaries.sort(key=lambda t: t.last_updated or "", reverse=True)
        return summaries

    async def delete_thread(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> bool:
        record = self._data.get(thread_id)
        if not record or self._denied_for_owner(record, owner_user_id):
            return False
        # Return value driven by pop's own result (sentinel default) rather
        # than assumed from the existence check above, so this stays
        # correct even if the method is refactored later — mirrors
        # PostgresCheckpointStore.delete_thread's rowcount-based truth
        # rather than an inferred True.
        removed = self._data.pop(thread_id, _SENTINEL) is not _SENTINEL
        if removed:
            await asyncio.to_thread(self._flush)
        return removed

    async def delete_threads_for_workflow(self, workflow_id: str) -> int:
        # A thread's record carries its owning workflow_id (and every
        # dependent bit — messages, pending_interrupt, node_outputs — is
        # nested inside that same record), so dropping the matching top-level
        # keys removes all chat history for the workflow in one pass.
        with self._lock:
            victims = [
                tid for tid, record in self._data.items()
                if record.get("workflow_id") == workflow_id
            ]
            for tid in victims:
                self._data.pop(tid, None)
        if victims:
            await asyncio.to_thread(self._flush)
        return len(victims)

    # ---------------- HITL pending interrupts ----------------

    async def save_pending_interrupt(
        self, thread_id: str, snapshot: Dict[str, Any],
        owner_user_id: Optional[str] = None,
    ) -> None:
        from datetime import datetime, timezone
        existing = self._data.get(thread_id) or {}
        record = existing or {
            "workflow_id":  snapshot.get("workflow_id", ""),
            "messages":     [],
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        record.setdefault("owner_user_id", existing.get("owner_user_id") or owner_user_id)
        # setdefault (not unconditional False) so a genuinely legacy record
        # loaded from disk with legacy_no_owner=True (stamped by _load())
        # keeps that flag; only a brand-new record (no key yet) gets False.
        record.setdefault("legacy_no_owner", False)
        record["pending_interrupt"] = snapshot
        self._data[thread_id] = record
        await asyncio.to_thread(self._flush)

    async def load_pending_interrupt(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        record = self._data.get(thread_id) or {}
        if self._denied_for_owner(record, owner_user_id):
            return None
        return record.get("pending_interrupt") or None

    async def delete_pending_interrupt(
        self, thread_id: str, owner_user_id: Optional[str] = None,
    ) -> None:
        record = self._data.get(thread_id)
        if not record or self._denied_for_owner(record, owner_user_id):
            return
        if "pending_interrupt" in record:
            record["pending_interrupt"] = None
            await asyncio.to_thread(self._flush)

    # ---------------- Per-node last outputs ----------------

    async def save_node_output(
        self,
        thread_id: str,
        workflow_id: str,
        node_id: str,
        agent: str,
        output: str,
        owner_user_id: Optional[str] = None,
    ) -> None:
        from datetime import datetime, timezone
        # Take the lock for the read-mutate so a concurrent save_messages /
        # save_pending_interrupt on the same thread can't see a partially
        # updated record (the lock is re-acquired inside _flush — Lock is
        # non-reentrant, so the mutation and the I/O are split).
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            record = self._data.get(thread_id) or {
                "workflow_id":  workflow_id,
                "messages":     [],
                "last_updated": now_iso,
            }
            record.setdefault("owner_user_id", record.get("owner_user_id") or owner_user_id)
            record.setdefault("legacy_no_owner", False)
            outputs = dict(record.get("node_outputs") or {})
            outputs[node_id] = {
                "agent":      agent,
                "output":     output,
                "updated_at": now_iso,
            }
            record["node_outputs"] = outputs
            self._data[thread_id] = record
        await asyncio.to_thread(self._flush)

    async def load_node_output(
        self, thread_id: str, node_id: str, owner_user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        record = self._data.get(thread_id) or {}
        if self._denied_for_owner(record, owner_user_id):
            return None
        outputs = record.get("node_outputs") or {}
        return outputs.get(node_id)

    # ---------------- Loop cross-run memory (lessons) ----------------
    #
    # Stored under a synthetic top-level key so it doesn't collide with any
    # thread_id (thread ids are UUIDs; "__loop_lessons__" is not). Keyed by
    # "workflow_id:node_id"; keeps only the most recent few digests.

    _LESSONS_KEY = "__loop_lessons__"
    _LESSONS_MAX = 5

    async def save_loop_lesson(
        self, workflow_id: str, node_id: str, digest: str
    ) -> None:
        from datetime import datetime, timezone
        if not digest:
            return
        bucket = self._data.setdefault(self._LESSONS_KEY, {})
        key = f"{workflow_id}:{node_id}"
        entries = bucket.get(key) or []
        entries.append({
            "digest": str(digest),
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        # Keep only the newest N so the file doesn't grow unbounded.
        bucket[key] = entries[-self._LESSONS_MAX:]
        await asyncio.to_thread(self._flush)

    async def load_loop_lessons(
        self, workflow_id: str, node_id: str
    ) -> Optional[str]:
        bucket = self._data.get(self._LESSONS_KEY) or {}
        entries = bucket.get(f"{workflow_id}:{node_id}") or []
        if not entries:
            return None
        digests = [e.get("digest", "") for e in entries if e.get("digest")]
        if not digests:
            return None
        # Cap total length so a long history can't blow the agent prompt.
        return "\n---\n".join(digests)[:4000]
