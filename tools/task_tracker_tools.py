# SPDX-License-Identifier: Apache-2.0
"""
AiNxt Agentic Platform — task_tracker MCP tools.

Create / list / update tasks on a configurable backend. Used by UC-57
(action items from meeting notes), UC-65 (onboarding checklist), UC-86
(delegation from inbox triage).

Default provider is a flat JSON file under the outbox — ready for demos and
drop-in replacement by Jira/Asana adapters that keep the same tool contract.

Functions exposed:
  create_task  — create a task with title/owner/due/details
  list_tasks   — list tasks, optionally filtered by status / owner
  update_task  — update a task's status / owner / due date

Companion server: mcp/servers/task_tracker_server.py
Registered in:   mcp/registry.py:_register_tools()

Configuration (env vars):
  TASK_TRACKER_STORE_PATH  — JSON file holding the tasks store
                              (default ./outbox/tasks/tasks.json)
"""

import datetime
import json
import os
import uuid
from typing import List


# ── Configuration ────────────────────────────────────────────────────────────

_STORE_PATH = os.getenv(
    "TASK_TRACKER_STORE_PATH",
    "./outbox/tasks/tasks.json",
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load() -> List[dict]:
    if os.path.exists(_STORE_PATH):
        return json.load(open(_STORE_PATH))
    return []


def _save(tasks: List[dict]) -> None:
    os.makedirs(os.path.dirname(_STORE_PATH), exist_ok=True)
    json.dump(tasks, open(_STORE_PATH, "w"), indent=2)


# ── Tool functions ───────────────────────────────────────────────────────────

def create_task(title: str, owner: str = "", due: str = "", details: str = "") -> dict:
    """Create a task with optional owner (email) and due date (YYYY-MM-DD)."""
    tasks = _load()
    t = {
        "id":      str(uuid.uuid4())[:8],
        "title":   title,
        "owner":   owner,
        "due":     due,
        "details": details,
        "status":  "open",
        "created": datetime.date.today().isoformat(),
    }
    tasks.append(t)
    _save(tasks)
    return t


def list_tasks(status: str = "", owner: str = "") -> List[dict]:
    """List tasks, optionally filtered by status (open/done) and/or owner."""
    return [
        t for t in _load()
        if (not status or t["status"] == status)
        and (not owner or t["owner"] == owner)
    ]


def update_task(task_id: str, status: str = "", owner: str = "", due: str = "") -> dict:
    """Update a task's status, owner, or due date."""
    tasks = _load()
    for t in tasks:
        if t["id"] == task_id:
            if status: t["status"] = status
            if owner:  t["owner"]  = owner
            if due:    t["due"]    = due
            _save(tasks)
            return t
    raise ValueError(f"task not found: {task_id}")
