# SPDX-License-Identifier: Apache-2.0
"""SharedBlackboard — asyncio-safe per-swarm shared workspace.

This is the missing piece in today's delegation layer: ``dispatch_delegation``
runs every sub-agent with ``history=[]``, so two sub-agents on the same
parent turn cannot see each other's work. The blackboard fixes that for
swarm workers — they can read findings from peers (when
``shared_memory_policy != "off"``) and write structured artefacts the
aggregator will later reduce.

Design properties:

* **Channels, not a single log.** ``append(role_id, channel, payload)``
  lets the orchestrator partition information ("findings", "candidates",
  "errors", "files") so the next worker can read a focused slice instead
  of scrolling everything every peer ever wrote.

* **Per-channel locking.** One ``asyncio.Lock`` per channel. Two workers
  writing to different channels never block each other; two workers
  writing to the same channel serialise on a fine-grained lock instead
  of a global one.

* **Snapshot reads.** ``read()`` always returns a shallow copy. No worker
  can mutate another worker's entry by holding onto a reference.

* **Bounded.** Each channel is capped at
  ``_BLACKBOARD_MAX_ENTRIES_PER_CHANNEL`` (default 200). On overflow we
  drop the oldest entry with a structured warning rather than crashing
  the run. Tunable via env ``SWARM_BLACKBOARD_PER_CHANNEL_MAX``.

* **In-process only in v1.** No Redis, no Postgres. A process restart
  loses the blackboard — consistent with v1's "swarms are not resumable"
  scope.

* **Character-budgeted digest.** ``summary_view()`` produces the digest
  the orchestrator/aggregator embed in their prompts. Char budgets, no
  tiktoken — same pattern as ``kb_retriever._format_context``.
"""
from __future__ import annotations

import asyncio

import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.logger import logger
# ---------------------------------------------------------------------------
# Config knobs
# ---------------------------------------------------------------------------

_BLACKBOARD_MAX_ENTRIES_PER_CHANNEL = int(
    os.getenv("SWARM_BLACKBOARD_PER_CHANNEL_MAX", "200")
)
_DEFAULT_SUMMARY_MAX_CHARS = int(os.getenv("SWARM_SUMMARY_MAX_CHARS", "4000"))
_PER_ENTRY_PREVIEW_CHARS = int(os.getenv("SWARM_SUMMARY_PER_ENTRY_CHARS", "400"))


# ---------------------------------------------------------------------------
# Entry shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BlackboardEntry:
    """One immutable entry. ``index`` is monotonic per channel."""
    role_id:  str
    channel:  str
    index:    int
    payload:  Any
    ts:       float = field(default_factory=time.time)

    @property
    def entry_id(self) -> str:
        """Stable id the aggregator cites in its ``sources`` array."""
        return f"{self.role_id}#{self.channel}#{self.index}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role_id":  self.role_id,
            "channel":  self.channel,
            "index":    self.index,
            "entry_id": self.entry_id,
            "ts":       self.ts,
            "payload":  self.payload,
        }


# ---------------------------------------------------------------------------
# Blackboard
# ---------------------------------------------------------------------------

class SharedBlackboard:
    """Per-swarm-run shared workspace.

    One instance per ``SwarmRuntime.execute`` call; lifetime is bounded
    by that call. The runtime never reuses a blackboard across runs.
    """

    def __init__(self, run_id: str):
        self._run_id = run_id
        # channel -> list of entries (append-only within the cap)
        self._entries: Dict[str, List[BlackboardEntry]] = defaultdict(list)
        # channel -> lock for the entries list above
        self._locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # role_id -> list of {filename, mime, content_b64 | path}
        self._artifacts: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._artifacts_lock = asyncio.Lock()
        # Truthy when at least one entry was ever appended.
        self._touched = False

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def touched(self) -> bool:
        return self._touched

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    async def append(self, role_id: str, channel: str, payload: Any) -> BlackboardEntry:
        """Append ``payload`` to ``channel`` under ``role_id``.

        Returns the persisted entry. The entry's ``index`` is the new
        length of the channel after the append, minus one.

        Overflow: when a channel reaches the cap, the OLDEST entry is
        dropped with a logged warning. We do NOT raise — a worker that
        wrote a long log shouldn't break the swarm.
        """
        if not channel:
            raise ValueError("blackboard.append: channel must be non-empty")
        if not role_id:
            raise ValueError("blackboard.append: role_id must be non-empty")

        lock = self._locks[channel]
        async with lock:
            entries = self._entries[channel]
            if len(entries) >= _BLACKBOARD_MAX_ENTRIES_PER_CHANNEL:
                dropped = entries.pop(0)
                logger.warning(f"[AGENT] swarm blackboard '{self._run_id}' channel '{channel}' overflowed cap={_BLACKBOARD_MAX_ENTRIES_PER_CHANNEL}; dropped oldest entry from role '{dropped.role_id}' index={dropped.index}")
            entry = BlackboardEntry(
                role_id=role_id, channel=channel,
                index=len(entries), payload=payload,
            )
            entries.append(entry)
            self._touched = True
            return entry

    async def put_artifact(self, role_id: str, file: Dict[str, Any]) -> None:
        """Record a worker-generated file for later inclusion in the envelope.

        ``file`` should match the shape ``AgentRunner.run`` returns under
        ``generated_files``: {filename, mime?, path? | content_b64?}.
        We don't validate — the aggregator and the parent will.
        """
        if not isinstance(file, dict) or not file.get("filename"):
            return
        async with self._artifacts_lock:
            self._artifacts[role_id].append(dict(file))

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def read(self, channel: str, *, since: int = 0) -> List[BlackboardEntry]:
        """Snapshot copy of every entry in ``channel`` with index >= ``since``.

        Always returns a new list — no shared mutation. Holds the
        channel lock for the duration of the slice (microseconds) so
        the snapshot is internally consistent.
        """
        lock = self._locks[channel]
        async with lock:
            entries = self._entries.get(channel, [])
            return [e for e in entries if e.index >= since]

    async def channels(self) -> List[str]:
        """List of channels that have at least one entry."""
        # No lock needed for a key snapshot — dict.keys() under the GIL
        # is safe, and we materialise immediately to avoid the lazy view
        # being mutated underneath us.
        return [c for c in list(self._entries.keys()) if self._entries[c]]

    def artifacts(self) -> List[Dict[str, Any]]:
        """Return every recorded artifact across all roles.

        Order: by role_id then by insertion order within the role.
        Not async — the aggregator calls this AFTER all workers finished,
        so no contention.
        """
        out: List[Dict[str, Any]] = []
        for role_id in sorted(self._artifacts.keys()):
            for f in self._artifacts[role_id]:
                copy = dict(f)
                copy.setdefault("role_id", role_id)
                out.append(copy)
        return out

    # ------------------------------------------------------------------
    # Snapshot for worker history injection
    # ------------------------------------------------------------------
    def snapshot(self, *, max_chars: Optional[int] = None) -> List[Dict[str, Any]]:
        """Chat-history-shaped snapshot for the next worker's ``runner.run``.

        ``AgentRunner.run`` accepts ``history: list[{role, content}]``.
        We render the blackboard as a single assistant message containing
        a structured digest — keeps the history short and prevents the
        worker LLM from being confused by many fake "assistant" turns.

        When ``broadcast`` and the blackboard is empty we return ``[]``
        so the worker doesn't see an "assistant: nothing here yet" turn.
        """
        digest = self.summary_view(max_chars=max_chars)
        if not digest.strip():
            return []
        return [{
            "role": "assistant",
            "content": (
                "[SHARED_BLACKBOARD DIGEST]\n"
                "The following findings have been written by your peers so far. "
                "Use them as grounded context; cite their entry_id when you build on them.\n\n"
                + digest
            ),
        }]

    def summary_view(self, *, max_chars: Optional[int] = None) -> str:
        """Char-budgeted digest used by orchestrator/aggregator/worker history.

        Order: chronological by ``ts``. Each entry truncated to
        ``_PER_ENTRY_PREVIEW_CHARS``. If the total exceeds the budget,
        the OLDEST entries are dropped first — recent context is what
        the next worker needs.
        """
        budget = int(max_chars or _DEFAULT_SUMMARY_MAX_CHARS)
        # Flatten every channel into one chronological list.
        flat: List[BlackboardEntry] = []
        for entries in self._entries.values():
            flat.extend(entries)
        flat.sort(key=lambda e: e.ts)

        # Render newest-first so we can drop from the tail (oldest) when
        # over budget. After we know which entries fit, we re-order back
        # to chronological for the output.
        rendered: List[str] = []
        kept: List[BlackboardEntry] = []
        total = 0
        for e in reversed(flat):
            preview = _preview_payload(e.payload, _PER_ENTRY_PREVIEW_CHARS)
            line = f"  [{e.entry_id}] {preview}"
            if total + len(line) + 1 > budget:
                break
            rendered.append(line)
            kept.append(e)
            total += len(line) + 1

        # Re-order back to chronological for output (callers expect
        # earlier-first to match the wall-clock order of worker writes).
        rendered.reverse()
        if not rendered:
            return ""
        return "\n".join(rendered)

    # ------------------------------------------------------------------
    # Aggregator helper
    # ------------------------------------------------------------------
    def flat_entries(self) -> List[Dict[str, Any]]:
        """Every entry as a flat list of dicts, chronological.

        Used by the aggregator to build its prompt — it needs the raw
        payloads, not the truncated previews. The aggregator may itself
        truncate before sending to the LLM if the total exceeds its own
        budget.
        """
        flat: List[BlackboardEntry] = []
        for entries in self._entries.values():
            flat.extend(entries)
        flat.sort(key=lambda e: e.ts)
        return [e.to_dict() for e in flat]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _preview_payload(payload: Any, max_chars: int) -> str:
    """Render a payload as a single line for the digest.

    Dicts/lists go through ``json.dumps`` (default=str to handle datetime
    etc.); plain strings are passed through. Anything else is
    ``str(...)``-coerced. Truncation marker appended when cut.
    """
    import json
    if isinstance(payload, str):
        s = payload
    else:
        try:
            s = json.dumps(payload, default=str, ensure_ascii=False)
        except Exception:
            s = str(payload)
    # Collapse internal newlines so each entry stays on one line.
    s = s.replace("\n", " ").replace("\r", " ")
    if len(s) > max_chars:
        s = s[: max_chars - 12].rstrip() + " …(trunc)"
    return s


__all__ = ["SharedBlackboard", "BlackboardEntry"]
